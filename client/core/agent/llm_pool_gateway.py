"""v6-lite T05: LLMPoolGateway — 真实 LLM gateway（接 /llm/pool/chat-tools）

设计依据：
- v6-lite §3 W2：LLMGateway（client 侧）新增 POST /llm/pool/chat-tools，
  复用 pool 的 tier/key 轮换；**禁止**引擎自建 provider client
- v6-02 §3：实现 LLMGateway 协议（async def call(request) -> response）
- v6-lite §3 W5（T08）：新增 stream() 方法，接 SSE 端点 /llm/pool/stream
- v6-lite-streaming-gui T02: stream() 加看门狗（spec D15）+ context_overflow 事件

职责：
1. 把 LLMRequest（messages: list[Message]）转成 server 接口要求的 list[dict] 格式
2. POST /llm/pool/chat-tools，带 tools / tool_choice（来自 LLMRequest）
3. 把响应转成 LLMResponse（含 content / tool_calls / usage / stop_reason）
4. T08+: stream() 方法接 SSE 端点，逐事件 yield 给调用方
5. T02（v6-lite-streaming-gui）: stream() 加看门狗（90s 空闲 / 30s stall）+ context_overflow 事件

不依赖 Qt（纯 Python，可在 CLI 脚本和 GUI 中复用）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import requests

from client.core.agent.compactor import ContextOverflow
from client.core.agent.types import LLMRequest, LLMResponse, Message

logger = logging.getLogger("localagent.agent.llm_pool_gateway")

# /llm/pool/chat-tools 端点路径
CHAT_TOOLS_PATH = "/llm/pool/chat-tools"

# T08: SSE 流式端点路径
STREAM_PATH = "/llm/pool/stream"


# T08: SSE 事件类型常量
SSE_EVENT_TEXT_DELTA = "text_delta"
SSE_EVENT_TOOL_CALL_DELTA = "tool_call_delta"
SSE_EVENT_THINKING_DELTA = "thinking_delta"  # v6-lite-streaming-gui T02
SSE_EVENT_USAGE = "usage"
SSE_EVENT_PROVIDER_ERROR = "provider_error"
SSE_EVENT_CONTEXT_OVERFLOW = "context_overflow"  # v6-lite-streaming-gui T02: 413 事件
SSE_EVENT_DONE = "done"


class LLMPoolGateway:
    """真实 LLM gateway：经 server /llm/pool/chat-tools 调用 LLM。

    实现 LLMGateway 协议（async def call(request) -> response）。
    不自建 provider client——所有 key 轮换/重试/熔断由 server 端 pool 处理。

    用法：
        gw = LLMPoolGateway(base_url="http://127.0.0.1:8766")
        resp = await gw.call(LLMRequest(messages=[...], tools=[...]))
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8766",
        *,
        timeout: float = 120.0,
        project: str = "v6-lite-agent",
        use_case: str | None = None,
        model_tier: str | None = None,
        # v6-lite-streaming-gui T02: 看门狗配置（spec D15）
        stream_idle_timeout_secs: int = 90,
        stall_detection_window_secs: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.project = project
        self.use_case = use_case
        self.model_tier = model_tier
        self._session = requests.Session()
        # 看门狗配置（D15）：0 = 禁用
        self.stream_idle_timeout_secs = stream_idle_timeout_secs
        self.stall_detection_window_secs = stall_detection_window_secs

    async def call(self, request: LLMRequest) -> LLMResponse:
        """实现 LLMGateway 协议。

        步骤：
        1. LLMRequest.messages (list[Message]) → list[dict]（OpenAI 消息格式）
        2. POST /llm/pool/chat-tools，带 tools / tool_choice
        3. 响应转 LLMResponse（失败时返回 is_error 风格的 LLMResponse，content 含错误信息）
        """
        # 1. 转消息格式
        messages_dicts = [self._message_to_dict(m) for m in request.messages]

        # 2. 构造请求体
        body: dict[str, Any] = {
            "messages": messages_dicts,
            "project": self.project,
        }
        if request.system:
            # 把 system 提到 messages 最前（如果还没有 system 消息）
            if not messages_dicts or messages_dicts[0].get("role") != "system":
                messages_dicts.insert(0, {"role": "system", "content": request.system})
                body["messages"] = messages_dicts
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = request.tool_choice
        if self.use_case:
            body["use_case"] = self.use_case
        if self.model_tier:
            body["model_tier"] = self.model_tier

        # 3. 发请求
        url = self.base_url + CHAT_TOOLS_PATH
        try:
            resp = self._session.post(url, json=body, timeout=self.timeout)
        except requests.exceptions.Timeout:
            return LLMResponse(
                content=f"[LLMPoolGateway error] request timed out after {self.timeout}s",
                stop_reason="error",
                model="",
            )
        except Exception as e:
            return LLMResponse(
                content=f"[LLMPoolGateway error] {type(e).__name__}: {e}",
                stop_reason="error",
                model="",
            )

        if resp.status_code != 200:
            # T08: 413 / context too large → 抛 ContextOverflow，由 runner 触发 reactive_compact_retry
            # provider 通常返回 413 + body 含 "context_length"/"context window"/"too long" 关键词
            err_text = ""
            try:
                err_text = resp.text[:1000]
            except Exception:
                pass
            err_lower = err_text.lower()
            is_context_overflow = (
                resp.status_code == 413
                or "context_length" in err_lower
                or "context window" in err_lower
                or "maximum context" in err_lower
                or "too long" in err_lower
            )
            if is_context_overflow:
                raise ContextOverflow(
                    f"HTTP {resp.status_code}: {err_text[:300]}",
                    status_code=resp.status_code,
                )
            return LLMResponse(
                content=f"[LLMPoolGateway error] HTTP {resp.status_code}: {resp.text[:500]}",
                stop_reason="error",
                model="",
            )

        # 4. 解析响应
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            return LLMResponse(
                content=f"[LLMPoolGateway error] invalid JSON response: {e}",
                stop_reason="error",
                model="",
            )

        if not data.get("ok"):
            err = data.get("error", "unknown error")
            return LLMResponse(
                content=f"[LLMPoolGateway error] pool.call failed: {err}",
                stop_reason="error",
                model="",
            )

        # 成功
        return LLMResponse(
            content=data.get("content", "") or "",
            tool_calls=data.get("tool_calls") or [],
            usage=data.get("usage") or {},
            stop_reason=self._normalize_stop_reason(data.get("finish_reason", "")),
            model=data.get("model", ""),
        )

    # ------------------------------------------------------------------
    # 消息转换
    # ------------------------------------------------------------------

    def _message_to_dict(self, msg: Message) -> dict:
        """把 Message 转 OpenAI 消息 dict。

        - role=user/assistant/system: {"role": ..., "content": str|list}
        - role=tool（工具结果）: {"role": "tool", "content": str, "tool_call_id": ...}
        - assistant 带 tool_calls: {"role": "assistant", "content": str, "tool_calls": list[dict]}
        """
        out: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_call_id:
            out["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls:
            out["tool_calls"] = msg.tool_calls
        return out

    def _normalize_stop_reason(self, finish_reason: str) -> str:
        """把 provider 的 finish_reason 归一化到 v6-lite 的 stop_reason。

        OpenAI: stop / tool_calls / length / content_filter
        Anthropic: end_turn / tool_use / max_tokens / stop_sequence
        v6-lite 统一用：end_turn / tool_use / max_tokens / error
        """
        if not finish_reason:
            return "end_turn"
        fr = finish_reason.lower()
        if fr in ("stop", "end_turn"):
            return "end_turn"
        if fr in ("tool_calls", "tool_use"):
            return "tool_use"
        if fr in ("length", "max_tokens"):
            return "max_tokens"
        if fr in ("content_filter",):
            return "content_filter"
        if fr in ("error",):
            return "error"
        return fr

    # ------------------------------------------------------------------
    # T08: SSE 流式接口（v6-lite-streaming-gui T02: 真流式 + 看门狗）
    # ------------------------------------------------------------------

    async def stream(self, request: LLMRequest) -> AsyncIterator[dict]:
        """SSE 流式调用，逐事件 yield（v6-lite-streaming-gui T02 真流式 + 看门狗）。

        事件格式（dict）：
        - {"type": "text_delta", "delta": "..."}
        - {"type": "thinking_delta", "delta": "..."}
        - {"type": "tool_call_delta", "tool_call": {...}}
        - {"type": "usage", "prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
        - {"type": "provider_error", "error": "..."}
        - {"type": "context_overflow", "error": "...", "status_code": 413}  # T02: 413 事件
        - {"type": "done", "finish_reason": "..."}

        看门狗（D15）：
        - stream_idle_timeout_secs：N 秒无任何数据 → yield provider_error
        - stall_detection_window_secs：N 秒窗口内 token 增量不足 → yield provider_error
        - 0 = 禁用对应看门狗

        客户端可通过 break/asyncio.CancelledError 中断迭代，
        server 端会在下一次 is_disconnected() 检查时停止发送。

        失败时（HTTP 错误、网络异常）只 yield 一个 provider_error + done 事件，
        不抛异常（与 call() 的 error-as-output-variant 保持一致）。
        413 context overflow 时 yield context_overflow 事件（runner 收到后抛 ContextOverflow）。
        """
        # 构造请求体（与 call() 共用逻辑，但 stream 用 POST /llm/pool/stream）
        body = self._build_request_body(request)
        url = self.base_url + STREAM_PATH

        try:
            # stream=True 让 requests 不缓冲整个响应
            resp = self._session.post(url, json=body, timeout=self.timeout, stream=True)
        except requests.exceptions.Timeout:
            yield {"type": SSE_EVENT_PROVIDER_ERROR, "error": f"request timed out after {self.timeout}s"}
            yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            return
        except Exception as e:
            yield {"type": SSE_EVENT_PROVIDER_ERROR, "error": f"{type(e).__name__}: {e}"}
            yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            return

        if resp.status_code != 200:
            try:
                err_body = resp.text[:500]
            except Exception:
                err_body = "(unreadable)"
            err_lower = err_body.lower()
            # T02: 413 context overflow → yield context_overflow 事件（runner 抛 ContextOverflow）
            is_context_overflow = (
                resp.status_code == 413
                or "context_length" in err_lower
                or "context window" in err_lower
                or "maximum context" in err_lower
                or "too long" in err_lower
            )
            if is_context_overflow:
                yield {
                    "type": SSE_EVENT_CONTEXT_OVERFLOW,
                    "error": f"HTTP {resp.status_code}: {err_body[:300]}",
                    "status_code": resp.status_code,
                }
                yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            else:
                yield {
                    "type": SSE_EVENT_PROVIDER_ERROR,
                    "error": f"HTTP {resp.status_code}: {err_body}",
                }
                yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
            resp.close()
            return

        # 逐行解析 SSE 事件，应用看门狗（D15）
        try:
            async for event in self._iter_sse_with_watchdog(resp):
                yield event
                if event.get("type") == SSE_EVENT_DONE:
                    break
        finally:
            resp.close()

    async def _iter_sse_with_watchdog(self, resp) -> AsyncIterator[dict]:
        """带看门狗的 SSE 事件迭代器（spec D15）。

        把 requests.Response.iter_lines（同步阻塞）桥接为 async iterator，
        并应用两个看门狗：
        - 空闲超时：stream_idle_timeout_secs 秒内无任何数据 → yield provider_error
        - stall 检测：stall_detection_window_secs 秒窗口内 token 增量不足 → yield provider_error

        看门狗命中后 yield provider_error + done，终止迭代（StallError 可重试，由 runner 决定）。
        """
        idle_timeout = self.stream_idle_timeout_secs
        stall_window = self.stall_detection_window_secs

        # 同步 iter_lines 桥接到异步：用 run_in_executor 拉一行，asyncio.wait_for 控超时
        loop = asyncio.get_event_loop()
        line_iter = iter(resp.iter_lines(decode_unicode=True))

        # stall 检测状态
        window_start = time.monotonic()
        window_token_count = 0
        # stall 阈值：stall_window 秒内 token 增量 < 此值 → stall
        # 简化：用字符数近似（1 token ≈ 4 字符），阈值 = 10 tokens
        STALL_MIN_TOKENS = 10

        while True:
            # 用 run_in_executor 拉下一行（同步阻塞调用扔到线程池）
            try:
                if idle_timeout > 0:
                    raw_line = await asyncio.wait_for(
                        loop.run_in_executor(None, self._next_line, line_iter),
                        timeout=idle_timeout,
                    )
                else:
                    raw_line = await loop.run_in_executor(None, self._next_line, line_iter)
            except TimeoutError:
                # 空闲超时（D15）：N 秒无任何数据
                logger.warning(
                    "SSE stream idle timeout: %ds without data, yielding provider_error",
                    idle_timeout,
                )
                yield {
                    "type": SSE_EVENT_PROVIDER_ERROR,
                    "error": f"stream idle timeout: no data for {idle_timeout}s",
                }
                yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
                return
            except StopIteration:
                # 迭代器耗尽（provider 关闭连接，可能未发 done）
                return

            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload_str = line[len("data:"):].strip()
            if not payload_str:
                continue
            try:
                event = json.loads(payload_str)
            except json.JSONDecodeError:
                logger.debug("skip unparseable SSE line: %r", payload_str[:80])
                continue

            # stall 检测：text_delta/thinking_delta 增量计入 token 窗口
            if stall_window > 0:
                now = time.monotonic()
                if event.get("type") in (SSE_EVENT_TEXT_DELTA, SSE_EVENT_THINKING_DELTA):
                    delta = event.get("delta", "")
                    # 粗略 token 估算：字符数 / 4
                    window_token_count += max(1, len(delta) // 4)
                # 检查窗口是否到期
                if now - window_start >= stall_window:
                    if window_token_count < STALL_MIN_TOKENS:
                        logger.warning(
                            "SSE stream stall detected: only %d tokens in %ds window, yielding provider_error",
                            window_token_count, stall_window,
                        )
                        yield {
                            "type": SSE_EVENT_PROVIDER_ERROR,
                            "error": (
                                f"stream stall: only {window_token_count} tokens "
                                f"in {stall_window}s window"
                            ),
                        }
                        yield {"type": SSE_EVENT_DONE, "finish_reason": "error"}
                        return
                    # 重置窗口
                    window_start = now
                    window_token_count = 0

            yield event

    @staticmethod
    def _next_line(line_iter):
        """安全读取下一行（run_in_executor 用），StopIteration 透传给调用方。"""
        return next(line_iter)

    def _build_request_body(self, request: LLMRequest) -> dict:
        """从 LLMRequest 构造 server 请求体（call 和 stream 共用）。"""
        messages_dicts = [self._message_to_dict(m) for m in request.messages]
        body: dict[str, Any] = {
            "messages": messages_dicts,
            "project": self.project,
        }
        if request.system:
            if not messages_dicts or messages_dicts[0].get("role") != "system":
                messages_dicts.insert(0, {"role": "system", "content": request.system})
                body["messages"] = messages_dicts
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = request.tool_choice
        if self.use_case:
            body["use_case"] = self.use_case
        if self.model_tier:
            body["model_tier"] = self.model_tier
        return body
