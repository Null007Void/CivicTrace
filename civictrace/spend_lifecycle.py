"""Reservation lifecycle, transmission window and evidence classification (CT-QC-039).

The decisive rule this module exists to enforce:

    **Expiring a reservation never refunds spending allowance.**

A reservation is a worst-case *charge* plus a *lease*. Those are two independent dimensions, and
conflating them is how an accounting system silently creates money:

``state`` (lease/disposition, the pre-existing column)
    ``active`` → ``settled`` (an outcome was recorded while the lease was held) /
    ``expired`` (the TTL sweep released the lease, nothing was recorded) /
    ``settled_expired`` (the lease was released and a late outcome was recorded afterwards).
    ``expired`` means *the slot is free again*; it never means *the money came back*.

``accounting_state`` (spend lifecycle, authoritative for allowance)
    ``active``
        Reserved. Nothing has been sent. The unit is committed; the request may still be issued.
    ``settled``
        A provider outcome was recorded for this identity (usage returned). The unit is spent.
    ``failed_confirmed``
        Positive evidence that the provider cannot have charged. **Before** transmission that is
        ``CONNECT_REFUSED_PRE_DISPATCH`` / ``CONNECT_TIMEOUT_PRE_DISPATCH`` /
        ``LOCAL_FAILURE_PRE_DISPATCH``. **After** transmission the only admissible evidence is a
        provider-confirmed, unambiguous no-charge record: ``PROVIDER_CONFIRMED_NO_CHARGE`` (an
        authenticated provider response stating the request was not processed and no billable
        execution occurred) or ``PROVIDER_ZERO_USAGE_RECORD`` (a provider-side settlement/usage
        record showing zero usage for this request identity). A status code — 5xx in particular —
        is **not** such evidence (CT-QC-041). Only this state is *eligible* for a refund, and a
        refund is an explicit, once-only operator/recovery action
        (:meth:`civictrace.spend_gate.SpendGate.refund_failed_confirmed`) — never a side effect.
    ``uncertain``
        Transmission may have happened and no provider-confirmed no-charge record exists. The
        allowance stays consumed until reconciliation, however long that takes. This is the state
        an expired, unresolved reservation lands in, the state a dispatch that timed out lands in,
        and — since CT-QC-041 — the state any request whose only failure evidence is an HTTP
        status code lands in, a 5xx included.

Transitions (each one appends an accounting-history row, in both backends):

    active --dispatch--> uncertain --settled outcome--> settled
       |                     |----provider-confirmed no-charge--> failed_confirmed
       |----success--------> settled
       |----confirmed no-charge--> failed_confirmed
       |----expiry (no evidence)--> uncertain                (lease released)

    settled/expired (accounting uncertain) --reliable no-charge record--> failed_confirmed
                                           --provider usage record--> settled
    (the second line is reconciliation of an already-dispositioned row: one accounting-only
    transition, exactly once, no lease change and no automatic refund)

Nothing in this module moves money. It classifies evidence; the stores apply the classification.

The transmission window
-----------------------
The window opens when the client hands a serialized request to the transport for sending and
closes when a complete provider response (or a provider-confirmed failure) is observed. Inside
that window the process cannot know whether bytes reached the provider, so the only safe
accounting answer is ``uncertain``.

What *does* prove no charge (``failed_confirmed``):

* **before** transmission: the connection was never established — DNS failure, TCP connect
  refused, connect timeout. No HTTP request was delivered, so no completion can have been billed;
* **before** transmission: the failure is local and pre-dispatch — no connection was obtained
  from the pool, or the request could not be serialized/sent;
* **after** transmission: an authenticated provider response that explicitly states the request
  was not processed and no billable execution occurred (``PROVIDER_CONFIRMED_NO_CHARGE``);
* **after** transmission: a provider-side settlement/usage record showing zero usage for this
  request identity (``PROVIDER_ZERO_USAGE_RECORD``).

What a status code does *not* prove (CT-QC-041)
----------------------------------------------
A received HTTP failure status used to be treated as proof that nothing was billed. It is not:
by the time a status arrives the request has been transmitted, and a provider that returns 500,
502, 503 or 504 may already have executed billable processing (it may have run inference and
failed while writing the response, or failed after the model call). ``PROVIDER_FAILURE_RESPONSE``
therefore means *"a status arrived"* and nothing more; it classifies ``uncertain``. Refunding on a
status code refunds a request that may have been served, which is exactly the accounting defect
this module exists to prevent. The same applies to a 2xx whose body is unusable
(``PROVIDER_HTTP_ERROR``/``INVALID_PROVIDER_RESPONSE`` telemetry carries a status only) — a 200
is even weaker evidence of non-charge.

What forces ``uncertain`` (never refundable on that evidence alone):

* any HTTP status after transmission, 5xx included (the defect CT-QC-041 corrects);
* a timeout after the request was sent (read timeout, write timeout);
* a connection dropped mid-call (protocol error, reset, EOF before a full response);
* a crash after the send was initiated but before a response was recorded;
* a no-charge *claim* that is not backed by the provider confirmation the class requires (see
  ``NO_CHARGE_SUPPORT``): an unsubstantiated claim is not evidence;
* no evidence at all. Absence of evidence is not evidence of absence: an unknown outcome is
  treated as *possibly charged*, because a refund on a maybe-charge is a real loss.

A ``SETTLED``/``SUCCESS`` outcome is recorded as ``settled``; it is not a refund either.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

# -- lease/disposition states (the pre-existing `state` column) --------------------------------

STATE_ACTIVE = 'active'
STATE_SETTLED = 'settled'
STATE_EXPIRED = 'expired'
STATE_SETTLED_EXPIRED = 'settled_expired'
VALID_STATES = frozenset({STATE_ACTIVE, STATE_SETTLED, STATE_EXPIRED, STATE_SETTLED_EXPIRED})
TERMINAL_STATES = frozenset({STATE_SETTLED, STATE_SETTLED_EXPIRED})

# -- spend lifecycle states (authoritative for allowance) --------------------------------------

LIFECYCLE_ACTIVE = 'active'
LIFECYCLE_SETTLED = 'settled'
LIFECYCLE_FAILED_CONFIRMED = 'failed_confirmed'
LIFECYCLE_UNCERTAIN = 'uncertain'
VALID_LIFECYCLE_STATES = frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_SETTLED,
                                    LIFECYCLE_FAILED_CONFIRMED, LIFECYCLE_UNCERTAIN})
# Only these may ever be refunded, and only through an explicit once-only call.
REFUNDABLE_STATES = frozenset({LIFECYCLE_FAILED_CONFIRMED})

# -- evidence classes -------------------------------------------------------------------------

#: The provider never received a request: DNS/connect refused before transmission.
EVIDENCE_CONNECT_REFUSED = 'CONNECT_REFUSED_PRE_DISPATCH'
#: The connection was never established (connect timeout). No request bytes were delivered.
EVIDENCE_CONNECT_TIMEOUT = 'CONNECT_TIMEOUT_PRE_DISPATCH'
#: A local failure before dispatch (no connection from the pool, unserializable request).
EVIDENCE_LOCAL_PRE_DISPATCH = 'LOCAL_FAILURE_PRE_DISPATCH'
#: A provider status response arrived. **Not** evidence of non-charge: the request was already
#: transmitted, so the provider may have executed billable processing before/during the error.
#: Kept as its own class (rather than deleted) so existing stored ``evidence_class`` values and
#: telemetry keep their meaning; CT-QC-041 changed its classification, not its wire value.
EVIDENCE_PROVIDER_RESPONSE = 'PROVIDER_FAILURE_RESPONSE'
#: An authenticated provider response explicitly stating the request was not processed and no
#: billable execution occurred. Refund-permitting, but only with ``NO_CHARGE_SUPPORT`` fields.
EVIDENCE_PROVIDER_NO_CHARGE = 'PROVIDER_CONFIRMED_NO_CHARGE'
#: A provider-side settlement/usage record showing zero usage for this request identity.
#: Refund-permitting, but only with ``NO_CHARGE_SUPPORT`` fields.
EVIDENCE_ZERO_USAGE = 'PROVIDER_ZERO_USAGE_RECORD'
#: The request was sent and the response never arrived.
EVIDENCE_TIMEOUT_AFTER_SEND = 'TIMEOUT_AFTER_SEND'
#: The connection dropped after the request was sent and before a full response.
EVIDENCE_DROP_MID_CALL = 'CONNECTION_DROPPED_MID_CALL'
#: The process died after dispatch with no outcome recorded (recovered by a later reader).
EVIDENCE_CRASH_AFTER_SEND = 'CRASH_AFTER_SEND'
#: A dispatch was recorded and nothing else is known.
EVIDENCE_UNCONFIRMED_AFTER_SEND = 'UNCONFIRMED_AFTER_SEND'
#: No evidence was supplied at all.
EVIDENCE_NONE = 'NO_EVIDENCE'
#: A provider usage record was returned (settlement, not a failure).
EVIDENCE_USAGE_RECORDED = 'PROVIDER_USAGE_RECORDED'

PRE_DISPATCH_CLASSES = frozenset({EVIDENCE_CONNECT_REFUSED, EVIDENCE_CONNECT_TIMEOUT,
                                 EVIDENCE_LOCAL_PRE_DISPATCH})
#: Classes that can only be produced after a request was handed to the transport. None of them
#: proves a non-charge on its own; ``PROVIDER_FAILURE_RESPONSE`` (a status code) is the one that
#: used to be mistaken for proof (CT-QC-041).
AFTER_SEND_CLASSES = frozenset({EVIDENCE_PROVIDER_RESPONSE, EVIDENCE_TIMEOUT_AFTER_SEND,
                               EVIDENCE_DROP_MID_CALL, EVIDENCE_CRASH_AFTER_SEND,
                               EVIDENCE_UNCONFIRMED_AFTER_SEND, EVIDENCE_NONE})
#: Post-transmission evidence that, with its required support, proves no billable execution.
#: This is the *only* way a transmitted request becomes ``failed_confirmed``.
NO_CHARGE_CLASSES = frozenset({EVIDENCE_PROVIDER_NO_CHARGE, EVIDENCE_ZERO_USAGE})
KNOWN_CLASSES = (PRE_DISPATCH_CLASSES | AFTER_SEND_CLASSES | NO_CHARGE_CLASSES
                 | frozenset({EVIDENCE_USAGE_RECORDED}))

#: A class name is a claim; these are the fields that make it provider-confirmed. A no-charge
#: claim whose support is absent, of the wrong type or contradictory is *not* evidence and is
#: downgraded to ``UNCONFIRMED_AFTER_SEND`` (uncertain), so a caller cannot refund by asserting
#: a string. ``usage_units`` must be the integer 0 — ``False`` and ``0.0`` are not zero units.
NO_CHARGE_SUPPORT: dict[str, tuple[tuple[str, Any], ...]] = {
    EVIDENCE_PROVIDER_NO_CHARGE: (('provider_response_received', True),
                                  ('no_charge_confirmed', True)),
    EVIDENCE_ZERO_USAGE: (('usage_evidence_source', 'PROVIDER_SETTLEMENT_RECORD'),
                          ('usage_units', 0)),
}

#: Only explicit PRE_DISPATCH evidence or a *supported* no-charge record permits a refund.
#: A status code never does: it is a received response, not a statement about billing.
REFUND_PERMITTING_CLASSES = PRE_DISPATCH_CLASSES | NO_CHARGE_CLASSES

#: Transport-exception-class → evidence class. Keyed by exception class *name* so the mapping is
#: unit-testable without constructing transport exceptions and identical for every adapter.
_TRANSPORT_ERROR_CLASSES: dict[str, str] = {
    'ConnectError': EVIDENCE_CONNECT_REFUSED,
    'ConnectTimeout': EVIDENCE_CONNECT_TIMEOUT,
    'PoolTimeout': EVIDENCE_LOCAL_PRE_DISPATCH,
    'ReadTimeout': EVIDENCE_TIMEOUT_AFTER_SEND,
    'WriteTimeout': EVIDENCE_TIMEOUT_AFTER_SEND,
    'RemoteProtocolError': EVIDENCE_DROP_MID_CALL,
    'ReadError': EVIDENCE_DROP_MID_CALL,
    'NetworkError': EVIDENCE_DROP_MID_CALL,
}

_SUCCESS_OUTCOMES = frozenset({'SUCCESS', 'SETTLED', 'OK'})

#: Human-readable classification table, mirrored verbatim in ``CT-COR3E-REPORT.md`` and in the
#: probes, so the report cannot drift from the code it documents. CT-QC-041: a status response
#: (``PROVIDER_FAILURE_RESPONSE``) is listed as uncertain, and the two no-charge classes are the
#: only post-transmission evidence that permits a refund.
CLASSIFICATION_TABLE: tuple[tuple[str, str, str], ...] = (
    (EVIDENCE_CONNECT_REFUSED, 'failed_confirmed', 'refund permitted (explicit call only)'),
    (EVIDENCE_CONNECT_TIMEOUT, 'failed_confirmed', 'refund permitted (explicit call only)'),
    (EVIDENCE_LOCAL_PRE_DISPATCH, 'failed_confirmed', 'refund permitted (explicit call only)'),
    (EVIDENCE_PROVIDER_NO_CHARGE, 'failed_confirmed',
     'refund permitted (explicit call only); requires provider_response_received + '
     'no_charge_confirmed'),
    (EVIDENCE_ZERO_USAGE, 'failed_confirmed',
     'refund permitted (explicit call only); requires usage_evidence_source='
     'PROVIDER_SETTLEMENT_RECORD + usage_units=0'),
    (EVIDENCE_PROVIDER_RESPONSE, 'uncertain',
     'never refunded: a status code (5xx included) is not a no-charge record'),
    (EVIDENCE_TIMEOUT_AFTER_SEND, 'uncertain', 'never refunded'),
    (EVIDENCE_DROP_MID_CALL, 'uncertain', 'never refunded'),
    (EVIDENCE_CRASH_AFTER_SEND, 'uncertain', 'never refunded'),
    (EVIDENCE_UNCONFIRMED_AFTER_SEND, 'uncertain', 'never refunded'),
    (EVIDENCE_NONE, 'uncertain', 'never refunded'),
    (EVIDENCE_USAGE_RECORDED, 'settled', 'not refundable (usage was returned)'),
)

SETTLEMENT_POLICY = (
    'a reservation is committed once and is never refunded automatically; an unresolved or '
    'expired reservation becomes uncertain (lease released, allowance still consumed); only '
    'failed_confirmed may be refunded, by one explicit call, and after transmission that state '
    'requires a provider-confirmed no-charge record (a status code, 5xx included, is never '
    'sufficient)'
)


@dataclass(frozen=True)
class Classification:
    """The accounting state an outcome maps to, with the evidence it rests on."""

    accounting_state: str
    evidence_class: str
    refundable: bool
    reason: str


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of one settlement/reconciliation attempt, addressed by durable identity.

    ``applied`` is True only when this call performed the first settlement of the reservation.
    ``transitioned`` (kept as the return value of ``settle``) is True only when the lease was
    still held, i.e. ``active -> settled``; a late response after expiry reconciles
    (``applied`` True, ``transitioned`` False) and a duplicate is a no-op
    (``applied`` False, ``duplicate`` True).
    """

    applied: bool
    transitioned: bool
    duplicate: bool
    reason: str
    reservation_id: str = ''
    request_digest: str = ''
    lease_state: str = ''
    accounting_state: str = ''
    evidence_class: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {'applied': self.applied, 'transitioned': self.transitioned,
                'duplicate': self.duplicate, 'reason': self.reason,
                'reservation_id': self.reservation_id,
                'request_digest': self.request_digest, 'lease_state': self.lease_state,
                'accounting_state': self.accounting_state,
                'evidence_class': self.evidence_class}


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def request_digest(intent: Mapping[str, Any] | None) -> str:
    """Deterministic request-intent digest: the durable identity of one logical request.

    Two callers that present the same intent produce the same digest, which is what makes a
    duplicate admission detectable *before* a second reservation (and a second provider request)
    can happen. Values are canonicalised as sorted JSON so dict ordering cannot change identity.
    """
    return _digest({'intent': dict(intent or {})})


