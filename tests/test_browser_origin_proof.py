"""CT-QC-062 browser-fidelity regression suite for the workbench write guard.

The defect this pins (Null QC, ``NULL_WB060_BROWSER_REGRESSION.md``): the served page sets
``Referrer-Policy: no-referrer``, so on a *normal* same-origin form submission the browser
serialises ``Origin: null`` while still declaring ``Sec-Fetch-Site: same-origin``. The 060 guard
rejected every null Origin unconditionally, so a real Chrome could not create a case through the
public workbench at all. ASGI tests that invented ``Origin: http://test`` could not see it.

What this suite represents, honestly:

* The header sets below are the ones a real Chrome 156 build was observed sending on loopback
  (``.private-evidence/ct-qc-062/header-capture-*.jsonl``); each case names the class it models.
* It is a header-fidelity client, not a browser: it proves the guard's *decision* for those header
  shapes. ``tests/test_browser_reference_client.py`` drives an actual browser for the end-to-end
  shape when a Playwright-capable interpreter and a Chromium build are present on the host.

Synthetic fixtures only; no provider call, no URL fetch, no paid resource, no network.
"""
import base64
import urllib.parse

import httpx
import pytest
import pytest_asyncio

from civictrace.app import WRITE_METHODS, app, reset_case_store_cache

#: Measured on Chrome 156, loopback, served page carrying ``Referrer-Policy: no-referrer``:
#: ``Origin: null`` + ``Sec-Fetch-Site: same-origin`` for a genuine Create Case form POST with a
#: picked .txt file. httpx fills in ``Host`` from the client's base_url.
CHROME_NO_REFERRER_FORM = {
    'Origin': 'null',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-User': '?1',
    'Content-Type': 'application/x-www-form-urlencoded',
}

CASE_FORM = {
    'case_title': 'SYNTHETIC: did the payment register record the Harbor Lane payment?',
    'project_name': 'Synthetic Harbor Lane Lighting Pilot',
    'municipality': 'Synthetic Bay Municipality (fictional test fixture)',
    'jurisdiction_id': 'synthetic-bay',
    'publisher': 'Synthetic Bay Municipality',
    'allowed_hosts': 'records.synthetic-bay.invalid',
    'provenance_note': 'SYNTHETIC TEST FIXTURE. Not a real government record.',
}

SOURCE_TEXT = ('SYNTHETIC payment register, row 12: invoice 4471, Synthetic Harbor Lane Lighting '
               'Pilot, paid $4,200 on 2026-08-02 (fixture, not a real record).')

SOURCE_FORM = {
    'source_title': 'SYNTHETIC payment register (fixture)',
    'official_url': 'https://records.synthetic-bay.invalid/synthetic-payments.txt',
    'document_role': 'OFFICIAL_PAYMENT_REGISTER',
    'document_date': '2026-08-05',
    'as_of': '2026-08-05',
    'source_anchor': 'Row 12',
}

FACT_FORM = {
    'anchor': 'Row 12',
    'exact_excerpt': 'paid $4,200 on 2026-08-02 (fixture, not a real record)',
    'predicate': 'PAID_AMOUNT',
    'subject_id': 'harbor-lane-lighting',
    'action_id': 'INVOICE-4471',
    'amount_cents': '420000',
    'event_date': '2026-08-02',
}

CLAIM_FORM = {
    'text': 'The synthetic payment register recorded $4,200 paid for the Harbor Lane pilot.',
    'predicate': 'PAID_AMOUNT',
    'subject_id': 'harbor-lane-lighting',
    'action_id': 'INVOICE-4471',
    'additional_record': 'Synthetic bank reconciliation (fixture)',
    'amount_cents': '420000',
    'as_of': '2026-08-05',
}

LOOPBACK_BASE = 'http://127.0.0.1:8766'


