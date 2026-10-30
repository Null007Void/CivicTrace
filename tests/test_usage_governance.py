"""QC-02 (raised bar) / QC-09 regression: shared durable atomic spend reservations.

Hermetic: local temporary stores, real child processes and in-process ASGI only. The live agent
is replaced by a stub, so no provider endpoint is contacted, no credential is read and no paid
inference occurs.

The store under test is ``civictrace.spend_gate.SpendGate`` — a SQLite database at a
**required shared** directory, with ``BEGIN IMMEDIATE`` reservations, per-reservation instance
identity and a TTL, and exactly-once recovery of leases abandoned by a killed instance. There
is no local-only backend and no in-memory fallback; every mechanic that changed relative to the
CT-COR-002 file ledger is named in the test docstring or the assertion message.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import civictrace.app as app_module
from civictrace.app import MAX_TRACKED_PEERS, PeerWindows, app
from civictrace.spend_gate import STORE_SCHEMA, SpendGate
from civictrace.usage_ledger import (
    Admission,
    LiveBudget,
    UsageLedger,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerLocked,
    UsageLedgerUnavailable,
)

ROOT = Path(__file__).resolve().parent.parent

# Independent two-process worker: it reads the same environment the app reads, so the test
# exercises the real deployment path rather than a test-only seam.
RESERVE_WORKER = """
import json, os, sys, time
from civictrace.nebius import REQUESTS_PER_CLAIM_CEILING
from civictrace.spend_gate import SpendGate
from civictrace.usage_ledger import LiveBudget, UsageLedgerError

attempts = int(sys.argv[1])
start_at = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
while time.time() < start_at:
    time.sleep(0.002)
budget = LiveBudget.from_env(requests_per_claim=REQUESTS_PER_CLAIM_CEILING)
gate = SpendGate()
admitted = refused = errors = 0
codes = {}
for _ in range(attempts):
    try:
        decision = gate.reserve(budget)
    except UsageLedgerError as exc:
        errors += 1
        codes[exc.code] = codes.get(exc.code, 0) + 1
        continue
    if decision.admitted:
        admitted += 1
    else:
        refused += 1
        codes[decision.code] = codes.get(decision.code, 0) + 1
print(json.dumps({"pid": os.getpid(), "admitted": admitted, "refused": refused,
                  "errors": errors, "codes": codes, "instance_id": gate.instance_id,
                  "store": gate.path.name}))
"""

# Holds a committed reservation and then hangs, so the parent can kill it mid-reservation
# (after the COMMIT, before any settlement).
HOLD_WORKER = """
import json, os, sys, time
from decimal import Decimal
from civictrace.spend_gate import SpendGate
from civictrace.usage_ledger import LiveBudget

budget = LiveBudget(request_cap=int(sys.argv[1]), dollar_cap=Decimal("100.00"),
                    per_claim_estimate_usd=Decimal("0.01"))
gate = SpendGate()
decision = gate.reserve(budget)
print(json.dumps({"admitted": decision.admitted, "reservation_id": decision.reservation_id,
                  "seq": decision.seq, "instance_id": gate.instance_id, "pid": os.getpid(),
                  "state": gate.read_state()["requests_committed"]}), flush=True)
time.sleep(180)
"""

# Dies hard *inside* the reservation transaction, after the row and counters are written but
# before COMMIT. os._exit skips every cleanup handler, which is what a killed container does.
CRASH_WORKER = """
import os, sys, time
from decimal import Decimal
from civictrace.spend_gate import SpendGate
from civictrace.usage_ledger import LiveBudget

budget = LiveBudget(request_cap=5, dollar_cap=Decimal("100.00"),
                    per_claim_estimate_usd=Decimal("0.01"))
gate = SpendGate()
with gate._transaction() as conn:
    row = gate._counters(conn)
    seq = int(row["requests_committed"]) + 1
    gate._commit_reservation(conn, budget, seq, time.time(), Decimal(row["estimated_spend_usd"]))
    print("prepared", flush=True)
    os._exit(9)
"""

SWEEP_WORKER = """
import json, sys, time
from civictrace.spend_gate import SpendGate

start_at = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
while time.time() < start_at:
    time.sleep(0.002)
