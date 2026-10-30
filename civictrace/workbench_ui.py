"""Server-rendered case workbench UI (CT-WB2, part 2 of the reusable workbench).

Why this module exists
----------------------
Part 1 (`civictrace/case_store.py`, `Court.from_case_store`, `/api/cases`) made cases generic but
only exposed a minimal read-only page plus a JSON API. An ordinary owner cannot run a case through
a JSON API. This module adds the ordinary workflow as plain server-rendered HTML forms:

* list cases and create one (paste a textarea, or pick a bounded ``.txt`` file),
* review one source: exact preserved text, hash provenance, an explicit
  ``ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED`` banner,
* map a fact: exact excerpt, server-derived normal forms, and the two human review transitions,
* build a claim from a plain-language template with the confirmation question attached,
* run the case-scoped investigation and read every dynamic citation.

Rules encoded here
------------------
* Nothing is fetched. A declared source URL is printed, labelled as never fetched, and never
  resolved; the only outbound capability in this process is the separately gated live path.
* Every dynamic value is HTML-escaped. Source text is rendered inside ``<pre>`` as inert data. A
  document that contains ``<script>`` or "ignore previous instructions" is data, not an
  instruction, and no rendering path interprets it.
* No inline styles or scripts are emitted: the shipped CSP (``style-src 'self'; script-src
  'self'``) is not weakened. The one script on these pages is the self-hosted
  ``/assets/workbench.js`` file-input helper.
* Forms are ``application/x-www-form-urlencoded``. Multipart would need a new runtime dependency,
  so the file picker reads the ``.txt`` locally and posts its exact bytes base64-encoded into the
  shape the JSON API already accepts. A picked file that never reached the server is a typed,
  visible refusal (``UPLOAD_NEEDS_SCRIPT``), never a silent empty import.
* A refusal renders an HTML page carrying the same typed code the JSON route returns. No route
  claims acceptance; every page repeats that imported text is not authenticated original bytes.
"""
from __future__ import annotations

import json

import base64
import binascii
import html
import re
import urllib.parse
from contextlib import contextmanager
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .case_store import (
    MAX_TEXT_BYTES,
    CaseStore,
    CaseStoreError,
    case_state,
)
from .court import (
    ALLOWED_DOCUMENT_ROLES,
    ALLOWED_PREDICATES,
    SOURCE_ROLE_BY_PREDICATE,
    Court,
    CourtValidationError,
)

router = APIRouter()

#: Overwritten by ``bind_store`` from ``app.MAX_REQUEST_BYTES`` so the two body bounds cannot drift.
MAX_FORM_BYTES = 512 * 1024
#: A form carries at most this many fields; anything larger is refused before parsing.
MAX_FORM_FIELDS = 200

_PROVENANCE_BANNER = (
    'ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED — imported text is not an authenticated original '
    'document. The SHA-256 shown hashes the imported text bytes only. Independent fact audit: '
    'PENDING. No page on this workbench establishes that a document is authentic.'
)

#: Stable display order for the predicate pickers (``ALLOWED_PREDICATES`` is a frozenset).
PREDICATE_ORDER = (
    'AUTHORIZED_USE_CEILING', 'DESIGN_AUTHORIZED', 'VOTE_APPROVED', 'EVENT_CLOSURE_APPROVED',
    'PAID_AMOUNT', 'CONSTRUCTION_STARTED', 'PHYSICAL_COMPLETED', 'CITY_REPORTS_PHASE',
    'NEVER_BUILT',
)

#: predicate -> (plain-language confirmation question, why the distinction matters)
PREDICATE_QUESTIONS = {
    'AUTHORIZED_USE_CEILING': (
        'Does this excerpt authorize a spending ceiling?',
        'An authorization is not a payment. Record the UP_TO qualifier when the record says so.'),
    'DESIGN_AUTHORIZED': (
        'Does this excerpt authorize design work?',
        'Design authorization is not a construction notice.'),
    'VOTE_APPROVED': (
        'Does this excerpt record the vote counts you entered?',
        'Enter yea, nay and abstain exactly as the record states them.'),
    'EVENT_CLOSURE_APPROVED': (
        'Does this excerpt approve one specific dated event closure?',
        'An event closure is a separate council action, even on the same street.'),
    'PAID_AMOUNT': (
        'Does this excerpt come from a payment register and record money actually paid?',
        'An authorized ceiling is never evidence of expenditure.'),
    'CONSTRUCTION_STARTED': (
        'Does this excerpt come from a construction record that says work started?',
        'An unrelated event on the same street is not a construction record.'),
    'PHYSICAL_COMPLETED': (
        'Does this excerpt come from an acceptance record that says the work was completed?',
        'A project tracker status is a report, not physical verification.'),
    'CITY_REPORTS_PHASE': (
        'Does this excerpt report what the city says, rather than a field inspection?',
        'A reported phase is a city statement; it is not an inspection finding.'),
    'NEVER_BUILT': (
        'Does this excerpt come from an adjudicated negative-evidence record?',
        'The absence of a record is not proof that something never happened.'),
}

_REVIEW_STATES = ('PENDING_HUMAN_REVIEW', 'HUMAN_REVIEWED', 'SOURCE_REVISED_REVIEW_REQUIRED')
_REVIEWABLE_TARGETS = ('HUMAN_REVIEWED', 'PENDING_HUMAN_REVIEW')
_REF_SPLIT = re.compile(r'[,\s]+')
_DIGITS = re.compile(r'^\d{1,12}$')

_store_provider: Callable[[], CaseStore] | None = None
_preview_provider: Callable[[str, str | None], dict] | None = None
_run_provider: Callable | None = None


def bind_store(provider: Callable[[], CaseStore], *, max_form_bytes: int | None = None) -> None:
    """Bind the process store accessor (called once from ``civictrace.app``)."""
    global _store_provider, MAX_FORM_BYTES
    _store_provider = provider
    if max_form_bytes is not None:
        MAX_FORM_BYTES = int(max_form_bytes)


def bind_investigation(*, preview: Callable[[str, str | None], dict],
                       runner: Callable) -> None:
    """Bind the selected-case live path, which lives in ``civictrace.app`` (CT-QC-064).

    The preview renders what a run *would* do; the runner performs the gates and then the
    existing admitted run. Binding rather than importing keeps this module free of the
    accounting code (and keeps the app the only owner of the spend backend and the provider
    client), so the workbench page has no second route to a provider.
    """
    global _preview_provider, _run_provider
    _preview_provider = preview
    _run_provider = runner


def _store() -> CaseStore:
    if _store_provider is None:  # pragma: no cover - app.py always binds before serving
        raise RuntimeError('workbench store provider is not bound')
    return _store_provider()


# -- typed refusals -----------------------------------------------------------------------------

class UiError(Exception):
    """A refusal that must be rendered as an HTML page with a stable code."""

    def __init__(self, status: int, code: str, message: str, back: str | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.back = back if back and back.startswith('/workbench') else '/workbench'


def ui_error_handler(_request: Request, exc: UiError) -> HTMLResponse:
    return _error_page(exc.status, exc.code, exc.message, exc.back)


@contextmanager
def _ui_errors(back: str):
    """Translate every typed store/court/form refusal into one rendered HTML error page."""
    try:
        yield
    except UiError:
        raise
    except CaseStoreError as exc:
        raise UiError(exc.status, exc.code, str(exc), back) from None
    except CourtValidationError as exc:
        raise UiError(422, exc.code, str(exc), back) from None
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get('code') or 'REQUEST_REFUSED')
            message = str(detail.get('message') or detail)
        else:
            code = 'REQUEST_REFUSED'
            message = str(detail)
        raise UiError(exc.status_code, code, message, back) from None


# -- form reading -------------------------------------------------------------------------------

async def _read_form(request: Request) -> dict[str, list[str]]:
    """Read one bounded urlencoded body; encoding and size failures are typed."""
    content_type = (request.headers.get('content-type') or '').split(';')[0].strip().lower()
    if content_type != 'application/x-www-form-urlencoded':
        raise UiError(415, 'UNSUPPORTED_FORM_ENCODING',
                      'Workbench forms post application/x-www-form-urlencoded only.')
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise UiError(400, 'INVALID_CONTENT_LENGTH', 'Content-Length is not an integer.') from None
        if declared_size > MAX_FORM_BYTES:
            raise UiError(413, 'REQUEST_TOO_LARGE',
                          f'Request body exceeds the bounded form limit of {MAX_FORM_BYTES} bytes.')
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_FORM_BYTES:
            raise UiError(413, 'REQUEST_TOO_LARGE',
                          f'Request body exceeds the bounded form limit of {MAX_FORM_BYTES} bytes.')
        chunks.append(chunk)
    raw = b''.join(chunks)
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise UiError(422, 'INVALID_UTF8', 'Form body is not valid UTF-8.') from None
    try:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True,
                                       max_num_fields=MAX_FORM_FIELDS, errors='strict')
    except ValueError:
        raise UiError(422, 'TOO_MANY_FORM_FIELDS',
                      f'A form may carry at most {MAX_FORM_FIELDS} fields.') from None
    form: dict[str, list[str]] = {}
    for key, value in pairs:
        if len(key) > 100:
            raise UiError(422, 'INVALID_FORM_FIELD', 'A form field name is too long.')
        form.setdefault(key, []).append(value)
    return form


