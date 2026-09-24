"""In-memory task and credential bridge for the CourseFlow browser extension."""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any


BRIDGE_VERSION = "0.1.0"
BRIDGE_EXTENSION_ID = "abmendmhafmkmcjkniplndbajmhnanej"
TASK_TTL_SECONDS = 30 * 60
# MV3 may suspend its worker between 30-second alarm wakeups.
CONNECTED_TTL_SECONDS = 45
ACTIVE_TASK_TTL_SECONDS = 90
MAX_TITLE_LENGTH = 200
MAX_HEADER_LENGTH = 4096
SAFE_MEDIA_HEADERS = {
    "accept", "accept-language", "authorization", "cookie", "origin",
    "referer", "user-agent", "range",
}
FAILURE_CODES = {
    "LOGIN_REQUIRED", "ACCESS_DENIED", "PAGE_TIMEOUT", "MEDIA_NOT_FOUND",
    "MULTIPLE_MEDIA", "PAGE_ERROR",
}
FAILURE_MESSAGES = {
    "LOGIN_REQUIRED": "当前浏览器中的腾讯会议尚未登录，请先在浏览器中正常登录腾讯会议后重试。",
    "ACCESS_DENIED": "当前账号无法访问该回放，或回放已不存在。",
    "PAGE_TIMEOUT": "等待回放或视频资源超时，请确认回放可播放后重试。",
    "MEDIA_NOT_FOUND": "已打开回放，但未发现 MP4 视频资源。",
    "MULTIPLE_MEDIA": "发现多个 MP4 视频资源，无法安全确定唯一资源。",
    "PAGE_ERROR": "回放页面解析失败，请稍后重试。",
}
TASK_STAGES = {"opening", "waiting_page", "waiting_media", "submitting"}
MEDIA_HOST_SUFFIXES = (
    ".tencent.com", ".qcloud.com", ".myqcloud.com", ".tencent-cloud.net",
    ".tencentcs.com",
)


def validate_recording_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("回放链接无效。")
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "meeting.tencent.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(("/cw/", "/crm/"))
    ):
        raise ValueError("请输入腾讯会议 https://meeting.tencent.com/cw/... 或 /crm/... 回放链接。")
    return urllib.parse.urlunsplit(("https", "meeting.tencent.com", parsed.path, parsed.query, ""))


def _is_tencent_media_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and any(host.endswith(suffix) and host != suffix[1:] for suffix in MEDIA_HOST_SUFFIXES)
        )
    except ValueError:
        return False


def _safe_media_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("媒体请求上下文无效。")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name).strip().lower()
        if name not in SAFE_MEDIA_HEADERS:
            continue
        if not isinstance(raw_value, str) or not raw_value or len(raw_value) > MAX_HEADER_LENGTH:
            continue
        if "\r" in raw_value or "\n" in raw_value:
            continue
        result[name] = raw_value
    return result


