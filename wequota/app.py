"""FastAPI 应用。

M1 提供查询与 mock 入库；M2 计量、M3 限额、M4 提醒通道逐步接入。
"""

from __future__ import annotations

import asyncio
import hmac
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .__init__ import __version__
from .account_profile import detect_profile
from .alerting import AlertDispatcher, AlertHub, build_dispatcher, EmailChannel
from .collector import Collector
from .collector.mock import MockCollector
from .collector.wechat_cli import CliStatus, ensure_cli
from .config import AlertConfig, AppConfig, BackupConfig, CollectorConfig, load_alert_config, \
    load_app_config, load_backup_config, load_collector_config, \
    load_config, load_quota_config, load_whisper_config, MeteringConfig, QuotaConfig, WhisperConfig, \
    CONFIG_SCHEMA_VERSION
from .settings import apply_values, read_values, save_values, schema_dict, SettingError
from .ingestion import Ingestor
from .importer import import_rows, load_text
from .metering import Meter
from .metering_pipeline import MeteringPipeline
from .quota import AlertEvent, AlertLevel, QuotaEngine, QuotaRule
from .stats import bucketize, by_type, daily_report, heatmap, rank_peers, summarize
from .storage import Database
from .sync import SyncWorker
from .transcribe import WhisperTranscriber, build_transcriber


class MergeAccountsRequest(BaseModel):
    stable_id: str | None = None
    account_ids: list[str] = []
    confirm: bool = False
    target_account_id: str | None = None

DEFAULT_DB_PATH = Path("data/wequota.db")
DEFAULT_ACCOUNT_ID = "wxid_demo"
# 前端构建产物目录（web/ 为 Vue3 源码，vite build 输出到此处；打包版走 _MEIPASS）
from .resources import webapp_dir as _webapp_dir
WEBAPP_DIR = _webapp_dir()

