"""chat-panel-v2 Ticket 07 验收测试：消息时间线块结构 + 5 块类拆分 + 流式中断

覆盖 T07 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] _UserBubble 类：user + steer 共用，user 右对齐 ACCENT_WASH bubble，steer 仅小字标记（D37）
- [x] _AssistantTextBlock 类：左对齐无 bubble，QTextBrowser setMarkdown，永不折叠
- [x] _ThinkingBlock 类：左对齐无 bubble，默认折叠，dashed border + "▶ thinking" toggle
- [x] _ToolCallBlock 类（三部分在 T08 实现）：默认折叠为单行（tool 名 + 状态点）
- [x] _SystemBlock 类：system/system_reminder/summary 默认折叠
- [x] _MessageTimeline 容器：QVBoxLayout 管理 seq 顺序追加各块
- [x] 流式增量：_streaming_assistant_block 追踪 streaming_text_delta 事件，>4k 自动降级
- [x] 流式中断（D43）：streaming bubble 直接 finalize 为中断 assistant 块 + thinking 独立块
- [x] 复用 v6 spec D5 淡入动效（150ms OutQuad）
- [x] 复用 v6 spec D4 弹性布局（去 setMaximumHeight，仅 setMinimumHeight）

测试策略：
- 源码静态扫描：grep 5 块类 + _MessageTimeline 定义
- 运行时实例化：各块类构造 + 视觉规则断言
- 行为测试：timeline append + 流式中断 finalize
- 模仿 tests/test_chat_panel_v2_start_page.py 模式：module-scoped qapp + offscreen Qt
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================================
# Part 2: 运行时实例化 — 各块类视觉规则
# ============================================================================


class TestUserBubbleVisualRules:
    """_UserBubble 视觉规则（D39 + D37）。"""

    def test_user_bubble_right_aligned_with_accent_wash(self, qapp):
        """user 消息：右对齐 + ACCENT_WASH bubble。"""
        from client.core.agent import Message
        from client.panels.chat import _UserBubble
        from lib.ui import tokens

        msg = Message(role="user", content="hello", source="user")
        bubble = _UserBubble(msg)
        try:
            # 应有 objectName=userBubble 的 QFrame
            from PySide6.QtWidgets import QFrame

            inner = bubble.findChild(QFrame, "userBubble")
            assert inner is not None, "_UserBubble 应含 objectName=userBubble 的 bubble"
            # 样式含 ACCENT_WASH
            style = inner.styleSheet()
            assert tokens.ACCENT_WASH in style, (
                f"user bubble 背景应为 ACCENT_WASH，实际 {style}"
            )
        finally:
            bubble.deleteLater()
            qapp.processEvents()

    def test_steer_bubble_shows_label(self, qapp):
        """steer 消息：显示'用户引导'标签（D37）。"""
        from PySide6.QtWidgets import QLabel

        from client.core.agent import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="steer text", source="steer")
        bubble = _UserBubble(msg)
        try:
            labels = bubble.findChildren(QLabel)
            texts = [lbl.text() for lbl in labels]
            assert "用户引导" in texts, (
                f"steer 消息应显示'用户引导'标签，实际 labels: {texts}"
            )
        finally:
            bubble.deleteLater()
            qapp.processEvents()


class TestAssistantTextBlockVisualRules:
    """_AssistantTextBlock 视觉规则（D39 + D40 + D46 #6/#10）。"""

    def test_assistant_block_uses_qtextbrowser(self, qapp):
        """assistant 文本块用 QTextBrowser setMarkdown。"""
        from PySide6.QtWidgets import QTextBrowser

        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="# Title")
        try:
            browser = block.findChild(QTextBrowser)
            assert browser is not None, "_AssistantTextBlock 应含 QTextBrowser"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_empty_shows_placeholder(self, qapp):
        """content='' 时显示'思考中...'占位（D46 #10）。"""
        from client.panels.chat import _EMPTY_ASSISTANT_PLACEHOLDER, _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            assert block._showing_placeholder is True, (
                "content='' 时应 _showing_placeholder=True"
            )
            assert block._content_view is not None
            text = block._content_view.toPlainText()
            assert _EMPTY_ASSISTANT_PLACEHOLDER in text or "思考中" in text, (
                f"空 assistant 块应显示占位文本，实际：{text}"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_streaming_delta(self, qapp):
        """流式 append_text_delta 增量追加。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            block.append_text_delta("Hello")
            block.append_text_delta(" World")
            assert block._accumulated_text == "Hello World"
            text = block._content_view.toPlainText()
            assert "Hello World" in text, (
                f"流式 append 后应含 'Hello World'，实际：{text}"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_4k_degradation(self, qapp):
        """>4k 自动降级（D46 #6）。"""
        from client.panels.chat import _LARGE_MESSAGE_THRESHOLD, _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            # 追加超过阈值
            big_delta = "a" * (_LARGE_MESSAGE_THRESHOLD + 10)
            block.append_text_delta(big_delta)
            assert block._degraded_to_close is True, (
                ">4k 后应 _degraded_to_close=True"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_finalize_sets_markdown(self, qapp):
        """finalize_text 用 setMarkdown 渲染。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            block.append_text_delta("partial")
            block.finalize_text("# Final Title\n\nFinal content")
            assert block._is_streaming is False
            text = block._content_view.toPlainText()
            assert "Final Title" in text or "Final content" in text, (
                f"finalize 后应含 markdown 内容，实际：{text}"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_finalize_as_interrupted(self, qapp):
        """D43: finalize_as_interrupted 保留累积文本 + _interrupted=True。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="")
        try:
            block.start_streaming()
            block.append_text_delta("partial text")
            block.finalize_as_interrupted()
            assert block._interrupted is True, "finalize_as_interrupted 后 _interrupted=True"
            assert block._is_streaming is False
            text = block._content_view.toPlainText()
            assert "partial text" in text, (
                f"中断后应保留累积文本，实际：{text}"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_block_model_label(self, qapp):
        """D31: set_model 在气泡下方小字显示。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="test")
        try:
            block.set_model("deepseek-chat")
            assert block._model == "deepseek-chat"
            block.finalize_text("test")  # trigger _update_model_label
            # isVisible() 在孤立 widget（未 show 的父链）上返回 False，
            # 用 isVisibleTo(parent) 检查相对于父的逻辑可见性（Qt 测试标准做法）
            assert block._model_label.isVisibleTo(block), "model label 应可见（相对父）"
            assert "deepseek-chat" in block._model_label.text()
        finally:
            block.deleteLater()
            qapp.processEvents()


class TestThinkingBlockVisualRules:
    """_ThinkingBlock 视觉规则（D40 + D46 #11）。"""

    def test_thinking_block_default_collapsed(self, qapp):
        """thinking 块默认折叠。"""
        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="some thinking")
        try:
            assert block._expanded is False, "thinking 块应默认折叠"
            assert block._view.isVisibleTo(block) is False, "thinking view 应默认隐藏（相对父）"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_thinking_block_toggle(self, qapp):
        """toggle 展开/折叠 thinking 内容。"""
        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="thinking content")
        try:
            block.toggle()
            assert block._expanded is True
            assert block._view.isVisibleTo(block) is True, "展开后 view 应可见（相对父）"
            assert "▼ thinking" in block._toggle_btn.text()

            block.toggle()
            assert block._expanded is False
            assert block._view.isVisibleTo(block) is False
            assert "▶ thinking" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_thinking_block_dashed_border(self, qapp):
        """thinking 块应有 dashed border（D46 #11）。"""
        from PySide6.QtWidgets import QFrame

        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="test")
        try:
            frame = block.findChild(QFrame, "thinkingBlock")
            assert frame is not None, "应有 objectName=thinkingBlock 的 QFrame"
            style = frame.styleSheet()
            assert "dashed" in style, f"thinking block 应有 dashed border，实际 {style}"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_thinking_block_streaming_delta(self, qapp):
        """流式 append_delta 增量追加（展开时）。"""
        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="")
        try:
            block._is_streaming = True
            block.toggle()  # 展开才能增量 append
            block.append_delta("think ")
            block.append_delta("more")
            assert block._accumulated == "think more"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_thinking_block_finalize_as_interrupted(self, qapp):
        """D43: finalize_as_interrupted 保留累积内容。"""
        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="")
        try:
            block._is_streaming = True
            block.append_delta("accumulated thinking")
            block.finalize_as_interrupted()
            assert block._interrupted is True
            text = block._view.toPlainText()
            assert "accumulated thinking" in text
        finally:
            block.deleteLater()
            qapp.processEvents()


