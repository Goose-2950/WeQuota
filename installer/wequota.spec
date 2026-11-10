# -*- mode: python ; coding: utf-8 -*-
"""WeQuota PyInstaller 打包配置（Windows / macOS）。

内置资源：
- wequota/webapp/         前端构建产物（先跑 npm run build）
- assets/tiktoken_cache/  tiktoken 离线编码表（断网可计量）
- assets/models/          faster-whisper 模型（可选，存在才内置）

构建：python -m installer.build_release  （或手动 pyinstaller installer/wequota.spec）
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821  SPECPATH 由 PyInstaller 注入

datas = [
    (str(ROOT / "wequota" / "webapp"), "wequota/webapp"),
    (str(ROOT / "assets" / "tiktoken_cache"), "assets/tiktoken_cache"),
]
# whisper 模型较大（~80MB），已下载才打进包里；未下载时运行时走 hf-mirror 拉取
models_dir = ROOT / "assets" / "models"
if models_dir.exists():
    datas.append((str(models_dir), "assets/models"))

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(ROOT / "installer" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir：避免 onefile 每次启动解压到 /tmp（慢且吃临时盘空间）
    name="WeQuota",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 在 macOS 上常引发 Gatekeeper 误报，不用
    console=True,       # 保留控制台：banner 与大屏地址要打印在这里
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="WeQuota",
)

# macOS 额外产出 .app 包（Gatekeeper 更友好，双击即可启动）。
# Windows / Linux 只保留 onedir 目录。
if sys.platform == "darwin":  # noqa: SIM108
    app = BUNDLE(
        coll,
        name="WeQuota.app",
        icon=None,
        bundle_identifier="cn.wequota.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSUIElement": False,          # 保留 Dock 图标：控制台窗口要能看到
            "NSAppleEventsUsageDescription": "用于发送本地桌面通知与读取本机微信数据",
        },
    )
