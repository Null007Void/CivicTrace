"""An offline, in-process double of the *exact* Cloud Firestore surface the adapter calls.

Why this file exists
--------------------
``civictrace/spend_firestore_client.py`` is a real adapter: it builds a real
``google-cloud-firestore`` client and drives a real store transaction. Proving it offline means
injecting a client that behaves like the SDK, not simulating the adapter's return values. This
double implements only the surface the adapter touches, and it implements it with the SDK's own
value types (``DocumentSnapshot``, ``DatetimeWithNanoseconds``, ``types.WriteResult``) and the
SDK's own exception classes, so the adapter is exercised against the same shapes a live project
would hand it.

The exact surface (see :class:`civictrace.spend_firestore_client.FirestoreBoundedTransport`):

``client.project`` / ``client.database``
    Strings identifying the project the client is bound to.
``client.collection(name) -> collection``; ``collection.document(id) -> ref``
    Reference resolution.
``ref.get(timeout=...) -> DocumentSnapshot``
    One non-transactional snapshot read.
``client.transaction(max_attempts=1) -> transaction``
    A transaction object driven by the *real* ``firestore.transactional`` decorator, which calls
    ``transaction._clean_up()``, ``transaction._begin(retry_id=...)``, the adapter's callback and
    then ``transaction._commit()``. So this double must provide those four members, exactly as
    ``google.cloud.firestore_v1.transaction.Transaction`` does.
``transaction.get(ref, timeout=...)`` / ``transaction.set(ref, body)``
    The transactional snapshot read, then the staged write.
``transaction.write_results``
    The commit reply the adapter reads the new revision from.

Transaction model
-----------------
One document, serializable transactions, optimistic concurrency — the only three properties the
accounting backend relies on:

* ``_begin`` captures ``(revision, document)`` — the snapshot the callback will read;
* ``_commit`` applies the staged writes only if the store is still at that revision, and raises
  ``google.api_core.exceptions.Aborted`` otherwise (nothing applied). That is Firestore's
  behaviour for a conflicting transaction;
* ``inject_contention`` forces the next *n* commits to abort regardless of revision, and
  ``rendezvous`` (a ``threading.Barrier``) waits once inside ``_begin`` so several workers provably
  hold overlapping snapshots.

What is *not* modelled: real latency, quotas, IAM, credential resolution, retry classification,
network partitions, document-size limits, multi-region behaviour, or the server's own transaction
timeout. Passing these tests therefore says nothing about a live Cloud Firestore project.
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from google.api_core import exceptions
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore_v1 import types as fs_types
from google.cloud.firestore_v1.document import DocumentSnapshot
from google.protobuf.timestamp_pb2 import Timestamp

#: Deterministic epoch for server-assigned update times: 2026-10-28T00:00:00Z.
_BASE_SECONDS = 1793145600


def _timestamp(revision: int) -> Timestamp:
    """A store-assigned commit timestamp for ``revision`` (1 µs steps, never repeated)."""
    micros = int(revision)
    return Timestamp(seconds=_BASE_SECONDS + micros // 1_000_000,
                     nanos=(micros % 1_000_000) * 1000)


def _update_time(revision: int) -> DatetimeWithNanoseconds:
    """The SDK's own timestamp type, exactly as a snapshot read would carry it."""
    return DatetimeWithNanoseconds.from_timestamp_pb(_timestamp(revision))


class FakeDocumentReference:
    """``DocumentReference``-shaped handle: an identity plus a non-transactional ``get``."""

    def __init__(self, client: 'FakeFirestoreClient', path: str):
        self._client = client
        self._path = path

    @property
    def id(self) -> str:
        return self._path.rsplit('/', 1)[-1]

    @property
    def path(self) -> str:
        return self._path

    def get(self, field_paths: Any = None, transaction: Any = None,
            retry: Any = None, timeout: float | None = None, **_: Any) -> DocumentSnapshot:
        return self._client.store.read(self._path, timeout=timeout, reference=self)


class FakeCollectionReference:
    def __init__(self, client: 'FakeFirestoreClient', name: str):
        self._client = client
        self._name = name

    @property
    def id(self) -> str:
        return self._name

    def document(self, document_id: str) -> FakeDocumentReference:
        if not isinstance(document_id, str) or not document_id or '/' in document_id:
            raise ValueError(f'the fake store only addresses one path segment, got {document_id!r}')
        return FakeDocumentReference(self._client, f'{self._name}/{document_id}')