gate = SpendGate()
print(json.dumps({"released": gate.sweep(), "instance_id": gate.instance_id}))
"""


class _StubRun:
    provider = {'kind': 'STUB_NO_PROVIDER_REQUEST', 'provider_requests': 1,
                'usage_as_returned': {'prompt_tokens': 10, 'completion_tokens': 5}}

    def as_json(self):
        return {'stub': True}


class _StubAgent:
    def __init__(self, calls, *, fail=False, error=None):
        self.calls = calls
        self.fail = fail
        self.error = error

    async def run(self, claim_id):
        self.calls.append(claim_id)
        if self.fail:
            from civictrace.nebius import ProviderUnavailable
            raise self.error or ProviderUnavailable('PROVIDER_TIMEOUT_OR_NETWORK_ERROR')
        return _StubRun()


@pytest.fixture(autouse=True)
def _isolated_governance(tmp_path, monkeypatch):
    """A private *existing* shared-store directory; the store never creates one itself."""
    store = tmp_path / 'shared-store'
    store.mkdir()
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(store))
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '50')
    for name in ('CIVICTRACE_DEPLOYMENT_ID', 'CIVICTRACE_MAX_LIVE_USD',
                 'CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', 'CIVICTRACE_USAGE_LOCK_TIMEOUT_S',
                 'CIVICTRACE_SPEND_RESERVATION_TTL_S', 'CIVICTRACE_INSTANCE_ID',
                 'CIVICTRACE_SPEND_STORE_EXPECT_ROOT', 'CIVICTRACE_USAGE_LEDGER_DIR',
                 'CIVICTRACE_LIVE_ENABLED', 'NEBIUS_API_KEY', 'CIVICTRACE_LIVE_CHECK_ACK'):
        monkeypatch.delenv(name, raising=False)
    app_module._peer_windows.clear()
    yield store


@pytest.fixture
def store_dir(_isolated_governance):
    return _isolated_governance


def _budget(request_cap=50, dollar_cap='100.00', estimate='0.01'):
    return LiveBudget(request_cap=request_cap, dollar_cap=Decimal(dollar_cap),
                      per_claim_estimate_usd=Decimal(estimate))


def _popen(code: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    return subprocess.Popen([sys.executable, '-c', code, *args], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close the child's pipes; idempotent, so it is safe after ``communicate``."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):  # pragma: no cover - already closed by a reader
            pass


def _retire(proc: subprocess.Popen, timeout: float = 60.0) -> None:
    """Kill the child if it is still running, reap it, then close its pipes.

    ``kill``/``wait`` reap the process but leave ``stdout``/``stderr`` open until the pipe
    objects are garbage collected, which raises ``ResourceWarning: unclosed file`` under a
    warning-strict (``-W error``) run. The handles are closed in a ``finally``, so a failed
    readiness read, a ``communicate`` timeout or any other exception between opening and
    reaping cannot leak them. Nothing the tests assert changes.
    """
    try:
        if proc.poll() is None:
            proc.kill()
    finally:
        try:
            proc.wait(timeout=timeout)
        finally:
            _close_pipes(proc)


class _WorkerCleanupError(RuntimeError):
    """One or more children could not be retired; every child was still retired first."""

    def __init__(self, failures: list[tuple[int, BaseException]]) -> None:
        self.failures = tuple(failures)
        detail = '; '.join(f'worker[{index}] {type(exc).__name__}: {exc}'
                           for index, exc in self.failures)
        super().__init__(f'{len(self.failures)} worker cleanup failure(s): {detail}')


def _retire_after_failure(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Best-effort reap after a failed retirement, so that child's pipes cannot leak.

    This is the second chance for the one child whose ``_retire`` raised: it may still be
    running (for example when ``wait`` timed out), so it is killed, reaped on a short
    budget and closed. Failures here are deliberately swallowed -- the original retirement
    error is what the caller has to see -- but the pipes are closed either way.
    """
    try:
        if proc.poll() is None:
            proc.kill()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        _close_pipes(proc)


def _retire_all(procs: list[subprocess.Popen]) -> None:
    """Retire every child, whatever happens to any single retirement.

    The previous shape was a bare ``_retire(proc)`` walk inside ``finally``: one failing
    retirement aborted the walk, so every later child stayed unreaped with both pipes open
    (the CT-QC-049 finding). Each child is now isolated; a child whose retirement raises is
    still reaped and closed on the best-effort path; and every failure is raised together,
    once, after the last child has been retired, so cleanup is complete *and* the error is
    still visible with its type, message and child index. Normal, error and timeout paths
    all close the pipes. Only ``Exception`` is collected, so an interrupt still surfaces as
    an interrupt.
    """
    failures: list[tuple[int, BaseException]] = []
    for index, proc in enumerate(procs):
        try:
            _retire(proc)
        except Exception as exc:  # noqa: BLE001 - the remaining children must still be retired
            failures.append((index, exc))
            _retire_after_failure(proc)
    if failures:
        raise _WorkerCleanupError(failures)


def _run_workers(attempts: int, workers: int = 2, *, cap: int,
                 dollar_cap='100.00', barrier_s: float = 0.5) -> list[dict]:
    env = dict(os.environ)
    env['CIVICTRACE_MAX_LIVE_ATTEMPTS'] = str(cap)
    env['CIVICTRACE_MAX_LIVE_USD'] = dollar_cap
    env['CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST'] = '0.01'
    env['PYTHONPATH'] = str(ROOT)
    start_at = time.time() + barrier_s
    procs = [subprocess.Popen([sys.executable, '-c', RESERVE_WORKER, str(attempts), repr(start_at)],
                              cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
             for _ in range(workers)]
    try:
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            assert proc.returncode == 0, err
            results.append(json.loads(out.strip().splitlines()[-1]))
        return results
    finally:
        _retire_all(procs)


def _readline_with_timeout(proc: subprocess.Popen, timeout: float = 60.0) -> str:
    box: dict[str, str] = {}

    def reader() -> None:
        try:
            box['line'] = proc.stdout.readline()
        except ValueError:  # pragma: no cover - closed pipe
            box['line'] = ''

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), 'worker did not signal readiness in time'
    return box.get('line', '')


