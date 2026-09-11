"""v6-lite T01 验收测试：单轮文本对话端到端

覆盖 T01 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] `client/core/agent/` 库可被外部测试导入，不依赖 Qt
- [x] MockLLM 单轮文本对话：build → llm → append → finalize 全链路通过
- [x] sessions/messages/events 三表正确写入
- [x] deps/config 拆分：MockLLM 可注入，不硬编码
- [x] 单测覆盖主循环 happy path

外加边界测试：
- max_iterations 守卫
- B2 后：budget_exceeded 路径已删除（test_budget_exceeded_guard_removed）
- EventStore 启动恢复（重开连接仍能读取）
- 协议检查（MockLLM 实现 LLMGateway）
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path（conftest.py 已加，但单独运行时兜底）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    LLMGateway,
    Message,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    Session,
    SessionRunner,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


async def _create_session_with_user_message(
    store: EventStore, user_text: str, session_id: str = "test-sess"
) -> Session:
    """helper：创建会话 + 写入一条 user 消息。"""
    session = await store.create_session(session_id=session_id, title="T01 test")
    await store.append_message(
        session_id, Message(role="user", content=user_text, source="user")
    )
    return session


# ============================================================================
# 1. 库可导入 + 不依赖 Qt
# ============================================================================


def test_library_imports_without_qt():
    """T01 acceptance: 库可被外部测试导入，不依赖 Qt。"""
    # 如果 import 链中任何模块 import PySide6/PyQt，本测试会因 conftest 的 mock_overlay_client
    # 而不报错，但 sys.modules 会含 PySide6。这里直接断言 client.core.agent 不引 PySide6。
    import client.core.agent as pkg
    assert pkg.SessionRunner is SessionRunner
    assert pkg.MockLLM is MockLLM
    assert pkg.EventStore is EventStore

    # 检查 agent 包内的模块源码不直接 import PySide6/PyQt
    pkg_dir = Path(pkg.__file__).parent
    forbidden_patterns = ["import PySide6", "from PySide6", "import PyQt", "from PyQt"]
    for py_file in pkg_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert pat not in text, f"{py_file.name} 不应 import Qt：找到 {pat}"


def test_mock_llm_implements_gateway_protocol():
    """T01 acceptance: MockLLM 实现 LLMGateway 协议（runtime_checkable）。"""
    mock = MockLLM(default_text="hello")
    assert isinstance(mock, LLMGateway), "MockLLM 必须实现 LLMGateway 协议"


# ============================================================================
# 2. EventStore 三表 CRUD（基础）
# ============================================================================


def test_event_store_schema_has_three_tables(tmp_db_path):
    """T01+T02: sessions/messages/events/tool_calls 四表 + schema_info。

    T01 阶段只有 3 表；T02 加了 tool_calls 表。此测试验证完整 schema。
    """
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        conn = s.conn
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sessions" in tables
        assert "messages" in tables
        assert "events" in tables
        assert "tool_calls" in tables  # T02 added
        assert "schema_info" in tables
    finally:
        s.close()


def test_event_store_events_has_reserved_columns(tmp_db_path):
    """T01: events 表预留 prompt_index/invalidated_seq/parent_trace_id/depth 列（只留字段不写逻辑）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        cols = {
            r[1] for r in s.conn.execute("PRAGMA table_info(events)").fetchall()
        }
        assert {"seq", "session_id", "type", "payload_json", "trace_id", "created_at"} <= cols
        # v6 预留字段（T04+ 补逻辑）
        assert {"prompt_index", "invalidated_seq", "parent_trace_id", "depth"} <= cols
    finally:
        s.close()


