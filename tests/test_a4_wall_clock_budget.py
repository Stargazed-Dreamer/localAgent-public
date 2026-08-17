"""A4 · wall_clock_budget 启用检查机制（regression 测试）

spec D5/D21：
- wall_clock_budget_secs 默认 0（不限制）
- > 0 时按 wall_clock_budget_secs 限制总运行时长
- D21：超时不立即终止，而是在步骤边界（不调 LLM / 不执行 tool_calls）优雅停止
- 返回 RunOutcome(status="interrupted", stop_reason="wall_clock_budget_exceeded")

测试策略（regression_only）：
1. wall_clock_budget_secs=0（默认）→ 不过早终止，正常完成
2. wall_clock_budget_secs=1 + mock clock 推进 → 1 秒后不开始下一步（不调 LLM）
3. wall_clock_budget_secs=N + clock 在 LLM 响应后超 → 不执行 tool_calls
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolCall,
    ToolResult,
)
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
)


def _make_store(tmp_path) -> EventStore:
    db_path = str(tmp_path / "test_a4.db")
    store = EventStore(db_path)
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


class TestA4WallClockBudget:
    """A4: wall_clock_budget 启用检查机制。"""

    def test_no_budget_no_early_termination(self, tmp_path):
        """wall_clock_budget_secs=0（默认）→ 不过早终止，正常完成。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            asyncio.run(store.append_message("s1", Message(role="user", content="hi")))

            llm = MockLLM(default_text="hello back")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    clock=_ControllableClock(start=1000.0),
                ),
                config=RunnerConfig(session_id="s1", max_iterations=3, wall_clock_budget_secs=0),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "completed"
            assert outcome.stop_reason != "wall_clock_budget_exceeded"
        finally:
            store.close()

    def test_budget_exceeded_skips_next_llm_call(self, tmp_path):
        """wall_clock_budget_secs=1 + clock 推进 → 1 秒后不开始下一步（不调 LLM）。

        场景：第一轮 LLM 返回 tool_calls，工具执行后第二轮顶部检查 wall_clock 超限。
        断言：第二轮 LLM 未被调用（llm.call_count 不增加），返回 interrupted。
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            asyncio.run(store.append_message("s2", Message(role="user", content="run tool")))

            # 可控时钟：第一次 clock()=1000，每次调用后 advance
            clock = _ControllableClock(start=1000.0)
            # 让 LLM 调用推进时间（模拟流式调用耗时），返回带 tool_calls 的 LLMResponse
            def llm_callable(req):
                clock.advance(0.5)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "c1", "type": "function",
                         "function": {"name": "noop", "arguments": "{}"}},
                    ],
                    stop_reason="tool_use",
                )

            llm = MockLLM(callable=llm_callable)

            # tool 执行推进 0.6 秒（模拟工具耗时）
            def tool_fn(tc: ToolCall) -> ToolResult:
                clock.advance(0.6)
                return ToolResult(tool_call_id=tc.id, content="ok")

            tool_executor = MockToolExecutor(callable=tool_fn)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=tool_executor,
                    clock=clock,
                ),
                config=RunnerConfig(
                    session_id="s2", max_iterations=5,
                    wall_clock_budget_secs=1,  # 1 秒预算
                    use_stream=False,  # 用 call() 路径，避免 stream 路径的双 call_count
                ),
            )
            outcome = asyncio.run(runner.run())

            # 第一轮：LLM 0.5s + tool 0.6s = 1.1s（已超 1s budget）
            # 第二轮顶部检查 wall_clock → 超限 → 不调 LLM，返回 interrupted
            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "wall_clock_budget_exceeded"
            # llm 只被调用 1 次（第二轮未调）
            assert llm.call_count == 1, (
                f"wall_clock_budget=1 应阻止第二轮 LLM 调用，但 call_count={llm.call_count}"
            )
        finally:
            store.close()

    def test_budget_exceeded_skips_tool_calls_execution(self, tmp_path):
        """wall_clock_budget_secs=N + clock 在 LLM 响应后超 → 不执行 tool_calls。

        场景：第一轮 LLM 调用后 clock 已超 budget，工具执行前检查 wall_clock 超限。
        断言：tool_executor 未被调用，返回 interrupted。
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s3"))
            asyncio.run(store.append_message("s3", Message(role="user", content="run tool")))

            clock = _ControllableClock(start=1000.0)

            def llm_callable(req):
                # LLM 调用推进 2 秒（已超 1s budget）
                clock.advance(2.0)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        {"id": "c1", "type": "function",
                         "function": {"name": "noop", "arguments": "{}"}},
                    ],
                    stop_reason="tool_use",
                )

            llm = MockLLM(callable=llm_callable)

            executed: list[str] = []

            def tool_fn(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                return ToolResult(tool_call_id=tc.id, content="ok")

            tool_executor = MockToolExecutor(callable=tool_fn)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=tool_executor,
                    clock=clock,
                ),
                config=RunnerConfig(
                    session_id="s3", max_iterations=5,
                    wall_clock_budget_secs=1,  # 1 秒预算
                    use_stream=False,
                ),
            )
            outcome = asyncio.run(runner.run())

            # LLM 调用推进 2 秒（已超 1s budget），工具执行前检查 → 超限 → 不执行
            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "wall_clock_budget_exceeded"
            assert len(executed) == 0, (
                f"wall_clock_budget=1 应阻止 tool_calls 执行，但 executed={executed}"
            )
        finally:
            store.close()
