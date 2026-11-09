# Project-mode runner review

> Historical first-pass review of an incomplete concurrent implementation. It is retained as review provenance, not the current status. By 2026-11-01 02:52 UTC the config module is present, exact lint argv is checked at both boundaries, exceptions record failed summaries, scanner global configuration is checked, and the real retained HashFleet merged bundle reconciles all 2,337 instances including its failure. The revalidation call in `_write` intentionally raises if path ancestry changes. See final verification evidence before drawing release conclusions.

Reviewer: independent, read-only. No source edits, commits, child workers, network scans, builds, or hardware use.
Date: 2026-10-31 local (2026-11-01 UTC). Scope reviewed: `src/swift_sonar_kit/project_runner.py`,
`src/swift_sonar_kit/project_artifacts.py`, `src/swift_sonar_kit/cli.py` (project dispatch), `tests/test_project_runner.py`,
plus the design note `docs/reviews/2026-11-01/runner-design.md`. The "upcoming" `project_config.py` is not present in the
checkout and could not be reviewed because it does not yet exist.

## How this was validated

- `PYTHONPATH=src python3.12 -m pytest tests/ -q` -> project tests cannot be collected (import error, see P0-1).
- `PYTHONPATH=src python3.12 -m pytest tests/test_cli.py tests/test_receipts.py tests/test_reports.py tests/test_server.py -q`
  -> 42 passed. The legacy pipeline is unaffected by the new code.
- Static read of every reviewed module plus `receipts.py` and `server.py` for the interfaces the runner consumes.
- No repository scan, build, Docker, credential read, or server contact occurred. No real xcresult fixture exists to exercise
  the native census, so native-path claims below are from code reading only.

## Findings (prioritized)

### P0-1 (Blocker) `project_config` module is absent; the feature and its tests do not import

`project_runner.py:20-21` imports `load_project, safe_path, serialize_properties` from `.project_config` and
`artifact_digest, validate_artifact` from `.project_artifacts`. The second module exists; `project_config.py` does not
(`ls src/swift_sonar_kit` shows no such file; `import swift_sonar_kit.project_config` -> `ModuleNotFoundError`).

Consequence: `tests/test_project_runner.py:14` (`from swift_sonar_kit import project_runner`) fails at collection, so **none**
of the 12 project tests run. The CLI subcommands `project-test/scan/run` also raise on first use because
`cli.main` lazily imports `project_runner` (`cli.py` dispatch block). As committed, the whole project workflow is
non-importable and unverifiable.

Repro:
```
cd /path/to/swift-sonar-kit
PYTHONPATH=src python3.12 -m pytest tests/test_project_runner.py -q
# -> ModuleNotFoundError: No module named 'swift_sonar_kit.project_config'
```

This is expected since the module is "upcoming", but it means every downstream finding about config/property/inventory
behavior is currently untestable. Land `project_config.py` before any parity or green claim.

### P0-2 (Interface spec the missing module must satisfy, load-bearing)

The runner silently depends on a specific transformation that `load_project` must perform, and getting it wrong will not
fail loudly, it will corrupt the scan. `project_runner.py:204` does `properties = dict(recipe["properties"])`, treating
`recipe["properties"]` as a **mapping**. But the on-disk recipe schema and the test fixture set
`"properties": "sonar-project.properties"`, a **filename string** (`tests/test_project_runner.py:37`). If `load_project`
returns the raw string here, `dict("sonar-project.properties")` raises; if it returned a char-indexable surrogate it would
produce garbage scanner settings. So `load_project` must replace the `properties` filename with the parsed+merged effective
property dict, and must also inject `recipe["inventory"]` (consumed at `project_runner.py:111,136,167` via `_artifacts` ->
`validate_artifact(..., recipe["inventory"], ...)`) and `recipe["digest"]`. Required contract to implement and test:

- `load_project(root: Path, config: Path) -> dict` returning keys `digest` (stable hash), `properties` (effective merged
  dict of scanner properties, filename already parsed and overlaid with `scannerProperties`), `inventory`
  (`{"sources": {...}, "tests": [...]}` per `project_artifacts._inventory`), `test.{argv,artifacts}`,
  `lint.{argv,import}`, `tools.{swiftlint,sonarScanner,sonarApple}`.
- `safe_path(root, relname, *, must_exist=False) -> Path` (used with `must_exist=True` at `project_runner.py:152`; must
  not create directories, `_job` does its own `mkdir`).
- `serialize_properties(properties: dict) -> str` producing Java-properties text (consumed at `project_runner.py:211`).

Add a direct test asserting `load_project` yields a dict `properties` and a populated `inventory`, so a regression in the
module cannot silently feed the runner a string.

### P1-1 (Should fix) The strict-lint gate does not independently validate the lint argv

