"""M5 统计接口测试：总览 / 排行榜 / 时段序列 / 类型占比 / 周月报表。"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.config import QuotaConfig
from wequota.storage import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "a.db")
    db.init()
    # 显式关掉「默认日额度」，让「没单独设额度 = 无额度」的旧语义不受全局默认值干扰
    client = TestClient(create_app(db=db, quota_config=QuotaConfig(default_limit_tokens=0)))
    client.post("/api/ingest/mock")          # 3 联系人 × 8 条（含 1 条群聊被丢弃）
    return dict(db=db, client=client)


def test_mock_data_is_never_in_the_future(env):
    """回归：mock 数据不得落在「现在」之后。

    统计窗口 end=now，若造出的消息时间戳在未来，会被静默排除，
    导致 summary 的消息数随系统时钟随机变化（曾出现 21 / 19 / 18 三个值）。
    """
    with env["db"]._connect() as conn:
        rows = conn.execute("SELECT timestamp FROM messages").fetchall()
    assert rows, "mock 数据未入库"
    now = datetime.now().astimezone()
    for row in rows:
        ts = row["timestamp"]
        assert datetime.fromisoformat(ts).astimezone() <= now, f"未来时间戳：{ts}"


def test_summary_returns_totals(env):
    s = env["client"].get("/api/stats/summary", params={"days": 7}).json()
    assert s["tokens"] > 0
    assert s["messages"] == 21            # 3 联系人 × 7 条计量消息（file 不计量）
    assert s["peers"] == 3
    assert s["top_peer"]["peer_id"]


def test_ranking_sorted_desc(env):
    items = env["client"].get("/api/stats/ranking", params={"days": 7}).json()["items"]
    assert len(items) == 3
    tokens = [i["tokens"] for i in items]
    assert tokens == sorted(tokens, reverse=True)
    assert all(i["peer_name"] for i in items)


def test_ranking_merges_quota_status(env):
    client = env["client"]
    client.put("/api/quota/rules", json={
        "peer_id": "wxid_alice", "peer_name": "Alice",
        "window_type": "daily", "limit_tokens": 500_000,
    })
    items = client.get("/api/stats/ranking", params={"days": 7}).json()["items"]
    alice = next(i for i in items if i["peer_id"] == "wxid_alice")
    assert alice["limit"] == 500_000
    assert alice["used"] > 0
    assert 0 < alice["ratio"] < 1
    assert alice["remaining"] == alice["limit"] - alice["used"]
    # 没设额度的联系人不应报错（本夹具显式关掉了默认额度）
    bob = next(i for i in items if i["peer_id"] == "wxid_bob")
    assert bob["limit"] is None


def test_ranking_uses_default_limit_when_enabled(tmp_path):
    """默认日额度开启后，没单独设额度的联系人也带得上额度（M10）。"""
    db = Database(tmp_path / "b.db")
    db.init()
    client = TestClient(create_app(db=db, quota_config=QuotaConfig(default_limit_tokens=50000)))
    client.post("/api/ingest/mock")
    items = client.get("/api/stats/ranking", params={"days": 7}).json()["items"]
    assert all(i["limit"] == 50000 for i in items)


def test_timeseries_hour_has_hourly_buckets(env):
    data = env["client"].get("/api/stats/timeseries", params={"bucket": "hour", "days": 1}).json()
    now_hour = datetime.now().astimezone().hour
    assert len(data["items"]) == now_hour + 1       # 从 0 点到当前小时
    assert data["items"][0]["label"] == "00:00"
    assert sum(i["tokens"] for i in data["items"]) > 0


def test_timeseries_day_has_seven_buckets(env):
    data = env["client"].get("/api/stats/timeseries", params={"bucket": "day", "days": 7}).json()
    assert len(data["items"]) == 7
    assert sum(i["tokens"] for i in data["items"]) > 0


def test_timeseries_peer_filter(env):
    all_items = env["client"].get("/api/stats/timeseries",
                                  params={"bucket": "day", "days": 7}).json()["items"]
    one = env["client"].get("/api/stats/timeseries",
                            params={"bucket": "day", "days": 7, "peer_id": "wxid_alice"}).json()["items"]
    assert sum(i["tokens"] for i in one) < sum(i["tokens"] for i in all_items)


def test_by_type_breakdown(env):
    items = env["client"].get("/api/stats/by-type", params={"days": 7}).json()["items"]
    types = {i["msg_type"] for i in items}
    assert {"text", "voice_call", "video_call"} <= types
    assert sum(i["tokens"] for i in items) > 0


def test_report_week_and_month(env):
    week = env["client"].get("/api/stats/report", params={"period": "week"}).json()
    assert len(week["items"]) == 7
    month = env["client"].get("/api/stats/report", params={"period": "month"}).json()
    assert len(month["items"]) == 30
    assert month["tokens"] >= week["tokens"]


def test_stats_endpoints_on_empty_db(tmp_path):
    client = TestClient(create_app(db=Database(tmp_path / "empty.db")))
    assert client.get("/api/stats/summary").json()["tokens"] == 0
    assert client.get("/api/stats/ranking").json()["items"] == []
    # 空库仍返回补齐的空桶（否则面积图会画错）
    series = client.get("/api/stats/timeseries").json()["items"]
    assert series and all(i["tokens"] == 0 for i in series)
    assert client.get("/api/stats/by-type").json()["items"] == []