def _one(form: dict[str, list[str]], key: str) -> str | None:
    values = form.get(key)
    return values[-1] if values else None


def _many(form: dict[str, list[str]], key: str) -> list[str]:
    return list(form.get(key) or [])


def _meta(form: dict[str, list[str]], key: str, *, required: bool = True,
          maximum: int | None = None) -> str | None:
    raw = _one(form, key)
    value = (raw or '').strip()
    if not value:
        if required:
            raise UiError(422, 'INVALID_INPUT', f'{key} is required.')
        return None
    if maximum is not None and len(value) > maximum:
        raise UiError(422, 'INVALID_INPUT', f'{key} must be at most {maximum} characters.')
    return value


def _exact_text(form: dict[str, list[str]], key: str) -> str | None:
    """Exact (unstripped) text: imported source text must be preserved byte for byte."""
    values = form.get(key)
    return values[-1] if values else None


def _int_field(form: dict[str, list[str]], key: str) -> int | None:
    raw = (_one(form, key) or '').strip()
    if not raw:
        return None
    if not _DIGITS.match(raw):
        raise UiError(422, 'INVALID_INPUT', f'{key} must be a non-negative integer.')
    return int(raw)


def _bool_field(form: dict[str, list[str]], key: str) -> bool:
    return (_one(form, key) or '').strip().lower() in ('1', 'yes', 'on', 'true')


def _ref_list(form: dict[str, list[str]], key: str) -> list[str]:
    raw = (_one(form, key) or '').strip()
    if not raw:
        return []
    return [item for item in _REF_SPLIT.split(raw) if item]


def _upload_text(form: dict[str, list[str]]) -> str | None:
    """Return imported text from a textarea or a picked ``.txt`` file, else ``None``."""
    encoded = (_one(form, 'text_base64') or '').strip()
    filename = (_one(form, 'filename') or '').strip()
    picked = (_one(form, 'source_file') or '').strip()
    if encoded:
        if not filename.lower().endswith('.txt'):
            raise UiError(422, 'INVALID_INPUT',
                          'A file upload must declare a .txt filename; other file types are not imported.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise UiError(422, 'INVALID_UPLOAD_ENCODING', 'Upload payload is not valid base64.') from None
        if len(raw) > MAX_TEXT_BYTES:
            raise UiError(413, 'SOURCE_TEXT_TOO_LARGE',
                          f'Imported text exceeds {MAX_TEXT_BYTES} UTF-8 bytes.')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raise UiError(422, 'INVALID_UTF8', 'Uploaded text is not valid UTF-8.') from None
    if picked:
        raise UiError(422, 'UPLOAD_NEEDS_SCRIPT',
                      'A file was selected but its bytes never reached the server: this preview '
                      'transmits a .txt file with a small local script, and that script did not run. '
                      'Paste the text into the box instead, or enable JavaScript for this page.')
    return _exact_text(form, 'text')


def _source_kwargs(form: dict[str, list[str]], *, publisher_default: str | None) -> dict:
    publisher = _meta(form, 'source_publisher', required=False, maximum=200) or publisher_default
    if not publisher:
        raise UiError(422, 'INVALID_INPUT', 'source publisher is required.')
    text = _upload_text(form)
    if text is None:
        raise UiError(422, 'EMPTY_SOURCE_TEXT',
                      'No source text was supplied: paste the exact text or pick a .txt file.')
    return {
        'title': _meta(form, 'source_title', maximum=300),
        'publisher': publisher,
        'official_url': _meta(form, 'official_url', maximum=1000),
        'document_role': _meta(form, 'document_role', maximum=80),
        'document_date': _meta(form, 'document_date', maximum=10),
        'as_of': _meta(form, 'as_of', maximum=10),
        'anchor': _meta(form, 'source_anchor', maximum=500),
        'text': text,
    }


# -- small rendering helpers --------------------------------------------------------------------

def _e(value) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def _options(values, selected: str | None) -> list[str]:
    parts = []
    for value in values:
        parts.extend(['<option value="', _e(value), '"',
                      ' selected' if value == selected else '', '>', _e(value), '</option>'])
    return parts


def _status_class(status: str) -> str:
    if status == 'DOCUMENTED_ACTION':
        return 'support'
    if status == 'CITY_REPORTED_STATUS':
        return 'reported'
    if status in ('WRONG_ACTION_SOURCE', 'CONTRADICTED_BY_RECORD'):
        return 'conflict'
    return 'refuse'


def _shell(title: str, body: list[str], *, banner: str = _PROVENANCE_BANNER,
           status: int = 200) -> HTMLResponse:
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>', _e(title), ' · CivicTrace case workbench</title>',
        '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">',
        '<link rel="stylesheet" href="/assets/site.css">',
        '<script src="/assets/workbench.js" defer></script>',
        '</head><body><div class="app-shell">',
        '<header class="topbar"><div class="wordmark">',
        '<span class="glyph" aria-hidden="true">◇</span> CIVIC<span>TRACE</span>',
        '<small> / CASE WORKBENCH</small></div>',
        '<div class="buildtag">LOCAL ONLY <span class="little-dot"></span> SOURCE AUDIT PENDING</div>',
        '</header><main id="primary">',
        '<p><a href="/workbench">All local cases</a> · '
        '<a href="/">Exeter sample preview</a></p>',
    ]
    if banner:
        parts.extend(['<p class="notice" role="status">', banner, '</p>'])
    parts.extend(body)
    parts.append('</main><footer><span>◇ CIVICTRACE · LOCAL CASE WORKBENCH</span>'
                 '<span>IMPORTED TEXT IS NOT AUTHENTICATED ORIGINAL BYTES</span></footer>'
                 '</div></body></html>')
    return HTMLResponse(''.join(parts), status_code=status)


def _error_page(status: int, code: str, message: str, back: str | None) -> HTMLResponse:
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>!</span> REQUEST REFUSED',
        '<span class="right-label">', _e(code), '</span></div>',
        '<h1>This action was refused.</h1>',
        '<p class="notice" role="alert">', _e(message), '</p>',
        '<p class="reason">status=', _e(status), ' · code=', _e(code), '</p>',
        '<p class="muted">A refusal is typed: the same code appears on the JSON route for this '
        'resource. Nothing was written for a refused mutation unless the message says otherwise.</p>',
        '<p><a href="', _e(back or '/workbench'), '">Back</a></p>',
        '</section>',
    ]
    return _shell(f'Refused: {code}', body, status=status)


# -- pages -------------------------------------------------------------------------------------

def _case_links(case: dict) -> list[str]:
    case_id = case['case_id']
    return [
        '<p><a href="/workbench/cases/', _e(case_id), '/claims/new">New claim from a template</a> · '
        '<a href="/workbench/cases/', _e(case_id), '/investigate">Run the case-scoped investigation</a></p>',
    ]


def _case_header(case: dict) -> list[str]:
    policy = case['publisher_policy']
    return [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> CASE ',
        '<span class="right-label">revision ', _e(case['revision']), '</span></div>',
        '<h1>', _e(case['case_title']), '</h1>',
        '<p>', _e(case['municipality']), ' — ', _e(case['project_name']), '</p>',
        '<p class="muted">State: ', _e(case_state(case)),
        ' · source integrity: ', _e(_integrity_state(case)),
        ' · jurisdiction: ', _e(case['jurisdiction_id']),
        ' · publisher policy: ', _e(policy['publisher']),
        ' (hosts: ', _e(', '.join(policy['allowed_hosts'])), ')</p>',
        '<p class="muted">', _e(case['audit']['provenance_note']), '</p>',
        '<p class="reason">original_source_verified=false · independent_fact_audit=',
        _e(case['audit']['independent_fact_audit']),
        ' · capture_basis=', _e(case['audit']['capture_basis']),
        ' · revision history keeps the creation record and the most recent 99 events</p>',
        '</section>',
    ]


def _integrity_state(case: dict) -> str:
    if not case['sources']:
        return 'NO_SOURCES_RECORDED'
    capture = {source['capture_class'] for source in case['sources']}
    if capture == {'USER_SUPPLIED_EXCERPT'}:
        return 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED'
    if 'USER_SUPPLIED_EXCERPT' in capture:
        return 'MIXED_CAPTURE__ORIGINAL_BYTES_PARTIAL'
    return 'SOURCE_CAPTURED__INDEPENDENT_AUDIT_PENDING'


