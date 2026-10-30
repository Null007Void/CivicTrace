# RC3 validation â€” 2026-10-29

RC3 is a corrective publication candidate, pending independent acceptance of its exact archive. No provider request, credential, browser launch, container operation or owner-state change occurred in this corrective build.

## RC3 local verification

- 88 targeted offline tests passed in 2.34 seconds, including two distribution-notice regressions. Docker now copies LICENSE, THIRD_PARTY_NOTICES.md and the runtime SBOM into the image before USER. Root MIT attribution is Copyright (c) 2026 Null007Void / Null Foundry; standard MIT terms are unchanged. Third-party notices remain byte-identical to RC2.

## Retained RC2 local verification

- 86 targeted offline tests passed in 3.70 seconds. This includes the portable launcher, bundled tutorial HTML-form routes, app/provider adapter, citation diagnostics, completion cap and new package-metadata regression. Existing Python 3.13 development environment used; no fresh local installation claimed.
- New ZIP regression simulates initial creator-system defaults 0 (Windows) and 3 (Unix), checks normalized creator/version/timestamp/permission metadata, verifies both artifacts and asserts byte identity. A repeated same-platform build is also checked. End-to-end Windows/Linux archive byte identity remains UNVERIFIED; Python/zlib/toolchain differences can still affect compression.
- All 21 core civictrace/data files remain byte-identical to accepted base manifest be8ce3af679bd3b2c75501ed80524ac66030fd7aa96c692718f7aa0322d30343. Browser test executable logic is unchanged; only its prerequisite docstring changed. No runtime token-cap or Court/accounting change.
- THIRD_PARTY_NOTICES component tables, license texts, NOTICE and bundled-file scope are byte-for-byte unchanged from RC1; only its inaccurate generator instruction was replaced with an accurate historical provenance/maintenance note. The referenced dependency maintenance document is now included.
- SBOM/lock coherence passed: 34 components, 34 license blocks, 593 parsed hashes; no dependency changes.
- Proposed file inventory, Python syntax, owner/private-path/credential-pattern scan and strict archive hash/member verification are checked before delivery. No Git history is included.

## Independent RC1 cloud review â€” reported evidence, not a new RC2 run

Fresh hash-locked runtime and development installs and pip check passed. Review reported 466 tests passed and one browser-environment failure, 47 second-case offline checks, six Court checks and 34-component SBOM verification. The browser test detected Playwright but could not obtain a usable browser in that environment: Chromium launch hit EPERM and the supported escalation environment failed to start because of a bwrap limitation. This is not a browser PASS or a demonstrated product assertion failure. RC2 documents prerequisites/discovery and does not weaken or skip the failing test.

The earlier local RC1 builder had 85 targeted tests passing, 467 tests collected, six Court smoke checks, a same-platform repeat ZIP, and fresh extraction/help smoke. Its full suite/fresh-install/browser work was deferred due low available host memory; do not substitute those limited checks for independent cloud evidence.

## Remaining qualification

A real-browser pass on this final package is still outstanding; follow BROWSER_TESTING.md in an authorized environment with matching Playwright/Chromium and sufficient resources. No new heavy local tests were run under the resource constraint. Docker remains unqualified because its Linux engine was unavailable; prepared writable case/spend volume changes need a real build/start/restart check. Independent review must verify RC3 identity and corrections before public upload. Public destination and release creation remain separate tasks. Do not claim universal provider support, source authentication, or a provider invoice hard cap.
