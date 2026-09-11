"""desktop_transaction 端点 — 桌面事务（多步动作 + 声明式后验 + 失败回滚）。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router / 常量 / takeover helper / snapshot 元数据缓存仍由 routes 提供，
_canonical_for_response / _verify_postcondition 由 action_endpoints 提供，
_check_token_or_403 由 lifecycle_endpoints 提供。
本模块定义：
- DesktopTransactionItem / DesktopTransactionRequest / DesktopTransactionItemResult /
  DesktopTransactionResponse 模型
- desktop_transaction（/desktop-transaction）端点

导入本模块即触发 @router.post 注册，无需额外调用。
"""

import io
import time

from lib.schema import BaseSchema

# 从 action_endpoints 复用共享 helper（routes.py 导入 action_endpoints 在本模块之前，已就绪）
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

# 从 lifecycle_endpoints 复用 window_token 校验（routes.py 导入 lifecycle_endpoints 在本模块之前，已就绪）
from server.screen.lifecycle_endpoints import (
    _check_token_or_403,
)

# 从主模块复用共享件（router / 日志 / 常量 / takeover helper / snapshot 元数据缓存）
from server.screen.routes import (
    _ACTION_SETTLE_DELAY,
    _COORDINATE_ACTIONS,
    _FOCUS_RETRY_DELAY,
    _KEYBOARD_ACTIONS,
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

# ========== P2-1：桌面事务（多步动作 + 声明式后验 + 失败回滚） ==========
# 第二轮评估 P2-1：把"截图-定位-点击-输入-提交-后验"这种多步原子操作包成一个事务。
# 失败时停止剩余步骤；rollback_policy=auto 时尝试执行 rollback_actions 回到操作前状态。
# 与 batch_actions 区别：
# - batch_actions 是"执行多个动作"，无事务语义（失败可选继续）
# - desktop_transaction 是"原子事务"，必须有 expected 后验，失败必须停止 + 可选回滚

class DesktopTransactionItem(BaseSchema):
    """事务中的单步动作。"""
    action: str  # click | double_click | right_click | type | type_immediate | hotkey | scroll | drag | wait
    x: int | None = None
    y: int | None = None
    text: str | None = None
    keys: list[str] | None = None
    direction: str = "down"
    amount: int = 3
    dx: int = 0
    dy: int = 0
    button: str = "left"  # mouse_down/mouse_up 的按键（left | right）
    wait: float = 0.0  # action="wait" 时的等待秒数
    element_text: str | None = None
    snapshot_id: str | None = None
    label: str | None = None  # 步骤标签（如"点击下载按钮"），便于日志和回滚


class DesktopTransactionRequest(BaseSchema):
    """桌面事务请求。

    必须提供 expected 字段（事务级后验），否则不构成事务——agent 应改用 batch_actions。
    rollback_policy:
        - "none"：失败时仅停止（默认）
        - "auto"：失败时按顺序执行 rollback_actions，再返回结果
    timeout: 事务整体超时秒数（默认 60s），超时视为失败并触发回滚
    """
    target: dict  # 窗口引用：{"hwnd": int} | {"window_title": str, "process_name"?: str} | {"window_token": dict}
    actions: list[DesktopTransactionItem]
    expected: dict  # 事务级后验：{"type": "ocr_contains"|"ocr_not_contains", "text": "...", ...}
    rollback_actions: list[DesktopTransactionItem] = []  # rollback_policy=auto 时执行
    rollback_policy: str = "none"  # none | auto
    timeout: float = 60.0
    interval: float = 0.0  # 步骤间额外间隔
    allow_unfocused_input: bool = False  # 与 batch_actions 一致
    dry_run: bool = False  # 事务级 dry-run：通过所有校验但不投递键鼠
    require_confirm: bool = False  # 显式要求事务执行前确认；危险步骤始终强制确认
    task_description: str | None = None  # 接管确认弹窗中显示的任务描述（overlay 未显示时）


class DesktopTransactionItemResult(BaseSchema):
    index: int
    action: str
    label: str | None = None
    status: str  # executed | executed_unverified | postcondition_failed | blocked | failed | dry_run | emergency_stopped
    message: str = ""
    focus_check_status: str | None = None
    transport_status: str | None = None
    delivery_status: str | None = None
    postcondition_status: str | None = None
    canonical_window: dict | None = None


class DesktopTransactionResponse(BaseSchema):
    success: bool  # 事务是否整体成功（所有动作发送 + 事务级 expected 后验通过）
    # 第三轮评估 P1-1：dry_run 是"预演通过"，不是失败。编排器应优先用 status + dry_run 字段判断
    status: str  # committed | committed_unverified | postcondition_failed | aborted | rolled_back | rollback_failed | dry_run | emergency_stopped
    dry_run: bool = False  # True=本次为 dry_run 预演（未投递键鼠），success=True 仅表示预演通过前置校验
    total_steps: int
    executed_steps: int
    failed_step_index: int | None = None  # 失败步骤的索引（None=全部成功）
    failed_reason: str = ""
    rollback_executed: bool = False
    rollback_steps_succeeded: int = 0
    rollback_results: list[DesktopTransactionItemResult] = []
    transaction_postcondition: str  # verified | failed | error | unavailable | not_checked
    elapsed_ms: int
    results: list[DesktopTransactionItemResult]
    canonical_window: dict | None = None
    message: str


@router.post("/desktop-transaction", response_model=DesktopTransactionResponse,
             operation_id="screen_desktop_transaction")
def desktop_transaction(req: DesktopTransactionRequest):
    """桌面事务 —— 第二轮评估 P2-1。

    把"截图-定位-点击-输入-提交-后验"这种多步原子操作包成一个事务：
    1. 必须声明 expected（事务级后验），不声明的事务用 batch_actions
    2. 任一步骤失败立即停止剩余步骤（不自动继续）
    3. rollback_policy=auto 时按顺序执行 rollback_actions
    4. timeout 整体超时，超时视为失败并触发回滚
    5. dry_run=true 时通过所有校验但不投递键鼠，返回 would_execute 结构

    与 batch_actions 区别：
    - batch_actions 是"执行多个动作"，无事务语义（失败可选继续，无后验约束）
    - desktop_transaction 是"原子事务"，必须有 expected，失败必须停止 + 可选回滚

    适用场景：
    - 表单填写并提交（点击输入框→输入→点提交→后验"已提交"）
    - 文件另存为（点文件菜单→另存为→输入路径→点保存→后验文件存在）
    - 多步导航（点标签→等加载→点按钮→后验页面变化）
    """
    # T15：副作用端点 403 拦截
    session_status = _enforce_session_permission()
    current_mode = session_status.get("mode", "normal")

    # 1. 基础校验
    if not _ADMIN_STATUS:
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message="键鼠操控需要管理员权限",
        )
    if not emergency.can_operate():
        return DesktopTransactionResponse(
            success=False, status="emergency_stopped", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message="紧急停止已触发",
        )
    if not req.actions:
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=0, executed_steps=0,
            transaction_postcondition="not_checked", elapsed_ms=0, results=[],
            message="actions 列表为空",
        )
    if not req.expected or not req.expected.get("type") or not req.expected.get("text"):
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message="事务必须声明 expected（{'type':'ocr_contains','text':'...'}）。无后验约束的多步操作请用 batch_actions",
        )
    if req.rollback_policy not in ("none", "auto"):
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message=f"rollback_policy='{req.rollback_policy}' 不支持，可选值: none | auto",
        )

    # 2. 解析 target → target_hwnd
    target = req.target or {}
    target_hwnd = target.get("hwnd")
    target_title = target.get("window_title")
    target_process = target.get("process_name")
    window_token = target.get("window_token")
    if target_hwnd is None and target_title:
        win = _find_window(target_title, target_process)
        if win:
            target_hwnd = win["hwnd"]
    if target_hwnd is None and not target_title:
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message="target 必须提供 hwnd 或 window_title",
        )

    # 3. window_token 校验（如提供）
    if window_token is not None:
        invalid_reason = _check_token_or_403(window_token)
        if invalid_reason is not None:
            return DesktopTransactionResponse(
                success=False, status="aborted", total_steps=len(req.actions),
                executed_steps=0, transaction_postcondition="not_checked",
                elapsed_ms=0, results=[],
                message=f"window token 失效: {invalid_reason}（请重新 window_resolve 获取新 token）",
            )

    # 4. 接线 config
    from server.config import get_screen_config
    screen_cfg = get_screen_config()
    focus_protection_enabled = screen_cfg.get("focus_protection_enabled", True)
    protected_processes = screen_cfg.get("protected_processes", None)

    # 4.5 接管确认（takeover confirm）：顶栏未显示时弹窗询问用户是否允许 agent 接管键鼠操作
    approved, takeover_reason = _ensure_takeover_approved(screen_cfg, req.task_description)
    if not approved:
        msg = "用户拒绝 agent 接管键鼠操作"
        if takeover_reason:
            msg += f"。用户反馈: {takeover_reason}"
        return DesktopTransactionResponse(
            success=False, status="aborted", total_steps=len(req.actions),
            executed_steps=0, transaction_postcondition="not_checked",
            elapsed_ms=0, results=[],
            message=msg,
        )

    confirm_items = [
        item for item in [*req.actions, *req.rollback_actions]
        if _check_danger(item.text, item.keys, item.element_text) == "confirm"
    ]
    # T15（决策 9）：watchdog 模式下 danger_level=confirm 自动跳过（与 execute_action 一致）
    if current_mode == "watchdog" and not req.require_confirm:
        confirm_items = []
    if req.require_confirm or confirm_items:
        labels = [
            item.element_text or item.text or item.label or item.action
            for item in (confirm_items or req.actions)
        ]
        confirmed, confirm_reason = _confirm_control_operation(
            action=f"desktop_transaction ({len(req.actions)} steps)",
            text="; ".join(labels)[:500],
        )
        if not confirmed:
            return DesktopTransactionResponse(
                success=False,
                status="aborted",
                total_steps=len(req.actions),
                executed_steps=0,
                transaction_postcondition="not_checked",
                elapsed_ms=0,
                results=[],
                failed_reason=confirm_reason,
                message="用户取消了桌面事务",
            )

    # 5. 激活目标窗口
    if target_hwnd:
        try:
            _force_focus_window(target_hwnd)
            time.sleep(_FOCUS_RETRY_DELAY)
        except Exception as e:
            logger.warning(f"事务开始前激活窗口失败: {e}")

    # 6. 显示覆盖层
    try:
        from server.overlay_client import overlay_client
        if not overlay_client.overlay_visible:
            overlay_client.show_overlay()
    except Exception:
        pass

    t0 = time.perf_counter()
    deadline = t0 + req.timeout
    results: list[DesktopTransactionItemResult] = []
    executed_steps = 0
    failed_step_index: int | None = None
    failed_reason = ""
    aborted = False

    # 7. 逐步执行
    for i, item in enumerate(req.actions):
        # 紧急停止检查
        if not emergency.can_operate():
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="emergency_stopped", message="紧急停止触发",
            ))
            aborted = True
            failed_step_index = i
            failed_reason = "紧急停止触发"
            break

        # 超时检查
        if time.perf_counter() >= deadline:
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="blocked", message=f"事务超时（{req.timeout}s）",
            ))
            aborted = True
            failed_step_index = i
            failed_reason = f"事务超时（{req.timeout}s）"
            break

        # wait action
        if item.action == "wait":
            time.sleep(item.wait or 0.0)
            results.append(DesktopTransactionItemResult(
                index=i, action="wait", label=item.label,
                status="executed", message=f"等待 {item.wait}s",
            ))
            executed_steps += 1
            if req.interval:
                time.sleep(req.interval)
            continue

        # 防呆参数检查
        ok, errmsg = _validate_action_params(
            item.action, item.x, item.y, item.text, item.keys,
            direction=item.direction, amount=item.amount,
            dx=item.dx, dy=item.dy, button=item.button,
        )
        if not ok:
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="blocked", message=f"防呆检查失败: {errmsg}",
            ))
            aborted = True
            failed_step_index = i
            failed_reason = f"防呆检查失败: {errmsg}"
            break

        # snapshot 新鲜度检查
        if focus_protection_enabled and item.action in _COORDINATE_ACTIONS and item.snapshot_id:
            fresh, stale_reason, _meta = _verify_snapshot_freshness(item.snapshot_id, target_hwnd)
            if not fresh:
                results.append(DesktopTransactionItemResult(
                    index=i, action=item.action, label=item.label,
                    status="blocked", message=f"{STALE_COORDINATES}: {stale_reason}",
                ))
                aborted = True
                failed_step_index = i
                failed_reason = f"{STALE_COORDINATES}: {stale_reason}"
                break

        # 窗口范围检查
        if target_hwnd and item.x is not None and item.y is not None:
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(target_hwnd)
                if not (left <= item.x <= right and top <= item.y <= bottom):
                    results.append(DesktopTransactionItemResult(
                        index=i, action=item.action, label=item.label,
                        status="blocked",
                        message=f"坐标 ({item.x}, {item.y}) 不在窗口范围内 ({left},{top},{right},{bottom})",
                    ))
                    aborted = True
                    failed_step_index = i
                    failed_reason = "坐标越界"
                    break
            except Exception as e:
                results.append(DesktopTransactionItemResult(
                    index=i, action=item.action, label=item.label,
                    status="blocked", message=f"窗口范围检查异常: {e}",
                ))
                aborted = True
                failed_step_index = i
                failed_reason = f"窗口范围检查异常: {e}"
                break

        # 危险关键词检查
        danger = _check_danger(item.text, item.keys, item.element_text)
        if danger == "block":
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="blocked", message="危险关键词拦截",
            ))
            aborted = True
            failed_step_index = i
            failed_reason = "危险关键词拦截"
            break

        # 焦点强校验
        focus_check_status = "skipped"
        if focus_protection_enabled and item.action in _KEYBOARD_ACTIONS:
            ok_focus, reason_focus, _ev = verify_focus_for_input(
                target_hwnd,
                allow_unfocused_input=req.allow_unfocused_input,
                protected_processes=protected_processes,
            )
            focus_check_status = reason_focus
            if not ok_focus:
                results.append(DesktopTransactionItemResult(
                    index=i, action=item.action, label=item.label,
                    status="blocked",
                    message=f"{focus_check_status}（零按键发送）",
                    focus_check_status=focus_check_status,
                    transport_status="not_sent",
                ))
                aborted = True
                failed_step_index = i
                failed_reason = focus_check_status
                break

        # dry-run：通过所有校验但不投递
        if req.dry_run:
            canonical_info_item = None
            if target_hwnd and focus_protection_enabled:
                try:
                    canonical_info_item = resolve_canonical_window(target_hwnd)
                except Exception:
                    pass
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="dry_run",
                message="dry_run=true：已通过前置校验，未投递键鼠",
                focus_check_status=focus_check_status,
                transport_status="not_sent",
                delivery_status="skipped",
                canonical_window=_canonical_for_response(canonical_info_item),
            ))
            executed_steps += 1
            if item.wait:
                time.sleep(min(item.wait, 0.05))
            if req.interval:
                time.sleep(min(req.interval, 0.05))
            continue

        # 执行动作
        try:
            r = _execute_action(
                action=item.action, x=item.x, y=item.y,
                text=item.text, keys=item.keys,
                direction=item.direction, amount=item.amount,
                dx=item.dx, dy=item.dy, button=item.button,
            )
            # 焦点漂移检测
            delivery = "unknown"
            target_match_after = None
            canonical_info_item = None
            if focus_protection_enabled and target_hwnd:
                ev_after = collect_focus_evidence(target_hwnd)
                target_match_after = ev_after.get("target_match")
                delivery = "delivered" if target_match_after else "leaked"
                try:
                    canonical_info_item = resolve_canonical_window(target_hwnd)
                except Exception:
                    pass

            if not r["success"]:
                results.append(DesktopTransactionItemResult(
                    index=i, action=item.action, label=item.label,
                    status="failed", message=r["message"],
                    focus_check_status=focus_check_status,
                    transport_status="error",
                    delivery_status=delivery,
                    canonical_window=_canonical_for_response(canonical_info_item),
                ))
                aborted = True
                failed_step_index = i
                failed_reason = f"步骤 {i} 执行失败: {r['message']}"
                break

            # 焦点漂移即视为事务失败
            if delivery == "leaked":
                results.append(DesktopTransactionItemResult(
                    index=i, action=item.action, label=item.label,
                    status="failed",
                    message=f"焦点漂移：步骤 {i} 后前台不再属于目标 family",
                    focus_check_status=focus_check_status,
                    transport_status="sent",
                    delivery_status="leaked",
                    canonical_window=_canonical_for_response(canonical_info_item),
                ))
                aborted = True
                failed_step_index = i
                failed_reason = "焦点漂移"
                break

            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="executed", message=r["message"],
                focus_check_status=focus_check_status,
                transport_status="sent",
                delivery_status=delivery,
                canonical_window=_canonical_for_response(canonical_info_item),
            ))
            executed_steps += 1
        except Exception as e:
            results.append(DesktopTransactionItemResult(
                index=i, action=item.action, label=item.label,
                status="failed", message=f"步骤 {i} 异常: {e}",
            ))
            aborted = True
            failed_step_index = i
            failed_reason = f"步骤 {i} 异常: {e}"
            break

        if item.wait:
            time.sleep(item.wait)
        if req.interval:
            time.sleep(req.interval)

    # 8. dry-run 模式：跳过事务级后验和回滚
    # 第三轮评估 P1-1：dry_run=true 表示"预演通过前置校验"，是成功结果（success=True）。
    # 编排器应同时检查 status=="dry_run" + dry_run==True 判定本次未真实执行。
    if req.dry_run:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return DesktopTransactionResponse(
            success=True,  # dry-run 预演通过视为成功（编排器用 status/dry_run 字段区分）
            status="dry_run",
            dry_run=True,
            total_steps=len(req.actions),
            executed_steps=executed_steps,
            failed_step_index=None,
            failed_reason="",
            rollback_executed=False,
            rollback_steps_succeeded=0,
            rollback_results=[],
            transaction_postcondition="not_checked",
            elapsed_ms=elapsed_ms,
            results=results,
            message=f"事务 dry-run 完成：{executed_steps}/{len(req.actions)} 步通过前置校验，未投递键鼠",
        )

    # 9. 事务级后验（仅在所有步骤都成功执行时进行）
    transaction_postcondition = "not_checked"
    if not aborted:
        try:
            time.sleep(_ACTION_SETTLE_DELAY)
            postcond_bytes = None
            if target_hwnd:
                postcond_bytes = _capture_window(target_hwnd)
            if postcond_bytes is None:
                postcond_bytes = _capture_fullscreen()
            if postcond_bytes:
                from PIL import Image
                pil_postcond = Image.open(io.BytesIO(postcond_bytes))
                transaction_postcondition = _verify_postcondition(req.expected, pil_postcond)
            else:
                transaction_postcondition = "error"
        except Exception as e:
            logger.warning(f"事务级后验失败: {e}")
            transaction_postcondition = "error"

    # 10. 判定事务状态
    if aborted:
        tx_status = "aborted"
    elif transaction_postcondition == "verified":
        tx_status = "committed"
    elif transaction_postcondition == "unavailable":
        # OCR 模型被存活管理器拒绝（压力/卸载/冷却）≠ 后验失败：
        # 动作已真实执行，验证器暂时不可用 → 提交但不回滚（design §6 消费者矩阵）
        tx_status = "committed_unverified"
    elif transaction_postcondition == "failed":
        tx_status = "postcondition_failed"
        aborted = True  # 后验失败也算失败，触发回滚
        failed_reason = f"事务级后验失败：expected={req.expected}"
    else:  # error / not_checked
        tx_status = "postcondition_failed"
        aborted = True
        failed_reason = f"事务级后验异常：{transaction_postcondition}"

    # 11. 回滚（rollback_policy=auto 且事务失败时）
    rollback_executed = False
    rollback_steps_succeeded = 0
    rollback_results: list[DesktopTransactionItemResult] = []
    if aborted and req.rollback_policy == "auto" and req.rollback_actions:
        rollback_executed = True
        for j, rb_item in enumerate(req.rollback_actions):
            # 紧急停止
            if not emergency.can_operate():
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action=rb_item.action, label=rb_item.label,
                    status="emergency_stopped",
                ))
                break

            if rb_item.action == "wait":
                time.sleep(rb_item.wait or 0.0)
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action="wait", label=rb_item.label,
                    status="executed",
                ))
                rollback_steps_succeeded += 1
                continue

            # 防呆
            ok, errmsg = _validate_action_params(
                rb_item.action, rb_item.x, rb_item.y, rb_item.text, rb_item.keys,
                direction=rb_item.direction, amount=rb_item.amount,
                dx=rb_item.dx, dy=rb_item.dy, button=rb_item.button,
            )
            if not ok:
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action=rb_item.action, label=rb_item.label,
                    status="blocked", message=f"防呆检查失败: {errmsg}",
                ))
                break

            # 危险关键词
            danger = _check_danger(rb_item.text, rb_item.keys, rb_item.element_text)
            if danger == "block":
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action=rb_item.action, label=rb_item.label,
                    status="blocked", message="危险关键词拦截",
                ))
                break

            # 执行回滚动作（dry-run 不适用回滚，回滚必须真实执行）
            try:
                r = _execute_action(
                    action=rb_item.action, x=rb_item.x, y=rb_item.y,
                    text=rb_item.text, keys=rb_item.keys,
                    direction=rb_item.direction, amount=rb_item.amount,
                    dx=rb_item.dx, dy=rb_item.dy, button=rb_item.button,
                )
                canonical_info_rb = None
                if target_hwnd and focus_protection_enabled:
                    try:
                        canonical_info_rb = resolve_canonical_window(target_hwnd)
                    except Exception:
                        pass
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action=rb_item.action, label=rb_item.label,
                    status="executed" if r["success"] else "failed",
                    message=r["message"],
                    transport_status="sent" if r["success"] else "error",
                    canonical_window=_canonical_for_response(canonical_info_rb),
                ))
                if r["success"]:
                    rollback_steps_succeeded += 1
                    _touch_task_authorization_after_delivery(success=True)
                else:
                    break
            except Exception as e:
                rollback_results.append(DesktopTransactionItemResult(
                    index=j, action=rb_item.action, label=rb_item.label,
                    status="failed", message=str(e),
                ))
                break

            if rb_item.wait:
                time.sleep(rb_item.wait)

        # 回滚完成后状态调整
        tx_status = "rolled_back" if rollback_steps_succeeded == len(req.rollback_actions) else "rollback_failed"

    # 12. 最终 canonical_window（最后一次刷新的）
    final_canonical = None
    if target_hwnd:
        try:
            final_canonical = _canonical_for_response(resolve_canonical_window(target_hwnd))
        except Exception:
            pass
        # 优先用 results 中最后一个的 canonical_window
        for r_item in reversed(results):
            if r_item.canonical_window:
                final_canonical = r_item.canonical_window
                break

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    success = (tx_status in ("committed", "committed_unverified"))
    msg = f"事务 {tx_status}：{executed_steps}/{len(req.actions)} 步执行"
    if aborted and failed_reason:
        msg += f" | 失败原因: {failed_reason}"
    if rollback_executed:
        msg += f" | 回滚 {rollback_steps_succeeded}/{len(req.rollback_actions)} 步成功"
    msg += f" | 后验: {transaction_postcondition}"

    return DesktopTransactionResponse(
        success=success,
        status=tx_status,
        total_steps=len(req.actions),
        executed_steps=executed_steps,
        failed_step_index=failed_step_index,
        failed_reason=failed_reason,
        rollback_executed=rollback_executed,
        rollback_steps_succeeded=rollback_steps_succeeded,
        rollback_results=rollback_results,
        transaction_postcondition=transaction_postcondition,
        elapsed_ms=elapsed_ms,
        results=results,
        canonical_window=final_canonical,
        message=msg,
    )
