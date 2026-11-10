"""O3：大屏 / API 访问令牌与 CORS 白名单。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wequota.app import create_app
from wequota.config import AppConfig
from wequota.storage import Database

TOKEN = "s3cret-token"


@pytest.fixture()
def make_client(tmp_path):
    def _make(**app_kwargs) -> TestClient:
        db = Database(tmp_path / "auth.db")
        db.init()
        return TestClient(create_app(db=db, app_config=AppConfig(**app_kwargs)))
    return _make


def test_auth_disabled_by_default(make_client):
    client = make_client()
    assert client.get("/api/health").status_code == 200


def test_api_requires_bearer_or_query_token(make_client):
    client = make_client(auth_token=TOKEN)

    denied = client.get("/api/health")
    assert denied.status_code == 401
    assert denied.headers.get("www-authenticate") == "Bearer"

    assert client.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(f"/api/health?token={TOKEN}").status_code == 200


def test_healthz_and_dashboard_stay_open(make_client):
    client = make_client(auth_token=TOKEN)
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_websocket_requires_token(make_client):
    client = make_client(auth_token=TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/alerts"):
            pass

    with client.websocket_connect(f"/ws/alerts?token={TOKEN}"):
        pass


def test_cors_defaults_to_same_origin(make_client):
    client = make_client()
    res = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res.headers


def test_cors_whitelist_is_applied(make_client):
    client = make_client(cors_origins=["http://ok.example"])
    res = client.get("/api/health", headers={"Origin": "http://ok.example"})
    assert res.headers.get("access-control-allow-origin") == "http://ok.example"

    res = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in res.headers


def test_cors_preflight_is_not_blocked_by_auth(make_client):
    client = make_client(auth_token=TOKEN, cors_origins=["http://ok.example"])
    res = client.options(
        "/api/health",
        headers={
            "Origin": "http://ok.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://ok.example"
