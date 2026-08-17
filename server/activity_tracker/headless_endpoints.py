"""headless-agent-session REST 端点（Ticket 07）

spec: temp/sdd/headless-agent-session/spec.md
ticket: Ticket 07

端点：
- POST /headless/sessions/{session_id}/interrupt：中断 headless 会话（主会话或 judge 会话）

中断行为：
- 中断主会话 → run_main_agent 检测 interrupt_event.set() → SessionRunner 中断 →
  HeadlessSessionAction 检测 main_result.error 或 outcome.status=interrupted → 不调 judge → 整体 failed
- 中断 judge 会话 → run_judge_agent 检测 interrupt_event.set() → judge session 中断 →
  outcome.status != completed → 降级 uncertain → 整体 failed

设计要点：
- interrupt registry 是模块级 dict（headless_runner._headless_sessions），REST 端点跨请求访问
- asyncio.Event 必须在后端 event loop 中创建，REST 端点同 loop 内调用 event.set() 安全
- session 不存在时返回 404（幂等：重复中断已结束的 session 也返回 404）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema
from server.activity_tracker.headless_runner import (
    get_interrupt_event,
    interrupt_session,
)

router = APIRouter(prefix="/headless", tags=["headless"])


class InterruptResponse(BaseSchema):
    """POST /headless/sessions/{session_id}/interrupt 响应。"""
    session_id: str
    interrupted: bool
    message: str = ""


@router.post(
    "/sessions/{session_id}/interrupt",
    operation_id="headless_interrupt_session",
    response_model=InterruptResponse,
)
async def interrupt_headless_session(session_id: str) -> InterruptResponse:
    """中断 headless 会话（主会话或 judge 会话）。

    幂等：session 不存在或已结束时返回 404，重复调用安全。

    Args:
        session_id: headless 会话 ID（headless-{uuid} 或 headless-judge-{uuid}）

    Returns:
        InterruptResponse：含 interrupted=True/False + message

    Raises:
        HTTPException 404: session 不存在（未注册或已清理）
    """
    ev = get_interrupt_event(session_id)
    if ev is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"headless session {session_id!r} not found "
                "(not running or already finished)"
            ),
        )

    if ev.is_set():
        # 已中断过，幂等返回（不重复 set）
        return InterruptResponse(
            session_id=session_id,
            interrupted=False,
            message="session already interrupted",
        )

    ok = interrupt_session(session_id)
    if not ok:
        # 极端情况：get_interrupt_event 拿到了，interrupt_session 没拿到（并发清理）
        raise HTTPException(
            status_code=404,
            detail=f"headless session {session_id!r} disappeared during interrupt",
        )

    return InterruptResponse(
        session_id=session_id,
        interrupted=True,
        message="interrupt signal sent",
    )
