"""SQLite 存储层。

设计要点：
- 单文件 SQLite，WAL 模式，适合单机自部署。
- messages 以 (account_id, msg_id) 幂等去重，重复投递只更新不重复计数。
- timestamp=事件时间（统计归属），ingested_at=入库时间，两者差值用于识别「补录」。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import METERED_TYPES, MONEY_TYPES, MessageEnvelope

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    peer_id       TEXT NOT NULL,
    peer_name     TEXT DEFAULT '',
    msg_id        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    msg_type      TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    text          TEXT,
    media_path    TEXT,
    media_meta    TEXT,
    extra         TEXT,
    token_count   INTEGER,
    meter_detail  TEXT,
    quota_applied INTEGER DEFAULT 0,
    money_alerted INTEGER DEFAULT 0,
    UNIQUE (account_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_time  ON messages (account_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_peer  ON messages (account_id, peer_id, timestamp);

CREATE TABLE IF NOT EXISTS quota_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    peer_id       TEXT NOT NULL,
    peer_name     TEXT DEFAULT '',
    window_type   TEXT NOT NULL,
    custom_start  TEXT,
    custom_end    TEXT,
    limit_tokens  INTEGER NOT NULL,
    warn_ratio    REAL DEFAULT 0.8,
    enabled       INTEGER DEFAULT 1,
    updated_at    TEXT,
    UNIQUE (account_id, peer_id)
);

CREATE TABLE IF NOT EXISTS quota_usage (
    account_id    TEXT NOT NULL,
    peer_id       TEXT NOT NULL,
    window_start  TEXT NOT NULL,
    used_tokens   INTEGER DEFAULT 0,
    alerted_warn  INTEGER DEFAULT 0,
    alerted_over  INTEGER DEFAULT 0,
    backfill_only INTEGER DEFAULT 0,
    PRIMARY KEY (account_id, peer_id, window_start)
);

CREATE TABLE IF NOT EXISTS alert_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    peer_id      TEXT NOT NULL,
    level        TEXT NOT NULL,
    used_tokens  INTEGER,
    limit_tokens INTEGER,
    window_start TEXT,
    created_at   TEXT,
    channels     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_logs_time ON alert_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS transcripts (
    key        TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    model      TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    wechat_id    TEXT DEFAULT '',
    avatar_url   TEXT DEFAULT '',
    stable_id    TEXT DEFAULT '',
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    account_id   TEXT NOT NULL,
    peer_id      TEXT NOT NULL,
    peer_name    TEXT DEFAULT '',
    updated_at   TEXT,
    PRIMARY KEY (account_id, peer_id)
);

CREATE TABLE IF NOT EXISTS sync_cursor (
    account_id     TEXT PRIMARY KEY,
    last_msg_id    TEXT,
    last_timestamp TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS peer_cursor (
    account_id       TEXT NOT NULL,
    peer_id          TEXT NOT NULL,
    last_create_time INTEGER DEFAULT 0,
    last_local_id    INTEGER DEFAULT 0,
    updated_at       TEXT,
    PRIMARY KEY (account_id, peer_id)
);

CREATE TABLE IF NOT EXISTS account_aliases (
    alias_id     TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    method       TEXT DEFAULT 'manual',
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 入库时间比事件时间晚于该阈值即视为「补录」（手机聊天在 PC 重新登录后补读）
BACKFILL_THRESHOLD = timedelta(minutes=5)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为已存在的旧库补列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(alert_logs)").fetchall()}
        if "channels" not in cols:
            conn.execute("ALTER TABLE alert_logs ADD COLUMN channels TEXT")
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "extra" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN extra TEXT")
        if "money_alerted" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN money_alerted INTEGER DEFAULT 0")
        account_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        for column in ("wechat_id", "avatar_url", "stable_id"):
            if column not in account_cols:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} TEXT DEFAULT ''")
        peer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(peer_cursor)").fetchall()}
        if "last_local_id" not in peer_cols:
            conn.execute("ALTER TABLE peer_cursor ADD COLUMN last_local_id INTEGER DEFAULT 0")

    def upsert_messages(self, messages: Iterable[MessageEnvelope]) -> int:
        """批量入账，重复 (account_id, msg_id) 跳过。返回实际写入条数。"""
        rows = []
        now = _iso(datetime.now(timezone.utc))
        for m in messages:
            rows.append((
                m.account_id, m.peer_id, m.peer_name, m.msg_id, m.direction,
                m.msg_type, _iso(m.timestamp), now, m.text, m.media_path,
                json.dumps(m.media_meta, ensure_ascii=False) if m.media_meta else None,
                json.dumps(m.extra, ensure_ascii=False) if m.extra else None,
                m.token_count,
                json.dumps(m.meter_detail, ensure_ascii=False) if m.meter_detail else None,
            ))
        if not rows:
            return 0
        cur = self._connect().cursor()
        try:
            cur.executemany(
                """INSERT INTO messages
                   (account_id, peer_id, peer_name, msg_id, direction, msg_type,
                    timestamp, ingested_at, text, media_path, media_meta, extra,
                    token_count, meter_detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (account_id, msg_id) DO NOTHING""",
                rows,
            )
            written = cur.rowcount or 0
            cur.connection.commit()
            return written if written >= 0 else 0
        finally:
            cur.connection.close()

    def query_messages(
        self,
        account_id: str | None = None,
        peer_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        msg_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        if peer_id:
            sql += " AND peer_id = ?"
            params.append(peer_id)
        if start:
            sql += " AND timestamp >= ?"
            params.append(_iso(start))
        if end:
            sql += " AND timestamp <= ?"
            params.append(_iso(end))
        if msg_type:
            sql += " AND msg_type = ?"
            params.append(msg_type)
        sql += " ORDER BY timestamp ASC LIMIT ?"
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["timestamp"] = _parse_iso(d["timestamp"])
            d["ingested_at"] = _parse_iso(d["ingested_at"])
            out.append(d)
        return out

    def count_messages(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()

    def list_peer_summaries(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """列出全部联系人与消息数（联系人表 + 消息表合并，不截断 TOP N）。"""
        account = account_id or ""
        sql = (
            "SELECT p.peer_id, "
            "COALESCE(NULLIF(MAX(p.peer_name), ''), p.peer_id) AS peer_name, "
            "COUNT(m.id) AS messages "
            "FROM ("
            "  SELECT peer_id, peer_name FROM contacts WHERE account_id = ? "
            "  UNION ALL "
            "  SELECT peer_id, peer_name FROM messages WHERE account_id = ?"
            ") p "
            "LEFT JOIN messages m ON m.account_id = ? AND m.peer_id = p.peer_id "
            "GROUP BY p.peer_id "
            "ORDER BY messages DESC, peer_name ASC"
        )
        conn = self._connect()
        try:
            rows = conn.execute(sql, (account, account, account)).fetchall()
        finally:
            conn.close()
        return [
            {
                "peer_id": r["peer_id"],
                "peer_name": r["peer_name"] or r["peer_id"],
                "messages": int(r["messages"] or 0),
            }
            for r in rows
        ]

    def upsert_contacts(self, account_id: str, contacts: Iterable[dict[str, str]]) -> int:
        """记录账号通讯录（只存 peer_id 与显示名），供联系人筛选显示无消息联系人。"""
        now = _iso(datetime.now(timezone.utc))
        rows = [
            (account_id, str(c.get("username") or ""), str(c.get("name") or ""), now)
            for c in contacts
            if c.get("username")
        ]
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO contacts (account_id, peer_id, peer_name, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT (account_id, peer_id) DO UPDATE SET
                     peer_name = excluded.peer_name,
                     updated_at = excluded.updated_at""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)

    def pending_messages(self, limit: int = 500) -> list[dict[str, Any]]:
        """尚未计量的消息（token_count 为空且类型需要计量）。"""
        placeholders = ",".join("?" * len(METERED_TYPES))
        sql = (
            f"SELECT * FROM messages WHERE token_count IS NULL "
            f"AND msg_type IN ({placeholders}) ORDER BY timestamp ASC LIMIT ?"
        )
        conn = self._connect()
        try:
            rows = conn.execute(sql, (*METERED_TYPES, limit)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def reset_metering(self) -> int:
        """清空计量结果与限额应用标记，用于「换模型/改系数后重算」。"""
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE messages SET token_count=NULL, meter_detail=NULL, quota_applied=0"
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # ---------- 限额规则 ----------

    def upsert_rule(self, rule: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO quota_rules
                   (account_id, peer_id, peer_name, window_type, custom_start, custom_end,
                    limit_tokens, warn_ratio, enabled, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (account_id, peer_id) DO UPDATE SET
                     peer_name=excluded.peer_name, window_type=excluded.window_type,
                     custom_start=excluded.custom_start, custom_end=excluded.custom_end,
                     limit_tokens=excluded.limit_tokens, warn_ratio=excluded.warn_ratio,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    rule["account_id"], rule["peer_id"], rule.get("peer_name", ""),
                    rule["window_type"], rule.get("custom_start"), rule.get("custom_end"),
                    rule["limit_tokens"], rule.get("warn_ratio", 0.8),
                    1 if rule.get("enabled", True) else 0, _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_rule(self, account_id: str, peer_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM quota_rules WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def list_rules(self, account_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quota_rules"
        params: list[Any] = []
        if account_id:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def delete_rule(self, account_id: str, peer_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM quota_rules WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 窗口用量 ----------

    def add_usage(self, account_id: str, peer_id: str, window_start: datetime, tokens: int,
                  backfill: bool = False) -> int:
        """累加窗口用量，返回累加后的总量。"""
        key = _iso(window_start)
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO quota_usage (account_id, peer_id, window_start, used_tokens, backfill_only)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT (account_id, peer_id, window_start) DO UPDATE SET
                     used_tokens = used_tokens + excluded.used_tokens,
                     backfill_only = MAX(backfill_only, excluded.backfill_only)""",
                (account_id, peer_id, key, tokens, 1 if backfill else 0),
            )
            conn.commit()
            row = conn.execute(
                "SELECT used_tokens FROM quota_usage WHERE account_id=? AND peer_id=? AND window_start=?",
                (account_id, peer_id, key),
            ).fetchone()
            return int(row[0]) if row else tokens
        finally:
            conn.close()

    def get_usage(self, account_id: str, peer_id: str, window_start: datetime) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM quota_usage WHERE account_id=? AND peer_id=? AND window_start=?",
                (account_id, peer_id, _iso(window_start)),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def mark_alerted(self, account_id: str, peer_id: str, window_start: datetime, level: str) -> None:
        field = "alerted_warn" if level == "warn" else "alerted_over"
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE quota_usage SET {field} = 1 "
                f"WHERE account_id=? AND peer_id=? AND window_start=?",
                (account_id, peer_id, _iso(window_start)),
            )
            conn.commit()
        finally:
            conn.close()

    def log_alert(self, account_id: str, peer_id: str, level: str, used: int,
                  limit: int | None, window_start: datetime | None) -> int:
        """记录一条告警，返回自增 id（供提醒通道回填投递结果）。"""
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO alert_logs (account_id, peer_id, level, used_tokens,
                   limit_tokens, window_start, created_at) VALUES (?,?,?,?,?,?,?)""",
                (account_id, peer_id, level, used, limit,
                 _iso(window_start) if window_start else None, _iso(datetime.now(timezone.utc))),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def update_alert_channels(self, alert_id: int, channels: dict[str, Any]) -> None:
        """回填各通道投递结果，便于事后排查「提醒发出去没有」。"""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE alert_logs SET channels = ? WHERE id = ?",
                (json.dumps(channels, ensure_ascii=False), alert_id),
            )
            conn.commit()
        finally:
            conn.close()

    def recent_alerts(self, account_id: str | None = None,
                      limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alert_logs"
        params: list[Any] = []
        if account_id:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["channels"] = json.loads(d["channels"]) if d.get("channels") else {}
            out.append(d)
        return out

    def pending_quota_messages(self, limit: int = 500) -> list[dict[str, Any]]:
        """已计量但尚未计入限额窗口的消息。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE token_count IS NOT NULL AND quota_applied = 0 "
                "ORDER BY timestamp ASC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def mark_quota_applied(self, message_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE messages SET quota_applied = 1 WHERE id = ?", (message_id,))
            conn.commit()
        finally:
            conn.close()

    def total_tokens(
        self,
        account_id: str,
        peer_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """统计窗口/时间区间内的 token 总量（按事件时间）。"""
        sql = "SELECT COALESCE(SUM(token_count), 0) FROM messages WHERE account_id = ?"
        params: list[Any] = [account_id]
        if peer_id:
            sql += " AND peer_id = ?"
            params.append(peer_id)
        if start:
            sql += " AND timestamp >= ?"
            params.append(_iso(start))
        if end:
            sql += " AND timestamp <= ?"
            params.append(_iso(end))
        conn = self._connect()
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        finally:
            conn.close()

    def iter_metered(
        self,
        account_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        peer_id: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        """取已计量的消息行（供统计聚合使用）。

        只返回 token_count 非空的行；时间过滤按事件时间。分桶/排序交给
        `wequota.stats`，避免 SQLite 的时区函数在跨平台下行为不一致。
        """
        sql = ("SELECT id, peer_id, peer_name, msg_type, timestamp, token_count "
               "FROM messages WHERE account_id = ? AND token_count IS NOT NULL")
        params: list[Any] = [account_id]
        if peer_id:
            sql += " AND peer_id = ?"
            params.append(peer_id)
        if start:
            sql += " AND timestamp >= ?"
            params.append(_iso(start))
        if end:
            sql += " AND timestamp <= ?"
            params.append(_iso(end))
        sql += " ORDER BY timestamp ASC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["timestamp"] = _parse_iso(d["timestamp"])
            out.append(d)
        return out

    def daily_message_counts(
        self, account_id: str, start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        """按本地日期统计消息量（含未计量消息），用于年度活跃热力图。

        时间范围用 UTC 边界配合 `date(...,'localtime')`，保证跨年边界仍按
        用户本地日历归日；索引由 (account_id, timestamp) 覆盖。
        """
        conn = self._connect()
        try:
            # 消息 timestamp 可能带本地时区（导入）或 UTC（采集器）。字符串比较
            # 在不同 offset 下会把跨年边界算错，因此左右各放宽一天；日期分组后
            # 由 heatmap() 只取目标年份，不会把相邻年份算进总数。
            rows = conn.execute(
                """SELECT date(timestamp, 'localtime') AS day, COUNT(*) AS n
                   FROM messages
                   WHERE account_id = ? AND timestamp >= ? AND timestamp < ?
                   GROUP BY day ORDER BY day""",
                (account_id, _iso(start - timedelta(days=1)),
                 _iso(end + timedelta(days=1))),
            ).fetchall()
        finally:
            conn.close()
        return [{"date": r["day"], "count": int(r["n"])}
                for r in rows if r["day"]]

    def message_years(self, account_id: str) -> list[int]:
        """有消息的本地年份，最近的在前。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT strftime('%Y', timestamp, 'localtime') AS year
                   FROM messages WHERE account_id = ? ORDER BY year DESC""",
                (account_id,),
            ).fetchall()
        finally:
            conn.close()
        years: list[int] = []
        for row in rows:
            try:
                years.append(int(row["year"]))
            except (TypeError, ValueError):
                continue
        return years

    def is_backfill(self, message_id: int) -> bool:
        """该条消息是否为「补录」：入库时间明显晚于事件时间。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT timestamp, ingested_at FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        event_time = _parse_iso(row["timestamp"])
        ingested = _parse_iso(row["ingested_at"])
        return ingested - event_time > BACKFILL_THRESHOLD

    def update_token(self, message_id: int, token_count: int, detail: dict | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE messages SET token_count = ?, meter_detail = ? WHERE id = ?",
                (token_count, json.dumps(detail, ensure_ascii=False) if detail else None, message_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 账号（M7 多微信号隔离） ----------

    def upsert_account(self, account_id: str, display_name: str = "",
                       wechat_id: str = "", avatar_url: str = "",
                       stable_id: str = "") -> None:
        """登记微信账号；空字段保留已有值（避免每轮覆盖头像/名称）。"""
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO accounts
                     (account_id, display_name, wechat_id, avatar_url, stable_id,
                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (account_id) DO UPDATE SET
                     display_name = CASE WHEN excluded.display_name <> ''
                                         THEN excluded.display_name ELSE display_name END,
                     wechat_id = CASE WHEN excluded.wechat_id <> ''
                                      THEN excluded.wechat_id ELSE wechat_id END,
                     avatar_url = CASE WHEN excluded.avatar_url <> ''
                                       THEN excluded.avatar_url ELSE avatar_url END,
                     stable_id = CASE WHEN excluded.stable_id <> ''
                                      THEN excluded.stable_id ELSE stable_id END,
                     updated_at = excluded.updated_at""",
                (account_id, display_name, wechat_id, avatar_url, stable_id,
                 _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc))),
            )
            conn.commit()
        finally:
            conn.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        """列出库里出现过的所有微信账号（显式登记 + 有消息 + 有游标）。"""
        conn = self._connect()
        try:
            profiles = {
                r["account_id"]: {
                    "display_name": r["display_name"] or "",
                    "wechat_id": r["wechat_id"] or "",
                    "avatar_url": r["avatar_url"] or "",
                    "stable_id": r["stable_id"] or "",
                    "updated_at": r["updated_at"] or "",
                }
                for r in conn.execute(
                    "SELECT account_id, display_name, wechat_id, avatar_url, stable_id, "
                    "updated_at FROM accounts")
            }
            ids = set(profiles)
            for sql in ("SELECT DISTINCT account_id FROM messages",
                        "SELECT account_id FROM sync_cursor"):
                ids.update(r["account_id"] for r in conn.execute(sql))
            out: list[dict[str, Any]] = []
            for account_id in sorted(ids):
                stat = conn.execute(
                    "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM messages WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                cursor = conn.execute(
                    "SELECT last_timestamp, updated_at FROM sync_cursor WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                profile = profiles.get(account_id, {})
                out.append({
                    "account_id": account_id,
                    "display_name": profile.get("display_name", ""),
                    "wechat_id": profile.get("wechat_id", ""),
                    "avatar_url": profile.get("avatar_url", ""),
                    "stable_id": profile.get("stable_id", ""),
                    "updated_at": profile.get("updated_at", ""),
                    "messages": int(stat["n"] or 0),
                    "last_message_at": stat["last"],
                    "last_sync_at": cursor["updated_at"] if cursor else None,
                    "last_timestamp": cursor["last_timestamp"] if cursor else None,
                })
        finally:
            conn.close()
        return out

    def merge_accounts(self, sources: Iterable[str], target: str) -> dict[str, Any]:
        """把同一稳定身份的历史别名合并到最新账号。

        只在用户确认后调用。消息按 msg_id 去重；额度规则遇到同联系人时保留
        target 的规则；用量清空后交给 QuotaEngine 重新按消息计算。
        """
        target = str(target or "").strip()
        source_ids = [str(s) for s in dict.fromkeys(sources)
                      if s and str(s) != target]
        if not target or not source_ids:
            return {"target": target, "sources": [], "messages": 0}

        conn = self._connect()
        moved_messages = 0
        try:
            with conn:
                target_row = conn.execute(
                    "SELECT * FROM accounts WHERE account_id = ?", (target,)
                ).fetchone()
                for source in source_ids:
                    source_row = conn.execute(
                        "SELECT * FROM accounts WHERE account_id = ?", (source,)
                    ).fetchone()
                    # 消息：先删掉 target 已有的同 msg_id，再迁移其余消息。
                    conn.execute(
                        "DELETE FROM messages WHERE account_id = ? AND msg_id IN "
                        "(SELECT msg_id FROM messages WHERE account_id = ?)",
                        (source, target),
                    )
                    moved_messages += conn.execute(
                        "UPDATE messages SET account_id = ? WHERE account_id = ?",
                        (target, source),
                    ).rowcount or 0
                    # 额度规则：target 已有同联系人规则时保留 target。
                    conn.execute(
                        "DELETE FROM quota_rules WHERE account_id = ? AND peer_id IN "
                        "(SELECT peer_id FROM quota_rules WHERE account_id = ?)",
                        (source, target),
                    )
                    conn.execute(
                        "UPDATE quota_rules SET account_id = ? WHERE account_id = ?",
                        (target, source),
                    )
                    # 用量由 messages 重建，不直接相加，避免同一记录重复计量。
                    conn.execute("DELETE FROM quota_usage WHERE account_id = ?", (source,))
                    conn.execute(
                        "UPDATE alert_logs SET account_id = ? WHERE account_id = ?",
                        (target, source),
                    )
                    # 同步游标：target 已有游标时丢弃 source 的旧游标。
                    existing_cursor = conn.execute(
                        "SELECT 1 FROM sync_cursor WHERE account_id = ?", (target,)
                    ).fetchone()
                    if existing_cursor:
                        conn.execute("DELETE FROM sync_cursor WHERE account_id = ?", (source,))
                    else:
                        conn.execute(
                            "UPDATE sync_cursor SET account_id = ? WHERE account_id = ?",
                            (target, source),
                        )
                    # per-peer 游标取较大的 (create_time, local_id)。
                    for row in conn.execute(
                        "SELECT peer_id, last_create_time, last_local_id, updated_at "
                        "FROM peer_cursor WHERE account_id = ?", (source,)
                    ).fetchall():
                        old = conn.execute(
                            "SELECT last_create_time, last_local_id, updated_at "
                            "FROM peer_cursor WHERE account_id = ? AND peer_id = ?",
                            (target, row["peer_id"])
                        ).fetchone()
                        if old:
                            old_key = (int(old["last_create_time"] or 0),
                                       int(old["last_local_id"] or 0))
                            new_key = (int(row["last_create_time"] or 0),
                                       int(row["last_local_id"] or 0))
                            if new_key > old_key:
                                conn.execute(
                                    "UPDATE peer_cursor SET last_create_time = ?, "
                                    "last_local_id = ?, updated_at = ? "
                                    "WHERE account_id = ? AND peer_id = ?",
                                    (new_key[0], new_key[1], row["updated_at"],
                                     target, row["peer_id"]),
                                )
                        else:
                            conn.execute(
                                "INSERT INTO peer_cursor "
                                "(account_id, peer_id, last_create_time, last_local_id, updated_at) "
                                "VALUES (?,?,?,?,?)",
                                (target, row["peer_id"], row["last_create_time"],
                                 row["last_local_id"], row["updated_at"]),
                            )
                        conn.execute(
                            "DELETE FROM peer_cursor WHERE account_id = ? AND peer_id = ?",
                            (source, row["peer_id"]),
                        )
                    # 通讯录：target 已有的联系人保留，其余迁移。
                    conn.execute(
                        "DELETE FROM contacts WHERE account_id = ? AND peer_id IN "
                        "(SELECT peer_id FROM contacts WHERE account_id = ?)",
                        (source, target),
                    )
                    conn.execute(
                        "UPDATE contacts SET account_id = ? WHERE account_id = ?",
                        (target, source),
                    )
                    # 账号资料：target 缺的字段用 source 的补。
                    if target_row is None:
                        conn.execute(
                            """INSERT INTO accounts
                               (account_id, display_name, wechat_id, avatar_url,
                                stable_id, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?)""",
                            (target,
                             source_row["display_name"] if source_row else "",
                             source_row["wechat_id"] if source_row else "",
                             source_row["avatar_url"] if source_row else "",
                             source_row["stable_id"] if source_row else "",
                             _iso(datetime.now(timezone.utc)),
                             _iso(datetime.now(timezone.utc))),
                        )
                        target_row = conn.execute(
                            "SELECT * FROM accounts WHERE account_id = ?", (target,)
                        ).fetchone()
                    elif source_row is not None:
                        conn.execute(
                            """UPDATE accounts SET
                                 display_name = CASE WHEN display_name <> '' THEN display_name ELSE ? END,
                                 wechat_id = CASE WHEN wechat_id <> '' THEN wechat_id ELSE ? END,
                                 avatar_url = CASE WHEN avatar_url <> '' THEN avatar_url ELSE ? END,
                                 stable_id = CASE WHEN stable_id <> '' THEN stable_id ELSE ? END,
                                 updated_at = ?
                               WHERE account_id = ?""",
                            (source_row["display_name"], source_row["wechat_id"],
                             source_row["avatar_url"], source_row["stable_id"],
                             _iso(datetime.now(timezone.utc)), target),
                        )
                    conn.execute("DELETE FROM accounts WHERE account_id = ?", (source,))
                    conn.execute(
                        """INSERT OR REPLACE INTO account_aliases
                           (alias_id, canonical_id, method, created_at)
                           VALUES (?,?,?,?)""",
                        (source, target, "manual", _iso(datetime.now(timezone.utc))),
                    )
                    old_key = f"money_alert_since:{source}"
                    new_key = f"money_alert_since:{target}"
                    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (new_key,)).fetchone():
                        conn.execute("DELETE FROM meta WHERE key = ?", (old_key,))
                    else:
                        conn.execute("UPDATE OR IGNORE meta SET key = ? WHERE key = ?",
                                     (new_key, old_key))
                        conn.execute("DELETE FROM meta WHERE key = ?", (old_key,))
                # 重新计算 target 的额度用量。
                conn.execute("DELETE FROM quota_usage WHERE account_id = ?", (target,))
                conn.execute(
                    "UPDATE messages SET quota_applied = 0 WHERE account_id = ?", (target,)
                )
        finally:
            conn.close()
        return {"target": target, "sources": source_ids, "messages": moved_messages}

    def get_cursor(self, account_id: str) -> tuple[str | None, datetime | None]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_msg_id, last_timestamp FROM sync_cursor WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None, None
        ts = _parse_iso(row["last_timestamp"]) if row["last_timestamp"] else None
        return row["last_msg_id"], ts

    def set_cursor(self, account_id: str, last_msg_id: str | None, last_timestamp: datetime | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO sync_cursor (account_id, last_msg_id, last_timestamp, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT (account_id) DO UPDATE SET
                     last_msg_id=excluded.last_msg_id,
                     last_timestamp=excluded.last_timestamp,
                     updated_at=excluded.updated_at""",
                (account_id, last_msg_id, _iso(last_timestamp) if last_timestamp else None,
                 _iso(datetime.now(timezone.utc))),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 每联系人采集游标（M7 直读解密副本：按 create_time 增量） ----------

    def get_peer_cursor(self, account_id: str, peer_id: str) -> int:
        """返回该联系人上次采集到的最大 create_time（epoch 秒）；无记录返回 0。"""
        return self.get_peer_cursor_state(account_id, peer_id)[0]

    def get_peer_cursor_state(self, account_id: str, peer_id: str) -> tuple[int, int]:
        """返回 (last_create_time, last_local_id)。

        `last_local_id` 用于处理同一秒内多条消息：只按时间推进会把这一秒的
        整组消息反复读出来，表现为每轮都“读取 N 条”。旧库默认 0，仍按 >=
        兼容；新游标会同时比较 local_id。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_create_time, last_local_id FROM peer_cursor "
                "WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            ).fetchone()
            if not row:
                return 0, 0
            return int(row["last_create_time"] or 0), int(row["last_local_id"] or 0)
        finally:
            conn.close()

    def set_peer_cursor(self, account_id: str, peer_id: str, last_create_time: int) -> None:
        self.set_peer_cursor_state(account_id, peer_id, last_create_time, 0)

    def set_peer_cursor_state(self, account_id: str, peer_id: str,
                              last_create_time: int, last_local_id: int = 0) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO peer_cursor
                     (account_id, peer_id, last_create_time, last_local_id, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT (account_id, peer_id) DO UPDATE SET
                     last_create_time = CASE
                       WHEN excluded.last_create_time > peer_cursor.last_create_time
                         OR (excluded.last_create_time = peer_cursor.last_create_time
                             AND excluded.last_local_id > peer_cursor.last_local_id)
                       THEN excluded.last_create_time ELSE peer_cursor.last_create_time END,
                     last_local_id = CASE
                       WHEN excluded.last_create_time > peer_cursor.last_create_time
                         OR (excluded.last_create_time = peer_cursor.last_create_time
                             AND excluded.last_local_id > peer_cursor.last_local_id)
                       THEN excluded.last_local_id ELSE peer_cursor.last_local_id END,
                     updated_at = excluded.updated_at""",
                (account_id, peer_id, int(last_create_time), int(last_local_id),
                 _iso(datetime.now(timezone.utc))),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 通用键值（跟踪起点等） ----------

    def get_meta(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO meta (key, value) VALUES (?,?)
                   ON CONFLICT (key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def ensure_money_alert_since(self, account_id: str,
                                 when: datetime | None = None,
                                 grace_seconds: float = 0) -> datetime:
        """返回该账号的资金提醒跟踪起点；首次调用时写入当前时间。

        首次全量回填的历史转账/红包早于该起点，只入库不提醒。`grace_seconds`
        只在首次写入时生效，用于避免「刚创建起点就吞掉刚到达的事件」。
        """
        key = f"money_alert_since:{account_id}"
        value = self.get_meta(key)
        if value:
            return _parse_iso(value)
        cutoff = when or (datetime.now(timezone.utc)
                          - timedelta(seconds=max(grace_seconds, 0)))
        self.set_meta(key, _iso(cutoff))
        return cutoff

    # ---------- 转账 / 红包资金事件 ----------

    def pending_money_messages(self, account_id: str | None = None,
                               since: datetime | None = None,
                               limit: int = 500) -> list[dict[str, Any]]:
        """尚未处理（未发资金提醒）的转账/红包消息，可按账号与时间过滤。"""
        placeholders = ",".join("?" * len(MONEY_TYPES))
        sql = (f"SELECT * FROM messages WHERE msg_type IN ({placeholders}) "
               f"AND money_alerted = 0")
        params: list[Any] = list(MONEY_TYPES)
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        if since is not None:
            sql += " AND timestamp >= ?"
            params.append(_iso(since))
        sql += " ORDER BY timestamp ASC LIMIT ?"
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["timestamp"] = _parse_iso(d["timestamp"])
            d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
            out.append(d)
        return out

    def mark_money_alerted_before(self, account_id: str, before: datetime) -> None:
        """把跟踪起点之前的历史资金事件静默标记为已处理。"""
        placeholders = ",".join("?" * len(MONEY_TYPES))
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE messages SET money_alerted = 1 "
                f"WHERE account_id = ? AND money_alerted = 0 "
                f"AND msg_type IN ({placeholders}) AND timestamp < ?",
                (account_id, *MONEY_TYPES, _iso(before)),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_money_alerted(self, message_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE messages SET money_alerted = 1 WHERE id = ?", (message_id,))
            conn.commit()
        finally:
            conn.close()

    def unclassified_money_peers(self, account_id: str) -> list[tuple[str, str]]:
        """旧库中仍被当作普通 file 的 type=49 消息所属联系人（M14-O1）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT peer_id, peer_name FROM messages "
                "WHERE account_id = ? AND msg_type = 'file' AND extra IS NULL",
                (account_id,),
            ).fetchall()
        finally:
            conn.close()
        return [(str(r["peer_id"]), str(r["peer_name"] or "")) for r in rows]

    def classify_money_messages(
        self, account_id: str,
        events: Iterable[tuple[str, str, dict[str, Any]]],
    ) -> int:
        """把旧库中已解析出的资金事件回填为 transfer/red_packet（幂等）。"""
        rows = [
            (kind, json.dumps(extra, ensure_ascii=False), account_id, msg_id)
            for msg_id, kind, extra in events
            if kind in MONEY_TYPES
        ]
        if not rows:
            return 0
        conn = self._connect()
        try:
            cur = conn.executemany(
                "UPDATE messages SET msg_type = ?, extra = ?, money_alerted = 0 "
                "WHERE account_id = ? AND msg_id = ? AND msg_type = 'file'",
                rows,
            )
            updated = cur.rowcount or 0
            conn.commit()
            return updated
        finally:
            conn.close()

    # ---------- 语音转写缓存（M6） ----------

    def get_transcript(self, key: str) -> str | None:
        """命中缓存则直接返回转写文本，避免同一条语音重复跑模型。"""
        conn = self._connect()
        try:
            row = conn.execute("SELECT text FROM transcripts WHERE key = ?", (key,)).fetchone()
            return row["text"] if row else None
        finally:
            conn.close()

    def save_transcript(self, key: str, text: str, model: str | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO transcripts (key, text, model, created_at) VALUES (?,?,?,?)
                   ON CONFLICT (key) DO UPDATE SET text=excluded.text, model=excluded.model""",
                (key, text, model, _iso(datetime.now(timezone.utc))),
            )
            conn.commit()
        finally:
            conn.close()
