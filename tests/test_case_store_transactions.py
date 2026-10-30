"""CT-QC-060 regressions: every case-store mutation is one detached-candidate commit.

Each test pins an invariant that the pre-fix store violated: a rejected edit left the live dict
mutated (forged excerpt, partial claim text, changed source text, stale confirmation digest) while
the file on disk was untouched, and a failed write left memory ahead of disk with an untyped
exception.

Synthetic fixture only; no provider call, no URL fetch, no paid resource.
"""
import hashlib
import json
from pathlib import Path

import pytest

from civictrace import case_store as case_store_module
from civictrace.case_store import CaseStore, CaseStoreError, _canonical_sha256
from civictrace.court import Court

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'synthetic_case.json')
                     .read_text(encoding='utf-8'))
CASE_FIELDS = ('project_name', 'municipality', 'case_title', 'jurisdiction_id',
               'publisher', 'allowed_hosts', 'provenance_note')
FORGED = 'approved up to $414,529.78 including contingency'


def seed(store):
    case = store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    sources, facts, claims = {}, {}, {}
    for source in FIXTURE['sources']:
        record = store.add_source(
            case['case_id'], title=source['title'], publisher=source['publisher'],
            official_url=source['official_url'], document_role=source['document_role'],
            document_date=source['document_date'], as_of=source['as_of'],
            anchor=source['anchor'], text=source['text'])
        sources[source['key']] = record['source_id']
    for fact in FIXTURE['facts']:
        payload = {key: value for key, value in fact.items() if key not in ('key', 'source')}
        record = store.add_fact(case['case_id'], source_id=sources[fact['source']], **payload)
        facts[fact['key']] = record['fact_id']
    for claim in FIXTURE['claims']:
        payload = {key: value for key, value in claim.items() if key not in ('key', 'fact_keys')}
        record = store.add_claim(case['case_id'],
                                 fact_ids=[facts[key] for key in claim['fact_keys']], **payload)
        claims[claim['key']] = record['claim_id']
    return case['case_id'], sources, facts, claims


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


class Snapshot:
    """Authoritative state a rejected or failed call must leave untouched, byte for byte."""

    def __init__(self, store: CaseStore, case_id: str):
        self.store = store
        self.case_id = case_id
        self.case = store.get_case(case_id)
        self.case_digest = digest(self.case)
        self.revision = self.case['revision']
        self.store_revision = store.store_revision()
        self.disk = store.path.read_bytes()
        self.disk_digest = hashlib.sha256(self.disk).hexdigest()

    def assert_unchanged(self) -> None:
        assert digest(self.store.get_case(self.case_id)) == self.case_digest
        assert self.store.get_case(self.case_id)['revision'] == self.revision
        assert self.store.store_revision() == self.store_revision
        assert self.store.path.read_bytes() == self.disk


@pytest.fixture
def confirmed_case(tmp_path):
    """Seeded case whose authorization claim is CONFIRMED on a human-reviewed fact mapping."""
    store = CaseStore(tmp_path / 'cases.json')
    case_id, sources, facts, claims = seed(store)
    for fact_id in facts.values():
        store.update_fact(case_id, fact_id, {'review_state': 'HUMAN_REVIEWED'})
    store.confirm_claim(case_id, claims['authorization'], reviewer='synthetic-reviewer')
    assert store.get_case(case_id)['claims'][0]['confirmation']['state'] == 'CONFIRMED'
    return store, case_id, sources, facts, claims


def test_rejected_fact_edit_leaves_authoritative_state_byte_identical(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    snapshot = Snapshot(store, case_id)
    with pytest.raises(CaseStoreError) as rejected:
        store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED})
    assert rejected.value.code == 'EXCERPT_NOT_IN_SOURCE'
    snapshot.assert_unchanged()

    fact = [item for item in store.get_case(case_id)['facts'] if item['fact_id'] == facts['cap']][0]
    assert fact['exact_excerpt'] == FIXTURE['facts'][0]['exact_excerpt']
    assert fact['review_state'] == 'HUMAN_REVIEWED'
    assert fact['normal_forms']['normalized_excerpt'] != ' '.join(FORGED.casefold().split())
    assert store.get_case(case_id)['claims'][0]['confirmation']['state'] == 'CONFIRMED'


