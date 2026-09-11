"""uia 端点 — UI Automation 语义层（accessibility snapshot + 语义动作）。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router / 日志仍由 routes 提供，_verify_postcondition 由 action_endpoints 提供。
本模块定义：
- UiaSnapshotRequest / UiaElement / UiaSnapshotResponse 模型 + uia_snapshot（/uia/snapshot）
- UiaActionRequest / UiaActionResponse 模型 + uia_action（/uia/action）

注：take_uia_snapshot / execute_semantic_action 仍以函数内局部导入方式从
server.screen.uia 引入（与拆分前一致），避免模块加载期强依赖 uiautomation 库。

导入本模块即触发 @router.post 注册，无需额外调用。
"""


from fastapi import HTTPException

from lib.schema import BaseSchema

# 从 action_endpoints 复用 _verify_postcondition（routes.py 导入 action_endpoints 在 uia 之前，已就绪）
from server.screen.action_endpoints import (
    _verify_postcondition,
)

# 截图子模块
from server.screen.capture import (
    _capture_fullscreen,
)

# 从主模块复用共享件（router / 日志）
from server.screen.routes import (
    _confirm_control_operation,
    _enforce_session_permission,
    _ensure_takeover_approved,
    _touch_task_authorization_after_delivery,
    logger,
    router,
)
from server.screen.security import _check_danger, emergency

# 窗口子模块
from server.screen.windows import (
    _find_window,
)

# ========== UIA 语义层（评估文档 P0-5）==========

class UiaSnapshotRequest(BaseSchema):
    """UIA accessibility snapshot 请求。

    评估文档第 9 节 P0-5：返回 role/name/value/enabled/checked/selected/bounds/
    automation_id/element_id，让 agent 像 Browser Use 一样从结构化 accessibility tree
    中找元素，而不是依赖坐标点击。

    必须指定 hwnd 或 window_title。UIA 不可用时返回 fallback_reason=UIA_NOT_AVAILABLE，
    agent 应回退 OCR/视觉定位。
    """
    window_title: str | None = None
    hwnd: int | None = None
    process_name: str | None = None  # 进程名过滤（与 list_windows 一致）
    max_depth: int = 8  # 遍历最大深度（root=0；标准应用深度 6-10 够用）
    interesting_only: bool = True  # True=只返回有 role/name/value 的元素（默认）
    max_elements: int = 500  # 上限（防 UIA 树过大撑爆上下文）


class UiaElement(BaseSchema):
    element_id: str  # 格式 {canonical_hwnd}:{snapshot_id_short}:{element_index}
    role: str  # ControlType 名（Button/Edit/ListItem...）
    name: str
    value: str | None = None
    enabled: bool = True
    checked: bool | None = None  # ToggleState: True=On
    selected: bool | None = None  # SelectionItem.IsSelected
    bounds: list[int]  # [left, top, right, bottom] 物理像素
    automation_id: str | None = None
    element_index: int
    depth: int
    parent_index: int | None = None
    children_indices: list[int] = []
    # ZCode 风格能力标志：空格连接短串（pressable/editable/toggleable/selectable/expandable/focused）
    flags: str = ""
    # 该元素支持的语义动作列表（对齐 screen_semantic_action 的 action 枚举）
    actions: list[str] = []


class UiaSnapshotResponse(BaseSchema):
    success: bool
    snapshot_id: str  # 后续 screen_semantic_action 必须引用此 id
    canonical_hwnd: int
    elements: list[UiaElement]
    element_count: int
    fallback_reason: str | None = None  # UIA_NOT_AVAILABLE / uia_no_target / uia_snapshot_failed
    elapsed_ms: int
    message: str
    app_lessons_hint: str | None = None  # 软件经验提示（按目标窗口 process_name 匹配）


class UiaActionRequest(BaseSchema):
    """UIA 语义动作请求（不依赖坐标，直接 invoke/toggle/set_value）。

    评估文档第 9 节 P0-5：element id 绑定 window token + snapshot version，过期返回
    STALE_ELEMENT。UIA 不可用时返回 fallback_reason=UIA_NOT_AVAILABLE，agent 应回退
    execute_action + 坐标。
    """
    element_id: str  # 来自 snapshot 的元素 ID
    snapshot_id: str  # 必须引用同一 snapshot
    action: str  # invoke | select | toggle | set_value | expand | collapse | scroll
    value: str | None = None  # set_value 时的目标值
    direction: str | None = "down"  # scroll 方向：up/down/left/right
    amount: int = 1  # scroll 量：1=小步（SmallIncrement）>1=大步（LargeIncrement）
    expected: dict | None = None  # 声明式后验：uia_value_equals（内建）或 ocr_contains/ocr_not_contains（OCR）
    element_text: str | None = None  # 安全分类与确认弹窗文案
    require_confirm: bool = False
    task_description: str | None = None


