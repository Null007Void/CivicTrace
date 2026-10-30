"""Deterministic offline doubles for the Firestore-class accounting backend (CT-QC-038).

**Not a production backend.** Nothing here can be selected by ``CIVICTRACE_SPEND_BACKEND`` and
nothing here touches a network, a credential or a clock: a stub store is only reachable by being
passed explicitly to :class:`civictrace.spend_firestore.FirestoreSpendBackend` from a test or a
probe. A deployment that selects ``firestore`` without wiring a real transport is refused (see
:func:`civictrace.spend_firestore.build_transport`); it is never handed one of these.

What is replicated, and why it is the part that matters
------------------------------------------------------
The backend's safety rests on three store guarantees, so the double implements exactly those and
nothing more:

* **One snapshot per read.** :meth:`MemoryDocumentStore.read` returns an immutable copy plus the
  revision that the copy corresponds to. A caller cannot observe a later write through an earlier
  snapshot.
* **Atomic, revision-checked apply.** :meth:`MemoryDocumentStore.commit` applies the whole body
  only if the supplied ``expect_revision`` still matches, under one lock — create-if-absent when
  ``expect_revision`` is ``None``. A stale snapshot is answered ``contended`` and *nothing* is
  applied, which is how Firestore's ``ABORTED`` behaves for a conflicting transaction.
* **Contention is injectable.** :meth:`MemoryDocumentStore.inject_contention` makes the next *n*
  commits abort regardless of the revision, and :class:`StubBoundedTransport` accepts a
  ``rendezvous`` barrier so several workers provably hold overlapping snapshots. Both are
  deterministic: no sleeps, no randomness, no timers.

What is *not* replicated: Firestore's real latency, quotas, IAM, retry classification, document
size enforcement or multi-region behaviour. Passing these tests therefore says nothing about a
live Cloud Firestore project, and no claim in ``CT-COR3B-REPORT.md`` treats it as if it did.
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from .spend_firestore import BoundedTransport, StoreUnavailable

APPLIED = 'applied'
CONTENDED = 'contended'


class MemoryDocumentStore:
    """A single-node, in-memory document store with optimistic concurrency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._documents: dict[str, dict[str, Any]] = {}
        self._revisions: dict[str, int] = {}
        self._forced_contention = 0
        self.reads = 0
        self.commits = 0
        self.applied = 0
        self.contended = 0
        self.created = 0

    # -- test controls ---------------------------------------------------------------------

    def inject_contention(self, count: int) -> None:
        """Force the next ``count`` commits to abort without applying anything."""
        with self._lock:
            self._forced_contention = max(0, int(count))

    def snapshot(self, document_id: str) -> dict[str, Any] | None:
        """Test-only inspection: a copy of the stored body (``None`` if absent)."""
        with self._lock:
            return copy.deepcopy(self._documents.get(document_id))

    def revision(self, document_id: str) -> int | None:
        with self._lock:
            return self._revisions.get(document_id)

    def document_count(self) -> int:
        with self._lock:
            return len(self._documents)

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {'reads': self.reads, 'commits': self.commits, 'applied': self.applied,
                    'contended': self.contended, 'created': self.created}

    # -- store semantics -------------------------------------------------------------------

    def read(self, document_id: str) -> dict[str, Any]:
        with self._lock:
            self.reads += 1
            return {'revision': self._revisions.get(document_id),
                    'document': copy.deepcopy(self._documents.get(document_id))}

    def commit(self, document_id: str, document: Any,
               expect_revision: int | None) -> dict[str, Any]:
        if not isinstance(document, dict):
            return {'status': 'invalid', 'reason': 'document body is not a map'}
        with self._lock:
            self.commits += 1
            if self._forced_contention > 0:
                self._forced_contention -= 1
                self.contended += 1
                return {'status': CONTENDED, 'reason': 'injected_abort'}
            current = self._revisions.get(document_id)
            if expect_revision is None:
                if current is not None:
                    self.contended += 1
                    return {'status': CONTENDED, 'reason': 'document_already_exists'}
                new_revision = 1
            else:
                if current is None or current != expect_revision:
                    self.contended += 1
                    return {'status': CONTENDED, 'reason': 'stale_revision'}
                new_revision = current + 1
            self._revisions[document_id] = new_revision
            self._documents[document_id] = copy.deepcopy(document)
            self.applied += 1
            if expect_revision is None:
                self.created += 1
            return {'status': APPLIED, 'revision': new_revision}


class StubBoundedTransport(BoundedTransport):
    """A :class:`BoundedTransport` over a :class:`MemoryDocumentStore`.

    * ``failures`` — the next *n* calls raise :class:`StoreUnavailable` (unreachable backend).
    * ``latency_s`` — simulated per-call latency; a call whose latency exceeds the caller's
      ``deadline_s`` raises :class:`StoreUnavailable`, which is how a deadline breach must behave.
    * ``rendezvous`` — a ``threading.Barrier`` waited on before the *first* ``read`` returns, so
      concurrent workers provably snapshot the same revision (deterministic contention).
    """

    def __init__(self, store: MemoryDocumentStore, *, label: str = 'stub',
                 latency_s: float = 0.0, failures: int = 0,
                 rendezvous: threading.Barrier | None = None):
        self.store = store
        self.label = label
        self.latency_s = float(latency_s)
        self.failures = int(failures)
        self.rendezvous = rendezvous
        self._rendezvous_pending = 1 if rendezvous is not None else 0
        self.calls = 0
        self.operations: list[str] = []
        self.deadlines: list[float] = []

    def call(self, operation: str, payload: dict[str, Any], *, deadline_s: float) -> dict[str, Any]:
        self.calls += 1
        self.operations.append(operation)
        self.deadlines.append(deadline_s)
        if not (deadline_s > 0):
            raise StoreUnavailable(f'{self.label}: deadline must be positive')
        if self.failures > 0:
            self.failures -= 1
            raise StoreUnavailable(f'{self.label}: injected transport failure during {operation}')
        if self.latency_s > deadline_s:
            raise StoreUnavailable(
                f'{self.label}: simulated latency {self.latency_s}s exceeded the '
                f'{deadline_s}s deadline during {operation}')
        document_id = payload.get('document_id')
        if not isinstance(document_id, str) or not document_id:
            raise StoreUnavailable(f'{self.label}: {operation} without a document id')
        if operation == 'read':
            reply = self.store.read(document_id)
            if self._rendezvous_pending:
                self._rendezvous_pending -= 1
                if self.rendezvous is not None:
                    self.rendezvous.wait(timeout=30)
            return reply
        if operation == 'commit':
            return self.store.commit(document_id, payload.get('document'),
                                     payload.get('expect_revision'))
        raise StoreUnavailable(f'{self.label}: unknown operation {operation!r}')