def _live_client(peer=('198.51.100.11', 52001)):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer),
                             base_url='http://test')


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_LIVE_ENABLED', '1')
    monkeypatch.setenv('NEBIUS_API_KEY', 'test-key-not-used')
    calls: list[str] = []

    def stub(*, fail=False, error=None):
        monkeypatch.setattr(app_module, 'configured_live_agent',
                            lambda court: _StubAgent(calls, fail=fail, error=error))
        return calls

    return stub


# -- the shared location is required, never defaulted, never created ---------------------------

def test_store_path_is_required_and_has_no_local_default(monkeypatch):
    monkeypatch.delenv('CIVICTRACE_SPEND_STORE_PATH', raising=False)
    with pytest.raises(UsageLedgerError) as excinfo:
        SpendGate()
    assert excinfo.value.code == 'LIVE_SPEND_STORE_NOT_CONFIGURED'
    # The historical facade resolves through the same gate, so it cannot resurrect a local path.
    with pytest.raises(UsageLedgerError) as excinfo:
        UsageLedger()
    assert excinfo.value.code == 'LIVE_SPEND_STORE_NOT_CONFIGURED'


def test_relative_store_path_is_refused(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', 'relative/usage')
    with pytest.raises(UsageLedgerError) as excinfo:
        SpendGate()
    assert excinfo.value.code == 'LIVE_SPEND_STORE_NOT_CONFIGURED'
    assert 'absolute' in excinfo.value.message


def test_missing_store_directory_is_refused_and_not_created(tmp_path, monkeypatch):
    absent = tmp_path / 'mount-not-mounted'
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_PATH', str(absent))
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        SpendGate()
    assert excinfo.value.code == 'LIVE_USAGE_LEDGER_UNAVAILABLE'
    # A missing shared mount must not be silently recreated on local disk: that is the
    # container-local failure this backend exists to remove.
    assert absent.exists() is False


def test_store_outside_the_expected_shared_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_EXPECT_ROOT', str(tmp_path / 'mnt' / 'shared'))
    with pytest.raises(UsageLedgerError) as excinfo:
        SpendGate()
    assert excinfo.value.code == 'LIVE_SPEND_STORE_NOT_CONFIGURED'
    # Same store, root widened to its parent: allowed.
    monkeypatch.setenv('CIVICTRACE_SPEND_STORE_EXPECT_ROOT', str(tmp_path))
    assert SpendGate().deployment_id == 'civictrace-rc1'


@pytest.mark.asyncio
async def test_unconfigured_store_refuses_live_and_keeps_offline_path(live_env, monkeypatch):
    calls = live_env()
    monkeypatch.delenv('CIVICTRACE_SPEND_STORE_PATH', raising=False)
    async with _live_client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
        offline = await client.post('/api/challenge', json={'claim_id': 'spent'})
        case = await client.get('/api/case')
    assert live.status_code == 503
    assert live.json()['detail']['code'] == 'LIVE_SPEND_STORE_NOT_CONFIGURED'
    assert calls == []
    assert offline.status_code == 200 and offline.json()['status'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert case.status_code == 200


# -- durable cap: persistence, restart, instances, revisions ---------------------------------

def test_ledger_persists_across_restart_and_refuses_after_cap():
    budget = _budget(request_cap=2)
    first = SpendGate()
    assert [first.reserve(budget).admitted for _ in range(2)] == [True, True]
    # A new process would construct a new object; nothing in memory carries the count.
    restarted = SpendGate()
    decision = restarted.reserve(budget)
    assert decision.admitted is False
    assert decision.code == 'LIVE_REQUEST_BUDGET_EXHAUSTED'
    assert restarted.read_state()['requests_committed'] == 2


def test_two_processes_never_exceed_the_shared_cap():
    results = _run_workers(40, workers=2, cap=50)
    admitted = sum(item['admitted'] for item in results)
    assert sum(item['errors'] for item in results) == 0
    assert results[0]['refused'] + results[1]['refused'] == 80 - admitted
    assert admitted == 50
    state = SpendGate().read_state()
    assert state['requests_committed'] == 50
    # Every committed unit is exactly one reservation row: no leaked or duplicated count.
    assert state['reservations']['total'] == 50


def test_two_simultaneous_instances_share_one_cap():
    results = _run_workers(1, workers=2, cap=1)
    assert sum(item['admitted'] for item in results) == 1
    assert results[0]['instance_id'] != results[1]['instance_id']
    state = SpendGate().read_state()
    assert state['requests_committed'] == 1
    assert state['reservations']['total'] == 1
    assert state['reservations']['last_reservation']['instance_id'] in {
        results[0]['instance_id'], results[1]['instance_id']}


def test_four_processes_never_exceed_the_shared_cap_under_contention():
    results = _run_workers(25, workers=4, cap=40)
    admitted = sum(item['admitted'] for item in results)
    assert sum(item['errors'] for item in results) == 0
    assert admitted == 40
    assert SpendGate().read_state()['requests_committed'] == 40


# -- cleanup is complete even when one child's retirement fails -------------------------------

def test_first_child_retirement_failure_still_retires_every_later_child(monkeypatch):
    """One failing retirement must not abandon the later children or their pipes.

    The first worker is made to fail (a non-numeric cap crashes it), so cleanup runs from the
    body's own failure path, and the first retirement raises *after* reaping its child -- the
    shape that used to abort the walk and leave every later child unreaped with open pipes.
    """
    real_popen = subprocess.Popen
    real_retire = _retire
    spawned: list[subprocess.Popen] = []
    retired: list[subprocess.Popen] = []

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    def failing_first_retire(proc, timeout=60.0):
        retired.append(proc)
        if len(retired) == 1:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=timeout)
            finally:
                raise RuntimeError('injected first-child retirement failure')
        return real_retire(proc, timeout)

    monkeypatch.setattr(subprocess, 'Popen', recording_popen)
    monkeypatch.setattr(sys.modules[__name__], '_retire', failing_first_retire)
    try:
        with pytest.raises(_WorkerCleanupError) as excinfo:
            _run_workers(1, workers=3, cap='not-an-int')

        assert len(spawned) == 3
        # Every child was attempted, in order: the walk did not stop at the failing one.
        assert [id(proc) for proc in retired] == [id(proc) for proc in spawned]
        # Every child -- including the two after the failure -- is reaped with both pipes shut.
        assert [proc.returncode is not None for proc in spawned] == [True, True, True]
        assert [(proc.stdout.closed, proc.stderr.closed) for proc in spawned] == [
            (True, True), (True, True), (True, True)]
        # One aggregate error, not one per child, and it names the failing child and cause.
        cleanup_error = excinfo.value
        assert len(cleanup_error.failures) == 1
        assert '1 worker cleanup failure(s)' in str(cleanup_error)
        assert ('worker[0] RuntimeError: injected first-child retirement failure'
                in str(cleanup_error))
        # The failure that stopped the body is still reachable, not replaced by the cleanup.
        assert isinstance(cleanup_error.__context__, AssertionError)
    finally:
        for proc in spawned:
            real_retire(proc)


def test_first_child_retirement_timeout_still_retires_later_children(monkeypatch):
    """A timeout on a still-running child must not abandon the children after it.

    Three real children are left running; the first retirement raises ``TimeoutExpired``
    before killing anything, so only the post-failure best-effort path can make it safe.
    """
    procs = [_popen('import time; time.sleep(120)') for _ in range(3)]
    real_retire = _retire
    retired: list[subprocess.Popen] = []

    def timing_out_first_retire(proc, timeout=60.0):
        retired.append(proc)
        if len(retired) == 1:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        return real_retire(proc, timeout)

    monkeypatch.setattr(sys.modules[__name__], '_retire', timing_out_first_retire)
    try:
        assert [proc.poll() for proc in procs] == [None, None, None]
        with pytest.raises(_WorkerCleanupError) as excinfo:
            _retire_all(procs)

        assert [id(proc) for proc in retired] == [id(proc) for proc in procs]
        assert [proc.returncode is not None for proc in procs] == [True, True, True]
        assert [(proc.stdout.closed, proc.stderr.closed) for proc in procs] == [
            (True, True), (True, True), (True, True)]
        cleanup_error = excinfo.value
        assert [index for index, _ in cleanup_error.failures] == [0]
        assert isinstance(cleanup_error.failures[0][1], subprocess.TimeoutExpired)
        assert 'worker[0] TimeoutExpired' in str(cleanup_error)
    finally:
        for proc in procs:
            real_retire(proc)


def test_multiple_retirement_failures_are_collected_and_propagated_once(monkeypatch):
    """Failures are collected across children and surface as one aggregate after full cleanup."""
    procs = [_popen('import time; time.sleep(120)') for _ in range(3)]
    real_retire = _retire
    retired: list[subprocess.Popen] = []

    def failing_first_and_last_retire(proc, timeout=60.0):
        retired.append(proc)
        if len(retired) in (1, 3):
            raise RuntimeError(f'injected cleanup failure {len(retired)}')
        return real_retire(proc, timeout)

    monkeypatch.setattr(sys.modules[__name__], '_retire', failing_first_and_last_retire)
    try:
        with pytest.raises(_WorkerCleanupError) as excinfo:
            _retire_all(procs)

        assert [id(proc) for proc in retired] == [id(proc) for proc in procs]
        assert [proc.returncode is not None for proc in procs] == [True, True, True]
        assert [(proc.stdout.closed, proc.stderr.closed) for proc in procs] == [
            (True, True), (True, True), (True, True)]
        cleanup_error = excinfo.value
        assert [index for index, _ in cleanup_error.failures] == [0, 2]
        assert '2 worker cleanup failure(s)' in str(cleanup_error)
        assert 'worker[0] RuntimeError: injected cleanup failure 1' in str(cleanup_error)
        assert 'worker[2] RuntimeError: injected cleanup failure 3' in str(cleanup_error)
    finally:
        for proc in procs:
            real_retire(proc)


def test_concurrent_threads_cannot_race_past_the_cap():
    """The HTTP path reserves via asyncio.to_thread, so in-process races are thread races."""
    from concurrent.futures import ThreadPoolExecutor

    budget = _budget(request_cap=25)
    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _: SpendGate().reserve(budget), range(64)))
    assert sum(1 for decision in decisions if decision.admitted) == 25
    assert sum(1 for decision in decisions
               if not decision.admitted and decision.code == 'LIVE_REQUEST_BUDGET_EXHAUSTED') == 39
    assert SpendGate().read_state()['requests_committed'] == 25


