"""Tests for path-only portable audit discovery and automatic preparation.

SPDX-License-Identifier: GPL-3.0-only
"""

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from swift_sonar_kit import automatic as auto
from swift_sonar_kit.project_config import load_project


class BaseAutomaticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swift-sonar-test-"))
        self.root = self.tmp / "test repo"
        self.root.mkdir(parents=True, exist_ok=True)
        # Initialize git repo in root
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, content: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def git_commit(self):
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "init", "--allow-empty"], check=True)


class DeterministicNamingTests(BaseAutomaticTest):
    def test_naming_from_package_swift(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "AcmeTools")\n')
        key, name = auto.deterministic_naming(self.root)
        self.assertEqual(name, "AcmeTools")
        self.assertEqual(key, "AcmeTools")
        self.assertNotIn("HashFleet", key)
        self.assertNotIn("HashFleet", name)

    def test_naming_from_directory_if_no_package(self):
        key, name = auto.deterministic_naming(self.root)
        self.assertEqual(name, "test repo")
        self.assertEqual(key, "test-repo")

    def test_naming_numeric_prefix(self):
        num_dir = self.tmp / "12345"
        num_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init", "-q", str(num_dir)], check=True)
        key, name = auto.deterministic_naming(num_dir)
        self.assertEqual(key, "project-12345")
        self.assertFalse(key.isdigit())

    def test_explicit_overrides_respected(self):
        self.write("Package.swift", 'let package = Package(name: "DefaultName")\n')
        key, name = auto.deterministic_naming(
            self.root,
            project_key="custom-key-123",
            project_name="Custom Display Name",
        )
        self.assertEqual(key, "custom-key-123")
        self.assertEqual(name, "Custom Display Name")

    def test_invalid_key_rejected(self):
        with self.assertRaises(ValueError):
            auto.deterministic_naming(self.root, project_key="bad key with spaces")
        with self.assertRaises(ValueError):
            auto.deterministic_naming(self.root, project_key="12345")  # all digits rejected by Sonar


class PathAttacksAndSafetyTests(BaseAutomaticTest):
    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            auto.discover(self.root, config="../outside.json")

    def test_absolute_config_path_rejected(self):
        with self.assertRaises(ValueError):
            auto.discover(self.root, config="/etc/passwd")

    def test_control_character_in_path_rejected(self):
        with self.assertRaises(ValueError):
            auto.discover(self.root, config="bad\npath.json")

    def test_symlink_candidate_recipe_fails_closed(self):
        outside = self.tmp / "outside-recipe.json"
        outside.write_text("{}", encoding="utf-8")
        link = self.root / "swift-sonar.json"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            auto.discover(self.root)


class ExistingRecipePreservationTests(BaseAutomaticTest):
    def test_existing_swift_sonar_json_preserved(self):
        self.write("Sources/App.swift", "public struct App {}\n")
        self.write("Tests/AppTests.swift", "import XCTest\n")
        self.write(
            "sonar-project.properties",
            "sonar.projectKey=reviewed-key\nsonar.projectName=Reviewed App\nsonar.sources=Sources\nsonar.tests=Tests\n",
        )
        self.write(
            "swift-sonar.json",
            json.dumps({
                "schema": 1,
                "properties": "sonar-project.properties",
                "scannerProperties": {},
                "test": {
                    "argv": ["python3", "Tools/run_tests.py"],
                    "artifacts": [{"kind": "file", "path": "build/report.txt"}],
                },
                "lint": {
                    "argv": ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"],
                    "import": "native",
                },
                "tools": {
                    "swiftlint": "0.65.1",
                    "sonarScanner": "8.3.0",
                    "sonarApple": "0.5.1",
                },
            }),
        )
        self.git_commit()

        plan = auto.discover(self.root)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["kind"], "existing")
        self.assertEqual(plan["project_key"], "reviewed-key")
        self.assertEqual(plan["project_name"], "Reviewed App")
        self.assertEqual(plan["config_path"], Path("swift-sonar.json"))

        prepared = auto.prepare(self.root, plan=plan)
        self.assertEqual(prepared["status"], "ready")
        self.assertEqual(prepared["config_path"], Path("swift-sonar.json"))
        # Verify file was not overwritten
        content = (self.root / "swift-sonar.json").read_text(encoding="utf-8")
        self.assertIn("Tools/run_tests.py", content)

    def test_existing_tools_swift_sonar_project_json_preserved(self):
        self.write("Sources/App.swift", "public struct App {}\n")
        self.write(
            "sonar-project.properties",
            "sonar.projectKey=tools-key\nsonar.projectName=Tools Project\nsonar.sources=Sources\n",
        )
        self.write(
            "Tools/swift-sonar-project.json",
            json.dumps({
                "schema": 1,
                "properties": "sonar-project.properties",
                "scannerProperties": {},
                "test": {
                    "argv": ["python3", "Tools/run_tests.py"],
                    "artifacts": [{"kind": "file", "path": "build/report.txt"}],
                },
                "lint": {
                    "argv": ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"],
                    "import": "native",
                },
                "tools": {
                    "swiftlint": "0.65.1",
                    "sonarScanner": "8.3.0",
                    "sonarApple": "0.5.1",
                },
            }),
        )
        self.git_commit()

        plan = auto.discover(self.root)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["kind"], "existing")
        self.assertEqual(plan["config_path"], Path("Tools/swift-sonar-project.json"))


