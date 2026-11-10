"""Mock 采集器：M1 阶段驱动全链路的假数据源。

它产出与真实采集器（M7 的 wechat-cli 适配器）完全相同的 MessageEnvelope，
因此 M7 落地时核心服务无需任何改动。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..contracts import MessageEnvelope

# (peer_id, peer_name) 造几个联系人
PEERS = [
    ("wxid_alice", "Alice"),
    ("wxid_bob", "Bob"),
    ("wxid_carol", "Carol"),
]


class MockCollector:
    """按固定序列生成消息，支持 since 游标与补录注入。"""

    def __init__(
        self,
        account_id: str = "wxid_mock",
        base_time: datetime | None = None,
        backfill: Sequence[tuple[str, str, str, datetime]] | None = None,
    ):
        self.account_id = account_id
        self.base_time = base_time or datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        self.backfill = list(backfill or [])

    def _build(self, peer_id: str, peer_name: str, idx: int, hours_ago: int) -> list[MessageEnvelope]:
        # 整批往后挪 8 分钟再叠加 +0..+8 的分钟偏移，保证最后一条也落在 base_time 之前。
        # 否则当 base_time 正好是当前时刻（或整点截断后的当前小时）时，偏移到未来的消息
        # 会被统计窗口（end=now）丢掉，导致统计用例随系统时钟随机失败。
        ts = self.base_time - timedelta(hours=hours_ago, minutes=8)
        mid = lambda suffix: f"{self.account_id}:{peer_id}:{idx}:{suffix}"  # noqa: E731
        return [
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("t"), peer_id=peer_id,
                peer_name=peer_name, direction="send" if idx % 2 else "recv",
                msg_type="text", timestamp=ts,
                text="明天下午三点会议室讨论一下方案细节，记得带上上次的评审意见",
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("v"), peer_id=peer_id,
                peer_name=peer_name, direction="recv", msg_type="voice", timestamp=ts + timedelta(minutes=1),
                media_path="/tmp/voice.amr", media_meta={"duration_s": 12},
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("i"), peer_id=peer_id,
                peer_name=peer_name, direction="send", msg_type="image",
                timestamp=ts + timedelta(minutes=2),
                media_path="/tmp/a.jpg", media_meta={"width": 1024, "height": 768},
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("e"), peer_id=peer_id,
                peer_name=peer_name, direction="recv", msg_type="emoji",
                timestamp=ts + timedelta(minutes=3),
                media_path="/tmp/e.png", media_meta={"width": 240, "height": 240},
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("vd"), peer_id=peer_id,
                peer_name=peer_name, direction="send", msg_type="video",
                timestamp=ts + timedelta(minutes=4),
                media_path="/tmp/a.mp4", media_meta={"duration_s": 8, "width": 720, "height": 1280},
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("vc"), peer_id=peer_id,
                peer_name=peer_name, direction="recv", msg_type="voice_call",
                timestamp=ts + timedelta(minutes=5), media_meta={"duration_s": 180},
            ),
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("vv"), peer_id=peer_id,
                peer_name=peer_name, direction="send", msg_type="video_call",
                timestamp=ts + timedelta(minutes=6), media_meta={"duration_s": 300},
            ),
            # 群聊：v1 直接丢弃
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("g"), peer_id="12345@chatroom",
                peer_name="项目讨论群", direction="recv", msg_type="text",
                timestamp=ts + timedelta(minutes=7), text="群里这条应该被忽略",
            ),
            # 不计量的事件类型
            MessageEnvelope(
                account_id=self.account_id, msg_id=mid("f"), peer_id=peer_id,
                peer_name=peer_name, direction="recv", msg_type="file",
                timestamp=ts + timedelta(minutes=8), media_path="/tmp/doc.pdf",
            ),
        ]

    def _all(self) -> list[MessageEnvelope]:
        msgs: list[MessageEnvelope] = []
        for idx, (peer_id, peer_name) in enumerate(PEERS):
            msgs.extend(self._build(peer_id, peer_name, idx, hours_ago=idx * 2))
        for msg_id, peer_id, text, ts in self.backfill:
            msgs.append(MessageEnvelope(
                account_id=self.account_id, msg_id=msg_id, peer_id=peer_id,
                peer_name="Alice", direction="send", msg_type="text", timestamp=ts, text=text,
            ))
        return sorted(msgs, key=lambda m: m.timestamp)

    def fetch(self, since: datetime | None = None, limit: int = 500) -> list[MessageEnvelope]:
        msgs = self._all()
        if since is not None:
            msgs = [m for m in msgs if m.timestamp > since]
        return msgs[:limit]

    def stream(self) -> Iterable[MessageEnvelope]:
        yield from self._all()
