"""Public continuation contract, using mocked workflows only.

SPDX-License-Identifier: GPL-3.0-only
"""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swift_sonar_kit import cli, console, project_runner
from swift_sonar_kit import analysis_runner as workflow


class LintContinuationCLITests(unittest.TestCase):
    def test_flag_is_explicit_and_supported_only_for_scanning_commands(self):
        for action in ("run", "project-scan", "project-run"):
            argv = [action, "/fixture"] if action == "run" else [action, "--root", "/fixture"]
            self.assertFalse(cli.parser().parse_args(argv).continue_on_lint)
            self.assertTrue(cli.parser().parse_args(argv + ["--continue-on-lint"]).continue_on_lint)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(["project-test", "--root", "/fixture", "--continue-on-lint"])

    def test_focused_commands_forward_flag_without_enabling_failed_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            for action in ("project-scan", "project-run"):
                with self.subTest(action=action), \
                        patch.object(project_runner, "project_test", return_value=0) as test, \
                        patch.object(project_runner, "project_scan", return_value=1) as scan:
                    code = cli.main([action, "--root", temp, "--continue-on-lint", "--prepare-only"])
                    self.assertEqual(code, 1)
                    self.assertTrue(scan.call_args.kwargs["continue_on_lint"])
                    self.assertTrue(scan.call_args.kwargs["prepare_only"])
                    self.assertFalse(scan.call_args.kwargs["publish_failed_tests"])
                    self.assertEqual(test.call_count, int(action == "project-run"))

    def test_failed_tests_still_stop_focused_run_before_scan(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(project_runner, "project_test", return_value=7), \
                patch.object(project_runner, "project_scan") as scan:
            self.assertEqual(cli.main(["project-run", "--root", temp, "--continue-on-lint"]), 7)
            scan.assert_not_called()

    def test_both_output_modes_save_each_stage_and_do_not_offer_corrections(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json), tempfile.TemporaryDirectory(prefix="continuation CLI ") as temp:
                root = Path(temp).resolve()
                preflight = [{"file": str(root / "Sources/A.swift"), "line": 1, "rule_id": "identifier_name",
                              "severity": "Error", "reason": "Preflight finding"}]
                final = [{"file": str(root / "Sources/B.swift"), "line": 2, "rule_id": "line_length",
                          "severity": "Error", "reason": "Final finding"}]

                def execute(args):
                    self.assertTrue(args.continue_on_lint)
                    self.assertFalse(hasattr(args, "publish_failed_tests"))
                    console.present_findings(preflight, root, "preflight-lint.json")
                    console.present_findings(final, root, "project-lint.json")
                    console.remember_lint_tool({"path": "fixture-swiftlint"})
                    console.remember_result({"status": "failed", "stage": "scan", "published": True,
                                             "scanner_exit_code": 0, "diagnostic_lint_continuation": True})
                    return 1

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(workflow, "run", side_effect=execute), \
                        patch("swift_sonar_kit.corrections.offer_corrections") as corrections, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    argv = ["run", temp, "--continue-on-lint"] + (["--json"] if as_json else [])
                    self.assertEqual(cli.main(argv), 1)
                saved = json.loads((root / ".swift-sonar/run-result.json").read_text())
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["exit_code"], 1)
                self.assertEqual(saved["scanner_exit_code"], 0)
                self.assertTrue(saved["published"])
                self.assertEqual(saved["findings"], final)
                self.assertEqual(saved["lint_reports"], {"preflight-lint.json": preflight, "project-lint.json": final})
                corrections.assert_not_called()
                if as_json:
                    self.assertEqual(json.loads(stdout.getvalue()), saved)
                    self.assertNotIn("| File", stdout.getvalue())
                else:
                    self.assertIn("Preflight finding", stdout.getvalue())
                    self.assertIn("Final finding", stdout.getvalue())
                    self.assertIn("JSON report", stdout.getvalue())

    def test_empty_final_report_does_not_erase_preflight_findings(self):
        stdout = io.StringIO()
        findings = [{"rule_id": "line_length", "reason": "Earlier finding"}]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with console.run_output(as_json=True) as output:
                console.present_findings(findings, Path("/fixture"), "preflight-lint.json")
                console.present_findings([], Path("/fixture"), "project-lint.json")
                console.remember_result({"status": "failed"})
            output.finish(1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["lint_reports"]["preflight-lint.json"], findings)
        self.assertEqual(payload["lint_reports"]["project-lint.json"], [])

    def test_plan_flag_remains_non_writing_with_continuation(self):
        with tempfile.TemporaryDirectory() as temp:
            def plan(args):
                self.assertTrue(args.plan_only)
                self.assertTrue(args.continue_on_lint)
                console.remember_result({"status": "ready"})
                return 0
            with patch.object(workflow, "run", side_effect=plan), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["run", temp, "--continue-on-lint", "--plan-only", "--json"]), 0)
            self.assertFalse((Path(temp) / ".swift-sonar").exists())

    def test_secrets_are_redacted_from_all_saved_stage_findings(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"SONAR_TOKEN": "fixture-stage-secret"}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with console.run_output(as_json=True) as output:
                console.present_findings([{"rule_id": "fixture", "reason": "fixture-stage-secret"}],
                                         Path("/fixture"), "preflight-lint.json")
                console.present_findings([], Path("/fixture"), "project-lint.json")
            output.finish(1)
        self.assertNotIn("fixture-stage-secret", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["lint_reports"]["preflight-lint.json"][0]["reason"],
                         "[redacted]")


if __name__ == "__main__":
    unittest.main()