def test_store_is_keyed_by_deployment_constant_not_pid_or_revision(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '2')
    monkeypatch.setenv('CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', '0.01')
    budget = _budget(request_cap=2)
    before = SpendGate()
    first = before.reserve(budget)
    monkeypatch.setenv('CIVICTRACE_REVISION', 'rev-2026-10-30')
    monkeypatch.setenv('HOSTNAME', 'other-instance')
    after = SpendGate()
    second = after.reserve(budget)
    third = SpendGate().reserve(budget)
    assert before.path == after.path
    assert 'pid' not in before.path.name and str(os.getpid()) not in before.path.name
    assert before.instance_id != after.instance_id
    # Rollback/rollout continues counting: two independent instances, one ceiling.
    assert [first.admitted, second.admitted, third.admitted] == [True, True, False]


def test_store_is_created_atomically_and_holds_a_receipt(store_dir):
    gate = SpendGate()
    assert not gate.path.exists()
    assert gate.reserve(_budget(request_cap=1)).admitted
    state = gate.read_state()
    assert state['deployment_id'] == 'civictrace-rc1'
    assert state['requests_committed'] == 1
    assert [row['kind'] for row in state['receipts']] == ['reserve']
    assert {'schema': STORE_SCHEMA, 'journal_mode': 'delete'} == {
        'schema': state['store']['schema'], 'journal_mode': state['store']['journal_mode']}
    # SQLite rollback-journal semantics: nothing but the store file survives a committed
    # admission, and the store's own integrity check passes.
    assert gate.integrity_check() == 'ok'
    assert {path.name for path in store_dir.iterdir()} == {gate.path.name}
    assert not any(name.endswith('-journal') for name in
                   {path.name for path in store_dir.iterdir()})


