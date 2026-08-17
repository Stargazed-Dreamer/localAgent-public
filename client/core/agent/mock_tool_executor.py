"""MockToolExecutor：脚本化工具执行结果（T02 测试用）

设计依据：
- v6-lite §3 W1：MockToolExecutor 不接真实 server，只回 fixture
- v6-lite §4.5：error-as-output-variant（工具错误是结构化输出，不抛 traceback）
- v6-02 §3：实现 ToolExecutor 协议（async def execute(tool_call) -> ToolResult）

T02 范围：
- 脚本化 / callable / 默认 fixture 三种构造模式
- 不接真实 server（W2+ 用 HttpClientToolExecutor）
- 不做审批（W3+ 桥接 command_guard）

使用方式：
    # 默认：所有工具调用返回固定文本
    mock = MockToolExecutor(default_result="ok")

    # 脚本：按 tool_call.id 或 name 匹配 fixture
    mock = MockToolExecutor(fixtures={
        "call_1": ToolResult(tool_call_id="call_1", content="result-1"),
        "list_dir": ToolResult(tool_call_id="", content="dir-listing"),  # by name
    })

    # callable：动态生成
    mock = MockToolExecutor(callable=lambda tc: ToolResult(
        tool_call_id=tc.id,
        content=f"executed {tc.name} with {tc.args}",
    ))

    # 故障注入：返回 is_error=True
    mock = MockToolExecutor(default_result=ToolResult(
        tool_call_id="", content="connection refused", is_error=True,
    ))
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from client.core.agent.types import ToolCall, ToolResult

logger = logging.getLogger("localagent.agent.mock_tool_executor")


class MockToolExecutor:
    """脚本化工具执行器（实现 ToolExecutor 协议）。

    四种构造模式（按优先级）：
    1. callable: Callable[[ToolCall], ToolResult] —— 动态生成
    2. fixtures: dict[str, ToolResult | str] —— 按 tool_call.id 或 name 匹配
       （fixture 可以是 ToolResult 或 str，str 自动包装为 content）
    3. script: Sequence[ToolResult | str] —— 按调用顺序消费
    4. default_result: ToolResult | str —— 所有调用返回同一结果

    记录所有调用，便于测试断言：
        executor.call_count
        executor.calls[i]   # 第 i 次的 ToolCall
    """

    def __init__(
        self,
        *,
        fixtures: dict[str, ToolResult | str] | None = None,
        script: Sequence[ToolResult | str] | None = None,
        callable: Callable[[ToolCall], ToolResult] | None = None,
        default_result: ToolResult | str = "mock tool result",
    ):
        self._callable = callable
        self._fixtures = fixtures or {}
        self._script = list(script) if script is not None else None
        self._script_index = 0
        self._default_result = default_result

        # 调用记录（测试断言用）
        self.calls: list[ToolCall] = []
        self.results: list[ToolResult] = []
        self.call_count = 0

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """实现 ToolExecutor 协议。

        error-as-output-variant（v6-lite §4.5）：内部异常捕获转 is_error=True，
        不抛 traceback 给上层。
        """
        self.call_count += 1
        self.calls.append(tool_call)
        try:
            result = self._generate(tool_call)
        except Exception as e:
            # 内部异常 → is_error=True 的 ToolResult（不抛给上层）
            result = ToolResult(
                tool_call_id=tool_call.id,
                content=f"MockToolExecutor internal error: {type(e).__name__}: {e}",
                is_error=True,
            )
            logger.exception("MockToolExecutor internal error for tool_call %s", tool_call.id)
        # 确保 tool_call_id 一致
        if not result.tool_call_id:
            result.tool_call_id = tool_call.id
        self.results.append(result)
        logger.debug(
            "MockToolExecutor execute #%d: name=%s, is_error=%s, content=%r",
            self.call_count, tool_call.name, result.is_error, result.content[:80],
        )
        return result

    def _generate(self, tool_call: ToolCall) -> ToolResult:
        if self._callable is not None:
            return self._callable(tool_call)
        # fixtures 优先按 id 匹配，再按 name 匹配
        if self._fixtures:
            if tool_call.id in self._fixtures:
                return self._wrap(self._fixtures[tool_call.id], tool_call.id)
            if tool_call.name in self._fixtures:
                return self._wrap(self._fixtures[tool_call.name], tool_call.id)
        if self._script is not None:
            if self._script_index >= len(self._script):
                raise StopIteration(
                    f"MockToolExecutor script exhausted: index={self._script_index}, "
                    f"len={len(self._script)}, call_count={self.call_count}"
                )
            item = self._script[self._script_index]
            self._script_index += 1
            return self._wrap(item, tool_call.id)
        # 默认：返回 default_result
        return self._wrap(self._default_result, tool_call.id)

    @staticmethod
    def _wrap(item: ToolResult | str, tool_call_id: str) -> ToolResult:
        """把 str 或 ToolResult 统一为 ToolResult（str 自动包装为 content）。"""
        if isinstance(item, ToolResult):
            # 复制一份避免共享引用（确保 tool_call_id 正确）
            return ToolResult(
                tool_call_id=item.tool_call_id or tool_call_id,
                content=item.content,
                is_error=item.is_error,
                artifact_path=item.artifact_path,
                preview=item.preview,
            )
        return ToolResult(tool_call_id=tool_call_id, content=str(item))
