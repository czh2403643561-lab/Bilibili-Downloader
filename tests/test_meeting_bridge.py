import logging
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import app as courseflow_app
from meeting_bridge import BRIDGE_EXTENSION_ID, MeetingBridgeService


class MeetingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.bridge = MeetingBridgeService("testbridgeid1234567890abcdefgh", lambda: self.now)
        self.addCleanup(self.bridge.shutdown)
        self.extension_id = "testbridgeid1234567890abcdefgh"
        self.browser = "chrome"
        self.origin = "chrome-extension://testbridgeid1234567890abcdefgh"
        self.edge_origin = "edge-extension://testbridgeid1234567890abcdefgh"
        self.token = self.bridge.pair(self.extension_id, self.browser, self.origin)["token"]

    def test_recording_url_allowlist(self):
        self.assertEqual(
            self.bridge.create_task("https://meeting.tencent.com/cw/recording?id=private")["status"],
            "pending",
        )
        self.assertEqual(self.bridge.create_task("https://meeting.tencent.com/crm/recording")["status"], "pending")

    def test_rejects_non_meeting_url_and_unsafe_variants(self):
        for value in (
            "https://example.com/cw/recording",
            "http://meeting.tencent.com/cw/recording",
            "https://meeting.tencent.com.evil.test/cw/recording",
            "https://meeting.tencent.com/login",
            "https://user:pass@meeting.tencent.com/cw/recording",
            "https://meeting.tencent.com:444/cw/recording",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bridge.create_task(value)

    def test_task_can_only_be_claimed_once(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        claimed = self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertIsNone(self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token))
        self.assertEqual(self.bridge.get_task(task["task_id"])["status"], "processing")

    def test_success_result_is_safe_for_frontend(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        public = self.bridge.finish(task["task_id"], self.extension_id, self.browser, self.origin, self.token, {
            "status": "success",
            "title": "真实课程",
            "duration_seconds": 1234,
            "media": {
                "url": "https://media.qcloud.com/course/video.mp4?signature=secret",
                "headers": {
                    "cookie": "session=secret",
                    "authorization": "Bearer secret",
                    "referer": "https://meeting.tencent.com/cw/recording",
                    "x-unexpected": "must-not-persist",
                },
            },
        })
        serialized = repr(public)
        self.assertEqual(public["status"], "success")
        self.assertTrue(public["media_found"])
        self.assertNotIn("qcloud", serialized)
        self.assertNotIn("signature", serialized)
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("headers", serialized)

    def test_media_context_is_never_returned_to_frontend(self):
        task = self.bridge.create_task("https://meeting.tencent.com/crm/recording")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        self.bridge.finish(task["task_id"], self.extension_id, self.browser, self.origin, self.token, {
            "status": "success",
            "title": "课",
            "media": {"url": "https://cdn.tencent-cloud.net/a.mp4", "headers": {"cookie": "secret"}},
        })
        self.assertEqual(self.bridge.get_task(task["task_id"]), {
            "task_id": task["task_id"], "status": "success", "stage": "done", "title": "课",
            "duration_seconds": None, "media_found": True, "error": None,
        })

    def test_progress_updates_only_valid_active_task(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        self.bridge.update_progress(task["task_id"], self.extension_id, self.browser, self.origin, self.token, "waiting_media")
        self.assertEqual(self.bridge.get_task(task["task_id"])["stage"], "waiting_media")
        with self.assertRaises(ValueError):
            self.bridge.update_progress(task["task_id"], self.extension_id, self.browser, self.origin, self.token, "invented")

    def test_failed_result_uses_safe_message_not_extension_payload(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        public = self.bridge.finish(task["task_id"], self.extension_id, self.browser, self.origin, self.token, {
            "status": "failed", "code": "LOGIN_REQUIRED", "message": "private https://example.test?token=secret",
        })
        self.assertEqual(public["error"]["code"], "LOGIN_REQUIRED")
        self.assertNotIn("private", public["error"]["message"])

    def test_ttl_removes_task_and_sensitive_context(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        self.bridge.finish(task["task_id"], self.extension_id, self.browser, self.origin, self.token, {
            "status": "success", "title": "课",
            "media": {"url": "https://cdn.qcloud.com/a.mp4", "headers": {"cookie": "secret"}},
        })
        self.now += 1801
        with self.assertRaises(KeyError):
            self.bridge.get_task(task["task_id"])

    def test_heartbeat_connected_timeout(self):
        self.bridge.heartbeat(self.extension_id, self.browser, self.origin, self.token, {"version": "0.1.0", "browser": "chrome"})
        self.assertTrue(self.bridge.status()["connected"])
        self.now += 46
        self.assertFalse(self.bridge.status()["connected"])

    def test_extension_id_and_process_token_authorize_without_origin(self):
        self.assertTrue(self.bridge.authorized(self.extension_id, "chrome", None, self.token))
        self.assertTrue(self.bridge.authorized(self.extension_id, "edge", self.edge_origin, self.token))
        self.assertFalse(self.bridge.authorized(self.extension_id, "chrome", "https://evil.example", self.token))
        self.assertFalse(self.bridge.authorized(self.extension_id, "chrome", self.origin, "wrong"))
        self.assertFalse(self.bridge.authorized("wrong-extension-id", "chrome", self.origin, self.token))
        self.assertFalse(self.bridge.authorized(self.extension_id, "edge", self.origin, self.token))
        with self.assertRaises(PermissionError):
            self.bridge.pair("wrong-extension-id", "chrome", None)

    def test_pair_token_lives_only_for_server_process(self):
        self.assertEqual(self.bridge.pair(self.extension_id, self.browser, None)["token"], self.token)
        restarted = MeetingBridgeService(self.extension_id, lambda: self.now)
        self.addCleanup(restarted.shutdown)
        self.assertNotEqual(restarted.pair(self.extension_id, self.browser, None)["token"], self.token)

    def test_heartbeat_rejects_invalid_payload(self):
        with self.assertRaises(ValueError):
            self.bridge.heartbeat(self.extension_id, self.browser, self.origin, self.token, {"version": "0.1.0", "browser": "firefox"})

    def test_sensitive_values_are_not_logged(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/private?signature=do-not-log")
        self.bridge.claim_next(self.extension_id, self.browser, self.origin, self.token)
        with self.assertNoLogs(logging.getLogger(), level="INFO"):
            self.bridge.finish(task["task_id"], self.extension_id, self.browser, self.origin, self.token, {
                "status": "success", "title": "private title",
                "media": {
                    "url": "https://cdn.tencent.com/private.mp4?signature=do-not-log",
                    "headers": {"cookie": "do-not-log", "authorization": "do-not-log"},
                },
            })


class MeetingBridgeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.previous_bridge = courseflow_app.MEETING_BRIDGE
        self.bridge = MeetingBridgeService(BRIDGE_EXTENSION_ID)
        courseflow_app.MEETING_BRIDGE = self.bridge
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), courseflow_app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.bridge.shutdown()
        courseflow_app.MEETING_BRIDGE = self.previous_bridge

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def identity(self, browser="chrome", origin=None, token=None):
        headers = {
            "X-CourseFlow-Extension-Id": BRIDGE_EXTENSION_ID,
            "X-CourseFlow-Extension-Browser": browser,
        }
        if origin:
            headers["Origin"] = origin
        if token:
            headers["X-CourseFlow-Bridge-Token"] = token
        return headers

    def test_pair_requires_fixed_extension_id_but_not_origin(self):
        status, headers, body = self.request("GET", "/api/meeting-bridge/pair", self.identity())
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), f"chrome-extension://{BRIDGE_EXTENSION_ID}")
        self.assertTrue(json.loads(body)["token"])
        bad = self.identity()
        bad["X-CourseFlow-Extension-Id"] = "not-the-extension"
        self.assertEqual(self.request("GET", "/api/meeting-bridge/pair", bad)[0], 403)

    def test_heartbeat_requires_token_and_accepts_originless_extension_identity(self):
        identity = self.identity()
        status, _, pair_body = self.request("GET", "/api/meeting-bridge/pair", identity)
        self.assertEqual(status, 200)
        token = json.loads(pair_body)["token"]
        payload = json.dumps({"version": "0.1.0", "browser": "chrome"})
        self.assertEqual(self.request("POST", "/api/meeting-bridge/heartbeat", {
            **identity, "Content-Type": "application/json"
        }, payload)[0], 403)
        status, _, _ = self.request("POST", "/api/meeting-bridge/heartbeat", {
            **identity, "X-CourseFlow-Bridge-Token": token, "Content-Type": "application/json"
        }, payload)
        self.assertEqual(status, 200)
        self.assertTrue(self.bridge.status()["connected"])

    def test_wrong_token_and_malicious_origin_are_rejected(self):
        bad_token = self.identity(token="wrong")
        bad_token["Content-Type"] = "application/json"
        body = json.dumps({"version": "0.1.0", "browser": "chrome"})
        self.assertEqual(self.request("POST", "/api/meeting-bridge/heartbeat", bad_token, body)[0], 403)
        evil = self.identity(origin="https://evil.example", token="wrong")
        self.assertEqual(self.request("GET", "/api/meeting-bridge/pair", evil)[0], 403)

    def test_preflight_is_scoped_and_never_uses_wildcard_origin(self):
        headers = {
            "Origin": f"chrome-extension://{BRIDGE_EXTENSION_ID}",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-courseflow-bridge-token,x-courseflow-extension-id,x-courseflow-extension-browser",
        }
        status, response_headers, _ = self.request("OPTIONS", "/api/meeting-bridge/heartbeat", headers)
        self.assertEqual(status, 204)
        self.assertEqual(response_headers.get("Access-Control-Allow-Origin"), headers["Origin"])
        self.assertNotEqual(response_headers.get("Access-Control-Allow-Origin"), "*")
        headers.pop("Origin")
        self.assertEqual(self.request("OPTIONS", "/api/meeting-bridge/heartbeat", headers)[0], 204)
        headers["Origin"] = "https://evil.example"
        self.assertEqual(self.request("OPTIONS", "/api/meeting-bridge/heartbeat", headers)[0], 403)

    def test_token_is_not_written_to_request_logs(self):
        _, _, pair_body = self.request("GET", "/api/meeting-bridge/pair", self.identity())
        token = json.loads(pair_body)["token"]
        headers = self.identity(token=token)
        headers["Content-Type"] = "application/json"
        with self.assertLogs(courseflow_app.LOG, level="INFO") as captured:
            self.request("POST", "/api/meeting-bridge/heartbeat", headers,
                         json.dumps({"version": "0.1.0", "browser": "chrome"}))
        self.assertNotIn(token, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
