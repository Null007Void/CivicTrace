"""CT-QC-062 real-browser reference client: a Playwright-driven Chrome against the shipped pages.

What this test proves, when it can run: a genuine browser, loading the shipped workbench and using
only the shipped pages (the Create Case form with a picked ``.txt``), can create a case, add a fact
mapping, record a human review, cite the mapping in a claim and confirm it. The defect fixed in this
task was exactly that a real browser could not: the page ships ``Referrer-Policy: no-referrer``, so
Chrome serialises ``Origin: null`` on a normal same-origin form POST, and the 060 guard rejected every
opaque Origin. An ASGI test that invents ``Origin: http://test`` cannot see it.

Browser availability is environment-dependent. The harness searches the named interpreter,
the current interpreter and PATH for Playwright; if none can import it the test skips. An
importable package without a working browser is a failure, not a skip. Follow
``docs/BROWSER_TESTING.md`` for matched Chromium installation and isolated PATH instructions.
No default browser-pass claim is made.

The header-decision matrix for the measured Chrome shapes is covered unconditionally (no browser
needed) by ``tests/test_browser_origin_proof.py``.

Synthetic fixtures only: no provider call, no URL fetch, no paid resource, no network beyond
127.0.0.1. The browser and the uvicorn child are this test's own processes and are closed in the
same test that started them.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'tests' / 'browser_reference_driver.py'


def _importable(python: str, module: str) -> bool:
    try:
        probe = subprocess.run([python, '-c', f'import {module}'], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _playwright_python() -> str | None:
    """The interpreter that will drive the browser, or ``None`` when none is available."""
    candidates = []
    named = os.environ.get('CIVICTRACE_PLAYWRIGHT_PYTHON')
    if named:
        candidates.append(named)
    candidates.append(sys.executable)
    for name in ('python3.11', 'python3', 'python'):
        found = __import__('shutil').which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if pathlib.Path(candidate).exists() and _importable(candidate, 'playwright'):
            return candidate
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _close_child_streams(child: subprocess.Popen) -> None:
    """Close the child's stdio pipes; idempotent, tolerating already-closed streams.

    ``terminate()``/``wait()`` reaps the process but leaves a ``stdout=PIPE`` handle for the garbage
    collector, and pytest turns that ``ResourceWarning`` into a failure under ``-W error``. Closing
    both streams here makes the strict run measure the product rather than this harness. This is the
    same correction CT-QC-049R made in ``tests/test_usage_governance.py``.
    """
    for stream in (child.stdout, child.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):  # already closed by ``communicate`` or by the child's exit path
            pass


def _wait_for_workbench(base_url: str, timeout: float = 60.0, child=None) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if child is not None and child.poll() is not None:
            raise AssertionError(f'uvicorn exited before serving: {child.returncode}')
        try:
            with urllib.request.urlopen(base_url + '/healthz', timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:  # still starting
            last = exc
        time.sleep(0.2)
    raise AssertionError(f'the app never answered on {base_url}: {last}')


def test_real_browser_can_drive_the_workbench_create_case_flow(tmp_path):
    playwright_python = _playwright_python()
    if playwright_python is None:
        pytest.skip('no Playwright-capable interpreter on this host; set '
                    'CIVICTRACE_PLAYWRIGHT_PYTHON to run the real-browser flow')
    if not _importable(sys.executable, 'uvicorn'):
        pytest.skip('the interpreter running this test cannot start uvicorn')

    port = _free_port()
    base_url = f'http://127.0.0.1:{port}'
    store = tmp_path / 'cases.json'
    out = tmp_path / 'browser-flow.json'
    env = dict(os.environ)
    env.update({'CIVICTRACE_CASE_STORE': str(store), 'PYTHONUNBUFFERED': '1'})
    child = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'civictrace.app:app',
         '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for_workbench(base_url, child=child)
        run = subprocess.run(
            [playwright_python, str(DRIVER), 'flow', '--base-url', base_url,
             '--work-dir', str(tmp_path), '--out', str(out)],
            capture_output=True, timeout=300, cwd=str(ROOT), env=env)
        assert run.returncode == 0, (
            f'driver failed ({run.returncode})\n'
            f'stdout: {run.stdout.decode("utf-8", "replace")[-3000:]}\n'
            f'stderr: {run.stderr.decode("utf-8", "replace")[-3000:]}')
    finally:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:  # own child only; never a blanket kill
            child.kill()
            child.wait(timeout=20)
        finally:
            _close_child_streams(child)

    result = json.loads(out.read_text(encoding='utf-8'))
    steps = {step['step']: step for step in result['steps']}
    assert result['launched'] in ('chrome', 'bundled-chromium'), result
    assert any(step.startswith('pick a .txt file') for step in steps), list(steps)
    assert steps['create case with a picked .txt']['imported_notice'] is True
    assert '/workbench/cases/case_' in steps['create case with a picked .txt']['url']
    assert steps['add fact mapping']['notice'] is True
    assert steps['record human review']['state'] is True
    assert steps['confirm claim with attestation']['confirmed'] is True
    assert steps['reopen the case page']['shows_claim'] is True

    created = [post for post in result['posts']
               if post['url'].endswith('/workbench/cases') and post['method'] == 'POST']
    assert created, result['posts']
    assert created[0]['content_type'] == 'application/x-www-form-urlencoded'
    assert created[0]['origin'] in ('null', None), (
        'the measured Chrome build serialises an opaque Origin for this page; if this changed, the '
        f'header matrix in test_browser_origin_proof.py is the authority, not this test: {created[0]}')
    statuses = [entry['status'] for entry in result['write_responses']
                if entry['url'].endswith('/workbench/cases')]
    assert statuses and all(status in (200, 303) for status in statuses), result['write_responses']

    # The store the browser wrote is the real one on disk, not an in-memory fake.
    stored = json.loads(store.read_text(encoding='utf-8'))
    assert result['case_id'] in json.dumps(stored), result['case_id']
