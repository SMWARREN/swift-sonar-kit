"""Source-only SwiftLint plus Sonar analysis discovery.

Everything here is read-only. It never runs a build, a test, a coverage producer, a simulator,
a version probe or SwiftPM/Xcode enumeration, never touches the network and never writes
configuration. The only subprocess is the Git file listing used by the scope inventory.
Recipe test commands and artifact declarations are deliberately neither executed, read nor
validated, and every scanner report property is dropped from the effective analysis scope so
no stale coverage or native test report can be imported.

SPDX-License-Identifier: GPL-3.0-only
"""

import json
import os
from pathlib import Path
import re

from .automatic import deterministic_naming
from .bootstrap import DEFAULT_TOOLS
from .project_config import (
    DEFAULT_LINT,
    LANGUAGES,
    MAX_CONFIG_BYTES,
    REPORTS,
    RESERVED,
    SCHEMA,
    _canonical_root,
    _classify,
    _exact,
    _fail,
    _git_files,
    _parts,
    _read,
    _sha,
    build_inventory,
    parse_properties,
    safe_path,
    strict_json,
)

KIND = "analysis-only"
RECIPE_CANDIDATES = ("swift-sonar.json", "Tools/swift-sonar-project.json")
PROPERTIES_FILE = "sonar-project.properties"
SOURCE_DIRS = frozenset({"App", "Sources", "Core", "Shared", "Tools", "Scripts"})
TEST_DIRS = frozenset({"Tests", "AppTests", "MacAppTests", "UnitTests", "UITests"})
GENERATED_DIRS = frozenset({"build", "DerivedData", ".build"})
MAX_FILE_BYTES = 64 * 1024 * 1024
FRESH_EXCLUSIONS = "build/**,.swift-sonar/**,.build/**,DerivedData/**"


def _exists(base: Path, relative: str) -> bool:
    path = base / relative
    return path.exists() or path.is_symlink()


def _relative_config(base: Path, root: Path, config: Path | str) -> str:
    value = os.fspath(config)
    if value.startswith("/"):
        candidate = Path(value)
        for anchor in (base, Path(os.path.abspath(root))):
            try:
                value = candidate.relative_to(anchor).as_posix()
                break
            except ValueError:
                continue
        else:
            _fail("Config path must be inside the project root")
    return "/".join(_parts(value, "Config path"))


def _select_config(base: Path, root: Path, config: Path | str | None) -> str | None:
    if config is not None:
        relative = _relative_config(base, root, config)
        safe_path(base, relative, True)
        return relative
    for candidate in RECIPE_CANDIDATES:
        if _exists(base, candidate):
            return "/".join(_parts(candidate))
    if _exists(base, PROPERTIES_FILE):
        return PROPERTIES_FILE
    return None


def _tools(value: object) -> dict:
    value = _exact(value, set(DEFAULT_TOOLS), set(DEFAULT_TOOLS), "tools")
    for name, version in value.items():
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+){0,3}", version):
            _fail(f"tools.{name} must be a dotted numeric version")
    return {name: value[name] for name in DEFAULT_TOOLS}


def _lint(value: object) -> None:
    lint = _exact(value, {"argv", "import"}, set(), "lint")
    if lint.get("argv", DEFAULT_LINT) != DEFAULT_LINT:
        _fail("lint.argv must be the full-repository strict command: " + " ".join(DEFAULT_LINT))
    if lint.get("import", "native") != "native":
        _fail("lint.import must be native")


def _read_properties(base: Path, relative: str, what: str) -> tuple[dict, bytes]:
    data = _read(safe_path(base, relative, True), MAX_CONFIG_BYTES, what)
    properties = parse_properties(data)
    for key, value in properties.items():
        _classify(key, value, what)
    return properties, data


def _load_recipe(base: Path, relative: str) -> tuple[dict, dict, dict]:
    """Read only schema, properties, scannerProperties, lint and tools from a recipe."""
    raw = _read(safe_path(base, relative, True), MAX_CONFIG_BYTES, "Recipe")
    recipe = _exact(strict_json(raw, "Recipe"),
                    {"schema", "properties", "scannerProperties", "test", "lint", "tools"},
                    {"schema", "properties", "tools"}, "Recipe")
    if type(recipe["schema"]) is not int or recipe["schema"] != SCHEMA:
        _fail("Recipe schema must be integer 1")
    properties_name = "/".join(_parts(recipe["properties"], "properties path"))
    baseline, properties_bytes = _read_properties(base, properties_name, "Scanner properties")
    overlay = recipe.get("scannerProperties", {})
    if not isinstance(overlay, dict):
        _fail("scannerProperties must be an object")
    merged = dict(baseline)
    for key, value in overlay.items():
        _classify(key, value, "Overlay")
        merged[key] = value
    _lint(recipe.get("lint", {}))
    tools = _tools(recipe["tools"])
    binding = {"config": _sha(raw), "propertiesFile": properties_name,
               "propertiesBytes": _sha(properties_bytes), "overlay": dict(overlay)}
    return merged, tools, binding


