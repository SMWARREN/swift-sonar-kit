"""Retained legacy orchestration boundaries, not the public analysis-only default.

SPDX-License-Identifier: GPL-3.0-only.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from swift_sonar_kit import analysis_runner, cli, console, workflow
from swift_sonar_kit import project_runner as runner


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable sonar workflow ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "Sources").mkdir()
        (self.root / "Sources/Example.swift").write_text("let example = 42\n")
        (self.root / "Tests").mkdir()
        (self.root / "Tests/ExampleTests.swift").write_text("// tests\n")
        (self.root / ".gitignore").write_text("build/\n.swift-sonar/\n")
        (self.root / "sonar-project.properties").write_text(
            "sonar.projectKey=example-project\nsonar.projectName=Example Project\n"
            "sonar.sources=Sources\nsonar.tests=Tests\n"
            "sonar.coverageReportPaths=build/audit/coverage.xml\n")
        self.recipe = {"schema": 1, "properties": "sonar-project.properties",
                       "test": {"argv": [sys.executable, "-c", "pass"], "artifacts": [
                           {"kind": "generic-coverage", "path": "build/audit/coverage.xml"}]},
                       "tools": {"swiftlint": "0.65.1", "sonarScanner": "8.3.1", "sonarApple": "0.5.1"}}
        self.config = Path("swift-sonar.json")
        self.save_recipe()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.args = cli.parser().parse_args(["run", str(self.root)])
        self.legacy_defaults(self.args)
        self.args.url = None
        self.plan = {"status": "ready", "kind": "existing", "config_path": self.config}
        for name in ("discover", "prepare"):
            mock = patch("swift_sonar_kit.automatic." + name, return_value=self.plan)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        self.test = patch.object(workflow, "project_test", return_value=0).start()
        self.lint = patch.object(workflow, "project_lint_preflight", return_value={"status": "passed", "findings": 0}).start()
        self.scan = patch.object(workflow, "project_scan", side_effect=self.fake_scan).start()
        self.local_server = patch("swift_sonar_kit.local_server.ensure_local_server",
                                  return_value={"url": "http://127.0.0.1:9123", "token": "private-test-token"}).start()
        self.setup = patch.object(workflow, "_setup_environment",
                                  side_effect=lambda *args: contextlib.nullcontext({"status": "ready"})).start()
        self.addCleanup(patch.stopall)

    def save_recipe(self):
        (self.root / self.config).write_text(json.dumps(self.recipe))

    @staticmethod
    def legacy_defaults(args):
        # These options no longer belong to public `run`. Keep the old internal
        # workflow exercised with its original explicit contract and assertions.
        args.max_test_age = 3600
        args.no_archive = False
        args.publish_failed_tests = False

    def legacy_output(self, argv):
        """Exercise the old workflow's rendering, not the new CLI dispatch."""
        args = cli.parser().parse_args(argv)
        self.legacy_defaults(args)
        with console.run_output(as_json=args.as_json) as output:
            code = workflow.run(args)
            return output.finish(code, root=args.root, save=not args.plan_only, setup_only=args.setup_only)

    def fake_scan(self, root, config, **kwargs):
        directory = root / ".swift-sonar"
        directory.mkdir(exist_ok=True)
        (directory / "project-plan.json").write_text(json.dumps({
            "schema": 1, "recipe": workflow.load_project(root, config)["digest"],
            "lint": {"status": "passed", "findings": 0},
            "diagnostic_lint_continuation": False,
            "diagnostic_failed_tests": bool(self.test.return_value and kwargs.get("publish_failed_tests")),
        }))
        if not kwargs.get("prepare_only"):
            self.assertEqual(os.environ.get("SONAR_TOKEN"), "private-test-token")
            (directory / "project-summary.json").write_text(json.dumps(
                {"publication_attempted": True, "scanner_exit_code": 0}))
        return 0

    def run_workflow(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return workflow.run(self.args)

    def result(self):
        return json.loads((self.root / ".swift-sonar/run-summary.json").read_text())

    def previous(self):
        output = self.root / "build/audit"
        output.mkdir(parents=True, exist_ok=True)
        (output / "coverage.xml").write_text("previous-coverage")
        (output / "test.log").write_text("retained-evidence")
        return output

    def test_path_only_generates_and_uses_project_owned_server_after_local_gates(self):
        self.assertEqual(self.run_workflow(), 0)
        self.assertEqual(self.scan.call_count, 2)
        self.assertTrue(self.scan.call_args_list[0].kwargs["prepare_only"])
        self.local_server.assert_called_once_with(self.root, "example-project")
        self.assertEqual(self.result()["status"], "passed")
        self.assertTrue(self.result()["published"])
        self.assertNotIn("private-test-token", json.dumps(self.result()))

    def test_prepare_only_does_not_create_or_contact_server(self):
        self.args.prepare_only = True
        self.assertEqual(self.run_workflow(), 0)
        self.local_server.assert_not_called()
        self.assertEqual(self.scan.call_count, 1)
        self.assertEqual(self.result()["status"], "prepared")
        self.assertFalse(self.result()["published"])

    def test_plan_only_is_read_only(self):
        self.args.plan_only = True
        self.assertEqual(self.run_workflow(), 0)
        self.prepare.assert_not_called()
        self.test.assert_not_called()
        self.local_server.assert_not_called()
        self.setup.assert_not_called()
        self.assertFalse((self.root / ".swift-sonar").exists())

    def test_setup_only_never_runs_analysis_or_archives_prior_evidence(self):
        self.args.setup_only = True
        self.previous()
        directory = self.root / ".swift-sonar"
        directory.mkdir()
        (directory / "run-summary.json").write_text("prior summary")
        self.assertEqual(self.run_workflow(), 0)
        self.setup.assert_called_once()
        self.prepare.assert_not_called()
        self.lint.assert_not_called()
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertEqual((self.root / "build/audit/coverage.xml").read_text(), "previous-coverage")
        self.assertEqual((directory / "run-summary.json").read_text(), "prior summary")
        self.assertFalse((directory / "history").exists())

    def test_setup_failure_stops_before_preparation_or_tests(self):
        self.setup.side_effect = ValueError("dependency download failed")
        with self.assertRaisesRegex(ValueError, "dependency download failed"):
            self.run_workflow()
        self.prepare.assert_not_called()
        self.lint.assert_not_called()
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()

    def test_setup_only_failure_preserves_prior_project_summary(self):
        self.args.setup_only = True
        directory = self.root / ".swift-sonar"
        directory.mkdir()
        previous = directory / "project-summary.json"
        previous.write_text("previous scan summary")
        self.setup.side_effect = ValueError("dependency setup failed")
        with self.assertRaisesRegex(ValueError, "dependency setup failed"):
            self.run_workflow()
        self.assertEqual(previous.read_text(), "previous scan summary")
        self.test.assert_not_called()
        self.scan.assert_not_called()

    def test_unsupported_discovery_does_not_download_tools(self):
        self.discover.return_value = {"status": "review_needed", "reason": "ambiguous project"}
        with self.assertRaisesRegex(ValueError, "ambiguous project"):
            self.run_workflow()
        self.setup.assert_not_called()

    def test_setup_precedes_recipe_preparation_and_local_gates(self):
        events = []
        self.setup.side_effect = lambda *args: events.append("setup") or contextlib.nullcontext({"status": "ready"})
        self.prepare.side_effect = lambda *args, **kwargs: events.append("prepare") or self.plan
        self.lint.side_effect = lambda *args: events.append("lint") or {"status": "passed", "findings": 0}
        self.test.side_effect = lambda *args, **kwargs: events.append("tests") or 0
        self.args.prepare_only = True
        self.assertEqual(self.run_workflow(), 0)
        self.assertEqual(events, ["setup", "prepare", "lint", "tests"])
        self.assertEqual(self.result()["setup"], {"status": "ready"})

    def test_setup_only_cli_saves_separately_in_both_output_modes(self):
        directory = self.root / ".swift-sonar"
        directory.mkdir()
        previous = directory / "run-result.json"
        previous.write_text('{"user_scan":"untouched"}')
        for as_json in (False, True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = self.legacy_output(["run", str(self.root), "--setup-only"] + (["--json"] if as_json else []))
            self.assertEqual(code, 0)
            saved = json.loads((directory / "setup-result.json").read_text())
            self.assertEqual(saved["stage"], "dependency-setup")
            self.assertFalse(saved["published"])
            self.assertEqual(previous.read_text(), '{"user_scan":"untouched"}')
            if as_json:
                self.assertEqual(json.loads(stdout.getvalue()), saved)
        self.prepare.assert_not_called()
        self.test.assert_not_called()

    def test_setup_only_conflicting_modes_are_rejected_before_work(self):
        for flag in ("--plan-only", "--prepare-only"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(["run", str(self.root), "--setup-only", flag])
            self.assertEqual(raised.exception.code, 2)
        self.setup.assert_not_called()

    def test_setup_environment_uses_reviewed_pins_and_declared_coverage(self):
        patch.stopall()
        for coverage in (False, True):
            recipe = {"tools": self.recipe["tools"], "test": {"artifacts": [
                {"kind": "python-coverage" if coverage else "generic-coverage", "path": "build/audit/report.xml"}]}}
            with patch("swift_sonar_kit.bootstrap.prepared_environment") as prepare:
                workflow._setup_environment(self.root, {"recipe": recipe})
            prepare.assert_called_once_with(self.root, tools=self.recipe["tools"], python_coverage=coverage)

    def test_unsupported_plan_is_nonzero(self):
        self.args.plan_only = True
        self.discover.return_value = {"status": "unsupported"}
        self.assertEqual(self.run_workflow(), 2)

    def test_failed_tests_block_lint_and_server(self):
        self.test.return_value = 7
        self.assertEqual(self.run_workflow(), 7)
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertFalse(self.result()["published"])
        self.assertEqual(self.result()["stage"], "tests")

    def test_preflight_lint_findings_block_before_tests_archival_and_server(self):
        self.previous()
        self.lint.return_value = {"status": "findings", "findings": 29}
        self.assertEqual(self.run_workflow(), 1)
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertEqual((self.root / "build/audit/coverage.xml").read_text(), "previous-coverage")
        self.assertEqual(self.result()["tests"], "not-run")
        self.assertFalse(self.result()["published"])

    def formatting_fixture(self, *, as_json=False, count=1):
        """Use Git plus a fake lint process, never installed analysis tools."""
        findings = [{"file": str(self.root / "Sources/Example.swift"), "line": 1,
                     "character": 5, "severity": "Warning", "rule_id": "trailing_comma",
                     "reason": "Collection literals should have trailing commas"}
                    for _ in range(count)]
        original = subprocess.run

        def execute(argv, **kwargs):
            if argv[0] == "fixture-swiftlint":
                self.assertIn("--strict", argv)
                self.assertIn("json", argv)
                return subprocess.CompletedProcess(argv, 2, json.dumps(findings), "")
            if Path(argv[0]).name != "git":
                raise AssertionError("Real tools are forbidden in formatting fixtures")
            return original(argv, **kwargs)

        self.lint.side_effect = runner.project_lint_preflight
        stdout, stderr = io.StringIO(), io.StringIO()
        source_before = (self.root / "Sources/Example.swift").read_bytes()
        with patch.object(runner, "toolchain", return_value={"swiftlint": {"path": "fixture-swiftlint"}}), \
                patch.object(runner.subprocess, "run", side_effect=execute), \
                patch.dict(os.environ, {"SONAR_HOST_URL": ""}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            argv = ["run", str(self.root)] + (["--json"] if as_json else [])
            code = self.legacy_output(argv)
        self.assertEqual((self.root / "Sources/Example.swift").read_bytes(), source_before)
        self.assertEqual(json.loads((self.root / ".swift-sonar/preflight-lint.json").read_text()), findings)
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertFalse(self.result()["published"])
        self.assertEqual(self.result()["tests"], "not-run")
        saved = json.loads((self.root / ".swift-sonar/run-result.json").read_text())
        self.assertEqual(saved["findings"], findings)
        self.assertEqual(saved["exit_code"], code)
        self.assertFalse(saved["published"])
        if as_json:
            self.assertEqual(json.loads(stdout.getvalue()), saved)
        return code, stdout.getvalue(), stderr.getvalue(), findings

    def test_legacy_output_lists_findings_in_table_without_later_stages(self):
        code, stdout, _, _ = self.formatting_fixture()
        self.assertEqual(code, 1)
        for word in ("File", "Line", "Severity", "Rule", "Message", "trailing_comma", "Collection"):
            self.assertIn(word, stdout)
        self.assertIn(".swift-sonar/preflight-lint.json", stdout)
        self.assertNotIn(str(self.root / "Sources/Example.swift"), stdout)

    def test_legacy_json_contains_all_findings_and_no_table_on_stdout(self):
        code, stdout, stderr, findings = self.formatting_fixture(as_json=True, count=205)
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["stage"], "lint-preflight")
        self.assertEqual(payload["exit_code"], 1)
        self.assertEqual(payload["findings"], findings)
        self.assertEqual(len(payload["findings"]), 205)
        self.assertNotIn("| File", stdout)
        self.assertIn("publication were not started", stderr)

    def test_legacy_output_reports_table_row_limit_without_losing_saved_findings(self):
        code, stdout, _, findings = self.formatting_fixture(count=205)
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 205)
        self.assertIn("omitted", stdout)
        self.assertIn(".swift-sonar/preflight-lint.json", stdout)

    def test_legacy_json_preserves_test_failure_exit_code(self):
        self.test.return_value = 7
        stdout = io.StringIO()
        with patch.dict(os.environ, {"SONAR_HOST_URL": ""}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = self.legacy_output(["run", str(self.root), "--json"])
        self.assertEqual(code, 7)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["test_exit_code"], 7)
        self.assertEqual(payload["stage"], "tests")
        self.assertFalse(payload["published"])
        self.assertEqual(json.loads((self.root / ".swift-sonar/run-result.json").read_text()), payload)
        self.scan.assert_not_called()
        self.local_server.assert_not_called()

    def test_json_cli_prepare_success_remains_local(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"SONAR_HOST_URL": ""}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = self.legacy_output(["run", str(self.root), "--prepare-only", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "prepared")
        self.assertEqual(payload["exit_code"], 0)
        self.assertFalse(payload["published"])
        self.assertEqual(json.loads((self.root / ".swift-sonar/run-result.json").read_text()), payload)
        self.local_server.assert_not_called()
        self.assertNotIn("private-test-token", stdout.getvalue())

    def test_cli_plan_only_renders_table_or_json_without_writes(self):
        for as_json in (False, True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                argv = ["run", str(self.root), "--plan-only"] + (["--json"] if as_json else [])
                self.assertEqual(self.legacy_output(argv), 0)
            if as_json:
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["status"], "ready")
                self.assertEqual(payload["config_path"], "swift-sonar.json")
            else:
                self.assertIn("| Key", stdout.getvalue())
                self.assertIn("ready", stdout.getvalue())
        self.prepare.assert_not_called()
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertFalse((self.root / ".swift-sonar").exists())

    def test_lint_failure_blocks_server_even_if_prior_plan_exists(self):
        (self.root / ".swift-sonar").mkdir()
        (self.root / ".swift-sonar/project-plan.json").write_text("{}")
        self.scan.side_effect = None
        self.scan.return_value = 1
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()
        self.assertEqual(self.result()["stage"], "local-validation")

    def test_failed_diagnostics_remain_failed_after_scanner_acceptance(self):
        self.args.publish_failed_tests = True
        self.test.return_value = 1
        def failed_scan(root, config, **kwargs):
            self.fake_scan(root, config, **kwargs)
            return 1
        self.scan.side_effect = failed_scan
        self.assertEqual(self.run_workflow(), 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertTrue(self.result()["published"])

    def test_failed_diagnostics_without_valid_plan_never_start_server(self):
        self.args.publish_failed_tests = True
        self.test.return_value = 1
        self.scan.side_effect = None
        self.scan.return_value = 1
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()

    def diagnostic_scan(self, root, config, **kwargs):
        self.fake_scan(root, config, **kwargs)
        path = root / ".swift-sonar/project-plan.json"
        plan = json.loads(path.read_text())
        plan.update(lint={"status": "findings", "findings": 2}, diagnostic_lint_continuation=True)
        path.write_text(json.dumps(plan))
        if not kwargs.get("prepare_only"):
            (root / ".swift-sonar/project-summary.json").write_text(json.dumps({
                "publication_attempted": True, "scanner_exit_code": 0, "lint": plan["lint"],
            }))
        return 1

    def test_opted_preflight_and_final_findings_publish_but_remain_failed(self):
        self.args.continue_on_lint = True
        self.lint.return_value = {"status": "findings", "findings": 3}
        self.scan.side_effect = self.diagnostic_scan
        self.assertEqual(self.run_workflow(), 1)
        self.assertEqual(self.scan.call_count, 2)
        self.assertTrue(all(call.kwargs["continue_on_lint"] for call in self.scan.call_args_list))
        self.local_server.assert_called_once()
        result = self.result()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["scanner_exit_code"], 0)
        self.assertTrue(result["published"])
        self.assertTrue(result["diagnostic_lint_continuation"])
        self.assertEqual(result["lint_preflight"]["findings"], 3)
        self.assertEqual(result["lint"]["findings"], 2)

    def test_preflight_findings_never_disappear_when_final_lint_is_clean(self):
        self.args.continue_on_lint = True
        self.lint.return_value = {"status": "findings", "findings": 3}
        self.assertEqual(self.run_workflow(), 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertTrue(self.result()["published"])
        self.assertEqual(self.result()["scanner_exit_code"], 0)
        self.assertTrue(self.result()["diagnostic_lint_continuation"])
        self.assertEqual(self.result()["lint"]["findings"], 0)

    def test_final_only_findings_can_continue_without_bypassing_clean_preflight(self):
        self.args.continue_on_lint = True
        self.scan.side_effect = self.diagnostic_scan
        self.assertEqual(self.run_workflow(), 1)
        self.assertEqual(self.result()["lint_preflight"]["findings"], 0)
        self.assertEqual(self.result()["lint"]["findings"], 2)
        self.assertTrue(self.result()["published"])

    def test_diagnostic_prepare_only_is_nonzero_and_never_contacts_server(self):
        self.args.continue_on_lint = True
        self.args.prepare_only = True
        self.scan.side_effect = self.diagnostic_scan
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()
        self.assertEqual(self.scan.call_count, 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertFalse(self.result()["published"])

    def test_lint_continuation_does_not_imply_failed_test_permission(self):
        self.args.continue_on_lint = True
        self.lint.return_value = {"status": "findings", "findings": 3}
        self.test.return_value = 7
        self.assertEqual(self.run_workflow(), 7)
        self.scan.assert_not_called()
        self.local_server.assert_not_called()

    def test_lint_continuation_rejects_non_diagnostic_validation_failure(self):
        self.args.continue_on_lint = True
        def broken_validation(root, config, **kwargs):
            self.diagnostic_scan(root, config, **kwargs)
            return 2
        self.scan.side_effect = broken_validation
        self.assertEqual(self.run_workflow(), 2)
        self.local_server.assert_not_called()
        self.assertEqual(self.result()["stage"], "local-validation")

    def test_lint_continuation_rejects_stale_or_mismatched_diagnostic_plan(self):
        self.args.continue_on_lint = True
        self.args.no_archive = True
        directory = self.root / ".swift-sonar"
        directory.mkdir()
        plan_path = directory / "project-plan.json"
        plan_path.write_text(json.dumps({
            "schema": 1, "recipe": workflow.load_project(self.root, self.config)["digest"],
            "lint": {"status": "findings", "findings": 2}, "diagnostic_lint_continuation": True,
        }))
        self.scan.side_effect = None
        self.scan.return_value = 1
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()
        self.assertFalse(plan_path.exists())
        def mismatched(root, config, **kwargs):
            self.diagnostic_scan(root, config, **kwargs)
            plan = json.loads(plan_path.read_text())
            plan["recipe"] = "old-recipe"
            plan_path.write_text(json.dumps(plan))
            return 1
        self.scan.side_effect = mismatched
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()

    def test_lint_continuation_requires_explicit_plan_marker(self):
        self.args.continue_on_lint = True
        def unmarked(root, config, **kwargs):
            self.diagnostic_scan(root, config, **kwargs)
            plan_path = root / ".swift-sonar/project-plan.json"
            plan = json.loads(plan_path.read_text())
            plan.pop("diagnostic_lint_continuation")
            plan_path.write_text(json.dumps(plan))
            return 1
        self.scan.side_effect = unmarked
        self.assertEqual(self.run_workflow(), 1)
        self.local_server.assert_not_called()

    def test_lint_continuation_never_bypasses_tool_or_malformed_failures(self):
        self.args.continue_on_lint = True
        self.previous()
        self.lint.side_effect = ValueError("SwiftLint execution failed")
        with self.assertRaisesRegex(ValueError, "execution failed"):
            self.run_workflow()
        self.test.assert_not_called()
        self.scan.assert_not_called()
        self.local_server.assert_not_called()
        self.assertEqual((self.root / "build/audit/coverage.xml").read_text(), "previous-coverage")

    def test_lint_continuation_preserves_scanner_publication_uncertainty(self):
        self.args.continue_on_lint = True
        def scanner_failure(root, config, **kwargs):
            self.diagnostic_scan(root, config, **kwargs)
            if not kwargs.get("prepare_only"):
                (root / ".swift-sonar/project-summary.json").write_text(json.dumps({
                    "publication_attempted": True, "scanner_exit_code": 2,
                    "lint": {"status": "findings", "findings": 2},
                }))
            return 1
        self.scan.side_effect = scanner_failure
        self.assertEqual(self.run_workflow(), 1)
        self.assertIsNone(self.result()["published"])
        self.assertEqual(self.result()["scanner_exit_code"], 2)

    def test_continuation_plan_only_is_read_only_and_no_archive_preserves_reports(self):
        self.args.continue_on_lint = True
        self.args.plan_only = True
        self.assertEqual(self.run_workflow(), 0)
        self.assertFalse((self.root / ".swift-sonar").exists())
        self.test.assert_not_called()
        self.args.plan_only = False
        self.args.prepare_only = True
        self.args.no_archive = True
        self.previous()
        self.scan.side_effect = self.diagnostic_scan
        self.assertEqual(self.run_workflow(), 1)
        self.assertEqual((self.root / "build/audit/coverage.xml").read_text(), "previous-coverage")
        self.local_server.assert_not_called()

    def test_explicit_server_does_not_administer_docker_and_restores_environment(self):
        self.args.url = "http://127.0.0.1:9000"
        with patch.dict(os.environ, {"SONAR_TOKEN": "private-test-token", "SONAR_HOST_URL": "http://localhost:9999"}):
            self.assertEqual(self.run_workflow(), 0)
            self.assertEqual(os.environ["SONAR_HOST_URL"], "http://localhost:9999")
        self.local_server.assert_not_called()
        self.assertEqual(self.result()["server_url"], self.args.url)

    def test_explicit_server_without_token_fails_without_setup(self):
        self.args.url = "http://127.0.0.1:9000"
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "SONAR_TOKEN"):
            self.run_workflow()
        self.local_server.assert_not_called()
        self.prepare.assert_not_called()
        self.test.assert_not_called()
        self.assertFalse((self.root / ".swift-sonar").exists())

    def test_server_credentials_are_restored_on_scan_error(self):
        def fail_scan(root, config, **kwargs):
            if kwargs.get("prepare_only"):
                return self.fake_scan(root, config, **kwargs)
            raise ValueError("scanner failed")
        self.scan.side_effect = fail_scan
        with patch.dict(os.environ, {"SONAR_TOKEN": "original"}), self.assertRaisesRegex(ValueError, "scanner failed"):
            self.run_workflow()
        self.assertNotIn("private-test-token", (self.root / ".swift-sonar/run-summary.json").read_text())

    def test_scanner_gate_failure_does_not_assert_publication_was_absent(self):
        def fail_gate(root, config, **kwargs):
            self.fake_scan(root, config, **kwargs)
            if not kwargs.get("prepare_only"):
                (root / ".swift-sonar/project-summary.json").write_text(json.dumps(
                    {"publication_attempted": True, "scanner_exit_code": 2}))
                return 1
            return 0
        self.scan.side_effect = fail_gate
        self.assertEqual(self.run_workflow(), 1)
        self.assertIsNone(self.result()["published"])
        self.assertEqual(self.result()["publication_status"], "not-established")

    def test_prior_dedicated_reports_are_archived_not_deleted(self):
        output = self.previous()
        archive = workflow.archive_previous_outputs(self.root, self.config)
        self.assertFalse(output.exists())
        self.assertEqual((archive / "audit/coverage.xml").read_text(), "previous-coverage")
        self.assertEqual((archive / "audit/test.log").read_text(), "retained-evidence")
        self.assertTrue((self.root / "Sources/Example.swift").exists())

    def test_archive_rejects_tracked_output(self):
        self.previous()
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "build/audit/test.log"], check=True)
        with self.assertRaisesRegex(ValueError, "tracked"):
            workflow.archive_previous_outputs(self.root, self.config)
        self.assertTrue((self.root / "build/audit/test.log").exists())

    def test_archive_rejects_unignored_directory(self):
        self.previous()
        (self.root / ".gitignore").write_text(".swift-sonar/\n")
        with self.assertRaisesRegex(ValueError, "not ignored"):
            workflow.archive_previous_outputs(self.root, self.config)

    def test_archive_rejects_symlink_output_ancestor(self):
        (self.root / "Elsewhere").mkdir()
        (self.root / "build").symlink_to(self.root / "Elsewhere", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            workflow.archive_previous_outputs(self.root, self.config)

    def test_archive_does_not_move_independent_manifest(self):
        self.previous()
        (self.root / "build/audit/expected.json").write_text('["Example/check()"]')
        self.recipe["test"]["artifacts"].append({"kind": "xcresult", "path": "build/audit/native.xcresult",
                                                 "expectedTests": "build/audit/expected.json"})
        self.save_recipe()
        with self.assertRaisesRegex(ValueError, "manifest"):
            workflow.archive_previous_outputs(self.root, self.config)
        self.assertTrue((self.root / "build/audit/expected.json").exists())

    def test_lock_blocks_concurrent_automatic_workflow(self):
        with workflow._lock(self.root, "automatic.lock"), self.assertRaisesRegex(ValueError, "Another"):
            self.run_workflow()
        self.test.assert_not_called()

    def test_project_lock_is_held_during_preparation_tests_and_scanning(self):
        def assert_locked():
            code = ("import fcntl,sys\n"
                    "with open(sys.argv[1], 'a') as stream:\n"
                    " try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    " except BlockingIOError: sys.exit(42)\n")
            result = subprocess.run([sys.executable, "-c", code,
                                     str(self.root / ".swift-sonar/project.lock")], check=False)
            self.assertEqual(result.returncode, 42)
        def prepare(*args, **kwargs):
            assert_locked()
            return self.plan
        def test(*args, **kwargs):
            assert_locked()
            return 0
        def scan(*args, **kwargs):
            assert_locked()
            return self.fake_scan(*args, **kwargs)
        self.prepare.side_effect = prepare
        self.test.side_effect = test
        self.scan.side_effect = scan
        self.assertEqual(self.run_workflow(), 0)

    def test_archive_lock_rejects_active_project_job(self):
        self.previous()
        with workflow._lock(self.root, "project.lock"), self.assertRaisesRegex(ValueError, "Another"):
            workflow.archive_previous_outputs(self.root, self.config)
        self.assertTrue((self.root / "build/audit/coverage.xml").exists())

    def test_no_archive_leaves_previous_evidence_untouched(self):
        self.previous()
        self.args.no_archive = True
        self.args.prepare_only = True
        self.assertEqual(self.run_workflow(), 0)
        self.assertEqual((self.root / "build/audit/coverage.xml").read_text(), "previous-coverage")

    def test_summary_supports_terminal_and_json(self):
        self.args.prepare_only = True
        self.run_workflow()
        for as_json in (True, False):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(workflow.summary(self.root, as_json=as_json), 0)
            self.assertIn("Example Project", output.getvalue())
            self.assertNotIn("private-test-token", output.getvalue())

    def test_summary_rejects_symlink_and_unknown_schema(self):
        self.args.prepare_only = True
        self.run_workflow()
        path = self.root / ".swift-sonar/run-summary.json"
        path.write_text('{"schema":2}')
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            workflow.summary(self.root)
        path.unlink()
        path.symlink_to(self.root / "swift-sonar.json")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            workflow.summary(self.root)

    def test_cli_dispatches_path_with_spaces(self):
        with patch.object(analysis_runner, "run", return_value=0) as run:
            self.assertEqual(cli.main(["run", str(self.root), "--prepare-only"]), 0)
        self.assertEqual(run.call_args.args[0].root, self.root)
        self.assertTrue(run.call_args.args[0].prepare_only)


if __name__ == "__main__":
    unittest.main()
