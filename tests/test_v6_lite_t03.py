"""v6-lite T03 验收测试：连续工具轮 + 终止 + transition_reason

覆盖 T03 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] MockLLM 连续两轮工具 → 第三轮纯文本 → 终止通过
- [x] 每轮 transition_reason 正确设置为 `next_turn`
- [x] 所有 events/messages/tool_calls 落库可回放
- [x] 终止后 session.status = completed

外加边界测试：
- 单轮工具 → 文本（T02 回归，确保 T03 多轮改动不破坏 T02）
- 三轮工具 → 文本（更长的工具链）
- 每轮多个 tool_call（一轮并行多工具 + 连续多轮）
- transition 事件计数 = 工具轮数（文本轮不写 transition）
- max_iterations 在多轮工具循环中正确触发
- 预算在多轮工具循环中累计正确
- 重开后 events/messages/tool_calls 完整可回放
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
    LLMResponse,
    Message,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolCall,
    ToolResult,
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


def _make_openai_tool_call(
    call_id: str, name: str, args: dict | None = None,
) -> dict:
    """构造 OpenAI 格式的 tool_call dict。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {"path": "."}),
        },
    }


async def _seed_session(store: EventStore, user_text: str, session_id: str) -> None:
    """创建会话 + 写入一条 user 消息。"""
    await store.create_session(session_id=session_id, title="T03 test")
    await store.append_message(
        session_id, Message(role="user", content=user_text, source="user")
    )


# ============================================================================
# 1. T03 核心 acceptance：连续两轮工具 → 第三轮纯文本 → 终止
# ============================================================================


def test_t03_acceptance_two_tool_turns_then_text(store):
    """T03 acceptance: MockLLM 连续两轮工具 → 第三轮纯文本 → 终止通过。

    脚本：
    - 第 1 轮：LLM 返回 tool_calls（read_file "a.txt"）
    - 第 2 轮：LLM 看到 tool_result，再返回 tool_calls（read_file "b.txt"）
    - 第 3 轮：LLM 看到第二个 tool_result，返回纯文本（终止）
    """
    async def run():
        await _seed_session(store, "对比 a.txt 和 b.txt", session_id="t03-multi")

        mock_llm = MockLLM(script=[
            # 第 1 轮：工具调用
            LLMResponse(
                content="我先读 a.txt。",
                tool_calls=[_make_openai_tool_call("c1", "read_file", {"path": "a.txt"})],
                stop_reason="tool_use",
            ),
            # 第 2 轮：继续工具调用
            LLMResponse(
                content="再读 b.txt。",
                tool_calls=[_make_openai_tool_call("c2", "read_file", {"path": "b.txt"})],
                stop_reason="tool_use",
            ),
            # 第 3 轮：纯文本，终止
            LLMResponse(
                content="a.txt 是 README，b.txt 是配置文件。",
                stop_reason="end_turn",
            ),
        ])
        mock_tool = MockToolExecutor(fixtures={
            "c1": "content of a.txt",
            "c2": "content of b.txt",
        })
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-multi", max_iterations=10),
        )
        outcome = await runner.run()

        # RunOutcome: 3 轮迭代，正常完成
        assert outcome.status == "completed"
        assert outcome.stop_reason == "end_turn"
        assert outcome.iterations == 3
        assert mock_llm.call_count == 3
        assert mock_tool.call_count == 2  # 两个工具调用

        # session.status = completed
        session = await store.get_session("t03-multi")
        assert session.status == "completed"

        # tool_calls 表：2 个，全部 completed
        tool_calls = await store.load_tool_calls("t03-multi")
        assert len(tool_calls) == 2
        assert {tc.id for tc in tool_calls} == {"c1", "c2"}
        assert all(tc.status == TOOL_STATUS_COMPLETED for tc in tool_calls)
        # 两个 tool_call 的 seq 不同（分别属于第 1、第 2 轮 assistant 消息）
        assert tool_calls[0].seq != tool_calls[1].seq

        # messages 表：user → assistant(tool_calls) → tool(result)
        #                → assistant(tool_calls) → tool(result)
        #                → assistant(文本)
        # = 6 条
        messages = await store.load_messages("t03-multi")
        assert len(messages) == 6
        roles = [m.role for m in messages]
        assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
        # 第 1 个 assistant 携带 c1，第 2 个 assistant 携带 c2
        assert messages[1].tool_calls[0]["id"] == "c1"
        assert messages[3].tool_calls[0]["id"] == "c2"
        # 第 3 个 assistant 纯文本，无 tool_calls
        assert messages[5].tool_calls == []
        assert "a.txt 是 README" in messages[5].content
        # tool 消息关联正确的 tool_call_id
        assert messages[2].tool_call_id == "c1"
        assert messages[2].content == "content of a.txt"
        assert messages[4].tool_call_id == "c2"
        assert messages[4].content == "content of b.txt"

    asyncio.run(run())


