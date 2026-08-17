"""客户端通用格式化工具（供各面板共用，避免重复实现）。

抽取自 monitoring / dashboard / daily_summary 面板的 _format_* 静态方法。
"""

from __future__ import annotations


def format_uptime(seconds) -> str:
    """格式化运行时长：45s / 12m / 3h 25m / 2d 5h"""
    try:
        s = int(seconds)
    except Exception:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def format_elapsed(seconds: float) -> str:
    """格式化耗时（带秒）：45s / 12m 30s / 3h 25m"""
    try:
        s = int(seconds)
    except Exception:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def format_bytes(n: int) -> str:
    """格式化字节数：512B / 1.2KB / 3.45MB"""
    try:
        n = int(n)
    except Exception:
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


def format_tokens(n) -> str:
    """格式化 token 数：1234 → 1.2K，1234567 → 1.2M"""
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
