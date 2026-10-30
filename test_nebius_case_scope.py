"""CT-QC-061: case-scoped Nebius wiring, proven through the production path with a mock transport.

Every "provider" interaction below is a synthetic ``httpx.MockTransport``: no live Nebius/NVIDIA
request is made, no credential is read, nothing is charged. The non-sample case is built through the
**public case-store API** — the same calls the workbench makes — from the workbench's own synthetic
fixture (reserved ``.invalid`` hostnames, fictional municipality), so the Court under test is exactly
what a real second case produces.

What these tests are for, in one line each:

* the shipped sample still derives the two role tools and its own declared entry selectors;
* a workbench case derives *its own* selectors, and no Exeter string can appear in its request;
* a mock run drives the same ``NebiusAgent`` code a live run would use, end to end, to a Court
  decision over the synthetic case's citations;
* live eligibility is derived from document *role* plus reviewed fact mappings, refuses before any
  request, and flips to eligible on the same case once the mappings are reviewed;
* every ceiling and the two-distinct-tools invariant survive unchanged;
* a case cannot read another case's source through its own tool set.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from civictrace.case_store import CaseStore, case_claim_book, case_registry
from civictrace.court import Court
from civictrace.nebius import (
    LIVE_ELIGIBILITY_REFUSAL,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_TOKENS,
    MAX_TOOL_ROUNDS,
    REQUESTS_PER_CLAIM_CEILING,
    NebiusAgent,
    ProviderUnavailable,
    configured_live_agent,
    derive_case_tooling,
    live_eligibility,
)

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'synthetic_case.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
CASE_FIELDS = ('project_name', 'municipality', 'case_title', 'jurisdiction_id',
               'publisher', 'allowed_hosts', 'provenance_note')
TEST_KEY = 'example-test-key'
USAGE = {'prompt_tokens': 11, 'completion_tokens': 3}


# -- synthetic cases, built the way the workbench builds them ------------------------------------

def seed_case(store: CaseStore, *, reviewed: bool = True, with_tracker: bool = True):
    """Create a case through the public store API; return the generated ids."""
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    case_id = case['case_id']
    sources: dict[str, str] = {}
    for source in FIXTURE['sources']:
        if not with_tracker and source['key'] == 'tracker':
            continue
        record = store.add_source(
            case_id, title=source['title'], publisher=source['publisher'],
            official_url=source['official_url'], document_role=source['document_role'],
            document_date=source['document_date'], as_of=source['as_of'],
            anchor=source['anchor'], text=source['text'])
        sources[source['key']] = record['source_id']
    facts: dict[str, str] = {}
    for fact in FIXTURE['facts']:
        if fact['source'] not in sources:
            continue
        payload = {key: value for key, value in fact.items() if key not in ('key', 'source')}
        record = store.add_fact(case_id, source_id=sources[fact['source']], **payload)
        facts[fact['key']] = record['fact_id']
    if reviewed:
        for fact_id in facts.values():
            store.update_fact(case_id, fact_id, {'review_state': 'HUMAN_REVIEWED'})
    claims: dict[str, str] = {}
    for claim in FIXTURE['claims']:
        if any(key not in facts for key in claim['fact_keys']):
            continue
        payload = {key: value for key, value in claim.items() if key not in ('key', 'fact_keys')}
        record = store.add_claim(case_id, fact_ids=[facts[key] for key in claim['fact_keys']],
                                 **payload)
        claims[claim['key']] = record['claim_id']
    return case_id, sources, facts, claims


@pytest.fixture()
def synthetic(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, sources, facts, claims = seed_case(store)
    return store, case_id, sources, facts, claims


def court_for(store: CaseStore, case_id: str) -> Court:
    return Court.from_case_store(store, case_id)


# -- synthetic provider transport ----------------------------------------------------------------

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


def scripted_client(script, log, calls_seen=None):
    """Mock transport: record every request body and replay ``script`` in order."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'api.tokenfactory.us-central1.nebius.com'
        assert request.url.path.endswith('/v1/chat/completions')
        body = json.loads(request.content)
        log.append(body)
        if calls_seen is not None:
            calls_seen.append(1)
        assert len(log) <= len(script), 'product issued more requests than the run protocol allows'
        return httpx.Response(200, json=script[len(log) - 1])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def authorization_answer(claim_id: str, cap_fact_id: str) -> dict:
    return {'claim_id': claim_id, 'proposed_predicate': 'AUTHORIZED_USE_CEILING',
            'fact_ids': [cap_fact_id],
            'observation': 'The synthetic minutes authorize a ceiling; nothing here is a payment.'}


