"""C5 · steer 投递配对保护（spec D7 数据完整性）

测试范围：
- 工具批次执行中 steer 消息到达 → 写入 messages 表后，steer 出现在两个 tool_result 之间
- _build_request 前需规范化消息顺序：assistant(tool_calls) 后的 tool_result 必须连续
- steer 消息应移到该批次所有 tool_result 之后

设计依据：
- spec D7：数据完整性，steer 不破坏 assistant(tool_calls) → tool(result) 配对
- OpenAI 协议：assistant 携带 tool_calls 时，后续必须紧跟 role=tool 消息（每 tool_call 一条），
  中间不能插入其他 role 的消息，否则 provider 返回 400
- steer 语义（D13）：source="steer" 标记引导消息，不打断当前工具执行，下一轮读到

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
    store = EventStore(db_path=str(tmp_path / "test_c5.db"))
    store.init()
    return store


async def _make_steering_between_tools_session(store: EventStore, session_id: str) -> None:
    """构造 steer 在两个 tool_result 之间的状态。

    消息顺序：
    - user: "call foo and bar"
    - assistant: tool_calls=[tc1, tc2]
    - tool: tc1 result  ← tc1 先执行完
    - user(steer): "请额外查询 X"  ← steer 在 tc1 和 tc2 之间到达
    - tool: tc2 result  ← tc2 后执行完

    这是 steer 在工具批次执行中到达时的真实落库顺序。
    修复前：_build_request 直接用这个顺序 → OpenAI 400
    修复后：_build_request 规范化顺序 → tool_result 连续，steer 移到 tool_result 之后
    """
    await store.create_session(session_id=session_id)
    await store.append_message(session_id, Message(
        role="user", content="call foo and bar", source="user",
    ))
    await store.append_message(session_id, Message(
        role="assistant",
        content="calling foo and bar",
        source="assistant",
        tool_calls=[
            {"id": "tc1", "type": "function",
             "function": {"name": "foo", "arguments": "{}"}},
            {"id": "tc2", "type": "function",
             "function": {"name": "bar", "arguments": "{}"}},
        ],
    ))
    # tc1 result
    await store.append_tool_result_message(
        session_id,
        ToolResult(tool_call_id="tc1", content="foo result", is_error=False),
    )
    # steer 消息（在 tc1 和 tc2 之间到达，写入 messages 表）
    await store.append_message(session_id, Message(
        role="user", content="请额外查询 X", source="steer", visible=True,
    ))
    # tc2 result
    await store.append_tool_result_message(
        session_id,
        ToolResult(tool_call_id="tc2", content="bar result", is_error=False),
    )


def _collect_request_messages(request) -> list[Message]:
    """从 LLMRequest 提取 messages 列表。"""
    return list(request.messages)


# ============================================================================
# C5: steer 在 tool_result 之间 → 规范化顺序
# ============================================================================


class TestSteerBetweenToolResultsNormalized:
    """C5: steer 在两个 tool_result 之间 → _build_request 规范化顺序。"""

    def test_steer_moved_after_all_tool_results(self, tmp_path):
        """steer 在 tc1/tc2 result 之间 → 规范化后 steer 在两个 tool_result 之后。"""
        store = _new_store(tmp_path)

        async def run():
            await _make_steering_between_tools_session(store, "s1")

            # MockLLM 第一轮返回 end_turn
            mock_llm = MockLLM(default_text="done")
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=2),
            )
            await runner.run()

            assert mock_llm.call_count >= 1
            request = mock_llm.requests[0]
            return _collect_request_messages(request)

        msgs = _run(run())
        store.close()

        # 找 assistant 消息（带 tool_calls）的位置
        assistant_idx = None
        for i, m in enumerate(msgs):
            if m.role == "assistant" and m.tool_calls:
                assistant_idx = i
                break
        assert assistant_idx is not None, "Should have assistant message with tool_calls"

        # assistant 之后的消息应该是 tool_result（连续，不插入 steer）
        after_assistant = msgs[assistant_idx + 1:]
        # 前两条必须是 role=tool
        assert after_assistant[0].role == "tool", (
            f"C5: After assistant(tool_calls), first message should be tool, "
            f"got {after_assistant[0].role} (source={after_assistant[0].source})"
        )
        assert after_assistant[1].role == "tool", (
            f"C5: After assistant(tool_calls), second message should be tool, "
            f"got {after_assistant[1].role} (source={after_assistant[1].source}) "
            f"— steer may have broken tool_result contiguity"
        )

        # steer 消息应该在两个 tool_result 之后
        tool_result_ids = [m.tool_call_id for m in after_assistant[:2]]
        assert set(tool_result_ids) == {"tc1", "tc2"}, (
            f"C5: Both tc1 and tc2 results should be contiguous after assistant, "
            f"got {tool_result_ids}"
        )

        # steer 在 tool_result 之后
        if len(after_assistant) > 2:
            steer_msg = after_assistant[2]
            assert steer_msg.role == "user" and steer_msg.source == "steer", (
                f"C5: steer should be after all tool_results, "
                f"got role={steer_msg.role} source={steer_msg.source}"
            )

    def test_no_steer_no_reordering(self, tmp_path):
        """无 steer 时消息顺序不变（行为不变）。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s_ok")
            await store.append_message("s_ok", Message(
                role="user", content="call foo", source="user",
            ))
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

        # 顺序：user → assistant(tool_calls) → tool(result)
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant" and msgs[1].tool_calls
        assert msgs[2].role == "tool"
        assert msgs[2].tool_call_id == "tc_ok"

    def test_steer_after_complete_tool_batch_unchanged(self, tmp_path):
        """steer 在工具批次完成后到达 → 顺序天然正确，不需要规范化。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s_after")
            await store.append_message("s_after", Message(
                role="user", content="call foo", source="user",
            ))
            await store.append_message("s_after", Message(
                role="assistant",
                content="calling foo",
                source="assistant",
                tool_calls=[{"id": "tc1", "type": "function",
                             "function": {"name": "foo", "arguments": "{}"}}],
            ))
            await store.append_tool_result_message(
                "s_after",
                ToolResult(tool_call_id="tc1", content="foo result", is_error=False),
            )
            # steer 在 tool_result 之后到达 → 顺序天然正确
            await store.append_message("s_after", Message(
                role="user", content="请额外查询 X", source="steer", visible=True,
            ))

            mock_llm = MockLLM(default_text="done")
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s_after", max_iterations=2),
            )
            await runner.run()

            assert mock_llm.call_count >= 1
            request = mock_llm.requests[0]
            return _collect_request_messages(request)

        msgs = _run(run())
        store.close()

        # 顺序：user → assistant(tool_calls) → tool(result) → user(steer)
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant" and msgs[1].tool_calls
        assert msgs[2].role == "tool"
        assert msgs[3].role == "user" and msgs[3].source == "steer"
