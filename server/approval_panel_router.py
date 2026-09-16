"""审批面板 HTTP 端点（/approvals/*）。

职责：
- 暴露面板进程需要的查询/上报接口
- 桥接 lib.approval_router（纯逻辑层）与 FastAPI

端点：
- GET  /approvals/status           → 心跳快照 + pending 计数
- GET  /approvals/pending           → pending 队列（含 seconds_left）
- POST /approvals/heartbeat         → 面板心跳上报
- POST /approvals/{id}/activity     → 用户操作重置该请求的 deadline
- POST /approvals/{id}/decision     → 用户决策（approve/deny）
- POST /approvals/{id}/ack          → 用户对已超时卡片点"收到"

注意：审批请求入队不在本路由——由 command_guard.run_gui_dialog 调用
lib.approval_router.route_approval 完成。本路由只负责面板侧的查询与上报。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.approval_router import heartbeat as _heartbeat
from lib.approval_router import router as _router
from lib.approval_router import store as _store
from lib.schema import BaseSchema

router = APIRouter(prefix="/approvals", tags=["审批面板"])


# ========== 请求/响应模型 ==========


class HeartbeatRequest(BaseSchema):
    panel_version: str = ""


class DecisionRequest(BaseSchema):
    decision: str = Field(pattern="^(approve|deny|timeout)$")
    feedback: str = ""


class SimpleResponse(BaseSchema):
    ok: bool = True


# ========== 端点 ==========


@router.get("/status", operation_id="approvals_status")
async def approvals_status() -> dict:
    """面板在线状态 + pending 计数。

    面板进程轮询此端点判断 server 是否可达；server 也通过此端点反向感知面板是否在线。
    """
    status = _heartbeat.get_status()
    # last_heartbeat 是 monotonic 时间戳，对客户端无意义，转 wall-clock 字符串
    last_hb = status.get("last_heartbeat", 0.0)
    if last_hb > 0:
        import time

        # monotonic 差值 + wall-clock now 推算（足够面板展示用）
        elapsed = _heartbeat.now() - last_hb
        wall_clock = time.time() - elapsed
        last_hb_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall_clock))
    else:
        last_hb_str = ""
    return {
        "panel_online": status["panel_online"],
        "pending_count": _store.pending_count(),
        "last_heartbeat": last_hb_str,
        "panel_version": status.get("panel_version", ""),
    }


@router.get("/pending", operation_id="approvals_pending")
async def approvals_pending() -> dict:
    """返回所有 pending 审批（FIFO 顺序）。"""
    items = _store.list_pending()
    return {"pending": [item.to_pending_dict() for item in items], "count": len(items)}


@router.post("/heartbeat", operation_id="approvals_heartbeat", response_model=SimpleResponse)
async def approvals_heartbeat(req: HeartbeatRequest) -> SimpleResponse:
    """面板心跳上报。"""
    _heartbeat.update_heartbeat(req.panel_version)
    return SimpleResponse(ok=True)


@router.post(
    "/{approval_id}/activity",
    operation_id="approvals_record_activity",
    response_model=SimpleResponse,
)
async def approvals_record_activity(approval_id: str) -> SimpleResponse:
    """用户在面板上操作（打字/点击）时上报，重置该请求的 server 端 deadline。

    用于避免用户在 feedback 框打字打到一半被超时。
    """
    if not _store.record_activity(approval_id):
        raise HTTPException(status_code=404, detail="审批请求不存在或已不可重置")
    return SimpleResponse(ok=True)


@router.post(
    "/{approval_id}/decision",
    operation_id="approvals_submit_decision",
    response_model=SimpleResponse,
)
async def approvals_submit_decision(approval_id: str, req: DecisionRequest) -> SimpleResponse:
    """用户提交决策（approve/deny/timeout）。

    - approve/deny：通知等待中的 run_gui_dialog（notify_decision）。
      approval_token 的签发由调用方 command_guard_request_approval / user_review_for_llm_deny
      在收到 await_decision 返回后调用 record_*_decision 完成——避免重复签发。
    - timeout：卡片内部倒计时归零时上报。调 notify_timeout 标记 store 状态为 "timeout"
      （而非 "decided"），让 await_decision 返回 timeout → 调用方返 408。
      卡片随后转为"已超时"只读态，用户点"收到"时走 /ack 端点。
    """
    item = _store.get(approval_id)
    if item is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    if item.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态为 {item.status}，不可提交决策")
    if req.decision == "timeout":
        # 卡片倒计时归零：标记 timeout 状态，唤醒 await_decision
        if not _router.notify_timeout(approval_id):
            raise HTTPException(status_code=409, detail="审批请求状态不可重置")
    else:
        # approve/deny：通知等待中的 await_decision（approval_token 留空，由调用方签发）
        if not _router.notify_decision(approval_id, req.decision, req.feedback, ""):
            raise HTTPException(status_code=409, detail="审批请求状态不可重置")
        # decided 条目由 store 在 add/list_pending 时惰性清理（超 expires_at 后移除），
        # 立即 remove 会破坏"窗口内重复提交→409"语义
    return SimpleResponse(ok=True)


@router.post(
    "/{approval_id}/ack",
    operation_id="approvals_ack_timeout",
    response_model=SimpleResponse,
)
async def approvals_ack_timeout(approval_id: str) -> SimpleResponse:
    """用户对已超时卡片点"收到"，移除该卡片。"""
    item = _store.get(approval_id)
    if item is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    if item.status != "timeout":
        raise HTTPException(status_code=409, detail=f"当前状态为 {item.status}，仅 timeout 状态可 ack")
    _store.ack_timeout(approval_id)
    # ack 后从队列移除（卡片消失）
    _store.remove(approval_id)
    return SimpleResponse(ok=True)
