"""Strict recipe loader, properties codec, safe paths and scope inventory tests.

SPDX-License-Identifier: GPL-3.0-only
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from swift_sonar_kit import project_config as pc


DEFAULT_LINT = ["swiftlint", "lint", "--quiet", "--strict", "--reporter", "json"]


class PropertiesCodecTests(unittest.TestCase):
    def test_continuations_escapes_separators_and_unicode(self):
        data = (b"# comment line\n! bang comment\n"
                b"a.b = first\\\n  second\n"      # logical-line continuation, leading space stripped
                b"key\\:with\\=sep : value=kept\n"  # escaped separators in key, raw = in value tail
                b"accent = caf\\u00e9\n"
                b"spaced = x\\ y\n"              # escaped space is preserved in a value
                b"trailing = v\\\\\n")             # even backslashes do not continue
        parsed = pc.parse_properties(data)
        self.assertEqual(parsed["a.b"], "firstsecond")
        self.assertEqual(parsed["key:with=sep"], "value=kept")
        self.assertEqual(parsed["accent"], "caf\u00e9")
        self.assertEqual(parsed["spaced"], "x y")
        self.assertEqual(parsed["trailing"], "v\\")

    def test_serialize_round_trips_and_escapes_ascii(self):
        original = {"sonar.projectName": "Example Project", "a:b=c": "x y", "u": "\u00e9\u4e2d"}
        text = pc.serialize_properties(original)
        self.assertTrue(all(ord(c) < 128 for c in text))  # ASCII only
        self.assertEqual(pc.parse_properties(text.encode()), original)

    def test_full_consumer_properties_parse_and_classify(self):
        path = Path(__file__).parent / "fixtures/consumer-project.properties"
        parsed = pc.parse_properties(path.read_bytes())
        self.assertEqual(parsed["sonar.python.version"], "3.14")
        self.assertIn("App", parsed["sonar.sources"].split(","))
        for key, value in parsed.items():
            pc._classify(key, value, "Baseline")
        self.assertEqual(pc.parse_properties(pc.serialize_properties(parsed).encode()), parsed)

    def test_duplicate_decoded_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate decoded"):
            pc.parse_properties(b"a=1\na=2\n")

    def test_dangling_continuation_and_raw_non_ascii_rejected(self):
        with self.assertRaisesRegex(ValueError, "continuation"):
            pc.parse_properties(b"a=1\\")  # trailing backslash at end of file
        with self.assertRaisesRegex(ValueError, "ASCII"):
            pc.parse_properties(b"a=\xc3\xa9\n")

    def test_malformed_unicode_escape_rejected(self):
        with self.assertRaisesRegex(ValueError, "unicode escape"):
            pc.parse_properties(b"a=\\u12zz\n")


class SafePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swift sonar cfg ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "dir").mkdir()
        (self.root / "dir/file.txt").write_text("x\n")

    def test_resolves_relative_and_allows_missing_leaf(self):
        self.assertEqual(pc.safe_path(self.root, "dir/file.txt", True), self.root / "dir/file.txt")
        self.assertEqual(pc.safe_path(self.root, "dir/new.txt"), self.root / "dir/new.txt")

    def test_absolute_traversal_and_control_rejected(self):
        for value in ("/etc/passwd", "../escape", "dir/../../x", "dir/\x01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pc.safe_path(self.root, value)

    def test_symlink_component_and_must_exist_rejected(self):
        os.symlink(self.root / "dir", self.root / "link")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            pc.safe_path(self.root, "link/file.txt")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            pc.safe_path(self.root, "dir/absent.txt", True)

    def test_non_directory_ancestor_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-directory ancestor"):
            pc.safe_path(self.root, "dir/file.txt/below")


class GlobTests(unittest.TestCase):
    def test_star_is_segment_scoped_and_doublestar_spans(self):
        self.assertTrue(pc.glob_regex("Tests/**/*.swift").match("Tests/a/b/X.swift"))
        self.assertTrue(pc.glob_regex("Tools/test_*.py").match("Tools/test_x.py"))
        self.assertFalse(pc.glob_regex("Tools/test_*.py").match("Tools/sub/test_x.py"))
        self.assertTrue(pc.glob_regex("**/build/**").match("a/build/c/d"))

    def test_unsupported_syntax_rejected(self):
        for pattern in ("[a-z].swift", "a{b,c}", "/abs", "a\\b"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                pc.glob_regex(pattern)


class LoadProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swift sonar project ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ("Sources", "Tests", "Tools", "build"):
            (self.root / name).mkdir()
        (self.root / "Sources/main.swift").write_text("let answer = 42\n")
        (self.root / "Tests/ExampleTests.swift").write_text("// test\n")
        (self.root / "Tools/check.py").write_text("print('check')\n")
        (self.root / "Tools/test_helper.py").write_text("print('t')\n")
        (self.root / ".gitignore").write_text("build/\n.swift-sonar/\n")
        self.write_properties(
            "sonar.projectKey=example\nsonar.projectName=Example App\nsonar.projectVersion=2.0\n"
            "sonar.sources=Sources,Tools\nsonar.tests=Tests,Tools\n"
            "sonar.test.inclusions=Tests/**/*.swift,Tools/test_*.py\n"
            "sonar.exclusions=**/build/**\nsonar.python.version=3.12\n"
            "sonar.coverageReportPaths=build/coverage.xml\n")
        self.recipe = {"schema": 1, "properties": "sonar-project.properties",
                       "scannerProperties": {},
                       "test": {"argv": ["python3", "Tools/run.py"], "artifacts": [
                           {"kind": "generic-coverage", "path": "build/coverage.xml"}]},
                       "lint": {"argv": DEFAULT_LINT, "import": "native"},
                       "tools": {"swiftlint": "0.65.1", "sonarScanner": "8.3.0", "sonarApple": "0.5.1"}}
        self.config = Path("swift-sonar.json")
        self.save()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.commit()

    def write_properties(self, text):
        (self.root / "sonar-project.properties").write_text(text)

    def save(self):
        (self.root / self.config).write_text(json.dumps(self.recipe))

    def commit(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)

    def load(self):
        self.commit()
        return pc.load_project(self.root, self.config)

    def test_properties_is_merged_dict_and_inventory_is_populated(self):
        recipe = self.load()
        self.assertIsInstance(recipe["properties"], dict)
        self.assertEqual(recipe["properties"]["sonar.projectVersion"], "2.0")
        self.assertIn("Sources/main.swift", recipe["inventory"]["sources"])
        self.assertIn("Tools/check.py", recipe["inventory"]["sources"])

    def test_multilanguage_scope_and_overlap_roots_split_by_test_inclusions(self):
        recipe = self.load()
        inv = recipe["inventory"]
        self.assertIn("Tools/check.py", inv["sources"])        # production utility stays source
        self.assertIn("Tools/test_helper.py", inv["tests"])    # test module classified as test
        self.assertIn("Tests/ExampleTests.swift", inv["tests"])
        self.assertEqual(inv["languages"]["swift"], {"sources": 1, "tests": 1})
        self.assertEqual(inv["languages"]["python"], {"sources": 1, "tests": 1})
        self.assertFalse(set(inv["sources"]) & set(inv["tests"]))

    def test_digest_is_stable_and_bound_to_inputs(self):
        first = self.load()["digest"]
        self.assertEqual(self.load()["digest"], first)
        (self.root / "Sources/main.swift").write_text("let changed = 1\n")
        # Source bytes are not in the recipe digest (bound by receipt snapshot instead).
        self.assertEqual(self.load()["digest"], first)
        self.write_properties((self.root / "sonar-project.properties").read_text() + "sonar.cpd.exclusions=x/**\n")
        self.assertNotEqual(self.load()["digest"], first)

    def test_web_and_php_sources_and_tests_are_classified_without_narrowing(self):
        extensions = ("js", "jsx", "mjs", "cjs", "ts", "tsx", "php")
        for suffix in extensions:
            (self.root / f"Sources/feature.{suffix}").write_text("// source\n")
            (self.root / f"Tests/feature.{suffix}").write_text("// test\n")
        original = (self.root / "sonar-project.properties").read_text()
        self.write_properties(original.replace("Tests/**/*.swift", "Tests/**"))
        inventory = self.load()["inventory"]
        for suffix in extensions:
            self.assertIn(f"Sources/feature.{suffix}", inventory["sources"])
            self.assertIn(f"Tests/feature.{suffix}", inventory["tests"])
        self.assertEqual(inventory["languages"]["javascript"], {"sources": 4, "tests": 4})
        self.assertEqual(inventory["languages"]["typescript"], {"sources": 2, "tests": 2})
        self.assertEqual(inventory["languages"]["php"], {"sources": 1, "tests": 1})
        self.assertEqual(inventory["languages"]["swift"], {"sources": 1, "tests": 1})
        self.assertEqual(inventory["languages"]["python"], {"sources": 1, "tests": 1})

    def test_native_lcov_reports_remain_explicit_typed_inputs(self):
        paths = ["build/web.lcov", "build/tools.lcov"]
        self.recipe["test"]["artifacts"].extend(
            {"kind": "javascript-coverage", "path": path} for path in paths)
        self.recipe["scannerProperties"]["sonar.javascript.lcov.reportPaths"] = ",".join(paths)
        self.save()
        recipe = self.load()
        self.assertEqual(recipe["reports"]["javascript"], sorted(paths))
        self.assertEqual(recipe["reports"]["generic"], ["build/coverage.xml"])
        self.assertEqual(recipe["properties"]["sonar.javascript.lcov.reportPaths"], ",".join(paths))

    def test_unimported_native_lcov_is_rejected(self):
        self.recipe["test"]["artifacts"].append({"kind": "javascript-coverage", "path": "build/web.lcov"})
        self.save()
        with self.assertRaisesRegex(ValueError, "not imported"):
            self.load()

    def test_native_lcov_reference_rejects_wrong_kind(self):
        self.recipe["scannerProperties"]["sonar.javascript.lcov.reportPaths"] = "build/coverage.xml"
        self.save()
        with self.assertRaisesRegex(ValueError, "javascript-coverage"):
            self.load()

    def test_native_lcov_requires_explicit_paths_not_globs(self):
        self.recipe["test"]["artifacts"].append({"kind": "javascript-coverage", "path": "build/web.lcov"})
        self.recipe["scannerProperties"]["sonar.javascript.lcov.reportPaths"] = "build/*.lcov"
        self.save()
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.load()

    def test_native_manifest_is_bound_without_changing_artifact_contract(self):
        manifest = self.root / "Tools/ids.json"
        manifest.write_text(json.dumps(["Suite/testA()"]))
        artifact = {"kind": "xcresult", "path": "build/App.xcresult", "expectedTests": "Tools/ids.json"}
        self.recipe["test"]["artifacts"].append(artifact)
        self.save()
        first = self.load()
        self.assertEqual(first["test"]["artifacts"][-1], artifact)
        manifest.write_text(json.dumps(["Suite/testB()"]))
        self.assertNotEqual(self.load()["digest"], first["digest"])

    def test_test_inclusions_always_exclude_matching_source_files(self):
        original = (self.root / "sonar-project.properties").read_text()
        for suffix in ("sonar.tests=Tests\n", "sonar.test.exclusions=Tools/test_*.py\n"):
            with self.subTest(suffix=suffix):
                properties = original.replace("sonar.tests=Tests,Tools\n", "") if suffix.startswith("sonar.tests=") else original
                self.write_properties(properties + suffix)
                inventory = self.load()["inventory"]
                self.assertNotIn("Tools/test_helper.py", inventory["sources"])
                self.assertNotIn("Tools/test_helper.py", inventory["tests"])

    def test_overlay_wins_for_permitted_keys_and_records_provenance(self):
        self.recipe["scannerProperties"] = {"sonar.projectVersion": "9.9"}
        self.save()
        recipe = self.load()
        self.assertEqual(recipe["properties"]["sonar.projectVersion"], "9.9")
        override = next(o for o in recipe["overrides"] if o["key"] == "sonar.projectVersion")
        self.assertEqual((override["baseline"], override["overlay"]), ("2.0", "9.9"))

    def test_report_reference_must_match_declared_artifact_kind(self):
        self.recipe["test"]["artifacts"] = [{"kind": "python-coverage", "path": "build/coverage.xml"}]
        self.save()
        with self.assertRaisesRegex(ValueError, "generic-coverage"):
            self.load()

    def test_declared_coverage_not_imported_is_rejected(self):
        self.write_properties(
            "sonar.projectKey=example\nsonar.sources=Sources,Tools\nsonar.tests=Tests,Tools\n"
            "sonar.test.inclusions=Tests/**/*.swift,Tools/test_*.py\n")
        # Recipe still declares a generic-coverage artifact that properties no longer import.
        with self.assertRaisesRegex(ValueError, "not imported"):
            self.load()

    def test_unconsumed_xcresult_and_file_artifacts_allowed(self):
        (self.root / "build/App.xcresult").mkdir()
        (self.root / "Tools/ids.json").write_text(json.dumps(["Suite/testA()"]))
        self.write_properties(
            "sonar.projectKey=example\nsonar.sources=Sources,Tools\nsonar.tests=Tests,Tools\n"
            "sonar.test.inclusions=Tests/**/*.swift,Tools/test_*.py\n")
        self.recipe["scannerProperties"] = {}
        self.recipe["test"]["artifacts"] = [
            {"kind": "xcresult", "path": "build/App.xcresult", "expectedTests": "Tools/ids.json"},
            {"kind": "file", "path": "build/summary.txt"}]
        self.save()
        recipe = self.load()
        self.assertEqual(recipe["reports"], {"native": [], "generic": [], "python": []})

    def test_protected_credential_and_scope_keys_rejected(self):
        for key in ("sonar.host.url", "sonar.token", "sonar.working.directory", "sonar.scanner.skip"):
            with self.subTest(key=key):
                self.write_properties(
                    f"sonar.projectKey=example\nsonar.sources=Sources\n{key}=x\n")
                with self.assertRaisesRegex(ValueError, "trusted invocation|owned"):
                    self.load()

    def test_gate_conflict_rejected_but_matching_value_allowed(self):
        self.recipe["test"]["artifacts"] = [{"kind": "file", "path": "build/summary.txt"}]
        self.save()
        self.write_properties(
            "sonar.projectKey=example\nsonar.sources=Sources\nsonar.qualitygate.wait=false\n")
        with self.assertRaisesRegex(ValueError, "runner-owned"):
            self.load()
        self.write_properties(
            "sonar.projectKey=example\nsonar.sources=Sources\nsonar.qualitygate.wait=true\n")
        self.assertEqual(self.load()["properties"]["sonar.qualitygate.wait"], "true")

    def test_unknown_property_fails_closed(self):
        self.write_properties("sonar.projectKey=example\nsonar.sources=Sources\nsonar.mystery=1\n")
        with self.assertRaisesRegex(ValueError, "supported registry"):
            self.load()

    def test_source_and_test_overlap_without_inclusions_rejected(self):
        self.recipe["test"]["artifacts"] = [{"kind": "file", "path": "build/summary.txt"}]
        self.save()
        self.write_properties(
            "sonar.projectKey=example\nsonar.sources=Tools\nsonar.tests=Tools\n")
        with self.assertRaisesRegex(ValueError, "both source and test"):
            self.load()

    def test_strict_json_duplicate_and_schema_boolean_rejected(self):
        (self.root / self.config).write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            self.load()
        self.recipe["schema"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "schema"):
            self.load()

    def test_unknown_recipe_field_and_empty_argv_rejected(self):
        self.recipe["extra"] = 1
        self.save()
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            self.load()
        self.recipe.pop("extra")
        self.recipe["test"]["argv"] = []
        self.save()
        with self.assertRaisesRegex(ValueError, "nonempty list"):
            self.load()

    def test_shell_command_string_and_nul_rejected(self):
        self.recipe["test"]["argv"] = ["bash", "-c", "echo hi"]
        self.save()
        with self.assertRaisesRegex(ValueError, "Shell command"):
            self.load()
        self.recipe["test"]["argv"] = ["python3", "run\x00.py"]
        self.save()
        with self.assertRaisesRegex(ValueError, "control-free"):
            self.load()

    def test_lint_argv_must_be_strict_default(self):
        self.recipe["lint"]["argv"] = ["swiftlint", "lint", "--quiet", "--reporter", "json"]
        self.save()
        with self.assertRaisesRegex(ValueError, "strict"):
            self.load()

    def test_duplicate_and_overlapping_artifacts_rejected(self):
        self.recipe["test"]["artifacts"] = [
            {"kind": "generic-coverage", "path": "build/coverage.xml"},
            {"kind": "file", "path": "build/coverage.xml"}]
        self.save()
        with self.assertRaisesRegex(ValueError, "duplicated or overlap"):
            self.load()

    def test_absolute_and_traversal_config_or_properties_rejected(self):
        self.recipe["properties"] = "../outside.properties"
        self.save()
        with self.assertRaisesRegex(ValueError, "parent"):
            self.load()

    def test_paths_with_spaces_supported(self):
        spaced = Path(self.temp.name).resolve() / "sub dir"
        # Move the whole checkout into a space-containing path via a fresh git repo.
        import shutil
        dest = Path(tempfile.mkdtemp(prefix="space path ")) / "Repo With Spaces"
        shutil.copytree(self.root, dest)
        self.addCleanup(shutil.rmtree, dest.parent)
        subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
        recipe = pc.load_project(dest, self.config)
        self.assertIn("Sources/main.swift", recipe["inventory"]["sources"])


if __name__ == "__main__":
    unittest.main()
