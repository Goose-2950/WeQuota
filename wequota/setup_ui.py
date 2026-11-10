"""M10：启动配置页（MiMo Code 首启风格）。

排布：logo 居中在控制台上方 → 分组配置列表（当前值右对齐）→ 底部按键提示。
交互：↑↓ 移动高亮、Enter 进入编辑/切换开关、编辑中 Esc 取消、菜单中 Esc/Ctrl+C
放弃修改照常启动；只有选中「保存并启动」才写 config.json。

降级：终端读不到单键（非 TTY / 老控制台 / 重定向）时自动切成编号菜单，功能不减。
所有终端能力都用可注入的 IO（`SetupIO`），测试里喂按键序列即可全程无真实终端。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .alerting import AlertConfig, EmailChannel
from .brand import C_DIM, C_MARK, C_TITLE, C_VALUE, LOGO_ART, center_line, display_width, \
    paint, supports_color
from .config import AlertConfig as _AlertConfig, AppConfig, BackupConfig, CollectorConfig, \
    dump_default_config, QuotaConfig, WhisperConfig, _read_config_file
from .quota import AlertEvent, AlertLevel

# 按键名（read_key 返回值）
KEY_UP = "up"
KEY_DOWN = "down"
KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_CTRL_C = "ctrl_c"
KEY_BACKSPACE = "backspace"

SAVE_LABEL = "保存并启动"


class SetupIO(Protocol):
    """配置页需要的终端能力；测试用假实现，真实运行用 ConsoleIO。"""

    def read_key(self) -> str: ...

    def read_line(self, prompt: str) -> str: ...

    def read_secret(self, prompt: str) -> str: ...

    def write(self, text: str = "") -> None: ...

    def clear(self) -> None: ...

    @property
    def width(self) -> int: ...


# ---------------------------------------------------------------- 字段模型

@dataclass
class Field:
    """一个可编辑的配置项。key 是「段.字段」的扁平路径。"""

    key: str
    label: str
    kind: str = "text"          # text / secret / int / number / toggle / hours / choice
    hint: str = ""
    secret: bool = False
    unit: str = ""
    choices: list[str] = field(default_factory=list)
    default: Any = ""

    def display(self, value: Any) -> str:
        if self.kind == "toggle":
            return "✓ 开启" if value else "✗ 关闭"
        if self.kind == "hours":
            return format_hours(value)
        if self.secret:
            return "••••••" if value else "未配置"
        if value in ("", None, []):
            return "未配置"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return f"{value}{self.unit}" if self.unit else str(value)

    def parse(self, raw: str, current: Any) -> tuple[Any, str | None]:
        """把输入解析成配置值；返回 (值, 错误说明)。"""
        text = raw.strip()
        if self.kind == "toggle":
            return current, None
        if self.kind == "hours":
            if not text:
                return None, None
            hours = parse_hours(text)
            return (hours, None) if hours else (current, "格式应为 23-7（起止小时）")
        if self.kind == "int":
            if not text:
                return current, None
            try:
                return int(text), None
            except ValueError:
                return current, "请输入整数"
        if self.kind == "number":
            if not text:
                return current, None
            try:
                return float(text), None
            except ValueError:
                return current, "请输入数字"
        if self.kind == "choice":
            if not text:
                return current, None
            return (text, None) if text in self.choices else (current, f"可选：{'/'.join(self.choices)}")
        if not text:
            return current, None      # 空输入 = 保持原值
        return text, None


@dataclass
class SubMenu:
    """一组字段折叠成主菜单里的一行（邮件那 6 个字段）。"""

    key: str
    label: str
    fields: list[Field]
    hint: str = ""

    def summary(self, values: dict[str, Any]) -> str:
        if self.key == "email" and not values.get("alert.email_enabled"):
            return "已关闭"
        host = values.get("alert.smtp_host", "")
        return host or "未配置"

    @property
    def kind(self) -> str:
        return "submenu"


@dataclass
class Group:
    title: str
    items: list[Any]
    collapsible: bool = False
    summary: str = ""
    expanded: bool = False


@dataclass
class Row:
    kind: str                    # header / field / submenu / group / save
    label: str = ""
    target: Any = None


@dataclass
class SetupResult:
    saved: bool
    message: str
    values: dict[str, Any] = field(default_factory=dict)


def parse_hours(text: str) -> list[int] | None:
    """`23-7` → [23, 7]；支持中文破折号与全角冒号；非法返回 None。"""
    raw = (text or "").strip().replace("—", "-").replace("－", "-").replace("~", "-")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("-") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    return [start, end]


def format_hours(value: Any) -> str:
    if not value or len(value) != 2:
        return "不启用"
    return f"{value[0]}-{value[1]}"


# ---------------------------------------------------------------- 默认值

def default_values() -> dict[str, Any]:
    """从各配置 dataclass 取出默认值。"""
    alert, collector = AlertConfig(), CollectorConfig()
    whisper, quota = WhisperConfig(), QuotaConfig()
    app, backup = AppConfig(), BackupConfig()
    return {
        "collector.account_id": collector.account_id,
        "alert.desktop_enabled": alert.desktop_enabled,
        "alert.email_enabled": alert.email_enabled,
        "alert.smtp_host": alert.smtp_host,
        "alert.smtp_port": alert.smtp_port,
        "alert.smtp_security": alert.smtp_security,
        "alert.smtp_user": alert.smtp_user,
        "alert.smtp_password": alert.smtp_password,
        "alert.email_from": alert.email_from,
        "alert.email_to": list(alert.email_to),
        "alert.cooldown_seconds": alert.cooldown_seconds,
        "alert.quiet_hours": list(alert.quiet_hours) if alert.quiet_hours else None,
        "quota.default_limit_tokens": quota.default_limit_tokens,
        "collector.enabled": collector.enabled,
        "collector.poll_interval_s": collector.poll_interval_s,
        "collector.full_backfill": collector.full_backfill,
        "collector.full_backfill_limit": collector.full_backfill_limit,
        "whisper.enabled": whisper.enabled,
        "whisper.model_size": whisper.model_size,
        "app.allow_mock_ingest": app.allow_mock_ingest,
        "backup.enabled": backup.enabled,
        "autostart": False,
    }


FLAT_KEYS = list(default_values().keys())


def load_values(config_path: str | Path) -> dict[str, Any]:
    """已有 config.json 时用现有值作为默认，否则用代码默认值。"""
    values = default_values()
    data = _read_config_file(config_path)
    for key in FLAT_KEYS:
        if key == "autostart":
            continue
        section, name = key.split(".", 1)
        if name in (data.get(section) or {}):
            values[key] = data[section][name]
    return values


def values_to_config(values: dict[str, Any]) -> dict[str, Any]:
    """扁平值 → 嵌套配置字典（autostart 不落 config，走开机自启管理器）。"""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "autostart":
            continue
        section, name = key.split(".", 1)
        out.setdefault(section, {})[name] = value
    return out


# ---------------------------------------------------------------- 菜单定义

def build_groups() -> list[Group]:
    return [
        Group("基础", [
            Field("collector.account_id", "微信号", "text",
                  hint="wxid_ 开头，多账号数据隔离用的键", default="wxid_me"),
        ]),
        Group("提醒", [
            Field("alert.desktop_enabled", "桌面通知", "toggle"),
            SubMenu("email", "邮件提醒", [
                Field("alert.email_enabled", "启用邮件提醒", "toggle"),
                Field("alert.smtp_host", "SMTP 服务器", "text", hint="如 smtp.qq.com"),
                Field("alert.smtp_port", "SMTP 端口", "int", hint="465 / 587"),
                Field("alert.smtp_security", "加密方式", "choice",
                      choices=["ssl", "starttls", "none"],
                      hint="ssl=465（QQ/网易）｜starttls=587（Gmail/Outlook）"),
                Field("alert.smtp_user", "邮箱账号", "text", hint="如 you@qq.com"),
                Field("alert.smtp_password", "密码/授权码", "secret", secret=True,
                      hint="QQ/163 邮箱填「授权码」，不是登录密码"),
                Field("alert.email_from", "发件人", "text", hint="留空=邮箱账号"),
                Field("alert.email_to", "收件人", "text", hint="多个用逗号分隔"),
            ]),
            Field("alert.quiet_hours", "免打扰时段", "hours", hint="23-7，留空=不启用"),
            Field("alert.cooldown_seconds", "提醒冷却", "int", unit=" 秒",
                  hint="同一档位在该秒数内不重复打扰"),
        ]),
        Group("高级", [
            Field("quota.default_limit_tokens", "默认日额度", "int", unit=" token",
                  hint="未单独设额度的联系人按此计"),
            Field("collector.enabled", "实时采集", "toggle"),
            Field("collector.poll_interval_s", "采集轮询间隔", "number", unit=" 秒",
                  hint="拉一次聊天记录的间隔"),
            Field("collector.full_backfill", "首次全量回填", "toggle",
                  hint="关闭=只读启动后的新消息，首次启动更快"),
            Field("collector.full_backfill_limit", "回填单联系人上限", "int", unit=" 条",
                  hint="首次回填每个联系人最多读多少条；0=一次读完，越小越快"),
            Field("whisper.enabled", "语音转写", "toggle"),
            Field("whisper.model_size", "转写模型", "choice", choices=["tiny", "base"],
                  hint="tiny 约 75MB / base 约 145MB"),
            Field("app.allow_mock_ingest", "演示数据入口", "toggle",
                  hint="关掉后 /api/ingest/mock 不可用，避免误触污染统计"),
            Field("backup.enabled", "每日数据库快照", "toggle",
                  hint="每天一份，保留最近 3 份"),
            Field("autostart", "开机自启", "toggle"),
        ], collapsible=True, summary="默认额度 / 采集 / 转写 / 自启"),
    ]


# ---------------------------------------------------------------- 渲染 + 交互

class SetupScreen:
    """配置页状态机：渲染 → 读键 → 改状态 → 重绘。"""

    def __init__(self, values: dict[str, Any], io: SetupIO, color: bool | None = None,
                 autostart: Any = None, test_mail: Callable[[AlertConfig], tuple[bool, str]]
                 | None = None):
        self.values = dict(values)
        self.io = io
        self.use_color = supports_color() if color is None else color
        self.groups = build_groups()
        self.cursor = 0
        self.expanded: set[str] = set()
        self.buffer: str | None = None       # 编辑中的输入缓冲
        self.editing: Field | None = None
        self.error: str = ""
        self.submenu: SubMenu | None = None
        self.sub_index = 0
        self.autostart = autostart
        self._test_mail = test_mail or send_test_mail
        self._asked_mail = False
        self._autostart_initial: bool | None = None
        if autostart is not None:
            try:
                self._autostart_initial = bool(autostart.status().enabled)
                self.values["autostart"] = self._autostart_initial
            except Exception:
                self._autostart_initial = None
        self._reset_cursor()      # 放在最后：rows() 依赖 submenu/expanded 已初始化

    # ---------- 行模型 ----------

    def rows(self) -> list[Row]:
        if self.submenu is not None:
            out = [Row("header", self.submenu.label)]
            out += [Row("field", f.label, f) for f in self.submenu.fields]
            out.append(Row("back", "← 返回"))
            return out

        out: list[Row] = []
        for g in self.groups:
            if g.collapsible and g.title not in self.expanded:
                out.append(Row("group", f"{g.title}设置（{g.summary}）", g))
                continue
            out.append(Row("header", g.title))
            for item in g.items:
                out.append(Row("submenu", item.label, item)
                           if isinstance(item, SubMenu) else Row("field", item.label, item))
        out.append(Row("save", SAVE_LABEL))
        return out

    def selectable(self, rows: list[Row]) -> list[int]:
        return [i for i, r in enumerate(rows) if r.kind != "header"]

    def _reset_cursor(self) -> None:
        """光标落在第一个可选行（分组标题不可选）。"""
        rows = self.rows()
        self.cursor = self.selectable(rows)[0] if self.selectable(rows) else 0

    # ---------- 渲染 ----------

    def _center(self, line: str) -> str:
        return center_line(line, self.io.width)

    def render(self) -> str:
        width = self.io.width
        lines: list[str] = ["", ""]
        for raw in LOGO_ART.strip("\n").splitlines():
            lines.append(self._center(paint(raw, C_MARK, self.use_color)))
        lines.append(self._center(paint("  WeQuota — 首次启动配置", C_TITLE, self.use_color)))
        lines.append("")

        rows = self.rows()
        value_col = max(18, width - 24)
        for i, row in enumerate(rows):
            if row.kind == "header":
                lines.append(f"  {paint(row.label, C_DIM, self.use_color)}")
                continue
            marker = "▸" if i == self.cursor else " "
            label = row.label
            value = self._row_value(row)
            gap = max(1, value_col - display_width(f"  {marker} {label}"))
            head = paint(f"  {marker} {label}", C_MARK if i == self.cursor else C_DIM,
                         self.use_color)
            lines.append(f"{head}{' ' * gap}{paint(value, C_VALUE, self.use_color)}")
            if i == self.cursor and self.editing is not None and row.kind == "field":
                lines.append(self._editor_line(row.target))
        lines.append("")
        if self.error:
            lines.append(f"  {paint('! ' + self.error, C_MARK, self.use_color)}")
            lines.append("")
        lines.append(f"  {paint('↑↓ 移动 · Enter 编辑/切换 · Esc 返回 · Ctrl+C 放弃修改并启动', C_DIM, self.use_color)}")
        return "\n".join(lines)

    def _row_value(self, row: Row) -> str:
        if row.kind == "save":
            return "写入 config.json 后启动服务"
        if row.kind == "back":
            return ""
        if row.kind == "group":
            return "Enter 展开"
        if row.kind == "submenu":
            return row.target.summary(self.values)
        return row.target.display(self.values.get(row.target.key))

    def _editor_line(self, f: Field) -> str:
        current = self.values.get(f.key)
        shown = "" if current in (None, [], "") else str(current)
        if f.kind == "toggle":
            return ""
        if f.secret:
            shown = "••••••" if shown else ""
        hint = f" [{shown}]" if shown else (f" [默认 {f.default}]" if f.default else "")
        tail = f"  {f.hint}" if f.hint else ""
        return f"      {paint('›', C_MARK, self.use_color)} {f.label}{hint}: {self.buffer or ''}_{tail}"

    # ---------- 交互 ----------

    def run(self) -> SetupResult:
        if self.buffer is None:
            self.io.clear()
            self.io.write(self.render())
        while True:
            key = self.io.read_key()
            outcome = self.handle_key(key)
            if outcome is not None:
                return outcome

    def handle_key(self, key: str) -> SetupResult | None:
        if self.editing is not None:
            return self._handle_editing(key)
        return self._handle_menu(key)

    def _handle_editing(self, key: str) -> SetupResult | None:
        f = self.editing
        assert f is not None
        if key == KEY_ENTER:
            value, err = f.parse(self.buffer or "", self.values.get(f.key))
            if err:
                self.error = err
            else:
                self.values[f.key] = value
                self._after_edit(f, value)
                self.error = ""
            self.editing, self.buffer = None, None
        elif key == KEY_ESC or key == KEY_CTRL_C:
            self.editing, self.buffer, self.error = None, None, ""
        elif key == KEY_BACKSPACE:
            self.buffer = (self.buffer or "")[:-1]
        elif len(key) == 1:
            self.buffer = (self.buffer or "") + key
        self.io.clear()
        self.io.write(self.render())
        return None

    def _after_edit(self, f: Field, value: Any) -> None:
        """邮件账号填好后，发件人/收件人默认跟随（回车即采纳的智能默认值）。"""
        if f.key == "alert.smtp_user" and isinstance(value, str) and "@" in value:
            if not self.values.get("alert.email_from"):
                self.values["alert.email_from"] = value
            if not self.values.get("alert.email_to"):
                self.values["alert.email_to"] = [value]

    def _handle_menu(self, key: str) -> SetupResult | None:
        rows = self.rows()
        selectable = self.selectable(rows)
        if key in (KEY_UP, KEY_DOWN):
            if not selectable:
                return None
            pos = selectable.index(self.cursor) if self.cursor in selectable else 0
            delta = -1 if key == KEY_UP else 1
            self.cursor = selectable[(pos + delta) % len(selectable)]
        elif key == KEY_CTRL_C:
            return SetupResult(False, "已放弃修改，用现有配置启动", self.values)
        elif key == KEY_ESC:
            if self.submenu is not None:
                self._leave_submenu()
            else:
                return SetupResult(False, "已放弃修改，用现有配置启动", self.values)
        elif key == KEY_ENTER:
            outcome = self._activate(rows)
            if outcome is not None:
                return outcome
        self.io.clear()
        self.io.write(self.render())
        return None

    def _activate(self, rows: list[Row]) -> SetupResult | None:
        row = rows[self.cursor] if 0 <= self.cursor < len(rows) else None
        if row is None:
            return None
        if row.kind == "save":
            return SetupResult(True, "配置已保存", self.values)
        if row.kind == "group":
            self.expanded.add(row.target.title)
            self._reset_cursor()
            return None
        if row.kind == "back":
            self._leave_submenu()
            return None
        if row.kind == "submenu":
            self.submenu = row.target
            self.sub_index = 0
            self.cursor = 1          # 跳过子菜单标题
            return None
        if row.kind == "field":
            f = row.target
            if f.kind == "toggle":
                self.values[f.key] = not bool(self.values.get(f.key))
            else:
                self.editing, self.buffer = f, ""
        return None

    def _leave_submenu(self) -> None:
        menu = self.submenu
        self.submenu = None
        self._reset_cursor()
        if menu is not None and menu.key == "email" and not self._asked_mail:
            self._maybe_send_test_mail()

    def _maybe_send_test_mail(self) -> None:
        cfg = self.alert_config()
        if not (cfg.email_enabled and cfg.email_configured):
            return
        self._asked_mail = True
        answer = self.io.read_line("  现在发一封测试邮件确认配置？(y/N)：").strip().lower()
        if answer not in ("y", "yes"):
            return
        ok, message = self._test_mail(cfg)
        self.io.write(f"  {'✓' if ok else '✗'} {message}")
        self.io.read_line("  按回车继续…")

    def alert_config(self) -> AlertConfig:
        return AlertConfig.from_dict({
            k.split(".", 1)[1]: v for k, v in self.values.items() if k.startswith("alert.")
        })

    # ---------- 落盘 ----------

    def save(self, config_path: str | Path) -> dict[str, Any]:
        """把编辑结果合并进 config.json；返回写好的完整配置。"""
        p = Path(config_path)
        if not p.exists():
            dump_default_config(p)        # 首次保存先落一份完整默认配置，避免缺段
        data = _read_config_file(p)
        for section, patch in values_to_config(self.values).items():
            merged = data.setdefault(section, {})
            if isinstance(merged, dict):
                merged.update(patch)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._apply_autostart()
        return data

    def _apply_autostart(self) -> None:
        if self.autostart is None:
            return
        install = bool(self.values.get("autostart"))
        if self._autostart_initial is not None and install == self._autostart_initial:
            return                        # 没改过就别去动系统自启项
        (self.autostart.install() if install else self.autostart.uninstall())
        self._autostart_initial = install     # 已同步，避免下次重复动系统自启项


# ---------------------------------------------------------------- 编号降级模式

def run_numbered(screen: SetupScreen) -> SetupResult:
    """读不到方向键时的等价菜单：输编号选，输值编辑。"""
    while True:
        screen.io.write("")
        screen.io.write(screen.render_numbered())
        choice = screen.io.read_line("  选择编号（回车=保存并启动，q=放弃）：").strip().lower()
        if choice in ("", "s", "save"):
            return SetupResult(True, "配置已保存", screen.values)
        if choice in ("q", "quit", "esc"):
            return SetupResult(False, "已放弃修改，用现有配置启动", screen.values)
        rows = [r for r in screen.rows() if r.kind != "header"]
        if not choice.isdigit() or not (1 <= int(choice) <= len(rows)):
            screen.io.write("  ! 编号不存在")
            continue
        row = rows[int(choice) - 1]
        if row.kind == "save":
            return SetupResult(True, "配置已保存", screen.values)
        if row.kind == "group":
            screen.expanded.add(row.target.title)
            continue
        if row.kind == "submenu":
            screen.submenu = row.target
            _edit_submenu_numbered(screen)
            continue
        if row.kind == "field":
            _edit_field_numbered(screen, row.target)


def _edit_field_numbered(screen: SetupScreen, f: Field) -> None:
    if f.kind == "toggle":
        screen.values[f.key] = not bool(screen.values.get(f.key))
        return
    current = screen.values.get(f.key)
    shown = "" if current in (None, [], "") else (
        "••••••" if f.secret else (format_hours(current) if f.kind == "hours" else str(current)))
    hint = f" [{shown}]" if shown else ""
    raw = screen.io.read_secret(f"  {f.label}{hint}：") if f.secret else \
        screen.io.read_line(f"  {f.label}{hint}：")
    value, err = f.parse(raw, current)
    if err:
        screen.io.write(f"  ! {err}")
        return
    screen.values[f.key] = value
    screen._after_edit(f, value)


def _edit_submenu_numbered(screen: SetupScreen) -> None:
    menu = screen.submenu
    if menu is None:
        return
    while True:
        screen.io.write(f"  —— {menu.label} ——")
        for i, f in enumerate(menu.fields, start=1):
            screen.io.write(f"  [{i}] {f.label}：{f.display(screen.values.get(f.key))}")
        screen.io.write(f"  [0] 返回")
        choice = screen.io.read_line("  选择编号：").strip()
        if choice == "0":
            screen._leave_submenu()
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(menu.fields)):
            screen.io.write("  ! 编号不存在")
            continue
        _edit_field_numbered(screen, menu.fields[int(choice) - 1])


def render_numbered(screen: SetupScreen) -> str:
    lines: list[str] = []
    index = 1
    for row in screen.rows():
        if row.kind == "header":
            lines.append(f"  {row.label}")
            continue
        value = screen._row_value(row)
        label = row.label if row.kind != "group" else f"{row.label}（Enter 展开）"
        lines.append(f"  [{index}] {label}" + (f"    {value}" if value else ""))
        index += 1
    return "\n".join(lines)


SetupScreen.render_numbered = render_numbered      # 编号模式复用同一份行模型


# ---------------------------------------------------------------- 真实终端 IO

class ConsoleIO:
    """真实控制台：方向键走单键读取，密码走 getpass。"""

    def __init__(self, stream: Any | None = None) -> None:
        self._stream = stream or sys.stdin
        self._out = sys.stdout

    @property
    def width(self) -> int:
        try:
            import shutil

            return max(60, shutil.get_terminal_size().columns)
        except Exception:
            return 80

    def read_key(self) -> str:
        return read_key(self._stream)

    def read_line(self, prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""

    def read_secret(self, prompt: str) -> str:
        try:
            import getpass

            return getpass.getpass(prompt)
        except Exception:
            return self.read_line(prompt)

    def write(self, text: str = "") -> None:
        print(text, flush=True)

    def clear(self) -> None:
        print("\x1b[2J\x1b[H", end="", flush=True)


def raw_keys_available(stream: Any | None = None) -> bool:
    """能否读到方向键：非 TTY 一律不行；环境变量可强制关闭（便于测试/CI）。"""
    if os.environ.get("WEQUOTA_NO_RAW_KEYS"):
        return False
    target = sys.stdin if stream is None else stream
    if not getattr(target, "isatty", None) or not target.isatty():
        return False
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401
            import tty  # noqa: F401
        return True
    except Exception:
        return False


def read_key(stream: Any | None = None) -> str:
    """读一个按键；方向键返回 up/down，Ctrl+C 返回 ctrl_c，其余返回单字符。"""
    target = sys.stdin if stream is None else stream
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix(target)


def _read_key_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {"H": KEY_UP, "P": KEY_DOWN, "K": "left", "M": "right"}.get(code, KEY_ENTER)
    if ch == "\r":
        return KEY_ENTER
    if ch == "\x1b":
        return KEY_ESC
    if ch == "\x03":
        return KEY_CTRL_C
    if ch in ("\x08", "\x7f"):
        return KEY_BACKSPACE
    return ch


def _read_key_posix(stream: Any) -> str:
    import termios
    import tty

    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = stream.read(1)
        if ch == "\x1b":
            nxt = _peek(stream)
            if nxt == "[":
                stream.read(1)
                return {"A": KEY_UP, "B": KEY_DOWN}.get(stream.read(1), KEY_ESC)
            return KEY_ESC
        if ch in ("\r", "\n"):
            return KEY_ENTER
        if ch == "\x03":
            return KEY_CTRL_C
        if ch in ("\x7f", "\x08"):
            return KEY_BACKSPACE
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _peek(stream: Any) -> str:
    """ESC 后是否紧跟 '[' —— 单独按 Esc 时不能卡住等第二个字节。"""
    try:
        import select

        ready, _, _ = select.select([stream], [], [], 0.05)
        if not ready:
            return ""
        return stream.read(1)
    except Exception:
        return ""


# ---------------------------------------------------------------- 测试邮件

def send_test_mail(config: AlertConfig) -> tuple[bool, str]:
    """按当前配置真发一封测试邮件，失败给出可读原因。"""
    from datetime import datetime

    event = AlertEvent(
        account_id="setup", peer_id="setup", peer_name="配置测试",
        level=AlertLevel.WARN, used=0, limit=1, ratio=0.0,
        window_start=datetime.now().astimezone(),
    )
    try:
        ok = EmailChannel(config).send(event)
    except Exception as exc:
        return False, f"发送失败：{exc}"
    return (True, "测试邮件已发送，请查收收件箱") if ok else \
        (False, "发送失败：请检查 SMTP 服务器/端口/账号/授权码")


# ---------------------------------------------------------------- 入口

def run_setup(config_path: str | Path, io: SetupIO | None = None,
              autostart: Any = None, color: bool | None = None,
              interactive: bool | None = None) -> SetupResult:
    """跑一遍配置页；saved=True 时已写入 config.json。"""
    screen = SetupScreen(load_values(config_path), io or ConsoleIO(),
                         color=color, autostart=autostart)
    keys_ok = raw_keys_available() if interactive is None else interactive
    if not keys_ok:
        screen.io.write("  （终端不支持方向键，已切换为编号菜单）")
        screen.io.write("")
        for raw in LOGO_ART.strip("\n").splitlines():
            screen.io.write(screen._center(raw))
        result = run_numbered(screen)
    else:
        result = screen.run()
    if result.saved:
        screen.save(config_path)
    return result
