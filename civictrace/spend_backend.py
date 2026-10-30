"""Backend-neutral spend gate: the accounting contract the live path depends on (CT-QC-038).

Before this module the live route instantiated :class:`civictrace.spend_gate.SpendGate`
directly, which hard-wires "shared accounting storage" to "a SQLite file inside a directory the
operator mounted". That is a defensible backend for a self-hosted deployment with a real shared
volume, and it is *not* a defensible Cloud Run backend: Cloud Storage FUSE/NFS mounts do not
provide the POSIX locking and mmap behaviour a SQLite write transaction needs, and Cloud Run
instances do not share a container filesystem at all. Requiring a directory, or requiring SQLite
transactions, therefore does not by itself establish safe accounting across instances.

This module is the seam that makes the requirement explicit:

* :class:`SpendBackend` is the whole contract the application is allowed to rely on —
  ``reserve`` (atomic check + reservation), ``settle`` (record the outcome of a spent
  reservation), ``sweep`` (release expired leases) and ``status`` (audit read).
* :class:`SqliteSpendBackend` adapts the existing, preserved SQLite shared-directory store.
  It is the default backend and the local/self-hosted option.
* ``firestore`` selects :class:`civictrace.spend_firestore.FirestoreSpendBackend`, a
  transaction-class backend whose admission is one snapshot read plus one conditional write in
  one transaction, with deterministic contention retry.

Selection is ``CIVICTRACE_SPEND_BACKEND=sqlite|firestore`` (default ``sqlite``). The selector is
resolved **once** per backend construction and there is deliberately no fallback: an unknown
selector is a typed refusal, a configured-but-unusable Firestore backend is a typed refusal, and
neither ever constructs the SQLite backend. ``sqlite`` on a container-local path can therefore
never quietly become the accounting store for a deployment that asked for a cloud backend. The
route maps every :class:`civictrace.usage_ledger.UsageLedgerError` to HTTP 503 *before* the
provider call, so a refusal costs nothing and spends nothing.

What is deliberately not here: the Google Cloud Firestore client. Adding it changes the release
SBOM, which requires an explicitly reviewed dependency (owner decision recorded in
``CT-COR3B-REPORT.md``). The single wiring point is
:func:`civictrace.spend_firestore.build_transport`; until a reviewed adapter is installed there,
selecting ``firestore`` fails closed. The offline tests exercise the *same* backend code against
a deterministic stub transactional store and they do **not** verify a live cloud backend.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from .spend_gate import SpendGate
from .spend_lifecycle import Reconciliation
from .usage_ledger import (
    LIVE_SPEND_BACKEND_INVALID,
    Admission,
    LiveBudget,
    UsageConfigError,
)

SPEND_BACKEND_ENV = 'CIVICTRACE_SPEND_BACKEND'
BACKEND_SQLITE = 'sqlite'
BACKEND_FIRESTORE = 'firestore'
DEFAULT_SPEND_BACKEND = BACKEND_SQLITE
KNOWN_BACKENDS = (BACKEND_SQLITE, BACKEND_FIRESTORE)


class SpendBackend(ABC):
    """The accounting contract. One deployment constant, one shared ceiling.

    Implementations must guarantee, for every ``reserve`` call, that the read of the committed
    counters and the write of the reservation are one atomic step with respect to every other
    instance sharing the deployment constant. ``settle`` must be exactly-once per reservation.
    ``sweep`` must never move money: a lease is released, a committed unit is not refunded.
    """

    name: str = ''
    deployment_id: str = ''
    instance_id: str = ''

    @property
    @abstractmethod
    def store_label(self) -> str:
        """Human-readable identity of the concrete store (filename or document path)."""

    @abstractmethod
    def reserve(self, budget: LiveBudget, *, now_epoch: float | None = None,
                request_intent: dict[str, Any] | None = None,
                request_digest: str | None = None) -> Admission:
        """Atomically consume one admission on the shared store, or refuse with a typed code.

        A caller-supplied request identity that already owns a reservation is a
        ``LIVE_REQUEST_IDENTITY_REUSED`` refusal with ``duplicate_of`` set: no second unit, no
        second transmission.
        """

    @abstractmethod
    def settle(self, admission: Admission, *, outcome: str,
               provider_requests: int | None = None,
               prompt_tokens: int | None = None,
               completion_tokens: int | None = None,
               evidence: dict[str, Any] | None = None) -> bool:
        """Record the outcome of a spent reservation exactly once. Never moves money.

        True only for the ``active -> settled`` transition; a late reconciliation after expiry
        returns False and a repeat is a no-op.
        """

    @abstractmethod
    def reconcile(self, admission: Admission | str | None = None, *, outcome: str,
                  provider_requests: int | None = None,
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None,
                  evidence: dict[str, Any] | None = None,
                  reservation_id: str | None = None,
                  request_digest: str | None = None) -> Reconciliation:
        """Settle by durable identity, from any instance or revision. Never raises."""

    @abstractmethod
    def mark_transmitted(self, admission: Admission | str | None = None, *,
                         round_num: int | None = None,
                         now_epoch: float | None = None,
                         reservation_id: str | None = None) -> bool:
        """Durably mark a dispatch before it happens: the lifecycle becomes ``uncertain``."""

    @abstractmethod
    def refund_failed_confirmed(self, reservation_id: str, *,
                                reason: str = 'provider_confirmed_no_charge',
                                now_epoch: float | None = None) -> dict[str, Any]:
        """Restore allowance for ``failed_confirmed`` only. Explicit, once-only, typed refusal."""

    @abstractmethod
    def history(self, reservation_id: str | None = None, *,
                limit: int = 200) -> list[dict[str, Any]]:
        """Append-only accounting history (oldest first)."""

    @abstractmethod
    def sweep(self, *, now_epoch: float | None = None, released_by: str | None = None) -> int:
        """Release expired leases; returns the number of leases released by this call."""

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Audit read: counters, reservation states and store identity."""

    def startup_sweep(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        """Recover leases abandoned by a dead predecessor; safe to call on every start."""
        released = self.sweep(now_epoch=now_epoch, released_by=self.instance_id)
        return {
            'released': released,
            'instance_id': self.instance_id,
            'deployment_id': self.deployment_id,
            'backend': self.name,
            'store': self.store_label,
        }


class SqliteSpendBackend(SpendBackend):
    """The preserved SQLite shared-directory store behind the backend contract.

    This is a *local/self-hosted* backend: it is correct when the deployment can guarantee one
    real shared filesystem with working advisory locks (a bind-mounted volume, a single host, a
    shared VM). It is not a Cloud Run backend; that is what the Firestore-class backend is for
    and why the selector exists at all.
    """

    name = BACKEND_SQLITE

    def __init__(self, *, gate: SpendGate | None = None, **kwargs: Any):
        self._gate = gate if gate is not None else SpendGate(**kwargs)
        self.deployment_id = self._gate.deployment_id
        self.instance_id = self._gate.instance_id

    @property
    def gate(self) -> SpendGate:
        return self._gate

    @property
    def store_label(self) -> str:
        return self._gate.path.name

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

    def reconcile(self, admission: Admission | str | None = None, *, outcome: str,
                  provider_requests: int | None = None,
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None,
                  evidence: dict[str, Any] | None = None,
                  reservation_id: str | None = None,
                  request_digest: str | None = None) -> Reconciliation:
        return self._gate.reconcile(admission, outcome=outcome,
                                    provider_requests=provider_requests,
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens, evidence=evidence,
                                    reservation_id=reservation_id,
                                    request_digest=request_digest)

    def mark_transmitted(self, admission: Admission | str | None = None, *,
                         round_num: int | None = None,
                         now_epoch: float | None = None,
                         reservation_id: str | None = None) -> bool:
        return self._gate.mark_transmitted(admission, round_num=round_num,
                                           now_epoch=now_epoch, reservation_id=reservation_id)

    def refund_failed_confirmed(self, reservation_id: str, *,
                                reason: str = 'provider_confirmed_no_charge',
                                now_epoch: float | None = None) -> dict[str, Any]:
        return self._gate.refund_failed_confirmed(reservation_id, reason=reason,
                                                  now_epoch=now_epoch)

    def history(self, reservation_id: str | None = None, *,
                limit: int = 200) -> list[dict[str, Any]]:
        return self._gate.history(reservation_id, limit=limit)

    def sweep(self, *, now_epoch: float | None = None, released_by: str | None = None) -> int:
        return self._gate.sweep(now_epoch=now_epoch, released_by=released_by)

    def status(self) -> dict[str, Any]:
        payload = self._gate.read_state()
        payload['backend'] = self.name
        payload['backend_kind'] = 'sqlite-file-transaction'
        return payload


def resolve_backend_name(selector: str | None = None) -> str:
    """Resolve ``CIVICTRACE_SPEND_BACKEND``. Unknown values are refused, never defaulted."""
    raw = selector if selector is not None else os.getenv(SPEND_BACKEND_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_SPEND_BACKEND
    value = str(raw).strip().lower()
    if value not in KNOWN_BACKENDS:
        raise UsageConfigError(
            LIVE_SPEND_BACKEND_INVALID,
            f'{SPEND_BACKEND_ENV} must be one of {KNOWN_BACKENDS}, got {raw!r}; live inference '
            'refused (an unknown backend is never replaced by a default)')
    return value


def open_spend_backend(*, selector: str | None = None, transport: Any = None,
                       **kwargs: Any) -> SpendBackend:
    """Build the configured accounting backend. Never falls back to another backend.

    ``transport`` is only meaningful for the Firestore-class backend and exists so tests and
    probes can inject the bounded transport seam; production supplies it through
    :func:`civictrace.spend_firestore.build_transport`.
    """
    name = resolve_backend_name(selector)
    if name == BACKEND_SQLITE:
        if transport is not None:
            raise UsageConfigError(
                LIVE_SPEND_BACKEND_INVALID,
                'a transport was supplied for the sqlite backend, so the selector and the wiring '
                'disagree; live inference refused rather than guessing which one was meant')
        return SqliteSpendBackend(**kwargs)

    # Imported lazily: the Firestore module depends on this module for the interface, and the
    # SQLite-only deployment must not pay for it at import time.
    from .spend_firestore import FirestoreSpendBackend

    return FirestoreSpendBackend.from_env(transport=transport, **kwargs)