FRONTEND_HINT = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>WeQuota</title>
<style>body{background:#0f1420;color:#e6ebf5;font-family:"PingFang SC","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
div{max-width:520px;line-height:1.9}code{background:#1e2536;padding:2px 6px;border-radius:4px}
a{color:#4c8dff}</style></head><body><div>
<h2>WeQuota 数据大屏未构建</h2>
<p>前端源码在 <code>web/</code>（Vue3 + Vite + ECharts），先构建一次：</p>
<p><code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>构建产物会输出到 <code>wequota/webapp/</code>，刷新本页即可看到大屏。<br/>
开发调试可用 <code>npm run dev</code>（Vite 热更新，已配置 /api 与 /ws 代理）。</p>
<p>后端 API 已可用，见 <a href="/docs">/docs</a>。</p>
</div></body></html>"""


def _bearer_token(header: str | None) -> str:
    """从 `Authorization: Bearer xxx` 里取出 token；格式不对返回空串。"""
    value = (header or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _token_matches(provided: str | None, expected: str) -> bool:
    """常数时间比较，避免通过响应时间猜测 token。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def create_app(
    db: Database | None = None,
    dispatcher: AlertDispatcher | None = None,
    hub: AlertHub | None = None,
    alert_config: AlertConfig | None = None,
    transcriber: WhisperTranscriber | None = None,
    whisper_config: WhisperConfig | None = None,
    collector: Collector | None = None,
    sync_worker: SyncWorker | None = None,
    account_id: str | None = None,
    collector_status: CliStatus | None = None,
    collector_config: CollectorConfig | None = None,
    quota_config: QuotaConfig | None = None,
    app_config: AppConfig | None = None,
    backup_config: BackupConfig | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。

    `config_path` 为空表示**纯默认配置**（测试/嵌入式调用），不读取工作目录的
    `config.json`；CLI 与 `uvicorn wequota.app:app` 会显式传入配置文件路径。
    """
    app_config = app_config or (load_app_config(config_path) if config_path else AppConfig())
    db = db or Database(DEFAULT_DB_PATH)
    db.init()
    app = FastAPI(title="WeQuota", version=__version__, description="微信聊天 Token 限额工具")
    app.state.db = db
    app.state.default_account_id = account_id or DEFAULT_ACCOUNT_ID
    # 设置面板保存时回写的位置；留空表示「只改内存、不落盘」（测试或未指定时用）
    app.state.config_path = str(config_path) if config_path else ""
    auth_token = (app_config.auth_token or "").strip()
    cors_origins = [str(o).strip() for o in (app_config.cors_origins or []) if str(o).strip()]
    # 留空 = 仅同源（默认最安全）；需要跨域时显式配置 app.cors_origins
    app.add_middleware(
        CORSMiddleware, allow_origins=cors_origins, allow_methods=["*"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def _auth_guard(request: Request, call_next):
        if (auth_token and request.method != "OPTIONS"
                and request.url.path.startswith("/api/")):
            provided = (_bearer_token(request.headers.get("authorization"))
                        or request.query_params.get("token", ""))
            if not _token_matches(provided, auth_token):
                return JSONResponse(
                    {"detail": "未授权：请提供访问令牌"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "WeQuota", "version": __version__}

    whisper_config = whisper_config or (load_whisper_config(config_path) if config_path
                                        else WhisperConfig())
    if transcriber is None:
        transcriber = build_transcriber(whisper_config, db=db)
    meter = Meter(load_config(config_path) if config_path else MeteringConfig(),
                  transcriber=transcriber)
    pipeline = MeteringPipeline(db, meter)
    quota_config = quota_config or (load_quota_config(config_path) if config_path
                                    else QuotaConfig())
    quota = QuotaEngine(db, app.state.default_account_id,
                        default_limit_tokens=quota_config.default_limit_tokens)
    app.state.transcriber = transcriber
    if alert_config is None:
        # 外部注入 dispatcher 时以它内部那份为准——改它才真的生效（热更新靠原地改这个对象）
        alert_config = (dispatcher.config if dispatcher is not None
                        else load_alert_config(config_path) if config_path else AlertConfig())
    if dispatcher is None:
        dispatcher, hub = build_dispatcher(alert_config, db=db, hub=hub)
    hub = hub or AlertHub()
    app.state.hub = hub
    app.state.dispatcher = dispatcher
    backup_config = backup_config or (load_backup_config(config_path) if config_path
                                      else BackupConfig())

    # ---------------- M7 采集与同步 ----------------
    collector_config = collector_config or (load_collector_config(config_path) if config_path
                                            else CollectorConfig())
    demo_mode = collector is None
    if collector is None:
        collector = MockCollector(account_id=app.state.default_account_id)
    if sync_worker is None:
        sync_worker = SyncWorker(
            db, collector, pipeline=pipeline, quota=quota, dispatcher=dispatcher,
            account_id=app.state.default_account_id,
            interval_s=collector_config.poll_interval_s,
            sleep_interval_s=collector_config.sleep_interval_s,
            catchup_days=collector_config.catchup_days,
            max_failures=collector_config.max_failures,
            quota_default_limit=quota_config.default_limit_tokens,
        )
    app.state.collector = collector
    app.state.sync_worker = sync_worker
    app.state.collector_status = collector_status
    app.state.collector_config = collector_config
    app.state.demo_mode = demo_mode

    # 配置包：段名 → 配置对象。设置面板读写它，原地改字段即可让运行中的通道/引擎立即看到。
    app.state.settings_bundle = {
        "quota": quota_config, "alert": alert_config, "collector": collector_config,
        "whisper": whisper_config, "app": app_config, "backup": backup_config,
    }
    app.state.alert_config = alert_config
    app.state.quota_config = quota_config
    app.state.app_config = app_config
    app.state.whisper_config = whisper_config
    app.state.backup_config = backup_config

    _engines: dict[str, QuotaEngine] = {app.state.default_account_id: quota}

    def engine_for(account: str) -> QuotaEngine:
        """每个微信号一套限额引擎（规则/用量按 account_id 隔离）。"""
        if account not in _engines:
            _engines[account] = QuotaEngine(
                db, account, default_limit_tokens=quota_config.default_limit_tokens)
        return _engines[account]

    def account_of(value: str | None) -> str:
        return value or app.state.default_account_id

    def _run_quota(events: list[AlertEvent]) -> dict[str, Any]:
        delivered = dispatcher.dispatch(events)
        return {"alerts": [e.model_dump(mode="json") for e in events], "delivered": delivered}

    @app.post("/api/ingest/mock")
    def ingest_mock(account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        """演示数据入口：用 mock 采集器驱动链路（未接入 wechat-cli 时使用）。

        接入真实采集后可关掉（config.json → app.allow_mock_ingest = false），
        避免误触把演示数据写进真实统计。
        """
        if not app_config.allow_mock_ingest:
            raise HTTPException(
                status_code=403,
                detail="演示数据入口已关闭（config.json → app.allow_mock_ingest = false）",
            )
        inserted = Ingestor(db, account_id=account_id).run(MockCollector(account_id=account_id))
        metered = pipeline.run_until_drained()
        events = engine_for(account_id).run_pending()
        return {"inserted": inserted, "metered": metered,
                "messages": db.count_messages(), **_run_quota(events)}

    @app.post("/api/meter/run")
    def run_metering() -> dict[str, Any]:
        metered = pipeline.run_until_drained()
        return {"metered": metered, **_run_quota(quota.run_pending())}

    # ---------------- M9 备份导入器 ----------------

    async def _read_upload(request: Request, filename: str) -> tuple[list[dict] | None, str]:
        """读上传内容：文件名从 X-Filename 头取，正文用原始 body（不引入 multipart 依赖）。"""
        suffix = Path(filename).suffix or ".json"
        text = (await request.body()).decode("utf-8-sig", errors="replace")
        try:
            return load_text(text, suffix), ""
        except ValueError as exc:
            return None, str(exc)

    @app.post("/api/import/preview")
    async def import_preview(request: Request, account_id: str | None = None,
                             filename: str = Header(default="", alias="X-Filename")) -> dict:
        """试解析备份文件：报告能解析多少 / 跳过多少 / 错误哪些，不写库。"""
        rows, error = await _read_upload(request, filename)
        if error:
            return {"ok": False, "error": error}
        report = import_rows(rows, db=db, account_id=account_of(account_id),
                             source=filename, dry_run=True)
        return {"ok": report.ok(), **report.as_dict()}

    @app.post("/api/import/run")
    async def import_run(request: Request, account_id: str | None = None,
                         dry_run: bool = Query(default=False),
                         filename: str = Header(default="", alias="X-Filename")) -> dict:
        """正式导入：入库 + 计量 + 限额评估（dry_run=true 只解析不写库）。"""
        rows, error = await _read_upload(request, filename)
        if error:
            return {"ok": False, "error": error}
        account = account_of(account_id)
        report = import_rows(rows, db=db, account_id=account, source=filename,
                             dry_run=dry_run, pipeline=pipeline, quota=engine_for(account))
        if not dry_run:
            db.upsert_account(account)
        return {"ok": report.ok(), **report.as_dict(), "messages": db.count_messages()}

    # ---------------- M7 采集 / 同步 / 多账号 ----------------

    def _accounts_payload() -> list[dict[str, Any]]:
        """补充本机资料后的账号列表。"""
        items = db.list_accounts()
        for item in items:
            if item.get("wechat_id") and item.get("stable_id") and item.get("avatar_url"):
                continue
            profile = detect_profile(item["account_id"])
            if not profile:
                continue
            db.upsert_account(
                item["account_id"],
                display_name=profile.get("display_name", ""),
                wechat_id=profile.get("wechat_id", ""),
                avatar_url=profile.get("avatar_url", ""),
                stable_id=profile.get("stable_id", ""),
            )
            for key in ("display_name", "wechat_id", "avatar_url", "stable_id"):
                if profile.get(key):
                    item[key] = profile[key]
        return items

    @app.get("/api/accounts")
    def list_accounts() -> dict[str, Any]:
        """库里出现过的微信账号（多微信号隔离，大屏可切换）。"""
        return {"items": _accounts_payload(),
                "default": app.state.default_account_id}

    @app.post("/api/accounts/merge")
    def merge_accounts(req: MergeAccountsRequest) -> dict[str, Any]:
        """把同一稳定身份的历史别名合并到“最新账号”（用户确认后执行）。

        这是 C+A 方案：稳定 wxid 负责识别，用户点击确认负责授权；不会自动调用。
        """
        if not req.confirm:
            raise HTTPException(status_code=400, detail="合并账号需要 confirm=true")
        items = _accounts_payload()
        if req.account_ids:
            # 方案 A 的手动确认：允许用户选择无法由当前缓存自动识别的旧别名。
            wanted = set(req.account_ids)
            group = [item for item in items if item["account_id"] in wanted]
        else:
            group = [item for item in items
                     if req.stable_id and item.get("stable_id") == req.stable_id]
        if len(group) < 2:
            raise HTTPException(status_code=400, detail="至少选择两个需要合并的账号")
        by_id = {item["account_id"]: item for item in group}
        if req.target_account_id:
            target = by_id.get(req.target_account_id)
            if target is None:
                raise HTTPException(status_code=400, detail="目标账号不在同身份组中")
        else:
            target = max(
                group,
                key=lambda item: (
                    item.get("last_message_at") or "",
                    item.get("last_sync_at") or "",
                    item.get("updated_at") or "",
                    int(item.get("messages") or 0),
                ),
            )
        target_id = target["account_id"]
        sources = [item["account_id"] for item in group if item["account_id"] != target_id]
        result = db.merge_accounts(sources, target_id)
        # 重新按消息计算 target 的额度用量；合并后的历史事件不再重复提醒。
        engine = engine_for(target_id)
        engine.run_pending()
        sync_worker.quota = engine
        sync_worker.account_id = target_id
        if hasattr(sync_worker.collector, "account_id"):
            sync_worker.collector.account_id = target_id
        app.state.default_account_id = target_id
        collector_config.account_id = target_id
        if config_path:
            save_values(config_path, app.state.settings_bundle, CONFIG_SCHEMA_VERSION)
        return {
            "ok": True,
            "target": target_id,
            "merged": result["sources"],
            "messages_moved": result["messages"],
            "accounts": _accounts_payload(),
        }

    @app.get("/api/collector/status")
    def collector_status_endpoint() -> dict[str, Any]:
        """采集器与同步线程状态：是否已装 wechat-cli、是否在轮询、上次错误。"""
        status = app.state.collector_status
        return {
            "installed": bool(status.installed) if status else False,
            "cli": status.as_dict() if status else None,
            "mode": "demo" if app.state.demo_mode else "wechat-cli",
            "collector": type(app.state.collector).__name__,
            "account_id": sync_worker.account_id,
            "worker": sync_worker.snapshot(),
        }

    @app.post("/api/collector/install")
    def install_collector() -> dict[str, Any]:
        """手动触发 wechat-cli 安装（首次启动会自动尝试一次）。"""
        cfg = app.state.collector_config
        status = ensure_cli(
            platform_name=None, auto_install=True,
            binary=cfg.binary or None,
            cli_version=cfg.cli_version or None,
            cli_ref=cfg.cli_ref or None,
            python_executable=sys.executable,
        )
        app.state.collector_status = status
        return {"installed": status.installed, **status.as_dict()}

    @app.post("/api/sync/run")
    def sync_run() -> dict[str, Any]:
        """手动跑一次增量同步（采集 → 入库 → 计量 → 限额 → 提醒）。

        手动同步会重新从 0% 展示一整轮流水线；后台自动轮询在首轮完成后
        保持 100%，只显示“检查增量消息”，避免 15 秒一次把进度打回起点。
        """
        return sync_worker.poll_once(restart_progress=True).as_dict()

    @app.get("/api/sync/status")
    def sync_status() -> dict[str, Any]:
        return sync_worker.snapshot()

    @app.post("/api/sync/start")
    def sync_start() -> dict[str, Any]:
        sync_worker.start()
        return {"running": sync_worker.is_alive(), **sync_worker.snapshot()}

    @app.post("/api/sync/stop")
    def sync_stop() -> dict[str, Any]:
        sync_worker.stop()
        return {"running": False, **sync_worker.snapshot()}

    @app.post("/api/sync/catchup")
    def sync_catchup(days: int = 0) -> dict[str, Any]:
        """请求一次补读：回退游标重拉断档期的消息（手机端聊天）。"""
        if days > 0:
            sync_worker.catchup_days = days
        sync_worker.request_catchup()
        return sync_worker.poll_once(restart_progress=True).as_dict()

    # ---------------- M4 提醒通道 ----------------

    @app.get("/api/alerts")
    def list_alerts(account_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """告警历史（含各通道投递回执）。"""
        return {"items": db.recent_alerts(account_id=account_id, limit=limit)}

    @app.get("/api/alerts/status")
    def alert_status() -> dict[str, Any]:
        return {"channels": dispatcher.channel_status()}

    # ---------- 语音转写（M6） ----------

    @app.get("/api/transcribe/status")
    def transcribe_status() -> dict[str, Any]:
        """语音转写引擎与模型状态；未启用时给出降级说明。"""
        if transcriber is None:
            return {"enabled": False, "engine_installed": False, "model_ready": False,
                    "ready": False, "model_size": whisper_config.model_size,
                    "reason": "语音转写已在配置中关闭"}
        return transcriber.status()

    @app.post("/api/transcribe/download")
    def download_whisper_model() -> dict[str, Any]:
        """手动触发模型下载（从 hf-mirror），返回是否成功。"""
        if transcriber is None:
            return {"ok": False, "message": "语音转写未启用"}
        ok, message = transcriber.download_model()
        return {"ok": ok, "message": message, "status": transcriber.status()}

    @app.post("/api/alerts/test")
    def test_alert(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """手动触发一条测试提醒，用于验证通道是否配置正确。"""
        payload = payload or {}
        used = int(payload.get("used", 6000))
        limit = int(payload.get("limit", 5000))
        event = AlertEvent(
            account_id=payload.get("account_id", "wxid_demo"),
            peer_id=payload.get("peer_id", "wequota_test"),
            peer_name=payload.get("peer_name", "测试联系人"),
            level=AlertLevel(payload.get("level", "exceeded")),
            used=used, limit=limit, ratio=(used / limit) if limit else 0.0,
        )
        event.alert_id = db.log_alert(event.account_id, event.peer_id, event.level.value,
                                      used, limit, None)
        return {"event": event.model_dump(mode="json"), **_run_quota([event])}

    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket) -> None:
        """数据大屏长连接：连上先回放最近告警，之后实时推送。"""
        if auth_token:
            provided = (ws.query_params.get("token", "")
                        or _bearer_token(ws.headers.get("authorization")))
            if not _token_matches(provided, auth_token):
                await ws.close(code=1008)
                return
        await ws.accept()
        queue = hub.subscribe()
        try:
            for item in hub.recent():
                await ws.send_json(item)
            while True:
                item = await queue.get()
                await ws.send_json(item)
        except WebSocketDisconnect:
            hub.unsubscribe(queue)
        except Exception:
            hub.unsubscribe(queue)

    @app.get("/api/quota/rules")
    def list_rules(account_id: str | None = None) -> dict[str, Any]:
        engine = engine_for(account_of(account_id))
        return {"items": [r.model_dump(mode="json") for r in engine.list_rules()]}

    @app.put("/api/quota/rules")
    def set_rule(rule: QuotaRule) -> dict[str, Any]:
        """实时改额：保存后立即重新评估当前窗口，可能触发提醒。"""
        engine = engine_for(rule.account_id or app.state.default_account_id)
        events = engine.set_rule(rule)
        return {"rule": rule.model_dump(mode="json"), **_run_quota(events)}

    @app.delete("/api/quota/rules/{peer_id}")
    def delete_rule(peer_id: str, account_id: str | None = None) -> dict[str, Any]:
        engine_for(account_of(account_id)).delete_rule(peer_id)
        return {"deleted": peer_id}

    @app.get("/api/quota/usage")
    def quota_usage(peer_id: str | None = None,
                    account_id: str | None = None) -> dict[str, Any]:
        """指定联系人的额度使用情况；不传 peer_id 返回全部有规则的联系人。"""
        engine = engine_for(account_of(account_id))
        if peer_id:
            u = engine.usage(peer_id)
            return {"items": [u.model_dump(mode="json")]}
        peers = {r.peer_id for r in engine.list_rules()}
        return {"items": [engine.usage(p).model_dump(mode="json") for p in sorted(peers)]}

    # ---------------- M5 统计与大屏 ----------------

    def _window(days: int | None) -> tuple[datetime | None, datetime | None]:
        """按「最近 N 天」算统计区间：起点为当天的本地零点，终点为当前时刻。"""
        if not days:
            return None, None
        now = datetime.now().astimezone()
        start = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return start, now

    @app.get("/api/stats/summary")
    def stats_summary(days: int | None = 7, peer_id: str | None = None,
                      account_id: str | None = None) -> dict[str, Any]:
        """总览：token 总量、消息数、联系人数、消耗最高的联系人。"""
        account = account_of(account_id)
        start, end = _window(days)
        rows = db.iter_metered(account, start=start, end=end, peer_id=peer_id)
        data = summarize(rows, start=start, end=end)
        data["alerts"] = len(db.recent_alerts(account_id=account, limit=1000))
        return data

    @app.get("/api/stats/ranking")
    def stats_ranking(days: int | None = 7, limit: int = Query(default=10, ge=1, le=200),
                      account_id: str | None = None) -> dict[str, Any]:
        """联系人 token 消耗排行榜，并合并当前窗口的额度状态。"""
        start, end = _window(days)
        engine = engine_for(account_of(account_id))
        rows = db.iter_metered(engine.account_id, start=start, end=end)
        items = rank_peers(rows, limit=limit)
        for item in items:
            usage = engine.usage(item["peer_id"])
            item.update({
                "used": usage.used, "limit": usage.limit, "ratio": round(usage.ratio, 4),
                "remaining": usage.remaining, "window_start": usage.window_start,
            })
        return {"items": items}

    @app.get("/api/stats/timeseries")
    def stats_timeseries(
        bucket: str = "hour",
        days: int | None = 1,
        peer_id: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """时段 token 消耗（面积图）：bucket=hour 按小时，bucket=day 按天，空桶自动补 0。"""
        start, end = _window(days)
        rows = db.iter_metered(account_of(account_id), start=start, end=end, peer_id=peer_id)
        return {"bucket": bucket, "items": bucketize(rows, bucket=bucket, start=start, end=end)}

    @app.get("/api/stats/heatmap")
    def stats_heatmap(year: int | None = None,
                      account_id: str | None = None) -> dict[str, Any]:
        """当前账号的年度消息频率热力图（每个方块一天）。

        年份列表来自数据库中的真实消息时间；不传 year 时优先当前年，
        没有当前年数据则取最近有数据的一年。
        """
        account = account_of(account_id)
        today = datetime.now().astimezone().date()
        years = db.message_years(account)
        if not years:
            years = [today.year]
        if year is None or year not in years:
            year = today.year if today.year in years else years[0]

        local = datetime.now().astimezone().tzinfo
        start = datetime(year, 1, 1, tzinfo=local).astimezone(timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=local).astimezone(timezone.utc)
        counts = {
            row["date"]: row["count"]
            for row in db.daily_message_counts(account, start, end)
        }
        data = heatmap(counts, year=year, today=today)
        data["years"] = years
        return data

    @app.get("/api/stats/by-type")
    def stats_by_type(days: int | None = 7, peer_id: str | None = None,
                      account_id: str | None = None) -> dict[str, Any]:
        """各消息类型的 token 占比（饼图）。"""
        start, end = _window(days)
        rows = db.iter_metered(account_of(account_id), start=start, end=end, peer_id=peer_id)
        return {"items": by_type(rows)}

    @app.get("/api/stats/report")
    def stats_report(period: str = "week", peer_id: str | None = None,
                     account_id: str | None = None) -> dict[str, Any]:
        """周报 / 月报：每日 token 与消息数 + 区间总量 + 排行榜。"""
        days = 30 if period == "month" else 7
        start, end = _window(days)
        rows = db.iter_metered(account_of(account_id), start=start, end=end, peer_id=peer_id)
        items = daily_report(rows, end=end, days=days)
        return {
            "period": period, "items": items,
            "tokens": sum(i["tokens"] for i in items),
            "messages": sum(i["messages"] for i in items),
            "ranking": rank_peers(rows, limit=10),
        }

    @app.get("/api/stats/total")
    def total_tokens(
        account_id: str | None = None,
        peer_id: str | None = None,
    ) -> dict[str, Any]:
        """已计量 token 总量（M5 大屏统计的雏形）。"""
        return {"tokens": db.total_tokens(account_id or "wxid_demo", peer_id=peer_id)}

    @app.get("/api/messages")
    def list_messages(
        account_id: str | None = None,
        peer_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        msg_type: str | None = None,
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, Any]:
        rows = db.query_messages(
            account_id=account_id, peer_id=peer_id, start=start, end=end,
            msg_type=msg_type, limit=limit,
        )
        items = [
            {
                "id": r["id"],
                "account_id": r["account_id"],
                "peer_id": r["peer_id"],
                "peer_name": r["peer_name"],
                "direction": r["direction"],
                "msg_type": r["msg_type"],
                "timestamp": r["timestamp"].isoformat(),
                "ingested_at": r["ingested_at"].isoformat(),
                "backfill": (r["ingested_at"] - r["timestamp"]).total_seconds() > 300,
                "text": r["text"],
                "token_count": r["token_count"],
                "meter_detail": r["meter_detail"],
            }
            for r in rows
        ]
        return {"items": items, "total": len(items)}

    @app.get("/api/peers")
    def list_peers(account_id: str | None = None) -> dict[str, Any]:
        account = account_of(account_id)
        return {"items": db.list_peer_summaries(account)}

    # ---------------- M11 大屏设置 ----------------

    def _settings_bundle() -> dict[str, Any]:
        return getattr(app.state, "settings_bundle", None) or {}

    def _sync_runtime(changed: list[str]) -> None:
        """让能热更新的部分立刻生效。

        提醒类改的就是启动时那几个配置对象本身（各通道持有同一份引用），已经生效；
        这里只处理「值被复制到别处」的默认额度——限额引擎和同步线程各存了一份。
        """
        if "quota.default_limit_tokens" not in changed:
            return
        limit = int(app.state.quota_config.default_limit_tokens or 0)
        for engine in list(_engines.values()):
            engine.default_limit_tokens = limit
        worker = getattr(app.state, "sync_worker", None)
        if worker is not None:
            worker.quota_default_limit = limit
            if getattr(worker, "quota", None) is not None:
                worker.quota.default_limit_tokens = limit

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        """返回字段结构（schema）+ 当前值；秘密字段已掩码。"""
        return {
            **schema_dict(),
            "values": read_values(_settings_bundle()),
            "config_path": str(getattr(app.state, "config_path", "") or ""),
        }

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """保存设置：原地改配置对象 → 热更新 → 写回 config.json。"""
        bundle = _settings_bundle()
        values = ((payload or {}).get("values") or {})
        changed, restart, errors = apply_values(bundle, values)
        if changed:
            _sync_runtime(changed)
            path = getattr(app.state, "config_path", None) or ""
            if path:
                try:
                    save_values(path, bundle, CONFIG_SCHEMA_VERSION)
                except OSError as exc:
                    errors.append(f"写入 {path} 失败：{exc}")
        return {
            "saved": bool(changed) and not errors,
            "changed": changed,
            "restart_required": sorted({k.split(".", 1)[0] for k in restart}),
            "errors": errors,
            "values": read_values(bundle),
        }

    @app.post("/api/settings/test-email")
    def test_email(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """真发一封测试邮件验证 SMTP。

        body 里的 `values` 只用于这一次尝试（未保存的配置也能先验证），不会落盘。
        """
        payload = payload or {}
        base = app.state.alert_config
        cfg = replace(base)
        tmp_bundle = dict(_settings_bundle())
        tmp_bundle["alert"] = cfg
        _, _, errors = apply_values(tmp_bundle, payload.get("values") or {})
        if errors:
            return {"ok": False, "message": "；".join(errors)}
        # 测试时不看开关：用户可能还没保存「启用邮件提醒」就想先验证能不能发通
        cfg.email_enabled = True
        if not cfg.email_configured:
            return {"ok": False, "message": "还没填 SMTP 服务器或收件人"}
        channel = EmailChannel(cfg)
        event = AlertEvent(
            account_id=app.state.default_account_id,
            peer_id="wequota_test", peer_name="WeQuota 测试",
            level=AlertLevel("warn"), used=0, limit=1, ratio=0.0,
        )
        ok = channel.send(event)
        return {
            "ok": ok,
            "message": ("已发送，请查收收件箱（没看到就翻下垃圾邮件）" if ok
                        else f"发送失败：{channel.last_error or '未知原因'}"),
        }

    @app.get("/")
    def index() -> HTMLResponse:
        index_file = WEBAPP_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse(FRONTEND_HINT, status_code=200)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"frontend": "built" if (WEBAPP_DIR / "index.html").exists() else "missing"}

    # 静态资源（JS/CSS/图片）：前端构建后才注册，未构建时访问 / 会看到构建指引
    if (WEBAPP_DIR / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=WEBAPP_DIR / "assets"), name="assets")
        app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")

    return app


app = create_app(config_path="config.json")   # uvicorn wequota.app:app 入口（会读取仓库 config.json）
