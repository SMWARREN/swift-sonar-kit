"""Secure automatic tool bootstrapping and environment preparation.

SPDX-License-Identifier: GPL-3.0-only
"""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

# Pinned official release artifacts with verified checksums.
# Unknown versions fail closed with an actionable error and are never silently upgraded.
SWIFTLINT_0_65_1_SHA = "c1e429b0599cf1b516f369a2d9ec04eaf0e436f3c12b637df8851fa52ff694d0"
SONAR_SCANNER_8_3_1_ARM64_SHA = "feea9890c330c6ceb32333cb22629bec2241607cdf1cd1129208bb5ecd77f5da"
SONAR_SCANNER_8_3_1_X64_SHA = "25a5d51c89ad500c1a5d40e13763b2ef168b187f8a024a0590664f4a1897b55a"

PINS = {
    "swiftlint": {
        "0.65.1": {
            "all": {
                "url": "https://github.com/realm/SwiftLint/releases/download/0.65.1/portable_swiftlint.zip",
                "sha256": SWIFTLINT_0_65_1_SHA,
                "size": 11952647,
                "binary": "swiftlint",
                "version_args": ["version"],
                "version_pattern": r"(?m)^([0-9]+\.[0-9]+\.[0-9]+)\s*$",
                "version": "0.65.1",
            }
        }
    },
    "sonarScanner": {
        "8.3.1": {
            "arm64": {
                "url": "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-8.3.1.16491-macosx-aarch64.zip",
                "sha256": SONAR_SCANNER_8_3_1_ARM64_SHA,
                "binary": "sonar-scanner-8.3.1.16491-macosx-aarch64/bin/sonar-scanner",
                "version_args": ["--version"],
                "version_pattern": r"SonarScanner CLI ([0-9.]+)",
                "version": "8.3.1.16491",
            },
            "x64": {
                "url": "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-8.3.1.16491-macosx-x64.zip",
                "sha256": SONAR_SCANNER_8_3_1_X64_SHA,
                "binary": "sonar-scanner-8.3.1.16491-macosx-x64/bin/sonar-scanner",
                "version_args": ["--version"],
                "version_pattern": r"SonarScanner CLI ([0-9.]+)",
                "version": "8.3.1.16491",
            },
        },
        "8.3.1.16491": {
            "arm64": {
                "url": "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-8.3.1.16491-macosx-aarch64.zip",
                "sha256": SONAR_SCANNER_8_3_1_ARM64_SHA,
                "binary": "sonar-scanner-8.3.1.16491-macosx-aarch64/bin/sonar-scanner",
                "version_args": ["--version"],
                "version_pattern": r"SonarScanner CLI ([0-9.]+)",
                "version": "8.3.1.16491",
            },
            "x64": {
                "url": "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-8.3.1.16491-macosx-x64.zip",
                "sha256": SONAR_SCANNER_8_3_1_X64_SHA,
                "binary": "sonar-scanner-8.3.1.16491-macosx-x64/bin/sonar-scanner",
                "version_args": ["--version"],
                "version_pattern": r"SonarScanner CLI ([0-9.]+)",
                "version": "8.3.1.16491",
            },
        },
    },
}

DEFAULT_TOOLS = {
    "swiftlint": "0.65.1",
    "sonarScanner": "8.3.1",
    "sonarApple": "0.5.1",
}

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 180
MAX_EXTRACT_BYTES = 1024 * 1024 * 1024  # 1 GB
MAX_EXTRACT_FILES = 50_000


def _child_environment() -> dict:
    try:
        from .project_runner import child_environment
        return child_environment()
    except Exception:
        return {"PATH": os.environ.get("PATH", "")}


def _bootstrap_environment() -> dict:
    env = _child_environment()
    for key in list(env.keys()):
        if key.startswith(("PYTHON", "DYLD_", "LD_")):
            del env[key]
    return env


def _current_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x64"
    raise ValueError(f"Unsupported system architecture for bootstrap: {machine}")


