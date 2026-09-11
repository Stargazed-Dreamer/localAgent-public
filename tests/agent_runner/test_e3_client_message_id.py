"""E3 · client_message_id 幂等去重。

facade.start() 加 client_message_id 参数，重复提交时返回上次 outcome，
不重复触发 LLM 调用。

测试策略（red_green，spec 标注）：
- 红测试：同 client_message_id 提交两次，断言触发两次 LLM（修复前失败）
- 绿测试：修复后断言只触发一次

注：测试用 MockLLM，不依赖后端运行。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_e3.db"))
    store.init()
    return store


class _CountingLLM:
    """计数 Mock LLM，记录调用次数。"""

    def __init__(self):
        self.call_count = 0

    async def call(self, request):
        self.call_count += 1
        return LLMResponse(content=f"response {self.call_count}", stop_reason="end_turn")

    async def stream(self, request):
        self.call_count += 1
        yield {"type": "text_delta", "delta": f"response {self.call_count}"}
        yield {"type": "done", "finish_reason": "stop"}


# ============================================================================
# Part 1: EventStore client_message_id 方法
# ============================================================================


class TestEventStoreClientMessageId:
    """E3: EventStore 的 client_message_id 方法。"""

    def test_table_exists_after_init(self, tmp_path):
        """client_message_ids 表在 init() 后存在。"""
        store = _new_store(tmp_path)
        try:
            cursor = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='client_message_ids'"
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["name"] == "client_message_ids"
        finally:
            store.close()

    def test_has_client_message_id_returns_false_for_new(self, tmp_path):
        """新 client_message_id → has_client_message_id 返回 False。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                result = await store.has_client_message_id("cmid_new")
                assert result is False
            _run(_run_async())
        finally:
            store.close()

    def test_has_client_message_id_returns_true_after_record(self, tmp_path):
        """record 后 → has_client_message_id 返回 True。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s1")
                await store.record_client_message_id(
                    "cmid_1", "s1", "completed", "end_turn"
                )
                result = await store.has_client_message_id("cmid_1")
                assert result is True
            _run(_run_async())
        finally:
            store.close()

    def test_get_outcome_for_client_message_id(self, tmp_path):
        """record 后 → get_outcome_for_client_message_id 返回正确结果。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_outcome")
                await store.record_client_message_id(
                    "cmid_outcome", "s_outcome", "interrupted", "wall_clock_budget_exceeded"
                )
                result = await store.get_outcome_for_client_message_id("cmid_outcome")
                assert result is not None
                assert result["session_id"] == "s_outcome"
                assert result["outcome_status"] == "interrupted"
                assert result["outcome_stop_reason"] == "wall_clock_budget_exceeded"
            _run(_run_async())
        finally:
            store.close()

    def test_get_outcome_returns_none_for_missing(self, tmp_path):
        """不存在的 client_message_id → get_outcome 返回 None。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                result = await store.get_outcome_for_client_message_id("cmid_missing")
                assert result is None
            _run(_run_async())
        finally:
            store.close()

    def test_record_is_idempotent(self, tmp_path):
        """同 client_message_id record 两次 → INSERT OR REPLACE 不报错。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_idem")
                await store.record_client_message_id(
                    "cmid_idem", "s_idem", "completed", "end_turn"
                )
                # 第二次 record 同 cmid（REPLACE 语义）
                await store.record_client_message_id(
                    "cmid_idem", "s_idem", "failed", "provider_error"
                )
                result = await store.get_outcome_for_client_message_id("cmid_idem")
                assert result["outcome_status"] == "failed"
                assert result["outcome_stop_reason"] == "provider_error"
            _run(_run_async())
        finally:
            store.close()


# ============================================================================
# Part 2: Facade client_message_id 幂等
# ============================================================================


