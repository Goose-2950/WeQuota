"""M8：资源路径（PyInstaller frozen 兼容）与平台数据目录。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wequota import resources


def test_is_frozen_false_in_dev():
    assert resources.is_frozen() is False


def test_bundle_dir_is_project_root_in_dev():
    # 开发模式：bundle 目录 = 仓库根（wequota 包的上一级）
    assert resources.bundle_dir() == Path(resources.__file__).resolve().parent.parent


def test_resource_path_resolves_inside_bundle():
    p = resources.resource_path("assets/tiktoken_cache")
    assert p == resources.bundle_dir() / "assets/tiktoken_cache"


def test_frozen_mode_uses_meipass(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.is_frozen() is True
    assert resources.bundle_dir() == tmp_path
    assert resources.resource_path("wequota/webapp") == tmp_path / "wequota/webapp"


def test_data_dir_dev_defaults_to_repo_data():
    d = resources.data_dir(frozen=False, platform_name="windows")
    assert d.name == "data"


def test_data_dir_frozen_windows_uses_appdata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    d = resources.data_dir(frozen=True, platform_name="windows")
    assert d == tmp_path / "Roaming" / "WeQuota"


def test_data_dir_frozen_macos_uses_application_support(tmp_path: Path):
    d = resources.data_dir(frozen=True, platform_name="darwin", home=tmp_path)
    assert d == tmp_path / "Library" / "Application Support" / "WeQuota"


def test_data_dir_frozen_linux_uses_xdg(tmp_path: Path):
    d = resources.data_dir(frozen=True, platform_name="linux", home=tmp_path)
    assert d == tmp_path / ".local" / "share" / "wequota"


def test_webapp_dir_prefers_bundled(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "wequota" / "webapp"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.webapp_dir() == bundled
