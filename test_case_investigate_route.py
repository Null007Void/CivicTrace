"""CT-QC-064: the selected-case investigate route, exercised through the actual product path.

Every "provider" interaction here is a synthetic ``httpx.MockTransport``: no live Nebius/NVIDIA
request is made, no credential is read, nothing is charged. The case is built through the public
JSON API and, in the UI tests, through the normal workbench pages, from the reserved-``.invalid``
synthetic fixture, so the Court under test is what a real second case produces.

The matrix this file proves, in order:

* a selected case + immutable revision + human-confirmed claim is the only accepted selector;
* a stale revision, a claim from another case, an unconfirmed claim, and a case missing one of
  the two reviewed source roles each refuse **before** any reservation or agent construction;
* with live activation off the route refuses with its own typed code, still before any
  reservation;
* when the configured accounting backend is unusable the route refuses before any provider
  request (the mock transport records zero);
* the happy path drives the same ``NebiusAgent`` the live path uses, settles through the real
  durable SQLite ledger, and its request bodies contain nothing about Exeter;
* the Court's existing refusal semantics (unseen citation, unreviewed mapping) survive end to
  end, and the spend is never refunded when the Court refuses the model's proposal;
* the workbench page names the case, revision and claim, shows the eligibility state, and
  renders live activation as visibly disabled with a disabled submit control.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from civictrace import app as app_module
from civictrace.app import app, reset_case_store_cache
from civictrace.nebius import (
    LIVE_ELIGIBILITY_REFUSAL,
    MAX_TOOL_ROUNDS,
    REQUESTS_PER_CLAIM_CEILING,
    NebiusAgent,
)
from civictrace.usage_ledger import UsageLedgerError

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'synthetic_case.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
CASE_FIELDS = ('project_name', 'municipality', 'case_title', 'jurisdiction_id',
               'publisher', 'allowed_hosts', 'provenance_note')
TEST_KEY = 'example-test-key'
USAGE = {'prompt_tokens': 11, 'completion_tokens': 3}

LIVE_GATE_VARS = ('CIVICTRACE_LIVE_ENABLED', 'NEBIUS_API_KEY', 'CIVICTRACE_LIVE_CHECK_ACK')
LEDGER_VARS = ('CIVICTRACE_SPEND_BACKEND', 'CIVICTRACE_DEPLOYMENT_ID', 'CIVICTRACE_MAX_LIVE_USD',
               'CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', 'CIVICTRACE_USAGE_LOCK_TIMEOUT_S',
               'CIVICTRACE_SPEND_RESERVATION_TTL_S', 'CIVICTRACE_INSTANCE_ID',
               'CIVICTRACE_SPEND_STORE_EXPECT_ROOT', 'CIVICTRACE_USAGE_LEDGER_DIR',
               'CIVICTRACE_SPEND_BACKEND_TIMEOUT_S', 'CIVICTRACE_SPEND_BACKEND_ATTEMPTS')

#: A workbench form post is a browser write: it declares the origin it was served from and the
#: same-origin fetch metadata the app's write guard requires (httpx adds neither on its own).
FORM_HEADERS = {'content-type': 'application/x-www-form-urlencoded',
                'Origin': 'http://test', 'Sec-Fetch-Site': 'same-origin'}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Private case store, private existing spend-store directory, every live gate closed."""
    monkeypatch.setenv('CIVICTRACE_CASE_STORE', str(tmp_path / 'cases.json'))
    shared = tmp_path / 'shared-store'
    shared.mkdir()
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(shared))
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '50')
    for name in LIVE_GATE_VARS + LEDGER_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_case_store_cache()
    app_module._peer_windows.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test', follow_redirects=False) as async_client:
        yield async_client
    reset_case_store_cache()
    app_module._peer_windows.clear()


# -- seeding, through the public JSON API the workbench itself uses ----------------------------

async def create_case(client: httpx.AsyncClient, **overrides) -> dict:
    payload = {key: FIXTURE['case'][key] for key in CASE_FIELDS}
    payload.update(overrides)
    response = await client.post('/api/cases', json=payload)
    assert response.status_code == 201, response.text
    return response.json()['case']