def default_request_digest(deployment_id: str, reservation_id: str) -> str:
    """Identity for a reservation whose caller supplied no intent: unique and stable.

    Derived from the reservation id, never from a pid, clock or random value that a retry could
    not reproduce, so the row's identity survives restarts and revisions.
    """
    return _digest({'deployment_id': deployment_id, 'reservation_id': reservation_id})


def legacy_request_digest(reservation_id: str) -> str:
    """Deterministic identity assigned to a pre-CT-QC-039 row during schema migration.

    No digest was recorded for those rows, so one is derived from the reservation id they *did*
    have. It is marked ``legacy:`` in the digest payload so it can never be confused with a
    caller-supplied intent.
    """
    return _digest({'legacy_reservation_id': reservation_id})


def is_digest(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(ch in '0123456789abcdef' for ch in value))


def evidence_class_for_transport_error(exception_name: str) -> str:
    """Map a transport exception class name to an evidence class.

    Unknown exception classes are ``CONNECTION_DROPPED_MID_CALL`` (i.e. uncertain): a transport
    that failed in a way we cannot classify must never be treated as proof that nothing was
    charged.
    """
    return _TRANSPORT_ERROR_CLASSES.get(str(exception_name), EVIDENCE_DROP_MID_CALL)


def _no_charge_support_ok(evidence_class: str, evidence: Mapping[str, Any]) -> bool:
    """True only when a no-charge claim carries the provider fields its class requires.

    Booleans are compared by identity (``1 == True`` must not pass for a real confirmation) and
    ``usage_units`` must be the integer ``0`` (``False``/``0.0`` are not a zero-usage record).
    """
    for key, expected in NO_CHARGE_SUPPORT[evidence_class]:
        actual = evidence.get(key)
        if type(expected) is bool:
            if actual is not expected:
                return False
        elif type(expected) is int:
            if type(actual) is not int or actual != expected:
                return False
        elif not isinstance(actual, str) or actual != expected:
            return False
    return True


