"""CT-QC-038 regression: swappable atomic accounting backend, proved offline.

Hermetic: no cloud project, no credential, no network, no provider. The Firestore-class backend
is exercised against a deterministic stub transactional store (:mod:`civictrace.spend_stub_store`)
that replicates exactly the three store guarantees the backend relies on — one snapshot per read,
atomic revision-checked apply, retryable contention — and the local SQLite backend is exercised
against a real temporary store.

**These tests do not verify a live Cloud Firestore backend.** They verify that the backend code,
the selection contract, the fail-closed paths and the no-fallback rule behave as documented when
the store behaves the way a Firestore transaction does. Cloud Run qualification stays open until
deployment-specific evidence exists (``CT-COR3B-REPORT.md``).
"""
from __future__ import annotations

import inspect
import os
import threading
import time
from decimal import Decimal

import httpx
import pytest
from google.api_core import exceptions as api_exceptions

import civictrace.app as app_module
import civictrace.spend_firestore as firestore_module
from civictrace.spend_backend import (
    BACKEND_FIRESTORE,
    BACKEND_SQLITE,
    KNOWN_BACKENDS,
    SpendBackend,
    SqliteSpendBackend,
    open_spend_backend,
    resolve_backend_name,
)
from civictrace.spend_firestore import (
    DOCUMENT_SCHEMA,
    MAX_TRACKED_RESERVATIONS,
    FirestoreConfig,
    FirestoreSpendBackend,
    build_transport,
)
from civictrace.spend_firestore_client import FirestoreBoundedTransport
from civictrace.spend_stub_store import MemoryDocumentStore, StubBoundedTransport
from fake_firestore import FakeFirestoreClient, FakeStore
from civictrace.usage_ledger import (
    LIVE_REQUEST_BUDGET_EXHAUSTED,
    LIVE_SPEND_BACKEND_CONTENDED,
    LIVE_SPEND_BACKEND_INVALID,
    LIVE_SPEND_BACKEND_NOT_CONFIGURED,
    LIVE_SPEND_BACKEND_UNAVAILABLE,
    LIVE_SPEND_BUDGET_EXHAUSTED,
    LIVE_USAGE_LEDGER_CORRUPT,
    LiveBudget,
    UsageConfigError,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerUnavailable,
)

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


def _budget(request_cap=10, dollar_cap='100.00', estimate='0.01') -> LiveBudget:
    return LiveBudget(request_cap=request_cap, dollar_cap=Decimal(dollar_cap),
                      per_claim_estimate_usd=Decimal(estimate))


def _config(project=_PROJECT, collection='civictrace-spend') -> FirestoreConfig:
    return FirestoreConfig.build(project=project, collection=collection)


def _backend(store: MemoryDocumentStore, *, transport=None, deployment=_DEPLOYMENT,
             instance='instance-1', ttl=60.0, attempts=8, timeout=1.0, backoff=0.0
             ) -> FirestoreSpendBackend:
    return FirestoreSpendBackend(
        config=_config(), transport=transport or StubBoundedTransport(store),
        deployment_id=deployment, instance_id=instance, reservation_ttl_s=ttl,
        max_attempts=attempts, timeout_s=timeout, backoff_s=backoff)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    store = tmp_path / 'shared-store'
    store.mkdir()
    for name in _GOVERNANCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(store))
    app_module._peer_windows.clear()
    yield store


class _StubRun:
    provider = {'kind': 'STUB_NO_PROVIDER_REQUEST', 'provider_requests': 1,
                'usage_as_returned': {'prompt_tokens': 10, 'completion_tokens': 5}}

    def as_json(self):
        return {'stub': True}


class _StubAgent:
    def __init__(self, calls):
        self.calls = calls

    async def run(self, claim_id):
        self.calls.append(claim_id)
        return _StubRun()


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list[str] = []
    monkeypatch.setattr(app_module, 'configured_live_agent', lambda court: _StubAgent(calls))
    return calls


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                             base_url='http://test')


def _sqlite_files(store_dir) -> list:
    return sorted(store_dir.glob('spend-*.sqlite3'))


# -- selection contract: sqlite | firestore, never a default for an unknown value --------------

def test_default_selector_builds_the_preserved_sqlite_backend(_isolated):
    backend = open_spend_backend()
    assert isinstance(backend, SqliteSpendBackend)
    assert backend.name == BACKEND_SQLITE
    assert isinstance(backend, SpendBackend)
    assert backend.deployment_id == 'civictrace-rc1'
    assert backend.reserve(_budget(request_cap=1)).admitted
    status = backend.status()
    assert status['backend'] == BACKEND_SQLITE
    assert status['requests_committed'] == 1


