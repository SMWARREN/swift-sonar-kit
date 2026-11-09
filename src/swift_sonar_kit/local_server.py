"""Opt-in, project-owned local Sonar provisioning. SPDX-License-Identifier: GPL-3.0-only.

No import-time side effects. The caller must first pass tests and strict lint.
Credentials never enter Docker arguments, source trees, or diagnostic messages.
Interrupted ambiguous token issuance fails closed, retaining private recovery state.
"""

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler

from . import server

IMAGE = "sonarqube@sha256:aa7146fd72ef79ea8ca06315ca3121db9bb1c24a83ba5255e8df67b43dee6e04"
VERSION = "26.9.0.129388"
PLUGIN_VERSION = "0.5.1"
PLUGIN_SHA256 = "c27432e52a6ebc698b77ad42bf3d98f5e34fb20d3f398799bda74fbc008f8612"
PLUGIN_SIZE = 14891363
PLUGIN_NAME = "sonar-apple-plugin-0.5.1.jar"
PLUGIN_URL = "https://github.com/insideapp-fr/sonar-apple/releases/download/0.5.1/" + PLUGIN_NAME
OWNER = "io.swift-sonar-kit"
WAIT_SECONDS = 240
DOCKER_TIMEOUT = 30
MAX_STATE = 65536
VOLUME_PARTS = ("data", "extensions", "logs", "temp")


class LocalServerError(ValueError):
    """A deliberately sanitized actionable setup failure."""


def _identity(root: Path, project_key: str) -> str:
    if (not isinstance(project_key, str) or len(project_key) > 400
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", project_key)
            or not re.search(r"[A-Za-z_.:-]", project_key)):
        raise LocalServerError("Invalid Sonar project key")
    slug = re.sub(r"[^a-z0-9]+", "-", project_key.lower()).strip("-")[:40] or "project"
    # Include the exact key too, so distinct keys with the same slug never collide.
    digest = hashlib.sha256((str(root) + "\0" + project_key).encode()).hexdigest()[:20]
    return "swift-sonar-" + slug + "-" + digest