def _secure_cache_dir(cache_path: Path | None) -> Path:
    if cache_path is None:
        raw_path = Path.home() / ".cache" / "swift-sonar-kit" / "tools"
    else:
        raw_path = Path(cache_path)

    # Validate ancestors for symlinks and unsafe permissions without .resolve() erasing them.
    # On macOS, temp dirs live under /var/folders/... where /var is a symlink to private/var.
    current = Path("/")
    for part in raw_path.parts:
        if part == "/":
            continue
        current = current / part
        if current.exists():
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode):
                if str(current) in ("/var", "/private/var") and os.readlink(current) in ("private/var", "/private/var"):
                    continue
                raise ValueError(f"Cache path cannot traverse symbolic link: {current}")

            # Ancestor owner & non-shared writable checks
            if current != raw_path:
                if st.st_uid not in (os.getuid(), 0):
                    raise ValueError(f"Cache ancestor {current} is owned by untrusted uid {st.st_uid}")
                if st.st_mode & 0o002 != 0 and not (st.st_mode & stat.S_ISVTX):
                    raise ValueError(f"Cache ancestor {current} is world-writable without sticky bit")

    raw_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(raw_path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"Cache directory cannot be a symbolic link: {raw_path}")
    if st.st_uid != os.getuid():
        raise ValueError(f"Cache directory {raw_path} is not owned by current user (uid {os.getuid()})")
    if st.st_mode & 0o077 != 0:
        try:
            os.chmod(raw_path, 0o700)
            st = os.lstat(raw_path)
        except OSError:
            pass
        if st.st_mode & 0o077 != 0:
            raise ValueError(f"Cache directory {raw_path} has unsafe permissions: {oct(st.st_mode & 0o777)} (must be 0700)")

    return raw_path


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        seen = set()
        total_uncompressed = 0
        infolist = zf.infolist()
        if len(infolist) > MAX_EXTRACT_FILES:
            raise ValueError(f"Archive entry count exceeds limit of {MAX_EXTRACT_FILES}: {len(infolist)}")

        for member in infolist:
            name = member.filename
            if not name:
                continue
            if name.startswith("/") or name.startswith("\\"):
                raise ValueError(f"Unsafe absolute path in archive: {name}")
            parts = Path(name).parts
            if ".." in parts or "." in parts[:-1]:
                raise ValueError(f"Path traversal detected in archive entry: {name}")
            if name in seen:
                raise ValueError(f"Duplicate archive member detected: {name}")
            seen.add(name)

            total_uncompressed += member.file_size
            if total_uncompressed > MAX_EXTRACT_BYTES:
                raise ValueError(f"Archive uncompressed size exceeds limit of {MAX_EXTRACT_BYTES} bytes")

            dest = (target_dir / name).resolve()
            try:
                dest.relative_to(target_dir)
            except ValueError:
                raise ValueError(f"Archive entry escapes destination directory: {name}")

            # Check member type: reject special files (FIFO, device, socket)
            type_bits = (member.external_attr >> 16) & 0o170000
            is_dir = member.is_dir() or type_bits == 0o040000
            is_symlink = type_bits == 0o120000
            is_regular = type_bits == 0o100000 or (type_bits == 0 and not is_dir)
            if not (is_dir or is_symlink or is_regular):
                raise ValueError(f"Archive entry has unsupported special file type: {name} ({oct(type_bits)})")

            if is_dir:
                dest.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if is_symlink:
                link_target = zf.read(member).decode("utf-8")
                target_resolved = (dest.parent / link_target).resolve()
                try:
                    target_resolved.relative_to(target_dir)
                except ValueError:
                    raise ValueError(f"Symlink escapes destination directory: {name} -> {link_target}")
                dest.symlink_to(link_target)
            else:
                with zf.open(member, "r") as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    if mode & 0o111:
                        os.chmod(dest, mode | 0o700)
                    else:
                        os.chmod(dest, mode)
                elif dest.name in ("swiftlint", "sonar-scanner", "java"):
                    os.chmod(dest, 0o755)


