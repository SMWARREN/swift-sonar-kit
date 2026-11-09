"""Local selected-command evidence, not a test census or signature.

SPDX-License-Identifier: GPL-3.0-only
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Test provenance requires an accessible Git repository")
    return result.stdout


def snapshot(root: Path) -> dict:
    root = root.resolve(strict=True)
    if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("Use the Git repository root, not a subdirectory")
    paths = set(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    digest = hashlib.sha256()
    count = 0
    for raw in sorted(paths - {b""}):
        relative = Path(os.fsdecode(raw))
        if relative.parts[0] in {".swift-sonar", ".scannerwork"}:
            continue
        path = root / relative
        digest.update(raw + b"\0")
        if not path.exists() and not path.is_symlink():
            digest.update(b"deleted\0")
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("Receipt sources must be in-root files, without submodules or escaping links")
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)) + b"\0")
        digest.update(bytes.fromhex(file_hash(resolved)))
        count += 1
    if not count:
        raise ValueError("No Git-visible input files to attest")
    return {"sha256": digest.hexdigest(), "file_count": count}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_path(root: Path, value: Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact must stay inside the project root")
    return path


def run_test(root: Path, command: list[str], artifacts: list[Path]) -> int:
    if not command:
        raise ValueError("Provide an explicit test command after --")
    state = root / ".swift-sonar"
    if state.is_symlink():
        raise ValueError("Report directory cannot be a symbolic link")
    state.mkdir(exist_ok=True)
    receipt = state / "tests.json"
    receipt.unlink(missing_ok=True)  # A failed rerun must never reuse an older success.
    before = snapshot(root)
    started = time.time()
    env = os.environ.copy()
    env.pop("SONAR_TOKEN", None)
    result = subprocess.run(command, cwd=root, env=env, check=False)
    if result.returncode:
        return result.returncode if result.returncode > 0 else 1
    after = snapshot(root)
    if before != after:
        raise ValueError("Project inputs changed during tests. Stabilize inputs and rerun")
    evidence = {}
    for raw in artifacts:
        path = local_path(root, raw)
        if not path.is_file() or path.stat().st_mtime < started:
            raise ValueError("Test artifact was not freshly written by this run")
        evidence[path.relative_to(root).as_posix()] = file_hash(path)
    data = {"schema": 1, "scope": "selected-command-only", "exit_code": 0,
            "started_at": started, "finished_at": time.time(), "inputs": after,
            "command_name": Path(command[0]).name,
            "command_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
            "artifacts": evidence}
    receipt.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0


def verify(root: Path, artifacts: list[Path], max_age: int = 3600) -> dict:
    if max_age <= 0:
        raise ValueError("Receipt maximum age must be positive")
    path = local_path(root, Path(".swift-sonar/tests.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1
            or type(data.get("exit_code")) is not int or data["exit_code"] != 0
            or data.get("scope") != "selected-command-only"):
        raise ValueError("Missing or invalid successful test receipt")
    start, end = data.get("started_at"), data.get("finished_at")
    now = time.time()
    if (type(start) not in (float, int) or type(end) not in (float, int)
            or not 0 < start <= end <= now or now - end > max_age):
        raise ValueError("Test receipt is stale or has invalid timestamps")
    if data.get("inputs") != snapshot(root):
        raise ValueError("Project inputs changed after tests. Run swift-sonar test again")
    recorded = data.get("artifacts")
    if not isinstance(recorded, dict):
        raise ValueError("Invalid artifact evidence")
    for raw in artifacts:
        artifact = local_path(root, raw)
        if recorded.get(artifact.relative_to(root).as_posix()) != file_hash(artifact):
            raise ValueError("Coverage artifact is not bound to this successful test run")
    return data
