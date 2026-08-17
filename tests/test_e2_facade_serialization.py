"""E2 · 验证 facade.py 串行化（spec D22）。

D22：不实现 RunCoordinator，验证 facade.py 串行化足够；不足则补 asyncio.Lock。

调查结论：
- facade.py 无 Lock/queue，依赖 QThread + UI 禁用（chat.py 的 _on_start_page_send
  检查 worker.isRunning() 后 return）
- 但 _send_in_send_mode 故意不等待旧 worker 结束就 start 新 worker，存在
  短暂并发窗口
- 同一 facade 实例上并发调 start() 会导致 _runner 被 overwrite，第一个 run
  变成孤儿 task（无人管理）

修复：facade.start() 加 asyncio.Lock，同 facade 并发 start → 第二次排队等待
第一次完成（不拒绝，保证用户消息不丢）。

测试策略（red_green，spec 标注）：
- 红测试：同 facade 连续 start 两次，断言并发跑（修复前 _runner 被 overwrite）
- 绿测试：修复后断言第二次排队（第一次完成后才执行第二次）
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
from client.core.agent.facade import SessionFacade  # noqa: E402
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    """同步运行 async 协程。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_e2.db"))
    store.init()
    return store


class _ControllableClock:
    """可控时钟。"""

    def __init__(self, start: float = 1000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, secs: float) -> None:
        self._t += secs


class _SlowMockLLM:
    """慢速 Mock LLM，stream 可控延迟，用于测试并发 start。

    通过 events 控制 stream 的进度：
    - start_event: set 后 stream 开始 yield
    - release_event: set 后 stream yield done（完成）
    """

    def __init__(self):
        self.call_count = 0
        self.start_event = asyncio.Event()
        self.release_event = asyncio.Event()

    async def call(self, request):
        self.call_count += 1
        self.start_event.set()
        # 等待 release 信号才返回
        await self.release_event.wait()
        return LLMResponse(content="done", stop_reason="end_turn")

    async def stream(self, request):
        self.call_count += 1
        self.start_event.set()
        yield {"type": "text_delta", "delta": "thinking"}
        # 等待 release 信号才完成
        await self.release_event.wait()
        yield {"type": "done", "finish_reason": "stop"}


# ============================================================================
# Part 1: 同 facade 实例并发 start → 应串行化
# ============================================================================


