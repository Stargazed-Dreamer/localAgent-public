"""Accounting 面板 — 记账 GUI 工具

6 个 tab：导入 / 审核 / 月度 / 汇总 / 映射表 / 导出
数据源：本地 SQLite（data/accounting.db），不走后端 HTTP。

核心流程：导入账单 → 审核分类 → 导出 Obsidian md
"""

import re
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.core.panel_base import PanelBase, PanelMeta
from workspace.accounting.panel_widgets import (
    _AccountingCalendar,
    _fmt_amount,
    _MappingDelegate,
    _ReviewDataLoader,
    _set_combo_completion,
)
from server.component_config import get_component_config
from workspace.accounting.exporter import export_month, git_commit
from workspace.accounting.parser import (
    classify_and_prepare,
    import_bill_file,
    read_bill_file,
)
from workspace.accounting.store import AccountingStore

# 标记颜色（行背景色）
_MARK_COLORS = {
    "✓": QColor("#1f4437"),   # 绿
    "!": QColor("#49391f"),   # 橙
    "?": QColor("#4a292e"),   # 红
    "⊘": QColor("#343942"),   # 灰
}
_MODIFIED_COLOR = QColor("#51471f")  # 修改高亮黄

# 板块选项
_SECTORS = ["出", "进", "理财"]


