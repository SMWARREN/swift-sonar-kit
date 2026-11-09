"""Path-only portable audit discovery and automatic project preparation.

Deterministically discovers project characteristics, reuses existing reviewed recipes,
and automatically prepares fresh SwiftPM and Xcode projects with strict lint, real tests,
and line coverage without modifying user source code or credentials.

SPDX-License-Identifier: GPL-3.0-only
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import uuid

from . import __version__
from .project_config import (
    DEFAULT_LINT,
    MAX_PATH_CHARS,
    SCHEMA,
    _canonical_root,
    _controls,
    _fail,
    _parts,
    _sha,
    build_inventory,
    load_project,
    safe_path,
    serialize_properties,
)
from .project_runner import child_environment
from .reports import lcov_report

RESERVED_DIRS = frozenset({".git", ".swift-sonar", ".scannerwork", ".build", "DerivedData"})
DEFAULT_TOOLS = {
    "swiftlint": "0.65.1",
    "sonarScanner": "8.3.0",
    "sonarApple": "0.5.1",
}


# ---------------------------------------------------------------- project naming

def deterministic_naming(
    root: Path,
    *,
    project_key: str | None = None,
    project_name: str | None = None,
) -> tuple[str, str]:
    """Derive deterministic project key and project name from repository inputs.

    Never uses HashFleet or other hardcoded organization names as defaults.
    Ensures projectKey satisfies Sonar regex [A-Za-z0-9_.:-]+ and is not all digits.
    """
    base = _canonical_root(root)
    name = project_name
    if not name:
        package_swift = base / "Package.swift"
        if package_swift.is_symlink():
            _fail("Package.swift cannot be a symbolic link")
        if package_swift.is_file():
            safe_path(base, "Package.swift", must_exist=True)
            try:
                content = package_swift.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'name:\s*"([^"\r\n\t]+)"', content)
                if match and match.group(1).strip():
                    name = match.group(1).strip()
            except OSError:
                pass
    if not name:
        for entry in sorted(base.iterdir()):
            if entry.suffix in {".xcodeproj", ".xcworkspace"} and entry.stem:
                if entry.is_symlink():
                    _fail(f"Xcode project cannot be a symbolic link: {entry.name}")
                safe_path(base, entry.name, must_exist=True)
                name = entry.stem
                break
    if not name:
        name = base.name or "project"
    if _controls(name) or len(name) > 1000:
        _fail("Derived or supplied project name is invalid")

    key = project_key
    if not key:
        slug = re.sub(r"[^A-Za-z0-9_.:-]+", "-", name).strip("-")
        if not slug or slug.isdigit():
            slug = f"project-{slug}" if slug else "project"
        key = slug[:400]
    else:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key) or key.isdigit():
            _fail("Supplied project key is invalid for SonarQube")

    return key, name


# ---------------------------------------------------------------- tool inspection

def _clean_tool_version(raw: str) -> str:
    match = re.search(r"\b([0-9]+(?:\.[0-9]+){0,3})", raw)
    if not match:
        _fail(f"Could not parse valid numeric version from {raw!r}")
    return match.group(1)


def inspect_tool_versions(
    root: Path,
    overrides: dict[str, str] | None = None,
    runner: Callable | None = None,
) -> dict[str, str]:
    """Inspect host tool versions with pinned fallbacks and safe version extraction."""
    result = dict(DEFAULT_TOOLS)
    if overrides:
        for k, v in overrides.items():
            if k in DEFAULT_TOOLS:
                result[k] = _clean_tool_version(str(v))

    def run_probe(cmd: list[str]) -> str | None:
        if runner:
            try:
                code, out, _ = runner(cmd, cwd=root)
                return out if code == 0 else None
            except Exception:
                return None
        executable = shutil.which(cmd[0])
        if not executable:
            return None
        try:
            p = subprocess.run(
                [executable, *cmd[1:]],
                cwd=root,
                env=child_environment(),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return (p.stdout + p.stderr).strip() if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    if not (overrides and "swiftlint" in overrides):
        out = run_probe(["swiftlint", "version"])
        if out:
            match = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", out)
            if match:
                result["swiftlint"] = match.group(1)

    if not (overrides and "sonarScanner" in overrides):
        out = run_probe(["sonar-scanner", "--version"])
        if out:
            match = re.search(r"SonarScanner CLI ([0-9]+(?:\.[0-9]+){0,3})", out)
            if match:
                result["sonarScanner"] = match.group(1)

    return result


# ---------------------------------------------------------------- discovery

def detect_project_kind(root: Path, *, config: Path | str | None = None) -> tuple[str, Path | None]:
    """Determine project kind: 'existing', 'swiftpm', 'xcode', or review-needed."""
    base = _canonical_root(root)
    if config:
        config_path = safe_path(base, config, must_exist=True)
        return "existing", config_path.relative_to(base)

    # Check candidate recipes. If present, validate strictly with safe_path (fails on symlink).
    for candidate in ("swift-sonar.json", "Tools/swift-sonar-project.json"):
        cand_path = base / candidate
        if cand_path.exists() or cand_path.is_symlink():
            rel = safe_path(base, candidate, must_exist=True).relative_to(base)
            return "existing", rel

    # If repository already has sonar-project.properties without a recipe, do NOT overwrite or narrow it.
    prop_path = base / "sonar-project.properties"
    if prop_path.exists() or prop_path.is_symlink():
        safe_path(base, "sonar-project.properties", must_exist=True)
        return "review_needed_unbound_properties", None

    pkg_path = base / "Package.swift"
    if pkg_path.is_symlink():
        _fail("Package.swift cannot be a symbolic link")
    if pkg_path.is_file():
        safe_path(base, "Package.swift", must_exist=True)
        return "swiftpm", None

    workspaces = []
    projects = []
    for e in sorted(base.iterdir()):
        if e.suffix in {".xcworkspace", ".xcodeproj"} and not e.name.startswith("."):
            if e.is_symlink():
                _fail(f"Xcode project cannot be a symbolic link: {e.name}")
            safe_path(base, e.name, must_exist=True)
            if e.suffix == ".xcworkspace":
                workspaces.append(e)
            else:
                projects.append(e)
    if len(workspaces) > 1:
        return "review_needed_ambiguous_xcode_workspaces", None
    if len(projects) > 1 and not workspaces:
        return "review_needed_ambiguous_xcode_projects", None
    if workspaces or projects:
        return "xcode", None

    return "unsupported", None


def detect_swiftpm_sources(root: Path) -> tuple[list[str], list[str]]:
    """Detect production source and test directories for a SwiftPM package."""
    base = _canonical_root(root)
    sources: list[str] = []
    tests: list[str] = []

    dir_entries = {e.name: e for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")}
    for name, entry in sorted(dir_entries.items()):
        if entry.is_symlink():
            _fail(f"Source or test directory cannot be a symbolic link: {name}")
        safe_path(base, name, must_exist=True)
        if name in {"build", "Tools", "DerivedData"}:
            continue
        if any(entry.rglob("*.swift")):
            if "test" in name.lower():
                tests.append(name)
            else:
                sources.append(name)

    if not sources:
        _fail("No Swift production sources detected in project")

    return sorted(set(sources)), sorted(set(tests))


def detect_xcode_setup(root: Path, *, runner: Callable | None = None) -> dict:
    """Detect Xcode project/workspace, scheme, runnable destination, and explicit source directories."""
    base = _canonical_root(root)
    workspaces = [e for e in sorted(base.iterdir()) if e.suffix == ".xcworkspace" and not e.name.startswith(".")]
    projects = [e for e in sorted(base.iterdir()) if e.suffix == ".xcodeproj" and not e.name.startswith(".")]

    if len(workspaces) > 1:
        _fail("Multiple Xcode workspaces found. Specify target workspace or scheme explicitly.")
    if len(projects) > 1 and not workspaces:
        _fail("Multiple Xcode projects found. Specify target project explicitly.")

    target_file = None
    file_arg: list[str] = []
    if workspaces:
        target_file = workspaces[0]
        file_arg = ["-workspace", target_file.name]
    elif projects:
        target_file = projects[0]
        file_arg = ["-project", target_file.name]
    else:
        _fail("No Xcode project or workspace found")

    cmd = ["xcodebuild", "-list", "-json", *file_arg]
    if runner:
        code, out, err = runner(cmd, cwd=base)
    else:
        p = subprocess.run(
            cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        code, out, err = p.returncode, p.stdout, p.stderr

    if code != 0 or not out.strip():
        _fail(f"xcodebuild -list failed. Could not enumerate schemes: {err.strip() or out.strip()}")

    schemes: list[str] = []
    try:
        info = json.loads(out)
        project_info = info.get("project") or info.get("workspace") or {}
        schemes = project_info.get("schemes", [])
    except Exception as e:
        _fail(f"Could not parse xcodebuild -list schemes: {e}")

    if not schemes:
        _fail("xcodebuild -list found no schemes")

    selected_scheme: str | None = None
    if len(schemes) == 1:
        selected_scheme = schemes[0]
    else:
        for s in schemes:
            if s.lower() == target_file.stem.lower():
                selected_scheme = s
                break
        if not selected_scheme:
            non_tests = [s for s in schemes if not s.lower().endswith("tests")]
            if len(non_tests) == 1:
                selected_scheme = non_tests[0]
            else:
                _fail(f"Ambiguous Xcode schemes found: {', '.join(schemes)}. Specify target scheme explicitly.")

    # Query xcodebuild -showdestinations for real runnable destination
    dest_cmd = ["xcodebuild", "-scheme", selected_scheme, "-showdestinations", *file_arg]
    dest_code, dest_out, dest_err = 0, "", ""
    if runner:
        dest_code, dest_out, dest_err = runner(dest_cmd, cwd=base)
    else:
        dp = subprocess.run(
            dest_cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        dest_code, dest_out, dest_err = dp.returncode, dp.stdout, dp.stderr

    runnable_destination = None
    if dest_code == 0 and dest_out.strip():
        # Check for macOS destination
        if "platform:macOS" in dest_out or "platform:OS X" in dest_out:
            runnable_destination = "platform=macOS"
        else:
            match = re.search(r"\{\s*platform:iOS Simulator,\s*id:([A-Fa-f0-9\-]+)", dest_out)
            if match:
                runnable_destination = f"platform=iOS Simulator,id={match.group(1)}"
            else:
                match_name = re.search(r"\{\s*platform:iOS Simulator,\s*(?:OS:[^,]+,\s*)?name:([^}]+)\}", dest_out)
                if match_name:
                    runnable_destination = f"platform=iOS Simulator,name={match_name.group(1).strip()}"

    if not runnable_destination:
        runnable_destination = "platform=macOS"

    # Detect conventional source and test directories without silently dropping secondary targets
    sources: list[str] = []
    tests: list[str] = []
    dir_entries = {e.name: e for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")}
    for name, entry in sorted(dir_entries.items()):
        if entry.is_symlink():
            _fail(f"Source or test directory cannot be a symbolic link: {name}")
        safe_path(base, name, must_exist=True)
        if any(entry.rglob("*.swift")):
            if name in {"Sources", "Source", "App", "Shared", "Core", selected_scheme, target_file.stem}:
                sources.append(name)
            elif name in {"Tests", f"{selected_scheme}Tests", f"{target_file.stem}Tests", "UnitTests", "UITests"}:
                tests.append(name)

    if not sources:
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "build", "Tools", "DerivedData")):
                if any(entry.rglob("*.swift")):
                    if "test" in entry.name.lower():
                        tests.append(entry.name)
                    else:
                        sources.append(entry.name)

    if not sources:
        _fail("Could not detect conventional Swift production directory for Xcode project")

    return {
        "target_file": target_file,
        "scheme": selected_scheme,
        "destination": runnable_destination,
        "is_workspace": bool(workspaces),
        "sources": sorted(set(sources)),
        "tests": sorted(set(tests)),
    }


def discover(
    root: Path | str,
    *,
    config: Path | str | None = None,
    project_key: str | None = None,
    project_name: str | None = None,
    tool_versions: dict[str, str] | None = None,
) -> dict:
    """Discover project characteristics from path only.

    Returns a discovery plan dictionary with deterministic project identifiers,
    source/test scope, toolchain, and preparation readiness.
    """
    base = _canonical_root(Path(root))
    p_key, p_name = deterministic_naming(base, project_key=project_key, project_name=project_name)
    kind, existing_config = detect_project_kind(base, config=config)
    tools = inspect_tool_versions(base, tool_versions)

    if kind == "existing":
        assert existing_config is not None
        loaded = load_project(base, existing_config)
        prop_sources = [s.strip() for s in loaded["properties"].get("sonar.sources", "").split(",") if s.strip()]
        prop_tests = [t.strip() for t in loaded["properties"].get("sonar.tests", "").split(",") if t.strip()]
        return {
            "status": "ready",
            "kind": "existing",
            "root": base,
            "project_key": loaded["properties"].get("sonar.projectKey", p_key),
            "project_name": loaded["properties"].get("sonar.projectName", p_name),
            "config_path": existing_config,
            "properties_path": Path(loaded["propertiesFile"]),
            "sources": prop_sources,
            "tests": prop_tests,
            "recipe": loaded,
            "reason": None,
            "toolchain": tools,
        }

    if kind == "review_needed_unbound_properties":
        return {
            "status": "review_needed",
            "kind": "review_needed_unbound_properties",
            "root": base,
            "project_key": p_key,
            "project_name": p_name,
            "config_path": None,
            "properties_path": Path("sonar-project.properties"),
            "sources": [],
            "tests": [],
            "recipe": None,
            "reason": (
                "Repository contains reviewed sonar-project.properties but no swift-sonar.json recipe. "
                "Automatic mode will not silently replace or narrow your reviewed properties baseline. "
                "See docs/PROJECT-MODE.md to configure swift-sonar.json."
            ),
            "toolchain": tools,
        }

    if kind.startswith("review_needed_ambiguous_xcode"):
        return {
            "status": "review_needed",
            "kind": kind,
            "root": base,
            "project_key": p_key,
            "project_name": p_name,
            "config_path": None,
            "properties_path": None,
            "sources": [],
            "tests": [],
            "recipe": None,
            "reason": "Multiple Xcode projects or workspaces found. Specify target project explicitly.",
            "toolchain": tools,
        }

    if kind == "swiftpm":
        sources, tests = detect_swiftpm_sources(base)
        config_path = Path("swift-sonar.json")
        prop_path = Path("sonar-project.properties")

        properties = {
            "sonar.projectKey": p_key,
            "sonar.projectName": p_name,
            "sonar.projectVersion": "1.0",
            "sonar.sources": ",".join(sources),
            "sonar.exclusions": "build/**,.swift-sonar/**,.build/**",
            "sonar.coverageReportPaths": "build/sonar/coverage.xml",
        }
        if tests:
            properties["sonar.tests"] = ",".join(tests)

        recipe = {
            "schema": SCHEMA,
            "properties": str(prop_path),
            "scannerProperties": {},
            "test": {
                "argv": [sys.executable, "-m", "swift_sonar_kit.automatic", "execute", "--root", "."],
                "artifacts": [
                    {"kind": "generic-coverage", "path": "build/sonar/coverage.xml"}
                ],
            },
            "lint": {
                "argv": list(DEFAULT_LINT),
                "import": "native",
            },
            "tools": tools,
        }
        return {
            "status": "ready",
            "kind": "swiftpm",
            "root": base,
            "project_key": p_key,
            "project_name": p_name,
            "config_path": config_path,
            "properties_path": prop_path,
            "sources": sources,
            "tests": tests,
            "recipe": recipe,
            "reason": None,
            "toolchain": tools,
        }

    if kind == "xcode":
        config_path = Path("swift-sonar.json")
        prop_path = Path("sonar-project.properties")
        return {
            "status": "ready",
            "kind": "xcode",
            "root": base,
            "project_key": p_key,
            "project_name": p_name,
            "config_path": config_path,
            "properties_path": prop_path,
            "sources": [],
            "tests": [],
            "recipe": None,
            "reason": None,
            "toolchain": tools,
        }

    return {
        "status": "unsupported",
        "kind": "unsupported",
        "root": base,
        "project_key": p_key,
        "project_name": p_name,
        "config_path": None,
        "properties_path": None,
        "sources": [],
        "tests": [],
        "recipe": None,
        "reason": "No supported project manifest found (Package.swift, swift-sonar.json, or Xcode project).",
        "toolchain": tools,
    }


# ---------------------------------------------------------------- exclusive safe write helper

def _safe_write(path: Path, content: str, *, allow_overwrite: bool = False) -> None:
    """Exclusive, atomic write with nofollow symlink rejection and race-free publish."""
    if path.is_symlink():
        _fail("Refusing to write through a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = f".tmp-{uuid.uuid4().hex}"
    temp_path = path.parent / temp_name
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if allow_overwrite:
            os.replace(temp_path, path)
        else:
            try:
                os.link(temp_path, path, follow_symlinks=False)
            except FileExistsError:
                _fail(f"Refusing to overwrite existing file: {path.name}")
            temp_path.unlink()
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _ensure_git_ignores(base: Path) -> None:
    """Ensure build outputs are ignored in Git so runtime reports do not invalidate source snapshot."""
    gi_path = base / ".gitignore"
    if gi_path.is_symlink():
        _fail(".gitignore cannot be a symbolic link")
    if gi_path.is_file():
        gi_text = gi_path.read_text(encoding="utf-8", errors="replace")
        missing = [entry for entry in ("build/", ".build/", ".swift-sonar/", ".scannerwork/") if entry not in gi_text]
        if missing:
            new_gi = gi_text + ("\n" if not gi_text.endswith("\n") else "") + "\n".join(missing) + "\n"
            _safe_write(safe_path(base, ".gitignore"), new_gi, allow_overwrite=True)
    elif not gi_path.exists():
        _safe_write(safe_path(base, ".gitignore"), "build/\n.build/\n.swift-sonar/\n.scannerwork/\n")


# ---------------------------------------------------------------- Xcode native test enumeration

def enumerate_xcode_tests(
    root: Path,
    scheme: str,
    manifest_out: Path,
    *,
    project_path: Path | None = None,
    workspace_path: Path | None = None,
    destination: str = "platform=macOS",
    runner: Callable | None = None,
) -> list[str]:
    """Enumerate tests independently via xcodebuild before running tests.

    Produces expected-native-tests.json JSON array of unique test identifier strings.
    Never derives expected inventory from completed results.
    """
    base = _canonical_root(root)
    file_arg = []
    if workspace_path:
        file_arg = ["-workspace", str(workspace_path.relative_to(base) if workspace_path.is_relative_to(base) else workspace_path)]
    elif project_path:
        file_arg = ["-project", str(project_path.relative_to(base) if project_path.is_relative_to(base) else project_path)]

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_enum_path = tf.name

    try:
        cmd = [
            "xcodebuild", "test",
            *file_arg,
            "-scheme", scheme,
            "-destination", destination,
            "-enumerate-tests",
            "-test-enumeration-style", "flat",
            "-test-enumeration-format", "json",
            "-test-enumeration-output-path", temp_enum_path,
        ]
        if runner:
            exit_code, stdout, stderr = runner(cmd, cwd=base)
        else:
            p = subprocess.run(
                cmd,
                cwd=base,
                env=child_environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            exit_code, stdout, stderr = p.returncode, p.stdout, p.stderr

        if exit_code != 0:
            _fail(f"xcodebuild -enumerate-tests failed (exit {exit_code}): {stderr.strip() or stdout.strip()}")

        enum_bytes = Path(temp_enum_path).read_bytes()
        if not enum_bytes.strip() and stdout.strip():
            enum_bytes = stdout.strip().encode("utf-8")
        if not enum_bytes.strip():
            _fail("xcodebuild test enumeration produced empty output file")

        try:
            data = json.loads(enum_bytes.decode("utf-8"))
        except Exception as e:
            _fail(f"xcodebuild test enumeration produced invalid JSON: {e}")
    finally:
        if os.path.exists(temp_enum_path):
            os.unlink(temp_enum_path)

    identities: list[str] = []

    # Actual xcodebuild schema: {"values": [{"enabledTests": [{"identifier": "Suite/testMethod()"}]}]}
    if not isinstance(data, dict) or "values" not in data or not isinstance(data["values"], list):
        _fail("Unsupported xcodebuild test enumeration JSON shape: missing values list")

    for v in data["values"]:
        if not isinstance(v, dict):
            _fail("Unsupported xcodebuild test enumeration JSON shape: malformed plan item")
        enabled = v.get("enabledTests")
        if not isinstance(enabled, list):
            _fail("Unsupported xcodebuild test enumeration JSON shape: missing enabledTests list")
        for item in enabled:
            if not isinstance(item, dict):
                _fail("Unsupported xcodebuild test enumeration JSON shape: malformed test item")
            ident = item.get("identifier")
            if not isinstance(ident, str) or not ident:
                _fail("Unsupported xcodebuild test enumeration JSON shape: missing test identifier")
            if "()" in ident or "/" in ident:
                identities.append(ident)

    identities = sorted(set(identities))
    if not identities:
        _fail("No test cases enumerated by xcodebuild -enumerate-tests")

    manifest_file = safe_path(base, manifest_out)
    _safe_write(manifest_file, json.dumps(identities, indent=2) + "\n", allow_overwrite=True)
    return identities


# ---------------------------------------------------------------- prepare

def prepare(
    root: Path | str,
    *,
    plan: dict | None = None,
    config: Path | str | None = None,
    project_key: str | None = None,
    project_name: str | None = None,
    tool_versions: dict[str, str] | None = None,
    runner: Callable | None = None,
) -> dict:
    """Automatically prepare local configuration, properties, and pre-test manifests.

    Does NOT run final tests/artifacts itself. Root calls project_runner.project_test(root, config)
    to capture source snapshot and execute test command.
    """
    base = _canonical_root(Path(root))
    if plan is None:
        plan = discover(
            base,
            config=config,
            project_key=project_key,
            project_name=project_name,
            tool_versions=tool_versions,
        )

    if plan["status"] in {"unsupported", "review_needed"}:
        raise ValueError(plan.get("reason") or "Project discovery failed")

    if plan["kind"] == "existing":
        assert plan["config_path"] is not None
        return {
            "status": "ready",
            "config_path": plan["config_path"],
            "properties_path": plan["properties_path"],
            "plan": plan,
            "artifacts": plan["recipe"]["test"]["artifacts"],
            "metadata": {"reused_existing": True},
            "reason": None,
        }

    if plan["kind"] == "swiftpm":
        sources = plan["sources"]
        cov_rel = Path("build/sonar/coverage.xml")
        prop_path = plan["properties_path"]
        config_path = plan["config_path"]

        # Ensure .swiftlint.yml exists to exclude build and runner state if user has none
        swiftlint_cfg = base / ".swiftlint.yml"
        if swiftlint_cfg.is_symlink():
            _fail(".swiftlint.yml cannot be a symbolic link")
        if not swiftlint_cfg.exists():
            _safe_write(
                safe_path(base, ".swiftlint.yml"),
                "excluded:\n  - .build\n  - build\n  - .swift-sonar\n",
            )

        _ensure_git_ignores(base)

        prop_file = safe_path(base, prop_path)
        properties = {
            "sonar.projectKey": plan["project_key"],
            "sonar.projectName": plan["project_name"],
            "sonar.projectVersion": "1.0",
            "sonar.sources": ",".join(sources),
            "sonar.exclusions": "build/**,.swift-sonar/**,.build/**",
            "sonar.coverageReportPaths": str(cov_rel),
        }
        if plan["tests"]:
            properties["sonar.tests"] = ",".join(plan["tests"])
        _safe_write(prop_file, serialize_properties(properties))

        config_file = safe_path(base, config_path)
        recipe = {
            "schema": SCHEMA,
            "properties": str(prop_path),
            "scannerProperties": {},
            "test": {
                "argv": [sys.executable, "-m", "swift_sonar_kit.automatic", "execute", "--root", "."],
                "artifacts": [
                    {"kind": "generic-coverage", "path": str(cov_rel)}
                ],
            },
            "lint": {
                "argv": list(DEFAULT_LINT),
                "import": "native",
            },
            "tools": plan["toolchain"],
        }
        _safe_write(config_file, json.dumps(recipe, indent=2) + "\n")

        loaded = load_project(base, config_path)
        artifacts = loaded["test"]["artifacts"]

        return {
            "status": "ready",
            "config_path": config_path,
            "properties_path": prop_path,
            "plan": plan,
            "artifacts": artifacts,
            "metadata": {"kind": "swiftpm"},
            "reason": None,
        }

    if plan["kind"] == "xcode":
        prop_path = plan["properties_path"]
        config_path = plan["config_path"]
        # Expected native test manifest placed outside build/ so workflow archive_previous_outputs does not touch it
        manifest_path = Path("Tools/expected-native-tests.json")
        xcresult_path = Path("build/audit/native.xcresult")

        try:
            xcode_setup = detect_xcode_setup(base, runner=runner)
        except Exception as e:
            raise ValueError(f"Xcode scheme selection review needed: {e}")

        scheme = xcode_setup["scheme"]
        destination = xcode_setup["destination"]
        target_file = xcode_setup["target_file"]
        is_workspace = xcode_setup["is_workspace"]
        sources = xcode_setup["sources"]
        tests = xcode_setup["tests"]

        # Ensure .swiftlint.yml exists to exclude build and runner state if user has none
        swiftlint_cfg = base / ".swiftlint.yml"
        if not swiftlint_cfg.exists() and not swiftlint_cfg.is_symlink():
            _safe_write(
                safe_path(base, ".swiftlint.yml"),
                "excluded:\n  - build\n  - .swift-sonar\n  - DerivedData\n",
            )

        _ensure_git_ignores(base)

        # Independent test enumeration before tests
        try:
            identities = enumerate_xcode_tests(
                base,
                scheme,
                manifest_path,
                workspace_path=target_file if is_workspace else None,
                project_path=None if is_workspace else target_file,
                destination=destination,
                runner=runner,
            )
        except Exception as e:
            raise ValueError(f"Xcode test enumeration could not complete automatically: {e}")

        # Write properties
        prop_file = safe_path(base, prop_path)
        properties = {
            "sonar.projectKey": plan["project_key"],
            "sonar.projectName": plan["project_name"],
            "sonar.projectVersion": "1.0",
            "sonar.sources": ",".join(sources),
            "sonar.exclusions": "build/**,.swift-sonar/**,DerivedData/**",
            "sonar.apple.resultBundlePath": str(xcresult_path),
        }
        if tests:
            properties["sonar.tests"] = ",".join(tests)
        _safe_write(prop_file, serialize_properties(properties))

        test_argv = [
            "xcodebuild", "test",
            "-scheme", scheme,
            "-destination", destination,
            "-enableCodeCoverage", "YES",
            "-resultBundlePath", str(xcresult_path),
        ]
        if is_workspace:
            test_argv[2:2] = ["-workspace", target_file.name]
        else:
            test_argv[2:2] = ["-project", target_file.name]

        recipe = {
            "schema": SCHEMA,
            "properties": str(prop_path),
            "scannerProperties": {},
            "test": {
                "argv": test_argv,
                "artifacts": [
                    {
                        "kind": "xcresult",
                        "path": str(xcresult_path),
                        "expectedTests": str(manifest_path),
                    }
                ],
            },
            "lint": {
                "argv": list(DEFAULT_LINT),
                "import": "native",
            },
            "tools": plan["toolchain"],
        }
        config_file = safe_path(base, config_path)
        _safe_write(config_file, json.dumps(recipe, indent=2) + "\n")

        loaded = load_project(base, config_path)
        artifacts = loaded["test"]["artifacts"]

        return {
            "status": "ready",
            "config_path": config_path,
            "properties_path": prop_path,
            "plan": plan,
            "artifacts": artifacts,
            "metadata": {"kind": "xcode", "enumerated_tests": len(identities)},
            "reason": None,
        }

    raise ValueError(f"Unsupported project kind: {plan['kind']}")


# ---------------------------------------------------------------- recipe test execution helper

def parse_lcov_text_for_production(lcov_text: str, root: Path, sources: list[str]) -> str:
    """Filter LCOV tracefile to production sources only and normalize source paths."""
    base = _canonical_root(root)
    source_bases = [safe_path(base, s, must_exist=True) for s in sources]
    filtered_lines: list[str] = []
    current_sf: str | None = None
    is_production = False

    for line in lcov_text.splitlines():
        if line.startswith("SF:"):
            raw_path = line[3:].strip()
            resolved = Path(os.path.realpath(raw_path if os.path.isabs(raw_path) else base / raw_path))
            current_sf = None
            is_production = False
            for s_base in source_bases:
                if (resolved == s_base or resolved.is_relative_to(s_base)) and resolved.is_file():
                    rel = resolved.relative_to(base).as_posix()
                    current_sf = f"SF:{rel}"
                    is_production = True
                    break
            if is_production and current_sf:
                filtered_lines.append(current_sf)
        elif is_production:
            filtered_lines.append(line)

    if not any(l.startswith("SF:") for l in filtered_lines):
        _fail("No production line coverage records found in LCOV data")

    return "\n".join(filtered_lines) + "\n"


def execute_swiftpm_tests_and_coverage(
    root: Path,
    sources: list[str],
    coverage_out: Path,
    *,
    runner: Callable | None = None,
) -> int:
    """Called during project_test to run swift test and generate coverage.xml artifact.

    Binds SwiftPM testing to dedicated scratch directory build/sonar/swiftpm, queries exact
    fresh profile and test binary paths, exports real LCOV via llvm-cov, and converts to
    Sonar generic XML. Strictly fails nonzero on any missing profile, missing binary,
    or failed export without fabricating synthetic coverage.
    """
    base = _canonical_root(root)
    cov_path = safe_path(base, coverage_out)
    scratch_rel = "build/sonar/swiftpm"
    scratch_dir = base / scratch_rel

    test_start_time = time.time()
    cmd = ["swift", "test", "--scratch-path", scratch_rel, "--enable-code-coverage"]
    if runner:
        exit_code, stdout, stderr = runner(cmd, cwd=base)
    else:
        p = subprocess.run(
            cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        exit_code, stdout, stderr = p.returncode, p.stdout, p.stderr

    if exit_code != 0:
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"exit_code": exit_code, "stdout": stdout, "stderr": stderr}, indent=2),
            allow_overwrite=True,
        )
        return exit_code

    # Locate fresh profdata strictly via swift test --show-codecov-path
    codecov_cmd = ["swift", "test", "--scratch-path", scratch_rel, "--show-codecov-path"]
    if runner:
        c_code, c_out, c_err = runner(codecov_cmd, cwd=base)
    else:
        cp = subprocess.run(
            codecov_cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        c_code, c_out, c_err = cp.returncode, cp.stdout, cp.stderr

    if c_code != 0 or not c_out.strip():
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Failed to query code coverage path from swift test --show-codecov-path", "stderr": c_err, "exit_code": c_code}, indent=2),
            allow_overwrite=True,
        )
        return 1

    codecov_path = Path(c_out.strip()).resolve()
    resolved_scratch = scratch_dir.resolve()
    if not (codecov_path == resolved_scratch or codecov_path.is_relative_to(resolved_scratch)):
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Code coverage path is outside dedicated scratch directory"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    profdata = codecov_path.parent / "default.profdata"
    if not profdata.is_file():
        candidates = [p for p in scratch_dir.glob("**/default.profdata")
                      if p.is_file() and not p.is_symlink() and (p == resolved_scratch or p.is_relative_to(resolved_scratch))]
        if candidates:
            profdata = candidates[0]

    if not profdata or profdata.is_symlink() or not profdata.is_file():
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Missing fresh default.profdata in dedicated scratch directory"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    # Verify profile freshness: must not be a stale file from prior to test run
    if profdata.stat().st_mtime < (test_start_time - 2.0):
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Stale code coverage profile detected from prior execution"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    # Locate fresh test binary strictly via swift build --show-bin-path
    bin_cmd = ["swift", "build", "--scratch-path", scratch_rel, "--show-bin-path"]
    if runner:
        b_code, b_out, b_err = runner(bin_cmd, cwd=base)
    else:
        bp = subprocess.run(
            bin_cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        b_code, b_out, b_err = bp.returncode, bp.stdout, bp.stderr

    if b_code != 0 or not b_out.strip():
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Failed to query build binary path from swift build --show-bin-path", "stderr": b_err, "exit_code": b_code}, indent=2),
            allow_overwrite=True,
        )
        return 1

    bin_dir = Path(b_out.strip()).resolve()
    if bin_dir.is_symlink() or not bin_dir.is_dir() or not (bin_dir == resolved_scratch or bin_dir.is_relative_to(resolved_scratch)):
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Build binary path is outside dedicated scratch directory or invalid"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    test_binaries: list[Path] = []
    for xctest in sorted(bin_dir.glob("*.xctest")):
        if xctest.is_symlink():
            continue
        macos_dir = xctest / "Contents" / "MacOS"
        if macos_dir.is_dir():
            for f in sorted(macos_dir.iterdir()):
                if f.is_file() and not f.is_symlink() and os.access(f, os.X_OK):
                    test_binaries.append(f)
        elif xctest.is_file() and os.access(xctest, os.X_OK):
            test_binaries.append(xctest)

    if not test_binaries:
        for f in sorted(bin_dir.glob("*PackageTests")):
            if f.is_file() and not f.is_symlink() and os.access(f, os.X_OK):
                test_binaries.append(f)

    if not test_binaries:
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "Missing fresh test executable in dedicated scratch directory"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    source_paths = [str(base / s) for s in sources]
    export_cmd = [
        "xcrun", "llvm-cov", "export",
        "--format=lcov",
        f"-instr-profile={profdata}",
        str(test_binaries[0]),
    ]
    for extra_binary in test_binaries[1:]:
        export_cmd.extend(["-object", str(extra_binary)])
    export_cmd.extend(["--sources", *source_paths])
    if runner:
        e_code, e_out, e_err = runner(export_cmd, cwd=base)
    else:
        ep = subprocess.run(
            export_cmd,
            cwd=base,
            env=child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        e_code, e_out, e_err = ep.returncode, ep.stdout, ep.stderr

    if e_code != 0 or not e_out.strip():
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": "llvm-cov export failed or produced empty output", "stderr": e_err, "exit_code": e_code}, indent=2),
            allow_overwrite=True,
        )
        return 1

    lcov_text = e_out
    try:
        filtered_lcov = parse_lcov_text_for_production(lcov_text, base, sources)
        sonar_coverage_xml = lcov_report([filtered_lcov], base)
    except Exception as e:
        _safe_write(
            safe_path(base, ".swift-sonar/test-failure.json"),
            json.dumps({"error": f"Coverage report conversion failed: {e}"}, indent=2),
            allow_overwrite=True,
        )
        return 1

    _safe_write(cov_path, sonar_coverage_xml, allow_overwrite=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Internal recipe runner invoked by project_runner during project_test."""
    pkg_root = Path(__file__).resolve().parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("Usage: python3 -m swift_sonar_kit.automatic execute --root <dir>")
        return 1
    action = args[0]
    if action == "execute":
        root = Path.cwd()
        if "--root" in args:
            idx = args.index("--root")
            if idx + 1 < len(args):
                root = Path(args[idx + 1])
        sources, _ = detect_swiftpm_sources(root)
        cov_path = Path("build/sonar/coverage.xml")
        return execute_swiftpm_tests_and_coverage(root, sources, cov_path)
    print(f"Unknown action: {action}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
