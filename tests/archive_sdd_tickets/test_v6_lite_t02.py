"""v6-lite T02 验收测试：工具调用回灌端到端

覆盖 T02 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] tool_calls 表正确写入（pending → running → completed 状态流转）
- [x] MockLLM 工具轮：返回 tool_calls → 执行 → 回灌 → 继续循环通过
- [x] tool_calls 先写 pending 再执行的顺序有测试断言
- [x] 工具结果作为 tool_result 消息回灌到 messages 表

外加边界测试：
- MockToolExecutor 实现 ToolExecutor 协议
- OpenAI 格式 tool_call 解析（含坏 JSON）
- error-as-output-variant（is_error=True 的 ToolResult 不抛异常）
- 一轮多个 tool_calls（并行解析，串行执行）
- 幂等：相同 tool_call.id 二次 append 不重复插入
- T01 单轮文本对话回归（无 tool_executor 仍可跑）
- transition 事件 next_turn reason
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    LLMResponse,
    Message,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolCall,
    ToolExecutor,
    ToolResult,
)
from client.core.agent.types import (  # noqa: E402
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_PENDING,
    TOOL_STATUS_RUNNING,
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


def _make_openai_tool_call(
    call_id: str = "call_test_1", name: str = "list_dir",
    args: dict | None = None, args_raw: str | None = None,
) -> dict:
    """构造 OpenAI 格式的 tool_call dict。"""
    if args_raw is None:
        import json as _json
        args_raw = _json.dumps(args or {"path": "."})
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args_raw},
    }


async def _seed_session(store: EventStore, user_text: str, session_id: str = "t02-sess") -> None:
    """创建会话 + 写入一条 user 消息。"""
    await store.create_session(session_id=session_id, title="T02 test")
    await store.append_message(
        session_id, Message(role="user", content=user_text, source="user")
    )


# ============================================================================
# 1. 协议检查
# ============================================================================


def test_mock_tool_executor_implements_protocol():
    """T02: MockToolExecutor 实现 ToolExecutor 协议（runtime_checkable）。"""
    ex = MockToolExecutor(default_result="ok")
    assert isinstance(ex, ToolExecutor)


# ============================================================================
# 2. ToolCall / OpenAI 格式解析
# ============================================================================


def test_tool_call_from_openai_dict_parses_args():
    """T02: ToolCall.from_openai_tool_call 正确解析 OpenAI 格式。"""
    tc = _make_openai_tool_call(call_id="c1", name="list_dir", args={"path": "/tmp"})
    parsed = ToolCall.from_openai_tool_call(tc, session_id="s1", seq=5)
    assert parsed.id == "c1"
    assert parsed.name == "list_dir"
    assert parsed.args == {"path": "/tmp"}
    assert parsed.args_raw == '{"path": "/tmp"}'
    assert parsed.session_id == "s1"
    assert parsed.seq == 5
    assert parsed.status == TOOL_STATUS_PENDING


def test_tool_call_from_openai_dict_handles_bad_json():
    """T02: 坏 JSON tool args 不抛异常（v6-lite §6 故障注入之一）。"""
    tc = _make_openai_tool_call(args_raw="{not valid json")
    parsed = ToolCall.from_openai_tool_call(tc)
    assert parsed.args == {}  # 坏 JSON 时 args 留空
    assert parsed.args_raw == "{not valid json"


def test_tool_call_to_db_roundtrip():
    """T02: ToolCall.to_db / from_db 往返一致。"""
    tc = ToolCall(
        id="c1", session_id="s1", seq=3, name="read_file",
        args={"path": "/a/b"}, safety="read_only", status="completed",
        started_at=100.0, ended_at=101.0, trace_id="trace-1",
    )
    db_row = tc.to_db()
    restored = ToolCall.from_db(db_row)
    assert restored.id == tc.id
    assert restored.session_id == tc.session_id
    assert restored.seq == tc.seq
    assert restored.name == tc.name
    assert restored.args == tc.args
    assert restored.safety == tc.safety
    assert restored.status == tc.status
    assert restored.started_at == tc.started_at
    assert restored.ended_at == tc.ended_at
    assert restored.trace_id == tc.trace_id


# ============================================================================
# 3. EventStore tool_calls CRUD
# ============================================================================


def test_event_store_has_tool_calls_table(tmp_db_path):
    """T02: tool_calls 表已建。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    try:
        tables = {
            r[0] for r in s.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tool_calls" in tables
        # 列检查
        cols = {r[1] for r in s.conn.execute("PRAGMA table_info(tool_calls)").fetchall()}
        expected = {"id", "session_id", "seq", "name", "args_json",
                    "safety", "status", "started_at", "ended_at", "trace_id"}
        assert expected <= cols
    finally:
        s.close()


def test_append_tool_call_writes_pending_event(store):
    """T02 acceptance: tool_calls 表先写 pending 再执行（v6-lite §4.10）。"""
    async def run():
        await store.create_session(session_id="s1")
        tc = ToolCall(id="c1", session_id="s1", seq=1, name="list_dir", args={"path": "."})
        result = await store.append_tool_call("s1", tc)
        assert result.status == TOOL_STATUS_PENDING
        assert result.session_id == "s1"
        assert result.seq == 1
        # DB 验证
        rows = await store.load_tool_calls("s1")
        assert len(rows) == 1
        assert rows[0].status == TOOL_STATUS_PENDING
        # event 验证
        events = await store.load_events("s1")
        pendings = [e for e in events if e.type == "tool_call_pending"]
        assert len(pendings) == 1
        assert pendings[0].payload["tool_call_id"] == "c1"
        assert pendings[0].payload["name"] == "list_dir"
    asyncio.run(run())


def test_append_tool_call_is_idempotent(store):
    """T02: tool_call.id 幂等（重启恢复时不重复执行已完成调用，v6-01 §1.2）。"""
    async def run():
        await store.create_session(session_id="s1")
        tc = ToolCall(id="c1", session_id="s1", seq=1, name="list_dir", args={"path": "."})
        await store.append_tool_call("s1", tc)
        # 第二次 append 同 id → 返回已有记录，不重复插入
        await store.append_tool_call("s1", tc)
        rows = await store.load_tool_calls("s1")
        assert len(rows) == 1
    asyncio.run(run())


def test_update_tool_call_status_writes_event(store):
    """T02: 状态流转（pending → running → completed）写 event。"""
    async def run():
        await store.create_session(session_id="s1")
        tc = ToolCall(id="c1", session_id="s1", seq=1, name="list_dir")
        await store.append_tool_call("s1", tc)

        await store.update_tool_call_status("c1", TOOL_STATUS_RUNNING, started_at=100.0)
        await store.update_tool_call_status("c1", TOOL_STATUS_COMPLETED, ended_at=101.0)

        rows = await store.load_tool_calls("s1")
        assert rows[0].status == TOOL_STATUS_COMPLETED
        assert rows[0].started_at == 100.0
        assert rows[0].ended_at == 101.0
        events = await store.load_events("s1")
        changes = [e for e in events if e.type == "tool_call_status_changed"]
        assert len(changes) == 2
        assert changes[0].payload["status"] == TOOL_STATUS_RUNNING
        assert changes[1].payload["status"] == TOOL_STATUS_COMPLETED
    asyncio.run(run())


def test_append_tool_result_message_writes_role_tool(store):
    """T02 acceptance: 工具结果作为 tool_result 消息回灌到 messages 表。"""
    async def run():
        await store.create_session(session_id="s1")
        result = ToolResult(
            tool_call_id="c1", content="file.txt\nreadme.md",
            is_error=False,
        )
        msg = await store.append_tool_result_message("s1", result)
        assert msg.role == "tool"
        assert msg.source == "tool_result"
        assert msg.tool_call_id == "c1"
        assert "file.txt" in msg.content

        # DB 验证
        messages = await store.load_messages("s1")
        assert len(messages) == 1
        assert messages[0].role == "tool"
        assert messages[0].tool_call_id == "c1"

        # event 验证
        events = await store.load_events("s1")
        results = [e for e in events if e.type == "tool_result_appended"]
        assert len(results) == 1
        assert results[0].payload["tool_call_id"] == "c1"
        assert results[0].payload["is_error"] is False
    asyncio.run(run())


# ============================================================================
# 4. MockToolExecutor 模式
# ============================================================================


def test_mock_tool_executor_default_result():
    """T02: MockToolExecutor default_result 模式。"""
    async def run():
        ex = MockToolExecutor(default_result="ok")
        tc = ToolCall(id="c1", name="noop")
        r = await ex.execute(tc)
        assert r.tool_call_id == "c1"
        assert r.content == "ok"
        assert r.is_error is False
        assert ex.call_count == 1
    asyncio.run(run())


def test_mock_tool_executor_fixtures_by_id_and_name():
    """T02: MockToolExecutor fixtures 按 id 或 name 匹配。"""
    async def run():
        ex = MockToolExecutor(fixtures={
            "c1": "by-id-result",
            "list_dir": ToolResult(tool_call_id="", content="dir-listing"),
        })
        # by id
        r1 = await ex.execute(ToolCall(id="c1", name="anything"))
        assert r1.content == "by-id-result"
        # by name（id 不在 fixtures，按 name 匹配）
        r2 = await ex.execute(ToolCall(id="c2", name="list_dir"))
        assert r2.content == "dir-listing"
    asyncio.run(run())


def test_mock_tool_executor_callable():
    """T02: MockToolExecutor callable 模式（动态生成）。"""
    async def run():
        ex = MockToolExecutor(callable=lambda tc: ToolResult(
            tool_call_id=tc.id,
            content=f"executed {tc.name}({tc.args})",
        ))
        r = await ex.execute(ToolCall(id="c1", name="list_dir", args={"path": "/tmp"}))
        assert "list_dir" in r.content
        assert "/tmp" in r.content
    asyncio.run(run())


def test_mock_tool_executor_error_does_not_raise():
    """T02: error-as-output-variant（v6-lite §4.5）—— executor 内部异常转 is_error=True，不抛。"""
    async def run():
        def raise_on_call(tc):
            raise RuntimeError("simulated tool failure")
        ex = MockToolExecutor(callable=raise_on_call)
        r = await ex.execute(ToolCall(id="c1", name="bad_tool"))
        assert r.is_error is True
        assert "simulated tool failure" in r.content
        assert "RuntimeError" in r.content
    asyncio.run(run())


# ============================================================================
# 5. SessionRunner 工具回灌端到端（核心 acceptance）
# ============================================================================


def test_session_runner_tool_call_round_trip(store):
    """T02 acceptance: MockLLM 工具轮：返回 tool_calls → 执行 → 回灌 → 继续循环通过。

    脚本：
    - 第 1 轮：LLM 返回 tool_calls（list_dir）
    - 第 2 轮：LLM 看到 tool_result，返回纯文本（终止）
    """
    async def run():
        await _seed_session(store, "列出当前目录", session_id="t02-round")

        mock_llm = MockLLM(script=[
            LLMResponse(
                content="我来列出当前目录。",
                tool_calls=[_make_openai_tool_call(call_id="c1", name="list_dir", args={"path": "."})],
                stop_reason="tool_use",
                model="mock-llm",
            ),
            LLMResponse(
                content="当前目录包含 file.txt 和 readme.md。",
                stop_reason="end_turn",
                model="mock-llm",
            ),
        ])
        mock_tool = MockToolExecutor(fixtures={
            "c1": "file.txt\nreadme.md",
        })
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t02-round", max_iterations=10),
        )
        outcome = await runner.run()

        # RunOutcome
        assert outcome.status == "completed"
        assert outcome.stop_reason == "end_turn"
        assert outcome.iterations == 2  # 第 1 轮工具 + 第 2 轮文本
        assert mock_llm.call_count == 2
        assert mock_tool.call_count == 1

        # tool_calls 表：状态流转 pending → running → completed
        tool_calls = await store.load_tool_calls("t02-round")
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "c1"
        assert tool_calls[0].name == "list_dir"
        assert tool_calls[0].status == TOOL_STATUS_COMPLETED
        assert tool_calls[0].started_at > 0
        assert tool_calls[0].ended_at >= tool_calls[0].started_at

        # messages 表：user → assistant(tool_calls) → tool(result) → assistant(文本)
        messages = await store.load_messages("t02-round")
        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[0].content == "列出当前目录"
        assert messages[1].role == "assistant"
        assert messages[1].content == "我来列出当前目录。"
        assert len(messages[1].tool_calls) == 1
        assert messages[2].role == "tool"
        assert messages[2].tool_call_id == "c1"
        assert "file.txt" in messages[2].content
        assert messages[3].role == "assistant"
        assert messages[3].content == "当前目录包含 file.txt 和 readme.md。"

        # transition_reason = next_turn
        assert runner.last_transition_reason == TRANSITION_NEXT_TURN
        events = await store.load_events("t02-round")
        transitions = [e for e in events if e.type == "transition"]
        assert len(transitions) == 1
        assert transitions[0].payload["reason"] == TRANSITION_NEXT_TURN
        assert transitions[0].payload["tool_calls_count"] == 1
    asyncio.run(run())


