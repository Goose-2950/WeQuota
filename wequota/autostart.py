"""M8：开机自启管理。

- Windows：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 注册表项
- macOS：~/Library/LaunchAgents/com.wequota.agent.plist + launchctl
- Linux：~/.config/autostart/wequota.desktop（XDG autostart，非 v1 目标平台，便于开发机验证）

副作用全部可注入：registry（Windows 注册表）、runner（launchctl 等外部命令）、
home（家目录），测试不起真进程、不碰真注册表。
"""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

APP_NAME = "WeQuota"
MACOS_LABEL = "com.wequota.agent"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class AutostartError(Exception):
    """平台不支持或自启操作失败。"""


class RegistryLike(Protocol):
    def set_value(self, name: str, value: str) -> None: ...
    def delete_value(self, name: str) -> None: ...
    def get_value(self, name: str) -> str: ...


@dataclass
class AutostartResult:
    ok: bool
    message: str
    path: str | None = None


@dataclass
class AutostartStatus:
    enabled: bool
    platform: str
    detail: str = ""


def default_launch_command(frozen: bool | None = None,
                           executable: str | None = None) -> list[str]:
    """生成开机启动命令：打包版直接起 exe，源码版用 python -m wequota.cli。"""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    exe = executable or sys.executable
    if frozen:
        return [exe]
    return [exe, "-m", "wequota.cli"]


class _WinRegistry:
    """winreg 薄封装（仅 Windows 可用，import 延迟到实例化）。"""

    def __init__(self) -> None:
        import winreg  # type: ignore[import-not-found]
        self._winreg = winreg

    def set_value(self, name: str, value: str) -> None:
        key = self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                   self._winreg.KEY_SET_VALUE)
        with key:
            self._winreg.SetValueEx(key, name, 0, self._winreg.REG_SZ, value)

    def delete_value(self, name: str) -> None:
        key = self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                   self._winreg.KEY_SET_VALUE)
        with key:
            self._winreg.DeleteValue(key, name)

    def get_value(self, name: str) -> str:
        key = self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                   self._winreg.KEY_READ)
        with key:
            value, _ = self._winreg.QueryValueEx(key, name)
            return str(value)


def _default_runner(cmd: list[str]) -> int:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        return proc.returncode
    except OSError:
        return 127


def _quote_cmd(command: list[str]) -> str:
    return " ".join(f'"{c}"' for c in command)


@dataclass
class AutostartManager:
    command: list[str]
    platform_name: str | None = None
    home: Path | None = None
    registry: RegistryLike | None = None
    runner: Callable[[list[str]], int] | None = None
    app_name: str = APP_NAME
    _platform: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        p = (self.platform_name or platform.system()).lower()
        if p.startswith("win"):
            p = "windows"
        elif p in ("darwin", "mac", "macos", "osx"):
            p = "darwin"
        self._platform = p
        self.home = self.home or Path.home()
        self.runner = self.runner or _default_runner

    # ---------- 对外接口 ----------

    def install(self) -> AutostartResult:
        if self._platform == "windows":
            return self._install_windows()
        if self._platform == "darwin":
            return self._install_macos()
        if self._platform == "linux":
            return self._install_linux()
        raise AutostartError(f"不支持在 {self._platform} 上设置开机自启（v1 仅 Windows/macOS）")

    def uninstall(self) -> AutostartResult:
        if self._platform == "windows":
            return self._uninstall_windows()
        if self._platform == "darwin":
            return self._uninstall_macos()
        if self._platform == "linux":
            return self._uninstall_linux()
        raise AutostartError(f"不支持在 {self._platform} 上设置开机自启（v1 仅 Windows/macOS）")

    def status(self) -> AutostartStatus:
        if self._platform == "windows":
            try:
                self._registry().get_value(self.app_name)
                return AutostartStatus(True, self._platform, "注册表 Run 项已写入")
            except (FileNotFoundError, OSError):
                return AutostartStatus(False, self._platform, "注册表 Run 项不存在")
        if self._platform == "darwin":
            plist = self._plist_path()
            return AutostartStatus(plist.exists(), self._platform, str(plist))
        if self._platform == "linux":
            desktop = self._desktop_path()
            return AutostartStatus(desktop.exists(), self._platform, str(desktop))
        return AutostartStatus(False, self._platform, "不支持的平台")

    # ---------- Windows ----------

    def _registry(self) -> RegistryLike:
        if self.registry is None:
            self.registry = _WinRegistry()
        return self.registry

    def _install_windows(self) -> AutostartResult:
        self._registry().set_value(self.app_name, _quote_cmd(self.command))
        return AutostartResult(True, f"已写入注册表 Run 项（HKCU\\...\\Run\\{self.app_name}）",
                               path=RUN_KEY)

    def _uninstall_windows(self) -> AutostartResult:
        try:
            self._registry().delete_value(self.app_name)
        except (FileNotFoundError, OSError):
            pass  # 幂等：没装过也算成功
        return AutostartResult(True, "已删除注册表 Run 项", path=RUN_KEY)

    # ---------- macOS ----------

    def _plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"

    def _plist_xml(self) -> str:
        args = "\n".join(f"        <string>{c}</string>" for c in self.command)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""

    def _install_macos(self) -> AutostartResult:
        plist = self._plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(self._plist_xml(), encoding="utf-8")
        rc = self.runner(["launchctl", "load", "-w", str(plist)])
        note = "" if rc == 0 else "（launchctl 加载失败，重启后仍会生效）"
        return AutostartResult(True, f"已写入 LaunchAgent{note}", path=str(plist))

    def _uninstall_macos(self) -> AutostartResult:
        plist = self._plist_path()
        if plist.exists():
            self.runner(["launchctl", "unload", "-w", str(plist)])
            plist.unlink()
        return AutostartResult(True, "已移除 LaunchAgent", path=str(plist))

    # ---------- Linux（XDG autostart） ----------

    def _desktop_path(self) -> Path:
        return self.home / ".config" / "autostart" / "wequota.desktop"

    def _install_linux(self) -> AutostartResult:
        desktop = self._desktop_path()
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={self.app_name}\n"
            f"Exec={' '.join(self.command)}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "NoDisplay=true\n",
            encoding="utf-8",
        )
        return AutostartResult(True, "已写入 XDG autostart", path=str(desktop))

    def _uninstall_linux(self) -> AutostartResult:
        desktop = self._desktop_path()
        if desktop.exists():
            desktop.unlink()
        return AutostartResult(True, "已移除 XDG autostart 项", path=str(desktop))
