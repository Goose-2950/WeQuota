"""发布版自检（release smoke）：在**目标平台**上一键验证打包产物能不能跑。

用法：
    python -m installer.verify_release              # 跑全部检查
    python -m installer.verify_release --skip-model # 跳过模型检查

检查项：Python 版本 / 内置资源（前端+编码表+模型）/ 数据目录可写 / 数据库读写 /
开机自启往返（不留垃圾）/ API 启动 / 桌面通知可用性 / 语音模型。
任一项失败即返回非 0，便于放进发布流程的门禁。
"""

from __future__ import annotations

import argparse
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from wequota import __version__


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    optional: bool = False


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and not r.optional]

    @property
    def passed(self) -> list[CheckResult]:
        return [r for r in self.results if r.ok]


def check_python_version(version_info: tuple[int, int, int] | None = None) -> CheckResult:
    v = version_info or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    major, minor, micro = v[:3]
    ok = (major, minor) == (3, 11)
    detail = f"Python {major}.{minor}.{micro}（需要 Python 3.11.x）"
    return CheckResult("python", ok, detail)


def check_python() -> CheckResult:
    result = check_python_version()
    result.detail += (f"（{'打包产物' if getattr(sys, 'frozen', False) else '源码运行'}）"
                      f" · {platform.platform()}")
    return result


def check_bundled_assets() -> CheckResult:
    """打包版必须有前端产物与 tiktoken 编码表；模型缺失只提示（可后台下载）。"""
    from wequota.resources import resource_path

    frozen = bool(getattr(sys, "frozen", False))
    missing = []
    webapp = resource_path("wequota") / "webapp" / "index.html"
    cache = resource_path("assets") / "tiktoken_cache"
    if not webapp.exists():
        missing.append("wequota/webapp/index.html（前端未构建）")
    if not cache.exists():
        missing.append("assets/tiktoken_cache（离线编码表）")
    model_dir = resource_path("assets") / "models"
    has_model = model_dir.exists() and any(model_dir.rglob("model.bin"))
    detail = "内置资源齐全" if not missing else "缺少：" + "、".join(missing)
    detail += "；whisper 模型" + ("已内置" if has_model else "未内置（运行时按需下载）")
    if not frozen and missing:
        # 源码运行没构建前端是正常的，降级为提示
        return CheckResult("assets", False, detail + "（源码运行需先 npm run build）",
                           optional=True)
    return CheckResult("assets", not missing, detail)


def check_data_dir_writable(home: Path | None = None) -> CheckResult:
    from wequota.resources import data_dir

    d = data_dir(home=home)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("data-dir", True, str(d))
    except OSError as exc:
        return CheckResult("data-dir", False, f"{d} 不可写：{exc}")


def check_database(db_path: Path | None = None) -> CheckResult:
    from wequota.storage import Database

    db_path = db_path or (Path(tempfile.mkdtemp()) / "verify.db")
    try:
        db = Database(db_path)
        db.init()
        n = db.count_messages()
        return CheckResult("database", True, f"{db_path} 可读写（现有 {n} 条消息）")
    except Exception as exc:                      # 数据库打不开是硬故障
        return CheckResult("database", False, f"{db_path} 初始化失败：{exc}")


def check_autostart_roundtrip(home: Path | None = None) -> CheckResult:
    """装 → 查 → 卸，验证平台自启能力且**恢复验证前状态**。"""
    from wequota.autostart import AutostartManager, default_launch_command

    manager = AutostartManager(command=default_launch_command(), home=home)
    before = manager.status().enabled
    enabled = False
    try:
        installed = manager.install()
        if not installed.ok:
            return CheckResult("autostart", False, f"安装失败：{installed.message}")
        enabled = manager.status().enabled
    finally:
        # 不能在用户本来开着自启时把人家关掉：按验证前的状态还原。
        if before:
            manager.install()
        else:
            manager.uninstall()
    return CheckResult("autostart", enabled,
                       f"平台 {manager._platform}：装→查→卸 往返成功（事前已开启={before}）")


