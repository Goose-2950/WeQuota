"""wechat-cli 安装与检测（M7）。

**合规红线（勿越）**：本模块只做「调用外部 `wechat-cli` 做安装/版本检测」，
不读取、不解析、不解密任何微信数据。实时采集见 `collector/huohuoer.py`。

安装源（重要）：PyPI 上的裸包名 `wechat-cli` 现为他人项目（Frida 注入、可发消息），
与上游无关。本项目统一从 GitHub 安装：完整 commit 用 archive zip，分支/tag 用 git ref。
Windows 微信 4.1+ 使用 SarielChin/wechat-cli 的 0.3.0（已合并上游 PR #22 的
Windows Config.Cipher 运行时密钥扫描）；macOS 亦可用 npm `@canghe_ai/wechat-cli`
（按版本锁定）。
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

CLI_NAME = "wechat-cli"
# PyPI 上的裸包名 `wechat-cli` 已被他人占用（现为 Frida 注入实现，可发消息、更激进），
# 必须从源码/git 安装，禁止按裸包名 pip install wechat-cli。
# 0.3.0 fork 支持 Windows 微信 4.1+；上游 huohuoer 0.2.4 只到 4.1.8.x。
GIT_URL = "git+https://github.com/SarielChin/wechat-cli.git"
# 锁 commit 时优先用 GitHub archive zip：pip 的 git clone --filter=blob:none
# 在部分网络会失败，而 zip 只有几 MB，下载/构建都更稳。
GIT_ARCHIVE_URL = "https://github.com/SarielChin/wechat-cli/archive/{ref}.zip"
PIP_PACKAGE = GIT_URL
NPM_PACKAGE = "@canghe_ai/wechat-cli"
SUPPORTED_PLATFORMS = ("windows", "darwin", "linux")

# npm 版本 + `--version` 比对用；pip 安装按 commit 锁定（见 PINNED_CLI_COMMIT）。
PINNED_CLI_VERSION = "0.3.0"
PINNED_CLI_COMMIT = "800b01a158a9b8dd4ace0f1cc55c1a901eb5ed5c"
# 版本/分支不锁的特殊值
LATEST = "latest"

# 自动安装要从 GitHub 下载/构建，30 秒的普通命令超时不够；单独给安装留足时间。
INSTALL_TIMEOUT = 600.0


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _git_spec(ref: str | None) -> str:
    """安装规格；完整 commit 用 archive zip，其余用 git ref（分支/tag/latest）。"""
    if not ref or ref == LATEST:
        return GIT_URL
    if _COMMIT_RE.match(ref):
        return GIT_ARCHIVE_URL.format(ref=ref)
    return f"{GIT_URL}@{ref}"


def _npm_spec(version: str | None) -> str:
    if not version or version == LATEST:
        return NPM_PACKAGE
    return f"{NPM_PACKAGE}@{version}"


def manual_install_hint(version: str | None = PINNED_CLI_VERSION,
                        ref: str | None = PINNED_CLI_COMMIT) -> str:
    """手动安装指引（pip 锁 commit，npm 锁版本，方便用户照抄）。"""
    return (
        f"pip install {_git_spec(ref)}"
        f"（macOS 也可 npm install -g {_npm_spec(version)}；"
        f"请勿安装 PyPI 上的同名包 wechat-cli）"
    )


MANUAL_INSTALL_HINT = manual_install_hint()


class WechatCliError(RuntimeError):
    """调用外部 wechat-cli 失败（未安装 / 未登录 / 输出异常）。"""


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], CommandResult | tuple[int, str, str]]


def run_command(cmd: list[str], timeout: float = 30.0) -> CommandResult:
    """默认命令执行器：任何异常都转成非 0 退出码，不让主流程崩。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError:
        return CommandResult(127, "", f"命令不存在：{cmd[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"命令超时：{' '.join(cmd)}")
    except OSError as exc:  # 权限不足 / 被安全软件拦截等
        return CommandResult(126, "", str(exc))


def current_platform() -> str:
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        return "windows"
    if sysname == "darwin":
        return "darwin"
    return "linux"


class CliStatus:
    """wechat-cli 的检测结果。"""

    def __init__(
        self,
        installed: bool,
        path: str | None = None,
        version: str | None = None,
        source: str | None = None,
        platform: str = "",
        message: str = "",
        expected_version: str | None = None,
    ) -> None:
        self.installed = installed
        self.path = path
        self.version = version
        self.source = source          # preinstalled / npm / pip
        self.platform = platform
        self.message = message
        # 锁定的版本（None = 未锁）；version_matched 只在安装了且能解析版本号时才有意义
        self.expected_version = expected_version

    @property
    def version_matched(self) -> bool | None:
        """安装的版本与锁定版本是否一致；任一侧拿不到版本号则为 None（无法判断）。"""
        if not self.expected_version or self.expected_version == LATEST or not self.version:
            return None
        return self.version == self.expected_version

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"CliStatus(installed={self.installed}, version={self.version}, "
                f"source={self.source}, platform={self.platform})")

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed, "path": self.path, "version": self.version,
            "source": self.source, "platform": self.platform, "message": self.message,
            "expected_version": self.expected_version,
        }


def _parse_version(text: str) -> str | None:
    match = re.search(r"\d+(?:\.\d+)+", text or "")
    return match.group(0) if match else None


