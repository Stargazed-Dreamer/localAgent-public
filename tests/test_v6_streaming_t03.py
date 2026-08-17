"""v6-lite-streaming-gui T03 验收测试：ChatPanel 打字机三档 toggle + 常量一致性

覆盖 T03 acceptance（temp/sdd/v6-lite-streaming-gui/tickets.md）：
- [x] ChatPanel 打字机三档 toggle（close→fast→normal→close 循环）
- [x] _apply_typewriter_mode 调整 _poll_timer interval
- [x] thinking toggle（_on_toggle_thinking stub，T10 后顶栏按钮已移除）

注：原 _MessageBubble 流式增量渲染 / >4k 自动降级 / thinking 气泡渲染等测试
已在 chat-panel-v2 重设计中随死代码一并删除（_MessageBubble 已移除）。
新块类（_AssistantTextBlock 流式 + _ThinkingBlock thinking）的覆盖见
tests/test_chat_panel_v2_timeline.py 的 TestStreamingAndInterruption 与
TestThinkingBlockVisualRules。

运行：QT_QPA_PLATFORM=offscreen uv run pytest tests/test_v6_streaming_t03.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from client.panels.chat import (  # noqa: E402
    _LARGE_MESSAGE_THRESHOLD,
    _TYPEWRITER_MODES,
    _TYPEWRITER_POLL_INTERVAL,
)


@pytest.fixture(scope="module")
def app():
    """会话级 QApplication。"""
    instance = QApplication.instance() or QApplication([])
    yield instance


# ============================================================================
# ChatPanel 打字机三档 toggle
# ============================================================================


class TestTypewriterToggle:
    """ChatPanel 打字机三档 toggle 逻辑。"""

    def test_typewriter_toggle_cycles(self, app):
        """_on_toggle_typewriter → close→fast→normal→close 循环切换。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 初始默认 fast（config 默认）
            assert panel._typewriter_mode == "fast"
            # fast → normal
            panel._on_toggle_typewriter()
            assert panel._typewriter_mode == "normal"
            # normal → close
            panel._on_toggle_typewriter()
            assert panel._typewriter_mode == "close"
            # close → fast
            panel._on_toggle_typewriter()
            assert panel._typewriter_mode == "fast"
        finally:
            panel.deleteLater()

    def test_apply_typewriter_mode_changes_poll_interval(self, app):
        """_apply_typewriter_mode → _poll_timer interval 按档位调整。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            for mode in _TYPEWRITER_MODES:
                panel._typewriter_mode = mode
                panel._apply_typewriter_mode()
                expected = _TYPEWRITER_POLL_INTERVAL[mode]
                assert panel._poll_timer.interval() == expected
        finally:
            panel.deleteLater()

    def test_typewriter_btn_text_reflects_mode(self, app):
        """toggle 后按钮文字反映当前档位（T10 后加「打字机效果：」前缀）。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 初始 fast → 切到 normal
            panel._on_toggle_typewriter()
            assert panel._typewriter_btn.text() == "打字机效果：正常"
            # normal → close
            panel._on_toggle_typewriter()
            assert panel._typewriter_btn.text() == "打字机效果：关闭"
            # close → fast
            panel._on_toggle_typewriter()
            assert panel._typewriter_btn.text() == "打字机效果：快速"
        finally:
            panel.deleteLater()

    def test_thinking_toggle(self, app):
        """_on_toggle_thinking → _show_thinking 切换（T10 后按钮已移除，方法保留为 toggle stub）。

        注：原顶栏 thinking_toggle_btn 已在 chat-panel-v2 T10 移除（D47）。
        新 UX：thinking 块自带折叠 toggle（_ThinkingBlock.toggle）。
        此测试改为验证 _on_toggle_thinking 仍能切换 _show_thinking 状态（stub 行为）。
        """
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            assert panel._show_thinking is False
            # T10 后按钮已移除（_thinking_toggle_btn=None），方法保留为 toggle stub
            panel._on_toggle_thinking()
            assert panel._show_thinking is True
            # 再次 toggle → False
            panel._on_toggle_thinking()
            assert panel._show_thinking is False
        finally:
            panel.deleteLater()


# ============================================================================
# 常量一致性
# ============================================================================


class TestConstants:
    """常量定义一致性。"""

    def test_typewriter_modes_order(self):
        """三档顺序：close, fast, normal。"""
        assert _TYPEWRITER_MODES == ("close", "fast", "normal")

    def test_poll_intervals(self):
        """各档位 poll interval：close 200ms, fast 30ms, normal 16ms。"""
        assert _TYPEWRITER_POLL_INTERVAL["close"] == 200
        assert _TYPEWRITER_POLL_INTERVAL["fast"] == 30
        assert _TYPEWRITER_POLL_INTERVAL["normal"] == 16

    def test_large_message_threshold(self):
        """>4k 阈值 = 4000。"""
        assert _LARGE_MESSAGE_THRESHOLD == 4000
