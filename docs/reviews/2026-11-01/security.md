# Independent security & acceptance review: swift-sonar-kit reusable full-project mode

Reviewer: dove (independent). Date: 2026-11-01 UTC. Static read-only review: file reads are tool calls, but **no scans, no test execution, no builds, no edits to the repo, no container/credential access, and no child agents**.
Files inspected: src/swift_sonar_kit/{cli,receipts,reports,server}.py; tests/test_{cli,receipts,reports,server}.py; docs/REUSABLE-HASHFLEET-PLAN.md. All reads succeeded (no retries needed).

Framing: v0.1 is honest and narrowly scoped - it prints "selected command only, not a census" and keeps VALIDATION separate. It does **not** claim exhaustive tests. The risks below are mostly **requirements for the proposed reusable/xcresult-depth project mode** the plan adds, plus a few latent gaps in shipped code. They are not claims that the current v0.1 forges evidence or is currently exploitable unless stated. Ordered by severity.

---

## R1 (HIGH, future-mode requirement) xcresult *directory* binding must not be added naively
- Where: `receipts.run_test` does the freshness checks - `path.is_file()` and `path.stat().st_mtime < started` (lines ~86-88). `receipts.verify` does NOT re-check is_file/mtime; it recomputes `file_hash(artifact)` for each requested artifact and compares against the recorded hash (lines ~117-120). `file_hash` opens the path with `open("rb")`. All of these assume a regular file.
- Current behaviour is fail-closed: an `.xcresult` is a bundle directory, so today `run_test` raises "was not freshly written" for any directory artifact (and `file_hash` would error on a directory). There is no current escape here.
- Risk is for the *proposed* extension (plan item 5, "extend receipts to support xcresult directories safely"). If added naively by hashing only the top path or trusting the directory mtime, the receipt will NOT detect (a) a stale inner result file under a freshly `touch`ed bundle dir, nor (b) a symlink inside the bundle pointing outside root. Note `receipts.snapshot` (lines 37-41) already permits in-root file symlinks but binds both the link text (`os.readlink`) and the resolved content, and rejects escaping/non-file links; `cli._sources` (lines 62-63) is stricter and rejects all symlinks in the selected tree. The new artifact path should reuse snapshot-style binding rather than reinvent weaker checks.
- Fix for the new mode: recursively enumerate bundle contents in sorted order; reject any `is_symlink()` or resolved path not `is_relative_to(root)`; hash every contained file; require every inner file's mtime >= `started`, not just the directory's.
- Failing-case fixtures to add (with the new mode):
  - xcresult dir containing an inner symlink to `/etc/hosts` or `../escape` -> run_test/verify must raise.
  - xcresult dir whose mtime is fresh but an inner file has `os.utime(..,(1,1))` (stale) -> must raise "not freshly written".
  - xcresult dir whose inner bytes change between run_test and verify -> `verify` must raise "not bound".

## R2 (HIGH, future-mode requirement) Project mode must bind the test recipe; `--max-test-age` is unbounded
- Where: `receipts.verify` (lines 98-121) validates schema/exit_code/scope/timestamps/inputs/artifacts but does not check `command_name`/`command_sha256` (which `run_test` records at lines 91-92). `cli._scan` (lines 83, 125, 135) calls `verify` without a command. `verify` rejects only `max_age <= 0` (lines 99-100); `cli` exposes `--max-test-age` as an unbounded int (cli.py line 36).
- Clarification: this is NOT a claim that v0.1 forges legacy evidence. v0.1 deliberately records only a *selected-command* receipt and advertises exactly that. The gap is that the proposed deeper project mode (plan items 5/6: "bind source, build recipe, successful results" and "independently reconcile enumerated test identities") needs stronger binding than the current receipt provides. As-is, the receipt gate is satisfied by any prior successful command (the test harness legitimately seeds it with `assert 1 + 1 == 2`, test_cli.py line 52), and a consumer can pass a very large `--max-test-age` to accept an old receipt whose inputs happen to be unchanged.
- Fix for the new mode: have `_scan` require and pass the expected command/recipe identity (and, for xcresult, the enumerated test-identity census) into `verify`, comparing `command_sha256`; cap `max_test_age` with a sane upper bound or anchor freshness to the build artifact rather than a caller-chosen window.
- Failing-case fixtures to add:
  - Project-mode receipt produced by a no-op command (`true`) must be rejected when a real test recipe is configured.
  - `verify(root, [], max_age=10**9)` on a receipt finished far in the past must still fail staleness once capped.
  - Receipt whose recorded `command_sha256` differs from the configured recipe -> reject.

## R3 (MEDIUM) Native + generic SwiftLint duplication risk
- Where: `cli._scan` always writes `sonar.externalIssuesReportPaths=.swift-sonar/issues.json` (lines 102, 108). `server.preflight` requires the `sonarapple` plugin to be installed and exactly versioned (server.py lines 77-79).
- Clarification: an installed plugin does NOT prove the native SwiftLint sensor is enabled in the active quality profile, nor that every finding is exactly doubled. So this is a **duplication risk**, consistent with the plan's own warning and its **prior documented v0.1 fixture observation** ("Native and generic SwiftLint imports can duplicate findings. This was observed in v0.1 fixture validation"), not a guaranteed 2N on every HashFleet run. The code offers no mode toggle, native-rule disable path, or dedup, so if both the native sensor and the generic import are active for the same sources, findings can duplicate.
- Fix: implement the plan's explicit mode split - compatibility mode keeps the native import and should not also emit generic SwiftLint issues; an opt-in generic mode needs an administrator-reviewed profile that disables the native rules. Record which mode ran in scan-summary.json.
- Failing-case fixtures to add:
  - With both native sensor and generic import active for the same scope, assert findings are not duplicated (count stays N under compatibility mode).
  - Assert generic `issues.json` is not registered when native sonarapple rules are active for the same scope.

