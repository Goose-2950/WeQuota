"""M9：官方备份导入器（JSON / CSV / JSONL）——无采集器也能体验全功能。

字段别名与 wechat-cli 适配器保持一致（微信生态字段名混乱），支持：
- 秒 / 毫秒 / ISO / "2026/10/01 12:00:00" 多种时间戳写法
- 数字类型码（1 文本 / 3 图片 / 34 语音 / 43 视频 / 47 表情 / 49 文件）与中英文类型名
- 脏数据逐条跳过并计入 errors，不让整批失败
- 群聊按 v1 规则丢弃（与实时采集一致）
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import MessageEnvelope

# ---- 字段名别名（左：本项目语义，右：常见上游写法） ----
ALIASES = {
    "msg_id": ("msg_id", "msgid", "msgId", "id", "local_id", "message_id"),
    "peer_id": ("peer_id", "peerid", "peerId", "talker", "talker_id", "talkerId",
                "wxid", "contact", "contact_id"),
    "peer_name": ("peer_name", "peerName", "nickname", "remark", "display_name", "contact_name"),
    "direction": ("direction", "is_send", "isSend", "isend", "send", "out"),
    "msg_type": ("msg_type", "msgType", "type", "type_name", "msg_type_name", "kind"),
    "timestamp": ("timestamp", "time", "create_time", "createTime", "created_at", "date"),
    "text": ("text", "content", "message", "msg", "body", "transcript"),
    "duration_s": ("duration_s", "duration", "duration_ms", "seconds", "length"),
    "media_path": ("media_path", "mediaPath", "path", "file", "filepath", "src"),
    "amount_yuan": ("amount_yuan", "amount", "money", "feedesc", "金额"),
    "width": ("width", "w"),
    "height": ("height", "h"),
}

# 微信数字类型码 → 本项目类型
TYPE_CODES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
              49: "file", 10000: "other"}
TYPE_NAMES = {
    "text": "text", "文本": "text", "文字": "text",
    "image": "image", "图片": "image", "img": "image",
    "voice": "voice", "语音": "voice", "audio": "voice",
    "video": "video", "视频": "video",
    "emoji": "emoji", "表情": "emoji", "sticker": "emoji",
    "file": "file", "文件": "file",
    "voice_call": "voice_call", "语音通话": "voice_call",
    "video_call": "video_call", "视频通话": "video_call",
    "transfer": "transfer", "转账": "transfer",
    "red_packet": "red_packet", "redpacket": "red_packet", "红包": "red_packet",
    "other": "other", "其他": "other",
}

TRUE_VALUES = {"1", "true", "yes", "y", "send", "out", "发送", "是"}


@dataclass
class ImportReport:
    """导入结果回执：成功 / 跳过 / 错误逐条可见。"""
    source: str = ""
    total: int = 0          # 解析出的原始行数
    parsed: int = 0         # 成功转成 Envelope 的条数
    skipped: int = 0        # 群聊等按规则丢弃
    inserted: int = 0       # 实际入库条数（dry_run 时为 0）
    metered: int = 0
    dry_run: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)

    def ok(self) -> bool:
        return self.parsed > 0

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "total": self.total, "parsed": self.parsed,
                "skipped": self.skipped, "inserted": self.inserted, "metered": self.metered,
                "dry_run": self.dry_run, "errors": self.errors[:20],
                "error_count": len(self.errors)}


def pick(row: dict[str, Any], key: str) -> Any:
    """按别名取字段值（大小写/连字符不敏感，空值与 None 视为缺失）。"""
    norm = {str(k).lower().replace("-", "_").replace(" ", "_"): v
            for k, v in row.items() if v not in (None, "")}
    for name in ALIASES[key]:
        if name.lower() in norm:
            return norm[name.lower()]
    return None


def normalize_type(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return TYPE_CODES.get(int(value))
    text = str(value).strip().lower()
    if text.isdigit():
        return TYPE_CODES.get(int(text))
    return TYPE_NAMES.get(text)


def normalize_direction(value: Any) -> str:
    if isinstance(value, bool):
        return "send" if value else "recv"
    return "send" if str(value).strip().lower() in TRUE_VALUES else "recv"


def normalize_timestamp(value: Any) -> datetime | None:
    """支持 epoch 秒 / 毫秒、ISO 字符串、常见中文日期格式。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        raw = float(value)
        if raw > 1e11:                      # 毫秒级
            raw /= 1000.0
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_envelope(row: dict[str, Any],
                account_id: str) -> tuple[MessageEnvelope | None, str | None]:
    """一行 → Envelope；返回 (envelope, 错误原因)，失败时为 (None, 原因)。"""
    msg_id, peer_id = pick(row, "msg_id"), pick(row, "peer_id")
    if not msg_id or not peer_id:
        return None, "缺少 msg_id 或 peer_id"
    msg_type = normalize_type(pick(row, "msg_type"))
    if msg_type is None:
        return None, f"无法识别的消息类型：{pick(row, 'msg_type')!r}"
    ts = normalize_timestamp(pick(row, "timestamp"))
    if ts is None:
        return None, f"无法解析的时间戳：{pick(row, 'timestamp')!r}"

    meta: dict[str, Any] = {}
    duration = pick(row, "duration_s")
    if duration is not None:
        try:
            value = float(duration)
            meta["duration_s"] = value / 1000.0 if value > 3600 else value   # 毫秒兜底
        except (TypeError, ValueError):
            pass
    width, height = pick(row, "width"), pick(row, "height")
    if width and height:
        try:
            meta["width"], meta["height"] = int(width), int(height)
        except (TypeError, ValueError):
            pass

    text, media_path = pick(row, "text"), pick(row, "media_path")
    extra = None
    if msg_type in ("transfer", "red_packet"):
        amount = pick(row, "amount_yuan")
        amount_val: float | None = None
        if amount not in (None, ""):
            try:
                amount_val = float(str(amount).replace("¥", "").replace("￥", "").strip())
            except (TypeError, ValueError):
                amount_val = None
        extra = {"kind": msg_type, "amount_yuan": amount_val}
    try:
        env = MessageEnvelope(
            account_id=account_id, msg_id=str(msg_id), peer_id=str(peer_id),
            peer_name=str(pick(row, "peer_name") or ""),
            direction=normalize_direction(pick(row, "direction")),
            msg_type=msg_type, timestamp=ts,
            text=str(text) if text is not None else None,
            media_path=str(media_path) if media_path else None,
            media_meta=meta or None,
            extra=extra,
        )
    except Exception as exc:                # pydantic 校验失败：单条跳过
        return None, f"字段校验失败：{exc}"
    return env, None


