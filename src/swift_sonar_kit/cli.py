"""Explicit local preparation and opt-in publication. SPDX-License-Identifier: GPL-3.0-only."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import __version__, receipts
from .console import run_output, remember_error
from .reports import lcov_report, swiftlint_report
from .server import preflight


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Free Swift analysis bridge, not a commercial analyzer clone")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="Run SwiftLint and Sonar source analysis only, without builds or tests")
    run.add_argument("root", type=Path, help="Path to your project, including paths with spaces")
    run.add_argument("--config", type=Path, help="Reuse an explicit existing project recipe")
    run.add_argument("--project-key", help="Override the project-derived Sonar key")
    run.add_argument("--project-name", help="Override the analysis display name without modifying project files")
    run.add_argument("--url", default=os.environ.get("SONAR_HOST_URL"),
                     help="Use an existing server with SONAR_TOKEN instead of creating a local one")
    run.add_argument("--plan-only", action="store_true", help="Inspect discovery without tests, writes or server setup")
    run.add_argument("--prepare-only", action="store_true", help="Run lint and prepare source-only settings without server requests")
    run.add_argument("--continue-on-lint", action="store_true",
                     help="Compatibility flag: source-only runs always continue with reportable lint findings")
    run.add_argument("--setup-only", action="store_true",
                     help="Prepare private tool dependencies only, without tests or server requests")
    run.add_argument("--json", action="store_true", dest="as_json",
                     help="Print one JSON result to stdout; progress goes to stderr")
    run.add_argument("--no-browser", action="store_true",
                     help="Do not open the Sonar dashboard after an interactive completed scan")
    run.add_argument("--no-fix-prompt", action="store_true",
                     help="Never offer interactive SwiftLint corrections after the saved analysis report")
    summary = commands.add_parser("summary", help="Read the last path-only workflow report")
    summary.add_argument("root", type=Path)
    summary.add_argument("--json", action="store_true", dest="as_json")
    for name in ("issues", "coverage", "doctor", "test", "scan"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        if name in {"issues", "coverage"}:
            command.add_argument("--input", type=Path, required=True, action="append")
            command.add_argument("--output", type=Path, required=True)
        if name in {"doctor", "scan"}:
            command.add_argument("--url", default=os.environ.get("SONAR_HOST_URL", "http://localhost:9000"))
            command.add_argument("--plugin-version", default="0.5.1")
        if name == "test":
            command.add_argument("--artifact", type=Path, action="append", default=[])
            command.add_argument("command", nargs=argparse.REMAINDER)
        if name == "scan":
            command.add_argument("--project-key", required=True)
            command.add_argument("--source", type=Path, action="append", required=True)
            command.add_argument("--lcov", type=Path, action="append", default=[])
            command.add_argument("--max-test-age", type=int, default=3600)
            command.add_argument("--prepare-only", action="store_true", help="No network or scanner execution")
    for name in ("project-test", "project-scan", "project-run"):
        command = commands.add_parser(name, help="Preserve the complete reviewed project workflow")
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--config", type=Path, default=Path("swift-sonar.json"))
        command.add_argument("--publish-failed-tests", action="store_true",
                             help="Retain complete failed test evidence for diagnosis, never return a passing result")
        if name != "project-test":
            command.add_argument("--continue-on-lint", action="store_true",
                                 help="Submit despite lint findings, never report a passing audit")
            command.add_argument("--url", default=os.environ.get("SONAR_HOST_URL", "http://localhost:9000"))
            command.add_argument("--project-key", help="Explicit isolated analysis key override")
            command.add_argument("--max-test-age", type=int, default=3600)
            command.add_argument("--prepare-only", action="store_true", help="Do not contact or publish to Sonar")
    return result


def _write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symbolic-link output")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _properties_value(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("Configuration values cannot contain control characters")
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace("=", "\\=").replace(":", "\\:")


def _sources(root: Path, values: list[Path]) -> list[str]:
    sources = []
    for raw in values:
        path = receipts.local_path(root, raw)
        relative = path.relative_to(root).as_posix()
        if not path.is_dir() or relative == "." or "," in relative or relative.startswith("."):
            raise ValueError("Choose explicit in-root source directories, not the entire repository")
        _properties_value(relative)
        if (root / raw).is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
            raise ValueError("Selected source directories cannot contain symbolic links")
        if not any(path.rglob("*.swift")):
            raise ValueError("Each source directory must contain Swift files")
        sources.append(relative)
    unique = sorted(set(sources))
    for index, value in enumerate(unique):
        if any(Path(value).is_relative_to(Path(parent)) for parent in unique[:index]):
            raise ValueError("Source directories must not overlap")
    return unique


def _scan(args: argparse.Namespace, root: Path) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", args.project_key) or args.project_key.isdigit():
        raise ValueError("Invalid Sonar project key")
    sources = _sources(root, args.source)
    directory = receipts.local_path(root, Path(".swift-sonar"))
    if (root / ".swift-sonar").is_symlink():
        raise ValueError("Report directory cannot be a symbolic link")
    directory.mkdir(exist_ok=True)
    (directory / "scan-summary.json").unlink(missing_ok=True)
    evidence = receipts.verify(root, args.lcov, args.max_test_age)
    # A fresh command is required. No import of a cached arbitrary lint result in scan.
    env = {key: value for key, value in os.environ.items() if not key.startswith("SONAR_")}
    findings = []
    for source in sources:
        process = subprocess.run(["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json", source],
                                 cwd=root, env=env, capture_output=True, text=True, check=False)
        if process.returncode not in {0, 2}:
            raise ValueError("SwiftLint execution failed. Check the installed tool and project configuration")
        data = json.loads(process.stdout)
        if not isinstance(data, list):
            raise ValueError("SwiftLint did not return a findings array")
        if process.returncode and not data:
            raise ValueError("SwiftLint failed without reportable findings")
        findings.extend(data)
    report = swiftlint_report(findings, root)
    if any(not any(Path(issue["primaryLocation"]["filePath"]).is_relative_to(Path(s)) for s in sources)
           for issue in report["issues"]):
        raise ValueError("SwiftLint reported a file outside the selected source directories")
    _write(directory / "issues.json", json.dumps(report, indent=2) + "\n")
    properties = {
        "sonar.projectKey": args.project_key,
        "sonar.sources": ",".join(sources),
        "sonar.sourceEncoding": "UTF-8",
        "sonar.inclusions": "**/*.swift",
        "sonar.externalIssuesReportPaths": ".swift-sonar/issues.json",
        "sonar.qualitygate.wait": "true",
        "sonar.qualitygate.timeout": "300",
        "sonar.scm.exclusions.disabled": "false",
    }
    if args.lcov:
        texts = [receipts.local_path(root, path).read_text(encoding="utf-8") for path in args.lcov]
        coverage = lcov_report(texts, root)
        # Report adapters validate root boundaries. Scanner also needs selected-source boundaries.
        import xml.etree.ElementTree as ET
        if any(not any(Path(item.attrib["path"]).is_relative_to(Path(s)) for s in sources)
               for item in ET.fromstring(coverage)):
            raise ValueError("Coverage includes files outside the selected source directories")
        _write(directory / "coverage.xml", coverage)
        properties["sonar.coverageReportPaths"] = ".swift-sonar/coverage.xml"
    _write(directory / "sonar-project.properties", "".join(
        f"{key}={_properties_value(value)}\n" for key, value in properties.items()))
    receipts.verify(root, args.lcov, args.max_test_age)
    print(f"Prepared {len(report['issues'])} SwiftLint findings. Tests: selected command only, not a census.")
    if args.prepare_only:
        return 1 if report["issues"] else 0
    token = os.environ.get("SONAR_TOKEN", "")
    server = preflight(args.url, token, args.plugin_version)
    env.update(SONAR_HOST_URL=args.url, SONAR_TOKEN=token)
    result = subprocess.run(["sonar-scanner", "-Dproject.settings=.swift-sonar/sonar-project.properties",
                             "-Dsonar.qualitygate.wait=true"], cwd=root, env=env, check=False)
    # Never turn a server gate pass into a pass for independently failing lint.
    receipts.verify(root, args.lcov, args.max_test_age)
    summary = {"tool_version": __version__, "server": server, "test_scope": evidence["scope"],
               "lint_findings": len(report["issues"]), "scanner_exit_code": result.returncode,
               "coverage_supplied": bool(args.lcov)}
    _write(directory / "scan-summary.json", json.dumps(summary, indent=2) + "\n")
    return 1 if result.returncode or report["issues"] else 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action == "run":
        if args.setup_only and (args.plan_only or args.prepare_only):
            parser().error("--setup-only cannot be combined with --plan-only or --prepare-only")
        with run_output(as_json=args.as_json) as output:
            status = _dispatch(args)
        status = output.finish(status, root=args.root, save=not args.plan_only, setup_only=args.setup_only)
        from .dashboard import open_after_scan
        open_after_scan(args, output)
        if (not args.as_json and not args.plan_only and not args.no_fix_prompt
                and output.error is None and output.saved_path is not None
                and output.findings and output.lint_tool
                and ((output.result.get("status") == "blocked"
                      and output.result.get("stage") in {"lint-preflight", "local-validation"})
                     or (output.result.get("mode") == "analysis-only"
                         and output.result.get("status") == "failed"
                         and output.result.get("stage") in {"complete", "local-analysis"}))):
            from .corrections import offer_corrections
            try:
                offer_corrections(output.saved_path.parent.parent, output.findings, output.lint_tool)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                text = str(error)
                token = os.environ.get("SONAR_TOKEN")
                if token:
                    text = text.replace(token, "[redacted]")
                diagnostic = json.dumps(text)[1:-1]
                print(f"swift-sonar: Correction step unavailable: {diagnostic}. "
                      "Original audit result and exit status are unchanged.", file=sys.stderr)
        return status
    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    try:
        root = args.root.resolve(strict=True)
        if args.action == "run":
            from .analysis_runner import run
            return run(args)
        if args.action == "summary":
            from .workflow import summary
            return summary(root, as_json=args.as_json)
        if args.action in {"project-test", "project-scan", "project-run"}:
            from .project_runner import project_scan, project_test
            if args.action in {"project-test", "project-run"}:
                status = project_test(root, args.config, record_failures=args.publish_failed_tests)
                if args.action == "project-test" or (status and not args.publish_failed_tests):
                    return status
            return project_scan(root, args.config, url=args.url, prepare_only=args.prepare_only,
                                project_key=args.project_key, max_age=args.max_test_age,
                                publish_failed_tests=args.publish_failed_tests,
                                continue_on_lint=args.continue_on_lint)
        if args.action == "doctor":
            print(json.dumps(preflight(args.url, os.environ.get("SONAR_TOKEN", ""), args.plugin_version), indent=2))
        elif args.action == "test":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            return receipts.run_test(root, command, args.artifact)
        elif args.action == "scan":
            return _scan(args, root)
        else:
            paths = [root / path for path in args.input]
            if args.action == "issues":
                findings = []
                for path in paths:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        raise ValueError("SwiftLint report must be an array")
                    findings.extend(data)
                output = json.dumps(swiftlint_report(findings, root), indent=2) + "\n"
            else:
                output = lcov_report([path.read_text(encoding="utf-8") for path in paths], root)
            _write(root / args.output, output)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # File errors and parser errors can include input paths. Never render raw tool stderr.
        text = str(error)
        token = os.environ.get("SONAR_TOKEN")
        if token:
            text = text.replace(token, "[redacted]")
        remember_error(text)
        print(f"swift-sonar: {text}", file=sys.stderr)
        return 2
