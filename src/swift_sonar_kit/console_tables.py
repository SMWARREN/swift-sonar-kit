# SPDX-License-Identifier: GPL-3.0-only
"""Safe, dependency-free rendering of scan output for terminals."""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_BIDI = {chr(i) for i in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C)}
_TOKEN = re.compile(r"(?i)(SONAR_TOKEN\s*[:=]\s*)([^\s,;]+)")
_MAX_CELL_CHARS = 4096


def _clean(value: Any) -> str:
    """Convert arbitrary values to inert, visible, single-line text."""
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    secret = os.environ.get("SONAR_TOKEN")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = _TOKEN.sub(r"\1[REDACTED]", text)
    text = _ANSI.sub(lambda m: "".join(f"\\x{ord(c):02x}" for c in m.group()), text)
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char in _BIDI or unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            if char in "\n\r\t":
                out.append("\\n" if char == "\n" else "\\r" if char == "\r" else "\\t")
            else:
                out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(char)
    sanitized = "".join(out)
    if len(sanitized) > _MAX_CELL_CHARS:
        return sanitized[:_MAX_CELL_CHARS - len("[truncated]")] + "[truncated]"
    return sanitized


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1) for c in text)


def _clip_word(word: str, limit: int) -> list[str]:
    lines: list[str] = []
    current = ""
    used = 0
    for char in word:
        cw = _width(char)
        if current and used + cw > limit:
            lines.append(current)
            current, used = "", 0
        current += char
        used += cw
    if current or not lines:
        lines.append(current)
    return lines


def _wrap(text: str, limit: int) -> list[str]:
    limit = max(1, limit)
    result: list[str] = []
    for paragraph in (text,):
        words = paragraph.split()
        if not words:
            result.append("")
            continue
        line = ""
        for word in words:
            if _width(word) > limit:
                if line:
                    result.append(line)
                    line = ""
                parts = _clip_word(word, limit)
                result.extend(parts[:-1])
                line = parts[-1]
            elif not line:
                line = word
            elif _width(line) + 1 + _width(word) <= limit:
                line += " " + word
            else:
                result.append(line)
                line = word
        result.append(line)
    return result or [""]


def _table(headers: list[str], rows: list[list[str]], width: int | None) -> str:
    if width is None:
        width = shutil.get_terminal_size(fallback=(120, 24)).columns
    else:
        width = max(20, int(width))
    width = min(width, 240)
    available = max(1, width - (3 * len(headers) + 1))
    # Keep data columns usable instead of giving almost all space to the last
    # column. Narrow requested widths may be exceeded rather than crushing
    # every field into unreadable fragments.
    # Locations and severities are short. Give the extra room to actionable
    # paths, rule identifiers and messages instead of padding numeric columns.
    weights = [3, 0, 0, 2, 3] if len(headers) == 5 else [1, 3]
    widths = [12, 8, 8, 14, 14] if len(headers) == 5 else [8, 16]
    remaining = max(0, available - sum(widths))
    weight_sum = sum(weights)
    shares = [remaining * weight // weight_sum for weight in weights]
    for i, share in enumerate(shares):
        widths[i] += share
    for i in range(remaining - sum(shares)):
        widths[i % len(widths)] += 1
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    output = [border]
    def emit(values: list[str]) -> None:
        wrapped = [_wrap(v, w) for v, w in zip(values, widths)]
        for idx in range(max(map(len, wrapped))):
            cells = [items[idx] if idx < len(items) else "" for items in wrapped]
            output.append("| " + " | ".join(cell + " " * max(0, w - _width(cell)) for cell, w in zip(cells, widths)) + " |")
    emit(headers)
    output.append(border)
    for row in rows:
        emit(row)
    output.append(border)
    return "\n".join(output)


def _field(obj: dict, *names: str) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _location_path(value: Any, root: Path) -> str:
    try:
        if value is None or str(value) == "":
            return "-"
        p = Path(str(value))
        base = Path(root)
        p_abs = Path(os.path.abspath(os.path.normpath(str(p if p.is_absolute() else base / p))))
        b_abs = Path(os.path.abspath(os.path.normpath(str(base))))
        rel = os.path.relpath(str(p_abs), str(b_abs))
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return "<outside project>"
        return rel or "."
    except Exception:
        return "<invalid path>"


def render_findings(findings: list[dict], root: Path, *, width: int | None = None, max_rows: int = 200) -> str:
    """Render findings safely; paths are made root-relative without file access."""
    if not findings:
        return "No lint findings."
    try:
        limit = max(0, int(max_rows))
    except (TypeError, ValueError, OverflowError):
        limit = 200
    rows: list[list[str]] = []
    for finding in findings[:limit]:
        if not isinstance(finding, dict):
            finding = {}
        path = _field(finding, "file", "path", "filename", "uri")
        line = _field(finding, "line", "startLine")
        col = _field(finding, "character", "column", "col", "startColumn")
        loc = "-" if line is None else _clean(line) + ((":" + _clean(col)) if col is not None else "")
        rows.append([
            _clean(_location_path(path, root)),
            loc,
            _clean(_field(finding, "severity") or "-"),
            _clean(_field(finding, "rule", "rule_id", "ruleId", "id") or "-"),
            _clean(_field(finding, "message", "reason") or "-"),
        ])
    omitted = len(findings) - len(rows)
    if omitted:
        rows.append([f"... {omitted} findings omitted", "", "", "", ""])
    return _table(["File", "Line", "Severity", "Rule", "Message"], rows, width)


def render_fields(fields: list[tuple[str, object]], *, width: int | None = None) -> str:
    """Render run or summary facts as a safe Key/Value table."""
    return _table(["Key", "Value"], [[_clean(k), _clean(v)] for k, v in fields], width)
