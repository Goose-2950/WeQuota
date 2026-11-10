"""CLI 启动入口测试：随机空闲端口 + 控制台 logo。"""

from __future__ import annotations

import logging

import socket

import pytest

from wequota.cli import pick_free_port, render_banner
from wequota import __version__
from wequota.cli import archive_database, count_database_messages
from wequota.storage import Database


def test_pick_free_port_returns_bindable_port():
    port = pick_free_port()
    assert 1024 <= port <= 65535
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_pick_free_port_never_returns_fixed_8080_by_default():
    """大屏不固定端口：每次启动向系统申请空闲端口。"""
    ports = {pick_free_port() for _ in range(8)}
    assert len(ports) > 1 or 8080 not in ports


def test_pick_free_port_skips_occupied_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    occupied = s.getsockname()[1]
    try:
        for _ in range(5):
            assert pick_free_port() != occupied
    finally:
        s.close()


def test_banner_shows_project_name_and_version():
    banner = render_banner()
    assert "WeQuota" in banner
    assert __version__ in banner


def test_banner_includes_dashboard_url_line():
    banner = render_banner(dashboard_url="http://127.0.0.1:52341")
    assert "http://127.0.0.1:52341" in banner


def test_banner_shows_alert_channel_status():
    banner = render_banner(channel_status={
        "desktop": "enabled", "email": "未配置 SMTP", "dashboard": "enabled",
    })
    assert "桌面通知" in banner and "邮件提醒" in banner and "大屏弹窗" in banner
    assert "未配置 SMTP" in banner


def test_banner_without_channel_status_still_ok():
    assert "WeQuota" in render_banner()


def test_banner_shows_whisper_status():
    assert "语音转写" in render_banner(whisper_status="faster-whisper tiny（int8）就绪")
    assert "faster-whisper tiny" in render_banner(whisper_status="faster-whisper tiny（int8）就绪")


def test_banner_whisper_absent_by_default():
    assert "语音转写" not in render_banner()


def test_banner_marks_whisper_ready_with_check():
    banner = render_banner(whisper_status="faster-whisper tiny（int8）就绪", whisper_ready=True)
    assert "✓ 语音转写" in banner
    assert "· 语音转写" in render_banner(whisper_status="模型未下载", whisper_ready=False)


def test_banner_shows_collector_status():
    banner = render_banner(collector_status="wechat-cli 4.1.8 · 账号 wxid_demo",
                           collector_ready=True)
    assert "✓ 采集器" in banner and "wxid_demo" in banner
    assert "· 采集器" in render_banner(collector_status="未就绪", collector_ready=False)


def test_banner_without_collector_status_has_no_line():
    assert "采集器" not in render_banner()


def test_setup_collector_disabled_by_config():
    from wequota.config import CollectorConfig
    from wequota.cli import setup_collector
    collector, worker, status, line, ready = setup_collector(None, CollectorConfig(enabled=False), None)
    assert collector is None and worker is None
    assert ready is False and "关闭" in line


def test_setup_collector_builds_worker_when_cli_ready(tmp_path, monkeypatch):
    from wequota.cli import setup_collector
    from wequota import cli as cli_module
    from wequota.collector.wechat_cli import CliStatus
    from wequota.collector.huohuoer import HuohuoerCollector
    from wequota.config import CollectorConfig
    from wequota.storage import Database
    from wequota.sync import SyncWorker

    monkeypatch.setattr(
        cli_module, "ensure_cli",
        lambda **kw: CliStatus(True, path="wechat-cli", version="4.1.8",
                               message="wechat-cli 4.1.8", platform="windows"),
    )
    db = Database(tmp_path / "cli.db")
    db.init()
    cfg = CollectorConfig(account_id="wxid_me", auto_start=False)
    collector, worker, status, line, ready = setup_collector(db, cfg, None)
    assert isinstance(collector, HuohuoerCollector)
    assert isinstance(worker, SyncWorker)
    assert ready is True and "wxid_me" in line
    assert worker.account_id == "wxid_me"
    assert worker.is_alive() is False       # auto_start=False 不启动线程
    assert status.installed is True and status.version == "4.1.8"


