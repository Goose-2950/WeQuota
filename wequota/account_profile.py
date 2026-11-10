"""本机微信账号资料探测。

微信 4.x 的数据目录通常形如 ``xwechat_files/<wxid>_<suffix>/db_storage``，
其中目录名里的 wxid 是稳定身份；用户可见的微信号（alias）和昵称可能变化。
本模块只读 wechat-cli 已经解密到本机的 contact.db，不读取/解密源数据库。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_CONFIG = Path.home() / ".wechat-cli" / "config.json"
_STABLE_SUFFIX = re.compile(r"^(.+)_([0-9a-fA-F]{4,})$")


def _cli_config() -> dict[str, Any]:
    try:
        value = json.loads(_CONFIG.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def stable_account_id(db_dir: str | Path | None) -> str:
    """从 db_storage 的父目录名推导稳定 wxid（去掉 WeChat 随机后缀）。"""
    if not db_dir:
        return ""
    path = Path(db_dir)
    parent = path.parent.name if path.name.lower() == "db_storage" else path.name
    match = _STABLE_SUFFIX.match(parent)
    return match.group(1) if match else parent


def _contact_db(decrypted_dir: str | Path | None) -> Path | None:
    if not decrypted_dir:
        return None
    base = Path(decrypted_dir)
    for candidate in (
        base / "contact" / "contact.db",
        base / "contact.db",
    ):
        if candidate.is_file():
            return candidate
    return None


def detect_profile(account_id: str) -> dict[str, str]:
    """尽力读取本机账号资料；读不到时返回空字段，由 UI 使用账号 ID 兜底。"""
    cfg = _cli_config()
    db_dir = cfg.get("db_dir") or cfg.get("wechat_db_dir")
    decrypted_dir = cfg.get("decrypted_dir")
    stable = stable_account_id(db_dir)
    account_dir = Path(db_dir).parent.name if db_dir else ""
    matches_local = bool(stable and account_id in {stable, account_dir})
    fallback = {
        "account_id": account_id,
        "stable_id": stable if matches_local else "",
        "wechat_id": account_id,
        "display_name": "",
        "avatar_url": "",
    }
    contact_db = _contact_db(decrypted_dir)
    if contact_db is None:
        return fallback

    # 先按用户填入的账号/可见微信号找；只有确认是本机账号时才回退到稳定 wxid，
    # 避免把 demo 或其它账号误认成当前真实微信。
    candidates = [account_id, account_dir] if matches_local else [account_id]
    candidates = [value for value in dict.fromkeys(candidates) if value]
    try:
        conn = sqlite3.connect(f"file:{contact_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = None
            for candidate in candidates:
                row = conn.execute(
                    "SELECT username, nick_name, remark, alias, small_head_url, "
                    "big_head_url FROM contact WHERE username = ? LIMIT 1",
                    (candidate,),
                ).fetchone()
                if row:
                    break
            if row is None and account_id:
                # 用户填的可能就是可见微信号 alias。
                row = conn.execute(
                    "SELECT username, nick_name, remark, alias, small_head_url, "
                    "big_head_url FROM contact WHERE alias = ? LIMIT 1",
                    (account_id,),
                ).fetchone()
            if row is None and matches_local and stable:
                row = conn.execute(
                    "SELECT username, nick_name, remark, alias, small_head_url, "
                    "big_head_url FROM contact WHERE username = ? LIMIT 1",
                    (stable,),
                ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return fallback
    if row is None:
        return fallback

    username = str(row["username"] or "")
    if username and username != stable and not matches_local:
        # 防止把联系人（而非本人）资料挂到账号上。
        if not (row["alias"] and account_id == str(row["alias"])):
            return fallback
    stable_value = stable if (matches_local or username == stable) else ""
    return {
        "account_id": account_id,
        "stable_id": stable_value,
        "wechat_id": str(row["alias"] or stable_value or username or account_id),
        "display_name": str(row["remark"] or row["nick_name"] or ""),
        "avatar_url": str(row["small_head_url"] or row["big_head_url"] or ""),
    }
