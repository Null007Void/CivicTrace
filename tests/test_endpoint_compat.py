"""Endpoint-conformance tests for the owner-mandated us-central1 BASE_URL.

Task CTRACE-G2-ENDPOINT-001. Every provider interaction below is a synthetic
``httpx.MockTransport``: no live Nebius/NVIDIA request, no credential, no charge.
"""
import json
from urllib.parse import urlsplit

import httpx
import pytest

import civictrace.nebius as nebius
from civictrace.court import Court
from civictrace.nebius import BASE_URL, CHAT_COMPLETIONS_PATH, NebiusAgent

# Owner directive 2026-10-28, verbatim. Must not be re-derived or normalised.
MANDATED = 'https://api.tokenfactory.us-central1.nebius.com/v1/'
SUPER = 'nvidia/nemotron-3-super-120b-a12b'
TEST_KEY = 'example-test-key'

ENDPOINT_ENV_NAMES = (
    'NEBIUS_BASE_URL', 'NEBIUS_ENDPOINT', 'NEBIUS_API_BASE',
    'OPENAI_BASE_URL', 'NEBIUS_API_URL', 'CIVICTRACE_NEBIUS_URL',
)


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


def test_base_url_is_exactly_the_owner_mandated_endpoint():
    assert BASE_URL == MANDATED
    assert isinstance(BASE_URL, str)
    assert BASE_URL.endswith('/')


def test_chat_completions_path_derivation_is_unchanged_in_shape():
    assert CHAT_COMPLETIONS_PATH == 'chat/completions'
    derived = BASE_URL + CHAT_COMPLETIONS_PATH
    parts = urlsplit(derived)
    assert parts.scheme == 'https'
    assert parts.netloc == 'api.tokenfactory.us-central1.nebius.com'
    assert parts.path == '/v1/chat/completions'
    assert parts.query == '' and parts.fragment == ''
    assert '//' not in parts.path


def test_legacy_global_host_is_gone_from_the_endpoint_constant():
    assert 'us-central1' in BASE_URL
    assert 'api.tokenfactory.nebius.com' not in BASE_URL
    assert not hasattr(nebius, 'GLOBAL_BASE_URL')


@pytest.mark.parametrize('name', ENDPOINT_ENV_NAMES)
def test_environment_cannot_override_the_endpoint_constant(monkeypatch, name):
    monkeypatch.setenv(name, 'https://attacker.invalid/v1')
    assert nebius.BASE_URL == BASE_URL == MANDATED


@pytest.mark.asyncio
async def test_on_the_wire_url_is_exactly_the_mandated_endpoint_plus_path():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=tool_message() if len(seen) == 1 else final_message())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NebiusAgent(Court.from_directory(), key=TEST_KEY, model=SUPER,
                                   client=client).run('authorization')
    assert seen == [MANDATED + 'chat/completions', MANDATED + 'chat/completions']
    assert result.court_result['status'] == 'DOCUMENTED_ACTION'


@pytest.mark.asyncio
async def test_reported_provider_endpoint_matches_the_host_actually_contacted():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        return httpx.Response(200, json=tool_message() if len(seen) == 1 else final_message())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NebiusAgent(Court.from_directory(), key=TEST_KEY, model=SUPER,
                                   client=client).run('authorization')
    reported = urlsplit(result.provider['endpoint'])
    assert result.provider['endpoint'] == MANDATED
    assert {host for host, _ in seen} == {reported.netloc}
    assert {path for _, path in seen} == {reported.path + 'chat/completions'}
