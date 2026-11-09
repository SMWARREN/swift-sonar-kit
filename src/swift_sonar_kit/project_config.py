"""Strict project recipe loader, Java properties codec, safe paths and scope inventory.

Everything here is read-only. A recipe binds a checked-in scanner properties baseline to a
test argv, typed artifacts and a strict lint policy. Unknown fields, keys and report
references fail closed. The test argv is trusted repository code, not a sandbox.

SPDX-License-Identifier: GPL-3.0-only
"""

import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

SCHEMA = 1
RESERVED = frozenset({".git", ".swift-sonar", ".scannerwork"})
LANGUAGES = {".swift": "swift", ".py": "python", ".php": "php",
             ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
             ".ts": "typescript", ".tsx": "typescript"}
DEFAULT_LINT = ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"]
KINDS = frozenset({"xcresult", "generic-coverage", "python-coverage", "javascript-coverage", "file"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
MAX_CONFIG_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_FILES = 200_000

METADATA = frozenset({"sonar.projectKey", "sonar.projectName", "sonar.projectVersion",
                      "sonar.sourceEncoding", "sonar.python.version"})
ROOT_LISTS = frozenset({"sonar.sources", "sonar.tests"})
PATTERN_LISTS = frozenset({"sonar.inclusions", "sonar.exclusions", "sonar.test.inclusions",
                           "sonar.test.exclusions", "sonar.coverage.exclusions",
                           "sonar.cpd.exclusions"})
# Report registry: property -> (artifact kind, may be a list or glob).
REPORTS = {"sonar.apple.resultBundlePath": ("xcresult", False),
           "sonar.coverageReportPaths": ("generic-coverage", True),
           "sonar.python.coverage.reportPaths": ("python-coverage", True),
           "sonar.javascript.lcov.reportPaths": ("javascript-coverage", True)}
GATE = {"sonar.qualitygate.wait": "true", "sonar.qualitygate.timeout": "300"}
PROTECTED = frozenset({"sonar.host.url", "sonar.token", "sonar.login", "sonar.password",
                       "project.settings", "sonar.projectBaseDir", "sonar.working.directory",
                       "sonar.scanner.skip", "sonar.scanner.dumpToFile", "sonar.analysis.mode",
                       "sonar.scanner.apiBaseUrl", "sonar.scanner.metadataFilePath"})
PROTECTED_PREFIXES = ("sonar.scanner.", "sonar.proxy", "sonar.ws.", "sonar.auth", "sonar.organization",
                      "sonar.branch", "sonar.pullrequest", "sonar.modules", "sonar.jdbc", "sonar.cfamily.")


def _fail(message: str) -> None:
    raise ValueError(message)


def _controls(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


# ---------------------------------------------------------------- safe paths

def _canonical_root(root: Path) -> Path:
    try:
        resolved = Path(os.path.realpath(root, strict=True))
    except OSError:
        _fail("Project root is not accessible")
    if not resolved.is_dir():
        _fail("Project root must be a directory")
    return resolved


def _parts(value: object, what: str = "Path") -> tuple[str, ...]:
    if isinstance(value, Path):
        value = os.fspath(value)
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        _fail(f"{what} must be a nonempty string")
    if _controls(value) or "\\" in value:
        _fail(f"{what} contains control or backslash characters")
    if value.startswith("/"):
        _fail(f"{what} must be relative to the project root")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        _fail(f"{what} cannot contain empty, current, parent or trailing-slash components")
    return parts


def safe_path(root: Path, value: object, must_exist: bool = False) -> Path:
    """Resolve a lexical relative path below the canonical root.

    Every existing component is lstat-ed. A symlink at any depth, a non-directory ancestor,
    traversal, an absolute path or control characters are rejected. Missing trailing
    components are allowed unless must_exist. Nothing is created.
    """
    base = _canonical_root(root)
    parts = _parts(value)
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if must_exist:
                _fail(f"Path does not exist: {'/'.join(parts)}")
            return base.joinpath(*parts)
        except OSError:
            _fail(f"Path is inaccessible: {'/'.join(parts)}")
        if stat.S_ISLNK(info.st_mode):
            _fail(f"Path cannot traverse a symbolic link: {'/'.join(parts)}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _fail(f"Path has a non-directory ancestor: {'/'.join(parts)}")
    return base.joinpath(*parts)


def _read(path: Path, limit: int, what: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        _fail(f"{what} cannot be opened")
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            _fail(f"{what} must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        _fail(f"{what} exceeds the size bound")
    return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- strict JSON

def _no_duplicates(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("Recipe JSON contains a duplicate key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    _fail("Recipe JSON contains a non-finite number")


def strict_json(data: bytes, what: str) -> object:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail(f"{what} is not valid strict JSON")


# ---------------------------------------------------------------- Java properties

_SPACE = " \t\f"


def _logical_lines(text: str) -> list[str]:
    lines, pending = [], None
    for natural in re.split(r"\r\n|\r|\n", text):
        if pending is None:
            natural = natural.lstrip(_SPACE)
            if not natural or natural[0] in "#!":
                continue
        else:
            natural = natural.lstrip(_SPACE)
        slashes = len(natural) - len(natural.rstrip("\\"))
        if slashes % 2:
            pending = (pending or "") + natural[:-1]
            continue
        lines.append((pending or "") + natural)
        pending = None
    if pending is not None:
        _fail("Properties end inside a line continuation")
    return lines


def _unescape(text: str, start: int, stop_at_separator: bool) -> tuple[str, int]:
    out, units, i = [], [], start
    def flush() -> None:
        if units:
            try:
                out.append(bytes(b for u in units for b in u.to_bytes(2, "big")).decode("utf-16-be"))
            except UnicodeDecodeError:
                _fail("Properties contain an unpaired surrogate escape")
            units.clear()
    while i < len(text):
        c = text[i]
        if stop_at_separator and c in "=:" + _SPACE:
            break
        if c != "\\":
            flush()
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(text):
            _fail("Properties contain a dangling backslash")
        c = text[i]
        if c == "u":
            digits = text[i + 1:i + 5]
            if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                _fail("Properties contain a malformed unicode escape")
            units.append(int(digits, 16))
            i += 5
            continue
        flush()
        out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(c, c))
        i += 1
    flush()
    return "".join(out), i


def parse_properties(data: bytes) -> dict[str, str]:
    """Parse Java .properties bytes with continuations, escapes and \\uXXXX.

    Raw non-ASCII bytes are rejected because scanner versions disagree on Latin-1 versus
    UTF-8 loading. Duplicate decoded keys, empty keys and control characters are rejected.
    """
    if any(b >= 0x80 for b in data):
        _fail("Properties must be ASCII with \\uXXXX escapes for other characters")
    result: dict[str, str] = {}
    for line in _logical_lines(data.decode("ascii")):
        key, index = _unescape(line, 0, True)
        while index < len(line) and line[index] in _SPACE:
            index += 1
        if index < len(line) and line[index] in "=:":
            index += 1
        while index < len(line) and line[index] in _SPACE:
            index += 1
        value, _ = _unescape(line, index, False)
        if not key:
            _fail("Properties contain an empty key")
        if _controls(key) or _controls(value):
            _fail(f"Property contains control characters: {key.encode('ascii', 'backslashreplace').decode()}")
        if key in result:
            _fail(f"Duplicate decoded property key: {key}")
        result[key] = value
    return result


def _escape(text: str, key: bool) -> str:
    out = []
    for index, c in enumerate(text):
        if ord(c) < 32 or ord(c) == 127:
            _fail("Property contains control characters")
        if c == "\\":
            out.append("\\\\")
        elif c in "=:#!" or (c == " " and (key or index == 0)):
            out.append("\\" + c)
        elif ord(c) > 126:
            encoded = c.encode("utf-16-be")
            out.extend("\\u%04x" % int.from_bytes(encoded[n:n + 2], "big") for n in range(0, len(encoded), 2))
        else:
            out.append(c)
    return "".join(out)


def serialize_properties(properties: dict) -> str:
    """Deterministic Java properties text (sorted keys, ASCII, round-trips parse_properties)."""
    lines = []
    for key in sorted(properties):
        value = properties[key]
        if not isinstance(key, str) or not key or not isinstance(value, str):
            _fail("Properties must map nonempty string keys to string values")
        lines.append(f"{_escape(key, True)}={_escape(value, False)}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------- globs and property registry

def glob_regex(pattern: str, what: str = "Pattern") -> "re.Pattern[str]":
    """Sonar path glob: ** spans directories, * and ? stay within one segment."""
    if (not pattern or _controls(pattern) or "\\" in pattern or pattern.startswith(("/", "file:"))
            or any(c in pattern for c in "[]{}")):
        _fail(f"{what} uses unsupported glob syntax: {pattern!r}")
    if pattern.endswith("/"):
        pattern += "**"
    segments = pattern.split("/")
    if any(s in {"", ".", ".."} for s in segments):
        _fail(f"{what} uses unsupported glob syntax: {pattern!r}")
    out = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        body = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
                       for c in re.sub(r"\*{2,}", "*", segment))
        out.append(body + ("" if last else "/"))
    return re.compile("".join(out) + r"\Z")


def _items(value: str, what: str) -> list[str]:
    if not value.strip():
        return []
    items = [item.strip() for item in value.split(",")]
    if any(not item or '"' in item or "'" in item for item in items):
        _fail(f"{what} has empty or quoted list entries")
    return items


def _classify(key: str, value: str, source: str) -> None:
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,200}", key):
        _fail(f"{source} has an invalid property key")
    if not isinstance(value, str) or _controls(value) or len(value) > 20_000:
        _fail(f"{source} property {key} has an invalid value")
    if "${" in value:
        _fail(f"{source} property {key} uses unsupported interpolation")
    if key in PROTECTED or key.startswith(PROTECTED_PREFIXES):
        _fail(f"{source} property {key} is owned by the trusted invocation")
    if key in GATE:
        if value != GATE[key]:
            _fail(f"{source} property {key} conflicts with the runner-owned value")
    elif key == "sonar.projectKey":
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,400}", value) or value.isdigit():
            _fail(f"{source} has an invalid sonar.projectKey")
    elif key == "sonar.sourceEncoding":
        try:
            codecs.lookup(value)
        except LookupError:
            _fail(f"{source} has an unknown sonar.sourceEncoding")
    elif key == "sonar.python.version":
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)*(,[0-9]+(\.[0-9]+)*)*", value):
            _fail(f"{source} has an invalid sonar.python.version")
    elif key in METADATA:
        if not value.strip() or len(value) > 1000:
            _fail(f"{source} property {key} must be nonempty")
    elif key in ROOT_LISTS:
        for item in _items(value, key):
            _parts(item, key) if item != "." else None
    elif key in PATTERN_LISTS:
        for item in _items(value, key):
            glob_regex(item, key)
    elif key in REPORTS:
        if not _items(value, key) and key == "sonar.apple.resultBundlePath":
            _fail(f"{source} property {key} must name one path")
    else:
        _fail(f"{source} property {key} is not in the supported registry")


# ---------------------------------------------------------------- recipe validation

def _argv(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(f"{what} must be a nonempty list")
    for item in value:
        if not isinstance(item, str) or not item or _controls(item):
            _fail(f"{what} must contain nonempty control-free strings")
    return list(value)


def _no_shell_command(argv: list[str]) -> None:
    for index, item in enumerate(argv):
        if os.path.basename(item) in SHELLS and any(
                a.startswith("-") and not a.startswith("--") and "c" in a[1:] for a in argv[index + 1:]):
            _fail("Shell command strings (-c) are not a recipe language. Use a checked-in script")


def _exact(value: object, allowed: set[str], required: set[str], what: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{what} must be an object")
    if set(value) - allowed:
        _fail(f"{what} has unsupported fields")
    if required - set(value):
        _fail(f"{what} is missing required fields")
    return value


def _artifacts(root: Path, value: object) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail("test.artifacts must be a nonempty list")
    result, seen = [], []
    for item in value:
        kind = item.get("kind") if isinstance(item, dict) else None
        item = _exact(item, {"kind", "path"} | ({"expectedTests"} if kind == "xcresult" else set()),
                      {"kind", "path"} | ({"expectedTests"} if kind == "xcresult" else set()), "Artifact")
        if kind not in KINDS:
            _fail("Unknown artifact kind")
        parts = _parts(item["path"], "Artifact path")
        if parts[0] in RESERVED:
            _fail("Artifacts cannot live in Git or runner state directories")
        for other in seen:
            if parts[:len(other)] == other or other[:len(parts)] == parts:
                _fail("Artifact paths are duplicated or overlap")
        seen.append(parts)
        entry = {"kind": kind, "path": "/".join(parts)}
        if kind == "xcresult":
            manifest = "/".join(_parts(item["expectedTests"], "expectedTests path"))
            data = _read(safe_path(root, manifest, True), MAX_MANIFEST_BYTES, "expectedTests")
            names = strict_json(data, "expectedTests")
            if (not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names)
                    or len(set(names)) != len(names)):
                _fail("expectedTests must be a nonempty JSON array of unique identifier strings")
            entry["expectedTests"] = manifest
        result.append(entry)
    return result


def _resolve_reports(properties: dict, artifacts: list[dict]) -> dict:
    """Every report reference must name declared artifacts of the registry kind."""
    names = {"xcresult": "native", "generic-coverage": "generic", "python-coverage": "python",
             "javascript-coverage": "javascript"}
    reports = {"native": [], "generic": [], "python": []}
    for key, (kind, many) in REPORTS.items():
        if key not in properties:
            continue
        references = _items(properties[key], key)
        if not many and (len(references) != 1 or "," in properties[key]):
            _fail(f"{key} must name exactly one declared path")
        consumed = set()
        for reference in references:
            glob = any(c in reference for c in "*?")
            if glob and kind == "javascript-coverage":
                _fail(f"{key} requires explicit report paths, not globs")
            if glob and not many:
                _fail(f"{key} cannot use a glob")
            if glob:
                matcher = glob_regex(reference, key)
                hits = [a for a in artifacts if matcher.match(a["path"])]
            else:
                hits = [a for a in artifacts if a["path"] == "/".join(_parts(reference, key))]
            if not hits:
                _fail(f"{key} references a path that is not a declared artifact: {reference}")
            if any(a["kind"] != kind for a in hits):
                _fail(f"{key} must reference only {kind} artifacts: {reference}")
            consumed.update(a["path"] for a in hits)
        reports[names[kind]] = sorted(consumed)
    for artifact in artifacts:
        group = names.get(artifact["kind"])
        # Unconsumed xcresult (validation-only evidence) and file artifacts are allowed.
        if group in {"generic", "python", "javascript"} and artifact["path"] not in reports.get(group, []):
            _fail(f"Declared {artifact['kind']} artifact is not imported by scanner properties: {artifact['path']}")
    return reports


# ---------------------------------------------------------------- scope inventory

def _git_files(root: Path) -> list[str]:
    def run(*args: str) -> bytes:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
        if result.returncode:
            _fail("Project mode requires an accessible Git repository")
        return result.stdout
    if Path(os.fsdecode(run("rev-parse", "--show-toplevel")).strip()).resolve() != root:
        _fail("Use the Git repository root, not a subdirectory")
    # Tracked files plus untracked non-ignored. Ignored generated or vendored files are skipped.
    raw = run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({os.fsdecode(item) for item in raw.split(b"\0") if item})


def _roots(root: Path, properties: dict, key: str) -> list[tuple[str, ...]]:
    result = []
    for item in _items(properties.get(key, ""), key):
        parts = () if item == "." else _parts(item, key)
        if parts and parts[0] in RESERVED:
            _fail(f"{key} cannot select Git or runner state directories")
        if parts:
            info = os.lstat(safe_path(root, "/".join(parts), True))
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                _fail(f"{key} entry must be a directory or regular file: {item}")
        result.append(parts)
    return result


def _under(parts: tuple[str, ...], roots: list[tuple[str, ...]]) -> bool:
    return any(parts[:len(base)] == base for base in roots)


def build_inventory(root: Path, properties: dict, artifacts: list[dict]) -> dict:
    """Classify supported source files using Sonar scope, inclusion and exclusion semantics.

    Source and test roots may overlap (for example Tools). A file in both is a test only if
    sonar.test.inclusions selects it. Coverage exclusions never shrink this inventory.
    """
    if "sonar.sources" not in properties:
        _fail("sonar.sources is required")
    sources, tests = _roots(root, properties, "sonar.sources"), _roots(root, properties, "sonar.tests")
    if not sources:
        _fail("sonar.sources selects nothing")
    def matchers(key: str):
        return [glob_regex(item, key) for item in _items(properties.get(key, ""), key)]
    inc, exc = matchers("sonar.inclusions"), matchers("sonar.exclusions")
    tinc, texc = matchers("sonar.test.inclusions"), matchers("sonar.test.exclusions")
    outputs = [tuple(a["path"].split("/")) for a in artifacts]
    production, test_files, counts = [], [], {}
    candidates = _git_files(root)
    if len(candidates) > MAX_FILES:
        _fail("Repository has too many files for inventory")
    for rel in candidates:
        suffix = os.path.splitext(rel)[1]
        parts = tuple(rel.split("/"))
        if suffix not in LANGUAGES or parts[0] in RESERVED or not (_under(parts, sources) or _under(parts, tests)):
            continue
        if any(parts[:len(o)] == o for o in outputs):
            continue
        try:
            info = os.lstat(os.path.join(root, rel))
        except OSError:
            continue  # Tracked file deleted in the working tree.
        if _controls(rel) or "\\" in rel:
            _fail("Selected source path contains control or backslash characters")
        if stat.S_ISLNK(info.st_mode):
            _fail(f"Selected source is a symbolic link: {rel}")
        if not stat.S_ISREG(info.st_mode):
            continue
        safe_path(root, rel, True)
        main = (_under(parts, sources) and (not inc or any(m.match(rel) for m in inc))
                and not any(m.match(rel) for m in exc)
                and not any(m.match(rel) for m in tinc))
        test = (_under(parts, tests) and (not tinc or any(m.match(rel) for m in tinc))
                and not any(m.match(rel) for m in texc))
        if main and test and _under(parts, sources) and _under(parts, tests):
            if not tinc:
                _fail(f"File is in both source and test roots without sonar.test.inclusions: {rel}")
            main = False  # Overlap roots are resolved by explicit test inclusions.
        language = counts.setdefault(LANGUAGES[suffix], {"sources": 0, "tests": 0})
        if test:
            test_files.append(rel)
            language["tests"] += 1
        elif main:
            production.append(rel)
            language["sources"] += 1
    if set(production) & set(test_files):
        _fail("Production and test classifications overlap")
    if not production:
        _fail("Scanner scope selects no supported production files")
    body = {"sources": sorted(production), "tests": sorted(test_files), "languages": counts}
    body["sha256"] = _sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return body


# ---------------------------------------------------------------- public loader

def load_project(root: Path, config: Path) -> dict:
    """Validate a schema-1 recipe and return the effective plan inputs.

    Returns the recipe with properties replaced by the merged effective dictionary, plus
    inventory, resolved report groups, override provenance and a digest binding the config,
    properties file, expected-test manifests, effective properties and inventory bytes.
    """
    base = _canonical_root(root)
    config_path = "/".join(_parts(config, "Config path"))
    config_bytes = _read(safe_path(base, config_path, True), MAX_CONFIG_BYTES, "Recipe")
    recipe = _exact(strict_json(config_bytes, "Recipe"),
                    {"schema", "properties", "scannerProperties", "test", "lint", "tools"},
                    {"schema", "properties", "test", "tools"}, "Recipe")
    if type(recipe["schema"]) is not int or recipe["schema"] != SCHEMA:
        _fail("Recipe schema must be integer 1")
    properties_name = "/".join(_parts(recipe["properties"], "properties path"))
    properties_bytes = _read(safe_path(base, properties_name, True), MAX_CONFIG_BYTES, "Scanner properties")
    effective = parse_properties(properties_bytes)
    for key, value in effective.items():
        _classify(key, value, "Baseline")
    overlay = recipe.get("scannerProperties", {})
    if not isinstance(overlay, dict):
        _fail("scannerProperties must be an object")
    overrides = []
    for key, value in overlay.items():
        _classify(key, value, "Overlay")
        overrides.append({"key": key, "baseline": effective.get(key), "overlay": value})
        effective[key] = value
    if "sonar.projectKey" not in effective:
        _fail("sonar.projectKey is required")
    test = _exact(recipe["test"], {"argv", "artifacts"}, {"argv", "artifacts"}, "test")
    test_argv = _argv(test["argv"], "test.argv")
    _no_shell_command(test_argv)
    artifacts = _artifacts(base, test["artifacts"])
    lint = _exact(recipe.get("lint", {"argv": DEFAULT_LINT, "import": "native"}), {"argv", "import"}, set(), "lint")
    lint_argv = _argv(lint.get("argv", DEFAULT_LINT), "lint.argv")
    if lint_argv != DEFAULT_LINT:
        _fail("lint.argv must be the full-repository strict command: " + " ".join(DEFAULT_LINT))
    if lint.get("import", "native") != "native":
        _fail("lint.import must be native")
    tools = _exact(recipe["tools"], {"swiftlint", "sonarScanner", "sonarApple"},
                   {"swiftlint", "sonarScanner", "sonarApple"}, "tools")
    for name, version in tools.items():
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+){0,3}", version):
            _fail(f"tools.{name} must be a dotted numeric version")
    reports = _resolve_reports(effective, artifacts)
    inventory = build_inventory(base, effective, artifacts)
    result = {"schema": SCHEMA, "config": config_path, "propertiesFile": properties_name,
              "properties": effective, "overrides": overrides,
              "test": {"argv": test_argv, "artifacts": artifacts},
              "lint": {"argv": lint_argv, "import": "native"}, "tools": dict(tools),
              "reports": reports, "inventory": inventory}
    manifests = {a["expectedTests"]: _sha(_read(safe_path(base, a["expectedTests"], True),
                                               MAX_MANIFEST_BYTES, "expectedTests"))
                 for a in artifacts if a["kind"] == "xcresult"}
    bound = {"config": _sha(config_bytes), "propertiesBytes": _sha(properties_bytes),
             "expectedTests": manifests, "recipe": result}
    result["digest"] = _sha(json.dumps(bound, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    return result
