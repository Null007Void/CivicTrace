"""Single-process CivicTrace app: the Exeter sample surface plus the local case workbench.

The workbench routes under ``/api/cases`` persist to one local, versioned JSON store chosen by the
process (``CIVICTRACE_CASE_STORE``). Nothing here fetches a supplied URL, calls a provider, or
touches a deployed store; judge deployment is still NOT YET AUTHORIZED.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import os
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import workbench_ui
from .case_store import (
    MAX_TEXT_BYTES,
    CaseStore,
    CaseStoreError,
    case_claim_book,
    case_registry,
    case_state,
)
from .court import Court, CourtValidationError
from .nebius import (
    LIVE_ELIGIBILITY_REFUSAL,
    REQUESTS_PER_CLAIM_CEILING,
    ProviderUnavailable,
    configured_live_agent,
    live_eligibility,
)
from .spend_backend import open_spend_backend
from .usage_ledger import Admission, LiveBudget, UsageLedgerError

APP_ROOT = Path(__file__).resolve().parent
#: The shipped Exeter records are loaded as an explicit *sample* case. Case-scoped work goes
#: through ``Court.from_case_store`` (see ``_court_for_case``).
court = Court.from_exeter_sample()

#: One local store file. Chosen by the process, never by a request.
MAX_REQUEST_BYTES = 512 * 1024
DEFAULT_CASE_STORE = APP_ROOT.parent / 'case-data' / 'cases.json'
_CASE_STORES: dict[str, CaseStore] = {}


def case_store_path() -> Path:
    configured = os.environ.get('CIVICTRACE_CASE_STORE')
    return Path(configured) if configured else DEFAULT_CASE_STORE


def _store() -> CaseStore:
    path = str(case_store_path())
    store = _CASE_STORES.get(path)
    if store is None:
        store = _CASE_STORES[path] = CaseStore(path)
    return store


def reset_case_store_cache() -> None:
    """Drop cached store handles (tests switch ``CIVICTRACE_CASE_STORE`` between cases)."""
    _CASE_STORES.clear()


def _case_http(exc: CaseStoreError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.as_detail())


def _court_http(exc: CourtValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=exc.as_detail())


def startup_sweep() -> dict | None:
    """Release leases abandoned by a dead predecessor; called once by the ASGI lifespan.

    A failure here is not fatal: admission fails closed per request, so the startup sweep only
    accelerates recovery of a killed instance's reservations. The configured backend is opened
    once per call, so a misconfigured cloud backend refuses here too rather than being replaced.
    """
    try:
        return open_spend_backend().startup_sweep()
    except UsageLedgerError:
        return None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await asyncio.to_thread(startup_sweep)
    yield


app = FastAPI(title='CivicTrace Evidence Court', version='0.1.0',
              docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
_live_gate = asyncio.Semaphore(2)
# Per-peer windows are process-local abuse control only; the global monetary/request ceiling is
# the shared durable spend gate below. The map is explicitly bounded (QC-09): 1000 rotating
# source addresses must not create 1000 live keys.
PEER_WINDOW_SECONDS = 60.0
PEER_REQUEST_LIMIT = 8
MAX_TRACKED_PEERS = 256


class PeerWindows:
    """Bounded, expiring peer request map.

    Keys are inserted only through :meth:`allow`. Before an insertion that would exceed
    ``max_peers``, expired windows are dropped and then the least-recently-used peer is
    evicted, so the live key count is bounded by ``max_peers`` regardless of how many distinct
    peers an unauthenticated caller presents. The bound is a memory bound, not an
    authentication mechanism: eviction can let a rotating attacker back in, and a real
    deployment still needs a gateway/reverse-proxy limit.
    """

    def __init__(self, *, limit: int = PEER_REQUEST_LIMIT,
                 window_seconds: float = PEER_WINDOW_SECONDS,
                 max_peers: int = MAX_TRACKED_PEERS):
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.max_peers = max(1, int(max_peers))
        self._windows: OrderedDict[str, deque] = OrderedDict()

    def _prune(self, peer: str, now: float) -> deque:
        queue = self._windows[peer]
        while queue and now - queue[0] > self.window_seconds:
            queue.popleft()
        return queue

    def _expire_all(self, now: float) -> None:
        for peer in list(self._windows):
            if not self._prune(peer, now):
                del self._windows[peer]

    def allow(self, peer: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._windows) > self.max_peers * 3 // 4:
            self._expire_all(now)
        queue = self._windows.get(peer)
        if queue is None:
            while len(self._windows) >= self.max_peers:
                self._windows.popitem(last=False)
            queue = self._windows[peer] = deque()
        else:
            self._windows.move_to_end(peer)
            queue = self._prune(peer, now)
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True

    def size(self) -> int:
        return len(self._windows)

    def clear(self) -> None:
        self._windows.clear()


_peer_windows = PeerWindows()


class RequestId(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    # Claim IDs are lower-case identifiers that may contain digits after the first
    # character (curated book includes `vote_5_0`). Anything else stays a typed 422;
    # membership in the curated claim book is still required (404) before evaluation.
    claim_id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')


def _provider_error_detail(error: ProviderUnavailable, message: str) -> dict:
    usage = error.telemetry.get('usage_as_returned')
    if not isinstance(usage, dict):
        usage = {'prompt_tokens': None, 'completion_tokens': None}
    return {
        'code': error.reason,
        'message': message,
        'provider_usage': {
            'prompt_tokens': usage.get('prompt_tokens') if type(usage.get('prompt_tokens')) is int else None,
            'completion_tokens': usage.get('completion_tokens') if type(usage.get('completion_tokens')) is int else None,
            'complete': error.telemetry.get('usage_complete') is True,
        },
        'finish_reason': error.telemetry.get('finish_reason'),
    }


# -- browser write protection ------------------------------------------------------------------
# A cross-site page can make a browser issue a "simple" write (text/plain, urlencoded or
# multipart) that no CORS preflight guards, and CORS response headers are advisory: a server that
# answers 201 has already created the case. So every mutating request is checked on its own
# evidence -- the Origin it declares and the Fetch Metadata the browser attached -- instead of on
# a response header. Nothing here authenticates a caller: a non-browser client can forge any
# header it likes, so this is a CSRF boundary for browsers, not an authentication scheme.
WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
JSON_CONTENT_TYPE = 'application/json'
FORM_CONTENT_TYPES = frozenset({'multipart/form-data', 'application/x-www-form-urlencoded'})
SAFE_FETCH_SITES = frozenset({'same-origin', 'none'})
#: A document served with the ``Referrer-Policy: no-referrer`` this app ships makes the browser
#: serialise ``Origin: null`` on a normal form submission, while the Fetch Metadata headers still
#: describe the real client: Chrome 156 sent ``Origin: null`` with ``Sec-Fetch-Site: same-origin``
#: for a genuine same-origin form POST (measured, CT-WB6R-REPORT.md). The referrer policy strips the
#: referrer, not the fetch metadata, so an opaque Origin alone is no longer treated as proof of a
#: hostile request -- and it is still never accepted on its own.
OPAQUE_ORIGINS = frozenset({'null'})
#: Hostnames that can only name this machine. Used *only* by the opaque-Origin form proof: it keeps
#: that branch off a hostname a DNS-rebinding page could resolve to 127.0.0.1.
LOOPBACK_HOSTNAMES = frozenset({'127.0.0.1', 'localhost', '::1'})


def _server_origin(request: Request) -> str:
    """The origin this process was addressed as, from the request line the browser sent."""
    host = (request.headers.get('host') or '').strip().lower()
    return f'{request.url.scheme}://{host}'


def _host_header_hostname(request: Request) -> str:
    """The Host header without its port and IPv6 brackets; empty when it is not one authority."""
    raw = (request.headers.get('host') or '').strip()
    if not raw:
        return ''
    if raw.startswith('['):
        end = raw.find(']')
        if end == -1:
            return ''
        remainder = raw[end + 1:]
        if remainder and not remainder.startswith(':'):
            return ''
        return raw[1:end].strip().lower()
    if raw.count(':') > 1:
        # An unbracketed IPv6 literal is not a Host shape a browser sends; refuse to guess.
        return ''
    return raw.split(':', 1)[0].strip().lower()


def _has_same_origin_form_proof(request: Request, site: str | None, content_type: str) -> bool:
    """Browser-attested proof for a form write whose Origin the referrer policy made opaque.

    All three conditions are needed: the browser states ``Sec-Fetch-Site: same-origin`` (a header
    page script cannot set), the body is a form encoding (the only shape a no-CORS cross-site page
    can send, and the only shape this proof is offered for), and the request was addressed to a
    loopback authority (so a rebound hostname cannot borrow ``same-origin``). ``same-site`` -- a
    hostile page on another port of 127.0.0.1 -- is deliberately not proof.
    """
    return (site is not None and site.strip().lower() == 'same-origin'
            and content_type in FORM_CONTENT_TYPES
            and _host_header_hostname(request) in LOOPBACK_HOSTNAMES)



def _declared_content_type(request: Request) -> str:
    return (request.headers.get('content-type') or '').split(';', 1)[0].strip().lower()


def _write_rejection(status: int, code: str, message: str) -> JSONResponse:
    rejected = JSONResponse(status_code=status, content={'detail': {'code': code,
                                                                   'message': message}})
    rejected.headers['Cache-Control'] = 'no-store'
    rejected.headers['X-Content-Type-Options'] = 'nosniff'
    return rejected


def _browser_write_rejection(request: Request) -> JSONResponse | None:
    """Return a typed rejection for a write that a browser could have been tricked into making."""
    origin = request.headers.get('origin')
    site = request.headers.get('sec-fetch-site')
    content_type = _declared_content_type(request)
    expected_origin = _server_origin(request)
    opaque_origin = origin is not None and origin.lower() in OPAQUE_ORIGINS

    if opaque_origin and not _has_same_origin_form_proof(request, site, content_type):
        # Still the 060 rejection for every opaque-Origin shape that cannot prove itself: a
        # cross-site attacker page, a `file://` document, a sandboxed frame, a rebound hostname,
        # a JSON or text/plain body, or a request with no Fetch Metadata at all.
        return _write_rejection(403, 'NULL_ORIGIN_WRITE_BLOCKED',
                                'A write from an opaque (null) origin is not accepted')
    if origin is not None and not opaque_origin:
        normalised = origin.lower()
        if normalised != expected_origin:
            return _write_rejection(403, 'CROSS_ORIGIN_WRITE_BLOCKED',
                                    'A write must originate from this workbench origin')
    if site is not None:
        if site.strip().lower() not in SAFE_FETCH_SITES:
            return _write_rejection(403, 'CROSS_SITE_WRITE_BLOCKED',
                                    'A cross-site browser write is not accepted')
        if origin is None:
            return _write_rejection(403, 'BROWSER_WRITE_WITHOUT_ORIGIN',
                                    'A browser write must declare its Origin')
    if origin is None and content_type in FORM_CONTENT_TYPES:
        # Only a browser submits a form, and every browser submission carries Origin.
        return _write_rejection(403, 'BROWSER_WRITE_WITHOUT_ORIGIN',
                                'A form write must declare its Origin')
    if origin is None and not request.url.path.startswith('/api/'):
        # The page routes exist only to be driven by this workbench page in a browser; the JSON
        # API under /api/ stays available to local scripts.
        return _write_rejection(403, 'BROWSER_WRITE_WITHOUT_ORIGIN',
                                'A workbench page write must declare its Origin')
    if content_type:
        allowed = {JSON_CONTENT_TYPE}
        if not request.url.path.startswith('/api/'):
            allowed |= FORM_CONTENT_TYPES
        if content_type not in allowed:
            return _write_rejection(415, 'UNSUPPORTED_WRITE_MEDIA_TYPE',
                                    'This route does not accept that write content type')
    return None


@app.middleware('http')
async def browser_write_guard(request: Request, call_next):
    """Reject browser writes that this origin did not make before any route handler runs."""
    if request.method in WRITE_METHODS:
        rejection = _browser_write_rejection(request)
        if rejection is not None:
            return rejection
    return await call_next(request)


@app.middleware('http')
async def safety_headers(request: Request, call_next):
    # Bound request bodies for every mutating route, including the FastAPI-bound PATCH models.
    if request.method in ('POST', 'PUT', 'PATCH'):
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                rejected = JSONResponse(status_code=400, content={'detail': {
                    'code': 'INVALID_CONTENT_LENGTH',
                    'message': 'Content-Length is not an integer'}})
                rejected.headers['Cache-Control'] = 'no-store'
                rejected.headers['X-Content-Type-Options'] = 'nosniff'
                return rejected
            if declared_size > MAX_REQUEST_BYTES:
                rejected = JSONResponse(status_code=413, content={'detail': {
                    'code': 'REQUEST_TOO_LARGE',
                    'message': 'Request body exceeds the bounded upload limit',
                    'limit_bytes': MAX_REQUEST_BYTES}})
                rejected.headers['Cache-Control'] = 'no-store'
                rejected.headers['X-Content-Type-Options'] = 'nosniff'
                return rejected
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.get('/')
async def index():
    return FileResponse(APP_ROOT / 'static' / 'index.html', media_type='text/html')


@app.get('/assets/site.css')
async def css():
    return FileResponse(APP_ROOT / 'static' / 'site.css', media_type='text/css')


@app.get('/assets/site.js')
async def js():
    return FileResponse(APP_ROOT / 'static' / 'site.js', media_type='application/javascript')


@app.get('/assets/favicon.svg')
async def favicon():
    return FileResponse(APP_ROOT / 'static' / 'favicon.svg', media_type='image/svg+xml')


@app.get('/assets/workbench.js')
async def workbench_js():
    """Self-hosted file-input helper for the workbench UI; no styling, no remote script."""
    return FileResponse(APP_ROOT / 'static' / 'workbench.js',
                        media_type='application/javascript')


@app.get('/healthz')
async def health():
    return {'ok':True,'service':'civictrace','version':'0.1.0','live_inference_enabled':os.getenv('CIVICTRACE_LIVE_ENABLED')=='1',
            'primary_source_integrity':court.integrity_state}


@app.get('/api/case')
async def case():
    # Public only: no private planning, provider credentials, or local paths.
    return {'case_id':court.registry['case_id'],'title':court.registry['case_title'],
            'municipality':court.registry['municipality'],
            'source_integrity_state':court.integrity_state,
            'source_capture_complete':court.capture_complete,
            'source_warning':court.registry['source_integrity_warning'],
            'registry_sha256':court.registry_sha256,
            'sources':[{k:s[k] for k in ('source_id','title','official_url','document_role','as_of','raw_sha256','retrieved_at_utc','review_state')}
                       for s in court.registry['sources']],
            'claims':[{k:c[k] for k in ('id','text','predicate')} for c in court.claims.values()],
            'live_capability':{'owner_enabled':os.getenv('CIVICTRACE_LIVE_ENABLED')=='1',
                               'provider_configured':bool(os.getenv('NEBIUS_API_KEY'))},
            'scope_note':'Two original official source types, one project. Not an audit of payments or actual physical construction.'}


@app.post('/api/challenge')
async def challenge(body: RequestId):
    if body.claim_id not in court.claims:
        raise HTTPException(status_code=404, detail='Unknown curated claim. Unreviewed free-form assertions require human review.')
    return court.evaluate(body.claim_id).as_json()


def _rate_limit(request: Request) -> None:
    # In-memory best-effort abuse control with a bounded key map (QC-09). The durable global
    # ceiling is enforced by the usage ledger on admission, not here.
    peer = request.client.host if request.client else 'unknown'
    if not _peer_windows.allow(peer):
        raise HTTPException(status_code=429, detail={'code':'LIVE_RATE_LIMIT','message':'Live investigation request limit reached'})


def _settle(admission: Admission, outcome: str, evidence: dict | None) -> None:
    """Record the settlement receipt; never allowed to change the HTTP outcome.

    The provider telemetry is passed as *evidence*, not as an instruction: the store classifies
    it (``civictrace.spend_lifecycle``) and only an explicit pre-dispatch class or a provider
    failure response can mark a reservation ``failed_confirmed``. Nothing here refunds, and an
    uncertain outcome keeps its allowance consumed.
    """
    payload = evidence if isinstance(evidence, dict) else {}
    usage = payload.get('usage_as_returned') if isinstance(payload.get('usage_as_returned'), dict) else {}
    try:
        open_spend_backend().settle(
            admission, outcome=outcome,
            provider_requests=payload.get('provider_requests'),
            prompt_tokens=usage.get('prompt_tokens'),
            completion_tokens=usage.get('completion_tokens'),
            evidence=payload)
    except UsageLedgerError:
        # The admission is already durable; a settlement write failure cannot grant budget.
        pass


def _mark_transmission(admission: Admission, round_num: int) -> None:
    """Durably record one provider dispatch *before* it happens (CT-QC-039).

    A failure here is not fatal: the reservation is already committed, and an unresolved
    reservation becomes ``uncertain`` on expiry, so the allowance stays consumed either way.
    Refusing the provider call instead would be worse — it would strand an admitted unit.
    """
    try:
        open_spend_backend().mark_transmitted(admission, round_num=round_num)
    except UsageLedgerError:
        pass


async def _dispatch_hook(admission: Admission, round_num: int) -> None:
    await asyncio.to_thread(_mark_transmission, admission, round_num)


def _reserve_on_configured_backend(budget: LiveBudget) -> Admission:
    """Open the configured accounting backend and take one atomic reservation.

    Opening happens here (off the event loop) so configuration and reachability failures surface
    as :class:`UsageLedgerError` before the provider call, exactly like a reservation refusal.
    """
    return open_spend_backend().reserve(budget)


async def _admitted_investigation(live_court: Court, claim_id: str,
                                  request: Request) -> dict:
    """The one admitted run path, shared by both investigate routes (CT-QC-064).

    The Court is an argument and nothing else about the run changes: the durable budget, the
    atomic reservation on the configured backend, the pre-dispatch marker and the settlement are
    the same objects and the same order for the shipped sample route and for a selected-case run.
    Every gate that must refuse *before* money can be spent is checked by the caller, before this
    coroutine is entered.
    """
    try:
        agent = configured_live_agent(live_court)
    except ProviderUnavailable as e:
        raise HTTPException(status_code=503, detail=_provider_error_detail(e, 'No live Nebius inference performed.')) from None
    _rate_limit(request)
    try:
        budget = LiveBudget.from_env(requests_per_claim=REQUESTS_PER_CLAIM_CEILING)
    except UsageLedgerError as e:
        raise HTTPException(status_code=503, detail={'code': e.code}) from None
    if _live_gate.locked():
        raise HTTPException(status_code=429, detail={'code':'PROVIDER_CONCURRENCY_LIMIT'})
    async with _live_gate:
        # Durable global admission on the *configured* accounting backend (QC-02 raised bar,
        # CT-QC-038 backend seam). The reservation is atomic with respect to every instance
        # sharing the deployment constant and is taken BEFORE any provider request, so restarts,
        # separate instances and a revision rollback cannot each spend the ceiling.
        # Unconfigured/unreachable/contended/corrupt store = typed refusal, never a reset and
        # never a silent switch to another backend (in particular never SQLite-on-a-local-path
        # for a deployment that configured a cloud backend).
        try:
            admission = await asyncio.to_thread(_reserve_on_configured_backend, budget)
        except UsageLedgerError as e:
            raise HTTPException(status_code=503, detail={
                'code': e.code,
                'message': 'Live inference refused: the configured spend backend is not usable. '
                           'No provider request was made.',
            }) from None
        if not admission.admitted:
            raise HTTPException(status_code=429, detail={'code': admission.code,
                                                         'requests_committed': admission.requests_committed})
        try:
            # The dispatch hook is only offered to an agent that declares support for it, so a
            # test double (or any alternative agent) keeps its original signature. The hook writes
            # a durable "transmission started" marker before each provider request, which is what
            # makes a crash-after-send recoverable as ``uncertain`` instead of looking untouched.
            run_kwargs = {}
            if getattr(agent, 'supports_dispatch_hook', False):
                run_kwargs['on_dispatch'] = (lambda round_num:
                                             _dispatch_hook(admission, round_num))
            result = await agent.run(claim_id, **run_kwargs)
        except ProviderUnavailable as e:
            # The reservation is never refunded: an uncertain timeout may already have cost money.
            await asyncio.to_thread(_settle, admission, 'PROVIDER_UNAVAILABLE', e.telemetry)
            raise HTTPException(status_code=502, detail=_provider_error_detail(e, 'No successful live investigation is claimed.')) from None
        await asyncio.to_thread(_settle, admission, 'SUCCESS',
                                getattr(result, 'provider', None) if isinstance(getattr(result, 'provider', None), dict) else None)
    return result.as_json()


@app.post('/api/investigate')
async def investigate(body: RequestId, request: Request):
    if body.claim_id not in court.claims:
        raise HTTPException(status_code=404, detail={'code':'UNKNOWN_GUIDED_QUESTION'})
    return await _admitted_investigation(court, body.claim_id, request)


# --------------------------------------------------------------------------------------------
# Local case workbench (CT-WB1 part 1). Offline only: no provider call, no URL fetch, no OCR.
# --------------------------------------------------------------------------------------------

_ID_PATTERN = r'^[a-z][a-z0-9_]{0,39}$'
#: Human-declared subject/action references use the Court's grammar (dots, colons, hyphens).
_REF_PATTERN = r'^[A-Za-z0-9_.:-]{1,128}$'


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SourceCreate(_Strict):
    """One source: metadata the owner declares plus pasted text or a bounded .txt upload.

    ``official_url`` is recorded as a declared location. It is never fetched, and its presence is
    not evidence that the document exists at that address.
    """
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=200)
    official_url: str = Field(min_length=8, max_length=1000)
    document_role: str = Field(min_length=1, max_length=80)
    document_date: str = Field(min_length=10, max_length=10)
    as_of: str = Field(min_length=10, max_length=10)
    anchor: str = Field(min_length=1, max_length=500)
    text: str | None = Field(default=None, max_length=MAX_TEXT_BYTES * 4)
    text_base64: str | None = Field(default=None, max_length=200_000)
    filename: str | None = Field(default=None, max_length=255)

    @model_validator(mode='after')
    def _exactly_one_payload(self):
        if (self.text is None) == (self.text_base64 is None):
            raise ValueError('provide exactly one of text or text_base64')
        if self.text_base64 is not None and not (self.filename or '').lower().endswith('.txt'):
            raise ValueError('a file upload must declare a .txt filename')
        return self


class CaseCreate(_Strict):
    project_name: str = Field(min_length=1, max_length=200)
    municipality: str = Field(min_length=1, max_length=200)
    case_title: str = Field(min_length=1, max_length=300)
    jurisdiction_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    publisher: str = Field(min_length=1, max_length=200)
    allowed_hosts: list[str] = Field(min_length=1, max_length=8)
    provenance_note: str | None = Field(default=None, max_length=500)
    source: SourceCreate | None = None


class SourcePatch(_Strict):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    official_url: str | None = Field(default=None, min_length=8, max_length=1000)
    document_role: str | None = Field(default=None, min_length=1, max_length=80)
    document_date: str | None = Field(default=None, min_length=10, max_length=10)
    as_of: str | None = Field(default=None, min_length=10, max_length=10)
    anchor: str | None = Field(default=None, min_length=1, max_length=500)
    exact_text: str | None = Field(default=None, max_length=MAX_TEXT_BYTES * 4)


class FactCreate(_Strict):
    source_id: str = Field(pattern=_ID_PATTERN)
    anchor: str = Field(min_length=1, max_length=500)
    exact_excerpt: str = Field(min_length=1, max_length=2000)
    predicate: str = Field(min_length=1, max_length=80)
    subject_id: str = Field(pattern=_REF_PATTERN)
    action_id: str = Field(pattern=_REF_PATTERN)
    qualifiers: list[str] = Field(default_factory=list, max_length=16)
    review_state: Literal['PENDING_HUMAN_REVIEW', 'HUMAN_REVIEWED'] = 'PENDING_HUMAN_REVIEW'
    reviewed_by: str | None = Field(default=None, max_length=120)
    amount_cents: int | None = Field(default=None, ge=0)
    phase: str | None = Field(default=None, max_length=64)
    event_date: str | None = Field(default=None, max_length=10)
    yea: int | None = Field(default=None, ge=0, le=99)
    nay: int | None = Field(default=None, ge=0, le=99)
    abstain: int | None = Field(default=None, ge=0, le=99)


class FactPatch(_Strict):
    anchor: str | None = Field(default=None, min_length=1, max_length=500)
    exact_excerpt: str | None = Field(default=None, min_length=1, max_length=2000)
    predicate: str | None = Field(default=None, min_length=1, max_length=80)
    subject_id: str | None = Field(default=None, pattern=_REF_PATTERN)
    action_id: str | None = Field(default=None, pattern=_REF_PATTERN)
    qualifiers: list[str] | None = Field(default=None, max_length=16)
    review_state: Literal['PENDING_HUMAN_REVIEW', 'HUMAN_REVIEWED',
                          'SOURCE_REVISED_REVIEW_REQUIRED'] | None = None
    reviewed_by: str | None = Field(default=None, max_length=120)
    amount_cents: int | None = Field(default=None, ge=0)
    phase: str | None = Field(default=None, max_length=64)
    event_date: str | None = Field(default=None, max_length=10)
    yea: int | None = Field(default=None, ge=0, le=99)
    nay: int | None = Field(default=None, ge=0, le=99)
    abstain: int | None = Field(default=None, ge=0, le=99)


class ClaimCreate(_Strict):
    text: str = Field(min_length=1, max_length=1000)
    predicate: str = Field(min_length=1, max_length=80)
    subject_id: str = Field(pattern=_REF_PATTERN)
    action_id: str = Field(pattern=_REF_PATTERN)
    fact_ids: list[str] = Field(min_length=1, max_length=8)
    additional_record: str = Field(min_length=1, max_length=500)
    amount_cents: int | None = Field(default=None, ge=0)
    phase: str | None = Field(default=None, max_length=64)
    event_date: str | None = Field(default=None, max_length=10)
    yea: int | None = Field(default=None, ge=0, le=99)
    nay: int | None = Field(default=None, ge=0, le=99)
    abstain: int | None = Field(default=None, ge=0, le=99)
    ceiling: bool | None = None
    as_of: str | None = Field(default=None, max_length=10)


class ClaimPatch(_Strict):
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    predicate: str | None = Field(default=None, min_length=1, max_length=80)
    subject_id: str | None = Field(default=None, pattern=_REF_PATTERN)
    action_id: str | None = Field(default=None, pattern=_REF_PATTERN)
    fact_ids: list[str] | None = Field(default=None, min_length=1, max_length=8)
    additional_record: str | None = Field(default=None, min_length=1, max_length=500)
    amount_cents: int | None = Field(default=None, ge=0)
    phase: str | None = Field(default=None, max_length=64)
    event_date: str | None = Field(default=None, max_length=10)
    yea: int | None = Field(default=None, ge=0, le=99)
    nay: int | None = Field(default=None, ge=0, le=99)
    abstain: int | None = Field(default=None, ge=0, le=99)
    ceiling: bool | None = None
    as_of: str | None = Field(default=None, max_length=10)


class ClaimConfirmation(_Strict):
    confirmed: bool = True
    reviewer: str | None = Field(default=None, max_length=120)


def _validation_detail(exc: ValidationError) -> dict:
    errors = []
    for error in exc.errors():
        errors.append({
            'loc': [str(part) for part in error.get('loc', ())],
            'msg': str(error.get('msg', 'invalid value')),
            'type': str(error.get('type', 'value_error')),
        })
    return {'code': 'INVALID_INPUT', 'message': 'Request body failed validation', 'errors': errors}


def _validated(model, payload: dict):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from None


@app.exception_handler(RequestValidationError)
async def _invalid_request(_request: Request, exc: RequestValidationError):
    # Typed, field-located rejection for every FastAPI-bound model; no echo of the submitted value.
    return JSONResponse(status_code=422, content={'detail': _validation_detail(exc)})


async def _read_json_object(request: Request) -> dict:
    """Read a bounded body and decode it ourselves so encoding/size failures are typed."""
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            if int(declared) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail={
                    'code': 'REQUEST_TOO_LARGE',
                    'message': 'Request body exceeds the bounded upload limit',
                    'limit_bytes': MAX_REQUEST_BYTES})
        except ValueError:
            raise HTTPException(status_code=400, detail={'code': 'INVALID_CONTENT_LENGTH',
                                                         'message': 'Content-Length is not an integer'}) from None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail={
                'code': 'REQUEST_TOO_LARGE',
                'message': 'Request body exceeds the bounded upload limit',
                'limit_bytes': MAX_REQUEST_BYTES})
        chunks.append(chunk)
    raw = b''.join(chunks)
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_UTF8', 'message': 'Request body is not valid UTF-8'}) from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_JSON', 'message': 'Request body is not valid JSON'}) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_JSON', 'message': 'Request body must be a JSON object'})
    return payload


def _source_text(source: SourceCreate) -> str:
    if source.text_base64 is None:
        return source.text or ''
    try:
        raw = base64.b64decode(source.text_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_UPLOAD_ENCODING', 'message': 'Upload payload is not valid base64'}) from None
    if len(raw) > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail={
            'code': 'SOURCE_TEXT_TOO_LARGE',
            'message': f'Imported text exceeds {MAX_TEXT_BYTES} UTF-8 bytes',
            'limit_bytes': MAX_TEXT_BYTES})
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_UTF8', 'message': 'Uploaded .txt file is not valid UTF-8'}) from None


def _source_view(source: dict) -> dict:
    capture = source['capture_class']
    return {
        'source_id': source['source_id'],
        'title': source['title'],
        'publisher': source['publisher'],
        'official_url': source['official_url'],
        'document_role': source['document_role'],
        'document_date': source['document_date'],
        'as_of': source['as_of'],
        'anchor': source['anchor'],
        'capture_class': capture,
        'hash_subject': ('ORIGINAL_SOURCE_BYTES' if capture == 'INDEPENDENT_ORIGINAL_BYTES'
                         else 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'),
        'content_sha256': source.get('content_sha256'),
        'raw_sha256': source.get('raw_sha256'),
        'exact_text': source.get('exact_text'),
        'retrieved_at_utc': source['retrieved_at_utc'],
        'review_state': source['review_state'],
        'original_source_verified': bool(source.get('original_source_verified')),
        'independent_audit': source.get('independent_audit', 'PENDING'),
        'provenance_label': source['provenance_label'],
    }


def _fact_view(fact: dict) -> dict:
    view = {
        'fact_id': fact['fact_id'],
        'source_id': fact['source_id'],
        'anchor': fact['anchor'],
        'exact_excerpt': fact['exact_excerpt'],
        'predicate': fact['predicate'],
        'subject_id': fact['subject_id'],
        'action_id': fact['action_id'],
        'jurisdiction_id': fact['jurisdiction_id'],
        'qualifiers': list(fact['qualifiers']),
        'reported_at': fact['reported_at'],
        'review_state': fact['review_state'],
        'reviewed_by': fact.get('reviewed_by'),
        'normal_forms': fact['normal_forms'],
        'created_at_utc': fact['created_at_utc'],
        'updated_at_utc': fact.get('updated_at_utc'),
    }
    for key in ('amount_cents', 'phase', 'event_date', 'yea', 'nay', 'abstain'):
        if key in fact:
            view[key] = fact[key]
    return view


def _claim_view(claim: dict) -> dict:
    view = {
        'claim_id': claim['claim_id'],
        'text': claim['text'],
        'predicate': claim['predicate'],
        'subject_id': claim['subject_id'],
        'action_id': claim['action_id'],
        'jurisdiction_id': claim['jurisdiction_id'],
        'fact_ids': list(claim['fact_ids']),
        'additional_record': claim['additional_record'],
        'confirmation': claim['confirmation'],
        'plain_language': claim['plain_language'],
        'created_at_utc': claim['created_at_utc'],
        'updated_at_utc': claim.get('updated_at_utc'),
    }
    for key in ('amount_cents', 'phase', 'event_date', 'yea', 'nay', 'abstain', 'ceiling', 'as_of'):
        if key in claim:
            view[key] = claim[key]
    return view


def _case_integrity_state(case: dict) -> str:
    if not case['sources']:
        return 'NO_SOURCES_RECORDED'
    classes = {source['capture_class'] for source in case['sources']}
    if classes == {'USER_SUPPLIED_EXCERPT'}:
        return 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED'
    if 'USER_SUPPLIED_EXCERPT' in classes:
        return 'MIXED_CAPTURE__ORIGINAL_BYTES_PARTIAL'
    return 'SOURCE_CAPTURED__INDEPENDENT_AUDIT_PENDING'


def _case_view(case: dict) -> dict:
    return {
        'case_id': case['case_id'],
        'case_title': case['case_title'],
        'project_name': case['project_name'],
        'municipality': case['municipality'],
        'jurisdiction_id': case['jurisdiction_id'],
        'publisher_policy': case['publisher_policy'],
        'revision': case['revision'],
        'state': case_state(case),
        'source_integrity_state': _case_integrity_state(case),
        'created_at_utc': case['created_at_utc'],
        'updated_at_utc': case['updated_at_utc'],
        'audit': case['audit'],
        'provenance': {
            'original_source_verified': False,
            'independent_fact_audit': 'PENDING',
            'note': case['audit']['provenance_note'],
        },
        'sources': [_source_view(source) for source in case['sources']],
        'facts': [_fact_view(fact) for fact in case['facts']],
        'claims': [_claim_view(claim) for claim in case['claims']],
    }


def _court_for_case(case_id: str) -> Court:
    return Court.from_case_store(_store(), case_id)


@app.get('/api/cases')
async def list_cases():
    try:
        return _store().list_cases()
    except CaseStoreError as exc:
        raise _case_http(exc) from None


@app.post('/api/cases', status_code=201)
async def create_case(request: Request):
    body = _validated(CaseCreate, await _read_json_object(request))
    # Decode/validate the first source *before* creating the case, so a bad upload never leaves a
    # half-created shell behind (and so the caller gets the typed upload error, not a store error).
    text = _source_text(body.source) if body.source is not None else None
    store = _store()
    case = None
    try:
        case = store.create_case(
            project_name=body.project_name, municipality=body.municipality,
            case_title=body.case_title, jurisdiction_id=body.jurisdiction_id,
            publisher=body.publisher, allowed_hosts=body.allowed_hosts,
            provenance_note=body.provenance_note)
        source_id = None
        if body.source is not None:
            source = store.add_source(
                case['case_id'], title=body.source.title, publisher=body.source.publisher,
                official_url=body.source.official_url, document_role=body.source.document_role,
                document_date=body.source.document_date, as_of=body.source.as_of,
                anchor=body.source.anchor, text=text,
                capture_class='USER_SUPPLIED_EXCERPT')
            source_id = source['source_id']
        case = store.get_case(case['case_id'])
    except CaseStoreError as exc:
        if case is not None:
            store.discard_case(case['case_id'])
        raise _case_http(exc) from None
    return {'case': _case_view(case), 'created_source_id': source_id}


@app.get('/api/cases/{case_id}')
async def get_case(case_id: str, revision: int | None = Query(default=None, ge=1)):
    store = _store()
    try:
        if revision is None:
            return {'case': _case_view(store.get_case(case_id))}
        entry = store.get_revision(case_id, revision)
        case = store.get_case(case_id)
        return {'case_id': case_id, 'current_revision': case['revision'],
                'revision_entry': entry,
                'revision_is_current': entry['revision'] == case['revision']}
    except CaseStoreError as exc:
        raise _case_http(exc) from None


@app.get('/api/cases/{case_id}/revisions')
async def case_revisions(case_id: str):
    try:
        case = _store().get_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'case_id': case_id, 'revision': case['revision'],
            'retention': 'CASE_CREATED_PLUS_LAST_99_EVENTS',
            'revisions': case['revisions']}


@app.post('/api/cases/{case_id}/sources', status_code=201)
async def add_case_source(case_id: str, request: Request):
    body = _validated(SourceCreate, await _read_json_object(request))
    try:
        source = _store().add_source(
            case_id, title=body.title, publisher=body.publisher,
            official_url=body.official_url, document_role=body.document_role,
            document_date=body.document_date, as_of=body.as_of, anchor=body.anchor,
            text=_source_text(body), capture_class='USER_SUPPLIED_EXCERPT')
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'source': _source_view(source)}


@app.get('/api/cases/{case_id}/sources/{source_id}')
async def get_case_source(case_id: str, source_id: str):
    try:
        case = _store().get_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    for source in case['sources']:
        if source['source_id'] == source_id:
            return {'source': _source_view(source)}
    raise HTTPException(status_code=404, detail={'code': 'SOURCE_NOT_IN_CASE',
                                                 'message': 'Source does not belong to this case'})


@app.patch('/api/cases/{case_id}/sources/{source_id}')
async def patch_case_source(case_id: str, source_id: str, body: SourcePatch):
    try:
        source = _store().update_source(case_id, source_id, body.model_dump(exclude_unset=True))
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'source': _source_view(source)}


@app.delete('/api/cases/{case_id}/sources/{source_id}')
async def delete_case_source(case_id: str, source_id: str):
    try:
        return _store().delete_source(case_id, source_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None


@app.post('/api/cases/{case_id}/facts', status_code=201)
async def add_case_fact(case_id: str, body: FactCreate):
    try:
        fact = _store().add_fact(
            case_id, source_id=body.source_id, anchor=body.anchor,
            exact_excerpt=body.exact_excerpt, predicate=body.predicate,
            subject_id=body.subject_id, action_id=body.action_id,
            qualifiers=body.qualifiers, review_state=body.review_state,
            reviewed_by=body.reviewed_by, amount_cents=body.amount_cents,
            phase=body.phase, event_date=body.event_date, yea=body.yea, nay=body.nay,
            abstain=body.abstain)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'fact': _fact_view(fact)}


@app.patch('/api/cases/{case_id}/facts/{fact_id}')
async def patch_case_fact(case_id: str, fact_id: str, body: FactPatch):
    try:
        fact = _store().update_fact(case_id, fact_id, body.model_dump(exclude_unset=True))
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'fact': _fact_view(fact)}


@app.delete('/api/cases/{case_id}/facts/{fact_id}')
async def delete_case_fact(case_id: str, fact_id: str):
    try:
        return _store().delete_fact(case_id, fact_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None


@app.post('/api/cases/{case_id}/claims', status_code=201)
async def add_case_claim(case_id: str, body: ClaimCreate):
    try:
        claim = _store().add_claim(
            case_id, text=body.text, predicate=body.predicate,
            subject_id=body.subject_id, action_id=body.action_id,
            fact_ids=body.fact_ids, additional_record=body.additional_record,
            amount_cents=body.amount_cents, phase=body.phase, event_date=body.event_date,
            yea=body.yea, nay=body.nay, abstain=body.abstain, ceiling=body.ceiling,
            as_of=body.as_of)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'claim': _claim_view(claim)}


@app.patch('/api/cases/{case_id}/claims/{claim_id}')
async def patch_case_claim(case_id: str, claim_id: str, body: ClaimPatch):
    try:
        claim = _store().update_claim(case_id, claim_id, body.model_dump(exclude_unset=True))
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'claim': _claim_view(claim)}


@app.post('/api/cases/{case_id}/claims/{claim_id}/confirm')
async def confirm_case_claim(case_id: str, claim_id: str, body: ClaimConfirmation):
    try:
        claim = _store().confirm_claim(case_id, claim_id, confirmed=body.confirmed,
                                       reviewer=body.reviewer)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return {'claim': _claim_view(claim)}


@app.delete('/api/cases/{case_id}/claims/{claim_id}')
async def delete_case_claim(case_id: str, claim_id: str):
    try:
        return _store().delete_claim(case_id, claim_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None


@app.post('/api/cases/{case_id}/challenge')
async def challenge_case_claim(case_id: str, body: RequestId):
    try:
        case_court = _court_for_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    except CourtValidationError as exc:
        raise _court_http(exc) from None
    if body.claim_id not in case_court.claims:
        raise HTTPException(status_code=404, detail={
            'code': 'UNKNOWN_CASE_CLAIM',
            'message': 'No such claim in this case; unreviewed statements need a human review first'})
    return case_court.evaluate(body.claim_id).as_json()


# --------------------------------------------------------------------------------------------
# Selected-case live investigation (CT-QC-064). Every gate below refuses *before* the durable
# reservation that authorises a provider dispatch, and the admitted run itself is the shared
# path above -- the same budget, reservation, pre-dispatch marker and settlement objects the
# shipped sample route uses. No second accounting path, no direct agent call, no reset/refund.
# --------------------------------------------------------------------------------------------

#: The order a selected-case run follows. Gates 1-7 refuse before money can move; gates 8-9 are
#: the existing durable admission and dispatch steps, reached only if every gate above passed.
CASE_RUN_GATE_ORDER = (
    'CASE_EXISTS',
    'REVISION_IS_CURRENT',
    'CLAIM_IN_SELECTED_CASE',
    'CLAIM_CONFIRMED_BY_A_HUMAN',
    'COURT_BOUND_TO_PINNED_CASE_REVISION',
    'TWO_DISTINCT_REVIEWED_SOURCE_ROLES',
    'REVISION_STILL_PINNED_BEFORE_RESERVATION',
    'DURABLE_ADMISSION_ON_CONFIGURED_BACKEND',
    'PROVIDER_DISPATCH_THROUGH_THE_ADMITTED_RUN',
)


class CaseInvestigationRequest(_Strict):
    """A selected case, one immutable revision, one human-confirmed claim. Nothing else."""

    claim_id: str = Field(pattern=_ID_PATTERN)
    revision: int = Field(ge=1, le=10_000_000)


def _stale_revision(case_id: str, requested: int, current: int) -> HTTPException:
    return HTTPException(status_code=409, detail={
        'code': 'STALE_CASE_REVISION',
        'message': 'The selected case revision is no longer current. Re-read the case and select '
                   'the confirmed claim again; nothing was reserved and no provider was called.',
        'case_id': case_id,
        'requested_revision': requested,
        'current_revision': current,
    })


def _pinned_case_for_investigation(case_id: str, revision: int) -> dict:
    """Gate 1-2: the case exists and the caller pinned the *current* immutable revision."""
    try:
        case = _store().get_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    if case['revision'] != revision:
        raise _stale_revision(case_id, revision, case['revision'])
    return case


def _confirmed_case_claim(case: dict, claim_id: str) -> dict:
    """Gate 3-4: the claim lives in *this* case and a human confirmed it."""
    claim = next((item for item in case['claims'] if item['claim_id'] == claim_id), None)
    if claim is None:
        raise HTTPException(status_code=404, detail={
            'code': 'CLAIM_NOT_IN_CASE',
            'message': 'No such claim in the selected case. A claim from another case is never '
                       'resolved here; nothing was reserved and no provider was called.',
        })
    if claim['confirmation']['state'] != 'CONFIRMED':
        raise HTTPException(status_code=409, detail={
            'code': 'CLAIM_NOT_CONFIRMED',
            'message': 'Only a claim a human has confirmed in this case may be investigated. '
                       'Nothing was reserved and no provider was called.',
            'claim_id': claim_id,
            'confirmation_state': claim['confirmation']['state'],
        })
    return claim


def _case_court_for_pinned_case(case: dict):
    """Gate 5: one Court built from the pinned revision's own registry and claim book."""
    try:
        return Court(case_registry(case), case_claim_book(case))
    except CourtValidationError as exc:
        raise _court_http(exc) from None


