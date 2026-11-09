"""Synthetic public analysis-only acceptance. Never runs real analyzers or builds.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from swift_sonar_kit import analysis_config, analysis_runner, cli, project_runner, workflow
from swift_sonar_kit.bootstrap import DEFAULT_TOOLS
from swift_sonar_kit.project_config import parse_properties


class AnalysisRunnerTests(unittest.TestCase):
    def test_source_only_settings_override_native_lcov_discovery(self):
        plan = {"properties": {"sonar.javascript.lcov.reportPaths": "coverage/lcov.info"}}
        settings = analysis_runner._settings(plan, ".swift-sonar/unavailable", ".scannerwork")
        self.assertEqual(settings["sonar.javascript.lcov.reportPaths"], ".swift-sonar/unavailable.lcov")
        self.assertEqual(plan["properties"]["sonar.javascript.lcov.reportPaths"], "coverage/lcov.info")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis only ")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = base / "Project With Spaces"
        self.root.mkdir()
        self.real_run = subprocess.run
        self.real_run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", ".swift-sonar/\nbuild/\n")
        self.write("Sources/A.swift", "struct Example {}\n")
        self.write("Tests/ATests.swift", "struct ExampleTests {}\n")
        self.write("Tools/bomb.sh", "exit 99\n")
        props = {"sonar.projectKey": "Example", "sonar.projectName": "Example App",
                 "sonar.sources": "Sources", "sonar.tests": "Tests",
                 "sonar.apple.resultBundlePath": "build/old.xcresult",
                 "sonar.coverageReportPaths": "build/old-coverage.xml",
                 "sonar.python.coverage.reportPaths": "build/old-python.xml"}
        self.write("sonar-project.properties", "".join(f"{k}={v}\n" for k, v in props.items()))
        self.write("swift-sonar.json", json.dumps({"schema": 1, "properties": "sonar-project.properties",
                    "tools": DEFAULT_TOOLS, "test": {"argv": ["/bin/bash", "Tools/bomb.sh"],
                    "artifacts": [{"kind": "xcresult", "path": "build/old.xcresult",
                                   "expectedTests": "Tools/missing-manifest.json"}]}}))
        self.write("build/old-coverage.xml", "must not import or move")
        self.write(".swift-sonar/project-tests.json", "old test evidence")
        self.write(".swift-sonar/project-summary.json", "old project summary")
        self.originals = {p: (self.root / p).read_bytes() for p in (
            "swift-sonar.json", "sonar-project.properties", "build/old-coverage.xml",
            ".swift-sonar/project-tests.json", ".swift-sonar/project-summary.json")}
        tool_dir = base / "tools"
        tool_dir.mkdir()
        self.setup = {"tools": {}, "python": None, "bin_dirs": [], "cache": str(tool_dir)}
        for name in ("swiftlint", "sonarScanner"):
            p = tool_dir / name
            p.write_text("#!/bin/sh\nexit 99\n")
            p.chmod(0o700)
            self.setup["tools"][name] = {"path": str(p), "version": DEFAULT_TOOLS[name], "source": "fixture"}
        self.findings = []
        self.lint_code = 0
        self.lint_stdout = None
        self.scanner_code = 0
        self.calls = []
        self.interactive_browser = False
        self.mutate_at = None
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"SONAR_TOKEN": "fixture-secret-token"}, clear=False).start()
        patch.object(subprocess, "run", side_effect=self.child).start()
        self.environment = patch.object(analysis_runner, "prepared_environment", side_effect=self.prepared).start()
        self.server = patch("swift_sonar_kit.local_server.ensure_local_server", return_value={
            "url": "http://localhost:43210", "token": "fixture-secret-token"}).start()
        self.preflight = patch.object(analysis_runner, "preflight", return_value={"language": "swift"}).start()
        self.legacy = [patch.object(project_runner, name, side_effect=AssertionError("legacy execution forbidden")).start()
                       for name in ("project_test", "project_scan", "toolchain", "verify_project")]
        patch.object(workflow, "archive_previous_outputs", side_effect=AssertionError("archival forbidden")).start()
        patch("swift_sonar_kit.automatic.prepare", side_effect=AssertionError("test preparation forbidden")).start()
        patch("swift_sonar_kit.receipts.run_test", side_effect=AssertionError("tests forbidden")).start()
        patch("swift_sonar_kit.project_config.load_project", side_effect=AssertionError("test recipe forbidden")).start()

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    @contextmanager
    def prepared(self, root, **kwargs):
        self.assertFalse(kwargs["python_coverage"])
        self.assertEqual(kwargs["tools"], DEFAULT_TOOLS)
        yield self.setup

    def child(self, argv, **kwargs):
        if argv[0] == "git":
            return self.real_run(argv, **kwargs)
        self.calls.append((list(argv), kwargs))
        if argv[0] == self.setup["tools"]["swiftlint"]["path"]:
            self.assertNotIn("SONAR_TOKEN", kwargs["env"])
            self.assertEqual(argv[1:], ["lint", "--quiet", "--strict", "--reporter", "json"])
            if self.mutate_at == "lint":
                self.write("Sources/A.swift", "struct Changed {}\n")
            if self.mutate_at == "config":
                path = self.root / "sonar-project.properties"
                path.write_text(path.read_text() + "sonar.projectName=Changed\n")
            if self.mutate_at == "tool":
                Path(self.setup["tools"]["swiftlint"]["path"]).write_text("changed executable")
            return subprocess.CompletedProcess(argv, self.lint_code,
                self.lint_stdout if self.lint_stdout is not None else json.dumps(self.findings), "")
        if argv[0] == self.setup["tools"]["sonarScanner"]["path"]:
            env = kwargs["env"]
            self.assertEqual(env["SONAR_TOKEN"], "fixture-secret-token")
            self.assertEqual(env["SONAR_HOST_URL"], "http://localhost:43210")
            for forbidden in ("xcodebuild", "xcrun", "swift", "swiftlint", "periphery", "mobsfscan", "oclint"):
                self.assertIsNone(shutil.which(forbidden, path=env["PATH"]))
            self.assertTrue(shutil.which("sh", path=env["PATH"]))
            security = shutil.which("security", path=env["PATH"])
            self.assertIsNotNone(security, "Scanner SSL bootstrap requires macOS security on PATH")
            self.assertEqual(Path(security).resolve(), Path("/usr/bin/security").resolve())
            self.assertEqual(set(p.name for p in Path(env["PATH"]).iterdir()),
                             {"sh", "dirname", "basename", "readlink", "uname", "sed", "grep", "security"})
            self.assertNotIn("fixture-secret-token", " ".join(argv))
            props = parse_properties((self.root / ".swift-sonar/analysis-effective.properties").read_bytes())
            self.assertEqual(props["sonar.sources"], "Sources")
            self.assertEqual(props["sonar.tests"], "Tests")
            self.assertEqual(props["sonar.coverageReportPaths"], "")
            self.assertNotIn("build/old", str(props))
            self.assertNotIn("fixture-secret-token", str(props))
            self.assertEqual(props["sonar.externalIssuesReportPaths"], ".swift-sonar/analysis-issues.json")
            imported = json.loads((self.root / props["sonar.externalIssuesReportPaths"]).read_text())
            self.assertEqual(len(imported["issues"]), len({
                (item["rule_id"], item["file"], item.get("line"), item["reason"])
                for item in self.findings if Path(item["file"]).name in ("A.swift", "ATests.swift")}))
            for key in ("sonar.apple.resultBundlePath", "sonar.python.coverage.reportPaths",
                        "sonar.python.xunit.reportPath"):
                self.assertFalse((self.root / props[key]).exists())
            if self.mutate_at == "scanner":
                self.write("Sources/A.swift", "struct Changed {}\n")
            if self.mutate_at == "settings":
                self.write(".swift-sonar/analysis-effective.properties", "modified")
            if self.mutate_at == "issues":
                self.write(".swift-sonar/analysis-issues.json", '{"rules":[],"issues":[]}\n')
            if self.mutate_at == "lcov":
                self.write(props["sonar.javascript.lcov.reportPaths"], "unexpected report")
            return subprocess.CompletedProcess(argv, self.scanner_code)
        raise AssertionError(f"Unexpected command, must not build/test/enumerate: {argv}")

    def invoke(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: self.interactive_browser
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", str(self.root), "--no-fix-prompt", *flags])
        return code, out.getvalue(), err.getvalue()

    def saved(self):
        return json.loads((self.root / ".swift-sonar/run-result.json").read_text())

    def assert_preserved(self):
        for name, data in self.originals.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse((self.root / ".swift-sonar/python").exists())
        self.assertFalse((self.root / ".swift-sonar/history").exists())
        for child in self.legacy:
            child.assert_not_called()

    def test_public_default_runs_lint_and_sonar_only_with_missing_native_manifest(self):
        code, out, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("SwiftLint + Sonar only", out)
        report = self.saved()
        self.assertEqual(report["mode"], "analysis-only")
        self.assertEqual(report["server_url"], "http://localhost:43210")
        self.assertEqual(report["dashboard_url"], "http://localhost:43210/dashboard?id=Example")
        self.assertIn("Sonar dashboard", out)
        self.assertEqual(report["tests"], "not-run")
        self.assertEqual(report["builds"], "not-run")
        self.assertEqual(report["coverage"], "not-collected")
        self.assertTrue(report["published"])
        self.assert_preserved()

    def test_lint_findings_do_not_block_sonar_without_a_flag(self):
        self.findings = [{"file": str(self.root / "Sources/A.swift"), "line": 1,
                          "rule_id": "line_length", "severity": "Warning", "reason": "Too long"}]
        self.lint_code = 2
        code, out, _ = self.invoke("--json")
        self.assertEqual(code, 1)
        report = self.saved()
        self.assertEqual(json.loads(out), report)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["scanner_exit_code"], 0)
        self.assertTrue(report["published"])
        self.assertEqual(report["findings"], self.findings)
        self.assertEqual(report["lint_reports"]["analysis-lint.json"], self.findings)
        self.assertEqual(len(self.calls), 2)
        self.assert_preserved()

    def test_compatibility_continue_flag_is_not_required(self):
        self.assertEqual(self.invoke("--continue-on-lint")[0], 0)
        self.assertEqual(len(self.calls), 2)

    def test_lint_execution_errors_and_malformed_findings_stop_before_server(self):
        for code, text in ((3, "[]"), (2, "[]"), (0, "not json"), (0, '[{"reason":"missing rule"}]')):
            with self.subTest(code=code, text=text):
                self.calls.clear()
                self.lint_code, self.lint_stdout = code, text
                self.assertEqual(self.invoke("--json")[0], 2)
                self.assertEqual(self.saved()["stage"], "lint")
                self.assertFalse(self.saved()["published"])
                self.server.assert_not_called()
                self.assertEqual(len(self.calls), 1)
        self.assert_preserved()

    def test_prepare_only_is_lint_and_settings_not_tests_or_server(self):
        self.assertEqual(self.invoke("--prepare-only")[0], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.saved()["stage"], "local-analysis")
        self.assertFalse(self.saved()["published"])
        self.server.assert_not_called()
        self.preflight.assert_not_called()
        self.assert_preserved()

    def test_prepare_with_findings_retains_nonzero_without_scan(self):
        self.findings = [{"file": str(self.root / "Sources/A.swift"), "line": 1,
                          "rule_id": "identifier_name", "severity": "Warning", "reason": "Short name"}]
        self.lint_code = 2
        self.assertEqual(self.invoke("--prepare-only")[0], 1)
        self.server.assert_not_called()
        self.assertEqual(len(self.calls), 1)

    def test_setup_only_does_not_run_lint_or_bootstrap_coverage(self):
        self.write(".swift-sonar/run-result.json", "existing full run")
        self.assertEqual(self.invoke("--setup-only")[0], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.root / ".swift-sonar/run-result.json").read_text(), "existing full run")
        report = json.loads((self.root / ".swift-sonar/setup-result.json").read_text())
        self.assertEqual(report["mode"], "analysis-only")
        self.server.assert_not_called()
        self.assert_preserved()

    def test_setup_failure_preserves_project_and_run_summaries(self):
        self.write(".swift-sonar/run-result.json", "existing run result")
        self.write(".swift-sonar/run-summary.json", "existing run summary")
        self.environment.side_effect = ValueError("fixture dependency unavailable")
        self.assertEqual(self.invoke("--setup-only")[0], 2)
        self.assertEqual((self.root / ".swift-sonar/run-result.json").read_text(), "existing run result")
        self.assertEqual((self.root / ".swift-sonar/run-summary.json").read_text(), "existing run summary")
        self.assert_preserved()

    def test_plan_only_never_bootstraps_or_writes(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        code, out, _ = self.invoke("--plan-only", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["kind"], "analysis-only")
        self.environment.assert_not_called()
        self.assertEqual(self.calls, [])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(self.root).parts}
        self.assertEqual(before, after)

    def test_scanner_failure_preserves_publication_uncertainty_and_lint(self):
        self.scanner_code = 7
        self.assertEqual(self.invoke()[0], 1)
        self.assertIsNone(self.saved()["published"])
        self.assertEqual(self.saved()["quality_gate"], "not-established")
        self.assertEqual(self.saved()["scanner_exit_code"], 7)
        self.assertTrue(self.saved()["publication_attempted"])
        self.assert_preserved()

    def test_source_mutation_during_lint_prevents_publication(self):
        self.mutate_at = "lint"
        self.assertEqual(self.invoke()[0], 2)
        self.server.assert_not_called()
        self.assertFalse(self.saved()["published"])

    def test_source_or_settings_mutation_during_scanner_cannot_report_pass(self):
        for stage in ("scanner", "settings", "issues"):
            with self.subTest(stage=stage):
                self.mutate_at = stage
                self.assertEqual(self.invoke()[0], 2)
                self.assertEqual(self.saved()["status"], "failed")
                self.assertIsNone(self.saved()["published"])

    def test_lcov_sentinel_created_before_scanner_blocks_publication(self):
        def create_report(*_):
            props = parse_properties((self.root / ".swift-sonar/analysis-effective.properties").read_bytes())
            self.write(props["sonar.javascript.lcov.reportPaths"], "unexpected report")
            return self.preflight.return_value
        self.preflight.side_effect = create_report
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.saved()["published"])

    def test_lcov_sentinel_created_during_scanner_cannot_report_pass(self):
        self.mutate_at = "lcov"
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.saved()["status"], "failed")
        self.assertIsNone(self.saved()["published"])

    def test_existing_server_does_not_create_docker_server(self):
        self.assertEqual(self.invoke("--url", "http://localhost:43210")[0], 0)
        self.server.assert_not_called()

    def test_missing_existing_server_token_fails_before_dependency_install(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.invoke("--url", "http://localhost:43210")[0], 2)
        self.environment.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_legacy_test_flags_are_rejected_for_default_run(self):
        for flags in (("--publish-failed-tests",), ("--max-test-age", "60"), ("--no-archive",)):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(["run", str(self.root), *flags])

    def test_generated_analysis_paths_are_excluded_from_scanner_sources(self):
        plan = analysis_config.discover_analysis(self.root)
        props = analysis_runner._settings(plan, ".swift-sonar/missing", ".swift-sonar/work")
        self.assertIn(".swift-sonar/**", props["sonar.exclusions"].split(","))
        self.assertIn(".scannerwork/**", props["sonar.exclusions"].split(","))

    def test_scanner_capability_path_is_cleaned_after_failure(self):
        self.scanner_code = 9
        self.invoke()
        self.assertEqual(list((self.root / ".swift-sonar").glob(".analysis-path-*")), [])

    def test_missing_system_security_fails_clearly_without_reading_keychains(self):
        original_access = os.access
        def available(path, mode):
            return False if Path(path) == Path("/usr/bin/security") else original_access(path, mode)
        with patch.object(analysis_runner.os, "access", side_effect=available):
            with self.assertRaisesRegex(ValueError, "system utility is unavailable: security"):
                with analysis_runner._scanner_path(self.root):
                    self.fail("Missing certificate utility must not be ignored")
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.root / ".swift-sonar").glob(".analysis-path-*")), [])

    def test_scanner_full_build_version_accepts_configured_release_prefix(self):
        self.setup["tools"]["sonarScanner"]["version"] = "8.3.1.16491"
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(len(self.calls), 2)

    def test_different_scanner_release_is_rejected_before_execution(self):
        for version in ("8.3.10", "8.4.0"):
            with self.subTest(version=version):
                self.setup["tools"]["sonarScanner"]["version"] = version
                self.assertEqual(self.invoke()[0], 2)
                self.assertEqual(self.calls, [])
                self.server.assert_not_called()

    def test_generic_import_is_real_deduplicated_and_scoped_not_a_native_replay(self):
        first = {"file": str(self.root / "Sources/A.swift"), "line": 1,
                 "rule_id": "identifier_name", "severity": "Warning", "reason": "Actual finding"}
        self.write("Tools/NotSelected.swift", "struct Other {}\n")
        other = {**first, "file": str(self.root / "Tools/NotSelected.swift"), "rule_id": "line_length"}
        self.findings = [first, first, other]
        self.lint_code = 2
        self.assertEqual(self.invoke()[0], 1)
        report = self.saved()
        self.assertEqual(report["lint"]["findings"], 3)
        self.assertEqual(report["lint"]["imported_findings"], 1)
        generic = json.loads((self.root / ".swift-sonar/analysis-issues.json").read_text())
        self.assertEqual(generic["issues"], [{"ruleId": "identifier_name", "primaryLocation": {
            "filePath": "Sources/A.swift", "message": "Actual finding", "textRange": {"startLine": 1}}}])
        self.assertEqual([rule["id"] for rule in generic["rules"]], ["identifier_name"])

    def test_missing_or_out_of_range_finding_source_stops_before_scanner(self):
        for path, line in (("Sources/Missing.swift", 1), ("Sources/A.swift", 2)):
            with self.subTest(path=path, line=line):
                self.findings = [{"file": str(self.root / path), "line": line,
                                  "rule_id": "line_length", "severity": "Warning", "reason": "Bad source"}]
                self.lint_code = 2
                self.assertEqual(self.invoke()[0], 2)
                self.server.assert_not_called()
                self.assertFalse(self.saved()["published"])

    def test_fresh_project_needs_no_recipe_generation_or_test_preparation(self):
        for name in ("swift-sonar.json", "sonar-project.properties"):
            (self.root / name).unlink()
        # The unknown Tools script has no Swift/Python sources and is not scope.
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(len(self.calls), 2)
        for name in ("swift-sonar.json", "sonar-project.properties", ".swiftlint.yml"):
            self.assertFalse((self.root / name).exists())
        self.assertEqual(self.saved()["project_name"], "Project With Spaces")

    def test_config_or_executable_change_during_lint_stops_before_publication(self):
        for stage in ("config", "tool"):
            with self.subTest(stage=stage):
                self.write("sonar-project.properties", self.originals["sonar-project.properties"].decode())
                self.mutate_at = stage
                self.assertEqual(self.invoke()[0], 2)
                self.server.assert_not_called()
                self.assertFalse(self.saved()["published"])

    def test_analysis_correction_offer_follows_saved_failed_scan_and_keeps_exit(self):
        self.findings = [{"file": str(self.root / "Sources/A.swift"), "line": 1,
                          "rule_id": "line_length", "severity": "Warning", "reason": "Too long"}]
        self.lint_code = 2
        def offered(root, findings, tool):
            self.assertEqual(root, self.root)
            self.assertEqual(findings, self.findings)
            self.assertEqual(tool["path"], self.setup["tools"]["swiftlint"]["path"])
            self.assertEqual(self.saved()["status"], "failed")
            self.assertTrue(self.saved()["published"])
            self.assertEqual(self.saved()["exit_code"], 1)
        with patch("swift_sonar_kit.corrections.offer_corrections", side_effect=offered) as offer, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", str(self.root)]), 1)
        offer.assert_called_once()
        self.assert_preserved()

    def test_interactive_browser_opens_only_after_result_is_saved(self):
        def opened(url, new):
            self.assertEqual(url, "http://localhost:43210/dashboard?id=Example")
            self.assertEqual(new, 2)
            self.assertTrue(self.saved()["published"])
            self.assertEqual(self.saved()["dashboard_url"], url)
            return True
        from swift_sonar_kit import dashboard
        self.interactive_browser = True
        with patch.dict(os.environ, {"CI": "", "NO_BROWSER": "", "JCODE_NO_BROWSER": ""}), \
                patch.object(dashboard.sys.stdin, "isatty", return_value=True), \
                patch.object(dashboard.webbrowser, "open", side_effect=opened) as browser:
            self.assertEqual(self.invoke()[0], 0)
            browser.assert_called_once()

    def test_browser_compatibility_modes_and_failures_do_not_open(self):
        from swift_sonar_kit import dashboard
        self.interactive_browser = True
        with patch.dict(os.environ, {"CI": "", "NO_BROWSER": "", "JCODE_NO_BROWSER": ""}), \
                patch.object(dashboard.sys.stdin, "isatty", return_value=True), \
                patch.object(dashboard.webbrowser, "open") as browser:
            for flags in (("--json",), ("--no-browser",), ("--plan-only",),
                          ("--setup-only",), ("--prepare-only",)):
                with self.subTest(flags=flags):
                    self.assertEqual(self.invoke(*flags)[0], 0)
                    browser.assert_not_called()
            self.scanner_code = 7
            self.assertEqual(self.invoke()[0], 1)
            browser.assert_not_called()

    def test_browser_failure_does_not_change_lint_failure_or_saved_json(self):
        from swift_sonar_kit import dashboard
        self.findings = [{"file": str(self.root / "Sources/A.swift"), "line": 1,
                          "rule_id": "line_length", "severity": "Warning", "reason": "Too long"}]
        self.lint_code = 2
        self.interactive_browser = True
        with patch.dict(os.environ, {"CI": "", "NO_BROWSER": "", "JCODE_NO_BROWSER": ""}), \
                patch.object(dashboard.sys.stdin, "isatty", return_value=True), \
                patch.object(dashboard.webbrowser, "open", side_effect=OSError("browser unavailable")) as browser:
            code, _, error = self.invoke()
        self.assertEqual(code, 1)
        browser.assert_called_once()
        self.assertIn("Could not open the browser", error)
        self.assertEqual(self.saved()["exit_code"], 1)
        self.assertEqual(self.saved()["status"], "failed")
        self.assertTrue(self.saved()["published"])


if __name__ == "__main__":
    unittest.main()
