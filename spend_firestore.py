"""Firestore-transaction-class accounting backend (CT-QC-038): offline-proved, not deployed.

Why this exists
---------------
The SQLite backend needs one real shared filesystem with working advisory locks. Cloud Run gives
neither: instances do not share a container filesystem, and Cloud Storage FUSE/NFS mounts do not
provide the POSIX locking and mmap semantics a SQLite write transaction relies on. Requiring a
directory, or requiring "SQLite transactions", therefore does not establish safe accounting
across Cloud Run instances. A store whose transaction semantics are guaranteed *by the service*
does.

What this module relies on (the exact Firestore semantics)
---------------------------------------------------------
1. **One document per deployment** — ``<collection>/spend-<deployment_id>``. The document id is
   derived from the deployment constant only (never pid, host, port, revision or instance id), so
   two revisions, two instances and a replacement process all address one document and therefore
   one ceiling.
2. **A single snapshot read per attempt.** Inside one transaction the client reads the document
   once and evaluates both ceilings against that snapshot. There is no second read and no
   read outside the transaction.
3. **An atomic conditional write in the same transaction**, keyed on the revision observed by
   that snapshot (Firestore: the document's ``update_time`` precondition / ``transaction.create``
   for the first write). If any other writer committed in between, the write is refused as
   contended (Firestore: ``ABORTED``) and nothing is applied.
4. **Deterministic retry on contention only.** The backend re-runs the whole
   read-evaluate-write attempt, up to a configured bound, with a deterministic, jitter-free
   backoff. A retry re-reads, so a reservation is created once; the counters move once.
5. **Missing document = first run** (counters 0). A document that exists with a foreign schema,
   a different deployment, a negative/non-numeric counter or a broken
   ``requests_committed == tracked + evicted`` invariant is **corrupt**, and is never reset.

Interface parity with the SQLite backend is :class:`civictrace.spend_backend.SpendBackend`:
``reserve`` / ``settle`` / ``reconcile`` / ``mark_transmitted`` / ``refund_failed_confirmed`` /
``history`` / ``sweep`` / ``status``. ``settle`` is exactly-once per reservation and never moves
money; ``sweep`` releases a *lease* and leaves the committed unit in place.

CT-QC-039 added the spend lifecycle to both backends (``active`` / ``settled`` /
``failed_confirmed`` / ``uncertain``, see :mod:`civictrace.spend_lifecycle`): a dispatched
reservation is ``uncertain`` until an outcome is recorded, an expired unresolved reservation
stays ``uncertain`` with its allowance consumed, and only ``failed_confirmed`` can be refunded —
by one explicit call. Each transition appends an entry to the document's ``accounting_history``.
That list is a *bounded audit window* (``MAX_ACCOUNTING_HISTORY``) because a Firestore document
is size-capped at 1 MiB; the unbounded append-only record is the SQLite store, and the counters
remain authoritative in both.

The transport seam
------------------
The backend talks to the store only through :class:`BoundedTransport` — a bounded call with two
operations, ``read`` and ``commit``. :func:`build_transport` is the single wiring point where a
real project is connected; since CT-QC-042 it constructs
:class:`civictrace.spend_firestore_client.FirestoreBoundedTransport`, the production adapter over
``google-cloud-firestore`` (pinned in ``requirements.txt`` / ``requirements.lock``). The adapter
issues one Store transaction per attempt and applies the replacement body only if the
transactional snapshot is still at the revision the body was computed from, so the ceiling check
and the write land as one compare-and-swap. Importing the SDK happens only inside that adapter
module, and only when the ``firestore`` backend is actually selected.

Selecting ``CIVICTRACE_SPEND_BACKEND=firestore`` therefore either constructs a real transport or
fails closed with ``LIVE_SPEND_BACKEND_UNAVAILABLE`` — never a silent switch to SQLite.

**Offline proof, not cloud proof.** The deterministic stub store in
:mod:`civictrace.spend_stub_store` replicates the semantics above (snapshot read, revision-checked
atomic apply, injected contention), and the adapter is exercised through dependency-injected
clients that implement the exact SDK surface it calls (``tests/fake_firestore.py``). Together those
tests prove the backend and the adapter logic with no network, no project and no credentials. They
do **not** verify a live Cloud Firestore backend, its IAM, its quotas or its latency: Cloud Run
qualification stays open until deployment-specific evidence exists. See ``CT-COR4A-REPORT.md``.
"""
from __future__ import annotations

import copy
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, TypeVar

from .spend_backend import BACKEND_FIRESTORE, SpendBackend
from .spend_gate import resolve_deployment_id, resolve_instance_id, resolve_reservation_ttl
from .spend_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_FAILED_CONFIRMED,
    LIFECYCLE_SETTLED,
    LIFECYCLE_UNCERTAIN,
    REFUNDABLE_STATES,
    SETTLEMENT_POLICY,
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_SETTLED,
    STATE_SETTLED_EXPIRED,
    TERMINAL_STATES,
    VALID_LIFECYCLE_STATES,
    VALID_STATES,
    Reconciliation,
    bucket_counts,
    classify,
    default_request_digest,
    is_digest,
    request_digest as digest_of_intent,
)
from .usage_ledger import (
    LIVE_REQUEST_BUDGET_EXHAUSTED,
    LIVE_REQUEST_IDENTITY_INVALID,
    LIVE_REQUEST_IDENTITY_REUSED,
    LIVE_RESERVATION_NOT_REFUNDABLE,
    LIVE_RESERVATION_UNKNOWN,
    LIVE_SPEND_BACKEND_CONTENDED,
    LIVE_SPEND_BACKEND_NOT_CONFIGURED,
    LIVE_SPEND_BACKEND_UNAVAILABLE,
    LIVE_SPEND_BUDGET_EXHAUSTED,
    LIVE_USAGE_LEDGER_CORRUPT,
    MAX_RECEIPTS,
    MONEY,
    Admission,
    LiveBudget,
    UsageConfigError,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerUnavailable,
)

T = TypeVar('T')