def install_plan(platform_name: str,
                 version: str | None = PINNED_CLI_VERSION,
                 python_executable: str | None = None,
                 ref: str | None = PINNED_CLI_COMMIT) -> list[list[str]]:
    """按平台返回依次尝试的安装命令；空列表表示不支持自动安装。

    - 所有平台：pip 从 GitHub 安装 SarielChin/wechat-cli；完整 commit 用
      archive zip（更稳），分支/tag/latest 用 git ref
    - macOS：优先 npm 预编译二进制 `@canghe_ai/wechat-cli`，失败回退 pip

    `ref` 为空或 `latest` 表示跟踪默认分支；`version` 仅用于 npm 版本锁定。
    """
    py = python_executable or sys.executable
    pip_cmd = [py, "-m", "pip", "install", "--upgrade"]
    pip_spec = _git_spec(ref)
    if platform_name == "darwin":
        return [
            ["npm", "install", "-g", _npm_spec(version)],
            pip_cmd + [pip_spec],
        ]
    return [pip_cmd + [pip_spec]]


def version_note(version: str | None, expected: str | None) -> str | None:
    """版本与锁定版本不一致时的提示文案；一致或无法判断则 None。"""
    if not version or not expected or expected == LATEST:
        return None
    if version == expected:
        return None
    return (f"当前 {version} 与锁定版本 {expected} 不一致，"
            f"如需切换请执行：{manual_install_hint(expected)}")


def detect_cli(
    runner: Runner | None = None,
    platform_name: str | None = None,
    binary: str | None = None,
    expected_version: str | None = PINNED_CLI_VERSION,
) -> CliStatus:
    """检测 wechat-cli 是否已安装可用（顺带比对锁定版本）。"""
    runner = runner or run_command
    name = binary or CLI_NAME
    platform_label = platform_name or current_platform()
    rc, out, err = runner([name, "--version"])
    if rc != 0:
        detail = (err or out or "").strip().splitlines()
        return CliStatus(
            False, platform=platform_label, expected_version=expected_version,
            message=detail[0] if detail else f"未检测到 {name}",
        )
    version = _parse_version(out)
    message = f"{name} {version}" if version else f"{name} 已就绪"
    note = version_note(version, expected_version)
    if note:
        message = f"{message}（{note}）"
    return CliStatus(
        True, path=name, version=version, source="preinstalled", platform=platform_label,
        message=message, expected_version=expected_version,
    )


def ensure_cli(
    runner: Runner | None = None,
    platform_name: str | None = None,
    auto_install: bool = True,
    binary: str | None = None,
    cli_version: str | None = None,
    cli_ref: str | None = None,
    python_executable: str | None = None,
) -> CliStatus:
    """检测 wechat-cli；缺失则按平台自动安装（双通道，失败给手动指引）。

    `cli_version`：npm 版本 / `--version` 比对；None/"" = `PINNED_CLI_VERSION`。
    `cli_ref`：git commit/tag/branch；None/"" = `PINNED_CLI_COMMIT`，
    `latest` = 跟踪默认分支。
    """
    using_default_runner = runner is None
    runner = runner or run_command
    python_executable = python_executable or sys.executable
    platform_label = platform_name or current_platform()
    version = PINNED_CLI_VERSION if not cli_version else cli_version
    ref = PINNED_CLI_COMMIT if not cli_ref else cli_ref
    detect_binary = binary or _environment_cli_binary(python_executable, platform_label)
    status = detect_cli(runner=runner, platform_name=platform_label, binary=detect_binary,
                        expected_version=version)
    if status.installed or not auto_install:
        return status

    plan = install_plan(platform_label, version, python_executable=python_executable,
                        ref=ref)
    if not plan:
        status.message = (
            f"v1 暂不支持在 {platform_label} 上自动安装 {CLI_NAME}（仅 Windows/macOS）；"
            f"请手动安装后重启：{manual_install_hint(version, ref)}"
        )
        return status

    errors: list[str] = []
    for cmd in plan:
        # 自定义 runner（测试/宿主注入）保持单参数协议；只有默认执行器才享受
        # 安装专用超时，避免为了装一个 CLI 把普通 contacts/history 调用也拖长。
        if using_default_runner:
            rc, out, err = run_command(cmd, timeout=INSTALL_TIMEOUT)
        else:
            rc, out, err = runner(cmd)
        if rc != 0:
            errors.append(f"{' '.join(cmd)} → {(err or out).strip()[:160] or rc}")
            continue
        status = detect_cli(runner=runner, platform_name=platform_label, binary=detect_binary,
                            expected_version=version)
        if status.installed:
            status.source = "npm" if cmd[0] == "npm" else "pip"
            return status
        errors.append(f"{' '.join(cmd)} → 安装完成但仍不可用")

    status.installed = False
    status.source = None
    status.message = (f"自动安装失败（{' | '.join(errors)}）；"
                      f"请手动安装：{manual_install_hint(version, ref)}")
    return status


def _environment_cli_binary(python_executable: str | None, platform_label: str) -> str:
    """Prefer the CLI script installed beside the selected Python interpreter."""
    if not python_executable:
        return CLI_NAME
    root = Path(python_executable).resolve().parent
    # venv 里的 python 本来就在 Scripts/bin 下，不能再拼一层
    # （否则会得到 ...\Scripts\Scripts\wechat-cli.exe，装完检测不到）。
    if platform_label == "windows":
        script_dir = root if root.name.lower() == "scripts" else root / "Scripts"
        return str(script_dir / f"{CLI_NAME}.exe")
    script_dir = root if root.name == "bin" else root / "bin"
    # The path is deterministic before installation, so detection after pip
    # completes cannot accidentally fall back to another interpreter's PATH.
    return str(script_dir / CLI_NAME)
