"""Artifact evidence for project runs: deterministic digests and native/report validation.

Local, unsigned evidence for a trusted checkout. It reduces ordinary races and stale or
substituted outputs. It is not adversarial attestation against a process that controls the
checkout and the filesystem.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import xml.etree.ElementTree as ET

from .project_lcov import validate_lcov

KINDS = frozenset({"xcresult", "generic-coverage", "python-coverage", "javascript-coverage", "file"})
MANIFEST_VERSION = 1
SUPPORTED_RESULT_SCHEMAS = frozenset({"0.4.0", "0.5.0"})  # Fixture-tested against Xcode 27.1 (27A9275) and Xcode 27.2 (27B52).

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_NATIVE_JSON_BYTES = 256 * 1024 * 1024
MAX_TREE_FILES = 50_000
MAX_TREE_ENTRIES = 60_000
MAX_TREE_BYTES = 8 * 1024 * 1024 * 1024
MAX_TREE_DEPTH = 32
MAX_NODE_DEPTH = 64
MAX_PATH_CHARS = 4096
MAX_RECORDS = 5_000_000
MAX_DEVICE_CONFIGS = 64
MAX_SUPPLEMENT = 50_000
CLOCK_SKEW = 1.0  # Seconds of tolerated clock/timestamp granularity at each window edge.
TOOL_TIMEOUT = 300

# Observed: hex of the argument bytes ("inf" -> 696e66) or a 64-hex digest for long values.
_ARGS_ID = re.compile(r"(?:[0-9a-f]{2}){1,1024}")


def _fail(message: str) -> None:
    raise ValueError(message)


# ---------------------------------------------------------------- paths and safe reads

def _relative(value: object, what: str = "Artifact path") -> tuple[str, ...]:
    """Validate a lexical in-root relative POSIX path, without touching the filesystem."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        _fail(f"{what} must be a nonempty string")
    if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value):
        _fail(f"{what} contains control or backslash characters")
    if value.startswith("/") or value.endswith("/"):
        _fail(f"{what} must be a relative path without a trailing slash")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        _fail(f"{what} cannot contain empty, current, or parent components")
    return parts


def _root(root: Path) -> Path:
    try:
        resolved = Path(os.path.realpath(root, strict=True))
    except OSError:
        _fail("Project root is not accessible")
    if not resolved.is_dir():
        _fail("Project root must be a directory")
    return resolved


