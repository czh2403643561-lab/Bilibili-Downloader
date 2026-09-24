import http.client
import json
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import app


QR_URL = "https://example.test/scan?token=fake"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" />'


class LoginQrTests(unittest.TestCase):
    def setUp(self):
        self.previous_login_state = app.LOGIN_STATE.copy()
        app.LOGIN_STATE.clear()

    def tearDown(self):
        app.LOGIN_STATE.clear()
        app.LOGIN_STATE.update(self.previous_login_state)

    def test_qr_svg_generates_valid_svg(self):
        class FakeQrCode:
            def __init__(self, error_correction, border):
                self.matrix = [[True, False], [False, True]]

            def add_data(self, value):
                self.value = value

            def make(self, fit):
                self.fit = fit

            def get_matrix(self):
                return self.matrix

        fake_qrcode = SimpleNamespace(
            QRCode=FakeQrCode,
            constants=SimpleNamespace(ERROR_CORRECT_M=1),
        )
        with patch.object(app, "qr_module", return_value=fake_qrcode):
            result = app.qr_svg("https://example.test/scan")
        self.assertEqual(ET.fromstring(result).tag, "{http://www.w3.org/2000/svg}svg")

    def test_start_login_checks_svg_before_returning_waiting_status(self):
        fake_response = (200, {"code": 0, "data": {"url": QR_URL, "qrcode_key": "fake-key"}}, [])
        with (
            patch.object(app, "anonymous_cookie"),
            patch.object(app, "login_request", return_value=fake_response),
            patch.object(app.BILIBILI_SESSION, "merge_set_cookies"),
            patch.object(app.BILIBILI_SESSION, "log_cookie_names"),
            patch.object(app.LOG, "warning"),
            patch.object(app, "qr_svg", side_effect=ValueError("二维码组件准备失败，请重启工具后重试。")) as render,
        ):
            with self.assertRaisesRegex(ValueError, "二维码组件准备失败"):
                app.start_login()
        render.assert_called_once_with(QR_URL)
        self.assertNotIn("qrcode_key", app.LOGIN_STATE)

    def test_each_login_uses_a_cache_busted_image_url_without_login_url(self):
        response = (200, {"code": 0, "data": {"url": QR_URL, "qrcode_key": "fake-key"}}, [])
        with (
            patch.object(app, "anonymous_cookie"),
            patch.object(app, "login_request", return_value=response),
            patch.object(app.BILIBILI_SESSION, "merge_set_cookies"),
            patch.object(app.BILIBILI_SESSION, "log_cookie_names"),
            patch.object(app, "qr_svg", return_value=SVG),
        ):
            first = app.start_login()["qr_image"]
            second = app.start_login()["qr_image"]
        self.assertRegex(first, r"^/api/login/qr\.svg\?v=[a-f0-9]+$")
        self.assertRegex(second, r"^/api/login/qr\.svg\?v=[a-f0-9]+$")
        self.assertNotEqual(first, second)
        self.assertNotIn(QR_URL, first)

    def test_poll_login_keeps_waiting_behavior(self):
        app.LOGIN_STATE.update({"qrcode_key": "fake-key", "expires_at": app.time.time() + 30})
        with patch.object(app, "login_request", return_value=(200, {"data": {"code": 86101}}, [])):
            result = app.poll_login()
        self.assertEqual(result, {"logged_in": False, "status": "等待扫码"})

    def test_frontend_explains_image_failure_and_offers_retry(self):
        static = Path(__file__).resolve().parents[1] / "static"
        source = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("$('#login-qr').addEventListener('error'", source)
        self.assertIn("二维码加载失败，请点击“重新生成二维码”重试。", source)
        self.assertIn("$('#login-start').textContent = '重新生成二维码'", source)

    def test_bilibili_download_and_transcription_entries_remain_bound(self):
        static = Path(__file__).resolve().parents[1] / "static"
        source = (static / "app.js").read_text(encoding="utf-8")
        page = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("$('#download-button').addEventListener('click', createTask)", source)
        self.assertIn("单视频", page)
        self.assertIn("UP 主批量", page)
        self.assertIn("转写中心", page)


class LoginQrHttpTests(unittest.TestCase):
    def setUp(self):
        self.previous_login_state = app.LOGIN_STATE.copy()
        app.LOGIN_STATE.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        app.LOGIN_STATE.clear()
        app.LOGIN_STATE.update(self.previous_login_state)

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_qr_endpoint_uses_saved_url_without_a_url_query(self):
        app.LOGIN_STATE["qr_url"] = QR_URL
        with patch.object(app, "qr_svg", return_value=SVG) as render:
            status, headers, body = self.request("/api/login/qr.svg?v=unique")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("image/svg+xml"))
        self.assertEqual(body, SVG)
        render.assert_called_once_with(QR_URL)

    def test_qr_endpoint_returns_404_without_a_saved_url(self):
        status, _, _ = self.request("/api/login/qr.svg")
        self.assertEqual(status, 404)

    def test_fake_login_start_flows_into_local_qr_endpoint(self):
        fake_response = (200, {"code": 0, "data": {"url": QR_URL, "qrcode_key": "fake-key"}}, [])
        with (
            patch.object(app, "anonymous_cookie"),
            patch.object(app, "login_request", return_value=fake_response),
            patch.object(app.BILIBILI_SESSION, "merge_set_cookies"),
            patch.object(app.BILIBILI_SESSION, "log_cookie_names"),
            patch.object(app, "qr_svg", return_value=SVG) as render,
        ):
            status, _, response_body = self.request(
                "/api/login/start", method="POST", body="{}", headers={"Content-Type": "application/json"}
            )
            result = json.loads(response_body)
            image_status, image_headers, image_body = self.request(result["qr_image"])
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "等待扫码")
        self.assertNotIn(QR_URL, result["qr_image"])
        self.assertNotIn("fake-key", result["qr_image"])
        self.assertEqual(image_status, 200)
        self.assertTrue(image_headers["Content-Type"].startswith("image/svg+xml"))
        self.assertEqual(image_body, SVG)
        self.assertEqual(render.call_args_list, [call(QR_URL), call(QR_URL)])

    def test_login_start_returns_error_when_qr_generation_fails(self):
        fake_response = (200, {"code": 0, "data": {"url": QR_URL, "qrcode_key": "fake-key"}}, [])
        with (
            patch.object(app, "anonymous_cookie"),
            patch.object(app, "login_request", return_value=fake_response),
            patch.object(app.BILIBILI_SESSION, "merge_set_cookies"),
            patch.object(app.BILIBILI_SESSION, "log_cookie_names"),
            patch.object(app.LOG, "warning"),
            patch.object(app, "qr_svg", side_effect=ValueError("generator failed")),
        ):
            status, _, response_body = self.request(
                "/api/login/start", method="POST", body="{}", headers={"Content-Type": "application/json"}
            )
        self.assertEqual(status, 400)
        self.assertIn("二维码组件准备失败，请重启工具后重试。", json.loads(response_body)["error"])
        self.assertNotIn("qrcode_key", app.LOGIN_STATE)


if __name__ == "__main__":
    unittest.main()