def test_setup_collector_falls_back_when_cli_missing(tmp_path, monkeypatch):
    from wequota.cli import setup_collector
    from wequota import cli as cli_module
    from wequota.collector.wechat_cli import CliStatus
    from wequota.config import CollectorConfig
    from wequota.storage import Database

    monkeypatch.setattr(
        cli_module, "ensure_cli",
        lambda **kw: CliStatus(False, message="自动安装失败（permission denied）",
                               platform="windows"),
    )
    db = Database(tmp_path / "cli2.db")
    db.init()
    collector, worker, status, line, ready = setup_collector(db, CollectorConfig(), None)
    assert collector is None and worker is None and ready is False
    assert "手动安装" in line


# ---------------- 启动 logo（Claude Code 风格 + 绿色主色） ----------------

def test_logo_art_is_six_lines_and_fits_80_columns():
    from wequota.cli import LOGO_ART
    lines = LOGO_ART.strip("\n").splitlines()
    assert len(lines) == 6
    assert max(len(line) for line in lines) <= 66      # 80 列终端不折行


def test_logo_art_uses_shadow_block_glyphs():
    from wequota.cli import LOGO_ART
    assert "██╗" in LOGO_ART and "███████╗" in LOGO_ART


def test_supports_color_false_for_non_tty(monkeypatch):
    from wequota import cli

    class NotTty:
        def isatty(self):
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli.supports_color(NotTty()) is False