def evidence_from_telemetry(evidence: Mapping[str, Any] | None) -> tuple[str, bool]:
    """Read ``(evidence_class, provider_answered)`` out of caller-supplied telemetry.

    Only sanitized keys are trusted; an unknown/absent class is ``NO_EVIDENCE`` and an absent
    provider-response flag is ``False``. Callers cannot smuggle a refund by passing an arbitrary
    dict. Two further gates apply since CT-QC-041:

    * naming a no-charge class is a *claim*, honoured only when the class's ``NO_CHARGE_SUPPORT``
      fields are present and well-typed; otherwise the claim is downgraded to
      ``UNCONFIRMED_AFTER_SEND`` (uncertain) and no refund is possible;
    * a status code is never promoted into a no-charge class. ``http_status`` only sets
      ``answered``, which now means "a response arrived", not "nothing was billed".
    """
    if not isinstance(evidence, Mapping):
        return EVIDENCE_NONE, False
    raw_class = evidence.get('transmission_evidence')
    answered = (evidence.get('provider_response_received') is True
                or type(evidence.get('http_status')) is int)
    if not isinstance(raw_class, str) or raw_class not in KNOWN_CLASSES:
        return EVIDENCE_NONE, answered
    if raw_class in NO_CHARGE_CLASSES:
        if _no_charge_support_ok(raw_class, evidence):
            return raw_class, answered
        return EVIDENCE_UNCONFIRMED_AFTER_SEND, answered
    return raw_class, answered


