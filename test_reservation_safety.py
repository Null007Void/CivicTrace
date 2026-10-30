"""CT-QC-039 regression: uncertain-outcome accounting for the live spend gate.

The decisive invariant under test, at every step of every failure window:

    **expiring or failing a reservation never refunds spending allowance unless the
    reservation's own evidence proves the provider cannot have charged.**

Hermetic. No provider, no credential, no network: the application is driven through in-process
ASGI with a stub agent, the SQLite backend through a temporary store, and the Firestore-class
backend through the deterministic stub transactional store from
:mod:`civictrace.spend_stub_store` (which is *not* a live cloud backend; see
``CT-COR3B-REPORT.md``).

Matrix covered here (see ``CT-COR3C-REPORT.md`` for the table):

* crash before commit                      → no reservation, no spend, no history
* crash/dispatch after send, before outcome → ``uncertain``, allowance consumed
* timeout after send                       → ``uncertain``, allowance consumed, refund refused
* connect refused pre-dispatch             → ``failed_confirmed``, refund permitted but explicit
* HTTP status after transmission (CT-QC-041) → never ``failed_confirmed``; see
  ``tests/test_refund_eligibility.py`` for the 5xx matrix and the no-charge evidence classes
* expiry of an unresolved reservation      → ``uncertain`` + slot released + allowance retained
* late response after expiry               → settles once, same identity, no double charge
* repeated settlement                      → no-op beyond the first
* revision/deployment keying               → old-key reservation reconciles, history preserved
* duplicate request identities             → no second reservation, no second settlement
* append-only accounting history           → engine-enforced (UPDATE/DELETE refused)
* v1 store migration                       → rows, counters and history preserved
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import civictrace.app as app_module
from civictrace.spend_backend import SqliteSpendBackend, open_spend_backend
from civictrace.spend_firestore import DOCUMENT_SCHEMA, FirestoreConfig, FirestoreSpendBackend
from civictrace.spend_gate import (
    LEGACY_STORE_SCHEMA,
    LEGACY_STORE_SCHEMA_VERSION,
    STORE_SCHEMA,
    STORE_SCHEMA_VERSION,
    SpendGate,
)
from civictrace.spend_lifecycle import (
    CLASSIFICATION_TABLE,
    EVIDENCE_CONNECT_REFUSED,
    EVIDENCE_DROP_MID_CALL,
    EVIDENCE_NONE,
    EVIDENCE_PROVIDER_NO_CHARGE,
    EVIDENCE_PROVIDER_RESPONSE,
    EVIDENCE_TIMEOUT_AFTER_SEND,
    EVIDENCE_ZERO_USAGE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_FAILED_CONFIRMED,
    LIFECYCLE_SETTLED,
    LIFECYCLE_UNCERTAIN,
    SETTLEMENT_POLICY,
    classify,
    evidence_class_for_transport_error,
    request_digest,
)
from civictrace.spend_stub_store import MemoryDocumentStore, StubBoundedTransport
from civictrace.usage_ledger import (
    LIVE_REQUEST_IDENTITY_REUSED,
    LIVE_RESERVATION_NOT_REFUNDABLE,
    LiveBudget,
    UsageLedgerError,
)

ROOT = Path(__file__).resolve().parent.parent
_PROJECT = 'civictrace-test'
_DEPLOYMENT = 'deploy-alpha'

_GOVERNANCE_ENV = (
    'CIVICTRACE_SPEND_BACKEND', 'CIVICTRACE_SPEND_STORE_PATH', 'CIVICTRACE_USAGE_LEDGER_DIR',
    'CIVICTRACE_DEPLOYMENT_ID', 'CIVICTRACE_MAX_LIVE_ATTEMPTS', 'CIVICTRACE_MAX_LIVE_USD',
    'CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', 'CIVICTRACE_USAGE_LOCK_TIMEOUT_S',
    'CIVICTRACE_SPEND_RESERVATION_TTL_S', 'CIVICTRACE_INSTANCE_ID',
    'CIVICTRACE_SPEND_STORE_EXPECT_ROOT', 'CIVICTRACE_FIRESTORE_PROJECT',
    'CIVICTRACE_FIRESTORE_DATABASE', 'CIVICTRACE_FIRESTORE_COLLECTION',
    'CIVICTRACE_SPEND_BACKEND_TIMEOUT_S', 'CIVICTRACE_SPEND_BACKEND_ATTEMPTS',
    'CIVICTRACE_LIVE_ENABLED', 'NEBIUS_API_KEY', 'CIVICTRACE_LIVE_CHECK_ACK',
)

# Dies inside the reservation transaction after the row and counters are staged but before the
# COMMIT: the crash-before-commit window, with no chance to run cleanup handlers.
CRASH_BEFORE_COMMIT_WORKER = """
import os, time
from decimal import Decimal
from civictrace.spend_gate import SpendGate
from civictrace.usage_ledger import LiveBudget

budget = LiveBudget(request_cap=5, dollar_cap=Decimal("100.00"),
                    per_claim_estimate_usd=Decimal("0.01"))
