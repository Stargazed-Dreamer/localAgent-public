"""屏幕控制模块（包）- 截图、窗口管理、键鼠操控、安全机制、焦点安全

拆分自原 server/screen.py（~2500 行），保持 `from server.screen import xxx` 完全兼容。

子模块：
- windows: 窗口枚举/查找、管理员权限检查、强力焦点激活
- capture: 全屏/窗口截图、滚动拼接、颜色命名
- input: SendInput 注入键鼠操控、参数防呆
- security: 紧急停止、危险关键词检查、自动跳过坐标缓存
- focus: 评估文档 P0 — canonical window token、焦点证据、焦点泄漏检测
- routes: 15+ REST 端点 + 25 Pydantic 模型 + 启动/关闭钩子
"""

# Windows 子模块：窗口枚举、查找、管理员权限、焦点激活
# 截图子模块：mss 复用、全屏/窗口截图、滚动拼接、颜色命名
from server.screen.capture import (
    _capture_fullscreen,
    _capture_via_fullscreen_crop,
    _capture_window,
    _color_name,
    _find_overlap_and_stitch,
    _get_fullscreen_offset,
    _get_mss,
    _images_equal,
    _mss_local,
    is_desktop_locked,
)

# 焦点安全子模块：评估文档 P0 改造
# - canonical window token（UWP host/child 同族）
# - 焦点证据收集（foreground/focus/target_match）
# - 焦点泄漏检测（FOCUS_LEAK_PREVENTED 保护 ChatGPT/Codex/Trae/Cursor 等宿主）
# - 焦点强校验主入口 verify_focus_for_input
from server.screen.focus import (
    DEFAULT_PROTECTED_PROCESSES,
    EXECUTED_UNVERIFIED,
    FOCUS_LEAK_PREVENTED,
    FOCUS_NOT_VERIFIED,
    STALE_COORDINATES,
    collect_focus_evidence,
    detect_focus_leak,
    is_hwnd_in_family,
    is_protected_process,
    resolve_canonical_window,
    verify_focus_for_input,
)

# 键鼠操控子模块：SendInput 注入、参数防呆、操作执行
from server.screen.input import (
    _INPUT,
    _KEYBDINPUT,
    _MOUSEINPUT,
    SUPPORTED_ACTIONS,
    _execute_action,
    _send_double_click,
    _send_unicode_text,
    _send_vk,
    _validate_action_params,
)

# 窗口生命周期子模块：评估文档 P1-B 改造
# - list_apps / launch_app / wait_for_window：应用列表/启动/等待窗口
# - resolve_window：多匹配候选 + 颁发 window_token（canonical_hwnd + pid + create_time）
# - minimize/restore/raise/close_window：窗口操作 + 返回 post_state
# - verify_window_token：检测 hwnd 销毁 / pid 复用 / 进程重启 → WINDOW_TOKEN_INVALID
from server.screen.lifecycle import (
    WINDOW_AMBIGUOUS,
    WINDOW_NOT_FOUND,
    WINDOW_TOKEN_INVALID,
    close_window,
    launch_app,
    list_apps,
    minimize_window,
    raise_window,
    resolve_window,
    restore_window,
    verify_window_token,
    wait_for_window,
)

# 路由子模块：router、启动/关闭钩子、全部 Pydantic 模型
from server.screen.routes import (
    ActionRequest,
    ActionResponse,
    AnalyzeRequest,
    BatchActionItem,
    BatchActionsRequest,
    CaptureInlineResponse,
    CaptureRequest,
    CaptureResponse,
    ClipboardReadResponse,
    ClipboardWriteRequest,
    ClipboardWriteResponse,
    ConfirmStartRequest,
    ConfirmStartResponse,
    FocusWindowRequest,
    OverlayRequest,
    OverlayResponse,
    PreviewActionRequest,
    PreviewActionResponse,
    PreviewPoint,
    ScreenStatusResponse,
    ScrollCaptureRequest,
    ScrollCaptureResponse,
    SnapshotRequest,
    UiaActionRequest,
    UiaActionResponse,
    UiaElement,
    UiaSnapshotRequest,
    UiaSnapshotResponse,
    WaitForRequest,
    WaitForResponse,
    WindowInfo,
    WindowsResponse,
    ZoomRequest,
    ZoomResponse,
    on_shutdown,
    on_startup,
    router,
)

# 安全子模块：紧急停止单例、危险关键词、自动跳过缓存、安全检查
from server.screen.security import (
    _AUTO_SKIP_MAXLEN,
    DANGER_KEYWORDS_BLOCK,
    DANGER_KEYWORDS_CONFIRM,
    EmergencyStopManager,
    _auto_skip_coords,
    _check_danger,
    _check_window_bounds,
    _record_skip,
    clear_auto_skip_cache,
    emergency,
)

