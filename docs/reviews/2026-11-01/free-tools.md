# Free/open-source Swift analysis additions review

**Reviewed:** 2026-11-01. **Scope:** Read `README.md` and `docs/REUSABLE-HASHFLEET-PLAN.md`; verify primary upstream sources. No tools installed or run. These are recommendations, not locally validated integrations. Preserve the full reusable HashFleet workflow: any new check must be additive, configured per consumer, and must not replace Xcode/xcresult, multi-language reports, existing Sonar properties, coverage, or native findings.

## Prioritized findings

### 1. SwiftLint `analyze` (best near-term incremental Swift check)

**Verified upstream:** SwiftLint documents analyzer rules as a separate rule list run by `swiftlint analyze`; its repo labels the license MIT. The plan says HashFleet configures `unused_import`, while ordinary lint does not execute analyzer rules. Analyzer requires a compiler invocation log, so this adds compiler-informed checks without substituting for existing lint or sonar-apple.

**Recommendation:** Integrate as an optional, separately reported check for Xcode consumers first. Reuse the actual build's compiler log; do not invent a generic invocation or silently report success when absent. Keep lint and analyzer findings distinguishable to avoid duplicates.

**Licensing/privacy:** MIT for upstream SwiftLint. Local execution is preferable; compiler logs may disclose local paths, build settings, and other environment details if uploaded. No additional network requirement established here. Pin versions and validate compatibility with selected Xcode/Swift toolchains.

**Fixtures:** Positive: a small compiled Swift target with a known analyzer violation such as unused import, plus a fresh compiler log, must produce a finding/nonzero result. Negative: clean target with valid fresh log passes; missing, stale, malformed, or unrelated log must be `failed`/`not run`, never clean. Test SwiftPM separately rather than assuming Xcode behavior.

Source: https://github.com/realm/SwiftLint (README and LICENSE; upstream `analyze` documentation).

### 2. Gitleaks (valuable repository-wide security complement)

**Verified upstream:** Gitleaks scans repositories, files, and stdin, including Git history; upstream README identifies MIT licensing. It detects secrets, not general Swift vulnerabilities. This is clearly additive to SwiftLint and coverage and applies to arbitrary consumer repositories.

**Recommendation:** Offer an opt-in local `git`/directory scan, with separate status/findings and a redacted Sonar adapter. Never include the matched secret or full finding snippet in Sonar output, logs, test fixtures, or the review report. Avoid making scan findings disappear into a green server gate.

**Licensing/privacy:** Gitleaks CLI is MIT. Upstream notes a GitHub Action, but that is a distinct distribution; do not infer its terms from the CLI. Local CLI use avoids sending source to a hosted service, but output artifacts can themselves leak secrets. Current upstream README calls Gitleaks feature complete and says future releases will be security patches only, a maintenance risk to track; this does not establish abandonment.

**Fixtures:** Positive: synthetic canary token in a temporary file and in a temporary Git commit should be detected, while serialized report output contains no canary value. Negative: ordinary text and a clean repo pass; malformed output/tool failure is not a pass. Include a test that scans only intended repository scope and honors exclusions deliberately.

Sources: https://github.com/gitleaks/gitleaks and https://raw.githubusercontent.com/gitleaks/gitleaks/master/LICENSE.

### 3. OSV-Scanner for lockfile dependency advisories (conditional, prove Swift support first)

**Verified upstream:** OSV-Scanner is an open-source dependency vulnerability scanner. Its current supported-artifacts page documents lockfile scanning and recommends resolved lockfiles, but the page's supported lockfile table does **not** list Swift `Package.resolved`. The plan records prior release-note evidence that v2.4.0 enabled `swift/packageresolved` for SwiftURL scans, while noting documentation lag. The checked release page now lists v2.7.0 as latest on its page, so the v2.4.0 evidence is historical, not proof that today's general Swift support is reliable.

**Recommendation:** Treat this as a narrowly gated experiment, not a default claim of complete Swift dependency coverage. Before integration, test actual Package.resolved formats (SwiftPM and Xcode), prove package inventory extraction, and match a known advisory. If the upstream support/docs disagree or no Swift dependencies are emitted, mark unsupported/failed, not clean. Keep it independent of the Sonar server gate until semantics are validated.

**Licensing/privacy:** The project is open source; verify and record the license for the pinned release before adoption (not independently established in the fetched evidence). Vulnerability checks rely on OSV data and may make network requests; disclose query behavior, offer documented offline mode where feasible, and do not upload private source/lockfiles without explicit review. Exact telemetry/query payload for the intended mode remains unverified here.

**Fixtures:** Positive: pinned synthetic/resolvable dependency set containing a known OSV advisory reports the expected package/version/advisory; validate both lockfile layouts. Negative: clean known inventory reports no advisory, and a missing/empty/unsupported lockfile or network failure yields `not run`/`failed`, never “no vulnerabilities.”

Sources: https://github.com/google/osv-scanner/releases and https://google.github.io/osv-scanner/supported-languages-and-lockfiles/.

## Overlooked check: lockfile support and empty-inventory sentinel

Before describing any reusable Swift dependency scan as covered, add a fixture/check that confirms `Package.resolved` is actually recognized and that at least one expected package appears in the tool's inventory. This gap is supported by the mismatch between the release-history evidence cited in the plan and the current upstream supported-lockfiles page, which omits Swift. Empty inventory must be surfaced as unsupported/failed, not a passing vulnerability scan. This is a validation requirement, not a fourth scanner recommendation.

## Validation boundary

No binaries were installed or executed, no positive/negative fixtures were run, and no local toolchain or provider compatibility is claimed. Source verification was read-only. Keep HashFleet's current server/profile/configuration untouched; all candidates should be optional consumers of the shared runner, with existing native coverage, test evidence, multi-language scope, and scanner properties preserved.
