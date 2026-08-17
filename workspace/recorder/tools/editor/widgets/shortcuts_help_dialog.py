"""快捷键帮助对话框（02-editor §9）。

F1 触发的模态 QDialog，列出编辑器主窗口与截图查看器的全部快捷键。

布局：
- 分组展示（主窗口 / 截图查看器）
- 两列：快捷键（mono，accent 色） + 说明（primary）
- 按钮：[关闭]（默认 primary）
- Esc 关闭（QDialog 自带）
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


@dataclass(frozen=True)
class ShortcutEntry:
    """单条快捷键说明。"""

    key: str         # 快捷键文本（如 "Ctrl+Z"）
    description: str # 中文说明


# 主窗口快捷键表（02-editor §9，与 editor_window.py _build_toolbar 顺序一致）
MAIN_WINDOW_SHORTCUTS: list[ShortcutEntry] = [
    ShortcutEntry("Delete", "移除块 / 恢复块"),
    ShortcutEntry("M", "合并到前块"),
    ShortcutEntry("S", "拆分块"),
    ShortcutEntry("K", "标关键 / 取消"),
    ShortcutEntry("X", "标异常 / 取消"),
    ShortcutEntry("A", "标可自动化 / 取消"),
    ShortcutEntry("I", "插入块"),
    ShortcutEntry("Ctrl+Z", "撤销"),
    ShortcutEntry("Ctrl+Shift+Z", "重做"),
    ShortcutEntry("F", "标记就绪 / 取消"),
    ShortcutEntry("P", "播放预览"),
    ShortcutEntry("F2", "重命名章节"),
    ShortcutEntry("↑ / ↓", "选择上一块 / 下一块"),
    ShortcutEntry("Enter", "查看截图"),
    ShortcutEntry("F1", "快捷键帮助"),
]


# 截图查看器快捷键表（02-editor §9 末尾括号内）
VIEWER_SHORTCUTS: list[ShortcutEntry] = [
    ShortcutEntry("Ctrl + 滚轮", "缩放（10% – 800%，光标锚点）"),
    ShortcutEntry("← / →", "上一帧 / 下一帧"),
    ShortcutEntry("双击", "适应窗口 ↔ 100% 切换"),
    ShortcutEntry("Esc", "关闭查看器"),
]


class ShortcutsHelpDialog(QDialog):
    """编辑器快捷键帮助对话框（F1 触发）。

    Args:
        parent: 父窗口（EditorWindow）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("快捷键帮助")
        self.setModal(True)

        self._build_ui()

    # ===== UI 构建 =====

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            tokens.DIALOG_MARGIN,
            tokens.DIALOG_MARGIN,
            tokens.DIALOG_MARGIN,
            tokens.SPACE_LG,
        )
        layout.setSpacing(tokens.DIALOG_SECTION_GAP)

        # 标题
        title = QLabel("编辑器快捷键")
        set_text_role(title, "heading")
        layout.addWidget(title)

        # 主窗口分组
        layout.addWidget(self._build_section("主窗口", MAIN_WINDOW_SHORTCUTS))

        # 截图查看器分组
        layout.addWidget(self._build_section("截图查看器", VIEWER_SHORTCUTS))

        # 弹性间隔把按钮推到底部
        layout.addStretch(1)

        # 关闭按钮（primary，回车 = 关闭）
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.NoButton)
        close_btn = btn_box.addButton("关闭", QDialogButtonBox.ButtonRole.AcceptRole)
        set_kind(close_btn, "primary")
        close_btn.setDefault(True)
        btn_box.accepted.connect(self.accept)
        layout.addWidget(btn_box)

        self.setMinimumWidth(tokens.DIALOG_MIN_WIDTH)
        self.adjustSize()

    def _build_section(
        self,
        title_text: str,
        entries: list[ShortcutEntry],
    ) -> QWidget:
        """构造一个分组（标题 + 快捷键表）。

        用 QFrame kind="card" 包裹，内部 QGridLayout 两列对齐。
        """
        frame = QFrame()
        set_kind(frame, "card")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(
            tokens.SPACE_LG,
            tokens.SPACE_MD,
            tokens.SPACE_LG,
            tokens.SPACE_MD,
        )
        frame_layout.setSpacing(tokens.SPACE_SM)

        section_title = QLabel(title_text)
        set_text_role(section_title, "title")
        frame_layout.addWidget(section_title)

        # 快捷键表（2 列网格）
        grid = QGridLayout()
        grid.setHorizontalSpacing(tokens.SPACE_LG)
        grid.setVerticalSpacing(tokens.SPACE_XS)
        for row, entry in enumerate(entries):
            key_label = QLabel(entry.key)
            set_text_role(key_label, "mono")
            key_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            # mono 字体下用 accent 色突出快捷键
            key_label.setStyleSheet(f"color: {tokens.ACCENT};")
            grid.addWidget(key_label, row, 0)

            desc_label = QLabel(entry.description)
            grid.addWidget(desc_label, row, 1)

        # 快捷键列固定宽度，说明列拉伸
        grid.setColumnMinimumWidth(0, 110)
        grid.setColumnStretch(1, 1)
        frame_layout.addLayout(grid)

        return frame


__all__ = ["ShortcutsHelpDialog", "ShortcutEntry", "MAIN_WINDOW_SHORTCUTS", "VIEWER_SHORTCUTS"]