`_lint` (`project_runner.py:172-187`) builds `argv = [tools["swiftlint"]["path"], *recipe["lint"]["argv"][1:]]` and runs it
verbatim. It never confirms that argv selects `lint`, retains `--strict`, and uses `--reporter json`. The design makes this a
requirement of the strict gate ("retains `--strict` and JSON reporting, and cannot add flags that bypass lint or silently
narrow the reviewed scope"). Today a recipe that drops `--strict` or swaps the reporter would still be accepted, and since
`_lint` is the independent strict-lint-before-publication gate, that silently weakens it. If this validation is intended to
live in `project_config.load_project`, it still must exist and be tested; there is currently no test that a recipe without
`--strict`/`--reporter json` is rejected. Suggested repro test: feed a recipe with
`lint.argv = ["swiftlint","lint","--quiet","--reporter","json"]` (no `--strict`) and assert publication is blocked.

### P1-2 (Should fix) Malformed lint stdout surfaces as an uncaught exception, not a recorded `failed` status

`_lint` does `findings = json.loads(result.stdout)` (`project_runner.py:177`). `json.JSONDecodeError` subclasses
`ValueError`, so the existing `test_lint_malformed_and_failed_tools_are_not_clean` expectation is met, but in the real
`project_scan` flow a lint tool that prints human diagnostics before/around JSON on a `0`/`2` exit raises straight out of the
job rather than writing a `project-summary.json` with `status: failed` and `published: false`. The design wants "malformed
JSON ... mean failed" recorded as a status. Low severity (it still fails closed, no publication), but the summary artifact is
absent on that path, which is inconsistent with the lint-findings path at `project_runner.py:201`.

### P2-1 (Minor) Dead/confusing redundant `safe_path` call in `_write`

`project_runner.py:45` calls `safe_path(root, ".swift-sonar/" + name)` and discards the result immediately before
`os.replace(temporary, path)`. It has no effect (the real re-validation value would need to be assigned to `path`). It reads
like an intended "recheck ancestry at the use boundary" that was left incomplete. Either bind it (`path = safe_path(...)`) to
actually re-validate before the rename, or remove it to avoid implying a guarantee that is not there.

## Preserved guarantees confirmed (no overclaim)

- **Known HashFleet ticker UI failure stays visible.** The native reader fails closed: `_tally` rejects any summary whose
  `result != "Passed"` or with nonzero `failedTests/skippedTests/expectedFailures` (`project_artifacts.py:503,510-514`), and
  `_identities` rejects any failure/skip node and reconciles leaf identities against the checked-in `expectedTests`
  (`_validate_xcresult`, `project_artifacts.py:619-623`). A bundle containing the ticker failure cannot produce a schema-2
  receipt, so no green exhaustive claim is possible. The receipt also records `scope: configured-project-recipe` and an
  explicit limitation string "Census covers declared native manifests, not every possible test" (`project_runner.py:139-143`).
- **Strict lint blocks publication before any network.** `project_scan` runs `_lint` and returns `1` with
  `published: false` on findings, before `preflight` or token read (`project_runner.py:199-202`).
- **Credential / injection boundary for the scanner child.** `child_environment` strips every `SONAR_*` plus
  `JAVA_TOOL_OPTIONS/_JAVA_OPTIONS/JDK_JAVA_OPTIONS` (`project_runner.py:25-29`); the scanner then receives only
  `SONAR_HOST_URL`/`SONAR_TOKEN` (`project_runner.py:226-227`). `SONAR_SCANNER_OPTS` is covered by the `SONAR_` prefix.
- **Faithful scope.** The runner copies the baseline effective properties (`dict(recipe["properties"])`) and only forces the
  trusted safety keys `sonar.qualitygate.wait/timeout` and `sonar.working.directory` (`project_runner.py:204-210`); it does not
  inject `sonar.inclusions`/Swift-only scope. The multi-language test asserts `sonar.sources=Sources,Tools` is preserved
  (once collectable).

## Residual risks to verify (consistent with the design's own blockers)

- **Global scanner precedence is unguarded.** Nothing addresses `sonar-scanner`'s global
  `$SONAR_SCANNER_HOME/conf/sonar-scanner.properties` or a cached launcher config overriding the generated
  `project.settings` plan. The design says to block publication if effective precedence cannot be established; the runner has
  no such guard or fixture. `JAVA_HOME` is also passed through (needed for the JVM but still a swap vector). This is a "local
  unsigned evidence, not a sandbox" limitation and should be stated, not silently assumed safe.
- **Snapshot vs generated artifacts.** `receipts.snapshot` only ignores top-level `.swift-sonar`/`.scannerwork`
  (`receipts.py:30`). A report written to a non-gitignored path would flip the before/after snapshot and make project-test
  fail with "Project inputs changed during tests". Adoption requires the consumer's report outputs to be gitignored; the
  fixture gets away with it via `.gitignore` entries for `build/`. Worth documenting as an adoption precondition.

## Environment note

`pyproject.toml` requires Python >= 3.11, and `cli.py` uses `list | None` annotations that fail on the system `python3`
(3.9). Use `python3.12` for all repros above. This is not a regression in the new code.

## Limits of this review

The headline blocker (P0-1) prevented executing the project test suite, so every config/property/inventory/native behavior
is assessed from code reading only, not from a passing run. No real xcresult fixture exists, so native-census compatibility
is unproven. HashFleet's actual effective properties, legacy `-D` flags, and test identities were not available and were not
inferred. These remain required follow-ups before any parity claim.