def test_supports_color_true_for_tty_but_respects_no_color(monkeypatch):
    from wequota import cli

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cli.supports_color(Tty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.supports_color(Tty()) is False


def test_supports_color_false_when_stream_has_no_isatty(monkeypatch):
    from wequota import cli
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli.supports_color(object()) is False


def test_banner_plain_text_has_no_escape_sequences():
    banner = render_banner(color=False, dashboard_url="http://127.0.0.1:52341")
    assert "\x1b[" not in banner
    assert "http://127.0.0.1:52341" in banner


def test_banner_colored_contains_escape_and_reset():
    banner = render_banner(color=True, dashboard_url="http://127.0.0.1:52341")
    assert "\x1b[38;5;78m" in banner and "\x1b[0m" in banner
    assert "http://127.0.0.1:52341" in banner


def test_banner_colored_keeps_mark_and_label_contiguous():
    """上色不能把 "✓ 采集器" 这类文案切成两半（旧断言依赖它整段出现）。"""
    banner = render_banner(color=True, collector_status="wechat-cli 4.1.8",
                           collector_ready=True, whisper_status="tiny 就绪",
                           whisper_ready=True)
    assert "✓ 采集器" in banner and "✓ 语音转写" in banner
    assert "· 采集器" in render_banner(color=True, collector_status="未就绪",
                                      collector_ready=False)


def test_paint_returns_plain_text_when_disabled():
    from wequota.cli import paint
    assert paint("abc", "\x1b[38;5;78m", False) == "abc"
    assert paint("", "\x1b[38;5;78m", True) == ""
    assert paint("abc", "\x1b[38;5;78m", True).endswith("\x1b[0m")


def test_enable_windows_vt_is_noop_on_non_windows():
    from wequota import cli
    cli.enable_windows_vt()          # 非 Windows 直接返回，不应抛异常


def test_display_width_counts_fullwidth_as_two():
    from wequota.cli import display_width
    assert display_width("abc") == 3
    assert display_width("采集器") == 6


def test_pad_label_aligns_by_display_width():
    from wequota.cli import display_width, pad_label
    assert display_width(pad_label("数据库就绪")) == 12
    assert display_width(pad_label("采集器")) == 12
    assert display_width(pad_label("abc")) == 12


def test_banner_values_share_one_column():
    """不同长度的中文标签，取值列对齐在终端里看是同一列。"""
    from wequota.cli import display_width
    banner = render_banner(color=False, db_path="/x/wequota.db",
                           collector_status="ready", collector_ready=True,
                           whisper_status="tiny", whisper_ready=True)
    rows = [(l, v) for l, v in [(line, val) for line in banner.splitlines()
                                for val in ("/x/wequota.db", "ready", "tiny")
                                if line.rstrip().endswith(val)]]
    columns = {display_width(l[:l.rindex(v)]) for l, v in rows}
    assert len(rows) == 3
    assert len(columns) == 1        # 三条的取值都从同一列开始 → 终端里对齐


# ---------- --download-model ----------

class _StubTranscriber:
    def __init__(self, ok: bool, message: str) -> None:
        self._ok = ok
        self._message = message
        self.model_dir = "/tmp/models/faster-whisper-tiny"
        self.calls = 0

    def pinned_revision(self) -> str:
        return "d90ca5fe260221311c53c58e660288d3deb8d356"

    def download_model(self):
        self.calls += 1
        return self._ok, self._message


class _StubWhisperConfig:
    model_size = "tiny"


def test_handle_download_model_prints_target_and_result(capsys):
    from wequota.cli import handle_download_model
    stub = _StubTranscriber(True, "已下载 Systran/faster-whisper-tiny@d90ca5fe2602")
    code = handle_download_model(stub, _StubWhisperConfig())
    out = capsys.readouterr().out
    assert code == 0
    assert stub.calls == 1
    assert "d90ca5fe2602" in out and "✓" in out


def test_handle_download_model_failure_returns_nonzero(capsys):
    from wequota.cli import handle_download_model
    stub = _StubTranscriber(False, "下载失败: network error")
    assert handle_download_model(stub, _StubWhisperConfig()) == 1
    assert "✗" in capsys.readouterr().out


def test_handle_download_model_when_whisper_disabled(capsys):
    from wequota.cli import handle_download_model
    assert handle_download_model(None, _StubWhisperConfig()) == 0
    assert "关闭" in capsys.readouterr().out


# ---------- 启动快照备份（遗留项 F1） ----------

def test_backup_is_skipped_when_disabled(tmp_path):
    from wequota.cli import handle_backup
    from wequota.config import BackupConfig

    logger = logging.getLogger("wequota-test-backup")
    assert handle_backup(tmp_path / "a.db", BackupConfig(enabled=False), logger) is None


def test_backup_reports_saved_snapshot(tmp_path):
    from wequota.cli import handle_backup
    from wequota.config import BackupConfig
    from wequota.storage import Database

    db = Database(tmp_path / "wequota.db")
    db.init()
    logger = logging.getLogger("wequota-test-backup")
    note = handle_backup(db.path, BackupConfig(keep=2), logger, backup_dir=tmp_path)
    assert note is not None and note.startswith("快照 ")
    assert (tmp_path / "backups").exists()


def test_backup_failure_never_breaks_startup(tmp_path, monkeypatch):
    """备份抛异常也要照常启动：只记日志，不向上抛。"""
    import wequota.cli as cli_module
    from wequota.config import BackupConfig

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cli_module, "snapshot_database", boom)
    logger = logging.getLogger("wequota-test-backup")
    assert cli_module.handle_backup(tmp_path / "a.db", BackupConfig(), logger) is None

def test_count_database_messages_and_archive(tmp_path):
    db_path = tmp_path / "wequota.db"
    db = Database(db_path)
    db.init()
    from wequota.contracts import MessageEnvelope
    from datetime import datetime, timezone
    db.upsert_messages([MessageEnvelope(
        account_id="wxid_a", msg_id="m1", peer_id="wxid_b", peer_name="B",
        direction="recv", msg_type="text",
        timestamp=datetime.now(timezone.utc), text="x",
    )])
    assert count_database_messages(db_path) == 1
    archived = archive_database(db_path)
    assert archived is not None and archived.exists()
    assert not db_path.exists()
    assert count_database_messages(db_path) == 0
