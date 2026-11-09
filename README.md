# Swift Sonar Kit

A free, reusable bridge from **SwiftLint and static analysis** (with optional real line coverage in advanced project workflows) to SonarQube Community Build. Built from the practical analysis workflow used by HashFleet, without depending on its source tree, device fleet, credentials, or build layout.

**Not SonarSource's commercial Swift analyzer.** This toolkit composes independently licensed tools. It does not unlock paid features or reproduce compiler-aware security analysis. The current server path uses the community-maintained **sonar-apple** plugin to register Swift. Its documented Swift 5 parser support is not a guarantee of full Swift 6 semantics.

## Quick start: one project path

Download or clone this repository locally, then change only the project path in the one-line command below. The launcher prepares supported missing analyzer dependencies in private caches or a project-local environment before running analysis. No manual virtualenv or `pip install` step is required for this workflow. It does not install global tools or upload your project to GitHub/PyPI.

```sh
cd /path/to/swift-sonar-kit
./scan "/path/to/My Project"
```

By default, `./scan PATH` runs **SwiftLint lint and Sonar scanner only**. It does not perform automatic app builds, launch simulators, execute tests, or collect code coverage. The default public run is implemented via a new dedicated analysis runner and analysis configuration (`analysis_runner` and `analysis_config`).

Source discovery reads candidate source and test scope from any reviewed recipe (`swift-sonar.json` or `Tools/swift-sonar-project.json`) or existing properties file, but it never executes test commands, enumerates tests, requires test manifests, validates or imports existing coverage or native reports, archives build directories, or creates a coverage virtual environment.

