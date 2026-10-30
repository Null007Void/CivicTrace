"""CT-WB2 workbench UI tests: synthetic fixtures only, fully offline.

No provider call, no URL fetch, no paid resource. The fixture municipality and records are
fictional, the hostnames use the reserved ``.invalid`` TLD, and nothing here is real evidence.
The rehearsal test drives the scripted acceptance run in ``scripts/workbench_second_case_rehearsal``
against a throwaway store, so the UI workflow itself is exercised end to end on every suite run.
"""
import base64
import hashlib
import urllib.parse

import httpx
import pytest
import pytest_asyncio

from civictrace import app as app_module
from civictrace import workbench_ui
from civictrace.app import app, reset_case_store_cache
from civictrace.case_store import CaseStore, case_claim_book, case_registry
from civictrace.court import Court, CourtValidationError
from scripts.workbench_second_case_rehearsal import (
    SECOND_CASE,
    NoNetwork,
    run_rehearsal,
)

#: A workbench form post is a browser write, so it declares the origin it was served from and the
#: same-origin fetch metadata the app's write guard requires (httpx adds neither on its own).
FORM_HEADERS = {'content-type': 'application/x-www-form-urlencoded',
                'Origin': 'http://test', 'Sec-Fetch-Site': 'same-origin'}


def _encode(pairs) -> bytes:
    return urllib.parse.urlencode(pairs, doseq=True).encode('utf-8')


async def post_form(client: httpx.AsyncClient, path: str, pairs) -> httpx.Response:
    return await client.post(path, content=_encode(pairs), headers=FORM_HEADERS)


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_CASE_STORE', str(tmp_path / 'cases.json'))
    reset_case_store_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test', follow_redirects=False) as async_client:
        yield async_client
    reset_case_store_cache()


CASE_FIELDS = (
    ('case_title', SECOND_CASE['case']['case_title']),
    ('project_name', SECOND_CASE['case']['project_name']),
    ('municipality', SECOND_CASE['case']['municipality']),
    ('jurisdiction_id', SECOND_CASE['case']['jurisdiction_id']),
    ('publisher', SECOND_CASE['case']['publisher']),
    ('allowed_hosts', SECOND_CASE['case']['allowed_hosts']),
    ('provenance_note', SECOND_CASE['case']['provenance_note']),
)


def _source_fields(source: dict, *, payload: str = 'text') -> list:
    fields = [
        ('source_title', source['title']),
        ('official_url', source['official_url']),
        ('document_role', source['document_role']),
        ('document_date', source['document_date']),
        ('as_of', source['as_of']),
        ('source_anchor', source['anchor']),
        ('text', ''),
        ('text_base64', ''),
        ('filename', ''),
        ('source_file', ''),
    ]
    if payload == 'text':
        fields = [(key, source['text'] if key == 'text' else value) for key, value in fields]
    else:
        fields.append(('text_base64', base64.b64encode(
            source['text'].encode('utf-8')).decode('ascii')))
        fields.append(('filename', source['filename']))
    return fields


