"""中文/含空格路径测试（遗留项 F3 的同源风险）。

真机（中文 Windows 用户名、macOS 中文目录）跑不了之前，先把「路径里出现中文与空格」
这条最可能出问题的路径在本机覆盖掉：数据库、日志、备份、导入、转写缓存、数据目录。
"""

from __future__ import annotations

import logging

import pytest

from wequota.backup import snapshot_database
from wequota.contracts import MessageEnvelope
from wequota.importer import import_file
from wequota.resources import data_dir
from wequota.storage import Database
from wequota.transcribe import WhisperConfig, WhisperTranscriber

ZH_DIR = "用户 数据"
ZH_DB = "微信 数据.db"


def test_database_works_under_chinese_path(tmp_path):
    db = Database(tmp_path / ZH_DIR / ZH_DB)
    db.init()
    env = MessageEnvelope(account_id="wxid_中文", msg_id="m1", peer_id="p",
                          peer_name="张三", direction="send", msg_type="text",
                          timestamp="2026-10-13T10:00:00+00:00", text="你好")
    env.token_count = 3
    db.upsert_messages([env])
    assert db.count_messages() == 1
    assert db.path.exists()


def test_import_file_reads_chinese_named_backup(tmp_path):
    """备份文件名带中文（微信备份常见）时不能抛 UnicodeEncodeError。"""
    db = Database(tmp_path / ZH_DB)
    db.init()
    source = tmp_path / "聊天记录 备份.csv"
    source.write_text(
        "msg_id,peer_id,msg_type,timestamp,内容\n"
        "m1,wxid_zhangsan,text,2026-10-13 10:00:00,你好世界\n",
        encoding="utf-8",
    )
    report = import_file(source, db=db, account_id="wxid_me")
    assert report.ok() is True
    assert report.inserted == 1


def test_transcriber_accepts_chinese_model_dir(tmp_path):
    transcriber = WhisperTranscriber(WhisperConfig(),
                                     model_dir=tmp_path / "模型" / "faster-whisper-tiny")
    assert transcriber.model_dir.name == "faster-whisper-tiny"
    assert transcriber.model_ready() is False
    assert isinstance(transcriber.describe(), str)      # 描述里不能有路径编码异常


def test_logging_to_chinese_path(tmp_path):
    from wequota.cli import setup_logging

    logger = setup_logging(tmp_path / ZH_DIR / "日志" / "wequota.log",
                           logger_name="wequota-zh-path-test")
    logger.info("中文日志 %s", "✓")
    assert (tmp_path / ZH_DIR / "日志" / "wequota.log").exists()
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_backup_under_chinese_path(tmp_path):
    db = Database(tmp_path / ZH_DIR / ZH_DB)
    db.init()
    result = snapshot_database(db.path, tmp_path / "备份 目录", keep=2, day="20261013")
    assert result["ok"] is True


def test_data_dir_handles_chinese_home(tmp_path):
    """打包版落到平台用户目录时，用户名可能是中文（Windows 很常见）。"""
    home = tmp_path / "张 三"
    win = data_dir(frozen=True, platform_name="Windows", home=home)
    mac = data_dir(frozen=True, platform_name="Darwin", home=home)
    assert win.name == "WeQuota" and str(home) in str(win)
    assert mac.parts[-3:] == ("Library", "Application Support", "WeQuota")
