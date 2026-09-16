"""完整轮次判定 + 分组展开 key 稳定性测试（2026-09-13 第二轮反馈）。

- apply_turn_end_flags：操作按钮只出现在"完整 agent 轮次"的最后一条回复
  （用户一句 → agent 多次工具调用+多段输出 → 最终回复），中间回复不显示按钮。
- 会话树 header 展开状态 key 稳定性：分支/删除后数量变化不再导致分组被收回。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.types import Message  # noqa: E402
from client.panels.chat import _MessageTimeline  # noqa: E402


@pytest.fixture
def timeline(qapp):
    tl = _MessageTimeline()
    try:
        yield tl
    finally:
        tl.deleteLater()
        qapp.processEvents()


def _assistant(seq: int, content: str, tool_calls: list[dict] | None = None) -> Message:
    return Message(
        role="assistant", content=content, source="assistant",
        seq=seq, tool_calls=tool_calls,
    )


def _find_text_block(timeline: _MessageTimeline, seq: int):
    block = timeline._assistant_blocks_by_seq.get(seq)
    assert block is not None, f"seq {seq} 应已登记 assistant 块"
    return block


def test_turn_end_last_message_shows_actions(qapp, timeline):
    """user → assistant(final)：最终回复显示按钮。"""
    timeline.append_user_block(Message(role="user", content="问", seq=1))
    timeline.append_assistant_blocks(_assistant(2, "答"))
    timeline.apply_turn_end_flags({2})
    block = _find_text_block(timeline, 2)
    assert block is not None
    assert block._actions_row.isHidden() is False


def test_intermediate_reply_hides_actions(qapp, timeline):
    """assistant(tool_calls) → tool → assistant(final)：中间回复不显示按钮。"""
    timeline.append_user_block(Message(role="user", content="问", seq=1))
    timeline.append_assistant_blocks(
        _assistant(2, "中间输出", tool_calls=[{"id": "t1", "function": {"name": "ls", "arguments": "{}"}}])
    )
    timeline.append_assistant_blocks(_assistant(4, "最终回答"))
    # 判定集合由 ChatPanel 按下一条是否 user 计算：seq4 是末尾 → 轮次结束
    timeline.apply_turn_end_flags({4})
    assert _find_text_block(timeline, 2)._actions_row.isHidden() is True
    assert _find_text_block(timeline, 4)._actions_row.isHidden() is False


def test_multi_turn_each_final_reply_has_actions(qapp, timeline):
    """多轮对话：每一轮的最终回复都有按钮（seq2 与 seq4 各自轮次结束）。"""
    timeline.append_user_block(Message(role="user", content="问1", seq=1))
    timeline.append_assistant_blocks(_assistant(2, "答1"))
    timeline.append_user_block(Message(role="user", content="问2", seq=3))
    timeline.append_assistant_blocks(_assistant(4, "答2"))
    timeline.apply_turn_end_flags({2, 4})
    assert _find_text_block(timeline, 2)._actions_row.isHidden() is False
    assert _find_text_block(timeline, 4)._actions_row.isHidden() is False


def test_demote_after_new_messages(qapp, timeline):
    """原本是末尾的回复，在后续消息到达后被降级为中间回复（按钮隐藏）。"""
    timeline.append_user_block(Message(role="user", content="问", seq=1))
    timeline.append_assistant_blocks(_assistant(2, "以为的最终回答"))
    timeline.apply_turn_end_flags({2})
    assert _find_text_block(timeline, 2)._actions_row.isHidden() is False
    # agent 继续工具调用 → 新消息到达后重新判定
    timeline.append_assistant_blocks(_assistant(3, "真正的最终回答"))
    timeline.apply_turn_end_flags({3})
    assert _find_text_block(timeline, 2)._actions_row.isHidden() is True
    assert _find_text_block(timeline, 3)._actions_row.isHidden() is False


def test_clear_resets_registry(qapp, timeline):
    """clear 后 seq 登记表清空（防悬挂引用）。"""
    timeline.append_user_block(Message(role="user", content="问", seq=1))
    timeline.append_assistant_blocks(_assistant(2, "答"))
    timeline.clear()
    assert timeline._assistant_blocks_by_seq == {}


def test_event_filter_blocks_tree_tooltip(qapp, monkeypatch, tmp_path):
    """eventFilter 吞掉会话树/最近列表的 ToolTip 事件（"Loc..." 小窗口根因）。

    Qt item 视图为截断文本自动弹完整标题 tooltip；tooltip 是真实顶层窗口，
    窗口标题取应用显示名"LocalAgent 客户端"（用户看到的"Loc... 小窗口"）。
    """
    import tests.client_ui.test_chat_panel_v2_hover_buttons as hb

    panel = hb._make_chat_panel(monkeypatch, tmp_path)
    try:
        tree_vp = panel._session_tree.viewport()
        recent_vp = panel._start_page._recent_list.viewport()
        for viewport in (tree_vp, recent_vp):
            evt = QEvent(QEvent.Type.ToolTip)
            assert panel.eventFilter(viewport, evt) is True, "ToolTip 事件应被吞掉"
            evt2 = QEvent(QEvent.Type.HelpRequest)
            assert panel.eventFilter(viewport, evt2) is True, "HelpRequest 事件应被吞掉"
        # 其他对象不受影响（走 super()，不吞）
        other = QEvent(QEvent.Type.ToolTip)
        assert panel.eventFilter(panel, other) is False
    finally:
        hb._cleanup_panel(panel, qapp)


from PySide6.QtCore import QEvent  # noqa: E402  (测试文件底部导入，供上方用例使用)
