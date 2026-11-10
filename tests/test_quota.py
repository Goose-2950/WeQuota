"""限额引擎测试。

规则：
- 每联系人一条规则：窗口类型（日/周/月/自定义区间）+ 额度
- 消息 token 按「事件时间」落入所属窗口累计
- 80% 预警、100% 超限，每窗口每档只触发一次
- 补录进已过窗口：只更新统计，不补发提醒
- 实时改额：调低额度导致已超限时立即触发一次提醒
- 自定义窗口到期后规则自动失效
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wequota.contracts import MessageEnvelope
from wequota.metering import Meter
from wequota.metering_pipeline import MeteringPipeline
from wequota.quota import (
    AlertLevel,
    QuotaEngine,
    QuotaRule,
    resolve_window,
)
from wequota.storage import Database

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 12, 15, 0, 0, tzinfo=TZ)  # 周一


def rule(**over) -> QuotaRule:
    base = dict(
        account_id="wxid_demo", peer_id="peer_a", peer_name="Alice",
        window_type="daily", limit_tokens=1000, enabled=True,
    )
    base.update(over)
    return QuotaRule(**base)


def env(msg_id: str, ts: datetime, tokens: int, peer_id: str = "peer_a") -> MessageEnvelope:
    e = MessageEnvelope(
        account_id="wxid_demo", msg_id=msg_id, peer_id=peer_id, peer_name="Alice",
        direction="send", msg_type="text", timestamp=ts, text="x",
    )
    e.token_count = tokens
    return e


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "q.db")
    d.init()
    return d


@pytest.fixture()
def engine(db) -> QuotaEngine:
    return QuotaEngine(db)


# ---------- 窗口归属 ----------

def test_daily_window_is_today_local():
    start, end = resolve_window(rule(), NOW)
    assert start == datetime(2026, 10, 12, 0, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 10, 13, 0, 0, 0, tzinfo=TZ)


def test_weekly_window_starts_monday():
    start, _ = resolve_window(rule(window_type="weekly"), NOW)
    assert start.weekday() == 0
    assert start <= NOW


def test_monthly_window_starts_first_day():
    start, _ = resolve_window(rule(window_type="monthly"), NOW)
    assert (start.day, start.month) == (1, 10)


def test_custom_window_uses_rule_range():
    r = rule(window_type="custom",
             custom_start=datetime(2026, 10, 1, tzinfo=TZ),
             custom_end=datetime(2026, 10, 20, tzinfo=TZ))
    start, end = resolve_window(r, NOW)
    assert (start, end) == (r.custom_start, r.custom_end)


def test_custom_window_outside_range_is_none():
    r = rule(window_type="custom",
             custom_start=datetime(2026, 9, 1, tzinfo=TZ),
             custom_end=datetime(2026, 9, 30, tzinfo=TZ))
    assert resolve_window(r, NOW) is None


def test_window_boundary_splits_usage(db, engine):
    """跨窗口的消息不能累加到同一个窗口。"""
    engine.set_rule(rule(window_type="daily", limit_tokens=1000))
    engine.apply_message(env("m1", datetime(2026, 10, 11, 23, 50, tzinfo=TZ), 400), now=NOW)
    engine.apply_message(env("m2", datetime(2026, 10, 12, 0, 5, tzinfo=TZ), 300), now=NOW)
    assert engine.usage("peer_a", now=NOW).used == 300


# ---------- 用量与阈值 ----------

def test_usage_accumulates_send_and_recv(db, engine):
    engine.set_rule(rule(limit_tokens=1000))
    engine.apply_message(env("m1", NOW, 300, ), now=NOW)
    engine.apply_message(env("m2", NOW, 200), now=NOW)
    u = engine.usage("peer_a", now=NOW)
    assert u.used == 500
    assert u.limit == 1000
    assert u.ratio == 0.5


def test_alert_at_eighty_percent_once(db, engine):
    engine.set_rule(rule(limit_tokens=1000))
    events = engine.apply_message(env("m1", NOW, 850), now=NOW)
    assert [e.level for e in events] == [AlertLevel.WARN]
    again = engine.apply_message(env("m2", NOW, 10), now=NOW)
    assert again == []  # 同窗口同档位不重复提醒


def test_alert_at_hundred_percent(db, engine):
    engine.set_rule(rule(limit_tokens=1000))
    e1 = engine.apply_message(env("m1", NOW, 900), now=NOW)   # 90% → 预警
    e2 = engine.apply_message(env("m2", NOW, 200), now=NOW)   # 110% → 超限
    assert e1[0].level == AlertLevel.WARN
    assert e2[0].level == AlertLevel.EXCEEDED


def test_no_rule_means_no_limit(db, engine):
    assert engine.usage("peer_a", now=NOW).limit is None
    assert engine.apply_message(env("m1", NOW, 10_000), now=NOW) == []


def test_disabled_rule_not_enforced(db, engine):
    engine.set_rule(rule(limit_tokens=100, enabled=False))
    assert engine.apply_message(env("m1", NOW, 500), now=NOW) == []


def test_per_contact_isolation(db, engine):
    engine.set_rule(rule(peer_id="peer_a", limit_tokens=100))
    engine.set_rule(rule(peer_id="peer_b", peer_name="Bob", limit_tokens=1000))
    engine.apply_message(env("m1", NOW, 90, peer_id="peer_a"), now=NOW)
    assert engine.usage("peer_a", now=NOW).ratio == 0.9
    assert engine.usage("peer_b", now=NOW).used == 0


# ---------- 补录规则 ----------

def test_backfill_into_past_window_updates_stats_without_alert(db, engine):
    """手机端聊天的消息补读进「已过窗口」：只更新统计，不翻旧账发提醒。"""
    engine.set_rule(rule(window_type="daily", limit_tokens=1000))
    old_day = NOW - timedelta(days=2)
    events = engine.apply_message(env("m_late", old_day, 5000), now=NOW)
    assert events == []
    past = engine.usage("peer_a", at=old_day)
    assert past.used == 5000
    assert past.is_backfill_only is True


def test_backfill_into_current_window_still_alerts(db, engine):
    engine.set_rule(rule(window_type="daily", limit_tokens=1000))
    earlier_today = NOW - timedelta(hours=3)
    events = engine.apply_message(env("m_late", earlier_today, 900), now=NOW)
    assert [e.level for e in events] == [AlertLevel.WARN]


# ---------- 实时改额 ----------

def test_lowering_limit_triggers_immediate_alert(db, engine):
    engine.set_rule(rule(limit_tokens=10_000))
    engine.apply_message(env("m1", NOW, 900), now=NOW)  # 9%，无提醒
    events = engine.set_rule(rule(limit_tokens=500), now=NOW)  # 调低到 500 → 180%
    assert events and events[0].level == AlertLevel.EXCEEDED


def test_raising_limit_does_not_repeat_alert(db, engine):
    engine.set_rule(rule(limit_tokens=1000))
    engine.apply_message(env("m1", NOW, 1200), now=NOW)
    events = engine.set_rule(rule(limit_tokens=10_000), now=NOW)
    assert events == []


# ---------- 自定义窗口过期 ----------

def test_expired_custom_rule_is_disabled(db, engine):
    engine.set_rule(rule(window_type="custom", limit_tokens=100,
                         custom_start=NOW - timedelta(days=5),
                         custom_end=NOW - timedelta(days=1)))
    r = engine.get_rule("peer_a")
    assert r.enabled is False


def test_rule_crud(db, engine):
    engine.set_rule(rule(peer_id="peer_a", limit_tokens=1))
    engine.set_rule(rule(peer_id="peer_b", peer_name="Bob", limit_tokens=2))
    assert len(engine.list_rules()) == 2
    engine.delete_rule("peer_a")
    assert [r.peer_id for r in engine.list_rules()] == ["peer_b"]


# ---------- 与计量管线联动 ----------

def test_end_to_end_metering_then_quota(db):
    """真实链路：消息落库 → 计量 → 按事件时间计入窗口。"""
    from wequota.config import MeteringConfig

    msg = MessageEnvelope(
        account_id="wxid_demo", msg_id="m1", peer_id="peer_a", peer_name="Alice",
        direction="send", msg_type="voice_call", timestamp=NOW,
        media_meta={"duration_s": 60},
    )
    db.upsert_messages([msg])
    MeteringPipeline(db, Meter(MeteringConfig())).run_until_drained()

    engine = QuotaEngine(db)
    # 建规则时会自动把此前已计量但未计入窗口的历史消息补计进窗口
    engine.set_rule(rule(window_type="daily", limit_tokens=10_000), now=NOW)
    assert engine.usage("peer_a", now=NOW).used == 3000   # 60秒 × 2 × 25
    # 已计入的消息不会重复累加
    assert engine.run_pending(now=NOW) == []
    assert engine.usage("peer_a", now=NOW).used == 3000


def test_no_warn_after_exceeded(db):
    """回归：超限后不得再降级发预警（同窗口只提醒一次，且档位不倒挂）。"""
    engine = QuotaEngine(db)
    engine.set_rule(rule(limit_tokens=1000), now=NOW)
    assert engine.apply_message(env("m1", NOW, 500), now=NOW) == []          # 未到 80%
    second = engine.apply_message(env("m2", NOW, 500), now=NOW)              # 累计 1000
    assert [e.level.value for e in second] == ["exceeded"]
    third = engine.apply_message(env("m3", NOW, 500), now=NOW)               # 累计 1500
    assert third == []
    assert engine.usage("peer_a", now=NOW).used == 1500
    levels = [a["level"] for a in db.recent_alerts()]
    assert levels == ["exceeded"]


# ---------- 默认额度（M10 启动配置页新增） ----------

def test_without_default_limit_unknown_contact_is_not_metered(db):
    """没设默认额度时，未建规则的联系人不提醒（保持旧行为）。"""
    engine = QuotaEngine(db)
    assert engine.apply_message(env("m1", NOW, 900), now=NOW) == []
    assert engine.usage("peer_a", now=NOW).used == 0


def test_default_limit_applies_to_contact_without_rule(db):
    engine = QuotaEngine(db, default_limit_tokens=1000)
    events = engine.apply_message(env("m1", NOW, 900), now=NOW)
    assert [e.level.value for e in events] == ["warn"]      # 900/1000 = 90%
    assert engine.usage("peer_a", now=NOW).used == 900


def test_explicit_rule_beats_default_limit(db):
    engine = QuotaEngine(db, default_limit_tokens=1000)
    engine.set_rule(rule(limit_tokens=100_000), now=NOW)
    assert engine.apply_message(env("m1", NOW, 900), now=NOW) == []
    assert engine.usage("peer_a", now=NOW).used == 900


def test_run_pending_counts_contacts_without_rule_when_default_set(db):
    """默认额度开启后，未建规则的联系人在批量补计时也要入账。"""
    Meter().meter(env("m1", NOW, 900))
    db.upsert_messages([env("m1", NOW, 900)])
    engine = QuotaEngine(db, default_limit_tokens=1000)
    engine.run_pending(now=NOW)
    assert engine.usage("peer_a", now=NOW).used == 900
