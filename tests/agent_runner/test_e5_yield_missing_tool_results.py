"""E5 · yieldMissingToolResultBlocks 三处全覆盖（spec D7 数据完整性）

v6-01 §3.2：任何抛错或 abort 路径都调用 yieldMissingToolResultBlocks 生成器，
给每个未配对的 tool_use 注入 is_error=true 的 tool_result（防 provider 400）。

三处全覆盖：
1. 启动恢复（reconciler.py）：已由 C4/reconciler 实现（扫描 pending/running tool_calls
   无 tool_result → 补 is_error=true tool_result）
2. model_error 路径（runner.py）：LLM 返回 stop_reason="error" + tool_calls 时，
   不执行 tool_calls，直接补 is_error=true tool_result + 返回 failed
3. abort 路径（runner.py）：用户中断/wall_clock 超时等 abort 路径返回前，
   扫描 dangling tool_calls → 补 is_error=true tool_result

测试策略（red_green: required）：
- 红测试：mock model_error/abort 路径不补 tool_result，断言 dangling 存在（修复前失败）
- 绿测试：修复后断言三处都补 tool_result，无 dangling

不依赖后端运行，全部用 mock。
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
    LLMResponse,
    Message,
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
    store = EventStore(db_path=str(tmp_path / "test_e5.db"))
    store.init()
    return store


def _make_tool_call(tc_id: str, name: str) -> dict:
    """OpenAI 格式 tool_call dict。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


class _ControllableClock:
    """可控时钟：手动推进时间，用于 wall_clock_budget 测试。"""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, secs: float) -> None:
        self._t += secs


# ============================================================================
# Part 1: model_error 路径补 tool_result
# ============================================================================


