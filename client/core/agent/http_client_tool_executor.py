"""v6-lite HttpClientToolExecutor — 经 HTTP 调用 server 端点执行工具

设计依据：
- v6-lite §3 W2：引擎经 client/core/http_client.py 调 server 端点执行工具，
  请求带 X-Agent-Caller 头（自然过 http_guard）
- v6-lite §3 W3：审批桥接——approval_required 工具走 server 现有审批链
  - 路径 A（exec_* 同步阻塞）：server 内部完成 GUI 弹窗，拒绝时返回 403 user_denied + user_feedback
  - 路径 B（非审查端点）：server 返回 403 approval_required + approval_id，
    引擎调 POST /command-guard/request-approval（同步阻塞 GUI）→ 拿 token → 重试原请求加 X-Approval-Token 头
- v6-lite §4.5：error-as-output-variant——工具错误是结构化输出，不抛 traceback
- v6-lite §4.10：副作用前先落 durable record（runner 负责，executor 只管执行）

职责：
1. 接收 ToolCall（已解析 args），按 operation_id 查 ToolRegistry 拿 ToolEntry
2. 用 ToolEntry.build_request(args) 拼 path / query / json body
3. 发 HTTP 请求到 server，带 X-Agent-Caller 头
4. 把响应转成 ToolResult（错误也转成 is_error=True 的 ToolResult，不抛异常）
5. T06+: 403 审批挑战处理（路径 A user_denied / 路径 B approval_required）

不依赖 Qt（纯 Python，可在 CLI 脚本和 GUI 中复用）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from client.core.agent.l0_artifact_store import L0ArtifactStore
from client.core.agent.tool_registry import ToolRegistry
from client.core.agent.types import ToolCall, ToolResult, ToolResultVariant

logger = logging.getLogger("localagent.agent.http_tool_executor")

# X-Agent-Caller 头标识：让 server http_guard 知道这是 agent 调用（区别于 GUI 用户调用）
# server 的 http_guard 会按 operation_id + safety 决定是否走审批链
AGENT_CALLER_HEADER_VALUE = "v6-lite-agent"

# T06b: 审批挑战处理端点（server/command_guard.py 的 request_approval 端点）
# 请求体: {approval_id, agent_reason}
# 响应体（批准）: {approved: true, approval_token, expires_in_seconds, feedback}
# 响应体（拒绝）: {approved: false, decision: "deny", feedback}
APPROVAL_REQUEST_PATH = "/command-guard/request-approval"

# server 端 GUI 弹窗超时 180s（config.command_guard.gui_timeout_seconds），
# 引擎 approval_timeout 留余量 200s
DEFAULT_APPROVAL_TIMEOUT_S = 200.0


class HttpClientToolExecutor:
    """经 HTTP 调用 server 端点执行工具。

    用法：
        reg = ToolRegistry(base_url=...); reg.refresh()
        ex = HttpClientToolExecutor(registry=reg, base_url=...)
        result = await ex.execute(tool_call)

    T08+：注入 L0ArtifactStore 后，>8KB 的成功响应自动落盘，
    返回的 ToolResult.content 是 preview+path，artifact_path 字段记录完整路径。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        base_url: str = "http://127.0.0.1:8766",
        *,
        timeout: float = 30.0,
        caller: str = AGENT_CALLER_HEADER_VALUE,
        l0_store: L0ArtifactStore | None = None,
    ):
        self.registry = registry
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.caller = caller
        self.l0_store = l0_store  # T08: None 时不落盘（向后兼容 T05-T07）
        self._session = requests.Session()
        # C6（spec D6）：USER_DENIED 缓存，key = (tool_name, args_hash)
        # 用户拒绝审批后，同 (tool_name, args) 再次调用直接返回缓存，不再弹审批窗
        self._denied_cache: dict[str, str] = {}

    @staticmethod
    def _cache_key(tool_name: str, args: dict) -> str:
        """C6：构造缓存键 (tool_name, args_hash)。

        args 用 sort_keys=True 的 JSON 序列化后取 hash，确保键值顺序不影响命中。
        """
        args_canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return f"{tool_name}:{hash(args_canonical)}"

    def _check_denied_cache(self, tool_call: ToolCall) -> ToolResult | None:
        """C6：检查 USER_DENIED 缓存，命中则返回缓存的 ToolResult。"""
        key = self._cache_key(tool_call.name, tool_call.args)
        cached_content = self._denied_cache.get(key)
        if cached_content is None:
            return None
        logger.info(
            "C6 cache hit: tool=%s args_hash=%s → returning cached USER_DENIED",
            tool_call.name, hash(key),
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            content=cached_content,
            variant=ToolResultVariant.USER_DENIED,
            created_at=time.time(),
        )

    def _store_denied_cache(self, tool_name: str, args: dict, content: str) -> None:
        """C6：存入 USER_DENIED 缓存。"""
        key = self._cache_key(tool_name, args)
        self._denied_cache[key] = content
        logger.info(
            "C6 cache store: tool=%s args_hash=%s (will skip approval on next call)",
            tool_name, hash(key),
        )

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """执行工具调用，返回 ToolResult（错误也返回，不抛异常）。

        步骤：
        1. 查 registry 拿 ToolEntry（找不到 → is_error=True）
        2. 用 build_request 拼 path/query/body
        3. 发 HTTP 请求（带 X-Agent-Caller 头）
        4. 响应转 ToolResult（HTTP 错误/非 200 → is_error=True，content 含 status_code + body）

        C6（spec D6）：execute 开始时检查 USER_DENIED 缓存，命中直接返回。
        D8: rest_endpoints 桶工具拆包——agent 调 rest_endpoints(operation_id=..., args=...)
        时，拆包成真实 operation_id 的 ToolCall 再走正常路径。
        """
        started = time.time()
        # D8: rest_endpoints 桶拆包
        if tool_call.name == "rest_endpoints":
            inner_args = tool_call.args or {}
            inner_op = inner_args.get("operation_id", "")
            inner_params = inner_args.get("args", {}) or {}
            if not inner_op:
                return ToolResult(
                    tool_call_id=tool_call.id,
                    content="rest_endpoints requires 'operation_id' arg",
                    variant=ToolResultVariant.ERROR,
                    error_type="invalid_argument",
                    created_at=started,
                )
            # 拆包成真实 operation_id 的 ToolCall（保留原 tool_call_id 以便回灌配对）
            inner_call = ToolCall(
                id=tool_call.id,
                name=inner_op,
                args=inner_params if isinstance(inner_params, dict) else {},
                session_id=tool_call.session_id,
                seq=tool_call.seq,
            )
            tool_call = inner_call
        # C6：检查 USER_DENIED 缓存（同 tool + args 曾被拒绝 → 直接返回缓存）
        cached = self._check_denied_cache(tool_call)
        if cached is not None:
            return cached
        entry = self.registry.lookup(tool_call.name)
        if entry is None:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' not found in registry",
                variant=ToolResultVariant.ERROR,
                error_type="not_found",
                created_at=started,
            )

        # 拼 path / query / body
        try:
            final_path, query_params, json_body = entry.build_request(tool_call.args)
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Failed to build request for '{tool_call.name}': {type(e).__name__}: {e}",
                variant=ToolResultVariant.ERROR,
                error_type="build_failed",
                created_at=started,
            )

        url = self.base_url + final_path
        headers = {"X-Agent-Caller": self.caller}

        try:
            resp = self._send_request(
                method=entry.method,
                url=url,
                query=query_params,
                json_body=json_body,
                headers=headers,
            )
        except requests.exceptions.Timeout:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' timed out after {self.timeout}s",
                variant=ToolResultVariant.TIMEOUT,
                timeout_seconds=self.timeout,
                created_at=started,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' request error: {type(e).__name__}: {e}",
                variant=ToolResultVariant.ERROR,
                error_type="request_error",
                created_at=started,
            )

        # 解析响应
        try:
            body_text = resp.text
        except Exception:
            body_text = "(unreadable response body)"

        # HTTP 错误或非 200 → is_error=True
        # T06a/T06b: 403 审批挑战分两条路径处理
        if resp.status_code >= 400:
            # 尝试提取 JSON 体的 error/message 字段（server 统一返回格式）
            err_msg = body_text[:1000]
            err_json: dict | None = None
            try:
                err_json = resp.json()
                if isinstance(err_json, dict):
                    # 优先取 error / message / detail 字段
                    for key in ("error", "message", "detail"):
                        if key in err_json:
                            err_msg = str(err_json[key])[:1000]
                            break
            except (ValueError, json.JSONDecodeError):
                err_json = None

            # T06a/T06b: 403 审批挑战分流
            if resp.status_code == 403 and isinstance(err_json, dict):
                # 路径 A（exec_* 同步阻塞审批拒绝）：server 返回 error=user_denied + user_feedback
                # → 把用户 feedback 作为 tool_result 内容回灌模型（variant=USER_DENIED）
                if err_json.get("error") == "user_denied" and "user_feedback" in err_json:
                    feedback = str(err_json["user_feedback"])[:1000] or "(no feedback)"
                    content = f"User denied approval: {feedback}"
                    # C6：缓存 USER_DENIED（同 tool+args 再次调用直接返回）
                    self._store_denied_cache(tool_call.name, tool_call.args, content)
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        content=content,
                        variant=ToolResultVariant.USER_DENIED,
                        created_at=started,
                    )
                # 路径 B（非审查端点审批挑战）：server 返回 approval_id（approval_required）
                # → 调 POST /command-guard/request-approval → 拿 token → 重试原请求加 X-Approval-Token 头
                if "approval_id" in err_json:
                    return await self._handle_approval_challenge(
                        tool_call=tool_call,
                        entry=entry,
                        approval_id=err_json["approval_id"],
                        err_msg=err_msg,
                        started=started,
                    )

            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' failed: HTTP {resp.status_code}: {err_msg}",
                variant=ToolResultVariant.ERROR,
                error_type="http_error",
                created_at=started,
            )

        # 成功：把响应体作为 content（如果是 JSON，序列化成紧凑字符串；否则原文本）
        try:
            data = resp.json()
            content = json.dumps(data, ensure_ascii=False)
        except (ValueError, json.JSONDecodeError):
            content = body_text

        return self._maybe_apply_l0(tool_call, content, started, variant=ToolResultVariant.SUCCESS)

    def _send_request(
        self,
        *,
        method: str,
        url: str,
        query: dict | None,
        json_body: dict | None,
        headers: dict,
    ) -> requests.Response:
        """统一发送 HTTP 请求。"""
        method = method.upper()
        if method == "GET":
            return self._session.get(
                url, params=query, headers=headers, timeout=self.timeout
            )
        if method == "POST":
            return self._session.post(
                url, json=json_body, params=query, headers=headers, timeout=self.timeout
            )
        if method == "PUT":
            return self._session.put(
                url, json=json_body, params=query, headers=headers, timeout=self.timeout
            )
        if method == "PATCH":
            return self._session.patch(
                url, json=json_body, params=query, headers=headers, timeout=self.timeout
            )
        if method == "DELETE":
            return self._session.delete(
                url, json=json_body, params=query, headers=headers, timeout=self.timeout
            )
        # 不应该到这里（registry 已过滤 method）
        raise ValueError(f"Unsupported HTTP method: {method}")

    # ------------------------------------------------------------------
    # T06b: 路径 B 审批挑战处理（非审查端点的 approval_required）
    # ------------------------------------------------------------------

    async def _handle_approval_challenge(
        self,
        *,
        tool_call: ToolCall,
        entry: Any,
        approval_id: str,
        err_msg: str,
        started: float,
    ) -> ToolResult:
        """处理路径 B 审批挑战：调 /command-guard/request-approval → 拿 token → 重试原请求。

        流程（v6-lite §3 W3）：
        1. 构造 agent_reason（为什么需要此工具调用）
        2. POST /command-guard/request-approval（同步阻塞，server 端弹 GUI）
           - 批准 → {approved: true, approval_token, expires_in_seconds, feedback}
           - 拒绝 → {approved: false, decision: "deny", feedback}
        3. 批准：用 approval_token 重试原请求（加 X-Approval-Token 头）
        4. 拒绝：把 feedback 作为 tool_result（is_error=True）回灌模型

        所有异常都转成 is_error=True 的 ToolResult，不抛（error-as-output-variant §4.5）。
        """
        # 1. 构造 agent_reason
        agent_reason = (
            f"Agent needs to call tool '{tool_call.name}' to process user query. "
            f"safety={entry.safety}"
        )

        # 2. 调 POST /command-guard/request-approval（同步阻塞 GUI）
        approval_url = f"{self.base_url}{APPROVAL_REQUEST_PATH}"
        try:
            approval_resp = self._session.post(
                approval_url,
                json={
                    "approval_id": approval_id,
                    "agent_reason": agent_reason,
                },
                headers={"X-Agent-Caller": self.caller},
                timeout=DEFAULT_APPROVAL_TIMEOUT_S,
            )
        except requests.exceptions.Timeout:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=(
                    f"Approval request timed out after {DEFAULT_APPROVAL_TIMEOUT_S}s "
                    f"for tool '{tool_call.name}' (approval_id={approval_id})"
                ),
                variant=ToolResultVariant.TIMEOUT,
                timeout_seconds=DEFAULT_APPROVAL_TIMEOUT_S,
                created_at=started,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=(
                    f"Approval request failed: {type(e).__name__}: {e} "
                    f"(approval_id={approval_id})"
                ),
                variant=ToolResultVariant.ERROR,
                error_type="approval_request_error",
                created_at=started,
            )

        # 解析审批响应
        try:
            approval_resp.raise_for_status()
            approval_data = approval_resp.json()
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=(
                    f"Approval response parse failed: {type(e).__name__}: {e} "
                    f"(approval_id={approval_id}, HTTP {approval_resp.status_code})"
                ),
                variant=ToolResultVariant.ERROR,
                error_type="approval_parse_error",
                created_at=started,
            )

        # 3. 处理审批结果
        if not approval_data.get("approved", False):
            feedback = approval_data.get("feedback", "No feedback provided.")
            content = f"User denied approval: {feedback}"
            # C6：缓存路径 B 的 USER_DENIED（同 tool+args 再次调用直接返回）
            self._store_denied_cache(tool_call.name, tool_call.args, content)
            return ToolResult(
                tool_call_id=tool_call.id,
                content=content,
                variant=ToolResultVariant.USER_DENIED,
                created_at=started,
            )

        # 4. 提取 approval_token 并重试原请求
        approval_token = approval_data.get("approval_token")
        if not approval_token:
            return ToolResult(
                tool_call_id=tool_call.id,
                content="Approval granted but no approval_token returned by server.",
                variant=ToolResultVariant.ERROR,
                error_type="approval_no_token",
                created_at=started,
            )

        # 5. 重试原请求，加 X-Approval-Token 头
        # 重新拼 path/query/body（保持与原请求一致）
        try:
            final_path, query_params, json_body = entry.build_request(tool_call.args)
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=(
                    f"Rebuild request after approval failed: {type(e).__name__}: {e}"
                ),
                variant=ToolResultVariant.ERROR,
                error_type="build_failed",
                created_at=started,
            )
        retry_url = self.base_url + final_path
        retry_headers = {
            "X-Agent-Caller": self.caller,
            "X-Approval-Token": approval_token,
        }

        try:
            retry_resp = self._send_request(
                method=entry.method,
                url=retry_url,
                query=query_params,
                json_body=json_body,
                headers=retry_headers,
            )
        except requests.exceptions.Timeout:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Approved retry timed out after {self.timeout}s",
                variant=ToolResultVariant.TIMEOUT,
                timeout_seconds=self.timeout,
                created_at=started,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Approved retry failed: {type(e).__name__}: {e}",
                variant=ToolResultVariant.ERROR,
                error_type="retry_error",
                created_at=started,
            )

        # 6. 处理重试响应
        if retry_resp.status_code >= 400:
            try:
                retry_body = retry_resp.text[:1000]
            except Exception:
                retry_body = "(unreadable response body)"
            return ToolResult(
                tool_call_id=tool_call.id,
                content=(
                    f"Approved request still failed: HTTP {retry_resp.status_code}: "
                    f"{retry_body}"
                ),
                variant=ToolResultVariant.ERROR,
                error_type="http_error",
                created_at=started,
            )

        # 成功：解析响应体
        try:
            data = retry_resp.json()
            content = json.dumps(data, ensure_ascii=False)
        except (ValueError, json.JSONDecodeError):
            try:
                content = retry_resp.text
            except Exception:
                content = "(unreadable response body)"
        return self._maybe_apply_l0(tool_call, content, started, variant=ToolResultVariant.SUCCESS)

    def _maybe_apply_l0(
        self,
        tool_call: ToolCall,
        content: str,
        started: float,
        *,
        variant: ToolResultVariant,
    ) -> ToolResult:
        """T08: 若 l0_store 注入且 content > 8KB，落盘并返回 preview+path。

        错误响应不落盘（让模型看到完整错误信息便于改道）。
        签名用 variant（spec D6 新真源）替代旧 is_error: bool。
        """
        if self.l0_store is None or variant != ToolResultVariant.SUCCESS:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=content,
                variant=variant,
                created_at=started,
            )
        new_content, artifact_path = self.l0_store.maybe_persist(
            session_id=tool_call.session_id,
            tool_call_id=tool_call.id,
            content=content,
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            content=new_content,
            variant=ToolResultVariant.SUCCESS,
            created_at=started,
            artifact_path=artifact_path,
            preview=new_content if artifact_path else "",
        )
