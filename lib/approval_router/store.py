"""审批队列内存存储。

纯逻辑层，线程安全（threading.Lock）。server 重启则队列丢失（设计行为，决策 8）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from lib.approval_router import heartbeat as _heartbeat


@dataclass
class ApprovalItem:
    """单个审批请求的状态。"""

    approval_id: str
    item_type: str  # "shell" | "http"
    payload: dict[str, Any]  # 含 method/path/command/body_preview/guard_reason/agent_reason/llm_opinion
    created_at: float
    base_timeout: float  # 基础超时（gui_timeout + 20，server 端兜底）
    status: str = "pending"  # pending | decided | timeout | acked
    decision: str = ""  # approve | deny | timeout
    feedback: str = ""
    approval_token: str = ""
    last_activity: float = 0.0  # 最后活动时间（activity 重置 deadline）

    def __post_init__(self) -> None:
        if self.last_activity == 0.0:
            self.last_activity = self.created_at

    @property
    def expires_at(self) -> float:
        """基于 last_activity 的动态过期时间（activity 重置后延长）。"""
        return self.last_activity + self.base_timeout

    def seconds_left(self) -> int:
        """剩余秒数（用于进度条 / pending 响应）。"""
        return max(0, int(self.expires_at - _heartbeat.now()))

    def to_pending_dict(self) -> dict:
        """转换为 GET /approvals/pending 的响应项。"""
        return {
            "approval_id": self.approval_id,
            "type": self.item_type,
            "method": self.payload.get("method", ""),
            "path": self.payload.get("path", ""),
            "command": self.payload.get("command", ""),
            "body_preview": self.payload.get("body_preview", ""),
            "guard_reason": self.payload.get("guard_reason", ""),
            "agent_reason": self.payload.get("agent_reason", ""),
            "llm_opinion": self.payload.get("llm_opinion", ""),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "seconds_left": self.seconds_left(),
            "status": self.status,
        }


_store_lock = threading.Lock()
_items: dict[str, ApprovalItem] = {}


def _purge_finished_expired_locked() -> None:
    """惰性清理已终结且超过 deadline 的条目（decided/acked），防长期运行内存缓涨。

    保留条目至 expires_at（last_activity + base_timeout）：此窗口内决策端点仍能查到
    条目并返回 409（重复提交/对 decided 项 ack 的既有语义），超期后才移除。
    timeout 状态不清理（等待用户 ack 的正常路径）。
    """
    now = _heartbeat.now()
    expired = [
        aid
        for aid, item in _items.items()
        if item.status in ("decided", "acked") and item.expires_at <= now
    ]
    for aid in expired:
        _items.pop(aid, None)


def add(item: ApprovalItem) -> None:
    """入队一个审批请求。"""
    with _store_lock:
        _purge_finished_expired_locked()
        _items[item.approval_id] = item


def get(approval_id: str) -> ApprovalItem | None:
    with _store_lock:
        return _items.get(approval_id)


def remove(approval_id: str) -> bool:
    """移除一个审批请求（用户决策后或点"收到"后）。"""
    with _store_lock:
        return _items.pop(approval_id, None) is not None


def list_pending() -> list[ApprovalItem]:
    """列出所有 pending 状态的审批（按创建时间 FIFO）。"""
    with _store_lock:
        _purge_finished_expired_locked()
        pending = [item for item in _items.values() if item.status == "pending"]
    pending.sort(key=lambda x: x.created_at)
    return pending


def list_all() -> list[ApprovalItem]:
    """列出所有审批（含已超时未 ack 的）。"""
    with _store_lock:
        items = list(_items.values())
    items.sort(key=lambda x: x.created_at)
    return items


def mark_timeout(approval_id: str) -> bool:
    """标记审批为已超时（server 端 deadline 到期）。"""
    with _store_lock:
        item = _items.get(approval_id)
        if item is None:
            return False
        if item.status == "pending":
            item.status = "timeout"
            item.decision = "timeout"
            return True
        return False


def record_activity(approval_id: str) -> bool:
    """重置该请求的超时 deadline（用户在操作）。"""
    with _store_lock:
        item = _items.get(approval_id)
        if item is None:
            return False
        if item.status != "pending":
            return False
        item.last_activity = _heartbeat.now()
        return True


def set_decision(
    approval_id: str, decision: str, feedback: str, approval_token: str = ""
) -> bool:
    """记录用户决策（approve/deny）。"""
    with _store_lock:
        item = _items.get(approval_id)
        if item is None:
            return False
        if item.status != "pending":
            return False
        item.status = "decided"
        item.decision = decision
        item.feedback = feedback
        item.approval_token = approval_token
        return True


def ack_timeout(approval_id: str) -> bool:
    """用户对已超时卡片点"收到"。"""
    with _store_lock:
        item = _items.get(approval_id)
        if item is None:
            return False
        if item.status == "timeout":
            item.status = "acked"
            return True
        return False


def pending_count() -> int:
    with _store_lock:
        return sum(1 for item in _items.values() if item.status == "pending")


def reset() -> None:
    """清空所有状态（测试用）。"""
    with _store_lock:
        _items.clear()
