"""CT-QC-042 regression: the real Firestore transport adapter, proved with injected offline clients.

Every test in this module drives :class:`civictrace.spend_firestore_client.FirestoreBoundedTransport`
— the production adapter — and the real :class:`civictrace.spend_firestore.FirestoreSpendBackend`
lifecycle behind it. The *only* substitute is the store itself: :mod:`fake_firestore` implements
the exact SDK surface the adapter calls (client, collection, document reference, transaction,
snapshot, write result) with the SDK's own value and exception types.

Hermetic: no cloud project, no credential resolution, no socket, no provider. Nothing here is a
qualification of a live Cloud Firestore deployment — IAM, quotas, latency, the server's own
transaction timeout, regional behaviour and credential acquisition are all unexercised. Live
backend qualification remains owner-gated and open (``CT-COR4A-REPORT.md``).
"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from google.api_core import exceptions as api_exceptions
from google.auth.exceptions import DefaultCredentialsError

import civictrace.app as app_module
import civictrace.spend_firestore as firestore_module
import civictrace.spend_firestore_client as adapter_module
from civictrace.spend_firestore import (
    FirestoreConfig,
    FirestoreSpendBackend,
    StoreContention,
    StoreInvalid,
    StoreUnavailable,
)
from civictrace.spend_firestore_client import (
    SDK_REQUIREMENT,
    FirestoreBoundedTransport,
)
from civictrace.spend_lifecycle import (
    EVIDENCE_CONNECT_REFUSED,
    EVIDENCE_PROVIDER_NO_CHARGE,
    EVIDENCE_PROVIDER_RESPONSE,
    EVIDENCE_ZERO_USAGE,
    LIFECYCLE_FAILED_CONFIRMED,
    LIFECYCLE_SETTLED,
    LIFECYCLE_UNCERTAIN,
)
from civictrace.usage_ledger import (
    LIVE_REQUEST_BUDGET_EXHAUSTED,
    LIVE_REQUEST_IDENTITY_REUSED,
    LIVE_RESERVATION_NOT_REFUNDABLE,
    LIVE_RESERVATION_UNKNOWN,
    LIVE_SPEND_BACKEND_CONTENDED,
    LIVE_SPEND_BACKEND_INVALID,
    LIVE_SPEND_BACKEND_NOT_CONFIGURED,
    LIVE_SPEND_BACKEND_UNAVAILABLE,
    LIVE_USAGE_LEDGER_CORRUPT,
    LiveBudget,
    UsageConfigError,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerUnavailable,
)
from fake_firestore import FakeFirestoreClient, FakeStore

_PROJECT = 'civictrace-test'
_DEPLOYMENT = 'deploy-alpha'
_COLLECTION = 'civictrace-spend'
_OTHER_PROJECT = 'other-project-123'

_GOVERNANCE_ENV = (
    'CIVICTRACE_SPEND_BACKEND', 'CIVICTRACE_SPEND_STORE_PATH', 'CIVICTRACE_USAGE_LEDGER_DIR',
    'CIVICTRACE_DEPLOYMENT_ID', 'CIVICTRACE_MAX_LIVE_ATTEMPTS', 'CIVICTRACE_MAX_LIVE_USD',
    'CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', 'CIVICTRACE_USAGE_LOCK_TIMEOUT_S',
    'CIVICTRACE_SPEND_RESERVATION_TTL_S', 'CIVICTRACE_INSTANCE_ID',
    'CIVICTRACE_SPEND_STORE_EXPECT_ROOT', 'CIVICTRACE_FIRESTORE_PROJECT',
    'CIVICTRACE_FIRESTORE_DATABASE', 'CIVICTRACE_FIRESTORE_COLLECTION',
    'CIVICTRACE_SPEND_BACKEND_TIMEOUT_S', 'CIVICTRACE_SPEND_BACKEND_ATTEMPTS',
    'CIVICTRACE_LIVE_ENABLED', 'NEBIUS_API_KEY', 'CIVICTRACE_LIVE_CHECK_ACK',
    'GOOGLE_APPLICATION_CREDENTIALS',
)

_SUPPORTED_ZERO_USAGE = {'transmission_evidence': EVIDENCE_ZERO_USAGE,
                         'usage_evidence_source': 'PROVIDER_SETTLEMENT_RECORD',
                         'usage_units': 0}
_SUPPORTED_NO_CHARGE = {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
                        'provider_response_received': True, 'no_charge_confirmed': True}


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    store_dir = tmp_path / 'shared-store'
    store_dir.mkdir()
    for name in _GOVERNANCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(store_dir))
    app_module._peer_windows.clear()
    yield store_dir


def _budget(request_cap=10, dollar_cap='100.00', estimate='0.01') -> LiveBudget:
    return LiveBudget(request_cap=request_cap, dollar_cap=Decimal(dollar_cap),
                      per_claim_estimate_usd=Decimal(estimate))


def _config(project=_PROJECT, collection=_COLLECTION) -> FirestoreConfig:
    return FirestoreConfig.build(project=project, collection=collection)


def _transport(store: FakeStore, *, config=None, client=None) -> FirestoreBoundedTransport:
    return FirestoreBoundedTransport(config or _config(),
                                     client=client or FakeFirestoreClient(store))


def _backend(store: FakeStore, *, instance='instance-1', ttl=60.0, attempts=8,
             timeout_s=1.0, transport=None) -> FirestoreSpendBackend:
    return FirestoreSpendBackend(config=_config(), transport=transport or _transport(store),
                                 deployment_id=_DEPLOYMENT, instance_id=instance,
                                 reservation_ttl_s=ttl, timeout_s=timeout_s,
                                 max_attempts=attempts, backoff_s=0.0)


def _document(store: FakeStore) -> dict:
    document = store.snapshot()
    assert document is not None, 'no accounting document was written'
    return document


# -- 1. the adapter's two operations over an injected client ----------------------------------

def test_read_returns_the_stored_document_and_its_revision():
    store = FakeStore()
    transport = _transport(store)
    document_id = _config().document_path(_DEPLOYMENT)

    first = transport.call('read', {'document_id': document_id}, deadline_s=1.0)
    assert first == {'revision': None, 'document': None}

    body = {'schema': 'civictrace-spend-document/1', 'schema_version': 1, 'marker': 'x'}
    reply = transport.call('commit', {'document_id': document_id, 'document': body,
                                      'expect_revision': None}, deadline_s=1.0)
    assert reply['status'] == 'applied'
    assert isinstance(reply['revision'], int) and reply['revision'] > 0

    second = transport.call('read', {'document_id': document_id}, deadline_s=1.0)
    assert second['document'] == body
    assert second['revision'] == reply['revision']
    assert transport.reads == 2 and transport.commits == 1 and transport.applied == 1
    assert transport._client.network_calls == 0     # nothing in this path opens a socket


def test_an_absent_document_is_a_first_run_not_a_corrupt_store():
    transport = _transport(FakeStore())
    reply = transport.call('read', {'document_id': _config().document_path(_DEPLOYMENT)},
                           deadline_s=1.0)
    assert reply == {'revision': None, 'document': None}


def test_a_stale_revision_is_contended_and_writes_nothing():
    store = FakeStore()
    transport = _transport(store)
    document_id = _config().document_path(_DEPLOYMENT)
    transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                              'expect_revision': None}, deadline_s=1.0)
    with pytest.raises(StoreContention):
        transport.call('commit', {'document_id': document_id, 'document': {'v': 2},
                                  'expect_revision': None}, deadline_s=1.0)
    assert _document(store) == {'v': 1}
    assert store.counters()['revision'] == 1


def test_commit_of_a_stale_existing_revision_is_refused():
    store = FakeStore()
    transport = _transport(store)
    document_id = _config().document_path(_DEPLOYMENT)
    transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                              'expect_revision': None}, deadline_s=1.0)
    revision = transport.call('read', {'document_id': document_id},
                              deadline_s=1.0)['revision']
    transport.call('commit', {'document_id': document_id, 'document': {'v': 2},
                              'expect_revision': revision}, deadline_s=1.0)
    with pytest.raises(StoreContention):
        transport.call('commit', {'document_id': document_id, 'document': {'v': 3},
                                  'expect_revision': revision}, deadline_s=1.0)
    assert _document(store) == {'v': 2}


def test_a_document_outside_the_configured_store_is_refused_before_any_call():
    store = FakeStore()
    transport = _transport(store)
    foreign = FirestoreConfig.build(project=_OTHER_PROJECT).document_path(_DEPLOYMENT)
    with pytest.raises(StoreUnavailable) as excinfo:
        transport.call('read', {'document_id': foreign}, deadline_s=1.0)
    assert 'outside the configured accounting store' in str(excinfo.value)
    other_collection = FirestoreConfig.build(project=_PROJECT,
                                             collection='another-collection').document_path(
        _DEPLOYMENT)
    with pytest.raises(StoreUnavailable):
        transport.call('commit', {'document_id': other_collection, 'document': {},
                                  'expect_revision': None}, deadline_s=1.0)
    assert store.document_count() == 0
    assert store.counters()['reads'] == 0 and store.counters()['commits'] == 0


def test_a_non_positive_deadline_or_unknown_operation_is_refused():
    store = FakeStore()
    transport = _transport(store)
    document_id = _config().document_path(_DEPLOYMENT)
    for deadline in (0, -1.0, float('nan'), True):
        with pytest.raises(StoreUnavailable):
            transport.call('read', {'document_id': document_id}, deadline_s=deadline)
    with pytest.raises(StoreUnavailable):
        transport.call('delete', {'document_id': document_id}, deadline_s=1.0)
    assert store.counters()['reads'] == 0


def test_a_malformed_payload_is_refused_rather_than_written():
    store = FakeStore()
    transport = _transport(store)
    with pytest.raises(StoreUnavailable):
        transport.call('read', {'document_id': ''}, deadline_s=1.0)
    with pytest.raises(StoreInvalid):
        transport.call('commit', {'document_id': _config().document_path(_DEPLOYMENT),
                                  'document': 'not-a-map', 'expect_revision': None},
                       deadline_s=1.0)
    with pytest.raises(StoreInvalid):
        transport.call('commit', {'document_id': _config().document_path(_DEPLOYMENT),
                                  'document': {}, 'expect_revision': 'four'}, deadline_s=1.0)
    assert store.document_count() == 0


def test_an_existing_document_without_a_concurrency_token_is_untrustworthy():
    store = FakeStore()
    store.document = {'schema': 'civictrace-spend-document/1'}
    store.revision = 1
    store.omit_update_time = True
    with pytest.raises(StoreInvalid):
        _transport(store).call('read', {'document_id': _config().document_path(_DEPLOYMENT)},
                               deadline_s=1.0)


# -- 2. vendor failure taxonomy -----------------------------------------------------------------

def test_store_read_failures_map_to_store_unavailable():
    store = FakeStore()
    store.inject_read_failure(1, lambda: api_exceptions.PermissionDenied('denied'))
    transport = _transport(store)
    with pytest.raises(StoreUnavailable):
        transport.call('read', {'document_id': _config().document_path(_DEPLOYMENT)},
                       deadline_s=1.0)
    store.inject_read_failure(1, lambda: api_exceptions.ServiceUnavailable('unavailable'))
    with pytest.raises(StoreUnavailable):
        transport.call('read', {'document_id': _config().document_path(_DEPLOYMENT)},
                       deadline_s=1.0)


def test_a_deadline_breach_is_store_unavailable_not_a_false_success():
    store = FakeStore()
    store.latency_s = 5.0
    transport = _transport(store)
    with pytest.raises(StoreUnavailable):
        transport.call('read', {'document_id': _config().document_path(_DEPLOYMENT)},
                       deadline_s=0.05)


def test_commit_contention_and_vendor_failures_map_to_the_seam_taxonomy():
    store = FakeStore()
    document_id = _config().document_path(_DEPLOYMENT)
    transport = _transport(store)
    store.inject_contention(1)
    with pytest.raises(StoreContention):
        transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                                  'expect_revision': None}, deadline_s=1.0)
    assert store.document_count() == 0

    store.inject_commit_failure(1, lambda: api_exceptions.ServiceUnavailable('down'))
    with pytest.raises(StoreUnavailable):
        transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                                  'expect_revision': None}, deadline_s=1.0)
    assert store.document_count() == 0

    store.inject_commit_failure(1, lambda: api_exceptions.ResourceExhausted('quota'))
    with pytest.raises(StoreUnavailable):
        transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                                  'expect_revision': None}, deadline_s=1.0)
    assert store.document_count() == 0
    assert transport.contended == 1


# -- 3. construction: real SDK, explicit config, fail closed -----------------------------------

def test_construction_without_an_injected_client_never_touches_the_network(monkeypatch):
    """The real factory is replaced, not the adapter: construction is the only thing proved."""
    store = FakeStore()
    monkeypatch.setattr(adapter_module, '_new_client',
                        lambda config: FakeFirestoreClient(store, project=config.project,
                                                          database=config.database))
    transport = firestore_module.build_transport(_config())
    assert isinstance(transport, FirestoreBoundedTransport)
    assert transport.config.project == _PROJECT
    document_id = _config().document_path(_DEPLOYMENT)
    transport.call('commit', {'document_id': document_id, 'document': {'v': 1},
                              'expect_revision': None}, deadline_s=1.0)
    assert _document(store) == {'v': 1}


def test_the_real_client_is_built_once_per_project_and_database(monkeypatch):
    from types import SimpleNamespace

    built: list[tuple[str, str]] = []

    def _client(project=None, database=None):
        built.append((project, database))
        return FakeFirestoreClient(FakeStore(), project=project, database=database)

    monkeypatch.setattr(adapter_module, '_CLIENTS', {})
    monkeypatch.setattr(adapter_module, '_load_sdk',
                        lambda: SimpleNamespace(firestore=SimpleNamespace(Client=_client)))
    first = adapter_module._new_client(_config())
    assert adapter_module._new_client(_config()) is first
    assert built == [(_PROJECT, '(default)')]
    other = adapter_module._new_client(FirestoreConfig.build(project=_PROJECT,
                                                             database='spend-db'))
    assert other is not first
    assert built == [(_PROJECT, '(default)'), (_PROJECT, 'spend-db')]


def test_the_pinned_sdk_offers_the_surface_the_adapter_calls():
    """Contract with the pinned distribution: the call shapes this adapter writes must exist."""
    import importlib.metadata
    import inspect

    import google.cloud.firestore as firestore

    assert importlib.metadata.version('google-cloud-firestore') == SDK_REQUIREMENT.split('==')[1]
    assert callable(firestore.transactional)
    client_signature = inspect.signature(firestore.Client.__init__)
    assert {'project', 'database'}.issubset(client_signature.parameters)
    assert callable(getattr(firestore.Client, 'collection', None))
    transaction = firestore.Client.transaction
    assert {'max_attempts', 'read_only'}.issubset(inspect.signature(transaction).parameters)
    from google.cloud.firestore_v1.transaction import Transaction
    for member in ('get', 'set', 'commit', '_begin', '_commit', '_clean_up', '_rollback',
                   'in_progress'):
        assert hasattr(Transaction, member), member
    assert inspect.signature(Transaction.commit).parameters.get('timeout') is not None


def test_the_sdk_is_imported_only_through_the_adapter_module():
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / 'civictrace'
    importers = []
    for path in sorted(root.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name == 'google' or name.startswith('google.') for name in names):
                importers.append(path.name)
    assert sorted(set(importers)) == ['spend_firestore_client.py']


def test_an_unimportable_sdk_is_a_typed_refusal(monkeypatch):
    def _missing():
        raise ModuleNotFoundError('No module named google.cloud.firestore')

    monkeypatch.setattr(adapter_module, '_load_sdk', _missing)
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        firestore_module.build_transport(_config())
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert SDK_REQUIREMENT in excinfo.value.message
    assert 'no provider request was made' in excinfo.value.message


def test_unusable_credentials_refuse_at_construction(monkeypatch):
    def _no_credentials(config):
        raise DefaultCredentialsError('no ADC')

    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        FirestoreBoundedTransport(_config(), client_factory=_no_credentials)
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert 'no provider request was made' in excinfo.value.message


def test_a_client_for_another_project_or_no_client_is_refused():
    with pytest.raises(UsageLedgerUnavailable):
        FirestoreBoundedTransport(_config(), client=FakeFirestoreClient(FakeStore(),
                                                                        project=_OTHER_PROJECT))
    with pytest.raises(UsageLedgerUnavailable):
        FirestoreBoundedTransport(_config(), client_factory=lambda config: None)
    with pytest.raises(UsageConfigError) as excinfo:
        FirestoreBoundedTransport(_config(), client=FakeFirestoreClient(FakeStore()),
                                  client_factory=lambda config: None)
    assert excinfo.value.code == LIVE_SPEND_BACKEND_INVALID


def test_the_adapter_prefix_matches_the_backend_document_path():
    """Drift guard: the adapter's prefix and FirestoreConfig.document_path must agree."""
    config = _config()
    transport = _transport(FakeStore(), config=config)
    assert config.document_path(_DEPLOYMENT) == f'{transport._prefix}spend-{_DEPLOYMENT}'


