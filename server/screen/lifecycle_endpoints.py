"""lifecycle 端点 — 应用列表/启动/等待 + 窗口解析/操作 + window_token 校验。

从 server/screen/routes.py 拆出（2026-07-24），减少主文件体积。
共享的 router 仍由 routes 提供，本模块定义：
- AppListResponse / AppLaunchRequest / AppLaunchResponse / AppWaitRequest / AppWaitResponse 模型
- WindowResolveRequest / WindowResolveResponse / WindowOpRequest / WindowOpResponse 模型
- _check_token_or_403 共享 helper（被 desktop_transaction 端点复用）
- app_list（/app/list）/ app_launch（/app/launch）/ app_wait（/app/wait）
- window_resolve（/window/resolve）
- window_minimize / window_restore / window_raise / window_close

导入本模块即触发 @router.post 注册，无需额外调用。
"""

from fastapi import HTTPException

from lib.schema import BaseSchema

# 窗口生命周期子模块（与 routes.py 顶部一致，使用别名以保持搬移代码不变）
from server.screen.lifecycle import (
    WINDOW_TOKEN_INVALID,
)
from server.screen.lifecycle import (
    close_window as _close_window,
)
from server.screen.lifecycle import (
    launch_app as _launch_app,
)
from server.screen.lifecycle import (
    list_apps as _list_apps,
)
from server.screen.lifecycle import (
    minimize_window as _minimize_window,
)
from server.screen.lifecycle import (
    raise_window as _raise_window,
)
from server.screen.lifecycle import (
    resolve_window as _resolve_window,
)
from server.screen.lifecycle import (
    restore_window as _restore_window,
)
from server.screen.lifecycle import (
    verify_window_token as _verify_window_token,
)
from server.screen.lifecycle import (
    wait_for_window as _wait_for_window,
)

# 从主模块复用共享件（router）
from server.screen.routes import (
    _confirm_control_operation,
    _enforce_session_permission,
    _touch_task_authorization_after_delivery,
    router,
)

# ========== P1-B：窗口生命周期 API（app_list/app_launch/app_wait/window_resolve + 窗口操作 + token 失效） ==========


class AppListResponse(BaseSchema):
    success: bool
    apps: list[dict]
    count: int
    message: str = ""
    app_lessons_hint: str | None = None  # 软件经验提示（命中 apps/*.md 时注入）


class AppLaunchRequest(BaseSchema):
    command: str
    args: list[str] | None = None
    working_dir: str | None = None


class AppLaunchResponse(BaseSchema):
    success: bool
    pid: int
    message: str


class AppWaitRequest(BaseSchema):
    title: str | None = None
    process_name: str | None = None
    pid: int | None = None
    timeout: float = 10.0
    poll_interval: float = 0.3


class AppWaitResponse(BaseSchema):
    success: bool
    status: str  # found | timeout | ambiguous
    matches: list[dict]
    elapsed_ms: int
    message: str


class WindowResolveRequest(BaseSchema):
    title: str | None = None
    process_name: str | None = None
    pid: int | None = None
    automation_id: str | None = None


class WindowResolveResponse(BaseSchema):
    success: bool
    status: str  # found | WINDOW_AMBIGUOUS | WINDOW_NOT_FOUND
    matches: list[dict]
    message: str


class WindowOpRequest(BaseSchema):
    hwnd: int
    window_token: dict | None = None  # 可选：验证 token 是否仍有效后再操作
    force: bool = False  # 仅 close 支持：True=直接杀进程（会丢失未保存数据）


class WindowOpResponse(BaseSchema):
    success: bool
    status: str  # executed | WINDOW_TOKEN_INVALID | modal_blocking | failed
    message: str
    post_state: dict
    token_invalid_reason: str | None = None
    modal_info: dict | None = None  # close 特有：模态对话框处理详情（status=modal_blocking 时填充）


