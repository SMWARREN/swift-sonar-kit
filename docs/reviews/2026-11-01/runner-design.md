# Reusable project runner: smallest compatibility contract

Review date: 2026-11-01 UTC (2026-10-31 local session). Status: proposed design, not implemented or execution-validated.

## Recommendation

Add two opt-in commands, `project-test` and `project-scan`, beside the existing commands. Their single source of scanner scope is a checked-in, reviewed properties file, not a second Swift-only source list. Add a versioned JSON recipe that binds that file to an argv test command, typed artifacts, native-result expectations, and lint policy. Do not implement this by forwarding to the existing `_scan`: that function deliberately generates a narrower Swift-only configuration.

Keep `test`, `scan`, `issues`, `coverage`, and `doctor` behavior and schema-1 receipts unchanged. Use a separate `.swift-sonar/project-tests.json` schema-2 receipt for project commands. A legacy receipt cannot satisfy project-scan. Leave HashFleet's current entrypoint, properties, server, profile, data, and adapter untouched until parity is demonstrated.

### Evidence from this checkout

| Finding | Source |
| --- | --- |
| There are no project commands today. | `src/swift_sonar_kit/cli.py:parser`, lines 16-38; `main`, lines 143-175. |
| Current scan replaces effective scope with explicit Swift production directories and generated properties. It does not inherit existing project properties. | `cli.py:_sources`, lines 54-71; `_scan`, lines 74-140, especially properties at 103-124; `README.md:49,68`. |
| Existing safeguards worth reusing include fresh strict lint, independent lint failure, repeat receipt verification, an explicit settings file, stripped `SONAR_*` environment variables, and a forced quality-gate wait. | `cli.py:_scan`, lines 83-140. |
| Existing evidence hashes Git-visible inputs and fresh regular-file artifacts. Directories cannot pass `is_file()`. Verification binds only the artifact paths the caller requests, and does not compare the stored command digest with an expected recipe. | `receipts.py:snapshot`, lines 21-46; `run_test`, lines 64-95; `verify`, lines 98-121. |
| `local_path` checks resolved containment but does not reject every in-root symlink or a symlinked ancestor. `_write` rejects a symlink leaf, not every parent. | `receipts.py:local_path`, lines 57-61; `cli.py:_write`, lines 41-45. |
| Publication already has read-only URL/token/server/plugin/language checks, no redirect following, and TLS validation. | `server.py:validate_url`, lines 18-36; `_get`, lines 39-60; `preflight`, lines 63-86. |
| Full HashFleet depth includes multi-language classification, native Xcode results/coverage, optional generic Swift/Python coverage, and full-repository strict lint with native import retained by default. End-to-end parity is explicitly unproven. | `docs/REUSABLE-HASHFLEET-PLAN.md:23-35,72-81`; `README.md:70-78`; `examples/README.md:13-21`. |

The actual HashFleet properties, scanner arguments, build recipe, and result bundles were not inspected. The plan is evidence of required capabilities, not a complete machine-readable inventory of that consumer. No exact HashFleet paths, native plugin property names, tool versions beyond the README targets, or parity result are inferred.

## Public command contract

```sh
swift-sonar project-test --root '/repos/Example App' --config Tools/swift-sonar.json
swift-sonar project-scan --root '/repos/Example App' --config Tools/swift-sonar.json --prepare-only
swift-sonar project-scan --root '/repos/Example App' --config Tools/swift-sonar.json --url http://localhost:9000
```

Both commands require `--root`; `--config` defaults to `swift-sonar.json`, relative to that root, never the caller's cwd. Both require the actual Git top level. The root itself can be supplied through an alias and then canonicalized. Descendant config/source/artifact/output paths must not traverse symlinks.

`project-test` runs only the selected test/build recipe and native-result validation. `project-scan --prepare-only` requires that evidence, runs fresh local lint, validates all imports, and writes an inspectable effective plan/properties file without preflight, credentials, network, or scanner execution. It is preparation, not a test runner or offline lint-free preview. Publication is explicit by omitting `--prepare-only`, as in today's scan contract.