def _private_dir(path: Path) -> Path:
    """Reject symlinks, foreign owners and permissive private directories."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LocalServerError("Private storage cannot contain symbolic links")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise LocalServerError("Private storage must be owned by this user with mode 0700")
    return path


def _storage(root: Path, identity: str) -> tuple[Path, Path]:
    home = Path.home().resolve(strict=True)
    state_base = home / ".local" / "share" / "swift-sonar-kit"
    cache = home / ".cache" / "swift-sonar-kit"
    if state_base.is_relative_to(root) or cache.is_relative_to(root):
        raise LocalServerError("Local server storage must be outside the project source tree")
    for base in (state_base, cache):
        # Create public ancestors one component at a time, never through a symlink.
        current = home
        for part in base.relative_to(home).parts[:-1]:
            current /= part
            if current.is_symlink():
                raise LocalServerError("Private storage cannot contain symbolic links")
            current.mkdir(mode=0o700, exist_ok=True)
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise LocalServerError("Private storage ancestors must be user-owned and not writable by others")
        _private_dir(base)
    parent = _private_dir(state_base / "servers")
    return _private_dir(parent / identity), cache


@contextmanager
def _directory(path: Path):
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LocalServerError("Private storage permissions changed")
        yield fd
    finally:
        os.close(fd)


def _read_file(directory: Path, name: str, limit: int, mode: int = 0o600) -> bytes | None:
    with _directory(directory) as parent:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1 or info.st_size > limit):
                raise LocalServerError("Private file ownership, permissions or size is unsafe")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise LocalServerError("Private file exceeds its size limit")
            return data


def _write_file(directory: Path, name: str, data: bytes, mode: int = 0o600) -> None:
    with _directory(directory) as parent:
        temporary = ".write-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _lock(directory: Path):
    with _directory(directory) as parent:
        fd = os.open("setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise LocalServerError("Unsafe local server lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LocalServerError("Another local server setup owns this project lock. Retry later") from None
            yield
        finally:
            os.close(fd)


def _save(directory: Path, state: dict) -> None:
    _write_file(directory, "state.json", json.dumps(state, sort_keys=True).encode())


def _docker(*args: str, timeout: int = DOCKER_TIMEOUT) -> str:
    try:
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}}
        result = subprocess.run(["docker", *args], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=False, env=environment)
    except (OSError, subprocess.SubprocessError):
        raise LocalServerError("Local Docker command failed or timed out. Private state retained") from None
    if result.returncode or len(result.stdout) > server.MAX_RESPONSE:
        raise LocalServerError("Local Docker command failed. Check Docker availability. Private state retained")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError:
        raise LocalServerError("Docker returned an invalid response") from None


def _local_docker() -> None:
    endpoint = os.environ.get("DOCKER_HOST")
    if endpoint and not endpoint.startswith("unix:///"):
        raise LocalServerError("Local setup requires a local Docker Unix socket, not a remote daemon")
    if not endpoint or os.environ.get("DOCKER_CONTEXT"):
        endpoint = json.loads(_docker("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"))
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise LocalServerError("Local setup requires a local Docker Unix socket, not a remote daemon")


def _inspect(kind: str, name: str) -> dict | None:
    options = ["--all"] if kind == "container" else []
    template = "{{.Names}}" if kind == "container" else "{{.Name}}"
    listing = _docker(kind, "ls", *options, "--format", template)
    if name not in listing.splitlines():
        return None
    result = json.loads(_docker(kind, "inspect", name))
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise LocalServerError("Docker inspection returned an unexpected shape")
    return result[0]


def _image() -> str:
    try:
        raw = _docker("image", "inspect", IMAGE)
    except LocalServerError:
        _docker("image", "pull", "--quiet", IMAGE, timeout=180)
        raw = _docker("image", "inspect", IMAGE)
    records = json.loads(raw)
    if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict):
        image = records[0]
        image_id = image.get("Id", "")
        if (re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                and IMAGE in image.get("RepoDigests", [])
                and "SONAR_VERSION=" + VERSION in image.get("Config", {}).get("Env", [])):
            return image_id
    raise LocalServerError("Pinned Sonar image digest or version did not match official identity")


class _PluginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise LocalServerError("Plugin download redirected outside official HTTPS GitHub hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _verify_plugin(data: bytes) -> bytes:
    if len(data) != PLUGIN_SIZE or hashlib.sha256(data).hexdigest() != PLUGIN_SHA256:
        raise LocalServerError("Plugin size or SHA256 verification failed")
    return data


def _plugin(cache: Path, directory: Path) -> Path:
    data = _read_file(cache, PLUGIN_NAME, PLUGIN_SIZE)
    if data is None:
        opener = server.build_opener(ProxyHandler({}), _PluginRedirect(), server.HTTPSHandler(context=ssl.create_default_context()))
        started = time.monotonic()
        try:
            with opener.open(PLUGIN_URL, timeout=15) as response:
                if response.getcode() != 200 or response.headers.get("Content-Length") not in (None, str(PLUGIN_SIZE)):
                    raise LocalServerError("Plugin download returned an unexpected response")
                chunks = []
                length = 0
                while length <= PLUGIN_SIZE and time.monotonic() - started < 90:
                    chunk = response.read(min(65536, PLUGIN_SIZE + 1 - length))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length += len(chunk)
                data = _verify_plugin(b"".join(chunks))
        except LocalServerError:
            raise
        except (OSError, ValueError):
            raise LocalServerError("Official plugin download failed or timed out") from None
        _write_file(cache, PLUGIN_NAME, data)
    _verify_plugin(data)
    # Artifact is non-secret and readable by the container UID, unlike credentials.
    installed = _read_file(directory, PLUGIN_NAME, PLUGIN_SIZE, 0o644)
    if installed is None:
        _write_file(directory, PLUGIN_NAME, data, 0o644)
    else:
        _verify_plugin(installed)
    return directory / PLUGIN_NAME


def _labels(state: dict) -> dict:
    return {OWNER + ".owner": "local-server-v1", OWNER + ".identity": state["identity"],
            OWNER + ".root-sha256": hashlib.sha256(state["root"].encode()).hexdigest(),
            OWNER + ".project-key": state["project_key"], OWNER + ".nonce": state["nonce"]}


def _label_args(state: dict) -> list[str]:
    return [arg for key, value in sorted(_labels(state).items()) for arg in ("--label", key + "=" + value)]


def _check_labels(actual: dict | None, state: dict) -> None:
    actual = actual or {}
    if {key: value for key, value in actual.items() if key.startswith(OWNER + ".")} != _labels(state):
        raise LocalServerError("Docker ownership labels do not match private metadata. Nothing was restarted")


def _volumes(state: dict, *, create: bool) -> None:
    for part in VOLUME_PARTS:
        name = state["identity"] + "-" + part
        volume = _inspect("volume", name)
        if volume is None:
            if not create:
                raise LocalServerError("Owned volume is missing. Private state retained, no reset attempted")
            _docker("volume", "create", *_label_args(state), name)
            volume = _inspect("volume", name)
        if not volume or volume.get("Name") != name or volume.get("Driver") != "local" or volume.get("Options"):
            raise LocalServerError("Docker volume identity or driver is unsafe")
        _check_labels(volume.get("Labels"), state)


def _desktop_plugin_source(source: str, plugin: Path) -> str:
    """Normalize only Desktop macOS's proven exact bind alias, never arbitrary paths."""
    expected = str(plugin)
    if source != "/host_mnt" + expected or not plugin.is_absolute() or sys.platform != "darwin":
        return source
    _local_docker()
    operating_system = json.loads(_docker("info", "--format", "{{json .OperatingSystem}}"))
    return expected if operating_system == "Docker Desktop" else source


