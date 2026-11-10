"""采集器抽象。

新增采集器只需实现 fetch(since) -> list[MessageEnvelope]，核心服务零改动。
`HuohuoerCollector`（直读 huohuoer 解密副本，M13）与 M9 的备份导入器都是它的实现。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..contracts import MessageEnvelope


@runtime_checkable
class Collector(Protocol):
    account_id: str

    def fetch(self, since: datetime | None = None, limit: int = 500) -> list[MessageEnvelope]:
        """拉取 since 之后的增量消息；since=None 表示全量。"""
        ...
