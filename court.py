"""Deterministic, closed-domain, fail-closed documentary claim adjudicator.

Important: This does NOT infer legal meaning from arbitrary text. Facts must have been reviewed
by an external auditor before they qualify for release status.

CT-WB1 generalisation
---------------------
A :class:`Court` instance binds exactly one case. The official-source host and publisher are
checked against *that case's* declared ``publisher_policy``; nothing is allowlisted globally. The
Exeter records remain available through :meth:`Court.from_exeter_sample`, which is explicitly a
sample loader, and a versioned workbench store is read through :meth:`Court.from_case_store`.

A user-declared URL is not authenticity. Sources carry a ``capture_class``:

* ``USER_SUPPLIED_EXCERPT`` — text the owner pasted/uploaded. Its ``content_sha256`` hashes those
  imported bytes only. It is never reported as an original-document hash, it can never set
  ``original_source_verified``, and :meth:`Court.evaluate` keeps reporting the capture as
  preliminary.
* ``INDEPENDENT_ORIGINAL_BYTES`` — original bytes captured and hashed outside the workbench.

The Court authority semantics are unchanged: predicate, source-role, jurisdiction, subject,
action, date, value and seen-citation invariants are enforced exactly as before. The only
eligibility addition is that a fact mapping whose human review state is not ``HUMAN_REVIEWED``
cannot support a claim (``UNREVIEWED_FACT_MAPPING``), and an empty/draft registry is reported
honestly instead of crashing or inventing evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_PREDICATES = frozenset({
    'AUTHORIZED_USE_CEILING', 'DESIGN_AUTHORIZED', 'VOTE_APPROVED',
    'CITY_REPORTS_PHASE', 'EVENT_CLOSURE_APPROVED', 'PAID_AMOUNT',
    'CONSTRUCTION_STARTED', 'PHYSICAL_COMPLETED', 'NEVER_BUILT',
})
SOURCE_ROLE_BY_PREDICATE = {
    'AUTHORIZED_USE_CEILING': 'ADOPTED_ACTION_MINUTES',
    'DESIGN_AUTHORIZED': 'ADOPTED_ACTION_MINUTES',
    'VOTE_APPROVED': 'ADOPTED_ACTION_MINUTES',
    'CITY_REPORTS_PHASE': 'CITY_INFORMATIONAL_TRACKER',
    'EVENT_CLOSURE_APPROVED': 'ADOPTED_ACTION_MINUTES',
    'PAID_AMOUNT': 'OFFICIAL_PAYMENT_REGISTER',
    'CONSTRUCTION_STARTED': 'OFFICIAL_CONSTRUCTION_RECORD',
    'PHYSICAL_COMPLETED': 'OFFICIAL_ACCEPTANCE_RECORD',
    'NEVER_BUILT': 'ADJUDICATED_NEGATIVE_EVIDENCE',
}
#: Kept as the internal spelling used throughout :meth:`Court.evaluate`.
_SOURCE_ROLES = SOURCE_ROLE_BY_PREDICATE
ALLOWED_DOCUMENT_ROLES = frozenset(SOURCE_ROLE_BY_PREDICATE.values())
CAPTURE_USER_SUPPLIED = 'USER_SUPPLIED_EXCERPT'
CAPTURE_INDEPENDENT_ORIGINAL = 'INDEPENDENT_ORIGINAL_BYTES'
CAPTURE_CLASSES = frozenset({CAPTURE_USER_SUPPLIED, CAPTURE_INDEPENDENT_ORIGINAL})
REVIEW_STATES = frozenset({'PENDING_HUMAN_REVIEW', 'HUMAN_REVIEWED',
                           'SOURCE_REVISED_REVIEW_REQUIRED', 'INDEPENDENTLY_AUDITED'})
#: A fact mapping supports a claim only after a human review (or the stronger independent audit).
ELIGIBLE_FACT_REVIEW_STATES = frozenset({'HUMAN_REVIEWED', 'INDEPENDENTLY_AUDITED'})
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_HOST_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$')

#: The Exeter records are a sample case, not the product's hardcoded universe. These two
#: declarations are sample data used only by :meth:`Court.from_exeter_sample`; the general Court
#: reads both from whatever case it was constructed with.
_EXETER_SAMPLE_POLICY = {'publisher': 'City of Exeter', 'allowed_hosts': ['cityofexeter.ca.gov']}
_EXETER_SAMPLE_RECORD_REFS = {
    'EX-MINUTES-20260428': [
        {'tool': 'read_council_item', 'args': {'item': 'J.1'}, 'action_id': 'J.1-res-2026-13'},
        {'tool': 'read_council_item', 'args': {'item': 'I.4'}, 'action_id': 'I.4-consent-event'},
    ],
    'EX-TRACKER-20261009': [
        {'tool': 'read_project_tracker', 'args': {'row': 14}, 'action_id': 'TRACKER-ROW-14'},
    ],
}


class CourtValidationError(ValueError):
    """Typed rejection of a registry/claim-book that violates a case invariant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict[str, str]:
        return {'code': self.code, 'message': str(self)}


