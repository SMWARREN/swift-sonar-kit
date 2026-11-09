# Validation record: 2026-10-31

## Scope and environment

Local macOS ARM64 validation of v0.1.0. Python 3.12, Apple Swift 6.4 (swiftlang-6.4.0.34.1), SwiftLint 0.65.1, SonarScanner 8.3.0.16463, SonarQube Community Build 26.9.0.129388 and sonar-apple 0.5.1. This is an observed combination, not an exhaustive compatibility matrix. The sample uses simple Swift syntax and does not prove full Swift 6 parser support in the community plugin.

## Acceptance evidence

| Requirement | Observed check | Result |
| --- | --- | --- |
| Report conversion | 10 adapter tests for issue schema, severity, deterministic merging, path containment, malformed input and line bounds | Passed |
| Test evidence | 10 receipt tests for selected-command scope, source/artifact freshness, failed reruns, invalid schema, token isolation and symlinks | Passed |
| CLI orchestration | 12 tests using independent fake executables, including scanner failure, lint failure despite a green gate, source changes and stale-summary removal | Passed |
| Server preflight | 10 tests for URLs, tokens, schemas, exact plugin matching, response limits and a real local redirect server | Passed |
| Python fixture total | unittest discovery | 42 passed, zero failures |
| Distribution | Built wheel without runtime dependencies, installed in isolated virtual environment, ran entry-point help/version | Passed |
| Real Swift recipe | Separate dependency-free sample, three XCTest cases, fresh llvm-cov LCOV, receipt binding and strict SwiftLint | Passed |
| Live clean scan | Isolated swift-sonar-kit-validation project, external report import, generic coverage import and awaited gate | Passed |
| Live clean metrics | One production file, 3 executable line observations, 0 uncovered, 0 issues, gate OK | Verified through server API |
| Live nonempty scan | Separate deliberately faulty sample, one trailing-whitespace finding, imported external_swiftlint rule at line 2 | Verified through server API |
| Failure propagation | Deliberately faulty live project failed server gate and toolkit returned exit 1 | Passed as expected |
| HashFleet interoperability | Existing real strict-lint JSON converted through both CLI and optional consumer wrapper | Passed |
| HashFleet coverage fidelity | Production subset exactly matches existing export: 48 files and 4,914 line observations | Passed |
| Consumer isolation | Help, conversion, missing-toolkit error and shell syntax checks. Existing app and scanner configuration untouched | Passed |

The clean sample has 100% of its **three observed executable lines** covered. That is not 100% toolkit coverage, 100% HashFleet coverage or a guarantee that software has no bugs. Toolkit Python line/branch coverage was not measured. A receipt is selected-command evidence, not an exhaustive native test census.

## Findings retained rather than hidden

- The initial sample recipe assumed a different SwiftPM test-bundle name. Tests passed but coverage export failed. The recipe was corrected to discover the actual single test bundle and the complete test/export/receipt/scan path then passed.
- The first deliberate lint fixture put trailing whitespace in a comment, which this SwiftLint configuration did not flag. Its expected-failure acceptance check failed. Moving the deliberate whitespace to a code line produced a verified finding and the final expected-failure check passed. No analyzer rule or gate was relaxed.
- The default local Swift profile reports the same deliberate finding twice: native SwiftLint:trailing_whitespace and generic external_swiftlint:trailing_whitespace. Both were observed through the server API. The generic import is valid, but adoption requires a dedicated profile without overlapping native SwiftLint rules. The toolkit did not mutate shared profiles to conceal duplication.
- Temporary fixtures generated SCM warnings for an absent initial commit or an uncommitted deliberate change. These do not affect the measured import assertions. Normal consumers should scan committed source for complete blame information.

## Review and publication limitations

Automatic swarm attempts were made without changing operator-selected model pools. Three attempts failed before inference with provider HTTP 400 payload errors. Two workers stalled without deliverables and were stopped. All implementation and acceptance work was completed by the coordinator. No independent swarm review is claimed. Provider failures were reported separately.

The GitHub Actions matrix is configured for macOS and Ubuntu with Python 3.11 and 3.12, but remote CI has not run. Linux Swift coverage, other Xcode projects and other Sonar/plugin versions remain unverified. The repository has not been pushed publicly, and no PyPI package has been published.

Only disposable sample projects were published to the existing local Sonar server. HashFleet source, native test pipeline, existing dashboard and shared quality profiles were not changed by toolkit validation. Private reports, tokens, machine paths and generated results are not part of this repository.
