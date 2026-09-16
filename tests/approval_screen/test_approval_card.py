"""审批卡片 widget 测试（Ticket 05）。

验证：
- 卡片可实例化（shell/http 两种类型）
- 倒计时归零 → 上报 timeout + 转只读态
- textChanged/keyPress 重置倒计时
- 批准/拒绝按钮提交决策 + 发射 card_closed 信号
- "收到"按钮提交 ack + 发射 card_closed 信号
- 超时态按钮可见性切换
"""

import pytest


@pytest.fixture(autouse=True)
def _qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _mock_requests(monkeypatch):
    """mock requests 避免卡片真发 HTTP。"""

    class FakeResp:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {}

        def json(self):
            return self._json

    _calls = {"post": []}

    def fake_post(url, json=None, timeout=None):
        _calls["post"].append({"url": url, "json": json})
        return FakeResp(200, {"ok": True})

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    # 让测试能检查调用记录
    pytest.mock_post_calls = _calls


def _make_shell_item(approval_id="test_001", seconds_left=180):
    return {
        "approval_id": approval_id,
        "type": "shell",
        "command": "rm -rf /tmp/test",
        "guard_reason": "destructive",
        "agent_reason": "test reason",
        "llm_opinion": "",
        "seconds_left": seconds_left,
        "status": "pending",
    }


def _wait_for(condition, timeout_ms=3000):
    """8-8 决策提交线程化后信号经 queued 连接回主线程，轮询等待其送达。

    在 worker 线程 mock（requests.post 被 monkeypatch，瞬时返回）下
    decision_done → card_closed 仍需一次事件循环派发，不能同步断言。
    """
    import time

    from PySide6.QtCore import QCoreApplication

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _make_http_item(approval_id="http_001", seconds_left=120):
    return {
        "approval_id": approval_id,
        "type": "http",
        "method": "POST",
        "path": "/exec/cmd",
        "body_preview": '{"cmd": "del /f *"}',
        "guard_reason": "destructive http",
        "agent_reason": "need to delete files",
        "seconds_left": seconds_left,
        "status": "pending",
    }


def _make_advanced_tool_item(approval_id="adv_001", seconds_left=120):
    return {
        "approval_id": approval_id,
        "type": "advanced_tool",
        "operation_id": "memory_delete",
        "params_preview": "{'key': 'important_key'}",
        "guard_reason": "危险工具 memory_delete 需用户审批",
        "agent_reason": "调用危险工具 memory_delete",
        "seconds_left": seconds_left,
        "status": "pending",
    }


class TestCardInstantiation:
    def test_shell_card_creates(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        assert card.approval_id == "test_001"
        assert card._is_timeout is False
        # 用 isHidden() 而非 isVisible()：isVisible() 要求 widget 真正渲染过，
        # offscreen 模式下未 show 的 widget 永远 isVisible()=False。
        # isHidden() 反映 setVisible(True/False) 的语义意图。
        assert not card._approve_btn.isHidden()
        assert not card._deny_btn.isHidden()
        assert card._ack_btn.isHidden()
        card.deleteLater()

    def test_http_card_creates(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_http_item())
        assert card.approval_id == "http_001"
        assert card._item_type == "http"
        card.deleteLater()

    def test_timeout_card_creates_in_timeout_state(self):
        from client.approval_panel.card import ApprovalCard

        item = _make_shell_item()
        item["status"] = "timeout"
        card = ApprovalCard(item)
        assert card._is_timeout is True
        assert card._approve_btn.isHidden()
        assert card._deny_btn.isHidden()
        assert not card._ack_btn.isHidden()
        assert card._feedback.isReadOnly()
        card.deleteLater()


class TestCountdown:
    def test_tick_decrements_remaining(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=60))
        initial = card._remaining
        card._on_tick()
        assert card._remaining == initial - 1
        card.deleteLater()

    def test_countdown_zero_triggers_timeout(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=1))
        card._on_tick()  # remaining goes to 0
        assert card._is_timeout is True
        assert card._timer.isActive() is False
        # 验证 timeout POST 被调用
        assert any(
            "timeout" in str(c.get("json", {}).get("decision", ""))
            for c in pytest.mock_post_calls["post"]
        )
        card.deleteLater()

    def test_textchanged_resets_countdown(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=60))
        # 消耗几秒
        card._on_tick()
        card._on_tick()
        assert card._remaining == 58
        # 模拟用户打字
        card._on_user_activity()
        assert card._remaining == 60  # 重置回原始值
        card.deleteLater()

    def test_keypress_resets_countdown(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=60))
        card._on_tick()
        card._on_tick()
        assert card._remaining == 58
        # 模拟按键
        event = QKeyEvent(QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier)
        card.keyPressEvent(event)
        assert card._remaining == 60
        card.deleteLater()


