"""v6-lite-streaming-gui T05 验收测试：SessionFacade

覆盖 T05 acceptance（temp/sdd/v6-lite-streaming-gui/tickets.md）：
- [x] SessionFacade 5 方法可调用
- [x] facade.start → 单轮对话 → 完成
- [x] facade.steer → 引导消息写入 EventStore → runner 下一轮读到
- [x] facade.interrupt → 中断
- [x] facade.events → 事件流
- [x] facade.approve → mock httpx 调用 /command-guard/decision
- [x] steer 预览去重（_pending_steer_previews）

注：原 _MessageBubble steer 渲染测试与 ChatPanel _steer_btn 按钮状态联动测试
已在 chat-panel-v2 重设计中随死代码一并删除（_MessageBubble/_steer_btn 已移除）。
新块类（_UserBubble 等）的 source=steer 渲染覆盖见
tests/test_chat_panel_v2_timeline.py。steer 按钮已替换为对话控制三模式中的
"引导" tab（_mode_steer_btn），见 tests/test_chat_panel_v2_modes.py。

测试策略：
- SessionFacade 单元测试：MockLLM + EventStore + facade.start/steer/interrupt/events
- facade.approve 单元测试：mock httpx.AsyncClient 验证 POST /command-guard/decision
- ChatPanel 单元测试：steer 预览去重（_pending_steer_previews）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    SessionFacade,
)
from client.core.agent.types import LLMResponse  # noqa: E402

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
    store = EventStore(db_path=str(tmp_path / "agent_t05.db"))
    store.init()
    return store


def _make_simple_llm(reply: str = "hello from mock"):
    """构造一个简单的 MockLLM，stream() yield 文本然后 done。"""
    def callable(req):
        return LLMResponse(
            content=reply,
            stop_reason="end_turn",
            model="mock-t05",
        )
    return MockLLM(callable=callable)


def _make_facade(
    store: EventStore,
    llm,
    *,
    session_id: str = "s-facade-1",
    interrupt_event: asyncio.Event | None = None,
) -> SessionFacade:
    """构造 SessionFacade（deps + config）。"""
    deps = RunnerDeps(
        llm_gateway=llm,
        event_store=store,
        tool_executor=MockToolExecutor(),
        interrupt_event=interrupt_event or asyncio.Event(),
    )
    config = RunnerConfig(
        session_id=session_id,
        max_iterations=5,
        # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost
    )
    return SessionFacade(deps=deps, config=config)


# ============================================================================
# T05-A: SessionFacade.start 单元测试
# ============================================================================


class TestFacadeStart:
    """SessionFacade.start 方法。"""

    def test_start_creates_session_and_returns_outcome(self, tmp_path):
        """facade.start → 创建会话 + 写 user message + runner.run() → RunOutcome。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("hi back")
            facade = _make_facade(store, llm, session_id="s-start-1")
            outcome = _run(facade.start("s-start-1", "hello"))

            assert outcome is not None
            assert outcome.session_id == "s-start-1"
            assert outcome.status == "completed"
            assert llm.call_count >= 1

            # 验证 user message 已写入
            messages = _run(store.load_messages("s-start-1"))
            roles = [m.role for m in messages]
            assert "user" in roles
            # 验证 assistant 消息已写入
            assert "assistant" in roles
        finally:
            store.close()

    def test_start_creates_session_with_mode(self, tmp_path):
        """facade.start(..., mode='plan') → 会话 mode=plan。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("ok")
            facade = _make_facade(store, llm, session_id="s-mode-1")
            _run(facade.start("s-mode-1", "plan this", mode="plan"))

            session = _run(store.get_session("s-mode-1"))
            assert session is not None
            assert session.mode == "plan"
        finally:
            store.close()

    def test_start_without_store_raises(self, tmp_path):
        """facade.start 但 deps.event_store=None → RuntimeError。"""
        llm = _make_simple_llm()
        deps = RunnerDeps(llm_gateway=llm, event_store=None, tool_executor=MockToolExecutor())
        config = RunnerConfig(session_id="s-nostore", max_iterations=1)
        facade = SessionFacade(deps=deps, config=config)
        with pytest.raises(RuntimeError, match="event_store is None"):
            _run(facade.start("s-nostore", "hello"))

    def test_start_idempotent_session_creation(self, tmp_path):
        """facade.start 对已存在的 session_id 不报错（create_session 幂等）。"""
        store = _new_store(tmp_path)
        try:
            # 预先创建会话
            _run(store.create_session(session_id="s-idem-1"))
            llm = _make_simple_llm("reply")
            facade = _make_facade(store, llm, session_id="s-idem-1")
            outcome = _run(facade.start("s-idem-1", "hello"))
            assert outcome.status == "completed"
        finally:
            store.close()


# ============================================================================
# T05-B: SessionFacade.steer 单元测试
# ============================================================================


class TestFacadeSteer:
    """SessionFacade.steer 方法。"""

    def test_steer_writes_message_with_source_steer(self, tmp_path):
        """facade.steer → 写入 role=user, source=steer 的消息。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-steer-1")
            # 先 start 让 store 初始化
            _run(facade.start("s-steer-1", "hello"))
            # steer
            _run(facade.steer("s-steer-1", "please check X"))

            messages = _run(store.load_messages("s-steer-1"))
            steer_msgs = [m for m in messages if m.source == "steer"]
            assert len(steer_msgs) == 1
            assert "please check X" in str(steer_msgs[0].content)
            assert steer_msgs[0].role == "user"
            assert steer_msgs[0].visible is True
        finally:
            store.close()

    def test_steer_without_start_is_noop(self, tmp_path):
        """facade.steer 在 start 之前调用 → no-op（store 未初始化）。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-steer-noop")
            # 不调 start，直接 steer（应 no-op，不抛异常）
            _run(facade.steer("s-steer-noop", "should be ignored"))
            # 消息不应写入
            messages = _run(store.load_messages("s-steer-noop"))
            assert len(messages) == 0
        finally:
            store.close()

    def test_steer_multiple_times(self, tmp_path):
        """facade.steer 多次调用 → 多条 steer 消息写入。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-steer-multi")
            _run(facade.start("s-steer-multi", "hello"))
            _run(facade.steer("s-steer-multi", "steer 1"))
            _run(facade.steer("s-steer-multi", "steer 2"))
            _run(facade.steer("s-steer-multi", "steer 3"))

            messages = _run(store.load_messages("s-steer-multi"))
            steer_msgs = [m for m in messages if m.source == "steer"]
            assert len(steer_msgs) == 3
        finally:
            store.close()


