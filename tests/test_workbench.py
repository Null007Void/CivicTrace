"""CT-WB1 workbench tests: synthetic fixture only, fully offline.

No provider call, no URL fetch, no paid resource. The fixture municipality and records are
fictional and the fixture domain is the reserved ``.invalid`` TLD; nothing here is real evidence.
"""
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from civictrace.app import app, reset_case_store_cache
from civictrace.case_store import (
    MAX_TEXT_BYTES,
    CaseStore,
    CaseStoreError,
    case_claim_book,
    case_registry,
    case_state,
)
from civictrace.court import Court, CourtValidationError

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'synthetic_case.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
CASE_FIELDS = ('project_name', 'municipality', 'case_title', 'jurisdiction_id',
               'publisher', 'allowed_hosts', 'provenance_note')


def seed_case(store: CaseStore):
    """Create the synthetic case through the public store API; return generated ids."""
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    sources = {}
    for source in FIXTURE['sources']:
        record = store.add_source(
            case['case_id'], title=source['title'], publisher=source['publisher'],
            official_url=source['official_url'], document_role=source['document_role'],
            document_date=source['document_date'], as_of=source['as_of'],
            anchor=source['anchor'], text=source['text'])
        sources[source['key']] = record['source_id']
    facts = {}
    for fact in FIXTURE['facts']:
        payload = {key: value for key, value in fact.items() if key not in ('key', 'source')}
        record = store.add_fact(case['case_id'], source_id=sources[fact['source']], **payload)
        facts[fact['key']] = record['fact_id']
    claims = {}
    for claim in FIXTURE['claims']:
        payload = {key: value for key, value in claim.items() if key not in ('key', 'fact_keys')}
        record = store.add_claim(case['case_id'],
                                 fact_ids=[facts[key] for key in claim['fact_keys']], **payload)
        claims[claim['key']] = record['claim_id']
    return case['case_id'], sources, facts, claims


def review(store: CaseStore, case_id: str, fact_ids) -> None:
    for fact_id in fact_ids:
        store.update_fact(case_id, fact_id, {'review_state': 'HUMAN_REVIEWED'})


def api_case_payload(source_text=None):
    payload = {key: FIXTURE['case'][key] for key in CASE_FIELDS}
    if source_text is not None:
        source = FIXTURE['sources'][0]
        payload['source'] = {
            'title': source['title'], 'publisher': source['publisher'],
            'official_url': source['official_url'], 'document_role': source['document_role'],
            'document_date': source['document_date'], 'as_of': source['as_of'],
            'anchor': source['anchor'], 'text': source_text,
        }
    return payload


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_CASE_STORE', str(tmp_path / 'cases.json'))
    reset_case_store_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as async_client:
        yield async_client
    reset_case_store_cache()


# -- store: durability, ids, draft states -------------------------------------------------

def test_empty_store_is_typed_and_writes_nothing(tmp_path):
    path = tmp_path / 'cases.json'
    store = CaseStore(path)
    listing = store.list_cases()
    assert listing['state'] == 'NO_CASES_YET'
    assert listing['cases'] == []
    assert listing['message']
    assert not path.exists()


def test_create_and_reopen_persists_atomically(tmp_path):
    path = tmp_path / 'cases.json'
    store = CaseStore(path)
    case_id, sources, facts, claims = seed_case(store)
    assert path.exists()
    document = json.loads(path.read_text(encoding='utf-8'))
    assert document['schema_version'] == 1
    assert case_id in document['cases']
    assert not list(tmp_path.glob('*.tmp-*'))
    assert store.get_case(case_id)['revision'] == 9  # create + 2 sources + 3 facts + 3 claims
    assert all(source_id.startswith('src_') for source_id in sources.values())
    assert all(fact_id.startswith('fact_') for fact_id in facts.values())
    assert all(claim_id.startswith('claim_') for claim_id in claims.values())
    reopened = CaseStore(path)
    assert reopened.get_case(case_id) == store.get_case(case_id)
    assert reopened.store_revision() == store.store_revision()
    assert reopened.list_cases()['state'] == 'CASES_PRESENT'


