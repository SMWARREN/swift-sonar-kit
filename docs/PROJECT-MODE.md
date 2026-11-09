# Project mode: adopting a complete audit

This is an advanced, explicitly selected test-driven workflow. The default `./scan PATH` instead runs SwiftLint and Sonar source analysis only, without builds, simulators, tests, coverage or native manifests. See [the default workflow](PATH-ONLY.md).

Project mode runs a consumer-owned test recipe, validates its declared artifacts, runs strict lint, and preserves the consumer's reviewed Sonar configuration. It is the comprehensive workflow, not a Swift-only scan. The [HashFleet verification report](PROJECT-MODE-VERIFICATION-2026-11-01.md) records an actual isolated comparison, including remaining app failures.

## Configure a consumer

1. Install the kit and its required host tools. Add `.swift-sonar/`, scanner work directories, and generated reports to the consumer's `.gitignore`.
2. Keep production/test scope in the consumer's reviewed `sonar-project.properties`. Project mode supports a restricted set of properties and rejects unknown or protected overrides rather than silently ignoring them.
3. Check in a script that runs the intended tests and exports the real reports. It must retain nonzero failures, even when continuing independent checks. Declare its argv, not an unreviewed shell string.
4. Create `swift-sonar.json` using the [standalone example](../examples/project-mode/swift-sonar.json). Replace its Python fixture recipe and file artifact with the consumer's actual recipe and typed reports. Do not label an ordinary file as coverage.
5. For each `xcresult` artifact, provide `expectedTests`, a checked-in JSON array of exact native identities independently enumerated from the intended test selection. A merged bundle needs its own reconciled manifest. Artifact kinds are `xcresult`, `generic-coverage`, `python-coverage`, `javascript-coverage`, and `file`.
6. Point supported `scannerProperties` report overrides at declared artifacts. Preserve existing language scope and quality-profile ownership. Do not include credentials in the JSON or properties files.

A native artifact declaration looks like this:

```json
{
  "kind": "xcresult",
  "path": "build/audit/native.xcresult",
  "expectedTests": "Tools/expected-native-tests.json"
}
```

Run from any directory by supplying the consumer root:

```sh
swift-sonar project-run --root "/repos/My App" --config swift-sonar.json --prepare-only
# Inspect .swift-sonar/project-plan.json and the retained test evidence.
# This next command publishes to the explicitly configured trusted server.
swift-sonar project-scan --root "/repos/My App" --config swift-sonar.json
```

`project-test` runs only the recipe and evidence validation. `project-run` combines testing and scanning. All declared output paths must be fresh at test start. Preserve old reports elsewhere before another run. Neither command deletes previous reports. Build-affecting configuration and scripts must remain Git-visible for the source fingerprint.

## Standalone integration example

The small [second-repository fixture](../examples/project-mode/) demonstrates a checked-in scanner properties baseline, a versioned recipe, real Python test execution, and file-level evidence. Its file artifact is deliberately **not** presented as coverage. The fixture does not demonstrate native Xcode result import or HashFleet parity.

## Requirements and boundary

Run on a macOS host with Python 3.12, Git, SwiftLint 0.65.1, SonarScanner CLI 8.3.0, and the compatible Sonar Apple plugin available to the scanner. Xcode builds/tests and SwiftLint run on the macOS host. Docker may host the SonarQube server, but Docker/Linux cannot run Xcode or replace the host-side Apple toolchain. This Python-only fixture's test command does not itself need Xcode. Configured `xcresult` artifacts additionally require Xcode, `xcode-select`, `xcrun`, Swift, and a supported result schema. The reader currently accepts only the fixture-tested Xcode result schema 0.4.0.

From the repository root, the one-command acceptance run (after installing the toolkit in the Python 3.12 environment) is:

```sh
PYTHONPATH=src python3.12 -m unittest discover -s tests -p 'test_project_example.py' -v
```

The test copies this fixture to a temporary Git repository whose path contains spaces, uses explicitly mocked tool-version probes and mocked SwiftLint output, then runs the configured Python test command for real and exercises offline preparation. It does not claim that those mocked host-tool checks replace running real SwiftLint/scanner probes.

For a manually prepared checkout with the required tools installed:

```sh
swift-sonar project-run --root "$PWD" --prepare-only
```

