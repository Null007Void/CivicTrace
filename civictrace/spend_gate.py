"""Shared-durable spend gate: SQLite transactional reservations for live inference (QC-02).

CT-COR-002 replaced the process-global counters with a JSON document guarded by a local
advisory file lock. That was durable on one filesystem but *container-local*: in a horizontal
deployment every instance owns its own copy of the file, so every instance gets its own
allowance. The owner bar for this task is a ledger that a second instance, a second revision
and a replacement instance all contend on.

This module is that store, and it is the only admission backend the live path has:

* **Storage is a required shared location.** The directory comes from
  ``CIVICTRACE_SPEND_STORE_PATH`` (absolute; the store file is
  ``spend-<deployment>.sqlite3`` inside it) and must already exist. It is *never* created by
  this code: if a shared mount is missing, ``mkdir`` would silently create a local directory on
  the container filesystem, which is exactly the defect being corrected. A missing, relative or
  unwritable location is a typed refusal, so live inference fails closed.
* **Atomicity is a database transaction, not a file lock.** Every admission runs
  ``BEGIN IMMEDIATE`` (a write lock taken before the read), reads the committed counters,
  evaluates both ceilings, inserts the reservation row and increments the counters, then
  commits. Two processes — or two hosts on a shared mount — are serialised by the store, so
  they cannot both observe the last remaining unit.
* **Keying is the deployment constant.** The file name and the ``store_meta.deployment_id``
  row use ``CIVICTRACE_DEPLOYMENT_ID`` only. A revision label, pid, port or hostname is not
  part of the key, so a rollback to an older revision continues the same count.
* **Reservations carry identity and a lease.** Each row stores the instance id, the durable
  request identity (a caller-supplied intent digest, or one derived from the reservation id), the
  reserved timestamp and an expiry computed from a conservative TTL. A sweep releases stale
  ``active`` leases exactly once (the ``state='active'`` predicate plus the reported row count
  make the transition idempotent) and never decrements the counters: "release" means releasing
  the *lease*, not refunding spend.
* **Expiry never refunds (CT-QC-039).** Releasing a lease frees a concurrency slot; whether the
  provider was charged is a separate question. Every reservation therefore also carries a spend
  lifecycle — ``active`` / ``settled`` / ``failed_confirmed`` / ``uncertain`` (see
  :mod:`civictrace.spend_lifecycle`) — and an expired, unresolved reservation becomes
  ``uncertain``: the allowance stays consumed until a late outcome reconciles it. Only
  ``failed_confirmed`` may be refunded, only through one explicit call, and after transmission
  that state requires a provider-confirmed no-charge record: an HTTP status code (5xx included)
  is never sufficient (CT-QC-041). Settlement itself never moves money.
* **Every transition is append-only.** ``accounting_history`` records each reserve, dispatch,
  release, settlement and refund with the request identity, both states and the evidence class.
  SQLite triggers refuse UPDATE and DELETE on that table, so the audit trail is immutable at the
  engine level rather than by convention. A v1 store is migrated additively in place, one
  migration row per pre-existing reservation.
* **Fail-closed taxonomy.** Unconfigured path, absent directory, unopenable/corrupt store,
  wrong schema, deployment mismatch and lock timeout each raise a typed error; the route maps
  them to HTTP 503 before any provider call. A pre-existing file that holds no schema is
  refused rather than initialised, so an emptied or replaced store cannot silently reset the
  count to zero.

Not established here (owner-gated environment decisions, no cloud call was made): the shared
mount itself, its lock semantics over NFS/FUSE, and any provider-side or account-level
financial stop. ``CT-COR2D-REPORT.md`` names the exact configuration point.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

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
    Reconciliation,
    classify,
    default_request_digest,
    is_digest,
    legacy_request_digest,
    request_digest as digest_of_intent,
    summarise_counts,
)
from .usage_ledger import (
    LIVE_REQUEST_BUDGET_EXHAUSTED,
    LIVE_REQUEST_IDENTITY_INVALID,
    LIVE_REQUEST_IDENTITY_REUSED,
    LIVE_RESERVATION_NOT_REFUNDABLE,
    LIVE_RESERVATION_UNKNOWN,
    LIVE_SPEND_BUDGET_EXHAUSTED,
    LIVE_SPEND_STORE_NOT_CONFIGURED,
    LIVE_USAGE_LEDGER_CORRUPT,
    LIVE_USAGE_LEDGER_LOCKED,
    LIVE_USAGE_LEDGER_UNAVAILABLE,
    MAX_RECEIPTS,
    MONEY,
    Admission,
    LiveBudget,
    UsageConfigError,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerLocked,
    UsageLedgerUnavailable,
)

STORE_SCHEMA = 'civictrace-spend-store/2'
STORE_SCHEMA_VERSION = 2
# CT-QC-039 bumped the store schema because a reservation now carries its durable request
# identity and its spend lifecycle. A v1 store is migrated in place, additively, inside the
# caller's write transaction: no row, counter or receipt is dropped, and one history row per
# pre-existing reservation records what the migration decided about it.
LEGACY_STORE_SCHEMA = 'civictrace-spend-store/1'
LEGACY_STORE_SCHEMA_VERSION = 1

STORE_ENV = 'CIVICTRACE_SPEND_STORE_PATH'
LEGACY_STORE_ENV = 'CIVICTRACE_USAGE_LEDGER_DIR'
EXPECT_ROOT_ENV = 'CIVICTRACE_SPEND_STORE_EXPECT_ROOT'
INSTANCE_ENV = 'CIVICTRACE_INSTANCE_ID'

# A claim is bounded by REQUESTS_PER_CLAIM_CEILING provider round trips plus local work, so a
# 15-minute lease is comfortably longer than any admitted run. The TTL is deliberately
# conservative: releasing a lease early only matters for accounting visibility, never for the
# ceiling (the unit stays committed).
DEFAULT_RESERVATION_TTL_S = 900.0
MAX_RESERVATION_TTL_S = 21600.0
MIN_RESERVATION_TTL_S = 0.05

_STATE_ACTIVE = STATE_ACTIVE
_STATE_SETTLED = STATE_SETTLED
_STATE_EXPIRED = STATE_EXPIRED
_STATE_SETTLED_EXPIRED = STATE_SETTLED_EXPIRED

_LIFECYCLE_ACTIVE = LIFECYCLE_ACTIVE
_LIFECYCLE_SETTLED = LIFECYCLE_SETTLED
_LIFECYCLE_FAILED_CONFIRMED = LIFECYCLE_FAILED_CONFIRMED
_LIFECYCLE_UNCERTAIN = LIFECYCLE_UNCERTAIN

# A store file that exists without a schema is either (a) a peer that created the file moments
# ago and has yet to commit its initialisation or (b) an emptied/replaced store. Case (a) is
# retried outside the write lock for a bounded window; case (b) is then refused.
_SCHEMA_WAIT_ATTEMPTS = 8
_SCHEMA_WAIT_SECONDS = 0.02

# Read window for the audit API; the table itself is never trimmed (it is append-only).
MAX_HISTORY_ROWS = 200


class _NoSchemaYet(Exception):
    """Internal signal: the file exists but no initialiser has committed a schema yet."""

_CREATE_STORE_META = """
CREATE TABLE IF NOT EXISTS store_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    deployment_id TEXT NOT NULL,
    created_utc TEXT NOT NULL
)
"""
_CREATE_COUNTERS = """
CREATE TABLE IF NOT EXISTS counters (
    deployment_id TEXT PRIMARY KEY,
    requests_committed INTEGER NOT NULL,
    estimated_spend_usd TEXT NOT NULL,
    caps_observed TEXT,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL
)
"""
_CREATE_RESERVATIONS = """
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    reserved_epoch REAL NOT NULL,
    reserved_utc TEXT NOT NULL,
    expires_epoch REAL NOT NULL,
    state TEXT NOT NULL,
    estimated_usd TEXT NOT NULL,
    outcome TEXT,
    provider_requests INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    settled_epoch REAL,
    released_epoch REAL,
    released_by TEXT,
    request_digest TEXT,
    transmitted_epoch REAL,
    transmitted_utc TEXT,
    transmitted_round INTEGER,
    accounting_state TEXT NOT NULL DEFAULT 'active',
    evidence_class TEXT,
    refunded_epoch REAL,
    refunded_usd TEXT,
    UNIQUE (deployment_id, seq)
)
"""
_CREATE_RECEIPTS = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    utc TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""
# Append-only accounting history: one row per lifecycle transition, never updated or deleted
# (enforced by the triggers below, so the invariant does not rest on this module's discipline).
_CREATE_ACCOUNTING_HISTORY = """
CREATE TABLE IF NOT EXISTS accounting_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    seq INTEGER,
    request_digest TEXT,
    event TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    lease_state TEXT,
    accounting_state TEXT,
    evidence_class TEXT,
    reason TEXT,
    utc TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""
