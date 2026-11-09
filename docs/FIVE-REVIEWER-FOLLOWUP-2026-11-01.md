# Five-reviewer follow-up: reusable Swift/Sonar workflow

Checked 2026-11-01 UTC. This is a completed research/review deliverable, not a claim that the proposed project runner or additional scanners are implemented.

## Answer to the user

The goal remains one reusable implementation selected by repository path and project configuration. HashFleet should use that implementation through an adapter only after it preserves the existing workflow. Users should not have to customize a Dockerfile for every Swift application. Docker hosts the server, while Xcode builds, simulator tests and native result processing remain on a Mac.

**The current isolated v0.1 toolkit is not yet equivalent to HashFleet's complete native pipeline.** Prior converter parity is useful but narrower: matching input LCOV gave matching production line observations, not matching end-to-end dashboards. More tools cannot compensate for omitted source/test classifications, Python tooling, native results, coverage, or gate behavior.

## Five delivered review lanes

All normal spawns used the existing automatic pools, without manual model/account overrides. These identities come from runtime records, not from a worker's self-description.

| Lane | Accepted worker/model | Deliverable |
| --- | --- | --- |
| Free/open-source tools | monkey, OpenAI GPT-6 Luna | [free-tools.md](reviews/2026-11-01/free-tools.md) |
| HashFleet parity | dog, native Claude Sonnet 5.5 | [parity.md](reviews/2026-11-01/parity.md) |
| Shared configuration | orangutan, OpenAI GPT-6 Astra | [runner-design.md](reviews/2026-11-01/runner-design.md) |
| Docker/onboarding | camel, OpenAI GPT-6 Luna | [onboarding.md](reviews/2026-11-01/onboarding.md) |
| Security/acceptance | dove, native Claude Opus 4.8 | [security.md](reviews/2026-11-01/security.md) |

This was not a first-attempt five-worker success. The earlier October 31 failures remain recorded in the roadmap. The first repaired-runtime retry completed three lanes but exposed a second runtime account-metadata bug in the two native Claude lanes. Those lanes were retried after repair and produced their reports. A private-daemon task timeout also required restarting only that isolated process, not the shared server. No findings from failed attempts are represented as completed reviews.

## Accepted implementation priorities

### 1. Preserve the whole existing project configuration

Add separate, backward-compatible `project-test` and `project-scan` commands with a checked-in JSON recipe and authoritative existing Sonar properties. They are **proposed commands**, not available commands in v0.1.

- Retain source/test classifications, inclusions, exclusions, project metadata and multi-language analysis. Do not force `**/*.swift` over HashFleet's existing Python/tooling scope.
- Execute reviewed argv arrays rather than shell interpolation. A repository build script remains trusted executable code, not a sandbox.
- Reject repository-owned credentials, server endpoints and gate/scanner overrides before adding properties reuse. Currently v0.1 generates its own settings, so this is a requirement for the new mode rather than a demonstrated existing properties-injection vulnerability.
- Resolve a root containing spaces correctly, reject escaping/symlinked inputs, and produce an inspectable preparation plan without publishing or changing server state.

### 2. Bind meaningful native test evidence

Project mode must bind the actual recipe/configuration, current source, toolchain, successful native results and every imported report.

- Add deterministic directory manifests for xcresult, including nested-file changes, rejected symlinks/special files and documented freshness checks. Current v0.1 rejects directory artifacts, so it currently fails closed rather than silently accepting an unsafe bundle.
- Validate successful results and reconcile independent test identities, not just a total count or command exit code. A file hash or configurable age alone cannot prove exhaustive testing.
- Keep schema-1 selected-command receipts honest and backward compatible. They are not proof that a configured Xcode recipe ran and must not satisfy the stronger project-mode contract.
- Require real supported-Xcode fixtures in addition to mock tests. Do not label all tests or coverage as 100% merely because the selected test suite passes.

### 3. Keep strict lint and a single issue-import strategy

Compatibility mode should run the full-repository strict lint gate before publication, matching HashFleet, and retain native plugin import by default. Do not simultaneously add generic SwiftLint findings for the same rules/scope.

