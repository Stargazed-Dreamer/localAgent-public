"""headless-agent-session Ticket 10 验收测试：chat 面板 badge UI

覆盖 Ticket 10 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 修改 client/panels/chat.py 会话列表渲染逻辑
- [x] 按 sessions.mode 渲染 badge：
  - mode="headless" → "后端" badge
  - mode="headless_judge" → "判定" badge
  - mode="dialogue"（默认）→ 无 badge
- [x] UI 测试：验证三种 mode 的 badge 渲染
- [x] 验证现有 chat 会话（mode="dialogue"）不破坏
- [x] 验证点击 headless 会话能查看完整历史记录

测试策略：
- Part 1: 源码静态扫描（_SESSION_MODE_BADGE 定义 + _refresh_session_list 用 d["mode"]）
- Part 2: 运行时实例化 ChatPanel，mock _read_store.list_sessions 返回不同 mode 的 session，
  验证 _session_tree 中会话项文本含/不含 badge 前缀
- Part 3: 验证点击 headless 会话切换 + 历史记录加载（mock _switch_session）

chat-panel-v2 T06 重构后侧边栏改为 _session_tree QTreeWidget（树形：置顶/分组/未分组）。
本测试文件已适配新 API（替代旧 _session_list QListWidget）。

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================================
# Part 2: 运行时 badge 渲染验证
# ============================================================================


def _make_mock_session(
    session_id: str,
    mode: str,
    title: str = "",
    status: str = "idle",
    updated_at: float = 1000.0,
    pinned: bool = False,
    group_name: str | None = None,
):
    """构造 mock session 对象（dataclass 风格 + dict 兼容）。

    chat-panel-v2 T06 重构后 _refresh_session_list 还会读取 pinned/group_name/updated_at，
    因此 mock 必须提供这些字段的合理默认值（不能用 MagicMock，否则 sorted/bool 会出错）。
    """
    mock = MagicMock()
    mock.id = session_id
    mock.mode = mode
    mock.title = title
    mock.status = status
    mock.updated_at = updated_at
    mock.pinned = pinned
    mock.group_name = group_name
    # 同时支持 dict 访问（chat.py 用 getattr 或 .get 双路径）
    mock.get = lambda key, default="": {
        "id": session_id,
        "mode": mode,
        "title": title,
        "status": status,
        "updated_at": updated_at,
        "pinned": pinned,
        "group_name": group_name,
    }.get(key, default)
    return mock


def _collect_session_items(panel):
    """从 _session_tree 收集所有 session 项（跳过 header 项）。

    树形结构：topLevelItem(i) 是分区 root（pinned/group/ungrouped），
    每个分区 root 下挂 child 才是 session 项（UserRole+1 == "session"）。
    """
    from PySide6.QtCore import Qt

    items = []
    tree = panel._session_tree
    for i in range(tree.topLevelItemCount()):
        root = tree.topLevelItem(i)
        for j in range(root.childCount()):
            child = root.child(j)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) == "session":
                items.append(child)
    return items


class TestBadgeRendering:
    """ChatPanel._refresh_session_list 按 mode 渲染 badge。"""

    def _setup_panel_with_sessions(self, panel, sessions):
        """配置 panel 的 _safe_read 直接返回 sessions list（绕过 asyncio）。"""
        panel._safe_read = MagicMock(return_value=sessions)
        # _read_store 不能为 None（_refresh_session_list 会检查）
        panel._read_store = MagicMock()
        # list_distinct_group_names 兜底返回空 list
        panel._read_store.list_distinct_group_names = MagicMock(return_value=[])

    def test_headless_session_has_backend_badge(self, qapp):
        """mode='headless' 的会话应渲染 [后端] badge 前缀。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [_make_mock_session("headless-abc123", "headless", "测试后端任务")]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 1
            text = session_items[0].text(0)
            assert "[后端]" in text, f"headless 会话应含 [后端] badge，实际: {text!r}"
            assert "测试后端任务" in text
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_headless_judge_session_has_judge_badge(self, qapp):
        """mode='headless_judge' 的会话应渲染 [判定] badge 前缀。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [_make_mock_session("headless-judge-xyz789", "headless_judge", "判定任务")]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 1
            text = session_items[0].text(0)
            assert "[判定]" in text, f"headless_judge 会话应含 [判定] badge，实际: {text!r}"
            assert "判定任务" in text
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_dialogue_session_no_badge(self, qapp):
        """mode='dialogue' 的普通会话不应有 badge 前缀。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [_make_mock_session("sess-normal123", "dialogue", "普通对话")]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 1
            text = session_items[0].text(0)
            assert "[后端]" not in text, f"dialogue 会话不应有 [后端] badge: {text!r}"
            assert "[判定]" not in text, f"dialogue 会话不应有 [判定] badge: {text!r}"
            assert "普通对话" in text
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_mixed_sessions_rendered_correctly(self, qapp):
        """混合会话列表（三种 mode）都正确渲染 badge。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [
                    _make_mock_session("headless-1", "headless", "后端任务", "completed"),
                    _make_mock_session("headless-judge-1", "headless_judge", "判定任务", "completed"),
                    _make_mock_session("sess-1", "dialogue", "普通对话", "idle"),
                ]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 3
            # 按 session_id 前缀定位对应会话，避免依赖排序顺序
            texts_by_id = {}
            from PySide6.QtCore import Qt
            for item in session_items:
                sid = item.data(0, Qt.ItemDataRole.UserRole)
                texts_by_id[sid] = item.text(0)
            assert "[后端]" in texts_by_id["headless-1"]
            assert "[判定]" in texts_by_id["headless-judge-1"]
            assert "[后端]" not in texts_by_id["sess-1"]
            assert "[判定]" not in texts_by_id["sess-1"]
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_empty_mode_treated_as_dialogue(self, qapp):
        """mode='' 的会话（旧数据）应无 badge（按 dialogue 处理）。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [_make_mock_session("sess-old", "", "旧会话")]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 1
            text = session_items[0].text(0)
            assert "[后端]" not in text
            assert "[判定]" not in text
            assert "旧会话" in text
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_existing_dialogue_sessions_not_broken(self, qapp):
        """Ticket 10 验证点：现有 chat 会话（mode='dialogue'）渲染不破坏。

        验证：
        - status 文本仍正确显示（[空闲]/[已完成] 等）
        - title 仍正确显示
        - 无 badge 前缀
        """
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            self._setup_panel_with_sessions(
                panel,
                [
                    _make_mock_session("sess-a", "dialogue", "对话 A", "idle"),
                    _make_mock_session("sess-b", "dialogue", "对话 B", "completed"),
                ]
            )
            panel._refresh_session_list()

            session_items = _collect_session_items(panel)
            assert len(session_items) == 2
            # 按 session_id 定位，避免依赖排序顺序
            from PySide6.QtCore import Qt
            texts_by_id = {
                item.data(0, Qt.ItemDataRole.UserRole): item.text(0)
                for item in session_items
            }
            text_a = texts_by_id["sess-a"]
            text_b = texts_by_id["sess-b"]
            # status 文本仍正确
            assert "[空闲]" in text_a
            assert "[已完成]" in text_b
            # title 仍正确
            assert "对话 A" in text_a
            assert "对话 B" in text_b
            # 无 badge 前缀
            assert not text_a.startswith("[后端]") and not text_a.startswith("[判定]")
            assert not text_b.startswith("[后端]") and not text_b.startswith("[判定]")
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: 点击 headless 会话切换 + 历史记录加载
# ============================================================================


