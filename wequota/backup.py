"""数据库快照备份（遗留项 F1 的轻量版）。

策略刻意做得很保守，避免备份本身成为风险源：
- **在线备份**：走 sqlite3 的 backup API，不需要停写、不会锁死主库。
- **一天最多一份**：文件名带日期，同一天重复启动直接跳过（不刷屏、不占盘）。
- **只保留最近 N 份**（默认 3），超出的最老的先删。
- **任何异常都吞掉**：备份失败只影响可恢复性，绝不能挡住启动。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_PREFIX = "wequota-"
SNAPSHOT_SUFFIX = ".db"


def snapshot_path(backup_dir: Path, day: str | None = None) -> Path:
    """当天快照的目标路径（同一天固定一个名字 → 天然去重）。"""
    day = day or datetime.now().strftime("%Y%m%d")
    return Path(backup_dir) / f"{SNAPSHOT_PREFIX}{day}{SNAPSHOT_SUFFIX}"


def _existing_snapshots(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(p for p in backup_dir.iterdir()
                  if p.is_file() and p.name.startswith(SNAPSHOT_PREFIX)
                  and p.name.endswith(SNAPSHOT_SUFFIX))


def rotate(backup_dir: Path, keep: int) -> list[str]:
    """删掉超出保留份数的最老快照，返回被删文件名。"""
    if keep <= 0:
        return []
    snapshots = _existing_snapshots(backup_dir)
    stale = snapshots[:max(0, len(snapshots) - keep)]
    removed: list[str] = []
    for p in stale:
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            continue
    return removed


def snapshot_database(db_path: str | Path, backup_dir: str | Path,
                      keep: int = 3, day: str | None = None) -> dict[str, Any]:
    """做一份当天的数据库快照。

    返回 {ok, path, skipped, removed, error}：
    - 当天已有一份 → skipped=True（不重写，保住当天第一份）
    - 失败 → ok=False 且 error 有原因，调用方只需打日志
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    target = snapshot_path(backup_dir, day=day)
    result: dict[str, Any] = {"ok": False, "path": str(target), "skipped": False,
                              "removed": [], "error": None}
    if target.exists():
        result["skipped"] = True
        result["ok"] = True
        result["removed"] = rotate(backup_dir, keep)
        return result
    if not db_path.exists():
        result["error"] = f"数据库不存在：{db_path}"
        return result

    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 用普通连接（非 URI）：中文/含空格的路径不必做 URI 转义；
        # sqlite3 的 backup() 本身是官方推荐的在线备份方式，WAL 下也安全
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(target))
        src.backup(dst)                     # 在线备份，不需要停写
        dst.commit()
        result["ok"] = True
    except (sqlite3.Error, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            if target.exists():
                target.unlink()             # 半成品不要留在那儿误导人
        except OSError:
            pass
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    result["removed"] = rotate(backup_dir, keep)
    return result