Plugin installation alone does not prove every native rule is active, nor guarantee exactly twice as many findings. The existing validation records a duplication fixture, and the static review identifies a credible dual-import risk. Test it with nonempty findings and an explicitly known profile. A later generic mode requires administrator-reviewed configuration, not silent changes to HashFleet's profile.

Retain read-only server preflight, force the server gate, preserve local failure independently of a green dashboard gate, and keep credentials off argv/reports. The scanner needs its token in its own environment. That is distinct from unnecessarily passing it to test/lint children.

## Useful deeper checks, after compatibility

These are candidates, not installed integrations or locally proven findings:

1. **SwiftLint `analyze`:** compiler-informed analyzer rules beyond ordinary lint. Use a fresh clean compiler invocation log, prove a known positive and clean negative fixture, and reject missing/empty analysis rather than accepting zero files as clean. Upstream requirements were checked in the SwiftLint README.
2. **Gitleaks CLI:** local secret checks for files/history. Redact at the adapter boundary, use synthetic canaries in tests, and prove secret values never enter reports/logs/Sonar. Review CLI and separately distributed Action terms independently. Current SwiftLint message pass-through is not proof that a secret was leaked, but it cannot be reused blindly for secret-scanner output.
3. **OSV-Scanner:** conditional dependency advisory checks. Upstream release history includes Swift `Package.resolved` extraction, while the general supported-lockfile documentation has lagged. First prove the expected package inventory and a known advisory using supported lockfile layouts. Missing inventory, unsupported input, network failure and tool failure must not become a green vulnerability result.

The earlier Periphery and Semgrep candidates remain optional research, not replacements for the native baseline. No free wrapper should be marketed as containing SonarSource's proprietary Swift analyzer or all paid cross-file capabilities.

Primary sources checked during the follow-up:

```text
https://github.com/realm/SwiftLint/blob/main/README.md
https://github.com/gitleaks/gitleaks
https://github.com/google/osv-scanner/releases
https://google.github.io/osv-scanner/supported-languages-and-lockfiles/
```

## Review corrections and evidence limits

Worker reports are static reviews. They did not execute production scans, mutate profiles, or certify dashboard parity. The coordinator required correction of security-report attribution and claims about guaranteed duplication, token handling, current vs proposed configuration, and hypothetical vs demonstrated secret leaks before accepting that report. Design recommendations are not treated as automatically correct implementations.

Coordinator baseline validation after the review: **42 toolkit tests passed** under Python 3.12. This is the existing test suite, not full application coverage or implementation of the proposed project mode. HashFleet application files, original scanner, production Sonar server, profiles, and data volumes were not changed by this review.

## Swarm recovery evidence

Two concrete runtime defects were fixed independently of this toolkit:

- Gemini/Antigravity tool schemas used array-valued `type` where the wire format requires a scalar. Regression tests and real tool-call/follow-up probes passed after normalization. NVIDIA GLM 5.3 Flash also completed its approved-route tool probe, although its latency was much higher.
- Explicit native OAuth account selection changed the active provider but left a stale saved route. The lease correctly refused to run. The repair persists the verified explicit selection while leaving passive restoration fail-closed. Both formerly blocked native review lanes then executed tools and completed.

Account-focused tests passed 50 cases, and the spawn-session suite passed 56 cases, with overlap between suites. Independent review added partial-switch/failure tests. The wider Gemini test run still has a separately reproduced pre-existing assertion conflict, so no whole-runtime green claim is made.

Repairs were committed and integrated into the Jcode source checkout while preserving unrelated edits exactly. A repaired executable was exercised on a private socket. **The shared daemon was not reloaded or promoted**; activating the staged runtime remains a coordinated idle-window operation.

## Adoption acceptance remains open

Before replacing HashFleet's default entrypoint, freeze inputs/tool versions and compare isolated project keys with equivalent profiles/gates. Compare classified file inventory, nonzero issue tuples, executable-line observations and denominators, and native test identities. Include failed tests/lint, stale/mutated results, malformed reports, paths with spaces and excluded/generated/vendor files. Keep the working native path until this evidence exists.
