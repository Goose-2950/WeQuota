"""M4 提醒通道 API 测试：历史查询、测试提醒、WebSocket 推送、通道状态。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wequota.alerting import AlertDispatcher, AlertHub, DashboardChannel, DesktopChannel, EmailChannel
from wequota.app import create_app
from wequota.config import AlertConfig
from wequota.quota import AlertEvent, AlertLevel
from wequota.storage import Database

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "wequota.db")
    db.init()
    calls: list[tuple[str, str]] = []
    hub = AlertHub()
    cfg = AlertConfig()
    dispatcher = AlertDispatcher(
        {
            "desktop": DesktopChannel(cfg, notify_fn=lambda t, m: calls.append((t, m))),
            "email": EmailChannel(cfg),
            "dashboard": DashboardChannel(cfg, hub),
        },
        config=cfg, db=db,
    )
    app = create_app(db=db, dispatcher=dispatcher, hub=hub)
    client = TestClient(app)
    return dict(db=db, hub=hub, calls=calls, dispatcher=dispatcher, client=client)


def test_alerts_history_empty_by_default(env):
    assert env["client"].get("/api/alerts").json()["items"] == []


def test_test_alert_endpoint_delivers_to_all_enabled_channels(env):
    r = env["client"].post("/api/alerts/test", json={
        "peer_id": "peer_x", "peer_name": "测试联系人", "level": "exceeded",
        "used": 6000, "limit": 5000,
    })
    assert r.status_code == 200
    delivered = r.json()["delivered"]
    assert delivered["desktop"] is True
    assert delivered["dashboard"] is True
    assert delivered["email"] is False          # 未配置 SMTP
    assert len(env["calls"]) == 1
    assert "测试联系人" in env["calls"][0][1]
    assert env["hub"].recent()[-1]["peer_id"] == "peer_x"


def test_channel_status_endpoint(env):
    status = env["client"].get("/api/alerts/status").json()["channels"]
    assert status["desktop"] == "enabled"
    assert status["dashboard"] == "enabled"
    assert status["email"] == "未配置 SMTP"


def test_quota_alert_is_logged_with_delivery_result(env):
    client = env["client"]
    client.post("/api/ingest/mock")
    # 额度设得极小 → 改额后立即触发超限
    client.put("/api/quota/rules", json={
        "account_id": "wxid_demo", "peer_id": "wxid_alice", "peer_name": "Alice",
        "window_type": "daily", "limit_tokens": 1000,
    })
    items = client.get("/api/alerts").json()["items"]
    assert items, "限额超限应产生告警日志"
    top = items[0]
    assert top["level"] == "exceeded"
    assert top["peer_id"] == "wxid_alice"
    assert top["channels"]["desktop"] is True     # 投递回执已回填
    assert env["calls"], "桌面通知应被调用"


def test_websocket_receives_alert(env):
    client = env["client"]
    with client.websocket_connect("/ws/alerts") as ws:
        env["dispatcher"].dispatch([AlertEvent(
            account_id="wxid_demo", peer_id="peer_ws", peer_name="WS", level=AlertLevel.WARN,
            used=900, limit=1000, ratio=0.9, window_start=NOW, window_end=NOW,
        )])
        payload = ws.receive_json()
        assert payload["peer_id"] == "peer_ws"
        assert payload["level"] == "warn"
        assert "WS" in payload["message"]


def test_websocket_replays_recent_history(env):
    env["hub"].publish({"peer_id": "old", "level": "warn", "message": "历史告警"})
    with env["client"].websocket_connect("/ws/alerts") as ws:
        payload = ws.receive_json()
        assert payload["peer_id"] == "old"