def _sources_block(case: dict) -> list[str]:
    parts = ['<section class="panel">',
             '<div class="section-label"><span>02</span> SOURCES',
             '<span class="right-label">', _e(len(case['sources'])), ' recorded</span></div>']
    if not case['sources']:
        parts.append('<p class="muted">No sources recorded yet. This is a draft: nothing is '
                     'invented to fill the gap.</p>')
    for source in case['sources']:
        parts.extend([
            '<div class="source">',
            '<span class="source-id">', _e(source['capture_class']), '</span>',
            '<strong><a href="/workbench/cases/', _e(case['case_id']), '/sources/',
            _e(source['source_id']), '">', _e(source['title']), '</a></strong>',
            '<small>', _e(source['publisher']), ' · ', _e(source['document_role']),
            ' · document date ', _e(source['document_date']), ' · as of ', _e(source['as_of']),
            ' · anchor ', _e(source['anchor']), '</small>',
            '<small>Declared location (never fetched): ', _e(source['official_url']), '</small>',
            '<small>Imported bytes SHA-256: ', _e(source.get('content_sha256') or 'none'),
            ' · original-bytes hash: ', _e(source.get('raw_sha256') or 'none'), '</small>',
            '</div>',
        ])
    parts.extend([
        '</section>',
        '<section class="panel">',
        '<div class="section-label"><span>03</span> ADD A SOURCE',
        '<span class="right-label">.txt UTF-8 · bounded</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/sources" '
        'data-upload-form="1">',
        '<p><label for="source_title">Title</label><br>'
        '<input id="source_title" name="source_title" maxlength="300" size="60"></p>',
        '<p><label for="official_url">Declared official URL (https, host in this case policy; '
        'never fetched)</label><br>'
        '<input id="official_url" name="official_url" maxlength="1000" size="60"></p>',
        '<p><label for="document_role">Document role</label><br><select id="document_role" '
        'name="document_role">', *_options(sorted(ALLOWED_DOCUMENT_ROLES), None), '</select></p>',
        '<p><label for="document_date">Document date (YYYY-MM-DD)</label><br>'
        '<input id="document_date" name="document_date" maxlength="10" size="12"> '
        '<label for="as_of">As-of date</label> '
        '<input id="as_of" name="as_of" maxlength="10" size="12"></p>',
        '<p><label for="source_anchor">Anchor</label><br>'
        '<input id="source_anchor" name="source_anchor" maxlength="500" size="60"></p>',
        '<p><label for="text">Paste the exact text</label><br>'
        '<textarea id="text" name="text" rows="8" cols="80"></textarea></p>',
        '<p><label for="source_file">…or pick a .txt file (UTF-8, at most ', _e(MAX_TEXT_BYTES),
        ' bytes)</label><br>'
        '<input id="source_file" name="source_file" type="file" accept=".txt,text/plain">'
        '<input type="hidden" name="filename"><input type="hidden" name="text_base64">'
        '<br><small data-upload-status></small></p>',
        '<p class="muted">A picked file is read in this page and posted to this same origin; no '
        'address is fetched and nothing is sent anywhere else.</p>',
        '<p><button class="primary-btn" type="submit">Add source</button></p>',
        '</form></section>',
    ])
    return parts


def _facts_block(case: dict) -> list[str]:
    parts = ['<section class="panel">',
             '<div class="section-label"><span>04</span> FACT MAPPINGS',
             '<span class="right-label">review is a human action</span></div>']
    if not case['facts']:
        parts.append('<p class="muted">No fact mappings recorded yet. Map an exact excerpt on a '
                     'source page; an excerpt that does not occur in the imported text is refused.</p>')
    for fact in case['facts']:
        parts.extend([
            '<div class="rule"><span class="index">', _e(fact['review_state'][:1]), '</span><div>',
            '<b><a href="/workbench/cases/', _e(case['case_id']), '/facts/', _e(fact['fact_id']),
            '">', _e(fact['predicate']), '</a></b>',
            '<small>', _e(fact['fact_id']), ' · review state ', _e(fact['review_state']),
            ' · anchor ', _e(fact['anchor']), '</small>',
            '<small>', _e(fact['exact_excerpt']), '</small>',
            '</div></div>',
        ])
    parts.append('</section>')
    return parts


def _claim_citation_lines(case: dict) -> list[str]:
    """Cite each claim's facts from this case's own records (never from another case)."""
    sources = {source['source_id']: source for source in case['sources']}
    facts = {fact['fact_id']: fact for fact in case['facts']}
    parts = []
    for claim in case['claims']:
        parts.extend([
            '<div class="rule"><span class="index">', _e(claim['confirmation']['state'][:1]),
            '</span><div>',
            '<b><a href="/workbench/cases/', _e(case['case_id']), '/claims/',
            _e(claim['claim_id']), '">', _e(claim['text']), '</a></b>',
            '<small>', _e(claim['predicate']), ' · action ', _e(claim['action_id']),
            ' · confirmation ', _e(claim['confirmation']['state']), '</small>',
        ])
        for fact_id in claim['fact_ids']:
            fact = facts.get(fact_id)
            if fact is None:
                parts.extend(['<small class="reason">cited fact ', _e(fact_id),
                              ' is not in this case</small>'])
                continue
            source = sources.get(fact['source_id'])
            parts.extend([
                '<small>cites ', _e(fact_id), ' · anchor ', _e(fact['anchor']),
                ' · ', _e(source['title'] if source else 'source not in this case'),
                ' · content_sha256 ',
                _e((source or {}).get('content_sha256') or 'none'),
                ' · provenance ',
                _e((source or {}).get('provenance_label') or 'none'), '</small>',
            ])
        parts.append('</div></div>')
    return parts


def _claims_block(case: dict) -> list[str]:
    parts = ['<section class="panel">',
             '<div class="section-label"><span>05</span> CLAIMS',
             '<span class="right-label">dynamic citations from this case</span></div>']
    if not case['claims']:
        parts.append('<p class="muted">No claims recorded yet.</p>')
    parts.extend(_claim_citation_lines(case))
    parts.extend(_case_links(case))
    parts.append('</section>')
    return parts


def _case_page(case: dict, *, notice: str | None = None) -> HTMLResponse:
    body = []
    if notice:
        body.extend(['<p class="notice" role="status">', _e(notice), '</p>'])
    body.extend(_case_header(case))
    body.extend(_sources_block(case))
    body.extend(_facts_block(case))
    body.extend(_claims_block(case))
    return _shell(case['case_title'], body)


def _workbench_index(listing: dict) -> HTMLResponse:
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>00</span> LOCAL CASES',
        '<span class="right-label">offline · one local store</span></div>',
        '<h1>Case workbench</h1>',
        '<p class="muted">Create a case, import the exact text you were given, map excerpts a human '
        'has reviewed, then run a case-scoped investigation. Nothing here fetches a URL, parses a '
        'PDF or authenticates a document.</p>',
    ]
    if listing['state'] == 'NO_CASES_YET':
        body.extend(['<p class="notice" role="status">', _e(listing['message']),
                     ' The state is typed (NO_CASES_YET); no placeholder case is invented.</p>'])
    else:
        body.append('<div>')
        for item in listing['cases']:
            body.extend([
                '<div class="source">',
                '<span class="source-id">', _e(item['state']), '</span>',
                '<strong><a href="/workbench/cases/', _e(item['case_id']), '">',
                _e(item['case_title']), '</a></strong>',
                '<small>', _e(item['municipality']), ' — ', _e(item['project_name']),
                ' · revision ', _e(item['revision']),
                ' · sources ', _e(item['source_count']),
                ' · facts ', _e(item['fact_count']),
                ' · claims ', _e(item['claim_count']), '</small>',
                '</div>',
            ])
        body.append('</div>')
    body.extend([
        '</section>',
        '<section class="panel">',
        '<div class="section-label"><span>01</span> CREATE A CASE',
        '<span class="right-label">metadata you declare</span></div>',
        '<form method="post" action="/workbench/cases" data-upload-form="1">',
        '<p><label for="case_title">Case title</label><br>'
        '<input id="case_title" name="case_title" maxlength="300" size="70"></p>',
        '<p><label for="project_name">Project name</label><br>'
        '<input id="project_name" name="project_name" maxlength="200" size="70"></p>',
        '<p><label for="municipality">Municipality</label><br>'
        '<input id="municipality" name="municipality" maxlength="200" size="70"></p>',
        '<p><label for="jurisdiction_id">Jurisdiction id (lowercase slug)</label><br>'
        '<input id="jurisdiction_id" name="jurisdiction_id" maxlength="64" size="30"></p>',
        '<p><label for="publisher">Publisher name (every source must match it)</label><br>'
        '<input id="publisher" name="publisher" maxlength="200" size="60"></p>',
        '<p><label for="allowed_hosts">Allowed source hostnames (comma separated, 1-8)</label><br>'
        '<input id="allowed_hosts" name="allowed_hosts" maxlength="400" size="60"></p>',
        '<p><label for="provenance_note">Provenance note (optional)</label><br>'
        '<input id="provenance_note" name="provenance_note" maxlength="500" size="70"></p>',
        '<p><label><input type="checkbox" name="include_source" value="1" checked> '
        'include the first source now</label></p>',
        '<p><label for="source_title">Source title</label><br>'
        '<input id="source_title" name="source_title" maxlength="300" size="70"></p>',
        '<p><label for="official_url">Declared official URL (https; never fetched)</label><br>'
        '<input id="official_url" name="official_url" maxlength="1000" size="70"></p>',
        '<p><label for="document_role">Document role</label><br><select id="document_role" '
        'name="document_role">', *_options(sorted(ALLOWED_DOCUMENT_ROLES), None), '</select></p>',
        '<p><label for="document_date">Document date (YYYY-MM-DD)</label><br>'
        '<input id="document_date" name="document_date" maxlength="10" size="12"> '
        '<label for="as_of">As-of date</label> '
        '<input id="as_of" name="as_of" maxlength="10" size="12"></p>',
        '<p><label for="source_anchor">Anchor</label><br>'
        '<input id="source_anchor" name="source_anchor" maxlength="500" size="60"></p>',
        '<p><label for="text">Paste the exact text</label><br>'
        '<textarea id="text" name="text" rows="10" cols="80"></textarea></p>',
        '<p><label for="source_file">…or pick a .txt file (UTF-8, at most ', _e(MAX_TEXT_BYTES),
        ' bytes)</label><br>'
        '<input id="source_file" name="source_file" type="file" accept=".txt,text/plain">'
        '<input type="hidden" name="filename"><input type="hidden" name="text_base64">'
        '<br><small data-upload-status></small></p>',
        '<p class="muted">The imported bytes are hashed as imported text. This is not an original '
        'document hash and it never sets original-source verification.</p>',
        '<p><button class="primary-btn" type="submit">Create case</button></p>',
        '</form></section>',
    ])
    return _shell('Case workbench', body)


