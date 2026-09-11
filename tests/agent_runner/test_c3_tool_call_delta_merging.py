"""C3 · streaming tool_call delta 合并（spec D7 数据完整性）

测试范围：
- OpenAI 分片格式 tool_call_delta（有 index 字段）→ 按 index 合并
- server 已合并格式 tool_call_delta（无 index 字段）→ 直接 append（行为不变）
- 集成测试：runner._stream_llm 收到分片 → assistant 消息 tool_calls 合并正确

设计依据：
- spec D7：数据完整性，client 端不信任上游，防御性合并
- server 端 pool.stream() 已用 ToolCallAccumulator 合并，但 client 端再合并一次
- 防止 server 端逻辑变更 / 接入其他 provider 直接透传 OpenAI 分片

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
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.types import (  # noqa: E402
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
    store = EventStore(db_path=str(tmp_path / "test_c3.db"))
    store.init()
    return store


class _MockStreamingLLM:
    """脚本化流式 LLM：stream() 逐事件 yield 预设事件列表。"""

    def __init__(self, events: list[dict] | None = None):
        self._events = events or []
        self.requests = []
        self.stream_count = 0

    async def stream(self, request):
        self.stream_count += 1
        self.requests.append(request)
        for event in self._events:
            yield event


# ============================================================================
# C3-A: OpenAI 分片格式合并（有 index 字段）
# ============================================================================


class TestOpenAIDeltaFragmentsMerged:
    """C3: OpenAI 分片格式 tool_call_delta（有 index）→ 按 index 合并。"""

    def test_two_fragments_same_index_merged_into_one_tool_call(self, tmp_path):
        """同 index 的两个分片 → 合并为一个完整 tool_call。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call foo"))

            events = [
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "foo", "arguments": "{\"a\":"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "1}"},
                }},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = _MockStreamingLLM(events=events)
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            await runner.run()

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            return assistant_msgs[0].tool_calls

        tool_calls = _run(run())
        store.close()

        # 修复前：2 个 tool_call（第一个有 id，第二个无 id 分片）
        # 修复后：1 个 tool_call（同 index 合并，arguments 拼接）
        assert len(tool_calls) == 1, (
            f"Expected 1 merged tool_call, got {len(tool_calls)}: {tool_calls}"
        )
        tc0 = tool_calls[0]
        assert tc0["id"] == "call_1"
        assert tc0["function"]["name"] == "foo"
        assert tc0["function"]["arguments"] == '{"a":1}'

    def test_three_fragments_two_indices_merge_into_two_tool_calls(self, tmp_path):
        """3 个分片（index 0 两个 + index 1 一个）→ 合并为 2 个 tool_call。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call foo and bar"))

            events = [
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "foo", "arguments": "{\"a\":"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "1}"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 1, "id": "call_2", "type": "function",
                    "function": {"name": "bar", "arguments": "{}"},
                }},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = _MockStreamingLLM(events=events)
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            await runner.run()

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            return assistant_msgs[0].tool_calls

        tool_calls = _run(run())
        store.close()

        # 修复前：3 个 tool_call（含无效分片）
        # 修复后：2 个 tool_call（index 0 合并，index 1 完整）
        assert len(tool_calls) == 2, (
            f"Expected 2 merged tool_calls, got {len(tool_calls)}: {tool_calls}"
        )
        tc0 = tool_calls[0]
        assert tc0["id"] == "call_1"
        assert tc0["function"]["name"] == "foo"
        assert tc0["function"]["arguments"] == '{"a":1}'
        tc1 = tool_calls[1]
        assert tc1["id"] == "call_2"
        assert tc1["function"]["name"] == "bar"
        assert tc1["function"]["arguments"] == '{}'

    def test_arguments_split_across_many_fragments_merged_correctly(self, tmp_path):
        """arguments 拆成 4 个分片 → 拼接为完整 JSON。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call foo"))

            events = [
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "call_x", "type": "function",
                    "function": {"name": "foo", "arguments": ""},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "{\"key"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "\":\"val"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "ue\"}"},
                }},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = _MockStreamingLLM(events=events)
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            await runner.run()

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            return assistant_msgs[0].tool_calls

        tool_calls = _run(run())
        store.close()

        assert len(tool_calls) == 1, (
            f"Expected 1 merged tool_call, got {len(tool_calls)}: {tool_calls}"
        )
        assert tool_calls[0]["function"]["arguments"] == '{"key":"value"}'


# ============================================================================
# C3-B: server 已合并格式（无 index 字段）行为不变
# ============================================================================


class TestServerMergedFormatUnchanged:
    """C3: server 已合并格式（无 index）→ 直接 append，行为不变。"""

    def test_server_merged_tool_call_appended_as_is(self, tmp_path):
        """server 已合并的完整 tool_call（无 index）→ 直接 append，不合并。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call foo"))

            tc1 = {"id": "call_1", "type": "function",
                   "function": {"name": "foo", "arguments": '{"a":1}'}}
            tc2 = {"id": "call_2", "type": "function",
                   "function": {"name": "bar", "arguments": '{}'}}
            events = [
                {"type": "tool_call_delta", "tool_call": tc1},
                {"type": "tool_call_delta", "tool_call": tc2},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = _MockStreamingLLM(events=events)
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            await runner.run()

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            return assistant_msgs[0].tool_calls

        tool_calls = _run(run())
        store.close()

        # 两个完整 tool_call，不合并
        assert len(tool_calls) == 2
        assert tool_calls[0] == {"id": "call_1", "type": "function",
                                  "function": {"name": "foo", "arguments": '{"a":1}'}}
        assert tool_calls[1] == {"id": "call_2", "type": "function",
                                  "function": {"name": "bar", "arguments": '{}'}}

    def test_mixed_server_merged_and_openai_fragments(self, tmp_path):
        """混合：server 已合并（无 index）+ OpenAI 分片（有 index）→ 各自正确处理。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call tools"))

            events = [
                # server 已合并的完整 tool_call（无 index）
                {"type": "tool_call_delta", "tool_call": {
                    "id": "call_a", "type": "function",
                    "function": {"name": "alpha", "arguments": '{"x":1}'},
                }},
                # OpenAI 分片格式（有 index）
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "call_b", "type": "function",
                    "function": {"name": "beta", "arguments": "{\"y\":"},
                }},
                {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "function": {"arguments": "2}"},
                }},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = _MockStreamingLLM(events=events)
            mock_tool = MockToolExecutor(default_result="ok")
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            await runner.run()

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            return assistant_msgs[0].tool_calls

        tool_calls = _run(run())
        store.close()

        # 期望：2 个 tool_call（call_a 完整 + call_b 合并）
        assert len(tool_calls) == 2, (
            f"Expected 2 tool_calls (1 complete + 1 merged), got {len(tool_calls)}: {tool_calls}"
        )
        assert tool_calls[0]["id"] == "call_a"
        assert tool_calls[0]["function"]["name"] == "alpha"
        assert tool_calls[1]["id"] == "call_b"
        assert tool_calls[1]["function"]["name"] == "beta"
        assert tool_calls[1]["function"]["arguments"] == '{"y":2}'