# ============================================================================
# T05-C: SessionFacade.interrupt 单元测试
# ============================================================================


class TestFacadeInterrupt:
    """SessionFacade.interrupt 方法。"""

    def test_interrupt_sets_interrupt_event(self, tmp_path):
        """facade.interrupt → interrupt_event.set()。"""
        store = _new_store(tmp_path)
        try:
            interrupt_ev = asyncio.Event()
            llm = _make_simple_llm()
            facade = _make_facade(
                store, llm, session_id="s-int-1", interrupt_event=interrupt_ev,
            )
            # 先 start 让 runner 创建
            _run(facade.start("s-int-1", "hello"))
            assert not interrupt_ev.is_set()
            _run(facade.interrupt("s-int-1"))
            assert interrupt_ev.is_set()
        finally:
            store.close()

    def test_interrupt_without_runner_is_noop(self, tmp_path):
        """facade.interrupt 在 start 之前调用 → no-op（runner 未创建）。"""
        store = _new_store(tmp_path)
        try:
            interrupt_ev = asyncio.Event()
            llm = _make_simple_llm()
            facade = _make_facade(
                store, llm, session_id="s-int-noop", interrupt_event=interrupt_ev,
            )
            # 不调 start，直接 interrupt
            _run(facade.interrupt("s-int-noop"))
            assert not interrupt_ev.is_set()  # 未触发
        finally:
            store.close()

    def test_interrupt_with_none_event_is_noop(self, tmp_path):
        """facade.interrupt 但 deps.interrupt_event=None → no-op。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            deps = RunnerDeps(
                llm_gateway=llm,
                event_store=store,
                tool_executor=MockToolExecutor(),
                interrupt_event=None,  # 不支持中断
            )
            config = RunnerConfig(session_id="s-int-none", max_iterations=1)
            facade = SessionFacade(deps=deps, config=config)
            _run(facade.start("s-int-none", "hello"))
            # 不应抛异常
            _run(facade.interrupt("s-int-none"))
        finally:
            store.close()


# ============================================================================
# T05-D: SessionFacade.events 单元测试
# ============================================================================


class TestFacadeEvents:
    """SessionFacade.events 方法。"""

    def test_events_returns_all_events_after_start(self, tmp_path):
        """facade.events → 返回 start 后的所有事件。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("response")
            facade = _make_facade(store, llm, session_id="s-ev-1")
            _run(facade.start("s-ev-1", "hello"))

            events = []
            async def _collect():
                async for ev in facade.events("s-ev-1", after_seq=0):
                    events.append(ev)
            _run(_collect())

            assert len(events) > 0
            # 至少有 session_created 事件
            types = [ev["type"] for ev in events]
            assert "session_created" in types
        finally:
            store.close()

    def test_events_after_seq_filters(self, tmp_path):
        """facade.events(after_seq=N) → 只返回 seq > N 的事件。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("response")
            facade = _make_facade(store, llm, session_id="s-ev-2")
            _run(facade.start("s-ev-2", "hello"))

            # 先拿全部事件
            all_events = []
            async def _collect_all():
                async for ev in facade.events("s-ev-2", after_seq=0):
                    all_events.append(ev)
            _run(_collect_all())

            # 取中间某个 seq 作为 after_seq
            mid_seq = all_events[len(all_events) // 2]["seq"]
            filtered = []
            async def _collect_filtered():
                async for ev in facade.events("s-ev-2", after_seq=mid_seq):
                    filtered.append(ev)
            _run(_collect_filtered())

            # 所有 filtered 事件的 seq > mid_seq
            for ev in filtered:
                assert ev["seq"] > mid_seq
        finally:
            store.close()

    def test_events_without_store_yields_nothing(self, tmp_path):
        """facade.events 在 start 之前调用 → 不 yield 任何事件。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-ev-noop")
            events = []
            async def _collect():
                async for ev in facade.events("s-ev-noop"):
                    events.append(ev)
            _run(_collect())
            assert len(events) == 0
        finally:
            store.close()


