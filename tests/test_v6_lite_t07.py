"""v6-lite T07 验收测试：W4 ChatPanel 最小可用 + 防死循环

覆盖 T07 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] 防死循环标志位存在且默认 False（v6-02 §2.1）
- [x] retry 时标志位不重置（has_attempted_reactive_compact / has_attempted_nudge）
- [x] EventStore.list_sessions() 正确返回所有会话（ChatPanel 依赖）
- [x] synthetic 消息 visible=false 不被 load_messages 默认返回（面板不渲染）
- [x] maxBudgetUsd 循环内检查（v6-lite §4.4）
- [x] ChatPanel 类可导入 + PANEL_META 正确（自动发现元数据）
- [x] transition_reason 4 种支持（next_turn / reactive_compact_retry / verification_nudge / user_interrupted）

测试策略：
- 标志位与 EventStore 行为用纯 Python 测试（无 Qt 依赖）
- ChatPanel 仅做导入与元数据校验（Qt 部分需 GUI 环境，留给 dogfood 验证）
- 不接真实 server（用 MockLLM + MockToolExecutor）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

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
from client.core.agent.types import (  # noqa: E402
    TRANSITION_NEXT_TURN,
    TRANSITION_REACTIVE_COMPACT_RETRY,
    TRANSITION_USER_INTERRUPTED,
    TRANSITION_VERIFICATION_NUDGE,
    LLMResponse,
    Message,
)

# ============================================================================
# Helpers
# ============================================================================


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "agent_t07.db"))
    store.init()
    return store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# T07-A: 防死循环标志位（v6-02 §2.1）
# ============================================================================


class TestIdleLoopPreventionFlags:
    """has_attempted_reactive_compact / has_attempted_nudge 标志位（v6-02 §2.1）。

    硬规则：retry 时不重置（防止 compact + nudge 互触发死循环，
    cc_src 真实踩坑 "burning thousands of API calls"）。
    """

    def test_flags_exist_and_default_false(self):
        """标志位存在且默认 False。"""
        store = EventStore(":memory:")
        store.init()
        try:
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hi"),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="x"),
            )
            assert hasattr(runner, "has_attempted_reactive_compact")
            assert hasattr(runner, "has_attempted_nudge")
            assert runner.has_attempted_reactive_compact is False
            assert runner.has_attempted_nudge is False
        finally:
            store.close()

    def test_flags_persist_across_retry_simulation(self, tmp_path):
        """模拟 retry 场景：标志位被 set 后不应被 run() 重置。

        场景：构造一次 run，run 内手工 set 标志位模拟 reactive_compact 已尝试，
        下一次 run() 入口不应清零（防死循环硬规则）。
        """
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-retry", title="retry test"))
            _run(store.append_message(
                "sess-retry", Message(role="user", content="hi", source="user"),
            ))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hello"),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-retry"),
            )
            # 模拟已经尝试过 reactive_compact（上次 run 因 413 触发了压缩重试）
            runner.has_attempted_reactive_compact = True
            runner.has_attempted_nudge = True

            outcome = _run(runner.run())

            # run() 完成后标志位应保留 True（不被 run 入口重置）
            assert runner.has_attempted_reactive_compact is True, (
                "has_attempted_reactive_compact must NOT be reset on run() entry "
                "(v6-02 §2.1 防死循环硬规则)"
            )
            assert runner.has_attempted_nudge is True, (
                "has_attempted_nudge must NOT be reset on run() entry "
                "(v6-02 §2.1 防死循环硬规则)"
            )
            assert outcome.status == "completed"
        finally:
            store.close()

    def test_transition_reason_constants_exist(self):
        """4 种 transition_reason 常量存在（v6-02 §2，T07 范围）。"""
        assert TRANSITION_NEXT_TURN == "next_turn"
        assert TRANSITION_REACTIVE_COMPACT_RETRY == "reactive_compact_retry"
        assert TRANSITION_VERIFICATION_NUDGE == "verification_nudge"
        assert TRANSITION_USER_INTERRUPTED == "user_interrupted"


# ============================================================================
# T07-B: EventStore.list_sessions() — ChatPanel 左侧列表依赖
# ============================================================================


class TestListSessions:
    """list_sessions() 是 T07 ChatPanel 新增的依赖（左侧会话列表）。"""

    def test_list_sessions_returns_all_sessions(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1", title="first"))
            _run(store.create_session(session_id="sess-2", title="second"))
            _run(store.create_session(session_id="sess-3", title="third"))

            sessions = _run(store.list_sessions())
            assert len(sessions) == 3
            ids = {s.id for s in sessions}
            assert ids == {"sess-1", "sess-2", "sess-3"}
        finally:
            store.close()

    def test_list_sessions_empty_db(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            sessions = _run(store.list_sessions())
            assert sessions == []
        finally:
            store.close()

    def test_list_sessions_ordered_by_updated_at_desc(self, tmp_path):
        """会话按 updated_at 倒序（最近活动的在前，ChatPanel 列表展示用）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="old", title="old"))
            _run(store.create_session(session_id="new", title="new"))
            # 更新 old 的 updated_at（模拟最近活动）
            _run(store.update_session_status("old", "completed"))

            sessions = _run(store.list_sessions())
            # old 最近被更新，应排第一
            assert sessions[0].id == "old"
            assert sessions[1].id == "new"
        finally:
            store.close()


