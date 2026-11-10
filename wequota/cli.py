"""WeQuota CLI 启动入口：ASCII logo + 随机空闲端口 + 控制台提示。"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import uvicorn

from .__init__ import __version__
from .alerting import build_dispatcher
from .app import create_app
from .autostart import AutostartManager, default_launch_command
from .backup import snapshot_database
from .config import AppConfig, BackupConfig, CollectorConfig, config_version_note, \
    dump_default_config, load_alert_config, load_app_config, load_backup_config, \
    load_collector_config, load_quota_config, load_whisper_config
from .collector.huohuoer import build_default_collector
from .collector.wechat_cli import CLI_NAME, CliStatus, MANUAL_INSTALL_HINT, \
    WechatCliError, ensure_cli
from .firstrun import init_guidance, needs_first_run, run_wizard
from .resources import data_dir
from .setup_ui import run_setup
from .storage import Database
from .sync import SyncWorker
from .transcribe import WhisperTranscriber, build_transcriber, model_pin

# logo / 配色 / 对齐工具放在 brand.py（setup_ui 也要用，避免循环导入），
# 这里重新导出以保证 `from wequota.cli import LOGO_ART, paint, ...` 继续可用。
from .brand import C_DIM, C_MARK, C_RESET, C_TITLE, C_VALUE, LOGO, LOGO_ART, \
    center_line, display_width, pad_label, paint, supports_color

STATUS_LINES = [
    ("数据库就绪", "data/wequota.db"),
]

CHANNEL_LABELS = {"desktop": "桌面通知", "email": "邮件提醒", "dashboard": "大屏弹窗"}


def configure_stdio() -> None:
    """把 stdout/stderr 统一切成 UTF-8 + replace。

    Windows 控制台（尤其输出被重定向到文件时）默认用 GBK，banner 里的 ✓ / · / ➜
    等符号无法编码，会直接抛 UnicodeEncodeError 让进程在启动第一步就崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass          # 某些宿主环境不允许改编码，忽略即可


def enable_windows_vt() -> None:
    """Windows 老控制台默认不解析 ANSI 转义序列，这里打开虚拟终端模式。

    失败就静默降级（banner 变纯文本），绝不能让启动流程因为上色而崩。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32              # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)            # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass



def pick_free_port() -> int:
    """向操作系统申请一个空闲端口（不固定 8080，避免端口冲突）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class RuntimePaths:
    """运行时路径：开发版在仓库内，打包版落到平台用户数据目录。"""
    data_dir: Path
    db_path: Path
    config_path: Path
    log_path: Path


def resolve_runtime_paths(frozen: bool | None = None,
                          platform_name: str | None = None,
                          home: Path | None = None) -> RuntimePaths:
    """统一决定数据库 / 配置 / 日志放哪里。

    开发版沿用仓库布局（config.json 在仓库根、data/ 存库）；
    打包版（PyInstaller）全部放到平台用户数据目录，避免往安装目录写文件。
    """
    from .resources import is_frozen
    if frozen is None:
        frozen = is_frozen()
    d = data_dir(frozen=frozen, platform_name=platform_name, home=home)
    if frozen:
        return RuntimePaths(data_dir=d, db_path=d / "wequota.db",
                            config_path=d / "config.json",
                            log_path=d / "logs" / "wequota.log")
    repo_root = d.parent                       # data_dir 开发版 = 仓库根/data
    return RuntimePaths(data_dir=d, db_path=d / "wequota.db",
                        config_path=repo_root / "config.json",
                        log_path=d / "logs" / "wequota.log")


def setup_logging(log_path: Path, logger_name: str = "wequota") -> logging.Logger:
    """控制台 + 滚动文件双通道日志；重复调用幂等（不重复挂 handler）。"""
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    return logger