PROJECT_ENV = 'CIVICTRACE_FIRESTORE_PROJECT'
DATABASE_ENV = 'CIVICTRACE_FIRESTORE_DATABASE'
COLLECTION_ENV = 'CIVICTRACE_FIRESTORE_COLLECTION'
TIMEOUT_ENV = 'CIVICTRACE_SPEND_BACKEND_TIMEOUT_S'
ATTEMPTS_ENV = 'CIVICTRACE_SPEND_BACKEND_ATTEMPTS'

DOCUMENT_SCHEMA = 'civictrace-spend-document/1'
DOCUMENT_SCHEMA_VERSION = 1
DEFAULT_DATABASE = '(default)'
DEFAULT_COLLECTION = 'civictrace-spend'
DEFAULT_TIMEOUT_S = 2.0
MAX_TIMEOUT_S = 10.0
DEFAULT_ATTEMPTS = 5
MAX_ATTEMPTS = 16
CONTENTION_BACKOFF_S = 0.02
MAX_CONTENTION_BACKOFF_S = 0.25

# The reservation map is an audit trail, not the counter: ``requests_committed`` is
# authoritative and is never reduced. The map is bounded so a document cannot grow past the
# 1 MiB Firestore limit; evicted rows are counted so ``requests_committed == tracked + evicted``
# stays checkable.
MAX_TRACKED_RESERVATIONS = 200
# The document's accounting history is a bounded audit window for the same reason. Every
# transition appends a row; the oldest rows are dropped and counted. The unbounded append-only
# record is the SQLite store — this backend's document is size-capped by the vendor.
MAX_ACCOUNTING_HISTORY = 200

_STATE_ACTIVE = STATE_ACTIVE
_STATE_SETTLED = STATE_SETTLED
_STATE_EXPIRED = STATE_EXPIRED
_STATE_SETTLED_EXPIRED = STATE_SETTLED_EXPIRED
_VALID_STATES = VALID_STATES

# Deliberately identical to the SQLite backend's policy text (asserted by the regression suite) so
# an operator reading either store sees one settlement rule.
_SETTLEMENT_POLICY = SETTLEMENT_POLICY

_PROJECT_PATTERN = re.compile(r'^[a-z][a-z0-9:\-]{4,61}[a-z0-9]$')
_DATABASE_PATTERN = re.compile(r'^[A-Za-z0-9._()\-]{1,128}$')
_COLLECTION_PATTERN = re.compile(r'^[A-Za-z0-9._\-]{1,120}$')


class SpendStoreError(Exception):
    """Base class for the store-level failures the transport seam may report."""


class StoreContention(SpendStoreError):
    """The snapshot went stale before the conditional write: retryable, nothing was applied."""


class StoreUnavailable(SpendStoreError):
    """The store could not be reached or did not answer inside the deadline."""


class StoreInvalid(SpendStoreError):
    """The store answered with something this backend cannot trust."""


def _utc() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY))


# -- configuration -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FirestoreConfig:
    """Deployment-time coordinates of the accounting document. No credential is stored here.

    Authentication is expected to be ambient (Application Default Credentials from the Cloud Run
    service account); this module never reads, stores or logs a credential.
    """

    project: str
    database: str = DEFAULT_DATABASE
    collection: str = DEFAULT_COLLECTION

    @classmethod
    def from_env(cls) -> 'FirestoreConfig':
        return cls.build(
            project=os.getenv(PROJECT_ENV),
            database=os.getenv(DATABASE_ENV),
            collection=os.getenv(COLLECTION_ENV),
        )

    @classmethod
    def build(cls, *, project: str | None = None, database: str | None = None,
              collection: str | None = None) -> 'FirestoreConfig':
        raw_project = (project or '').strip()
        if not raw_project:
            raise UsageConfigError(
                LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                f'{PROJECT_ENV} must name the Firestore project of the accounting store; '
                'live inference refused (a Firestore backend without a project is never '
                'replaced by the SQLite backend)')
        if not _PROJECT_PATTERN.match(raw_project):
            raise UsageConfigError(
                LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                f'{PROJECT_ENV} is not a valid project id (6-63 chars, lower-case letters, '
                'digits, "-" or ":", starting with a letter)')
        raw_database = (database or '').strip() or DEFAULT_DATABASE
        if not _DATABASE_PATTERN.match(raw_database):
            raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                                   f'{DATABASE_ENV} is not a valid database id')
        raw_collection = (collection or '').strip() or DEFAULT_COLLECTION
        if (not _COLLECTION_PATTERN.match(raw_collection)
                or raw_collection in {'.', '..'} or '/' in raw_collection):
            raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                                   f'{COLLECTION_ENV} is not a valid collection id')
        return cls(project=raw_project, database=raw_database, collection=raw_collection)

    def document_id(self, deployment_id: str) -> str:
        """``spend-<deployment>`` — the deployment constant is the whole key."""
        return f'spend-{deployment_id}'

    def document_path(self, deployment_id: str) -> str:
        return f'projects/{self.project}/databases/{self.database}/documents/' \
               f'{self.collection}/{self.document_id(deployment_id)}'


def resolve_timeout_s(explicit: float | None = None) -> float:
    if explicit is None:
        raw = os.getenv(TIMEOUT_ENV)
        try:
            explicit = DEFAULT_TIMEOUT_S if raw is None else float(raw)
        except ValueError:
            raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                                   f'{TIMEOUT_ENV} is not a number') from None
    if not (0 < float(explicit) <= MAX_TIMEOUT_S):
        raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                               f'{TIMEOUT_ENV} must be in (0, {MAX_TIMEOUT_S}]')
    return float(explicit)


def resolve_attempts(explicit: int | None = None) -> int:
    if explicit is None:
        raw = os.getenv(ATTEMPTS_ENV)
        try:
            explicit = DEFAULT_ATTEMPTS if raw is None else int(raw)
        except (TypeError, ValueError):
            raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                                   f'{ATTEMPTS_ENV} is not an integer') from None
    if not (1 <= int(explicit) <= MAX_ATTEMPTS):
        raise UsageConfigError(LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                               f'{ATTEMPTS_ENV} must be in [1, {MAX_ATTEMPTS}]')
    return int(explicit)


# -- transport seam ----------------------------------------------------------------------------

