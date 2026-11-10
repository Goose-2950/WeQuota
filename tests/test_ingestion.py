"""Mock Collector + Ingestion 测试。

M1 阶段用 mock 数据源驱动全链路，M7 接入 wechat-cli 时只替换采集器实现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wequota.collector.mock import MockCollector
from wequota.ingestion import Ingestor, is_group_peer
from wequota.storage import Database

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "t.db")
    d.init()
    return d


def test_mock_collector_covers_all_metered_types():
    msgs = MockCollector(account_id="wxid_demo").fetch(limit=200)
    types = {m.msg_type for m in msgs}
    assert {"text", "voice", "image", "video", "emoji", "voice_call", "video_call"} <= types


def test_mock_collector_messages_are_valid_and_timestamped():
    msgs = MockCollector(account_id="wxid_demo").fetch(limit=50)
    assert all(m.timestamp.tzinfo is not None for m in msgs)
    assert all(m.msg_id for m in msgs)


def test_mock_collector_respects_since_cursor():
    c = MockCollector(account_id="wxid_demo")
    all_msgs = c.fetch(limit=200)
    older = sorted(all_msgs, key=lambda m: m.timestamp)[len(all_msgs) // 2].timestamp
    newer = c.fetch(since=older)
    assert all(m.timestamp > older for m in newer)


def test_group_peers_are_ignored_in_v1():
    assert is_group_peer("123456@chatroom") is True
    assert is_group_peer("wxid_abc123") is False


def test_ingest_persists_messages_and_skips_groups(db):
    collector = MockCollector(account_id="wxid_demo")
    ingestor = Ingestor(db, account_id="wxid_demo")
    inserted = ingestor.run(collector)
    assert inserted > 0
    rows = db.query_messages()
    assert rows
    assert all(is_group_peer(r["peer_id"]) is False for r in rows)


def test_ingest_is_idempotent(db):
    """重复跑一次采集不能重复入账（补同步/重启场景）。"""
    collector = MockCollector(account_id="wxid_demo")
    ingestor = Ingestor(db, account_id="wxid_demo")
    first = ingestor.run(collector)
    second = ingestor.run(collector)
    assert second == 0
    assert db.count_messages() == first


def test_ingest_advances_sync_cursor(db):
    collector = MockCollector(account_id="wxid_demo")
    ingestor = Ingestor(db, account_id="wxid_demo")
    ingestor.run(collector)
    last_msg_id, last_ts = db.get_cursor("wxid_demo")
    assert last_msg_id
    assert last_ts is not None


def test_incremental_ingest_only_picks_new_messages(db):
    """第二轮只取游标之后的新消息。"""
    collector = MockCollector(account_id="wxid_demo")
    ingestor = Ingestor(db, account_id="wxid_demo")
    ingestor.run(collector)
    before = db.count_messages()
    ingestor.run(collector)  # 无新消息
    assert db.count_messages() == before


def test_backfill_messages_land_with_original_event_time(db):
    """手机端聊天的消息在 PC 重新登录后补读，事件时间必须是原始发生时间。"""
    collector = MockCollector(
        account_id="wxid_demo",
        backfill=[("m_late_1", "peer_a", "手机上发的消息", NOW - timedelta(days=2))],
    )
    Ingestor(db, account_id="wxid_demo").run(collector)
    row = next(r for r in db.query_messages() if r["msg_id"] == "m_late_1")
    assert row["timestamp"] == NOW - timedelta(days=2)
    assert db.is_backfill(row["id"]) is True
