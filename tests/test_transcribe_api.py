"""M6：语音转写状态与模型下载接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.config import WhisperConfig
from wequota.storage import Database


def test_status_endpoint_reports_whisper_state(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    client = TestClient(create_app(db=db))
    data = client.get("/api/transcribe/status").json()
    assert "enabled" in data and "model_ready" in data and "model_size" in data
    assert "reason" in data


def test_status_reflects_injected_transcriber(tmp_path):
    from wequota.transcribe import WhisperTranscriber

    t = WhisperTranscriber(WhisperConfig(model_size="base"), model_dir=tmp_path / "none",
                           loader=lambda *a, **kw: object())
    db = Database(tmp_path / "t.db")
    db.init()
    client = TestClient(create_app(db=db, transcriber=t))
    data = client.get("/api/transcribe/status").json()
    assert data["model_size"] == "base"
    assert data["model_ready"] is False
    assert data["ready"] is False


def test_health_includes_whisper_ready(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    assert TestClient(create_app(db=db)).get("/api/health").status_code == 200