Trusted invocation owns server selection, credentials, gate policy, and any isolated project-key override. `--url` may retain today's `SONAR_HOST_URL` fallback. `SONAR_TOKEN` is read only for publication. Add `--max-test-age` with today's positive 3600-second default. No arbitrary scanner arguments, environment map, shell text, executable downloads, or server-mutation hooks.

Suggested exit meanings: 0 for successful requested work, 1 for findings/scanner/gate failure, 2 for invalid configuration/evidence/tool execution. Project-test can preserve the current positive child exit code, mapping signal termination to 1. Tool failure must never be labeled clean.

## Exact example JSON

This is an illustrative consumer configuration, not a claim about HashFleet's actual paths. Its companion properties file must already refer to the same report paths through the correctly reviewed native/generic/Python properties. Optional coverage artifacts are omitted when that consumer does not import them, never silently omitted when its properties do import them.

```json
{
  "schema": 1,
  "properties": "sonar-project.properties",
  "scannerProperties": {},
  "test": {
    "argv": ["/bin/bash", "Tools/test-with-reports.sh"],
    "artifacts": [
      {
        "kind": "xcresult",
        "path": "build/TestResults/App.xcresult",
        "expectedTests": "Tools/sonar-test-identities.json"
      },
      {
        "kind": "generic-coverage",
        "path": "build/reports/swift-coverage.xml"
      },
      {
        "kind": "python-coverage",
        "path": "build/reports/python-coverage.xml"
      }
    ]
  },
  "lint": {
    "argv": ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"],
    "import": "native"
  },
  "tools": {
    "swiftlint": "0.65.1",
    "sonarScanner": "8.3.0",
    "sonarApple": "0.5.1"
  }
}
```

Field rules:

- Require schema integer 1, not boolean; reject duplicate JSON keys, unknown fields, empty argv, non-string argv members, NUL/control characters, duplicate artifacts, unknown kinds, and overlapping artifact roots. `scannerProperties` is optional, defaults to `{}`, and accepts string values only.
- `properties` is the checked-in authoritative scanner baseline. `scannerProperties` is the reviewed translation of legacy scanner `-D` arguments, not a free-form argument list. Overlay values win over the base file only for permitted keys; the plan lists every difference and its provenance. Every path/scope/property check is performed on the final merged map. No hidden environment or invocation overlay is allowed.
- Sources, tests, inclusions, exclusions, project version, encoding, and native/import settings stay in the existing properties file. Do not duplicate them in JSON or insert `sonar.inclusions=**/*.swift`. Multi-language tooling remains selected if the baseline selects it.
- Every imported artifact must be represented and receipt-bound, including Python reports and any additional native bundles. The typed-property registry maps supported scanner report properties to declared paths; references not covered by a supported adapter block adoption rather than being dropped or accepted blindly. Native bundle coverage and test import may share one declared xcresult artifact.
- The initial compatibility implementation supports only `lint.import=native`. It executes full-repository strict lint from root using the checked-in lint configuration, but does not generate or add generic SwiftLint issues. Validate that lint argv resolves to the pinned SwiftLint executable, selects `lint`, retains `--strict` and JSON reporting, and cannot add flags that bypass lint or silently narrow the reviewed scope. Existing generic reports from other engines may remain if validated and declared. Generic SwiftLint migration is a later explicit mode requiring separately reviewed profile configuration, not automatic profile changes or dual import.
- The example versions are the README's compatibility targets, not newly verified installed versions. Check actual executables on the host before use. Record the selected Xcode version/build, developer directory, Swift toolchain, result-tool version, and toolkit version in evidence; scan must use the same native-result reader/toolchain. Support only fixture-tested Xcode result formats, failing closed for unsupported ones.
- The expected-tests file is checked in and separately hashed. Its adapter-defined identities must retain suite/target, case identity, and parameterized instance information where available. Exact sets or multisets plus explicitly permitted skips are compared, not merely aggregate counts. If enumeration cannot distinguish expected instances, record a validation failure instead of claiming census parity. Per-bundle files are appropriate for separate iOS/macOS/UI suites.

