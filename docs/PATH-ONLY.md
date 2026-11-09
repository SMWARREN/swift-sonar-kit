# Path-only workflow

Download or clone this kit locally, then change the consumer path in this one-line command:

```sh
cd /path/to/swift-sonar-kit
./scan "/path/to/My Project"
```

The checkout launcher uses Python's standard library directly and prepares supported dependencies automatically. No manual virtual environment or `pip install` step is required.

By default, `./scan PATH` runs **SwiftLint lint and Sonar scanner only**. It does not perform automatic app builds, launch simulators, execute tests, or collect code coverage. The default public run is implemented via a new dedicated analysis runner and analysis configuration (`analysis_runner` and `analysis_config`).

Legacy explicit workflows (`project-test`, `project-scan`, and `project-run`) are retained untouched and are accurately labeled as an advanced opt-in separate workflow. An installed command remains available as an alternative.

## Requirements and discovery

Source discovery reads candidate source and test scope from any reviewed recipe (`swift-sonar.json` or `Tools/swift-sonar-project.json`) or existing properties file. However, this source-only scan never executes test commands, enumerates tests, requires test manifests, validates or imports existing coverage or native reports, archives build directories, or creates a coverage virtual environment.

The runtime requires Python 3.11+. The checkout launcher can bootstrap private Python (via uv 0.13.7 and CPython 3.12.16) when only a Python 3.9+ host interpreter is present. Supported tool downloads target macOS arm64/x86_64. Missing native analyzer tools (SwiftLint 0.65.1 and SonarScanner 8.3.1.16491) are downloaded into private cache (`~/.cache/swift-sonar-kit/tools`).

There is no Xcode or iOS simulator prerequisite for this source-only scan. However, SwiftLint may require an appropriate installed developer toolchain (such as Xcode Command Line Tools) for parsing Swift files, so this workflow does not claim zero platform dependencies. Automatic local server setup additionally needs a running local Docker daemon. The kit does not install Docker or arbitrary operating system packages. If the pinned Community Build image is absent, the kit performs a bounded pull by immutable digest (`sonarqube@sha256:aa7146fd72ef79ea8ca06315ca3121db9bb1c24a83ba5255e8df67b43dee6e04`) and verifies the returned repository digest and Sonar version. Plugin download for sonar-apple is also bounded and verified. Explicitly selected existing-server runs (`--url` or `SONAR_HOST_URL`) do not require Docker.

To inspect without writes or downloads, run `./scan "/path/to/My Project" --plan-only` with an already available Python 3.11+ interpreter.

