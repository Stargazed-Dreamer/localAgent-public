"""路由决策：审批请求走面板还是走原 subprocess 弹窗。

纯逻辑层。route_approval 同步决策，await_decision async 等待面板用户响应。
支持 activity 信号重置超时 deadline（用户打字时不超时）。
"""
from __future__ import annotations

import asyncio

from lib.approval_router import heartbeat as _heartbeat
from lib.approval_router import store as _store
from lib.approval_router.store import ApprovalItem

# 面板在线路径的最大等待上限（防永远不超时，24h）
DEFAULT_MAX_TOTAL_TIMEOUT = 86400.0

# 等待中的 Event（approval_id → asyncio.Event）
# notify_decision 时 set()，await_decision 检查 deadline 后继续等
_events: dict[str, asyncio.Event] = {}


def route_approval(
    approval_id: str, payload: dict, base_timeout: float
) -> dict:
    """路由决策：面板在线则入队，否则返回 fallback。

    参数：
        approval_id: 审批请求的唯一 ID（由调用方 create_pending 生成）
        payload: 审批请求的 payload（含 type/method/path/command 等）
        base_timeout: 基础超时秒数（gui_timeout + 20，server 端兜底）

    返回：
        面板在线：{"route": "panel", "approval_id": "..."}
        面板离线：{"route": "fallback"}
    """
    if not _heartbeat.is_panel_online():
        return {"route": "fallback"}

    item = ApprovalItem(
        approval_id=approval_id,
        item_type=payload.get("type", "shell"),
        payload=payload,
        created_at=_heartbeat.now(),
        base_timeout=base_timeout,
    )
    _store.add(item)
    return {"route": "panel", "approval_id": approval_id}


async def await_decision(
    approval_id: str,
    base_timeout: float,
    max_total_timeout: float = DEFAULT_MAX_TOTAL_TIMEOUT,
) -> dict:
    """等待面板用户决策。支持 activity 重置 deadline。

    返回：
        用户决策：{"decision": "approve"|"deny", "feedback": str, "approval_token": str}
        超时：{"decision": "timeout", "feedback": "", "approval_token": ""}
        请求不存在：{"decision": "timeout", "feedback": "", "approval_token": ""}
    """
    event = asyncio.Event()
    _events[approval_id] = event

    item = _store.get(approval_id)
    if item is None:
        _events.pop(approval_id, None)
        return {"decision": "timeout", "feedback": "", "approval_token": ""}

    hard_deadline = item.created_at + max_total_timeout

    try:
        while True:
            now = _heartbeat.now()
            activity_deadline = item.last_activity + base_timeout
            deadline = min(hard_deadline, activity_deadline)
            remaining = deadline - now

            if remaining <= 0:
                _store.mark_timeout(approval_id)
                return {"decision": "timeout", "feedback": "", "approval_token": ""}

            # 检查是否已决策（可能被 notify_decision 在 await 间隙设置）
            if item.status != "pending":
                return {
                    "decision": item.decision,
                    "feedback": item.feedback,
                    "approval_token": item.approval_token,
                }

            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
                # event 被 set（用户决策了）
                return {
                    "decision": item.decision,
                    "feedback": item.feedback,
                    "approval_token": item.approval_token,
                }
            except TimeoutError:
                # deadline 可能被 activity 重置，循环重新检查
                # 或者是真的超时了
                if item.status != "pending":
                    return {
                        "decision": item.decision,
                        "feedback": item.feedback,
                        "approval_token": item.approval_token,
                    }
                continue
    finally:
        _events.pop(approval_id, None)


def notify_decision(
    approval_id: str, decision: str, feedback: str, approval_token: str = ""
) -> bool:
    """通知等待中的 await_decision 用户已决策。

    由 server 端 /approvals/{id}/decision 端点调用。
    """
    ok = _store.set_decision(approval_id, decision, feedback, approval_token)
    if not ok:
        return False
    event = _events.get(approval_id)
    if event is not None:
        event.set()
    return True


def notify_timeout(approval_id: str) -> bool:
    """标记审批为已超时（面板端倒计时归零时调用）。

    与 server 端 deadline 超时不同：这是面板内部倒计时归零主动上报。
    """
    ok = _store.mark_timeout(approval_id)
    if not ok:
        return False
    event = _events.get(approval_id)
    if event is not None:
        event.set()
    return True


def reset() -> None:
    """重置所有状态（测试用）。"""
    _store.reset()
    _heartbeat.reset()
    _events.clear()