def _download_verified(url: str, expected_sha256: str, target_file: Path,
                       max_size: int = MAX_DOWNLOAD_BYTES, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"Insecure download URL rejected: {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "swift-sonar-kit-bootstrap"})
    hasher = hashlib.sha256()
    total_bytes = 0
    deadline = time.monotonic() + timeout

    fd, temp_path_str = tempfile.mkstemp(prefix=".dl-", dir=target_file.parent)
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(fd, "wb") as out_stream:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if not final_url.startswith("https://"):
                    raise ValueError(f"Insecure redirect to non-HTTPS URL: {final_url}")
                while True:
                    if time.monotonic() > deadline:
                        raise ValueError(f"Download exceeded deadline of {timeout}s")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        raise ValueError(f"Download exceeded maximum size bound of {max_size} bytes")
                    hasher.update(chunk)
                    out_stream.write(chunk)
        digest = hasher.hexdigest().lower()
        if digest != expected_sha256.lower():
            raise ValueError(f"Checksum mismatch for {url}: expected {expected_sha256}, got {digest}")
        os.replace(temp_path, target_file)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _probe_tool(executable: str | Path, args: list[str], pattern: str, root: Path) -> str | None:
    env = _child_environment()
    try:
        res = subprocess.run([str(executable), *args], cwd=root, env=env,
                             capture_output=True, text=True, timeout=10, check=False)
        if res.returncode == 0:
            m = re.search(pattern, res.stdout + res.stderr)
            if m:
                return m.group(1).strip()
    except Exception:
        return None
    return None


def _check_scanner_global_config(executable: str | Path, root: Path) -> bool:
    env = _child_environment()
    try:
        res = subprocess.run([str(executable), "--version"], cwd=root, env=env,
                             capture_output=True, text=True, timeout=10, check=False)
        if res.returncode != 0:
            return False
        output = res.stdout + res.stderr
        m = re.search(r"Scanner configuration file: ([^\r\n]+)", output)
        if not m:
            return False
        cfg = Path(m.group(1).strip())
        if not cfg.is_file():
            return False
        for line in cfg.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith(("#", "!")):
                return False
        return True
    except Exception:
        return False


def _validate_tool_binary(tool_install_dir: Path, binary_rel: str) -> Path | None:
    if not tool_install_dir.exists():
        return None
    st_dir = os.lstat(tool_install_dir)
    if stat.S_ISLNK(st_dir.st_mode):
        raise ValueError(f"Tool installation directory cannot be a symbolic link: {tool_install_dir}")
    if not stat.S_ISDIR(st_dir.st_mode):
        raise ValueError(f"Tool installation path is not a directory: {tool_install_dir}")
    if st_dir.st_mode & 0o022 != 0:
        raise ValueError(f"Tool installation directory has unsafe permissions (writable by others): {tool_install_dir}")

    parts = Path(binary_rel).parts
    current = tool_install_dir
    for part in parts[:-1]:
        current = current / part
        if not current.exists():
            return None
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"Tool binary ancestor cannot be a symbolic link: {current}")
        if not stat.S_ISDIR(st.st_mode):
            return None
        if st.st_mode & 0o022 != 0:
            raise ValueError(f"Tool directory has unsafe permissions: {current}")

    target_bin = tool_install_dir / binary_rel
    if not target_bin.exists():
        return None
    st_bin = os.lstat(target_bin)
    if stat.S_ISLNK(st_bin.st_mode):
        raise ValueError(f"Tool executable cannot be a symbolic link: {target_bin}")
    if not stat.S_ISREG(st_bin.st_mode):
        raise ValueError(f"Tool executable is not a regular file: {target_bin}")
    if not os.access(target_bin, os.X_OK):
        return None

    return target_bin


def _resolve_tool_pin(tool_name: str, requested_version: str) -> dict:
    if sys.platform != "darwin":
        raise ValueError(f"Automatic bootstrap of macOS binaries is only supported on Darwin/macOS, not '{sys.platform}'")

    tool_pins = PINS.get(tool_name)
    if not tool_pins:
        raise ValueError(f"Unsupported tool for auto-download: {tool_name}. Only verified tools are supported.")

    # Exact supported pins only! Reject arbitrary extensions like 8.3.1.99999
    pin_entry = tool_pins.get(requested_version)
    if not pin_entry:
        raise ValueError(
            f"Unsupported version pin for {tool_name}: '{requested_version}'. "
            f"Only verified pinned versions ({', '.join(sorted(tool_pins.keys()))}) can be auto-bootstrapped."
        )
    arch = _current_arch()
    if "all" in pin_entry:
        return pin_entry["all"]
    if arch in pin_entry:
        return pin_entry[arch]
    raise ValueError(f"No binary pin for {tool_name} {requested_version} on architecture '{arch}'")


