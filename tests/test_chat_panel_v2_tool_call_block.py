"""chat-panel-v2 Ticket 08 验收测试：_ToolCallBlock 三部分（D41 + D41a + D46 #1/#2/#3）

覆盖 T08 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] _ToolCallBlock 三部分垂直堆叠：
  - 原请求：tool 名（ToolCategory 5 类染色）+ args JSON 折叠代码块
  - 原结果：tool_result content 原文折叠代码块
  - 格式解析：5 关键字段表（success/exit_code/stdout/stderr/error）+ 语义着色 + 其余 JSON
- [x] 格式失败兜底：result 不是 JSON → 整个 content 作为原结果展示，不显示格式解析区
- [x] sticky 标题候选（is_collapsible=True，title=tool 名）

测试策略：
- 实例化 _ToolCallBlock，验证折叠态/展开态 UI 结构
- 验证关键字段提取 + 语义着色规则
- 验证格式失败兜底
- 验证 attach_tool_result 重建结果区
- 验证 update_status 保持展开状态
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# Fixtures
# ============================================================================


# ============================================================================
# Part 1: 折叠态（默认）
# ============================================================================


class TestToolCallBlockCollapsed:
    """_ToolCallBlock 折叠态（D41：默认折叠为单行）。"""

    def test_default_collapsed(self, qapp):
        """默认 _expanded=False，展开内容隐藏。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._expanded is False
            assert block._expand_content is not None
            assert block._expand_content.isVisibleTo(block) is False
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_summary_row_has_tool_name(self, qapp):
        """单行摘要含 tool 名（toggle_btn 文本含 tool 名）。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            btns = block.findChildren(QPushButton)
            name_btns = [b for b in btns if "exec_python" in b.text()]
            assert len(name_btns) >= 1, "toggle_btn 应含 tool 名"
            assert "▶" in name_btns[0].text(), "折叠态应为 ▶ 箭头"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_tool_category_color(self, qapp):
        """D46 #1：tool 名按 ToolCategory 5 类染色。"""
        from client.panels.chat import _TOOL_CATEGORY_COLOR, _ToolCallBlock
        from lib.ui import tokens

        # exec_python → execute（黄）
        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            from PySide6.QtWidgets import QPushButton

            btns = block.findChildren(QPushButton)
            name_btn = next(b for b in btns if "exec_python" in b.text())
            expected = _TOOL_CATEGORY_COLOR.get("execute", tokens.TEXT_TERTIARY)
            assert expected in name_btn.styleSheet()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_status_color_and_text(self, qapp):
        """D41：单行摘要含状态点（语义色）+ 状态文本。"""
        from client.panels.chat import _TOOL_STATUS_COLOR, _TOOL_STATUS_TEXT, _ToolCallBlock
        from lib.ui import tokens

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="completed")
        try:
            # 找到状态文本 QLabel（_TOOL_STATUS_TEXT['completed']='已完成'）
            from PySide6.QtWidgets import QLabel

            labels = block.findChildren(QLabel)
            status_labels = [lbl for lbl in labels if lbl.text() == _TOOL_STATUS_TEXT["completed"]]
            assert len(status_labels) >= 1, "应有状态文本 QLabel"
            expected = _TOOL_STATUS_COLOR.get("completed", tokens.TEXT_TERTIARY)
            assert expected in status_labels[0].styleSheet()
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 2: 展开态三部分
# ============================================================================