# -- 4. the reserved/settle lifecycle through the real adapter ---------------------------------

def test_reserve_writes_one_document_and_settles_exactly_once():
    store = FakeStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3))
    assert admission.admitted and admission.seq == 1
    assert store.counters()['applied'] == 1

    assert backend.settle(admission, outcome='SUCCESS', provider_requests=1,
                          prompt_tokens=10, completion_tokens=5) is True
    status = backend.status()
    assert status['requests_committed'] == 1
    assert status['accounting']['by_state'] == {LIFECYCLE_SETTLED: 1}
    assert status['estimated_spend_usd'] == '0.010000'
    assert backend.settle(admission, outcome='SUCCESS') is False   # no second settlement
    assert backend.status()['requests_committed'] == 1

    row = _document(store)['reservations'][admission.reservation_id]
    assert row['state'] == 'settled' and row['accounting_state'] == LIFECYCLE_SETTLED
    events = [entry['event'] for entry in _document(store)['accounting_history']]
    assert events == ['reserve', 'settle']
    assert backend.history(admission.reservation_id)[-1]['event'] == 'settle'


def test_a_duplicate_request_identity_never_reserves_or_settles_twice():
    store = FakeStore()
    backend = _backend(store)
    intent = {'claim_id': 'spent', 'attempt': 1}
    first = backend.reserve(_budget(request_cap=5), request_intent=intent)
    assert first.admitted
    duplicate = backend.reserve(_budget(request_cap=5), request_intent=intent)
    assert duplicate.admitted is False
    assert duplicate.code == LIVE_REQUEST_IDENTITY_REUSED
    assert duplicate.duplicate_of == first.reservation_id
    assert duplicate.seq == first.seq
    assert backend.status()['requests_committed'] == 1
    assert store.counters()['applied'] == 1
    assert backend.settle(duplicate, outcome='SUCCESS') is False


