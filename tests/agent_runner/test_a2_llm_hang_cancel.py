"""A2 · LLM hang 时 task cancel（red_green 测试）

spec D9 核心安全约束：用户 interrupt 后，LLM 调用也必须立即阻断。
A1 处理工具循环中的 interrupt，A2 处理 LLM hang 时的 interrupt。

A2 修复点：
1. facade.py `interrupt()` 调 `self._runner_task.cancel()`（asyncio.Task.cancel）
2. facade.py `start()` 保存 `self._runner_task = asyncio.current_task()`
3. runner.py `_run_loop()` 捕获 `asyncio.CancelledError` → 写 transition(user_interrupted)
   + 返回 RunOutcome(status="interrupted")

红测试（修复前失败）：
- MockLLM.call() asyncio.sleep(999) 模拟 hang
- facade.interrupt() 后断言 5 秒内 task done + RunOutcome(interrupted)
- 修复前：只 set event，runner 卡在 LLM await 点不会检查 event → 5 秒超时

绿测试（修复后通过）：
- 同样场景，5 秒内 task done + RunOutcome(interrupted)
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
    RunnerConfig,
    RunnerDeps,
    SessionFacade,
)
from client.core.agent.types import (  # noqa: E402
    TRANSITION_USER_INTERRUPTED,
    LLMRequest,
    LLMResponse,
)


def _make_store(tmp_path) -> EventStore:
    db_path = str(tmp_path / "test_a2.db")
    store = EventStore(db_path)
    store.init()
    return store


class HangLLM:
    """模拟 LLM hang：call() 永远不返回（asyncio.sleep 999s）。"""

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        await asyncio.sleep(999)  # hang
        return LLMResponse(content="never reached")

    async def stream(self, request: LLMRequest):
        await asyncio.sleep(999)  # hang
        yield  # never reached


class TestA2LLMHangCancel:
    """A2: LLM hang 时 task cancel。

    场景：MockLLM call() hang 999s，facade.interrupt() 后应 5 秒内终止。
    修复前：只 set event，runner 卡在 LLM await 点 → 5 秒超时。
    修复后：facade.interrupt() 调 task.cancel() → CancelledError 中止 await → 返回 interrupted。
    """

    def test_llm_hang_interrupt_terminates_within_5s(self, tmp_path):
        """红/绿测试：LLM hang 时 interrupt → 5 秒内 task done + RunOutcome(interrupted)。"""
        store = _make_store(tmp_path)
        try:
            ev = asyncio.Event()
            hang_llm = HangLLM()

            facade = SessionFacade(
                deps=RunnerDeps(
                    llm_gateway=hang_llm,
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=3, use_stream=False),
            )

            async def run_and_interrupt():
                # 启动 start() 在 task 中
                start_task = asyncio.create_task(facade.start("s1", "hi"))
                # 等 0.3s 让 runner 进入 LLM await（hang）
                await asyncio.sleep(0.3)
                assert hang_llm.call_count == 1, "LLM 应已被调用一次（hang 中）"
                # 调 interrupt()
                await facade.interrupt("s1")
                # 等 task 完成（修复前会 hang 999s，5 秒超时）
                try:
                    outcome = await asyncio.wait_for(start_task, timeout=5.0)
                    return outcome
                except TimeoutError:
                    start_task.cancel()
                    return None  # 超时

            result = asyncio.run(run_and_interrupt())

            # 核心断言 1：5 秒内 task done（修复前 None）
            assert result is not None, (
                "A2 修复前 bug：LLM hang 时 interrupt 5 秒内未终止 task（task 仍在 hang）"
            )
            # 核心断言 2：RunOutcome status=interrupted
            assert result.status == "interrupted", (
                f"A2 修复后 status 应为 interrupted，实际={result.status}"
            )
            assert result.stop_reason == "user_interrupted"

            # 验证 transition(user_interrupted) 事件写入
            events = asyncio.run(store.load_events("s1"))
            transitions = [
                e for e in events
                if e.type == "transition"
                and e.payload.get("reason") == TRANSITION_USER_INTERRUPTED
            ]
            assert len(transitions) >= 1, (
                f"应写 transition(user_interrupted) 事件，"
                f"实际 events={[e.type for e in events]}"
            )

            # 验证 session 状态为 interrupted
            session = asyncio.run(store.get_session("s1"))
            assert session.status == "interrupted"
        finally:
            store.close()

    def test_llm_hang_stream_mode_interrupt_terminates(self, tmp_path):
        """补充：stream 模式下 LLM hang 也能 interrupt。"""
        store = _make_store(tmp_path)
        try:
            ev = asyncio.Event()
            hang_llm = HangLLM()

            facade = SessionFacade(
                deps=RunnerDeps(
                    llm_gateway=hang_llm,
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s2", max_iterations=3, use_stream=True),
            )

            async def run_and_interrupt():
                start_task = asyncio.create_task(facade.start("s2", "hi"))
                await asyncio.sleep(0.3)
                await facade.interrupt("s2")
                try:
                    outcome = await asyncio.wait_for(start_task, timeout=5.0)
                    return outcome
                except TimeoutError:
                    start_task.cancel()
                    return None

            result = asyncio.run(run_and_interrupt())

            assert result is not None, (
                "A2 修复前 bug：stream 模式 LLM hang 时 interrupt 5 秒内未终止"
            )
            assert result.status == "interrupted"
        finally:
            store.close()

    def test_no_interrupt_normal_completion(self, tmp_path):
        """回归测试：无 interrupt 时正常完成（task cancel 机制不影响正常流程）。"""
        from client.core.agent import MockLLM

        store = _make_store(tmp_path)
        try:
            ev = asyncio.Event()  # 不 set
            llm = MockLLM(default_text="hello!")

            facade = SessionFacade(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s3", max_iterations=3, use_stream=False),
            )

            outcome = asyncio.run(facade.start("s3", "hi"))
            assert outcome.status == "completed"
        finally:
            store.close()
