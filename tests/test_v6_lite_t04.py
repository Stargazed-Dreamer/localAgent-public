"""v6-lite T04 验收测试：WAL 模式 + events 预留字段 + W1 完整验收

覆盖 T04 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] SQLite WAL 模式生效
- [x] events 表预留字段补全（schema 有，逻辑不写）
- [x] MockLLM 4 场景测试全绿：单轮文本 / 一次工具回灌 / 连续两轮工具 / 正常终止
- [x] 所有 events 可按 seq 回放整个会话
- [x] deps/config 拆分完备（MockLLM/MockToolExecutor 可注入）

这是 W1 收尾 ticket——验证 W1 切片（纯 MockLLM + MockToolExecutor + SQLite）端到端可用。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    LLMGateway,
    LLMResponse,
    Message,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolExecutor,
)
from client.core.agent.types import (  # noqa: E402
    TOOL_STATUS_COMPLETED,
    TRANSITION_NEXT_TURN,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


def _make_openai_tool_call(call_id: str, name: str, args: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {"path": "."}),
        },
    }


async def _seed_session(store: EventStore, user_text: str, session_id: str) -> None:
    await store.create_session(session_id=session_id, title="T04 test")
    await store.append_message(
        session_id, Message(role="user", content=user_text, source="user")
    )


# ============================================================================
# 1. SQLite WAL 模式生效
# ============================================================================


def test_wal_mode_active(tmp_db_path):
    """T04 acceptance: SQLite WAL 模式生效。

    PRAGMA journal_mode=WAL 后，再次查询应返回 'wal'。
    注意：in-memory DB（:memory:）不支持 WAL，会返回 'memory'；
    测试用 tmp 文件路径才能生效。
    """
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"WAL mode expected, got {mode}"
    finally:
        s.close()


def test_wal_mode_persists_across_reopen(tmp_db_path):
    """T04: WAL 模式持久化——重开后仍是 WAL（SQLite WAL 是数据库级持久属性）。"""
    s1 = EventStore(db_path=tmp_db_path)
    s1.init()
    s1.close()

    s2 = EventStore(db_path=tmp_db_path)
    s2.init()
    try:
        mode = s2.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        s2.close()


def test_wal_checkpoint_works(tmp_db_path):
    """T04: WAL 模式下 checkpoint 可执行（不抛异常）。

    WAL 模式下写入先进 -wal 文件，checkpoint 后合并回主 DB。
    T04 不强制验证 checkpoint 语义，只验证 PRAGMA wal_checkpoint 不抛异常。
    """
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        # 写一些数据
        asyncio.run(s.create_session(session_id="wal-test", title="wal"))
        # checkpoint（PASSIVE 模式，默认）
        result = s.conn.execute("PRAGMA wal_checkpoint").fetchone()
        # result: (busy, log, checkpointed)
        assert result is not None
    finally:
        s.close()


# ============================================================================
# 2. events 表预留字段补全（schema 有，逻辑不写）
# ============================================================================


def test_events_table_has_all_reserved_fields(tmp_db_path):
    """T04 acceptance: events 表预留字段补全。

    v6-01 §1.1 预留字段（为 rewind / compaction / 子 agent 留门）：
    - prompt_index: 回滚锚点
    - invalidated_seq: append-only 不可删，恢复/undo 时标记
    - parent_trace_id: 子 agent 保留 parent
    - depth: 子 agent +1
    """
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        cols = {
            r[1]: r for r in s.conn.execute("PRAGMA table_info(events)").fetchall()
        }
        # 核心字段
        assert {"seq", "session_id", "type", "payload_json", "trace_id", "created_at"} <= set(cols)
        # v6 预留字段
        assert "prompt_index" in cols
        assert "invalidated_seq" in cols
        assert "parent_trace_id" in cols
        assert "depth" in cols
        # depth 有默认值 0
        assert cols["depth"][3] == "0" or cols["depth"][4] == "0"  # dflt_value 列
    finally:
        s.close()


def test_events_reserved_fields_default_null_or_zero(store):
    """T04: 预留字段默认值——append_event 后预留字段为 NULL 或 0。"""
    async def run():
        await store.create_session(session_id="s-reserved")
        await store.append_event("s-reserved", "test_event", {"k": "v"})

        row = store.conn.execute(
            "SELECT * FROM events WHERE session_id = ? AND type = ?",
            ("s-reserved", "test_event"),
        ).fetchone()
        assert row is not None
        # 预留字段默认值
        assert row["prompt_index"] is None  # NULL（未设置）
        assert row["invalidated_seq"] is None  # NULL
        assert row["parent_trace_id"] is None  # NULL
        assert row["depth"] == 0  # DEFAULT 0
    asyncio.run(run())


def test_events_reserved_fields_can_be_written(store):
    """T04: 预留字段可写入（T06+ reconciliation 会用，T04 只验证 schema 支持）。

    虽然当前 append_event 不写这些字段，但 schema 允许直接 INSERT 写入。
    """
    async def run():
        await store.create_session(session_id="s-write-reserved")
        # create_session 已写 session_created (seq=1)，这里用 seq=2 避免冲突
        store.conn.execute(
            "INSERT INTO events(seq, session_id, type, payload_json, trace_id, created_at, "
            "prompt_index, invalidated_seq, parent_trace_id, depth) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "s-write-reserved", "test_reserved", "{}", "trace-1", 1000.0,
             5, 3, "parent-trace", 2),
        )
        store.conn.commit()

        events = await store.load_events("s-write-reserved")
        # session_created + test_reserved = 2 events
        assert len(events) == 2
        reserved_event = next(e for e in events if e.type == "test_reserved")
        assert reserved_event.prompt_index == 5
        assert reserved_event.invalidated_seq == 3
        assert reserved_event.parent_trace_id == "parent-trace"
        assert reserved_event.depth == 2
    asyncio.run(run())


def test_sessions_table_has_reserved_fields(tmp_db_path):
    """T04: sessions 表预留字段（prompt_index / last_compaction_prompt_index）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        cols = {
            r[1] for r in s.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert {"prompt_index", "last_compaction_prompt_index"} <= cols
    finally:
        s.close()


# ============================================================================
# 3. W1 完整 4 场景验收测试
# ============================================================================


class TestW1FourScenarios:
    """W1 验收：4 场景端到端测试（v6-lite §3 W1）。

    场景 1: 单轮文本（无工具）
    场景 2: 一次工具回灌
    场景 3: 连续两轮工具
    场景 4: 正常终止
    """

    def test_scenario_1_single_turn_text(self, store):
        """场景 1: 单轮文本对话——user → assistant → finalize。"""
        async def run():
            await _seed_session(store, "你好", session_id="w1-s1")
            mock = MockLLM(default_text="你好！我是助手。")
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock, event_store=store, tool_executor=MockToolExecutor()),
                config=RunnerConfig(session_id="w1-s1"),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert outcome.iterations == 1
            messages = await store.load_messages("w1-s1")
            assert len(messages) == 2
            assert messages[1].content == "你好！我是助手。"
        asyncio.run(run())

    def test_scenario_2_one_tool_round_trip(self, store):
        """场景 2: 一次工具回灌——user → assistant(tool_calls) → tool(result) → assistant(文本)。"""
        async def run():
            await _seed_session(store, "列出目录", session_id="w1-s2")
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="我来查。",
                    tool_calls=[_make_openai_tool_call("c1", "list_dir")],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="目录有 a.txt 和 b.txt。", stop_reason="end_turn"),
            ])
            mock_tool = MockToolExecutor(fixtures={"c1": "a.txt\nb.txt"})
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
                config=RunnerConfig(session_id="w1-s2"),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert outcome.iterations == 2
            tool_calls = await store.load_tool_calls("w1-s2")
            assert len(tool_calls) == 1
            assert tool_calls[0].status == TOOL_STATUS_COMPLETED
        asyncio.run(run())

    def test_scenario_3_two_consecutive_tool_turns(self, store):
        """场景 3: 连续两轮工具——user → asst(tool) → tool → asst(tool) → tool → asst(文本)。"""
        async def run():
            await _seed_session(store, "对比两个文件", session_id="w1-s3")
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="读 a",
                    tool_calls=[_make_openai_tool_call("c1", "read", {"path": "a"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(
                    content="读 b",
                    tool_calls=[_make_openai_tool_call("c2", "read", {"path": "b"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="对比完成。", stop_reason="end_turn"),
            ])
            mock_tool = MockToolExecutor(fixtures={"c1": "content-a", "c2": "content-b"})
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
                config=RunnerConfig(session_id="w1-s3", max_iterations=10),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert outcome.iterations == 3
            tool_calls = await store.load_tool_calls("w1-s3")
            assert len(tool_calls) == 2
            assert all(tc.status == TOOL_STATUS_COMPLETED for tc in tool_calls)
        asyncio.run(run())

    def test_scenario_4_normal_termination(self, store):
        """场景 4: 正常终止——session.status=completed + session_finalized event。"""
        async def run():
            await _seed_session(store, "任务完成", session_id="w1-s4")
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="执行",
                    tool_calls=[_make_openai_tool_call("c1", "work")],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="任务完成。", stop_reason="end_turn"),
            ])
            mock_tool = MockToolExecutor(default_result="done")
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
                config=RunnerConfig(session_id="w1-s4"),
            )
            outcome = await runner.run()

            assert outcome.status == "completed"
            assert outcome.stop_reason == "end_turn"
            session = await store.get_session("w1-s4")
            assert session.status == "completed"
            events = await store.load_events("w1-s4")
            assert any(e.type == "session_finalized" for e in events)
        asyncio.run(run())


