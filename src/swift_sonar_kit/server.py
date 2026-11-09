"""Read-only Sonar checks. SPDX-License-Identifier: GPL-3.0-only."""

import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

MAX_RESPONSE = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Sonar redirects are refused. Configure the final trusted URL")


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Sonar URL must be absolute and contain no whitespace or controls")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Malformed Sonar URL") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in url or "#" in url
            or "%" in parsed.netloc or port == 0):
        raise ValueError("Sonar URL cannot contain credentials, query parameters or fragments")
    decoded = unquote(parsed.path)
    if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in decoded) or ".." in decoded.split("/"):
        raise ValueError("Unsafe Sonar context path")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote Sonar servers require HTTPS. HTTP is allowed only on loopback")
    return url.rstrip("/")


def _get(opener, base: str, token: str, endpoint: str) -> dict:
    request = Request(base + "/api/" + endpoint, headers={"Authorization": "Bearer " + token,
                                                         "Accept": "application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            if response.getcode() != 200:
                raise ValueError("Sonar endpoint did not return HTTP 200")
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("Sonar response exceeds the size limit")
        data = json.loads(raw)
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise ValueError("Sonar redirects are refused") from None
        raise ValueError("Sonar HTTP request failed. Check URL, token and permissions") from None
    except (URLError, OSError, UnicodeError):
        raise ValueError("Sonar connection failed. Check server availability and TLS trust") from None
    except json.JSONDecodeError:
        raise ValueError("Sonar returned invalid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("Sonar endpoint returned an unexpected JSON shape")
    return data


def preflight(url: str, token: str, expected_plugin_version: str = "0.5.1") -> dict:
    base = validate_url(url)
    if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Set a nonempty SONAR_TOKEN without whitespace or control characters")
    if not isinstance(expected_plugin_version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]*", expected_plugin_version):
        raise ValueError("Invalid expected plugin version")
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    status = _get(opener, base, token, "system/status")
    version = status.get("version")
    if status.get("status") != "UP" or not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]*", version):
        raise ValueError("Sonar is not UP or did not report a valid version")
    plugins = _get(opener, base, token, "plugins/installed").get("plugins")
    if not isinstance(plugins, list) or any(not isinstance(p, dict) for p in plugins):
        raise ValueError("Sonar plugin inventory has an unexpected shape")
    matching = [p for p in plugins if p.get("key") == "sonarapple"]
    if len(matching) != 1 or matching[0].get("version") != expected_plugin_version:
        raise ValueError("Expected sonarapple plugin/version is missing. Review server installation")
    languages = _get(opener, base, token, "languages/list").get("languages")
    if not isinstance(languages, list) or any(not isinstance(p, dict) for p in languages):
        raise ValueError("Sonar language inventory has an unexpected shape")
    if not any(language.get("key") == "swift" for language in languages):
        raise ValueError("Swift is not registered on this Sonar server")
    return {"server_version": version.replace(token, "[redacted]"),
            "plugin_version": expected_plugin_version.replace(token, "[redacted]"), "language": "swift"}
