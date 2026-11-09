"""Mocked post-report CLI checks. No real scans, lint, fixes, apps, or servers.

SPDX-License-Identifier: GPL-3.0-only
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from swift_sonar_kit import cli, console, project_runner
from swift_sonar_kit import analysis_runner as workflow


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class CorrectionCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="correction CLI spaces ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "Sources/Example.swift"
        self.source.parent.mkdir()
        self.source.write_text("let example = [1, 2] \n")
        self.original_source = self.source.read_bytes()
        self.findings = [{"file": str(self.source), "line": 1, "severity": "Warning",
                          "rule_id": "trailing_whitespace", "reason": "Remove trailing whitespace"}]
        self.tool = {"path": "/fixture/swiftlint", "version": "0.65.1", "sha256": "a" * 64}
        self.result = {"status": "blocked", "stage": "lint-preflight", "published": False}
        self.code = 1
        self.use_tool = True

    def fake_workflow(self, args):
        if not args.plan_only:
            directory = self.root / ".swift-sonar"
            directory.mkdir(exist_ok=True)
            (directory / "preflight-lint.json").write_text(json.dumps(self.findings))
            (directory / "run-summary.json").write_text(json.dumps(self.result))
        console.remember_result(self.result)
        if self.use_tool:
            console.remember_lint_tool(self.tool)
        console.present_findings(self.findings, self.root, "preflight-lint.json")
        return self.code

    def run_cli(self, *flags, offer_effect=None, workflow_effect=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(workflow, "run", side_effect=workflow_effect or self.fake_workflow), \
                patch("swift_sonar_kit.corrections.offer_corrections", side_effect=offer_effect) as offer:
            code = cli.main(["run", str(self.root), *flags])
        self.assertEqual(self.source.read_bytes(), self.original_source)
        return code, stdout.getvalue(), stderr.getvalue(), offer

    def test_offer_happens_after_original_report_is_saved_and_preserves_failed_audit(self):
        originals = {}

        def pretend_fix(root, findings, tool):
            self.assertEqual(root, self.root)
            self.assertEqual(findings, self.findings)
            self.assertEqual(tool, self.tool)
            for name in ("run-result.json", "run-summary.json", "preflight-lint.json"):
                originals[name] = (root / ".swift-sonar" / name).read_bytes()
            saved = json.loads(originals["run-result.json"])
            self.assertEqual(saved["exit_code"], 1)
            self.assertEqual(saved["status"], "blocked")
            self.assertEqual(saved["findings"], self.findings)
            return {"status": "corrected", "recheck_exit_code": 0}

        code, _, _, offer = self.run_cli(offer_effect=pretend_fix)
        self.assertEqual(code, 1)
        offer.assert_called_once()
        for name, original in originals.items():
            self.assertEqual((self.root / ".swift-sonar" / name).read_bytes(), original)

    def test_final_local_validation_lint_block_can_offer(self):
        self.result["stage"] = "local-validation"
        code, _, _, offer = self.run_cli()
        self.assertEqual(code, 1)
        offer.assert_called_once_with(self.root, self.findings, self.tool)

    def test_json_never_offers_and_stdout_matches_original_saved_result(self):
        code, stdout, _, offer = self.run_cli("--json")
        self.assertEqual(code, 1)
        offer.assert_not_called()
        self.assertEqual(json.loads(stdout), json.loads(
            (self.root / ".swift-sonar/run-result.json").read_text()))

    def test_explicit_disable_never_offers(self):
        code, _, _, offer = self.run_cli("--no-fix-prompt")
        self.assertEqual(code, 1)
        offer.assert_not_called()
        self.assertTrue((self.root / ".swift-sonar/run-result.json").is_file())

    def test_plan_only_remains_nonwriting_and_never_offers(self):
        code, _, _, offer = self.run_cli("--plan-only")
        self.assertEqual(code, 1)
        offer.assert_not_called()
        self.assertFalse((self.root / ".swift-sonar").exists())

    def test_missing_findings_never_offers(self):
        self.findings = []
        _, _, _, offer = self.run_cli()
        offer.assert_not_called()

    def test_missing_verified_tool_never_offers(self):
        self.use_tool = False
        _, _, _, offer = self.run_cli()
        offer.assert_not_called()

    def test_unrelated_failure_or_success_never_offers(self):
        for result, code in (({"status": "failed", "stage": "tests"}, 7),
                             ({"status": "blocked", "stage": "server-setup"}, 2),
                             ({"status": "passed", "stage": "lint-preflight"}, 0)):
            with self.subTest(result=result):
                self.result, self.code = result, code
                actual, _, _, offer = self.run_cli()
                self.assertEqual(actual, code)
                offer.assert_not_called()

    def test_expected_workflow_error_is_saved_without_an_offer(self):
        def failed(args):
            self.fake_workflow(args)
            raise ValueError("fixture invalid project")

        code, _, _, offer = self.run_cli(workflow_effect=failed)
        self.assertEqual(code, 2)
        offer.assert_not_called()
        result = json.loads((self.root / ".swift-sonar/run-result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "fixture invalid project")

    def test_report_save_failure_prevents_offer_and_keeps_original_nonzero_exit(self):
        with patch.object(console, "_save_result", side_effect=PermissionError("fixture denied")):
            code, _, stderr, offer = self.run_cli()
        self.assertEqual(code, 1)
        offer.assert_not_called()
        self.assertIn("Could not save JSON report", stderr)
        self.assertFalse((self.root / ".swift-sonar/run-result.json").exists())

    def test_correction_error_cannot_replace_audit_status_or_leak_token(self):
        with patch.dict(os.environ, {"SONAR_TOKEN": "fixture-secret"}):
            code, _, stderr, offer = self.run_cli(
                offer_effect=ValueError("fixture-secret\x1b[31m unavailable"))
        offer.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIn("Original audit result and exit status are unchanged", stderr)
        self.assertNotIn("fixture-secret", stderr)
        self.assertNotIn("\x1b", stderr)
        self.assertIn("[redacted]", stderr)
        saved = json.loads((self.root / ".swift-sonar/run-result.json").read_text())
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["exit_code"], 1)
        self.assertNotIn("correction", saved)

    def test_verified_lint_identity_is_captured_without_changing_strict_command(self):
        (self.root / ".swift-sonar").mkdir()
        argv = ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"]
        process = subprocess.CompletedProcess(argv, 2, json.dumps(self.findings), "")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(project_runner.subprocess, "run", return_value=process) as run:
            with console.run_output(as_json=True) as output:
                result = project_runner._lint(self.root, {"lint": {"argv": argv, "import": "native"}},
                                              {"swiftlint": self.tool})
        self.assertEqual(result["findings"], 1)
        self.assertEqual(output.lint_tool, self.tool)
        self.assertEqual(output.findings, self.findings)
        self.assertEqual(run.call_args.args[0], [self.tool["path"], *argv[1:]])
        self.tool["sha256"] = "b" * 64
        self.assertEqual(output.lint_tool["sha256"], "a" * 64)

    def test_lint_identity_is_not_reused_by_another_run(self):
        with console.run_output() as first:
            console.remember_lint_tool(self.tool)
        with console.run_output() as second:
            pass
        self.assertEqual(first.lint_tool, self.tool)
        self.assertIsNone(second.lint_tool)

    def test_interactive_public_flow_backs_up_before_fake_fix_and_keeps_failed_audit(self):
        executable = self.root / "mock-swiftlint"
        executable.write_bytes(b"mock-only: never executed")
        executable.chmod(0o755)
        self.tool.update(path=str(executable), sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        # Older kit versions may already have a non-writable-by-others 0755 output directory.
        directory = self.root / ".swift-sonar"
        directory.mkdir()
        directory.chmod(0o755)
        original_report = []
        calls = []

        def fake_native(argv, **kwargs):
            self.assertEqual(argv[0], str(executable))
            calls.append(argv)
            if "--fix" in argv:
                original_report.append((directory / "run-result.json").read_bytes())
                manifests = list((directory / "corrections").glob("*/manifest.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text())
                backup = self.root / manifest["files"][0]["backup"]
                self.assertEqual(backup.read_bytes(), self.original_source)
                self.assertTrue(backup.name.endswith(".swift.bak"))
                self.assertEqual(kwargs["env"]["SCRIPT_INPUT_FILE_COUNT"], "1")
                self.assertEqual(kwargs["env"]["SCRIPT_INPUT_FILE_0"], str(self.source))
                self.source.write_bytes(b"let example = [1, 2]\n")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            self.assertEqual(argv[1:], ["lint", "--quiet", "--strict", "--reporter", "json"])
            self.assertNotIn("SCRIPT_INPUT_FILE_COUNT", kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        stdout, stderr = TTYBuffer(), TTYBuffer()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("sys.stdin", TTYBuffer()), patch.dict(os.environ, {"CI": ""}), \
                patch("builtins.input", return_value="yes") as ask, \
                patch.object(workflow, "run", side_effect=self.fake_workflow), \
                patch("subprocess.Popen", side_effect=AssertionError("Real child processes are forbidden")), \
                patch("swift_sonar_kit.corrections.subprocess.run", side_effect=fake_native):
            code = cli.main(["run", str(self.root)])
        self.assertEqual(code, 1)
        ask.assert_called_once_with("Fix automatically correctable SwiftLint issues? [y/N] ")
        self.assertEqual(len(calls), 2)
        self.assertEqual((directory / "run-result.json").read_bytes(), original_report[0])
        reports = list((directory / "corrections").glob("*/result.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())["status"], "clean-recheck")
        self.assertEqual(self.source.read_bytes(), b"let example = [1, 2]\n")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_interactive_public_default_enter_saves_report_without_correcting(self):
        executable = self.root / "mock-swiftlint"
        executable.write_bytes(b"mock-only: never executed")
        executable.chmod(0o755)
        self.tool.update(path=str(executable), sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        with contextlib.redirect_stdout(TTYBuffer()), contextlib.redirect_stderr(TTYBuffer()), \
                patch("sys.stdin", TTYBuffer()), patch.dict(os.environ, {"CI": ""}), \
                patch("builtins.input", return_value="") as ask, \
                patch.object(workflow, "run", side_effect=self.fake_workflow), \
                patch("subprocess.Popen", side_effect=AssertionError("Real child processes are forbidden")), \
                patch("swift_sonar_kit.corrections.subprocess.run") as native:
            code = cli.main(["run", str(self.root)])
        self.assertEqual(code, 1)
        ask.assert_called_once()
        native.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original_source)
        self.assertTrue((self.root / ".swift-sonar/run-result.json").is_file())
        self.assertFalse((self.root / ".swift-sonar/corrections").exists())


if __name__ == "__main__":
    unittest.main()