def test_unknown_selector_is_a_typed_refusal_and_never_defaulted(_isolated, monkeypatch):
    with pytest.raises(UsageConfigError) as direct:
        open_spend_backend(selector='dynamodb')
    assert direct.value.code == LIVE_SPEND_BACKEND_INVALID
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'sqlite3')
    with pytest.raises(UsageConfigError) as from_env:
        open_spend_backend()
    assert from_env.value.code == LIVE_SPEND_BACKEND_INVALID
    # Nothing was opened and nothing was written: an unknown name is not "close enough" to SQLite.
    assert _sqlite_files(_isolated) == []


def test_selector_accepts_exactly_the_documented_values(_isolated, monkeypatch):
    assert resolve_backend_name() == BACKEND_SQLITE
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', ' Firestore ')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    # CT-QC-042 wires a real transport at this point. The selector resolves to firestore, the
    # backend is constructed, and an unusable store still refuses typed — it is NOT a fallback
    # to the SQLite backend.
    store = FakeStore()
    store.inject_read_failure(1, lambda: api_exceptions.ServiceUnavailable('down'))
    backend = open_spend_backend(transport=FirestoreBoundedTransport(
        _config(), client=FakeFirestoreClient(store)))
    assert backend.name == BACKEND_FIRESTORE
    with pytest.raises(UsageLedgerUnavailable) as refused:
        backend.reserve(_budget(request_cap=1))
    assert refused.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.document_count() == 0
    assert _sqlite_files(_isolated) == []


def test_offline_doubles_are_not_selectable(_isolated):
    for name in ('stub', 'memory', 'inmemory', 'local', 'file'):
        assert name not in KNOWN_BACKENDS
        with pytest.raises(UsageConfigError):
            open_spend_backend(selector=name)


def test_a_transport_with_the_sqlite_selector_is_a_refusal(_isolated):
    """Selector and wiring disagreeing is refused, not silently resolved in either direction."""
    store = MemoryDocumentStore()
    with pytest.raises(UsageConfigError) as excinfo:
        open_spend_backend(selector='sqlite', transport=StubBoundedTransport(store))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_INVALID
    assert _sqlite_files(_isolated) == []


def test_both_backends_publish_the_same_operation_surface(_isolated):
    for cls in (SqliteSpendBackend, FirestoreSpendBackend):
        for method in ('reserve', 'settle', 'sweep', 'status', 'startup_sweep'):
            assert callable(getattr(cls, method))
        assert issubclass(cls, SpendBackend)
        for method in ('reserve', 'settle', 'sweep', 'status'):
            implementation = inspect.signature(getattr(cls, method))
            contract = inspect.signature(getattr(SpendBackend, method))
            assert list(implementation.parameters) == list(contract.parameters)
            assert [p.kind for p in implementation.parameters.values()] == \
                   [p.kind for p in contract.parameters.values()]


# -- configuration and wiring point: absent config or transport refuses, never substitutes -----

@pytest.mark.parametrize('project', [None, '', '   ', 'Bad_Project', 'UPPER', 'x', 'a' * 80])
def test_firestore_project_is_required_and_validated(project):
    with pytest.raises(UsageConfigError) as excinfo:
        FirestoreConfig.build(project=project)
    assert excinfo.value.code == LIVE_SPEND_BACKEND_NOT_CONFIGURED


@pytest.mark.parametrize('collection', ['a/b', '..', 'with space', 'x' * 200])
def test_firestore_collection_is_validated(collection):
    with pytest.raises(UsageConfigError):
        FirestoreConfig.build(project=_PROJECT, collection=collection)


def test_firestore_selected_without_project_refuses_at_construction(_isolated, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    with pytest.raises(UsageConfigError) as excinfo:
        open_spend_backend()
    assert excinfo.value.code == LIVE_SPEND_BACKEND_NOT_CONFIGURED
    assert _sqlite_files(_isolated) == []


def test_cloud_selector_never_constructs_the_sqlite_backend(_isolated, monkeypatch):
    """Structural no-fallback proof: the sqlite constructor is never reached for a cloud selector."""
    import civictrace.spend_backend as spend_backend_module

    def _forbidden(*args, **kwargs):
        raise AssertionError('SQLite backend was constructed for a firestore deployment')

    store = FakeStore()
    store.inject_read_failure(1, lambda: api_exceptions.ServiceUnavailable('down'))
    monkeypatch.setattr(spend_backend_module, 'SqliteSpendBackend', _forbidden)
    monkeypatch.setattr(firestore_module, 'build_transport',
                        lambda config, **kwargs: FirestoreBoundedTransport(
                            config, client=FakeFirestoreClient(store)))
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    backend = open_spend_backend()          # the real adapter is constructed, not SQLite
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=1))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.document_count() == 0
    assert _sqlite_files(_isolated) == []


