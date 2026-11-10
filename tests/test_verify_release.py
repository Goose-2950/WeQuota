"""M8+：发布版自检脚本（目标平台一键跑，本机 Linux 先跑通）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from installer.verify_release import (
    CheckResult,
    check_api_boot,
    check_autostart_roundtrip,
    check_bundled_assets,
    check_database,
    check_python,
    check_python_version,
    render_report,
    run_checks,
)


def test_check_python_reports_version():
    r = check_python()
    assert r.ok is (sys.version_info[:2] == (3, 11))
    assert r.name == "python"


def test_check_python_version_accepts_only_python_311():
    assert check_python_version((3, 10, 11)).ok is False
    assert check_python_version((3, 11, 0)).ok is True
    assert check_python_version((3, 11, 9)).ok is True
    assert check_python_version((3, 12, 0)).ok is False


def test_check_bundled_assets_detects_missing_webapp(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)   # 空的 bundle
    r = check_bundled_assets()
    assert r.ok is False
    assert "webapp" in r.detail


def test_check_bundled_assets_ok_when_all_present(monkeypatch, tmp_path: Path):
    (tmp_path / "wequota" / "webapp").mkdir(parents=True)
    (tmp_path / "wequota" / "webapp" / "index.html").write_text("<html>", encoding="utf-8")
    (tmp_path / "assets" / "tiktoken_cache").mkdir(parents=True)
    (tmp_path / "assets" / "tiktoken_cache" / "x").write_text("1", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    r = check_bundled_assets()
    assert r.ok is True
    assert "whisper" in r.detail or "模型" in r.detail      # 模型缺失只是提示


def test_check_database_writes_and_reopens(tmp_path: Path):
    r = check_database(db_path=tmp_path / "sub" / "verify.db")
    assert r.ok is True


def test_check_autostart_roundtrip_restores_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from wequota.autostart import AutostartManager
    before = AutostartManager(command=["x"], home=tmp_path).status().enabled
    r = check_autostart_roundtrip(home=tmp_path)
    assert r.ok is True
    after = AutostartManager(command=["x"], home=tmp_path).status().enabled
    assert before == after          # 往返后状态还原，不留垃圾


def test_check_api_boot_hits_health(tmp_path: Path):
    r = check_api_boot(db_path=tmp_path / "api.db")
    assert r.ok is True
    assert "/api/health" in r.detail


def test_run_checks_collects_all_results():
    results = run_checks(checks=[
        lambda: CheckResult("a", True, "ok"),
        lambda: CheckResult("b", False, "bad"),
    ])
    assert [r.name for r in results] == ["a", "b"]
    assert results[0].ok and not results[1].ok


def test_run_checks_swallows_check_exception():
    def boom():
        raise RuntimeError("炸了")
    results = run_checks(checks=[boom])
    assert len(results) == 1 and results[0].ok is False
    assert "炸了" in results[0].detail


def test_render_report_marks_pass_and_fail():
    text = render_report([CheckResult("a", True, "ok"), CheckResult("b", False, "bad")])
    assert "✓" in text and "✗" in text
    assert "1/2" in text or "通过 1" in text


def test_render_report_exit_zero_only_when_all_ok():
    from installer.verify_release import all_ok
    assert all_ok([CheckResult("a", True, "")]) is True
    assert all_ok([CheckResult("a", True, ""), CheckResult("b", False, "")]) is False