#: Reason attached to every uncertain classification that rests on a status code. A status shows
#: the provider *saw* the request; it says nothing about billable processing before the error.
STATUS_ONLY_REASON = (
    'a provider status response was received after transmission; a status code shows the provider '
    'saw the request but not whether billable processing occurred, so the allowance stays '
    'consumed until a provider-confirmed no-charge record is reconciled')


def classify(outcome: str, evidence: Mapping[str, Any] | None = None, *,
             dispatched: bool = False) -> Classification:
    """Classify one settlement attempt. Conservative by construction.

    Ordering matters and is intentional:

    1. a success outcome is ``settled`` — usage was recorded, so there is nothing to refund;
    2. an explicit ``PROVIDER_USAGE_RECORDED`` class is ``settled`` for the same reason;
    3. an explicit pre-dispatch evidence class is ``failed_confirmed`` — no request was ever
       delivered, so no provider can have charged for it;
    4. a *supported* provider no-charge record (``PROVIDER_CONFIRMED_NO_CHARGE`` /
       ``PROVIDER_ZERO_USAGE_RECORD``) is ``failed_confirmed`` — the only post-transmission
       evidence that may permit a refund;
    5. a status response (``PROVIDER_FAILURE_RESPONSE``, any HTTP status including 5xx) is
       ``uncertain`` — the request was transmitted and the status says nothing about billing
       (CT-QC-041: this used to be ``failed_confirmed``, the defect being corrected);
    6. everything else is ``uncertain``, including "no evidence" and "a dispatch was recorded but
       nothing is known". Only explicit evidence can prove a non-charge; silence never can.
    """
    label = str(outcome or '')
    if label.upper() in _SUCCESS_OUTCOMES:
        return Classification(LIFECYCLE_SETTLED, EVIDENCE_USAGE_RECORDED, False,
                              'a provider usage outcome was recorded; the unit is spent')
    evidence_class, answered = evidence_from_telemetry(evidence)
    if evidence_class == EVIDENCE_USAGE_RECORDED:
        return Classification(LIFECYCLE_SETTLED, EVIDENCE_USAGE_RECORDED, False,
                              'a provider usage outcome was recorded; the unit is spent')
    if evidence_class in PRE_DISPATCH_CLASSES:
        return Classification(LIFECYCLE_FAILED_CONFIRMED, evidence_class, True,
                              f'{evidence_class}: the request was never delivered to the provider')
    if evidence_class in NO_CHARGE_CLASSES:
        return Classification(LIFECYCLE_FAILED_CONFIRMED, evidence_class, True,
                              f'{evidence_class}: the provider confirmed no billable execution '
                              'for this request identity, so no charge can have been incurred')
    if evidence_class == EVIDENCE_PROVIDER_RESPONSE or (answered
                                                       and evidence_class == EVIDENCE_NONE):
        return Classification(LIFECYCLE_UNCERTAIN, EVIDENCE_PROVIDER_RESPONSE, False,
                              STATUS_ONLY_REASON)
    if evidence_class == EVIDENCE_NONE:
        evidence_class = EVIDENCE_UNCONFIRMED_AFTER_SEND if dispatched else EVIDENCE_NONE
    return Classification(LIFECYCLE_UNCERTAIN, evidence_class, False,
                          'transmission is possible and no provider-confirmed outcome exists; '
                          'the allowance stays consumed until reconciled')