class TestModelErrorFillsToolResults:
    """E5: model_error + tool_calls → 不执行 tool_calls + 补 is_error=true tool_result。

    场景：LLM 返回 stop_reason="error" 且携带 tool_calls（部分流式 tool_calls
    可能有 corrupt arguments）。当前行为是执行 tool_calls（有风险）。
    E5 修复：不执行 tool_calls，补 is_error=true tool_result，返回 failed。
    """

    def test_model_error_with_tool_calls_fills_error_results(self, tmp_path):
        """model_error + tool_calls → 每个 tool_call 都有 is_error=true tool_result。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_err")
                await store.append_message("s_err", Message(
                    role="user", content="call tools", source="user",
                ))

                # MockLLM 返回 stop_reason="error" + 2 个 tool_calls
                llm = MockLLM(callable=lambda req: LLMResponse(
                    content="[stream error] provider timeout",
                    tool_calls=[
                        _make_tool_call("tc_err_1", "tool_a"),
                        _make_tool_call("tc_err_2", "tool_b"),
                    ],
                    stop_reason="error",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ))

                executed: list[str] = []
                def tool_fn(tc: ToolCall) -> ToolResult:
                    executed.append(tc.name)
                    return ToolResult(tool_call_id=tc.id, content="should not reach")

                tool_executor = MockToolExecutor(callable=tool_fn)

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=tool_executor,
                    ),
                    config=RunnerConfig(
                        session_id="s_err", max_iterations=3,
                        use_stream=False,  # 直接 call()，返回 stop_reason="error"
                    ),
                )
                outcome = await runner.run()

                # E5：model_error + tool_calls → 不执行 tool_calls
                assert len(executed) == 0, (
                    f"E5: model_error 时不应执行 tool_calls，但 executed={executed}"
                )

                # E5：每个 tool_call 都应有 is_error=true tool_result
                has_tc1 = await store.has_tool_result("tc_err_1")
                has_tc2 = await store.has_tool_result("tc_err_2")
                assert has_tc1, (
                    "E5: model_error 后 tc_err_1 应有 is_error=true tool_result（dangling）"
                )
                assert has_tc2, (
                    "E5: model_error 后 tc_err_2 应有 is_error=true tool_result（dangling）"
                )

                # 验证 tool_result 是 is_error=true（dangling/error 标记）
                messages = await store.load_messages("s_err")
                tool_msgs = [m for m in messages if m.role == "tool"]
                assert len(tool_msgs) == 2, (
                    f"E5: 应有 2 条 tool_result 消息，实际 {len(tool_msgs)}"
                )
                for tm in tool_msgs:
                    # tool_result content 应含 error/dangling/interrupted 提示
                    content_lower = (tm.content or "").lower()
                    assert any(kw in content_lower for kw in
                               ["error", "dangling", "interrupt", "provider"]), (
                        f"E5: model_error 补的 tool_result 应标 error，"
                        f"content={tm.content!r}"
                    )

                # 返回 failed（model_error 不应伪装 completed）
                assert outcome.status == "failed", (
                    f"E5: model_error + tool_calls 应返回 failed，"
                    f"实际 status={outcome.status}"
                )

            _run(run())
        finally:
            store.close()

    def test_model_error_without_tool_calls_keeps_current_behavior(self, tmp_path):
        """model_error + 无 tool_calls → 保持当前行为（completed），不补任何 tool_result。

        回归测试：E5 只在 model_error + tool_calls 时改变行为。
        无 tool_calls 时 model_error 落到正常 finalize 路径（pre-existing behavior）。
        """
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_err_notc")
                await store.append_message("s_err_notc", Message(
                    role="user", content="hi", source="user",
                ))

                llm = MockLLM(callable=lambda req: LLMResponse(
                    content="[stream error] timeout",
                    tool_calls=[],  # 无 tool_calls
                    stop_reason="error",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ))

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=MockToolExecutor(),
                    ),
                    config=RunnerConfig(
                        session_id="s_err_notc", max_iterations=3,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()

                # 无 tool_calls → 落到 finalize 路径（pre-existing: completed）
                # E5 不改变此行为
                assert outcome.status == "completed"

                # 无 tool_result 消息
                messages = await store.load_messages("s_err_notc")
                tool_msgs = [m for m in messages if m.role == "tool"]
                assert len(tool_msgs) == 0

            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 2: abort 路径（工具循环中断）补 tool_result
# ============================================================================


class TestAbortToolLoopInterruptFillsToolResults:
    """E5: 工具循环中 interrupt → 未执行的 tool_calls 补 is_error=true tool_result。

    场景：LLM 返回 3 个 tool_calls，执行第 2 个时 interrupt_event.set()。
    A1 修复：第 3 个 tool_call 不执行。
    E5 修复：第 3 个 tool_call 补 is_error=true tool_result（防 dangling）。
    """

    def test_tool_loop_interrupt_fills_unexecuted_tool_results(self, tmp_path):
        """工具循环中断 → 未执行的 tool_call 补 is_error=true tool_result。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_abort")
                await store.append_message("s_abort", Message(
                    role="user", content="run 3 tools", source="user",
                ))

                ev = asyncio.Event()
                executed: list[str] = []

                def tool_fn(tc: ToolCall) -> ToolResult:
                    executed.append(tc.name)
                    if tc.name == "tool_2":
                        ev.set()
                    return ToolResult(
                        tool_call_id=tc.id,
                        content=f"result of {tc.name}",
                    )

                tool_executor = MockToolExecutor(callable=tool_fn)
                llm = MockLLM(callable=lambda req: LLMResponse(
                    content="",
                    tool_calls=[
                        _make_tool_call("tc_abort_1", "tool_1"),
                        _make_tool_call("tc_abort_2", "tool_2"),
                        _make_tool_call("tc_abort_3", "tool_3"),
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
                    config=RunnerConfig(session_id="s_abort", max_iterations=3),
                )
                outcome = await runner.run()

                # A1：第 3 个工具未执行
                assert "tool_3" not in executed
                assert outcome.status == "interrupted"

                # E5：第 3 个 tool_call 应有 is_error=true tool_result（dangling 修复）
                has_tc1 = await store.has_tool_result("tc_abort_1")
                has_tc2 = await store.has_tool_result("tc_abort_2")
                has_tc3 = await store.has_tool_result("tc_abort_3")
                assert has_tc1, "tc_abort_1 已执行，应有 tool_result"
                assert has_tc2, "tc_abort_2 已执行，应有 tool_result"
                assert has_tc3, (
                    "E5: 工具循环中断后 tc_abort_3 未执行，"
                    "应补 is_error=true tool_result（防 dangling）"
                )

                # 验证 tc_abort_3 的 tool_result 是 error（dangling 标记）
                messages = await store.load_messages("s_abort")
                tool_msgs = [m for m in messages if m.role == "tool"]
                tc3_msg = next(
                    (m for m in tool_msgs if m.tool_call_id == "tc_abort_3"), None,
                )
                assert tc3_msg is not None, "tc_abort_3 应有 tool_result 消息"
                content_lower = (tc3_msg.content or "").lower()
                assert any(kw in content_lower for kw in
                           ["error", "dangling", "interrupt"]), (
                    f"E5: 未执行的 tool_call 补的 tool_result 应标 error/dangling，"
                    f"content={tc3_msg.content!r}"
                )

            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 3: abort 路径（wall_clock_exceeded after LLM）补 tool_result
# ============================================================================


class TestAbortWallClockExceededAfterLLMFillsToolResults:
    """E5: wall_clock_exceeded after LLM → 未执行的 tool_calls 补 is_error=true tool_result。

    场景：LLM streaming 中 wall_clock 超时 → wall_clock_exceeded=True。
    runner 看到 flag 后不执行 tool_calls，返回 interrupted。
    E5 修复：返回前补 is_error=true tool_result for all tool_calls。
    """

    def test_wall_clock_exceeded_after_llm_fills_tool_results(self, tmp_path):
        """wall_clock_exceeded=True → 不执行 tool_calls + 补 is_error tool_result。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_wc1")
                await store.append_message("s_wc1", Message(
                    role="user", content="call tools", source="user",
                ))

                tool_calls = [
                    _make_tool_call("tc_wc1_1", "tool_a"),
                    _make_tool_call("tc_wc1_2", "tool_b"),
                ]

                # 自定义 mock gateway：stream 返回 tool_calls + wall_clock 超时
                clock = _ControllableClock(start=1000.0)

                async def mock_stream(request):
                    yield {"type": "text_delta", "delta": "calling tools"}
                    # 推进时间到超时（wall_budget=10，run_start_ts=1000，elapsed=15>10）
                    clock.advance(15.0)
                    for tc in tool_calls:
                        yield {"type": "tool_call_delta", "tool_call": tc}
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

                executed: list[str] = []
                class MockToolExec:
                    async def execute(self, tc):
                        executed.append(tc.name)
                        return ToolResult(tool_call_id=tc.id, content="ok")

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=MockGateway(),
                        event_store=store,
                        tool_executor=MockToolExec(),
                        uuid=lambda: "uuid_e5_wc1",
                        clock=clock,
                    ),
                    config=RunnerConfig(
                        session_id="s_wc1", max_iterations=3,
                        wall_clock_budget_secs=10,
                        use_stream=True,
                    ),
                )
                outcome = await runner.run()

                # E1：wall_clock_exceeded → 不执行 tool_calls
                assert len(executed) == 0
                assert outcome.status == "interrupted"
                assert outcome.stop_reason == "wall_clock_budget_exceeded"

                # E5：未执行的 tool_calls 应补 is_error=true tool_result
                has_tc1 = await store.has_tool_result("tc_wc1_1")
                has_tc2 = await store.has_tool_result("tc_wc1_2")
                assert has_tc1, (
                    "E5: wall_clock_exceeded 后 tc_wc1_1 未执行，"
                    "应补 is_error=true tool_result（防 dangling）"
                )
                assert has_tc2, (
                    "E5: wall_clock_exceeded 后 tc_wc1_2 未执行，"
                    "应补 is_error=true tool_result（防 dangling）"
                )

                # 验证 tool_result 是 error
                messages = await store.load_messages("s_wc1")
                tool_msgs = [m for m in messages if m.role == "tool"]
                assert len(tool_msgs) == 2, (
                    f"E5: 应补 2 条 tool_result，实际 {len(tool_msgs)}"
                )
                for tm in tool_msgs:
                    content_lower = (tm.content or "").lower()
                    assert any(kw in content_lower for kw in
                               ["error", "dangling", "interrupt"]), (
                        f"E5: wall_clock 超时补的 tool_result 应标 error，"
                        f"content={tm.content!r}"
                    )

            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 4: abort 路径（wall_clock_exceeded before tool execution）补 tool_result
# ============================================================================


class TestAbortWallClockExceededBeforeToolExecFillsToolResults:
    """E5: wall_clock_exceeded before tool execution → 补 is_error tool_result。

    场景：LLM 返回 tool_calls，clock 在 LLM 响应后超 budget，
    工具执行前检查 wall_clock 超限 → 返回 interrupted。
    E5 修复：返回前补 is_error=true tool_result for all tool_calls。
    """

    def test_wall_clock_exceeded_before_tool_exec_fills_tool_results(self, tmp_path):
        """wall_clock 超时 before tool execution → 补 is_error tool_result。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_wc2")
                await store.append_message("s_wc2", Message(
                    role="user", content="call tools", source="user",
                ))

                clock = _ControllableClock(start=1000.0)

                def llm_callable(req):
                    # LLM 调用推进 2 秒（已超 1s budget）
                    clock.advance(2.0)
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            _make_tool_call("tc_wc2_1", "tool_a"),
                            _make_tool_call("tc_wc2_2", "tool_b"),
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
                        session_id="s_wc2", max_iterations=3,
                        wall_clock_budget_secs=1,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()

                # A4：wall_clock 超时 → 不执行 tool_calls
                assert len(executed) == 0
                assert outcome.status == "interrupted"
                assert outcome.stop_reason == "wall_clock_budget_exceeded"

                # E5：未执行的 tool_calls 应补 is_error=true tool_result
                has_tc1 = await store.has_tool_result("tc_wc2_1")
                has_tc2 = await store.has_tool_result("tc_wc2_2")
                assert has_tc1, (
                    "E5: wall_clock 超时 before tool exec 后 tc_wc2_1 未执行，"
                    "应补 is_error=true tool_result（防 dangling）"
                )
                assert has_tc2, (
                    "E5: wall_clock 超时 before tool exec 后 tc_wc2_2 未执行，"
                    "应补 is_error=true tool_result（防 dangling）"
                )

                # 验证 tool_result 是 error
                messages = await store.load_messages("s_wc2")
                tool_msgs = [m for m in messages if m.role == "tool"]
                assert len(tool_msgs) == 2, (
                    f"E5: 应补 2 条 tool_result，实际 {len(tool_msgs)}"
                )

            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 5: 回归测试（无 abort 时无副作用）
# ============================================================================


class TestNoAbortNoSideEffects:
    """E5: 无 abort/model_error 时不应补任何 tool_result（行为不变）。"""

    def test_normal_completion_no_extra_tool_results(self, tmp_path):
        """正常完成（无 abort）→ 不补 is_error tool_result。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_ok")
                await store.append_message("s_ok", Message(
                    role="user", content="call 1 tool", source="user",
                ))

                call_count = [0]
                def llm_callable(req):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        return LLMResponse(
                            content="",
                            tool_calls=[_make_tool_call("tc_ok_1", "tool_a")],
                            stop_reason="tool_use",
                        )
                    return LLMResponse(content="done", stop_reason="end_turn")

                llm = MockLLM(callable=llm_callable)
                tool_executor = MockToolExecutor(callable=lambda tc: ToolResult(
                    tool_call_id=tc.id, content="ok",
                ))

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=tool_executor,
                    ),
                    config=RunnerConfig(session_id="s_ok", max_iterations=3),
                )
                outcome = await runner.run()

                # 正常完成
                assert outcome.status == "completed"

                # 只有 1 条 tool_result（tc_ok_1 的正常结果），无 is_error 补丁
                messages = await store.load_messages("s_ok")
                tool_msgs = [m for m in messages if m.role == "tool"]
                assert len(tool_msgs) == 1
                assert tool_msgs[0].tool_call_id == "tc_ok_1"
                # 正常 tool_result content 不应含 error/dangling
                content_lower = (tool_msgs[0].content or "").lower()
                assert not any(kw in content_lower for kw in
                               ["dangling", "interrupt", "provider_error"]), (
                    f"E5 回归：正常完成时不应补 error tool_result，"
                    f"content={tool_msgs[0].content!r}"
                )

            _run(run())
        finally:
            store.close()
