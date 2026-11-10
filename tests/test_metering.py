"""Token 计量管线测试。

口径（v0.3 方案）：
- text        : tiktoken cl100k_base
- voice 消息   : 转写后按文字计；无模型时降级 时长 × 25
- image/emoji : tiles = ceil(w/512)*ceil(h/512)，tokens = tiles*170 + 85
- video 消息   : frames = min(round(时长*fps), 32)，tokens = frames * 255
- voice_call  : 时长 × 2 × 25   （双方同时输出）
- video_call  : 时长 × 2 × 255  （双方同时输出）
- file/other  : 0（只记事件）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wequota.config import MeteringConfig
from wequota.contracts import MessageEnvelope
from wequota.metering import Meter
from wequota.metering_pipeline import MeteringPipeline
from wequota.storage import Database

TS = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)


def env(msg_id: str = "m1", **over) -> MessageEnvelope:
    base = dict(
        account_id="wxid_demo", msg_id=msg_id, peer_id="peer_a", peer_name="Alice",
        direction="send", msg_type="text", timestamp=TS, text="你好",
    )
    base.update(over)
    return MessageEnvelope(**base)


@pytest.fixture()
def meter() -> Meter:
    return Meter(MeteringConfig())


# ---------- 文字 ----------

def test_text_token_is_positive_and_stable(meter):
    e = env(text="明天下午三点会议室讨论一下方案细节，记得带上上次的评审意见")
    n1 = meter.meter(e)
    n2 = meter.meter(env(msg_id="m2", text=e.text))
    assert n1 > 0 and n1 == n2


def test_text_token_grows_with_length(meter):
    short = meter.meter(env(text="好"))
    long = meter.meter(env(msg_id="m2", text="好" * 100))
    assert long > short * 5


def test_text_detail_records_engine_and_encoding(meter):
    e = env(text="hello")
    meter.meter(e)
    assert e.meter_detail["engine"] == "tiktoken"
    assert e.meter_detail["encoding"] == "cl100k_base"


# ---------- 图片 / 表情包 ----------

def test_image_uses_tile_formula(meter):
    e = env(msg_type="image", text=None, media_meta={"width": 1024, "height": 768})
    assert meter.meter(e) == 4 * 170 + 85  # 2x2 tiles


def test_emoji_small_image_is_one_tile(meter):
    e = env(msg_type="emoji", text=None, media_meta={"width": 240, "height": 240})
    assert meter.meter(e) == 1 * 170 + 85


def test_image_missing_size_falls_back_to_default(meter):
    e = env(msg_type="image", text=None, media_meta={})
    assert meter.meter(e) > 0


def test_image_config_is_overridable():
    cfg = MeteringConfig(tokens_per_tile=100, image_base_tokens=10)
    e = env(msg_type="image", text=None, media_meta={"width": 1024, "height": 768})
    assert Meter(cfg).meter(e) == 4 * 100 + 10


# ---------- 视频消息 ----------

def test_video_frames_by_duration(meter):
    e = env(msg_type="video", text=None, media_meta={"duration_s": 8})
    assert meter.meter(e) == 8 * 255


def test_video_frames_capped(meter):
    e = env(msg_type="video", text=None, media_meta={"duration_s": 120})
    assert meter.meter(e) == 32 * 255


def test_video_without_duration_uses_default_frames(meter):
    e = env(msg_type="video", text=None, media_meta={})
    assert meter.meter(e) > 0


# ---------- 通话（双方同时输出，×2） ----------

def test_voice_call_duplex_formula(meter):
    e = env(msg_type="voice_call", text=None, media_meta={"duration_s": 180})
    assert meter.meter(e) == 180 * 2 * 25


def test_video_call_duplex_formula(meter):
    e = env(msg_type="video_call", text=None, media_meta={"duration_s": 300})
    assert meter.meter(e) == 300 * 2 * 255


def test_call_detail_records_duplex_factor(meter):
    e = env(msg_type="voice_call", text=None, media_meta={"duration_s": 60})
    meter.meter(e)
    assert e.meter_detail["duplex_factor"] == 2
    assert e.meter_detail["duration_s"] == 60


def test_call_rate_configurable():
    cfg = MeteringConfig(voice_call_rate=10)
    e = env(msg_type="voice_call", text=None, media_meta={"duration_s": 60})
    assert Meter(cfg).meter(e) == 60 * 2 * 10


# ---------- 语音消息 ----------

def test_voice_without_transcriber_degrades_to_duration(meter):
    e = env(msg_type="voice", text=None, media_meta={"duration_s": 12})
    assert meter.meter(e) == 12 * 25
    assert e.meter_detail["engine"] == "fallback_duration"


def test_voice_with_transcriber_counts_text():
    class FakeTranscriber:
        def transcribe(self, path: str) -> str:
            return "明天下午三点开会"

    m = Meter(MeteringConfig(), transcriber=FakeTranscriber())
    e = env(msg_type="voice", text=None, media_path="/tmp/a.amr",
            media_meta={"duration_s": 12})
    n = m.meter(e)
    assert n > 0
    assert e.meter_detail["engine"] == "whisper"
    assert e.meter_detail["transcript"]


# ---------- 不计量类型 ----------

@pytest.mark.parametrize("t", ["file", "other"])
def test_non_metered_types_are_zero(meter, t):
    e = env(msg_type=t, text=None)
    assert meter.meter(e) == 0


# ---------- 流水线 ----------

@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "m.db")
    d.init()
    return d


def test_pipeline_fills_pending_messages(db, meter):
    db.upsert_messages([
        env("m1", text="你好世界"),
        env("m2", msg_type="image", text=None, media_meta={"width": 1024, "height": 768}),
        env("m3", msg_type="voice_call", text=None, media_meta={"duration_s": 60}),
    ])
    pipeline = MeteringPipeline(db, meter)
    assert pipeline.run_once() == 3
    rows = db.query_messages()
    assert all(r["token_count"] is not None for r in rows)
    assert {r["msg_id"]: r["token_count"] for r in rows}["m3"] == 60 * 2 * 25


def test_pipeline_is_incremental(db, meter):
    db.upsert_messages([env("m1", text="你好")])
    pipeline = MeteringPipeline(db, meter)
    assert pipeline.run_once() == 1
    assert pipeline.run_once() == 0


def test_pipeline_saves_meter_detail(db, meter):
    db.upsert_messages([env("m1", msg_type="video", text=None, media_meta={"duration_s": 10})])
    MeteringPipeline(db, meter).run_once()
    row = db.query_messages()[0]
    assert row["meter_detail"]
    assert "frames" in row["meter_detail"]
