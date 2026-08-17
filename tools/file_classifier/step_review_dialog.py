"""step_review_dialog.py — R2 专门审核窗口（ticket G+H）

R1（流式模态窗口）定好每项的分类后，由「审核」按钮打开本窗口做**逐类最终核对**：

- 分步骤审核：一次只展示**一个分类目标**涉及的所有条目
- 审核操作：可勾选「跳过」排除某些条目，但**不能更改其分类目标**（分类在 R1 已锁定）
- 暂存分类（path 为空）同样进入审核（ticket G），目标显示为"暂存"
- 审核结束时由主窗口处理结果：
    - 未跳过的条目 → 标"已审"（仅 R2 产生已审，Q9）
    - 跳过的条目 → 自动清除分类（无所属，Q8；重分回主窗口拖拽/右键或再 AI 预测）

纯同步对话框（无跨线程信号连接），返回结果供主窗口回写。
"""

import os
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QAbstractItemView,
)

from type_descriptor import describe_file_type

# 配色（与主窗口/流式窗口一致，局部定义保持零耦合）
COLOR_SUCCESS = "#5cb88a"
COLOR_MID = "#e8c268"
COLOR_TEXT_DIM = "#747c78"
COLOR_TEXT_SECONDARY = "#a8b0ac"

# 表格列号
COL_SKIP = 0
COL_TYPE = 1
COL_NAME = 2
COL_SIZE = 3
COL_MTIME = 4
COL_SRC = 5

_HEADERS = ["跳过", "类型", "文件名", "大小", "修改时间", "来源路径"]


def _human_readable_size(size) -> str:
    """字节数转可读格式（与其它窗口一致，局部定义避免循环导入）"""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "未知"
    if size < 0:
        return "未知"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _format_time(timestamp) -> str:
    """时间戳格式化"""
    if not timestamp:
        return "未知"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "未知"


