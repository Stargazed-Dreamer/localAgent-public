"""chat-panel-v2 Ticket 06：侧边栏分组 + 置顶 + 三点菜单 + 搜索

覆盖 T06 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] 置顶区：置顶会话按 updated_at 倒序展示
- [x] 分组列表：分组名 UTF-8 顺序排序，组内按 updated_at 倒序
- [x] 会话项三点菜单：删除/重命名/移动到分组/置顶-取消置顶（导出在 T11）
- [x] 新建分组：三点菜单"移动到分组"弹分组选择 + "新建分组"选项
- [x] 搜索框：实时按标题过滤置顶区 + 分组列表
- [x] 开始页新建会话时选分组归属
- [x] 行内重命名（QLineEdit Enter 确认 / Esc 取消）

测试策略：
- Part 1: EventStore 数据层（update_session_group/pin + list_distinct_group_names + Session 字段往返）
- Part 2: ChatPanel UI 层（_refresh_session_list 树形渲染 + 搜索过滤 + 三点菜单动作 + 行内重命名）
- Part 3: _StartPage 分组选择器（refresh_groups + _current_group_selection）

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式（参考 test_chat_panel_v2_start_page.py）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))



# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _force_cleanup_qt_state_after_each_test(qapp):
    """autouse: 每个测试后强制清理 Qt 累积状态（参考 test_chat_panel_v2_shutdown_recovery.py）。"""
    import gc

    from PySide6.QtCore import QCoreApplication

    yield
    try:
        qapp.closeAllWindows()
    except Exception:
        pass
    for _ in range(3):
        try:
            QCoreApplication.sendPostedEvents(None, 0)
        except Exception:
            break
    gc.collect()


def _run(coro):
    """同步执行 async coroutine。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _open_store(db_path: str):
    """打开一个独立 EventStore。"""
    from client.core.agent import EventStore

    s = EventStore(db_path=db_path)
    s.init()
    return s


def _make_chat_panel(db_path: str, monkeypatch):
    """构造一个 ChatPanel 实例（patch DEFAULT_DB_PATH 为临时路径）。

    调用方负责 panel.deleteLater() + qapp.processEvents() 清理。
    """
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
    return ChatPanel()


def _cleanup_panel(panel, qapp):
    """清理 panel 资源（停定时器 + deleteLater）。"""
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
# Part 1: EventStore 数据层
# ============================================================================


