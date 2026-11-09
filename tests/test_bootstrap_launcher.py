"""Launcher fixtures only, no real bootstrap or scans. SPDX-License-Identifier: GPL-3.0-only."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class LauncherBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.launcher = self.root / "scan"
        shutil.copyfile(Path(__file__).resolve().parents[1] / "scan", self.launcher)
        (self.bin / "dirname").symlink_to("/usr/bin/dirname")
        self.calls = self.root / "calls.jsonl"
        self.managed = self.bin / "managed-python"
        self.fake_python(self.managed)

    def fake_python(self, path):
        path.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FIXTURE_CALLS'], 'a') as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, args]) + '\\n')
if args and args[0] == '-c':
    sys.exit(1 if pathlib.Path(sys.argv[0]).name == 'python3' and '(3, 11)' in args[1] else 0)
if args and args[0].endswith('bootstrap_runtime.py'):
    print(os.environ['FIXTURE_MANAGED'])
    sys.exit(0)
print(json.dumps({'argv': args, 'pythonpath': os.environ.get('PYTHONPATH')}))
''')
        path.chmod(0o700)

    def run_launcher(self, *args, python=None):
        env = {"PATH": str(self.bin), "HOME": str(self.root),
               "FIXTURE_CALLS": str(self.calls), "FIXTURE_MANAGED": str(self.managed)}
        if python is not None:
            env["PYTHON"] = python
        result = subprocess.run(["/bin/sh", str(self.launcher), *args], env=env,
                                capture_output=True, text=True, timeout=10, check=False)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, calls

    def test_installed_modern_python_dispatches_without_runtime_setup(self):
        self.fake_python(self.bin / "python3.12")
        result, calls = self.run_launcher("/fixture/project with spaces", "--setup-only", "--json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], ["-m", "swift_sonar_kit", "run", "/fixture/project with spaces", "--setup-only", "--json"])
        self.assertEqual(payload["pythonpath"], str(self.root / "src"))
        self.assertFalse(any(any("bootstrap_runtime.py" in a for a in args) for _, args in calls))

    def test_only_older_python_prepares_private_runtime_then_dispatches(self):
        self.fake_python(self.bin / "python3")
        result, calls = self.run_launcher("/fixture/project", "--setup-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"][-1], "--setup-only")
        runtime = [(name, args) for name, args in calls if any("bootstrap_runtime.py" in a for a in args)]
        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0][0], "python3")
        self.assertEqual(calls[-1][0], "managed-python")

    def test_readonly_plan_and_help_never_download_runtime(self):
        self.fake_python(self.bin / "python3")
        for flag in ("--plan-only", "--help", "-h"):
            with self.subTest(flag=flag):
                result, calls = self.run_launcher("/fixture/project", flag)
                self.assertEqual(result.returncode, 2)
                self.assertIn("No runtime was downloaded", result.stderr)
                self.assertFalse(any(any("bootstrap_runtime.py" in a for a in args) for _, args in calls))

    def test_explicit_interpreter_is_not_silently_replaced(self):
        self.fake_python(self.bin / "python3")
        result, calls = self.run_launcher("/fixture/project", python=str(self.bin / "python3"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("PYTHON must point", result.stderr)
        self.assertEqual(len(calls), 1)

    def test_explicit_modern_interpreter_preserves_project_path(self):
        result, calls = self.run_launcher("/fixture/project with spaces", python=str(self.managed))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["argv"][-1], "/fixture/project with spaces")
        self.assertTrue(all(name == "managed-python" for name, _ in calls))

    def test_no_arguments_prints_usage_without_probing_or_setup(self):
        result, calls = self.run_launcher()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--setup-only", result.stderr)
        self.assertEqual(calls, [])