def _source_page(case: dict, source: dict) -> HTMLResponse:
    facts = [fact for fact in case['facts'] if fact['source_id'] == source['source_id']]
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> SOURCE REVIEW',
        '<span class="right-label">', _e(source['source_id']), '</span></div>',
        '<h1>', _e(source['title']), '</h1>',
        '<p>', _e(source['publisher']), ' · ', _e(source['document_role']),
        ' · document date ', _e(source['document_date']), ' · as of ', _e(source['as_of']),
        ' · anchor ', _e(source['anchor']), '</p>',
        '<p><strong>Capture label:</strong> ', _e(source['capture_class']), '</p>',
        '<p class="reason">content_sha256=', _e(source.get('content_sha256') or 'none'),
        ' · raw_sha256=', _e(source.get('raw_sha256') or 'none'),
        ' · hash_subject=',
        _e('ORIGINAL_SOURCE_BYTES' if source['capture_class'] == 'INDEPENDENT_ORIGINAL_BYTES'
           else 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'),
        ' · provenance_label=', _e(source['provenance_label']), '</p>',
        '<p class="notice" role="status">ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED. '
        'The hash above covers the imported text bytes; it does not authenticate the document, '
        'and independent human fact audit is PENDING.</p>',
        '<p class="muted">Declared location (never fetched, never resolved): ',
        _e(source['official_url']), '</p>',
        '<p class="muted">Retrieved into this store at ', _e(source['retrieved_at_utc']),
        ' · review state ', _e(source['review_state']), '</p>',
        '<div class="section-label"><span>02</span> EXACT PRESERVED TEXT',
        '<span class="right-label">inert data, escaped</span></div>',
        '<pre>', html.escape(source.get('exact_text') or '(original bytes were not imported)',
                             quote=False), '</pre>',
        '</section>',
        '<section class="panel">',
        '<div class="section-label"><span>03</span> MAP A FACT FROM THIS EXCERPT',
        '<span class="right-label">excerpt must occur in the text</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/sources/',
        _e(source['source_id']), '/facts">',
        '<p><label for="anchor">Anchor</label><br>'
        '<input id="anchor" name="anchor" maxlength="500" size="60" value="',
        _e(source['anchor']), '"></p>',
        '<p><label for="exact_excerpt">Exact excerpt (copy it from the text above)</label><br>'
        '<textarea id="exact_excerpt" name="exact_excerpt" rows="4" cols="80"></textarea></p>',
        '<p><label for="predicate">Predicate</label><br><select id="predicate" name="predicate">',
        *_options(PREDICATE_ORDER, None), '</select></p>',
        '<p><label for="subject_id">Subject reference</label><br>'
        '<input id="subject_id" name="subject_id" maxlength="128" size="40"></p>',
        '<p><label for="action_id">Action reference</label><br>'
        '<input id="action_id" name="action_id" maxlength="128" size="40"></p>',
        '<p><label for="qualifiers">Qualifiers (comma separated, optional)</label><br>'
        '<input id="qualifiers" name="qualifiers" maxlength="400" size="60"></p>',
        '<p><label for="amount_cents">Amount in cents (optional)</label><br>'
        '<input id="amount_cents" name="amount_cents" maxlength="12" size="14"> '
        '<label for="event_date">Event date (optional)</label> '
        '<input id="event_date" name="event_date" maxlength="10" size="12"></p>',
        '<p><label for="phase">Phase (optional)</label><br>'
        '<input id="phase" name="phase" maxlength="64" size="30"></p>',
        '<p class="muted">Adding a mapping creates it as PENDING_HUMAN_REVIEW. A human review is a '
        'separate recorded action; only a HUMAN_REVIEWED mapping can support a claim.</p>',
        '<p><button class="primary-btn" type="submit">Add mapping</button></p>',
        '</form></section>',
        '<section class="panel">',
        '<div class="section-label"><span>04</span> MAPPINGS FROM THIS SOURCE',
        '<span class="right-label">', _e(len(facts)), ' recorded</span></div>',
    ]
    if not facts:
        body.append('<p class="muted">No mappings cite this source yet.</p>')
    for fact in facts:
        body.extend([
            '<div class="rule"><span class="index">', _e(fact['review_state'][:1]), '</span><div>',
            '<b><a href="/workbench/cases/', _e(case['case_id']), '/facts/', _e(fact['fact_id']),
            '">', _e(fact['predicate']), '</a></b>',
            '<small>', _e(fact['review_state']), ' · ', _e(fact['fact_id']), '</small>',
            '<small>', _e(fact['exact_excerpt']), '</small></div></div>',
        ])
    body.append('</section>')
    return _shell(f'Source: {source["title"]}', body)


def _fact_page(case: dict, fact: dict, source: dict) -> HTMLResponse:
    normal = fact.get('normal_forms') or {}
    citing = [claim for claim in case['claims'] if fact['fact_id'] in claim['fact_ids']]
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> FACT MAPPING EDITOR',
        '<span class="right-label">', _e(fact['fact_id']), '</span></div>',
        '<h1>', _e(fact['predicate']), '</h1>',
        '<p>Review state: <strong>', _e(fact['review_state']), '</strong>',
        ' · reviewer ', _e(fact.get('reviewed_by') or 'none'),
        ' · reported at ', _e(fact['reported_at']), '</p>',
        '<p class="muted">Source: <a href="/workbench/cases/', _e(case['case_id']), '/sources/',
        _e(source['source_id']), '">', _e(source['title']), '</a> · anchor ',
        _e(fact['anchor']), '</p>',
        '<blockquote>', html.escape(fact['exact_excerpt'], quote=False), '</blockquote>',
        '<div class="section-label"><span>02</span> NORMAL FORMS',
        '<span class="right-label">server derived</span></div>',
        '<p class="reason">method=', _e(normal.get('method') or 'none'),
        ' · normalized_sha256=', _e(normal.get('normalized_sha256') or 'none'), '</p>',
        '<pre>', html.escape(normal.get('normalized_excerpt') or '', quote=False), '</pre>',
        '<p class="muted">Normal forms are a review aid (NFKC, casefold, whitespace collapse), not a '
        'legal reading. Containment was checked against the imported text when this mapping was '
        'written.</p>',
        '</section>',
        '<section class="panel">',
        '<div class="section-label"><span>03</span> REVIEW TRANSITION',
        '<span class="right-label">human action, recorded</span><br></div>',
        '<p class="muted">Only PENDING_HUMAN_REVIEW and HUMAN_REVIEWED are settable here. '
        'SOURCE_REVISED_REVIEW_REQUIRED is produced by editing the source or the excerpt, never by '
        'a reviewer choice.</p>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/facts/',
        _e(fact['fact_id']), '/review">',
        '<p><label for="target">New review state</label><br><select id="target" name="target">',
        *_options(_REVIEWABLE_TARGETS, None), '</select></p>',
        '<p><label for="reviewer">Reviewer name (required to mark reviewed)</label><br>'
        '<input id="reviewer" name="reviewer" maxlength="120" size="40"></p>',
        '<p><button class="primary-btn" type="submit">Record review state</button></p>',
        '</form></section>',
        '<section class="panel">',
        '<div class="section-label"><span>04</span> EDIT THE MAPPING',
        '<span class="right-label">a content edit invalidates confirmations</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/facts/',
        _e(fact['fact_id']), '">',
        '<p><label for="anchor">Anchor</label><br>'
        '<input id="anchor" name="anchor" maxlength="500" size="60" value="',
        _e(fact['anchor']), '"></p>',
        '<p><label for="exact_excerpt">Exact excerpt</label><br>'
        '<textarea id="exact_excerpt" name="exact_excerpt" rows="4" cols="80">',
        html.escape(fact['exact_excerpt'], quote=False), '</textarea></p>',
        '<p><label for="predicate">Predicate</label><br><select id="predicate" name="predicate">',
        *_options(PREDICATE_ORDER, fact['predicate']), '</select></p>',
        '<p><label for="subject_id">Subject reference</label><br>'
        '<input id="subject_id" name="subject_id" maxlength="128" size="40" value="',
        _e(fact['subject_id']), '"></p>',
        '<p><label for="action_id">Action reference</label><br>'
        '<input id="action_id" name="action_id" maxlength="128" size="40" value="',
        _e(fact['action_id']), '"></p>',
        '<p><label for="qualifiers">Qualifiers (comma separated)</label><br>'
        '<input id="qualifiers" name="qualifiers" maxlength="400" size="60" value="',
        _e(', '.join(fact['qualifiers'])), '"></p>',
        '<p><label for="amount_cents">Amount in cents (blank keeps the current value)</label><br>'
        '<input id="amount_cents" name="amount_cents" maxlength="12" size="14" value="',
        _e(fact.get('amount_cents', '')), '"></p>',
        '<p><label for="phase">Phase</label> '
        '<input id="phase" name="phase" maxlength="64" size="24" value="',
        _e(fact.get('phase', '')), '"> <label for="event_date">Event date</label> '
        '<input id="event_date" name="event_date" maxlength="10" size="12" value="',
        _e(fact.get('event_date', '')), '"></p>',
        '<p><button class="primary-btn" type="submit">Save mapping</button></p>',
        '</form></section>',
        '<section class="panel">',
        '<div class="section-label"><span>05</span> CLAIMS CITING THIS MAPPING',
        '<span class="right-label">', _e(len(citing)), ' claim(s)</span></div>',
    ]
    if not citing:
        body.append('<p class="muted">No claim cites this mapping.</p>')
    for claim in citing:
        body.extend([
            '<div class="rule"><span class="index">', _e(claim['confirmation']['state'][:1]),
            '</span><div><b>', _e(claim['text']), '</b>',
            '<small>', _e(claim['predicate']), ' · confirmation ',
            _e(claim['confirmation']['state']),
            ' · invalidation reason ',
            _e(claim['confirmation'].get('invalidation_reason') or 'none'), '</small>',
            '</div></div>',
        ])
    body.append('</section>')
    return _shell(f'Fact mapping: {fact["predicate"]}', body)


