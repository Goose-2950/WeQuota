"""FastAPI 空壳 API 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.storage import Database

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "api.db")
    d.init()
    return d


@pytest.fixture()
def client(db) -> TestClient:
    app = create_app(db=db)
    return TestClient(app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "WeQuota"


def test_mock_ingest_then_list(client):
    r = client.post("/api/ingest/mock")
    assert r.status_code == 200
    assert r.json()["inserted"] > 0
    listed = client.get("/api/messages").json()
    assert len(listed["items"]) > 0


def test_messages_filter_by_peer(client):
    client.post("/api/ingest/mock")
    items = client.get("/api/messages", params={"peer_id": "wxid_bob"}).json()["items"]
    assert items and all(i["peer_id"] == "wxid_bob" for i in items)


def test_messages_filter_by_time_range(client):
    client.post("/api/ingest/mock")
    items = client.get(
        "/api/messages",
        params={"start": (NOW - timedelta(hours=5)).isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat()},
    ).json()["items"]
    assert isinstance(items, list)


def test_messages_exclude_groups(client):
    client.post("/api/ingest/mock")
    items = client.get("/api/messages").json()["items"]
    assert all("@chatroom" not in i["peer_id"] for i in items)


def test_ingest_then_all_messages_are_metered(client):
    """M2：入库后自动跑计量，所有计量类型消息都有 token_count。"""
    client.post("/api/ingest/mock")
    items = client.get("/api/messages", params={"limit": 100}).json()["items"]
    metered = [i for i in items if i["msg_type"] not in ("file", "other")]
    assert metered and all(i["token_count"] is not None for i in metered)


def test_token_total_is_reported(client):
    client.post("/api/ingest/mock")
    total = client.get("/api/stats/total").json()["tokens"]
    assert total > 0


def test_peers_endpoint(client):
    client.post("/api/ingest/mock")
    peers = client.get("/api/peers").json()["items"]
    names = {p["peer_id"] for p in peers}
    assert "wxid_alice" in names