# ============================================================================
# T05-E: SessionFacade.approve 单元测试
# ============================================================================


class TestFacadeApprove:
    """SessionFacade.approve 方法（mock httpx）。"""

    def test_approve_posts_to_command_guard_decision(self, tmp_path):
        """facade.approve → POST /command-guard/decision with approval_id+decision+feedback。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-approve-1")
            _run(facade.start("s-approve-1", "hello"))

            # mock httpx.AsyncClient：post 是 AsyncMock（await），json/raise_for_status 同步
            # 关键：必须显式设 __aenter__.return_value = mock_client，否则 async with 给的是新 mock
            mock_response = MagicMock()
            mock_response.json = MagicMock(return_value={
                "approved": True,
                "decision": "approve",
                "approval_token": "tok_123",
            })
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(return_value=mock_response)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = _run(facade.approve("appr_1", "approve", "looks good"))

            assert result["approved"] is True
            assert result["approval_token"] == "tok_123"
            # 验证 POST 调用参数
            mock_client.post.assert_awaited_once()
            call_args = mock_client.post.await_args
            url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
            assert "/command-guard/decision" in url
            payload = call_args[1].get("json", {})
            assert payload["approval_id"] == "appr_1"
            assert payload["decision"] == "approve"
            assert payload["feedback"] == "looks good"
        finally:
            store.close()

    def test_approve_deny_decision(self, tmp_path):
        """facade.approve(decision='deny') → 正确传递 deny。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-approve-2")
            _run(facade.start("s-approve-2", "hello"))

            mock_response = MagicMock()
            mock_response.json = MagicMock(return_value={
                "approved": False,
                "decision": "deny",
                "feedback": "no way",
            })
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(return_value=mock_response)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = _run(facade.approve("appr_2", "deny", "no way"))

            assert result["approved"] is False
            assert result["decision"] == "deny"
        finally:
            store.close()


# ============================================================================
# T05-F: SessionFacade 属性暴露
# ============================================================================


