"""Bilibili Downloader 的本地网页入口；下载核心由 BBDownNext 提供。"""

from __future__ import annotations

import argparse
import base64
import cgi
import ctypes
import http.client
from ctypes import wintypes
import html
import json
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from build_info import BUILD_FILES, current_build_id as shared_current_build_id
from meeting_browser import MeetingBrowserService

APP_NAME = "BilibiliDownloader"
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TOOLS_DIR = ROOT / "tools"
BBDOWN = TOOLS_DIR / "BBDownNext" / "BBDown.exe"
DEFAULT_APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
APP_DATA = Path(os.environ.get("BILIBILI_DOWNLOADER_DATA_DIR", DEFAULT_APP_DATA))
CONFIG_FILE = APP_DATA / "config.json"
AUTH_FILE = APP_DATA / "bilibili-auth.dat"
MIMO_AUTH_FILE = APP_DATA / "mimo-auth.dat"
QR_PACKAGE_DIR = APP_DATA / "python-packages"
LOG_DIR = APP_DATA / "logs"
APP_PORT = 23666
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
TRANSCRIPTION_PROVIDERS = {
    "local": {"label": "本地 FunASR", "model": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"},
    "mimo-v2.5-asr": {"label": "小米 MiMo-V2.5-ASR", "model": "mimo-v2.5-asr"},
    "mimo-v2.6-flash": {"label": "小米 MiMo-V2.6-Flash", "model": "mimo-v2.6-flash"},
}
ALLOWED_COVER_SUFFIX = ".hdslb.com"
BILIBILI_SPACE_API = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
BILIBILI_ARC_SEARCH_API = "https://api.bilibili.com/x/space/wbi/arc/search"
BILIBILI_LEGACY_ARC_SEARCH_API = "https://api.bilibili.com/x/space/arc/search"
BILIBILI_SEASONS_API = "https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
BILIBILI_SEASON_ARCHIVES_API = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
BILIBILI_SERIES_ARCHIVES_API = "https://api.bilibili.com/x/series/archives"
BILIBILI_PROFILE_API = "https://api.bilibili.com/x/web-interface/card"
BILIBILI_SPACE_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,"
    "decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,avatarAutoTheme,"
    "cardsEnhance,eva3CardOpus,eva3CardVideo,eva3CardComment,eva3CardUser"
)
WEB_LOCALE = json.dumps({"c_locale": {"language": "zh", "script": "Hans"}, "always_translate": False}, separators=(",", ":"))
WEB_DEVICE = json.dumps({"platform": "web", "device": "pc", "spmid": "333.1387", "mobi_app": "web_cn"}, separators=(",", ":"))
WBI_MIXIN_KEY_TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
WBI_MIXIN_KEY = ""
UP_FILTER_CACHE: dict[tuple[str, str, str], list[dict]] = {}


def redact(value: object) -> str:
    """避免诊断日志意外记录 Cookie、token 或带敏感查询参数的链接。"""
    text = str(value)
    text = re.sub(r"(?i)(cookie|token|sessdata|access_token|refresh_token|qrcode_key|api[-_]?key|authorization|mimo_api_key)([=:])[^\s,&;]+", r"\1\2<已脱敏>", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    return logger


LOG = logging.getLogger(APP_NAME)
MEETING_BROWSER = MeetingBrowserService(lambda: APP_DATA)


def current_build_id() -> str:
    return shared_current_build_id(ROOT)


BUILD_ID = current_build_id()
RESTART_LOCK = threading.Lock()
RESTART_SCHEDULED = False


def disk_build_id() -> str:
    try:
        return current_build_id()
    except OSError:
        LOG.exception("计算磁盘 build_id 失败")
        return ""


def configure_data_dir(data_dir: str | None) -> None:
    """测试可指定隔离目录，正常启动仍只使用当前用户的 AppData。"""
    global APP_DATA, CONFIG_FILE, AUTH_FILE, MIMO_AUTH_FILE, QR_PACKAGE_DIR, LOG_DIR, MIMO_API_KEY_CACHE
    if data_dir:
        APP_DATA = Path(data_dir).expanduser().resolve()
        CONFIG_FILE = APP_DATA / "config.json"
        AUTH_FILE = APP_DATA / "bilibili-auth.dat"
        MIMO_AUTH_FILE = APP_DATA / "mimo-auth.dat"
        QR_PACKAGE_DIR = APP_DATA / "python-packages"
        LOG_DIR = APP_DATA / "logs"
        MIMO_API_KEY_CACHE = None
        if "BILIBILI_SESSION" in globals():
            BILIBILI_SESSION.reset()


def read_config() -> dict:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        directory = str(config.get("download_dir", "")).strip()
        # 目录已移动或是旧测试临时目录时，视为首次使用，不删除任何用户文件。
        if directory and not Path(directory).is_dir():
            config["download_dir"] = ""
        return config
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"download_dir": ""}


def save_config(config: dict) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(plain: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("敏感凭据只能在 Windows 本机安全保存。")
    source = ctypes.create_string_buffer(plain)
    input_blob = _DataBlob(len(plain), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    crypt = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(input_blob), "Bilibili Downloader login", None, None, None, 0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


MIMO_API_KEY_CACHE: str | None = None


def load_mimo_api_key() -> str:
    global MIMO_API_KEY_CACHE
    if MIMO_API_KEY_CACHE is not None:
        return MIMO_API_KEY_CACHE
    try:
        encoded = MIMO_AUTH_FILE.read_text(encoding="ascii")
        plain = _dpapi(base64.b64decode(encoded), False).decode("utf-8")
        payload = json.loads(plain)
        key = str(payload.get("api_key") or "") if isinstance(payload, dict) else ""
    except (FileNotFoundError, OSError, ValueError, UnicodeError, ctypes.ArgumentError, json.JSONDecodeError):
        key = ""
    MIMO_API_KEY_CACHE = key
    return key


def save_mimo_api_key(value: str) -> None:
    global MIMO_API_KEY_CACHE
    APP_DATA.mkdir(parents=True, exist_ok=True)
    plain = json.dumps({"version": 1, "api_key": value}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encrypted = _dpapi(plain, True)
    temporary = MIMO_AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(base64.b64encode(encrypted).decode("ascii"), encoding="ascii")
    temporary.replace(MIMO_AUTH_FILE)
    MIMO_API_KEY_CACHE = value


def mimo_key_hint(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if len(value) >= 9 else "已配置"


DEVICE_COOKIE_NAMES = {"buvid3", "buvid4", "buvid_fp", "b_nut", "b_lsid", "_uuid", "buvid_fp_plain"}


class BilibiliSession:
    """将账号和设备 Cookie 放在同一个加密会话中，且只允许记录名称。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.loaded = False
        self.account: dict[str, str] = {}
        self.device: dict[str, str] = {}

    @staticmethod
    def _parse_cookie(value: str) -> dict[str, str]:
        cookie = SimpleCookie()
        try:
            cookie.load(value)
        except (TypeError, ValueError):
            return {}
        return {name: morsel.value for name, morsel in cookie.items() if morsel.value}

    @staticmethod
    def _header(values: dict[str, str]) -> str:
        return "; ".join(f"{name}={value}" for name, value in values.items())

    def _load_locked(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            encoded = AUTH_FILE.read_text(encoding="ascii")
            plain = _dpapi(base64.b64decode(encoded), False).decode("utf-8")
            decoded = json.loads(plain)
            if isinstance(decoded, dict) and decoded.get("version") == 2:
                self.account = {str(k): str(v) for k, v in (decoded.get("account") or {}).items() if v}
                self.device = {str(k): str(v) for k, v in (decoded.get("device") or {}).items() if v}
                return
            raise ValueError("legacy cookie")
        except (FileNotFoundError, OSError, ValueError, UnicodeError, ctypes.ArgumentError, json.JSONDecodeError):
            # 兼容旧版只保存账号 Cookie 的 DPAPI 文件；下次保存会自动升级为统一会话格式。
            try:
                encoded = AUTH_FILE.read_text(encoding="ascii")
                legacy = _dpapi(base64.b64decode(encoded), False).decode("utf-8")
                self.account = self._parse_cookie(legacy)
            except (FileNotFoundError, OSError, ValueError, UnicodeError, ctypes.ArgumentError):
                self.account = {}

    def _save_locked(self) -> None:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        plain = json.dumps({"version": 2, "account": self.account, "device": self.device}, separators=(",", ":"), ensure_ascii=False)
        encrypted = _dpapi(plain.encode("utf-8"), True)
        temporary = AUTH_FILE.with_suffix(".tmp")
        temporary.write_text(base64.b64encode(encrypted).decode("ascii"), encoding="ascii")
        temporary.replace(AUTH_FILE)

    def reset(self) -> None:
        with self.lock:
            self.loaded = False
            self.account = {}
            self.device = {}

    def cookie_header(self, *, account: bool = True, device: bool = True) -> str:
        with self.lock:
            self._load_locked()
            values: dict[str, str] = {}
            if device:
                values.update(self.device)
            if account:
                values.update(self.account)
            return self._header(values)

    def cookie_names(self) -> tuple[list[str], list[str]]:
        with self.lock:
            self._load_locked()
            return sorted(self.account), sorted(self.device)

    def log_cookie_names(self, context: str) -> None:
        account, device = self.cookie_names()
        LOG.info("B 站会话 %s：account cookie names=%s device cookie names=%s", context, account, device)

    def merge(self, value: str, *, account: bool = False, device: bool = False) -> None:
        values = self._parse_cookie(value)
        if not values:
            return
        with self.lock:
            self._load_locked()
            for name, cookie_value in values.items():
                target = self.device if name.lower() in DEVICE_COOKIE_NAMES or device else self.account
                if account and name.lower() not in DEVICE_COOKIE_NAMES:
                    target = self.account
                target[name] = cookie_value
            self._save_locked()

    def merge_set_cookies(self, headers: list[str]) -> None:
        for header in headers:
            self.merge(header, account=True)

    def merge_device_values(self, values: dict[str, str]) -> None:
        with self.lock:
            self._load_locked()
            for name, value in values.items():
                if value:
                    self.device[name] = value
            self._save_locked()

    def clear_account(self) -> None:
        with self.lock:
            self._load_locked()
            self.account = {}
            # 退出账号不丢弃本机设备标识，避免下一次登录又从空设备会话开始。
            self._save_locked()


BILIBILI_SESSION = BilibiliSession()


def save_auth_cookie(cookie: str) -> None:
    BILIBILI_SESSION.merge(cookie, account=True)


def load_auth_cookie() -> str:
    return BILIBILI_SESSION.cookie_header(account=True, device=False)


def clear_auth_cookie() -> None:
    BILIBILI_SESSION.clear_account()


def run_hidden(command: list[str], **kwargs) -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, creationflags=flags, **kwargs)


def select_folder() -> str:
    """使用 Python 标准库目录选择器，不启动 PowerShell。"""
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows 文件夹选择器。")
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        raise RuntimeError("当前 Python 未包含 Windows 文件夹选择组件，请重新安装 Python。") from error

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        return filedialog.askdirectory(
            parent=root,
            title="选择默认下载目录",
            mustexist=False,
        )
    except tk.TclError as error:
        raise RuntimeError("无法打开 Windows 文件夹选择窗口，请重启工具后重试。") from error
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass


def allowed_cover_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and (host == "hdslb.com" or host.endswith(ALLOWED_COVER_SUFFIX))


class CoverRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not allowed_cover_url(newurl):
            raise ValueError("封面地址跳转到了不受支持的域名。")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def get_cover(value: str) -> tuple[bytes, str]:
    if not allowed_cover_url(value):
        raise ValueError("封面地址不是受支持的 B 站图片地址。")
    cache_dir = APP_DATA / "cover-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(value.encode("utf-8")).hexdigest()
    cache_file = cache_dir / f"{key}.bin"
    meta_file = cache_dir / f"{key}.json"
    if cache_file.exists() and meta_file.exists():
        try:
            return cache_file.read_bytes(), json.loads(meta_file.read_text(encoding="utf-8"))["content_type"]
        except (OSError, KeyError, json.JSONDecodeError):
            cache_file.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), CoverRedirectHandler())
    request = urllib.request.Request(value, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    with opener.open(request, timeout=15) as response:
        if not allowed_cover_url(response.geturl()):
            raise ValueError("封面地址跳转到了不受支持的域名。")
        content_type = response.headers.get_content_type()
        if not content_type.startswith("image/"):
            raise ValueError("B 站返回的封面不是图片。")
        body = response.read(5 * 1024 * 1024 + 1)
    if len(body) > 5 * 1024 * 1024:
        raise ValueError("封面文件过大。")
    temporary = cache_file.with_suffix(".tmp")
    temporary.write_bytes(body)
    temporary.replace(cache_file)
    meta_file.write_text(json.dumps({"content_type": content_type}), encoding="utf-8")
    return body, content_type


class BBDownService:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.token = ""
        self.download_dir = ""
        self.port = 0
        self.lock = threading.Lock()
        self.respect_config = True

    @staticmethod
    def pick_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def ffmpeg_available() -> bool:
        candidates = [
            TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
            TOOLS_DIR / "ffmpeg" / "ffmpeg.exe",
        ]
        return any(path.exists() for path in candidates) or shutil.which("ffmpeg") is not None

    def dependency_problem(self) -> str:
        if not BBDOWN.exists():
            return "缺少 BBDownNext：请重新双击“启动工具.vbs”自动准备依赖。"
        if not self.ffmpeg_available():
            return "缺少 FFmpeg：请重新双击“启动工具.vbs”自动准备依赖。"
        return ""

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        extra_paths = [TOOLS_DIR / "ffmpeg" / "bin", TOOLS_DIR / "ffmpeg", BBDOWN.parent]
        env["PATH"] = os.pathsep.join([str(path) for path in extra_paths] + [env.get("PATH", "")])
        return env

    def _consume_output(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            LOG.info("BBDown: %s", line.rstrip())

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def ensure_started(self, download_dir: str, respect_config: bool = True) -> tuple[bool, str]:
        problem = self.dependency_problem()
        if problem:
            return False, problem
        # 配置切换和前端轮询可能并发到达；以磁盘中的最新配置为准，避免旧轮询把服务切回旧目录。
        if respect_config:
            configured_dir = read_config().get("download_dir", "")
            if configured_dir:
                download_dir = configured_dir
        with self.lock:
            if self.process and self.process.poll() is None and self.download_dir == download_dir:
                return True, ""
            self.stop()
            self.token = secrets.token_urlsafe(32)
            self.port = self.pick_port()
            command = [
                str(BBDOWN), "serve", "--listen", f"http://127.0.0.1:{self.port}",
                "--serve-token", self.token, "--work-dir", download_dir, "--max-concurrent", "1",
            ]
            LOG.info("启动 BBDown 服务，下载目录：%s", download_dir)
            self.process = run_hidden(
                command,
                cwd=ROOT,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="gbk",
                errors="replace",
            )
            self.download_dir = download_dir
            threading.Thread(target=self._consume_output, args=(self.process,), daemon=True).start()
            for _ in range(25):
                if self.process.poll() is not None:
                    return False, "BBDownNext 未能启动，请在“设置”中导出诊断日志查看原因。"
                try:
                    with LOCAL_OPENER.open(f"http://127.0.0.1:{self.port}/healthz", timeout=1):
                        return True, ""
                except urllib.error.URLError:
                    time.sleep(0.2)
            return False, "BBDownNext 启动超时，请检查防火墙或导出诊断日志。"

    def request(self, path: str, method: str = "GET", data: dict | None = None) -> tuple[int, object]:
        if not self.download_dir:
            raise RuntimeError("请先选择下载目录。")
        ok, message = self.ensure_started(self.download_dir, respect_config=self.respect_config)
        if not ok:
            raise RuntimeError(message)
        payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", payload, method=method,
            headers={"Content-Type": "application/json", "X-BBDown-Token": self.token},
        )
        try:
            with LOCAL_OPENER.open(request, timeout=15) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(body)
            except json.JSONDecodeError:
                return error.code, {"error": redact(body)}


SERVICE = BBDownService()


def transcription_provider_settings() -> tuple[str, dict]:
    provider = str(read_config().get("transcription_provider") or "local")
    if provider not in TRANSCRIPTION_PROVIDERS:
        provider = "local"
    return provider, TRANSCRIPTION_PROVIDERS[provider]


def public_transcription_settings() -> dict:
    provider, details = transcription_provider_settings()
    key = load_mimo_api_key()
    return {
        "provider": provider,
        "model": details["model"],
        "label": details["label"],
        "mimo_api_key_configured": bool(key),
        "mimo_api_key_hint": mimo_key_hint(key) if key else "",
    }


def mimo_request(
    path: str,
    api_key: str,
    payload: dict | None = None,
    method: str = "POST",
    *,
    task_id: str = "",
    provider: str = "",
    model: str = "",
    chunk_index: int = 0,
    chunk_total: int = 0,
    chunk_bytes: int = 0,
    cancellation_check=None,
    cancel_event: threading.Event | None = None,
    request_lock: threading.Lock | None = None,
    attempt_callback=None,
) -> tuple[int, object]:
    if not api_key:
        raise RuntimeError("尚未配置 MiMo API Key。")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
    headers = {"api-key": api_key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    retryable = {429, 500, 502, 503, 504}
    last_error = "MiMo API 请求失败。"
    for attempt in range(4):
        if cancellation_check:
            cancellation_check()
        if attempt_callback:
            attempt_callback(attempt + 1)
        if request_lock:
            request_lock.acquire()
        try:
            if cancellation_check:
                cancellation_check()
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            started_clock = time.monotonic()
            request = urllib.request.Request(f"{MIMO_BASE_URL}{path}", body, method=method, headers=headers)
        finally:
            if request_lock:
                request_lock.release()
        try:
            with LOCAL_OPENER.open(request, timeout=120) as response:
                raw = response.read()
                try:
                    result = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    result = {}
                metadata = mimo_response_metadata(result)
                if task_id:
                    LOG.info(
                        "MiMo request task_id=%s provider=%s model=%s chunk=%s/%s bytes=%s attempt=%s started_at=%s duration_s=%.3f status=%s will_retry=false retry_reason=none finish_reason=%s usage=%s content_chars=%s",
                        task_id, provider, model, chunk_index, chunk_total, chunk_bytes, attempt + 1, started_at,
                        time.monotonic() - started_clock, response.status, metadata["finish_reason"], metadata["usage"], metadata["content_chars"],
                    )
                return response.status, result
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                result = {}
            last_error = asr_error_message(result, f"MiMo API 请求失败（HTTP {error.code}）。")
            will_retry = error.code in retryable and attempt < 3
            retry_reason = "429" if error.code == 429 else "5xx" if 500 <= error.code <= 599 else "none"
            metadata = mimo_response_metadata(result)
            if task_id:
                LOG.warning(
                    "MiMo request task_id=%s provider=%s model=%s chunk=%s/%s bytes=%s attempt=%s started_at=%s duration_s=%.3f status=%s will_retry=%s retry_reason=%s finish_reason=%s usage=%s content_chars=%s",
                    task_id, provider, model, chunk_index, chunk_total, chunk_bytes, attempt + 1, started_at,
                    time.monotonic() - started_clock, error.code, str(will_retry).lower(), retry_reason,
                    metadata["finish_reason"], metadata["usage"], metadata["content_chars"],
                )
            if not will_retry:
                raise RuntimeError(last_error)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as error:
            last_error = "MiMo API 网络请求失败，请检查网络连接。"
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            timed_out = isinstance(reason, (TimeoutError, socket.timeout))
            will_retry = attempt < 3
            retry_reason = "timeout" if timed_out else "network"
            if task_id:
                LOG.warning(
                    "MiMo request task_id=%s provider=%s model=%s chunk=%s/%s bytes=%s attempt=%s started_at=%s duration_s=%.3f status=none will_retry=%s retry_reason=%s finish_reason=none usage={} content_chars=0",
                    task_id, provider, model, chunk_index, chunk_total, chunk_bytes, attempt + 1, started_at,
                    time.monotonic() - started_clock, str(will_retry).lower(), retry_reason,
                )
            if attempt == 3:
                raise RuntimeError(last_error) from error
        delay = 0.8 * (2 ** attempt)
        if cancel_event:
            if cancel_event.wait(delay) and cancellation_check:
                cancellation_check()
        else:
            time.sleep(delay)
    raise RuntimeError(last_error)


def test_mimo_api_key(api_key: str, provider: str) -> None:
    if provider not in {"mimo-v2.5-asr", "mimo-v2.6-flash"}:
        return
    status, payload = mimo_request("/models", api_key, method="GET")
    if status < 200 or status >= 300:
        raise RuntimeError(asr_error_message(payload, f"MiMo API 测试失败（HTTP {status}）。"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        ids = {str(item.get("id")) for item in payload["data"] if isinstance(item, dict)}
        model = TRANSCRIPTION_PROVIDERS[provider]["model"]
        if ids and model not in ids:
            raise RuntimeError(f"当前 MiMo API Key 未返回模型 {model}。")


def ffmpeg_executable() -> Path | None:
    candidates = [TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe", TOOLS_DIR / "ffmpeg" / "ffmpeg.exe"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def mimo_audio_chunks(audio_path: Path, temp_dir: Path, provider: str) -> list[Path]:
    executable = ffmpeg_executable()
    if executable is None:
        raise RuntimeError("缺少 FFmpeg，无法准备 MiMo 音频。")
    temp_dir.mkdir(parents=True, exist_ok=True)
    max_encoded_bytes = 9 * 1024 * 1024 if provider == "mimo-v2.5-asr" else 48 * 1024 * 1024
    segment_seconds = 300 if provider == "mimo-v2.5-asr" else 600
    for _ in range(5):
        for old in temp_dir.glob("chunk-*.mp3"):
            old.unlink(missing_ok=True)
        output_pattern = str(temp_dir / "chunk-%05d.mp3")
        command = [
            str(executable), "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
            "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1", output_pattern,
        ]
        result = subprocess.run(command, cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800, check=False)
        if result.returncode != 0:
            raise RuntimeError("FFmpeg 无法转换 MiMo 音频。")
        chunks = sorted(temp_dir.glob("chunk-*.mp3"))
        if chunks and all(((path.stat().st_size + 2) // 3) * 4 + 64 < max_encoded_bytes for path in chunks):
            return chunks
        segment_seconds //= 2
    raise RuntimeError("音频切片后仍超过 MiMo 单次请求限制。")


def mimo_response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def _response_text_length(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(item.get("text", "")) for item in value if isinstance(item, dict) and isinstance(item.get("text"), str))
    return 0


def mimo_response_metadata(payload: object) -> dict[str, object]:
    choices = payload.get("choices") if isinstance(payload, dict) else []
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    usage_values = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "audio_tokens": prompt_details.get("audio_tokens", usage.get("audio_tokens")),
        "audio_seconds": prompt_details.get("audio_seconds", usage.get("audio_seconds")),
        "reasoning_tokens": completion_details.get("reasoning_tokens", usage.get("reasoning_tokens")),
    }
    return {
        "finish_reason": choice.get("finish_reason"),
        "content_chars": len(mimo_response_text(payload)),
        "reasoning_content_chars": _response_text_length(message.get("reasoning_content")),
        "usage": {key: value for key, value in usage_values.items() if value is not None},
    }


def mimo_transcribe_audio(audio_path: Path, temp_dir: Path, provider: str, model: str, progress_callback=None, task_id: str = "") -> dict:
    api_key = load_mimo_api_key()
    if not api_key:
        raise RuntimeError("当前已选择 MiMo，但尚未配置 API Key。请到“设置”中保存并测试。")
    if progress_callback:
        progress_callback(10, "正在准备音频")
    chunks = mimo_audio_chunks(audio_path, temp_dir, provider)
    if not chunks:
        raise RuntimeError("音频准备失败：没有生成可转写的音频片段。")
    if progress_callback:
        progress_callback(15, f"音频准备完成，共 {len(chunks)} 段")
    texts = []
    for index, chunk in enumerate(chunks, 1):
        check_transcription_cancelled(task_id) if task_id else None
        if progress_callback:
            progress_callback(20 + round(70 * (index - 1) / len(chunks)), f"正在转写第 {index}/{len(chunks)} 段")
        encoded = base64.b64encode(chunk.read_bytes()).decode("ascii")
        mime = mimetypes.guess_type(chunk.name)[0] or "audio/mpeg"
        audio_data = f"data:{mime};base64,{encoded}"
        if provider == "mimo-v2.5-asr":
            messages = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": audio_data}}]}]
            payload = {"model": model, "messages": messages, "asr_options": {"language": "zh"}}
        else:
            messages = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": audio_data}}, {"type": "text", "text": "忠实转写音频原话，不总结、不润色、不补充内容；不要输出时间戳、speaker 或发言人标签；按自然语义和话题停顿整理成正常中文段落。"}]}]
            payload = {
                "model": model,
                "messages": messages,
                "thinking": {"type": "disabled"},
                "max_completion_tokens": 65536,
            }
        try:
            def report_attempt(attempt: int) -> None:
                if task_id:
                    check_transcription_cancelled(task_id)
                if progress_callback:
                    progress_callback(20 + round(70 * (index - 1) / len(chunks)), f"正在转写第 {index}/{len(chunks)} 段")
                    update_transcription(task_id, status="processing", request_attempt=attempt)

            status, response = mimo_request(
                "/chat/completions", api_key, payload,
                task_id=task_id, provider=provider, model=model, chunk_index=index, chunk_total=len(chunks),
                chunk_bytes=chunk.stat().st_size,
                cancellation_check=(lambda: check_transcription_cancelled(task_id)) if task_id else None,
                cancel_event=TRANSCRIPTION_CANCEL_EVENTS.get(task_id) if task_id else None,
                request_lock=TRANSCRIPTION_REQUEST_LOCKS.get(task_id) if task_id else None,
                attempt_callback=report_attempt,
            )
            check_transcription_cancelled(task_id) if task_id else None
            metadata = mimo_response_metadata(response)
            if status < 200 or status >= 300:
                raise RuntimeError(asr_error_message(response, f"MiMo 第 {index}/{len(chunks)} 个音频片段请求失败。"))
            finish_reason = metadata["finish_reason"]
            if finish_reason in {"length", "max_tokens"}:
                raise RuntimeError(f"MiMo 第 {index}/{len(chunks)} 个音频片段输出达到 token 上限（finish_reason={finish_reason}）。")
            if finish_reason not in {None, "stop"}:
                raise RuntimeError(f"MiMo 第 {index}/{len(chunks)} 个音频片段返回异常（finish_reason={finish_reason}）。")
            text = mimo_response_text(response)
            if not text:
                LOG.warning(
                    "MiMo empty content provider=%s model=%s chunk=%s/%s bytes=%s status=%s finish_reason=%s usage=%s content_chars=%s reasoning_content_chars=%s",
                    provider,
                    model,
                    index,
                    len(chunks),
                    chunk.stat().st_size,
                    status,
                    metadata["finish_reason"],
                    metadata["usage"],
                    metadata["content_chars"],
                    metadata["reasoning_content_chars"],
                )
                raise RuntimeError("MiMo 返回空文字稿。")
            texts.append(text)
        except TranscriptionCancelled:
            raise
        except Exception as error:
            raise RuntimeError(f"MiMo 第 {index}/{len(chunks)} 个音频片段失败：{short_error(error)}") from error
        if progress_callback:
            progress_callback(20 + round(70 * index / len(chunks)), f"已完成第 {index}/{len(chunks)} 段")
    if progress_callback:
        progress_callback(95, "正在整理文字稿")
    return {
        "api_version": "mimo-openai-compatible",
        "task_id": "",
        "status": "succeeded",
        "progress": 100,
        "filename": audio_path.name,
        "text": "\n".join(texts),
        "segments": [],
        "timestamps": [],
        "model": {"provider": "mimo", "model_id": model, "timestamps_supported": False},
        "metrics": {"chunk_count": len(chunks)},
        "error": None,
    }


TRANSCRIPTION_LOCK = threading.RLock()
TRANSCRIPTION_TASKS: dict[str, dict] = {}
TRANSCRIPTION_ACTIVE_STATUSES = {"queued", "downloading", "uploading", "processing"}
TRANSCRIPTION_CANCEL_EVENTS: dict[str, threading.Event] = {}
TRANSCRIPTION_REQUEST_LOCKS: dict[str, threading.Lock] = {}
TRANSCRIPTION_SERVICES: dict[str, BBDownService] = {}
MIMO_QUEUE_CONDITION = threading.Condition()
MIMO_QUEUE: list[str] = []
MIMO_ACTIVE_TASK_ID = ""
TRANSCRIPTION_HISTORY_FIELDS = (
    "task_id", "name", "source_url", "source_title", "status", "stage", "progress", "error",
    "result", "filename", "created_at", "finished_at", "parent_id", "provider", "model",
    "cancel_requested", "stage_started_at", "request_attempt", "asr_task_id", "download_task_id", "asr_cancel_sent", "queue_ahead",
)
ASR_SERVICE_HOST = "127.0.0.1"
ASR_SERVICE_PORT = 8765
TRANSCRIPTION_AUDIO_SUFFIXES = {".m4a", ".mka", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus"}


def transcription_root() -> Path:
    root = APP_DATA / "transcription-temp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def transcription_history_dir() -> Path:
    return APP_DATA / "transcription-history"


TRANSCRIPTION_META_PREFIX = re.compile(
    r"^\s*(?:\d{1,3}[.,]\d{1,3}|\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    r"\s*[-–—]\s*(?:\d{1,3}[.,]\d{1,3}|\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    r"\s*\|\s*(?:SPEAKER_\d{1,3}|(?:发言人|主讲人|说话人)(?:\s*\d{1,3})?)\s*[:：]\s*",
    re.IGNORECASE,
)


def normalize_transcript_text(value: object) -> str:
    """只清理明确的逐行转写元数据，并用原字词做确定性自然分段。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    cleaned_lines = [TRANSCRIPTION_META_PREFIX.sub("", line, count=1).strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    paragraphs: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        if not run:
            return
        joined = ""
        for line in run:
            if joined and joined[-1:].isascii() and joined[-1:].isalnum() and line[:1].isascii() and line[:1].isalnum():
                joined += " "
            joined += line
        sentences = re.split(r"(?<=[。！？!?；;])|(?<!\d)(?<=\.)(?=\s|$)", joined)
        current = ""
        for sentence in sentences:
            if not sentence:
                continue
            units: list[str] = []
            while len(sentence) > 280:
                boundary = max((match.end() for match in re.finditer(r"[，,、；;]", sentence[:280])), default=0)
                boundary = boundary if boundary >= 140 else 280
                units.append(sentence[:boundary])
                sentence = sentence[boundary:]
            if sentence:
                units.append(sentence)
            for unit in units:
                if current and len(current) >= 180:
                    paragraphs.append(current.strip())
                    current = ""
                current += unit
        if current.strip():
            paragraphs.append(current.strip())
        run.clear()

    for line in cleaned_lines:
        if line:
            run.append(line)
        else:
            flush_run()
    flush_run()
    return "\n\n".join(paragraphs)


def persist_transcription(task: dict) -> None:
    task_id = str(task.get("task_id") or "")
    if not re.fullmatch(r"[a-f0-9]{24}", task_id):
        return
    history_dir = transcription_history_dir()
    path = history_dir / f"{task_id}.json"
    temp_path = history_dir / f"{task_id}.json.tmp"
    record = {key: task.get(key) for key in TRANSCRIPTION_HISTORY_FIELDS}
    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError):
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        LOG.warning("无法保存转写历史 task_id=%s", task_id)


def load_transcription_history() -> None:
    history_dir = transcription_history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    recovered: dict[str, dict] = {}
    for path in history_dir.glob("*.json"):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("忽略无法读取的转写历史文件：%s", path.name)
            continue
        if not isinstance(task, dict) or not re.fullmatch(r"[a-f0-9]{24}", str(task.get("task_id") or "")):
            continue
        if task.get("status") in TRANSCRIPTION_ACTIVE_STATUSES:
            if task.get("cancel_requested"):
                task.update(status="cancelled", stage="已取消", finished_at=datetime.now().isoformat(timespec="seconds"))
            else:
                task.update(
                    status="failed",
                    stage="任务已中断",
                    progress=0,
                    error="程序关闭时任务仍在运行，已标记中断；请重新创建任务。",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
        recovered[task["task_id"]] = {key: task.get(key) for key in TRANSCRIPTION_HISTORY_FIELDS}
    with TRANSCRIPTION_LOCK:
        TRANSCRIPTION_TASKS.update(recovered)
        for task in recovered.values():
            if task.get("stage") == "任务已中断" or (task.get("status") == "cancelled" and task.get("cancel_requested")):
                persist_transcription(task)


def public_transcription(task: dict) -> dict:
    public = {key: task.get(key) for key in TRANSCRIPTION_HISTORY_FIELDS}
    result = public.get("result")
    if isinstance(result, dict):
        result = dict(result)
        result["text"] = normalize_transcript_text(result.get("text"))
        public["result"] = result
    return public


def update_transcription(task_id: str, **fields: object) -> dict | None:
    with TRANSCRIPTION_LOCK:
        task = TRANSCRIPTION_TASKS.get(task_id)
        if task is None:
            return None
        if task.get("cancel_requested") and fields.get("status") != "cancelled":
            if fields.get("status") in {"succeeded", "failed"}:
                fields = {
                    "status": "cancelled",
                    "stage": "已取消",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                }
            else:
                return dict(task)
        if ("stage" in fields and fields["stage"] != task.get("stage")) or ("request_attempt" in fields and fields["request_attempt"] != task.get("request_attempt")):
            fields.setdefault("stage_started_at", datetime.now().astimezone().isoformat(timespec="seconds"))
        task.update(fields)
        if isinstance(task.get("result"), dict):
            task["result"] = dict(task["result"])
            task["result"]["text"] = normalize_transcript_text(task["result"].get("text"))
        persist_transcription(task)
        return dict(task)


class TranscriptionCancelled(Exception):
    pass


def transcription_cancel_requested(task_id: str) -> bool:
    with TRANSCRIPTION_LOCK:
        return bool(TRANSCRIPTION_TASKS.get(task_id, {}).get("cancel_requested"))


def check_transcription_cancelled(task_id: str) -> None:
    if transcription_cancel_requested(task_id):
        raise TranscriptionCancelled("转写任务已取消。")


def mark_transcription_cancelled(task_id: str) -> dict | None:
    return update_transcription(
        task_id,
        status="cancelled",
        stage="已取消",
        finished_at=datetime.now().isoformat(timespec="seconds"),
    )


def refresh_mimo_queue_positions() -> None:
    with MIMO_QUEUE_CONDITION:
        queued = list(MIMO_QUEUE)
        ahead_base = 1 if MIMO_ACTIVE_TASK_ID else 0
    for index, task_id in enumerate(queued):
        with TRANSCRIPTION_LOCK:
            task = TRANSCRIPTION_TASKS.get(task_id)
            old_ahead = task.get("queue_ahead") if task else None
        ahead = ahead_base + index
        if task and (old_ahead != ahead or task.get("status") != "queued"):
            update_transcription(task_id, status="queued", stage=f"云端排队中 · 前方 {ahead} 个任务", queue_ahead=ahead)


def acquire_mimo_queue(task_id: str) -> None:
    global MIMO_ACTIVE_TASK_ID
    with MIMO_QUEUE_CONDITION:
        if task_id not in MIMO_QUEUE:
            MIMO_QUEUE.append(task_id)
            MIMO_QUEUE_CONDITION.notify_all()
    refresh_mimo_queue_positions()
    while True:
        check_transcription_cancelled(task_id)
        with MIMO_QUEUE_CONDITION:
            if MIMO_ACTIVE_TASK_ID == "" and MIMO_QUEUE and MIMO_QUEUE[0] == task_id:
                MIMO_QUEUE.pop(0)
                MIMO_ACTIVE_TASK_ID = task_id
                MIMO_QUEUE_CONDITION.notify_all()
                acquired = True
            else:
                acquired = False
                MIMO_QUEUE_CONDITION.wait(timeout=0.5)
        if acquired:
            refresh_mimo_queue_positions()
            return


def release_mimo_queue(task_id: str) -> None:
    global MIMO_ACTIVE_TASK_ID
    with MIMO_QUEUE_CONDITION:
        if MIMO_ACTIVE_TASK_ID == task_id:
            MIMO_ACTIVE_TASK_ID = ""
        if task_id in MIMO_QUEUE:
            MIMO_QUEUE.remove(task_id)
        MIMO_QUEUE_CONDITION.notify_all()
    refresh_mimo_queue_positions()


def cancel_transcription(task_id: str) -> dict:
    with TRANSCRIPTION_LOCK:
        task = TRANSCRIPTION_TASKS.get(task_id)
        if task is None:
            raise KeyError("没有找到这个转写任务。")
        if task.get("status") not in TRANSCRIPTION_ACTIVE_STATUSES:
            raise ValueError("这个转写任务已经结束。")
        request_lock = TRANSCRIPTION_REQUEST_LOCKS.get(task_id)
    if request_lock:
        request_lock.acquire()
    try:
        with TRANSCRIPTION_LOCK:
            task = TRANSCRIPTION_TASKS.get(task_id)
            if task is None:
                raise KeyError("没有找到这个转写任务。")
            if task.get("status") not in TRANSCRIPTION_ACTIVE_STATUSES:
                raise ValueError("这个转写任务已经结束。")
            if task.get("cancel_requested"):
                return public_transcription(dict(task))
            task["cancel_requested"] = True
            task["stage"] = "正在取消"
            task["stage_started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            persist_transcription(task)
            snapshot = dict(task)
            cancel_event = TRANSCRIPTION_CANCEL_EVENTS.get(task_id)
            service = TRANSCRIPTION_SERVICES.get(task_id)
    finally:
        if request_lock:
            request_lock.release()
    if cancel_event:
        cancel_event.set()

    with MIMO_QUEUE_CONDITION:
        was_queued = task_id in MIMO_QUEUE
        if was_queued:
            MIMO_QUEUE.remove(task_id)
        is_active_mimo = MIMO_ACTIVE_TASK_ID == task_id
        MIMO_QUEUE_CONDITION.notify_all()

    asr_task_id = str(snapshot.get("asr_task_id") or "")
    if asr_task_id:
        with TRANSCRIPTION_LOCK:
            current = TRANSCRIPTION_TASKS.get(task_id)
            if current:
                current["asr_cancel_sent"] = True
                persist_transcription(current)
        delete_asr_job(asr_task_id)
    download_task_id = str(snapshot.get("download_task_id") or "")
    if service and download_task_id:
        try:
            service.request(f"/api/v1/tasks/{urllib.parse.quote(download_task_id, safe='')}/stop", "POST", {})
        except Exception as error:
            LOG.warning("停止转写音频下载失败 task_id=%s error=%s", task_id, redact(error))

    if was_queued or (snapshot.get("status") == "queued" and not is_active_mimo and not snapshot.get("asr_task_id")):
        cancelled = mark_transcription_cancelled(task_id)
        refresh_mimo_queue_positions()
        return public_transcription(cancelled or snapshot)
    return public_transcription(snapshot)


def new_transcription_task(source: dict, *, parent_id: str | None = None, filename: str = "", provider_override: str | None = None, model_override: str | None = None) -> dict:
    task_id = secrets.token_hex(12)
    title = str(source.get("title") or "未命名视频")
    name = f"{title} · {filename}" if filename else title
    provider, details = transcription_provider_settings()
    if provider_override in TRANSCRIPTION_PROVIDERS:
        provider = provider_override
    model = model_override or TRANSCRIPTION_PROVIDERS[provider]["model"]
    task = {
        "task_id": task_id,
        "name": name,
        "source_url": str(source.get("url") or ""),
        "source_title": title,
        "status": "queued",
        "stage": "等待开始",
        "progress": 0,
        "error": "",
        "result": None,
        "filename": filename,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": "",
        "parent_id": parent_id,
        "provider": provider,
        "model": model,
        "cancel_requested": False,
        "stage_started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request_attempt": 0,
        "pages": [str(page) for page in source.get("pages", []) if str(page).isdigit()],
        "all_pages": bool(source.get("all_pages")),
        "audio_path": "",
        "asr_task_id": "",
        "download_task_id": "",
    }
    with TRANSCRIPTION_LOCK:
        TRANSCRIPTION_TASKS[task_id] = task
        TRANSCRIPTION_CANCEL_EVENTS[task_id] = threading.Event()
        TRANSCRIPTION_REQUEST_LOCKS[task_id] = threading.Lock()
        persist_transcription(task)
    return task


def delete_transcription_history(task_ids: list[str] | None = None, *, clear_all: bool = False) -> dict[str, list[str]]:
    selected = set(task_ids or [])
    if not clear_all and any(not re.fullmatch(r"[a-f0-9]{24}", task_id) for task_id in selected):
        raise ValueError("转写任务编号无效。")
    deleted: list[str] = []
    skipped: list[str] = []
    with TRANSCRIPTION_LOCK:
        candidates = list(TRANSCRIPTION_TASKS) if clear_all else list(selected)
        for task_id in candidates:
            task = TRANSCRIPTION_TASKS.get(task_id)
            if task is None:
                continue
            if task.get("status") in TRANSCRIPTION_ACTIVE_STATUSES:
                skipped.append(task_id)
                continue
            history_path = transcription_history_dir() / f"{task_id}.json"
            try:
                history_path.with_suffix(".json.tmp").unlink(missing_ok=True)
                history_path.unlink(missing_ok=True)
            except OSError as error:
                LOG.warning("无法删除转写历史 task_id=%s error=%s", task_id, short_error(error))
                skipped.append(task_id)
                continue
            del TRANSCRIPTION_TASKS[task_id]
            deleted.append(task_id)
        if clear_all:
            active_ids = {task_id for task_id, task in TRANSCRIPTION_TASKS.items() if task.get("status") in TRANSCRIPTION_ACTIVE_STATUSES}
            history_dir = transcription_history_dir()
            for path in (*history_dir.glob("*.json"), *history_dir.glob("*.json.tmp")):
                task_id = path.name.removesuffix(".json.tmp") if path.name.endswith(".json.tmp") else path.stem
                if task_id in active_ids:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    LOG.warning("无法清理转写历史缓存 task_id=%s error=%s", task_id, short_error(error))
                    skipped.append(task_id)
    return {"deleted": deleted, "skipped": skipped}


def asr_error_message(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error") or detail.get("detail")
        if detail:
            return short_error(detail)
    return fallback


def asr_json(path: str, method: str = "GET", data: dict | None = None) -> tuple[int, object]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        f"http://{ASR_SERVICE_HOST}:{ASR_SERVICE_PORT}{path}",
        body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with LOCAL_OPENER.open(request, timeout=15) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                return response.status, {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return error.code, {}
    except urllib.error.URLError as error:
        raise RuntimeError("本地转写服务未启动。") from error


def asr_upload(audio_path: Path) -> dict:
    boundary = f"----BilibiliDownloader{secrets.token_hex(12)}"
    filename = audio_path.name.replace("\\", "_").replace('"', "_")
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    epilogue = f"\r\n--{boundary}--\r\n".encode("ascii")
    connection = http.client.HTTPConnection(ASR_SERVICE_HOST, ASR_SERVICE_PORT, timeout=120)
    try:
        content_length = len(preamble) + audio_path.stat().st_size + len(epilogue)
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("Connection", "close")
        connection.endheaders()
        connection.send(preamble)
        with audio_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                connection.send(chunk)
        connection.send(epilogue)
        response = connection.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if response.status != HTTPStatus.ACCEPTED:
            raise RuntimeError(asr_error_message(payload, f"转写服务提交失败（HTTP {response.status}）。"))
        if not isinstance(payload, dict) or not payload.get("task_id"):
            raise RuntimeError("转写服务提交失败：返回的任务信息不完整。")
        return payload
    except OSError as error:
        raise RuntimeError("转写服务提交失败：无法读取或发送音频文件。") from error
    finally:
        connection.close()


def delete_asr_job(task_id: str) -> None:
    try:
        status, payload = asr_json(f"/v1/jobs/{urllib.parse.quote(task_id, safe='')}", "DELETE")
        if status not in {HTTPStatus.NO_CONTENT, HTTPStatus.OK}:
            LOG.warning("清理转写服务任务失败 status=%s error=%s", status, asr_error_message(payload, "未知错误"))
    except Exception as error:
        LOG.warning("清理转写服务任务失败：%s", redact(error))


def transcription_audio_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in TRANSCRIPTION_AUDIO_SUFFIXES),
        key=lambda path: str(path).casefold(),
    )


def run_local_transcription_audio(task_id: str, audio_path: Path) -> None:
    asr_task_id = ""
    try:
        check_transcription_cancelled(task_id)
        with TRANSCRIPTION_LOCK:
            source_title = str(TRANSCRIPTION_TASKS.get(task_id, {}).get("source_title") or "未命名视频")
        update_transcription(task_id, status="uploading", stage="提交本地转写服务", progress=40, filename=audio_path.name, name=f"{source_title} · {audio_path.name}")
        status, health = asr_json("/health")
        if status != HTTPStatus.OK or not isinstance(health, dict) or health.get("ready") is not True:
            raise RuntimeError(asr_error_message(health, "本地转写服务未就绪。"))
        check_transcription_cancelled(task_id)
        payload = asr_upload(audio_path)
        asr_task_id = str(payload["task_id"])
        update_transcription(task_id, asr_task_id=asr_task_id, status="queued", stage="排队中", progress=45)
        while True:
            check_transcription_cancelled(task_id)
            status, current = asr_json(f"/v1/jobs/{urllib.parse.quote(asr_task_id, safe='')}")
            if status != HTTPStatus.OK or not isinstance(current, dict):
                raise RuntimeError(asr_error_message(current, "无法读取本地转写任务状态。"))
            current_status = str(current.get("status") or "")
            raw_progress = current.get("progress")
            service_progress = max(0, min(100, int(float(raw_progress)))) if isinstance(raw_progress, (int, float)) else 0
            if current_status == "queued":
                update_transcription(task_id, status="queued", stage="排队中", progress=max(45, min(55, 45 + service_progress // 10)))
            elif current_status == "processing":
                update_transcription(task_id, status="processing", stage="正在转写", progress=45 + round(service_progress * 0.55))
            elif current_status == "succeeded":
                result_status, result = asr_json(f"/v1/jobs/{urllib.parse.quote(asr_task_id, safe='')}/result")
                if result_status != HTTPStatus.OK or not isinstance(result, dict):
                    raise RuntimeError(asr_error_message(result, "文字稿结果读取失败。"))
                update_transcription(task_id, status="succeeded", stage="转写完成", progress=100, result=result, finished_at=datetime.now().isoformat(timespec="seconds"))
                return
            elif current_status in {"failed", "cancelled"}:
                if current_status == "cancelled" and transcription_cancel_requested(task_id):
                    raise TranscriptionCancelled("转写任务已取消。")
                raise RuntimeError(asr_error_message(current, "本地转写服务未完成任务。"))
            else:
                raise RuntimeError(f"本地转写服务返回未知状态：{current_status or '空'}")
            time.sleep(1)
    except TranscriptionCancelled:
        mark_transcription_cancelled(task_id)
    except Exception as error:
        update_transcription(task_id, status="failed", stage="转写失败", progress=0, error=short_error(error), finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        if asr_task_id:
            with TRANSCRIPTION_LOCK:
                task = TRANSCRIPTION_TASKS.get(task_id) or {}
                cancel_sent = bool(task.get("asr_cancel_sent"))
            if not cancel_sent:
                delete_asr_job(asr_task_id)


def run_transcription_audio(task_id: str, audio_path: Path) -> None:
    with TRANSCRIPTION_LOCK:
        task = dict(TRANSCRIPTION_TASKS.get(task_id) or {})
    provider = str(task.get("provider") or "local")
    model = str(task.get("model") or TRANSCRIPTION_PROVIDERS["local"]["model"])
    if provider == "local":
        run_local_transcription_audio(task_id, audio_path)
        return
    acquired = False
    try:
        acquire_mimo_queue(task_id)
        acquired = True
        check_transcription_cancelled(task_id)
        def progress_callback(progress: int, stage: str) -> None:
            update_transcription(task_id, status="processing", stage=stage, progress=progress)
        result = mimo_transcribe_audio(audio_path, audio_path.parent / "mimo", provider, model, progress_callback, task_id)
        check_transcription_cancelled(task_id)
        update_transcription(task_id, status="succeeded", stage="转写完成", progress=100, result=result, finished_at=datetime.now().isoformat(timespec="seconds"))
    except TranscriptionCancelled:
        mark_transcription_cancelled(task_id)
    except Exception as error:
        update_transcription(task_id, status="failed", stage="转写失败", progress=0, error=short_error(error), finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        if acquired:
            release_mimo_queue(task_id)


def run_manual_transcription(task_id: str, audio_path: Path, temp_dir: Path) -> None:
    try:
        update_transcription(task_id, status="queued", stage="等待转写", progress=5, filename=audio_path.name)
        run_transcription_audio(task_id, audio_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        with TRANSCRIPTION_LOCK:
            TRANSCRIPTION_CANCEL_EVENTS.pop(task_id, None)
            TRANSCRIPTION_REQUEST_LOCKS.pop(task_id, None)


def run_transcription_pipeline(task_id: str, source: dict) -> None:
    temp_dir = transcription_root() / task_id
    service = BBDownService()
    service.respect_config = False
    with TRANSCRIPTION_LOCK:
        TRANSCRIPTION_SERVICES[task_id] = service
    try:
        check_transcription_cancelled(task_id)
        update_transcription(task_id, status="downloading", stage="正在获取音频", progress=5)
        status, health = asr_json("/health")
        if status != HTTPStatus.OK or not isinstance(health, dict) or health.get("ready") is not True:
            raise RuntimeError(asr_error_message(health, "本地转写服务未就绪，无法开始自动转写。"))
        temp_dir.mkdir(parents=True, exist_ok=True)
        ok, message = service.ensure_started(str(temp_dir), respect_config=False)
        if not ok:
            raise RuntimeError(message)
        check_transcription_cancelled(task_id)
        request_data = {
            "url": source["url"],
            "pages": ",".join(source.get("pages") or []) if source.get("pages") else "all",
            "content": "a",
            "mux": "mpeg4",
            "dfnPriority": "1080P 高码率,1080P60,1080P+,1080P",
            "maxRetry": 3,
        }
        status, result = service.request("/api/v1/tasks", "POST", request_data)
        if status >= 300 or not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError(short_error(result.get("error") if isinstance(result, dict) else result or "音频下载任务提交失败。"))
        download_task_id = str(result["id"])
        update_transcription(task_id, download_task_id=download_task_id)
        while True:
            check_transcription_cancelled(task_id)
            status, current = service.request(f"/api/v1/tasks/{urllib.parse.quote(download_task_id, safe='')}")
            if status >= 300 or not isinstance(current, dict):
                raise RuntimeError("无法读取音频下载任务状态。")
            raw_progress = current.get("progress")
            progress = max(0, min(100, int(float(raw_progress) * 100))) if isinstance(raw_progress, (int, float)) else 0
            update_transcription(task_id, progress=max(5, round(progress * 0.35)), stage="正在获取音频")
            current_status = str(current.get("status") or "")
            if current_status == "Finished":
                if current.get("isCancelled") or not current.get("isSuccessful"):
                    raise RuntimeError(short_error(current.get("errorMessage") or "音频下载失败。"))
                break
            if current.get("isCancelled"):
                raise RuntimeError("音频下载已取消。")
            time.sleep(1)
        audio_files = transcription_audio_files(temp_dir)
        if not audio_files:
            raise RuntimeError("音频下载完成但未找到音频文件。")
        for index, audio_path in enumerate(audio_files):
            check_transcription_cancelled(task_id)
            if index == 0:
                target_id = task_id
            else:
                with TRANSCRIPTION_LOCK:
                    parent_task = dict(TRANSCRIPTION_TASKS.get(task_id) or {})
                child = new_transcription_task(
                    source,
                    parent_id=task_id,
                    filename=audio_path.name,
                    provider_override=str(parent_task.get("provider") or "local"),
                    model_override=str(parent_task.get("model") or ""),
                )
                target_id = child["task_id"]
            update_transcription(target_id, name=f"{source.get('title') or '未命名视频'} · {audio_path.name}", filename=audio_path.name, audio_path=str(audio_path))
            run_transcription_audio(target_id, audio_path)
    except TranscriptionCancelled:
        mark_transcription_cancelled(task_id)
    except Exception as error:
        if transcription_cancel_requested(task_id):
            mark_transcription_cancelled(task_id)
        else:
            update_transcription(task_id, status="failed", stage="转写失败", progress=0, error=short_error(error), finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        service.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)
        with TRANSCRIPTION_LOCK:
            TRANSCRIPTION_SERVICES.pop(task_id, None)
            TRANSCRIPTION_CANCEL_EVENTS.pop(task_id, None)
            TRANSCRIPTION_REQUEST_LOCKS.pop(task_id, None)
            for child_id, task in TRANSCRIPTION_TASKS.items():
                if task.get("parent_id") == task_id:
                    TRANSCRIPTION_CANCEL_EVENTS.pop(child_id, None)
                    TRANSCRIPTION_REQUEST_LOCKS.pop(child_id, None)


def start_transcription(source: dict) -> dict:
    task = new_transcription_task(source)
    threading.Thread(target=run_transcription_pipeline, args=(task["task_id"], source), daemon=True).start()
    return public_transcription(task)


def schedule_restart() -> bool:
    """让本地启动器接管版本替换，避免当前进程继续服务旧代码。"""
    global RESTART_SCHEDULED
    with RESTART_LOCK:
        if RESTART_SCHEDULED:
            return False
        RESTART_SCHEDULED = True

    def restart() -> None:
        try:
            launcher = ROOT / "启动工具.pyw"
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            executable = str(pythonw if pythonw.exists() else sys.executable)
            LOG.warning("检测到磁盘代码更新，启动器将替换当前后台实例")
            run_hidden(
                [executable, str(launcher)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            LOG.exception("自动重启启动器失败")

    threading.Thread(target=restart, daemon=True).start()
    return True


def video_metadata(value: str) -> dict:
    bvid = re.search(r"(?i)(BV[0-9A-Z]{10})", value)
    av = re.search(r"(?i)(?:^|[^a-z])av(\d+)", value)
    if not bvid and not av:
        raise ValueError("请输入有效的 B 站视频链接或 BV 号。")
    query = {"bvid": bvid.group(1)} if bvid else {"aid": av.group(1)}
    url = "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise ValueError("无法连接 B 站获取视频信息，请检查网络后重试。") from error
    if payload.get("code") != 0 or not payload.get("data"):
        raise ValueError(payload.get("message") or "无法解析该视频，请确认链接有效且公开视频可访问。")
    data = payload["data"]
    pages = [
        {"page": page["page"], "title": page.get("part") or f"P{page['page']}", "duration": page.get("duration", 0)}
        for page in data.get("pages", [])
    ]
    # 以 BBDown 的 info-only 实际验证下载核心可解析该资源；页面资料仍用 B 站公开接口保证稳定展示。
    check = run_hidden(
        [str(BBDOWN), value, "--info-only", "--all", "--hide-streams"], cwd=ROOT,
        env=SERVICE._environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="gbk", errors="replace",
    )
    output, _ = check.communicate(timeout=45)
    LOG.info("BBDown info-only 解析：%s", output[-1500:])
    if check.returncode != 0:
        raise ValueError("BBDownNext 无法解析该视频，请检查链接或登录状态。")
    return {
        "url": data.get("short_link_v2") or value,
        "bvid": data.get("bvid", ""),
        "title": data.get("title", "未命名视频"),
        "cover": data.get("pic", ""),
        "owner": data.get("owner", {}).get("name", "未知 UP 主"),
        "pages": pages,
    }


def parse_up_mid(value: str) -> str:
    value = value.strip()
    if value.isdigit():
        return value
    match = re.search(r"(?i)(?:https?://)?(?:www\.)?space\.bilibili\.com/(\d+)", value)
    if match:
        return match.group(1)
    raise ValueError("请输入有效的 UP 主主页链接或 mid。")


def bilibili_json(url: str, *, referer: str = "https://www.bilibili.com/", extra_headers: dict | None = None, track_response_cookies: bool = True) -> tuple[int, dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Origin": "https://space.bilibili.com",
    }
    session_cookie = BILIBILI_SESSION.cookie_header()
    if session_cookie:
        headers["Cookie"] = session_cookie
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with LOCAL_OPENER.open(request, timeout=15) as response:
            if track_response_cookies:
                BILIBILI_SESSION.merge_set_cookies(response.headers.get_all("Set-Cookie") or [])
            body = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                LOG.warning("B 站接口返回非 JSON status=%s host=%s", response.status, urllib.parse.urlsplit(url).netloc)
                return response.status, {}
    except urllib.error.HTTPError as error:
        if track_response_cookies:
            BILIBILI_SESSION.merge_set_cookies(error.headers.get_all("Set-Cookie") or [])
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            LOG.warning("B 站接口 HTTP %s：%s", error.code, body[:500])
            return error.code, {}
    except urllib.error.URLError as error:
        raise ValueError("无法连接 B 站获取投稿，请检查网络后重试。") from error


LOGIN_STATE: dict[str, object] = {}
LOGIN_LOCK = threading.Lock()
BILIBILI_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILIBILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"


def login_request(url: str) -> tuple[int, dict, list[str]]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    cookie = BILIBILI_SESSION.cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with LOCAL_OPENER.open(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body), error.headers.get_all("Set-Cookie") or []
        except json.JSONDecodeError:
            return error.code, {}, []
    except urllib.error.URLError as error:
        raise ValueError("无法连接 B 站登录服务，请检查网络后重试。") from error


def login_status_text(code: int) -> str:
    return {86101: "等待扫码", 86090: "已扫码，等待确认", 86038: "二维码已过期"}.get(code, "等待扫码")


def start_login() -> dict:
    anonymous_cookie()
    status, payload, set_cookies = login_request(BILIBILI_QR_GENERATE)
    BILIBILI_SESSION.merge_set_cookies(set_cookies)
    BILIBILI_SESSION.log_cookie_names("申请二维码后")
    data = payload.get("data") or {}
    if status != 200 or payload.get("code") != 0 or not data.get("url") or not data.get("qrcode_key"):
        raise ValueError("申请 B 站登录二维码失败，请稍后重试。")
    with LOGIN_LOCK:
        LOGIN_STATE.clear()
        LOGIN_STATE.update({"qrcode_key": str(data["qrcode_key"]), "status": "等待扫码", "expires_at": time.time() + 180, "qr_url": str(data["url"])})
    return {"status": "等待扫码", "expires_at": LOGIN_STATE["expires_at"], "qr_url": str(data["url"]), "qr_image": "/api/login/qr.svg?url=" + urllib.parse.quote(str(data["url"]), safe=""), "qrcode_key": str(data["qrcode_key"])}


def poll_login() -> dict:
    with LOGIN_LOCK:
        key = str(LOGIN_STATE.get("qrcode_key") or "")
        expires_at = float(LOGIN_STATE.get("expires_at") or 0)
    if not key:
        return {"logged_in": bool(load_auth_cookie()), "status": "未登录"}
    if time.time() >= expires_at:
        with LOGIN_LOCK:
            LOGIN_STATE["status"] = "二维码已过期"
        return {"logged_in": False, "status": "二维码已过期"}
    status, payload, set_cookies = login_request(BILIBILI_QR_POLL + "?" + urllib.parse.urlencode({"qrcode_key": key, "source": "main_web"}))
    data = payload.get("data") or {}
    result_code = int(data.get("code") or payload.get("code") or 0)
    if result_code == 0 and data.get("url"):
        cookie = SimpleCookie()
        for header in set_cookies:
            cookie.load(header)
        pairs = [f"{name}={morsel.value}" for name, morsel in cookie.items()]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(data["url"])).query)
        for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
            if query.get(name) and not any(pair.startswith(name + "=") for pair in pairs):
                pairs.append(f"{name}={query[name][0]}")
        if not pairs:
            raise ValueError("B 站登录成功但未取得登录凭据，请重试。")
        BILIBILI_SESSION.merge_set_cookies(set_cookies)
        BILIBILI_SESSION.merge("; ".join(pairs), account=True)
        BILIBILI_SESSION.log_cookie_names("扫码登录成功后")
        with LOGIN_LOCK:
            LOGIN_STATE.clear()
            LOGIN_STATE["status"] = "登录成功"
        return {"logged_in": True, "status": "登录成功"}
    text = login_status_text(result_code)
    with LOGIN_LOCK:
        LOGIN_STATE["status"] = text
    return {"logged_in": False, "status": text}


def account_status() -> dict:
    if not load_auth_cookie():
        return {"logged_in": False, "status": "未登录"}
    try:
        status, payload = bilibili_json("https://api.bilibili.com/x/web-interface/nav", referer="https://www.bilibili.com/")
        data = payload.get("data") or {}
        if status == 200 and payload.get("code") == 0 and data.get("isLogin"):
            BILIBILI_SESSION.log_cookie_names("登录状态核验")
            return {"logged_in": True, "status": "已登录", "name": data.get("uname") or "B 站账号", "mid": data.get("mid")}
    except ValueError:
        pass
    return {"logged_in": False, "status": "登录已失效，请重新扫码"}


def qr_module():
    if str(QR_PACKAGE_DIR) not in sys.path:
        sys.path.insert(0, str(QR_PACKAGE_DIR))
    try:
        import qrcode
        return qrcode
    except ImportError:
        QR_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
        python = Path(sys.executable)
        pip_env = os.environ.copy()
        for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            pip_env.pop(proxy_name, None)
        result = subprocess.run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
            "--target", str(QR_PACKAGE_DIR), "qrcode",
        ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=pip_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=45, check=False)
        if result.returncode != 0:
            LOG.warning("二维码组件准备失败：%s", result.stdout[-500:])
            raise ValueError("二维码组件准备失败，请检查网络后重试。")
        try:
            import qrcode
            return qrcode
        except ImportError as error:
            raise ValueError("二维码组件准备失败，请重新启动工具后重试。") from error


def qr_svg(value: str) -> bytes:
    qrcode = qr_module()
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    path = "".join(f"M{x} {y}h1v1h-1z" for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="white"/><path d="{path}" fill="black"/></svg>'.encode("utf-8")


def anonymous_cookie() -> str:
    existing = BILIBILI_SESSION.cookie_header(account=False, device=True)
    if "buvid3=" in existing and "buvid4=" in existing:
        return existing
    status, payload = bilibili_json("https://api.bilibili.com/x/frontend/finger/spi", extra_headers={"Cookie": ""})
    data = payload.get("data") or {}
    values = {cookie_name: str(data[data_name]) for cookie_name, data_name in (("buvid3", "b_3"), ("buvid4", "b_4")) if data.get(data_name)}
    if status != 200 or not values:
        raise ValueError("B 站匿名访问参数暂时不可用，请稍后重试。")
    BILIBILI_SESSION.merge_device_values(values)
    return BILIBILI_SESSION.cookie_header(account=False, device=True)


def request_cookie() -> str:
    anonymous_cookie()
    return BILIBILI_SESSION.cookie_header()


def wbi_signed_query(params: dict) -> str:
    global WBI_MIXIN_KEY
    if not WBI_MIXIN_KEY:
        status, payload = bilibili_json("https://api.bilibili.com/x/web-interface/nav")
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        img_key = re.search(r"/([^/]+)\.[a-z]+$", img_url)
        sub_key = re.search(r"/([^/]+)\.[a-z]+$", sub_url)
        if status != 200 or not img_key or not sub_key:
            raise ValueError("B 站安全参数暂时不可用，请稍后重试。")
        raw_key = img_key.group(1) + sub_key.group(1)
        WBI_MIXIN_KEY = "".join(raw_key[index] for index in WBI_MIXIN_KEY_TABLE if index < len(raw_key))[:32]
    signed = {**params, "wts": int(time.time())}
    clean = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in signed.items()}
    query = "&".join(f"{key}={urllib.parse.quote(clean[key], safe='')}" for key in sorted(clean))
    return f"{query}&w_rid={hashlib.md5((query + WBI_MIXIN_KEY).encode('utf-8')).hexdigest()}"


def up_profile(mid: str) -> dict:
    status, payload = bilibili_json(
        BILIBILI_PROFILE_API + "?" + urllib.parse.urlencode({"mid": mid}),
        referer=f"https://space.bilibili.com/{mid}",
    )
    code = payload.get("code")
    if status == 412 or code in {-352, -412}:
        raise ValueError("已登录但 B 站拒绝了本次 UP 主资料请求，程序正在尝试备用数据源。")
    if code == -799:
        raise ValueError("B 站请求过于频繁，请稍后重试。")
    data = payload.get("data") or {}
    card = data.get("card") or {}
    if code != 0 or not card:
        raise ValueError("找不到该 UP 主或主页不存在。")
    return {
        "mid": mid,
        "name": card.get("name") or "未命名 UP 主",
        "face": card.get("face") or "",
        "total": int(data.get("archive_count") or 0),
    }


def duration_seconds(value: str) -> int:
    try:
        parts = [int(part) for part in str(value).split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        pass
    return 0


def up_period_cutoff(period: str) -> float | None:
    days = {"year": 365, "half_year": 182, "quarter": 90}.get(period)
    return time.time() - days * 86400 if days else None


def space_feed(query: dict, mid: str) -> tuple[int, dict]:
    signed_url = BILIBILI_SPACE_API + "?" + wbi_signed_query(query)
    status, payload = bilibili_json(
        signed_url,
        referer=f"https://space.bilibili.com/{mid}",
        extra_headers={"Cookie": request_cookie()},
    )
    data = payload.get("data") or {}
    # 该公开接口偶尔会对带签名请求返回 code=0 但空列表；再尝试一次兼容的无签名请求。
    if payload.get("code") == 0 and not (data.get("items") or {}):
        fallback_status, fallback_payload = bilibili_json(
            BILIBILI_SPACE_API + "?" + urllib.parse.urlencode(query),
            referer=f"https://space.bilibili.com/{mid}",
            extra_headers={"Cookie": request_cookie()},
        )
        fallback_data = fallback_payload.get("data") or {}
        if fallback_payload.get("code") == 0 and fallback_data.get("items"):
            return fallback_status, fallback_payload
    return status, payload


def normalize_up_item(item: dict, fallback_name: str) -> dict | None:
    if item.get("type") != "DYNAMIC_TYPE_AV":
        return None
    archive = {}
    author = {}
    modules = item.get("modules") or []
    if isinstance(modules, dict):
        modules = [modules]
    for module in modules:
        if not isinstance(module, dict):
            continue
        dynamic = module.get("module_dynamic") or {}
        archive = dynamic.get("dyn_archive") or archive
        author = module.get("module_author") or author
    bvid = str(archive.get("bvid") or "").strip()
    if not bvid:
        return None
    pub_ts = int(author.get("pub_ts") or 0)
    duration_text = str(archive.get("duration_text") or "")
    return {
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": archive.get("title") or fallback_name,
        "cover": archive.get("cover") or "",
        "pub_ts": pub_ts,
        "publish_time": datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d") if pub_ts else "未知时间",
        "duration": duration_seconds(duration_text),
        "duration_text": duration_text or "未知时长",
    }


def normalize_arc_item(item: dict, fallback_name: str) -> dict | None:
    bvid = str(item.get("bvid") or "").strip()
    if not bvid:
        return None
    title = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("title") or fallback_name))).strip()
    pub_ts = int(item.get("created") or item.get("pubdate") or 0)
    duration_text = str(item.get("length") or "")
    return {
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": title or fallback_name,
        "cover": str(item.get("pic") or "").replace("http://", "https://"),
        "pub_ts": pub_ts,
        "publish_time": datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d") if pub_ts else "未知时间",
        "duration": duration_seconds(duration_text),
        "duration_text": duration_text or "未知时长",
    }


def bili_error(status: int, payload: dict, action: str) -> None:
    code = payload.get("code")
    if status == 412 or code in {-352, -412}:
        LOG.warning("B 站%s触发安全验证 status=%s code=%s message=%s", action, status, code, payload.get("message"))
        raise ValueError(f"已登录但 B 站拒绝了本次{action}请求，程序正在尝试备用数据源。")
    if code == -799:
        raise ValueError("B 站请求过于频繁，请稍后重试。")
    if code != 0:
        LOG.warning("B 站%s失败 status=%s code=%s message=%s", action, status, code, payload.get("message"))
        raise ValueError(f"B 站{action}失败，请稍后重试。")


def arc_search_query(mid: str, page: int, keyword: str, page_size: int) -> dict:
    return {
        "mid": mid, "pn": page, "ps": page_size, "index": 0, "order": "pubdate", "keyword": keyword,
        "platform": "web", "web_location": "333.1387", "order_avoided": "true",
        "x-bili-locale-json": WEB_LOCALE, "x-bili-device-req-json": WEB_DEVICE,
        "dm_img_list": "[]",
        "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
        "dm_cover_img_str": "QU5HTEUgKE1pY3Jvc29mdCwgTWljcm9zb2Z0IEJhc2ljIFJlbmRlciBEcml2ZXIgKDB4MDAwMDAwOEMpIERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEpR29vZ2xlIEluYy4gKE5WSURJIEdlRm9yY2U",
        "dm_img_inter": json.dumps({"ds": [], "wh": [4922, 5679, 104], "of": [195, 390, 195]}, separators=(",", ":")),
    }


def arc_search_once(mid: str, page: int, keyword: str, page_size: int, *, cookie: str, headers: dict | None = None, track_response_cookies: bool = True) -> tuple[int, dict]:
    signed_url = BILIBILI_ARC_SEARCH_API + "?" + wbi_signed_query(arc_search_query(mid, page, keyword, page_size))
    return bilibili_json(
        signed_url,
        referer=f"https://space.bilibili.com/{mid}/video",
        extra_headers={"Cookie": cookie, "Accept": "application/json, text/plain, */*", **(headers or {})},
        track_response_cookies=track_response_cookies,
    )


def legacy_arc_search_once(mid: str, page: int, keyword: str, page_size: int, *, cookie: str) -> tuple[int, dict]:
    query = {"mid": mid, "pn": page, "ps": page_size, "index": 0, "order": "pubdate", "keyword": keyword}
    return bilibili_json(
        BILIBILI_LEGACY_ARC_SEARCH_API + "?" + urllib.parse.urlencode(query),
        referer=f"https://space.bilibili.com/{mid}/upload/video",
        extra_headers={"Cookie": cookie, "Accept": "application/json, text/plain, */*"},
    )


def arc_page_result(status: int, payload: dict, page: int, page_size: int, owner_name: str, source: str) -> dict:
    data = payload.get("data") or {}
    page_info = data.get("page") or {}
    items = [normalized for raw in (data.get("list") or {}).get("vlist", []) if (normalized := normalize_arc_item(raw, owner_name))]
    count = int(page_info.get("count") or 0)
    actual_page = int(page_info.get("pn") or page)
    actual_size = int(page_info.get("ps") or page_size)
    if page > 1 and actual_page != page:
        raise ValueError("B 站返回的投稿分页不可用，已尝试的投稿数据源均未能给出可信结果。")
    return {"items": items, "total": count, "page": actual_page, "page_size": actual_size, "total_pages": (count + actual_size - 1) // actual_size if count else 0, "source": source}


def fetch_arc_page(mid: str, page: int, keyword: str, page_size: int, owner_name: str) -> dict:
    cookie = request_cookie()
    status, payload = arc_search_once(mid, page, keyword, page_size, cookie=cookie)
    if status != 412 and payload.get("code") not in {-352, -412, -799}:
        bili_error(status, payload, "获取投稿")
        return arc_page_result(status, payload, page, page_size, owner_name, "arc-wbi")

    # 新版 WBI 明确拒绝时只切换一次已验证过的旧版官方接口；不对 -352/-412 无限重试或堆叠风控参数。
    LOG.warning("新版投稿接口被拒绝 status=%s code=%s，切换旧版投稿接口", status, payload.get("code"))
    legacy_status, legacy_payload = legacy_arc_search_once(mid, page, keyword, page_size, cookie=cookie)
    legacy_code = legacy_payload.get("code")
    if legacy_status == 412 or legacy_code in {-352, -412, -799}:
        if payload.get("code") == -799 or legacy_code == -799:
            raise ValueError("已登录，但 B 站暂时限制了投稿查询；已尝试可验证的数据源，暂时无法获得可信投稿列表，请稍后再试。")
        raise ValueError("已登录，但 B 站拒绝了本次投稿请求；已尝试全部可验证的数据源，暂无可信投稿列表。")
    bili_error(legacy_status, legacy_payload, "获取投稿备用数据")
    return arc_page_result(legacy_status, legacy_payload, page, page_size, owner_name, "arc-legacy")


def fetch_all_arc_items(mid: str, keyword: str, page_size: int, owner_name: str) -> tuple[list[dict], int]:
    first = fetch_arc_page(mid, 1, keyword, page_size, owner_name)
    all_items = list(first["items"])
    total_pages = first["total_pages"]
    for page in range(2, total_pages + 1):
        all_items.extend(fetch_arc_page(mid, page, keyword, page_size, owner_name)["items"])
    unique: dict[str, dict] = {item["bvid"]: item for item in all_items}
    return list(unique.values()), first["total"]


def fetch_up_page(value: str, page: int = 1, period: str = "all", keyword: str = "", page_size: int = 30) -> dict:
    mid = parse_up_mid(value)
    profile = up_profile(mid)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 30))
    keyword = keyword.strip()
    period = period if period in {"all", "year", "half_year", "quarter"} else "all"
    if period == "all":
        result = fetch_arc_page(mid, page, keyword, page_size, profile["name"])
        profile["total"] = result["total"]
        return {"profile": profile, "items": result["items"], "total": result["total"], "page": result["page"], "page_size": result["page_size"], "total_pages": result["total_pages"], "source": result["source"]}
    cache_key = (mid, period, keyword.casefold())
    if cache_key not in UP_FILTER_CACHE:
        all_items, _ = fetch_all_arc_items(mid, keyword, page_size, profile["name"])
        cutoff = up_period_cutoff(period) or 0
        UP_FILTER_CACHE[cache_key] = [item for item in all_items if not item["pub_ts"] or item["pub_ts"] >= cutoff]
    filtered = UP_FILTER_CACHE[cache_key]
    total = len(filtered)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    profile["total"] = total
    return {"profile": profile, "items": filtered[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": total_pages, "source": "arc-filtered"}


def normalize_archive_item(item: dict, fallback_name: str) -> dict | None:
    normalized = normalize_arc_item(item, fallback_name)
    if normalized:
        normalized["duration"] = int(item.get("duration") or normalized["duration"] or 0)
        normalized["duration_text"] = format_duration(normalized["duration"])
    return normalized


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def fetch_collections(value: str, page: int = 1, page_size: int = 30) -> dict:
    mid = parse_up_mid(value)
    profile = up_profile(mid)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 20))
    query = {"mid": mid, "page_num": page, "page_size": page_size, "web_location": "333.1387", "x-bili-locale-json": WEB_LOCALE, "x-bili-device-req-json": WEB_DEVICE}
    status, payload = bilibili_json(BILIBILI_SEASONS_API + "?" + urllib.parse.urlencode(query), referer=f"https://space.bilibili.com/{mid}/video")
    bili_error(status, payload, "获取合集和系列")
    data = payload.get("data") or {}
    lists = data.get("items_lists") or {}
    page_info = lists.get("page") or {}
    items = []
    for kind, raw_items in (("season", lists.get("seasons_list") or []), ("series", lists.get("series_list") or [])):
        for raw in raw_items:
            meta = raw.get("meta") or raw
            collection_id = str(meta.get("season_id") if kind == "season" else meta.get("series_id") or meta.get("id") or "")
            if not collection_id:
                continue
            archives = raw.get("archives") or []
            first_video = archives[0] if archives and isinstance(archives[0], dict) else {}
            cover = str(first_video.get("pic") or meta.get("cover") or "").replace("http://", "https://")
            items.append({"kind": kind, "id": collection_id, "name": meta.get("name") or meta.get("title") or "未命名合集", "cover": cover, "total": int(meta.get("total") or 0)})
    total = int(page_info.get("total") or len(items))
    actual_size = int(page_info.get("page_size") or page_size)
    actual_page = int(page_info.get("page_num") or page)
    profile["collection_total"] = total
    return {"profile": profile, "items": items, "total": total, "page": actual_page, "page_size": actual_size, "total_pages": (total + actual_size - 1) // actual_size if total else 0}


def fetch_collection_detail(value: str, kind: str, collection_id: str, page: int = 1, page_size: int = 30) -> dict:
    mid = parse_up_mid(value)
    profile = up_profile(mid)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 30))
    if kind == "season":
        query = {"mid": mid, "season_id": collection_id, "sort_reverse": "false", "page_num": page, "page_size": page_size, "web_location": "333.1387", "x-bili-locale-json": WEB_LOCALE, "x-bili-device-req-json": WEB_DEVICE}
        endpoint = BILIBILI_SEASON_ARCHIVES_API
    elif kind == "series":
        query = {"mid": mid, "series_id": collection_id, "only_normal": "true", "sort": "desc", "pn": page, "ps": page_size}
        endpoint = BILIBILI_SERIES_ARCHIVES_API
    else:
        raise ValueError("合集类型不正确，请重新打开合集列表。")
    status, payload = bilibili_json(endpoint + "?" + urllib.parse.urlencode(query), referer=f"https://space.bilibili.com/{mid}/video")
    bili_error(status, payload, "获取合集视频")
    data = payload.get("data") or {}
    raw_page = data.get("page") or {}
    archives = data.get("archives") or []
    items = [normalized for raw in archives if (normalized := normalize_archive_item(raw, profile["name"]))]
    total = int(raw_page.get("total") or len(data.get("aids") or archives))
    actual_size = int(raw_page.get("page_size") or raw_page.get("ps") or page_size)
    actual_page = int(raw_page.get("page_num") or raw_page.get("pn") or page)
    meta = data.get("meta") or {}
    return {"profile": profile, "collection": {"kind": kind, "id": collection_id, "name": meta.get("name") or meta.get("title") or "合集视频"}, "items": items, "total": total, "page": actual_page, "page_size": actual_size, "total_pages": (total + actual_size - 1) // actual_size if total else 0}


def short_error(value: object) -> str:
    text = redact(value or "下载未完成")
    return text[:100] + ("…" if len(text) > 100 else "")


def flush_log_handlers() -> None:
    for handler in LOG.handlers:
        try:
            handler.flush()
        except OSError:
            pass


def reveal_diagnostics_in_explorer(target: Path) -> bool:
    try:
        subprocess.Popen(
            ["explorer.exe", f"/select,{target}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except OSError as error:
        LOG.warning("无法在资源管理器中定位诊断包：%s", error)
        try:
            os.startfile(LOG_DIR)  # type: ignore[attr-defined]
        except OSError as directory_error:
            LOG.warning("无法打开日志目录：%s", directory_error)
        return False


class Handler(SimpleHTTPRequestHandler):
    server_version = "BilibiliDownloader/0.1"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("网页请求：" + fmt, *args)

    def send_json(self, data: object, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def handle_manual_transcription_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 2 * 1024 * 1024 * 1024:
            raise ValueError("音频文件超过 2 GB，暂不支持转写。")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("请选择要转写的本地音频文件。")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", "") or not getattr(file_item, "file", None):
            raise ValueError("请选择要转写的本地音频文件。")
        filename = Path(str(file_item.filename)).name
        if Path(filename).suffix.lower() not in {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac"}:
            raise ValueError("请选择 M4A、MP3、WAV、FLAC、OGG 或 AAC 音频文件。")
        provider, _ = transcription_provider_settings()
        if provider != "local" and not load_mimo_api_key():
            raise ValueError("当前已选择 MiMo，但尚未配置 API Key。请到“设置”中保存并测试。")
        source = {"url": "", "title": filename, "pages": [], "all_pages": True}
        task = new_transcription_task(source)
        temp_dir = transcription_root() / task["task_id"]
        temp_dir.mkdir(parents=True, exist_ok=True)
        audio_path = temp_dir / filename
        try:
            with audio_path.open("wb") as target:
                while chunk := file_item.file.read(1024 * 1024):
                    target.write(chunk)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            with TRANSCRIPTION_LOCK:
                TRANSCRIPTION_TASKS.pop(task["task_id"], None)
            try:
                (transcription_history_dir() / f"{task['task_id']}.json").unlink(missing_ok=True)
            except OSError:
                LOG.warning("无法清理未提交的转写历史 task_id=%s", task["task_id"])
            raise ValueError("无法保存本地音频文件。")
        threading.Thread(target=run_manual_transcription, args=(task["task_id"], audio_path, temp_dir), daemon=True).start()
        self.send_json({"item": public_transcription(task)}, HTTPStatus.ACCEPTED)

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/api/health":
                config = read_config()
                current_disk_build = disk_build_id()
                self.send_json({
                    "app": APP_NAME,
                    "online": True,
                    "build_id": BUILD_ID,
                    "running_build_id": BUILD_ID,
                    "disk_build_id": current_disk_build,
                    "stale": bool(current_disk_build and current_disk_build != BUILD_ID),
                    "instance_id": getattr(self.server, "instance_id", ""),
                    "download_dir": config.get("download_dir", ""),
                    "dependency_problem": SERVICE.dependency_problem(),
                })
            elif path == "/api/cover":
                cover_url = urllib.parse.parse_qs(parsed_url.query).get("url", [""])[0]
                body, content_type = get_cover(cover_url)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/login/qr.svg":
                value = urllib.parse.parse_qs(parsed_url.query).get("url", [""])[0]
                with LOGIN_LOCK:
                    expected = str(LOGIN_STATE.get("qr_url") or "")
                if not value or value != expected:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = qr_svg(value)
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/config":
                self.send_json(read_config())
            elif path == "/api/transcription/settings":
                self.send_json(public_transcription_settings())
            elif path == "/api/login/status":
                with LOGIN_LOCK:
                    pending = bool(LOGIN_STATE.get("qrcode_key"))
                    pending_status = str(LOGIN_STATE.get("status") or "")
                result = account_status()
                if not result["logged_in"] and pending:
                    result["status"] = pending_status
                self.send_json(result)
            elif path == "/api/tasks":
                if not SERVICE.download_dir:
                    SERVICE.download_dir = read_config().get("download_dir", "")
                if not SERVICE.download_dir:
                    self.send_json({"running": [], "finished": []})
                else:
                    _, result = SERVICE.request("/api/v1/tasks")
                    self.send_json(result)
            elif path == "/api/transcriptions":
                with TRANSCRIPTION_LOCK:
                    tasks = [public_transcription(task) for task in TRANSCRIPTION_TASKS.values()]
                tasks.sort(key=lambda task: str(task.get("created_at") or ""))
                self.send_json({"items": tasks})
            elif path == "/api/meeting/status":
                self.send_json(MEETING_BROWSER.status())
            else:
                self.serve_static(path)
        except (RuntimeError, ValueError) as error:
            self.send_json({"error": short_error(error)}, 400)
        except Exception as error:
            LOG.exception("处理 GET 失败：%s", error)
            self.send_json({"error": "发生意外错误，请导出诊断日志查看详情。"}, 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            cancel_match = re.fullmatch(r"/api/transcriptions/([a-f0-9]{24})/cancel", path)
            if cancel_match:
                try:
                    self.send_json({"item": cancel_transcription(cancel_match.group(1))})
                except KeyError as error:
                    self.send_json({"error": short_error(error)}, HTTPStatus.NOT_FOUND)
                except ValueError as error:
                    self.send_json({"error": short_error(error)}, HTTPStatus.CONFLICT)
                return
            if path == "/api/transcriptions/manual":
                self.handle_manual_transcription_upload()
                return
            data = self.read_json()
            if path == "/api/select-directory":
                selected = select_folder()
                if not selected:
                    self.send_json({"cancelled": True})
                    return
                Path(selected).mkdir(parents=True, exist_ok=True)
                config = read_config()
                config["download_dir"] = selected
                save_config(config)
                ok, message = SERVICE.ensure_started(selected)
                self.send_json({"download_dir": selected, "ready": ok, "error": message})
            elif path == "/api/config":
                selected = str(data.get("download_dir", "")).strip()
                if not selected:
                    raise ValueError("下载目录不能为空。")
                Path(selected).mkdir(parents=True, exist_ok=True)
                config = read_config()
                config["download_dir"] = selected
                save_config(config)
                ok, message = SERVICE.ensure_started(selected)
                self.send_json({"download_dir": selected, "ready": ok, "error": message})
            elif path == "/api/transcription/settings":
                provider = str(data.get("provider") or "local")
                if provider not in TRANSCRIPTION_PROVIDERS:
                    raise ValueError("请选择有效的转写引擎。")
                api_key = str(data.get("api_key") or "").strip()
                if provider != "local":
                    if api_key and not re.fullmatch(r"sk-[A-Za-z0-9._-]{6,}", api_key):
                        raise ValueError("MiMo API Key 格式应为 sk- 开头。")
                    api_key = api_key or load_mimo_api_key()
                    if not api_key:
                        raise ValueError("请填写 MiMo API Key。")
                    test_mimo_api_key(api_key, provider)
                    save_mimo_api_key(api_key)
                config = read_config()
                config["transcription_provider"] = provider
                save_config(config)
                self.send_json(public_transcription_settings())
            elif path == "/api/login/start":
                self.send_json(start_login())
            elif path == "/api/login/poll":
                self.send_json(poll_login())
            elif path == "/api/login/logout":
                clear_auth_cookie()
                with LOGIN_LOCK:
                    LOGIN_STATE.clear()
                self.send_json({"logged_in": False, "status": "未登录"})
            elif path == "/api/meeting/login/start":
                self.send_json(MEETING_BROWSER.start_login(), HTTPStatus.ACCEPTED)
            elif path == "/api/meeting/logout":
                self.send_json(MEETING_BROWSER.logout())
            elif path == "/api/meeting/parse":
                self.send_json(MEETING_BROWSER.parse(str(data.get("url") or "")), HTTPStatus.ACCEPTED)
            elif path == "/api/parse":
                if not BBDOWN.exists():
                    raise ValueError("缺少 BBDownNext：请重新双击“启动工具.vbs”自动准备依赖。")
                self.send_json(video_metadata(str(data.get("url", "")).strip()))
            elif path == "/api/up":
                self.send_json(fetch_up_page(
                    str(data.get("input", "")).strip(),
                    int(data.get("page", 1) or 1),
                    str(data.get("period", "all")).strip(),
                    str(data.get("keyword", "")).strip(),
                    int(data.get("page_size", 30) or 30),
                ))
            elif path == "/api/up/collections":
                self.send_json(fetch_collections(
                    str(data.get("input", "")).strip(),
                    int(data.get("page", 1) or 1),
                    int(data.get("page_size", 30) or 30),
                ))
            elif path == "/api/up/collection":
                self.send_json(fetch_collection_detail(
                    str(data.get("input", "")).strip(),
                    str(data.get("kind", "")).strip(),
                    str(data.get("collection_id", "")).strip(),
                    int(data.get("page", 1) or 1),
                    int(data.get("page_size", 30) or 30),
                ))
            elif path == "/api/shutdown":
                MEETING_BROWSER.shutdown()
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == "/api/restart":
                disk_id = disk_build_id()
                if not disk_id or disk_id == BUILD_ID:
                    self.send_json({"ok": True, "restarting": False, "build_id": BUILD_ID})
                    return
                scheduled = schedule_restart()
                self.send_json({"ok": True, "restarting": True, "scheduled": scheduled, "disk_build_id": disk_id})
            elif path == "/api/transcriptions":
                raw_items = data.get("items")
                if not isinstance(raw_items, list) or not raw_items:
                    raise ValueError("请至少选择一个视频。")
                created = []
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    url = str(raw.get("url") or "").strip()
                    if not url:
                        continue
                    pages = [str(page) for page in raw.get("pages", []) if str(page).isdigit()]
                    if not pages and not bool(raw.get("all_pages")):
                        raise ValueError("转写任务没有可用的分 P。")
                    source = {"url": url, "title": str(raw.get("title") or "未命名视频"), "pages": pages, "all_pages": bool(raw.get("all_pages"))}
                    created.append(start_transcription(source))
                if not created:
                    raise ValueError("没有可用的视频地址。")
                self.send_json({"items": created}, HTTPStatus.ACCEPTED)
            elif path == "/api/tasks":
                pages = [str(page) for page in data.get("pages", []) if str(page).isdigit()]
                all_pages = bool(data.get("all_pages"))
                if not pages and not all_pages:
                    raise ValueError("请至少选择一个分 P。")
                mode = data.get("mode")
                if mode not in {"video", "audio"}:
                    raise ValueError("请选择视频 MP4 或仅音频 M4A。")
                url = str(data.get("url", "")).strip()
                if not url:
                    raise ValueError("下载地址不能为空。")
                request_data = {
                    "url": url,
                    "pages": ",".join(pages) if pages else "all",
                    "content": "avmC" if mode == "video" else "a",
                    "mux": "mpeg4",
                    "dfnPriority": "1080P 高码率,1080P60,1080P+,1080P",
                    "maxRetry": 3,
                }
                status, result = SERVICE.request("/api/v1/tasks", "POST", request_data)
                self.send_json(result, status)
            elif re.fullmatch(r"/api/tasks/[^/]+/stop", path):
                task_id = urllib.parse.quote(path.split("/")[3], safe="")
                status, result = SERVICE.request(f"/api/v1/tasks/{task_id}/stop", "POST", {})
                self.send_json(result, status)
            elif path == "/api/open-download-directory":
                directory = read_config().get("download_dir", "")
                if not directory:
                    raise ValueError("请先选择下载目录。")
                os.startfile(directory)  # type: ignore[attr-defined]
                self.send_json({"ok": True})
            elif path == "/api/open-log-directory":
                os.startfile(LOG_DIR)  # type: ignore[attr-defined]
                self.send_json({"ok": True})
            elif path == "/api/export-logs":
                flush_log_handlers()
                target = LOG_DIR / f"{APP_NAME}-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for log_file in LOG_DIR.glob("*.log"):
                        archive.write(log_file, log_file.name)
                reveal_diagnostics_in_explorer(target)
                self.send_json({"path": str(target)})
            else:
                self.send_json({"error": "未找到接口。"}, 404)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            self.send_json({"error": short_error(error)}, 400)
        except Exception as error:
            LOG.exception("处理 POST 失败：%s", error)
            if path in {"/api/select-directory", "/api/config"}:
                self.send_json({"error": "目录设置失败，请检查目录权限后重试；仍失败时请导出诊断日志。"}, 500)
            else:
                self.send_json({"error": "发生意外错误，请导出诊断日志查看详情。"}, 500)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path != "/api/transcriptions":
                self.send_json({"error": "未找到接口。"}, 404)
                return
            data = self.read_json()
            clear_all = data.get("clear_all") is True
            task_ids = data.get("task_ids")
            if not clear_all and (not isinstance(task_ids, list) or not task_ids or not all(isinstance(item, str) for item in task_ids)):
                raise ValueError("请先选择要删除的转写任务。")
            result = delete_transcription_history(task_ids if isinstance(task_ids, list) else None, clear_all=clear_all)
            self.send_json(result)
        except (RuntimeError, ValueError) as error:
            self.send_json({"error": short_error(error)}, 400)
        except Exception as error:
            LOG.exception("删除转写历史失败：%s", error)
            self.send_json({"error": "删除转写历史失败，请稍后重试。"}, 500)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = self.guess_type(str(candidate)) or "application/octet-stream"
        raw = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--data-dir", help="仅供自动化测试使用的配置与日志目录")
    parser.add_argument("--instance-id", default="", help="启动器用于确认当前本地服务已就绪")
    args = parser.parse_args()
    configure_data_dir(args.data_dir)
    global LOG
    LOG = setup_logging()
    load_transcription_history()
    config = read_config()
    SERVICE.download_dir = config.get("download_dir", "")
    server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
    server.instance_id = args.instance_id  # type: ignore[attr-defined]
    LOG.info("本地页面启动：http://127.0.0.1:%s", APP_PORT)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{APP_PORT}")).start()
    try:
        server.serve_forever()
    finally:
        MEETING_BROWSER.shutdown()
        SERVICE.stop()


if __name__ == "__main__":
    main()