For a read-only discovery plan without tool probes, downloads, file writes, or network requests, append `--plan-only` (this performs read-only source and configuration discovery against configured pins only). For local lint and source-only scanner configuration without contacting or starting a server, append `--prepare-only` (in this default workflow, `--prepare-only` prepares lint and scanner settings only, without any server interaction and without running tests). To use the installed command instead, see [install from this checkout](#install-from-this-checkout).

To download/verify dependencies **without running lint analysis or Sonar**, append `--setup-only`. In the source-only workflow, setup downloads analyzer tools and runtime only (no Python coverage virtual environment from old recipes). Setup results are saved separately to `.swift-sonar/setup-result.json`, leaving existing scan reports unchanged. A normal run performs this setup automatically. `--plan-only` never downloads or installs anything. See [automatic setup and limits](docs/PATH-ONLY.md#automatic-private-setup).

Earlier receipts, including the [November 7 setup-only receipt](docs/ONE-STEP-SETUP-2026-11-07.md) and earlier OrbitMath receipts, record the historical test-driven workflow rather than the current default source-only scan acceptance. The current default source-only workflow has been verified with mocked and synthetic tests, with no full real-server scan acceptance performed yet.

In this source-only default workflow, **SwiftLint findings do not block Sonar**: both SwiftLint and Sonar scanner run. The overall command exits nonzero if SwiftLint reports findings, if the scanner fails, or if the server quality gate fails (individual Sonar issues do not fail the overall command if the quality gate passes and SwiftLint is clean). The flag **`--continue-on-lint`** is retained as a compatibility no-op for the source-only default. To prevent unintended legacy execution semantics, legacy flags such as `--publish-failed-tests`, `--no-archive`, and `--max-test-age` are rejected on the public run command. See [diagnostic continuation](docs/PATH-ONLY.md#continue-to-sonar-with-lint-findings).

Normal terminal output uses readable tables, including **File, Line, Severity, Rule, and Message** for lint findings. The raw local SwiftLint report is written to `.swift-sonar/analysis-lint.json`, converted to `.swift-sonar/analysis-issues.json`, and imported into Sonar via generic external issues (`sonar.externalIssuesReportPaths`). Output displays distinguish between total local lint findings across the repository and deduplicated findings within the selected source/test scope included in the generic report (not a server-visible issue count). To prevent duplicate findings, sensor replays, or stub execution, native plugin analyzer executables (such as SwiftLint, Periphery, compiler sensors, MobSF, or OCLint) are made unavailable in the restricted scanner execution PATH. Every analysis run (full or prepare-only) also saves its complete result to `.swift-sonar/run-result.json`. The saved result and terminal table explicitly report `tests='not-run'`, `coverage='not-collected'`, and an explicit analysis-only mode (discovery plans use `kind`). Stale reports or external coverage artifacts are never imported. Any omitted report properties declared in existing recipes or properties files are displayed, while original recipe and property files remain unchanged. Append `--json` to print that same result as one machine-readable document on stdout. Progress and child-tool output go to stderr. This flag changes presentation only, not execution, failure gates, or publication. `--plan-only` remains read-only and does not create this result file. See [output formats](docs/PATH-ONLY.md#output-formats).

After a completed scan with a successful scanner exit, the terminal prints the **Sonar dashboard URL** and saves it as `dashboard_url` in `.swift-sonar/run-result.json`. In an interactive terminal, the kit opens that project's dashboard in your default browser after saving the result, including when lint findings keep the overall exit nonzero. Sonar's Issues page lists server-side findings. A passing quality gate does not mean there are zero Sonar issues. Append `--no-browser` to disable automatic opening. JSON, piped/non-interactive, CI, plan-only, setup-only, prepare-only, and failed scanner runs never open a browser. `NO_BROWSER` and `JCODE_NO_BROWSER` are also respected. Browser failures do not change the saved scan outcome.

If SwiftLint reports findings in an interactive terminal run, the kit saves the source-only report first, then asks **`Fix automatically correctable SwiftLint issues? [y/N]`**. Nothing is changed unless you explicitly answer `y` or `yes`. Before correction, the kit backs up the exact affected Swift files, including uncommitted edits, under `.swift-sonar/corrections/`. It then runs SwiftLint's built-in corrections (`swiftlint lint --fix`) and a separate full-repository strict-lint recheck. Note that the correction tool is SwiftLint, which runs independently of Sonar. Remaining findings still need manual work. The original audit report and nonzero exit status are not turned into a pass: review the diff and rerun the audit yourself. JSON, piped/non-interactive, CI, and plan-only runs never prompt. Append `--no-fix-prompt` to disable the offer in a terminal. See [optional corrections](docs/PATH-ONLY.md#optional-swiftlint-corrections).

The path-only run reuses candidate scope from reviewed recipes or prepares supported SwiftPM/Xcode layouts. With no selected server URL, it provisions only a kit-owned local Docker server (requiring a running local Docker daemon, and no arbitrary OS installation is performed). Passing `--url URL` or `SONAR_HOST_URL` plus secret-store `SONAR_TOKEN` selects an existing server. `--prepare-only` stops before server requests.

## Existing workflows

**Advanced opt-in project mode:** The separate, explicit project commands (`project-test`, `project-scan`, and `project-run`) remain an advanced opt-in workflow. Project mode preserves an existing reviewed Sonar configuration, with Swift, Python, PHP, JavaScript and TypeScript source/test inventory, native Xcode results, generic coverage, Python coverage and native JavaScript/TypeScript LCOV. It retains its prior test-driven contract, without automatic coverage setup guarantees. It runs whole-repository strict SwiftLint before publication and keeps the plugin as the sole SwiftLint issue importer. It does not silently narrow your project to Swift files.

```sh
# Configure your repository once using docs/PROJECT-MODE.md.
swift-sonar project-run --root "/repos/My App" --config swift-sonar.json --prepare-only
# Inspect .swift-sonar/project-plan.json before publishing the same validated evidence.
swift-sonar project-scan --root "/repos/My App" --config swift-sonar.json
```

`project-test` runs just the configured tests. `project-run` combines testing and scanning. These advanced commands require fresh declared outputs and never delete previous reports. A failed recipe cannot become a successful scan merely because the server gate is green. Native manifests and report validation are stronger than the legacy selected-command receipt, but are still local unsigned evidence, not a guarantee of exhaustive testing. See [project-mode contract](docs/PROJECT-MODE.md).

**Verified HashFleet parity:** the full project workflow was compared against the existing scanner in separate local projects, with matching file measures, coverage, issues and profiles. The kit correctly retained a nonzero result for two app UI failures. See the [November 1 verification report](docs/PROJECT-MODE-VERIFICATION-2026-11-01.md) for the exact scope, recovery steps and remaining limitations.

**Focused mode** retains the original v0.1 commands below. It is intentionally narrower and is not a replacement for a comprehensive existing project pipeline.

## What focused mode does

- Converts SwiftLint JSON into modern Sonar generic external issues, including rules absent from an old plugin catalog.
- Converts one or multiple LCOV files into generic **line** coverage, merging observations with OR without inventing branches or uncovered executable lines.
- Checks server health, exact plugin version and Swift registration before publication.
- Rejects credential-bearing URLs, remote plaintext HTTP and authenticated redirects.
- Runs an explicit test command, binds its successful exit to a Git-visible source fingerprint and freshly generated coverage artifacts, then refuses stale or changed inputs.
- Runs fresh strict lint, prepares separate scanner configuration and waits for the server quality gate. Lint findings still cause a nonzero exit even if the server gate passes.
- Keeps preparation local with `--prepare-only`. `scan`, `project-scan`, and `project-run` without that flag can publish source/analysis to your configured server.

## Requirements

The automatic dependency path supports macOS arm64 and x86_64. It reuses matching installed tools or downloads checksum-pinned SwiftLint **0.65.1** and SonarScanner **8.3.1.16491** into `~/.cache/swift-sonar-kit/tools`. If Python 3.11+ is absent, an existing Python 3.9+ host interpreter can prepare private uv **0.13.7** and CPython **3.12.16** under `~/.cache/swift-sonar-kit/runtime`. Missing native analyzer tools are privately installed. For source-only scans, there is no Xcode or iOS simulator prerequisite, though SwiftLint may need an appropriate installed developer toolchain for parsing Swift files (do not assume zero platform dependencies). Advanced project recipes that use Python coverage require a separately prepared matching environment. The default source-only scan does not create `.swift-sonar/python` or install Python coverage. Reviewed tool pins are retained. Unsupported download pins fail with an explanation rather than silently upgrading.

Git and a running local Docker daemon (for automatic local-server use) are system prerequisites. The kit does not install Xcode or Docker, run sudo/Homebrew, change shell profiles, or guess arbitrary application dependencies. Existing-server runs do not require Docker. Automatic server setup pulls the pinned Community Build image by immutable digest (`sonarqube@sha256:aa7146fd72ef79ea8ca06315ca3121db9bb1c24a83ba5255e8df67b43dee6e04`) with a bounded timeout if missing, verifies digest/version, and downloads/verifies sonar-apple when needed. Focused commands do not gain automatic setup or server administration. The path-only scan may create only its own isolated server after local gates pass, not administer existing servers or change profiles.

See [server setup](docs/SERVER-SETUP.md) for explicit administrator-controlled adoption and duplicate-issue precautions. The [HashFleet-depth adoption plan](docs/REUSABLE-HASHFLEET-PLAN.md) records current parity gaps, optional tool research and the checks required before replacing an existing pipeline.

The initial compatibility target is sonar-apple **0.5.1**, SwiftLint **0.65.1**, SonarScanner **8.3.0**, and Community Build **26.9.0.129388**. These are local compatibility targets, not a claim that every combination is supported. See [validation](docs/VALIDATION.md) for what was actually exercised.

## Install from this checkout

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/swift-sonar --help
```

No runtime Python dependencies. For development, `PYTHONPATH=src python3 -m unittest discover -s tests -v` runs the fixture suite.

## Swift package quick start

Run from the **consumer repository root**, after dependency resolution has stabilized its lockfile:

```sh
# Configure SONAR_TOKEN through your shell/CI secret store. Never commit it.
export SONAR_HOST_URL=http://localhost:9000
swift-sonar doctor
swift-sonar test -- swift test
swift-sonar scan --project-key my-swift-library --source Sources --prepare-only
# Review .swift-sonar/ before explicitly publishing:
swift-sonar scan --project-key my-swift-library --source Sources
```

Add `.swift-sonar/` and `.scannerwork/` to the consumer's .gitignore. Repeat `--source` for multiple non-overlapping production directories. Do not include tests, dependencies or generated code as production sources. Existing sonar-project.properties is not overwritten or inherited by this dedicated scan mode.

## Xcode and coverage

```sh
swift-sonar test -- xcodebuild test -project MyApp.xcodeproj -scheme MyApp \
  -destination 'platform=iOS Simulator,name=YOUR_SIMULATOR' -enableCodeCoverage YES
swift-sonar scan --project-key my-ios-app --source MyApp --prepare-only
```

For line coverage, your checked-in test script should run tests, export **production-only LCOV** via llvm-cov, and stop on every failure. Register the generated file with `swift-sonar test --artifact build/coverage.lcov -- bash Scripts/test-with-coverage.sh`, then pass `--lcov build/coverage.lcov` to scan. Each file must be written during that test command and remain byte-identical. Repeat both flags for multiple reports. Missing, outside-root, stale and out-of-bounds source records are errors, never silently discarded. See [recipes](examples/README.md).

Offline conversion, with no claims about freshness:

```sh
swift-sonar issues --input build/swiftlint.json --output build/sonar-issues.json
swift-sonar coverage --input build/coverage.lcov --output build/sonar-coverage.xml
```

These files can also feed an existing advanced pipeline using sonar.externalIssuesReportPaths and sonar.coverageReportPaths. Do not replace your existing comprehensive multi-language scanner configuration with the focused scan command without reviewing the differences.

## Important limits

- Default `./scan PATH` runs SwiftLint lint and Sonar scanner only. It does not run tests, build applications, or collect coverage. Advanced project testing, coverage collection, and xcresult validation are separate, explicit opt-in workflows (`project-test`, `project-run`).
- The linting and correction tool used is SwiftLint, which operates independently of Sonar.
- A legacy receipt attests only the **selected command and its exit status**. Project receipts additionally bind the reviewed recipe, toolchain, report hashes and declared native census. A trusted script and incomplete manifest can still omit tests. Neither mode guarantees exhaustive testing or 100% coverage.
- Focused mode does not import xcresult test identities. Project mode validates declared native result bundles against separate expected manifests and preserves native plugin import. Sonar's aggregate can differ from the independent parameterized-test census. Supported reader formats and remaining acceptance limits are documented in the project-mode guide.
- SwiftLint lint is not SwiftLint analyze. Compiler-log analyzer rules, Periphery, dependency scanning and advanced security analyzers are future separate integrations.
- Project mode keeps native SwiftLint import only. Focused mode adds generic issues, while sonar-apple also runs its own SwiftLint sensor. To avoid duplicate native/external issues, use a dedicated project profile with the plugin's native SwiftLint rules disabled when adopting generic issues. This toolkit **does not modify profiles automatically**. Do not interpret combined issue totals as unique findings without checking the profile.
- Receipts are local unsigned evidence, not an adversarial attestation. Git-ignored inputs are outside the fingerprint. Keep build-affecting inputs Git-visible. Submodules and escaping symlinks are rejected. Run one test/scan job per checkout. Concurrent jobs sharing a checkout or report directory are not supported.
- A passing server gate depends on the project's gate configuration. Nonzero test/lint failures are enforced separately.
- Python adapters are portable. Full server/plugin behavior still needs validation on each supported Sonar/Xcode/SwiftLint combination.

## License and contributions

GPL-3.0-only, see LICENSE. Third-party tools keep their own licenses and are not bundled. See [third-party notices](THIRD_PARTY_NOTICES.md), [security](SECURITY.md), and [contributing](CONTRIBUTING.md). This repository is not affiliated with SonarSource. HashFleet is a consumer, not a runtime dependency.
