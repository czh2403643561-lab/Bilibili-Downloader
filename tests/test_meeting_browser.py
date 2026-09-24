import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from meeting_browser import (
    MeetingBrowserService,
    MeetingFlowError,
    account_response_authenticated,
    validate_recording_url,
)


class FakeResponse:
    def __init__(self, url="https://meeting.tencent.com/wemeet-tapi/v2/login-logic/user/query-detail", status=200, payload=None):
        self.url = url
        self.status = status
        self._payload = payload if payload is not None else {"code": -51003, "data": []}

    def json(self):
        return self._payload


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    def is_visible(self, timeout=None):
        if "qr-code-wrapper" in self.selector:
            return self.page.qr_visible
        return self.page.visible.get(self.selector, False)

    def count(self):
        return self.page.video_count if self.selector == "video" else 0

    def all(self):
        return []

    def inner_text(self, timeout=None):
        return ""


class FakePage:
    def __init__(self, url="https://meeting.tencent.com/", signal=False, qr_visible=False, video_count=0, visible=None):
        self.url = url
        self.signal = signal
        self.qr_visible = qr_visible
        self.video_count = video_count
        self.visible = visible or {}
        self.frames = [self]
        self.main_frame = self
        self._handlers = {}

    def on(self, event, callback):
        self._handlers.setdefault(event, []).append(callback)

    def goto(self, url, **kwargs):
        self.url = url
        payload = {"code": 0, "data": {"account": "synthetic"}} if self.signal else {"code": -51003, "data": []}
        for callback in self._handlers.get("response", []):
            callback(FakeResponse(payload=payload))
        return FakeResponse(status=200)

    def wait_for_timeout(self, milliseconds):
        pass

    def locator(self, selector):
        return FakeLocator(self, selector)

    def title(self):
        return "腾讯会议"


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, service, signal=False):
        self.service = service
        self.signal = signal
        self.contexts = []

    def launch_persistent_context(self, **kwargs):
        context = FakeContext(FakePage(signal=self.signal))
        self.contexts.append(context)
        return context


class FakePlaywright:
    def __init__(self, service, signal=False):
        self.chromium = FakeChromium(service, signal)


class MeetingBrowserRegressionTests(unittest.TestCase):
    def setUp(self):
        self.service = MeetingBrowserService(lambda: Path("C:/isolated-courseflow-test"))

    def test_qr_visible_overrides_auth_signal(self):
        page = FakePage(signal=True, qr_visible=True, visible={'[class*="user-info"]': True})
        self.assertFalse(self.service._is_authenticated(page, account_signal=True))

    def test_visible_qr_keeps_login_window_waiting_instead_of_succeeding(self):
        visible_context = FakeContext(FakePage(qr_visible=True))
        with patch("meeting_browser.LOGIN_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(MeetingFlowError) as raised:
                self.service._login_operation((None, visible_context))
        self.assertEqual(raised.exception.reason_code, "AUTH_SIGNAL_NOT_CONFIRMED")
        self.assertFalse(visible_context.closed)
        self.assertFalse(self.service._verified_login)

    def test_generic_user_info_without_account_signal_is_not_authenticated(self):
        page = FakePage(visible={'[class*="user-info"]': True})
        self.assertFalse(self.service._is_authenticated(page, account_signal=False))

    def test_official_account_probe_requires_successful_nonempty_data(self):
        self.assertFalse(account_response_authenticated(200, {"code": -51003, "data": []}))
        self.assertFalse(account_response_authenticated(200, {"code": 0, "data": []}))
        self.assertTrue(account_response_authenticated(200, {"code": 0, "data": {"id": "synthetic"}}))
        self.assertFalse(account_response_authenticated(403, {"code": 0, "data": {"id": "synthetic"}}))

    def test_only_explicit_official_login_routes_are_login_redirects(self):
        self.assertTrue(self.service._is_explicit_login_redirect("https://meeting.tencent.com/login.html"))
        self.assertTrue(self.service._is_explicit_login_redirect("https://login.wecom.tencent.com/wwlogin/partner/login"))
        self.assertFalse(self.service._is_explicit_login_redirect("https://meeting.tencent.com/cw/recording"))
        self.assertFalse(self.service._is_explicit_login_redirect("https://unrelated.example/login"))
        self.assertFalse(self.service._is_explicit_login_redirect("https://unrelated.example/cw/recording"))

    def test_recording_route_without_homepage_avatar_is_accessible(self):
        page = FakePage("https://meeting.tencent.com/cw/recording", video_count=1)
        access, player_found, _ = self.service._classify_recording_access(page, FakeResponse())
        self.assertEqual(access, "RECORDING_PAGE")
        self.assertTrue(player_found)

    def test_recording_route_without_avatar_can_continue_to_media_phase(self):
        page = FakePage("https://meeting.tencent.com/cw/recording", video_count=0)
        access, player_found, _ = self.service._classify_recording_access(page, FakeResponse())
        self.assertEqual(access, "RECORDING_PAGE")
        self.assertFalse(player_found)

    def test_fresh_profile_verification_failure_never_sets_logged_in(self):
        visible_context = FakeContext(FakePage(signal=True))
        playwright = FakePlaywright(self.service, signal=False)
        with patch.object(self.service, "_browser_executable", return_value=Path("C:/fake-browser.exe")):
            with self.assertRaises(MeetingFlowError) as raised:
                self.service._login_operation((playwright, visible_context))
        self.assertEqual(raised.exception.reason_code, "PROFILE_SESSION_NOT_PERSISTED")
        self.assertTrue(visible_context.closed)
        self.assertFalse(self.service._verified_login)
        self.assertEqual(len(playwright.chromium.contexts), 1)

    def test_explicit_login_page_classifies_as_login_required(self):
        page = FakePage("https://meeting.tencent.com/login.html")
        access, _, _ = self.service._classify_recording_access(page, FakeResponse())
        self.assertEqual(access, "LOGIN_REQUIRED")

    def test_recording_url_allowlist_remains_strict(self):
        self.assertEqual(validate_recording_url("https://meeting.tencent.com/cw/a/b"), "https://meeting.tencent.com/cw/a/b")
        for url in (
            "http://meeting.tencent.com/cw/a",
            "https://meeting.tencent.com.evil.test/cw/a",
            "https://meeting.tencent.com:444/cw/a",
            "https://evil.example/crm/a",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_recording_url(url)

    def test_failure_log_contains_reason_code_not_exception_url_or_credentials(self):
        class FakeRuntime:
            def start(self):
                return playwright

        class RuntimeModule:
            def sync_playwright(self):
                return FakeRuntime()

        class RuntimeChromium:
            def launch_persistent_context(self, **kwargs):
                return FakeContext(FakePage())

        class RuntimePlaywright:
            chromium = RuntimeChromium()

            def stop(self):
                pass

        playwright = RuntimePlaywright()
        failure = "denied https://meeting.tencent.com/cw/private?token=synthetic Cookie=synthetic"
        with patch.object(self.service, "_playwright_module", return_value=RuntimeModule()), patch.object(
            self.service, "_browser_executable", return_value=Path("C:/fake-browser.exe")
        ), self.assertLogs("BilibiliDownloader", logging.WARNING) as captured:
            self.service._run_browser_operation(
                "parsing", lambda runtime: (_ for _ in ()).throw(MeetingFlowError(failure, "RECORDING_ACCESS_DENIED"))
            )
        output = "\n".join(captured.output)
        self.assertIn("reason=RECORDING_ACCESS_DENIED", output)
        self.assertNotIn("private", output)
        self.assertNotIn("synthetic", output)


if __name__ == "__main__":
    unittest.main()
