"""Ticket 04：侧边栏元界面 + 开始页

覆盖 spec D1（开始页 hub）/ D4（侧边栏元界面区）/ D8（最近5 按 updated_at 倒序）/
D11（Ctrl+Enter 发送 + Enter 换行）/ D12（触发时机）/ D20（最近对话项单击切换）/
D21（侧边栏纯列表）/ D22（元界面样式：固定顶部 + 分隔线 + 图标）/ D23（元界面不可折叠）。

测试策略：
- 源码静态扫描：grep 关键 class / method / icon 定义
- 运行时实例化：_StartPage 信号 + 模板 tab + 输入框 + 最近 5 列表
- 行为测试：切模板 tab 不清空已输入文本 / refresh_recent_sessions 取前 5 /
  Ctrl+Enter 触发 send_requested / _on_recent_clicked emit session_selected
- 触发时机：点新对话 / 首次启动 / 删除当前会话（_switch_session 在选中会话时切回消息视图）

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式（参考 test_ui_theme.py）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================================
# Part 2: 运行时实例化 — _StartPage 组件 + ChatPanel 视图切换
# ============================================================================


class TestStartPageComponents:
    """_StartPage 实例化验证（spec D1 组件结构）。"""

    def test_start_page_object_name(self, qapp):
        """_StartPage objectName 应为 startPage（便于 findChild 定位）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            assert page.objectName() == "startPage"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_start_page_has_template_tabs(self, qapp):
        """_StartPage 应有 QTabWidget 模板 tab（D1 顶部模板 tab）。"""
        from PySide6.QtWidgets import QTabWidget

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            tabs = page.findChild(QTabWidget, "templateTabs")
            assert tabs is not None, "_StartPage 应有 objectName=templateTabs 的 QTabWidget"
            # 默认应有至少 1 个 tab（兜底空白 tab，spec D1）
            assert tabs.count() >= 1, "默认应至少有 1 个模板 tab（兜底空白）"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_start_page_has_new_template_button(self, qapp):
        """开始页标题行应有 "前往新建模板" 按钮，点击发出 manage_templates_requested。

        2026-09-13：按钮从 tab 栏 corner widget 挪到标题行（corner widget 与真实
        模板 tab 语义撞车且几何被 QTabWidget corner 区域裁剪）。
        """
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            btns = [b for b in page.findChildren(QPushButton) if b.text() == "前往新建模板"]
            assert len(btns) == 1, "应有且仅有一个 '前往新建模板' 按钮"
            got = []
            page.manage_templates_requested.connect(lambda: got.append(1))
            btns[0].click()
            assert got == [1], "点击按钮应发出 manage_templates_requested 信号"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_start_page_single_input_design(self, qapp):
        """开始页单输入框设计：无独立 _input_edit，模板 tab 内输入框含 Ctrl+Enter 提示。"""
        from PySide6.QtWidgets import QTextEdit

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            assert not hasattr(page, "_input_edit"), (
                "单输入框设计下 _StartPage 不应再渲染独立 _input_edit"
            )
            edit = page._current_input_edit()
            assert isinstance(edit, QTextEdit), "当前模板 tab 内应有输入框"
            # Ctrl+Enter 发送提示在发送按钮 tooltip 上（D11）
            assert "Ctrl+Enter" in page._send_btn.toolTip(), (
                "发送按钮 tooltip 应含 Ctrl+Enter 提示（D11）"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_start_page_has_send_button(self, qapp):
        """_StartPage 应有发送按钮（D1 发送按钮）。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            assert isinstance(page._send_btn, QPushButton)
            assert page._send_btn.text() == "发送"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_start_page_has_recent_list(self, qapp):
        """_StartPage 应有 QListWidget 最近 5 对话列表（D1 下方最近 5）。"""
        from PySide6.QtWidgets import QListWidget

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            assert isinstance(page._recent_list, QListWidget), (
                "_StartPage._recent_list 应是 QListWidget"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()


class TestStartPageRefreshRecentSessions:
    """refresh_recent_sessions 行为测试（D8 全部按 updated_at 倒序取前 5）。"""

    def test_refresh_recent_sessions_takes_top_5(self, qapp):
        """refresh_recent_sessions 应只取前 5 条（D8）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            # 构造 8 条会话，updated_at 递增
            sessions = [
                {"id": f"sess-{i}", "title": f"会话{i}", "updated_at": float(i)}
                for i in range(8)
            ]
            page.refresh_recent_sessions(sessions)
            assert page._recent_list.count() == 5, (
                f"应取前 5 条，实际 {page._recent_list.count()}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_refresh_recent_sessions_shows_title_and_time(self, qapp):
        """refresh_recent_sessions 应展示 title + 时间（D1 标题 + 时间）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            sessions = [
                {"id": "sess-1", "title": "测试会话", "updated_at": 1700000000.0},
            ]
            page.refresh_recent_sessions(sessions)
            assert page._recent_list.count() == 1
            item = page._recent_list.item(0)
            text = item.text()
            assert "测试会话" in text, f"列表项应含 title '测试会话'，实际 {text!r}"
            # 时间字段存在（mm-dd HH:MM 格式）
            assert "·" in text or "-" in text, f"列表项应含时间字段，实际 {text!r}"
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_refresh_recent_sessions_uses_title_when_no_title(self, qapp):
        """无 title 时用 sid 前 8 字符做 display（spec D8 兜底）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            sessions = [{"id": "abcdefgh-session-1", "title": "", "updated_at": 1700000000.0}]
            page.refresh_recent_sessions(sessions)
            assert page._recent_list.count() == 1
            item = page._recent_list.item(0)
            assert "abcdefgh" in item.text(), (
                f"无 title 时应用 sid 前 8 字符，实际 {item.text()!r}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: ChatPanel 视图切换行为
# ============================================================================


class TestChatPanelViewSwitching:
    """ChatPanel 主区视图切换（D12 触发时机 + D21 切到消息视图）。"""

    def test_initial_view_is_start_page(self, qapp):
        """ChatPanel 初始化后默认显示开始页（D12 首次启动）。"""
        from PySide6.QtWidgets import QStackedWidget

        from client.panels.chat import ChatPanel, _StartPage

        panel = ChatPanel()
        try:
            stack = panel.findChild(QStackedWidget)
            assert stack is not None, "ChatPanel 应有 QStackedWidget"
            current = stack.currentWidget()
            assert isinstance(current, _StartPage), (
                "首次启动应显示 _StartPage（D12），实际 "
                f"{type(current).__name__}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_on_new_session_switches_to_start_page(self, qapp):
        """点新对话 → 切回开始页（D12 触发时机之一）。"""
        from PySide6.QtWidgets import QStackedWidget

        from client.panels.chat import ChatPanel, _StartPage

        panel = ChatPanel()
        try:
            stack = panel.findChild(QStackedWidget)
            # 模拟切到消息视图后点新对话
            stack.setCurrentWidget(panel._messages_view)
            assert not isinstance(stack.currentWidget(), _StartPage)

            panel._on_new_session()
            qapp.processEvents()
            assert isinstance(stack.currentWidget(), _StartPage), (
                "点新对话后应切到 _StartPage（D12）"
            )
            # _current_session_id 应被清空（开始页发送时才创建）
            assert panel._current_session_id is None
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_on_manage_templates_switches_to_template_panel(self, qapp):
        """点模板管理 → 切到模板管理面板（D5，T05 占位）。"""
        from PySide6.QtWidgets import QStackedWidget

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            stack = panel.findChild(QStackedWidget)
            panel._on_manage_templates()
            qapp.processEvents()
            current = stack.currentWidget()
            # T05 实现前是占位 QLabel；T05 实现后是 _TemplateManagerPanel
            # 此处只验证"切到了非 _start_page 且非 _messages_view 的第三视图"
            assert current is not panel._start_page, (
                "点模板管理后应切到模板管理面板（非开始页）"
            )
            assert current is not panel._messages_view, (
                "点模板管理后应切到模板管理面板（非消息视图）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_switch_session_switches_to_messages_view(self, qapp):
        """_switch_session → 切到消息时间线视图（D21）。"""
        from PySide6.QtWidgets import QStackedWidget

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            stack = panel.findChild(QStackedWidget)
            # 模拟切换到一个会话
            panel._switch_session("test-session-id")
            qapp.processEvents()
            assert stack.currentWidget() is panel._messages_view, (
                "_switch_session 应切到 _messages_view（D21）"
            )
            assert panel._current_session_id == "test-session-id"
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 4: Ctrl+Enter 发送 + Enter 换行
# ============================================================================


class TestStartPageCtrlEnterSend:
    """_StartPage 输入框 Ctrl+Enter 发送 + Enter 换行（D11）。"""

    def test_ctrl_enter_emits_send_requested(self, qapp):
        """Ctrl+Enter 触发 send_requested 信号（D11）。"""
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            # 当前 tab 输入框写入文本
            edit = page._current_input_edit()
            edit.setPlainText("hello")

            received: list[tuple[str, list, str]] = []
            # T06 后 send_requested 携带 (text, skills, group_name) 三参数
            page.send_requested.connect(
                lambda text, skills, group: received.append((text, skills, group))
            )

            # 模拟 Ctrl+Enter 按键
            ctrl_enter = QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier,
                "\r",
            )
            page._input_key_press(ctrl_enter)

            assert len(received) == 1, (
                f"Ctrl+Enter 应触发 send_requested 一次，实际 {len(received)} 次"
            )
            assert received[0][0] == "hello", (
                f"send_requested 应携带文本 'hello'，实际 {received[0][0]!r}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_enter_inserts_newline_no_signal(self, qapp):
        """Enter 不触发 send_requested（D11 Enter 换行）。"""
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            edit = page._current_input_edit()
            edit.setPlainText("hello")

            received: list = []
            page.send_requested.connect(lambda *args: received.append(args))

            # 仅 Enter（无 Ctrl）
            enter_only = QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
                "\r",
            )
            page._input_key_press(enter_only)

            assert received == [], (
                "Enter 不应触发 send_requested（D11 Enter 是换行）"
            )
            # 输入框应多一行（QTextEdit 默认 Enter 插入换行）
            text = edit.toPlainText()
            assert "hello" in text, "原文本应保留"
        finally:
            page.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 5: 模板 tab 切换不清空已输入文本（D1 关键澄清）
# ============================================================================


class TestStartPageTemplateTabTextCache:
    """切模板 tab 不清空已输入文本（每 tab 独立 QTextEdit）。"""

    def test_different_tabs_have_independent_text(self, qapp):
        """切模板 tab 不清空已输入文本（D1 关键澄清）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            # 假设至少有 1 个 tab（兜底空白）
            assert page._template_tabs.count() >= 1

            # 手动加第二个 tab（保证可测多 tab 切换）
            page._add_template_tab("test_tpl_2", "测试2", "预设2", ["recurring.test"])

            assert page._template_tabs.count() >= 2

            # tab 0 写入文本
            page._template_tabs.setCurrentIndex(0)
            edit0 = page._template_tabs.widget(0)
            edit0.setPlainText("text in tab 0")

            # 切到 tab 1
            page._template_tabs.setCurrentIndex(1)
            edit1 = page._template_tabs.widget(1)
            edit1.setPlainText("text in tab 1")

            # 切回 tab 0，文本应保留
            page._template_tabs.setCurrentIndex(0)
            qapp.processEvents()
            assert edit0.toPlainText() == "text in tab 0", (
                "切回 tab 0 应保留已输入文本（D1 tab 切换不清空）"
            )

            # 切到 tab 1，文本应保留
            page._template_tabs.setCurrentIndex(1)
            qapp.processEvents()
            assert edit1.toPlainText() == "text in tab 1", (
                "切回 tab 1 应保留已输入文本（D1 tab 切换不清空）"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_template_id_stored_in_widget_property(self, qapp):
        """每个 tab 的 QTextEdit 应在 property 中存 template_id（用于发送时取 skills）。"""
        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            # 加一个 tab
            page._add_template_tab("tpl-abc", "测试", "预设", ["recurring.test"])
            # 找到对应 tab widget
            last_idx = page._template_tabs.count() - 1
            tab_widget = page._template_tabs.widget(last_idx)
            assert tab_widget is not None
            tpl_id = tab_widget.property("template_id")
            assert tpl_id == "tpl-abc", (
                f"tab widget property template_id 应为 'tpl-abc'，实际 {tpl_id!r}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 6: 最近 5 单击切换（D20）
# ============================================================================


class TestStartPageRecentClick:
    """_StartPage 最近 5 列表单击 → emit session_selected（D20）。"""

    def test_recent_click_emits_session_id(self, qapp):
        """单击最近列表项 → emit session_selected（D20）。"""

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            # 构造 3 条会话
            sessions = [
                {"id": "sess-a", "title": "A", "updated_at": 1.0},
                {"id": "sess-b", "title": "B", "updated_at": 2.0},
                {"id": "sess-c", "title": "C", "updated_at": 3.0},
            ]
            page.refresh_recent_sessions(sessions)
            assert page._recent_list.count() == 3

            received: list[str] = []
            page.session_selected.connect(lambda sid: received.append(sid))

            # 模拟点击第二项
            item = page._recent_list.item(1)
            page._on_recent_clicked(item)

            assert len(received) == 1, f"单击应 emit session_selected 一次，实际 {len(received)} 次"
            assert received[0] == "sess-b", (
                f"emit 的 session_id 应为 'sess-b'，实际 {received[0]!r}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()

    def test_recent_click_first_item_emits_correct_id(self, qapp):
        """单击第一项应 emit 对应 session_id（D20）。"""

        from client.panels.chat import _StartPage

        page = _StartPage()
        try:
            sessions = [
                {"id": "first-sess", "title": "首个", "updated_at": 1.0},
                {"id": "second-sess", "title": "次个", "updated_at": 2.0},
            ]
            page.refresh_recent_sessions(sessions)

            received: list[str] = []
            page.session_selected.connect(lambda sid: received.append(sid))

            item = page._recent_list.item(0)
            page._on_recent_clicked(item)

            assert received == ["first-sess"], (
                f"第一项单击应 emit 'first-sess'，实际 {received!r}"
            )
        finally:
            page.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 7: 侧边栏元界面区（D22 固定顶部 + 分隔线 + 图标，D23 不可折叠）
# ============================================================================


class TestSidebarMetaInterface:
    """侧边栏元界面区（D22 + D23）。"""

    def test_sidebar_has_new_session_button_with_icon(self, qapp):
        """侧边栏顶部应有 ✚ 新对话按钮（D22 图标）。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            buttons = panel.findChildren(QPushButton)
            new_btn_texts = [b.text() for b in buttons if "新对话" in b.text()]
            assert any("✚" in t for t in new_btn_texts), (
                f"侧边栏应有 ✚ 新对话按钮，实际按钮文本集合：{new_btn_texts}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_sidebar_has_template_management_button_with_icon(self, qapp):
        """侧边栏顶部应有 ▦ 模板管理按钮（D22 图标）。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            buttons = panel.findChildren(QPushButton)
            tpl_btn_texts = [b.text() for b in buttons if "模板管理" in b.text()]
            assert any("▦" in t for t in tpl_btn_texts), (
                f"侧边栏应有 ▦ 模板管理按钮，实际按钮文本集合：{tpl_btn_texts}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_new_session_button_connected_to_on_new_session(self, qapp):
        """点 ✚ 新对话 按钮 → 触发 _on_new_session（切到开始页，D12）。"""
        from PySide6.QtWidgets import QPushButton, QStackedWidget

        from client.panels.chat import ChatPanel, _StartPage

        panel = ChatPanel()
        try:
            # 找到 ✚ 新对话 按钮
            buttons = panel.findChildren(QPushButton)
            new_btn = next(
                (b for b in buttons if "新对话" in b.text() and "✚" in b.text()),
                None,
            )
            assert new_btn is not None, "应找到 ✚ 新对话 按钮"

            # 切到消息视图模拟正在看会话
            stack = panel.findChild(QStackedWidget)
            stack.setCurrentWidget(panel._messages_view)

            # 点击按钮
            new_btn.click()
            qapp.processEvents()

            assert isinstance(stack.currentWidget(), _StartPage), (
                "点 ✚ 新对话 后应切到 _StartPage（D12）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_template_management_button_connected_to_on_manage_templates(self, qapp):
        """点 ▦ 模板管理 按钮 → 触发 _on_manage_templates（切到模板管理面板）。"""
        from PySide6.QtWidgets import QPushButton, QStackedWidget

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            buttons = panel.findChildren(QPushButton)
            tpl_btn = next(
                (b for b in buttons if "模板管理" in b.text() and "▦" in b.text()),
                None,
            )
            assert tpl_btn is not None, "应找到 ▦ 模板管理 按钮"

            stack = panel.findChild(QStackedWidget)
            tpl_btn.click()
            qapp.processEvents()

            # 切到第三视图（非 _start_page，非 _messages_view）
            current = stack.currentWidget()
            assert current is not panel._start_page, "点模板管理后应离开开始页"
            assert current is not panel._messages_view, "点模板管理后应离开消息视图"
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 8: 元界面区不可折叠（D23）
# ============================================================================


class TestMetaInterfaceNotCollapsible:
    """元界面区不可单独折叠（D23）。

    D23 说"元界面区始终展开"——侧边栏整体可折叠（D4 弹性布局），
    但元界面区在侧边栏内不单独折叠（始终可见，无 collapse 按钮）。
    """

    def test_meta_interface_no_collapse_toggle(self, qapp):
        """元界面区不应有 collapse/折叠 toggle 按钮（D23 始终展开）。"""
        from PySide6.QtWidgets import QPushButton

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 没有针对元界面区的折叠按钮（无 collapse/toggle 文字按钮）
            buttons = panel.findChildren(QPushButton)
            collapse_btns = [
                b for b in buttons
                if any(kw in b.text().lower() for kw in ("collapse", "折叠", "toggle 元", "收起元"))
            ]
            assert collapse_btns == [], (
                "元界面区不应有折叠按钮（D23 始终展开）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()
