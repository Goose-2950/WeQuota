"""M8：首次启动向导。

检测到没有 config.json 时，引导用户：
1. 填写微信号 account_id（数据隔离的键）
2. 选择是否启用邮件提醒（开启后写入占位 SMTP 配置，需后续补全）
3. 打印 wechat-cli init 权限指引（Windows 管理员 / macOS 完全磁盘访问）

IO 可注入（prompt / print），CLI 用真实 input/print，测试用假 IO。
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import dump_default_config

DEFAULT_ACCOUNT_ID = "wxid_me"


class WizardIO(Protocol):
    def prompt(self, text: str) -> str: ...
    def print(self, text: str = "") -> None: ...


@dataclass
class WizardResult:
    ok: bool
    message: str
    account_id: str = ""
    email_enabled: bool = False


def needs_first_run(config_path: str | Path) -> bool:
    """没有配置文件 = 首次启动。"""
    return not Path(config_path).exists()


def init_guidance(platform_name: str | None = None) -> str:
    """wechat-cli init 的平台化权限指引。"""
    p = (platform_name or platform.system()).lower()
    lines = [
        "下一步：初始化 wechat-cli（读取本机微信数据所需，仅首次需要）",
        "  1. 确认 PC 微信已登录",
        "  2. 执行：wechat-cli init",
    ]
    if p.startswith("win"):
        lines.append("  ※ Windows 需要以【管理员身份】运行终端，才能读取微信进程内存中的密钥")
    elif p == "darwin":
        lines.append("  ※ macOS 需要在「系统设置 → 隐私与安全性 → 完全磁盘访问」中授权终端")
    lines.append("  ※ wechat-cli 只读本地数据、不碰微信协议，密钥与数据全程不出本机")
    return "\n".join(lines)


def run_wizard(config_path: str | Path, io: WizardIO,
               platform_name: str | None = None) -> WizardResult:
    """执行首启向导；已有配置时拒绝覆盖。"""
    cfg = Path(config_path)
    if cfg.exists():
        return WizardResult(False, f"配置文件已存在：{cfg}，如需重置请先手动删除")

    io.print("=" * 56)
    io.print("  欢迎使用 WeQuota！检测到首次启动，花 30 秒完成初始化。")
    io.print("=" * 56)

    account = io.prompt("① 你的微信号 ID（wxid_ 开头，直接回车用 wxid_me）：").strip()
    if not account:
        account = DEFAULT_ACCOUNT_ID

    email_answer = io.prompt("② 是否启用邮件提醒？需自备 SMTP（y/N）：").strip().lower()
    email_enabled = email_answer in ("y", "yes")

    # 先落一份完整默认配置，再打补丁，保证新增字段不丢
    dump_default_config(cfg)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data.setdefault("collector", {})["account_id"] = account
    if email_enabled:
        email = data.setdefault("alert", {}).setdefault("email", {})
        email["enabled"] = True
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    io.print("")
    io.print(f"  ✓ 配置已写入 {cfg}")
    if email_enabled:
        io.print("  · 邮件提醒已开启：请稍后在 config.json 的 alert.email 段补全 SMTP 信息")
    io.print("")
    io.print(init_guidance(platform_name))
    io.print("")

    return WizardResult(True, "初始化完成", account_id=account, email_enabled=email_enabled)