# ============================================================================
# T07-C: synthetic 消息 visible=false（v6-lite §4.6）
# ============================================================================


class TestSyntheticMessageVisibility:
    """synthetic 消息 visible=false，load_messages 默认不返回（面板不渲染）。"""

    def test_synthetic_message_not_in_default_load(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-s"))
            _run(store.append_message(
                "sess-s", Message(role="user", content="hi", source="user"),
            ))
            _run(store.append_message(
                "sess-s",
                Message(
                    role="user",
                    content="verification_required",
                    source="synthetic",
                    visible=False,
                ),
            ))
            _run(store.append_message(
                "sess-s", Message(role="assistant", content="hello", source="assistant"),
            ))

            # 默认 load_messages 跳过 visible=false
            visible = _run(store.load_messages("sess-s"))
            assert len(visible) == 2  # user + assistant，跳过 synthetic
            assert all(m.visible for m in visible)

            # include_invisible=True 返回全部
            all_msgs = _run(store.load_messages("sess-s", include_invisible=True))
            assert len(all_msgs) == 3
            sources = {m.source for m in all_msgs}
            assert "synthetic" in sources
        finally:
            store.close()

    def test_synthetic_message_persisted_with_visible_false(self, tmp_path):
        """synthetic 消息落库后 visible=0（重启后仍不渲染）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-p"))
            _run(store.append_message(
                "sess-p",
                Message(role="user", content="nudge", source="synthetic", visible=False),
            ))

            # 重开 store 模拟重启
            store.close()
            store2 = EventStore(db_path=str(tmp_path / "agent_t07.db"))
            store2.init()
            try:
                visible = _run(store2.load_messages("sess-p"))
                assert len(visible) == 0  # synthetic 仍被过滤
                all_msgs = _run(store2.load_messages("sess-p", include_invisible=True))
                assert len(all_msgs) == 1
                assert all_msgs[0].source == "synthetic"
                assert all_msgs[0].visible is False
            finally:
                store2.close()
        finally:
            # ADR-0021：EventStore 重构后用 _init_conn（替代旧 _conn），close() 幂等
            if store._init_conn is not None:
                store.close()


# ============================================================================
# T07-D: maxBudgetUsd 已删除（B2/spec D2/D3）
# 原 TestMaxBudgetUsdGuard 已删除：budget_exceeded 路径已移除，
# 改用 total_tokens 记账。test_v6_lite_t01.py::test_budget_exceeded_guard_removed 验证。
# ============================================================================


# ============================================================================
# T07-E: ChatPanel 导入与元数据（不依赖 Qt 运行环境）
# ============================================================================


class TestChatPanelImport:
    """ChatPanel 类可导入 + PANEL_META 元数据正确（自动发现所需）。

    完整 GUI 交互测试需 Qt 显示环境，留给 dogfood 阶段手动验证。
    """

    def test_chat_panel_module_importable(self):
        """chat.py 模块可导入（无 import error）。"""
        try:
            from client.panels import chat  # noqa: F401
        except ImportError as e:
            # PySide6 不在环境里时跳过（CI 可能无 Qt）
            if "PySide6" in str(e):
                pytest.skip("PySide6 not installed")
            raise

    def test_chat_panel_meta_correct(self):
        try:
            from client.panels.chat import ChatPanel
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.skip("PySide6 not installed")
            raise

        meta = ChatPanel.PANEL_META
        assert meta.id == "chat"
        assert meta.title == "对话"
        assert meta.category == "main"
        assert meta.requires_backend is True
        assert meta.requires_agent is True
        # order 应该靠前（主面板）
        assert meta.order < 50

    def test_chat_panel_is_panel_base_subclass(self):
        try:
            from client.core.panel_base import PanelBase
            from client.panels.chat import ChatPanel
        except ImportError as e:
            if "PySide6" in str(e):
                pytest.skip("PySide6 not installed")
            raise

        assert issubclass(ChatPanel, PanelBase)


# ============================================================================
# T07-F: transition_reason 在 run() 中正确设置
# ============================================================================


class TestTransitionReasonSet:
    """run() 结束后 last_transition_reason 正确设置（T07 范围 4 种之一）。"""

    def test_normal_completion_no_transition(self, tmp_path):
        """无 tool_calls 的纯文本轮终止时不写 transition_reason（直接 finalize）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-t1"))
            _run(store.append_message(
                "sess-t1", Message(role="user", content="hi", source="user"),
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hello"),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-t1"),
            )
            outcome = _run(runner.run())
            assert outcome.status == "completed"
            # 纯文本轮直接 finalize，不写 transition 事件
            assert runner.last_transition_reason is None
        finally:
            store.close()

    def test_tool_loop_sets_next_turn(self, tmp_path):
        """工具轮后设置 transition_reason = next_turn。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-t2"))
            _run(store.append_message(
                "sess-t2", Message(role="user", content="lookup", source="user"),
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(script=[
                        LLMResponse(
                            content="",
                            tool_calls=[{
                                "id": "call-1", "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }],
                        ),
                        LLMResponse(content="done"),
                    ]),
                    event_store=store,
                    tool_executor=MockToolExecutor(default_result="ok"),
                ),
                config=RunnerConfig(session_id="sess-t2"),
            )
            outcome = _run(runner.run())
            assert outcome.status == "completed"
            assert runner.last_transition_reason == TRANSITION_NEXT_TURN
        finally:
            store.close()

    def test_interrupt_sets_user_interrupted(self, tmp_path):
        """中断时设置 transition_reason = user_interrupted。"""
        import asyncio as _asyncio

        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-t3"))
            _run(store.append_message(
                "sess-t3", Message(role="user", content="hi", source="user"),
            ))

            interrupt_event = _asyncio.Event()

            # MockLLM 第一次返回后中断
            call_count = [0]

            def scripted(req):
                call_count[0] += 1
                if call_count[0] >= 1:
                    # 触发中断（下一轮顶部检查到）
                    interrupt_event.set()
                return LLMResponse(
                    content="",
                    tool_calls=[{
                        "id": "call-x", "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }],
                )

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(callable=scripted),
                    event_store=store,
                    tool_executor=MockToolExecutor(default_result="ok"),
                    interrupt_event=interrupt_event,
                ),
                config=RunnerConfig(session_id="sess-t3", max_iterations=10, use_stream=False),  # call 模式轮次间中断
            )
            outcome = _run(runner.run())
            assert outcome.status == "interrupted"
            assert runner.last_transition_reason == TRANSITION_USER_INTERRUPTED
        finally:
            store.close()
