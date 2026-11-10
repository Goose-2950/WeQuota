"""定位 huohuoer/wechat-cli 留下的明文 SQLite 解密副本（M13）。

huohuoer 的 DBCache 把整库解密到 `%TEMP%/wechat_cli_cache/<md5(rel_key)[:12]>.db`，
并把 `rel_key -> {path, db_mt, wal_mt}` 索引写在同目录 `_mtimes.json`。

本模块只做「找文件 + 只读打开」，**不含任何密钥提取 / 解密代码**：解密全部由
外部工具完成，WeQuota 仅消费其结果。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    """huohuoer DBCache 的默认缓存目录：系统临时目录下 wechat_cli_cache。"""
    return Path(tempfile.gettempdir()) / "wechat_cli_cache"


def _state_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".wechat-cli"


class DecryptedCacheError(RuntimeError):
    """找不到解密副本：通常意味着还没跑过 `wechat-cli init` / 任一查询命令。"""


class DecryptedCache:
    """huohuoer 解密副本索引。"""

    def __init__(self, cache_dir: str | Path | None = None,
                 state_dir: str | Path | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.state_dir = Path(state_dir) if state_dir else _state_dir()

    # ---------- 索引 ----------

    @property
    def mtimes_file(self) -> Path:
        return self.cache_dir / "_mtimes.json"

    def _load_index(self) -> dict[str, Any]:
        if not self.mtimes_file.exists():
            return {}
        try:
            data = json.loads(self.mtimes_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def resolve(self, rel_key: str) -> Path | None:
        """把逻辑路径（如 `session/session.db`）映射到解密后的明文库路径。"""
        norm = rel_key.replace("\\", "/")
        index = self._load_index()
        for key, info in index.items():
            if key.replace("\\", "/") == norm:
                path = info.get("path") if isinstance(info, dict) else None
                if path and Path(path).exists():
                    return Path(path)
        return None

    def available(self) -> bool:
        return bool(self._load_index())

    def message_db_paths(self) -> list[Path]:
        """所有已解密的 `message/message_N.db` 明文库。"""
        out: list[Path] = []
        for key, info in self._load_index().items():
            norm = key.replace("\\", "/")
            if not norm.startswith("message/") or not norm.endswith(".db"):
                continue
            path = info.get("path") if isinstance(info, dict) else None
            if path and Path(path).exists():
                out.append(Path(path))
        return sorted(out)

    def session_db_path(self) -> Path | None:
        return self.resolve("session/session.db")

    def contact_db_path(self) -> Path | None:
        return self.resolve("contact/contact.db")


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """以只读模式打开明文库，避免误写破坏上游缓存。"""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@lru_cache(maxsize=1)
def find_cache() -> DecryptedCache:
    return DecryptedCache()