The wrapper must run all intended host-side Xcode and other tests, export reports, stop on any failed command, and avoid publication. It is executable reviewed repository code, not a sandboxed declarative recipe. No wrapper honesty or exhaustive test claim follows from an exit code alone.

## Required validation and execution order

### 1. Resolve the invocation without side effects

Canonicalize the root, require a directory and Git top level, then resolve config and properties relative to it. Require config, properties, expected-test manifests, and repository script arguments to be tracked regular files with reviewed working-tree contents. Require config/properties/manifests to match the Git index for the checked-in contract. Do not require all application source changes to be committed; bind their actual Git-visible bytes through the receipt.

Parse configuration strictly. Reject lexical traversal, absolute config/report paths, controls, and unsupported interpolation syntax. Inspect existing path components with `lstat` before resolving so containment does not erase evidence of a symlink. Missing output leaves are allowed only below validated in-root ancestors. A URL supplied to the invocation can be syntax-checked locally now with `validate_url`; do not read the token or contact the server.

### 2. Parse and classify the effective scanner properties

Use a Java-properties-compatible parser/serializer for the supported scanner encoding, including continuations, escaped separators, and Unicode escapes. Reject malformed syntax and duplicate decoded keys. Never use `split('=')` plus a raw concatenated tail: that can miss escaped protected keys. If the parser cannot preserve a baseline's meaning, stop and report the unsupported construct without editing the original.

Merge the reviewed overlay, enforce the protected-policy table below, and classify every property through a versioned registry as scope, report path, safe scalar, or protected. Unknown properties require an implemented/reviewed classification before migration. This is intentionally fail-closed, not an assertion that all existing configurations already work.

Build effective production/test inventories using the preserved scanner scope and inclusion/exclusion semantics. Permit an explicitly reviewed `sonar.sources=.` baseline only if the resolved classified inventory satisfies these checks. Do not reuse `_sources`, whose no-root, Swift-only restrictions would destroy compatibility. Reject production/test classification overlap, escaping/missing selected files, selected symlinks, empty expected scope, and unintended generated/vendor/report/state directories. Preserve exclusions, including coverage exclusions, without confusing coverage denominator with production file inventory. Unsupported matching semantics block publication rather than being approximated.

Resolve every report property, including any supported glob, to the exact declared artifact set. During project-test preflight, validate intended output paths and their ancestors without requiring outputs to exist; defer glob expansion, report contents, and source-record checks until after the successful command. For project-scan, require every import to exist before lint or publication. Resolve native embedded absolute paths only if they canonicalize to the current checkout. Reject outside-root paths, ambiguous remappings, missing files, unexpected reports, and source records outside the appropriate production/test classification. Coverage cannot absorb test/dependency files. Test records must refer to the intended test identities. Full-repository lint can legitimately find lint-only files outside scanner scope, but those findings still fail the independent local lint check and must not be silently reclassified as production coverage.

### 3. Establish safe output state and invalidate old success

Use a private in-root `.swift-sonar` directory, reject symlinked state/output parents and leaves, and enforce one project job per checkout using a lock. Use separate project receipt/summary/plan filenames. Project-test invalidates the prior project-test receipt and scan summary before tool probes or test execution, including an attempted failing rerun. Project-scan invalidates only its old summary/plan, retaining the receipt it must verify. Read-only Git/config discovery may precede this step. Failure paths never write a success receipt or reuse an old summary as the current result.

Use atomic no-follow temporary-file creation and rename for evidence writes. Recheck path ancestry at the use boundary. These controls reduce ordinary races, but are not adversarial attestation against a hostile process controlling the checkout.

### 4. Run project-test and establish fresh directory evidence

