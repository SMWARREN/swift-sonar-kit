"""Source-only analysis discovery tests using synthetic temporary Git repositories.

Every test forbids subprocesses other than read-only Git and asserts nothing is written.

SPDX-License-Identifier: GPL-3.0-only
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from swift_sonar_kit import analysis_config as ac
from swift_sonar_kit import project_config as pc
from swift_sonar_kit.bootstrap import DEFAULT_TOOLS, _resolve_tool_pin

REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen
ALLOWED_GIT = {"rev-parse", "ls-files"}


def guarded_popen(argv, *args, **kwargs):
    """Every process launch (subprocess.run included) must be a read-only Git query."""
    if not (isinstance(argv, list) and argv[:2] == ["git", "-C"] and argv[3] in ALLOWED_GIT):
        raise AssertionError(f"forbidden subprocess: {argv!r}")
    return REAL_POPEN(argv, *args, **kwargs)


def forbidden(*args, **kwargs):
    raise AssertionError("forbidden process launch")


def snapshot(root: Path) -> dict:
    result = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files + dirs:
            path = Path(current) / name
            result[str(path.relative_to(root))] = (path.read_bytes() if path.is_file() and not path.is_symlink()
                                                   else os.readlink(path) if path.is_symlink() else None)
    return result


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class AnalysisConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A path with spaces and no ".git" anywhere above it that we need.
        self.root = Path(os.path.realpath(self.tmp.name)) / "My Project"
        self.root.mkdir()
        REAL_RUN(["git", "init", "-q", str(self.root)], check=True)  # before the guard is installed
        patches = [mock.patch("subprocess.Popen", guarded_popen), mock.patch("os.system", forbidden),
                   mock.patch("os.posix_spawn", forbidden), mock.patch("os.execv", forbidden)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def discover(self, **kwargs):
        before = snapshot(self.root)
        result = ac.discover_analysis(self.root, **kwargs)
        self.assertEqual(snapshot(self.root), before, "discovery must not write")
        return result

    def recipe(self, properties: str, recipe: dict | None = None, path: str = "swift-sonar.json") -> None:
        write(self.root, "sonar-project.properties", properties)
        body = {"schema": 1, "properties": "sonar-project.properties",
                "test": {"argv": ["python3", "Tools/bomb.py"],
                         "artifacts": [{"kind": "xcresult", "path": "build/x.xcresult",
                                        "expectedTests": "Tools/missing-manifest.json"}]},
                "tools": dict(DEFAULT_TOOLS)}
        body.update(recipe or {})
        write(self.root, path, json.dumps(body))

    def test_default_tools_are_bootstrap_supported_pins_without_probes(self):
        write(self.root, "Sources/a.swift", "x\n")
        result = self.discover()
        self.assertEqual(result["tools"], DEFAULT_TOOLS)
        # Pin compatibility is for the supported target, not the test host.
        with mock.patch("sys.platform", "darwin"):
            for name in ("swiftlint", "sonarScanner"):
                self.assertEqual(_resolve_tool_pin(name, result["tools"][name])["version"].split(".")[:3],
                                 result["tools"][name].split(".")[:3])

    # ------------------------------------------------------------ recipes

    def test_recipe_preserves_scope_drops_only_reports_and_ignores_test_section(self):
        write(self.root, "Sources/App/main.swift", "print(1)\n")
        write(self.root, "Sources/Gen/gen.swift", "// generated\n")
        write(self.root, "Tests/AppTests/a_test.swift", "// t\n")
        write(self.root, "Tools/bomb.py", "raise SystemExit('the test script must never run')\n")
        props = ("sonar.projectKey=my-key\nsonar.projectName=My Name\nsonar.sources=Sources,Tools\n"
                 "sonar.tests=Tests\nsonar.test.inclusions=Tests/**/*.swift\nsonar.exclusions=Sources/Gen/**\n"
                 "sonar.coverage.exclusions=Tools/**\nsonar.python.version=3.12\n"
                 "sonar.apple.resultBundlePath=build/x.xcresult\n"
                 "sonar.coverageReportPaths=build/cov.xml\nsonar.python.coverage.reportPaths=build/py.xml\n")
        self.recipe(props)
        original = (self.root / "sonar-project.properties").read_bytes()
        result = self.discover()
        self.assertEqual((self.root / "sonar-project.properties").read_bytes(), original)
        self.assertEqual((result["schema"], result["status"], result["kind"]), (1, "ready", "analysis-only"))
        self.assertEqual(result["config_path"], "swift-sonar.json")
        self.assertEqual(result["omitted_report_properties"],
                         ["sonar.apple.resultBundlePath", "sonar.coverageReportPaths",
                          "sonar.python.coverage.reportPaths"])
        self.assertFalse(set(result["properties"]) & set(pc.REPORTS))
        for key in ("sonar.sources", "sonar.tests", "sonar.test.inclusions", "sonar.exclusions",
                    "sonar.coverage.exclusions", "sonar.python.version", "sonar.projectKey", "sonar.projectName"):
            self.assertEqual(result["properties"][key], pc.parse_properties(original)[key])
        self.assertEqual(result["project_key"], "my-key")
        self.assertEqual(result["project_name"], "My Name")
        self.assertEqual(result["inventory"]["sources"], ["Sources/App/main.swift", "Tools/bomb.py"])
        self.assertEqual(result["inventory"]["tests"], ["Tests/AppTests/a_test.swift"])
        self.assertEqual(result["tools"], DEFAULT_TOOLS)
        self.assertEqual(result["root"], str(self.root))
        json.dumps(result)

    def test_recipe_without_test_section_and_secondary_location_with_overlay_and_pins(self):
        write(self.root, "Sources/a.swift", "x\n")
        write(self.root, "Tools/props.properties", "sonar.projectKey=k\nsonar.sources=Sources\n")
        pins = {"swiftlint": "0.60.0", "sonarScanner": "8.0.1", "sonarApple": "0.5.0"}
        body = {"schema": 1, "properties": "Tools/props.properties", "tools": pins,
                "scannerProperties": {"sonar.coverageReportPaths": "stale.xml", "sonar.projectVersion": "9"}}
        write(self.root, "Tools/swift-sonar-project.json", json.dumps(body))
        result = self.discover()
        self.assertEqual(result["config_path"], "Tools/swift-sonar-project.json")
        self.assertEqual(result["tools"], pins)
        self.assertEqual(result["properties"]["sonar.projectVersion"], "9")
        self.assertEqual(result["omitted_report_properties"], ["sonar.coverageReportPaths"])
        self.assertEqual(result["project_name"], "My Project")

    def test_digest_binds_config_properties_sources_and_tools(self):
        write(self.root, "Sources/a.swift", "x\n")
        self.recipe("sonar.projectKey=k\nsonar.sources=Sources\n")
        first = self.discover()
        self.assertEqual(self.discover()["digest"], first["digest"])
        write(self.root, "Sources/a.swift", "y\n")
        second = self.discover()
        self.assertNotEqual(second["digest"], first["digest"])
        self.recipe("sonar.projectKey=k\nsonar.sources=Sources\nsonar.coverageReportPaths=a.xml\n")
        self.assertNotEqual(self.discover()["digest"], second["digest"])
        self.recipe("sonar.projectKey=k\nsonar.sources=Sources\n", {"tools": {**DEFAULT_TOOLS, "swiftlint": "0.1"}})
        self.assertNotEqual(self.discover()["digest"], second["digest"])

    def test_recipe_shape_lint_and_tool_failures(self):
        write(self.root, "Sources/a.swift", "x\n")
        props = "sonar.projectKey=k\nsonar.sources=Sources\n"
        cases = [({"lint": {"argv": ["swiftlint", "lint"]}}, "lint.argv"),
                 ({"lint": {"import": "external"}}, "lint.import"),
                 ({"tools": {"swiftlint": "0.1"}}, "missing required"),
                 ({"tools": {**DEFAULT_TOOLS, "swiftlint": "latest"}}, "dotted numeric"),
                 ({"extra": 1}, "unsupported fields"),
                 ({"schema": True}, "schema"),
                 ({"scannerProperties": {"sonar.host.url": "http://x"}}, "trusted invocation"),
                 ({"scannerProperties": {"sonar.unknown": "x"}}, "supported registry")]
        for override, message in cases:
            with self.subTest(override=override):
                self.recipe(props, override)
                with self.assertRaisesRegex(ValueError, message):
                    ac.discover_analysis(self.root)

    def test_duplicate_recipe_key_and_missing_properties_file_fail(self):
        write(self.root, "Sources/a.swift", "x\n")
        write(self.root, "swift-sonar.json", '{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ac.discover_analysis(self.root)
        write(self.root, "swift-sonar.json", json.dumps(
            {"schema": 1, "properties": "nope.properties", "tools": dict(DEFAULT_TOOLS)}))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ac.discover_analysis(self.root)

    # ------------------------------------------------------------ direct properties

    def test_direct_properties_without_recipe_with_overrides(self):
        write(self.root, "Core/a.py", "x = 1\n")
        write(self.root, "sonar-project.properties",
              "sonar.projectKey=orig\nsonar.sources=Core\nsonar.coverageReportPaths=c.xml\n")
        result = self.discover(project_key="over-key", project_name="Over Name")
        self.assertEqual(result["config_path"], "sonar-project.properties")
        self.assertEqual(result["tools"], DEFAULT_TOOLS)
        self.assertEqual((result["project_key"], result["project_name"]), ("over-key", "Over Name"))
        self.assertEqual(result["omitted_report_properties"], ["sonar.coverageReportPaths"])
        self.assertEqual(result["inventory"]["sources"], ["Core/a.py"])

    def test_explicit_config_selects_named_recipe_and_rejects_outside_paths(self):
        write(self.root, "Sources/a.swift", "x\n")
        self.recipe("sonar.projectKey=k\nsonar.sources=Sources\n", path="alt config.json")
        result = self.discover(config=Path("alt config.json"))
        self.assertEqual(result["config_path"], "alt config.json")
        self.assertEqual(self.discover(config=self.root / "alt config.json")["config_path"], "alt config.json")
        for bad in (Path("../x.json"), Path("/etc/passwd"), Path("missing.json")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ac.discover_analysis(self.root, config=bad)

    def test_unsafe_properties_fail_closed(self):
        write(self.root, "Sources/a.swift", "x\n")
        bad = ["sonar.sources=Sources\nsonar.token=abc\n", "sonar.sources=Sources\nsonar.host.url=http://x\n",
               "sonar.sources=Sources\nsonar.mystery=1\n", "sonar.sources=Sources\nbad key!=1\n",
               "sonar.sources=Sources\nsonar.projectName=a\\u0001b\n", "sonar.sources=Sources\nsonar.projectKey=${X}\n",
               "sonar.sources=../outside\n", "sonar.sources=/abs\n", "sonar.sources=Sources\nsonar.exclusions=[a]\n",
               "sonar.sources=Sources\nsonar.projectKey=12345\n", "sonar.sources=Sources\nsonar.sources=Core\n",
               "sonar.projectKey=k\n"]
        for text in bad:
            with self.subTest(text=text):
                write(self.root, "sonar-project.properties", text)
                with self.assertRaises(ValueError):
                    ac.discover_analysis(self.root)

    def test_symlinked_config_properties_and_source_fail_closed(self):
        write(self.root, "Sources/a.swift", "x\n")
        outside = Path(self.tmp.name) / "outside.properties"
        outside.write_text("sonar.sources=Sources\n")
        (self.root / "sonar-project.properties").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            ac.discover_analysis(self.root)
        (self.root / "sonar-project.properties").unlink()
        self.recipe("sonar.projectKey=k\nsonar.sources=Sources\n")
        (self.root / "sonar-project.properties").unlink()
        (self.root / "sonar-project.properties").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            ac.discover_analysis(self.root)
        (self.root / "sonar-project.properties").unlink()
        write(self.root, "sonar-project.properties", "sonar.projectKey=k\nsonar.sources=Sources\n")
        (self.root / "Sources/link.swift").symlink_to(Path(self.tmp.name) / "outside.properties")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            ac.discover_analysis(self.root)

    def test_symlinked_project_root_component_and_non_git_root_fail(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.root)
        write(self.root, "Sources/a.swift", "x\n")
        self.assertEqual(ac.discover_analysis(link)["root"], str(self.root))  # canonical, like project mode
        plain = Path(self.tmp.name) / "plain"
        write(plain, "Sources/a.swift", "x\n")
        with self.assertRaises(ValueError):
            ac.discover_analysis(plain)
        with self.assertRaisesRegex(ValueError, "Git repository root"):
            ac.discover_analysis(self.root / "Sources")

    def test_git_ignored_generated_sources_are_not_in_inventory(self):
        write(self.root, "Sources/a.swift", "x\n")
        write(self.root, "Sources/Generated/g.swift", "x\n")
        write(self.root, ".gitignore", "Sources/Generated/\n")
        write(self.root, "sonar-project.properties", "sonar.projectKey=k\nsonar.sources=Sources\n")
        self.assertEqual(self.discover()["inventory"]["sources"], ["Sources/a.swift"])

    # ------------------------------------------------------------ fresh folders

    def test_fresh_swiftpm_detects_sources_and_tests_without_enumeration(self):
        write(self.root, "Package.swift", 'let package = Package(name: "Widget Kit")\n')
        write(self.root, "Sources/Widget/w.swift", "x\n")
        write(self.root, "Scripts/tool.py", "x = 1\n")
        write(self.root, "Tests/WidgetTests/t.swift", "x\n")
        write(self.root, "Extras/readme.md", "ignored non source\n")
        result = self.discover()
        self.assertIsNone(result["config_path"])
        self.assertEqual(result["project_name"], "Widget Kit")
        self.assertEqual(result["project_key"], "Widget-Kit")
        self.assertEqual(result["properties"]["sonar.sources"], "Scripts,Sources")
        self.assertEqual(result["properties"]["sonar.tests"], "Tests")
        self.assertEqual(result["inventory"]["sources"], ["Scripts/tool.py", "Sources/Widget/w.swift"])
        self.assertEqual(result["inventory"]["tests"], ["Tests/WidgetTests/t.swift"])
        self.assertEqual(result["omitted_report_properties"], [])
        self.assertNotIn("HashFleet", json.dumps(result))
        self.assertFalse((self.root / "swift-sonar.json").exists())

    def test_fresh_xcode_uses_directory_conventions_without_scheme_or_test_enumeration(self):
        (self.root / "Fancy.xcodeproj").mkdir()
        write(self.root, "Fancy.xcodeproj/project.pbxproj", "// pbx\n")
        write(self.root, "App/main.swift", "x\n")
        write(self.root, "Shared/s.swift", "x\n")
        write(self.root, "Fancy/f.swift", "x\n")
        write(self.root, "AppTests/a.swift", "x\n")
        write(self.root, "UITests/u.swift", "x\n")
        write(self.root, "FancyIntegrationTests/i.swift", "x\n")
        result = self.discover()
        self.assertEqual(result["project_name"], "Fancy")
        self.assertEqual(result["properties"]["sonar.sources"], "App,Fancy,Shared")
        self.assertEqual(result["properties"]["sonar.tests"], "AppTests,FancyIntegrationTests,UITests")
        self.assertEqual(len(result["inventory"]["tests"]), 3)

    def test_fresh_ambiguous_or_empty_layouts_fail_without_dot_guess(self):
        with self.assertRaisesRegex(ValueError, "No conventional"):
            ac.discover_analysis(self.root)
        write(self.root, "Mystery/a.swift", "x\n")
        write(self.root, "Sources/a.swift", "x\n")
        with self.assertRaisesRegex(ValueError, "unrecognized top-level"):
            ac.discover_analysis(self.root)
        (self.root / "Mystery/a.swift").unlink()
        write(self.root, "stray.py", "x\n")
        with self.assertRaisesRegex(ValueError, "root need a reviewed"):
            ac.discover_analysis(self.root)
        (self.root / "stray.py").unlink()
        shutil_dir = self.root / "Tests"
        write(shutil_dir, "t.swift", "x\n")
        (self.root / "Sources/a.swift").unlink()
        with self.assertRaisesRegex(ValueError, "No conventional"):
            ac.discover_analysis(self.root)

    def test_overrides_for_fresh_folder_and_invalid_overrides(self):
        write(self.root, "Sources/a.swift", "x\n")
        result = self.discover(project_key="custom.key", project_name="Custom")
        self.assertEqual((result["project_key"], result["project_name"]), ("custom.key", "Custom"))
        for key in ("123", "bad key", "a/b", ""):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ac.discover_analysis(self.root, project_key=key or "\n")

    def test_no_process_launch_beyond_git_and_nothing_written_on_failure(self):
        write(self.root, "Sources/a.swift", "x\n")
        write(self.root, "sonar-project.properties", "sonar.projectKey=k\nsonar.sources=Sources\nsonar.token=x\n")
        before = snapshot(self.root)
        with self.assertRaises(ValueError):
            ac.discover_analysis(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".gitignore").exists() or (self.root / ".swiftlint.yml").exists())


if __name__ == "__main__":
    unittest.main()
