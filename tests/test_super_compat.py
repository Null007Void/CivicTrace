"""Model-allowlist regression tests for the G2 Super compatibility correction.

Every provider interaction here is a synthetic ``httpx.MockTransport``. No live
Nebius endpoint is contacted, no credential is read and no paid inference occurs.
"""
import json

import httpx
import pytest

from civictrace.court import Court
from civictrace.nebius import (
    ALLOWED_MODELS,
    BASE_URL,
    DEFAULT_MODEL,
    NebiusAgent,
    ProviderUnavailable,
    configured_live_agent,
)

LIGHTNING = 'nvidia/Nemotron-3_5-Lightning'
SUPER = 'nvidia/nemotron-3-super-120b-a12b'
TEST_KEY = 'example-test-key'


def tool_message():
    return {'id': 'provider-tool-1', 'choices': [{'message': {'role': 'assistant', 'content': None,
        'tool_calls': [
            {'id': 'tool-a', 'type': 'function', 'function': {'name': 'read_council_item', 'arguments': '{"item":"J.1"}'}},
            {'id': 'tool-b', 'type': 'function', 'function': {'name': 'read_project_tracker', 'arguments': '{"row":14}'}},
        ]}, 'finish_reason': 'tool_calls'}],
        'usage': {'prompt_tokens': 135, 'completion_tokens': 27}}


def final_message():
    content = {'claim_id': 'authorization', 'proposed_predicate': 'AUTHORIZED_USE_CEILING',
               'fact_ids': ['EX-AUTH-CAP'],
               'observation': 'The council authorized a ceiling; this is not a disbursement.'}
    return {'id': 'provider-final-2',
            'choices': [{'message': {'role': 'assistant', 'content': json.dumps(content)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 170, 'completion_tokens': 55}}


def recording_client(seen, expected_model):
    """Mock transport that records host/path/model and asserts the fixed endpoint."""
    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.host, request.url.path, body['model']))
        assert request.url.host == 'api.tokenfactory.us-central1.nebius.com'
        assert request.url.path == '/v1/chat/completions'
        assert body['model'] == expected_model
        return httpx.Response(200, json=tool_message() if len(seen) == 1 else final_message())
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_allowlist_is_exactly_lightning_plus_super():
    assert DEFAULT_MODEL == LIGHTNING
    assert isinstance(ALLOWED_MODELS, frozenset)
    assert ALLOWED_MODELS == frozenset({LIGHTNING, SUPER})


@pytest.mark.asyncio
async def test_lightning_roundtrip_behavior_preserved():
    seen = []
    async with recording_client(seen, LIGHTNING) as client:
        result = await NebiusAgent(Court.from_directory(), key=TEST_KEY, model=LIGHTNING, client=client).run('authorization')
    assert result.court_result['status'] == 'DOCUMENTED_ACTION'
    assert result.provider['model'] == LIGHTNING
    assert [model for _, _, model in seen] == [LIGHTNING, LIGHTNING]


@pytest.mark.asyncio
async def test_super_roundtrip_accepted_and_allowlisted_on_the_wire():
    seen = []
    async with recording_client(seen, SUPER) as client:
        result = await NebiusAgent(Court.from_directory(), key=TEST_KEY, model=SUPER, client=client).run('authorization')
    assert result.court_result['status'] == 'DOCUMENTED_ACTION'
    assert result.provider['model'] == SUPER
    assert [model for _, _, model in seen] == [SUPER, SUPER]


@pytest.mark.parametrize('model', [
    'untrusted/unknown',
    'nvidia/Nemotron-3-Super-120B-A12B',
    'nvidia/nemotron-3-super-120b-a12b-v1',
    'nvidia/nemotron-3_5-lightning',
    ' nvidia/nemotron-3-super-120b-a12b',
    'nvidia/nemotron-3-super-120b-a12b ',
    'nvidia/nemotron-3-super-120b-a12b\n',
    '',
])
def test_unallowlisted_model_ids_fail_closed(model):
    with pytest.raises(ProviderUnavailable, match='UNAPPROVED_MODEL_ID'):
        NebiusAgent(Court.from_directory(), key=TEST_KEY, model=model)


@pytest.mark.asyncio
@pytest.mark.parametrize('model', [
    'untrusted/unknown',
    'nvidia/nemotron-3-super-120b-a12b ',  # near-miss: trailing space
    'nvidia/Nemotron-3-Super-120B-A12B',   # near-miss: case change
])
async def test_rejected_model_makes_no_provider_call(model):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={'error': 'must not be reached'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable, match='UNAPPROVED_MODEL_ID'):
            NebiusAgent(Court.from_directory(), key=TEST_KEY, model=model, client=client)
    assert calls == []


def _owner_enabled(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)


def test_env_unset_still_selects_lightning(monkeypatch):
    _owner_enabled(monkeypatch)
    monkeypatch.delenv('NEBIUS_MODEL', raising=False)
    assert configured_live_agent(Court.from_directory()).model == LIGHTNING


def test_env_may_select_super(monkeypatch):
    _owner_enabled(monkeypatch)
    monkeypatch.setenv('NEBIUS_MODEL', SUPER)
    assert configured_live_agent(Court.from_directory()).model == SUPER


@pytest.mark.parametrize('model', [
    'untrusted/unknown',
    'nvidia/nemotron-3-super-120b-a12b ',
    'nvidia/Nemotron-3-Super-120B-A12B',
])
def test_env_selection_cannot_bypass_allowlist(monkeypatch, model):
    _owner_enabled(monkeypatch)
    monkeypatch.setenv('NEBIUS_MODEL', model)
    with pytest.raises(ProviderUnavailable, match='UNAPPROVED_MODEL_ID'):
        configured_live_agent(Court.from_directory())


def test_owner_gate_still_required_for_super(monkeypatch):
    monkeypatch.delenv('CIVICTRACE_LIVE_ENABLED', raising=False)
    monkeypatch.setenv('NEBIUS_API_KEY', TEST_KEY)
    monkeypatch.setenv('NEBIUS_MODEL', SUPER)
    with pytest.raises(ProviderUnavailable, match='LIVE_INFERENCE_NOT_OWNER_ENABLED'):
        configured_live_agent(Court.from_directory())


@pytest.mark.asyncio
async def test_endpoint_configuration_cannot_bypass_allowlist(monkeypatch):
    for name in ('NEBIUS_BASE_URL', 'NEBIUS_ENDPOINT', 'NEBIUS_API_BASE', 'OPENAI_BASE_URL'):
        monkeypatch.setenv(name, 'https://attacker.invalid/v1')
    seen = []
    async with recording_client(seen, SUPER) as client:
        result = await NebiusAgent(Court.from_directory(), key=TEST_KEY, model=SUPER, client=client).run('authorization')
    assert result.court_result['status'] == 'DOCUMENTED_ACTION'
    assert {(host, path) for host, path, _ in seen} == {('api.tokenfactory.us-central1.nebius.com', '/v1/chat/completions')}
    assert BASE_URL == 'https://api.tokenfactory.us-central1.nebius.com/v1/'
