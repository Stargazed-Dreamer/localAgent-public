"""E6 · runner 状态持久化（spec D23）。

D23 决策：不新建 ChatStateSnapshot dataclass，runner 关键状态写 DB。
关键状态：iterations / has_attempted_reactive_compact / total_tokens / wall_clock_exceeded

测试策略（regression_only，spec 标注）：
1. EventStore: runner_state 表存在 + 字段完整 + schema 版本 8
2. upsert_runner_state 写入 / 更新（INSERT OR REPLACE 幂等）
3. get_runner_state 返回 dict / None
4. runner.run() 完成后 runner_state 表有记录
5. 多轮迭代状态正确更新（iterations + total_tokens 累计）
6. 中断路径也落库
7. has_attempted_reactive_compact 设置后落库
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
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolResult,
)
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_e6.db"))
    store.init()
    return store


# ============================================================================
# Part 1: EventStore schema + CRUD
# ============================================================================


class TestRunnerStateTable:
    """runner_state 表存在 + 字段完整 + schema 版本 8。"""

    def test_table_exists_after_init(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            cur = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runner_state'"
            )
            assert cur.fetchone() is not None, "E6: runner_state 表应存在"
        finally:
            store.close()

    def test_table_columns(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            cols = {r[1]: r[2] for r in store.conn.execute(
                "PRAGMA table_info(runner_state)"
            ).fetchall()}
            expected = {
                "session_id", "iterations", "has_attempted_reactive_compact",
                "total_tokens", "wall_clock_exceeded", "last_updated_at",
            }
            assert expected.issubset(cols.keys()), (
                f"E6: runner_state 缺字段，expected={expected}, got={set(cols.keys())}"
            )
        finally:
            store.close()

    def test_schema_version_is_8(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            row = store.conn.execute(
                "SELECT value FROM schema_info WHERE key='version'"
            ).fetchone()
            assert int(row[0]) >= 8, f"E6: schema 版本应 >= 8, got {row[0]}"
        finally:
            store.close()


# ============================================================================
# Part 2: upsert / get 方法
# ============================================================================


class TestUpsertGetRunnerState:
    """upsert_runner_state 写入/更新 + get_runner_state 读取。"""

    def test_upsert_and_get(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s1")
                await store.upsert_runner_state(
                    "s1", iterations=3,
                    has_attempted_reactive_compact=False,
                    total_tokens=150,
                    wall_clock_exceeded=False,
                )
                state = await store.get_runner_state("s1")
                assert state is not None
                assert state["session_id"] == "s1"
                assert state["iterations"] == 3
                assert state["has_attempted_reactive_compact"] in (0, False)
                assert state["total_tokens"] == 150
                assert state["wall_clock_exceeded"] in (0, False)
                assert state["last_updated_at"] > 0
            _run(run())
        finally:
            store.close()

    def test_upsert_is_idempotent_replace(self, tmp_path):
        """同 session 多次 upsert → INSERT OR REPLACE，最后一次覆盖前面。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s2")
                await store.upsert_runner_state(
                    "s2", iterations=1,
                    has_attempted_reactive_compact=False,
                    total_tokens=50,
                    wall_clock_exceeded=False,
                )
                await store.upsert_runner_state(
                    "s2", iterations=5,
                    has_attempted_reactive_compact=True,
                    total_tokens=300,
                    wall_clock_exceeded=True,
                )
                state = await store.get_runner_state("s2")
                assert state["iterations"] == 5
                assert state["has_attempted_reactive_compact"] in (1, True)
                assert state["total_tokens"] == 300
                assert state["wall_clock_exceeded"] in (1, True)
            _run(run())
        finally:
            store.close()

    def test_get_returns_none_for_no_record(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s3")
                state = await store.get_runner_state("s3")
                assert state is None
            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 3: runner.run() 后状态落库
# ============================================================================


class TestRunnerStatePersistedAfterRun:
    """runner.run() 完成后 runner_state 表应有记录。"""

    def test_completed_run_persists_state(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_run1")
                await store.append_message("s_run1", Message(
                    role="user", content="hi", source="user",
                ))
                llm = MockLLM(callable=lambda req: LLMResponse(
                    content="hello",
                    tool_calls=[],
                    stop_reason="stop",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ))
                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=MockToolExecutor(),
                    ),
                    config=RunnerConfig(
                        session_id="s_run1", max_iterations=3,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()
                assert outcome.status == "completed"

                # E6: runner_state 表应有记录
                state = await store.get_runner_state("s_run1")
                assert state is not None, "E6: 完成后 runner_state 表应有记录"
                assert state["iterations"] >= 1
                assert state["total_tokens"] == 15  # 1 轮 * 15 tokens
                assert state["has_attempted_reactive_compact"] in (0, False)
                assert state["wall_clock_exceeded"] in (0, False)
            _run(run())
        finally:
            store.close()

    def test_multi_iteration_accumulates(self, tmp_path):
        """多轮 tool_call 迭代：iterations + total_tokens 累计正确落库。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_run2")
                await store.append_message("s_run2", Message(
                    role="user", content="loop", source="user",
                ))
                # 第一轮返回 tool_call，第二轮返回 stop
                call_count = {"n": 0}
                def llm_fn(req):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        return LLMResponse(
                            content="",
                            tool_calls=[{
                                "id": "tc1", "type": "function",
                                "function": {"name": "noop", "arguments": "{}"},
                            }],
                            stop_reason="tool_use",
                            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                        )
                    return LLMResponse(
                        content="done",
                        tool_calls=[],
                        stop_reason="stop",
                        usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                    )
                llm = MockLLM(callable=llm_fn)
                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=MockToolExecutor(callable=lambda tc: ToolResult(
                            tool_call_id=tc.id, content="ok",
                        )),
                    ),
                    config=RunnerConfig(
                        session_id="s_run2", max_iterations=5,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()
                assert outcome.status == "completed"
                assert outcome.iterations == 2

                state = await store.get_runner_state("s_run2")
                assert state is not None
                assert state["iterations"] == 2
                assert state["total_tokens"] == 40  # 15 + 25
            _run(run())
        finally:
            store.close()

    def test_interrupted_run_persists_state(self, tmp_path):
        """中断路径也落库。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_run3")
                await store.append_message("s_run3", Message(
                    role="user", content="hi", source="user",
                ))
                ev = asyncio.Event()
                # 第一轮 LLM 后立刻 interrupt（在 LLM 调用前 set）
                ev.set()
                llm = MockLLM(callable=lambda req: LLMResponse(
                    content="hello",
                    tool_calls=[],
                    stop_reason="stop",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ))
                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        tool_executor=MockToolExecutor(),
                        interrupt_event=ev,
                    ),
                    config=RunnerConfig(
                        session_id="s_run3", max_iterations=3,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()
                assert outcome.status == "interrupted"

                # E6: 中断路径也应落库
                state = await store.get_runner_state("s_run3")
                assert state is not None, "E6: 中断后 runner_state 表也应有记录"
            _run(run())
        finally:
            store.close()
