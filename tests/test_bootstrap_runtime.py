"""Tests for standalone portable Python runtime bootstrap.

SPDX-License-Identifier: GPL-3.0-only
"""

from __future__ import annotations

import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from swift_sonar_kit import bootstrap_runtime


def _make_tar_gz(members: list[dict]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for m in members:
            name = m["name"]
            content = m.get("content", b"")
            ti = tarfile.TarInfo(name=name)
            if m.get("type") == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = m.get("linkname", "target")
            elif m.get("type") == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = m.get("linkname", "target")
            else:
                ti.type = tarfile.REGTYPE
                ti.size = len(content)
                ti.mode = m.get("mode", 0o755)
            tar.addfile(ti, io.BytesIO(content) if ti.isreg() else None)
    return buf.getvalue()


class BootstrapRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_dir = Path(self.temp_dir.name) / "runtime"
        self.cache_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)

    def test_unsupported_os_rejected(self):
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported operating system 'linux'"):
                bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_unsupported_arch_rejected(self):
        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="armv7l"):
                with self.assertRaisesRegex(RuntimeError, "Unsupported architecture 'armv7l'"):
                    bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_digest_mismatch_no_execute(self):
        corrupt_bytes = b"corrupted-download-payload"
        fake_resp = MagicMock()
        fake_resp.read.side_effect = [corrupt_bytes, b""]
        fake_resp.geturl.return_value = "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz"
        fake_resp.__enter__.return_value = fake_resp

        custom_assets = {
            "arm64": {
                "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz",
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                "size": len(corrupt_bytes),
            }
        }

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch.dict(bootstrap_runtime.PINNED_UV_ASSETS, custom_assets):
                    with patch("urllib.request.urlopen", return_value=fake_resp):
                        with patch("subprocess.run") as mock_run:
                            with self.assertRaises(ValueError) as ctx:
                                bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                            self.assertIn("SHA256 digest mismatch", str(ctx.exception))
                            mock_run.assert_not_called()

        tmp_dir = self.cache_dir / "tmp"
        if tmp_dir.exists():
            remaining_dl = list(tmp_dir.glob("uv_dl_*"))
            self.assertEqual(remaining_dl, [], "Temporary download file was not cleaned up on digest mismatch")

    def test_download_size_mismatch_no_execute(self):
        short_bytes = b"short"
        fake_resp = MagicMock()
        fake_resp.read.side_effect = [short_bytes, b""]
        fake_resp.geturl.return_value = "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz"
        fake_resp.__enter__.return_value = fake_resp

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("urllib.request.urlopen", return_value=fake_resp):
                    with patch("subprocess.run") as mock_run:
                        with self.assertRaises(ValueError) as ctx:
                            bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                        self.assertIn("does not match expected size", str(ctx.exception))
                        mock_run.assert_not_called()

    def test_archive_path_traversal_rejected(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        tar_bytes = _make_tar_gz([
            {"name": "../evil/uv", "content": b"bad-binary"},
        ])
        archive_path = self.cache_dir / "tmp" / "test.tar.gz"
        archive_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        archive_path.write_bytes(tar_bytes)

        with self.assertRaisesRegex(ValueError, "Archive entry contains path traversal"):
            bootstrap_runtime._extract_uv_binary(archive_path, bin_dir)

    def test_archive_symlink_rejected(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        tar_bytes = _make_tar_gz([
            {"name": "uv-aarch64-apple-darwin/uv", "type": "symlink", "linkname": "/usr/bin/touch"},
        ])
        archive_path = self.cache_dir / "tmp" / "symlink.tar.gz"
        archive_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        archive_path.write_bytes(tar_bytes)

        with self.assertRaisesRegex(ValueError, "symlink or hardlink"):
            bootstrap_runtime._extract_uv_binary(archive_path, bin_dir)

    def test_archive_duplicate_uv_rejected(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        tar_bytes = _make_tar_gz([
            {"name": "entry1/uv", "content": b"first"},
            {"name": "entry2/uv", "content": b"second"},
        ])
        archive_path = self.cache_dir / "tmp" / "dup.tar.gz"
        archive_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        archive_path.write_bytes(tar_bytes)

        with self.assertRaisesRegex(ValueError, "Duplicate 'uv' executable entries"):
            bootstrap_runtime._extract_uv_binary(archive_path, bin_dir)

    def test_archive_absolute_path_rejected(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        tar_bytes = _make_tar_gz([
            {"name": "/usr/local/bin/uv", "content": b"payload"},
        ])
        archive_path = self.cache_dir / "tmp" / "abs.tar.gz"
        archive_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        archive_path.write_bytes(tar_bytes)

        with self.assertRaisesRegex(ValueError, "invalid or absolute path"):
            bootstrap_runtime._extract_uv_binary(archive_path, bin_dir)

    def test_cache_collision_and_shared_permissions_rejected(self):
        shared_cache = self.cache_dir / "shared_unsafe"
        shared_cache.mkdir(parents=True, mode=0o777, exist_ok=True)
        os.chmod(shared_cache, 0o777)

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "unsafe shared write permissions"):
                    bootstrap_runtime.ensure_runtime(cache=shared_cache)

    def test_cache_symlink_rejected(self):
        real_dir = self.cache_dir / "real_dir"
        real_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        symlink_dir = self.cache_dir / "symlink_dir"
        os.symlink(real_dir, symlink_dir)

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "Unsafe symlink in cache path"):
                    bootstrap_runtime.ensure_runtime(cache=symlink_dir)

    def test_cache_ancestor_symlink_rejected(self):
        real_parent = self.cache_dir / "real_parent"
        real_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        symlink_parent = self.cache_dir / "symlink_parent"
        os.symlink(real_parent, symlink_parent)
        nested_cache = symlink_parent / "nested_runtime"

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "Unsafe symlink in cache path"):
                    bootstrap_runtime.ensure_runtime(cache=nested_cache)

    def test_cache_child_dir_symlink_rejected(self):
        outside_dir = Path(self.temp_dir.name) / "outside_bin"
        outside_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        symlinked_child = self.cache_dir / "bin"
        os.symlink(outside_dir, symlinked_child)

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "Cache child directory is a symlink"):
                    bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_lock_timeout_leaves_no_success(self):
        lock_file = self.cache_dir / "runtime.lock"
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with patch.object(sys, "platform", "darwin"):
                with patch.object(platform, "machine", return_value="arm64"):
                    # Fast-forward time to immediately trigger timeout
                    with patch("time.monotonic", side_effect=[0.0, 301.0, 302.0]):
                        with self.assertRaises(TimeoutError):
                            bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_cache_non_owner_rejected(self):
        real_stat = os.stat
        target_path = str(self.cache_dir.resolve())

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            try:
                p_str = os.fspath(path)
            except Exception:
                p_str = str(path)
            if p_str == target_path:
                return os.stat_result((
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    99999,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                ))
            return st

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("os.stat", side_effect=fake_stat):
                    with self.assertRaisesRegex(RuntimeError, "owned by uid 99999"):
                        bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_full_bootstrap_and_repeat_reuse(self):
        fake_uv_bytes = b"fake-uv-exec-content"
        tar_bytes = _make_tar_gz([
            {"name": "uv-aarch64-apple-darwin/uv", "content": fake_uv_bytes, "mode": 0o755},
        ])

        custom_assets = {
            "arm64": {
                "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz",
                "sha256": bootstrap_runtime.hashlib.sha256(tar_bytes).hexdigest(),
                "size": len(tar_bytes),
            }
        }

        fake_resp = MagicMock()
        fake_resp.read.side_effect = [tar_bytes, b""]
        fake_resp.geturl.return_value = custom_assets["arm64"]["url"]
        fake_resp.__enter__.return_value = fake_resp

        python_bin = self.cache_dir / "python" / "cpython-3.12.16-apple-darwin" / "bin" / "python3"

        def fake_run(cmd, *args, **kwargs):
            cmd_strs = [str(c) for c in cmd]
            if "--version" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.7 (c949a37 2024-09-01)\n", stderr="")
            if "python" in cmd_strs and "install" in cmd_strs:
                python_bin.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                python_bin.write_text("#!/bin/sh\necho 3.12.16\n")
                os.chmod(python_bin, 0o700)
                return subprocess.CompletedProcess(cmd, 0, stdout="Installed Python 3.12.16\n", stderr="")
            if "python" in cmd_strs and "find" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{python_bin}\n", stderr="")
            if "-c" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="3.12.16\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch.dict(bootstrap_runtime.PINNED_UV_ASSETS, custom_assets):
                    with patch("urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
                        with patch("subprocess.run", side_effect=fake_run):
                            # 1. First run: bootstrap from scratch
                            ready_1 = bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                            self.assertEqual(ready_1, str(python_bin.resolve()))
                            self.assertEqual(mock_urlopen.call_count, 1)

                            # 2. Second run: repeat reuse must NOT download or install
                            mock_urlopen.reset_mock()
                            ready_2 = bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                            self.assertEqual(ready_2, str(python_bin.resolve()))
                            mock_urlopen.assert_not_called()

    def test_python_symlink_within_managed_dir_accepted(self):
        python_dir = self.cache_dir / "python"
        python_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        cpython_bin_dir = python_dir / "cpython-3.12.16" / "bin"
        cpython_bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        real_python = cpython_bin_dir / "python3.12"
        real_python.write_text("#!/bin/sh\necho 3.12.16\n")
        os.chmod(real_python, 0o700)

        symlink_python = cpython_bin_dir / "python3"
        os.symlink(real_python, symlink_python)

        clean_env = {"PATH": "/usr/bin:/bin"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [str(real_python)], 0, stdout="3.12.16\n", stderr=""
            )
            # Legitimate internal symlink should be verified and accepted
            ok = bootstrap_runtime._verify_cached_python(symlink_python, python_dir, clean_env)
            self.assertTrue(ok)

    def test_cache_tampering_hash_mismatch_fails_closed(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"tampered-binary-content")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "size": len(b"tampered-binary-content"),
        }))

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "tampering detected"):
                    bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_cache_tampering_manifest_version_fails_closed(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"binary-content")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.12.0",
            "sha256": bootstrap_runtime.hashlib.sha256(b"binary-content").hexdigest(),
            "size": len(b"binary-content"),
        }))

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with self.assertRaisesRegex(RuntimeError, "does not match expected"):
                    bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_uv_version_check_exact_mismatch_fails_closed(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"binary-content")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": bootstrap_runtime.hashlib.sha256(b"binary-content").hexdigest(),
            "size": len(b"binary-content"),
        }))

        # Version output has 0.13.70 which contains substring 0.13.7 but is NOT exact
        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.70\n", stderr="")

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("subprocess.run", side_effect=fake_run):
                    with self.assertRaisesRegex(RuntimeError, "exact version '0.13.70' does not match"):
                        bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_timeout_leaves_no_success(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"uv-binary")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": bootstrap_runtime.hashlib.sha256(b"uv-binary").hexdigest(),
            "size": len(b"uv-binary"),
        }))

        def timeout_run(cmd, *args, **kwargs):
            cmd_strs = [str(c) for c in cmd]
            if "--version" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.7\n", stderr="")
            if "install" in cmd_strs:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("subprocess.run", side_effect=timeout_run):
                    with self.assertRaises(TimeoutError):
                        bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_out_of_cache_python_rejected(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"uv-binary")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": bootstrap_runtime.hashlib.sha256(b"uv-binary").hexdigest(),
            "size": len(b"uv-binary"),
        }))

        def out_of_cache_run(cmd, *args, **kwargs):
            cmd_strs = [str(c) for c in cmd]
            if "--version" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.7\n", stderr="")
            if "install" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "find" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="/usr/bin/python3\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("subprocess.run", side_effect=out_of_cache_run):
                    with self.assertRaisesRegex(RuntimeError, "outside private cache directory"):
                        bootstrap_runtime.ensure_runtime(cache=self.cache_dir)

    def test_errors_do_not_contain_raw_child_stderr(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"uv-binary")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": bootstrap_runtime.hashlib.sha256(b"uv-binary").hexdigest(),
            "size": len(b"uv-binary"),
        }))

        def fail_run(cmd, *args, **kwargs):
            cmd_strs = [str(c) for c in cmd]
            if "--version" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.7\n", stderr="")
            if "install" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="SECRET_CREDENTIAL_IN_STDERR")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch("subprocess.run", side_effect=fail_run):
                    with self.assertRaises(RuntimeError) as ctx:
                        bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                    self.assertNotIn("SECRET_CREDENTIAL", str(ctx.exception))
                    self.assertIn("exit code 1", str(ctx.exception))

    def test_env_has_no_credentials_or_inherited_configs(self):
        bin_dir = self.cache_dir / "bin"
        bin_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_bin = bin_dir / "uv"
        uv_bin.write_bytes(b"uv-binary")
        os.chmod(uv_bin, 0o700)

        manifest = bin_dir / "uv.manifest.json"
        manifest.write_text(json.dumps({
            "version": "0.13.7",
            "sha256": bootstrap_runtime.hashlib.sha256(b"uv-binary").hexdigest(),
            "size": len(b"uv-binary"),
        }))

        python_bin = self.cache_dir / "python" / "bin" / "python3"
        python_bin.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        python_bin.write_text("#!/bin/sh\n")
        os.chmod(python_bin, 0o700)

        captured_envs = []

        def capture_run(cmd, *args, **kwargs):
            captured_envs.append(kwargs.get("env", {}))
            cmd_strs = [str(c) for c in cmd]
            if "--version" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="uv 0.13.7\n", stderr="")
            if "find" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{python_bin}\n", stderr="")
            if "-c" in cmd_strs:
                return subprocess.CompletedProcess(cmd, 0, stdout="3.12.16\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        tainted_environ = {
            "SONAR_TOKEN": "secret-sonar-token-123",
            "API_SECRET_KEY": "super-secret-key",
            "UV_CONFIG_FILE": "/path/to/uv.toml",
            "UV_INDEX_URL": "https://evil.pypi.org/simple",
            "PIP_INDEX_URL": "https://evil.pypi.org/simple",
            "HTTP_PROXY": "http://proxy.evil.org:8080",
            "HTTPS_PROXY": "http://proxy.evil.org:8080",
            "ALL_PROXY": "socks5://proxy.evil.org:1080",
            "http_proxy": "http://proxy.evil.org:8080",
            "https_proxy": "http://proxy.evil.org:8080",
            "all_proxy": "socks5://proxy.evil.org:1080",
            "PYTHONPATH": "/malicious/python/path",
            "PYTHONHOME": "/malicious/python/home",
        }

        with patch.object(sys, "platform", "darwin"):
            with patch.object(platform, "machine", return_value="arm64"):
                with patch.dict(os.environ, tainted_environ, clear=False):
                    with patch("subprocess.run", side_effect=capture_run):
                        result = bootstrap_runtime.ensure_runtime(cache=self.cache_dir)
                        self.assertEqual(result, str(python_bin.resolve()))

        self.assertTrue(len(captured_envs) > 0, "No subprocess environment captured")
        for env in captured_envs:
            self.assertNotIn("SONAR_TOKEN", env)
            self.assertNotIn("API_SECRET_KEY", env)
            self.assertNotIn("UV_CONFIG_FILE", env)
            self.assertNotIn("UV_INDEX_URL", env)
            self.assertNotIn("PIP_INDEX_URL", env)
            self.assertNotIn("HTTP_PROXY", env)
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("ALL_PROXY", env)
            self.assertNotIn("http_proxy", env)
            self.assertNotIn("https_proxy", env)
            self.assertNotIn("all_proxy", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("PYTHONHOME", env)

            self.assertIn("PATH", env)
            self.assertIn("HOME", env)
            self.assertIn("TMPDIR", env)
            self.assertIn("UV_PYTHON_INSTALL_DIR", env)
            self.assertIn("UV_CACHE_DIR", env)
            self.assertEqual(env.get("UV_NO_CONFIG"), "1")

    def test_main_cli_success(self):
        fake_python = "/path/to/cache/python/bin/python3"
        with patch.object(bootstrap_runtime, "ensure_runtime", return_value=fake_python):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                    rc = bootstrap_runtime.main(["--cache", str(self.cache_dir)])
                    self.assertEqual(rc, 0)
                    self.assertEqual(mock_stdout.getvalue(), f"{fake_python}\n")
                    self.assertEqual(mock_stderr.getvalue(), "")

    def test_main_cli_failure(self):
        with patch.object(bootstrap_runtime, "ensure_runtime", side_effect=RuntimeError("Bootstrap test failure")):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                    rc = bootstrap_runtime.main(["--cache", str(self.cache_dir)])
                    self.assertEqual(rc, 1)
                    self.assertEqual(mock_stdout.getvalue(), "")
                    self.assertIn("Bootstrap test failure", mock_stderr.getvalue())
