"""限额引擎的 API 层测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.config import QuotaConfig
from wequota.storage import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "qa.db")
    d.init()
    return d


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(create_app(db=db))


def test_set_rule_then_usage(client):
    client.post("/api/ingest/mock")
    r = client.put("/api/quota/rules", json={"peer_id": "wxid_alice", "peer_name": "Alice",
                                             "window_type": "daily", "limit_tokens": 2000})
    assert r.status_code == 200
    body = client.get("/api/quota/usage", params={"peer_id": "wxid_alice"}).json()
    u = body["items"][0]
    assert u["limit"] == 2000
    assert u["used"] > 0


def test_lowering_limit_via_api_triggers_alert(client):
    client.post("/api/ingest/mock")
    client.put("/api/quota/rules", json={"peer_id": "wxid_alice", "limit_tokens": 10 ** 9})
    r = client.put("/api/quota/rules", json={"peer_id": "wxid_alice", "limit_tokens": 100})
    assert r.json()["alerts"], "调低额度后应立即触发超限提醒"


def test_rules_list_and_delete(client):
    client.put("/api/quota/rules", json={"peer_id": "wxid_bob", "limit_tokens": 500})
    assert len(client.get("/api/quota/rules").json()["items"]) == 1
    client.delete("/api/quota/rules/wxid_bob")
    assert client.get("/api/quota/rules").json()["items"] == []


def test_usage_without_rule_falls_back_to_default_limit(client):
    """M10：开启默认日额度后，没单独设规则的联系人也有限额可比对。"""
    body = client.get("/api/quota/usage", params={"peer_id": "wxid_nobody"}).json()
    assert body["items"][0]["limit"] == QuotaConfig().default_limit_tokens


def test_usage_without_rule_and_without_default_limit_has_no_limit(db):
    app = create_app(db=db, quota_config=QuotaConfig(default_limit_tokens=0))
    body = TestClient(app).get("/api/quota/usage", params={"peer_id": "wxid_nobody"}).json()
    assert body["items"][0]["limit"] is None
