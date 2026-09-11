"""stream_review_dialog.py — R1 流式审核窗口（ticket F）

用户触发 AI 预测后弹出的**模态**审核窗口：
- 模态 = 阻塞主窗口（不能做其它操作、不能切换 tab），必须等 AI 跑完或主动中断
- 预测线程逐条 emit 预测结果，本窗口实时插入一行（AI 与用户流式并行）
- 每行展示：(a) 基础信息（类型/文件名/大小/修改时间）+ (b) AI 预测（分类+置信度）
- 每行操作：[分类下拉（默认=AI预测，可改）] + [接受] + [跳过]
  - 接受（未改下拉）= accept；接受（改了下拉）= adjust（调整）
  - 用户处理完一项，该行立即从待审列表移除，不阻塞后续预测项
  - 跳过 = 不选任何分类（标记 skipped）
- [中断] 按钮：置位预测线程中断标志（request_stop），已产出的预测照常展示
- [关闭] 按钮：仅在预测结束（或已中断）后可用

决策通过 decision_made 信号发回主窗口回写：
    decision_made(filename, action, category)
    action ∈ {"accept", "adjust", "skip"}；skip 时 category 为 ""
"""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from type_descriptor import describe_file_type

# 置信度配色（与主表格一致）
COLOR_HIGH = "#5cb88a"    # 高置信度 绿
COLOR_MID = "#e8c268"     # 中等 黄
COLOR_LOW = "#8a918d"     # 低/未知 灰

CONFIDENCE_HIGH = 0.8
CONFIDENCE_LOW = 0.5


def _human_readable_size(size: int) -> str:
    """字节数转可读格式（与 classifier_gui.human_readable_size 一致，局部定义避免循环导入）"""
    if not size or size < 0:
        return "未知"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _format_time(timestamp: float) -> str:
    """时间戳格式化（与 classifier_gui 一致）"""
    if not timestamp:
        return "未知"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "未知"


def _confidence_color(conf: float) -> QColor:
    if conf >= CONFIDENCE_HIGH:
        return QColor(COLOR_HIGH)
    if conf >= CONFIDENCE_LOW:
        return QColor(COLOR_MID)
    return QColor(COLOR_LOW)


# 表格列号
COL_TYPE = 0
COL_NAME = 1
COL_SIZE = 2
COL_MTIME = 3
COL_PRED = 4
COL_CONF = 5
COL_CATEGORY = 6
COL_ACTION = 7

_HEADERS = ["类型", "文件名", "大小", "修改时间", "AI预测", "置信度", "调整分类", "操作"]