@router.get("/app/list", response_model=AppListResponse, operation_id="screen_app_list")
def app_list():
    """列出当前运行的应用（按 process_name 聚合）。

    与 list_windows 区别：windows 列每个窗口，apps 按 process 聚合，
    便于 agent 决定调用 app_launch 还是直接 window_resolve。

    响应含 app_lessons_hint：若运行的进程中有命中
    .agents/skills/computer_use/apps/*.md 软件经验文件的，自动注入提示。
    agent 看到 hint 后应调 screen_match_app 查询完整经验。
    """
    apps = _list_apps()
    # 自动注入软件经验提示
    hint = None
    try:
        from server.screen.app_lessons import build_app_lessons_hint, match_app_for_process_names
        process_names = [a.get("process_name", "") for a in apps if a.get("process_name")]
        lessons = match_app_for_process_names(process_names)
        hint = build_app_lessons_hint(lessons)
    except Exception:
        pass
    return AppListResponse(success=True, apps=apps, count=len(apps), app_lessons_hint=hint)


@router.post("/app/launch", response_model=AppLaunchResponse, operation_id="screen_app_launch")
def app_launch(req: AppLaunchRequest):
    """启动应用（非阻塞；如需等待窗口出现用 /screen/app/wait）。

    安全：shell=False，command 必须是可执行文件路径或 PATH 中的名称。
    """
    # T15：副作用端点 403 拦截（启动进程属副作用操作）
    _enforce_session_permission()
    result = _launch_app(req.command, req.args, req.working_dir)
    # T15：副作用成功后 extend idle 计时（与其他副作用端点保持一致）
    _touch_task_authorization_after_delivery(success=bool(result["success"]))
    return AppLaunchResponse(
        success=result["success"],
        pid=result["pid"],
        message=result["message"],
    )


@router.post("/app/wait", response_model=AppWaitResponse, operation_id="screen_app_wait")
def app_wait(req: AppWaitRequest):
    """轮询等待窗口出现（agent 启动应用后用此端点等窗口就绪）。

    任意条件命中即返回。多匹配时返回 status=ambiguous（应再 window_resolve 缩小范围）。
    """
    result = _wait_for_window(
        title=req.title,
        process_name=req.process_name,
        pid=req.pid,
        timeout=req.timeout,
        poll_interval=req.poll_interval,
    )
    return AppWaitResponse(
        success=result["success"],
        status=result["status"],
        matches=result["matches"],
        elapsed_ms=result["elapsed_ms"],
        message=result["message"],
    )


@router.post("/window/resolve", response_model=WindowResolveResponse, operation_id="screen_window_resolve")
def window_resolve(req: WindowResolveRequest):
    """按 title/process/pid/automation_id 解析窗口，多匹配返回候选而非随便选择。

    返回的 matches 中每个窗口含 window_token（canonical_hwnd + pid + process_create_time），
    后续 window 操作可传 window_token 验证窗口是否仍为同一实例（进程重启后 token 失效）。
    """
    if not any([req.title, req.process_name, req.pid, req.automation_id]):
        raise HTTPException(
            status_code=400,
            detail="必须提供至少一个查询条件：title / process_name / pid / automation_id",
        )
    result = _resolve_window(
        title=req.title,
        process_name=req.process_name,
        pid=req.pid,
        automation_id=req.automation_id,
    )
    return WindowResolveResponse(
        success=result["success"],
        status=result["status"],
        matches=result["matches"],
        message=result["message"],
    )


def _check_token_or_403(window_token: dict | None) -> str | None:
    """校验 window_token（如提供）；返回 None 表示通过，否则返回失败原因。"""
    if window_token is None:
        return None  # 未提供 token 不校验（向后兼容）
    valid, reason = _verify_window_token(window_token)
    if valid:
        return None
    return reason


@router.post("/window/minimize", response_model=WindowOpResponse, operation_id="screen_window_minimize")
def window_minimize(req: WindowOpRequest):
    """最小化窗口。如提供 window_token 则先验证 token 有效性。"""
    # T15：副作用端点 403 拦截
    _enforce_session_permission()
    invalid_reason = _check_token_or_403(req.window_token)
    if invalid_reason is not None:
        return WindowOpResponse(
            success=False,
            status=WINDOW_TOKEN_INVALID,
            message=f"window token 失效: {invalid_reason}",
            post_state={"hwnd": req.hwnd, "exists": False},
            token_invalid_reason=invalid_reason,
        )
    result = _minimize_window(req.hwnd)
    _touch_task_authorization_after_delivery(success=bool(result["success"]))
    return WindowOpResponse(
        success=result["success"],
        status="executed" if result["success"] else "failed",
        message=result["message"],
        post_state=result["post_state"],
    )