def _bootstrap_tool(tool_name: str, requested_version: str, cache_dir: Path, root: Path) -> tuple[Path, str, str]:
    pin = _resolve_tool_pin(tool_name, requested_version)
    expected_version = pin["version"]
    pattern = pin["version_pattern"]
    version_args = pin["version_args"]
    binary_rel = pin["binary"]

    # 1. Probe system installed tool first
    cmd_name = "swiftlint" if tool_name == "swiftlint" else "sonar-scanner"
    system_path = shutil.which(cmd_name)
    if system_path:
        observed = _probe_tool(system_path, version_args, pattern, root)
        if observed and (observed == expected_version or observed == requested_version or observed.startswith(requested_version + ".")):
            if tool_name != "sonarScanner" or _check_scanner_global_config(system_path, root):
                print(f"[setup] Reusing {tool_name} {observed} (system).", file=sys.stderr)
                return Path(system_path).resolve(), observed, "system"

    # 2. Check cached installation with lexical path and symlink validation
    tool_install_dir = cache_dir / f"{tool_name}-{expected_version}"
    if tool_install_dir.exists():
        validated_bin = _validate_tool_binary(tool_install_dir, binary_rel)
        if validated_bin:
            observed = _probe_tool(validated_bin, version_args, pattern, root)
            if observed and (observed == expected_version or observed == requested_version or observed.startswith(requested_version + ".")):
                if tool_name != "sonarScanner" or _check_scanner_global_config(validated_bin, root):
                    print(f"[setup] Reusing {tool_name} {observed} (cached).", file=sys.stderr)
                    return validated_bin, observed, "cached"
        raise ValueError(
            f"Existing installation at {tool_install_dir} is invalid or damaged. "
            f"Remove it deliberately before bootstrapping."
        )

    # 3. Download and extract atomically with exclusive lock
    lock_file = cache_dir / f"{tool_name}-{expected_version}.lock"
    try:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise ValueError(f"Failed to open lock file securely: {lock_file}") from e

    try:
        st = os.fstat(lock_fd)
        if st.st_uid != os.getuid():
            raise ValueError(f"Lock file not owned by current user: {lock_file}")
        if st.st_mode & 0o077 != 0:
            os.fchmod(lock_fd, 0o600)
            st = os.fstat(lock_fd)
            if st.st_mode & 0o077 != 0:
                raise ValueError(f"Lock file has unsafe permissions: {oct(st.st_mode & 0o777)}")

        locked = False
        start_time = time.time()
        while time.time() - start_time < 15:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (BlockingIOError, OSError):
                time.sleep(0.1)
        if not locked:
            raise ValueError(f"Timed out acquiring lock on {lock_file}: another process is bootstrapping this tool")

        # Double check after acquiring lock
        if tool_install_dir.exists():
            validated_bin = _validate_tool_binary(tool_install_dir, binary_rel)
            if validated_bin:
                observed = _probe_tool(validated_bin, version_args, pattern, root)
                if observed and (observed == expected_version or observed == requested_version or observed.startswith(requested_version + ".")):
                    if tool_name != "sonarScanner" or _check_scanner_global_config(validated_bin, root):
                        print(f"[setup] Reusing {tool_name} {observed} (cached).", file=sys.stderr)
                        return validated_bin, observed, "cached"
            raise ValueError(
                f"Existing installation at {tool_install_dir} is invalid or damaged. "
                f"Remove it deliberately before bootstrapping."
            )

        print(f"[setup] Downloading {tool_name} {expected_version}...", file=sys.stderr)
        archive_file = cache_dir / f"{tool_name}-{expected_version}.zip"
        _download_verified(pin["url"], pin["sha256"], archive_file, max_size=pin.get("size", MAX_DOWNLOAD_BYTES) * 2 or MAX_DOWNLOAD_BYTES)

        temp_dir = Path(tempfile.mkdtemp(prefix=f".extract-{tool_name}-", dir=cache_dir))
        try:
            _safe_extract_zip(archive_file, temp_dir)
            extracted_bin = temp_dir / binary_rel
            if not extracted_bin.is_file():
                raise ValueError(f"Extracted archive missing expected binary: {binary_rel}")
            if not os.access(extracted_bin, os.X_OK):
                os.chmod(extracted_bin, 0o755)

            observed = _probe_tool(extracted_bin, version_args, pattern, root)
            if not observed or not (observed == expected_version or observed == requested_version or observed.startswith(requested_version + ".")):
                raise ValueError(f"Bootstrapped {tool_name} reported unexpected version: {observed} (expected {expected_version})")

            if tool_name == "sonarScanner":
                if not _check_scanner_global_config(extracted_bin, root):
                    raise ValueError("Bootstrapped SonarScanner global configuration is not a clean distribution")

            os.replace(temp_dir, tool_install_dir)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            if archive_file.exists():
                archive_file.unlink()

        final_bin = tool_install_dir / binary_rel
        return final_bin, observed, "bootstrapped"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _ensure_python_coverage(root: Path) -> Path:
    dot_sonar = root / ".swift-sonar"
    if dot_sonar.exists():
        st = os.lstat(dot_sonar)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f".swift-sonar directory cannot be a symbolic link: {dot_sonar}")
        if st.st_mode & 0o022 != 0:
            raise ValueError(f".swift-sonar directory has unsafe permissions (writable by others): {oct(st.st_mode & 0o777)}")
    dot_sonar.mkdir(parents=True, exist_ok=True, mode=0o700)

    venv_dir = dot_sonar / "python"
    if venv_dir.exists():
        st = os.lstat(venv_dir)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"Project virtualenv cannot be a symbolic link: {venv_dir}")
        if st.st_mode & 0o022 != 0:
            raise ValueError(f"Project virtualenv has unsafe permissions (writable by others): {oct(st.st_mode & 0o777)}")

    venv_bin = venv_dir / "bin"
    if venv_bin.exists():
        st = os.lstat(venv_bin)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"Project virtualenv bin directory cannot be a symbolic link: {venv_bin}")
        if st.st_mode & 0o022 != 0:
            raise ValueError(f"Project virtualenv bin directory has unsafe permissions: {oct(st.st_mode & 0o777)}")

    venv_python = venv_bin / "python"
    lexical_python = venv_python.absolute()
    env = _bootstrap_environment()

    # Create venv if absent
    if not venv_python.exists():
        print("[setup] Creating isolated project Python environment...", file=sys.stderr)
        try:
            res = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            raise ValueError(f"Timed out creating isolated virtualenv at {venv_dir}")
        if res.returncode != 0:
            raise ValueError(f"Failed to create isolated virtualenv at {venv_dir} (exit {res.returncode})")

    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if not pyvenv_cfg.exists():
        raise ValueError(f"Virtualenv at {venv_dir} missing pyvenv.cfg")
    st_cfg = os.lstat(pyvenv_cfg)
    if stat.S_ISLNK(st_cfg.st_mode) or not stat.S_ISREG(st_cfg.st_mode):
        raise ValueError(f"Virtualenv pyvenv.cfg is not a regular file: {pyvenv_cfg}")

    # Check venv prefix FIRST before any pip install!
    prefix_code = "import sys; print(sys.prefix)"
    try:
        prefix_probe = subprocess.run([str(lexical_python), "-I", "-c", prefix_code],
                                      cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError(f"Timed out probing virtualenv prefix in {lexical_python}")

    if prefix_probe.returncode != 0:
        raise ValueError(f"Failed to probe virtualenv prefix in {lexical_python} (exit {prefix_probe.returncode})")

    probed_prefix = prefix_probe.stdout.strip()
    try:
        matches_prefix = Path(probed_prefix).resolve() == venv_dir.resolve()
    except Exception:
        matches_prefix = False

    if not matches_prefix:
        raise ValueError(
            f"Probed virtualenv prefix '{probed_prefix}' does not match expected venv at {venv_dir}. "
            f"Refusing to execute pip into base interpreter."
        )

    cov_code = "import coverage; print(coverage.__version__)"
    try:
        cov_probe = subprocess.run([str(lexical_python), "-I", "-c", cov_code],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError(f"Timed out probing coverage in {lexical_python}")

    current_ver = cov_probe.stdout.strip() if cov_probe.returncode == 0 else None
    if current_ver == "7.17.2":
        print("[setup] Reusing isolated Python coverage 7.17.2.", file=sys.stderr)
    else:
        print("[setup] Preparing isolated Python coverage...", file=sys.stderr)
        install_cmd = [
            str(lexical_python), "-I", "-m", "pip", "install",
            "--disable-pip-version-check",
            "--isolated",
            "--index-url", "https://pypi.org/simple",
            "--only-binary=:all:",
            "coverage==7.17.2",
        ]
        try:
            res = subprocess.run(install_cmd, cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            raise ValueError(f"Timed out installing coverage==7.17.2 into {venv_dir}")
        if res.returncode != 0:
            raise ValueError(f"Failed to install coverage==7.17.2 into {venv_dir} (exit {res.returncode})")

        try:
            verify_probe = subprocess.run([str(lexical_python), "-I", "-c", cov_code],
                                          cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            raise ValueError(f"Timed out verifying coverage in {lexical_python}")

        if verify_probe.returncode != 0 or verify_probe.stdout.strip() != "7.17.2":
            raise ValueError(f"Verification of coverage==7.17.2 failed in isolated venv {lexical_python}")

    return lexical_python


def ensure_environment(root: Path, *, tools: dict[str, str] | None = None,
                       python_coverage: bool = False, cache: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    cache_dir = _secure_cache_dir(cache)

    if tools is None:
        req_tools = dict(DEFAULT_TOOLS)
    else:
        req_tools = dict(tools)

    for tool_name in req_tools:
        if tool_name not in ("swiftlint", "sonarScanner", "sonarApple"):
            raise ValueError(f"Unsupported tool for auto-download: {tool_name}. Only verified tools are supported.")

    resolved_tools = {}
    bin_dirs = []

    # Bootstrap swiftlint
    if "swiftlint" in req_tools:
        sw_path, sw_ver, sw_source = _bootstrap_tool("swiftlint", req_tools["swiftlint"], cache_dir, root)
        resolved_tools["swiftlint"] = {"path": str(sw_path), "version": sw_ver, "source": sw_source}
        if sw_path.parent not in bin_dirs:
            bin_dirs.append(sw_path.parent)

    # Bootstrap sonarScanner
    if "sonarScanner" in req_tools:
        sc_path, sc_ver, sc_source = _bootstrap_tool("sonarScanner", req_tools["sonarScanner"], cache_dir, root)
        resolved_tools["sonarScanner"] = {"path": str(sc_path), "version": sc_ver, "source": sc_source}
        if sc_path.parent not in bin_dirs:
            bin_dirs.append(sc_path.parent)

    # Record server plugin if present
    if "sonarApple" in req_tools:
        resolved_tools["sonarApple"] = {"version": req_tools["sonarApple"]}

    python_executable = None
    if python_coverage:
        python_executable = _ensure_python_coverage(root)

    return {
        "tools": resolved_tools,
        "bin_dirs": [str(d) for d in bin_dirs],
        "python": str(python_executable) if python_executable else None,
        "cache": str(cache_dir),
    }


@contextmanager
def prepared_environment(root: Path, *, tools: dict[str, str] | None = None,
                         python_coverage: bool = False, cache: Path | None = None):
    receipt = ensure_environment(root, tools=tools, python_coverage=python_coverage, cache=cache)
    old_path = os.environ.get("PATH")
    old_python = os.environ.get("PYTHON")

    bin_dirs = receipt.get("bin_dirs", [])
    if bin_dirs:
        current_paths = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        new_paths = list(bin_dirs) + [p for p in current_paths if p not in bin_dirs]
        os.environ["PATH"] = os.pathsep.join(new_paths)

    if receipt.get("python"):
        os.environ["PYTHON"] = receipt["python"]

    try:
        yield receipt
    finally:
        if old_path is not None:
            os.environ["PATH"] = old_path
        else:
            os.environ.pop("PATH", None)
        if old_python is not None:
            os.environ["PYTHON"] = old_python
        else:
            os.environ.pop("PYTHON", None)
