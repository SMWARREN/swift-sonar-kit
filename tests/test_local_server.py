"""Offline local-server safety fixtures, never invoke a real Docker daemon."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from swift_sonar_kit import local_server as local
from swift_sonar_kit import server

TOKEN = "fixture-project-secret-token"
PASSWORD = "p" * 48
IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64


class Response(io.BytesIO):
    headers = {}

    def getcode(self):
        return 200


class Opener:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return Response(value if isinstance(value, bytes) else json.dumps(value).encode())


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "checkout"
        self.root.mkdir()
        self.key = "example:swift-app"
        self.identity = local._identity(self.root, self.key)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.directory, self.cache = local._storage(self.root, self.identity)
        self.plugin = self.directory / local.PLUGIN_NAME
        self.state = {"schema": 1, "root": str(self.root), "project_key": self.key,
                      "identity": self.identity, "image_id": IMAGE_ID, "image_ref": local.IMAGE, "nonce": "c" * 32,
                      "admin_password": PASSWORD, "token_name": self.identity + "-analysis",
                      "phase": "ready", "token": TOKEN, "container_id": CONTAINER_ID}

    def container(self, running=True):
        return {"Name": "/" + self.identity, "Id": CONTAINER_ID, "Image": IMAGE_ID,
                "Config": {"Image": IMAGE_ID, "Labels": local._labels(self.state)},
                "HostConfig": {"Privileged": False, "NetworkMode": "default", "PublishAllPorts": False,
                               "PortBindings": {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}},
                "Mounts": [{"Type": "volume", "Name": self.identity + "-" + part,
                            "Destination": "/opt/sonarqube/" + part, "RW": True} for part in local.VOLUME_PARTS]
                + [{"Type": "bind", "Source": str(self.plugin), "RW": False,
                    "Destination": "/opt/sonarqube/extensions/plugins/" + local.PLUGIN_NAME}],
                "NetworkSettings": {"Ports": {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32123"}]} if running else {}},
                "State": {"Running": running}}

    def run_fixture(self, *, existing=True, stopped=False, mutate=None):
        if existing:
            local._save(self.directory, self.state)
        state = {"container": self.container(not stopped) if existing else None, "volumes": {}}
        if mutate and state["container"]:
            mutate(state["container"])
        for part in local.VOLUME_PARTS:
            name = self.identity + "-" + part
            if existing:
                state["volumes"][name] = {"Name": name, "Driver": "local", "Options": None, "Labels": local._labels(self.state)}
        commands = []
        requests = []
        password_rotated = existing

        def inspect(kind, name):
            return state["container"] if kind == "container" else state["volumes"].get(name)

        def docker(*args, **kwargs):
            commands.append(args)
            current = local._load(self.directory, self.root, self.key, self.identity)
            if args[:2] == ("volume", "create"):
                name = args[-1]
                state["volumes"][name] = {"Name": name, "Driver": "local", "Options": None, "Labels": local._labels(current)}
            elif args[:2] == ("container", "create"):
                state["container"] = self.container(False)
                state["container"]["Config"]["Labels"] = local._labels(current)
            elif args[:2] == ("container", "start"):
                state["container"]["State"]["Running"] = True
                state["container"]["NetworkSettings"] = self.container()["NetworkSettings"]
            else:
                raise AssertionError("Unexpected mutation")
            return CONTAINER_ID

        def request(url, endpoint, password, data=None):
            nonlocal password_rotated
            requests.append((endpoint, password, data))
            if endpoint == "authentication/validate":
                return {"valid": password_rotated}
            if endpoint == "users/change_password":
                self.assertEqual(password, "admin")
                self.assertEqual(data["previousPassword"], "admin")
                password_rotated = True
                return {}
            self.assertTrue(password_rotated, "Only password rotation may use default administrator")
            self.assertNotEqual(password, "admin")
            if endpoint.startswith("projects/search"):
                return {"components": []}
            if endpoint == "projects/create":
                return {"project": {"key": self.key}}
            if endpoint == "user_tokens/generate":
                self.assertEqual(data["type"], "PROJECT_ANALYSIS_TOKEN")
                self.assertEqual(data["projectKey"], self.key)
                return {"token": TOKEN}
            if endpoint == "plugins/installed":
                return {"plugins": [{"key": "sonarapple", "version": local.PLUGIN_VERSION}]}
            if endpoint == "languages/list":
                return {"languages": [{"key": "swift"}]}
            raise AssertionError("Unexpected endpoint")

        with ExitStack() as stack:
            stack.enter_context(patch.object(local, "_local_docker"))
            stack.enter_context(patch.object(local, "_inspect", side_effect=inspect))
            stack.enter_context(patch.object(local, "_docker", side_effect=docker))
            stack.enter_context(patch.object(local, "_image", return_value=IMAGE_ID))
            stack.enter_context(patch.object(local, "_plugin", return_value=self.plugin))
            stack.enter_context(patch.object(local, "_wait"))
            stack.enter_context(patch.object(local, "_request", side_effect=request))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            errors = stack.enter_context(redirect_stderr(io.StringIO()))
            result = local.ensure_local_server(self.root, self.key)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        for command in commands:
            self.assertNotIn(TOKEN, repr(command))
            self.assertNotIn(PASSWORD, repr(command))
            self.assertNotIn("hashfleet-sonar", repr(command))
        return result, commands, requests

    def test_identity_stable_key_slug_root_hash(self):
        self.assertEqual(local._identity(self.root, self.key), self.identity)
        self.assertTrue(self.identity.startswith("swift-sonar-example-swift-app-"))
        self.assertNotEqual(local._identity(self.root / "other", self.key), self.identity)
        self.assertNotEqual(local._identity(self.root, "example-swift-app"), self.identity)
        self.assertNotIn("hashfleet", self.identity)
        self.assertLess(len(local._identity(self.root, "a" * 400)), 80)

    def test_invalid_keys(self):
        for key in (None, "", "123", "-x\n", "x/y", "a " , "a" * 401):
            with self.subTest(key=key), self.assertRaises(local.LocalServerError):
                local._identity(self.root, key)

    def test_canonical_root_alias_reuses_identity(self):
        alias = self.home / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result, _, _ = self.run_fixture()
        with patch.object(local, "_storage", side_effect=RuntimeError("boundary")) as storage:
            with self.assertRaises(RuntimeError):
                local.ensure_local_server(alias, self.key)
        self.assertEqual(storage.call_args.args, (self.root, result["identity"]))

    def test_private_state_modes_and_outside_checkout(self):
        local._save(self.directory, self.state)
        for path in (self.directory, self.directory.parent, self.cache):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / "state.json").stat().st_mode), 0o600)
        self.assertFalse(self.directory.is_relative_to(self.root))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(local._load(self.directory, self.root, self.key, self.identity), self.state)

    def test_storage_inside_project_rejected(self):
        with self.assertRaisesRegex(local.LocalServerError, "outside"):
            local._storage(self.home, self.identity)

    def test_permissive_directory_rejected(self):
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(local.LocalServerError, "0700"):
            local._storage(self.root, self.identity)

    def test_symlink_file_rejected_without_read(self):
        target = self.home / "secret"
        target.write_text(TOKEN)
        (self.directory / "state.json").symlink_to(target)
        with patch.object(local, "_local_docker"), self.assertRaises(local.LocalServerError) as caught:
            local.ensure_local_server(self.root, self.key)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(target.read_text(), TOKEN)

    def test_symlink_storage_ancestor_rejected(self):
        fake = self.home / "other"
        fake.mkdir()
        original = self.home / ".cache"
        original.rename(self.home / ".old-cache")
        original.symlink_to(fake, target_is_directory=True)
        with self.assertRaisesRegex(local.LocalServerError, "symbolic"):
            local._storage(self.root, self.identity)
        self.assertEqual(list(fake.iterdir()), [])

    def test_hardlinked_or_permissive_state_rejected(self):
        local._save(self.directory, self.state)
        path = self.directory / "state.json"
        path.chmod(0o644)
        with self.assertRaises(local.LocalServerError):
            local._load(self.directory, self.root, self.key, self.identity)
        path.chmod(0o600)
        os.link(path, self.home / "copy")
        with self.assertRaises(local.LocalServerError):
            local._load(self.directory, self.root, self.key, self.identity)

    def test_lock_is_nonblocking_and_private(self):
        with local._lock(self.directory):
            with self.assertRaisesRegex(local.LocalServerError, "Another"):
                with local._lock(self.directory):
                    self.fail("lock collision accepted")
        self.assertEqual(stat.S_IMODE((self.directory / "setup.lock").stat().st_mode), 0o600)

    def test_unowned_collision_does_not_mutate(self):
        with patch.object(local, "_local_docker"), patch.object(local, "_inspect", return_value=self.container()), patch.object(local, "_docker") as docker:
            with self.assertRaisesRegex(local.LocalServerError, "Unowned"):
                local.ensure_local_server(self.root, self.key)
            docker.assert_not_called()
        self.assertFalse((self.directory / "state.json").exists())

    def test_owned_running_reuse_has_no_docker_mutations(self):
        result, commands, requests = self.run_fixture()
        self.assertEqual(result["token"], TOKEN)
        self.assertEqual(result["url"], "http://127.0.0.1:32123")
        self.assertEqual(commands, [])
        self.assertFalse(any(data is not None for _, _, data in requests))

    def test_owned_stopped_restart_only_after_validation(self):
        _, commands, _ = self.run_fixture(stopped=True)
        self.assertEqual(commands, [("container", "start", CONTAINER_ID)])

    def test_full_new_setup_rotates_then_creates_project_scoped_token(self):
        result, commands, requests = self.run_fixture(existing=False)
        endpoints = [request[0] for request in requests]
        self.assertLess(endpoints.index("users/change_password"), endpoints.index("projects/create"))
        self.assertLess(endpoints.index("projects/create"), endpoints.index("user_tokens/generate"))
        create = next(args for args in commands if args[:2] == ("container", "create"))
        self.assertIn("127.0.0.1::9000", create)
        self.assertEqual(create[-1], IMAGE_ID)
        self.assertEqual(result["container"], self.identity)
        saved = local._load(self.directory, self.root, self.key, self.identity)
        self.assertEqual(saved["phase"], "ready")
        self.assertEqual(saved["token"], TOKEN)
        self.assertNotIn(saved["admin_password"], repr(commands))

    def test_container_rejects_identity_image_label_mount_and_port_tampering(self):
        mutations = [lambda c: c.update(Name="/hashfleet-sonar"), lambda c: c.update(Id="other"),
                     lambda c: c.update(Image="sha256:" + "d" * 64),
                     lambda c: c["Config"]["Labels"].update({local.OWNER + ".nonce": "other"}),
                     lambda c: c["Mounts"][0].update(Name="foreign"),
                     lambda c: c["Mounts"][-1].update(RW=True),
                     lambda c: c["HostConfig"].update(Privileged=True),
                     lambda c: c["HostConfig"].update(NetworkMode="host"),
                     lambda c: c["HostConfig"]["PortBindings"]["9000/tcp"][0].update(HostIp="0.0.0.0"),
                     lambda c: c["NetworkSettings"]["Ports"]["9000/tcp"][0].update(HostIp="::"),
                     lambda c: c["NetworkSettings"]["Ports"].update({"9001/tcp": []}),
                     lambda c: c["NetworkSettings"]["Ports"]["9000/tcp"][0].update(HostPort="0")]
        for mutate in mutations:
            with self.subTest(mutation=mutate), self.assertRaises(local.LocalServerError):
                container = self.container()
                mutate(container)
                local._check_container(container, self.state, self.plugin)

    def test_desktop_exact_plugin_alias_requires_local_darwin_daemon_proof(self):
        container = self.container()
        container["Mounts"][-1]["Source"] = "/host_mnt" + str(self.plugin)
        container["HostConfig"]["Mounts"] = [{"Type": "bind", "Source": "/host_mnt" + str(self.plugin)}]
        with patch.object(local.sys, "platform", "darwin"), patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}):
            with patch.object(local, "_docker", side_effect=[json.dumps("unix:///local/docker.sock"), json.dumps("Docker Desktop")]) as docker:
                self.assertEqual(local._check_container(container, self.state, self.plugin), "http://127.0.0.1:32123")
        self.assertEqual(docker.call_args_list[0].args, ("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"))
        self.assertEqual(docker.call_args_list[1].args, ("info", "--format", "{{json .OperatingSystem}}"))

    def test_desktop_alias_wrong_source_rejected_without_daemon_calls(self):
        for source in ("/host_mnt" + str(self.plugin) + "-foreign", "/host_mnt/foreign/" + local.PLUGIN_NAME):
            container = self.container()
            container["Mounts"][-1]["Source"] = source
            with patch.object(local.sys, "platform", "darwin"), patch.object(local, "_docker") as docker:
                with self.assertRaisesRegex(local.LocalServerError, "mounts differ"):
                    local._check_container(container, self.state, self.plugin)
                docker.assert_not_called()

    def test_desktop_alias_non_desktop_or_non_darwin_rejected(self):
        container = self.container()
        container["Mounts"][-1]["Source"] = "/host_mnt" + str(self.plugin)
        with patch.object(local.sys, "platform", "linux"), patch.object(local, "_docker") as docker:
            with self.assertRaisesRegex(local.LocalServerError, "mounts differ"):
                local._check_container(container, self.state, self.plugin)
            docker.assert_not_called()
        with patch.object(local.sys, "platform", "darwin"), patch.dict(os.environ, {"DOCKER_HOST": "unix:///local/docker.sock", "DOCKER_CONTEXT": ""}):
            with patch.object(local, "_docker", return_value=json.dumps("Linux")):
                with self.assertRaisesRegex(local.LocalServerError, "mounts differ"):
                    local._check_container(container, self.state, self.plugin)

    def test_desktop_alias_remote_context_rejected_before_info(self):
        container = self.container()
        container["Mounts"][-1]["Source"] = "/host_mnt" + str(self.plugin)
        with patch.object(local.sys, "platform", "darwin"), patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": "remote"}):
            with patch.object(local, "_docker", return_value=json.dumps("ssh://remote")) as docker:
                with self.assertRaisesRegex(local.LocalServerError, "local Docker"):
                    local._check_container(container, self.state, self.plugin)
                self.assertEqual(docker.call_count, 1)

    def test_tampered_owned_container_never_restarted(self):
        with self.assertRaises(local.LocalServerError):
            self.run_fixture(stopped=True, mutate=lambda c: c["Mounts"][0].update(Name="foreign"))

    def test_missing_container_is_not_reset(self):
        local._save(self.directory, self.state)
        with patch.object(local, "_local_docker"), patch.object(local, "_image", return_value=IMAGE_ID), patch.object(local, "_inspect", return_value=None), patch.object(local, "_plugin", return_value=self.plugin), patch.object(local, "_docker") as docker:
            with self.assertRaisesRegex(local.LocalServerError, "missing"):
                local.ensure_local_server(self.root, self.key)
            docker.assert_not_called()
        self.assertEqual(local._load(self.directory, self.root, self.key, self.identity), self.state)

    def test_metadata_cross_project_rejected(self):
        local._save(self.directory, self.state)
        with self.assertRaisesRegex(local.LocalServerError, "metadata"):
            local._load(self.directory, self.root, "different", self.identity)

    def test_interrupted_token_issuance_retains_state(self):
        self.state.pop("token")
        self.state["phase"] = "token_pending"
        local._save(self.directory, self.state)
        before = (self.directory / "state.json").read_bytes()
        with patch.object(local, "_request", return_value={"valid": True}) as request:
            with self.assertRaisesRegex(local.LocalServerError, "Interrupted token"):
                local._setup_credentials(self.directory, self.state, "http://127.0.0.1:9000")
            self.assertEqual(request.call_count, 1)
        self.assertEqual((self.directory / "state.json").read_bytes(), before)

    def test_interrupted_rotation_reuses_saved_password(self):
        self.state.pop("token")
        self.state["phase"] = "password_pending"
        responses = [{"valid": True}, {"components": [{"key": self.key}]}, {"token": TOKEN},
                     {"plugins": [{"key": "sonarapple", "version": "0.5.1"}]}, {"languages": [{"key": "swift"}]}]
        with patch.object(local, "_request", side_effect=responses) as request:
            self.assertEqual(local._setup_credentials(self.directory, self.state, "http://127.0.0.1:9000"), TOKEN)
        self.assertFalse(any(call.args[1] == "users/change_password" for call in request.call_args_list))

    def test_ready_bad_admin_does_not_fallback_to_default(self):
        with patch.object(local, "_request", return_value={"valid": False}) as request:
            with self.assertRaisesRegex(local.LocalServerError, "rejected"):
                local._setup_credentials(self.directory, self.state, "http://127.0.0.1:9000")
            self.assertEqual(request.call_count, 1)

    def test_post_secrets_in_body_only_no_redirects_or_proxies(self):
        opener = Opener([{}])
        with patch.object(server, "build_opener", return_value=opener) as factory:
            local._request("http://127.0.0.1:9000", "users/change_password", PASSWORD,
                           {"password": TOKEN, "previousPassword": PASSWORD})
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn(TOKEN, request.full_url)
        self.assertNotIn(PASSWORD, request.full_url)
        self.assertEqual(parse_qs(request.data.decode())["password"], [TOKEN])
        self.assertTrue(any(isinstance(handler, server.NoRedirect) for handler in factory.call_args.args))
        self.assertTrue(any(getattr(handler, "proxies", None) == {} for handler in factory.call_args.args))

    def test_authentication_401_is_false_not_raw_error(self):
        error = HTTPError("http://" + TOKEN, 401, PASSWORD, {}, None)
        with patch.object(server, "build_opener", return_value=Opener([error])):
            self.assertEqual(local._request("http://127.0.0.1:9000", "authentication/validate", PASSWORD), {"valid": False})

    def test_ready_state_requires_container_id(self):
        self.state.pop("container_id")
        local._save(self.directory, self.state)
        with self.assertRaisesRegex(local.LocalServerError, "metadata"):
            local._load(self.directory, self.root, self.key, self.identity)

    def test_transport_errors_are_sanitized(self):
        errors = [URLError(TOKEN), OSError(PASSWORD), HTTPError("http://" + TOKEN, 401, PASSWORD, {}, None),
                  b"bad json " + TOKEN.encode(), b"x" * (server.MAX_RESPONSE + 1), b"[]"]
        for error in errors:
            with self.subTest(error=type(error)), patch.object(server, "build_opener", return_value=Opener([error])):
                with self.assertRaises(local.LocalServerError) as caught:
                    local._request("http://127.0.0.1:9000", "test", PASSWORD)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))

    def test_docker_failure_sanitized_and_timeout_bounded(self):
        for outcome in (subprocess.TimeoutExpired([TOKEN], 30, output=PASSWORD), OSError(TOKEN),
                        subprocess.CompletedProcess([], 1, TOKEN.encode(), PASSWORD.encode())):
            mock = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
            with patch.object(subprocess, "run", mock), self.assertRaises(local.LocalServerError) as caught:
                local._docker("container", "ls")
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn(PASSWORD, str(caught.exception))
            self.assertEqual(mock.call_args.kwargs["timeout"], 30)
            self.assertTrue(mock.call_args.kwargs["capture_output"])

    def test_readiness_wait_is_bounded_and_wrong_version_rejected(self):
        with patch.object(local, "WAIT_SECONDS", 4), patch.object(server, "_get", return_value={"status": "STARTING"}) as get, patch.object(local.time, "sleep"):
            with self.assertRaisesRegex(local.LocalServerError, "timed out"):
                local._wait("http://127.0.0.1:9000")
            self.assertEqual(get.call_count, 2)
        with patch.object(server, "_get", return_value={"status": "UP", "version": "other"}):
            with self.assertRaisesRegex(local.LocalServerError, "unexpected version"):
                local._wait("http://127.0.0.1:9000")

    def test_image_uses_exact_inspected_id_and_version(self):
        result = [{"Id": IMAGE_ID, "RepoDigests": [local.IMAGE], "Config": {"Env": ["SONAR_VERSION=" + local.VERSION]}}]
        with patch.object(local, "_docker", side_effect=[local.LocalServerError("not installed"), "pulled", json.dumps(result)]) as docker:
            self.assertEqual(local._image(), IMAGE_ID)
            self.assertEqual(docker.call_args_list[1].args, ("image", "pull", "--quiet", local.IMAGE))
            self.assertEqual(docker.call_args_list[1].kwargs, {"timeout": 180})
            self.assertTrue(all(call.args[-1] == local.IMAGE for call in docker.call_args_list))
        result[0]["Config"]["Env"] = ["SONAR_VERSION=other"]
        with patch.object(local, "_docker", return_value=json.dumps(result)), self.assertRaises(local.LocalServerError):
            local._image()

    def test_docker_environment_excludes_sonar_and_jvm_credentials(self):
        with patch.dict(os.environ, {"SONAR_TOKEN": TOKEN, "JAVA_TOOL_OPTIONS": PASSWORD, "OTHER_SECRET": TOKEN}):
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"ok", b"")) as run:
                local._docker("container", "ls")
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("SONAR_TOKEN", environment)
        self.assertNotIn("JAVA_TOOL_OPTIONS", environment)
        self.assertNotIn("OTHER_SECRET", environment)
        self.assertNotIn(TOKEN, repr(run.call_args))
        self.assertNotIn(PASSWORD, repr(run.call_args))

    def test_remote_context_rejected(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": "remote"}):
            with patch.object(local, "_docker", return_value=json.dumps("ssh://remote")):
                with self.assertRaisesRegex(local.LocalServerError, "local Docker"):
                    local._local_docker()

    def test_remote_docker_rejected(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375", "DOCKER_CONTEXT": ""}):
            with self.assertRaisesRegex(local.LocalServerError, "local Docker"):
                local._local_docker()

    def test_plugin_checks_exact_size_and_hash_and_caches_generically(self):
        data = b"fixture plugin"
        with patch.object(local, "PLUGIN_SIZE", len(data)), patch.object(local, "PLUGIN_SHA256", hashlib.sha256(data).hexdigest()):
            local._write_file(self.cache, local.PLUGIN_NAME, data)
            self.assertEqual(local._plugin(self.cache, self.directory), self.plugin)
            self.assertEqual(self.plugin.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(self.plugin.stat().st_mode), 0o644)
            with self.assertRaisesRegex(local.LocalServerError, "SHA256"):
                local._verify_plugin(b"x" * len(data))
            with self.assertRaises(local.LocalServerError):
                local._verify_plugin(data + b"x")
        self.assertIn("swift-sonar-kit", str(self.cache))
        self.assertNotIn("hashfleet", str(self.cache))

    def test_plugin_redirect_rejects_untrusted_hosts_http_and_credentials(self):
        handler = local._PluginRedirect()
        request = server.Request(local.PLUGIN_URL)
        for url in ("http://github.com/a", "https://evil.example/a", "https://github.com.evil/a",
                    "https://user:pass@github.com/a", "https://github.com:444/a"):
            with self.subTest(url=url), self.assertRaises(local.LocalServerError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_oversized_download_not_written(self):
        response = Response(b"too much")
        response.headers = {"Content-Length": "999999999"}
        opener = Mock()
        opener.open.return_value = response
        with patch.object(server, "build_opener", return_value=opener):
            with self.assertRaises(local.LocalServerError):
                local._plugin(self.cache, self.directory)
        self.assertFalse((self.cache / local.PLUGIN_NAME).exists())

    def test_volume_ownership_rejected(self):
        volume = {"Name": self.identity + "-data", "Driver": "local", "Options": None, "Labels": {}}
        with patch.object(local, "_inspect", return_value=volume), patch.object(local, "_docker") as docker:
            with self.assertRaisesRegex(local.LocalServerError, "ownership"):
                local._volumes(self.state, create=False)
            docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
