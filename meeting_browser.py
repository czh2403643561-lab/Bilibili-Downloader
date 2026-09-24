"""腾讯会议独立浏览器会话与单条回放媒体识别。"""

from __future__ import annotations

import importlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEETING_HOME = "https://meeting.tencent.com/"
MEDIA_TTL_SECONDS = 30 * 60
LOGIN_TIMEOUT_SECONDS = 5 * 60
SAFE_REQUEST_HEADERS = {
    "accept", "accept-language", "authorization", "cookie", "origin",
    "referer", "user-agent", "range",
}
LOG = logging.getLogger("BilibiliDownloader")


def validate_recording_url(value: str) -> str:
    if len(value) > 4096:
        raise ValueError("回放链接过长，无法解析。")
    parsed = urllib.parse.urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if (parsed.scheme != "https" or host != "meeting.tencent.com" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or not re.match(r"^/(?:cw|crm)/.+", path, re.I)):
        raise ValueError("请输入腾讯会议 https://meeting.tencent.com/cw/... 或 /crm/... 回放链接。")
    return urllib.parse.urlunsplit(("https", "meeting.tencent.com", path, parsed.query, ""))


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).strip().lower()
        text = str(value)
        if name in SAFE_REQUEST_HEADERS and len(text) <= 4096 and "\r" not in text and "\n" not in text:
            result[name] = text
    return result