_CREATE_HISTORY_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS accounting_history_no_update "
    "BEFORE UPDATE ON accounting_history "
    "BEGIN SELECT RAISE(ABORT, 'accounting_history is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS accounting_history_no_delete "
    "BEFORE DELETE ON accounting_history "
    "BEGIN SELECT RAISE(ABORT, 'accounting_history is append-only'); END",
)
_CREATE_EXPIRY_INDEX = """
CREATE INDEX IF NOT EXISTS reservations_expiry
    ON reservations (deployment_id, state, expires_epoch)
"""
_CREATE_IDENTITY_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS reservations_request_identity
    ON reservations (deployment_id, request_digest) WHERE request_digest IS NOT NULL
"""
_CREATE_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS accounting_history_reservation
    ON accounting_history (deployment_id, reservation_id, id)
"""

#: Columns added by the CT-QC-039 migration. Each ALTER is additive with a constant default, so
#: an existing store keeps every row, counter and receipt.
_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ('request_digest', 'TEXT'),
    ('transmitted_epoch', 'REAL'),
    ('transmitted_utc', 'TEXT'),
    ('transmitted_round', 'INTEGER'),
    ('accounting_state', "TEXT NOT NULL DEFAULT 'active'"),
    ('evidence_class', 'TEXT'),
    ('refunded_epoch', 'REAL'),
    ('refunded_usd', 'TEXT'),
)

_REQUIRED_TABLES = {'store_meta', 'counters', 'reservations', 'receipts'}

_SETTLEMENT_POLICY = SETTLEMENT_POLICY


def _utc() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY))


def _valid_identifier(value: str, *, field: str) -> str:
    if not value or len(value) > 128 or any(not (ch.isalnum() or ch in '._-:') for ch in value):
        raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                               f'{field} must be 1-128 chars of [A-Za-z0-9._-:]')
    return value


# -- shared configuration resolution -----------------------------------------------------------
# Promoted to module functions by CT-QC-038 so every backend resolves the deployment constant,
# the instance identity and the reservation TTL identically. Two backends that disagreed on the
# deployment key would not share one ceiling, which is the defect these keys exist to prevent.

def resolve_deployment_id(deployment_id: str | None = None) -> str:
    """The deployment constant. Never a pid, port, hostname or revision label."""
    raw_id = deployment_id if deployment_id is not None else os.getenv('CIVICTRACE_DEPLOYMENT_ID')
    raw_id = (raw_id if raw_id is not None else 'civictrace-rc1').strip()
    if not raw_id or len(raw_id) > 64 or any(not (ch.isalnum() or ch in '._-') for ch in raw_id):
        raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                               'CIVICTRACE_DEPLOYMENT_ID must be 1-64 chars of [A-Za-z0-9._-]')
    return raw_id


def resolve_reservation_ttl(reservation_ttl_s: float | None = None) -> float:
    """Lease length for one reservation, in seconds."""
    if reservation_ttl_s is None:
        raw = os.getenv('CIVICTRACE_SPEND_RESERVATION_TTL_S')
        try:
            reservation_ttl_s = DEFAULT_RESERVATION_TTL_S if raw is None else float(raw)
        except ValueError:
            raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                                   'CIVICTRACE_SPEND_RESERVATION_TTL_S is not a number') from None
    if not (MIN_RESERVATION_TTL_S <= float(reservation_ttl_s) <= MAX_RESERVATION_TTL_S):
        raise UsageConfigError(
            LIVE_SPEND_STORE_NOT_CONFIGURED,
            f'CIVICTRACE_SPEND_RESERVATION_TTL_S must be in '
            f'[{MIN_RESERVATION_TTL_S}, {MAX_RESERVATION_TTL_S}]')
    return float(reservation_ttl_s)


def resolve_instance_id(instance_id: str | None = None) -> str:
    """Identity of this process for reservation rows. Not part of any accounting key."""
    if instance_id is not None:
        return _valid_identifier(instance_id.strip(), field=INSTANCE_ENV)
    raw = os.getenv(INSTANCE_ENV)
    if raw and raw.strip():
        return _valid_identifier(raw.strip(), field=INSTANCE_ENV)
    try:
        host = socket.gethostname() or 'unknown-host'
    except OSError:
        host = 'unknown-host'
    host = ''.join(ch if (ch.isalnum() or ch in '._-') else '_' for ch in host)[:64]
    return f'{host}:{os.getpid()}:{int(time.time())}:{uuid.uuid4().hex[:8]}'