# -- 1. the shipped sample still derives exactly what it did before -------------------------------

def test_sample_case_tooling_is_derived_from_its_own_declared_records():
    sample = Court.from_exeter_sample()
    tooling = derive_case_tooling(sample)
    assert tooling.case_id == sample.case_id
    assert tooling.tool_names() == ['read_council_item', 'read_project_tracker']

    council, tracker = tooling.tools
    assert council['function']['parameters'] == {
        'type': 'object', 'properties': {'item': {'type': 'string', 'enum': ['J.1', 'I.4']}},
        'required': ['item'], 'additionalProperties': False,
    }
    assert tracker['function']['parameters'] == {
        'type': 'object', 'properties': {'row': {'type': 'integer', 'enum': [14]}},
        'required': ['row'], 'additionalProperties': False,
    }
    # Descriptions come from the case's own source titles, not from a hardcoded sentence.
    assert 'Exeter City Council action minutes' in council['function']['description']
    assert 'project tracker' in tracker['function']['description'].lower()

    # Declared entry selectors resolve against the Court natively; no generic fallback is used.
    assert {selector.court_tool for selector in tooling.selectors} == {
        'read_council_item', 'read_project_tracker'}
    assert [selector.args for selector in tooling.selectors] == [
        {'item': 'J.1'}, {'item': 'I.4'}, {'row': 14}]
    assert tooling.eligibility['eligible'] is True
    assert tooling.source_scope.startswith('Only the 2 source(s) registered in case ')
    assert 'Exeter' not in tooling.system_prompt.split('CASE')[0]  # no case name in the preamble


# -- 2. a workbench case offers its own selectors and nothing foreign -----------------------------

def test_workbench_case_offers_its_own_source_ids_and_no_foreign_selectors(synthetic):
    store, case_id, sources, _facts, _claims = synthetic
    tooling = derive_case_tooling(court_for(store, case_id))

    assert tooling.tool_names() == ['read_council_item', 'read_project_tracker']
    council, tracker = tooling.tools
    assert council['function']['parameters']['properties']['source_id']['enum'] == [
        sources['minutes']]
    assert tracker['function']['parameters']['properties']['source_id']['enum'] == [
        sources['tracker']]
    assert [selector.court_tool for selector in tooling.selectors] == [
        'read_case_source', 'read_case_source']
    assert [selector.source_id for selector in tooling.selectors] == [
        sources['minutes'], sources['tracker']]

    request_text = json.dumps({'tools': list(tooling.tools),
                               'system': tooling.system_prompt,
                               'scope': tooling.source_scope})
    for foreign in ('Exeter', 'E Street', 'J.1', 'I.4', 'row 14', 'cityofexeter'):
        assert foreign not in request_text, foreign
    assert 'Synthetic Example Municipality (fictional test fixture)' in tooling.system_prompt
    assert 'SYNTHETIC: did the fictional council authorize the sidewalk pilot?' in tooling.system_prompt
    assert case_id in tooling.source_scope
    assert tooling.eligibility['eligible'] is True
    assert tooling.eligibility['roles']['ADOPTED_ACTION_MINUTES']['reviewed_fact_count'] == 2
    assert tooling.eligibility['roles']['CITY_INFORMATIONAL_TRACKER']['reviewed_fact_count'] == 1


# -- 3. the production loop, mock transport, second case ------------------------------------------