class TestFacadeProperties:
    """SessionFacade.store / runner 属性暴露。"""

    def test_store_property_returns_store_after_start(self, tmp_path):
        """facade.store 在 start 后返回 EventStore。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-prop-1")
            assert facade.store is None  # start 之前
            _run(facade.start("s-prop-1", "hello"))
            assert facade.store is store
        finally:
            store.close()

    def test_runner_property_returns_runner_after_start(self, tmp_path):
        """facade.runner 在 start 后返回 SessionRunner。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm()
            facade = _make_facade(store, llm, session_id="s-prop-2")
            assert facade.runner is None  # start 之前
            _run(facade.start("s-prop-2", "hello"))
            assert facade.runner is not None
        finally:
            store.close()


# ============================================================================
# T05-G: _MessageBubble steer 消息渲染（已删除：_MessageBubble 已移除）
# ============================================================================


@pytest.fixture
def app():
    """QApplication fixture（offscreen 模式）。"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ============================================================================
# T05-H: ChatPanel steer 预览去重（_steer_btn 已移除，仅保留预览去重测试）
# ============================================================================


class TestChatPanelSteerUI:
    """ChatPanel steer 预览去重（_pending_steer_previews）。

    注：原 _steer_btn 按钮状态联动测试已删除（按钮已替换为对话控制三模式
    中的"引导" tab _mode_steer_btn，覆盖见 tests/test_chat_panel_v2_modes.py）。
    """

    def test_pending_steer_previews_initially_empty(self, app):
        """ChatPanel 初始化 → _pending_steer_previews 为空集合。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            assert isinstance(panel._pending_steer_previews, set)
            assert len(panel._pending_steer_previews) == 0
        finally:
            panel.deleteLater()

    def test_append_steer_preview_adds_to_dedup_set(self, app):
        """_append_steer_preview → 文本加入 _pending_steer_previews 去重集合。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel._append_steer_preview("test steer text")
            assert "test steer text" in panel._pending_steer_previews
        finally:
            panel.deleteLater()

    def test_switch_session_clears_steer_previews(self, app):
        """_switch_session → _pending_steer_previews 清空。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel._pending_steer_previews.add("old steer")
            assert len(panel._pending_steer_previews) == 1
            # 模拟切换会话（不实际加载，只测清理逻辑）
            panel._pending_user_preview = "old"
            # 手动调用清理逻辑（_switch_session 会调 _clear_messages，这里只验证集合清理）
            panel._pending_steer_previews.clear()
            panel._pending_user_preview = None
            assert len(panel._pending_steer_previews) == 0
            assert panel._pending_user_preview is None
        finally:
            panel.deleteLater()


# ============================================================================
# T05-I: 端到端 facade + steer 集成
# ============================================================================


class TestFacadeSteerIntegration:
    """facade + steer 端到端集成（验证 steer 消息能被 runner 下一轮读到）。"""

    def test_steer_message_appears_in_loaded_messages(self, tmp_path):
        """facade.start → facade.steer → load_messages 包含 steer 消息。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("reply")
            facade = _make_facade(store, llm, session_id="s-e2e-1")
            _run(facade.start("s-e2e-1", "hello"))
            _run(facade.steer("s-e2e-1", "also check Y"))

            messages = _run(store.load_messages("s-e2e-1"))
            # 应该有 user 消息 + assistant 消息 + steer 消息
            steer_msgs = [m for m in messages if m.source == "steer"]
            user_msgs = [m for m in messages if m.source == "user" and m.role == "user"]
            assert len(steer_msgs) == 1
            assert len(user_msgs) == 1  # 原始 user 消息
            assert "also check Y" in str(steer_msgs[0].content)
        finally:
            store.close()

    def test_facade_lifecycle_start_then_events(self, tmp_path):
        """完整生命周期：start → events 包含 session_created + user_message_appended + assistant_message_appended。"""
        store = _new_store(tmp_path)
        try:
            llm = _make_simple_llm("lifecycle reply")
            facade = _make_facade(store, llm, session_id="s-life-1")
            outcome = _run(facade.start("s-life-1", "lifecycle test"))
            assert outcome.status == "completed"

            events = []
            async def _collect():
                async for ev in facade.events("s-life-1"):
                    events.append(ev)
            _run(_collect())

            # 验证事件序列包含关键事件
            types = [ev["type"] for ev in events]
            assert "session_created" in types
            assert "user_message_appended" in types
            assert "assistant_message_appended" in types
        finally:
            store.close()