def handle_import(path: Path, db: Database, account_id: str, dry_run: bool = False,
                  quota_default_limit: int = 0) -> int:
    """CLI 导入备份文件：解析 → 入库 → 计量 → 限额，打印回执。"""
    from .config import load_config
    from .importer import import_file
    from .metering import Meter
    from .metering_pipeline import MeteringPipeline
    from .quota import QuotaEngine
    from .transcribe import build_transcriber

    whisper_config = load_whisper_config()
    transcriber = build_transcriber(whisper_config, db=db)
    pipeline = MeteringPipeline(db, Meter(load_config(), transcriber=transcriber))
    quota = QuotaEngine(db, account_id, default_limit_tokens=quota_default_limit)
    try:
        report = import_file(path, db=db, account_id=account_id, dry_run=dry_run,
                             pipeline=pipeline, quota=quota)
    except (ValueError, OSError) as exc:
        print(f"  ✗ 导入失败：{exc}", flush=True)
        return 1
    tag = "试解析" if dry_run else "已导入"
    print(f"  {'✓' if report.ok() else '✗'} {tag} {report.source}："
          f"共 {report.total} 行，解析 {report.parsed} 条，跳过群聊 {report.skipped} 条，"
          f"入库 {report.inserted} 条，计量 {report.metered} 条", flush=True)
    for err in report.errors[:5]:
        print(f"    · 第 {err['row']} 行：{err['reason']}", flush=True)
    if len(report.errors) > 5:
        print(f"    · ……另有 {len(report.errors) - 5} 条错误", flush=True)
    return 0 if report.ok() else 1


def handle_download_model(transcriber: WhisperTranscriber | None,
                          config) -> int:
    """按清单锁定版本下载语音模型（换设备/首次部署用），完成后退出。"""
    if transcriber is None:
        print("  · 语音转写已在配置中关闭（config.json → whisper.enabled = false）", flush=True)
        return 0
    pin = model_pin(config.model_size)
    revision = transcriber.pinned_revision()
    target = f"{pin.repo_id}@{revision[:12]}" if pin else config.model_size
    print(f"  ➜ 下载语音模型 {target}", flush=True)
    print(f"    目标目录 {transcriber.model_dir}", flush=True)
    ok, message = transcriber.download_model()
    print(f"  {'✓' if ok else '✗'} {message}", flush=True)
    return 0 if ok else 1


def handle_setup(config_path: Path, logger: logging.Logger | None = None,
                 screen_factory: Callable[..., Any] | None = None) -> tuple[bool, str]:
    """跑启动配置页；失败自动回退到旧问答向导，绝不阻塞启动。"""
    factory = screen_factory or run_setup
    try:
        result = factory(config_path, autostart=AutostartManager(command=default_launch_command()))
    except Exception as exc:            # 终端能力再怎么怪，也必须让服务起得来
        if logger:
            logger.warning("启动配置页不可用，回退问答向导：%s", exc)
        legacy = run_wizard(config_path, io=_ConsoleIO())
        return legacy.ok, legacy.message
    if result.saved:
        print(f"  ✓ 配置已写入 {config_path}（以后可用 --setup 再改）", flush=True)
    return result.saved, result.message


class _ConsoleIO:
    """真实控制台 IO（向导用）。"""

    def prompt(self, text: str) -> str:
        return input(text)

    def print(self, text: str = "") -> None:
        print(text, flush=True)


def count_database_messages(db_path: Path) -> int:
    """不创建数据库，只读已有库的消息数；失败按 0 处理。"""
    if not db_path.exists():
        return 0
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return 0


def archive_database(db_path: Path) -> Path | None:
    """初始化覆盖前把旧库移到 data/backups/，而不是直接删除。"""
    if not db_path.exists():
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"wequota-before-setup-{stamp}.db"
    shutil.move(str(db_path), str(target))
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink(missing_ok=True)
    return target