class UiaActionResponse(BaseSchema):
    success: bool
    status: str  # executed | executed_unverified | postcondition_failed | blocked | failed
    action: str
    element_id: str
    element_role: str | None = None
    element_name: str | None = None
    transport_status: str  # sent | not_sent | error
    postcondition_status: str  # verified | failed | error | executed_unverified | not_checked
    postcondition_actual_value: str | None = None  # P1-2：uia_value_equals 后验时实际读回的 Value
    fallback_reason: str | None = None  # STALE_ELEMENT / UIA_NOT_AVAILABLE / None
    elapsed_ms: int
    message: str


@router.post("/uia/snapshot", response_model=UiaSnapshotResponse, operation_id="screen_accessibility_snapshot")
def uia_snapshot(req: UiaSnapshotRequest):
    """UIA accessibility snapshot —— 评估文档 P0-5。

    返回目标窗口的 UIA 树结构（role/name/value/checked/selected/bounds/automation_id），
    让 agent 像 Browser Use 一样从结构化数据中找元素。element_id 绑定 canonical_hwnd
    + snapshot_id + element_index，后续 screen_semantic_action 必须引用同一 snapshot。

    UIA 不可用时（uiautomation 库未安装）返回 fallback_reason=UIA_NOT_AVAILABLE，
    agent 应回退 screen_ocr 或 vision_locate。

    注：必须指定 hwnd 或 window_title。UWP/WinUI 应用同族 HWND 已通过
    resolve_canonical_window 统一，agent 传任一同族 HWND 都能取到完整 accessibility tree。
    """
    # 解析 hwnd
    target_hwnd = req.hwnd
    target_process_name = req.process_name
    if target_hwnd is None:
        if not req.window_title:
            raise HTTPException(status_code=400, detail="必须提供 hwnd 或 window_title")
        win = _find_window(req.window_title, req.process_name)
        if not win:
            raise HTTPException(status_code=404, detail=f"未找到窗口: {req.window_title}")
        target_hwnd = win["hwnd"]
        target_process_name = win.get("process_name", req.process_name)

    from server.screen.uia import take_uia_snapshot
    result = take_uia_snapshot(
        hwnd=target_hwnd,
        max_depth=req.max_depth,
        interesting_only=req.interesting_only,
        max_elements=req.max_elements,
    )
    # runtime_id 是 UIA 内部标识（供 execute_semantic_action 通过
    # _find_uia_control_by_runtime_id 定位元素），不暴露给 agent。
    # 拷贝 elements 去掉 runtime_id，避免污染 take_uia_snapshot 的内部缓存
    # （_uia_snapshots 与返回值共用同一 elements list 引用）。
    result["elements"] = [
        {k: v for k, v in elem.items() if k != "runtime_id"}
        for elem in result.get("elements", [])
    ]
    # 自动注入软件经验提示（按目标窗口 process_name 匹配 apps/*.md）
    try:
        from server.screen.app_lessons import build_app_lessons_hint, match_app_for_process
        if target_process_name:
            lessons = match_app_for_process(target_process_name)
            result["app_lessons_hint"] = build_app_lessons_hint(lessons)
    except Exception:
        pass
    return UiaSnapshotResponse(**result)


