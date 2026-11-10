"""M13 直读解密副本采集链路测试（用合成 SQLite 缓存，不碰真实微信）。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wequota.collector.decrypted_cache import DecryptedCache
from wequota.collector.huohuoer import HuohuoerCollector
from wequota.contracts import MessageEnvelope
from wequota.collector.wechat_db import (
    is_private_username,
    read_contacts,
    row_to_envelope,
    table_for_username,
)
from wequota.storage import Database

PEER = "wxid_friend"
ME = "wxid_me"

TRANSFER_XML = (
    '<msg><appmsg><type>2000</type><wcpayinfo>'
    '<feedesc>¥66.60</feedesc><pay_memo>午饭</pay_memo></wcpayinfo></appmsg></msg>'
)
RED_PACKET_XML = (
    '<msg><appmsg><type>2001</type><wcpayinfo>'
    '<sendertitle>恭喜发财</sendertitle></wcpayinfo></appmsg></msg>'
)


def _ok_runner(cmd):
    return (0, "", "")


def _build_cache(tmp_path: Path, with_ct: bool = True) -> DecryptedCache:
    contact_db = tmp_path / "contact_0.db"
    conn = sqlite3.connect(contact_db)
    conn.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)")
    conn.executemany("INSERT INTO contact VALUES (?,?,?)", [
        (PEER, "好友昵称", "好友备注"),
        ("group@chatroom", "某群", None),
        ("gh_news", "公众号", None),
    ])
    conn.commit()
    conn.close()

    msg_db = tmp_path / "message_0.db"
    conn = sqlite3.connect(msg_db)
    conn.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    conn.executemany("INSERT INTO Name2Id VALUES (?,?)", [(1, PEER), (2, ME)])
    table = table_for_username(PEER)
    ct_column = "WCDB_CT_message_content INTEGER" if with_ct else ""
    conn.execute(
        f"CREATE TABLE {table} (local_id INTEGER PRIMARY KEY, local_type INTEGER, "
        f"create_time INTEGER, real_sender_id INTEGER, message_content BLOB"
        f"{', ' + ct_column if ct_column else ''})"
    )
    rows = [
        (1, 1, 1000, 1, "你好，在吗", 0),
        (2, 49, 2000, 1, TRANSFER_XML, 0),
        (3, 49, 3000, 2, RED_PACKET_XML, 0),
        (4, 1, 4000, 2, "在的", 0),
    ]
    if with_ct:
        conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?)", rows)
    else:
        conn.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            [row[:5] for row in rows],
        )
    conn.commit()
    conn.close()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "_mtimes.json").write_text(json.dumps({
        "contact/contact.db": {"path": str(contact_db), "db_mt": 0, "wal_mt": 0},
        "message/message_0.db": {"path": str(msg_db), "db_mt": 0, "wal_mt": 0},
    }), encoding="utf-8")
    return DecryptedCache(cache_dir=cache_dir, state_dir=tmp_path / "state")


def test_read_contacts_and_private_filter(tmp_path):
    cache = _build_cache(tmp_path)
    contacts = {c["username"]: c["name"] for c in read_contacts(cache)}
    assert contacts[PEER] == "好友备注"
    assert is_private_username(PEER) is True
    assert is_private_username("group@chatroom") is False
    assert is_private_username("gh_news") is False


def test_row_to_envelope_classifies_money_and_direction(tmp_path):
    cache = _build_cache(tmp_path)
    from wequota.collector.wechat_db import iter_peer_envelopes

    envs = iter_peer_envelopes(cache, PEER, "好友备注", ME, since_create_time=0)
    by_id = {e.msg_id: e for e in envs}
    assert f"{PEER}:1" in by_id and by_id[f"{PEER}:1"].msg_type == "text"

    transfer = by_id[f"{PEER}:2"]
    assert transfer.msg_type == "transfer"
    assert transfer.is_money_event and not transfer.is_metered
    assert transfer.amount_yuan == 66.6
    assert transfer.extra["memo"] == "午饭"
    assert transfer.direction == "recv"

    packet = by_id[f"{PEER}:3"]
    assert packet.msg_type == "red_packet"
    assert packet.direction == "send"          # real_sender_id=2 是本人
    assert packet.amount_yuan is None


def test_collector_backfills_and_advances_peer_cursor(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    collector = HuohuoerCollector(ME, db=db, runner=_ok_runner, cache=cache)

    messages = collector.fetch()
    assert len(messages) == 4
    assert db.get_peer_cursor(ME, PEER) == 0        # 入库成功前不推进游标
    db.upsert_messages(messages)
    collector.ack(messages)
    assert db.get_peer_cursor(ME, PEER) == 4000     # newest create_time
    assert db.get_peer_cursor_state(ME, PEER) == (4000, 4)
    # 游标包含 local_id：下一轮不会把 4000 这一秒的边界消息整组重复读出来。
    assert collector.fetch() == []
    # 入库后应留下 2 条资金事件等待提醒
    assert len(db.pending_money_messages()) == 2


def test_peer_cursor_local_id_reads_same_second_tail(tmp_path):
    cache = _build_cache(tmp_path)
    from wequota.collector.wechat_db import iter_peer_envelopes

    tail = iter_peer_envelopes(
        cache, PEER, "好友备注", ME,
        since_create_time=4000, since_local_id=3,
    )
    assert [m.msg_id for m in tail] == [f"{PEER}:4"]


def test_fetch_progress_finishes_reading_below_100(tmp_path):
    """读取联系人只是流水线前半段，进度必须留给入库与 Token 分析。"""
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    collector = HuohuoerCollector(ME, db=db, runner=_ok_runner, cache=cache)

    messages = collector.fetch()
    progress = collector.progress
    assert len(messages) == 4
    assert progress["stage"] == "collected"
    assert progress["active"] is True
    assert progress["percent"] == 85
    assert progress["processed"] == progress["total"] == 1
    assert progress["messages"] == 4


def test_fetch_uses_global_cap_and_zero_means_unlimited(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    collector = HuohuoerCollector(
        ME, db=db, runner=_ok_runner, cache=cache,
        limit_per_peer=10, full_backfill_limit=10,
        max_messages_per_poll=2,
    )
    first = collector.fetch()
    assert [m.msg_id for m in first] == [f"{PEER}:1", f"{PEER}:2"]

    # 显式 0 = 本轮不限；未入库/未 ack 时仍从起点读取。
    all_messages = collector.fetch(limit=0)
    assert [m.msg_id for m in all_messages] == [
        f"{PEER}:1", f"{PEER}:2", f"{PEER}:3", f"{PEER}:4",
    ]


def test_full_backfill_uses_large_limit_until_caught_up(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    collector = HuohuoerCollector(
        ME, db=db, runner=_ok_runner, cache=cache,
        limit_per_peer=1, full_backfill_limit=3,
    )

    first = collector.fetch()
    assert {m.msg_id for m in first} == {f"{PEER}:1", f"{PEER}:2", f"{PEER}:3"}
    db.upsert_messages(first)
    collector.ack(first)
    assert db.get_peer_cursor(ME, PEER) == 3000

    second = collector.fetch()
    # 精确游标：3000 秒边界上的 :3 已入库，不再重复读，只取 :4。
    assert {m.msg_id for m in second} == {f"{PEER}:4"}
    db.upsert_messages(second)
    collector.ack(second)
    assert db.get_peer_cursor_state(ME, PEER) == (4000, 4)
    assert db.get_meta(collector._backfill_key(PEER)) == "1"

    # 追平后转回增量限流：游标已在 4000/:4，下一轮正常读到空。
    third = collector.fetch()
    assert third == []


def test_full_backfill_limit_zero_fetches_all_in_one_poll(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    collector = HuohuoerCollector(
        ME, db=db, runner=_ok_runner, cache=cache,
        limit_per_peer=1, full_backfill_limit=0,
    )
    assert len(collector.fetch()) == 4
    assert db.get_meta(collector._backfill_key(PEER)) == "1"


def test_collector_incremental_includes_boundary_second(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    db.set_peer_cursor(ME, PEER, 2000)
    collector = HuohuoerCollector(ME, db=db, runner=_ok_runner, cache=cache)

    messages = collector.fetch()
    # >= 会重复带出边界消息，靠 (account_id, msg_id) 幂等去重，避免同秒消息丢失。
    assert {m.msg_id for m in messages} == {
        f"{PEER}:2", f"{PEER}:3", f"{PEER}:4",
    }


def test_collector_refreshes_decrypted_cache_every_poll(tmp_path):
    cache = _build_cache(tmp_path)
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(list(cmd))
        return (0, "", "")

    collector = HuohuoerCollector(ME, runner=runner, cache=cache)
    collector.fetch()
    collector.fetch()
    assert sum(1 for c in calls if "contacts" in c) == 2
    assert sum(1 for c in calls if "history" in c) == 2


def test_collector_catchup_rewinds_per_peer_cursor(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    db.set_peer_cursor(ME, PEER, 5000)
    collector = HuohuoerCollector(ME, db=db, runner=_ok_runner, cache=cache)

    since = datetime.fromtimestamp(2500, tz=timezone.utc)
    messages = collector.fetch(since=since)
    assert {m.msg_id for m in messages} == {
        f"{PEER}:3", f"{PEER}:4",
    }


def test_collector_skips_contacts_without_message_table(tmp_path):
    cache = _build_cache(tmp_path)
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(list(cmd))
        return (0, "", "")

    collector = HuohuoerCollector(ME, runner=runner, cache=cache)
    messages = collector.fetch()
    # 群/公众号 + 无消息表的联系人都不应触发额外读取。
    assert len(messages) == 4
    assert sum(1 for c in calls if "history" in c) == 1


def test_call_duration_parses_seconds_and_clock_formats():
    from wequota.collector.wechat_db import call_duration_s

    assert call_duration_s("<voip><msg>Duration:12</msg></voip>") == 12.0
    assert call_duration_s("<voip><msg>Duration:01:23</msg></voip>") == 83.0
    assert call_duration_s("<voip><msg>Duration:1:02:03</msg></voip>") == 3723.0
    assert call_duration_s("<voip><msg>Canceled</msg></voip>") is None


def test_row_to_envelope_meters_voice_and_video_calls():
    from wequota.collector.wechat_db import RawMessage

    voice = RawMessage(1, 50, 1000, 1,
                       "<voip><msg>Duration:12</msg></voip>", 50, 0)
    env = row_to_envelope(voice, PEER, "好友", ME)
    assert env is not None
    assert env.msg_type == "voice_call" and env.duration_s == 12.0

    video = RawMessage(2, 50, 2000, 1,
                       '<voip type="video"><msg>Duration:00:30</msg></voip>', 50, 0)
    env = row_to_envelope(video, PEER, "好友", ME)
    assert env is not None
    assert env.msg_type == "video_call" and env.duration_s == 30.0


def test_row_to_envelope_downgrades_call_without_duration():
    from wequota.collector.wechat_db import RawMessage

    raw = RawMessage(3, 50, 3000, 1, "<voip><msg>Canceled</msg></voip>", 50, 0)
    env = row_to_envelope(raw, PEER, "好友", ME)
    assert env is not None and env.msg_type == "other"


def test_decompress_zstd_compressed_content():
    zstd = pytest.importorskip("zstandard")
    from wequota.collector.wechat_db import decompress_content

    payload = zstd.ZstdCompressor().compress("压缩消息".encode("utf-8"))
    assert decompress_content(payload, 4) == "压缩消息"


def test_reads_db_without_ct_column(tmp_path):
    from wequota.collector.wechat_db import iter_peer_envelopes

    cache = _build_cache(tmp_path, with_ct=False)
    envs = iter_peer_envelopes(cache, PEER, "好友备注", ME, since_create_time=0)
    assert [e.msg_id for e in envs] == [f"{PEER}:1", f"{PEER}:2", f"{PEER}:3", f"{PEER}:4"]


def test_migrate_money_reclassifies_legacy_rows_and_silences_history(tmp_path):
    cache = _build_cache(tmp_path)
    db = Database(tmp_path / "cli.db")
    db.init()
    db.upsert_messages([
        MessageEnvelope(account_id=ME, msg_id=f"{PEER}:2", peer_id=PEER,
                        peer_name="好友备注", direction="recv", msg_type="file",
                        timestamp=datetime.fromtimestamp(2000, tz=timezone.utc)),
        MessageEnvelope(account_id=ME, msg_id=f"{PEER}:3", peer_id=PEER,
                        peer_name="好友备注", direction="send", msg_type="file",
                        timestamp=datetime.fromtimestamp(3000, tz=timezone.utc)),
    ])
    collector = HuohuoerCollector(ME, db=db, runner=_ok_runner, cache=cache)

    assert db.unclassified_money_peers(ME) == [(PEER, "好友备注")]
    assert collector.migrate_money() == 2

    rows = {r["msg_id"]: r for r in db.query_messages(account_id=ME)}
    assert rows[f"{PEER}:2"]["msg_type"] == "transfer"
    assert json.loads(rows[f"{PEER}:2"]["extra"])["amount_yuan"] == 66.6
    assert rows[f"{PEER}:3"]["msg_type"] == "red_packet"
    # 历史事件在跟踪起点之前：入库但静默，避免升级后补发旧提醒
    assert all(r["money_alerted"] == 1 for r in rows.values())

    # 幂等：迁移完成后没有待分类的 file 行，不再扫描
    assert collector.migrate_money() == 0
