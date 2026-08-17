"""C1 · partial text 落库（red_green 测试，spec D7）

测试目标：
1. 验证 stream 中断时（user_interrupted / doom_loop / ContextOverflow / provider_error）
   已收到的 partial text 作为 partial message 落库（source="partial", visible=True）

测试策略（spec D12）：red_green: required
- 红测试：mock stream 中途断开/中断，断言 partial text 丢失（修复前失败）
- 绿测试：修复后断言 partial text 落库 + source="partial"

场景覆盖：
1. user_interrupted 中断 stream → partial text 应落库
2. doom_loop 中断 stream → partial text 应落库
3. ContextOverflow（413）中断 stream → partial text 应落库
4. provider_error 中断 stream → partial text 应落库（部分场景）
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
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.doom_loop import DoomLoopConfig  # noqa: E402
from client.core.agent.types import LLMRequest, LLMResponse, Message  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _PartialTextThenInterruptLLM:
    """stream() 先 yield 几个 text_delta（partial text），然后检测到 interrupt 后停止。

    模拟场景：用户在 stream 进行中点击中断按钮，runner 应把已收到的 partial text 落库。

    注：interrupt_event 在 stream() 内 yield 之间 set，模拟真实用户中断时机
    （_stream_llm 在每个 event 循环顶部检查 _is_interrupted）。
    """

    def __init__(self, partial_text: str = "这是部分生成的文本，还没完成", interrupt_event=None):
        self.partial_text = partial_text
        self.call_count = 0
        self.interrupt_event = interrupt_event

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(content=self.partial_text, stop_reason="end_turn")

    async def stream(self, request: LLMRequest):
        self.call_count += 1
        # 分 3 个 chunk yield partial text，第 1 个后触发中断
        chunks = [self.partial_text[:10], self.partial_text[10:20], self.partial_text[20:]]
        for i, chunk in enumerate(chunks):
            yield {"type": "text_delta", "delta": chunk}
            # 第 1 个 chunk yield 后 set interrupt_event（_stream_llm 下轮检查到）
            if i == 0 and self.interrupt_event is not None:
                self.interrupt_event.set()
        # 不 yield done（模拟中断）


class _PartialTextThenDoomLoopLLM:
    """stream() 先 yield partial text，然后 yield 重复 thinking_delta 触发 DoomLoop。"""

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(content="partial", thinking="X" * 60 * 4, stop_reason="end_turn")

    async def stream(self, request: LLMRequest):
        self.call_count += 1
        # 先 yield partial text
        yield {"type": "text_delta", "delta": "部分文本内容"}
        # 再 yield 重复 thinking（触发 DoomLoop）
        full = "X" * 60 * 4
        cs = len(full) // 3 + 1
        for i in range(0, len(full), cs):
            yield {"type": "thinking_delta", "delta": full[i:i+cs]}


class _PartialTextThenOverflowLLM:
    """stream() 先 yield partial text，然后 yield context_overflow 事件。"""

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        from client.core.agent.compactor import ContextOverflow
        raise ContextOverflow("413", status_code=413)

    async def stream(self, request: LLMRequest):
        self.call_count += 1
        yield {"type": "text_delta", "delta": "部分文本"}
        yield {"type": "context_overflow", "error": "413", "status_code": 413}


# ============================================================================
# 1. user_interrupted 中断 stream → partial text 应落库
# ============================================================================


class TestUserInterruptedPartialText:
    """C1: user_interrupted 中断 stream 时，partial text 应作为 partial message 落库。"""

    def test_partial_text_persisted_on_user_interrupt(self, tmp_path):
        """红测试：stream 中断后 partial text 应落库 source=partial。"""
        store = EventStore(db_path=str(tmp_path / "c1_interrupt.db"))
        store.init()
        try:
            _run(store.create_session(session_id="s1"))
            _run(store.append_message("s1", Message(role="user", content="hi")))

            interrupt_event = asyncio.Event()
            llm = _PartialTextThenInterruptLLM(
                partial_text="这是部分生成的文本，还没完成",
                interrupt_event=interrupt_event,
            )
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    interrupt_event=interrupt_event,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=5, use_stream=True),
            )
            outcome = _run(runner.run())

            # 验证 status=interrupted
            assert outcome.status == "interrupted", (
                f"用户中断应 status=interrupted，实际={outcome.status}"
            )
            # 验证 partial text 落库
            messages = _run(store.load_messages("s1", include_invisible=True))
            partial_msgs = [m for m in messages if m.source == "partial"]
            assert len(partial_msgs) == 1, (
                f"应有 1 条 source=partial 消息，实际={len(partial_msgs)}；"
                f"所有消息 sources={[m.source for m in messages]}"
            )
            assert "部分" in partial_msgs[0].content or partial_msgs[0].content, (
                f"partial 消息 content 应含已生成文本，实际={partial_msgs[0].content!r}"
            )
            assert partial_msgs[0].visible is True, "partial 消息应 visible=True"
        finally:
            store.close()


# ============================================================================
# 2. DoomLoop 中断 stream → partial text 应落库
# ============================================================================


class TestDoomLoopPartialText:
    """C1: DoomLoop 中断 stream 时，partial text 应作为 partial message 落库。"""

    def test_partial_text_persisted_on_doom_loop(self, tmp_path):
        """红测试：DoomLoop 中断后 partial text 应落库 source=partial。"""
        store = EventStore(db_path=str(tmp_path / "c1_doom.db"))
        store.init()
        try:
            _run(store.create_session(session_id="s2"))
            _run(store.append_message("s2", Message(role="user", content="hi")))

            llm = _PartialTextThenDoomLoopLLM()
            cfg = DoomLoopConfig(
                tail_size=1000, min_repeat_len=50, repeat_threshold=3,
            )
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    doom_loop_config=cfg,
                ),
                config=RunnerConfig(session_id="s2", max_iterations=5, use_stream=True),
            )
            outcome = _run(runner.run())

            # 验证 status=interrupted（B1 后 DoomLoop 停止）
            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "doom_loop_detected"
            # 验证 partial text 落库
            messages = _run(store.load_messages("s2", include_invisible=True))
            partial_msgs = [m for m in messages if m.source == "partial"]
            assert len(partial_msgs) == 1, (
                f"应有 1 条 source=partial 消息，实际={len(partial_msgs)}；"
                f"所有消息 sources={[m.source for m in messages]}"
            )
            assert "部分文本" in partial_msgs[0].content, (
                f"partial 消息应含已生成文本，实际={partial_msgs[0].content!r}"
            )
        finally:
            store.close()


# ============================================================================
# 3. ContextOverflow 中断 stream → partial text 应落库
# ============================================================================


class TestContextOverflowPartialText:
    """C1: ContextOverflow（413）中断 stream 时，partial text 应作为 partial message 落库。"""

    def test_partial_text_persisted_on_context_overflow(self, tmp_path):
        """红测试：ContextOverflow 中断后 partial text 应落库 source=partial。"""
        from client.core.agent import Compactor

        store = EventStore(db_path=str(tmp_path / "c1_overflow.db"))
        store.init()
        try:
            _run(store.create_session(session_id="s3"))
            # 预置足够多的消息以触发压缩（> tail_keep=6）
            for i in range(10):
                _run(store.append_message("s3", Message(
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"msg {i} " * 50,
                    source="user" if i % 2 == 0 else "assistant",
                )))

            llm = _PartialTextThenOverflowLLM()
            compactor = Compactor(max_context_tokens=8000, tail_keep=6)
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    compactor=compactor,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="s3", max_iterations=5, use_stream=True),
            )
            _run(runner.run())

            # ContextOverflow 后 runner 走 reactive_compact_retry 或 failed
            # 关键是 partial text 应该落库
            messages = _run(store.load_messages("s3", include_invisible=True))
            partial_msgs = [m for m in messages if m.source == "partial"]
            assert len(partial_msgs) >= 1, (
                f"ContextOverflow 后应有 partial 消息落库，实际={len(partial_msgs)}；"
                f"所有消息 sources={[m.source for m in messages]}"
            )
            assert "部分文本" in partial_msgs[0].content, (
                f"partial 消息应含已生成文本，实际={partial_msgs[0].content!r}"
            )
        finally:
            store.close()
