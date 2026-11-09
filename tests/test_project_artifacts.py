import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from swift_sonar_kit import project_artifacts as pa

_REAL_BUNDLE_ENV = os.environ.get("SWIFT_SONAR_TEST_REAL_BUNDLE") or os.environ.get("SWIFT_SONAR_REAL_BUNDLE")
REAL_BUNDLE = Path(_REAL_BUNDLE_ENV) if _REAL_BUNDLE_ENV else None
_REAL_FAILED_BUNDLE_ENV = os.environ.get("SWIFT_SONAR_TEST_REAL_FAILED_BUNDLE") or os.environ.get("SWIFT_SONAR_REAL_FAILED_BUNDLE")
REAL_FAILED_BUNDLE = Path(_REAL_FAILED_BUNDLE_ENV) if _REAL_FAILED_BUNDLE_ENV else None
URL = "test://com.apple.xcode/App/AppTests/"


def case(suite, name, args=()):
    node = {"name": name, "nodeIdentifier": f"{suite}/{name}", "nodeIdentifierURL": f"{URL}{suite}/{name}",
            "nodeType": "Test Case", "result": "Passed"}
    if args:
        node["children"] = [{"name": a, "nodeType": "Arguments", "result": "Passed",
                             "nodeIdentifierURL": f"{node['nodeIdentifierURL']}?args={a.encode().hex()}"}
                            for a in args]
    return node


def tests_json(*cases):
    suite = {"name": "Suite", "nodeType": "Test Suite", "result": "Passed", "children": list(cases)}
    return {"devices": [{"deviceId": "d", "deviceName": "n", "platform": "iOS Simulator",
                         "osVersion": "1", "architecture": "arm64", "modelName": "m"}],
            "testPlanConfigurations": [{"configurationId": "1", "configurationName": "c"}],
            "testNodes": [{"name": "Plan", "nodeType": "Test Plan", "result": "Passed",
                           "children": [{"name": "B", "nodeType": "Unit test bundle", "result": "Passed",
                                         "children": [suite]}]}]}


tests_json.__test__ = False


def summary_json(cases, instances, start, finish, **over):
    value = {"result": "Passed", "totalTestCount": cases, "passedTests": cases, "failedTests": 0,
             "skippedTests": 0, "expectedFailures": 0, "testFailures": [], "startTime": start,
             "finishTime": finish,
             "devicesAndConfigurations": [{"device": {"deviceId": "d", "deviceName": "n"},
                                           "testPlanConfiguration": {"configurationId": "1", "configurationName": "c"},
                                           "passedTests": instances, "failedTests": 0, "skippedTests": 0,
                                           "expectedFailures": 0}]}
    value.update(over)
    return value


class FakeTool:
    """Stands in for `xcrun xcresulttool` so synthetic bundles can be exercised."""

    def __init__(self, summary, tests, version=b"xcresulttool version 25115, schema version: 0.4.0 (legacy)\n"):
        self.summary, self.tests, self.version = summary, tests, version

    def __call__(self, args):
        if args[0] == "version":
            return self.version
        return json.dumps(self.summary if args[2] == "summary" else self.tests).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.t0 = time.time()
        self.t1 = self.t0 + 60
        for name in ("Sources", "Tests", "build"):
            (self.root / name).mkdir()
        (self.root / "Sources/A.swift").write_text("a\nb\nc\n")
        (self.root / "Sources/B.swift").write_text("a\nb\n")
        (self.root / "Tests/T.swift").write_text("t\nt\n")
        self.inventory = {"sources": ["Sources/A.swift", "Sources/B.swift"], "tests": ["Tests/T.swift"]}

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def stamp(self, rel, when=None):
        when = self.t0 + 10 if when is None else when
        path = self.root / rel
        for item in [path, *path.rglob("*")] if path.is_dir() else [path]:
            os.utime(item, (when, when))