async def seed_case(client: httpx.AsyncClient):
    """Create the synthetic second project through the UI forms; return its ids."""
    minutes = SECOND_CASE['sources']['minutes']
    tracker = SECOND_CASE['sources']['tracker']
    form = list(CASE_FIELDS) + [('include_source', '1')]
    form += _source_fields(minutes, payload='upload')
    created = await post_form(client, '/workbench/cases', form)
    assert created.status_code == 303, created.text
    case_id = created.headers['location'].split('?')[0].rsplit('/', 1)[-1]
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    source_ids = {'minutes': detail['sources'][0]['source_id']}
    added = await post_form(client, f'/workbench/cases/{case_id}/sources',
                            _source_fields(tracker, payload='text'))
    assert added.status_code == 303, added.text
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    source_ids['tracker'] = detail['sources'][1]['source_id']
    fact_ids = {}
    for key, fact in SECOND_CASE['facts'].items():
        form = [
            ('anchor', fact['anchor']),
            ('exact_excerpt', fact['exact_excerpt']),
            ('predicate', fact['predicate']),
            ('subject_id', fact['subject_id']),
            ('action_id', fact['action_id']),
        ]
        if fact.get('qualifiers'):
            form.append(('qualifiers', ','.join(fact['qualifiers'])))
        for name in ('amount_cents', 'phase', 'event_date'):
            if name in fact:
                form.append((name, str(fact[name])))
        response = await post_form(
            client, f'/workbench/cases/{case_id}/sources/{source_ids[fact["source_key"]]}/facts',
            form)
        assert response.status_code == 303, response.text
        fact_ids[key] = response.headers['location'].split('created_fact=')[1]
    claim_ids = {}
    for key, claim in SECOND_CASE['claims'].items():
        form = [
            ('text', claim['text']),
            ('predicate', claim['predicate']),
            ('subject_id', claim['subject_id']),
            ('action_id', claim['action_id']),
            ('additional_record', claim['additional_record']),
        ]
        for fact_key in claim['fact_keys']:
            form.append(('fact_ids', fact_ids[fact_key]))
        for name in ('amount_cents', 'as_of', 'phase'):
            if name in claim:
                form.append((name, str(claim[name])))
        if claim.get('ceiling'):
            form.append(('ceiling', '1'))
        response = await post_form(client, f'/workbench/cases/{case_id}/claims', form)
        assert response.status_code == 303, response.text
        claim_ids[key] = response.headers['location'].rsplit('/', 1)[-1]
    return case_id, source_ids, fact_ids, claim_ids


async def review_all(client: httpx.AsyncClient, case_id: str, fact_ids) -> None:
    for fact_id in fact_ids.values():
        response = await post_form(client, f'/workbench/cases/{case_id}/facts/{fact_id}/review',
                                   [('target', 'HUMAN_REVIEWED'),
                                    ('reviewer', 'synthetic-reviewer')])
        assert response.status_code == 303, response.text


# -- pages: inventory, typed states, provenance ------------------------------------------------

@pytest.mark.asyncio
async def test_workbench_index_is_typed_and_offers_every_create_field(client, tmp_path):
    page = await client.get('/workbench')
    assert page.status_code == 200
    assert page.headers['content-type'].startswith('text/html')
    assert 'NO_CASES_YET' in page.text
    assert 'No cases have been created in this local store yet.' in page.text
    assert not (tmp_path / 'cases.json').exists()  # a read never writes a store file
    for field in ('case_title', 'project_name', 'municipality', 'jurisdiction_id', 'publisher',
                  'allowed_hosts', 'source_title', 'official_url', 'document_role',
                  'document_date', 'as_of', 'source_anchor'):
        assert f'name="{field}"' in page.text
    assert 'name="text"' in page.text and '<textarea' in page.text
    assert 'type="file"' in page.text and 'accept=".txt' in page.text
    assert 'name="text_base64"' in page.text and 'name="filename"' in page.text
    assert 'never fetched' in page.text
    assert 'ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED' in page.text


@pytest.mark.asyncio
async def test_txt_upload_then_source_review_page_shows_hash_provenance(client):
    case_id, sources, _, _ = await seed_case(client)
    minutes = SECOND_CASE['sources']['minutes']
    expected = hashlib.sha256(minutes['text'].encode('utf-8')).hexdigest()

    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    source = detail['sources'][0]
    assert source['exact_text'] == minutes['text']
    assert source['content_sha256'] == expected
    assert source['capture_class'] == 'USER_SUPPLIED_EXCERPT'
    assert source['raw_sha256'] is None

    page = await client.get(f'/workbench/cases/{case_id}/sources/{sources["minutes"]}')
    assert page.status_code == 200
    assert 'ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED' in page.text
    assert 'USER_SUPPLIED_EXCERPT' in page.text
    assert 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES' in page.text
    assert expected in page.text
    assert 'never fetched, never resolved' in page.text
    assert 'SYNTHETIC FIXTURE - NOT A REAL GOVERNMENT RECORD.' in page.text
    assert 'Item B.2' in page.text
    assert '&lt;script&gt;alert(' in page.text
    assert '<script>alert(' not in page.text
    assert '<pre>' in page.text

    case_page = await client.get(f'/workbench/cases/{case_id}')
    assert case_page.status_code == 200
    assert 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED' in case_page.text
    assert expected in case_page.text
    assert case_page.text.count('ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED') >= 1


