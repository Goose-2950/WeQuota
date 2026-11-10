"""M5 统计聚合测试（TDD）：排行榜 / 时段分桶 / 类型占比 / 周期报表。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from wequota.stats import (
    bucketize,
    by_type,
    heatmap,
    rank_peers,
    recent_dates,
    summarize,
)
from wequota.storage import Database

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 12, 15, 30, tzinfo=TZ)


def rows_of(db: Database) -> list[dict]:
    return db.iter_metered("wxid_demo")


def seed(db: Database, items: list[tuple[str, str, datetime, int, str]]) -> None:
    """(peer_id, peer_name, timestamp, tokens, msg_type)"""
    from wequota.contracts import MessageEnvelope

    msgs = []
    for i, (peer, name, ts, tokens, mtype) in enumerate(items):
        meta = {"duration_s": 60} if mtype.endswith("call") else None
        m = MessageEnvelope(
            account_id="wxid_demo", msg_id=f"m{i}", peer_id=peer, peer_name=name,
            direction="send", msg_type=mtype, timestamp=ts, text="x", media_meta=meta,
        )
        m.token_count = tokens
        msgs.append(m)
    db.upsert_messages(msgs)


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "s.db")
    d.init()
    return d


# ---------- 联系人排行榜 ----------

def test_rank_peers_sorted_by_tokens(db):
    seed(db, [
        ("p1", "Alice", NOW, 100, "text"),
        ("p2", "Bob", NOW, 500, "video_call"),
        ("p1", "Alice", NOW, 200, "image"),
        ("p3", "Carol", NOW, 400, "voice_call"),
    ])
    rank = rank_peers(rows_of(db))
    assert [r["peer_id"] for r in rank] == ["p2", "p3", "p1"]
    assert rank[0]["tokens"] == 500
    assert rank[1]["tokens"] == 400
    # Alice 两条合并
    alice = next(r for r in rank if r["peer_id"] == "p1")
    assert alice["tokens"] == 300 and alice["messages"] == 2


def test_rank_peers_limit(db):
    seed(db, [(f"p{i}", f"P{i}", NOW, i * 10, "text") for i in range(1, 8)])
    assert len(rank_peers(rows_of(db), limit=3)) == 3


def test_rank_peers_ignores_null_tokens(db):
    """未计量的消息（token_count 为空）不得计入统计。"""
    from wequota.contracts import MessageEnvelope

    m = MessageEnvelope(
        account_id="wxid_demo", msg_id="x", peer_id="p1", peer_name="A",
        direction="send", msg_type="text", timestamp=NOW, text="未计量",
    )
    db.upsert_messages([m])
    seed(db, [("p1", "A", NOW, 50, "text")])
    rank = rank_peers(rows_of(db))
    assert rank[0]["tokens"] == 50 and rank[0]["messages"] == 1


# ---------- 时段分桶 ----------

def test_bucketize_hour_fills_gaps(db):
    """按小时分桶必须补齐空桶，否则面积图会画错。"""
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    seed(db, [
        ("p1", "A", day.replace(hour=9), 100, "text"),
        ("p1", "A", day.replace(hour=9, minute=30), 50, "text"),
        ("p2", "B", day.replace(hour=14), 300, "image"),
    ])
    buckets = bucketize(rows_of(db), bucket="hour", start=day, end=day + timedelta(days=1))
    assert len(buckets) == 24
    assert buckets[9]["tokens"] == 150
    assert buckets[14]["tokens"] == 300
    assert buckets[0]["tokens"] == 0
    assert sum(b["tokens"] for b in buckets) == 450


def test_bucketize_day_fills_gaps(db):
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - timedelta(days=2)
    seed(db, [
        ("p1", "A", start, 100, "text"),
        ("p1", "A", day, 200, "text"),
    ])
    # end 为开区间：bucket 为 day-2 / day-1 / day 三天
    buckets = bucketize(rows_of(db), bucket="day", start=start, end=day + timedelta(days=1))
    assert len(buckets) == 3
    assert [b["tokens"] for b in buckets] == [100, 0, 200]
    assert sum(b["messages"] for b in buckets) == 2


def test_bucketize_filters_by_time_range(db):
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    seed(db, [
        ("p1", "A", day - timedelta(days=5), 999, "text"),   # 区间外
        ("p1", "A", day, 10, "text"),
    ])
    buckets = bucketize(rows_of(db), bucket="day", start=day, end=day + timedelta(days=1))
    assert sum(b["tokens"] for b in buckets) == 10


def test_bucketize_supports_peer_filter(db):
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    seed(db, [
        ("p1", "A", day, 100, "text"),
        ("p2", "B", day, 900, "text"),
    ])
    rows = [r for r in rows_of(db) if r["peer_id"] == "p1"]
    buckets = bucketize(rows, bucket="day", start=day, end=day + timedelta(days=1))
    assert sum(b["tokens"] for b in buckets) == 100


# ---------- 类型占比 ----------

def test_by_type_breakdown(db):
    seed(db, [
        ("p1", "A", NOW, 100, "text"),
        ("p1", "A", NOW, 200, "text"),
        ("p2", "B", NOW, 500, "video_call"),
    ])
    items = by_type(rows_of(db))
    top = items[0]
    assert top["msg_type"] == "video_call" and top["tokens"] == 500
    text = next(i for i in items if i["msg_type"] == "text")
    assert text["tokens"] == 300 and text["messages"] == 2


# ---------- 总览与报表 ----------

def test_summarize(db):
    seed(db, [
        ("p1", "A", NOW, 100, "text"),
        ("p2", "B", NOW, 500, "video_call"),
        ("p2", "B", NOW - timedelta(days=40), 999, "text"),  # 区间外
    ])
    s = summarize(rows_of(db), start=NOW - timedelta(days=7), end=NOW + timedelta(days=1))
    assert s["tokens"] == 600
    assert s["messages"] == 2
    assert s["peers"] == 2
    assert s["top_peer"]["peer_id"] == "p2"


def test_recent_dates_covers_requested_days():
    end = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    days = recent_dates(end=end, days=7)
    assert len(days) == 7
    assert days[-1] == end
    assert (end - days[0]).days == 6


# ---------- 年度消息热力图 ----------

def test_heatmap_fills_every_day_and_marks_levels():
    counts = {
        "2026-01-01": 1,
        "2026-01-02": 2,
        "2026-01-03": 3,
        "2026-01-04": 8,
        "2026-03-10": 4,
    }
    data = heatmap(counts, year=2026, today=date(2026, 11, 2))
    assert data["year"] == 2026
    assert data["total"] == 18
    assert data["active_days"] == 5
    assert len(data["items"]) == 365
    assert data["peak"] == {"date": "2026-01-04", "count": 8}

    by_date = {item["date"]: item for item in data["items"]}
    assert by_date["2026-01-01"]["count"] == 1
    assert by_date["2026-01-01"]["level"] >= 1
    assert by_date["2026-01-04"]["level"] == 4
    assert by_date["2026-01-05"]["level"] == 0
    assert by_date["2026-11-03"]["future"] is True


def test_heatmap_leap_year_and_empty_data():
    data = heatmap({}, year=2024, today=date(2024, 6, 1))
    assert len(data["items"]) == 366
    assert data["total"] == 0
    assert data["peak"] is None
    assert all(item["level"] == 0 for item in data["items"])
