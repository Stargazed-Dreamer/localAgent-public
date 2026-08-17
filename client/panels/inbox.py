"""Inbox 面板 — 收件箱（主面板分类，待办二级入口）

展示 Loop 推送的待审查条目，纵向列表 + 点击展开详情。

功能：
- 多选框 + 全选/反选
- 顶部批量操作：标记已解决 / 忽略 / 删除（对选中项操作）
- 下拉筛选：全部 / 待处理 / 已解决 / 已忽略
- 一键全部展开 / 全部收起
- 操作按钮在标题行（收起状态即可操作）：解决 / 忽略 / 删除
- 点击标题展开/收起详情

API:
- GET /inbox?status=... — 列出条目
- PATCH /inbox/{id} — 更新状态（resolved/ignored）
- DELETE /inbox/{id} — 删除条目
- POST /inbox/batch — 批量操作
"""

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from lib.ui import icon_button, tokens
from lib.ui.theme import set_kind, set_text_role

_STATUS_COLORS = {
    "pending": tokens.WARNING_TEXT,
    "resolved": tokens.SUCCESS_TEXT,
    "ignored": tokens.TEXT_TERTIARY,
}

_STATUS_TEXT = {
    "pending": "待处理",
    "resolved": "已解决",
    "ignored": "已忽略",
}


