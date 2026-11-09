"""Console presentation state, separate from audit evidence and failure gates.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager, nullcontext, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import TextIO
import uuid

from . import __version__
from .console_tables import render_fields, render_findings
from .project_config import safe_path


_ACTIVE_OUTPUT = ContextVar("swift_sonar_run_output", default=None)
_RESULT_PATH = ".swift-sonar/run-result.json"


def _redact(value):
    """Keep an existing-server credential out of presentation, including errors."""
    token = os.environ.get("SONAR_TOKEN")
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, dict):
        return {_redact(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _save_result(root: Path, payload: dict, *, setup_only: bool = False) -> Path:
    """Atomically replace only the kit's latest result, never follow a symlink."""
    directory = safe_path(root, ".swift-sonar")
    directory.mkdir(mode=0o700, exist_ok=True)
    filename = "setup-result.json" if setup_only else "run-result.json"
    relative = ".swift-sonar/" + filename
    path = safe_path(root, relative)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".run-result-" + uuid.uuid4().hex
    temporary_created = False
    try:
        try:
            previous = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and not stat.S_ISREG(previous.st_mode):
            raise ValueError("JSON report destination is not a regular file")
        payload["report_path"] = _redact(str(path))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        temporary_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, default=str, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, relative)
        opened = os.fstat(directory_fd)
        current = directory.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("JSON report directory changed while saving")
        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            if temporary_created:
                os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)
    return path


@dataclass
class RunOutput:
    stream: TextIO
    as_json: bool = True
    result: dict = field(default_factory=dict)
    findings: list[dict] | None = None
    lint_reports: dict[str, list[dict]] = field(default_factory=dict)
    error: str | None = None
    lint_tool: dict | None = None
    saved_path: Path | None = None

    def finish(self, exit_code: int, *, root: Path | None = None, save: bool = False,
               setup_only: bool = False) -> int:
        self.saved_path = None
        payload = {"schema": 1, "tool_version": __version__, **self.result,
                   "exit_code": exit_code, "finished_at": datetime.now(timezone.utc).isoformat()}
        payload.setdefault("status", "failed" if exit_code else "completed")
        if self.findings is not None:
            payload["findings"] = self.findings
        if self.lint_reports:
            payload["lint_reports"] = self.lint_reports
        if self.error is not None:
            payload.update(status="failed", error=self.error)
        payload = _redact(payload)
        saved = None
        if save:
            try:
                if root is None:
                    raise ValueError("No project directory is available for the JSON report")
                if setup_only:
                    saved = _save_result(root.resolve(strict=True), payload, setup_only=True)
                else:
                    saved = _save_result(root.resolve(strict=True), payload)
                self.saved_path = saved
            except (OSError, ValueError) as error:
                payload.pop("report_path", None)
                payload["report_error"] = _redact(str(error))
                exit_code = exit_code or 2
                payload.update(status="failed", exit_code=exit_code)
                diagnostic = json.dumps(payload["report_error"])[1:-1]
                print(f"swift-sonar: Could not save JSON report: {diagnostic}", file=sys.stderr)
        # ASCII escapes preserve machine-readable strings without terminal controls.
        if self.as_json:
            print(json.dumps(payload, indent=2, default=str, allow_nan=False), file=self.stream)
        elif saved is not None:
            print(render_fields([("JSON report", saved)]), file=self.stream)
        return exit_code


@contextmanager
def run_output(*, as_json: bool = False):
    """Collect the same full result in table and JSON presentation modes."""
    output = RunOutput(sys.stdout, as_json=as_json)
    token = _ACTIVE_OUTPUT.set(output)
    try:
        with redirect_stdout(sys.stderr) if as_json else nullcontext():
            yield output
    finally:
        _ACTIVE_OUTPUT.reset(token)


@contextmanager
def json_output():
    """Reserve stdout for one JSON document and keep live progress on stderr."""
    with run_output(as_json=True) as output:
        yield output


def remember_result(result: dict) -> None:
    output = _ACTIVE_OUTPUT.get()
    if output is not None:
        output.result = dict(result)


def remember_error(message: str) -> None:
    output = _ACTIVE_OUTPUT.get()
    if output is not None:
        output.error = message


def remember_lint_tool(tool: dict) -> None:
    """Retain the already-verified executable for a later, separately consented fix."""
    output = _ACTIVE_OUTPUT.get()
    if output is not None:
        output.lint_tool = dict(tool)


def present_fields(fields: list[tuple[str, object]]) -> None:
    output = _ACTIVE_OUTPUT.get()
    if output is None or not output.as_json:
        print(render_fields(fields))


def present_findings(findings: list[dict], root: Path, report_name: str) -> None:
    """Render the findings already produced by lint, never rerun or alter lint."""
    output = _ACTIVE_OUTPUT.get()
    if output is not None:
        output.findings = findings
        output.lint_reports[report_name] = list(findings)
        if output.as_json:
            return
    print(render_findings(findings, root))
    if findings:
        print(f"Full findings (relative to project root): .swift-sonar/{report_name}")


def child_stdout():
    """Subprocesses inherit fd 1, not redirect_stdout's Python stream override."""
    output = _ACTIVE_OUTPUT.get()
    return sys.stderr if output is not None and output.as_json else None
