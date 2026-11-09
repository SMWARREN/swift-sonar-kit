"""Synthetic browser behavior. No real browser, server or analyzer calls.

SPDX-License-Identifier: GPL-3.0-only
"""

from copy import deepcopy
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from swift_sonar_kit import dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(no_browser=False, as_json=False, plan_only=False,
                                    prepare_only=False, setup_only=False)
        self.output = SimpleNamespace(error=None, saved_path=Path("/project/.swift-sonar/run-result.json"),
            result={"mode": "analysis-only", "stage": "complete", "published": True,
                    "scanner_exit_code": 0, "status": "passed", "project_key": "Example",
                    "server_url": "http://localhost:43210",
                    "dashboard_url": "http://localhost:43210/dashboard?id=Example"})
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"CI": "", "NO_BROWSER": "", "JCODE_NO_BROWSER": ""}).start()
        self.stdin = patch.object(dashboard.sys.stdin, "isatty", return_value=True).start()
        self.stdout = patch.object(dashboard.sys.stdout, "isatty", return_value=True).start()
        self.browser = patch.object(dashboard.webbrowser, "open", return_value=True).start()

    def test_context_url_and_project_key_are_encoded_without_credentials(self):
        self.assertEqual(dashboard.dashboard_url("https://example.test/sonar/", "A:B +C"),
                         "https://example.test/sonar/dashboard?id=A%3AB+%2BC")

    def test_unsafe_server_urls_are_rejected(self):
        for base in ("https://user:secret@example.test", "file:///tmp/report",
                     "http://example.test", "https://example.test?token=secret"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                dashboard.dashboard_url(base, "Example")

    def test_completed_saved_interactive_scan_opens(self):
        dashboard.open_after_scan(self.args, self.output)
        self.browser.assert_called_once_with(self.output.result["dashboard_url"], new=2)

    def test_lint_failure_still_opens_published_analysis(self):
        self.output.result.update(status="failed", lint={"findings": 2})
        dashboard.open_after_scan(self.args, self.output)
        self.browser.assert_called_once()

    def test_explicit_and_nonanalysis_modes_suppress_opening(self):
        for flag in ("no_browser", "as_json", "plan_only", "prepare_only", "setup_only"):
            with self.subTest(flag=flag):
                setattr(self.args, flag, True)
                dashboard.open_after_scan(self.args, self.output)
                self.browser.assert_not_called()
                setattr(self.args, flag, False)

    def test_environment_policy_suppresses_opening(self):
        for name in ("CI", "NO_BROWSER", "JCODE_NO_BROWSER"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "1"}):
                dashboard.open_after_scan(self.args, self.output)
                self.browser.assert_not_called()

    def test_each_nonterminal_stream_suppresses_opening(self):
        for stream in (self.stdin, self.stdout):
            stream.return_value = False
            dashboard.open_after_scan(self.args, self.output)
            self.browser.assert_not_called()
            stream.return_value = True

    def test_errors_unsaved_incomplete_and_uncertain_runs_do_not_open(self):
        for field, value in (("error", "failed"), ("saved_path", None)):
            with self.subTest(field=field):
                original = getattr(self.output, field)
                setattr(self.output, field, value)
                dashboard.open_after_scan(self.args, self.output)
                self.browser.assert_not_called()
                setattr(self.output, field, original)
        for field, value in (("mode", "legacy"), ("stage", "sonar"),
                             ("published", None), ("published", False), ("scanner_exit_code", 7),
                             ("dashboard_url", "https://unrelated.test/dashboard?id=Example")):
            with self.subTest(field=field, value=value):
                original = self.output.result[field]
                self.output.result[field] = value
                dashboard.open_after_scan(self.args, self.output)
                self.browser.assert_not_called()
                self.output.result[field] = original

    def test_browser_failure_warns_without_changing_scan_evidence(self):
        original = deepcopy(self.output.result)
        for failure in (False, OSError("private child error")):
            with self.subTest(failure=type(failure).__name__):
                self.browser.reset_mock()
                self.browser.side_effect = failure if isinstance(failure, Exception) else None
                self.browser.return_value = failure
                error = io.StringIO()
                with redirect_stderr(error):
                    self.assertIsNone(dashboard.open_after_scan(self.args, self.output))
                self.assertIn("Could not open the browser", error.getvalue())
                self.assertNotIn("private child error", error.getvalue())
                self.assertEqual(self.output.result, original)
                self.browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
