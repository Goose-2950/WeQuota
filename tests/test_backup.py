"""数据库快照备份测试（遗留项 F1 的轻量版）。"""

from __future__ import annotations

import sqlite3

import pytest

from wequota.backup import rotate, snapshot_database, snapshot_path
from wequota.config import BackupConfig
from wequota.storage import Database


def _seed(db_path) -> None:
    db = Database(db_path)
    db.init()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("INSERT INTO messages (account_id, peer_id, peer_name, msg_id,"
                     " direction, msg_type, timestamp, ingested_at) VALUES"
                     " ('a','p','P','m1','send','text','2026-10-13T00:00:00+00:00',"
                     " '2026-10-13T00:00:01+00:00')")
        conn.commit()


def test_snapshot_creates_a_copy_of_the_database(tmp_path):
    db_path = tmp_path / "wequota.db"
    _seed(db_path)
    result = snapshot_database(db_path, tmp_path / "backups", keep=3, day="20261013")
    assert result["ok"] is True
    snapshot = tmp_path / "backups" / "wequota-20261013.db"
    assert snapshot.exists()
    with sqlite3.connect(str(snapshot)) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_snapshot_is_skipped_when_today_already_has_one(tmp_path):
    db_path = tmp_path / "wequota.db"
    _seed(db_path)
    backup_dir = tmp_path / "backups"
    first = snapshot_database(db_path, backup_dir, keep=3, day="20261013")
    second = snapshot_database(db_path, backup_dir, keep=3, day="20261013")
    assert first["skipped"] is False and second["skipped"] is True
    assert len(list(backup_dir.iterdir())) == 1      # 同一天不会堆出多份


def test_snapshot_keeps_only_the_newest_n(tmp_path):
    db_path = tmp_path / "wequota.db"
    _seed(db_path)
    backup_dir = tmp_path / "backups"
    for day in ("20261010", "20261011", "20261012", "20261013"):
        snapshot_database(db_path, backup_dir, keep=3, day=day)
    names = sorted(p.name for p in backup_dir.iterdir())
    assert names == ["wequota-20261011.db", "wequota-20261012.db", "wequota-20261013.db"]


def test_rotate_removes_oldest_first_and_tolerates_keep_zero(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in ("20261001", "20261002", "20261003"):
        (backup_dir / f"wequota-{day}.db").write_text("x")
    removed = rotate(backup_dir, keep=2)
    assert removed == ["wequota-20261001.db"]
    assert rotate(backup_dir, keep=0) == []          # keep<=0 = 不清理，别误删


def test_snapshot_reports_error_instead_of_raising(tmp_path):
    result = snapshot_database(tmp_path / "missing.db", tmp_path / "backups")
    assert result["ok"] is False
    assert result["error"]


def test_snapshot_path_uses_given_day():
    assert snapshot_path("/tmp/b", "20261231").name == "wequota-20261231.db"


def test_backup_config_defaults():
    cfg = BackupConfig()
    assert cfg.enabled is True and cfg.keep == 3 and cfg.dir == ""


def test_backup_config_ignores_unknown_keys():
    cfg = BackupConfig.from_dict({"enabled": False, "typo": 1})
    assert cfg.enabled is False
    assert cfg.keep == 3


def test_snapshot_survives_chinese_paths(tmp_path):
    """中文目录名不能成为备份失败的理由（遗留项 F3 的同源风险）。"""
    db_dir = tmp_path / "数据 目录"
    db_dir.mkdir()
    db_path = db_dir / "微信.db"
    _seed(db_path)
    result = snapshot_database(db_path, tmp_path / "备份", keep=2, day="20261013")
    assert result["ok"] is True
    assert (tmp_path / "备份" / "wequota-20261013.db").exists()