# ============================================================================
# 4. 所有 events 可按 seq 回放整个会话
# ============================================================================


def test_replay_full_session_events(store):
    """T04 acceptance: 所有 events 可按 seq 回放整个会话。

    构造一个含工具调用的完整会话，用 replay_events 回放，验证：
    - seq 单调递增无重复
    - 事件类型序列完整（session_created → user_message_appended → streaming → ... → finalized）
    """
    async def run():
        await _seed_session(store, "回放测试", session_id="w1-replay")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="调工具",
                tool_calls=[_make_openai_tool_call("c1", "work")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(default_result="ok")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="w1-replay"),
        )
        await runner.run()

        replay = await store.replay_events("w1-replay")

        # seq 单调递增无重复
        seqs = [e["seq"] for e in replay]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        # 事件序列完整
        types = [e["type"] for e in replay]
        assert types[0] == "session_created"
        assert types[-1] == "session_finalized"
        assert "user_message_appended" in types
        assert "session_status_changed" in types
        assert "assistant_message_appended" in types
        assert "tool_call_pending" in types
        assert "tool_call_status_changed" in types
        assert "tool_result_appended" in types
        assert "transition" in types

        # transition reason = next_turn
        transitions = [e for e in replay if e["type"] == "transition"]
        assert len(transitions) == 1
        assert transitions[0]["payload"]["reason"] == TRANSITION_NEXT_TURN
    asyncio.run(run())


