"""模型清单：名称 + 锁定版本 + 文件指纹。

模型本体（tiny ~75MB）不入仓库，只把「下哪一份」写死在 transcribe.MODEL_MANIFEST，
换设备时用 `wequota --download-model` 按 revision 重新下载，拿到的就是同一份字节。
"""

from __future__ import annotations

import hashlib
import sys
import types

import pytest

from wequota.config import WhisperConfig
from wequota.transcribe import MODEL_MANIFEST, FilePin, ModelPin, \
    file_sha256, model_pin, model_size_note, validate_model_size, verify_model_files
from wequota.transcribe import WhisperTranscriber


# ---------- 清单本身 ----------

def test_manifest_pins_tiny_repo_and_revision():
    pin = model_pin("tiny")
    assert pin is not None
    assert pin.repo_id == "Systran/faster-whisper-tiny"
    assert len(pin.revision) == 40            # HF revision = 40 位提交号
    assert set(pin.files) == {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt"}


def test_manifest_file_pins_are_complete():
    for size, pin in MODEL_MANIFEST.items():
        for name, fpin in pin.files.items():
            assert len(fpin.sha256) == 64, f"{size}/{name} sha256 不是 64 位"
            assert fpin.size > 0, f"{size}/{name} size 必须为正"


def test_unknown_model_size_has_no_pin():
    assert model_pin("large-v3") is None


def test_file_sha256_matches_hashlib(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"wequota")
    assert file_sha256(p) == hashlib.sha256(b"wequota").hexdigest()


# ---------- 校验 ----------

def _make_dir(tmp_path, contents: dict[str, bytes]) -> str:
    d = tmp_path / "model"
    d.mkdir()
    for name, data in contents.items():
        (d / name).write_bytes(data)
    return str(d)


def _pin_of(contents: dict[str, bytes]) -> ModelPin:
    return ModelPin(
        repo_id="Systran/faster-whisper-test",
        revision="0" * 40,
        files={name: FilePin(hashlib.sha256(data).hexdigest(), len(data))
               for name, data in contents.items()},
    )


def test_verify_model_files_accepts_matching_dir(tmp_path):
    contents = {"config.json": b"{}", "model.bin": b"weights"}
    ok, problems = verify_model_files(_make_dir(tmp_path, contents), _pin_of(contents))
    assert ok and problems == []


def test_verify_model_files_reports_mismatch(tmp_path):
    contents = {"config.json": b"{}", "model.bin": b"weights"}
    d = _make_dir(tmp_path, contents)
    (tmp_path / "model" / "model.bin").write_bytes(b"corrupted!!")
    pin = _pin_of(contents)
    ok, problems = verify_model_files(d, pin)
    assert not ok
    assert any("model.bin" in p for p in problems)


def test_verify_model_files_reports_missing(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    ok, problems = verify_model_files(d, _pin_of({"config.json": b"{}"}))
    assert not ok
    assert any("缺失" in p for p in problems)


def test_verify_model_files_without_pin_is_not_ok(tmp_path):
    ok, problems = verify_model_files(tmp_path, None)
    assert not ok and problems


def test_verify_model_reports_human_readable_result(tmp_path):
    contents = {"config.json": b"{}"}
    t = WhisperTranscriber(WhisperConfig(model_size="tiny"), model_dir=tmp_path / "nope")
    ok, message = t.verify_model()
    assert not ok                      # 目录不存在 → 文件缺失
    assert "config.json" in message


def test_pinned_revision_uses_manifest_by_default(tmp_path):
    t = WhisperTranscriber(WhisperConfig(model_size="tiny"), model_dir=tmp_path)
    assert t.pinned_revision() == MODEL_MANIFEST["tiny"].revision


def test_pinned_revision_can_be_overridden(tmp_path):
    t = WhisperTranscriber(WhisperConfig(model_size="tiny", model_revision="abc123"),
                           model_dir=tmp_path)
    assert t.pinned_revision() == "abc123"


def test_status_exposes_repo_and_revision(tmp_path):
    t = WhisperTranscriber(WhisperConfig(model_size="tiny"), model_dir=tmp_path,
                           engine_available=True)
    s = t.status()
    assert s["model_repo"] == "Systran/faster-whisper-tiny"
    assert s["model_revision"] == MODEL_MANIFEST["tiny"].revision


# ---------- 下载时锁定版本 ----------

class _FakeHub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def snapshot_download(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs.get("local_dir", "")


@pytest.fixture
def fake_hub(monkeypatch):
    fake = _FakeHub()
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(snapshot_download=fake.snapshot_download))
    return fake


def test_download_pins_revision_from_manifest(tmp_path, fake_hub):
    t = WhisperTranscriber(WhisperConfig(model_size="tiny"), model_dir=tmp_path / "m")
    t.download_model()
    assert fake_hub.calls, "应调用 snapshot_download"
    assert fake_hub.calls[0]["revision"] == MODEL_MANIFEST["tiny"].revision


def test_download_prefers_config_revision(tmp_path, fake_hub):
    t = WhisperTranscriber(WhisperConfig(model_size="tiny", model_revision="deadbeef"),
                           model_dir=tmp_path / "m")
    t.download_model()
    assert fake_hub.calls[0]["revision"] == "deadbeef"


def test_download_without_pin_omits_revision(tmp_path, fake_hub):
    t = WhisperTranscriber(WhisperConfig(model_size="large-v3"), model_dir=tmp_path / "m")
    t.download_model()
    assert "revision" not in fake_hub.calls[0]


def test_download_reports_incomplete_files(tmp_path, fake_hub):
    """假下载没落文件 → 必须报「文件不完整」，不能假装成功。"""
    t = WhisperTranscriber(WhisperConfig(model_size="tiny"), model_dir=tmp_path / "m")
    ok, message = t.download_model()
    assert not ok
    assert "不完整" in message


# ---------------- 模型规格校验（遗留项 C3 缓解） ----------------

def test_unknown_model_size_is_rejected_with_actionable_message():
    t = WhisperTranscriber(WhisperConfig(model_size="huge"))
    assert validate_model_size("huge") is not None
    assert "huge" in validate_model_size("huge")
    ok, message = t.download_model()
    assert ok is False
    assert "未知" in message          # 明确报错，而不是去下一个不存在的仓库


def test_known_but_unverified_size_gets_a_note():
    assert model_size_note("tiny") is None           # 清单里有 → 无需提示
    note = model_size_note("base")
    assert note and "未在本项目实测" in note
    assert validate_model_size("base") is None       # 但仍是合法规格，允许下载


def test_status_exposes_model_note():
    t = WhisperTranscriber(WhisperConfig(model_size="medium"))
    assert "未在本项目实测" in t.status()["model_note"]


def test_pinned_size_has_no_note():
    t = WhisperTranscriber(WhisperConfig())
    assert t.status()["model_note"] is None
