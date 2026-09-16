"""浏览器弹窗处理端点（spec: browser-dialog-handling）。

2026-08-06 新增。提供 handle_dialog 端点，让 agent 能主动 accept/dismiss
队列中的 dialog 并输入 prompt 文本。替代 wait_and_action 的 dialog_action
参数（后者在 session 模式下仅作意图声明，无法真正 accept/prompt 输入）。

依赖 Ticket 01 的 _on_dialog 改造：dialog 不再立即 dismiss，保留 Dialog
对象引用供本端点调 accept/dismiss。

Ticket 03 扩展：新增 `_check_dialog_block(sess)` helper，所有操作端点
（action/navigate/screenshot/snapshot/evaluate/wait_for/wait_and_action）
在 session 模式下执行前预检，有 pending dialog 时直接返回
`blocked_by_dialog: true` + dialog 信息，不调 page 操作（被动反馈通道）。
"""

from __future__ import annotations

import logging
from typing import Literal

from lib.schema import BaseSchema

from .routes import router  # noqa: E402  （循环导入安全：routes.py 末尾才 import 本模块）

logger = logging.getLogger("localagent.browser_dialog")


# ========== 共享 helper（Ticket 03） ==========


def _check_dialog_block(sess) -> dict | None:
    """预检 session 是否被 pending dialog 阻塞。

    Returns:
        - None：无 dialog 阻塞（可继续执行 page 操作）
        - dict：有 dialog 阻塞，结构为
            {
                "blocked_by_dialog": True,
                "blocking_dialog": {
                    "type": "alert" / "confirm" / "prompt" / "beforeunload",
                    "message": "...",
                    "page_url": "...",
                    "ts": float,
                    "auto_dismissed": False,
                    "session_id": "...",  # 哪个 session 被阻塞
                },
            }

    判断依据：sess.has_pending_dialog()（last_dialog_obj 非 None 且未被超时兜底 dismiss）。
    队列空或 dialog 已被处理/超时兜底 → 返回 None（不阻塞）。
    """
    if sess is None:
        return None
    if not sess.has_pending_dialog():
        return None
    # 取队列中第一个未 auto_dismissed 的事件作为阻塞 dialog
    blocking = None
    for ev in sess.pending_dialogs:
        if not ev.get("auto_dismissed"):
            blocking = ev
            break
    if blocking is None:
        # has_pending_dialog() 为 True 但队列无未 dismissed 事件（极端竞态）→ 不阻塞
        return None
    return {
        "blocked_by_dialog": True,
        "blocking_dialog": {
            "type": blocking.get("type"),
            "message": blocking.get("message"),
            "page_url": blocking.get("page_url"),
            "ts": blocking.get("ts"),
            "auto_dismissed": blocking.get("auto_dismissed", False),
            "session_id": sess.session_id,
        },
    }


# ========== 请求/响应模型 ==========


class BrowserHandleDialogRequest(BaseSchema):
    """handle_dialog �请求：手动处理队列中的 JS dialog。

    - session_id: 必传，确定哪个 tab 的 dialog 队列
    - action: "accept"（点确定）或 "dismiss"（点取消）
    - prompt_text: prompt 类型对话框输入的文本（action="accept" 时生效）
    """
    session_id: str
    action: Literal["accept", "dismiss"]
    prompt_text: str | None = None


class BrowserHandleDialogResponse(BaseSchema):
    """handle_dialog 响应。

    - handled: 是否成功处理（accept/dismiss 调用成功）
    - dialog: 被处理的 dialog 信息（type/message/page_url/ts/auto_dismissed）
    - error: 错误信息（DIALOG_NOT_FOUND / DIALOG_ALREADY_DISMISSED / TAB_NOT_FOUND）
    """
    handled: bool = False
    dialog: dict | None = None
    error: dict | None = None


# ========== 端点 ==========