def test_the_request_cap_and_the_dollar_cap_bind_over_the_adapter():
    store = FakeStore()
    backend = _backend(store)
    assert backend.reserve(_budget(request_cap=1)).admitted
    refused = backend.reserve(_budget(request_cap=1))
    assert refused.admitted is False and refused.code == LIVE_REQUEST_BUDGET_EXHAUSTED

    cheap = _budget(request_cap=10, dollar_cap='0.015')
    blocked = backend.reserve(cheap)
    assert blocked.admitted is False and blocked.code == 'LIVE_SPEND_BUDGET_EXHAUSTED'
    assert backend.status()['requests_committed'] == 1


def test_a_foreign_document_in_the_store_is_refused_and_never_reset():
    store = FakeStore()
    store.document = {'schema': 'someone-elses-document/9', 'schema_version': 9,
                      'requests_committed': 0}
    store.revision = 1
    with pytest.raises(UsageLedgerCorrupt) as excinfo:
        _backend(store).reserve(_budget())
    assert excinfo.value.code == LIVE_USAGE_LEDGER_CORRUPT
    assert _document(store)['schema'] == 'someone-elses-document/9'
    assert store.counters()['revision'] == 1


# -- 5. contention: deterministic bounded retry, never an unverified admission ------------------

def test_contention_is_retried_then_admitted_exactly_once():
    store = FakeStore()
    backend = _backend(store, attempts=8)
    store.inject_contention(2)
    admission = backend.reserve(_budget(request_cap=3))
    assert admission.admitted and admission.seq == 1
    assert backend.retry_count == 2
    assert backend.attempts == 3
    status = backend.status()
    assert status['requests_committed'] == 1
    assert store.counters()['aborted'] == 2 and store.counters()['applied'] == 1


