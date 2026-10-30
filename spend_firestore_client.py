"""The real Cloud Firestore transport adapter (CT-QC-042): the live wiring point, offline-proved.

What this module is
-------------------
:class:`civictrace.spend_firestore.BoundedTransport` is a two-operation seam: one bounded
``read`` of the accounting document and one bounded conditional ``commit`` of a replacement
body. :class:`FirestoreBoundedTransport` is the production implementation of that seam against
``google-cloud-firestore`` (pinned in ``requirements.txt`` and ``requirements.lock``), and
:func:`civictrace.spend_firestore.build_transport` constructs it. Before this module the seam
raised ``LIVE_SPEND_BACKEND_UNAVAILABLE`` unconditionally, so a deployment that selected
``CIVICTRACE_SPEND_BACKEND=firestore`` could never admit a live request.

The atomicity argument (why the budget check is not a check-then-write race)
----------------------------------------------------------------------------
The backend evaluates the ceilings against the snapshot returned by ``read`` and stages a
complete replacement document. The adapter then applies that body only inside one Firestore
transaction:

1. ``client.transaction(max_attempts=1)`` — one store transaction per attempt; retries are the
   backend's, not the SDK's, so the deterministic bounded retry policy stays in one place and is
   observable (``FirestoreSpendBackend.retry_count``).
2. inside the transaction, ``transaction.get(document_ref)`` re-reads the document and yields the
   store-assigned ``update_time`` of that transactional snapshot;
3. the observed revision must equal the revision the snapshot was read at (``None`` = the
   document must still be absent, i.e. create-if-absent). A mismatch raises
   :class:`civictrace.spend_firestore.StoreContention` *before* any write is staged, and the
   transaction is abandoned — nothing is applied;
4. only then ``transaction.set(document_ref, document)`` stages the replacement and the SDK's
   transactional wrapper commits it. If another writer committed in between, the store refuses
   the commit with ``ABORTED``; the adapter maps that to :class:`StoreContention` as well.

So "read the counters, check the caps, write the new counters" is one compare-and-swap against a
store-verified revision: the caps can only have been checked against the state that is still
current when the write lands. A stale snapshot is never written; a lost commit reply is never
retried into a second reservation (the backend turns it into a terminal refusal).

Revision encoding
-----------------
Firestore's concurrency token is the document's ``update_time`` (server-assigned, microsecond
resolution, unique per commit). The seam's revision is an ``int``, so the adapter encodes it as
integer nanoseconds since the Unix epoch, computed from integer date fields only (never from a
float ``timestamp()``). Two different commits cannot share an encoded revision, and a re-read of
the same commit re-encodes to the same integer.

Fail-closed failure mapping
---------------------------
Every failure becomes one of the seam's three typed errors, each of which the backend already
maps to a live-inference refusal *before* any provider request:

===============================  ==========================  ====================================
Store condition                  Adapter result               Backend refusal code
===============================  ==========================  ====================================
``ABORTED`` / stale revision     ``StoreContention``         retried, then
                                                             ``LIVE_SPEND_BACKEND_CONTENDED``
``FAILED_PRECONDITION``          ``StoreContention``         (same; still fail-closed)
deadline, unauthenticated,       ``StoreUnavailable``        ``LIVE_SPEND_BACKEND_UNAVAILABLE``
permission, unavailable, quota,
internal, missing SDK, unusable
client, foreign document path
untrusted stored body /         ``StoreInvalid``            ``LIVE_USAGE_LEDGER_CORRUPT``
unencodable revision
===============================  ==========================  ====================================

There is no fallback of any kind: a transport that cannot reach its store refuses admission, and
the SQLite backend is structurally unreachable from a ``firestore`` selector (see
:mod:`civictrace.spend_backend`).

Credentials
-----------
None are read, stored, logged or accepted as arguments. The client authenticates with Application
Default Credentials, which on Cloud Run is the service account of the revision. The project and
database are *explicit* configuration (``CIVICTRACE_FIRESTORE_PROJECT`` /
``CIVICTRACE_FIRESTORE_DATABASE``); a client whose project differs from that configuration is
refused rather than trusted.

Verification boundary (read this before believing anything)
------------------------------------------------------------
The tests exercise *this class* with dependency-injected offline clients that implement exactly
the SDK surface listed in :class:`FirestoreBoundedTransport`. That proves the adapter's
transactional logic, its idempotency and its failure taxonomy. It does **not** prove a live Cloud
Firestore project: no cloud call, no IAM check, no quota, no latency, no regional behaviour and no
credential resolution has been exercised. Live backend qualification remains owner-gated and
open (see ``CT-COR4A-REPORT.md``).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .spend_firestore import (
    BoundedTransport,
    FirestoreConfig,
    SpendStoreError,
    StoreContention,
    StoreInvalid,
    StoreUnavailable,
)
from .usage_ledger import (
    LIVE_SPEND_BACKEND_INVALID,
    LIVE_SPEND_BACKEND_NOT_CONFIGURED,
    LIVE_SPEND_BACKEND_UNAVAILABLE,
    UsageConfigError,
    UsageLedgerUnavailable,
)

#: The exact SDK distribution this adapter is written against. A different major/minor is not
#: claimed compatible; ``requirements.lock`` pins the same version with hashes.
SDK_REQUIREMENT = 'google-cloud-firestore==2.34.1'

#: One transaction per seam attempt. The backend owns retry, backoff and the attempt bound, so the
#: SDK must not silently retry an ``ABORTED`` commit behind the backend's counter.
TRANSACTION_ATTEMPTS = 1

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Sdk:
    """The imported SDK surface this adapter relies on. Imported lazily, never at module import."""

    firestore: Any
    api_exceptions: Any
    auth_exceptions: Any


_SDK: _Sdk | None = None


def _load_sdk() -> _Sdk:
    """Import ``google-cloud-firestore`` on first use.

    Deliberately not a module-level import: a deployment that runs the SQLite backend must not
    pay for (or fail on) a vendor SDK it never uses, and the offline test double must be
    importable without it. An import failure is a typed refusal, never a silent downgrade.
    """
    global _SDK
    if _SDK is None:
        import google.cloud.firestore as firestore
        from google.api_core import exceptions as api_exceptions
        from google.auth import exceptions as auth_exceptions

        _SDK = _Sdk(firestore=firestore, api_exceptions=api_exceptions,
                    auth_exceptions=auth_exceptions)
    return _SDK


#: One client per (project, database) for the life of the process. A backend — and therefore a
#: transport — is constructed per admission, so building an SDK client (and its gRPC channels) on
#: every request would add a handshake to every admission. A failed construction is never cached.
_CLIENTS: dict[tuple[str, str], Any] = {}
_CLIENTS_LOCK = threading.Lock()


def _new_client(config: FirestoreConfig) -> Any:
    """Build (or reuse) the production client from explicit configuration, ambient credentials.

    A thin function so tests and probes can prove the adapter never constructs a real client
    while still exercising the real adapter class. Credentials are Application Default
    Credentials, resolved by the SDK; a rotation therefore takes effect on process restart.
    """
    key = (config.project, config.database)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            sdk = _load_sdk()
            client = sdk.firestore.Client(project=config.project, database=config.database)
            _CLIENTS[key] = client
        return client


def _revision_ns(update_time: Any) -> int | None:
    """Encode a store concurrency token as integer nanoseconds since the epoch.

    Accepts the three shapes the SDK produces for a document's ``update_time``: a
    ``DatetimeWithNanoseconds`` (document snapshots), a protobuf ``Timestamp`` (write results) and
    a plain aware ``datetime``. Anything else is untrusted: the adapter refuses rather than
    inventing a revision.
    """
    if update_time is None:
        return None
    seconds = getattr(update_time, 'seconds', None)
    nanos = getattr(update_time, 'nanos', None)
    if type(seconds) is int and type(nanos) is int:
        return seconds * 1_000_000_000 + nanos
    if isinstance(update_time, datetime):
        aware = (update_time if update_time.tzinfo is not None
                 else update_time.replace(tzinfo=timezone.utc))
        delta = aware - _EPOCH
        whole_seconds = delta.days * 86400 + delta.seconds
        # ``DatetimeWithNanoseconds.nanosecond`` is authoritative; a plain datetime only has
        # microseconds. Integer arithmetic only, so two reads of one commit agree exactly.
        sub_ns = int(getattr(update_time, 'nanosecond', 0) or 0) or delta.microseconds * 1000
        return whole_seconds * 1_000_000_000 + sub_ns
    raise StoreInvalid('the store returned a concurrency token this adapter cannot encode')


def _revision_from_write_results(write_results: Any) -> int | None:
    """New revision of the applied write, when the commit reply carries one (advisory)."""
    try:
        results = list(write_results or [])
    except TypeError:
        return None
    if not results:
        return None
    try:
        return _revision_ns(getattr(results[0], 'update_time', None))
    except StoreInvalid:
        return None


class FirestoreBoundedTransport(BoundedTransport):
    """The production :class:`BoundedTransport`: one real Firestore document, one transaction.

    Construction
        ``config`` carries project / database / collection (validated by
        :class:`FirestoreConfig`). ``client`` is the dependency-injection point: an object exposing
        exactly the surface this adapter calls. Passing ``None`` builds the real SDK client
        through :func:`_new_client`; any failure there is a typed refusal *at construction*, so a
        deployment with unusable credentials or a broken SDK refuses every admission before the
        provider is reached.

    Surface called on the client (this is the whole vendor contract)
        ``client.collection(name) -> collection``; ``collection.document(document_id) -> ref``;
        ``ref.get(timeout=...) -> snapshot`` with ``.exists``, ``.to_dict()``, ``.update_time``;
        ``client.transaction(max_attempts=1) -> transaction`` with ``.get(ref, timeout=...)``,
        ``.set(ref, body)``, ``.commit()``, ``.write_results``; and the module-level
        ``transactional`` decorator. Nothing else is touched.

    Deadline
        The snapshot read and the transactional read are bounded by ``deadline_s``. The commit is
        one store RPC whose bound is the SDK's own default: the documented ``transactional``
        decorator does not forward a timeout to ``commit``. A commit that exceeds it surfaces as a
        transport failure, which the backend turns into a *terminal* refusal — it is never retried
        into a second reservation and never counted as an admission.
    """

    label = 'firestore'

    def __init__(self, config: FirestoreConfig, *, client: Any = None,
                 client_factory: Any = None):
        if client is not None and client_factory is not None:
            raise UsageConfigError(
                LIVE_SPEND_BACKEND_INVALID,
                'a Firestore transport was given both a client and a client factory; live '
                'inference refused rather than choosing one')
        if not isinstance(config, FirestoreConfig):
            raise UsageConfigError(
                LIVE_SPEND_BACKEND_NOT_CONFIGURED,
                'the Firestore transport needs a validated FirestoreConfig')
        self.config = config
        self._prefix = (f'projects/{config.project}/databases/{config.database}/documents/'
                        f'{config.collection}/')
        # Evidence surface for tests and probes; no credential, project or document content.
        self.reads = 0
        self.commits = 0
        self.applied = 0
        self.contended = 0
        self.operations: list[str] = []
        self.deadlines: list[float] = []
        self._client = client if client is not None else self._build_client(client_factory)
        self._validate_client()

    # -- construction ----------------------------------------------------------------------

    def _validate_client(self) -> None:
        """Refuse a client bound to another project, and a build without the transactional API.

        Applied to an injected client as well as a constructed one: the guard is about what the
        adapter will write to, not about how the client arrived.
        """
        configured = getattr(self._client, 'project', None)
        if isinstance(configured, str) and configured and configured != self.config.project:
            raise UsageLedgerUnavailable(
                LIVE_SPEND_BACKEND_UNAVAILABLE,
                f'the Firestore client is bound to project {configured!r} but the accounting '
                f'document belongs to {self.config.project!r}; live inference refused rather '
                'than writing accounting state into another project')
        sdk = _load_sdk()
        if not callable(getattr(sdk.firestore, 'transactional', None)):
            raise UsageLedgerUnavailable(
                LIVE_SPEND_BACKEND_UNAVAILABLE,
                f'{SDK_REQUIREMENT} does not expose the transactional API this adapter needs; '
                'live inference refused before any provider request')

    def _build_client(self, client_factory: Any) -> Any:
        if client_factory is None:
            try:
                _load_sdk()
            except Exception as exc:  # the pinned SDK is not installed in this environment
                raise UsageLedgerUnavailable(
                    LIVE_SPEND_BACKEND_UNAVAILABLE,
                    f'{SDK_REQUIREMENT} is not importable ({type(exc).__name__}); live inference '
                    'refused before any provider request (no provider request was made and the '
                    'SQLite backend was not substituted)') from None
        factory = client_factory or _new_client
        try:
            client = factory(self.config)
        except UsageLedgerUnavailable:
            raise
        except Exception as exc:  # missing SDK, unusable credentials, bad project/database
            raise UsageLedgerUnavailable(
                LIVE_SPEND_BACKEND_UNAVAILABLE,
                f'the Firestore accounting client could not be constructed '
                f'({type(exc).__name__}); live inference refused before any provider request '
                '(no provider request was made and the SQLite backend was not substituted)'
            ) from None
        if client is None:
            raise UsageLedgerUnavailable(
                LIVE_SPEND_BACKEND_UNAVAILABLE,
                'the Firestore client factory returned no client; live inference refused before '
                'any provider request (no provider request was made and the SQLite backend was '
                'not substituted)')
        return client

    # -- the seam --------------------------------------------------------------------------

    def call(self, operation: str, payload: dict[str, Any], *, deadline_s: float) -> dict[str, Any]:
        """One bounded round trip. Raises only the seam's typed errors (or a vendor exception the
        store layer converts into one)."""
        self.operations.append(str(operation))
        self.deadlines.append(deadline_s)
        if not isinstance(payload, dict):
            raise StoreUnavailable(f'{self.label}: {operation} payload is not a map')
        if type(deadline_s) not in (int, float) or not math.isfinite(float(deadline_s)) \
                or float(deadline_s) <= 0:
            raise StoreUnavailable(f'{self.label}: deadline must be a positive number of seconds')
        deadline = float(deadline_s)
        document_ref = self._document_ref(payload.get('document_id'))
        if operation == 'read':
            return self._read(document_ref, deadline)
        if operation == 'commit':
            return self._commit(document_ref, payload, deadline)
        raise StoreUnavailable(f'{self.label}: unknown operation {operation!r}')

    def _document_ref(self, document_id: Any) -> Any:
        """Resolve the configured document, refusing any path outside this deployment's store.

        The seam addresses a document by its fully-qualified path. A path from another project,
        database or collection means the wiring and the configuration disagree; writing there
        would move accounting state of a different deployment, so it is refused.
        """
        if not isinstance(document_id, str) or not document_id:
            raise StoreUnavailable(f'{self.label}: operation without a document id; live '
                                   'inference refused')
        if not document_id.startswith(self._prefix):
            raise StoreUnavailable(
                f'{self.label}: document {document_id!r} is outside the configured accounting '
                f'store {self._prefix!r}; live inference refused rather than addressing another '
                'project, database or collection')
        local_id = document_id[len(self._prefix):]
        if not local_id or '/' in local_id:
            raise StoreUnavailable(f'{self.label}: malformed accounting document id '
                                   f'{document_id!r}; live inference refused')
        return self._client.collection(self.config.collection).document(local_id)

    # -- operations ------------------------------------------------------------------------

    def _read(self, document_ref: Any, deadline: float) -> dict[str, Any]:
        self.reads += 1
        try:
            snapshot = document_ref.get(timeout=deadline)
        except SpendStoreError:
            raise
        except Exception as exc:
            raise self._map_exception(exc, 'read') from None
        exists = bool(getattr(snapshot, 'exists', False))
        if not exists:
            return {'revision': None, 'document': None}
        try:
            document = snapshot.to_dict()
        except Exception as exc:
            raise StoreInvalid(f'{self.label}: snapshot body is unreadable ({type(exc).__name__})') \
                from None
        if not isinstance(document, dict):
            raise StoreInvalid(f'{self.label}: snapshot body is not a map')
        revision = _revision_ns(getattr(snapshot, 'update_time', None))
        if revision is None:
            # An existing document with no concurrency token cannot be written conditionally;
            # treating it as "first run" would enable a blind overwrite.
            raise StoreInvalid(f'{self.label}: an existing accounting document has no update_time')
        return {'revision': revision, 'document': document}

    def _commit(self, document_ref: Any, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        self.commits += 1
        document = payload.get('document')
        if not isinstance(document, dict):
            raise StoreInvalid(f'{self.label}: staged document is not a map')
        expect_revision = payload.get('expect_revision')
        if expect_revision is not None and type(expect_revision) is not int:
            raise StoreInvalid(f'{self.label}: expect_revision is not an integer')

        try:
            sdk = _load_sdk()
        except Exception as exc:
            raise StoreUnavailable(
                f'{self.label}: {SDK_REQUIREMENT} is not importable ({type(exc).__name__})') \
                from None

        @sdk.firestore.transactional
        def _apply(transaction: Any) -> None:
            snapshots = list(transaction.get(document_ref, timeout=deadline))
            if not snapshots:
                raise StoreInvalid(f'{self.label}: the transaction read returned no snapshot')
            snapshot = snapshots[0]
            exists = bool(getattr(snapshot, 'exists', False))
            observed = (_revision_ns(getattr(snapshot, 'update_time', None)) if exists else None)
            if exists and observed is None:
                raise StoreInvalid(
                    f'{self.label}: an existing accounting document has no update_time inside '
                    'the transaction')
            if observed != expect_revision:
                self.contended += 1
                raise StoreContention(
                    f'{self.label}: accounting document {document_ref.id!r} changed between the '
                    f'snapshot (revision {expect_revision!r}) and the commit '
                    f'(revision {observed!r}); nothing was applied')
            transaction.set(document_ref, document)

        transaction = self._client.transaction(max_attempts=TRANSACTION_ATTEMPTS)
        try:
            _apply(transaction)
        except SpendStoreError:
            raise
        except Exception as exc:
            raise self._map_exception(exc, 'commit') from None
        self.applied += 1
        reply: dict[str, Any] = {'status': 'applied'}
        revision = _revision_from_write_results(getattr(transaction, 'write_results', None))
        if revision is not None:
            reply['revision'] = revision
        return reply

    # -- failure taxonomy ------------------------------------------------------------------

    def _map_exception(self, exc: Exception, operation: str) -> Exception:
        """Map one vendor exception onto the seam's typed errors.

        Contention stays retryable; everything else is "no accounting decision was made", which
        the backend turns into a refusal before any provider request. The mapper never inspects
        message text: only exception types are trusted.
        """
        sdk = _SDK
        if sdk is None:
            try:
                sdk = _load_sdk()
            except Exception:  # the SDK is not importable: no type information is available
                sdk = None
        if sdk is not None:
            api = sdk.api_exceptions
            aborted = getattr(api, 'Aborted', None)
            precondition = getattr(api, 'FailedPrecondition', None)
            # The SDK's transactional wrapper reports an exhausted ``ABORTED`` as
            # ``ValueError`` with the vendor exception as ``__cause__``.
            contention_types = tuple(t for t in (aborted, precondition) if t is not None)
            cause = getattr(exc, '__cause__', None)
            if contention_types and (isinstance(exc, contention_types)
                                     or isinstance(cause, contention_types)):
                self.contended += 1
                return StoreContention(
                    f'{self.label}: the store refused the {operation} as contended '
                    f'({type(cause or exc).__name__}); nothing was applied')
            if isinstance(exc, getattr(sdk.auth_exceptions, 'DefaultCredentialsError', ())):
                return StoreUnavailable(
                    f'{self.label}: no Application Default Credentials are available for the '
                    f'{operation}; live inference refused (no provider request was made)')
            if isinstance(exc, getattr(api, 'GoogleAPICallError', ())):
                return StoreUnavailable(
                    f'{self.label}: the store answered the {operation} with '
                    f'{type(exc).__name__}; live inference refused')
        if isinstance(exc, (TimeoutError, OSError)):
            return StoreUnavailable(
                f'{self.label}: {type(exc).__name__} during the {operation}; live inference '
                'refused')
        if isinstance(exc, ValueError):
            return StoreUnavailable(
                f'{self.label}: the store rejected the {operation} ({type(exc).__name__}); live '
                'inference refused')
        return StoreUnavailable(
            f'{self.label}: unexpected {type(exc).__name__} during the {operation}; live '
            'inference refused')
