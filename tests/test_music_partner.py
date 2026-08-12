import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from _support import RUNTIME_DIR
import music_partner as mp


class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=None):
        self.payload = payload
        self.status_code = status
        self.json_error = json_error
        self.headers = {"Content-Type": "application/json"}
        self.cookies = requests.cookies.RequestsCookieJar()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("request failed", response=self)

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class QueueSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


class DispatchSession:
    def __init__(self):
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.sign_calls = 0

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "nuser/account" in url:
            return FakeResponse({"profile": {"nickname": "tester"}})
        if "daily/task" in url:
            return FakeResponse({
                "code": 200,
                "data": {
                    "id": "1",
                    "works": [],
                    "completedCount": 0,
                    "integral": 0,
                    "dailyTaskScoreLimit": {
                        "dailyBasicTaskScore": 8,
                        "dailyMaxExtendEvaluateScore": 15,
                    },
                },
            })
        if "evaluate/record" in url:
            return FakeResponse({"code": 200, "data": {"weekList": []}})
        raise AssertionError(f"unexpected GET endpoint: {url}")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "extra/wait" in url:
            return FakeResponse({"code": 200, "data": []})
        if "interact/report" in url:
            return FakeResponse({"code": 200, "data": {"interactResult": True}})
        if "work/evaluate" in url:
            self.sign_calls += 1
            if self.sign_calls == 1:
                return FakeResponse({"code": 200, "data": {"evaluateRes": True}})
            return FakeResponse({"code": 200, "data": {"evaluateRes": True}})
        raise AssertionError(f"unexpected POST endpoint: {url}")


def make_partner():
    return mp.MusicPartner("__csrf=csrf-secret; MUSIC_U=music-secret", quiet=True)


class MusicPartnerNetworkTests(unittest.TestCase):
    def test_authenticated_endpoints_are_https_and_every_call_has_timeout(self):
        partner = make_partner()
        session = DispatchSession()
        partner.session = session
        partner._sleep_random = lambda *args, **kwargs: None

        partner._get_user_name()
        partner.fetch_task()
        partner.rate_work("1", "2")
        partner.fetch_extra_works()
        partner.fetch_today_record()
        partner.rate_extra_work("1", {"id": 3, "resourceId": 4})

        for endpoint in (partner.task_url, partner.extra_list_url, partner.record_url):
            self.assertTrue(endpoint.startswith("https://"), endpoint)
        self.assertTrue(session.calls)
        for _, _, kwargs in session.calls:
            self.assertEqual(kwargs.get("timeout"), mp.REQUEST_TIMEOUT)
            self.assertIs(kwargs.get("allow_redirects"), False)

    def test_redirect_is_rejected_without_following(self):
        partner = make_partner()
        partner.session = QueueSession([FakeResponse({}, status=302)])
        with self.assertRaises(mp.RequestFailure):
            partner._request_json("GET", partner.task_url, "重定向测试")
        self.assertEqual(len(partner.session.calls), 1)

    def test_get_retries_are_limited_and_post_is_not_retried(self):
        partner = make_partner()
        partner.session = QueueSession([
            requests.Timeout("GET https://example.invalid/?csrf_token=secret"),
            FakeResponse({"code": 200}),
        ])
        self.assertEqual(partner._request_json("GET", "https://example.invalid", "GET测试")["code"], 200)
        self.assertEqual(len(partner.session.calls), 2)

        partner.session = QueueSession([requests.Timeout("POST https://example.invalid/?csrf_token=secret")])
        with self.assertRaises(mp.RequestFailure):
            partner._request_json("POST", "https://example.invalid", "POST测试")
        self.assertEqual(len(partner.session.calls), 1)

    def test_timeout_non_json_and_http_error_are_failures_not_no_work(self):
        cases = [
            requests.Timeout("timeout"),
            FakeResponse(json_error=ValueError("not json")),
            FakeResponse({"error": "bad"}, status=503),
        ]
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__):
                partner = make_partner()
                needs_retry = isinstance(outcome, requests.Timeout) or getattr(outcome, "status_code", 0) >= 500
                partner.session = QueueSession([outcome, outcome] if needs_retry else [outcome])
                with self.assertRaises(mp.RequestFailure):
                    partner._request_json("GET", "https://example.invalid", "故障测试")

    def test_unknown_structure_maps_to_unknown(self):
        partner = make_partner()
        partner.fetch_task = mock.Mock(side_effect=mp.UnknownResponse("unknown"))
        self.assertIs(partner.run(), mp.TaskStatus.UNKNOWN)

    def test_request_failure_maps_to_failed(self):
        partner = make_partner()
        partner.fetch_task = mock.Mock(side_effect=mp.RequestFailure("failed"))
        self.assertIs(partner.run(), mp.TaskStatus.FAILED)

    def test_sensitive_values_are_redacted_from_error_logs(self):
        partner = make_partner()
        secret_url = "https://example.invalid/api?csrf_token=csrf-secret&MUSIC_U=music-secret&key=qr-secret"
        partner.session = QueueSession([requests.Timeout(secret_url), requests.Timeout(secret_url)])
        with self.assertLogs(mp.log, level="WARNING") as captured:
            with self.assertRaises(mp.RequestFailure):
                partner._request_json("GET", secret_url, "脱敏测试")
        output = "\n".join(captured.output)
        for secret in ("csrf-secret", "music-secret", "qr-secret"):
            self.assertNotIn(secret, output)
        self.assertNotIn(secret_url, output)

        dictionary_error = {
            "Cookie": "foo=raw-cookie",
            "csrf_token": "dict-csrf",
            "MUSIC_U": "dict-music-u",
            "key": "dict-key",
        }
        redacted = mp.redact_sensitive(dictionary_error)
        for secret in ("raw-cookie", "dict-csrf", "dict-music-u", "dict-key"):
            self.assertNotIn(secret, redacted)
        header = mp.redact_sensitive("Cookie: foo=alpha; bar=beta; __csrf=gamma")
        for secret in ("alpha", "beta", "gamma"):
            self.assertNotIn(secret, header)

    def test_malformed_daily_task_is_unknown(self):
        partner = make_partner()
        partner.session = QueueSession([FakeResponse({"code": 200, "data": {}})])
        with self.assertRaises(mp.UnknownResponse):
            partner.fetch_task()

    def test_malformed_nested_task_fields_are_unknown(self):
        partner = make_partner()
        payload = {
            "code": 200,
            "data": {
                "id": "1",
                "works": [],
                "completedCount": 0,
                "integral": 0,
                "dailyTaskScoreLimit": {},
            },
        }
        partner.session = QueueSession([FakeResponse(payload)])
        with self.assertRaises(mp.UnknownResponse):
            partner.fetch_task()

    def test_basic_score_requires_explicit_success_result(self):
        partner = make_partner()
        partner.session = QueueSession([FakeResponse({"code": 200})])
        self.assertIs(partner.rate_work("1", "2"), mp.TaskStatus.UNKNOWN)
        partner.session = QueueSession([FakeResponse({"code": 200, "data": {"evaluateRes": False}})])
        self.assertIs(partner.rate_work("1", "2"), mp.TaskStatus.FAILED)

    def test_explicit_interaction_failure_is_failed(self):
        partner = make_partner()
        partner.session = QueueSession([FakeResponse({"code": 500, "message": "failed"})])
        self.assertIs(
            partner.report_resource_interact({"id": 1, "resourceId": 2}),
            mp.TaskStatus.FAILED,
        )


