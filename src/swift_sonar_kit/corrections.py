# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, backed-up SwiftLint correction offers for path-only runs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
from typing import Any, Iterator

from .console_tables import render_findings
from .project_config import safe_path


_PROMPT = "Fix automatically correctable SwiftLint issues? [y/N] "
_MAX_FINDINGS = 10_000
_MAX_FINDINGS_BYTES = 8 * 1024 * 1024
_MAX_FILES = 300
_MAX_PATHS_CHARS = 32 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_TOOL_BYTES = 512 * 1024 * 1024
_FIX_TIMEOUT = 180
_RECHECK_TIMEOUT = 180
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_TOKEN_ASSIGNMENT = re.compile(r"(?i)(SONAR_TOKEN\s*[:=]\s*)([^\s,;]+)")
_BIDI = {chr(i) for i in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C)}


def _safe_error(error: Any) -> str:
    try:
        text = str(error)
    except Exception:
        text = "<unprintable error>"
    secret = os.environ.get("SONAR_TOKEN")
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _TOKEN_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _ANSI.sub(lambda match: "".join(f"\\x{ord(char):02x}" for char in match.group()), text)
    safe: list[str] = []
    for char in text:
        code = ord(char)
        if char in _BIDI or unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            safe.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            safe.append(char)
    return "".join(safe)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Full-repository SwiftLint recheck returned duplicate JSON keys")
        result[key] = value
    return result


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _tool_identity(tool: dict) -> tuple[Path, str]:
    if not isinstance(tool, dict):
        raise ValueError("Pinned SwiftLint identity must be a mapping")
    path_value, digest = tool.get("path"), tool.get("sha256")
    if (not isinstance(path_value, str) or not os.path.isabs(path_value)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        raise ValueError("Pinned SwiftLint identity requires an absolute path and lowercase SHA-256")
    path = Path(path_value)
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_TOOL_BYTES
                or not os.access(path, os.X_OK)):
            raise ValueError("Pinned SwiftLint path must be an executable regular file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("Pinned SwiftLint executable cannot be opened safely") from error
    digest_now = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("Pinned SwiftLint executable changed while opening")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_now.update(chunk)
    if digest_now.hexdigest() != digest:
        raise ValueError("SwiftLint executable no longer matches the captured scan identity")
    return path, digest


def _relative_finding_path(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Every correction finding must identify an in-project Swift file")
    raw = Path(value)
    if ".." in raw.parts or "." in raw.parts:
        raise ValueError("Correction finding paths cannot contain traversal components")
    if raw.is_absolute():
        try:
            relative = raw.relative_to(root)
        except ValueError as error:
            raise ValueError("Correction finding path is outside the project") from error
    else:
        relative = raw
    if (relative.is_absolute() or not relative.parts or relative.suffix != ".swift"
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise ValueError("Correction finding paths must be relative .swift files")
    candidate = safe_path(root, relative.as_posix(), must_exist=True)
    try:
        info = os.lstat(candidate)
    except OSError as error:
        raise ValueError("Correction source file cannot be inspected") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Correction source must be a regular, unlinked Swift file")
    return relative.as_posix()


def _read_source(root: Path, relative: str) -> tuple[bytes, os.stat_result]:
    path = safe_path(root, relative, must_exist=True)
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Correction source must remain a regular, unlinked file")
        if before.st_size > _MAX_FILE_BYTES:
            raise ValueError("Correction source exceeds the per-file backup size limit")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("Correction source cannot be opened safely") from error
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("Correction source changed while opening")
        data = stream.read(_MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Correction source exceeds the per-file backup size limit")
    if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
        raise ValueError("Correction source changed while reading")
    return data, after


def _snapshot(root: Path, findings: list[dict]) -> dict[str, tuple[bytes, str]]:
    if len(findings) > _MAX_FINDINGS:
        raise ValueError("Too many findings for an interactive correction offer")
    try:
        serialized_findings = json.dumps(findings, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("SwiftLint findings are not bounded JSON data") from error
    if len(serialized_findings) > _MAX_FINDINGS_BYTES:
        raise ValueError("SwiftLint findings exceed the correction input size limit")
    paths: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Malformed SwiftLint finding cannot be corrected")
        paths.add(_relative_finding_path(root, finding.get("file")))
    if len(paths) > _MAX_FILES:
        raise ValueError("Too many distinct files for one correction offer")
    if sum(len(path) for path in paths) > _MAX_PATHS_CHARS:
        raise ValueError("Correction file list exceeds the interactive display limit")
    snapshots: dict[str, tuple[bytes, str]] = {}
    total = 0
    for relative in sorted(paths):
        data, _ = _read_source(root, relative)
        total += len(data)
        if total > _MAX_TOTAL_BYTES:
            raise ValueError("Correction backups exceed the total size limit")
        snapshots[relative] = (data, hashlib.sha256(data).hexdigest())
    return snapshots


def _private_directory(root: Path, relative: str) -> Path:
    current = root
    accumulated: list[str] = []
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise ValueError("Unsafe correction backup directory")
        accumulated.append(part)
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Correction backup path contains a non-directory or symbolic link")
        is_state_root = accumulated == [".swift-sonar"]
        write_mask = 0o022 if is_state_root else 0o077
        if info.st_mode & write_mask:
            raise ValueError("Correction backup directories must be private (0700)")
        safe_path(root, "/".join(accumulated), must_exist=True)
    return current


def _new_private_directory(root: Path, relative: str) -> Path:
    path = safe_path(root, relative)
    os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("New correction attempt directory is not private")
    safe_path(root, relative, must_exist=True)
    return path


def _write_private(root: Path, relative: str, data: bytes) -> None:
    path = safe_path(root, relative)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def _write_json(root: Path, relative: str, payload: dict) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    _write_private(root, relative, data)


@contextmanager
def _project_locks(root: Path) -> Iterator[None]:
    """Acquire the same automatic-then-project locks without _job side effects."""
    _private_directory(root, ".swift-sonar")
    descriptors: list[int] = []
    try:
        for name in ("automatic.lock", "project.lock"):
            path = safe_path(root, f".swift-sonar/{name}")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Project correction lock is not a regular private file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another project job is active; corrections were not started") from error
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _backup(root: Path, backup_rel: str, snapshots: dict[str, tuple[bytes, str]],
            tool_sha256: str) -> None:
    files_rel = f"{backup_rel}/files"
    _private_directory(root, files_rel)
    entries = []
    for relative, (data, digest) in snapshots.items():
        # SwiftLint may recurse through hidden directories. A non-Swift suffix
        # prevents these source copies from becoming full-recheck inputs.
        target_rel = f"{files_rel}/{relative}.bak"
        parent = str(Path(target_rel).parent).replace(os.sep, "/")
        _private_directory(root, parent)
        _write_private(root, target_rel, data)
        backup_path = safe_path(root, target_rel, must_exist=True)
        backup_info = os.lstat(backup_path)
        if not stat.S_ISREG(backup_info.st_mode) or backup_info.st_nlink != 1 or backup_info.st_size != len(data):
            raise ValueError("Correction backup failed verification")
        with open(backup_path, "rb") as stream:
            if hashlib.sha256(stream.read(_MAX_FILE_BYTES + 1)).hexdigest() != digest:
                raise ValueError("Correction backup hash does not match its source")
        entries.append({"file": relative, "sha256": digest, "size": len(data),
                        "backup": target_rel})
    manifest = {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "tool_sha256": tool_sha256, "files": entries}
    manifest_rel = f"{backup_rel}/manifest.json"
    _write_json(root, manifest_rel, manifest)


def _after_hashes(root: Path, snapshots: dict[str, tuple[bytes, str]]) -> list[dict]:
    changes = []
    for relative, (_, before_hash) in snapshots.items():
        try:
            data, _ = _read_source(root, relative)
            after_hash = hashlib.sha256(data).hexdigest()
            changes.append({"file": relative, "before_sha256": before_hash,
                            "after_sha256": after_hash, "changed": after_hash != before_hash})
        except (OSError, ValueError) as error:
            changes.append({"file": relative, "before_sha256": before_hash,
                            "after_sha256": None, "changed": None, "error": _safe_error(error)})
    return changes


def _report_write(root: Path, backup_rel: str, report: dict) -> tuple[str | None, str | None]:
    relative = f"{backup_rel}/result.json"
    report["report"] = relative
    try:
        _write_json(root, relative, report)
        return relative, None
    except (OSError, ValueError) as error:
        report.pop("report", None)
        message = _safe_error(error)
        report["report_error"] = message
        return None, message


def _execute(root: Path, snapshots: dict[str, tuple[bytes, str]], tool_path: Path,
             tool_sha256: str) -> dict:
    result: dict[str, Any] = {
        "schema": 1,
        "status": "failed",
        "tool": {"path": str(tool_path), "sha256": tool_sha256},
        "files": [],
        "fix_returncode": None,
        "recheck_returncode": None,
        "remaining_findings": [],
    }
    backup_rel: str | None = None
    fix_attempted = False
    try:
        with _project_locks(root):
            _tool_identity({"path": str(tool_path), "sha256": tool_sha256})
            current = _snapshot(root, [{"file": path} for path in snapshots])
            if {key: value[1] for key, value in current.items()} != {key: value[1] for key, value in snapshots.items()}:
                raise ValueError("Swift sources changed while awaiting confirmation; corrections were not started")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
            _private_directory(root, ".swift-sonar/corrections")
            candidate_backup_rel = f".swift-sonar/corrections/{stamp}"
            _new_private_directory(root, candidate_backup_rel)
            backup_rel = candidate_backup_rel
            result["backup_directory"] = backup_rel
            _backup(root, backup_rel, snapshots, tool_sha256)
            result["backup_manifest"] = f"{backup_rel}/manifest.json"
            # Re-read immediately before correction, after durable backup creation.
            latest = _snapshot(root, [{"file": path} for path in snapshots])
            if {key: value[1] for key, value in latest.items()} != {key: value[1] for key, value in snapshots.items()}:
                raise ValueError("Swift sources changed after backup; corrections were not started")
            from .project_runner import child_environment

            env = child_environment()
            env["SCRIPT_INPUT_FILE_COUNT"] = str(len(snapshots))
            for index, relative in enumerate(sorted(snapshots)):
                env[f"SCRIPT_INPUT_FILE_{index}"] = str(root / relative)
            fix_argv = [str(tool_path), "lint", "--fix", "--use-script-input-files",
                        "--force-exclude", "--quiet"]
            fix_attempted = True
            try:
                fixed = subprocess.run(fix_argv, cwd=root, env=env, capture_output=True,
                                       timeout=_FIX_TIMEOUT, check=False)
                result["fix_returncode"] = fixed.returncode
            except KeyboardInterrupt:
                result["fix_interrupted"] = True
                result["fix_error"] = "Interrupted while SwiftLint may have been correcting files"
            except (OSError, subprocess.SubprocessError) as error:
                result["fix_error"] = _safe_error(error)
            changes = _after_hashes(root, snapshots)
            for entry in changes:
                entry["backup"] = f"{backup_rel}/files/{entry['file']}.bak"
            result["files"] = changes
            if any("error" in entry for entry in changes):
                result["file_validation_error"] = (
                    "Some selected files could not be verified after correction. "
                    "Review their retained backups before continuing."
                )
            valid_recheck = False
            if result.get("fix_interrupted"):
                result["recheck_skipped"] = "Fix command was interrupted; no follow-up process was started"
            else:
                recheck_argv = [str(tool_path), "lint", "--quiet", "--strict", "--reporter", "json"]
                try:
                    checked = subprocess.run(recheck_argv, cwd=root, env=child_environment(),
                                             capture_output=True, text=True,
                                             timeout=_RECHECK_TIMEOUT, check=False)
                    result["recheck_returncode"] = checked.returncode
                    def reject_constant(_: str) -> None:
                        raise ValueError("Full-repository SwiftLint recheck returned non-finite JSON")
                    parsed = json.loads(checked.stdout, object_pairs_hook=_unique_object,
                                        parse_constant=reject_constant)
                    if (not isinstance(parsed, list)
                            or any(not isinstance(item, dict) or not isinstance(item.get("rule_id"), str)
                                   for item in parsed)):
                        raise ValueError("Full-repository SwiftLint recheck returned malformed findings")
                    if checked.returncode not in (0, 2) or bool(parsed) != (checked.returncode == 2):
                        raise ValueError("Full-repository SwiftLint recheck exit code disagrees with its findings")
                    result["remaining_findings"] = parsed
                    result["remaining_findings_count"] = len(parsed)
                    valid_recheck = True
                except KeyboardInterrupt:
                    result["recheck_interrupted"] = True
                    result["recheck_error"] = "Interrupted during full-repository SwiftLint recheck"
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    result["recheck_error"] = _safe_error(error)
            fix_ok = (result.get("fix_returncode") == 0
                      and "fix_error" not in result
                      and "file_validation_error" not in result)
            result["status"] = ("partial-or-failed" if not fix_ok
                                else "clean-recheck" if valid_recheck and not result["remaining_findings"]
                                else "findings-remain" if valid_recheck and result["remaining_findings"]
                                else "partial-or-failed")
            result["guidance"] = "The original audit remains failed. Review these changes and rerun the full workflow; no tests or scan were rerun here."
            if backup_rel:
                _, report_error = _report_write(root, backup_rel, result)
                if report_error:
                    print(f"Correction report could not be saved: {report_error}", file=sys.stderr)
            print(f"Correction status: {result['status']}. The original audit remains failed; review changes and rerun the full workflow.", file=sys.stderr)
            if "file_validation_error" in result:
                print(result["file_validation_error"], file=sys.stderr)
            if "recheck_error" in result:
                print(f"Full-repository SwiftLint recheck failed: {result['recheck_error']}", file=sys.stderr)
            elif "recheck_skipped" in result:
                print(result["recheck_skipped"], file=sys.stderr)
            else:
                print(render_findings(result["remaining_findings"], root))
            if result.get("report"):
                print(f"Correction report: {result['report']}", file=sys.stderr)
    except KeyboardInterrupt:
        result["status"] = "partial-or-failed" if fix_attempted else "not-started"
        result["error"] = _safe_error("Interrupted during correction workflow")
        result["guidance"] = "No automatic rollback was attempted. Retain and review any backup; the original audit result is unchanged."
        if backup_rel:
            result["backup_directory"] = backup_rel
            _, report_error = _report_write(root, backup_rel, result)
            if report_error:
                print(f"Correction report could not be saved: {report_error}", file=sys.stderr)
        print(f"Corrections interrupted. {_safe_error(result['guidance'])}", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["status"] = "partial-or-failed" if fix_attempted else "not-started"
        result["error"] = _safe_error(error)
        result["guidance"] = "No automatic rollback was attempted. Retain and review any backup; the original audit result is unchanged."
        if backup_rel:
            result["backup_directory"] = backup_rel
            _, report_error = _report_write(root, backup_rel, result)
            if report_error:
                print(f"Correction report could not be saved: {report_error}", file=sys.stderr)
        print(f"Corrections not fully completed: {result['error']}", file=sys.stderr)
    return result


def offer_corrections(root: Path, findings: list[dict], tool: dict) -> dict | None:
    """Offer backed-up autocorrection only after explicit interactive consent.

    No project writes or child processes occur before an exact ``y``/``yes`` response.
    Declines and noninteractive/CI calls return ``None``. Post-consent outcomes retain
    backups and an independent correction report, never modifying the original audit.
    """
    if not findings or os.environ.get("CI") or not _is_tty(sys.stdin) or not _is_tty(sys.stdout):
        return None
    root = Path(os.path.realpath(root, strict=True))
    if not root.is_dir():
        raise ValueError("Project root must be a directory")
    tool_path, digest = _tool_identity(tool)
    snapshots = _snapshot(root, findings)
    print("SwiftLint may apply any enabled autocorrection within these selected files, not just the listed findings; files may be overwritten:", file=sys.stderr)
    for relative in sorted(snapshots):
        print(f"  {ascii(relative)}", file=sys.stderr)
    print("Exact originals will be backed up first. The existing audit remains failed and must be rerun.", file=sys.stderr)
    try:
        answer = input(_PROMPT)
    except (EOFError, KeyboardInterrupt):
        return None
    if answer.strip().lower() not in {"y", "yes"}:
        return None
    return _execute(root, snapshots, tool_path, digest)