class TestFacadeConcurrentStartSerialized:
    """E2: 同 facade 实例并发 start() → 第二次排队等待第一次完成。"""

    def test_concurrent_start_serialized(self, tmp_path):
        """同 facade 连续 start 两次 → 第二次排队，不并发执行。

        红测试（修复前）：两次 start 并发执行，_runner 被 overwrite，
        第一次 run 变成孤儿 task。
        绿测试（修复后）：asyncio.Lock 串行化，第二次等第一次完成后才执行。
        """
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s1")
                await store.append_message(
                    "s1", Message(role="user", content="hi", source="user")
                )
                await store.create_session(session_id="s2")
                await store.append_message(
                    "s2", Message(role="user", content="hi2", source="user")
                )

                llm = _SlowMockLLM()
                clock = _ControllableClock(start=1000.0)

                config1 = RunnerConfig(
                    session_id="s1", max_iterations=1, use_stream=False
                )
                # 同 facade 实例，第二次 start 用不同 session_id
                # 但 facade 是单次使用的（生命周期：一个 facade 对应一次 run()）
                # 所以这里测试的是：如果误用同一 facade 调两次 start，应串行化
                facade = SessionFacade(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        clock=clock,
                    ),
                    config=config1,
                )

                # 启动第一个 start（不 await，让它在后台跑）
                task1 = asyncio.ensure_future(
                    facade.start("s1", "first message")
                )
                # 等 LLM 被调用
                await asyncio.wait_for(llm.start_event.wait(), timeout=2.0)
                # 此时第一个 run 正在进行（LLM 在等 release_event）

                # 启动第二个 start（同一 facade）
                task2 = asyncio.ensure_future(
                    facade.start("s2", "second message")
                )

                # 给一点时间让 task2 有机会运行
                await asyncio.sleep(0.1)

                # 修复前：task2 也调了 LLM（call_count=2），两个 run 并发
                # 修复后：task2 被 Lock 阻塞，LLM 只被调一次（call_count=1）
                assert llm.call_count == 1, (
                    f"E2: 同 facade 并发 start 应串行化，"
                    f"但 LLM 被调了 {llm.call_count} 次（期望 1）"
                )

                # 释放第一个 run
                llm.release_event.set()
                outcome1 = await asyncio.wait_for(task1, timeout=2.0)
                assert outcome1.status == "completed"

                # 第二个 run 现在应该开始（Lock 释放后）
                # 重置 start_event 以检测第二次 LLM 调用
                llm.start_event.clear()
                await asyncio.wait_for(llm.start_event.wait(), timeout=2.0)
                assert llm.call_count == 2, (
                    f"E2: 第一个 run 完成后第二个应开始，"
                    f"但 LLM call_count={llm.call_count}（期望 2）"
                )

                # 释放第二个 run
                llm.release_event.set()
                outcome2 = await asyncio.wait_for(task2, timeout=2.0)
                assert outcome2.status == "completed"

            _run(_run_async())
        finally:
            store.close()

    def test_sequential_start_no_blocking(self, tmp_path):
        """串行 start（第一次完成后再调第二次）→ 不阻塞，正常完成。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_seq1")
                await store.append_message(
                    "s_seq1", Message(role="user", content="hi", source="user")
                )
                await store.create_session(session_id="s_seq2")
                await store.append_message(
                    "s_seq2", Message(role="user", content="hi2", source="user")
                )

                # 使用快速 LLM（不延迟）
                class FastLLM:
                    def __init__(self):
                        self.call_count = 0

                    async def call(self, request):
                        self.call_count += 1
                        return LLMResponse(content="done", stop_reason="end_turn")

                    async def stream(self, request):
                        self.call_count += 1
                        yield {"type": "text_delta", "delta": "done"}
                        yield {"type": "done", "finish_reason": "stop"}

                llm = FastLLM()
                clock = _ControllableClock(start=1000.0)

                config1 = RunnerConfig(
                    session_id="s_seq1", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        clock=clock,
                    ),
                    config=config1,
                )

                # 第一次 start
                outcome1 = await facade.start("s_seq1", "first")
                assert outcome1.status == "completed"

                # 第二次 start（同一 facade，但第一次已完成）
                # 注：facade 是单次使用的，第二次 start 会 overwrite _runner
                # 但 Lock 不应阻塞（第一次已释放 Lock）
                outcome2 = await facade.start("s_seq2", "second")
                assert outcome2.status == "completed"

                assert llm.call_count == 2

            _run(_run_async())
        finally:
            store.close()


# ============================================================================
# Part 2: steer/interrupt 与 start 不冲突（event loop 串行化）
# ============================================================================


class TestSteerInterruptSafeWithStart:
    """E2: steer/interrupt 与 start 在同一 event loop 上，天然串行化。"""

    def test_steer_during_start_no_corruption(self, tmp_path):
        """start() 运行中调 steer() → 不破坏数据（event loop 串行化）。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_steer")
                await store.append_message(
                    "s_steer",
                    Message(role="user", content="hi", source="user"),
                )

                llm = _SlowMockLLM()
                clock = _ControllableClock(start=1000.0)

                config = RunnerConfig(
                    session_id="s_steer", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        clock=clock,
                    ),
                    config=config,
                )

                # 启动 start（后台）
                task = asyncio.ensure_future(
                    facade.start("s_steer", "original message")
                )
                await asyncio.wait_for(llm.start_event.wait(), timeout=2.0)

                # start 运行中调 steer（同一 event loop，会排队）
                # steer 是 async 方法，会在 start 的 await 点间隙执行
                await facade.steer("s_steer", "steer instruction")

                # 释放 start
                llm.release_event.set()
                outcome = await asyncio.wait_for(task, timeout=2.0)
                assert outcome.status == "completed"

                # steer 消息应已写入 EventStore
                msgs = await store.load_messages("s_steer")
                steer_msgs = [m for m in msgs if m.source == "steer"]
                assert len(steer_msgs) == 1
                assert "steer instruction" in steer_msgs[0].content

            _run(_run_async())
        finally:
            store.close()

    def test_interrupt_during_start_safe(self, tmp_path):
        """start() 运行中调 interrupt() → 安全中断（event loop 串行化）。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_interrupt")
                await store.append_message(
                    "s_interrupt",
                    Message(role="user", content="hi", source="user"),
                )

                llm = _SlowMockLLM()
                clock = _ControllableClock(start=1000.0)

                config = RunnerConfig(
                    session_id="s_interrupt", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        clock=clock,
                        interrupt_event=asyncio.Event(),
                    ),
                    config=config,
                )

                # 启动 start（后台）
                task = asyncio.ensure_future(
                    facade.start("s_interrupt", "original message")
                )
                await asyncio.wait_for(llm.start_event.wait(), timeout=2.0)

                # start 运行中调 interrupt
                await facade.interrupt("s_interrupt")

                # 释放 LLM（让 runner 继续，但 interrupt_event 已 set）
                llm.release_event.set()
                outcome = await asyncio.wait_for(task, timeout=2.0)

                # interrupt 后 status 应为 interrupted（或 completed，取决于时序）
                # 至少不应 crash
                assert outcome.status in ("interrupted", "completed")

            _run(_run_async())
        finally:
            store.close()