def _locate(root: Path, parts: tuple[str, ...], what: str) -> tuple[Path, os.stat_result]:
    """lstat every component below root. Any symlink, ancestor or leaf, is rejected."""
    current = root
    info = None
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            _fail(f"{what} is missing or inaccessible")
        if stat.S_ISLNK(info.st_mode):
            _fail(f"{what} cannot traverse a symbolic link")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _fail(f"{what} has a non-directory ancestor")
    assert info is not None
    return current, info


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path: Path, limit: int, keep: bool) -> tuple[str, int, bytes | None, os.stat_result]:
    """Hash one regular file without following links, failing if it changes while read."""
    try:
        before = os.lstat(path)
    except OSError:
        _fail("Evidence file disappeared")
    if not stat.S_ISREG(before.st_mode):
        _fail("Evidence must be regular files, not symlinks or special files")
    if before.st_size > limit:
        _fail("Evidence file exceeds the size bound")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        _fail("Evidence file cannot be opened without following links")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            _fail("Evidence must be regular files, not symlinks or special files")
        while block := os.read(fd, 1024 * 1024):
            total += len(block)
            if total > limit:
                _fail("Evidence file exceeds the size bound")
            digest.update(block)
            if keep:
                chunks.append(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        final = os.lstat(path)
    except OSError:
        _fail("Evidence file disappeared")
    if not (_signature(before) == _signature(opened) == _signature(after) == _signature(final)) \
            or total != before.st_size:
        _fail("Evidence file changed while it was read")
    return digest.hexdigest(), total, (b"".join(chunks) if keep else None), final


def _file_evidence(root: Path, parts: tuple[str, ...], what: str, limit: int,
                   keep: bool = False) -> tuple[dict, bytes | None]:
    path, _ = _locate(root, parts, what)
    sha, size, data, info = _read_regular(path, limit, keep)
    return {"type": "file", "sha256": sha, "size": size, "members": 1, "directories": 0,
            "bytes": size, "newest_mtime": info.st_mtime, "oldest_mtime": info.st_mtime}, data


def _tree_pass(base: Path, hash_files: bool) -> list[tuple]:
    """One traversal. Returns sorted (relative, type, size, sha256, signature) entries."""
    entries: list[tuple] = []
    totals = {"files": 0, "bytes": 0}

    def visit(path: Path, rel: str, depth: int) -> None:
        if depth > MAX_TREE_DEPTH:
            _fail("Directory evidence is nested too deeply")
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail("Directory evidence cannot contain symlinks or special files")
        with os.scandir(path) as listing:
            names = sorted(entry.name for entry in listing)
        if not names:
            _fail("Directory evidence cannot contain empty directories")
        entries.append((rel, "dir", 0, "", (info.st_dev, info.st_ino, info.st_mtime_ns)))
        if len(entries) > MAX_TREE_ENTRIES:
            _fail("Directory evidence has too many entries")
        for name in names:
            if not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or "/" in name:
                _fail("Directory evidence has an unsafe member name")
            child = path / name
            child_rel = name if rel == "." else f"{rel}/{name}"
            child_info = os.lstat(child)
            if stat.S_ISDIR(child_info.st_mode):
                visit(child, child_rel, depth + 1)
            elif stat.S_ISREG(child_info.st_mode):
                totals["files"] += 1
                totals["bytes"] += child_info.st_size
                if totals["files"] > MAX_TREE_FILES or totals["bytes"] > MAX_TREE_BYTES:
                    _fail("Directory evidence exceeds the size bound")
                if hash_files:
                    sha, size, _, final = _read_regular(child, MAX_TREE_BYTES, False)
                else:
                    sha, size, final = "", child_info.st_size, child_info
                entries.append((child_rel, "file", size, sha, _signature(final)))
                if len(entries) > MAX_TREE_ENTRIES:
                    _fail("Directory evidence has too many entries")
            else:
                _fail("Directory evidence cannot contain symlinks or special files")

    try:
        visit(base, ".", 0)
    except OSError:
        _fail("Directory evidence changed or is inaccessible")
    entries.sort(key=lambda item: item[0])
    return entries


def _tree_evidence(root: Path, parts: tuple[str, ...], what: str) -> dict:
    base, info = _locate(root, parts, what)
    if not stat.S_ISDIR(info.st_mode):
        _fail(f"{what} must be a directory")
    first = _tree_pass(base, True)
    second = _tree_pass(base, False)  # Detect additions, removals, renames, and rewrites.
    if [(e[0], e[1], e[2], e[4]) for e in first] != [(e[0], e[1], e[2], e[4]) for e in second]:
        _fail("Directory evidence changed while it was read")
    manifest = [[e[0], e[1], e[2], e[3]] for e in first]
    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    sha = hashlib.sha256(f"swift-sonar-tree-manifest-v{MANIFEST_VERSION}\n".encode() + payload).hexdigest()
    files = [e for e in first if e[1] == "file"]
    mtimes = [e[4][3] / 1e9 for e in files]
    return {"type": "tree", "sha256": sha, "manifest_version": MANIFEST_VERSION,
            "members": len(files), "directories": len(first) - len(files),
            "bytes": sum(e[2] for e in files),
            "newest_mtime": max(mtimes, default=0.0), "oldest_mtime": min(mtimes, default=0.0)}


def _artifact(artifact: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(artifact, dict):
        _fail("Artifact must be an object")
    allowed = {"kind", "path"} | ({"expectedTests"} if artifact.get("kind") == "xcresult" else set())
    if set(artifact) - allowed:
        _fail("Artifact has unsupported fields")
    kind = artifact.get("kind")
    if kind not in KINDS:
        _fail("Unknown artifact kind")
    if kind == "xcresult" and "expectedTests" not in artifact:
        _fail("xcresult artifacts require expectedTests")
    return kind, _relative(artifact.get("path"))


# ---------------------------------------------------------------- public: digest

def artifact_digest(root: Path, artifact: dict) -> dict:
    """Deterministic file or tree evidence for one declared artifact (no timestamps).

    Files report their SHA-256. xcresult directories report a versioned manifest hash over
    sorted relative members, entry types, file sizes, and file SHA-256 values. Symlinks (any
    ancestor or member), special files, empty directories, traversal, unstable reads, and
    oversized evidence are rejected.
    """
    digest = _digest(root, artifact)
    digest.pop("newest_mtime"), digest.pop("oldest_mtime")
    return digest


def _digest(root: Path, artifact: dict) -> dict:
    kind, parts = _artifact(artifact)
    root = _root(root)
    what = "Artifact"
    if kind == "xcresult":
        result = _tree_evidence(root, parts, what)
        expected, _ = _file_evidence(root, _relative(artifact["expectedTests"], "expectedTests path"),
                                     "expectedTests", MAX_JSON_BYTES)
        result["expectedTests"] = {"path": artifact["expectedTests"], "sha256": expected["sha256"]}
    else:
        result, _ = _file_evidence(root, parts, what, MAX_FILE_BYTES)
    return {"kind": kind, "path": "/".join(parts), **result}


# ---------------------------------------------------------------- strict JSON / XML

def _no_duplicates(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON contains a duplicate key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    _fail("JSON contains a non-finite number")


def _json(data: bytes, what: str) -> object:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail(f"{what} is not valid strict JSON")


_DTD = re.compile(r'<!DOCTYPE\s+coverage\s+SYSTEM\s+"[^"<>\[\]&]{0,512}"\s*>')
_XML_DECL = re.compile(r'^<\?xml\s[^>]*?encoding\s*=\s*["\']([^"\']+)["\']')


def _xml(data: bytes, what: str) -> ET.Element:
    """Parse XML with no DTD subset, entity, or external reference possible."""
    if len(data) > MAX_XML_BYTES:
        _fail(f"{what} exceeds the size bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"{what} must be UTF-8 XML")
    if text.startswith("\ufeff"):
        _fail(f"{what} must not start with a byte order mark")
    declared = _XML_DECL.match(text)
    if declared and declared.group(1).lower() not in {"utf-8", "utf8"}:
        _fail(f"{what} must be UTF-8 XML")
    # coverage.py emits exactly one external-DTD DOCTYPE with no internal subset. Allow only that.
    text = _DTD.sub("", text, count=1)
    if re.search(r"<!(?!--|\[CDATA\[)", text):
        _fail(f"{what} contains an unsafe XML declaration")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, RecursionError):
        _fail(f"{what} is not well-formed XML")


def _count(value: object, what: str, minimum: int = 0) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 12:
        _fail(f"{what} must be a nonnegative integer")
    number = int(value)
    if number < minimum:
        _fail(f"{what} is out of range")
    return number


# ---------------------------------------------------------------- coverage reports

def _inventory(root: Path, inventory: object) -> tuple[dict[str, int], set[str]]:
    if not isinstance(inventory, dict) or not inventory.get("sources"):
        _fail("Inventory requires nonempty sources")
    raw = inventory["sources"]
    sources: dict[str, int] = {}
    items = raw.items() if isinstance(raw, dict) else ((item, None) for item in raw)
    for name, lines in items:
        rel = "/".join(_relative(name, "Inventory source"))
        if lines is None:
            _, data = _file_evidence(root, _relative(rel), "Inventory source", MAX_FILE_BYTES, True)
            try:
                lines = len(data.decode("utf-8").splitlines())
            except UnicodeDecodeError:
                _fail("Inventory source is not UTF-8")
        if type(lines) is not int or lines < 0:
            _fail("Inventory line counts must be nonnegative integers")
        sources[rel] = lines
    tests = {"/".join(_relative(name, "Inventory test")) for name in inventory.get("tests", ())}
    if tests & set(sources):
        _fail("Inventory production and test classifications overlap")
    return sources, tests


def _contained(root: Path, raw: str) -> str | None:
    """Relative path if raw is lexically under root, else None. Never follows links."""
    candidate = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    try:
        relative = os.path.relpath(candidate, root)
    except ValueError:
        return None
    if relative == "." or relative == ".." or relative.startswith("../"):
        return None
    return relative


def _record_path(root: Path, raw: object, bases: list[str], sources: dict[str, int],
                 tests: set[str]) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS \
            or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        _fail("Report source must be a nonempty, control-free string")
    if ".." in raw.split("/"):
        _fail("Report source cannot contain parent components")
    if os.path.isabs(raw):
        options = [raw]
    else:
        options = [os.path.join(base, raw) for base in bases] or [raw]
    contained = [rel for option in options if (rel := _contained(root, option)) is not None]
    if not contained:
        _fail("Report source is outside the project root")
    if any(rel in tests for rel in contained):
        _fail("Coverage cannot refer to test files")
    matches = {rel for rel in contained if rel in sources}
    if not matches:
        _fail("Report source is not in the production inventory")
    if len(matches) > 1:
        _fail("Report source is ambiguous across <source> roots")
    rel = matches.pop()
    _locate(root, _relative(rel), "Report source")  # Rejects symlinked ancestors and leaves.
    return rel


def _sonar_generic(element: ET.Element, root: Path, sources: dict, tests: set) -> dict[str, dict[int, bool]]:
    files: dict[str, dict[int, bool]] = {}
    for child in element:
        if child.tag != "file" or set(child.attrib) - {"path"}:
            _fail("Generic coverage allows only <file path> elements")
        path = _record_path(root, child.get("path"), [], sources, tests)
        if path in files:
            _fail("Generic coverage repeats a source file")
        lines = files.setdefault(path, {})
        for line in child:
            if line.tag != "lineToCover" or set(line.attrib) - {"lineNumber", "covered",
                                                                  "branchesToCover", "coveredBranches"}:
                _fail("Generic coverage allows only <lineToCover> elements")
            number = _count(line.get("lineNumber"), "Coverage line", 1)
            if number > sources[path]:
                _fail("Coverage line is outside the current source")
            covered = line.get("covered")
            if covered not in {"true", "false"} or number in lines:
                _fail("Coverage line requires covered=true|false and cannot repeat")
            if (line.get("branchesToCover") is None) != (line.get("coveredBranches") is None):
                _fail("Branch attributes must appear together")
            if line.get("branchesToCover") is not None:
                branches = _count(line.get("branchesToCover"), "branchesToCover")
                if _count(line.get("coveredBranches"), "coveredBranches") > branches:
                    _fail("Covered branches exceed branches to cover")
            lines[number] = covered == "true"
    return files


def _cobertura(element: ET.Element, root: Path, sources: dict, tests: set) -> dict[str, dict[int, bool]]:
    bases = []
    for source in element.findall("sources/source"):
        text = (source.text or "").strip()
        if not text:
            _fail("Cobertura <source> is empty")
        resolved = Path(os.path.realpath(text)) if os.path.isabs(text) else None
        if resolved is None or _contained(root, str(resolved)) is None and resolved != root:
            _fail("Cobertura <source> must be an absolute path inside the project")
        bases.append(str(resolved))
    files: dict[str, dict[int, bool]] = {}
    for cls in element.findall("packages/package/classes/class"):
        path = _record_path(root, cls.get("filename"), bases, sources, tests)
        if path in files:
            _fail("Cobertura repeats a source file")
        lines = files.setdefault(path, {})
        for line in cls.findall("lines/line"):
            number = _count(line.get("number"), "Coverage line", 1)
            if number > sources[path]:
                _fail("Coverage line is outside the current source")
            hits = _count(line.get("hits"), "Coverage hits")
            if number in lines:
                _fail("Coverage line cannot repeat")
            lines[number] = hits > 0
    return files


def _validate_coverage(root: Path, kind: str, data: bytes, inventory: object) -> dict:
    sources, tests = _inventory(root, inventory)
    if kind == "javascript-coverage":
        def resolve_source(raw: str) -> tuple[str, int]:
            path = _record_path(root, raw, [], sources, tests)
            if Path(path).suffix not in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
                _fail("LCOV source must be JavaScript or TypeScript")
            _, info = _locate(root, _relative(path), "LCOV source")
            if not stat.S_ISREG(info.st_mode):
                _fail("LCOV source must be a regular file")
            return path, sources[path]
        return validate_lcov(data, resolve_source, _count, MAX_RECORDS, MAX_PATH_CHARS)
    element = _xml(data, "Coverage report")
    if element.tag != "coverage":
        _fail("Coverage report root must be <coverage>")
    is_cobertura = element.find("packages") is not None
    if is_cobertura:
        if kind != "python-coverage":
            _fail("generic-coverage requires Sonar generic XML, not Cobertura")
        files = _cobertura(element, root, sources, tests)
        form = "cobertura"
    else:
        if kind == "python-coverage":
            _fail("python-coverage requires a Cobertura report")
        if element.get("version") != "1" or set(element.attrib) - {"version"}:
            _fail("Generic coverage requires <coverage version=\"1\">")
        files = _sonar_generic(element, root, sources, tests)
        form = "sonar-generic"
    total = sum(len(lines) for lines in files.values())
    if not files or not total or total > MAX_RECORDS:
        _fail("Coverage report has no usable line observations")
    observations = [[path, sorted(lines.items())] for path, lines in sorted(files.items())]
    canonical = json.dumps(observations, separators=(",", ":")).encode()
    return {"format": form, "files": len(files), "lines": total,
            "covered_lines": sum(v for lines in files.values() for v in lines.values()),
            "observations_sha256": hashlib.sha256(canonical).hexdigest()}


# ---------------------------------------------------------------- native xcresult

def _reader_identity() -> dict:
    """Identity of the installed native result reader. Fail closed on unknown formats."""
    out = _xcresulttool(["version"])
    match = re.match(r"xcresulttool version (\d+), schema version: (\d+\.\d+\.\d+)", out.decode("utf-8", "replace"))
    if not match or match.group(2) not in SUPPORTED_RESULT_SCHEMAS:
        _fail("Unsupported or unrecognized xcresulttool schema version")
    located = _run(["xcrun", "--find", "xcresulttool"]).decode("utf-8", "replace").strip()
    return {"tool": "xcresulttool", "version": match.group(1), "schema": match.group(2), "path": located}


def _run(argv: list[str]) -> bytes:
    keep = ("PATH", "HOME", "DEVELOPER_DIR", "TMPDIR", "LANG", "LC_ALL")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    try:
        result = subprocess.run(argv, capture_output=True, env=env, timeout=TOOL_TIMEOUT,
                                stdin=subprocess.DEVNULL, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        _fail("Native result tool could not run")
    if result.returncode or len(result.stdout) > MAX_NATIVE_JSON_BYTES:
        _fail("Native result tool failed or produced oversized output")
    return result.stdout


def _xcresulttool(args: list[str]) -> bytes:
    return _run(["xcrun", "xcresulttool", *args])


def _read_native(bundle: Path) -> tuple[object, object]:
    base = ["get", "test-results"]
    summary = _json(_xcresulttool([*base, "summary", "--path", str(bundle), "--compact"]), "Native summary")
    tests = _json(_xcresulttool([*base, "tests", "--path", str(bundle), "--compact"]), "Native test list")
    return summary, tests


def _number(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail("Native value must be a finite number")
    return float(value)


_DIAGNOSTIC_FAILURES = ContextVar("native_diagnostic_failures", default=False)


def _accepted_result(value: object) -> bool:
    return value == "Passed" or (_DIAGNOSTIC_FAILURES.get() and value == "Failed")


def _tally(summary: object) -> dict:
    """Top-level and per device/configuration native counts. Anything not clean passed fails."""
    if not isinstance(summary, dict):
        _fail("Native summary must be an object")
    if not _accepted_result(summary.get("result")):
        _fail("Native result is not Passed")
    counts = {}
    for key in ("totalTestCount", "passedTests", "failedTests", "skippedTests", "expectedFailures"):
        if type(summary.get(key)) is not int or summary[key] < 0:
            _fail("Native summary counts must be nonnegative integers")
        counts[key] = summary[key]
    if not counts["totalTestCount"] or (counts["failedTests"] and not _DIAGNOSTIC_FAILURES.get()) or counts["skippedTests"] \
            or counts["expectedFailures"] or counts["passedTests"] + counts["failedTests"] != counts["totalTestCount"]:
        _fail("Native summary must contain tests and no failed, skipped, or expected-failure tests")
    if (not isinstance(summary.get("testFailures"), list)
            or (not counts["failedTests"] and summary["testFailures"])
            or (bool(counts["failedTests"]) != (summary["result"] == "Failed"))):
        _fail("Native summary reports inconsistent test failures")
    configs = summary.get("devicesAndConfigurations")
    if not isinstance(configs, list) or not configs or len(configs) > MAX_DEVICE_CONFIGS:
        _fail("Native summary requires devicesAndConfigurations")
    entries: dict[tuple[str, str], int] = {}
    for config in configs:
        if not isinstance(config, dict) or not isinstance(config.get("device"), dict) \
                or not isinstance(config.get("testPlanConfiguration"), dict):
            _fail("Native device summary is malformed")
        device_id = config["device"].get("deviceId")
        config_id = config["testPlanConfiguration"].get("configurationId")
        if not isinstance(device_id, str) or not device_id or not isinstance(config_id, str) or not config_id:
            _fail("Native device summary requires deviceId and configurationId")
        for key in ("passedTests", "failedTests", "skippedTests", "expectedFailures"):
            if type(config.get(key)) is not int or config[key] < 0:
                _fail("Native device summary counts are malformed")
        if config["skippedTests"] or config["expectedFailures"] or (config["failedTests"] and not _DIAGNOSTIC_FAILURES.get()):
            _fail("Native device summary has failed, skipped, or expected-failure tests")
        executed = config["passedTests"] + config["failedTests"]
        if executed <= 0 or (device_id, config_id) in entries:
            _fail("Native device summary has no executed tests or repeats a device/configuration")
        entries[(device_id, config_id)] = executed
    return {"cases": counts["totalTestCount"], "entries": entries, "result": summary["result"],
            "failed": counts["failedTests"], "passed": counts["passedTests"],
            "start": _number(summary.get("startTime")), "finish": _number(summary.get("finishTime"))}


def _check_subtree(node: object, depth: int, allow_case: bool) -> list:
    """Validate a node and its descendants are clean passes. Returns its children."""
    if depth > MAX_NODE_DEPTH or not isinstance(node, dict) \
            or not isinstance(node.get("nodeType"), str) or not isinstance(node.get("name"), str):
        _fail("Native test node is malformed")
    kind = node["nodeType"]
    if "result" in node and not _accepted_result(node["result"]):
        _fail("Native test node is failed, skipped, or not passed")
    if kind in {"Skip Message", "Expected Failure"} or (kind == "Failure Message" and not _DIAGNOSTIC_FAILURES.get()):
        _fail("Native test tree contains a failure, skip, or expected-failure node")
    if kind == "Test Case" and not allow_case:
        _fail("Nested native Test Case nodes are unsupported")
    children = node.get("children", [])
    if not isinstance(children, list):
        _fail("Native test node children are malformed")
    return children


def _walk_tree(tests: dict, on_case) -> None:
    def visit(node: object, depth: int) -> None:
        children = _check_subtree(node, depth, True)
        if node["nodeType"] == "Test Case":
            if not _accepted_result(node.get("result")):
                _fail("Native Test Case requires Passed")
            on_case(node)
            return
        for child in children:
            visit(child, depth + 1)

    for node in tests["testNodes"]:
        visit(node, 0)


def _case_names(node: dict) -> tuple[str, str]:
    identity, url = node.get("nodeIdentifier"), node.get("nodeIdentifierURL")
    if not isinstance(identity, str) or not identity or not isinstance(url, str) or not url:
        _fail("Native Test Case requires nodeIdentifier and nodeIdentifierURL")
    return identity, url


def _args_suffix(argument: object, url: str) -> str:
    argument_url = argument.get("nodeIdentifierURL") if isinstance(argument, dict) else None
    prefix = url + "?args="
    if not _accepted_result(argument.get("result")) or not isinstance(argument_url, str) \
            or not argument_url.startswith(prefix) or not _ARGS_ID.fullmatch(argument_url[len(prefix):]):
        _fail("Native parameterized instance has an unsupported identity")
    return argument_url[len(prefix):]


def _identities(tests: object) -> tuple[list[str], int]:
    """Single-device shape: Test Case nodeIdentifier, or id + ?args=<hex> per parameterized instance."""
    if not isinstance(tests, dict) or not isinstance(tests.get("testNodes"), list) \
            or not isinstance(tests.get("devices"), list) or len(tests["devices"]) != 1 \
            or not isinstance(tests.get("testPlanConfigurations"), list) \
            or len(tests["testPlanConfigurations"]) != 1:
        _fail("Native test list has an unsupported shape")
    found: list[str] = []
    cases = 0

    def on_case(node: dict) -> None:
        nonlocal cases
        identity, url = _case_names(node)
        cases += 1
        children = node.get("children", [])
        if not isinstance(children, list):
            _fail("Native test node children are malformed")
        for child in children:
            _check_subtree(child, 1, False)
        arguments = [c for c in children if c.get("nodeType") == "Arguments"]
        if arguments:
            found.extend(identity + "?args=" + _args_suffix(argument, url) for argument in arguments)
        else:
            found.append(identity)

    _walk_tree(tests, on_case)
    return found, cases


# Merged (multi-device) shape: Test Case -> Device -> Test Plan Configuration -> [Arguments].

def _has_device_nodes(tests: dict) -> bool:
    seen = []

    def on_case(node: dict) -> None:
        children = node.get("children", [])
        seen.append(isinstance(children, list) and any(
            isinstance(c, dict) and c.get("nodeType") == "Device" for c in children))

    _walk_tree(tests, on_case)
    if any(seen) and not all(seen):
        _fail("Native test tree mixes merged and single-device cases")
    return bool(seen) and all(seen)


def _device_table(tests: dict) -> dict[str, tuple[str, str, str]]:
    table = {}
    for device in tests.get("devices", []):
        fields = [device.get(k) if isinstance(device, dict) else None
                  for k in ("deviceId", "platform", "osVersion", "deviceName")]
        if not all(isinstance(f, str) and f and "|" not in f and not any(ord(c) < 32 for c in f)
                   for f in fields) or fields[0] in table:
            _fail("Native device table is malformed")
        table[fields[0]] = (fields[1], fields[2], fields[3])
    labels = list(table.values())
    if not table or len(set(labels)) != len(labels):
        _fail("Native devices are ambiguous: platform, OS, and name must be unique")
    return table


def _target(url: str) -> str:
    prefix = "test://com.apple.xcode/"
    parts = url[len(prefix):].split("/") if url.startswith(prefix) else []
    if len(parts) < 3 or not parts[1] or "|" in parts[1] or any(ord(c) < 32 for c in parts[1]):
        _fail("Native Test Case URL has no target")
    return parts[1]


def _units(target: str, identity: str, url: str, devices: list, table: dict) -> list[tuple]:
    """(device_id, config_id, target, identity, args) per executed instance."""
    units = []
    for device in devices:
        for child in _check_subtree(device, 1, False):
            if device.get("nodeType") != "Device" or device.get("nodeIdentifier") not in table:
                _fail("Native Test Case child is not a known Device")
            if child.get("nodeType") != "Test Plan Configuration" or not _accepted_result(child.get("result")) \
                    or not isinstance(child.get("nodeIdentifier"), str) or not child["nodeIdentifier"]:
                _fail("Native Device child is not a passed Test Plan Configuration")
            arguments = []
            for leaf in _check_subtree(child, 2, False):
                kind = leaf.get("nodeType") if isinstance(leaf, dict) else None
                if kind == "Arguments":
                    _check_subtree(leaf, 3, False)
                    arguments.append(leaf)
                elif kind != "Runtime Warning" and not (kind == "Failure Message" and _DIAGNOSTIC_FAILURES.get()):
                    _fail("Native Test Plan Configuration has an unsupported child")
            key = (device["nodeIdentifier"], child["nodeIdentifier"], target, identity)
            if arguments:
                units.extend(key + (_args_suffix(a, url),) for a in arguments)
            else:
                units.append(key + ("",))
    return units


def _merged_units(tests: dict, table: dict) -> list[tuple]:
    found: list[tuple] = []

    def on_case(node: dict) -> None:
        identity, url = _case_names(node)
        children = node.get("children", [])
        if not isinstance(children, list) or not children:
            _fail("Native merged Test Case has no Device children")
        found.extend(_units(_target(url), identity, url, children, table))

    _walk_tree(tests, on_case)
    return found


def _legacy_value(node: object, depth: int = 0) -> object:
    if depth > 200:
        _fail("Legacy result tree is nested too deeply")
    if isinstance(node, dict):
        if "_value" in node:
            return node["_value"]
        if "_values" in node and isinstance(node["_values"], list):
            return [_legacy_value(item, depth + 1) for item in node["_values"]]
        return {k: _legacy_value(v, depth + 1) for k, v in node.items() if k != "_type"}
    return node


def _legacy(bundle: Path, identifier: str | None = None) -> object:
    args = ["get", "--legacy", "--path", str(bundle), "--format", "json"]
    if identifier is not None:
        args += ["--id", identifier]
    return _legacy_value(_json(_xcresulttool(args), "Legacy result index"))


def _legacy_index(bundle: Path, absent: set[str]) -> dict[tuple[str, str, str], str]:
    """Index (target, case identifier, args) -> case URL for targets the modern tree omits.

    The legacy plan is used only to learn which cases exist. Every instance's device,
    configuration, identity, and pass state still comes from the modern test-details reader.
    """
    root = _legacy(bundle)
    actions = root.get("actions") if isinstance(root, dict) else None
    if not isinstance(actions, list):
        _fail("Legacy result index is malformed")
    index: dict[tuple[str, str, str], str] = {}
    for action in actions:
        result = action.get("actionResult") if isinstance(action, dict) else None
        reference = result.get("testsRef") if isinstance(result, dict) else None
        if not isinstance(reference, dict) or not isinstance(reference.get("id"), str):
            continue
        plan = _legacy(bundle, reference["id"])
        for summary in plan.get("summaries", []) if isinstance(plan, dict) else []:
            for testable in summary.get("testableSummaries", []):
                target = testable.get("targetName")
                if target not in absent:
                    continue
                if result.get("status") != "succeeded" and not (_DIAGNOSTIC_FAILURES.get() and result.get("status") == "failed"):
                    _fail("Legacy action for an omitted target did not succeed")

                def leaf(test: dict) -> None:
                    if test.get("subtests"):
                        for child in test["subtests"]:
                            leaf(child)
                        return
                    identifier, url = test.get("identifier"), test.get("identifierURL")
                    if test.get("testStatus") is None and identifier in {"All tests", "Selected tests"}:
                        return
                    if (test.get("testStatus") != "Success" and not (_DIAGNOSTIC_FAILURES.get() and test.get("testStatus") == "Failure")) or not isinstance(identifier, str) \
                            or not isinstance(url, str) or _target(url) != target:
                        _fail("Legacy test leaf is not a successful case of its target")
                    base, _, args = url.partition("?args=")
                    if args and not _ARGS_ID.fullmatch(args):
                        _fail("Legacy parameterized instance has an unsupported identity")
                    index[(target, identifier, args)] = base

                for test in testable.get("tests", []):
                    leaf(test)
    return index


def _supplement(bundle: Path, tests: dict, table: dict, known: list[tuple]) -> list[tuple]:
    present = {unit[2] for unit in known}
    index = _legacy_index(bundle, _legacy_targets(bundle) - present)
    if not index or len(index) > MAX_SUPPLEMENT:
        _fail("Native test list omits tests that cannot be enumerated")
    by_case: dict[tuple[str, str], set[str]] = {}
    urls: dict[tuple[str, str], str] = {}
    for (target, identifier, args), url in index.items():
        by_case.setdefault((target, identifier), set()).add(args)
        urls[(target, identifier)] = url
    found: list[tuple] = []
    for (target, identifier), expected_args in sorted(by_case.items()):
        details = _json(_xcresulttool(["get", "test-results", "test-details", "--path", str(bundle),
                                       "--compact", "--test-id", urls[(target, identifier)]]), "Native test details")
        if not isinstance(details, dict) or not _accepted_result(details.get("testResult")) \
                or details.get("testIdentifier") != identifier \
                or not isinstance(details.get("testRuns"), list):
            _fail("Native test details are not a passed run of the indexed case")
        units = _units(target, identifier, urls[(target, identifier)], details["testRuns"], table)
        if {unit[4] for unit in units} != expected_args:
            _fail("Native test details disagree with the legacy index for an omitted target")
        found.extend(units)
    return found


def _legacy_targets(bundle: Path) -> set[str]:
    root = _legacy(bundle)
    targets: set[str] = set()
    for action in root.get("actions", []) if isinstance(root, dict) else []:
        reference = (action.get("actionResult") or {}).get("testsRef") if isinstance(action, dict) else None
        if isinstance(reference, dict) and isinstance(reference.get("id"), str):
            plan = _legacy(bundle, reference["id"])
            for summary in plan.get("summaries", []) if isinstance(plan, dict) else []:
                targets.update(t["targetName"] for t in summary.get("testableSummaries", [])
                               if isinstance(t.get("targetName"), str))
    return targets


def _merged(bundle: Path, summary: dict, tests: dict, tally: dict) -> dict:
    table = _device_table(tests)
    entries = tally["entries"]
    if any(device not in table for device, _ in entries):
        _fail("Native summary names a device missing from the device table")
    units = _merged_units(tests, table)

    def counts(items: list[tuple]) -> dict:
        result: dict[tuple[str, str], int] = {}
        for unit in items:
            result[unit[:2]] = result.get(unit[:2], 0) + 1
        return result

    supplemented = 0
    if counts(units) != entries:
        extra = _supplement(bundle, tests, table, units)
        units += extra
        supplemented = len(extra)
    if counts(units) != entries:
        _fail("Native test units do not match the per device/configuration summary counts")
    if len({u[2:4] for u in units}) != tally["cases"]:
        _fail("Native distinct test cases do not match totalTestCount")
    labels = []
    for device, config, target, identity, args in units:
        platform, version, name = table[device]
        labels.append("|".join((platform, version, name, f"cfg{config}", target, identity))
                      + (f"?args={args}" if args else ""))
    if len(set(labels)) != len(labels):
        _fail("Native test identities are not unique")
    per_device = [{"platform": table[d][0], "os": table[d][1], "device": table[d][2],
                   "configuration": c, "instances": n} for (d, c), n in sorted(entries.items(),
                                                                              key=lambda kv: table[kv[0][0]])]
    return {"shape": "merged", "identities": sorted(labels), "test_cases": tally["cases"],
            "devices": per_device, "supplemented_instances": supplemented}


def native_xcresult(bundle: Path, *, allow_failed: bool = False) -> dict:
    """Validate native evidence, with explicitly scoped diagnostic failure retention."""
    if type(allow_failed) is not bool:
        _fail("allow_failed must be a boolean")
    token = _DIAGNOSTIC_FAILURES.set(allow_failed)
    try:
        return _native_xcresult(bundle)
    finally:
        _DIAGNOSTIC_FAILURES.reset(token)


def _native_xcresult(bundle: Path) -> dict:
    """Read a bundle with the installed native reader. Returns identities and validated counts.

    Single-device bundles use plain identities. Merged multi-device bundles use
    `platform|os|device|cfg<id>|target|nodeIdentifier[?args=<hex>]`. Counting basis is
    executed instances (parameterized Arguments counted individually) per device/configuration.
    """
    reader = _reader_identity()
    summary, tests = _read_native(bundle)
    tally = _tally(summary)
    if not isinstance(tests, dict) or not isinstance(tests.get("testNodes"), list) \
            or not isinstance(tests.get("devices"), list) \
            or not isinstance(tests.get("testPlanConfigurations"), list):
        _fail("Native test list has an unsupported shape")
    if _has_device_nodes(tests) or len(tally["entries"]) > 1:
        merged = _merged(bundle, summary, tests, tally)
        identities, cases, extra = merged.pop("identities"), merged.pop("test_cases"), merged
    else:
        identities, cases = _identities(tests)
        (instances,) = tally["entries"].values()
        if cases != tally["cases"] or len(identities) != instances:
            _fail("Native test tree does not match the native summary counts")
        if len(set(identities)) != len(identities):
            _fail("Native test identities are not unique")
        extra = {"shape": "single"}
    return {"reader": reader, "result": tally["result"], "started_at": tally["start"],
            "finished_at": tally["finish"], "test_cases": cases, "test_instances": len(identities),
            "failed": tally["failed"], "passed": tally["passed"], "skipped": 0,
            "expected_failures": 0, "identities": sorted(identities), **extra}


def _expected_tests(root: Path, value: object) -> tuple[list[str], str]:
    evidence, data = _file_evidence(root, _relative(value, "expectedTests path"), "expectedTests",
                                    MAX_JSON_BYTES, True)
    parsed = _json(data, "expectedTests")
    if not isinstance(parsed, list) or not parsed or not all(
            isinstance(item, str) and item and not any(ord(c) < 32 for c in item) for item in parsed):
        _fail("expectedTests must be a nonempty JSON array of identifier strings")
    if len(set(parsed)) != len(parsed):
        _fail("expectedTests contains duplicate identifiers")
    return sorted(parsed), evidence["sha256"]


def _sample(items: set[str]) -> str:
    return ", ".join(sorted(items)[:3])[:300]


def _validate_xcresult(root: Path, artifact: dict, parts: tuple[str, ...], started: float,
                       finished: float, allow_failed: bool = False) -> dict:
    base, _ = _locate(root, parts, "Artifact")
    if not (base / "Info.plist").is_file():
        _fail("Directory is not an xcresult bundle")
    expected, expected_sha = _expected_tests(root, artifact["expectedTests"])
    native = native_xcresult(base, allow_failed=allow_failed)
    if not (started - CLOCK_SKEW <= native["started_at"] <= native["finished_at"] <= finished + CLOCK_SKEW):
        _fail("Native test timestamps are outside this run")
    actual = native["identities"]
    if actual != expected:
        missing, unexpected = set(expected) - set(actual), set(actual) - set(expected)
        _fail(f"Native test identities differ from expectedTests: {len(missing)} missing"
              f" ({_sample(missing)}), {len(unexpected)} unexpected ({_sample(unexpected)})")
    identities = native.pop("identities")
    native["identities_sha256"] = hashlib.sha256("\n".join(identities).encode()).hexdigest()
    native["expected_tests_sha256"] = expected_sha
    native["identity_format"] = ("nodeIdentifier[?args=<hex>]" if native["shape"] == "single" else
                                 "platform|os|device|cfg<id>|target|nodeIdentifier[?args=<hex>]")
    return native


# ---------------------------------------------------------------- public: validate

def validate_artifact(root: Path, artifact: dict, inventory: dict, started_at: float,
                      finished_at: float, *, allow_failed: bool = False) -> dict:
    """Validate one artifact produced inside [started_at, finished_at].

    Returns the digest plus kind-specific facts: native counts/identity reconciliation for
    xcresult, or a coverage summary for generic-coverage/python-coverage/javascript-coverage. Reports and
    bundles are never rewritten. The digest is recomputed afterwards and must be unchanged.
    """
    for value in (started_at, finished_at):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            _fail("Run window must be finite positive timestamps")
    if started_at > finished_at:
        _fail("Run window is reversed")
    kind, parts = _artifact(artifact)
    root = _root(root)
    full = _digest(root, artifact)
    if full["oldest_mtime"] < started_at - CLOCK_SKEW or full["newest_mtime"] > finished_at + CLOCK_SKEW:
        _fail("Artifact was not written during this run")
    before = {k: v for k, v in full.items() if not k.endswith("_mtime")}
    result: dict = {"kind": kind, "path": before["path"], "digest": before,
                    "written_between": [full["oldest_mtime"], full["newest_mtime"]]}
    if kind == "xcresult":
        result["native"] = _validate_xcresult(root, artifact, parts, started_at, finished_at, allow_failed)
    elif kind in {"generic-coverage", "python-coverage", "javascript-coverage"}:
        _, data = _file_evidence(root, parts, "Artifact", MAX_XML_BYTES, True)
        if hashlib.sha256(data).hexdigest() != before["sha256"]:
            _fail("Coverage report changed while it was read")
        result["coverage"] = _validate_coverage(root, kind, data, inventory)
    if _digest(root, artifact) != full:
        _fail("Artifact changed during validation")
    return result