@router.post(
    "/handle_dialog",
    response_model=BrowserHandleDialogResponse,
    operation_id="browser_handle_dialog",
)
async def browser_handle_dialog(req: BrowserHandleDialogRequest):
    """手动处理队列中的 JS dialog（accept/dismiss + prompt 文本输入）。

    非阻塞：队列无 dialog 立即返回 DIALOG_NOT_FOUND（timeout=0）。
    agent 应先调 browser_wait_for(wait_type="dialog") 等 dialog 出现，再调本端点。

    使用场景：
    - confirm 对话框点"确定"（action="accept"）
    - prompt 对话框输入文本（action="accept", prompt_text="hello"）
    - alert 对话框关闭（action="dismiss" 或 "accept" 效果相同）
    - beforeunload 对话框跳过（action="accept"）

    错误码：
    - TAB_NOT_FOUND: session 不存在
    - DIALOG_NOT_FOUND: dialog 队列为空（无 dialog 触发或已被消费）
    - DIALOG_ALREADY_DISMISSED: dialog 已被超时兜底自动 dismiss（5 分钟未处理）
    """
    from .error_model import BROWSER_ERROR_CODES
    from .session.manager import get_session_manager

    mgr = await get_session_manager()
    sess = mgr.get_session(req.session_id)
    if sess is None:
        return BrowserHandleDialogResponse(
            handled=False,
            error={
                "error_code": "TAB_NOT_FOUND",
                "error_message": BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                "phase": "locate",
                "debug_detail": f"session_id={req.session_id} 不存在",
            },
        )

    # 取队列首部 dialog
    pending = sess.pop_pending_dialog()
    if pending is None:
        return BrowserHandleDialogResponse(
            handled=False,
            error={
                "error_code": "DIALOG_NOT_FOUND",
                "error_message": BROWSER_ERROR_CODES["DIALOG_NOT_FOUND"],
                "phase": "locate",
                "debug_detail": "dialog 队列为空，无 dialog 可处理",
            },
        )

    # 检查 Dialog 对象是否仍可用（未被超时兜底 dismiss）
    dlg = sess.last_dialog_obj
    if dlg is None or sess.dialog_auto_dismissed:
        return BrowserHandleDialogResponse(
            handled=False,
            dialog=pending,
            error={
                "error_code": "DIALOG_ALREADY_DISMISSED",
                "error_message": BROWSER_ERROR_CODES["DIALOG_ALREADY_DISMISSED"],
                "phase": "act",
                "debug_detail": "dialog 已被超时兜底自动 dismiss，无法再 accept/dismiss",
            },
        )

    # 主动处理 dialog
    try:
        if req.action == "accept":
            if pending.get("type") == "prompt":
                await dlg.accept(req.prompt_text or "")
            else:
                await dlg.accept()
        else:
            await dlg.dismiss()
        # 清理：Dialog 对象处理后清空，避免悬空引用
        sess.last_dialog_obj = None
        # dialog_auto_dismissed 保持 False（handle_dialog 处理的，不是超时兜底）
        logger.info(
            "handle_dialog: %s dialog 已 %s（session=%s, message=%s）",
            pending.get("type"), req.action, req.session_id[:8],
            pending.get("message", "")[:50],
        )
        return BrowserHandleDialogResponse(handled=True, dialog=pending)
    except Exception as e:
        # Dialog 处理失败（可能已被其他路径处理，或 page 已关闭）
        logger.warning("handle_dialog: 处理失败 %s", e)
        sess.last_dialog_obj = None
        return BrowserHandleDialogResponse(
            handled=False,
            dialog=pending,
            error={
                "error_code": "EXECUTION_ERROR",
                "error_message": BROWSER_ERROR_CODES["EXECUTION_ERROR"],
                "phase": "act",
                "debug_detail": f"accept/dismiss 调用失败: {e}",
            },
        )


# ========== Ticket 05: set_http_credentials 端点 ==========


class BrowserSetHttpCredentialsRequest(BaseSchema):
    """set_http_credentials 请求：动态设置 HTTP Basic Auth 凭证。

    作用域：context 级（整个浏览器实例），非 per-tab。
    session_id 用于触发 SessionManager 重连（凭证实际存在 context 上）。
    host 字段仅作记录用（Playwright set_http_credentials 不支持按 host 分配，
    会作用于所有 401 响应）。clear=True 时清除已设置的凭证。
    """
    session_id: str
    host: str | None = None  # 仅作记录用（Playwright context 级凭证不区分 host）
    username: str | None = None
    password: str | None = None
    clear: bool = False


class BrowserSetHttpCredentialsResponse(BaseSchema):
    success: bool
    error: dict | None = None


