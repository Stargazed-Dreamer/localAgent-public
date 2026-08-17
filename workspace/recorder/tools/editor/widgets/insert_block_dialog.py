"""插入块类型选择对话框（02-editor §7）。

替换原 QInputDialog.getItem：自定义 QDialog 提供
- 类型列表（QListWidget）：每项 = 图标 + 类型名 + 一行说明（tertiary）
- 位置说明：将插入到选中块之后 / 追加到时间轴末尾
- 按钮：[取消] [插入]（primary）；双击列表项 = 直接插入

类型选定后仍由 EditorWindow._on_insert 走原有的文本/文件输入子流程。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from lib.ui import icon, tokens
from lib.ui.theme import set_kind, set_text_role


@dataclass(frozen=True)
class InsertType:
    """可插入块类型描述。"""

    key: str          # 内部 key（"note" / "chapter" / "image" / "text"）
    label: str        # 类型名（中文）
    description: str  # 一行说明（tertiary 色）
    icon_name: str    # lib/ui 图标名
    icon_color: str   # 图标色 token


# 四种可插入类型（与 EditorWindow._on_insert 原 items 顺序保持一致）
INSERT_TYPES: list[InsertType] = [
    InsertType(
        key="note",
        label="注释",
        description="插入一段用户备注，可编辑文本",
        icon_name="message-square",
        icon_color=tokens.ICON_DEFAULT,
    ),
    InsertType(
        key="chapter",
        label="章节",
        description="插入章节分隔，可重命名",
        icon_name="bookmark",
        icon_color=tokens.ICON_DEFAULT,
    ),
    InsertType(
        key="image",
        label="参考图片",
        description="从文件选择图片作为参考素材",
        icon_name="camera",
        icon_color=tokens.ICON_DEFAULT,
    ),
    InsertType(
        key="text",
        label="文件文本",
        description="从 .txt/.md 等文件导入文本内容",
        icon_name="file-text",
        icon_color=tokens.ICON_DEFAULT,
    ),
]


class InsertBlockDialog(QDialog):
    """插入块类型选择对话框（02-editor §7）。

    Args:
        position_hint: 位置说明文本，如 "将插入到选中块（00:03.2 截图）之后"；
            无选中块时传 "追加到时间轴末尾"。
        parent: 父窗口。
    """

    def __init__(
        self,
        position_hint: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("插入块")
        self.setModal(True)
        self._selected_key: str | None = None

        self._build_ui(position_hint)
        self._populate_types()

    # ===== UI 构建 =====

    def _build_ui(self, position_hint: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(16)

        # 位置说明
        hint_label = QLabel(position_hint)
        set_text_role(hint_label, "tertiary")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        # 类型列表
        self._list = QListWidget()
        self._list.setIconSize(_icon_size())
        self._list.setSpacing(2)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, stretch=1)

        # 按钮区
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.NoButton)
        self._cancel_btn = btn_box.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self._insert_btn = btn_box.addButton("插入", QDialogButtonBox.ButtonRole.AcceptRole)
        set_kind(self._insert_btn, "primary")
        self._insert_btn.setEnabled(False)
        self._insert_btn.clicked.connect(self._on_insert_clicked)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self.setMinimumWidth(420)
        self.adjustSize()

    def _populate_types(self) -> None:
        """填充类型列表（每项 = 图标 + 类型名 + 一行说明）。"""
        for t in INSERT_TYPES:
            item = QListWidgetItem()
            item.setIcon(icon(t.icon_name, t.icon_color))
            item.setText(t.label)
            item.setToolTip(t.description)
            # 用 Qt.UserRole 存 InsertType，避免后续按 row 反查
            item.setData(Qt.ItemDataRole.UserRole, t)
            # 描述放在 WhatsThis（QListWidget 不直接支持双行文本）
            item.setWhatsThis(t.description)
            self._list.addItem(item)

    # ===== 事件 =====

    def _on_selection_changed(self) -> None:
        items = self._list.selectedItems()
        self._insert_btn.setEnabled(bool(items))

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """双击列表项 = 直接插入（等同点 [插入]）。"""
        if item is None:
            return
        self._selected_key = item.data(Qt.ItemDataRole.UserRole).key
        self.accept()

    def _on_insert_clicked(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        self._selected_key = items[0].data(Qt.ItemDataRole.UserRole).key
        self.accept()

    # ===== 结果导出 =====

    def selected_key(self) -> str | None:
        """返回用户选择的类型 key（"note"/"chapter"/"image"/"text"）。

        用户取消时返回 None。
        """
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._selected_key


def _icon_size():
    from PySide6.QtCore import QSize
    return QSize(tokens.ICON_SIZE_LG, tokens.ICON_SIZE_LG)


__all__ = ["InsertBlockDialog", "InsertType", "INSERT_TYPES"]