def test_event_store_sessions_has_reserved_columns(tmp_db_path):
    """T01: sessions 表预留 prompt_index/last_compaction_prompt_index 列。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        cols = {
            r[1] for r in s.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert {"id", "title", "mode", "status", "created_at", "updated_at"} <= cols
        assert {"prompt_index", "last_compaction_prompt_index"} <= cols
    finally:
        s.close()


def test_create_session_writes_session_created_event(store):
    """T01: create_session 写 session_created event。"""
    async def run():
        session = await store.create_session(session_id="s1", title="hello")
        assert session.id == "s1"
        assert session.status == "idle"
        events = await store.load_events("s1")
        assert len(events) == 1
        assert events[0].type == "session_created"
        assert events[0].payload == {"title": "hello", "mode": "dialogue"}
    asyncio.run(run())


def test_append_message_assigns_seq_and_writes_event(store):
    """T01: append_message 自动分配 seq + 写 user_message_appended event。"""
    async def run():
        await store.create_session(session_id="s1")
        m1 = await store.append_message(
            "s1", Message(role="user", content="你好", source="user")
        )
        m2 = await store.append_message(
            "s1", Message(role="user", content="第二条", source="user")
        )
        assert m1.seq == 1
        assert m2.seq == 2
        assert m1.session_id == "s1"
        assert m1.id != ""
        events = await store.load_events("s1")
        # session_created + 2 * user_message_appended
        assert len(events) == 3
        assert events[1].type == "user_message_appended"
        assert events[2].type == "user_message_appended"
    asyncio.run(run())


def test_load_messages_skips_invisible_by_default(store):
    """T01: load_messages 默认跳过 visible=0 的 synthetic 消息。"""
    async def run():
        await store.create_session(session_id="s1")
        await store.append_message(
            "s1", Message(role="user", content="user-msg", source="user")
        )
        # synthetic 消息（v6-02 §4 续轮合成消息）
        await store.append_message(
            "s1",
            Message(role="user", content="verification_required", source="synthetic", visible=False),
        )
        visible = await store.load_messages("s1")
        assert len(visible) == 1
        assert visible[0].content == "user-msg"
        all_msgs = await store.load_messages("s1", include_invisible=True)
        assert len(all_msgs) == 2
    asyncio.run(run())


# ============================================================================
# 3. SessionRunner 单轮文本对话 happy path（核心 acceptance）
# ============================================================================


def test_session_runner_single_turn_happy_path(store):
    """T01 acceptance: MockLLM 单轮文本对话 build → llm → append → finalize 全链路通过。"""
    async def run():
        # 1. 准备会话 + 用户消息
        await _create_session_with_user_message(store, "你好", session_id="sess-1")

        # 2. 构建 runner（MockLLM 注入）
        mock = MockLLM(default_text="你好！我是 MockLLM。")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock, event_store=store),
            config=RunnerConfig(session_id="sess-1", max_iterations=10),
        )

        # 3. 运行
        outcome = await runner.run()

        # 4. 断言 RunOutcome
        assert outcome.session_id == "sess-1"
        assert outcome.status == "completed"
        assert outcome.stop_reason == "end_turn"
        assert outcome.iterations == 1
        # B2（spec D2/D3）：删 total_cost_usd 字段，验证 total_tokens 累计（MockLLM 默认 usage）
        assert outcome.total_tokens > 0

        # 5. 断言 session.status = completed
        session = await store.get_session("sess-1")
        assert session.status == "completed"

        # 6. 断言 messages 表：user + assistant
        messages = await store.load_messages("sess-1")
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "你好"
        assert messages[1].role == "assistant"
        assert messages[1].content == "你好！我是 MockLLM。"
        assert messages[1].source == "assistant"

        # 7. 断言 MockLLM 调用记录
        assert mock.call_count == 1
        assert len(mock.requests[0].messages) == 1
        assert mock.requests[0].messages[0].content == "你好"
    asyncio.run(run())


def test_session_runner_writes_session_status_events(store):
    """T01: SessionRunner 写入完整事件序列：session_created → user_message_appended →
    session_status_changed(streaming) → assistant_message_appended → session_status_changed(completed)
    → session_finalized。"""
    async def run():
        await _create_session_with_user_message(store, "hi", session_id="sess-1")

        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=MockLLM(default_text="hello"), event_store=store),
            config=RunnerConfig(session_id="sess-1"),
        )
        await runner.run()

        events = await store.load_events("sess-1")
        event_types = [e.type for e in events]
        # 完整事件序列
        assert event_types[0] == "session_created"
        assert event_types[1] == "user_message_appended"
        assert "session_status_changed" in event_types
        assert event_types[-1] == "session_finalized"
        # session_status_changed 至少 2 次（streaming + completed）
        status_changes = [e for e in events if e.type == "session_status_changed"]
        statuses = [e.payload["status"] for e in status_changes]
        assert "streaming" in statuses
        assert "completed" in statuses
    asyncio.run(run())


# ============================================================================
# 4. deps/config 拆分（可注入、不硬编码）
# ============================================================================


def test_deps_config_split_mock_llm_injectable(store):
    """T01 acceptance: deps/config 拆分：MockLLM 可注入，不硬编码。"""
    async def run():
        await _create_session_with_user_message(store, "ping", session_id="s-deps-1")

        # 同一 runner 代码，不同 MockLLM 响应
        mock_a = MockLLM(default_text="response-a")
        mock_b = MockLLM(default_text="response-b")

        for mock, expected in [(mock_a, "response-a"), (mock_b, "response-b")]:
            # 每次新建 session 避免状态污染
            sid = f"s-deps-{expected}"
            await store.create_session(session_id=sid)
            await store.append_message(sid, Message(role="user", content="ping", source="user"))
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock, event_store=store),
                config=RunnerConfig(session_id=sid),
            )
            outcome = await runner.run()
            assert outcome.status == "completed"
            msgs = await store.load_messages(sid)
            assert msgs[-1].content == expected
    asyncio.run(run())


def test_runner_config_is_frozen():
    """T01: RunnerConfig 是 frozen dataclass（v6-02 §3，进入 run() 时 snapshot 不可变）。"""
    cfg = RunnerConfig(session_id="s1", max_iterations=5)
    with pytest.raises((AttributeError, TypeError)):  # noqa: B017
        cfg.session_id = "s2"  # type: ignore[misc]


def test_runner_deps_injectable_clock_and_uuid():
    """T01: RunnerDeps.clock/uuid 可注入（v6-02 §3.1 测试收益）。"""
    fixed_time = 12345.0
    fixed_uuid = "fixed-uuid-1234"
    deps = RunnerDeps(
        llm_gateway=MockLLM(),
        clock=lambda: fixed_time,
        uuid=lambda: fixed_uuid,
    )
    assert deps.clock() == fixed_time
    assert deps.uuid() == fixed_uuid


# ============================================================================
# 5. 边界 / 守卫测试
# ============================================================================


def test_max_iterations_guard(store):
    """T01+T02: max_iterations 守卫——LLM 始终返回 tool_calls，达到 max_iterations 终止。

    T01 阶段此测试期望 NotImplementedError（tool_calls 未实现）；
    T02 实现后改用真实 tool_executor + 永远返回 tool_calls 的 MockLLM 触发循环上限。
    """
    async def run():
        await _create_session_with_user_message(store, "loop", session_id="s-maxit")
        from client.core.agent.types import LLMResponse
        # LLM 每轮都返回 tool_calls，永不终止
        mock = MockLLM(callable=lambda req: LLMResponse(
            content="继续调工具",
            tool_calls=[{"id": f"call-{int(time.time()*1000)}", "type": "function",
                         "function": {"name": "noop", "arguments": "{}"}}],
            stop_reason="tool_use",
        ))
        mock_tool = MockToolExecutor(default_result="noop-result")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="s-maxit", max_iterations=3),
        )
        outcome = await runner.run()
        assert outcome.status == "failed"
        assert outcome.stop_reason == "max_iterations"
        assert outcome.iterations == 3

        # 验证 max_iterations_reached event 落库
        events = await store.load_events("s-maxit")
        max_it_events = [e for e in events if e.type == "max_iterations_reached"]
        assert len(max_it_events) == 1
        assert max_it_events[0].payload["max_iterations"] == 3
    asyncio.run(run())


def test_budget_exceeded_guard_removed(store):
    """B2（spec D2/D3）：budget_exceeded 路径已删除，验证不再触发。

    原 test_budget_exceeded_guard 测试 max_budget_usd 守卫，B2 后字段删除，
    此测试改写为验证：高 usage 响应正常完成（不再 budget_exceeded）。
    """
    async def run():
        await _create_session_with_user_message(store, "expensive", session_id="s-budget")
        from client.core.agent.types import LLMResponse
        mock = MockLLM(script=[
            LLMResponse(content="costly", usage={"total_tokens": 100, "prompt_tokens": 60, "completion_tokens": 40}),
        ])
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock, event_store=store),
            config=RunnerConfig(session_id="s-budget"),  # B2: 删 max_budget_usd
        )
        outcome = await runner.run()
        # B2 后：高 usage 不再触发 budget_exceeded，正常 completed
        assert outcome.status == "completed"
        assert outcome.stop_reason == "end_turn"
        assert outcome.total_tokens == 100

        # 验证不再有 budget_exceeded event
        events = await store.load_events("s-budget")
        budget_events = [e for e in events if e.type == "budget_exceeded"]
        assert len(budget_events) == 0
    asyncio.run(run())


# ============================================================================
# 6. EventStore 重启恢复（重开连接仍能读取）
# ============================================================================


def test_event_store_reopen_and_replay(tmp_db_path):
    """T01: EventStore close 后重新 init，仍能读取会话与事件（持久化验证）。"""
    # 第一次：写入数据
    s1 = EventStore(db_path=tmp_db_path)
    s1.init()
    async def write():
        await s1.create_session(session_id="persist-1", title="持久化测试")
        await s1.append_message(
            "persist-1", Message(role="user", content="hello", source="user")
        )
    asyncio.run(write())
    s1.close()

    # 第二次：重新打开同一个 DB
    s2 = EventStore(db_path=tmp_db_path)
    s2.init()
    async def read():
        session = await s2.get_session("persist-1")
        assert session is not None
        assert session.title == "持久化测试"
        messages = await s2.load_messages("persist-1")
        assert len(messages) == 1
        assert messages[0].content == "hello"
        events = await s2.load_events("persist-1")
        # session_created + user_message_appended
        assert len(events) >= 2
        # 回放（W1 验收：所有 events 可按 seq 回放整个会话）
        replay = await s2.replay_events("persist-1")
        assert replay[0]["type"] == "session_created"
        assert replay[1]["type"] == "user_message_appended"
    asyncio.run(read())
    s2.close()


# ============================================================================
# 7. 全链路 demo（建会话 → build → llm → append → finalize → 重开读取）
# ============================================================================


def test_end_to_end_demo_with_reopen(tmp_db_path):
    """T01 全链路：建会话 → 写 user → runner 跑完 → close → reopen → 验证状态与消息。"""
    # 第一次：跑完整对话
    s1 = EventStore(db_path=tmp_db_path)
    s1.init()
    async def run_dialogue():
        await _create_session_with_user_message(s1, "你好，请自我介绍", session_id="demo-1")
        mock = MockLLM(default_text="我是 MockLLM，用于 v6-lite T01 验收。")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock, event_store=s1),
            config=RunnerConfig(session_id="demo-1"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        return outcome
    asyncio.run(run_dialogue())
    s1.close()

    # 第二次：重新打开，验证状态
    s2 = EventStore(db_path=tmp_db_path)
    s2.init()
    async def verify_persisted():
        session = await s2.get_session("demo-1")
        assert session.status == "completed"
        messages = await s2.load_messages("demo-1")
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "你好，请自我介绍"
        assert messages[1].role == "assistant"
        assert messages[1].content == "我是 MockLLM，用于 v6-lite T01 验收。"
        events = await s2.load_events("demo-1")
        # 至少 6 个事件：created + user_msg + status_streaming + assistant_msg + status_completed + finalized
        assert len(events) >= 6
        # 最后一个事件必须是 session_finalized
        assert events[-1].type == "session_finalized"
    asyncio.run(verify_persisted())
    s2.close()


# ============================================================================
# 8. 默认 DB 路径检查
# ============================================================================