def test_persisted_dollar_estimate_cap_binds_before_request_cap():
    gate = SpendGate()
    budget = _budget(request_cap=100, dollar_cap='0.02', estimate='0.01')
    assert [gate.reserve(budget).admitted for _ in range(2)] == [True, True]
    refused = gate.reserve(budget)
    assert refused.admitted is False
    assert refused.code == 'LIVE_SPEND_BUDGET_EXHAUSTED'
    state = gate.read_state()
    assert state['estimated_spend_usd'] == '0.020000'
    # Survives a restart: a fresh object still sees the exhausted dollar budget.
    assert SpendGate().reserve(budget).code == 'LIVE_SPEND_BUDGET_EXHAUSTED'


# -- replacement-instance recovery -----------------------------------------------------------

def test_mid_reservation_kill_replacement_recovers_exactly_once(monkeypatch):
    """A process killed after COMMIT leaves one committed, *leased* reservation.

    The replacement releases the lease exactly once (state active -> expired), never refunds it
    and never charges it twice: the committed counter stays 1 and equals the row count. Its own
    two admissions then take the counter to the cap of 3.
    """
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '0.4')
    victim = _popen(HOLD_WORKER, '3')
    try:
        line = _readline_with_timeout(victim)
        assert line.strip(), victim.stderr.read() if victim.poll() is not None else 'no output'
        held = json.loads(line)
        assert held['admitted'] is True and held['state'] == 1
    finally:
        _retire(victim)
    assert victim.returncode != 0

    before = SpendGate().read_state()
    assert before['requests_committed'] == 1
    assert before['reservations']['active'] == 1
    assert before['reservations']['total'] == 1
    assert before['reservations']['last_reservation']['reservation_id'] == held['reservation_id']
    assert before['reservations']['last_reservation']['instance_id'] == held['instance_id']

    time.sleep(0.7)
    replacement = SpendGate()
    assert replacement.instance_id != held['instance_id']
    first_sweep = replacement.startup_sweep()
    assert first_sweep['released'] == 1
    # Exactly once: a second sweep releases nothing.
    assert replacement.startup_sweep()['released'] == 0
    assert replacement.sweep() == 0

    recovered = replacement.read_state()
    assert recovered['requests_committed'] == 1, 'recovery must not refund committed spend'
    assert recovered['reservations']['expired'] == 1
    assert recovered['reservations']['total'] == recovered['requests_committed']

    budget = _budget(request_cap=3)
    second = replacement.reserve(budget)
    third = replacement.reserve(budget)
    assert [second.admitted, third.admitted] == [True, True]
    assert [second.seq, third.seq] == [2, 3]
    assert replacement.reserve(budget).code == 'LIVE_REQUEST_BUDGET_EXHAUSTED'
    final = replacement.read_state()
    assert final['requests_committed'] == 3
    assert final['reservations']['total'] == 3
    assert final['reservations']['expired'] == 1
    assert final['reservations']['active'] == 2


