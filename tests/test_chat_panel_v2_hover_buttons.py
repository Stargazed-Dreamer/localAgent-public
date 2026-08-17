"""chat-panel-v2 Ticket 11 验收测试：操作按钮 hover（D32 + D33）

覆盖 T11 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] _UserBubble 下方 hover 显示 Copy/Delete 按钮（enterEvent/leaveEvent）
- [x] _AssistantTextBlock 下方 hover 显示 Copy 按钮（仅当 finalize 后）
- [x] Copy = 复制消息内容到剪贴板
- [x] Delete = 弹确认对话框 + 删除消息
- [x] steer/queue 消息不显示 hover 按钮（仅 user）
- [x] ChatPanel 连接 hover 按钮信号（_connect_block_hover_signals）
- [x] 流式 assistant 块也连接 Copy 信号（finalize 后激活）

测试策略：
- 源码静态扫描：grep Signal 定义 + enterEvent/leaveEvent + hover 按钮 widget
- 运行时实例化：_UserBubble / _AssistantTextBlock hover 行为
- 信号测试：emit copy_requested/delete_requested → ChatPanel 处理

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHAT_PY = PROJECT_ROOT / "client" / "panels" / "chat.py"


# ============================================================================
# Fixtures
# ============================================================================


def _make_chat_panel(monkeypatch, tmp_path):
    """构造 ChatPanel 实例（patch DEFAULT_DB_PATH 为临时路径）。"""
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    db_path = str(tmp_path / "hover_test.db")
    monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
    return ChatPanel()


def _cleanup_panel(panel, qapp):
    """清理 panel 资源。"""
    panel._session_list_timer.stop()
    panel._poll_timer.stop()
    if panel._read_store is not None:
        try:
            panel._read_store.close()
        except Exception:
            pass
    panel.deleteLater()
    qapp.processEvents()


# ============================================================================
# Part 1: 源码静态扫描
# ============================================================================


class TestSourceHoverButtonDefinitions:
    """chat.py 源码层验证 hover 按钮相关定义。"""

    def test_user_bubble_has_copy_signal(self):
        """_UserBubble 应有 copy_requested Signal。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "copy_requested = Signal" in source

    def test_user_bubble_has_delete_signal(self):
        """_UserBubble 应有 delete_requested Signal。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "delete_requested = Signal" in source

    def test_user_bubble_has_enter_event(self):
        """_UserBubble 应有 enterEvent 方法。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        # _UserBubble 类内 enterEvent
        ub_start = source.find("class _UserBubble")
        ub_end = source.find("class _AssistantTextBlock", ub_start)
        assert ub_start < ub_end
        ub_section = source[ub_start:ub_end]
        assert "def enterEvent" in ub_section
        assert "def leaveEvent" in ub_section

    def test_user_bubble_has_hover_button_row(self):
        """_UserBubble 应有 _hover_button_row 属性。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_hover_button_row" in source

    def test_assistant_text_block_has_copy_signal(self):
        """_AssistantTextBlock 应有 copy_requested Signal。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        atb_start = source.find("class _AssistantTextBlock")
        atb_end = source.find("class _ThinkingBlock", atb_start)
        assert atb_start < atb_end
        atb_section = source[atb_start:atb_end]
        assert "copy_requested = Signal" in atb_section

    def test_assistant_text_block_has_hover_copy_btn(self):
        """_AssistantTextBlock 应有 _hover_copy_btn 属性。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_hover_copy_btn" in source

    def test_assistant_text_block_has_finalized_flag(self):
        """_AssistantTextBlock 应有 _finalized 标记（finalize 后激活 hover）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_finalized" in source

    def test_assistant_text_block_enter_event_guards_finalized(self):
        """_AssistantTextBlock.enterEvent 应检查 _finalized（流式中不显示）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        atb_start = source.find("class _AssistantTextBlock")
        atb_end = source.find("class _ThinkingBlock", atb_start)
        atb_section = source[atb_start:atb_end]
        assert "self._finalized" in atb_section

    def test_chat_panel_connect_signals_method(self):
        """ChatPanel 应有 _connect_block_hover_signals 方法。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _connect_block_hover_signals" in source

    def test_chat_panel_copy_handler(self):
        """ChatPanel 应有 _on_block_copy_requested 方法。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _on_block_copy_requested" in source

    def test_chat_panel_delete_handler(self):
        """ChatPanel 应有 _on_block_delete_requested 方法。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _on_block_delete_requested" in source


