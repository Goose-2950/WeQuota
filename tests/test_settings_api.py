"""M11：大屏设置面板的后端行为。

重点守住三件事：秘密字段只出不进、能热更新的立刻生效、需重启的明确告知。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.config import AlertConfig
from wequota.settings import SECRET_MASK, guess_preset
from wequota.storage import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "a.db")
    db.init()
    config_path = tmp_path / "config.json"
    # config_path 指向临时目录：设置面板保存时写这里，绝不能污染仓库的 config.json
    app = create_app(db=db, config_path=config_path)
    return dict(client=TestClient(app), app=app, db=db, config_path=config_path)


# ---------------- schema ----------------

def test_settings_exposes_schema_and_values(env):
    body = env["client"].get("/api/settings").json()
    assert [s["id"] for s in body["sections"]] == \
        ["quota", "alert", "smtp", "collector", "whisper", "system"]
    assert body["values"]["quota.default_limit_tokens"] == 50000
    assert body["config_path"] == str(env["config_path"])


def test_schema_marks_restart_fields(env):
    body = env["client"].get("/api/settings").json()
    flat = {f["key"]: f for s in body["sections"] for f in s["fields"]}
    assert flat["whisper.model_size"]["needs_restart"] is True
    assert flat["collector.enabled"]["needs_restart"] is True
    assert flat["alert.email_enabled"]["needs_restart"] is False


def test_schema_exposes_mail_presets(env):
    presets = {p["id"] for p in env["client"].get("/api/settings").json()["presets"]}
    assert {"qq", "163", "126", "gmail"} <= presets


# ---------------- 秘密字段 ----------------

def test_password_is_never_returned(env):
    env["client"].put("/api/settings", json={"values": {"alert.smtp_password": "topsecret"}})
    body = env["client"].get("/api/settings").json()
    assert body["values"]["alert.smtp_password"] == SECRET_MASK
    assert "topsecret" not in json.dumps(body, ensure_ascii=False)


def test_password_is_still_persisted(env):
    env["client"].put("/api/settings", json={"values": {"alert.smtp_password": "topsecret"}})
    saved = json.loads(env["config_path"].read_text(encoding="utf-8"))
    assert saved["alert"]["smtp_password"] == "topsecret"


def test_resending_mask_keeps_password(env):
    env["client"].put("/api/settings", json={"values": {"alert.smtp_password": "topsecret"}})
    env["client"].put("/api/settings", json={
        "values": {"alert.smtp_password": SECRET_MASK, "alert.smtp_host": "smtp.qq.com"}})
    saved = json.loads(env["config_path"].read_text(encoding="utf-8"))
    assert saved["alert"]["smtp_password"] == "topsecret"   # 掩码不算「改了」
    assert saved["alert"]["smtp_host"] == "smtp.qq.com"     # 同一批里的其它字段照常保存


# ---------------- 校验 ----------------

def test_invalid_value_is_reported_without_losing_others(env):
    body = env["client"].put("/api/settings", json={
        "values": {"alert.quiet_hours": "晚点到早", "quota.default_limit_tokens": 9000}}).json()
    assert any("23-7" in e for e in body["errors"])
    assert "quota.default_limit_tokens" in body["changed"]     # 别的字段照改
    assert env["app"].state.quota_config.default_limit_tokens == 9000


def test_out_of_range_number_is_rejected(env):
    body = env["client"].put("/api/settings", json={
        "values": {"alert.cooldown_seconds": -5}}).json()
    assert body["errors"] and body["changed"] == []


def test_bad_email_is_rejected(env):
    body = env["client"].put("/api/settings", json={
        "values": {"alert.email_to": "not-an-email"}}).json()
    assert body["errors"] and "不是有效的邮箱地址" in body["errors"][0]


def test_unknown_key_is_ignored(env):
    body = env["client"].put("/api/settings", json={"values": {"evil.path": "/etc/passwd"}}).json()
    assert body["changed"] == []


def test_quiet_hours_round_trip(env):
    env["client"].put("/api/settings", json={"values": {"alert.quiet_hours": "23-7"}})
    assert env["app"].state.alert_config.quiet_hours == [23, 7]
    env["client"].put("/api/settings", json={"values": {"alert.quiet_hours": ""}})
    assert env["app"].state.alert_config.quiet_hours is None


# ---------------- 热更新 ----------------

def test_default_limit_hot_reloads_into_running_engine(env):
    """改默认额度后，已经在跑的限额引擎要立刻按新值走，不用重启。"""
    client, app = env["client"], env["app"]
    client.post("/api/ingest/mock")
    client.put("/api/settings", json={"values": {"quota.default_limit_tokens": 100_000}})
    engine = _first_engine(app)
    assert engine.default_limit_tokens == 100_000
    assert app.state.sync_worker.quota_default_limit == 100_000


def _first_engine(app):
    # 引擎是闭包里的私有字典，通过已注册账号的公开路径取回
    from wequota.quota import QuotaEngine
    worker_engine = getattr(app.state.sync_worker, "quota", None)
    assert isinstance(worker_engine, QuotaEngine)
    return worker_engine


def test_alert_toggle_hot_reloads(env):
    """提醒开关改的是通道持有的同一份配置对象，应立即生效。"""
    client, app = env["client"], env["app"]
    assert app.state.dispatcher.channels["desktop"].config.desktop_enabled is True
    client.put("/api/settings", json={"values": {"alert.desktop_enabled": False}})
    assert app.state.dispatcher.channels["desktop"].config.desktop_enabled is False


def test_restart_required_sections_are_reported(env):
    body = env["client"].put("/api/settings", json={
        "values": {"whisper.model_size": "base"}}).json()
    assert body["restart_required"] == ["whisper"]
    assert body["changed"] == ["whisper.model_size"]


def test_mock_gate_follows_hot_reload(env):
    client = env["client"]
    client.put("/api/settings", json={"values": {"app.allow_mock_ingest": False}})
    assert client.post("/api/ingest/mock").status_code == 403


def test_settings_survive_reload_of_config_file(env):
    client, config_path = env["client"], env["config_path"]
    client.put("/api/settings", json={
        "values": {"quota.default_limit_tokens": 12345,
                   "whisper.model_size": "base",
                   "alert.email_to": "a@b.com, c@d.com"}})
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["quota"]["default_limit_tokens"] == 12345
    assert saved["whisper"]["model_size"] == "base"
    assert saved["alert"]["email_to"] == ["a@b.com", "c@d.com"]
    assert saved["schema_version"] >= 1


# ---------------- 测试邮件 ----------------

def test_test_email_without_smtp_explains_what_is_missing(env):
    body = env["client"].post("/api/settings/test-email", json={}).json()
    assert body["ok"] is False
    assert "SMTP 服务器或收件人" in body["message"]


def test_test_email_reports_send_failure(env):
    client, app = env["client"], env["app"]
    app.state.alert_config.smtp_host = "smtp.invalid.example"
    app.state.alert_config.email_to = ["me@example.com"]
    body = client.post("/api/settings/test-email", json={}).json()
    assert body["ok"] is False
    assert body["message"].startswith("发送失败")


def test_test_email_values_are_not_persisted(env):
    """未保存的配置也能先验证：临时值只用于这一次尝试。"""
    client, app = env["client"], env["app"]
    client.post("/api/settings/test-email", json={"values": {
        "alert.smtp_host": "smtp.invalid.example", "alert.email_to": "me@example.com"}})
    assert app.state.alert_config.smtp_host == ""      # 内存里没被改
    assert not env["config_path"].exists() or \
        json.loads(env["config_path"].read_text(encoding="utf-8")).get("alert", {}) \
        .get("smtp_host", "") == ""


# ---------------- 邮箱预设 ----------------

@pytest.mark.parametrize("email,host,port,security", [
    ("123456@qq.com", "smtp.qq.com", 465, "ssl"),
    ("me@163.com", "smtp.163.com", 465, "ssl"),
    ("me@126.com", "smtp.126.com", 465, "ssl"),
    ("me@gmail.com", "smtp.gmail.com", 587, "starttls"),
])
def test_guess_preset_by_domain(email, host, port, security):
    preset = guess_preset(email)
    assert preset is not None
    assert (preset["host"], preset["port"], preset["security"]) == (host, port, security)


def test_guess_preset_returns_none_for_unknown_domain():
    assert guess_preset("me@corp.example") is None
    assert guess_preset("") is None


def test_auth_token_is_secret_and_marked_restart(env):
    body = env["client"].put("/api/settings", json={
        "values": {"app.auth_token": "tok123"}}).json()
    assert body["changed"] == ["app.auth_token"]
    assert "app" in body["restart_required"]
    assert body["values"]["app.auth_token"] == SECRET_MASK
    assert env["app"].state.settings_bundle["app"].auth_token == "tok123"
    saved = json.loads(env["config_path"].read_text(encoding="utf-8"))
    assert saved["app"]["auth_token"] == "tok123"


def test_cors_origins_round_trip_and_validate(env):
    body = env["client"].put("/api/settings", json={
        "values": {"app.cors_origins": "https://a.example, https://b.example"}}).json()
    assert body["errors"] == []
    assert env["app"].state.settings_bundle["app"].cors_origins == \
        ["https://a.example", "https://b.example"]
    assert body["values"]["app.cors_origins"] == "https://a.example, https://b.example"

    bad = env["client"].put("/api/settings", json={
        "values": {"app.cors_origins": "ftp://nope"}}).json()
    assert bad["errors"] and "http://" in bad["errors"][0]
    assert env["app"].state.settings_bundle["app"].cors_origins == \
        ["https://a.example", "https://b.example"]


def test_without_config_path_settings_stay_in_memory_only(tmp_path):
    """未指定配置路径时（测试/内嵌场景）只热更新，绝不碰当前目录的 config.json。"""
    from pathlib import Path

    db = Database(tmp_path / "b.db")
    db.init()
    app = create_app(db=db)                      # 不传 config_path
    client = TestClient(app)
    assert client.get("/api/settings").json()["config_path"] == ""
    body = client.put("/api/settings", json={
        "values": {"quota.default_limit_tokens": 777}}).json()
    assert body["saved"] is True
    assert app.state.quota_config.default_limit_tokens == 777   # 内存里生效了
    assert not Path("config.json").exists() or \
        '"default_limit_tokens": 777' not in Path("config.json").read_text(encoding="utf-8")
