"""计量流水线：扫描未计量消息，折算后回写数据库。"""

from __future__ import annotations

import json

from .metering import Meter
from .storage import Database

NON_METERED_TYPES = ("file", "other")


class MeteringPipeline:
    def __init__(self, db: Database, meter: Meter, batch_size: int = 500):
        self.db = db
        self.meter = meter
        self.batch_size = batch_size

    def run_once(self) -> int:
        """处理一批未计量消息，返回已处理条数。"""
        rows = self.db.pending_messages(limit=self.batch_size)
        processed = 0
        for row in rows:
            meta = json.loads(row["media_meta"]) if row["media_meta"] else {}
            tokens, detail = self.meter.measure(row["msg_type"], row["text"], meta, row["media_path"])
            self.db.update_token(row["id"], tokens, detail)
            processed += 1
        return processed

    def run_until_drained(self, max_batches: int = 100) -> int:
        total = 0
        for _ in range(max_batches):
            n = self.run_once()
            total += n
            if n == 0:
                break
        return total