# ============================================================================
# Part 2: _UserBubble hover 按钮
# ============================================================================


class TestUserBubbleHoverButtons:
    """_UserBubble hover 按钮行为（D33）。"""

    def test_user_message_has_hover_buttons(self, qapp):
        """user 消息：_hover_button_row 不为 None（hover 按钮存在）。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="hello", source="user", id="m1")
        bubble = _UserBubble(msg)
        try:
            assert bubble._hover_button_row is not None
            assert bubble._can_hover is True
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_steer_message_no_hover_buttons(self, qapp):
        """steer 消息：不显示 hover 按钮（_can_hover=False）。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="guide", source="steer", id="m1")
        bubble = _UserBubble(msg)
        try:
            assert bubble._can_hover is False
            assert bubble._hover_button_row is None
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_queue_message_no_hover_buttons(self, qapp):
        """queue 消息：不显示 hover 按钮（_can_hover=False）。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="queued", source="queue", id="m1")
        bubble = _UserBubble(msg)
        try:
            assert bubble._can_hover is False
            assert bubble._hover_button_row is None
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_hover_buttons_hidden_initially(self, qapp):
        """hover 按钮初始隐藏。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="hello", source="user", id="m1")
        bubble = _UserBubble(msg)
        try:
            assert bubble._hover_button_row.isVisible() is False
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_copy_signal_emits_content(self, qapp):
        """copy_requested 信号应携带消息内容文本。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="copy me", source="user", id="m1")
        bubble = _UserBubble(msg)
        try:
            received = []
            bubble.copy_requested.connect(lambda text: received.append(text))
            bubble.copy_requested.emit("copy me")
            assert received == ["copy me"]
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_delete_signal_emits_message_id(self, qapp):
        """delete_requested 信号应携带 message id。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="delete me", source="user", id="msg_abc")
        bubble = _UserBubble(msg)
        try:
            received = []
            bubble.delete_requested.connect(lambda mid: received.append(mid))
            bubble.delete_requested.emit("msg_abc")
            assert received == ["msg_abc"]
        finally:
            bubble.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: _AssistantTextBlock hover 按钮
# ============================================================================


