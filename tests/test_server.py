import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from swift_sonar_kit import server

TOKEN = "fixture-secret-token"
GOOD = [{"status":"UP", "version":"26.9.0.129388"},
        {"plugins":[{"key":"sonarapple", "version":"0.5.1"}]},
        {"languages":[{"key":"swift"}]}]


class Response(io.BytesIO):
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


class ServerTests(unittest.TestCase):
    def check(self, values):
        opener = Opener(values)
        with patch.object(server, "build_opener", return_value=opener):
            result = server.preflight("https://sonar.example/context", TOKEN)
        return result, opener

    def test_valid_context_and_bearer_header(self):
        result, opener = self.check(GOOD)
        self.assertEqual(result, {"server_version":"26.9.0.129388", "plugin_version":"0.5.1", "language":"swift"})
        self.assertEqual(len(opener.requests), 3)
        for request, timeout in opener.requests:
            self.assertTrue(request.full_url.startswith("https://sonar.example/context/api/"))
            self.assertNotIn(TOKEN, request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(timeout, 15)

    def test_unsafe_urls_are_rejected_before_network(self):
        urls = ["http://sonar.example", "http://127.0.0.2", "file:///etc/passwd", "https://user:password@host",
                "https://host?token=x", "https://host#fragment", "https://host?", "https://host#",
                "https://host:bad", "https://host:0", "https://host/../admin", "https://host/%2e%2e/admin",
                "https://host/%0a", "https://host/%7f", "https://host/\x7f", "https://host\n", "https://", "https://%31%32%37.0.0.1"]
        with patch.object(server, "build_opener") as opener:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    server.preflight(url, TOKEN)
            opener.assert_not_called()

    def test_only_explicit_loopback_allows_plain_http(self):
        for url in ("http://localhost:9000/", "http://127.0.0.1:9000", "http://[::1]:9000/context"):
            self.assertEqual(server.validate_url(url), url.rstrip("/"))

    def test_missing_and_control_tokens_rejected(self):
        with patch.object(server, "build_opener") as opener:
            for token in ("", None, "secret\nvalue", "with space", "é"):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    server.preflight("https://host", token)
            opener.assert_not_called()

    def test_bad_status_and_schema_fail_closed(self):
        for value in ({"status":"STARTING", "version":"1.0"}, {}, [], {"status":"UP", "version":TOKEN}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check([value])

    def test_missing_duplicate_or_wrong_plugin_rejected(self):
        for plugins in (None, [], [None], [{"key":"sonarapple", "version":"0.5.2"}],
                        [{"key":"sonarapple", "version":"0.5.1"}] * 2):
            with self.subTest(plugins=plugins), self.assertRaises(ValueError):
                self.check([GOOD[0], {"plugins":plugins}])

    def test_missing_swift_and_bad_languages_rejected(self):
        for languages in (None, [], [None], [{"key":"java"}]):
            with self.subTest(languages=languages), self.assertRaises(ValueError):
                self.check([GOOD[0], GOOD[1], {"languages":languages}])

    def test_network_and_http_errors_do_not_leak_token(self):
        errors = [URLError(TOKEN), OSError(TOKEN), HTTPError("https://" + TOKEN, 401, TOKEN, {}, None),
                  HTTPError("https://" + TOKEN, 302, TOKEN, {}, None)]
        for error in errors:
            with self.subTest(error=type(error)):
                try:
                    self.check([error])
                except ValueError as caught:
                    self.assertNotIn(TOKEN, str(caught))
                else:
                    self.fail("Network failure unexpectedly accepted")

    def test_invalid_and_oversized_json_rejected(self):
        for value in (b"not json", b"x" * (server.MAX_RESPONSE + 1), b"null", b"\xff"):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                self.check([value])

    def test_real_http_redirect_is_not_followed(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-receive-token")
                self.end_headers()
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaisesRegex(ValueError, "redirect"):
                server.preflight(f"http://127.0.0.1:{httpd.server_port}", TOKEN)
            self.assertEqual(requests, ["/api/system/status"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
