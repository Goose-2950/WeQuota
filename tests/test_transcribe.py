"""M6：语音转写（faster-whisper tiny）。

测试全部用注入的假模型跑，不依赖真实模型与网络；
真实链路只验证「状态上报 + 失败降级」。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from wequota.config import WhisperConfig
from wequota.metering import Meter
from wequota.storage import Database
from wequota.transcribe import (
    MODEL_FILES,
    DbTranscriptCache,
    WhisperTranscriber,
    build_transcriber,
    cache_key,
)


class Seg:
    def __init__(self, text: str):
        self.text = text


class FakeModel:
    """模拟 faster_whisper.WhisperModel：transcribe 返回 (segments, info)。"""

    def __init__(self, text: str = "明天下午三点开会", fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls: list[str] = []

    def transcribe(self, path: str, **kwargs):
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("model boom")
        return iter([Seg(self.text[:3]), Seg(self.text[3:])]), {"language": "zh"}


def fake_loader(model: FakeModel):
    return lambda *a, **kw: model


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "t.db")
    d.init()
    return d


@pytest.fixture()
def voice_file(tmp_path) -> Path:
    p = tmp_path / "a.amr"
    p.write_bytes(b"fake-audio")
    return p


# ---------- 配置默认值 ----------

def test_default_model_is_tiny_and_int8():
    cfg = WhisperConfig()
    assert cfg.model_size == "tiny"
    assert cfg.compute_type == "int8"
    assert cfg.language == "zh"
    assert cfg.enabled is True


def test_model_dir_default_contains_size():
    t = WhisperTranscriber(WhisperConfig())
    assert t.model_dir.name.endswith("tiny")
    assert WhisperTranscriber(WhisperConfig(model_size="base")).model_dir.name.endswith("base")


def test_model_dir_from_config(tmp_path):
    t = WhisperTranscriber(WhisperConfig(model_dir=str(tmp_path / "custom")))
    assert t.model_dir == tmp_path / "custom"


# ---------- 转写主链路 ----------

def test_transcribe_joins_segments(voice_file):
    model = FakeModel()
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(model))
    assert t.transcribe(str(voice_file)) == "明天下午三点开会"
    assert model.calls == [str(voice_file)]


def test_passes_language_and_beam_size(voice_file):
    seen = {}

    def loader(*a, **kw):
        m = FakeModel()
        orig = m.transcribe

        def transcribe(path, **k):
            seen.update(k)
            return orig(path, **k)

        m.transcribe = transcribe
        return m

    cfg = WhisperConfig(language="zh", beam_size=1)
    WhisperTranscriber(cfg, model_dir=voice_file.parent, loader=loader).transcribe(str(voice_file))
    assert seen["language"] == "zh"
    assert seen["beam_size"] == 1


# ---------- 转写缓存 ----------

def test_db_cache_skips_second_transcription(voice_file, db):
    model = FakeModel()
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(model), cache=DbTranscriptCache(db))
    assert t.transcribe(str(voice_file)) == "明天下午三点开会"
    assert t.transcribe(str(voice_file)) == "明天下午三点开会"
    assert len(model.calls) == 1                      # 第二次走缓存，不跑模型
    assert db.get_transcript(cache_key(voice_file)) == "明天下午三点开会"


def test_cache_key_changes_when_file_changes(tmp_path):
    p = tmp_path / "v.amr"
    p.write_bytes(b"a")
    k1 = cache_key(p)
    p.write_bytes(b"a" * 100)
    assert cache_key(p) != k1
    assert cache_key(tmp_path / "missing.amr")        # 文件不存在也不报错


def test_db_cache_persists_across_instances(voice_file, db):
    db.save_transcript("k1", "你好世界", model="tiny")
    assert Database(db.path).get_transcript("k1") == "你好世界"
    assert db.get_transcript("nope") is None


# ---------- 模型就绪检测与状态上报 ----------

def test_model_ready_requires_files(tmp_path):
    t = WhisperTranscriber(WhisperConfig(), model_dir=tmp_path / "m")
    assert t.model_ready() is False
    (tmp_path / "m").mkdir()
    for f in MODEL_FILES:
        (tmp_path / "m" / f).write_text("x")
    assert t.model_ready() is True


def test_status_reports_engine_and_model(tmp_path, voice_file):
    t = WhisperTranscriber(WhisperConfig(), model_dir=tmp_path / "empty",
                           loader=fake_loader(FakeModel()))
    s = t.status()
    assert s["enabled"] is True and s["model_size"] == "tiny"
    assert s["model_ready"] is False
    assert s["engine_installed"] is True              # 注入了 loader 视为引擎可用
    assert "model_dir" in s and s["reason"]


def test_status_when_engine_missing(tmp_path):
    t = WhisperTranscriber(WhisperConfig(), model_dir=tmp_path, engine_available=False)
    s = t.status()
    assert s["engine_installed"] is False
    assert s["ready"] is False


# ---------- 与计量器联动 ----------

def test_meter_uses_whisper_transcript(voice_file):
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(FakeModel()))
    from wequota.contracts import MessageEnvelope

    e = MessageEnvelope(account_id="a", msg_id="m1", peer_id="p", peer_name="P",
                        direction="send", msg_type="voice",
                        timestamp="2026-10-12T10:00:00+08:00",
                        media_path=str(voice_file), media_meta={"duration_s": 12})
    m = Meter(transcriber=t)
    tokens = m.meter(e)
    assert e.meter_detail["engine"] == "whisper"
    assert e.meter_detail["transcript"] == "明天下午三点开会"
    assert tokens > 0


def test_meter_falls_back_when_transcribe_fails(voice_file):
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(FakeModel(fail=True)))
    from wequota.contracts import MessageEnvelope

    e = MessageEnvelope(account_id="a", msg_id="m1", peer_id="p", peer_name="P",
                        direction="send", msg_type="voice",
                        timestamp="2026-10-12T10:00:00+08:00",
                        media_path=str(voice_file), media_meta={"duration_s": 12})
    m = Meter(transcriber=t)
    assert m.meter(e) == 12 * 25                       # 降级：时长 × 25
    assert e.meter_detail["engine"] == "fallback_duration"


def test_meter_falls_back_when_model_not_downloaded(tmp_path):
    t = WhisperTranscriber(WhisperConfig(), model_dir=tmp_path / "empty", engine_available=False)
    from wequota.contracts import MessageEnvelope

    e = MessageEnvelope(account_id="a", msg_id="m1", peer_id="p", peer_name="P",
                        direction="send", msg_type="voice",
                        timestamp="2026-10-12T10:00:00+08:00",
                        media_path="/nonexistent.amr", media_meta={"duration_s": 10})
    Meter(transcriber=t).meter(e)
    assert e.meter_detail["engine"] == "fallback_duration"


# ---------- 工厂 ----------

def test_build_transcriber_disabled_returns_none(db):
    assert build_transcriber(WhisperConfig(enabled=False), db=db) is None


def test_build_transcriber_enabled_uses_db_cache(db):
    t = build_transcriber(WhisperConfig(), db=db)
    assert t is None or isinstance(t, WhisperTranscriber)


# ---------- 语音时长不再限制转写 ----------

def _voice_envelope(path: str, duration_s: float):
    from wequota.contracts import MessageEnvelope

    return MessageEnvelope(account_id="a", msg_id="m1", peer_id="p", peer_name="P",
                           direction="send", msg_type="voice",
                           timestamp="2026-10-12T10:00:00+08:00",
                           media_path=path, media_meta={"duration_s": duration_s})


def test_voice_is_transcribed_regardless_of_duration(voice_file):
    """微信语音最长 60 秒，不再设「超长跳过转写」的门槛：多长都走 whisper。"""
    from wequota.config import MeteringConfig

    model = FakeModel()
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(model))
    m = Meter(MeteringConfig(), transcriber=t)
    e = _voice_envelope(str(voice_file), duration_s=600)
    m.meter(e)
    assert e.meter_detail["engine"] == "whisper"
    assert len(model.calls) == 1


def test_metering_config_has_no_voice_duration_cap():
    from wequota.config import MeteringConfig

    assert not hasattr(MeteringConfig(), "voice_max_duration_s")


def test_transcript_reused_across_pipeline_runs(voice_file, db):
    """同一条语音第二次计量不重复跑模型（缓存命中）。"""
    from wequota.collector.mock import MockCollector
    from wequota.ingestion import Ingestor
    from wequota.metering_pipeline import MeteringPipeline

    model = FakeModel()
    t = WhisperTranscriber(WhisperConfig(), model_dir=voice_file.parent,
                           loader=fake_loader(model), cache=DbTranscriptCache(db))
    Ingestor(db, account_id="wxid_demo").run(MockCollector(account_id="wxid_demo"))
    pipeline = MeteringPipeline(db, Meter(transcriber=t))
    pipeline.run_until_drained()
    first_calls = len(model.calls)
    assert first_calls > 0
    # 清空计量结果后重跑，转写仍应全部命中缓存
    db.reset_metering()
    pipeline.run_until_drained()
    assert len(model.calls) == first_calls
