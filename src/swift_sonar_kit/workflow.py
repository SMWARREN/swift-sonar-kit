"""One project path, explicit local execution and honest publication gates.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from . import __version__
from .console import present_fields, remember_result
from .project_config import load_project, safe_path
from .project_runner import _job, project_lint_preflight, project_scan, project_test
from .server import validate_url


def _state(root: Path) -> Path:
    directory = safe_path(root, ".swift-sonar")
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


@contextmanager
def _lock(root: Path, name: str):
    _state(root)
    path = safe_path(root, ".swift-sonar/" + name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another project job is using this checkout") from error
        yield
    finally:
        os.close(fd)


def _record(root: Path, result: dict) -> None:
    """Only store non-secret orchestration facts, never container credentials."""
    path = safe_path(root, ".swift-sonar/run-summary.json")
    payload = {"schema": 1, "tool_version": __version__, **result}
    _state(root)
    temporary = safe_path(root, ".swift-sonar/.run-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
        remember_result(payload)
    finally:
        temporary.unlink(missing_ok=True)


def archive_previous_outputs(root: Path, config: Path) -> Path | None:
    """Archive a dedicated, ignored output directory without touching source files.

    Project mode itself never moves outputs. This explicit automatic workflow does,
    before testing, only when every declared artifact has a common dedicated output
    directory that is ignored by Git and contains no tracked/source/test inputs.
    An unsafe or ambiguous layout is rejected instead of moving arbitrary files.
    """
    with _job(root, allow_owned=True):
        return _archive_previous_outputs(root, config)


def _archive_previous_outputs(root: Path, config: Path) -> Path | None:
    recipe = load_project(root, config)
    paths = [Path(item["path"]) for item in recipe["test"]["artifacts"]]
    common = Path(os.path.commonpath([str(path.parent) for path in paths]))
    existing = [safe_path(root, str(path)) for path in paths if safe_path(root, str(path)).exists()]
    base = safe_path(root, common.as_posix()) if common.as_posix() != "." else root
    if not existing and not base.exists():
        return None
    if common.as_posix() == "." or len(common.parts) < 2 or common.parts[0] not in {"build", ".build"}:
        if not existing:
            return None
        raise ValueError("Previous reports need a dedicated ignored build subdirectory before automatic archival")
    if not base.exists():
        return None
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", common.as_posix()],
                             capture_output=True, check=False)
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", common.as_posix()],
                             capture_output=True, check=False)
    if tracked.returncode or tracked.stdout or ignored.returncode:
        raise ValueError("Refusing to archive an output directory that is tracked or not ignored by Git")
    inventory = recipe["inventory"]
    for key in ("sources", "tests"):
        for value in inventory.get(key, []):
            candidate = Path(value)
            if candidate == common or candidate.is_relative_to(common) or common.is_relative_to(candidate):
                raise ValueError("Output archival would overlap a production or test input")
    protected = [recipe["config"], recipe["propertiesFile"]]
    protected += [item["expectedTests"] for item in recipe["test"]["artifacts"] if "expectedTests" in item]
    for value in protected:
        if Path(value).is_relative_to(common):
            raise ValueError("Output archival would move a recipe, properties file or independent test manifest")
    _state(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    archive = safe_path(root, ".swift-sonar/history/" + stamp)
    archive.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.move(str(base), str(archive / common.name))
    for name in ("project-tests.json", "project-plan.json", "project-summary.json", "project-lint.json", "run-summary.json"):
        source = safe_path(root, ".swift-sonar/" + name)
        if source.exists():
            shutil.move(str(source), str(archive / name))
    return archive


@contextmanager
def _scanner_environment(url: str, token: str):
    previous = {key: os.environ.get(key) for key in ("SONAR_HOST_URL", "SONAR_TOKEN")}
    os.environ.update(SONAR_HOST_URL=url, SONAR_TOKEN=token)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(args) -> int:
    """Run the same project-test, local preparation and scan flow for one root."""
    from .automatic import DEFAULT_TOOLS, discover, prepare

    root = args.root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project path must be a directory")
    if args.url:
        validate_url(args.url)
    if args.max_test_age <= 0:
        raise ValueError("Receipt maximum age must be positive")
    setup_only = getattr(args, "setup_only", False)
    plan = discover(root, config=args.config, project_key=args.project_key, project_name=args.project_name,
                    tool_versions=None if args.plan_only else DEFAULT_TOOLS)
    if args.plan_only:
        remember_result(plan)
        present_fields([(key.replace("_", " ").title(), value) for key, value in plan.items()])
        return 0 if plan.get("status") == "ready" else 2
    if plan.get("status") != "ready":
        raise ValueError(plan.get("reason") or "Project discovery needs review before automatic setup")
    if args.url and not args.prepare_only and not setup_only and not os.environ.get("SONAR_TOKEN"):
        raise ValueError("An existing server needs SONAR_TOKEN from your secret store")
    # Setup-only shares the checkout lock, but must not use _job's scan-failure
    # report writer if dependency setup fails before any analysis is attempted.
    project_lock = _lock(root, "project.lock") if setup_only else _job(root)
    with _lock(root, "automatic.lock"), project_lock:
        remember_result({"status": "running", "stage": "dependency-setup", "published": False})
        with _setup_environment(root, plan) as setup:
            if setup_only:
                result = {"status": "prepared", "stage": "dependency-setup", "published": False,
                          "project_key": plan.get("project_key"), "setup": setup}
                remember_result(result)
                present_fields([("Setup", "ready"), ("Tests", "not run"), ("Publication", "not attempted")])
                return 0
            prepared = prepare(root, plan=plan, config=args.config, project_key=args.project_key,
                               project_name=args.project_name)
            return _execute(root, args, prepared, setup=setup)


def _setup_environment(root: Path, plan: dict):
    """Prepare only supported, declared dependencies, never run project tests."""
    from .bootstrap import prepared_environment

    recipe = plan.get("recipe") or {}
    required = recipe.get("tools") or plan.get("toolchain")
    python_coverage = any(item.get("kind") == "python-coverage"
                          for item in recipe.get("test", {}).get("artifacts", []))
    return prepared_environment(root, tools=required, python_coverage=python_coverage)


def _execute(root: Path, args, prepared: dict, *, setup: dict | None = None) -> int:
    config = Path(prepared["config_path"])
    if config.is_absolute():
        config = config.relative_to(root)
    recipe = load_project(root, config)
    key = args.project_key or recipe["properties"]["sonar.projectKey"]
    name = args.project_name or recipe["properties"].get("sonar.projectName", key)
    present_fields([("Project", f"{name} ({key})"), ("Root", root), ("Workflow", config)])
    result = {"project_key": key, "project_name": name, "config": config.as_posix(),
              "status": "running", "published": False}
    if setup is not None:
        result["setup"] = setup
    continue_on_lint = getattr(args, "continue_on_lint", False)
    try:
        lint = project_lint_preflight(root, config)
        result["lint_preflight"] = lint
        preflight_findings = _has_lint_findings(lint)
        result["diagnostic_lint_continuation"] = bool(continue_on_lint and preflight_findings)
        if lint["status"] != "passed" and not (continue_on_lint and preflight_findings):
            result.update(status="blocked", stage="lint-preflight", tests="not-run")
            _record(root, result)
            print(f"Strict SwiftLint found {lint['findings']} issues. Tests and publication were not started.")
            print("Findings are retained under the project root in .swift-sonar/preflight-lint.json")
            return 1
        if preflight_findings:
            print("Continuing with recorded SwiftLint findings. The overall audit will remain failed.")
        if not args.no_archive:
            archive = archive_previous_outputs(root, config)
            if archive:
                print(f"Previous audit evidence preserved: {archive.relative_to(root)}")
        _record(root, result)
        status = project_test(root, config, record_failures=args.publish_failed_tests)
        result["test_exit_code"] = status
        if status and not args.publish_failed_tests:
            result.update(status="failed", stage="tests")
            _record(root, result)
            print("Tests failed. Evidence retained, no scan published.")
            return status
        # Invalidate only the old generated plan. A prior diagnostic plan must
        # never turn a failed current validation into permission to start a server.
        plan_path = safe_path(root, ".swift-sonar/project-plan.json")
        plan_path.unlink(missing_ok=True)
        prepared_status = project_scan(root, config, prepare_only=True, project_key=args.project_key,
                                       max_age=args.max_test_age, publish_failed_tests=args.publish_failed_tests,
                                       continue_on_lint=continue_on_lint)
        plan = {}
        if plan_path.is_file():
            if plan_path.stat().st_size > 1024 * 1024:
                raise ValueError("Project plan exceeds the size bound")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise ValueError("Unsupported project plan")
        valid_plan = plan.get("schema") == 1 and plan.get("recipe") == recipe["digest"]
        final_lint = plan.get("lint", {}) if valid_plan else {}
        lint_diagnostic = (continue_on_lint and valid_plan
                           and plan.get("diagnostic_lint_continuation") is True
                           and _has_lint_findings(final_lint))
        test_diagnostic = (status and args.publish_failed_tests and valid_plan
                           and plan.get("diagnostic_failed_tests") is True)
        if final_lint:
            result["lint"] = final_lint
        result["diagnostic_lint_continuation"] = bool(
            continue_on_lint and (preflight_findings or lint_diagnostic))
        if prepared_status and not (prepared_status == 1 and (test_diagnostic or lint_diagnostic)):
            result.update(status="blocked", stage="local-validation")
            _record(root, result)
            return prepared_status
        if args.prepare_only:
            failed = bool(status or prepared_status or preflight_findings)
            result.update(status="failed" if failed else "prepared", stage="local-preparation")
            _record(root, result)
            return 1 if failed else 0
        if args.url:
            url = args.url
            token = os.environ.get("SONAR_TOKEN", "")
            if not token:
                raise ValueError("An existing server needs SONAR_TOKEN from your secret store")
        else:
            from .local_server import ensure_local_server
            server = ensure_local_server(root, key)
            url, token = server["url"], server["token"]
        with _scanner_environment(url, token):
            scanned = project_scan(root, config, url=url, project_key=args.project_key,
                                   max_age=args.max_test_age, publish_failed_tests=args.publish_failed_tests,
                                   continue_on_lint=continue_on_lint)
        native_summary = safe_path(root, ".swift-sonar/project-summary.json")
        details = json.loads(native_summary.read_text(encoding="utf-8")) if native_summary.is_file() else {}
        attempted = details.get("publication_attempted") is True
        accepted = attempted and details.get("scanner_exit_code") == 0
        if isinstance(details.get("lint"), dict):
            result["lint"] = details["lint"]
        any_lint = preflight_findings or _has_lint_findings(result.get("lint", {}))
        result["diagnostic_lint_continuation"] = bool(continue_on_lint and any_lint)
        overall = scanned or int(bool(status or any_lint or prepared_status))
        result.update(status="failed" if overall else "passed", stage="scan", server_url=url,
                      publication_attempted=attempted, published=True if accepted else None if attempted else False,
                      publication_status="scanner-completed" if accepted else "not-established",
                      scanner_exit_code=details.get("scanner_exit_code"), exit_code=overall)
        _record(root, result)
        print(f"Audit {'failed' if overall else 'passed'}. Report: {root / '.swift-sonar/run-summary.json'}")
        return overall
    except (OSError, ValueError, subprocess.SubprocessError):
        result.update(status="failed", stage="validation-or-execution")
        _record(root, result)
        raise


def _has_lint_findings(lint: dict) -> bool:
    """Only an explicit, positive finding count is a continuable lint result."""
    if not isinstance(lint, dict):
        return False
    count = lint.get("findings")
    return lint.get("status") == "findings" and type(count) is int and count > 0


def summary(root: Path, *, as_json: bool = False) -> int:
    root = root.resolve(strict=True)
    path = safe_path(root, ".swift-sonar/run-summary.json", must_exist=True)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Audit summary exceeds the size bound")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("Unsupported audit summary")
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        present_fields([
            ("Project", data.get("project_name", data.get("project_key", "not recorded"))),
            ("Status", data.get("status", "unknown")),
            ("Stage", data.get("stage", "not recorded")),
            ("Published", data.get("published", "not recorded")),
            ("Report", path),
        ])
    return 0