def _claim_template_page(case: dict, template: str | None) -> HTMLResponse:
    template = template if template in ALLOWED_PREDICATES else None
    question, note = PREDICATE_QUESTIONS[template] if template else ('', '')
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> CLAIM TEMPLATE',
        '<span class="right-label">', _e(template or 'choose a predicate'), '</span></div>',
        '<h1>New claim</h1>',
        '<p class="muted">A claim is a structured, checkable statement. Choose the predicate that '
        'matches the record class, then cite the mappings whose exact excerpts support it.</p>',
        '<div class="challenges">',
    ]
    for predicate in PREDICATE_ORDER:
        body.extend([
            '<a class="challenge-btn secondary" href="/workbench/cases/', _e(case['case_id']),
            '/claims/new?template=', _e(predicate), '">', _e(predicate),
            ' <span>↗</span></a>',
        ])
    body.extend(['</div>'])
    if template:
        body.extend([
            '<p class="notice" role="status">Confirmation question: ', _e(question), '</p>',
            '<p class="muted">', _e(note), '</p>',
        ])
    body.extend([
        '</section>',
        '<section class="panel">',
        '<div class="section-label"><span>02</span> STRUCTURED CLAIM',
        '<span class="right-label">plain language + fields</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/claims">',
        '<p><label for="text">Plain-language statement</label><br>'
        '<textarea id="text" name="text" rows="3" cols="80"></textarea></p>',
        '<p><label for="predicate">Predicate</label><br><select id="predicate" name="predicate">',
        *_options(PREDICATE_ORDER, template), '</select></p>',
        '<p class="reason">',
    ])
    for predicate in PREDICATE_ORDER:
        body.extend(['required document role for ', _e(predicate), ': ',
                     _e(SOURCE_ROLE_BY_PREDICATE[predicate]), '<br>'])
    body.extend([
        '</p>',
        '<p><label for="subject_id">Subject reference</label><br>'
        '<input id="subject_id" name="subject_id" maxlength="128" size="40"></p>',
        '<p><label for="action_id">Action reference</label><br>'
        '<input id="action_id" name="action_id" maxlength="128" size="40"></p>',
        '<p><label for="additional_record">What record would settle this claim?</label><br>'
        '<input id="additional_record" name="additional_record" maxlength="500" size="70"></p>',
        '<p><label for="amount_cents">Amount in cents (optional)</label><br>'
        '<input id="amount_cents" name="amount_cents" maxlength="12" size="14"> '
        '<label for="event_date">Event date (optional)</label> '
        '<input id="event_date" name="event_date" maxlength="10" size="12"> '
        '<label for="as_of">As of (optional)</label> '
        '<input id="as_of" name="as_of" maxlength="10" size="12"></p>',
        '<p><label for="phase">Phase (optional)</label> '
        '<input id="phase" name="phase" maxlength="64" size="24"> '
        '<label for="yea">Yea</label> <input id="yea" name="yea" maxlength="2" size="3"> '
        '<label for="nay">Nay</label> <input id="nay" name="nay" maxlength="2" size="3"> '
        '<label for="abstain">Abstain</label> <input id="abstain" name="abstain" maxlength="2" size="3"></p>',
        '<p><label><input type="checkbox" name="ceiling" value="1"> the cited record sets a spend '
        'ceiling (UP_TO)</label></p>',
        '<div class="section-label"><span>03</span> CITE REVIEWED MAPPINGS',
        '<span class="right-label">this case only</span></div>',
    ])
    if not case['facts']:
        body.append('<p class="muted">No mappings exist in this case yet; add one on a source page.</p>')
    for fact in case['facts']:
        body.extend([
            '<p><label><input type="checkbox" name="fact_ids" value="', _e(fact['fact_id']), '"> ',
            _e(fact['fact_id']), ' · ', _e(fact['predicate']),
            ' · review ', _e(fact['review_state']), ' · anchor ', _e(fact['anchor']),
            '</label><br><small>', _e(fact['exact_excerpt']), '</small></p>',
        ])
    body.extend([
        '<p class="muted">A mapping that is not HUMAN_REVIEWED cannot support the claim: the '
        'investigation will report REVIEW_REQUIRED and UNREVIEWED_FACT_MAPPING. Confirmation is a '
        'separate recorded human action, not a finding of truth.</p>',
        '<p><button class="primary-btn" type="submit">Add claim</button></p>',
        '</form></section>',
    ])
    return _shell('New claim', body)


def _claim_page(case: dict, claim: dict) -> HTMLResponse:
    plain = claim.get('plain_language') or {}
    structured = plain.get('structured') or {}
    structured_text = ', '.join(f'{key}={structured[key]!r}' for key in sorted(structured))
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> CLAIM ',
        '<span class="right-label">', _e(claim['claim_id']), '</span></div>',
        '<h1>', _e(claim['text']), '</h1>',
        '<p>Predicate ', _e(claim['predicate']), ' · subject ', _e(claim['subject_id']),
        ' · action ', _e(claim['action_id']), ' · jurisdiction ', _e(claim['jurisdiction_id']), '</p>',
        '<p>Confirmation: <strong>', _e(claim['confirmation']['state']), '</strong>',
        ' · reviewer ', _e(claim['confirmation'].get('reviewer') or 'none'),
        ' · invalidation reason ',
        _e(claim['confirmation'].get('invalidation_reason') or 'none'), '</p>',
        '<p class="reason">claim content_sha256=',
        _e(claim['confirmation'].get('content_sha256') or 'none'),
        ' · evidence_sha256=', _e(claim['confirmation'].get('evidence_sha256') or 'none'), '</p>',
        '<div class="section-label"><span>02</span> PLAIN-LANGUAGE TEMPLATE',
        '<span class="right-label">template v', _e(plain.get('template_version', '?')),
        '</span></div>',
        '<p class="notice" role="status">', _e(plain.get('confirmation_prompt') or ''), '</p>',
        '<p class="reason">structured=', _e(structured_text or 'none'), '</p>',
        '<div class="section-label"><span>03</span> CITED EXCERPTS FROM THIS CASE',
        '<span class="right-label">dynamic citations</span></div>',
    ]
    body.extend(_claim_specific_citations(case, claim))
    body.extend([
        '<div class="section-label"><span>04</span> HUMAN CONFIRMATION',
        '<span class="right-label">attestation required</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/claims/',
        _e(claim['claim_id']), '/confirm">',
        '<p class="muted">', _e(PREDICATE_QUESTIONS.get(claim['predicate'], ('', ''))[0]),
        ' Confirm only that the statement and fields match the cited excerpts.</p>',
        '<p><label><input type="checkbox" name="attest" value="yes"> I have read the exact '
        'excerpts and the structured fields and they match.</label></p>',
        '<p><label for="reviewer">Reviewer name</label><br>'
        '<input id="reviewer" name="reviewer" maxlength="120" size="40" value="',
        _e(claim['confirmation'].get('reviewer') or ''), '"></p>',
        '<p><button class="primary-btn" type="submit" name="action" value="confirm">Record '
        'confirmation</button> '
        '<button class="challenge-btn secondary" type="submit" name="action" value="withdraw">'
        'Withdraw confirmation</button></p>',
        '</form>',
        '<p><a href="/workbench/cases/', _e(case['case_id']), '/investigate">Run the case-scoped '
        'investigation</a></p>',
        '</section>',
    ])
    return _shell(f'Claim: {claim["predicate"]}', body)


def _claim_specific_citations(case: dict, claim: dict) -> list[str]:
    sources = {source['source_id']: source for source in case['sources']}
    facts = {fact['fact_id']: fact for fact in case['facts']}
    parts = []
    for fact_id in claim['fact_ids']:
        fact = facts.get(fact_id)
        if fact is None:
            parts.extend(['<p class="reason">cited fact ', _e(fact_id),
                          ' is not registered in this case</p>'])
            continue
        source = sources.get(fact['source_id']) or {}
        parts.extend([
            '<div class="evidence-pill">',
            '<a href="/workbench/cases/', _e(case['case_id']), '/sources/',
            _e(fact['source_id']), '">', _e(fact_id), ' · ', _e(fact['anchor']),
            '</a>',
            '<small>', _e(source.get('title') or 'source not in this case'), ' · ',
            _e(source.get('document_role') or 'unknown role'), '</small>',
            '<small>', _e(fact['exact_excerpt']), '</small>',
            '<small class="reason">capture_class=', _e(source.get('capture_class') or 'none'),
            ' · content_sha256=', _e(source.get('content_sha256') or 'none'),
            ' · provenance_label=', _e(source.get('provenance_label') or 'none'),
            ' · review ', _e(fact['review_state']), '</small>',
            '</div>',
        ])
    if not parts:
        parts.append('<p class="muted">This claim cites no mapping.</p>')
    return parts