def maybe_reset_data_for_setup(db_path: Path, *, forced: bool = False) -> None:
    """--setup 后处理已有数据：提示并提供初始化覆盖。

    非 TTY / 未确认时保留原库；只有明确确认或 `--reset-data` 才归档旧库，
    下一次启动重新全量读取微信。
    """
    count = count_database_messages(db_path)
    if count <= 0:
        return
    print("", flush=True)
    print(f"  ! 检测到当前数据库已有 {count:,} 条消息。", flush=True)
    print("  初始化覆盖会归档旧数据库，并重新从微信全量读取；额度、提醒记录也会重新开始。", flush=True)
    confirmed = forced
    if not confirmed:
        if not sys.stdin.isatty():
            print("  当前不是交互终端，已保留现有数据；如需覆盖请加 --reset-data。", flush=True)
            return
        answer = input("  是否清空当前数据并重新初始化采集？(y/N)：").strip().lower()
        confirmed = answer in ("y", "yes", "是")
    if not confirmed:
        print("  已保留现有数据，继续使用当前数据库。", flush=True)
        return
    try:
        target = archive_database(db_path)
    except OSError as exc:
        print(f"  ✗ 旧数据库归档失败：{exc}", flush=True)
        print("    请先退出正在运行的 WeQuota，再重新执行 --setup。", flush=True)
        return
    if target:
        print(f"  ✓ 旧数据库已归档到 {target}", flush=True)
        print("  ✓ 下次启动会创建空库并重新读取微信；全量回填期间请保持微信登录。", flush=True)


def handle_backup(db_path: Path, config: BackupConfig, logger: logging.Logger,
                  backup_dir: Path | None = None) -> str | None:
    """启动时做一份当日数据库快照；失败只告警，绝不挡住启动。"""
    if not config.enabled:
        return None
    target_dir = Path(config.dir) if config.dir else (backup_dir or db_path.parent) / "backups"
    try:
        result = snapshot_database(db_path, target_dir, keep=config.keep)
    except Exception as exc:            # 备份是最不关键的一环，出任何岔子都不能影响启动
        logger.warning("数据库快照失败：%s", exc)
        return None
    if not result["ok"]:
        logger.warning("数据库快照失败：%s", result["error"])
        return None
    if result["skipped"]:
        return None                     # 当天已有，静默
    removed = result["removed"]
    suffix = f"，清理 {len(removed)} 份旧快照" if removed else ""
    logger.info("数据库快照已保存：%s%s", result["path"], suffix)
    return f"快照 {Path(result['path']).name}{suffix}"


def handle_autostart(action: str, manager: AutostartManager | None = None) -> int:
    """--autostart install/uninstall/status 子命令。"""
    manager = manager or AutostartManager(command=default_launch_command())
    if action == "install":
        result = manager.install()
    elif action == "uninstall":
        result = manager.uninstall()
    else:
        status = manager.status()
        mark = "✓" if status.enabled else "·"
        print(f"  {mark} 开机自启：{'已开启' if status.enabled else '未开启'}"
              f"（{status.detail}）", flush=True)
        return 0
    print(f"  {'✓' if result.ok else '✗'} {result.message}", flush=True)
    return 0 if result.ok else 1


def render_banner(
    dashboard_url: str | None = None,
    db_path: str | Path = "data/wequota.db",
    channel_status: dict[str, str] | None = None,
    whisper_status: str | None = None,
    whisper_ready: bool = False,
    collector_status: str | None = None,
    collector_ready: bool = False,
    color: bool | None = None,
) -> str:
    """拼出启动 banner。color=None 自动判断（交互终端才上色）。"""
    use = supports_color() if color is None else color
    lines = [
        paint(LOGO_ART.strip("\n"), C_MARK, use),
        paint(f"  WeQuota v{__version__} — 微信聊天 Token 限额工具", C_TITLE, use),
        "",
    ]
    for label, value in STATUS_LINES:
        val = value if label != "数据库就绪" else db_path
        # 标记与标签一起上色，保证 "✓ 采集器" 这类文本不被转义序列切断
        lines.append(f"  {paint(f'✓ {pad_label(label)}', C_MARK, use)}  "
                     f"{paint(str(val), C_VALUE, use)}")
    if collector_status:
        mark = "✓" if collector_ready else "·"
        head = f"{mark} {pad_label('采集器')}"
        lines.append(f"  {paint(head, C_MARK, use)}  {paint(collector_status, C_VALUE, use)}")
    if channel_status:
        lines.append("")
        for name, label in CHANNEL_LABELS.items():
            state = channel_status.get(name, "未知")
            mark = "✓" if state == "enabled" else "·"
            lines.append(f"  {paint(mark, C_MARK, use)} {paint(f'提醒通道 {pad_label(label, 10)}', C_DIM, use)}"
                         f" {paint(str(state), C_VALUE, use)}")
    if whisper_status:
        lines.append("")
        mark = "✓" if whisper_ready else "·"
        whisper_head = f"{mark} {pad_label('语音转写')}"
        lines.append(f"  {paint(whisper_head, C_MARK, use)}"
                     f"  {paint(str(whisper_status), C_VALUE, use)}")
    if dashboard_url:
        lines += ["", f"  {paint('➜', C_MARK, use)}  {paint('数据大屏', C_DIM, use)}"
                      f"  {paint(dashboard_url, C_VALUE, use)}", ""]
    return "\n".join(lines)


