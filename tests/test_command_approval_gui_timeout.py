"""command_approval_gui 审批弹窗内部超时机制测试（Ticket 03）。

覆盖：
- _reset_countdown 重置倒计时
- _on_tick 递减 + 归零自动 deny
- textChanged 信号触发重置
- keyPressEvent 触发重置
- payload.gui_timeout_seconds 读取
"""

import os

import pytest

# 确保离屏模式（conftest.py 已设，此处兜底）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from server.command_approval_gui import ApprovalDialog


@pytest.fixture
def qapp():
    """每个测试独立的 QApplication（offscreen 模式）。"""
    app = QApplication.instance() or QApplication([])
    yield app
    # 清理：关闭所有窗口
    app.closeAllWindows()


@pytest.fixture
def dialog(qapp):
    """创建一个带 10s 超时的 ApprovalDialog（短超时便于测试）。"""
    payload = {
        "type": "shell",
        "command": "rm -rf /tmp/test",
        "guard_reason": "破坏性命令",
        "agent_reason": "测试用途",
        "gui_timeout_seconds": 10,
    }
    d = ApprovalDialog(payload)
    yield d
    d.close()


class TestApprovalDialogTimeout:
    def test_initial_state(self, dialog):
        """初始化后倒计时 = 总时长。"""
        assert dialog._timeout_seconds == 10
        assert dialog._remaining == 10
        assert dialog.result["decision"] == "deny"  # 默认 deny
        assert dialog.result["feedback"] == ""

    def test_reset_countdown(self, dialog):
        """_reset_countdown 恢复完整倒计时。"""
        # 模拟倒计时走了一些
        dialog._remaining = 3
        dialog._reset_countdown()
        assert dialog._remaining == 10

    def test_on_tick_decrements(self, dialog):
        """_on_tick 每次减 1。"""
        dialog._remaining = 10
        dialog._on_tick()
        assert dialog._remaining == 9

    def test_on_tick_zero_triggers_deny(self, dialog):
        """倒计时归零自动 deny + 超时 feedback。"""
        dialog._remaining = 1
        dialog._on_tick()
        assert dialog._remaining == 0
        assert dialog.result["decision"] == "deny"
        assert dialog.result["feedback"] == "（超时自动拒绝）"

    def test_text_changed_resets_countdown(self, dialog):
        """feedback 框 textChanged 信号重置倒计时。"""
        # 模拟倒计时走了一些
        dialog._remaining = 3
        # 模拟用户输入（触发 textChanged 信号）
        dialog._feedback.setPlainText("用户正在打字...")
        # textChanged 信号应已触发 _reset_countdown
        assert dialog._remaining == 10

    def test_key_press_event_resets_countdown(self, dialog):
        """任意按键重置倒计时（兜底机制）。"""
        dialog._remaining = 3
        # 模拟按键事件
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
        dialog.keyPressEvent(event)
        assert dialog._remaining == 10

    def test_finish_approve(self, dialog):
        """用户点批准：result 更新 + timer 停止。"""
        dialog._feedback.setPlainText("允许执行")
        dialog._finish("approve")
        assert dialog.result["decision"] == "approve"
        assert dialog.result["feedback"] == "允许执行"
        assert not dialog._timer.isActive()

    def test_finish_deny(self, dialog):
        """用户点拒绝：result 更新 + timer 停止。"""
        dialog._feedback.setPlainText("太危险了")
        dialog._finish("deny")
        assert dialog.result["decision"] == "deny"
        assert dialog.result["feedback"] == "太危险了"
        assert not dialog._timer.isActive()

    def test_default_timeout_180_when_not_specified(self, qapp):
        """payload 未传 gui_timeout_seconds 时默认 180s。"""
        payload = {"type": "shell", "command": "ls"}
        d = ApprovalDialog(payload)
        assert d._timeout_seconds == 180
        d.close()

    def test_progress_bar_updates_on_tick(self, dialog):
        """进度条随倒计时更新。"""
        initial_value = dialog._progress.value()
        assert initial_value == 10
        dialog._on_tick()
        assert dialog._progress.value() == 9

    def test_progress_bar_resets_on_text_changed(self, dialog):
        """textChanged 后进度条恢复满值。"""
        dialog._remaining = 3
        dialog._progress.setValue(3)
        dialog._feedback.setPlainText("打字中")
        assert dialog._progress.value() == 10

    def test_multiple_ticks_then_reset(self, dialog):
        """多次 tick 后 textChanged 重置，倒计时恢复。"""
        for _ in range(5):
            dialog._on_tick()
        assert dialog._remaining == 5
        # 用户开始打字
        dialog._feedback.setPlainText("正在思考...")
        assert dialog._remaining == 10
        # 继续 tick
        dialog._on_tick()
        assert dialog._remaining == 9


