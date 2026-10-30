"""Synthetic transport and rendering regressions; no provider calls."""
import json
import httpx
import pytest
from civictrace.court import Court
from civictrace.nebius import NebiusAgent, ProviderUnavailable
from civictrace.workbench_ui import _live_result_page
from test_nebius import tool_message, final_message, client_for_responses

@pytest.mark.asyncio
@pytest.mark.parametrize("facts,status", [(["EX-AUTH-CAP"],"DOCUMENTED_ACTION"),(["EX-AUTH-CAP","EX-CITY-PHASE"],"REVIEW_REQUIRED"),(["EX-CITY-PHASE"],"NOT_VERIFIED_FROM_SELECTED_RECORDS")])
async def test_context_is_not_automatically_support(facts,status):
    log=[]
    async with client_for_responses([tool_message(),final_message(facts=facts)],log) as client:
        run=await NebiusAgent(Court.from_directory(),key="example-test-key",client=client).run("authorization")
    assert run.court_result["status"]==status
    assert run.candidate["registered_fact_ids_proposed"]==facts
    assert run.provider["requests_dispatched"]==run.provider["responses_received"]==run.provider["provider_requests"]==2
    assert len(log)==2

@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["http","timeout","malformed"])
async def test_failure_dispatch_and_response_counts_no_retry(failure):
    calls=[]
    def handler(req):
        calls.append(1)
        if failure=="timeout": raise httpx.ReadTimeout("SECRET_SENTINEL",request=req)
        return httpx.Response(401 if failure=="http" else 200,text="SECRET_SENTINEL")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable) as exc:
            await NebiusAgent(Court.from_directory(),key="SECRET_SENTINEL",client=client).run("authorization")
    evidence=exc.value.telemetry
    assert evidence["provider_requests"]==evidence["requests_dispatched"]==1
    assert evidence["responses_received"]==(0 if failure=="timeout" else 1)
    assert evidence["validated_responses"]==0
    assert "SECRET_SENTINEL" not in json.dumps(evidence)
    assert len(calls)==1

def test_diagnostics_escape_and_allowlist():
    run={"candidate":{"registered_fact_ids_proposed":["<script>bad()</script>"],"observation":"SECRET_SENTINEL"},
         "provider":{"headers":{"Authorization":"SECRET_SENTINEL"},"raw_prompt":"SECRET_SENTINEL",
         "tool_calls":[{"name":"<script>bad()</script>","args":{"secret":"SECRET_SENTINEL"}}]},
         "court_result":{"status":"REVIEW_REQUIRED"}}
    html=_live_result_page({"case_id":"test","revision":1,"case_title":"Test"},run).body.decode()
    assert "SECRET_SENTINEL" not in html
    assert "<script>bad()</script>" not in html
    assert "&lt;script&gt;" in html
    assert "not accepted model support" in html
    assert "registered_fact_ids_proposed" in html