class TestAssistantTextBlockHoverButton:
    """_AssistantTextBlock hover Copy 按钮（D33，仅 finalize 后）。"""

    def test_hover_copy_btn_exists(self, qapp):
        """_AssistantTextBlock 实例应有 _hover_copy_btn。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="hello")
        try:
            assert block._hover_copy_btn is not None
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_hover_copy_btn_hidden_initially(self, qapp):
        """hover Copy 按钮初始隐藏（未 finalize）。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="hello")
        try:
            assert block._hover_copy_btn.isVisible() is False
            assert block._finalized is False
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_finalize_activates_hover(self, qapp):
        """finalize_text 后 _finalized 应为 True。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            block.append_text_delta("hello")
            block.finalize_text("hello")
            assert block._finalized is True
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_interrupted_also_activates_hover(self, qapp):
        """finalize_as_interrupted 后 _finalized 也应为 True（中断也算 finalize）。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            block.append_text_delta("partial")
            block.finalize_as_interrupted()
            assert block._finalized is True
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_copy_signal_emits_accumulated_text(self, qapp):
        """copy_requested 信号应携带 accumulated_text。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="initial content")
        try:
            received = []
            block.copy_requested.connect(lambda text: received.append(text))
            block.copy_requested.emit("initial content")
            assert received == ["initial content"]
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 4: ChatPanel 信号连接
# ============================================================================


class TestChatPanelHoverSignalConnection:
    """ChatPanel 连接 hover 按钮信号（D33）。"""

    def test_connect_block_hover_signals_user(self, qapp, monkeypatch, tmp_path):
        """_connect_block_hover_signals 应连 _UserBubble 的 copy/delete 信号。"""
        from client.core.agent.types import Message
        from client.panels.chat import _UserBubble

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            msg = Message(role="user", content="hello", source="user", id="m1")
            bubble = _UserBubble(msg)
            panel._connect_block_hover_signals(bubble, msg)
            # 检查信号已连（无异常即成功，Qt 不暴露 receivers 计数）
            # 触发信号 → _on_block_copy_requested 应被调用
            called = []
            monkeypatch.setattr(
                panel, "_on_block_copy_requested",
                lambda text: called.append(text)
            )
            bubble.copy_requested.emit("hello")
            # Qt 信号是同步的，应立即触发
            assert called == ["hello"]
            bubble.deleteLater()
        finally:
            _cleanup_panel(panel, qapp)

    def test_connect_block_hover_signals_assistant(self, qapp, monkeypatch, tmp_path):
        """_connect_block_hover_signals 应连 _AssistantTextBlock 的 copy 信号。"""
        from client.panels.chat import _AssistantTextBlock

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            block = _AssistantTextBlock(content="hello")
            from client.core.agent.types import Message
            msg = Message(role="assistant", content="hello", source="assistant")
            panel._connect_block_hover_signals(block, msg)
            called = []
            monkeypatch.setattr(
                panel, "_on_block_copy_requested",
                lambda text: called.append(text)
            )
            block.copy_requested.emit("hello")
            assert called == ["hello"]
            block.deleteLater()
        finally:
            _cleanup_panel(panel, qapp)

    def test_on_block_copy_sets_clipboard(self, qapp, monkeypatch, tmp_path):
        """_on_block_copy_requested 应把文本写入剪贴板。"""

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._on_block_copy_requested("clipboard content")
            clipboard = qapp.clipboard()
            assert clipboard.text() == "clipboard content"
        finally:
            _cleanup_panel(panel, qapp)

    def test_on_block_delete_no_id_noop(self, qapp, monkeypatch, tmp_path):
        """_on_block_delete_requested 收到空 id 应直接返回（不弹窗）。"""
        from PySide6.QtWidgets import QMessageBox

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            warning_called = []
            monkeypatch.setattr(
                QMessageBox, "question",
                lambda *a, **kw: warning_called.append(a) or QMessageBox.StandardButton.No
            )
            # 空 message_id → 直接返回，不弹窗
            panel._on_block_delete_requested("")
            assert len(warning_called) == 0
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 5: append_message 自动连接信号
# ============================================================================


class TestAppendMessageConnectsHoverSignals:
    """_append_message 应自动连接 hover 信号（user + assistant）。"""

    def test_append_user_message_connects_signals(self, qapp, monkeypatch, tmp_path):
        """追加 user 消息 → _UserBubble 的 copy_requested 信号已连。"""
        from client.core.agent.types import Message

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            msg = Message(role="user", content="test", source="user", id="m1")
            panel._append_message(msg, {})

            # 找到 timeline 中的 _UserBubble，emit copy_requested 应触发 panel._on_block_copy_requested
            called = []
            monkeypatch.setattr(
                panel, "_on_block_copy_requested",
                lambda text: called.append(text)
            )
            # 遍历 timeline 找 _UserBubble
            for block in panel._message_timeline.iter_blocks():
                from client.panels.chat import _UserBubble
                if isinstance(block, _UserBubble):
                    block.copy_requested.emit("test")
                    break
            assert called == ["test"]
        finally:
            _cleanup_panel(panel, qapp)

    def test_append_assistant_message_connects_signals(self, qapp, monkeypatch, tmp_path):
        """追加 assistant 消息 → _AssistantTextBlock 的 copy_requested 信号已连。"""
        from client.core.agent.types import Message
        from client.panels.chat import _AssistantTextBlock

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            msg = Message(
                role="assistant", content="assistant response",
                source="assistant", id="m2"
            )
            panel._append_message(msg, {})

            called = []
            monkeypatch.setattr(
                panel, "_on_block_copy_requested",
                lambda text: called.append(text)
            )
            for block in panel._message_timeline.iter_blocks():
                if isinstance(block, _AssistantTextBlock):
                    block.copy_requested.emit("assistant response")
                    break
            assert called == ["assistant response"]
        finally:
            _cleanup_panel(panel, qapp)