class BoundedTransport(ABC):
    """A bounded round trip to the accounting document. This is the whole vendor surface.

    An adapter for a real project implements exactly two operations, each a single bounded call:

    ``read``
        payload ``{'document_id': str}`` →
        ``{'revision': int | None, 'document': dict | None}``
        The revision is the store-assigned concurrency token for the snapshot (Firestore:
        the document's ``update_time``/version); ``None`` and a ``None`` document mean "absent".
    ``commit``
        payload ``{'document_id': str, 'document': dict, 'expect_revision': int | None}`` →
        ``{'status': 'applied', 'revision': int}`` or ``{'status': 'contended'}``.
        ``expect_revision=None`` must be a create-if-absent; a differing revision or an existing
        document is ``contended``. The adapter must let the *store* refuse the write, never
        compare-then-write client-side outside a transaction.

    Raising :class:`StoreUnavailable` (or any timeout/transport exception, which the store layer
    converts) means "no accounting decision was made". Raising :class:`StoreContention` directly
    is allowed and is treated as a retryable abort.
    """

    @abstractmethod
    def call(self, operation: str, payload: dict[str, Any], *, deadline_s: float) -> dict[str, Any]:
        ...


def build_transport(config: FirestoreConfig, **injection: Any) -> BoundedTransport:
    """Construct the production Firestore transport for ``config`` (CT-QC-042).

    The transport is the real adapter in :mod:`civictrace.spend_firestore_client`: it builds a
    ``google-cloud-firestore`` client from the explicit project/database of ``config`` (ambient
    Application Default Credentials, no credential is ever read by this code) and performs the
    conditional write inside one store transaction. Construction is the refusal point for an
    unusable SDK, project, database or credential: every such failure raises
    :class:`UsageLedgerUnavailable` with ``LIVE_SPEND_BACKEND_UNAVAILABLE`` *before* any provider
    execution, and the SQLite backend is not substituted.

    ``**injection`` is the dependency-injection seam used by tests and probes (``client=`` or
    ``client_factory=``); production calls pass only ``config``. No offline double is selectable
    through ``CIVICTRACE_SPEND_BACKEND``.
    """
    try:
        from .spend_firestore_client import FirestoreBoundedTransport
    except Exception as exc:  # a broken installation is a refusal, never a fallback
        raise UsageLedgerUnavailable(
            LIVE_SPEND_BACKEND_UNAVAILABLE,
            f'the Firestore transport adapter is not importable ({type(exc).__name__}); live '
            'inference refused (no provider request was made and the SQLite backend was not '
            'substituted)') from None
    return FirestoreBoundedTransport(config, **injection)


class TransactionView:
    """One attempt: one snapshot, at most one conditional write.

    The mutator reads through :meth:`read_document` and stages a replacement with
    :meth:`write_document`. Nothing reaches the store until the commit, and the commit is applied
    only if the snapshot revision is still current.
    """

    __slots__ = ('document_id', 'revision', '_snapshot', '_pending')

    def __init__(self, document_id: str, revision: int | None, document: dict[str, Any] | None):
        self.document_id = document_id
        self.revision = revision
        self._snapshot = document
        self._pending: dict[str, Any] | None = None

    def read_document(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._snapshot)

    def write_document(self, document: dict[str, Any]) -> None:
        if not isinstance(document, dict):
            raise StoreInvalid('staged document is not a map')
        self._pending = copy.deepcopy(document)

    @property
    def has_write(self) -> bool:
        return self._pending is not None

    @property
    def pending(self) -> dict[str, Any]:
        return {} if self._pending is None else self._pending


class TransactionalStore:
    """Snapshot-read + conditional-write over a :class:`BoundedTransport`.

    One :meth:`transact` call is exactly one attempt; the backend owns the retry loop so the
    retry policy is identical for every store and testable without a cloud project.
    """

    def __init__(self, transport: BoundedTransport, document_id: str, *, timeout_s: float):
        self.transport = transport
        self.document_id = document_id
        self.timeout_s = float(timeout_s)

    def transact(self, work: Callable[[TransactionView], T]) -> T:
        snapshot = self._call('read', {'document_id': self.document_id})
        revision = snapshot.get('revision')
        document = snapshot.get('document')
        if revision is not None and type(revision) is not int:
            raise StoreInvalid('snapshot revision is not an integer')
        if document is not None and not isinstance(document, dict):
            raise StoreInvalid('snapshot document is not a map')
        view = TransactionView(self.document_id, revision, document)
        result = work(view)
        if not view.has_write:
            return result
        reply = self._call('commit', {
            'document_id': self.document_id,
            'document': view.pending,
            'expect_revision': revision,
        })
        status = reply.get('status')
        if status == 'applied':
            return result
        if status == 'contended':
            raise StoreContention(
                f'document {self.document_id!r} changed between the snapshot and the commit')
        raise StoreInvalid(f'commit reply was not understood ({status!r})')

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = self.transport.call(operation, payload, deadline_s=self.timeout_s)
        except SpendStoreError:
            raise
        except (TimeoutError, OSError) as exc:
            raise StoreUnavailable(f'{type(exc).__name__} during {operation}') from None
        except Exception as exc:  # any vendor exception is "no decision", never a free pass
            raise StoreUnavailable(f'{type(exc).__name__} during {operation}') from None
        if not isinstance(reply, dict):
            raise StoreUnavailable(f'{operation} reply is not a map')
        return reply


# -- document state ----------------------------------------------------------------------------

@dataclass
class _DocumentState:
    requests_committed: int
    estimated_spend_usd: Decimal
    reservations: dict[str, dict[str, Any]]
    receipts: list[dict[str, Any]]
    evicted: int
    created_utc: str
    updated_utc: str
    caps_observed: dict[str, Any] | None = None
    history: list[dict[str, Any]] | None = None
    history_evicted: int = 0


def _invalid(detail: str) -> StoreInvalid:
    return StoreInvalid(detail)


def _row_lifecycle(row: dict[str, Any]) -> str:
    """The spend lifecycle of a stored row, tolerating a pre-CT-QC-039 row.

    A row written before this change has no ``accounting_state``; it is derived from the lease
    state it does have (``expired`` ⇒ uncertain, a settled row ⇒ settled), never assumed active.
    """
    value = row.get('accounting_state')
    if value in VALID_LIFECYCLE_STATES:
        return str(value)
    state = row.get('state')
    if state in TERMINAL_STATES:
        return LIFECYCLE_SETTLED
    if state == _STATE_EXPIRED:
        return LIFECYCLE_UNCERTAIN
    return LIFECYCLE_ACTIVE


