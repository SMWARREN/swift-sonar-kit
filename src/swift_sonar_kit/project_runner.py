"""Whole-project orchestration with native reports and independent local gates.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from . import __version__, receipts
from .console import child_stdout, present_findings, remember_lint_tool
from .project_config import load_project, safe_path, serialize_properties
from .project_artifacts import artifact_digest, native_xcresult, validate_artifact
from .server import preflight, validate_url

_ACTIVE_JOBS = ContextVar("swift_sonar_active_jobs", default=frozenset())


def child_environment() -> dict:
    """Do not hand scanner credentials or JVM injection flags to build/lint tools."""
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
               "DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS", "SWIFT_EXEC", "CC", "CXX", "CFLAGS",
               "CXXFLAGS", "LDFLAGS", "PYTHON", "PYTHONPATH", "VIRTUAL_ENV", "CI",
               "HF_IOS_SCHEME", "HF_MAC_SCHEME", "HF_IOS_DESTINATION", "HF_UNIT_DESTINATION", "HF_MAC_DESTINATION"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    # Tests run in the consumer checkout, not the toolkit's working directory.
    # Preserve the meaning of a developer's relative module paths across chdir.
    if "PYTHONPATH" in environment:
        environment["PYTHONPATH"] = os.pathsep.join(
            os.path.abspath(entry or os.curdir)
            for entry in environment["PYTHONPATH"].split(os.pathsep)
        )
    return environment


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write(root: Path, name: str, value) -> None:
    path = safe_path(root, ".swift-sonar/" + name)
    payload = value if isinstance(value, str) else json.dumps(value, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".project-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, ".swift-sonar/" + name)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _job(root: Path, *, allow_owned: bool = False):
    identity = str(root.resolve(strict=True))
    active = _ACTIVE_JOBS.get()
    if allow_owned and identity in active:
        yield
        return
    directory = safe_path(root, ".swift-sonar")
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = safe_path(root, ".swift-sonar/project.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    token = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another project job is using this checkout") from error
        token = _ACTIVE_JOBS.set(active | {identity})
        try:
            yield
        except (OSError, ValueError, subprocess.SubprocessError):
            _write(root, "project-summary.json", {"status": "failed", "publication_status": "not-established",
                    "detail": "Project validation or execution failed. See the command error."})
            raise
    finally:
        if token is not None:
            _ACTIVE_JOBS.reset(token)
        os.close(fd)


def _invalidate(root: Path, *names: str) -> None:
    for name in names:
        safe_path(root, ".swift-sonar/" + name).unlink(missing_ok=True)


def _probe(argv: list[str], root: Path) -> str:
    result = subprocess.run(argv, cwd=root, env=child_environment(), capture_output=True,
                            text=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError(f"Required tool probe failed: {Path(argv[0]).name}")
    return (result.stdout + result.stderr).strip()


def toolchain(root: Path, recipe: dict) -> dict:
    result = {"kit": __version__}
    context = {key: value for key, value in child_environment().items()
               if key not in {"TMPDIR", "USER", "LOGNAME", "SHELL"}}
    result["buildEnvironment"] = hashlib.sha256(_json_bytes(context)).hexdigest()
    for key, binary, arguments, pattern in (
            ("swiftlint", "swiftlint", ["version"], r"(?m)^([0-9]+\.[0-9]+\.[0-9]+)\s*$"),
            ("sonarScanner", "sonar-scanner", ["--version"], r"SonarScanner CLI ([0-9.]+)")):
        executable = shutil.which(binary)
        if not executable:
            raise ValueError(f"Required tool is missing: {binary}")
        path = Path(executable).resolve(strict=True)
        output = _probe([str(path), *arguments], root)
        match = re.search(pattern, output)
        if key == "sonarScanner":
            config_match = re.search(r"Scanner configuration file: ([^\r\n]+)", output)
            if not config_match:
                raise ValueError("Scanner did not identify its global configuration file")
            global_config = Path(config_match[1].strip())
            if not global_config.is_file():
                raise ValueError("Scanner global configuration is not a regular file")
            # Global properties otherwise silently add unchecked scope or credentials.
            if any(line.strip() and not line.lstrip().startswith(("#", "!"))
                   for line in global_config.read_text(encoding="utf-8").splitlines()):
                raise ValueError("Project mode requires an unconfigured scanner distribution. Keep project settings in the reviewed recipe")
            result["scannerGlobalConfig"] = {"path": str(global_config), "sha256": receipts.file_hash(global_config)}
        expected = recipe["tools"][key]
        if not match or not (match[1] == expected or match[1].startswith(expected + ".")):
            raise ValueError(f"Installed {binary} does not match configured version {expected}")
        result[key] = {"path": str(path), "sha256": receipts.file_hash(path), "version": match[1]}
    test_command = recipe["test"]["argv"][0]
    if "/" in test_command and not Path(test_command).is_absolute():
        test_executable = str(safe_path(root, test_command.removeprefix("./"), must_exist=True))
    else:
        test_executable = shutil.which(test_command)
    if not test_executable or not os.access(test_executable, os.X_OK):
        raise ValueError("Configured test executable is missing or not executable")
    test_path = Path(test_executable).resolve(strict=True)
    result["testExecutable"] = {"path": str(test_path), "sha256": receipts.file_hash(test_path)}
    if any(item["kind"] == "xcresult" for item in recipe["test"]["artifacts"]):
        result["xcode"] = _probe(["xcodebuild", "-version"], root)
        result["developerDirectory"] = _probe(["xcode-select", "-p"], root)
        result["xcresulttool"] = _probe(["xcrun", "xcresulttool", "version"], root)
        result["swift"] = _probe(["xcrun", "swift", "--version"], root)
    # A development install changing implementation must invalidate its own evidence.
    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.name.encode() + b"\0" + source.read_bytes())
    result["implementation"] = digest.hexdigest()
    return result


def _artifacts(root: Path, recipe: dict, start: float, end: float, allow_failed: bool = False) -> dict:
    result = {}
    for item in recipe["test"]["artifacts"]:
        before = artifact_digest(root, item)
        validation = validate_artifact(root, item, recipe["inventory"], start, end, allow_failed=allow_failed)
        if before != artifact_digest(root, item):
            raise ValueError("Report changed while being validated")
        result[item["path"]] = {"digest": before, "validation": validation}
    return result


def _prepare_native_artifacts(root: Path, recipe: dict, *, allow_failed: bool) -> None:
    for artifact in recipe["test"]["artifacts"]:
        if artifact["kind"] == "xcresult":
            native_xcresult(safe_path(root, artifact["path"], must_exist=True), allow_failed=allow_failed)


def project_test(root: Path, config: Path, *, record_failures: bool = False) -> int:
    with _job(root, allow_owned=True):
        _invalidate(root, "project-tests.json", "project-summary.json", "project-plan.json")
        recipe = load_project(root, config)
        before = receipts.snapshot(root)
        tools = toolchain(root, recipe)
        for artifact in recipe["test"]["artifacts"]:
            path = safe_path(root, artifact["path"])
            if path.exists():
                raise ValueError(f"Move prior output aside before testing: {artifact['path']}")
        start = time.time()
        result = subprocess.run(recipe["test"]["argv"], cwd=root, env=child_environment(),
                                stdout=child_stdout(), check=False)
        if result.returncode and not record_failures:
            _write(root, "project-summary.json", {"status": "failed", "published": False,
                    "tests": {"status": "failed", "exit_code": result.returncode}})
            return result.returncode if result.returncode > 0 else 1
        # Xcode 27 materializes database.sqlite3 on its first modern result read.
        # Treat that official-reader preparation as part of this run, before the
        # report hashes and end time are frozen. Never exclude the cache from hashes.
        _prepare_native_artifacts(root, recipe, allow_failed=record_failures)
        end = time.time()
        if before != receipts.snapshot(root) or recipe["digest"] != load_project(root, config)["digest"]:
            raise ValueError("Project or recipe inputs changed during tests")
        artifacts = _artifacts(root, recipe, start, end, allow_failed=record_failures)
        native_failed = any(item["validation"].get("native", {}).get("result") == "Failed"
                            for item in artifacts.values())
        exit_code = (result.returncode if result.returncode > 0 else 1) if result.returncode else int(native_failed)
        if tools != toolchain(root, recipe) or before != receipts.snapshot(root):
            raise ValueError("Toolchain or project changed during artifact validation")
        evidence = {"schema": 2, "scope": "configured-project-recipe", "exit_code": exit_code,
                    "test_status": "failed" if exit_code else "passed",
                    "diagnostic_recording": record_failures,
                    "started_at": start, "finished_at": end, "inputs": before,
                    "recipe": recipe["digest"], "toolchain": tools, "artifacts": artifacts,
                    "command_sha256": hashlib.sha256(_json_bytes(recipe["test"]["argv"])).hexdigest(),
                    "limitations": "Local unsigned evidence. Census covers declared native manifests, not every possible test."}
        _write(root, "project-tests.json", evidence)
        print(f"Project tests {evidence['test_status']}. Bound {len(artifacts)} fresh reports.")
        return exit_code


def verify_project(root: Path, config: Path, recipe: dict, max_age: int, tools: dict,
                   allow_failed: bool = False) -> dict:
    if type(max_age) is not int or max_age <= 0:
        raise ValueError("Receipt maximum age must be a positive integer")
    path = safe_path(root, ".swift-sonar/project-tests.json", must_exist=True)
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(evidence, dict) or type(evidence.get("schema")) is not int
            or evidence["schema"] != 2 or type(evidence.get("exit_code")) is not int
            or evidence["exit_code"] < 0
            or (evidence["exit_code"] != 0 and not (allow_failed and evidence.get("diagnostic_recording") is True))
            or evidence.get("scope") != "configured-project-recipe"):
        raise ValueError("Project mode requires a successful schema-2 project-test receipt")
    start, end = evidence.get("started_at"), evidence.get("finished_at")
    now = time.time()
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end))
            or not 0 < start <= end <= now or now - end > max_age):
        raise ValueError("Project test evidence is stale or has invalid timestamps")
    if (recipe["digest"] != load_project(root, config)["digest"] or evidence.get("recipe") != recipe["digest"]
            or evidence.get("inputs") != receipts.snapshot(root) or evidence.get("toolchain") != tools
            or evidence.get("command_sha256") != hashlib.sha256(_json_bytes(recipe["test"]["argv"])).hexdigest()):
        raise ValueError("Project inputs, configuration, command or toolchain changed after tests")
    if evidence.get("artifacts") != _artifacts(root, recipe, start, end, allow_failed=allow_failed):
        raise ValueError("Imported reports changed after tests")
    return evidence


def _lint(root: Path, recipe: dict, tools: dict, *, report_name: str = "project-lint.json",
          findings_out: list[dict] | None = None) -> dict:
    required = ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"]
    if recipe.get("lint") != {"argv": required, "import": "native"}:
        raise ValueError("Project mode requires full-repository strict native SwiftLint")
    argv = [tools["swiftlint"]["path"], *recipe["lint"]["argv"][1:]]
    result = subprocess.run(argv, cwd=root, env=child_environment(), capture_output=True, text=True, check=False)
    if result.returncode not in (0, 2):
        raise ValueError("SwiftLint execution failed, no analysis published")
    findings = json.loads(result.stdout)
    if not isinstance(findings, list) or (result.returncode and not findings):
        raise ValueError("SwiftLint returned invalid evidence, no analysis published")
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("rule_id"), str):
            raise ValueError("SwiftLint returned malformed findings")
    # Native plugin imports issues. This is only the independent strict lint gate.
    status = {"status": "findings" if findings else "passed", "findings": len(findings),
              "exit_code": result.returncode, "import": "native"}
    _write(root, report_name, findings)
    if findings_out is not None:
        findings_out.extend(findings)
    remember_lint_tool(tools["swiftlint"])
    present_findings(findings, root, report_name)
    return status


def project_lint_preflight(root: Path, config: Path) -> dict:
    """Cheap local rejection only, never a replacement for source-bound tests/lint.

    Keep the last full audit intact. This separate report cannot satisfy a test
    receipt and is repeated by project_scan after real tests have completed.
    """
    with _job(root, allow_owned=True):
        recipe = load_project(root, config)
        before = receipts.snapshot(root)
        tools = toolchain(root, recipe)
        result = _lint(root, recipe, tools, report_name="preflight-lint.json")
        if before != receipts.snapshot(root) or recipe["digest"] != load_project(root, config)["digest"]:
            raise ValueError("Project or recipe changed during lint preflight")
        return result


def project_scan(root: Path, config: Path, *, url: str = "http://localhost:9000",
                 prepare_only: bool = False, project_key: str | None = None,
                 max_age: int = 3600, publish_failed_tests: bool = False,
                 continue_on_lint: bool = False) -> int:
    validate_url(url)
    with _job(root, allow_owned=True):
        _invalidate(root, "project-summary.json", "project-plan.json")
        recipe = load_project(root, config)
        tools = toolchain(root, recipe)
        evidence = verify_project(root, config, recipe, max_age, tools, allow_failed=publish_failed_tests)
        lint = _lint(root, recipe, tools)
        if not isinstance(lint, dict) or lint.get("status") not in ("passed", "findings"):
            raise ValueError(f"Invalid SwiftLint status: {lint.get('status') if isinstance(lint, dict) else lint}")
        lint_failed = lint["status"] == "findings"
        if lint_failed and not continue_on_lint:
            _write(root, "project-summary.json", {"status": "blocked", "lint": lint, "published": False})
            print("Strict SwiftLint found issues. No scan was published.")
            return 1
        diagnostic_lint_continuation = bool(continue_on_lint and lint_failed)
        properties = dict(recipe["properties"])
        if project_key is not None:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", project_key) or project_key.isdigit():
                raise ValueError("Invalid isolated project key")
            properties["sonar.projectKey"] = project_key
        properties.update({"sonar.qualitygate.wait": "true", "sonar.qualitygate.timeout": "300",
                           "sonar.working.directory": ".swift-sonar/scannerwork"})
        settings = serialize_properties(properties)
        _write(root, "project-effective.properties", settings)
        plan = {"schema": 1, "tool_version": __version__, "recipe": recipe["digest"],
                "properties": properties, "inventory": recipe["inventory"], "toolchain": tools,
                "artifacts": evidence["artifacts"], "lint": lint,
                "test_scope": evidence["scope"], "limitations": evidence["limitations"],
                "project_key_override": project_key, "published": False,
                "test_status": evidence.get("test_status", "passed"),
                "diagnostic_failed_tests": bool(evidence["exit_code"]),
                "diagnostic_lint_continuation": diagnostic_lint_continuation}
        verify_project(root, config, recipe, max_age, tools, allow_failed=publish_failed_tests)
        _write(root, "project-plan.json", plan)
        if prepare_only:
            print("Project scan prepared locally. No server requests or publication.")
            failed = bool(evidence["exit_code"] or lint_failed)
            _write(root, "project-summary.json", {
                "status": "failed" if failed else "passed",
                "exit_code": 1 if failed else 0,
                "scanner_exit_code": None,
                "published": False,
                "publication_attempted": False,
                "lint": lint,
                "diagnostic_lint_continuation": diagnostic_lint_continuation,
                "test_scope": evidence["scope"],
                "test_exit_code": evidence["exit_code"],
                "test_status": evidence.get("test_status", "passed"),
            })
            return 1 if failed else 0
        token = os.environ.get("SONAR_TOKEN", "")
        server = preflight(url, token, recipe["tools"]["sonarApple"])
        verify_project(root, config, recipe, max_age, tools, allow_failed=publish_failed_tests)
        env = child_environment()
        env.update(SONAR_HOST_URL=url, SONAR_TOKEN=token)
        # CWD is authoritative. Do not combine project.settings with projectBaseDir.
        argv = [tools["sonarScanner"]["path"], "-Dproject.settings=.swift-sonar/project-effective.properties",
                "-Dsonar.qualitygate.wait=true", "-Dsonar.qualitygate.timeout=300", f"-Dsonar.host.url={url}"]
        result = subprocess.run(argv, cwd=root, env=env, stdout=child_stdout(), check=False)
        verify_project(root, config, recipe, max_age, toolchain(root, recipe), allow_failed=publish_failed_tests)
        if safe_path(root, ".swift-sonar/project-effective.properties").read_text(encoding="utf-8") != settings:
            raise ValueError("Scanner settings changed during publication")
        overall_failed = bool(result.returncode or evidence["exit_code"] or lint_failed)
        scanner_passed = result.returncode == 0
        _write(root, "project-summary.json", {
            "status": "failed" if overall_failed else "passed",
            "exit_code": 1 if overall_failed else 0,
            "scanner_exit_code": result.returncode,
            "published": True if scanner_passed else None,
            "server": server,
            "lint": lint,
            "diagnostic_lint_continuation": diagnostic_lint_continuation,
            "test_scope": evidence["scope"],
            "test_exit_code": evidence["exit_code"],
            "test_status": evidence.get("test_status", "passed"),
            "publication_attempted": True,
            "quality_gate": "passed" if scanner_passed else "not-passed",
        })
        return 1 if overall_failed else 0
