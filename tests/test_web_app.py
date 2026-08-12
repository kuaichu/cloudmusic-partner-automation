import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from _support import RUNTIME_DIR
import web_app


class WebStatusCacheTests(unittest.TestCase):
    def setUp(self):
        web_app.clear_status_cache()

    def test_status_cache_avoids_upstream_refresh_within_ttl(self):
        config = RUNTIME_DIR / "cache-config.json"
        calls = []

        def build(_):
            calls.append(1)
            return [{"index": 1, "strategy": "ready"}]

        with mock.patch.object(web_app, "_build_account_summaries", side_effect=build), mock.patch.object(
            web_app, "process_state", return_value={"running": False, "pid": None, "returncode": None}
        ):
            first = web_app.build_status(config, ttl=45)
            second = web_app.build_status(config, ttl=45)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["accounts"], second["accounts"])
        self.assertNotIn("config_path", first)
        self.assertNotIn("log_path", first)


class WebAuthorizationTests(unittest.TestCase):
    def test_unauthorized_run_returns_403_and_no_store(self):
        class TestHandler(web_app.Handler):
            admin_token = "startup-token"
            allowed_hosts = {"127.0.0.1", "localhost", "::1"}
            config_path = RUNTIME_DIR / "unused.json"

            def log_message(self, fmt, *args):
                pass

        server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            with mock.patch.object(web_app, "start_runner") as runner:
                connection.request(
                    "POST",
                    "/api/run",
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                response = connection.getresponse()
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 403)
            self.assertEqual(body["error"], "forbidden")
            self.assertIn("no-store", response.getheader("Cache-Control"))
            runner.assert_not_called()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_authorized_run_accepts_only_allowed_host_and_same_origin(self):
        class TestHandler(web_app.Handler):
            admin_token = "startup-token"
            allowed_hosts = {"127.0.0.1"}
            config_path = RUNTIME_DIR / "unused.json"

            def log_message(self, fmt, *args):
                pass

        server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with mock.patch.object(web_app, "start_runner", return_value={"started": True, "running": True}):
                good = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                good.request(
                    "POST",
                    "/api/run",
                    headers={
                        "Origin": f"http://127.0.0.1:{port}",
                        "X-Admin-Token": "startup-token",
                    },
                )
                good_response = good.getresponse()
                good_response.read()
                self.assertEqual(good_response.status, 202)
                good.close()

                bad = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                bad.request(
                    "POST",
                    "/api/run",
                    headers={
                        "Host": "192.0.2.123",
                        "Origin": "http://192.0.2.123",
                        "X-Admin-Token": "startup-token",
                    },
                )
                bad_response = bad.getresponse()
                bad_response.read()
                self.assertEqual(bad_response.status, 403)
                bad.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


class WebBoundaryTests(unittest.TestCase):
    def test_remote_listening_requires_explicit_flags(self):
        with self.assertRaises(ValueError):
            web_app.validate_bind_options("0.0.0.0", False, [])
        with self.assertRaises(ValueError):
            web_app.validate_bind_options("0.0.0.0", True, [])
        self.assertFalse(web_app.validate_bind_options("0.0.0.0", True, ["192.0.2.10"]))
        self.assertTrue(web_app.validate_bind_options("127.0.0.1", False, []))

    def test_log_tail_removes_secrets_urls_paths_and_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "sample.log"
            log_path.write_text(
                "ERROR config C:\\private\\copartner_ck.json Cookie='raw-cookie'\n"
                "GET https://example.invalid/?key=raw-key\n"
                "ERROR config C:/private/secret.json\n"
                "ERROR config \\\\server\\share\\secret.json\n"
                "PermissionError: C:/private/denied.txt\n"
                "Traceback (most recent call last):\n"
                "  File \"C:\\private\\module.py\", line 1\n",
                encoding="utf-8",
            )
            text = web_app.tail_text(log_path)
        for value in (
            "raw-cookie",
            "raw-key",
            "C:\\private",
            "C:/private",
            "server\\share",
            "https://example.invalid",
            "denied.txt",
        ):
            self.assertNotIn(value, text)
        self.assertIn("<exception details omitted>", text)

    def test_request_line_logging_redacts_sensitive_query(self):
        handler = object.__new__(web_app.Handler)
        handler.address_string = lambda: "127.0.0.1"
        output = io.StringIO()
        with mock.patch.object(web_app.sys, "stdout", output):
            handler.log_message('"GET /api/status?key=raw-key&MUSIC_U=raw-music HTTP/1.1" 200 -')
        self.assertNotIn("raw-key", output.getvalue())
        self.assertNotIn("raw-music", output.getvalue())


if __name__ == "__main__":
    unittest.main()
