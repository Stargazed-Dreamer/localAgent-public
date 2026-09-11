"""Small shared control factories for the LocalAgent UI system."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from lib.ui import tokens as T
from lib.ui.icons import icon, icon_pixmap


def icon_button(
    icon_name: str,
    tooltip: str,
    *,
    color: str = T.ICON_DEFAULT,
    parent: QWidget | None = None,
) -> QToolButton:
    """Create a theme-aligned 28px icon-only tool button."""
    button = QToolButton(parent)
    button.setProperty("iconOnly", "true")
    button.setIcon(icon(icon_name, color, T.ICON_SIZE_MD))
    button.setIconSize(QSize(T.ICON_SIZE_MD, T.ICON_SIZE_MD))
    button.setFixedSize(T.CTRL_HEIGHT_MD, T.CTRL_HEIGHT_MD)
    button.setToolTip(tooltip)
    button.setStatusTip(tooltip)
    button.setAccessibleName(tooltip)
    return button


class EmptyState(QWidget):
    """空状态占位（components.md §12 的组件化实现）。

    图标 + 标题 + 可选下一步指引，整体 tertiary 色（objectName="EmptyState" 走 QSS 钩子）。
    用法::

        empty = EmptyState("inbox", "没有到期任务", hint="到期任务由 Loop 周期推送")
        layout.addWidget(empty)   # 与列表区域互斥 setVisible
    """

    def __init__(
        self,
        icon_name: str,
        title: str,
        hint: str = "",
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(T.SPACE_LG, T.SPACE_XL, T.SPACE_LG, T.SPACE_XL)
        layout.setSpacing(T.SPACE_SM)

        icon_lab = QLabel()
        icon_lab.setPixmap(icon_pixmap(icon_name, T.TEXT_TERTIARY, 48))
        icon_lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(icon_lab)

        title_lab = QLabel(title)
        title_lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lab.setWordWrap(True)
        layout.addWidget(title_lab)

        if hint:
            hint_lab = QLabel(hint)
            hint_lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint_lab.setWordWrap(True)
            layout.addWidget(hint_lab)
        layout.addStretch(1)
