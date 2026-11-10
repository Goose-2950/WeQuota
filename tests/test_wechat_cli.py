"""M7 采集落地测试：wechat-cli 自动安装 / 检测 / git commit 锁定。

合规红线：本模块测试只涉及「外部命令调用」，不涉及任何解密逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

from wequota.collector import wechat_cli as cli_module
from wequota.collector.wechat_cli import (
    CLI_NAME,
    CliStatus,
    INSTALL_TIMEOUT,
    LATEST,
    PINNED_CLI_COMMIT,
    PINNED_CLI_VERSION,
    _environment_cli_binary,
    detect_cli,
    ensure_cli,
    install_plan,
    manual_install_hint,
)


VERSION_OUT = f"wechat-cli, version {PINNED_CLI_VERSION}\n"


class FakeRunner:
    """有状态的假执行器：装之前 wechat-cli 不可用，装成功后 --version 才有输出。"""

    def __init__(self, version_out: str = VERSION_OUT, installed: bool = False,
                 rc_by_tool: dict[str, int] | None = None, install_error: str = "",
                 payload: str | None = None, fetch_rc: int = 0, fetch_out: str = "",
                 fetch_err: str = ""):
        self.version_out = version_out
        self.installed = installed
        self.rc_by_tool = rc_by_tool or {}
        self.install_error = install_error
        self.payload = payload
        self.fetch_rc = fetch_rc
        self.fetch_out = fetch_out
        self.fetch_err = fetch_err
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]):
        self.calls.append(list(cmd))
        is_cli = Path(cmd[0]).stem == CLI_NAME
        if is_cli and "new-messages" in cmd:
            return self.fetch_rc, self.payload if self.payload is not None else self.fetch_out, \
                self.fetch_err
        if is_cli:
            if self.installed:
                return 0, self.version_out, ""
            return 127, "", f"{cmd[0]}: command not found"
        tool = "pip" if len(cmd) >= 3 and cmd[1:3] == ["-m", "pip"] else cmd[0]
        rc = self.rc_by_tool.get(tool, 0)
        if rc == 0:
            self.installed = True
            return 0, "installed ok", ""
        return rc, "", self.install_error or "permission denied"


# ---------------- 自动安装 ----------------

def test_install_plan_windows_uses_pip():
    plan = install_plan("windows", python_executable="C:/venv/Scripts/python.exe")
    assert plan and plan[0][:3] == ["C:/venv/Scripts/python.exe", "-m", "pip"]
    assert any("wechat-cli" in " ".join(cmd) for cmd in plan)


def test_install_plan_macos_prefers_npm_then_pip():
    plan = install_plan("darwin", python_executable="/venv/bin/python")
    assert len(plan) >= 2
    assert plan[0][0] == "npm"
    assert plan[1][:3] == ["/venv/bin/python", "-m", "pip"]


def test_install_plan_linux_uses_pinned_archive_zip():
    plan = install_plan("linux", python_executable="/venv/bin/python")
    assert len(plan) == 1
    assert plan[0][:3] == ["/venv/bin/python", "-m", "pip"]
    joined = " ".join(plan[0])
    assert "SarielChin/wechat-cli" in joined
    assert f"/archive/{PINNED_CLI_COMMIT}.zip" in joined


def test_environment_cli_binary_does_not_duplicate_scripts_dir(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    py = scripts / "python.exe"
    py.write_text("", encoding="utf-8")
    assert Path(_environment_cli_binary(str(py), "windows")) == scripts / "wechat-cli.exe"

    outside = tmp_path / "python.exe"
    outside.write_text("", encoding="utf-8")
    assert Path(_environment_cli_binary(str(outside), "windows")) == \
        tmp_path / "Scripts" / "wechat-cli.exe"

    bindir = tmp_path / "bin"
    bindir.mkdir()
    py_posix = bindir / "python"
    py_posix.write_text("", encoding="utf-8")
    assert Path(_environment_cli_binary(str(py_posix), "linux")) == bindir / "wechat-cli"


def test_detect_cli_reports_missing_binary():
    status = detect_cli(runner=FakeRunner(), platform_name="windows")
    assert status.installed is False
    assert status.source is None


def test_detect_cli_parses_version():
    status = detect_cli(runner=FakeRunner(installed=True), platform_name="windows")
    assert status.installed is True
    assert status.version == PINNED_CLI_VERSION


def test_ensure_cli_installs_then_detects():
    runner = FakeRunner()
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True,
                        python_executable="C:/venv/Scripts/python.exe")
    assert status.installed is True
    assert status.version == PINNED_CLI_VERSION
    assert status.source == "pip"
    assert any(cmd[:3] == ["C:/venv/Scripts/python.exe", "-m", "pip"]
               for cmd in runner.calls)


def test_default_install_uses_long_install_timeout(monkeypatch):
    """默认执行器必须用安装专用超时，否则 Git clone 会被 30 秒超时误杀。"""
    calls: list[tuple[list[str], float]] = []
    state = {"installed": False}

    def fake_run(cmd, timeout=30.0):
        calls.append((list(cmd), timeout))
        if "--version" in cmd:
            if state["installed"]:
                return 0, f"wechat-cli, version {PINNED_CLI_VERSION}\n", ""
            return 1, "", "not found"
        state["installed"] = True
        return 0, "installed ok", ""

    monkeypatch.setattr(cli_module, "run_command", fake_run)
    status = cli_module.ensure_cli(
        platform_name="linux", auto_install=True,
        python_executable="/venv/bin/python",
    )
    assert status.installed is True
    install_timeouts = [timeout for cmd, timeout in calls if "--version" not in cmd]
    assert install_timeouts == [cli_module.INSTALL_TIMEOUT]
    assert INSTALL_TIMEOUT >= 300


def test_ensure_cli_macos_falls_back_to_pip_when_npm_missing():
    runner = FakeRunner(rc_by_tool={"npm": 127, "pip": 0},
                        install_error="npm not found")
    status = ensure_cli(runner=runner, platform_name="darwin", auto_install=True,
                        python_executable="/venv/bin/python")
    assert status.installed is True
    assert status.source == "pip"
    assert any(cmd[0] == "npm" for cmd in runner.calls)
    assert any(cmd[:3] == ["/venv/bin/python", "-m", "pip"] for cmd in runner.calls)


def test_ensure_cli_macos_prefers_npm_when_available():
    runner = FakeRunner()
    status = ensure_cli(runner=runner, platform_name="darwin", auto_install=True)
    assert status.source == "npm"


def test_ensure_cli_reports_manual_steps_when_all_plans_fail():
    runner = FakeRunner(rc_by_tool={"pip": 1}, install_error="permission denied")
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True)
    assert status.installed is False
    assert status.source is None
    assert "wechat-cli" in status.message


def test_ensure_cli_does_not_install_when_disabled():
    runner = FakeRunner()
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=False)
    assert status.installed is False
    assert all(cmd[:3] != [sys.executable, "-m", "pip"] for cmd in runner.calls)


def test_ensure_cli_on_linux_installs_from_git():
    runner = FakeRunner()
    status = ensure_cli(runner=runner, platform_name="linux", auto_install=True)
    assert status.installed is True
    assert any(any("SarielChin/wechat-cli" in tok for tok in cmd) for cmd in runner.calls)


def test_already_installed_cli_is_not_reinstalled():
    runner = FakeRunner(installed=True)
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True)
    assert status.installed is True
    assert all(cmd[:3] != [sys.executable, "-m", "pip"] for cmd in runner.calls)


def test_cli_status_dataclass_defaults():
    s = CliStatus(installed=False, platform="windows")
    assert s.version is None and s.path is None and s.source is None


# ---------------- 版本锁定（遗留项 B3） ----------------

def test_install_plan_uses_git_source_without_version_pin():
    """PyPI 同名包被占用，只能从 git 源码安装；git 源无法用 == 锁版本。"""
    plan = install_plan("windows")
    assert any("SarielChin/wechat-cli" in tok for cmd in plan for tok in cmd)
    assert all("==" not in tok for cmd in plan for tok in cmd)


def test_install_plan_latest_does_not_pin():
    plan = install_plan("windows", LATEST)
    assert all(f"=={PINNED_CLI_VERSION}" not in " ".join(cmd) for cmd in plan)


def test_manual_install_hint_points_to_pinned_archive_zip():
    hint = manual_install_hint()
    assert "SarielChin/wechat-cli" in hint
    assert f"/archive/{PINNED_CLI_COMMIT}.zip" in hint
    assert "wechat-cli==" not in hint


def test_detect_cli_warns_when_version_differs_from_pin():
    runner = FakeRunner(installed=True, version_out="wechat-cli 9.9.9\n")
    status = detect_cli(runner=runner, platform_name="windows")
    assert status.version == "9.9.9"
    assert status.version_matched is False
    assert PINNED_CLI_VERSION in status.message


def test_detect_cli_is_quiet_when_version_matches_pin():
    runner = FakeRunner(installed=True, version_out=f"wechat-cli {PINNED_CLI_VERSION}\n")
    status = detect_cli(runner=runner, platform_name="windows")
    assert status.version_matched is True
    assert "不一致" not in status.message


def test_detect_cli_matched_is_none_when_unpinned():
    status = detect_cli(runner=FakeRunner(installed=True), platform_name="windows",
                        expected_version=LATEST)
    assert status.version_matched is None


def test_ensure_cli_installs_from_git_source():
    """git 源安装不锁版本，一次成功即可，不再有「回退不锁版本」那一轮。"""
    calls: list[list[str]] = []
    installed = [False]

    def runner(cmd: list[str]):
        calls.append(list(cmd))
        if Path(cmd[0]).stem == CLI_NAME:
            return (0, VERSION_OUT, "") if installed[0] else (127, "", "not found")
        installed[0] = True
        return 0, "installed ok", ""

    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True)
    assert status.installed is True
    pip_calls = [c for c in calls if c[:3] == [sys.executable, "-m", "pip"]]
    assert len(pip_calls) == 1
    assert any("SarielChin/wechat-cli" in tok for tok in pip_calls[0])


def test_ensure_cli_reports_failure_when_git_install_unavailable():
    runner = FakeRunner(rc_by_tool={"pip": 1}, install_error="No matching distribution")
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True)
    assert status.installed is False
    assert "→" in status.message


def test_ensure_cli_git_source_ignores_cli_version_override():
    runner = FakeRunner()
    ensure_cli(runner=runner, platform_name="windows", auto_install=True,
               cli_version="5.0.0")
    assert any(any("SarielChin/wechat-cli" in tok for tok in c) for c in runner.calls)
    assert all("wechat-cli==5.0.0" not in " ".join(c) for c in runner.calls)


def test_ensure_cli_latest_never_falls_back_to_pinned():
    runner = FakeRunner(rc_by_tool={"pip": 1}, install_error="boom")
    status = ensure_cli(runner=runner, platform_name="windows", auto_install=True,
                        cli_version=LATEST)
    assert status.installed is False
    # 不锁版本时只有一条安装命令（没有额外的回退轮）
    assert sum(1 for c in runner.calls if c[:3] == [sys.executable, "-m", "pip"]) == 1


def test_install_plan_pins_commit_by_default():
    plan = install_plan("windows")
    joined = " ".join(plan[0])
    assert f"/archive/{PINNED_CLI_COMMIT}.zip" in joined
    assert f"@{PINNED_CLI_COMMIT}" not in joined


def test_install_plan_can_override_git_ref():
    plan = install_plan("windows", ref="main")
    joined = " ".join(plan[0])
    assert "@main" in joined and PINNED_CLI_COMMIT not in joined


def test_install_plan_latest_ref_tracks_default_branch():
    plan = install_plan("windows", ref=LATEST)
    assert all("wechat-cli.git@" not in tok for tok in plan[0])


def test_ensure_cli_passes_git_ref_override():
    runner = FakeRunner()
    ensure_cli(runner=runner, platform_name="windows", auto_install=True,
               cli_ref="main")
    assert any(any(tok.endswith("@main") for tok in cmd) for cmd in runner.calls)
