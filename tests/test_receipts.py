import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from swift_sonar_kit import receipts


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("build/\n.swift-sonar/\n")
        self.source = self.root / "Example.swift"
        self.source.write_text("let value = 1\n")
        self.command = [sys.executable, "-c", "assert 2 + 2 == 4"]

    def test_success_is_scoped_to_selected_command(self):
        self.assertEqual(receipts.run_test(self.root, self.command, []), 0)
        result = receipts.verify(self.root, [])
        self.assertEqual(result["scope"], "selected-command-only")
        self.assertNotIn("command", result)
        self.assertEqual(result["exit_code"], 0)

    def test_failed_rerun_invalidates_prior_success(self):
        receipts.run_test(self.root, self.command, [])
        self.assertEqual(receipts.run_test(self.root, [sys.executable, "-c", "raise SystemExit(3)"], []), 3)
        self.assertFalse((self.root / ".swift-sonar/tests.json").exists())

    def test_changed_source_fails(self):
        receipts.run_test(self.root, self.command, [])
        self.source.write_text("let changed = 2\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            receipts.verify(self.root, [])

    def test_deleted_and_new_inputs_change_fingerprint(self):
        initial = receipts.snapshot(self.root)
        (self.root / "New.swift").write_text("new\n")
        self.assertNotEqual(initial, receipts.snapshot(self.root))
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.source.unlink()
        self.assertNotEqual(initial, receipts.snapshot(self.root))

    def test_mutation_during_tests_fails_without_receipt(self):
        command = [sys.executable, "-c", "from pathlib import Path; Path('Example.swift').write_text('changed')"]
        with self.assertRaisesRegex(ValueError, "during"):
            receipts.run_test(self.root, command, [])
        self.assertFalse((self.root / ".swift-sonar/tests.json").exists())

    def test_artifact_must_be_fresh_and_bound(self):
        (self.root / "build").mkdir()
        artifact = self.root / "build/coverage.lcov"
        artifact.write_text("old")
        os.utime(artifact, (1, 1))
        with self.assertRaisesRegex(ValueError, "freshly"):
            receipts.run_test(self.root, self.command, [artifact])
        command = [sys.executable, "-c", "from pathlib import Path; Path('build/coverage.lcov').write_text('new')"]
        self.assertEqual(receipts.run_test(self.root, command, [artifact]), 0)
        receipts.verify(self.root, [artifact])
        artifact.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "not bound"):
            receipts.verify(self.root, [artifact])

    def test_stale_future_and_malformed_receipts_fail(self):
        receipts.run_test(self.root, self.command, [])
        path = self.root / ".swift-sonar/tests.json"
        original = json.loads(path.read_text())
        for value in ({}, [], dict(original, started_at=0), dict(original, finished_at=time.time()+3600),
                      dict(original, started_at=1, finished_at=2), dict(original, finished_at=True),
                      dict(original, schema=True), dict(original, exit_code=False),
                      dict(original, scope="all-tests-passed")):
            path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                receipts.verify(self.root, [])

    def test_token_not_in_child_environment(self):
        command = [sys.executable, "-c", "import os; assert 'SONAR_TOKEN' not in os.environ"]
        with patch.dict(os.environ, {"SONAR_TOKEN": "private-test-token"}):
            self.assertEqual(receipts.run_test(self.root, command, []), 0)

    def test_rejects_empty_command_and_escaping_links(self):
        with self.assertRaises(ValueError):
            receipts.run_test(self.root, [], [])
        (self.root / "link").symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            receipts.snapshot(self.root)

    def test_report_directory_link_cannot_write_elsewhere(self):
        (self.root / ".swift-sonar").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            receipts.run_test(self.root, self.command, [])


if __name__ == "__main__":
    unittest.main()
