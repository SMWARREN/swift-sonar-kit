# HashFleet vs swift-sonar-kit parity review (read-only, static)

Scope read: hashfleet Tools/sonar-scan.sh, sonar-project.properties, Tools/swift-sonar-kit.sh; kit cli.py, receipts.py, reports.py, server.py, docs/REUSABLE-HASHFLEET-PLAN.md.
No scans, Docker, Sonar or device access. Not read: Tools/sonar_swift_preflight.py (called by native script, out of scope), .swiftlint.yml, VALIDATION.md. Equivalence of full scans is NOT established by this review.

## Top gaps (most consequential first)

1. Scope and multi-language loss (cli.py `_sources`, `_scan` properties dict)
   - Native sonar-project.properties: sonar.sources=App,Shared,Sources,Widgets,MacMenuBar,MacApp,NotificationService,PushProvider,Tools,Scripts; sonar.tests=AppTests,MacAppTests,Tests,UITests,Tools,Scripts/tests; sonar.test.inclusions (Swift + Tools/test_*.py + Scripts/tests/test_*.py); sonar.exclusions; sonar.python.version=3.14; projectName/version.
   - Kit `_scan` generates its own properties: only user `--source` dirs, `sonar.inclusions=**/*.swift`, no sonar.tests, no test inclusions, no exclusions, no python version, no name/version. `_sources` also raises if a dir has no .swift files, so Tools/ and Scripts/ (Python) cannot be selected, and `sonar.inclusions` would drop Python anyway.
   - Consequence: Swift test files inside source dirs are counted as production code (inflating issues, distorting coverage denominator), Python/tooling analysis disappears, generated/build/DerivedData files are not excluded, and sonar.projectKey is caller-supplied (a wrong key would write to a different or new dashboard project and break new-code history).

2. Native Swift tests/coverage not reproduced (receipts.py, cli.py `_scan`, native script SONAR_RESULT_BUNDLE)
   - Native: optional `SONAR_RESULT_BUNDLE` becomes `-Dsonar.apple.resultBundlePath=<project-relative>`; `SONAR_REQUIRE_TESTS=1` makes it mandatory; without it the script warns that tests/coverage are unverified.
   - Kit: no xcresult import at all (plan table confirms). Coverage is only LCOV to generic line XML (`lcov_report`, line OR-merge, branch records ignored). `receipts.verify` is called unconditionally in `_scan`, so scan hard-fails without a `swift-sonar test` receipt, a different contract than native optional/require flag, yet the receipt is "selected-command-only", not a test census (`scope` field). It binds artifact hashes, not parameterized test identities or Xcode pass/fail counts.
   - Plan notes the SwiftPM converter scope mismatch: original `Sources/**` vs kit retained 254 files including tests/deps. Executable-line denominator is unproven.
   - Also `receipts.run_test` requires each artifact to satisfy `path.is_file()`, so an xcresult directory can never be registered as test evidence.