def test_contention_exhaustion_is_a_typed_refusal_and_applies_nothing():
    store = FakeStore()
    backend = _backend(store, attempts=3)
    store.inject_contention(99)
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=3))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_CONTENDED
    assert backend.retry_count == 2 and backend.attempts == 3
    assert store.document_count() == 0


def test_two_instances_racing_on_one_snapshot_admit_exactly_one():
    store = FakeStore()
    barrier = store.use_rendezvous(2)
    first, second = _backend(store, instance='instance-1'), _backend(store, instance='instance-2')
    results: list[object] = []
    errors: list[BaseException] = []

    def _run(backend, attempt):
        try:
            results.append(backend.reserve(_budget(request_cap=1),
                                           request_intent={'claim_id': 'spent', 'attempt': attempt}))
        except BaseException as exc:            # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(first, 1)),
               threading.Thread(target=_run, args=(second, 2))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert sum(1 for admission in results if admission.admitted) == 1
    assert store.counters()['revision'] == 1
    assert _document(store)['requests_committed'] == 1
    assert store.counters()['aborted'] == 1
    assert barrier.broken is False


def test_a_transport_failure_during_admission_refuses_before_any_provider_call():
    store = FakeStore()
    store.inject_read_failure(1, lambda: api_exceptions.ServiceUnavailable('down'))
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        _backend(store).reserve(_budget())
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.document_count() == 0

    store.inject_commit_failure(1, lambda: api_exceptions.DeadlineExceeded('slow'))
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        _backend(store).reserve(_budget())
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert store.document_count() == 0