class TestToolCallBlockExpandedThreeParts:
    """_ToolCallBlock 展开态：三部分垂直堆叠（D41）。"""

    def test_toggle_shows_expand_content(self, qapp):
        """toggle 后展开内容可见。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            block.toggle()
            assert block._expanded is True
            assert block._expand_content.isVisibleTo(block) is True
            # toggle_btn 文本变为 ▼
            assert "▼" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_request_section_args_json(self, qapp):
        """D41 原请求区：args JSON 美化展示。"""
        from PySide6.QtWidgets import QLabel, QTextEdit

        from client.panels.chat import _ToolCallBlock

        args = {"code": "print('hello')", "language": "python"}
        tc = {
            "id": "tc-1",
            "function": {"name": "exec_python", "arguments": json.dumps(args)},
        }
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            block.toggle()  # 展开
            # 找到 "原请求" 标题
            labels = block.findChildren(QLabel)
            req_titles = [lbl for lbl in labels if lbl.text() == "原请求"]
            assert len(req_titles) >= 1, "应有 '原请求' 标题"
            # 找到 args 内容（QTextEdit）
            edits = block.findChildren(QTextEdit)
            args_edit = next((e for e in edits if "print('hello')" in e.toPlainText()), None)
            assert args_edit is not None, "应有含 args JSON 的 QTextEdit"
            # 美化 JSON（缩进展示）
            assert "  " in args_edit.toPlainText() or "\n" in args_edit.toPlainText(), (
                "args JSON 应美化（多行 / 缩进）"
            )
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_result_section_after_attach(self, qapp):
        """D41 原结果区：attach_tool_result 后展示 content 原文。"""
        from PySide6.QtWidgets import QLabel, QTextEdit

        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="running")
        try:
            # attach 前无原结果区
            labels = block.findChildren(QLabel)
            assert not any(lbl.text() == "原结果" for lbl in labels), (
                "attach 前不应有 '原结果' 标题"
            )
            # attach
            from client.core.agent import Message

            result = Message(
                role="tool",
                content='{"success": true, "stdout": "hello\\n"}',
                tool_call_id="tc-1",
            )
            block.attach_tool_result(result)
            block.toggle()
            # attach 后有原结果区
            labels = block.findChildren(QLabel)
            req_titles = [lbl for lbl in labels if lbl.text() == "原结果"]
            assert len(req_titles) >= 1, "attach 后应有 '原结果' 标题"
            # 原结果原文展示
            edits = block.findChildren(QTextEdit)
            result_edit = next(
                (e for e in edits if "success" in e.toPlainText() and "stdout" in e.toPlainText()),
                None,
            )
            assert result_edit is not None, "应有含 content 原文的 QTextEdit"
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: 格式解析区（D41a）
# ============================================================================


class TestToolCallBlockFormatSection:
    """格式解析区：5 关键字段表 + 语义着色（D41a）。"""

    def _make_block_with_result(self, qapp, result_content):
        """构造 block 并 attach tool_result。"""
        from client.core.agent import Message
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="running")
        result = Message(role="tool", content=result_content, tool_call_id="tc-1")
        block.attach_tool_result(result)
        block.toggle()
        return block

    def test_format_section_shown_when_json(self, qapp):
        """D41a：result 是 JSON dict → 显示格式解析区。"""
        from PySide6.QtWidgets import QLabel

        block = self._make_block_with_result(
            qapp, '{"success": true, "exit_code": 0}'
        )
        try:
            labels = block.findChildren(QLabel)
            fmt_titles = [lbl for lbl in labels if lbl.text() == "格式解析"]
            assert len(fmt_titles) >= 1, "result 是 JSON 应显示 '格式解析' 区"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_format_section_hidden_when_not_json(self, qapp):
        """D41a 兜底：result 不是 JSON → 不显示格式解析区。"""
        from PySide6.QtWidgets import QLabel

        block = self._make_block_with_result(qapp, "not a json string")
        try:
            labels = block.findChildren(QLabel)
            fmt_titles = [lbl for lbl in labels if lbl.text() == "格式解析"]
            assert len(fmt_titles) == 0, "result 非 JSON 不应显示 '格式解析' 区"
            # 原结果区仍存在
            assert any(lbl.text() == "原结果" for lbl in labels), "原结果区应存在"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_format_section_hidden_when_json_array(self, qapp):
        """D41a 兜底：result 是 JSON 数组（非 dict）→ 不显示格式解析区。"""
        from PySide6.QtWidgets import QLabel

        block = self._make_block_with_result(qapp, '[1, 2, 3]')
        try:
            labels = block.findChildren(QLabel)
            fmt_titles = [lbl for lbl in labels if lbl.text() == "格式解析"]
            assert len(fmt_titles) == 0, "JSON 数组不应显示 '格式解析' 区"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_key_fields_extracted_in_order(self, qapp):
        """D41a：5 关键字段按固定顺序提取（success/exit_code/stdout/stderr/error）。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            parsed = {
                "error": "some error",
                "stderr": "warn",
                "stdout": "out",
                "exit_code": 1,
                "success": False,
                "extra_field": "ignored",
            }
            key_fields = block._extract_key_fields(parsed)
            # 5 关键字段都提取
            assert set(key_fields.keys()) == {
                "success", "exit_code", "stdout", "stderr", "error"
            }
            # extra_field 不提取
            assert "extra_field" not in key_fields
            # 值标准化
            assert key_fields["success"] == "false"
            assert key_fields["exit_code"] == "1"
            assert key_fields["stdout"] == "out"
            assert key_fields["stderr"] == "warn"
            assert key_fields["error"] == "some error"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_key_field_label_text(self, qapp):
        """D41a：字段标签格式 = 'label: value'。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._format_field_label("success", "true") == "成功: true"
            assert block._format_field_label("exit_code", "0") == "退出码: 0"
            assert block._format_field_label("stdout", "hello") == "stdout: hello"
            assert block._format_field_label("stderr", "warn") == "stderr: warn"
            assert block._format_field_label("error", "err") == "错误: err"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_field_color_success(self, qapp):
        """D41a：success true→绿，false→红。"""
        from client.panels.chat import _ToolCallBlock
        from lib.ui import tokens

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._field_color("success", "true") == tokens.SUCCESS_TEXT
            assert block._field_color("success", "false") == tokens.DANGER_TEXT
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_field_color_exit_code(self, qapp):
        """D41a：exit_code 0→绿，非0→红。"""
        from client.panels.chat import _ToolCallBlock
        from lib.ui import tokens

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._field_color("exit_code", "0") == tokens.SUCCESS_TEXT
            assert block._field_color("exit_code", "1") == tokens.DANGER_TEXT
            assert block._field_color("exit_code", "127") == tokens.DANGER_TEXT
            # 非数字 → 次要文本色
            assert block._field_color("exit_code", "nan") == tokens.TEXT_SECONDARY
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_field_color_stderr(self, qapp):
        """D41a：stderr 非空→WARNING 黄，空→次要文本色。"""
        from client.panels.chat import _ToolCallBlock
        from lib.ui import tokens

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._field_color("stderr", "some warning") == tokens.WARNING_TEXT
            assert block._field_color("stderr", "") == tokens.TEXT_SECONDARY
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_field_color_error(self, qapp):
        """D41a：error 非空→红，空→次要文本色。"""
        from client.panels.chat import _ToolCallBlock
        from lib.ui import tokens

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block._field_color("error", "failed") == tokens.DANGER_TEXT
            assert block._field_color("error", "") == tokens.TEXT_SECONDARY
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 4: 交互（toggle / update_status / attach_tool_result）
# ============================================================================


class TestToolCallBlockInteraction:
    """_ToolCallBlock 交互行为。"""

    def test_toggle_arrow_changes(self, qapp):
        """toggle 后箭头从 ▶ 变 ▼。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert "▶" in block._toggle_btn.text()
            block.toggle()
            assert "▼" in block._toggle_btn.text()
            block.toggle()
            assert "▶" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_collapse_via_sticky_interface(self, qapp):
        """D42：collapse() 方法（sticky 标题点击时调用）。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            # 折叠态调 collapse → no-op
            block.collapse()
            assert block._expanded is False
            # 展开后调 collapse → 折叠
            block.toggle()
            assert block._expanded is True
            block.collapse()
            assert block._expanded is False
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_update_status_preserves_expanded_state(self, qapp):
        """update_status 重建 UI 时保持 _expanded 状态。"""
        from client.panels.chat import TOOL_STATUS_COMPLETED, _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            block.toggle()  # 展开
            assert block._expanded is True
            block.update_status(TOOL_STATUS_COMPLETED)
            # 状态更新后展开状态保持
            assert block._expanded is True
            assert block._status == TOOL_STATUS_COMPLETED
            # toggle_btn 文本仍是 ▼
            assert "▼" in block._toggle_btn.text()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_attach_tool_result_shows_result_section(self, qapp):
        """attach_tool_result 后展开内容含原结果区。"""
        from PySide6.QtWidgets import QLabel

        from client.core.agent import Message
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="running")
        try:
            # attach 前无原结果区
            assert not any(
                lbl.text() == "原结果" for lbl in block.findChildren(QLabel)
            )
            result = Message(
                role="tool",
                content='{"success": true}',
                tool_call_id="tc-1",
            )
            block.attach_tool_result(result)
            block.toggle()
            # attach 后有原结果区
            assert any(lbl.text() == "原结果" for lbl in block.findChildren(QLabel))
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_sticky_interface_properties(self, qapp):
        """D42：sticky 标题接口（is_collapsible / title）。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "exec_python", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block.is_collapsible is True
            assert block.title == "exec_python"
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_unknown_tool_name_fallback(self, qapp):
        """无 function.name 时 tool_name 兜底 '(unknown)'。"""
        from client.panels.chat import _ToolCallBlock

        tc = {"id": "tc-1", "function": {"name": "", "arguments": "{}"}}
        block = _ToolCallBlock(tool_call_dict=tc, status="pending")
        try:
            assert block.tool_name == "(unknown)"
        finally:
            block.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 5: sticky 接口一致性（_ThinkingBlock / _SystemBlock）