def _encode(pairs) -> bytes:
    items = pairs.items() if isinstance(pairs, dict) else pairs
    return urllib.parse.urlencode(list(items), doseq=True).encode('utf-8')


def _client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url,
                             follow_redirects=False)


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_CASE_STORE', str(tmp_path / 'cases.json'))
    reset_case_store_cache()
    yield tmp_path / 'cases.json'
    reset_case_store_cache()


async def _case_count(client: httpx.AsyncClient) -> int:
    listed = await client.get('/api/cases')
    assert listed.status_code == 200
    return len(listed.json()['cases'])


def _assert_rejected(response, code: str, status: int = 403) -> None:
    assert response.status_code == status, response.text
    assert response.json()['detail']['code'] == code
    assert 'access-control-allow-origin' not in {key.lower() for key in response.headers}


# -- the measured browser shape must work -------------------------------------------------------

@pytest.mark.asyncio
async def test_chrome_no_referrer_form_post_creates_the_case(store):
    """Null's exact scenario class: loopback Host, Origin: null, same-origin form POST."""
    async with _client(LOOPBACK_BASE) as client:
        # A read leaves no trace, exactly like a browser opening the page first.
        page = await client.get('/workbench')
        assert page.status_code == 200
        assert page.headers['referrer-policy'] == 'no-referrer'

        form = dict(CASE_FORM)
        form.update({
            'include_source': '1',
            'text_base64': base64.b64encode(SOURCE_TEXT.encode('utf-8')).decode('ascii'),
            'filename': 'synthetic-payments.txt',
            'source_file': 'synthetic-payments.txt',
        })
        form.update(SOURCE_FORM)
        response = await client.post('/workbench/cases', content=_encode(form),
                                     headers=CHROME_NO_REFERRER_FORM)
        assert response.status_code == 303, response.text
        assert response.headers['location'].startswith('/workbench/cases/case_')
        assert await _case_count(client) == 1

        created = response.headers['location'].split('?')[0].rsplit('/', 1)[-1]
        detail = (await client.get(f'/api/cases/{created}')).json()['case']
        assert detail['sources'][0]['exact_text'] == SOURCE_TEXT

        case_page = await client.get(response.headers['location'])
        assert case_page.status_code == 200
        assert 'SYNTHETIC: did the payment register record' in case_page.text


@pytest.mark.asyncio
async def test_null_origin_multipart_is_accepted_by_the_proof_but_the_page_refuses_the_encoding(store):
    """Honest limit: the proof covers both form encodings; the page route still wants urlencoded.

    A ``UiError`` on a page route is rendered as HTML, so the typed code is asserted in the body.
    """
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post(
            '/workbench/cases',
            content=b'--x\r\nContent-Disposition: form-data; name="case_title"\r\n\r\nx\r\n--x--\r\n',
            headers={**CHROME_NO_REFERRER_FORM, 'Content-Type': 'multipart/form-data; boundary=x'})
        assert response.status_code == 415, response.text
        assert 'UNSUPPORTED_FORM_ENCODING' in response.text
        assert await _case_count(client) == 0


# -- every hostile shape the 060 guard rejected is still rejected --------------------------------