def prepare_case_investigation(case_id: str, claim_id: str, revision: int) -> dict:
    """Run every pre-reservation gate for a selected-case investigation, in order.

    Returns the pinned case, the confirmed claim, the case-bound Court and the derived live
    eligibility. Raises a typed :class:`fastapi.HTTPException` instead when any gate refuses.
    Pure with respect to accounting: it never opens the spend backend and never constructs an
    agent, so a refusal here cannot reserve budget or reach a provider.
    """
    case = _pinned_case_for_investigation(case_id, revision)
    claim = _confirmed_case_claim(case, claim_id)
    case_court = _case_court_for_pinned_case(case)
    eligibility = live_eligibility(case_court)
    if not eligibility['eligible']:
        raise HTTPException(status_code=409, detail={
            'code': LIVE_ELIGIBILITY_REFUSAL,
            'message': 'Live inference is refused before any reservation: a selected-case run '
                       'needs a reviewed fact mapping on an adopted-action-minutes source AND on '
                       'a city-informational-tracker source, each read through its own tool. '
                       'No provider was called and nothing was reserved.',
            'case_id': case_id,
            'revision': case['revision'],
            'live_eligibility': eligibility,
        })
    # Gate 7: the Court above was built from the pinned copy, so a case that moved while it was
    # being built must not be dispatched against a revision the caller did not select.
    try:
        current = _store().get_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    if current['revision'] != case['revision']:
        raise _stale_revision(case_id, case['revision'], current['revision'])
    return {'case': case, 'claim': claim, 'court': case_court, 'eligibility': eligibility}