@pytest.mark.asyncio
async def test_mock_run_traverses_the_selected_case_wiring_end_to_end(synthetic, monkeypatch):
    """The same ``NebiusAgent.run`` a live run would execute, with only the transport mocked."""
    store, case_id, sources, facts, claims = synthetic
    court = court_for(store, case_id)
    log: list[dict] = []
    script = [
        tool_calls_response('sc-1', [('read_council_item', {'source_id': sources['minutes']})]),
        tool_calls_response('sc-2', [('read_project_tracker', {'source_id': sources['tracker']})]),
        answer_response('sc-3', authorization_answer(claims['authorization'], facts['cap'])),
    ]
    client = scripted_client(script, log)
    async with client:
        agent = NebiusAgent(court, key=TEST_KEY, client=client)
        result = await agent.run(claims['authorization'])

    assert len(log) == 3
    tool_bodies = [body for body in log if body.get('tools')]
    finalization_bodies = [body for body in log if 'tools' not in body]
    assert len(tool_bodies) == MAX_TOOL_ROUNDS == 2
    assert len(finalization_bodies) == 1

    # The request names the selected case and only that case's records.
    for body in tool_bodies:
        assert body['tools'] == list(agent.tooling.tools)
        assert body['tool_choice'] == 'auto'
        assert body['max_completion_tokens'] == MAX_TOKENS == 700
        payload = json.loads(body['messages'][1]['content'])
        assert payload['question_id'] == claims['authorization']
        assert payload['source_scope'] == agent.tooling.source_scope
        assert case_id in payload['source_scope']
        assert body['messages'][0]['content'] == agent.tooling.system_prompt
        assert 'Exeter' not in json.dumps(body)

    # The decision is the synthetic case's Court decision over facts the tools actually returned.
    assert result.court_result['status'] == 'DOCUMENTED_ACTION'
    assert [citation['fact_id'] for citation in result.court_result['source_citations']] == [
        facts['cap']]
    assert result.court_result['source_citations'][0]['title'] == FIXTURE['sources'][0]['title']
    assert result.provider['case_id'] == case_id
    assert result.provider['case_registry_sha256'] == court.registry_sha256
    assert result.provider['offered_tools'] == ['read_council_item', 'read_project_tracker']
    assert result.provider['live_eligibility']['eligible'] is True
    assert [call['name'] for call in result.provider['tool_calls']] == [
        'read_council_item', 'read_project_tracker']
    assert [call['source_role'] for call in result.provider['tool_calls']] == [
        'ADOPTED_ACTION_MINUTES', 'CITY_INFORMATIONAL_TRACKER']
    assert [call['court_tool'] for call in result.provider['tool_calls']] == [
        'read_case_source', 'read_case_source']
    assert result.provider['usage_as_returned'] == {
        'prompt_tokens': 33, 'completion_tokens': 9}
    assert result.provider['usage_complete'] is True
    assert TEST_KEY not in json.dumps(result.as_json())


@pytest.mark.asyncio
async def test_exeter_shaped_selector_is_refused_on_a_workbench_case(synthetic):
    store, case_id, _sources, _facts, claims = synthetic
    log: list[dict] = []
    client = scripted_client([tool_calls_response('x-1', [('read_council_item', {'item': 'J.1'})])],
                             log)
    async with client:
        with pytest.raises(ProviderUnavailable,
                           match='REJECTED_UNALLOWLISTED_TOOL_CALL') as caught:
            await NebiusAgent(court_for(store, case_id), key=TEST_KEY, client=client).run(
                claims['authorization'])
    assert len(log) == 1
    assert caught.value.telemetry['case_id'] == case_id


@pytest.mark.asyncio
async def test_case_cannot_read_another_cases_source_through_its_own_tool_set(synthetic):
    store, case_id, _sources, _facts, claims = synthetic
    other = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})['case_id']
    other_source = store.add_source(
        other, title='Other synthetic minutes', publisher=FIXTURE['case']['publisher'],
        official_url=FIXTURE['sources'][0]['official_url'],
        document_role='ADOPTED_ACTION_MINUTES', document_date='2026-03-02',
        as_of='2026-03-02', anchor='Item Z.9', text='SYNTHETIC FIXTURE - other case.\nItem Z.9\n'
    )['source_id']
    log: list[dict] = []
    client = scripted_client(
        [tool_calls_response('y-1', [('read_council_item', {'source_id': other_source})])], log)
    async with client:
        with pytest.raises(ProviderUnavailable, match='REJECTED_UNALLOWLISTED_TOOL_CALL'):
            await NebiusAgent(court_for(store, case_id), key=TEST_KEY, client=client).run(
                claims['authorization'])
    assert len(log) == 1


# -- 4. live eligibility is case-derived, refuses early, and is not vacuous -----------------------

