"""Accounting 面板的辅助组件

从 accounting.py 拆出，避免主文件过大。
包含：
- _fmt_amount / _set_combo_completion：格式化与补全辅助函数
- _AccountingCalendar：带每日净流水标注的日历组件
- _MappingDelegate：映射表编辑器 delegate
- _ReviewDataLoader：后台加载审核数据的 QThread
"""
from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QCompleter,
    QLineEdit,
    QStyledItemDelegate,
)

from workspace.accounting.store import AccountingStore


def _fmt_amount(amount: float) -> str:
    """格式化金额：整数不加小数点"""
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}"


def _set_combo_completion(combo: QComboBox, values: list[str]) -> None:
    """为可编辑下拉框提供包含匹配补全，并保持下拉宽度可读。"""
    combo.setMinimumHeight(34)
    combo.setStyleSheet(
        "QComboBox { padding: 2px 6px; min-height: 30px; }"
        "QComboBox QLineEdit { padding: 1px 4px; min-height: 26px; }"
    )
    combo.setEditable(True)
    line_edit = combo.lineEdit()
    if line_edit is None:
        return
    line_edit.setMinimumHeight(26)
    line_edit.setContentsMargins(0, 0, 0, 0)
    completer = QCompleter(sorted({v for v in values if v}), line_edit)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    line_edit.setCompleter(completer)
    combo.view().setMinimumWidth(220)


class _AccountingCalendar(QCalendarWidget):
    """带每日净流水标注的统一深色日历。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._day_totals: dict[QDate, float] = {}
        self.setGridVisible(True)
        self.setNavigationBarVisible(True)
        self.setMinimumHeight(270)

    def set_day_totals(self, totals: dict[QDate, float]) -> None:
        self._day_totals = totals
        self._refresh_day_formats()
        self.updateCells()

    def _refresh_day_formats(self) -> None:
        if not self._day_totals:
            return
        for date, total in self._day_totals.items():
            fmt = QTextCharFormat()
            if total > 0:
                fmt.setBackground(QColor(50, 130, 95, 70))
            elif total < 0:
                fmt.setBackground(QColor(170, 70, 80, 70))
            self.setDateTextFormat(date, fmt)

    def paintCell(self, painter: QPainter, rect, date: QDate) -> None:
        super().paintCell(painter, rect, date)
        total = self._day_totals.get(date)
        if total is None or abs(total) < 0.005:
            return
        painter.save()
        color = QColor("#68d6aa") if total > 0 else QColor("#ff8b94")
        painter.setPen(color)
        font = QFont(painter.font())
        font.setPointSize(max(7, font.pointSize() - 2))
        painter.setFont(font)
        text = f"{'+' if total > 0 else ''}{_fmt_amount(total)}"
        painter.drawText(rect.adjusted(1, rect.height() // 2, -1, -2),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, text)
        painter.restore()


class _MappingDelegate(QStyledItemDelegate):
    """映射表编辑器：统一高度并为文本编辑提供补全。"""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setMinimumHeight(30)
        values = self._panel._mapping_completion_values(index.column())
        if values:
            completer = QCompleter(values, editor)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            editor.setCompleter(completer)
        return editor


class _ReviewDataLoader(QThread):
    """后台加载审核数据（交易列表 + 预取 descriptions），避免阻塞主线程

    解决两个性能问题：
    1. 主线程同步 DB 查询阻塞 UI（方案C：异步加载）
    2. 每行交易单独查 descriptions 的 N+1 问题（方案B：一次性预取）

    SQLite 连接在 AccountingStore 中用 check_same_thread=False 创建，
    且这里只做读操作，可安全在 QThread 中使用。
    """
    data_ready = Signal(dict)
    error = Signal(str)

    def __init__(self, store: "AccountingStore", month, mark_filter: str, source_filter: str):
        super().__init__()
        self._store = store
        self._month = month
        self._mark_filter = mark_filter
        self._source_filter = source_filter

    def run(self) -> None:
        try:
            # 1. 月份列表
            months = self._store.get_months()
            # 2. 交易查询
            if self._month:
                txs = self._store.query(month=self._month, include_skip=True)
            else:
                txs = self._store.query(include_skip=True)
            # 3. 过滤
            if self._mark_filter != "全部":
                txs = [t for t in txs if t["mark"] == self._mark_filter]
            if self._source_filter != "全部":
                txs = [t for t in txs if t["source"] == self._source_filter]
            # 4. 排序：待审核在前，时间正序
            txs.sort(key=lambda t: (
                0 if t["confirm_status"] != "confirmed" else 1,
                t["trade_time"],
            ))
            # 5. 一次性预取所有 category→descriptions（修复 N+1：512次查询→1次批量）
            all_categories = {t["category"] for t in txs if t["category"]}
            descriptions_map = {
                cat: self._store.get_descriptions_for_category(cat)
                for cat in all_categories
            }
            # 6. config（板块→大类映射）
            config = self._store.get_config()

            self.data_ready.emit({
                "months": months,
                "txs": txs,
                "descriptions_map": descriptions_map,
                "config": config,
            })
        except Exception as e:
            self.error.emit(str(e))