class StepReviewDialog(QDialog):
    """R2 逐类审核窗口

    Args:
        items: 待审核条目列表，每项 dict：
            {"filename", "path"(源路径), "size", "modified", "is_dir", "category"}
        categories: 分类配置列表 [{"name", "path", ...}, ...]（用于推导目标路径/暂存）

    结果通过 get_results() 获取（accept 后调用）。
    """

    def __init__(self, items: list, categories: list, parent=None):
        super().__init__(parent)
        self.items = list(items or [])
        self.categories = list(categories or [])
        self._cat_path_map = {c.get("name", ""): (c.get("path", "") or "")
                              for c in self.categories if isinstance(c, dict)}

        # 按分类分组（保持 categories 配置顺序；配置外的分类排最后）
        self._groups = self._build_groups()
        self._group_idx = 0
        # filename -> bool（是否跳过），跨分类切换持久
        self._skip_state = {}

        self.setWindowTitle("分类审核（R2 逐类核对）")
        self.setModal(True)
        self.resize(1000, 580)

        self._build_ui()
        if self._groups:
            self._load_group(0)
        else:
            self.category_combo.addItem("（无可审核条目）")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.finish_btn.setEnabled(False)

    def _build_groups(self) -> list:
        """按分类聚合条目，返回 [{"category", "is_staging", "target", "items": [...]}, ...]"""
        order_index = {c.get("name", ""): i for i, c in enumerate(self.categories)
                       if isinstance(c, dict)}
        grouped = {}
        for it in self.items:
            cat = it.get("category", "") or ""
            grouped.setdefault(cat, []).append(it)

        groups = []
        for cat in sorted(grouped.keys(),
                          key=lambda c: order_index.get(c, len(order_index))):
            path = self._cat_path_map.get(cat, "")
            is_staging = not path
            target = "暂存（不移动，仅标记已处理）" if is_staging else path
            groups.append({
                "category": cat,
                "is_staging": is_staging,
                "target": target,
                "items": grouped[cat],
            })
        return groups

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── 顶部：分类步骤导航 ──
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.prev_btn = QPushButton("◀ 上一类")
        self.prev_btn.clicked.connect(self._prev_group)
        nav.addWidget(self.prev_btn)

        self.category_combo = QComboBox()
        for i, g in enumerate(self._groups):
            self.category_combo.addItem(
                f"分类 {i + 1}/{len(self._groups)}：{g['category']}（{len(g['items'])} 项）")
        self.category_combo.currentIndexChanged.connect(self._on_combo_changed)
        nav.addWidget(self.category_combo, 1)

        self.next_btn = QPushButton("下一类 ▶")
        self.next_btn.clicked.connect(self._next_group)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)

        # 目标路径行
        self.target_label = QLabel("")
        self.target_label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
        self.target_label.setWordWrap(True)
        root.addWidget(self.target_label)

        # 提示
        hint = QLabel("逐类核对：可勾选「跳过」，但不能修改分类目标。"
                      "审核完成后，跳过项自动清除分类，其余标为「已审」。")
        hint.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ── 中部：当前分类的条目表 ──
        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_SKIP, QHeaderView.Fixed)
        header.resizeSection(COL_SKIP, 50)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_MTIME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_SRC, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        # ── 底部：计数 + 按钮 ──
        bottom = QHBoxLayout()
        self.count_label = QLabel("")
        bottom.addWidget(self.count_label, 1)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(cancel_btn)

        self.finish_btn = QPushButton("完成审核")
        self.finish_btn.setObjectName("primary")
        self.finish_btn.clicked.connect(self.accept)
        bottom.addWidget(self.finish_btn)
        root.addLayout(bottom)

    # ── 分组导航 ──

    def _current_group(self):
        if 0 <= self._group_idx < len(self._groups):
            return self._groups[self._group_idx]
        return None

    def _save_current_checks(self):
        """把当前表格的勾选状态写回 _skip_state"""
        g = self._current_group()
        if not g:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_NAME)
            check = self.table.item(row, COL_SKIP)
            if item and check:
                fname = item.data(Qt.UserRole)
                if fname:
                    self._skip_state[fname] = (check.checkState() == Qt.Checked)

    def _load_group(self, idx: int):
        """保存当前勾选并载入第 idx 个分类组"""
        self._save_current_checks()
        if not (0 <= idx < len(self._groups)):
            return
        self._group_idx = idx
        # 同步 combo（阻断信号避免递归）
        self.category_combo.blockSignals(True)
        self.category_combo.setCurrentIndex(idx)
        self.category_combo.blockSignals(False)

        g = self._groups[idx]
        staging = g["is_staging"]
        self.target_label.setText(
            f"目标分类：{g['category']}　→　{g['target']}")

        self.table.setRowCount(0)
        for it in g["items"]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            fname = it.get("filename", "")
            is_dir = bool(it.get("is_dir", False))

            # 跳过复选框
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if self._skip_state.get(fname, False)
                                else Qt.Unchecked)
            self.table.setItem(row, COL_SKIP, check)

            # 类型
            self._set_text(row, COL_TYPE, "文件夹" if is_dir else describe_file_type(fname))

            # 文件名（文件夹加图标前缀）
            name_item = QTableWidgetItem(("📁 " if is_dir else "") + fname)
            name_item.setData(Qt.UserRole, fname)
            name_item.setToolTip(it.get("path", ""))
            self.table.setItem(row, COL_NAME, name_item)

            # 大小 / 修改时间
            self._set_text(row, COL_SIZE,
                           "—" if is_dir else _human_readable_size(it.get("size", 0)))
            self._set_text(row, COL_MTIME, _format_time(it.get("modified", 0)))

            # 来源路径
            src_item = QTableWidgetItem(it.get("path", ""))
            src_item.setToolTip(it.get("path", ""))
            self.table.setItem(row, COL_SRC, src_item)

        self.prev_btn.setEnabled(idx > 0)
        self.next_btn.setEnabled(idx < len(self._groups) - 1)
        self._update_count()

    def _set_text(self, row: int, col: int, text: str):
        item = QTableWidgetItem(text)
        if col == COL_SRC:
            item.setForeground(QColor(COLOR_TEXT_DIM))
        self.table.setItem(row, col, item)

    def _update_count(self):
        g = self._current_group()
        if not g:
            self.count_label.setText("")
            return
        total_all = len(self.items)
        self.count_label.setText(
            f"当前分类 {len(g['items'])} 项　|　全部待审 {total_all} 项")

    def _prev_group(self):
        if self._group_idx > 0:
            self._load_group(self._group_idx - 1)

    def _next_group(self):
        if self._group_idx < len(self._groups) - 1:
            self._load_group(self._group_idx + 1)

    def _on_combo_changed(self, idx: int):
        if idx != self._group_idx and 0 <= idx < len(self._groups):
            self._load_group(idx)

    # ── 结果 ──

    def get_results(self) -> dict:
        """返回审核结果

        Returns:
            {
                "reviewed": [filename, ...],  # 未跳过 → 标"已审"
                "skipped": [filename, ...],   # 跳过 → 清除分类
            }
        """
        # 确保当前展示的分组勾选状态也已保存
        self._save_current_checks()
        reviewed, skipped = [], []
        for it in self.items:
            fname = it.get("filename", "")
            if not fname:
                continue
            if self._skip_state.get(fname, False):
                skipped.append(fname)
            else:
                reviewed.append(fname)
        return {"reviewed": reviewed, "skipped": skipped}

    # ── 关闭保护：完成审核前关闭需二次确认（避免误触丢失审核进度） ──

    def closeEvent(self, event):
        # accept()/reject() 走的是正常路径；这里拦截右上角 X
        if self.result() == 0 and not getattr(self, "_closing_ok", False):
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, "放弃审核？",
                "审核尚未完成，关闭窗口将不保存本次审核结果。确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)

    def accept(self):
        self._closing_ok = True
        super().accept()

    def reject(self):
        self._closing_ok = True
        super().reject()