def _state_from_document(document: dict[str, Any] | None, *, deployment_id: str,
                         now_utc: str) -> _DocumentState:
    """Validate a document strictly. Absent is a first run; anything else untrusted is refused."""
    if document is None:
        return _DocumentState(requests_committed=0, estimated_spend_usd=Decimal(0),
                              reservations={}, receipts=[], evicted=0,
                              created_utc=now_utc, updated_utc=now_utc, caps_observed=None,
                              history=[], history_evicted=0)
    if document.get('schema') != DOCUMENT_SCHEMA:
        raise _invalid('unexpected accounting document schema')
    if document.get('schema_version') != DOCUMENT_SCHEMA_VERSION:
        raise _invalid('unexpected accounting document schema version')
    if document.get('deployment_id') != deployment_id:
        raise _invalid('deployment key mismatch in the accounting document')
    requests = document.get('requests_committed')
    if type(requests) is not int or requests < 0:
        raise _invalid('requests_committed is not a non-negative integer')
    try:
        spend = Decimal(str(document.get('estimated_spend_usd')))
    except (InvalidOperation, TypeError, ValueError):
        raise _invalid('estimated_spend_usd is not a decimal string') from None
    if not spend.is_finite() or spend < 0:
        raise _invalid('estimated_spend_usd is not finite and non-negative')
    reservations = document.get('reservations') or {}
    if not isinstance(reservations, dict):
        raise _invalid('reservations is not a map')
    for key, row in reservations.items():
        if not isinstance(key, str) or not key:
            raise _invalid('a reservation key is not a non-empty string')
        if not isinstance(row, dict):
            raise _invalid('a reservation row is not a map')
        if type(row.get('seq')) is not int or row['seq'] < 1:
            raise _invalid('a reservation row has no positive integer seq')
        if row.get('state') not in _VALID_STATES:
            raise _invalid('a reservation row has an unknown state')
        lifecycle = row.get('accounting_state')
        if lifecycle is not None and lifecycle not in VALID_LIFECYCLE_STATES:
            raise _invalid('a reservation row has an unknown accounting state')
        digest = row.get('request_digest')
        if digest is not None and not is_digest(digest):
            raise _invalid('a reservation row has a malformed request digest')
        if not isinstance(row.get('expires_epoch'), (int, float)) or isinstance(row.get('expires_epoch'), bool):
            raise _invalid('a reservation row has no numeric expires_epoch')
        try:
            amount = Decimal(str(row.get('estimated_usd')))
        except (InvalidOperation, TypeError, ValueError):
            raise _invalid('a reservation row has a non-decimal estimate') from None
        if not amount.is_finite() or amount < 0:
            raise _invalid('a reservation row has a negative or non-finite estimate')
    evicted = document.get('reservations_evicted', 0)
    if type(evicted) is not int or evicted < 0:
        raise _invalid('reservations_evicted is not a non-negative integer')
    if requests != len(reservations) + evicted:
        raise _invalid('requests_committed does not reconcile with the tracked reservations '
                       '(no double count and no silent reset are both required)')
    receipts = document.get('receipts') or []
    if not isinstance(receipts, list):
        raise _invalid('receipts is not a list')
    receipts = [entry for entry in receipts if isinstance(entry, dict)]
    history_evicted = document.get('accounting_history_evicted', 0)
    if type(history_evicted) is not int or history_evicted < 0:
        raise _invalid('accounting_history_evicted is not a non-negative integer')
    raw_history = document.get('accounting_history') or []
    if not isinstance(raw_history, list):
        raise _invalid('accounting_history is not a list')
    history = []
    for entry in raw_history:
        if not isinstance(entry, dict):
            raise _invalid('an accounting history entry is not a map')
        if not isinstance(entry.get('event'), str) or not entry['event']:
            raise _invalid('an accounting history entry has no event')
        lifecycle = entry.get('accounting_state')
        if lifecycle is not None and lifecycle not in VALID_LIFECYCLE_STATES:
            raise _invalid('an accounting history entry has an unknown accounting state')
        history.append(copy.deepcopy(entry))
    caps = document.get('caps_observed')
    if caps is not None and not isinstance(caps, dict):
        caps = None
    return _DocumentState(requests_committed=requests, estimated_spend_usd=spend,
                          reservations=copy.deepcopy(reservations), receipts=receipts,
                          evicted=evicted,
                          created_utc=str(document.get('created_utc') or now_utc),
                          updated_utc=str(document.get('updated_utc') or now_utc),
                          caps_observed=caps, history=history,
                          history_evicted=history_evicted)


def _document_from_state(state: _DocumentState, *, deployment_id: str,
                         now_utc: str) -> dict[str, Any]:
    state.updated_utc = now_utc
    return {
        'schema': DOCUMENT_SCHEMA,
        'schema_version': DOCUMENT_SCHEMA_VERSION,
        'deployment_id': deployment_id,
        'requests_committed': state.requests_committed,
        'estimated_spend_usd': _money(state.estimated_spend_usd),
        'reservations_evicted': state.evicted,
        'caps_observed': state.caps_observed,
        'created_utc': state.created_utc,
        'updated_utc': state.updated_utc,
        'reservations': copy.deepcopy(state.reservations),
        'receipts': copy.deepcopy(state.receipts),
        'accounting_history': copy.deepcopy(state.history or []),
        'accounting_history_evicted': int(state.history_evicted),
    }


def _append_receipt(state: _DocumentState, kind: str, payload: dict[str, Any]) -> None:
    entry = {'kind': kind, 'utc': _utc()}
    entry.update(payload)
    state.receipts.append(entry)
    if len(state.receipts) > MAX_RECEIPTS:
        del state.receipts[:-MAX_RECEIPTS]