`--plan-only` performs read-only discovery against configured tool pins. It does not probe tools, write files, download tools, run tests, execute linting, or contact/start a server. It reports detected project kind, candidate scope, configured tool pins, and any reasons for review. This is discovery, not proof that an arbitrary project builds or that the full workflow succeeds. To install the Python package in a virtual environment instead, see [install from this checkout](../README.md#install-from-this-checkout).

## Automatic private setup

A normal path-only run first discovers project layout, then prepares supported analyzer dependencies before running analysis. Matching installed tools are reused. Missing supported tools are downloaded over HTTPS and checked against frozen official-artifact SHA256 pins before execution:

- SwiftLint 0.65.1 and SonarScanner 8.3.1.16491: `~/.cache/swift-sonar-kit/tools`.
- When needed, uv 0.13.7 and managed CPython 3.12.16: `~/.cache/swift-sonar-kit/runtime`.

For the source-only workflow, setup installs analyzer tools and runtime only. No Python coverage virtual environment is created from old recipes (recipes declaring `python-coverage` do not set up `.swift-sonar/python` in this source-only path, which remains part of the advanced opt-in project mode).

Unknown download versions fail rather than replace a reviewed pin. No global package install, Homebrew, sudo, shell profile edit, or automatic Xcode/Docker installation occurs. Network access, disk space, and provider availability are required on first setup. A successful setup is not a successful scan.

```sh
# Download and verify dependencies only. No lint analysis, tests, archival or server.
./scan "/path/to/My Project" --setup-only
```

`--setup-only --json` emits one setup result and saves it privately at `.swift-sonar/setup-result.json`. The table mode saves the same result. Existing `run-result.json`, `run-summary.json`, and build reports are not replaced or archived. Setup-only cannot be combined with plan-only or prepare-only. It does create cache/environment/lock files. In contrast, `--plan-only` does not write or download anything. Runtime bootstrap is also suppressed for launcher help and plan-only when no suitable Python is already installed.

Earlier receipts (including the [November 7 setup-only receipt](ONE-STEP-SETUP-2026-11-07.md)) record the historical test-driven workflow. The current default source-only workflow has been verified with mocked and synthetic tests, with no full real-server scan acceptance performed yet. See the [version 0.4.0 verification and limits](ANALYSIS-ONLY-2026-11-07.md).

## Local preparation or full run

```sh
# Local lint and source-only scanner settings without server requests.
./scan "/path/to/My Project" --prepare-only

# Full run: lint and Sonar scanner against local Docker server (or existing server).
./scan "/path/to/My Project"
```

The workflow discovers source and test directories from a recognized recipe (`swift-sonar.json` or `Tools/swift-sonar-project.json`) or prepares candidate scope for supported SwiftPM/Xcode layouts. A pre-existing `sonar-project.properties` without a bound recipe is not overwritten or silently narrowed.

`./scan PATH --prepare-only` runs SwiftLint lint and generates source-only scanner configuration without contacting or starting a server. It does not execute app builds, simulator runs, tests, or coverage collection.

In a full run (`./scan PATH`), the launcher runs SwiftLint lint and Sonar scanner against either a kit-owned local Docker server or an existing server if `--url` or `SONAR_HOST_URL` is configured.

**SwiftLint findings do not block Sonar by default:** both SwiftLint and Sonar scanner run. The overall scan command exits nonzero if SwiftLint reports findings, if the scanner fails, or if the server quality gate fails (individual Sonar issues do not fail the overall command if the quality gate passes and SwiftLint is clean).

To prevent implicit legacy test or archival semantics, legacy options such as `--publish-failed-tests`, `--no-archive`, and `--max-test-age` are rejected on the public run command.

Each full or prepare-only run saves a complete result to `.swift-sonar/run-result.json`, including the actual exit status and full lint findings when available. The raw local SwiftLint report is written to `.swift-sonar/analysis-lint.json`, converted to `.swift-sonar/analysis-issues.json`, and imported into Sonar via generic external issues (`sonar.externalIssuesReportPaths`). Output displays distinguish between total local lint findings across the repository and deduplicated findings within the selected source/test scope included in the generic report (not a count of issues confirmed visible on the server). To prevent duplicate findings, sensor replays, or stub execution, native plugin analyzer executables (such as SwiftLint, Periphery, compiler sensors, MobSF, or OCLint) are made unavailable in the restricted scanner execution PATH.

The terminal table and `run-result.json` explicitly record `tests='not-run'`, `coverage='not-collected'`, and kind/mode as analysis-only. Stale reports or external test/coverage artifacts are not imported. Any omitted report properties declared in existing recipes or properties files are shown in the summary, while original recipe or properties files remain untouched.

Earlier receipts (such as the OrbitMath acceptance receipt and historical consumer receipts) reflect the earlier test-driven workflow, not acceptance of the current default source-only scan. The current default source-only workflow has been verified with mocked and synthetic tests, with no full real-server scan acceptance performed yet. See the [version 0.4.0 verification and limits](ANALYSIS-ONLY-2026-11-07.md).

## Continue to Sonar with lint findings

```sh
./scan "/path/to/My Project" --continue-on-lint
```

In the default source-only workflow, SwiftLint findings do not block Sonar by default: both SwiftLint lint and Sonar scanner execute during a standard run. The overall command exits nonzero if lint reported findings, if the scanner failed, or if the server quality gate failed (individual Sonar issues do not fail the overall command if the quality gate passes and lint is clean).

Because running both lint and Sonar is now the default behavior, the `--continue-on-lint` flag is retained as a compatibility no-op for the source-only default.

With or without this flag, valid SwiftLint findings do not prevent Sonar submission, but they do cause the overall command to return a nonzero exit status. Raw lint reports are preserved at `.swift-sonar/analysis-lint.json` and in `.swift-sonar/run-result.json`.

Adding `--prepare-only` performs local lint and configuration preparation only, stopping before server requests and returning nonzero when lint reports findings. Adding `--plan-only` remains read-only discovery.

## Output formats

The completion table prints a **Sonar dashboard** URL, also saved as `dashboard_url` alongside `server_url` in the result JSON. Local SwiftLint findings are listed in the terminal. Server-side findings are available on the project's Sonar **Issues** page, and a passing quality gate is not a zero-issues guarantee. After a successful scanner exit, an interactive terminal run opens the dashboard in your default browser after saving its report. This also happens if lint findings keep the overall command nonzero. Use `--no-browser` to opt out. JSON, piped/non-interactive, CI, plan-only, setup-only, prepare-only, and failed scanner runs never open the browser. `NO_BROWSER` and `JCODE_NO_BROWSER` suppress opening too. Failure to launch a browser leaves the original saved outcome and exit code unchanged.

Without an output flag, the launcher displays project facts and findings as readable tables. Lint columns are **File | Line | Severity | Rule | Message**, with project-relative paths and wrapped text. Empty lint results are explicitly labeled. Display is bounded to 200 findings and 4096 characters per cell, and omitted rows and truncated cells are marked. The output table and `.swift-sonar/run-result.json` explicitly show analysis-only execution, with `tests='not-run'` and `coverage='not-collected'`. Any omitted report properties from existing recipes or properties files are displayed, while original files are left untouched. Stale reports from previous runs or external tools are never imported.

```sh
# Readable terminal output, using the normal workflow.
./scan "/path/to/My Project"

# One JSON document on stdout, suitable for scripts or saving to a file.
./scan "/path/to/My Project" --json
```

`--json` prints the same complete result saved at `.swift-sonar/run-result.json`, including workflow facts, analysis-only mode, the actual exit code, expected failures, and the complete lint `findings` array. Progress and child-tool output are routed to stderr so they do not corrupt stdout. Saving requires a valid, writable, safe project output path. If the result cannot be saved, the command reports an error and does not claim that it was saved. The result is written atomically with private file permissions. `--plan-only` uses the same table/JSON presentation choice for discovery but remains read-only and does not write `run-result.json`. `summary PATH` renders the separate saved workflow summary as a table, while `summary PATH --json` retains the saved-summary JSON format.

Formatting does not run an extra scan, auto-correct findings, alter raw reports, or make `--json` a dry run. The separate, explicitly consented correction step below is available only in an interactive terminal. Use `--plan-only` or `--prepare-only` for their documented execution limits.

## Optional SwiftLint corrections

When SwiftLint reports findings in an interactive human-readable terminal run, the kit saves the source-only report to `.swift-sonar/run-result.json` first, then offers:

```text
Fix automatically correctable SwiftLint issues? [y/N]
```

Enter `y` or `yes` to consent. Enter, `n`, any other response, EOF, or interrupting the prompt means no source changes. There is no automatic yes flag. The offer is suppressed for `--json`, `--plan-only`, pipes/non-TTY sessions, CI, and `--no-fix-prompt`. If the original report cannot be safely saved, corrections are not offered.

Note that the underlying tool is SwiftLint (`swiftlint lint --fix`), an independent linter that operates outside of Sonar.

After explicit consent, the kit:

1. Acquires the same checkout locks used by scans and checks the previously verified SwiftLint executable's hash.
2. Validates the unique in-project `.swift` files named by the current findings. Unsafe, symlinked, hard-linked, nonregular, outside-root, or oversized inputs are refused rather than silently skipped.
3. Saves private, exact pre-correction copies and a manifest in a unique `.swift-sonar/corrections/<attempt>/` subdirectory before invoking SwiftLint. Backup leaves use `.swift.bak` rather than `.swift`, so they do not become Swift input to later lint runs. This backs up current contents, including uncommitted edits, not an older Git revision.
4. Runs `swiftlint lint --fix` with only those backed-up files as script inputs. SwiftLint may apply enabled corrections throughout these files, not just on the displayed lines. It does not rewrite lint configuration or disable rules to hide findings.
5. Runs a **separate full-repository** strict lint with JSON reporting, displays the remaining findings, and saves a separate correction result at `.swift-sonar/corrections/<attempt>/result.json`. Correcting itself is not a lint verification pass.

SwiftLint only corrects rules that support correction. Other naming, complexity, or design findings require manual review. Review `git diff` yourself, address remaining findings, and rerun the original audit command. The correction step does not run project tests, resume the scan, contact Sonar, start a server, commit, reset, or restore source automatically.

The original `run-result.json`, raw lint reports, and workflow summaries are not rewritten by this step. The original nonzero exit status remains nonzero even if the separate lint recheck is clean. A new audit is required to establish a passing result for changed sources.

## Existing server selection

To deliberately use an existing server, pass `--url https://sonar.example` or set `SONAR_HOST_URL`, and provide `SONAR_TOKEN` from a shell/CI secret store. URL selection suppresses automatic local-server creation. Do not pass the token as an argument, write it into project properties, or commit it.

Existing focused and project commands remain available separately. `project-test`, `project-scan`, `project-run`, `test`, and `scan` retain their explicit test/evidence contracts without automatic coverage setup guarantees. `doctor`, `issues`, and `coverage` retain their existing inspection behavior. The default source-only run does not invoke the test-driven workflows.

## Privacy, uploads, and licensing

The kit is downloaded locally. It does not upload the kit or consumer repository to public GitHub/PyPI. A Sonar scan sends source/analysis data to the selected server, so choose a trusted server and review its access, retention, and publication settings. The local server is opt-in through the full scan command and only manages matching kit-owned resources. See [server setup](SERVER-SETUP.md) for exact boundaries.

The kit is GPL-3.0-only. SwiftLint, SonarScanner, Community Build, and sonar-apple retain their own licenses. The kit is not a paid analyzer, does not grant commercial features, and does not perform commercial compiler-aware security analysis. See [third-party notices](../THIRD_PARTY_NOTICES.md).
