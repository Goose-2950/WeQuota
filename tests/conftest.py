"""pytest 全局夹具。

两个本机环境问题在这里一次性解决：

1. `/tmp` 是 **10MB 的 tmpfs**，每个用例的 `tmp_path` 都会在里面建库，
   用例一多就 `OSError: No space left on device` → 把临时根目录挪到仓库内 `.tmp/pytest`。
2. pytest 会清理上一轮残留的临时目录（上千个文件），触发批量删除安全策略把测试卡住
   → 每轮用**独立子目录**，pytest 看不到上一轮的目录，就不会触发清理。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"
RUN_ROOT = TMP_ROOT / f"run-{int(time.time())}"       # 每轮独立，避免跨轮清理
RUN_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(RUN_ROOT))
os.environ.setdefault("TMPDIR", str(RUN_ROOT))
