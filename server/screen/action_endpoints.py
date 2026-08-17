"""action 端点 — 键鼠动作执行 + 焦点激活。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router / 常量 / takeover helper / snapshot 元数据缓存仍由 routes 提供，
本模块只定义：
- ActionRequest / ActionResponse / FocusWindowRequest 模型
- _canonical_for_response / _verify_postcondition 共享 helper（被其他端点模块复用）
- execute_action（/action）端点
- focus_window（/focus-window）端点

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import base64
import io
import time

from lib.schema import BaseSchema

# 截图子模块
from server.screen.capture import (
    _capture_fullscreen,
    _capture_window,
    _get_fullscreen_offset,
)

# 焦点安全子模块
from server.screen.focus import (
    EXECUTED_UNVERIFIED,
    FOCUS_LEAK_PREVENTED,
    FOCUS_NOT_VERIFIED,
    STALE_COORDINATES,
    collect_focus_evidence,
    resolve_canonical_window,
    verify_focus_for_input,
)

# 键鼠操控子模块
from server.screen.input import (
    _execute_action,
    _validate_action_params,
)

# 从主模块复用共享件（router / 日志 / 常量 / takeover helper / snapshot 元数据缓存）
from server.screen.routes import (
    _ACTION_SETTLE_DELAY,
    _COORDINATE_ACTIONS,
    _FOCUS_RETRY_DELAY,
    _KEYBOARD_ACTIONS,
    _enforce_session_permission,
    _ensure_takeover_approved,
    _task_authorization_is_active,
    _touch_task_authorization_after_delivery,
    _verify_snapshot_freshness,
    logger,
    router,
)

# 安全子模块
from server.screen.security import (
    _auto_skip_coords,
    _check_danger,
    _check_window_bounds,
    _record_skip,
    emergency,
)

# 窗口子模块
from server.screen.windows import (
    _ADMIN_STATUS,
    _enum_windows,
    _find_window,
    _force_focus_window,
)

# ========== Pydantic 模型 ==========

class ActionRequest(BaseSchema):
    action: str  # click | double_click | right_click | type | type_immediate | hotkey | scroll | drag
    x: int | None = None
    y: int | None = None
    text: str | None = None
    element_text: str | None = None  # 目标元素的文字描述，用于安全检查
    keys: list[str] | None = None
    direction: str | None = "down"
    amount: int | None = 3
    dx: int | None = 0
    dy: int | None = 0
    window_title: str | None = None
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突，如"qbittorrent.exe"）
    hwnd: int | None = None  # 直接指定窗口句柄（避免标题歧义）
    require_confirm: bool | None = None  # None 时用 config require_confirm_by_default
    activate_window: bool = True  # 操作前是否自动激活目标窗口（默认 True，避免焦点丢失）
    screenshot_after: bool | None = None  # [已废弃] 不再返回 base64 截图。如需截图请用 capture_screen(format=inline)；如需画面反馈请用 verify_prompt
    verify_prompt: str | None = None  # 期望状态描述；非空则操作后截图+调 VL 返回 ≤200 字画面描述，无论成败都反馈（batch_actions 不支持此字段）
    # 评估文档 P0：焦点安全 + 坐标绑定 snapshot + 声明式后验
    allow_unfocused_input: bool | None = None  # None 时用 config allow_unfocused_input_default；键盘动作未验证焦点时是否放行（默认 False 强校验）
    snapshot_id: str | None = None  # 截图快照 ID（来自 capture_screen 返回值）；坐标动作校验窗口几何是否变化（STALE_COORDINATES）
    expected: dict | None = None  # 声明式后验：{"type": "ocr_contains"|"ocr_not_contains", "text": "...", "mode"?: "fullscreen"|"window", "window_title"?: str, "timeout"?: float}
    # 第二轮评估 P1-1：dry-run 模式（不真发键鼠，返回校验结果：focus_check / snapshot 新鲜度 / 坐标范围）
    dry_run: bool = False  # True 时通过所有校验后直接返回 status="dry_run", transport_status="not_sent"，不投递任何事件
    # 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许接管。
    # 仅在 screen.takeover_confirm_enabled=true 且 overlay 未显示时触发；
    # 用户拒绝或超时 cancel 时返回 status="cancelled"，用户允许或超时 proceed 时进入正常流程。
    task_description: str | None = None

class ActionResponse(BaseSchema):
    success: bool
    status: str  # executed | executed_unverified | postcondition_failed | confirmed | cancelled | blocked | emergency_stopped | dry_run
    auto_confirmed: bool = False
    user_set_same_coords_skip: bool = False
    user_reason: str = ""  # 用户在确认窗口输入的反馈(取消时可能填写原因)
    message: str
    # VL 反馈闭环字段（仅当请求带 verify_prompt 时填充，否则全 None；batch_actions 响应始终为 None）
    # 注：已不再返回 base64 截图（screenshot_after 字段已移除，原 base64 会撑爆 LLM 上下文）。
    # 如需查看操作后截图，请用 capture_screen(format="inline") 单独获取（返回 ImageContent）。
    # 如需语义化画面反馈，请传 verify_prompt（服务端自动截图+调 VL，返回 ≤200 字描述）。
    vl_description: str | None = None  # ≤200 字画面描述（状态/观察/建议）
    vl_skipped: bool | None = None  # True=跳过 VL（config 关闭/截图失败/VL 不可用）
    vl_skip_reason: str | None = None  # 跳过原因
    # 评估文档 P0：焦点证据 + 分层状态
    focus_check_status: str | None = None  # focus_verified | unfocused_input_allowed | FOCUS_NOT_VERIFIED | FOCUS_LEAK_PREVENTED | skipped（非键盘动作跳过）
    transport_status: str | None = None  # sent（已发送键鼠）| not_sent（焦点/坐标校验拦截，零按键）| error（_execute_action 异常）
    delivery_status: str | None = None  # delivered（焦点仍在目标族）| leaked（焦点漂移到非目标）| unknown（未指定 target）| skipped（动作未发送）
    postcondition_status: str | None = None  # verified（expected 检查通过）| failed（expected 检查失败）| error（检查异常）| executed_unverified（未声明 expected）| not_checked（动作未发送）
    foreground_before: dict | None = None  # 操作前焦点证据：{hwnd, title, pid, process_name}
    foreground_after: dict | None = None  # 操作后焦点证据
    target_match_before: bool | None = None  # 操作前前台是否属于目标 family
    target_match_after: bool | None = None  # 操作后前台是否属于目标 family
    canonical_window: dict | None = None  # canonical window token：{canonical_hwnd, canonical_title, canonical_pid, canonical_process_name, is_uwp_host}

class FocusWindowRequest(BaseSchema):
    window_title: str | None = None
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突）
    hwnd: int | None = None
    task_description: str | None = None  # 接管确认弹窗中显示的任务描述（overlay 未显示时）


# ========== 共享 helper（被 batch/uia/desktop_transaction 端点复用） ==========

def _canonical_for_response(canonical: dict | None) -> dict | None:
    """从 resolve_canonical_window 的完整返回中提取响应字段（去掉 family_hwnds 大列表）。"""
    if not canonical:
        return None
    return {
        "canonical_hwnd": canonical.get("canonical_hwnd"),
        "canonical_title": canonical.get("canonical_title", ""),
        "canonical_pid": canonical.get("canonical_pid", 0),
        "canonical_process_name": canonical.get("canonical_process_name", ""),
        "is_uwp_host": canonical.get("is_uwp_host", False),
    }


def _verify_postcondition(expected: dict | None, pil_image) -> str:
    """评估文档 P0：声明式 expected 后验。

    expected 格式（仅支持以下两个字段，其他字段如 mode/window_title/timeout 未实现）：
        {"type": "ocr_contains" | "ocr_not_contains", "text": "..."}

    注意：本函数基于操作后已截好的 pil_image 做一次性 OCR 检查，
    不支持重试/超时/切换截图模式。需要轮询后验请用 screen_wait_for 端点。

    Returns:
        "verified" — 条件满足（ocr_contains 找到 / ocr_not_contains 未找到）
        "failed"   — 条件未满足
        "error"    — OCR 检查异常
        "executed_unverified" — 未声明 expected
        "not_checked" — 动作未发送（pil_image 为 None 但 expected 提供了，无法后验）
    """
    if expected is None:
        return "executed_unverified"

    if pil_image is None:
        return "not_checked"

    exp_type = expected.get("type", "")
    exp_text = expected.get("text", "")
    if not exp_text:
        return "error"
    if exp_type not in ("ocr_contains", "ocr_not_contains"):
        return "error"

    try:
        from server.ocr import _do_ocr
        ocr_resp = _do_ocr(pil_image)
        ocr_text = ocr_resp.text or ""
        if exp_type == "ocr_contains":
            return "verified" if exp_text in ocr_text else "failed"
        else:  # ocr_not_contains
            return "verified" if exp_text not in ocr_text else "failed"
    except Exception as e:
        logger.warning(f"expected 后验 OCR 失败: {e}")
        return "error"


# ========== 端点 ==========

@router.post("/action", response_model=ActionResponse, operation_id="execute_action")
def execute_action(req: ActionRequest):
    """执行键鼠操作（含安全检查和确认机制）

    评估文档 P0 新约束：
    - 键盘动作（type/type_immediate/hotkey）必须通过 verify_focus_for_input；
      FOCUS_LEAK_PREVENTED（前台是 ChatGPT/Codex/Trae/Cursor 等受保护进程而目标不是它）
      和 FOCUS_NOT_VERIFIED（未指定目标且未启用 allow_unfocused_input）时零按键发送。
    - 坐标动作（click/double_click/right_click/scroll/drag）若提供 snapshot_id，
      会校验窗口几何是否变化，变化则返回 STALE_COORDINATES（status=blocked）。
    - 响应包含分层状态：transport（是否发送键鼠）/ delivery（焦点是否仍在目标族）/
      postcondition（OCR 声明式后验是否通过）。
    - 未声明 expected 时，status="executed_unverified"（不再是 "executed"）。

    Tip — 操作前建议先调 screen_match_app(process_name=...) 查询该软件的经验
    （UIA 友好度/快捷键/菜单路径/已知坑），避免重复踩坑；新踩坑可用 screen_write_lesson
    写入 apps/<process_name>.md 供下次使用。
    """

    # 0. 会话权限检查（T14：副作用端点 403 拦截，无权限时 raise HTTPException(403)）
    # 放在最前面——无权限 = 无入口，emergency/参数检查均无需进入。
    # emergency 触发会同步 release session 到 no_permission，所以 Ctrl+` 后此处直接 403；
    # 若用户立刻重新授权，emergency cooldown 仍能在 step 1 拦截。
    session_status = _enforce_session_permission()
    current_mode = session_status.get("mode", "normal")

    # 0.1 config 接线
    from server.config import get_screen_config
    screen_cfg = get_screen_config()
    explicit_confirm = req.require_confirm is True
    if req.require_confirm is None:
        req.require_confirm = screen_cfg.get("require_confirm_by_default", True)
    if req.allow_unfocused_input is None:
        req.allow_unfocused_input = screen_cfg.get("allow_unfocused_input_default", False)
    focus_protection_enabled = screen_cfg.get("focus_protection_enabled", True)
    protected_processes = screen_cfg.get("protected_processes", None)  # None → 用默认

    # 1. 紧急停止检查
    if not emergency.can_operate():
        return ActionResponse(
            success=False, status="emergency_stopped",
            message=f"紧急停止已触发，请等待{emergency.cooldown_seconds}秒冷却",
        )

    # 1.5 防呆参数检查
    ok, errmsg = _validate_action_params(
        req.action, req.x, req.y, req.text, req.keys,
        direction=req.direction, amount=req.amount,
        dx=req.dx, dy=req.dy,
    )
    if not ok:
        return ActionResponse(success=False, status="blocked", message=errmsg)

    # 1.6 窗口未指定时的警告（不阻止操作，但在 message 中提示）
    window_warning = ""
    if not req.window_title and not req.hwnd and req.x is not None and req.y is not None:
        window_warning = " [警告] 未指定 window_title/hwnd，坐标可能落到错误窗口。推荐：设置 window_title 确保操作发到正确窗口，或先 focus_window 激活目标窗口"

    # 2. 窗口范围检查（优先用 hwnd 找窗口，其次用 window_title）
    target_hwnd = req.hwnd
    target_title = req.window_title
    if target_hwnd is None and target_title:
        win = _find_window(target_title, req.process_name)
        if win:
            target_hwnd = win["hwnd"]
    if target_hwnd and req.x is not None and req.y is not None:
        try:
            import win32gui
            left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
            if not (left <= req.x <= right and top <= req.y <= bottom):
                return ActionResponse(
                    success=False, status="blocked",
                    message=f"坐标 ({req.x}, {req.y}) 不在窗口 hwnd={target_hwnd} 范围内 ({left},{top},{right},{bottom})",
                )
        except Exception as e:
            logger.warning(f"窗口范围检查失败 hwnd={target_hwnd}: {e}")
            return ActionResponse(
                success=False, status="blocked",
                message=f"无法验证窗口 hwnd={target_hwnd} 的范围: {e}。窗口可能已关闭，请重新调用 list_windows 获取有效 hwnd",
            )
    elif target_title and req.x is not None and req.y is not None:
        if not _check_window_bounds(target_title, req.x, req.y, req.process_name):
            return ActionResponse(
                success=False, status="blocked",
                message=f"坐标 ({req.x}, {req.y}) 不在窗口 '{target_title}' 范围内",
            )

    # 2.5 评估文档 P0：坐标动作的 snapshot 新鲜度检查（STALE_COORDINATES）
    if req.action in _COORDINATE_ACTIONS and req.snapshot_id:
        fresh, stale_reason, _meta = _verify_snapshot_freshness(req.snapshot_id, target_hwnd)
        if not fresh:
            return ActionResponse(
                success=False, status="blocked",
                message=f"{STALE_COORDINATES}: {stale_reason}。请重新 capture_screen 获取最新 snapshot_id 后再定位坐标",
                focus_check_status="skipped",
                transport_status="not_sent",
                delivery_status="skipped",
                postcondition_status="not_checked",
            )

    # 3. 危险关键词检查
    danger_level = _check_danger(req.text, req.keys, req.element_text)
    if danger_level == "block":
        return ActionResponse(
            success=False, status="blocked",
            message="操作被安全策略拦截（包含危险关键词）",
        )

    # 3.5 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许 agent 接管键鼠操作
    # 放在 danger_keywords 之后、require_confirm 之前——纯校验先 fail-fast，
    # 用户授权后再进入 require_confirm 流程（避免用户拒绝接管却仍弹 require_confirm）
    approved, takeover_reason = _ensure_takeover_approved(screen_cfg, req.task_description)
    if not approved:
        msg = "用户拒绝 agent 接管键鼠操作"
        if takeover_reason:
            msg += f"。用户反馈: {takeover_reason}"
        return ActionResponse(
            success=False, status="cancelled",
            user_reason=takeover_reason,
            message=msg,
        )

    # 4. 自动跳过确认检查
    auto_skip = False
    if req.x is not None and req.y is not None:
        skip_key = (req.action, req.x, req.y)
        if (
            _auto_skip_coords.get(skip_key, False)
            and danger_level == "safe"
            and not explicit_confirm
        ):
            auto_skip = True
            req.require_confirm = False

    # 5. 确认流程
    user_set_skip = False
    if danger_level == "confirm":
        # T14（决策 9）：watchdog 模式下 danger_level=confirm 自动跳过（用户已显式授权强任务）
        # normal 模式仍强制弹窗确认；explicit_confirm=True 时尊重调用方意愿强制确认
        if current_mode == "watchdog" and not explicit_confirm:
            req.require_confirm = False
        else:
            req.require_confirm = True  # normal 模式或显式确认强制弹窗
    elif _task_authorization_is_active() and not explicit_confirm:
        req.require_confirm = False

    if req.require_confirm and not auto_skip:
        # 截图用于红框标注
        try:
            png_bytes = _capture_fullscreen()
            screenshot_b64 = base64.b64encode(png_bytes).decode("ascii")
        except Exception:
            screenshot_b64 = None

        # 计算截图内画点坐标（双屏修正：mss.monitors[0] 左上角可能非零）
        draw_x, draw_y = None, None
        if req.x is not None and req.y is not None:
            off_x, off_y = _get_fullscreen_offset()
            draw_x = req.x - off_x
            draw_y = req.y - off_y

        # 弹出确认窗口
        try:
            from server.overlay_client import overlay_client
            result = overlay_client.confirm_action(
                screenshot_b64=screenshot_b64,
                action=req.action,
                x=req.x, y=req.y,  # 显示给用户的原始坐标
                draw_x=draw_x, draw_y=draw_y,  # 截图内画点坐标（双屏修正）
                text=req.text, keys=req.keys,
            )
            if result["status"] == "cancelled":
                user_reason = result.get("reason", "")
                msg = "用户取消了操作"
                if user_reason:
                    msg += f"。用户反馈: {user_reason}"
                return ActionResponse(
                    success=False, status="cancelled",
                    user_reason=user_reason,
                    message=msg,
                )
            user_set_skip = result.get("same_coords_skip", False)
            if user_set_skip and req.x is not None and req.y is not None:
                skip_key = (req.action, req.x, req.y)
                _record_skip(skip_key)
        except Exception as e:
            logger.warning(f"确认窗口调用失败，默认取消操作: {e}")
            return ActionResponse(
                success=False, status="cancelled",
                message=f"确认窗口不可用: {e}",
            )

    # 6. 操作前显示/刷新覆盖层（重置自动隐藏计时器）
    try:
        from server.overlay_client import overlay_client
        if not overlay_client.overlay_visible:
            overlay_client.set_overlay_auto_hide_seconds(
                screen_cfg.get("overlay_auto_hide_seconds", 30)
            )
            overlay_client.show_overlay()
        else:
            overlay_client.poke_overlay()
    except Exception:
        pass

    # 7. 操作前激活窗口（默认 True；只有显式 activate_window=False 才跳过）
    focus_warning = ""
    if req.activate_window and target_hwnd:
        try:
            _force_focus_window(target_hwnd)
            time.sleep(_FOCUS_RETRY_DELAY)
            # 焦点检查：激活后前台仍非目标窗口则重试一次
            import win32gui
            if win32gui.GetForegroundWindow() != target_hwnd:
                _force_focus_window(target_hwnd)
                time.sleep(_FOCUS_RETRY_DELAY)
                if win32gui.GetForegroundWindow() != target_hwnd:
                    fg_title = win32gui.GetWindowText(win32gui.GetForegroundWindow()) or "未知"
                    focus_warning = f" [警告] 焦点被模态窗口抢占（当前前台: '{fg_title}'），操作可能发到错误窗口"
        except Exception as e:
            logger.warning(f"激活窗口失败 hwnd={target_hwnd}: {e}")

    # 7.5 评估文档 P0：操作前焦点证据 + 键盘动作焦点强校验
    evidence_before = collect_focus_evidence(target_hwnd) if focus_protection_enabled else None
    canonical_info = None
    if target_hwnd and focus_protection_enabled:
        try:
            canonical_info = resolve_canonical_window(target_hwnd)
        except Exception as e:
            logger.debug(f"resolve_canonical_window 失败 hwnd={target_hwnd}: {e}")

    focus_check_status = "skipped"  # 默认：非键盘动作或 focus_protection 关闭
    if focus_protection_enabled and req.action in _KEYBOARD_ACTIONS:
        ok, reason, evidence_before = verify_focus_for_input(
            target_hwnd,
            allow_unfocused_input=req.allow_unfocused_input,
            protected_processes=protected_processes,
            evidence=evidence_before,
        )
        focus_check_status = reason
        if not ok:
            # 零按键发送（FOCUS_LEAK_PREVENTED / FOCUS_NOT_VERIFIED）
            fg_info = evidence_before.get("foreground", {}) or {}
            leak_msg = (
                f"{focus_check_status}: 目标 hwnd={target_hwnd}，"
                f"前台 hwnd={fg_info.get('hwnd')} title='{fg_info.get('title','')}' "
                f"process={fg_info.get('process_name','')}。键盘动作未发送（零按键）。"
            )
            if focus_check_status == FOCUS_LEAK_PREVENTED:
                leak_msg += " 前台是受保护进程（agent 宿主），即使 allow_unfocused_input=true 也不放行。请先 focus_window 激活目标窗口。"
            elif focus_check_status == FOCUS_NOT_VERIFIED:
                leak_msg += " 未验证焦点（未指定 target 且 allow_unfocused_input=False）。请显式传 hwnd/window_title，或显式设置 allow_unfocused_input=true 由调用方承担风险。"
            return ActionResponse(
                success=False, status="blocked",
                message=leak_msg,
                focus_check_status=focus_check_status,
                transport_status="not_sent",
                delivery_status="skipped",
                postcondition_status="not_checked",
                foreground_before=fg_info,
                target_match_before=evidence_before.get("target_match"),
                canonical_window=_canonical_for_response(canonical_info),
            )

    # 7.6 第二轮评估 P1-1：dry-run 模式（不投递键鼠，返回完整校验结果）
    # 通过所有前置校验后立即返回，让 agent 程序化预演动作效果（焦点/坐标/snapshot 新鲜度）
    if req.dry_run:
        fg_info = evidence_before.get("foreground") if evidence_before else None
        return ActionResponse(
            success=True,
            status="dry_run",
            message=(
                f"dry_run=true：已通过所有前置校验（焦点={focus_check_status}，"
                f"坐标范围=ok，snapshot 新鲜度=ok），未投递任何键鼠事件。"
                f"如需真实执行请传 dry_run=false。"
            ),
            focus_check_status=focus_check_status,
            transport_status="not_sent",
            delivery_status="skipped",
            postcondition_status="not_checked",
            foreground_before=fg_info,
            target_match_before=evidence_before.get("target_match") if evidence_before else None,
            canonical_window=_canonical_for_response(canonical_info),
        )

    # 8. 执行操作
    transport_status = "sent"
    try:
        result = _execute_action(
            action=req.action, x=req.x, y=req.y,
            text=req.text, keys=req.keys,
            direction=req.direction, amount=req.amount,
            dx=req.dx, dy=req.dy,
        )
    except Exception as e:
        logger.exception(f"_execute_action 异常: {e}")
        # 第二轮评估 P0-3：动作后刷新 canonical_window（即使异常也要刷新）
        canonical_info_after = None
        if target_hwnd and focus_protection_enabled:
            try:
                canonical_info_after = resolve_canonical_window(target_hwnd)
            except Exception:
                canonical_info_after = canonical_info
        return ActionResponse(
            success=False, status="blocked",
            message=f"键鼠执行异常: {e}",
            focus_check_status=focus_check_status,
            transport_status="error",
            delivery_status="skipped",
            postcondition_status="not_checked",
            foreground_before=evidence_before.get("foreground") if evidence_before else None,
            target_match_before=evidence_before.get("target_match") if evidence_before else None,
            canonical_window=_canonical_for_response(canonical_info_after or canonical_info),
        )

    # 9. 操作后焦点证据 + delivery 判定
    evidence_after = collect_focus_evidence(target_hwnd) if focus_protection_enabled else None
    delivery_status = "unknown"
    target_match_after = None
    if evidence_after is not None:
        target_match_after = evidence_after.get("target_match")
        if target_hwnd is None:
            delivery_status = "unknown"
        elif target_match_after:
            delivery_status = "delivered"
        else:
            delivery_status = "leaked"

    # 9.4 第二轮评估 P1-2：动作后刷新 canonical_window（不保留点击前旧标题）
    canonical_info_after = canonical_info  # 默认沿用动作前
    if target_hwnd and focus_protection_enabled:
        try:
            canonical_info_after = resolve_canonical_window(target_hwnd)
        except Exception as e:
            logger.debug(f"动作后 resolve_canonical_window 失败 hwnd={target_hwnd}: {e}")

    # 9.5 操作后截图（仅当 verify_prompt 或 expected 需要时采集）
    # 注：不再返回 base64 截图（原 screenshot_after 字段会撑爆 LLM 上下文）。
    #     screenshot_after 请求参数已废弃——如需操作后截图请用 capture_screen(format="inline")。
    #     verify_prompt 提供语义化 VL 反馈（≤200字描述），优于原始 base64。
    vl_pil_image = None  # 给 VL 反馈用（不进响应，服务端调完 VL 即丢弃）
    postcond_pil_image = None  # 给 expected OCR 后验用
    if req.verify_prompt or req.expected:
        try:
            time.sleep(_ACTION_SETTLE_DELAY)  # 等待界面响应
            # 优先窗口截图（与 computer_use.md "窗口截图优先" 铁律一致），否则全屏
            shot_bytes = None
            if req.hwnd:
                shot_bytes = _capture_window(req.hwnd)
            elif req.window_title:
                win = _find_window(req.window_title, req.process_name)
                if win:
                    shot_bytes = _capture_window(win["hwnd"])
            if shot_bytes is None:
                shot_bytes = _capture_fullscreen()
            from PIL import Image
            pil_img = Image.open(io.BytesIO(shot_bytes))
            if req.verify_prompt:
                vl_pil_image = pil_img
            if req.expected:
                postcond_pil_image = pil_img
        except Exception as e:
            logger.warning(f"操作后截图失败: {e}")

    # 9.6 VL 反馈闭环（仅当 verify_prompt 非空；操作失败时仍调用，这是核心价值）
    vl_desc, vl_skipped, vl_skip_reason = None, None, None
    if req.verify_prompt:
        if not screen_cfg.get("feedback_vl_enabled", True):
            vl_skipped, vl_skip_reason = True, "feedback_vl_enabled=false（config 关闭）"
        elif vl_pil_image is None:
            vl_skipped, vl_skip_reason = True, "截图未生成（操作前页面可能已不可用）"
        else:
            try:
                from server.vl.feedback_vl import describe_after_action
                vl_result = describe_after_action(
                    vl_pil_image, req.verify_prompt, result["success"], result["message"],
                )
                if vl_result["status"] == "ok":
                    vl_desc = vl_result["description"]
                else:
                    vl_skipped, vl_skip_reason = True, vl_result["reason"]
            except Exception as e:
                logger.warning(f"VL 反馈异常: {e}")
                vl_skipped, vl_skip_reason = True, f"VL 反馈异常: {e}"

    # 9.7 评估文档 P0：声明式 expected 后验（OCR contains / not_contains）
    postcondition_status = _verify_postcondition(req.expected, postcond_pil_image)

    # 10. 组装响应
    # 第二轮评估 P0-3：明确区分"动作执行失败"与"后验未通过"
    # - blocked: 动作本身执行失败（_execute_action 返回 success=False）
    # - postcondition_failed: 动作已成功执行但后验 OCR 未通过（不应与 blocked 混淆）
    # - executed_unverified: 动作已执行但未做/无法做后验（expected=None / 截图失败 / OCR 异常）
    # - executed: 动作已执行且后验通过
    if not result["success"]:
        status = "blocked"
    elif postcondition_status == "verified":
        status = "executed"
    elif postcondition_status == "failed":
        status = "postcondition_failed"  # 第二轮 P0-3：明确区分动作成功但后验失败
    elif postcondition_status == "executed_unverified":
        # 评估文档 P0：未声明 expected 时返回 executed_unverified（不再伪称 success）
        status = EXECUTED_UNVERIFIED
    elif postcondition_status == "error":
        status = EXECUTED_UNVERIFIED  # 后验异常，按未验证处理（动作已发送）
    elif postcondition_status == "not_checked":
        # 第二轮 P0-3：声明了 expected 但截图失败导致后验未做——动作已发送，按未验证处理
        status = EXECUTED_UNVERIFIED
    else:
        status = EXECUTED_UNVERIFIED
    if req.require_confirm and not auto_skip and result["success"]:
        # 走过确认流程且动作成功：保留 confirmed 语义（向后兼容）
        if postcondition_status == "executed_unverified":
            status = "confirmed"
        elif postcondition_status == "verified":
            status = "executed"
    if auto_skip and result["success"] and postcondition_status == "executed_unverified":
        status = EXECUTED_UNVERIFIED

    fg_before = evidence_before.get("foreground") if evidence_before else None
    fg_after = evidence_after.get("foreground") if evidence_after else None

    _touch_task_authorization_after_delivery(
        success=bool(result["success"]),
        delivery_status=delivery_status,
    )

    return ActionResponse(
        success=result["success"],
        status=status,
        auto_confirmed=auto_skip,
        user_set_same_coords_skip=user_set_skip,
        message=result["message"] + window_warning + focus_warning,
        vl_description=vl_desc,
        vl_skipped=vl_skipped,
        vl_skip_reason=vl_skip_reason,
        focus_check_status=focus_check_status,
        transport_status=transport_status,
        delivery_status=delivery_status,
        postcondition_status=postcondition_status,
        foreground_before=fg_before,
        foreground_after=fg_after,
        target_match_before=evidence_before.get("target_match") if evidence_before else None,
        target_match_after=target_match_after,
        canonical_window=_canonical_for_response(canonical_info_after),
    )


# ========== 焦点激活 ==========

@router.post("/focus-window", operation_id="focus_window")
def focus_window(req: FocusWindowRequest):
    """强力激活指定窗口到前台。

    Windows UIPI 默认阻止 SetForegroundWindow，本端点组合 AttachThreadInput +
    BringWindowToTop + WScript.Shell.AppActivate 三套机制确保窗口真正获得焦点。
    在 execute_action 前调用此端点可避免键鼠发到错误窗口。

    优先用 hwnd（精确），其次用 window_title（模糊匹配）。

    Tip — 激活前可先调 screen_match_app 查该软件经验，了解 UIA 友好度和已知坑。
    """
    if not _ADMIN_STATUS:
        return {"success": False, "message": "需要管理员权限才能激活窗口"}

    # T14：副作用端点 403 拦截（focus_window 强抢前台焦点，属副作用操作）
    _enforce_session_permission()

    # 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许 agent 接管键鼠操作
    # focus_window 会强行抢占前台焦点，与用户争夺输入，故纳入保护范围
    from server.config import get_screen_config
    screen_cfg = get_screen_config()
    approved, takeover_reason = _ensure_takeover_approved(screen_cfg, req.task_description)
    if not approved:
        msg = "用户拒绝 agent 接管键鼠操作"
        if takeover_reason:
            msg += f"。用户反馈: {takeover_reason}"
        return {
            "success": False, "status": "cancelled",
            "user_reason": takeover_reason,
            "message": msg,
        }

    target_hwnd = req.hwnd
    if target_hwnd is None and req.window_title:
        win = _find_window(req.window_title, req.process_name)
        if not win:
            # 推荐：列出相似窗口或提示用 list_windows
            all_wins = _enum_windows()
            similar = [w["title"] for w in all_wins if req.window_title.lower() in w["title"].lower()][:5]
            hint = f" 可用 list_windows 查看所有窗口。相似标题: {similar}" if similar else " 请用 list_windows 查看可用窗口列表"
            return {"success": False, "message": f"未找到窗口: '{req.window_title}'。{hint}"}
        target_hwnd = win["hwnd"]
        matched_title = win["title"]
    else:
        matched_title = ""
        if target_hwnd:
            try:
                import win32gui
                matched_title = win32gui.GetWindowText(target_hwnd)
            except Exception:
                pass

    if not target_hwnd:
        return {"success": False, "message": "必须提供 window_title 或 hwnd 参数。推荐：用 list_windows 获取 hwnd 或 title，再调用 focus_window"}

    ok = _force_focus_window(target_hwnd)
    _touch_task_authorization_after_delivery(success=bool(ok))
    # 返回当前前台 hwnd 供 agent 验证
    try:
        import win32gui
        cur_fg = win32gui.GetForegroundWindow()
        cur_title = win32gui.GetWindowText(cur_fg)
    except Exception:
        cur_fg, cur_title = None, ""

    return {
        "success": ok,
        "target_hwnd": target_hwnd,
        "target_title": matched_title,
        "foreground_hwnd": cur_fg,
        "foreground_title": cur_title,
        "message": "焦点激活成功" if ok else "焦点激活失败（窗口可能被系统模态对话框遮挡）",
    }