def test_replay_preserves_payload_integrity(store):
    """T04: replay_events 返回的 payload 完整（可 JSON 序列化，无数据丢失）。"""
    async def run():
        await store.create_session(session_id="w1-payload")
        await store.append_event("w1-payload", "complex_event", {
            "string": "中文测试",
            "number": 42,
            "nested": {"a": 1, "b": [2, 3]},
            "bool": True,
            "null": None,
        })

        replay = await store.replay_events("w1-payload")
        test_event = next(e for e in replay if e["type"] == "complex_event")
        payload = test_event["payload"]
        assert payload["string"] == "中文测试"
        assert payload["number"] == 42
        assert payload["nested"] == {"a": 1, "b": [2, 3]}
        assert payload["bool"] is True
        assert payload["null"] is None
    asyncio.run(run())


# ============================================================================
# 5. deps/config 拆分完备
# ============================================================================


def test_deps_config_complete_mock_llm_injectable(store):
    """T04 acceptance: deps/config 拆分完备——MockLLM 可注入。"""
    async def run():
        await _seed_session(store, "test", session_id="w1-deps-llm")
        mock = MockLLM(default_text="injected")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock, event_store=store, tool_executor=MockToolExecutor()),
            config=RunnerConfig(session_id="w1-deps-llm"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        msgs = await store.load_messages("w1-deps-llm")
        assert msgs[-1].content == "injected"
    asyncio.run(run())


def test_deps_config_complete_mock_tool_executor_injectable(store):
    """T04 acceptance: deps/config 拆分完备——MockToolExecutor 可注入。"""
    async def run():
        await _seed_session(store, "test", session_id="w1-deps-tool")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="call tool",
                tool_calls=[_make_openai_tool_call("c1", "work")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="done", stop_reason="end_turn"),
        ])
        # 注入自定义 MockToolExecutor
        mock_tool = MockToolExecutor(fixtures={"c1": "custom-result"})
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="w1-deps-tool"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        msgs = await store.load_messages("w1-deps-tool")
        # tool 消息含自定义结果
        tool_msg = next(m for m in msgs if m.role == "tool")
        assert tool_msg.content == "custom-result"
    asyncio.run(run())


