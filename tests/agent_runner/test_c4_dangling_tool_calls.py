"""C4 · dangling tool_calls 循环内检查（spec D7 数据完整性）

测试范围：
- 上一轮 LLM 返回 tool_calls，部分执行后中断/崩溃 → 下一轮 _build_request 前
  扫描 dangling tool_calls（assistant 带 tool_calls 但缺 tool_result）→ 补 is_error=true
- 防止 provider 400（OpenAI 协议要求每个 tool_call 必须配一条 role=tool 消息）
- 与 E5（yieldMissingToolResultBlocks 三处全覆盖）配合，C4 是循环内检查，E5 是启动恢复

设计依据：
- spec D7：数据完整性，_build_request 前确保 messages 配对完整
- v6-02 §2.3 BuildConversationRequest：repair dangling tool calls
- A1 中断后 tool_calls 部分执行，未执行的不补 result（C4 在下轮补）

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
)
from client.core.agent.types import (  # noqa: E402
    Message,
    ToolResult,
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
    store = EventStore(db_path=str(tmp_path / "test_c4.db"))
    store.init()
    return store


async def _make_dangling_session(store: EventStore, session_id: str) -> None:
    """构造 dangling 状态：user + assistant(tool_calls=[tc1, tc2]) + tool(tc1)（缺 tc2）。"""
    await store.create_session(session_id=session_id)
    await store.append_message(session_id, Message(
        role="user", content="call foo and bar", source="user",
    ))
    # assistant 携带 2 个 tool_calls
    assistant_msg = Message(
        role="assistant",
        content="calling foo and bar",
        source="assistant",
        tool_calls=[
            {"id": "tc1", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}},
            {"id": "tc2", "type": "function",
             "function": {"name": "bar", "arguments": "{}"}},
        ],
    )
    await store.append_message(session_id, assistant_msg)
    # 只补 tc1 的 tool_result（模拟 tc2 中断未执行）
    await store.append_tool_result_message(
        session_id,
        ToolResult(tool_call_id="tc1", content="foo result", is_error=False),
    )


def _collect_request_messages(request) -> list[Message]:
    """从 LLMRequest 提取 messages 列表（便于断言配对）。"""
    return list(request.messages)


# ============================================================================
# C4-A: dangling tool_calls 补 is_error=true tool_result
# ============================================================================


class TestDanglingToolCallsRepaired:
    """C4: _build_request 前扫描 dangling tool_calls → 补 is_error=true tool_result。"""

    def test_dangling_tool_call_gets_error_result_before_llm_request(self, tmp_path):
        """缺 tc2 result → C4 在 _build_request 前补 is_error=true tool_result。"""
        store = _new_store(tmp_path)

        async def run():
            await _make_dangling_session(store, "s1")

            # MockLLM 第一轮返回 end_turn（不调工具，直接完成）
            mock_llm = MockLLM(default_text="done")
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=2),
            )
            await runner.run()

            # 检查 LLM 收到的 request.messages
            assert mock_llm.call_count >= 1
            request = mock_llm.requests[0]
            return _collect_request_messages(request)

        msgs = _run(run())
        store.close()

        # 找 assistant 消息（带 tool_calls）
        assistant_msgs = [m for m in msgs if m.role == "assistant" and m.tool_calls]
        assert len(assistant_msgs) >= 1
        assistant = assistant_msgs[-1]
        tc_ids = {tc["id"] for tc in assistant.tool_calls}
        assert tc_ids == {"tc1", "tc2"}

        # 找 tool 消息（tool_result）
        tool_msgs = [m for m in msgs if m.role == "tool"]
        tool_result_ids = {m.tool_call_id for m in tool_msgs}

        # 修复前：只有 tc1 的 tool_result（tc2 dangling）
        # 修复后：C4 补了 tc2 的 is_error=true tool_result
        assert "tc1" in tool_result_ids, f"tc1 should have tool_result, got {tool_result_ids}"
        assert "tc2" in tool_result_ids, (
            f"C4 should repair dangling tc2 with is_error tool_result, "
            f"got tool_result_ids={tool_result_ids}"
        )

        # tc2 的 tool_result 应该是 is_error=true（C4 补的是 error result）
        tc2_msg = next(m for m in tool_msgs if m.tool_call_id == "tc2")
        # tool_result 消息的 content 应含 error / interrupted 提示
        assert "interrupt" in tc2_msg.content.lower() or "error" in tc2_msg.content.lower() or "dangling" in tc2_msg.content.lower(), (
            f"C4 repaired tool_result should indicate error/interrupted, "
            f"got content={tc2_msg.content!r}"
        )

    def test_no_dangling_no_repair_needed(self, tmp_path):
        """无 dangling（所有 tool_call 都有 result）→ C4 不补任何 result。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s_ok")
            await store.append_message("s_ok", Message(
                role="user", content="call foo", source="user",
            ))
            # assistant 带 1 个 tool_call，tool_result 已补 → 无 dangling
            await store.append_message("s_ok", Message(
                role="assistant",
                content="calling foo",
                source="assistant",
                tool_calls=[{"id": "tc_ok", "type": "function",
                             "function": {"name": "foo", "arguments": "{}"}}],
            ))
            await store.append_tool_result_message(
                "s_ok",
                ToolResult(tool_call_id="tc_ok", content="foo result", is_error=False),
            )

            mock_llm = MockLLM(default_text="done")
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s_ok", max_iterations=2),
            )
            await runner.run()

            assert mock_llm.call_count >= 1
            request = mock_llm.requests[0]
            return _collect_request_messages(request)

        msgs = _run(run())
        store.close()

        # 只有 1 个 tool_result（tc_ok），C4 没补新的
        tool_msgs = [m for m in msgs if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "tc_ok"

    def test_multiple_dangling_all_repaired(self, tmp_path):
        """多个 dangling tool_calls（tc2 + tc3 缺 result）→ C4 全补。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s_multi")
            await store.append_message("s_multi", Message(
                role="user", content="call foo bar baz", source="user",
            ))
            await store.append_message("s_multi", Message(
                role="assistant",
                content="calling 3 tools",
                source="assistant",
                tool_calls=[
                    {"id": "tc1", "type": "function",
                     "function": {"name": "foo", "arguments": "{}"}},
                    {"id": "tc2", "type": "function",
                     "function": {"name": "bar", "arguments": "{}"}},
                    {"id": "tc3", "type": "function",
                     "function": {"name": "baz", "arguments": "{}"}},
                ],
            ))
            # 只补 tc1（tc2 + tc3 dangling）
            await store.append_tool_result_message(
                "s_multi",
                ToolResult(tool_call_id="tc1", content="foo result", is_error=False),
            )

            mock_llm = MockLLM(default_text="done")
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s_multi", max_iterations=2),
            )
            await runner.run()

            assert mock_llm.call_count >= 1
            request = mock_llm.requests[0]
            return _collect_request_messages(request)

        msgs = _run(run())
        store.close()

        tool_msgs = [m for m in msgs if m.role == "tool"]
        tool_result_ids = {m.tool_call_id for m in tool_msgs}
        # 修复后：tc1 + tc2 + tc3 都有 tool_result
        assert tool_result_ids == {"tc1", "tc2", "tc3"}, (
            f"All 3 tool_calls should have tool_result, got {tool_result_ids}"
        )
