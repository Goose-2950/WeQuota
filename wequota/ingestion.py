"""入库：把采集器的 Envelope 写入 SQLite，维护同步游标。

规则：
- v1 忽略群聊（peer_id 命中 @chatroom 等群标识）。
- 以 (account_id, msg_id) 幂等去重，重复投递不重复入账。
- 事件时间（timestamp）原样保留，用于统计窗口归属；入库时间单独记录以识别补录。
"""

from __future__ import annotations

from datetime import datetime

from .collector import Collector
from .contracts import MessageEnvelope
from .storage import Database

GROUP_MARKERS = ("@chatroom", "@openim", "chatroom")


def is_group_peer(peer_id: str) -> bool:
    return any(marker in peer_id.lower() for marker in GROUP_MARKERS)


class Ingestor:
    def __init__(self, db: Database, account_id: str):
        self.db = db
        self.account_id = account_id

    def run(self, collector: Collector) -> int:
        """执行一次增量采集入库，返回新增条数。"""
        _, last_ts = self.db.get_cursor(self.account_id)
        messages = collector.fetch(since=last_ts)
        accepted = [m for m in messages if not is_group_peer(m.peer_id)]
        inserted = self.db.upsert_messages(accepted)
        if accepted:
            newest = max(accepted, key=lambda m: m.timestamp)
            self.db.set_cursor(self.account_id, newest.msg_id, newest.timestamp)
            ack = getattr(collector, "ack", None)
            if callable(ack):
                ack(accepted)
        return inserted


def ingest_batch(db: Database, messages: list[MessageEnvelope], account_id: str) -> int:
    """供导入器/测试直接批量入库。"""
    accepted = [m for m in messages if not is_group_peer(m.peer_id)]
    return db.upsert_messages(accepted)