def _case_run_provenance(binding: dict) -> dict:
    """Honest provenance for a selected-case run: which case, which revision, which claim."""
    case = binding['case']
    claim = binding['claim']
    return {
        'case_id': case['case_id'],
        'revision': case['revision'],
        'claim_id': claim['claim_id'],
        'claim_predicate': claim['predicate'],
        'claim_confirmation': {
            'state': claim['confirmation']['state'],
            'content_sha256': claim['confirmation'].get('content_sha256'),
            'evidence_sha256': claim['confirmation'].get('evidence_sha256'),
            'reviewer_recorded': bool(claim['confirmation'].get('reviewer')),
        },
        'case_registry_sha256': binding['court'].registry_sha256,
        'live_eligibility': binding['eligibility'],
        'case_isolation': 'COURT_BOUND_TO_THE_SELECTED_CASE_ONLY',
        'reservation_order': list(CASE_RUN_GATE_ORDER),
        'provenance': {
            'original_source_verified': False,
            'independent_fact_audit': 'PENDING',
            'capture_basis': case['audit']['capture_basis'],
            'hash_subject': 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES',
            'note': 'Imported text is not authenticated original bytes; any finding is preliminary.',
        },
    }


def case_investigation_preview(case_id: str, claim_id: str | None = None) -> dict:
    """What a selected-case run would investigate, and which gate would block it.

    Reads the store and derives eligibility only: it opens no spend backend, constructs no agent,
    and makes no request. This is what the workbench page renders while live activation is off.
    """
    case = _store().get_case(case_id)
    court_error = None
    case_court = None
    eligibility = None
    try:
        case_court = _case_court_for_pinned_case(case)
        eligibility = live_eligibility(case_court)
    except HTTPException as exc:
        court_error = exc.detail if isinstance(exc.detail, dict) else {'code': 'COURT_REFUSED'}
    confirmed = [item for item in case['claims'] if item['confirmation']['state'] == 'CONFIRMED']
    selected = None
    if claim_id:
        selected = next((item for item in case['claims'] if item['claim_id'] == claim_id), None)
    if selected is None and confirmed:
        selected = confirmed[0]
    owner_enabled = os.getenv('CIVICTRACE_LIVE_ENABLED') == '1'
    gates = [
        {'gate': 'CASE_EXISTS', 'ok': True},
        {'gate': 'REVISION_PINNED_IS_CURRENT', 'ok': True},
        {'gate': 'CLAIM_SELECTED', 'ok': selected is not None},
        {'gate': 'CLAIM_CONFIRMED_BY_A_HUMAN',
         'ok': bool(selected and selected['confirmation']['state'] == 'CONFIRMED')},
        {'gate': 'COURT_BOUND_TO_PINNED_CASE_REVISION', 'ok': court_error is None},
        {'gate': 'TWO_DISTINCT_REVIEWED_SOURCE_ROLES',
         'ok': bool(eligibility and eligibility['eligible'])},
        {'gate': 'LIVE_OWNER_AUTHORIZATION', 'ok': owner_enabled},
    ]
    return {
        'case_id': case['case_id'],
        'case_title': case['case_title'],
        'revision': case['revision'],
        'case_state': case_state(case),
        'claim_id': selected['claim_id'] if selected else None,
        'claim_confirmation_state': (selected['confirmation']['state'] if selected else None),
        'claim_predicate': selected['predicate'] if selected else None,
        'claims': [{'claim_id': item['claim_id'], 'predicate': item['predicate'],
                    'text': item['text'],
                    'confirmation_state': item['confirmation']['state'],
                    'would_run': item['confirmation']['state'] == 'CONFIRMED'}
                   for item in case['claims']],
        'eligibility': eligibility,
        'court_error': court_error,
        'live_activation': {
            'owner_enabled': owner_enabled,
            'state': ('OWNER_ENABLED' if owner_enabled
                      else 'DISABLED_PENDING_OWNER_AUTHORIZATION'),
            'provider_configured': bool(os.getenv('NEBIUS_API_KEY')),
            'detail': ('Live inference is disabled in this build: no provider credential is read '
                       'and no request is sent. Enabling it is an owner action, not a UI toggle.'),
        },
        'case_isolation': 'THIS RUN CAN READ ONLY RECORDS REGISTERED IN THIS CASE',
        'reservation_order': list(CASE_RUN_GATE_ORDER),
        'gates': gates,
        'provenance': {
            'original_source_verified': False,
            'independent_fact_audit': 'PENDING',
            'hash_subject': 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES',
        },
        'case_registry_sha256': case_court.registry_sha256 if case_court is not None else None,
    }


