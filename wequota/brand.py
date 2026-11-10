"""品牌与终端输出原子：logo 字标、配色、按显示宽度对齐。

单独成模块是为了让 cli 与 setup_ui 都只依赖它、互不循环导入。
上色规则：只在「交互式终端 + 未声明 NO_COLOR + TERM 不是 dumb」时输出 ANSI，
重定向到文件/日志时是纯文本，绝不写进 ESC 垃圾。
"""

from __future__ import annotations

import os
import sys
import unicodedata

# 启动 logo：ANSI Shadow 字体，宽 62 列（80 列终端不折行）。
LOGO_ART = r"""
 ██╗    ██╗███████╗ ██████╗ ██╗   ██╗ ██████╗ ████████╗ █████╗
 ██║    ██║██╔════╝██╔═══██╗██║   ██║██╔═══██╗╚══██╔══╝██╔══██╗
 ██║ █╗ ██║█████╗  ██║   ██║██║   ██║██║   ██║   ██║   ███████║
 ██║███╗██║██╔══╝  ██║▄▄ ██║██║   ██║██║   ██║   ██║   ██╔══██║
 ╚███╔███╔╝███████╗╚██████╔╝╚██████╔╝╚██████╔╝   ██║   ██║  ██║
  ╚══╝╚══╝ ╚══════╝ ╚══▀▀═╝  ╚═════╝  ╚═════╝    ╚═╝   ╚═╝  ╚═╝
"""

LOGO = LOGO_ART                  # 兼容旧名

C_MARK = "\x1b[38;5;78m"         # logo / ✓ ➜：亮绿（项目主色）
C_TITLE = "\x1b[38;5;151m"       # 版本行：浅绿
C_DIM = "\x1b[38;5;243m"         # 标签：灰
C_VALUE = "\x1b[38;5;250m"       # 取值：浅灰白
C_RESET = "\x1b[0m"


def supports_color(stream: object | None = None) -> bool:
    """是否输出 ANSI 颜色：非交互终端 / NO_COLOR / TERM=dumb 一律不上色。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def paint(text: str, code: str, enabled: bool = True) -> str:
    """给一段文本上色；enabled=False 时原样返回（纯文本降级）。"""
    if not enabled or not text:
        return text
    return f"{code}{text}{C_RESET}"


def display_width(text: str) -> int:
    """按终端显示宽度算长度：中日韩全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad_label(text: str, width: int = 12) -> str:
    """按显示宽度右补空格，保证中文标签和英文标签在终端里对齐。"""
    return text + " " * max(0, width - display_width(text))


def center_line(line: str, width: int) -> str:
    """按显示宽度居中一行（logo 在控制台窗口里居中靠上）。"""
    pad = max(0, (width - display_width(line)) // 2)
    return " " * pad + line
