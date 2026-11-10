"""M8：资源路径与数据目录（PyInstaller frozen 兼容）。

- 开发模式：资源在仓库根（wequota 包上一级），数据在 ./data
- 打包模式（PyInstaller）：资源在 sys._MEIPASS，数据落到平台用户目录
  （Windows %APPDATA%/WeQuota，macOS ~/Library/Application Support/WeQuota，
  Linux ~/.local/share/wequota），避免往安装目录写文件
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

APP_DIR_NAME = "WeQuota"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """只读资源根目录：打包版 = sys._MEIPASS，开发版 = 仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def resource_path(rel: str | Path) -> Path:
    """取随包资源（webapp / tiktoken_cache / 内置模型）的绝对路径。"""
    return bundle_dir() / rel


def data_dir(frozen: bool | None = None, platform_name: str | None = None,
             home: Path | None = None) -> Path:
    """可写数据目录：开发版沿用 ./data；打包版用平台用户数据目录。

    `home` 显式传入时优先按它推算（测试/多用户隔离用）；未传时 Windows 仍以
    `%APPDATA%` 为准（可能被组策略重定向，不能想当然用 `Path.home()`）。
    """
    if frozen is None:
        frozen = is_frozen()
    if not frozen:
        return bundle_dir() / "data"
    p = (platform_name or platform.system()).lower()
    if p.startswith("win"):
        if home is not None:
            base = home / "AppData" / "Roaming"
        else:
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_DIR_NAME
    home = home or Path.home()
    if p == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    return home / ".local" / "share" / "wequota"


def webapp_dir() -> Path:
    """前端构建产物目录（打包版在 _MEIPASS/wequota/webapp）。"""
    return resource_path("wequota") / "webapp"
