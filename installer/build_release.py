"""发布构建脚本：前端构建 → whisper 模型就绪 → PyInstaller 打包。

用法：
    python -m installer.build_release            # 全量构建
    python -m installer.build_release --dry-run  # 只打印各步骤命令

每一步都是独立 BuildStep，命令可注入 runner 执行，便于测试与排障。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = Path(__file__).resolve().parent / "wequota.spec"


def python_version_error(version_info: tuple[int, int, int] | None = None) -> str | None:
    """Return a release-build error unless the interpreter is Python 3.11.x."""
    v = version_info or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    if tuple(v[:2]) != (3, 11):
        return f"发布构建需要 Python 3.11.x，当前为 {v[0]}.{v[1]}.{v[2]}"
    return None


@dataclass
class BuildStep:
    name: str
    command: list[str]
    cwd: Path = ROOT
    optional: bool = False          # optional 步骤失败不中断构建


def pyinstaller_command() -> list[str]:
    return [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            str(SPEC_PATH)]


def build_steps(python: str | None = None) -> list[BuildStep]:
    """完整构建流水线。whisper 模型步骤 optional：没网时运行时仍可后台下载。"""
    py = python or sys.executable
    return [
        BuildStep("dependency-check", [py, "-m", "pip", "check"]),
        BuildStep("frontend", ["npm", "run", "build"], cwd=ROOT / "web"),
        BuildStep("whisper-model",
                  [py, "-c",
                   "from wequota.transcribe import build_transcriber; "
                   "from wequota.config import WhisperConfig; "
                   "t = build_transcriber(WhisperConfig(auto_download=True)); "
                   "ok, msg = t.download_model(); print(msg); "
                   "raise SystemExit(0 if ok else 1)"],
                  optional=True),
        BuildStep("pyinstaller", pyinstaller_command()),
    ]


def run_steps(steps: list[BuildStep],
              runner: Callable[[BuildStep], int] | None = None) -> bool:
    """顺序执行构建步骤；非 optional 步骤失败即中断。"""
    def _default(step: BuildStep) -> int:
        print(f"\n=== [{step.name}] {' '.join(step.command)} (cwd={step.cwd}) ===",
              flush=True)
        return subprocess.run(step.command, cwd=step.cwd).returncode

    run = runner or _default
    for step in steps:
        rc = run(step)
        if rc != 0:
            if step.optional:
                print(f"  · 可选步骤 {step.name} 失败（rc={rc}），继续", flush=True)
                continue
            print(f"  ✗ 步骤 {step.name} 失败（rc={rc}），构建中断", flush=True)
            return False
        print(f"  ✓ 步骤 {step.name} 完成", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WeQuota 发布构建")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    args = parser.parse_args(argv)
    version_error = python_version_error()
    if version_error:
        print(f"✗ {version_error}", file=sys.stderr, flush=True)
        return 2
    steps = build_steps()
    if args.dry_run:
        for s in steps:
            tag = "（可选）" if s.optional else ""
            print(f"[{s.name}]{tag} cd {s.cwd} && {' '.join(s.command)}")
        return 0
    return 0 if run_steps(steps) else 1


if __name__ == "__main__":
    sys.exit(main())