The runner gives build/lint child processes a strict environment allowlist, not the caller's entire environment. It supports standard execution/toolchain variables (including `PATH`, `HOME`, locale, `DEVELOPER_DIR`, `SDKROOT`, `TOOLCHAINS`, Swift/compiler flags, Python environment variables, and `CI`) and these project-specific overrides: `HF_IOS_SCHEME`, `HF_MAC_SCHEME`, `HF_IOS_DESTINATION`, `HF_UNIT_DESTINATION`, and `HF_MAC_DESTINATION`. Scanner credentials and unrelated `SONAR_*` variables are not passed to build/lint tools. The runner intentionally records the effective build-environment context in evidence.

`--publish-failed-tests` is an explicit diagnostic override. It retains complete failed native test results so they can be inspected/published for diagnosis, but `project-test`/`project-run` still return nonzero even if the Sonar quality gate passes. Without this flag, failed tests do not produce a successful receipt and project-scan rejects them. Missing, stale, partial, or mismatched reports remain rejected; the override does not waive completeness or freshness checks. This flag alone does not waive strict lint. The separate `--continue-on-lint` option on `project-run` and `project-scan` permits submission with valid lint findings while keeping preparation and the overall audit nonzero. Scanner success is recorded independently; it is not audit success. The option does not waive test receipt, freshness, scope, toolchain, or malformed-evidence checks.

The command requires the checkout root. `--config` defaults to `swift-sonar.json`, resolved relative to `--root`. Preparation is local-only, but still runs the fresh strict SwiftLint gate and validates the configured artifact. It does not contact a SonarQube server. `project-scan` without `--prepare-only` publishes, so do not omit that flag unless publication is intended and a trusted server/token are configured.

## Evidence and limits

### Native JavaScript/TypeScript and PHP coverage

Project inventory includes `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, and `.php`
alongside Swift and Python, using the existing reviewed source/test scope. PHP
can use a declared `generic-coverage` report with `sonar.coverageReportPaths`.
Declare each native LCOV file as `javascript-coverage` and list its exact path in
`sonar.javascript.lcov.reportPaths`. Multiple comma-separated paths are allowed;
LCOV path globs are not. Every declared coverage artifact must be imported.

The wrapper validates report paths, current production inventory, observations,
digests and freshness. Native LCOV bytes are passed unchanged to the scanner,
including observed branch data, rather than converted to line-only XML. This
does not generate coverage, infer executable lines, or make missing observations
covered. Tests, outside-root files and malformed reports are rejected.

This report support does not provision a hosted service, publish the toolkit,
or activate a consumer's GitHub gate. Hosted integration still needs an approved
reachable server, a pinned toolkit revision, suitable credential isolation, and
verification that scanner/test/gate failures block the required check. Local
server issue dispositions do not automatically transfer to a new server.

`project-test` executes the argv recipe and validates newly created declared artifacts. This sample records `build/test-summary.txt` as `file` evidence solely to prove a real test process produced a fresh file. It makes no coverage claim. It does not make or supply an `.xcresult` archive. A project importing coverage or native test/coverage data must declare every report path and use the matching typed artifact (`generic-coverage`, `python-coverage`, `javascript-coverage`, or `xcresult`) plus reviewed scanner properties. Native test manifests are JSON arrays of exact test identity strings in the reader's supported format, for example `Tools/expected-tests.json`; do not substitute aggregate counts or invented Swift identities.

Xcode 27 may create `database.sqlite3` inside a result bundle on its first modern-reader access. Project testing primes the official native reader before freezing the end time and artifact hashes. The generated cache is included in those hashes, never excluded, and later mutation still blocks scanning.

Evidence is local and unsigned. It binds the declared artifact, recipe, toolchain, and Git-visible inputs for a bounded time; it is not adversarial attestation and does not prove that a test script ran every intended test. The wrapper/script remains reviewed repository code.

`lint.import` must be `native`. SwiftLint runs over the project with `--strict` and JSON reporting. Findings block publication by default. With explicit `--continue-on-lint`, the same findings are retained and publication may proceed diagnostically, but the overall result still fails. This is an independent strict check, not generated generic SwiftLint issues. Keep scanner scope and language settings in `sonar-project.properties`, the authoritative baseline. Do not add a generated Swift-only inclusion list. The scanner baseline and recipe must be reviewed together.

The small fixture is not evidence of full HashFleet parity. The separate [real verification report](PROJECT-MODE-VERIFICATION-2026-11-01.md) supplies that comparison and explicitly retains two app UI failures. Project mode does not claim to fix or bypass them.