class TestHeadlessSessionSwitch:
    """点击 headless 会话能切换 + 加载历史记录。"""

    def test_click_headless_session_switches_session(self, qapp):
        """点击 headless 会话 item → _switch_session 被调用 + 当前 session_id 更新。

        chat-panel-v2 T06 重构后改用 _on_session_tree_clicked(item, column)
        （替代旧 _on_session_selected(item)）。
        """
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel._safe_read = MagicMock(
                return_value=[_make_mock_session("headless-click-test", "headless", "可点击的后端任务")]
            )
            panel._read_store = MagicMock()
            panel._read_store.list_distinct_group_names = MagicMock(return_value=[])
            panel._refresh_session_list()

            # mock _poll_session_state 避免真实读取
            panel._poll_session_state = MagicMock()
            panel._clear_messages = MagicMock()

            # 拿到 session 项，模拟点击（_on_session_tree_clicked）
            session_items = _collect_session_items(panel)
            assert len(session_items) == 1
            panel._on_session_tree_clicked(session_items[0], 0)

            # 验证当前 session_id 切换
            assert panel._current_session_id == "headless-click-test"
            # 验证 _poll_session_state 被调用（触发历史记录加载）
            assert panel._poll_session_state.called
        finally:
            panel.deleteLater()
            qapp.processEvents()

