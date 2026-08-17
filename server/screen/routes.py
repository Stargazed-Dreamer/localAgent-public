"""屏幕控制路由：14 个 REST 端点 + 4 个 legacy 别名 + 24 个 Pydantic 模型 + 启动/关闭钩子。

拆分自原 server/screen.py。依赖 windows/capture/input/security/focus 五个子模块。
路由 path/operation_id/响应结构与拆分前完全一致，确保 MCP 工具名不变。

注：端点用 def（非 async），FastAPI 自动放线程池执行，避免阻塞事件循环
（截图/键鼠/OCR 均为阻塞调用）。

评估文档（docs/evaluations/2026-07-20-computer-use-vs-localagent.md）P0 改进：
- 键盘动作前强制焦点校验（FOCUS_NOT_VERIFIED / FOCUS_LEAK_PREVENTED）
- canonical window token（UWP host/child 同族）
- 焦点证据返回（foreground_before/after）
- 分层状态（transport / delivery / postcondition）
- 坐标绑定 snapshot_id（STALE_COORDINATES）
- 声明式 expected 后验（ocr_contains / ocr_not_contains）
- P0-5 UI Automation 语义层（screen_accessibility_snapshot / screen_semantic_action / STALE_ELEMENT）
"""

import base64
import io
import logging

# Windows DPI感知：由 server/main.py 在启动时统一设置（SetProcessDpiAwareness(2)），
# 此处不再重复调用以避免 import 副作用。
import time
import uuid

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("localagent.screen")
router = APIRouter(prefix="/screen", tags=["Screen"])


