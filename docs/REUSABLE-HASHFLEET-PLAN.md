# Reusable HashFleet-depth scanning: review and adoption plan

Research checked: 2026-10-31, with the [completed five-reviewer follow-up](FIVE-REVIEWER-FOLLOWUP-2026-11-01.md) on 2026-11-01 UTC. Status: proposal, not an implemented replacement for the current scanner.

## Preserve the working setup

Keep HashFleet's existing `Tools/sonar-scan.sh`, project properties, Docker server, data volumes, plugin, quality profile and dashboard unchanged. The toolkit must grow to match that workflow before becoming its default implementation. No automatic tool installation, profile edits, server replacement, public repository publication or paid service activation is part of this review.

The desired user experience is one shared runner with a repository path and project configuration. HashFleet should be one consumer, not a special hardcoded branch. Another Swift application should select its own source/test layout, build recipe and reports while using the same validated analysis implementation.

## What has actually been compared

Coordinator checks on 2026-10-31:

- HashFleet's optional `Tools/swift-sonar-kit.sh` adapter invokes the standalone Python implementation. Direct and adapter invocations produced byte-identical generic coverage XML for the same LCOV input.
- Comparing the original `Tools/swiftpm_coverage.py` converter with the standalone converter gave identical production coverage observations across 48 files and 4,914 lines.
- Scope differs: the original converter selects `Sources/**/*.swift`. The standalone offline converter retained 254 valid in-root files, including tests/dependencies. That is not an equivalent production denominator.
- Fresh lint returned zero findings in both examined scopes. This does not prove equivalence for nonempty findings, rule metadata, severity or omitted files.
- No two full end-to-end Sonar scans were run for this comparison. Full dashboard parity remains unproven.

The existing v0.1 validation is documented separately in `VALIDATION.md`. Tiny fixture coverage must not be presented as whole-project coverage or evidence that all bugs are absent.

## Gaps to close before migration

| Capability | Existing HashFleet workflow | Standalone v0.1 | Required adoption check |
| --- | --- | --- | --- |
| Source/test scope | Swift plus Python/tooling, explicit test inclusions and exclusions | Dedicated Swift production-directory scan | Same classified file inventory, including multi-language tooling |
| Xcode results | Native sonar-apple result-bundle import | No xcresult import | Fresh successful bundle, exact test identity census retained outside aggregate dashboard |
| Coverage | Native Xcode, optional generic Swift and Python reports | LCOV to generic line coverage | Equal executable-line observations and denominator for each intended scope |
| SwiftLint | Full-repository strict lint plus plugin sensor | Selected-source lint plus generic external issues | Same lint scope and deliberate single issue-import strategy |
| Configuration | Existing project properties and scanner arguments | Separate generated properties | Explicit validated reuse without losing exclusions, version, tests or report paths |
| Test evidence | Selected bundle existence, separate acceptance evidence | Selected command receipt and fresh file hashes | Bind source, build recipe, successful results and all imported artifacts |
| Runtime safety | Existing deployment | Read-only preflight and no server mutation | Preserve the server and fail closed on invalid configuration |

Native and generic SwiftLint imports can duplicate findings. This was observed in v0.1 fixture validation. Do not silently disable native rules on the existing HashFleet profile, or advertise combined totals as unique findings. A compatibility mode should retain native imports by default. An explicitly chosen generic mode needs separate administrator-reviewed profile configuration.

## Proposed shared configuration contract

This is a design, not a new command available in v0.1.

1. Consumer supplies a repository root and a checked-in, schema-versioned configuration.
2. Configuration selects reviewed existing Sonar properties, test/build argv, source/test classifications, expected tools and report paths. Avoid arbitrary shell interpolation.
3. Preserve existing multi-language properties. Reject credentials, endpoint overrides and gate-bypass settings in repository configuration. Server/token selection belongs to an explicit trusted invocation and secret store.
4. A preparation mode resolves paths, checks scope, validates artifacts and writes an inspectable plan without publishing.
5. Host-side build/test jobs produce fresh artifacts. Extend receipts to support xcresult directories safely, including deterministic hashes and checks for changed, missing or escaping content. A receipt alone still cannot prove a test script is honest or exhaustive.
6. Validate successful Xcode results and independently reconcile enumerated test identities. A parameterized Swift test aggregate in Sonar is not the authoritative census.
7. Publication uses a pinned compatible toolchain and enforces local test/lint outcomes as well as the server gate. Optional tools report `passed`, `findings`, `failed`, or `not run` distinctly.
8. HashFleet's adapter delegates to this runner only after isolated compatibility validation. Keep the old entrypoint available during adoption.

### Docker boundary

Docker hosts the Sonar server. The current HashFleet scanner runs on the host, where its plugin invokes SwiftLint. Xcode builds, simulator tests and xcresult tooling remain a macOS/Xcode job. Do not describe a Linux Docker image as a complete iOS testing environment. Portable report conversion and suitable SwiftPM workloads can have a separately tested Linux path.

For new users, provide administrator-controlled server setup with a pinned image digest, verified plugin checksum, persistent data and explicit startup. Connecting a repository should not reset an existing instance. Repository selection belongs in consumer configuration or a mount/working-directory mapping, not a customized Dockerfile for every app.

## Additional tools worth evaluating

