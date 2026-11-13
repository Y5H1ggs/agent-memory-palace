"""_console.py - 让中文输出在非 UTF-8 控制台上也能打印（不炸 UnicodeEncodeError）。

起因：GitHub Actions 的 windows-latest 默认代码页是 cp1252，`print("记忆")` 直接抛
UnicodeEncodeError —— 本地 cp936 一切正常，CI 上必红。这不是「CI 环境特殊」，
而是任何英文区域设置的 Windows 用户都会遇到的真实问题。

处理原则：只在**当前编码装不下中文**时才切到 UTF-8；cp936 / cp932 这类能装下中文的编码
不动它，避免把本来正常的终端搞成乱码。
"""
from __future__ import annotations

import sys

_PROBE = "中文编码探测"


def ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding in ("utf8", "utf8mb4"):
            continue
        try:
            _PROBE.encode(encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass
