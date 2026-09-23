"""Bilibili Downloader 的本地网页入口；下载核心由 BBDownNext 提供。"""

from __future__ import annotations

import argparse
import html
import json
import hashlib
import logging
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
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "BilibiliDownloader"
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TOOLS_DIR = ROOT / "tools"
BBDOWN = TOOLS_DIR / "BBDownNext" / "BBDown.exe"
DEFAULT_APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
APP_DATA = Path(os.environ.get("BILIBILI_DOWNLOADER_DATA_DIR", DEFAULT_APP_DATA))
CONFIG_FILE = APP_DATA / "config.json"
LOG_DIR = APP_DATA / "logs"
APP_PORT = 23666
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ALLOWED_COVER_SUFFIX = ".hdslb.com"
BUILD_FILES = ("app.py", "static/index.html", "static/app.js", "static/styles.css")
BILIBILI_SPACE_API = "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
BILIBILI_ARC_SEARCH_API = "https://api.bilibili.com/x/space/wbi/arc/search"
BILIBILI_SEASONS_API = "https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
BILIBILI_SEASON_ARCHIVES_API = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
BILIBILI_SERIES_ARCHIVES_API = "https://api.bilibili.com/x/series/archives"
BILIBILI_PROFILE_API = "https://api.bilibili.com/x/web-interface/card"
BILIBILI_SPACE_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,"
    "decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,avatarAutoTheme,"
    "cardsEnhance,eva3CardOpus,eva3CardVideo,eva3CardComment,eva3CardUser"
)
WBI_MIXIN_KEY_TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
WBI_MIXIN_KEY = ""
UP_FILTER_CACHE: dict[tuple[str, str, str], list[dict]] = {}


def redact(value: object) -> str:
    """避免诊断日志意外记录 Cookie、token 或带敏感查询参数的链接。"""
    text = str(value)
    text = re.sub(r"(?i)(cookie|token|sessdata|access_token|refresh_token)([=:])[^\s,&;]+", r"\1\2<已脱敏>", text)
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


def current_build_id() -> str:
    digest = hashlib.sha256()
    for relative in BUILD_FILES:
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


BUILD_ID = current_build_id()


def configure_data_dir(data_dir: str | None) -> None:
    """测试可指定隔离目录，正常启动仍只使用当前用户的 AppData。"""
    global APP_DATA, CONFIG_FILE, LOG_DIR
    if data_dir:
        APP_DATA = Path(data_dir).expanduser().resolve()
        CONFIG_FILE = APP_DATA / "config.json"
        LOG_DIR = APP_DATA / "logs"


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
            return "缺少 BBDownNext：请先双击“安装依赖.ps1”。"
        if not self.ffmpeg_available():
            return "缺少 FFmpeg：请先双击“安装依赖.ps1”。"
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

    def ensure_started(self, download_dir: str) -> tuple[bool, str]:
        problem = self.dependency_problem()
        if problem:
            return False, problem
        # 配置切换和前端轮询可能并发到达；以磁盘中的最新配置为准，避免旧轮询把服务切回旧目录。
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
        ok, message = self.ensure_started(self.download_dir)
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