@router.post("/uia/action", response_model=UiaActionResponse, operation_id="screen_semantic_action")
def uia_action(req: UiaActionRequest):
    """UIA 语义动作 —— 评估文档 P0-5。

    对 snapshot 中的 element 执行语义动作（invoke/select/toggle/set_value/expand/collapse/scroll），
    不依赖坐标，直接通过 UIA pattern 调用。比 execute_action 的坐标点击更可靠：
    - 不受窗口移动/缩放影响（不需要 STALE_COORDINATES 检查）
    - 不受焦点漂移影响（UIA 动作直接通过 COM 调用，不走 SendInput）
    - 不存在 FOCUS_LEAK_PREVENTED 风险

    Tip — 操作前建议先调 screen_match_app(process_name=...) 查询该软件经验
    （UIA 友好度/快捷键/菜单路径/已知坑），避免重复踩坑。

    element_id 绑定 snapshot_id + canonical_hwnd + element_index，过期/不匹配返回
    STALE_ELEMENT。agent 必须重新 take_uia_snapshot。

    第三轮评估 P1-2：expected 支持两种后验类型：
    - {"type":"uia_value_equals","text":"..."}：UIA 内建后验，execute_semantic_action
      内部读回 ValuePattern.Value 比对，无需 agent 再调 snapshot。仅 action=set_value 有效。
    - {"type":"ocr_contains","text":"..."}：OCR 后验（与 execute_action 一致）。
    """
    # T15：副作用端点 403 拦截
    session_status = _enforce_session_permission()
    current_mode = session_status.get("mode", "normal")

    if not emergency.can_operate():
        return UiaActionResponse(
            success=False,
            status="emergency_stopped",
            action=req.action,
            element_id=req.element_id,
            transport_status="not_sent",
            postcondition_status="not_checked",
            elapsed_ms=0,
            message="紧急停止已触发",
        )

    danger_level = _check_danger(element_text=req.element_text)
    if danger_level == "block":
        return UiaActionResponse(
            success=False,
            status="blocked",
            action=req.action,
            element_id=req.element_id,
            transport_status="not_sent",
            postcondition_status="not_checked",
            elapsed_ms=0,
            message="操作被安全策略拦截（包含危险关键词）",
        )

    from server.config import get_screen_config
    screen_cfg = get_screen_config()
    approved, takeover_reason = _ensure_takeover_approved(
        screen_cfg, req.task_description
    )
    if not approved:
        return UiaActionResponse(
            success=False,
            status="cancelled",
            action=req.action,
            element_id=req.element_id,
            transport_status="not_sent",
            postcondition_status="not_checked",
            elapsed_ms=0,
            message=(
                "用户拒绝 agent 接管 UIA 操作"
                + (f"。用户反馈: {takeover_reason}" if takeover_reason else "")
            ),
        )

    # T15（决策 9）：watchdog 模式下 danger_level=confirm 自动跳过（与 execute_action 一致）
    should_force_confirm = req.require_confirm or (
        danger_level == "confirm" and current_mode != "watchdog"
    )
    if should_force_confirm:
        confirmed, confirm_reason = _confirm_control_operation(
            action=f"uia:{req.action}",
            text=req.element_text,
        )
        if not confirmed:
            return UiaActionResponse(
                success=False,
                status="cancelled",
                action=req.action,
                element_id=req.element_id,
                transport_status="not_sent",
                postcondition_status="not_checked",
                elapsed_ms=0,
                message=(
                    "用户取消了 UIA 操作"
                    + (f"。用户反馈: {confirm_reason}" if confirm_reason else "")
                ),
            )

    from server.screen.uia import execute_semantic_action

    # P1-2：把 expected 透传给 execute_semantic_action，uia_value_equals 内建后验在内部完成
    result = execute_semantic_action(
        element_id=req.element_id,
        snapshot_id=req.snapshot_id,
        action=req.action,
        value=req.value,
        direction=req.direction,
        amount=req.amount,
        expected=req.expected,
    )

    # 后验策略：
    # - uia_value_equals：已在 execute_semantic_action 内部处理（result 含 postcondition_status + postcondition_actual_value）
    # - 其他 expected 类型（ocr_contains 等）：路由层做 OCR 后验
    # - expected=None：保持 executed_unverified
    expected_type = (req.expected or {}).get("type") if req.expected else None
    if expected_type == "uia_value_equals":
        # 内建后验已填充 postcondition_status（可能为 verified/failed/error/not_checked）
        postcondition_status = result.get("postcondition_status") or "not_checked"
    elif not result.get("success"):
        postcondition_status = "not_checked"  # 动作未发送，不后验
    elif req.expected is None:
        postcondition_status = "executed_unverified"
    else:
        # 动作成功 + 非 uia_value_equals 的 expected → OCR 后验
        try:
            import io as _io

            from PIL import Image
            # 截图后验（全屏）
            png_bytes = _capture_fullscreen()
            if png_bytes:
                pil_image = Image.open(_io.BytesIO(png_bytes))
                postcondition_status = _verify_postcondition(req.expected, pil_image)
            else:
                postcondition_status = "error"
        except Exception as e:
            logger.warning(f"UIA action postcondition OCR 失败: {e}")
            postcondition_status = "error"

    # 合并 postcondition_status 到响应（覆盖内部 not_checked 默认值）
    result["postcondition_status"] = postcondition_status
    _touch_task_authorization_after_delivery(
        success=bool(result.get("success")),
        delivery_status=(
            "delivered" if result.get("transport_status") == "sent" else "unknown"
        ),
    )
    return UiaActionResponse(**result)
