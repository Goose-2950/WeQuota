"""配置 schema 版本与应用开关（遗留项 F5 / E4）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.config import (
    CONFIG_SCHEMA_VERSION,
    AppConfig,
    BackupConfig,
    CollectorConfig,
    config_version_note,
    dump_default_config,
    load_app_config,
    load_backup_config,
    load_collector_config,
    read_config_version,
)
from wequota.storage import Database


@pytest.fixture()
def client(tmp_path):
    db = Database(tmp_path / "app.db")
    db.init()
    return TestClient(create_app(db=db))


# ---------------- E4：演示数据入口开关 ----------------

def test_mock_ingest_is_allowed_by_default(client):
    assert client.post("/api/ingest/mock").status_code == 200


def test_mock_ingest_can_be_turned_off(tmp_path):
    db = Database(tmp_path / "app.db")
    db.init()
    app = create_app(db=db, app_config=AppConfig(allow_mock_ingest=False))
    r = TestClient(app).post("/api/ingest/mock")
    assert r.status_code == 403
    assert "allow_mock_ingest" in r.json()["detail"]


def test_turned_off_mock_does_not_write_data(tmp_path):
    db = Database(tmp_path / "app.db")
    db.init()
    app = create_app(db=db, app_config=AppConfig(allow_mock_ingest=False))
    client = TestClient(app)
    assert client.post("/api/ingest/mock").status_code == 403
    assert db.count_messages() == 0


def test_app_config_is_read_from_config_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"app": {"allow_mock_ingest": False}}), encoding="utf-8")
    assert load_app_config(p).allow_mock_ingest is False
    assert load_app_config(tmp_path / "missing.json").allow_mock_ingest is True


# ---------------- F5：配置 schema 版本 ----------------

def test_dumped_config_carries_schema_version(tmp_path):
    p = tmp_path / "config.json"
    dump_default_config(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == CONFIG_SCHEMA_VERSION
    assert read_config_version(p) == CONFIG_SCHEMA_VERSION
    assert config_version_note(p) is None


def test_dumped_config_has_every_section(tmp_path):
    p = tmp_path / "config.json"
    dump_default_config(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    for section in ("metering", "alert", "whisper", "quota", "app", "backup", "collector"):
        assert section in data


def test_legacy_config_without_version_gets_a_note(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"metering": {"voice_fallback_rate": 30}}), encoding="utf-8")
    assert read_config_version(p) == 0
    assert "老格式" in (config_version_note(p) or "")


def test_newer_config_gets_a_note(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema_version": CONFIG_SCHEMA_VERSION + 1}),
                 encoding="utf-8")
    assert "高于" in (config_version_note(p) or "")


def test_unparsable_config_is_treated_as_version_zero(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json", encoding="utf-8")
    assert read_config_version(p) == 0


def test_missing_config_file_is_treated_as_version_zero(tmp_path):
    assert read_config_version(tmp_path / "nope.json") == 0


# ---------------- 新配置段 ----------------

def test_collector_config_has_cli_version(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"collector": {"cli_version": "latest"}}), encoding="utf-8")
    assert load_collector_config(p).cli_version == "latest"
    assert CollectorConfig().cli_version == ""      # 留空 = 用适配器锁定的版本


def test_backup_config_is_read_from_config_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"backup": {"enabled": False, "keep": 7}}), encoding="utf-8")
    cfg = load_backup_config(p)
    assert cfg.enabled is False and cfg.keep == 7
    assert BackupConfig().keep == 3


def test_collector_config_has_cli_ref(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"collector": {"cli_ref": "main"}}), encoding="utf-8")
    assert load_collector_config(p).cli_ref == "main"
    assert CollectorConfig().cli_ref == ""       # 留空 = 锁定 PINNED_CLI_COMMIT
    assert CollectorConfig().poll_interval_s == 15.0
    assert CollectorConfig().full_backfill_limit == 5000
