"""CT-QC-041 regression: a provider status code is never no-charge evidence.

The rule this module pins down:

    **After a request has been transmitted, HTTP 500/502/503/504 — any status at all — does not
    establish that the provider did not charge.** A 5xx can arrive after the provider has already
    executed billable processing. Such an outcome is ``uncertain``: the allowance stays consumed
    until reliable, provider-confirmed reconciliation arrives. ``failed_confirmed`` (and therefore
    a refund) after transmission requires an explicit no-charge record:
    ``PROVIDER_CONFIRMED_NO_CHARGE`` or ``PROVIDER_ZERO_USAGE_RECORD``, each with the supporting
    fields its class requires.

Hermetic: no provider, no credential, no network. The SQLite backend runs against a temporary
store, the Firestore-class backend against the deterministic stub transactional store, and the
route is driven through in-process ASGI with a stub agent.

Matrix:

* HTTP 500/502/503/504 after transmission      → ``uncertain``, allowance NOT refunded
* a status with no evidence class at all       → ``uncertain``
* timeout / drop after send                    → ``uncertain`` (explicitly re-tested here)
* provider-confirmed no-charge response        → ``failed_confirmed``, one explicit refund
* provider zero-usage settlement record        → ``failed_confirmed``, one explicit refund
* a no-charge *claim* without its support      → ``uncertain`` (no refund by assertion)
* pre-dispatch failures                        → ``failed_confirmed`` (unchanged)
* late / duplicate settlements                 → idempotent under the new classification
* reconciliation of ``uncertain`` with no-charge evidence → one refund, never two
* reconciliation of ``uncertain`` with usage evidence     → ``settled``, refund refused
* the same contract on the Firestore-class backend and through the HTTP route
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

import civictrace.app as app_module
from civictrace.nebius import ProviderUnavailable
from civictrace.spend_firestore import FirestoreConfig, FirestoreSpendBackend
from civictrace.spend_gate import SpendGate
from civictrace.spend_lifecycle import (
    CLASSIFICATION_TABLE,
    EVIDENCE_CONNECT_REFUSED,
    EVIDENCE_CONNECT_TIMEOUT,
    EVIDENCE_DROP_MID_CALL,
    EVIDENCE_LOCAL_PRE_DISPATCH,
    EVIDENCE_PROVIDER_NO_CHARGE,
    EVIDENCE_PROVIDER_RESPONSE,
    EVIDENCE_TIMEOUT_AFTER_SEND,
    EVIDENCE_USAGE_RECORDED,
    EVIDENCE_ZERO_USAGE,
    LIFECYCLE_FAILED_CONFIRMED,
    LIFECYCLE_SETTLED,
    LIFECYCLE_UNCERTAIN,
    NO_CHARGE_CLASSES,
    PRE_DISPATCH_CLASSES,
    REFUND_PERMITTING_CLASSES,
    classify,
)
from civictrace.spend_stub_store import MemoryDocumentStore, StubBoundedTransport
from civictrace.usage_ledger import (
    LIVE_RESERVATION_NOT_REFUNDABLE,
    LiveBudget,
    UsageLedgerError,
)

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

#: A provider status response, as the adapter actually reports it (nebius.py attaches the class
#: plus the status). ``http_status`` alone is also exercised: the store must not need the class.
STATUS_ONLY_5XX = (
    (500, {'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 500}),
    (502, {'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 502}),
    (503, {'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 503}),
    (504, {'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 504}),
    (503, {'http_status': 503}),
)

SUPPORTED_NO_CHARGE = {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
                       'provider_response_received': True,
                       'no_charge_confirmed': True}
SUPPORTED_ZERO_USAGE = {'transmission_evidence': EVIDENCE_ZERO_USAGE,
                        'usage_evidence_source': 'PROVIDER_SETTLEMENT_RECORD',
                        'usage_units': 0}


def _budget(request_cap: int = 10, dollar_cap: str = '100.00',
            estimate: str = '0.01') -> LiveBudget:
    return LiveBudget(request_cap=request_cap, dollar_cap=Decimal(dollar_cap),
                      per_claim_estimate_usd=Decimal(estimate))


def _app_estimate() -> str:
    from civictrace.nebius import REQUESTS_PER_CLAIM_CEILING
    budget = LiveBudget.from_env(requests_per_claim=REQUESTS_PER_CLAIM_CEILING)
    return str(budget.per_claim_estimate_usd.quantize(Decimal('0.000001')))


def _config() -> FirestoreConfig:
    return FirestoreConfig.build(project='civictrace-test')


def _backend(store: MemoryDocumentStore, *, instance: str = 'instance-1') -> FirestoreSpendBackend:
    return FirestoreSpendBackend(config=_config(), transport=StubBoundedTransport(store),
                                 deployment_id=_DEPLOYMENT, instance_id=instance,
                                 reservation_ttl_s=60.0, max_attempts=8, timeout_s=1.0,
                                 backoff_s=0.0)


def _live_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app, client=(
        '198.51.100.41', 51041)), base_url='http://test')


def _events(gate: SpendGate, reservation_id: str) -> list[str]:
    return [entry['event'] for entry in gate.history(reservation_id)]


def _refund_code(gate: SpendGate, reservation_id: str) -> str:
    with pytest.raises(UsageLedgerError) as excinfo:
        gate.refund_failed_confirmed(reservation_id)
    return excinfo.value.code


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    store = tmp_path / 'shared-store'
    store.mkdir()
    for name in _GOVERNANCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(store))
    app_module._peer_windows.clear()
    yield store


# -- 1. a status code after transmission is never failed_confirmed -----------------------------

@pytest.mark.parametrize('status, evidence', STATUS_ONLY_5XX)
def test_http_5xx_after_transmission_is_uncertain_and_the_allowance_stays_consumed(status,
                                                                                   evidence):
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    assert gate.mark_transmitted(admission, round_num=1) is True

    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE', evidence=evidence) is True

    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN, \
        f'HTTP {status} must not be read as proof of non-consumption'
    assert state['estimated_spend_usd'] == '0.010000', f'HTTP {status} refunded allowance'
    assert state['requests_committed'] == 1
    assert state['accounting']['failed_confirmed'] == 0
    assert state['accounting']['refunded'] == 0

    # The refund path refuses, and the refusal names the reason.
    assert _refund_code(gate, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'

    settle_rows = [entry for entry in gate.history(admission.reservation_id)
                   if entry['event'] == 'settle']
    assert len(settle_rows) == 1
    assert settle_rows[0]['evidence_class'] == EVIDENCE_PROVIDER_RESPONSE
    assert settle_rows[0]['allowance_refunded'] is False


@pytest.mark.parametrize('status, evidence', STATUS_ONLY_5XX)
def test_classification_of_a_status_code_is_uncertain_at_the_engine(status, evidence):
    result = classify('PROVIDER_UNAVAILABLE', evidence, dispatched=True)
    assert result.accounting_state == LIFECYCLE_UNCERTAIN
    assert result.refundable is False
    assert result.evidence_class == EVIDENCE_PROVIDER_RESPONSE


@pytest.mark.parametrize('evidence', [
    {'transmission_evidence': EVIDENCE_TIMEOUT_AFTER_SEND},
    {'transmission_evidence': EVIDENCE_DROP_MID_CALL},
    {'transmission_evidence': EVIDENCE_USAGE_RECORDED, 'http_status': 0},
])
def test_other_after_send_classes_are_never_refundable_on_their_own(evidence):
    result = classify('PROVIDER_UNAVAILABLE', evidence, dispatched=True)
    assert result.accounting_state in {LIFECYCLE_UNCERTAIN, LIFECYCLE_SETTLED}
    assert result.refundable is False


def test_ambiguous_timeout_after_send_is_uncertain_and_not_refundable():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                       evidence={'transmission_evidence': EVIDENCE_TIMEOUT_AFTER_SEND,
                                 'transmission_dispatched': True}) is True
    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['estimated_spend_usd'] == '0.010000'
    assert _refund_code(gate, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE


# -- 2. the two evidence classes that do permit failed_confirmed after transmission ------------

def test_provider_confirmed_no_charge_permits_failed_confirmed_and_exactly_one_refund():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)

    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                       evidence=SUPPORTED_NO_CHARGE) is True
    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == \
        LIFECYCLE_FAILED_CONFIRMED
    assert state['estimated_spend_usd'] == '0.010000', 'settlement never refunds by itself'
    assert state['accounting']['failed_confirmed'] == 1

    refund = gate.refund_failed_confirmed(admission.reservation_id,
                                          reason='provider stated not processed')
    assert refund['refunded_usd'] == '0.010000'
    state = gate.read_state()
    assert state['estimated_spend_usd'] == '0.000000'
    assert state['requests_committed'] == 1, 'the request counter is never decremented'
    assert _refund_code(gate, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE
    assert _events(gate, admission.reservation_id).count('refund') == 1


def test_zero_usage_settlement_record_permits_failed_confirmed_and_one_refund():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                       evidence=SUPPORTED_ZERO_USAGE) is True
    state = gate.read_state()
    assert state['reservations']['last_reservation']['accounting_state'] == \
        LIFECYCLE_FAILED_CONFIRMED
    assert gate.refund_failed_confirmed(admission.reservation_id)['refunded_usd'] == '0.010000'
    assert gate.read_state()['estimated_spend_usd'] == '0.000000'


#: A no-charge *claim* without the provider fields its class requires is not evidence.
UNSUPPORTED_NO_CHARGE_CLAIMS = (
    ('bare class', {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE}),
    ('no charge not confirmed',
     {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
      'provider_response_received': True}),
    ('confirmation is not True',
     {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE,
      'provider_response_received': True, 'no_charge_confirmed': 1}),
    ('no provider response',
     {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE, 'no_charge_confirmed': True}),
    ('zero usage, one unit',
     {'transmission_evidence': EVIDENCE_ZERO_USAGE,
      'usage_evidence_source': 'PROVIDER_SETTLEMENT_RECORD', 'usage_units': 1}),
    ('zero usage claimed as False',
     {'transmission_evidence': EVIDENCE_ZERO_USAGE,
      'usage_evidence_source': 'PROVIDER_SETTLEMENT_RECORD', 'usage_units': False}),
    ('zero usage, wrong source',
     {'transmission_evidence': EVIDENCE_ZERO_USAGE,
      'usage_evidence_source': 'CLIENT_GUESS', 'usage_units': 0}),
)


@pytest.mark.parametrize('label, evidence', UNSUPPORTED_NO_CHARGE_CLAIMS)
def test_an_unsupported_no_charge_claim_is_uncertain_and_cannot_be_refunded(label, evidence):
    result = classify('PROVIDER_UNAVAILABLE', evidence, dispatched=True)
    assert result.accounting_state == LIFECYCLE_UNCERTAIN, label
    assert result.refundable is False, label

    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE', evidence=evidence) is True
    assert gate.read_state()['reservations']['last_reservation']['accounting_state'] == \
        LIFECYCLE_UNCERTAIN, label
    assert gate.read_state()['estimated_spend_usd'] == '0.010000', label
    assert _refund_code(gate, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE, label


def test_pre_dispatch_failures_remain_failed_confirmed():
    for evidence_class in (EVIDENCE_CONNECT_REFUSED, EVIDENCE_CONNECT_TIMEOUT,
                           EVIDENCE_LOCAL_PRE_DISPATCH):
        result = classify('PROVIDER_UNAVAILABLE', {'transmission_evidence': evidence_class},
                          dispatched=False)
        assert result.accounting_state == LIFECYCLE_FAILED_CONFIRMED, evidence_class
        assert result.refundable is True, evidence_class

        gate = SpendGate()
        admission = gate.reserve(_budget(request_cap=3))
        # No dispatch: the request never reached the transport.
        assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                           evidence={'transmission_evidence': evidence_class}) is True
        assert gate.read_state()['reservations']['last_reservation']['accounting_state'] == \
            LIFECYCLE_FAILED_CONFIRMED, evidence_class
        assert gate.refund_failed_confirmed(admission.reservation_id)['refunded_usd'] == \
            '0.010000', evidence_class


def test_the_refund_permitting_set_is_exactly_pre_dispatch_plus_supported_no_charge():
    assert REFUND_PERMITTING_CLASSES == PRE_DISPATCH_CLASSES | NO_CHARGE_CLASSES
    assert EVIDENCE_PROVIDER_RESPONSE not in REFUND_PERMITTING_CLASSES
    documented = {row[0]: row[1] for row in CLASSIFICATION_TABLE}
    assert documented[EVIDENCE_PROVIDER_RESPONSE] == LIFECYCLE_UNCERTAIN
    for evidence_class in NO_CHARGE_CLASSES:
        assert documented[evidence_class] == LIFECYCLE_FAILED_CONFIRMED


# -- 3. reconciliation: uncertain resolves exactly once under reliable evidence ---------------

def test_reconciling_uncertain_with_no_charge_evidence_refunds_exactly_once():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3), request_intent={'claim_id': 'spent',
                                                                    'attempt': 1})
    gate.mark_transmitted(admission, round_num=1)
    gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 500})
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'

    # A replacement process holds only the durable request identity.
    replacement = SpendGate(instance_id='rev-2')
    resolved = replacement.reconcile(request_digest=admission.request_digest,
                                     outcome='PROVIDER_UNAVAILABLE',
                                     evidence=SUPPORTED_NO_CHARGE)
    assert resolved.applied is True
    assert resolved.transitioned is False, 'the lease was already dispositioned'
    assert resolved.accounting_state == LIFECYCLE_FAILED_CONFIRMED
    assert resolved.reservation_id == admission.reservation_id
    assert replacement.read_state()['estimated_spend_usd'] == '0.010000', \
        'reconciliation classifies, it never refunds'

    # Exactly once: the same reconciliation again applies nothing.
    again = replacement.reconcile(request_digest=admission.request_digest,
                                  outcome='PROVIDER_UNAVAILABLE',
                                  evidence=SUPPORTED_NO_CHARGE)
    assert again.applied is False and again.duplicate is True
    events = _events(replacement, admission.reservation_id)
    assert events.count('reconcile_accounting') == 1

    refund = replacement.refund_failed_confirmed(admission.reservation_id,
                                                 reason='provider confirmed no execution')
    assert refund['refunded_usd'] == '0.010000'
    final = replacement.read_state()
    assert final['estimated_spend_usd'] == '0.000000'
    assert final['requests_committed'] == 1
    assert final['accounting']['refunded'] == 1
    assert _refund_code(replacement, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE
    assert replacement.read_state()['estimated_spend_usd'] == '0.000000', \
        'a second refund would create allowance'
    assert _events(replacement, admission.reservation_id) == [
        'reserve', 'transmission_started', 'settle', 'reconcile_accounting', 'refund']


def test_reconciling_uncertain_with_usage_evidence_settles_consumed_and_refuses_the_refund():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 503})
    assert gate.read_state()['reservations']['last_reservation']['accounting_state'] == \
        LIFECYCLE_UNCERTAIN

    settled = gate.reconcile(admission.reservation_id, outcome='PROVIDER_UNAVAILABLE',
                             evidence={'transmission_evidence': EVIDENCE_USAGE_RECORDED,
                                       'provider_response_received': True,
                                       'usage_as_returned': {'prompt_tokens': 12,
                                                             'completion_tokens': 3}})
    assert settled.applied is True and settled.transitioned is False
    assert settled.accounting_state == LIFECYCLE_SETTLED
    state = gate.read_state()
    assert state['estimated_spend_usd'] == '0.010000', 'usage means consumed, never refunded'
    assert state['reservations']['last_reservation']['state'] == 'settled'
    assert _refund_code(gate, admission.reservation_id) == LIVE_RESERVATION_NOT_REFUNDABLE

    repeat = gate.reconcile(admission.reservation_id, outcome='PROVIDER_UNAVAILABLE',
                            evidence={'transmission_evidence': EVIDENCE_USAGE_RECORDED})
    assert repeat.applied is False and repeat.duplicate is True
    assert _events(gate, admission.reservation_id).count('reconcile_accounting') == 1


def test_reconciliation_without_decisive_evidence_writes_nothing():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    gate.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 500})
    before = _events(gate, admission.reservation_id)
    receipts_before = [row['kind'] for row in gate.read_state()['receipts']]

    # Another status is not new evidence; neither is a bare no-charge claim.
    for evidence in ({'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 502},
                     {'transmission_evidence': EVIDENCE_PROVIDER_NO_CHARGE}):
        result = gate.reconcile(admission.reservation_id, outcome='PROVIDER_UNAVAILABLE',
                                evidence=evidence)
        assert result.applied is False
        assert result.accounting_state == LIFECYCLE_UNCERTAIN

    state = gate.read_state()
    assert _events(gate, admission.reservation_id) == before
    assert [row['kind'] for row in state['receipts']] == receipts_before
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['estimated_spend_usd'] == '0.010000'


def test_late_and_duplicate_status_settlements_are_idempotent_under_the_new_classification():
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=3))
    gate.mark_transmitted(admission, round_num=1)
    evidence = {'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE, 'http_status': 503}
    assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE', evidence=evidence) is True

    before_events = _events(gate, admission.reservation_id)
    before_receipts = [row['kind'] for row in gate.read_state()['receipts']]
    for _ in range(3):
        assert gate.settle(admission, outcome='PROVIDER_UNAVAILABLE', evidence=evidence) is False
    duplicate = gate.reconcile(admission.reservation_id, outcome='PROVIDER_UNAVAILABLE',
                               evidence=evidence)
    assert duplicate.applied is False and duplicate.duplicate is True
    state = gate.read_state()
    assert _events(gate, admission.reservation_id) == before_events
    assert [row['kind'] for row in state['receipts']] == before_receipts
    assert state['requests_committed'] == 1
    assert state['estimated_spend_usd'] == '0.010000'

    # A late *success* for the same identity still settles once, from uncertain to consumed.
    late = gate.reconcile(admission.reservation_id, outcome='SUCCESS', provider_requests=1)
    assert late.applied is True and late.transitioned is False
    assert late.accounting_state == LIFECYCLE_SETTLED
    assert gate.reconcile(admission.reservation_id, outcome='SUCCESS').duplicate is True
    assert gate.read_state()['estimated_spend_usd'] == '0.010000'


# -- 4. the same contract on the Firestore-class backend --------------------------------------

def test_firestore_backend_treats_status_codes_as_uncertain_and_refuses_the_refund():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3))
    backend.mark_transmitted(admission, round_num=1)
    assert backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                          evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE,
                                    'http_status': 500}) is True
    status = backend.status()
    assert status['reservations']['uncertain'] == 1
    assert status['reservations']['failed_confirmed'] == 0
    assert status['estimated_spend_usd'] == '0.010000'
    document = store.snapshot(backend.document_id)
    assert document['reservations'][admission.reservation_id]['accounting_state'] == \
        LIFECYCLE_UNCERTAIN
    with pytest.raises(UsageLedgerError) as excinfo:
        backend.refund_failed_confirmed(admission.reservation_id)
    assert excinfo.value.code == LIVE_RESERVATION_NOT_REFUNDABLE
    assert backend.status()['estimated_spend_usd'] == '0.010000'


def test_firestore_backend_reconciles_uncertain_with_no_charge_evidence_once():
    store = MemoryDocumentStore()
    backend = _backend(store)
    admission = backend.reserve(_budget(request_cap=3),
                                request_intent={'claim_id': 'spent', 'attempt': 2})
    backend.mark_transmitted(admission, round_num=1)
    backend.settle(admission, outcome='PROVIDER_UNAVAILABLE',
                   evidence={'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE,
                             'http_status': 504})

    replacement = _backend(store, instance='instance-2')
    resolved = replacement.reconcile(request_digest=admission.request_digest,
                                     outcome='PROVIDER_UNAVAILABLE',
                                     evidence=SUPPORTED_ZERO_USAGE)
    assert resolved.applied is True and resolved.transitioned is False
    assert resolved.accounting_state == LIFECYCLE_FAILED_CONFIRMED
    assert replacement.reconcile(request_digest=admission.request_digest,
                                 outcome='PROVIDER_UNAVAILABLE',
                                 evidence=SUPPORTED_ZERO_USAGE).duplicate is True

    refund = replacement.refund_failed_confirmed(admission.reservation_id)
    assert refund['refunded_usd'] == '0.010000'
    assert replacement.status()['estimated_spend_usd'] == '0.000000'
    assert replacement.status()['requests_committed'] == 1
    with pytest.raises(UsageLedgerError):
        replacement.refund_failed_confirmed(admission.reservation_id)
    events = [entry['event'] for entry in
              store.snapshot(backend.document_id)['accounting_history']]
    assert events == ['reserve', 'transmission_started', 'settle', 'reconcile_accounting',
                      'refund']


# -- 5. the live route ------------------------------------------------------------------------

class _StatusFailureStub:
    """Stub agent that behaves like ``NebiusAgent`` on a 5xx: dispatch, then status telemetry."""

    supports_dispatch_hook = True

    def __init__(self, calls: list, status: int):
        self.calls = calls
        self.status = status

    async def run(self, claim_id, *, on_dispatch=None):
        self.calls.append(claim_id)
        if on_dispatch is not None:
            await on_dispatch(1)
        raise ProviderUnavailable('PROVIDER_HTTP_ERROR', telemetry={
            'transmission_evidence': EVIDENCE_PROVIDER_RESPONSE,
            'transmission_dispatched': True,
            'provider_response_received': True,
            'http_status': self.status,
            'usage_as_returned': {'prompt_tokens': None, 'completion_tokens': None},
            'usage_complete': False,
        })


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [500, 502, 503, 504])
async def test_app_route_keeps_a_5xx_allowance_consumed_and_refuses_the_refund(monkeypatch,
                                                                               status):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list = []
    monkeypatch.setattr(app_module, 'configured_live_agent',
                        lambda court: _StatusFailureStub(calls, status))
    async with _live_client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 502
    assert response.json()['detail']['code'] == 'PROVIDER_HTTP_ERROR'
    assert calls == ['spent']

    gate = SpendGate()
    state = gate.read_state()
    assert state['requests_committed'] == 1
    assert state['estimated_spend_usd'] == _app_estimate(), 'the allowance stays consumed'
    assert state['reservations']['last_reservation']['accounting_state'] == LIFECYCLE_UNCERTAIN
    assert state['accounting']['refunded'] == 0
    assert _refund_code(gate, state['reservations']['last_reservation']['reservation_id']) == \
        LIVE_RESERVATION_NOT_REFUNDABLE
    assert SpendGate().read_state()['estimated_spend_usd'] == _app_estimate()
