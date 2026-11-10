"""M7 接口测试：账号列表 / 采集器状态 / 手动同步 / 按账号隔离统计。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.collector.mock import MockCollector
from wequota.storage import Database

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@pytest.fixture()
def db(tmp_path) -> Database:
    d = Database(tmp_path / "m7.db")
    d.init()
    return d


def seed(db: Database, account_id: str, peer_id: str = "wxid_alice"):
    """用 mock 采集器给指定账号灌一批数据。"""
    from wequota.ingestion import Ingestor
    from wequota.metering import Meter
    from wequota.metering_pipeline import MeteringPipeline
    Ingestor(db, account_id).run(MockCollector(account_id=account_id, base_time=NOW))
    MeteringPipeline(db, Meter()).run_until_drained()


def test_accounts_endpoint_lists_ingested_accounts(db):
    client = TestClient(create_app(db=db))
    seed(db, "wxid_a")
    seed(db, "wxid_b")
    items = client.get("/api/accounts").json()["items"]
    ids = {i["account_id"] for i in items}
    assert {"wxid_a", "wxid_b"} <= ids
    a = next(i for i in items if i["account_id"] == "wxid_a")
    assert a["messages"] > 0
    assert a["last_message_at"]


def test_collector_status_endpoint_reports_state(db):
    client = TestClient(create_app(db=db))
    body = client.get("/api/collector/status").json()
    assert "installed" in body
    assert "worker" in body
    assert body["worker"]["account_id"] == "wxid_demo"


def test_collector_install_uses_current_python_interpreter(db, monkeypatch):
    import sys
    import wequota.app as app_module
    from wequota.collector.wechat_cli import CliStatus

    captured = {}

    def fake_ensure_cli(**kwargs):
        captured.update(kwargs)
        return CliStatus(False, platform="windows", message="not installed")

    monkeypatch.setattr(app_module, "ensure_cli", fake_ensure_cli)
    client = TestClient(create_app(db=db))
    response = client.post("/api/collector/install")

    assert response.status_code == 200
    assert captured["python_executable"] == sys.executable


def test_sync_run_endpoint_ingests_once(db):
    from wequota.collector.mock import MockCollector
    from wequota.sync import SyncWorker
    collector = MockCollector(account_id="wxid_demo", base_time=NOW)
    worker = SyncWorker(db, collector, account_id="wxid_demo")
    client = TestClient(create_app(db=db, sync_worker=worker))
    body = client.post("/api/sync/run").json()
    assert body["inserted"] > 0
    assert body["metered"] > 0
    assert client.get("/api/sync/status").json()["polls"] == 1


def test_stats_can_be_scoped_per_account(db):
    client = TestClient(create_app(db=db))
    seed(db, "wxid_a", "wxid_alice")
    seed(db, "wxid_b", "wxid_bob")
    a = client.get("/api/stats/summary", params={"days": 7, "account_id": "wxid_a"}).json()
    b = client.get("/api/stats/summary", params={"days": 7, "account_id": "wxid_b"}).json()
    assert a["tokens"] > 0 and b["tokens"] > 0
    assert a["messages"] == b["messages"] == 21
    rank_a = client.get("/api/stats/ranking",
                        params={"days": 7, "account_id": "wxid_a"}).json()["items"]
    assert {r["peer_id"] for r in rank_a} == {"wxid_alice", "wxid_bob", "wxid_carol"}


def test_default_account_keeps_backward_compatibility(db):
    """不传 account_id 时仍走默认账号，旧调用方式不受影响。"""
    client = TestClient(create_app(db=db))
    client.post("/api/ingest/mock")
    body = client.get("/api/stats/summary", params={"days": 7}).json()
    assert body["messages"] == 21


def test_quota_rules_are_isolated_by_account(db):
    client = TestClient(create_app(db=db))
    client.put("/api/quota/rules", json={
        "account_id": "wxid_a", "peer_id": "wxid_alice", "peer_name": "Alice",
        "window_type": "daily", "limit_tokens": 1000,
    })
    items = client.get("/api/quota/rules", params={"account_id": "wxid_b"}).json()["items"]
    assert items == []
    items = client.get("/api/quota/rules", params={"account_id": "wxid_a"}).json()["items"]
    assert len(items) == 1 and items[0]["peer_id"] == "wxid_alice"


def test_usage_endpoint_accepts_account(db):
    client = TestClient(create_app(db=db))
    client.put("/api/quota/rules", json={
        "account_id": "wxid_a", "peer_id": "wxid_alice", "peer_name": "Alice",
        "window_type": "daily", "limit_tokens": 1000,
    })
    items = client.get("/api/quota/usage", params={"account_id": "wxid_a"}).json()["items"]
    assert len(items) == 1 and items[0]["peer_id"] == "wxid_alice"


def test_timeseries_and_bytype_accept_account(db):
    client = TestClient(create_app(db=db))
    seed(db, "wxid_a")
    ts = client.get("/api/stats/timeseries",
                    params={"bucket": "day", "days": 7, "account_id": "wxid_a"}).json()
    assert ts["items"]
    bt = client.get("/api/stats/by-type",
                    params={"days": 7, "account_id": "wxid_a"}).json()
    assert bt["items"]


def test_heatmap_endpoint_accepts_account(db):
    client = TestClient(create_app(db=db))
    seed(db, "wxid_a")
    data = client.get("/api/stats/heatmap", params={"account_id": "wxid_a"}).json()
    assert data["year"] in data["years"]
    assert data["total"] > 0
    assert len(data["items"]) in (365, 366)
    assert any(item["count"] > 0 for item in data["items"])


def test_peers_endpoint_lists_all_contacts_without_top_limit(db):
    from wequota.contracts import MessageEnvelope
    msgs = []
    for index in range(30):
        msgs.append(MessageEnvelope(
            account_id="wxid_a", msg_id=f"peer-{index}", peer_id=f"wxid_p{index:02d}",
            peer_name=f"Peer {index}", direction="recv", msg_type="text",
            timestamp=NOW, text="x",
        ))
    db.upsert_messages(msgs)
    db.upsert_contacts("wxid_a", [{"username": "wxid_quiet", "name": "Quiet Contact"}])
    items = TestClient(create_app(db=db)).get(
        "/api/peers", params={"account_id": "wxid_a"}).json()["items"]
    assert len(items) == 31
    quiet = next(item for item in items if item["peer_id"] == "wxid_quiet")
    assert quiet["peer_name"] == "Quiet Contact"
    assert quiet["messages"] == 0


def test_collector_status_reports_wechat_cli_mode(db):
    from wequota.collector.mock import MockCollector
    from wequota.collector.wechat_cli import CliStatus
    collector = MockCollector(account_id="wxid_me")
    client = TestClient(create_app(
        db=db, collector=collector, account_id="wxid_me",
        collector_status=CliStatus(True, path="wechat-cli", version="0.3.0",
                                   message="wechat-cli 0.3.0", platform="windows"),
    ))
    body = client.get("/api/collector/status").json()
    assert body["installed"] is True
    assert body["mode"] == "wechat-cli"
    assert body["cli"]["version"] == "0.3.0"
    assert body["account_id"] == "wxid_me"


def test_sync_catchup_endpoint_runs_a_backfill_pass(db):
    from wequota.collector.mock import MockCollector
    from wequota.sync import SyncWorker
    worker = SyncWorker(db, MockCollector(account_id="wxid_demo", base_time=NOW),
                        account_id="wxid_demo")
    client = TestClient(create_app(db=db, sync_worker=worker))
    client.post("/api/sync/run")
    body = client.post("/api/sync/catchup").json()
    assert body["catchup"] is True
    _, ts = db.get_cursor("wxid_demo")
    assert ts is not None


def test_sync_start_and_stop(db):
    from wequota.collector.mock import MockCollector
    from wequota.sync import SyncWorker
    worker = SyncWorker(db, MockCollector(account_id="wxid_demo", base_time=NOW),
                        account_id="wxid_demo", interval_s=0.01, sleeper=lambda s: None)
    client = TestClient(create_app(db=db, sync_worker=worker))
    assert client.post("/api/sync/start").json()["running"] is True
    worker.stop()
    assert client.post("/api/sync/stop").json()["running"] is False
