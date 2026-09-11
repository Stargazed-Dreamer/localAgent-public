"""入站管理面板 — 入站网关的 client 侧（四页签）

1:1 对应 temp/sdd/inbound-gateway/inbound-gateway-mockup.html：
  页签1 Key 分发   — key 列表（启用/连接名/API Key/project/模型映射/最后使用/今日调用）
                     + 新建·编辑·禁用·删除 + 掩码点击切换明文 + 复制 + 接入片段
  页签2 调用日志   — 过滤（连接/模型/状态/日期/仅失败）+ 表格 + 点击行展开详情
  页签3 模型映射   — 按连接编辑别名表（harness 请求名 → 池内目标 + 按别名可选兜底 Tier 范围）
  页签4 Token 统计 — 概览卡 ×5 + 按连接 / 按转发目标聚合表（只读，数据源 /inbound/stats）

数据加载全部走 QThread（HttpClient 同步调用），不阻塞 UI。
统计数字全部来自后端 /inbound/stats（SQLite），面板不造假数据（spec Anti-Cheat）。
"""

from __future__ import annotations

import re
import time
from typing import Any

from PySide6.QtCore import QDate, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_text_role

_MASK_PREFIX = "sk-la-••••••••••••"


def _mask(secret: str) -> str:
    if not secret:
        return "—"
    return _MASK_PREFIX + secret[-3:]


def _tier_text(rng: list[int] | None) -> str:
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return "—"
    lo, hi = rng[0], rng[1]
    if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= 5 and 1 <= hi <= 5):
        return "—"
    return f"Tier {lo}" if lo == hi else f"Tier {lo}~{hi}"


def _tier_item(rng: list[int] | None) -> QTableWidgetItem:
    """兜底 Tier 列单元格（可手改为 "Tier 3" / "Tier 2~4" / "—"，保存时解析文本）。"""
    return QTableWidgetItem(_tier_text(rng))


def _parse_tier_text(text: str) -> list[int] | None:
    m = re.search(r"[Tt]ier\s*([1-5])(?:\s*[~～\-]\s*([1-5]))?", text or "")
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return [min(lo, hi), max(lo, hi)]


class _Fetch(QThread):
    """通用取数线程：一次 HTTP 调用，结果经信号回主线程。"""

    dataReady = Signal(object)

    def __init__(self, http: HttpClient, method: str, path: str,
                 params: dict | None = None, body: dict | None = None):
        super().__init__()
        self._http = http
        self._method = method
        self._path = path
        self._params = params
        self._body = body

    def run(self) -> None:
        m = self._method.lower()
        if m == "get":
            result = self._http.get(self._path, params=self._params)
        elif m == "post":
            result = self._http.post(self._path, json=self._body)
        elif m == "patch":
            result = self._http.patch(self._path, json=self._body)
        elif m == "delete":
            result = self._http.delete(self._path)
        else:
            result = None
        self.dataReady.emit(result)


