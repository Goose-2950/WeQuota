"""M8：首次启动向导（生成配置 + wechat-cli init 指引）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wequota.firstrun import needs_first_run, run_wizard


class FakeIO:
    """可注入的输入输出：预设回答队列，记录所有输出。"""

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.out: list[str] = []

    def prompt(self, text: str) -> str:
        self.out.append(text)
        return self.answers.pop(0) if self.answers else ""

    def print(self, text: str = "") -> None:
        self.out.append(text)


def test_needs_first_run_when_config_missing(tmp_path: Path):
    assert needs_first_run(tmp_path / "config.json") is True


def test_no_first_run_when_config_exists(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    assert needs_first_run(cfg) is False


def test_wizard_writes_config_with_account_id(tmp_path: Path):
    cfg = tmp_path / "config.json"
    io = FakeIO(answers=["wxid_alice", "n"])   # 账号 + 不装邮件
    result = run_wizard(cfg, io=io, platform_name="windows")
    assert result.ok is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["collector"]["account_id"] == "wxid_alice"
    # 默认配置的其他段保持完整
    assert "metering" in data and "alert" in data and "whisper" in data


def test_wizard_default_account_when_blank(tmp_path: Path):
    cfg = tmp_path / "config.json"
    io = FakeIO(answers=["", "n"])
    run_wizard(cfg, io=io, platform_name="windows")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["collector"]["account_id"] == "wxid_me"


def test_wizard_email_opt_in_writes_smtp_placeholder(tmp_path: Path):
    cfg = tmp_path / "config.json"
    io = FakeIO(answers=["wxid_bob", "y"])
    run_wizard(cfg, io=io, platform_name="darwin")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["alert"]["email"]["enabled"] is True


def test_wizard_prints_wechat_cli_init_guidance(tmp_path: Path):
    cfg = tmp_path / "config.json"
    io = FakeIO(answers=["wxid_bob", "n"])
    run_wizard(cfg, io=io, platform_name="darwin")
    text = "\n".join(io.out)
    assert "wechat-cli" in text
    assert "init" in text
    # macOS 要提示完全磁盘访问权限
    assert "完全磁盘访问" in text or "磁盘" in text


def test_wizard_windows_guidance_mentions_admin(tmp_path: Path):
    cfg = tmp_path / "config.json"
    io = FakeIO(answers=["", "n"])
    run_wizard(cfg, io=io, platform_name="windows")
    text = "\n".join(io.out)
    assert "管理员" in text


def test_wizard_refuses_to_overwrite_existing_config(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"collector": {"account_id": "wxid_old"}}', encoding="utf-8")
    io = FakeIO(answers=["wxid_new", "n"])
    result = run_wizard(cfg, io=io, platform_name="windows")
    assert result.ok is False
    assert "wxid_old" in cfg.read_text(encoding="utf-8")   # 原配置未被覆盖
