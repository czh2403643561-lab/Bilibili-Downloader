import logging
import unittest

from meeting_bridge import MeetingBridgeService


class MeetingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.bridge = MeetingBridgeService("testbridgeid1234567890abcdefgh", lambda: self.now)
        self.origin = "chrome-extension://testbridgeid1234567890abcdefgh"
        self.edge_origin = "edge-extension://testbridgeid1234567890abcdefgh"
        self.token = self.bridge.pair(self.origin)["token"]

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
        claimed = self.bridge.claim_next(self.origin, self.token)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertIsNone(self.bridge.claim_next(self.origin, self.token))
        self.assertEqual(self.bridge.get_task(task["task_id"])["status"], "processing")

    def test_success_result_is_safe_for_frontend(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.origin, self.token)
        public = self.bridge.finish(task["task_id"], self.origin, self.token, {
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
        self.bridge.claim_next(self.origin, self.token)
        self.bridge.finish(task["task_id"], self.origin, self.token, {
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
        self.bridge.claim_next(self.origin, self.token)
        self.bridge.update_progress(task["task_id"], self.origin, self.token, "waiting_media")
        self.assertEqual(self.bridge.get_task(task["task_id"])["stage"], "waiting_media")
        with self.assertRaises(ValueError):
            self.bridge.update_progress(task["task_id"], self.origin, self.token, "invented")

    def test_failed_result_uses_safe_message_not_extension_payload(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.origin, self.token)
        public = self.bridge.finish(task["task_id"], self.origin, self.token, {
            "status": "failed", "code": "LOGIN_REQUIRED", "message": "private https://example.test?token=secret",
        })
        self.assertEqual(public["error"]["code"], "LOGIN_REQUIRED")
        self.assertNotIn("private", public["error"]["message"])

    def test_ttl_removes_task_and_sensitive_context(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/recording")
        self.bridge.claim_next(self.origin, self.token)
        self.bridge.finish(task["task_id"], self.origin, self.token, {
            "status": "success", "title": "课",
            "media": {"url": "https://cdn.qcloud.com/a.mp4", "headers": {"cookie": "secret"}},
        })
        self.now += 1801
        with self.assertRaises(KeyError):
            self.bridge.get_task(task["task_id"])

    def test_heartbeat_connected_timeout(self):
        self.bridge.heartbeat(self.origin, self.token, {"version": "0.1.0", "browser": "chrome"})
        self.assertTrue(self.bridge.status()["connected"])
        self.now += 46
        self.assertFalse(self.bridge.status()["connected"])

    def test_origin_and_token_are_both_required(self):
        self.assertFalse(self.bridge.authorized("https://evil.example", self.token))
        self.assertFalse(self.bridge.authorized(self.origin, "wrong"))
        self.assertTrue(self.bridge.authorized(self.origin, self.token))
        self.assertTrue(self.bridge.authorized(self.edge_origin, self.token))
        with self.assertRaises(PermissionError):
            self.bridge.pair("chrome-extension://otherid")

    def test_heartbeat_rejects_invalid_payload(self):
        with self.assertRaises(ValueError):
            self.bridge.heartbeat(self.origin, self.token, {"version": "0.1.0", "browser": "firefox"})

    def test_sensitive_values_are_not_logged(self):
        task = self.bridge.create_task("https://meeting.tencent.com/cw/private?signature=do-not-log")
        self.bridge.claim_next(self.origin, self.token)
        with self.assertNoLogs(logging.getLogger(), level="INFO"):
            self.bridge.finish(task["task_id"], self.origin, self.token, {
                "status": "success", "title": "private title",
                "media": {
                    "url": "https://cdn.tencent.com/private.mp4?signature=do-not-log",
                    "headers": {"cookie": "do-not-log", "authorization": "do-not-log"},
                },
            })


if __name__ == "__main__":
    unittest.main()