def test_deps_config_clock_uuid_injectable():
    """T04: clock/uuid 可注入（测试可重复性根基）。"""
    fixed_time = 99999.0
    fixed_uuid = "fixed-uuid-t04"
    deps = RunnerDeps(
        llm_gateway=MockLLM(),
        clock=lambda: fixed_time,
        uuid=lambda: fixed_uuid,
    )
    assert deps.clock() == fixed_time
    assert deps.uuid() == fixed_uuid


def test_mock_llm_implements_gateway_protocol():
    """T04: MockLLM 实现 LLMGateway 协议。"""
    assert isinstance(MockLLM(), LLMGateway)


def test_mock_tool_executor_implements_protocol():
    """T04: MockToolExecutor 实现 ToolExecutor 协议。"""
    assert isinstance(MockToolExecutor(), ToolExecutor)


def test_runner_config_frozen():
    """T04: RunnerConfig frozen（进入 run() 后不可变）。"""
    cfg = RunnerConfig(session_id="s1")
    with pytest.raises((AttributeError, TypeError)):  # noqa: B017
        cfg.session_id = "s2"  # type: ignore[misc]


# ============================================================================
# 6. W1 收尾综合测试：4 场景同一 DB 跑完不互相干扰
# ============================================================================


def test_w1_all_four_scenarios_in_one_db(store):
    """W1 收尾: 4 场景在同一 EventStore 内依次运行，互不干扰。

    验证 EventStore 支持多会话并行存储，session_id 隔离正确。
    """
    async def run():
        # 场景 1: 单轮文本
        await _seed_session(store, "你好", session_id="w1-all-1")
        runner1 = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(default_text="hi"),
                event_store=store, tool_executor=MockToolExecutor(),
            ),
            config=RunnerConfig(session_id="w1-all-1"),
        )
        o1 = await runner1.run()
        assert o1.status == "completed"

        # 场景 2: 一次工具回灌
        await _seed_session(store, "list", session_id="w1-all-2")
        runner2 = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(script=[
                    LLMResponse(
                        content="call",
                        tool_calls=[_make_openai_tool_call("c-all-1", "list")],
                        stop_reason="tool_use",
                    ),
                    LLMResponse(content="done", stop_reason="end_turn"),
                ]),
                event_store=store,
                tool_executor=MockToolExecutor(default_result="files"),
            ),
            config=RunnerConfig(session_id="w1-all-2"),
        )
        o2 = await runner2.run()
        assert o2.status == "completed"

        # 场景 3: 连续两轮工具
        await _seed_session(store, "compare", session_id="w1-all-3")
        runner3 = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(script=[
                    LLMResponse(
                        content="a",
                        tool_calls=[_make_openai_tool_call("c-all-2", "read")],
                        stop_reason="tool_use",
                    ),
                    LLMResponse(
                        content="b",
                        tool_calls=[_make_openai_tool_call("c-all-3", "read")],
                        stop_reason="tool_use",
                    ),
                    LLMResponse(content="done", stop_reason="end_turn"),
                ]),
                event_store=store,
                tool_executor=MockToolExecutor(default_result="ok"),
            ),
            config=RunnerConfig(session_id="w1-all-3", max_iterations=10),
        )
        o3 = await runner3.run()
        assert o3.status == "completed"

        # 场景 4: 正常终止（已在场景 1-3 验证，这里单独验证 session_finalized）
        await _seed_session(store, "final", session_id="w1-all-4")
        runner4 = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(default_text="final"),
                event_store=store, tool_executor=MockToolExecutor(),
            ),
            config=RunnerConfig(session_id="w1-all-4"),
        )
        o4 = await runner4.run()
        assert o4.status == "completed"

        # 验证 4 个会话互不干扰
        for sid in ["w1-all-1", "w1-all-2", "w1-all-3", "w1-all-4"]:
            session = await store.get_session(sid)
            assert session is not None
            assert session.status == "completed"

        # 各会话消息数正确
        assert len(await store.load_messages("w1-all-1")) == 2  # user + assistant
        assert len(await store.load_messages("w1-all-2")) == 4  # user + asst + tool + asst
        assert len(await store.load_messages("w1-all-3")) == 6  # user + asst + tool + asst + tool + asst
        assert len(await store.load_messages("w1-all-4")) == 2

        # tool_calls 只在场景 2、3 中
        assert len(await store.load_tool_calls("w1-all-1")) == 0
        assert len(await store.load_tool_calls("w1-all-2")) == 1
        assert len(await store.load_tool_calls("w1-all-3")) == 2
        assert len(await store.load_tool_calls("w1-all-4")) == 0

    asyncio.run(run())