class DigestTests(Base):
    def bundle(self, name="build/r.xcresult"):
        self.write(f"{name}/Info.plist", "plist")
        self.write(f"{name}/Data/data.0", "x")
        self.write(f"{name}/Data/sub/data.1", "y")
        self.write("expected.json", "[]")
        return {"kind": "xcresult", "path": name, "expectedTests": "expected.json"}

    def test_file_digest_is_deterministic_and_binds_bytes(self):
        self.write("build/c.xml", "one")
        artifact = {"kind": "file", "path": "build/c.xml"}
        first = pa.artifact_digest(self.root, artifact)
        self.assertEqual(first, pa.artifact_digest(self.root, artifact))
        self.assertEqual(first["size"], 3)
        self.write("build/c.xml", "two")
        self.assertNotEqual(first["sha256"], pa.artifact_digest(self.root, artifact)["sha256"])

    def test_tree_manifest_detects_every_kind_of_mutation(self):
        artifact = self.bundle()
        base = pa.artifact_digest(self.root, artifact)
        self.assertEqual(base["members"], 3)
        self.assertEqual(base["directories"], 3)
        self.assertEqual(base, pa.artifact_digest(self.root, artifact))
        bundle = self.root / "build/r.xcresult"

        (bundle / "Data/data.0").write_text("z")  # Same size, different bytes.
        changed = pa.artifact_digest(self.root, artifact)["sha256"]
        self.assertNotEqual(base["sha256"], changed)
        (bundle / "Data/data.0").write_text("x")
        self.assertEqual(base["sha256"], pa.artifact_digest(self.root, artifact)["sha256"])

        (bundle / "Data/extra").write_text("")  # Addition.
        self.assertNotEqual(base["sha256"], pa.artifact_digest(self.root, artifact)["sha256"])
        (bundle / "Data/extra").unlink()

        (bundle / "Data/sub/data.1").rename(bundle / "Data/sub/data.2")  # Rename.
        self.assertNotEqual(base["sha256"], pa.artifact_digest(self.root, artifact)["sha256"])
        (bundle / "Data/sub/data.2").rename(bundle / "Data/sub/data.1")

        (bundle / "Data/sub/data.1").unlink()  # Removal.
        (bundle / "Data/sub/data.1").write_text("y")
        self.assertEqual(base["sha256"], pa.artifact_digest(self.root, artifact)["sha256"])

    def test_manifest_covers_expected_tests_binding(self):
        artifact = self.bundle()
        before = pa.artifact_digest(self.root, artifact)["expectedTests"]
        self.write("expected.json", '["A/b"]')
        self.assertNotEqual(before, pa.artifact_digest(self.root, artifact)["expectedTests"])

    def test_symlinks_are_rejected_at_leaf_ancestor_and_member(self):
        artifact = self.bundle()
        bundle = self.root / "build/r.xcresult"
        (bundle / "Data/link").symlink_to("data.0")
        with self.assertRaisesRegex(ValueError, "symlink"):
            pa.artifact_digest(self.root, artifact)
        (bundle / "Data/link").unlink()
        (bundle / "Data/dirlink").symlink_to("sub")
        with self.assertRaisesRegex(ValueError, "symlink"):
            pa.artifact_digest(self.root, artifact)
        (bundle / "Data/dirlink").unlink()

        shutil.move(str(self.root / "build"), str(self.root / "real"))
        (self.root / "build").symlink_to("real")  # Symlinked ancestor.
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            pa.artifact_digest(self.root, artifact)
        (self.root / "build").unlink()
        shutil.move(str(self.root / "real"), str(self.root / "build"))
        shutil.move(str(bundle), str(self.root / "bundle-real"))
        bundle.symlink_to(self.root / "bundle-real")  # Symlinked leaf.
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            pa.artifact_digest(self.root, artifact)

    def test_file_symlink_and_symlinked_root_alias(self):
        self.write("real.xml", "x")
        (self.root / "build/link.xml").symlink_to(self.root / "real.xml")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            pa.artifact_digest(self.root, {"kind": "file", "path": "build/link.xml"})
        alias = self.root.parent / (self.root.name + "-alias")
        alias.symlink_to(self.root)
        self.addCleanup(alias.unlink)
        self.write("build/ok.xml", "ok")  # The root itself may be an alias.
        self.assertEqual(pa.artifact_digest(alias, {"kind": "file", "path": "build/ok.xml"})["size"], 2)

    def test_special_files_and_empty_directories_are_rejected(self):
        artifact = self.bundle()
        bundle = self.root / "build/r.xcresult"
        os.mkfifo(bundle / "Data/fifo")
        with self.assertRaisesRegex(ValueError, "special"):
            pa.artifact_digest(self.root, artifact)
        os.unlink(bundle / "Data/fifo")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(bundle / "s"))
        with self.assertRaisesRegex(ValueError, "special"):
            pa.artifact_digest(self.root, artifact)
        os.unlink(bundle / "s")
        (bundle / "empty").mkdir()
        with self.assertRaisesRegex(ValueError, "empty"):
            pa.artifact_digest(self.root, artifact)
        (bundle / "empty").rmdir()
        self.write("build/fifo.xml", "")
        os.unlink(self.root / "build/fifo.xml")
        os.mkfifo(self.root / "build/fifo.xml")
        with self.assertRaisesRegex(ValueError, "regular"):
            pa.artifact_digest(self.root, {"kind": "file", "path": "build/fifo.xml"})

    def test_traversal_and_malformed_declarations_are_rejected(self):
        for path in ("../x", "build/../x", "/etc/passwd", "build//x", "./x", "", "x/", "a\\b", "a\nb", 3, None):
            with self.assertRaises(ValueError, msg=repr(path)):
                pa.artifact_digest(self.root, {"kind": "file", "path": path})
        for artifact in ({"kind": "other", "path": "x"}, {"kind": "file", "path": "x", "extra": 1},
                         {"kind": "xcresult", "path": "x"}, [], {"path": "x"}):
            with self.assertRaises(ValueError):
                pa.artifact_digest(self.root, artifact)
        with self.assertRaisesRegex(ValueError, "missing"):
            pa.artifact_digest(self.root, {"kind": "file", "path": "build/none.xml"})

    def test_unstable_read_is_rejected(self):
        self.write("build/c.xml", "abc")
        path = self.root / "build/c.xml"
        real = os.read
        state = {"done": False}

        def racing(fd, size):
            data = real(fd, size)
            if data and not state["done"]:
                state["done"] = True
                with path.open("a") as stream:
                    stream.write("more")
            return data

        with patch.object(pa.os, "read", racing), self.assertRaisesRegex(ValueError, "changed"):
            pa.artifact_digest(self.root, {"kind": "file", "path": "build/c.xml"})

    def test_tree_changing_between_passes_is_rejected(self):
        artifact = self.bundle()
        real = pa._tree_pass
        calls = []

        def mutate(base, hash_files):
            result = real(base, hash_files)
            calls.append(hash_files)
            if hash_files:
                (base / "Data/late").write_text("late")
            return result

        with patch.object(pa, "_tree_pass", mutate), self.assertRaisesRegex(ValueError, "changed"):
            pa.artifact_digest(self.root, artifact)

    def test_size_bounds(self):
        self.write("build/c.xml", "abcd")
        with patch.object(pa, "MAX_FILE_BYTES", 3), self.assertRaisesRegex(ValueError, "size"):
            pa.artifact_digest(self.root, {"kind": "file", "path": "build/c.xml"})
        artifact = self.bundle()
        with patch.object(pa, "MAX_TREE_FILES", 2), self.assertRaisesRegex(ValueError, "size"):
            pa.artifact_digest(self.root, artifact)
        with patch.object(pa, "MAX_TREE_DEPTH", 1), self.assertRaisesRegex(ValueError, "deeply"):
            pa.artifact_digest(self.root, artifact)