## R4 (MEDIUM, future-mode requirement) Reused/consumer properties will need a dangerous-key allowlist
- Where: `cli._scan` currently builds a fixed, hardcoded properties dict (lines 103-112) and does NOT reuse any checked-in consumer `sonar-project.properties`. So there is **no demonstrated current endpoint/credential exploit** in v0.1. The gate is also argv-forced (`-Dsonar.qualitygate.wait=true`, line 133) and `SONAR_*` env is stripped from the lint/scanner env build (line 85).
- Risk is for plan items 2-3, which call for reusing "reviewed existing Sonar properties" from a checked-in consumer config. When that is implemented, a consumer properties file could carry `sonar.host.url`, `sonar.token`/`sonar.login`/`sonar.password`, `sonar.qualitygate.*`, or `sonar.scanner.*`, redirecting the endpoint or weakening the gate.
- Fix for the new mode: before merging any consumer/reused properties, reject keys matching endpoint/credential/gate/scanner families (plan item 3). Keep server/token strictly in the trusted invocation + secret store.
- Failing-case fixtures to add (with the new mode): consumer properties containing each dangerous key -> rejected; a benign multi-language exclusion key -> preserved.

## R5 (MEDIUM) Separate the hypothetical secret-adapter risk from the shipped SwiftLint path
- Where: `reports.swiftlint_report` copies `reason` verbatim into `primaryLocation.message` (reports.py lines 34, 42-46); `cli._scan` writes that to `issues.json` for import. Secret redaction exists only in `cli.main`'s stderr handler (lines 171-173) and `server.preflight`'s version field (server.py line 85).
- Two distinct points, not conflated:
  1. Demonstrated-path note: there is currently no redaction of SwiftLint finding messages before publication. This is low risk for SwiftLint itself, whose `reason` text is rule descriptions rather than secret values; it is a pass-through observation, not a demonstrated secret leak.
  2. Hypothetical future risk: the plan proposes a Gitleaks adapter and states "never publish secret values to Sonar." A secret-detection adapter built on the current verbatim-message pass-through would import raw secrets into the dashboard. This is the material risk and it is contingent on that adapter being added.
- Fix: add a redaction/validation boundary for all finding messages before `issues.json` is written; for any future secret-detection adapter, store redacted match context only, never the raw secret.
- Failing-case fixtures to add: for a future secret adapter, a finding whose match contains a token-like secret -> published message is redacted (or rejected); prove raw secret bytes never reach issues.json.

---

## Acceptance-test gaps (missing negative coverage the plan's "Acceptance" section demands)
- No xcresult import path exists yet, so none of these are tested: fresh-bundle requirement, symlink/traversal rejection inside bundles, independent test-identity reconciliation (R1/R2).
- No test distinguishing a no-op/trivial command from a real test recipe in project mode (R2).
- No test for native-vs-generic duplication / mode separation (R3).
- No test rejecting endpoint/credential/gate-bypass keys in reused properties (R4).
- No test that published issue messages are redaction-safe for a future secret adapter (R5).
- "Same or deeper" / full-project coverage remains unproven by the plan's own account: it records that no two end-to-end scans were compared and that the five-worker review produced no independent approval (plan sections "What has actually been compared" and "Five-worker review record"). Do not advertise full-project or exhaustive testing until these fixtures pass.

## Positive findings (keep)
- Token kept off argv and out of generated files; `SONAR_SCANNER_JSON_PARAMS` stripped (cli lines 85, 131; test_cli line 76). Note: the scanner subprocess DOES receive `SONAR_TOKEN` in its environment by design (cli line 131 `env.update(SONAR_HOST_URL=..., SONAR_TOKEN=token)`) - the token stays off argv and off disk, but is intentionally passed via the scanner's env.
- Gate forced on argv; lint findings fail the scan independent of a green server gate (cli lines 133, 140; test_cli test_lint_findings_cannot_be_hidden_by_green_server_gate).
- Redirects refused and token never leaked in network errors (server.py NoRedirect, _get; test_server tests).
- Source-dir handling: `cli._sources` rejects all symlinks in the selected tree, plus overlap/whole-repo selection. `reports._source` rejects traversal and symlink *escapes* (resolved path must be `is_relative_to(root)` and a file) but permits safe in-root links; `receipts.snapshot` likewise permits in-root file symlinks while binding link text and resolved content. These are escape rejections, not blanket symlink bans.
- Receipt invalidates prior success on failed rerun and detects source mutation during tests (receipts run_test lines 72, 81).
- Artifact directories currently fail closed (R1), so the xcresult risk is strictly about a future naive extension.
