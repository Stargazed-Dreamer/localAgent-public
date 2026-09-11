"""WipTasks 面板 — WIP 任务（主面板分类，待办二级入口）

展示进行中的工作（Work In Progress），纵向列表 + 点击展开详情。

API:
- GET /wip?summary=true — 列出所有 WIP（轻量摘要）
- GET /wip/{id} — 获取详情
- PUT /wip/{id} — 更新（status/progress 等）
"""

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import EmptyState, tokens
from lib.ui.theme import set_kind, set_status, set_text_role

_WIP_STATUS_INDICATOR = {
    "active": "online",
    "paused": "warning",
    "blocked": "offline",
    "completed": "unknown",
}

_WIP_STATUS_TEXT = {
    "active": "进行中",
    "paused": "已暂停",
    "blocked": "已阻塞",
    "completed": "已完成",
}

_PRIORITY_COLORS = {
    "high": tokens.DANGER_TEXT,
    "medium": tokens.WARNING_TEXT,
    "low": tokens.TEXT_TERTIARY,
}


class _WipTaskCard(QFrame):
    """单个 WIP 任务卡片"""

    def __init__(self, task: dict, parent_panel, parent=None):
        super().__init__(parent)
        self._task = task
        self._parent_panel = parent_panel
        self._expanded = False
        self._detail_loaded = False

        self.setObjectName("wipCard")
        set_kind(self, "card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题行
        self._header = QFrame()
        self._header.setObjectName("wipHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet("QFrame#wipHeader { border: none; padding: 4px; }")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        # 状态指示
        status = task.get("status", "active")
        indicator = _WIP_STATUS_INDICATOR.get(status, "unknown")
        self._status_icon = QLabel("●")
        set_status(self._status_icon, indicator)
        header_layout.addWidget(self._status_icon)

        # 标题
        title = task.get("title", "(无标题)")
        self._title_label = QLabel(title)
        set_text_role(self._title_label, "title")
        header_layout.addWidget(self._title_label, 1)

        # 优先级
        priority = task.get("priority", "medium")
        prio_color = _PRIORITY_COLORS.get(priority, tokens.TEXT_TERTIARY)
        prio_label = QLabel(priority)
        prio_label.setStyleSheet(
            f"color: {prio_color}; border: 1px solid {prio_color}; padding: 1px 6px;"
        )
        header_layout.addWidget(prio_label)

        # 状态文本
        self._status_text = QLabel(_WIP_STATUS_TEXT.get(status, status))
        set_status(self._status_text, indicator)
        header_layout.addWidget(self._status_text)

        # 进度条（如果有）
        progress = task.get("progress")
        if progress is not None:
            progress_bar = QProgressBar()
            progress_bar.setValue(int(progress))
            progress_bar.setFixedWidth(80)
            progress_bar.setFixedHeight(16)
            header_layout.addWidget(progress_bar)

        # 箭头
        self._arrow = QLabel("展开")
        set_text_role(self._arrow, "secondary")
        header_layout.addWidget(self._arrow)

        self._header.mousePressEvent = lambda _e: self.toggle()
        layout.addWidget(self._header)

        # 详情区（懒加载）
        self._body = QFrame()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(14, 10, 14, 10)
        self._body_layout.setSpacing(6)

        # 占位提示
        self._loading_label = QLabel("加载详情中...")
        set_text_role(self._loading_label, "secondary")
        self._body_layout.addWidget(self._loading_label)

        self._body.setVisible(False)
        layout.addWidget(self._body)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow.setText("收起" if self._expanded else "展开")
        if self._expanded and not self._detail_loaded:
            self._load_detail()

    def set_expanded(self, expanded: bool) -> None:
        """幂等地设置展开状态。与 toggle 不同，重复调用同一值不会改变状态。"""
        if self._expanded == expanded:
            return
        self.toggle()

    def _load_detail(self) -> None:
        """异步加载详情"""
        from PySide6.QtCore import QThread

        task_id = self._task.get("id") or self._task.get("task_id", "")
        if not task_id:
            return

        class DetailThread(QThread):
            def __init__(self, http, tid):
                super().__init__()
                self._http = http
                self._tid = tid
                self.data = None

            def run(self):
                self.data = self._http.get(f"/wip/{self._tid}")

        self._detail_thread = DetailThread(self._parent_panel._http, task_id)
        self._detail_thread.finished.connect(self._on_detail_loaded)
        self._detail_thread.start()

    def _on_detail_loaded(self) -> None:
        t = getattr(self, "_detail_thread", None)
        if t is None or t.data is None:
            self._loading_label.setText("加载详情失败")
            return

        # 清除占位
        self._loading_label.setVisible(False)
        task = t.data
        self._detail_loaded = True

        # 字段
        for key, label in [
            ("id", "ID"),
            ("status", "状态"),
            ("priority", "优先级"),
            ("progress", "进度"),
            ("goal", "目标"),
            ("blocked_reason", "阻塞原因"),
            ("created_at", "创建时间"),
            ("updated_at", "更新时间"),
        ]:
            val = task.get(key)
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
            self._body_layout.addLayout(row)

        # next_steps
        next_steps = task.get("next_steps", [])
        if next_steps:
            ns_label = QLabel("下一步")
            set_text_role(ns_label, "secondary")
            self._body_layout.addWidget(ns_label)
            for i, step in enumerate(next_steps, 1):
                step_label = QLabel(f"{i}. {step}")
                set_text_role(step_label, "secondary")
                step_label.setWordWrap(True)
                step_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                self._body_layout.addWidget(step_label)

        # related
        for key, label in [
            ("related_skills", "关联 Skill"),
            ("related_files", "关联文件"),
            ("related_memory_keys", "关联记忆"),
            ("tags", "标签"),
        ]:
            vals = task.get(key, [])
            if not vals:
                continue
            row = QHBoxLayout()
            lbl = QLabel(label)
            set_text_role(lbl, "secondary")
            lbl.setFixedWidth(80)
            v = QLabel(", ".join(str(x) for x in vals))
            set_text_role(v, "secondary")
            v.setWordWrap(True)
            row.addWidget(lbl)
            row.addWidget(v, 1)
            self._body_layout.addLayout(row)

        # current_state JSON
        current_state = task.get("current_state", {})
        if current_state:
            cs_label = QLabel("当前状态")
            set_text_role(cs_label, "secondary")
            self._body_layout.addWidget(cs_label)
            cs_view = QTextEdit()
            cs_view.setReadOnly(True)
            cs_view.setMaximumHeight(100)
            try:
                cs_text = json.dumps(current_state, ensure_ascii=False, indent=2)
            except Exception:
                cs_text = str(current_state)
            cs_view.setPlainText(cs_text)
            cs_view.setStyleSheet(f"font-family: {tokens.FONT_MONO};")
            self._body_layout.addWidget(cs_view)

        # extra_data JSON
        extra = task.get("extra_data", {})
        if extra:
            ed_label = QLabel("额外数据")
            set_text_role(ed_label, "secondary")
            self._body_layout.addWidget(ed_label)
            ed_view = QTextEdit()
            ed_view.setReadOnly(True)
            ed_view.setMaximumHeight(100)
            try:
                ed_text = json.dumps(extra, ensure_ascii=False, indent=2)
            except Exception:
                ed_text = str(extra)
            ed_view.setPlainText(ed_text)
            ed_view.setStyleSheet(f"font-family: {tokens.FONT_MONO};")
            self._body_layout.addWidget(ed_view)

        # 操作按钮
        task_id = task.get("id") or task.get("task_id", "")
        status = task.get("status", "active")
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        if status == "active":
            pause_btn = QPushButton("暂停")
            pause_btn.clicked.connect(
                lambda: self._parent_panel.update_status(task_id, "paused")
            )
            btn_row.addWidget(pause_btn)
        elif status == "paused":
            resume_btn = QPushButton("恢复")
            set_kind(resume_btn, "primary")
            resume_btn.clicked.connect(
                lambda: self._parent_panel.update_status(task_id, "active")
            )
            btn_row.addWidget(resume_btn)

        if status != "completed":
            complete_btn = QPushButton("标记完成")
            set_kind(complete_btn, "primary")
            complete_btn.clicked.connect(
                lambda: self._parent_panel.update_status(task_id, "completed")
            )
            btn_row.addWidget(complete_btn)

        btn_row.addStretch()
        self._body_layout.addLayout(btn_row)


class WipTasksPanel(PanelBase):
    """WIP 任务面板"""

    PANEL_META = PanelMeta(
        id="wip_tasks",
        title="WIP 任务",
        icon="wrench",
        order=23,
        category="main",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._tasks: list[dict] = []
        self._status_filter: str | None = None
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None
        # UI 状态记忆：刷新后恢复展开状态，避免被定时重建清空
        self._expanded_ids: set[str] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(60000)  # 60s
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
        title = QLabel("WIP 任务")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        # 过滤按钮
        self._filter_all_btn = QPushButton("全部")
        self._filter_all_btn.clicked.connect(lambda: self._set_filter(None))
        header_row.addWidget(self._filter_all_btn)

        self._filter_active_btn = QPushButton("进行中")
        self._filter_active_btn.clicked.connect(lambda: self._set_filter("active"))
        header_row.addWidget(self._filter_active_btn)

        self._filter_paused_btn = QPushButton("已暂停")
        self._filter_paused_btn.clicked.connect(lambda: self._set_filter("paused"))
        header_row.addWidget(self._filter_paused_btn)

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

        # 空状态（与列表区域互斥显示，components.md §12）
        self._empty_state = EmptyState(
            "wrench",
            "没有 WIP 任务",
            hint="agent 开始长任务并留档后，进度会显示在这里",
        )
        self._empty_state.setVisible(False)
        layout.addWidget(self._empty_state, 1)

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
            self._tasks = []
            self._clear_list()

    # —— 数据刷新 ——

    def _set_filter(self, status: str | None) -> None:
        self._status_filter = status
        # 更新按钮样式
        for btn, key in [
            (self._filter_all_btn, None),
            (self._filter_active_btn, "active"),
            (self._filter_paused_btn, "paused"),
        ]:
            if key == status:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {tokens.ACCENT};"
                    f" color: {tokens.TEXT_ON_ACCENT};"
                    f" border: 1px solid {tokens.ACCENT}; padding: 4px 10px; }}"
                )
            else:
                btn.setStyleSheet("")
        self._refresh_async()

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _clear_list(self) -> None:
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_async(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class RefreshThread(QThread):
            def __init__(self, http, status_filter):
                super().__init__()
                self._http = http
                self._status_filter = status_filter
                self.data = None

            def run(self):
                params = {"summary": "true"}
                if self._status_filter:
                    params["status"] = self._status_filter
                self.data = self._http.get("/wip", params=params)

        self._refresh_thread = RefreshThread(self._http, self._status_filter)
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
        self._tasks = data.get("tasks", []) or []
        active = sum(1 for t in self._tasks if t.get("status") == "active")
        self._stats_label.setText(
            f"共 {len(self._tasks)} 个 / {active} 进行中"
        )

        # 重建前：从现有 card 收集展开状态，并保存滚动位置
        self._expanded_ids.clear()
        for i in range(self._list_layout.count()):
            it = self._list_layout.itemAt(i)
            w = it.widget() if it else None
            if isinstance(w, _WipTaskCard) and w._expanded:
                tid = w._task.get("id") or w._task.get("task_id", "")
                if tid:
                    self._expanded_ids.add(tid)
        scroll_pos = self._scroll.verticalScrollBar().value()

        self._clear_list()
        has_tasks = len(self._tasks) > 0
        self._scroll.setVisible(has_tasks)
        self._empty_state.setVisible(not has_tasks)

        for task in self._tasks:
            card = _WipTaskCard(task, self)
            tid = task.get("id") or task.get("task_id", "")
            if tid in self._expanded_ids:
                # set_expanded 触发 toggle → 自动重新加载详情（_load_detail）
                card.set_expanded(True)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)

        # 恢复滚动位置
        self._scroll.verticalScrollBar().setValue(scroll_pos)

    # —— 操作 ——

    def update_status(self, task_id: str, status: str) -> None:
        if not task_id:
            return
        if status == "completed":
            ret = QMessageBox.question(
                self, "确认", "确认标记此任务为已完成？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        worker = self._make_worker("put", f"/wip/{task_id}", json={"status": status})
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()