def _append_history(state: _DocumentState, *, reservation_id: str, seq: int | None,
                    request_digest: str | None, event: str, from_state: str | None,
                    to_state: str | None, lease_state: str | None, accounting_state: str | None,
                    evidence_class: str | None, reason: str,
                    payload: dict[str, Any] | None = None) -> None:
    """Append one accounting-history entry (bounded window, oldest entries counted out)."""
    entry: dict[str, Any] = {
        'utc': _utc(),
        'reservation_id': reservation_id,
        'seq': seq,
        'request_digest': request_digest,
        'event': event,
        'from_state': from_state,
        'to_state': to_state,
        'lease_state': lease_state,
        'accounting_state': accounting_state,
        'evidence_class': evidence_class,
        'reason': reason,
    }
    entry.update(payload or {})
    if state.history is None:
        state.history = []
    state.history.append(entry)
    while len(state.history) > MAX_ACCOUNTING_HISTORY:
        del state.history[0]
        state.history_evicted += 1


def _evict_reservations(state: _DocumentState) -> None:
    """Bound the audit map, never the counters. Counters stay authoritative."""
    while len(state.reservations) > MAX_TRACKED_RESERVATIONS:
        oldest = min(state.reservations, key=lambda key: state.reservations[key]['seq'])
        del state.reservations[oldest]
        state.evicted += 1


def _sweep_state(state: _DocumentState, now: float, released_by: str) -> int:
    """Release expired *leases*, exactly once. Never decrements a counter.

    The reservation moves to ``uncertain`` as well: an unresolved reservation whose owner is gone
    may already have reached the provider, so its allowance stays consumed. Only the lease — the
    concurrency slot — is released here.
    """
    released = 0
    for reservation_id, row in state.reservations.items():
        if row['state'] == _STATE_ACTIVE and float(row['expires_epoch']) <= now:
            row['state'] = _STATE_EXPIRED
            row['released_epoch'] = now
            row['released_by'] = released_by
            row['accounting_state'] = LIFECYCLE_UNCERTAIN
            released += 1
            _append_history(
                state, reservation_id=reservation_id, seq=row.get('seq'),
                request_digest=row.get('request_digest'), event='lease_expired',
                from_state=_STATE_ACTIVE, to_state=_STATE_EXPIRED,
                lease_state=_STATE_EXPIRED, accounting_state=LIFECYCLE_UNCERTAIN,
                evidence_class=None,
                reason='reservation TTL elapsed with no recorded outcome: slot released, spend '
                       'allowance retained (transmission possible)',
                payload={'released_by': released_by, 'released_epoch': now,
                         'estimated_usd_retained': row.get('estimated_usd'),
                         'allowance_refunded': False})
    return released


# -- the backend -------------------------------------------------------------------------------