def _decision_block(decision, claim: dict) -> list[str]:
    capture_labels = sorted({citation.get('capture_class') or 'UNKNOWN'
                             for citation in decision.source_citations})
    parts = [
        '<div class="rule"><div>',
        '<b>', _e(claim['text']), '</b>',
        '<p><span class="status ', _status_class(decision.status), '">',
        _e(decision.status), '</span></p>',
        '<p class="reason">reason_codes=',
        _e(' · '.join(decision.reason_codes) or 'none'), '</p>',
        '<p class="muted">Provenance: ORIGINAL-SOURCE-VERIFICATION: UNVERIFIED · capture class ',
        _e(', '.join(capture_labels) or 'none'),
        ' · hash subject ', _e('IMPORTED_TEXT_NOT_ORIGINAL_BYTES'),
        ' · original_source_verified=',
        _e('true' if decision.original_source_verified else 'false'),
        ' · independent_fact_audit=', _e(decision.independent_fact_audit),
        ' · source_integrity=', _e(decision.source_integrity_state),
        ' · finding maturity=', _e(decision.finding_maturity), '</p>',
    ]
    if not decision.source_citations:
        parts.append('<p class="muted">No registered citation in this case for this claim.</p>')
    for citation in decision.source_citations:
        parts.extend([
            '<div class="evidence-pill">',
            '<a href="', _e(citation['url']), '" target="_blank" rel="noopener noreferrer">',
            _e(citation['fact_id']), ' · ', _e(citation['anchor']), ' ↗</a>',
            '<small>', _e(citation['title']), ' · ', _e(citation['document_role']),
            ' · document as of ', _e(citation['document_as_of']), '</small>',
            '<small>', _e(citation['exact_excerpt']), '</small>',
            '<small class="reason">capture_class=', _e(citation['capture_class']),
            ' · hash_subject=', _e(citation['hash_subject']),
            ' · content_sha256=', _e(citation['content_sha256'] or 'none'),
            ' · provenance_label=', _e(citation['provenance_label']), '</small>',
            '</div>',
        ])
    parts.extend(['<p class="muted">To investigate further: ', _e(decision.additional_record),
                  '</p></div></div>'])
    return parts


def _live_panel(preview: dict | None) -> list[str]:
    """The live selected-case panel: what would run, which gates pass, and why it is off."""
    if preview is None:  # pragma: no cover - app.py always binds the preview
        return []
    activation = preview['live_activation']
    enabled = bool(activation['owner_enabled'])
    eligibility = preview.get('eligibility') or {}
    role_entries = list((eligibility.get('roles') or {}).items())
    selected_claim = preview.get('claim_id')
    can_run = bool(enabled and preview.get('claim_confirmation_state') == 'CONFIRMED'
                   and eligibility.get('eligible'))
    parts = [
        '<section class="panel">',
        '<div class="section-label"><span>02</span> LIVE SELECTED-CASE INVESTIGATION',
        '<span class="right-label">', _e(activation['state']), '</span></div>',
        '<h2>One case, one immutable revision, one confirmed claim</h2>',
        '<p><strong>Case</strong> ', _e(preview['case_id']),
        ' · <strong>revision</strong> ', _e(preview['revision']),
        ' · <strong>state</strong> ', _e(preview['case_state']),
        ' · <strong>case registry sha256</strong> ',
        _e(preview.get('case_registry_sha256') or 'not derivable'), '</p>',
        '<p><strong>Claim that would be investigated</strong> ',
        _e(selected_claim or 'none selected'),
        ' · confirmation ', _e(preview.get('claim_confirmation_state') or 'none'),
        ' · predicate ', _e(preview.get('claim_predicate') or 'none'), '</p>',
        '<p class="notice" role="status">LIVE ACTIVATION: ',
        _e('ENABLED BY THE OWNER' if enabled else
           'DISABLED — owner authorization is required. No provider is contacted from this page.'),
        '</p>',
        '<p class="muted">', _e(activation['detail']), '</p>',
        '<p class="muted">Case isolation: ', _e(preview['case_isolation']),
        '. A claim, source or mapping registered in another case is refused before anything is '
        'reserved.</p>',
        '<div class="section-label"><span>02a</span> GATES, IN ORDER</div>',
    ]
    for gate in preview['gates']:
        parts.extend(['<p class="reason">', _e(gate['gate']), ' → ',
                      _e('PASS' if gate['ok'] else 'BLOCKED'), '</p>'])
    if eligibility:
        parts.extend([
            '<p class="reason">two-source-role eligibility: ',
            _e('ELIGIBLE' if eligibility.get('eligible') else 'NOT ELIGIBLE'),
            ' · ', _e(eligibility.get('reason') or 'both reviewed roles present'), '</p>',
        ])
        for role, entry in role_entries:
            parts.extend([
                '<p class="muted">', _e(role), ' · tool ', _e(entry.get('tool')),
                ' · registered sources ', _e(entry.get('registered_sources')),
                ' · selectors ', _e(entry.get('selectors')),
                ' · reviewed mappings ', _e(entry.get('reviewed_fact_count')),
                ' · tool derived ', _e('true' if entry.get('tool_derived') else 'false'),
                ' · note ', _e(entry.get('not_derived_reason') or 'none'), '</p>',
            ])
    elif preview.get('court_error'):
        parts.extend(['<p class="reason">Court refused this case: ',
                      _e(preview['court_error'].get('code')), ' · ',
                      _e(preview['court_error'].get('message') or ''), '</p>'])
    parts.extend([
        '<p class="muted">Reservation order: ',
        _e(' → '.join(preview['reservation_order'])),
        '. Budget, admission, dispatch marker and settlement are the existing durable path; '
        'there is no direct agent call, no accounting reset and no refund.</p>',
        '<form method="post" action="/workbench/cases/', _e(preview['case_id']),
        '/investigate/live">',
        '<p><label for="claim_id">Claim this run would investigate</label><br>'
        '<select id="claim_id" name="claim_id">',
    ])
    for claim in preview['claims']:
        parts.extend([
            '<option value="', _e(claim['claim_id']), '"',
            ' selected' if claim['claim_id'] == selected_claim else '', '>',
            _e(claim['predicate']), ' · ', _e(claim['text'][:80]),
            ' · ', _e(claim['confirmation_state']), '</option>',
        ])
    if not preview['claims']:
        parts.append('<option value="">no claims in this case</option>')
    parts.extend([
        '</select></p>',
        '<input type="hidden" name="revision" value="', _e(preview['revision']), '">',
        '<p class="muted">The hidden revision pins the exact immutable revision this page showed. '
        'If the case has moved on, the run is refused as STALE_CASE_REVISION before any '
        'reservation.</p>',
        '<p><button class="primary-btn" type="submit"', '' if can_run else ' disabled',
        '>Run the live selected-case investigation</button></p>',
    ])
    if not can_run:
        parts.extend([
            '<p class="reason">This button is disabled: ',
            _e('live inference is not owner-enabled'
               if not enabled else 'a gate above is blocked'),
            '. Nothing on this page contacts a provider.</p>',
        ])
    parts.append('</form></section>')
    return parts


