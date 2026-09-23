"""Bilibili Downloader 的本地网页入口；下载核心由 BBDownNext 提供。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
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
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DATA / "config.json"
LOG_DIR = APP_DATA / "logs"
APP_PORT = 23666
BBDOWN_PORT = 23667
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


LOG = setup_logging()


def read_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
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


class BBDownService:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.token = ""
        self.download_dir = ""
        self.lock = threading.Lock()

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
        with self.lock:
            if self.process and self.process.poll() is None and self.download_dir == download_dir:
                return True, ""
            self.stop()
            self.token = secrets.token_urlsafe(32)
            command = [
                str(BBDOWN), "serve", "--listen", f"http://127.0.0.1:{BBDOWN_PORT}",
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
                    with LOCAL_OPENER.open(f"http://127.0.0.1:{BBDOWN_PORT}/healthz", timeout=1):
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
            f"http://127.0.0.1:{BBDOWN_PORT}{path}", payload, method=method,
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


def select_folder() -> str:
    command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = '选择默认下载目录'; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }"
    )
    result = run_hidden(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    stdout, _ = result.communicate(timeout=120)
    return stdout.strip()


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
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/health":
                config = read_config()
                self.send_json({"download_dir": config.get("download_dir", ""), "dependency_problem": SERVICE.dependency_problem()})
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
                SERVICE.download_dir = selected
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
                SERVICE.download_dir = selected
                ok, message = SERVICE.ensure_started(selected)
                self.send_json({"download_dir": selected, "ready": ok, "error": message})
            elif path == "/api/parse":
                if not BBDOWN.exists():
                    raise ValueError("缺少 BBDownNext：请先双击“安装依赖.ps1”。")
                self.send_json(video_metadata(str(data.get("url", "")).strip()))
            elif path == "/api/tasks":
                pages = [str(page) for page in data.get("pages", []) if str(page).isdigit()]
                if not pages:
                    raise ValueError("请至少选择一个分 P。")
                mode = data.get("mode")
                if mode not in {"video", "audio"}:
                    raise ValueError("请选择视频 MP4 或仅音频 M4A。")
                request_data = {
                    "url": str(data.get("url", "")).strip(),
                    "pages": ",".join(pages),
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
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    config = read_config()
    SERVICE.download_dir = config.get("download_dir", "")
    server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
    LOG.info("本地页面启动：http://127.0.0.1:%s", APP_PORT)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{APP_PORT}")).start()
    try:
        server.serve_forever()
    finally:
        SERVICE.stop()


if __name__ == "__main__":
    main()
