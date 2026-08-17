"""DueTodos 面板 — 到期任务（主面板分类，待办二级入口）

展示到期的周期待办，纵向列表 + 点击展开详情。

API:
- GET /todos/due — 获取到期待办
- POST /todos/{id}/done — 标记完成
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


class _DueTodoCard(QFrame):
    """单个到期待办卡片"""

    def __init__(self, todo: dict, parent_panel, parent=None):
        super().__init__(parent)
        self._todo = todo
        self._parent_panel = parent_panel
        self._expanded = False

        self.setObjectName("dueTodoCard")
        set_kind(self, "card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题行
        self._header = QFrame()
        self._header.setObjectName("dueTodoHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet("QFrame#dueTodoHeader { border: none; padding: 4px; }")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        # 到期指示
        self._status_icon = QLabel("到期")
        header_layout.addWidget(self._status_icon)

        # 标题
        title = todo.get("title", "(无标题)")
        self._title_label = QLabel(title)
        set_text_role(self._title_label, "title")
        header_layout.addWidget(self._title_label, 1)

        # 类型
        todo_type = todo.get("type", "")
        if todo_type:
            type_label = QLabel(f"[{todo_type}]")
            set_text_role(type_label, "secondary")
            header_layout.addWidget(type_label)

        # skill
        skill = todo.get("skill", "")
        if skill:
            skill_label = QLabel(f"({skill})")
            set_text_role(skill_label, "tertiary")
            header_layout.addWidget(skill_label)

        # 到期时间
        next_due = todo.get("next_due_at", "")
        if next_due:
            due_label = QLabel(f"到期: {next_due}")
            set_text_role(due_label, "warning")
            header_layout.addWidget(due_label)

        # 完成按钮（在标题行，收起状态可操作）
        done_btn = QPushButton("完成")
        done_btn.setFixedSize(72, 24)
        set_kind(done_btn, "primary")
        done_btn.clicked.connect(lambda _e: parent_panel.mark_done(todo.get("id", "")))
        header_layout.addWidget(done_btn)

        # 箭头
        self._arrow = QLabel("展开")
        set_text_role(self._arrow, "secondary")
        header_layout.addWidget(self._arrow)

        self._header.mousePressEvent = lambda e: self.toggle()
        layout.addWidget(self._header)

        # 备注行（直接显示，不需展开）
        notes = todo.get("notes", "")
        if notes:
            notes_row = QLabel(notes)
            set_text_role(notes_row, "secondary")
            notes_row.setStyleSheet(
                f"padding: 4px 14px; border-left: 3px solid {tokens.SUCCESS};"
            )
            notes_row.setWordWrap(True)
            notes_row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(notes_row)

        # 详情区
        self._body = QFrame()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(14, 10, 14, 10)
        body_layout.setSpacing(6)

        # 字段
        for key, label in [
            ("id", "ID"),
            ("type", "类型"),
            ("frequency", "频率"),
            ("skill", "Skill"),
            ("status", "状态"),
            ("next_due_at", "下次到期"),
            ("last_done_at", "上次完成"),
            ("condition", "条件"),
            ("condition_status", "条件状态"),
            ("start_date", "开始日期"),
            ("end_date", "结束日期"),
            ("trigger_condition", "触发条件"),
        ]:
            val = todo.get(key)
            if val is None or val == "":
                continue
            row = QHBoxLayout()
            lbl = QLabel(label)
            set_text_role(lbl, "secondary")
            lbl.setFixedWidth(80)
            v = QLabel(str(val))
            set_text_role(v, "secondary")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(lbl)
            row.addWidget(v, 1)
            body_layout.addLayout(row)

        # notes
        notes = todo.get("notes", "")
        if notes:
            notes_label = QLabel("备注")
            set_text_role(notes_label, "secondary")
            body_layout.addWidget(notes_label)
            notes_text = QLabel(notes)
            set_text_role(notes_text, "secondary")
            notes_text.setWordWrap(True)
            notes_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body_layout.addWidget(notes_text)

        # related_memory_keys
        related_keys = todo.get("related_memory_keys", [])
        if related_keys:
            rk_label = QLabel(f"关联记忆: {', '.join(related_keys)}")
            set_text_role(rk_label, "secondary")
            body_layout.addWidget(rk_label)

        self._body.setVisible(False)
        layout.addWidget(self._body)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow.setText("收起" if self._expanded else "展开")

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self.toggle()


class DueTodosPanel(PanelBase):
    """到期任务面板"""

    PANEL_META = PanelMeta(
        id="due_todos",
        title="到期任务",
        icon="clock",
        order=22,
        category="main",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._todos: list[dict] = []
        self._cards: list[_DueTodoCard] = []
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None
        # UI 状态记忆：刷新后恢复展开状态，避免被定时重建清空
        self._expanded_ids: set[str] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(30000)  # 30s
        self._timer.timeout.connect(self._refresh_async)

        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._normal_widget = QWidget()
        self._stack.addWidget(self._normal_widget)
        self._offline_widget = self._build_offline_widget()
        self._stack.addWidget(self._offline_widget)

        self._build_normal_ui(self._normal_widget)

    def _build_offline_widget(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("后端离线\n\n请先启动后端服务（start.bat）")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(label, "secondary")
        layout.addWidget(label)
        return w

    def _build_normal_ui(self, root: QWidget) -> None:
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶栏
        header_row = QHBoxLayout()
        title = QLabel("到期任务")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        expand_all_btn = QPushButton("全部展开")
        expand_all_btn.setFixedWidth(80)
        expand_all_btn.clicked.connect(self._expand_all)
        header_row.addWidget(expand_all_btn)

        collapse_all_btn = QPushButton("全部收起")
        collapse_all_btn.setFixedWidth(80)
        collapse_all_btn.clicked.connect(self._collapse_all)
        header_row.addWidget(collapse_all_btn)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 统计
        self._stats_label = QLabel("加载中...")
        set_text_role(self._stats_label, "secondary")
        layout.addWidget(self._stats_label)

        # 滚动列表
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()

        self._scroll.setWidget(self._list_container)
        layout.addWidget(self._scroll, 1)

        # 空状态
        self._empty_label = QLabel("没有到期任务")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(self._empty_label, "secondary")
        self._empty_label.setVisible(False)
        layout.addWidget(self._empty_label)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._timer.start()
        self._refresh_async()

    def on_hide(self) -> None:
        self._timer.stop()

    def on_refresh(self) -> None:
        self._refresh_async()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            self._refresh_async()
        else:
            self._stack.setCurrentWidget(self._offline_widget)
            self._timer.stop()
            self._todos = []
            self._clear_list()

    # —— 数据刷新 ——

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _clear_list(self) -> None:
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards = []

    def _refresh_async(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class RefreshThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.data = None

            def run(self):
                self.data = self._http.get("/todos/due")

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        if t is None or t.data is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)

        data = t.data
        if not isinstance(data, dict):
            return
        self._todos = data.get("due", []) or []
        self._stats_label.setText(f"共 {len(self._todos)} 个到期任务")

        # 重建前：按 todo id 收集展开状态，并保存滚动位置
        self._expanded_ids = {
            c._todo.get("id", "") for c in self._cards if c._expanded
        }
        scroll_pos = self._scroll.verticalScrollBar().value()

        self._clear_list()
        self._empty_label.setVisible(len(self._todos) == 0)

        for todo in self._todos:
            card = _DueTodoCard(todo, self)
            todo_id = todo.get("id", "")
            if todo_id in self._expanded_ids:
                card.set_expanded(True)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)
            self._cards.append(card)

        # 恢复滚动位置
        self._scroll.verticalScrollBar().setValue(scroll_pos)

    # —— 展开/收起 ——

    def _expand_all(self) -> None:
        for card in self._cards:
            card.set_expanded(True)

    def _collapse_all(self) -> None:
        for card in self._cards:
            card.set_expanded(False)

    # —— 操作 ——

    def mark_done(self, todo_id: str) -> None:
        if not todo_id:
            return
        ret = QMessageBox.question(
            self, "确认", "确认标记此任务为已完成？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("post", f"/todos/{todo_id}/done")
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()