class CoverageTests(Base):
    GENERIC = ('<coverage version="1"><file path="Sources/A.swift">'
               '<lineToCover lineNumber="1" covered="true"/><lineToCover lineNumber="3" covered="false"/>'
               '</file></coverage>')

    def run_cov(self, text, kind="generic-coverage", inventory=None, mtime=None):
        self.write("build/cov.xml", text)
        self.stamp("build/cov.xml", mtime)
        return pa.validate_artifact(self.root, {"kind": kind, "path": "build/cov.xml"},
                                    self.inventory if inventory is None else inventory, self.t0, self.t1)

    def test_generic_summary_and_bytes_preserved(self):
        result = self.run_cov(self.GENERIC)
        self.assertEqual(result["coverage"]["format"], "sonar-generic")
        self.assertEqual((result["coverage"]["files"], result["coverage"]["lines"],
                          result["coverage"]["covered_lines"]), (1, 2, 1))
        self.assertEqual((self.root / "build/cov.xml").read_text(), self.GENERIC)
        self.assertEqual(result["digest"]["size"], len(self.GENERIC))

    def test_cobertura_python_with_source_roots(self):
        text = (f'<?xml version="1.0" ?>\n<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">'
                f'<coverage><sources><source>{self.root}/Sources</source></sources><packages><package><classes>'
                '<class filename="A.swift"><lines><line number="1" hits="2"/><line number="2" hits="0"/></lines></class>'
                '</classes></package></packages></coverage>')
        result = self.run_cov(text, "python-coverage")
        self.assertEqual(result["coverage"]["format"], "cobertura")
        self.assertEqual(result["coverage"]["covered_lines"], 1)

    def test_unsafe_or_invalid_xml_is_rejected(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE coverage [<!ENTITY a "x">]><coverage version="1">&a;</coverage>')
        external = '<!DOCTYPE coverage SYSTEM "file:///etc/passwd"><coverage version="1"/>'
        subset = '<!DOCTYPE coverage SYSTEM "x" [<!ENTITY a "b">]><coverage version="1"/>'
        for text in (bomb, external, subset, "<coverage", "not xml", "\ufeff" + self.GENERIC,
                     '<?xml version="1.0" encoding="UTF-16"?>' + self.GENERIC, "<other/>"):
            with self.assertRaises(ValueError, msg=text[:40]):
                self.run_cov(text)

    def test_records_must_match_inventory(self):
        def doc(path, extra=""):
            return (f'<coverage version="1"><file path="{path}"><lineToCover lineNumber="1" covered="true"{extra}/>'
                    '</file></coverage>')
        for path, message in (("Tests/T.swift", "test files"), ("Sources/Other.swift", "inventory"),
                              ("../x.swift", "parent"), ("/etc/passwd", "outside"),
                              (f"{self.root.parent}/x.swift", "outside"), ("", "nonempty")):
            with self.assertRaisesRegex(ValueError, message, msg=path):
                self.run_cov(doc(path))
        self.run_cov(doc(f"{self.root}/Sources/A.swift"))  # Absolute in-root path is canonical-equal.
        with self.assertRaisesRegex(ValueError, "Branch"):
            self.run_cov(doc("Sources/A.swift", ' branchesToCover="2"'))

    def test_invalid_lines_are_rejected(self):
        def doc(line):
            return f'<coverage version="1"><file path="Sources/A.swift">{line}</file></coverage>'
        for line in ('<lineToCover lineNumber="0" covered="true"/>', '<lineToCover lineNumber="4" covered="true"/>',
                     '<lineToCover lineNumber="-1" covered="true"/>', '<lineToCover lineNumber="x" covered="true"/>',
                     '<lineToCover lineNumber="1" covered="yes"/>', '<lineToCover lineNumber="1"/>',
                     '<lineToCover lineNumber="1" covered="true"/><lineToCover lineNumber="1" covered="false"/>',
                     '<lineToCover lineNumber="1" covered="true" extra="1"/>', '<bad/>', ""):
            with self.assertRaises(ValueError, msg=line):
                self.run_cov(doc(line))
        with self.assertRaisesRegex(ValueError, "repeats"):
            self.run_cov('<coverage version="1"><file path="Sources/A.swift"><lineToCover lineNumber="1" covered="true"/>'
                         '</file><file path="Sources/A.swift"><lineToCover lineNumber="2" covered="true"/></file></coverage>')

    def test_python_kind_requires_cobertura_and_inventory_is_validated(self):
        with self.assertRaisesRegex(ValueError, "Cobertura"):
            self.run_cov(self.GENERIC, "python-coverage")
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.run_cov(self.GENERIC, inventory={"sources": ["Sources/A.swift"], "tests": ["Sources/A.swift"]})
        with self.assertRaisesRegex(ValueError, "sources"):
            self.run_cov(self.GENERIC, inventory={})

    def test_symlinked_source_is_rejected(self):
        (self.root / "Sources/L.swift").symlink_to("A.swift")
        text = '<coverage version="1"><file path="Sources/L.swift"><lineToCover lineNumber="1" covered="true"/></file></coverage>'
        with self.assertRaisesRegex(ValueError, "symbolic link|missing"):
            self.run_cov(text, inventory={"sources": {"Sources/L.swift": 3}, "tests": []})

    def test_stale_and_future_reports_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not written during"):
            self.run_cov(self.GENERIC, mtime=self.t0 - 3600)
        with self.assertRaisesRegex(ValueError, "not written during"):
            self.run_cov(self.GENERIC, mtime=self.t1 + 3600)
        for window in ((float("nan"), self.t1), (self.t0, float("inf")), (self.t1, self.t0), (True, self.t1), (0, 5)):
            with self.assertRaises(ValueError, msg=repr(window)):
                pa.validate_artifact(self.root, {"kind": "file", "path": "build/cov.xml"}, {}, *window)

    def test_plain_file_kind_has_digest_only(self):
        self.write("build/f.bin", "data")
        self.stamp("build/f.bin")
        result = pa.validate_artifact(self.root, {"kind": "file", "path": "build/f.bin"}, {}, self.t0, self.t1)
        self.assertEqual(set(result), {"kind", "path", "digest", "written_between"})


class XcresultTests(Base):
    def setUp(self):
        super().setUp()
        self.artifact = {"kind": "xcresult", "path": "build/r.xcresult", "expectedTests": "expected.json"}
        self.write("build/r.xcresult/Info.plist", "plist")
        self.write("build/r.xcresult/Data/d0", "x")
        self.stamp("build/r.xcresult")
        self.cases = [case("S", "a()"), case("S", "p(v:)", ("1", "2")), case("S", "b()")]
        self.identities = ["S/a()", "S/b()", f"S/p(v:)?args={'1'.encode().hex()}", f"S/p(v:)?args={'2'.encode().hex()}"]
        self.expected(self.identities)

    def expected(self, items):
        self.write("expected.json", json.dumps(items))

    def validate(self, summary=None, tests=None, **kw):
        summary = summary or summary_json(3, 4, self.t0 + 5, self.t0 + 20)
        tests = tests or tests_json(*self.cases)
        with patch.object(pa, "_xcresulttool", FakeTool(summary, tests, **kw)), \
                patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            return pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1)

    def validate_with_failed_policy(self, allow_failed, summary=None, tests=None):
        summary = summary or summary_json(3, 4, self.t0 + 5, self.t0 + 20)
        tests = tests or tests_json(*self.cases)
        with patch.object(pa, "_xcresulttool", FakeTool(summary, tests)), \
                patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            return pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1,
                                        allow_failed=allow_failed)

    def test_success_reports_native_counts_and_identities(self):
        native = self.validate()["native"]
        self.assertEqual((native["test_cases"], native["test_instances"]), (3, 4))
        self.assertEqual((native["failed"], native["skipped"]), (0, 0))
        self.assertEqual(native["reader"]["schema"], "0.4.0")
        self.assertEqual(len(native["identities_sha256"]), 64)

    def test_same_count_but_wrong_identities_fails(self):
        self.expected(["S/a()", "S/b()", "S/other()", "S/other2()"])
        with self.assertRaisesRegex(ValueError, "differ from expectedTests: 2 missing"):
            self.validate()

    def test_parameterized_instance_must_match_exactly(self):
        swapped = [case("S", "a()"), case("S", "p(v:)", ("1", "3")), case("S", "b()")]
        with self.assertRaisesRegex(ValueError, "1 missing"):
            self.validate(tests=tests_json(*swapped))

    def test_collapsed_parameterized_case_does_not_satisfy_instances(self):
        self.expected(["S/a()", "S/b()", "S/p(v:)"])
        with self.assertRaisesRegex(ValueError, "differ"):
            self.validate()

    def test_extra_or_missing_tests_fail(self):
        self.expected(self.identities + ["S/c()"])
        with self.assertRaisesRegex(ValueError, "1 missing"):
            self.validate()
        self.expected(self.identities[:-1])
        with self.assertRaisesRegex(ValueError, "1 unexpected"):
            self.validate()

    def test_malformed_expected_tests(self):
        for text in ("[]", "{}", '["a","a"]', '[1]', "[", '[""]'):
            self.write("expected.json", text)
            with self.assertRaises(ValueError, msg=text):
                self.validate()

    def test_native_failures_skips_and_zero_fail(self):
        bad = {"result": "Failed"}, {"failedTests": 1, "passedTests": 2}, {"skippedTests": 1, "passedTests": 2}, \
            {"expectedFailures": 1}, {"totalTestCount": 0, "passedTests": 0}, {"testFailures": [{"x": 1}]}, \
            {"startTime": float("nan")}, {"finishTime": "1"}, {"passedTests": True}
        for over in bad:
            summary = summary_json(3, 4, self.t0 + 5, self.t0 + 20, **over)
            with self.assertRaises(ValueError, msg=str(over)):
                self.validate(summary=summary)
        summary = summary_json(3, 4, self.t0 + 5, self.t0 + 20)
        summary["devicesAndConfigurations"] *= 2
        with self.assertRaisesRegex(ValueError, "repeats a device/configuration"):
            self.validate(summary=summary)

    def test_failed_native_complete_census_is_diagnostic_only(self):
        cases = copy.deepcopy(self.cases)
        cases[0]["result"] = "Failed"
        summary = summary_json(3, 4, self.t0 + 5, self.t0 + 20,
                              result="Failed", passedTests=2, failedTests=1,
                              testFailures=[{"testIdentifier": "S/a()"}],
                              devicesAndConfigurations=[{
                                  "device": {"deviceId": "d", "deviceName": "n"},
                                  "testPlanConfiguration": {"configurationId": "1", "configurationName": "c"},
                                  "passedTests": 3, "failedTests": 1, "skippedTests": 0,
                                  "expectedFailures": 0,
                              }])
        with self.assertRaises(ValueError):
            self.validate_with_failed_policy(False, summary, tests_json(*cases))
        result = self.validate_with_failed_policy(True, summary, tests_json(*cases))
        native = result["native"]
        self.assertEqual(native["result"], "Failed")
        self.assertEqual((native["test_cases"], native["test_instances"]), (3, 4))
        self.assertEqual((native["passed"], native["failed"], native["skipped"],
                          native["expected_failures"]), (2, 1, 0, 0))
        self.assertEqual(native["passed"] + native["failed"], native["test_cases"])
        self.assertEqual(native["test_instances"], len(self.identities))
        self.assertRegex(native["identities_sha256"], r"^[0-9a-f]{64}$")

    def test_allow_failed_does_not_accept_skipped_expected_failure_or_unknown(self):
        cases = copy.deepcopy(self.cases)
        cases[0]["result"] = "Skipped"
        with self.assertRaises(ValueError):
            self.validate_with_failed_policy(True, summary_json(
                3, 4, self.t0 + 5, self.t0 + 20, result="Failed", passedTests=2,
                failedTests=0, skippedTests=1, testFailures=[]), tests_json(*cases))
        cases[0]["result"] = "Expected Failure"
        with self.assertRaises(ValueError):
            self.validate_with_failed_policy(True, summary_json(
                3, 4, self.t0 + 5, self.t0 + 20, result="Failed", passedTests=2,
                failedTests=0, expectedFailures=1, testFailures=[]), tests_json(*cases))
        cases[0]["result"] = "NotSure"
        with self.assertRaises(ValueError):
            self.validate_with_failed_policy(True, summary_json(
                3, 4, self.t0 + 5, self.t0 + 20, result="Failed", passedTests=2,
                failedTests=1, testFailures=[{"testIdentifier": "S/a()"}]), tests_json(*cases))


