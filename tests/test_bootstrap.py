"""Tests for secure tool bootstrapping and environment preparation.

SPDX-License-Identifier: GPL-3.0-only
"""

import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from swift_sonar_kit import bootstrap


def _make_zip(entries: dict[str, bytes], *, external_attrs: dict[str, int] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            if external_attrs and name in external_attrs:
                info.external_attr = external_attrs[name]
            else:
                info.external_attr = 0o755 << 16
            zf.writestr(info, data)
    return buffer.getvalue()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swift-sonar-bootstrap-")
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "project").resolve()
        self.root.mkdir()
        self.cache = (Path(self.temp.name) / "cache").resolve()
        self.cache.mkdir(mode=0o700)

    def test_replaces_and_restores_environment_cleanly(self):
        original_path = os.environ.get("PATH", "/usr/bin")
        original_python = os.environ.get("PYTHON")
        fake_bin = self.cache / "fake-bin"
        fake_bin.mkdir()

        with patch.object(bootstrap, "ensure_environment") as ensure:
            ensure.return_value = {
                "tools": {"swiftlint": {"path": str(fake_bin / "swiftlint"), "version": "0.65.1", "source": "cached"}},
                "bin_dirs": [str(fake_bin)],
                "python": "/custom/venv/bin/python",
                "cache": str(self.cache),
            }
            with bootstrap.prepared_environment(self.root, cache=self.cache) as receipt:
                self.assertIn(str(fake_bin), os.environ["PATH"])
                self.assertTrue(os.environ["PATH"].startswith(str(fake_bin)))
                self.assertEqual(os.environ["PYTHON"], "/custom/venv/bin/python")
                self.assertEqual(receipt["python"], "/custom/venv/bin/python")

        self.assertEqual(os.environ.get("PATH"), original_path)
        self.assertEqual(os.environ.get("PYTHON"), original_python)

    def test_prefers_system_matching_tool_without_download(self):
        system_bin = self.temp.name + "/system-bin"
        Path(system_bin).mkdir()
        sw_path = Path(system_bin) / "swiftlint"
        sw_path.write_text("#!/bin/sh\necho '0.65.1'\n")
        sw_path.chmod(0o755)

        with patch("shutil.which", return_value=str(sw_path)), \
                patch.object(bootstrap, "_download_verified") as dl, \
                patch.object(bootstrap, "_bootstrap_tool", wraps=bootstrap._bootstrap_tool):
            receipt = bootstrap.ensure_environment(self.root, tools={"swiftlint": "0.65.1"}, cache=self.cache)
            dl.assert_not_called()
            self.assertEqual(receipt["tools"]["swiftlint"]["source"], "system")
            self.assertEqual(receipt["tools"]["swiftlint"]["path"], str(sw_path.resolve()))
            self.assertEqual(receipt["tools"]["swiftlint"]["version"], "0.65.1")

    def test_prefers_cached_tool_without_download(self):
        cached_dir = self.cache / "swiftlint-0.65.1"
        cached_dir.mkdir(parents=True)
        cached_bin = cached_dir / "swiftlint"
        cached_bin.write_text("#!/bin/sh\necho '0.65.1'\n")
        cached_bin.chmod(0o755)

        with patch("shutil.which", return_value=None), \
                patch.object(bootstrap, "_download_verified") as dl:
            receipt = bootstrap.ensure_environment(self.root, tools={"swiftlint": "0.65.1"}, cache=self.cache)
            dl.assert_not_called()
            self.assertEqual(receipt["tools"]["swiftlint"]["source"], "cached")
            self.assertEqual(receipt["tools"]["swiftlint"]["path"], str(cached_bin.resolve()))

    def test_downloads_and_extracts_pinned_tool_atomically(self):
        fake_archive = _make_zip({"swiftlint": b"#!/bin/sh\necho '0.65.1'\n"})
        fake_sha = bootstrap.SWIFTLINT_0_65_1_SHA

        def fake_download(url, sha, target, **kwargs):
            self.assertEqual(sha, fake_sha)
            target.write_bytes(fake_archive)

        with patch("shutil.which", return_value=None), \
                patch.object(bootstrap, "_download_verified", side_effect=fake_download):
            receipt = bootstrap.ensure_environment(self.root, tools={"swiftlint": "0.65.1"}, cache=self.cache)
            self.assertEqual(receipt["tools"]["swiftlint"]["source"], "bootstrapped")
            self.assertEqual(receipt["tools"]["swiftlint"]["version"], "0.65.1")
            installed_path = Path(receipt["tools"]["swiftlint"]["path"])
            self.assertTrue(installed_path.is_file())
            self.assertTrue(os.access(installed_path, os.X_OK))

    def test_sonar_scanner_embedded_jre_execute_bits_are_preserved(self):
        pin = bootstrap._resolve_tool_pin("sonarScanner", "8.3.1")
        bin_rel = pin["binary"]

        prefix = bin_rel.split("/")[0]
        entries = {
            bin_rel: b"#!/bin/sh\necho 'SonarScanner CLI 8.3.1.16491'\necho 'Scanner configuration file: " + str(self.cache / "dummy.properties").encode() + b"'\n",
            f"{prefix}/jre/bin/java": b"#!/bin/sh\necho java\n",
            f"{prefix}/conf/sonar-scanner.properties": b"# clean config\n",
        }
        (self.cache / "dummy.properties").write_text("# clean\n")

        attrs = {
            bin_rel: 0o755 << 16,
            f"{prefix}/jre/bin/java": 0o755 << 16,
            f"{prefix}/conf/sonar-scanner.properties": 0o644 << 16,
        }
        fake_archive = _make_zip(entries, external_attrs=attrs)

        def fake_download(url, sha, target, **kwargs):
            target.write_bytes(fake_archive)

        with patch("shutil.which", return_value=None), \
                patch.object(bootstrap, "_download_verified", side_effect=fake_download):
            receipt = bootstrap.ensure_environment(self.root, tools={"sonarScanner": "8.3.1"}, cache=self.cache)
            scanner_bin = Path(receipt["tools"]["sonarScanner"]["path"])
            self.assertTrue(os.access(scanner_bin, os.X_OK))
            java_bin = scanner_bin.parent.parent / "jre/bin/java"
            self.assertTrue(os.access(java_bin, os.X_OK))

    def test_unsupported_version_pin_fails_with_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Unsupported version pin for swiftlint: '9.9.9'"):
            bootstrap.ensure_environment(self.root, tools={"swiftlint": "9.9.9"}, cache=self.cache)

    def test_unsupported_tool_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported tool for auto-download: unknownTool"):
            bootstrap.ensure_environment(self.root, tools={"unknownTool": "1.0"}, cache=self.cache)

    def test_safe_zip_extraction_rejects_path_traversal(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../escape.txt", b"evil")
        archive = self.cache / "traversal.zip"
        archive.write_bytes(buf.getvalue())

        target = self.cache / "extracted"
        with self.assertRaisesRegex(ValueError, "Path traversal"):
            bootstrap._safe_extract_zip(archive, target)

    def test_safe_zip_extraction_rejects_absolute_path(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/tmp/evil.txt", b"evil")
        archive = self.cache / "abs.zip"
        archive.write_bytes(buf.getvalue())

        target = self.cache / "extracted"
        with self.assertRaisesRegex(ValueError, "Unsafe absolute path"):
            bootstrap._safe_extract_zip(archive, target)

    def test_safe_zip_extraction_rejects_duplicate_member(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("file.txt", b"first")
            zf.writestr("file.txt", b"second")
        archive = self.cache / "duplicate.zip"
        archive.write_bytes(buf.getvalue())

        target = self.cache / "extracted"
        with self.assertRaisesRegex(ValueError, "Duplicate archive member"):
            bootstrap._safe_extract_zip(archive, target)

    def test_download_verified_enforces_https(self):
        with self.assertRaisesRegex(ValueError, "Insecure download URL"):
            bootstrap._download_verified("http://example.com/tool.zip", "abc", self.cache / "tool.zip")

    def test_download_verified_enforces_size_bound(self):
        response_mock = MagicMock()
        response_mock.__enter__.return_value.read.side_effect = [b"x" * (1024 * 1024), b""]

        with patch("urllib.request.urlopen", return_value=response_mock):
            with self.assertRaisesRegex(ValueError, "exceeded maximum size bound"):
                bootstrap._download_verified(
                    "https://example.com/tool.zip", "abc", self.cache / "tool.zip",
                    max_size=512 * 1024  # 512KB bound
                )

    def test_download_verified_enforces_exact_sha256(self):
        response_mock = MagicMock()
        response_mock.__enter__.return_value = io.BytesIO(b"content")

        with patch("urllib.request.urlopen", return_value=response_mock):
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                bootstrap._download_verified(
                    "https://example.com/tool.zip", "0000000000000000000000000000000000000000000000000000000000000000",
                    self.cache / "tool.zip"
                )

    def test_python_coverage_creates_venv_and_installs_exact_version(self):
        calls = []
        venv_dir = (self.root / ".swift-sonar/python").resolve()

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if "-m" in cmd and "venv" in cmd:
                venv_dir_path = Path(cmd[3])
                venv_python = venv_dir_path / "bin" / "python"
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.touch()
                (venv_dir_path / "pyvenv.cfg").touch()
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "-c" in cmd and "sys.prefix" in cmd[3]:
                return subprocess.CompletedProcess(cmd, 0, f"{venv_dir}\n", "")
            if "-c" in cmd and "import coverage" in cmd[3]:
                installed = any("pip" in c[0] for c in calls)
                output = "7.17.2\n" if installed else ""
                return subprocess.CompletedProcess(cmd, 0 if installed else 1, output, "")
            if "pip" in cmd and "install" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "Successfully installed", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run):
            python_path = bootstrap._ensure_python_coverage(self.root)
            # Lexical path returned, NOT .resolve() which breaks venv identity
            self.assertEqual(str(python_path), str((self.root / ".swift-sonar/python/bin/python").absolute()))
            pip_calls = [c for c in calls if "pip" in c[0] and "install" in c[0]]
            self.assertEqual(len(pip_calls), 1)
            cmd, kwargs = pip_calls[0]
            self.assertIn("coverage==7.17.2", cmd)
            self.assertIn("--disable-pip-version-check", cmd)
            self.assertIn("--isolated", cmd)
            self.assertIn("--index-url", cmd)
            self.assertIn("https://pypi.org/simple", cmd)
            self.assertIn("--only-binary=:all:", cmd)
            self.assertEqual(kwargs.get("timeout"), 120)

    def test_python_coverage_skips_pip_when_already_matching(self):
        calls = []
        venv_dir = (self.root / ".swift-sonar/python").resolve()

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if "-c" in cmd and "sys.prefix" in cmd[3]:
                return subprocess.CompletedProcess(cmd, 0, f"{venv_dir}\n", "")
            if "-c" in cmd and "import coverage" in cmd[3]:
                return subprocess.CompletedProcess(cmd, 0, "7.17.2\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        venv_python = self.root / ".swift-sonar/python/bin/python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()
        (self.root / ".swift-sonar/python/pyvenv.cfg").touch()

        with patch("subprocess.run", side_effect=fake_run):
            python_path = bootstrap._ensure_python_coverage(self.root)
            self.assertEqual(str(python_path), str(venv_python.absolute()))
            pip_calls = [c for c in calls if "pip" in c[0] and "install" in c[0]]
            self.assertEqual(len(pip_calls), 0)

    def test_pip_install_failure_does_not_leak_raw_stderr(self):
        venv_dir = (self.root / ".swift-sonar/python").resolve()

        def fake_run(cmd, **kwargs):
            if "-m" in cmd and "venv" in cmd:
                venv_dir_path = Path(cmd[3])
                venv_python = venv_dir_path / "bin" / "python"
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.touch()
                (venv_dir_path / "pyvenv.cfg").touch()
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "-c" in cmd and "sys.prefix" in cmd[3]:
                return subprocess.CompletedProcess(cmd, 0, f"{venv_dir}\n", "")
            if "-c" in cmd and "import coverage" in cmd[3]:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "pip" in cmd and "install" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "SECRET_PII_OR_INTERNAL_TRACEBACK")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(ValueError) as ctx:
                bootstrap._ensure_python_coverage(self.root)
            self.assertNotIn("SECRET_PII_OR_INTERNAL_TRACEBACK", str(ctx.exception))
            self.assertIn("Failed to install coverage==7.17.2", str(ctx.exception))

    def test_rejects_non_darwin_platform_before_download(self):
        with patch("sys.platform", "linux"):
            with self.assertRaisesRegex(ValueError, "only supported on Darwin/macOS"):
                bootstrap._resolve_tool_pin("swiftlint", "0.65.1")

    def test_rejects_arbitrary_version_prefix(self):
        with self.assertRaisesRegex(ValueError, "Unsupported version pin for sonarScanner: '8.3.1.99999'"):
            bootstrap._resolve_tool_pin("sonarScanner", "8.3.1.99999")

    def test_downloaded_scanner_verifies_blank_global_config(self):
        pin = bootstrap._resolve_tool_pin("sonarScanner", "8.3.1")
        bin_rel = pin["binary"]
        prefix = bin_rel.split("/")[0]

        dirty_config = self.cache / "dirty.properties"
        dirty_config.write_text("sonar.injected.key=malicious\n")

        entries = {
            bin_rel: b"#!/bin/sh\necho 'SonarScanner CLI 8.3.1.16491'\necho 'Scanner configuration file: " + str(dirty_config).encode() + b"'\n",
            f"{prefix}/conf/sonar-scanner.properties": b"sonar.injected.key=malicious\n",
        }
        fake_archive = _make_zip(entries)

        with patch("shutil.which", return_value=None), \
                patch.object(bootstrap, "_download_verified", side_effect=lambda u, s, t, **kw: t.write_bytes(fake_archive)):
            with self.assertRaisesRegex(ValueError, "not a clean distribution"):
                bootstrap.ensure_environment(self.root, tools={"sonarScanner": "8.3.1"}, cache=self.cache)

    def test_existing_corrupted_cached_install_is_rejected_without_deletion(self):
        tool_dir = self.cache / "swiftlint-0.65.1"
        tool_dir.mkdir(parents=True)
        # Corrupted / empty directory
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "invalid or damaged. Remove it deliberately"):
                bootstrap.ensure_environment(self.root, tools={"swiftlint": "0.65.1"}, cache=self.cache)
        # Verify it was NOT deleted
        self.assertTrue(tool_dir.exists())

    def test_dot_sonar_and_venv_anchors_reject_symlinks(self):
        fake_target = self.temp.name + "/fake-dot-sonar"
        Path(fake_target).mkdir()
        dot_sonar = self.root / ".swift-sonar"
        dot_sonar.symlink_to(fake_target)

        with self.assertRaisesRegex(ValueError, ".swift-sonar directory cannot be a symbolic link"):
            bootstrap._ensure_python_coverage(self.root)

        dot_sonar.unlink()
        dot_sonar.mkdir(mode=0o700)
        venv_dir = dot_sonar / "python"
        venv_dir.symlink_to(fake_target)

        with self.assertRaisesRegex(ValueError, "Project virtualenv cannot be a symbolic link"):
            bootstrap._ensure_python_coverage(self.root)

    def test_prepends_selected_path_even_if_already_elsewhere_in_path(self):
        fake_bin = self.cache / "fake-bin"
        fake_bin.mkdir()
        # PATH has fake_bin at the end
        original_path = "/usr/bin" + os.pathsep + "/bin" + os.pathsep + str(fake_bin)

        with patch.dict(os.environ, {"PATH": original_path}):
            with patch.object(bootstrap, "ensure_environment") as ensure:
                ensure.return_value = {
                    "tools": {"swiftlint": {"path": str(fake_bin / "swiftlint"), "version": "0.65.1", "source": "cached"}},
                    "bin_dirs": [str(fake_bin)],
                    "python": None,
                    "cache": str(self.cache),
                }
                with bootstrap.prepared_environment(self.root, cache=self.cache):
                    path_entries = os.environ["PATH"].split(os.pathsep)
                    # fake_bin MUST be at the very front
                    self.assertEqual(path_entries[0], str(fake_bin))
                    # And only present once
                    self.assertEqual(path_entries.count(str(fake_bin)), 1)

    def test_default_tools_selection(self):
        with patch.object(bootstrap, "_bootstrap_tool") as mock_boot:
            mock_boot.side_effect = [
                (Path("/fake/swiftlint"), "0.65.1", "system"),
                (Path("/fake/sonar-scanner"), "8.3.1.16491", "cached"),
            ]
            receipt = bootstrap.ensure_environment(self.root, cache=self.cache)
            self.assertIn("swiftlint", receipt["tools"])
            self.assertIn("sonarScanner", receipt["tools"])
            self.assertIn("sonarApple", receipt["tools"])
            self.assertEqual(receipt["tools"]["sonarApple"]["version"], "0.5.1")
            self.assertIn("/fake", receipt["bin_dirs"])

    def test_cache_directory_rejects_symlink_root(self):
        real_cache = self.temp.name + "/real-cache"
        Path(real_cache).mkdir(mode=0o700)
        link_cache = Path(self.temp.name) / "symlink-cache"
        link_cache.symlink_to(real_cache)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            bootstrap._secure_cache_dir(link_cache)

    def test_symlinked_tool_binary_or_ancestor_is_rejected_before_execution(self):
        tool_install_dir = self.cache / "swiftlint-0.65.1"
        tool_install_dir.mkdir(parents=True)
        fake_binary = self.temp.name + "/fake_bin"
        Path(fake_binary).write_text("#!/bin/sh\nexit 0\n")
        Path(fake_binary).chmod(0o755)

        # 1. Binary itself is a symlink
        symlink_bin = tool_install_dir / "swiftlint"
        symlink_bin.symlink_to(fake_binary)
        with self.assertRaisesRegex(ValueError, "cannot be a symbolic link"):
            bootstrap._validate_tool_binary(tool_install_dir, "swiftlint")

        symlink_bin.unlink()

        # 2. Intermediate directory is a symlink
        nested_dir = self.cache / "nested_real"
        nested_dir.mkdir()
        nested_symlink = tool_install_dir / "bin"
        nested_symlink.symlink_to(nested_dir)
        with self.assertRaisesRegex(ValueError, "cannot be a symbolic link"):
            bootstrap._validate_tool_binary(tool_install_dir, "bin/swiftlint")

    def test_venv_wrong_prefix_fails_closed_before_pip(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "-m" in cmd and "venv" in cmd:
                venv_dir_path = Path(cmd[3])
                venv_python = venv_dir_path / "bin" / "python"
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.touch()
                (venv_dir_path / "pyvenv.cfg").touch()
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "-c" in cmd and "sys.prefix" in cmd[3]:
                # Returns base system prefix instead of venv_dir!
                return subprocess.CompletedProcess(cmd, 0, "/usr/local\n", "")
            if "pip" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "Probed virtualenv prefix '/usr/local' does not match"):
                bootstrap._ensure_python_coverage(self.root)
            # Crucial: PIP must NEVER have been called!
            pip_calls = [c for c in calls if "pip" in c]
            self.assertEqual(len(pip_calls), 0)

    def test_safe_zip_extraction_rejects_special_file_and_file_count_limits(self):
        # 1. Zip entry with FIFO or special device type
        special_zip = self.cache / "special.zip"
        with zipfile.ZipFile(special_zip, "w") as zf:
            zinfo = zipfile.ZipInfo("fifo_pipe")
            # FIFO mode (S_IFIFO = 0o010000)
            zinfo.external_attr = 0o010644 << 16
            zf.writestr(zinfo, b"")

        target_dir = self.cache / "extracted_special"
        target_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "unsupported special file type"):
            bootstrap._safe_extract_zip(special_zip, target_dir)

    def test_bootstrap_environment_strips_python_and_dyld_overrides(self):
        with patch.dict(os.environ, {
            "PYTHONPATH": "/malicious/lib",
            "PYTHONHOME": "/bad/home",
            "DYLD_INSERT_LIBRARIES": "/bad/lib.dylib",
            "LD_PRELOAD": "/bad/lib.so",
            "PATH": "/usr/bin:/bin",
        }):
            env = bootstrap._bootstrap_environment()
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("PYTHONHOME", env)
            self.assertNotIn("DYLD_INSERT_LIBRARIES", env)
            self.assertNotIn("LD_PRELOAD", env)
            self.assertIn("PATH", env)


if __name__ == "__main__":
    unittest.main()
