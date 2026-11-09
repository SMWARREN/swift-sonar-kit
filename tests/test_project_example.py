import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swift_sonar_kit import project_runner


EXAMPLE = Path(__file__).parents[1] / "examples" / "project-mode"


class ProjectExampleTests(unittest.TestCase):
    def test_copy_to_space_path_executes_tests_and_prepares_offline(self):
        with tempfile.TemporaryDirectory(prefix="project example ") as temporary:
            root = Path(temporary) / "Second Repository With Spaces"
            shutil.copytree(EXAMPLE, root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)

            def fake_toolchain(_root, _recipe):
                # Explicitly mocked version probes: this portable test does not claim
                # the host has the pinned SwiftLint/Scanner binaries installed.
                return {"kit": "fixture", "swiftlint": {"path": "mock-swiftlint"},
                        "sonarScanner": {"path": "mock-sonar-scanner"}}

            with patch.object(project_runner, "toolchain", side_effect=fake_toolchain), \
                    patch.object(project_runner, "_lint", return_value={
                        "status": "passed", "findings": 0, "exit_code": 0, "import": "native"
                    }), \
                    patch.object(project_runner, "preflight", side_effect=AssertionError("offline preparation contacted server")):
                self.assertEqual(project_runner.project_test(root, Path("swift-sonar.json")), 0)
                self.assertEqual(project_runner.project_scan(
                    root, Path("swift-sonar.json"), prepare_only=True
                ), 0)

            summary = (root / "build/test-summary.txt").read_text()
            self.assertIn("tests_run=2", summary)
            self.assertIn("failures=0", summary)
            evidence = json.loads((root / ".swift-sonar/project-tests.json").read_text())
            self.assertEqual(evidence["scope"], "configured-project-recipe")
            self.assertEqual(evidence["artifacts"]["build/test-summary.txt"]["validation"]["kind"], "file")
            plan = json.loads((root / ".swift-sonar/project-plan.json").read_text())
            self.assertEqual(plan["properties"]["sonar.sources"], "Sources,Tools")
            self.assertEqual(plan["properties"]["sonar.tests"], "Tests")


if __name__ == "__main__":
    unittest.main()
