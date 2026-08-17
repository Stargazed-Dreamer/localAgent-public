"""用户消息注入模块

允许用户在 agent 工作期间通过 GUI 或 API 发送补充指令。
指令暂存在内存中，当 agent 调用非工作端点时自动返回并清除。

工作端点（如 /llm/pool/*）被排除，因为它们由脚本调用而非 agent 直接使用。
MCP 工具调用通过 monkey-patch 单独处理（在 _patched_execute_api_tool 中注入）。
"""

import logging
import threading
import time

from fastapi import APIRouter
from pydantic import Field

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.user_message")
router = APIRouter(prefix="/user", tags=["用户消息"])

# ========== 线程安全消息存储 ==========

_lock = threading.Lock()
_pending_messages: list[dict] = []  # [{"text": "...", "timestamp": 123.0, "id": 0}]
_next_id = 1


def add_message(text: str) -> dict:
    """添加一条用户补充消息"""
    global _next_id
    with _lock:
        msg = {
            "id": _next_id,
            "text": text,
            "timestamp": time.time(),
        }
        _next_id += 1
        _pending_messages.append(msg)
        logger.info(f"用户补充消息已暂存 (id={msg['id']}): {text[:80]}...")
        return msg


def get_pending() -> list[dict]:
    """获取所有待发送消息（不清除）"""
    with _lock:
        return list(_pending_messages)


def consume_pending() -> list[dict]:
    """获取并清除所有待发送消息"""
    with _lock:
        msgs = _pending_messages[:]
        _pending_messages.clear()
        return msgs


def clear_all() -> int:
    """清除所有待发送消息，返回清除数量"""
    with _lock:
        count = len(_pending_messages)
        _pending_messages.clear()
        return count


def clear_by_id(msg_id: int) -> bool:
    """按 ID 删除单条消息"""
    with _lock:
        for i, m in enumerate(_pending_messages):
            if m["id"] == msg_id:
                _pending_messages.pop(i)
                return True
        return False


def has_pending() -> bool:
    """是否有待发送消息"""
    with _lock:
        return len(_pending_messages) > 0


# ========== 排除路径（脚本/系统端点，非 agent 直接调用） ==========

# 设计原则：用户消息的存在意义就是让 agent 收到，所以排除清单应最小化。
# 只排除"绝不会由 agent 主动调用"的端点（脚本代理、协议端点、文档/静态资源）。
# 工作端点（/exec /ocr /vision /screen /browser /mindforge 等）不排除——
# agent 在执行这些操作时也应能收到用户的补充指令。
EXCLUDED_PREFIXES = (
    "/llm/pool",      # LLM 并发池（脚本代理调用，非 agent 直接调用）
    "/mcp",            # MCP 协议端点（单独通过 monkey-patch 处理）
    "/user",           # 用户消息端点自身（避免自反馈）
    "/approvals",      # 审批面板轮询端点（面板进程调用，非 agent）
    "/static",         # 静态文件
    "/output",         # 输出文件
    "/docs",           # API 文档（Swagger UI）
    "/openapi.json",   # OpenAPI schema（精确路径，避免 /openapi 误排除 /openapis 等）
    "/redoc",          # ReDoc 文档
)


def should_inject(path: str) -> bool:
    """判断给定路径是否应该注入用户消息。

    使用精确前缀匹配：path == prefix 或 path 以 prefix + "/" 开头。
    不使用 startswith(prefix) 单边匹配，避免 "/user" 误排除 "/users" 等路径。
    """
    return all(not (path == prefix or path.startswith(prefix + "/")) for prefix in EXCLUDED_PREFIXES)


def get_supplement_text() -> str | None:
    """消费待发送消息，返回格式化的补充指令文本（无消息则返回 None）"""
    msgs = consume_pending()
    if not msgs:
        return None
    if len(msgs) == 1:
        return f"[用户补充指令] {msgs[0]['text']}"
    parts = []
    for m in msgs:
        parts.append(f"[用户补充指令 #{m['id']}] {m['text']}")
    return "\n".join(parts)


# ========== REST 端点 ==========

class UserMessageRequest(BaseSchema):
    """用户消息请求"""
    text: str = Field(..., min_length=1, description="补充指令文本")


class UserMessageResponse(BaseSchema):
    """用户消息响应"""
    status: str
    message: dict = Field(default_factory=dict)
    pending_count: int = 0


class UserMessageListResponse(BaseSchema):
    """用户消息列表响应"""
    pending: list[dict]
    count: int


@router.post("/message", response_model=UserMessageResponse, operation_id="user_message_send")
async def send_message(req: UserMessageRequest):
    """发送用户补充指令

    指令暂存在后端，当 agent 调用非工作端点时自动返回。
    """
    msg = add_message(req.text)
    return UserMessageResponse(
        status="ok",
        message=msg,
        pending_count=len(get_pending()),
    )


@router.get("/message", response_model=UserMessageListResponse, operation_id="user_message_list")
async def list_messages():
    """查看所有待发送消息（不清除）"""
    pending = get_pending()
    return UserMessageListResponse(pending=pending, count=len(pending))


@router.delete("/message", response_model=UserMessageListResponse, operation_id="user_message_clear_all")
async def clear_messages():
    """清除所有待发送消息"""
    clear_all()
    return UserMessageListResponse(pending=[], count=0)


@router.delete("/message/{msg_id}", response_model=UserMessageResponse, operation_id="user_message_clear_one")
async def clear_one_message(msg_id: int):
    """按 ID 删除单条待发送消息"""
    deleted = clear_by_id(msg_id)
    return UserMessageResponse(
        status="deleted" if deleted else "not_found",
        pending_count=len(get_pending()),
    )
