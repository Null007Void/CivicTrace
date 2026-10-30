"""Bundled tutorial through actual HTML form routes; ASGI only, no browser/network."""
import base64
from pathlib import Path
import httpx,pytest
from civictrace.app import app,reset_case_store_cache
from test_workbench_ui import post_form

@pytest.mark.asyncio
async def test_bundled_first_case_forms_and_reopen(tmp_path,monkeypatch):
    monkeypatch.setenv('CIVICTRACE_CASE_STORE',str(tmp_path/'cases.json'))
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED','0')
    reset_case_store_cache()
    root=Path(__file__).resolve().parent.parent
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        create=await post_form(c,'/workbench/cases',{'case_title':'Bar-screen tutorial','project_name':'Wastewater mechanical bar screen','municipality':'Exeter, California','jurisdiction_id':'exeter-ca','publisher':'City of Exeter','allowed_hosts':'cityofexeter.ca.gov'})
        assert create.status_code==303,create.text
        cid=create.headers['location'].split('?')[0].rsplit('/',1)[-1];base='/workbench/cases/'+cid
        specs=[('01-bar-screen-council-minutes.txt','ADOPTED_ACTION_MINUTES','2026-07-28','AUTHORIZED_USE_CEILING','2026-07-28-I.10',{'amount_cents':'41452978','qualifiers':'UP_TO'},'DOCUMENTED_ACTION'),('02-bar-screen-project-tracker.txt','CITY_INFORMATIONAL_TRACKER','2026-10-23','CITY_REPORTS_PHASE','TRACKER-ROW-19',{'phase':'CONTRACT_AWARDED'},'CITY_REPORTED_STATUS')]
        for filename,role,date,predicate,action,extra,status in specs:
            raw=(root/'tutorial'/filename).read_bytes();text=raw.decode('utf-8')
            response=await post_form(c,base+'/sources',{'source_title':filename,'official_url':'https://cityofexeter.ca.gov/tutorial-reference','document_role':role,'document_date':date,'as_of':date,'source_anchor':'Tutorial selected excerpt','text_base64':base64.b64encode(raw).decode(),'filename':filename})
            assert response.status_code==303,response.text
            data=(await c.get('/api/cases/'+cid)).json()['case'];source=data['sources'][-1]
            assert source['exact_text']==text and source['raw_sha256'] is None
            response=await post_form(c,base+'/sources/'+source['source_id']+'/facts',{'anchor':'Tutorial selected excerpt','exact_excerpt':text,'predicate':predicate,'subject_id':'exeter-wastewater-mechanical-bar-screen','action_id':action,**extra})
            assert response.status_code==303,response.text
            data=(await c.get('/api/cases/'+cid)).json()['case'];fid=data['facts'][-1]['fact_id']
            response=await post_form(c,base+'/facts/'+fid+'/review',{'target':'HUMAN_REVIEWED','reviewer':'tutorial-test-reviewer'})
            assert response.status_code==303,response.text
            claim={'text':'Tutorial claim: '+predicate,'predicate':predicate,'subject_id':'exeter-wastewater-mechanical-bar-screen','action_id':action,'fact_ids':fid,'additional_record':'Tutorial record',**{k:v for k,v in extra.items() if k!='qualifiers'}}
            if predicate=='AUTHORIZED_USE_CEILING':claim['ceiling']='1'
            else:claim['as_of']=date
            response=await post_form(c,base+'/claims',claim)
            assert response.status_code==303,response.text
            claimid=response.headers['location'].rsplit('/',1)[-1]
            response=await post_form(c,base+'/claims/'+claimid+'/confirm',{'action':'confirm','attest':'yes','reviewer':'tutorial-test-reviewer'})
            assert response.status_code==303,response.text
        page=await c.get(base+'/investigate')
        assert 'DOCUMENTED_ACTION' in page.text and 'CITY_REPORTED_STATUS' in page.text
        assert 'LIVE_INFERENCE_NOT_OWNER_ENABLED' in page.text or 'not owner-enabled' in page.text
        revision=(await c.get('/api/cases/'+cid)).json()['case']['revision']
        reset_case_store_cache()
        assert (await c.get('/api/cases/'+cid)).json()['case']['revision']==revision
        page=await c.get(base+'/investigate')
        assert 'DOCUMENTED_ACTION' in page.text and 'CITY_REPORTED_STATUS' in page.text
    reset_case_store_cache()