class SpendGate:
    """Transactional, shared-filesystem spend gate for one deployment constant."""

    def __init__(self, *, directory: Path | str | None = None,
                 deployment_id: str | None = None,
                 lock_timeout_s: float | None = None,
                 reservation_ttl_s: float | None = None,
                 instance_id: str | None = None,
                 expect_root: Path | str | None = None):
        self.store_dir = self._resolve_store_dir(directory)
        self.deployment_id = resolve_deployment_id(deployment_id)
        self.lock_timeout_s = self._resolve_lock_timeout(lock_timeout_s)
        self.reservation_ttl_s = self._resolve_ttl(reservation_ttl_s)
        self.instance_id = self._resolve_instance_id(instance_id)
        self._expect_root = self._resolve_expect_root(expect_root)
        if self._expect_root is not None:
            try:
                inside = self.store_dir.resolve().is_relative_to(self._expect_root.resolve())
            except OSError:
                inside = False
            if not inside:
                raise UsageConfigError(
                    LIVE_SPEND_STORE_NOT_CONFIGURED,
                    f'{EXPECT_ROOT_ENV} is {self._expect_root} but the store resolves outside it')
        # ``_existed_before_open`` is set per connection in :meth:`_connect`; it decides whether
        # an empty store may be initialised or must be refused. A file that already existed
        # without our schema is never treated as a first run.
        self._existed_before_open = self.path.exists()

    # -- configuration ---------------------------------------------------------------------

    @staticmethod
    def _resolve_store_dir(explicit: Path | str | None) -> Path:
        if explicit is not None:
            candidate = Path(explicit)
        else:
            raw = None
            for name in (STORE_ENV, LEGACY_STORE_ENV):
                value = os.getenv(name)
                if value and value.strip():
                    raw = value.strip()
                    break
            if raw is None:
                raise UsageConfigError(
                    LIVE_SPEND_STORE_NOT_CONFIGURED,
                    f'{STORE_ENV} must name the shared store directory; there is no local '
                    'fallback for live inference')
            candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                                   f'{STORE_ENV} must be an absolute path, got a relative one')
        try:
            if not candidate.exists():
                raise UsageLedgerUnavailable(
                    LIVE_USAGE_LEDGER_UNAVAILABLE,
                    f'shared spend store directory {candidate.name!r} does not exist; live '
                    'inference refused (a missing mount is never recreated locally)')
            if not candidate.is_dir():
                raise UsageLedgerUnavailable(
                    LIVE_USAGE_LEDGER_UNAVAILABLE,
                    'shared spend store path exists but is not a directory')
        except OSError as exc:
            raise UsageLedgerUnavailable(LIVE_USAGE_LEDGER_UNAVAILABLE,
                                         f'shared spend store cannot be reached: {type(exc).__name__}') from None
        return candidate

    @staticmethod
    def _resolve_expect_root(explicit: Path | str | None) -> Path | None:
        raw: str | None
        if explicit is not None:
            raw = str(explicit)
        else:
            raw = os.getenv(EXPECT_ROOT_ENV)
        if raw is None or not raw.strip():
            return None
        return Path(raw.strip()).expanduser()

    @staticmethod
    def _resolve_lock_timeout(lock_timeout_s: float | None) -> float:
        if lock_timeout_s is None:
            raw = os.getenv('CIVICTRACE_USAGE_LOCK_TIMEOUT_S')
            try:
                lock_timeout_s = 2.0 if raw is None else float(raw)
            except ValueError:
                raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                                       'CIVICTRACE_USAGE_LOCK_TIMEOUT_S is not a number') from None
        if not (0 < float(lock_timeout_s) <= 30):
            raise UsageConfigError(LIVE_SPEND_STORE_NOT_CONFIGURED,
                                   'CIVICTRACE_USAGE_LOCK_TIMEOUT_S must be in (0, 30]')
        return float(lock_timeout_s)

    @staticmethod
    def _resolve_ttl(reservation_ttl_s: float | None) -> float:
        return resolve_reservation_ttl(reservation_ttl_s)

    @staticmethod
    def _resolve_instance_id(instance_id: str | None) -> str:
        return resolve_instance_id(instance_id)

    @property
    def path(self) -> Path:
        return self.store_dir / f'spend-{self.deployment_id}.sqlite3'

    @property
    def directory(self) -> Path:
        """Historical name for the store directory (the CT-COR-002 ledger used ``directory``)."""
        return self.store_dir

    # -- error mapping ---------------------------------------------------------------------

    @staticmethod
    def _corrupt(detail: str) -> UsageLedgerCorrupt:
        return UsageLedgerCorrupt(LIVE_USAGE_LEDGER_CORRUPT,
                                  f'shared spend store cannot be trusted ({detail}); '
                                  'live inference refused')

    @staticmethod
    def _unavailable(exc: BaseException, detail: str = '') -> UsageLedgerError:
        reason = detail or type(exc).__name__
        return UsageLedgerUnavailable(LIVE_USAGE_LEDGER_UNAVAILABLE,
                                      f'shared spend store is unavailable ({reason})')

    def _map_sqlite_error(self, exc: BaseException) -> UsageLedgerError:
        message = str(exc).lower()
        if ('not a database' in message or 'malformed' in message
                or 'file is encrypted' in message or 'disk image is malformed' in message):
            return self._corrupt(message[:120])
        if 'locked' in message or 'busy' in message:
            return UsageLedgerLocked(LIVE_USAGE_LEDGER_LOCKED,
                                     'shared spend store is locked by another writer; '
                                     'admission refused')
        return self._unavailable(exc)

    # -- connection / transaction ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # Evaluated per connection, before sqlite creates the file: an empty store that already
        # existed is refusing material, a store this connection creates is a first run.
        self._existed_before_open = self.path.exists()
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.lock_timeout_s,
                                   isolation_level=None)
        except sqlite3.Error as exc:
            raise self._map_sqlite_error(exc) from None
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f'PRAGMA busy_timeout = {int(self.lock_timeout_s * 1000)}')
            # Rollback journal, not WAL: WAL needs a shared-memory (-shm) file that NFS/FUSE
            # mounts do not reliably provide, and a shared mount is the point of this store.
            mode = str(conn.execute('PRAGMA journal_mode = DELETE').fetchone()[0]).lower()
            if mode != 'delete':
                raise self._unavailable(RuntimeError('journal_mode'),
                                        f'journal_mode is {mode!r}, expected delete')
            conn.execute('PRAGMA synchronous = FULL')
        except sqlite3.Error as exc:
            conn.close()
            raise self._map_sqlite_error(exc) from None
        except BaseException:
            conn.close()
            raise
        return conn

    def _ensure_schema(self) -> None:
        """Commit the schema in its own transaction before the first admission.

        A crash *during* the very first reservation must not leave a file that exists without a
        schema: later readers would (correctly) refuse it as corrupt. Initialising first means
        the worst case for a crashed first admission is an empty, valid store.
        """
        if self.path.exists():
            return
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        # A peer can create the store file and be a few microseconds away from committing its
        # schema. That state is indistinguishable on disk from an emptied store, so it is never
        # initialised here — but it is retried a bounded number of times *outside* the write
        # lock (holding the lock would block the peer's initialisation). A store that is still
        # schema-less after the window is refused.
        for attempt in range(1, _SCHEMA_WAIT_ATTEMPTS + 1):
            conn = self._connect()
            try:
                self._begin(conn, write=write)
                try:
                    self._prepare(conn)
                except _NoSchemaYet:
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    if attempt >= _SCHEMA_WAIT_ATTEMPTS:
                        raise self._corrupt(
                            'store file exists but holds no schema and no initialiser '
                            'committed it') from None
                    time.sleep(_SCHEMA_WAIT_SECONDS)
                    continue
                try:
                    yield conn
                except UsageLedgerError:
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                except sqlite3.Error as exc:
                    # Never leak an untyped engine error: callers (and the route) must see one of
                    # the governance codes, so a failing store is a typed refusal, not a 500.
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise self._map_sqlite_error(exc) from None
                except BaseException:
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                else:
                    try:
                        conn.execute('COMMIT')
                    except sqlite3.Error as exc:
                        try:
                            conn.execute('ROLLBACK')
                        except sqlite3.Error:
                            pass
                        raise self._map_sqlite_error(exc) from None
                return
            finally:
                conn.close()
        raise self._corrupt('store file exists but holds no schema')

    def _begin(self, conn: sqlite3.Connection, *, write: bool) -> None:
        """Acquire the transaction, waiting out a busy writer until the configured timeout."""
        deadline = time.monotonic() + self.lock_timeout_s
        statement = 'BEGIN IMMEDIATE' if write else 'BEGIN'
        while True:
            try:
                conn.execute(statement)
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if 'locked' not in message and 'busy' not in message:
                    raise self._map_sqlite_error(exc) from None
                if time.monotonic() >= deadline:
                    raise UsageLedgerLocked(
                        LIVE_USAGE_LEDGER_LOCKED,
                        'shared spend store is locked by another writer; admission refused'
                    ) from None
                time.sleep(0.01)

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Validate (or, for a store this object created, initialise) the schema."""
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None
        if not tables:
            if self._existed_before_open:
                raise _NoSchemaYet()
            self._initialise(conn)
            return
        if not _REQUIRED_TABLES <= tables:
            raise self._corrupt('store schema is incomplete')
        try:
            meta = conn.execute(
                'SELECT schema, schema_version, deployment_id FROM store_meta WHERE id = 1'
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None
        if meta is None:
            raise self._corrupt('store metadata row is missing')
        if meta['deployment_id'] != self.deployment_id:
            raise self._corrupt('deployment key mismatch')
        if (meta['schema'] == LEGACY_STORE_SCHEMA
                and int(meta['schema_version']) == LEGACY_STORE_SCHEMA_VERSION):
            # A pre-CT-QC-039 store is upgraded additively, in this write transaction, before any
            # caller can read it. Counters, receipts and reservation rows are preserved; each
            # row's spend lifecycle is derived from the state it already had.
            self._migrate_v1(conn)
            return
        if meta['schema'] != STORE_SCHEMA:
            raise self._corrupt('unexpected store schema')
        if int(meta['schema_version']) != STORE_SCHEMA_VERSION:
            raise self._corrupt('unexpected store schema version')
        if 'accounting_history' not in tables:
            raise self._corrupt('store schema is missing the accounting history table')

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """Upgrade a ``civictrace-spend-store/1`` store to the CT-QC-039 shape, in place.

        Additive only: columns are added with constant defaults, no row is rewritten except to
        give it the identity and lifecycle it did not previously record. The migration appends
        one history row per existing reservation, so the audit trail starts with what was
        actually known at upgrade time rather than with a fabricated outcome.
        """
        now_utc = _utc()
        try:
            for column, ddl in _V2_COLUMNS:
                conn.execute(f'ALTER TABLE reservations ADD COLUMN {column} {ddl}')
            conn.execute(_CREATE_ACCOUNTING_HISTORY)
            for statement in _CREATE_HISTORY_TRIGGERS:
                conn.execute(statement)
            conn.execute(_CREATE_IDENTITY_INDEX)
            conn.execute(_CREATE_HISTORY_INDEX)
            rows = conn.execute(
                'SELECT reservation_id, seq, state FROM reservations WHERE deployment_id = ?',
                (self.deployment_id,)).fetchall()
            for row in rows:
                lease_state = str(row['state'])
                # A legacy 'settled'/'settled_expired' row recorded an outcome; a legacy
                # 'expired' row recorded that the lease was released and nothing else, which is
                # exactly ``uncertain``. Legacy 'active' stays active.
                if lease_state in TERMINAL_STATES:
                    accounting_state = _LIFECYCLE_SETTLED
                elif lease_state == _STATE_EXPIRED:
                    accounting_state = _LIFECYCLE_UNCERTAIN
                else:
                    accounting_state = _LIFECYCLE_ACTIVE
                digest = legacy_request_digest(str(row['reservation_id']))
                conn.execute(
                    'UPDATE reservations SET request_digest = ?, accounting_state = ? '
                    'WHERE deployment_id = ? AND reservation_id = ?',
                    (digest, accounting_state, self.deployment_id, row['reservation_id']))
                self._history(conn, reservation_id=str(row['reservation_id']),
                              seq=int(row['seq']), request_digest=digest,
                              event='migrate_store_schema',
                              from_state=None, to_state=lease_state,
                              lease_state=lease_state, accounting_state=accounting_state,
                              evidence_class=None,
                              reason=f'upgraded {LEGACY_STORE_SCHEMA} row: lifecycle derived '
                                     f'from its recorded lease state {lease_state!r}',
                              payload={'from_schema': LEGACY_STORE_SCHEMA,
                                       'to_schema': STORE_SCHEMA})
            conn.execute('UPDATE store_meta SET schema = ?, schema_version = ? WHERE id = 1',
                         (STORE_SCHEMA, STORE_SCHEMA_VERSION))
            self._receipt(conn, 'migrate', {
                'from_schema': LEGACY_STORE_SCHEMA,
                'to_schema': STORE_SCHEMA,
                'rows': len(rows),
            })
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None

    def _initialise(self, conn: sqlite3.Connection) -> None:
        now = _utc()
        try:
            conn.execute(_CREATE_STORE_META)
            conn.execute(_CREATE_COUNTERS)
            conn.execute(_CREATE_RESERVATIONS)
            conn.execute(_CREATE_RECEIPTS)
            conn.execute(_CREATE_ACCOUNTING_HISTORY)
            for statement in _CREATE_HISTORY_TRIGGERS:
                conn.execute(statement)
            conn.execute(_CREATE_EXPIRY_INDEX)
            conn.execute(_CREATE_IDENTITY_INDEX)
            conn.execute(_CREATE_HISTORY_INDEX)
            conn.execute(
                'INSERT INTO store_meta (id, schema, schema_version, deployment_id, created_utc) '
                'VALUES (1, ?, ?, ?, ?)',
                (STORE_SCHEMA, STORE_SCHEMA_VERSION, self.deployment_id, now))
            conn.execute(
                'INSERT INTO counters (deployment_id, requests_committed, estimated_spend_usd, '
                'caps_observed, created_utc, updated_utc) VALUES (?, 0, ?, NULL, ?, ?)',
                (self.deployment_id, _money(Decimal(0)), now, now))
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None

    # -- state helpers ---------------------------------------------------------------------

    def _counters(self, conn: sqlite3.Connection) -> sqlite3.Row:
        try:
            row = conn.execute(
                'SELECT requests_committed, estimated_spend_usd, caps_observed, created_utc, '
                'updated_utc FROM counters WHERE deployment_id = ?',
                (self.deployment_id,)).fetchone()
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None
        if row is None:
            raise self._corrupt('counter row for this deployment is missing')
        committed = row['requests_committed']
        if type(committed) is not int or committed < 0:
            raise self._corrupt('requests_committed is not a non-negative integer')
        try:
            amount = Decimal(row['estimated_spend_usd'])
        except Exception:
            raise self._corrupt('estimated_spend_usd is not decimal') from None
        if not amount.is_finite() or amount < 0:
            raise self._corrupt('estimated_spend_usd is not finite and non-negative')
        return row

    def _sweep(self, conn: sqlite3.Connection, now: float, released_by: str) -> int:
        """Release expired *leases* exactly once. Never touches counters.

        Releasing a lease frees a concurrency slot; it says nothing about whether the provider
        was charged. The reservation therefore also moves to the ``uncertain`` lifecycle: an
        unresolved reservation whose owner is gone may already have reached the provider, so its
        allowance stays consumed until a late outcome reconciles it. Every release appends one
        history row, so the audit shows that the money did **not** move.
        """
        try:
            rows = conn.execute(
                "SELECT reservation_id, seq, request_digest, estimated_usd, accounting_state "
                "FROM reservations WHERE deployment_id = ? AND state = ? AND expires_epoch <= ?",
                (self.deployment_id, _STATE_ACTIVE, now)).fetchall()
            released = 0
            for row in rows:
                cursor = conn.execute(
                    "UPDATE reservations SET state = ?, released_epoch = ?, released_by = ?, "
                    "accounting_state = ? WHERE deployment_id = ? AND reservation_id = ? "
                    "AND state = ?",
                    (_STATE_EXPIRED, now, released_by, _LIFECYCLE_UNCERTAIN,
                     self.deployment_id, row['reservation_id'], _STATE_ACTIVE))
                if (cursor.rowcount or 0) <= 0:
                    continue
                released += 1
                self._history(
                    conn, reservation_id=str(row['reservation_id']), seq=int(row['seq']),
                    request_digest=row['request_digest'], event='lease_expired',
                    from_state=_STATE_ACTIVE, to_state=_STATE_EXPIRED,
                    lease_state=_STATE_EXPIRED,
                    accounting_state=_LIFECYCLE_UNCERTAIN, evidence_class=None,
                    reason='reservation TTL elapsed with no recorded outcome: slot released, '
                           'spend allowance retained (transmission possible)',
                    payload={'released_by': released_by, 'released_epoch': now,
                             'estimated_usd_retained': row['estimated_usd'],
                             'allowance_refunded': False})
            return released
        except sqlite3.DatabaseError as exc:
            raise self._map_sqlite_error(exc) from None

    def _history(self, conn: sqlite3.Connection, *, reservation_id: str, seq: int | None,
                 request_digest: str | None, event: str, from_state: str | None,
                 to_state: str | None, lease_state: str | None,
                 accounting_state: str | None, evidence_class: str | None, reason: str,
                 payload: dict[str, Any] | None = None) -> None:
        """Append one accounting-history row. Never updates, never deletes.

        The append-only property is enforced by SQLite triggers on the table, not by this
        function being the only writer.
        """
        conn.execute(
            'INSERT INTO accounting_history (deployment_id, reservation_id, seq, request_digest, '
            'event, from_state, to_state, lease_state, evidence_class, reason, utc, payload) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (self.deployment_id, reservation_id, seq, request_digest, event, from_state,
             to_state, lease_state, evidence_class, reason, _utc(),
             json.dumps(payload or {}, sort_keys=True)))

    def _receipt(self, conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> None:
        conn.execute(
            'INSERT INTO receipts (deployment_id, kind, utc, payload) VALUES (?, ?, ?, ?)',
            (self.deployment_id, kind, _utc(), json.dumps(payload, sort_keys=True)))
        conn.execute(
            'DELETE FROM receipts WHERE deployment_id = ? AND id NOT IN '
            '(SELECT id FROM receipts WHERE deployment_id = ? ORDER BY id DESC LIMIT ?)',
            (self.deployment_id, self.deployment_id, MAX_RECEIPTS))

    def _commit_reservation(self, conn: sqlite3.Connection, budget: LiveBudget, seq: int,
                            now: float, spend: Decimal,
                            request_digest: str | None = None) -> tuple[str, Decimal]:
        """Insert the reservation row and advance the counters inside the caller's transaction."""
        reservation_id = uuid.uuid4().hex
        digest = request_digest or default_request_digest(self.deployment_id, reservation_id)
        committed = seq
        new_spend = (spend + budget.per_claim_estimate_usd).quantize(MONEY)
        caps = {
            'request_cap': budget.request_cap,
            'dollar_cap_usd': _money(budget.dollar_cap),
            'estimated_usd_per_claim': _money(budget.per_claim_estimate_usd),
            'observed_utc': _utc(),
            'observed_instance': self.instance_id,
        }
        conn.execute(
            'INSERT INTO reservations (reservation_id, deployment_id, seq, instance_id, '
            'reserved_epoch, reserved_utc, expires_epoch, state, estimated_usd, request_digest, '
            'accounting_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (reservation_id, self.deployment_id, committed, self.instance_id, now, _utc(),
             now + self.reservation_ttl_s, _STATE_ACTIVE,
             _money(budget.per_claim_estimate_usd), digest, _LIFECYCLE_ACTIVE))
        self._history(conn, reservation_id=reservation_id, seq=committed, request_digest=digest,
                      event='reserve', from_state=None, to_state=_STATE_ACTIVE,
                      lease_state=_STATE_ACTIVE, accounting_state=_LIFECYCLE_ACTIVE,
                      evidence_class=None,
                      reason='admission consumed one unit: reservation committed before any '
                             'provider transmission',
                      payload={'estimated_usd': _money(budget.per_claim_estimate_usd),
                               'instance_id': self.instance_id,
                               'expires_epoch': now + self.reservation_ttl_s})
        conn.execute(
            'UPDATE counters SET requests_committed = ?, estimated_spend_usd = ?, '
            'caps_observed = ?, updated_utc = ? WHERE deployment_id = ?',
            (committed, _money(new_spend), json.dumps(caps, sort_keys=True), _utc(),
             self.deployment_id))
        self._receipt(conn, 'reserve', {
            'seq': committed,
            'estimated_usd': _money(budget.per_claim_estimate_usd),
            'requests_committed_after': committed,
            'estimated_spend_usd_after': _money(new_spend),
            'reservation_id': reservation_id,
            'request_digest': digest,
            'instance_id': self.instance_id,
            'expires_epoch': now + self.reservation_ttl_s,
        })
        return reservation_id, new_spend

    # -- public operations -----------------------------------------------------------------

    def reserve(self, budget: LiveBudget, *, now_epoch: float | None = None,
                request_intent: dict[str, Any] | None = None,
                request_digest: str | None = None) -> Admission:
        """Atomically consume one admission on the shared store, or refuse.

        ``BEGIN IMMEDIATE`` takes the store's write lock before the counters are read, so
        concurrent instances cannot both see the last remaining unit. The expired-lease sweep
        runs in the same transaction (this is the periodic sweep: every admission performs one).

        A caller may present a durable *request identity* — either an intent map (canonicalised
        into a digest) or the digest itself. If that identity already owns a reservation in this
        deployment, the call is refused with ``LIVE_REQUEST_IDENTITY_REUSED`` and
        ``duplicate_of`` set: no second unit is consumed and the caller is expected to reconcile
        the existing reservation instead of transmitting the same request twice. Without a
        supplied identity, one is derived from the reservation id, so every reservation is still
        addressable by identity after a restart.
        """
        now = time.time() if now_epoch is None else float(now_epoch)
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
        self._ensure_schema()
        with self._transaction() as conn:
            released = self._sweep(conn, now, self.instance_id)
            if released:
                self._receipt(conn, 'sweep', {
                    'released': released,
                    'by_instance': self.instance_id,
                    'reason': 'reservation_ttl_expired_during_admission',
                })
            counters = self._counters(conn)
            requests = int(counters['requests_committed'])
            spend = Decimal(counters['estimated_spend_usd'])
            if digest is not None:
                existing = conn.execute(
                    'SELECT reservation_id, seq, state, accounting_state FROM reservations '
                    'WHERE deployment_id = ? AND request_digest = ?',
                    (self.deployment_id, digest)).fetchone()
                if existing is not None:
                    # Identity is owned. Refusing here is the only answer that cannot double
                    # count: a second admission would reserve a second unit for one request.
                    return Admission(False, LIVE_REQUEST_IDENTITY_REUSED, int(existing['seq']),
                                     requests, spend, budget.per_claim_estimate_usd,
                                     reservation_id=str(existing['reservation_id']),
                                     instance_id=self.instance_id, request_digest=digest,
                                     accounting_state=str(existing['accounting_state'] or ''),
                                     duplicate_of=str(existing['reservation_id']))
            if requests + 1 > budget.request_cap:
                return Admission(False, LIVE_REQUEST_BUDGET_EXHAUSTED, requests, requests, spend,
                                 budget.per_claim_estimate_usd, instance_id=self.instance_id,
                                 request_digest=digest or '')
            if spend + budget.per_claim_estimate_usd > budget.dollar_cap:
                return Admission(False, LIVE_SPEND_BUDGET_EXHAUSTED, requests, requests, spend,
                                 budget.per_claim_estimate_usd, instance_id=self.instance_id,
                                 request_digest=digest or '')
            seq = requests + 1
            reservation_id, new_spend = self._commit_reservation(conn, budget, seq, now, spend,
                                                                 request_digest=digest)
        return Admission(True, 'ADMITTED', seq, seq, new_spend, budget.per_claim_estimate_usd,
                         reservation_id=reservation_id, instance_id=self.instance_id,
                         expires_epoch=now + self.reservation_ttl_s,
                         request_digest=digest or default_request_digest(self.deployment_id,
                                                                        reservation_id),
                         accounting_state=_LIFECYCLE_ACTIVE)

    def settle(self, admission: Admission, *, outcome: str,
               provider_requests: int | None = None,
               prompt_tokens: int | None = None,
               completion_tokens: int | None = None,
               evidence: dict[str, Any] | None = None) -> bool:
        """Record the outcome of a spent reservation. Never raises, never moves money.

        Returns True only when this call made the ``active -> settled`` transition (the lease was
        still held). A late response after expiry reconciles the reservation for the audit trail
        and returns False; a repeated settlement is a no-op. Counters are never touched by a
        settlement: recording an outcome is not a refund, and an uncertain outcome is not a
        licence to restore allowance.

        ``evidence`` carries the sanitized transmission evidence (see
        :mod:`civictrace.spend_lifecycle`). Only an explicit pre-dispatch class or a
        *provider-confirmed* no-charge record can classify the reservation ``failed_confirmed``;
        anything else — including any HTTP status after transmission (5xx included), a timeout
        after send, a dropped connection, or no evidence at all — lands in ``uncertain``.
        """
        if not admission.admitted or not admission.reservation_id:
            return False
        if admission.duplicate_of:
            # A duplicate-identity refusal owns no transmission of its own; reconciling the
            # original row from here would record an outcome for a request this caller never
            # made. The caller must reconcile by identity explicitly.
            return False
        return self.reconcile(admission, outcome=outcome, provider_requests=provider_requests,
                              prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens, evidence=evidence).transitioned

    def _reconcile_terminal_accounting(self, conn: sqlite3.Connection, row: sqlite3.Row, *,
                                       lease_state: str, stored_digest: str, outcome: str,
                                       evidence: dict[str, Any] | None,
                                       dispatched: bool) -> Reconciliation:
        """Apply one *accounting-only* reconciliation to an already-dispositioned row (CT-QC-041).

        A reservation whose lease is terminal is normally finished: a repeat settlement is a
        no-op. But a settled ``uncertain`` row is finished only about the *slot* — the money
        question is still open, and reliable later evidence is exactly what reconciliation is
        for. This method resolves it exactly once:

        * a supported provider no-charge record moves it to ``failed_confirmed``, which is the
          only state an explicit refund accepts;
        * a provider usage record moves it to ``settled`` (consumed; a refund stays refused);
        * anything else — no evidence, a status code, an unsupported no-charge claim — leaves it
          ``uncertain`` and writes nothing, because indecisive evidence never moves money.

        The lease state, the counters and the settlement timestamp are untouched; the transition
        is appended to the accounting history like every other one.
        """
        rid = str(row['reservation_id'])
        current = str(row['accounting_state'] or '')
        if current != _LIFECYCLE_UNCERTAIN:
            return Reconciliation(False, False, True, 'already_settled', rid, stored_digest,
                                  lease_state, current, str(row['evidence_class'] or ''))
        classification = classify(outcome, evidence, dispatched=dispatched)
        if classification.accounting_state == _LIFECYCLE_UNCERTAIN:
            # Indecisive evidence never moves money. An exact repeat of the outcome already on
            # the row is a duplicate (nothing to do); different indecisive evidence is simply
            # not enough and is reported as such.
            stored_class = str(row['evidence_class'] or '')
            duplicate = stored_class == classification.evidence_class
            return Reconciliation(False, False, duplicate,
                                  'already_settled' if duplicate else 'no_decisive_evidence',
                                  rid, stored_digest, lease_state, current,
                                  classification.evidence_class)
        cursor = conn.execute(
            'UPDATE reservations SET accounting_state = ?, evidence_class = ? '
            'WHERE deployment_id = ? AND reservation_id = ? AND accounting_state = ?',
            (classification.accounting_state, classification.evidence_class,
             self.deployment_id, rid, _LIFECYCLE_UNCERTAIN))
        if (cursor.rowcount or 0) <= 0:
            # Another writer resolved the same row first: exactly-once, no second transition.
            return Reconciliation(False, False, True, 'already_settled', rid, stored_digest,
                                  lease_state, current, str(row['evidence_class'] or ''))
        seq = int(row['seq']) if row['seq'] is not None else None
        self._history(conn, reservation_id=rid, seq=seq,
                      request_digest=stored_digest or None, event='reconcile_accounting',
                      from_state=lease_state, to_state=lease_state, lease_state=lease_state,
                      accounting_state=classification.accounting_state,
                      evidence_class=classification.evidence_class,
                      reason=classification.reason,
                      payload={'outcome': str(outcome)[:64], 'dispatched': dispatched,
                               'estimated_usd_reserved': row['estimated_usd'],
                               'allowance_refunded': False, 'lease_unchanged': True})
        self._receipt(conn, 'reconcile_accounting', {
            'seq': seq,
            'reservation_id': rid,
            'request_digest': stored_digest,
            'outcome': str(outcome)[:64],
            'lease_state': lease_state,
            'accounting_state_before': current,
            'accounting_state_after': classification.accounting_state,
            'evidence_class': classification.evidence_class,
        })
        return Reconciliation(True, False, False, 'reconciled', rid, stored_digest, lease_state,
                              classification.accounting_state, classification.evidence_class)

    def reconcile(self, admission: Admission | str | None = None, *, outcome: str,
                  provider_requests: int | None = None,
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None,
                  evidence: dict[str, Any] | None = None,
                  reservation_id: str | None = None,
                  request_digest: str | None = None) -> Reconciliation:
        """Settle a reservation addressed by its durable identity, whoever created it.

        This is the recovery entry point: a replacement instance, a later revision or an operator
        holding only the reservation id and its request digest can reconcile a reservation whose
        own process died. The row is the same row, so the outcome lands exactly once against the
        same identity; a second call finds a terminal row and changes nothing. Never raises.
        """
        if isinstance(admission, str) and reservation_id is None:
            reservation_id = admission
            admission = None
        rid = reservation_id or (getattr(admission, 'reservation_id', '') if admission else '')
        expected_digest = request_digest or (
            getattr(admission, 'request_digest', '') if admission else '') or ''
        if not rid and not expected_digest:
            return Reconciliation(False, False, False, 'no_reservation_identity')
        try:
            with self._transaction() as conn:
                if rid:
                    row = conn.execute(
                        'SELECT * FROM reservations WHERE deployment_id = ? '
                        'AND reservation_id = ?', (self.deployment_id, rid)).fetchone()
                else:
                    # Identity-first recovery: the caller holds the request digest (the durable
                    # identity of the logical request) but not necessarily the row id — a
                    # replacement instance, a later revision, or an operator replaying a late
                    # provider response.
                    row = conn.execute(
                        'SELECT * FROM reservations WHERE deployment_id = ? '
                        'AND request_digest = ?', (self.deployment_id, expected_digest)).fetchone()
                if row is None:
                    return Reconciliation(False, False, False, 'unknown_reservation', rid,
                                          expected_digest)
                rid = str(row['reservation_id'])
                stored_digest = str(row['request_digest'] or '')
                if expected_digest and stored_digest and expected_digest != stored_digest:
                    # Same reservation id, different request intent: never settle another
                    # request's outcome against this row.
                    return Reconciliation(False, False, False, 'identity_mismatch', rid,
                                          stored_digest, str(row['state']),
                                          str(row['accounting_state'] or ''))
                lease_state = str(row['state'])
                dispatched = row['transmitted_epoch'] is not None
                if lease_state in TERMINAL_STATES:
                    # The lease is dispositioned. Since CT-QC-041 that is not automatically the
                    # end of the accounting question: a row settled in-flight as ``uncertain``
                    # may still be resolved by reliable later evidence, once.
                    return self._reconcile_terminal_accounting(
                        conn, row, lease_state=lease_state, stored_digest=stored_digest,
                        outcome=outcome, evidence=evidence, dispatched=dispatched)
                classification = classify(outcome, evidence, dispatched=dispatched)
                to_state = (_STATE_SETTLED if lease_state == _STATE_ACTIVE
                            else _STATE_SETTLED_EXPIRED)
                conn.execute(
                    'UPDATE reservations SET state = ?, accounting_state = ?, outcome = ?, '
                    'provider_requests = ?, prompt_tokens = ?, completion_tokens = ?, '
                    'settled_epoch = ?, evidence_class = ? '
                    'WHERE deployment_id = ? AND reservation_id = ? AND state = ?',
                    (to_state, classification.accounting_state, str(outcome)[:64],
                     provider_requests if type(provider_requests) is int else None,
                     prompt_tokens if type(prompt_tokens) is int else None,
                     completion_tokens if type(completion_tokens) is int else None,
                     time.time(), classification.evidence_class, self.deployment_id, rid,
                     lease_state))
                self._history(
                    conn, reservation_id=rid, seq=int(row['seq']) if row['seq'] is not None else None,
                    request_digest=stored_digest or None, event='settle',
                    from_state=lease_state, to_state=to_state, lease_state=to_state,
                    accounting_state=classification.accounting_state,
                    evidence_class=classification.evidence_class, reason=classification.reason,
                    payload={'outcome': str(outcome)[:64],
                             'dispatched': dispatched,
                             'estimated_usd_reserved': row['estimated_usd'],
                             'allowance_refunded': False})
                counters = self._counters(conn)
                self._receipt(conn, 'settle', {
                    'seq': int(counters['requests_committed']),
                    'admission_seq': getattr(admission, 'seq', int(row['seq']) if row['seq'] else 0),
                    'outcome': str(outcome)[:64],
                    'provider_requests': provider_requests if type(provider_requests) is int else None,
                    'prompt_tokens': prompt_tokens if type(prompt_tokens) is int else None,
                    'completion_tokens': completion_tokens if type(completion_tokens) is int else None,
                    'estimated_usd_reserved': _money(Decimal(row['estimated_usd'])),
                    'reservation_id': rid,
                    'request_digest': stored_digest,
                    'settled_state': to_state,
                    'accounting_state': classification.accounting_state,
                    'evidence_class': classification.evidence_class,
                })
                return Reconciliation(True, lease_state == _STATE_ACTIVE, False, 'settled', rid,
                                      stored_digest, to_state,
                                      classification.accounting_state,
                                      classification.evidence_class)
        except (UsageLedgerError, OSError):
            return Reconciliation(False, False, False, 'store_error', rid, expected_digest)

    def mark_transmitted(self, admission: Admission | str | None = None, *,
                         round_num: int | None = None,
                         now_epoch: float | None = None,
                         reservation_id: str | None = None) -> bool:
        """Durably record that a provider request is about to be handed to the transport.

        Called *before* the send, so a crash after this point leaves an ``uncertain`` reservation
        rather than an innocently ``active`` one. It is idempotent (the first dispatch wins the
        timestamp; later rounds append a history row and change nothing) and never touches the
        counters. A terminal reservation is not modified: once an outcome is recorded, a late
        dispatch attempt cannot reopen the accounting.
        """
        if isinstance(admission, str) and reservation_id is None:
            reservation_id = admission
            admission = None
        rid = reservation_id or (getattr(admission, 'reservation_id', '') if admission else '')
        if not rid:
            return False
        now = time.time() if now_epoch is None else float(now_epoch)
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    'SELECT seq, state, accounting_state, transmitted_epoch, request_digest '
                    'FROM reservations WHERE deployment_id = ? AND reservation_id = ?',
                    (self.deployment_id, rid)).fetchone()
                if row is None:
                    return False
                lease_state = str(row['state'])
                if lease_state in TERMINAL_STATES:
                    return False
                first = row['transmitted_epoch'] is None
                if first:
                    conn.execute(
                        'UPDATE reservations SET transmitted_epoch = ?, transmitted_utc = ?, '
                        'transmitted_round = ?, accounting_state = ? '
                        'WHERE deployment_id = ? AND reservation_id = ?',
                        (now, _utc(), round_num if type(round_num) is int else None,
                         _LIFECYCLE_UNCERTAIN, self.deployment_id, rid))
                self._history(
                    conn, reservation_id=rid, seq=int(row['seq']),
                    request_digest=row['request_digest'],
                    event='transmission_started' if first else 'dispatch_attempt',
                    from_state=None if first else lease_state,
                    to_state=lease_state, lease_state=lease_state,
                    accounting_state=_LIFECYCLE_UNCERTAIN if first
                    else str(row['accounting_state'] or ''), evidence_class=None,
                    reason=('a provider request was dispatched: transmission is possible, so the '
                            'reservation is uncertain until an outcome is recorded') if first
                    else 'a further provider request was dispatched for the same reservation',
                    payload={'round': round_num if type(round_num) is int else None,
                             'dispatched_epoch': now, 'first_dispatch': first,
                             'allowance_refunded': False})
                return True
        except (UsageLedgerError, OSError):
            return False

    def refund_failed_confirmed(self, reservation_id: str, *,
                                reason: str = 'provider_confirmed_no_charge',
                                now_epoch: float | None = None) -> dict[str, Any]:
        """Restore allowance for a ``failed_confirmed`` reservation. Explicit, once-only, typed.

        This is the *only* operation that can undo a committed unit, and it refuses unless the
        reservation's own recorded evidence proves no charge: an ``uncertain`` reservation (a
        timeout after send, a crash after send, an expired lease with no outcome) is refused.
        The request counter is deliberately not decremented: sequence numbers stay monotonic, so
        a refund can never make a later admission look like an earlier one.
        """
        now = time.time() if now_epoch is None else float(now_epoch)
        if not reservation_id:
            raise UsageLedgerUnavailable(LIVE_RESERVATION_UNKNOWN,
                                         'no reservation id supplied for refund')
        with self._transaction() as conn:
            row = conn.execute(
                'SELECT seq, state, accounting_state, estimated_usd, refunded_epoch, '
                'request_digest FROM reservations WHERE deployment_id = ? AND reservation_id = ?',
                (self.deployment_id, reservation_id)).fetchone()
            if row is None:
                raise UsageLedgerUnavailable(
                    LIVE_RESERVATION_UNKNOWN,
                    f'reservation {reservation_id!r} does not exist in this deployment; nothing '
                    'was refunded')
            accounting_state = str(row['accounting_state'] or '')
            if accounting_state not in REFUNDABLE_STATES:
                raise UsageLedgerError(
                    LIVE_RESERVATION_NOT_REFUNDABLE,
                    f'reservation {reservation_id!r} is {accounting_state!r}; only '
                    f'{sorted(REFUNDABLE_STATES)} may be refunded. An uncertain outcome may '
                    'already have cost money, so its allowance stays consumed until an outcome '
                    'is reconciled.')
            if row['refunded_epoch'] is not None:
                raise UsageLedgerError(
                    LIVE_RESERVATION_NOT_REFUNDABLE,
                    f'reservation {reservation_id!r} was already refunded; a second refund would '
                    'create allowance')
            amount = Decimal(row['estimated_usd'])
            counters = self._counters(conn)
            spend = Decimal(counters['estimated_spend_usd'])
            if spend < amount:
                raise self._corrupt('refund would make recorded spend negative')
            new_spend = (spend - amount).quantize(MONEY)
            conn.execute(
                'UPDATE reservations SET refunded_epoch = ?, refunded_usd = ? '
                'WHERE deployment_id = ? AND reservation_id = ?',
                (now, _money(amount), self.deployment_id, reservation_id))
            conn.execute(
                'UPDATE counters SET estimated_spend_usd = ?, updated_utc = ? '
                'WHERE deployment_id = ?',
                (_money(new_spend), _utc(), self.deployment_id))
            self._history(conn, reservation_id=reservation_id, seq=int(row['seq']),
                          request_digest=row['request_digest'], event='refund',
                          from_state=str(row['state']), to_state=str(row['state']),
                          lease_state=str(row['state']), accounting_state=accounting_state,
                          evidence_class=None, reason=str(reason)[:200],
                          payload={'refunded_usd': _money(amount),
                                   'estimated_spend_usd_after': _money(new_spend)})
            self._receipt(conn, 'refund', {
                'reservation_id': reservation_id,
                'refunded_usd': _money(amount),
                'estimated_spend_usd_after': _money(new_spend),
                'reason': str(reason)[:200],
            })
        return {'refunded': True, 'reservation_id': reservation_id,
                'refunded_usd': _money(amount),
                'estimated_spend_usd': _money(new_spend)}

    def history(self, reservation_id: str | None = None, *,
                limit: int = MAX_HISTORY_ROWS) -> list[dict[str, Any]]:
        """Append-only accounting history, oldest first, bounded by ``limit``."""
        bound = max(1, min(int(limit), MAX_HISTORY_ROWS))
        self._ensure_schema()
        with self._transaction() as conn:
            if reservation_id:
                rows = conn.execute(
                    'SELECT * FROM accounting_history WHERE deployment_id = ? '
                    'AND reservation_id = ? ORDER BY id DESC LIMIT ?',
                    (self.deployment_id, reservation_id, bound)).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM accounting_history WHERE deployment_id = ? '
                    'ORDER BY id DESC LIMIT ?', (self.deployment_id, bound)).fetchall()
        entries: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row['payload'])
            except ValueError:
                payload = {'payload_corrupt': True}
            entry = {
                'id': int(row['id']),
                'reservation_id': row['reservation_id'],
                'seq': row['seq'],
                'request_digest': row['request_digest'],
                'event': row['event'],
                'from_state': row['from_state'],
                'to_state': row['to_state'],
                'lease_state': row['lease_state'],
                'accounting_state': row['accounting_state'],
                'evidence_class': row['evidence_class'],
                'reason': row['reason'],
                'utc': row['utc'],
            }
            if isinstance(payload, dict):
                entry.update(payload)
            entries.append(entry)
        return entries

    def sweep(self, *, now_epoch: float | None = None, released_by: str | None = None) -> int:
        """Release expired leases (operator/recovery entry point). Returns rows released."""
        now = time.time() if now_epoch is None else float(now_epoch)
        actor = released_by or self.instance_id
        self._ensure_schema()
        with self._transaction() as conn:
            released = self._sweep(conn, now, actor)
            if released:
                self._receipt(conn, 'sweep', {
                    'released': released,
                    'by_instance': actor,
                    'reason': 'explicit_sweep',
                })
        return released

    def startup_sweep(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        """Recover leases abandoned by a dead predecessor; safe to call on every start."""
        released = self.sweep(now_epoch=now_epoch, released_by=self.instance_id)
        return {
            'released': released,
            'instance_id': self.instance_id,
            'deployment_id': self.deployment_id,
            'store': self.path.name,
        }

    def integrity_check(self) -> str:
        """SQLite ``quick_check`` result (``ok`` for a healthy store)."""
        self._ensure_schema()
        with self._transaction(write=False) as conn:
            try:
                return str(conn.execute('PRAGMA quick_check').fetchone()[0])
            except sqlite3.DatabaseError as exc:
                raise self._map_sqlite_error(exc) from None

    def read_state(self) -> dict[str, Any]:
        """Audit read (write lock so a fresh store can be initialised consistently)."""
        self._ensure_schema()
        with self._transaction() as conn:
            counters = self._counters(conn)
            counts = {row['state']: int(row['n']) for row in conn.execute(
                'SELECT state, COUNT(*) AS n FROM reservations WHERE deployment_id = ? '
                'GROUP BY state', (self.deployment_id,))}
            accounting = {row['accounting_state'] or LIFECYCLE_ACTIVE: int(row['n'])
                          for row in conn.execute(
                              'SELECT accounting_state, COUNT(*) AS n FROM reservations '
                              'WHERE deployment_id = ? GROUP BY accounting_state',
                              (self.deployment_id,))}
            refunded = int(conn.execute(
                'SELECT COUNT(*) FROM reservations WHERE deployment_id = ? '
                'AND refunded_epoch IS NOT NULL', (self.deployment_id,)).fetchone()[0])
            history_total = int(conn.execute(
                'SELECT COUNT(*) FROM accounting_history WHERE deployment_id = ?',
                (self.deployment_id,)).fetchone()[0])
            receipt_rows = conn.execute(
                'SELECT kind, utc, payload FROM receipts WHERE deployment_id = ? '
                'ORDER BY id DESC LIMIT ?', (self.deployment_id, MAX_RECEIPTS)).fetchall()
            last = conn.execute(
                'SELECT reservation_id, seq, instance_id, state, reserved_utc, expires_epoch, '
                'outcome, request_digest, accounting_state, transmitted_epoch, refunded_epoch '
                'FROM reservations WHERE deployment_id = ? ORDER BY seq DESC LIMIT 1',
                (self.deployment_id,)).fetchone()
            journal_mode = str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        receipts: list[dict[str, Any]] = []
        for row in reversed(receipt_rows):
            try:
                payload = json.loads(row['payload'])
            except ValueError:
                payload = {'payload_corrupt': True}
            if not isinstance(payload, dict):
                payload = {'payload_corrupt': True}
            entry = {'kind': row['kind'], 'utc': row['utc']}
            entry.update(payload)
            receipts.append(entry)
        caps = None
        if counters['caps_observed']:
            try:
                caps = json.loads(counters['caps_observed'])
            except ValueError:
                caps = None
        return {
            'schema': 'civictrace-usage-ledger/1',
            'deployment_id': self.deployment_id,
            'requests_committed': int(counters['requests_committed']),
            'estimated_spend_usd': counters['estimated_spend_usd'],
            'created_utc': counters['created_utc'],
            'updated_utc': counters['updated_utc'],
            'caps_observed': caps,
            'receipts': receipts,
            'settlement_policy': _SETTLEMENT_POLICY,
            'reservations': {
                **summarise_counts(counts, accounting),
                'last_reservation': (None if last is None else {
                    'reservation_id': last['reservation_id'],
                    'seq': int(last['seq']),
                    'instance_id': last['instance_id'],
                    'state': last['state'],
                    'reserved_utc': last['reserved_utc'],
                    'expires_epoch': last['expires_epoch'],
                    'outcome': last['outcome'],
                    'request_digest': last['request_digest'],
                    'accounting_state': last['accounting_state'],
                    'transmitted_epoch': last['transmitted_epoch'],
                    'refunded_epoch': last['refunded_epoch'],
                }),
            },
            'accounting': {
                'states': sorted(VALID_LIFECYCLE_STATES),
                'by_state': accounting,
                'uncertain': accounting.get(LIFECYCLE_UNCERTAIN, 0),
                'failed_confirmed': accounting.get(LIFECYCLE_FAILED_CONFIRMED, 0),
                'refunded': refunded,
                'history_rows': history_total,
                'history_append_only': True,
                'auto_refund_of_uncertain': False,
            },
            'accounting_history': self.history(limit=50),
            'store': {
                'schema': STORE_SCHEMA,
                'filename': self.path.name,
                'journal_mode': journal_mode,
                'deployment_id': self.deployment_id,
                'instance_id': self.instance_id,
            },
        }

    # -- test / probe seam -----------------------------------------------------------------

    @contextmanager
    def exclusive_write_lock(self) -> Iterator[None]:
        """Hold the store's write lock in this process (used to prove the fail-closed path)."""
        with self._transaction():
            pass
        conn = self._connect()
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while True:
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    break
                except sqlite3.OperationalError as exc:
                    message = str(exc).lower()
                    if 'locked' not in message and 'busy' not in message:
                        raise self._map_sqlite_error(exc) from None
                    if time.monotonic() >= deadline:
                        raise UsageLedgerLocked(LIVE_USAGE_LEDGER_LOCKED,
                                                'store lock not acquired for the holder') from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
