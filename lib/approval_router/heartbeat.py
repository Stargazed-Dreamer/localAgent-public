"""心跳状态机：跟踪审批面板进程在线状态。

纯逻辑层，不依赖 server / Qt / FastAPI。时钟可注入便于测试。
"""
from __future__ import annotations

import time

# 默认心跳阈值（秒）：超过此时间无心跳判定面板离线
# 30s = 3 × 10s 心跳间隔，防抖动
DEFAULT_THRESHOLD = 30.0

# 模块级状态（单例，server 生命周期内有效）
_last_heartbeat: float = 0.0
_panel_version: str = ""

# 可注入时钟（默认 time.monotonic），便于测试推进时间
_time_fn = time.monotonic


def now() -> float:
    """当前时钟时间（可注入）。"""
    return _time_fn()


def set_clock(fn) -> None:
    """注入时钟函数（测试用）。"""
    global _time_fn
    _time_fn = fn


def reset_clock() -> None:
    """恢复默认时钟。"""
    global _time_fn
    _time_fn = time.monotonic


def update_heartbeat(panel_version: str = "") -> None:
    """面板心跳上报。"""
    global _last_heartbeat, _panel_version
    _last_heartbeat = _time_fn()
    if panel_version:
        _panel_version = panel_version


def is_panel_online(threshold: float = DEFAULT_THRESHOLD) -> bool:
    """面板是否在线（threshold 秒内有心跳）。"""
    if _last_heartbeat == 0.0:
        return False
    return (_time_fn() - _last_heartbeat) < threshold


def get_status() -> dict:
    """返回心跳状态快照（monotonic 时间戳，server 端负责转 wall-clock）。"""
    return {
        "panel_online": is_panel_online(),
        "last_heartbeat": _last_heartbeat,
        "panel_version": _panel_version,
    }


def reset() -> None:
    """重置状态（测试用）。"""
    global _last_heartbeat, _panel_version
    _last_heartbeat = 0.0
    _panel_version = ""
