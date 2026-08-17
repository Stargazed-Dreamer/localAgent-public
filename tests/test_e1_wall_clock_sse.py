"""E1 · wall_clock_budget SSE 循环内检查（spec D21）。

A4 已实现循环边界检查（loop top + before tool batch）。
E1 补 SSE 流式内的检查：在 streaming 事件循环内检测 wall_clock 超时，
设置 LLMResponse.wall_clock_exceeded=True（不立即终止流），runner 看到
flag 后不执行 tool_calls / 不回调 LLM。

D21 验收：超时后当前 streaming 完成，但不执行 tool_calls / 不回调 LLM。

测试策略（regression_only，spec 标注）：
- 测试 wall_clock_exceeded=True 时不执行 tool_calls
- 测试当前 streaming 完成后再停止（不立即中断）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from client.core.agent.event_store import EventStore  # noqa: E402
from client.core.agent.runner import SessionRunner  # noqa: E402
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    ToolResult,
    ToolResultVariant,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    """同步运行 async 协程（独立 event loop）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_e1.db"))
    store.init()
    return store


class _ControllableClock:
    """可控时钟：手动推进时间，用于 wall_clock_budget 测试。"""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, secs: float) -> None:
        self._t += secs


# ============================================================================
# Part 1: LLMResponse 字段
# ============================================================================


class TestLLMResponseWallClockField:
    """LLMResponse 应有 wall_clock_exceeded: bool = False 字段。"""

    def test_default_is_false(self):
        """LLMResponse 默认 wall_clock_exceeded=False。"""
        resp = LLMResponse(content="hello")
        assert resp.wall_clock_exceeded is False

    def test_can_set_true(self):
        """LLMResponse.wall_clock_exceeded 可设 True。"""
        resp = LLMResponse(content="hello", wall_clock_exceeded=True)
        assert resp.wall_clock_exceeded is True


# ============================================================================
# Part 2: Runner 主循环 wall_clock_exceeded 检查
# ============================================================================