3. SwiftLint strictness/scope and gate ordering (cli.py `_scan` vs sonar-scan.sh lines ~133-142)
   - Native runs `swiftlint lint --quiet --strict` over the whole repo (no path), exits 1 BEFORE publishing on any finding ("No scan was published"), then sonar-scanner runs and the sonar-apple plugin also imports SwiftLint itself.
   - Kit runs lint per selected source dir only (so files in the config's other scope, e.g. Tools/ Swift, Package manifests, tests dirs, are unlinted), then PUBLISHES and afterwards returns 1 if findings exist. Behavior differs: a dashboard analysis with failing lint is created. `--prepare-only` returns 1 on findings, which is closer to native. Return-code handling accepts only 0 or 2 (strict violations), good; but native treats any nonzero as failure.
   - Unverified: whether explicit path args honor `.swiftlint.yml` `excluded`/`included` identically to a bare repo run, and whether strict output severities are all "Error" (reports.py maps Error to MAJOR/HIGH, Warning to MINOR/LOW, so rule severity may differ from the native plugin catalog).
   - reports.py raises on any finding in a deleted/nonexistent file or out-of-range line, so a stale-config situation aborts instead of degrading; acceptable but a behavior difference.

4. Issue deduplication and profile/gate behavior (cli.py properties; server.py `preflight`)
   - Kit sets `sonar.externalIssuesReportPaths` and never sets anything that disables the sonar-apple plugin's own SwiftLint sensor. With the plugin installed (preflight requires it, key `sonarapple` version 0.5.1), the same violation can appear as native rule issue plus external `swiftlint:<rule>` issue. Plan itself says duplicates were observed in v0.1 fixtures. No dedup strategy, mode flag, or rule-key reconciliation exists in code. `reports.py` dedups only within the external report (key rule,path,line,message).
   - Native quality profile and gate are untouched, but the kit never verifies profile/gate for the target project: `preflight` checks status UP, plugin version, swift language only. `sonar.qualitygate.wait=true` is set in both, timeout 300 explicit in kit. No check that the project uses the expected profile, so external-issue rules (engine swiftlint, not in the profile) are not subject to profile activation and may or may not feed the gate the same way.
   - Kit strips all `SONAR_*` env then re-adds only HOST_URL/TOKEN, dropping native knobs (SONAR_RESULT_BUNDLE etc.). URL env differs: native SONAR_URL, kit SONAR_HOST_URL default.
   - Kit's `scan-summary.json` is written after scanner run; if the scanner fails mid-run no summary but exit 1; fine.

5. Adapter and operational contract gaps (Tools/swift-sonar-kit.sh, cli.py parser)
   - Adapter is a thin passthrough (`exec python -m swift_sonar_kit "$action" --root "$ROOT" "$@"`). It carries no HashFleet config, so `scan` requires `--project-key`, repeated `--source`, `--lcov` each time. Nothing maps sonar-project.properties or native env. Plan item "checked-in schema-versioned configuration" is design only.
   - No server bootstrap parity (docker run, plugin SHA256 verification, existing-container refusal). Kit is intentionally preflight-only, which is fine for safety but means a fresh environment cannot be prepared by it.
   - Native also runs Tools/sonar_swift_preflight.py (unreviewed here) which may enforce checks the kit lacks.
   - Receipt `snapshot` requires an exact Git root and raises on any symlink/submodule among Git-visible files, so repos with submodules or tracked links fail closed (safe, but a likely adoption blocker). `_sources` rejects any symlink under selected dirs and dot-prefixed dirs.

## Minimum parity contract (before the kit may be called equivalent or replace native)

1. Config reuse: kit reads (or is generated from) the existing sonar-project.properties verbatim for projectKey/Name/Version, sources, tests, test.inclusions, exclusions, python.version. Reject only credentials/host/gate-bypass keys. Multi-language source dirs (Tools, Scripts, Python tests) allowed; drop the Swift-only `sonar.inclusions` and the "must contain .swift" rule. Fixture: classified file inventory (source vs test vs excluded, per language) identical to a native `sonar-scanner` dry-run inventory on the same revision.
2. Tests/coverage: support `SONAR_RESULT_BUNDLE`-equivalent (xcresult dir, project-relative, deterministic directory hash) and `SONAR_REQUIRE_TESTS=1` semantics (optional by default with the same "unverified" warning, mandatory when set). Pass `sonar.apple.resultBundlePath` identically. Keep generic LCOV only as an additive, labeled path. Check: equal executable-line observations and denominator per intended scope, and a failing-test fixture that fails both routes.
3. Lint: run `swiftlint lint --quiet --strict` over the same scope as native (whole repo, no path restriction) as a pre-publish blocking gate that exits before sonar-scanner on any finding; keep the per-finding validation as an additive report. Check: nonzero-finding fixture makes both routes refuse to publish; both honor `.swiftlint.yml` excluded/included.
4. Dedup: default compatibility mode keeps native plugin import and does NOT set `sonar.externalIssuesReportPaths`; generic-external mode is opt-in, requires an administrator-reviewed profile where the plugin lint sensor is disabled, and reports unique-issue counts. Check: isolated project with one known violation yields exactly one issue in each mode (query /api/issues/search on an isolated key only).
5. Gate/profile: preflight asserts server UP, plugin version, swift (and python) language, and, for the target key, expected quality profile name and gate via read-only API; `sonar.qualitygate.wait=true` with explicit timeout; scanner exit nonzero fails the run; no gate-bypass property accepted. Existing server/volume never touched.
6. Acceptance evidence (from plan, still unmet): frozen revision and tool versions, compare isolated project keys (never the `hashfleet` production key), compare issue tuples (engine, rule, path, line, severity), file inventory, coverage lines, test identities; include paths with spaces, multiple roots, test-only files, excluded trees; run after the final code edit. Until then report "not proven equivalent".

## Evidence caveats
- All findings are from static reading. Nothing here proves runtime behavior of sonar-apple (whether it imports SwiftLint when external report present) beyond the plan's own v0.1 fixture note.
- The plan's reported coordinator checks (byte-identical coverage XML on same LCOV, zero-finding lint) are not reproduced here.