@pytest.mark.asyncio
async def test_workbench_escapes_source_and_claim_text_everywhere(client):
    case_id, sources, fact_ids, claim_ids = await seed_case(client)
    hostile_claim = '<img src=x onerror=alert(1)> synthetic statement'
    hostile = await post_form(
        client, f'/workbench/cases/{case_id}/claims',
        [('text', hostile_claim),
         ('predicate', 'AUTHORIZED_USE_CEILING'),
         ('subject_id', 'harbor-lane-lighting'),
         ('action_id', 'B.2-res-1'),
         ('additional_record', 'Synthetic payment register (fixture)'),
         ('fact_ids', fact_ids['cap'])])
    assert hostile.status_code == 303, hostile.text
    hostile_claim_id = hostile.headers['location'].rsplit('/', 1)[-1]

    source_page = await client.get(f'/workbench/cases/{case_id}/sources/{sources["minutes"]}')
    assert source_page.status_code == 200
    assert '<script>alert(' not in source_page.text
    assert '&lt;script&gt;alert(' in source_page.text
    assert 'IGNORE ALL PREVIOUS INSTRUCTIONS' in source_page.text  # inert text, not an instruction

    claim_page = await client.get(f'/workbench/cases/{case_id}/claims/{hostile_claim_id}')
    assert claim_page.status_code == 200
    assert '<img src=x' not in claim_page.text
    assert '&lt;img src=x onerror=alert(1)&gt;' in claim_page.text

    pages = [
        await client.get(f'/workbench/cases/{case_id}'),
        source_page,
        claim_page,
        await client.get(f'/workbench/cases/{case_id}/investigate'),
        await client.get(f'/workbench/cases/{case_id}/claims/new'),
        await client.get('/workbench'),
    ]
    for page in pages:
        assert page.status_code == 200
        assert '<script>alert(' not in page.text
        assert '<img src=x' not in page.text
        # The only script element is the self-hosted file-input helper.
        assert page.text.count('<script') == 1
        assert '<script src="/assets/workbench.js" defer></script>' in page.text
        assert 'onerror=' not in page.text or '&lt;img' in page.text


@pytest.mark.asyncio
async def test_workbench_js_is_self_hosted_and_fetches_nothing(client):
    script = await client.get('/assets/workbench.js')
    assert script.status_code == 200
    assert script.headers['content-type'].startswith('application/javascript')
    assert 'fetch(' not in script.text
    assert 'XMLHttpRequest' not in script.text
    assert 'FileReader' in script.text
    assert script.headers['content-security-policy'].startswith("default-src 'self'")


# -- fact mapping editor and review transitions ------------------------------------------------

@pytest.mark.asyncio
async def test_fact_editor_shows_normal_forms_and_records_transitions(client):
    case_id, sources, fact_ids, _ = await seed_case(client)
    page = await client.get(f'/workbench/cases/{case_id}/facts/{fact_ids["cap"]}')
    assert page.status_code == 200
    assert 'NFKC_CASEFOLD_WHITESPACE_COLLAPSE' in page.text
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    fact = next(item for item in detail['facts'] if item['fact_id'] == fact_ids['cap'])
    assert fact['normal_forms']['normalized_excerpt'] in page.text
    assert fact['normal_forms']['normalized_sha256'] in page.text
    assert 'PENDING_HUMAN_REVIEW' in page.text

    no_reviewer = await post_form(client, f'/workbench/cases/{case_id}/facts/{fact_ids["cap"]}/review',
                                  [('target', 'HUMAN_REVIEWED')])
    assert no_reviewer.status_code == 422
    assert 'REVIEWER_REQUIRED' in no_reviewer.text

    unsettable = await post_form(client, f'/workbench/cases/{case_id}/facts/{fact_ids["cap"]}/review',
                                 [('target', 'SOURCE_REVISED_REVIEW_REQUIRED'),
                                  ('reviewer', 'synthetic-reviewer')])
    assert unsettable.status_code == 422
    assert 'REVIEW_STATE_NOT_SETTABLE' in unsettable.text

    reviewed = await post_form(client, f'/workbench/cases/{case_id}/facts/{fact_ids["cap"]}/review',
                               [('target', 'HUMAN_REVIEWED'), ('reviewer', 'synthetic-reviewer')])
    assert reviewed.status_code == 303
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    fact = next(item for item in detail['facts'] if item['fact_id'] == fact_ids['cap'])
    assert fact['review_state'] == 'HUMAN_REVIEWED'
    assert fact['reviewed_by'] == 'synthetic-reviewer'

    withdrawn = await post_form(client, f'/workbench/cases/{case_id}/facts/{fact_ids["cap"]}/review',
                                [('target', 'PENDING_HUMAN_REVIEW')])
    assert withdrawn.status_code == 303
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    fact = next(item for item in detail['facts'] if item['fact_id'] == fact_ids['cap'])
    assert fact['review_state'] == 'PENDING_HUMAN_REVIEW'


