"""Focused regressions for the client visual migration."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QToolButton  # noqa: E402

from client.core.app import Sidebar  # noqa: E402
from client.panels.chat import ChatPanel  # noqa: E402
from client.panels.dashboard import DashboardPanel  # noqa: E402
from client.panels.inbox import _InboxItemCard  # noqa: E402
from client.panels.keys import KeysPanel  # noqa: E402
from client.panels.loop import _LoopTaskCard  # noqa: E402
from lib.ui import apply_theme, tokens  # noqa: E402
from lib.ui.theme import build_qss  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    yield app


class _FakePanelActions:
    def _update_batch_buttons(self) -> None:
        pass

    def resolve_item(self, _item_id: str) -> None:
        pass

    def ignore_item(self, _item_id: str) -> None:
        pass

    def delete_item(self, _item_id: str) -> None:
        pass

    def run_task_now(self, _task_id: str) -> None:
        pass

    def pause_task(self, _task_id: str) -> None:
        pass

    def resume_task(self, _task_id: str) -> None:
        pass


def _assert_icon_buttons(widget, expected_count: int) -> None:
    buttons = widget.findChildren(QToolButton)
    assert len(buttons) == expected_count
    for button in buttons:
        assert button.text() == ""
        assert not button.icon().isNull()
        assert button.width() == tokens.CTRL_HEIGHT_MD
        assert button.height() == tokens.CTRL_HEIGHT_MD
        assert button.toolTip()


def test_compact_row_actions_are_icon_buttons(qapp):
    actions = _FakePanelActions()

    loop_card = _LoopTaskCard(
        {"task_id": "task-a", "action_type": "test", "enabled": True},
        actions,
    )
    _assert_icon_buttons(loop_card, 2)

    inbox_card = _InboxItemCard(
        {"id": "item-a", "title": "Inbox item", "status": "pending"},
        actions,
    )
    _assert_icon_buttons(inbox_card, 3)

    keys_panel = KeysPanel()
    key_ops = keys_panel._build_key_ops_widget("key-a")
    _assert_icon_buttons(key_ops, 4)

    loop_card.close()
    inbox_card.close()
    key_ops.close()
    keys_panel.close()


def test_sidebar_headers_and_items_have_distinct_alignment(qapp):
    sidebar = Sidebar()
    sidebar.add_category_header("Main")
    panel_row = sidebar.add_panel_item("dashboard", "D", "Overview")

    header_widget = sidebar.itemWidget(sidebar.item(0))
    assert header_widget is not None
    header_label = header_widget.findChild(QLabel)
    assert header_label is not None
    assert header_label.property("textRole") == "title"

    panel_widget = sidebar.itemWidget(sidebar.item(panel_row))
    margins = panel_widget.layout().contentsMargins()
    assert margins.top() == 0
    assert margins.bottom() == 0
    assert panel_widget._label.alignment() & Qt.AlignmentFlag.AlignVCenter

    qss = build_qss()
    assert "QListWidget#sidebar::item" in qss
    assert "border-radius: 0" in qss
    sidebar.close()


def test_dashboard_llm_summary_stays_compact(qapp):
    panel = DashboardPanel()
    panel._update_from_health({
        "version": "test",
        "uptime_seconds": 1,
        "llm_pool": {
            "total_keys": 5,
            "available_keys": 4,
            "cooldown_keys": 1,
            "expired_keys": 0,
            "free_keys": 4,
            "paid_keys": 1,
            "total_models": 14,
            "free_models": 13,
            "current_active": 2,
            "recent_calls_count": 8,
        },
    })

    detail_lines = panel._llm_pool_card._details_widget.text().splitlines()
    assert len(detail_lines) <= 3
    assert panel._activity_list.objectName() == "dashboardActivityList"
    panel.close()


def test_chat_interrupt_control_lives_in_input_bar(qapp):
    panel = ChatPanel()
    assert panel._interrupt_btn.parentWidget().objectName() == "chatInputBar"
    assert panel._refresh_btn.toolTip()
    # chat-panel-v2 T10：_copy_last_btn 已从顶栏移除（D47），改为操作按钮 hover 显示（T11 实现）
    # 此断言已删除（_copy_last_btn=None）
    panel.close()


def test_chat_switch_clears_optimistic_preview(qapp):
    panel = ChatPanel()
    panel._pending_user_preview = "same text"
    panel._switch_session("sess-does-not-exist")
    assert panel._pending_user_preview is None
    panel.close()