class StreamReviewDialog(QDialog):
    """R1 流式审核窗口（模态）

    Args:
        category_names: 分类名列表（下拉选项）
        file_info_map: filename → 文件元信息 {"filename","size","modified","is_dir","path"}

    Signals:
        decision_made(filename, action, category): 用户对某项做出决策
        stop_requested(): 用户点击「中断」
    """
    decision_made = Signal(str, str, str)
    stop_requested = Signal()

    def __init__(self, category_names: list, file_info_map: dict, parent=None):
        super().__init__(parent)
        self.category_names = list(category_names)
        self.file_info_map = file_info_map or {}

        # 状态
        self._finished = False       # 预测线程是否已结束
        self._stopped = False        # 是否被中断
        self._pending = {}           # filename -> 行号无关的待审集合（用 filename 索引）
        self._received = 0
        self._processed = 0
        self._accepted = 0
        self._skipped = 0

        self.setWindowTitle("AI 分类流式审核")
        self.setModal(True)
        self.resize(1000, 560)
        # 模态阻塞主窗口；仅允许通过「中断」/「关闭」按钮退出
        self.setWindowFlag(Qt.WindowCloseButtonHint, True)

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── 顶部：进度 + 中断 ──
        top = QHBoxLayout()
        top.setSpacing(8)
        self.status_label = QLabel("AI 预测进行中...")
        top.addWidget(self.status_label, 1)

        self.stop_btn = QPushButton("中断")
        self.stop_btn.setToolTip("停止 AI 继续预测（已产出的结果仍会展示，可继续处理）")
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        top.addWidget(self.stop_btn)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setEnabled(False)  # 预测结束后才可用
        self.close_btn.clicked.connect(self.accept)
        top.addWidget(self.close_btn)
        root.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 初始不定长（逐条流式，总数渐进已知）
        root.addWidget(self.progress_bar)

        # ── 中部：待审列表 ──
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_TYPE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_MTIME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_PRED, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_CONF, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_CATEGORY, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        # ── 底部：计数 ──
        bottom = QHBoxLayout()
        self.count_label = QLabel(self._count_text())
        bottom.addWidget(self.count_label, 1)

        hint = QLabel("提示：AI 与你的审核并行——处理完一项会自动移除，无需等待全部预测完成。")
        bottom.addWidget(hint)
        root.addLayout(bottom)

    def _count_text(self) -> str:
        return (f"已接收 {self._received}　待处理 {len(self._pending)}　"
                f"已接受 {self._accepted}　已跳过 {self._skipped}")

    # ── 供主窗口连接的槽（由预测线程信号驱动） ──

    def add_prediction(self, pred: dict):
        """插入一条预测到待审列表（预测线程逐条 emit 时调用）"""
        fname = pred.get("filename", "")
        if not fname or fname in self._pending:
            return
        self._received += 1

        info = self.file_info_map.get(fname, {})
        is_dir = pred.get("is_folder", False) or info.get("is_dir", False)

        row = self.table.rowCount()
        self.table.insertRow(row)

        # 类型
        type_text = "文件夹" if is_dir else describe_file_type(fname)
        self._set_text(row, COL_TYPE, type_text)
        # 文件名
        name_item = QTableWidgetItem(fname)
        name_item.setData(Qt.UserRole, fname)
        # ticket J：冲突项高亮（相似文件名被分到不同类，供重点核查）
        if pred.get("conflict"):
            name_item.setForeground(QColor(COLOR_MID))
            tip = (f"⚠ 相似文件名冲突：与「{pred.get('conflict_with', '')}」高度相似"
                   f"但分类不同，请重点核查")
            if pred.get("reason"):
                tip += f"\nLLM 理由：{pred['reason']}"
            name_item.setToolTip(tip)
        self.table.setItem(row, COL_NAME, name_item)
        # 大小 / 修改时间
        self._set_text(row, COL_SIZE,
                       "—" if is_dir else _human_readable_size(info.get("size", 0)))
        self._set_text(row, COL_MTIME, _format_time(info.get("modified", 0)))
        # AI 预测
        category = pred.get("category", "")
        conf = float(pred.get("confidence", 0.0))
        self._set_text(row, COL_PRED, category or "未知")
        # ticket J：冲突/低置信场景下 LLM 附带的理由，悬停可见
        if pred.get("reason"):
            pred_item = self.table.item(row, COL_PRED)
            if pred_item is not None:
                pred_item.setToolTip(f"LLM 理由：{pred['reason']}")
        conf_item = QTableWidgetItem(f"{conf:.0%}")
        conf_item.setForeground(_confidence_color(conf))
        conf_item.setData(Qt.UserRole, conf)
        self.table.setItem(row, COL_CONF, conf_item)

        # 调整分类下拉（默认 = AI 预测；若预测不在分类列表也保留显示）
        combo = QComboBox()
        options = list(self.category_names)
        if category and category not in options:
            options.insert(0, category)
        combo.addItems(options if options else [category or "未知"])
        if category:
            combo.setCurrentText(category)
        self.table.setCellWidget(row, COL_CATEGORY, combo)

        # 操作按钮
        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(2, 2, 2, 2)
        action_layout.setSpacing(4)
        accept_btn = QPushButton("接受")
        accept_btn.setToolTip("采用当前下拉框中的分类（可先调整再接受）")
        accept_btn.clicked.connect(lambda _=False, f=fname: self._on_accept(f))
        skip_btn = QPushButton("跳过")
        skip_btn.setToolTip("不分配分类，标记为跳过")
        skip_btn.clicked.connect(lambda _=False, f=fname: self._on_skip(f))
        action_layout.addWidget(accept_btn)
        action_layout.addWidget(skip_btn)
        self.table.setCellWidget(row, COL_ACTION, action_widget)

        self._pending[fname] = {"row": row, "predicted": category, "combo": combo}
        self.count_label.setText(self._count_text())

    def set_progress(self, current: int, total: int, message: str):
        """更新进度条与状态（来自预测线程 progress 信号）"""
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        self.status_label.setText(message)

    def on_all_finished(self, stopped: bool = False):
        """预测线程结束（正常完成或中断）"""
        self._finished = True
        self._stopped = stopped
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        if stopped:
            msg = f"已中断。待处理 {len(self._pending)} 项，可继续处理或直接关闭。"
        else:
            msg = f"预测完成。待处理 {len(self._pending)} 项。"
        self.status_label.setText(msg)
        # 若无待处理项，自动关闭（无需用户再点）
        if not self._pending:
            self.accept()

    def on_error(self, message: str):
        """预测出错"""
        self._finished = True
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.status_label.setText(f"预测出错: {message}")

    # ── 行内操作 ──

    def _on_accept(self, filename: str):
        entry = self._pending.get(filename)
        if not entry:
            return
        combo = entry["combo"]
        chosen = combo.currentText().strip() if combo else entry["predicted"]
        action = "accept" if chosen == entry["predicted"] else "adjust"
        self._remove_row(filename)
        self._accepted += 1
        self.count_label.setText(self._count_text())
        self.decision_made.emit(filename, action, chosen)
        self._maybe_auto_close()

    def _on_skip(self, filename: str):
        if filename not in self._pending:
            return
        self._remove_row(filename)
        self._skipped += 1
        self.count_label.setText(self._count_text())
        self.decision_made.emit(filename, "skip", "")
        self._maybe_auto_close()

    def _remove_row(self, filename: str):
        entry = self._pending.pop(filename, None)
        if entry is None:
            return
        # 通过 UserRole 定位真实行号（行可能因先前删除而变化）
        for r in range(self.table.rowCount()):
            item = self.table.item(r, COL_NAME)
            if item and item.data(Qt.UserRole) == filename:
                self.table.removeRow(r)
                break

    def _maybe_auto_close(self):
        """预测已结束且无待处理项 → 自动关闭"""
        if self._finished and not self._pending:
            self.accept()

    def _on_stop_clicked(self):
        self.stop_btn.setEnabled(False)
        self.status_label.setText("正在中断 AI 预测...")
        self.stop_requested.emit()

    def _set_text(self, row: int, col: int, text: str):
        item = QTableWidgetItem(text)
        self.table.setItem(row, col, item)

    # ── 关闭保护：预测未结束时，点右上角 X 视为请求中断而非直接关闭 ──

    def closeEvent(self, event):
        if not self._finished:
            # 尚未结束：转为中断请求，阻止直接关闭（避免孤立预测线程）
            self.stop_requested.emit()
            self.status_label.setText("正在中断 AI 预测，请稍候...")
            event.ignore()
            return
        super().closeEvent(event)

    def keyPressEvent(self, event):
        # 预测未结束时禁用 Esc 关闭（等价于关闭保护）
        if event.key() == Qt.Key_Escape and not self._finished:
            self.stop_requested.emit()
            self.status_label.setText("正在中断 AI 预测，请稍候...")
            return
        super().keyPressEvent(event)