@pytest.mark.asyncio
async def test_same_site_attacker_page_form_post_is_rejected(store):
    """Measured: a hostile page on 127.0.0.1:8799 sends Sec-Fetch-Site: same-site."""
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            'Origin': 'http://127.0.0.1:8799', 'Sec-Fetch-Site': 'same-site',
            'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document',
            'Content-Type': 'application/x-www-form-urlencoded'})
        _assert_rejected(response, 'CROSS_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_cross_site_attacker_page_form_post_is_rejected(store):
    """Measured: a hostile page on localhost:8799 sends Sec-Fetch-Site: cross-site."""
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            'Origin': 'http://localhost:8799', 'Sec-Fetch-Site': 'cross-site',
            'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document',
            'Content-Type': 'application/x-www-form-urlencoded'})
        _assert_rejected(response, 'CROSS_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_without_fetch_metadata_is_rejected(store):
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            'Origin': 'null', 'Content-Type': 'application/x-www-form-urlencoded'})
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_with_cross_site_fetch_metadata_is_rejected(store):
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            **CHROME_NO_REFERRER_FORM, 'Sec-Fetch-Site': 'cross-site'})
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_same_origin_form_to_a_rebound_hostname_is_rejected(store):
    """DNS-rebinding class: the attacker's own hostname resolves to 127.0.0.1, so the browser
    calls it same-origin. The proof is loopback-Host-only, so this stays a typed rejection."""
    async with _client('http://rebind.example.invalid:8766') as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM),
                                     headers=CHROME_NO_REFERRER_FORM)
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_lookalike_loopback_hostname_is_rejected(store):
    """``127.0.0.1.evil.example`` must not satisfy the loopback test by suffix."""
    async with _client('http://127.0.0.1.evil.example:8766') as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM),
                                     headers=CHROME_NO_REFERRER_FORM)
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_json_api_write_is_still_rejected(store):
    """The proof is form-only: an opaque-origin JSON write keeps the 060 refusal."""
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/api/cases', content=b'{}', headers={
            'Origin': 'null', 'Sec-Fetch-Site': 'same-origin',
            'Content-Type': 'application/json'})
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_text_plain_write_is_not_proof(store):
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            **CHROME_NO_REFERRER_FORM, 'Content-Type': 'text/plain'})
        _assert_rejected(response, 'NULL_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


@pytest.mark.asyncio
async def test_whitespace_padded_opaque_origin_is_not_treated_as_proof(store):
    """``" null "`` is not the opaque-origin token a browser sends; treat it as a foreign origin."""
    async with _client(LOOPBACK_BASE) as client:
        response = await client.post('/workbench/cases', content=_encode(CASE_FORM), headers={
            **CHROME_NO_REFERRER_FORM, 'Origin': ' null '})
        _assert_rejected(response, 'CROSS_ORIGIN_WRITE_BLOCKED')
        assert await _case_count(client) == 0


# -- coverage: no page route is left behind the old requirement ---------------------------------

def _walk_routes(routes):
    for route in routes:
        nested = getattr(route, 'original_router', None)
        if nested is not None and getattr(nested, 'routes', None):
            yield from _walk_routes(nested.routes)
            continue
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None) or set()
        if path and methods:
            yield path, set(methods)


@pytest.mark.asyncio
async def test_every_workbench_write_route_accepts_the_measured_browser_shape(store):
    """Every mutating page route must get past the guard for the measured Chrome headers."""
    paths = sorted({path for path, methods in _walk_routes(app.routes)
                    if methods & WRITE_METHODS and path.startswith('/workbench')})
    for expected in ('/workbench/cases', '/workbench/cases/{case_id}/sources',
                     '/workbench/cases/{case_id}/sources/{source_id}/facts',
                     '/workbench/cases/{case_id}/facts/{fact_id}/review',
                     '/workbench/cases/{case_id}/claims',
                     '/workbench/cases/{case_id}/claims/{claim_id}/confirm'):
        assert expected in paths, f'{expected} missing from the walked write surface: {paths}'
    assert len(paths) >= 8, f'expected the whole workbench write surface, found {paths}'
    async with _client(LOOPBACK_BASE) as client:
        for path in paths:
            concrete = path
            while '{' in concrete:
                start = concrete.index('{')
                end = concrete.index('}', start)
                concrete = concrete[:start] + 'case_00000000000000ff' + concrete[end + 1:]
            response = await client.post(concrete, content=b'',
                                         headers=CHROME_NO_REFERRER_FORM)
            body = response.text
            for refused in ('NULL_ORIGIN_WRITE_BLOCKED', 'BROWSER_WRITE_WITHOUT_ORIGIN'):
                assert refused not in body, f'{path} still refused the browser shape: {body[:200]}'
            assert response.status_code not in (403,), f'{path} answered 403: {body[:200]}'


