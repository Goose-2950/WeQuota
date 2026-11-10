"""SQLite 存储层测试：建表、幂等入账、补录识别、按时间/联系人查询。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wequota.contracts import MessageEnvelope
from wequota.storage import Database

TS0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)


def env(msg_id: str = "m1", **over) -> MessageEnvelope:
    base = dict(
        account_id="wxid_demo", msg_id=msg_id, peer_id="peer_a", peer_name="Alice",
        direction="send", msg_type="text", timestamp=TS0, text="hello",
    )
    base.update(over)
    return MessageEnvelope(**base)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def test_init_creates_schema_without_error(db):
    db.init()
    assert db.count_messages() == 0


def test_insert_and_query_by_peer(db):
    db.init()
    db.upsert_messages([env("m1"), env("m2", peer_id="peer_b", peer_name="Bob")])
    assert db.count_messages() == 2
    assert len(db.query_messages(peer_id="peer_b")) == 1


def test_duplicate_msg_id_is_idempotent(db):
    """同一条消息重复投递（采集器重跑/补同步）不能重复入账。"""
    db.init()
    db.upsert_messages([env("m1")])
    db.upsert_messages([env("m1"), env("m2")])
    assert db.count_messages() == 2


def test_ingested_at_records_late_arrival(db):
    """手机聊天在 PC 重新登录后被补读：ingested_at 明显晚于 timestamp。"""
    db.init()
    db.upsert_messages([env("late", timestamp=TS0 - timedelta(days=2))])
    row = db.query_messages(peer_id="peer_a")[0]
    assert row["ingested_at"] > row["timestamp"]
    assert db.is_backfill(row["id"]) is True


def test_query_by_time_range_uses_event_time(db):
    db.init()
    db.upsert_messages([
        env("old", timestamp=TS0 - timedelta(days=3)),
        env("new", timestamp=TS0),
    ])
    rows = db.query_messages(start=TS0 - timedelta(hours=1), end=TS0 + timedelta(hours=1))
    assert [r["msg_id"] for r in rows] == ["new"]


def test_multi_account_isolation(db):
    db.init()
    db.upsert_messages([env("a1"), env("b1", account_id="wxid_other")])
    assert len(db.query_messages(account_id="wxid_demo")) == 1
    assert len(db.query_messages(account_id="wxid_other")) == 1


def test_token_count_persisted(db):
    db.init()
    e = env("m1")
    e.token_count = 123
    e.meter_detail = {"formula": "tiktoken"}
    db.upsert_messages([e])
    row = db.query_messages(peer_id="peer_a")[0]
    assert row["token_count"] == 123