def merged_tests_json():
    devices = [
        {"deviceId": "d-ios", "platform": "iOS Simulator", "osVersion": "18.0", "deviceName": "Phone"},
        {"deviceId": "d-mac", "platform": "macOS", "osVersion": "15.0", "deviceName": "Mac"},
    ]
    cases = []
    for name in ("a()", "b()"):
        url = f"{URL}AppTests/S/{name}"
        cases.append({"name": name, "nodeIdentifier": f"S/{name}", "nodeIdentifierURL": url,
                      "nodeType": "Test Case", "result": "Passed", "children": [
                          {"name": device["deviceName"], "nodeType": "Device", "result": "Passed",
                           "nodeIdentifier": device["deviceId"], "children": [
                               {"name": "Debug", "nodeType": "Test Plan Configuration", "result": "Passed",
                                "nodeIdentifier": "cfg-1"}]} for device in devices]})
    return {"devices": devices,
            "testPlanConfigurations": [{"configurationId": "cfg-1", "configurationName": "Debug"}],
            "testNodes": [{"name": "Plan", "nodeType": "Test Plan", "result": "Passed",
                           "children": [{"name": "AppTests", "nodeType": "Unit test bundle",
                                         "result": "Passed", "children": [{"name": "Suite",
                                         "nodeType": "Test Suite", "result": "Passed", "children": cases}]}]}]}


