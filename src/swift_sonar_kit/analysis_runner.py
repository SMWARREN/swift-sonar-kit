"""Source analysis only: SwiftLint and Sonar, never project builds or tests.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from . import receipts
from .analysis_config import discover_analysis
from .bootstrap import prepared_environment
from .console import child_stdout, present_fields, remember_result
from .dashboard import dashboard_url
from .project_config import DEFAULT_LINT, safe_path, serialize_properties
from .reports import swiftlint_report
from .project_runner import _lint, _write, child_environment
from .server import preflight, validate_url
from .workflow import _lock, _record


# The pinned sonar-apple plugin discovers optional executables through PATH.
# Expose the scanner's shell utilities and macOS certificate reader. Actual SwiftLint
# findings are imported as a fresh generic report, not rerun by the plugin.
# In particular, do not expose xcrun, xcodebuild, swift, periphery, mobsfscan or
# oclint. Unavailable optional sensors may log warnings, not run extra tools.
# The scanner SSL library invokes `security` by name to read system trust.
# Keep certificate verification enabled and expose the genuine system binary.
_SCANNER_UTILITIES = ("sh", "dirname", "basename", "readlink", "uname", "sed", "grep", "security")


def _tools(setup: dict, plan: dict) -> dict:
    result = {}
    for name in ("swiftlint", "sonarScanner"):
        item = dict(setup.get("tools", {}).get(name, {}))
        path = Path(item.get("path", ""))
        version, expected = item.get("version", ""), plan["tools"][name]
        if (not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK)
                or not (version == expected or version.startswith(expected + "."))):
            raise ValueError(f"Analysis requires the verified configured {name} executable")
        item["sha256"] = receipts.file_hash(path)
        result[name] = item
    return result


def _verify(root: Path, args, original: dict, inputs: dict, tools: dict) -> None:
    current = discover_analysis(root, config=args.config, project_key=args.project_key,
                                project_name=args.project_name)
    if current["digest"] != original["digest"] or receipts.snapshot(root) != inputs:
        raise ValueError("Project source or analysis configuration changed during analysis")
    if any(receipts.file_hash(Path(item["path"])) != item["sha256"] for item in tools.values()):
        raise ValueError("Analysis executable changed during analysis")


@contextmanager
def _scanner_path(root: Path):
    """Restrict plugin child-tool lookup without modifying the shell's PATH.

    This is a narrow capability boundary for the pinned native scanner/plugin,
    not a sandbox for arbitrary server plugins or a security attestation.
    """
    state = safe_path(root, ".swift-sonar", must_exist=True)
    with tempfile.TemporaryDirectory(prefix=".analysis-path-", dir=state) as directory:
        base = Path(directory)
        for name in _SCANNER_UTILITIES:
            source = Path("/bin" if name == "sh" else "/usr/bin") / name
            if not source.is_file() or not os.access(source, os.X_OK):
                raise ValueError(f"Required scanner system utility is unavailable: {name}")
            (base / name).symlink_to(source)
        yield str(base)


def _settings(plan: dict, unavailable: str, work: str) -> dict:
    properties = dict(plan["properties"])
    exclusions = [item.strip() for item in properties.get("sonar.exclusions", "").split(",") if item.strip()]
    properties["sonar.exclusions"] = ",".join(dict.fromkeys([*exclusions, ".swift-sonar/**", ".scannerwork/**"]))
    properties.update({
        "sonar.qualitygate.wait": "true",
        "sonar.qualitygate.timeout": "300",
        "sonar.working.directory": work,
        # Explicitly override server-side/default report discovery as well as
        # removing reviewed build-report references from the source-only plan.
        # These paths must not exist. No fake empty coverage/results are created.
        "sonar.apple.resultBundlePath": unavailable + ".xcresult",
        "sonar.coverageReportPaths": "",
        "sonar.python.coverage.reportPaths": unavailable + ".xml",
        "sonar.javascript.lcov.reportPaths": unavailable + ".lcov",
        "sonar.testExecutionReportPaths": "",
        "sonar.python.xunit.reportPath": unavailable + "-tests.xml",
        "sonar.externalIssuesReportPaths": ".swift-sonar/analysis-issues.json",
    })
    return properties


def run(args) -> int:
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project path must be a directory")
    url = validate_url(args.url) if args.url else None
    plan = discover_analysis(root, config=args.config, project_key=args.project_key,
                             project_name=args.project_name)
    if args.plan_only:
        remember_result(plan)
        present_fields([(key.replace("_", " ").title(), value) for key, value in plan.items()])
        return 0
    setup_only = getattr(args, "setup_only", False)
    if url and not args.prepare_only and not setup_only and not os.environ.get("SONAR_TOKEN"):
        raise ValueError("An existing server needs SONAR_TOKEN from your secret store")
    with _lock(root, "automatic.lock"), _lock(root, "project.lock"):
        result = {"mode": "analysis-only", "project_key": plan["project_key"],
                  "project_name": plan["project_name"], "config": plan.get("config_path"),
                  "status": "running", "stage": "dependency-setup", "published": False,
                  "tests": "not-run", "builds": "not-run", "coverage": "not-collected",
                  "omitted_report_properties": plan.get("omitted_report_properties", [])}
        remember_result(result)
        # The old project recipe may declare Python coverage. Source-only scans
        # deliberately never request that dependency or any project test tools.
        with prepared_environment(root, tools=plan["tools"], python_coverage=False) as setup:
            result["setup"] = setup
            if setup_only:
                result.update(status="prepared", stage="dependency-setup")
                remember_result(result)
                present_fields([("Setup", "ready"), ("Workflow", "SwiftLint + Sonar only"),
                                ("Builds / tests", "not run"), ("Coverage", "not collected")])
                return 0
            try:
                return _execute(root, args, plan, setup, result, url)
            except (OSError, ValueError, subprocess.SubprocessError):
                result.update(status="failed")
                _record(root, result)
                raise


def _execute(root: Path, args, plan: dict, setup: dict, result: dict, url: str | None) -> int:
    present_fields([("Project", plan["project_name"]), ("Root", root),
                    ("Workflow", "SwiftLint + Sonar only"), ("Builds / tests", "not run"),
                    ("Coverage", "not collected")])
    if plan.get("omitted_report_properties"):
        present_fields([("Unused test / coverage report settings",
                         ", ".join(plan["omitted_report_properties"]))])
    tools = _tools(setup, plan)
    inputs = receipts.snapshot(root)
    result["stage"] = "lint"
    remember_result(result)
    recipe = {"lint": {"argv": list(DEFAULT_LINT), "import": "native"}}
    findings = []
    lint = _lint(root, recipe, tools, report_name="analysis-lint.json", findings_out=findings)
    generic = swiftlint_report(findings, root)
    selected = set(plan["inventory"]["sources"] + plan["inventory"]["tests"])
    generic["issues"] = [issue for issue in generic["issues"]
                         if issue["primaryLocation"]["filePath"] in selected]
    used_rules = {issue["ruleId"] for issue in generic["issues"]}
    generic["rules"] = [rule for rule in generic["rules"] if rule["id"] in used_rules]
    _write(root, "analysis-issues.json", generic)
    issue_path = safe_path(root, ".swift-sonar/analysis-issues.json", must_exist=True)
    issue_digest = receipts.file_hash(issue_path)
    lint.update(imported_findings=len(generic["issues"]), **{"import": "generic"})
    result["lint"] = lint
    _verify(root, args, plan, inputs, tools)
    # Findings are reportable code issues, not a reason to skip the other
    # requested analyzer. Execution/malformed-output failures still fail closed.
    lint_failed = lint["status"] == "findings"
    if lint_failed:
        print(f"SwiftLint reported {lint['findings']} findings. Continuing to Sonar analysis.")
    run_id = uuid.uuid4().hex
    unavailable = ".swift-sonar/not-collected-" + run_id
    work = ".swift-sonar/analysis-scannerwork-" + run_id
    settings = serialize_properties(_settings(plan, unavailable, work))
    _write(root, "analysis-effective.properties", settings)
    _write(root, "analysis-plan.json", {"schema": 1, "mode": "analysis-only", "digest": plan["digest"],
           "inventory": plan["inventory"], "properties": _settings(plan, unavailable, work),
           "lint": lint, "tests": "not-run", "coverage": "not-collected", "published": False})
    if args.prepare_only:
        result.update(status="failed" if lint_failed else "prepared", stage="local-analysis",
                      publication_attempted=False, scanner_exit_code=None)
        _record(root, result)
        present_fields([("Sonar", "not submitted (--prepare-only)"), ("Lint findings", lint["findings"])])
        return int(lint_failed)
    result["stage"] = "sonar-setup"
    _record(root, result)
    if url is None:
        from .local_server import ensure_local_server
        server = ensure_local_server(root, plan["project_key"])
        url = validate_url(server["url"])
        token = server["token"]
    else:
        token = os.environ.get("SONAR_TOKEN", "")
    server_info = preflight(url, token, plan["tools"]["sonarApple"])
    _verify(root, args, plan, inputs, tools)
    for suffix in (".xcresult", ".xml", "-tests.xml", ".lcov"):
        if safe_path(root, unavailable + suffix).exists():
            raise ValueError("Analysis-only report sentinel unexpectedly exists")
    if (safe_path(root, ".swift-sonar/analysis-effective.properties").read_text(encoding="utf-8") != settings
            or receipts.file_hash(issue_path) != issue_digest):
        raise ValueError("Analysis settings or lint report changed before publication")
    result.update(stage="sonar", publication_attempted=True, published=None)
    _record(root, result)
    env = child_environment()
    env.update(SONAR_HOST_URL=url, SONAR_TOKEN=token)
    with _scanner_path(root) as path:
        env["PATH"] = path
        process = subprocess.run([tools["sonarScanner"]["path"],
                                 "-Dproject.settings=.swift-sonar/analysis-effective.properties",
                                 "-Dsonar.qualitygate.wait=true", "-Dsonar.qualitygate.timeout=300",
                                 f"-Dsonar.host.url={url}"], cwd=root, env=env,
                                 stdout=child_stdout(), check=False)
    _verify(root, args, plan, inputs, tools)
    if (safe_path(root, ".swift-sonar/analysis-effective.properties").read_text(encoding="utf-8") != settings
            or receipts.file_hash(issue_path) != issue_digest):
        raise ValueError("Analysis settings or lint report changed during publication")
    for suffix in (".xcresult", ".xml", "-tests.xml", ".lcov"):
        if safe_path(root, unavailable + suffix).exists():
            raise ValueError("Analysis-only report sentinel appeared during publication")
    result.update(status="failed" if process.returncode or lint_failed else "passed", stage="complete",
                  scanner_exit_code=process.returncode, published=True if process.returncode == 0 else None,
                  quality_gate="passed" if process.returncode == 0 else "not-established", server=server_info,
                  server_url=url, dashboard_url=dashboard_url(url, plan["project_key"]))
    _record(root, result)
    present_fields([("Sonar scanner exit", process.returncode), ("Lint findings", lint["findings"]),
                    ("Selected-source SwiftLint issues imported", lint["imported_findings"]),
                    ("Result", result["status"]), ("Sonar dashboard", result["dashboard_url"]),
                    ("Builds / tests", "not run")])
    return int(bool(process.returncode or lint_failed))
