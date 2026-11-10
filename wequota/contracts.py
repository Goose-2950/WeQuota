"""采集器与核心服务之间的唯一数据契约。

任何采集实现（wechat-cli 适配器、备份导入器、用户自实现插件）都必须产出
符合本模块定义的 MessageEnvelope，核心服务不关心数据从哪来。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 计量类型：进入 Token 计量管线
METERED_TYPES = (
    "text",
    "voice",
    "image",
    "video",
    "emoji",
    "voice_call",
    "video_call",
)

# 时长驱动类型：按通话时长公式计量
DURATION_TYPES = ("voice_call", "video_call")

# 资金事件类型：不计量，但会触发转账/红包提醒
MONEY_TYPES = ("transfer", "red_packet")

MessageType = Literal[
    "text", "voice", "image", "video", "emoji",
    "file", "voice_call", "video_call", "other",
    "transfer", "red_packet",
]


class MessageEnvelope(BaseModel):
    """一条采集到的消息。"""

    model_config = ConfigDict(validate_assignment=True)

    account_id: str = Field(min_length=1, description="微信账号标识（wxid）")
    msg_id: str = Field(min_length=1, description="采集侧唯一 ID，入账幂等键")
    peer_id: str = Field(min_length=1, description="联系人标识")
    peer_name: str = ""
    direction: Literal["send", "recv"]

    msg_type: MessageType

    # 事件时间：消息真实发生时间，统计归属一律以它为准（不用处理时间）
    timestamp: datetime

    text: str | None = None
    media_path: str | None = None
    media_meta: dict[str, Any] | None = None

    # 资金事件（transfer / red_packet）附加信息：
    #   {"kind": "transfer"|"red_packet", "amount_yuan": float|None,
    #    "memo": str|None, "title": str|None, "raw": str|None}
    extra: dict[str, Any] | None = None

    # 计量阶段回填
    token_count: int | None = None
    meter_detail: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("timestamp"), datetime):
            ts: datetime = data["timestamp"]
            if ts.tzinfo is None:
                data = {**data, "timestamp": ts.replace(tzinfo=timezone.utc)}
        return data

    @model_validator(mode="after")
    def _check_duration_types(self) -> "MessageEnvelope":
        if self.msg_type in DURATION_TYPES:
            meta = self.media_meta or {}
            duration = meta.get("duration_s")
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise ValueError(
                    f"{self.msg_type} 必须在 media_meta 中提供正数 duration_s（秒），"
                    f"否则无法按通话时长公式计量"
                )
        return self

    @property
    def is_metered(self) -> bool:
        """file / other / transfer / red_packet 只记录事件，不进入计量管线。"""
        return self.msg_type in METERED_TYPES

    @property
    def is_money_event(self) -> bool:
        """是否为转账 / 红包事件（触发专门的资金提醒，而非 Token 限额提醒）。"""
        return self.msg_type in MONEY_TYPES

    @property
    def amount_yuan(self) -> float | None:
        value = (self.extra or {}).get("amount_yuan")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def duration_s(self) -> float | None:
        if self.msg_type in DURATION_TYPES and self.media_meta:
            return float(self.media_meta["duration_s"])
        return None