async def add_source(client: httpx.AsyncClient, case_id: str, *, title: str, role: str,
                     as_of: str, anchor: str, text: str,
                     url: str = 'https://records.example.invalid/synthetic.txt') -> dict:
    response = await client.post(f'/api/cases/{case_id}/sources', json={
        'title': title, 'publisher': FIXTURE['case']['publisher'], 'official_url': url,
        'document_role': role, 'document_date': as_of, 'as_of': as_of,
        'anchor': anchor, 'text': text})
    assert response.status_code == 201, response.text
    return response.json()['source']


async def add_fact(client: httpx.AsyncClient, case_id: str, *, source_id: str, anchor: str,
                   exact_excerpt: str, predicate: str, subject_id: str, action_id: str,
                   qualifiers: list[str] | None = None, **extra) -> dict:
    payload = {'source_id': source_id, 'anchor': anchor, 'exact_excerpt': exact_excerpt,
               'predicate': predicate, 'subject_id': subject_id, 'action_id': action_id,
               'qualifiers': qualifiers or []}
    payload.update(extra)
    response = await client.post(f'/api/cases/{case_id}/facts', json=payload)
    assert response.status_code == 201, response.text
    return response.json()['fact']


async def add_claim(client: httpx.AsyncClient, case_id: str, *, text: str, predicate: str,
                    subject_id: str, action_id: str, fact_ids: list[str],
                    additional_record: str, **extra) -> dict:
    payload = {'text': text, 'predicate': predicate, 'subject_id': subject_id,
               'action_id': action_id, 'fact_ids': fact_ids,
               'additional_record': additional_record}
    payload.update(extra)
    response = await client.post(f'/api/cases/{case_id}/claims', json=payload)
    assert response.status_code == 201, response.text
    return response.json()['claim']


async def review_fact(client: httpx.AsyncClient, case_id: str, fact_id: str) -> None:
    response = await client.patch(f'/api/cases/{case_id}/facts/{fact_id}',
                                  json={'review_state': 'HUMAN_REVIEWED'})
    assert response.status_code == 200, response.text


async def confirm_claim(client: httpx.AsyncClient, case_id: str, claim_id: str) -> None:
    response = await client.post(f'/api/cases/{case_id}/claims/{claim_id}/confirm',
                                 json={'confirmed': True, 'reviewer': 'synthetic-reviewer'})
    assert response.status_code == 200, response.text


async def case_revision(client: httpx.AsyncClient, case_id: str) -> int:
    return (await client.get(f'/api/cases/{case_id}')).json()['case']['revision']


@dataclass
class Seeded:
    case_id: str
    revision: int
    sources: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    claims: dict = field(default_factory=dict)