def start_model_download(transcriber: WhisperTranscriber) -> None:
    """后台下载 whisper 模型，避免拖慢启动（期间语音计量走时长降级）。"""
    def _run() -> None:
        ok, message = transcriber.download_model()
        print(f"  {'✓' if ok else '·'} 语音模型下载: {message}", flush=True)

    threading.Thread(target=_run, name="whisper-download", daemon=True).start()


def setup_collector(db: Database, config: CollectorConfig,
                    dispatcher, quota_default_limit: int = 0) -> tuple[object | None, object | None, CliStatus | None, str, bool]:
    """检测/安装 wechat-cli 并建好同步线程。

    返回 (collector, worker, cli_status, 状态文案, 是否就绪)；未就绪时前两项为 None，
    由调用方降级为示例数据模式（大屏仍可用）。
    """
    if not config.enabled:
        return None, None, None, "已在配置中关闭（collector.enabled = false）", False

    status = ensure_cli(auto_install=config.auto_install,
                        binary=config.binary or None,
                        cli_version=config.cli_version or None,
                        cli_ref=config.cli_ref or None)
    if not status.installed:
        # status.message 通常已带手动安装指引，避免重复拼接
        hint = status.message if "手动安装" in status.message else \
            f"{status.message}；手动安装：{MANUAL_INSTALL_HINT}"
        return None, None, status, hint, False

    collector = build_default_collector(
        config.account_id, db, config, binary=config.binary or status.path or None)
    worker = SyncWorker(
        db, collector, dispatcher=dispatcher, account_id=config.account_id,
        interval_s=config.poll_interval_s, sleep_interval_s=config.sleep_interval_s,
        catchup_days=config.catchup_days, max_failures=config.max_failures,
        quota_default_limit=quota_default_limit,
    )
    if config.auto_start:
        worker.start()
    return collector, worker, status, f"{status.message} · 账号 {config.account_id}", True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wequota", description="WeQuota — 微信聊天 Token 限额工具")
    parser.add_argument("--autostart", choices=["install", "uninstall", "status"],
                        help="设置/取消/查看开机自启")
    parser.add_argument("--setup", action="store_true", help="重新运行首次启动向导")
    parser.add_argument("--reset-data", action="store_true",
                        help="配合 --setup：归档旧数据库并重新全量读取微信")
    parser.add_argument("--no-wizard", action="store_true", help="首次启动不进入交互向导")
    parser.add_argument("--import", dest="import_path",
                        help="导入备份文件（.json/.jsonl/.csv/.tsv）后退出，不启动服务")
    parser.add_argument("--account", default=None,
                        help="导入时指定的微信号 ID（默认取配置里的 collector.account_id）")
    parser.add_argument("--dry-run", action="store_true",
                        help="配合 --import：只解析并报告，不写库")
    parser.add_argument("--download-model", action="store_true",
                        help="按清单锁定版本下载语音转写模型后退出（换设备重新部署时用）")
    parser.add_argument("--migrate-money", action="store_true",
                        help="重扫解密副本，把旧库里的 type=49 转账/红包回填分类后退出")
    args = parser.parse_args(argv)
    configure_stdio()          # Windows GBK 控制台下先保住中文输出

    if args.autostart:
        sys.exit(handle_autostart(args.autostart))

    paths = resolve_runtime_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(paths.log_path)

    # 首次启动：TTY 下进入配置页（logo 居中 + 分组选项），非 TTY（服务/后台）直接落默认配置
    config_path = paths.config_path
    first_run = needs_first_run(config_path)
    if args.setup or (first_run and not args.no_wizard and sys.stdin.isatty()):
        saved, message = handle_setup(config_path, logger=logger)
        if not saved and args.setup:
            print(f"  · {message}", flush=True)
        if saved and first_run:
            print("", flush=True)
            print(init_guidance(), flush=True)
            print("", flush=True)
        if saved and args.setup:
            maybe_reset_data_for_setup(paths.db_path, forced=args.reset_data)
    if not config_path.exists():
        dump_default_config(config_path)
        print(f"  ✓ 已生成默认配置文件 {config_path}（可调整计量系数与提醒设置）", flush=True)
    else:
        note = config_version_note(config_path)
        if note:
            print(f"  · 配置提示：{note}", flush=True)

    port = pick_free_port()
    db = Database(paths.db_path)
    db.init()
    collector_config = load_collector_config(config_path)
    quota_config = load_quota_config(config_path)
    app_config = load_app_config(config_path)
    backup_config = load_backup_config(config_path)

    # 每天一份数据库快照（失败只告警）：给断电/误导入留一个回滚点
    handle_backup(paths.db_path, backup_config, logger, backup_dir=paths.data_dir)

    # --import：导入备份文件后直接退出，不启动服务
    if args.import_path:
        account = args.account or collector_config.account_id
        sys.exit(handle_import(Path(args.import_path), db, account, args.dry_run,
                               quota_default_limit=quota_config.default_limit_tokens))

    # --migrate-money：旧库 type=49 重分类后退出，不启动服务
    if args.migrate_money:
        account = args.account or collector_config.account_id
        cli_status = ensure_cli(
            auto_install=collector_config.auto_install,
            binary=collector_config.binary or None,
            cli_version=collector_config.cli_version or None,
            cli_ref=collector_config.cli_ref or None,
        )
        if not cli_status.installed:
            print(f"  ✗ 资金事件回填失败：{cli_status.message}", flush=True)
            sys.exit(1)
        collector = build_default_collector(
            account, db, collector_config, binary=cli_status.path)
        try:
            updated = collector.migrate_money()
        except WechatCliError as exc:
            print(f"  ✗ 资金事件回填失败：{exc}", flush=True)
            sys.exit(1)
        if collector.last_error:
            print(f"  · {collector.last_error}", flush=True)
        print(f"  ✓ 资金事件回填完成：更新 {updated} 条（历史事件只入库不提醒）", flush=True)
        sys.exit(0)

    logger.info("WeQuota v%s 启动，数据目录 %s", __version__, paths.data_dir)

    whisper_config = load_whisper_config(config_path)
    transcriber = build_transcriber(whisper_config, db=db)

    # --download-model：按锁定版本拉取模型后退出，不启动服务
    if args.download_model:
        sys.exit(handle_download_model(transcriber, whisper_config))

    alert_config = load_alert_config(config_path)
    dispatcher, hub = build_dispatcher(alert_config, db=db)

    collector, worker, cli_status, collector_line, collector_ready = setup_collector(
        db, collector_config, dispatcher, quota_default_limit=quota_config.default_limit_tokens)
    if collector is None and collector_config.enabled:
        # 没装采集器时用示例数据跑通全链路，大屏不至于空着
        print(f"  · 采集器未就绪，改用示例数据（mock）", flush=True)

    whisper_line = None
    if transcriber is not None:
        whisper_line = transcriber.describe()
        if not transcriber.model_ready() and whisper_config.auto_download:
            whisper_line += "（后台下载中，期间语音按时长折算）"
            start_model_download(transcriber)

    enable_windows_vt()
    print(render_banner(
        dashboard_url=f"http://127.0.0.1:{port}",
        db_path=db.path,
        channel_status=dispatcher.channel_status(),
        whisper_status=whisper_line,
        whisper_ready=bool(transcriber is not None and transcriber.model_ready()),
        collector_status=collector_line,
        collector_ready=collector_ready,
    ), flush=True)
    app = create_app(db=db, dispatcher=dispatcher, hub=hub,
                     transcriber=transcriber, whisper_config=whisper_config,
                     collector=collector, sync_worker=worker,
                     account_id=collector_config.account_id,
                     collector_status=cli_status,
                     collector_config=collector_config,
                     quota_config=quota_config,
                     app_config=app_config, backup_config=backup_config,
                     config_path=config_path)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