@pytest.mark.asyncio
async def test_claim_template_carries_plain_language_confirmation_question(client):
    case_id, _, fact_ids, _ = await seed_case(client)
    page = await client.get(f'/workbench/cases/{case_id}/claims/new?template=PAID_AMOUNT')
    assert page.status_code == 200
    assert 'Does this excerpt come from a payment register and record money actually paid?' in page.text
    assert 'An authorized ceiling is never evidence of expenditure.' in page.text
    assert 'required document role for PAID_AMOUNT: OFFICIAL_PAYMENT_REGISTER' in page.text
    for fact_id in fact_ids.values():
        assert f'value="{fact_id}"' in page.text

    claim = await post_form(client, f'/workbench/cases/{case_id}/claims',
                            [('text', 'The synthetic municipality paid $4,200.'),
                             ('predicate', 'PAID_AMOUNT'),
                             ('subject_id', 'harbor-lane-lighting'),
                             ('action_id', 'B.2-res-1'),
                             ('additional_record', 'Synthetic paid invoices (fixture)'),
                             ('fact_ids', fact_ids['cap'])])
    assert claim.status_code == 303
    claim_id = claim.headers['location'].rsplit('/', 1)[-1]
    claim_page = await client.get(f'/workbench/cases/{case_id}/claims/{claim_id}')
    assert claim_page.status_code == 200
    assert 'Confirmation is a human review action, not a finding of truth.' in claim_page.text
    assert 'I have read the exact excerpts and the structured fields and they match.' in claim_page.text
    assert 'REVIEW_REQUIRED' not in claim_page.text


# -- investigation page: dynamic citations and provenance labels --------------------------------

@pytest.mark.asyncio
async def test_investigation_page_renders_only_case_citations_with_provenance(client):
    case_id, sources, fact_ids, claim_ids = await seed_case(client)
    await review_all(client, case_id, fact_ids)
    confirmed = await post_form(
        client, f'/workbench/cases/{case_id}/claims/{claim_ids["authorization"]}/confirm',
        [('action', 'confirm'), ('attest', 'yes'), ('reviewer', 'synthetic-reviewer')])
    assert confirmed.status_code == 303

    page = await client.get(f'/workbench/cases/{case_id}/investigate')
    assert page.status_code == 200
    assert 'DOCUMENTED_ACTION' in page.text
    assert 'CITY_REPORTED_STATUS' in page.text
    assert 'CEILING_NOT_PAYMENT' in page.text
    assert 'TRACKER_REPORT_NOT_PHYSICAL_VERIFICATION' in page.text
    minutes_hash = hashlib.sha256(
        SECOND_CASE['sources']['minutes']['text'].encode('utf-8')).hexdigest()
    assert minutes_hash in page.text
    assert fact_ids['cap'] in page.text
    assert 'Item B.2' in page.text
    assert 'provenance_label=USER_SUPPLIED_EXCERPT__ORIGINAL_BYTES_UNVERIFIED' in page.text
    assert 'hash_subject=IMPORTED_TEXT_NOT_ORIGINAL_BYTES' in page.text
    assert 'original_source_verified=false' in page.text
    assert 'independent_fact_audit=PENDING' in page.text
    assert page.text.count('ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED') >= len(claim_ids)

    # The run-it form is part of the page and offers the observed-evidence probes.
    assert 'name="evidence_mode"' in page.text
    assert 'value="unseen"' in page.text


# -- typed refusals through the HTML surface ----------------------------------------------------