def test_session_runner_pending_before_running_order(store):
    """T02 acceptance: tool_calls 先写 pending 再执行的顺序有测试断言。

    用 events 表的 seq 顺序验证：
    - tool_call_pending 必须在 tool_call_status_changed(running) 之前
    """
    async def run():
        await _seed_session(store, "调工具", session_id="t02-order")
        mock_llm = MockLLM(script=[
            LLMResponse(content="调用中", tool_calls=[_make_openai_tool_call(call_id="c1", name="noop")]),
            LLMResponse(content="完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(default_result="noop-result")
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="t02-order"),
        )
        await runner.run()

        events = await store.load_events("t02-order")
        # 找到 pending 和 running 两个事件的 seq
        pendings = [e for e in events if e.type == "tool_call_pending"]
        runnings = [e for e in events if e.type == "tool_call_status_changed"
                    and e.payload["status"] == TOOL_STATUS_RUNNING]
        assert len(pendings) == 1
        assert len(runnings) == 1
        # 关键断言：pending 的 seq < running 的 seq（顺序正确）
        assert pendings[0].seq < runnings[0].seq, (
            f"pending seq={pendings[0].seq} must be < running seq={runnings[0].seq} "
            "(v6-lite §4.10 副作用前先落 durable record)"
        )
    asyncio.run(run())


def test_session_runner_multiple_tool_calls_in_one_turn(store):
    """T02: 一轮多个 tool_calls（完整解析后再调度，v6-lite §4.1）。"""
    async def run():
        await _seed_session(store, "调两个工具", session_id="t02-multi")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="我来调两个工具。",
                tool_calls=[
                    _make_openai_tool_call(call_id="c1", name="list_dir", args={"path": "/a"}),
                    _make_openai_tool_call(call_id="c2", name="read_file", args={"path": "/b"}),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(content="两个工具都完成了。", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(fixtures={
            "c1": "dir-a-content",
            "c2": "file-b-content",
        })
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="t02-multi"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        assert outcome.iterations == 2

        # 两个 tool_calls 都 completed
        tool_calls = await store.load_tool_calls("t02-multi")
        assert len(tool_calls) == 2
        assert {tc.id for tc in tool_calls} == {"c1", "c2"}
        assert all(tc.status == TOOL_STATUS_COMPLETED for tc in tool_calls)
        assert mock_tool.call_count == 2

        # messages: user → assistant(2 tool_calls) → tool(c1) → tool(c2) → assistant(文本)
        messages = await store.load_messages("t02-multi")
        assert len(messages) == 5
        assert messages[1].role == "assistant"
        assert len(messages[1].tool_calls) == 2
        assert messages[2].role == "tool"
        assert messages[2].tool_call_id == "c1"
        assert messages[3].role == "tool"
        assert messages[3].tool_call_id == "c2"
        assert messages[4].role == "assistant"
    asyncio.run(run())


def test_session_runner_error_tool_result_does_not_crash(store):
    """T02: 工具返回 is_error=True 的结果，runner 正常回灌给模型。"""
    async def run():
        await _seed_session(store, "调会失败的工具", session_id="t02-err")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="调用失败的工具。",
                tool_calls=[_make_openai_tool_call(call_id="c1", name="bad_tool")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="工具失败了，我换种方式。", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(fixtures={
            "c1": ToolResult(tool_call_id="c1", content="connection refused", is_error=True),
        })
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool),
            config=RunnerConfig(session_id="t02-err"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        # tool_call 状态 = failed（is_error=True 时）
        tool_calls = await store.load_tool_calls("t02-err")
        assert tool_calls[0].status == "failed"
        # tool_result 消息可见
        messages = await store.load_messages("t02-err")
        tool_msg = next(m for m in messages if m.role == "tool")
        assert "connection refused" in tool_msg.content
    asyncio.run(run())


def test_session_runner_without_tool_executor_raises_on_tool_calls(store):
    """T02: tool_executor 未注入但 LLM 返回 tool_calls → 抛 RuntimeError（T01 向后兼容边界）。"""
    async def run():
        await _seed_session(store, "调工具", session_id="t02-noexec")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="调工具",
                tool_calls=[_make_openai_tool_call(call_id="c1", name="noop")],
            ),
        ])
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=mock_llm, event_store=store, tool_executor=None),
            config=RunnerConfig(session_id="t02-noexec"),
        )
        with pytest.raises(RuntimeError, match="tool_executor is None"):
            await runner.run()
    asyncio.run(run())


