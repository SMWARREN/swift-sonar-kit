# SPDX-License-Identifier: GPL-3.0-only
import os
import unittest
from unittest.mock import patch
from pathlib import Path

from swift_sonar_kit.console_tables import render_fields, render_findings


class ConsoleTablesTests(unittest.TestCase):
    def test_normal_width_keeps_short_filename_readable_and_fits_terminal(self):
        text = render_findings([{
            "file": "Sources/Example.swift", "line": 12, "character": 3,
            "severity": "Warning", "rule_id": "trailing_comma", "reason": "Add a trailing comma",
        }], Path("/project"), width=100)
        self.assertIn("Sources/Example.swift", text)
        self.assertIn("12:3", text)
        self.assertIn("trailing_comma", text)
        self.assertEqual(max(map(len, text.splitlines())), 100)

    def test_unbounded_terminal_width_does_not_expand_output_indefinitely(self):
        text = render_fields([("Status", "passed")], width=1_000_000)
        self.assertLessEqual(max(map(len, text.splitlines())), 240)

    def test_empty_findings_are_explicitly_successful(self):
        self.assertEqual(render_findings([], Path("/project")), "No lint findings.")

    def test_paths_location_columns_and_no_filesystem_access(self):
        text = render_findings([
            {"file": "/project/src/A.swift", "line": 7, "column": 3,
             "severity": "warning", "rule": "line_length", "message": "long line"},
            {"file": "/elsewhere/secret.swift", "message": "outside"},
            {"message": "missing location"},
        ], Path("/project"))
        self.assertIn("src/A.swift", text)
        self.assertIn("7:3", text)
        self.assertIn("<outside project>", text)
        self.assertIn("missing", text)
        self.assertIn("-", text)

    def test_wraps_long_words_and_reports_omitted_count(self):
        findings = [{"file": f"src/{i}.swift", "rule": "R" * 60,
                     "message": "M" * 100} for i in range(3)]
        rendered = render_findings(findings, Path("/project"), width=90, max_rows=2)
        self.assertIn("findings", rendered)
        self.assertIn("1", rendered)
        self.assertGreaterEqual(rendered.count("R"), 120)
        self.assertGreaterEqual(rendered.count("M"), 200)

    def test_terminal_injection_bidi_and_token_are_sanitized(self):
        value = "hello\x1b[31mRED\x1b[0m\nworld\u202e SONAR_TOKEN=abc123"
        rendered = render_fields([("summary", value)])
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("abc123", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("\\n", rendered)
        self.assertIn("\\u202e", rendered)

    def test_unicode_and_malformed_values_render_gracefully(self):
        class Broken:
            def __str__(self):
                raise RuntimeError("bad")
        rendered = render_findings([
            {"path": "src/界e\u0301.swift", "line": Broken(), "message": Broken()},
            "not a finding dictionary",
        ], Path("/project"))
        self.assertIn("界e\u0301.swift", rendered)
        self.assertIn("<unprintable>", rendered)
        summary = render_fields([("count", 2), (Broken(), None)])
        self.assertIn("count", summary)
        self.assertIn("None", summary)
        self.assertIn("<unprintable>", summary)

    def test_actual_swiftlint_json_rule_id_and_reason_shape(self):
        rendered = render_findings([{
            "file": "Sources/Example.swift",
            "line": 12,
            "character": 8,
            "severity": "Warning",
            "rule_id": "trailing_comma",
            "reason": "Collection literals should have trailing commas",
        }], Path("/project"))
        self.assertIn("trailing_comma", rendered)
        message_cells = [line.strip().strip("|").split("|")[4].strip()
                         for line in rendered.splitlines()
                         if line.startswith("|") and len(line.strip().strip("|").split("|")) == 5]
        self.assertIn("Collection literals should have trailing commas", " ".join(message_cells))
        self.assertNotIn("| -", rendered)

    def test_environment_token_redacted_without_assignment_prefix(self):
        with patch.dict(os.environ, {"SONAR_TOKEN": "secret-value"}):
            rendered = render_fields([("run", "token secret-value leaked")])
        self.assertNotIn("secret-value", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_bad_path_and_cell_size_are_bounded(self):
        class BadPath:
            def __str__(self):
                raise RuntimeError("cannot stringify path")

        rendered = render_findings([
            {"file": BadPath(), "message": "ordinary " + "long " * 100},
            {"file": "src/huge.swift", "message": "x" * 100_000},
        ], Path("/project"))
        self.assertIn("<invalid path>", rendered)
        self.assertIn("[truncated]", rendered)
        self.assertLessEqual(rendered.count("x"), 4096)
        self.assertIn("ordinary", rendered)
        self.assertIn("long", rendered)


if __name__ == "__main__":
    unittest.main()