def _check_container(container: dict, state: dict, plugin: Path) -> str | None:
    _check_labels(container.get("Config", {}).get("Labels"), state)
    if (container.get("Name") != "/" + state["identity"] or container.get("Image") != state["image_id"]
            or container.get("Config", {}).get("Image") != state["image_id"]
            or (state.get("container_id") and container.get("Id") != state["container_id"])):
        raise LocalServerError("Container identity or immutable image differs from private metadata")
    host = container.get("HostConfig", {})
    if (host.get("Privileged") or host.get("NetworkMode") not in ("default", "bridge")
            or host.get("PublishAllPorts") or host.get("PortBindings") != {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}):
        raise LocalServerError("Container must use the exact dynamically allocated loopback-only port mapping")
    expected = {"/opt/sonarqube/" + part: ("volume", state["identity"] + "-" + part, True) for part in VOLUME_PARTS}
    expected["/opt/sonarqube/extensions/plugins/" + PLUGIN_NAME] = ("bind", str(plugin), False)
    actual = {}
    for mount in container.get("Mounts", []):
        destination = mount.get("Destination")
        if destination in actual:
            raise LocalServerError("Duplicate Docker mount destination")
        source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
        if mount.get("Type") == "bind" and destination == "/opt/sonarqube/extensions/plugins/" + PLUGIN_NAME:
            source = _desktop_plugin_source(source, plugin)
        actual[destination] = (mount.get("Type"), source, mount.get("RW"))
    if actual != expected:
        raise LocalServerError("Container mounts differ from private metadata")
    ports = container.get("NetworkSettings", {}).get("Ports") or {}
    if container.get("State", {}).get("Running"):
        bindings = ports.get("9000/tcp")
        if (set(ports) != {"9000/tcp"} or not isinstance(bindings, list) or len(bindings) != 1
                or bindings[0].get("HostIp") != "127.0.0.1"
                or not re.fullmatch(r"[0-9]{1,5}", bindings[0].get("HostPort", ""))
                or not 1 <= int(bindings[0]["HostPort"]) <= 65535):
            raise LocalServerError("Running container has an unsafe published port")
        return "http://127.0.0.1:" + bindings[0]["HostPort"]
    return None