def test_rejected_fact_edit_cannot_make_the_court_cite_a_forged_excerpt(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    with pytest.raises(CaseStoreError):
        store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED})
    decision = Court.from_case_store(store, case_id).evaluate(claims['authorization'])
    assert decision.status == 'DOCUMENTED_ACTION'
    cited = [citation['exact_excerpt'] for citation in decision.source_citations]
    assert FORGED not in cited
    assert cited == [FIXTURE['facts'][0]['exact_excerpt']]


def test_rejected_source_edit_leaves_text_and_review_states_unchanged(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    snapshot = Snapshot(store, case_id)
    with pytest.raises(CaseStoreError) as rejected:
        store.update_source(case_id, sources['minutes'],
                            {'exact_text': FIXTURE['sources'][0]['text'] + 'AMENDMENT\n',
                             'title': ''})
    assert rejected.value.code == 'INVALID_INPUT'
    snapshot.assert_unchanged()

    source = [item for item in store.get_case(case_id)['sources']
              if item['source_id'] == sources['minutes']][0]
    assert source['exact_text'] == FIXTURE['sources'][0]['text']
    case = store.get_case(case_id)
    assert all(fact['review_state'] == 'HUMAN_REVIEWED' for fact in case['facts'])
    assert case['claims'][0]['confirmation']['state'] == 'CONFIRMED'


def test_rejected_claim_edit_leaves_text_and_confirmation_digest_consistent(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    snapshot = Snapshot(store, case_id)
    with pytest.raises(CaseStoreError) as rejected:
        store.update_claim(case_id, claims['status'], {'text': 'PARTIAL WRITE',
                                                      'predicate': 'NOT_A_PREDICATE'})
    assert rejected.value.code == 'UNKNOWN_PREDICATE'
    snapshot.assert_unchanged()

    claim = [item for item in store.get_case(case_id)['claims']
             if item['claim_id'] == claims['status']][0]
    assert claim['text'] == FIXTURE['claims'][2]['text']
    recomputed = _canonical_sha256({key: claim.get(key) for key in (
        'text', 'predicate', 'subject_id', 'action_id', 'jurisdiction_id', 'fact_ids',
        'additional_record', 'amount_cents', 'phase', 'event_date', 'yea', 'nay', 'abstain',
        'ceiling', 'as_of')})
    assert claim['confirmation']['content_sha256'] == recomputed


def test_rejected_edit_does_not_bump_case_or_store_revision(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    snapshot = Snapshot(store, case_id)
    for call in (lambda: store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED}),
                 lambda: store.update_claim(case_id, claims['status'], {'predicate': 'X'}),
                 lambda: store.update_source(case_id, sources['minutes'], {'as_of': '2026-05-05'})):
        with pytest.raises(CaseStoreError):
            call()
    snapshot.assert_unchanged()


def test_persistence_failure_on_update_leaves_memory_and_disk_untouched(confirmed_case, monkeypatch):
    store, case_id, sources, facts, claims = confirmed_case
    snapshot = Snapshot(store, case_id)

    def refuse(_path, _payload):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(case_store_module, '_atomic_write', refuse)
    with pytest.raises(CaseStoreError) as failed:
        store.update_fact(case_id, facts['cap'], {'anchor': 'Item A.1 (re-anchored)'})
    assert failed.value.code == 'CASE_STORE_WRITE_FAILED'
    assert failed.value.status == 503
    monkeypatch.undo()

    snapshot.assert_unchanged()
    # The same edit succeeds once persistence works again, and only then changes state.
    updated = store.update_fact(case_id, facts['cap'], {'anchor': 'Item A.1 (re-anchored)'})
    assert updated['anchor'] == 'Item A.1 (re-anchored)'
    assert store.get_case(case_id)['revision'] == snapshot.revision + 1
    assert store.store_revision() == snapshot.store_revision + 1


def test_persistence_failure_on_create_leaves_no_case_in_memory_or_on_disk(tmp_path):
    blocker = tmp_path / 'blocked'
    blocker.write_text('not a directory', encoding='utf-8')
    store = CaseStore(blocker / 'cases.json')
    with pytest.raises(CaseStoreError) as failed:
        store.create_case(**{key: FIXTURE['case'][key] for key in CASE_FIELDS})
    assert failed.value.code == 'CASE_STORE_WRITE_FAILED'
    assert failed.value.status == 503
    assert store.list_cases()['cases'] == []
    assert store.list_cases()['state'] == 'NO_CASES_YET'
    assert store.store_revision() == 0
    assert not (blocker / 'cases.json').exists()
    assert not list(tmp_path.glob('*.tmp-*'))


def test_rejected_edit_cannot_wedge_a_later_valid_edit(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    with pytest.raises(CaseStoreError):
        store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED})
    edited = store.update_fact(case_id, facts['cap'], {'anchor': 'Item A.1 (re-anchored)'})
    assert edited['anchor'] == 'Item A.1 (re-anchored)'
    assert edited['exact_excerpt'] == FIXTURE['facts'][0]['exact_excerpt']


