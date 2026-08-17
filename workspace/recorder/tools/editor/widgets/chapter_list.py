"""左侧章节列表：按 chapter 块生成，点击跳转。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem


class ChapterList(QListWidget):
    """章节列表（左侧 ~150px）。"""

    block_selected = Signal(str)  # 选中章节的 block_id
    block_double_clicked = Signal(str)

    def __init__(self, timeline: dict[str, Any]) -> None:
        super().__init__()
        self._block_ids: list[str] = []
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._build_list(timeline)
        self.currentRowChanged.connect(self._on_row_changed)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)

    def _build_list(self, timeline: dict[str, Any]) -> None:
        """从 timeline 的 blocks 中提取 chapter 块生成列表。"""
        self.clear()
        self._block_ids = []
        for block in timeline.get("blocks", []):
            if block.get("type") == "chapter":
                name = str(block.get("primary", {}).get("name", "")).strip() or "未命名章节"
                ts = block.get("timestamp", 0.0)
                item = QListWidgetItem(f"{name} [{ts:.1f}s]")
                item.setToolTip(item.text())
                self.addItem(item)
                self._block_ids.append(block["id"])

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._block_ids):
            self.block_selected.emit(self._block_ids[row])

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        row = self.row(item)
        if 0 <= row < len(self._block_ids):
            self.block_double_clicked.emit(self._block_ids[row])

    def refresh(self, timeline: dict[str, Any]) -> None:
        """刷新章节列表。"""
        self._build_list(timeline)
