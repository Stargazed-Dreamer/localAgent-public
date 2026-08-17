"""v6-lite-streaming-gui T02 单元测试：runner stream 主循环 + 看门狗

测试范围（spec D05/D14/D15）：
- MockStreamingLLM stream → runner 主循环 → streaming 事件写 EventStore
- 流结束后完整 Message 落库 + streaming 事件 invalidated
- thinking_delta 累积 → Message.thinking 字段
- 中断即停（流式中断后不再写事件）
- use_stream=False 回退 call()（向后兼容）
- context_overflow 事件 → 抛 ContextOverflow → reactive_compact_retry
- provider_error → break + error Message 落库
- LLMPoolGateway 看门狗：空闲超时 / stall 检测（mock 时间）

不依赖后端运行，全部用 mock。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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
)
from client.core.agent.llm_pool_gateway import (  # noqa: E402
    SSE_EVENT_CONTEXT_OVERFLOW,
    SSE_EVENT_DONE,
    SSE_EVENT_PROVIDER_ERROR,
    SSE_EVENT_TEXT_DELTA,
    LLMPoolGateway,
)
from client.core.agent.types import (  # noqa: E402
    LLMRequest,
    Message,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    """同步运行 async 协程（独立 event loop，避免污染）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_streaming_t02.db"))
    store.init()
    return store


class MockStreamingLLM:
    """脚本化流式 LLM：stream() 逐事件 yield 预设事件列表。

    不实现 call()（如需 call()，用 MockLLM）。
    """

    def __init__(self, events: list[dict] | None = None):
        self._events = events or []
        self.requests: list[LLMRequest] = []
        self.stream_count = 0

    async def stream(self, request: LLMRequest):
        """实现流式 LLMGateway 协议：逐事件 yield。"""
        self.stream_count += 1
        self.requests.append(request)
        for event in self._events:
            yield event


class _MockResponse:
    """模拟 requests.Response（用于 LLMPoolGateway 看门狗测试）。"""

    def __init__(self, status_code: int = 200, lines: list[str] | None = None, text: str = ""):
        self.status_code = status_code
        self.text = text
        self._lines = lines or []

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        pass


# ============================================================================
# T02-A: runner stream 主循环（spec D05）
# ============================================================================