async def _run_selected_case_from_ui(request: Request, case_id: str, claim_id: str,
                                     revision: int) -> dict:
    """The workbench form's entry into the selected-case path (same gates, same admitted run)."""
    binding = prepare_case_investigation(case_id, claim_id, revision)
    run = await _admitted_investigation(binding['court'], claim_id, request)
    return {**run, 'case_investigation': _case_run_provenance(binding)}


@app.post('/api/cases/{case_id}/investigate')
async def investigate_case(case_id: str, request: Request):
    """Selected-case investigate route: gates first, then the existing admitted run path."""
    body = _validated(CaseInvestigationRequest, await _read_json_object(request))
    binding = prepare_case_investigation(case_id, body.claim_id, body.revision)
    run = await _admitted_investigation(binding['court'], body.claim_id, request)
    return {**run, 'case_investigation': _case_run_provenance(binding)}


def _render_case_page(case: dict) -> str:
    """Minimal server-rendered case page. Every value is escaped; source text is inert data."""
    def e(value) -> str:
        return html.escape(str(value), quote=True)

    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<title>', e(case['case_title']), '</title></head><body>',
        '<p><a href="/api/cases">All local cases</a></p>',
        '<h1>', e(case['case_title']), '</h1>',
        '<p>', e(case['municipality']), ' — ', e(case['project_name']),
        ' — revision ', e(case['revision']), ' — ', e(case_state(case)), '</p>',
        '<p><strong>Provenance:</strong> ', e(_case_integrity_state(case)),
        '. Original source verified: no. Independent fact audit: pending.</p>',
        '<h2>Sources</h2>',
    ]
    if not case['sources']:
        parts.append('<p>No sources recorded yet.</p>')
    for source in case['sources']:
        parts.extend([
            '<section><h3>', e(source['title']), '</h3>',
            '<p>', e(source['publisher']), ' — ', e(source['document_role']),
            ' — as of ', e(source['as_of']), ' — ', e(source['provenance_label']), '</p>',
            '<p>Declared location (never fetched): ', e(source['official_url']), '</p>',
            '<p>Anchor: ', e(source['anchor']), '</p>',
            '<pre>', html.escape(source.get('exact_text') or '(original bytes not imported)', quote=False),
            '</pre></section>',
        ])
    parts.append('<h2>Fact mappings</h2>')
    if not case['facts']:
        parts.append('<p>No fact mappings recorded yet.</p>')
    for fact in case['facts']:
        parts.extend([
            '<section><h3>', e(fact['predicate']), '</h3>',
            '<p>Review state: ', e(fact['review_state']), ' — anchor: ', e(fact['anchor']), '</p>',
            '<blockquote>', html.escape(fact['exact_excerpt'], quote=False), '</blockquote>',
        ])
    parts.append('<h2>Claims</h2>')
    if not case['claims']:
        parts.append('<p>No claims recorded yet.</p>')
    for claim in case['claims']:
        parts.extend([
            '<section><h3>', e(claim['text']), '</h3>',
            '<p>Confirmation: ', e(claim['confirmation']['state']),
            ' — predicate: ', e(claim['predicate']), '</p></section>',
        ])
    parts.append('</body></html>')
    return ''.join(parts)


@app.get('/cases/{case_id}')
async def case_page(case_id: str):
    try:
        case = _store().get_case(case_id)
    except CaseStoreError as exc:
        raise _case_http(exc) from None
    return HTMLResponse(_render_case_page(case))


# --------------------------------------------------------------------------------------------
# Server-rendered case workbench UI (CT-WB2). These routes add no capability: they call the same
# store and the same case-scoped Court as ``/api/cases``. The store accessor is bound rather than
# imported so this module stays the only owner of the store path and its cache.
# --------------------------------------------------------------------------------------------
workbench_ui.bind_store(_store, max_form_bytes=MAX_REQUEST_BYTES)
# The workbench's live panel and its one form post call back into this module: the gates and the
# admitted run live here, so the UI cannot reach a provider by another route (CT-QC-064).
workbench_ui.bind_investigation(preview=case_investigation_preview,
                                runner=_run_selected_case_from_ui)
app.add_exception_handler(workbench_ui.UiError, workbench_ui.ui_error_handler)
app.include_router(workbench_ui.router)