class FirestoreSpendBackend(SpendBackend):
    """One accounting document, one atomic check+reserve transaction, deterministic retries."""

    name = BACKEND_FIRESTORE

    def __init__(self, *, config: FirestoreConfig, transport: BoundedTransport | None = None,
                 deployment_id: str | None = None, instance_id: str | None = None,
                 reservation_ttl_s: float | None = None, timeout_s: float | None = None,
                 max_attempts: int | None = None, backoff_s: float = CONTENTION_BACKOFF_S):
        self.config = config
        self.deployment_id = resolve_deployment_id(deployment_id)
        self.instance_id = resolve_instance_id(instance_id)
        self.reservation_ttl_s = resolve_reservation_ttl(reservation_ttl_s)
        self.timeout_s = resolve_timeout_s(timeout_s)
        self.max_attempts = resolve_attempts(max_attempts)
        self.backoff_s = max(0.0, float(backoff_s))
        # Evidence surface for probes: how often a stale snapshot forced a re-read.
        self.retry_count = 0
        self.attempts = 0
        self.document_id = config.document_path(self.deployment_id)
        self._transport = transport if transport is not None else build_transport(config)
        self._store = TransactionalStore(self._transport, self.document_id, timeout_s=self.timeout_s)

    @classmethod
    def from_env(cls, *, transport: BoundedTransport | None = None, **kwargs: Any
                 ) -> 'FirestoreSpendBackend':
        config = kwargs.pop('config', None) or FirestoreConfig.from_env()
        return cls(config=config, transport=transport, **kwargs)

    @property
    def store_label(self) -> str:
        return self.document_id

    # -- transaction driving ---------------------------------------------------------------

    def _run(self, work: Callable[[TransactionView], T]) -> T:
        attempt = 0
        while True:
            attempt += 1
            self.attempts += 1
            try:
                return self._store.transact(work)
            except StoreContention:
                if attempt >= self.max_attempts:
                    raise UsageLedgerUnavailable(
                        LIVE_SPEND_BACKEND_CONTENDED,
                        f'accounting document {self.document_id!r} stayed contended for '
                        f'{attempt} attempts; live inference refused (a reservation is never '
                        'granted without a committed conditional write)') from None
                self.retry_count += 1
                if self.backoff_s:
                    time.sleep(min(self.backoff_s * attempt, MAX_CONTENTION_BACKOFF_S))
            except StoreUnavailable as exc:
                raise UsageLedgerUnavailable(
                    LIVE_SPEND_BACKEND_UNAVAILABLE,
                    f'Firestore accounting backend is unavailable ({exc}); live inference '
                    'refused before any provider request') from None
            except StoreInvalid as exc:
                raise UsageLedgerCorrupt(
                    LIVE_USAGE_LEDGER_CORRUPT,
                    f'Firestore accounting document cannot be trusted ({exc}); live inference '
                    'refused (it is never reset to zero)') from None

    # -- operations ------------------------------------------------------------------------

    def reserve(self, budget: LiveBudget, *, now_epoch: float | None = None,
                request_intent: dict[str, Any] | None = None,
                request_digest: str | None = None) -> Admission:
        now = time.time() if now_epoch is None else float(now_epoch)
        now_utc = _utc()
        digest: str | None
        if request_digest is not None:
            if not is_digest(request_digest):
                raise UsageConfigError(
                    LIVE_REQUEST_IDENTITY_INVALID,
                    'request_digest must be a 64-character lower-case hex sha256; live inference '
                    'refused (an unaddressable reservation could not be reconciled)')
            digest = request_digest
        elif request_intent is not None:
            digest = digest_of_intent(request_intent)
        else:
            digest = None

        def work(view: TransactionView) -> Admission:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            released = _sweep_state(state, now, self.instance_id)
            if released:
                _append_receipt(state, 'sweep', {
                    'released': released,
                    'by_instance': self.instance_id,
                    'reason': 'reservation_ttl_expired_during_admission',
                })
            if digest is not None:
                for existing_id, existing in state.reservations.items():
                    if existing.get('request_digest') == digest:
                        if released:
                            view.write_document(_document_from_state(
                                state, deployment_id=self.deployment_id, now_utc=now_utc))
                        return Admission(False, LIVE_REQUEST_IDENTITY_REUSED,
                                         int(existing['seq']), state.requests_committed,
                                         state.estimated_spend_usd,
                                         budget.per_claim_estimate_usd, reservation_id=existing_id,
                                         instance_id=self.instance_id, request_digest=digest,
                                         accounting_state=_row_lifecycle(existing),
                                         duplicate_of=existing_id)
            if state.requests_committed + 1 > budget.request_cap:
                if released:
                    view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                             now_utc=now_utc))
                return Admission(False, LIVE_REQUEST_BUDGET_EXHAUSTED, state.requests_committed,
                                 state.requests_committed, state.estimated_spend_usd,
                                 budget.per_claim_estimate_usd, instance_id=self.instance_id,
                                 request_digest=digest or '')
            if state.estimated_spend_usd + budget.per_claim_estimate_usd > budget.dollar_cap:
                if released:
                    view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                             now_utc=now_utc))
                return Admission(False, LIVE_SPEND_BUDGET_EXHAUSTED, state.requests_committed,
                                 state.requests_committed, state.estimated_spend_usd,
                                 budget.per_claim_estimate_usd, instance_id=self.instance_id,
                                 request_digest=digest or '')
            seq = state.requests_committed + 1
            reservation_id = uuid.uuid4().hex
            row_digest = digest or default_request_digest(self.deployment_id, reservation_id)
            state.reservations[reservation_id] = {
                'seq': seq,
                'instance_id': self.instance_id,
                'reserved_epoch': now,
                'reserved_utc': now_utc,
                'expires_epoch': now + self.reservation_ttl_s,
                'state': _STATE_ACTIVE,
                'accounting_state': LIFECYCLE_ACTIVE,
                'estimated_usd': _money(budget.per_claim_estimate_usd),
                'request_digest': row_digest,
                'outcome': None,
                'provider_requests': None,
                'prompt_tokens': None,
                'completion_tokens': None,
                'settled_epoch': None,
                'released_epoch': None,
                'released_by': None,
                'transmitted_epoch': None,
                'transmitted_utc': None,
                'transmitted_round': None,
                'evidence_class': None,
                'refunded_epoch': None,
                'refunded_usd': None,
            }
            state.requests_committed = seq
            state.estimated_spend_usd = (state.estimated_spend_usd
                                         + budget.per_claim_estimate_usd).quantize(MONEY)
            state.caps_observed = {
                'request_cap': budget.request_cap,
                'dollar_cap_usd': _money(budget.dollar_cap),
                'estimated_usd_per_claim': _money(budget.per_claim_estimate_usd),
                'observed_utc': _utc(),
                'observed_instance': self.instance_id,
            }
            _append_history(state, reservation_id=reservation_id, seq=seq,
                            request_digest=row_digest, event='reserve', from_state=None,
                            to_state=_STATE_ACTIVE, lease_state=_STATE_ACTIVE,
                            accounting_state=LIFECYCLE_ACTIVE, evidence_class=None,
                            reason='admission consumed one unit: reservation committed before any '
                                   'provider transmission',
                            payload={'estimated_usd': _money(budget.per_claim_estimate_usd),
                                     'instance_id': self.instance_id,
                                     'expires_epoch': now + self.reservation_ttl_s})
            _append_receipt(state, 'reserve', {
                'seq': seq,
                'estimated_usd': _money(budget.per_claim_estimate_usd),
                'requests_committed_after': seq,
                'estimated_spend_usd_after': _money(state.estimated_spend_usd),
                'reservation_id': reservation_id,
                'request_digest': row_digest,
                'instance_id': self.instance_id,
                'expires_epoch': now + self.reservation_ttl_s,
            })
            _evict_reservations(state)
            view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                     now_utc=now_utc))
            return Admission(True, 'ADMITTED', seq, seq, state.estimated_spend_usd,
                             budget.per_claim_estimate_usd, reservation_id=reservation_id,
                             instance_id=self.instance_id,
                             expires_epoch=now + self.reservation_ttl_s,
                             request_digest=row_digest,
                             accounting_state=LIFECYCLE_ACTIVE)

        return self._run(work)

    def settle(self, admission: Admission, *, outcome: str,
               provider_requests: int | None = None,
               prompt_tokens: int | None = None,
               completion_tokens: int | None = None,
               evidence: dict[str, Any] | None = None) -> bool:
        if not admission.admitted or not admission.reservation_id:
            return False
        if admission.duplicate_of:
            return False
        return self.reconcile(admission, outcome=outcome, provider_requests=provider_requests,
                              prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens, evidence=evidence).transitioned

    def reconcile(self, admission: Admission | str | None = None, *, outcome: str,
                  provider_requests: int | None = None,
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None,
                  evidence: dict[str, Any] | None = None,
                  reservation_id: str | None = None,
                  request_digest: str | None = None) -> Reconciliation:
        if isinstance(admission, str) and reservation_id is None:
            reservation_id = admission
            admission = None
        rid = reservation_id or (getattr(admission, 'reservation_id', '') if admission else '')
        expected_digest = request_digest or (
            getattr(admission, 'request_digest', '') if admission else '') or ''
        if not rid and not expected_digest:
            return Reconciliation(False, False, False, 'no_reservation_identity')
        now = time.time()
        now_utc = _utc()

        def work(view: TransactionView) -> Reconciliation:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            resolved = rid
            if resolved:
                row = state.reservations.get(resolved)
            else:
                row = None
                for existing_id, existing in state.reservations.items():
                    if existing.get('request_digest') == expected_digest:
                        resolved, row = existing_id, existing
                        break
            resolved = str(resolved)
            if row is None:
                # Evicted (bounded audit trail) or never ours. The counter already includes this
                # unit, so there is nothing to settle and nothing to refund.
                return Reconciliation(False, False, False, 'unknown_reservation', resolved,
                                      expected_digest)
            stored_digest = str(row.get('request_digest') or '')
            if expected_digest and stored_digest and expected_digest != stored_digest:
                return Reconciliation(False, False, False, 'identity_mismatch', resolved,
                                      stored_digest, str(row.get('state')),
                                      _row_lifecycle(row))
            lease_state = str(row['state'])
            dispatched = row.get('transmitted_epoch') is not None
            if lease_state in TERMINAL_STATES:
                current = _row_lifecycle(row)
                if current != LIFECYCLE_UNCERTAIN:
                    # Already settled by an earlier attempt whose reply was lost: exactly-once
                    # holds because the retry re-reads and writes nothing.
                    return Reconciliation(False, False, True, 'already_settled', resolved,
                                          stored_digest, lease_state, current,
                                          str(row.get('evidence_class') or ''))
                # CT-QC-041: the lease is dispositioned but the money question is still open.
                # Reliable later evidence resolves it once; indecisive evidence writes nothing.
                classification = classify(outcome, evidence, dispatched=dispatched)
                if classification.accounting_state == LIFECYCLE_UNCERTAIN:
                    stored_class = str(row.get('evidence_class') or '')
                    duplicate = stored_class == classification.evidence_class
                    return Reconciliation(False, False, duplicate,
                                          'already_settled' if duplicate
                                          else 'no_decisive_evidence',
                                          resolved, stored_digest, lease_state, current,
                                          classification.evidence_class)
                row['accounting_state'] = classification.accounting_state
                row['evidence_class'] = classification.evidence_class
                _append_history(state, reservation_id=resolved, seq=row.get('seq'),
                                request_digest=stored_digest or None,
                                event='reconcile_accounting', from_state=lease_state,
                                to_state=lease_state, lease_state=lease_state,
                                accounting_state=classification.accounting_state,
                                evidence_class=classification.evidence_class,
                                reason=classification.reason,
                                payload={'outcome': str(outcome)[:64], 'dispatched': dispatched,
                                         'estimated_usd_reserved': row.get('estimated_usd'),
                                         'allowance_refunded': False, 'lease_unchanged': True})
                _append_receipt(state, 'reconcile_accounting', {
                    'seq': row.get('seq'),
                    'reservation_id': resolved,
                    'request_digest': stored_digest,
                    'outcome': str(outcome)[:64],
                    'lease_state': lease_state,
                    'accounting_state_before': current,
                    'accounting_state_after': classification.accounting_state,
                    'evidence_class': classification.evidence_class,
                })
                view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                         now_utc=now_utc))
                return Reconciliation(True, False, False, 'reconciled', resolved, stored_digest,
                                      lease_state, classification.accounting_state,
                                      classification.evidence_class)
            classification = classify(outcome, evidence, dispatched=dispatched)
            row['state'] = _STATE_SETTLED if lease_state == _STATE_ACTIVE else _STATE_SETTLED_EXPIRED
            row['accounting_state'] = classification.accounting_state
            row['evidence_class'] = classification.evidence_class
            row['outcome'] = str(outcome)[:64]
            row['provider_requests'] = provider_requests if type(provider_requests) is int else None
            row['prompt_tokens'] = prompt_tokens if type(prompt_tokens) is int else None
            row['completion_tokens'] = completion_tokens if type(completion_tokens) is int else None
            row['settled_epoch'] = now
            _append_history(state, reservation_id=resolved, seq=row.get('seq'),
                            request_digest=stored_digest or None, event='settle',
                            from_state=lease_state, to_state=row['state'],
                            lease_state=row['state'],
                            accounting_state=classification.accounting_state,
                            evidence_class=classification.evidence_class,
                            reason=classification.reason,
                            payload={'outcome': str(outcome)[:64], 'dispatched': dispatched,
                                     'estimated_usd_reserved': row.get('estimated_usd'),
                                     'allowance_refunded': False})
            _append_receipt(state, 'settle', {
                'seq': state.requests_committed,
                'admission_seq': getattr(admission, 'seq', row.get('seq') or 0),
                'outcome': str(outcome)[:64],
                'provider_requests': row['provider_requests'],
                'prompt_tokens': row['prompt_tokens'],
                'completion_tokens': row['completion_tokens'],
                'estimated_usd_reserved': row.get('estimated_usd'),
                'reservation_id': resolved,
                'request_digest': stored_digest,
                'settled_state': row['state'],
                'accounting_state': classification.accounting_state,
                'evidence_class': classification.evidence_class,
            })
            # Never touch the counters: a settlement records an outcome, it never moves money.
            view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                     now_utc=now_utc))
            return Reconciliation(True, lease_state == _STATE_ACTIVE, False, 'settled', resolved,
                                  stored_digest, row['state'],
                                  classification.accounting_state,
                                  classification.evidence_class)

        try:
            return self._run(work)
        except (UsageLedgerError, OSError):
            return Reconciliation(False, False, False, 'store_error', rid, expected_digest)

    def sweep(self, *, now_epoch: float | None = None, released_by: str | None = None) -> int:
        now = time.time() if now_epoch is None else float(now_epoch)
        actor = released_by or self.instance_id
        now_utc = _utc()

        def work(view: TransactionView) -> int:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            released = _sweep_state(state, now, actor)
            if released:
                _append_receipt(state, 'sweep', {
                    'released': released,
                    'by_instance': actor,
                    'reason': 'explicit_sweep',
                })
                view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                         now_utc=now_utc))
            return released

        return self._run(work)

    def mark_transmitted(self, admission: Admission | str | None = None, *,
                         round_num: int | None = None,
                         now_epoch: float | None = None,
                         reservation_id: str | None = None) -> bool:
        """Durably mark a dispatch before it happens (lifecycle becomes ``uncertain``)."""
        if isinstance(admission, str) and reservation_id is None:
            reservation_id = admission
            admission = None
        rid = reservation_id or (getattr(admission, 'reservation_id', '') if admission else '')
        if not rid:
            return False
        now = time.time() if now_epoch is None else float(now_epoch)
        now_utc = _utc()

        def work(view: TransactionView) -> bool:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            row = state.reservations.get(rid)
            if row is None or str(row.get('state')) in TERMINAL_STATES:
                return False
            first = row.get('transmitted_epoch') is None
            if first:
                row['transmitted_epoch'] = now
                row['transmitted_utc'] = now_utc
                row['transmitted_round'] = round_num if type(round_num) is int else None
                row['accounting_state'] = LIFECYCLE_UNCERTAIN
            _append_history(state, reservation_id=rid, seq=row.get('seq'),
                            request_digest=row.get('request_digest'),
                            event='transmission_started' if first else 'dispatch_attempt',
                            from_state=None if first else str(row.get('state')),
                            to_state=str(row.get('state')), lease_state=str(row.get('state')),
                            accounting_state=(LIFECYCLE_UNCERTAIN if first
                                              else _row_lifecycle(row)),
                            evidence_class=None,
                            reason=('a provider request was dispatched: transmission is possible, '
                                    'so the reservation is uncertain until an outcome is recorded')
                            if first else
                            'a further provider request was dispatched for the same reservation',
                            payload={'round': round_num if type(round_num) is int else None,
                                     'dispatched_epoch': now, 'first_dispatch': first,
                                     'allowance_refunded': False})
            view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                     now_utc=now_utc))
            return True

        try:
            return self._run(work)
        except (UsageLedgerError, OSError):
            return False

    def refund_failed_confirmed(self, reservation_id: str, *,
                                reason: str = 'provider_confirmed_no_charge',
                                now_epoch: float | None = None) -> dict[str, Any]:
        """Restore allowance for ``failed_confirmed`` only. Explicit, once-only, typed refusal."""
        now = time.time() if now_epoch is None else float(now_epoch)
        now_utc = _utc()
        if not reservation_id:
            raise UsageLedgerUnavailable(LIVE_RESERVATION_UNKNOWN,
                                         'no reservation id supplied for refund')

        def work(view: TransactionView) -> dict[str, Any]:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            row = state.reservations.get(reservation_id)
            if row is None:
                raise UsageLedgerUnavailable(
                    LIVE_RESERVATION_UNKNOWN,
                    f'reservation {reservation_id!r} does not exist in this deployment; nothing '
                    'was refunded')
            lifecycle = _row_lifecycle(row)
            if lifecycle not in REFUNDABLE_STATES:
                raise UsageLedgerError(
                    LIVE_RESERVATION_NOT_REFUNDABLE,
                    f'reservation {reservation_id!r} is {lifecycle!r}; only '
                    f'{sorted(REFUNDABLE_STATES)} may be refunded. An uncertain outcome may '
                    'already have cost money, so its allowance stays consumed until an outcome '
                    'is reconciled.')
            if row.get('refunded_epoch') is not None:
                raise UsageLedgerError(
                    LIVE_RESERVATION_NOT_REFUNDABLE,
                    f'reservation {reservation_id!r} was already refunded; a second refund would '
                    'create allowance')
            amount = Decimal(str(row.get('estimated_usd')))
            if state.estimated_spend_usd < amount:
                raise _invalid('refund would make recorded spend negative')
            state.estimated_spend_usd = (state.estimated_spend_usd - amount).quantize(MONEY)
            row['refunded_epoch'] = now
            row['refunded_usd'] = _money(amount)
            _append_history(state, reservation_id=reservation_id, seq=row.get('seq'),
                            request_digest=row.get('request_digest'), event='refund',
                            from_state=str(row.get('state')), to_state=str(row.get('state')),
                            lease_state=str(row.get('state')), accounting_state=lifecycle,
                            evidence_class=None, reason=str(reason)[:200],
                            payload={'refunded_usd': _money(amount),
                                     'estimated_spend_usd_after': _money(
                                         state.estimated_spend_usd)})
            _append_receipt(state, 'refund', {
                'reservation_id': reservation_id,
                'refunded_usd': _money(amount),
                'estimated_spend_usd_after': _money(state.estimated_spend_usd),
                'reason': str(reason)[:200],
            })
            view.write_document(_document_from_state(state, deployment_id=self.deployment_id,
                                                     now_utc=now_utc))
            return {'refunded': True, 'reservation_id': reservation_id,
                    'refunded_usd': _money(amount),
                    'estimated_spend_usd': _money(state.estimated_spend_usd)}

        return self._run(work)

    def history(self, reservation_id: str | None = None, *,
                limit: int = MAX_ACCOUNTING_HISTORY) -> list[dict[str, Any]]:
        """Bounded accounting history, oldest first (the document is size-capped)."""
        bound = max(1, min(int(limit), MAX_ACCOUNTING_HISTORY))
        now_utc = _utc()

        def work(view: TransactionView) -> list[dict[str, Any]]:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            entries = list(state.history or [])
            if reservation_id:
                entries = [entry for entry in entries
                           if entry.get('reservation_id') == reservation_id]
            return copy.deepcopy(entries[-bound:])

        return self._run(work)

    def status(self) -> dict[str, Any]:
        now_utc = _utc()

        def work(view: TransactionView) -> dict[str, Any]:
            state = _state_from_document(view.read_document(), deployment_id=self.deployment_id,
                                         now_utc=now_utc)
            rows = list(state.reservations.values())
            buckets = bucket_counts([str(row['state']) for row in rows],
                                    [_row_lifecycle(row) for row in rows])
            buckets['evicted'] = state.evicted
            accounting = {
                'states': sorted(VALID_LIFECYCLE_STATES),
                'by_state': buckets['by_accounting_state'],
                'uncertain': buckets['uncertain'],
                'failed_confirmed': buckets['failed_confirmed'],
                'refunded': sum(1 for row in rows if row.get('refunded_epoch') is not None),
                'history_rows': len(state.history or []),
                'history_evicted': state.history_evicted,
                'history_bound': MAX_ACCOUNTING_HISTORY,
                'history_append_only': True,
                'auto_refund_of_uncertain': False,
            }
            return {
                'backend': self.name,
                'backend_kind': 'firestore-transaction',
                'deployment_id': self.deployment_id,
                'instance_id': self.instance_id,
                'document': self.document_id,
                'revision': view.revision,
                'requests_committed': state.requests_committed,
                'estimated_spend_usd': _money(state.estimated_spend_usd),
                'created_utc': state.created_utc,
                'updated_utc': state.updated_utc,
                'caps_observed': state.caps_observed,
                'reservations': buckets,
                'accounting': accounting,
                'settlement_policy': _SETTLEMENT_POLICY,
            }

        return self._run(work)