def test_a_broken_sdk_fails_closed_at_construction_and_never_falls_back(monkeypatch):
    """CT-QC-042 replaced the 'no transport is wired' refusal: the SDK is the new failure point."""
    import civictrace.spend_firestore_client as adapter_module

    def _missing():
        raise ModuleNotFoundError('No module named google.cloud.firestore')

    monkeypatch.setattr(adapter_module, '_load_sdk', _missing)
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        build_transport(_config())
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert 'no provider request was made' in excinfo.value.message
    # Constructing the backend is itself the refusal point (before any admission attempt).
    with pytest.raises(UsageLedgerUnavailable):
        FirestoreSpendBackend(config=_config(), deployment_id=_DEPLOYMENT,
                              instance_id='instance-1', reservation_ttl_s=60.0, backoff_s=0.0)


def test_firestore_timeout_and_attempt_bounds_are_validated(_isolated, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND_TIMEOUT_S', '0')
    with pytest.raises(UsageConfigError) as timeout_error:
        open_spend_backend(selector='firestore', transport=StubBoundedTransport(MemoryDocumentStore()))
    assert timeout_error.value.code == LIVE_SPEND_BACKEND_NOT_CONFIGURED
    monkeypatch.delenv('CIVICTRACE_SPEND_BACKEND_TIMEOUT_S')
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND_ATTEMPTS', '99')
    with pytest.raises(UsageConfigError) as attempts_error:
        open_spend_backend(selector='firestore', transport=StubBoundedTransport(MemoryDocumentStore()))
    assert attempts_error.value.code == LIVE_SPEND_BACKEND_NOT_CONFIGURED


# -- the decisive property: atomic check + reservation under concurrency ----------------------

def test_concurrent_workers_admit_exactly_the_cap_and_never_more():
    """8 workers hold overlapping snapshots of one document; exactly ``cap`` reservations exist."""
    store = MemoryDocumentStore()
    cap = 5
    workers = 8
    attempts_per_worker = 3
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    outcomes: list[dict] = []

    def worker(index: int) -> None:
        backend = _backend(store, instance=f'instance-{index}', attempts=16,
                           transport=StubBoundedTransport(store, rendezvous=barrier))
        admitted = refused = errors = 0
        codes: dict[str, int] = {}
        seqs: list[int] = []
        for _ in range(attempts_per_worker):
            try:
                decision = backend.reserve(_budget(request_cap=cap))
            except UsageLedgerError as exc:
                errors += 1
                codes[exc.code] = codes.get(exc.code, 0) + 1
                continue
            if decision.admitted:
                admitted += 1
                seqs.append(decision.seq)
            else:
                refused += 1
                codes[decision.code] = codes.get(decision.code, 0) + 1
        with lock:
            outcomes.append({'index': index, 'admitted': admitted, 'refused': refused,
                             'errors': errors, 'codes': codes, 'seqs': seqs,
                             'retries': backend.retry_count})

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()

    total_admitted = sum(item['admitted'] for item in outcomes)
    assert total_admitted == cap, outcomes
    assert sum(item['errors'] for item in outcomes) == 0, outcomes
    assert store.counters()['contended'] >= 1, 'the retry path was never exercised'
    assert sum(item['retries'] for item in outcomes) >= 1

    status = _backend(store, instance='auditor').status()
    assert status['requests_committed'] == cap
    assert status['reservations']['total'] == cap
    assert status['reservations']['evicted'] == 0
    assert sorted(seq for item in outcomes for seq in item['seqs']) == list(range(1, cap + 1))


def test_two_instances_share_one_ceiling_and_one_sequence():
    store = MemoryDocumentStore()
    first = _backend(store, instance='instance-a')
    second = _backend(store, instance='instance-b')
    decisions = [first.reserve(_budget(request_cap=2)).seq, second.reserve(_budget(request_cap=2)).seq]
    assert decisions == [1, 2]
    refused = first.reserve(_budget(request_cap=2))
    assert refused.admitted is False and refused.code == LIVE_REQUEST_BUDGET_EXHAUSTED
    assert second.status()['requests_committed'] == 2


def test_document_key_is_the_deployment_constant_only(_isolated, monkeypatch):
    store = MemoryDocumentStore()
    first = _backend(store, instance='instance-a')
    second = _backend(store, instance='instance-b')
    # A different instance id, a differing CIVICTRACE_INSTANCE_ID and a revision-label env change
    # must all key the same document: the deployment constant is the whole key.
    monkeypatch.setenv('CIVICTRACE_INSTANCE_ID', 'instance-c')
    third = _backend(store, instance='instance-c')
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID', _DEPLOYMENT)
    fourth = _backend(store, deployment=None)
    assert first.document_id == second.document_id == third.document_id == fourth.document_id
    assert first.document_id.endswith(f'/{_config().document_id(_DEPLOYMENT)}')
    assert first.document_id.startswith('projects/civictrace-test/databases/(default)/documents/')
    other = _backend(store, deployment='deploy-beta')
    assert other.document_id != first.document_id


def test_rollback_and_replacement_continue_the_same_count():
    store = MemoryDocumentStore()
    original = _backend(store, instance='instance-old')
    first = original.reserve(_budget(request_cap=1))
    assert first.admitted and first.seq == 1

    # A replacement process (new instance id) after a revision rollback: same deployment constant.
    replacement = _backend(store, instance='instance-new')
    refused = replacement.reserve(_budget(request_cap=1))
    assert refused.admitted is False and refused.code == LIVE_REQUEST_BUDGET_EXHAUSTED
    assert refused.requests_committed == 1

    # A newer revision that raises the ceiling continues the count instead of restarting it.
    newer = _backend(store, instance='instance-newer')
    second = newer.reserve(_budget(request_cap=2))
    assert second.admitted and second.seq == 2
    status = newer.status()
    assert status['requests_committed'] == 2
    assert status['reservations']['total'] == 2


def test_dollar_cap_binds_before_the_request_cap():
    store = MemoryDocumentStore()
    backend = _backend(store)
    budget = _budget(request_cap=10, dollar_cap='1.00', estimate='0.60')
    first = backend.reserve(budget)
    assert first.admitted and first.estimated_spend_usd == Decimal('0.60')
    second = backend.reserve(budget)
    assert second.admitted is False and second.code == LIVE_SPEND_BUDGET_EXHAUSTED
    status = backend.status()
    assert status['estimated_spend_usd'] == '0.600000'
    assert status['requests_committed'] == 1


# -- retry semantics: one reservation, one charge, one settlement -----------------------------

def test_retried_reserve_creates_one_reservation_and_charges_once():
    store = MemoryDocumentStore()
    backend = _backend(store, attempts=5)
    store.inject_contention(2)
    decision = backend.reserve(_budget(request_cap=3))
    assert decision.admitted
    assert backend.retry_count == 2
    assert store.counters()['applied'] == 1
    status = backend.status()
    assert status['requests_committed'] == 1
    assert status['reservations']['total'] == 1
    assert status['estimated_spend_usd'] == '0.010000'
    document = store.snapshot(backend.document_id)
    assert list(document['reservations']) == [decision.reservation_id]


def test_exhausted_contention_is_a_typed_refusal_and_charges_nothing():
    store = MemoryDocumentStore()
    backend = _backend(store, attempts=3)
    store.inject_contention(10)
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=3))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_CONTENDED
    assert store.counters()['applied'] == 0
    assert store.document_count() == 0
    store.inject_contention(0)
    assert backend.status()['requests_committed'] == 0