def _validate_json_limits(value: Any) -> None:
    """Bound the local registry before hashing or traversing nested data."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 10_000 or depth > 32:
            raise ValueError('Registry exceeds structural limits')
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > 200_000:
            raise ValueError('Registry contains oversized text')
        elif current is not None and not isinstance(current, (str, int, bool)):
            raise ValueError('Registry contains unsupported value type')


def _bounded_str(value: Any, field: str, *, maximum: int, code: str = 'INVALID_INPUT') -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CourtValidationError(code, f'Invalid {field}')
    return value


def _iso_date(value: Any, field: str, *, code: str = 'INVALID_INPUT') -> date:
    try:
        return date.fromisoformat(_bounded_str(value, field, maximum=10))
    except ValueError:
        raise CourtValidationError(code, f'Invalid {field}') from None


def _retrieval_date(value: Any) -> date:
    try:
        return datetime.fromisoformat(_bounded_str(value, 'retrieved_at_utc', maximum=40)).date()
    except ValueError:
        raise CourtValidationError('INVALID_INPUT', 'Invalid retrieved_at_utc') from None


def _canonical_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _publisher_policy(value: Any) -> dict:
    if not isinstance(value, dict):
        raise CourtValidationError('MISSING_PUBLISHER_POLICY',
                                   'Case does not declare an official publisher policy')
    if set(value) - {'publisher', 'allowed_hosts'}:
        raise CourtValidationError('INVALID_PUBLISHER_POLICY',
                                   'Publisher policy contains unknown fields')
    publisher = _bounded_str(value.get('publisher'), 'publisher', maximum=200)
    hosts = value.get('allowed_hosts')
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 8:
        raise CourtValidationError('INVALID_PUBLISHER_POLICY',
                                   'Publisher policy must declare 1-8 hostnames')
    clean = []
    for host in hosts:
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host) or host != host.lower():
            raise CourtValidationError('INVALID_PUBLISHER_POLICY',
                                       'Publisher policy hostnames must be lowercase DNS names')
        if host not in clean:
            clean.append(host)
    return {'publisher': publisher, 'allowed_hosts': clean}


def _record_refs(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise CourtValidationError('INVALID_RECORD_REFS', 'record_refs must be a bounded list')
    clean = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {'tool', 'args', 'action_id'}:
            raise CourtValidationError('INVALID_RECORD_REFS', 'Invalid record reference entry')
        tool = _bounded_str(item.get('tool'), 'record_refs.tool', maximum=64)
        args = item.get('args')
        if not isinstance(args, dict) or not 1 <= len(args) <= 4:
            raise CourtValidationError('INVALID_RECORD_REFS',
                                       'Invalid record reference arguments')
        for key, arg_value in args.items():
            if not isinstance(key, str) or len(key) > 32:
                raise CourtValidationError('INVALID_RECORD_REFS',
                                           'Invalid record reference argument name')
            if type(arg_value) not in (str, int) or (isinstance(arg_value, str) and len(arg_value) > 128):
                raise CourtValidationError('INVALID_RECORD_REFS',
                                           'Record reference arguments must be bounded scalars')
        ref = {'tool': tool, 'args': dict(args)}
        if item.get('action_id') is not None:
            ref['action_id'] = _bounded_str(item['action_id'], 'record_refs.action_id', maximum=128)
        clean.append(ref)
    return clean


def _same_arguments(declared: dict, supplied: dict) -> bool:
    """Exact match including scalar type, so ``True`` never matches the integer ``1``."""
    if set(declared) != set(supplied):
        return False
    return all(type(declared[key]) is type(supplied[key]) and declared[key] == supplied[key]
               for key in declared)


@dataclass(frozen=True)
class Decision:
    claim_id: str
    text: str
    status: str
    reason_codes: tuple[str, ...]
    source_citations: tuple[dict[str, Any], ...]
    registry_sha256: str
    source_integrity_state: str
    source_capture_complete: bool
    additional_record: str
    independently_audited: bool
    finding_maturity: str
    model_predicate_accepted: bool
    original_source_verified: bool
    independent_fact_audit: str

    def as_json(self) -> dict:
        data = asdict(self)
        data['reason_codes'] = list(self.reason_codes)
        data['source_citations'] = list(self.source_citations)
        return data


class Court:
    def __init__(self, registry: dict, claim_book: dict):
        _validate_json_limits(registry)
        _validate_json_limits(claim_book)
        if not isinstance(registry, dict) or not isinstance(claim_book, dict):
            raise CourtValidationError('INVALID_REGISTRY', 'Registry and claim book must be objects')
        self.registry = registry
        self.claim_book = claim_book
        self.case_id = _bounded_str(registry.get('case_id'), 'case_id', maximum=128)
        self.publisher_policy = _publisher_policy(registry.get('publisher_policy'))
        self.case_jurisdiction = _bounded_str(registry.get('jurisdiction_id'), 'jurisdiction',
                                             maximum=64)
        self.sources = {s['source_id']: s for s in registry['sources']}
        self.facts = {f['fact_id']: f for f in registry['facts']}
        self.claims = {c['id']: c for c in claim_book['claims']}
        if len(self.sources) != len(registry['sources']) or len(self.facts) != len(registry['facts']) or len(self.claims) != len(claim_book['claims']):
            raise CourtValidationError('DUPLICATE_ID', 'Duplicate source, fact, or claim ID')
        self.capture_classes: dict[str, str] = {}
        for source in self.sources.values():
            self.capture_classes[source['source_id']] = self._validate_source(source)
        for fact in self.facts.values():
            self._validate_fact(fact)
        for claim in self.claims.values():
            self._validate_claim(claim)
        self.registry_sha256 = _canonical_hash(registry)
        self.capture_complete = bool(self.sources) and all(
            self.capture_classes[source_id] == CAPTURE_INDEPENDENT_ORIGINAL
            and _SHA256_RE.fullmatch(s['raw_sha256'] or '')
            and s.get('retrieved_at_utc')
            for source_id, s in self.sources.items()
        )
        self.audit_complete = (
            registry.get('audit_state') == 'INDEPENDENTLY_AUDITED_FACT_CONTRACT'
            and bool(self.sources)
            and all(self.capture_classes[sid] == CAPTURE_INDEPENDENT_ORIGINAL
                    for sid in self.sources)
            and all(s.get('independent_reviewer') and s.get('review_state') == 'INDEPENDENTLY_AUDITED' for s in self.sources.values())
            and bool(self.facts)
            and all(f.get('independent_reviewer') and f.get('review_state') == 'INDEPENDENTLY_AUDITED' for f in self.facts.values())
        )
        self.original_source_verified = bool(self.sources) and all(
            self.capture_classes[sid] == CAPTURE_INDEPENDENT_ORIGINAL
            and s.get('original_source_verified') is True
            for sid, s in self.sources.items()
        )
        self.independent_fact_audit = 'COMPLETE' if self.audit_complete else 'PENDING'
        if self.audit_complete:
            self.integrity_state = 'SOURCE_AUDITED_AND_PINNED'
        elif not self.sources:
            self.integrity_state = 'NO_SOURCES_RECORDED'
        elif any(capture == CAPTURE_USER_SUPPLIED for capture in self.capture_classes.values()):
            self.integrity_state = 'USER_SUPPLIED_TEXT__ORIGINAL_BYTES_UNVERIFIED'
        elif self.capture_complete:
            self.integrity_state = 'SOURCE_CAPTURED__INDEPENDENT_AUDIT_PENDING'
        else:
            self.integrity_state = 'PRELIMINARY_SOURCE_REVIEW__ORIGINAL_HASH_PENDING'

    # -- validation -------------------------------------------------------------------------

    def _validate_source(self, source: dict) -> str:
        source_id = _bounded_str(source.get('source_id'), 'source_id', maximum=128)
        case_id = source.get('case_id')
        if case_id is not None and case_id != self.case_id:
            raise CourtValidationError('WRONG_CASE_SOURCE',
                                       'Source belongs to a different case than this Court')
        capture = source.get('capture_class', CAPTURE_INDEPENDENT_ORIGINAL)
        if capture not in CAPTURE_CLASSES:
            raise CourtValidationError('INVALID_CAPTURE_CLASS', 'Unknown source capture class')
        _bounded_str(source.get('title'), 'source title', maximum=300)
        official_url = _bounded_str(source.get('official_url'), 'official_url', maximum=1000)
        try:
            parsed_url = urlsplit(official_url)
            hostname = (parsed_url.hostname or '').lower()
        except ValueError:
            raise CourtValidationError('SOURCE_URL_NOT_ALLOWLISTED',
                                       'Official source URL is not allowlisted') from None
        if (parsed_url.scheme != 'https' or not hostname
                or hostname not in self.publisher_policy['allowed_hosts']
                or parsed_url.username or parsed_url.password):
            raise CourtValidationError('SOURCE_URL_NOT_ALLOWLISTED',
                                       'Official source URL is not allowlisted')
        if source.get('publisher') != self.publisher_policy['publisher']:
            raise CourtValidationError('WRONG_PUBLISHER',
                                       'Source publisher disagrees with the case publisher policy')
        if source.get('jurisdiction_id') != self.case_jurisdiction:
            raise CourtValidationError('WRONG_CASE_JURISDICTION',
                                       'Source jurisdiction disagrees with the case')
        document_role = _bounded_str(source.get('document_role'), 'document_role', maximum=80)
        if document_role not in ALLOWED_DOCUMENT_ROLES:
            raise CourtValidationError('UNKNOWN_DOCUMENT_ROLE',
                                       'Source declares an unknown document role')
        if capture == CAPTURE_USER_SUPPLIED:
            if source.get('raw_sha256') is not None:
                raise CourtValidationError('IMPORTED_TEXT_CANNOT_CARRY_ORIGINAL_HASH',
                                           'Imported text cannot carry an original-bytes hash')
            if source.get('original_source_verified') is True:
                raise CourtValidationError('IMPORTED_TEXT_CANNOT_BE_VERIFIED',
                                           'Imported text cannot claim a verified original source')
            content_hash = source.get('content_sha256')
            if not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash):
                raise CourtValidationError('INVALID_SOURCE_HASH', 'Invalid imported-text SHA-256')
            exact_text = _bounded_str(source.get('exact_text'), 'exact_text', maximum=65_536)
            if hashlib.sha256(exact_text.encode('utf-8')).hexdigest() != content_hash:
                raise CourtValidationError('IMPORTED_TEXT_HASH_MISMATCH',
                                           'Imported-text hash does not match the stored text')
        else:
            raw_hash = source.get('raw_sha256')
            if not isinstance(raw_hash, str) or not _SHA256_RE.fullmatch(raw_hash):
                raise CourtValidationError('INVALID_SOURCE_HASH', 'Invalid source SHA-256')
        if _iso_date(source.get('as_of'), 'source as_of') > _retrieval_date(source.get('retrieved_at_utc')):
            raise CourtValidationError('INVALID_SOURCE_DATE', 'Source as_of is after retrieval date')
        _record_refs(source.get('record_refs'))
        return capture

    def _validate_fact(self, fact: dict) -> None:
        _bounded_str(fact.get('fact_id'), 'fact_id', maximum=128)
        case_id = fact.get('case_id')
        if case_id is not None and case_id != self.case_id:
            raise CourtValidationError('WRONG_CASE_FACT',
                                       'Fact belongs to a different case than this Court')
        if fact.get('source_id') not in self.sources or fact.get('predicate') not in ALLOWED_PREDICATES:
            raise CourtValidationError('UNREGISTERED_FACT_SOURCE',
                                       'Unregistered fact source or predicate')
        src = self.sources[fact['source_id']]
        _bounded_str(fact.get('subject_id'), 'subject_id', maximum=128)
        _bounded_str(fact.get('action_id'), 'action_id', maximum=128)
        if fact.get('jurisdiction_id') != src.get('jurisdiction_id'):
            raise CourtValidationError('WRONG_JURISDICTION', 'Source jurisdiction disagrees with fact')
        if _SOURCE_ROLES.get(fact['predicate']) != src.get('document_role'):
            raise CourtValidationError('WRONG_SOURCE_ROLE',
                                       'Fact uses wrong source authority class')
        _bounded_str(fact.get('anchor'), 'documentary anchor', maximum=500)
        _bounded_str(fact.get('exact_excerpt'), 'exact excerpt', maximum=2000)
        if not isinstance(fact.get('qualifiers'), list) or len(fact['qualifiers']) > 16 or any(
            not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in fact['qualifiers']
        ):
            raise CourtValidationError('INVALID_QUALIFIERS', 'Invalid fact qualifiers')
        if _iso_date(fact.get('reported_at'), 'fact reported_at') != _iso_date(src.get('as_of'), 'source as_of'):
            raise CourtValidationError('FACT_DATE_MISMATCH', 'Fact date disagrees with source as_of')
        if fact.get('review_state', 'HUMAN_REVIEWED') not in REVIEW_STATES:
            raise CourtValidationError('INVALID_FACT_REVIEW_STATE', 'Invalid fact review state')
        for key in ('amount_cents', 'yea', 'nay', 'abstain'):
            if key in fact and (type(fact[key]) is not int or fact[key] < 0):
                raise CourtValidationError('INVALID_FACT_VALUE', f'Invalid fact {key}')
        for key in ('phase', 'event_date'):
            if key in fact and not isinstance(fact[key], str):
                raise CourtValidationError('INVALID_FACT_VALUE', f'Invalid fact {key}')
        normal_forms = fact.get('normal_forms')
        if normal_forms is not None:
            if not isinstance(normal_forms, dict) or len(normal_forms) > 16:
                raise CourtValidationError('INVALID_FACT_NORMAL_FORMS',
                                           'Invalid fact normal forms')
            for name, value in normal_forms.items():
                if not isinstance(name, str) or len(name) > 64:
                    raise CourtValidationError('INVALID_FACT_NORMAL_FORMS',
                                               'Invalid fact normal form name')
                if not isinstance(value, str) or len(value) > 2000:
                    raise CourtValidationError('INVALID_FACT_NORMAL_FORMS',
                                               'Invalid fact normal form value')

    def _validate_claim(self, claim: dict) -> None:
        _bounded_str(claim.get('id'), 'claim ID', maximum=64)
        case_id = claim.get('case_id')
        if case_id is not None and case_id != self.case_id:
            raise CourtValidationError('WRONG_CASE_CLAIM',
                                       'Claim belongs to a different case than this Court')
        _bounded_str(claim.get('text'), 'claim text', maximum=1000)
        _bounded_str(claim.get('additional_record'), 'additional record', maximum=500)
        if claim.get('predicate') not in ALLOWED_PREDICATES:
            raise CourtValidationError('UNKNOWN_CLAIM_PREDICATE',
                                       'Claim declares an unknown predicate')
        for key in ('subject_id', 'action_id'):
            _bounded_str(claim.get(key), key, maximum=128)
        if claim.get('jurisdiction_id') != self.case_jurisdiction:
            raise CourtValidationError('WRONG_CASE_JURISDICTION',
                                       'Claim jurisdiction disagrees with the case')
        fact_ids = claim.get('fact_ids')
        if not isinstance(fact_ids, list) or len(fact_ids) > 8 or any(
                not isinstance(item, str) for item in fact_ids):
            raise CourtValidationError('INVALID_FACT_CITATIONS', 'Invalid fact citation list')
        for fact_id in fact_ids:
            if fact_id not in self.facts:
                raise CourtValidationError('WRONG_CASE_FACT',
                                           'Claim cites a fact that is not registered in this case')
        if 'as_of' in claim:
            _iso_date(claim['as_of'], 'claim as_of')

    # -- loaders ----------------------------------------------------------------------------

    @classmethod
    def from_exeter_sample(cls, directory: Path | None = None) -> Court:
        """Load the shipped Exeter records as *the sample case*.

        The host/publisher policy and the read-tool selectors below are sample data. The general
        path is :meth:`from_case_store`; :meth:`from_directory` remains only as a legacy alias so
        existing callers keep working.
        """
        base = directory or (ROOT / 'data')
        registry = copy.deepcopy(json.loads((base / 'exeter_registry.json').read_text(encoding='utf-8')))
        claim_book = json.loads((base / 'claims.json').read_text(encoding='utf-8'))
        registry.setdefault('publisher_policy', copy.deepcopy(_EXETER_SAMPLE_POLICY))
        registry.setdefault('evidence_scope', 'SAMPLE_CASE_EXETER')
        for source in registry['sources']:
            source.setdefault('capture_class', CAPTURE_INDEPENDENT_ORIGINAL)
            source.setdefault('original_source_verified', False)
            source.setdefault('provenance_label',
                              'CAPTURED_OFFICIAL_BYTES__INDEPENDENT_AUDIT_PENDING')
            source['record_refs'] = copy.deepcopy(_EXETER_SAMPLE_RECORD_REFS.get(source['source_id'], []))
        return cls(registry, claim_book)

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> Court:
        """Legacy alias for :meth:`from_exeter_sample`; retained for existing callers."""
        return cls.from_exeter_sample(directory)

    @classmethod
    def from_case_store(cls, case_store, case_id: str) -> Court:
        """General path: bind one Court to one stored case.

        ``case_store`` is any object exposing ``get_case(case_id)`` (see
        :class:`civictrace.case_store.CaseStore`). The projection lives in the store module, so
        this class never learns the store's on-disk schema.
        """
        from .case_store import case_claim_book, case_registry

        case = case_store.get_case(case_id)
        return cls(case_registry(case), case_claim_book(case))

    # -- citations --------------------------------------------------------------------------

    def _citations(self, facts: list[dict]) -> tuple[dict, ...]:
        entries = []
        for fact in facts:
            source = self.sources[fact['source_id']]
            capture = self.capture_classes.get(fact['source_id'], CAPTURE_INDEPENDENT_ORIGINAL)
            provenance_label = source.get('provenance_label') or (
                'USER_SUPPLIED_EXCERPT__ORIGINAL_BYTES_UNVERIFIED'
                if capture == CAPTURE_USER_SUPPLIED
                else 'CAPTURED_OFFICIAL_BYTES__INDEPENDENT_AUDIT_PENDING'
            )
            entries.append({
                'fact_id': fact['fact_id'],
                'source_id': fact['source_id'],
                'title': source['title'],
                'url': source['official_url'],
                'document_role': source['document_role'],
                'anchor': fact['anchor'],
                'exact_excerpt': fact['exact_excerpt'],
                'document_as_of': source['as_of'],
                'raw_sha256': source.get('raw_sha256'),
                'content_sha256': source.get('content_sha256'),
                'capture_class': capture,
                'hash_subject': ('ORIGINAL_SOURCE_BYTES' if capture == CAPTURE_INDEPENDENT_ORIGINAL
                                 else 'IMPORTED_TEXT_NOT_ORIGINAL_BYTES'),
                'original_source_verified': source.get('original_source_verified') is True,
                'provenance_label': provenance_label,
                'meaning': 'Primary-record location; not proof of all implied downstream events',
            })
        return tuple(entries)

    # -- adjudication -----------------------------------------------------------------------

    def evaluate(self, claim_id: str, *, proposed_fact_ids: list[str] | None = None,
                 observed_fact_ids: set[str] | None = None, model_claim_id: str | None = None,
                 proposed_predicate: str | None = None) -> Decision:
        """Trusted claim ID binds predicate. Model cannot rewrite the requested question.

        proposed_fact_ids: model-cited IDs (when None, evaluate against preselected
          curated context; supports static documented question answering).
        observed_fact_ids: facts actually given by dispatched tools; use this in
          model mode so an unvisited fact cannot support a proposed answer.
        """
        if claim_id not in self.claims:
            raise CourtValidationError('UNKNOWN_CLAIM', 'Unknown trusted challenge identifier')
        c = self.claims[claim_id]
        effective = c['fact_ids'] if proposed_fact_ids is None else proposed_fact_ids
        if not isinstance(effective, list) or len(effective) > 8 or any(not isinstance(v, str) for v in effective):
            raise CourtValidationError('INVALID_FACT_CITATIONS', 'Invalid fact citation list')
        reason: list[str] = []
        selected: list[dict] = []
        label = 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
        model_accepted = True
        evidence_cutoff = (max(_retrieval_date(s['retrieved_at_utc']) for s in self.sources.values())
                           if self.sources else None)
        if evidence_cutoff is None:
            reason.append('NO_EVIDENCE_RECORDED')
        elif 'as_of' in c and _iso_date(c['as_of'], 'claim as_of') > evidence_cutoff:
            reason.append('AS_OF_AFTER_EVIDENCE_CUTOFF')
        if model_claim_id is not None and model_claim_id != claim_id:
            model_accepted = False
            reason.append('CLAIM_ID_RELABELED')
        if proposed_predicate is not None and proposed_predicate != c['predicate']:
            model_accepted = False
            reason.append('PREDICATE_RELABELED')
        if len(effective) != len(set(effective)):
            reason.append('DUPLICATE_FACT_ID')
        missing = set(effective).difference(self.facts)
        if missing:
            reason.append('UNREGISTERED_FACT')
        if observed_fact_ids is not None and set(effective).difference(observed_fact_ids):
            reason.append('UNSEEN_SOURCE_CITATION')
        candidates = [self.facts[fid] for fid in effective if fid in self.facts]
        unreviewed = [f['fact_id'] for f in candidates
                      if f.get('review_state', 'HUMAN_REVIEWED') not in ELIGIBLE_FACT_REVIEW_STATES]
        if unreviewed:
            reason.append('UNREVIEWED_FACT_MAPPING')
        eligible = []
        mismatched = []
        wrong_action = False
        for fact in candidates:
            src = self.sources[fact['source_id']]
            if fact['jurisdiction_id'] != c['jurisdiction_id'] or src['jurisdiction_id'] != c['jurisdiction_id']:
                mismatched.append('WRONG_JURISDICTION')
            elif fact['subject_id'] != c['subject_id'] or (
                fact['predicate'] == c['predicate'] and fact['action_id'] != c['action_id']
            ):
                wrong_action = True
            elif fact['predicate'] != c['predicate'] or src['document_role'] != _SOURCE_ROLES[c['predicate']]:
                mismatched.append('PREDICATE_OR_DOCUMENT_ROLE_MISMATCH')
            elif (('as_of' in c and fact.get('reported_at') != c['as_of']) or any(k in c and fact.get(k) != c[k] for k in ('amount_cents','phase','event_date','yea','nay','abstain'))):
                mismatched.append('DOCUMENTARY_VALUE_CONTRADICTION')
            elif c.get('ceiling') and 'UP_TO' not in fact.get('qualifiers', []):
                mismatched.append('CEILING_QUALIFIER_MISSING')
            else:
                eligible.append(fact)
        # Model-scoped proposals with extra wrong citations never become green merely
        # because one legitimate fact was also included.
        disqualifying = any(x in reason for x in (
            'UNREGISTERED_FACT', 'UNSEEN_SOURCE_CITATION', 'CLAIM_ID_RELABELED',
            'PREDICATE_RELABELED', 'DUPLICATE_FACT_ID', 'AS_OF_AFTER_EVIDENCE_CUTOFF',
            'UNREVIEWED_FACT_MAPPING', 'NO_EVIDENCE_RECORDED',
        ))
        if proposed_fact_ids is not None and eligible and (wrong_action or mismatched):
            disqualifying = True
            reason.append('MIXED_OR_INVALID_MODEL_CITATIONS')
        if eligible and not disqualifying:
            selected = eligible
            label = 'CITY_REPORTED_STATUS' if c['predicate'] == 'CITY_REPORTS_PHASE' else 'DOCUMENTED_ACTION'
            reason.append('MATCHES_REGISTERED_FACT_CONTRACT')
        elif mismatched or wrong_action:
            if wrong_action:
                reason.append('WRONG_ACTION_SOURCE')
                label = 'WRONG_ACTION_SOURCE' if not disqualifying else 'REVIEW_REQUIRED'
            reason.extend(sorted(set(mismatched)))
            if 'DOCUMENTARY_VALUE_CONTRADICTION' in mismatched and not wrong_action and not disqualifying:
                label = 'CONTRADICTED_BY_RECORD'
        if c['predicate'] == 'PAID_AMOUNT' and not eligible:
            reason.append('CEILING_NOT_PAYMENT')
        if c['predicate'] in ('CONSTRUCTION_STARTED','PHYSICAL_COMPLETED','NEVER_BUILT') and not eligible:
            reason.append('ABSENCE_IS_NOT_NEGATIVE_PROOF')
        if c['predicate'] in ('PHYSICAL_COMPLETED','NEVER_BUILT') and not eligible:
            reason.append('TRACKER_REPORT_NOT_PHYSICAL_VERIFICATION')
        if c['predicate'] == 'CITY_REPORTS_PHASE' and label == 'CITY_REPORTED_STATUS':
            reason.append('CITY_REPORT_ONLY_NOT_FIELD_INSPECTION')
        if not eligible and not reason:
            reason.append('NO_ELIGIBLE_SOURCE_CLASS_OR_FACT')
        if disqualifying:
            label = 'REVIEW_REQUIRED'
            selected = []
        # On refusals, show contextual documentary records without treating them as affirmative claim support.
        trusted_context = [self.facts[fid] for fid in c['fact_ids'] if fid in self.facts]
        selected_for_display = selected if selected else trusted_context
        return Decision(
            claim_id=claim_id, text=c['text'], status=label,
            reason_codes=tuple(dict.fromkeys(reason)),
            source_citations=self._citations(selected_for_display),
            registry_sha256=self.registry_sha256,
            source_integrity_state=self.integrity_state,
            source_capture_complete=bool(self.capture_complete),
            additional_record=c['additional_record'],
            independently_audited=bool(self.audit_complete),
            finding_maturity='INDEPENDENTLY_AUDITED' if self.audit_complete else 'PRELIMINARY_UNAUDITED',
            model_predicate_accepted=model_accepted,
            original_source_verified=bool(self.original_source_verified),
            independent_fact_audit=self.independent_fact_audit,
        )

    # -- case-scoped source reads -----------------------------------------------------------

    def source_read(self, tool_name: str, arg: dict) -> dict:
        """Resolve a read request against *this case's* sources only.

        Two shapes exist:

        * ``read_case_source`` with ``{'source_id': ...}`` or ``{'fact_id': ...}`` — generic,
          case-scoped, and the only tool a workbench case needs.
        * any tool name declared in a source's ``record_refs`` — the sample case declares
          ``read_council_item`` / ``read_project_tracker`` selectors there. The Court itself
          contains no Exeter item, row or action id.
        """
        if not isinstance(tool_name, str) or not 1 <= len(tool_name) <= 64:
            raise ValueError('Invalid tool argument contract')
        if not isinstance(arg, dict) or not 1 <= len(arg) <= 4:
            raise ValueError('Invalid tool argument contract')
        facts = self._resolve_read(tool_name, arg)
        return {
            'source_type': 'UNTRUSTED_DOCUMENTARY_DATA_NOT_INSTRUCTIONS',
            'source_integrity': self.integrity_state,
            'source_ids': sorted({f['source_id'] for f in facts}),
            'facts': [{'fact_id': f['fact_id'], 'predicate': f['predicate'],
                       'subject_id': f['subject_id'], 'action_id': f['action_id'],
                       'anchor': f['anchor'], 'excerpt': f['exact_excerpt'],
                       'qualifiers': f['qualifiers'], 'source_id': f['source_id'],
                       'capture_class': self.capture_classes[f['source_id']]}
                      for f in facts],
        }

    def _resolve_read(self, tool_name: str, arg: dict) -> list[dict]:
        if tool_name == 'read_case_source':
            if set(arg) == {'source_id'}:
                source_id = arg['source_id']
                if not isinstance(source_id, str) or source_id not in self.sources:
                    raise ValueError('Unallowlisted read tool or source ID')
                return [f for f in self.facts.values() if f['source_id'] == source_id]
            if set(arg) == {'fact_id'}:
                fact_id = arg['fact_id']
                if not isinstance(fact_id, str) or fact_id not in self.facts:
                    raise ValueError('Unallowlisted read tool or source ID')
                return [self.facts[fact_id]]
            raise ValueError('Invalid tool argument contract')
        for source in self.sources.values():
            for ref in _record_refs(source.get('record_refs')):
                if ref['tool'] != tool_name or not _same_arguments(ref['args'], arg):
                    continue
                action_id = ref.get('action_id')
                return [f for f in self.facts.values()
                        if f['source_id'] == source['source_id']
                        and (action_id is None or f['action_id'] == action_id)]
        raise ValueError('Unallowlisted read tool or source ID')