class MergedXcresultTests(Base):
    def setUp(self):
        super().setUp()
        self.bundle = self.root / "build/r.xcresult"
        self.bundle.mkdir()
        (self.bundle / "Info.plist").write_text("plist")
        (self.bundle / "Data").mkdir()
        (self.bundle / "Data/d0").write_text("x")
        self.stamp("build/r.xcresult")
        self.summary = summary_json(2, 2, self.t0 + 5, self.t0 + 20,
                                    devicesAndConfigurations=[{
                                        "device": {"deviceId": did, "deviceName": name},
                                        "testPlanConfiguration": {"configurationId": "cfg-1", "configurationName": "Debug"},
                                        "passedTests": 2, "failedTests": 0, "skippedTests": 0,
                                        "expectedFailures": 0,
                                    } for did, name in (("d-ios", "Phone"), ("d-mac", "Mac"))])

    def test_merged_result_reconciles_per_device_and_distinct_cases(self):
        with patch.object(pa, "_xcresulttool", FakeTool(self.summary, merged_tests_json())), \
                patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            result = pa.native_xcresult(self.bundle)
        self.assertEqual(result["shape"], "merged")
        self.assertEqual((result["test_cases"], result["test_instances"]), (2, 4))
        self.assertEqual([(d["device"], d["instances"]) for d in result["devices"]],
                         [("Phone", 2), ("Mac", 2)])
        self.assertEqual(len(result["identities"]), 4)
        self.assertEqual(len(set(result["identities"])), 4)

    def test_merged_result_rejects_per_device_count_disagreement(self):
        bad_summary = copy.deepcopy(self.summary)
        bad_summary["devicesAndConfigurations"][0]["passedTests"] = 3
        with patch.object(pa, "_xcresulttool", FakeTool(bad_summary, merged_tests_json())), \
                patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"), \
                patch.object(pa, "_supplement", return_value=[]), \
                self.assertRaisesRegex(ValueError, "do not match"):
            pa.native_xcresult(self.bundle)