class TestEventStoreGroupPinned:
    """D6/D10：EventStore sessions 表 group_name + pinned CRUD。"""

    def test_session_has_group_name_and_pinned_fields(self):
        """Session dataclass 应有 group_name/pinned 字段（T01 加列）。"""
        from client.core.agent import Session

        s = Session(id="x", title="t")
        assert hasattr(s, "group_name"), "Session 应有 group_name 字段"
        assert hasattr(s, "pinned"), "Session 应有 pinned 字段"
        assert s.group_name is None, "默认 group_name = None（未分组）"
        assert s.pinned is False, "默认 pinned = False"

    def test_update_session_group_sets_group_name(self, tmp_path):
        """update_session_group 写入 group_name。"""
        db = str(tmp_path / "test_group.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1", title="t1"))
            _run(s.update_session_group("sess-1", "工作"))
            sess = _run(s.get_session("sess-1"))
            assert sess is not None
            assert sess.group_name == "工作"
        finally:
            s.close()

    def test_update_session_group_none_clears_group(self, tmp_path):
        """group_name=None 或 '' 取消分组。"""
        db = str(tmp_path / "test_group_clear.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1"))
            _run(s.update_session_group("sess-1", "工作"))
            # 用空字符串取消
            _run(s.update_session_group("sess-1", ""))
            sess = _run(s.get_session("sess-1"))
            assert sess.group_name is None
            # 用 None 取消
            _run(s.update_session_group("sess-1", "工作"))
            _run(s.update_session_group("sess-1", None))
            sess = _run(s.get_session("sess-1"))
            assert sess.group_name is None
        finally:
            s.close()

    def test_update_session_pinned_true_false(self, tmp_path):
        """update_session_pinned 切换置顶状态。"""
        db = str(tmp_path / "test_pin.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1"))
            # 默认 False
            sess = _run(s.get_session("sess-1"))
            assert sess.pinned is False
            # 置顶
            _run(s.update_session_pinned("sess-1", True))
            sess = _run(s.get_session("sess-1"))
            assert sess.pinned is True
            # 取消置顶
            _run(s.update_session_pinned("sess-1", False))
            sess = _run(s.get_session("sess-1"))
            assert sess.pinned is False
        finally:
            s.close()

    def test_list_sessions_returns_group_and_pinned(self, tmp_path):
        """list_sessions 返回的 Session 含 group_name/pinned 字段。"""
        db = str(tmp_path / "test_list.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1", title="t1"))
            _run(s.update_session_group("sess-1", "A"))
            _run(s.update_session_pinned("sess-1", True))
            _run(s.create_session(session_id="sess-2", title="t2"))

            sessions = _run(s.list_sessions())
            by_id = {x.id: x for x in sessions}
            assert by_id["sess-1"].group_name == "A"
            assert by_id["sess-1"].pinned is True
            assert by_id["sess-2"].group_name is None
            assert by_id["sess-2"].pinned is False
        finally:
            s.close()

    def test_list_distinct_group_names_utf8_sorted(self, tmp_path):
        """list_distinct_group_names 返回非空分组名（UTF-8 排序，不含 NULL）。"""
        db = str(tmp_path / "test_distinct.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1"))
            _run(s.create_session(session_id="sess-2"))
            _run(s.create_session(session_id="sess-3"))
            _run(s.create_session(session_id="sess-4"))
            _run(s.update_session_group("sess-1", "工作"))
            _run(s.update_session_group("sess-2", "学习"))
            _run(s.update_session_group("sess-3", "Apple"))
            # sess-4 未分组
            groups = _run(s.list_distinct_group_names())
            # UTF-8 顺序：Apple < 学习 < 工作（按字节序）
            assert groups == ["Apple", "学习", "工作"], f"实际：{groups}"
        finally:
            s.close()

    def test_list_distinct_group_names_excludes_null(self, tmp_path):
        """未分组的会话不进 list_distinct_group_names。"""
        db = str(tmp_path / "test_null.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1"))
            _run(s.create_session(session_id="sess-2"))
            _run(s.update_session_group("sess-1", "G1"))
            # sess-2 未分组
            groups = _run(s.list_distinct_group_names())
            assert groups == ["G1"]
        finally:
            s.close()

    def test_update_session_group_refreshes_updated_at(self, tmp_path):
        """update_session_group 同步刷新 updated_at（影响排序）。"""
        db = str(tmp_path / "test_ts.db")
        s = _open_store(db)
        try:
            _run(s.create_session(session_id="sess-1"))
            old = _run(s.get_session("sess-1"))
            # 等一点时间确保 timestamp 不同
            import time as _t

            _t.sleep(0.05)
            _run(s.update_session_group("sess-1", "G1"))
            new = _run(s.get_session("sess-1"))
            assert new.updated_at > old.updated_at
        finally:
            s.close()


# ============================================================================
# Part 2: ChatPanel UI 层 — 侧边栏树形渲染
# ============================================================================


