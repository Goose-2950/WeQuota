"""直读解密副本的路径/索引测试（不碰真实微信）。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from wequota.collector.decrypted_cache import DecryptedCache, open_readonly
from wequota.collector.wechat_db import message_table_index, table_for_username


def _make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (value TEXT)")
    conn.execute("INSERT INTO t VALUES ('ok')")
    conn.commit()
    conn.close()


def test_open_readonly_handles_unicode_space_and_uri_specials(tmp_path):
    # `?` 在 Windows 上是非法文件名字符，只在 POSIX 上覆盖；`#` 两边都合法。
    special = "message?x=1.db" if sys.platform != "win32" else "message#x=1.db"
    path = tmp_path / "中文 目录#1" / special
    _make_db(path)
    conn = open_readonly(path)
    try:
        assert conn.execute("SELECT value FROM t").fetchone()["value"] == "ok"
    finally:
        conn.close()


def test_open_readonly_rejects_writes(tmp_path):
    path = tmp_path / "message_0.db"
    _make_db(path)
    conn = open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES ('nope')")
    finally:
        conn.close()


def test_message_table_index_maps_tables_to_db_paths(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    msg_db = tmp_path / "message_0.db"
    _make_db(msg_db)
    table = table_for_username("wxid_friend")
    conn = sqlite3.connect(msg_db)
    conn.execute(f"CREATE TABLE {table} (local_id INTEGER)")
    conn.commit()
    conn.close()
    (cache_dir / "_mtimes.json").write_text(json.dumps({
        "message/message_0.db": {"path": str(msg_db)},
    }), encoding="utf-8")

    cache = DecryptedCache(cache_dir=cache_dir)
    index = message_table_index(cache)
    assert index[table] == str(msg_db)
