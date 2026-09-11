"""Tools 面板 — 精筛后的工具启动器

复用 client.widgets.tool_runner 的 ScriptRunner / QueueManager / ToolDetailWidget /
CategoryPageWidget，增加 user_facing 过滤和"显示全部工具"切换。
"""


from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
)

from client.core.constants import TOOLS_MANIFEST_PATH
from client.core.panel_base import PanelBase, PanelMeta
from client.core.state import AppState
from client.widgets.tool_runner import (
    CategoryPageWidget,
    QueueManager,
    ToolDetailWidget,
    filter_categories,
    filter_tools_by_user_facing,
    get_category_map,
    load_manifest,
)
from lib.ui import icon, tokens
from lib.ui.theme import set_text_role


class ToolsPanel(PanelBase):
    """工具面板：按分类展示 user_facing=true 的工具，支持"显示全部"切换。"""

    PANEL_META = PanelMeta(
        id="tools",
        title="工具",
        icon="wrench",
        order=20,
        category="main",
        requires_backend=False,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = AppState.instance()
        self._show_all = self._state.load_show_all_tools()
        self._manifest = load_manifest()
        self._category_map = get_category_map(self._manifest)
        self._manifest_mtime = self._read_manifest_mtime()
        self._tool_widgets: dict[str, ToolDetailWidget] = {}
        self._category_pages: dict[str, CategoryPageWidget] = {}
        self._queue_manager = QueueManager()
        self._build_ui()
        self._load_tools()
        self._connect_queue_signals()

    @staticmethod
    def _read_manifest_mtime() -> float:
        """读取 tools_manifest.json 的修改时间，用于检测是否需要重建"""
        try:
            return TOOLS_MANIFEST_PATH.stat().st_mtime
        except OSError:
            return 0.0

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部栏：标题 + 显示全部工具
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet(f"QFrame {{ background-color: {tokens.BG_PANEL}; border-bottom: 1px solid {tokens.BORDER}; }}")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 0, 16, 0)
        top_layout.setSpacing(12)

        title = QLabel("工具箱")
        set_text_role(title, "title")
        top_layout.addWidget(title)

        top_layout.addStretch()

        self._show_all_cb = QCheckBox("显示全部工具")
        self._show_all_cb.setChecked(self._show_all)
        self._show_all_cb.setStyleSheet(f"QCheckBox {{ color: {tokens.TEXT_SECONDARY}; font-size: 12px; }}")
        self._show_all_cb.toggled.connect(self._on_show_all_toggled)
        top_layout.addWidget(self._show_all_cb)

        outer.addWidget(top_bar)

        # 中部：左侧分类 + 右侧内容
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        main_layout.addWidget(self._build_sidebar())

        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, 1)

        outer.addLayout(main_layout, 1)

        # 底部队列栏
        self._build_queue_bar(outer)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        # 弹性：min/max 替代 setFixedWidth（与 chat.py D4 弹性布局策略一致）
        sidebar.setMinimumWidth(180)
        sidebar.setMaximumWidth(240)
        sidebar.setStyleSheet(f"QFrame {{ background-color: {tokens.BG_BASE}; }}")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._category_list = QListWidget()
        self._category_list.setStyleSheet(
            f"QListWidget {{ background-color: {tokens.BG_BASE}; color: {tokens.TEXT_SECONDARY}; border: none; "
            f"font-size: 13px; outline: 0; }}"
            f"QListWidget::item {{ padding: 10px 14px; border-bottom: 1px solid {tokens.BORDER}; }}"
            f"QListWidget::item:selected {{ background-color: {tokens.ACCENT_WASH}; border-left: 3px solid {tokens.ACCENT}; }}"
            f"QListWidget::item:hover {{ background-color: {tokens.BG_HOVER}; }}"
        )
        self._category_list.currentRowChanged.connect(self._on_category_changed)
        layout.addWidget(self._category_list, 1)

        self._status_label = QLabel()
        set_text_role(self._status_label, "caption")
        self._status_label.setStyleSheet(
            f"padding: 10px 14px; background-color: {tokens.BG_BASE};"
        )
        layout.addWidget(self._status_label)

        return sidebar

    def _build_queue_bar(self, parent_layout: QVBoxLayout) -> None:
        bar = QFrame()
        bar.setFixedHeight(46)
        bar.setStyleSheet(f"QFrame {{ background-color: {tokens.BG_PANEL}; border-top: 1px solid {tokens.BORDER}; }}")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(8)

        queue_label = QLabel("队列:")
        set_text_role(queue_label, "tertiary")
        layout.addWidget(queue_label)

        self._queue_status_label = QLabel("0 个任务")
        self._queue_status_label.setMinimumWidth(200)
        set_text_role(self._queue_status_label, "tertiary")
        layout.addWidget(self._queue_status_label)

        layout.addStretch()

        self._queue_run_btn = QPushButton("执行队列")
        self._queue_run_btn.setEnabled(False)
        self._queue_run_btn.clicked.connect(self._on_run_queue)
        layout.addWidget(self._queue_run_btn)

        self._queue_stop_btn = QPushButton("停止")
        self._queue_stop_btn.setEnabled(False)
        self._queue_stop_btn.clicked.connect(self._on_stop_queue)
        layout.addWidget(self._queue_stop_btn)

        self._queue_clear_btn = QPushButton("清空")
        self._queue_clear_btn.setEnabled(False)
        self._queue_clear_btn.clicked.connect(self._on_clear_queue)
        layout.addWidget(self._queue_clear_btn)

        parent_layout.addWidget(bar)

    # —— 工具加载 ——

    def _load_tools(self) -> None:
        """加载/重新加载工具列表（按当前 show_all 状态过滤）"""
        # 重建前记住当前分类（"显示全部工具"切换不应重置选中）
        current_item = self._category_list.currentItem()
        prev_cat_id = current_item.data(Qt.ItemDataRole.UserRole) if current_item else None
        # 清空旧内容
        self._category_list.clear()
        for page in self._category_pages.values():
            self._stack.removeWidget(page)
            page.deleteLater()
        self._category_pages.clear()
        self._tool_widgets.clear()

        tools = filter_tools_by_user_facing(
            self._manifest.get("tools", []), show_all=self._show_all
        )
        cat_tools: dict[str, list] = {}
        for tool in tools:
            cat_id = tool.get("category", "other")
            cat_tools.setdefault(cat_id, []).append(tool)

        visible_cats = filter_categories(self._manifest, show_all=self._show_all)
        target_row = 0
        for row, cat in enumerate(visible_cats):
            cat_id = cat["id"]
            cat_name = cat["name"]
            page = CategoryPageWidget(
                cat_tools.get(cat_id, []), cat_name, self._on_enqueue_task
            )
            self._category_pages[cat_id] = page
            self._stack.addWidget(page)
            for detail in page.detail_widgets:
                self._tool_widgets[detail.tool_id] = detail

            item = QListWidgetItem(cat_name)
            cat_icon = cat.get("icon", "folder")
            try:
                # manifest icon 存 SVG 图标名（B5 迁移），未命中回退 emoji/文本前缀
                item.setIcon(icon(cat_icon))
            except KeyError:
                item.setText(f"{cat_icon}  {cat_name}")
            item.setSizeHint(QSize(180, 38))
            item.setData(Qt.ItemDataRole.UserRole, cat_id)
            self._category_list.addItem(item)
            if cat_id == prev_cat_id:
                target_row = row

        # 更新状态
        total = len(self._manifest.get("tools", []))
        self._status_label.setText(f"显示 {len(tools)}/{total} 个工具")

        if self._category_list.count() > 0:
            self._category_list.setCurrentRow(target_row)

    # —— 面板钩子 ——

    def on_show(self) -> None:
        """切换到本面板时：只在 manifest 文件被修改时才重建（避免每次切换都卡）

        旧实现每次切换都 load_manifest() + 销毁重建 29 个 ToolDetailWidget，
        每个 widget 含表单/按钮/QTextEdit，主线程同步创建数百个控件导致明显卡顿。
        现在用 mtime 检测：文件没变就不重建。
        """
        current_mtime = self._read_manifest_mtime()
        if current_mtime != self._manifest_mtime:
            self._manifest = load_manifest()
            self._category_map = get_category_map(self._manifest)
            self._load_tools()
            self._manifest_mtime = current_mtime

    def on_hide(self) -> None:
        pass

    def on_refresh(self) -> None:
        """用户主动点刷新：强制重新加载"""
        self._manifest = load_manifest()
        self._category_map = get_category_map(self._manifest)
        self._manifest_mtime = self._read_manifest_mtime()
        self._load_tools()

    def on_backend_status_change(self, online: bool) -> None:
        pass

    # —— 事件处理 ——

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._show_all = checked
        self._state.save_show_all_tools(checked)
        self._load_tools()

    def _on_category_changed(self, row: int) -> None:
        visible_cats = filter_categories(self._manifest, show_all=self._show_all)
        if 0 <= row < len(visible_cats):
            cat_id = visible_cats[row]["id"]
            page = self._category_pages.get(cat_id)
            if page:
                self._stack.setCurrentWidget(page)

    def _on_enqueue_task(self, tool_id: str, tool_name: str, command: list[str]) -> None:
        self._queue_manager.add_task(tool_id, tool_name, command)

    # —— 队列信号 ——

    def _connect_queue_signals(self) -> None:
        self._queue_manager.task_started.connect(self._on_queue_task_started)
        self._queue_manager.task_output.connect(self._on_queue_task_output)
        self._queue_manager.task_finished.connect(self._on_queue_task_finished)
        self._queue_manager.queue_changed.connect(self._on_queue_changed)
        self._queue_manager.queue_finished.connect(self._on_queue_finished)
        self._on_queue_changed()

    def _switch_to_tool(self, tool_id: str) -> None:
        visible_cats = filter_categories(self._manifest, show_all=self._show_all)
        for cat_idx, cat in enumerate(visible_cats):
            cat_id = cat["id"]
            page = self._category_pages.get(cat_id)
            if not page:
                continue
            tabs = page.get_tabs()
            if tabs:
                for i in range(tabs.count()):
                    tab_page = tabs.widget(i)
                    if tab_page is None:
                        continue
                    # 分类页用 QScrollArea 包一层：滚动区域内才是真实详情页
                    detail = tab_page.widget() if isinstance(tab_page, QScrollArea) else tab_page
                    if isinstance(detail, ToolDetailWidget) and detail.tool_id == tool_id:
                        self._category_list.setCurrentRow(cat_idx)
                        tabs.setCurrentIndex(i)
                        return

    def _on_queue_task_started(self, tool_id: str) -> None:
        widget = self._tool_widgets.get(tool_id)
        if widget:
            self._switch_to_tool(tool_id)
            widget.on_queue_started(widget._build_command())

    def _on_queue_task_output(self, tool_id: str, line: str) -> None:
        widget = self._tool_widgets.get(tool_id)
        if widget:
            widget.on_queue_output(line)

    def _on_queue_task_finished(self, tool_id: str, code: int, msg: str) -> None:
        widget = self._tool_widgets.get(tool_id)
        if widget:
            widget.on_queue_finished(code, msg)

    def _on_queue_changed(self) -> None:
        status = self._queue_manager.get_status()
        if status["running"] and status["current"]:
            self._queue_status_label.setText(
                f"执行中: {status['current'][1]} | 待执行: {status['pending']}"
            )
        else:
            self._queue_status_label.setText(f"{status['total']} 个任务")
        self._queue_run_btn.setEnabled(not status["running"] and status["total"] > 0)
        self._queue_stop_btn.setEnabled(status["running"])
        self._queue_clear_btn.setEnabled(not status["running"] and status["total"] > 0)

    def _on_queue_finished(self) -> None:
        self._on_queue_changed()

    def _on_run_queue(self) -> None:
        self._queue_manager.start_queue()

    def _on_stop_queue(self) -> None:
        self._queue_manager.stop_queue()

    def _on_clear_queue(self) -> None:
        self._queue_manager.clear()