# -- 6. CT-QC-039/041 safeguards re-run through the adapter path --------------------------------

def test_sweep_releases_the_lease_and_keeps_the_allowance_consumed():
    store = FakeStore()
    backend = _backend(store, ttl=0.2)
    admission = backend.reserve(_budget(request_cap=3), now_epoch=1_000.0)
    assert backend.mark_transmitted(admission, round_num=1) is True
    assert backend.sweep(now_epoch=1_000.0 + 5.0) == 1      # lease released, once
    assert backend.sweep(now_epoch=1_000.0 + 5.0) == 0
    status = backend.status()
    assert status['requests_committed'] == 1
    assert status['estimated_spend_usd'] == '0.010000'      # never refunded by expiry
    assert status['reservations']['expired'] == 1
    assert status['accounting']['by_state'] == {LIFECYCLE_UNCERTAIN: 1}
    with pytest.raises(UsageLedgerError) as excinfo:
        backend.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE


def test_a_5xx_after_transmission_stays_uncertain_and_is_never_refunded():
    store = FakeStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3))
    assert backend.mark_transmitted(admission, round_num=1) is True
    assert backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                          evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE,
                                    'http_status': 500}) is True
    status = backend.status()
    assert status['accounting']['by_state'] == {LIFECYCLE_UNCERTAIN: 1}
    assert status['estimated_spend_usd'] == '0.010000'
    with pytest.raises(UsageLedgerError) as excinfo:
        backend.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert backend.status()['estimated_spend_usd'] == '0.010000'
    row = _document(store)['reservations'][admission.reservation_id]
    assert row['evidence_class'] == EVIDENCE_PROVIDER_RESPONSE
    assert row['refunded_epoch'] is None