class MeetingBridgeService:
    """Keeps tasks, bridge credentials and captured media context in process memory only."""

    def __init__(self, extension_id: str, clock=time.time):
        self.extension_origins = {
            f"chrome-extension://{extension_id}",
            f"edge-extension://{extension_id}",
        }
        self._clock = clock
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._last_seen = 0.0
        self._browser = ""
        self._version = ""
        self._tasks: dict[str, dict[str, Any]] = {}
        self._active_task_id = ""
        self._stop_cleanup = threading.Event()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, name="meeting-bridge-cleanup", daemon=True)
        self._cleanup_thread.start()

    def origin_allowed(self, origin: str | None) -> bool:
        return isinstance(origin, str) and any(secrets.compare_digest(origin, allowed) for allowed in self.extension_origins)

    def pair(self, origin: str | None) -> dict[str, str]:
        if not self.origin_allowed(origin):
            raise PermissionError("浏览器扩展来源未获授权。")
        return {"token": self._token, "version": BRIDGE_VERSION}

    def authorized(self, origin: str | None, token: str | None) -> bool:
        return (
            self.origin_allowed(origin)
            and isinstance(token, str)
            and len(token) <= 128
            and secrets.compare_digest(token, self._token)
        )

    def heartbeat(self, origin: str | None, token: str | None, payload: Any) -> dict[str, Any]:
        if not self.authorized(origin, token):
            raise PermissionError("浏览器桥接鉴权失败。")
        browser = payload.get("browser") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or not isinstance(browser, str) or browser not in {"chrome", "edge"}:
            raise ValueError("浏览器桥接状态无效。")
        version = payload.get("version")
        if not isinstance(version, str) or not version or len(version) > 32:
            raise ValueError("浏览器桥接版本无效。")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            self._last_seen = now
            self._browser = payload["browser"]
            self._version = version
            if self._active_task_id:
                task = self._tasks.get(self._active_task_id)
                if task and task["status"] == "processing":
                    task["lease_expires_at"] = now + ACTIVE_TASK_TTL_SECONDS
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            return {
                "connected": bool(self._last_seen and now - self._last_seen <= CONNECTED_TTL_SECONDS),
                "last_seen": datetime.fromtimestamp(self._last_seen, timezone.utc).isoformat() if self._last_seen else None,
                "version": self._version or BRIDGE_VERSION,
                "browser": self._browser or None,
            }

    def create_task(self, value: str) -> dict[str, Any]:
        recording_url = validate_recording_url(value)
        now = self._clock()
        task_id = secrets.token_hex(12)
        with self._lock:
            self._purge_locked(now)
            task = {
                "task_id": task_id,
                "url": recording_url,
                "status": "pending",
                "stage": "claiming",
                "created_at": now,
                "expires_at": now + TASK_TTL_SECONDS,
                "lease_expires_at": 0.0,
                "result": None,
                "error": None,
            }
            self._tasks[task_id] = task
            return self._public_task(task)

    def claim_next(self, origin: str | None, token: str | None) -> dict[str, str] | None:
        if not self.authorized(origin, token):
            raise PermissionError("浏览器桥接鉴权失败。")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            if self._active_task_id:
                return None
            task = next((item for item in self._tasks.values() if item["status"] == "pending"), None)
            if not task:
                return None
            task["status"] = "processing"
            task["stage"] = "opening"
            task["lease_expires_at"] = now + ACTIVE_TASK_TTL_SECONDS
            self._active_task_id = task["task_id"]
            return {"task_id": task["task_id"], "url": task["url"]}

    def update_progress(self, task_id: str, origin: str | None, token: str | None, stage: Any) -> dict[str, Any]:
        if not self.authorized(origin, token):
            raise PermissionError("浏览器桥接鉴权失败。")
        if not isinstance(stage, str) or stage not in TASK_STAGES:
            raise ValueError("解析阶段无效。")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError("解析任务已过期或不存在。")
            if task["status"] != "processing" or self._active_task_id != task_id:
                raise ValueError("解析任务当前不可更新状态。")
            task["stage"] = stage
            task["lease_expires_at"] = now + ACTIVE_TASK_TTL_SECONDS
            return self._public_task(task)

    def finish(self, task_id: str, origin: str | None, token: str | None, payload: Any) -> dict[str, Any]:
        if not self.authorized(origin, token):
            raise PermissionError("浏览器桥接鉴权失败。")
        if not isinstance(payload, dict):
            raise ValueError("解析结果无效。")
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError("解析任务已过期或不存在。")
            if task["status"] != "processing" or self._active_task_id != task_id:
                raise ValueError("解析任务当前不可提交结果。")
            if payload.get("status") == "success":
                media = payload.get("media")
                if not isinstance(media, dict) or not _is_tencent_media_url(media.get("url")):
                    raise ValueError("没有有效的腾讯 MP4 媒体资源。")
                headers = _safe_media_headers(media.get("headers", {}))
                title = payload.get("title")
                title = " ".join(title.split())[:MAX_TITLE_LENGTH] if isinstance(title, str) else ""
                duration = payload.get("duration_seconds")
                if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0 or duration > 7 * 24 * 3600:
                    duration = None
                task["result"] = {
                    "title": title or "腾讯会议回放",
                    "duration_seconds": int(duration) if duration else None,
                    "media": {"url": media["url"], "headers": headers},
                    "completed_at": now,
                }
                task["status"] = "success"
                task["stage"] = "done"
                task["expires_at"] = now + TASK_TTL_SECONDS
                task["error"] = None
            elif payload.get("status") == "failed":
                code = payload.get("code")
                if not isinstance(code, str) or code not in FAILURE_CODES:
                    code = "PAGE_ERROR"
                task["status"] = "failed"
                task["stage"] = "failed"
                task["error"] = {"code": code, "message": FAILURE_MESSAGES[code]}
                task["expires_at"] = now + TASK_TTL_SECONDS
            else:
                raise ValueError("解析结果状态无效。")
            task["lease_expires_at"] = 0.0
            self._active_task_id = ""
            return self._public_task(task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError("解析任务已过期或不存在。")
            return self._public_task(task)

    def shutdown(self) -> None:
        self._stop_cleanup.set()
        with self._lock:
            self._tasks.clear()
            self._active_task_id = ""
            self._last_seen = 0.0

    def _cleanup_loop(self) -> None:
        while not self._stop_cleanup.wait(30):
            with self._lock:
                self._purge_locked(self._clock())

    def _purge_locked(self, now: float) -> None:
        for task_id, task in list(self._tasks.items()):
            if task["status"] == "processing" and task["lease_expires_at"] <= now:
                task["status"] = "failed"
                task["stage"] = "failed"
                task["error"] = {"code": "PAGE_TIMEOUT", "message": "浏览器桥接已中断，请重新解析。"}
                task["result"] = None
                task["expires_at"] = now + TASK_TTL_SECONDS
                task["lease_expires_at"] = 0.0
                if self._active_task_id == task_id:
                    self._active_task_id = ""
            elif task["expires_at"] <= now:
                self._tasks.pop(task_id, None)
                if self._active_task_id == task_id:
                    self._active_task_id = ""

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        result = task.get("result") or {}
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "stage": task.get("stage"),
            "title": result.get("title"),
            "duration_seconds": result.get("duration_seconds"),
            "media_found": bool(result.get("media")),
            "error": task.get("error"),
        }
