"""Tasks 面板 — 待办总览（主面板分类）

统一入口，展示三个二级入口：
1. 收件箱（inbox）— Loop 推送的待审查条目
2. 到期任务（due_todos）— 到期的周期待办
3. WIP 任务（wip_tasks）— 进行中的工作

每个入口显示红点 + 未完成数量，点击切换到对应子面板。

数据源：
- GET /inbox?status=pending — 待处理收件箱数
- GET /todos/due — 到期任务数
- GET /wip?status=active&summary=true — 活跃 WIP 数
"""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import icon_pixmap, tokens
from lib.ui.theme import set_kind, set_text_role


class _SubEntryCard(QFrame):
    """二级入口卡片"""

    clicked = Signal(str)  # 发射 panel_id

    def __init__(self, icon: str, title: str, subtitle: str, target_panel_id: str, parent=None):
        super().__init__(parent)
        self._target_panel_id = target_panel_id
        self._count = 0

        self.setObjectName("subEntryCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        set_kind(self, "card")
        self.setStyleSheet(f"""
            QFrame#subEntryCard:hover {{
                background: {tokens.BG_HOVER};
                border: 1px solid {tokens.ACCENT};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # 图标（优先 SVG 图标名，未命中回退 emoji 文本——与主窗口侧边栏同规则）
        icon_label = QLabel()
        icon_label.setFixedWidth(36)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            icon_label.setPixmap(icon_pixmap(icon, tokens.ICON_DEFAULT, 24))
        except KeyError:
            icon_label.setText(icon)
        layout.addWidget(icon_label)

        # 标题 + 副标题
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = QLabel(title)
        set_text_role(title_label, "title")
        text_col.addWidget(title_label)

        self._subtitle_label = QLabel(subtitle)
        set_text_role(self._subtitle_label, "secondary")
        text_col.addWidget(self._subtitle_label)
        layout.addLayout(text_col, 1)

        # 红点 + 数量
        self._badge_label = QLabel("")
        self._badge_label.setStyleSheet(
            f"background: {tokens.DANGER}; color: {tokens.TEXT_PRIMARY};"
            " border-radius: 10px; padding: 2px 8px; min-width: 20px;"
        )
        self._badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge_label.setVisible(False)
        layout.addWidget(self._badge_label)

        # 箭头
        arrow = QLabel("›")
        set_text_role(arrow, "tertiary")
        layout.addWidget(arrow)

        self.mousePressEvent = lambda _e: self.clicked.emit(self._target_panel_id)

    def set_count(self, count: int) -> None:
        """设置未完成数量（>0 显示红点）"""
        self._count = count
        if count > 0:
            self._badge_label.setText(str(count))
            self._badge_label.setVisible(True)
        else:
            self._badge_label.setVisible(False)

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle_label.setText(subtitle)


class TasksPanel(PanelBase):
    """待办总览面板"""

    PANEL_META = PanelMeta(
        id="tasks",
        title="待办",
        icon="list",
        order=20,
        category="main",
        requires_backend=True,
    )

    switch_panel_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None

        # 缓存计数
        self._inbox_count = 0
        self._due_count = 0
        self._wip_count = 0

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
        layout.setSpacing(14)

        # 顶栏
        header_row = QHBoxLayout()
        title = QLabel("待办")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 三个二级入口
        self._inbox_card = _SubEntryCard(
            "inbox", "收件箱", "Loop 推送的待审查条目", "inbox"
        )
        self._inbox_card.clicked.connect(self._on_sub_entry_clicked)
        layout.addWidget(self._inbox_card)

        self._due_card = _SubEntryCard(
            "clock", "到期任务", "到期的周期待办", "due_todos"
        )
        self._due_card.clicked.connect(self._on_sub_entry_clicked)
        layout.addWidget(self._due_card)

        self._wip_card = _SubEntryCard(
            "wrench", "WIP 任务", "进行中的工作", "wip_tasks"
        )
        self._wip_card.clicked.connect(self._on_sub_entry_clicked)
        layout.addWidget(self._wip_card)

        layout.addStretch()

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._refresh_async()

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

    @Slot(list)
    def on_todos_changed(self, todos: list) -> None:
        """ServiceManager todos_due_changed 信号的 slot"""
        self._due_count = len(todos) if todos else 0
        self._due_card.set_count(self._due_count)
        self._update_aggregate()

    # —— 数据刷新 ——

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _on_sub_entry_clicked(self, panel_id: str) -> None:
        self.switch_panel_requested.emit(panel_id)

    def _refresh_async(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class RefreshThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.inbox_data = None
                self.due_data = None
                self.wip_data = None

            def run(self):
                self.inbox_data = self._http.get("/inbox", params={"status": "pending", "limit": 1000})
                self.due_data = self._http.get("/todos/due")
                self.wip_data = self._http.get("/wip", params={"status": "active", "summary": "true"})

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        if t is None:
            return
        if t.inbox_data is None and t.due_data is None and t.wip_data is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)

        # 收件箱
        if t.inbox_data and isinstance(t.inbox_data, dict):
            items = t.inbox_data.get("items", []) or []
            self._inbox_count = len(items)
        else:
            self._inbox_count = 0
        self._inbox_card.set_count(self._inbox_count)
        self._inbox_card.set_subtitle(f"待处理 {self._inbox_count} 条")

        # 到期任务
        if t.due_data and isinstance(t.due_data, dict):
            due = t.due_data.get("due", []) or []
            self._due_count = len(due)
        else:
            self._due_count = 0
        self._due_card.set_count(self._due_count)
        self._due_card.set_subtitle(f"到期 {self._due_count} 个")

        # WIP 任务
        if t.wip_data and isinstance(t.wip_data, dict):
            tasks = t.wip_data.get("tasks", []) or []
            self._wip_count = len(tasks)
        else:
            self._wip_count = 0
        self._wip_card.set_count(self._wip_count)
        self._wip_card.set_subtitle(f"活跃 {self._wip_count} 个")

        self._update_aggregate()

    def _update_aggregate(self) -> None:
        """聚合红点数（供 sidebar 显示）

        sidebar badge 由 app.py 直接查询此 panel 的 pending_count 属性，
        这里仅作为聚合计算的扩展点保留。
        """
        # 通过信号通知主窗口更新 sidebar badge
        # 主窗口连接此信号后更新 sidebar
        pass  # sidebar badge 由 app.py 直接查询此 panel 的计数

    @property
    def pending_count(self) -> int:
        """未完成总数（inbox + due，不含 wip）"""
        return self._inbox_count + self._due_count
