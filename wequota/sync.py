"""增量同步工作线程（M7）：心跳轮询 + 微信未登录休眠 + 重新登录后 catch-up 补读。

行为约定：
- **心跳**：按 `interval_s`（默认 15 秒）跑一次 `Ingestor.run(collector)`，只取游标之后的增量。
- **休眠**：连续失败 `max_failures` 次（典型是微信退出/未登录）→ 状态转 `sleeping`，
  轮询间隔拉长到 `sleep_interval_s`（默认 60 秒），CPU 降到空闲水位。
- **catch-up**：休眠后首次成功 → 把游标回退 `catchup_days` 天再补读一次，
  把「手机端聊天」补进库；这些消息的 `timestamp` 是真实发生时间，
  因此会按事件时间归入原窗口（已过窗口只更新统计、不补发提醒，见 quota.py）。
- 每次成功采集后：跑计量 → 跑限额 → 需要时经 dispatcher 投递提醒。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .alerting import AlertDispatcher, dispatch_money_events
from .collector import Collector
from .ingestion import Ingestor
from .metering import Meter
from .metering_pipeline import MeteringPipeline
from .quota import AlertEvent, QuotaEngine
from .storage import Database

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_SLEEPING = "sleeping"
STATUS_ERROR = "error"


@dataclass
class PollResult:
    inserted: int = 0
    metered: int = 0
    alerts: list[AlertEvent] = field(default_factory=list)
    money_alerts: int = 0
    catchup: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted, "metered": self.metered,
            "alerts": [e.model_dump(mode="json") for e in self.alerts],
            "money_alerts": self.money_alerts,
            "catchup": self.catchup, "error": self.error,
        }


class SyncWorker:
    """把采集器拉到的增量消息推进核心服务（入库 → 计量 → 限额 → 提醒）。"""

    def __init__(
        self,
        db: Database,
        collector: Collector,
        pipeline: MeteringPipeline | None = None,
        quota: QuotaEngine | None = None,
        dispatcher: AlertDispatcher | None = None,
        account_id: str | None = None,
        interval_s: float = 15.0,
        sleep_interval_s: float = 60.0,
        catchup_days: int = 3,
        max_failures: int = 3,
        quota_default_limit: int = 0,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.collector = collector
        self.account_id = account_id or getattr(collector, "account_id", "wxid_demo")
        self.interval_s = interval_s
        self.sleep_interval_s = sleep_interval_s
        self.catchup_days = catchup_days
        self.max_failures = max_failures
        self.pipeline = pipeline or MeteringPipeline(db, Meter())
        self.quota_default_limit = int(quota_default_limit or 0)
        self.quota = quota or QuotaEngine(db, self.account_id,
                                          default_limit_tokens=self.quota_default_limit)
        self.dispatcher = dispatcher

        self.status = STATUS_IDLE
        self.polls = 0
        self.inserted_total = 0
        self.last_inserted = 0
        self.last_error: str | None = None
        self.last_success_at: datetime | None = None
        self.consecutive_failures = 0
        self.progress: dict[str, Any] = {
            "active": False, "stage": "idle", "message": "待采集",
            "current": "", "total": 0, "processed": 0,
            "messages": 0, "percent": 0, "inserted": 0,
            "started_at": "", "completed_at": "",
            "check_active": False, "check_percent": 0,
            "check_current": "", "check_processed": 0,
            "check_total": 0, "check_messages": 0,
        }
        if hasattr(collector, "set_progress_callback"):
            collector.set_progress_callback(self._on_collector_progress)

        self._clock = clock
        self._sleeper = sleeper
        self._needs_catchup = False
        self._prev_cursor: tuple[str | None, datetime | None] = (None, None)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._cycle_complete = False
        self._detailed_progress = True

    # ---------- 进度 ----------

    def _on_collector_progress(self, value: dict[str, Any]) -> None:
        with self._lock:
            if self._cycle_complete or not self._detailed_progress:
                # 首轮全量完成后的自动轮询只是增量检查，不再把总进度打回 0；
                # 单独暴露本轮增量检查的百分比，前端用第二条进度条展示。
                merged = dict(value)
                raw_percent = max(0.0, min(100.0, float(value.get("percent") or 0)))
                check_percent = min(100, round(max(0.0, raw_percent - 8) / 77 * 100))
                merged.update(
                    active=True, stage="checking", message="检查增量消息",
                    percent=100, check_active=True, check_percent=check_percent,
                    check_current=value.get("current", ""),
                    check_processed=value.get("processed", 0),
                    check_total=value.get("total", 0),
                    check_messages=value.get("messages", 0),
                )
                merged["started_at"] = self.progress.get("started_at", "")
                merged["completed_at"] = self.progress.get("completed_at", "")
                self.progress = merged
            else:
                self.progress = dict(value)

    def _set_progress(self, **values: Any) -> None:
        with self._lock:
            if not self._detailed_progress:
                # 首轮完成后的自动增量检查：保持 100% 与已有阶段，不把步骤条
                # 反复切回“读取/入库/分析”，只更新本轮计数和详情。
                values["percent"] = 100
                if values.get("stage") not in ("done", "error", "checking"):
                    values.pop("stage", None)
            self.progress.update(values)

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ---------- 单次轮询 ----------

    def poll_once(self, restart_progress: bool = False) -> PollResult:
        with self._lock:
            self.polls += 1
            result = PollResult()
            ingestor = Ingestor(self.db, self.account_id)
            if restart_progress or not self._cycle_complete:
                self._cycle_complete = False
                self._detailed_progress = True
                self._set_progress(
                    active=True, stage="preparing", message="准备采集",
                    current="", total=0, processed=0, messages=0,
                    percent=0, inserted=0,
                    started_at=self._now_iso(), completed_at="",
                    check_active=False, check_percent=0, check_current="",
                    check_processed=0, check_total=0, check_messages=0,
                )
            else:
                self._detailed_progress = False
                self._set_progress(
                    active=True, stage="checking", message="检查增量消息",
                    current="", total=0, processed=0, messages=0,
                    percent=100, inserted=0,
                    check_active=True, check_percent=0, check_current="",
                    check_processed=0, check_total=0, check_messages=0,
                )

            # 首次运行先固定资金提醒起点：本次全量回填的历史转账/红包只入库不提醒。
            # 留 60s grace，避免把刚发生、时间戳略早于启动时刻的事件误判成历史。
            ensure_money_since = getattr(self.db, "ensure_money_alert_since", None)
            if callable(ensure_money_since):
                try:
                    ensure_money_since(self.account_id, grace_seconds=60)
                except Exception:
                    pass

            try:
                result.inserted = ingestor.run(self.collector)
            except Exception as exc:                      # 采集器不可用时不能拖垮服务
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                self.status = (STATUS_SLEEPING
                               if self.consecutive_failures >= self.max_failures
                               else STATUS_ERROR)
                result.error = self.last_error
                self._set_progress(
                    active=False, stage="error", message=self.last_error,
                    percent=(min(99, int(self.progress.get("percent", 0)))
                             if self._detailed_progress else 100),
                    check_active=False,
                )
                return result

            was_sleeping = self.status == STATUS_SLEEPING
            self.consecutive_failures = 0
            self.last_error = None
            self.status = STATUS_RUNNING
            self.last_success_at = self._now()
            self.inserted_total += result.inserted
            self.last_inserted = result.inserted
            self.db.upsert_account(self.account_id)

            # 休眠恢复 → 回退游标补读（手机端聊天 / PC 重新登录同步下来的历史）
            if was_sleeping or self._needs_catchup:
                self._needs_catchup = False
                self.rewind_cursor()
                try:
                    extra = ingestor.run(self.collector)
                except Exception as exc:
                    extra = 0
                    self.last_error = f"补读失败：{exc}"
                    result.error = self.last_error
                if extra == 0:
                    self.restore_cursor()      # 断档期没有新消息，游标退回原位
                result.inserted += extra
                result.catchup = True
                self.inserted_total += result.inserted - self.last_inserted
                self.last_inserted = result.inserted

            self._set_progress(
                active=True, stage="ingesting", message="写入本地数据库", percent=86)
            self._set_progress(
                active=True, stage="metering", message="分析聊天记录与 Token", percent=91)
            result.metered = self.pipeline.run_until_drained()
            self._set_progress(
                active=True, stage="evaluating", message="额度与提醒评估", percent=97)
            events = self.quota.run_pending()
            result.alerts = list(events)
            if events and self.dispatcher is not None:
                self.dispatcher.dispatch(events)
            if self.dispatcher is not None:
                result.money_alerts = dispatch_money_events(
                    self.dispatcher, self.db, self.account_id)
            self._cycle_complete = True
            self._set_progress(
                active=False, stage="done", message="采集与分析完成", percent=100,
                current="", processed=self.progress.get("total", 0),
                inserted=result.inserted, completed_at=self._now_iso(),
                check_active=False,
            )
            return result

    # ---------- 游标 ----------

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).astimezone()

    now = _now

    def rewind_cursor(self) -> datetime:
        """把同步游标回退 N 天，让下一轮把断档期间的消息重新拉一遍（幂等入账）。

        只回退时间戳、保留 last_msg_id；若补读没拿到新消息可用 `restore_cursor` 还原。
        """
        self._prev_cursor = self.db.get_cursor(self.account_id)
        msg_id, _ = self._prev_cursor
        target = self._now() - timedelta(days=self.catchup_days)
        self.db.set_cursor(self.account_id, msg_id, target)
        return target

    def restore_cursor(self) -> None:
        msg_id, ts = self._prev_cursor
        if ts is not None:
            self.db.set_cursor(self.account_id, msg_id, ts)

    def request_catchup(self) -> None:
        """手动请求一次补读（大屏/CLI 可调用）。"""
        self._needs_catchup = True

    def next_delay(self) -> float:
        return self.sleep_interval_s if self.status == STATUS_SLEEPING else self.interval_s

    # ---------- 线程 ----------

    def run_forever(self, max_polls: int | None = None) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            self.poll_once()
            if max_polls is not None and self.polls >= max_polls:
                break
            self._sleeper(self.next_delay())

    def start(self) -> bool:
        """后台线程跑轮询；已在跑则忽略。"""
        if self.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name=f"sync-{self.account_id}", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ---------- 状态快照 ----------

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "account_id": self.account_id,
            "running": self.is_alive(),
            "logged_in": self.status == STATUS_RUNNING,
            "polls": self.polls,
            "inserted_total": self.inserted_total,
            "last_inserted": self.last_inserted,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "interval_s": self.interval_s,
            "sleep_interval_s": self.sleep_interval_s,
            "catchup_days": self.catchup_days,
            "consecutive_failures": self.consecutive_failures,
            "catchup_pending": self._needs_catchup,
            "progress": dict(self.progress),
        }


def run_sync_once(db: Database, collector: Collector, dispatcher: AlertDispatcher | None = None,
                  quota: QuotaEngine | None = None,
                  pipeline: MeteringPipeline | None = None) -> PollResult:
    """一次性同步（供 CLI/API 手动触发）。"""
    worker = SyncWorker(db, collector, dispatcher=dispatcher, quota=quota, pipeline=pipeline)
    return worker.poll_once()