class _InboxItemCard(QFrame):
    """单个收件箱条目卡片（含多选框 + 标题行操作按钮）"""

    def __init__(self, item: dict, parent_panel, parent=None):
        super().__init__(parent)
        self._item = item
        self._parent_panel = parent_panel
        self._expanded = False

        self.setObjectName("inboxCard")
        set_kind(self, "card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题行
        self._header = QFrame()
        self._header.setObjectName("inboxCardHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            f"QFrame#inboxCardHeader {{ background: {tokens.BG_CARD}; border: none; padding: 4px; }}"
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        # 多选框（点击不触发展开）
        self._checkbox = QCheckBox()
        # 连接状态变化信号，使批量按钮即时刷新（此前缺失，导致勾选后按钮不亮）
        self._checkbox.stateChanged.connect(
            lambda _s: parent_panel._update_batch_buttons()
        )
        header_layout.addWidget(self._checkbox)

        # 状态指示
        status = item.get("status", "pending")
        color = _STATUS_COLORS.get(status, tokens.TEXT_TERTIARY)
        self._status_icon = QLabel("●")
        self._status_icon.setStyleSheet(f"color: {color}; font-size: 14px;")
        header_layout.addWidget(self._status_icon)

        # 标题
        title = item.get("title", "(无标题)")
        self._title_label = QLabel(title)
        set_text_role(self._title_label, "title")
        header_layout.addWidget(self._title_label, 1)

        # 来源
        source = item.get("source", "")
        if source:
            source_label = QLabel(f"[{source}]")
            set_text_role(source_label, "secondary")
            header_layout.addWidget(source_label)

        # 状态文本
        self._status_text = QLabel(_STATUS_TEXT.get(status, status))
        self._status_text.setStyleSheet(f"color: {color};")
        header_layout.addWidget(self._status_text)

        # 操作按钮（在标题行，收起状态可操作）
        if status == "pending":
            resolve_btn = icon_button("check", "标记已解决", color=tokens.SUCCESS_TEXT)
            resolve_btn.clicked.connect(lambda _e: parent_panel.resolve_item(item.get("id", "")))
            header_layout.addWidget(resolve_btn)

            ignore_btn = icon_button("eye-off", "忽略", color=tokens.TEXT_TERTIARY)
            ignore_btn.clicked.connect(lambda _e: parent_panel.ignore_item(item.get("id", "")))
            header_layout.addWidget(ignore_btn)

        delete_btn = icon_button("trash", "删除", color=tokens.ICON_DANGER)
        delete_btn.clicked.connect(lambda _e: parent_panel.delete_item(item.get("id", "")))
        header_layout.addWidget(delete_btn)

        # 箭头
        self._arrow = QLabel("▶")
        set_text_role(self._arrow, "secondary")
        header_layout.addWidget(self._arrow)

        # 点击标题区（不含按钮和复选框）展开/收起
        self._header.mousePressEvent = lambda e: self.toggle()
        layout.addWidget(self._header)

        # 时间行（标题下方，不占横向空间）
        time_parts = []
        updated_at = item.get("updated_at", "")
        created_at = item.get("created_at", "")
        if updated_at:
            time_parts.append(f"更新: {updated_at}")
        elif created_at:
            time_parts.append(f"创建: {created_at}")
        if time_parts:
            time_label = QLabel("  ".join(time_parts))
            time_label.setStyleSheet(
                f"color: {tokens.TEXT_TERTIARY}; background: {tokens.BG_BASE}; padding: 2px 14px;"
            )
            layout.addWidget(time_label)

        # 详情区
        self._body = QFrame()
        self._body.setStyleSheet(
            f"QFrame {{ background: {tokens.BG_BASE}; border: none; }}"
        )
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(14, 10, 14, 10)
        body_layout.setSpacing(6)

        # 字段
        for key, label in [
            ("id", "ID"),
            ("category", "分类"),
            ("source", "来源"),
            ("created_at", "创建时间"),
            ("updated_at", "更新时间"),
            ("resolved_at", "解决时间"),
        ]:
            val = item.get(key)
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

        # 描述
        desc = item.get("description", "")
        if desc:
            desc_label = QLabel("描述")
            set_text_role(desc_label, "secondary")
            body_layout.addWidget(desc_label)
            desc_text = QLabel(desc)
            set_text_role(desc_text, "secondary")
            desc_text.setWordWrap(True)
            desc_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body_layout.addWidget(desc_text)

        # payload JSON
        payload = item.get("payload", {})
        if payload:
            payload_label = QLabel("Payload")
            set_text_role(payload_label, "secondary")
            body_layout.addWidget(payload_label)
            payload_view = QTextEdit()
            payload_view.setReadOnly(True)
            payload_view.setMaximumHeight(120)
            try:
                payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
            except Exception:
                payload_text = str(payload)
            payload_view.setPlainText(payload_text)
            payload_view.setStyleSheet(
                f"QTextEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
                f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
                f" border: 1px solid {tokens.BG_CARD}; }}"
            )
            body_layout.addWidget(payload_view)

        # resolution
        resolution = item.get("resolution")
        if resolution:
            res_label = QLabel("解决结果")
            set_text_role(res_label, "secondary")
            body_layout.addWidget(res_label)
            try:
                res_text = json.dumps(resolution, ensure_ascii=False, indent=2)
            except Exception:
                res_text = str(resolution)
            res_view = QTextEdit()
            res_view.setReadOnly(True)
            res_view.setMaximumHeight(80)
            res_view.setPlainText(res_text)
            res_view.setStyleSheet(
                f"QTextEdit {{ background: {tokens.BG_BASE}; color: {tokens.TEXT_PRIMARY};"
                f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
                f" border: 1px solid {tokens.BG_CARD}; }}"
            )
            body_layout.addWidget(res_view)

        self._body.setVisible(False)
        layout.addWidget(self._body)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow.setText("▼" if self._expanded else "▶")

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self.toggle()

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)


class InboxPanel(PanelBase):
    """收件箱面板"""

    PANEL_META = PanelMeta(
        id="inbox",
        title="收件箱",
        icon="inbox",
        order=21,
        category="main",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._items: list[dict] = []
        self._cards: list[_InboxItemCard] = []
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None
        self._select_all_cb: QCheckBox | None = None
        # UI 状态记忆：刷新后恢复选中/展开，避免被定时重建清空
        self._expanded_ids: set[str] = set()
        self._checked_ids: set[str] = set()

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
        layout.setSpacing(10)

        # 顶栏
        header_row = QHBoxLayout()
        title = QLabel("收件箱")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        # 下拉筛选
        self._filter_combo = QComboBox()
        self._filter_combo.addItem("仅待处理", "pending")
        self._filter_combo.addItem("全部", "")
        self._filter_combo.addItem("已解决", "resolved")
        self._filter_combo.addItem("已忽略", "ignored")
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._filter_combo.setStyleSheet(
            f"QComboBox {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 4px 8px; min-width: 100px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
            f"QComboBox QAbstractItemView {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" selection-background-color: {tokens.ACCENT}; }}"
        )
        header_row.addWidget(self._filter_combo)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 批量操作栏
        batch_row = QHBoxLayout()
        batch_row.setSpacing(8)

        self._select_all_cb = QCheckBox("全选")
        self._select_all_cb.stateChanged.connect(self._on_select_all)
        batch_row.addWidget(self._select_all_cb)

        invert_btn = QPushButton("反选")
        invert_btn.clicked.connect(self._on_invert_select)
        invert_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        batch_row.addWidget(invert_btn)

        batch_row.addSpacing(12)

        self._batch_resolve_btn = QPushButton("批量解决")
        self._batch_resolve_btn.clicked.connect(self._batch_resolve)
        self._batch_resolve_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.SUCCESS}; color: {tokens.TEXT_ON_ACCENT}; padding: 4px 10px; border: none; }}"
            f"QPushButton:hover {{ background: {tokens.SUCCESS_TEXT}; }}"
            f"QPushButton:disabled {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_TERTIARY}; }}"
        )
        self._batch_resolve_btn.setEnabled(False)
        batch_row.addWidget(self._batch_resolve_btn)

        self._batch_ignore_btn = QPushButton("批量忽略")
        self._batch_ignore_btn.clicked.connect(self._batch_ignore)
        self._batch_ignore_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.TEXT_TERTIARY}; color: {tokens.TEXT_ON_ACCENT}; padding: 4px 10px; border: none; }}"
            f"QPushButton:hover {{ background: {tokens.TEXT_SECONDARY}; }}"
            f"QPushButton:disabled {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_TERTIARY}; }}"
        )
        self._batch_ignore_btn.setEnabled(False)
        batch_row.addWidget(self._batch_ignore_btn)

        self._batch_delete_btn = QPushButton("批量删除")
        self._batch_delete_btn.clicked.connect(self._batch_delete)
        set_kind(self._batch_delete_btn, "danger")
        self._batch_delete_btn.setEnabled(False)
        batch_row.addWidget(self._batch_delete_btn)

        batch_row.addStretch()

        expand_all_btn = QPushButton("全部展开")
        expand_all_btn.clicked.connect(self._expand_all)
        expand_all_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        batch_row.addWidget(expand_all_btn)

        collapse_all_btn = QPushButton("全部收起")
        collapse_all_btn.clicked.connect(self._collapse_all)
        collapse_all_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        batch_row.addWidget(collapse_all_btn)

        layout.addLayout(batch_row)

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
        self._empty_label = QLabel("没有待处理条目")
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
            self._items = []
            self._clear_list()

    # —— 筛选 ——

    def _on_filter_changed(self) -> None:
        self._refresh_async()

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

        status_filter = self._filter_combo.currentData()

        class RefreshThread(QThread):
            def __init__(self, http, status_filter):
                super().__init__()
                self._http = http
                self._status_filter = status_filter
                self.data = None

            def run(self):
                params = {"limit": 1000}
                if self._status_filter:
                    params["status"] = self._status_filter
                self.data = self._http.get("/inbox", params=params)

        self._refresh_thread = RefreshThread(self._http, status_filter)
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
        self._items = data.get("items", []) or []
        pending = sum(1 for i in self._items if i.get("status") == "pending")
        self._stats_label.setText(
            f"共 {len(self._items)} 条 / {pending} 待处理"
        )

        # 重建前：按 item id 收集当前展开/选中状态，并保存滚动位置
        self._expanded_ids = {
            c._item.get("id", "") for c in self._cards if c._expanded
        }
        self._checked_ids = {
            c._item.get("id", "") for c in self._cards if c.is_checked()
        }
        scroll_pos = self._scroll.verticalScrollBar().value()

        self._clear_list()
        self._empty_label.setVisible(len(self._items) == 0)

        for item in self._items:
            card = _InboxItemCard(item, self)
            item_id = item.get("id", "")
            if item_id in self._expanded_ids:
                card.set_expanded(True)
            if item_id in self._checked_ids:
                # 屏蔽信号避免逐条触发 _update_batch_buttons
                card._checkbox.blockSignals(True)
                card.set_checked(True)
                card._checkbox.blockSignals(False)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)
            self._cards.append(card)

        self._update_batch_buttons()

        # 恢复滚动位置
        self._scroll.verticalScrollBar().setValue(scroll_pos)

    # —— 选择操作 ——

    def _on_select_all(self, state: int) -> None:
        checked = state == 2  # Qt.Checked
        for card in self._cards:
            card.set_checked(checked)
        self._update_batch_buttons()

    def _on_invert_select(self) -> None:
        for card in self._cards:
            card.set_checked(not card.is_checked())
        self._select_all_cb.blockSignals(True)
        all_checked = all(c.is_checked() for c in self._cards) if self._cards else False
        self._select_all_cb.setChecked(all_checked)
        self._select_all_cb.blockSignals(False)
        self._update_batch_buttons()

    def _get_selected_ids(self) -> list[str]:
        return [
            c._item.get("id", "")
            for c in self._cards
            if c.is_checked() and c._item.get("id")
        ]

    def _update_batch_buttons(self) -> None:
        selected = self._get_selected_ids()
        has_selected = len(selected) > 0
        self._batch_resolve_btn.setEnabled(has_selected)
        self._batch_ignore_btn.setEnabled(has_selected)
        self._batch_delete_btn.setEnabled(has_selected)

    # —— 展开/收起 ——

    def _expand_all(self) -> None:
        for card in self._cards:
            card.set_expanded(True)

    def _collapse_all(self) -> None:
        for card in self._cards:
            card.set_expanded(False)

    # —— 单项操作 ——

    def resolve_item(self, item_id: str) -> None:
        if not item_id:
            return
        worker = self._make_worker("patch", f"/inbox/{item_id}", json={"status": "resolved"})
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()

    def ignore_item(self, item_id: str) -> None:
        if not item_id:
            return
        worker = self._make_worker("patch", f"/inbox/{item_id}", json={"status": "ignored"})
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()

    def delete_item(self, item_id: str) -> None:
        if not item_id:
            return
        ret = QMessageBox.question(
            self, "确认", "确认删除此条目？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("delete", f"/inbox/{item_id}")
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()

    # —— 批量操作 ——

    def _batch_resolve(self) -> None:
        ids = self._get_selected_ids()
        if not ids:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认将 {len(ids)} 个条目标记为已解决？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("post", "/inbox/batch", json={"action": "resolve", "ids": ids})
        worker.done.connect(self._on_batch_done)
        worker.failed.connect(lambda _err: self._on_batch_done(None))
        worker.start()

    def _batch_ignore(self) -> None:
        ids = self._get_selected_ids()
        if not ids:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认忽略 {len(ids)} 个条目？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("post", "/inbox/batch", json={"action": "ignore", "ids": ids})
        worker.done.connect(self._on_batch_done)
        worker.failed.connect(lambda _err: self._on_batch_done(None))
        worker.start()

    def _batch_delete(self) -> None:
        ids = self._get_selected_ids()
        if not ids:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认删除 {len(ids)} 个条目？此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("post", "/inbox/batch", json={"action": "delete", "ids": ids})
        worker.done.connect(self._on_batch_done)
        worker.failed.connect(lambda _err: self._on_batch_done(None))
        worker.start()

    def _on_batch_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "批量操作失败")
        self._refresh_async()