def test_a_supported_zero_usage_record_reconciles_and_refunds_exactly_once():
    store = FakeStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3),
                                request_intent={'claim_id': 'spent', 'attempt': 2})
    backend.mark_transmitted(admission, round_num=1)
    backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                   evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE,
                             'http_status': 504})

    replacement = _backend(store, instance='instance-2')
    first = replacement.reconcile(request_digest=admission.request_digest,
                                  outcome='PROVIDER_UNAVAILABLE',
                                  evidence=_SUPPORTED_ZERO_USAGE)
    assert first.applied is True and first.transitioned is False
    assert first.accounting_state == LIFECYCLE_FAILED_CONFIRMED
    assert replacement.reconcile(request_digest=admission.request_digest,
                                 outcome='PROVIDER_UNAVAILABLE',
                                 evidence=_SUPPORTED_ZERO_USAGE).duplicate is True

    refund = replacement.refund_failed_confirmed(admission.reservation_id)
    assert refund['refunded_usd'] == '0.010000'
    status = replacement.status()
    assert status['estimated_spend_usd'] == '0.000000'
    assert status['requests_committed'] == 1
    with pytest.raises(UsageLedgerError) as excinfo:
        replacement.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    events = [entry['event'] for entry in _document(store)['accounting_history']]
    assert events == ['reserve', 'transmission_started', 'settle', 'reconcile_accounting',
                      'refund']


