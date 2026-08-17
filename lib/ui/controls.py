"""Small shared control factories for the LocalAgent UI system."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QToolButton, QWidget

from lib.ui import tokens as T
from lib.ui.icons import icon


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