Check required tools locally and resolve executables through trusted invocation/PATH, not a config-provided PATH. Require all declared xcresult output paths to be absent immediately before launch. If a bundle already exists, fail with instructions to explicitly move it aside. Do not delete or trust it, and do not let a wrapper's later `touch` make it fresh. This is the smallest safe fixed-path contract; per-run placeholders can be a later extension.

Capture the pre-run input snapshot, config/properties/overlay digest, expected-test manifests, exact argv digest, tool identities, and start time. Execute `subprocess.run(argv, cwd=root, shell=False, env=filtered_env, check=False)`. Never join argv, expand `$VAR`, evaluate globs, or use implicit `sh -c`. A checked-in script invoked through an interpreter is permitted, but command-string switches such as `bash -c` are not a recipe language. The script is still trusted code and can have arbitrary side effects, which must be documented.

Strip all `SONAR_*` variables from test/lint children, improving on `run_test` which currently removes only `SONAR_TOKEN`. Do not forward arbitrary repository environment settings. Do not claim this removes every developer secret from a host environment or creates a sandbox. Prefer a documented host-build environment allowlist, with explicitly trusted build variables for Xcode/SDK/tool selection, never JSON-provided scanner or credential variables.

After successful exit, require unchanged Git-visible input state and recipe digest. Each file report must be a fresh regular file, produced within the run and hashed. Each xcresult must now be a nonempty directory at its previously absent path. Directory mtime alone is not evidence. Reject symlinks at every level, escapes, sockets/devices/FIFOs, and unstable reads. Compute a deterministic tree manifest containing sorted relative member paths, entry type, file size, and SHA-256 bytes; include directory entries so additions/removals/renames are detected. Hash canonical manifest bytes with an explicit manifest format version. Record member counts and total bytes, and bound traversal size/depth.

The pinned native result reader must successfully open every bundle and return successful action/test status, no unapproved failed/cancelled/skipped cases, an expected nonempty test census, and run timestamps consistent with this invocation. Store the independent identity reconciliation result with the bundle digest. Unsupported timestamps or identities require a proven version-specific adapter, not an inferred pass. Hash/recheck the bundle around native parsing to detect concurrent changes. Copying old contents into a newly created directory is not made trustworthy by absence alone; native run metadata and census are additional checks, still local unsigned evidence.

Finally record completion time and atomically write a schema-2 receipt binding every declared artifact, not merely coverage. Use finite numeric timestamps and reject time reversal. Record `scope=selected-command-only` separately from the exact census that was actually reconciled. Git-ignored build-affecting inputs remain a documented limitation unless explicitly fingerprinted; generated artifact directories should be ignored so they do not invalidate the before/after source snapshot.

### 5. Prepare project-scan entirely locally

Reload and validate config/properties, match the receipt's complete artifact set, recipe/manifest/tool digests, Git snapshot, and finite positive freshness window. Recompute every tree/file hash and native-result validation. Reject schema-1 receipts, config swaps, command changes, missing/extra artifact declarations, expired/future timestamps, or mutation of any member.

Run fresh full-repository strict lint with validated argv and filtered environment. Parse its JSON even in native-import mode: zero findings and exit 0 mean passed; valid findings mean findings; malformed JSON, unsupported exit codes, or nonzero exit without reportable findings mean failed. No generic SwiftLint report is automatically imported. Store status and counts without calling findings a successful clean check.

Validate native/generic/Python report contents and source classifications with supported adapters. Preserve each engine's original observations and coverage ownership; do not OR-merge unrelated native/generic/Python report formats or fabricate executable lines/branches. Existing `lcov_report` is reusable only for a deliberately configured LCOV conversion. Avoid publishing overlapping coverage observations without proving the baseline's intended precedence.

Write `.swift-sonar/project-effective.properties` plus a redacted plan listing property provenance, original/effective differences, classified inventory digest, artifact hashes, tool versions, lint outcome, and the selected-command limitation. Force only the trusted safety properties listed below. Preserve original properties bytes on disk. Reverify evidence after lint and plan generation. `--prepare-only` stops here with no scanner, server requests, or token access.

