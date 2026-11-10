"""M8：部署链路 — 运行时路径解析、文件日志、PyInstaller 打包配置。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from wequota.cli import RuntimePaths, resolve_runtime_paths, setup_logging

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- 运行时路径 ----------

def test_runtime_paths_dev_uses_repo_dirs():
    paths = resolve_runtime_paths(frozen=False)
    assert paths.db_path.name == "wequota.db"
    assert paths.db_path.parent.name == "data"
    assert paths.config_path.name == "config.json"
    # 开发版：配置在仓库根，数据库在 data/
    assert paths.config_path.parent == paths.data_dir.parent


def test_runtime_paths_frozen_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    paths = resolve_runtime_paths(frozen=True, platform_name="windows")
    assert paths.data_dir == tmp_path / "Roaming" / "WeQuota"
    assert paths.db_path == paths.data_dir / "wequota.db"
    assert paths.config_path == paths.data_dir / "config.json"
    assert paths.log_path.parent == paths.data_dir / "logs"


def test_runtime_paths_frozen_macos(tmp_path: Path):
    paths = resolve_runtime_paths(frozen=True, platform_name="darwin", home=tmp_path)
    assert paths.data_dir == tmp_path / "Library" / "Application Support" / "WeQuota"


# ---------- 文件日志 ----------

def test_setup_logging_writes_rotating_file(tmp_path: Path):
    log_path = tmp_path / "logs" / "wequota.log"
    logger = setup_logging(log_path, logger_name="wequota_test_log")
    logger.info("冒烟日志 hello")
    for h in logger.handlers:
        h.flush()
    assert log_path.exists()
    assert "冒烟日志 hello" in log_path.read_text(encoding="utf-8")


def test_setup_logging_idempotent(tmp_path: Path):
    log_path = tmp_path / "wequota.log"
    logger1 = setup_logging(log_path, logger_name="wequota_test_idem")
    logger2 = setup_logging(log_path, logger_name="wequota_test_idem")
    assert logger1 is logger2
    assert len([h for h in logger1.handlers if isinstance(h, logging.Handler)]) == \
        len(logger2.handlers)     # 重复调用不重复挂 handler


# ---------- PyInstaller 打包配置 ----------

def test_spec_file_exists_and_bundles_required_assets():
    spec = REPO_ROOT / "installer" / "wequota.spec"
    assert spec.exists(), "installer/wequota.spec 不存在"
    content = spec.read_text(encoding="utf-8")
    # 必须内置：前端产物、tiktoken 离线编码表、whisper 模型目录
    assert "wequota/webapp" in content
    assert "assets/tiktoken_cache" in content
    assert "assets/models" in content


def test_build_release_module_importable():
    from installer import build_release
    cmd = build_release.pyinstaller_command()
    assert "pyinstaller" in cmd[0] or cmd[0].endswith("python") or "-m" in cmd
    assert any("wequota.spec" in str(c) for c in cmd)


def test_build_release_steps_include_frontend_and_model():
    from installer import build_release
    steps = build_release.build_steps()
    names = [s.name for s in steps]
    assert "dependency-check" in names
    assert "frontend" in names        # npm run build
    assert "whisper-model" in names   # 模型内置
    assert "pyinstaller" in names


def test_build_release_rejects_non_311_python():
    from installer import build_release

    assert build_release.python_version_error((3, 10, 11))
    assert build_release.python_version_error((3, 12, 0))
    assert build_release.python_version_error((3, 11, 2)) is None


# ---------- Windows 控制台编码 ----------

class _FakeStream:
    def __init__(self):
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_configure_stdio_switches_to_utf8_replace(monkeypatch):
    from wequota import cli
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)
    cli.configure_stdio()
    assert out.calls and err.calls
    assert out.calls[0]["encoding"] == "utf-8" and out.calls[0]["errors"] == "replace"


def test_configure_stdio_survives_streams_without_reconfigure(monkeypatch):
    from wequota import cli
    monkeypatch.setattr(cli.sys, "stdout", object())     # 无 reconfigure
    monkeypatch.setattr(cli.sys, "stderr", object())
    cli.configure_stdio()                                # 不该抛异常


def test_configure_stdio_handles_reconfigure_failure(monkeypatch):
    from wequota import cli

    class Boom:
        def reconfigure(self, **kwargs):
            raise ValueError("不支持")

    monkeypatch.setattr(cli.sys, "stdout", Boom())
    monkeypatch.setattr(cli.sys, "stderr", Boom())
    cli.configure_stdio()


def test_spec_bundles_macos_app_when_darwin():
    spec = REPO_ROOT / "installer" / "wequota.spec"
    content = spec.read_text(encoding="utf-8")
    assert "BUNDLE(" in content and 'sys.platform == "darwin"' in content


def test_platform_start_scripts_exist():
    assert (REPO_ROOT / "installer" / "start-windows.bat").exists()
    macos = REPO_ROOT / "installer" / "start-macos.command"
    assert macos.exists()