def bilibili_json(url: str, *, referer: str = "https://www.bilibili.com/", extra_headers: dict | None = None) -> tuple[int, dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Origin": "https://space.bilibili.com",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with LOCAL_OPENER.open(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            LOG.warning("B 站接口 HTTP %s：%s", error.code, body[:500])
            return error.code, {}
    except urllib.error.URLError as error:
        raise ValueError("无法连接 B 站获取投稿，请检查网络后重试。") from error


ANONYMOUS_COOKIE = ""


def anonymous_cookie() -> str:
    global ANONYMOUS_COOKIE
    if ANONYMOUS_COOKIE:
        return ANONYMOUS_COOKIE
    status, payload = bilibili_json("https://api.bilibili.com/x/frontend/finger/spi")
    data = payload.get("data") or {}
    pairs = [f"{cookie_name}={data[data_name]}" for cookie_name, data_name in (("buvid3", "b_3"), ("buvid4", "b_4")) if data.get(data_name)]
    if status != 200 or not pairs:
        raise ValueError("B 站匿名访问参数暂时不可用，请稍后重试。")
    ANONYMOUS_COOKIE = "; ".join(pairs)
    return ANONYMOUS_COOKIE


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
        raise ValueError("B 站暂时要求安全验证，无法获取该 UP 主投稿。")
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
        extra_headers={"Cookie": anonymous_cookie()},
    )
    data = payload.get("data") or {}
    # 该公开接口偶尔会对带签名请求返回 code=0 但空列表；再尝试一次兼容的无签名请求。
    if payload.get("code") == 0 and not (data.get("items") or {}):
        fallback_status, fallback_payload = bilibili_json(
            BILIBILI_SPACE_API + "?" + urllib.parse.urlencode(query),
            referer=f"https://space.bilibili.com/{mid}",
            extra_headers={"Cookie": anonymous_cookie()},
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
        raise ValueError(f"B 站暂时要求安全验证，无法{action}，请稍后重试。")
    if code == -799:
        raise ValueError("B 站请求过于频繁，请稍后重试。")
    if code != 0:
        LOG.warning("B 站%s失败 status=%s code=%s message=%s", action, status, code, payload.get("message"))
        raise ValueError(f"B 站{action}失败，请稍后重试。")


def fetch_arc_page(mid: str, page: int, keyword: str, page_size: int, owner_name: str) -> dict:
    query = {
        "mid": mid, "pn": page, "ps": page_size, "order": "pubdate", "keyword": keyword,
        "platform": "web", "web_location": "1550101", "order_avoided": "true",
        "dm_img_list": "[]",
        "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
        "dm_cover_img_str": "QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgUlRYIDIwNjAgKDB4MDAwMDFGMDgpIERpcmVjdDNEMTEgdnNfNV8wIHBzXzVfMCwgRDNEMTEpR29vZ2xlIEluYy4gKE5WSURJQ",
        "dm_img_inter": json.dumps({"ds": [], "wh": [5715, 6540, 43], "of": [335, 670, 335]}, separators=(",", ":")),
    }
    status, payload = 0, {}
    for attempt in range(3):
        signed_url = BILIBILI_ARC_SEARCH_API + "?" + wbi_signed_query(query)
        status, payload = bilibili_json(
            signed_url,
            referer=f"https://space.bilibili.com/{mid}/video",
            extra_headers={"Cookie": anonymous_cookie(), "Accept": "application/json, text/plain, */*"},
        )
        if status != 412 and payload.get("code") not in {-352, -412, -799}:
            break
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    bili_error(status, payload, "获取投稿")
    data = payload.get("data") or {}
    page_info = data.get("page") or {}
    items = [normalized for raw in (data.get("list") or {}).get("vlist", []) if (normalized := normalize_arc_item(raw, owner_name))]
    count = int(page_info.get("count") or 0)
    actual_page = int(page_info.get("pn") or page)
    actual_size = int(page_info.get("ps") or page_size)
    return {"items": items, "total": count, "page": actual_page, "page_size": actual_size, "total_pages": (count + actual_size - 1) // actual_size if count else 0}


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
        return {"profile": profile, "items": result["items"], "total": result["total"], "page": result["page"], "page_size": result["page_size"], "total_pages": result["total_pages"], "source": "arc"}
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
    query = {"mid": mid, "page_num": page, "page_size": page_size, "web_location": "333.999"}
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
            items.append({"kind": kind, "id": collection_id, "name": meta.get("name") or meta.get("title") or "未命名合集", "cover": str(meta.get("cover") or "").replace("http://", "https://"), "total": int(meta.get("total") or 0)})
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
        query = {"mid": mid, "season_id": collection_id, "sort_reverse": "false", "page_num": page, "page_size": page_size, "web_location": "333.999"}
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

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/api/health":
                config = read_config()
                self.send_json({
                    "app": APP_NAME,
                    "online": True,
                    "build_id": BUILD_ID,
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
            elif path == "/api/config":
                self.send_json(read_config())
            elif path == "/api/tasks":
                if not SERVICE.download_dir:
                    SERVICE.download_dir = read_config().get("download_dir", "")
                if not SERVICE.download_dir:
                    self.send_json({"running": [], "finished": []})
                else:
                    _, result = SERVICE.request("/api/v1/tasks")
                    self.send_json(result)
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
            elif path == "/api/parse":
                if not BBDOWN.exists():
                    raise ValueError("缺少 BBDownNext：请先双击“安装依赖.ps1”。")
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
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
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
                target = APP_DATA / f"{APP_NAME}-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for log_file in LOG_DIR.glob("*.log"):
                        archive.write(log_file, log_file.name)
                os.startfile(target)  # type: ignore[attr-defined]
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
        SERVICE.stop()


if __name__ == "__main__":
    main()