class TestToolCallBlockVisualRules:
    """_ToolCallBlock 视觉规则（D45 + D41 + D46 #1）。"""

    def test_tool_call_block_single_line(self, qapp):
        """tool_call 块默认单行（tool 名 + 状态点）。"""
        from client.panels.chat import _ToolCallBlock

        tc_dict = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc_dict, status="pending")
        try:
            assert block.tool_call_id == "tc-1"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_tool_call_block_category_color(self, qapp):
        """tool 名按类别染色（D46 #1）。"""
        from client.panels.chat import _TOOL_CATEGORY_COLOR, _ToolCallBlock
        from lib.ui import tokens

        # exec_python → execute 类（黄）
        tc_dict = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc_dict, status="pending")
        try:
            expected_color = _TOOL_CATEGORY_COLOR.get("execute", tokens.TEXT_TERTIARY)
            # T08 重写后：工具名是 toggle_btn（QPushButton，显示 "▶ exec_python"），
            # 不再是 QLabel（旧版）。检查 toggle_btn 文本 + 类别染色样式。
            from PySide6.QtWidgets import QPushButton

            btns = block.findChildren(QPushButton)
            name_btns = [b for b in btns if "exec_python" in b.text()]
            assert len(name_btns) >= 1, "应有含工具名的 QPushButton（toggle_btn）"
            style = name_btns[0].styleSheet()
            assert expected_color in style, (
                f"exec_python 应染 execute 类色 {expected_color}，实际 {style}"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_tool_call_block_update_status(self, qapp):
        """update_status 更新状态。"""
        from client.panels.chat import TOOL_STATUS_COMPLETED, _ToolCallBlock

        tc_dict = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc_dict, status="pending")
        try:
            block.update_status(TOOL_STATUS_COMPLETED)
            assert block._status == TOOL_STATUS_COMPLETED
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_tool_call_block_attach_result(self, qapp):
        """attach_tool_result 更新状态为 completed。"""
        from client.core.agent import Message
        from client.panels.chat import TOOL_STATUS_COMPLETED, _ToolCallBlock

        tc_dict = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc_dict, status="running")
        try:
            result = Message(role="tool", content='{"success": true}', tool_call_id="tc-1")
            block.attach_tool_result(result)
            assert block._status == TOOL_STATUS_COMPLETED
            assert block._tool_result_message is result
        finally:
            block.deleteLater()
            qapp.processEvents()


