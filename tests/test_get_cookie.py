import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from _support import RUNTIME_DIR
import get_cookie as gc
import music_partner as mp


class FakeResponse:
    def __init__(self, payload=None, status=200, url=None):
        self.payload = payload
        self.status_code = status
        self.url = url
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("failed", response=self)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CookieConfigTests(unittest.TestCase):
    def test_atomic_replace_failure_preserves_original_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copartner_ck.json"
            original = json.dumps({"MUSIC_COPARTNER": [{"cookie": "old"}], "keep": True})
            path.write_text(original, encoding="utf-8")
            with mock.patch.object(gc.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    gc.save_config("__csrf=new; MUSIC_U=new", str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_invalid_old_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copartner_ck.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                gc.save_config("__csrf=new; MUSIC_U=new", str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_manual_input_uses_hidden_prompt(self):
        with mock.patch.object(gc.getpass, "getpass", return_value="__csrf=x; MUSIC_U=y") as hidden:
            result = gc.manual_input()
        hidden.assert_called_once()
        self.assertEqual(result[0], "__csrf=x; MUSIC_U=y")


class CookieNetworkTests(unittest.TestCase):
    def test_request_helper_sets_timeout_and_does_not_retry_post(self):
        session = FakeSession([requests.Timeout("POST https://example.invalid?key=secret")])
        with self.assertRaises(gc.CookieRequestError):
            gc.request_json(session, "POST", "https://example.invalid", "提交测试")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["timeout"], gc.REQUEST_TIMEOUT)

    def test_non_https_final_response_is_rejected(self):
        session = FakeSession([FakeResponse({}, url="http://example.invalid")])
        with self.assertRaises(gc.CookieRequestError):
            gc.request_json(session, "GET", "https://example.invalid", "降级测试")

    def test_dictionary_style_secrets_are_redacted(self):
        redacted = gc.redact_sensitive({
            "Cookie": "foo=raw-cookie",
            "csrf_token": "raw-csrf",
            "MUSIC_U": "raw-music-u",
            "key": "raw-key",
        })
        for secret in ("raw-cookie", "raw-csrf", "raw-music-u", "raw-key"):
            self.assertNotIn(secret, redacted)
        header = gc.redact_sensitive("Cookie: foo=alpha; bar=beta; __csrf=gamma")
        for secret in ("alpha", "beta", "gamma"):
            self.assertNotIn(secret, header)

    def test_browser_exception_log_is_redacted(self):
        def fail(**kwargs):
            raise RuntimeError("Cookie: foo=raw-cookie; MUSIC_U=raw-music")

        fake_module = types.SimpleNamespace(chrome=fail, edge=fail, brave=fail, chromium=fail)
        with mock.patch.dict(sys.modules, {"browser_cookie3": fake_module}), self.assertLogs(
            gc.log, level="ERROR"
        ) as captured:
            self.assertIsNone(gc.extract_from_browser("chrome"))
        output = "\n".join(captured.output)
        self.assertNotIn("raw-cookie", output)
        self.assertNotIn("raw-music", output)

    def test_qrcode_key_is_not_logged_and_png_is_cleaned_in_finally(self):
        response = FakeResponse()
        calls = {"count": 0}

        def fake_request(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return response, None
            if calls["count"] == 2:
                return response, {"code": 200, "unikey": "qr-secret-key"}
            return response, {"code": 800, "message": "expired qr-secret-key"}

        with tempfile.TemporaryDirectory() as tmp:
            fake_file = str(Path(tmp) / "get_cookie.py")

            def create_png(url, path):
                Path(path).write_bytes(b"png")
                return True

            with mock.patch.object(gc, "__file__", fake_file), mock.patch.object(
                gc, "request_json", side_effect=fake_request
            ), mock.patch.object(gc, "print_qr_ascii", return_value=False), mock.patch.object(
                gc, "generate_qr_image", side_effect=create_png
            ), mock.patch.object(gc.os, "startfile", side_effect=OSError("disabled"), create=True), self.assertLogs(
                gc.log, level="INFO"
            ) as captured:
                self.assertIsNone(gc.login_qrcode())
            self.assertFalse((Path(tmp) / "qrcode.png").exists())
            self.assertNotIn("qr-secret-key", "\n".join(captured.output))


class CookieCliTests(unittest.TestCase):
    def test_test_flag_is_read_only_and_never_calls_run(self):
        calls = []

        class FakePartner:
            def __init__(self, cookie, quiet=False):
                calls.append("init")

            def check_access(self):
                calls.append("check_access")
                return {"user_name": "tester", "daily_task_access": True}

            def run(self):
                raise AssertionError("run must not be called by --test")

        result = ("__csrf=x; MUSIC_U=y", {"nickname": "manual", "userId": ""})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            gc, "manual_input", return_value=result
        ), mock.patch.object(gc, "save_config"), mock.patch.object(mp, "MusicPartner", FakePartner):
            code = gc.main(["--manual", "--test", "--output", str(Path(tmp) / "config.json")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["init", "check_access"])

    def test_incomplete_cookie_never_overwrites_config(self):
        result = ("__csrf=only", {"nickname": "manual", "userId": ""})
        with mock.patch.object(gc, "manual_input", return_value=result), mock.patch.object(gc, "save_config") as save:
            code = gc.main(["--manual", "--no-test"])
        self.assertEqual(code, 1)
        save.assert_not_called()

    def test_empty_cookie_values_never_overwrite_config(self):
        result = ("__csrf=; MUSIC_U=", {"nickname": "manual", "userId": ""})
        with mock.patch.object(gc, "manual_input", return_value=result), mock.patch.object(gc, "save_config") as save:
            code = gc.main(["--manual", "--no-test"])
        self.assertEqual(code, 1)
        save.assert_not_called()

    def test_failed_read_only_check_never_overwrites_config(self):
        class FakePartner:
            def __init__(self, cookie, quiet=False):
                pass

            def check_access(self):
                raise RuntimeError("permission failed")

        result = ("__csrf=x; MUSIC_U=y", {"nickname": "manual", "userId": ""})
        with mock.patch.object(gc, "manual_input", return_value=result), mock.patch.object(
            gc, "save_config"
        ) as save, mock.patch.object(mp, "MusicPartner", FakePartner):
            code = gc.main(["--manual", "--test"])
        self.assertEqual(code, 1)
        save.assert_not_called()

    def test_phone_and_email_paths_are_explicitly_disabled(self):
        with self.assertRaises(SystemExit) as phone:
            gc.main(["--phone", "123"])
        self.assertNotEqual(phone.exception.code, 0)
        with self.assertRaises(SystemExit) as email:
            gc.main(["--email", "x@example.com"])
        self.assertNotEqual(email.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