def _top_level_languages(base: Path) -> tuple[dict[str, int], list[str]]:
    """Count Git-visible Swift/Python files per top-level directory; list stray root files."""
    counts: dict[str, int] = {}
    stray: list[str] = []
    for rel in _git_files(base):
        if os.path.splitext(rel)[1] not in LANGUAGES:
            continue
        try:
            os.lstat(base / rel)
        except OSError:
            continue  # Tracked file deleted in the working tree.
        parts = rel.split("/")
        top = parts[0]
        if top in RESERVED or top in GENERATED_DIRS or top.startswith("."):
            continue
        if len(parts) == 1:
            if not (top == "Package.swift" or re.fullmatch(r"Package@swift-[0-9.]+\.swift", top)):
                stray.append(top)
            continue
        counts[top] = counts.get(top, 0) + 1
    return counts, stray


def _detect(base: Path, name: str) -> dict:
    counts, stray = _top_level_languages(base)
    if stray:
        _fail("Ambiguous layout: Swift or Python files at the project root need a reviewed "
              "sonar-project.properties: " + ", ".join(sorted(stray)))
    named = {name, base.name}
    sources, tests, unknown = [], [], []
    for directory in sorted(counts):
        if directory in TEST_DIRS or directory.endswith("Tests"):
            tests.append(directory)
        elif directory in SOURCE_DIRS or directory in named:
            sources.append(directory)
        else:
            unknown.append(directory)
    if unknown:
        _fail("Ambiguous layout: unrecognized top-level directories contain Swift or Python files "
              "(add a reviewed sonar-project.properties): " + ", ".join(unknown))
    if not sources:
        _fail("No conventional Swift or Python production directory found "
              "(App, Sources, Core, Shared, Tools, Scripts or a project-named directory)")
    for directory in sources + tests:
        safe_path(base, directory, True)
    properties = {"sonar.sources": ",".join(sources), "sonar.exclusions": FRESH_EXCLUSIONS}
    if tests:
        properties["sonar.tests"] = ",".join(tests)
    return properties


def _content_hashes(base: Path, inventory: dict) -> dict:
    """Hash selected source and test bytes so a later recheck detects any edit."""
    hashes = {}
    for rel in inventory["sources"] + inventory["tests"]:
        hashes[rel] = _sha(_read(safe_path(base, rel, True), MAX_FILE_BYTES, "Selected source"))
    return hashes


def _digest(parts: dict) -> str:
    return _sha(json.dumps(parts, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def discover_analysis(root: Path, *, config: Path | None = None, project_key: str | None = None,
                      project_name: str | None = None) -> dict:
    """Return a read-only plan for a SwiftLint plus Sonar analysis (no build, test or coverage).

    The result is JSON-serializable: schema, status, kind, root, project_key, project_name,
    config_path (relative string or None), origin (recipe, properties or detected), properties
    (effective, without report properties), tools, inventory, digest and
    omitted_report_properties (sorted names removed from the effective scope only).
    Raises ValueError for unsafe, malformed, unsupported or ambiguous inputs.
    """
    base = _canonical_root(Path(root))
    selected = _select_config(base, Path(root), config)
    tools = dict(DEFAULT_TOOLS)
    if selected is None:
        origin, raw_properties, binding = "detected", None, {"detected": True}
    elif selected.endswith(".properties"):
        origin = "properties"
        raw_properties, data = _read_properties(base, selected, "Scanner properties")
        binding = {"config": _sha(data)}
    else:
        origin = "recipe"
        raw_properties, tools, binding = _load_recipe(base, selected)

    if raw_properties is None:
        key, name = deterministic_naming(base, project_key=project_key, project_name=project_name)
        raw_properties = _detect(base, name)
        raw_properties["sonar.projectKey"] = key
        raw_properties["sonar.projectName"] = name
    else:
        raw_properties = dict(raw_properties)
        for field, prop, value in (("project key", "sonar.projectKey", project_key),
                                   ("project name", "sonar.projectName", project_name)):
            if value is not None:
                _classify(prop, value, f"Supplied {field}")
                raw_properties[prop] = value
        if "sonar.projectKey" not in raw_properties or "sonar.projectName" not in raw_properties:
            key, name = deterministic_naming(base, project_key=raw_properties.get("sonar.projectKey"),
                                             project_name=raw_properties.get("sonar.projectName"))
            raw_properties.setdefault("sonar.projectKey", key)
            raw_properties.setdefault("sonar.projectName", name)

    omitted = sorted(key for key in raw_properties if key in REPORTS)
    effective = {key: value for key, value in raw_properties.items() if key not in REPORTS}
    if "sonar.sources" not in effective:
        _fail("sonar.sources is required. Analysis never guesses the whole project directory")
    inventory = build_inventory(base, effective, [])
    result = {"schema": SCHEMA, "status": "ready", "kind": KIND, "root": str(base),
              "project_key": effective["sonar.projectKey"], "project_name": effective["sonar.projectName"],
              "config_path": selected, "origin": origin, "properties": effective, "tools": tools,
              "inventory": inventory, "omitted_report_properties": omitted}
    result["digest"] = _digest({"binding": binding, "rawProperties": raw_properties, "result": result,
                                  "content": _content_hashes(base, inventory)})
    return result