@router.post(
    "/set_http_credentials",
    response_model=BrowserSetHttpCredentialsResponse,
    operation_id="browser_set_http_credentials",
)
async def browser_set_http_credentials(req: BrowserSetHttpCredentialsRequest):
    """动态设置 HTTP Basic Auth 凭证（context 级），解决 401 WWW-Authenticate 阻塞。

    ⚠️ 已失效（4-6）：Playwright 1.44+ 移除了 BrowserContext.set_http_credentials，
    本端点调用在运行时必然抛 AttributeError 返回 EXECUTION_ERROR，且 401 场景实际
    无解。已从 MCP 网关摘除（mcp_whitelist.GATEWAY_EXCLUDE），端点保留待凭证注入
    方案落地后恢复，请勿调用。

    凭证存 session 内存（Playwright BrowserContext），不落盘。
    clear=True 时清除（传 None 给 Playwright）。

    安全：
    - 密码不进日志（logger 只记录 username 长度和 host，不记录密码）
    - 调用方需自行确保只在需要时设置，使用后建议 clear

    使用场景：
    - 站点返回 401 + WWW-Authenticate，浏览器原生对话框阻塞后续操作
    - agent 预先设置凭证后再 navigate，避免 401 阻塞
    """
    from .error_model import BROWSER_ERROR_CODES
    from .session.manager import get_session_manager

    mgr = await get_session_manager()
    # session_id 用于触发 SessionManager 重连（即使 session 不存在也会重连）
    # 但若 session_id 不存在，可能 browser 未连接，此时返回 BROWSER_DISCONNECTED
    sess = mgr.get_session(req.session_id)
    if sess is None:
        # session 不存在，但仍可尝试获取 context（可能 browser 已连接但 session 未创建）
        # 检查 browser 是否连接
        if not mgr.is_connected:  # property，非方法
            return BrowserSetHttpCredentialsResponse(
                success=False,
                error={
                    "error_code": "BROWSER_DISCONNECTED",
                    "error_message": BROWSER_ERROR_CODES["BROWSER_DISCONNECTED"],
                    "phase": "connect",
                    "debug_detail": f"session_id={req.session_id} 不存在且浏览器未连接",
                },
            )

    context = await mgr.get_context()
    if context is None:
        return BrowserSetHttpCredentialsResponse(
            success=False,
            error={
                "error_code": "BROWSER_DISCONNECTED",
                "error_message": BROWSER_ERROR_CODES["BROWSER_DISCONNECTED"],
                "phase": "connect",
                "debug_detail": "无法获取 BrowserContext（浏览器未连接或无 context）",
            },
        )

    try:
        # Playwright 1.44+ 已移除 BrowserContext.set_http_credentials（无法动态修改
        # context 凭证，需在 browser.new_context(http_credentials=...) 创建时注入）。
        # 此处调用在运行时必然抛 AttributeError，由下方 except 捕获返回错误；
        # 未来接入凭证注入时移除这两处 type: ignore[attr-defined]。
        if req.clear:
            # 清除凭证
            await context.set_http_credentials(None)  # type: ignore[attr-defined]
            logger.info(
                "set_http_credentials: 已清除凭证（session=%s, host=%s）",
                req.session_id[:8] if req.session_id else "N/A",
                req.host or "ALL",
            )
        else:
            # 设置凭证（密码不进日志）
            if not req.username or not req.password:
                return BrowserSetHttpCredentialsResponse(
                    success=False,
                    error={
                        "error_code": "INVALID_SELECTOR",
                        "error_message": BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                        "phase": "act",
                        "debug_detail": "clear=False 时 username 和 password 必填",
                    },
                )
            await context.set_http_credentials({  # type: ignore[attr-defined]
                "username": req.username,
                "password": req.password,
            })
            logger.info(
                "set_http_credentials: 已设置凭证（session=%s, host=%s, username_len=%d）",
                req.session_id[:8] if req.session_id else "N/A",
                req.host or "ALL",
                len(req.username),
            )
        return BrowserSetHttpCredentialsResponse(success=True)
    except Exception as e:
        logger.warning("set_http_credentials: 调用失败 %s", e)
        return BrowserSetHttpCredentialsResponse(
            success=False,
            error={
                "error_code": "EXECUTION_ERROR",
                "error_message": BROWSER_ERROR_CODES["EXECUTION_ERROR"],
                "phase": "act",
                "debug_detail": f"set_http_credentials 调用失败: {e}",
            },
        )


# ========== Ticket 06: grant_permissions 端点 ==========


# Playwright 支持的 permissions 集合（BrowserContext.grant_permissions）
_SUPPORTED_PERMISSIONS = {
    "geolocation", "notifications", "camera", "microphone",
    # 以下为 Chromium 扩展支持
    "clipboard-read", "clipboard-write",
}