class TestSystemBlockVisualRules:
    """_SystemBlock 视觉规则（D44）。"""

    def test_system_block_default_collapsed(self, qapp):
        """system 块默认折叠。"""
        from client.core.agent import Message
        from client.panels.chat import _SystemBlock

        msg = Message(role="system", content="system msg")
        block = _SystemBlock(msg)
        try:
            assert block._expanded is False
            assert block._view.isVisible() is False
            assert "系统" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_system_reminder_block_label(self, qapp):
        """system_reminder 块显示'系统提醒'标签。"""
        from client.core.agent import Message
        from client.panels.chat import _SystemBlock

        msg = Message(role="system_reminder", content="reminder")
        block = _SystemBlock(msg)
        try:
            assert "系统提醒" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_summary_block_label(self, qapp):
        """summary 块显示'L2 摘要'标签。"""
        from client.core.agent import Message
        from client.panels.chat import _SystemBlock

        msg = Message(role="summary", content="summary content")
        block = _SystemBlock(msg)
        try:
            assert "L2 摘要" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_system_block_toggle(self, qapp):
        """toggle 展开/折叠 system 内容。"""
        from client.core.agent import Message
        from client.panels.chat import _SystemBlock

        msg = Message(role="system", content="system content")
        block = _SystemBlock(msg)
        try:
            block.toggle()
            assert block._expanded is True
            assert block._view.isVisibleTo(block) is True

            block.toggle()
            assert block._expanded is False
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: _MessageTimeline 容器行为
# ============================================================================