def test_retried_settle_settles_exactly_once():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=2))
    assert admission.admitted
    store.inject_contention(1)  # the settle transaction aborts once, then re-reads and applies
    assert backend.settle(admission, outcome='SUCCESS', provider_requests=1) is True
    applied_before = store.counters()['applied']
    # A retried settlement (lost reply, an app retry, a duplicate call) must not settle twice.
    assert backend.settle(admission, outcome='SUCCESS', provider_requests=1) is False
    assert store.counters()['applied'] == applied_before
    document = store.snapshot(backend.document_id)
    row = document['reservations'][admission.reservation_id]
    assert row['state'] == 'settled'
    assert row['outcome'] == 'SUCCESS'
    settle_receipts = [r for r in document['receipts'] if r['kind'] == 'settle']
    assert len(settle_receipts) == 1
    status = backend.status()
    assert status['requests_committed'] == 1
    assert status['reservations']['settled'] == 1


def test_settlement_after_lease_expiry_keeps_the_committed_unit():
    store = MemoryDocumentStore()
    backend = _backend(store, ttl=60.0)
    admission = backend.reserve(_budget(request_cap=2), now_epoch=time.time() - 600.0)
    assert admission.admitted
    assert backend.sweep() == 1
    status = backend.status()
    assert status['reservations']['expired'] == 1
    assert status['requests_committed'] == 1
    assert backend.settle(admission, outcome='PROVIDER_UNAVAILABLE') is False
    status = backend.status()
    assert status['reservations']['settled_expired'] == 1
    # Releasing a lease never refunds the unit: the ceiling stays an upper bound.
    assert status['requests_committed'] == 1
    assert status['estimated_spend_usd'] == '0.010000'


