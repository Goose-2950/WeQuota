"""MessageEnvelope 数据契约测试。

契约是采集器与核心服务之间的唯一约定，必须先钉死。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wequota.contracts import MessageEnvelope

TS = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)


def make(**over) -> MessageEnvelope:
    base = dict(
        account_id="wxid_demo",
        msg_id="m1",
        peer_id="peer_a",
        peer_name="Alice",
        direction="send",
        msg_type="text",
        timestamp=TS,
        text="hello",
    )
    base.update(over)
    return MessageEnvelope(**base)


def test_text_message_ok():
    e = make()
    assert e.msg_type == "text"
    assert e.text == "hello"
    assert e.token_count is None


def test_all_metered_types_accepted():
    for t in ("text", "voice", "image", "video", "emoji", "voice_call", "video_call"):
        assert make(msg_type=t, text=None, media_meta={"duration_s": 30}).msg_type == t


def test_unknown_type_rejected():
    with pytest.raises(Exception):
        make(msg_type="hologram")


def test_direction_must_be_send_or_recv():
    with pytest.raises(Exception):
        make(direction="sideways")


def test_call_requires_duration_seconds():
    """通话按时长计量，缺 duration_s 必须拒绝，否则计量管线会算成 0。"""
    with pytest.raises(Exception):
        make(msg_type="voice_call", media_meta={"width": 100})
    with pytest.raises(Exception):
        make(msg_type="video_call", media_meta=None)


def test_call_with_duration_ok():
    e = make(msg_type="video_call", text=None, media_meta={"duration_s": 120})
    assert e.media_meta["duration_s"] == 120


def test_timestamp_required():
    with pytest.raises(Exception):
        MessageEnvelope(
            account_id="a", msg_id="m", peer_id="p", peer_name="n",
            direction="recv", msg_type="text", text="x",
        )


def test_naive_timestamp_normalized_to_utc():
    e = make(timestamp=datetime(2026, 10, 12, 18, 0, 0))
    assert e.timestamp.tzinfo is not None


def test_token_count_defaults_none_and_settable():
    e = make()
    assert e.token_count is None
    e.token_count = 42
    assert e.token_count == 42