def test_an_unsupported_no_charge_claim_cannot_create_a_refund():
    store = FakeStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3))
    backend.mark_transmitted(admission, round_num=1)
    backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                   evidence={'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
                             'provider_response_received': True, 'no_charge_confirmed': False})
    assert backend.status()['accounting']['by_state'] == {LIFECYCLE_UNCERTAIN: 1}
    with pytest.raises(UsageLedgerError):
        backend.refund_failed_confirmed(admission.reservation_id)


def test_a_pre_dispatch_failure_is_refundable_once_and_keeps_the_request_count():
    store = FakeStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3))
    assert backend.settle(admission, outcome='CONNECT_REFUSED',
                          evidence={'transmission_evidence': EVIDENCE_CONNECT_REFUSED}) is True
    assert backend.status()['accounting']['by_state'] == {LIFECYCLE_FAILED_CONFIRMED: 1}
    refund = backend.refund_failed_confirmed(admission.reservation_id,
                                             reason='connect refused before dispatch')
    assert refund['refunded_usd'] == '0.010000'
    status = backend.status()
    assert status['estimated_spend_usd'] == '0.000000'
    assert status['requests_committed'] == 1        # the counter is never decremented
    with pytest.raises(UsageLedgerError) as excinfo:
        backend.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE


def test_a_second_instance_can_settle_by_durable_identity_after_a_lost_reply():
    store = FakeStore()
    first = _backend(store, instance='instance-1')
    admission = first.reserve(_budget(request_cap=3), request_intent={'claim_id': 'spent'})
    second = _backend(store, instance='instance-2')
    settled = second.reconcile(request_digest=admission.request_digest, outcome='SUCCESS',
                               provider_requests=1)
    assert settled.applied is True and settled.transitioned is True
    assert settled.reservation_id == admission.reservation_id
    assert second.status()['accounting']['by_state'] == {LIFECYCLE_SETTLED: 1}
    assert first.reconcile(request_digest=admission.request_digest,
                           outcome='SUCCESS').duplicate is True
    assert first.status()['requests_committed'] == 1