class AccountingPanel(PanelBase):
    """记账面板：账单导入 + 审核 + 导出"""

    PANEL_META = PanelMeta(
        id="accounting",
        title="记账",
        icon="💰",
        order=30,
        category="main",
        requires_backend=False,  # 本地 SQLite，不需要后端
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store: AccountingStore | None = None
        self._init_store()
        # 预览数据（导入 tab 用）
        self._preview_records: list[dict] = []
        self._preview_prepared: list[dict] = []
        self._current_file_path: Path | None = None
        # 审核 tab 信号阻塞标志（程序填充时避免触发 cellChanged）
        self._review_loading = False
        # 审核 tab 后台加载线程引用（防止被 GC 回收）
        self._review_loader: _ReviewDataLoader | None = None
        # 审核 tab 未暂存修改（tx_id → {sector, category, description, mark}）
        self._pending_edits: dict[str, dict] = {}
        self._mapping_completion_cache: dict[int, list[str]] = {}
        self._summary_start_date = QDate.currentDate()
        self._summary_end_date = QDate.currentDate()
        # 方案A：首次加载标志，避免每次切换面板都重新加载
        self._data_loaded = False
        self._build_ui()

    def _init_store(self) -> None:
        cfg = get_component_config("accounting") or {}
        self._store = AccountingStore(cfg["db_path"])
        self._store.initialize()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_import_tab(), "📥 导入")
        self._tabs.addTab(self._build_review_tab(), "✏️ 审核")
        self._tabs.addTab(self._build_monthly_tab(), "📅 月度")
        self._tabs.addTab(self._build_summary_tab(), "📊 汇总")
        self._tabs.addTab(self._build_mapping_tab(), "🔧 映射表")
        self._tabs.addTab(self._build_export_tab(), "📤 导出")
        layout.addWidget(self._tabs)

    # ═══════════════════════════════════════════════════════════
    # 导入 tab
    # ═══════════════════════════════════════════════════════════

    def _build_import_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 文件选择行
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("账单文件:"))
        self._file_path_edit = QLineEdit()
        self._file_path_edit.setPlaceholderText("选择 xlsx / csv / zip 文件")
        self._file_path_edit.setReadOnly(True)
        file_row.addWidget(self._file_path_edit, 1)
        btn_browse = QPushButton("📁 浏览")
        btn_browse.clicked.connect(self._on_browse_file)
        file_row.addWidget(btn_browse)
        layout.addLayout(file_row)

        # zip 密码行
        zip_row = QHBoxLayout()
        zip_row.addWidget(QLabel("ZIP 密码:"))
        self._zip_pwd_edit = QLineEdit()
        self._zip_pwd_edit.setPlaceholderText("支付宝 zip 密码（非 zip 文件留空）")
        self._zip_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        zip_row.addWidget(self._zip_pwd_edit, 1)
        btn_preview = QPushButton("🔍 预览")
        btn_preview.clicked.connect(self._on_preview)
        zip_row.addWidget(btn_preview)
        layout.addLayout(zip_row)

        # 预览表
        self._import_preview_table = QTableWidget(0, 8)
        self._import_preview_table.setHorizontalHeaderLabels([
            "日期", "来源", "原始名称", "金额", "方向", "标记", "大类", "说明",
        ])
        self._import_preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self._import_preview_table.horizontalHeader().setStretchLastSection(True)
        self._import_preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self._import_preview_table, 1)

        # 导入按钮 + 统计
        bottom_row = QHBoxLayout()
        self._import_status_label = QLabel("")
        bottom_row.addWidget(self._import_status_label, 1)
        self._btn_import = QPushButton("📥 导入到数据库")
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._on_import)
        bottom_row.addWidget(self._btn_import)
        layout.addLayout(bottom_row)

        return w

    def _on_browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择账单文件", "", "账单文件 (*.xlsx *.csv *.zip);;所有文件 (*)"
        )
        if path:
            self._current_file_path = Path(path)
            self._file_path_edit.setText(path)
            self._import_status_label.setText("")
            self._btn_import.setEnabled(False)

    def _on_preview(self) -> None:
        if not self._current_file_path or not self._current_file_path.exists():
            QMessageBox.warning(self, "提示", "请先选择账单文件")
            return

        zip_pwd = self._zip_pwd_edit.text().strip() or None
        try:
            records = read_bill_file(self._current_file_path, zip_pwd)
        except ValueError as e:
            QMessageBox.warning(self, "解析失败", str(e))
            return
        except RuntimeError as e:
            if "password" in str(e).lower():
                QMessageBox.warning(self, "解压失败", f"ZIP 密码错误: {e}")
            else:
                QMessageBox.critical(self, "解析失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "解析失败", str(e))
            return

        if not records:
            QMessageBox.information(self, "提示", "文件中没有账单记录")
            return

        self._preview_records = records
        mapping = self._store.get_mapping()
        self._preview_prepared = classify_and_prepare(records, mapping)

        # 填充预览表
        self._import_preview_table.setRowCount(len(self._preview_prepared))
        for i, p in enumerate(self._preview_prepared):
            items = [
                p["time"][:16],
                p["source"],
                p["counterparty"][:20],
                _fmt_amount(p["amount"]),
                p["direction"],
                p["mark"],
                p["category"],
                p["description"][:20],
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                if col == 5:  # 标记列着色
                    color = _MARK_COLORS.get(p["mark"])
                    if color:
                        item.setBackground(color)
                self._import_preview_table.setItem(i, col, item)

        # 统计
        marks = Counter(p["mark"] for p in self._preview_prepared)
        self._import_status_label.setText(
            f"共 {len(self._preview_prepared)} 条 | "
            f"✓{marks.get('✓', 0)} !{marks.get('!', 0)} "
            f"?{marks.get('?', 0)} ⊘{marks.get('⊘', 0)}"
        )
        self._btn_import.setEnabled(True)

    def _on_import(self) -> None:
        if not self._preview_prepared:
            QMessageBox.warning(self, "提示", "请先预览账单")
            return

        reply = QMessageBox.question(
            self, "确认导入",
            f"将导入 {len(self._preview_prepared)} 条记录到数据库，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            result = import_bill_file(
                self._current_file_path, self._store,
                zip_password=self._zip_pwd_edit.text().strip() or None,
            )
            QMessageBox.information(
                self, "导入成功",
                f"导入完成:\n"
                f"  新增: {result['inserted']} 条\n"
                f"  跳过(重复): {result['skipped']} 条\n"
                f"  未映射: {result['unmapped_count']} 条\n"
                f"  批次: {result['batch_id']}",
            )
            # 清空预览
            self._preview_records = []
            self._preview_prepared = []
            self._import_preview_table.setRowCount(0)
            self._btn_import.setEnabled(False)
            self._import_status_label.setText(
                f"上次导入: +{result['inserted']} 条 (跳过 {result['skipped']})"
            )
            # 刷新审核 tab
            self._load_review_data()
            self._refresh_monthly_month_options()
            self._refresh_summary_month_options()
            self._refresh_export_months()
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    # ═══════════════════════════════════════════════════════════
    # 审核 tab
    # ═══════════════════════════════════════════════════════════

    def _build_review_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 过滤栏
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("月份:"))
        self._review_month_filter = QComboBox()
        self._review_month_filter.addItem("全部", None)
        self._review_month_filter.currentIndexChanged.connect(self._load_review_data)
        filter_row.addWidget(self._review_month_filter)

        filter_row.addWidget(QLabel("标记:"))
        self._review_mark_filter = QComboBox()
        self._review_mark_filter.addItems(["全部", "✓", "!", "?", "⊘"])
        self._review_mark_filter.currentIndexChanged.connect(self._load_review_data)
        filter_row.addWidget(self._review_mark_filter)

        filter_row.addWidget(QLabel("来源:"))
        self._review_source_filter = QComboBox()
        self._review_source_filter.addItems(["全部", "微信", "支付宝"])
        self._review_source_filter.currentIndexChanged.connect(self._load_review_data)
        filter_row.addWidget(self._review_source_filter)

        filter_row.addStretch()
        btn_refresh = QPushButton("⟳ 刷新")
        btn_refresh.clicked.connect(self._load_review_data)
        filter_row.addWidget(btn_refresh)
        layout.addLayout(filter_row)

        # 审核表格
        self._review_table = QTableWidget(0, 10)
        self._review_table.setAlternatingRowColors(True)
        self._review_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._review_table.setHorizontalHeaderLabels([
            "标记", "日期", "原始名称", "商品", "金额", "方向",
            "板块", "大类", "说明", "tx_id",
        ])
        self._review_table.setColumnHidden(9, True)  # 隐藏 tx_id 列
        header = self._review_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self._review_table.cellChanged.connect(self._on_review_cell_changed)
        layout.addWidget(self._review_table, 1)

        # 底部操作栏
        bottom_row = QHBoxLayout()
        self._review_status_label = QLabel("")
        bottom_row.addWidget(self._review_status_label, 1)
        btn_stage = QPushButton("💾 暂存修改")
        btn_stage.setMinimumHeight(34)
        btn_stage.clicked.connect(self._on_stage_edits)
        bottom_row.addWidget(btn_stage)
        btn_discard = QPushButton("↩ 放弃修改")
        btn_discard.setMinimumHeight(34)
        btn_discard.clicked.connect(self._on_discard_edits)
        bottom_row.addWidget(btn_discard)
        btn_confirm_selected = QPushButton("✓ 确认选中")
        btn_confirm_selected.setMinimumHeight(34)
        btn_confirm_selected.clicked.connect(self._on_confirm_selected)
        bottom_row.addWidget(btn_confirm_selected)
        btn_confirm_all = QPushButton("✓✓ 全部确认")
        btn_confirm_all.setMinimumHeight(34)
        btn_confirm_all.setProperty("primary", True)
        btn_confirm_all.clicked.connect(self._on_confirm_all)
        bottom_row.addWidget(btn_confirm_all)
        layout.addLayout(bottom_row)

        return w

    def _load_review_data(self) -> None:
        """加载审核数据到表格（异步：DB 查询放后台线程，避免阻塞 UI）

        性能优化要点：
        - 方案C：DB 查询移到 QThread，主线程不阻塞
        - 方案B：descriptions 一次性预取（在 _ReviewDataLoader 中），不再每行查 DB
        - 加载中标志防止重复触发（用户快速切换过滤时忽略新请求）
        """
        if self._review_loading:
            return  # 已在加载中，忽略重复请求
        self._review_loading = True
        self._review_table.setRowCount(0)
        self._review_status_label.setText("加载中…")

        # 读取当前过滤条件（过滤控件可能还没初始化，如在 _build_ui 期间）
        if hasattr(self, "_review_month_filter"):
            month = self._review_month_filter.currentData()
            mark_filter = self._review_mark_filter.currentText()
            source = self._review_source_filter.currentText()
        else:
            month = None
            mark_filter = "全部"
            source = "全部"

        # 启动后台加载线程
        self._review_loader = _ReviewDataLoader(
            self._store, month, mark_filter, source
        )
        self._review_loader.data_ready.connect(self._on_review_data_ready)
        self._review_loader.error.connect(self._on_review_data_error)
        self._review_loader.start()

    def _on_review_data_error(self, msg: str) -> None:
        """后台加载失败"""
        self._review_loading = False
        self._review_loader = None
        self._review_status_label.setText(f"加载失败: {msg}")

    def _on_review_data_ready(self, data: dict) -> None:
        """后台数据加载完成，填充表格（主线程）"""
        try:
            txs = data["txs"]
            months = data["months"]
            descriptions_map = data["descriptions_map"]
            config = data["config"]

            # 更新月份过滤选项
            current_month = self._review_month_filter.currentData()
            self._review_month_filter.blockSignals(True)
            self._review_month_filter.clear()
            self._review_month_filter.addItem("全部", None)
            for mk in months:
                self._review_month_filter.addItem(mk, mk)
            # 恢复选中
            if current_month:
                idx = self._review_month_filter.findData(current_month)
                if idx >= 0:
                    self._review_month_filter.setCurrentIndex(idx)
            self._review_month_filter.blockSignals(False)

            self._review_table.setRowCount(len(txs))

            for i, tx in enumerate(txs):
                # 标记
                mark_item = QTableWidgetItem(tx["mark"])
                color = _MARK_COLORS.get(tx["mark"])
                if color:
                    mark_item.setBackground(color)
                mark_item.setFlags(mark_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._review_table.setItem(i, 0, mark_item)

                # 日期
                date_item = QTableWidgetItem(tx["trade_time"][:16])
                date_item.setFlags(date_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._review_table.setItem(i, 1, date_item)

                # 原始名称
                name_item = QTableWidgetItem(tx["counterparty"][:30])
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                name_item.setToolTip(tx["counterparty"])
                self._review_table.setItem(i, 2, name_item)

                # 商品
                product_item = QTableWidgetItem(tx["product"][:30])
                product_item.setFlags(product_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                product_item.setToolTip(tx["product"])
                self._review_table.setItem(i, 3, product_item)

                # 金额
                amt_text = _fmt_amount(tx["amount"])
                if tx["direction"] == "收入":
                    amt_text = f"+{amt_text}"
                amt_item = QTableWidgetItem(amt_text)
                amt_item.setFlags(amt_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                amt_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._review_table.setItem(i, 4, amt_item)

                # 方向
                dir_item = QTableWidgetItem(tx["direction"])
                dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._review_table.setItem(i, 5, dir_item)

                # 板块（QComboBox）
                sector_combo = QComboBox()
                sector_combo.addItems(_SECTORS)
                sector_combo.setMinimumHeight(34)
                sector_combo.setStyleSheet(
                    "QComboBox { padding: 2px 6px; min-height: 30px; }"
                )
                idx = sector_combo.findText(tx["sector"] or "")
                if idx >= 0:
                    sector_combo.setCurrentIndex(idx)
                sector_combo.currentIndexChanged.connect(
                    lambda _, row=i: self._on_sector_changed(row)
                )
                self._review_table.setCellWidget(i, 6, sector_combo)

                # 大类（可编辑 QComboBox）
                cat_combo = QComboBox()
                sector_name = tx["sector"] or "出"
                categories = config["sectors"].get(sector_name, [])
                cat_combo.addItems(categories)
                _set_combo_completion(cat_combo, categories)
                cat_combo.setCurrentText(tx["category"] or "")
                cat_combo.currentTextChanged.connect(
                    lambda _, row=i: self._on_category_changed(row)
                )
                self._review_table.setCellWidget(i, 7, cat_combo)

                # 说明（可编辑 QComboBox）— 用预取的 descriptions_map，不再每行查 DB
                desc_combo = QComboBox()
                descriptions = descriptions_map.get(tx["category"] or "", [])
                desc_combo.addItems(descriptions)
                _set_combo_completion(desc_combo, descriptions)
                desc_combo.setCurrentText(tx["description"] or "")
                desc_combo.currentTextChanged.connect(
                    lambda _, row=i: self._on_description_changed(row)
                )
                self._review_table.setCellWidget(i, 8, desc_combo)
                self._review_table.setRowHeight(i, 40)

                # tx_id（隐藏列）
                id_item = QTableWidgetItem(tx["id"])
                self._review_table.setItem(i, 9, id_item)

                # 行背景色（根据标记）
                if color:
                    for col in range(9):
                        item = self._review_table.item(i, col)
                        if item and col != 0:
                            item.setBackground(color)

            # 统计
            pending_count = sum(1 for t in txs if t["confirm_status"] != "confirmed" and not t["is_skip"])
            staged_count = len(self._pending_edits)
            self._review_status_label.setText(
                f"共 {len(txs)} 条 | 待审核 {pending_count} 条 | 未暂存修改 {staged_count} 条"
            )
        finally:
            self._review_loading = False
            self._review_loader = None

    def _on_review_cell_changed(self, row: int, col: int) -> None:
        """单元格内容变化（目前仅用于标记列的联动，主要编辑通过 combo 信号）"""
        if self._review_loading:
            return

    def _get_tx_id(self, row: int) -> str | None:
        item = self._review_table.item(row, 9)
        return item.text() if item else None

    def _on_sector_changed(self, row: int) -> None:
        """板块改变 → 更新大类可选项 + 暂存修改"""
        if self._review_loading:
            return
        sector_combo = self._review_table.cellWidget(row, 6)
        cat_combo = self._review_table.cellWidget(row, 7)
        if not sector_combo or not cat_combo:
            return
        new_sector = sector_combo.currentText()
        config = self._store.get_config()
        categories = config["sectors"].get(new_sector, [])

        cat_combo.blockSignals(True)
        cat_combo.clear()
        cat_combo.addItems(categories)
        _set_combo_completion(cat_combo, categories)
        cat_combo.blockSignals(False)

        # 写入暂存（不立即写 DB）
        tx_id = self._get_tx_id(row)
        if tx_id:
            self._pending_edits.setdefault(tx_id, {}).update({
                "sector": new_sector,
                "confirm_status": "manual",
            })
            self._update_review_status()
        self._mark_row_modified(row)

    def _on_description_changed(self, row: int) -> None:
        """说明改变 → 暂存修改 + 标记"""
        if self._review_loading:
            return
        desc_combo = self._review_table.cellWidget(row, 8)
        cat_combo = self._review_table.cellWidget(row, 7)
        if not desc_combo:
            return
        new_desc = desc_combo.currentText().strip()
        if not new_desc:
            return
        tx_id = self._get_tx_id(row)
        if tx_id:
            # 暂存修改（不立即写 DB）
            self._pending_edits.setdefault(tx_id, {}).update({
                "description": new_desc,
                "mark": "✓",
                "confirm_status": "manual",
            })
            # 如果新说明不在历史列表中，添加到配置（配置是独立表，可立即写）
            category = cat_combo.currentText().strip() if cat_combo else ""
            if category:
                self._store.add_description(category, new_desc)
            # 更新标记列
            mark_item = self._review_table.item(row, 0)
            if mark_item:
                mark_item.setText("✓")
                mark_item.setBackground(_MARK_COLORS["✓"])
            self._update_review_status()
        self._mark_row_modified(row)

    def _on_category_changed(self, row: int) -> None:
        """大类改变 → 更新说明可选项 + 暂存修改"""
        if self._review_loading:
            return
        cat_combo = self._review_table.cellWidget(row, 7)
        desc_combo = self._review_table.cellWidget(row, 8)
        if not cat_combo:
            return
        new_category = cat_combo.currentText().strip()
        if not new_category:
            return

        # 更新说明可选项
        if desc_combo:
            descriptions = self._store.get_descriptions_for_category(new_category)
            desc_combo.blockSignals(True)
            desc_combo.clear()
            desc_combo.addItems(descriptions)
            _set_combo_completion(desc_combo, descriptions)
            desc_combo.blockSignals(False)

        # 暂存修改（不立即写 DB）
        tx_id = self._get_tx_id(row)
        if tx_id:
            self._pending_edits.setdefault(tx_id, {}).update({
                "category": new_category,
                "mark": "✓",
                "confirm_status": "manual",
            })
            self._store.ensure_category(
                self._review_table.cellWidget(row, 6).currentText(), new_category
            )
            # 更新标记列
            mark_item = self._review_table.item(row, 0)
            if mark_item:
                mark_item.setText("✓")
                mark_item.setBackground(_MARK_COLORS["✓"])
            self._update_review_status()
        self._mark_row_modified(row)

    def _mark_row_modified(self, row: int) -> None:
        """标记行为已修改（黄色背景）"""
        for col in range(9):
            item = self._review_table.item(row, col)
            if item and col != 0:  # 不覆盖标记列颜色
                item.setBackground(_MODIFIED_COLOR)

    def _on_stage_edits(self) -> None:
        """暂存修改：将 _pending_edits 批量写入 DB（不标记 confirmed）"""
        if not self._pending_edits:
            QMessageBox.information(self, "提示", "没有未暂存的修改")
            return
        count = 0
        for tx_id, updates in self._pending_edits.items():
            self._store.update_transaction(tx_id, updates)
            count += 1
        self._pending_edits.clear()
        QMessageBox.information(self, "暂存成功", f"已暂存 {count} 条修改到数据库")
        self._update_review_status()

    def _on_discard_edits(self) -> None:
        """放弃修改：清空 _pending_edits，重新从 DB 加载"""
        if not self._pending_edits:
            return
        reply = QMessageBox.question(
            self, "放弃修改",
            f"将放弃 {len(self._pending_edits)} 条未暂存修改，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._pending_edits.clear()
        self._load_review_data()

    def _update_review_status(self) -> None:
        """刷新审核 tab 底栏统计（只更新未暂存修改数）"""
        staged_count = len(self._pending_edits)
        current_text = self._review_status_label.text()
        if "未暂存修改" in current_text:
            current_text = re.sub(
                r"未暂存修改 \d+ 条",
                f"未暂存修改 {staged_count} 条",
                current_text,
            )
            self._review_status_label.setText(current_text)
        elif current_text:
            # 没有未暂存修改字段，追加
            self._review_status_label.setText(
                f"{current_text} | 未暂存修改 {staged_count} 条"
            )

    def _on_confirm_selected(self) -> None:
        """确认选中的行（先暂存修改，再标记 confirmed）"""
        rows = set()
        for item in self._review_table.selectedItems():
            rows.add(item.row())
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要确认的行")
            return
        tx_ids = []
        for row in rows:
            tx_id = self._get_tx_id(row)
            if tx_id:
                tx_ids.append(tx_id)
        if not tx_ids:
            return
        # 先暂存这些行的修改到 DB
        for tx_id in tx_ids:
            if tx_id in self._pending_edits:
                self._store.update_transaction(tx_id, self._pending_edits.pop(tx_id))
        # 再标记 confirmed
        count = self._store.confirm_transactions(tx_ids)
        QMessageBox.information(self, "确认成功", f"已确认 {count} 条交易")
        self._load_review_data()

    def _on_confirm_all(self) -> None:
        """确认所有待审核（先暂存所有修改，再全部确认）"""
        stats = self._store.get_stats()
        if stats["pending"] == 0 and not self._pending_edits:
            QMessageBox.information(self, "提示", "没有待审核的交易")
            return
        reply = QMessageBox.question(
            self, "确认全部",
            f"将确认所有 {stats['pending']} 条待审核交易"
            f"（含 {len(self._pending_edits)} 条未暂存修改），是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # 先暂存所有修改
        for tx_id, updates in self._pending_edits.items():
            self._store.update_transaction(tx_id, updates)
        self._pending_edits.clear()
        # 再全部确认
        count = self._store.confirm_all_pending()
        QMessageBox.information(self, "确认成功", f"已确认 {count} 条交易")
        self._load_review_data()

    # ═══════════════════════════════════════════════════════════
    # 月度 tab
    # ═══════════════════════════════════════════════════════════

    def _build_monthly_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 月份选择：只展示实际有数据的年份和月份；下拉框原生支持滚轮切换
        month_row = QHBoxLayout()
        month_row.addWidget(QLabel("年份:"))
        self._monthly_year_combo = QComboBox()
        self._monthly_year_combo.setMinimumWidth(110)
        self._monthly_year_combo.currentIndexChanged.connect(self._on_monthly_year_changed)
        month_row.addWidget(self._monthly_year_combo)
        month_row.addWidget(QLabel("月份:"))
        self._monthly_month_combo = QComboBox()
        self._monthly_month_combo.setMinimumWidth(100)
        self._monthly_month_combo.currentIndexChanged.connect(self._load_monthly_data)
        month_row.addWidget(self._monthly_month_combo)
        month_row.addStretch()
        btn_refresh = QPushButton("⟳ 刷新")
        btn_refresh.clicked.connect(self._load_monthly_data)
        month_row.addWidget(btn_refresh)
        layout.addLayout(month_row)

        # 月度树（三级折叠：板块/大类/明细）
        self._monthly_tree = QTreeWidget()
        self._monthly_tree.setHeaderLabels(["内容", "金额", "状态"])
        monthly_header = self._monthly_tree.header()
        monthly_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        monthly_header.setStretchLastSection(False)
        monthly_header.resizeSection(0, 380)
        monthly_header.resizeSection(1, 150)
        monthly_header.resizeSection(2, 135)
        self._monthly_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 右键菜单
        self._monthly_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._monthly_tree.customContextMenuRequested.connect(self._on_monthly_context_menu)
        layout.addWidget(self._monthly_tree, 1)

        # 底部统计
        self._monthly_summary_label = QLabel("")
        layout.addWidget(self._monthly_summary_label)

        self._refresh_monthly_month_options()
        return w

    def _get_sector_for_category(
        self, category: str, items: list[dict] | None = None
    ) -> str:
        """从配置反查板块，已删除大类则回退到交易保存的板块。"""
        config = self._store.get_config()
        for sector, categories in config["sectors"].items():
            if category in categories:
                return sector
        stored_sectors = [
            item.get("sector", "") for item in (items or [])
            if item.get("sector", "") in _SECTORS
        ]
        if stored_sectors:
            return Counter(stored_sectors).most_common(1)[0][0]
        if items and all(item.get("direction") == "收入" for item in items):
            return "进"
        return "出"

    def _format_category_line(self, category: str, items: list[dict]) -> str:
        """格式化为 Obsidian md 阅读视图风格：吃: 10午餐+5饮料"""
        parts = []
        for it in items:
            amt = _fmt_amount(it["amount"])
            name = it["description"] or "未命名"
            if it["direction"] == "收入":
                parts.append(f"+{amt}{name}")
            else:
                parts.append(f"{amt}{name}")
        return f"{category}: {'+'.join(parts)}"

    def _refresh_monthly_month_options(self) -> None:
        months = self._store.get_months()
        years = sorted({mk.split(".")[0] for mk in months}, reverse=True)
        current_year = self._monthly_year_combo.currentText() if hasattr(self, "_monthly_year_combo") else ""
        self._monthly_year_combo.blockSignals(True)
        self._monthly_year_combo.clear()
        self._monthly_year_combo.addItems(years)
        if current_year in years:
            self._monthly_year_combo.setCurrentText(current_year)
        elif years:
            self._monthly_year_combo.setCurrentIndex(0)
        self._monthly_year_combo.blockSignals(False)
        self._on_monthly_year_changed()

    def _on_monthly_year_changed(self, _index: int = -1) -> None:
        year = self._monthly_year_combo.currentText()
        months = [mk for mk in self._store.get_months() if mk.startswith(f"{year}.")]
        current = self._monthly_month_combo.currentText()
        self._monthly_month_combo.blockSignals(True)
        self._monthly_month_combo.clear()
        self._monthly_month_combo.addItems([mk.split(".", 1)[1] for mk in months])
        if current in [self._monthly_month_combo.itemText(i) for i in range(self._monthly_month_combo.count())]:
            self._monthly_month_combo.setCurrentText(current)
        elif self._monthly_month_combo.count():
            self._monthly_month_combo.setCurrentIndex(0)
        self._monthly_month_combo.blockSignals(False)
        self._load_monthly_data()

    def _monthly_selected_month(self) -> str:
        year = self._monthly_year_combo.currentText()
        month = self._monthly_month_combo.currentText()
        return f"{year}.{month}" if year and month else ""

    def _gray_unavailable_dates(self, calendar: QCalendarWidget, available_months: set) -> None:
        """灰化无数据月份的所有日期"""
        fmt_disabled = QTextCharFormat()
        fmt_disabled.setBackground(QColor("#eeeeee"))
        fmt_disabled.setForeground(QColor("#bbbbbb"))
        fmt_normal = QTextCharFormat()  # 默认格式（清除灰化）

        start = calendar.minimumDate()
        end = calendar.maximumDate()
        d = QDate(start)
        while d <= end:
            mk = f"{d.year()}.{d.month()}"
            if mk in available_months:
                calendar.setDateTextFormat(d, fmt_normal)
            else:
                calendar.setDateTextFormat(d, fmt_disabled)
            d = d.addDays(1)

    def _load_monthly_data(self) -> None:
        """加载月度数据到折叠树"""
        month_key = self._monthly_selected_month()
        if not month_key:
            self._monthly_summary_label.setText("暂无数据")
            self._monthly_tree.clear()
            return

        summary = self._store.get_month_summary(month_key)
        self._monthly_tree.clear()

        # 按板块分组
        sectors_data: dict[str, dict] = {}
        for cat, data in summary.items():
            sector = self._get_sector_for_category(cat, data["items"])
            sectors_data.setdefault(sector, {})[cat] = data

        total_expense = 0.0
        total_income = 0.0
        total_count = 0

        section_titles = {"出": "支出明细", "进": "收入明细", "理财": "投资明细"}
        for sector_name in ["出", "进", "理财"]:
            if sector_name not in sectors_data or not sectors_data[sector_name]:
                continue
            cats = sectors_data[sector_name]
            # 板块总额
            sector_total = sum(d["net"] for d in cats.values())

            sector_item = QTreeWidgetItem([
                section_titles[sector_name],
                (f"+{_fmt_amount(sector_total)}" if sector_total > 0 else _fmt_amount(sector_total)),
                "",
            ])
            font = sector_item.font(0)
            font.setBold(True)
            sector_item.setFont(0, font)
            sector_item.setFont(1, font)
            self._monthly_tree.addTopLevelItem(sector_item)
            sector_item.setExpanded(True)
            sector_item.setForeground(0, QColor("#1976d2") if sector_name == "进" else QColor("#d32f2f") if sector_name == "出" else QColor("#388e3c"))

            for cat, data in sorted(cats.items()):
                total_expense += data["total_expense"]
                total_income += data["total_income"]
                total_count += data["count"]

                cat_total = data["net"]
                denominator = sum(abs(d["net"]) for d in cats.values()) or 1
                percentage = abs(cat_total) / denominator * 100
                cat_item = QTreeWidgetItem([
                    cat, (f"+{_fmt_amount(cat_total)}" if cat_total > 0 else _fmt_amount(cat_total)),
                    f"{percentage:.1f}% · {data['count']}条",
                ])
                cat_item.setFont(0, font)
                sector_item.addChild(cat_item)
                cat_item.setExpanded(False)

                # 三级明细节点：Obsidian 编辑视图风格
                for tx in data["items"]:
                    detail_text = f"{tx['trade_time'][5:16]}  {tx['description'] or tx['counterparty']}"
                    amt = _fmt_amount(tx["amount"])
                    if tx["direction"] == "收入":
                        amt = f"+{amt}"
                    elif data["total_income"] > 0:
                        amt = f"-{amt}"
                    detail_item = QTreeWidgetItem([
                        detail_text, amt, tx["confirm_status"],
                    ])
                    detail_item.setData(0, Qt.ItemDataRole.UserRole, tx["id"])
                    detail_item.setForeground(1, QColor("#388e3c") if tx["direction"] == "收入" else QColor("#d32f2f"))
                    cat_item.addChild(detail_item)

        self._monthly_summary_label.setText(
            f"共 {total_count} 条 | 支出 {_fmt_amount(total_expense)} 元 | "
            f"收入 {_fmt_amount(total_income)} 元 | "
            f"净额 {_fmt_amount(total_income - total_expense)} 元"
        )

    def _on_monthly_context_menu(self, pos) -> None:
        """月度树右键菜单"""
        item = self._monthly_tree.itemAt(pos)
        if not item:
            return
        tx_id = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)

        if tx_id:
            # 明细节点：支持重新审核
            action_review = menu.addAction("✏️ 重新审核此条")
            action_review.triggered.connect(lambda: self._jump_to_review(tx_id))

        # 所有节点：复制文本
        action_copy = menu.addAction("📋 复制文本")
        action_copy.triggered.connect(lambda: self._copy_tree_item_text(item))

        if menu.actions():
            menu.exec(self._monthly_tree.viewport().mapToGlobal(pos))

    def _copy_tree_item_text(self, item: QTreeWidgetItem) -> None:
        """复制树节点文本到剪贴板"""
        text = item.text(0)
        if item.text(1):
            text += f"  {item.text(1)}"
        if item.text(2):
            text += f"  [{item.text(2)}]"
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        self._monthly_summary_label.setText(f"已复制: {text[:50]}...")

    def _jump_to_review(self, tx_id: str) -> None:
        """跳转到审核 tab 并定位到该交易"""
        # 通过 tab 文本切到审核 tab
        for i in range(self._tabs.count()):
            if "审核" in self._tabs.tabText(i):
                self._tabs.setCurrentIndex(i)
                break

        # 查找该 tx_id 所在行
        target_row = -1
        for row in range(self._review_table.rowCount()):
            if self._get_tx_id(row) == tx_id:
                target_row = row
                break

        if target_row >= 0:
            self._review_table.selectRow(target_row)
            self._review_table.scrollToItem(self._review_table.item(target_row, 0))
        else:
            QMessageBox.information(self, "提示", "该交易不在当前过滤范围内，请调整筛选条件")

    # ═══════════════════════════════════════════════════════════
    # 汇总 tab
    # ═══════════════════════════════════════════════════════════

    def _build_summary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 起止月份：只展示实际有数据的年月
        range_row = QHBoxLayout()
        self._summary_start_year = QComboBox()
        self._summary_start_month = QComboBox()
        self._summary_end_year = QComboBox()
        self._summary_end_month = QComboBox()
        for combo in (self._summary_start_year, self._summary_start_month,
                      self._summary_end_year, self._summary_end_month):
            combo.setMinimumHeight(30)
            combo.setMinimumWidth(82)
        range_row.addWidget(QLabel("起始:"))
        range_row.addWidget(self._summary_start_year)
        range_row.addWidget(self._summary_start_month)
        self._summary_start_date_label = QLabel("")
        range_row.addWidget(self._summary_start_date_label)
        self._summary_start_expand = QPushButton("展开日历")
        self._summary_start_expand.setMinimumHeight(32)
        self._summary_start_expand.clicked.connect(lambda: self._pick_summary_date("start"))
        range_row.addWidget(self._summary_start_expand)
        range_row.addSpacing(12)
        range_row.addWidget(QLabel("结束:"))
        range_row.addWidget(self._summary_end_year)
        range_row.addWidget(self._summary_end_month)
        self._summary_end_date_label = QLabel("")
        range_row.addWidget(self._summary_end_date_label)
        self._summary_end_expand = QPushButton("展开日历")
        self._summary_end_expand.setMinimumHeight(32)
        self._summary_end_expand.clicked.connect(lambda: self._pick_summary_date("end"))
        range_row.addWidget(self._summary_end_expand)
        range_row.addStretch()
        layout.addLayout(range_row)
        self._summary_start_year.currentIndexChanged.connect(lambda: self._refresh_summary_month_combo("start"))
        self._summary_end_year.currentIndexChanged.connect(lambda: self._refresh_summary_month_combo("end"))
        self._summary_start_month.currentIndexChanged.connect(self._load_summary_data)
        self._summary_end_month.currentIndexChanged.connect(self._load_summary_data)

        # 来源筛选 + 计算按钮
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("来源:"))
        self._summary_source_combo = QComboBox()
        self._summary_source_combo.addItems(["全部", "微信", "支付宝"])
        self._summary_source_combo.currentIndexChanged.connect(self._load_summary_data)
        filter_row.addWidget(self._summary_source_combo)
        btn_calc = QPushButton("📊 计算")
        btn_calc.clicked.connect(self._load_summary_data)
        filter_row.addWidget(btn_calc)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        # 汇总表格
        self._summary_table = QTableWidget(0, 0)
        self._summary_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._summary_table, 2)

        # 底部总计
        self._summary_total_label = QLabel("")
        layout.addWidget(self._summary_total_label)

        self._refresh_summary_month_options()
        return w

    def _refresh_summary_month_options(self) -> None:
        months = self._store.get_months()
        years = sorted({mk.split(".")[0] for mk in months}, reverse=True)
        for combo in (self._summary_start_year, self._summary_end_year):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(years)
            if current in years:
                combo.setCurrentText(current)
            elif years:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._refresh_summary_month_combo("start")
        self._refresh_summary_month_combo("end")
        if months:
            first = months[-1].split(".")
            last = months[0].split(".")
            self._summary_start_date = QDate(int(first[0]), int(first[1]), 1)
            self._summary_end_date = QDate(int(last[0]), int(last[1]), 1).addMonths(1).addDays(-1)
            self._update_summary_month_selectors_from_dates()
        self._load_summary_data()

    def _refresh_summary_month_combo(self, which: str) -> None:
        year_combo = self._summary_start_year if which == "start" else self._summary_end_year
        month_combo = self._summary_start_month if which == "start" else self._summary_end_month
        months = [mk for mk in self._store.get_months() if mk.startswith(f"{year_combo.currentText()}.")]
        current = month_combo.currentText()
        month_combo.blockSignals(True)
        month_combo.clear()
        month_combo.addItems([mk.split(".", 1)[1] for mk in months])
        if current in [month_combo.itemText(i) for i in range(month_combo.count())]:
            month_combo.setCurrentText(current)
        elif month_combo.count():
            month_combo.setCurrentIndex(0)
        month_combo.blockSignals(False)

    def _update_summary_date_labels(self) -> None:
        self._summary_start_date_label.setText(self._summary_start_date.toString("yyyy-MM-dd"))
        self._summary_end_date_label.setText(self._summary_end_date.toString("yyyy-MM-dd"))

    def _update_summary_month_selectors_from_dates(self) -> None:
        for which, date in (("start", self._summary_start_date), ("end", self._summary_end_date)):
            year_combo = self._summary_start_year if which == "start" else self._summary_end_year
            month_combo = self._summary_start_month if which == "start" else self._summary_end_month
            year_combo.setCurrentText(str(date.year()))
            self._refresh_summary_month_combo(which)
            month_combo.setCurrentText(str(date.month()))
        self._update_summary_date_labels()

    def _get_summary_day_totals(self) -> dict[QDate, float]:
        source = self._summary_source_combo.currentText() if hasattr(self, "_summary_source_combo") else "全部"
        totals: dict[QDate, float] = {}
        txs = self._store.query(include_skip=False, source=None if source == "全部" else source)
        for tx in txs:
            date = QDate.fromString(tx["trade_time"][:10], "yyyy-MM-dd")
            if not date.isValid():
                continue
            signed = tx["amount"] if tx["direction"] == "收入" else -tx["amount"]
            totals[date] = totals.get(date, 0.0) + signed
        return totals

    def _pick_summary_date(self, which: str) -> None:
        selected = self._summary_start_date if which == "start" else self._summary_end_date
        dialog = QDialog(self)
        dialog.setWindowTitle("选择详细日期")
        dialog.setModal(True)
        box = QVBoxLayout(dialog)
        calendar = _AccountingCalendar(dialog)
        calendar.setSelectedDate(selected)
        available = self._store.get_available_months()
        if available:
            first = available[0].split(".")
            last = available[-1].split(".")
            calendar.setMinimumDate(QDate(int(first[0]), int(first[1]), 1))
            calendar.setMaximumDate(QDate(int(last[0]), int(last[1]), 1).addMonths(1).addDays(-1))
        calendar.set_day_totals(self._get_summary_day_totals())
        box.addWidget(calendar)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        accept = QPushButton("确定")
        accept.setProperty("primary", True)
        cancel.clicked.connect(dialog.reject)
        accept.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        box.addLayout(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if which == "start":
                self._summary_start_date = calendar.selectedDate()
            else:
                self._summary_end_date = calendar.selectedDate()
            self._update_summary_month_selectors_from_dates()
            self._load_summary_data()

    def _load_summary_data(self) -> None:
        """加载汇总统计（年月下拉决定范围，详细日历决定边界日期）。"""
        if not self._summary_start_year.currentText() or not self._summary_start_month.currentText():
            self._summary_table.setRowCount(0)
            self._summary_total_label.setText("暂无数据")
            return
        self._summary_start_date = QDate(
            int(self._summary_start_year.currentText()),
            int(self._summary_start_month.currentText()),
            min(self._summary_start_date.day(), QDate(
                int(self._summary_start_year.currentText()),
                int(self._summary_start_month.currentText()), 1,
            ).daysInMonth()),
        )
        self._summary_end_date = QDate(
            int(self._summary_end_year.currentText()),
            int(self._summary_end_month.currentText()),
            min(self._summary_end_date.day(), QDate(
                int(self._summary_end_year.currentText()),
                int(self._summary_end_month.currentText()), 1,
            ).daysInMonth()),
        )
        if self._summary_start_date > self._summary_end_date:
            self._summary_start_date, self._summary_end_date = self._summary_end_date, self._summary_start_date
            self._update_summary_month_selectors_from_dates()
        self._update_summary_date_labels()
        start_date, end_date = self._summary_start_date, self._summary_end_date
        start = f"{start_date.year()}.{start_date.month()}"
        end = f"{end_date.year()}.{end_date.month()}"
        start_y, start_m = start_date.year(), start_date.month()
        end_y, end_m = end_date.year(), end_date.month()

        source_filter = self._summary_source_combo.currentText()

        # 生成月份序列
        month_keys = []
        y, m = start_y, start_m
        while (y, m) <= (end_y, end_m):
            month_keys.append(f"{y}.{m}")
            m += 1
            if m > 12:
                y, m = y + 1, 1

        if not month_keys:
            return

        # 收集所有大类
        all_categories = set()
        month_data: dict[str, dict[str, dict]] = {}  # {month: {category: {expense, income}}}

        for mk in month_keys:
            summary = self._store.get_month_summary(mk)
            month_data[mk] = {}
            for cat, data in summary.items():
                # 来源过滤
                items = [
                    it for it in data["items"]
                    if start_date <= QDate.fromString(it["trade_time"][:10], "yyyy-MM-dd") <= end_date
                ]
                if source_filter != "全部":
                    items = [it for it in items if it["source"] == source_filter]
                expense = sum(it["amount"] for it in items if it["direction"] != "收入")
                income = sum(it["amount"] for it in items if it["direction"] == "收入")
                if expense > 0 or income > 0:
                    month_data[mk][cat] = {"expense": expense, "income": income}
                    all_categories.add(cat)

        # 排序大类
        config = self._store.get_config()
        category_order = []
        for sector in ("出", "进", "理财"):
            for category in config["sectors"].get(sector, []):
                if category not in category_order:
                    category_order.append(category)
        sorted_categories = [c for c in category_order if c in all_categories]
        sorted_categories += sorted(all_categories - set(category_order))

        # 填充表格
        self._summary_table.setRowCount(len(month_keys))
        self._summary_table.setColumnCount(len(sorted_categories) + 2)
        headers = sorted_categories + ["总支出", "总收入"]
        self._summary_table.setHorizontalHeaderLabels(headers)
        total_start_col = len(sorted_categories)
        total_header_color = QColor("#31574f")
        for col in (total_start_col, total_start_col + 1):
            header_item = self._summary_table.horizontalHeaderItem(col)
            if header_item:
                font = header_item.font()
                font.setBold(True)
                header_item.setFont(font)
                header_item.setBackground(total_header_color)
                header_item.setForeground(QColor("#f4f6f8"))

        total_expense = 0.0
        total_income = 0.0
        for i, mk in enumerate(month_keys):
            row_expense = 0.0
            row_income = 0.0
            for j, cat in enumerate(sorted_categories):
                data = month_data[mk].get(cat)
                if data:
                    expense = data["expense"]
                    income = data["income"]
                    text = ""
                    if expense > 0:
                        text += _fmt_amount(expense)
                    if income > 0:
                        if text:
                            text += "\n"
                        text += f"+{_fmt_amount(income)}"
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self._summary_table.setItem(i, j, item)
                    row_expense += expense
                    row_income += income
                else:
                    self._summary_table.setItem(i, j, QTableWidgetItem(""))
            # 总计列
            exp_item = QTableWidgetItem(_fmt_amount(row_expense) if row_expense else "")
            exp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            exp_item.setBackground(QColor("#293f3a"))
            exp_item.setForeground(QColor("#ff9299"))
            exp_font = exp_item.font()
            exp_font.setBold(True)
            exp_item.setFont(exp_font)
            self._summary_table.setItem(i, len(sorted_categories), exp_item)
            inc_item = QTableWidgetItem(_fmt_amount(row_income) if row_income else "")
            inc_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            inc_item.setBackground(QColor("#293f3a"))
            inc_item.setForeground(QColor("#68d6aa"))
            inc_font = inc_item.font()
            inc_font.setBold(True)
            inc_item.setFont(inc_font)
            self._summary_table.setItem(i, len(sorted_categories) + 1, inc_item)
            # 月份标签
            mk_item = QTableWidgetItem(mk)
            self._summary_table.setVerticalHeaderItem(i, mk_item)
            total_expense += row_expense
            total_income += row_income

        self._summary_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        self._summary_total_label.setText(
            f"范围 {start} ~ {end} | "
            f"总支出 {_fmt_amount(total_expense)} 元 | "
            f"总收入 {_fmt_amount(total_income)} 元 | "
            f"净额 {_fmt_amount(total_income - total_expense)} 元"
        )

    # ═══════════════════════════════════════════════════════════
    # 映射表 tab
    # ═══════════════════════════════════════════════════════════

    def _build_mapping_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 顶部操作栏：类型筛选 + 搜索 + 新增/删除
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("类型:"))
        self._mapping_type_filter = QComboBox()
        self._mapping_type_filter.addItems(["全部", "固定", "可变", "跳过", "退款"])
        self._mapping_type_filter.currentIndexChanged.connect(self._load_mapping_table)
        top_row.addWidget(self._mapping_type_filter)

        top_row.addWidget(QLabel("搜索:"))
        self._mapping_search = QLineEdit()
        self._mapping_search.setPlaceholderText("过滤 键/名称/大类")
        self._mapping_search.textChanged.connect(self._load_mapping_table)
        top_row.addWidget(self._mapping_search, 1)

        btn_add = QPushButton("➕ 新增")
        btn_add.clicked.connect(self._on_mapping_add_row)
        top_row.addWidget(btn_add)
        btn_del = QPushButton("🗑 删除选中")
        btn_del.clicked.connect(self._on_mapping_delete_selected)
        top_row.addWidget(btn_del)
        btn_refresh = QPushButton("⟳ 刷新")
        btn_refresh.clicked.connect(self._load_mapping_table)
        top_row.addWidget(btn_refresh)
        layout.addLayout(top_row)

        # 大类配置：板块配置与映射/审核共用，支持新增、重命名和删除。
        category_row = QHBoxLayout()
        category_row.addWidget(QLabel("大类配置:"))
        self._category_manage_sector = QComboBox()
        self._category_manage_sector.addItems(_SECTORS)
        self._category_manage_sector.setMinimumWidth(86)
        self._category_manage_sector.currentIndexChanged.connect(
            self._refresh_category_manage_options
        )
        category_row.addWidget(self._category_manage_sector)
        self._category_manage_combo = QComboBox()
        self._category_manage_combo.setMinimumWidth(220)
        self._category_manage_combo.currentIndexChanged.connect(
            self._on_category_manage_selected
        )
        category_row.addWidget(self._category_manage_combo, 1)
        btn_category_save = QPushButton("保存大类")
        btn_category_save.setMinimumHeight(32)
        btn_category_save.clicked.connect(self._on_category_manage_save)
        category_row.addWidget(btn_category_save)
        btn_category_add = QPushButton("新增大类")
        btn_category_add.setMinimumHeight(32)
        btn_category_add.clicked.connect(self._on_category_manage_add)
        category_row.addWidget(btn_category_add)
        btn_category_delete = QPushButton("删除大类")
        btn_category_delete.setMinimumHeight(32)
        btn_category_delete.setProperty("danger", True)
        btn_category_delete.clicked.connect(self._on_category_manage_delete)
        category_row.addWidget(btn_category_delete)
        layout.addLayout(category_row)

        # 映射表格（7 列：类型 | 键/模式/关键词 | 名称 | 大类 | 需确认 | 触发次数 | 操作）
        self._mapping_table = QTableWidget(0, 7)
        self._mapping_table.setHorizontalHeaderLabels([
            "类型", "键/模式/关键词", "名称/默认名称", "大类/默认大类", "需确认", "触发次数", "操作",
        ])
        header = self._mapping_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._mapping_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._mapping_table.setItemDelegate(_MappingDelegate(self, self._mapping_table))
        layout.addWidget(self._mapping_table, 1)

        # 底部统计
        self._mapping_status_label = QLabel("")
        layout.addWidget(self._mapping_status_label)

        self._refresh_category_manage_options()

        return w

    def _mapping_completion_values(self, column: int) -> list[str]:
        return self._mapping_completion_cache.get(column, [])

    def _refresh_category_manage_options(self, _index: int = -1) -> None:
        sector = self._category_manage_sector.currentText()
        categories = self._store.get_categories_for_sector(sector)
        current = self._category_manage_combo.currentText()
        self._category_manage_combo.blockSignals(True)
        self._category_manage_combo.clear()
        self._category_manage_combo.addItems(categories)
        _set_combo_completion(self._category_manage_combo, categories)
        if current in categories:
            self._category_manage_combo.setCurrentText(current)
        self._category_manage_combo.setProperty("original_category", current if current in categories else "")
        self._category_manage_combo.blockSignals(False)

    def _on_category_manage_selected(self, _index: int = -1) -> None:
        self._category_manage_combo.setProperty(
            "original_category", self._category_manage_combo.currentText().strip()
        )

    def _on_category_manage_save(self) -> None:
        sector = self._category_manage_sector.currentText()
        new_category = self._category_manage_combo.currentText().strip()
        old_category = self._category_manage_combo.property("original_category") or ""
        if not new_category:
            QMessageBox.warning(self, "保存失败", "大类名称不能为空")
            return
        try:
            if old_category and old_category != new_category:
                self._store.rename_category(sector, old_category, new_category)
            else:
                self._store.ensure_category(sector, new_category)
            self._refresh_category_manage_options()
            self._load_mapping_table()
            self._refresh_monthly_month_options()
            self._refresh_summary_month_options()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _on_category_manage_add(self) -> None:
        sector = self._category_manage_sector.currentText()
        category = self._category_manage_combo.currentText().strip()
        if not category:
            QMessageBox.warning(self, "新增失败", "大类名称不能为空")
            return
        self._store.ensure_category(sector, category)
        self._refresh_category_manage_options()
        self._category_manage_combo.setCurrentText(category)
        self._load_review_data()

    def _on_category_manage_delete(self) -> None:
        sector = self._category_manage_sector.currentText()
        category = self._category_manage_combo.currentText().strip()
        if not category:
            return
        usage = self._store.get_category_usage(category)
        reply = QMessageBox.question(
            self,
            "删除大类",
            f"删除板块“{sector}”中的大类“{category}”配置？\n\n"
            f"历史交易 {usage['transactions']} 条会保留原分类；"
            f"依赖它的固定映射 {usage['fixed_mappings']} 条、"
            f"可变映射 {usage['variable_mappings']} 条会一并删除，"
            "以后遇到相同流水时将重新进入审核。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._store.delete_category(sector, category)
        self._refresh_category_manage_options()
        self._load_review_data()

    def _load_mapping_table(self) -> None:
        """加载映射表到可编辑表格"""
        type_filter = self._mapping_type_filter.currentText() if hasattr(self, "_mapping_type_filter") else "全部"
        keyword = self._mapping_search.text().strip().lower() if hasattr(self, "_mapping_search") else ""
        mapping = self._store.get_mapping()
        hit_counts = self._store.get_mapping_hit_counts()
        self._mapping_completion_cache = {
            1: sorted(set(mapping["fixed"]) | set(mapping["variable"]) | set(mapping["skip_patterns"]) | set(mapping["refund_keywords"])),
            2: sorted({info["name"] for info in mapping["fixed"].values()} | {info.get("default_name", "") for info in mapping["variable"].values()}),
            3: sorted({info["category"] for info in mapping["fixed"].values()} | {info.get("default_category", "") for info in mapping["variable"].values()}),
        }
        completer = QCompleter(
            sorted(set(self._mapping_completion_cache[1] + self._mapping_completion_cache[2] + self._mapping_completion_cache[3])),
            self._mapping_search,
        )
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._mapping_search.setCompleter(completer)

        rows: list[tuple] = []
        # 固定映射
        if type_filter in ("全部", "固定"):
            for mk, info in sorted(mapping["fixed"].items()):
                rows.append(("固定", mk, info["name"], info["category"], None))
        # 可变映射
        if type_filter in ("全部", "可变"):
            for mk, info in sorted(mapping["variable"].items()):
                rows.append(("可变", mk, info.get("default_name", ""),
                             info.get("default_category", ""), info.get("need_confirm", True)))
        # 跳过模式
        if type_filter in ("全部", "跳过"):
            for p in sorted(mapping["skip_patterns"]):
                rows.append(("跳过", p, "", "", None))
        # 退款关键词
        if type_filter in ("全部", "退款"):
            for k in sorted(mapping["refund_keywords"]):
                rows.append(("退款", k, "", "", None))

        # 关键词过滤
        if keyword:
            rows = [r for r in rows if keyword in r[1].lower() or keyword in r[2].lower() or keyword in r[3].lower()]

        self._mapping_table.setRowCount(len(rows))
        for i, (typ, key, name, cat, need_confirm) in enumerate(rows):
            # 类型（只读）
            type_item = QTableWidgetItem(typ)
            type_item.setData(Qt.ItemDataRole.UserRole, (typ, key))
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._mapping_table.setItem(i, 0, type_item)

            # 键/模式/关键词（可编辑）
            key_item = QTableWidgetItem(key)
            self._mapping_table.setItem(i, 1, key_item)

            # 名称（可编辑；跳过/退款类型禁用）
            name_item = QTableWidgetItem(name)
            if typ in ("跳过", "退款"):
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                name_item.setBackground(QColor("#30353f"))
                name_item.setForeground(QColor("#a7afbb"))
            self._mapping_table.setItem(i, 2, name_item)

            # 大类（可编辑；跳过/退款类型禁用）
            cat_item = QTableWidgetItem(cat)
            if typ in ("跳过", "退款"):
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                cat_item.setBackground(QColor("#30353f"))
                cat_item.setForeground(QColor("#a7afbb"))
            self._mapping_table.setItem(i, 3, cat_item)

            # 需确认（仅可变类型有效）
            if typ == "可变":
                check = QCheckBox()
                check.setChecked(bool(need_confirm))
                check_container = QWidget()
                check_layout = QHBoxLayout(check_container)
                check_layout.addWidget(check)
                check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                check_layout.setContentsMargins(0, 0, 0, 0)
                self._mapping_table.setCellWidget(i, 4, check_container)
            else:
                na_item = QTableWidgetItem("—")
                na_item.setFlags(na_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                na_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._mapping_table.setItem(i, 4, na_item)

            hit_item = QTableWidgetItem(str(hit_counts.get(key, 0)))
            hit_item.setFlags(hit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            hit_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._mapping_table.setItem(i, 5, hit_item)

            # 操作列：保存 + 删除按钮
            op_container = QWidget()
            op_layout = QHBoxLayout(op_container)
            op_layout.setContentsMargins(2, 0, 2, 0)
            op_layout.setSpacing(2)
            btn_save = QPushButton("💾")
            btn_save.setProperty("compact", True)
            btn_save.setFixedSize(34, 30)
            btn_save.setToolTip("保存此行")
            btn_save.clicked.connect(lambda _, row=i: self._on_mapping_save_row(row))
            btn_del = QPushButton("🗑")
            btn_del.setProperty("compact", True)
            btn_del.setFixedSize(34, 30)
            btn_del.setToolTip("删除此行")
            btn_del.clicked.connect(lambda _, row=i: self._on_mapping_delete_row(row))
            op_layout.addWidget(btn_save)
            op_layout.addWidget(btn_del)
            self._mapping_table.setCellWidget(i, 6, op_container)
            self._mapping_table.setRowHeight(i, 36)

        self._mapping_status_label.setText(
            f"固定 {len(mapping['fixed'])} | 可变 {len(mapping['variable'])} | "
            f"跳过 {len(mapping['skip_patterns'])} | 退款 {len(mapping['refund_keywords'])}"
        )
        self._mapping_status_label.setToolTip("映射增删改只影响后续导入；已有交易保留当时的分类，避免历史统计被重写。")

    def _on_mapping_add_row(self) -> None:
        """新增空行供用户填写"""
        row = 0
        self._mapping_table.insertRow(row)
        # 类型用 QComboBox 让用户选
        type_combo = QComboBox()
        type_combo.addItems(["固定", "可变", "跳过", "退款"])
        type_combo.setMinimumHeight(30)
        type_combo.view().setMinimumWidth(120)
        self._mapping_table.setCellWidget(row, 0, type_combo)
        # 其余列空可编辑
        for col in range(1, 4):
            self._mapping_table.setItem(row, col, QTableWidgetItem(""))
        # 需确认默认勾选（可变类型才有效）
        check = QCheckBox()
        check.setChecked(True)
        check_container = QWidget()
        check_layout = QHBoxLayout(check_container)
        check_layout.addWidget(check)
        check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        check_layout.setContentsMargins(0, 0, 0, 0)
        self._mapping_table.setCellWidget(row, 4, check_container)
        hit_item = QTableWidgetItem("0")
        hit_item.setFlags(hit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        hit_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mapping_table.setItem(row, 5, hit_item)
        # 操作按钮
        op_container = QWidget()
        op_layout = QHBoxLayout(op_container)
        op_layout.setContentsMargins(2, 0, 2, 0)
        op_layout.setSpacing(2)
        btn_save = QPushButton("💾")
        btn_save.setProperty("compact", True)
        btn_save.setFixedSize(34, 30)
        btn_save.setToolTip("保存此行")
        btn_save.clicked.connect(lambda _, r=row: self._on_mapping_save_row(r))
        btn_del = QPushButton("🗑")
        btn_del.setProperty("compact", True)
        btn_del.setFixedSize(34, 30)
        btn_del.setToolTip("删除此行")
        btn_del.clicked.connect(lambda _, r=row: self._on_mapping_delete_row(r))
        op_layout.addWidget(btn_save)
        op_layout.addWidget(btn_del)
        self._mapping_table.setCellWidget(row, 6, op_container)
        self._mapping_table.setRowHeight(row, 36)
        # 新行置顶并确保编辑器可见
        self._mapping_table.scrollToItem(self._mapping_table.item(row, 1))

    def _on_mapping_save_row(self, row: int) -> None:
        """保存某行到 DB"""
        # 读取类型（可能是 QComboBox 新增行或 QTableWidgetItem 已有行）
        type_widget = self._mapping_table.cellWidget(row, 0)
        if type_widget and isinstance(type_widget, QComboBox):
            typ = type_widget.currentText()
        else:
            type_item = self._mapping_table.item(row, 0)
            typ = type_item.text() if type_item else ""

        key = self._mapping_table.item(row, 1).text().strip() if self._mapping_table.item(row, 1) else ""
        name = self._mapping_table.item(row, 2).text().strip() if self._mapping_table.item(row, 2) else ""
        cat = self._mapping_table.item(row, 3).text().strip() if self._mapping_table.item(row, 3) else ""

        # 需确认 checkbox
        need_confirm = True
        check_container = self._mapping_table.cellWidget(row, 4)
        if check_container:
            checkboxes = check_container.findChildren(QCheckBox)
            if checkboxes:
                need_confirm = checkboxes[0].isChecked()

        if not key:
            QMessageBox.warning(self, "保存失败", "键/模式/关键词不能为空")
            return

        try:
            # 双击修改键或类型时先移除旧映射，避免旧条目继续影响后续分类。
            type_item = self._mapping_table.item(row, 0)
            original = type_item.data(Qt.ItemDataRole.UserRole) if type_item else None
            if original and tuple(original) != (typ, key):
                old_typ, old_key = original
                if old_typ == "固定":
                    self._store.delete_mapping_fixed(old_key)
                elif old_typ == "可变":
                    self._store.delete_mapping_variable(old_key)
                elif old_typ == "跳过":
                    self._store.delete_skip_pattern(old_key)
                elif old_typ == "退款":
                    self._store.delete_refund_keyword(old_key)
            if typ == "固定":
                if not name or not cat:
                    QMessageBox.warning(self, "保存失败", "固定映射需填写名称和大类")
                    return
                self._store.update_mapping_fixed(key, name, cat)
                self._store.ensure_category("出", cat)
            elif typ == "可变":
                self._store.update_mapping_variable(key, "", name, cat, need_confirm)
                if cat:
                    self._store.ensure_category("出", cat)
            elif typ == "跳过":
                self._store.add_skip_pattern(key)
            elif typ == "退款":
                self._store.add_refund_keyword(key)
            else:
                QMessageBox.warning(self, "保存失败", f"未知类型: {typ}")
                return
            QMessageBox.information(self, "保存成功", f"已保存: {typ} → {key}")
            self._load_mapping_table()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _on_mapping_delete_row(self, row: int) -> None:
        """删除某行"""
        type_item = self._mapping_table.item(row, 0)
        type_widget = self._mapping_table.cellWidget(row, 0)
        if type_widget and isinstance(type_widget, QComboBox):
            typ = type_widget.currentText()
        elif type_item:
            typ = type_item.text()
        else:
            typ = ""

        key_item = self._mapping_table.item(row, 1)
        key = key_item.text().strip() if key_item else ""

        if not key:
            # 新增的空行直接移除
            self._mapping_table.removeRow(row)
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"将删除 {typ} 映射: {key}\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            if typ == "固定":
                self._store.delete_mapping_fixed(key)
            elif typ == "可变":
                self._store.delete_mapping_variable(key)
            elif typ == "跳过":
                self._store.delete_skip_pattern(key)
            elif typ == "退款":
                self._store.delete_refund_keyword(key)
            self._load_mapping_table()
        except Exception as e:
            QMessageBox.critical(self, "删除失败", str(e))

    def _on_mapping_delete_selected(self) -> None:
        """批量删除选中行"""
        rows = sorted({idx.row() for idx in self._mapping_table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的行")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"将删除 {len(rows)} 行映射，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # 从后往前删除避免行号错位
        for row in rows:
            self._on_mapping_delete_row(row)

    # ═══════════════════════════════════════════════════════════
    # 导出 tab
    # ═══════════════════════════════════════════════════════════

    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # 月份选择
        month_row = QHBoxLayout()
        month_row.addWidget(QLabel("月份:"))
        self._export_month_combo = QComboBox()
        month_row.addWidget(self._export_month_combo)
        btn_refresh = QPushButton("⟳ 刷新")
        btn_refresh.clicked.connect(self._refresh_export_months)
        month_row.addWidget(btn_refresh)
        month_row.addStretch()
        layout.addLayout(month_row)

        # 路径显示
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Obsidian 目录:"))
        cfg = get_component_config("accounting") or {}
        self._export_path_label = QLabel(cfg.get("accounting_dir", "未配置"))
        self._export_path_label.setStyleSheet("color: #a7afbb;")
        path_row.addWidget(self._export_path_label, 1)
        layout.addLayout(path_row)

        # 模式选择
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式:"))
        self._export_mode_combo = QComboBox()
        self._export_mode_combo.addItems(["append（追加到已有 md）"])
        self._export_mode_combo.setEnabled(True)  # v1 只支持 append
        mode_row.addWidget(self._export_mode_combo)
        self._export_git_check = QCheckBox("导出后 git commit")
        self._export_git_check.setChecked(False)
        mode_row.addWidget(self._export_git_check)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # 导出按钮
        btn_row = QHBoxLayout()
        self._export_status_label = QLabel("")
        btn_row.addWidget(self._export_status_label, 1)
        self._btn_export = QPushButton("📤 导出")
        self._btn_export.clicked.connect(self._on_export)
        btn_row.addWidget(self._btn_export)
        layout.addLayout(btn_row)

        # 预览区
        layout.addWidget(QLabel("已确认交易预览:"))
        self._export_preview_table = QTableWidget(0, 5)
        self._export_preview_table.setHorizontalHeaderLabels([
            "日期", "说明", "金额", "方向", "大类",
        ])
        self._export_preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._export_preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._export_preview_table, 1)

        # 月份选择变化时刷新预览
        self._export_month_combo.currentIndexChanged.connect(self._refresh_export_preview)

        return w

    def _refresh_export_months(self) -> None:
        """刷新导出月份选项"""
        current = self._export_month_combo.currentText()
        months = self._store.get_months()
        self._export_month_combo.blockSignals(True)
        self._export_month_combo.clear()
        for mk in months:
            self._export_month_combo.addItem(mk)
        if current and self._export_month_combo.findText(current) >= 0:
            self._export_month_combo.setCurrentText(current)
        elif months:
            self._export_month_combo.setCurrentIndex(0)
        self._export_month_combo.blockSignals(False)
        self._refresh_export_preview()

    def _refresh_export_preview(self) -> None:
        """刷新导出预览"""
        month_key = self._export_month_combo.currentText()
        if not month_key:
            return
        confirmed = self._store.get_month_confirmed(month_key)
        self._export_preview_table.setRowCount(len(confirmed))
        total_expense = 0.0
        total_income = 0.0
        for i, tx in enumerate(confirmed):
            items = [
                tx["trade_time"][:16],
                tx["description"],
                _fmt_amount(tx["amount"]),
                tx["direction"],
                tx["category"],
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._export_preview_table.setItem(i, col, item)
            if tx["direction"] == "收入":
                total_income += tx["amount"]
            else:
                total_expense += tx["amount"]
        self._export_status_label.setText(
            f"{month_key}: {len(confirmed)} 条已确认 | "
            f"支出 {_fmt_amount(total_expense)} | 收入 {_fmt_amount(total_income)}"
        )

    def _on_export(self) -> None:
        """导出到 Obsidian md"""
        month_key = self._export_month_combo.currentText()
        if not month_key:
            QMessageBox.warning(self, "提示", "请先选择月份")
            return

        cfg = get_component_config("accounting") or {}
        md_dir = Path(cfg.get("accounting_dir", ""))
        if not md_dir.exists():
            QMessageBox.warning(self, "导出失败", f"Obsidian 目录不存在: {md_dir}")
            return

        try:
            md_path = export_month(month_key, self._store, md_dir, mode="append")
            QMessageBox.information(
                self, "导出成功",
                f"已导出 {month_key} 到:\n{md_path}",
            )
            self._export_status_label.setText(f"上次导出: {month_key} → {md_path.name}")

            # git commit
            if self._export_git_check.isChecked():
                try:
                    git_commit([month_key], md_dir)
                    QMessageBox.information(self, "Git Commit", f"已提交 {month_key} 到 git")
                except Exception as e:
                    QMessageBox.warning(self, "Git Commit 失败", str(e))
        except FileNotFoundError as e:
            QMessageBox.warning(self, "导出失败", str(e))
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ═══════════════════════════════════════════════════════════
    # PanelBase 钩子
    # ═══════════════════════════════════════════════════════════

    def on_show(self) -> None:
        # 方案A：首次加载标志——避免每次切换面板都重新加载
        # 后续切换进来直接显示上次的数据，用户点刷新按钮（on_refresh）才重新加载
        if self._data_loaded:
            return
        self._data_loaded = True
        # 刷新各 tab 数据（审核 tab 是异步加载，不会阻塞 UI）
        self._load_review_data()
        self._refresh_monthly_month_options()
        self._refresh_summary_month_options()
        self._refresh_export_months()
        self._load_mapping_table()
        self._refresh_category_manage_options()

    def on_refresh(self) -> None:
        """用户主动点刷新：强制重新加载所有 tab 数据"""
        self._load_review_data()
        self._refresh_monthly_month_options()
        self._refresh_summary_month_options()
        self._refresh_export_months()
        self._load_mapping_table()
        self._refresh_category_manage_options()