async def seed_case(client: httpx.AsyncClient, *, with_tracker: bool = True,
                    review_tracker: bool = True, confirm: bool = True,
                    extra: str | None = None) -> Seeded:
    """One synthetic case with a reviewed two-role set and a confirmed ceiling claim.

    ``extra`` adds a second adopted-action-minutes source carrying one more mapping:
    ``'reviewed'`` leaves that mapping HUMAN_REVIEWED (unseen-citation probe) and
    ``'pending'`` leaves it PENDING_HUMAN_REVIEW (unreviewed-mapping probe).
    """
    case = await create_case(client)
    case_id = case['case_id']
    minutes = await add_source(
        client, case_id, title=FIXTURE['sources'][0]['title'],
        role='ADOPTED_ACTION_MINUTES', as_of='2026-03-02', anchor='Item A.1',
        text=FIXTURE['sources'][0]['text'])
    sources = {'minutes': minutes['source_id']}
    if with_tracker:
        tracker = await add_source(
            client, case_id, title=FIXTURE['sources'][1]['title'],
            role='CITY_INFORMATIONAL_TRACKER', as_of='2026-03-20', anchor='Row 2',
            url='https://records.example.invalid/synthetic-tracker.txt',
            text=FIXTURE['sources'][1]['text'])
        sources['tracker'] = tracker['source_id']

    facts = {}
    for fact in FIXTURE['facts']:
        if fact['source'] == 'tracker' and not with_tracker:
            continue
        record = await add_fact(
            client, case_id, source_id=sources[fact['source']], anchor=fact['anchor'],
            exact_excerpt=fact['exact_excerpt'], predicate=fact['predicate'],
            subject_id=fact['subject_id'], action_id=fact['action_id'],
            qualifiers=fact.get('qualifiers'),
            **{key: fact[key] for key in ('amount_cents', 'phase') if key in fact})
        facts[fact['key']] = record['fact_id']

    claim = await add_claim(
        client, case_id, text=FIXTURE['claims'][0]['text'],
        predicate=FIXTURE['claims'][0]['predicate'],
        subject_id=FIXTURE['claims'][0]['subject_id'],
        action_id=FIXTURE['claims'][0]['action_id'],
        fact_ids=[facts['cap']], additional_record=FIXTURE['claims'][0]['additional_record'],
        amount_cents=FIXTURE['claims'][0]['amount_cents'],
        ceiling=True)

    for key, fact_id in facts.items():
        if key == 'phase' and not review_tracker:
            continue
        await review_fact(client, case_id, fact_id)
    if confirm:
        await confirm_claim(client, case_id, claim['claim_id'])

    if extra is not None:
        second = await add_source(
            client, case_id, title='SYNTHETIC second minutes (fixture)',
            role='ADOPTED_ACTION_MINUTES', as_of='2026-03-05', anchor='Vote 1',
            url='https://records.example.invalid/synthetic-minutes-2.txt',
            text=('SYNTHETIC FIXTURE - NOT A REAL GOVERNMENT RECORD.\n'
                  'Vote 3-0 to approve the synthetic sidewalk pilot.\n'))
        sources['minutes2'] = second['source_id']
        record = await add_fact(
            client, case_id, source_id=second['source_id'], anchor='Vote 1',
            exact_excerpt='Vote 3-0 to approve the synthetic sidewalk pilot',
            predicate='VOTE_APPROVED', subject_id='synthetic-sidewalk', action_id='V-1',
            qualifiers=['SYNTHETIC_FIXTURE'], yea=3, nay=0, abstain=0)
        facts['vote'] = record['fact_id']
        if extra == 'reviewed':
            await review_fact(client, case_id, record['fact_id'])

    return Seeded(case_id=case_id, revision=await case_revision(client, case_id),
                  sources=sources, facts=facts, claims={'authorization': claim['claim_id']})


# -- synthetic provider transport, the same shape the adapter tests use ------------------------

def tool_calls_response(response_id: str, calls, usage=None) -> dict:
    return {'id': response_id,
            'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'{response_id}-call-{index}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}
                for index, (name, args) in enumerate(calls)]},
                'finish_reason': 'tool_calls'}],
            'usage': dict(USAGE if usage is None else usage)}


def answer_response(response_id: str, payload, usage=None) -> dict:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {'id': response_id,
            'choices': [{'message': {'role': 'assistant', 'content': content},
                         'finish_reason': 'stop'}],
            'usage': dict(USAGE if usage is None else usage)}