def summarise_counts(lease_counts: Mapping[str, int],
                     accounting_counts: Mapping[str, int]) -> dict[str, Any]:
    """Reported reservation buckets from two count maps, shared by both backends."""
    by_state = {str(key): int(value) for key, value in lease_counts.items()}
    by_accounting = {str(key): int(value) for key, value in accounting_counts.items()}
    return {
        'total': sum(by_state.values()),
        'active': by_state.get(STATE_ACTIVE, 0),
        'settled': by_state.get(STATE_SETTLED, 0),
        'expired': by_state.get(STATE_EXPIRED, 0),
        'settled_expired': by_state.get(STATE_SETTLED_EXPIRED, 0),
        'by_state': by_state,
        'by_accounting_state': by_accounting,
        'uncertain': by_accounting.get(LIFECYCLE_UNCERTAIN, 0),
        'failed_confirmed': by_accounting.get(LIFECYCLE_FAILED_CONFIRMED, 0),
        'accounting_active': by_accounting.get(LIFECYCLE_ACTIVE, 0),
        'accounting_settled': by_accounting.get(LIFECYCLE_SETTLED, 0),
    }


def bucket_counts(lease_states: list[str], accounting_states: list[str]) -> dict[str, Any]:
    """Reported reservation buckets for a status read, shared by both backends.

    The legacy keys (``active``/``settled``/``expired``/``settled_expired``) count the lease
    dimension exactly as before, so existing dashboards and probes keep their meaning. The
    accounting keys are the new authoritative spend dimension and are reported separately rather
    than folded into the legacy ones: ``expired`` therefore overlaps ``uncertain`` by design, and
    a reader must not mistake "slot released" for "money returned".
    """
    lease: dict[str, int] = {}
    for state in lease_states:
        lease[state] = lease.get(state, 0) + 1
    accounting: dict[str, int] = {}
    for state in accounting_states:
        accounting[state] = accounting.get(state, 0) + 1
    return summarise_counts(lease, accounting)
