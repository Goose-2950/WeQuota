"""M5 统计聚合：把已计量消息行聚合成大屏需要的各种视图。

设计要点：
- 全部是**纯函数**（输入消息行列表，输出聚合结果），不碰数据库，便于测试与复用。
- 分桶一律在 Python 侧按**本地时区**做：SQLite 的 `strftime(...,'localtime')` 在不同平台
  依赖 TZ 环境变量，行为不稳定，这里统一用 `datetime.astimezone()`。
- 空桶必须补齐，否则 ECharts 面积图会把没有消息的时间段画成斜线。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

BUCKETS = ("hour", "day")


def _local(ts: datetime) -> datetime:
    """统一转本地时区（naive 视为 UTC，与 storage._iso 的约定一致）。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return ts.astimezone()


def _floor(ts: datetime, bucket: str) -> datetime:
    t = _local(ts)
    if bucket == "day":
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    return t.replace(minute=0, second=0, microsecond=0)


def _label(start: datetime, bucket: str) -> str:
    return start.strftime("%H:%M") if bucket == "hour" else start.strftime("%m-%d")


# ---------------------------------------------------------------- 联系人排行榜


def rank_peers(rows: Iterable[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """按 token 消耗给联系人排序（双向合并）。"""
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        tokens = int(r.get("token_count") or 0)
        item = agg.setdefault(r["peer_id"], {
            "peer_id": r["peer_id"], "peer_name": r.get("peer_name", ""),
            "tokens": 0, "messages": 0,
        })
        item["tokens"] += tokens
        item["messages"] += 1
    out = sorted(agg.values(), key=lambda x: (-x["tokens"], x["peer_id"]))
    return out[:limit] if limit else out


# ---------------------------------------------------------------- 时段分桶


def bucketize(
    rows: Iterable[dict[str, Any]],
    bucket: str = "hour",
    start: datetime | None = None,
    end: datetime | None = None,
    fill: bool = True,
) -> list[dict[str, Any]]:
    """按时段聚合 token 消耗，默认补齐空桶。"""
    if bucket not in BUCKETS:
        raise ValueError(f"bucket 必须是 {BUCKETS} 之一")

    rows = list(rows)
    if start is None and rows:
        start = min(_floor(_local(r["timestamp"]), bucket) for r in rows)
    if end is None and rows:
        end = max(_local(r["timestamp"]) for r in rows)
    if start is None or end is None:
        return []

    start = _floor(start, bucket)
    end = _local(end)

    agg: dict[datetime, dict[str, Any]] = {}
    for r in rows:
        ts = _local(r["timestamp"])
        # end 为开区间（exclusive），避免多出一个空桶
        if ts < start or ts >= end:
            continue
        key = _floor(r["timestamp"], bucket)
        item = agg.setdefault(key, {"tokens": 0, "messages": 0})
        item["tokens"] += int(r.get("token_count") or 0)
        item["messages"] += 1

    if not fill:
        return [_bucket_item(k, v, bucket) for k, v in sorted(agg.items())]

    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    out: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        v = agg.get(cursor, {"tokens": 0, "messages": 0})
        out.append(_bucket_item(cursor, v, bucket))
        cursor += step
    return out


def _bucket_item(key: datetime, value: dict[str, Any], bucket: str) -> dict[str, Any]:
    return {
        "bucket_start": key.isoformat(),
        "label": _label(key, bucket),
        "tokens": int(value.get("tokens", 0)),
        "messages": int(value.get("messages", 0)),
    }


# ---------------------------------------------------------------- 类型占比


def by_type(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = agg.setdefault(r["msg_type"], {"msg_type": r["msg_type"], "tokens": 0, "messages": 0})
        item["tokens"] += int(r.get("token_count") or 0)
        item["messages"] += 1
    return sorted(agg.values(), key=lambda x: -x["tokens"])


# ---------------------------------------------------------------- 总览 / 报表


def summarize(rows: Iterable[dict[str, Any]],
              start: datetime | None = None,
              end: datetime | None = None) -> dict[str, Any]:
    rows = list(rows)
    if start or end:
        rows = [r for r in rows
                if (start is None or _local(r["timestamp"]) >= _local(start))
                and (end is None or _local(r["timestamp"]) <= _local(end))]
    peers = {r["peer_id"] for r in rows}
    ranked = rank_peers(rows, limit=1)
    return {
        "tokens": sum(int(r.get("token_count") or 0) for r in rows),
        "messages": len(rows),
        "peers": len(peers),
        "top_peer": ranked[0] if ranked else None,
    }


def recent_dates(end: datetime, days: int = 7) -> list[datetime]:
    """返回以 end 当天结尾、长度为 days 的本地日期序列（日报表的横轴）。"""
    end_day = _floor(end, "day")
    return [end_day - timedelta(days=i) for i in range(days - 1, -1, -1)]


def daily_report(rows: Iterable[dict[str, Any]], end: datetime | None = None,
                 days: int = 7) -> list[dict[str, Any]]:
    """周/月报表：每日 token 与消息数。"""
    end = end or datetime.now().astimezone()
    days_list = recent_dates(end, days)
    return bucketize(rows, bucket="day", start=days_list[0], end=end)


# ---------------------------------------------------------------- 年度热力图


def heatmap(counts: dict[str, int], year: int,
            today: date | None = None) -> dict[str, Any]:
    """GitHub 贡献图同款年度日历：每天一个方块，按消息量分为 0–4 级。

    `counts` 的键是本地日期（YYYY-MM-DD）。返回整年所有日期（包括未来日期），
    空白天补 0，避免前端因为缺日而错位。
    """
    today = today or date.today()
    first = date(year, 1, 1)
    last = date(year, 12, 31)
    day = first

    values: list[tuple[date, int]] = []
    while day <= last:
        values.append((day, int(counts.get(day.isoformat(), 0))))
        day += timedelta(days=1)

    nonzero = sorted({n for _, n in values if n > 0})
    levels: dict[int, int] = {}
    if len(nonzero) == 1:
        levels[nonzero[0]] = 4
    elif len(nonzero) <= 4:
        for index, value in enumerate(nonzero):
            levels[value] = 1 + round(index / (len(nonzero) - 1) * 3)
    elif nonzero:
        q1 = nonzero[max(0, int((len(nonzero) - 1) * 0.25))]
        q2 = nonzero[max(0, int((len(nonzero) - 1) * 0.50))]
        q3 = nonzero[max(0, int((len(nonzero) - 1) * 0.75))]
        for value in nonzero:
            levels[value] = 1 if value <= q1 else 2 if value <= q2 else 3 if value <= q3 else 4

    items = [
        {
            "date": d.isoformat(),
            "count": n,
            "level": levels.get(n, 0),
            "future": d > today,
        }
        for d, n in values
    ]
    total = sum(n for _, n in values)
    active_days = sum(1 for _, n in values if n > 0)
    peak = max(values, key=lambda item: item[1]) if values else (first, 0)
    return {
        "year": year,
        "total": total,
        "active_days": active_days,
        "max": peak[1],
        "peak": {"date": peak[0].isoformat(), "count": peak[1]} if peak[1] else None,
        "items": items,
    }