class ProviderSpy:
    """A mock-transport agent factory: counts constructions and every serialized request."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.log: list[dict] = []
        self.constructed = 0
        self._clients: list[httpx.AsyncClient] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'api.tokenfactory.us-central1.nebius.com'
        assert request.url.path.endswith('/v1/chat/completions')
        body = json.loads(request.content)
        self.log.append(body)
        assert len(self.log) <= len(self.script), 'more requests than the run protocol allows'
        return httpx.Response(200, json=self.script[len(self.log) - 1])

    def factory(self, court) -> NebiusAgent:
        self.constructed += 1
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self._clients.append(client)
        return NebiusAgent(court, key=TEST_KEY, client=client)

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._clients.clear()


def install_spy(monkeypatch, script: list[dict]) -> ProviderSpy:
    spy = ProviderSpy(script)
    monkeypatch.setattr(app_module, 'configured_live_agent', spy.factory)
    return spy


@dataclass
class LedgerProbe:
    opened: list = field(default_factory=list)

    def never_opened(self) -> bool:
        return not self.opened


def forbid_ledger(monkeypatch) -> LedgerProbe:
    """Fail the request loudly if any gate leaks into the accounting backend."""
    probe = LedgerProbe()

    def refuse(*_args, **_kwargs):
        probe.opened.append(1)
        raise AssertionError('the durable accounting backend was opened before every gate passed')

    monkeypatch.setattr(app_module, 'open_spend_backend', refuse)
    return probe


def authorization_answer(claim_id: str, fact_id: str) -> dict:
    return {'claim_id': claim_id, 'proposed_predicate': 'AUTHORIZED_USE_CEILING',
            'fact_ids': [fact_id],
            'observation': 'The synthetic minutes authorize a ceiling; nothing here is a payment.'}


async def post_case_investigation(client: httpx.AsyncClient, case_id: str, *, claim_id: str,
                                  revision: int):
    return await client.post(f'/api/cases/{case_id}/investigate',
                             json={'claim_id': claim_id, 'revision': revision})


# -- 1. gates that refuse before any reservation or agent construction -------------------------

@pytest.mark.asyncio
async def test_stale_revision_is_refused_before_any_reservation(client, monkeypatch):
    seeded = await seed_case(client)
    ledger = forbid_ledger(monkeypatch)
    spy = install_spy(monkeypatch, [])

    response = await post_case_investigation(
        client, seeded.case_id, claim_id=seeded.claims['authorization'],
        revision=seeded.revision - 1)

    assert response.status_code == 409
    detail = response.json()['detail']
    assert detail['code'] == 'STALE_CASE_REVISION'
    assert detail['requested_revision'] == seeded.revision - 1
    assert detail['current_revision'] == seeded.revision
    assert ledger.never_opened()
    assert spy.constructed == 0 and spy.log == []


@pytest.mark.asyncio
async def test_claim_from_another_case_is_refused_in_both_directions(client, monkeypatch):
    first = await seed_case(client)
    second = await seed_case(client)
    ledger = forbid_ledger(monkeypatch)
    spy = install_spy(monkeypatch, [])

    crossed = await post_case_investigation(
        client, first.case_id, claim_id=second.claims['authorization'],
        revision=first.revision)
    assert crossed.status_code == 404
    assert crossed.json()['detail']['code'] == 'CLAIM_NOT_IN_CASE'

    reverse = await post_case_investigation(
        client, second.case_id, claim_id=first.claims['authorization'],
        revision=second.revision)
    assert reverse.status_code == 404
    assert reverse.json()['detail']['code'] == 'CLAIM_NOT_IN_CASE'

    assert ledger.never_opened()
    assert spy.constructed == 0 and spy.log == []


@pytest.mark.asyncio
async def test_unconfirmed_claim_is_refused(client, monkeypatch):
    seeded = await seed_case(client, confirm=False)
    ledger = forbid_ledger(monkeypatch)
    spy = install_spy(monkeypatch, [])

    response = await post_case_investigation(
        client, seeded.case_id, claim_id=seeded.claims['authorization'],
        revision=seeded.revision)

    assert response.status_code == 409
    detail = response.json()['detail']
    assert detail['code'] == 'CLAIM_NOT_CONFIRMED'
    assert detail['confirmation_state'] == 'UNCONFIRMED'
    assert ledger.never_opened()
    assert spy.constructed == 0 and spy.log == []


@pytest.mark.asyncio
async def test_two_distinct_reviewed_source_roles_are_required(client, monkeypatch):
    ledger = forbid_ledger(monkeypatch)
    spy = install_spy(monkeypatch, [])

    missing_role = await seed_case(client, with_tracker=False)
    refused = await post_case_investigation(
        client, missing_role.case_id, claim_id=missing_role.claims['authorization'],
        revision=missing_role.revision)
    assert refused.status_code == 409
    detail = refused.json()['detail']
    assert detail['code'] == LIVE_ELIGIBILITY_REFUSAL
    roles = detail['live_eligibility']['roles']
    assert roles['ADOPTED_ACTION_MINUTES']['tool_derived'] is True
    assert roles['CITY_INFORMATIONAL_TRACKER']['tool_derived'] is False
    assert (roles['CITY_INFORMATIONAL_TRACKER']['not_derived_reason']
            == 'ROLE_NOT_REGISTERED_IN_SELECTED_CASE')
    assert detail['live_eligibility']['eligible'] is False

    unreviewed_role = await seed_case(client, review_tracker=False)
    second = await post_case_investigation(
        client, unreviewed_role.case_id, claim_id=unreviewed_role.claims['authorization'],
        revision=unreviewed_role.revision)
    assert second.status_code == 409
    detail = second.json()['detail']
    assert detail['code'] == LIVE_ELIGIBILITY_REFUSAL
    roles = detail['live_eligibility']['roles']
    assert roles['ADOPTED_ACTION_MINUTES']['reviewed_fact_count'] == 2
    assert roles['CITY_INFORMATIONAL_TRACKER']['reviewed_fact_count'] == 0

    assert ledger.never_opened()
    assert spy.constructed == 0 and spy.log == []


@pytest.mark.asyncio
async def test_live_disabled_refuses_before_any_reservation(client, monkeypatch):
    seeded = await seed_case(client)
    ledger = forbid_ledger(monkeypatch)
    calls: list[int] = []
    constructions: list[int] = []
    real_factory = app_module.configured_live_agent

    def counting_factory(court):
        # Calls the *real* factory, so the owner gate is still the one that refuses. A call that
        # raises at the owner gate leaves ``constructions`` empty: no agent, no client, no request.
        calls.append(1)
        agent = real_factory(court)
        constructions.append(1)
        return agent

    monkeypatch.setattr(app_module, 'configured_live_agent', counting_factory)

    response = await post_case_investigation(
        client, seeded.case_id, claim_id=seeded.claims['authorization'],
        revision=seeded.revision)

    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'LIVE_INFERENCE_NOT_OWNER_ENABLED'
    assert calls == [1] and constructions == []
    assert ledger.never_opened()


@pytest.mark.asyncio
async def test_unusable_accounting_backend_refuses_with_zero_provider_requests(client, monkeypatch):
    seeded = await seed_case(client)
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    spy = install_spy(monkeypatch, [])
    opened: list[int] = []

    def unusable(*_args, **_kwargs):
        opened.append(1)
        raise UsageLedgerError('LIVE_SPEND_STORE_NOT_CONFIGURED',
                               'configured accounting backend is not usable in this test')

    monkeypatch.setattr(app_module, 'open_spend_backend', unusable)

    response = await post_case_investigation(
        client, seeded.case_id, claim_id=seeded.claims['authorization'],
        revision=seeded.revision)

    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'LIVE_SPEND_STORE_NOT_CONFIGURED'
    assert opened == [1]           # the admission was attempted ...
    assert spy.constructed == 1    # ... the agent existed, but ...
    assert spy.log == []           # ... not one provider request was serialized or sent
    await spy.aclose()


@pytest.mark.asyncio
async def test_request_body_is_bounded_to_the_selected_case_selector(client):
    seeded = await seed_case(client)
    response = await client.post(f'/api/cases/{seeded.case_id}/investigate',
                                 json={'claim_id': seeded.claims['authorization'],
                                       'revision': str(seeded.revision)})
    assert response.status_code == 422
    assert 'revision' in json.dumps(response.json())
    injected = await client.post(f'/api/cases/{seeded.case_id}/investigate',
                                 json={'claim_id': seeded.claims['authorization'],
                                       'revision': seeded.revision,
                                       'predicate': 'PHYSICAL_COMPLETED'})
    assert injected.status_code == 422
    missing = await client.post(f'/api/cases/{seeded.case_id}/investigate',
                                json={'claim_id': seeded.claims['authorization']})
    assert missing.status_code == 422


# -- 2. the happy path: same agent, same durable accounting, nothing Exeter --------------------

@pytest.mark.asyncio
async def test_selected_case_happy_path_drives_the_product_route_and_settles_durably(
        client, monkeypatch):
    seeded = await seed_case(client)
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    script = [
        tool_calls_response('sc-1', [('read_council_item', {'source_id': seeded.sources['minutes']})]),
        tool_calls_response('sc-2', [('read_project_tracker', {'source_id': seeded.sources['tracker']})]),
        answer_response('sc-3', authorization_answer(seeded.claims['authorization'],
                                                    seeded.facts['cap'])),
    ]
    spy = install_spy(monkeypatch, script)
    try:
        response = await post_case_investigation(
            client, seeded.case_id, claim_id=seeded.claims['authorization'],
            revision=seeded.revision)
    finally:
        await spy.aclose()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['court_result']['status'] == 'DOCUMENTED_ACTION'
    assert payload['court_result']['reason_codes'] == ['MATCHES_REGISTERED_FACT_CONTRACT']
    assert [item['fact_id'] for item in payload['court_result']['source_citations']] == [
        seeded.facts['cap']]
    assert payload['court_result']['source_integrity_state'] == (
        'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED')
    assert payload['court_result']['original_source_verified'] is False

    provenance = payload['case_investigation']
    assert provenance['case_id'] == seeded.case_id
    assert provenance['revision'] == seeded.revision
    assert provenance['claim_id'] == seeded.claims['authorization']
    assert provenance['claim_confirmation']['state'] == 'CONFIRMED'
    assert provenance['claim_confirmation']['reviewer_recorded'] is True
    assert provenance['claim_confirmation']['evidence_sha256']
    assert provenance['live_eligibility']['eligible'] is True
    assert provenance['case_isolation'] == 'COURT_BOUND_TO_THE_SELECTED_CASE_ONLY'
    assert provenance['provenance']['original_source_verified'] is False
    assert provenance['provenance']['independent_fact_audit'] == 'PENDING'
    assert provenance['provenance']['hash_subject'] == 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'
    assert provenance['reservation_order'] == list(app_module.CASE_RUN_GATE_ORDER)

    provider = payload['provider']
    assert provider['case_id'] == seeded.case_id
    assert provider['case_registry_sha256'] == provenance['case_registry_sha256']
    assert provider['offered_tools'] == ['read_council_item', 'read_project_tracker']
    assert [call['source_role'] for call in provider['tool_calls']] == [
        'ADOPTED_ACTION_MINUTES', 'CITY_INFORMATIONAL_TRACKER']
    assert provider['usage_as_returned'] == {'prompt_tokens': 33, 'completion_tokens': 9}
    assert TEST_KEY not in json.dumps(payload)

    # The serialized requests: three, in the product protocol order, naming only this case.
    assert len(spy.log) == REQUESTS_PER_CLAIM_CEILING == MAX_TOOL_ROUNDS + 1 == 3
    assert len([body for body in spy.log if body.get('tools')]) == MAX_TOOL_ROUNDS
    request_text = json.dumps(spy.log)
    for foreign in ('Exeter', 'cityofexeter', 'EX-MINUTES', 'EX-TRACKER', 'J.1', 'I.4',
                    'Row 14', 'E Street'):
        assert foreign not in request_text, foreign
    first = spy.log[0]
    assert first['tools'][0]['function']['parameters']['properties']['source_id']['enum'] == [
        seeded.sources['minutes']]
    assert first['tools'][1]['function']['parameters']['properties']['source_id']['enum'] == [
        seeded.sources['tracker']]
    assert json.loads(first['messages'][1]['content'])['question_id'] == (
        seeded.claims['authorization'])

    # The existing durable accounting path recorded exactly one admission and one settlement.
    status = app_module.open_spend_backend().status()
    assert status['requests_committed'] == 1
    assert status['reservations']['settled'] >= 1
    assert status['reservations']['uncertain'] == 0
    events = [entry.get('event') for entry in status['accounting_history']]
    assert 'reserve' in events and 'settle' in events
    assert status['accounting']['by_state']['settled'] >= 1


# -- 3. the Court's existing refusals survive the product route ---------------------------------

@pytest.mark.asyncio
async def test_unseen_citation_is_still_refused_and_the_spend_is_not_refunded(client, monkeypatch):
    seeded = await seed_case(client, extra='reviewed')
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    script = [
        tool_calls_response('u-1', [('read_council_item', {'source_id': seeded.sources['minutes']})]),
        tool_calls_response('u-2', [('read_project_tracker', {'source_id': seeded.sources['tracker']})]),
        answer_response('u-3', authorization_answer(seeded.claims['authorization'],
                                                    seeded.facts['vote'])),
    ]
    spy = install_spy(monkeypatch, script)
    try:
        response = await post_case_investigation(
            client, seeded.case_id, claim_id=seeded.claims['authorization'],
            revision=seeded.revision)
    finally:
        await spy.aclose()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['court_result']['status'] == 'REVIEW_REQUIRED'
    assert 'UNSEEN_SOURCE_CITATION' in payload['court_result']['reason_codes']
    # On a refusal the Court shows the claim's own trusted context, never the unseen citation.
    shown = [item['fact_id'] for item in payload['court_result']['source_citations']]
    assert seeded.facts['vote'] not in shown
    assert shown == [seeded.facts['cap']]
    assert len(spy.log) == 3
    status = app_module.open_spend_backend().status()
    assert status['requests_committed'] == 1
    assert status['accounting']['refunded'] == 0


@pytest.mark.asyncio
async def test_unreviewed_mapping_is_still_refused_on_the_product_route(client, monkeypatch):
    seeded = await seed_case(client, extra='pending')
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    script = [
        tool_calls_response('p-1', [('read_council_item', {'source_id': seeded.sources['minutes2']})]),
        tool_calls_response('p-2', [('read_project_tracker', {'source_id': seeded.sources['tracker']})]),
        answer_response('p-3', authorization_answer(seeded.claims['authorization'],
                                                    seeded.facts['vote'])),
    ]
    spy = install_spy(monkeypatch, script)
    try:
        response = await post_case_investigation(
            client, seeded.case_id, claim_id=seeded.claims['authorization'],
            revision=seeded.revision)
    finally:
        await spy.aclose()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['court_result']['status'] == 'REVIEW_REQUIRED'
    assert 'UNREVIEWED_FACT_MAPPING' in payload['court_result']['reason_codes']
    assert 'UNSEEN_SOURCE_CITATION' not in payload['court_result']['reason_codes']


# -- 4. the workbench page and its one live form post -------------------------------------------

@pytest.mark.asyncio
async def test_workbench_page_shows_selection_eligibility_and_disabled_live_activation(client):
    seeded = await seed_case(client)
    page = await client.get(f'/workbench/cases/{seeded.case_id}/investigate')

    assert page.status_code == 200
    text = page.text
    assert 'LIVE SELECTED-CASE INVESTIGATION' in text
    assert 'DISABLED_PENDING_OWNER_AUTHORIZATION' in text
    assert 'LIVE ACTIVATION: DISABLED' in text
    assert 'No provider is contacted from this page.' in text
    assert seeded.case_id in text
    assert f'<strong>revision</strong> {seeded.revision}' in text
    assert seeded.claims['authorization'] in text
    assert 'Claim that would be investigated' in text
    assert 'two-source-role eligibility: ELIGIBLE' in text
    assert 'ADOPTED_ACTION_MINUTES' in text and 'CITY_INFORMATIONAL_TRACKER' in text
    assert 'LIVE_OWNER_AUTHORIZATION → BLOCKED' in text
    assert 'TWO_DISTINCT_REVIEWED_SOURCE_ROLES → PASS' in text
    assert 'CLAIM_CONFIRMED_BY_A_HUMAN → PASS' in text
    assert f'name="revision" value="{seeded.revision}"' in text
    assert 'investigate/live' in text
    assert 'disabled' in text
    assert 'ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED' in text


@pytest.mark.asyncio
async def test_workbench_live_form_renders_typed_refusals_with_zero_provider_calls(
        client, monkeypatch):
    first = await seed_case(client)
    second = await seed_case(client)
    ledger = forbid_ledger(monkeypatch)
    calls: list[int] = []
    constructions: list[int] = []
    real_factory = app_module.configured_live_agent

    def counting_factory(court):
        calls.append(1)
        agent = real_factory(court)
        constructions.append(1)
        return agent

    monkeypatch.setattr(app_module, 'configured_live_agent', counting_factory)

    live_off = await client.post(
        f'/workbench/cases/{first.case_id}/investigate/live',
        content=f'claim_id={first.claims["authorization"]}&revision={first.revision}'.encode(),
        headers=FORM_HEADERS)
    assert live_off.status_code == 503
    assert 'LIVE_INFERENCE_NOT_OWNER_ENABLED' in live_off.text

    stale = await client.post(
        f'/workbench/cases/{first.case_id}/investigate/live',
        content=f'claim_id={first.claims["authorization"]}&revision={first.revision - 1}'.encode(),
        headers=FORM_HEADERS)
    assert stale.status_code == 409
    assert 'STALE_CASE_REVISION' in stale.text

    crossed = await client.post(
        f'/workbench/cases/{first.case_id}/investigate/live',
        content=f'claim_id={second.claims["authorization"]}&revision={first.revision}'.encode(),
        headers=FORM_HEADERS)
    assert crossed.status_code == 404
    assert 'CLAIM_NOT_IN_CASE' in crossed.text

    # Only the live-off request reached the agent factory at all; it refused at the owner gate,
    # so no agent, client, reservation or socket existed for any of the three refusals.
    assert calls == [1] and constructions == []
    assert ledger.never_opened()