class XcresultTests(XcresultTests):
    def test_failed_skipped_or_missing_nodes_in_tree_fail(self):
        for result in ("Failed", "Skipped", "Expected Failure", "unknown"):
            cases = copy.deepcopy(self.cases)
            cases[0]["result"] = result
            with self.assertRaises(ValueError, msg=result):
                self.validate(tests=tests_json(*cases))
        cases = copy.deepcopy(self.cases)
        cases[0]["children"] = [{"name": "f", "nodeType": "Failure Message"}]
        with self.assertRaisesRegex(ValueError, "failure"):
            self.validate(tests=tests_json(*cases))
        cases = copy.deepcopy(self.cases)
        cases[1]["children"][0]["result"] = "Failed"
        with self.assertRaises(ValueError):
            self.validate(tests=tests_json(*cases))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.validate(tests=tests_json(*self.cases[:2]))
        cases = copy.deepcopy(self.cases)
        cases[0]["nodeIdentifier"] = ""
        with self.assertRaises(ValueError):
            self.validate(tests=tests_json(*cases))

    def test_duplicate_native_identities_fail(self):
        cases = [case("S", "a()"), case("S", "a()"), case("S", "b()")]
        with self.assertRaisesRegex(ValueError, "unique"):
            self.validate(summary=summary_json(3, 3, self.t0 + 5, self.t0 + 20), tests=tests_json(*cases))

    def test_native_timestamps_must_be_inside_the_run(self):
        for start, finish in ((self.t0 - 3600, self.t0 + 5), (self.t0 + 5, self.t1 + 3600),
                              (self.t0 + 20, self.t0 + 5), (self.t0 - 3600, self.t0 - 3000)):
            with self.assertRaisesRegex(ValueError, "timestamps"):
                self.validate(summary=summary_json(3, 4, start, finish))

    def test_stale_bundle_files_fail_even_when_native_data_is_fresh(self):
        self.stamp("build/r.xcresult/Data/d0", self.t0 - 7200)  # Old member copied into a new directory.
        with self.assertRaisesRegex(ValueError, "not written during"):
            self.validate()

    def test_unsupported_reader_schema_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.validate(version=b"xcresulttool version 1, schema version: 9.9.9\n")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.validate(version=b"garbage")

    def test_not_a_bundle_and_malformed_native_json(self):
        (self.root / "build/r.xcresult/Info.plist").unlink()
        self.stamp("build/r.xcresult")
        with self.assertRaisesRegex(ValueError, "not an xcresult"):
            self.validate()
        self.write("build/r.xcresult/Info.plist", "plist")
        self.stamp("build/r.xcresult")
        for tests in ({}, {"devices": [], "testNodes": [], "testPlanConfigurations": []}):
            with self.assertRaises(ValueError):
                with patch.object(pa, "_xcresulttool", FakeTool(summary_json(3, 4, self.t0 + 5, self.t0 + 20), tests)), \
                        patch.object(pa, "_run", lambda argv: b"x"):
                    pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1)

        class Broken(FakeTool):
            def __call__(self, args):
                return b'{"a":1,"a":2}' if args[0] != "version" else self.version

        with patch.object(pa, "_xcresulttool", Broken(None, None)), patch.object(pa, "_run", lambda argv: b"x"):
            with self.assertRaisesRegex(ValueError, "duplicate key"):
                pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1)

    def test_bundle_mutation_during_native_read_fails(self):
        tool = FakeTool(summary_json(3, 4, self.t0 + 5, self.t0 + 20), tests_json(*self.cases))

        def mutating(args):
            if args[0] == "get":
                self.write("build/r.xcresult/Data/d0", "mutated")
            return tool(args)

        with patch.object(pa, "_xcresulttool", mutating), patch.object(pa, "_run", lambda argv: b"x"):
            with self.assertRaisesRegex(ValueError, "changed during validation"):
                pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1)

    def test_symlinked_bundle_member_fails(self):
        (self.root / "build/r.xcresult/Data/l").symlink_to("d0")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.validate()


