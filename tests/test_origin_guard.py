"""CT-QC-060 adversarial ASGI suite: cross-origin browser writes must not reach a handler.

The defect this pins: ``POST /api/cases`` with ``Origin: https://attacker.invalid``,
``Sec-Fetch-Site: cross-site``, ``Content-Type: text/plain`` and a valid JSON body returned 201
and created a case. CORS response headers cannot fix that -- a simple request is not preflighted
and the server has already committed the write by the time a browser would read any header.

Synthetic fixture only; no provider call, no URL fetch, no paid resource.
"""
import json
import urllib.parse
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from civictrace.app import WRITE_METHODS, app, reset_case_store_cache

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'synthetic_case.json')
                     .read_text(encoding='utf-8'))
CASE_FIELDS = ('project_name', 'municipality', 'case_title', 'jurisdiction_id',
               'publisher', 'allowed_hosts', 'provenance_note')
PAYLOAD = {key: FIXTURE['case'][key] for key in CASE_FIELDS}
BODY = json.dumps(PAYLOAD)
SAME_ORIGIN = {'Origin': 'http://test', 'Sec-Fetch-Site': 'same-origin'}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_CASE_STORE', str(tmp_path / 'cases.json'))
    reset_case_store_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as async_client:
        yield async_client
    reset_case_store_cache()


async def case_count(client) -> int:
    listed = await client.get('/api/cases')
    assert listed.status_code == 200
    return len(listed.json()['cases'])


