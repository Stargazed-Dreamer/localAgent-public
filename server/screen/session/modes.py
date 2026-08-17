"""权限模式定义 + 端点分类。

三档权限模式：
- NO_PERMISSION: 只读端点放行，副作用端点返回 403
- NORMAL: 普通执行模式，10 分钟告警 + 30 分钟降级到无权限
- WATCHDOG: 看门狗模式，默认 10h（1-999h 可调），到期降级到 NORMAL
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Computer Use 权限模式。"""

    NO_PERMISSION = "no_permission"
    NORMAL = "normal"
    WATCHDOG = "watchdog"


# 副作用端点名单：无权限模式下返回 403
# 这些端点会改变系统状态（键鼠操作 / 窗口操作 / 桌面事务）
ENDPOINTS_SIDE_EFFECT: frozenset[str] = frozenset({
    "screen_action",
    "screen_batch_actions",
    "screen_desktop_transaction",
    "screen_semantic_action",
    "screen_focus_window",
    "screen_window_minimize",
    "screen_window_restore",
    "screen_window_raise",
    "screen_window_close",
    "screen_app_launch",
    "screen_app_wait",
    "screen_preview_action",
})

# 只读端点名单：无权限模式下正常执行，且任何模式下调用语重置 idle 计时
ENDPOINTS_READONLY: frozenset[str] = frozenset({
    "screen_capture",
    "screen_ocr",
    "screen_snapshot",
    "screen_accessibility_snapshot",
    "screen_wait_for",
    "screen_analyze",
    "screen_app_list",
    "screen_window_resolve",
    "screen_list_windows",
})


def is_side_effect_endpoint(operation_id: str) -> bool:
    """判断端点是否为副作用端点（无权限时返回 403）。"""
    return operation_id in ENDPOINTS_SIDE_EFFECT


def is_readonly_endpoint(operation_id: str) -> bool:
    """判断端点是否为只读端点（无权限时放行）。"""
    return operation_id in ENDPOINTS_READONLY