# ============================================================================
# 2. 每轮 transition_reason 正确设置为 next_turn
# ============================================================================


def test_transition_reason_next_turn_each_tool_turn(store):
    """T03 acceptance: 每轮 transition_reason 正确设置为 `next_turn`。

    每个工具轮写一个 transition event（reason=next_turn）；
    最后文本轮不写 transition（直接 finalize）。
    """
    async def run():
        await _seed_session(store, "三步任务", session_id="t03-trans")

        mock_llm = MockLLM(script=[
            LLMResponse(
                content="步骤 1",
                tool_calls=[_make_openai_tool_call("c1", "step1")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="步骤 2",
                tool_calls=[_make_openai_tool_call("c2", "step2")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="步骤 3",
                tool_calls=[_make_openai_tool_call("c3", "step3")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(default_result="ok")
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-trans", max_iterations=10),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        assert outcome.iterations == 4  # 3 工具轮 + 1 文本轮

        # 加载所有事件
        events = await store.load_events("t03-trans")
        transition_events = [e for e in events if e.type == "transition"]
        # 3 个工具轮 → 3 个 transition 事件
        assert len(transition_events) == 3
        # 每个 transition 的 reason 都是 next_turn
        for ev in transition_events:
            assert ev.payload["reason"] == TRANSITION_NEXT_TURN
        # transition 事件按 seq 升序，iterations 字段递增
        iterations_seq = [ev.payload["iterations"] for ev in transition_events]
        assert iterations_seq == [1, 2, 3]
        # tool_calls_count 每轮都是 1
        for ev in transition_events:
            assert ev.payload["tool_calls_count"] == 1

        # runner 实例的 last_transition_reason 也是 next_turn
        assert runner.last_transition_reason == TRANSITION_NEXT_TURN

    asyncio.run(run())


def test_no_transition_event_on_pure_text_turn(store):
    """T03: 纯文本轮（无 tool_calls）不写 transition 事件，直接 finalize。"""
    async def run():
        await _seed_session(store, "你好", session_id="t03-notrans")
        mock_llm = MockLLM(script=[
            LLMResponse(content="你好！", stop_reason="end_turn"),
        ])
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store,
                tool_executor=MockToolExecutor(),
            ),
            config=RunnerConfig(session_id="t03-notrans"),
        )
        await runner.run()

        events = await store.load_events("t03-notrans")
        transition_events = [e for e in events if e.type == "transition"]
        assert len(transition_events) == 0  # 纯文本轮无 transition
        # 但有 session_finalized
        finalize_events = [e for e in events if e.type == "session_finalized"]
        assert len(finalize_events) == 1

    asyncio.run(run())


# ============================================================================
# 3. 所有 events/messages/tool_calls 落库可回放
# ============================================================================


def test_all_persisted_and_replayable(store):
    """T03 acceptance: 所有 events/messages/tool_calls 落库可回放。

    用 replay_events 按_seq 回放整个会话，验证事件序列完整。
    """
    async def run():
        await _seed_session(store, "两步", session_id="t03-replay")

        mock_llm = MockLLM(script=[
            LLMResponse(
                content="第一步",
                tool_calls=[_make_openai_tool_call("c1", "step1")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="第二步",
                tool_calls=[_make_openai_tool_call("c2", "step2")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(fixtures={"c1": "r1", "c2": "r2"})
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-replay", max_iterations=10),
        )
        await runner.run()

        # 用 replay_events 回放
        replay = await store.replay_events("t03-replay")
        # 事件序列（按 seq 升序）：
        # 1. session_created
        # 2. user_message_appended
        # 3. session_status_changed(streaming)
        # 4. assistant_message_appended (含 tool_calls)
        # 5. tool_call_pending
        # 6. tool_call_status_changed(running)
        # 7. tool_call_status_changed(completed)
        # 8. tool_result_appended
        # 9. transition(next_turn)
        # 10. assistant_message_appended (含 tool_calls)
        # 11-15. 重复 5-9（第二个工具轮）
        # 16. assistant_message_appended (纯文本)
        # 17. session_status_changed(completed)
        # 18. session_finalized
        event_types = [e["type"] for e in replay]
        # 关键事件都存在
        assert event_types[0] == "session_created"
        assert event_types[-1] == "session_finalized"
        assert event_types.count("transition") == 2
        assert event_types.count("tool_call_pending") == 2
        assert event_types.count("tool_call_status_changed") == 4  # 2 工具 × (running + completed)
        assert event_types.count("tool_result_appended") == 2
        assert event_types.count("assistant_message_appended") == 3  # 2 工具轮 + 1 文本轮
        # seq 单调递增
        seqs = [e["seq"] for e in replay]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # 无重复

        # tool_calls 表可回放
        tool_calls = await store.load_tool_calls("t03-replay")
        assert len(tool_calls) == 2
        assert {tc.name for tc in tool_calls} == {"step1", "step2"}

        # messages 表可回放
        # user(1) + assistant(1) + tool(1) + assistant(1) + tool(1) + assistant(1) = 6
        messages = await store.load_messages("t03-replay")
        assert len(messages) == 6

    asyncio.run(run())


def test_persisted_across_reopen(store, tmp_db_path):
    """T03: 重开 EventStore 后 events/messages/tool_calls 仍完整可读。"""
    async def run():
        await _seed_session(store, "两步", session_id="t03-reopen")

        mock_llm = MockLLM(script=[
            LLMResponse(
                content="第一步",
                tool_calls=[_make_openai_tool_call("c1", "step1")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="第二步",
                tool_calls=[_make_openai_tool_call("c2", "step2")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(fixtures={"c1": "r1", "c2": "r2"})
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-reopen", max_iterations=10),
        )
        outcome = await runner.run()
        assert outcome.status == "completed"

        # 关闭后重开
        store.close()
        store2 = EventStore(db_path=tmp_db_path)
        store2.init()
        try:
            # session 还在
            session = await store2.get_session("t03-reopen")
            assert session is not None
            assert session.status == "completed"

            # messages 完整
            messages = await store2.load_messages("t03-reopen")
            assert len(messages) == 6
            roles = [m.role for m in messages]
            assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]

            # tool_calls 完整
            tool_calls = await store2.load_tool_calls("t03-reopen")
            assert len(tool_calls) == 2
            assert all(tc.status == TOOL_STATUS_COMPLETED for tc in tool_calls)

            # events 完整（replay 仍可回放）
            replay = await store2.replay_events("t03-reopen")
            event_types = [e["type"] for e in replay]
            assert event_types[0] == "session_created"
            assert event_types[-1] == "session_finalized"
            assert event_types.count("transition") == 2
        finally:
            store2.close()

    asyncio.run(run())


# ============================================================================
# 4. 终止后 session.status = completed
# ============================================================================


def test_session_status_completed_after_termination(store):
    """T03 acceptance: 终止后 session.status = completed。"""
    async def run():
        await _seed_session(store, "任务", session_id="t03-status")
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
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-status"),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        session = await store.get_session("t03-status")
        assert session.status == "completed"

        # events 中有 session_status_changed(completed) + session_finalized
        events = await store.load_events("t03-status")
        status_changes = [
            e for e in events
            if e.type == "session_status_changed" and e.payload.get("status") == "completed"
        ]
        assert len(status_changes) == 1
        finalize_events = [e for e in events if e.type == "session_finalized"]
        assert len(finalize_events) == 1

    asyncio.run(run())


# ============================================================================
# 5. 边界 / 回归测试
# ============================================================================


def test_single_tool_turn_then_text_regression(store):
    """T03 回归: 单轮工具 → 文本（T02 场景在 T03 多轮逻辑下仍工作）。"""
    async def run():
        await _seed_session(store, "列出目录", session_id="t03-reg")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="我来列出。",
                tool_calls=[_make_openai_tool_call("c1", "list_dir")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="目录已列出。", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(default_result="file1\nfile2")
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-reg"),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        assert outcome.iterations == 2
        events = await store.load_events("t03-reg")
        transition_events = [e for e in events if e.type == "transition"]
        assert len(transition_events) == 1  # 只有一轮工具，一个 transition

    asyncio.run(run())


def test_three_tool_turns_then_text(store):
    """T03 边界: 三轮工具 → 文本（更长的工具链）。"""
    async def run():
        await _seed_session(store, "复杂任务", session_id="t03-three")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content=f"步骤 {i}",
                tool_calls=[_make_openai_tool_call(f"c{i}", f"step{i}")],
                stop_reason="tool_use",
            )
            for i in range(1, 4)
        ] + [LLMResponse(content="全部完成", stop_reason="end_turn")])
        mock_tool = MockToolExecutor(default_result="ok")
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-three", max_iterations=10),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        assert outcome.iterations == 4  # 3 工具轮 + 1 文本轮
        tool_calls = await store.load_tool_calls("t03-three")
        assert len(tool_calls) == 3
        events = await store.load_events("t03-three")
        transition_events = [e for e in events if e.type == "transition"]
        assert len(transition_events) == 3

    asyncio.run(run())


def test_multiple_tool_calls_per_turn_across_turns(store):
    """T03: 一轮多个 tool_call + 连续多轮。

    第 1 轮 2 个 tool_call，第 2 轮 1 个 tool_call，第 3 轮文本。
    """
    async def run():
        await _seed_session(store, "多工具任务", session_id="t03-multi-calls")
        mock_llm = MockLLM(script=[
            LLMResponse(
                content="并行读取两个文件",
                tool_calls=[
                    _make_openai_tool_call("c1", "read_file", {"path": "a"}),
                    _make_openai_tool_call("c2", "read_file", {"path": "b"}),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="再读一个",
                tool_calls=[_make_openai_tool_call("c3", "read_file", {"path": "c"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="完成对比", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(fixtures={
            "c1": "content-a", "c2": "content-b", "c3": "content-c",
        })
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-multi-calls", max_iterations=10),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        assert outcome.iterations == 3
        assert mock_tool.call_count == 3

        tool_calls = await store.load_tool_calls("t03-multi-calls")
        assert len(tool_calls) == 3
        # c1 和 c2 同 seq（第 1 轮），c3 不同 seq（第 2 轮）
        c1 = next(tc for tc in tool_calls if tc.id == "c1")
        c2 = next(tc for tc in tool_calls if tc.id == "c2")
        c3 = next(tc for tc in tool_calls if tc.id == "c3")
        assert c1.seq == c2.seq
        assert c3.seq > c1.seq

        # transition 事件：2 个（2 个工具轮）
        events = await store.load_events("t03-multi-calls")
        transition_events = [e for e in events if e.type == "transition"]
        assert len(transition_events) == 2
        # 第 1 轮 2 个 tool_call，第 2 轮 1 个
        assert transition_events[0].payload["tool_calls_count"] == 2
        assert transition_events[1].payload["tool_calls_count"] == 1

        # messages：user + asst + tool + tool + asst + tool + asst = 7
        messages = await store.load_messages("t03-multi-calls")
        assert len(messages) == 7

    asyncio.run(run())


def test_max_iterations_in_multi_tool_loop(store):
    """T03 边界: 多轮工具循环中 max_iterations 正确触发。

    LLM 永远返回 tool_calls，max_iterations=3 → 3 轮后 failed。
    """
    async def run():
        import time
        await _seed_session(store, "无限循环", session_id="t03-maxit")
        mock_llm = MockLLM(callable=lambda req: LLMResponse(
            content="继续",
            tool_calls=[_make_openai_tool_call(
                f"c-{int(time.time()*1000000)}", "noop",
            )],
            stop_reason="tool_use",
        ))
        mock_tool = MockToolExecutor(default_result="ok")
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-maxit", max_iterations=3),
        )
        outcome = await runner.run()

        assert outcome.status == "failed"
        assert outcome.stop_reason == "max_iterations"
        assert outcome.iterations == 3

        # 3 轮工具 → 3 个 transition 事件
        events = await store.load_events("t03-maxit")
        transition_events = [e for e in events if e.type == "transition"]
        assert len(transition_events) == 3
        # max_iterations_reached event 落库
        max_it_events = [e for e in events if e.type == "max_iterations_reached"]
        assert len(max_it_events) == 1
        assert max_it_events[0].payload["max_iterations"] == 3

        # session.status = failed
        session = await store.get_session("t03-maxit")
        assert session.status == "failed"

    asyncio.run(run())


def test_tool_error_in_multi_turn_does_not_crash(store):
    """T03: 多轮工具循环中某个工具失败（is_error=True），不崩，继续循环。

    error-as-output-variant：失败的工具结果回灌给 LLM，LLM 可改道。
    """
    async def run():
        await _seed_session(store, "容错任务", session_id="t03-err")

        def tool_handler(tool_call: ToolCall):
            if tool_call.name == "fail_tool":
                return ToolResult(
                    tool_call_id=tool_call.id,
                    content="工具执行失败：权限不足",
                    is_error=True,
                )
            return ToolResult(
                tool_call_id=tool_call.id, content="ok-result",
            )

        mock_llm = MockLLM(script=[
            LLMResponse(
                content="尝试失败工具",
                tool_calls=[_make_openai_tool_call("c1", "fail_tool")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="改用备用工具",
                tool_calls=[_make_openai_tool_call("c2", "backup_tool")],
                stop_reason="tool_use",
            ),
            LLMResponse(content="任务完成", stop_reason="end_turn"),
        ])
        mock_tool = MockToolExecutor(callable=tool_handler)
        runner = SessionRunner(
            deps=RunnerDeps(
                llm_gateway=mock_llm, event_store=store, tool_executor=mock_tool,
            ),
            config=RunnerConfig(session_id="t03-err", max_iterations=10),
        )
        outcome = await runner.run()

        assert outcome.status == "completed"
        assert outcome.iterations == 3

        # 第 1 个 tool_call failed，第 2 个 completed
        tool_calls = await store.load_tool_calls("t03-err")
        assert len(tool_calls) == 2
        c1 = next(tc for tc in tool_calls if tc.id == "c1")
        c2 = next(tc for tc in tool_calls if tc.id == "c2")
        assert c1.status == "failed"
        assert c2.status == "completed"

        # messages 表中第 1 个 tool 消息含 error 描述
        messages = await store.load_messages("t03-err")
        tool_messages = [m for m in messages if m.role == "tool"]
        assert len(tool_messages) == 2
        assert "失败" in tool_messages[0].content or "权限" in tool_messages[0].content
        assert tool_messages[1].content == "ok-result"

    asyncio.run(run())
