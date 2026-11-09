# Path-only workflow acceptance, November 7, 2026

## Requested outcome and boundaries

A user downloads this repository, supplies a Swift project path, and runs the same local test, real coverage, strict lint, and Sonar workflow. Runtime names derive from the consumer project and canonical root, not a fixed consumer application name. Host prerequisites are documented, not silently installed.

The implementation is version 0.3.0. No public GitHub, package-registry, or consumer-source publication was authorized or performed by this development task. A successful Sonar invocation does transmit source and analysis to the explicitly selected server or the kit-owned loopback server.

## Verified release checks

- Native reader compatibility was committed separately with sanitized schema-0.5.0 fixtures, unknown-schema rejection, parameterized-identity negative controls, and real local bundle checks. See `NATIVE-READER-2026-11-07.md` for its separate evidence and limitations.
- The first frozen public regression run executed 233 tests: 230 passed and 3 were explicitly skipped. After the last runtime change, the complete suite ran **237 tests: 235 passed, 2 external-native-fixture checks skipped**. The latter explicitly enabled real SwiftPM acceptance, including execution with a relative developer `PYTHONPATH`.
- The final version-0.3.0 wheel built offline, installed into a new isolated virtual environment, and imported outside the checkout with `PYTHONPATH` unset. A fresh consumer project in a path with spaces passed local preparation and a repeat run with prior evidence archived. All installed Python source hashes matched the frozen checkout.
- A real generic SwiftPM library named `OrbitMath`, in a path containing spaces, passed the actual checkout launcher through strict lint, tests, production-only LLVM coverage, artifact binding, and local scan preparation.
- The first full invocation downloaded and verified the plugin, then created correctly project-derived, ownership-labeled Docker resources. It stopped safely before server start and analysis upload because Docker Desktop rewrote the host bind path with `/host_mnt`. The exact failure was `Container mounts differ from private metadata`. This failed attempt was retained. The correction accepts only the exact expected plugin-path alias after macOS, local Unix-socket and Docker Desktop identity checks.
- The corrected full checkout run and a subsequent full repeat both exited **0**. The repeat reused the exact same owned container ID, preserved prior audit evidence, and again completed the scanner quality-gate wait successfully. Only the generic acceptance project was submitted to this isolated loopback server.
- Independent read-only checks against the actual server confirmed **quality gate OK**, **85.7% coverage**, **7 lines to cover**, and **1 uncovered line**. The real XML marked production line 5 as covered and the deliberately unexecuted branch on line 7 as uncovered.
- Live API checks confirmed a project-scoped analysis token bound to the exact consumer key, rejected the old default administrator password, verified loopback-only port binding and checked private-state directory/file modes of 0700/0600 outside the project tree. No credential values were printed or committed.

## Failure controls and retained rejected evidence

Missing or stale profile data, failed exact SwiftPM path queries, missing binaries, failed or empty LLVM export, unknown Xcode enumeration shapes, ambiguous project selection, and concurrent destination-file creation fail closed. No synthetic coverage fallback is accepted. Real coverage checks assert both an executed branch line and a known unexecuted branch line, not merely nonempty XML.

A previous worker acceptance claim was rejected because a synthetic coverage fallback and stale profile fallback existed. Those green results are not accepted as release evidence. Subsequent code and negative controls remove those paths, and final real receipts use the corrected implementation. A separate developer-suite attempt failed at a real SwiftPM child invocation. Relative `PYTHONPATH` entries are now anchored before changing the child working directory, with an actual child-import regression test. Final frozen checks above were performed after all runtime corrections.

One independent live-API verification attempt also failed because its adapter expected a top-level token `projectKey` field. The observed API returns `project.key`. The adapter was corrected to assert the same exact key in the actual schema, without dropping or weakening the project-scope check. The corrected verification passed before and after the full repeat.

## Actual consumer scan result

An existing comprehensive consumer recipe was retained, including its original multi-language/native-artifact scope. Both the initial invocation and the final version-0.3.0 `./scan PATH --prepare-only` invocation stopped at the mandatory strict lint preflight with **29 findings**. Tests were **not run**, prior audit output was **not archived**, and publication was **false**. A final full `./scan PATH` attempt, without `--prepare-only`, also stopped at the same 29-finding gate before tests or server setup. This does not establish a current native test census, a completed Sonar analysis, or an updated server dashboard.

The consumer scanner version pin was aligned to the installed 8.3.1 tool. No application source, miner configuration, user data, or application installation was changed by this scan task. The consumer's unrelated edits were preserved.

## Limitations and reproducibility

The real server check used macOS on Apple Silicon, Docker Desktop 29.5.2, the pinned Community Build 26.9.0.129388 image already cached locally, sonar-apple 0.5.1, SwiftLint 0.65.1 and SonarScanner 8.3.1.16491. Automatic image pull is bounded and fixture-tested, but a cold network pull was not forced during acceptance. Broader automatic Xcode discovery remains limited to unambiguous layouts and is not claimed universally supported. The installed wheel received fresh/repeat local-preparation acceptance, while full private-server publication was exercised through the checkout launcher.

Optional plugin Xcode-warning, Periphery and MobSF sensors logged command/parsing errors because corresponding inputs/tools were not configured for the SwiftPM fixture. They are **not** successful or validated advanced-analysis integrations. The verified scope is strict SwiftLint, real tests, real production line coverage, source-bound artifact validation, scanner submission and the configured server gate.

To reproduce the public local checks, use `PYTHONPATH=src python3.12 -m unittest discover -s tests -v`. Set `SWIFT_SONAR_RUN_REAL_TESTS=1` to enable the real SwiftPM test when its documented host tools are installed. For the actual path workflow, run `./scan "/path/to/My Project"`. Append `--prepare-only` to test locally without contacting or creating a server. The full run sends analysis to the selected or kit-owned server, not to a public package registry.

The independent reviewer reported no new blockers in the final frozen runtime delta. Source-bound reports are local unsigned evidence, not an exhaustive-testing or hardware/security certificate.

The final wheel SHA-256 is `e26ef6ac08c47c6187fc8344dfe2565dad889741aaae74648763abc98d4f0075`. All 12 runtime modules also passed Python 3.11 syntax parsing, which is not a substitute for a separate Python 3.11 runtime test. The temporary acceptance container was stopped after the final live checks to release resources. Its volumes, private credentials and audit evidence were retained. No existing consumer server was stopped or removed by this cleanup.
