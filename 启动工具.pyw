"""Windows 双击启动器：不创建控制台，负责准备依赖并确认本地服务已就绪。"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path


APP_NAME = "Bilibili Downloader"
ROOT = Path(__file__).resolve().parent
APP_DATA = Path(os.environ.get("BILIBILI_DOWNLOADER_DATA_DIR", Path(os.environ.get("APPDATA", Path.home())) / "BilibiliDownloader"))
LOG_DIR = APP_DATA / "logs"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
APP_PORT = 23666
BBDOWN = ROOT / "tools" / "BBDownNext" / "BBDown.exe"
FFMPEG = ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
BBDOWN_URL = "https://github.com/KaiHuaDou/BBDownNext/releases/download/v2.2.0/BBDown-win-x64.exe"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(message: str, exc_info: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("BilibiliDownloaderLauncher")
    if not logger.handlers:
        handler = logging.FileHandler(LAUNCHER_LOG, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    logger.error(message, exc_info=exc_info)


def show_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    except Exception:
        log("无法显示 Windows 消息框：" + message, exc_info=True)


def hidden_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def download_file(url: str, target: Path, label: str) -> None:
    temporary = target.with_suffix(target.suffix + ".download")
    try:
        log(f"开始下载 {label}：{url}")
        with urllib.request.urlopen(url, timeout=30) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(target)
        log(f"下载完成：{label}")
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"网络下载组件失败：{label}。请检查网络后重试。") from error


def prepare_dependencies() -> None:
    (ROOT / "tools" / "BBDownNext").mkdir(parents=True, exist_ok=True)
    (ROOT / "tools" / "ffmpeg" / "bin").mkdir(parents=True, exist_ok=True)
    if not BBDOWN.exists():
        download_file(BBDOWN_URL, BBDOWN, "BBDownNext")
    if FFMPEG.exists():
        return

    zip_path = ROOT / "tools" / "ffmpeg-download.zip"
    extract_path = ROOT / "tools" / "ffmpeg-extract"
    try:
        download_file(FFMPEG_URL, zip_path, "FFmpeg")
        extract_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_path)
        candidates = list(extract_path.glob("*/bin/ffmpeg.exe"))
        if not candidates:
            raise RuntimeError("网络下载组件失败：FFmpeg 压缩包中没有找到 ffmpeg.exe。")
        shutil.copy2(candidates[0], FFMPEG)
        log("FFmpeg 准备完成")
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("网络下载组件失败：FFmpeg。请检查网络后重试。") from error
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(extract_path, ignore_errors=True)


def health() -> dict | None:
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}/api/health")
        with LOCAL_OPENER.open(request, timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def port_is_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", APP_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def start_app() -> None:
    existing = health()
    if existing is not None:
        if existing.get("app") == "BilibiliDownloader" and existing.get("online") is True:
            log("发现已运行实例，直接打开现有页面")
            webbrowser.open(f"http://127.0.0.1:{APP_PORT}/")
            return
        raise RuntimeError("本地端口 23666 已被其他程序占用，无法启动 Bilibili Downloader。")
    if port_is_open():
        raise RuntimeError("本地端口 23666 已被其他程序占用或旧实例异常，请关闭占用该端口的程序后重试。")

    instance_id = uuid.uuid4().hex
    command = [sys.executable, str(ROOT / "app.py"), "--instance-id", instance_id]
    log("启动 app.py")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        creationflags=hidden_flags(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    for _ in range(120):
        time.sleep(0.25)
        current = health()
        if current and current.get("app") == "BilibiliDownloader" and current.get("instance_id") == instance_id:
            log("app.py API 已就绪，打开浏览器")
            webbrowser.open(f"http://127.0.0.1:{APP_PORT}/")
            return
        if process.poll() is not None:
            raise RuntimeError("app.py 启动失败，请查看 launcher.log。")
    raise RuntimeError("本地服务启动超时，请查看 launcher.log。")


def main() -> None:
    log("启动流程开始")
    try:
        prepare_dependencies()
        start_app()
        log("启动流程完成")
    except Exception as error:
        log(f"启动失败：{error}", exc_info=True)
        show_error(str(error) + "\n\n详细信息已写入 launcher.log。")


if __name__ == "__main__":
    main()