class Schema050Tests(Base):
    """Focused regressions verifying Xcode 27.2 / xcresulttool 25407 schema 0.5.0 contract."""

    FIXTURE_DIR = Path(__file__).parent / "fixtures" / "native-0.5"
    VERSION_050 = b"xcresulttool version 25407, schema version: 0.5.0 (legacy commands format version: 3.60)\n"

    def setUp(self):
        super().setUp()
        self.artifact = {"kind": "xcresult", "path": "build/r.xcresult", "expectedTests": "expected.json"}
        self.write("build/r.xcresult/Info.plist", "plist")
        self.write("build/r.xcresult/Data/d0", "x")
        self.stamp("build/r.xcresult")

    def test_schema_050_reader_identity_and_single_device(self):
        summary = json.loads((self.FIXTURE_DIR / "single_passed_summary.json").read_text())
        summary["startTime"] = self.t0 + 5
        summary["finishTime"] = self.t0 + 20
        tests = json.loads((self.FIXTURE_DIR / "single_passed_tests.json").read_text())
        expected_ids = [
            "SanitizedTests/testSimpleOne()",
            "SanitizedTests/testSimpleTwo()",
            "SanitizedTests/testParameterized(value:)?args=" + "alpha".encode().hex(),
            "SanitizedTests/testParameterized(value:)?args=" + "beta".encode().hex(),
        ]
        self.write("expected.json", json.dumps(expected_ids))
        tool = FakeTool(summary, tests, version=self.VERSION_050)
        with patch.object(pa, "_xcresulttool", tool), patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            direct = pa.native_xcresult(self.root / "build/r.xcresult")
            result = pa.validate_artifact(self.root, self.artifact, {}, self.t0, self.t1)
        self.assertEqual(direct["identities"], sorted(expected_ids))
        native = result["native"]
        self.assertEqual(native["reader"]["schema"], "0.5.0")
        self.assertEqual(native["reader"]["version"], "25407")
        self.assertEqual(native["reader"]["tool"], "xcresulttool")
        self.assertEqual((native["test_cases"], native["test_instances"]), (3, 4))
        self.assertEqual((native["passed"], native["failed"], native["skipped"]), (3, 0, 0))
        self.assertEqual(native["identities_sha256"], hashlib.sha256("\n".join(sorted(expected_ids)).encode()).hexdigest())

    def test_schema_050_merged_multi_device(self):
        summary = json.loads((self.FIXTURE_DIR / "merged_passed_summary.json").read_text())
        tests = json.loads((self.FIXTURE_DIR / "merged_passed_tests.json").read_text())
        tool = FakeTool(summary, tests, version=self.VERSION_050)
        with patch.object(pa, "_xcresulttool", tool), patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            native = pa.native_xcresult(self.root / "build/r.xcresult")
        self.assertEqual(native["reader"]["schema"], "0.5.0")
        self.assertEqual(native["shape"], "merged")
        self.assertEqual(native["test_cases"], 3)
        self.assertEqual(native["test_instances"], 8)
        self.assertEqual(len(native["devices"]), 2)
        for identity in native["identities"]:
            self.assertIn("SanitizedSuite", identity)

    def test_schema_050_diagnostic_failure_census(self):
        summary = json.loads((self.FIXTURE_DIR / "failure_summary.json").read_text())
        tests = json.loads((self.FIXTURE_DIR / "failure_tests.json").read_text())
        tool = FakeTool(summary, tests, version=self.VERSION_050)
        with patch.object(pa, "_xcresulttool", tool), patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            with self.assertRaisesRegex(ValueError, "Native result is not Passed"):
                pa.native_xcresult(self.root / "build/r.xcresult", allow_failed=False)
            failed_native = pa.native_xcresult(self.root / "build/r.xcresult", allow_failed=True)
        self.assertEqual(failed_native["reader"]["schema"], "0.5.0")
        self.assertEqual(failed_native["result"], "Failed")
        self.assertEqual(failed_native["test_cases"], 2)
        self.assertEqual(failed_native["passed"], 1)
        self.assertEqual(failed_native["failed"], 1)

    def test_schema_050_rejects_skips_and_expected_failures(self):
        summary = json.loads((self.FIXTURE_DIR / "single_passed_summary.json").read_text())
        tests = json.loads((self.FIXTURE_DIR / "single_passed_tests.json").read_text())
        summary_skip = copy.deepcopy(summary)
        summary_skip["skippedTests"] = 1
        summary_skip["passedTests"] = 2
        tool = FakeTool(summary_skip, tests, version=self.VERSION_050)
        with patch.object(pa, "_xcresulttool", tool), patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
            with self.assertRaisesRegex(ValueError, "skipped"):
                pa.native_xcresult(self.root / "build/r.xcresult", allow_failed=True)

    def test_unknown_schemas_fail_closed(self):
        for unsupported_ver in [
            b"xcresulttool version 25000, schema version: 0.3.0\n",
            b"xcresulttool version 26000, schema version: 0.6.0\n",
            b"xcresulttool version 30000, schema version: 1.0.0\n",
            b"xcresulttool version 00000, schema version: invalid\n",
        ]:
            tool = FakeTool({}, {}, version=unsupported_ver)
            with patch.object(pa, "_xcresulttool", tool), patch.object(pa, "_run", lambda argv: b"/x/xcresulttool\n"):
                with self.assertRaisesRegex(ValueError, "Unsupported or unrecognized xcresulttool schema version"):
                    pa.native_xcresult(self.root / "build/r.xcresult")


