"""记忆面板共享组件 — TableWithDetail / FilterBar / ActionButtonsBar

Ticket 04：3 个复合组件，供记忆库 / 时间线 / 检索追踪 / 证据账本 / 写入记忆页复用。

设计原则：
- 复用 lib/ui/theme.py 的 set_kind / set_text_role 统一视觉风格
- 复用 lib/ui/tokens.py 的 SUCCESS_TEXT / WARNING_TEXT / DANGER_TEXT / ACCENT 等
- 危险按钮一律走 client/widgets/confirm_dialog.py 的 ConfirmDialog.confirm
  （禁止使用 Qt 内置 question dialog，必须用 ConfirmDialog 三级风险色条）
- 业务术语中文化（过时/保留/归档/待处理/自动/手动），技术术语保留英文无括注
  （fact_type 字段名 / source 值 tool/agent/user / consumption_contexts / trigger_keywords / score）
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.widgets.confirm_dialog import ConfirmDialog
from client.widgets.markdown_viewer import MarkdownViewer
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

# —— 业务术语中文化对照（spec「中文化术语对照表」节）——
# stale → 过时；keep → 保留；archive → 归档；pending → 待处理；auto → 自动；manual → 手动
# fact_type 字段名 + 5 类值（user/feedback/project/reference/experience）保留英文
# source 值（tool/agent/user）保留英文
FACT_TYPES = ["user", "feedback", "project", "reference", "experience"]
SOURCES = ["tool", "agent", "user"]


# ============================================================
# TableWithDetail —— 表格 + 下方详情区
# ============================================================

class TableWithDetail(QWidget):
    """表格 + 下方 Markdown 详情区复合组件

    - 上方 QTableWidget：列点击排序，行点击切换详情
    - 下方 MarkdownViewer：渲染分字段卡片
    - 选中行变化时发 row_activated 信号，携带整行原始数据
    """

    row_activated = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list[dict] = []
        self._rows: list[dict] = []          # 原始行数据，与表格行序对齐
        self._detail_renderer: Callable[[dict], str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SPACE_SM)

        # —— 上方表格 ——
        self._table = QTableWidget(0, 0)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table, 1)

        # —— 下方详情区 ——
        self._detail_label = QLabel("详情")
        set_text_role(self._detail_label, "heading")
        layout.addWidget(self._detail_label)

        self._detail = MarkdownViewer()
        self._detail.set_readonly(True)
        self._detail.setMinimumHeight(160)
        self._detail.setPlaceholderText("点击表格行查看详情")
        layout.addWidget(self._detail, 1)

    # —— 配置 ——

    def set_columns(self, columns: list[dict]) -> None:
        """配置列定义。

        Args:
            columns: 每列 ``{"key": str, "label": str, "sortable": bool=True, "width": int|None}``
                - key：取行 dict 哪个字段
                - label：列头显示文字（中文）
                - sortable：是否允许点击列头排序（默认 True）
                - width：列宽（None = 自适应）
        """
        self._columns = columns
        self._table.setColumnCount(len(columns))
        self._table.setHorizontalHeaderLabels([c["label"] for c in columns])
        hdr = self._table.horizontalHeader()
        for i, col in enumerate(columns):
            # setSectionsClickable 是 QHeaderView 的统一开关；
            # 按列级禁用排序需用 setSortIndicatorShown + sectionClickableHint，
            # 简化处理：所有列都允许排序，业务侧在 _on_header_clicked 判断
            hdr.setSectionsClickable(True)
            w = col.get("width")
            if w is not None:
                self._table.setColumnWidth(i, w)
        hdr.setStretchLastSection(True)

    def set_detail_renderer(self, renderer: Callable[[dict], str] | None) -> None:
        """设置详情渲染器。renderer(row_dict) -> markdown 字符串。None 用默认渲染。"""
        self._detail_renderer = renderer

    def set_detail_markdown(self, md: str) -> None:
        """直接覆盖详情区内容（用于异步拉取详情后渲染）

        优先于 row_activated 的同步默认渲染。传空串清空。
        """
        self._detail.set_markdown(md or "")

    # —— 数据 ——

    def set_rows(self, rows: list[dict]) -> None:
        """填充行数据。每行是 dict，按 columns 的 key 取值显示"""
        self._rows = list(rows)
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            for c, col in enumerate(self._columns):
                key = col["key"]
                val = row.get(key, "")
                item = QTableWidgetItem(str(val) if val is not None else "")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # 第一列稍亮以便扫视
                if c == 0:
                    item.setForeground(Qt.GlobalColor.white)
                self._table.setItem(r, c, item)
        self._table.setSortingEnabled(True)
        self._detail.set_markdown("")
        if self._rows:
            self._table.selectRow(0)

    def append_row(self, row: dict) -> None:
        """追加单行（用于增量刷新高亮场景）"""
        self._rows.append(row)
        r = len(self._rows) - 1
        self._table.insertRow(r)
        for c, col in enumerate(self._columns):
            val = row.get(col["key"], "")
            item = QTableWidgetItem(str(val) if val is not None else "")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if c == 0:
                item.setForeground(Qt.GlobalColor.white)
            self._table.setItem(r, c, item)

    def clear(self) -> None:
        """清空表格和详情"""
        self._rows = []
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._detail.set_markdown("")

    def select_row_by_field(self, field: str, value: Any) -> bool:
        """按某列字段值选中行，找到返回 True 并发 row_activated 信号"""
        for r, row in enumerate(self._rows):
            if row.get(field) == value:
                self._table.selectRow(r)
                return True
        return False

    def current_row_data(self) -> dict | None:
        """当前选中行的原始数据"""
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            # sortingEnabled 时 visualRow 与数据 index 不对齐，
            # 用 item(row, 0).data 取回行号
            first_item = self._table.item(row, 0)
            if first_item is not None:
                # 我们没存 UserRole，退回用 visualRow → self._rows 索引
                # 但排序后 self._rows 顺序仍为原始顺序，无法靠 visualRow 还原
                # 用 workaround：扫描 self._rows 找第一列文本匹配
                first_text = first_item.text()
                for orig in self._rows:
                    if str(orig.get(self._columns[0]["key"], "")) == first_text:
                        return orig
        return None

    # —— 内部 ——

    def _on_header_clicked(self, section: int) -> None:
        """列头点击 → 切换排序方向（QTableWidget 默认行为 + 升降序切换）"""
        if not self._columns[section].get("sortable", True):
            return
        hdr = self._table.horizontalHeader()
        if hdr.sortIndicatorSection() == section:
            new_order = (
                Qt.SortOrder.AscendingOrder
                if hdr.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
                else Qt.SortOrder.DescendingOrder
            )
        else:
            new_order = Qt.SortOrder.AscendingOrder
        self._table.sortItems(section, new_order)
        hdr.setSortIndicator(section, new_order)

    def _on_selection_changed(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        row_idx = rows[0].row()
        # 通过第一列文本回查原始行（排序后 self._rows 顺序不可靠）
        first_item = self._table.item(row_idx, 0)
        if first_item is None or not self._columns:
            return
        first_text = first_item.text()
        first_key = self._columns[0]["key"]
        for orig in self._rows:
            if str(orig.get(first_key, "")) == first_text:
                self._render_detail(orig)
                self.row_activated.emit(orig)
                return

    def _render_detail(self, row: dict) -> None:
        md = self._detail_renderer(row) if self._detail_renderer is not None \
            else self._default_render(row)
        self._detail.set_markdown(md)

    @staticmethod
    def _default_render(row: dict) -> str:
        """默认详情渲染：键值列表"""
        if not row:
            return ""
        lines = ["| 字段 | 值 |", "|---|---|"]
        for k, v in row.items():
            val_str = str(v) if v is not None else ""
            if len(val_str) > 80:
                val_str = val_str[:77] + "..."
            # 转义 markdown 表格的 |
            val_str = val_str.replace("|", "\\|")
            lines.append(f"| {k} | {val_str} |")
        return "\n".join(lines)


# ============================================================
# FilterBar —— 筛选条
# ============================================================

class FilterBar(QWidget):
    """记忆库 / 时间线 / 检索追踪 / 证据账本 页通用筛选条

    字段（业务术语中文化，技术术语保留英文无括注）：
        - 类型（fact_type）下拉：全部 + 5 类值
        - 来源（source）下拉：全部 + tool/agent/user（技术术语保留无括注）
        - 过时 QCheckBox「仅看过时」
        - 时间范围 QCheckBox + QDateTimeEdit 起 / 止
        - 关键词 QLineEdit + 搜索按钮 + 重置按钮

    filters_changed = Signal(dict)
        dict 含 keys: fact_type / source / stale_only / time_enabled / time_from / time_to / keyword
    """

    filters_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._wire_signals()

    # —— UI ——

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SPACE_SM)

        # 类型（fact_type）
        layout.addWidget(self._label("类型"))
        self._fact_type_combo = QComboBox()
        self._fact_type_combo.addItem("全部", "")
        for t in FACT_TYPES:
            self._fact_type_combo.addItem(t, t)
        layout.addWidget(self._fact_type_combo)

        # 来源（source）
        layout.addWidget(self._label("来源"))
        self._source_combo = QComboBox()
        self._source_combo.addItem("全部", "")
        for s in SOURCES:
            self._source_combo.addItem(s, s)
        layout.addWidget(self._source_combo)

        # 过时 QCheckBox
        self._stale_check = QCheckBox("仅看过时")
        layout.addWidget(self._stale_check)

        # 时间范围
        self._time_check = QCheckBox("时间范围")
        layout.addWidget(self._time_check)
        self._time_from = QDateTimeEdit(QDateTime.currentDateTime().addDays(-7))
        self._time_from.setCalendarPopup(True)
        self._time_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._time_from.setEnabled(False)
        layout.addWidget(self._time_from)
        layout.addWidget(self._label("至"))
        self._time_to = QDateTimeEdit(QDateTime.currentDateTime())
        self._time_to.setCalendarPopup(True)
        self._time_to.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._time_to.setEnabled(False)
        layout.addWidget(self._time_to)

        # 关键词
        layout.addWidget(self._label("关键词"))
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("搜索关键词，回车应用")
        self._keyword_edit.setMinimumWidth(160)
        layout.addWidget(self._keyword_edit, 1)

        # 搜索 / 重置
        self._search_btn = QPushButton("搜索")
        set_kind(self._search_btn, "primary")
        layout.addWidget(self._search_btn)

        self._reset_btn = QPushButton("重置")
        set_kind(self._reset_btn, "ghost")
        layout.addWidget(self._reset_btn)

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        set_text_role(lbl, "secondary")
        return lbl

    def _wire_signals(self) -> None:
        self._fact_type_combo.currentIndexChanged.connect(self._emit_changed)
        self._source_combo.currentIndexChanged.connect(self._emit_changed)
        self._stale_check.toggled.connect(self._emit_changed)
        self._time_check.toggled.connect(self._on_time_toggled)
        self._time_from.dateTimeChanged.connect(self._emit_changed)
        self._time_to.dateTimeChanged.connect(self._emit_changed)
        self._keyword_edit.returnPressed.connect(self._emit_changed)
        self._search_btn.clicked.connect(self._emit_changed)
        self._reset_btn.clicked.connect(self.reset)

    def _on_time_toggled(self, on: bool) -> None:
        self._time_from.setEnabled(on)
        self._time_to.setEnabled(on)
        self._emit_changed()

    # —— 对外 API ——

    def filters(self) -> dict:
        """返回当前筛选条件"""
        return {
            "fact_type": self._fact_type_combo.currentData() or "",
            "source": self._source_combo.currentData() or "",
            "stale_only": self._stale_check.isChecked(),
            "time_enabled": self._time_check.isChecked(),
            "time_from": self._time_from.dateTime().toString(Qt.DateFormat.ISODate)
                          if self._time_check.isChecked() else "",
            "time_to": self._time_to.dateTime().toString(Qt.DateFormat.ISODate)
                        if self._time_check.isChecked() else "",
            "keyword": self._keyword_edit.text().strip(),
        }

    def set_filter(self, name: str, value: Any) -> None:
        """从外部设置某个筛选条件（跳转链接带筛选参数时用）

        name 可选：fact_type / source / stale_only / time_enabled / keyword

        不发 filters_changed 信号，调用方按需自行触发查询或调 _search_btn.click()。
        实现：blockSignals(True) 屏蔽 self.filters_changed 发射，子控件信号仍触发
        _emit_changed 但其 emit 被 self 阻塞。
        """
        was_blocked = self.signalsBlocked()
        self.blockSignals(True)
        try:
            if name == "fact_type":
                idx = self._fact_type_combo.findData(value or "")
                if idx >= 0:
                    self._fact_type_combo.setCurrentIndex(idx)
            elif name == "source":
                idx = self._source_combo.findData(value or "")
                if idx >= 0:
                    self._source_combo.setCurrentIndex(idx)
            elif name == "stale_only":
                self._stale_check.setChecked(bool(value))
            elif name == "time_enabled":
                self._time_check.setChecked(bool(value))
            elif name == "keyword":
                self._keyword_edit.setText(str(value or ""))
        finally:
            self.blockSignals(was_blocked)

    def reset(self) -> None:
        """重置所有筛选条件"""
        self._fact_type_combo.setCurrentIndex(0)
        self._source_combo.setCurrentIndex(0)
        self._stale_check.setChecked(False)
        self._time_check.setChecked(False)
        self._keyword_edit.clear()
        self._emit_changed()

    def _emit_changed(self) -> None:
        self.filters_changed.emit(self.filters())


# ============================================================
# ActionButtonsBar —— 操作按钮分级条
# ============================================================

class ActionButtonsBar(QWidget):
    """操作按钮分级条

    - 左侧：安全按钮（绿色 primary，复用 tokens.ACCENT）+ 中等按钮（黄色 WARNING 描边）
    - 右侧：「高级操作」折叠按钮（点击展开 QFrame 含危险按钮红色边框）

    危险按钮点击时弹 ConfirmDialog.confirm(parent, title, message, risk_level)，
    用户确认后才执行 callback（必须用 ConfirmDialog，禁止使用 Qt 内置 question dialog）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SPACE_SM)

        # 左侧：安全 / 中等按钮容器
        self._safe_layout = QHBoxLayout()
        self._safe_layout.setSpacing(tokens.SPACE_SM)
        layout.addLayout(self._safe_layout)

        layout.addStretch(1)

        # 右侧：「高级操作」折叠按钮 + 折叠面板
        self._advanced_toggle = QPushButton("高级操作")
        set_kind(self._advanced_toggle, "ghost")
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self._advanced_toggle)

        self._advanced_frame = QFrame()
        self._advanced_frame.setVisible(False)
        # 红色边框提示危险区域
        self._advanced_frame.setStyleSheet(
            f"QFrame {{ border: 1px solid {tokens.DANGER}; "
            f"border-radius: {tokens.RADIUS_SM}px; "
            f"padding: {tokens.SPACE_SM}px; }}"
        )
        adv_layout = QHBoxLayout(self._advanced_frame)
        adv_layout.setContentsMargins(tokens.SPACE_SM, tokens.SPACE_SM,
                                       tokens.SPACE_SM, tokens.SPACE_SM)
        adv_layout.setSpacing(tokens.SPACE_SM)
        self._danger_layout = adv_layout
        layout.addWidget(self._advanced_frame)

    def _on_advanced_toggled(self, on: bool) -> None:
        self._advanced_frame.setVisible(on)
        self._advanced_toggle.setText("收起高级操作" if on else "高级操作")

    # —— 对外 API ——

    def add_safe_action(self, text: str, callback: Callable[[], None]) -> QPushButton:
        """添加安全按钮（绿色 primary，复用 ACCENT）"""
        btn = QPushButton(text)
        set_kind(btn, "primary")
        btn.clicked.connect(callback)
        self._safe_layout.addWidget(btn)
        return btn

    def add_medium_action(self, text: str, callback: Callable[[], None]) -> QPushButton:
        """添加中等按钮（黄色 WARNING 描边）"""
        btn = QPushButton(text)
        # 中等按钮自定义 warning 样式（lib/ui/theme 未提供 warning kind）
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: transparent;
                border: 1px solid {tokens.WARNING};
                color: {tokens.WARNING_TEXT};
                padding: 5px 14px;
                min-height: {tokens.CTRL_HEIGHT_MD}px;
                border-radius: {tokens.RADIUS_SM}px;
            }}
            QPushButton:hover {{ background-color: {tokens.WARNING_WASH}; }}
            QPushButton:pressed {{
                background-color: {tokens.WARNING};
                color: {tokens.TEXT_ON_ACCENT};
            }}
            QPushButton:disabled {{
                background-color: {tokens.BG_PANEL};
                border-color: {tokens.BORDER};
                color: {tokens.TEXT_DISABLED};
            }}
            """
        )
        btn.clicked.connect(callback)
        self._safe_layout.addWidget(btn)
        return btn

    def add_danger_action(self,
                          text: str,
                          callback: Callable[[], None],
                          confirm_title: str,
                          confirm_message: str | Callable[[], str],
                          risk_level: str = "danger") -> QPushButton:
        """添加危险按钮（红色边框，折叠到「高级操作」区）

        点击时先弹 ConfirmDialog.confirm，用户确认后才执行 callback。
        risk_level: info / warning / danger（决定色条 + 按钮文字）

        confirm_message 可传 str 或 callable：
            - str：固定文案（用于不依赖当前选中态的全局危险操作，如清理孤立）
            - callable：每次点击时调 `confirm_message()` 拿最新文案
              （用于依赖当前行的操作，如删除选中记忆 — 文案含 key 名）
        """
        btn = QPushButton(text)
        set_kind(btn, "danger")

        def _on_clicked() -> None:
            msg = confirm_message() if callable(confirm_message) else confirm_message
            ok = ConfirmDialog.confirm(
                self, confirm_title, msg, risk_level
            )
            if ok:
                callback()

        btn.clicked.connect(_on_clicked)
        self._danger_layout.addWidget(btn)
        # 确保「高级操作」区可见性
        if not self._advanced_frame.isVisible():
            self._advanced_toggle.setChecked(True)
        return btn

    def add_danger_widget(self, widget: QWidget) -> None:
        """往「高级操作」折叠区追加自定义 widget（如 QSpinBox 选天数）

        widget 追加到 danger_layout 末尾。如需让 widget 出现在危险按钮前，
        调用方应先调本方法加完所有 widget，再调 add_danger_action 加按钮。
        """
        self._danger_layout.addWidget(widget)
        if not self._advanced_frame.isVisible():
            self._advanced_toggle.setChecked(True)

    def clear_actions(self) -> None:
        """清空所有按钮（切换页面时调用）"""
        def _clear(layout: QHBoxLayout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                if item is None:
                    continue
                w = item.widget()
                if w is not None:
                    w.deleteLater()
        _clear(self._safe_layout)
        _clear(self._danger_layout)
