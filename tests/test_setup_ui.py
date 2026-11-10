"""M10 启动配置页：logo 居中 + 分组选项 + 方向键编辑 + 编号降级。"""

from __future__ import annotations

import json

import pytest

from wequota.setup_ui import KEY_CTRL_C, KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_UP, \
    KEY_BACKSPACE, ConsoleIO, Field, SetupScreen, SetupResult, default_values, \
    format_hours, load_values, parse_hours, render_numbered, run_numbered, run_setup
from wequota import cli as cli_module


class FakeIO:
    """喂按键 / 喂输入行的假终端；把每次重绘都记下来供断言。"""

    def __init__(self, keys=None, lines=None, width: int = 100) -> None:
        self.keys = list(keys or [])
        self.lines = list(lines or [])
        self.frames: list[str] = []
        self.output: list[str] = []
        self.width = width

    def read_key(self) -> str:
        return self.keys.pop(0) if self.keys else KEY_CTRL_C

    def read_line(self, prompt: str = "") -> str:
        self.output.append(prompt)
        return self.lines.pop(0) if self.lines else ""

    def read_secret(self, prompt: str = "") -> str:
        return self.read_line(prompt)

    def write(self, text: str = "") -> None:
        self.output.append(text)
        if text.strip():
            self.frames.append(text)

    def clear(self) -> None:
        pass

    @property
    def last(self) -> str:
        return self.frames[-1] if self.frames else ""


class FakeAutostart:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[str] = []

    def status(self):
        class S:
            pass
        s = S()
        s.enabled = self.enabled
        s.detail = "fake"
        return s

    def install(self):
        self.calls.append("install")
        self.enabled = True
        return type("R", (), {"ok": True, "message": "ok"})()

    def uninstall(self):
        self.calls.append("uninstall")
        self.enabled = False
        return type("R", (), {"ok": True, "message": "ok"})()


def row_index(screen: SetupScreen, label: str) -> int:
    for i, row in enumerate(screen.rows()):
        if row.label == label:
            return i
    raise AssertionError(f"找不到行：{label}（现有：{[r.label for r in screen.rows()]}）")


def goto(screen: SetupScreen, label: str) -> None:
    screen.cursor = row_index(screen, label)


def make_screen(io: FakeIO | None = None, **kw) -> SetupScreen:
    return SetupScreen(default_values(), io or FakeIO(), color=False,
                       autostart=kw.get("autostart"), test_mail=kw.get("test_mail"))


# ---------- 版面 ----------

def test_logo_is_centered_above_the_options():
    screen = make_screen(FakeIO(width=100))
    frame = screen.render()
    lines = frame.splitlines()
    logo_lines = [l for l in lines if "█" in l]
    assert logo_lines, "应渲染 logo"
    first_padding = len(logo_lines[0]) - len(logo_lines[0].lstrip())
    assert first_padding > 0                                  # 居中才有缩进
    assert logo_lines[0].strip().startswith("█")
    assert frame.index("█") < frame.index("基础")              # logo 在选项上方


def test_advanced_group_is_collapsed_until_expanded():
    io = FakeIO()
    screen = make_screen(io)
    assert "默认日额度" not in screen.render()
    assert "高级设置" in screen.render()
    goto(screen, "高级设置（默认额度 / 采集 / 转写 / 自启）")
    screen.handle_key(KEY_ENTER)
    assert "默认日额度" in screen.render()


def test_current_values_are_shown():
    screen = make_screen()
    frame = screen.render()
    assert "wxid_demo" in frame          # 微信号当前值
    assert "✓ 开启" in frame             # 桌面通知默认开


def test_save_row_is_last():
    screen = make_screen()
    assert screen.rows()[-1].kind == "save"


# ---------- 键盘导航 ----------

def test_arrow_keys_move_the_highlight():
    io = FakeIO()
    screen = make_screen(io)
    first = screen.cursor
    screen.handle_key(KEY_DOWN)
    assert screen.cursor != first
    screen.handle_key(KEY_UP)
    assert screen.cursor == first


def test_enter_on_toggle_flips_value():
    screen = make_screen()
    goto(screen, "桌面通知")
    before = screen.values["alert.desktop_enabled"]
    screen.handle_key(KEY_ENTER)
    assert screen.values["alert.desktop_enabled"] is not before