@pytest.mark.asyncio
async def test_ui_refusals_are_typed_html_not_tracebacks(client):
    case_id, sources, fact_ids, _ = await seed_case(client)

    missing = await post_form(client, '/workbench/cases', [('case_title', 'only a title')])
    assert missing.status_code == 422
    assert missing.headers['content-type'].startswith('text/html')
    assert 'INVALID_INPUT' in missing.text

    non_txt = list(CASE_FIELDS) + [('include_source', '1')]
    non_txt += _source_fields(SECOND_CASE['sources']['minutes'], payload='upload')
    non_txt = [(key, 'notes.pdf' if key == 'filename' else value) for key, value in non_txt]
    refused = await post_form(client, '/workbench/cases', non_txt)
    assert refused.status_code == 422
    assert 'INVALID_INPUT' in refused.text
    assert '.txt filename' in refused.text

    bad_utf8 = await client.post('/workbench/cases', content=b'\xff\xfe\x00bad',
                                 headers=FORM_HEADERS)
    assert bad_utf8.status_code == 422
    assert 'INVALID_UTF8' in bad_utf8.text

    picked = await post_form(
        client, f'/workbench/cases/{case_id}/sources',
        [(key, value) for key, value in _source_fields(
            SECOND_CASE['sources']['tracker'], payload='text')
         if key not in ('text', 'text_base64', 'filename', 'source_file')]
        + [('source_file', 'notes.txt')])
    assert picked.status_code == 422
    assert 'UPLOAD_NEEDS_SCRIPT' in picked.text

    oversized = list(CASE_FIELDS) + [('include_source', '1'),
                                     ('source_title', 'oversized'),
                                     ('official_url', SECOND_CASE['sources']['minutes']['official_url']),
                                     ('document_role', 'ADOPTED_ACTION_MINUTES'),
                                     ('document_date', '2026-06-01'),
                                     ('as_of', '2026-06-01'),
                                     ('source_anchor', 'Item B.2'),
                                     ('text', 'x' * (workbench_ui.MAX_TEXT_BYTES + 1))]
    too_large = await post_form(client, '/workbench/cases', oversized)
    assert too_large.status_code == 413
    assert 'SOURCE_TEXT_TOO_LARGE' in too_large.text

    # A compensation path: no case leaked from the refused creations.
    listing = (await client.get('/api/cases')).json()
    assert len(listing['cases']) == 1
    detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
    assert len(detail['sources']) == 2


@pytest.mark.asyncio
async def test_ui_wrong_case_and_unknown_ids_are_typed(client):
    case_id, sources, fact_ids, claim_ids = await seed_case(client)
    other = await post_form(client, '/workbench/cases', list(CASE_FIELDS))
    assert other.status_code == 303
    other_id = other.headers['location'].rsplit('/', 1)[-1]

    foreign_source = await client.get(f'/workbench/cases/{other_id}/sources/{sources["minutes"]}')
    assert foreign_source.status_code == 404
    assert 'SOURCE_NOT_IN_CASE' in foreign_source.text
    foreign_fact = await client.get(f'/workbench/cases/{other_id}/facts/{fact_ids["cap"]}')
    assert foreign_fact.status_code == 404
    assert 'FACT_NOT_IN_CASE' in foreign_fact.text
    foreign_claim = await post_form(client, f'/workbench/cases/{other_id}/investigate',
                                    [('claim_id', claim_ids['authorization'])])
    assert foreign_claim.status_code == 404
    assert 'CLAIM_NOT_IN_CASE' in foreign_claim.text
    unknown = await client.get('/workbench/cases/case_0000000000000000')
    assert unknown.status_code == 404
    assert 'CASE_NOT_FOUND' in unknown.text

    # The other case never sees case 1's records on its own page.
    other_page = await client.get(f'/workbench/cases/{other_id}')
    assert other_page.status_code == 200
    assert sources['minutes'] not in other_page.text
    assert fact_ids['cap'] not in other_page.text