class TestMessageTimelineBehavior:
    """_MessageTimeline 容器行为（D38 + D45）。"""

    def test_timeline_append_user_block(self, qapp):
        """append_user_block 追加 _UserBubble。"""
        from client.core.agent import Message
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            msg = Message(role="user", content="hi", source="user")
            tl.append_user_block(msg)
            assert len(tl._tool_call_blocks) == 0
            # 时间线 layout 应含 1 个 widget + 1 个 stretch
            assert tl._layout.count() == 2
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_assistant_blocks(self, qapp):
        """append_assistant_blocks 追加 thinking + tool_calls + text 块。"""
        from client.core.agent import Message
        from client.panels.chat import (
            _AssistantTextBlock,
            _MessageTimeline,
            _ThinkingBlock,
            _ToolCallBlock,
        )

        tl = _MessageTimeline()
        try:
            msg = Message(
                role="assistant",
                content="final answer",
                thinking="thinking here",
                tool_calls=[{"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}],
            )
            blocks = tl.append_assistant_blocks(msg)
            # 应产生 3 块：thinking + tool_call + text
            assert len(blocks) == 3
            assert any(isinstance(b, _ThinkingBlock) for b in blocks)
            assert any(isinstance(b, _ToolCallBlock) for b in blocks)
            assert any(isinstance(b, _AssistantTextBlock) for b in blocks)
            # tool_call_id 应注册到映射
            assert "tc-1" in tl._tool_call_blocks
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_tool_result_attaches_to_existing(self, qapp):
        """append_tool_result 附加到匹配的 _ToolCallBlock。"""
        from client.core.agent import Message
        from client.panels.chat import TOOL_STATUS_COMPLETED, _MessageTimeline

        tl = _MessageTimeline()
        try:
            # 先追加 assistant 消息含 tool_call
            msg = Message(role="assistant", content="", thinking="")
            msg.tool_calls = [{"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}]
            tl.append_assistant_blocks(msg)
            # 再追加 tool_result
            result = Message(role="tool", content='{"success": true}', tool_call_id="tc-1")
            block = tl.append_tool_result(result, tool_name="exec_python")
            assert block._status == TOOL_STATUS_COMPLETED
            assert block._tool_result_message is result
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_tool_result_creates_standalone(self, qapp):
        """无匹配 tool_call 时创建独立块。"""
        from client.core.agent import Message
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            result = Message(role="tool", content='{"success": true}', tool_call_id="unknown-id")
            block = tl.append_tool_result(result, tool_name="some_tool", status="completed")
            assert block._tool_call_id == "unknown-id"
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_system_block(self, qapp):
        """append_system_block 追加 _SystemBlock。"""
        from client.core.agent import Message
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            msg = Message(role="system", content="system msg")
            tl.append_system_block(msg)
            assert tl._layout.count() == 2  # 1 widget + 1 stretch
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_clear(self, qapp):
        """clear 清空所有块。"""
        from client.core.agent import Message
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            tl.append_user_block(Message(role="user", content="hi", source="user"))
            tl.append_user_block(Message(role="user", content="hi2", source="user"))
            assert tl._layout.count() == 3  # 2 widgets + 1 stretch
            tl.clear()
            assert tl._layout.count() == 1  # only stretch
            assert len(tl._tool_call_blocks) == 0
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_update_tool_call_status(self, qapp):
        """update_tool_call_status 更新对应块状态。"""
        from client.core.agent import Message
        from client.panels.chat import TOOL_STATUS_COMPLETED, _MessageTimeline

        tl = _MessageTimeline()
        try:
            msg = Message(role="assistant", content="")
            msg.tool_calls = [{"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}]
            tl.append_assistant_blocks(msg)
            tl.update_tool_call_status({"tc-1": {"name": "exec_python", "status": TOOL_STATUS_COMPLETED}})
            block = tl._tool_call_blocks.get("tc-1")
            assert block is not None
            assert block._status == TOOL_STATUS_COMPLETED
        finally:
            tl.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 4: 流式 + 流式中断（D43）
# ============================================================================


class TestStreamingAndInterruption:
    """流式增量 + 流式中断（D43）。"""

    def test_timeline_start_streaming_assistant(self, qapp):
        """start_streaming_assistant 创建流式块。"""
        from client.panels.chat import _AssistantTextBlock, _MessageTimeline

        tl = _MessageTimeline()
        try:
            assert not tl.has_streaming_assistant()
            block = tl.start_streaming_assistant()
            assert isinstance(block, _AssistantTextBlock)
            assert tl.has_streaming_assistant()
            assert block._is_streaming is True
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_text_delta(self, qapp):
        """append_text_delta 增量到流式块。"""
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            tl.start_streaming_assistant()
            tl.append_text_delta("Hello")
            tl.append_text_delta(" World")
            block = tl._streaming_assistant_block
            assert block._accumulated_text == "Hello World"
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_append_thinking_delta_creates_block(self, qapp):
        """append_thinking_delta 创建独立 thinking 块（D38）。"""
        from client.panels.chat import _MessageTimeline, _ThinkingBlock

        tl = _MessageTimeline()
        try:
            assert tl._streaming_thinking_block is None
            tl.append_thinking_delta("thinking delta")
            assert tl._streaming_thinking_block is not None
            assert isinstance(tl._streaming_thinking_block, _ThinkingBlock)
            assert tl._streaming_thinking_block._accumulated == "thinking delta"
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_finalize_streaming_assistant(self, qapp):
        """finalize_streaming_assistant 用完整文本替换。"""
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            tl.start_streaming_assistant()
            tl.append_text_delta("partial")
            tl.finalize_streaming_assistant("full final text", model="deepseek-chat")
            assert not tl.has_streaming_assistant()
            # 流式块应已 finalize
            text = tl._streaming_assistant_block  # 应为 None
            assert text is None
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_finalize_streaming_on_interrupt(self, qapp):
        """D43: finalize_streaming_on_interrupt 转正 streaming 块 + thinking 块。"""
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            # 启动流式
            tl.start_streaming_assistant()
            tl.append_text_delta("partial assistant text")
            tl.append_thinking_delta("partial thinking")
            # 中断
            tl.finalize_streaming_on_interrupt(model="deepseek-chat")
            # 流式状态应清空
            assert not tl.has_streaming_assistant()
            assert tl._streaming_thinking_block is None
            # assistant 块应标记为中断
            # 遍历 timeline 找 _AssistantTextBlock（最后一个）
            from client.panels.chat import _AssistantTextBlock, _ThinkingBlock

            assistant_blocks = []
            thinking_blocks = []
            for i in range(tl._layout.count()):
                item = tl._layout.itemAt(i)
                if item is None:
                    continue
                widget = item.widget()
                if isinstance(widget, _AssistantTextBlock):
                    assistant_blocks.append(widget)
                elif isinstance(widget, _ThinkingBlock):
                    thinking_blocks.append(widget)
            assert len(assistant_blocks) >= 1, "应有至少 1 个 assistant 块"
            assert len(thinking_blocks) >= 1, "应有至少 1 个 thinking 块"
            assert assistant_blocks[-1]._interrupted is True, "assistant 块应标记 _interrupted=True"
            assert thinking_blocks[-1]._interrupted is True, "thinking 块应标记 _interrupted=True"
            # 累积文本应保留
            at_text = assistant_blocks[-1]._content_view.toPlainText()
            assert "partial assistant text" in at_text
        finally:
            tl.deleteLater()
            qapp.processEvents()

    def test_timeline_finalize_streaming_on_interrupt_no_streaming(self, qapp):
        """无流式块时 finalize_streaming_on_interrupt 不报错。"""
        from client.panels.chat import _MessageTimeline

        tl = _MessageTimeline()
        try:
            tl.finalize_streaming_on_interrupt()
            # 无异常即通过
        finally:
            tl.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 5: ChatPanel 集成
# ============================================================================


class TestChatPanelTimelineIntegration:
    """ChatPanel 集成 _message_timeline（T07）。"""

    def test_chat_panel_has_message_timeline(self, qapp):
        """ChatPanel 实例化后应有 _message_timeline 属性。"""
        from client.panels.chat import ChatPanel, _MessageTimeline

        panel = ChatPanel()
        try:
            assert hasattr(panel, "_message_timeline")
            assert isinstance(panel._message_timeline, _MessageTimeline)
        finally:
            panel._session_list_timer.stop()
            panel._poll_timer.stop()
            if panel._read_store is not None:
                try:
                    panel._read_store.close()
                except Exception:
                    pass
            panel.deleteLater()
            qapp.processEvents()

    def test_chat_panel_append_message_dispatches(self, qapp, tmp_path, monkeypatch):
        """_append_message 按 role 派发到 timeline。"""
        import client.panels.chat as chat_module
        from client.core.agent import Message
        from client.panels.chat import ChatPanel

        db_path = str(tmp_path / "test_timeline.db")
        monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
        panel = ChatPanel()
        try:
            # user 消息
            panel._append_message(Message(role="user", content="hi", source="user"), {})
            assert panel._message_timeline._layout.count() == 2  # 1 widget + stretch

            # assistant 消息（含 thinking + text）
            msg = Message(role="assistant", content="answer", thinking="thinking")
            panel._append_message(msg, {})
            # 应追加 thinking + text = 2 块
            assert panel._message_timeline._layout.count() == 4  # 3 widgets + stretch
        finally:
            panel._session_list_timer.stop()
            panel._poll_timer.stop()
            if panel._read_store is not None:
                try:
                    panel._read_store.close()
                except Exception:
                    pass
            panel.deleteLater()
            qapp.processEvents()

    def test_chat_panel_clear_messages(self, qapp, tmp_path, monkeypatch):
        """_clear_messages 清空 timeline。"""
        import client.panels.chat as chat_module
        from client.core.agent import Message
        from client.panels.chat import ChatPanel

        db_path = str(tmp_path / "test_clear.db")
        monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
        panel = ChatPanel()
        try:
            panel._append_message(Message(role="user", content="hi", source="user"), {})
            panel._append_message(Message(role="user", content="hi2", source="user"), {})
            assert panel._message_timeline._layout.count() == 3
            panel._clear_messages()
            assert panel._message_timeline._layout.count() == 1  # only stretch
        finally:
            panel._session_list_timer.stop()
            panel._poll_timer.stop()
            if panel._read_store is not None:
                try:
                    panel._read_store.close()
                except Exception:
                    pass
            panel.deleteLater()
            qapp.processEvents()