def test_refund_of_an_unknown_reservation_is_a_typed_refusal():
    backend = _backend(FakeStore())
    backend.reserve(_budget())
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.refund_failed_confirmed('no-such-reservation')
    assert excinfo.value.code == LIVE_RESERVATION_UNKNOWN


# -- 7. the wiring points: build_transport and the live route ----------------------------------

def test_build_transport_accepts_an_injected_client_and_refuses_a_broken_one(monkeypatch):
    store = FakeStore()
    transport = firestore_module.build_transport(_config(), client=FakeFirestoreClient(store))
    assert isinstance(transport, FirestoreBoundedTransport)
    assert transport.config.collection == _COLLECTION

    def _broken(config):
        raise api_exceptions.PermissionDenied('no access to the project')

    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        firestore_module.build_transport(_config(), client_factory=_broken)
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE


def test_the_app_route_serves_a_live_request_through_the_real_adapter(monkeypatch):
    import httpx

    store = FakeStore()
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID', _DEPLOYMENT)
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '1')
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list[str] = []

    class _Run:
        provider = {'kind': 'STUB_NO_PROVIDER_REQUEST', 'provider_requests': 1,
                    'usage_as_returned': {'prompt_tokens': 10, 'completion_tokens': 5}}

        def as_json(self):
            return {'stub': True}

    class _Agent:
        async def run(self, claim_id):
            calls.append(claim_id)
            return _Run()

    monkeypatch.setattr(app_module, 'configured_live_agent', lambda court: _Agent())
    monkeypatch.setattr(firestore_module, 'build_transport',
                        lambda config, **kwargs: FirestoreBoundedTransport(
                            config, client=FakeFirestoreClient(store, project=config.project,
                                                               database=config.database)))

    async def _drive():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            first = await client.post('/api/investigate', json={'claim_id': 'spent'})
            second = await client.post('/api/investigate', json={'claim_id': 'spent'})
        return first, second

    import asyncio
    first, second = asyncio.run(_drive())
    assert first.status_code == 200 and first.json() == {'stub': True}
    assert calls == ['spent']
    assert second.status_code == 429
    assert second.json()['detail']['code'] == LIVE_REQUEST_BUDGET_EXHAUSTED
    document = _document(store)
    assert document['requests_committed'] == 1
    reservation = next(iter(document['reservations'].values()))
    assert reservation['state'] == 'settled' and reservation['outcome'] == 'SUCCESS'
    assert store.counters()['revision'] >= 2


def test_the_firestore_selector_still_refuses_a_broken_store_and_never_falls_back(
        tmp_path, monkeypatch):
    """Selecting firestore with an unusable store refuses typed; no SQLite file is created."""
    store = FakeStore()
    store.inject_read_failure(1, lambda: api_exceptions.ServiceUnavailable('down'))
    monkeypatch.setenv('CIVICTRACE_FIRESTORE_PROJECT', _PROJECT)
    monkeypatch.setenv('CIVICTRACE_SPEND_BACKEND', 'firestore')
    backend = firestore_module.FirestoreSpendBackend.from_env(transport=_transport(store))
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        backend.reserve(_budget(request_cap=1))
    assert excinfo.value.code == LIVE_SPEND_BACKEND_UNAVAILABLE
    assert sorted(tmp_path.glob('**/spend-*.sqlite3')) == []
    assert store.document_count() == 0


def test_the_config_refuses_before_the_transport_is_ever_constructed():
    for project in (None, '', 'Bad_Project'):
        with pytest.raises(UsageConfigError) as excinfo:
            FirestoreConfig.build(project=project)
        assert excinfo.value.code == LIVE_SPEND_BACKEND_NOT_CONFIGURED
