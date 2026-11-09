"""Project workflow acceptance and failure gates. SPDX-License-Identifier: GPL-3.0-only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from swift_sonar_kit import cli
from swift_sonar_kit import project_runner as runner


class ProjectRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swift sonar project ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ("Sources", "Tests", "Tools", "build"):
            (self.root / name).mkdir()
        (self.root / "Sources/main.swift").write_text("let answer = 42\n")
        (self.root / "Tests/ExampleTests.swift").write_text("// test\n")
        (self.root / "Tools/check.py").write_text("print('check')\n")
        (self.root / ".gitignore").write_text("build/\n.swift-sonar/\n")
        (self.root / "sonar-project.properties").write_text(
            "sonar.projectKey=example\nsonar.projectName=Example App\nsonar.projectVersion=2.0\n"
            "sonar.sources=Sources,Tools\nsonar.tests=Tests\nsonar.test.inclusions=Tests/**/*.swift\n"
            "sonar.exclusions=**/build/**\nsonar.python.version=3.12\n")
        # Keep the script literal explicit so the fixture executes a real process.
        (self.root / "Tools/run.py").write_text(
            "from pathlib import Path\nPath('build/coverage.xml').write_text("
            + repr('<coverage version="1"><file path="Sources/main.swift"><lineToCover lineNumber="1" covered="true"/></file></coverage>') + ")\n")
        self.config = Path("swift-sonar.json")
        self.recipe = {"schema": 1, "properties": "sonar-project.properties",
                       "scannerProperties": {"sonar.coverageReportPaths": "build/coverage.xml"},
                       "test": {"argv": [sys.executable, "Tools/run.py"], "artifacts": [
                           {"kind": "generic-coverage", "path": "build/coverage.xml"}]},
                       "lint": {"argv": ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"], "import": "native"},
                       "tools": {"swiftlint": "0.65.1", "sonarScanner": "8.3.0", "sonarApple": "0.5.1"}}
        self.save_recipe()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.tools = {"kit": "test", "swiftlint": {"path": "swiftlint"}, "sonarScanner": {"path": "sonar-scanner"}}
        self.probe = patch.object(runner, "toolchain", return_value=self.tools)
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def save_recipe(self):
        (self.root / self.config).write_text(json.dumps(self.recipe))

    def run_recipe(self):
        return runner.project_test(self.root, self.config)

    def scan(self, **kwargs):
        return runner.project_scan(self.root, self.config, **kwargs)

    def test_complete_preparation_preserves_multilanguage_scope_without_network(self):
        before = (self.root / "sonar-project.properties").read_bytes()
        self.assertEqual(self.run_recipe(), 0)
        with patch.object(runner, "_lint", return_value={"status": "passed", "findings": 0}), \
                patch.object(runner, "preflight", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.scan(prepare_only=True), 0)
        plan = json.loads((self.root / ".swift-sonar/project-plan.json").read_text())
        self.assertEqual(plan["properties"]["sonar.sources"], "Sources,Tools")
        self.assertEqual(plan["properties"]["sonar.projectVersion"], "2.0")
        self.assertIn("Tools/check.py", plan["inventory"]["sources"])
        self.assertNotIn("sonar.externalIssuesReportPaths", plan["properties"])
        self.assertNotIn("sonar.inclusions", plan["properties"])
        self.assertEqual(before, (self.root / "sonar-project.properties").read_bytes())

    def test_native_cache_is_prepared_before_freezing_and_then_bound(self):
        (self.root / "Tools/ids.json").write_text(json.dumps(["Suite/check()"]))
        self.recipe["test"]["artifacts"].append({"kind": "xcresult", "path": "build/Test.xcresult", "expectedTests": "Tools/ids.json"})
        self.save_recipe()
        script = self.root / "Tools/run.py"
        script.write_text(script.read_text() + "\nPath('build/Test.xcresult').mkdir()\nPath('build/Test.xcresult/Info.plist').write_text('fixture')\n")
        def read_native(bundle, *, allow_failed=False):
            cache = bundle / "database.sqlite3"
            if not cache.exists():
                cache.write_bytes(b"official-reader-cache-fixture")
            stamp = (bundle / "Info.plist").stat().st_mtime
            return {"reader": {"fixture": True}, "result": "Passed", "started_at": stamp, "finished_at": stamp,
                    "test_cases": 1, "test_instances": 1, "failed": 0, "passed": 1, "skipped": 0,
                    "expected_failures": 0, "identities": ["Suite/check()"], "shape": "single"}
        with patch.object(runner, "native_xcresult", side_effect=read_native) as prepare, \
                patch("swift_sonar_kit.project_artifacts.native_xcresult", side_effect=read_native), \
                patch.object(runner, "_lint", return_value={"status": "passed", "findings": 0}):
            self.assertEqual(self.run_recipe(), 0)
            prepare.assert_called_once_with(self.root / "build/Test.xcresult", allow_failed=False)
            self.assertEqual(self.scan(prepare_only=True), 0)
            evidence = json.loads((self.root / ".swift-sonar/project-tests.json").read_text())
            self.assertEqual(evidence["artifacts"]["build/Test.xcresult"]["digest"]["members"], 2)
            (self.root / "build/Test.xcresult/database.sqlite3").write_bytes(b"tampered-cache")
            with self.assertRaisesRegex(ValueError, "changed"):
                self.scan(prepare_only=True)

    def test_strict_findings_block_publication_before_preflight(self):
        self.assertEqual(self.run_recipe(), 0)
        with patch.object(runner, "_lint", return_value={"status": "findings", "findings": 1}), \
                patch.object(runner, "preflight", side_effect=AssertionError("must not publish")):
            self.assertEqual(self.scan(), 1)
        self.assertFalse(json.loads((self.root / ".swift-sonar/project-summary.json").read_text())["published"])

    def test_early_lint_is_only_rejection_not_a_test_receipt(self):
        with patch.object(runner, "_lint", return_value={"status": "passed", "findings": 0}) as lint:
            self.assertEqual(runner.project_lint_preflight(self.root, self.config)["status"], "passed")
        self.assertEqual(lint.call_args.kwargs["report_name"], "preflight-lint.json")
        self.assertFalse((self.root / ".swift-sonar/project-tests.json").exists())
        with patch.object(runner, "_lint", side_effect=AssertionError("receipt must be checked first")):
            with self.assertRaises(ValueError):
                self.scan(prepare_only=True)

    def test_early_lint_rejects_source_changes(self):
        def mutate(*args, **kwargs):
            (self.root / "Sources/main.swift").write_text("let changed = 43\n")
            return {"status": "passed", "findings": 0}
        with patch.object(runner, "_lint", side_effect=mutate), self.assertRaisesRegex(ValueError, "changed"):
            runner.project_lint_preflight(self.root, self.config)

    def test_changed_source_blocks_scan(self):
        self.assertEqual(self.run_recipe(), 0)
        (self.root / "Sources/main.swift").write_text("let changed = 1\n")
        with self.assertRaisesRegex(ValueError, "changed"), patch.object(runner, "_lint", side_effect=AssertionError()):
            self.scan(prepare_only=True)

    def test_changed_report_blocks_scan(self):
        self.assertEqual(self.run_recipe(), 0)
        report = self.root / "build/coverage.xml"
        report.write_text(report.read_text().replace('covered="true"', 'covered="false"'))
        with self.assertRaises(ValueError):
            self.scan(prepare_only=True)

    def test_changed_configuration_blocks_scan(self):
        self.assertEqual(self.run_recipe(), 0)
        self.recipe["scannerProperties"]["sonar.projectVersion"] = "3.0"
        self.save_recipe()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.scan(prepare_only=True)

    def test_preexisting_output_invalidates_old_success_without_deletion(self):
        self.assertEqual(self.run_recipe(), 0)
        with self.assertRaisesRegex(ValueError, "prior output"):
            self.run_recipe()
        self.assertFalse((self.root / ".swift-sonar/project-tests.json").exists())
        self.assertTrue((self.root / "build/coverage.xml").exists())

    def test_failed_recipe_cannot_leave_success_receipt(self):
        self.assertEqual(self.run_recipe(), 0)
        (self.root / "build/coverage.xml").unlink()
        (self.root / "Tools/run.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(self.run_recipe(), 7)
        self.assertFalse((self.root / ".swift-sonar/project-tests.json").exists())

    def test_mutation_during_lint_is_rejected(self):
        self.assertEqual(self.run_recipe(), 0)
        def mutate(*args):
            (self.root / "Sources/main.swift").write_text("let changed = 42\n")
            return {"status": "passed"}
        with patch.object(runner, "_lint", side_effect=mutate), self.assertRaisesRegex(ValueError, "changed"):
            self.scan(prepare_only=True)

    def test_schema_one_and_invalid_timestamps_cannot_satisfy_project_mode(self):
        for field, value in (("schema", 1), ("started_at", float("nan")), ("finished_at", time.time() + 600)):
            with self.subTest(field=field):
                if (self.root / "build/coverage.xml").exists():
                    (self.root / "build/coverage.xml").unlink()
                self.assertEqual(self.run_recipe(), 0)
                path = self.root / ".swift-sonar/project-tests.json"
                data = json.loads(path.read_text())
                data[field] = value
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    self.scan(prepare_only=True)

    def test_publish_uses_native_settings_and_sanitized_environment(self):
        self.assertEqual(self.run_recipe(), 0)
        with patch.object(runner, "_lint", return_value={"status": "passed"}), \
                patch.object(runner, "preflight", return_value={"status": "UP"}), \
                patch.object(runner, "verify_project", wraps=runner.verify_project), \
                patch.dict(os.environ, {"SONAR_TOKEN": "canary", "SONAR_SCANNER_OPTS": "bad", "JAVA_TOOL_OPTIONS": "bad"}):
            original = subprocess.run
            calls = []
            def execute(argv, **kwargs):
                if argv[0] != "sonar-scanner":
                    return original(argv, **kwargs)
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(runner.subprocess, "run", side_effect=execute):
                self.assertEqual(self.scan(project_key="isolated"), 0)
        argv, options = calls[0]
        self.assertNotIn("canary", " ".join(argv))
        self.assertEqual(options["env"]["SONAR_TOKEN"], "canary")
        self.assertNotIn("SONAR_SCANNER_OPTS", options["env"])
        self.assertNotIn("JAVA_TOOL_OPTIONS", options["env"])
        self.assertIn("-Dsonar.qualitygate.wait=true", argv)
        self.assertEqual(options["cwd"], self.root)
        for path in (self.root / ".swift-sonar").glob("*.json"):
            self.assertNotIn("canary", path.read_text())

    def test_lint_malformed_and_failed_tools_are_not_clean(self):
        for code, text in ((0, '{}'), (2, '[]'), (1, '[]'), (0, 'not json')):
            with self.subTest(code=code, text=text), \
                    patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], code, text, "")), \
                    self.assertRaises(ValueError):
                runner._lint(self.root, self.recipe, self.tools)

    def test_project_cli_dispatch_and_missing_root(self):
        with patch.object(runner, "project_test", return_value=0) as test, \
                patch.object(runner, "project_scan", return_value=0) as scan:
            self.assertEqual(cli.main(["project-run", "--root", str(self.root), "--prepare-only"]), 0)
            test.assert_called_once()
            scan.assert_called_once()
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["project-scan"])

    def failed_evidence(self):
        script = self.root / "Tools/run.py"
        script.write_text(script.read_text() + "raise SystemExit(1)\n")
        self.assertEqual(runner.project_test(self.root, self.config, record_failures=True), 1)
        evidence = json.loads((self.root / ".swift-sonar/project-tests.json").read_text())
        self.assertEqual(evidence["test_status"], "failed")
        self.assertEqual(evidence["exit_code"], 1)

    def test_failed_evidence_requires_explicit_diagnostic_flag(self):
        self.failed_evidence()
        with patch.object(runner, "preflight", side_effect=AssertionError("no publication")), self.assertRaises(ValueError):
            self.scan()
        with patch.object(runner, "_lint", return_value={"status": "passed"}), \
                patch.object(runner, "preflight", side_effect=AssertionError("no network")):
            self.assertEqual(self.scan(prepare_only=True, publish_failed_tests=True), 1)
        plan = json.loads((self.root / ".swift-sonar/project-plan.json").read_text())
        self.assertTrue(plan["diagnostic_failed_tests"])
        self.assertEqual(plan["test_status"], "failed")

    def test_green_server_cannot_hide_failed_test_evidence(self):
        self.failed_evidence()
        original = subprocess.run
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0) if argv[0] == "sonar-scanner" else original(argv, **kwargs)
        with patch.object(runner, "_lint", return_value={"status": "passed"}), \
                patch.object(runner, "preflight", return_value={"status": "UP"}), \
                patch.object(runner.subprocess, "run", side_effect=execute):
            self.assertEqual(self.scan(publish_failed_tests=True), 1)
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["quality_gate"], "passed")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["test_exit_code"], 1)

    def test_cli_only_continues_failed_recipe_with_explicit_diagnostic_option(self):
        for flag in (False, True):
            with self.subTest(flag=flag), patch.object(runner, "project_test", return_value=1) as test, \
                    patch.object(runner, "project_scan", return_value=1) as scan:
                argv = ["project-run", "--root", str(self.root)]
                if flag:
                    argv.append("--publish-failed-tests")
                self.assertEqual(cli.main(argv), 1)
                self.assertEqual(test.call_args.kwargs["record_failures"], flag)
                self.assertEqual(scan.call_count, int(flag))
                if flag:
                    self.assertTrue(scan.call_args.kwargs["publish_failed_tests"])

    def test_child_environment_rejects_credentials_and_injection(self):
        with patch.dict(os.environ, {"SONAR_TOKEN": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
                                    "JAVA_TOOL_OPTIONS": "bad", "PATH": "/usr/bin"}, clear=True):
            self.assertEqual(runner.child_environment(), {"PATH": "/usr/bin"})

    def test_child_environment_anchors_relative_module_paths(self):
        with patch.dict(os.environ, {"PYTHONPATH": "src" + os.pathsep + ""}, clear=True):
            environment = runner.child_environment()
        expected = os.pathsep.join((str(Path.cwd() / "src"), str(Path.cwd())))
        self.assertEqual(environment["PYTHONPATH"], expected)
        completed = subprocess.run(
            [sys.executable, "-c", "import swift_sonar_kit; print(swift_sonar_kit.__version__)"],
            cwd=self.root, env={**runner.child_environment(), **environment},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_default_continue_on_lint_blocks_when_findings_exist(self):
        self.assertEqual(self.run_recipe(), 0)
        with patch.object(runner, "_lint", return_value={"status": "findings", "findings": 3, "exit_code": 2, "import": "native"}), \
                patch.object(runner, "preflight", side_effect=AssertionError("must not call preflight")):
            self.assertEqual(self.scan(), 1)
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["status"], "blocked")
        self.assertFalse(summary["published"])
        self.assertEqual(summary["lint"]["findings"], 3)
        self.assertFalse((self.root / ".swift-sonar/project-plan.json").exists())

    def test_opted_findings_prepare_only_writes_plan_and_diagnostic_summary(self):
        self.assertEqual(self.run_recipe(), 0)
        with patch.object(runner, "_lint", return_value={"status": "findings", "findings": 2, "exit_code": 2, "import": "native"}), \
                patch.object(runner, "preflight", side_effect=AssertionError("must not call preflight")):
            self.assertEqual(self.scan(prepare_only=True, continue_on_lint=True), 1)
        plan = json.loads((self.root / ".swift-sonar/project-plan.json").read_text())
        self.assertTrue(plan["diagnostic_lint_continuation"])
        self.assertEqual(plan["lint"]["findings"], 2)
        self.assertFalse(plan["published"])
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["exit_code"], 1)
        self.assertIsNone(summary["scanner_exit_code"])
        self.assertFalse(summary["published"])
        self.assertFalse(summary["publication_attempted"])
        self.assertTrue(summary["diagnostic_lint_continuation"])
        self.assertEqual(summary["lint"]["findings"], 2)

    def test_opted_findings_full_scan_records_published_qualitygate_but_fails_overall(self):
        self.assertEqual(self.run_recipe(), 0)
        original = subprocess.run
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0) if argv[0] == "sonar-scanner" else original(argv, **kwargs)
        with patch.object(runner, "_lint", return_value={"status": "findings", "findings": 2, "exit_code": 2, "import": "native"}), \
                patch.object(runner, "preflight", return_value={"status": "UP"}), \
                patch.object(runner.subprocess, "run", side_effect=execute):
            self.assertEqual(self.scan(continue_on_lint=True), 1)
        plan = json.loads((self.root / ".swift-sonar/project-plan.json").read_text())
        self.assertTrue(plan["diagnostic_lint_continuation"])
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["exit_code"], 1)
        self.assertEqual(summary["scanner_exit_code"], 0)
        self.assertTrue(summary["published"])
        self.assertEqual(summary["quality_gate"], "passed")
        self.assertTrue(summary["diagnostic_lint_continuation"])
        self.assertEqual(summary["lint"]["findings"], 2)
        self.assertTrue(summary["publication_attempted"])

    def test_opted_findings_scanner_failure_records_failed_gate_and_exit_code(self):
        self.assertEqual(self.run_recipe(), 0)
        original = subprocess.run
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2) if argv[0] == "sonar-scanner" else original(argv, **kwargs)
        with patch.object(runner, "_lint", return_value={"status": "findings", "findings": 2, "exit_code": 2, "import": "native"}), \
                patch.object(runner, "preflight", return_value={"status": "UP"}), \
                patch.object(runner.subprocess, "run", side_effect=execute):
            self.assertEqual(self.scan(continue_on_lint=True), 1)
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["exit_code"], 1)
        self.assertEqual(summary["scanner_exit_code"], 2)
        self.assertIsNone(summary["published"])
        self.assertEqual(summary["quality_gate"], "not-passed")
        self.assertTrue(summary["diagnostic_lint_continuation"])

    def test_continue_on_lint_never_bypasses_tool_execution_or_malformed_errors(self):
        self.assertEqual(self.run_recipe(), 0)
        original_run = runner.subprocess.run
        for code, text in ((1, '[]'), (0, 'not json'), (0, '[{"invalid": true}]')):
            def dispatch(argv, **kwargs):
                if argv and argv[0] == self.tools["swiftlint"]["path"]:
                    return subprocess.CompletedProcess(argv, code, text, "")
                return original_run(argv, **kwargs)

            with self.subTest(code=code, text=text), \
                    patch.object(runner.subprocess, "run", side_effect=dispatch), \
                    patch.object(runner, "preflight", side_effect=AssertionError("preflight must not be called")) as preflight, \
                    self.assertRaises(ValueError):
                self.scan(continue_on_lint=True)
            preflight.assert_not_called()

        with patch.object(runner, "_lint", return_value={"status": "unknown"}), \
                patch.object(runner, "preflight", side_effect=AssertionError("preflight must not be called")) as preflight, \
                self.assertRaises(ValueError):
            self.scan(continue_on_lint=True)
        preflight.assert_not_called()

    def test_continue_on_lint_never_bypasses_stale_or_missing_receipt(self):
        with self.assertRaises(ValueError):
            self.scan(continue_on_lint=True)
        self.assertEqual(self.run_recipe(), 0)
        receipt = self.root / ".swift-sonar/project-tests.json"
        data = json.loads(receipt.read_text())
        data["finished_at"] = time.time() - 7200
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.scan(continue_on_lint=True, max_age=3600)

    def test_opted_in_clean_lint_has_false_diagnostic_flag_and_passes(self):
        self.assertEqual(self.run_recipe(), 0)
        original = subprocess.run
        def execute(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0) if argv[0] == "sonar-scanner" else original(argv, **kwargs)
        with patch.object(runner, "_lint", return_value={"status": "passed", "findings": 0, "exit_code": 0, "import": "native"}), \
                patch.object(runner, "preflight", return_value={"status": "UP"}), \
                patch.object(runner.subprocess, "run", side_effect=execute):
            self.assertEqual(self.scan(continue_on_lint=True), 0)
        plan = json.loads((self.root / ".swift-sonar/project-plan.json").read_text())
        self.assertFalse(plan["diagnostic_lint_continuation"])
        summary = json.loads((self.root / ".swift-sonar/project-summary.json").read_text())
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["scanner_exit_code"], 0)
        self.assertTrue(summary["published"])
        self.assertFalse(summary["diagnostic_lint_continuation"])

    def test_job_lock_rejects_concurrent_writer(self):
        with runner._job(self.root), self.assertRaisesRegex(ValueError, "Another project job"):
            with runner._job(self.root):
                self.fail("second lock accepted")


class ProjectToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project tools ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = {}
        for name in ("swiftlint", "sonar-scanner", "run-tests"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
            self.paths[name] = str(path)
        self.config = self.root / "scanner.properties"
        self.config.write_text("# Unconfigured distribution\n")
        self.recipe = {"tools": {"swiftlint": "0.65.1", "sonarScanner": "8.3.0"},
                       "test": {"argv": ["./run-tests"], "artifacts": []}}

    def probe(self, argv, root):
        if Path(argv[0]).name == "swiftlint":
            return "0.65.1"
        return f"Scanner configuration file: {self.config}\nSonarScanner CLI 8.3.0.16463"

    def collect(self):
        with patch.object(runner.shutil, "which", side_effect=self.paths.get), \
                patch.object(runner, "_probe", side_effect=self.probe):
            return runner.toolchain(self.root, self.recipe)

    def test_repository_relative_executable_and_version_are_bound(self):
        evidence = self.collect()
        self.assertEqual(evidence["testExecutable"]["path"], self.paths["run-tests"])
        self.assertEqual(evidence["sonarScanner"]["version"], "8.3.0.16463")
        self.assertIn("sha256", evidence["scannerGlobalConfig"])
        (self.root / "run-tests").write_text("#!/bin/sh\nexit 1\n")
        self.assertNotEqual(evidence["testExecutable"], self.collect()["testExecutable"])

    def test_global_settings_cannot_inject_unreviewed_properties(self):
        self.config.write_text("# safe comment\nsonar.sources=elsewhere\n")
        with self.assertRaisesRegex(ValueError, "unconfigured scanner"):
            self.collect()

    def test_version_and_build_environment_changes_are_detectable(self):
        with patch.dict(os.environ, {"HF_IOS_SCHEME": "first"}):
            first = self.collect()
        with patch.dict(os.environ, {"HF_IOS_SCHEME": "second"}):
            self.assertNotEqual(first["buildEnvironment"], self.collect()["buildEnvironment"])
        self.recipe["tools"]["swiftlint"] = "9.9.9"
        with self.assertRaisesRegex(ValueError, "configured version"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