@pytest.mark.asyncio
async def test_ui_entering_a_url_never_touches_the_network(client):
    with NoNetwork() as guard:
        created = await post_form(client, '/workbench/cases',
                                  list(CASE_FIELDS) + [('include_source', '1')]
                                  + _source_fields(SECOND_CASE['sources']['minutes'],
                                                   payload='text'))
        assert created.status_code == 303
        case_id = created.headers['location'].split('?')[0].rsplit('/', 1)[-1]
        pages = [await client.get('/workbench'),
                 await client.get(f'/workbench/cases/{case_id}'),
                 await client.get(f'/workbench/cases/{case_id}/investigate')]
    assert [page.status_code for page in pages] == [200, 200, 200]
    assert guard.attempts == []


# -- court-level guard that the UI cannot reach ------------------------------------------------

def test_court_rejects_foreign_jurisdiction_record(tmp_path):
    """A fact stamped with another jurisdiction cannot be adjudicated in this case.

    The store stamps the case jurisdiction on every record, so the HTTP surface cannot build this
    shape; the Court guard itself is still proven here on a synthetic in-memory registry.
    """
    store = CaseStore(tmp_path / 'cases.json')
    case = store.create_case(project_name='SYNTHETIC decoy',
                             municipality=SECOND_CASE['case']['municipality'],
                             case_title='SYNTHETIC jurisdiction guard',
                             jurisdiction_id=SECOND_CASE['case']['jurisdiction_id'],
                             publisher=SECOND_CASE['case']['publisher'],
                             allowed_hosts=[SECOND_CASE['case']['allowed_hosts']])
    source = store.add_source(case['case_id'],
                              title=SECOND_CASE['sources']['minutes']['title'],
                              publisher=SECOND_CASE['case']['publisher'],
                              official_url=SECOND_CASE['sources']['minutes']['official_url'],
                              document_role='ADOPTED_ACTION_MINUTES',
                              document_date='2026-06-01', as_of='2026-06-01',
                              anchor='Item B.2',
                              text=SECOND_CASE['sources']['minutes']['text'])
    fact = store.add_fact(case['case_id'], source_id=source['source_id'], anchor='Item B.2',
                          exact_excerpt=SECOND_CASE['facts']['cap']['exact_excerpt'],
                          predicate='AUTHORIZED_USE_CEILING',
                          subject_id='harbor-lane-lighting', action_id='B.2-res-1',
                          qualifiers=['UP_TO'])
    stored = store.get_case(case['case_id'])
    registry = case_registry(stored)
    claims = case_claim_book(stored)
    Court(registry, claims)  # the unmodified projection is valid

    registry = case_registry(stored)
    registry['facts'][0]['jurisdiction_id'] = 'some-other-jurisdiction'
    with pytest.raises(CourtValidationError) as foreign:
        Court(registry, claims)
    assert foreign.value.code == 'WRONG_JURISDICTION'
    assert fact['fact_id'] == registry['facts'][0]['fact_id']
    assert source['source_id'] == registry['sources'][0]['source_id']


# -- rehearsal script and bound parity ---------------------------------------------------------

def test_form_bound_matches_the_shared_request_bound():
    assert workbench_ui.MAX_FORM_BYTES == app_module.MAX_REQUEST_BYTES


@pytest.mark.asyncio
async def test_offline_second_case_rehearsal_passes_every_check():
    evidence = await run_rehearsal()
    failed = [item['name'] for item in evidence['checks'] if not item['ok']]
    assert failed == [], failed
    assert evidence['verdict'] == 'PASS'
    assert len(evidence['checks']) >= 40
    assert evidence['network_attempts'] == 0
    statuses = {key: value['status'] for key, value in evidence['results'].items()}
    assert statuses['authorization'] == 'DOCUMENTED_ACTION'
    assert statuses['spent'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert statuses['construction_from_event'] == 'WRONG_ACTION_SOURCE'
    assert statuses['completed'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert statuses['phase'] == 'CITY_REPORTED_STATUS'
    names = {item['name'] for item in evidence['checks']}
    for required in ('distinction_authorization_is_not_payment',
                     'distinction_unrelated_event_is_not_construction',
                     'distinction_tracker_report_is_not_physical_completion',
                     'wrong_case_records_refused',
                     'unseen_citation_rejected_in_ui',
                     'malformed_and_oversized_input_fails_visibly',
                     'restart_reopen_preserves_bytes_revision_and_decisions',
                     'no_outbound_network_attempt'):
        assert required in names