class MusicPartnerFlowTests(unittest.TestCase):
    def test_empty_server_work_is_no_work(self):
        partner = make_partner()
        task = {
            "id": "1",
            "integral": 8,
            "completedCount": 5,
            "works": [],
            "dailyTaskScoreLimit": {"dailyBasicTaskScore": 8, "dailyMaxExtendEvaluateScore": 15},
        }
        partner.fetch_task = mock.Mock(return_value=task)
        partner.fetch_extra_works = mock.Mock(return_value=[])
        partner.fetch_today_record = mock.Mock(return_value=None)
        partner._get_user_name = mock.Mock(return_value="tester")
        partner._is_extra_no_more_today = mock.Mock(return_value=False)
        partner._get_extra_done_today = mock.Mock(return_value=None)
        self.assertIs(partner.run(), mp.TaskStatus.NO_WORK)

    def test_business_order_remains_basic_then_interact_then_extra(self):
        partner = make_partner()
        order = []
        initial_task = {
            "id": "10",
            "integral": 0,
            "completedCount": 0,
            "works": [{"completed": False, "work": {"id": 1, "name": "basic", "authorName": "a"}}],
            "dailyTaskScoreLimit": {"dailyBasicTaskScore": 1, "dailyMaxExtendEvaluateScore": 1},
        }
        completed_task = {
            **initial_task,
            "integral": 1,
            "completedCount": 1,
            "works": [{"completed": True, "work": {"id": 1, "name": "basic", "authorName": "a"}}],
        }
        partner.fetch_task = mock.Mock(side_effect=[initial_task, completed_task, completed_task])
        partner.rate_work = mock.Mock(side_effect=lambda *args: order.append("basic") or mp.TaskStatus.SUCCESS)
        partner.fetch_extra_works = mock.Mock(return_value=[
            {"completed": False, "work": {"id": 2, "resourceId": 3, "name": "extra", "authorName": "b"}}
        ])
        partner.fetch_today_record = mock.Mock(
            side_effect=[None, {"completeCount": 2, "taskIntegral": 2, "taskCompleted": True}]
        )
        partner._get_user_name = mock.Mock(return_value="tester")
        partner._is_extra_no_more_today = mock.Mock(return_value=False)
        partner._get_extra_done_today = mock.Mock(return_value=None)
        partner._sleep_random = mock.Mock()
        partner.rate_extra_work = mock.Mock(
            side_effect=lambda *args: order.extend(["interact", "extra"]) or mp.TaskStatus.SUCCESS
        )
        self.assertIs(partner.run(), mp.TaskStatus.SUCCESS)
        self.assertEqual(order, ["basic", "interact", "extra"])

    def test_successful_submission_without_completion_confirmation_is_unknown(self):
        partner = make_partner()
        task = {
            "id": "1",
            "integral": 0,
            "completedCount": 0,
            "works": [{"completed": False, "work": {"id": 1, "name": "basic", "authorName": "a"}}],
            "dailyTaskScoreLimit": {"dailyBasicTaskScore": 1, "dailyMaxExtendEvaluateScore": 0},
        }
        partner.fetch_task = mock.Mock(return_value=task)
        partner.rate_work = mock.Mock(return_value=mp.TaskStatus.SUCCESS)
        partner.fetch_extra_works = mock.Mock(return_value=[])
        partner.fetch_today_record = mock.Mock(return_value=None)
        partner._get_user_name = mock.Mock(return_value="tester")
        partner._is_extra_no_more_today = mock.Mock(return_value=False)
        partner._get_extra_done_today = mock.Mock(return_value=None)
        partner._sleep_random = mock.Mock()
        self.assertIs(partner.run(), mp.TaskStatus.UNKNOWN)

    def test_local_no_more_cache_is_not_completion_evidence(self):
        partner = make_partner()
        completed_task = {
            "id": "1",
            "integral": 1,
            "completedCount": 1,
            "works": [{"completed": True, "work": {"id": 1, "name": "basic", "authorName": "a"}}],
            "dailyTaskScoreLimit": {"dailyBasicTaskScore": 1, "dailyMaxExtendEvaluateScore": 1},
        }
        partner.fetch_task = mock.Mock(return_value=completed_task)
        partner.fetch_today_record = mock.Mock(return_value=None)
        partner._is_extra_no_more_today = mock.Mock(return_value=True)
        self.assertFalse(partner._verify_completion(1, 1))

    def test_failed_interaction_prevents_extra_score_post(self):
        partner = make_partner()
        partner.report_resource_interact = mock.Mock(return_value=mp.TaskStatus.FAILED)
        partner.session = mock.Mock()
        result = partner.rate_extra_work("1", {"id": 2, "resourceId": 3})
        self.assertIs(result, mp.TaskStatus.FAILED)
        partner.session.post.assert_not_called()