def test_crash_before_commit_leaves_no_reservation(store_dir):
    """A die-inside-the-transaction leaves neither a row nor a counter, and no hot journal."""
    gate = SpendGate()
    gate.read_state()  # initialise the store so the crash happens in a real transaction
    victim = _popen(CRASH_WORKER)
    try:
        out, err = victim.communicate(timeout=120)
    finally:
        _retire(victim)
    assert victim.returncode == 9, err
    assert out.strip().endswith('prepared')

    journal = gate.path.with_name(gate.path.name + '-journal')
    after = SpendGate().read_state()  # BEGIN IMMEDIATE rolls the hot journal back
    assert after['requests_committed'] == 0
    assert after['reservations']['total'] == 0
    assert SpendGate().integrity_check() == 'ok'
    if journal.exists():
        # A journal left beside the store must not be *hot*: SQLite zeroes the header magic when
        # it finishes the rollback, so uncommitted rows can never be read as committed.
        assert journal.read_bytes()[:4] == b'\x00\x00\x00\x00'
    decision = SpendGate().reserve(_budget(request_cap=2))
    assert decision.admitted and decision.seq == 1
    assert not journal.exists(), 'the next commit removes the stale journal'
    assert SpendGate().read_state()['requests_committed'] == 1


def test_sweep_is_exactly_once_under_two_concurrent_replacements(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '0.4')
    stale = SpendGate().reserve(_budget(request_cap=5), now_epoch=time.time() - 60.0)
    assert stale.admitted is True

    start_at = time.time() + 0.5
    procs = [_popen(SWEEP_WORKER, repr(start_at)) for _ in range(2)]
    released = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            released.append(json.loads(out.strip().splitlines()[-1])['released'])
    finally:
        for proc in procs:
            _retire(proc)
    assert sum(released) == 1, f'two replacement sweepers released {released}'
    assert sorted(released) == [0, 1]
    state = SpendGate().read_state()
    assert state['reservations']['expired'] == 1
    assert state['requests_committed'] == 1


def test_settlement_after_lease_expiry_does_not_change_counters(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '0.4')
    gate = SpendGate()
    admission = gate.reserve(_budget(request_cap=5), now_epoch=time.time() - 60.0)
    assert admission.admitted is True
    assert gate.sweep() == 1
    assert gate.settle(admission, outcome='SUCCESS', provider_requests=1) is False
    state = gate.read_state()
    assert state['requests_committed'] == 1
    assert state['reservations']['settled_expired'] == 1
    assert state['reservations']['total'] == 1
    # A settlement can never move money in either direction.
    assert state['estimated_spend_usd'] == '0.010000'


# -- fail-closed admission -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_corrupt_store_refuses_live_and_leaves_offline_path_working(live_env):
    calls = live_env()
    gate = SpendGate()
    gate.directory.mkdir(parents=True, exist_ok=True)
    gate.path.write_bytes(b'{"schema": "civictrace-usage-ledger/1", "requests_committed": -4')
    async with _live_client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
        offline = await client.post('/api/challenge', json={'claim_id': 'spent'})
        case = await client.get('/api/case')
    assert live.status_code == 503
    assert live.json()['detail']['code'] == 'LIVE_USAGE_LEDGER_CORRUPT'
    assert calls == []
    assert offline.status_code == 200 and offline.json()['status'] == 'NOT_VERIFIED_FROM_SELECTED_RECORDS'
    assert case.status_code == 200


