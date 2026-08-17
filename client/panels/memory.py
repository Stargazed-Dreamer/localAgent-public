"""Memory 面板 — 三层记忆系统监控

展示记忆系统状态、记忆列表、时间线，支持维护操作。

功能：
- Tab1 概览：统计卡片 + 维护状态 + 维护操作按钮
- Tab2 记忆：搜索/列表/详情/删除
- Tab3 时间线：按时间范围/source 筛选消息

API:
- GET /memory/status — 系统状态
- GET /memory/list — key 列表
- GET /memory/{key} — 单条详情
- DELETE /memory/{key} — 删除
- POST /memory/search — 语义搜索
- GET /memory/timeline — 时间线
- POST /memory/maintain — 触发维护
- POST /memory/reindex — 重建索引
- POST /memory/compress — 压缩
- POST /memory/cleanup/orphaned — 清理孤立
"""

import json

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


class _StatCard(QFrame):
    """概览统计卡片：标题 + 数值"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        set_kind(self, "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self._title_label = QLabel(title)
        set_text_role(self._title_label, "secondary")
        layout.addWidget(self._title_label)

        self._value_label = QLabel("—")
        self._value_label.setStyleSheet(
            f"font-size: {tokens.FONT_DISPLAY}px; font-weight: 600;"
            f" color: {tokens.TEXT_PRIMARY};"
        )
        layout.addWidget(self._value_label)

    def set_value(self, value: str) -> None:
        """设置数值，恢复默认颜色"""
        self._value_label.setText(value)
        self._value_label.setStyleSheet(
            f"font-size: {tokens.FONT_DISPLAY}px; font-weight: 600;"
            f" color: {tokens.TEXT_PRIMARY};"
        )

    def set_value_color(self, value: str, color: str) -> None:
        """设置数值 + 自定义颜色（如绿色就绪/红色不可用）"""
        self._value_label.setText(value)
        self._value_label.setStyleSheet(
            f"font-size: {tokens.FONT_DISPLAY}px; font-weight: 600; color: {color};"
        )


class MemoryPanel(PanelBase):
    """记忆系统面板"""

    PANEL_META = PanelMeta(
        id="memory",
        title="记忆",
        icon="brain",
        order=42,            # monitor 类，紧挨 terminal(41)
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._refresh_btn: QPushButton | None = None
        # 各操作的异步线程槽（参考 inbox.py 的 _refresh_thread 模式，按操作分离）
        self._overview_thread = None
        self._list_thread = None
        self._detail_thread = None
        self._timeline_thread = None
        self._action_thread = None
        # 列表数据
        self._memory_keys: list[dict] = []
        self._timeline_messages: list[dict] = []
        self._current_detail_key: str | None = None
        # 统计卡片引用
        self._stat_cards: dict[str, _StatCard] = {}
        # 维护状态标签引用
        self._maintainer_labels: dict[str, QLabel] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(60000)  # 60s 概览自动刷新
        self._timer.timeout.connect(self._refresh_overview_async)

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
        layout.setSpacing(10)

        # 顶栏
        header_row = QHBoxLayout()
        title = QLabel("记忆")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 三 Tab
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_overview_tab(), "概览")
        self._tabs.addTab(self._build_memory_tab(), "记忆")
        self._tabs.addTab(self._build_timeline_tab(), "时间线")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs, 1)

    def _build_overview_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 滚动容器（概览内容可能较长）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(12)

        # —— 统计卡片网格 ——
        stats_title = QLabel("统计")
        set_text_role(stats_title, "title")
        inner_layout.addWidget(stats_title)

        stats_grid = QGridLayout()
        stats_grid.setSpacing(8)
        card_defs = [
            ("messages", "消息总数"),
            ("facts", "Facts 数"),
            ("summaries", "摘要数"),
            ("db_size_mb", "DB 大小(MB)"),
            ("embedding", "Embedding 状态"),
            ("pending_messages", "待处理消息"),
            ("interaction_count", "交互总数"),
            ("bm25_terms", "BM25 词项"),
            ("vectors", "向量数"),
        ]
        for i, (key, card_title) in enumerate(card_defs):
            card = _StatCard(card_title)
            self._stat_cards[key] = card
            stats_grid.addWidget(card, i // 3, i % 3)
        inner_layout.addLayout(stats_grid)

        # Embedding 模型名
        self._embedding_model_label = QLabel("Embedding 模型: —")
        set_text_role(self._embedding_model_label, "secondary")
        inner_layout.addWidget(self._embedding_model_label)

        # —— 维护状态区 ——
        maint_title = QLabel("维护状态")
        set_text_role(maint_title, "title")
        inner_layout.addWidget(maint_title)

        maint_card = QFrame()
        set_kind(maint_card, "card")
        maint_layout = QGridLayout(maint_card)
        maint_layout.setContentsMargins(12, 10, 12, 10)
        maint_layout.setSpacing(6)
        maint_fields = [
            ("last_run", "上次运行"),
            ("stale_threshold_days", "Stale 阈值(天)"),
            ("interval_hours", "运行间隔(小时)"),
            ("validate_enabled", "LLM 验证"),
            ("status_archive", "Archive 数"),
            ("status_keep", "Keep 数"),
            ("status_pending", "Pending 数"),
            ("stale_total", "Stale 总数"),
        ]
        for i, (key, label_text) in enumerate(maint_fields):
            row = i // 2
            col = (i % 2) * 2
            lbl = QLabel(label_text)
            set_text_role(lbl, "secondary")
            maint_layout.addWidget(lbl, row, col)
            val = QLabel("—")
            set_text_role(val, "secondary")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            maint_layout.addWidget(val, row, col + 1)
            self._maintainer_labels[key] = val
        inner_layout.addWidget(maint_card)

        # —— 操作按钮区 ——
        ops_title = QLabel("维护操作")
        set_text_role(ops_title, "title")
        inner_layout.addWidget(ops_title)

        ops_row = QHBoxLayout()
        ops_row.setSpacing(8)

        maintain_btn = QPushButton("触发维护")
        maintain_btn.clicked.connect(self._on_maintain_clicked)
        maintain_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.ACCENT}; color: {tokens.TEXT_ON_ACCENT};"
            f" padding: 6px 14px; border: none; }}"
            f"QPushButton:hover {{ background: {tokens.ACCENT_HOVER}; }}"
        )
        ops_row.addWidget(maintain_btn)

        reindex_btn = QPushButton("重建索引")
        reindex_btn.clicked.connect(self._on_reindex_clicked)
        ops_row.addWidget(reindex_btn)

        compress_btn = QPushButton("压缩记忆")
        compress_btn.clicked.connect(self._on_compress_clicked)
        ops_row.addWidget(compress_btn)

        cleanup_btn = QPushButton("清理孤立")
        cleanup_btn.clicked.connect(self._on_cleanup_clicked)
        set_kind(cleanup_btn, "danger")
        ops_row.addWidget(cleanup_btn)

        ops_row.addStretch()
        inner_layout.addLayout(ops_row)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        return w

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 搜索行
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("输入关键词搜索记忆...")
        self._search_edit.returnPressed.connect(self._on_search_clicked)
        search_row.addWidget(self._search_edit, 1)

        search_btn = QPushButton("搜索")
        search_btn.clicked.connect(self._on_search_clicked)
        search_row.addWidget(search_btn)

        reset_btn = QPushButton("重置")
        reset_btn.clicked.connect(self._on_reset_search_clicked)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 3px 8px; }}"
            f"QPushButton:hover {{ background: {tokens.BG_HOVER}; }}"
        )
        search_row.addWidget(reset_btn)

        list_refresh_btn = QPushButton("刷新列表")
        list_refresh_btn.clicked.connect(self._refresh_list_async)
        search_row.addWidget(list_refresh_btn)
        layout.addLayout(search_row)

        # 列表
        self._memory_list = QListWidget()
        self._memory_list.itemClicked.connect(self._on_memory_item_clicked)
        layout.addWidget(self._memory_list, 1)

        # 详情区
        detail_header = QHBoxLayout()
        detail_label = QLabel("详情")
        set_text_role(detail_label, "title")
        detail_header.addWidget(detail_label)
        detail_header.addStretch()

        self._delete_memory_btn = QPushButton("删除")
        set_kind(self._delete_memory_btn, "danger")
        self._delete_memory_btn.setEnabled(False)
        self._delete_memory_btn.clicked.connect(self._on_delete_memory_clicked)
        detail_header.addWidget(self._delete_memory_btn)
        layout.addLayout(detail_header)

        self._memory_detail = QTextEdit()
        self._memory_detail.setReadOnly(True)
        self._memory_detail.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_CARD}; color: {tokens.TEXT_PRIMARY};"
            f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
            f" border: 1px solid {tokens.BORDER}; }}"
        )
        self._memory_detail.setMaximumHeight(220)
        layout.addWidget(self._memory_detail)
        return w

    def _build_timeline_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 筛选行
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self._time_range_cb = QCheckBox("限制时间范围")
        filter_row.addWidget(self._time_range_cb)

        self._since_edit = QDateTimeEdit()
        self._since_edit.setCalendarPopup(True)
        self._since_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._since_edit.setEnabled(False)
        filter_row.addWidget(self._since_edit)

        sep = QLabel("→")
        set_text_role(sep, "secondary")
        filter_row.addWidget(sep)

        self._until_edit = QDateTimeEdit()
        self._until_edit.setCalendarPopup(True)
        self._until_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._until_edit.setEnabled(False)
        filter_row.addWidget(self._until_edit)

        # 时间范围 checkbox 联动启用/禁用
        self._time_range_cb.toggled.connect(
            lambda checked: (
                self._since_edit.setEnabled(checked),
                self._until_edit.setEnabled(checked),
            )
        )

        filter_row.addSpacing(12)
        source_label = QLabel("来源:")
        set_text_role(source_label, "secondary")
        filter_row.addWidget(source_label)

        self._source_combo = QComboBox()
        self._source_combo.addItem("全部", None)
        self._source_combo.addItem("tool", "tool")
        self._source_combo.addItem("agent", "agent")
        self._source_combo.addItem("user", "user")
        filter_row.addWidget(self._source_combo)

        limit_label = QLabel("数量:")
        set_text_role(limit_label, "secondary")
        filter_row.addWidget(limit_label)

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 500)
        self._limit_spin.setValue(50)
        filter_row.addWidget(self._limit_spin)

        query_btn = QPushButton("查询")
        query_btn.clicked.connect(self._refresh_timeline_async)
        filter_row.addWidget(query_btn)
        layout.addLayout(filter_row)

        # 列表
        self._timeline_list = QListWidget()
        self._timeline_list.itemClicked.connect(self._on_timeline_item_clicked)
        layout.addWidget(self._timeline_list, 1)

        # 详情区
        detail_label = QLabel("详情")
        set_text_role(detail_label, "title")
        layout.addWidget(detail_label)

        self._timeline_detail = QTextEdit()
        self._timeline_detail.setReadOnly(True)
        self._timeline_detail.setStyleSheet(
            f"QTextEdit {{ background: {tokens.BG_CARD}; color: {tokens.TEXT_PRIMARY};"
            f" font-family: {tokens.FONT_MONO}; font-size: 11px;"
            f" border: 1px solid {tokens.BORDER}; }}"
        )
        self._timeline_detail.setMaximumHeight(220)
        layout.addWidget(self._timeline_detail)
        return w

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._timer.start()
        self._refresh_overview_async()

    def on_hide(self) -> None:
        self._timer.stop()

    def on_refresh(self) -> None:
        # 刷新当前 Tab
        idx = self._tabs.currentIndex() if hasattr(self, "_tabs") else 0
        if idx == 0:
            self._refresh_overview_async()
        elif idx == 1:
            self._refresh_list_async()
        elif idx == 2:
            self._refresh_timeline_async()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            self._refresh_overview_async()
        else:
            self._stack.setCurrentWidget(self._offline_widget)
            self._timer.stop()
            self._memory_keys = []
            self._timeline_messages = []
            self._memory_list.clear()
            self._timeline_list.clear()

    # —— Tab 切换 ——

    def _on_tab_changed(self, idx: int) -> None:
        # 首次进入 Tab 时自动加载（避免重复刷新）
        if idx == 1 and not self._memory_keys:
            self._refresh_list_async()
        elif idx == 2 and not self._timeline_messages:
            self._refresh_timeline_async()

    def _on_refresh_clicked(self) -> None:
        self.on_refresh()

    # —— 概览刷新 ——

    def _refresh_overview_async(self) -> None:
        if self._overview_thread is not None and self._overview_thread.isRunning():
            return

        class OverviewThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.data = None

            def run(self):
                self.data = self._http.get("/memory/status")

        self._overview_thread = OverviewThread(self._http)
        self._overview_thread.finished.connect(self._on_overview_done)
        self._set_loading(True)
        self._overview_thread.start()

    def _on_overview_done(self) -> None:
        self._set_loading(False)
        t = self._overview_thread
        if t is None or t.data is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)
        data = t.data
        if not isinstance(data, dict):
            return
        self._populate_overview(data)

    def _populate_overview(self, data: dict) -> None:
        # 统计卡片
        self._stat_cards["messages"].set_value(str(data.get("messages", 0)))
        self._stat_cards["facts"].set_value(str(data.get("facts", 0)))
        self._stat_cards["summaries"].set_value(str(data.get("summaries", 0)))
        db_size = data.get("db_size_mb", 0.0) or 0.0
        self._stat_cards["db_size_mb"].set_value(f"{db_size:.2f}")

        embedding_ready = data.get("embedding_ready", False)
        if embedding_ready:
            self._stat_cards["embedding"].set_value_color("就绪", tokens.SUCCESS_TEXT)
        else:
            self._stat_cards["embedding"].set_value_color("不可用", tokens.DANGER_TEXT)

        embedding_model = data.get("embedding_model", "") or "—"
        self._embedding_model_label.setText(f"Embedding 模型: {embedding_model}")

        self._stat_cards["pending_messages"].set_value(str(data.get("pending_messages", 0)))
        self._stat_cards["interaction_count"].set_value(str(data.get("interaction_count", 0)))
        self._stat_cards["bm25_terms"].set_value(str(data.get("bm25_terms", 0)))
        self._stat_cards["vectors"].set_value(str(data.get("vectors", 0)))

        # 维护状态
        maint = data.get("maintainer", {}) or {}
        self._maintainer_labels["last_run"].setText(str(maint.get("last_run", "—") or "—"))
        self._maintainer_labels["stale_threshold_days"].setText(
            str(maint.get("stale_threshold_days", "—") or "—")
        )
        self._maintainer_labels["interval_hours"].setText(
            str(maint.get("interval_hours", "—") or "—")
        )
        self._maintainer_labels["validate_enabled"].setText(
            "启用" if maint.get("validate_enabled") else "禁用"
        )
        status_counts = maint.get("status_counts", {}) or {}
        self._maintainer_labels["status_archive"].setText(
            str(status_counts.get("archive", 0))
        )
        self._maintainer_labels["status_keep"].setText(
            str(status_counts.get("keep", 0))
        )
        self._maintainer_labels["status_pending"].setText(
            str(status_counts.get("pending", 0))
        )
        stale_total = maint.get("stale_total", 0) or 0
        if stale_total and stale_total > 0:
            # stale 总数 > 0 时红色高亮
            self._maintainer_labels["stale_total"].setStyleSheet(
                f"color: {tokens.DANGER_TEXT}; font-weight: 600;"
            )
        else:
            self._maintainer_labels["stale_total"].setStyleSheet("")
        self._maintainer_labels["stale_total"].setText(str(stale_total))

    # —— 记忆列表 ——

    def _refresh_list_async(self) -> None:
        if self._list_thread is not None and self._list_thread.isRunning():
            return

        class ListThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.data = None

            def run(self):
                self.data = self._http.get("/memory/list")

        self._list_thread = ListThread(self._http)
        self._list_thread.finished.connect(self._on_list_done)
        self._set_loading(True)
        self._list_thread.start()

    def _on_list_done(self) -> None:
        self._set_loading(False)
        t = self._list_thread
        if t is None or t.data is None:
            return
        data = t.data
        if not isinstance(data, dict):
            return
        self._memory_keys = data.get("keys", []) or []
        self._populate_memory_list(self._memory_keys, is_search=False)

    def _populate_memory_list(self, items: list[dict], is_search: bool) -> None:
        """填充记忆列表（兼容 list 和 search 两种结果）"""
        self._memory_list.clear()
        if not items:
            empty = QListWidgetItem("(空)" if is_search else "没有记忆条目")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._memory_list.addItem(empty)
            return
        for item in items:
            if is_search:
                # search 结果：message_id / score / content / timestamp / source
                score = item.get("score", 0.0)
                content = item.get("content", "")
                source = item.get("source", "")
                timestamp = item.get("timestamp", "")
                preview = content[:60].replace("\n", " ")
                if len(content) > 60:
                    preview += "..."
                text = f"[{source}] score={score:.3f}  {preview}"
                if timestamp:
                    text += f"  @ {timestamp}"
                lw_item = QListWidgetItem(text)
                lw_item.setData(Qt.UserRole, item)
                self._memory_list.addItem(lw_item)
            else:
                # list 结果：key / summary / updated_at / fact_source / fact_type
                key = item.get("key", "")
                summary = item.get("summary", "")
                updated_at = item.get("updated_at", "")
                fact_source = item.get("fact_source")  # auto/manual
                fact_type = item.get("fact_type")       # preference/project/reference/...
                # 第 1 行：key + 来源/类型 badge
                text = key
                badges = []
                if fact_source == "auto":
                    badges.append("auto")
                if fact_type:
                    badges.append(fact_type)
                if badges:
                    text += "  [" + "/".join(badges) + "]"
                # 第 2 行：可读摘要（后端已解析 name/description，非裸 JSON）
                if summary:
                    text += "\n" + str(summary)[:80]
                # 第 3 行：更新时间
                if updated_at:
                    text += f"\n更新: {updated_at}"
                lw_item = QListWidgetItem(text)
                lw_item.setData(Qt.UserRole, item)
                self._memory_list.addItem(lw_item)

    # —— 搜索 ——

    def _on_search_clicked(self) -> None:
        query = self._search_edit.text().strip()
        if not query:
            QMessageBox.information(self, "提示", "请输入搜索关键词")
            return
        if self._list_thread is not None and self._list_thread.isRunning():
            return

        class SearchThread(QThread):
            def __init__(self, http, query):
                super().__init__()
                self._http = http
                self._query = query
                self.data = None

            def run(self):
                self.data = self._http.post(
                    "/memory/search", json={"query": self._query, "top_k": 20}
                )

        self._list_thread = SearchThread(self._http, query)
        self._list_thread.finished.connect(self._on_search_done)
        self._set_loading(True)
        self._list_thread.start()

    def _on_search_done(self) -> None:
        self._set_loading(False)
        t = self._list_thread
        if t is None or t.data is None:
            QMessageBox.warning(self, "失败", "搜索失败")
            return
        data = t.data
        if not isinstance(data, dict):
            return
        results = data.get("results", []) or []
        self._populate_memory_list(results, is_search=True)

    def _on_reset_search_clicked(self) -> None:
        self._search_edit.clear()
        self._refresh_list_async()

    # —— 记忆详情 ——

    def _on_memory_item_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        if not isinstance(data, dict):
            return
        key = data.get("key", "")
        if not key:
            # search 结果无 key，直接展示原始数据
            self._show_memory_detail(data, key="")
            return
        # 异步拉取详情
        self._current_detail_key = key
        self._fetch_detail_async(key)

    def _fetch_detail_async(self, key: str) -> None:
        if self._detail_thread is not None and self._detail_thread.isRunning():
            return

        class DetailThread(QThread):
            def __init__(self, http, key):
                super().__init__()
                self._http = http
                self._key = key
                self.data = None

            def run(self):
                self.data = self._http.get(f"/memory/{self._key}")

        self._detail_thread = DetailThread(self._http, key)
        self._detail_thread.finished.connect(self._on_detail_done)
        self._set_loading(True)
        self._detail_thread.start()

    def _on_detail_done(self) -> None:
        self._set_loading(False)
        t = self._detail_thread
        if t is None or t.data is None:
            self._memory_detail.setPlainText("(获取详情失败)")
            return
        self._show_memory_detail(t.data, key=self._current_detail_key)

    def _show_memory_detail(self, data: dict, key: str) -> None:
        text = self._render_memory_detail(data, key)
        self._memory_detail.setPlainText(text)
        # 仅当有 key 时启用删除按钮（search 结果不可直接删除）
        if key:
            self._delete_memory_btn.setEnabled(True)
        else:
            self._delete_memory_btn.setEnabled(False)

    @staticmethod
    def _render_memory_detail(data: dict, key: str) -> str:
        """友好渲染记忆详情（分字段展示，非裸 JSON）

        GET /memory/{key} 返回的 dict 已由后端解析（value JSON 合并到顶层），
        这里按「标题 / 主体内容 / 结构化字段 / 时间 / 其他」分块展示。
        """
        if not isinstance(data, dict):
            try:
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception:
                return str(data)

        # 已知元字段（单独展示，不放进主体）
        _META_KEYS = {
            "source", "updated_at", "created_at", "fact_type", "occurred_at",
            "mentioned_at", "access_count", "confidence",
            "consumption_contexts", "trigger_keywords",
        }
        # 主体内容字段（按优先级展示）
        _CONTENT_KEYS = ("name", "title", "summary", "description", "content")

        lines: list[str] = []
        # 标题
        title = key or data.get("name") or data.get("title") or "(无标题)"
        badges: list[str] = []
        if data.get("fact_type"):
            badges.append(str(data["fact_type"]))
        if data.get("source"):
            badges.append(str(data["source"]))
        header = str(title)
        if badges:
            header += "  [" + "/".join(badges) + "]"
        lines.append(header)
        lines.append("━" * 44)

        # 主体内容
        for k in _CONTENT_KEYS:
            v = data.get(k)
            if v and isinstance(v, str) and v.strip():
                lines.append(f"【{k}】")
                lines.append(str(v))
                lines.append("")

        # 结构化字段
        if data.get("occurred_at"):
            lines.append(f"发生时间: {data['occurred_at']}")
        if data.get("mentioned_at"):
            lines.append(f"最近引用: {data['mentioned_at']}")
        if data.get("access_count") is not None:
            lines.append(f"访问次数: {data['access_count']}")
        if data.get("confidence") is not None:
            lines.append(f"置信度: {data['confidence']}")

        # 列表字段（JSON 字符串或 list）
        for k in ("consumption_contexts", "trigger_keywords"):
            v = data.get(k)
            if v:
                try:
                    if isinstance(v, str):
                        v = json.loads(v)
                    if isinstance(v, list):
                        lines.append(f"{k}: {', '.join(str(x) for x in v)}")
                except (ValueError, TypeError):
                    lines.append(f"{k}: {v}")

        # 时间
        if data.get("created_at"):
            lines.append(f"创建: {data['created_at']}")
        if data.get("updated_at"):
            lines.append(f"更新: {data['updated_at']}")

        # 其他字段（未识别的，避免丢失信息，折叠到末尾）
        other = {
            k: v for k, v in data.items()
            if k not in _META_KEYS and k not in _CONTENT_KEYS and v is not None
        }
        if other:
            lines.append("")
            lines.append("━" * 44)
            lines.append("【其他字段】")
            lines.append(json.dumps(other, ensure_ascii=False, indent=2))

        return "\n".join(lines)

    def _on_delete_memory_clicked(self) -> None:
        key = self._current_detail_key
        if not key:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认删除记忆 '{key}'？此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._run_action_async(
            action="删除记忆",
            path=f"/memory/{key}",
            method="delete",
            refresh="list",
        )

    # —— 时间线 ——

    def _refresh_timeline_async(self) -> None:
        if self._timeline_thread is not None and self._timeline_thread.isRunning():
            return

        # 收集筛选参数
        params: dict = {"limit": self._limit_spin.value()}
        source = self._source_combo.currentData()
        if source:
            params["source"] = source
        if self._time_range_cb.isChecked():
            params["since"] = self._since_edit.dateTime().toSecsSinceEpoch()
            params["until"] = self._until_edit.dateTime().toSecsSinceEpoch()

        class TimelineThread(QThread):
            def __init__(self, http, params):
                super().__init__()
                self._http = http
                self._params = params
                self.data = None

            def run(self):
                self.data = self._http.get("/memory/timeline", params=self._params)

        self._timeline_thread = TimelineThread(self._http, params)
        self._timeline_thread.finished.connect(self._on_timeline_done)
        self._set_loading(True)
        self._timeline_thread.start()

    def _on_timeline_done(self) -> None:
        self._set_loading(False)
        t = self._timeline_thread
        if t is None or t.data is None:
            return
        data = t.data
        if not isinstance(data, dict):
            return
        self._timeline_messages = data.get("messages", []) or []
        self._populate_timeline(self._timeline_messages)

    def _populate_timeline(self, messages: list[dict]) -> None:
        self._timeline_list.clear()
        if not messages:
            empty = QListWidgetItem("(无时间线消息)")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._timeline_list.addItem(empty)
            return
        for msg in messages:
            timestamp = msg.get("timestamp", "")
            source = msg.get("source", "")
            content = msg.get("content", "")
            preview = content[:60].replace("\n", " ")
            if len(content) > 60:
                preview += "..."
            text = f"[{source}] {preview}"
            if timestamp:
                text += f"  @ {timestamp}"
            lw_item = QListWidgetItem(text)
            lw_item.setData(Qt.UserRole, msg)
            self._timeline_list.addItem(lw_item)

    def _on_timeline_item_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        if not isinstance(data, dict):
            return
        text = self._render_timeline_detail(data)
        self._timeline_detail.setPlainText(text)

    @staticmethod
    def _render_timeline_detail(data: dict) -> str:
        """友好渲染时间线条目（message），解析 content JSON 分字段展示"""
        lines: list[str] = []
        # 头部：[source/role] @ timestamp
        source = data.get("source", "")
        role = data.get("role", "")
        ts = data.get("timestamp") or data.get("created_at") or ""
        header_parts = []
        if source:
            header_parts.append(str(source))
        if role and role != source:
            header_parts.append(str(role))
        header = "[" + "/".join(header_parts) + "]" if header_parts else ""
        if ts:
            header += f"  @ {ts}" if header else f"@ {ts}"
        if header:
            lines.append(header)
            lines.append("━" * 44)

        # content 可能是纯文本，也可能是 JSON 字符串（tool call / fact）
        content = data.get("content", "")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    # tool call 结构
                    if "tool" in parsed:
                        lines.append(f"工具: {parsed.get('tool')}")
                        if parsed.get("args_summary"):
                            lines.append(f"参数: {parsed['args_summary']}")
                        if parsed.get("result_summary"):
                            lines.append(f"结果: {parsed['result_summary']}")
                        if parsed.get("duration_ms") is not None:
                            lines.append(f"耗时: {parsed['duration_ms']}ms")
                    else:
                        # fact 结构：按 name/title/summary/description/content 展示
                        for k in ("name", "title", "summary", "description", "content", "type"):
                            v = parsed.get(k)
                            if v and isinstance(v, str) and v.strip():
                                lines.append(f"【{k}】{v}")
                        # 其他字段
                        other = {k: v for k, v in parsed.items()
                                 if k not in ("name", "title", "summary", "description", "content", "type")
                                 and v is not None}
                        if other:
                            lines.append("")
                            lines.append("【其他】")
                            lines.append(json.dumps(other, ensure_ascii=False, indent=2))
                    # content 已渲染，跳过下方纯文本展示
                    content = None
            except (ValueError, TypeError):
                pass

        if content:
            lines.append(str(content))

        # metadata
        meta = data.get("metadata")
        if meta and isinstance(meta, str) and meta.strip() not in ("", "{}"):
            lines.append("")
            lines.append(f"metadata: {meta}")

        return "\n".join(lines) if lines else json.dumps(data, ensure_ascii=False, indent=2)

    # —— 维护操作 ——

    def _on_maintain_clicked(self) -> None:
        ret = QMessageBox.question(
            self, "确认",
            "确认触发记忆维护？\n\n将扫描 stale 记忆并调用 LLM 验证，可能消耗 token。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # POST /memory/maintain 需 MaintainRequest body（force 默认 False）
        self._run_action_async(
            action="触发维护",
            path="/memory/maintain",
            json_body={"force": False},
            method="post",
            refresh="overview",
        )

    def _on_reindex_clicked(self) -> None:
        ret = QMessageBox.question(
            self, "确认",
            "确认重建语义索引？\n\n将重建向量索引和 BM25 统计，可能耗时较长。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._run_action_async(
            action="重建索引",
            path="/memory/reindex",
            method="post",
            refresh="overview",
        )

    def _on_compress_clicked(self) -> None:
        ret = QMessageBox.question(
            self, "确认",
            "确认压缩记忆？\n\n将旧消息压缩为摘要。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # POST /memory/compress?force=false（force 走 query 参数）
        self._run_action_async(
            action="压缩记忆",
            path="/memory/compress?force=false",
            method="post",
            refresh="overview",
        )

    def _on_cleanup_clicked(self) -> None:
        ret = QMessageBox.question(
            self, "确认",
            "确认清理孤立记忆？\n\n将删除超过 30 天的已压缩消息和旧摘要，不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._run_action_async(
            action="清理孤立",
            path="/memory/cleanup/orphaned",
            method="post",
            refresh="overview",
        )

    def _run_action_async(
        self,
        action: str,
        path: str,
        json_body: dict | None = None,
        method: str = "post",
        refresh: str = "overview",
    ) -> None:
        """通用异步操作（POST/DELETE），完成后弹消息框 + 自动刷新"""
        if self._action_thread is not None and self._action_thread.isRunning():
            QMessageBox.warning(self, "忙", "正在执行其他操作，请稍候")
            return

        class ActionThread(QThread):
            def __init__(self, http, path, json_body, method):
                super().__init__()
                self._http = http
                self._path = path
                self._json_body = json_body
                self._method = method
                self.data = None

            def run(self):
                fn = getattr(self._http, self._method)
                self.data = fn(self._path, json=self._json_body)

        self._action_thread = ActionThread(self._http, path, json_body, method)
        # 用闭包捕获 refresh 参数
        self._action_thread.finished.connect(
            lambda: self._on_action_done(action, refresh)
        )
        self._set_loading(True)
        self._action_thread.start()

    def _on_action_done(self, action: str, refresh: str) -> None:
        self._set_loading(False)
        t = self._action_thread
        if t is None:
            return
        if t.data is None:
            QMessageBox.warning(self, "失败", f"{action}操作失败，请检查后端日志")
            return
        # 展示结果
        try:
            result_text = json.dumps(t.data, ensure_ascii=False, indent=2)
        except Exception:
            result_text = str(t.data)
        if len(result_text) > 1500:
            result_text = result_text[:1500] + "\n...(已截断)"
        QMessageBox.information(self, "完成", f"{action}完成：\n\n{result_text}")
        # 自动刷新
        if refresh == "list":
            self._refresh_list_async()
            self._memory_detail.setPlainText("")
            self._current_detail_key = None
            self._delete_memory_btn.setEnabled(False)
        else:
            self._refresh_overview_async()