class TestRunnerWallClockExceeded:
    """runner.py 主循环应检查 response.wall_clock_exceeded，跳过 tool_calls。"""

    def test_wall_clock_exceeded_skips_tool_calls(self, tmp_path):
        """response.wall_clock_exceeded=True → 不执行 tool_calls，返回 interrupted。

        场景：streaming 中 wall_clock 超时 → wall_clock_exceeded=True
        期望：runner 不执行 tool_calls，返回 RunOutcome(interrupted, wall_clock_budget_exceeded)
        """
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="sess_e1", title="E1 test")
                await store.append_message(
                    "sess_e1",
                    Message(role="user", content="call a tool", source="user"),
                )

                tool_call = {
                    "id": "tc_e1_1",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": '{"path":"/tmp/x"}',
                    },
                }

                # Mock LLM gateway：stream 返回 text + tool_call
                # clock 在 stream 中推进到超时
                clock = _ControllableClock(start=1000.0)

                async def mock_stream(request):
                    # 第一个事件：text_delta，clock 未超时
                    yield {"type": "text_delta", "delta": "I want to call a tool"}
                    # 推进时间到超时（wall_budget=10，run_start_ts=1000，elapsed=15>10）
                    clock.advance(15.0)
                    yield {
                        "type": "tool_call_delta",
                        "tool_call": tool_call,
                    }
                    yield {
                        "type": "usage",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                    yield {"type": "done", "finish_reason": "tool_calls"}

                class MockGateway:
                    async def stream(self, request):
                        async for ev in mock_stream(request):
                            yield ev

                # Mock tool executor：记录是否被调用
                tool_call_count = {"count": 0}

                class MockToolExec:
                    async def execute(self, tool_call):
                        tool_call_count["count"] += 1
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            content="mock result",
                            variant=ToolResultVariant.SUCCESS,
                        )

                config = RunnerConfig(
                    session_id="sess_e1",
                    max_iterations=5,
                    wall_clock_budget_secs=10,  # 10 秒预算
                    use_stream=True,
                )

                runner = SessionRunner(
                    deps=RunnerDeps(
                        event_store=store,
                        llm_gateway=MockGateway(),
                        tool_executor=MockToolExec(),
                        uuid=lambda: "uuid_e1",
                        clock=clock,
                    ),
                    config=config,
                )

                outcome = await runner.run()

                # E1：wall_clock_exceeded=True → 不执行 tool_calls
                assert tool_call_count["count"] == 0, (
                    "E1: response.wall_clock_exceeded=True 时不应执行 tool_calls，"
                    f"但 tool_executor 被调了 {tool_call_count['count']} 次"
                )

                # 返回 interrupted + wall_clock_budget_exceeded
                assert outcome.status == "interrupted"
                assert outcome.stop_reason == "wall_clock_budget_exceeded"

            _run(_run_async())
        finally:
            store.close()

    def test_wall_clock_not_exceeded_executes_tool_calls(self, tmp_path):
        """response.wall_clock_exceeded=False → 正常执行 tool_calls。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="sess_e1b", title="E1 test b")
                await store.append_message(
                    "sess_e1b",
                    Message(role="user", content="call a tool", source="user"),
                )

                tool_call = {
                    "id": "tc_e1b_1",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": '{"path":"/tmp/x"}',
                    },
                }

                async def mock_stream(request):
                    yield {"type": "text_delta", "delta": "reading file"}
                    yield {"type": "tool_call_delta", "tool_call": tool_call}
                    yield {
                        "type": "usage",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                    yield {"type": "done", "finish_reason": "tool_calls"}

                class MockGateway:
                    async def stream(self, request):
                        async for ev in mock_stream(request):
                            yield ev

                tool_call_count = {"count": 0}

                class MockToolExec:
                    async def execute(self, tool_call):
                        tool_call_count["count"] += 1
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            content="mock result",
                            variant=ToolResultVariant.SUCCESS,
                        )

                # clock 返回稳定值（不超时），wall_clock_budget_secs=0 不限制
                def mock_clock():
                    return 1000.0

                config = RunnerConfig(
                    session_id="sess_e1b",
                    max_iterations=5,
                    wall_clock_budget_secs=0,  # 0 = 不限制
                    use_stream=True,
                )

                runner = SessionRunner(
                    deps=RunnerDeps(
                        event_store=store,
                        llm_gateway=MockGateway(),
                        tool_executor=MockToolExec(),
                        uuid=lambda: "uuid_e1b",
                        clock=mock_clock,
                    ),
                    config=config,
                )

                await runner.run()

                # 不超时 → 正常执行 tool_calls
                assert tool_call_count["count"] >= 1, (
                    "E1: wall_clock 未超时时应正常执行 tool_calls"
                )

            _run(_run_async())
        finally:
            store.close()


# ============================================================================
# Part 3: streaming 不立即中断
# ============================================================================


class TestStreamingNotInterruptedImmediately:
    """wall_clock 超时时不立即中断 streaming（D21）。"""

    def test_stream_completes_even_if_wall_clock_exceeded(self, tmp_path):
        """wall_clock 超时后 streaming 仍完成（收到 done 事件），只是 flag 被设。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(
                    session_id="sess_e1c", title="E1 test c"
                )
                await store.append_message(
                    "sess_e1c",
                    Message(role="user", content="say hi", source="user"),
                )

                events_received = {"count": 0, "got_done": False}

                async def mock_stream(request):
                    # 模拟 5 个 text_delta + done
                    for i in range(5):
                        events_received["count"] += 1
                        yield {"type": "text_delta", "delta": f"chunk {i}"}
                    events_received["count"] += 1
                    events_received["got_done"] = True
                    yield {"type": "done", "finish_reason": "stop"}

                class MockGateway:
                    async def stream(self, request):
                        async for ev in mock_stream(request):
                            yield ev

                class MockToolExec:
                    async def execute(self, tool_call):
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            content="ok",
                            variant=ToolResultVariant.SUCCESS,
                        )

                # clock：开始时小，stream 中变大（超时）
                clock = _ControllableClock(start=1000.0)

                # 推进 clock 的 wrapper：每次 yield 后推进时间
                # 由于 mock_stream 是生成器，我们在 stream 内推进
                async def mock_stream_with_clock_advance(request):
                    for i in range(5):
                        events_received["count"] += 1
                        # 第 3 个事件后推进时间到超时
                        if i == 2:
                            clock.advance(15.0)
                        yield {"type": "text_delta", "delta": f"chunk {i}"}
                    events_received["count"] += 1
                    events_received["got_done"] = True
                    yield {"type": "done", "finish_reason": "stop"}

                class MockGatewayAdvanced:
                    async def stream(self, request):
                        async for ev in mock_stream_with_clock_advance(request):
                            yield ev

                config = RunnerConfig(
                    session_id="sess_e1c",
                    max_iterations=5,
                    wall_clock_budget_secs=10,
                    use_stream=True,
                )

                runner = SessionRunner(
                    deps=RunnerDeps(
                        event_store=store,
                        llm_gateway=MockGatewayAdvanced(),
                        tool_executor=MockToolExec(),
                        uuid=lambda: "uuid_e1c",
                        clock=clock,
                    ),
                    config=config,
                )

                await runner.run()

                # streaming 应完成（收到所有 5 个 chunk + done）
                assert events_received["got_done"], (
                    "E1/D21: wall_clock 超时不应立即中断 streaming，应等当前 stream 完成"
                )

            _run(_run_async())
        finally:
            store.close()