gate = SpendGate()
with gate._transaction() as conn:
    row = gate._counters(conn)
    seq = int(row["requests_committed"]) + 1
    gate._commit_reservation(conn, budget, seq, time.time(), Decimal(row["estimated_spend_usd"]))
    print("prepared", flush=True)
    os._exit(9)
"""


def _budget(request_cap: int = 10, dollar_cap: str = '100.00', estimate: str = '0.01') -> LiveBudget:
    return LiveBudget(request_cap=request_cap, dollar_cap=Decimal(dollar_cap),
                      per_claim_estimate_usd=Decimal(estimate))


def _app_estimate() -> str:
    """The per-claim estimate the application reserves (worst case for one claim)."""
    from civictrace.nebius import REQUESTS_PER_CLAIM_CEILING
    budget = LiveBudget.from_env(requests_per_claim=REQUESTS_PER_CLAIM_CEILING)
    return str(budget.per_claim_estimate_usd.quantize(Decimal('0.000001')))


#: A gate whose lease is short enough that a reservation stamped in the past is already expired.
def _expiring_gate(**kwargs) -> SpendGate:
    return SpendGate(reservation_ttl_s=0.05, **kwargs)


def _config(project: str = _PROJECT) -> FirestoreConfig:
    return FirestoreConfig.build(project=project)


def _backend(store: MemoryDocumentStore, *, deployment: str = _DEPLOYMENT,
             instance: str = 'instance-1', ttl: float = 60.0) -> FirestoreSpendBackend:
    return FirestoreSpendBackend(config=_config(), transport=StubBoundedTransport(store),
                                 deployment_id=deployment, instance_id=instance,
                                 reservation_ttl_s=ttl, max_attempts=8, timeout_s=1.0,
                                 backoff_s=0.0)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    store = tmp_path / 'shared-store'
    store.mkdir()
    for name in _GOVERNANCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(store))
    app_module._peer_windows.clear()
    yield store


def _popen(code: str) -> subprocess.Popen:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    return subprocess.Popen([sys.executable, '-c', code], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class _StubRun:
    provider = {'kind': 'STUB_NO_PROVIDER_REQUEST', 'provider_requests': 1,
                'usage_as_returned': {'prompt_tokens': 10, 'completion_tokens': 5}}

    def as_json(self):
        return {'stub': True}


class _DispatchingStubAgent:
    """A stub agent that behaves like the real one *at the transmission boundary* only.

    It declares the dispatch hook, calls it before "sending", and then raises the exact
    telemetry a timeout-after-send produces. No provider, no HTTP, no credential.
    """

    supports_dispatch_hook = True

    def __init__(self, calls: list, *, evidence: str, dispatch: bool = True):
        self.calls = calls
        self.evidence = evidence
        self.dispatch = dispatch

    async def run(self, claim_id, *, on_dispatch=None):
        from civictrace.nebius import ProviderUnavailable
        self.calls.append(claim_id)
        if self.dispatch and on_dispatch is not None:
            await on_dispatch(1)
        raise ProviderUnavailable('PROVIDER_TIMEOUT_OR_NETWORK_ERROR', telemetry={
            'transmission_evidence': self.evidence,
            'transmission_dispatched': True,
            'provider_response_received': self.evidence == EVIDENCE_PROVIDER_RESPONSE,
            'usage_as_returned': {'prompt_tokens': None, 'completion_tokens': None},
            'usage_complete': False,
        })


def _live_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app, client=(
        '198.51.100.21', 51001)), base_url='http://test')


# -- the window that opens before anything is sent -------------------------------------------

def test_crash_before_commit_leaves_no_reservation_no_spend_and_no_history():
    gate = SpendGate()
    gate.read_state()  # create the schema so the crash happens inside a real transaction
    victim = _popen(CRASH_BEFORE_COMMIT_WORKER)
    out, err = victim.communicate(timeout=120)
    assert victim.returncode == 9, err
    assert out.strip().endswith('prepared')

    after = SpendGate().read_state()
    assert after['requests_committed'] == 0
    assert after['reservations']['total'] == 0
    assert after['estimated_spend_usd'] == '0.000000'
    assert after['accounting_history'] == []
    assert SpendGate().history() == []
    assert SpendGate().integrity_check() == 'ok'

    # The next admission is seq 1: a crashed write left no sequence or unit behind.
    decision = SpendGate().reserve(_budget(request_cap=2))
    assert decision.admitted and decision.seq == 1
    assert decision.accounting_state == LIFECYCLE_ACTIVE
    assert decision.request_digest and len(decision.request_digest) == 64


def test_dispatch_marker_is_durable_before_the_send_and_marks_uncertain():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    assert admission.accounting_state == LIFECYCLE_ACTIVE
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'

    assert gate.mark_transmitted(admission, round_num=1) is True
    state = gate.read_state()
    # The allowance did not move, but the lifecycle did: transmission is now possible.
    assert state['estimated_spend_usd'] == '0.010000'
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['reservations']['last_reservation']['transmitted_epoch'] is not None
    assert state['accounting']['uncertain'] == 1

    # Durability is the point: a *new* gate (a restarted process) sees the same marker.
    restarted = SpendGate()
    reloaded = restarted.read_state()
    assert reloaded['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert reloaded['requests_committed'] == 1 and reloaded['estimated_spend_usd'] == '0.010000'
    events = [entry['event'] for entry in restarted.history(admission.reservation_id)]
    assert events == ['reserve', 'transmission_started']

    # Repeated dispatch attempts are recorded but change nothing (no second transition).
    assert restarted.mark_transmitted(admission, round_num=2) is True
    assert restarted.read_state()['estimated_spend_usd'] == '0.010000'
    events = [entry['event'] for entry in restarted.history(admission.reservation_id)]
    assert events == ['reserve', 'transmission_started', 'dispatch_attempt']


def test_timeout_after_send_is_uncertain_and_refund_is_refused():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    evidence = {'transmission_evidence': EVIDENCE_TIMEOUT_AFTER_SEND,
                'transmission_dispatched': True}
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                       evidence=evidence) is True
    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['estimated_spend_usd'] == '0.010000', 'an uncertain outcome never refunds'
    assert state['requests_committed'] == 1

    with pytest.raises(UsageLedgerError) as excinfo:
        gate.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'
    assert [entry['event'] for entry in gate.history(admission.reservation_id)] == [
        'reserve', 'transmission_started', 'settle']


def test_connect_refused_is_failed_confirmed_and_refund_is_explicit_and_once_only():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                       evidence={'transmission_evidence': EVIDENCE_CONNECT_REFUSED}) is True
    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_FAILED_CONFIRMED
    assert state['estimated_spend_usd'] == '0.010000', 'settlement never refunds by itself'
    assert state['accounting']['failed_confirmed'] == 1

    refund = gate.refund_failed_confirmed(admission.reservation_id, reason='connect refused')
    assert refund['refunded_usd'] == '0.010000'
    state = gate.read_state()
    assert state['estimated_spend_usd'] == '0.000000'
    # The request counter is never decremented: sequence numbers stay monotonic.
    assert state['requests_committed'] == 1

    with pytest.raises(UsageLedgerError) as excinfo:
        gate.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert gate.read_state()['estimated_spend_usd'] == '0.000000'
    events = [entry['event'] for entry in gate.history(admission.reservation_id)]
    assert events.count('refund') == 1


# -- the expiry window ------------------------------------------------------------------------

def test_expiry_of_unresolved_reservation_is_uncertain_and_releases_only_the_slot():
    gate = _expiring_gate()
    stale = gate.reserve(_budget(request_cap=2), now_epoch=time.time() - 600.0)
    assert stale.admitted
    assert gate.sweep() == 1
    state = gate.read_state()
    assert state['reservations']['expired'] == 1, 'the lease (slot) is released'
    assert state['reservations']['uncertain'] == 1, 'the spend lifecycle is uncertain'
    assert state['estimated_spend_usd'] == '0.010000', 'the allowance is NOT restored'
    assert state['requests_committed'] == 1
    assert state['estimated_spend_usd'] == '0.010000'
    # Another sweep releases nothing: the expired lease is not re-released.
    assert gate.sweep() == 0

    # The slot really is free: an admission succeeds even though the unit is still consumed,
    # and the dollar ceiling still counts both units.
    second = gate.reserve(_budget(request_cap=2, dollar_cap='0.02'))
    assert second.admitted and second.seq == 2
    refused = SpendGate().reserve(_budget(request_cap=5, dollar_cap='0.02'))
    assert refused.code == 'LIVE_SPEND_BUDGET_EXHAUSTED'
    assert refused.estimated_spend_usd == Decimal('0.020000')

    with pytest.raises(UsageLedgerError):
        gate.refund_failed_confirmed(stale.reservation_id)
    assert gate.read_state()['estimated_spend_usd'] == '0.020000'


def test_late_response_after_expiry_reconciles_once_against_the_same_identity():
    gate = _expiring_gate()
    stale = gate.reserve(_budget(request_cap=3), now_epoch=time.time() - 600.0)
    assert gate.sweep() == 1

    first = gate.reconcile(stale.reservation_id, outcome='SUCCESS',
                           request_digest=stale.request_digest, provider_requests=1)
    assert first.applied is True and first.transitioned is False
    assert first.accounting_state == LIFECYCLE_SETTLED
    assert first.lease_state == 'settled_expired'
    assert first.request_digest == stale.request_digest
    state = gate.read_state()
    assert state['reservations']['settled_expired'] == 1
    assert state['estimated_spend_usd'] == '0.010000', 'a late settlement never re-charges'

    # The same late response arriving twice (retry, duplicate delivery) settles nothing further.
    again = gate.reconcile(stale.reservation_id, outcome='SUCCESS',
                           request_digest=stale.request_digest)
    assert again.applied is False and again.duplicate is True
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'
    receipt_kinds = [row['kind'] for row in gate.read_state()['receipts']]
    assert receipt_kinds.count('settle') == 1
    events = [entry['event'] for entry in gate.history(stale.reservation_id)]
    assert events == ['reserve', 'lease_expired', 'settle']

    # A different request intent must never settle this row.
    other = request_digest({'claim': 'a-different-request'})
    mismatch = gate.reconcile(stale.reservation_id, outcome='SUCCESS', request_digest=other)
    assert mismatch.applied is False and mismatch.reason == 'identity_mismatch'
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'


def test_repeated_settlement_is_a_noop_beyond_the_first():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    assert gate.settle(admission, outcome='SUCCESS', provider_requests=1) is True
    applied_before = len(gate.history())
    receipts_before = [row['kind'] for row in gate.read_state()['receipts']]
    for _ in range(3):
        assert gate.settle(admission, outcome='SUCCESS', provider_requests=1) is False
    assert len(gate.history()) == applied_before
    assert [row['kind'] for row in gate.read_state()['receipts']] == receipts_before
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'


def test_reconcile_resolves_the_reservation_from_the_request_identity_alone():
    """A replacement holds the request digest, not the row id: identity is enough."""
    gate = _expiring_gate()
    admission = gate.reserve(_budget(request_cap=3), now_epoch=time.time() - 600.0,
                             request_intent={'claim_id': 'spent', 'attempt': 4})
    assert admission.admitted
    assert gate.sweep() == 1

    replacement = SpendGate(instance_id='rev-2')
    result = replacement.reconcile(request_digest=admission.request_digest, outcome='SUCCESS')
    assert result.applied is True
    assert result.reservation_id == admission.reservation_id
    assert result.request_digest == admission.request_digest
    assert replacement.read_state()['estimated_spend_usd'] == '0.010000'

    again = replacement.reconcile(request_digest=admission.request_digest, outcome='SUCCESS')
    assert again.applied is False and again.duplicate is True
    assert replacement.read_state()['estimated_spend_usd'] == '0.010000'


# -- identity: duplicates, concurrency, revisions ----------------------------------------------

def test_duplicate_request_identity_never_double_reserves_under_concurrency():
    gate = SpendGate()
    intent = {'claim_id': 'spent', 'attempt': 1, 'model': 'nvidia/Nemotron-3_5-Lightning'}
    digest = request_digest(intent)
    admitted: list = []
    refused: list = []
    errors: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        local = SpendGate()
        barrier.wait(timeout=30)
        try:
            decision = local.reserve(_budget(request_cap=10), request_intent=intent)
        except UsageLedgerError as exc:  # pragma: no cover - would be a store fault
            with lock:
                errors.append(exc.code)
            return
        with lock:
            (admitted if decision.admitted else refused).append(decision)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(admitted) == 1, f'{len(admitted)} admissions for one request identity'
    assert len(refused) == 7
    winner = admitted[0]
    assert winner.request_digest == digest
    for decision in refused:
        assert decision.code == LIVE_REQUEST_IDENTITY_REUSED
        assert decision.duplicate_of == winner.reservation_id
        assert decision.reservation_id == winner.reservation_id

    state = gate.read_state()
    assert state['requests_committed'] == 1
    assert state['estimated_spend_usd'] == '0.010000'
    assert state['reservations']['total'] == 1
    # Exactly one settle is possible: the refusal carries duplicate_of and owns no transmission.
    assert gate.settle(refused[0], outcome='SUCCESS') is False
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'


def test_duplicate_identity_is_refused_across_instances_and_deployments_are_separate():
    intent = {'claim_id': 'spent', 'attempt': 7}
    first = SpendGate(instance_id='rev-1')
    decision = first.reserve(_budget(request_cap=10), request_intent=intent)
    assert decision.admitted

    second = SpendGate(instance_id='rev-2')
    duplicate = second.reserve(_budget(request_cap=10), request_intent=intent)
    assert duplicate.admitted is False
    assert duplicate.code == LIVE_REQUEST_IDENTITY_REUSED
    assert duplicate.duplicate_of == decision.reservation_id
    assert second.read_state()['requests_committed'] == 1

    other_deployment = SpendGate(deployment_id='deploy-beta', instance_id='rev-1')
    fresh = other_deployment.reserve(_budget(request_cap=10), request_intent=intent)
    assert fresh.admitted is True, 'a different deployment key has its own ceiling'
    assert other_deployment.read_state()['requests_committed'] == 1


def test_revision_change_reconciles_unresolved_reservation_and_preserves_history():
    old = _expiring_gate(instance_id='rev-1')
    admission = old.reserve(_budget(request_cap=3), now_epoch=time.time() - 900.0,
                            request_intent={'claim_id': 'spent', 'revision': 'rev-1'})
    assert admission.admitted
    assert old.mark_transmitted(admission, round_num=1)

    # A replacement revision (new instance id, same deployment constant) recovers the lease and
    # reconciles the late outcome against the *same* identity, from its own process.
    replacement = SpendGate(instance_id='rev-2')
    assert replacement.startup_sweep()['released'] == 1
    state = replacement.read_state()
    assert state['requests_committed'] == 1 and state['estimated_spend_usd'] == '0.010000'
    assert state['reservations']['uncertain'] == 1

    result = replacement.reconcile(admission.reservation_id, outcome='SUCCESS',
                                   request_digest=admission.request_digest, provider_requests=2)
    assert result.applied is True and result.transitioned is False
    assert result.accounting_state == LIFECYCLE_SETTLED
    final = replacement.read_state()
    assert final['requests_committed'] == 1 and final['estimated_spend_usd'] == '0.010000'

    # History written by the old revision is preserved and readable by the new one.
    events = [(entry['event'], entry.get('instance_id')) for entry
              in replacement.history(admission.reservation_id)]
    assert [event for event, _ in events] == ['reserve', 'transmission_started', 'lease_expired',
                                              'settle']
    assert events[0][1] == 'rev-1' and events[1][1] is None

    # A different deployment constant cannot even see the reservation.
    stranger = SpendGate(deployment_id='deploy-beta', instance_id='rev-9')
    unknown = stranger.reconcile(admission.reservation_id, outcome='SUCCESS')
    assert unknown.applied is False and unknown.reason == 'unknown_reservation'
    assert stranger.read_state()['requests_committed'] == 0


# -- history integrity ------------------------------------------------------------------------

def test_accounting_history_is_append_only_at_the_engine_level():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    before = gate.history()
    assert [entry['event'] for entry in before] == ['reserve', 'transmission_started']

    conn = sqlite3.connect(str(gate.path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounting_history SET reason = 'rewritten'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM accounting_history')
        conn.rollback()
    finally:
        conn.close()
    assert gate.history() == before


def test_v1_store_is_migrated_in_place_without_losing_rows_counters_or_history(monkeypatch):
    gate = SpendGate()
    path = gate.path
    # Build a store exactly as the pre-CT-QC-039 code would have left it.
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(f"""
            CREATE TABLE store_meta (id INTEGER PRIMARY KEY CHECK (id = 1), schema TEXT NOT NULL,
                schema_version INTEGER NOT NULL, deployment_id TEXT NOT NULL, created_utc TEXT NOT NULL);
            CREATE TABLE counters (deployment_id TEXT PRIMARY KEY, requests_committed INTEGER NOT NULL,
                estimated_spend_usd TEXT NOT NULL, caps_observed TEXT, created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL);
            CREATE TABLE reservations (reservation_id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL,
                seq INTEGER NOT NULL, instance_id TEXT NOT NULL, reserved_epoch REAL NOT NULL,
                reserved_utc TEXT NOT NULL, expires_epoch REAL NOT NULL, state TEXT NOT NULL,
                estimated_usd TEXT NOT NULL, outcome TEXT, provider_requests INTEGER,
                prompt_tokens INTEGER, completion_tokens INTEGER, settled_epoch REAL,
                released_epoch REAL, released_by TEXT, UNIQUE (deployment_id, seq));
            CREATE TABLE receipts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                deployment_id TEXT NOT NULL, kind TEXT NOT NULL, utc TEXT NOT NULL,
                payload TEXT NOT NULL);
            INSERT INTO store_meta VALUES (1, '{LEGACY_STORE_SCHEMA}',
                {LEGACY_STORE_SCHEMA_VERSION}, 'civictrace-rc1', '2026-10-01T00:00:00+00:00');
            INSERT INTO counters VALUES ('civictrace-rc1', 2, '0.020000', NULL,
                '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00');
            INSERT INTO reservations VALUES ('legacy-expired', 'civictrace-rc1', 1, 'old-instance',
                1.0, '2026-10-01T00:00:01+00:00', 2.0, 'expired', '0.010000', NULL, NULL, NULL,
                NULL, NULL, NULL, 'old-instance');
            INSERT INTO reservations VALUES ('legacy-settled', 'civictrace-rc1', 2, 'old-instance',
                3.0, '2026-10-01T00:00:02+00:00', 900.0, 'settled', '0.010000', 'SUCCESS', 1,
                10, 5, 4.0, NULL, NULL);
        """)
        conn.commit()
    finally:
        conn.close()

    migrated = SpendGate()
    state = migrated.read_state()
    assert state['store']['schema'] == STORE_SCHEMA
    assert state['requests_committed'] == 2, 'the migration never resets the counter'
    assert state['estimated_spend_usd'] == '0.020000', 'the migration never refunds'
    assert state['reservations']['expired'] == 1
    assert state['reservations']['uncertain'] == 1, 'a legacy expired row is uncertain, not free'
    assert state['reservations']['accounting_settled'] == 1
    assert state['accounting_history'] != []
    migrate_events = [entry['event'] for entry in migrated.history('legacy-expired')]
    assert migrate_events == ['migrate_store_schema']
    assert [row['kind'] for row in state['receipts']] == ['migrate']
    assert migrated.integrity_check() == 'ok'

    # The legacy uncertain row may not be refunded, and it still consumes allowance.
    with pytest.raises(UsageLedgerError) as excinfo:
        migrated.refund_failed_confirmed('legacy-expired')
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert migrated.read_state()['estimated_spend_usd'] == '0.020000'

    # A migrated store keeps working: a new admission continues the same sequence.
    decision = migrated.reserve(_budget(request_cap=10))
    assert decision.admitted and decision.seq == 3
    assert migrated.settle(decision, outcome='SUCCESS') is True
    assert STORE_SCHEMA_VERSION == 2


# -- the decisive rule, driven through every phase -------------------------------------------

def test_uncertain_spend_is_never_automatically_refunded_in_any_phase():
    gate = _expiring_gate()
    budget = _budget(request_cap=10)

    # Phase 1: dispatched, then timed out.
    timed_out = gate.reserve(budget)
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'
    gate.mark_transmitted(timed_out, round_num=1)
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'
    gate.settle(timed_out, outcome='PROVIDER_UNAVAILABLE',
                evidence={'transmission_evidence': EVIDENCE_DROP_MID_CALL})
    for step in range(2):
        gate.settle(timed_out, outcome='PROVIDER_UNAVAILABLE',
                    evidence={'transmission_evidence': EVIDENCE_DROP_MID_CALL})
        assert gate.read_state()['estimated_spend_usd'] == '0.010000', \
            f'allowance restored on repeat {step}'

    # Phase 2: expired with no outcome at all, then reconciled late.
    expired = gate.reserve(budget, now_epoch=time.time() - 900.0)
    assert gate.sweep() == 1
    assert gate.read_state()['estimated_spend_usd'] == '0.020000'
    gate.reconcile(expired.reservation_id, outcome='PROVIDER_UNAVAILABLE')  # still unknown
    assert gate.read_state()['estimated_spend_usd'] == '0.020000'
    gate.reconcile(expired.reservation_id, outcome='PROVIDER_UNAVAILABLE')
    assert gate.read_state()['estimated_spend_usd'] == '0.020000'

    # Phase 3: no evidence at all (the conservative default).
    unknown = gate.reserve(budget)
    gate.settle(unknown, outcome='PROVIDER_UNAVAILABLE')
    assert gate.read_state()['estimated_spend_usd'] == '0.030000'

    for reservation_id in (timed_out.reservation_id, expired.reservation_id,
                           unknown.reservation_id):
        with pytest.raises(UsageLedgerError) as excinfo:
            gate.refund_failed_confirmed(reservation_id)
        assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE

    final = gate.read_state()
    assert final['estimated_spend_usd'] == '0.030000'
    assert final['requests_committed'] == 3
    assert final['accounting']['refunded'] == 0
    assert final['accounting']['auto_refund_of_uncertain'] is False
    assert all('allowance_refunded' in entry and entry['allowance_refunded'] is False
               for entry in final['accounting_history']
               if entry['event'] in {'lease_expired', 'settle'})


# -- the same contract on the Firestore-class backend ----------------------------------------

def test_firestore_backend_expiry_is_uncertain_and_retains_allowance():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3), now_epoch=time.time() - 900.0)
    assert admission.admitted and admission.accounting_state == LIFECYCLE_ACTIVE
    assert backend.mark_transmitted(admission, round_num=1) is True
    status = backend.status()
    assert status['reservations']['uncertain'] == 1
    assert status['estimated_spend_usd'] == '0.010000'

    assert backend.sweep() == 1
    status = backend.status()
    assert status['reservations']['expired'] == 1
    assert status['reservations']['uncertain'] == 1
    assert status['estimated_spend_usd'] == '0.010000'
    document = store.snapshot(backend.document_id)
    row = document['reservations'][admission.reservation_id]
    assert row['accounting_state'] == LIFECYCLE_UNCERTAIN
    kinds = [entry['event'] for entry in document['accounting_history']]
    assert kinds == ['reserve', 'transmission_started', 'lease_expired']

    with pytest.raises(UsageLedgerError) as excinfo:
        backend.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert backend.status()['estimated_spend_usd'] == '0.010000'


def test_firestore_backend_late_response_settles_once_and_repeats_are_noops():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3), now_epoch=time.time() - 900.0)
    backend.sweep()
    first = backend.reconcile(admission.reservation_id, outcome='SUCCESS',
                              request_digest=admission.request_digest, provider_requests=1)
    assert first.applied is True and first.transitioned is False
    assert first.accounting_state == LIFECYCLE_SETTLED
    applied = store.counters()['applied']
    again = backend.reconcile(admission.reservation_id, outcome='SUCCESS',
                              request_digest=admission.request_digest)
    assert again.applied is False and again.duplicate is True
    assert store.counters()['applied'] == applied, 'a repeat settlement writes nothing'
    status = backend.status()
    assert status['reservations']['settled_expired'] == 1
    assert status['estimated_spend_usd'] == '0.010000'


def test_firestore_backend_reconciles_from_the_request_identity_alone():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3), now_epoch=time.time() - 900.0,
                                request_intent={'claim_id': 'spent', 'attempt': 9})
    assert admission.admitted
    assert backend.sweep() == 1
    replacement = _backend(store, instance='instance-2')
    result = replacement.reconcile(request_digest=admission.request_digest, outcome='SUCCESS')
    assert result.applied is True and result.reservation_id == admission.reservation_id
    assert replacement.status()['estimated_spend_usd'] == '0.010000'
    assert replacement.reconcile(request_digest=admission.request_digest,
                                 outcome='SUCCESS').duplicate is True


def test_firestore_backend_duplicate_identity_is_refused_and_refund_needs_evidence():
    store = MemoryDocumentStore()
    backend = _backend(store)
    intent = {'claim_id': 'spent', 'attempt': 2}
    admission = backend.reserve(_budget(request_cap=5), request_intent=intent)
    assert admission.admitted
    duplicate = _backend(store, instance='instance-2').reserve(_budget(request_cap=5),
                                                              request_intent=intent)
    assert duplicate.admitted is False
    assert duplicate.code == LIVE_REQUEST_IDENTITY_REUSED
    assert duplicate.duplicate_of == admission.reservation_id
    assert backend.status()['requests_committed'] == 1

    # Provider-confirmed no-charge record: failed_confirmed, refundable, exactly once.
    # A status code is NOT such a record (CT-QC-041); the 5xx matrix lives in
    # tests/test_refund_eligibility.py.
    backend.mark_transmitted(admission, round_num=1)
    assert backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                          evidence={'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
                                    'provider_response_received': True,
                                    'no_charge_confirmed': True}) is True
    assert backend.status()['reservations']['failed_confirmed'] == 1
    refund = backend.refund_failed_confirmed(admission.reservation_id)
    assert refund['refunded_usd'] == '0.010000'
    assert backend.status()['estimated_spend_usd'] == '0.000000'
    assert backend.status()['requests_committed'] == 1
    with pytest.raises(UsageLedgerError):
        backend.refund_failed_confirmed(admission.reservation_id)
    assert store.snapshot(backend.document_id)['accounting_history'][-1]['event'] == 'refund'


def test_both_backends_publish_the_same_vocabulary_and_policy():
    from civictrace import spend_gate

    store = MemoryDocumentStore()
    firestore_backend = _backend(store)
    sqlite_backend = SqliteSpendBackend()
    assert spend_gate._SETTLEMENT_POLICY == SETTLEMENT_POLICY
    assert firestore_backend.name == 'firestore' and sqlite_backend.name == 'sqlite'
    assert firestore_backend.status()['settlement_policy'] == SETTLEMENT_POLICY
    sqlite_backend.reserve(_budget(request_cap=2))
    assert sqlite_backend.status()['settlement_policy'] == SETTLEMENT_POLICY
    for backend in (firestore_backend, sqlite_backend):
        status = backend.status()
        assert set(status['accounting']['states']) == {LIFECYCLE_ACTIVE, LIFECYCLE_SETTLED,
                                                       LIFECYCLE_FAILED_CONFIRMED,
                                                       LIFECYCLE_UNCERTAIN}
        assert status['accounting']['auto_refund_of_uncertain'] is False
        assert status['accounting']['history_append_only'] is True


# -- evidence classification: the table the report documents ----------------------------------

#: Telemetry each post-transmission no-charge class requires. A class *name* is a claim; these
#: fields are the provider confirmation that makes it evidence (CT-QC-041).
_NO_CHARGE_SUPPORT_FIELDS = {
    EVIDENCE_PROVIDER_NO_CHARGE: {'provider_response_received': True,
                                  'no_charge_confirmed': True},
    EVIDENCE_ZERO_USAGE: {'usage_evidence_source': 'PROVIDER_SETTLEMENT_RECORD',
                          'usage_units': 0},
}


@pytest.mark.parametrize('evidence_class, expected_state',
                         [(row[0], row[1]) for row in CLASSIFICATION_TABLE])
def test_classification_table_matches_the_documented_states(evidence_class, expected_state):
    evidence = {'transmission_evidence': evidence_class,
                **_NO_CHARGE_SUPPORT_FIELDS.get(evidence_class, {})}
    result = classify('PROVIDER_UNAVAILABLE', evidence, dispatched=True)
    assert result.accounting_state == expected_state
    assert result.refundable is (
        expected_state == LIFECYCLE_FAILED_CONFIRMED and evidence_class != EVIDENCE_NONE)


@pytest.mark.parametrize('exception_name, expected', [
    ('ConnectError', EVIDENCE_CONNECT_REFUSED),
    ('ConnectTimeout', 'CONNECT_TIMEOUT_PRE_DISPATCH'),
    ('PoolTimeout', 'LOCAL_FAILURE_PRE_DISPATCH'),
    ('ReadTimeout', EVIDENCE_TIMEOUT_AFTER_SEND),
    ('WriteTimeout', EVIDENCE_TIMEOUT_AFTER_SEND),
    ('RemoteProtocolError', EVIDENCE_DROP_MID_CALL),
    ('SomethingNew', EVIDENCE_DROP_MID_CALL),
])
def test_transport_errors_map_to_documented_evidence_classes(exception_name, expected):
    assert evidence_class_for_transport_error(exception_name) == expected


def test_unknown_or_missing_evidence_always_classifies_uncertain():
    for evidence in (None, {}, {'transmission_evidence': 'MADE_UP_CLASS'},
                     {'transmission_evidence': EVIDENCE_NONE, 'transmission_dispatched': True}):
        result = classify('PROVIDER_UNAVAILABLE', evidence)
        assert result.accounting_state == LIFECYCLE_UNCERTAIN
        assert result.refundable is False
    success = classify('SUCCESS', None)
    assert success.accounting_state == LIFECYCLE_SETTLED and success.refundable is False


# -- the application path ---------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('evidence_class', [EVIDENCE_TIMEOUT_AFTER_SEND, EVIDENCE_NONE])
async def test_app_marks_dispatch_durably_and_keeps_uncertain_allowance(monkeypatch,
                                                                        evidence_class):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list = []
    monkeypatch.setattr(app_module, 'configured_live_agent',
                        lambda court: _DispatchingStubAgent(calls, evidence=evidence_class))
    async with _live_client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 502
    assert response.json()['detail']['code'] == 'PROVIDER_TIMEOUT_OR_NETWORK_ERROR'
    assert calls == ['spent']

    state = SpendGate().read_state()
    assert state['requests_committed'] == 1
    assert state['estimated_spend_usd'] == _app_estimate(), 'the allowance stays consumed'
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['reservations']['last_reservation']['transmitted_epoch'] is not None
    events = [entry['event'] for entry in state['accounting_history']]
    assert events == ['reserve', 'transmission_started', 'settle']
    assert SpendGate().history()[-1]['allowance_refunded'] is False


@pytest.mark.asyncio
async def test_app_refunds_nothing_when_the_provider_never_saw_the_request(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list = []
    monkeypatch.setattr(app_module, 'configured_live_agent',
                        lambda court: _DispatchingStubAgent(
                            calls, evidence=EVIDENCE_CONNECT_REFUSED, dispatch=True))
    async with _live_client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 502
    state = SpendGate().read_state()
    # Even the refundable class is not refunded by the request path: refunds are explicit.
    assert state['reservations']['last_reservation']['accounting_state'] == \
        LIFECYCLE_FAILED_CONFIRMED
    assert state['estimated_spend_usd'] == _app_estimate()
    assert state['accounting']['refunded'] == 0


@pytest.mark.asyncio
async def test_app_does_not_offer_the_hook_to_an_agent_that_does_not_declare_it(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list = []

    class _PlainStub:
        async def run(self, claim_id):  # deliberately no on_dispatch parameter
            calls.append(claim_id)
            return _StubRun()

    monkeypatch.setattr(app_module, 'configured_live_agent', lambda court: _PlainStub())
    async with _live_client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 200
    assert calls == ['spent']
    state = SpendGate().read_state()
    # No dispatch evidence exists, so the success settlement is 'settled' and never refundable.
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_SETTLED
    assert state['estimated_spend_usd'] == _app_estimate()


def test_document_schema_is_unchanged_by_the_additive_lifecycle_fields():
    """The Firestore document stayed backward-compatible: rows without the new fields still read.

    A pre-CT-QC-039 document (no ``accounting_state``, no ``request_digest``, no history) must be
    readable and must classify an expired row as uncertain rather than active — the migration for
    a document is a derivation, not a rewrite.
    """
    store = MemoryDocumentStore()
    backend = _backend(store)
    assert backend.reserve(_budget(request_cap=2)).admitted
    document = store.snapshot(backend.document_id)
    assert document['schema'] == DOCUMENT_SCHEMA
    for row in document['reservations'].values():
        for key in ('accounting_state', 'request_digest', 'transmitted_epoch', 'evidence_class',
                    'refunded_epoch'):
            row.pop(key, None)
    document['reservations'] = {
        key: {**row, 'state': 'expired'} for key, row in document['reservations'].items()}
    document.pop('accounting_history', None)
    document.pop('accounting_history_evicted', None)
    revision = store.revision(backend.document_id)
    assert store.commit(backend.document_id, document, revision)['status'] == 'applied'

    status = backend.status()
    assert status['reservations']['expired'] == 1
    assert status['reservations']['uncertain'] == 1, 'a legacy expired row is uncertain'
    assert status['reservations']['active'] == 0
    assert status['estimated_spend_usd'] == '0.010000'
    with pytest.raises(UsageLedgerError):
        backend.refund_failed_confirmed(next(iter(document['reservations'])))


def test_selector_still_opens_the_sqlite_backend_with_the_lifecycle_surface(_isolated):
    backend = open_spend_backend()
    assert isinstance(backend, SqliteSpendBackend)
    admission = backend.reserve(_budget(request_cap=2),
                               request_intent={'claim_id': 'spent', 'attempt': 1})
    assert admission.admitted and admission.request_digest
    assert backend.mark_transmitted(admission, round_num=1) is True
    assert backend.status()['accounting']['uncertain'] == 1
    assert [entry['event'] for entry in backend.history(admission.reservation_id)] == [
        'reserve', 'transmission_started']
    assert json.loads(json.dumps(backend.status()))  # the audit read is JSON-serializable