class FreshSwiftPMTests(BaseAutomaticTest):
    def test_fresh_swiftpm_discovery_and_preparation(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "PortableLib")\n')
        self.write("Sources/PortableLib/PortableLib.swift", "public struct PortableLib { public init() {} }\n")
        self.write("Tests/PortableLibTests/PortableLibTests.swift", "import XCTest\n")
        self.git_commit()

        plan = auto.discover(self.root)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["kind"], "swiftpm")
        self.assertEqual(plan["project_key"], "PortableLib")
        self.assertEqual(plan["project_name"], "PortableLib")
        self.assertEqual(plan["sources"], ["Sources"])
        self.assertEqual(plan["tests"], ["Tests"])

        prepared = auto.prepare(self.root, plan=plan)
        self.assertEqual(prepared["status"], "ready")
        self.assertTrue((self.root / "swift-sonar.json").is_file())
        self.assertTrue((self.root / "sonar-project.properties").is_file())

        # Verify that generated project passes strict load_project validation
        loaded = load_project(self.root, prepared["config_path"])
        self.assertEqual(loaded["schema"], 1)
        self.assertEqual(loaded["properties"]["sonar.projectKey"], "PortableLib")
        self.assertEqual(loaded["properties"]["sonar.sources"], "Sources")
        self.assertEqual(loaded["properties"]["sonar.coverageReportPaths"], "build/sonar/coverage.xml")
        self.assertEqual(loaded["test"]["artifacts"][0]["kind"], "generic-coverage")

    def test_swiftpm_execute_generates_valid_coverage(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "ExecLib")\n')
        self.write("Sources/ExecLib/File.swift", "public struct File {\n  public func hello() -> String {\n    return \"hi\"\n  }\n}\n")
        self.write("Tests/ExecLibTests/FileTests.swift", "import XCTest\n")
        self.git_commit()

        # Create dummy profdata and binary in dedicated scratch path
        scratch_path = self.root / "build/sonar/swiftpm"
        scratch_path.mkdir(parents=True, exist_ok=True)
        profdata = scratch_path / "default.profdata"
        profdata.write_bytes(b"dummy profdata")
        bin_dir = scratch_path / "debug"
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary = bin_dir / "ExecLibPackageTests"
        binary.write_bytes(b"dummy binary")
        os.chmod(binary, 0o755)

        lcov_sample = "SF:Sources/ExecLib/File.swift\nDA:1,1\nDA:2,0\nend_of_record\n"

        def fake_runner(cmd, cwd):
            if "--show-codecov-path" in cmd:
                return 0, str(scratch_path / "codecov/ExecLib.json"), ""
            if "--show-bin-path" in cmd:
                return 0, str(bin_dir), ""
            if "llvm-cov" in cmd:
                return 0, lcov_sample, ""
            if "test" in cmd:
                return 0, "Test Suite 'All tests' passed", ""
            return 1, "", "unsupported"

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=fake_runner)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / cov_path).is_file())
        xml_content = (self.root / cov_path).read_text(encoding="utf-8")
        self.assertIn('<coverage version="1">', xml_content)
        self.assertIn('<file path="Sources/ExecLib/File.swift">', xml_content)
        self.assertIn('covered="true"', xml_content)
        self.assertIn('covered="false"', xml_content)

    def test_missing_profdata_fails_nonzero_without_coverage(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "NoProfLib")\n')
        self.write("Sources/NoProfLib/File.swift", "public struct File {}\n")
        self.git_commit()

        scratch_path = self.root / "build/sonar/swiftpm"
        scratch_path.mkdir(parents=True, exist_ok=True)

        def no_prof_runner(cmd, cwd):
            if "--show-codecov-path" in cmd:
                return 0, str(scratch_path / "codecov/NoProfLib.json"), ""
            if "test" in cmd:
                return 0, "All tests passed", ""
            return 1, "", ""

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=no_prof_runner)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / cov_path).exists())
        failure_json = self.root / ".swift-sonar/test-failure.json"
        self.assertTrue(failure_json.is_file())
        self.assertIn("Missing fresh default.profdata", failure_json.read_text(encoding="utf-8"))

    def test_llvm_cov_export_failure_fails_nonzero(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "FailedExportLib")\n')
        self.write("Sources/FailedExportLib/File.swift", "public struct File {}\n")
        self.git_commit()

        scratch_path = self.root / "build/sonar/swiftpm"
        codecov_dir = scratch_path / "codecov"
        codecov_dir.mkdir(parents=True, exist_ok=True)
        (codecov_dir / "default.profdata").write_bytes(b"dummy")
        bin_dir = scratch_path / "debug"
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary = bin_dir / "FailedExportLibPackageTests"
        binary.write_bytes(b"dummy")
        os.chmod(binary, 0o755)

        def export_fail_runner(cmd, cwd):
            if "--show-codecov-path" in cmd:
                return 0, str(codecov_dir / "FailedExportLib.json"), ""
            if "--show-bin-path" in cmd:
                return 0, str(bin_dir), ""
            if "llvm-cov" in cmd:
                return 1, "", "llvm-cov error: broken profile"
            if "test" in cmd:
                return 0, "Test Suite passed", ""
            return 1, "", ""

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=export_fail_runner)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / cov_path).exists())
        failure_json = self.root / ".swift-sonar/test-failure.json"
        self.assertTrue(failure_json.is_file())
        self.assertIn("llvm-cov export failed", failure_json.read_text(encoding="utf-8"))

    def test_stale_profile_fails_nonzero(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "StaleProfLib")\n')
        self.write("Sources/StaleProfLib/File.swift", "public struct File {}\n")
        self.git_commit()

        scratch_path = self.root / "build/sonar/swiftpm"
        codecov_dir = scratch_path / "codecov"
        codecov_dir.mkdir(parents=True, exist_ok=True)
        profdata = codecov_dir / "default.profdata"
        profdata.write_bytes(b"stale profdata")
        past_time = time.time() - 3600
        os.utime(profdata, (past_time, past_time))

        def stale_runner(cmd, cwd):
            if "--show-codecov-path" in cmd:
                return 0, str(codecov_dir / "StaleProfLib.json"), ""
            if "test" in cmd:
                return 0, "All tests passed", ""
            return 1, "", ""

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=stale_runner)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / cov_path).exists())
        failure_json = self.root / ".swift-sonar/test-failure.json"
        self.assertTrue(failure_json.is_file())
        self.assertIn("Stale code coverage profile detected", failure_json.read_text(encoding="utf-8"))

    def test_failed_show_bin_path_fails_nonzero(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "BinFailLib")\n')
        self.write("Sources/BinFailLib/File.swift", "public struct File {}\n")
        self.git_commit()

        scratch_path = self.root / "build/sonar/swiftpm"
        codecov_dir = scratch_path / "codecov"
        codecov_dir.mkdir(parents=True, exist_ok=True)
        profdata = codecov_dir / "default.profdata"
        profdata.write_bytes(b"fresh profdata")

        def bin_fail_runner(cmd, cwd):
            if "--show-codecov-path" in cmd:
                return 0, str(codecov_dir / "BinFailLib.json"), ""
            if "--show-bin-path" in cmd:
                return 1, "", "failed show bin path"
            if "test" in cmd:
                return 0, "All tests passed", ""
            return 1, "", ""

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=bin_fail_runner)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / cov_path).exists())
        failure_json = self.root / ".swift-sonar/test-failure.json"
        self.assertTrue(failure_json.is_file())
        self.assertIn("Failed to query build binary path", failure_json.read_text(encoding="utf-8"))

    def test_swiftpm_test_failure_preserves_evidence(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "FailingLib")\n')
        self.write("Sources/FailingLib/Failing.swift", "public struct Failing {}\n")
        self.git_commit()

        def failing_runner(cmd, cwd):
            return 2, "Test suite failed", "Fatal error: test failed"

        cov_path = Path("build/sonar/coverage.xml")
        code = auto.execute_swiftpm_tests_and_coverage(self.root, ["Sources"], cov_path, runner=failing_runner)
        self.assertEqual(code, 2)
        # Check preserved evidence
        failure_json = self.root / ".swift-sonar/test-failure.json"
        self.assertTrue(failure_json.is_file())
        data = json.loads(failure_json.read_text(encoding="utf-8"))
        self.assertEqual(data["exit_code"], 2)
        self.assertIn("Fatal error", data["stderr"])


