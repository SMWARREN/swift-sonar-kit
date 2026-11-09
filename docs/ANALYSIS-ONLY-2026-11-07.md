# Source-only default workflow, November 7, 2026

## Operator decision and scope

Version 0.4.0 introduced the change to the public `./scan PROJECT` / `swift-sonar run PROJECT` default to **SwiftLint lint plus Sonar source analysis only**. Automatic project builds, simulators, test execution, test enumeration, coverage collection, native manifests and test receipts are not part of this command. The operator reserved running the actual scan themselves. No actual consumer lint or Sonar scan, server provisioning, simulator, application build or application test run was performed to validate this change.

Explicit advanced `project-test`, `project-scan` and `project-run` commands retain their previous test/artifact contract. The old internal `workflow.run` orchestration is no longer the public dispatcher. Its retained unit tests are not evidence of public source-only behavior. Earlier OrbitMath and native-result receipts are historical and do not establish acceptance of this new workflow.

## Implementation boundaries

- `analysis_config.discover_analysis` reads reviewed source/test scope and tool pins without loading test recipes through `load_project`, requiring manifests, probing tools or writing files. A recipe's test command/artifact section is ignored. Report properties are omitted from the effective configuration only. Original recipes, properties and build outputs remain unchanged. Fresh conventional layouts are discovered without generating recipes or enumerating Xcode tests. Ambiguous scope fails instead of guessing the whole repository.
- `analysis_runner.run` uses private analyzer/runtime setup with `python_coverage=False`. `--plan-only` is read-only with no probes/downloads. `--setup-only` writes a separate setup result without analysis or replacement of previous scan reports. `--prepare-only` runs local lint and prepares source-only settings without server requests.
- SwiftLint runs once with strict JSON output. Genuine findings are handed off in memory, converted through the existing strict generic-issues adapter, deduplicated and filtered to the selected source/test inventory. The full original findings remain saved. `lint.imported_findings` counts entries included in this generic report, not issues the server has confirmed displaying. Test-file entries may not become server-visible issues. Invalid finding paths or line numbers fail even when outside the selected scope.
- Generic issues and effective settings are checked for modification before and after submission. Source/configuration content and analyzer executable hashes are rechecked after lint, after server preflight and after the scanner. Valid lint findings proceed to Sonar without a flag, but keep the overall result nonzero. Tool errors and invalid evidence stop. Scanner nonzero leaves publication unknown and the gate not established, rather than claiming no upload or a successful audit.
- The scanner receives a temporary PATH containing only `sh`, `dirname`, `basename`, `readlink`, `uname`, `sed`, `grep` and, as corrected in 0.4.1, `/usr/bin/security`. The certificate utility is needed by scanner SSL trust loading. This exposes normal user-level keychain capabilities to the scanner/plugins and is not a keychain sandbox. The pinned launcher uses those utilities and its embedded JRE. Native plugin SwiftLint, Periphery, compiler, MobSF and OCLint executable lookup is unavailable, avoiding duplicate lint execution or additional tool-driven workflows. No tool stubs, replayed empty reports, fake coverage or quality-profile changes are used. This is not a sandbox for arbitrary server plugins. Optional unavailable sensors may log warnings.
- Effective native/Python report paths point to unique nonexistent sentinels and generic coverage/test report paths are cleared. No fake report files are created. Original coverage/native report paths cannot re-enter the source-only effective scope.
- Tables, `--json`, private atomic `.swift-sonar/run-result.json` saving, and separately consented SwiftLint correction behavior remain available. Corrections are offered only after saving the failed report and do not upgrade its original status or exit code.

## Observed verification

Final portable suite command, with live acceptance disabled:

```sh
env -u SONAR_TOKEN -u SONAR_HOST_URL -u SONAR_SCANNER_OPTS -u JAVA_TOOL_OPTIONS \
  -u SWIFT_SONAR_RUN_REAL_TESTS -u SWIFT_SONAR_TEST_REAL_BUNDLE \
  -u SWIFT_SONAR_TEST_REAL_FAILED_BUNDLE PYTHONPATH=src \
  python3.12 -m unittest discover -s tests -v
```

