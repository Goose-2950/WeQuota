"""限额引擎：把消息 token 按「事件时间」归到统计窗口，判断是否超限。

核心规则（对应方案 v0.3）：
- 每联系人一条规则：窗口类型（日/周/月/自定义区间）+ 额度
- 消息 token 落入「消息发生时间」所属窗口；跨窗口自动分开累计（按 window_start 分键存储）
- 80% 预警、100% 超限，同一窗口同一档位只提醒一次
- 补录（手机聊天在 PC 重新登录后补读）落入已过窗口：只更新统计，不补发提醒
- 实时改额：改完立即重新评估；调低导致已超限时立刻触发一次提醒
- 自定义窗口过期：规则自动置为 disabled
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field, model_validator

from .contracts import MessageEnvelope
from .storage import Database

WINDOW_TYPES = ("daily", "weekly", "monthly", "custom")


class AlertLevel(str, Enum):
    WARN = "warn"        # 达到预警比例（默认 80%）
    EXCEEDED = "exceeded"  # 已超限


class QuotaRule(BaseModel):
    account_id: str = "wxid_demo"
    peer_id: str
    peer_name: str = ""
    window_type: str = "daily"
    custom_start: datetime | None = None
    custom_end: datetime | None = None
    limit_tokens: int = Field(gt=0)
    warn_ratio: float = Field(default=0.8, gt=0, le=1)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_window(self) -> "QuotaRule":
        if self.window_type not in WINDOW_TYPES:
            raise ValueError(f"window_type 必须是 {WINDOW_TYPES} 之一")
        if self.window_type == "custom" and not (self.custom_start and self.custom_end):
            raise ValueError("custom 窗口必须提供 custom_start 与 custom_end")
        return self


class Usage(BaseModel):
    peer_id: str
    peer_name: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    used: int = 0
    limit: int | None = None
    ratio: float = 0.0
    is_backfill_only: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)


class AlertEvent(BaseModel):
    account_id: str
    peer_id: str
    peer_name: str = ""
    level: AlertLevel
    used: int = 0
    limit: int = 0
    ratio: float = 0.0
    window_start: datetime | None = None
    window_end: datetime | None = None
    alert_id: int | None = None   # 对应 alert_logs.id，供提醒通道回填投递结果
    # 资金事件（转账 / 红包）专用：level 固定为 WARN，used/limit 语义不适用
    kind: str | None = None       # transfer / red_packet
    amount_yuan: float | None = None

    def is_money_event(self) -> bool:
        return self.kind in ("transfer", "red_packet")

    def message(self) -> str:
        if self.is_money_event():
            return self._money_message()
        pct = f"{self.ratio * 100:.0f}%"
        verb = "已超出限额" if self.level == AlertLevel.EXCEEDED else "即将达到限额"
        return (f"{self.peer_name or self.peer_id} {verb}：{self.used}/{self.limit} token（{pct}）")

    def _money_message(self) -> str:
        who = self.peer_name or self.peer_id
        label = "转账" if self.kind == "transfer" else "红包"
        if self.amount_yuan is not None:
            amount = f"¥{self.amount_yuan:.2f}"
            return f"{who} 给你发来一笔{label}：{amount}"
        return f"{who} 给你发来一个{label}（金额需在微信内查看）"


def money_alert_event(account_id: str, peer_id: str, peer_name: str,
                      kind: str, amount_yuan: float | None) -> AlertEvent:
    """构造一条资金事件提醒。"""
    return AlertEvent(
        account_id=account_id, peer_id=peer_id, peer_name=peer_name,
        level=AlertLevel.WARN, kind=kind, amount_yuan=amount_yuan,
    )


def _local(dt: datetime) -> datetime:
    return dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _coerce_datetime(value: Any) -> datetime:
    """数据库行可能是字符串，API 层可能是 datetime，统一处理。"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def resolve_window(rule: QuotaRule, at: datetime) -> tuple[datetime, datetime] | None:
    """返回 at 时刻所属窗口的 [start, end)；自定义窗口不在区间内则 None。"""
    t = _local(at)
    if rule.window_type == "daily":
        start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if rule.window_type == "weekly":
        start = (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    if rule.window_type == "monthly":
        start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, next_month
    # custom
    start, end = _local(rule.custom_start), _local(rule.custom_end)  # type: ignore[arg-type]
    if start <= t <= end:
        return start, end
    return None


class QuotaEngine:
    def __init__(self, db: Database, account_id: str = "wxid_demo",
                 default_limit_tokens: int = 0):
        self.db = db
        self.account_id = account_id
        # 未单独设规则的联系人按这个额度评估（0 = 不设默认，保持「没规则就不提醒」）
        self.default_limit_tokens = int(default_limit_tokens or 0)

    def _fallback_rule(self, peer_id: str, peer_name: str = "") -> "QuotaRule | None":
        if self.default_limit_tokens <= 0:
            return None
        return QuotaRule(account_id=self.account_id, peer_id=peer_id, peer_name=peer_name,
                         window_type="daily", limit_tokens=self.default_limit_tokens)

    # ---------- 规则管理 ----------

    def set_rule(self, rule: QuotaRule, now: datetime | None = None) -> list[AlertEvent]:
        """保存规则并立即重新评估当前窗口（实时改额生效）。返回触发的提醒事件。"""
        now = now or datetime.now().astimezone()
        old = self.db.get_rule(rule.account_id, rule.peer_id)
        stored = rule.model_dump()
        stored["custom_start"] = rule.custom_start.isoformat() if rule.custom_start else None
        stored["custom_end"] = rule.custom_end.isoformat() if rule.custom_end else None

        # 自定义窗口已过期 → 自动失效
        if rule.window_type == "custom" and rule.custom_end and _local(rule.custom_end) < now:
            rule.enabled = False
            stored["enabled"] = False
        self.db.upsert_rule(stored)

        if not rule.enabled:
            return []
        # 额度变化后重置提醒标记，避免旧档位抑制新额度下的提醒
        if old and int(old["limit_tokens"]) != rule.limit_tokens:
            window = resolve_window(rule, now)
            if window:
                self._reset_alert_flags(rule.peer_id, window[0])
        # 新规则/改额后，把此前未计入的历史消息补计进窗口
        # 注意：补计过程本身就可能触发提醒，必须把事件带出去，否则会「记了日志却没投递」
        events = self.run_pending(now=now)
        events.extend(self._evaluate(rule, now))
        return events

    def get_rule(self, peer_id: str) -> QuotaRule | None:
        row = self.db.get_rule(self.account_id, peer_id)
        return self._rule_from_row(row) if row else None

    def list_rules(self) -> list[QuotaRule]:
        return [self._rule_from_row(r) for r in self.db.list_rules(self.account_id)]

    def delete_rule(self, peer_id: str) -> None:
        self.db.delete_rule(self.account_id, peer_id)

    @staticmethod
    def _rule_from_row(row: dict[str, Any]) -> QuotaRule:
        return QuotaRule(
            account_id=row["account_id"], peer_id=row["peer_id"], peer_name=row["peer_name"],
            window_type=row["window_type"],
            custom_start=datetime.fromisoformat(row["custom_start"]) if row["custom_start"] else None,
            custom_end=datetime.fromisoformat(row["custom_end"]) if row["custom_end"] else None,
            limit_tokens=row["limit_tokens"], warn_ratio=row["warn_ratio"],
            enabled=bool(row["enabled"]),
        )

    # ---------- 计入用量 ----------

    def apply_message(self, message: MessageEnvelope, now: datetime | None = None) -> list[AlertEvent]:
        """把一条已计量消息的 token 计入其发生时间所属窗口。"""
        now = now or datetime.now().astimezone()
        tokens = message.token_count or 0
        return self._apply(message.peer_id, message.peer_name, tokens, message.timestamp, now)

    def apply_metered(self, row: dict[str, Any], now: datetime | None = None) -> list[AlertEvent]:
        """把数据库中一行已计量消息计入窗口（供流水线批量调用）。"""
        now = now or datetime.now().astimezone()
        event_time = _coerce_datetime(row["timestamp"])
        return self._apply(row["peer_id"], row.get("peer_name", ""),
                           row.get("token_count") or 0, event_time, now)

    def run_pending(self, limit: int = 500, now: datetime | None = None) -> list[AlertEvent]:
        """把尚未计入窗口的已计量消息批量处理（幂等）。"""
        now = now or datetime.now().astimezone()
        events: list[AlertEvent] = []
        for row in self.db.pending_quota_messages(limit=limit):
            # 没规则且没设默认额度的联系人先跳过（不标记已应用），等规则建立后再补计
            if self.get_rule(row["peer_id"]) is None and self.default_limit_tokens <= 0:
                continue
            events.extend(self.apply_metered(row, now=now))
            self.db.mark_quota_applied(row["id"])
        return events

    def _apply(self, peer_id: str, peer_name: str, tokens: int,
               event_time: datetime, now: datetime) -> list[AlertEvent]:
        rule = self.get_rule(peer_id) or self._fallback_rule(peer_id, peer_name)
        if rule is None or not rule.enabled or tokens <= 0:
            return []

        window = resolve_window(rule, event_time)
        if window is None:
            return []  # 不在自定义区间内，忽略
        current = resolve_window(rule, now)
        is_past_window = current is not None and window[0] < current[0]

        self.db.add_usage(rule.account_id, peer_id, window[0], tokens, backfill=is_past_window)
        if is_past_window:
            # 补录进已过窗口：只更新统计，不翻旧账发提醒
            return []
        return self._evaluate(rule, now)

    # ---------- 查询与评估 ----------

    def usage(self, peer_id: str, at: datetime | None = None,
              now: datetime | None = None) -> Usage:
        now = now or datetime.now().astimezone()
        at = at or now
        rule = self.get_rule(peer_id) or self._fallback_rule(peer_id)
        if rule is None:
            return Usage(peer_id=peer_id)
        window = resolve_window(rule, at) or resolve_window(rule, now)
        if window is None:
            return Usage(peer_id=peer_id, peer_name=rule.peer_name, limit=rule.limit_tokens)
        row = self.db.get_usage(rule.account_id, peer_id, window[0]) or {}
        used = int(row.get("used_tokens", 0) or 0)
        backfill_only = bool(row.get("backfill_only", 0))
        ratio = used / rule.limit_tokens if rule.limit_tokens else 0.0
        return Usage(
            peer_id=peer_id, peer_name=rule.peer_name, window_start=window[0],
            window_end=window[1], used=used, limit=rule.limit_tokens,
            ratio=ratio, is_backfill_only=backfill_only,
        )

    def _evaluate(self, rule: QuotaRule, now: datetime) -> list[AlertEvent]:
        window = resolve_window(rule, now)
        if window is None:
            return []
        row = self.db.get_usage(rule.account_id, rule.peer_id, window[0])
        if not row:
            return []
        used = int(row["used_tokens"] or 0)
        threshold = rule.limit_tokens
        if used >= threshold:
            # 已超限就不再降级发预警，避免同一窗口出现「先超限后预警」的倒挂提醒
            if row["alerted_over"]:
                return []
            return self._emit(rule, used, AlertLevel.EXCEEDED, window)
        if used >= threshold * rule.warn_ratio and not row["alerted_warn"]:
            return self._emit(rule, used, AlertLevel.WARN, window)
        return []

    def _emit(self, rule: QuotaRule, used: int, level: AlertLevel,
              window: tuple[datetime, datetime]) -> list[AlertEvent]:
        self.db.mark_alerted(rule.account_id, rule.peer_id, window[0], level.value)
        alert_id = self.db.log_alert(rule.account_id, rule.peer_id, level.value, used,
                                     rule.limit_tokens, window[0])
        return [AlertEvent(
            account_id=rule.account_id, peer_id=rule.peer_id, peer_name=rule.peer_name,
            level=level, used=used, limit=rule.limit_tokens,
            ratio=used / rule.limit_tokens, window_start=window[0], window_end=window[1],
            alert_id=alert_id,
        )]

    def _reset_alert_flags(self, peer_id: str, window_start: datetime) -> None:
        conn = self.db._connect()
        try:
            conn.execute(
                "UPDATE quota_usage SET alerted_warn = 0, alerted_over = 0 "
                "WHERE account_id = ? AND peer_id = ? AND window_start = ?",
                (self.account_id, peer_id, window_start.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
