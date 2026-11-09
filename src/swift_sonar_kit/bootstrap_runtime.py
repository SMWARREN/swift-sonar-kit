"""Standalone portable Python runtime bootstrap for swift-sonar-kit.

SPDX-License-Identifier: GPL-3.0-only

Ensures uv and a managed CPython >= 3.11 (pinned to 3.12.16) exist in a private,
secure user cache (~/.cache/swift-sonar-kit/runtime) without modifying global PATH,
shell profiles, or system installations.

Compatible with Python 3.9+ (including Xcode system Python).
Uses standard library only; contains no package-relative imports.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

UV_VERSION = "0.13.7"
TARGET_PYTHON_VERSION = "3.12.16"

PINNED_UV_ASSETS = {
    "arm64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz",
        "sha256": "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731",
        "size": 14347867,
    },
    "aarch64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-aarch64-apple-darwin.tar.gz",
        "sha256": "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731",
        "size": 14347867,
    },
    "x86_64": {
        "url": "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-apple-darwin.tar.gz",
        "sha256": "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7",
        "size": 17026382,
    },
}

_MAC_SYSTEM_SYMLINK_ROOTS = {Path("/tmp"), Path("/var"), Path("/etc")}


def _log(message: str) -> None:
    sys.stderr.write(f"[swift-sonar-kit] {message}\n")
    sys.stderr.flush()


def _parse_uv_version(output: str) -> Optional[str]:
    """Extract exact version string from 'uv --version' output."""
    tokens = output.strip().split()
    if len(tokens) >= 2 and tokens[0] == "uv":
        return tokens[1]
    return None


def _validate_cache_path_security(cache_dir: Path) -> Path:
    raw_path = cache_dir.expanduser()

    # Reject unsafe symlinks on raw path and components
    cur = raw_path
    while cur != cur.parent:
        if cur in _MAC_SYSTEM_SYMLINK_ROOTS:
            break
        if cur.is_symlink() or os.path.islink(cur):
            raise RuntimeError(f"Unsafe symlink in cache path: {cur}")
        cur = cur.parent

    resolved = raw_path.resolve()
    current_uid = os.getuid()

    # Validate ancestors from root downward
    for parent in reversed(resolved.parents):
        if not parent.exists():
            continue
        if os.path.islink(parent):
            raise RuntimeError(f"Unsafe symlink in cache ancestor path: {parent}")
        st = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"Cache path ancestor is not a directory: {parent}")

        # Check ownership: root or current user
        if st.st_uid != current_uid and st.st_uid != 0:
            raise RuntimeError(
                f"Cache path ancestor {parent} is owned by uid {st.st_uid}, expected {current_uid} or 0"
            )

        # World-writable checks: only permissible with sticky bit (e.g. /private/tmp)
        if (st.st_mode & 0o002) != 0 and not (st.st_mode & 0o1000):
            raise RuntimeError(
                f"Cache path ancestor {parent} is world-writable without sticky bit (mode {oct(st.st_mode)})"
            )

        # User-owned directories must not be shared-writable (group/other writable)
        if st.st_uid == current_uid and (st.st_mode & 0o022) != 0:
            raise RuntimeError(
                f"Cache path ancestor {parent} has unsafe shared write permissions (mode {oct(st.st_mode)})"
            )

    if resolved.exists():
        if resolved.is_symlink() or os.path.islink(resolved):
            raise RuntimeError(f"Cache directory cannot be a symlink: {resolved}")
        st = os.stat(resolved, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"Cache path exists but is not a directory: {resolved}")
        if st.st_uid != current_uid:
            raise RuntimeError(
                f"Cache directory {resolved} is owned by uid {st.st_uid}, expected {current_uid}"
            )
        if (st.st_mode & 0o022) != 0:
            raise RuntimeError(
                f"Cache directory {resolved} has unsafe shared write permissions (mode {oct(st.st_mode)})"
            )
    else:
        resolved.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(resolved, 0o700)

    return resolved


def _ensure_private_child_dir(parent_dir: Path, child_name: str) -> Path:
    """Validate and create/chmod a private child directory without following symlinks."""
    child_path = parent_dir / child_name
    if child_path.is_symlink() or os.path.islink(child_path):
        raise RuntimeError(f"Cache child directory is a symlink: {child_path}")

    current_uid = os.getuid()
    if child_path.exists():
        st = os.stat(child_path, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"Cache child path exists but is not a directory: {child_path}")
        if st.st_uid != current_uid:
            raise RuntimeError(
                f"Cache child directory {child_path} is owned by uid {st.st_uid}, expected {current_uid}"
            )
        if (st.st_mode & 0o022) != 0:
            raise RuntimeError(
                f"Cache child directory {child_path} has unsafe shared write permissions (mode {oct(st.st_mode)})"
            )
        os.chmod(child_path, 0o700)
    else:
        os.mkdir(str(child_path), 0o700)
        os.chmod(child_path, 0o700)

    return child_path


@contextlib.contextmanager
def _acquire_cache_lock(cache_dir: Path, timeout: float = 300.0):
    lock_file = cache_dir / "runtime.lock"
    if lock_file.is_symlink() or os.path.islink(lock_file):
        raise RuntimeError(f"Lock file cannot be a symlink: {lock_file}")

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(str(lock_file), flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"Failed to open lock file safely: {exc}")

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"Lock file is not a regular file: {lock_file}")
        if st.st_uid != os.getuid():
            raise RuntimeError(f"Lock file not owned by current user (uid {st.st_uid})")
        if (st.st_mode & 0o077) != 0:
            os.fchmod(fd, 0o600)

        start = time.monotonic()
        locked = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() - start >= timeout:
                    raise TimeoutError(f"Timed out waiting for runtime bootstrap lock ({timeout}s)")
                time.sleep(0.05)
        try:
            yield
        finally:
            if locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        os.close(fd)


def _build_child_env(cache_dir: Path, python_dir: Path) -> Dict[str, str]:
    """Build a minimal, clean execution environment stripped of credentials and configs."""
    home = Path.home().resolve()
    tmp_dir = (cache_dir / "tmp").resolve()
    uv_cache = (cache_dir / "uv_cache").resolve()
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(tmp_dir),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "UV_PYTHON_INSTALL_DIR": str(python_dir.resolve()),
        "UV_CACHE_DIR": str(uv_cache),
        "UV_NO_CONFIG": "1",
    }


def _download_asset(url: str, expected_sha256: str, expected_size: int, tmp_dir: Path) -> Path:
    if not url.startswith("https://github.com/astral-sh/uv/releases/download/"):
        raise ValueError(f"Refusing to download from untrusted URL: {url}")

    temp_fd, temp_path_str = tempfile.mkstemp(dir=str(tmp_dir), prefix="uv_dl_")
    temp_path = Path(temp_path_str)
    try:
        os.fchmod(temp_fd, 0o600)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "swift-sonar-kit-bootstrap/0.1"},
        )
        hasher = hashlib.sha256()
        total_bytes = 0

        with urllib.request.urlopen(req, timeout=300) as resp:
            resp_url = resp.geturl()
            if not resp_url.startswith("https://"):
                raise ValueError(f"Insecure redirect detected: {resp_url}")

            with os.fdopen(temp_fd, "wb") as fdst:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > expected_size + 65536:
                        raise ValueError(f"Download size {total_bytes} exceeded expected size {expected_size}")
                    hasher.update(chunk)
                    fdst.write(chunk)
                fdst.flush()
                os.fsync(fdst.fileno())

        if total_bytes != expected_size:
            raise ValueError(f"Download size {total_bytes} does not match expected size {expected_size}")

        digest = hasher.hexdigest()
        if digest != expected_sha256:
            raise ValueError(f"SHA256 digest mismatch: got {digest}, expected {expected_sha256}")

        return temp_path
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _extract_uv_binary(archive_path: Path, bin_dir: Path) -> Tuple[Path, str, int]:
    uv_member = None
    with tarfile.open(name=str(archive_path), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or name.startswith("\\") or "\0" in name:
                raise ValueError(f"Archive entry has invalid or absolute path: {name}")
            parts = [p for p in name.replace("\\", "/").split("/") if p]
            if ".." in parts:
                raise ValueError(f"Archive entry contains path traversal ('..'): {name}")

            if member.issym() or member.islnk():
                raise ValueError(f"Archive entry is a symlink or hardlink: {name}")

            if parts and parts[-1] == "uv":
                if not member.isreg():
                    raise ValueError(f"Expected regular file for 'uv', got member type: {member.type}")
                if uv_member is not None:
                    raise ValueError(f"Duplicate 'uv' executable entries in archive: {name}")
                uv_member = member

        if uv_member is None:
            raise ValueError("Archive did not contain the expected 'uv' executable")

        fsrc = tar.extractfile(uv_member)
        if fsrc is None:
            raise ValueError("Failed to extract file stream for uv member")

        temp_bin_fd, temp_bin_path_str = tempfile.mkstemp(dir=str(bin_dir), prefix="uv_tmp_")
        temp_bin_path = Path(temp_bin_path_str)
        try:
            os.fchmod(temp_bin_fd, 0o700)
            hasher = hashlib.sha256()
            total_bytes = 0
            max_size = 64 * 1024 * 1024
            with os.fdopen(temp_bin_fd, "wb") as fdst:
                while True:
                    chunk = fsrc.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        raise ValueError(f"Extracted binary size {total_bytes} exceeds limit of {max_size}")
                    hasher.update(chunk)
                    fdst.write(chunk)
                fdst.flush()
                os.fsync(fdst.fileno())

            digest = hasher.hexdigest()
            dest_bin = bin_dir / "uv"
            os.replace(temp_bin_path, dest_bin)
            os.chmod(dest_bin, 0o700)
            return dest_bin, digest, total_bytes
        except Exception:
            if temp_bin_path.exists():
                try:
                    temp_bin_path.unlink()
                except OSError:
                    pass
            raise


def _write_manifest(manifest_path: Path, version: str, sha256_hash: str, size: int) -> None:
    data = {
        "version": version,
        "sha256": sha256_hash,
        "size": size,
    }
    content = json.dumps(data, indent=2).encode("utf-8")
    temp_fd, temp_path_str = tempfile.mkstemp(dir=str(manifest_path.parent), prefix="manifest_tmp_")
    temp_path = Path(temp_path_str)
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, manifest_path)
        os.chmod(manifest_path, 0o600)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _verify_cached_uv(bin_dir: Path, expected_version: str, cache_dir: Path, python_dir: Path) -> bool:
    uv_bin = bin_dir / "uv"
    manifest_path = bin_dir / "uv.manifest.json"

    if not uv_bin.exists() or not manifest_path.exists():
        return False

    if uv_bin.is_symlink() or os.path.islink(uv_bin) or manifest_path.is_symlink() or os.path.islink(manifest_path):
        raise RuntimeError("Cached uv binary or manifest is a symlink (tampering detected)")

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Cached uv manifest is corrupted (tampering detected): {exc}")

    if manifest.get("version") != expected_version:
        raise RuntimeError(
            f"Cached uv version {manifest.get('version')} does not match expected {expected_version}"
        )

    expected_sha256 = manifest.get("sha256")
    expected_size = manifest.get("size")
    if not expected_sha256 or not expected_size:
        raise RuntimeError("Cached uv manifest missing required fields (tampering detected)")

    st = os.stat(uv_bin)
    if st.st_size != expected_size:
        raise RuntimeError(
            f"Cached uv binary size {st.st_size} does not match manifest {expected_size} (tampering detected)"
        )

    hasher = hashlib.sha256()
    with open(uv_bin, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)

    if hasher.hexdigest() != expected_sha256:
        raise RuntimeError(
            f"Cached uv binary SHA256 {hasher.hexdigest()} does not match manifest {expected_sha256} (tampering detected)"
        )

    clean_env = _build_child_env(cache_dir, python_dir)
    try:
        proc = subprocess.run(
            [str(uv_bin), "--version", "--no-config"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Cached uv --version check timed out")
    except Exception as exc:
        raise RuntimeError(f"Cached uv execution failed: {exc}")

    if proc.returncode != 0:
        raise RuntimeError(f"Cached uv --version check failed (exit code {proc.returncode})")

    parsed_version = _parse_uv_version(proc.stdout)
    if parsed_version != expected_version:
        raise RuntimeError(
            f"Cached uv exact version '{parsed_version}' does not match expected '{expected_version}'"
        )

    return True


def _verify_cached_python(python_bin: Path, python_dir: Path, clean_env: Dict[str, str]) -> bool:
    if not python_bin.exists():
        return False

    resolved_dir = python_dir.resolve()
    resolved_bin = python_bin.resolve()

    # Must resolve strictly within resolved_dir
    try:
        resolved_bin.relative_to(resolved_dir)
    except ValueError:
        raise RuntimeError(
            f"Managed Python interpreter {resolved_bin} is outside private cache directory {resolved_dir}"
        )

    current_uid = os.getuid()

    # Validate directory chain between resolved binary and resolved python dir
    cur = resolved_bin.parent
    while cur != resolved_dir and cur != cur.parent:
        if cur.is_symlink() or os.path.islink(cur):
            raise RuntimeError(f"Unsafe symlink in python directory tree: {cur}")
        st = os.stat(cur, follow_symlinks=False)
        if st.st_uid != current_uid:
            raise RuntimeError(f"Python directory {cur} not owned by current user")
        if (st.st_mode & 0o022) != 0:
            raise RuntimeError(f"Python directory {cur} has unsafe shared write permissions")
        cur = cur.parent

    # Validate actual resolved binary: regular file, owned by user, private permissions, executable
    st_bin = os.stat(resolved_bin, follow_symlinks=False)
    if not stat.S_ISREG(st_bin.st_mode):
        raise RuntimeError(f"Resolved Python interpreter is not a regular file: {resolved_bin}")
    if st_bin.st_uid != current_uid:
        raise RuntimeError(f"Resolved Python interpreter {resolved_bin} not owned by current user")
    if (st_bin.st_mode & 0o022) != 0:
        raise RuntimeError(f"Resolved Python interpreter {resolved_bin} has unsafe shared write permissions")
    if not os.access(str(resolved_bin), os.X_OK):
        raise RuntimeError(f"Resolved Python interpreter {resolved_bin} is not executable")

    try:
        proc = subprocess.run(
            [
                str(resolved_bin),
                "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')",
            ],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Managed Python version check timed out")
    except Exception:
        return False

    if proc.returncode != 0:
        return False

    version_str = proc.stdout.strip()
    return version_str == TARGET_PYTHON_VERSION


def ensure_runtime(cache: Path | None = None) -> str:
    """Ensure pinned uv and managed CPython >= 3.11 are ready in private cache.

    Returns the absolute path to the ready Python executable.
    Fails closed on any security or verification mismatch.
    """
    if sys.platform != "darwin":
        raise RuntimeError(
            f"Unsupported operating system '{sys.platform}'. Bootstrap runtime only supports macOS (darwin)."
        )

    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch_key = "arm64"
    elif machine == "x86_64":
        arch_key = "x86_64"
    else:
        raise RuntimeError(
            f"Unsupported architecture '{machine}'. Bootstrap runtime only supports arm64 and x86_64 on macOS."
        )

    asset = PINNED_UV_ASSETS[arch_key]

    if cache is None:
        cache_dir = Path.home() / ".cache" / "swift-sonar-kit" / "runtime"
    else:
        cache_dir = Path(cache)

    resolved_cache = _validate_cache_path_security(cache_dir)

    bin_dir = _ensure_private_child_dir(resolved_cache, "bin")
    python_dir = _ensure_private_child_dir(resolved_cache, "python")
    _ensure_private_child_dir(resolved_cache, "uv_cache")
    tmp_dir = _ensure_private_child_dir(resolved_cache, "tmp")

    clean_env = _build_child_env(resolved_cache, python_dir)

    with _acquire_cache_lock(resolved_cache):
        uv_bin = bin_dir / "uv"
        uv_ok = False
        if uv_bin.exists():
            uv_ok = _verify_cached_uv(bin_dir, UV_VERSION, resolved_cache, python_dir)

        if uv_ok:
            try:
                find_proc = subprocess.run(
                    [str(uv_bin), "python", "find", "--managed-python", TARGET_PYTHON_VERSION, "--no-config"],
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if find_proc.returncode == 0:
                    found_path_str = find_proc.stdout.strip()
                    if found_path_str:
                        candidate_py = Path(found_path_str)
                        if _verify_cached_python(candidate_py, python_dir, clean_env):
                            return str(candidate_py.resolve())
            except subprocess.TimeoutExpired:
                raise TimeoutError("Timed out searching for existing managed Python")

        if not uv_ok:
            _log(f"Downloading uv {UV_VERSION} for {arch_key}...")
            dl_path = _download_asset(asset["url"], asset["sha256"], asset["size"], tmp_dir)
            try:
                _log(f"Extracting uv {UV_VERSION}...")
                uv_bin, extracted_sha256, extracted_size = _extract_uv_binary(dl_path, bin_dir)
                _write_manifest(bin_dir / "uv.manifest.json", UV_VERSION, extracted_sha256, extracted_size)
            finally:
                if dl_path.exists():
                    try:
                        dl_path.unlink()
                    except OSError:
                        pass

            _verify_cached_uv(bin_dir, UV_VERSION, resolved_cache, python_dir)

        _log(f"Installing managed CPython {TARGET_PYTHON_VERSION} into private runtime...")
        try:
            install_proc = subprocess.run(
                [
                    str(uv_bin),
                    "python",
                    "install",
                    TARGET_PYTHON_VERSION,
                    "--install-dir",
                    str(python_dir),
                    "--no-config",
                ],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Timed out installing managed CPython 3.12.16 (300s limit)")

        if install_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to install CPython {TARGET_PYTHON_VERSION} via uv (exit code {install_proc.returncode})"
            )

        try:
            find_proc = subprocess.run(
                [str(uv_bin), "python", "find", "--managed-python", TARGET_PYTHON_VERSION, "--no-config"],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Timed out locating installed CPython 3.12.16")

        if find_proc.returncode != 0:
            raise RuntimeError(
                f"uv python find failed to locate {TARGET_PYTHON_VERSION} (exit code {find_proc.returncode})"
            )

        found_path_str = find_proc.stdout.strip()
        if not found_path_str:
            raise RuntimeError("uv python find returned empty path")

        candidate_py = Path(found_path_str)

        if not _verify_cached_python(candidate_py, python_dir, clean_env):
            raise RuntimeError(f"Installed Python interpreter at {candidate_py} failed verification")

        resolved_py = candidate_py.resolve()
        _log(f"Ready runtime: {resolved_py}")
        return str(resolved_py)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap private portable Python runtime for swift-sonar-kit."
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Path to private cache directory (default: ~/.cache/swift-sonar-kit/runtime)",
    )
    args = parser.parse_args(argv)
    try:
        ready_python = ensure_runtime(cache=args.cache)
        sys.stdout.write(f"{ready_python}\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        sys.stderr.write(f"[swift-sonar-kit] Bootstrap runtime error: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
