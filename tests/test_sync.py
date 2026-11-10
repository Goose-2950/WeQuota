"""M7 同步工作线程测试：增量轮询 / 心跳休眠 / 重新登录后 catch-up 补读。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wequota.metering import Meter
from wequota.metering_pipeline import MeteringPipeline
from wequota.quota import QuotaEngine, QuotaRule
from wequota.storage import Database
from wequota.sync import SyncWorker

# 用「真实当前时间」造数据，避免窗口归属用例依赖写死的日期
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


class ScriptedCollector:
    """可控采集器：按脚本产出消息或抛异常。"""

    def __init__(self, account_id="wxid_demo", batches=None, fail_times=0, error="微信未登录"):
        self.account_id = account_id
        self.batches = list(batches or [])
        self.fail_times = fail_times
        self.error = error
        self.fetch_calls: list[datetime | None] = []

    def fetch(self, since=None, limit=500):
        self.fetch_calls.append(since)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.error)
        if not self.batches:
            return []
        return self.batches.pop(0)


def make_env(tmp_path, collector, catchup_days=3, max_failures=3):
    db = Database(tmp_path / "sync.db")
    db.init()
    meter = Meter()
    pipeline = MeteringPipeline(db, meter)
    quota = QuotaEngine(db, collector.account_id)
    worker = SyncWorker(
        db, collector, pipeline=pipeline, quota=quota, dispatcher=None,
        account_id=collector.account_id, catchup_days=catchup_days,
        max_failures=max_failures, clock=lambda: NOW.timestamp(), sleeper=lambda _s: None,
    )
    return db, pipeline, quota, worker


NOW_TS = int(NOW.timestamp())


def make_msg(account_id, peer_id, msg_id, timestamp, text="你好呀", peer_name="Bob",
             direction="recv"):
    from wequota.contracts import MessageEnvelope
    return MessageEnvelope(
        account_id=account_id, msg_id=msg_id, peer_id=peer_id, peer_name=peer_name,
        direction=direction, msg_type="text", timestamp=timestamp, text=text,
    )


def text_msg(account_id, peer_id, msg_id, ts_offset_s=0, text="你好呀", peer_name="Bob"):
    return make_msg(account_id, peer_id, msg_id, NOW + timedelta(seconds=ts_offset_s),
                    text=text, peer_name=peer_name)


def test_poll_once_ingests_meters_and_applies_quota(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    quota.set_rule(QuotaRule(account_id="wxid_demo", peer_id="wxid_bob",
                             window_type="daily", limit_tokens=1), now=NOW)

    result = worker.poll_once()
    assert result.inserted == 1
    assert result.metered == 1
    assert result.error is None
    assert worker.status == "running"
    rows = db.query_messages(account_id="wxid_demo")
    assert rows[0]["token_count"] > 0
    assert result.alerts  # 额度 10 已超限


def test_second_poll_inserts_nothing(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")],
                                      [text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    assert worker.poll_once().inserted == 1
    assert worker.poll_once().inserted == 0
    assert db.count_messages() == 1


def test_poll_updates_cursor(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker.poll_once()
    msg_id, ts = db.get_cursor("wxid_demo")
    assert msg_id == "m1"
    assert ts is not None


def test_repeated_failures_put_worker_to_sleep(tmp_path):
    coll = ScriptedCollector(fail_times=5)
    db, pipeline, quota, worker = make_env(tmp_path, coll, max_failures=3)
    for _ in range(3):
        r = worker.poll_once()
        assert r.error is not None
    assert worker.status == "sleeping"
    assert "未登录" in worker.last_error


def test_success_after_sleep_resumes_running(tmp_path):
    coll = ScriptedCollector(fail_times=3)
    db, pipeline, quota, worker = make_env(tmp_path, coll, max_failures=2)
    worker.poll_once()
    worker.poll_once()
    assert worker.status == "sleeping"
    coll.fail_times = 0          # 微信重新登录，采集恢复
    coll.batches = [[text_msg("wxid_demo", "wxid_bob", "late1")]]
    r = worker.poll_once()
    assert worker.status == "running"
    assert r.error is None


def test_recovery_triggers_catchup_rewind(tmp_path):
    """微信重新登录后：游标回退 N 天，把手机端聊天补读进来。"""
    coll = ScriptedCollector(fail_times=3)
    db, pipeline, quota, worker = make_env(tmp_path, coll, max_failures=2, catchup_days=3)
    worker.poll_once()
    worker.poll_once()
    assert worker.status == "sleeping"
    coll.fail_times = 0          # PC 微信重新登录，采集恢复
    old = make_msg("wxid_demo", "wxid_bob", "phone1", NOW - timedelta(days=2),
                   text="这是手机端聊的", direction="send")
    coll.batches = [[old]]
    result = worker.poll_once()
    assert result.catchup is True
    assert result.inserted == 1
    # 游标确实被回退过：补读时传给采集器的 since 早于 3 天前
    since_used = coll.fetch_calls[-1]
    assert since_used <= NOW - timedelta(days=3) + timedelta(seconds=1)
    assert db.get_cursor("wxid_demo")[0] == "phone1"


def test_catchup_messages_are_flagged_as_backfill(tmp_path):
    coll = ScriptedCollector(fail_times=3)
    db, pipeline, quota, worker = make_env(tmp_path, coll, max_failures=2, catchup_days=1)
    worker.poll_once()
    worker.poll_once()
    coll.fail_times = 0
    coll.batches = [[make_msg("wxid_demo", "wxid_bob", "phone2", NOW - timedelta(hours=6),
                              text="补读", direction="send")]]
    worker.poll_once()
    rows = db.query_messages(account_id="wxid_demo")
    assert rows and db.is_backfill(rows[0]["id"]) is True


def test_no_catchup_when_never_slept(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    r = worker.poll_once()
    assert r.catchup is False


def test_sleep_uses_longer_interval(tmp_path):
    coll = ScriptedCollector(fail_times=3)
    db, pipeline, quota, worker = make_env(tmp_path, coll, max_failures=1)
    assert worker.next_delay() == 15.0
    worker.poll_once()
    assert worker.status == "sleeping"
    assert worker.next_delay() == 60.0


def test_run_forever_stops_after_max_polls(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker.run_forever(max_polls=3)
    assert worker.polls == 3
    assert worker.status == "running"


def test_start_and_stop_thread(tmp_path):
    coll = ScriptedCollector()
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker.interval_s = 0.01
    worker.start()
    for _ in range(200):
        if worker.polls >= 2:
            break
        import time
        time.sleep(0.01)
    worker.stop()
    assert worker.polls >= 1
    assert not worker.is_alive()


def test_snapshot_reports_state(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker.poll_once()
    snap = worker.snapshot()
    assert snap["status"] == "running"
    assert snap["account_id"] == "wxid_demo"
    assert snap["polls"] == 1
    assert snap["last_inserted"] == 1
    assert snap["inserted_total"] == 1
    assert snap["interval_s"] == 15.0


def test_progress_only_completes_after_metering(tmp_path):
    coll = ScriptedCollector(batches=[[text_msg("wxid_demo", "wxid_bob", "m1")]])
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker.poll_once()
    progress = worker.snapshot()["progress"]
    assert progress["stage"] == "done"
    assert progress["percent"] == 100
    assert progress["completed_at"]
    assert progress["inserted"] == 1

    # 后续自动轮询保持 100%，不会把进度条重置回 0。
    worker.poll_once()
    progress = worker.snapshot()["progress"]
    assert progress["stage"] == "done"
    assert progress["percent"] == 100


def test_incremental_check_exposes_its_own_progress(tmp_path):
    coll = ScriptedCollector()
    db, pipeline, quota, worker = make_env(tmp_path, coll)
    worker._cycle_complete = True
    worker._detailed_progress = False
    worker._on_collector_progress({
        "active": True, "stage": "reading", "percent": 45,
        "current": "Alice", "processed": 3, "total": 10, "messages": 7,
    })
    progress = worker.snapshot()["progress"]
    assert progress["stage"] == "checking"
    assert progress["percent"] == 100       # 总采集进度不重置
    assert progress["check_active"] is True
    assert progress["check_percent"] == 48  # (45 - 8) / 77
    assert progress["check_current"] == "Alice"
    assert progress["check_total"] == 10