class StateAndCliTests(unittest.TestCase):
    def test_state_atomic_replace_failure_preserves_original(self):
        partner = make_partner()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            original = '{"original": true}'
            state_path.write_text(original, encoding="utf-8")
            with mock.patch.object(mp, "STATE_FILE", str(state_path)), mock.patch.object(
                mp.os, "replace", side_effect=OSError("replace failed")
            ):
                with self.assertRaises(mp.PartnerError):
                    partner._save_state({"changed": True})
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)

    def test_corrupt_state_is_not_silently_overwritten(self):
        partner = make_partner()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            original = "{broken"
            state_path.write_text(original, encoding="utf-8")
            with mock.patch.object(mp, "STATE_FILE", str(state_path)):
                with self.assertRaises(mp.PartnerError):
                    partner._mark_extra_no_more_today(1)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)

    def test_empty_cookie_makes_cli_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"MUSIC_COPARTNER": [{"cookie": ""}]}), encoding="utf-8")
            self.assertEqual(mp.main(["--config", str(config)]), 1)

    def test_multi_account_continues_but_returns_nonzero(self):
        seen = []

        class FakePartner:
            def __init__(self, cookie, delay=None):
                self.cookie = cookie

            def run(self):
                seen.append(self.cookie)
                return mp.TaskStatus.FAILED if self.cookie == "first" else mp.TaskStatus.SUCCESS

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps({"MUSIC_COPARTNER": [{"cookie": "first"}, {"cookie": "second"}]}),
                encoding="utf-8",
            )
            with mock.patch.object(mp, "MusicPartner", FakePartner):
                self.assertEqual(mp.main(["--config", str(config)]), 1)
        self.assertEqual(seen, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
