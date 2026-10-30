"""API and public-surface smoke; no provider calls or paid resource usage."""
import httpx
import pytest
import pytest_asyncio

from civictrace.app import app
from scripts.build_public_package import public_path
from scripts.freeze_candidate import collect

LIVE_GATE_VARS = ('CIVICTRACE_LIVE_ENABLED', 'CIVICTRACE_LIVE_CHECK_ACK', 'NEBIUS_API_KEY')


def _neutralise_live_gates(monkeypatch):
    """Keep fail-closed assertions hermetic when the runner exports live gates."""
    for name in LIVE_GATE_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        yield c


@pytest.mark.asyncio
async def test_static_case_loads(client):
    r=await client.get('/')
    assert r.status_code==200
    assert 'Challenge a claim' in r.text
    assert 'Enter Evidence Court' in r.text
    assert 'E Street' in r.text
    assert r.headers['content-security-policy'].startswith("default-src 'self'")
    assert 'unsafe-inline' not in r.headers['content-security-policy']


@pytest.mark.asyncio
async def test_court_two_real_source_adversarial_paths(client):
    r=await client.post('/api/challenge',json={'claim_id':'spent'})
    assert r.status_code==200
    assert r.json()['status']=='NOT_VERIFIED_FROM_SELECTED_RECORDS'
    r=await client.post('/api/challenge',json={'claim_id':'construction_from_event'})
    assert r.status_code==200
    assert r.json()['status']=='WRONG_ACTION_SOURCE'


@pytest.mark.asyncio
async def test_court_positive_controls(client):
    for cid in ['authorization','event','vote']:
        r=await client.post('/api/challenge',json={'claim_id':cid})
        assert r.json()['status']=='DOCUMENTED_ACTION'


@pytest.mark.asyncio
async def test_vote_5_0_reaches_documented_contradiction(client):
    """D1: the advertised 5–0 claim must reach CONTRADICTED_BY_RECORD over HTTP."""
    r=await client.post('/api/challenge',json={'claim_id':'vote_5_0'})
    assert r.status_code==200
    d=r.json()
    assert d['claim_id']=='vote_5_0'
    assert d['status']=='CONTRADICTED_BY_RECORD'
    assert 'DOCUMENTARY_VALUE_CONTRADICTION' in d['reason_codes']
    assert [citation['fact_id'] for citation in d['source_citations']]==['EX-VOTE']
    assert d['independently_audited'] is False
    assert d['finding_maturity']=='PRELIMINARY_UNAUDITED'


@pytest.mark.asyncio
async def test_documented_vote_pair_is_performable_from_shipped_surfaces(client):
    """D1: the 4–0 vs 5–0 pair must be offered by the UI and adjudicable by the API."""
    advertised={claim['id'] for claim in (await client.get('/api/case')).json()['claims']}
    assert {'vote','vote_5_0'}<=advertised
    page=(await client.get('/')).text
    assert 'data-claim="vote"' in page
    assert 'data-claim="vote_5_0"' in page
    supported=await client.post('/api/challenge',json={'claim_id':'vote'})
    contradicted=await client.post('/api/challenge',json={'claim_id':'vote_5_0'})
    assert supported.json()['status']=='DOCUMENTED_ACTION'
    assert contradicted.json()['status']=='CONTRADICTED_BY_RECORD'


@pytest.mark.asyncio
async def test_every_advertised_claim_is_http_reachable(client):
    """No curated claim may be advertised by /api/case while the API refuses it."""
    claims=[claim['id'] for claim in (await client.get('/api/case')).json()['claims']]
    assert claims
    unreachable={}
    for claim_id in claims:
        r=await client.post('/api/challenge',json={'claim_id':claim_id})
        if r.status_code!=200:
            unreachable[claim_id]=r.status_code
    assert unreachable=={}


@pytest.mark.parametrize('claim_id',[
    'VOTE_5_0','vote-5-0','5_0_vote',' vote_5_0','vote_5_0 ','vote 5_0',
    'vote/5/0','vote_5_0;drop table claims','a'*41,'',
])
@pytest.mark.asyncio
async def test_claim_id_validation_still_fails_closed(client,claim_id):
    r=await client.post('/api/challenge',json={'claim_id':claim_id})
    assert r.status_code==422
    assert 'claim_id' in r.text


@pytest.mark.asyncio
async def test_provenance_preliminary_not_overstated(client, monkeypatch):
    _neutralise_live_gates(monkeypatch)
    d=(await client.get('/api/case')).json()
    assert len(d['sources'])==2
    assert 'PENDING' in d['source_integrity_state']
    assert d['source_capture_complete'] is True
    assert all(source['raw_sha256'] for source in d['sources'])
    assert d['live_capability']['owner_enabled'] is False
    assert 'C:\\Users' not in str(d)
    assert 'UNRELATED_PRIVATE_PROJECT_MARKER' not in str(d)


@pytest.mark.asyncio
async def test_invalid_claim_and_unknown_url_are_not_tools(client):
    r=await client.post('/api/challenge',json={'claim_id':'file:///etc/passwd'})
    assert r.status_code==422
    r=await client.post('/api/challenge',json={'claim_id':'unknown'})
    assert r.status_code==404
    r=await client.post('/api/challenge',json={'claim_id':'spent','predicate':'AUTHORIZED_USE_CEILING'})
    assert r.status_code==422


@pytest.mark.asyncio
async def test_live_refuses_by_default(client, monkeypatch):
    _neutralise_live_gates(monkeypatch)
    r=await client.post('/api/investigate',json={'claim_id':'spent'})
    assert r.status_code==503
    assert r.json()['detail']['code']=='LIVE_INFERENCE_NOT_OWNER_ENABLED'
    assert 'No live Nebius inference performed' in r.text


@pytest.mark.asyncio
async def test_health_no_secrets(client, monkeypatch):
    _neutralise_live_gates(monkeypatch)
    d=(await client.get('/healthz')).json()
    assert d['ok'] is True
    assert d['live_inference_enabled'] is False
    assert 'api_key' not in str(d).lower()


@pytest.mark.asyncio
async def test_assets_self_hosted(client):
    r=await client.get('/assets/site.js')
    assert r.status_code==200
    assert 'Challenge' in r.text or 'challenge' in r.text
    r=await client.get('/assets/site.css')
    assert r.status_code==200
    assert '--cyan' in r.text
    r=await client.get('/assets/favicon.svg')
    assert r.status_code==200
    assert r.headers['content-type'].startswith('image/svg+xml')


def test_candidate_evidence_records_are_not_public_package_members():
    assert public_path('README.md')
    assert not public_path('evidence/candidates/civictrace-rc1.json')


def test_warning_policy_is_bound_in_candidate_manifest():
    assert 'pytest.ini' in collect()['behavior']