### 6. Publish only after local validation

Reverify immediately before publication. Read the token only now, run existing `server.preflight` with the trusted URL and pinned plugin version, then invoke the scanner through a fixed argv list with the generated settings file, canonical base directory, and gate wait enforced. No free-form config-supplied scanner flags. Only the scanner receives `SONAR_TOKEN` and `SONAR_HOST_URL`; neither is written into properties, receipt, plan, or argv.

Reverify inputs, config, report trees, and generated settings after scanner completion before writing a success summary. Return nonzero for local lint findings even if the server gate passes. Report independent statuses for tests, native census, lint, scanner, and gate. A post-scan mutation can invalidate local success but cannot retract already-published server data; do not describe this as an atomic remote transaction. Preserve the one-job-per-checkout requirement.

## Protected scanner properties and injection boundaries

| Class | Contract |
| --- | --- |
| Credentials and destination | Reject `sonar.host.url`, `sonar.token`, `sonar.login`, `sonar.password`, and any credential/proxy/TLS-auth equivalent in either repository input. Do not silently inherit a localhost value just because it looks safe. Trusted invocation owns destination; a secret store supplies the token. |
| Settings/base/work paths | Repository inputs cannot set `project.settings`, `sonar.projectBaseDir`, or `sonar.working.directory`, or redirect settings/module roots outside validated scope. Runner owns generated settings, canonical root, and private work directory. Legacy module settings require explicit support with the same containment rules. |
| Gate and skip controls | Effective `sonar.qualitygate.wait` is always `true`; timeout is runner-owned (default 300 seconds). Accept identical canonical values already in the baseline, reject conflicts. Reject scan-skip, dry-run, preview, gate-bypass, or arbitrary scanner execution controls. Unsupported keys are not passed through. |
| Analysis identity and scope | Preserve validated project key, project version, sources, tests, inclusions, exclusions, and report locations from the reviewed baseline. Only a trusted explicit project-key override may select an isolated parity target, and the plan must expose the change. Do not force UTF-8 or Swift-only inclusions over a different reviewed baseline without an explicit compatibility decision. |
| Extra properties | A reviewed `scannerProperties` overlay passes exactly the same decoded-key and typed-value validation as the file. No raw text append and no duplicate-key last-wins bypass inside either input. Unknown property keys block rather than silently changing analysis depth. |
| Environment and scanner installation | Strip `SONAR_*` before adding only the selected host/token. Also neutralize scanner/JVM option injection such as `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, and `JDK_JAVA_OPTIONS` in scanner execution. Prevent unchecked global scanner settings, launcher flags, or property interpolation from overriding the validated plan. Pin/inspect the trusted scanner distribution and verify this boundary with fixtures; if effective precedence cannot be established, block publication. Do not alter the user's global scanner installation/configuration automatically. |

The protected names above establish ownership, not an exhaustive claim about every version's scanner properties. The versioned allow/classification registry is essential: a blacklist alone cannot safely classify unknown path, execution, or secret-bearing properties. A conflicting existing baseline should yield a compatibility blocker, never an automatic destructive rewrite or an unnoticed narrower scan.

## Three implementation priorities and acceptance tests

### Priority 1: config loader and faithful effective-plan compiler

Add a small `project_config.py` and thin command dispatch in `cli.parser/main`; keep `_scan` unchanged. Implement strict JSON, decoded properties parsing, trusted override ownership, path/classification registry, inventory generation, and inspectable preparation output before allowing publication.

Tests: installed-package CLI dispatch plus unchanged legacy commands; paths with spaces and multiple language/source roots; preserved tests, project version, exclusions, native bundle property, and Python/generic report properties; properties bytes unchanged; controlled overlay diffs; escaped/continued/Unicode protected keys; duplicate keys; absolute/traversal/symlink ancestors; root scope with valid reviewed exclusions; source/test overlap; unknown report properties fail closed. Mock token lookup, networking, and scanner launch and assert they are never reached by prepare-only. Do not claim the prototype covers actual HashFleet properties until those inputs are compared.

### Priority 2: versioned receipt and native xcresult validator

Factor artifact evidence behind typed file/tree adapters, reuse `snapshot`, and add separate schema-2 project evidence with complete config/argv/tool binding. Implement fresh-absence checks, deterministic tree manifests, native success decoding, and independent test identity reconciliation. Never treat a directory stat or successful wrapper exit as sufficient.

Tests: existing schema-1 receipt behavior unchanged; schema-1 rejected by project-scan; preexisting bundle even with new directory mtime; empty/malformed bundle; freshly copied stale metadata; add/delete/rename/mutate nested file without changing parent mtime; symlink at root/ancestor/member, special files, unstable reads; deterministic manifest ordering; stale/future/nonfinite/boolean timestamps; command/config/properties/manifests/tool changes; test failure invalidates old success; zero tests, failing/cancelled actions, explicit skipped-test policy, parameterized identities with equal counts but different members. Use synthetic temporary fixtures in a later authorized implementation session, followed by real macOS result-bundle fixtures for each supported Xcode build. Mock-only result parsing is not proof of Xcode compatibility.

### Priority 3: compatibility publisher and HashFleet parity gate

Implement project-scan as a separate orchestration path reusing the existing read-only preflight and independent lint/gate outcomes. Default to native import. Carry all accepted baseline properties and report types, keep old HashFleet entrypoints, and make unsupported depth a blocker rather than a fallback to focused scan.

Tests: exact scanner argv/cwd/environment and no shell expansion; arguments containing spaces/metacharacters remain individual literals; token absent from children/argv/files; environment and scanner-global overrides blocked; malformed lint and tool failure stop publication; lint findings stay nonzero despite green gate; no scanner after stale/mutated artifacts or failed preflight; mutation during lint/scanner detected; all imported artifacts checked, not only LCOV; native mode adds no generic SwiftLint importer. Finally run explicitly authorized isolated-key parity against the original workflow with frozen inputs, equivalent profiles/gates/new-code baselines, matching classified file inventories, rule/path/line/severity tuples with nonzero findings, executable-line observations/denominators per report scope, and exact native identities. Include failing tests and invalid artifacts. A green gate or zero lint findings alone is not parity.

Existing tests to retain/extend: `tests/test_cli.py:CLITests.test_prepare_has_no_network_and_preserves_consumer_configuration`, `test_scan_enforces_gate_keeps_token_off_argv_and_strips_injection`, `test_lint_findings_cannot_be_hidden_by_green_server_gate`, `test_source_mutation_during_lint_stops_publication`; `tests/test_receipts.py:ReceiptTests.test_failed_rerun_invalidates_prior_success`, `test_artifact_must_be_fresh_and_bound`, `test_stale_future_and_malformed_receipts_fail`. `reports.py:_source/lcov_report` demonstrates current root and line-bound checks, but does not supply native/Python readers or full scanner classification semantics.

## Review validation and limits

All five requested files were batch-read, including follow-up reads of truncated tails. Additional local reads covered CLI/receipt tests, report adapters, and consumer examples. No repository code was executed, no repository tests/scans/installations/network requests/Docker operations/credential access occurred, and no child workers were created. Only this design note was written. Proposed implementation tests above were not run. An in-memory `python3 -B` standard-library check passed for the note's JSON syntax, integer schema, three typed artifacts, argv strings, native import mode, exactly three implementation priorities, and absence of em dashes. This validates document structure only, not implementation or runtime compatibility.

Adoption blockers: actual HashFleet effective properties/legacy flags and test identities still need an authorized inventory; native result/property adapters and multi-language report validation do not exist in the inspected implementation; scanner-global precedence and full end-to-end parity remain unverified. These are required follow-ups, not reasons to replace the current deeper pipeline with v0.1 focused scan.
