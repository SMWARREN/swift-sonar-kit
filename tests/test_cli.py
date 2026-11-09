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

from swift_sonar_kit import cli, receipts


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parent = Path(self.temp.name).resolve()
        self.root = parent / "project"
        self.root.mkdir()
        (self.root / "Sources").mkdir()
        (self.root / "Sources/Example.swift").write_text("let value = 1\n")
        (self.root / ".gitignore").write_text(".swift-sonar/\nbuild/\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.bin = parent / "bin"
        self.bin.mkdir()
        lint = self.bin / "swiftlint"
        lint.write_text("#!" + sys.executable + "\n" + '''import os, json
from pathlib import Path
assert "SONAR_TOKEN" not in os.environ
if os.environ.get("FAKE_CHANGE"):
    Path("Sources/Example.swift").write_text("changed\n")
print(os.environ.get("FAKE_LINT", "[]"))
raise SystemExit(int(os.environ.get("FAKE_LINT_EXIT", "0")))
'''.replace('"changed\n"', '"changed\\n"'))
        lint.chmod(0o755)
        scanner = self.bin / "sonar-scanner"
        scanner.write_text("#!" + sys.executable + "\n" + '''import os, json, sys
from pathlib import Path
Path(".swift-sonar/invocation.json").write_text(json.dumps({"argv":sys.argv[1:], "has_token":bool(os.environ.get("SONAR_TOKEN")), "url":os.environ.get("SONAR_HOST_URL"), "injected":os.environ.get("SONAR_SCANNER_JSON_PARAMS")}))
raise SystemExit(int(os.environ.get("FAKE_SCANNER_EXIT", "0")))
''')
        scanner.chmod(0o755)
        self.env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                          "SONAR_TOKEN": "private-fixture-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.preflight = patch.object(cli, "preflight", return_value={"server_version":"fixture", "plugin_version":"0.5.1", "language":"swift"})
        self.server = self.preflight.start()
        self.addCleanup(self.preflight.stop)
        receipts.run_test(self.root, [sys.executable, "-c", "assert 1 + 1 == 2"], [])
        self.args = ["scan", "--root", str(self.root), "--project-key", "fixture", "--source", "Sources"]

    def run_cli(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = cli.main(args)
        return code, errors.getvalue()

    def test_prepare_has_no_network_and_preserves_consumer_configuration(self):
        config = self.root / "sonar-project.properties"
        config.write_text("original=true\n")
        receipts.run_test(self.root, [sys.executable, "-c", "assert True"], [])
        self.assertEqual(self.run_cli(self.args + ["--prepare-only"])[0], 0)
        self.server.assert_not_called()
        self.assertEqual(config.read_text(), "original=true\n")
        self.assertFalse((self.root / ".swift-sonar/invocation.json").exists())

    def test_scan_enforces_gate_keeps_token_off_argv_and_strips_injection(self):
        with patch.dict(os.environ, {"SONAR_SCANNER_JSON_PARAMS": '{"sonar.qualitygate.wait":"false"}'}):
            self.assertEqual(self.run_cli(self.args)[0], 0)
        invocation = json.loads((self.root / ".swift-sonar/invocation.json").read_text())
        self.assertIn("-Dsonar.qualitygate.wait=true", invocation["argv"])
        self.assertNotIn("private-fixture-token", str(invocation))
        self.assertTrue(invocation["has_token"])
        self.assertIsNone(invocation["injected"])
        self.server.assert_called_once()

    def test_scanner_failure_propagates(self):
        with patch.dict(os.environ, {"FAKE_SCANNER_EXIT":"7"}):
            self.assertEqual(self.run_cli(self.args)[0], 1)
        summary = json.loads((self.root / ".swift-sonar/scan-summary.json").read_text())
        self.assertEqual(summary["scanner_exit_code"], 7)

    def test_lint_findings_cannot_be_hidden_by_green_server_gate(self):
        finding = [{"file":"Sources/Example.swift", "line":1, "severity":"Warning", "rule_id":"example", "reason":"fixture finding"}]
        with patch.dict(os.environ, {"FAKE_LINT":json.dumps(finding), "FAKE_LINT_EXIT":"2"}):
            self.assertEqual(self.run_cli(self.args)[0], 1)
        self.assertTrue((self.root / ".swift-sonar/invocation.json").exists())
        self.assertEqual(len(json.loads((self.root / ".swift-sonar/issues.json").read_text())["issues"]), 1)

    def test_lint_execution_or_parse_failure_stops_scanner(self):
        for env in ({"FAKE_LINT_EXIT":"9"}, {"FAKE_LINT":"not json"}, {"FAKE_LINT":"{}"}, {"FAKE_LINT_EXIT":"2"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assertEqual(self.run_cli(self.args)[0], 2)
        self.server.assert_not_called()

    def test_source_mutation_during_lint_stops_publication(self):
        with patch.dict(os.environ, {"FAKE_CHANGE":"1"}):
            self.assertEqual(self.run_cli(self.args)[0], 2)
        self.server.assert_not_called()

    def test_missing_receipt_and_invalid_scope_fail_closed(self):
        (self.root / ".swift-sonar/tests.json").unlink()
        self.assertEqual(self.run_cli(self.args)[0], 2)
        self.assertEqual(self.run_cli(self.args + ["--source", "."])[0], 2)
        self.server.assert_not_called()

    def test_preflight_failure_never_runs_scanner(self):
        self.server.side_effect = ValueError("fixture preflight rejected")
        self.assertEqual(self.run_cli(self.args)[0], 2)
        self.assertFalse((self.root / ".swift-sonar/invocation.json").exists())

    def test_error_redacts_token(self):
        self.server.side_effect = ValueError("bad private-fixture-token")
        code, errors = self.run_cli(self.args)
        self.assertEqual(code, 2)
        self.assertNotIn("private-fixture-token", errors)
        self.assertIn("[redacted]", errors)

    def test_failed_rerun_removes_prior_scan_summary(self):
        self.assertEqual(self.run_cli(self.args)[0], 0)
        summary = self.root / ".swift-sonar/scan-summary.json"
        self.assertTrue(summary.exists())
        (self.root / "Sources/Example.swift").write_text("let changed = 2\n")
        self.assertEqual(self.run_cli(self.args)[0], 2)
        self.assertFalse(summary.exists())

    def test_source_symlinks_are_rejected_before_scan(self):
        (self.root / "Sources/Alias.swift").symlink_to("Example.swift")
        receipts.run_test(self.root, [sys.executable, "-c", "assert True"], [])
        code, error = self.run_cli(self.args)
        self.assertEqual(code, 2)
        self.assertIn("symbolic links", error)
        self.server.assert_not_called()

    def test_offline_converter_cli(self):
        raw = self.root / ".swift-sonar/raw.json"
        raw.write_text("[]")
        self.assertEqual(self.run_cli(["issues", "--root", str(self.root), "--input", str(raw), "--output", ".swift-sonar/out.json"])[0], 0)
        self.assertEqual(json.loads((self.root / ".swift-sonar/out.json").read_text()), {"rules":[], "issues":[]})
        self.server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