# ========== 子模块 re-export（供 routes 端点 + 端点模块通过 routes.X 访问，兼容 monkeypatch）==========
from lib.schema import BaseSchema  # noqa: E402
from server.screen.capture import (  # noqa: E402,F401
    _capture_fullscreen,
    _capture_window,
    _color_name,
    _find_overlap_and_stitch,
    _get_fullscreen_offset,
    _images_equal,
)
from server.screen.focus import (  # noqa: E402,F401
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
from server.screen.input import (  # noqa: E402,F401
    _execute_action,
    _validate_action_params,
)
from server.screen.security import (  # noqa: E402,F401
    _auto_skip_coords,
    _check_danger,
    _check_window_bounds,
    _record_skip,
    clear_auto_skip_cache,
    emergency,
)
from server.screen.windows import (  # noqa: E402,F401
    _ADMIN_STATUS,
    _enum_windows,
    _find_window,
    _force_focus_window,
    _is_admin,
)

# ========== 模块常量（避免魔法数散落） ==========

_OVERLAY_HIDE_DELAY = 0.05        # 截图前隐藏覆盖层后等待秒数
_ACTION_SETTLE_DELAY = 0.3        # 操作后等待界面响应秒数
_FOCUS_RETRY_DELAY = 0.2          # 焦点激活后等待秒数
_SCROLL_CAPTURE_MAX_HEIGHT = 8000 # 拼接长图最大高度（像素）
_GO_TOP_SCROLL_TIMES = 10         # 回到顶部时滚轮次数
_GO_TOP_SCROLL_AMOUNT = 3         # 回到顶部每次滚轮量
_SNAPSHOT_TTL_SECONDS = 300       # 截图 snapshot_id 元数据缓存保留秒数（5 分钟）


def _restore_overlay_after_capture() -> None:
    """截图/OCR 后恢复覆盖层显示。

    新 OverlayClient 通过 _tick_loop 每秒读 SessionManager.status() 推 IPC tick，
    子进程 OverlayWidget 自己根据 mode/phase/remaining_seconds/task_description
    渲染文案与颜色。所以本函数只需恢复 overlay 可见性，文案由 tick 自动刷新。

    旧实现按 watchdog/normal 模式拼接 message + persistent=True 调 show_overlay，
    但新 OverlayClient.show_overlay(position="top") 不接受 message/persistent 参数，
    旧调用靠 try/except 兜底掩盖了签名不匹配 bug（文案恢复逻辑实际失效）。
    """
    try:
        from server.overlay_client import overlay_client

        overlay_client.show_overlay()
    except Exception:
        pass


# 需要焦点校验的动作（键盘/剪贴板/快捷键类——评估文档 P0 要求"未验证焦点时不发送"）
_KEYBOARD_ACTIONS = frozenset({"type", "type_immediate", "hotkey"})
# 需要坐标绑定 snapshot 的动作（点击/拖拽/滚动——评估文档 P0 要求"坐标必须引用 snapshot id"）
_COORDINATE_ACTIONS = frozenset({"click", "double_click", "right_click", "scroll", "drag"})


# ========== 接管确认（takeover confirm） ==========

def _ensure_takeover_approved(screen_cfg: dict, task_description: str | None) -> tuple[bool, str]:
    """顶栏未显示时弹"接管确认"窗口，询问用户是否允许 agent 接管键鼠操作。

    评估文档外的新增保护：agent 调用争夺输入的端点前，若 overlay 未显示，
    必须先弹窗征求用户同意（30s 超时，超时行为可配置）。

    Returns:
        (approved, reason)
        - approved=True：顶栏已显示或用户允许或超时 proceed
        - approved=False：用户拒绝 / 超时 cancel / GUI 异常（fail-closed）
        - reason：用户反馈 / 超时原因 / 错误信息（approved=True 时也可能非空，如"timeout_auto_proceed"）
    """
    if not screen_cfg.get("takeover_confirm_enabled", True):
        return True, ""

    try:
        from server.overlay_client import overlay_client
        from server.screen.session import Mode as SessionMode
        from server.screen.session import get_session_manager
    except Exception as e:
        # GUI 模块不可用——fail-closed 取消
        logger.warning(f"takeover confirm: overlay/session 模块导入失败，fail-closed 取消: {e}")
        return False, f"GUI 模块不可用: {e}"

    # 当前任务授权短路。活动时间只在成功投递后更新，不在 gate 阶段更新。
    session = get_session_manager()
    if session.is_active():
        return True, ""

    # watchdog 模式短路：用户显式开启看门狗模式 = 强授权，takeover_confirm 弹窗无意义（用户不在场）
    if session.status().get("mode") == "watchdog":
        return True, ""

    if overlay_client.overlay_visible:
        # 顶栏已显示——直接放行，无需弹窗
        return True, ""

    timeout = int(screen_cfg.get("takeover_confirm_timeout_seconds", 30))
    timeout_action = screen_cfg.get("takeover_timeout_action", "cancel")
    if timeout_action not in ("proceed", "cancel"):
        timeout_action = "cancel"

    # 任务描述：agent 显式传 > 通用文案
    desc = task_description or "Agent 请求接管键鼠操作"

    try:
        result = overlay_client.ensure_takeover_approved(
            desc,
            timeout=timeout,
            timeout_action=timeout_action,
            allow_task_authorization=screen_cfg.get(
                "takeover_persistent_enabled", True
            ),
            source="agent",
        )
    except Exception as e:
        logger.warning(f"takeover confirm: ensure_takeover_approved 异常，fail-closed 取消: {e}")
        return False, f"接管确认异常: {e}"

    status = result.get("status", "cancelled")
    reason = result.get("reason", "")

    if status == "confirmed":  # noqa: SIM116  # 状态分派，含 reason fallback 不宜字典化
        # T22：ensure_takeover_approved 不再内部授权，gate 函数在 confirmed + task_authorization 时调 SessionManager.grant
        if result.get("task_authorization") and not session.is_active():
            authorized_mode = result.get("authorized_mode", "normal")
            if authorized_mode == "watchdog":
                session.grant(
                    mode=SessionMode.WATCHDOG,
                    task_description=desc,
                    source="agent",
                    max_duration_hours=result.get("max_duration_hours"),
                    shutdown_permitted=bool(result.get("shutdown_permitted", False)),
                )
            else:
                session.grant(mode=SessionMode.NORMAL, task_description=desc, source="agent")
        return True, reason
    elif status == "skipped":
        # 顶栏已显示（race condition：检查时为 False，调用时变 True）—— 放行
        return True, reason
    elif status == "error":
        # GUI 异常——fail-closed
        return False, reason or "GUI 子进程异常"
    else:  # cancelled
        return False, reason


def _task_authorization_is_active() -> bool:
    try:
        from server.screen.session import get_session_manager
        return get_session_manager().is_active()
    except Exception:
        return False


def _touch_task_authorization_after_delivery(
    *,
    success: bool,
    delivery_status: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Extend task authorization only after a successful, non-leaked delivery."""
    if not success or dry_run or delivery_status == "leaked":
        return False
    try:
        from server.screen.session import get_session_manager
        return get_session_manager().extend()
    except Exception:
        return False


def _enforce_session_permission() -> dict:
    """副作用端点权限检查：无权限时 raise 403。

    副作用端点（execute_action / batch_actions / desktop_transaction /
    screen_semantic_action / focus_window / screen_window_*）开头调用此函数。
    只读端点不调用（无权限下正常执行，但会调 SessionManager.extend() 重置 idle）。

    Returns:
        当前 session status 快照（active=True 时），供调用方读取 mode/phase 等。
        无权限时不会返回（raise 403）。
    """
    from fastapi import HTTPException

    from server.screen.session import get_session_manager
    session = get_session_manager()
    if not session.can_operate():
        status = session.status()
        raise HTTPException(
            status_code=403,
            detail={
                "error": "unauthorized",
                "message": "先调 POST /screen/control/request 请求授权",
                "current_mode": status.get("mode", "no_permission"),
            },
        )
    return session.status()


def _session_mode() -> str:
    """获取当前会话模式字符串（no_permission / normal / watchdog）。"""
    try:
        from server.screen.session import get_session_manager
        return get_session_manager().status().get("mode", "no_permission")
    except Exception:
        return "no_permission"


def _confirm_control_operation(
    *,
    action: str,
    text: str | None = None,
    keys: list[str] | None = None,
) -> tuple[bool, str]:
    """Ask once before an explicitly confirmed or danger-confirm operation."""
    try:
        screenshot_b64 = base64.b64encode(_capture_fullscreen()).decode("ascii")
    except Exception:
        screenshot_b64 = None
    try:
        from server.overlay_client import overlay_client
        result = overlay_client.confirm_action(
            screenshot_b64=screenshot_b64,
            action=action,
            text=text,
            keys=keys,
        )
    except Exception as exc:
        return False, f"确认窗口不可用: {exc}"
    if result.get("status") != "confirmed":
        return False, result.get("reason", "")
    return True, result.get("reason", "")


# ========== 当前任务授权（旧 takeover-persistent 名称兼容）==========


class ControlRequest(BaseSchema):
    """当前任务授权请求。"""
    task_description: str | None = None  # 任务描述（用于 GUI 反馈，≤30 字超出截断）
    source: str = "agent"
    mode: str = "normal"  # "normal" | "watchdog"；watchdog 模式不因空闲撤销，硬上限到期降级到 normal
    max_duration_hours: int | None = None  # watchdog 时长（小时），范围 [watchdog_min_hours, watchdog_max_hours]；None 用默认值


class ControlResponse(BaseSchema):
    """当前任务授权响应（保留 persistent_mode 兼容字段）。"""
    success: bool
    persistent_mode: bool
    source: str = ""  # "gui" | "agent" | ""
    message: str
    status: str = ""
    user_reason: str = ""
    requested_mode: str = "normal"  # agent 请求的模式（"normal" | "watchdog"）
    mode: str = "normal"  # 用户实际授权的模式（"normal" | "watchdog"）；可能因用户未勾选 watchdog 而从 requested_mode 降级
    max_duration_seconds: float | None = None
    task_authorization: dict = {}


@router.post("/control/request", response_model=ControlResponse, operation_id="screen_request_control")
def screen_request_control(req: ControlRequest):
    """Request user-mediated current-task authorization.

    mode="watchdog" 时弹窗显示看门狗复选框（默认不勾，用户主动勾选才授权 watchdog）。
    服务端以用户实际选择为准：agent 请求 watchdog 但用户未勾选 → 返回 mode="normal" + status="mode_downgraded"。
    """
    from server.config import get_screen_config
    from server.core.health import invalidate_health_cache
    from server.overlay_client import overlay_client
    from server.screen.session import Mode as SessionMode
    from server.screen.session import get_session_manager

    screen_cfg = get_screen_config()
    session = get_session_manager()
    if not screen_cfg.get("takeover_persistent_enabled", True):
        status = session.status()
        return ControlResponse(
            success=False,
            persistent_mode=False,
            source="",
            status="disabled",
            requested_mode=req.mode,
            mode=req.mode,
            task_authorization=status,
            message="当前任务授权已被配置禁用",
        )

    task_description = req.task_description or "Agent 请求接管键鼠操作"
    # 传 req.mode 到弹窗，让用户看到 agent 请求的是 watchdog 并主动勾选
    # ensure_takeover_approved 仅负责弹窗；授权由 SessionManager.grant 统一执行
    result = overlay_client.ensure_takeover_approved(
        task_description,
        timeout=int(screen_cfg.get("takeover_confirm_timeout_seconds", 30)),
        timeout_action=screen_cfg.get("takeover_timeout_action", "cancel"),
        allow_task_authorization=True,
        force_prompt=True,
        source=req.source,
        requested_mode=req.mode,
    )
    approved = result.get("status") in {"confirmed", "skipped"}
    authorized_mode = result.get("authorized_mode", "normal")
    # watchdog 硬上限：优先用 agent 传的 max_duration_hours，否则从 config 读默认
    max_duration_seconds = None
    if authorized_mode == "watchdog":
        if req.max_duration_hours is not None:
            max_hours = int(req.max_duration_hours)
        else:
            max_hours = int(screen_cfg.get("watchdog_default_hours", 10))
        # SessionManager.grant 内部会 clamp 到 [min, max]，此处仅传值
        max_duration_seconds = float(max_hours * 3600)
    # 决策 17：agent 调 request = 显式请求新授权，总是覆盖现有授权
    # （支持 normal→watchdog 升级、task_description 更改、idle 计时重置）
    if approved and result.get("task_authorization"):
        if authorized_mode == "watchdog":
            # 弹窗返回的 max_duration_hours / shutdown_permitted 优先；
            # 弹窗未返回时 fallback 到 agent 请求参数
            dialog_max_hours = result.get("max_duration_hours")
            if dialog_max_hours is not None:
                watchdog_max_hours = int(dialog_max_hours)
            elif req.max_duration_hours is not None:
                watchdog_max_hours = int(req.max_duration_hours)
            else:
                watchdog_max_hours = None
            session.grant(
                mode=SessionMode.WATCHDOG,
                task_description=task_description,
                source=req.source,
                max_duration_hours=watchdog_max_hours,
                shutdown_permitted=bool(result.get("shutdown_permitted", False)),
            )
        else:
            session.grant(
                mode=SessionMode.NORMAL,
                task_description=task_description,
                source=req.source,
            )
    authorization = session.status()
    invalidate_health_cache()
    user_reason = result.get("reason", "")
    if not approved:
        return ControlResponse(
            success=False,
            persistent_mode=False,
            source="",
            status="cancelled",
            requested_mode=req.mode,
            mode=req.mode,
            max_duration_seconds=max_duration_seconds,
            user_reason=user_reason,
            task_authorization=authorization,
            message="用户未授予当前任务控制权限",
        )

    # 授权后显示顶栏（若 ensure_takeover_approved 已显示则更新文案）
    if authorization["active"] and not overlay_client.overlay_visible:
        if authorized_mode == "watchdog":
            max_hours = max(1, int(round((max_duration_seconds or 36000) / 3600)))
            overlay_client.show_overlay(
                message=(
                    f"看门狗模式：{task_description} · 普通操作免确认 · "
                    f"危险操作仍拦截 · {max_hours} 小时后自动收回"
                ),
                persistent=True,
            )
        else:
            idle_total = (authorization.get("idle_warning_seconds", 600)
                          + authorization.get("idle_grace_seconds", 1200))
            timeout_minutes = max(1, int(idle_total / 60))
            overlay_client.show_overlay(
                message=(
                    f"Agent 正在操作：{task_description} · 普通操作免确认 · "
                    f"危险操作仍需确认 · 空闲 {timeout_minutes} 分钟自动收回"
                ),
                persistent=True,
            )

    # 状态判定：agent 请求 watchdog 但用户未勾选 → mode_downgraded
    if req.mode == "watchdog" and authorized_mode != "watchdog":
        status = "mode_downgraded"
    elif authorization["active"]:
        status = "authorized"
    else:
        status = "confirmed_once"

    return ControlResponse(
        success=True,
        persistent_mode=authorization["active"],
        source=authorization["source"],
        requested_mode=req.mode,
        mode=authorization.get("mode", authorized_mode),
        max_duration_seconds=authorization.get("max_duration_seconds"),
        status=status,
        user_reason=user_reason,  # ISSUE-004：成功路径也返回用户反馈
        task_authorization=authorization,
        message=(
            "当前任务授权已开启"
            if authorization["active"]
            else "已允许本次接管，未开启当前任务授权"
        ),
    )


@router.post("/control/release", response_model=ControlResponse, operation_id="screen_release_control")
def screen_release_control():
    """Agent 主动收回当前任务授权。

    收回后覆盖层隐藏、内存授权清除、worker 停止。
    幂等：当前没有授权时调用也返回 success=True。

    Returns:
        persistent_mode=False 表示当前任务授权已收回
    """
    from server.core.health import invalidate_health_cache
    from server.overlay_client import overlay_client
    from server.screen.session import get_session_manager

    session = get_session_manager()
    was_persistent = session.is_active()
    # 显式撤销任务授权（hide_overlay 不再自动撤销）
    if was_persistent:
        session.release("agent_release")
    overlay_client.hide_overlay()
    authorization = session.status()
    invalidate_health_cache()

    return ControlResponse(
        success=True,
        persistent_mode=False,
        source="",
        status="released",
        task_authorization=authorization,
        message=(
            "当前任务授权已收回"
            if was_persistent
            else "当前任务未授权（幂等）"
        ),
    )


# ========== 截图 snapshot 元数据缓存（用于 STALE_COORDINATES 检测） ==========
# snapshot_id → {window_rect, dpi, captured_at, window_title, hwnd}
_snapshot_metadata: dict[str, dict] = {}
_snapshot_metadata_lock = __import__("threading").Lock()


def _record_snapshot(snapshot_id: str, metadata: dict) -> None:
    """记录截图元数据，供 execute_action 的 STALE_COORDINATES 检测使用。"""
    with _snapshot_metadata_lock:
        # 顺手清理过期项（懒清理，避免泄漏）
        now = time.time()
        expired = [k for k, v in _snapshot_metadata.items()
                   if now - v.get("captured_at", 0) > _SNAPSHOT_TTL_SECONDS]
        for k in expired:
            _snapshot_metadata.pop(k, None)
        _snapshot_metadata[snapshot_id] = metadata


def _verify_snapshot_freshness(snapshot_id: str | None, target_hwnd: int | None) -> tuple[bool, str, dict | None]:
    """校验 snapshot 是否仍新鲜（窗口几何/DPI 未变）。

    Returns:
        (fresh, reason, meta)
        fresh=True 时坐标仍可用
        fresh=False 时返回 STALE_COORDINATES 原因，agent 必须重新截图定位
    """
    if not snapshot_id:
        return True, "", None  # 未提供 snapshot_id → 不校验（向后兼容）

    with _snapshot_metadata_lock:
        meta = dict(_snapshot_metadata.get(snapshot_id, {}))

    if not meta:
        return False, "snapshot_id 不存在或已过期（>5min）", None

    # TTL 检查
    if time.time() - meta.get("captured_at", 0) > _SNAPSHOT_TTL_SECONDS:
        return False, "snapshot 已过期（>5min）", meta

    # 窗口几何检查（若 target_hwnd 已知且 meta 记录了 window_rect）
    if target_hwnd and meta.get("window_rect"):
        try:
            import win32gui
            cur_rect = win32gui.GetWindowRect(target_hwnd)
            if tuple(cur_rect) != tuple(meta["window_rect"]):
                return False, (
                    f"窗口几何变化：截图时 {tuple(meta['window_rect'])}，"
                    f"当前 {tuple(cur_rect)}——STALE_COORDINATES"
                ), meta
        except Exception as e:
            # 窗口可能已关闭——视为 stale
            return False, f"无法获取当前窗口几何（{e}）——STALE_COORDINATES", meta

    return True, "", meta


# ========== Pydantic 模型 ==========

class CaptureRequest(BaseSchema):
    mode: str | None = None  # fullscreen | window（None 时用 config default_mode）
    window_title: str | None = None
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突，如"qbittorrent.exe"）
    hwnd: int | None = None  # 直接指定窗口句柄（避免标题歧义）
    format: str = "base64"  # base64（PNG，进文本上下文）| inline（JPEG 压缩，多模态 LLM 直接看图）| path（保存临时文件，返回路径，不污染上下文）
    max_edge: int = 1280  # format=inline 时最长边限制，超过则等比缩放（控制 token 用量）
    jpeg_quality: int = 85  # format=inline 时 JPEG 质量
    force_fullscreen_crop: bool = False  # 跳过 PrintWindow，直接全屏截图+裁剪（DirectX 全屏游戏用）

class CaptureResponse(BaseSchema):
    success: bool
    image: str | None = None  # base64编码的PNG（format=base64 时）
    path: str | None = None  # 临时文件路径（format=path 时），可传给 /ocr/path、/vision/vl/path 等
    width: int | None = None
    height: int | None = None
    window_title: str | None = None
    elapsed_ms: int
    snapshot_id: str | None = None  # 截图快照 ID（传入 execute_action 的 snapshot_id 字段用于 STALE_COORDINATES 检测）
    window_rect: list[int] | None = None  # 截图目标的窗口几何 [left, top, right, bottom]（fullscreen 时为虚拟屏）

class WindowInfo(BaseSchema):
    hwnd: int
    title: str
    class_name: str
    bbox: dict
    width: int
    height: int
    is_visible: bool
    is_minimized: bool
    z_order: int
    pid: int
    process_name: str
    is_foreground: bool = False  # 当前前台窗口标记（_enum_windows 产出，activity_tracker 消费）

class WindowsResponse(BaseSchema):
    windows: list[WindowInfo]
    count: int
    app_lessons_hint: str | None = None  # 软件经验提示（命中 apps/*.md 时注入）


class OverlayRequest(BaseSchema):
    action: str = "show"  # show | hide
    message: str = "Agent操作中，请尽量不要使用电脑"
    position: str = "top"  # top | bottom

class OverlayResponse(BaseSchema):
    success: bool
    message: str

class ConfirmStartRequest(BaseSchema):
    task_description: str
    hotkey_hint: str = "Ctrl+`"

class ConfirmStartResponse(BaseSchema):
    status: str  # confirmed | cancelled

class ScreenStatusResponse(BaseSchema):
    capture_available: bool
    windows_count: int
    emergency_stopped: bool
    overlay_visible: bool
    auto_skip_cache_size: int
    admin_privileges: bool  # 管理员权限（键鼠操控需要）


# ========== 基础路由 ==========

@router.get("/status", response_model=ScreenStatusResponse, operation_id="screen_status")
def screen_status():
    """查询屏幕控制模块状态"""
    capture_available = False
    windows_count = 0
    try:
        windows = _enum_windows()
        windows_count = len(windows)
        capture_available = True
    except Exception:
        pass

    overlay_visible = False
    try:
        from server.overlay_client import overlay_client
        overlay_visible = overlay_client.overlay_visible
    except Exception:
        pass

    return ScreenStatusResponse(
        capture_available=capture_available,
        windows_count=windows_count,
        emergency_stopped=emergency.is_stopped,
        overlay_visible=overlay_visible,
        auto_skip_cache_size=len(_auto_skip_coords),
        admin_privileges=_ADMIN_STATUS,
    )


@router.get("/windows", response_model=WindowsResponse, operation_id="list_windows")
def list_windows():
    """枚举当前所有可见窗口。

    响应含 app_lessons_hint：若返回的窗口中有 process_name 命中
    .agents/skills/computer_use/apps/*.md 软件经验文件，自动注入提示。
    agent 看到 hint 后应调 screen_match_app 查询完整经验（UIA 坑/快捷键/菜单路径）。
    """
    try:
        windows = _enum_windows()
        # 自动注入软件经验提示（闭环：list_windows 是 agent 操作软件的入口）
        hint = None
        try:
            from server.screen.app_lessons import match_app_for_process_names, build_app_lessons_hint
            process_names = list({w.get("process_name", "") for w in windows if w.get("process_name")})
            lessons = match_app_for_process_names(process_names)
            hint = build_app_lessons_hint(lessons)
        except Exception:
            pass  # 经验注入失败不影响窗口枚举
        return WindowsResponse(
            windows=[WindowInfo(**w) for w in windows],
            count=len(windows),
            app_lessons_hint=hint,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"窗口枚举失败: {e}") from None


@router.post("/capture", operation_id="capture_screen")
def capture_screen(req: CaptureRequest):
    """截图 - 全屏或指定窗口

    format=base64: 返回 PNG base64 字符串（进文本上下文，可能撑爆 LLM）
    format=inline: 返回 JPEG 压缩图片 + mcp_image_block=True，MCP 补丁转为 ImageContent，
                  多模态 LLM 直接看图，纯文本 LLM 只看摘要
    format=path:  保存 PNG 到临时文件，返回路径（不污染上下文），
                  路径可传给 /ocr/path、/vision/vl/path 等端点
    """
    # mode 未指定时用 config default_mode（默认 "window"，符合"窗口截图优先"铁律）
    if req.mode is None:
        from server.config import get_screen_config
        req.mode = get_screen_config().get("default_mode", "window")

    t0 = time.perf_counter()

    # 截图前隐藏覆盖层
    overlay_was_visible = False
    try:
        from server.overlay_client import overlay_client
        overlay_was_visible = overlay_client.overlay_visible
        if overlay_was_visible:
            overlay_client.hide_overlay()
            time.sleep(_OVERLAY_HIDE_DELAY)
    except Exception:
        pass

    try:
        if req.mode == "window":
            target_hwnd = req.hwnd
            if target_hwnd is None and req.window_title:
                win = _find_window(req.window_title, req.process_name)
                if not win:
                    raise HTTPException(status_code=404, detail=f"未找到窗口: {req.window_title}。推荐：用 list_windows 查看可用窗口")
                target_hwnd = win["hwnd"]
                captured_title = win["title"]
            elif target_hwnd:
                try:
                    import win32gui
                    captured_title = win32gui.GetWindowText(target_hwnd)
                except Exception:
                    captured_title = ""
            else:
                raise HTTPException(status_code=400, detail="mode=window 必须提供 window_title 或 hwnd")
            png_bytes = _capture_window(target_hwnd, force_fullscreen_crop=req.force_fullscreen_crop)
            if png_bytes is None:
                raise HTTPException(status_code=500, detail="窗口截图失败（可能被遮挡或最小化）")
            # 收集窗口几何用于 STALE_COORDINATES 检测
            window_rect_list: list[int] | None = None
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                window_rect_list = [int(left), int(top), int(right), int(bottom)]
            except Exception:
                window_rect_list = None
        else:
            png_bytes = _capture_fullscreen()
            captured_title = None
            target_hwnd = None
            # 全屏模式：记录虚拟屏几何
            try:
                off_x, off_y = _get_fullscreen_offset()
                # mss.monitors[0] 是虚拟屏，宽高需要从 monitors 取
                import mss
                with mss.mss() as sct:
                    vm = sct.monitors[0]
                    window_rect_list = [int(vm["left"]), int(vm["top"]),
                                        int(vm["left"] + vm["width"]), int(vm["top"] + vm["height"])]
            except Exception:
                window_rect_list = None

        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        orig_w, orig_h = img.size
        elapsed = int((time.perf_counter() - t0) * 1000)

        # 评估文档 P0：生成 snapshot_id 并记录元数据（供 execute_action 做 STALE_COORDINATES 检测）
        snapshot_id = uuid.uuid4().hex
        _record_snapshot(snapshot_id, {
            "captured_at": time.time(),
            "window_rect": tuple(window_rect_list) if window_rect_list else None,
            "window_title": captured_title,
            "hwnd": target_hwnd,
            "image_size": [orig_w, orig_h],
            "mode": req.mode,
        })

        if req.format == "path":
            # 保存到 temp/ 目录，返回路径（不污染上下文）
            import os
            import tempfile
            from datetime import datetime
            temp_dir = os.path.join(tempfile.gettempdir(), "localagent_captures")
            os.makedirs(temp_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            if captured_title:
                # 清理文件名中的非法字符
                safe_title = "".join(c for c in captured_title if c not in '<>:"/\\|?*')[:30]
                filename = f"cap_{timestamp}_{safe_title}.png"
            else:
                filename = f"cap_{timestamp}_fullscreen.png"
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, "wb") as f:
                f.write(png_bytes)
            return CaptureResponse(
                success=True, path=file_path, width=orig_w, height=orig_h,
                window_title=captured_title, elapsed_ms=elapsed,
                snapshot_id=snapshot_id, window_rect=window_rect_list,
            )
        elif req.format == "inline":
            # 等比缩放
            if max(orig_w, orig_h) > req.max_edge:
                scale = req.max_edge / max(orig_w, orig_h)
                new_w, new_h = int(orig_w * scale), int(orig_h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
            else:
                new_w, new_h = orig_w, orig_h
            # 转 JPEG
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=req.jpeg_quality, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return CaptureInlineResponse(
                mcp_image_block=True,
                image=b64,
                mime_type="image/jpeg",
                width=new_w,
                height=new_h,
                original_width=orig_w,
                original_height=orig_h,
                window_title=captured_title,
                elapsed_ms=elapsed,
                note=f"截图已压缩为 JPEG q={req.jpeg_quality} 最长边={req.max_edge}px，多模态 LLM 可直接看图。snapshot_id={snapshot_id[:8]}… 可传给 execute_action 做 STALE_COORDINATES 检测",
                snapshot_id=snapshot_id,
                window_rect=window_rect_list,
            )
        else:
            # format=base64，返回 PNG base64
            b64 = base64.b64encode(png_bytes).decode("ascii")
            return CaptureResponse(
                success=True, image=b64, width=orig_w, height=orig_h,
                window_title=captured_title, elapsed_ms=elapsed,
                snapshot_id=snapshot_id, window_rect=window_rect_list,
            )
    finally:
        # 截图后恢复覆盖层（ISSUE-005：保留 persistent 模式语义）
        if overlay_was_visible:
            _restore_overlay_after_capture()


class ScreenOcrRequest(BaseSchema):
    """截图+OCR 一体化请求（避免 base64 撑爆上下文）。

    截图来源与 /screen/capture 一致，OCR 引擎可选经典 PaddleOCR 或远程 VL。
    """
    mode: str | None = None  # fullscreen | window（None 时用 config default_mode）
    window_title: str | None = None
    process_name: str | None = None
    hwnd: int | None = None
    engine: str = "ocr"  # ocr（经典 PaddleOCR）| vl（远程 VL）
    max_height: int = 3000  # 长图分块最大高度（仅 engine=ocr 时生效）
    force_fullscreen_crop: bool = False


class ScreenOcrResponse(BaseSchema):
    success: bool
    text: str
    details: list[dict] = []
    image_size: list[int] = []
    window_rect: list[int] | None = None  # [left, top, right, bottom]；mode=window 时返回，供 agent 计算 screen_x = window_left + ocr_cx
    window_title: str | None = None
    warning: str | None = None  # 非致命警告（如 engine=vl 不返回 bbox）
    elapsed_ms: int


@router.post("/ocr", response_model=ScreenOcrResponse, operation_id="screen_ocr")
def screen_ocr(req: ScreenOcrRequest):
    """截图 + OCR 一体化端点（避免 base64 撑爆上下文）。

    一步完成截图+OCR，只返回 OCR 文本结果，不返回图片数据。
    engine=ocr: 经典 PaddleOCR（本地，速度快，Computer Use 文字定位首选）
    engine=vl:  远程 VL（ModelScope Qwen3-VL，适合复杂版面/手写）

    mode=window 时 details[].box 为窗口截图内坐标；加目标窗口 bbox 的
    left/top 后得到 execute_action 所需的屏幕物理像素坐标。
    """
    # mode 未指定时用 config default_mode
    if req.mode is None:
        from server.config import get_screen_config
        req.mode = get_screen_config().get("default_mode", "window")

    t0 = time.perf_counter()

    # 截图前隐藏覆盖层
    overlay_was_visible = False
    try:
        from server.overlay_client import overlay_client
        overlay_was_visible = overlay_client.overlay_visible
        if overlay_was_visible:
            overlay_client.hide_overlay()
            time.sleep(_OVERLAY_HIDE_DELAY)
    except Exception:
        pass

    try:
        # 1. 截图
        window_rect_for_response: list[int] | None = None
        if req.mode == "window":
            target_hwnd = req.hwnd
            if target_hwnd is None and req.window_title:
                win = _find_window(req.window_title, req.process_name)
                if not win:
                    raise HTTPException(status_code=404, detail=f"未找到窗口: {req.window_title}")
                target_hwnd = win["hwnd"]
                captured_title = win["title"]
            elif target_hwnd:
                try:
                    import win32gui
                    captured_title = win32gui.GetWindowText(target_hwnd)
                except Exception:
                    captured_title = ""
            else:
                raise HTTPException(status_code=400, detail="mode=window 必须提供 window_title 或 hwnd")
            png_bytes = _capture_window(target_hwnd, force_fullscreen_crop=req.force_fullscreen_crop)
            if png_bytes is None:
                raise HTTPException(status_code=500, detail="窗口截图失败（可能被遮挡或最小化）")
            # 提取窗口 rect 供 agent 计算屏幕坐标（screen_x = window_left + ocr_cx）
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                window_rect_for_response = [int(left), int(top), int(right), int(bottom)]
            except Exception:
                window_rect_for_response = None
        else:
            png_bytes = _capture_fullscreen()
            captured_title = None

        from PIL import Image
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        orig_w, orig_h = image.size

        # 2. OCR 识别
        warning_msg: str | None = None
        if req.engine == "vl":
            # 远程 VL
            from server.config import get_vision_config
            cfg = get_vision_config()
            if not cfg.get("vl_enabled", False):
                raise HTTPException(status_code=503, detail="远程 VL 未启用 (vl_enabled=false)")
            from server.vl.remote_vl import remote_vl
            remote_vl.reload_config()
            if not remote_vl.available:
                raise HTTPException(status_code=503, detail="远程 VL 不可用（所有 provider 冷却中或未配置）")
            result = remote_vl.ocr(image, use_case="vl_ocr")
            if result.get("status") != "ok":
                raise HTTPException(status_code=503, detail=result.get("detail", "远程 VL 识别失败"))
            text = result.get("markdown") or result.get("text") or ""
            blocks = result.get("blocks") or []
            details = [{"text": b.get("content", ""), "box": b.get("bbox", [])} for b in blocks if b.get("content")]
            # remote_vl.ocr() 当前不返回 blocks（只返回 markdown/text），details 永远为空
            # 明确提示 agent：engine=vl 不返回 bbox，定位应改用 engine=ocr
            if not details:
                warning_msg = "engine=vl 不返回 bbox，定位请用 engine=ocr；engine=vl 仅适合纯文本提取/版面理解"
        else:
            # 经典 PaddleOCR（复用 ocr 模块的 _do_ocr 逻辑）
            from server.ocr import _do_ocr
            ocr_resp = _do_ocr(image, max_height=req.max_height)
            text = ocr_resp.text
            details = ocr_resp.details

        elapsed = int((time.perf_counter() - t0) * 1000)
        return ScreenOcrResponse(
            success=True,
            text=text,
            details=details,
            image_size=[orig_w, orig_h],
            window_rect=window_rect_for_response,
            window_title=captured_title,
            warning=warning_msg,
            elapsed_ms=elapsed,
        )
    finally:
        # 截图后恢复覆盖层（ISSUE-005：保留 persistent 模式语义）
        if overlay_was_visible:
            _restore_overlay_after_capture()


@router.post("/overlay", response_model=OverlayResponse, operation_id="screen_overlay")
def screen_overlay(req: OverlayRequest):
    """显示/隐藏屏幕操作提示覆盖层（action=show|hide）"""
    try:
        from server.overlay_client import overlay_client
        if req.action == "hide":
            overlay_client.hide_overlay()
            return OverlayResponse(success=True, message="覆盖层已隐藏")
        # 显示前同步自动隐藏超时配置，确保 agent 显式调 /overlay 也能应用最新配置
        from server.config import get_screen_config
        overlay_client.set_overlay_auto_hide_seconds(
            get_screen_config().get("overlay_auto_hide_seconds", 30)
        )
        overlay_client.show_overlay(message=req.message, position=req.position)
        return OverlayResponse(success=True, message="覆盖层已显示")
    except Exception as e:
        return OverlayResponse(success=False, message=f"覆盖层操作失败: {e}")


@router.post("/confirm/start", response_model=ConfirmStartResponse, operation_id="confirm_start")
def confirm_start(req: ConfirmStartRequest):
    """Agent开始接管操作前的确认弹窗"""
    try:
        from server.overlay_client import overlay_client
        result = overlay_client.confirm_start(
            task_description=req.task_description,
            hotkey_hint=req.hotkey_hint,
        )
        return ConfirmStartResponse(status=result["status"])
    except Exception as e:
        # fail-closed：GUI 不可用时默认取消，避免 agent 在用户不知情下接管操作
        logger.warning(f"确认弹窗失败，默认取消: {e}")
        return ConfirmStartResponse(status="cancelled")


@router.post("/skip-cache/clear", operation_id="clear_skip_cache")
def clear_skip_cache():
    """清空自动跳过确认的坐标缓存"""
    clear_auto_skip_cache()
    return {"success": True, "message": "缓存已清空"}


# ========== 桌面快照（窗口 + OCR 文本） ==========

class SnapshotRequest(BaseSchema):
    with_ocr: bool = True  # 是否附带全屏 OCR 文本（首次可能慢 5-10s 加载模型）
    ocr_mode: str = "fullscreen"  # fullscreen | active_window
    max_windows: int = 30  # 最多返回多少个窗口（按 Z 序排序）
    include_minimized: bool = True  # 是否包含最小化窗口（默认 True，agent 需知道所有窗口状态）

@router.post("/snapshot", operation_id="screen_snapshot")
def screen_snapshot(req: SnapshotRequest):
    """桌面状态快照：窗口列表 + 全屏 OCR 文本。

    一次调用获取"现在屏幕上有什么"，避免 agent 多次 list_windows + capture + OCR 往返。
    返回纯文本，无 base64，不污染上下文。

    - with_ocr=True 时附带 OCR 文本（首次会触发模型加载）
    - ocr_mode=fullscreen 截全屏 OCR；active_window 只 OCR 当前前台窗口
    - 窗口按 Z 序排序，前 max_windows 个返回
    """
    if req.ocr_mode not in ("fullscreen", "active_window"):
        return {"success": False, "message": f"ocr_mode='{req.ocr_mode}' 不支持，可选值: fullscreen | active_window"}

    t0 = time.perf_counter()

    # 1. 窗口列表
    try:
        all_windows = _enum_windows()
    except Exception as e:
        return {"success": False, "message": f"窗口枚举失败: {e}"}

    # 过滤最小化
    if not req.include_minimized:
        all_windows = [w for w in all_windows if not w.get("is_minimized")]
    # 按 Z 序排序（z_order 越大越靠前）——其实 EnumWindows 已按 Z 序返回
    # 取前 N 个
    top_windows = all_windows[:req.max_windows]

    # 简化窗口信息（去掉大字段）
    win_list = []
    for w in top_windows:
        win_list.append({
            "hwnd": w["hwnd"],
            "title": w["title"],
            "pid": w.get("pid", 0),
            "process_name": w.get("process_name", ""),
            "bbox": w["bbox"],
            "is_minimized": w.get("is_minimized", False),
        })

    # 当前前台窗口
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_title = win32gui.GetWindowText(fg_hwnd)
    except Exception:
        fg_hwnd, fg_title = None, ""

    result = {
        "success": True,
        "foreground_hwnd": fg_hwnd,
        "foreground_title": fg_title,
        "windows_count": len(all_windows),
        "windows": win_list,
        "elapsed_ms": 0,  # 后面填
    }

    # 2. OCR 文本
    if req.with_ocr:
        try:
            # 截图前隐藏覆盖层
            overlay_was_visible = False
            try:
                from server.overlay_client import overlay_client
                overlay_was_visible = overlay_client.overlay_visible
                if overlay_was_visible:
                    overlay_client.hide_overlay()
                    time.sleep(_OVERLAY_HIDE_DELAY)
            except Exception:
                pass

            try:
                if req.ocr_mode == "active_window" and fg_hwnd:
                    png_bytes = _capture_window(fg_hwnd)
                else:
                    png_bytes = _capture_fullscreen()

                if png_bytes:
                    from PIL import Image
                    img = Image.open(io.BytesIO(png_bytes))

                    # 调用 OCR 模块（_do_ocr 内部会自动加载 PaddleOCR）
                    try:
                        from server.ocr import _do_ocr
                        ocr_resp = _do_ocr(img)
                        result["ocr_text"] = ocr_resp.text
                        result["ocr_details_count"] = len(ocr_resp.details or [])
                        result["ocr_image_size"] = list(img.size)
                    except Exception as e:
                        result["ocr_error"] = f"OCR 失败: {e}"
                        logger.warning(f"snapshot OCR 失败: {e}")
            finally:
                if overlay_was_visible:
                    _restore_overlay_after_capture()
        except Exception as e:
            result["ocr_error"] = f"OCR 准备失败: {e}"

    result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return result


# ========== P1-B：窗口生命周期 API（app_list/app_launch/app_wait/window_resolve + 窗口操作 + token 失效） ==========
# lifecycle 模块在文件顶部 import 块统一导入，避免 E402


# ========== P2-1：桌面事务（多步动作 + 声明式后验 + 失败回滚） ==========
# 第二轮评估 P2-1：把"截图-定位-点击-输入-提交-后验"这种多步原子操作包成一个事务。
# 失败时停止剩余步骤；rollback_policy=auto 时尝试执行 rollback_actions 回到操作前状态。
# 与 batch_actions 区别：
# - batch_actions 是"执行多个动作"，无事务语义（失败可选继续）
# - desktop_transaction 是"原子事务"，必须有 expected 后验，失败必须停止 + 可选回滚



# ========== 拆分端点注册 ==========
# 导入端点模块即触发 @router.post/@router.get 注册（模块级装饰器执行）。
# 必须在 router / 共享常量 / 共享 helper 全部定义完成后才能导入（下方所有端点
# 模块均 from server.screen.routes import router / 常量 / helper）。
# 同时 re-export 端点模块的模型与函数，保证 `from server.screen.routes import X`
# 与 `from server.screen import X`（经 __init__.py）向后兼容。
from . import action_endpoints as _action_endpoints  # noqa: E402,F401
from . import app_lessons as _app_lessons  # noqa: E402,F401
from . import batch_endpoints as _batch_endpoints  # noqa: E402,F401
from . import desktop_transaction_endpoints as _desktop_transaction_endpoints  # noqa: E402,F401
from . import lifecycle_endpoints as _lifecycle_endpoints  # noqa: E402,F401
from . import scroll_wait_analyze_endpoints as _scroll_wait_analyze_endpoints  # noqa: E402,F401
from . import uia_endpoints as _uia_endpoints  # noqa: E402,F401

# Re-export 模型与函数（保持 server.screen.routes 命名空间完整，__init__.py 兼容）
from .action_endpoints import (  # noqa: E402,F401
    ActionRequest,
    ActionResponse,
    FocusWindowRequest,
    _canonical_for_response,
    _verify_postcondition,
    execute_action,
    focus_window,
)
from .batch_endpoints import (  # noqa: E402,F401
    BatchActionItem,
    BatchActionsRequest,
    CaptureInlineResponse,
    PreviewActionRequest,
    PreviewActionResponse,
    PreviewPoint,
    batch_actions,
    preview_action,
)
from .desktop_transaction_endpoints import (  # noqa: E402,F401
    DesktopTransactionItem,
    DesktopTransactionItemResult,
    DesktopTransactionRequest,
    DesktopTransactionResponse,
    desktop_transaction,
)
from .lifecycle_endpoints import (  # noqa: E402,F401
    AppLaunchRequest,
    AppLaunchResponse,
    AppListResponse,
    AppWaitRequest,
    AppWaitResponse,
    WindowOpRequest,
    WindowOpResponse,
    WindowResolveRequest,
    WindowResolveResponse,
    _check_token_or_403,
    app_launch,
    app_list,
    app_wait,
    window_close,
    window_minimize,
    window_raise,
    window_resolve,
    window_restore,
)
from .scroll_wait_analyze_endpoints import (  # noqa: E402,F401
    AnalyzeRequest,
    ScrollCaptureRequest,
    ScrollCaptureResponse,
    WaitForRequest,
    WaitForResponse,
    screen_analyze,
    screen_wait_for,
    scroll_capture,
)
from .uia_endpoints import (  # noqa: E402,F401
    UiaActionRequest,
    UiaActionResponse,
    UiaElement,
    UiaSnapshotRequest,
    UiaSnapshotResponse,
    uia_action,
    uia_snapshot,
)

# ========== 启动/关闭钩子 ==========

def on_startup():
    """模块启动时调用"""
    emergency.start()
    logger.info("Screen模块已启动")


def on_shutdown():
    """模块关闭时调用"""
    emergency.stop()
    try:
        from server.overlay_client import overlay_client
        overlay_client.shutdown()
    except Exception:
        pass
    logger.info("Screen模块已关闭")


# ========== snake_case 兼容别名（已移除） ==========
# 主路径已统一为 kebab-case（/focus-window、/batch-actions、/scroll-capture、/wait-for）。
# 历史 snake_case 兼容别名（focus_window_legacy 等 4 个，include_in_schema=False）
# 已于 2026-08-01 移除：经全仓搜索无外部调用方，GATEWAY_EXCLUDE 中对应 _legacy 条目同步清理。