def _request(url: str, endpoint: str, password: str, data: dict | None = None) -> dict:
    """Use server.py's secure transport primitives, with body-only POST parameters."""
    headers = {"Accept": "application/json", "Authorization": "Basic " + base64.b64encode(("admin:" + password).encode()).decode()}
    payload = None if data is None else urlencode(data).encode()
    request = server.Request(server.validate_url(url) + "/api/" + endpoint, data=payload, headers=headers)
    opener = server.build_opener(ProxyHandler({}), server.NoRedirect(), server.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=15) as response:
            if response.getcode() not in (200, 204):
                raise LocalServerError("Sonar setup HTTP request failed. Private state retained")
            raw = response.read(server.MAX_RESPONSE + 1)
        if len(raw) > server.MAX_RESPONSE:
            raise LocalServerError("Sonar setup response exceeds the size bound")
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict):
            raise LocalServerError("Sonar setup response has an unexpected shape")
        return value
    except LocalServerError:
        raise
    except server.HTTPError as error:
        if endpoint == "authentication/validate" and error.code == 401:
            return {"valid": False}
        raise LocalServerError("Sonar setup HTTP request failed. Private state retained") from None
    except (OSError, ValueError):
        raise LocalServerError("Sonar setup request failed. Check availability and private recovery state") from None