class FakeTransaction:
    """``Transaction``-shaped object driven by the real ``firestore.transactional`` decorator."""

    def __init__(self, client: 'FakeFirestoreClient', max_attempts: int = 5,
                 read_only: bool = False):
        self._client = client
        self._max_attempts = int(max_attempts)
        self._read_only = bool(read_only)
        self._id: int | None = None
        self._token: int | None = None
        self._snapshot: DocumentSnapshot | None = None
        self._staged: list[tuple[str, dict[str, Any]]] = []
        self.write_results: list[Any] = []
        self.commit_time: Timestamp | None = None
        self.begins = 0

    # -- the members the SDK's orchestrator calls ------------------------------------------

    @property
    def in_progress(self) -> bool:
        return self._id is not None

    def _clean_up(self) -> None:
        self._id = None
        self._token = None
        self._snapshot = None
        self._staged = []

    def _begin(self, retry_id: Any = None) -> None:
        if self.in_progress:
            raise ValueError('The transaction has already begun.')
        self.begins += 1
        self._id, self._token, self._snapshot = self._client.store.begin(
            self._client, rendezvous_key=id(self))
        self._staged = []

    def _rollback(self) -> None:
        """Roll back: nothing was applied, so this only releases the local snapshot."""
        if not self.in_progress:
            raise ValueError('The transaction has no transaction ID, so it cannot be rolled back.')
        self._client.store.rollbacks += 1
        self._clean_up()

    def _commit(self) -> list[Any]:
        if not self.in_progress:
            raise ValueError('The transaction has no transaction ID, so it cannot be committed.')
        token = self._token
        staged = list(self._staged)
        results = self._client.store.commit(token, staged)
        self.write_results = results
        self.commit_time = results[0].update_time if results else None
        self._clean_up()
        return results

    # -- the members the adapter calls -----------------------------------------------------

    def get(self, ref_or_query: Any, retry: Any = None, timeout: float | None = None,
            **_: Any) -> list[DocumentSnapshot]:
        if not self.in_progress or self._snapshot is None:
            raise ValueError('The transaction has no transaction ID, so it cannot read.')
        if self._staged:
            raise ValueError('Read-after-write is not supported in the fake transaction.')
        if timeout is not None and not float(timeout) > 0:
            raise ValueError('timeout must be positive')
        self._client.store.check_latency(timeout)
        self._client.store.read_failures.burn()
        self._client.store.transaction_reads += 1
        # ``DocumentSnapshot`` deep-copies its data at construction and ``to_dict`` returns a
        # copy, so the captured snapshot can be shared: no mutation can leak back into the store.
        return [self._snapshot]

    def set(self, reference: FakeDocumentReference, document_data: dict,
            merge: bool = False) -> None:
        if not self.in_progress:
            raise ValueError('The transaction has no transaction ID, so it cannot write.')
        if not isinstance(document_data, dict):
            raise TypeError('document_data must be a dict')
        self._staged.append((reference.path, copy.deepcopy(document_data)))

    def commit(self, retry: Any = None, timeout: float | None = None) -> list[Any]:
        return self._commit()


