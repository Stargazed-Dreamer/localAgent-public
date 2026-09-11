"""A1 · 工具执行循环 interrupt 检查（red_green 测试）

spec D9 核心安全约束：用户 interrupt 后，任何工具执行必须立即阻断。
A1 修复点：runner.py `_run_loop` 工具执行 for 循环内，每个 tool_call 执行前检查
`self._is_interrupted()`。命中 → break 工具循环 + 写 transition(user_interrupted)
+ 返回 RunOutcome(status="interrupted")。

红测试（修复前失败）：
- MockLLM 返回 3 个 tool_calls
- MockToolExecutor 在执行第 2 个工具时 set interrupt_event
- 断言第 3 个工具未执行（修复前会执行）
- 断言 RunOutcome(status="interrupted")（修复前不是 interrupted）

绿测试（修复后通过）：
- 同样场景，断言第 3 个工具未执行 + RunOutcome(interrupted)
- 已执行的 tool_result 保留，未执行的不补
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
    TRANSITION_USER_INTERRUPTED,
    LLMResponse,
    Message,
)


def _make_store(tmp_path) -> EventStore:
    db_path = str(tmp_path / "test_a1.db")
    store = EventStore(db_path)
    store.init()
    return store


def _make_tool_call(tc_id: str, name: str) -> dict:
    """OpenAI 格式 tool_call dict。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


class TestA1ToolLoopInterrupt:
    """A1: 工具执行循环 interrupt 检查。

    场景：LLM 返回 3 个 tool_calls，第 2 个工具执行时 set interrupt_event。
    修复前：第 3 个工具仍会执行（for 循环无 interrupt 检查）。
    修复后：第 3 个工具不执行，RunOutcome(status="interrupted")。
    """

    def test_interrupt_during_tool_loop_stops_next_tool(self, tmp_path):
        """红测试：第 2 个工具执行时 interrupt → 第 3 个工具不应执行。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            asyncio.run(store.append_message("s1", Message(role="user", content="run 3 tools")))

            ev = asyncio.Event()
            executed: list[str] = []  # 记录执行的工具名

            def tool_callable(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                # 第 2 个工具执行时 set interrupt
                if tc.name == "tool_2":
                    ev.set()
                return ToolResult(
                    tool_call_id=tc.id,
                    content=f"result of {tc.name}",
                )

            tool_executor = MockToolExecutor(callable=tool_callable)

            # MockLLM 返回 3 个 tool_calls
            llm = MockLLM(callable=lambda req: LLMResponse(
                content="",
                tool_calls=[
                    _make_tool_call("call_1", "tool_1"),
                    _make_tool_call("call_2", "tool_2"),
                    _make_tool_call("call_3", "tool_3"),
                ],
                stop_reason="tool_use",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=tool_executor,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=3),
            )
            outcome = asyncio.run(runner.run())

            # 核心断言 1：第 3 个工具未执行（修复前会执行）
            assert "tool_3" not in executed, (
                f"A1 修复前 bug：interrupt 后第 3 个工具仍被执行。executed={executed}"
            )
            # 核心断言 2：RunOutcome status=interrupted
            assert outcome.status == "interrupted", (
                f"A1 修复前 bug：status 应为 interrupted，实际={outcome.status}。"
                f"executed={executed}"
            )
            assert outcome.stop_reason == "user_interrupted"
            # 已执行的工具保留
            assert "tool_1" in executed
            assert "tool_2" in executed
        finally:
            store.close()

    def test_interrupt_writes_user_interrupted_transition(self, tmp_path):
        """绿测试补充：interrupt 后写 transition(user_interrupted) 事件。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            asyncio.run(store.append_message("s2", Message(role="user", content="run 2 tools")))

            ev = asyncio.Event()
            executed: list[str] = []

            def tool_callable(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                if tc.name == "tool_a":
                    ev.set()
                return ToolResult(tool_call_id=tc.id, content=f"result of {tc.name}")

            tool_executor = MockToolExecutor(callable=tool_callable)
            llm = MockLLM(callable=lambda req: LLMResponse(
                content="",
                tool_calls=[
                    _make_tool_call("call_a", "tool_a"),
                    _make_tool_call("call_b", "tool_b"),
                ],
                stop_reason="tool_use",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=tool_executor,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s2", max_iterations=3),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "interrupted"
            assert "tool_b" not in executed

            # 验证 transition(user_interrupted) 事件写入
            events = asyncio.run(store.load_events("s2"))
            transitions = [
                ev for ev in events
                if ev.type == "transition"
                and ev.payload.get("reason") == TRANSITION_USER_INTERRUPTED
            ]
            assert len(transitions) >= 1, (
                f"应写 transition(user_interrupted) 事件，"
                f"实际 transitions={[e.payload for e in events if e.type == 'transition']}"
            )

            # 验证 session 状态为 interrupted
            session = asyncio.run(store.get_session("s2"))
            assert session.status == "interrupted"
        finally:
            store.close()

    def test_no_interrupt_all_tools_execute(self, tmp_path):
        """回归测试：无 interrupt 时所有工具正常执行。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s3"))
            asyncio.run(store.append_message("s3", Message(role="user", content="run 3 tools")))

            ev = asyncio.Event()  # 不 set
            executed: list[str] = []

            def tool_callable(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                return ToolResult(tool_call_id=tc.id, content=f"result of {tc.name}")

            tool_executor = MockToolExecutor(callable=tool_callable)
            call_count = [0]

            def llm_callable(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            _make_tool_call("c1", "tool_1"),
                            _make_tool_call("c2", "tool_2"),
                            _make_tool_call("c3", "tool_3"),
                        ],
                        stop_reason="tool_use",
                        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    )
                return LLMResponse(content="done", stop_reason="end_turn")

            llm = MockLLM(callable=llm_callable)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    tool_executor=tool_executor,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s3", max_iterations=3),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "completed"
            assert executed == ["tool_1", "tool_2", "tool_3"]
        finally:
            store.close()
