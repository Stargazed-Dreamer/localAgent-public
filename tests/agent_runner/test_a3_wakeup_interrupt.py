"""A3 · exec_python 唤醒决策循环 interrupt 检查（red_green 测试）

spec D9 核心安全约束：用户 interrupt 后，exec_python 唤醒决策循环也必须立即阻断。
A1 处理工具循环，A2 处理 LLM hang，A3 处理 exec_python 唤醒决策循环。

A3 修复点：
- runner.py `_wakeup_llm_for_decision` for 循环内，每个 wakeup_tc 执行前检查
  `self._is_interrupted()`
- 命中 interrupt → break 决策循环 + 返回当前 snap（_build_result_from_inspect）
- 不设轮数上限（D6），只靠 interrupt + wall_clock 兜底

红测试（修复前失败）：
- MockLLM 返回 3 个 tool_calls（exec_inspect/wait/exec_inspect）
- MockToolExecutor 在执行第 2 个工具时 set interrupt_event
- 断言第 3 个工具未执行（修复前会执行）

绿测试（修复后通过）：
- 同样场景，断言第 3 个工具未执行 + 返回当前 snap
"""

from __future__ import annotations

import asyncio
import json
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
    LLMResponse,
    Message,
)


def _make_store(tmp_path) -> EventStore:
    db_path = str(tmp_path / "test_a3.db")
    store = EventStore(db_path)
    store.init()
    return store


def _make_openai_tool_call(tc_id: str, name: str) -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"tid":"t1"}'},
    }


class TestA3WakeupInterrupt:
    """A3: exec_python 唤醒决策循环 interrupt 检查。

    场景：exec_python 返回 terminal_id + status=running，3 分钟唤醒 LLM 决策。
    LLM 返回 3 个 tool_calls，第 2 个执行时 set interrupt_event。
    修复前：第 3 个工具仍会执行（for 循环无 interrupt 检查）。
    修复后：第 3 个工具不执行，返回当前 snap。
    """

    def test_interrupt_during_wakeup_decision_loop(self, tmp_path):
        """红/绿测试：决策循环第 2 个工具执行时 interrupt → 第 3 个工具不应执行。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            asyncio.run(store.append_message("s1", Message(role="user", content="run script")))

            ev = asyncio.Event()
            executed: list[str] = []

            def tool_callable(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                if tc.name == "wait":
                    ev.set()  # 第 2 个工具执行时 set interrupt
                # 返回 inspect 风格的 result（status=running，让循环继续）
                if tc.name == "exec_inspect":
                    return ToolResult(
                        tool_call_id=tc.id,
                        content=json.dumps({
                            "status": "running",
                            "stdout_so_far": "still running",
                            "stderr_so_far": "",
                            "exit_code": None,
                            "elapsed": 200,
                        }),
                    )
                return ToolResult(tool_call_id=tc.id, content='{"status":"ok"}')

            tool_executor = MockToolExecutor(callable=tool_callable)

            # MockLLM 返回 3 个 tool_calls
            llm = MockLLM(callable=lambda req: LLMResponse(
                content="",
                tool_calls=[
                    _make_openai_tool_call("c1", "exec_inspect"),
                    _make_openai_tool_call("c2", "wait"),
                    _make_openai_tool_call("c3", "exec_inspect"),
                ],
                stop_reason="tool_use",
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

            # 直接调 _wakeup_llm_for_decision（绕过 _execute_with_wakeup 的 3 分钟等待）
            tc = ToolCall(id="exec_py_1", name="exec_python", args={},
                          session_id="s1", seq=1, safety="read_only")
            snap = {
                "status": "running",
                "stdout_so_far": "initial output",
                "stderr_so_far": "",
                "exit_code": None,
                "elapsed": 180,
            }
            result = asyncio.run(runner._wakeup_llm_for_decision(
                tc=tc, tid="t1", snap=snap, elapsed_secs=180,
                tool_executor=tool_executor, session_id="s1", store=store,
            ))

            # 核心断言 1：第 3 个工具未执行（修复前会执行）
            # executed 应该是 ["exec_inspect", "wait"]，不应有第 3 个
            assert len(executed) < 3, (
                f"A3 修复前 bug：interrupt 后第 3 个工具仍被执行。executed={executed}"
            )
            # 核心断言 2：返回了 ToolResult（当前 snap）
            assert result is not None
            assert result.tool_call_id == "exec_py_1"
        finally:
            store.close()

    def test_no_interrupt_all_decision_tools_execute(self, tmp_path):
        """回归测试：无 interrupt 时所有决策工具正常执行（不触发提前返回）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            asyncio.run(store.append_message("s2", Message(role="user", content="run script")))

            ev = asyncio.Event()  # 不 set
            executed: list[str] = []

            def tool_callable(tc: ToolCall) -> ToolResult:
                executed.append(tc.name)
                # exec_inspect 返回 running（不触发 done 提前返回），让循环继续
                if tc.name == "exec_inspect":
                    return ToolResult(
                        tool_call_id=tc.id,
                        content=json.dumps({
                            "status": "running",
                            "stdout_so_far": "still running",
                            "stderr_so_far": "",
                            "exit_code": None,
                            "elapsed": 200,
                        }),
                    )
                return ToolResult(tool_call_id=tc.id, content='{"status":"ok"}')

            tool_executor = MockToolExecutor(callable=tool_callable)
            llm = MockLLM(callable=lambda req: LLMResponse(
                content="",
                tool_calls=[
                    _make_openai_tool_call("c1", "exec_inspect"),
                    _make_openai_tool_call("c2", "wait"),
                    _make_openai_tool_call("c3", "exec_inspect"),
                ],
                stop_reason="tool_use",
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

            tc = ToolCall(id="exec_py_2", name="exec_python", args={},
                          session_id="s2", seq=1, safety="read_only")
            snap = {"status": "running", "stdout_so_far": "init", "stderr_so_far": "",
                    "exit_code": None, "elapsed": 180}
            result = asyncio.run(runner._wakeup_llm_for_decision(
                tc=tc, tid="t2", snap=snap, elapsed_secs=180,
                tool_executor=tool_executor, session_id="s2", store=store,
            ))

            # 无 interrupt 时所有工具执行（exec_inspect 返回 running 不触发提前返回）
            assert len(executed) == 3, f"应执行 3 个工具，实际={executed}"
            assert result is not None
        finally:
            store.close()