class _Failures:
    """A small injectable failure budget, consumed one call at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[BaseException] = []

    def arm(self, count: int, factory) -> None:
        with self._lock:
            self._pending.extend(factory() for _ in range(max(0, int(count))))

    def burn(self) -> None:
        with self._lock:
            if self._pending:
                raise self._pending.pop(0)


class FakeStore:
    """One document with serializable transactions and optimistic concurrency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.document: dict[str, Any] | None = None
        self.revision = 0                 # 0 = absent; every commit increments it
        self.latency_s = 0.0
        self.omit_update_time = False     # test-only: model an existing document with no token
        self.rendezvous: threading.Barrier | None = None
        self._rendezvous_pending = 0
        self._forced_contention = 0
        self._id_seq = 0
        self.reads = 0
        self.transaction_reads = 0
        self.commits = 0
        self.applied = 0
        self.aborted = 0
        self.rollbacks = 0
        self.read_failures = _Failures()
        self.commit_failures = _Failures()

    # -- test controls ---------------------------------------------------------------------

    def inject_contention(self, count: int) -> None:
        with self._lock:
            self._forced_contention = max(0, int(count))

    def inject_read_failure(self, count: int, factory=None) -> None:
        self.read_failures.arm(count, factory or (lambda: exceptions.ServiceUnavailable('read')))

    def inject_commit_failure(self, count: int, factory=None) -> None:
        self.commit_failures.arm(
            count, factory or (lambda: exceptions.ServiceUnavailable('commit')))

    def use_rendezvous(self, parties: int) -> threading.Barrier:
        self.rendezvous = threading.Barrier(parties)
        self._rendezvous_pending = int(parties)
        return self.rendezvous

    def snapshot(self, path: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self.document)

    def document_count(self) -> int:
        with self._lock:
            return 1 if self.document is not None else 0

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {'reads': self.reads, 'transaction_reads': self.transaction_reads,
                    'commits': self.commits, 'applied': self.applied, 'aborted': self.aborted,
                    'rollbacks': self.rollbacks, 'revision': self.revision}

    # -- store semantics -------------------------------------------------------------------

    def check_latency(self, timeout: float | None) -> None:
        if self.latency_s and timeout is not None and self.latency_s > float(timeout):
            raise exceptions.DeadlineExceeded(
                f'simulated latency {self.latency_s}s exceeded the {timeout}s deadline')

    def read(self, path: str, *, timeout: float | None = None,
             reference: FakeDocumentReference | None = None) -> DocumentSnapshot:
        if timeout is not None and not float(timeout) > 0:
            raise ValueError('timeout must be positive')
        self.check_latency(timeout)
        self.read_failures.burn()
        with self._lock:
            self.reads += 1
            revision, document = self.revision, copy.deepcopy(self.document)
        return self._snapshot(reference, revision, document)

    def begin(self, client: Any, *, rendezvous_key: Any = None
              ) -> tuple[int, int, DocumentSnapshot]:
        """Capture the transaction's snapshot. Returns (tx id, revision token, snapshot)."""
        with self._lock:
            self._id_seq = getattr(self, '_id_seq', 0) + 1
            token, document = self.revision, copy.deepcopy(self.document)
            transaction_id = self._id_seq
        reference = FakeDocumentReference(client, 'civictrace-spend/document')
        snapshot = self._snapshot(reference, token, document)
        if self._rendezvous_pending and self.rendezvous is not None:
            self._rendezvous_pending -= 1
            self.rendezvous.wait(timeout=30)
        return transaction_id, token, snapshot

    def commit(self, token: int | None, staged: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        self.commit_failures.burn()
        with self._lock:
            self.commits += 1
            if self._forced_contention > 0:
                self._forced_contention -= 1
                self.aborted += 1
                raise exceptions.Aborted('injected transaction abort')
            if token is None or token != self.revision:
                self.aborted += 1
                raise exceptions.Aborted(
                    f'the transaction snapshot is revision {token!r} but the document is at '
                    f'{self.revision!r}')
            if not staged:
                raise ValueError('the transaction staged no writes')
            self.revision += 1
            self.document = copy.deepcopy(staged[-1][1])
            self.applied += 1
            revision = self.revision
        return [fs_types.WriteResult(update_time=_timestamp(revision))]

    # -- helpers ---------------------------------------------------------------------------

    def _snapshot(self, reference: FakeDocumentReference | None, revision: int,
                  document: dict[str, Any] | None) -> DocumentSnapshot:
        reference = reference or FakeDocumentReference(FakeFirestoreClient(self), 'unknown/doc')
        update_time = None if self.omit_update_time else _update_time(max(revision, 1))
        if document is None:
            return DocumentSnapshot(reference, None, False, _update_time(max(revision, 1)),
                                    None, update_time)
        return DocumentSnapshot(reference, copy.deepcopy(document), True,
                                _update_time(revision), _update_time(1), update_time)


class FakeFirestoreClient:
    """``firestore.Client``-shaped object: project/database identity, collections, transactions."""

    def __init__(self, store: FakeStore, *, project: str = 'civictrace-test',
                 database: str = '(default)'):
        self.store = store
        self.project = project
        self.database = database
        self.transactions = 0
        #: Nothing in this double opens a socket; the counter exists so a test can assert it.
        self.network_calls = 0

    def collection(self, *collection_path: str) -> FakeCollectionReference:
        if len(collection_path) != 1:
            raise ValueError('the fake client addresses one collection')
        return FakeCollectionReference(self, collection_path[0])

    def transaction(self, max_attempts: int = 5, read_only: bool = False) -> FakeTransaction:
        self.transactions += 1
        return FakeTransaction(self, max_attempts=max_attempts, read_only=read_only)


def make_client(store: FakeStore | None = None, **kwargs: Any) -> FakeFirestoreClient:
    return FakeFirestoreClient(store or FakeStore(), **kwargs)