def check_api_boot(db_path: Path | None = None) -> CheckResult:
    from fastapi.testclient import TestClient

    from wequota.app import create_app
    from wequota.config import AppConfig
    from wequota.storage import Database

    db_path = db_path or (Path(tempfile.mkdtemp()) / "api.db")
    try:
        db = Database(db_path)
        db.init()
        # 用空配置启动：自检只验证 API 能否起来，不能被用户 config.json 里的
        # auth_token 拦成 401（否则开了访问令牌的用户自检必然失败）。
        client = TestClient(create_app(db=db, app_config=AppConfig()))
        health = client.get("/api/health")
        stats = client.get("/api/stats/summary", params={"days": 7})
        ok = health.status_code == 200 and stats.status_code == 200
        return CheckResult("api", ok, f"/api/health={health.status_code}、"
                                      f"/api/stats/summary={stats.status_code}")
    except Exception as exc:
        return CheckResult("api", False, f"API 启动失败：{exc}")


def check_notification() -> CheckResult:
    """只探测平台通知能力是否可用，不真的弹通知打扰用户。"""
    system = platform.system()
    if system == "Darwin":
        tool, hint = "osascript", "系统设置 → 通知 中允许 WeQuota"
    elif system == "Windows":
        tool, hint = "powershell", "需 Windows 10+，托盘气泡可能被「专注助手」拦截"
    else:
        tool, hint = "notify-send", "需安装 libnotify"
    from shutil import which

    found = which(tool) is not None
    return CheckResult("notify", found,
                       f"{system}: 依赖 {tool} {'已找到' if found else '未找到'}；{hint}",
                       optional=True)


def check_whisper() -> CheckResult:
    from wequota.config import WhisperConfig
    from wequota.transcribe import build_transcriber

    try:
        transcriber = build_transcriber(WhisperConfig(auto_download=False))
    except Exception as exc:
        return CheckResult("whisper", False, f"初始化失败：{exc}", optional=True)
    if transcriber is None:
        return CheckResult("whisper", False, "未安装 faster-whisper，语音按时长折算",
                           optional=True)
    ready = transcriber.model_ready()
    return CheckResult("whisper", ready, transcriber.describe(), optional=True)


DEFAULT_CHECKS: list[Callable[[], CheckResult]] = [
    check_python,
    check_bundled_assets,
    check_data_dir_writable,
    check_database,
    check_autostart_roundtrip,
    check_api_boot,
    check_notification,
    check_whisper,
]


def run_checks(checks: list[Callable[[], CheckResult]] | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in (checks or DEFAULT_CHECKS):
        try:
            results.append(check())
        except Exception as exc:                  # 单项检查炸了也要继续跑完
            results.append(CheckResult(getattr(check, "__name__", "check"), False,
                                       f"检查异常：{exc}"))
    return results


def all_ok(results: list[CheckResult]) -> bool:
    return all(r.ok or r.optional for r in results)


def render_report(results: list[CheckResult]) -> str:
    passed = sum(1 for r in results if r.ok)
    lines = [f"WeQuota v{__version__} 发布版自检 — {platform.platform()}", ""]
    for r in results:
        mark = "✓" if r.ok else ("·" if r.optional else "✗")
        tag = "（可选）" if r.optional else ""
        lines.append(f"  {mark} {r.name:<10}{tag} {r.detail}")
    lines.append("")
    lines.append(f"  通过 {passed}/{len(results)}"
                 + ("　→ 发布就绪" if all_ok(results) else "　→ 存在阻塞项，见 ✗"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WeQuota 发布版自检")
    parser.add_argument("--skip-model", action="store_true", help="跳过语音模型检查")
    args = parser.parse_args(argv)
    checks = [c for c in DEFAULT_CHECKS if not (args.skip_model and c is check_whisper)]
    results = run_checks(checks)
    print(render_report(results), flush=True)
    return 0 if all_ok(results) else 1


if __name__ == "__main__":
    sys.exit(main())