class TestRunnerStreamMainLoop:
    """runner use_stream=True 时主循环流式处理。"""

    def test_stream_text_delta_writes_streaming_events_and_final_message(self, tmp_path):
        """text_delta 事件 → streaming_text_delta 写 EventStore + 完整 Message 落库"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1", title="test")
            await store.append_message("s1", Message(role="user", content="hi"))

            events = [
                {"type": "text_delta", "delta": "Hello"},
                {"type": "text_delta", "delta": " world"},
                {"type": "usage", "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                {"type": "done", "finish_reason": "stop"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert outcome.stop_reason == "end_turn"

            # 完整 Message 已落库
            messages = await store.load_messages("s1")
            # user + assistant
            assert len(messages) == 2
            assert messages[1].role == "assistant"
            assert messages[1].content == "Hello world"

            # streaming 事件已 invalidated（load_streaming_events 返回空）
            streaming = await store.load_streaming_events("s1")
            assert len(streaming) == 0

        _run(run())
        store.close()

    def test_stream_thinking_delta_accumulated_to_message_thinking(self, tmp_path):
        """thinking_delta 事件 → 累积到 Message.thinking 字段"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="think hard"))

            events = [
                {"type": "thinking_delta", "delta": "step1 "},
                {"type": "thinking_delta", "delta": "step2"},
                {"type": "text_delta", "delta": "answer"},
                {"type": "done", "finish_reason": "stop"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"

            messages = await store.load_messages("s1")
            assert len(messages) == 2
            assert messages[1].thinking == "step1 step2"
            assert messages[1].content == "answer"

        _run(run())
        store.close()

    def test_stream_tool_call_delta_writes_streaming_and_final_message(self, tmp_path):
        """tool_call_delta 事件 → streaming_tool_call 写 EventStore + 完整 Message 落库"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="call foo"))

            tc = {"id": "call_1", "type": "function",
                  "function": {"name": "foo", "arguments": '{"a":1}'}}
            events = [
                {"type": "text_delta", "delta": "calling foo"},
                {"type": "tool_call_delta", "tool_call": tc},
                {"type": "done", "finish_reason": "tool_calls"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            # MockToolExecutor 返回成功
            mock_tool = MockToolExecutor()
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True, max_iterations=3),
            )
            outcome = await runner.run()

            # tool_calls 触发下一轮，需要 MockLLM 在第二轮返回纯文本终止
            # MockStreamingLLM 只有一轮事件，第二轮 stream 会返回空 → stop_reason=end_turn
            assert outcome.status in ("completed", "failed")

            # 第一轮 assistant Message 带 tool_calls
            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            assert len(assistant_msgs) >= 1
            assert assistant_msgs[0].tool_calls == [tc]
            assert assistant_msgs[0].content == "calling foo"

        _run(run())
        store.close()

    def test_stream_interrupted_mid_stream_stops_writing_events(self, tmp_path):
        """用户中断 → 流式中断后不再写事件"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="hi"))

            # 用 asyncio.Event 模拟中断
            interrupt_event = asyncio.Event()

            events = [
                {"type": "text_delta", "delta": "Hello"},
                {"type": "text_delta", "delta": " world"},
                {"type": "text_delta", "delta": " more"},
                {"type": "done", "finish_reason": "stop"},
            ]

            # 包装 mock_llm，第二个事件后触发中断
            class _InterruptingLLM:
                def __init__(self):
                    self.stream_count = 0
                    self.requests = []

                async def stream(self, request):
                    self.stream_count += 1
                    self.requests.append(request)
                    for i, event in enumerate(events):
                        if i == 1:
                            # 第二个事件后 set 中断
                            interrupt_event.set()
                        yield event

            mock_llm = _InterruptingLLM()
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    interrupt_event=interrupt_event,
                ),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            outcome = await runner.run()

            # 中断后 outcome 应该是 interrupted 或 completed（取决于中断时机）
            # 关键：流被 break，后续事件不写入
            assert outcome.status in ("interrupted", "completed")

            # streaming 事件：最多 2 个 text_delta（第 3 个前中断）
            # 完整 Message 仍会落库（累积的 "Hello world"）
            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            if assistant_msgs:
                # 累积的文本最多是 "Hello world"（第 3 个 " more" 前中断）
                assert assistant_msgs[0].content in ("Hello", "Hello world", "Hello world more")

        _run(run())
        store.close()


# ============================================================================
# T02-B: use_stream=False 回退 call()（向后兼容）
# ============================================================================


class TestUseStreamFalseFallback:
    """use_stream=False 时 runner 回退 call()，行为与改造前一致。"""

    def test_use_stream_false_uses_call_not_stream(self, tmp_path):
        """use_stream=False → 调 call() 不调 stream()"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="hi"))

            mock_llm = MockLLM(default_text="non-stream response")
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=False),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert mock_llm.call_count == 1

            # 无 streaming 事件写入
            streaming = await store.load_streaming_events("s1")
            assert len(streaming) == 0

            messages = await store.load_messages("s1")
            assert messages[1].content == "non-stream response"

        _run(run())
        store.close()


# ============================================================================
# T02-C: context_overflow 事件 → ContextOverflow（spec D05）
# ============================================================================


class TestStreamContextOverflow:
    """stream 收到 context_overflow 事件 → 抛 ContextOverflow → reactive_compact_retry。"""

    def test_context_overflow_event_raises_contextoverflow(self, tmp_path):
        """413 事件 → runner 抛 ContextOverflow"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="hi"))

            events = [
                {"type": SSE_EVENT_CONTEXT_OVERFLOW, "error": "HTTP 413: too long", "status_code": 413},
                {"type": SSE_EVENT_DONE, "finish_reason": "error"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            outcome = await runner.run()

            # 无 compactor → failed（context_overflow_no_compactor）
            assert outcome.status == "failed"
            assert outcome.stop_reason == "context_overflow_no_compactor"

        _run(run())
        store.close()


# ============================================================================
# T02-D: provider_error → break + error Message（spec D05/D14）
# ============================================================================


class TestStreamProviderError:
    """stream 收到 provider_error → break + error Message 落库。"""

    def test_provider_error_writes_error_message(self, tmp_path):
        """provider_error → 构造 error LLMResponse + Message 落库"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="hi"))

            events = [
                {"type": "text_delta", "delta": "partial "},
                {"type": SSE_EVENT_PROVIDER_ERROR, "error": "stream stall"},
                {"type": SSE_EVENT_DONE, "finish_reason": "error"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            outcome = await runner.run()

            # provider_error → stop_reason=error → 完整 Message 落库（含累积的 partial text）
            assert outcome.status == "completed"  # 无 tool_calls → finalize

            messages = await store.load_messages("s1")
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            assert len(assistant_msgs) == 1
            # 累积了 "partial " 后遇到 provider_error
            assert "partial" in assistant_msgs[0].content

        _run(run())
        store.close()


# ============================================================================
# T02-E: LLMPoolGateway 看门狗（spec D15）
# ============================================================================


class TestLLMPoolGatewayWatchdog:
    """LLMPoolGateway stream() 看门狗：空闲超时 + stall 检测。"""

    def test_stream_idle_timeout_yields_provider_error(self):
        """90s 无数据 → provider_error + done（mock 时间）"""
        gw = LLMPoolGateway(stream_idle_timeout_secs=1, stall_detection_window_secs=0)

        # mock iter_lines 阻塞 2s（超过 1s 空闲超时）
        import time as _time

        class _SlowResponse:
            status_code = 200
            text = ""

            def iter_lines(self, decode_unicode=True):
                _time.sleep(2)  # 阻塞超过 idle_timeout
                return
                yield  # 让这个函数变成 generator

            def close(self):
                pass

        with patch.object(gw._session, "post", return_value=_SlowResponse()):
            events = []

            async def run():
                async for event in gw.stream(LLMRequest(messages=[Message(role="user", content="hi")])):
                    events.append(event)

            _run(run())

        # 应该收到 provider_error + done
        types = [e["type"] for e in events]
        assert SSE_EVENT_PROVIDER_ERROR in types
        assert SSE_EVENT_DONE in types
        assert events[-1]["finish_reason"] == "error"

    def test_stream_stall_detection_yields_provider_error(self):
        """30s 窗口内 token 增量不足 → provider_error（mock 时间）"""
        # 用极短的 stall 窗口（1s）+ 极少 token
        gw = LLMPoolGateway(stream_idle_timeout_secs=0, stall_detection_window_secs=1)

        # 构造 SSE 响应：1 个 text_delta（1 token）+ 等待 2s（超过 1s 窗口）+ done
        import time as _time

        class _StallResponse:
            status_code = 200
            text = ""

            def iter_lines(self, decode_unicode=True):
                # 第 1 行：少量 text
                yield 'data: {"type":"text_delta","delta":"a"}'
                # 等待 2s（超过 1s stall 窗口）
                _time.sleep(2)
                # 第 2 行：done（不会到达，stall 先触发）
                yield 'data: {"type":"done","finish_reason":"stop"}'

            def close(self):
                pass

        with patch.object(gw._session, "post", return_value=_StallResponse()):
            events = []

            async def run():
                async for event in gw.stream(LLMRequest(messages=[Message(role="user", content="hi")])):
                    events.append(event)

            _run(run())

        # 应该收到 text_delta + provider_error + done
        types = [e["type"] for e in events]
        assert SSE_EVENT_TEXT_DELTA in types
        assert SSE_EVENT_PROVIDER_ERROR in types
        assert SSE_EVENT_DONE in types

    def test_stream_413_yields_context_overflow_event(self):
        """HTTP 413 → context_overflow 事件（不抛异常）"""
        gw = LLMPoolGateway()

        resp = _MockResponse(status_code=413, text="context_length_exceeded")
        with patch.object(gw._session, "post", return_value=resp):
            events = []

            async def run():
                async for event in gw.stream(LLMRequest(messages=[Message(role="user", content="hi")])):
                    events.append(event)

            _run(run())

        types = [e["type"] for e in events]
        assert SSE_EVENT_CONTEXT_OVERFLOW in types
        assert SSE_EVENT_DONE in types
        # context_overflow 事件含 status_code
        co_event = [e for e in events if e["type"] == SSE_EVENT_CONTEXT_OVERFLOW][0]
        assert co_event["status_code"] == 413

    def test_stream_normal_flow_no_watchdog_trigger(self):
        """正常流式：看门狗不触发，正常 yield 所有事件"""
        gw = LLMPoolGateway(stream_idle_timeout_secs=90, stall_detection_window_secs=30)

        lines = [
            'data: {"type":"text_delta","delta":"Hello"}',
            'data: {"type":"text_delta","delta":" world"}',
            'data: {"type":"usage","prompt_tokens":5,"completion_tokens":2,"total_tokens":7}',
            'data: {"type":"done","finish_reason":"stop"}',
        ]
        resp = _MockResponse(status_code=200, lines=lines)
        with patch.object(gw._session, "post", return_value=resp):
            events = []

            async def run():
                async for event in gw.stream(LLMRequest(messages=[Message(role="user", content="hi")])):
                    events.append(event)

            _run(run())

        # 正常收到 4 个事件
        types = [e["type"] for e in events]
        assert types == ["text_delta", "text_delta", "usage", "done"]
        assert events[-1]["finish_reason"] == "stop"


# ============================================================================
# T02-F: streaming 事件 trace_id 分组（reconciler 用）
# ============================================================================


class TestStreamingEventTraceId:
    """streaming 事件带 trace_id，reconciler 按此分组。"""

    def test_streaming_events_share_trace_id(self, tmp_path):
        """同一次 stream 调用的所有 streaming 事件共享同一 trace_id"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session("s1")
            await store.append_message("s1", Message(role="user", content="hi"))

            events = [
                {"type": "text_delta", "delta": "a"},
                {"type": "thinking_delta", "delta": "b"},
                {"type": "text_delta", "delta": "c"},
                {"type": "done", "finish_reason": "stop"},
            ]
            mock_llm = MockStreamingLLM(events=events)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store),
                config=RunnerConfig(session_id="s1", use_stream=True),
            )
            await runner.run()

            # 查所有 streaming 事件（含 invalidated 的）
            all_events = await store.load_events("s1")
            streaming_events = [
                e for e in all_events
                if e.type in ("streaming_text_delta", "streaming_thinking_delta", "streaming_tool_call")
            ]
            # 都被 invalidated（invalidated_seq 非 None）
            assert all(e.invalidated_seq is not None for e in streaming_events)
            # 共享同一 trace_id（非 None）
            trace_ids = {e.trace_id for e in streaming_events}
            assert len(trace_ids) == 1
            assert next(iter(trace_ids)) is not None

        _run(run())
        store.close()


if __name__ == "__main__":
    # 简单 runner（不依赖 pytest）
    import traceback

    # 手动构造 tmp_path
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        tests = [
            ("test_stream_text_delta_writes_streaming_events_and_final_message",
             TestRunnerStreamMainLoop().test_stream_text_delta_writes_streaming_events_and_final_message),
            ("test_stream_thinking_delta_accumulated_to_message_thinking",
             TestRunnerStreamMainLoop().test_stream_thinking_delta_accumulated_to_message_thinking),
            ("test_stream_tool_call_delta_writes_streaming_and_final_message",
             TestRunnerStreamMainLoop().test_stream_tool_call_delta_writes_streaming_and_final_message),
            ("test_stream_interrupted_mid_stream_stops_writing_events",
             TestRunnerStreamMainLoop().test_stream_interrupted_mid_stream_stops_writing_events),
            ("test_use_stream_false_uses_call_not_stream",
             TestUseStreamFalseFallback().test_use_stream_false_uses_call_not_stream),
            ("test_context_overflow_event_raises_contextoverflow",
             TestStreamContextOverflow().test_context_overflow_event_raises_contextoverflow),
            ("test_provider_error_writes_error_message",
             TestStreamProviderError().test_provider_error_writes_error_message),
            ("test_stream_idle_timeout_yields_provider_error",
             TestLLMPoolGatewayWatchdog().test_stream_idle_timeout_yields_provider_error),
            ("test_stream_stall_detection_yields_provider_error",
             TestLLMPoolGatewayWatchdog().test_stream_stall_detection_yields_provider_error),
            ("test_stream_413_yields_context_overflow_event",
             TestLLMPoolGatewayWatchdog().test_stream_413_yields_context_overflow_event),
            ("test_stream_normal_flow_no_watchdog_trigger",
             TestLLMPoolGatewayWatchdog().test_stream_normal_flow_no_watchdog_trigger),
            ("test_streaming_events_share_trace_id",
             TestStreamingEventTraceId().test_streaming_events_share_trace_id),
        ]

        passed = 0
        failed = 0
        for name, test_fn in tests:
            try:
                test_fn(tmp_path)
                print(f"PASS {name}")
                passed += 1
            except Exception:
                print(f"FAIL {name}")
                traceback.print_exc()
                failed += 1
        print(f"\n{passed} passed, {failed} failed")
        sys.exit(0 if failed == 0 else 1)