# ============================================================================


class TestStickyInterfaceConsistency:
    """T08：所有可折叠块类统一暴露 sticky 接口（is_collapsible / title / collapse）。"""

    def test_thinking_block_is_collapsible(self, qapp):
        from client.panels.chat import _ThinkingBlock

        block = _ThinkingBlock(content="thinking content")
        try:
            assert block.is_collapsible is True
            assert block.title == "thinking"
            # collapse 在展开后生效
            block.toggle()
            assert block._expanded is True
            block.collapse()
            assert block._expanded is False
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_system_block_is_collapsible(self, qapp):
        from client.core.agent import Message
        from client.panels.chat import _SystemBlock

        msg = Message(role="system_reminder", content="reminder")
        block = _SystemBlock(msg)
        try:
            assert block.is_collapsible is True
            assert block.title == "系统提醒"
            block.toggle()
            block.collapse()
            assert block._expanded is False
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_assistant_text_block_not_collapsible(self, qapp):
        """D42：不可折叠块（agent 文本）不触发 sticky 标题。"""
        from client.panels.chat import _AssistantTextBlock

        block = _AssistantTextBlock(content="hello")
        try:
            assert block.is_collapsible is False
            assert block.title == ""
            # collapse 是 no-op
            block.collapse()
        finally:
            block.deleteLater()
            qapp.processEvents()

    def test_user_bubble_not_collapsible(self, qapp):
        """D42：不可折叠块（user 消息）不触发 sticky 标题。"""
        from client.core.agent import Message
        from client.panels.chat import _UserBubble

        msg = Message(role="user", content="hi")
        block = _UserBubble(msg)
        try:
            assert block.is_collapsible is False
            assert block.title == ""
        finally:
            block.deleteLater()
            qapp.processEvents()
