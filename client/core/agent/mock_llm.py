"""MockLLM：脚本化 LLM 响应（T01 测试用）

设计依据：
- v6-lite §3 W1：MockLLM 脚本化响应（文本轮/工具轮/终止轮），单测跑通完整循环
- v6-02 §3：实现 LLMGateway 协议（async def call(request) -> response）

T01 范围：仅文本轮响应。
T02+：脚本化 tool_calls（MockToolExecutor 单独实现）。

使用方式：
    mock = MockLLM(script=["你好，我是 MockLLM。"])
    runner = SessionRunner(deps=RunnerDeps(llm_gateway=mock), config=...)
    await runner.run()

    # 或脚本队列：每轮消费一条
    mock = MockLLM(script=["第一轮回复", "第二轮回复"])

    # 或 callable：根据 request 动态生成
    mock = MockLLM(callable=lambda req: LLMResponse(content=f"echo: {req.messages[-1].content}"))
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from client.core.agent.types import LLMRequest, LLMResponse

logger = logging.getLogger("localagent.agent.mock_llm")


class MockLLM:
    """脚本化 LLM 网关（实现 LLMGateway 协议）。

    三种构造模式（按优先级）：
    1. callable: Callable[[LLMRequest], LLMResponse] —— 动态生成
    2. script: Sequence[str | LLMResponse] —— 按调用顺序消费，超出则抛 StopIteration
    3. default_text: str —— 每次返回同一文本

    记录所有调用，便于测试断言：
        mock.call_count
        mock.requests[i]   # 第 i 次的 LLMRequest
    """

    def __init__(
        self,
        *,
        script: Sequence[str | LLMResponse] | None = None,
        callable: Callable[[LLMRequest], LLMResponse] | None = None,
        default_text: str = "MockLLM response",
        default_usage: dict[str, int] | None = None,
    ):
        self._callable = callable
        self._script = list(script) if script is not None else None
        self._script_index = 0
        self._default_text = default_text
        self._default_usage = default_usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        # 调用记录（测试断言用）
        self.requests: list[LLMRequest] = []
        self.responses: list[LLMResponse] = []
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        """实现 LLMGateway 协议。"""
        self.call_count += 1
        self.requests.append(request)
        response = self._generate(request)
        self.responses.append(response)
        logger.debug(
            "MockLLM call #%d: messages=%d, response_content=%r",
            self.call_count, len(request.messages), response.content[:80],
        )
        return response

    async def stream(self, request: LLMRequest):
        """v6-lite-streaming-gui T02: 流式协议实现。

        把 _generate() 的 LLMResponse 转成事件序列：
        - thinking_delta（如果有 thinking）
        - text_delta（content）
        - tool_call_delta（每个 tool_call）
        - usage
        - done

        让旧测试（用 MockLLM + use_stream=True 默认）自动兼容流式路径。
        """
        self.call_count += 1
        self.requests.append(request)
        response = self._generate(request)
        self.responses.append(response)
        logger.debug(
            "MockLLM stream #%d: messages=%d, response_content=%r",
            self.call_count, len(request.messages), response.content[:80],
        )
        # 转成事件序列
        if response.thinking:
            yield {"type": "thinking_delta", "delta": response.thinking}
        if response.content:
            yield {"type": "text_delta", "delta": response.content}
        for tc in response.tool_calls:
            yield {"type": "tool_call_delta", "tool_call": tc}
        if response.usage:
            yield {
                "type": "usage",
                "prompt_tokens": response.usage.get("prompt_tokens", 0),
                "completion_tokens": response.usage.get("completion_tokens", 0),
                "total_tokens": response.usage.get("total_tokens", 0),
            }
        # finish_reason 映射：end_turn → stop, tool_use → tool_calls
        fr = response.stop_reason
        if fr == "end_turn":
            fr = "stop"
        elif fr == "tool_use":
            fr = "tool_calls"
        yield {"type": "done", "finish_reason": fr}

    def _generate(self, request: LLMRequest) -> LLMResponse:
        if self._callable is not None:
            return self._callable(request)
        if self._script is not None:
            if self._script_index >= len(self._script):
                raise StopIteration(
                    f"MockLLM script exhausted: index={self._script_index}, "
                    f"len={len(self._script)}, call_count={self.call_count}"
                )
            item = self._script[self._script_index]
            self._script_index += 1
            if isinstance(item, str):
                return LLMResponse(
                    content=item,
                    usage=dict(self._default_usage),
                    stop_reason="end_turn",
                    model="mock-llm",
                )
            return item
        # 默认：每轮返回 default_text
        return LLMResponse(
            content=self._default_text,
            usage=dict(self._default_usage),
            stop_reason="end_turn",
            model="mock-llm",
        )


# LLMGateway 协议运行时检查由测试断言（isinstance(mock, LLMGateway)）；
# 此处不做模块级 isinstance 自检，避免给 MockLLM 加必填参数时模块导入失败。