Observed final result: **434 tests run, 431 passed, 3 explicitly skipped, 0 failures/errors**. Skips are the opt-in real SwiftPM acceptance and two external native-result checks. No missing live receipt is counted as passing. Earlier full-suite integration failures were retained in the local log: legacy fixtures used removed public parser fields and the former dispatcher. The repair separated their original explicit namespace/rendering contract from public dispatch, preserving their assertions and production legacy code. An independent read-only review confirmed this separation and reviewed the final runner/import/CLI boundaries.

The new source-only modules have 17 discovery tests and 24 runner tests. Runner tests invoke the real public CLI with fake analyzer processes and reject every subprocess except read-only Git and the explicitly mocked lint/scanner. Additional guards reject legacy test runners, test preparation, receipt execution, recipe loading and build-output archival. Cases cover fresh and reviewed projects, missing manifests, paths with spaces, genuine findings imports, deduplication/scope, version-prefix matching and rejection, mutation failures, lint continuation, scanner failure uncertainty, credentials, setup/prepare/plan-only, JSON parity, correction handoff and temporary PATH cleanup. They do not exercise the real Sonar engine.

Additional observed checks:

- Cached Ruff 0.14.3 passed for all changed Python files. `git diff --check` and `sh -n scan` passed. No linter installation or network access was needed.
- The checkout launcher `./scan --help` ran with an explicitly installed Python and performed no setup or scan. At this initial checkpoint, runtime and package metadata both reported 0.4.0 (the patch below advances both to 0.4.1).
- Read-only discovery against the actual consumer's reviewed `Tools/swift-sonar-project.json` found **368 production and 304 test files**, pins SwiftLint 0.65.1 / scanner 8.3.1 / sonar-apple 0.5.1. It omitted the three reviewed native/generic/Python coverage properties without reading test artifacts or executing the recipe. This is source-discovery evidence only, not a successful lint or scan.

## Remaining acceptance limits

The restricted scanner environment, missing-report overrides and generic issue import have **not** been exercised in a real server scan for this release. Actual plugin warnings, parser compatibility, server-visible issue counts, remote quality gate and whole-flow success remain unverified. The executable fingerprint covers the scanner launcher, not a full rehash of its embedded JRE and jars. Scanner work directories are unique and retained under `.swift-sonar/`, so repeated runs accumulate local diagnostics. Installed external tools and platform prerequisites remain bounded by the bootstrap documentation.

The operator can now run the normal terminal command to inspect the actual experience. A successful synthetic suite is not a claim that this real scan has already succeeded.

## Operator-reported startup failure and 0.4.1 repair

After the initial commit, the operator ran the actual command and reported SonarScanner failing in `MacCertificateUtils` with `Cannot run program \"security\"` / ENOENT before analysis. The seven-entry PATH omitted macOS's certificate reader. The earlier mocked fixture validated only the chosen allowlist and missed this real runtime dependency.

Version 0.4.1 adds only the genuine `/usr/bin/security` executable to scanner lookup. Certificate validation is not disabled, no trust stores or keychain settings are changed by this repair, and no build/test tools are restored to PATH. The scanner's normal certificate-loading code may invoke the system utility when the operator reruns it. The regression now explicitly requires `security` to resolve to `/usr/bin/security`, uses an independently specified eight-entry allowlist, and checks a missing/non-executable security utility fails clearly and cleans the temporary directory. These scanner-path tests target the supported macOS environment.

The post-repair suite on macOS ran **435 tests: 432 passed, 3 external/live checks skipped, no failures/errors**. Cached Ruff and diff checks passed. An independent read-only review found no blockers. No actual scan or keychain read was performed by the repair validation. The operator's retry and real engine outcome remain pending; the startup correction is not a claim of end-to-end scan success.
