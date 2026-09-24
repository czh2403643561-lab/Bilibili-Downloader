"""Bilibili Downloader 与启动器共用的版本文件清单。"""

from __future__ import annotations

import hashlib
from pathlib import Path


# 只包含客户端启动与页面文件；asr-service 的模型/服务代码不参与客户端版本。
BUILD_FILES = (
    "build_info.py",
    "app.py",
    "meeting_browser.py",
    "meeting_bridge.py",
    "启动工具.pyw",
    "static/index.html",
    "static/app.js",
    "static/asr.js",
    "static/styles.css",
)


def current_build_id(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in BUILD_FILES:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
