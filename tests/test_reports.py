import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from swift_sonar_kit.reports import lcov_report, swiftlint_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "Example.swift").write_text("let value = 1\nprint(value)\n", encoding="utf-8")
        self.finding = {"file": "Example.swift", "line": 1, "column": 2,
                        "reason": "Example warning", "rule_id": "example", "severity": "Warning"}

    def test_empty_lint_is_valid_but_empty_coverage_is_not(self):
        self.assertEqual(swiftlint_report([], self.root), {"rules": [], "issues": []})
        for texts in ([], [""], ["SF:Example.swift\nend_of_record\n"]):
            with self.subTest(texts=texts), self.assertRaises(ValueError):
                lcov_report(texts, self.root)

    def test_modern_issue_schema_preserves_locations(self):
        report = swiftlint_report([self.finding], self.root)
        self.assertEqual(report["issues"][0], {"ruleId": "example", "primaryLocation": {
            "message": "Example warning", "filePath": "Example.swift", "textRange": {"startLine": 1}}})
        self.assertEqual(report["rules"][0]["impacts"], [{"softwareQuality": "MAINTAINABILITY", "severity": "LOW"}])
        json.dumps(report)

    def test_file_level_and_mixed_severity_are_deterministic(self):
        error = dict(self.finding, severity="Error", line=None)
        result = swiftlint_report([self.finding, error, self.finding], self.root)
        self.assertEqual(result, swiftlint_report([error, self.finding], self.root))
        self.assertEqual(len(result["issues"]), 2)
        self.assertEqual(result["rules"][0]["severity"], "MAJOR")
        self.assertNotIn("textRange", result["issues"][0]["primaryLocation"])

    def test_rejects_bad_lint_shapes_and_required_fields(self):
        for data in ({}, None, [None], [{}], [dict(self.finding, reason="")],
                     [dict(self.finding, severity="Critical")], [dict(self.finding, rule_id=1)]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                swiftlint_report(data, self.root)

    def test_rejects_invalid_lint_lines(self):
        for line in (-1, 0, 3, True, 1.2, "1"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                swiftlint_report([dict(self.finding, line=line)], self.root)

    def test_rejects_missing_and_outside_sources(self):
        for source in (None, "", "missing.swift", "../escape.swift", "/etc/hosts", "bad\npath"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                swiftlint_report([dict(self.finding, file=source)], self.root)

    def test_rejects_symlink_escape(self):
        (self.root / "link.swift").symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            swiftlint_report([dict(self.finding, file="link.swift")], self.root)
        with self.assertRaises(ValueError):
            lcov_report(["SF:link.swift\nDA:1,1\nend_of_record\n"], self.root)

    def test_lcov_merges_hits_with_or_and_preserves_uncovered(self):
        texts = ["TN:\nSF:Example.swift\nDA:2,0\nDA:1,0\nLF:2\nLH:0\nend_of_record\n",
                 f"SF:{self.root}/Example.swift\nDA:1,4,checksum\nend_of_record\n"]
        result = lcov_report(texts, self.root)
        self.assertEqual(result, lcov_report(list(reversed(texts)), self.root))
        lines = ET.fromstring(result).findall("./file/lineToCover")
        self.assertEqual([line.attrib for line in lines], [
            {"lineNumber": "1", "covered": "true"}, {"lineNumber": "2", "covered": "false"}])

    def test_xml_escapes_file_names(self):
        (self.root / 'A & "B".swift').write_text("value\n")
        result = lcov_report(['SF:A & "B".swift\nDA:1,1\nend_of_record\n'], self.root)
        self.assertEqual(ET.fromstring(result)[0].attrib["path"], 'A & "B".swift')

    def test_rejects_malformed_lcov(self):
        records = ["DA:1,1", "end_of_record", "SF:Example.swift", "BOGUS:x",
                   "SF:Example.swift\nSF:Example.swift\nend_of_record"]
        records += [f"SF:Example.swift\nDA:{value}\nend_of_record" for value in (
            "0,1", "3,1", "1,-1", "1,x", "-1,2", "1,1,2,3", "1.0,1", "1", "١,1")]
        for record in records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                lcov_report([record], self.root)


if __name__ == "__main__":
    unittest.main()