def parse_rows(rows: Iterable[dict[str, Any]], account_id: str,
               source: str = "") -> tuple[list[MessageEnvelope], ImportReport]:
    """若干行 → Envelope 列表 + 报告（群聊在此阶段丢弃）。"""
    from .ingestion import is_group_peer

    envelopes: list[MessageEnvelope] = []
    report = ImportReport(source=source)
    for idx, row in enumerate(rows):
        report.total += 1
        env, error = to_envelope(row, account_id)
        if error:
            report.errors.append({"row": idx, "reason": error})
            continue
        assert env is not None
        if is_group_peer(env.peer_id):
            report.skipped += 1
            continue
        envelopes.append(env)
        report.parsed += 1
    return envelopes, report


def load_json(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("messages", "data", "items", "records"):
            if isinstance(data.get(key), list):
                return list(data[key])
        return [data]
    if isinstance(data, list):
        return list(data)
    raise ValueError("JSON 顶层必须是数组，或含 messages/data/items/records 字段的对象")


def load_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_csv(text: str, delimiter: str = ",") -> list[dict[str, Any]]:
    rows = [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    if not rows:
        raise ValueError("CSV 无数据行（需要表头）")
    return rows


SUFFIX_LOADERS = {".json": load_json, ".jsonl": load_jsonl, ".csv": load_csv}


def load_text(text: str, suffix: str = ".json") -> list[dict[str, Any]]:
    suffix = (suffix or ".json").lower()
    if suffix == ".tsv":
        return load_csv(text, delimiter="\t")
    if suffix not in SUFFIX_LOADERS:
        raise ValueError(f"不支持的文件类型：{suffix}（仅 .json / .jsonl / .csv / .tsv）")
    return SUFFIX_LOADERS[suffix](text)


def load_file(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")     # 兼容 Excel 导出的 BOM
    return load_text(text, p.suffix)


def import_rows(rows: Iterable[dict[str, Any]], db: Any, account_id: str,
                source: str = "", dry_run: bool = False,
                pipeline: Any = None, quota: Any = None) -> ImportReport:
    """解析 → 入库 →（可选）计量 + 限额；dry_run 只解析不写库。"""
    from .ingestion import ingest_batch

    envelopes, report = parse_rows(rows, account_id, source=source)
    report.dry_run = dry_run
    if dry_run or not envelopes:
        return report
    report.inserted = ingest_batch(db, envelopes, account_id)
    if pipeline is not None:
        report.metered = pipeline.run_until_drained()
        if quota is not None:
            quota.run_pending()
    return report


def import_file(path: str | Path, db: Any, account_id: str, dry_run: bool = False,
                pipeline: Any = None, quota: Any = None) -> ImportReport:
    return import_rows(load_file(path), db=db, account_id=account_id, source=str(path),
                       dry_run=dry_run, pipeline=pipeline, quota=quota)