def test_restart_then_case_scoped_investigation(tmp_path):
    path = tmp_path / 'cases.json'
    store = CaseStore(path)
    case_id, sources, facts, claims = seed_case(store)
    review(store, case_id, list(facts.values()))
    reopened = CaseStore(path)
    court = Court.from_case_store(reopened, case_id)

    authorization = court.evaluate(claims['authorization'])
    assert authorization.status == 'DOCUMENTED_ACTION'
    assert authorization.source_capture_complete is False
    assert authorization.source_integrity_state == 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED'
    assert authorization.original_source_verified is False
    assert authorization.independent_fact_audit == 'PENDING'
    citation = authorization.source_citations[0]
    assert citation['capture_class'] == 'USER_SUPPLIED_EXCERPT'
    assert citation['hash_subject'] == 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'
    assert citation['raw_sha256'] is None
    assert citation['content_sha256'] == hashlib.sha256(
        FIXTURE['sources'][0]['text'].encode('utf-8')).hexdigest()

    assert court.evaluate(claims['spent']).status == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert 'CEILING_NOT_PAYMENT' in court.evaluate(claims['spent']).reason_codes
    status = court.evaluate(claims['status'])
    assert status.status == 'CITY_REPORTED_STATUS'
    assert 'CITY_REPORT_ONLY_NOT_FIELD_INSPECTION' in status.reason_codes