@router.post("/window/restore", response_model=WindowOpResponse, operation_id="screen_window_restore")
def window_restore(req: WindowOpRequest):
    """恢复窗口（从最小化还原）。如提供 window_token 则先验证 token 有效性。"""
    # T15：副作用端点 403 拦截
    _enforce_session_permission()
    invalid_reason = _check_token_or_403(req.window_token)
    if invalid_reason is not None:
        return WindowOpResponse(
            success=False,
            status=WINDOW_TOKEN_INVALID,
            message=f"window token 失效: {invalid_reason}",
            post_state={"hwnd": req.hwnd, "exists": False},
            token_invalid_reason=invalid_reason,
        )
    result = _restore_window(req.hwnd)
    _touch_task_authorization_after_delivery(success=bool(result["success"]))
    return WindowOpResponse(
        success=result["success"],
        status="executed" if result["success"] else "failed",
        message=result["message"],
        post_state=result["post_state"],
    )


@router.post("/window/raise", response_model=WindowOpResponse, operation_id="screen_window_raise")
def window_raise(req: WindowOpRequest):
    """置前窗口（激活到前台）。如提供 window_token 则先验证 token 有效性。"""
    # T15：副作用端点 403 拦截
    _enforce_session_permission()
    invalid_reason = _check_token_or_403(req.window_token)
    if invalid_reason is not None:
        return WindowOpResponse(
            success=False,
            status=WINDOW_TOKEN_INVALID,
            message=f"window token 失效: {invalid_reason}",
            post_state={"hwnd": req.hwnd, "exists": False},
            token_invalid_reason=invalid_reason,
        )
    result = _raise_window(req.hwnd)
    _touch_task_authorization_after_delivery(success=bool(result["success"]))
    return WindowOpResponse(
        success=result["success"],
        status="executed" if result["success"] else "failed",
        message=result["message"],
        post_state=result["post_state"],
    )


@router.post("/window/close", response_model=WindowOpResponse, operation_id="screen_window_close")
def window_close(req: WindowOpRequest):
    """关闭窗口。

    第三轮评估后续修复：之前窗口仍存在时返回 success=True 仅靠 message 警告是谎报。
    现在严格按 post_state.exists 判定 success，并新增 modal_blocking 状态。

    - force=False（默认，优雅关闭）：
        1) 发 WM_CLOSE
        2) 轮询等待窗口消失
        3) 若仍存在，自动检测并处理"是否保存"模态对话框
           - UIA 找"不保存(N)"/"Don't Save"按钮 invoke
           - 回退 PostMessage WM_COMMAND IDNO (wParam=7)（Win32 标准 IDNO=7）
        4) success=True ⟺ post_state.exists=False
        5) 模态未处理成功 → success=False, status=modal_blocking, modal_info 透传详情

    - force=True（强制杀进程，会丢失未保存数据）：
        通过 GetWindowThreadProcessId 拿 pid + OpenProcess(PROCESS_TERMINATE) +
        TerminateProcess 直接终止进程。force 永远走杀进程路径，不走 WM_CLOSE。

    如提供 window_token 则先验证 token 有效性。close 后 hwnd 失效，post_state.exists 应为 False。
    """
    # T15：副作用端点 403 拦截
    _enforce_session_permission()
    invalid_reason = _check_token_or_403(req.window_token)
    if invalid_reason is not None:
        return WindowOpResponse(
            success=False,
            status=WINDOW_TOKEN_INVALID,
            message=f"window token 失效: {invalid_reason}",
            post_state={"hwnd": req.hwnd, "exists": False},
            token_invalid_reason=invalid_reason,
        )
    operation_text = "强制关闭窗口（可能丢失未保存数据）" if req.force else "关闭窗口"
    confirmed, confirm_reason = _confirm_control_operation(
        action="screen_window_close",
        text=operation_text,
    )
    if not confirmed:
        return WindowOpResponse(
            success=False,
            status="cancelled",
            message=(
                "用户取消了窗口关闭操作"
                + (f"。用户反馈: {confirm_reason}" if confirm_reason else "")
            ),
            post_state={"hwnd": req.hwnd, "exists": True},
        )
    result = _close_window(req.hwnd, force=req.force)
    _touch_task_authorization_after_delivery(success=bool(result["success"]))
    return WindowOpResponse(
        success=result["success"],
        status=result.get("status", "executed" if result["success"] else "failed"),
        message=result["message"],
        post_state=result["post_state"],
        modal_info=result.get("modal_info"),
    )