def _wait(url: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    opener = server.build_opener(ProxyHandler({}), server.NoRedirect(), server.HTTPSHandler(context=ssl.create_default_context()))
    for _ in range(WAIT_SECONDS // 2):
        if time.monotonic() >= deadline:
            break
        try:
            status = server._get(opener, server.validate_url(url), "", "system/status")
        except ValueError:
            status = {}
        if status.get("status") == "UP":
            if status.get("version") != VERSION:
                raise LocalServerError("Owned Sonar server reported an unexpected version")
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise LocalServerError("Sonar readiness wait timed out. Owned container and private state retained")


def _setup_credentials(directory: Path, state: dict, url: str) -> str:
    password = state["admin_password"]
    if _request(url, "authentication/validate", password).get("valid") is not True:
        if state["phase"] not in {"created", "password_pending"}:
            raise LocalServerError("Stored administrator credential was rejected. Private state retained")
        state["phase"] = "password_pending"
        _save(directory, state)
        _request(url, "users/change_password", "admin", {"login": "admin", "previousPassword": "admin", "password": password})
        if _request(url, "authentication/validate", password).get("valid") is not True:
            raise LocalServerError("Administrator password rotation could not be verified")
    if state["phase"] in {"created", "password_pending"}:
        state["phase"] = "password_rotated"
        _save(directory, state)
    if state["phase"] == "token_pending":
        raise LocalServerError("Interrupted token issuance requires private-state recovery. No token was reset")
    if state["phase"] == "password_rotated":
        projects = _request(url, "projects/search?" + urlencode({"projects": state["project_key"]}), password).get("components")
        if not isinstance(projects, list):
            raise LocalServerError("Project lookup returned an unexpected shape")
        if not projects:
            _request(url, "projects/create", password, {"project": state["project_key"], "name": state["project_key"]})
        elif len(projects) != 1 or projects[0].get("key") != state["project_key"]:
            raise LocalServerError("Owned project lookup did not match its exact key")
        state["phase"] = "token_pending"
        _save(directory, state)
        response = _request(url, "user_tokens/generate", password,
                            {"name": state["token_name"], "type": "PROJECT_ANALYSIS_TOKEN", "projectKey": state["project_key"]})
        token = response.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{16,512}", token):
            raise LocalServerError("Project analysis token response was invalid. Private state retained")
        state.update(token=token, phase="ready")
        _save(directory, state)
    plugins = _request(url, "plugins/installed", password).get("plugins", [])
    languages = _request(url, "languages/list", password).get("languages", [])
    if ([item.get("version") for item in plugins if isinstance(item, dict) and item.get("key") == "sonarapple"] != [PLUGIN_VERSION]
            or not any(isinstance(item, dict) and item.get("key") == "swift" for item in languages)):
        raise LocalServerError("Expected sonarapple plugin and Swift language were not registered")
    return state["token"]


def _load(directory: Path, root: Path, key: str, identity: str) -> dict | None:
    raw = _read_file(directory, "state.json", MAX_STATE)
    if raw is None:
        return None
    state = json.loads(raw)
    if (not isinstance(state, dict) or state.get("schema") != 1 or state.get("root") != str(root)
            or state.get("project_key") != key or state.get("identity") != identity
            or state.get("phase") not in {"creating", "created", "password_pending", "password_rotated", "token_pending", "ready"}
            or not re.fullmatch(r"[0-9a-f]{32}", state.get("nonce", ""))
            or state.get("image_ref") != IMAGE
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", state.get("image_id", ""))
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", state.get("admin_password", ""))
            or state.get("token_name") != identity + "-analysis"
            or (state.get("phase") != "creating" and not re.fullmatch(r"[0-9a-f]{64}", state.get("container_id", "")))
            or (state.get("phase") == "ready" and not re.fullmatch(r"[!-~]{16,512}", state.get("token", "")))):
        raise LocalServerError("Private local server metadata does not match this project. State retained")
    return state


def ensure_local_server(root: Path, project_key: str) -> dict:
    """Ensure only this project's owned instance. Call only after explicit opt-in.

    Return a secret-bearing dict for in-memory use, never print or serialize it.
    No reset, deletion, rename, foreign-container restart or profile edit occurs.
    """
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise LocalServerError("Project root must be a directory")
        identity = _identity(root, project_key)
        directory, cache = _storage(root, identity)
        with _lock(directory):
            _local_docker()
            state = _load(directory, root, project_key, identity)
            if state is not None and _image() != state["image_id"]:
                raise LocalServerError("Owned image differs from the verified official digest. State retained")
            container = _inspect("container", identity)
            if state is None:
                if container or any(_inspect("volume", identity + "-" + part) for part in VOLUME_PARTS):
                    raise LocalServerError("Unowned Docker name collision. No existing resource was modified")
                state = {"schema": 1, "root": str(root), "project_key": project_key, "identity": identity,
                         "image_id": _image(), "image_ref": IMAGE, "nonce": secrets.token_hex(16), "phase": "creating",
                         "admin_password": secrets.token_urlsafe(36), "token_name": identity + "-analysis"}
                _save(directory, state)
            plugin = _plugin(cache, directory)
            # Validate all existing resources before any start or creation.
            if container:
                _check_container(container, state, plugin)
            elif state["phase"] != "creating" or state.get("container_id"):
                raise LocalServerError("Owned container is missing. Private state retained, no reset attempted")
            _volumes(state, create=state["phase"] == "creating" and container is None)
            if container is None:
                mounts = [arg for part in VOLUME_PARTS for arg in ("--mount", "type=volume,src=" + identity + "-" + part + ",dst=/opt/sonarqube/" + part)]
                _docker("container", "create", "--name", identity, *_label_args(state), "--publish", "127.0.0.1::9000",
                        *mounts, "--mount", "type=bind,src=" + str(plugin) + ",dst=/opt/sonarqube/extensions/plugins/" + PLUGIN_NAME + ",readonly",
                        state["image_id"])
                container = _inspect("container", identity)
                if not container:
                    raise LocalServerError("New container could not be inspected. Private state retained")
            url = _check_container(container, state, plugin)
            if state["phase"] == "creating":
                state.update(container_id=container["Id"], phase="created")
                _save(directory, state)
            if url is None:
                _docker("container", "start", state["container_id"])
                container = _inspect("container", identity)
                url = _check_container(container or {}, state, plugin)
                if url is None:
                    raise LocalServerError("Owned container did not start. Private state retained")
            _wait(url)
            token = _setup_credentials(directory, state, url)
            return {"url": url, "token": token, "container": identity, "identity": identity,
                    "server_version": VERSION, "plugin_version": PLUGIN_VERSION, "state_directory": str(directory)}
    except LocalServerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise LocalServerError("Local Sonar setup failed safely. Private state retained for inspection") from None