def test_draft_case_is_reported_honestly(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    assert case_state(case) == 'DRAFT_NO_SOURCES'
    court = Court.from_case_store(store, case['case_id'])
    assert court.integrity_state == 'NO_SOURCES_RECORDED'
    assert court.capture_complete is False
    assert court.claims == {}
    with pytest.raises(CourtValidationError):
        court.evaluate('anything')


def test_unreviewed_mapping_cannot_support_claim(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, _, claims = seed_case(store)
    court = Court.from_case_store(store, case_id)
    decision = court.evaluate(claims['authorization'])
    assert decision.status == 'REVIEW_REQUIRED'
    assert 'UNREVIEWED_FACT_MAPPING' in decision.reason_codes


def test_unseen_citation_is_rejected(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, facts, claims = seed_case(store)
    review(store, case_id, list(facts.values()))
    court = Court.from_case_store(store, case_id)
    decision = court.evaluate(claims['authorization'], proposed_fact_ids=[facts['cap']],
                              observed_fact_ids=set())
    assert decision.status == 'REVIEW_REQUIRED'
    assert 'UNSEEN_SOURCE_CITATION' in decision.reason_codes


# -- store: revision invalidation ---------------------------------------------------------

def test_confirmation_requires_review_and_fact_edit_invalidates(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, facts, claims = seed_case(store)
    with pytest.raises(CaseStoreError) as blocked:
        store.confirm_claim(case_id, claims['authorization'])
    assert blocked.value.code == 'CLAIM_EVIDENCE_NOT_REVIEWED'

    review(store, case_id, [facts['cap']])
    confirmed = store.confirm_claim(case_id, claims['authorization'], reviewer='synthetic-reviewer')
    assert confirmed['confirmation']['state'] == 'CONFIRMED'
    assert confirmed['confirmation']['evidence_sha256']
    revision_before = store.get_case(case_id)['revision']

    changed = store.update_fact(case_id, facts['cap'],
                                {'exact_excerpt': 'approved up to $1,000 in synthetic test funds'})
    assert changed['review_state'] == 'SOURCE_REVISED_REVIEW_REQUIRED'
    case = store.get_case(case_id)
    assert case['revision'] > revision_before
    assert case['claims'][0]['confirmation']['state'] == 'INVALIDATED_STALE'
    assert case['claims'][0]['confirmation']['invalidation_reason'] == 'FACT_MAPPING_REVISED'
    entry = store.get_revision(case_id, case['revision'])
    assert entry['event'] == 'FACT_UPDATED'
    assert claims['authorization'] in entry['detail']['claims_invalidated']
    events = [item['event'] for item in case['revisions']]
    assert 'CLAIM_CONFIRMED' in events and 'FACT_UPDATED' in events


def test_source_edit_invalidates_dependent_claims(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, sources, facts, claims = seed_case(store)
    review(store, case_id, list(facts.values()))
    store.confirm_claim(case_id, claims['authorization'])
    store.update_source(case_id, sources['minutes'],
                        {'exact_text': FIXTURE['sources'][0]['text'] + 'SYNTHETIC AMENDMENT.\n'})
    case = store.get_case(case_id)
    assert all(fact['review_state'] == 'SOURCE_REVISED_REVIEW_REQUIRED'
               for fact in case['facts'] if fact['source_id'] == sources['minutes'])
    assert case['claims'][0]['confirmation']['state'] == 'INVALIDATED_STALE'
    assert case['claims'][0]['confirmation']['invalidation_reason'] == 'SOURCE_REVISED'


def test_source_semantics_locked_while_facts_cite_it(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, sources, _, _ = seed_case(store)
    with pytest.raises(CaseStoreError) as locked:
        store.update_source(case_id, sources['minutes'], {'as_of': '2026-03-03'})
    assert locked.value.code == 'SOURCE_SEMANTICS_LOCKED_BY_FACTS'


# -- store: wrong-case isolation ----------------------------------------------------------

def test_wrong_case_source_and_fact_rejected(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_a, sources_a, facts_a, claims_a = seed_case(store)
    case_b = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})['case_id']
    with pytest.raises(CaseStoreError) as wrong:
        store.add_fact(case_b, source_id=sources_a['minutes'], anchor='x',
                       exact_excerpt='anything', predicate='AUTHORIZED_USE_CEILING',
                       subject_id='synthetic-sidewalk', action_id='A.1-res-1')
    assert wrong.value.code == 'SOURCE_NOT_IN_CASE'
    with pytest.raises(CaseStoreError) as claim_wrong:
        store.add_claim(case_b, text='x', predicate='AUTHORIZED_USE_CEILING',
                        subject_id='synthetic-sidewalk', action_id='A.1-res-1',
                        fact_ids=[facts_a['cap']], additional_record='x')
    assert claim_wrong.value.code == 'FACT_NOT_IN_CASE'

    registry = case_registry(store.get_case(case_a))
    registry['sources'][0]['case_id'] = case_b
    with pytest.raises(CourtValidationError) as source_case:
        Court(registry, case_claim_book(store.get_case(case_a)))
    assert source_case.value.code == 'WRONG_CASE_SOURCE'

    registry = case_registry(store.get_case(case_a))
    registry['facts'][0]['case_id'] = case_b
    with pytest.raises(CourtValidationError) as fact_case:
        Court(registry, case_claim_book(store.get_case(case_a)))
    assert fact_case.value.code == 'WRONG_CASE_FACT'

    claims = case_claim_book(store.get_case(case_a))
    claims['claims'][0]['fact_ids'] = [claims_a['authorization']]  # a claim id is not a fact id
    with pytest.raises(CourtValidationError) as bad_ref:
        Court(case_registry(store.get_case(case_a)), claims)
    assert bad_ref.value.code == 'WRONG_CASE_FACT'


# -- court: case-bound publisher policy and imported text honesty ------------------------

def test_case_bound_publisher_policy(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, _, _ = seed_case(store)
    base = store.get_case(case_id)

    registry = case_registry(base)
    registry.pop('publisher_policy')
    with pytest.raises(CourtValidationError) as missing:
        Court(registry, case_claim_book(base))
    assert missing.value.code == 'MISSING_PUBLISHER_POLICY'

    registry = case_registry(base)
    registry['sources'][0]['official_url'] = 'https://other.example.invalid/x.txt'
    with pytest.raises(CourtValidationError) as host:
        Court(registry, case_claim_book(base))
    assert host.value.code == 'SOURCE_URL_NOT_ALLOWLISTED'

    registry = case_registry(base)
    registry['sources'][0]['publisher'] = 'Another Publisher'
    with pytest.raises(CourtValidationError) as publisher:
        Court(registry, case_claim_book(base))
    assert publisher.value.code == 'WRONG_PUBLISHER'


def test_imported_text_cannot_be_promoted_to_original_bytes(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, _, _ = seed_case(store)
    base = store.get_case(case_id)

    registry = case_registry(base)
    registry['sources'][0]['raw_sha256'] = 'a' * 64
    with pytest.raises(CourtValidationError) as promoted:
        Court(registry, case_claim_book(base))
    assert promoted.value.code == 'IMPORTED_TEXT_CANNOT_CARRY_ORIGINAL_HASH'

    registry = case_registry(base)
    registry['sources'][0]['original_source_verified'] = True
    with pytest.raises(CourtValidationError) as verified:
        Court(registry, case_claim_book(base))
    assert verified.value.code == 'IMPORTED_TEXT_CANNOT_BE_VERIFIED'


# -- source_read: case-scoped namespaces --------------------------------------------------

def test_source_read_is_case_scoped_and_uses_declared_selectors(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case_id, sources, facts, _ = seed_case(store)
    review(store, case_id, list(facts.values()))
    court = Court.from_case_store(store, case_id)
    out = court.source_read('read_case_source', {'source_id': sources['minutes']})
    assert out['source_type'] == 'UNTRUSTED_DOCUMENTARY_DATA_NOT_INSTRUCTIONS'
    assert out['source_ids'] == [sources['minutes']]
    assert {item['fact_id'] for item in out['facts']} == {facts['cap'], facts['design']}

    other_case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})['case_id']
    other_source = store.add_source(
        other_case, title='other', publisher=FIXTURE['case']['publisher'],
        official_url=FIXTURE['sources'][0]['official_url'],
        document_role='ADOPTED_ACTION_MINUTES', document_date='2026-03-02',
        as_of='2026-03-02', anchor='x', text='other case text')['source_id']
    with pytest.raises(ValueError):
        court.source_read('read_case_source', {'source_id': other_source})

    registry = case_registry(store.get_case(case_id))
    registry['sources'][0]['record_refs'] = [
        {'tool': 'read_item_section', 'args': {'section': 'A.1'},
         'action_id': FIXTURE['facts'][0]['action_id']},
    ]
    declarative = Court(registry, case_claim_book(store.get_case(case_id)))
    resolved = declarative.source_read('read_item_section', {'section': 'A.1'})
    assert {item['fact_id'] for item in resolved['facts']} == {facts['cap'], facts['design']}
    with pytest.raises(ValueError):
        declarative.source_read('read_item_section', {'section': 'A.2'})


def test_exeter_sample_remains_allowlisted_and_general_court_does_not(tmp_path):
    sample = Court.from_exeter_sample()
    assert sample.source_read('read_council_item', {'item': 'J.1'})['source_ids'] == ['EX-MINUTES-20260428']
    assert sample.source_read('read_council_item', {'item': 'I.4'})['source_ids'] == ['EX-MINUTES-20260428']
    assert sample.source_read('read_project_tracker', {'row': 14})['source_ids'] == ['EX-TRACKER-20261009']
    for tool, arg in (('read_council_item', {'item': 'J.2'}),
                      ('read_project_tracker', {'row': 15}),
                      ('read_project_tracker', {'row': True})):
        with pytest.raises(ValueError):
            sample.source_read(tool, arg)

    store = CaseStore(tmp_path / 'cases.json')
    case_id, _, _, _ = seed_case(store)
    with pytest.raises(ValueError):
        Court.from_case_store(store, case_id).source_read('read_council_item', {'item': 'J.1'})


# -- store: excerpt containment and bounds ------------------------------------------------

def test_excerpt_must_occur_in_imported_text(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    source = store.add_source(
        case['case_id'], title='t', publisher=FIXTURE['case']['publisher'],
        official_url=FIXTURE['sources'][0]['official_url'],
        document_role='ADOPTED_ACTION_MINUTES', document_date='2026-03-02',
        as_of='2026-03-02', anchor='x', text='The synthetic minutes say nothing relevant.\n')
    with pytest.raises(CaseStoreError) as missing:
        store.add_fact(case['case_id'], source_id=source['source_id'], anchor='x',
                       exact_excerpt='this text is absent from the source',
                       predicate='AUTHORIZED_USE_CEILING', subject_id='s', action_id='a')
    assert missing.value.code == 'EXCERPT_NOT_IN_SOURCE'


def test_text_bounds_and_empty_text(tmp_path):
    store = CaseStore(tmp_path / 'cases.json')
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    common = dict(title='t', publisher=FIXTURE['case']['publisher'],
                  official_url=FIXTURE['sources'][0]['official_url'],
                  document_role='ADOPTED_ACTION_MINUTES', document_date='2026-03-02',
                  as_of='2026-03-02', anchor='x')
    with pytest.raises(CaseStoreError) as too_large:
        store.add_source(case['case_id'], text='x' * (MAX_TEXT_BYTES + 1), **common)
    assert too_large.value.code == 'SOURCE_TEXT_TOO_LARGE'
    with pytest.raises(CaseStoreError) as empty:
        store.add_source(case['case_id'], text='', **common)
    assert empty.value.code == 'EMPTY_SOURCE_TEXT'


def test_corrupt_or_future_store_is_refused(tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    with pytest.raises(CaseStoreError) as corrupt:
        CaseStore(bad)
    assert corrupt.value.code == 'CASE_STORE_CORRUPT'

    future = tmp_path / 'future.json'
    future.write_text(json.dumps({'schema_version': 99, 'store_revision': 1, 'cases': {}}),
                      encoding='utf-8')
    with pytest.raises(CaseStoreError) as version:
        CaseStore(future)
    assert version.value.code == 'UNSUPPORTED_CASE_STORE_VERSION'


# -- HTTP API ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_empty_state_then_create_pasted_text(client):
    empty = await client.get('/api/cases')
    assert empty.status_code == 200
    assert empty.json()['state'] == 'NO_CASES_YET'

    created = await client.post('/api/cases', json=api_case_payload(FIXTURE['sources'][0]['text']))
    assert created.status_code == 201, created.text
    body = created.json()
    case = body['case']
    assert case['case_id'].startswith('case_')
    assert case['state'] == 'DRAFT_NO_CLAIMS'
    assert case['source_integrity_state'] == 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED'
    assert case['audit']['original_source_verified'] is False
    assert case['audit']['independent_fact_audit'] == 'PENDING'
    source = case['sources'][0]
    assert source['source_id'] == body['created_source_id']
    assert source['capture_class'] == 'USER_SUPPLIED_EXCERPT'
    assert source['exact_text'] == FIXTURE['sources'][0]['text']
    assert source['content_sha256'] == hashlib.sha256(
        FIXTURE['sources'][0]['text'].encode('utf-8')).hexdigest()
    assert source['raw_sha256'] is None
    assert source['hash_subject'] == 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'

    listed = await client.get('/api/cases')
    assert listed.json()['state'] == 'CASES_PRESENT'
    assert listed.json()['cases'][0]['case_id'] == case['case_id']


@pytest.mark.asyncio
async def test_api_upload_happy_path_preserves_exact_bytes(client):
    raw = 'Résumé of synthetic minutes.\r\nSecond line with an em dash — and a tab\t.\n'.encode('utf-8')
    payload = api_case_payload()
    fixture_source = FIXTURE['sources'][0]
    payload['source'] = {
        'title': fixture_source['title'], 'publisher': fixture_source['publisher'],
        'official_url': fixture_source['official_url'],
        'document_role': fixture_source['document_role'],
        'document_date': fixture_source['document_date'], 'as_of': fixture_source['as_of'],
        'anchor': fixture_source['anchor'], 'text_base64': base64.b64encode(raw).decode('ascii'),
        'filename': 'synthetic-notes.txt',
    }
    created = await client.post('/api/cases', json=payload)
    assert created.status_code == 201, created.text
    source = created.json()['case']['sources'][0]
    assert source['exact_text'] == raw.decode('utf-8')
    assert source['content_sha256'] == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_api_upload_rejections_are_typed(client):
    payload = api_case_payload()
    fixture_source = FIXTURE['sources'][0]
    base = {
        'title': fixture_source['title'], 'publisher': fixture_source['publisher'],
        'official_url': fixture_source['official_url'],
        'document_role': fixture_source['document_role'],
        'document_date': fixture_source['document_date'], 'as_of': fixture_source['as_of'],
        'anchor': fixture_source['anchor'],
    }
    oversized = dict(base, text='x' * (MAX_TEXT_BYTES + 1))
    response = await client.post('/api/cases', json=dict(api_case_payload(), source=oversized))
    assert response.status_code == 413
    assert response.json()['detail']['code'] == 'SOURCE_TEXT_TOO_LARGE'

    bad_base64 = dict(base, text_base64='not base64!!', filename='x.txt')
    response = await client.post('/api/cases', json=dict(api_case_payload(), source=bad_base64))
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'INVALID_UPLOAD_ENCODING'

    bad_utf8 = dict(base, text_base64=base64.b64encode(b'\xff\xfe\x00bad').decode('ascii'),
                    filename='x.txt')
    response = await client.post('/api/cases', json=dict(api_case_payload(), source=bad_utf8))
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'INVALID_UTF8'

    wrong_name = dict(base, text_base64=base64.b64encode(b'text').decode('ascii'),
                      filename='notes.pdf')
    response = await client.post('/api/cases', json=dict(api_case_payload(), source=wrong_name))
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'INVALID_INPUT'

    empty = dict(base, text='')
    response = await client.post('/api/cases', json=dict(api_case_payload(), source=empty))
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'EMPTY_SOURCE_TEXT'

    # Every failed creation compensates: the store still reports no cases.
    assert (await client.get('/api/cases')).json()['state'] == 'NO_CASES_YET'


@pytest.mark.asyncio
async def test_api_rejects_malformed_request_bodies(client):
    response = await client.post('/api/cases', content=b'\xff\xfe\x00garbage')
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'INVALID_UTF8'

    response = await client.post('/api/cases', content=b'not json at all')
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'INVALID_JSON'

    response = await client.post('/api/cases', content=b'x' * (600 * 1024))
    assert response.status_code == 413
    assert response.json()['detail']['code'] == 'REQUEST_TOO_LARGE'

    extra = dict(api_case_payload(FIXTURE['sources'][0]['text']), case_id='caller_controlled')
    response = await client.post('/api/cases', json=extra)
    assert response.status_code == 422
    errors = response.json()['detail']['errors']
    assert any('case_id' in item['loc'] for item in errors)


@pytest.mark.asyncio
async def test_api_full_workflow_and_revision_invalidation(client):
    created = await client.post('/api/cases', json=api_case_payload(FIXTURE['sources'][0]['text']))
    case_id = created.json()['case']['case_id']
    minutes_id = created.json()['created_source_id']

    tracker = FIXTURE['sources'][1]
    added = await client.post(f'/api/cases/{case_id}/sources', json={
        'title': tracker['title'], 'publisher': tracker['publisher'],
        'official_url': tracker['official_url'], 'document_role': tracker['document_role'],
        'document_date': tracker['document_date'], 'as_of': tracker['as_of'],
        'anchor': tracker['anchor'], 'text': tracker['text'],
    })
    assert added.status_code == 201, added.text
    tracker_id = added.json()['source']['source_id']

    fact_ids = {}
    for fact, source_id in ((FIXTURE['facts'][0], minutes_id),
                            (FIXTURE['facts'][1], minutes_id),
                            (FIXTURE['facts'][2], tracker_id)):
        payload = {key: value for key, value in fact.items() if key not in ('key', 'source')}
        payload['source_id'] = source_id
        response = await client.post(f'/api/cases/{case_id}/facts', json=payload)
        assert response.status_code == 201, response.text
        fact_ids[fact['key']] = response.json()['fact']['fact_id']

    claim_payloads = {}
    for claim in FIXTURE['claims']:
        payload = {key: value for key, value in claim.items() if key not in ('key', 'fact_keys')}
        payload['fact_ids'] = [fact_ids[key] for key in claim['fact_keys']]
        response = await client.post(f'/api/cases/{case_id}/claims', json=payload)
        assert response.status_code == 201, response.text
        claim_payloads[claim['key']] = response.json()['claim']['claim_id']

    # Unreviewed mapping: the Court refuses to turn it into support.
    blocked = await client.post(f'/api/cases/{case_id}/challenge',
                                json={'claim_id': claim_payloads['authorization']})
    assert blocked.status_code == 200
    assert blocked.json()['status'] == 'REVIEW_REQUIRED'
    assert 'UNREVIEWED_FACT_MAPPING' in blocked.json()['reason_codes']

    for fact_id in (fact_ids['cap'], fact_ids['design'], fact_ids['phase']):
        response = await client.patch(f'/api/cases/{case_id}/facts/{fact_id}',
                                      json={'review_state': 'HUMAN_REVIEWED'})
        assert response.status_code == 200, response.text

    confirm = await client.post(f'/api/cases/{case_id}/claims/{claim_payloads["authorization"]}/confirm',
                                json={'confirmed': True, 'reviewer': 'synthetic-reviewer'})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()['claim']['confirmation']['state'] == 'CONFIRMED'

    supported = await client.post(f'/api/cases/{case_id}/challenge',
                                  json={'claim_id': claim_payloads['authorization']})
    assert supported.json()['status'] == 'DOCUMENTED_ACTION'
    contradicted = await client.post(f'/api/cases/{case_id}/challenge',
                                     json={'claim_id': claim_payloads['spent']})
    assert contradicted.json()['status'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'

    before = (await client.get(f'/api/cases/{case_id}')).json()['case']['revision']
    edited = await client.patch(f'/api/cases/{case_id}/facts/{fact_ids["cap"]}',
                                json={'exact_excerpt': 'approved up to $1,000 in synthetic test funds'})
    assert edited.status_code == 200, edited.text
    after = (await client.get(f'/api/cases/{case_id}')).json()['case']
    assert after['revision'] > before
    authorization = next(claim for claim in after['claims']
                         if claim['claim_id'] == claim_payloads['authorization'])
    assert authorization['confirmation']['state'] == 'INVALIDATED_STALE'

    history = await client.get(f'/api/cases/{case_id}/revisions')
    events = [item['event'] for item in history.json()['revisions']]
    assert 'CLAIM_CONFIRMED' in events and 'FACT_UPDATED' in events
    specific = await client.get(f'/api/cases/{case_id}', params={'revision': after['revision']})
    assert specific.status_code == 200
    assert specific.json()['revision_entry']['event'] == 'FACT_UPDATED'


@pytest.mark.asyncio
async def test_api_case_isolation(client):
    first = await client.post('/api/cases', json=api_case_payload(FIXTURE['sources'][0]['text']))
    first_case = first.json()['case']
    first_source = first.json()['created_source_id']

    second = await client.post('/api/cases', json=api_case_payload())
    second_case = second.json()['case']
    assert second_case['state'] == 'DRAFT_NO_SOURCES'

    detail = (await client.get(f'/api/cases/{second_case["case_id"]}')).json()['case']
    assert detail['sources'] == []
    assert first_source not in json.dumps(detail)

    leaked = await client.get(f'/api/cases/{second_case["case_id"]}/sources/{first_source}')
    assert leaked.status_code == 404
    assert leaked.json()['detail']['code'] == 'SOURCE_NOT_IN_CASE'

    foreign = await client.post(f'/api/cases/{second_case["case_id"]}/challenge',
                                json={'claim_id': 'claim_0000000000000000'})
    assert foreign.status_code == 404
    assert foreign.json()['detail']['code'] == 'UNKNOWN_CASE_CLAIM'
    assert first_case['case_id'] not in json.dumps(
        (await client.get(f'/api/cases/{second_case["case_id"]}')).json())


@pytest.mark.asyncio
async def test_rendered_page_escapes_source_scripts(client):
    hostile = '<script>alert(1)</script> SYNTHETIC FIXTURE inert text\n'
    created = await client.post('/api/cases', json=api_case_payload(hostile))
    case_id = created.json()['case']['case_id']
    page = await client.get(f'/cases/{case_id}')
    assert page.status_code == 200
    assert '<script>alert(1)</script>' not in page.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page.text
    assert 'Original source verified: no' in page.text
    data = await client.get(f'/api/cases/{case_id}')
    assert data.json()['case']['sources'][0]['exact_text'] == hostile


@pytest.mark.asyncio
async def test_api_claim_patch_invalidates_confirmation(client):
    created = await client.post('/api/cases', json=api_case_payload(FIXTURE['sources'][0]['text']))
    case_id = created.json()['case']['case_id']
    source_id = created.json()['created_source_id']
    fact = FIXTURE['facts'][0]
    payload = {key: value for key, value in fact.items() if key not in ('key', 'source')}
    payload['source_id'] = source_id
    payload['review_state'] = 'HUMAN_REVIEWED'
    fact_id = (await client.post(f'/api/cases/{case_id}/facts', json=payload)).json()['fact']['fact_id']
    claim = FIXTURE['claims'][0]
    claim_payload = {key: value for key, value in claim.items() if key not in ('key', 'fact_keys')}
    claim_payload['fact_ids'] = [fact_id]
    claim_id = (await client.post(f'/api/cases/{case_id}/claims',
                                  json=claim_payload)).json()['claim']['claim_id']
    confirmed = await client.post(f'/api/cases/{case_id}/claims/{claim_id}/confirm', json={})
    assert confirmed.json()['claim']['confirmation']['state'] == 'CONFIRMED'

    patched = await client.patch(f'/api/cases/{case_id}/claims/{claim_id}',
                                 json={'text': 'The synthetic council authorized synthetic funds.'})
    assert patched.status_code == 200
    assert patched.json()['claim']['confirmation']['state'] == 'INVALIDATED_STALE'
    assert patched.json()['claim']['confirmation']['invalidation_reason'] == 'CLAIM_CONTENT_REVISED'