def assert_typed_rejection(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    detail = response.json()['detail']
    assert detail['code'] == code
    assert detail['message']
    assert 'access-control-allow-origin' not in {key.lower() for key in response.headers}


@pytest.mark.asyncio
async def test_cross_site_simple_post_with_valid_json_body_cannot_create_a_case(client):
    """The reproduced defect: a no-preflight text/plain write from a hostile page."""
    response = await client.post('/api/cases', content=BODY, headers={
        'Origin': 'https://attacker.invalid', 'Sec-Fetch-Site': 'cross-site',
        'Content-Type': 'text/plain'})
    assert_typed_rejection(response, 403, 'CROSS_ORIGIN_WRITE_BLOCKED')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_cross_origin_json_post_cannot_create_a_case(client):
    response = await client.post('/api/cases', content=BODY, headers={
        'Origin': 'https://attacker.invalid', 'Sec-Fetch-Site': 'cross-site',
        'Content-Type': 'application/json'})
    assert_typed_rejection(response, 403, 'CROSS_ORIGIN_WRITE_BLOCKED')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_null_origin_is_rejected(client):
    response = await client.post('/api/cases', content=BODY, headers={
        'Origin': 'null', 'Sec-Fetch-Site': 'cross-site',
        'Content-Type': 'application/x-www-form-urlencoded'})
    assert_typed_rejection(response, 403, 'NULL_ORIGIN_WRITE_BLOCKED')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_lookalike_and_cross_port_origins_are_rejected(client):
    """Origin is matched exactly: no prefix, no other port, no scheme swap, no decoration."""
    for origin in ('http://test.attacker.invalid', 'http://test:8080', 'https://test',
                   'http://test/', ' http://test ', 'http://test\n'):
        response = await client.post('/api/cases', content=BODY, headers={
            'Origin': origin, 'Sec-Fetch-Site': 'same-origin',
            'Content-Type': 'application/json'})
        assert_typed_rejection(response, 403, 'CROSS_ORIGIN_WRITE_BLOCKED')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_same_site_fetch_metadata_is_rejected_even_with_a_matching_origin(client):
    response = await client.post('/api/cases', content=BODY, headers={
        'Origin': 'http://test', 'Sec-Fetch-Site': 'same-site',
        'Content-Type': 'application/json'})
    assert_typed_rejection(response, 403, 'CROSS_SITE_WRITE_BLOCKED')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_browser_write_without_origin_is_rejected(client):
    response = await client.post('/api/cases', content=BODY, headers={
        'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'})
    assert_typed_rejection(response, 403, 'BROWSER_WRITE_WITHOUT_ORIGIN')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_form_write_without_origin_is_rejected(client):
    response = await client.post('/cases', content=BODY,
                                 headers={'Content-Type': 'multipart/form-data; boundary=x'})
    assert_typed_rejection(response, 403, 'BROWSER_WRITE_WITHOUT_ORIGIN')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_workbench_page_write_without_origin_is_rejected(client):
    """The page routes are browser-only: no Origin, no fetch metadata still cannot write."""
    response = await client.post('/workbench/cases', content='case_title=x',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert_typed_rejection(response, 403, 'BROWSER_WRITE_WITHOUT_ORIGIN')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_same_origin_workbench_form_write_reaches_the_handler(client):
    """The real UI flow: a urlencoded same-origin form post is redirected, not refused."""
    response = await client.post('/workbench/cases', content=urllib.parse.urlencode({
        'case_title': 'Synthetic origin-guard case',
        'project_name': 'Synthetic Sidewalk Pilot',
        'municipality': FIXTURE['case']['municipality'],
        'jurisdiction_id': FIXTURE['case']['jurisdiction_id'],
        'publisher': FIXTURE['case']['publisher'],
        'allowed_hosts': FIXTURE['case']['allowed_hosts'][0],
    }).encode('utf-8'), headers={**SAME_ORIGIN,
                                 'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 303, response.text
    assert await case_count(client) == 1


@pytest.mark.asyncio
async def test_same_origin_text_plain_write_is_refused_with_415(client):
    """Both headers are honest here, so the content-type gate is what refuses the write."""
    response = await client.post('/api/cases', content=BODY, headers={
        **SAME_ORIGIN, 'Content-Type': 'text/plain'})
    assert_typed_rejection(response, 415, 'UNSUPPORTED_WRITE_MEDIA_TYPE')
    assert await case_count(client) == 0


@pytest.mark.asyncio
async def test_legitimate_same_origin_write_still_creates_the_case(client):
    response = await client.post('/api/cases', content=BODY,
                                 headers={**SAME_ORIGIN, 'Content-Type': 'application/json'})
    assert response.status_code == 201, response.text
    created = response.json()['case']['case_id']
    assert await case_count(client) == 1
    stored = await client.get(f'/api/cases/{created}')
    assert stored.json()['case']['case_id'] == created


@pytest.mark.asyncio
async def test_non_browser_api_client_keeps_working(client):
    """A local script is not CSRF-reachable and can forge any header a check would read."""
    response = await client.post('/api/cases', content=BODY,
                                 headers={'Content-Type': 'application/json'})
    assert response.status_code == 201, response.text


def _walk_routes(routes):
    """Yield (path, methods) for every route, including ones inside an included APIRouter.

    This FastAPI version keeps an ``_IncludedRouter`` placeholder in ``app.routes`` rather than
    flattening the included router's routes, so a plain ``for route in app.routes`` silently skips
    every workbench form route -- and a coverage test built on it would prove nothing about them.
    """
    for route in routes:
        nested = getattr(route, 'original_router', None)
        if nested is not None and getattr(nested, 'routes', None):
            yield from _walk_routes(nested.routes)
            continue
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None) or set()
        if path and methods:
            yield path, set(methods)


def _mutating_route_paths() -> list[str]:
    paths = {path for path, methods in _walk_routes(app.routes) if methods & WRITE_METHODS}
    return sorted(paths)


@pytest.mark.asyncio
async def test_every_mutating_route_rejects_a_cross_site_simple_write(client):
    paths = _mutating_route_paths()
    # Both families must be present, or the guard is being tested against a subset of the surface.
    assert '/api/cases' in paths
    assert '/workbench/cases' in paths
    # 19 distinct paths carrying 22 method bindings today (some paths take PATCH and DELETE).
    assert len(paths) >= 19, f'expected every mutating route, found {len(paths)}'
    attempted = []
    for path in paths:
        concrete = path
        while '{' in concrete:
            start = concrete.index('{')
            end = concrete.index('}', start)
            concrete = concrete[:start] + 'case_00000000000000ff' + concrete[end + 1:]
        response = await client.post(concrete, content=BODY, headers={
            'Origin': 'https://attacker.invalid', 'Sec-Fetch-Site': 'cross-site',
            'Content-Type': 'text/plain'})
        attempted.append((path, response.status_code, response.text[:200]))
        assert response.status_code == 403, f'{path} answered {response.status_code}'
        assert response.json()['detail']['code'] == 'CROSS_ORIGIN_WRITE_BLOCKED'
    assert len(attempted) == len(paths)


@pytest.mark.asyncio
async def test_no_submitted_url_is_ever_fetched(client, monkeypatch):
    """Every write route takes a URL; none of them may resolve or connect to it."""
    import socket

    def refuse_connect(*_args, **_kwargs):
        raise AssertionError('the app attempted an outbound connection')

    monkeypatch.setattr(socket.socket, 'connect', refuse_connect, raising=True)
    monkeypatch.setattr(socket, 'create_connection', refuse_connect, raising=True)

    payload = dict(PAYLOAD)
    payload['source'] = {key: FIXTURE['sources'][0][key] for key in (
        'title', 'publisher', 'official_url', 'document_role', 'document_date', 'as_of',
        'anchor', 'text')}
    accepted = await client.post('/api/cases', content=json.dumps(payload),
                                 headers={**SAME_ORIGIN, 'Content-Type': 'application/json'})
    assert accepted.status_code == 201, accepted.text

    rejected = await client.post('/api/cases', content=json.dumps({
        **PAYLOAD, 'source': {**payload['source'],
                              'official_url': 'https://not-allowlisted.invalid/x.txt'}}),
        headers={**SAME_ORIGIN, 'Content-Type': 'application/json'})
    assert rejected.status_code == 422