KNOWN_FIXTURE_COUNTS = {
    "controls12verified-unit-1-20261031.xcresult": (467, 529),
    "Test-HashFleetMac-2026.11.07_04-42-58--0500.xcresult": (201, 298),
    "Test-HashFleetMac-2026.11.07_04-37-57--0500.xcresult": (201, 298),
}


@unittest.skipUnless(REAL_BUNDLE and REAL_BUNDLE.is_dir() and shutil.which("xcrun"), "real xcresult or xcrun unavailable")
class RealReaderTests(Base):
    def test_real_bundle_copy_reconciles_parameterized_instances(self):
        shutil.copytree(REAL_BUNDLE, self.root / "build/r.xcresult")  # Read only on the original.
        native = pa.native_xcresult(self.root / "build/r.xcresult")
        expected_cases = int(os.environ["SWIFT_SONAR_TEST_REAL_CASES"]) if "SWIFT_SONAR_TEST_REAL_CASES" in os.environ else (
            KNOWN_FIXTURE_COUNTS.get(REAL_BUNDLE.name, (467, 529))[0]
        )
        expected_instances = int(os.environ["SWIFT_SONAR_TEST_REAL_INSTANCES"]) if "SWIFT_SONAR_TEST_REAL_INSTANCES" in os.environ else (
            KNOWN_FIXTURE_COUNTS.get(REAL_BUNDLE.name, (467, 529))[1]
        )
        self.assertEqual((native["test_cases"], native["test_instances"]), (expected_cases, expected_instances))
        self.assertGreater(native["test_instances"], native["test_cases"])
        self.write("expected.json", json.dumps(native["identities"]))
        self.stamp("build/r.xcresult", native["started_at"] + 1)
        artifact = {"kind": "xcresult", "path": "build/r.xcresult", "expectedTests": "expected.json"}
        result = pa.validate_artifact(self.root, artifact, {}, native["started_at"] - 300, native["finished_at"] + 60)
        self.assertEqual(result["native"]["test_instances"], native["test_instances"])
        collapsed = [item.split("?args=")[0] for item in native["identities"]]
        self.write("expected.json", json.dumps(sorted(set(collapsed))))
        with self.assertRaisesRegex(ValueError, "differ"):
            pa.validate_artifact(self.root, artifact, {}, native["started_at"] - 300, native["finished_at"] + 60)

    @unittest.skipUnless(REAL_FAILED_BUNDLE and REAL_FAILED_BUNDLE.is_dir(), "failed native xcresult unavailable")
    def test_real_failed_audit_is_complete_diagnostic_census(self):
        # Read the existing bundle directly. Never rebuild or mutate the source artifact.
        with self.assertRaises(ValueError):
            pa.native_xcresult(REAL_FAILED_BUNDLE)
        native = pa.native_xcresult(REAL_FAILED_BUNDLE, allow_failed=True)
        self.assertEqual(native["result"], "Failed")
        self.assertGreater(native["failed"], 0)
        self.assertEqual(native["passed"] + native["failed"], native["test_cases"])
        self.assertEqual(native["test_instances"], len(native["devices"]) and
                         sum(device["instances"] for device in native["devices"]))
        self.assertEqual(len(native["identities"]), native["test_instances"])


if __name__ == "__main__":
    unittest.main()