def test_sweep_releases_each_lease_exactly_once():
    store = MemoryDocumentStore()
    backend = _backend(store, ttl=60.0)
    stale = time.time() - 600.0
    assert backend.reserve(_budget(request_cap=4), now_epoch=stale).admitted
    assert backend.reserve(_budget(request_cap=4), now_epoch=stale).admitted
    assert backend.sweep() == 2
    assert backend.sweep() == 0
    assert _backend(store, instance='instance-2').sweep(released_by='replacement') == 0
    assert backend.status()['requests_committed'] == 2


# -- fail-closed: unreachable, deadline, corrupt, missing ------------------------------------

def test_unreachable_transport_refuses_at_admission_and_touches_nothing():
    store = MemoryDocumentStore()
    backend = _backend(store, transport=StubBoundedTransport(store, failures=1))
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=3))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.counters()['commits'] == 0
    assert store.document_count() == 0


def test_deadline_breach_is_a_typed_refusal():
    store = MemoryDocumentStore()
    backend = _backend(store, transport=StubBoundedTransport(store, latency_s=5.0), timeout=0.05)
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=3))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.document_count() == 0


@pytest.mark.parametrize('mutate,reason', [
    (lambda doc: doc.update(schema='other/1'), 'schema'),
    (lambda doc: doc.update(deployment_id='deploy-other'), 'deployment'),
    (lambda doc: doc.update(requests_committed=-4), 'negative'),
    (lambda doc: doc.update(estimated_spend_usd='not-a-number'), 'money'),
    (lambda doc: doc.update(requests_committed=7), 'reconcile'),
    (lambda doc: doc.update(schema_version=99), 'version'),
])
def test_invalid_document_is_refused_and_never_reset(mutate, reason):
    store = MemoryDocumentStore()
    backend = _backend(store)
    assert backend.reserve(_budget(request_cap=2)).admitted
    document = store.snapshot(backend.document_id)
    mutate(document)
    revision = store.revision(backend.document_id)
    assert store.commit(backend.document_id, document, revision)['status'] == 'applied'
    damaged = store.snapshot(backend.document_id)

    with pytest.raises(UsageLedgerCorrupt) as excinfo:
        backend.reserve(_budget(request_cap=2))
    assert excinfo.value.code == LIVE_USAGE_LEDGER_CORRUPT
    assert reason  # parametrisation label only
    # The damaged document is left exactly as found: no rewrite-to-zero, no re-initialisation.
    assert store.snapshot(backend.document_id) == damaged
    assert store.counters()['commits'] == 2  # the seed write and the refused transaction's read


def test_missing_document_is_a_first_run_and_status_never_writes():
    store = MemoryDocumentStore()
    backend = _backend(store)
    status = backend.status()
    assert status['requests_committed'] == 0
    assert status['revision'] is None
    assert status['reservations']['total'] == 0
    assert store.document_count() == 0 and store.counters()['commits'] == 0
    admission = backend.reserve(_budget(request_cap=1))
    assert admission.admitted and admission.seq == 1
    assert store.counters()['created'] == 1
    assert store.snapshot(backend.document_id)['schema'] == DOCUMENT_SCHEMA


def test_document_is_bound_to_one_deployment():
    store = MemoryDocumentStore()
    alpha = _backend(store, deployment='deploy-alpha')
    beta = _backend(store, deployment='deploy-beta')
    assert alpha.reserve(_budget(request_cap=1)).admitted
    assert alpha.reserve(_budget(request_cap=1)).admitted is False
    # A different deployment constant has its own ceiling and its own document.
    assert beta.reserve(_budget(request_cap=1)).admitted
    assert store.document_count() == 2