class TestRunGuiDialogPayload:
    """验证 run_gui_dialog 传 gui_timeout_seconds 给子进程。"""

    def test_payload_includes_gui_timeout_seconds(self, monkeypatch):
        """run_gui_dialog 构造的 payload 含 gui_timeout_seconds。"""
        import asyncio
        import json

        from server import command_guard

        # mock get_command_guard_config 返回自定义 gui_timeout
        def mock_config():
            return {"gui_timeout_seconds": 120, "enabled": True}

        monkeypatch.setattr(command_guard, "get_command_guard_config", mock_config)

        # 捕获传给 subprocess 的 payload
        captured_payload = {}

        class MockProc:
            returncode = 0

            def kill(self):
                pass

            async def communicate(self, input=None):
                captured_payload["data"] = input
                return (json.dumps({"decision": "deny", "feedback": ""}).encode(), b"")

            async def wait(self):
                return 0

        async def mock_create(*args, **kwargs):
            return MockProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create)

        asyncio.run(command_guard.run_gui_dialog({"type": "shell", "command": "ls"}))

        # 验证 payload 含 gui_timeout_seconds
        sent = json.loads(captured_payload["data"].decode())
        assert sent["gui_timeout_seconds"] == 120
        assert sent["type"] == "shell"
        assert sent["command"] == "ls"


class TestAdvancedToolBranch:
    """advanced_tool 类型 payload 的渲染测试（type 分支 elif）。

    验证 advanced_tool payload 不走 shell else 分支（避免显示空 command），
    而是正确渲染 operation_id / params_preview 等字段。
    """

    def test_advanced_tool_dialog_shows_fields(self, qapp):
        """advanced_tool payload 渲染 operation_id + params_preview，不崩。"""
        from PySide6.QtWidgets import QLabel, QPlainTextEdit

        payload = {
            "type": "advanced_tool",
            "operation_id": "memory_delete",
            "params_preview": "{'key': 'important_key'}",
            "guard_reason": "危险工具 memory_delete 需用户审批",
            "agent_reason": "调用危险工具 memory_delete",
            "gui_timeout_seconds": 10,
        }
        d = ApprovalDialog(payload)
        try:
            # 验证标题标签存在（区别于 shell 的"准备执行的完整命令："）
            labels = [lbl.text() for lbl in d.findChildren(QLabel)]
            assert any("待审批的网关工具调用" in t for t in labels)
            # 验证 operation_id 出现在某个 QPlainTextEdit 中
            edits = [e.toPlainText() for e in d.findChildren(QPlainTextEdit)]
            assert any("memory_delete" in t for t in edits)
            # params_preview 中的 'important_key' 只会出现在预览框
            assert any("important_key" in t for t in edits)
        finally:
            d.close()

    def test_advanced_tool_dialog_minimal_payload(self, qapp):
        """advanced_tool 仅 type + operation_id 也不崩。"""
        payload = {
            "type": "advanced_tool",
            "operation_id": "shutdown_server",
            "gui_timeout_seconds": 10,
        }
        d = ApprovalDialog(payload)
        try:
            assert d.result["decision"] == "deny"  # 默认 deny
            assert d._timeout_seconds == 10
        finally:
            d.close()

    def test_advanced_tool_does_not_show_shell_label(self, qapp):
        """advanced_tool 不应显示 shell 分支的'准备执行的完整命令：'标签。"""
        from PySide6.QtWidgets import QLabel

        payload = {
            "type": "advanced_tool",
            "operation_id": "apikey_delete_key",
            "params_preview": "{'key_id': 'k_001'}",
            "gui_timeout_seconds": 10,
        }
        d = ApprovalDialog(payload)
        try:
            labels = [lbl.text() for lbl in d.findChildren(QLabel)]
            assert not any("准备执行的完整命令" in t for t in labels)
        finally:
            d.close()