@pytest.mark.asyncio
async def test_missing_store_mount_fails_closed(live_env, tmp_path):
    calls = live_env()
    absent = tmp_path / 'blocked' / 'usage'
    (tmp_path / 'blocked').write_text('not a directory', encoding='utf-8')
    os.environ['CIVICTRACE_SPEND_STORE_PATH'] = str(absent)
    async with _live_client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
        offline = await client.post('/api/challenge', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == 'LIVE_USAGE_LEDGER_UNAVAILABLE'
    assert calls == []
    assert offline.status_code == 200
    assert absent.exists() is False


@pytest.mark.asyncio
async def test_busy_store_fails_closed(live_env, monkeypatch):
    calls = live_env()
    monkeypatch.setenv('CIVICTRACE_USAGE_LOCK_TIMEOUT_S', '0.05')
    holder = SpendGate(lock_timeout_s=0.05)
    with holder.exclusive_write_lock():
        async with _live_client() as client:
            live = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == 'LIVE_USAGE_LEDGER_LOCKED'
    assert calls == []


@pytest.mark.asyncio
async def test_unopenable_store_file_fails_closed(live_env, store_dir):
    """A store path that exists but cannot be opened is a refusal, not a skipped ceiling."""
    calls = live_env()
    (store_dir / 'spend-civictrace-rc1.sqlite3').mkdir()
    async with _live_client() as client:
        live = await client.post('/api/investigate', json={'claim_id': 'spent'})
        offline = await client.post('/api/challenge', json={'claim_id': 'spent'})
    assert live.status_code == 503
    assert live.json()['detail']['code'] == 'LIVE_USAGE_LEDGER_UNAVAILABLE'
    assert calls == []
    assert offline.status_code == 200


def test_corrupt_empty_and_foreign_files_raise_typed_errors_not_in_memory_fallback():
    gate = SpendGate()
    gate.path.write_text('{not json', encoding='utf-8')
    with pytest.raises(UsageLedgerCorrupt):
        gate.reserve(_budget())
    # An emptied store must never be read as a first run (that would reset the ceiling).
    gate.path.write_bytes(b'')
    with pytest.raises(UsageLedgerCorrupt):
        gate.reserve(_budget())
    gate.path.write_text(json.dumps({'schema': 'other/1'}), encoding='utf-8')
    with pytest.raises(UsageLedgerCorrupt):
        gate.reserve(_budget())
    # A missing store file is a legitimate first run. Deleting the store is an operator action
    # outside the enforcement boundary, not an in-product reset.
    gate.path.unlink()
    assert SpendGate().read_state()['requests_committed'] == 0


def test_deployment_key_mismatch_is_refused_not_rekeyed():
    gate = SpendGate()
    gate.reserve(_budget(request_cap=1))
    other = SpendGate(directory=gate.directory, deployment_id='other-deploy')
    # The filename differs, so no shared state is possible by accident; the store that exists
    # under the default key refuses a mismatched deployment id written into it.
    assert other.path != gate.path
    other.path.write_bytes(gate.path.read_bytes())
    with pytest.raises(UsageLedgerCorrupt) as excinfo:
        other.read_state()
    assert excinfo.value.code == 'LIVE_USAGE_LEDGER_CORRUPT'


def test_engine_failure_is_a_typed_refusal_not_an_untyped_error(monkeypatch):
    """A failing SQLite write must surface as a governance code, not as a 500."""
    import sqlite3

    import civictrace.spend_gate as spend_gate_module

    def explode(self, *args, **kwargs):
        raise sqlite3.OperationalError('database or disk is full')

    monkeypatch.setattr(spend_gate_module.SpendGate, '_commit_reservation', explode)
    gate = SpendGate()
    with pytest.raises(UsageLedgerUnavailable) as excinfo:
        gate.reserve(_budget(request_cap=1))
    assert excinfo.value.code == 'LIVE_USAGE_LEDGER_UNAVAILABLE'


def test_invalid_budget_configuration_is_a_typed_refusal(monkeypatch):
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', 'many')
    with pytest.raises(UsageLedgerError) as excinfo:
        LiveBudget.from_env(requests_per_claim=3)
    assert excinfo.value.code == 'INVALID_LIVE_ATTEMPT_CONFIGURATION'
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '50')
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_USD', 'NaN')
    with pytest.raises(UsageLedgerError) as excinfo:
        LiveBudget.from_env(requests_per_claim=3)
    assert excinfo.value.code == 'INVALID_LIVE_BUDGET_CONFIGURATION'
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_USD', '5')
    monkeypatch.setenv('CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', '0')
    with pytest.raises(UsageLedgerError) as excinfo:
        LiveBudget.from_env(requests_per_claim=3)
    assert excinfo.value.code == 'INVALID_LIVE_BUDGET_CONFIGURATION'
    monkeypatch.setenv('CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST', '0.01')
    monkeypatch.setenv('CIVICTRACE_DEPLOYMENT_ID', 'bad id/with slash')
    with pytest.raises(UsageLedgerError):
        SpendGate()
    # Reservation TTL and instance identity are validated the same way: a misconfiguration is a
    # refusal, not a silent default.
    monkeypatch.delenv('CIVICTRACE_DEPLOYMENT_ID')
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '0')
    with pytest.raises(UsageLedgerError):
        SpendGate()
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '900')
    monkeypatch.setenv('CIVICTRACE_INSTANCE_ID', 'bad instance/id')
    with pytest.raises(UsageLedgerError):
        SpendGate()


# -- admission ordering and settlement -------------------------------------------------------