class BrowserGrantPermissionsRequest(BaseSchema):
    """grant_permissions 请求：显式预授权权限，避免站点请求权限时弹原生对话框。

    主动预授权语义，非事件驱动。permissions 列表中的权限会被立即授予，
    站点后续请求这些权限时不再弹出原生权限对话框。

    - session_id: 必传，用于触发重连 + 获取 origin（origin=None 时）
    - permissions: 权限列表（geolocation/notifications/camera/microphone 等）
    - origin: 可选，限定 origin；None 时用当前 session page 的 origin
    """
    session_id: str
    permissions: list[str]
    origin: str | None = None


class BrowserGrantPermissionsResponse(BaseSchema):
    success: bool
    granted: list[str] = []
    error: dict | None = None


@router.post(
    "/grant_permissions",
    response_model=BrowserGrantPermissionsResponse,
    operation_id="browser_grant_permissions",
)
async def browser_grant_permissions(req: BrowserGrantPermissionsRequest):
    """显式预授权权限（geolocation/notifications/camera 等）。

    站点请求权限时不再弹原生对话框。主动预授权语义，非事件驱动。

    支持的 permissions：geolocation / notifications / camera / microphone
    （及 Chromium 扩展的 clipboard-read / clipboard-write）。

    - origin=None 时用当前 session page 的 origin
    - 已授予的权限再次授予是幂等的

    使用场景：
    - 站点请求地理位置权限弹原生对话框阻塞 → 预先 grant_permissions(["geolocation"])
    - 站点请求通知权限弹原生对话框 → 预先 grant_permissions(["notifications"])
    """
    from .error_model import BROWSER_ERROR_CODES
    from .session.manager import get_session_manager

    # 校验 permissions 非空
    if not req.permissions:
        return BrowserGrantPermissionsResponse(
            success=False,
            error={
                "error_code": "INVALID_SELECTOR",
                "error_message": BROWSER_ERROR_CODES["INVALID_SELECTOR"],
                "phase": "act",
                "debug_detail": "permissions 列表不能为空",
            },
        )

    # 校验 permissions 都在支持集合内
    unsupported = [p for p in req.permissions if p not in _SUPPORTED_PERMISSIONS]
    if unsupported:
        return BrowserGrantPermissionsResponse(
            success=False,
            error={
                "error_code": "UNSUPPORTED_ACTION",
                "error_message": BROWSER_ERROR_CODES["UNSUPPORTED_ACTION"],
                "phase": "act",
                "debug_detail": (
                    f"不支持的 permissions: {unsupported}。"
                    f"支持集合: {sorted(_SUPPORTED_PERMISSIONS)}"
                ),
            },
        )

    mgr = await get_session_manager()
    sess = mgr.get_session(req.session_id)
    if sess is None:
        return BrowserGrantPermissionsResponse(
            success=False,
            error={
                "error_code": "TAB_NOT_FOUND",
                "error_message": BROWSER_ERROR_CODES["TAB_NOT_FOUND"],
                "phase": "locate",
                "debug_detail": f"session_id={req.session_id} 不存在",
            },
        )

    # origin=None 时从 session page 推导
    origin = req.origin
    if origin is None:
        try:
            page_url = sess.page.url if sess.page and not sess.page.is_closed() else sess.url
            # 解析 origin（scheme + host + port）
            from urllib.parse import urlparse
            parsed = urlparse(page_url)
            origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
        except Exception:
            origin = None

    context = await mgr.get_context()
    if context is None:
        return BrowserGrantPermissionsResponse(
            success=False,
            error={
                "error_code": "BROWSER_DISCONNECTED",
                "error_message": BROWSER_ERROR_CODES["BROWSER_DISCONNECTED"],
                "phase": "connect",
                "debug_detail": "无法获取 BrowserContext（浏览器未连接或无 context）",
            },
        )

    try:
        # Playwright grant_permissions 签名：grant_permissions(permissions, origin=None)
        # origin=None 时不限定 origin（作用于所有 origin）
        await context.grant_permissions(req.permissions, origin=origin)
        logger.info(
            "grant_permissions: 已授予 %s（session=%s, origin=%s）",
            req.permissions, req.session_id[:8], origin or "ALL",
        )
        return BrowserGrantPermissionsResponse(
            success=True,
            granted=list(req.permissions),
        )
    except Exception as e:
        logger.warning("grant_permissions: 调用失败 %s", e)
        return BrowserGrantPermissionsResponse(
            success=False,
            error={
                "error_code": "EXECUTION_ERROR",
                "error_message": BROWSER_ERROR_CODES["EXECUTION_ERROR"],
                "phase": "act",
                "debug_detail": f"grant_permissions 调用失败: {e}",
            },
        )