These began as coordinator primary-source findings. The [five-reviewer follow-up](FIVE-REVIEWER-FOLLOWUP-2026-11-01.md) independently reviewed the first three candidates and the implementation gaps. None are installed integrations. Tool support claims are upstream claims until exercised locally.

| Candidate | Incremental value | Caveat and required proof |
| --- | --- | --- |
| SwiftLint `analyze` | Type-checked analyzer rules beyond ordinary lint | Requires a clean compiler invocation log. HashFleet already configures `unused_import`, but ordinary lint does not execute analyzer rules. First candidate for a pinned positive/negative fixture. |
| OSV-Scanner | Dependency advisory checks from Swift `Package.resolved` | v2.4.0 release notes enable `swift/packageresolved` for SwiftURL scans. General supported-lockfile documentation lagged that release. Prove actual package inventory and a known-advisory fixture before claiming useful Swift coverage. Disclose network queries or explicitly configure offline data. |
| Gitleaks CLI | Secret detection in files/history | Use redacted output, never publish secret values to Sonar. Review CLI and any separately distributed Action terms independently. Synthetic secret fixtures only during adapter validation. |
| Periphery maintained fork | Index-based unused Swift declaration analysis | Original `peripheryapp/periphery` is archived. `tiny-earth-moon/periphery` states maintenance/toolchain-compatibility scope, separate from closed-source Periphery Pro. Pin and test Swift/Xcode compatibility, library public API and runtime/ObjC/Codable false-positive cases. |
| Semgrep Community Edition | Custom Swift pattern/security checks | CE Swift is community-supported and limited relative to paid analyses. Do not claim paid cross-file capabilities or Pro rules. Review engine and individual rule licenses separately, use explicit local rules and positive/negative fixtures. |

CodeQL is not a universally free default for every private Swift consumer. Its query repository and CLI have different licensing terms. A public-repository-specific integration can be evaluated separately, not used to substantiate an unrestricted free offering.

Adding more scanners is not automatically better. Prioritize reproducible findings, bounded noise, clear licensing, explicit privacy behavior and distinct issue identities. Every proposed adapter needs malformed-report, failed-tool and empty-inventory cases so tool failure cannot look like a clean scan.

## Acceptance before saying “same or deeper”

- Freeze source revision, tool versions, report inputs and effective source/test scope.
- Compare isolated project keys using equivalent quality profiles, gates and new-code baselines, without mutating the production dashboard.
- Compare classified file inventory, issue engine/rule/path/line/severity tuples, executable-line coverage and independent test identities, not only a green gate or total issue count.
- Include nonzero findings and intentionally failing tests. Confirm both routes fail appropriately.
- Reject stale/mutated results, traversal and symlink escapes, malformed reports, missing tools and endpoint/gate overrides.
- Prove paths containing spaces, multiple source roots, test-only files, generated/vendor exclusions and multi-language reports.
- A deeper mode must preserve all accepted baseline evidence and add labeled checks. It must not silently replace a deeper native pipeline with a narrower green scan.
- Exercise installed-package CLI and consumer adapter after the final code edit. Linux support and GitHub Actions remain unverified until their actual jobs run.

## Five-worker review record

### Initial attempt, 2026-10-31

Exactly five read-only workers were launched through the configured automatic routing, with no model/account overrides:

- shrimp: additional free Swift tools.
- sauropod: HashFleet feature parity.
- blowfish: shared configuration design.
- buffalo: Docker/onboarding.
- butterfly: independent security and acceptance review.

At 20:39 UTC, sauropod and buffalo had failed before producing findings with provider HTTP 400 request-format errors. The other three exceeded their five-minute review budget, produced no deliverables after a final partial-report request and bounded await, and were stopped. No five-agent consensus or independent approval is claimed. The coordinator findings above do not substitute for independent review. The configured model pools and accounts were not changed to work around these failures.

### Recovered review, 2026-11-01 UTC

All five lanes subsequently delivered reports after bounded retries and two independently tested runtime repairs. The new accepted reviewers were monkey (free tools), dog (parity), orangutan (configuration), camel (onboarding), and dove (security). The automatic model pools were preserved. Two native lanes needed fresh automatic retries after the account-route repair; this was not a first-attempt success. See [the consolidated report and archived reviews](FIVE-REVIEWER-FOLLOWUP-2026-11-01.md) for actual routes, corrections, evidence, and remaining implementation work. The successful review does not establish scan parity.

## Primary sources checked

Public reference URLs, checked 2026-10-31:

```text
SwiftLint analyzer requirements:
https://github.com/realm/SwiftLint
OSV-Scanner Swift support release evidence:
https://github.com/google/osv-scanner/releases
OSV-Scanner general support documentation:
https://google.github.io/osv-scanner/supported-languages-and-lockfiles/
Gitleaks CLI:
https://github.com/gitleaks/gitleaks
Periphery original and maintained fork:
https://github.com/peripheryapp/periphery
https://github.com/tiny-earth-moon/periphery
https://raw.githubusercontent.com/tiny-earth-moon/periphery/master/LICENSE.md
Semgrep CE support and limits:
https://docs.semgrep.dev/semgrep-ce-languages
CodeQL repository/CLI distinction:
https://github.com/github/codeql/blob/main/README.md
Sonar generic issue import contract:
https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/importing-external-issues/generic-issue-import-format
```
