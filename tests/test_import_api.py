"""M9：备份导入接口测试（/api/import/preview|run）。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from wequota.app import create_app
from wequota.storage import Database

CSV = ("msg_id,peer_id,peer_name,msg_type,timestamp,content,duration\n"
       "1,wxid_bob,Bob,1,2026-10-01 10:00:00,明天下午三点开会讨论方案细节,\n"
       "2,wxid_bob,Bob,语音通话,2026-10-01 11:00:00,,180\n")


@pytest.fixture()
def client(tmp_path):
    db = Database(tmp_path / "imp.db")
    db.init()
    return TestClient(create_app(db=db))


def _upload(client, endpoint, content, filename, **params):
    """用 raw body 上传（接口不依赖 multipart），文件名走 X-Filename 头。"""
    return client.post(endpoint, content=content.encode("utf-8"),
                       headers={"X-Filename": filename}, params=params or None)


def test_preview_reports_parse_result(client, tmp_path):
    p = tmp_path / "a.csv"
    resp = _upload(client, "/api/import/preview", CSV, "a.csv", account_id="wxid_me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["parsed"] == 2 and data["inserted"] == 0 and data["dry_run"] is True
    assert data["total"] == 2 and data["error_count"] == 0


def test_preview_does_not_write_db(client, tmp_path):
    p = tmp_path / "a.csv"
    _upload(client, "/api/import/preview", CSV, "a.csv", account_id="wxid_me")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/stats/summary", params={"days": 7}).json()["messages"] == 0


def test_run_imports_and_meters(client, tmp_path):
    p = tmp_path / "b.csv"
    data = _upload(client, "/api/import/run", CSV, "b.csv", account_id="wxid_me").json()
    assert data["ok"] is True and data["inserted"] == 2 and data["metered"] == 2
    summary = client.get("/api/stats/summary",
                         params={"days": 3650, "account_id": "wxid_me"}).json()
    assert summary["messages"] == 2      # 文本 + 语音通话都计量
    assert summary["tokens"] > 0


def test_run_rejects_unknown_suffix(client, tmp_path):
    p = tmp_path / "c.xlsx"
    resp = _upload(client, "/api/import/run", "x", "c.xlsx")
    assert resp.json()["ok"] is False and "不支持" in resp.json()["error"]


def test_run_dry_run_form_flag(client, tmp_path):
    p = tmp_path / "d.csv"
    data = _upload(client, "/api/import/run", CSV, "d.csv", account_id="wxid_me", dry_run="true").json()
    assert data["inserted"] == 0 and data["parsed"] == 2


def test_import_json_array(client, tmp_path):
    payload = json.dumps([
        {"msg_id": "j1", "peer_id": "wxid_alice", "nickname": "Alice", "isSend": 0,
         "type": 1, "createTime": 1700000000, "content": "你好呀"},
    ], ensure_ascii=False)
    p = tmp_path / "e.json"
    data = _upload(client, "/api/import/run", payload, "e.json", account_id="wxid_me").json()
    assert data["parsed"] == 1 and data["inserted"] == 1


def test_import_registers_account(client, tmp_path):
    p = tmp_path / "f.csv"
    _upload(client, "/api/import/run", CSV, "f.csv", account_id="wxid_imported")
    accounts = client.get("/api/accounts").json()["items"]
    assert any(a["account_id"] == "wxid_imported" for a in accounts)