def test_later_success_does_not_persist_rejected_content(confirmed_case):
    store, case_id, sources, facts, claims = confirmed_case
    with pytest.raises(CaseStoreError):
        store.update_claim(case_id, claims['status'], {'text': 'PARTIAL WRITE',
                                                      'predicate': 'NOT_A_PREDICATE'})
    with pytest.raises(CaseStoreError):
        store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED})
    store.update_claim(case_id, claims['status'], {'additional_record': 'Synthetic review note'})

    document = json.loads(store.path.read_text(encoding='utf-8'))
    claim = [item for item in document['cases'][case_id]['claims']
             if item['claim_id'] == claims['status']][0]
    fact = [item for item in document['cases'][case_id]['facts']
            if item['fact_id'] == facts['cap']][0]
    assert claim['text'] == FIXTURE['claims'][2]['text']
    assert fact['exact_excerpt'] == FIXTURE['facts'][0]['exact_excerpt']
    assert 'PARTIAL WRITE' not in store.path.read_text(encoding='utf-8')
    assert FORGED not in store.path.read_text(encoding='utf-8')

    reopened = CaseStore(store.path)
    assert reopened.get_case(case_id) == store.get_case(case_id)


@pytest.mark.parametrize('name', ['add_source', 'update_source', 'delete_source', 'add_fact',
                                  'update_fact', 'delete_fact', 'add_claim', 'update_claim',
                                  'confirm_claim', 'delete_claim'])
def test_each_rejected_mutation_leaves_the_document_identical(confirmed_case, name):
    store, case_id, sources, facts, claims = confirmed_case
    if name == 'confirm_claim':
        # Make the evidence unreviewed again so the confirmation itself is the rejected step.
        store.update_fact(case_id, facts['cap'], {'review_state': 'PENDING_HUMAN_REVIEW'})
    snapshot = Snapshot(store, case_id)
    calls = {
        'add_source': lambda: store.add_source(
            case_id, title='t', publisher=FIXTURE['case']['publisher'],
            official_url=FIXTURE['sources'][0]['official_url'],
            document_role='ADOPTED_ACTION_MINUTES', document_date='2026-03-02',
            as_of='2026-03-02', anchor='x', text=''),
        'update_source': lambda: store.update_source(case_id, sources['tracker'],
                                                     {'document_role': 'ADOPTED_ACTION_MINUTES'}),
        'delete_source': lambda: store.delete_source(case_id, sources['minutes']),
        'add_fact': lambda: store.add_fact(
            case_id, source_id=sources['minutes'], anchor='x', exact_excerpt='absent text',
            predicate='AUTHORIZED_USE_CEILING', subject_id='s', action_id='a'),
        'update_fact': lambda: store.update_fact(case_id, facts['cap'], {'exact_excerpt': FORGED}),
        'delete_fact': lambda: store.delete_fact(case_id, 'fact_00000000000000ff'),
        'add_claim': lambda: store.add_claim(
            case_id, text='x', predicate='AUTHORIZED_USE_CEILING', subject_id='s', action_id='a',
            fact_ids=['fact_00000000000000ff'], additional_record='x'),
        'update_claim': lambda: store.update_claim(case_id, claims['status'],
                                                   {'predicate': 'NOT_A_PREDICATE'}),
        'confirm_claim': lambda: store.confirm_claim(case_id, claims['spent']),
        'delete_claim': lambda: store.delete_claim(case_id, 'claim_00000000000000ff'),
    }
    with pytest.raises(CaseStoreError):
        calls[name]()
    snapshot.assert_unchanged()