class TestDecisionSubmission:
    def test_approve_emits_card_closed(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        closed = []
        card.card_closed.connect(lambda aid: closed.append(aid))
        card._submit_decision("approve")
        assert _wait_for(lambda: closed == ["test_001"])
        # 验证 POST 被调用
        assert any(
            "approve" in str(c.get("json", {}).get("decision", ""))
            for c in pytest.mock_post_calls["post"]
        )
        card.deleteLater()

    def test_deny_emits_card_closed(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        closed = []
        card.card_closed.connect(lambda aid: closed.append(aid))
        card._submit_decision("deny")
        assert _wait_for(lambda: closed == ["test_001"])
        card.deleteLater()

    def test_decision_409_marks_timeout(self, monkeypatch):
        """决策返回 409（状态已变更）→ 卡片转超时态。"""

        class FakeResp409:
            status_code = 409

            def json(self):
                return {}

        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp409())
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        card._submit_decision("approve")
        assert _wait_for(lambda: card._is_timeout)
        assert not card._ack_btn.isHidden()
        card.deleteLater()


class TestAckButton:
    def test_ack_emits_card_closed(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        card._is_timeout = True
        card._update_button_visibility()
        closed = []
        card.card_closed.connect(lambda aid: closed.append(aid))
        card._ack_timeout()
        assert closed == ["test_001"]
        card.deleteLater()

    def test_ack_on_non_timeout_does_nothing(self, monkeypatch):
        """非超时态点 ack 不应关闭卡片（按钮不可见，但测试防御性）。"""

        class FakeResp409:
            status_code = 409

        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp409())
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item())
        closed = []
        card.card_closed.connect(lambda aid: closed.append(aid))
        card._ack_timeout()
        assert closed == []  # 409 不关闭
        card.deleteLater()


class TestCardUpdate:
    def test_update_from_pending_changes_remaining(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=180))
        # 模拟 server 返回更新后的 seconds_left
        card.update_from_pending({"approval_id": "test_001", "seconds_left": 100})
        assert card._remaining == 100
        assert card._progress.value() == 100
        card.deleteLater()

    def test_update_ignored_on_timeout(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=180))
        card._is_timeout = True
        card.update_from_pending({"approval_id": "test_001", "seconds_left": 50})
        # 超时态不更新
        assert card._remaining == 180
        card.deleteLater()

    def test_mark_timeout_transitions_state(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_shell_item(seconds_left=60))
        card.mark_timeout()
        assert card._is_timeout is True
        assert card._timer.isActive() is False
        assert not card._ack_btn.isHidden()
        card.deleteLater()


class TestPanelCardIntegration:
    def test_panel_adds_card_on_pending(self):
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([_make_shell_item("p_001")])
        assert "p_001" in panel._cards
        assert len(panel._cards) == 1
        panel.deleteLater()

    def test_panel_removes_card_when_pending_drops(self):
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([_make_shell_item("p_001")])
        assert "p_001" in panel._cards
        # pending 列表变空
        panel.on_pending_updated([])
        assert "p_001" not in panel._cards
        panel.deleteLater()

    def test_panel_updates_existing_card(self):
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([_make_shell_item("p_001", seconds_left=180)])
        card = panel._cards["p_001"]
        assert card._remaining == 180
        # 更新 seconds_left
        panel.on_pending_updated([_make_shell_item("p_001", seconds_left=100)])
        assert card._remaining == 100
        panel.deleteLater()

    def test_panel_handles_multiple_cards(self):
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([
            _make_shell_item("p_001"),
            _make_http_item("p_002"),
            _make_shell_item("p_003"),
        ])
        assert len(panel._cards) == 3
        assert "p_001" in panel._cards
        assert "p_002" in panel._cards
        assert "p_003" in panel._cards
        panel.deleteLater()

    def test_card_closed_signal_removes_from_panel(self):
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([_make_shell_item("p_001")])
        assert "p_001" in panel._cards
        # 模拟卡片发射 card_closed
        panel._cards["p_001"].card_closed.emit("p_001")
        assert "p_001" not in panel._cards
        panel.deleteLater()


class TestAdvancedToolCard:
    """advanced_tool 类型卡片的渲染测试（type 分支 elif）。

    验证 advanced_tool 不走 shell else 分支（避免显示空 command），
    而是正确渲染 operation_id / params_preview 等字段。
    """

    def test_advanced_tool_card_creates(self):
        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_advanced_tool_item())
        assert card.approval_id == "adv_001"
        assert card._item_type == "advanced_tool"
        card.deleteLater()

    def test_advanced_tool_card_shows_fields(self):
        """卡片预览框显示 operation_id + params_preview，不崩。"""
        from PySide6.QtWidgets import QPlainTextEdit

        from client.approval_panel.card import ApprovalCard

        card = ApprovalCard(_make_advanced_tool_item())
        try:
            edits = [e.toPlainText() for e in card.findChildren(QPlainTextEdit)]
            # operation_id 出现在预览框（agent_reason 框也会含 'memory_delete'）
            assert any("memory_delete" in t for t in edits)
            # params_preview 中的 'important_key' 只会出现在预览框
            assert any("important_key" in t for t in edits)
        finally:
            card.deleteLater()

    def test_advanced_tool_card_minimal_payload(self):
        """advanced_tool 仅 type + operation_id 也不崩。"""
        from client.approval_panel.card import ApprovalCard

        item = _make_advanced_tool_item()
        del item["params_preview"]
        del item["guard_reason"]
        del item["agent_reason"]
        card = ApprovalCard(item)
        try:
            assert card._item_type == "advanced_tool"
            # 预览框不空（至少显示工具名）
            from PySide6.QtWidgets import QPlainTextEdit

            edits = [e.toPlainText() for e in card.findChildren(QPlainTextEdit)]
            assert any("memory_delete" in t for t in edits)
        finally:
            card.deleteLater()

    def test_panel_handles_advanced_tool_card(self):
        """panel 能正常添加 advanced_tool 类型卡片。"""
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([
            _make_shell_item("p_001"),
            _make_advanced_tool_item("p_002"),
        ])
        assert len(panel._cards) == 2
        assert "p_002" in panel._cards
        assert panel._cards["p_002"]._item_type == "advanced_tool"
        panel.deleteLater()
