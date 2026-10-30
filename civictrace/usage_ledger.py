"""Admission *policy* for live provider inference: ceilings, budget parsing, typed refusals.

This module keeps the policy surface that ``civictrace/app.py`` and the regression suite use —
``LiveBudget``, ``Admission`` and the governance error taxonomy with its stable machine-readable
codes — and it is the historical import location for the admission object.

The storage backend that used to live here (a JSON document per deployment guarded by a local
advisory file lock) has been replaced. It was durable on one filesystem but container-local:
two instances of a horizontally scaled deployment each owned a separate file, so each got its
own allowance and no ceiling was global. The replacement is
:class:`civictrace.spend_gate.SpendGate`, a transactional SQLite store at a *required shared*
path with atomic ``BEGIN IMMEDIATE`` reservations, per-reservation instance identity and TTL,
and exactly-once recovery of leases abandoned by a killed instance.

CT-QC-038 made the storage swappable without changing that policy: ``SpendGate`` is the
``sqlite`` backend behind :class:`civictrace.spend_backend.SpendBackend`, selected with
``CIVICTRACE_SPEND_BACKEND=sqlite|firestore`` (default ``sqlite``). The ``firestore`` selector
builds a transaction-class cloud backend; an unusable configured backend is a typed refusal.
Neither selector has an in-memory or container-local fallback for live inference.

``UsageLedger`` below is a thin, deprecated facade over ``SpendGate`` kept so existing callers
and tests keep importing from this module. New code should call
:func:`civictrace.spend_backend.open_spend_backend` so the configured backend is honoured.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from typing import Any

SCHEMA = 'civictrace-usage-ledger/1'
DEFAULT_DEPLOYMENT_ID = 'civictrace-rc1'

# Owner-configured ceiling semantics are preserved: the request cap default and clamp are the
# values the shipped app already used (CIVICTRACE_MAX_LIVE_ATTEMPTS, 50, capped at 500).
DEFAULT_REQUEST_CAP = 50
MAX_REQUEST_CAP = 500
DEFAULT_DOLLAR_CAP = '5.00'
MAX_DOLLAR_CAP = '10000'
DEFAULT_EST_USD_PER_REQUEST = '0.005'
MAX_EST_USD_PER_REQUEST = '1.00'
DEFAULT_LOCK_TIMEOUT_S = 2.0

MONEY = Decimal('0.000001')
MAX_RECEIPTS = 100

# Stable governance codes. The three store codes keep the names the shipped app already returned
# so callers and probes that assert them are unaffected; NOT_CONFIGURED is new and distinct
# because an unset/unusable shared path is a configuration refusal, not a store fault.
LIVE_USAGE_LEDGER_UNAVAILABLE = 'LIVE_USAGE_LEDGER_UNAVAILABLE'
LIVE_USAGE_LEDGER_LOCKED = 'LIVE_USAGE_LEDGER_LOCKED'
LIVE_USAGE_LEDGER_CORRUPT = 'LIVE_USAGE_LEDGER_CORRUPT'
LIVE_SPEND_STORE_NOT_CONFIGURED = 'LIVE_SPEND_STORE_NOT_CONFIGURED'
# CT-QC-038 backend-selection codes. A cloud accounting backend that is misconfigured, unreachable
# or permanently contended is its own class of refusal: it must be visible to an operator as
# "the configured backend is not usable", and it must never be answered by a different backend.
LIVE_SPEND_BACKEND_INVALID = 'LIVE_SPEND_BACKEND_INVALID'
LIVE_SPEND_BACKEND_NOT_CONFIGURED = 'LIVE_SPEND_BACKEND_NOT_CONFIGURED'
LIVE_SPEND_BACKEND_UNAVAILABLE = 'LIVE_SPEND_BACKEND_UNAVAILABLE'
LIVE_SPEND_BACKEND_CONTENDED = 'LIVE_SPEND_BACKEND_CONTENDED'
INVALID_LIVE_BUDGET_CONFIGURATION = 'INVALID_LIVE_BUDGET_CONFIGURATION'
INVALID_LIVE_ATTEMPT_CONFIGURATION = 'INVALID_LIVE_ATTEMPT_CONFIGURATION'
LIVE_REQUEST_BUDGET_EXHAUSTED = 'LIVE_REQUEST_BUDGET_EXHAUSTED'
LIVE_SPEND_BUDGET_EXHAUSTED = 'LIVE_SPEND_BUDGET_EXHAUSTED'
# CT-QC-039 reservation-identity and reconciliation codes. A duplicate identity is refused
# *before* a second unit is reserved, and a refund is refused unless the reservation's own
# evidence permits one.
LIVE_REQUEST_IDENTITY_REUSED = 'LIVE_REQUEST_IDENTITY_REUSED'
LIVE_REQUEST_IDENTITY_INVALID = 'LIVE_REQUEST_IDENTITY_INVALID'
LIVE_RESERVATION_NOT_REFUNDABLE = 'LIVE_RESERVATION_NOT_REFUNDABLE'
LIVE_RESERVATION_UNKNOWN = 'LIVE_RESERVATION_UNKNOWN'


class UsageLedgerError(Exception):
    """Typed governance failure; ``code`` is the exact code surfaced to the caller."""

    def __init__(self, code: str, message: str = ''):
        self.code = code
        self.message = message or code
        super().__init__(self.code)


class UsageConfigError(UsageLedgerError):
    pass


class UsageLedgerUnavailable(UsageLedgerError):
    pass


class UsageLedgerLocked(UsageLedgerError):
    pass


class UsageLedgerCorrupt(UsageLedgerError):
    pass


def _decimal_env(name: str, default: str, maximum: str) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = Decimal(default)
    else:
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise UsageConfigError(INVALID_LIVE_BUDGET_CONFIGURATION,
                                   f'{name} is not a decimal value') from None
    if not value.is_finite() or value < 0:
        raise UsageConfigError(INVALID_LIVE_BUDGET_CONFIGURATION, f'{name} must be a finite non-negative value')
    if value > Decimal(maximum):
        raise UsageConfigError(INVALID_LIVE_BUDGET_CONFIGURATION, f'{name} exceeds the supported maximum')
    return value


@dataclass(frozen=True)
class LiveBudget:
    """Ceilings for one deployment, as configured by the owner for this admission."""

    request_cap: int
    dollar_cap: Decimal
    per_claim_estimate_usd: Decimal

    @classmethod
    def from_env(cls, *, requests_per_claim: int) -> 'LiveBudget':
        # Request cap parsing keeps the shipped error code for the shipped variable.
        raw = os.getenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', str(DEFAULT_REQUEST_CAP))
        try:
            request_cap = max(0, min(int(raw), MAX_REQUEST_CAP))
        except (TypeError, ValueError):
            raise UsageConfigError(INVALID_LIVE_ATTEMPT_CONFIGURATION,
                                   'CIVICTRACE_MAX_LIVE_ATTEMPTS is not an integer') from None
        per_request = _decimal_env('CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST',
                                   DEFAULT_EST_USD_PER_REQUEST, MAX_EST_USD_PER_REQUEST)
        if per_request <= 0:
            # A zero estimate would make the dollar ceiling silently non-binding; refuse to
            # pretend the budget is enforced.
            raise UsageConfigError(INVALID_LIVE_BUDGET_CONFIGURATION,
                                   'CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST must be greater than zero')
        # A claim can issue at most REQUESTS_PER_CLAIM_CEILING provider requests, so the
        # reservation is the worst-case estimate for one claim, not one HTTP call.
        estimate = (per_request * max(1, requests_per_claim)).quantize(MONEY)
        return cls(request_cap=request_cap,
                   dollar_cap=_decimal_env('CIVICTRACE_MAX_LIVE_USD', DEFAULT_DOLLAR_CAP, MAX_DOLLAR_CAP),
                   per_claim_estimate_usd=estimate)


@dataclass(frozen=True)
class Admission:
    """Result of one durable admission attempt. A refusal names the exhausted ceiling.

    ``reservation_id``, ``instance_id`` and ``expires_epoch`` identify the reservation row a
    successful admission created, so a settlement (or a recovery sweep) can address it exactly.

    ``request_digest`` is the durable identity of the logical request (a caller-supplied intent
    digest, or one derived from the reservation id when no intent was supplied); ``seq`` is its
    durable order in the deployment's accounting; ``accounting_state`` is the spend lifecycle
    state observed at the moment of the decision. A refusal with ``duplicate_of`` set did **not**
    create a second reservation: the identity already owned ``duplicate_of``, and the caller must
    reconcile that one instead of transmitting again.
    """

    admitted: bool
    code: str
    seq: int
    requests_committed: int
    estimated_spend_usd: Decimal
    reserved_usd: Decimal
    reservation_id: str = ''
    instance_id: str = ''
    expires_epoch: float | None = None
    request_digest: str = ''
    accounting_state: str = ''
    duplicate_of: str = ''


class UsageLedger:
    """Deprecated facade over :class:`civictrace.spend_gate.SpendGate`.

    Kept so the historical import surface (``from civictrace.usage_ledger import UsageLedger``)
    keeps working. Every call delegates to the shared-durable SQLite store; nothing here holds
    state, and there is no local fallback behind it.
    """

    def __init__(self, *, directory: Path | str | None = None,
                 deployment_id: str | None = None,
                 lock_timeout_s: float | None = None,
                 reservation_ttl_s: float | None = None,
                 instance_id: str | None = None,
                 expect_root: Path | str | None = None):
        from .spend_gate import SpendGate
        self._gate = SpendGate(directory=directory, deployment_id=deployment_id,
                               lock_timeout_s=lock_timeout_s,
                               reservation_ttl_s=reservation_ttl_s,
                               instance_id=instance_id, expect_root=expect_root)

    @property
    def directory(self) -> Path:
        return self._gate.store_dir

    @property
    def path(self) -> Path:
        return self._gate.path

    @property
    def deployment_id(self) -> str:
        return self._gate.deployment_id

    @property
    def instance_id(self) -> str:
        return self._gate.instance_id

    @property
    def reservation_ttl_s(self) -> float:
        return self._gate.reservation_ttl_s

    def reserve(self, budget: LiveBudget, *, now_epoch: float | None = None,
                request_intent: dict[str, Any] | None = None,
                request_digest: str | None = None) -> Admission:
        return self._gate.reserve(budget, now_epoch=now_epoch, request_intent=request_intent,
                                  request_digest=request_digest)

    def settle(self, admission: Admission, *, outcome: str,
               provider_requests: int | None = None,
               prompt_tokens: int | None = None,
               completion_tokens: int | None = None,
               evidence: dict[str, Any] | None = None) -> bool:
        return self._gate.settle(admission, outcome=outcome, provider_requests=provider_requests,
                                 prompt_tokens=prompt_tokens,
                                 completion_tokens=completion_tokens, evidence=evidence)

    def reconcile(self, admission: Admission | str | None = None, *, outcome: str, **kwargs: Any):
        return self._gate.reconcile(admission, outcome=outcome, **kwargs)

    def mark_transmitted(self, admission: Admission | str | None = None, **kwargs: Any) -> bool:
        return self._gate.mark_transmitted(admission, **kwargs)

    def refund_failed_confirmed(self, reservation_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._gate.refund_failed_confirmed(reservation_id, **kwargs)

    def history(self, reservation_id: str | None = None, *, limit: int = 200):
        return self._gate.history(reservation_id, limit=limit)

    def sweep(self, *, now_epoch: float | None = None, released_by: str | None = None) -> int:
        return self._gate.sweep(now_epoch=now_epoch, released_by=released_by)

    def startup_sweep(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        return self._gate.startup_sweep(now_epoch=now_epoch)

    def integrity_check(self) -> str:
        return self._gate.integrity_check()

    def read_state(self) -> dict[str, Any]:
        return self._gate.read_state()

    def exclusive_write_lock(self):
        return self._gate.exclusive_write_lock()
