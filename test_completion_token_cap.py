"""Offline request-contract tests: no real provider or credential."""
import json
import httpx
import pytest
from civictrace.court import Court
from civictrace.nebius import NebiusAgent, ProviderUnavailable

@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['nvidia/nemotron-3-super-120b-a12b', 'nvidia/Nemotron-3_5-Lightning'])
async def test_explicit_completion_cap_on_each_tool_and_final_turn(model):
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body['max_completion_tokens'] == 700
        assert 'max_tokens' not in body
        assert body['model'] == model
        return httpx.Response(200, json={'id': 'mock-only', 'choices': [
            {'message': {'role': 'assistant', 'content': '{}'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agent = NebiusAgent(Court.from_directory(), key='test-only', model=model, client=client)
        for turn in (1, 2, 3):
            await agent._call(client, [{'role': 'user', 'content': 'test'}],
                              allow_tools=turn < 3, round_num=turn)
    assert len(bodies) == 3
    assert 'tools' in bodies[0] and 'tools' in bodies[1]
    assert 'tools' not in bodies[2] and 'tool_choice' not in bodies[2]

@pytest.mark.asyncio
async def test_completion_exhaustion_stops_without_retry_or_court_success():
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'mock-truncated', 'choices': [
            {'message': {'role': 'assistant', 'content': '{'}, 'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 700}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable, match='PROVIDER_OUTPUT_TRUNCATED'):
            await NebiusAgent(Court.from_directory(), key='test-only',
                              model='nvidia/nemotron-3-super-120b-a12b', client=client).run('authorization')
    assert len(bodies) == 1
    assert bodies[0]['max_completion_tokens'] == 700
    assert 'max_tokens' not in bodies[0]
