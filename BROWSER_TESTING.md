# Browser test prerequisites and honest results

The normal application does not require Playwright. The optional `tests/test_browser_reference_client.py` launches its own temporary loopback server and browser to test file upload, mappings, reviews and confirmation. The Python package and matching browser binary must BOTH be installed for the selected test interpreter. Browser process creation, sandbox operation and loopback connections must be permitted by the execution environment. Do not disable a sandbox or weaken test assertions to get a pass.

## Run the real browser test

Create a dedicated virtual environment without `--system-site-packages`. Install the hash-locked development dependencies first. Then, only in an environment where software/browser installation is authorized:

```text
<venv-python> -m pip install --require-hashes -r requirements-dev.lock
<venv-python> -m pip install playwright
<venv-python> -m playwright install chromium
<venv-python> -m pip show playwright
```

Playwright is an optional test dependency, not part of CivicTrace's locked runtime. Record its installed version and browser version in review evidence. Use the same interpreter for installation and testing; updating Playwright may require installing its matching browser again. On supported Linux systems, missing OS libraries may require the documented `python -m playwright install --with-deps chromium` setup in an approved test image; that command changes OS dependencies and is not required or automatically run by CivicTrace. Official instructions: https://playwright.dev/python/docs/browsers .

Set `CIVICTRACE_PLAYWRIGHT_PYTHON` to the absolute path of that environment's Python. For example, in PowerShell:

```powershell
$env:CIVICTRACE_PLAYWRIGHT_PYTHON = (Resolve-Path .venv\Scripts\python.exe).Path
& $env:CIVICTRACE_PLAYWRIGHT_PYTHON -m pytest tests/test_browser_reference_client.py -q
```

On macOS/Linux, from the project root:

```sh
CIVICTRACE_PLAYWRIGHT_PYTHON="$PWD/.venv/bin/python" .venv/bin/python -m pytest tests/test_browser_reference_client.py -q
```

The driver first tries installed Google Chrome (`channel=chrome`), then Playwright's bundled Chromium. Installing bundled Chromium is enough for the fallback; do not install/replace the user's global Chrome just to satisfy this test. Both failures are reported. The test does not convert a missing browser, launch denial or assertion error into a skip.

## Understand discovery and skips

The harness probes, in order: `CIVICTRACE_PLAYWRIGHT_PYTHON` if set, the pytest interpreter, and `python3.11`, `python3`, `python` found on PATH. An invalid named interpreter does not prevent later fallback. If none can import Playwright, the browser test reports a skip. If Playwright imports but its browser is absent or cannot launch, the test fails. A successful import is not browser qualification. A fresh venv alone does not stop discovery of a globally installed Playwright interpreter on PATH.

For a deliberately dependency-only test run, use a fresh venv with no Playwright, unset `CIVICTRACE_PLAYWRIGHT_PYTHON` and `PYTHONPATH`, disable user-site imports, and limit PATH for that command to the venv executable directory. For example on macOS/Linux:

```sh
/usr/bin/env -u CIVICTRACE_PLAYWRIGHT_PYTHON -u PYTHONPATH PATH="$PWD/.venv/bin" PYTHONNOUSERSITE=1 .venv/bin/python -m pytest -q
```

On Windows, use a dedicated PowerShell session and close it afterward; changes here are process-local:

```powershell
Remove-Item Env:CIVICTRACE_PLAYWRIGHT_PYTHON -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONNOUSERSITE = '1'
$env:PATH = (Resolve-Path .venv\Scripts).Path
& .\.venv\Scripts\python.exe -m pytest -q
```

This intentionally isolates optional-browser discovery, not a claim that the browser test passed. Keep its skip in the report and run the real-browser instructions separately. Do not use PATH isolation to conceal an already failing browser result.

## Current qualification

Independent RC1 cloud review reported fresh runtime/development hash-locked installs and `pip check` passed; 466 tests passed and the browser test failed for its environment. Additional offline checks passed (47 second-case checks, 6 Court checks and 34-component SBOM). Chromium launch encountered EPERM, and the supported escalation environment failed to start because of a bwrap limitation. These are reported as environment blockers, not a browser PASS or demonstrated product defect. RC2 does not change browser test logic and has no new browser PASS. Docker remains separately unqualified.
