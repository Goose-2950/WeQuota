"""M11：大屏「设置」面板的后端支撑。

设计要点：
- **一份 schema 驱动前后端**：字段定义（分组 / 类型 / 提示 / 是否需重启）在这里声明，
  前端通过 `GET /api/settings` 拿到同一份结构渲染表单，避免两边各写一份而漂移。
- **只暴露「可安全编辑」的子集**：计量系数这类底层参数不放进来（仍改 config.json）。
- **秘密字段只出不进**：`smtp_password` 读取时统一返回掩码；保存时若仍是掩码则保持原值不动，
  这样前端永远拿不到真实授权码，也不会因为「回传掩码」把密码覆盖成一串圆点。
- **热更新优先**：能原地生效的（默认额度 / 提醒开关 / 免打扰 / 冷却 / 演示入口）立刻生效；
  采集器与转写引擎在启动时已建好实例，改了只能重启，schema 里标 `needs_restart` 由前端提示。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import _read_config_file

# 读取秘密字段时返回的占位符；回传同样的值表示「不修改」
SECRET_MASK = "••••••••"

# 免打扰时段的输入格式：`23-7`（支持跨零点），留空表示不启用
QUIET_HOURS_RE = re.compile(r"^\s*(\d{1,2})\s*[-~—]\s*(\d{1,2})\s*$")

SMTP_SECURITY_OPTIONS = [
    {"value": "ssl", "label": "SSL（端口 465）"},
    {"value": "starttls", "label": "STARTTLS（端口 587）"},
    {"value": "none", "label": "不加密（端口 25，不推荐）"},
]

# 常用邮箱的一键预设：填账号后前端可据此补全服务器/端口/加密方式
SMTP_PRESETS: list[dict[str, Any]] = [
    {"id": "qq", "name": "QQ 邮箱", "domain": "qq.com",
     "host": "smtp.qq.com", "port": 465, "security": "ssl",
     "note": "设置 → 账号与安全 → 开启「IMAP/SMTP 服务」→ 短信验证得到 16 位授权码"},
    {"id": "foxmail", "name": "Foxmail", "domain": "foxmail.com",
     "host": "smtp.qq.com", "port": 465, "security": "ssl",
     "note": "与 QQ 邮箱同一套服务器，密码填 16 位授权码"},
    {"id": "163", "name": "网易 163", "domain": "163.com",
     "host": "smtp.163.com", "port": 465, "security": "ssl",
     "note": "设置 → POP3/SMTP/IMAP → 开启服务 → 生成 16 位「客户端授权码」（只显示一次）"},
    {"id": "126", "name": "网易 126", "domain": "126.com",
     "host": "smtp.126.com", "port": 465, "security": "ssl",
     "note": "同 163：用客户端授权码，不是登录密码"},
    {"id": "yeah", "name": "网易 yeah.net", "domain": "yeah.net",
     "host": "smtp.yeah.net", "port": 465, "security": "ssl",
     "note": "同 163：用客户端授权码，不是登录密码"},
    {"id": "gmail", "name": "Gmail", "domain": "gmail.com",
     "host": "smtp.gmail.com", "port": 587, "security": "starttls",
     "note": "需开启两步验证后生成「应用专用密码」；国内网络通常连不通"},
    {"id": "outlook", "name": "Outlook / Hotmail", "domain": "outlook.com",
     "host": "smtp.office365.com", "port": 587, "security": "starttls",
     "note": "密码填应用密码；国内网络可能连不通"},
]


def guess_preset(email: str) -> dict[str, Any] | None:
    """按邮箱域名猜预设；猜不到返回 None（前端就不自动填）。"""
    domain = (email or "").split("@")[-1].strip().lower()
    if not domain:
        return None
    for preset in SMTP_PRESETS:
        if domain == preset["domain"] or domain.endswith("." + preset["domain"]):
            return preset
    return None


@dataclass
class FieldSpec:
    """一个可编辑字段；`key` 形如 `alert.email_enabled`（段名.属性名）。"""

    key: str
    label: str
    type: str = "text"           # toggle / int / number / text / secret / choice / hours / emails / urls
    hint: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    secret: bool = False
    needs_restart: bool = False
    min_value: float | None = None
    max_value: float | None = None

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def attr(self) -> str:
        return self.key.split(".", 1)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "type": self.type, "hint": self.hint,
            "options": self.options, "secret": self.secret,
            "needs_restart": self.needs_restart,
            "min": self.min_value, "max": self.max_value,
        }


@dataclass
class SectionSpec:
    id: str
    title: str
    fields: list[FieldSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title,
                "fields": [f.to_dict() for f in self.fields]}


SECTIONS: list[SectionSpec] = [
    SectionSpec("quota", "全局额度", [
        FieldSpec("quota.default_limit_tokens", "默认日额度", "int",
                  hint="token/天；未单独设额度的联系人按这个数计，0 = 不限制",
                  min_value=0, max_value=10_000_000),
    ]),
    SectionSpec("alert", "提醒", [
        FieldSpec("alert.desktop_enabled", "桌面通知", "toggle",
                  hint="系统通知中心弹提醒"),
        FieldSpec("alert.dashboard_enabled", "大屏弹窗", "toggle",
                  hint="数据大屏右下角实时弹出"),
        FieldSpec("alert.email_enabled", "邮件提醒", "toggle",
                  hint="需先在下面「邮件服务器」里配好 SMTP"),
        FieldSpec("alert.cooldown_seconds", "提醒冷却", "int",
                  hint="秒；同一联系人同一档位在这段时间内不重复打扰",
                  min_value=0, max_value=86_400),
        FieldSpec("alert.quiet_hours", "免打扰时段", "hours",
                  hint="格式 23-7（晚 11 点到早 7 点），留空 = 不启用"),
    ]),
    SectionSpec("smtp", "邮件服务器", [
        FieldSpec("alert.smtp_host", "SMTP 服务器", "text",
                  hint="QQ: smtp.qq.com ｜ 网易 163: smtp.163.com"),
        FieldSpec("alert.smtp_port", "端口", "int",
                  hint="SSL 465 / STARTTLS 587", min_value=1, max_value=65535),
        FieldSpec("alert.smtp_security", "加密方式", "choice",
                  options=SMTP_SECURITY_OPTIONS,
                  hint="端口与加密方式要配套，填错会连不上"),
        FieldSpec("alert.smtp_user", "邮箱账号", "text",
                  hint="完整地址，如 you@qq.com"),
        FieldSpec("alert.smtp_password", "密码 / 授权码", "secret", secret=True,
                  hint="QQ 与网易都填「授权码」而非登录密码；保存后不再回显"),
        FieldSpec("alert.email_from", "发件人", "text",
                  hint="留空 = 邮箱账号；多数服务商要求与账号一致"),
        FieldSpec("alert.email_to", "收件人", "emails",
                  hint="多个用逗号分隔，自己发给自己就填自己的地址"),
    ]),
    SectionSpec("collector", "实时采集", [
        FieldSpec("collector.enabled", "启用实时采集", "toggle", needs_restart=True,
                  hint="关闭后只使用导入的备份数据与示例数据"),
        FieldSpec("collector.poll_interval_s", "轮询间隔", "number", needs_restart=True,
                  hint="秒；越小越实时，也越占资源", min_value=1, max_value=3600),
        FieldSpec("collector.full_backfill", "首次全量回填", "toggle", needs_restart=True,
                  hint="关闭 = 只读启动后的新消息，首次启动更快；不会删除已入库数据"),
        FieldSpec("collector.full_backfill_limit", "回填单联系人上限", "int", needs_restart=True,
                  hint="条；0 = 一次读完，越小首次启动越快", min_value=0, max_value=1000000),
    ]),
    SectionSpec("whisper", "语音转写", [
        FieldSpec("whisper.enabled", "启用语音转写", "toggle", needs_restart=True,
                  hint="关闭后语音消息按「时长 × 25」折算"),
        FieldSpec("whisper.model_size", "模型规格", "choice", needs_restart=True,
                  options=[{"value": s, "label": s} for s in ("tiny", "base", "small", "medium")],
                  hint="tiny 约 75MB 已实测；其余规格未实测，换规格后要重新下载模型"),
    ]),
    SectionSpec("system", "其他", [
        FieldSpec("app.allow_mock_ingest", "演示数据入口", "toggle",
                  hint="关闭后 /api/ingest/mock 拒绝写入，避免误触污染真实统计"),
        FieldSpec("app.auth_token", "访问令牌", "secret", secret=True, needs_restart=True,
                  hint="留空 = 不启用鉴权；启用后 API 需 Authorization: Bearer <token>，大屏会提示输入"),
        FieldSpec("app.cors_origins", "跨域来源", "urls",
                  hint="逗号分隔，如 https://a.com；留空 = 仅同源；* = 允许所有（不推荐）"),
        FieldSpec("backup.enabled", "每日数据库快照", "toggle",
                  hint="每天启动时存一份到 data/backups/"),
        FieldSpec("backup.keep", "快照保留份数", "int", min_value=1, max_value=30),
    ]),
]

FIELDS: dict[str, FieldSpec] = {f.key: f for s in SECTIONS for f in s.fields}


def schema_dict() -> dict[str, Any]:
    return {"sections": [s.to_dict() for s in SECTIONS], "presets": SMTP_PRESETS}


def _display(spec: FieldSpec, value: Any) -> Any:
    """把内部值转成前端好渲染的形态（含秘密掩码与时段格式化）。"""
    if spec.secret:
        return SECRET_MASK if value else ""
    if spec.type == "hours":
        return "" if not value else f"{value[0]}-{value[1]}"
    if spec.type in ("emails", "urls"):
        return ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else (value or "")
    return value


def read_values(bundle: dict[str, Any]) -> dict[str, Any]:
    """从各配置对象里读出当前值（秘密字段已掩码）。"""
    values: dict[str, Any] = {}
    for spec in FIELDS.values():
        holder = bundle.get(spec.section)
        if holder is None:
            continue
        values[spec.key] = _display(spec, getattr(holder, spec.attr, None))
    return values


class SettingError(ValueError):
    """单个字段校验失败；message 直接给前端展示。"""


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    if spec.type == "toggle":
        return bool(raw)
    if spec.type in ("int", "number"):
        if isinstance(raw, bool) or raw is None or raw == "":
            raise SettingError(f"{spec.label}：请填数字")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise SettingError(f"{spec.label}：请填数字")
        if spec.type == "int":
            number = int(number)
        if spec.min_value is not None and number < spec.min_value:
            raise SettingError(f"{spec.label}：不能小于 {spec.min_value}")
        if spec.max_value is not None and number > spec.max_value:
            raise SettingError(f"{spec.label}：不能大于 {spec.max_value}")
        return number
    if spec.type == "choice":
        allowed = {o["value"] for o in spec.options}
        if str(raw) not in allowed:
            raise SettingError(f"{spec.label}：可选值为 {' / '.join(sorted(allowed))}")
        return str(raw)
    if spec.type == "hours":
        text = str(raw or "").strip()
        if not text:
            return None
        m = QUIET_HOURS_RE.match(text)
        if not m:
            raise SettingError(f"{spec.label}：请按 23-7 这样的格式填写")
        start, end = int(m.group(1)), int(m.group(2))
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise SettingError(f"{spec.label}：小时需在 0–23 之间")
        return [start, end]
    if spec.type == "emails":
        if isinstance(raw, (list, tuple)):
            items = [str(v).strip() for v in raw]
        else:
            items = [p.strip() for p in str(raw or "").replace(";", ",").split(",")]
        items = [i for i in items if i]
        for item in items:
            if "@" not in item:
                raise SettingError(f"{spec.label}：{item} 不是有效的邮箱地址")
        return items
    if spec.type == "urls":
        if isinstance(raw, (list, tuple)):
            items = [str(v).strip() for v in raw]
        else:
            items = [p.strip() for p in re.split(r"[,;\s]+", str(raw or ""))]
        items = [i for i in items if i]
        for item in items:
            if item != "*" and not re.match(r"^https?://", item):
                raise SettingError(f"{spec.label}：{item} 需以 http:// 或 https:// 开头")
        return items
    return "" if raw is None else str(raw)


def apply_values(
    bundle: dict[str, Any],
    values: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """把前端提交的值写回配置对象（原地修改，让运行中的通道/引擎立刻看到）。

    返回 (changed_keys, restart_keys, errors)。errors 非空时其他字段仍然照改——
    一个字段填错不该让用户把整张表重填一遍。
    """
    changed: list[str] = []
    restart: list[str] = []
    errors: list[str] = []
    for key, raw in (values or {}).items():
        spec = FIELDS.get(key)
        if spec is None:                      # 未知字段直接忽略，不写进配置
            continue
        holder = bundle.get(spec.section)
        if holder is None:
            continue
        if spec.secret and raw == SECRET_MASK:   # 前端原样回传掩码 = 不改动
            continue
        try:
            value = _coerce(spec, raw)
        except SettingError as exc:
            errors.append(str(exc))
            continue
        if getattr(holder, spec.attr, None) == value:
            continue
        setattr(holder, spec.attr, value)
        changed.append(key)
        if spec.needs_restart:
            restart.append(key)
    return changed, restart, errors


def save_values(config_path: str | Path, bundle: dict[str, Any],
                schema_version: int) -> None:
    """把改动后的配置包写回 config.json（保留文件里已有的其它字段与段落）。"""
    path = Path(config_path)
    data = dict(_read_config_file(path))
    for section, holder in bundle.items():
        if holder is None:
            continue
        current = dict(data.get(section) or {})
        current.update(asdict(holder))
        data[section] = current
    data["schema_version"] = schema_version
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
