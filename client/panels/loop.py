"""Loop 面板 — Loop 任务调度监控（监控分类）

从 web 界面 loop 标签页迁移，重新设计：
- 纵向列表布局（不左右分栏，避免右侧空白浪费）
- 每个任务一行：状态图标 + task_id + action_type + 播放/暂停按钮 + 立即运行按钮
- 点击标题栏展开/收起详情
- 状态：●绿=运行中 / ●黄=已暂停 / ●灰=已禁用 / ●蓝=执行中

API:
- GET /loop/tasks — 列出所有任务
- POST /loop/tasks/{task_id}/run — 立即运行
- POST /loop/tasks/{task_id}/pause — 暂停
- POST /loop/tasks/{task_id}/resume — 恢复
"""

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import icon, icon_button, tokens
from lib.ui.theme import set_kind, set_text_role


def _status_indicator(task: dict) -> tuple[str, str]:
    """返回 (图标, 颜色)"""
    if not task.get("enabled", True):
        return ("●", tokens.TEXT_TERTIARY)  # 灰：禁用
    if task.get("running", False):
        return ("●", tokens.INFO_TEXT)  # 蓝：执行中
    if task.get("paused", False):
        return ("●", tokens.WARNING_TEXT)  # 黄：暂停
    return ("●", tokens.SUCCESS_TEXT)  # 绿：正常等待


def _status_text(task: dict) -> str:
    if not task.get("enabled", True):
        return "禁用"
    if task.get("running", False):
        return "执行中"
    if task.get("paused", False):
        return "已暂停"
    return "等待中"