def test_text_field_edits_value():
    screen = make_screen()
    goto(screen, "微信号")
    screen.handle_key(KEY_ENTER)                       # 进入编辑
    for ch in "wxid_me":
        screen.handle_key(ch)
    screen.handle_key(KEY_ENTER)                       # 确认
    assert screen.values["collector.account_id"] == "wxid_me"


def test_backspace_and_escape_cancel_edit():
    screen = make_screen()
    goto(screen, "微信号")
    screen.handle_key(KEY_ENTER)
    for ch in "abc":
        screen.handle_key(ch)
    screen.handle_key(KEY_BACKSPACE)
    assert screen.buffer == "ab"
    screen.handle_key(KEY_ESC)
    assert screen.editing is None
    assert screen.values["collector.account_id"] != "abc"


def test_escape_in_menu_abandons_changes():
    screen = make_screen()
    goto(screen, "桌面通知")
    screen.handle_key(KEY_ENTER)
    result = screen.handle_key(KEY_ESC)
    assert result is not None and result.saved is False


def test_ctrl_c_also_abandons():
    screen = make_screen()
    result = screen.handle_key(KEY_CTRL_C)
    assert result is not None and result.saved is False


def test_invalid_number_input_is_rejected():
    screen = make_screen()
    goto(screen, "提醒冷却")
    screen.handle_key(KEY_ENTER)
    for ch in "abc":
        screen.handle_key(ch)
    screen.handle_key(KEY_ENTER)
    assert screen.values["alert.cooldown_seconds"] == 300
    assert screen.error


# ---------- 免打扰时段 ----------

def test_parse_hours_supports_crossing_midnight():
    assert parse_hours("23-7") == [23, 7]
    assert parse_hours("") is None
    assert parse_hours("25-7") is None
    assert parse_hours("abc") is None
    assert format_hours([23, 7]) == "23-7"
    assert format_hours(None) == "不启用"


def test_quiet_hours_edit_and_clear():
    screen = make_screen()
    goto(screen, "免打扰时段")
    screen.handle_key(KEY_ENTER)
    for ch in "23-7":
        screen.handle_key(ch)
    screen.handle_key(KEY_ENTER)
    assert screen.values["alert.quiet_hours"] == [23, 7]
    screen.handle_key(KEY_ENTER)
    screen.handle_key(KEY_ENTER)          # 空输入 = 清空
    assert screen.values["alert.quiet_hours"] is None


# ---------- 邮件子菜单 ----------

def _enter_email(screen: SetupScreen) -> None:
    goto(screen, "邮件提醒")
    screen.handle_key(KEY_ENTER)
    assert screen.submenu is not None


def test_email_submenu_fills_smart_defaults():
    screen = make_screen()
    _enter_email(screen)
    goto(screen, "邮箱账号")
    screen.handle_key(KEY_ENTER)
    for ch in "me@qq.com":
        screen.handle_key(ch)
    screen.handle_key(KEY_ENTER)
    assert screen.values["alert.email_from"] == "me@qq.com"
    assert screen.values["alert.email_to"] == ["me@qq.com"]


def test_email_submenu_back_row_returns_to_main():
    screen = make_screen()
    _enter_email(screen)
    goto(screen, "← 返回")
    screen.handle_key(KEY_ENTER)
    assert screen.submenu is None


def test_secret_field_is_masked_in_display():
    f = Field("alert.smtp_password", "密码", "secret", secret=True)
    assert f.display("") == "未配置"
    assert f.display("secret") == "••••••"


def test_test_mail_is_sent_only_when_confirmed():
    sent: list[object] = []

    def fake_send(cfg):
        sent.append(cfg)
        return True, "测试邮件已发送"

    io = FakeIO(lines=["y", ""])
    screen = make_screen(io, test_mail=fake_send)
    screen.values.update({
        "alert.email_enabled": True, "alert.smtp_host": "smtp.qq.com",
        "alert.smtp_user": "me@qq.com", "alert.email_to": ["me@qq.com"],
    })
    _enter_email(screen)
    goto(screen, "← 返回")
    screen.handle_key(KEY_ENTER)
    assert len(sent) == 1
    assert any("测试邮件已发送" in line for line in io.output)


def test_test_mail_skipped_when_declined():
    sent: list[object] = []

    def fake_send(cfg):
        sent.append(cfg)
        return True, "已发送"

    io = FakeIO(lines=["n"])
    screen = make_screen(io, test_mail=fake_send)
    screen.values.update({"alert.email_enabled": True, "alert.smtp_host": "smtp.qq.com",
                          "alert.email_to": ["me@qq.com"]})
    _enter_email(screen)
    goto(screen, "← 返回")
    screen.handle_key(KEY_ENTER)
    assert sent == []


