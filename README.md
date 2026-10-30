# CivicTrace

**See what selected public records actually establish.** Create a local case, import text, review exact evidence mappings, and investigate a claim. CivicTrace separates a model's proposed citations from the independent Evidence Court decision.

This is a portable release candidate for local testing, not a hosted service. Source verification and independent fact audit remain pending. An authorization is not a payment or proof of physical completion.

## Start here

Use Python 3.11 or newer (the tested development runtime is Python 3.13). Unzip the package into a directory you control. In that directory:

```text
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe launch.py
```

On macOS or Linux:

```sh
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python launch.py
```

Use `python3` for the first command if that is your Python command. Installation needs package-index access unless you already have all locked wheels. The runtime needs no GPU. Open **http://127.0.0.1:8765/workbench** and follow [Your first case](docs/FIRST_CASE.md). The home page also provides a built-in Exeter demonstration.

The launcher starts **offline** and ignores inherited live credentials/settings. It does not read `.env` files. Stop with Ctrl+C. Restart with the same command to reopen your cases.

Default data location: `%LOCALAPPDATA%/CivicTrace` on Windows; `~/.local/share/civictrace` on macOS/Linux. `cases.json` contains cases and history; `spend/` contains durable reservations. To choose a location, append `--data-dir <directory>`. Keep using the same directory; replacing or deleting spending state can erase accounting history. Stop the server before backing up the entire data directory. Run one server per case store.

## Optional real model investigation

CivicTrace has its own bounded investigator. It supports **Nebius Token Factory only**, at the fixed regional endpoint `https://api.tokenfactory.us-central1.nebius.com/v1/chat/completions`. It is not a universal agent connector or arbitrary OpenAI-compatible endpoint switch.

After completing the offline tutorial and deciding your budget, stop the server and restart with:

```text
<your-venv-python> launch.py --live
```

Use the same Python executable as above. Enter your Nebius key at the **hidden console prompt**, with recording off. Do not paste it into chat, a command line, `.env.example`, or a source file. The key stays in the running process; it is not saved by the launcher. Opening live mode does not itself send a provider request. Click the live investigation button only when ready.

The default model is `nvidia/nemotron-3-super-120b-a12b`. The only other accepted `--model` value is `nvidia/Nemotron-3_5-Lightning`; that alternative has not received the real-run qualification described below. Your account must have access to the selected model. Other providers/models require a code integration and testing; supplying their keys does not work.

Default total limits for one data directory: **2 claim investigations**, at most **3 provider requests each**, with **$0.15 reserved per possible provider request**: $0.45 per investigation, $0.90 total. A failure or uncertain timeout keeps its reservation. There are no automatic provider retries or refunds. These are conservative local estimates, **not a provider billing hard cap**; actual pricing/tokenization and account-level use can differ. Check your Nebius account's controls and usage separately.

Restarting does not replenish these limits. To deliberately raise a lifetime allowance, use `--max-investigations <total>` and `--budget-usd <total>` on the same data directory; prior reservations still count. These are totals, not additional allowance. Never reset the ledger to retry. Each provider **request** sends `max_completion_tokens=700`, including visible output and reasoning tokens. Each logical claim investigation makes at most **3 provider requests**; 700 is not an aggregate per-claim token limit.

See [Troubleshooting](docs/TROUBLESHOOTING.md), [Container storage](docs/CONTAINERS.md), and [Release status](docs/RELEASE_STATUS.md). Development checks: install `requirements-dev.lock` with `--require-hashes`, then `python -m pytest -q`. Read [Browser test setup](docs/BROWSER_TESTING.md) first: the suite searches for Playwright in the current interpreter and on PATH, and an installed Playwright package without its browser is a failure, not a successful browser check.

## What a result means

- `DOCUMENTED_ACTION`: selected documentary evidence supports the precise action, not downstream payment or construction.
- `CITY_REPORTED_STATUS`: the city reported a phase as of a date; it is not physical verification.
- `REVIEW_REQUIRED`: unresolved conflicts or citation/claim problems need human review.
- `NOT_VERIFIED_FROM_SELECTED_RECORDS`: this selection does not establish the claim; it does not prove the claim false.
- `WRONG_ACTION_SOURCE`: the citation concerns another action and cannot support this claim.
- `CONTRADICTED_BY_RECORD`: the selected record contradicts the precise documentary claim.

Both source roles must be read in a live investigation; contextual facts need not support the claim. The result separates registered proposed IDs, Court evidence/context, executed tools and usage. Dispatch counts do not prove delivery. Token usage is provider-reported, not an invoice.

## Scope, privacy and license

Imports are UTF-8 `.txt` or pasted text, up to 65,536 bytes per source. URLs are recorded but not fetched. PDF/OCR/spreadsheet import and municipal web crawling are not supported. Imported-text hashes do not authenticate originals. Review and confirmation are recorded human judgments, not proof of truth.

Keep this single-user service on loopback. It has no user authentication; do not expose its writable workbench publicly. Cases stay in your local data directory. When you run a live investigation, the selected claim and registered source excerpts are sent to Nebius; do not import private data you are unwilling to send.

Application code: [MIT](LICENSE). Copyright (c) 2026 Null007Void / Null Foundry. Dependencies: [third-party notices](THIRD_PARTY_NOTICES.md), [runtime SBOM](docs/SBOM.runtime.cdx.json). Tutorial source excerpts retain their separate attribution and status in [tutorial/README.md](tutorial/README.md); they are not relicensed as CivicTrace code.
