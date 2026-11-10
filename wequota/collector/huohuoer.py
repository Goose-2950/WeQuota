"""huohuoer/wechat-cli 采集器（M13）：外部工具负责解密，WeQuota 直读解密副本。

分工：
- 外部 `wechat-cli`：提取密钥（`init`）、按需把整库解密成明文副本；
- 本模块：每轮触发一次解密刷新 + 枚举私聊联系人，然后交给 `wechat_db`
  直读明文库取结构化数据。

上游的解密副本只在命令调用时按源库 mtime 重新生成，因此**每轮都必须触发一次**
`contacts`（刷新 contact.db）和 `history`（刷新所有变更的 message DB），否则增量
采集会一直读到首次解密后的旧副本。

主仓库**不含任何密钥提取 / 解密代码**；上游不可用时只影响这一条采集链路。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .decrypted_cache import DecryptedCache, find_cache
from .wechat_cli import (
    CLI_NAME,
    CommandResult,
    Runner,
    WechatCliError,
    run_command,
)
from .wechat_db import (
    is_private_username,
    iter_peer_envelopes,
    message_table_index,
    peer_money_events,
    read_contacts,
    table_for_username,
)
from ..contracts import MONEY_TYPES, MessageEnvelope


class HuohuoerCli:
    """外部 wechat-cli 的最小封装（触发解密 / 初始化）。"""

    def __init__(self, binary: str = CLI_NAME, runner: Runner | None = None,
                 timeout: float = 60.0) -> None:
        self.binary = binary
        self._runner: Runner = runner or run_command
        self.timeout = timeout

    def _run(self, args: list[str]) -> CommandResult:
        result = self._runner(args)
        return result if isinstance(result, CommandResult) else CommandResult(*result)

    def keys_ready(self) -> bool:
        return (Path.home() / ".wechat-cli" / "all_keys.json").exists()

    def init(self) -> None:
        rc, out, err = self._run([self.binary, "init"])
        if rc != 0 and not self.keys_ready():
            detail = (err or out or f"{self.binary} init 退出码 {rc}").strip()
            raise WechatCliError(detail + self._init_hint(detail))

    @staticmethod
    def _init_hint(detail: str) -> str:
        """按失败原因给平台化提示，避免 Windows 用户被 sudo 文案误导。"""
        if "未能从任何微信进程中提取到密钥" in detail:
            return ("（已能读取微信进程内存仍找不到密钥：常见原因是微信版本高于当前 "
                    "wechat-cli 的密钥扫描范围。本项目使用的 0.3.0（SarielChin fork）"
                    "已支持 Windows 微信 4.1+；若仍失败，可先用管理员终端重试，"
                    "或等上游更新。）")
        if sys.platform == "win32":
            return "（init 需要在管理员终端运行，用于提取微信数据库密钥）"
        return "（init 用于提取微信数据库密钥，macOS/Linux 可能需要 sudo）"

    def bootstrap(self) -> None:
        """确保密钥就绪，并每轮触发 contact.db / session.db 重新解密。"""
        if not self.keys_ready():
            self.init()
        rc, out, err = self._run([self.binary, "contacts", "--format", "json"])
        if rc != 0:
            raise WechatCliError(
                (err or out or f"{self.binary} contacts 退出码 {rc}").strip()
            )

    def trigger_peer(self, username: str) -> bool:
        """触发一次 history；上游会顺带检查并重解密所有变更的 message DB。"""
        rc, out, err = self._run(
            [self.binary, "history", username, "--limit", "1", "--format", "json"]
        )
        return rc == 0

    def recent_private_usernames(self, limit: int = 50) -> list[str]:
        """最近的私聊 username；用于优先刷新/采集活跃联系人。

        真实账号可能有上万个联系人，contact 表前几个常常是没有消息的系统联系人；
        先拿 sessions 里最近的私聊，能保证 `history` 至少有一个可成功的刷新目标。
        """
        rc, out, _err = self._run(
            [self.binary, "sessions", "--limit", str(limit), "--format", "json"]
        )
        if rc != 0:
            return []
        try:
            data = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        names: list[str] = []
        for item in data:
            if not isinstance(item, dict) or item.get("is_group"):
                continue
            username = str(item.get("username") or "")
            if username and not username.lower().startswith("gh_"):
                names.append(username)
        return list(dict.fromkeys(names))


class HuohuoerCollector:
    """直读解密副本的采集器，实现 `Collector` 协议。"""

    def __init__(
        self,
        account_id: str,
        *,
        db: Any | None = None,
        binary: str = CLI_NAME,
        runner: Runner | None = None,
        cache: DecryptedCache | None = None,
        full_backfill: bool = True,
        include_groups: bool = False,
        timeout: float = 60.0,
        limit_per_peer: int | None = None,
        full_backfill_limit: int | None = 5000,
        max_messages_per_poll: int | None = 0,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self.db = db
        self.binary = binary
        self.runner = runner
        self.cache = cache or find_cache()
        self.full_backfill = full_backfill
        self.include_groups = include_groups
        self.timeout = timeout
        self.limit_per_peer = limit_per_peer
        self.full_backfill_limit = full_backfill_limit
        self.max_messages_per_poll = max_messages_per_poll
        self._progress_callback = progress_callback
        self.progress: dict[str, Any] = {
            "active": False, "stage": "idle", "message": "待采集",
            "current": "", "total": 0, "processed": 0,
            "messages": 0, "percent": 0,
        }
        self.last_error: str | None = None
        self.skipped = 0
        self.runs = 0
        self._last_peer: str | None = None
        self._fetch_cursor_peer: str | None = None
        self._started_ts = int(datetime.now(timezone.utc).timestamp())

    def set_progress_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._progress_callback = callback

    def _report_progress(self, **values: Any) -> None:
        self.progress.update(values)
        if self._progress_callback is not None:
            try:
                self._progress_callback(dict(self.progress))
            except Exception:
                # 进度回调不能影响采集主链路
                pass

    def _peer_progress(self, index: int, total: int, messages: int,
                       current: str) -> None:
        # 读取阶段只占整条流水线的一段：后面还有入库、Token 计量和额度评估。
        # 这样进度条不会在“联系人读完”时就提前到 100%。
        percent = 8 + int(index / total * 74) if total else 8
        self._report_progress(
            active=True, stage="reading", message="读取聊天记录",
            current=current, total=total, processed=index,
            messages=messages, percent=min(percent, 82),
        )

    def _cli(self) -> HuohuoerCli:
        return HuohuoerCli(self.binary, self.runner, self.timeout)

    def _start_for_peer(self, peer_id: str, since: datetime | None) -> tuple[int, int]:
        """返回 (create_time, local_id) 游标。

        同一秒内多条消息必须用 local_id 续读；旧库 local_id=0 时沿用
        create_time >=，靠 msg_id 幂等去重。
        """
        since_ts = int(since.timestamp()) if since is not None else None
        if self.db is not None:
            cursor, cursor_local = self.db.get_peer_cursor_state(self.account_id, peer_id)
            if cursor > 0:
                # catch-up：重新登录后全局游标会回退，per-peer 游标必须跟着回退。
                if since_ts is not None and since_ts < cursor:
                    return since_ts, 0
                return cursor, cursor_local
            if self.full_backfill:
                return 0, 0
            return (since_ts if since_ts is not None else self._started_ts), 0
        return (since_ts if since_ts is not None else 0), 0

    def _backfill_key(self, peer_id: str) -> str:
        return f"backfill_done:{self.account_id}:{peer_id}"

    def _sync_contacts(self, peers: list[dict[str, str]]) -> None:
        """把通讯录同步到本地库，供大屏显示没有消息的联系人。

        联系人变化不频繁，10 分钟同步一次即可，避免每轮 15 秒都批量写
        几万行拖慢采集。
        """
        if self.db is None or not peers:
            return
        key = f"contacts_synced:{self.account_id}"
        try:
            last = float(self.db.get_meta(key) or 0)
        except (TypeError, ValueError):
            last = 0
        now = time.time()
        if last and now - last < 600:
            return
        self.db.upsert_contacts(self.account_id, peers)
        self.db.set_meta(key, str(int(now)))

    def _planned_limit(self, peer_id: str) -> tuple[int | None, bool]:
        """返回 (本轮 limit, 是否仍处于全量回填)。

        回填未追平前用 `full_backfill_limit`（0 = 一次性拉完）；追平后写 meta
        标记，后续轮次转回 `limit_per_poll`，重启也不会退回慢速回填。
        """
        if not self.full_backfill:
            return self.limit_per_peer, False
        if self.db is not None and self.db.get_meta(self._backfill_key(peer_id)):
            return self.limit_per_peer, False
        return (self.full_backfill_limit or None), True

    def _mark_backfill_done(self, peer_id: str) -> None:
        if self.db is not None:
            self.db.set_meta(self._backfill_key(peer_id), "1")

    def _refresh_messages(self, cli: HuohuoerCli, usernames: list[str]) -> None:
        """任选一个联系人跑 history，触发上游重解密所有变更的 message DB。"""
        if not usernames:
            return
        ordered = list(dict.fromkeys(usernames))
        if self._last_peer in ordered:
            ordered.remove(self._last_peer)
            ordered.insert(0, self._last_peer)
        for username in ordered[:20]:
            if cli.trigger_peer(username):
                self._last_peer = username
                return
        raise WechatCliError("wechat-cli history 刷新失败，无法获取最新消息")

    def fetch(self, since: datetime | None = None,
              limit: int | None = None) -> list[MessageEnvelope]:
        """拉取增量；默认扫描本轮所有联系人，进度按联系人总数推进。

        `limit=None` 时只有显式配置的 `max_messages_per_poll > 0` 才限制全局条数；
        默认 0 表示不截断，保证进度和消息总数覆盖所有联系人。
        传 `limit=0` 也表示本轮不限；显式正整数优先于配置。
        """
        self.runs += 1
        self._report_progress(
            active=True, stage="preparing", message="准备解密副本",
            current="", total=0, processed=0, messages=0, percent=0,
        )
        cli = self._cli()
        try:
            cli.bootstrap()
        except WechatCliError as exc:
            self.last_error = str(exc)
            self._report_progress(
                active=False, stage="error", message=str(exc), percent=0)
            raise

        self._report_progress(
            active=True, stage="refreshing", message="读取联系人",
            current="", total=0, processed=0, messages=0, percent=5,
        )
        contacts = read_contacts(self.cache)
        recent = cli.recent_private_usernames(50)
        recent_set = set(recent)
        peers = [
            c for c in contacts
            if self.include_groups or is_private_username(c["username"])
        ]
        # 活跃会话优先：真实账号有上万个联系人，全量回填时旧联系人不应占满
        # 全局 limit，否则新消息要等很多轮才轮得到。
        peers.sort(key=lambda c: c["username"] not in recent_set)
        # 全局 limit 很小（默认 500）时，如果每轮都从同一个 peer 开始，前几个
        # 消息多的联系人会长期占满预算。按上一轮结束位置轮转，保证所有 peer
        # 都能分批推进；重启后回到“活跃会话优先”。
        if self._fetch_cursor_peer:
            for idx, contact in enumerate(peers):
                if contact["username"] == self._fetch_cursor_peer:
                    peers = peers[idx + 1:] + peers[:idx + 1]
                    break
        self._sync_contacts(peers)
        self._report_progress(
            active=True, stage="reading", message="刷新消息库",
            current="", total=len(peers), processed=0, messages=0, percent=8,
        )
        try:
            self._refresh_messages(cli, recent + [c["username"] for c in peers])
        except WechatCliError as exc:
            self.last_error = str(exc)
            self._report_progress(
                active=False, stage="error", message=str(exc), percent=0)
            raise

        # history 刷新后可能新增 message DB / 表，索引必须在刷新之后建立。
        index = message_table_index(self.cache)
        collected: list[MessageEnvelope] = []
        # 默认不设全局上限：一轮会扫描所有联系人，进度才能从 0 到 100。
        # 显式 fetch(limit>0) 或 max_messages_per_poll>0 时才截断（测试/特殊调用）。
        if limit is not None:
            budget = limit if limit > 0 else None
        else:
            configured = self.max_messages_per_poll
            budget = configured if isinstance(configured, int) and configured > 0 else None
        total_peers = len(peers)
        processed_peers = 0
        for peer_index, contact in enumerate(peers, start=1):
            if budget is not None and len(collected) >= budget:
                break
            username = contact["username"]
            name = contact["name"]
            self._fetch_cursor_peer = username
            self._peer_progress(peer_index - 1, total_peers, len(collected), name)
            if table_for_username(username) not in index:
                self._peer_progress(peer_index, total_peers, len(collected), name)
                processed_peers = peer_index
                continue
            start, start_local = self._start_for_peer(username, since)
            peer_limit, backfilling = self._planned_limit(username)
            try:
                messages = iter_peer_envelopes(
                    self.cache, username, name, self.account_id,
                    since_create_time=start, limit=peer_limit,
                    since_local_id=start_local, index=index,
                )
            except Exception as exc:                       # 单联系人失败不拖垮整批
                self.last_error = f"{username}: {exc}"
                self._peer_progress(peer_index, total_peers, len(collected), name)
                processed_peers = peer_index
                continue
            if not messages:
                if backfilling:
                    self._mark_backfill_done(username)
                self._peer_progress(peer_index, total_peers, len(collected), name)
                processed_peers = peer_index
                continue

            take = messages
            if budget is not None:
                remaining = budget - len(collected)
                if remaining <= 0:
                    break
                take = messages[:remaining]
            collected.extend(take)
            self._peer_progress(peer_index, total_peers, len(collected), name)
            processed_peers = peer_index

            fully_taken = len(take) == len(messages)
            if (backfilling and fully_taken
                    and (peer_limit is None or len(messages) < peer_limit)):
                self._mark_backfill_done(username)
            if not fully_taken:
                # 被全局 limit 截断：本轮不能给该联系人打“回填完成”标记。
                break

        collected.sort(key=lambda m: m.timestamp)
        self.last_error = None
        # 读取结束只是流水线的前半段；入库、Token 分析和额度评估由 SyncWorker
        # 继续推进 85% → 100%，避免“联系人刚读完”就提前显示完成。
        self._report_progress(
            active=True, stage="collected", message="聊天记录读取完成", current="",
            total=total_peers, processed=processed_peers,
            messages=len(collected), percent=85,
        )
        return collected

    @staticmethod
    def _message_local_id(msg_id: str) -> int:
        try:
            return int(str(msg_id).rsplit(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            return 0

    def ack(self, messages: list[MessageEnvelope]) -> None:
        """入库成功后再推进 per-peer 游标，避免入库失败导致消息丢失。

        游标包含 (create_time, local_id)，同一秒内的多条消息不会在下一轮
        被整组重复读取。
        """
        if self.db is None or not messages:
            return
        newest: dict[str, tuple[int, int]] = {}
        for message in messages:
            ts = int(message.timestamp.timestamp())
            local_id = self._message_local_id(message.msg_id)
            current = newest.get(message.peer_id, (0, 0))
            if (ts, local_id) > current:
                newest[message.peer_id] = (ts, local_id)
        for peer_id, (ts, local_id) in newest.items():
            current_ts, current_local = self.db.get_peer_cursor_state(
                self.account_id, peer_id)
            if (ts, local_id) > (current_ts, current_local):
                self.db.set_peer_cursor_state(
                    self.account_id, peer_id, ts, local_id)
    def migrate_money(self) -> int:
        """一次性回填旧库：把已入库的 type=49 普通 file 重分类为转账/红包。

        只扫描 DB 里仍是 `file` 的联系人；刷新解密副本后按 type=49 精准读取，
        不加载无关消息。历史事件按资金提醒起点静默，幂等：没有旧行时返回 0。
        """
        if self.db is None:
            return 0
        peers = self.db.unclassified_money_peers(self.account_id)
        if not peers:
            return 0
        cli = self._cli()
        try:
            cli.bootstrap()
            self._refresh_messages(cli, [peer_id for peer_id, _ in peers])
        except WechatCliError as exc:
            self.last_error = str(exc)
            raise

        index = message_table_index(self.cache)
        events: list[tuple[str, str, dict[str, Any]]] = []
        for peer_id, _name in peers:
            if table_for_username(peer_id) not in index:
                continue
            try:
                found = peer_money_events(self.cache, peer_id, index=index)
            except Exception as exc:                       # 单联系人失败不拖垮迁移
                self.last_error = f"{peer_id}: {exc}"
                continue
            for msg_id, extra in found.items():
                kind = str(extra.get("kind") or "")
                if kind in MONEY_TYPES:
                    events.append((msg_id, kind, extra))

        since = self.db.ensure_money_alert_since(self.account_id, grace_seconds=60)
        updated = self.db.classify_money_messages(self.account_id, events)
        if updated:
            self.db.mark_money_alerted_before(self.account_id, since)
        self.last_error = None
        return updated


def build_default_collector(account_id: str, db: Any, config: Any,
                            binary: str | None = None) -> HuohuoerCollector:
    """按 CollectorConfig 组装默认采集器（供 cli/sync 复用）。"""
    cache = DecryptedCache(getattr(config, "cache_dir", "") or None)
    return HuohuoerCollector(
        account_id,
        db=db,
        binary=binary or getattr(config, "binary", "") or CLI_NAME,
        cache=cache,
        full_backfill=getattr(config, "full_backfill", True),
        include_groups=getattr(config, "include_groups", False),
        limit_per_peer=getattr(config, "limit_per_poll", 500) or None,
        full_backfill_limit=getattr(config, "full_backfill_limit", 5000),
        max_messages_per_poll=0,
    )