class TestFacadeClientMessageIdIdempotency:
    """E3: facade.start() 传相同 client_message_id → 返回缓存，不重复 LLM。"""

    def test_duplicate_client_message_id_skips_llm(self, tmp_path):
        """同 client_message_id 提交两次 → 第二次返回缓存，LLM 只调一次。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_dup1")
                await store.append_message(
                    "s_dup1", Message(role="user", content="hi", source="user")
                )

                llm = _CountingLLM()
                config = RunnerConfig(
                    session_id="s_dup1", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(llm_gateway=llm, event_store=store),
                    config=config,
                )

                # 第一次 start
                outcome1 = await facade.start(
                    "s_dup1", "first message",
                    client_message_id="cmid_dup",
                )
                assert outcome1.status == "completed"
                assert llm.call_count == 1

                # 第二次 start（同 client_message_id，不同 session）
                await store.create_session(session_id="s_dup2")
                await store.append_message(
                    "s_dup2", Message(role="user", content="hi2", source="user")
                )
                outcome2 = await facade.start(
                    "s_dup2", "second message",
                    client_message_id="cmid_dup",  # 同 cmid
                )

                # LLM 不应被再次调用（返回缓存）
                assert llm.call_count == 1, (
                    f"E3: 重复 client_message_id 应返回缓存不调 LLM，"
                    f"但 LLM 被调了 {llm.call_count} 次（期望 1）"
                )

                # 返回的 outcome 是缓存的（session_id=s_dup1, status=completed）
                assert outcome2.session_id == "s_dup1"
                assert outcome2.status == "completed"

            _run(_run_async())
        finally:
            store.close()

    def test_different_client_message_id_triggers_llm(self, tmp_path):
        """不同 client_message_id → 正常触发 LLM。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_diff1")
                await store.append_message(
                    "s_diff1", Message(role="user", content="hi", source="user")
                )
                await store.create_session(session_id="s_diff2")
                await store.append_message(
                    "s_diff2", Message(role="user", content="hi2", source="user")
                )

                llm = _CountingLLM()
                config = RunnerConfig(
                    session_id="s_diff1", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(llm_gateway=llm, event_store=store),
                    config=config,
                )

                await facade.start(
                    "s_diff1", "first", client_message_id="cmid_a",
                )
                assert llm.call_count == 1

                await facade.start(
                    "s_diff2", "second", client_message_id="cmid_b",
                )
                assert llm.call_count == 2  # 不同 cmid → 正常调用

            _run(_run_async())
        finally:
            store.close()

    def test_no_client_message_id_no_idempotency(self, tmp_path):
        """不传 client_message_id → 无幂等检查，每次都触发 LLM。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_nocmid1")
                await store.append_message(
                    "s_nocmid1", Message(role="user", content="hi", source="user")
                )
                await store.create_session(session_id="s_nocmid2")
                await store.append_message(
                    "s_nocmid2", Message(role="user", content="hi2", source="user")
                )

                llm = _CountingLLM()
                config = RunnerConfig(
                    session_id="s_nocmid1", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(llm_gateway=llm, event_store=store),
                    config=config,
                )

                await facade.start("s_nocmid1", "first")  # 不传 cmid
                await facade.start("s_nocmid2", "second")  # 不传 cmid
                assert llm.call_count == 2  # 无幂等 → 都调

            _run(_run_async())
        finally:
            store.close()

    def test_outcome_recorded_after_run(self, tmp_path):
        """run 完成后 → client_message_id 和 outcome 已记录到 DB。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_rec")
                await store.append_message(
                    "s_rec", Message(role="user", content="hi", source="user")
                )

                llm = _CountingLLM()
                config = RunnerConfig(
                    session_id="s_rec", max_iterations=1, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(llm_gateway=llm, event_store=store),
                    config=config,
                )

                await facade.start(
                    "s_rec", "test", client_message_id="cmid_rec",
                )

                # 验证 DB 有记录
                has = await store.has_client_message_id("cmid_rec")
                assert has is True

                outcome = await store.get_outcome_for_client_message_id("cmid_rec")
                assert outcome is not None
                assert outcome["session_id"] == "s_rec"
                assert outcome["outcome_status"] == "completed"

            _run(_run_async())
        finally:
            store.close()

    def test_interrupted_outcome_cached_correctly(self, tmp_path):
        """interrupted 状态的 outcome 也应正确缓存。"""
        store = _new_store(tmp_path)
        try:
            async def _run_async():
                await store.create_session(session_id="s_int")
                await store.append_message(
                    "s_int", Message(role="user", content="hi", source="user")
                )

                # LLM 返回带 tool_call，但 tool 执行前 interrupt
                import asyncio as _asyncio
                interrupt_event = _asyncio.Event()

                tool_call = {
                    "id": "tc_int",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }

                class InterruptLLM:
                    def __init__(self):
                        self.call_count = 0

                    async def call(self, request):
                        self.call_count += 1
                        return LLMResponse(
                            content="",
                            tool_calls=[tool_call],
                            stop_reason="tool_use",
                        )

                    async def stream(self, request):
                        self.call_count += 1
                        yield {"type": "tool_call_delta", "tool_call": tool_call}
                        yield {"type": "done", "finish_reason": "tool_calls"}

                from client.core.agent.types import ToolResult, ToolResultVariant

                class InterruptTool:
                    async def execute(self, tool_call):
                        # 执行前 set interrupt
                        interrupt_event.set()
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            content="ok",
                            variant=ToolResultVariant.SUCCESS,
                        )

                llm = InterruptLLM()
                config = RunnerConfig(
                    session_id="s_int", max_iterations=5, use_stream=False
                )
                facade = SessionFacade(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=InterruptTool(),
                        interrupt_event=interrupt_event,
                    ),
                    config=config,
                )

                outcome = await facade.start(
                    "s_int", "test", client_message_id="cmid_int",
                )
                # 可能是 interrupted 或 completed，取决于时序
                assert outcome.status in ("interrupted", "completed")

                # 验证 DB 记录与实际 outcome 一致
                cached = await store.get_outcome_for_client_message_id("cmid_int")
                assert cached is not None
                assert cached["outcome_status"] == outcome.status

            _run(_run_async())
        finally:
            store.close()
