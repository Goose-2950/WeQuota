"""M9：备份导入器（JSON / CSV / JSONL）测试。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wequota.importer import (
    ImportReport,
    import_file,
    import_rows,
    load_file,
    load_text,
    normalize_direction,
    normalize_timestamp,
    normalize_type,
    parse_rows,
    to_envelope,
)

NOW = datetime.now(timezone.utc)


# ---------- 字段归一化 ----------

def test_normalize_type_accepts_codes_and_names():
    assert normalize_type(1) == "text"
    assert normalize_type("3") == "image"
    assert normalize_type("语音") == "voice"
    assert normalize_type("VIDEO_CALL") == "video_call"
    assert normalize_type("未知类型") is None


def test_normalize_direction_variants():
    assert normalize_direction(True) == "send"
    assert normalize_direction("1") == "send"
    assert normalize_direction("out") == "send"
    assert normalize_direction("0") == "recv"
    assert normalize_direction(None) == "recv"


def test_normalize_timestamp_epoch_seconds_and_millis():
    assert normalize_timestamp(1700000000).year == 2023
    assert normalize_timestamp(1700000000000) == normalize_timestamp(1700000000)


def test_normalize_timestamp_strings():
    assert normalize_timestamp("2026-10-01 12:30:00") == \
        datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
    assert normalize_timestamp("2026/10/01 12:30:00").day == 1
    assert normalize_timestamp("2026-10-01T12:30:00").hour == 12
    assert normalize_timestamp("2026-10-01").day == 1
    assert normalize_timestamp("不是时间") is None


# ---------- 单行 → Envelope ----------

def test_to_envelope_alias_mapping():
    env, err = to_envelope({
        "msgId": "m1", "talker": "wxid_bob", "nickname": "Bob",
        "isSend": 1, "type": 1, "createTime": 1700000000,
        "content": "你好",
    }, account_id="wxid_me")
    assert err is None and env is not None
    assert env.msg_id == "m1" and env.peer_id == "wxid_bob"
    assert env.peer_name == "Bob" and env.direction == "send"
    assert env.msg_type == "text" and env.text == "你好"


def test_to_envelope_collects_media_meta():
    env, _ = to_envelope({
        "msg_id": "m2", "peer_id": "wxid_bob", "msg_type": "视频通话",
        "timestamp": "2026-10-01 10:00:00", "duration": 300,
    }, account_id="wxid_me")
    assert env.msg_type == "video_call"
    assert env.media_meta["duration_s"] == 300.0


def test_to_envelope_reports_error_for_bad_rows():
    env, err = to_envelope({"peer_id": "wxid_bob"}, account_id="wxid_me")
    assert env is None and "msg_id" in err


# ---------- 批量解析 ----------

def test_parse_rows_skips_group_chat(db_and_rows_factory=None):
    rows = [
        {"msg_id": "1", "peer_id": "wxid_bob", "msg_type": 1, "timestamp": 1700000000},
        {"msg_id": "2", "peer_id": "12345@chatroom", "msg_type": 1, "timestamp": 1700000000},
    ]
    envs, report = parse_rows(rows, "wxid_me", source="demo")
    assert len(envs) == 1 and report.parsed == 1 and report.skipped == 1
    assert report.total == 2 and not report.errors


def test_parse_rows_collects_errors_not_raise():
    rows = [
        {"msg_id": "1", "peer_id": "wxid_bob", "msg_type": 1, "timestamp": 1700000000},
        {"peer_id": "wxid_bob", "msg_type": 1, "timestamp": 1700000000},   # 缺 msg_id
        {"msg_id": "3", "peer_id": "wxid_bob", "msg_type": 999, "timestamp": 1700000000},
    ]
    envs, report = parse_rows(rows, "wxid_me")
    assert len(envs) == 1 and len(report.errors) == 2
    assert isinstance(report, ImportReport)


# ---------- 文件解析 ----------

def test_load_json_list_and_object():
    text = json.dumps([{"msg_id": "1"}], ensure_ascii=False)
    assert load_text(text, ".json") == [{"msg_id": "1"}]
    wrapped = json.dumps({"messages": [{"msg_id": "1"}]})
    assert load_text(wrapped, ".json") == [{"msg_id": "1"}]


def test_load_csv_rows():
    csv_text = ("msg_id,peer_id,msg_type,timestamp,content\n"
                "1,wxid_bob,1,2026-10-01 10:00:00,你好\n")
    rows = load_text(csv_text, ".csv")
    assert rows[0]["peer_id"] == "wxid_bob" and rows[0]["content"] == "你好"


def test_load_csv_strips_bom(tmp_path: Path):
    p = tmp_path / "bom.csv"
    p.write_text("\ufeffmsg_id,peer_id,msg_type,timestamp\n1,wxid_bob,1,2026-10-01 10:00:00\n",
                 encoding="utf-8")
    rows = load_file(p)
    assert rows[0]["msg_id"] == "1"      # BOM 被 utf-8-sig 吃掉，字段名干净


def test_load_jsonl(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"msg_id":"1"}\n{"msg_id":"2"}\n', encoding="utf-8")
    assert len(load_file(p)) == 2


def test_load_unknown_suffix_raises():
    with pytest.raises(ValueError):
        load_text("x", ".xlsx")


# ---------- 入库 ----------

@pytest.fixture()
def db(tmp_path):
    from wequota.storage import Database
    d = Database(tmp_path / "imp.db")
    d.init()
    return d


def _rows(n: int = 3):
    return [{"msg_id": f"m{i}", "peer_id": "wxid_bob", "msg_type": 1,
             "timestamp": 1700000000 + i, "content": f"第{i}条"}
            for i in range(n)]


def test_import_rows_dry_run_does_not_write(db):
    report = import_rows(_rows(), db, "wxid_me", dry_run=True)
    assert report.parsed == 3 and report.inserted == 0 and report.dry_run is True
    assert db.count_messages() == 0


def test_import_rows_writes_to_db(db):
    report = import_rows(_rows(), db, "wxid_me")
    assert report.inserted == 3 and db.count_messages() == 3


def test_import_rows_is_idempotent(db):
    import_rows(_rows(), db, "wxid_me")
    again = import_rows(_rows(), db, "wxid_me")
    assert again.inserted == 0          # (account_id, msg_id) 幂等
    assert db.count_messages() == 3


def test_import_file_runs_metering_when_pipeline_given(tmp_path: Path, db):
    from wequota.metering import Meter
    from wequota.metering_pipeline import MeteringPipeline
    from wequota.config import MeteringConfig

    p = tmp_path / "chat.csv"
    p.write_text("msg_id,peer_id,msg_type,timestamp,content\n"
                 "1,wxid_bob,1,2026-10-01 10:00:00,明天下午三点开会讨论方案\n",
                 encoding="utf-8")
    pipeline = MeteringPipeline(db, Meter(MeteringConfig()))
    report = import_file(p, db, "wxid_me", pipeline=pipeline)
    assert report.inserted == 1 and report.metered == 1
    rows = list(db.iter_metered("wxid_me"))
    assert rows and rows[0]["token_count"] > 0


# ---------- CLI 导入 ----------

def test_cli_handle_import_prints_report(tmp_path, capsys):
    from wequota.cli import handle_import
    from wequota.storage import Database

    p = tmp_path / "cli.csv"
    p.write_text("msg_id,peer_id,msg_type,timestamp,content\n"
                 "1,wxid_bob,1,2026-10-01 10:00:00,明天下午三点开会讨论方案\n"
                 "2,12345@chatroom,1,2026-10-01 10:00:00,群聊应被丢弃\n",
                 encoding="utf-8")
    db = Database(tmp_path / "cli.db")
    db.init()
    rc = handle_import(p, db, "wxid_me")
    out = capsys.readouterr().out
    assert rc == 0
    assert "解析 1 条" in out and "跳过群聊 1 条" in out
    assert db.count_messages() == 1


def test_cli_handle_import_dry_run_writes_nothing(tmp_path, capsys):
    from wequota.cli import handle_import
    from wequota.storage import Database

    p = tmp_path / "x.csv"
    p.write_text("msg_id,peer_id,msg_type,timestamp,content\n"
                 "1,wxid_bob,1,2026-10-01 10:00:00,你好\n", encoding="utf-8")
    db = Database(tmp_path / "x.db")
    db.init()
    assert handle_import(p, db, "wxid_me", dry_run=True) == 0
    assert "试解析" in capsys.readouterr().out
    assert db.count_messages() == 0


def test_cli_handle_import_bad_file_returns_1(tmp_path, capsys):
    from wequota.cli import handle_import
    from wequota.storage import Database

    db = Database(tmp_path / "y.db")
    db.init()
    assert handle_import(tmp_path / "missing.csv", db, "wxid_me") == 1
    assert "导入失败" in capsys.readouterr().out
