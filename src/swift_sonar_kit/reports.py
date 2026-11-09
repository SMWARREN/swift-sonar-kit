"""Strict, deterministic report adapters. SPDX-License-Identifier: GPL-3.0-only."""

from pathlib import Path
import xml.etree.ElementTree as ET


def _source(raw: object, root: Path) -> tuple[str, int]:
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 for c in raw):
        raise ValueError("Source path must be a nonempty, control-free string")
    root = root.resolve(strict=True)
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Report source must be an existing file inside the project")
    return path.relative_to(root).as_posix(), len(path.read_text(encoding="utf-8").splitlines())


def _string(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SwiftLint finding requires nonempty {key}")
    return value


def swiftlint_report(data: object, root: Path) -> dict:
    """Convert actual findings without relying on a plugin's bundled rule catalog."""
    if not isinstance(data, list):
        raise ValueError("SwiftLint report must be a JSON array")
    rules: dict[str, dict] = {}
    findings: dict[tuple, dict] = {}
    for record in data:
        if not isinstance(record, dict):
            raise ValueError("SwiftLint finding must be an object")
        rule_id = _string(record, "rule_id")
        message = _string(record, "reason")
        severity = _string(record, "severity")
        if severity not in {"Warning", "Error"}:
            raise ValueError("Unknown SwiftLint severity")
        path, limit = _source(record.get("file"), root)
        line = record.get("line")
        if line is not None and (type(line) is not int or not 1 <= line <= limit):
            raise ValueError("SwiftLint line is outside the current source")
        location = {"message": message, "filePath": path}
        if line is not None:
            location["textRange"] = {"startLine": line}
        findings[(rule_id, path, line or 0, message)] = {
            "ruleId": rule_id, "primaryLocation": location,
        }
        prior = rules.get(rule_id)
        high = severity == "Error" or (prior and prior["severity"] == "MAJOR")
        # Stable rule names, independent of finding order or localized tool output.
        rules[rule_id] = {
            "id": rule_id, "name": rule_id, "description": f"SwiftLint rule: {rule_id}",
            "engineId": "swiftlint", "cleanCodeAttribute": "CONVENTIONAL",
            "type": "CODE_SMELL", "severity": "MAJOR" if high else "MINOR",
            "impacts": [{"softwareQuality": "MAINTAINABILITY", "severity": "HIGH" if high else "LOW"}],
        }
    return {"rules": [rules[key] for key in sorted(rules)],
            "issues": [findings[key] for key in sorted(findings)]}


def lcov_report(texts: list[str], root: Path) -> str:
    """Merge observed line hits with OR. Never fabricate branch or missing coverage."""
    files: dict[str, dict[int, bool]] = {}
    for text in texts:
        current = None
        for entry in text.splitlines():
            if entry.startswith("SF:"):
                if current is not None:
                    raise ValueError("LCOV source record has no end_of_record")
                current = _source(entry[3:], root)
            elif entry.startswith("DA:"):
                if current is None:
                    raise ValueError("LCOV DA entry has no source record")
                fields = entry[3:].split(",")
                if len(fields) not in (2, 3) or not all(s.isascii() and s.isdigit() for s in fields[:2]):
                    raise ValueError("LCOV DA requires nonnegative integer line and count")
                number, count = int(fields[0]), int(fields[1])
                path, limit = current
                if not 1 <= number <= limit:
                    raise ValueError("LCOV line is outside the current source")
                lines = files.setdefault(path, {})
                lines[number] = lines.get(number, False) or count > 0
            elif entry == "end_of_record":
                if current is None:
                    raise ValueError("LCOV terminator has no source record")
                current = None
            elif not entry or entry.startswith(("TN:", "FN:", "FNDA:", "FNF:", "FNH:",
                                                "LF:", "LH:", "BRDA:", "BRF:", "BRH:")):
                continue
            else:
                raise ValueError("Unrecognized LCOV record")
        if current is not None:
            raise ValueError("LCOV source record has no end_of_record")
    if not files:
        raise ValueError("No line coverage observations were supplied")
    coverage = ET.Element("coverage", version="1")
    for path, lines in sorted(files.items()):
        element = ET.SubElement(coverage, "file", path=path)
        for number, covered in sorted(lines.items()):
            ET.SubElement(element, "lineToCover", lineNumber=str(number), covered=str(covered).lower())
    ET.indent(coverage)
    return ET.tostring(coverage, encoding="unicode") + "\n"
