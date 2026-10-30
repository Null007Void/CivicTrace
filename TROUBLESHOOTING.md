# Troubleshooting

| Symptom | What to do |
|---|---|
| Python/module missing | Use the virtual environment's Python and install the locked requirements. Do not mix system pip with another interpreter. |
| Port 8765 already in use | Keep the existing app if it is yours, or stop your own earlier foreground instance with Ctrl+C. Do not kill unrelated processes. |
| Invalid jurisdiction/subject/action | Use the tutorial's exact examples. A jurisdiction is a lowercase slug such as `exeter-ca`; it is not a city display name or URL. Subject/action IDs must be stable and consistent between mapping and claim. |
| Source URL refused | Use HTTPS and a hostname explicitly allowed by this case. Publisher must match the declared case publisher. No source download occurs. |
| Source role/predicate mismatch | Authorization needs adopted-action minutes; city phase reporting needs an informational tracker. Do not relabel a source simply to make a claim pass. |
| Exact excerpt rejected | Copy from the imported source text displayed in the app. Do not paraphrase, add ellipses or paste text from a different source. Check source and mapping selection. |
| File rejected/empty | Use a `.txt` UTF-8 file under 65,536 bytes; wait for its text to load. The self-hosted browser script is required for uploads. PDF, OCR and spreadsheet import are unsupported. |
| Mapping still pending | Editing/adding is separate from review. Open the mapping, compare its values to the source, then record `HUMAN_REVIEWED`. |
| Claim not confirmed | Select reviewed support, open the claim, read the confirmation, provide a reviewer label and tick the attestation. |
| Stale revision or invalidated confirmation | Reload the case after edits; re-review affected mappings and reconfirm the current claim. Do not replay an old form. |
| Live button disabled | Default mode is offline. For live mode, restart with `--live`; ensure the claim is confirmed and both distinct source roles have reviewed mappings. |
| `LIVE_CASE_NOT_ELIGIBLE...` | Add and review one legitimate adopted-minutes source and one tracker source; context need not be supporting evidence. |
| Authentication denied | Stop, check the Nebius account/key privately and restart with the correct key. Do not post the key or raw response. The failed attempt remains reserved; check remaining allowance before another attempt. |
| Model unavailable/unapproved | Only the README's two exact model IDs are accepted. Account availability can differ. Another provider's key or endpoint is unsupported. |
| Budget refusal | Limits are lifetime totals for the stable data directory. Failures/uncertain requests retain reservations. Deliberately change total limits only after reviewing spend; never reset/delete the ledger. |
| Timeout, truncation or provider refusal | Stop and read the sanitized error. No automatic retry occurs. A 700-token completion cap includes reasoning, so a real model can exhaust it. Do not interpret failure as Court acceptance. |
| `REVIEW_REQUIRED` or unverified | Inspect reason codes and proposed IDs. Reading both sources does not make both supporting evidence. Correct inaccurate mappings only against the actual records; never alter facts to force acceptance. |
| Cases appear missing | Confirm the printed data path and same `--data-dir`; do not overwrite an existing store. `python -m civictrace` bypasses the portable launcher and uses different default storage. |
| Storage permission error | Choose a writable user-owned data directory. Container users need a persistent writable volume for BOTH cases and spend data. |

The app does not automatically load `.env` files. The recommended launcher deliberately replaces inherited CivicTrace/provider settings; use its documented arguments. Never share keys, raw provider traces, or your case store while asking for help. Safe reports include the error code, release version, OS/Python version, selected model, and whether live mode was enabled.