class _LoopTaskCard(QFrame):
    """单个 Loop 任务卡片"""

    def __init__(self, task: dict, parent_panel, parent=None):
        super().__init__(parent)
        self._task = task
        self._parent_panel = parent_panel
        self._expanded = False

        self.setObjectName("loopCard")
        set_kind(self, "card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题行（点击展开/收起）
        self._header = QFrame()
        self._header.setObjectName("loopCardHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            f"QFrame#loopCardHeader {{ background: {tokens.BG_CARD}; border: none; padding: 4px; }}"
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        status_symbol, color = _status_indicator(task)
        self._status_icon = QLabel(status_symbol)
        self._status_icon.setStyleSheet(f"color: {color}; font-size: 14px;")
        header_layout.addWidget(self._status_icon)

        task_id = task.get("task_id", "?")
        self._title_label = QLabel(task_id)
        set_text_role(self._title_label, "title")
        header_layout.addWidget(self._title_label)

        action_type = task.get("action_type", "")
        self._action_label = QLabel(f"[{action_type}]")
        set_text_role(self._action_label, "secondary")
        header_layout.addWidget(self._action_label)

        header_layout.addStretch()

        # 下次运行时间（标题行直接显示）
        self._next_run_label = QLabel("")
        set_text_role(self._next_run_label, "secondary")
        header_layout.addWidget(self._next_run_label)

        # 失败次数（标题行直接显示，>0 标红）
        self._fail_count_label = QLabel("")
        set_text_role(self._fail_count_label, "secondary")
        header_layout.addWidget(self._fail_count_label)

        # 状态文本
        self._status_text_label = QLabel(_status_text(task))
        self._status_text_label.setStyleSheet(f"color: {color};")
        header_layout.addWidget(self._status_text_label)

        # 立即运行按钮
        self._run_btn = icon_button("refresh", "立即运行一次", color=tokens.ICON_ACTIVE)
        self._run_btn.clicked.connect(self._on_run_clicked)
        header_layout.addWidget(self._run_btn)

        # 播放/暂停按钮
        self._toggle_btn = icon_button("pause", "立即暂停")
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)
        header_layout.addWidget(self._toggle_btn)

        # 展开箭头
        self._arrow_label = QLabel("▶")
        set_text_role(self._arrow_label, "secondary")
        header_layout.addWidget(self._arrow_label)

        self._header.mousePressEvent = lambda _e: self.toggle()
        layout.addWidget(self._header)

        # 详情区（默认隐藏）
        self._body = QFrame()
        self._body.setObjectName("loopCardBody")
        self._body.setStyleSheet(
            f"QFrame#loopCardBody {{ background: {tokens.BG_BASE}; border: none; }}"
        )
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(14, 10, 14, 10)
        body_layout.setSpacing(6)

        # 字段
        self._fields: dict[str, QLabel] = {}
        for key, label in [
            ("trigger", "触发器"),
            ("source_segment", "来源段"),
            ("last_run_at", "上次运行"),
            ("next_run_at", "下次运行"),
            ("run_count", "运行次数"),
            ("fail_count", "失败次数"),
            ("fail_threshold", "失败阈值"),
            ("paused_reason", "暂停原因"),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label)
            set_text_role(lbl, "secondary")
            lbl.setFixedWidth(80)
            val = QLabel("—")
            set_text_role(val, "secondary")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(lbl)
            row.addWidget(val, 1)
            body_layout.addLayout(row)
            self._fields[key] = val

        # last_result JSON 预览
        result_label = QLabel("上次结果")
        set_text_role(result_label, "secondary")
        body_layout.addWidget(result_label)

        self._result_view = QTextEdit()
        self._result_view.setReadOnly(True)
        self._result_view.setMaximumHeight(140)
        self._result_view.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
            f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
            f" border: 1px solid {tokens.BG_CARD}; }}"
        )
        body_layout.addWidget(self._result_view)

        self._body.setVisible(False)
        layout.addWidget(self._body)

        self._refresh(task)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow_label.setText("▼" if self._expanded else "▶")

    def _refresh(self, task: dict) -> None:
        """更新卡片显示（不重建组件）"""
        self._task = task

        status_symbol, color = _status_indicator(task)
        self._status_icon.setText(status_symbol)
        self._status_icon.setStyleSheet(f"color: {color}; font-size: 14px;")
        self._status_text_label.setText(_status_text(task))
        self._status_text_label.setStyleSheet(f"color: {color};")

        task_id = task.get("task_id", "?")
        self._title_label.setText(task_id)
        self._action_label.setText(f"[{task.get('action_type', '')}]")

        # 下次运行时间（标题行）
        next_run = task.get("next_run_at", "")
        if next_run:
            self._next_run_label.setText(next_run)
            self._next_run_label.setToolTip(f"下次运行: {next_run}")
        else:
            self._next_run_label.setText("")

        # 失败次数（标题行，>0 标红）
        fail_count = task.get("fail_count", 0)
        fail_threshold = task.get("fail_threshold", 5)
        try:
            fail_count = int(fail_count)
        except (TypeError, ValueError):
            fail_count = 0
        if fail_count > 0:
            self._fail_count_label.setText(f"失败 {fail_count}/{fail_threshold}")
            self._fail_count_label.setStyleSheet(f"color: {tokens.DANGER_TEXT}; font-weight: bold;")
            self._fail_count_label.setToolTip(f"连续失败 {fail_count} 次（阈值 {fail_threshold}）")
        else:
            self._fail_count_label.setText("")
            self._fail_count_label.setStyleSheet("")

        # 字段填充
        self._fields["trigger"].setText(str(task.get("trigger", "—")))
        self._fields["source_segment"].setText(str(task.get("source_segment", "—")))
        self._fields["last_run_at"].setText(str(task.get("last_run_at", "—")))
        self._fields["next_run_at"].setText(str(task.get("next_run_at", "—")))
        self._fields["run_count"].setText(str(task.get("run_count", 0)))
        self._fields["fail_count"].setText(str(task.get("fail_count", 0)))
        self._fields["fail_threshold"].setText(str(task.get("fail_threshold", 5)))
        paused_reason = task.get("paused_reason") or "—"
        self._fields["paused_reason"].setText(str(paused_reason))

        # last_result
        last_result = task.get("last_result")
        if last_result is None:
            self._result_view.setPlainText("（无）")
        elif isinstance(last_result, (dict, list)):
            try:
                text = json.dumps(last_result, ensure_ascii=False, indent=2)
            except Exception:
                text = str(last_result)
            self._result_view.setPlainText(text)
        else:
            self._result_view.setPlainText(str(last_result))

        # 按钮状态
        is_enabled = task.get("enabled", True)
        is_paused = task.get("paused", False)
        is_running = task.get("running", False)

        # 播放/暂停按钮
        if not is_enabled:
            self._toggle_btn.setIcon(icon("pause", tokens.TEXT_DISABLED))
            self._toggle_btn.setEnabled(False)
            self._toggle_btn.setToolTip("任务已禁用")
            self._toggle_btn.setStatusTip("任务已禁用")
        elif is_paused:
            self._toggle_btn.setIcon(icon("play", tokens.ICON_ACTIVE))
            self._toggle_btn.setToolTip("恢复运行")
            self._toggle_btn.setStatusTip("恢复运行")
            self._toggle_btn.setEnabled(True)
        else:
            self._toggle_btn.setIcon(icon("pause", tokens.ICON_DEFAULT))
            self._toggle_btn.setToolTip("立即暂停")
            self._toggle_btn.setStatusTip("立即暂停")
            self._toggle_btn.setEnabled(True)

        # 立即运行按钮
        self._run_btn.setEnabled(is_enabled and not is_running)
        if is_running:
            self._run_btn.setToolTip("正在执行中")
            self._run_btn.setStatusTip("正在执行中")
        elif not is_enabled:
            self._run_btn.setToolTip("任务已禁用")
            self._run_btn.setStatusTip("任务已禁用")
        else:
            self._run_btn.setToolTip("立即运行一次")
            self._run_btn.setStatusTip("立即运行一次")

    def _on_run_clicked(self) -> None:
        task_id = self._task.get("task_id", "")
        if not task_id:
            return
        self._parent_panel.run_task_now(task_id)

    def _on_toggle_clicked(self) -> None:
        task_id = self._task.get("task_id", "")
        if not task_id:
            return
        is_paused = self._task.get("paused", False)
        if is_paused:
            self._parent_panel.resume_task(task_id)
        else:
            self._parent_panel.pause_task(task_id)


