# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic and mocked tests for explicit SwiftLint correction offers."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from swift_sonar_kit import corrections


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class CorrectionsTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve() / "project"
        root.mkdir()
        source = root / "Sources" / "Sample.swift"
        source.parent.mkdir()
        source.write_bytes(b"let values = [1,2]\n")
        tool = root / "mock-swiftlint"
        tool.write_bytes(b"synthetic executable placeholder")
        tool.chmod(0o755)
        identity = {"path": str(tool), "sha256": hashlib.sha256(tool.read_bytes()).hexdigest()}
        findings = [{"file": str(source), "line": 1, "severity": "Warning",
                     "rule_id": "trailing_comma", "reason": "Synthetic correctable finding"}]
        return temporary, root, source, tool, identity, findings

    def interactive(self, answer="yes"):
        stdout, stderr = TTYBuffer(), TTYBuffer()
        stack = (
            patch.object(corrections.sys, "stdin", TTYBuffer()),
            patch.object(corrections.sys, "stdout", stdout),
            patch.object(corrections.sys, "stderr", stderr),
            patch.dict(os.environ, {"CI": ""}),
            patch("builtins.input", return_value=answer),
        )
        return stdout, stderr, stack

    def test_default_no_decline_creates_nothing_and_runs_no_process(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("n")
        with stack[0], stack[1], stack[2], stack[3], stack[4] as ask, \
                patch.object(corrections.subprocess, "run") as run:
            self.assertIsNone(corrections.offer_corrections(root, findings, identity))
        ask.assert_called_once_with("Fix automatically correctable SwiftLint issues? [y/N] ")
        run.assert_not_called()
        self.assertFalse((root / ".swift-sonar").exists())
        self.assertEqual(source.read_bytes(), b"let values = [1,2]\n")
        self.assertIn("Exact originals will be backed up first", stderr.getvalue())

    def test_non_tty_and_ci_decline_before_validation_or_writes(self):
        temporary, root, _, _, _, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        for non_tty, ci_value in ((True, ""), (False, "true")):
            with self.subTest(non_tty=non_tty, ci=ci_value):
                stdin = io.StringIO("yes") if non_tty else TTYBuffer()
                stdout = io.StringIO() if non_tty else TTYBuffer()
                with patch.object(corrections.sys, "stdin", stdin), \
                        patch.object(corrections.sys, "stdout", stdout), \
                        patch.dict(os.environ, {"CI": ci_value}), \
                        patch("builtins.input") as ask, patch.object(corrections.subprocess, "run") as run:
                    self.assertIsNone(corrections.offer_corrections(root, findings, {}))
                ask.assert_not_called()
                run.assert_not_called()
                self.assertFalse((root / ".swift-sonar").exists())

    def test_empty_findings_and_nonaffirmative_answers_do_not_write(self):
        temporary, root, _, _, identity, _ = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        with stack[0], stack[1], stack[2], stack[3], stack[4] as ask, \
                patch.object(corrections.subprocess, "run") as run:
            self.assertIsNone(corrections.offer_corrections(root, [], identity))
        ask.assert_not_called()
        run.assert_not_called()
        for answer in ("no", "yep", "anything", ""):
            stdout, stderr, stack = self.interactive(answer)
            with stack[0], stack[1], stack[2], stack[3], stack[4], \
                    patch.object(corrections.subprocess, "run") as run:
                self.assertIsNone(corrections.offer_corrections(root, [{"file": "Sources/Sample.swift"}], identity))
            run.assert_not_called()
            self.assertFalse((root / ".swift-sonar").exists())

    def test_eof_and_prompt_interrupt_decline_without_writes(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        for exception in (EOFError, KeyboardInterrupt):
            stdout, stderr, stack = self.interactive("yes")
            with self.subTest(exception=exception), stack[0], stack[1], stack[2], stack[3], \
                    patch("builtins.input", side_effect=exception), \
                    patch.object(corrections.subprocess, "run") as run:
                self.assertIsNone(corrections.offer_corrections(root, findings, identity))
            run.assert_not_called()
            self.assertFalse((root / ".swift-sonar").exists())

    def test_accept_backs_up_exact_bytes_then_runs_pinned_fix_and_full_recheck(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        audit_dir = root / ".swift-sonar"
        audit_dir.mkdir(mode=0o700)
        audit = audit_dir / "run-result.json"
        audit.write_text('{"status":"blocked","exit_code":1}\n')
        audit_before = audit.read_bytes()
        original = source.read_bytes()
        stdout, stderr, stack = self.interactive("Y")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if "--fix" in argv:
                backup_copy = next((root / ".swift-sonar/corrections").glob("*/files/**/*.bak"))
                self.assertEqual(backup_copy.read_bytes(), original)
                self.assertFalse(backup_copy.name.endswith(".swift"))
                self.assertEqual(argv, [identity["path"], "lint", "--fix", "--use-script-input-files",
                                        "--force-exclude", "--quiet"])
                self.assertEqual(kwargs["env"]["SCRIPT_INPUT_FILE_COUNT"], "1")
                self.assertEqual(kwargs["env"]["SCRIPT_INPUT_FILE_0"], str(source))
                self.assertEqual(kwargs["timeout"], corrections._FIX_TIMEOUT)
                source.write_bytes(b"let values = [1, 2]\n")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            self.assertEqual(argv, [identity["path"], "lint", "--quiet", "--strict", "--reporter", "json"])
            self.assertNotIn("SCRIPT_INPUT_FILE_COUNT", kwargs["env"])
            self.assertEqual(kwargs["timeout"], corrections._RECHECK_TIMEOUT)
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "clean-recheck")
        self.assertEqual(result["fix_returncode"], 0)
        self.assertEqual(result["recheck_returncode"], 0)
        self.assertEqual(result["remaining_findings"], [])
        self.assertEqual(result["files"][0]["before_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(result["files"][0]["after_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertTrue(result["files"][0]["changed"])
        backup = root / result["files"][0]["backup"]
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(backup.name.endswith(".swift.bak"))
        manifest_path = root / result["backup_manifest"]
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual((root / result["report"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(audit.read_bytes(), audit_before)
        self.assertIn("original audit remains failed", stderr.getvalue())

    def test_remaining_findings_are_retained_and_never_reported_clean(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        remaining = [{"file": str(source), "rule_id": "line_length", "reason": "Still present"}]

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 2, json.dumps(remaining), "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "findings-remain")
        self.assertEqual(result["remaining_findings"], remaining)
        self.assertIn("line_length", stdout.getvalue())
        self.assertIn("rerun the full workflow", stderr.getvalue())

    def test_unverifiable_selected_file_never_reports_clean_after_empty_recheck(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        original = source.read_bytes()
        stdout, stderr, stack = self.interactive("yes")

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                source.unlink()
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "partial-or-failed")
        self.assertEqual(result["fix_returncode"], 0)
        self.assertEqual(result["recheck_returncode"], 0)
        self.assertEqual(result["remaining_findings"], [])
        self.assertIn("file_validation_error", result)
        self.assertIn("error", result["files"][0])
        self.assertIsNone(result["files"][0]["after_sha256"])
        self.assertEqual((root / result["files"][0]["backup"]).read_bytes(), original)
        saved = json.loads((root / result["report"]).read_text())
        self.assertEqual(saved["status"], "partial-or-failed")
        self.assertIn("could not be verified", stderr.getvalue())

    def test_fix_timeout_retains_backup_runs_recheck_and_reports_partial(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "partial-or-failed")
        self.assertIn("fix_error", result)
        self.assertTrue((root / result["backup_manifest"]).is_file())
        self.assertTrue((root / result["report"]).is_file())
        self.assertNotIn("clean-recheck", result["status"])
        self.assertIn("original audit remains failed", stderr.getvalue())

    def test_interrupt_during_fix_is_partial_and_stops_without_recheck(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "--fix" in argv:
                raise KeyboardInterrupt()
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertTrue(result["fix_interrupted"])
        self.assertEqual(result["status"], "partial-or-failed")
        self.assertEqual(len(calls), 1)
        self.assertTrue((root / result["report"]).is_file())

    def test_invalid_or_stale_tool_identity_fails_before_prompt_or_writes(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        invalid = dict(identity, sha256="0" * 64)
        stdout, stderr, stack = self.interactive("yes")
        with stack[0], stack[1], stack[2], stack[3], stack[4] as ask, \
                self.assertRaises(ValueError):
            corrections.offer_corrections(root, findings, invalid)
        ask.assert_not_called()
        self.assertFalse((root / ".swift-sonar").exists())

    def test_traversal_symlink_and_hardlink_sources_are_rejected_before_prompt(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        outside = root.parent / "outside.swift"
        outside.write_text("let outside = true\n")
        cases = [dict(file="../outside.swift")]
        link = root / "Sources" / "Linked.swift"
        link.symlink_to(source)
        cases.append(dict(file=str(link)))
        hard = root / "Sources" / "Hard.swift"
        os.link(source, hard)
        cases.append(dict(file=str(hard)))
        for finding in cases:
            stdout, stderr, stack = self.interactive("yes")
            with self.subTest(finding=finding), stack[0], stack[1], stack[2], stack[3], stack[4] as ask, \
                    self.assertRaises(ValueError):
                corrections.offer_corrections(root, [finding], identity)
            ask.assert_not_called()
            self.assertFalse((root / ".swift-sonar").exists())

    def test_source_changed_while_prompt_waits_is_not_corrected(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        run_calls = []

        def answer(prompt):
            self.assertEqual(prompt, "Fix automatically correctable SwiftLint issues? [y/N] ")
            source.write_bytes(b"changed by user while prompt was open\n")
            return "yes"

        with stack[0], stack[1], stack[2], stack[3], patch("builtins.input", side_effect=answer), \
                patch.object(corrections.subprocess, "run", side_effect=lambda *a, **k: run_calls.append(a)):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "not-started")
        self.assertIn("changed while awaiting confirmation", result["error"])
        self.assertEqual(source.read_bytes(), b"changed by user while prompt was open\n")
        self.assertEqual(run_calls, [])
        self.assertFalse((root / ".swift-sonar/corrections").exists())

    def test_existing_0755_state_directory_is_accepted_without_chmod(self):
        temporary, root, source, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        state = root / ".swift-sonar"
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        stdout, stderr, stack = self.interactive("yes")

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "clean-recheck")
        self.assertEqual(state.stat().st_mode & 0o777, 0o755)
        self.assertEqual((state / "corrections").stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / result["files"][0]["backup"]).read_bytes(), source.read_bytes())

    def test_lock_conflict_and_backup_failure_do_not_launch_swiftlint(self):
        for failure_point in ("lock", "backup"):
            temporary, root, _, _, identity, findings = self.fixture()
            self.addCleanup(temporary.cleanup)
            stdout, stderr, stack = self.interactive("yes")
            blocker = patch.object(corrections.fcntl, "flock", side_effect=BlockingIOError())
            backup_failure = patch.object(corrections, "_backup", side_effect=OSError("backup denied"))
            with self.subTest(failure_point=failure_point), stack[0], stack[1], stack[2], stack[3], stack[4], \
                    (blocker if failure_point == "lock" else backup_failure), \
                    patch.object(corrections.subprocess, "run") as run:
                result = corrections.offer_corrections(root, findings, identity)
            run.assert_not_called()
            self.assertIn(result["status"], {"not-started", "partial-or-failed"})
            if failure_point == "backup":
                self.assertIn("backup denied", result["error"])
                self.assertTrue((root / result["report"]).is_file())

    def test_nonzero_fix_and_malformed_or_inconsistent_recheck_never_claim_clean(self):
        fixtures = [
            ('[{"file":"Sources/Sample.swift"}]', 2),
            ("[NaN]", 2),
            ('[{"rule_id":"x","rule_id":"y"}]', 2),
            ('[{"rule_id":"x"}]', 1),
        ]
        for output, recheck_code in fixtures:
            temporary, root, _, _, identity, findings = self.fixture()
            self.addCleanup(temporary.cleanup)
            stdout, stderr, stack = self.interactive("yes")

            def fake_run(argv, **kwargs):
                if "--fix" in argv:
                    return subprocess.CompletedProcess(argv, 7, b"", b"")
                return subprocess.CompletedProcess(argv, recheck_code, output, "")

            with self.subTest(output=output, returncode=recheck_code), stack[0], stack[1], stack[2], stack[3], stack[4], \
                    patch.object(corrections.subprocess, "run", side_effect=fake_run):
                result = corrections.offer_corrections(root, findings, identity)
            self.assertEqual(result["status"], "partial-or-failed")
            self.assertEqual(result["fix_returncode"], 7)
            self.assertIn("recheck_error", result)
            self.assertTrue((root / result["backup_manifest"]).is_file())

    def test_nonzero_fix_with_valid_remaining_findings_is_partial(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        remaining = [{"rule_id": "line_length"}]

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                return subprocess.CompletedProcess(argv, 7, b"", b"")
            return subprocess.CompletedProcess(argv, 2, json.dumps(remaining), "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertEqual(result["status"], "partial-or-failed")
        self.assertEqual(result["remaining_findings"], remaining)

    def test_exception_text_is_redacted_and_terminal_controls_are_visible(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        secret = "synthetic-secret-value"

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                raise OSError(f"SONAR_TOKEN={secret} \x1b[31mhidden\u202e")
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.dict(os.environ, {"SONAR_TOKEN": secret}), \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        report_bytes = (root / result["report"]).read_bytes()
        report_text = report_bytes.decode()
        self.assertNotIn(secret, report_text)
        self.assertNotIn("\x1b", report_text)
        self.assertNotIn("\u202e", report_text)
        self.assertIn("[redacted]", report_text)
        self.assertIn("\\x1b", report_text)
        self.assertIn("\\u202e", report_text)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_report_save_failure_is_explicit_and_does_not_claim_a_report_path(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        stdout, stderr, stack = self.interactive("yes")
        write_json = corrections._write_json

        def fail_result_report(project, relative, payload):
            if relative.endswith("/result.json"):
                raise OSError("synthetic report disk failure")
            write_json(project, relative, payload)

        def fake_run(argv, **kwargs):
            if "--fix" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections, "_write_json", side_effect=fail_result_report), \
                patch.object(corrections.subprocess, "run", side_effect=fake_run):
            result = corrections.offer_corrections(root, findings, identity)
        self.assertNotIn("report", result)
        self.assertEqual(result["report_error"], "synthetic report disk failure")
        self.assertIn("Correction report could not be saved", stderr.getvalue())
        self.assertNotIn("Correction report:", stderr.getvalue())

    def test_attempt_directory_collision_never_reuses_existing_backup(self):
        temporary, root, _, _, identity, findings = self.fixture()
        self.addCleanup(temporary.cleanup)
        corrections_root = root / ".swift-sonar" / "corrections"
        corrections_root.mkdir(parents=True, mode=0o700)
        corrections_root.chmod(0o700)
        fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        name = fixed.strftime("%Y%m%dT%H%M%SZ") + "-collision"
        existing = corrections_root / name
        existing.mkdir(mode=0o700)
        sentinel = existing / "sentinel"
        sentinel.write_bytes(b"pre-existing user data")
        fake_datetime = SimpleNamespace(now=lambda _tz=None: fixed)
        fake_uuid = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="collision"))
        stdout, stderr, stack = self.interactive("yes")
        with stack[0], stack[1], stack[2], stack[3], stack[4], \
                patch.object(corrections, "datetime", fake_datetime), \
                patch.object(corrections, "uuid", fake_uuid), \
                patch.object(corrections.subprocess, "run") as run:
            result = corrections.offer_corrections(root, findings, identity)
        run.assert_not_called()
        self.assertEqual(result["status"], "not-started")
        self.assertEqual(sentinel.read_bytes(), b"pre-existing user data")
        self.assertNotIn("backup_directory", result)


if __name__ == "__main__":
    unittest.main()
