"""Formatting-only checks. No real lint, scan, app, or server execution.

SPDX-License-Identifier: GPL-3.0-only
"""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from swift_sonar_kit import cli, console
from swift_sonar_kit import analysis_runner as workflow


class ConsoleOutputTests(unittest.TestCase):
    def setUp(self):
        self.findings = [{"file": "/project/Sources/Example.swift", "line": 12,
                          "character": 3, "severity": "Warning", "rule_id": "trailing_comma",
                          "reason": "Collection literals should have trailing commas"}]

    def test_default_output_is_a_table_with_actual_swiftlint_fields(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            console.present_findings(self.findings, Path("/project"), "preflight-lint.json")
        text = stream.getvalue()
        for word in ("File", "Line", "Severity", "Rule", "Message", "12:3", "Sources/Example.swift",
                     "trailing_comma", "Collection", "preflight-lint.json"):
            self.assertIn(word, text)
        self.assertNotIn("/project/Sources", text)
        self.assertEqual(self.findings[0]["rule_id"], "trailing_comma")

    def test_json_stdout_is_one_document_and_progress_is_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with console.json_output() as output:
                print("fixture progress")
                console.present_fields([("Project", "not a JSON table")])
                console.remember_result({"status": "blocked", "published": False})
                console.present_findings(self.findings, Path("/project"), "preflight-lint.json")
            output.finish(1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["exit_code"], 1)
        self.assertFalse(payload["published"])
        self.assertEqual(payload["findings"], self.findings)
        self.assertEqual(stderr.getvalue(), "fixture progress\n")
        self.assertNotIn("| File", stdout.getvalue())
        self.assertIsNone(console.child_stdout())

    def test_json_redacts_known_token_and_escapes_terminal_controls(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"SONAR_TOKEN": "fixture-private-token"}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with console.json_output() as output:
                console.remember_result({"status": "failed", "nested": ["fixture-private-token\x1b[31m"]})
                console.remember_error("Bad fixture-private-token")
            output.finish(2)
        text = stdout.getvalue()
        self.assertNotIn("fixture-private-token", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("[redacted]", json.loads(text)["error"])

    def test_context_is_restored_after_exception_and_findings_are_not_retained(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError), console.json_output():
                console.present_findings(self.findings, Path("/project"), "preflight-lint.json")
                raise ValueError("fixture")
        self.assertIsNone(console.child_stdout())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with console.json_output() as output:
                console.remember_result({"status": "prepared"})
            output.finish(0)
        self.assertNotIn("findings", json.loads(stdout.getvalue()))

    def test_child_stdout_is_routed_to_diagnostics_not_json(self):
        # A harmless Python child proves OS-level output routing, not a scan.
        stdout = io.StringIO()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(diagnostics):
            with console.json_output() as output:
                subprocess.run([sys.executable, "-c", "print('fixture child progress')"],
                               stdout=console.child_stdout(), check=True)
                print("fixture Python progress")
                console.remember_result({"status": "prepared"})
            output.finish(0)
            diagnostics.seek(0)
            logs = diagnostics.read()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "prepared")
        self.assertIn("fixture child progress", logs)
        self.assertIn("fixture Python progress", logs)
        self.assertNotIn("fixture child", stdout.getvalue())

    def test_json_cli_error_is_structured_without_running_workflow(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(workflow, "run", side_effect=ValueError("fixture rejection")) as run:
            code = cli.main(["run", temp, "--json"])
        self.assertEqual(code, 2)
        self.assertTrue(run.call_args.args[0].as_json)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 2)
        self.assertIn("fixture rejection", payload["error"])
        self.assertIn("fixture rejection", stderr.getvalue())

    def test_nonexistent_project_has_one_json_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(workflow, "run") as run:
            code = cli.main(["run", str(Path(temp) / "missing"), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["exit_code"], 2)
        run.assert_not_called()

    def test_both_modes_always_save_full_private_json_in_selected_project(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json), tempfile.TemporaryDirectory(prefix="report project ") as temp:
                root = Path(temp).resolve()
                findings = self.findings * 205
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with console.run_output(as_json=as_json) as output:
                        console.remember_result({"status": "blocked", "published": False})
                        console.present_findings(findings, root, "preflight-lint.json")
                    self.assertEqual(output.finish(1, root=root, save=True), 1)
                path = root / ".swift-sonar/run-result.json"
                saved = json.loads(path.read_text())
                self.assertEqual(saved["findings"], findings)
                self.assertEqual(saved["exit_code"], 1)
                self.assertFalse(saved["published"])
                self.assertEqual(saved["report_path"], str(path))
                self.assertIn("finished_at", saved)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                if as_json:
                    self.assertEqual(json.loads(stdout.getvalue()), saved)
                else:
                    self.assertIn("JSON report", stdout.getvalue())
                    self.assertIn("omitted", stdout.getvalue())

    def test_latest_result_replaces_previous_without_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for status in ("blocked", "prepared"):
                with contextlib.redirect_stdout(io.StringIO()):
                    with console.run_output() as output:
                        console.remember_result({"status": status})
                    output.finish(0, root=root, save=True)
            path = root / ".swift-sonar/run-result.json"
            self.assertEqual(json.loads(path.read_text())["status"], "prepared")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_setup_report_symlink_is_rejected_without_replacing_scan_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = root / ".swift-sonar"
            directory.mkdir()
            prior = directory / "run-result.json"
            prior.write_text("prior scan")
            target = root / "untouched.json"
            target.write_text("private content")
            (directory / "setup-result.json").symlink_to(target)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                with console.json_output() as output:
                    console.remember_result({"status": "prepared", "stage": "dependency-setup"})
                self.assertEqual(output.finish(0, root=root, save=True, setup_only=True), 2)
            self.assertIn("report_error", json.loads(stdout.getvalue()))
            self.assertEqual(prior.read_text(), "prior scan")
            self.assertEqual(target.read_text(), "private content")

    def test_unsafe_output_links_are_rejected_without_touching_their_targets(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor), tempfile.TemporaryDirectory() as temp:
                root, outside = Path(temp) / "project", Path(temp) / "outside"
                root.mkdir()
                outside.mkdir()
                target = outside / "private.json"
                target.write_text("untouched")
                if ancestor:
                    (root / ".swift-sonar").symlink_to(outside, target_is_directory=True)
                else:
                    (root / ".swift-sonar").mkdir()
                    (root / ".swift-sonar/run-result.json").symlink_to(target)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with console.json_output() as output:
                        console.remember_result({"status": "prepared"})
                    self.assertEqual(output.finish(0, root=root, save=True), 2)
                data = json.loads(stdout.getvalue())
                self.assertEqual(data["status"], "failed")
                self.assertIn("report_error", data)
                self.assertNotIn("report_path", data)
                self.assertIn("Could not save JSON", stderr.getvalue())
                self.assertEqual(target.read_text(), "untouched")
                self.assertEqual(list(outside.iterdir()), [target])

    def test_atomic_save_failure_keeps_prior_report_and_original_failure_code(self):
        for original_code in (0, 7):
            with self.subTest(original_code=original_code), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                directory = root / ".swift-sonar"
                directory.mkdir()
                path = directory / "run-result.json"
                path.write_text('{"previous": true}')
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        patch.object(console.os, "replace", side_effect=PermissionError("fixture denied")):
                    with console.json_output() as output:
                        console.remember_result({"status": "failed" if original_code else "prepared"})
                    code = output.finish(original_code, root=root, save=True)
                self.assertEqual(code, original_code or 2)
                self.assertEqual(json.loads(stdout.getvalue())["exit_code"], code)
                self.assertEqual(json.loads(path.read_text()), {"previous": True})
                self.assertEqual(list(directory.iterdir()), [path])

    def test_saved_json_redacts_token_on_expected_early_error(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {"SONAR_TOKEN": "fixture-private-token"}), \
                patch.object(workflow, "run", side_effect=ValueError("Bad fixture-private-token")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", temp]), 2)
            report = (Path(temp) / ".swift-sonar/run-result.json").read_text()
        self.assertNotIn("fixture-private-token", report)
        self.assertIn("[redacted]", json.loads(report)["error"])

    def test_existing_temp_name_is_not_deleted_on_exclusive_creation_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = root / ".swift-sonar"
            directory.mkdir()
            unrelated = directory / ".run-result-fixture"
            unrelated.write_text("must remain")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(console.uuid, "uuid4") as identity:
                identity.return_value.hex = "fixture"
                with console.json_output() as output:
                    console.remember_result({"status": "prepared"})
                self.assertEqual(output.finish(0, root=root, save=True), 2)
            self.assertIn("report_error", json.loads(stdout.getvalue()))
            self.assertEqual(unrelated.read_text(), "must remain")
            self.assertFalse((directory / "run-result.json").exists())


if __name__ == "__main__":
    unittest.main()