class LoopPanel(PanelBase):
    """Loop 任务调度监控面板"""

    PANEL_META = PanelMeta(
        id="loop",
        title="Loop",
        icon="refresh",
        order=42,
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._tasks: list[dict] = []
        self._task_cards: dict[str, _LoopTaskCard] = {}
        self._expanded_ids: set[str] = set()
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None

        # 自动刷新（15s）
        self._timer = QTimer(self)
        self._timer.setInterval(15000)
        self._timer.timeout.connect(self._refresh_async)

        self._build_ui()

    # —— UI 构建 ——

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
        title = QLabel("Loop 任务调度")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 统计行
        self._stats_label = QLabel("加载中...")
        set_text_role(self._stats_label, "secondary")
        layout.addWidget(self._stats_label)

        # 滚动列表
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()

        scroll.setWidget(self._list_container)
        layout.addWidget(scroll, 1)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._timer.start()
        if self._tasks:
            self._refresh_async()
        else:
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

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _clear_list(self) -> None:
        # 移除所有卡片（保留末尾的 stretch）
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._task_cards.clear()

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
                self.data = self._http.get("/loop/tasks")

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

        self._tasks = data.get("tasks", []) or []
        total = data.get("task_count", len(self._tasks))
        active = data.get("active_count", 0)
        paused = data.get("paused_count", 0)
        self._stats_label.setText(
            f"共 {total} 个任务 / {active} 活跃 / {paused} 暂停"
        )

        # 记录当前展开状态
        for tid, card in self._task_cards.items():
            if card._expanded:
                self._expanded_ids.add(tid)

        # 重建列表
        self._clear_list()
        for task in self._tasks:
            tid = task.get("task_id", "")
            if not tid:
                continue
            card = _LoopTaskCard(task, self)
            if tid in self._expanded_ids:
                card._expanded = True
                card._body.setVisible(True)
                card._arrow_label.setText("▼")
            # 插到 stretch 之前
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)
            self._task_cards[tid] = card

    # —— 操作 ——

    def run_task_now(self, task_id: str) -> None:
        """立即运行任务（长超时，Loop 任务执行可能很慢）。异步 POST 不阻塞 UI。"""
        # Loop 任务执行可能很慢（如 github 任务），用 120s 超时
        worker = self._make_worker("post", f"/loop/tasks/{task_id}/run", timeout=120.0)
        worker.done.connect(lambda resp: self._on_run_task_done(task_id, resp))
        worker.failed.connect(lambda _err: self._on_run_task_done(task_id, None))
        worker.start()

    def _on_run_task_done(self, task_id: str, resp: dict | None) -> None:
        if resp is None:
            # 超时或网络错误，任务可能仍在执行
            QMessageBox.information(
                self, "已触发",
                f"任务 {task_id} 已触发，可能仍在执行中。\n请稍后刷新查看结果。"
            )
            self._refresh_async()
            return
        result = resp.get("result", {}) if isinstance(resp, dict) else {}
        success = result.get("success", False) if isinstance(result, dict) else False
        if success:
            QMessageBox.information(
                self, "成功",
                f"任务 {task_id} 已触发执行\n\n结果：{result.get('summary', '')}"
                if isinstance(result, dict) else f"任务 {task_id} 已触发执行"
            )
        else:
            err = result.get("error", "未知错误") if isinstance(result, dict) else "未知错误"
            QMessageBox.warning(self, "执行失败", f"任务 {task_id} 执行失败：\n{err}")
        self._refresh_async()

    def pause_task(self, task_id: str) -> None:
        worker = self._make_worker("post", f"/loop/tasks/{task_id}/pause")
        worker.done.connect(lambda resp: self._on_pause_resume_done(task_id, "暂停", resp))
        worker.failed.connect(lambda _err: self._on_pause_resume_done(task_id, "暂停", None))
        worker.start()

    def resume_task(self, task_id: str) -> None:
        worker = self._make_worker("post", f"/loop/tasks/{task_id}/resume")
        worker.done.connect(lambda resp: self._on_pause_resume_done(task_id, "恢复", resp))
        worker.failed.connect(lambda _err: self._on_pause_resume_done(task_id, "恢复", None))
        worker.start()

    def _on_pause_resume_done(self, task_id: str, action: str, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", f"{action}任务失败：{task_id}")
        self._refresh_async()
