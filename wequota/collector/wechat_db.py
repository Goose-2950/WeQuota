"""直读 huohuoer 解密副本，产出结构化消息（M13）。

与「解析 CLI 格式化文本」不同，这里直接读明文 SQLite，能拿到：
- 每条消息的 `local_id / local_type / create_time / real_sender_id`
- 原始 `message_content`（appmsg XML 就在里面 → 转账金额、红包祝福语）
- `WCDB_CT_message_content`（zstd 压缩标记）

用户消息表名规则（与 huohuoer 一致）：`Msg_` + `md5(username)`。
`real_sender_id` 通过同库 `Name2Id(rowid -> user_name)` 还原成 wxid。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, NamedTuple

from ..contracts import MessageEnvelope
from ..money import parse_money
from .decrypted_cache import DecryptedCache, open_readonly

try:  # 微信 4.x 的 message_content 可能是 zstd 压缩
    import zstandard as zstd
except ImportError:  # pragma: no cover - 未装则无法解析压缩内容
    zstd = None  # type: ignore

_table_re = re.compile(r"^Msg_[0-9a-f]{32}$")


class RawMessage(NamedTuple):
    local_id: int
    local_type: int
    create_time: int
    real_sender_id: int
    content: str | None       # 解压并解码后的文本/XML
    base_type: int
    sub_type: int


def split_type(local_type: int) -> tuple[int, int]:
    try:
        t = int(local_type)
    except (TypeError, ValueError):
        return 0, 0
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def table_for_username(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def decompress_content(content: Any, ct: Any) -> str | None:
    """按 WCDB_CT 标记解压：4 = zstd，其它按 utf-8 解码。"""
    if content is None:
        return None
    if ct == 4 and isinstance(content, (bytes, bytearray)):
        if zstd is None:
            return None
        try:
            return zstd.ZstdDecompressor().decompress(bytes(content)).decode("utf-8", "replace")
        except Exception:
            return None
    if isinstance(content, (bytes, bytearray)):
        try:
            return bytes(content).decode("utf-8", "replace")
        except Exception:
            return None
    return str(content)


def load_name2id(conn: sqlite3.Connection) -> dict[int, str]:
    mapping: dict[int, str] = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return mapping
    for rowid, user_name in rows:
        if user_name:
            mapping[int(rowid)] = str(user_name)
    return mapping


_NAME2ID_CACHE: dict[tuple[str, int, int], dict[int, str]] = {}
_COLUMNS_CACHE: dict[tuple[str, int, int, str], set[str]] = {}


def _file_stamp(path: str | os.PathLike[str]) -> tuple[int, int]:
    try:
        stat = os.stat(path)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return 0, 0


def _trim_cache(cache: dict) -> None:
    if len(cache) > 64:
        cache.clear()


def open_readonly_retry(path: str | os.PathLike[str], attempts: int = 2):
    """微信登录同步时解密副本可能正被上游替换，短暂重试一次。"""
    last: Exception | None = None
    for index in range(attempts):
        try:
            return open_readonly(path)
        except sqlite3.OperationalError as exc:
            last = exc
            if index + 1 < attempts:
                time.sleep(0.15)
    raise last if last else sqlite3.OperationalError("open failed")


def load_name2id_cached(path: str | os.PathLike[str],
                        conn: sqlite3.Connection) -> dict[int, str]:
    """按解密副本的 mtime/size 缓存 Name2Id，避免每个联系人重复读整张表。"""
    key = (str(path), *_file_stamp(path))
    cached = _NAME2ID_CACHE.get(key)
    if cached is not None:
        return cached
    mapping = load_name2id(conn)
    _trim_cache(_NAME2ID_CACHE)
    _NAME2ID_CACHE[key] = mapping
    return mapping


def table_columns_cached(path: str | os.PathLike[str], conn: sqlite3.Connection,
                         table: str) -> set[str]:
    key = (str(path), *_file_stamp(path), table)
    cached = _COLUMNS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
    except sqlite3.Error:
        columns = set()
    _trim_cache(_COLUMNS_CACHE)
    _COLUMNS_CACHE[key] = columns
    return columns


def list_message_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if _table_re.match(r[0])]


def read_contacts(cache: DecryptedCache) -> list[dict[str, str]]:
    """从解密后的 contact.db 读联系人（含群/公众号，调用方自行过滤）。"""
    path = cache.contact_db_path()
    if not path:
        return []
    conn = open_readonly_retry(path)
    try:
        rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[dict[str, str]] = []
    for r in rows:
        username = r["username"]
        if not username:
            continue
        name = r["remark"] or r["nick_name"] or username
        out.append({"username": username, "name": name})
    return out


def is_private_username(username: str) -> bool:
    if not username:
        return False
    low = username.lower()
    if "@chatroom" in low or "@openim" in low:
        return False
    if low.startswith("gh_"):           # 公众号
        return False
    if low.startswith("filehelper") or low.endswith("@placeholder_foldgroup"):
        return False
    return True


def message_table_index(cache: DecryptedCache) -> dict[str, str]:
    """扫描所有已解密的 message DB，返回 `Msg_<md5> -> db_path` 索引。

    每轮采集只扫一遍，避免为每个联系人重复打开全部 message DB。
    """
    index: dict[str, str] = {}
    for path in cache.message_db_paths():
        try:
            conn = open_readonly_retry(path)
        except sqlite3.Error:
            continue
        try:
            for name in list_message_tables(conn):
                index.setdefault(name, str(path))
        finally:
            conn.close()
    return index


def find_peer_table(cache: DecryptedCache, username: str,
                    index: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """在所有已解密的 message DB 里找到该联系人的 `Msg_<md5>` 表。"""
    table = table_for_username(username)
    if index is not None:
        path = index.get(table)
        return (path, table) if path else (None, None)
    for path in cache.message_db_paths():
        try:
            conn = open_readonly_retry(path)
        except sqlite3.Error:
            continue
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        except sqlite3.Error:
            exists = None
        finally:
            conn.close()
        if exists:
            return str(path), table
    return None, None


def _read_rows(conn: sqlite3.Connection, table: str, since_create_time: int,
               limit: int | None, since_local_id: int = 0,
               columns: set[str] | None = None) -> list[RawMessage]:
    # 旧库可能没有 WCDB_CT_message_content 列；缺列时按未压缩处理。
    if columns is None:
        try:
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
        except sqlite3.Error:
            return []
    ct_column = ("WCDB_CT_message_content" if "WCDB_CT_message_content" in columns
                 else "NULL AS WCDB_CT_message_content")
    try:
        sql = (f"SELECT local_id, local_type, create_time, real_sender_id, "
               f"message_content, {ct_column} FROM [{table}] ")
        params: list[Any] = []
        if since_local_id > 0:
            # 同一秒内可能有多条消息：用 (create_time, local_id) 游标精确续读，
            # 避免每轮把最后一秒的整组消息重复读出来。
            sql += "WHERE create_time > ? OR (create_time = ? AND local_id > ?) "
            params.extend([int(since_create_time), int(since_create_time),
                           int(since_local_id)])
        else:
            # 旧游标只有时间：保留 >= 语义，保证边界消息不丢，幂等去重。
            sql += "WHERE create_time >= ? "
            params.append(int(since_create_time))
        sql += "ORDER BY create_time ASC, local_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = None
        for attempt in range(2):
            try:
                rows = conn.execute(sql, params).fetchall()
                break
            except sqlite3.OperationalError:
                if attempt:
                    return []
                time.sleep(0.1)
            except sqlite3.Error:
                return []
        if rows is None:
            return []
    except sqlite3.Error:
        return []
    out: list[RawMessage] = []
    for r in rows:
        local_type = int(r["local_type"] or 0)
        base, sub = split_type(local_type)
        out.append(RawMessage(
            local_id=int(r["local_id"] or 0),
            local_type=local_type,
            create_time=int(r["create_time"] or 0),
            real_sender_id=int(r["real_sender_id"] or 0),
            content=decompress_content(r["message_content"], r["WCDB_CT_message_content"]),
            base_type=base,
            sub_type=sub,
        ))
    return out


# ---- 内容解析（时长 / 计费 / 文本） ----

_VOICE_LEN_RE = re.compile(r'voicelength="(\d+)"')
_VIDEO_LEN_RE = re.compile(r'(?:playlength|length)="(\d+)"')
_VOIP_DURATION_RE = re.compile(r"Duration:\s*([0-9]+(?::[0-9]+){0,2})", re.IGNORECASE)
_VIDEO_CALL_RE = re.compile(r"(?i)(?:type=[\"']?video|<video|videocall|video_call)")


def voice_duration_s(content: str | None) -> float | None:
    if not content:
        return None
    m = _VOICE_LEN_RE.search(content)
    if not m:
        return None
    try:
        ms = int(m.group(1))
    except ValueError:
        return None
    return ms / 1000.0 if ms > 0 else None


def video_duration_s(content: str | None) -> float | None:
    if not content:
        return None
    m = _VIDEO_LEN_RE.search(content)
    if not m:
        return None
    try:
        seconds = int(m.group(1))
    except ValueError:
        return None
    return float(seconds) if seconds > 0 else None


def call_duration_s(content: str | None) -> float | None:
    """解析通话 XML 里的 `<msg>Duration:…</msg>`，兼容秒数、mm:ss、hh:mm:ss。"""
    if not content:
        return None
    match = _VOIP_DURATION_RE.search(content)
    if not match:
        return None
    parts = match.group(1).split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        seconds = numbers[0] * 60 + numbers[1]
    else:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return float(seconds) if seconds > 0 else None


def is_video_call(content: str | None) -> bool:
    """通话类型 50 无法稳定区分语音/视频；XML 含 video 标记时按视频通话处理。"""
    return bool(content and _VIDEO_CALL_RE.search(content))


def plain_text(content: str | None) -> str | None:
    if content is None:
        return None
    if content.lstrip().startswith("<"):
        return None                     # XML 内容不当作纯文本
    return content


def _media_meta(raw: RawMessage) -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    if raw.base_type == 34:
        duration = voice_duration_s(raw.content)
        if duration:
            meta["duration_s"] = duration
    elif raw.base_type == 43:
        duration = video_duration_s(raw.content)
        if duration:
            meta["duration_s"] = duration
    elif raw.base_type == 50:
        duration = call_duration_s(raw.content)
        if duration:
            meta["duration_s"] = duration
    return meta or None


def row_to_envelope(raw: RawMessage, peer_id: str, peer_name: str,
                    account_id: str, names: dict[int, str] | None = None) -> MessageEnvelope | None:
    """把原始行转成 Envelope；无法识别或字段非法返回 None。"""
    msg_id = f"{peer_id}:{raw.local_id}"
    names = names or {}
    sender = names.get(raw.real_sender_id, "")
    direction = "recv" if (not sender or sender == peer_id) else "send"

    base = raw.base_type
    money = parse_money(raw.content, 49) if base == 49 else None
    if base == 1:
        msg_type = "text"
    elif base == 3:
        msg_type = "image"
    elif base == 34:
        msg_type = "voice"
    elif base == 43:
        msg_type = "video"
    elif base == 47:
        msg_type = "emoji"
    elif base == 49:
        msg_type = money["kind"] if money else "file"
    elif base == 50:
        msg_type = "video_call" if is_video_call(raw.content) else "voice_call"
    else:
        msg_type = "other"

    extra = money

    if msg_type in ("text", "voice"):
        text = plain_text(raw.content)
    else:
        text = None

    media_meta = _media_meta(raw)
    if msg_type in ("voice_call", "video_call") and not (media_meta or {}).get("duration_s"):
        # 通话缺时长无法计量，降级为 other（只记事件）
        msg_type = "other"

    try:
        return MessageEnvelope(
            account_id=account_id or "wxid_unknown",
            msg_id=msg_id,
            peer_id=peer_id,
            peer_name=peer_name or "",
            direction=direction,
            msg_type=msg_type,          # type: ignore[arg-type]
            timestamp=datetime.fromtimestamp(raw.create_time, tz=timezone.utc),
            text=text,
            media_meta=media_meta,
            extra=extra,
        )
    except Exception:
        return None


def iter_peer_envelopes(cache: DecryptedCache, username: str, peer_name: str,
                        account_id: str, since_create_time: int = 0,
                        limit: int | None = None, *,
                        since_local_id: int = 0,
                        index: dict[str, str] | None = None) -> list[MessageEnvelope]:
    """读取某联系人的增量消息；传入 `index` 可复用每轮扫描结果。

    `since_local_id` 与 `since_create_time` 组成精确游标，处理同一秒内多条消息。
    """
    db_path, table = find_peer_table(cache, username, index=index)
    if not db_path or not table:
        return []
    conn = open_readonly_retry(db_path)
    try:
        names = load_name2id_cached(db_path, conn)
        columns = table_columns_cached(db_path, conn, table)
        raw_rows = _read_rows(conn, table, since_create_time, limit,
                              since_local_id=since_local_id, columns=columns)
    finally:
        conn.close()
    out: list[MessageEnvelope] = []
    for raw in raw_rows:
        env = row_to_envelope(raw, username, peer_name, account_id, names)
        if env is not None:
            out.append(env)
    return out


def peer_money_events(cache: DecryptedCache, username: str, *,
                      index: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """扫描某联系人全部 type=49 消息，返回 `msg_id -> 资金事件`（仅转账/红包）。

    专供旧库迁移：只读 type=49 行并解析 XML，不构造完整 Envelope，也不加载
    语音/文本等无关消息。`local_type` 可能带高位子类型，按低 32 位匹配。
    """
    db_path, table = find_peer_table(cache, username, index=index)
    if not db_path or not table:
        return {}
    conn = open_readonly(db_path)
    try:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
        ct_column = ("WCDB_CT_message_content" if "WCDB_CT_message_content" in columns
                     else "NULL AS WCDB_CT_message_content")
        rows = conn.execute(
            f"SELECT local_id, message_content, {ct_column} FROM [{table}] "
            f"WHERE (local_type & 4294967295) = 49"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        content = decompress_content(r["message_content"], r["WCDB_CT_message_content"])
        money = parse_money(content, 49)
        if money is not None:
            out[f"{username}:{int(r['local_id'] or 0)}"] = money
    return out
