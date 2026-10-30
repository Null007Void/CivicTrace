"""Provider adapter unit tests use synthetic MOCK NETWORK RESPONSES, never paid calls."""
import json

import httpx
import pytest
from pydantic import ValidationError

from civictrace.court import Court
from civictrace.nebius import DEFAULT_MODEL, Candidate, NebiusAgent, ProviderUnavailable


def tool_message():
    return {'id':'provider-tool-1','choices':[{'message':{'role':'assistant','content':None,
        'tool_calls':[
            {'id':'tool-a','type':'function','function':{'name':'read_council_item','arguments':'{"item":"J.1"}'}},
            {'id':'tool-b','type':'function','function':{'name':'read_project_tracker','arguments':'{"row":14}'}}
        ]},'finish_reason':'tool_calls'}], 'usage':{'prompt_tokens':135,'completion_tokens':27}}


def final_message(claim_id='authorization',predicate='AUTHORIZED_USE_CEILING',facts=None):
    content={'claim_id':claim_id,'proposed_predicate':predicate,'fact_ids':facts if facts is not None else ['EX-AUTH-CAP'],
             'observation':'The council authorized a ceiling; this is not a disbursement.'}
    return {'id':'provider-final-2','choices':[{'message':{'role':'assistant','content':json.dumps(content)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':170,'completion_tokens':55}}


def client_for_responses(responses,log):
    def handler(req):
        assert req.url.host=='api.tokenfactory.us-central1.nebius.com'
        assert req.headers['authorization']=='Bearer example-test-key'
        assert req.url.path.endswith('/v1/chat/completions')
        body=json.loads(req.content)
        assert body['model']==DEFAULT_MODEL
        assert body['max_completion_tokens']<=700
        assert body['tools'] and body['tool_choice']=='auto'
        log.append(body)
        return httpx.Response(200,json=responses[len(log)-1])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_mock_two_tool_roundtrip_and_court_positive():
    log=[]
    private_ids=[]
    client=client_for_responses([tool_message(),final_message()],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client,
                                 private_response_id_sink=private_ids.append).run('authorization')
    assert result.provider['kind']=='LIVE_NEBIUS'  # Provenance field ONLY; test uses mock HTTP transport.
    assert result.provider['tool_calls'][0]['name']=='read_council_item'
    assert result.provider['tool_calls'][1]['name']=='read_project_tracker'
    assert result.court_result['status']=='DOCUMENTED_ACTION'
    assert result.provider['usage_as_returned']=={'prompt_tokens':305,'completion_tokens':82}
    assert result.provider['usage_complete'] is True
    assert result.candidate=={
        'claim_id':'authorization','registered_fact_ids_proposed':['EX-AUTH-CAP'],
        'observation_disclosed':False,
    }
    assert all(ref.startswith('sha256:') and len(ref)==71 for ref in result.provider['response_refs'])
    assert private_ids==['provider-tool-1','provider-final-2']
    assert len(log)==2
    assert sum(m['role']=='tool' for m in log[1]['messages'])==2


@pytest.mark.asyncio
async def test_mock_bad_paid_claim_is_still_refused():
    log=[]
    client=client_for_responses([tool_message(),final_message('spent','PAID_AMOUNT')],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('spent')
    assert result.court_result['status']=='NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert 'CEILING_NOT_PAYMENT' in result.court_result['reason_codes']


@pytest.mark.asyncio
async def test_mock_wrong_original_claim_relabel_is_blocked():
    log=[]
    client=client_for_responses([tool_message(),final_message('authorization','AUTHORIZED_USE_CEILING')],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('spent')
    assert result.court_result['status']=='REVIEW_REQUIRED'
    assert 'CLAIM_ID_RELABELED' in result.court_result['reason_codes']


@pytest.mark.asyncio
async def test_mock_unseen_fact_refused():
    log=[]
    client=client_for_responses([tool_message(),final_message('authorization','AUTHORIZED_USE_CEILING',['EX-EVENT-CLOSURE'])],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    assert result.court_result['status']=='REVIEW_REQUIRED'
    assert 'UNSEEN_SOURCE_CITATION' in result.court_result['reason_codes']


@pytest.mark.asyncio
async def test_mock_model_without_tools_does_not_claim_mode_a():
    log=[]
    client=client_for_responses([final_message()],log)
    async with client:
        with pytest.raises(ProviderUnavailable,match='MODEL_DID_NOT_PERFORM_REQUIRED_SOURCE_READS'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')


@pytest.mark.asyncio
async def test_unallowlisted_tool_injected_by_model_fails_closed():
    t=tool_message()
    t['choices'][0]['message']['tool_calls'][0]['function']['name']='fetch_url'
    log=[]
    client=client_for_responses([t],log)
    async with client:
        with pytest.raises(ProviderUnavailable,match='REJECTED_UNALLOWLISTED_TOOL_CALL'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')


@pytest.mark.asyncio
async def test_provider_rate_limit_no_retry():
    calls=[]
    def handler(req):
        calls.append(1)
        return httpx.Response(429,json={'error':'secret-data'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable,match='PROVIDER_RATE_LIMITED'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('spent')
    assert len(calls)==1


@pytest.mark.asyncio
async def test_provider_http_auth_error_hidden():
    def handler(req):return httpx.Response(401,json={'error':'secret test token'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable,match='PROVIDER_AUTH_DENIED'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('spent')


def test_schema_rejects_model_extra_fields():
    with pytest.raises(ValidationError):Candidate.model_validate({'claim_id':'spent','proposed_predicate':'PAID_AMOUNT','fact_ids':[],'observation':'x','please_override_verdict':True})


def test_missing_key_rejected():
    with pytest.raises(ProviderUnavailable,match='NO_NEBIUS_CREDENTIAL'):
        NebiusAgent(Court.from_directory(),key='')


def test_model_not_allowlisted():
    with pytest.raises(ProviderUnavailable,match='UNAPPROVED_MODEL_ID'):
        NebiusAgent(Court.from_directory(),key='token',model='untrusted/unknown')

@pytest.mark.asyncio
async def test_tool_budget_exceeded_fails_closed():
    t=tool_message()
    t['choices'][0]['message']['tool_calls']=t['choices'][0]['message']['tool_calls']*3
    calls=[]
    def handler(req):
        calls.append(1)
        return httpx.Response(200,json=t)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable,match='TOOL_BUDGET_EXCEEDED'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('spent')
    assert len(calls)==1


@pytest.mark.asyncio
async def test_model_invalid_json_after_tool_calls_fails_closed():
    bad=final_message()
    bad['choices'][0]['message']['content']='`not valid json`'
    log=[]
    client=client_for_responses([tool_message(),bad],log)
    async with client:
        with pytest.raises(ProviderUnavailable,match='INVALID_CANDIDATE_SCHEMA'):
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')


@pytest.mark.asyncio
async def test_provider_null_usage_still_nonclaiming():
    t=tool_message()
    t['usage']=None
    f=final_message()
    f['usage']=None
    log=[]
    client=client_for_responses([t,f],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    assert result.provider['usage_as_returned']=={'prompt_tokens':None,'completion_tokens':None}
    assert result.provider['usage_complete'] is False


@pytest.mark.asyncio
async def test_truncated_provider_output_preserves_usage_telemetry():
    truncated=final_message()
    truncated['choices'][0]['finish_reason']='length'
    log=[]
    client=client_for_responses([truncated],log)
    async with client:
        with pytest.raises(ProviderUnavailable,match='PROVIDER_OUTPUT_TRUNCATED') as caught:
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    assert caught.value.telemetry['finish_reason']=='length'
    assert caught.value.telemetry['usage_as_returned']=={'prompt_tokens':170,'completion_tokens':55}


@pytest.mark.asyncio
async def test_content_filter_finish_reason_fails_closed():
    filtered=final_message()
    filtered['choices'][0]['finish_reason']='content_filter'
    log=[]
    client=client_for_responses([tool_message(),filtered],log)
    async with client:
        with pytest.raises(ProviderUnavailable,match='PROVIDER_OUTPUT_FILTERED') as caught:
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    assert caught.value.telemetry['finish_reason']=='content_filter'
    assert caught.value.telemetry['usage_as_returned']=={'prompt_tokens':305,'completion_tokens':82}


@pytest.mark.asyncio
async def test_model_free_text_and_provider_id_are_not_disclosed():
    final=final_message()
    payload=json.loads(final['choices'][0]['message']['content'])
    payload['observation']=r'FACT: paid C:\Private\secret.txt'
    final['choices'][0]['message']['content']=json.dumps(payload)
    final['id']=r'C:\Private\provider-receipt'
    log=[]
    client=client_for_responses([tool_message(),final],log)
    async with client:
        result=await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    public=json.dumps(result.as_json())
    assert r'C:\Private' not in public
    assert 'FACT: paid' not in public
    assert result.candidate['observation_disclosed'] is False


@pytest.mark.asyncio
async def test_network_error_marks_usage_unknown():
    def handler(req):
        raise httpx.ConnectError('synthetic',request=req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable,match='PROVIDER_TIMEOUT_OR_NETWORK_ERROR') as caught:
            await NebiusAgent(Court.from_directory(),key='example-test-key',client=client).run('authorization')
    assert caught.value.telemetry['usage_as_returned']=={'prompt_tokens':None,'completion_tokens':None}
    assert caught.value.telemetry['usage_complete'] is False


# ---------------------------------------------------------------------------------------------
# CT-COR-001 (QC-01 product-loop convergence, QC-06 failure-path evidence). Every interaction
# below is a synthetic ``httpx.MockTransport``: no provider is contacted, no credential is sent.
# ---------------------------------------------------------------------------------------------
from civictrace.nebius import FINALIZATION_INSTRUCTION, MAX_TOOL_ROUNDS, REQUESTS_PER_CLAIM_CEILING

# One synthetic completion's usage. Three of them must total 30 prompt + 6 completion, which is
# the QC-06 arithmetic shape ("3x10+2") that the shipped loop previously discarded as null.
USAGE_10_2 = {'prompt_tokens': 10, 'completion_tokens': 2}
COUNCIL_J1 = ('read_council_item', {'item': 'J.1'})
COUNCIL_I4 = ('read_council_item', {'item': 'I.4'})
TRACKER_14 = ('read_project_tracker', {'row': 14})


def scripted_tool_response(response_id, calls, usage=None):
    return {'id': response_id,
            'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'{response_id}-call-{index}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}
                for index, (name, args) in enumerate(calls)]}, 'finish_reason': 'tool_calls'}],
            'usage': dict(USAGE_10_2 if usage is None else usage)}


def scripted_answer_response(response_id, content, usage=None):
    return {'id': response_id,
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': dict(USAGE_10_2 if usage is None else usage)}


def scripted_client(script, log):
    """Mock transport that records each request body and replays the script in order."""
    def handler(req):
        assert req.url.host == 'api.tokenfactory.us-central1.nebius.com'
        assert req.url.path.endswith('/v1/chat/completions')
        body = json.loads(req.content)
        log.append(body)
        assert len(log) <= len(script), 'product issued more requests than the run protocol allows'
        return httpx.Response(200, json=script[len(log) - 1])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def spent_answer():
    return json.dumps({'claim_id': 'spent', 'proposed_predicate': 'PAID_AMOUNT',
                       'fact_ids': ['EX-AUTH-CAP'],
                       'observation': 'Only an authorized ceiling was read; no payment record.'})


def construction_answer():
    return json.dumps({'claim_id': 'construction_from_event',
                       'proposed_predicate': 'CONSTRUCTION_STARTED',
                       'fact_ids': ['EX-EVENT-CLOSURE'],
                       'observation': 'An event-closure approval is not construction evidence.'})


ATTEMPT_SHAPED_READS = {
    'spent': [COUNCIL_J1, TRACKER_14],
    'construction_from_event': [COUNCIL_I4, TRACKER_14],
}
ATTEMPT_SHAPED_ANSWERS = {
    'spent': spent_answer,
    'construction_from_event': construction_answer,
}


def attempt_shaped_client(log):
    """Model behaviour captured in attempts 3/4: while tools are offered it keeps reading; it
    returns a final answer only on a round that withholds them.

    This is the failure shape the shipped loop had to survive: requests 1-2 ask for a read, and a
    third tool-bearing request produces yet another tool call (attempt 3, claim
    ``construction_from_event``). A transport that never withholds tools therefore cannot
    converge, which is exactly what attempt 3 observed.
    """
    requests_per_claim = {}

    def handler(req):
        body = json.loads(req.content)
        log.append(body)
        question = json.loads(body['messages'][1]['content'])['question_id']
        requests_per_claim[question] = requests_per_claim.get(question, 0) + 1
        assert requests_per_claim[question] <= REQUESTS_PER_CLAIM_CEILING, \
            f'request ceiling breached for {question}'
        if 'tools' in body:
            index = requests_per_claim[question] - 1
            reads = ATTEMPT_SHAPED_READS[question]
            return httpx.Response(200, json=scripted_tool_response(
                f'{question}-r{index + 1}', [reads[min(index, len(reads) - 1)]]))
        return httpx.Response(200, json=scripted_answer_response(
            f'{question}-final', ATTEMPT_SHAPED_ANSWERS[question]()))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_product_loop_converges_both_mandatory_challenges_after_two_tool_rounds():
    """QC-01: the shipped loop must finalize each guided claim without harness compensation.

    The scripted model behaves as captured in attempts 3/4: it keeps calling tools while they are
    offered, and answers only when the finalization round withholds them. Under the old loop that
    behaviour exhausted the three-request budget without a final answer; the product now withholds
    tools itself, so both mandatory challenges reach a Court decision.
    """
    log = []
    client = attempt_shaped_client(log)
    court = Court.from_directory()
    async with client:
        agent = NebiusAgent(court, key='example-test-key', client=client)
        spent = await agent.run('spent')
        construction = await agent.run('construction_from_event')

    assert REQUESTS_PER_CLAIM_CEILING == 3
    assert len(log) == 6                                    # 3 per claim, 6 for the two-claim run
    tool_bodies = [body for body in log if body.get('tools')]
    finalization_bodies = [body for body in log if 'tools' not in body]
    assert len(tool_bodies) == 2 * MAX_TOOL_ROUNDS == 4
    assert [index for index, body in enumerate(log) if 'tools' not in body] == [2, 5]
    assert all(body['tool_choice'] == 'auto' for body in tool_bodies)
    assert all('tool_choice' not in body for body in finalization_bodies)
    assert all(body['max_completion_tokens'] == 700 for body in log)
    assert [body['messages'][-1] for body in finalization_bodies] == [
        {'role': 'system', 'content': FINALIZATION_INSTRUCTION},
        {'role': 'system', 'content': FINALIZATION_INSTRUCTION},
    ]
    assert sum(message['role'] == 'tool' for message in log[2]['messages']) == 2

    assert spent.court_result['status'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert 'CEILING_NOT_PAYMENT' in spent.court_result['reason_codes']
    assert construction.court_result['status'] == 'WRONG_ACTION_SOURCE'
    for result in (spent, construction):
        assert result.provider['rounds'] == 3
        assert result.provider['protocol']['finalization_round_used'] is True
        assert result.provider['protocol']['finalization_tools_withheld'] is True
        assert result.provider['protocol']['requests_per_claim_ceiling'] == 3
        assert len(result.provider['response_refs']) == 3
        assert result.provider['usage_as_returned'] == {'prompt_tokens': 30, 'completion_tokens': 6}
        assert result.provider['usage_complete'] is True
        assert [call['name'] for call in result.provider['tool_calls']] == [
            'read_council_item', 'read_project_tracker']
        assert all(ref.startswith('sha256:') and len(ref) == 71
                   for ref in result.provider['response_refs'])


@pytest.mark.asyncio
async def test_turn_budget_failure_retains_usage_refs_and_tool_history():
    """QC-06: three requests of 10+2 tokens must report 30/6 with refs — not null usage.

    The scripted model keeps asking for tools, including on the finalization round. Those calls
    must not be executed (tools were not offered) and no fourth request may be issued; the
    failure must still carry everything the run observed.
    """
    log = []
    script = [
        scripted_tool_response('t1', [COUNCIL_J1]),
        scripted_tool_response('t2', [TRACKER_14]),
        scripted_tool_response('t3', [COUNCIL_J1]),
    ]
    client = scripted_client(script, log)
    async with client:
        with pytest.raises(ProviderUnavailable, match='PROVIDER_TURN_BUDGET_EXCEEDED') as caught:
            await NebiusAgent(Court.from_directory(), key='example-test-key', client=client).run('authorization')

    telemetry = caught.value.telemetry
    assert len(log) == 3                                     # structural ceiling: no request #4
    assert telemetry['usage_as_returned'] == {'prompt_tokens': 30, 'completion_tokens': 6}
    assert telemetry['usage_complete'] is True
    assert telemetry['usage_basis'] == 'PROVIDER_REPORTED_TOKEN_COUNTS_NOT_BILLED_COST'
    assert telemetry['provider_requests'] == 3
    assert len(telemetry['response_refs']) == 3
    assert all(ref.startswith('sha256:') and len(ref) == 71 for ref in telemetry['response_refs'])
    assert telemetry['error_class'] == 'PROVIDER_TURN_BUDGET_EXCEEDED'
    assert telemetry['finalization_round'] is True
    assert telemetry['finalization_tool_calls_rejected'] == 1
    assert [call['name'] for call in telemetry['tool_calls']] == [
        'read_council_item', 'read_project_tracker']
    assert [call['provider_round'] for call in telemetry['tool_calls']] == [1, 2]
    public = json.dumps(telemetry)
    assert 't1' not in public and 't3-call-0' not in public  # raw provider IDs never leak


@pytest.mark.asyncio
async def test_schema_failure_after_finalization_keeps_accumulated_evidence():
    log = []
    invalid = '`not valid json`'
    script = [
        scripted_tool_response('x1', [COUNCIL_J1]),
        scripted_tool_response('x2', [TRACKER_14]),
        scripted_answer_response('x3', invalid),
    ]
    client = scripted_client(script, log)
    async with client:
        with pytest.raises(ProviderUnavailable, match='INVALID_CANDIDATE_SCHEMA') as caught:
            await NebiusAgent(Court.from_directory(), key='example-test-key', client=client).run('authorization')

    telemetry = caught.value.telemetry
    assert telemetry['usage_as_returned'] == {'prompt_tokens': 30, 'completion_tokens': 6}
    assert telemetry['usage_complete'] is True
    assert len(telemetry['response_refs']) == 3
    assert len(telemetry['tool_calls']) == 2
    assert telemetry['finish_reason'] == 'stop'
    assert telemetry['content_length'] == len(invalid)
    assert telemetry['error_class'] == 'INVALID_CANDIDATE_SCHEMA'
    assert invalid not in json.dumps(telemetry)              # model narrative is never stored


@pytest.mark.asyncio
async def test_attempt4_single_tool_finalization_keeps_two_source_invariant():
    """Attempt 4: two executed calls naming ONE distinct tool, then an empty final answer.

    The two-source invariant must still fail closed; the change is that the failure is typed and
    keeps the accumulated usage, response references and executed tool history.
    """
    log = []
    script = [
        scripted_tool_response('a1', [COUNCIL_J1]),
        scripted_tool_response('a2', [COUNCIL_J1]),
        scripted_answer_response('a3', None),
    ]
    client = scripted_client(script, log)
    async with client:
        with pytest.raises(ProviderUnavailable,
                           match='MODEL_DID_NOT_PERFORM_REQUIRED_SOURCE_READS') as caught:
            await NebiusAgent(Court.from_directory(), key='example-test-key',
                              client=client).run('construction_from_event')

    telemetry = caught.value.telemetry
    assert len(log) == 3
    assert telemetry['distinct_tools'] == 1
    assert telemetry['executed_tool_calls'] == 2
    assert telemetry['usage_as_returned'] == {'prompt_tokens': 30, 'completion_tokens': 6}
    assert len(telemetry['response_refs']) == 3
    assert telemetry['finalization_round'] is True