@pytest.mark.asyncio
async def test_admission_is_durable_before_the_provider_call_and_never_refunded(live_env, monkeypatch):
    monkeypatch.setenv('CIVICTRACE_MAX_LIVE_ATTEMPTS', '1')
    calls = live_env(fail=True)
    async with _live_client() as client:
        first = await client.post('/api/investigate', json={'claim_id': 'spent'})
        second = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert first.status_code == 502
    assert first.json()['detail']['code'] == 'PROVIDER_TIMEOUT_OR_NETWORK_ERROR'
    assert second.status_code == 429
    assert second.json()['detail']['code'] == 'LIVE_REQUEST_BUDGET_EXHAUSTED'
    assert len(calls) == 1
    state = SpendGate().read_state()
    assert state['requests_committed'] == 1
    assert [row['kind'] for row in state['receipts']] == ['reserve', 'settle']
    assert state['receipts'][1]['outcome'] == 'PROVIDER_UNAVAILABLE'
    assert state['receipts'][1]['settled_state'] == 'settled'
    assert state['reservations']['settled'] == 1


@pytest.mark.asyncio
async def test_admission_is_written_on_success_with_sanitized_receipt(live_env):
    calls = live_env()
    async with _live_client() as client:
        response = await client.post('/api/investigate', json={'claim_id': 'spent'})
    assert response.status_code == 200
    assert calls == ['spent']
    state = SpendGate().read_state()
    settlement = state['receipts'][-1]
    assert settlement['kind'] == 'settle'
    assert settlement['outcome'] == 'SUCCESS'
    assert settlement['prompt_tokens'] == 10 and settlement['completion_tokens'] == 5
    assert 'test-key-not-used' not in json.dumps(state)


@pytest.mark.asyncio
async def test_unknown_claim_does_not_consume_budget(live_env):
    calls = live_env()
    async with _live_client() as client:
        missing = await client.post('/api/investigate', json={'claim_id': 'not_a_claim'})
    assert missing.status_code == 404
    assert calls == []
    assert not SpendGate().path.exists()


@pytest.mark.asyncio
async def test_startup_sweep_recovers_before_the_first_request(monkeypatch):
    """The function the ASGI lifespan calls releases a stale lease without a request."""
    monkeypatch.setenv('CIVICTRACE_SPEND_RESERVATION_TTL_S', '0.4')
    stale = SpendGate().reserve(_budget(request_cap=2), now_epoch=time.time() - 60.0)
    assert stale.admitted
    result = app_module.startup_sweep()
    assert result is not None and result['released'] == 1
    assert SpendGate().read_state()['reservations']['expired'] == 1
    # A second startup sweep (a restarting replacement) releases nothing.
    assert app_module.startup_sweep()['released'] == 0


# -- QC-09: bounded peer state ---------------------------------------------------------------

def test_thousand_distinct_peers_leave_a_bounded_key_count():
    windows = PeerWindows()
    for index in range(1000):
        windows.allow(f'192.0.2.{index % 256}', now=1000.0 + index * 0.001)
    assert 0 < windows.size() <= MAX_TRACKED_PEERS
    assert windows.size() < 1000


def test_peer_windows_expire_and_reclaim_memory():
    windows = PeerWindows()
    for index in range(MAX_TRACKED_PEERS):
        assert windows.allow(f'peer-{index}', now=0.0)
    assert windows.size() == MAX_TRACKED_PEERS
    windows.allow('late-peer', now=3600.0)
    assert windows.size() == 1


def test_peer_limit_still_limits_a_single_peer():
    windows = PeerWindows(limit=3, window_seconds=60.0)
    assert [windows.allow('10.0.0.1', now=float(i)) for i in range(3)] == [True, True, True]
    assert windows.allow('10.0.0.1', now=3.0) is False
    assert windows.allow('10.0.0.2', now=3.0) is True
    # The window slides: the oldest hit expires after 60s.
    assert windows.allow('10.0.0.1', now=3.0 + 61.0) is True


@pytest.mark.asyncio
async def test_http_rate_limit_is_per_peer_and_bounded(live_env):
    calls = live_env()
    async with _live_client(peer=('203.0.113.5', 40001)) as client:
        statuses = [(await client.post('/api/investigate', json={'claim_id': 'spent'})).status_code
                    for _ in range(9)]
    assert statuses[:8] == [200] * 8
    assert statuses[8] == 429
    assert app_module._peer_windows.size() == 1
    async with _live_client(peer=('203.0.113.6', 40002)) as other:
        assert (await other.post('/api/investigate', json={'claim_id': 'spent'})).status_code == 200
    assert app_module._peer_windows.size() == 2


def test_usage_ledger_facade_delegates_to_the_shared_gate():
    facade = UsageLedger()
    assert isinstance(facade._gate, SpendGate)
    assert facade.path == SpendGate().path
    assert facade.reserve(_budget(request_cap=1)).admitted
    assert facade.read_state()['requests_committed'] == 1
    assert isinstance(facade.settle(Admission(False, 'X', 0, 0, Decimal(0), Decimal(0)),
                                   outcome='NONE'), bool)
