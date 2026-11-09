# Swift Sonar Kit v0.1 implementation contract

Goal: a separate reusable free/open-source Swift analysis bridge. HashFleet is its first consumer, never a hardcoded dependency. Not SonarSource's commercial Swift analyzer and not a replacement semantic/security engine.

Python >=3.11, standard-library runtime, setuptools packaging. Root package `src/swift_sonar_kit`. CLI `swift-sonar`. No automatic downloads, server creation, restarts, rules/profile changes, or publication. Token only from SONAR_TOKEN. Source stays local until the user explicitly runs scan.

## Implementation and review ownership

Report adapters and server preflight were initially assigned as separate swarm tasks with non-overlapping file ownership. The workers failed before inference or stalled without deliverables. Those tasks were stopped, and the coordinator implemented and tested all components directly. No worker code or independent swarm approval is claimed. Future contributions should retain separate adapter, receipt, server and CLI boundaries and include acceptance evidence.

## Report API

`swiftlint_report(data: object, root: pathlib.Path) -> dict`: validate SwiftLint JSON list, return modern Sonar generic issues {rules:[], issues:[]}. Stable deterministic output. Required fields rule_id, reason, severity Warning/Error, file. line may be null (file-level finding), otherwise positive int within actual file length. Source must exist under resolved root (reject symlink escape, traversal, invalid/missing sources). Relative paths in output. Each rule keyed by rule_id, engineId `swiftlint`, name from type or rule_id, cleanCodeAttribute CONVENTIONAL, type CODE_SMELL, severity MAJOR for Error / MINOR for Warning, impacts MAINTAINABILITY HIGH/Error LOW/Warning. Mixed same-rule severity use maximum. Do NOT pretend these are security findings. Ignore columns rather than invent invalid UTF offsets. Preserve all findings, deterministic dedup allowed only exact same finding. Invalid inputs raise ValueError without leaking source contents. Modern JSON uses `ruleId` in issues.

`lcov_report(texts: list[str], root: pathlib.Path) -> str`: parse LCOV source/DA records to Sonar generic coverage XML version1. Resolve in-root existing source paths. Reject malformed numbers, negative counts, invalid/missing/outside source, DA outside record, unterminated records, out-of-range lines. Multiple records/files merge line coverage with OR. Deterministic sorted files/lines, XML escaping. Ignore recognized non-line LCOV tags, no invented branch coverage. Empty list/no DA raises ValueError, not fake100%.

## Server API

`preflight(url: str, token: str, expected_plugin_version: str = "0.5.1") -> dict`: authenticated read-only GET api/system/status, api/plugins/installed, api/languages/list. Require UP, sonarapple exact expected version, registered swift. Return only {server_version, plugin_version, language: "swift"}. Restrict URL HTTPS or explicit loopback HTTP (localhost/127.0.0.1/[::1]), reject URL userinfo/query/fragment and controls. Support server context path. Never follow redirects, including same-origin redirects. Never put token in errors/logs/URL. Fail closed on missing/malformed JSON/status/fields/network errors, raise ValueError with sanitized actionable messages. Verify TLS normally. Timeout15 seconds. Bound response to1MiB. No network in tests except bounded local mocks/server.

## Coordinator CLI plan

- `issues`: offline SwiftLint JSON conversion.
- `coverage`: offline one/multiple LCOV conversion.
- `doctor`: read-only server preflight.
- `test`: run explicit argv without shell, record successful exit and source fingerprint in local receipt. Document this attests selected command only, NOT exhaustive or coverage100%.
- `scan`: fresh SwiftLint, validated test receipt by default, supplied source directories, optional LCOV, preflight, SonarScanner with qualitygate.wait enforced. Failure propagation. No token argv. Reports in .swift-sonar (gitignored). No forwarding arbitrary scanner args that could disable gate or redirect token. Require source configuration, project key. Dedicated generated properties, never overwrite consumer properties.

## Acceptance

Unit tests happy/boundary/security fixtures. Real HashFleet strict lint report and existing LCOV converted offline. Read-only local Sonar preflight. Scanner fake-executable tests for gate arguments and failures. Isolated temporary Swift fixture scan only if authorized as task follow-through, no modification of HashFleet's existing dashboard/project settings. Package installation/entry-point smoke. No public push until repo contents and chosen target reviewed. CI uses only fixtures, never secrets from fork PRs.
