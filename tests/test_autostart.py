"""M8：开机自启（Windows 注册表 Run 项 / macOS LaunchAgent / Linux XDG autostart）。

平台与外部副作用全部可注入：registry（Windows）、runner（launchctl）、home（家目录）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wequota.autostart import (
    AutostartError,
    AutostartManager,
    default_launch_command,
)


class FakeRegistry:
    """模拟 winreg 的 HKCU Run 项读写。"""

    def __init__(self):
        self.values: dict[str, str] = {}

    def set_value(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete_value(self, name: str) -> None:
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def get_value(self, name: str) -> str:
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]


class FakeRunner:
    def __init__(self, rc: int = 0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        return self.rc


# ---------- 启动命令构造 ----------

def test_default_launch_command_dev_uses_python_module():
    cmd = default_launch_command(frozen=False, executable="/usr/bin/python3")
    assert cmd == ["/usr/bin/python3", "-m", "wequota.cli"]


def test_default_launch_command_frozen_uses_exe_only():
    cmd = default_launch_command(frozen=True, executable="C:\\Program Files\\WeQuota\\WeQuota.exe")
    assert cmd == ["C:\\Program Files\\WeQuota\\WeQuota.exe"]


# ---------- Windows ----------

def test_windows_install_writes_run_key():
    reg = FakeRegistry()
    m = AutostartManager(command=["C:\\WeQuota\\WeQuota.exe"], platform_name="windows",
                         registry=reg)
    result = m.install()
    assert result.ok is True
    assert reg.values["WeQuota"] == '"C:\\WeQuota\\WeQuota.exe"'
    assert "注册表" in result.message or "Run" in result.message


def test_windows_status_and_uninstall():
    reg = FakeRegistry()
    m = AutostartManager(command=["C:\\WeQuota\\WeQuota.exe"], platform_name="windows",
                         registry=reg)
    assert m.status().enabled is False
    m.install()
    assert m.status().enabled is True
    assert m.uninstall().ok is True
    assert m.status().enabled is False


def test_windows_uninstall_when_absent_is_ok():
    reg = FakeRegistry()
    m = AutostartManager(command=["x"], platform_name="windows", registry=reg)
    result = m.uninstall()
    assert result.ok is True          # 幂等：没装过也不报错


# ---------- macOS ----------

def test_macos_install_writes_launchagent_plist(tmp_path: Path):
    runner = FakeRunner()
    m = AutostartManager(command=["/Applications/WeQuota.app/Contents/MacOS/WeQuota"],
                         platform_name="darwin", home=tmp_path, runner=runner)
    result = m.install()
    assert result.ok is True
    plist = tmp_path / "Library" / "LaunchAgents" / "com.wequota.agent.plist"
    assert plist.exists()
    content = plist.read_text(encoding="utf-8")
    assert "com.wequota.agent" in content
    assert "<key>RunAtLoad</key>" in content
    assert "/Applications/WeQuota.app/Contents/MacOS/WeQuota" in content
    # 加载 LaunchAgent
    assert runner.calls and runner.calls[0][0] == "launchctl"
    assert str(plist) in runner.calls[0]


def test_macos_status_and_uninstall(tmp_path: Path):
    runner = FakeRunner()
    m = AutostartManager(command=["/usr/local/bin/wequota"], platform_name="darwin",
                         home=tmp_path, runner=runner)
    assert m.status().enabled is False
    m.install()
    assert m.status().enabled is True
    plist = tmp_path / "Library" / "LaunchAgents" / "com.wequota.agent.plist"
    assert m.uninstall().ok is True
    assert not plist.exists()
    assert any("unload" in call for call in runner.calls)


def test_macos_uninstall_when_absent_is_ok(tmp_path: Path):
    m = AutostartManager(command=["x"], platform_name="darwin", home=tmp_path,
                         runner=FakeRunner())
    assert m.uninstall().ok is True


# ---------- Linux（开发机冒烟用，非 v1 目标平台） ----------

def test_linux_install_writes_xdg_desktop(tmp_path: Path):
    m = AutostartManager(command=["/home/u/wequota", "--minimized"],
                         platform_name="linux", home=tmp_path)
    result = m.install()
    assert result.ok is True
    desktop = tmp_path / ".config" / "autostart" / "wequota.desktop"
    assert desktop.exists()
    content = desktop.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in content
    assert "Exec=/home/u/wequota --minimized" in content
    assert "X-GNOME-Autostart-enabled=true" in content


def test_linux_status_and_uninstall(tmp_path: Path):
    m = AutostartManager(command=["/usr/bin/wequota"], platform_name="linux", home=tmp_path)
    assert m.status().enabled is False
    m.install()
    assert m.status().enabled is True
    assert m.uninstall().ok is True
    assert m.status().enabled is False


# ---------- 不支持的平台 ----------

def test_unsupported_platform_raises():
    m = AutostartManager(command=["x"], platform_name="freebsd")
    with pytest.raises(AutostartError):
        m.install()


def test_command_with_spaces_is_quoted_windows():
    reg = FakeRegistry()
    m = AutostartManager(command=["C:\\Program Files\\WeQuota\\WeQuota.exe"],
                         platform_name="windows", registry=reg)
    m.install()
    assert reg.values["WeQuota"].startswith('"')