# UIA 语义层子模块：评估文档 P0-5 改造
# - take_uia_snapshot：accessibility tree 快照（role/name/value/checked/bounds/element_id）
# - execute_semantic_action：invoke/select/toggle/set_value/expand/collapse/scroll
# - STALE_ELEMENT 状态码（element_id 失效）
# - UIA_NOT_AVAILABLE 状态码（uiautomation 库不可用，需回退 OCR/视觉）
from server.screen.uia import (
    STALE_ELEMENT,
    UIA_NOT_AVAILABLE,
    execute_semantic_action,
    take_uia_snapshot,
    uia_available,
)
from server.screen.windows import (
    _ADMIN_STATUS,
    _enum_windows,
    _find_window,
    _force_focus_window,
    _get_idle_seconds,
    _is_admin,
)


def get_status() -> dict:
    """Screen 模块状态概览（供 /health 调用，不暴露 raw 私有容器）"""
    return {
        "admin_privileges": _ADMIN_STATUS,
        "auto_skip_cache_size": len(_auto_skip_coords),
        "emergency_stopped": emergency.is_stopped,
    }


__all__ = [
    # 路由与生命周期
    "router",
    "on_startup",
    "on_shutdown",
    # 安全（外部模块直接导入）
    "emergency",
    "_auto_skip_coords",
    "_ADMIN_STATUS",
    # 窗口管理
    "_enum_windows",
    "_find_window",
    "_is_admin",
    "_force_focus_window",
    "_get_idle_seconds",
    # 截图
    "_mss_local",
    "_get_mss",
    "_capture_fullscreen",
    "is_desktop_locked",
    "_get_fullscreen_offset",
    "_capture_via_fullscreen_crop",
    "_capture_window",
    "_images_equal",
    "_find_overlap_and_stitch",
    "_color_name",
    # 键鼠操控
    "_MOUSEINPUT",
    "_KEYBDINPUT",
    "_INPUT",
    "_send_vk",
    "_send_unicode_text",
    "_send_double_click",
    "SUPPORTED_ACTIONS",
    "_validate_action_params",
    "_execute_action",
    # 焦点安全（评估文档 P0）
    "DEFAULT_PROTECTED_PROCESSES",
    "FOCUS_NOT_VERIFIED",
    "FOCUS_LEAK_PREVENTED",
    "STALE_COORDINATES",
    "EXECUTED_UNVERIFIED",
    "resolve_canonical_window",
    "is_hwnd_in_family",
    "collect_focus_evidence",
    "is_protected_process",
    "detect_focus_leak",
    "verify_focus_for_input",
    # UIA 语义层（评估文档 P0-5）
    "STALE_ELEMENT",
    "UIA_NOT_AVAILABLE",
    "uia_available",
    "take_uia_snapshot",
    "execute_semantic_action",
    # 窗口生命周期（评估文档 P1-B）
    "WINDOW_TOKEN_INVALID",
    "WINDOW_AMBIGUOUS",
    "WINDOW_NOT_FOUND",
    "list_apps",
    "launch_app",
    "wait_for_window",
    "resolve_window",
    "minimize_window",
    "restore_window",
    "raise_window",
    "close_window",
    "verify_window_token",
    # 安全机制
    "EmergencyStopManager",
    "DANGER_KEYWORDS_BLOCK",
    "DANGER_KEYWORDS_CONFIRM",
    "_AUTO_SKIP_MAXLEN",
    "_record_skip",
    "clear_auto_skip_cache",
    "_check_danger",
    "_check_window_bounds",
    # Pydantic 模型
    "CaptureRequest",
    "CaptureResponse",
    "WindowInfo",
    "WindowsResponse",
    "ActionRequest",
    "ActionResponse",
    "OverlayRequest",
    "OverlayResponse",
    "ConfirmStartRequest",
    "ConfirmStartResponse",
    "ScreenStatusResponse",
    "FocusWindowRequest",
    "BatchActionItem",
    "BatchActionsRequest",
    "CaptureInlineResponse",
    "PreviewPoint",
    "PreviewActionRequest",
    "PreviewActionResponse",
    "ScrollCaptureRequest",
    "ScrollCaptureResponse",
    "SnapshotRequest",
    "WaitForRequest",
    "WaitForResponse",
    "AnalyzeRequest",
    "UiaSnapshotRequest",
    "UiaSnapshotResponse",
    "UiaElement",
    "UiaActionRequest",
    "UiaActionResponse",
]
