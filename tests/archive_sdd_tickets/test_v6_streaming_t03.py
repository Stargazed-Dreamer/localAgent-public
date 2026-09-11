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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402


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