def _live_result_page(case: dict, run: dict) -> HTMLResponse:
    """Render one completed selected-case run without overstating what it establishes."""
    provenance = run.get('case_investigation') or {}
    result = run.get('court_result') or {}
    provider = run.get('provider') or {}
    reasons = ' · '.join(result.get('reason_codes') or []) or 'none'
    citations = result.get('source_citations') or []
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> SELECTED-CASE RUN',
        '<span class="right-label">', _e(provenance.get('case_id') or case['case_id']),
        ' · revision ', _e(provenance.get('revision') or case['revision']), '</span></div>',
        '<h1>', _e(case['case_title']), '</h1>',
        '<p>Claim <strong>', _e(provenance.get('claim_id') or 'unknown'), '</strong>',
        ' · confirmation ', _e((provenance.get('claim_confirmation') or {}).get('state') or 'none'),
        ' · predicate ', _e(provenance.get('claim_predicate') or 'unknown'), '</p>',
        '<p><span class="status ', _status_class(str(result.get('status') or 'REVIEW_REQUIRED')),
        '">', _e(result.get('status') or 'NO_DECISION'), '</span></p>',
        '<p class="reason">reason_codes=', _e(reasons), '</p>',
        '<p class="muted">provider ', _e(provider.get('kind') or 'none'),
        ' · model ', _e(provider.get('model') or 'none'),
        ' · rounds ', _e(provider.get('rounds')),
        ' · offered tools ', _e(', '.join(provider.get('offered_tools') or []) or 'none'),
        ' · case registry sha256 ', _e(provider.get('case_registry_sha256') or 'none'), '</p>',
        '<p class="muted">case isolation ', _e(provenance.get('case_isolation') or 'unknown'),
        ' · live eligibility ',
        _e('eligible' if (provenance.get('live_eligibility') or {}).get('eligible') else 'refused'),
        ' · provenance original_source_verified=false · independent_fact_audit=PENDING</p>',
        '<div class="section-label"><span>02</span> COURT EVIDENCE / CONTEXT</div>',
        '<p>On a refused or review-required result, the citations below may be contextual records; '
        'they are not accepted model support. The model proposal is shown separately below.</p>',
    ]
    if not citations:
        body.append('<p class="muted">This run cited no registered mapping in this case.</p>')
    for citation in citations:
        body.extend([
            '<div class="evidence-pill">',
            '<span>', _e(citation.get('fact_id')), ' · ', _e(citation.get('anchor')), '</span>',
            '<small>', _e(citation.get('title')), ' · ', _e(citation.get('document_role')),
            ' · capture ', _e(citation.get('capture_class')), '</small>',
            '<small>', _e(citation.get('exact_excerpt')), '</small>',
            '</div>',
        ])
    # Explicit allowlist: never dump provider dictionaries, headers, prompts or observations.
    diagnostics = {
        'registered_fact_ids_proposed': (run.get('candidate') or {}).get('registered_fact_ids_proposed', []),
        'proposal_note': 'Registered proposed IDs only; unknown model IDs are withheld. Court validates the full proposal.',
        'requests_dispatched': provider.get('requests_dispatched'),
        'responses_received': provider.get('responses_received'),
        'validated_responses': provider.get('validated_responses'),
        'request_count_basis': provider.get('request_count_basis'),
        'usage_as_returned': {k: (provider.get('usage_as_returned') or {}).get(k)
                              for k in ('prompt_tokens', 'completion_tokens')},
        'usage_complete': provider.get('usage_complete'),
        'latency_ms': provider.get('latency_ms'),
        'finish_status': (provider.get('protocol') or {}).get('final_finish_reason'),
        'executed_tools': [{k: item.get(k) for k in
                           ('name', 'source_ids', 'fact_ids', 'provider_round')}
                          for item in provider.get('tool_calls', [])],
    }
    body.extend(['<h2>Sanitized model proposal and execution</h2>',
                 '<p>Token usage is provider-reported, not an invoice. Dispatch counts do not prove delivery.</p>',
                 '<pre>', _e(json.dumps(diagnostics, indent=2)), '</pre>'])
    body.extend([
        '<p><a href="/workbench/cases/', _e(case['case_id']), '/investigate">Back to the '
        'case-scoped investigation</a></p>',
        '</section>',
    ])
    return _shell('Selected-case run', body)


def _run_form(case: dict) -> list[str]:
    return [
        '<section class="panel">',
        '<div class="section-label"><span>03</span> RUN ONE CLAIM',
        '<span class="right-label">observed evidence is explicit</span></div>',
        '<form method="post" action="/workbench/cases/', _e(case['case_id']), '/investigate">',
        '<p><label for="claim_id">Claim</label><br><select id="claim_id" name="claim_id">',
        *[f'<option value="{_e(claim["claim_id"])}">'
          f'{_e(claim["predicate"])} · {_e(claim["text"][:80])}</option>'
          for claim in case['claims']],
        '</select></p>',
        '<p><label for="evidence_mode">Evidence the tool loop actually observed</label><br>'
        '<select id="evidence_mode" name="evidence_mode">'
        '<option value="declared">declared citations (trusted claim)</option>'
        '<option value="unseen">nothing observed (unseen-citation probe)</option>'
        '<option value="proposed_only">only the facts listed below</option>'
        '</select></p>',
        '<p><label for="proposed_fact_ids">Facts observed, comma separated</label><br>'
        '<input id="proposed_fact_ids" name="proposed_fact_ids" maxlength="600" size="60"></p>',
        '<p><label for="predicate_override">Predicate the model proposed (optional)</label><br>'
        '<input id="predicate_override" name="predicate_override" maxlength="80" size="40"></p>',
        '<p><button class="primary-btn" type="submit">Run investigation</button></p>',
        '</form></section>',
    ]


def _investigation_page(case: dict, decisions: list[tuple[dict, object]], *,
                        run_notice: str | None = None,
                        preview: dict | None = None) -> HTMLResponse:
    body = [
        '<section class="panel">',
        '<div class="section-label"><span>01</span> CASE-SCOPED INVESTIGATION',
        '<span class="right-label">', _e(case['case_id']), '</span></div>',
        '<h1>', _e(case['case_title']), '</h1>',
        '<p class="muted">Every result below is produced by a Court bound to this case only. '
        'Citations come from this case\u2019s own records. Nothing is fetched and no provider is '
        'called by the deterministic evaluation.</p>',
        '<p class="reason">claims=', _e(len(case['claims'])),
        ' · facts=', _e(len(case['facts'])),
        ' · sources=', _e(len(case['sources'])),
        ' · revision=', _e(case['revision']), '</p>',
        '</section>',
    ]
    if run_notice:
        body.extend(['<p class="notice" role="status">', _e(run_notice), '</p>'])
    body.extend(_live_panel(preview))
    if not case['claims']:
        body.append('<section class="panel"><p class="muted">No claims recorded yet, so there is '
                    'nothing to evaluate. This is a draft state, not a finding.</p></section>')
    for claim, decision in decisions:
        body.append('<section class="panel">')
        body.extend(_decision_block(decision, claim))
        body.append('</section>')
    body.extend(_run_form(case))
    return _shell('Investigation', body)


# -- store helpers ------------------------------------------------------------------------------

def _case(case_id: str) -> dict:
    return _store().get_case(case_id)


def _source(case: dict, source_id: str) -> dict:
    for source in case['sources']:
        if source['source_id'] == source_id:
            return source
    raise CaseStoreError('SOURCE_NOT_IN_CASE', 'Source does not belong to this case', status=404)


def _fact(case: dict, fact_id: str) -> dict:
    for fact in case['facts']:
        if fact['fact_id'] == fact_id:
            return fact
    raise CaseStoreError('FACT_NOT_IN_CASE', 'Fact does not belong to this case', status=404)


def _claim(case: dict, claim_id: str) -> dict:
    for claim in case['claims']:
        if claim['claim_id'] == claim_id:
            return claim
    raise CaseStoreError('CLAIM_NOT_IN_CASE', 'Claim does not belong to this case', status=404)


def _evaluate_all(case: dict) -> list[tuple[dict, object]]:
    court = Court.from_case_store(_store(), case['case_id'])
    return [(claim, court.evaluate(claim['claim_id'])) for claim in case['claims']]


# -- routes: pages ------------------------------------------------------------------------------

@router.get('/workbench')
async def workbench_index() -> HTMLResponse:
    with _ui_errors('/workbench'):
        listing = _store().list_cases()
    return _workbench_index(listing)


@router.get('/workbench/cases/{case_id}')
async def workbench_case(case_id: str, created_fact: str | None = None,
                         created_source: str | None = None) -> HTMLResponse:
    with _ui_errors('/workbench'):
        case = _case(case_id)
    notice = None
    if created_source:
        notice = f'Source {created_source} was imported. Its hash covers the imported text only.'
    if created_fact:
        notice = f'Fact mapping {created_fact} was recorded as PENDING_HUMAN_REVIEW.'
    return _case_page(case, notice=notice)


@router.get('/workbench/cases/{case_id}/sources/{source_id}')
async def workbench_source(case_id: str, source_id: str) -> HTMLResponse:
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
        source = _source(case, source_id)
    return _source_page(case, source)


@router.get('/workbench/cases/{case_id}/facts/{fact_id}')
async def workbench_fact(case_id: str, fact_id: str) -> HTMLResponse:
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
        fact = _fact(case, fact_id)
        source = _source(case, fact['source_id'])
    return _fact_page(case, fact, source)


@router.get('/workbench/cases/{case_id}/claims/new')
async def workbench_claim_template(case_id: str, template: str | None = None) -> HTMLResponse:
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
    return _claim_template_page(case, template)


@router.get('/workbench/cases/{case_id}/claims/{claim_id}')
async def workbench_claim(case_id: str, claim_id: str) -> HTMLResponse:
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
        claim = _claim(case, claim_id)
    return _claim_page(case, claim)


@router.get('/workbench/cases/{case_id}/investigate')
async def workbench_investigate(case_id: str, claim_id: str | None = None) -> HTMLResponse:
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
        decisions = _evaluate_all(case)
        preview = (_preview_provider(case_id, claim_id)
                   if _preview_provider is not None else None)
    return _investigation_page(case, decisions, preview=preview)


# -- routes: mutations --------------------------------------------------------------------------