class MeetingBrowserService:
    """所有 Playwright 对象都在所属 worker 线程内创建、使用并关闭。"""

    def __init__(self, app_data_provider):
        self._app_data_provider = app_data_provider
        self._operation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._state: dict[str, Any] = {"status": "idle", "message": "尚未登录", "logged_in": False}
        self._cancel = threading.Event()
        self._closed = threading.Event()
        self._results: dict[str, dict[str, Any]] = {}
        self._last_verified = 0.0
        self._verified_login = False

    @property
    def profile_dir(self) -> Path:
        return Path(self._app_data_provider()) / "meeting-browser-profile"

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            operation = self._state.copy()
            if operation.get("status") in {"idle", "logged_in", "logged_out", "failed"} and time.monotonic() - self._last_verified > 60:
                if self.profile_dir.is_dir() and not self._closed.is_set():
                    self._start_worker("正在核验腾讯会议登录状态", self._verify_worker)
                    operation = self._state.copy()
                elif not self.profile_dir.is_dir():
                    operation.update(status="logged_out", message="尚未登录", logged_in=False)
            operation["logged_in"] = bool(operation.get("logged_in") and self._verified_login)
            return operation

    def start_login(self) -> dict[str, Any]:
        with self._state_lock:
            if self._state.get("status") in {"starting", "waiting_login", "checking", "parsing"}:
                raise ValueError("腾讯会议浏览器正在执行其他操作，请稍后重试。")
            if self._closed.is_set():
                raise ValueError("本地服务正在关闭，请重新启动后再试。")
            self._cancel.clear()
            self._state = {"status": "starting", "message": "正在打开腾讯会议官方登录页…", "logged_in": False}
            threading.Thread(target=self._login_worker, name="meeting-login", daemon=True).start()
            return self._state.copy()

    def logout(self) -> dict[str, Any]:
        with self._state_lock:
            if self._state.get("status") in {"starting", "waiting_login", "checking", "parsing"}:
                raise ValueError("浏览器操作进行中，暂不能清除登录状态。")
            self._verified_login = False
            self._last_verified = time.monotonic()
            self._results.clear()
            profile = self.profile_dir
            if profile.exists():
                shutil.rmtree(profile)
            self._state = {"status": "logged_out", "message": "已退出登录", "logged_in": False}
            return self._state.copy()

    def parse(self, value: str) -> dict[str, Any]:
        recording_url = validate_recording_url(value)
        with self._state_lock:
            if self._closed.is_set():
                raise ValueError("本地服务正在关闭，请重新启动后再试。")
            if self._state.get("status") in {"starting", "waiting_login", "checking", "parsing"}:
                raise ValueError("腾讯会议浏览器正在执行其他操作，请稍后重试。")
            self._cancel.clear()
            self._state = {"status": "parsing", "message": "正在后台解析回放…", "logged_in": self._verified_login}
            threading.Thread(target=self._parse_worker, args=(recording_url,), name="meeting-parse", daemon=True).start()
            return self._state.copy()

    def result(self, parse_id: str) -> dict[str, Any]:
        now = time.time()
        with self._state_lock:
            for key in [key for key, item in self._results.items() if item["expires_at"] <= now]:
                self._results.pop(key, None)
            item = self._results.get(parse_id)
            if not item:
                raise KeyError("解析结果已过期，请重新解析回放。")
            # Future download code may retrieve this only from this process-local store.
            return {key: value for key, value in item.items() if key not in {"expires_at"}}

    def shutdown(self) -> None:
        self._closed.set()
        self._cancel.set()

    def _start_worker(self, message: str, target) -> None:
        self._state = {"status": "checking", "message": message, "logged_in": False}
        threading.Thread(target=target, name="meeting-status", daemon=True).start()

    def _login_worker(self) -> None:
        self._run_browser_operation("waiting_login", self._login_operation)

    def _verify_worker(self) -> None:
        self._run_browser_operation("checking", self._verify_operation)

    def _parse_worker(self, recording_url: str) -> None:
        self._run_browser_operation("parsing", lambda playwright: self._parse_operation(playwright, recording_url))

    def _run_browser_operation(self, initial_status: str, operation) -> None:
        started = time.monotonic()
        try:
            with self._operation_lock:
                if self._closed.is_set():
                    raise ValueError("本地服务正在关闭。")
                playwright_module = self._playwright_module()
                playwright = playwright_module.sync_playwright().start()
                context = None
                try:
                    browser = self._browser_executable()
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        executable_path=str(browser),
                        headless=initial_status != "waiting_login",
                        accept_downloads=False,
                        args=["--disable-blink-features=AutomationControlled"],
                        timeout=30000,
                    )
                    operation((playwright, context))
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                    playwright.stop()
        except Exception as error:
            message = self._friendly_error(error)
            LOG.warning("腾讯会议浏览器操作失败 status=%s kind=%s", initial_status, type(error).__name__)
            with self._state_lock:
                self._verified_login = False if initial_status != "parsing" else self._verified_login
                self._last_verified = time.monotonic()
                self._state = {"status": "failed", "message": message, "logged_in": self._verified_login}
        else:
            LOG.info("腾讯会议浏览器操作完成 status=%s elapsed=%.1fs", initial_status, time.monotonic() - started)

    def _playwright_module(self):
        packages = self._app_data_provider() / "python-packages"
        if str(packages) not in sys.path:
            sys.path.insert(0, str(packages))
        try:
            return importlib.import_module("playwright.sync_api")
        except (ImportError, AttributeError):
            packages.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location", "--target", str(packages), "playwright"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                timeout=120, check=False, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                LOG.warning("Playwright 依赖准备失败 exit=%s", result.returncode)
                raise RuntimeError("腾讯会议浏览器组件准备失败，请检查网络后重试。")
            importlib.invalidate_caches()
            return importlib.import_module("playwright.sync_api")

    def _browser_executable(self) -> Path:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        candidates = [
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise RuntimeError("未找到 Microsoft Edge 或 Google Chrome，请安装其中一个浏览器后重试。")

    def _login_operation(self, runtime) -> None:
        _, context = runtime
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MEETING_HOME, wait_until="domcontentloaded", timeout=60000)
        self._click_login_entry(page)
        with self._state_lock:
            self._state = {"status": "waiting_login", "message": "请在腾讯会议官方窗口中使用微信扫码登录。", "logged_in": False}
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self._cancel.is_set() and not self._closed.is_set():
            if self._is_logged_in(page):
                with self._state_lock:
                    self._verified_login = True
                    self._last_verified = time.monotonic()
                    self._state = {"status": "logged_in", "message": "腾讯会议已登录", "logged_in": True}
                return
            time.sleep(1)
        if self._cancel.is_set() or self._closed.is_set():
            raise RuntimeError("登录操作已关闭。")
        raise RuntimeError("等待扫码登录超时（5 分钟），请重新尝试。")

    def _verify_operation(self, runtime) -> None:
        _, context = runtime
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MEETING_HOME, wait_until="domcontentloaded", timeout=60000)
        logged_in = self._is_logged_in(page)
        with self._state_lock:
            self._verified_login = logged_in
            self._last_verified = time.monotonic()
            self._state = {
                "status": "logged_in" if logged_in else "logged_out",
                "message": "腾讯会议已登录" if logged_in else "登录已失效，请重新扫码",
                "logged_in": logged_in,
            }

    def _parse_operation(self, runtime, recording_url: str) -> None:
        _, context = runtime
        page = context.pages[0] if context.pages else context.new_page()
        candidates: dict[str, dict[str, Any]] = {}

        def on_response(response) -> None:
            try:
                headers = response.all_headers()
                content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                url = response.url
                is_mp4 = content_type in {"video/mp4", "application/mp4"} or urllib.parse.urlsplit(url).path.lower().endswith(".mp4")
                if not is_mp4 or response.status >= 400 or urllib.parse.urlsplit(url).scheme != "https":
                    return
                request = response.request
                request_headers = safe_headers(request.all_headers())
                if len(candidates) >= 50 and url not in candidates:
                    return
                candidates[url] = {"url": url, "headers": request_headers}
            except Exception:
                return

        page.on("response", on_response)
        response = page.goto(recording_url, wait_until="domcontentloaded", timeout=90000)
        if self._looks_like_login_redirect(page.url, page.title()):
            with self._state_lock:
                self._verified_login = False
                self._last_verified = time.monotonic()
                self._state = {"status": "logged_out", "message": "腾讯会议登录已失效，请重新扫码后解析。", "logged_in": False}
            return
        if not self._is_logged_in(page):
            raise RuntimeError("无法确认腾讯会议登录状态；请先在设置中完成官方扫码登录。")
        if response is not None and response.status >= 400:
            raise RuntimeError(f"腾讯会议回放页面返回 HTTP {response.status}。请确认链接有效且当前账号有访问权限。")
        try:
            page.locator("video").evaluate_all("els => els.forEach(v => { v.muted = true; v.preload = 'auto'; })")
            page.locator("video").first.evaluate("v => v.play().catch(() => {})")
        except Exception:
            pass
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline and not candidates and not self._cancel.is_set():
            page.wait_for_timeout(500)
        if self._cancel.is_set():
            raise RuntimeError("解析已取消。")
        if not candidates:
            raise RuntimeError("页面未发现可用的 MP4 视频流。请确认该回放可播放，并且账号有查看权限。")
        videos = page.locator("video").evaluate_all("els => els.map(v => ({duration: Number.isFinite(v.duration) ? v.duration : null, title: v.getAttribute('aria-label') || ''}))")
        duration = next((int(round(item["duration"])) for item in videos if item.get("duration") and item["duration"] > 0), None)
        title = self._clean_title(page.title())
        parse_id = secrets.token_hex(16)
        media = next(iter(candidates.values())) if len(candidates) == 1 else None
        result = {
            "parse_id": parse_id,
            "title": title,
            "duration_seconds": duration,
            "media_available": bool(media),
            "media_candidate_count": len(candidates),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + MEDIA_TTL_SECONDS,
        }
        if media:
            result["media"] = media
        with self._state_lock:
            now = time.time()
            self._results = {key: item for key, item in self._results.items() if item["expires_at"] > now}
            self._results[parse_id] = result
            self._verified_login = True
            self._last_verified = time.monotonic()
            self._state = {
                "status": "parsed", "message": "回放解析完成" if media else "发现多个视频流，无法安全确定唯一下载源",
                "logged_in": True, "result": {key: value for key, value in result.items() if key not in {"expires_at", "media"}},
            }

    def _is_logged_in(self, page) -> bool:
        if self._looks_like_login_redirect(page.url, page.title()):
            return False
        for selector in (
            '[aria-label*="个人中心"]', '[aria-label*="个人信息"]', '[data-testid*="avatar"]',
            '[class*="user-avatar"]', '[class*="userAvatar"]', '[class*="user-info"]',
        ):
            try:
                if page.locator(selector).first.is_visible(timeout=150):
                    return True
            except Exception:
                continue
        try:
            text = page.locator("body").inner_text(timeout=1000)
            return any(marker in text for marker in ("退出登录", "个人中心", "账号设置"))
        except Exception:
            return False

    @staticmethod
    def _looks_like_login_redirect(url: str, title: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        login_path = bool(re.search(r"/(?:login|signin|passport)(?:/|$)", parsed.path, re.I))
        return parsed.hostname != "meeting.tencent.com" or login_path or "登录" in title

    @staticmethod
    def _click_login_entry(page) -> None:
        for selector in ('button:has-text("登录")', 'a:has-text("登录")', 'text=微信登录'):
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=300):
                    locator.click(timeout=1500)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    @staticmethod
    def _clean_title(value: str) -> str:
        title = re.sub(r"\s*[-|｜]\s*腾讯会议.*$", "", value or "", flags=re.I).strip()
        return title[:300] or "腾讯会议回放"

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        text = str(error)
        if isinstance(error, RuntimeError) and text:
            return text[:300]
        if "Timeout" in type(error).__name__ or "timeout" in text.lower():
            return "腾讯会议页面响应超时，请确认网络正常、回放可访问后重试。"
        return "腾讯会议浏览器启动或页面解析失败，请检查浏览器和网络后重试。"