# ---------- 保存 ----------

def test_save_writes_all_sections(tmp_path):
    screen = make_screen()
    screen.values["collector.account_id"] = "wxid_me"
    screen.values["quota.default_limit_tokens"] = 40000
    screen.values["alert.quiet_hours"] = [23, 7]
    goto(screen, "保存并启动")
    result = screen.handle_key(KEY_ENTER)
    assert result is not None and result.saved is True
    cfg = tmp_path / "config.json"
    screen.save(cfg)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["collector"]["account_id"] == "wxid_me"
    assert data["quota"]["default_limit_tokens"] == 40000
    assert data["alert"]["quiet_hours"] == [23, 7]
    assert "metering" in data                 # 首次保存补一份完整默认配置
    assert "autostart" not in data            # 自启不落 config


def test_abandon_does_not_write(tmp_path):
    screen = make_screen()
    cfg = tmp_path / "config.json"
    result = screen.handle_key(KEY_CTRL_C)
    assert result is not None and result.saved is False
    assert not cfg.exists()


def test_save_merges_existing_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"metering": {"voice_fallback_rate": 30.0}}), encoding="utf-8")
    screen = SetupScreen(load_values(cfg), FakeIO(), color=False)
    screen.values["whisper.model_size"] = "base"
    screen.save(cfg)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["metering"]["voice_fallback_rate"] == 30.0     # 未动的段保留
    assert data["whisper"]["model_size"] == "base"


def test_autostart_toggle_only_when_changed():
    manager = FakeAutostart(enabled=False)
    screen = make_screen(autostart=manager)
    assert screen.values["autostart"] is False
    screen.values["autostart"] = True
    screen._apply_autostart()
    assert manager.calls == ["install"]
    screen._apply_autostart()                 # 状态没再变，不重复动系统
    assert manager.calls == ["install"]


def test_autostart_initial_state_reads_from_manager():
    manager = FakeAutostart(enabled=True)
    screen = make_screen(autostart=manager)
    assert screen.values["autostart"] is True


# ---------- 编号降级 ----------

def test_numbered_menu_renders_indices():
    io = FakeIO(lines=[""])
    screen = SetupScreen(default_values(), io, color=False)
    result = run_numbered(screen)
    assert result.saved is True
    assert "[1]" in "\n".join(io.output)


def test_numbered_menu_edits_field():
    io = FakeIO(lines=["1", "wxid_numbered", ""])
    screen = SetupScreen(default_values(), io, color=False)
    result = run_numbered(screen)
    assert result.saved is True
    assert screen.values["collector.account_id"] == "wxid_numbered"


def test_numbered_menu_quit_abandons():
    io = FakeIO(lines=["q"])
    screen = SetupScreen(default_values(), io, color=False)
    assert run_numbered(screen).saved is False


def test_run_setup_falls_back_to_numbered(tmp_path):
    io = FakeIO(lines=[""])
    cfg = tmp_path / "config.json"
    result = run_setup(cfg, io=io, interactive=False)
    assert result.saved is True
    assert cfg.exists()
    assert any("编号菜单" in line for line in io.output)


# ---------- CLI 接线 ----------

def test_handle_setup_falls_back_to_legacy_wizard(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"

    def boom(path, **kw):
        raise RuntimeError("终端不支持")

    monkeypatch.setattr(cli_module, "run_wizard",
                        lambda path, io=None, platform_name=None: type(
                            "R", (), {"ok": True, "message": "legacy"})())
    saved, message = cli_module.handle_setup(cfg, screen_factory=boom)
    assert saved is True and message == "legacy"


def test_handle_setup_reports_saved(tmp_path):
    cfg = tmp_path / "config.json"

    def factory(path, **kw):
        return SetupResult(True, "配置已保存", {})

    saved, _ = cli_module.handle_setup(cfg, screen_factory=factory)
    assert saved is True


def test_run_loop_saves_and_redraws(tmp_path):
    """主循环：喂一次回车落在「保存并启动」上就应落盘并退出循环。"""
    io = FakeIO(keys=[KEY_ENTER])
    screen = SetupScreen(default_values(), io, color=False)
    goto(screen, "保存并启动")
    result = screen.run()
    assert result.saved is True
    assert io.frames                       # 至少重绘过一帧


def test_console_io_width_has_sane_floor():
    assert ConsoleIO().width >= 60