@router.post('/workbench/cases')
async def workbench_create_case(request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors('/workbench'):
        project_name = _meta(form, 'project_name', maximum=200)
        municipality = _meta(form, 'municipality', maximum=200)
        case_title = _meta(form, 'case_title', maximum=300)
        jurisdiction_id = _meta(form, 'jurisdiction_id', maximum=64)
        publisher = _meta(form, 'publisher', maximum=200)
        hosts = [item for item in _REF_SPLIT.split((_one(form, 'allowed_hosts') or '').strip())
                 if item]
        if not hosts:
            raise UiError(422, 'INVALID_INPUT', 'allowed_hosts must declare at least one hostname.')
        include_source = _bool_field(form, 'include_source')
        # Validate an upload before the case exists, exactly like the JSON route, so a rejected
        # first source never leaves a half-created shell behind.
        source_text = _upload_text(form) if include_source else None
        store = _store()
        case = store.create_case(project_name=project_name, municipality=municipality,
                                 case_title=case_title, jurisdiction_id=jurisdiction_id,
                                 publisher=publisher, allowed_hosts=hosts,
                                 provenance_note=_meta(form, 'provenance_note', required=False,
                                                       maximum=500))
        source_id = None
        if include_source:
            try:
                source = store.add_source(case['case_id'], **_source_kwargs(
                    form, publisher_default=publisher))
            except CaseStoreError:
                store.discard_case(case['case_id'])
                raise
            source_id = source['source_id']
        case = store.get_case(case['case_id'])
    target = f'/workbench/cases/{case["case_id"]}'
    if source_id:
        target += f'?created_source={source_id}'
    return RedirectResponse(target, status_code=303)


@router.post('/workbench/cases/{case_id}/sources')
async def workbench_add_source(case_id: str, request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}'):
        case = _case(case_id)
        source = _store().add_source(
            case_id,
            **_source_kwargs(form, publisher_default=case['publisher_policy']['publisher']))
    return RedirectResponse(
        f'/workbench/cases/{case_id}?created_source={source["source_id"]}', status_code=303)


@router.post('/workbench/cases/{case_id}/sources/{source_id}/facts')
async def workbench_add_fact(case_id: str, source_id: str, request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/sources/{source_id}'):
        fact = _store().add_fact(
            case_id, source_id=source_id,
            anchor=_meta(form, 'anchor', maximum=500),
            exact_excerpt=_exact_text(form, 'exact_excerpt') or '',
            predicate=_meta(form, 'predicate', maximum=80),
            subject_id=_meta(form, 'subject_id', maximum=128),
            action_id=_meta(form, 'action_id', maximum=128),
            qualifiers=_ref_list(form, 'qualifiers'),
            amount_cents=_int_field(form, 'amount_cents'),
            phase=_meta(form, 'phase', required=False, maximum=64),
            event_date=_meta(form, 'event_date', required=False, maximum=10))
    return RedirectResponse(
        f'/workbench/cases/{case_id}?created_fact={fact["fact_id"]}', status_code=303)


@router.post('/workbench/cases/{case_id}/facts/{fact_id}/review')
async def workbench_review_fact(case_id: str, fact_id: str, request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/facts/{fact_id}'):
        target = _meta(form, 'target', maximum=64)
        if target not in _REVIEWABLE_TARGETS:
            raise UiError(422, 'REVIEW_STATE_NOT_SETTABLE',
                          'Only HUMAN_REVIEWED and PENDING_HUMAN_REVIEW can be recorded here; '
                          'SOURCE_REVISED_REVIEW_REQUIRED is produced by an edit.')
        reviewer = _meta(form, 'reviewer', required=False, maximum=120)
        if target == 'HUMAN_REVIEWED' and not reviewer:
            raise UiError(422, 'REVIEWER_REQUIRED',
                          'A human review must record who performed it.')
        _store().update_fact(case_id, fact_id, {'review_state': target, 'reviewed_by': reviewer})
    return RedirectResponse(f'/workbench/cases/{case_id}/facts/{fact_id}', status_code=303)


@router.post('/workbench/cases/{case_id}/facts/{fact_id}')
async def workbench_edit_fact(case_id: str, fact_id: str, request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/facts/{fact_id}'):
        patch: dict = {}
        for field, maximum in (('anchor', 500), ('subject_id', 128), ('action_id', 128),
                               ('predicate', 80)):
            raw = _one(form, field)
            if raw is None:
                continue
            patch[field] = _meta(form, field, maximum=maximum)
        if _one(form, 'qualifiers') is not None:
            patch['qualifiers'] = _ref_list(form, 'qualifiers')
        excerpt = _exact_text(form, 'exact_excerpt')
        if excerpt is not None and excerpt.strip():
            patch['exact_excerpt'] = excerpt
        for field in ('amount_cents', 'phase', 'event_date'):
            value = _one(form, field)
            if value is None or not value.strip():
                continue
            patch[field] = _int_field(form, field) if field == 'amount_cents' else value.strip()
        if not patch:
            raise UiError(422, 'NO_CHANGES', 'The edit form carried no changed field.')
        _store().update_fact(case_id, fact_id, patch)
    return RedirectResponse(f'/workbench/cases/{case_id}/facts/{fact_id}', status_code=303)


@router.post('/workbench/cases/{case_id}/claims')
async def workbench_add_claim(case_id: str, request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/claims/new'):
        fact_ids = _many(form, 'fact_ids')
        if not fact_ids:
            raise UiError(422, 'INVALID_INPUT',
                          'Cite at least one reviewed fact mapping from this case.')
        claim = _store().add_claim(
            case_id,
            text=(_exact_text(form, 'text') or '').strip(),
            predicate=_meta(form, 'predicate', maximum=80),
            subject_id=_meta(form, 'subject_id', maximum=128),
            action_id=_meta(form, 'action_id', maximum=128),
            fact_ids=fact_ids,
            additional_record=_meta(form, 'additional_record', maximum=500),
            amount_cents=_int_field(form, 'amount_cents'),
            phase=_meta(form, 'phase', required=False, maximum=64),
            event_date=_meta(form, 'event_date', required=False, maximum=10),
            yea=_int_field(form, 'yea'), nay=_int_field(form, 'nay'),
            abstain=_int_field(form, 'abstain'),
            ceiling=_bool_field(form, 'ceiling'),
            as_of=_meta(form, 'as_of', required=False, maximum=10))
    return RedirectResponse(f'/workbench/cases/{case_id}/claims/{claim["claim_id"]}',
                            status_code=303)


@router.post('/workbench/cases/{case_id}/claims/{claim_id}/confirm')
async def workbench_confirm_claim(case_id: str, claim_id: str,
                                  request: Request) -> Response:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/claims/{claim_id}'):
        action = (_one(form, 'action') or 'confirm').strip().lower()
        if action == 'withdraw':
            _store().confirm_claim(case_id, claim_id, confirmed=False)
        else:
            if not _bool_field(form, 'attest'):
                raise UiError(422, 'CONFIRMATION_ATTESTATION_REQUIRED',
                              'Confirmation requires the attestation checkbox: the statement and '
                              'the structured fields must match the cited excerpts.')
            reviewer = _meta(form, 'reviewer', required=False, maximum=120)
            _store().confirm_claim(case_id, claim_id, confirmed=True, reviewer=reviewer)
    return RedirectResponse(f'/workbench/cases/{case_id}/claims/{claim_id}', status_code=303)


@router.post('/workbench/cases/{case_id}/investigate')
async def workbench_run_investigation(case_id: str, request: Request) -> HTMLResponse:
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/investigate'):
        case = _case(case_id)
        claim_id = _meta(form, 'claim_id', maximum=64)
        claim = _claim(case, claim_id)
        mode = (_one(form, 'evidence_mode') or 'declared').strip()
        proposed: list[str] | None
        observed: set[str] | None
        if mode == 'declared':
            proposed, observed = None, None
        elif mode == 'unseen':
            proposed, observed = None, set()
        elif mode == 'proposed_only':
            proposed = _ref_list(form, 'proposed_fact_ids')
            if not proposed:
                raise UiError(422, 'INVALID_INPUT',
                              'proposed_only needs at least one fact id in the observed list.')
            observed = set(proposed)
        else:
            raise UiError(422, 'UNKNOWN_EVIDENCE_MODE', f'Unknown evidence mode {mode!r}.')
        predicate_override = _meta(form, 'predicate_override', required=False, maximum=80)
        court = Court.from_case_store(_store(), case_id)
        decision = court.evaluate(claim['claim_id'], proposed_fact_ids=proposed,
                                  observed_fact_ids=observed,
                                  proposed_predicate=predicate_override)
        decisions = _evaluate_all(case)
    notice = (f'This run evaluated {claim["predicate"]} with evidence mode {mode}. '
              f'Status {decision.status}; reason codes '
              f'{" · ".join(decision.reason_codes) or "none"}.')
    return _investigation_page(case, decisions, run_notice=notice)


@router.post('/workbench/cases/{case_id}/investigate/live')
async def workbench_run_live_investigation(case_id: str, request: Request) -> HTMLResponse:
    """The one live form post: pinned revision + confirmed claim -> the admitted run path.

    Every refusal (stale revision, claim from another case, unconfirmed claim, missing role,
    live activation off, accounting backend unusable) is rendered as a typed HTML page with the
    same code the JSON route returns. None of them can reserve budget or reach a provider.
    """
    form = await _read_form(request)
    with _ui_errors(f'/workbench/cases/{case_id}/investigate'):
        raw_revision = _meta(form, 'revision', maximum=12)
        if not _DIGITS.match(raw_revision or ''):
            raise UiError(422, 'INVALID_INPUT', 'The pinned revision must be a positive integer.')
        claim_id = _meta(form, 'claim_id', maximum=64)
        if _run_provider is None:  # pragma: no cover - app.py always binds the runner
            raise UiError(503, 'LIVE_PATH_NOT_BOUND',
                          'The selected-case live path is not bound in this process.')
        case = _case(case_id)
        run = await _run_provider(request, case_id, claim_id, int(raw_revision))
    return _live_result_page(case, run)