# ============================================================================
# 6. T01 回归（无 tool_executor 仍可跑单轮文本对话）
# ============================================================================


def test_t01_regression_single_turn_without_tool_executor(store):
    """T02 回归: T01 单轮文本对话不需要 tool_executor（向后兼容）。"""
    async def run():
        await _seed_session(store, "你好", session_id="t01-regress")
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=MockLLM(default_text="你好！"),
                event_store=store,
                tool_executor=None,  # 显式 None
            ),
            config=RunnerConfig(session_id="t01-regress"),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"
        assert outcome.iterations == 1
    asyncio.run(run())


# ============================================================================
# 7. 持久化 + 回放
# ============================================================================


def test_tool_calls_persisted_across_reopen(tmp_db_path):
    """T02: tool_calls 表持久化（重开 DB 仍可读取）。"""
    s1 = EventStore(db_path=tmp_db_path)
    s1.init()
    async def write():
        await s1.create_session(session_id="persist-1")
        tc = ToolCall(id="c1", session_id="persist-1", seq=1, name="list_dir", args={"path": "."})
        await s1.append_tool_call("persist-1", tc)
        await s1.update_tool_call_status("c1", TOOL_STATUS_COMPLETED, started_at=1.0, ended_at=2.0)
        result = ToolResult(tool_call_id="c1", content="file.txt")
        await s1.append_tool_result_message("persist-1", result)
    asyncio.run(write())
    s1.close()

    s2 = EventStore(db_path=tmp_db_path)
    s2.init()
    async def read():
        tool_calls = await s2.load_tool_calls("persist-1")
        assert len(tool_calls) == 1
        assert tool_calls[0].status == TOOL_STATUS_COMPLETED
        assert tool_calls[0].started_at == 1.0
        messages = await s2.load_messages("persist-1")
        assert len(messages) == 1
        assert messages[0].role == "tool"
        assert messages[0].content == "file.txt"
    asyncio.run(read())
    s2.close()
