"""HTTP 审批中间件：对 agent 调用的 approval_required 端点要求 X-Approval-Token。

与 command_guard.py（dcg shell 拦截）并列，补上 HTTP 端点调用零拦截的盲区。

工作流程：
1. 中间件检查 X-Agent-Caller 头（v6-lite 引擎调 REST 时设置，GUI/用户不加）
   注：MCP 路径上的 approval 由 mcp_gateway._patched_execute_api_tool patch 兜底，
   不经此中间件；本中间件仅对 v6-lite 引擎经 http_client 直连 REST 的请求生效。
2. 对 approval_required 端点，检查 X-Approval-Token
3. token 无效/缺失 → 返回 403 + approval_id
4. agent 调 /command-guard/request-approval → 用户批准 → 获 token
5. agent 重试原请求，加 X-Approval-Token 头

指纹方案：hash(method + path + body_hash)，与 dcg 的 command+shell+cwd 独立。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid

from server.config import get_command_guard_config

logger = logging.getLogger("localagent.http_guard")

# HTTP 审批的 pending 和 token 存储（与 command_guard 的 _pending/_tokens 独立）
_pending_http: dict[str, dict] = {}
_tokens_http: dict[str, dict] = {}


def _http_fingerprint(method: str, path: str, body: bytes) -> str:
    """计算 HTTP 请求指纹：hash(method + path + body_hash)。"""
    body_hash = hashlib.sha256(body).hexdigest()[:16] if body else ""
    normalized = json.dumps([method.upper(), path, body_hash], ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cleanup() -> None:
    """清理过期的 pending 和 token。"""
    now = time.time()
    for store in (_pending_http, _tokens_http):
        for key in [k for k, v in store.items() if v["expires_at"] <= now]:
            store.pop(key, None)


_BODY_PREVIEW_LIMIT = 2000


def _extract_body_preview(body: bytes) -> str:
    """从请求体提取 code/cmd 等可读字段，截断到合理长度供 GUI 展示。"""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        text = body.decode("utf-8", errors="replace").strip()
        return text[:_BODY_PREVIEW_LIMIT] + ("..." if len(text) > _BODY_PREVIEW_LIMIT else "")
    if not isinstance(data, dict):
        return str(data)[:_BODY_PREVIEW_LIMIT]
    # exec_python=code, exec_cmd=cmd, exec_terminal_spawn=cmd, apply_patch=patch
    for key in ("code", "cmd", "command", "patch", "script"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text[:_BODY_PREVIEW_LIMIT] + ("..." if len(text) > _BODY_PREVIEW_LIMIT else "")
    # 兜底：展示其他参数（排除 _approval_token 等内部字段）
    filtered = {k: v for k, v in data.items()
                if not k.startswith("_") and k != "approval_token"}
    if filtered:
        text = json.dumps(filtered, ensure_ascii=False, indent=2)
        return text[:_BODY_PREVIEW_LIMIT] + ("..." if len(text) > _BODY_PREVIEW_LIMIT else "")
    return ""


def create_pending(method: str, path: str, body: bytes) -> str:
    """创建 HTTP 审批请求，返回 approval_id。"""
    _cleanup()
    approval_id = f"http_approval_{uuid.uuid4().hex[:16]}"
    ttl = int(get_command_guard_config().get("approval_ttl_seconds", 300))
    _pending_http[approval_id] = {
        "method": method.upper(),
        "path": path,
        "body_hash": hashlib.sha256(body).hexdigest()[:16] if body else "",
        "body_preview": _extract_body_preview(body),
        "fingerprint": _http_fingerprint(method, path, body),
        "reason": f"HTTP {method.upper()} {path} 需要 user 审批（x-tool-safety=approval_required）",
        "expires_at": time.time() + ttl,
    }
    logger.info("http_guard: created approval %s for %s %s", approval_id, method, path)
    return approval_id


def consume_token(token: str, method: str, path: str, body: bytes) -> bool:
    """验证并消费一次性 approval token。"""
    if not token:
        return False
    _cleanup()
    record = _tokens_http.pop(token, None)
    if not record:
        return False
    return record["fingerprint"] == _http_fingerprint(method, path, body)


def get_pending_http(approval_id: str) -> dict:
    """获取 HTTP 审批请求详情。"""
    _cleanup()
    pending = _pending_http.get(approval_id)
    if not pending:
        raise ValueError("HTTP 审批请求不存在或已过期")
    return dict(pending)


def record_http_decision(approval_id: str, decision: str, feedback: str = "") -> dict:
    """记录用户对 HTTP 审批的决定，批准时签发一次性 token。"""
    _cleanup()
    pending = _pending_http.pop(approval_id, None)
    if not pending:
        raise ValueError("HTTP 审批请求不存在或已过期")
    if decision != "approve":
        logger.info("http_guard: approval %s denied", approval_id)
        return {"approved": False, "decision": "deny", "feedback": feedback}
    # 人审通过，清除 LLM 冷却期
    from server.approval_review import notify_user_approved
    notify_user_approved()
    token = secrets.token_urlsafe(32)
    ttl = int(get_command_guard_config().get("token_ttl_seconds", 120))
    _tokens_http[token] = {"fingerprint": pending["fingerprint"], "expires_at": time.time() + ttl}
    logger.info("http_guard: approval %s approved, token issued (ttl=%ds)", approval_id, ttl)
    return {
        "approved": True,
        "decision": "approve",
        "feedback": feedback,
        "approval_token": token,
        "expires_in_seconds": ttl,
    }


def check_approval(method: str, path: str, body: bytes, token: str) -> str | None:
    """检查是否需要审批。返回 approval_id（需审批）或 None（放行）。

    如果 token 有效，返回 None（放行）。
    如果需要审批，返回 approval_id。
    """
    if consume_token(token, method, path, body):
        return None  # token 有效，放行
    return create_pending(method, path, body)