def test_eligibility_is_derived_from_document_role_and_reviewed_mappings(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _sources, facts, _claims = seed_case(store, reviewed=False)
    unreviewed = live_eligibility(court_for(store, case_id))
    assert unreviewed['eligible'] is False
    assert unreviewed['reason'] == LIVE_ELIGIBILITY_REFUSAL
    assert {role: entry['reviewed_fact_count']
            for role, entry in unreviewed['roles'].items()} == {
        'ADOPTED_ACTION_MINUTES': 0, 'CITY_INFORMATIONAL_TRACKER': 0}
    # Both tools are still derivable; it is the review state that refuses the run.
    assert all(entry['tool_derived'] for entry in unreviewed['roles'].values())

    for fact_id in facts.values():
        store.update_fact(case_id, fact_id, {'review_state': 'HUMAN_REVIEWED'})
    reviewed = live_eligibility(court_for(store, case_id))
    assert reviewed['eligible'] is True
    assert reviewed['reason'] is None
    assert reviewed['roles']['ADOPTED_ACTION_MINUTES']['reviewed_fact_count'] == 2
    assert reviewed['roles']['CITY_INFORMATIONAL_TRACKER']['reviewed_fact_count'] == 1


def test_unreviewed_case_is_refused_before_any_request(tmp_path, monkeypatch):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _sources, _facts, _claims = seed_case(store, reviewed=False)
    court = court_for(store, case_id)
    calls: list[int] = []
    log: list[dict] = []
    client = scripted_client([], log, calls_seen=calls)
    with pytest.raises(ProviderUnavailable, match=LIVE_ELIGIBILITY_REFUSAL) as caught:
        NebiusAgent(court, key=TEST_KEY, client=client)
    assert calls == [] and log == []
    assert caught.value.telemetry['live_eligibility']['eligible'] is False
    assert caught.value.telemetry['case_id'] == case_id

    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    with pytest.raises(ProviderUnavailable, match=LIVE_ELIGIBILITY_REFUSAL):
        configured_live_agent(court)


def test_case_without_a_tracker_role_names_the_missing_boundary(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _sources, _facts, _claims = seed_case(store, with_tracker=False)
    eligibility = live_eligibility(court_for(store, case_id))
    assert eligibility['eligible'] is False
    assert eligibility['roles']['ADOPTED_ACTION_MINUTES']['tool_derived'] is True
    assert eligibility['roles']['CITY_INFORMATIONAL_TRACKER']['tool_derived'] is False
    assert (eligibility['roles']['CITY_INFORMATIONAL_TRACKER']['not_derived_reason']
            == 'ROLE_NOT_REGISTERED_IN_SELECTED_CASE')


def test_configured_live_agent_builds_a_case_scoped_agent_without_contacting_a_provider(
        synthetic, monkeypatch):
    store, case_id, _sources, _facts, _claims = synthetic
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    agent = configured_live_agent(court_for(store, case_id))
    assert isinstance(agent, NebiusAgent)
    assert agent.tooling.case_id == case_id
    assert agent.tooling.tool_names() == ['read_council_item', 'read_project_tracker']
    assert agent.eligibility['eligible'] is True
    assert agent.client is None  # the live client is built only inside run()


# -- 5. every ceiling and the two-distinct-tools invariant survive --------------------------------

@pytest.mark.asyncio
async def test_two_distinct_role_tools_are_still_required_for_a_workbench_case(synthetic):
    store, case_id, sources, facts, claims = synthetic
    log: list[dict] = []
    script = [
        tool_calls_response('d-1', [('read_council_item', {'source_id': sources['minutes']})]),
        tool_calls_response('d-2', [('read_council_item', {'source_id': sources['minutes']})]),
        answer_response('d-3', authorization_answer(claims['authorization'], facts['cap'])),
    ]
    client = scripted_client(script, log)
    async with client:
        with pytest.raises(ProviderUnavailable,
                           match='MODEL_DID_NOT_PERFORM_REQUIRED_SOURCE_READS') as caught:
            await NebiusAgent(court_for(store, case_id), key=TEST_KEY, client=client).run(
                claims['authorization'])
    telemetry = caught.value.telemetry
    assert len(log) == 3                      # 2 tool rounds + 1 finalization round, never a 4th
    assert telemetry['distinct_tools'] == 1
    assert telemetry['executed_tool_calls'] == 2
    assert telemetry['case_id'] == case_id
    assert REQUESTS_PER_CLAIM_CEILING == MAX_TOOL_ROUNDS + 1 == 3


# -- 6. prompt textures are bounded, and case text cannot smuggle a line break --------------------

def test_case_text_is_collapsed_to_one_bounded_printable_line(synthetic):
    store, case_id, _sources, _facts, _claims = synthetic
    registry = case_registry(store.get_case(case_id))
    registry['case_title'] = 'Line one\nLine two\twith tab\x00and nul ' + 'x' * 500
    registry['municipality'] = 'Synthetic\nMunicipality'
    court = Court(registry, case_claim_book(store.get_case(case_id)))
    tooling = derive_case_tooling(court)

    case_lines = [line for line in tooling.system_prompt.split('\n') if line.startswith('CASE ')]
    assert len(case_lines) == 1
    assert '\t' not in tooling.system_prompt and '\x00' not in tooling.system_prompt
    assert 'Line two' in case_lines[0] and 'Synthetic Municipality' in case_lines[0]
    assert len(tooling.system_prompt) <= MAX_SYSTEM_PROMPT_CHARS
    assert len(tooling.tools[0]['function']['description']) <= 400