def _dot_item(text: str, color: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    return item


def _dot_label(text: str, role: str) -> QLabel:
    """状态圆点用 QLabel 承载：行选中时主题 selection-color 不会把它染白（反馈①）。"""
    lbl = QLabel(text)
    set_text_role(lbl, role)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _readonly(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if text:
        item.setToolTip(text)
    return item


_COL_MIN_W = 60
_COL_MAX_W = 320


def _autosize_columns(t: QTableWidget, stretch_col: int) -> None:
    """加载数据后一次性自适应列宽（用户表格宽度方案）：

    resizeColumnsToContents + 60–320px 上下限，指定列 Stretch 吃掉剩余宽度，
    其余列 Interactive（用户之后仍可手动拖）。省略内容由 _readonly 的 tooltip 兜底。
    """
    t.resizeColumnsToContents()
    header = t.horizontalHeader()
    for c in range(t.columnCount()):
        if c == stretch_col:
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(c, max(_COL_MIN_W, min(_COL_MAX_W, t.columnWidth(c))))


class _AddMappingDialog(QDialog):
    """添加映射弹窗（反馈⑤ + 二轮兜底 Tier）：别名 + 目标下拉 + 兜底 Tier，提交校验。"""

    def __init__(self, parent, pool_models: list[str], existing_aliases: list[str]):
        super().__init__(parent)
        self.setWindowTitle("添加映射")
        self.setMinimumWidth(440)
        self._existing = set(existing_aliases)
        v = QVBoxLayout(self)

        tip1 = QLabel("别名：harness 请求时使用的模型名")
        set_text_role(tip1, "tertiary")
        v.addWidget(tip1)
        self._alias_edit = QLineEdit()
        self._alias_edit.setPlaceholderText("如 cline-chat")
        v.addWidget(self._alias_edit)

        tip2 = QLabel("目标：池内实际模型名，可从下拉选择，也可手输自定义")
        set_text_role(tip2, "tertiary")
        v.addWidget(tip2)
        self._target_combo = QComboBox()
        self._target_combo.setEditable(True)
        self._target_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._target_combo.addItems(pool_models)
        if not pool_models:
            le = self._target_combo.lineEdit()
            if le is not None:
                le.setPlaceholderText("（池未初始化，可直接手输模型名）")
        v.addWidget(self._target_combo)

        tip3 = QLabel("兜底 Tier 范围：目标模型不可用时，池在指定 Tier 范围内挑可用模型兜底；"
                      "不勾选 = 目标不可用直接报错（默认）")
        set_text_role(tip3, "tertiary")
        tip3.setWordWrap(True)
        v.addWidget(tip3)
        tier_row = QHBoxLayout()
        self._tier_ck = QCheckBox("启用兜底")
        self._tier_lo = QComboBox()
        self._tier_hi = QComboBox()
        tier_items = ["Tier 1（最轻）", "Tier 2", "Tier 3", "Tier 4", "Tier 5（最强）"]
        self._tier_lo.addItems(tier_items)
        self._tier_hi.addItems(tier_items)
        self._tier_hi.setCurrentIndex(4)
        for c in (self._tier_lo, self._tier_hi):
            c.setEnabled(False)
        self._tier_ck.toggled.connect(self._on_tier_ck_toggled)
        tier_row.addWidget(self._tier_ck)
        for w in (QLabel("从"), self._tier_lo, QLabel("到"), self._tier_hi):
            tier_row.addWidget(w)
        tier_row.addStretch()
        v.addLayout(tier_row)

        self._err_label = QLabel("")
        set_text_role(self._err_label, "danger")
        self._err_label.setVisible(False)
        v.addWidget(self._err_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("添加")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("取消")
        btns.accepted.connect(self._validate_and_accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _validate_and_accept(self) -> None:
        alias = self._alias_edit.text().strip()
        target = self._target_combo.currentText().strip()
        if not alias or not target:
            self._show_error("别名和目标模型都不能为空")
            return
        if alias in self._existing:
            self._show_error(f"别名「{alias}」已存在")
            return
        self.accept()

    def _show_error(self, msg: str) -> None:
        self._err_label.setText(msg)
        self._err_label.setVisible(True)

    def _on_tier_ck_toggled(self, checked: bool) -> None:
        self._tier_lo.setEnabled(checked)
        self._tier_hi.setEnabled(checked)

    def result_values(self) -> tuple[str, str, list[int] | None]:
        tier: list[int] | None = None
        if self._tier_ck.isChecked():
            lo = self._tier_lo.currentIndex() + 1
            hi = self._tier_hi.currentIndex() + 1
            tier = [min(lo, hi), max(lo, hi)]
        return self._alias_edit.text().strip(), self._target_combo.currentText().strip(), tier


class _KeyDialog(QDialog):
    """新建/编辑 Key 合并弹窗（反馈 1a）：连接名 + project 一表单完成，替代两次 QInputDialog。"""

    def __init__(self, parent, title: str, name: str = "", project: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        v = QVBoxLayout(self)

        form = QFormLayout()
        self._name_edit = QLineEdit(name)
        self._name_edit.setPlaceholderText("如 cline-work")
        self._project_edit = QLineEdit(project)
        self._project_edit.setPlaceholderText("用于区分连接的标签，如 cline")
        form.addRow("连接名称", self._name_edit)
        form.addRow("project 标签", self._project_edit)
        v.addLayout(form)

        tip = QLabel("project 标签透传给 LLM 池，用于该连接的 per-project 统计。")
        set_text_role(tip, "tertiary")
        tip.setWordWrap(True)
        v.addWidget(tip)

        self._err_label = QLabel("")
        set_text_role(self._err_label, "danger")
        self._err_label.setVisible(False)
        v.addWidget(self._err_label)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("保存")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("取消")
        btns.accepted.connect(self._validate_and_accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _validate_and_accept(self) -> None:
        if not self._name_edit.text().strip() or not self._project_edit.text().strip():
            self._err_label.setText("连接名称和 project 标签都不能为空")
            self._err_label.setVisible(True)
            return
        self.accept()

    def result_values(self) -> tuple[str, str]:
        return self._name_edit.text().strip(), self._project_edit.text().strip()


class InboundPanel(PanelBase):
    """入站管理面板。"""

    PANEL_META = PanelMeta(
        id="inbound",
        title="入站管理",
        icon="key",
        order=52,
        category="advanced",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._keys: list[dict] = []
        self._selected_key: dict | None = None
        self._pool_models: list[str] = []
        self._snippet_key_id: str | None = None
        self._fetches: list[_Fetch] = []          # 防 GC
        self._build_ui()
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(5000)
        self._auto_timer.timeout.connect(self._on_auto_refresh)

    # ================= UI 构建 =================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        self._tabs.addTab(self._build_keys_tab(), "Key 分发")
        self._tabs.addTab(self._build_calls_tab(), "调用日志")
        self._tabs.addTab(self._build_mapping_tab(), "模型映射")
        self._tabs.addTab(self._build_stats_tab(), "Token 统计")

    # ---- 页签 1：Key 分发 ----

    def _build_keys_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QHBoxLayout()
        btn_new = QPushButton("＋ 新建 Key")
        btn_new.clicked.connect(self._on_create_key)
        btn_edit = QPushButton("编辑")
        btn_edit.clicked.connect(self._on_edit_key)
        btn_toggle = QPushButton("禁用/启用")
        btn_toggle.clicked.connect(self._on_toggle_key)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._on_delete_key)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.load_keys)
        for b in (btn_new, btn_edit, btn_toggle, btn_del):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(btn_refresh)
        v.addLayout(bar)

        self._keys_table = QTableWidget(0, 7)
        self._keys_table.setHorizontalHeaderLabels(
            ["启用", "连接名称", "API Key", "project 标签", "模型映射", "最后使用", "今日调用"])
        self._keys_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._keys_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._keys_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._keys_table.verticalHeader().setVisible(False)
        self._keys_table.itemClicked.connect(self._on_key_cell_clicked)
        self._keys_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._keys_table.itemSelectionChanged.connect(self._on_key_selection_changed)

        card = QWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self._snippet_title = QLabel("接入片段")
        set_text_role(self._snippet_title, "secondary")
        bar.addWidget(self._snippet_title)
        bar.addStretch()
        # 明文开关（B11）：默认掩码显示，复制按钮始终复制明文
        self._snippet_cfg_text = ""
        self._snippet_curl_text = ""
        self._snippet_reveal = QCheckBox("显示明文")
        self._snippet_reveal.setToolTip(
            "默认掩码显示 api_key；勾选后明文显示。「复制配置/复制自测命令」始终复制明文。"
        )
        self._snippet_reveal.toggled.connect(self._update_snippet)
        bar.addWidget(self._snippet_reveal)
        btn_copy_cfg = QPushButton("复制配置")
        btn_copy_cfg.clicked.connect(lambda: self._copy_text(self._snippet_cfg_text))
        btn_copy_curl = QPushButton("复制自测命令")
        btn_copy_curl.clicked.connect(lambda: self._copy_text(self._snippet_curl_text))
        bar.addWidget(btn_copy_cfg)
        bar.addWidget(btn_copy_curl)
        cv.addLayout(bar)
        # 说明与可复制内容分离（反馈 1c）：说明只放标签，代码块纯用于粘贴
        self._snippet_desc = QLabel()
        set_text_role(self._snippet_desc, "tertiary")
        self._snippet_desc.setWordWrap(True)
        cv.addWidget(self._snippet_desc)
        cfg_label = QLabel("harness 配置（粘贴到 base_url / api_key / model 字段）")
        set_text_role(cfg_label, "tertiary")
        cv.addWidget(cfg_label)
        self._snippet_cfg = QPlainTextEdit()
        self._snippet_cfg.setReadOnly(True)
        self._snippet_cfg.setMaximumHeight(92)
        cv.addWidget(self._snippet_cfg)
        curl_label = QLabel("连通性自测命令（终端执行，返回 JSON 即接入成功）")
        set_text_role(curl_label, "tertiary")
        cv.addWidget(curl_label)
        self._snippet_curl = QPlainTextEdit()
        self._snippet_curl.setReadOnly(True)
        self._snippet_curl.setMaximumHeight(64)
        cv.addWidget(self._snippet_curl)
        hint = QLabel("服务只监听 127.0.0.1；本地 key 用于区分连接而非安全边界。"
                      "点击 API Key 单元格可在掩码/明文间切换。")
        set_text_role(hint, "tertiary")
        hint.setWordWrap(True)
        cv.addWidget(hint)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._keys_table)
        split.addWidget(card)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        split.setSizes([420, 200])
        v.addWidget(split, 1)
        return w

    # ---- 页签 2：调用日志 ----

    def _build_calls_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QHBoxLayout()
        self._filter_key = QComboBox()
        self._filter_model = QLineEdit()
        self._filter_model.setPlaceholderText("模型（回车应用）")
        self._filter_model.returnPressed.connect(self.load_calls)
        self._filter_status = QComboBox()
        self._filter_status.addItems(["全部状态", "成功", "失败"])
        self._date_from = QDateEdit(QDate.currentDate())
        self._date_from.setCalendarPopup(True)
        self._date_to = QDateEdit(QDate.currentDate())
        self._date_to.setCalendarPopup(True)
        self._only_fail = QCheckBox("仅失败")
        btn_q = QPushButton("查询")
        btn_q.clicked.connect(self.load_calls)
        self._auto_ck = QCheckBox("自动刷新")
        self._auto_ck.toggled.connect(self._on_auto_toggled)
        for widget in (self._filter_key, self._filter_model, self._filter_status,
                       self._date_from, QLabel("→"), self._date_to, self._only_fail):
            if isinstance(widget, str):
                lbl = QLabel(widget)
                set_text_role(lbl, "tertiary")
                bar.addWidget(lbl)
            else:
                bar.addWidget(widget)
        bar.addStretch()
        bar.addWidget(self._auto_ck)
        bar.addWidget(btn_q)
        v.addLayout(bar)

        # 筛选变化即自动查询（300ms 防抖，反馈③）；模型输入框保持回车应用
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(300)
        self._filter_debounce.timeout.connect(self.load_calls)
        self._filter_key.currentIndexChanged.connect(self._schedule_calls_reload)
        self._filter_status.currentIndexChanged.connect(self._schedule_calls_reload)
        self._date_from.dateChanged.connect(self._schedule_calls_reload)
        self._date_to.dateChanged.connect(self._schedule_calls_reload)
        self._only_fail.toggled.connect(self._schedule_calls_reload)

        self._calls_table = QTableWidget(0, 9)
        self._calls_table.setHorizontalHeaderLabels(
            ["时间", "连接", "请求模型 → 实际模型", "上游 key", "prompt", "completion",
             "TTFT", "总耗时", "状态"])
        self._calls_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._calls_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._calls_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._calls_table.verticalHeader().setVisible(False)
        self._calls_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._calls_table.itemClicked.connect(self._on_call_row_clicked)

        detail_pane = QWidget()
        dv = QVBoxLayout(detail_pane)
        dv.setContentsMargins(0, 0, 0, 0)
        detail_label = QLabel("调用详情（点击行展开）")
        set_text_role(detail_label, "secondary")
        dv.addWidget(detail_label)
        self._call_detail = QTextEdit()
        self._call_detail.setReadOnly(True)
        dv.addWidget(self._call_detail)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._calls_table)
        split.addWidget(detail_pane)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        split.setSizes([420, 200])
        v.addWidget(split, 1)
        return w

    # ---- 页签 3：模型映射 ----

    def _build_mapping_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QHBoxLayout()
        self._map_key_combo = QComboBox()
        self._map_key_combo.currentIndexChanged.connect(self._on_mapping_key_changed)
        btn_add = QPushButton("＋ 添加映射")
        btn_add.clicked.connect(self._on_mapping_add_row)
        btn_del_row = QPushButton("删除选中行")
        btn_del_row.clicked.connect(self._on_mapping_del_row)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._on_mapping_save)
        bar.addWidget(QLabel("编辑连接："))
        bar.addWidget(self._map_key_combo)
        bar.addWidget(btn_add)
        bar.addWidget(btn_del_row)
        bar.addStretch()
        bar.addWidget(btn_save)
        v.addLayout(bar)

        self._map_table = QTableWidget(0, 3)
        self._map_table.setHorizontalHeaderLabels(["harness 请求名", "池内目标（精确匹配）", "兜底 Tier 范围"])
        self._map_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._map_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._map_table.verticalHeader().setVisible(False)
        v.addWidget(self._map_table, 1)

        note = QLabel("解析顺序：key 别名 → 池内模型名精确匹配 → 都不中返回 404（带可用列表）。"
                      "兜底 Tier 范围按别名可选：配置后目标模型不可用时在该范围内换模型，默认不兜底。")
        set_text_role(note, "tertiary")
        note.setWordWrap(True)
        v.addWidget(note)
        return w

    # ---- 页签 4：Token 统计 ----

    def _build_stats_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QHBoxLayout()
        self._stats_days = QComboBox()
        self._stats_days.addItems(["今天", "近 7 天", "近 30 天", "自定义范围"])
        self._stats_days.currentIndexChanged.connect(self._on_stats_range_changed)
        # 自定义范围（反馈⑥）：起止日期均限制在 30 天保留期内，超出不可选
        self._stats_from = QDateEdit(QDate.currentDate().addDays(-6))
        self._stats_to = QDateEdit(QDate.currentDate())
        for d in (self._stats_from, self._stats_to):
            d.setCalendarPopup(True)
            d.setDateRange(QDate.currentDate().addDays(-29), QDate.currentDate())
            d.setVisible(False)
            d.dateChanged.connect(self._on_stats_date_changed)
        self._stats_arrow = QLabel("→")
        set_text_role(self._stats_arrow, "tertiary")
        self._stats_arrow.setVisible(False)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.load_stats)
        bar.addWidget(self._stats_days)
        bar.addWidget(self._stats_from)
        bar.addWidget(self._stats_arrow)
        bar.addWidget(self._stats_to)
        bar.addStretch()
        bar.addWidget(btn_refresh)
        v.addLayout(bar)

        self._stat_cards_layout = QHBoxLayout()
        v.addLayout(self._stat_cards_layout)
        self._stat_cards: list[QLabel] = []

        tables = QHBoxLayout()
        left = QVBoxLayout()
        lk = QLabel("按连接聚合")
        set_text_role(lk, "secondary")
        left.addWidget(lk)
        self._stats_by_key = QTableWidget(0, 5)
        self._stats_by_key.setHorizontalHeaderLabels(
            ["连接", "请求", "tokens", "TTFT p50", "成功率"])
        self._finish_table(self._stats_by_key)
        left.addWidget(self._stats_by_key, 1)
        right = QVBoxLayout()
        rk = QLabel("按转发目标聚合（各上游消耗）")
        set_text_role(rk, "secondary")
        right.addWidget(rk)
        self._stats_by_up = QTableWidget(0, 5)
        self._stats_by_up.setHorizontalHeaderLabels(
            ["上游 key", "请求", "tokens", "p95 延迟", "成功率"])
        self._finish_table(self._stats_by_up)
        right.addWidget(self._stats_by_up, 1)
        tables.addLayout(left)
        tables.addLayout(right)
        v.addLayout(tables, 1)

        note = QLabel("全部数字由网关 SQLite 日志表聚合（按连接 GROUP BY key_id，按转发目标 GROUP BY upstream_key）；"
                      "不复用池的 per-project / per-key 统计。此页为只读视图。")
        set_text_role(note, "tertiary")
        v.addWidget(note)
        return w

    @staticmethod
    def _finish_table(t: QTableWidget) -> None:
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)

    # ================= 数据加载 =================

    def _start_fetch(self, on_done, method: str, path: str,
                     params: dict | None = None, body: dict | None = None) -> None:
        f = _Fetch(self._http, method, path, params, body)
        f.dataReady.connect(on_done)
        f.finished.connect(lambda f=f: self._fetches.remove(f) if f in self._fetches else None)
        self._fetches.append(f)
        f.start()

    def on_show(self) -> None:
        self.load_keys()
        self.load_calls()
        self.load_stats()
        self._load_pool_models()

    def on_refresh(self) -> None:
        self.on_show()

    # ---- Key 分发 ----

    def load_keys(self) -> None:
        self._start_fetch(self._on_keys_loaded, "get", "/inbound/keys")

    def _on_keys_loaded(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        self._keys = result.get("keys", [])
        table = self._keys_table
        table.setRowCount(0)
        for k in self._keys:
            row = table.rowCount()
            table.insertRow(row)
            enabled = bool(k.get("enabled"))
            table.setCellWidget(row, 0, _dot_label("●" if enabled else "○",
                                                   "success" if enabled else "tertiary"))
            table.setItem(row, 1, _readonly(k.get("name", "")))
            secret_item = QTableWidgetItem(_mask(k.get("secret", "")))
            secret_item.setData(Qt.ItemDataRole.UserRole, k.get("secret", ""))
            secret_item.setToolTip("点击切换掩码/明文")
            table.setItem(row, 2, secret_item)
            table.setItem(row, 3, _readonly(k.get("project", "")))
            alias_count = len(k.get("aliases") or {})
            table.setItem(row, 4, _readonly(f"{alias_count} 条别名" if alias_count else "不限（精确匹配）"))
            table.setItem(row, 5, _readonly((k.get("last_used_at") or "—").replace("T", " ")))
            table.setItem(row, 6, _readonly(str(k.get("today_calls", 0))))
        _autosize_columns(table, stretch_col=1)
        if self._snippet_key_id:
            for row, kk in enumerate(self._keys):
                if kk.get("key_id") == self._snippet_key_id:
                    table.blockSignals(True)
                    table.selectRow(row)
                    table.blockSignals(False)
                    break
        self._refresh_mapping_combo()
        self._refresh_filter_combo()
        self._update_snippet()

    def _selected_row_key(self) -> dict | None:
        row = self._keys_table.currentRow()
        if row < 0 or row >= len(self._keys):
            return None
        return self._keys[row]

    def _on_key_cell_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 2:  # API Key 列：掩码 ↔ 明文
            secret = item.data(Qt.ItemDataRole.UserRole) or ""
            if item.text().startswith(_MASK_PREFIX):
                item.setText(secret)
            else:
                item.setText(_mask(secret))

    def _on_key_selection_changed(self) -> None:
        self._update_snippet()

    def _copy_text(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    def _update_snippet(self) -> None:
        """接入片段跟随选中行（反馈 1b）；无选中时沿用上次跟随的连接，再退回第一行。"""
        k = self._selected_row_key()
        if k is None and self._snippet_key_id:
            k = next((x for x in self._keys
                      if x.get("key_id") == self._snippet_key_id), None)
        if k is None and self._keys:
            k = self._keys[0]
        if not k:
            self._snippet_key_id = None
            self._snippet_title.setText("接入片段")
            self._snippet_desc.setText("（尚无 key，先「＋ 新建 Key」）")
            self._snippet_cfg_text = ""
            self._snippet_curl_text = ""
            self._snippet_cfg.setPlainText("")
            self._snippet_curl.setPlainText("")
            return
        self._snippet_key_id = k.get("key_id")
        name = k.get("name", "")
        secret = k.get("secret", "")
        aliases = k.get("aliases") or {}
        first_alias = next(iter(aliases), "<池内模型名>")
        self._snippet_title.setText(f"接入片段 — 当前连接：{name}")
        model_names = "、".join(aliases.keys()) if aliases else "（未配置别名，直接用池内模型名）"
        self._snippet_desc.setText(
            f"把下方配置的三项填进任意 OpenAI 兼容 harness 即完成接入。"
            f"该连接可用模型名：{model_names}。")
        self._snippet_cfg_text = (
            "base_url = http://127.0.0.1:8766/v1\n"
            f"api_key  = {secret}\n"
            f"model    = {first_alias}")
        self._snippet_curl_text = (
            f'curl http://127.0.0.1:8766/v1/chat/completions -H "Authorization: Bearer {secret}" \\\n'
            f'  -d \'{{"model":"{first_alias}","messages":[{{"role":"user","content":"ping"}}]}}\'')
        shown = secret if self._snippet_reveal.isChecked() else self._mask_secret(secret)
        self._snippet_cfg.setPlainText(self._snippet_cfg_text.replace(secret, shown))
        self._snippet_curl.setPlainText(self._snippet_curl_text.replace(secret, shown))

    @staticmethod
    def _mask_secret(secret: str) -> str:
        """掩码显示：保留前 8 位（如 sk-la-1a）与后 4 位，中间打点。"""
        if len(secret) <= 12:
            return "•" * len(secret)
        return f"{secret[:8]}••••{secret[-4:]}"

    def _on_create_key(self) -> None:
        dlg = _KeyDialog(self, "新建 Key")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, project = dlg.result_values()
        self._start_fetch(lambda _r: self.load_keys(), "post", "/inbound/keys",
                          body={"name": name, "project": project})

    def _on_edit_key(self) -> None:
        k = self._selected_row_key()
        if not k:
            return
        dlg = _KeyDialog(self, "编辑连接", name=k.get("name", ""), project=k.get("project", ""))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, project = dlg.result_values()
        self._start_fetch(lambda _r: self.load_keys(), "patch",
                          f"/inbound/keys/{k['key_id']}",
                          body={"name": name, "project": project})

    def _on_toggle_key(self) -> None:
        k = self._selected_row_key()
        if not k:
            return
        self._start_fetch(lambda _r: self.load_keys(), "patch",
                          f"/inbound/keys/{k['key_id']}",
                          body={"enabled": not bool(k.get("enabled"))})

    def _on_delete_key(self) -> None:
        k = self._selected_row_key()
        if not k:
            return
        ret = QMessageBox.question(
            self, "删除连接",
            f"删除连接「{k['name']}」？\n仅删除入站 key 与统计口径，不影响池内上游 key。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._start_fetch(lambda _r: self.load_keys(), "delete",
                          f"/inbound/keys/{k['key_id']}")

    # ---- 调用日志 ----

    def _refresh_filter_combo(self) -> None:
        cur = self._filter_key.currentText()
        self._filter_key.blockSignals(True)
        self._filter_key.clear()
        self._filter_key.addItem("全部连接", None)
        for k in self._keys:
            self._filter_key.addItem(k.get("name", ""), k.get("key_id"))
        if cur:
            idx = self._filter_key.findText(cur)
            if idx >= 0:
                self._filter_key.setCurrentIndex(idx)
        self._filter_key.blockSignals(False)

    def load_calls(self) -> None:
        params: dict[str, Any] = {"limit": 200}
        key_id = self._filter_key.currentData()
        if key_id:
            params["key_id"] = key_id
        model = self._filter_model.text().strip()
        if model:
            params["model"] = model
        if self._filter_status.currentIndex() == 1:
            params["status"] = "ok"
        elif self._filter_status.currentIndex() == 2 or self._only_fail.isChecked():
            params["status"] = "error"
        params["date_from"] = self._date_from.date().toString("yyyy-MM-dd")
        params["date_to"] = self._date_to.date().toString("yyyy-MM-dd")
        self._start_fetch(self._on_calls_loaded, "get", "/inbound/calls", params=params)

    def _on_calls_loaded(self, result: object) -> None:
        rows = result.get("calls", []) if isinstance(result, dict) else []
        table = self._calls_table
        self._last_calls = rows
        table.setRowCount(0)
        for r in rows:
            row = table.rowCount()
            table.insertRow(row)
            ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts") or 0))
            table.setItem(row, 0, _readonly(ts))
            table.setItem(row, 1, _readonly(r.get("key_name", "")))
            arrow = f"{r.get('model_requested', '')}  →  {r.get('model_used', '')}"
            if r.get("substituted"):
                arrow += "  ↺兜底"
                item_model = _readonly(arrow)
                item_model.setToolTip(
                    f"池未满足目标模型「{r.get('resolved_model') or r.get('model_requested', '')}」，"
                    f"在兜底范围内换用了「{r.get('model_used', '')}」。")
                table.setItem(row, 2, item_model)
            else:
                table.setItem(row, 2, _readonly(arrow))
            table.setItem(row, 3, _readonly(r.get("upstream_key") or "—"))
            table.setItem(row, 4, _readonly(f"{r.get('prompt_tokens', 0):,}"))
            table.setItem(row, 5, _readonly(f"{r.get('completion_tokens', 0):,}"))
            ttft = r.get("ttft_ms")
            table.setItem(row, 6, _readonly(f"{ttft}ms" if ttft is not None else "—"))
            table.setItem(row, 7, _readonly(f"{(r.get('duration_ms') or 0) / 1000:.1f}s"))
            ok = r.get("status") == "ok"
            table.setItem(row, 8, _dot_item("● 成功" if ok else "● 失败",
                                            tokens.SUCCESS_TEXT if ok else tokens.DANGER_TEXT))
        _autosize_columns(table, stretch_col=2)

    def _schedule_calls_reload(self, *_args) -> None:
        """筛选变化即自动查询（300ms 防抖，反馈③）。"""
        self._filter_debounce.start()

    def _on_call_row_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        rows = getattr(self, "_last_calls", [])
        if 0 <= row < len(rows):
            import json
            self._call_detail.setPlainText(json.dumps(rows[row], ensure_ascii=False, indent=2))

    def _on_auto_toggled(self, checked: bool) -> None:
        if checked:
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _on_auto_refresh(self) -> None:
        if self.isVisible():
            self.load_calls()

    # ---- 模型映射 ----

    def _refresh_mapping_combo(self) -> None:
        cur = self._map_key_combo.currentData()
        self._map_key_combo.blockSignals(True)
        self._map_key_combo.clear()
        for k in self._keys:
            self._map_key_combo.addItem(k.get("name", ""), k.get("key_id"))
        if cur:
            idx = self._map_key_combo.findData(cur)
            if idx >= 0:
                self._map_key_combo.setCurrentIndex(idx)
        self._map_key_combo.blockSignals(False)
        self._on_mapping_key_changed()

    def _current_mapping_key(self) -> dict | None:
        key_id = self._map_key_combo.currentData()
        for k in self._keys:
            if k.get("key_id") == key_id:
                return k
        return self._keys[0] if self._keys else None

    def _on_mapping_key_changed(self, *_args) -> None:
        k = self._current_mapping_key()
        self._map_table.setRowCount(0)
        if not k:
            return
        tiers = k.get("alias_fallback_tiers") or {}
        for alias, target in (k.get("aliases") or {}).items():
            row = self._map_table.rowCount()
            self._map_table.insertRow(row)
            self._map_table.setItem(row, 0, QTableWidgetItem(alias))
            self._map_table.setItem(row, 1, QTableWidgetItem(target))
            self._map_table.setItem(row, 2, _tier_item(tiers.get(alias)))

    def _on_mapping_add_row(self) -> None:
        dlg = _AddMappingDialog(self, self._pool_models, self._current_alias_names())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        alias, target, tier = dlg.result_values()
        row = self._map_table.rowCount()
        self._map_table.insertRow(row)
        self._map_table.setItem(row, 0, QTableWidgetItem(alias))
        self._map_table.setItem(row, 1, QTableWidgetItem(target))
        self._map_table.setItem(row, 2, _tier_item(tier))

    def _current_alias_names(self) -> list[str]:
        names: list[str] = []
        for row in range(self._map_table.rowCount()):
            item = self._map_table.item(row, 0)
            if item is not None and item.text().strip():
                names.append(item.text().strip())
        return names

    def _load_pool_models(self) -> None:
        self._start_fetch(self._on_pool_models_loaded, "get", "/llm/pool/models")

    def _on_pool_models_loaded(self, result: object) -> None:
        if not isinstance(result, dict) or not result.get("initialized"):
            self._pool_models = []
            return
        self._pool_models = [m.get("name", "") for m in result.get("models", [])
                             if isinstance(m, dict) and m.get("name")]

    def _on_mapping_del_row(self) -> None:
        rows = sorted({i.row() for i in self._map_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._map_table.removeRow(row)

    def _on_mapping_save(self) -> None:
        k = self._current_mapping_key()
        if not k:
            return
        aliases: dict[str, str] = {}
        tiers: dict[str, list[int]] = {}
        for row in range(self._map_table.rowCount()):
            item_a = self._map_table.item(row, 0)
            item_t = self._map_table.item(row, 1)
            item_f = self._map_table.item(row, 2)
            a = item_a.text().strip() if item_a else ""
            t = item_t.text().strip() if item_t else ""
            if a and t:
                aliases[a] = t
                ft = _parse_tier_text(item_f.text() if item_f else "")
                if ft is not None:
                    tiers[a] = ft
        self._start_fetch(lambda _r: self.load_keys(), "patch",
                          f"/inbound/keys/{k['key_id']}",
                          body={"aliases": aliases, "alias_fallback_tiers": tiers})

    # ---- Token 统计 ----

    def _on_stats_range_changed(self, index: int) -> None:
        custom = index == 3
        self._stats_from.setVisible(custom)
        self._stats_arrow.setVisible(custom)
        self._stats_to.setVisible(custom)
        self.load_stats()

    def _on_stats_date_changed(self, *_args) -> None:
        """约束起 ≤ 止（跨度由 setDateRange 限制在 30 天保留期内），然后刷新。"""
        d_from, d_to = self._stats_from.date(), self._stats_to.date()
        if d_from > d_to:
            if self.sender() is self._stats_from:
                self._stats_to.setDate(d_from)
            else:
                self._stats_from.setDate(d_to)
            return
        self.load_stats()

    def load_stats(self) -> None:
        idx = self._stats_days.currentIndex()
        if idx == 3:
            params: dict[str, Any] = {
                "date_from": self._stats_from.date().toString("yyyy-MM-dd"),
                "date_to": self._stats_to.date().toString("yyyy-MM-dd"),
            }
        else:
            params = {"days": (1, 7, 30)[idx]}
        self._start_fetch(self._on_stats_loaded, "get", "/inbound/stats", params=params)

    def _on_stats_loaded(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        ov = result.get("overview") or {}
        cards = [
            ("总请求", f"{ov.get('requests', 0):,}", f"失败 {ov.get('failed', 0)} · 重试 {ov.get('retries', 0)}"),
            ("总 tokens", f"{(ov.get('total_tokens') or 0) / 1e6:.2f}M",
             f"prompt {(ov.get('prompt_tokens') or 0) / 1e6:.1f}M / completion {(ov.get('completion_tokens') or 0) / 1e6:.1f}M"),
            ("TTFT 中位数", self._fmt_ms(ov.get("ttft_p50_ms")), "流式请求"),
            ("TTFT p95", self._fmt_ms(ov.get("ttft_p95_ms")), f"样本 {ov.get('ttft_samples', 0):,}"),
            ("成功率", f"{ov.get('success_rate')}%" if ov.get("success_rate") is not None else "—", ""),
            ("兜底替换", f"{ov.get('substituted', 0):,}", "换用非目标模型的请求"),
        ]
        while self._stat_cards_layout.count():
            item = self._stat_cards_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._stat_cards.clear()
        for lbl, val, sub in cards:
            frame = QWidget()
            fv = QVBoxLayout(frame)
            fv.setContentsMargins(10, 8, 10, 8)
            l1 = QLabel(lbl)
            set_text_role(l1, "tertiary")
            l2 = QLabel(val)
            l2.setStyleSheet(f"font-size:19px; color:{tokens.TEXT_PRIMARY};")
            l3 = QLabel(sub)
            set_text_role(l3, "tertiary")
            fv.addWidget(l1)
            fv.addWidget(l2)
            fv.addWidget(l3)
            frame.setStyleSheet(f"background:{tokens.BG_CARD};")
            self._stat_cards_layout.addWidget(frame)

        bk = result.get("by_key") or []
        self._stats_by_key.setRowCount(0)
        for r in bk:
            row = self._stats_by_key.rowCount()
            self._stats_by_key.insertRow(row)
            self._stats_by_key.setItem(row, 0, _readonly(r.get("key_name", "")))
            self._stats_by_key.setItem(row, 1, _readonly(f"{r.get('requests', 0):,}"))
            self._stats_by_key.setItem(row, 2, _readonly(f"{(r.get('tokens') or 0):,}"))
            self._stats_by_key.setItem(row, 3, _readonly(self._fmt_ms(r.get("ttft_p50_ms"))))
            sr = r.get("success_rate")
            color = tokens.SUCCESS_TEXT if (sr is not None and sr >= 95) else tokens.TEXT_PRIMARY
            self._stats_by_key.setItem(row, 4, _dot_item(
                f"{sr}%" if sr is not None else "—", color))

        bu = result.get("by_upstream") or []
        self._stats_by_up.setRowCount(0)
        for r in bu:
            row = self._stats_by_up.rowCount()
            self._stats_by_up.insertRow(row)
            self._stats_by_up.setItem(row, 0, _readonly(r.get("upstream_key", "")))
            self._stats_by_up.setItem(row, 1, _readonly(f"{r.get('requests', 0):,}"))
            self._stats_by_up.setItem(row, 2, _readonly(f"{(r.get('tokens') or 0):,}"))
            self._stats_by_up.setItem(row, 3, _readonly(self._fmt_ms(r.get("p95_duration_ms"))))
            sr = r.get("success_rate")
            color = tokens.SUCCESS_TEXT if (sr is not None and sr >= 95) else tokens.TEXT_PRIMARY
            self._stats_by_up.setItem(row, 4, _dot_item(
                f"{sr}%" if sr is not None else "—", color))
        _autosize_columns(self._stats_by_key, stretch_col=0)
        _autosize_columns(self._stats_by_up, stretch_col=0)

    @staticmethod
    def _fmt_ms(v: int | float | None) -> str:
        if v is None:
            return "—"
        return f"{v / 1000:.1f}s" if v >= 1000 else f"{int(v)}ms"

    def on_backend_status_change(self, online: bool) -> None:  # noqa: ARG002
        if online:
            self.on_show()
