# Project-mode verification: 2026-11-01

## Result

The reusable project runner reproduced HashFleet's existing scanner output in two separate local Sonar projects using the same fresh audit artifacts. It preserved the full configured scope instead of substituting a Swift-only scan. **This is verified workflow parity, not an all-green app audit or equivalence to SonarSource's commercial Swift analyzer.**

Implementation revisions:

- Swift Sonar Kit: `68f1214` (version 0.2.0).
- HashFleet consumer: `8b3a70b`.
- Evidence captured on November 1, 2026, with the comparison completed about 04:27 UTC.

No application source or acceptance assertions were changed to obtain this comparison. Production HashFleet analysis was unchanged. The two local comparison projects were retained. Nothing was published to GitHub.

## Real isolated scan comparison

| Observation | Existing HashFleet path | Reusable project path |
| --- | --- | --- |
| Local project | `hashfleet-parity-legacy-20261101` | `hashfleet-parity-kit-20261101` |
| Command exit | 0 | **1** |
| Scanner exit | 0 | 0 |
| Coverage | 79.6% | 79.6% |
| Line coverage | 79.7% | 79.7% |
| Branch coverage | 67.3% | 67.3% |
| Lines to cover | 40,170 | 40,170 |
| Uncovered lines | 8,160 | 8,160 |
| Conditions to cover | 318 | 318 |
| Uncovered conditions | 104 | 104 |
| Production file metric | 328 | 328 |
| Non-comment lines | 50,841 | 50,841 |
| Sonar aggregate tests | 733 | 733 |
| Sonar test failures | **2** | **2** |
| Skipped tests | 0 | 0 |
| Issues | 1 | 1 |
| Strict SwiftLint findings | 0 | 0 |

All **591 source/test file components** and their compared measures/classifications matched. Normalized issues, assigned quality profiles, and gate assignment matched. Both server gate responses were `OK`, but their condition arrays were empty. That is not evidence that a meaningful coverage threshold was evaluated.

The new command's nonzero exit is intentional: explicit diagnostic publication retained complete failed test evidence, and a green server gate did not erase the failed recipe. Ordinary operation rejects failed tests without the diagnostic override.

The one identical issue is an existing minor Xcode deprecation note in `App/Features/SystemIntegrations/FleetMinerControlIntents.swift:122` for `requestConfirmation(result:confirmationActionName:showPrompt:)`. It is not a SwiftLint finding.

## Independent native census

Sonar's aggregate test count is not the native exact-identity census. The runner separately reconciled every declared native identity and parameterized instance:

| Accepted bundle | Logical cases | Instances | Result |
| --- | ---: | ---: | --- |
| iOS unit shard 1 | 467 | 529 | Passed |
| iOS unit shard 2 | 491 | 840 | Passed after full-shard recovery |
| iOS unit shard 3 | 489 | 760 | Passed after full-shard retry |
| Native Mac | 118 | 198 | Passed |
| Tuning UI | 7 | 7 | Passed |
| Selected broader UI | 3 | 3 | **1 passed, 2 failed** |
| Merged | **1,575** | **2,337** | **1,573 passed, 2 failed** |

There were no skipped or expected-failure cases in the accepted census. Only the configured ten UI tests were run, not the entire 182-test UI inventory.

The two remaining app failures are:

1. `CompactDashboardUITests/testAccessibilityTickerExposesStaticUntruncatedMinerSummaries()`: expected three elements, observed zero.
2. `CompactDashboardUITests/testFleetToolsAndMinerControlsAreExplicitlyDiscoverable()`: `XCTAssertTrue` failed.

These failures were retained in both published analyses. This experiment does not establish the root cause of the second failure or claim either failure is repaired.

Other configured phases passed: SwiftPM tests (53 XCTest plus 284 Swift Testing), 95 Python tool tests, 18 CLI dashboard tests, 21 CLI settings tests, 11 CLI firmware/activity tests, and 16 Mac packaging tests. Native builds, release CLI build, merging, Swift coverage export, and Python coverage export also completed.

## Recovery and evidence integrity

This was **not an unattended clean one-command run**. The first shard-2 attempts failed during simulator startup. After restarting only the affected simulator without erasing its data, the entire 48-suite shard passed. Shard 3 initially encountered a rendering timeout, and its entire 46-suite shard passed on retry. Original failed bundles and phase exit records were retained. The supervising wrapper was paused during external retries to prevent an early merge, then resumed. Tests and assertions were not weakened.

Ten fresh declared artifacts were bound to the consumer's source/configuration/toolchain receipt. Native expected manifests were independently checked rather than inferred from an aggregate count. Xcode's native reader caches were prepared before freezing the artifacts and included in their hashes. Subsequent mutation still rejects the receipt.

The native merged identity digest was `f29272980e56847466e1878b400132fb2924cce849cea41460b312568c0e0e3e`. The expected-manifest digest was `c8b2ec5b49a93a443862926c937db78933315461782c4181dd0a417699dbd56b`.

Coverage export contained 48 Swift production files with 4,914 line observations (4,465 covered), plus 11 Python files with 882 line observations (608 covered). These report-specific observations are not a replacement for the combined server coverage metric.

## Toolkit and portability checks

- All **136 toolkit tests** passed from source and from the installed wheel after the last implementation change.
- The 0.2.0 wheel was built and installed outside the source tree.
- A second Git repository with spaces in its path executed two real Python fixture tests and offline preparation through the installed command, with real SwiftLint and scanner-version probes.
- That second repository establishes standalone packaging and basic project-mode execution. It is not a second independent Xcode app parity experiment.
- Negative regression coverage includes stale/missing/mutated artifacts, wrong native identities, native reader cache mutation, unsafe paths/configuration, failed recipes, and scope preservation.

Compatibility exercised here: Xcode 27.1 (`27A9275`), xcresulttool 25115 with schema 0.4.0, SwiftLint 0.65.1, SonarScanner 8.3.0.16463, sonar-apple 0.5.1, and SonarQube Community Build 26.9.0.129388. Other combinations require their own validation.

## How to use this result

Use [project mode](PROJECT-MODE.md) when replacing a comprehensive existing pipeline. Keep the consumer's reviewed scanner properties, a checked-in test recipe, declared typed artifacts, and independently reviewed native manifests. Do not substitute focused mode for this contract.

For the HashFleet consumer, the new entry point is:

```sh
# From the HashFleet checkout, with the compatible tools on PATH.
# PYTHON must select Python 3.11+ with coverage installed for this recipe.
PYTHON=/path/to/coverage-venv/bin/python bash Tools/swift-sonar-kit.sh project-run --prepare-only
```

The declared output paths must be fresh. Preserve previous audit outputs outside those paths before starting a new run. The runner does not delete them. Review the local plan before publishing with `project-scan`; explicitly configure the trusted server and credential through the existing secret mechanism. Diagnostic `--publish-failed-tests` is optional and does not turn a failure into success.

Docker can host the Sonar server, but Xcode tests still need a macOS host. A project root alone cannot describe an arbitrary app's schemes, destinations, artifacts, or intended test census. Those remain reviewed consumer configuration.

Not delivered by this verification: 100% coverage, an exhaustive UI matrix, automatic simulator repair, commercial Swift semantic/security analysis, SwiftLint analyzer rules, Periphery, or dependency/security scanners. Those are separate extensions, not capabilities implied by matching this workflow.