def test_reservation_audit_map_is_bounded_and_counters_still_reconcile():
    store = MemoryDocumentStore()
    backend = _backend(store)
    total = MAX_TRACKED_RESERVATIONS + 60
    for _ in range(total):
        assert backend.reserve(_budget(request_cap=total + 10)).admitted
    status = backend.status()
    assert status['requests_committed'] == total
    assert status['reservations']['total'] == MAX_TRACKED_RESERVATIONS
    assert status['reservations']['evicted'] == total - MAX_TRACKED_RESERVATIONS
    # The invariant is re-checked on every read; a later admission still succeeds.
    assert backend.reserve(_budget(request_cap=total + 10)).seq == total + 1


def test_settlement_policy_text_is_identical_for_both_backends():
    from civictrace import spend_gate
    assert firestore_module._SETTLEMENT_POLICY == spend_gate._SETTLEMENT_POLICY


# -- the application: 503 before the provider call, and no silent fallback --------------------

@pytest.mark.asyncio
async def test_configured_firestore_without_project_refuses_live_before_the_provider(
        live_env, _isolated, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    async with _client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
        offline = await client.post('/api/challenge', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == LIVE_SPEND_BACKEND_NOT_CONFIGURED
    assert live_env == []
    assert _sqlite_files(_isolated) == []
    assert offline.status_code == 200  # the deterministic path is untouched by a governance outage


@pytest.mark.asyncio
async def test_configured_firestore_unreachable_refuses_live_before_the_provider(
        live_env, _isolated, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    store = MemoryDocumentStore()
    monkeypatch.setattr(firestore_module, 'build_transport',
                        lambda config: StubBoundedTransport(store, failures=1))
    async with _client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert live_env == []
    assert store.document_count() == 0
    assert _sqlite_files(_isolated) == []


@pytest.mark.asyncio
async def test_unknown_backend_selector_refuses_live_before_the_provider(
        live_env, _isolated, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'gcs-object')
    async with _client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == LIVE_SPEND_BACKEND_INVALID
    assert live_env == []
    assert _sqlite_files(_isolated) == []


@pytest.mark.asyncio
async def test_firestore_backend_serves_a_live_request_end_to_end_offline(
        live_env, _isolated, monkeypatch):
    """The wiring is one patched function: backend, transport, document, admission, settlement."""
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID', _DEPLOYMENT)
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '1')
    store = MemoryDocumentStore()
    monkeypatch.setattr(firestore_module, 'build_transport',
                        lambda config: StubBoundedTransport(store))
    async with _client() as client:
        first = await client.post('/api/investigate', json={'claim_id': 'spent'})
        second = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert first.status_code == 200 and first.json() == {'stub': True}
    assert live_env == ['spent']  # the provider stub ran only for the admitted request
    assert second.status_code == 429
    assert second.json()['detail']['code'] == LIVE_REQUEST_BUDGET_EXHAUSTED
    document = store.snapshot(_config().document_path(_DEPLOYMENT))
    assert document['requests_committed'] == 1
    reservation = next(iter(document['reservations'].values()))
    assert reservation['state'] == 'settled' and reservation['outcome'] == 'SUCCESS'
    assert _sqlite_files(_isolated) == []


@pytest.mark.asyncio
async def test_default_backend_still_serves_the_sqlite_path(_isolated, live_env):
    async with _client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 200
    assert live_env == ['spent']
    assert len(_sqlite_files(_isolated)) == 1


def test_startup_sweep_uses_the_configured_backend_and_fails_soft(_isolated, monkeypatch):
    store = MemoryDocumentStore()
    backend = _backend(store, ttl=0.2)
    stale = time.time() - 600.0
    assert backend.reserve(_budget(request_cap=3), now_epoch=stale).admitted
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID', _DEPLOYMENT)
    monkeypatch.setattr(firestore_module, 'build_transport',
                        lambda config: StubBoundedTransport(store))
    result = app_module.startup_sweep()
    assert result is not None and result['released'] == 1
    assert result['backend'] == BACKEND_FIRESTORE
    assert app_module.startup_sweep()['released'] == 0
    # A misconfigured backend is a soft failure at startup (admission still fails closed).
    monkeypatch.delenv('CIVICTRACE_FIRESTORE_PROJECT')
    assert app_module.startup_sweep() is None