# -- the public workbench flows, under the browser shape, across a store reopen ------------------

@pytest.mark.asyncio
async def test_full_workbench_flow_under_browser_headers_survives_a_reopen(store):
    async with _client(LOOPBACK_BASE) as client:
        created = await client.post('/workbench/cases', content=_encode({
            **CASE_FORM, 'include_source': '1', **SOURCE_FORM,
            'text': SOURCE_TEXT,
        }), headers=CHROME_NO_REFERRER_FORM)
        assert created.status_code == 303, created.text
        case_id = created.headers['location'].split('?')[0].rsplit('/', 1)[-1]

        detail = (await client.get(f'/api/cases/{case_id}')).json()['case']
        source_id = detail['sources'][0]['source_id']

        added = await client.post(f'/workbench/cases/{case_id}/sources',
                                  content=_encode({
                                      **SOURCE_FORM,
                                      'source_title': 'SYNTHETIC payment register, second page',
                                      'source_anchor': 'Row 13',
                                      'text': SOURCE_TEXT.replace('Row 12', 'Row 13'),
                                  }), headers=CHROME_NO_REFERRER_FORM)
        assert added.status_code == 303, added.text

        fact = await client.post(f'/workbench/cases/{case_id}/sources/{source_id}/facts',
                                 content=_encode(FACT_FORM), headers=CHROME_NO_REFERRER_FORM)
        assert fact.status_code == 303, fact.text
        fact_id = fact.headers['location'].rsplit('created_fact=', 1)[-1]

        review = await client.post(f'/workbench/cases/{case_id}/facts/{fact_id}/review',
                                   content=_encode({'target': 'HUMAN_REVIEWED',
                                                    'reviewer': 'synthetic-reviewer'}),
                                   headers=CHROME_NO_REFERRER_FORM)
        assert review.status_code == 303, review.text

        claim = await client.post(f'/workbench/cases/{case_id}/claims',
                                  content=_encode({**CLAIM_FORM, 'fact_ids': [fact_id]}),
                                  headers=CHROME_NO_REFERRER_FORM)
        assert claim.status_code == 303, claim.text
        claim_id = claim.headers['location'].rsplit('/', 1)[-1]

        confirm = await client.post(f'/workbench/cases/{case_id}/claims/{claim_id}/confirm',
                                    content=_encode({'action': 'confirm', 'attest': 'yes',
                                                     'reviewer': 'synthetic-reviewer'}),
                                    headers=CHROME_NO_REFERRER_FORM)
        assert confirm.status_code == 303, confirm.text

        stored = (await client.get(f'/api/cases/{case_id}')).json()['case']
        assert stored['claims'][0]['confirmation']['state'] == 'CONFIRMED'
        assert stored['facts'][0]['review_state'] == 'HUMAN_REVIEWED'

    # A restart drops the cached store handle; the next request reopens the file from disk.
    reset_case_store_cache()
    async with _client(LOOPBACK_BASE) as client:
        reopened = await client.get(f'/workbench/cases/{case_id}')
        assert reopened.status_code == 200
        assert 'The synthetic payment register recorded' in reopened.text
        assert 'CONFIRMED' in reopened.text
        listing = (await client.get('/api/cases')).json()
        assert [item['case_id'] for item in listing['cases']] == [case_id]
        assert listing['cases'][0]['source_count'] == 2
        assert listing['cases'][0]['fact_count'] == 1
        assert listing['cases'][0]['claim_count'] == 1
        # The reopened store keeps the same revision chain, not a fresh case.
        revisions = (await client.get(f'/api/cases/{case_id}/revisions')).json()
        assert revisions['revision'] >= 6
        assert revisions['revisions'][0]['event'] == 'CASE_CREATED'
