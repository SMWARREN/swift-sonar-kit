# Native Reader Compatibility Analysis: Xcode 27.2 & xcresulttool Schema 0.5.0

Date: 2026-11-07
Status: Validated and Implemented (Staged for root integration)
Scope: `src/swift_sonar_kit/project_artifacts.py`, `tests/test_project_artifacts.py`, `tests/fixtures/native-0.5/`

---

## 1. Executive Summary

Xcode 27.2 (build 27B52) ships with `xcresulttool` version 25407 and bumped its default schema version from `0.4.0` (Xcode 27.1, build 25115) to `0.5.0`. Swift Sonar Kit's native reader previously enforced an allowlist of `SUPPORTED_RESULT_SCHEMAS = frozenset({"0.4.0"})`, causing all native `.xcresult` evaluations on Xcode 27.2 to fail closed with:
```
ValueError: Unsupported or unrecognized xcresulttool schema version
```

Rather than an uninspected allowlist widening, this work:
1. Audited the schema output formats emitted by Xcode 27.2 `xcresulttool` against `0.4.0`.
2. Verified that payload shapes across `summary`, `tests`, and `test-details` remain structurally compatible.
3. Tested live real bundles spanning single-device, parameterized instances, multi-device merged runs, and diagnostic failures.
4. Created sanitized offline fixture files under `tests/fixtures/native-0.5/` for reader/census roundtrips.
5. Added focused regressions in `tests/test_project_artifacts.py` (`Schema050Tests`).
6. Replaced hardcoded private repository paths with environment variable fixtures (`SWIFT_SONAR_TEST_REAL_BUNDLE`, `SWIFT_SONAR_TEST_REAL_FAILED_BUNDLE`).

---

## 2. Environment & Toolchain Inspection

- Host: macOS 26.6.2 (Darwin arm64)
- Xcode Version: Xcode 27.2 (27B52)
- xcresulttool Version: 25407
- xcresulttool Output:
  ```text
  xcresulttool version 25407, schema version: 0.5.0 (legacy commands format version: 3.60)
  ```
- Binary Location: `/Applications/Xcode.app/Contents/Developer/usr/bin/xcresulttool`

---

## 3. Schema Comparison (0.4.0 vs 0.5.0)

Using `xcrun xcresulttool get test-results <subcommand> --schema --schema-version <ver>`:
- `summary`: Schema definitions and required properties are identical between `0.4.0` and `0.5.0`.
- `tests`: Test tree definitions (`testNodes`, `devices`, `testPlanConfigurations`, `Arguments`) are identical.
- `test-details`: Activity and run detail structures are identical.

Running `xcresulttool get test-results <subcommand>` across actual bundles with `--schema-version 0.4.0` and `--schema-version 0.5.0` produced byte-identical JSON outputs.

### Test Counting Invariant
The native reader's dual accounting model remains intact:
- `summary.totalTestCount`: Unique test cases declared and run (e.g. 201).
- `devicesAndConfigurations[].passedTests`: Total executed test instances, including parameterized test arguments (e.g. 298 instances: 179 non-parameterized test cases + 119 argument invocations across 22 parameterized test cases).
- swift-sonar-kit's `_tally` and `_identities` reconcile this relationship: `totalTestCount == cases`, `instances == len(identities)`.

---

## 4. Real Artifact Verification Receipts

Four real `.xcresult` bundles were evaluated using read-only `xcresulttool` inspection:

1. **Current Mac Test Run (Xcode 27.2, single device, passed):**
   - Path: `$SCRATCH/Logs/Test/Test-HashFleetMac-2026.11.07_04-42-58--0500.xcresult`
   - Result: `Passed`
   - Test Cases: 201 (179 non-parameterized + 22 parameterized)
   - Test Instances: 298 (179 + 119 arguments)
   - Shape: `single`

2. **Prior Verified Unit Run (single device, passed):**
   - Path: `$PROJECT/build/controls12verified-unit-1-20261031.xcresult`
   - Result: `Passed`
   - Test Cases: 467
   - Test Instances: 529
   - Shape: `single`

3. **Merged Multi-Device Run (multi device, passed):**
   - Path: `$PROJECT/build/swift-sonar-project/merged.xcresult`
   - Result: `Passed`
   - Test Cases: 1,591
   - Test Instances: 2,364
   - Shape: `merged`

4. **Retained Audit Failure Run (diagnostic census):**
   - Path: `$PROJECT/build/controls12-final-audit-sonar.xcresult`
   - Default mode (`allow_failed=False`): Rejected with `ValueError: Native result is not Passed`
   - Diagnostic mode (`allow_failed=True`): Succeeded with `result="Failed"`, `failed=1`, `passed=1574`, total `1575` cases

---

## 5. Public Test Suite Decoupling

Hardcoded private paths in `tests/test_project_artifacts.py` were replaced:
- `REAL_BUNDLE`: read from `SWIFT_SONAR_TEST_REAL_BUNDLE` or `SWIFT_SONAR_REAL_BUNDLE`.
- `REAL_FAILED_BUNDLE`: read from `SWIFT_SONAR_TEST_REAL_FAILED_BUNDLE` or `SWIFT_SONAR_REAL_FAILED_BUNDLE`.
- When these environment variables are absent, integration tests gracefully skip via `@unittest.skipUnless`.
- When set, full end-to-end reconciliation against real Xcode output is performed. Unknown schemas fail acceptance immediately rather than skipping.

---

## 6. Sanitized Fixtures & Regression Suite

New sanitized offline fixtures are provided under `tests/fixtures/native-0.5/` specifically for reader and diagnostic census roundtrips:
- `single_passed_summary.json` & `single_passed_tests.json`: Single device with parameterized argument instances.
- `merged_passed_summary.json` & `merged_passed_tests.json`: Multi-device merged structure (`Test Case -> Device -> Test Plan Configuration -> Arguments`).
- `failure_summary.json` & `failure_tests.json`: Diagnostic failure structure.

Note: These fixtures validate parser schema roundtrip and census integrity; they are not substitutions for a fresh project audit or independent expected census acceptance.

New regression class `Schema050Tests` validates:
1. Reader identity extraction (`tool="xcresulttool"`, `version="25407"`, `schema="0.5.0"`).
2. Parameterized argument identity format (`nodeIdentifier?args=<hex>`) and checksum verification.
3. Merged multi-device label formatting (`platform|os|device|cfg<id>|target|nodeIdentifier`).
4. Diagnostic failure census retention and strict normal-mode failure rejection.
5. Strict rejection of skips (`skippedTests > 0`) and expected failures (`expectedFailures > 0`).
6. Fail-closed defense against unknown schemas (`0.3.0`, `0.6.0`, `1.0.0`, invalid strings).

---

## 7. Test Results Summary

Full test suite execution (`PYTHONPATH=src pytest`):
```text
163 passed in 17.18s (0 failures, 0 warnings)
```
Real bundle tests when environment fixtures provided:
- Known `467/529` bundle (`controls12verified-unit-1-20261031.xcresult`): PASSED
- Current `201/298` bundle (`Test-HashFleetMac-2026.11.07_04-42-58--0500.xcresult`): PASSED
- Retained failure diagnostic census (`controls12-final-audit-sonar.xcresult`): PASSED