class TestSidebarTreeRendering:
    """D4/D6/D13：_refresh_session_list 渲染置顶区 + 分组列表 + 未分组。"""

    def test_session_tree_is_qtreewidget(self, qapp, tmp_path, monkeypatch):
        """ChatPanel 应使用 QTreeWidget（非 QListWidget）渲染会话列表。"""
        from PySide6.QtWidgets import QTreeWidget


        db = str(tmp_path / "test_tree.db")
        panel = _make_chat_panel(db, monkeypatch)
        try:
            assert isinstance(panel._session_tree, QTreeWidget)
        finally:
            _cleanup_panel(panel, qapp)

    def test_filter_input_exists(self, qapp, tmp_path, monkeypatch):
        """侧边栏应有搜索框 QLineEdit + textChanged 信号连接。"""
        from PySide6.QtWidgets import QLineEdit


        db = str(tmp_path / "test_filter.db")
        panel = _make_chat_panel(db, monkeypatch)
        try:
            assert isinstance(panel._session_filter_input, QLineEdit)
            # 占位符含"搜索"
            assert "搜索" in (panel._session_filter_input.placeholderText() or "")
        finally:
            _cleanup_panel(panel, qapp)

    def test_pinned_sessions_rendered_under_pinned_header(self, qapp, tmp_path, monkeypatch):
        """置顶会话应在'📌 置顶'分区下。"""

        db = str(tmp_path / "test_pin_render.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-pin-1", title="置顶会话1"))
        _run(s.update_session_pinned("sess-pin-1", True))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            # 找置顶分区
            pinned_header = None
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                text = item.text(0)
                if "置顶" in text:
                    pinned_header = item
                    break
            assert pinned_header is not None, "应有置顶分区标题"
            # 置顶分区下应有 1 个会话子项
            assert pinned_header.childCount() == 1
            child = pinned_header.child(0)
            assert child.data(0, 0x100) == "sess-pin-1"  # UserRole = 0x100
        finally:
            _cleanup_panel(panel, qapp)

    def test_grouped_sessions_rendered_under_group_header(self, qapp, tmp_path, monkeypatch):
        """分组会话应在'📁 分组名'分区下。"""

        db = str(tmp_path / "test_group_render.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-g1", title="g1会话"))
        _run(s.update_session_group("sess-g1", "工作"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            # 找"📁 工作"分区
            group_header = None
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                if "工作" in item.text(0):
                    group_header = item
                    break
            assert group_header is not None, "应有'工作'分组标题"
            assert group_header.childCount() == 1
        finally:
            _cleanup_panel(panel, qapp)

    def test_ungrouped_sessions_rendered_under_ungrouped_header(
        self, qapp, tmp_path, monkeypatch
    ):
        """未分组会话应在'📪 未分组'分区下。"""

        db = str(tmp_path / "test_ungrouped.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-u1", title="未分组会话"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            ungrouped_header = None
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                if "未分组" in item.text(0):
                    ungrouped_header = item
                    break
            assert ungrouped_header is not None, "应有'未分组'分区标题"
            assert ungrouped_header.childCount() == 1
        finally:
            _cleanup_panel(panel, qapp)

    def test_groups_sorted_utf8_order(self, qapp, tmp_path, monkeypatch):
        """D6：分组名按 UTF-8 顺序排序（Apple < 学习 < 工作）。"""

        db = str(tmp_path / "test_group_order.db")
        s = _open_store(db)
        for sid, group in [
            ("sess-1", "工作"),
            ("sess-2", "学习"),
            ("sess-3", "Apple"),
        ]:
            _run(s.create_session(session_id=sid, title=f"t-{sid}"))
            _run(s.update_session_group(sid, group))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            # 收集所有"📁 分组名"标题（顺序）
            group_names_in_order = []
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                text = item.text(0)
                if text.startswith("📁"):
                    # 提取分组名（去掉前缀和括号计数）
                    name = text.replace("📁 ", "").split(" (")[0]
                    group_names_in_order.append(name)
            assert group_names_in_order == ["Apple", "学习", "工作"], (
                f"分组顺序应为 UTF-8 字节序，实际：{group_names_in_order}"
            )
        finally:
            _cleanup_panel(panel, qapp)

    def test_pinned_takes_priority_over_group(self, qapp, tmp_path, monkeypatch):
        """D6：pinned=True 的会话即使在分组中，也归置顶区。"""

        db = str(tmp_path / "test_priority.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-pg", title="pg"))
        _run(s.update_session_group("sess-pg", "工作"))
        _run(s.update_session_pinned("sess-pg", True))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            # 置顶区应有 1 项
            pinned_header = None
            group_header = None
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                t = item.text(0)
                if "置顶" in t:
                    pinned_header = item
                elif "工作" in t:
                    group_header = item
            assert pinned_header is not None and pinned_header.childCount() == 1
            # 分组"工作"应不存在（无会话）
            assert group_header is None, "置顶会话不应同时在分组下"
        finally:
            _cleanup_panel(panel, qapp)

    def test_intra_group_sorted_by_updated_at_desc(self, qapp, tmp_path, monkeypatch):
        """D6：组内会话按 updated_at 倒序。"""

        db = str(tmp_path / "test_intra.db")
        s = _open_store(db)
        # 创建顺序 t1 < t2 < t3（updated_at 自然递增）
        _run(s.create_session(session_id="sess-old", title="旧"))
        _run(s.update_session_group("sess-old", "G"))
        import time as _t

        _t.sleep(0.05)
        _run(s.create_session(session_id="sess-mid", title="中"))
        _run(s.update_session_group("sess-mid", "G"))
        _t.sleep(0.05)
        _run(s.create_session(session_id="sess-new", title="新"))
        _run(s.update_session_group("sess-new", "G"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            group_header = None
            for i in range(panel._session_tree.topLevelItemCount()):
                item = panel._session_tree.topLevelItem(i)
                if "G" in item.text(0):
                    group_header = item
                    break
            assert group_header is not None
            # 倒序：新 → 中 → 旧
            assert group_header.childCount() == 3
            assert group_header.child(0).data(0, 0x100) == "sess-new"
            assert group_header.child(1).data(0, 0x100) == "sess-mid"
            assert group_header.child(2).data(0, 0x100) == "sess-old"
        finally:
            _cleanup_panel(panel, qapp)


class TestSidebarSearchFilter:
    """D15：搜索框实时按标题过滤。"""

    def test_filter_text_filters_sessions(self, qapp, tmp_path, monkeypatch):
        """搜索框输入文本 → 仅显示标题包含该文本的会话。"""

        db = str(tmp_path / "test_search.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="股票分析"))
        _run(s.create_session(session_id="sess-2", title="记账任务"))
        _run(s.create_session(session_id="sess-3", title="股票监控"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            # 输入"股票"
            panel._on_session_filter_changed("股票")
            # 收集所有会话子项
            found_sids = []
            it = panel._session_tree.invisibleRootItem()
            for i in range(it.childCount()):
                top = it.child(i)
                for j in range(top.childCount()):
                    child = top.child(j)
                    if child.data(0, 0x101) == "session":  # UserRole+1 = "session"
                        found_sids.append(child.data(0, 0x100))
            assert set(found_sids) == {"sess-1", "sess-3"}, (
                f"过滤应只显示'股票'相关会话，实际：{found_sids}"
            )
        finally:
            _cleanup_panel(panel, qapp)

    def test_filter_empty_shows_all(self, qapp, tmp_path, monkeypatch):
        """空搜索框 → 显示全部会话。"""

        db = str(tmp_path / "test_search_empty.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="aaa"))
        _run(s.create_session(session_id="sess-2", title="bbb"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._on_session_filter_changed("")
            count = 0
            it = panel._session_tree.invisibleRootItem()
            for i in range(it.childCount()):
                top = it.child(i)
                count += top.childCount()
            assert count == 2, f"空过滤应显示全部 2 个会话，实际 {count}"
        finally:
            _cleanup_panel(panel, qapp)

    def test_filter_case_insensitive(self, qapp, tmp_path, monkeypatch):
        """搜索大小写不敏感。"""

        db = str(tmp_path / "test_ci.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="Apple Pie"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._on_session_filter_changed("apple")
            count = 0
            it = panel._session_tree.invisibleRootItem()
            for i in range(it.childCount()):
                top = it.child(i)
                count += top.childCount()
            assert count == 1, "大小写不敏感应匹配"
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 3: 三点菜单动作
# ============================================================================


class TestSessionContextMenuActions:
    """D6/D17/D18：会话项三点菜单动作。"""

    def test_toggle_pin_session(self, qapp, tmp_path, monkeypatch):
        """_toggle_pin_session 切换置顶状态。"""

        db = str(tmp_path / "test_toggle_pin.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._toggle_pin_session("sess-1", currently_pinned=False)
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.pinned is True
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_move_session_to_existing_group(self, qapp, tmp_path, monkeypatch):
        """_move_session_to_group 移到已有分组（mock QInputDialog.getItem 返回已有分组）。"""


        db = str(tmp_path / "test_move.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1"))
        # 预置一个已有分组
        _run(s.create_session(session_id="sess-pre"))
        _run(s.update_session_group("sess-pre", "已有分组"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            # mock QInputDialog.getItem 返回"已有分组"
            from PySide6.QtWidgets import QInputDialog

            monkeypatch.setattr(
                QInputDialog, "getItem",
                lambda *args, **kwargs: ("已有分组", True),
            )
            panel._move_session_to_group("sess-1", current_group=None)
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.group_name == "已有分组"
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_move_session_to_new_group(self, qapp, tmp_path, monkeypatch):
        """_move_session_to_group 选'新建分组' → 弹 QInputDialog.getText → 创建新分组。"""

        db = str(tmp_path / "test_new_group.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            from PySide6.QtWidgets import QInputDialog

            call_count = {"n": 0}

            def fake_get_item(*args, **kwargs):
                call_count["n"] += 1
                return ("（新建分组...）", True)

            def fake_get_text(*args, **kwargs):
                return ("新建分组A", True)

            monkeypatch.setattr(QInputDialog, "getItem", fake_get_item)
            monkeypatch.setattr(QInputDialog, "getText", fake_get_text)

            panel._move_session_to_group("sess-1", current_group=None)
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.group_name == "新建分组A"
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_move_session_to_ungrouped(self, qapp, tmp_path, monkeypatch):
        """_move_session_to_group 选'移到未分组' → group_name = None。"""

        db = str(tmp_path / "test_to_ungrouped.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1"))
        _run(s.update_session_group("sess-1", "G"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            from PySide6.QtWidgets import QInputDialog

            monkeypatch.setattr(
                QInputDialog, "getItem",
                lambda *args, **kwargs: ("（移到未分组）", True),
            )
            panel._move_session_to_group("sess-1", current_group="G")
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.group_name is None
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_delete_session_with_confirm_yes(self, qapp, tmp_path, monkeypatch):
        """_delete_session_with_confirm 用户确认 → 真删（D18）。"""

        db = str(tmp_path / "test_delete_yes.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="t1"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            # mock QMessageBox.question 返回 Yes
            from PySide6.QtWidgets import QMessageBox

            monkeypatch.setattr(
                QMessageBox, "question",
                lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
            )
            panel._delete_session_with_confirm("sess-1")
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess is None, "确认 Yes 后会话应已删除"
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_delete_session_with_confirm_no(self, qapp, tmp_path, monkeypatch):
        """_delete_session_with_confirm 用户取消 → 不删（D18）。"""

        db = str(tmp_path / "test_delete_no.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            from PySide6.QtWidgets import QMessageBox

            monkeypatch.setattr(
                QMessageBox, "question",
                lambda *args, **kwargs: QMessageBox.StandardButton.No,
            )
            panel._delete_session_with_confirm("sess-1")
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess is not None, "取消 No 后会话应保留"
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 4: 行内重命名（D19）
# ============================================================================


class TestInlineRename:
    """D19：双击会话项 → 行内重命名（Enter 提交 / Esc 取消）。"""

    def test_begin_rename_attaches_line_edit(self, qapp, tmp_path, monkeypatch):
        """_begin_rename 挂载 QLineEdit 到 item。"""
        from PySide6.QtWidgets import QLineEdit


        db = str(tmp_path / "test_rename_begin.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="原标题"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            item = panel._find_session_item("sess-1")
            assert item is not None
            panel._begin_rename(item, "sess-1")
            assert panel._renaming_sid == "sess-1"
            assert panel._rename_editor is not None
            assert isinstance(panel._rename_editor, QLineEdit)
            # 应该挂到 item widget
            assert panel._session_tree.itemWidget(item, 0) is panel._rename_editor
        finally:
            _cleanup_panel(panel, qapp)

    def test_commit_rename_writes_new_title(self, qapp, tmp_path, monkeypatch):
        """Enter 提交 → update_session_title 写库。"""

        db = str(tmp_path / "test_rename_commit.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="旧标题"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            item = panel._find_session_item("sess-1")
            panel._begin_rename(item, "sess-1")
            panel._rename_editor.setText("新标题")
            panel._commit_rename()
            # 验证写库
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.title == "新标题"
            finally:
                s2.close()
            # 状态清空
            assert panel._renaming_sid is None
            assert panel._rename_editor is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_cancel_rename_does_not_write(self, qapp, tmp_path, monkeypatch):
        """Esc 取消 → 不写库，保留原标题。"""

        db = str(tmp_path / "test_rename_cancel.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="原标题"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            item = panel._find_session_item("sess-1")
            panel._begin_rename(item, "sess-1")
            panel._rename_editor.setText("不应被保存")
            panel._cancel_rename()
            # 验证未写库
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.title == "原标题", "Esc 取消不应写库"
            finally:
                s2.close()
            assert panel._renaming_sid is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_commit_empty_title_keeps_old(self, qapp, tmp_path, monkeypatch):
        """空标题提交 → 保留旧标题（不写空字符串）。"""

        db = str(tmp_path / "test_rename_empty.db")
        s = _open_store(db)
        _run(s.create_session(session_id="sess-1", title="原标题"))
        s.close()

        panel = _make_chat_panel(db, monkeypatch)
        try:
            panel._refresh_session_list()
            item = panel._find_session_item("sess-1")
            panel._begin_rename(item, "sess-1")
            panel._rename_editor.setText("   ")  # 仅空格
            panel._commit_rename()
            s2 = _open_store(db)
            try:
                sess = _run(s2.get_session("sess-1"))
                assert sess.title == "原标题", "空标题不应写入"
            finally:
                s2.close()
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 5: _StartPage 分组选择器
# ============================================================================


class TestStartPageGroupSelector:
    """D6：开始页新建会话时选分组归属。"""

    def test_group_combo_exists(self, qapp):
        """_StartPage 应有 _group_combo QComboBox。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            assert isinstance(page._group_combo, QComboBox)
            # 默认有"未分组"和"新建分组"两项
            assert page._group_combo.count() >= 2
            assert page._group_combo.itemData(0) == ""
            assert page._group_combo.itemData(1) == "__new__"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_refresh_groups_populates_existing(self, qapp):
        """refresh_groups 把已有分组名加入 combo。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            page.refresh_groups(["工作", "学习"])
            assert page._group_combo.findData("工作") >= 0
            assert page._group_combo.findData("学习") >= 0
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_refresh_groups_preserves_selection(self, qapp):
        """refresh_groups 后保持当前选择不变。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            page.refresh_groups(["工作", "学习"])
            # 选"学习"
            page._group_combo.setCurrentIndex(page._group_combo.findData("学习"))
            # 再 refresh（不应丢失选择）
            page.refresh_groups(["工作", "学习", "新分组"])
            assert page._group_combo.currentData() == "学习"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_current_group_selection_returns_selected(self, qapp):
        """_current_group_selection 返回当前选中分组。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            page.refresh_groups(["工作"])
            page._group_combo.setCurrentIndex(page._group_combo.findData("工作"))
            assert page._current_group_selection() == "工作"
            # 选未分组
            page._group_combo.setCurrentIndex(0)
            assert page._current_group_selection() == ""
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_send_requested_emits_group_name(self, qapp):
        """send_requested 信号携带 group_name（三参数）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            page.refresh_groups(["工作"])
            page._group_combo.setCurrentIndex(page._group_combo.findData("工作"))
            # 输入框写入文本
            edit = page._current_input_edit()
            edit.setPlainText("hello")
            received: list = []
            page.send_requested.connect(lambda *args: received.append(args))
            page._on_send()
            assert len(received) == 1
            # 三参数：text, skills, group_name
            assert received[0][0] == "hello"
            assert received[0][2] == "工作"
        finally:
            page.deleteLater()
            qapp.processEvents()