class FreshXcodeTests(BaseAutomaticTest):
    def test_fresh_xcode_discovery_and_enumeration(self):
        self.write("MyProject.xcodeproj/project.pbxproj", "// PBX project\n")
        self.write("Sources/App.swift", "struct App {}\n")
        self.git_commit()

        def fake_runner(cmd, cwd):
            if "-list" in cmd:
                return 0, json.dumps({"project": {"schemes": ["MyProject"], "targets": ["MyProject"]}}), ""
            if "-enumerate-tests" in cmd:
                return 0, json.dumps({
                    "values": [
                        {
                            "enabledTests": [
                                {"identifier": "MyProjectTests/testAlpha()"},
                                {"identifier": "MyProjectTests/testBeta()"},
                            ]
                        }
                    ]
                }), ""
            return 1, "", "unsupported"

        plan = auto.discover(self.root)
        self.assertEqual(plan["kind"], "xcode")
        self.assertEqual(plan["status"], "ready")

        prepared = auto.prepare(self.root, plan=plan, runner=fake_runner)
        self.assertEqual(prepared["status"], "ready")

        # Verify expected tests manifest was created independently BEFORE running tests
        manifest_path = self.root / "Tools/expected-native-tests.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest, ["MyProjectTests/testAlpha()", "MyProjectTests/testBeta()"])

        # Verify load_project accepts generated recipe and xcresult artifact
        loaded = load_project(self.root, prepared["config_path"])
        self.assertEqual(loaded["schema"], 1)
        self.assertEqual(loaded["properties"]["sonar.apple.resultBundlePath"], "build/audit/native.xcresult")
        self.assertEqual(loaded["test"]["artifacts"][0]["kind"], "xcresult")
        self.assertEqual(loaded["test"]["artifacts"][0]["expectedTests"], "Tools/expected-native-tests.json")

    def test_ambiguous_xcode_schemes_fail_cleanly(self):
        self.write("Ambiguous.xcodeproj/project.pbxproj", "// PBX project\n")
        self.git_commit()

        def ambiguous_runner(cmd, cwd):
            if "-list" in cmd:
                return 0, json.dumps({"project": {"schemes": ["AppOne", "AppTwo"]}}), ""
            return 1, "", ""

        plan = auto.discover(self.root)
        with self.assertRaisesRegex(ValueError, "Ambiguous Xcode schemes found"):
            auto.prepare(self.root, plan=plan, runner=ambiguous_runner)


