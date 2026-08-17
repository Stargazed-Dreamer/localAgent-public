"""batch / preview 端点 — 批量键鼠操作 + 点击位置预览。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router / 常量 / takeover helper / snapshot 元数据缓存仍由 routes 提供，
_canonical_for_response / _verify_postcondition 由 action_endpoints 提供。
本模块定义：
- BatchActionItem / BatchActionsRequest / CaptureInlineResponse 模型
- PreviewPoint / PreviewActionRequest / PreviewActionResponse 模型
- batch_actions（/batch-actions）端点
- preview_action（/preview/action）端点

注：CaptureInlineResponse 也被 routes.py 的 /capture 端点使用——通过 routes.py
末尾的 re-export 块回到 routes.py 命名空间，/capture 调用时按模块全局解析即可命中。

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import base64
import io
import time
import uuid

from fastapi import HTTPException

from lib.schema import BaseSchema

# 从 action_endpoints 复用共享 helper（routes.py 导入 action_endpoints 在 batch 之前，已就绪）
from server.screen.action_endpoints import (
    _canonical_for_response,
    _verify_postcondition,
)

# 截图子模块
from server.screen.capture import (
    _capture_fullscreen,
    _capture_window,
)

# 焦点安全子模块
from server.screen.focus import (
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
    _OVERLAY_HIDE_DELAY,
    _confirm_control_operation,
    _enforce_session_permission,
    _ensure_takeover_approved,
    _touch_task_authorization_after_delivery,
    _verify_snapshot_freshness,
    logger,
    router,
)

# 安全子模块
from server.screen.security import (
    _check_danger,
    emergency,
)

# 窗口子模块
from server.screen.windows import (
    _ADMIN_STATUS,
    _find_window,
    _force_focus_window,
)

# ========== 批量键鼠操作 ==========

class BatchActionItem(BaseSchema):
    action: str  # click | double_click | right_click | type | type_immediate | hotkey | scroll | drag | wait
    x: int | None = None
    y: int | None = None
    text: str | None = None
    keys: list[str] | None = None
    direction: str | None = "down"
    amount: int | None = 3
    dx: int | None = 0
    dy: int | None = 0
    wait: float | None = 0.0  # action="wait" 时的等待秒数；其他 action 后的额外延迟
    element_text: str | None = None  # 安全检查用
    snapshot_id: str | None = None  # 评估文档 P0：坐标动作的截图 snapshot_id（STALE_COORDINATES 检测）
    # 第二轮评估 P1：批量动作每步支持声明式后验（type=ocr_contains/ocr_not_contains）
    expected: dict | None = None  # {"type": "ocr_contains", "text": "...", "mode"?: ..., "window_title"?: ...}

class BatchActionsRequest(BaseSchema):
    actions: list[BatchActionItem]
    window_title: str | None = None
    process_name: str | None = None  # 进程名过滤（与 execute_action 一致）
    hwnd: int | None = None
    require_confirm: bool = False  # 批量操作默认不需逐个确认（除非有危险关键词）
    activate_window: bool = True  # 批量开始前激活目标窗口
    stop_on_error: bool = True
    interval: float = 0.0  # 每步之间的额外间隔秒数
    # 评估文档 P0：批量焦点安全
    allow_unfocused_input: bool = False  # 键盘动作未验证焦点时是否放行（默认 False 强校验，与 execute_action 一致）
    stop_on_focus_drift: bool = True  # 任一步骤后焦点漂移到非目标族（delivery=leaked）时立即停止剩余动作
    # 第二轮评估 P1-1：批量动作 dry-run（不投递键鼠，返回每步校验结果）
    dry_run: bool = False
    # 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许接管
    task_description: str | None = None

@router.post("/batch-actions", operation_id="batch_actions")
def batch_actions(req: BatchActionsRequest):
    """批量顺序执行多个键鼠操作。

    适用于"点击菜单 → 等待 → 输入文本 → 回车"这类多步骤操作，避免多次 HTTP 往返。
    支持 action="wait" 在步骤间精确等待。

    安全策略：
    - 危险关键词（删除/支付/关机等）的步骤会被跳过并标记 blocked
    - stop_on_error=True 时遇到失败立即停止
    - 默认 activate_window=True 会先激活目标窗口
    - 评估文档 P0：键盘动作走焦点强校验（stop_on_focus_drift=True 时焦点漂移即停止）
    - 评估文档 P0：坐标动作走 snapshot_id 新鲜度检查（STALE_COORDINATES）

    Tip — 操作前建议先调 screen_match_app(process_name=...) 查询该软件经验
    （UIA 友好度/快捷键/菜单路径/已知坑），避免重复踩坑。
    """
    # T15：副作用端点 403 拦截（无权限时 raise HTTPException(403)）
    session_status = _enforce_session_permission()
    current_mode = session_status.get("mode", "normal")

    if not _ADMIN_STATUS:
        return {"success": False, "message": "键鼠操控需要管理员权限"}
    if not emergency.can_operate():
        return {"success": False, "status": "emergency_stopped", "message": "紧急停止已触发"}
    if not req.actions:
        return {"success": False, "message": "actions 列表为空"}

    # config 接线（focus_protection_enabled / protected_processes）
    from server.config import get_screen_config
    screen_cfg = get_screen_config()
    focus_protection_enabled = screen_cfg.get("focus_protection_enabled", True)
    protected_processes = screen_cfg.get("protected_processes", None)

    # 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许 agent 接管键鼠操作
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

    confirm_items = [
        item for item in req.actions
        if _check_danger(item.text, item.keys, item.element_text) == "confirm"
    ]
    # T15（决策 9）：watchdog 模式下 danger_level=confirm 自动跳过（与 execute_action 一致）
    if current_mode == "watchdog" and not req.require_confirm:
        confirm_items = []
    if req.require_confirm or confirm_items:
        labels = [
            item.element_text or item.text or item.action
            for item in (confirm_items or req.actions)
        ]
        confirmed, confirm_reason = _confirm_control_operation(
            action=f"batch_actions ({len(req.actions)} steps)",
            text="; ".join(labels)[:500],
        )
        if not confirmed:
            return {
                "success": False,
                "status": "cancelled",
                "user_reason": confirm_reason,
                "message": "用户取消了批量操作",
            }

    # 解析目标 hwnd
    target_hwnd = req.hwnd
    target_title = req.window_title
    if target_hwnd is None and target_title:
        win = _find_window(target_title, req.process_name)
        if win:
            target_hwnd = win["hwnd"]

    # 激活窗口
    focus_warning = ""
    if req.activate_window and target_hwnd:
        try:
            _force_focus_window(target_hwnd)
            time.sleep(_FOCUS_RETRY_DELAY)
            import win32gui
            if win32gui.GetForegroundWindow() != target_hwnd:
                _force_focus_window(target_hwnd)
                time.sleep(_FOCUS_RETRY_DELAY)
                if win32gui.GetForegroundWindow() != target_hwnd:
                    fg_title = win32gui.GetWindowText(win32gui.GetForegroundWindow()) or "未知"
                    focus_warning = f"焦点被模态窗口抢占（当前前台: '{fg_title}'），操作可能发到错误窗口"
        except Exception as e:
            logger.warning(f"批量操作前激活窗口失败: {e}")

    # 显示覆盖层
    try:
        from server.overlay_client import overlay_client
        if not overlay_client.overlay_visible:
            overlay_client.show_overlay()
    except Exception:
        pass

    t0 = time.perf_counter()
    results = []
    executed = 0
    failed = 0
    blocked = 0
    aborted = False
    aborted_reason = ""

    for i, item in enumerate(req.actions):
        # 紧急停止检查
        if not emergency.can_operate():
            results.append({"index": i, "action": item.action, "status": "emergency_stopped"})
            aborted = True
            aborted_reason = "紧急停止触发"
            break

        # wait action
        if item.action == "wait":
            time.sleep(item.wait or 0.0)
            results.append({"index": i, "action": "wait", "status": "executed", "waited": item.wait})
            executed += 1
            if req.interval:
                time.sleep(req.interval)
            continue

        # 防呆参数检查（用 keyword 传 direction/amount，避免位置参数错位）
        ok, errmsg = _validate_action_params(
            item.action, item.x, item.y, item.text, item.keys,
            direction=item.direction, amount=item.amount,
            dx=item.dx, dy=item.dy,
        )
        if not ok:
            results.append({"index": i, "action": item.action, "status": "blocked", "reason": errmsg})
            blocked += 1
            if req.stop_on_error:
                aborted = True
                aborted_reason = f"防呆检查失败: {errmsg}"
                break
            continue

        # 评估文档 P0：坐标动作的 snapshot 新鲜度检查
        if focus_protection_enabled and item.action in _COORDINATE_ACTIONS and item.snapshot_id:
            fresh, stale_reason, _meta = _verify_snapshot_freshness(item.snapshot_id, target_hwnd)
            if not fresh:
                results.append({"index": i, "action": item.action, "status": "blocked",
                                "reason": f"{STALE_COORDINATES}: {stale_reason}"})
                blocked += 1
                if req.stop_on_error:
                    aborted = True
                    aborted_reason = f"STALE_COORDINATES: {stale_reason}"
                    break
                continue

        # 窗口范围检查（与 execute_action 一致：坐标必须在目标窗口内）
        if target_hwnd and item.x is not None and item.y is not None:
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                if not (left <= item.x <= right and top <= item.y <= bottom):
                    results.append({"index": i, "action": item.action, "status": "blocked",
                                    "reason": f"坐标 ({item.x}, {item.y}) 不在窗口 hwnd={target_hwnd} 范围内 ({left},{top},{right},{bottom})"})
                    blocked += 1
                    if req.stop_on_error:
                        aborted = True
                        aborted_reason = "坐标越界"
                        break
                    continue
            except Exception as e:
                logger.warning(f"批量操作窗口范围检查失败 hwnd={target_hwnd}: {e}")
                results.append({
                    "index": i,
                    "action": item.action,
                    "status": "blocked",
                    "reason": f"无法验证窗口 hwnd={target_hwnd} 的范围: {e}",
                })
                blocked += 1
                if req.stop_on_error:
                    aborted = True
                    aborted_reason = f"窗口范围检查异常: {e}"
                    break
                continue

        # 危险关键词检查（签名：text, keys, element_text）
        danger = _check_danger(item.text, item.keys, item.element_text)
        if danger == "block":
            results.append({"index": i, "action": item.action, "status": "blocked", "reason": "危险关键词"})
            blocked += 1
            if req.stop_on_error:
                aborted = True
                aborted_reason = "危险关键词拦截"
                break
            continue

        # 评估文档 P0：键盘动作焦点强校验
        focus_check_status = "skipped"
        if focus_protection_enabled and item.action in _KEYBOARD_ACTIONS:
            ok_focus, reason_focus, _ev = verify_focus_for_input(
                target_hwnd,
                allow_unfocused_input=req.allow_unfocused_input,
                protected_processes=protected_processes,
            )
            focus_check_status = reason_focus
            if not ok_focus:
                # 零按键发送
                results.append({
                    "index": i, "action": item.action,
                    "status": "blocked",
                    "reason": f"{focus_check_status}（零按键发送）",
                    "focus_check_status": focus_check_status,
                })
                blocked += 1
                if req.stop_on_error:
                    aborted = True
                    aborted_reason = f"{focus_check_status}"
                    break
                continue

        # 第二轮评估 P1-1：dry-run 模式（不投递键鼠，返回每步校验结果）
        if req.dry_run:
            # 获取当前 canonical window 信息
            canonical_info_item = None
            if target_hwnd and focus_protection_enabled:
                try:
                    canonical_info_item = resolve_canonical_window(target_hwnd)
                except Exception:
                    pass
            results.append({
                "index": i, "action": item.action,
                "status": "dry_run",
                "message": "dry_run=true：已通过前置校验，未投递键鼠事件",
                "focus_check_status": focus_check_status,
                "transport_status": "not_sent",
                "delivery_status": "skipped",
                "canonical_window": _canonical_for_response(canonical_info_item),
            })
            executed += 1  # dry-run 不算失败
            if item.wait:
                time.sleep(min(item.wait, 0.05))  # dry-run 时压缩等待时间
            if req.interval:
                time.sleep(min(req.interval, 0.05))
            continue

        # 执行
        try:
            r = _execute_action(
                action=item.action, x=item.x, y=item.y,
                text=item.text, keys=item.keys,
                direction=item.direction, amount=item.amount,
                dx=item.dx, dy=item.dy,
            )
            # 评估文档 P0：执行后立即检查焦点是否漂移
            delivery = "unknown"
            target_match_after = None
            canonical_info_item = None
            if focus_protection_enabled and target_hwnd:
                ev_after = collect_focus_evidence(target_hwnd)
                target_match_after = ev_after.get("target_match")
                delivery = "delivered" if target_match_after else "leaked"
                # 第二轮评估 P1-2：每步刷新 canonical_window（不保留动作前旧标题）
                try:
                    canonical_info_item = resolve_canonical_window(target_hwnd)
                except Exception:
                    pass

            # 第二轮评估 P1：每步支持 expected 声明式后验
            item_postcond = "not_checked"
            if item.expected:
                # 截图后调 _verify_postcondition
                try:
                    time.sleep(_ACTION_SETTLE_DELAY)
                    shot_bytes_item = None
                    if target_hwnd:
                        shot_bytes_item = _capture_window(target_hwnd)
                    if shot_bytes_item is None:
                        shot_bytes_item = _capture_fullscreen()
                    from PIL import Image
                    pil_item = Image.open(io.BytesIO(shot_bytes_item))
                    item_postcond = _verify_postcondition(item.expected, pil_item)
                except Exception as e:
                    logger.warning(f"批量步骤 {i} 后验失败: {e}")
                    item_postcond = "error"

            # 第二轮评估 P0-3：批量动作也用 executed/postcondition_failed/executed_unverified
            if not r["success"]:
                item_status = "failed"
            elif item_postcond == "verified":
                item_status = "executed"
            elif item_postcond == "failed":
                item_status = "postcondition_failed"
            else:  # executed_unverified / error / not_checked → 动作已发送但后验未通过
                item_status = "executed_unverified"

            results.append({
                "index": i, "action": item.action,
                "status": item_status,
                "message": r["message"],
                "focus_check_status": focus_check_status,
                "transport_status": "sent" if r["success"] else "error",
                "delivery_status": delivery,
                "target_match_after": target_match_after,
                "postcondition_status": item_postcond,
                "canonical_window": _canonical_for_response(canonical_info_item),
            })
            if r["success"]:
                executed += 1
                _touch_task_authorization_after_delivery(
                    success=True,
                    delivery_status=delivery,
                )
                # 评估文档 P0：焦点漂移即停止剩余动作
                if req.stop_on_focus_drift and delivery == "leaked":
                    aborted = True
                    aborted_reason = (
                        f"焦点漂移：步骤 {i} 执行后前台不再属于目标 family "
                        f"（hwnd={target_hwnd}）。停止剩余 {len(req.actions) - i - 1} 个动作"
                    )
                    break
            else:
                failed += 1
                if req.stop_on_error:
                    aborted = True
                    aborted_reason = f"步骤 {i} 执行失败: {r['message']}"
                    break
        except Exception as e:
            results.append({"index": i, "action": item.action, "status": "failed", "message": str(e)})
            failed += 1
            if req.stop_on_error:
                aborted = True
                aborted_reason = f"步骤 {i} 异常: {e}"
                break

        if item.wait:
            time.sleep(item.wait)
        if req.interval:
            time.sleep(req.interval)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    msg = f"批量执行完成: {executed}/{len(req.actions)} 成功, {failed} 失败, {blocked} 拦截"
    if aborted and aborted_reason:
        msg += f" | 已中止: {aborted_reason}"
    if focus_warning:
        msg += f" | {focus_warning}"
    return {
        "success": failed == 0 and blocked == 0 and not aborted,
        "total": len(req.actions),
        "executed": executed,
        "failed": failed,
        "blocked": blocked,
        "aborted": aborted,
        "aborted_reason": aborted_reason if aborted else "",
        "elapsed_ms": elapsed_ms,
        "results": results,
        "message": msg,
    }


# ========== 内联截图（给多模态 LLM 直看） ==========

class CaptureInlineResponse(BaseSchema):
    mcp_image_block: bool = True  # 触发 main.py 的 ImageContent 转换
    image: str  # base64
    mime_type: str = "image/jpeg"
    width: int
    height: int
    original_width: int
    original_height: int
    window_title: str | None = None
    elapsed_ms: int
    note: str = ""
    snapshot_id: str | None = None  # 截图快照 ID（传给 execute_action.snapshot_id 做 STALE_COORDINATES 检测）
    window_rect: list[int] | None = None  # 截图目标的窗口几何 [left, top, right, bottom]


# ========== 点击位置预览（红点标记，给用户确认坐标） ==========

class PreviewPoint(BaseSchema):
    x: int  # 屏幕物理像素坐标（与 execute_action 一致，窗口模式下内部会减去窗口左上角偏移）
    y: int
    label: str | None = None  # 可选标签（如"下载标签"）

class PreviewActionRequest(BaseSchema):
    points: list[PreviewPoint]  # 要标记的目标坐标（支持多点）
    mode: str = "window"  # fullscreen | window
    window_title: str | None = None
    process_name: str | None = None  # 进程名过滤（避免同名窗口冲突）
    hwnd: int | None = None
    max_edge: int = 1280
    jpeg_quality: int = 85
    marker_size: int = 18  # L形臂长（原始像素）
    show_crosshair: bool = False  # 画贯穿十字线（多点时建议关闭，太乱）
    center_dot: bool = True  # 在点击中心画小红点
    # 第二轮评估 P0-4：默认 format=path 避免大段 base64 污染上下文
    # - path：保存临时文件返回路径（默认，纯文本 LLM 安全）
    # - inline：返回 base64 JPEG（多模态 LLM 直接看图，触发 mcp_image_block）
    format: str = "path"

class PreviewActionResponse(BaseSchema):
    mcp_image_block: bool = False  # format=inline 时为 True，触发 main.py 的 ImageContent 转换
    image: str = ""  # base64 JPEG（仅 format=inline 时填充）
    path: str | None = None  # 临时文件路径（format=path 时填充），可传给后续工具
    mime_type: str = "image/jpeg"
    width: int
    height: int
    original_width: int
    original_height: int
    window_title: str | None = None
    points_marked: int
    points_info: list[dict]  # 每个点的信息（含是否越界）
    elapsed_ms: int
    note: str = ""

@router.post("/preview/action", response_model=PreviewActionResponse, operation_id="preview_action")
def preview_action(req: PreviewActionRequest):
    """在截图上用红点标记目标点击位置，返回带标记的图片供用户确认。

    用途：点击前先预览坐标是否准确，避免盲点导致误操作。
    agent 将带红点的截图给用户看，用户确认后再执行 execute_action。

    坐标系：屏幕物理像素（与 execute_action 完全一致）。
    - mode=fullscreen：截图是全屏，坐标=屏幕坐标，1:1 对应。
    - mode=window：截图是窗口内容，但 points 仍传屏幕坐标，
      内部自动减去窗口左上角偏移转换为窗口内坐标画点。
      这样用户确认的坐标可直接传给 execute_action，无需手动转换。
    多模态 LLM 可直接看图（ImageContent），纯文本 LLM 看 points_info。
    """
    # 防呆：response_model=PreviewActionResponse 要求返回完整字段，
    # 早退必须用 HTTPException(400)，否则 dict 缺字段触发 Pydantic 验证 500
    if not req.points:
        raise HTTPException(status_code=400, detail="points 不能为空。推荐：传入 [{x:880,y:495,label:'下载标签'}] 标记目标位置（屏幕物理坐标，与 execute_action 一致）")
    if req.mode not in ("fullscreen", "window"):
        raise HTTPException(status_code=400, detail=f"mode='{req.mode}' 不支持，可选值: fullscreen | window")
    if req.mode == "window" and not req.window_title and not req.hwnd:
        raise HTTPException(status_code=400, detail="mode=window 必须提供 window_title 或 hwnd。推荐：用 list_windows 获取窗口信息")
    if req.format not in ("path", "inline"):
        raise HTTPException(status_code=400, detail=f"format='{req.format}' 不支持，可选值: path（默认，返回临时文件路径）| inline（返回 base64 JPEG，多模态 LLM 直接看图）")

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
        win_offset_x, win_offset_y = 0, 0  # 窗口左上角屏幕坐标（用于坐标转换）
        if req.mode == "window":
            target_hwnd = req.hwnd
            if target_hwnd is None and req.window_title:
                win = _find_window(req.window_title, req.process_name)
                if not win:
                    raise HTTPException(status_code=404, detail=f"未找到窗口: {req.window_title}。推荐：用 list_windows 查看可用窗口")
                target_hwnd = win["hwnd"]
                captured_title = win["title"]
                win_offset_x = win["bbox"]["left"]
                win_offset_y = win["bbox"]["top"]
            elif target_hwnd:
                try:
                    import win32gui
                    captured_title = win32gui.GetWindowText(target_hwnd)
                    left, top, _, _ = win32gui.GetWindowRect(target_hwnd)
                    win_offset_x, win_offset_y = left, top
                except Exception:
                    captured_title = ""
            png_bytes = _capture_window(target_hwnd)
            if png_bytes is None:
                raise HTTPException(status_code=500, detail="窗口截图失败")
        else:
            png_bytes = _capture_fullscreen()
            captured_title = None

        # 2. 在截图上标记红点
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(png_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        orig_w, orig_h = img.size
        draw = ImageDraw.Draw(img)

        # 字体：尝试 arial，失败用默认
        font_size = max(16, orig_w // 70)
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        arm = req.marker_size  # L形臂长
        gap = max(3, arm // 5)  # L形离中心的间隙（不遮挡点击中心）
        lw = max(2, arm // 8)  # L形线宽
        dot_r = max(2, arm // 6)  # 中心小红点半径
        points_info = []
        for i, pt in enumerate(req.points):
            # 输入是屏幕坐标，窗口模式下转换为窗口内坐标画点
            screen_x, screen_y = int(pt.x), int(pt.y)
            x = screen_x - win_offset_x
            y = screen_y - win_offset_y
            in_range = 0 <= x <= orig_w and 0 <= y <= orig_h
            points_info.append({
                "index": i + 1,
                "screen_x": screen_x, "screen_y": screen_y,  # 原始屏幕坐标（传给 execute_action）
                "x": x, "y": y,  # 窗口内坐标（图上位置）
                "label": pt.label,
                "in_range": in_range,
            })

            # 十字线（红色细线贯穿全图，多点时建议关闭）
            if req.show_crosshair:
                draw.line([(0, y), (orig_w, y)], fill=(255, 0, 0), width=1)
                draw.line([(x, 0), (x, orig_h)], fill=(255, 0, 0), width=1)

            # L形角标（四个角，开口朝向中心，不遮挡点击中心）
            RED = (255, 0, 0)
            o = arm + gap  # L形外边缘到中心的距离
            t = gap  # L形内边缘到中心的距离
            # 左上 L：竖线 + 横线
            draw.line([(x - o, y - o), (x - o, y - t)], fill=RED, width=lw)
            draw.line([(x - o, y - o), (x - t, y - o)], fill=RED, width=lw)
            # 右上 L
            draw.line([(x + o, y - o), (x + o, y - t)], fill=RED, width=lw)
            draw.line([(x + o, y - o), (x + t, y - o)], fill=RED, width=lw)
            # 左下 L
            draw.line([(x - o, y + o), (x - o, y + t)], fill=RED, width=lw)
            draw.line([(x - o, y + o), (x - t, y + o)], fill=RED, width=lw)
            # 右下 L
            draw.line([(x + o, y + o), (x + o, y + t)], fill=RED, width=lw)
            draw.line([(x + o, y + o), (x + t, y + o)], fill=RED, width=lw)

            # 中心小红点（精确标记点击中心）
            if req.center_dot:
                draw.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=(255, 0, 0))

            # 标签：白底黑字（显示屏幕坐标，方便传给 execute_action）
            label_text = f"#{i+1} screen({screen_x},{screen_y})"
            if pt.label:
                label_text += f" {pt.label}"
            try:
                bbox = draw.textbbox((0, 0), label_text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = len(label_text) * (font_size // 2), font_size
            tx = x + o + 4
            ty = y - o - th - 4
            if tx + tw > orig_w:
                tx = x - o - tw - 4
            if ty < 0:
                ty = y + o + 4
            draw.rectangle([tx - 3, ty - 3, tx + tw + 3, ty + th + 3], fill=(255, 255, 255), outline=(0, 0, 0))
            draw.text((tx, ty), label_text, fill=(0, 0, 0), font=font)

        # 3. 缩放 + JPEG 压缩
        if max(orig_w, orig_h) > req.max_edge:
            scale = req.max_edge / max(orig_w, orig_h)
            new_w, new_h = int(orig_w * scale), int(orig_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = orig_w, orig_h

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=req.jpeg_quality, optimize=True)
        jpg_bytes = buf.getvalue()

        elapsed = int((time.perf_counter() - t0) * 1000)
        out_of_range = [p for p in points_info if not p["in_range"]]
        note_parts = [f"已标记 {len(req.points)} 个点"]
        if out_of_range:
            note_parts.append(f"警告: {len(out_of_range)} 个点超出截图范围({orig_w}x{orig_h})")
        note_parts.append("请确认红点位置正确后再执行 execute_action")

        # 第二轮评估 P0-4：根据 format 决定输出方式
        # - path：保存临时文件返回路径（默认，不污染上下文）
        # - inline：返回 base64 JPEG（多模态 LLM 直接看图）
        b64 = ""
        img_path = None
        mcp_image_block = False
        if req.format == "inline":
            b64 = base64.b64encode(jpg_bytes).decode("ascii")
            mcp_image_block = True
        else:  # format == "path"
            import os
            import tempfile
            # 保存到 server 临时目录（与 capture_screen 的 path 模式一致）
            tmp_dir = os.path.join(tempfile.gettempdir(), "localagent_captures")
            os.makedirs(tmp_dir, exist_ok=True)
            img_filename = f"preview_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
            img_path = os.path.join(tmp_dir, img_filename)
            with open(img_path, "wb") as f:
                f.write(jpg_bytes)

        return PreviewActionResponse(
            mcp_image_block=mcp_image_block,
            image=b64,
            path=img_path,
            mime_type="image/jpeg",
            width=new_w,
            height=new_h,
            original_width=orig_w,
            original_height=orig_h,
            window_title=captured_title,
            points_marked=len(req.points),
            points_info=points_info,
            elapsed_ms=elapsed,
            note=" | ".join(note_parts),
        )
    finally:
        if overlay_was_visible:
            try:
                from server.overlay_client import overlay_client
                overlay_client.show_overlay()
            except Exception:
                pass
