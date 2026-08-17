"""HTTP 中间件：trace_id 注入 + MCP 统计 + 用户消息注入"""

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi.responses import Response as FastAPIResponse

from server.user_message import (
    get_supplement_text,
    has_pending,
    should_inject,
)

logger = logging.getLogger("localagent.middleware")


# ========== MCP 工具调用统计（原 server/mcp_stats.py 归并） ==========
# 数据持久化到 data/mcp_stats.json，重启后保留。
_STATS_FILE = Path(__file__).parent.parent.parent / "data" / "mcp_stats.json"
_stats: dict[str, dict] = {}
_stats_lock = threading.Lock()


def _load_mcp_stats() -> None:
    """启动时加载统计数据（一次性，由 setup_mcp 触发）"""
    global _stats
    if _STATS_FILE.exists():
        try:
            _stats = json.loads(_STATS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _stats = {}
    else:
        _stats = {}


def _save_mcp_stats() -> None:
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FILE.write_text(
            json.dumps(_stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def record_call(tool_name: str, duration_ms: float, success: bool = True) -> None:
    """记录一次 MCP 工具调用"""
    with _stats_lock:
        if tool_name not in _stats:
            _stats[tool_name] = {
                "count": 0, "error_count": 0, "total_ms": 0.0,
                "last_called": "", "last_error": "",
            }
        entry = _stats[tool_name]
        entry["count"] += 1
        entry["total_ms"] += duration_ms
        entry["last_called"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not success:
            entry["error_count"] += 1
            entry["last_error"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 异步保存（不阻塞请求）
    threading.Thread(target=_save_mcp_stats, daemon=True).start()


def get_mcp_stats() -> dict:
    """获取统计数据，按调用次数降序排列"""
    with _stats_lock:
        result = []
        for name, entry in _stats.items():
            avg_ms = round(entry["total_ms"] / entry["count"], 1) if entry["count"] > 0 else 0
            result.append({
                "tool": name,
                "count": entry["count"],
                "error_count": entry.get("error_count", 0),
                "avg_ms": avg_ms,
                "total_ms": round(entry["total_ms"], 1),
                "last_called": entry.get("last_called", ""),
            })
        result.sort(key=lambda x: x["count"], reverse=True)
        return {
            "total_calls": sum(r["count"] for r in result),
            "unique_tools": len(result),
            "tools": result,
        }


def reset_mcp_stats() -> None:
    """重置统计数据"""
    global _stats
    with _stats_lock:
        _stats = {}
    _save_mcp_stats()


def register_middleware(app):
    """注册所有 HTTP 中间件"""

    @app.middleware("http")
    async def trace_id_middleware(request, call_next):
        """为每个请求注入 trace_id，贯穿日志和错误响应。

        - 优先复用客户端传入的 X-Request-ID（便于跨进程追踪）
        - 否则生成 12 位 hex
        - 存入 request.state.trace_id 供异常处理器和路由使用
        - 响应头回传 X-Request-ID
        """
        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        response = await call_next(request)
        try:
            response.headers["X-Request-ID"] = trace_id
        except Exception:
            pass
        return response

    @app.middleware("http")
    async def mcp_stats_middleware(request, call_next):
        """拦截 /mcp POST 请求，记录工具调用统计"""
        if request.url.path == "/mcp" and request.method == "POST":
            # 读取请求体
            body = await request.body()
            tool_name = None
            try:
                data = json.loads(body)
                # MCP JSON-RPC: {"method": "tools/call", "params": {"name": "tool_name"}}
                if data.get("method") == "tools/call":
                    tool_name = data.get("params", {}).get("name")
            except (json.JSONDecodeError, AttributeError):
                pass

            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000

            if tool_name:
                success = 200 <= response.status_code < 400
                record_call(tool_name, duration_ms, success)

                # 记忆系统触发器：记录工具调用到三层记忆
                try:
                    from server.memory.manager import get_memory_manager
                    mgr = get_memory_manager()
                    if mgr.config.auto_record and tool_name not in mgr.config.exclude_tools:
                        # 从请求中提取参数摘要
                        args_summary = {}
                        try:
                            params = data.get("params", {})
                            arguments = params.get("arguments", {})
                            if isinstance(arguments, dict):
                                for k, v in list(arguments.items())[:5]:
                                    args_summary[k] = str(v)[:100] if isinstance(v, str) else v
                        except Exception:
                            pass
                        mgr.recorder.record_tool_call(
                            tool_name=tool_name,
                            arguments=args_summary,
                            duration_ms=duration_ms,
                        )
                except Exception:
                    pass  # 记忆系统不可用时不影响主流程

            return response
        return await call_next(request)

    @app.middleware("http")
    async def user_message_inject_middleware(request, call_next):
        """将待发送的用户补充指令注入到非工作端点的响应中"""
        response = await call_next(request)

        # 排除工作端点和系统端点
        if not should_inject(request.url.path):
            return response
        if not has_pending():
            return response

        # 消费待发送消息
        supplement = get_supplement_text()
        if not supplement:
            return response

        # 始终添加 header（URL 编码，因为 HTTP header 只支持 Latin-1）
        try:
            from urllib.parse import quote
            response.headers["X-User-Supplement"] = quote(supplement)[:2000]
        except Exception:
            pass  # header 设置失败不影响 JSON 注入

        # 对 JSON 响应注入 _user_supplement 字段
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            # 收集响应体
            body_bytes = b""
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    body_bytes += chunk.encode("utf-8")
                else:
                    body_bytes += chunk

            try:
                data = json.loads(body_bytes)
                if isinstance(data, dict):
                    data["_user_supplement"] = supplement
                else:
                    data = {"_original": data, "_user_supplement": supplement}
                new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers = dict(response.headers)
                headers["content-length"] = str(len(new_body))
                return FastAPIResponse(
                    content=new_body,
                    status_code=response.status_code,
                    headers=headers,
                    media_type="application/json",
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # 非 JSON，仅使用 header

        return response

    @app.middleware("http")
    async def http_guard_middleware(request, call_next):
        """对 agent 调用的 approval_required 端点要求 X-Approval-Token。

        仅拦截带 X-Agent-Caller: true 头的请求（agent 发起）。
        用户/GUI 请求不带此头，直接放行。
        """
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        safety_lookup = getattr(request.app.state, "safety_lookup", None)
        if not safety_lookup:
            return await call_next(request)

        safety = None
        for path_regex, method, level in safety_lookup:
            if method == request.method and path_regex.match(request.url.path):
                safety = level
                break

        if safety != "approval_required":
            return await call_next(request)

        # 二次确认：按当前 approval_level 过滤（用户在 GUI 改 level 后立即生效，
        # 无需重建 safety_lookup）。lookup 始终按 strict 完整清单构建，
        # 若当前 level 为 moderate/loose/none，对应端点在此降级为 safe 放行。
        from server.route_tags import classify_safety_runtime
        runtime_safety = classify_safety_runtime(request.method, request.url.path)
        if runtime_safety != "approval_required":
            return await call_next(request)

        is_agent = request.headers.get("X-Agent-Caller", "").lower() == "true"
        if not is_agent:
            return await call_next(request)

        body = await request.body()

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive

        # 三层递进审批：静态规则 → LLM 审查 → 人审
        from server.approval_review import get_op_from_path, try_auto_approve, user_review_for_llm_deny
        operation_id = get_op_from_path(request.url.path)
        user_feedback = ""
        if operation_id:
            body_dict = {}
            if body:
                try:
                    body_dict = json.loads(body)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            review = await asyncio.to_thread(
                try_auto_approve, operation_id, body_dict, request.method, request.url.path
            )
            if review["auto_approved"]:
                return await call_next(request)
            # 静态 block 或 LLM 未放行 → 直接弹人审，用户批准则放行
            if not review["auto_approved"] and review.get("layer1_static") in ("block", "pass"):
                block_decision = "static_block" if review.get("layer1_static") == "block" else review.get("layer2_llm", "deny")
                user_result = await user_review_for_llm_deny(
                    operation_id, body_dict, request.method, request.url.path,
                    body, review.get("reason", ""), block_decision,
                )
                user_feedback = user_result.get("feedback", "")
                if user_result["approved"]:
                    return await call_next(request)
                # 用户已拒绝：直接返回 403 + user_denied + feedback，不再创建新 approval_id
                # 治本：避免 agent 收到 "approval_required" 后重复申请，同时回传用户 feedback
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "user_denied",
                        "message": "用户已拒绝此操作。请尊重用户决定，不要重复申请审批；如需继续请说明理由或改用更安全方案。",
                        "user_feedback": user_feedback,
                        "operation_id": operation_id,
                    },
                )

        # 未弹过人审（layer1=skipped，非审查端点），走标准 approval_required 流程
        from server.http_guard import check_approval
        token = request.headers.get("X-Approval-Token", "")
        approval_id = check_approval(request.method, request.url.path, body, token)
        if approval_id is None:
            return await call_next(request)

        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={
                "error": "approval_required",
                "approval_id": approval_id,
                "message": f"HTTP {request.method} {request.url.path} 需要 user 审批",
            },
        )
