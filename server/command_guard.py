"""dcg command interception and one-time user approval tokens.

合并自原 server/command_guard_router.py（已删除）：shell 审批的 state + endpoint + GUI 启动闭环于此。
HTTP 审批仍由 server/http_guard.py 独立管理（指纹方案不同）。
审计日志在 server/approval_review.py（原 approval_log.py 已合并）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.schema import BaseSchema
from server.approval_review import log_approval_detailed
from server.config import get_command_guard_config

AGENT_BLOCK_MESSAGE = (
    "这条命令被用户的安全规则拦截。命令拦截不是为了阻碍 Agent 工作，而是应对模型幻觉或命令执行错误的最后防线。"
    "请不要尝试改写、拆分、编码、写入脚本或使用其他工具绕过。"
    "先向用户说明准备执行的完整命令、执行原因、可能影响和更安全的替代方案。"
    "如果当前 Agent 平台提供询问用户的工具，请优先使用；否则调用 LocalAgent 的 command_guard_request_approval 弹窗工具。"
    "先问用户通常没有害处。"
)


@dataclass
class GuardDecision:
    allowed: bool
    blocked: bool = False
    unavailable: bool = False
    reason: str = ""
    rule_id: str = ""
    approval_id: str = ""


_pending: dict[str, dict] = {}
_tokens: dict[str, dict] = {}

_dcg_cache: str = ""
_dcg_cache_expires: float = 0.0
_DCG_CACHE_TTL = 60.0

# 路由（原 command_guard_router.py 的 endpoint 合并于此）
router = APIRouter(prefix="/command-guard", tags=["命令安全"])


class ApprovalRequest(BaseSchema):
    approval_id: str
    agent_reason: str = Field(min_length=1, description="说明完整命令、执行原因、影响和更安全替代方案")


class DecisionRequest(BaseSchema):
    approval_id: str
    decision: str = Field(pattern="^(approve|deny)$")
    feedback: str = ""


def _fingerprint(command: str, shell: str, cwd: str) -> str:
    normalized = json.dumps([command, shell, str(Path(cwd).resolve())], ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cleanup() -> None:
    now = time.time()
    for store in (_pending, _tokens):
        for key in [key for key, value in store.items() if value["expires_at"] <= now]:
            store.pop(key, None)


def find_dcg() -> str | None:
    global _dcg_cache, _dcg_cache_expires
    now = time.time()
    if now < _dcg_cache_expires:
        return _dcg_cache or None
    config = get_command_guard_config()
    candidates = [
        config.get("dcg_path", ""),
        os.environ.get("DCG_EXECUTABLE", ""),
        shutil.which("dcg") or "",
        str(Path.home() / ".local" / "bin" / "dcg.exe"),
    ]
    result = next((str(Path(item).resolve()) for item in candidates if item and Path(item).is_file()), None)
    _dcg_cache = result or ""
    _dcg_cache_expires = now + _DCG_CACHE_TTL
    return result


def _invalidate_dcg_cache() -> None:
    """清除 dcg 路径缓存（测试用）"""
    global _dcg_cache, _dcg_cache_expires
    _dcg_cache = ""
    _dcg_cache_expires = 0.0


def consume_token(token: str, command: str, shell: str, cwd: str) -> bool:
    if not token:
        return False
    _cleanup()
    record = _tokens.pop(token, None)
    return bool(record and record["fingerprint"] == _fingerprint(command, shell, cwd))


def _blocked(command: str, shell: str, cwd: str, reason: str, rule_id: str) -> GuardDecision:
    _cleanup()
    approval_id = f"approval_{uuid.uuid4().hex[:16]}"
    ttl = int(get_command_guard_config().get("approval_ttl_seconds", 300))
    _pending[approval_id] = {
        "command": command,
        "shell": shell,
        "cwd": str(Path(cwd).resolve()),
        "fingerprint": _fingerprint(command, shell, cwd),
        "reason": reason,
        "rule_id": rule_id,
        "expires_at": time.time() + ttl,
    }
    return GuardDecision(False, blocked=True, reason=reason, rule_id=rule_id, approval_id=approval_id)


async def check_command(command: str, shell: str, cwd: str, approval_token: str = "") -> GuardDecision:
    config = get_command_guard_config()
    if not config.get("enabled", True):
        return GuardDecision(True)
    if consume_token(approval_token, command, shell, cwd):
        return GuardDecision(True)

    dcg = find_dcg()
    if not dcg:
        if config.get("fail_closed", True):
            return _blocked(command, shell, cwd, "dcg 未安装或不可执行，安全策略为 fail-closed", "localagent:dcg-unavailable")
        return GuardDecision(True, unavailable=True, reason="dcg 不可用，按配置 fail-open")

    try:
        proc = await asyncio.create_subprocess_exec(
            dcg, "--robot", "test", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, "DCG_ROBOT": "1"},
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        if config.get("fail_closed", True):
            return _blocked(command, shell, cwd, f"dcg 启动失败: {exc}", "localagent:dcg-launch-error")
        return GuardDecision(True, unavailable=True, reason=f"dcg 启动失败，按配置 fail-open: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=float(config.get("timeout_seconds", 3.0))
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        if config.get("fail_closed", True):
            return _blocked(command, shell, cwd, "dcg 检查超时，安全策略为 fail-closed", "localagent:dcg-timeout")
        return GuardDecision(True, unavailable=True, reason="dcg 检查超时，按配置 fail-open")

    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError:
        payload = {}
    decision = str(payload.get("decision", "")).lower()
    if proc.returncode == 1 or decision in {"deny", "block", "blocked"}:
        reason = payload.get("reason") or payload.get("message") or error or "命中 dcg 破坏性命令规则"
        rule_id = payload.get("rule_id") or payload.get("ruleId") or "dcg:destructive-command"
        return _blocked(command, shell, cwd, str(reason), str(rule_id))
    if proc.returncode not in (0, None):
        if config.get("fail_closed", True):
            return _blocked(command, shell, cwd, error or "dcg 检查失败", "localagent:dcg-error")
        return GuardDecision(True, unavailable=True, reason=error or "dcg 检查失败，按配置 fail-open")
    return GuardDecision(True)


def get_pending(approval_id: str) -> dict:
    _cleanup()
    pending = _pending.get(approval_id)
    if not pending:
        raise ValueError("审批请求不存在或已过期")
    return dict(pending)


def record_user_decision(approval_id: str, decision: str, feedback: str = "") -> dict:
    _cleanup()
    pending = _pending.pop(approval_id, None)
    if not pending:
        raise ValueError("审批请求不存在或已过期")
    if decision != "approve":
        return {"approved": False, "decision": "deny", "feedback": feedback}
    token = secrets.token_urlsafe(32)
    ttl = int(get_command_guard_config().get("token_ttl_seconds", 120))
    _tokens[token] = {"fingerprint": pending["fingerprint"], "expires_at": time.time() + ttl}
    return {
        "approved": True,
        "decision": "approve",
        "feedback": feedback,
        "approval_token": token,
        "expires_in_seconds": ttl,
    }


# ========== REST endpoints（原 command_guard_router.py 合并） ==========

@router.get("/status", operation_id="command_guard_status")
async def command_guard_status():
    config = get_command_guard_config()
    return {"enabled": config["enabled"], "dcg_path": find_dcg(), "agent_instruction": AGENT_BLOCK_MESSAGE}


@router.post("/decision", operation_id="command_guard_record_decision")
async def command_guard_record_decision(req: DecisionRequest):
    """记录 Agent 原生询问工具得到的用户决定，并签发一次性放行令牌。

    路由类型由 approval_id 前缀派发：http_approval_ → http_guard，其余 → command_guard。
    """
    try:
        if req.approval_id.startswith("http_approval_"):
            from server.http_guard import record_http_decision
            return record_http_decision(req.approval_id, req.decision, req.feedback)
        return record_user_decision(req.approval_id, req.decision, req.feedback)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def run_gui_dialog(payload: dict, approval_id: str = "") -> dict:
    """启动审批 GUI 子进程并等待用户决定。共享函数（shell/http 审批共用）。

    参数 payload 结构见 command_approval_gui.py。
    参数 approval_id：审批请求 ID，面板在线时用于路由到审批面板。
    返回 {"decision": "approve"|"deny"|"timeout", "feedback": str}。
    - decision="timeout"：面板在线路径超时（调用方应返 408，不消费 _pending）
    - 超时（subprocess 兜底）抛 asyncio.TimeoutError；子进程失败抛 RuntimeError。

    Ticket 02 改造：
    - 面板在线 → lib.approval_router.route_approval 入队 + await_decision 等待
    - 面板离线 → 原 subprocess 弹窗流程（Ticket 03 的内部倒计时 + textChanged 重置）
    """
    gui_timeout = float(get_command_guard_config().get("gui_timeout_seconds", 180))
    # server 端兜底超时：gui_timeout + 20s（面板 activity 重置上限 / subprocess buffer）
    server_timeout = gui_timeout + 20.0

    # ===== 面板在线路径 =====
    if approval_id:
        from lib.approval_router import router as approval_router

        route = approval_router.route_approval(approval_id, payload, server_timeout)
        if route["route"] == "panel":
            result = await approval_router.await_decision(approval_id, server_timeout)
            # 面板路径返回 dict（含 decision="timeout" 表示超时），不抛异常
            return result

    # ===== 面板离线 fallback：原 subprocess 弹窗流程 =====
    script = Path(__file__).with_name("command_approval_gui.py")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    # 注入 gui_timeout_seconds 给子进程（内部倒计时用）
    payload_with_timeout = {**payload, "gui_timeout_seconds": int(gui_timeout)}
    data = json.dumps(payload_with_timeout, ensure_ascii=False).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(data), timeout=server_timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"命令审批弹窗超时（server 兜底 {int(server_timeout)}秒未响应）") from None
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace") or "命令审批弹窗启动失败"
        raise RuntimeError(detail)
    return json.loads(stdout.decode("utf-8"))


@router.post("/request-approval", operation_id="command_guard_request_approval")
async def command_guard_request_approval(req: ApprovalRequest):
    """当 Agent 平台没有原生询问工具时，打开 PySide6 审批弹窗。"""
    is_http = req.approval_id.startswith("http_approval_")
    log_approval_detailed({
        "event": "request_approval_start",
        "approval_id": req.approval_id,
        "agent_reason": req.agent_reason,
        "agent_reason_source": "agent_written",
        "is_http": is_http,
    })
    # 审批会话兜底时长（与 run_gui_dialog 的 server_timeout 语义一致）
    gui_timeout = float(get_command_guard_config().get("gui_timeout_seconds", 180))
    session_ttl = gui_timeout + 20.0
    try:
        if is_http:
            from server.http_guard import extend_pending_http, get_pending_http
            pending = get_pending_http(req.approval_id)
        else:
            pending = get_pending(req.approval_id)
    except ValueError as exc:
        log_approval_detailed({
            "event": "request_approval_pending_error",
            "approval_id": req.approval_id,
            "error": str(exc),
        })
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if is_http:
        payload = {
            "type": "http",
            "method": pending["method"],
            "path": pending["path"],
            "body_preview": pending.get("body_preview", ""),
            "guard_reason": pending["reason"],
            "agent_reason": req.agent_reason,
        }
    else:
        payload = {
            "type": "shell",
            "command": pending["command"],
            "guard_reason": pending["reason"],
            "agent_reason": req.agent_reason,
        }
    try:
        # 发起 GUI 审批前延长 pending 存活期，覆盖整个审批会话
        # （默认 approval_ttl_seconds=300s，gui_timeout>280s 时 pending 会先过期
        # 导致用户批准后 record_http_decision 抛 ValueError，见 extend_pending_http）
        if is_http:
            extend_pending_http(req.approval_id, session_ttl)
        result = await run_gui_dialog(payload, approval_id=req.approval_id)
    except TimeoutError as exc:
        log_approval_detailed({
            "event": "request_approval_gui_timeout",
            "approval_id": req.approval_id,
            "error": str(exc),
        })
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        log_approval_detailed({
            "event": "request_approval_gui_error",
            "approval_id": req.approval_id,
            "error": str(exc),
        })
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    decision = result.get("decision", "deny")
    feedback = result.get("feedback", "")

    # 面板路径超时：返 408，不消费 _pending（agent 不应重试，面板卡片留存待用户 ack）
    if decision == "timeout":
        log_approval_detailed({
            "event": "request_approval_panel_timeout",
            "approval_id": req.approval_id,
            "is_http": is_http,
        })
        raise HTTPException(
            status_code=408,
            detail={
                "error": "approval_timeout",
                "approval_id": req.approval_id,
                "message": "审批超时未响应",
            },
        ) from None

    if is_http:
        from server.http_guard import record_http_decision
        record_result = record_http_decision(req.approval_id, decision, feedback)
    else:
        record_result = record_user_decision(req.approval_id, decision, feedback)
    log_approval_detailed({
        "event": "request_approval_end",
        "approval_id": req.approval_id,
        "user_decision": decision,
        "user_feedback": feedback,
        "approved": record_result.get("approved", False),
        "token_issued": bool(record_result.get("approval_token")),
    })
    return record_result