class OverwriteProtectionTests(BaseAutomaticTest):
    def test_safe_write_race_condition_protection(self):
        target = self.root / "protected.txt"
        target.write_text("original content", encoding="utf-8")
        with self.assertRaises(ValueError):
            auto._safe_write(target, "overwritten content", allow_overwrite=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "original content")

    def test_unbound_properties_requires_review_and_prevents_narrowing(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "NoOverwrite")\n')
        self.write("Sources/App.swift", "struct App {}\n")
        self.write("sonar-project.properties", "# Existing User Properties\nsonar.projectKey=user-key\nsonar.sources=Sources\n")
        self.git_commit()

        plan = auto.discover(self.root)
        self.assertEqual(plan["status"], "review_needed")
        self.assertIn("Repository contains reviewed sonar-project.properties but no swift-sonar.json recipe", plan["reason"])

        with self.assertRaises(ValueError):
            auto.prepare(self.root, plan=plan)

        # Original sonar-project.properties must be completely intact
        original = (self.root / "sonar-project.properties").read_text(encoding="utf-8")
        self.assertIn("# Existing User Properties", original)


class IntegrationAndEdgeCaseTests(BaseAutomaticTest):
    def test_swiftpm_multiple_source_targets_detected(self):
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "MultiLib")\n')
        self.write("Sources/App/App.swift", "public struct App {}\n")
        self.write("Core/Core.swift", "public struct Core {}\n")
        self.write("Tests/AppTests/AppTests.swift", "import XCTest\n")
        self.git_commit()

        sources, tests = auto.detect_swiftpm_sources(self.root)
        self.assertEqual(sources, ["Core", "Sources"])
        self.assertEqual(tests, ["Tests"])

    def test_unsupported_project_returns_unsupported_status(self):
        # Empty repo with no SwiftPM or Xcode
        self.write("README.md", "# Just a readme\n")
        self.git_commit()
        plan = auto.discover(self.root)
        self.assertEqual(plan["status"], "unsupported")
        self.assertEqual(plan["kind"], "unsupported")
        self.assertIn("No supported project manifest found", plan["reason"])

        with self.assertRaises(ValueError):
            auto.prepare(self.root, plan=plan)

    def test_tool_version_parsing_and_overrides(self):
        tools = auto.inspect_tool_versions(
            self.root,
            overrides={"swiftlint": "0.65.1.0", "sonarScanner": "8.3.1.16491", "sonarApple": "0.5.1"},
        )
        self.assertEqual(tools["swiftlint"], "0.65.1.0")
        self.assertEqual(tools["sonarScanner"], "8.3.1.16491")
        self.assertEqual(tools["sonarApple"], "0.5.1")

    @patch("swift_sonar_kit.automatic.execute_swiftpm_tests_and_coverage")
    def test_cli_main_execute_entrypoint(self, mock_exec):
        mock_exec.return_value = 0
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "CLILib")\n')
        self.write("Sources/CLILib/File.swift", "public struct File {}\n")
        self.git_commit()

        code = auto.main(["execute", "--root", str(self.root)])
        self.assertEqual(code, 0)
        mock_exec.assert_called_once()

    def test_workflow_plan_only_integration(self):
        import argparse
        from swift_sonar_kit import workflow
        self.write("Package.swift", '// swift-tools-version: 5.9\nlet package = Package(name: "WorkflowLib")\n')
        self.write("Sources/WorkflowLib/Lib.swift", "public struct Lib {}\n")
        self.git_commit()

        args = argparse.Namespace(
            root=self.root,
            config=None,
            project_key=None,
            project_name=None,
            url="http://localhost:9000",
            max_test_age=3600,
            plan_only=True,
            prepare_only=True,
            publish_failed_tests=False,
            no_archive=True,
        )
        code = workflow.run(args)
        self.assertEqual(code, 0)

    def test_paths_with_spaces_in_root_and_sources(self):
        space_root = self.tmp / "path with multiple spaces"
        space_root.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init", "-q", str(space_root)], check=True)
        subprocess.run(["git", "-C", str(space_root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(space_root), "config", "user.email", "test@example.com"], check=True)

        p_swift = space_root / "Package.swift"
        p_swift.write_text('// swift-tools-version: 5.9\nlet package = Package(name: "SpaceLib")\n', encoding="utf-8")
        src = space_root / "Sources/SpaceLib/Space.swift"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('public struct Space {}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(space_root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(space_root), "commit", "-q", "-m", "init"], check=True)

        plan = auto.discover(space_root)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["project_key"], "SpaceLib")

        prepared = auto.prepare(space_root, plan=plan)
        self.assertEqual(prepared["status"], "ready")
        self.assertTrue((space_root / "swift-sonar.json").is_file())


class RealSwiftPMAcceptanceTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("SWIFT_SONAR_RUN_REAL_TESTS") != "1" or not shutil.which("swift") or not shutil.which("swiftlint"):
            self.skipTest("Real SwiftPM acceptance requires SWIFT_SONAR_RUN_REAL_TESTS=1 and swift/swiftlint installed")
        scratch_dir = os.environ.get("JCODE_SCRATCH_DIR") or tempfile.gettempdir()
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="acceptance spaces ", dir=scratch_dir)
        self.repo = Path(self.tmp_dir.name) / "acceptance test spaces repo"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_real_swiftpm_end_to_end_acceptance(self):
        import subprocess
        # 1. SwiftPM Package.swift
        (self.repo / "Package.swift").write_text("""// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "RealAcceptanceLib",
    products: [
        .library(name: "RealAcceptanceLib", targets: ["RealAcceptanceLib"])
    ],
    targets: [
        .target(name: "RealAcceptanceLib"),
        .testTarget(name: "RealAcceptanceLibTests", dependencies: ["RealAcceptanceLib"])
    ]
)
""")

        src = self.repo / "Sources/RealAcceptanceLib/RealAcceptanceLib.swift"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("""public struct RealAcceptanceLib {
    public init() {}
    public func calculate(value: Int) -> Int {
        if value > 0 {
            return value * 10
        }
        return -1
    }
}
""")

        test_file = self.repo / "Tests/RealAcceptanceLibTests/RealAcceptanceLibTests.swift"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("""import XCTest
@testable import RealAcceptanceLib

final class RealAcceptanceLibTests: XCTestCase {
    func testCalc() {
        let lib = RealAcceptanceLib()
        XCTAssertEqual(lib.calculate(value: 3), 30)
    }
}
""")

        gitignore = self.repo / ".gitignore"
        gitignore.write_text(".build/\nbuild/\n.swift-sonar/\n.scannerwork/\n")

        swiftlint_yml = self.repo / ".swiftlint.yml"
        swiftlint_yml.write_text("excluded:\n  - .build\n  - build\n  - .swift-sonar\n")

        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Acceptance"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "acceptance@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "Initial commit"], cwd=self.repo, check=True)

        from swift_sonar_kit import project_runner
        from swift_sonar_kit import workflow

        # 2. Discover
        plan = auto.discover(self.repo)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["kind"], "swiftpm")
        self.assertEqual(plan["project_key"], "RealAcceptanceLib")
        self.assertEqual(plan["sources"], ["Sources"])
        self.assertEqual(plan["tests"], ["Tests"])

        # 3. Prepare
        prepared = auto.prepare(self.repo, plan=plan)
        self.assertEqual(prepared["status"], "ready")
        config_path = prepared["config_path"]

        # 4. Run real project_test
        test_code = project_runner.project_test(self.repo, config_path)
        self.assertEqual(test_code, 0)

        cov_file = self.repo / "build/sonar/coverage.xml"
        self.assertTrue(cov_file.is_file())
        self.assertGreater(cov_file.stat().st_size, 0)

        # Verify real coverage produced both covered and uncovered lines from llvm-cov
        import xml.etree.ElementTree as ET
        cov_xml = cov_file.read_text(encoding="utf-8")
        self.assertIn('<coverage version="1">', cov_xml)
        root_elem = ET.fromstring(cov_xml)
        file_elem = root_elem.find("file[@path='Sources/RealAcceptanceLib/RealAcceptanceLib.swift']")
        self.assertIsNotNone(file_elem)
        line_hits = {int(line.get("lineNumber")): line.get("covered") for line in file_elem.findall("lineToCover")}
        self.assertEqual(line_hits.get(5), "true")   # return value * 10 is executed
        self.assertEqual(line_hits.get(7), "false")  # return -1 is the exact known unexecuted branch

        # 5. Run real project_scan prepare_only
        scan_code = project_runner.project_scan(self.repo, config_path, prepare_only=True)
        self.assertEqual(scan_code, 0)
        self.assertTrue((self.repo / ".swift-sonar/project-plan.json").is_file())

        # 6. Test repeat run with archive_previous_outputs
        archive = workflow.archive_previous_outputs(self.repo, config_path)
        self.assertIsNotNone(archive)
        repeat_test_code = project_runner.project_test(self.repo, config_path)
        self.assertEqual(repeat_test_code, 0)


if __name__ == "__main__":
    unittest.main()
