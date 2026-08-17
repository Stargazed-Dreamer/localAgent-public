"""深色工程主题：QSS 生成与应用。

QSS 由 tokens.py 的 token 生成，禁止在 QSS 模板中写死色值/字号。

控件分类钩子（供各窗口使用，详见 docs/ui/components.md）：

- 按钮：``btn.setProperty("kind", "primary" | "danger" | "ghost")``
- 标签角色：``label.setProperty("textRole", "secondary" | "tertiary" | "caption"
  | "title" | "heading" | "danger" | "warning" | "success" | "accent")``
- 卡片容器：``frame.setProperty("kind", "card")``
- 空状态：容器设 ``objectName="EmptyState"``

设置动态属性后需 ``widget.style().unpolish(widget); widget.style().polish(widget)``
（本模块 ``set_kind`` / ``set_text_role`` 已代劳）。
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from lib.ui import tokens as T

_QSS_TEMPLATE = Template(r"""
/* ============================================================
   LocalAgent 深色工程主题（由 lib/ui/theme.py 生成，禁止手改产物）
   ============================================================ */

QWidget {
    background-color: $BG_BASE;
    color: $TEXT_PRIMARY;
    font-family: $FONT_FAMILY;
    font-size: ${FONT_BODY}px;
    selection-background-color: $ACCENT_WASH;
    selection-color: $TEXT_PRIMARY;
}

QDialog, QMainWindow { background-color: $BG_BASE; }

/* ---------- 文本角色 ---------- */
QLabel { background: transparent; }
QLabel[textRole="secondary"] { color: $TEXT_SECONDARY; }
QLabel[textRole="tertiary"] { color: $TEXT_TERTIARY; }
QLabel[textRole="caption"] { color: $TEXT_TERTIARY; font-size: ${FONT_CAPTION}px; }
QLabel[textRole="title"] { font-size: ${FONT_TITLE}px; font-weight: 600; }
QLabel[textRole="heading"] { font-size: ${FONT_HEADING}px; font-weight: 600; }
QLabel[textRole="danger"] { color: $DANGER_TEXT; }
QLabel[textRole="warning"] { color: $WARNING_TEXT; }
QLabel[textRole="success"] { color: $SUCCESS_TEXT; }
QLabel[textRole="accent"] { color: $ACCENT; }
QLabel[textRole="mono"] { font-family: $FONT_MONO; }
QLabel:disabled { color: $TEXT_DISABLED; }

/* ---------- 按钮 ---------- */
QPushButton {
    background-color: $BG_INPUT;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_SM}px;
    padding: 5px 14px;
    min-height: ${CTRL_HEIGHT_MD}px;
    color: $TEXT_PRIMARY;
}
QPushButton:hover { background-color: $BG_HOVER; border-color: $BORDER_STRONG; }
QPushButton:pressed { background-color: $BG_PRESSED; }
QPushButton:focus { border-color: $ACCENT_BORDER; }
QPushButton:disabled {
    background-color: $BG_PANEL;
    border-color: $BORDER;
    color: $TEXT_DISABLED;
}
QPushButton[kind="primary"], QPushButton:default {
    background-color: $ACCENT;
    border: 1px solid $ACCENT;
    color: $TEXT_ON_ACCENT;
    font-weight: 600;
}
QPushButton[kind="primary"]:hover, QPushButton:default:hover { background-color: $ACCENT_HOVER; border-color: $ACCENT_HOVER; }
QPushButton[kind="primary"]:pressed, QPushButton:default:pressed { background-color: $ACCENT_PRESSED; border-color: $ACCENT_PRESSED; }
QPushButton[kind="primary"]:disabled, QPushButton:default:disabled {
    background-color: $ACCENT_WASH;
    border-color: $ACCENT_WASH;
    color: $TEXT_DISABLED;
}
QPushButton[kind="danger"] {
    background-color: transparent;
    border: 1px solid $DANGER;
    color: $DANGER_TEXT;
}
QPushButton[kind="danger"]:hover { background-color: $DANGER_WASH; }
QPushButton[kind="danger"]:pressed { background-color: $DANGER; color: $TEXT_ON_ACCENT; }
QPushButton[kind="ghost"] {
    background-color: transparent;
    border: 1px solid transparent;
    color: $TEXT_SECONDARY;
}
QPushButton[kind="ghost"]:hover { background-color: $BG_HOVER; color: $TEXT_PRIMARY; }
QPushButton[kind="ghost"]:pressed { background-color: $BG_PRESSED; }
/* 可切换按钮（checkable）：标记类操作必须 checkable，checked 态=已标记 */
QPushButton:checkable:checked {
    background-color: $ACCENT_WASH;
    border: 1px solid $ACCENT_BORDER;
    color: $ACCENT;
}

/* ---------- 工具栏 / 工具按钮 ---------- */
QToolBar {
    background-color: $BG_PANEL;
    border: none;
    border-bottom: 1px solid $BORDER;
    spacing: 2px;
    padding: 4px 6px;
}
QToolBar::separator { background-color: $BORDER; width: 1px; margin: 5px 6px; }
QToolButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: ${RADIUS_SM}px;
    padding: 4px 8px;
    color: $TEXT_SECONDARY;
}
QToolButton:hover { background-color: $BG_HOVER; color: $TEXT_PRIMARY; }
QToolButton:pressed { background-color: $BG_PRESSED; }
QToolButton:checked {
    background-color: $ACCENT_WASH;
    border: 1px solid $ACCENT_BORDER;
    color: $ACCENT;
}
QToolButton:disabled { color: $TEXT_DISABLED; }
QToolButton[iconOnly="true"] {
    padding: 0;
}
QToolButton[popupMode="1"], QToolButton[popupMode="2"] { padding-right: 16px; }
QToolButton::menu-arrow { image: none; width: 0; }

/* ---------- 输入控件 ---------- */
QLineEdit, QPlainTextEdit, QTextEdit {
    background-color: $BG_INPUT;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_SM}px;
    padding: 5px 8px;
    color: $TEXT_PRIMARY;
    selection-background-color: $ACCENT_WASH;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color: $ACCENT; }
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {
    background-color: $BG_PANEL;
    color: $TEXT_DISABLED;
}
QLineEdit:read-only { color: $TEXT_TERTIARY; background-color: $BG_PANEL; }
QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] { border-color: $DANGER; }
QTextBrowser { background-color: $BG_CARD; border: 1px solid $BORDER; border-radius: ${RADIUS_SM}px; }

QComboBox {
    background-color: $BG_INPUT;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_SM}px;
    padding: 4px 8px;
    min-height: ${CTRL_HEIGHT_MD}px;
    color: $TEXT_PRIMARY;
}
QComboBox:hover { border-color: $BORDER_STRONG; }
QComboBox:focus { border-color: $ACCENT; }
QComboBox:disabled { background-color: $BG_PANEL; color: $TEXT_DISABLED; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $TEXT_TERTIARY;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: $BG_TOOLTIP;
    border: 1px solid $BORDER_STRONG;
    border-radius: ${RADIUS_SM}px;
    padding: 4px;
    outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 24px; padding: 2px 8px; border-radius: ${RADIUS_SM}px; }
QComboBox QAbstractItemView::item:selected { background-color: $ACCENT_WASH; color: $TEXT_PRIMARY; }

QSpinBox, QDoubleSpinBox {
    background-color: $BG_INPUT;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_SM}px;
    padding: 4px 6px;
    min-height: ${CTRL_HEIGHT_MD}px;
    color: $TEXT_PRIMARY;
}
QSpinBox:focus, QDoubleSpinBox:focus { border-color: $ACCENT; }
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: transparent; border: none; width: 16px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid $TEXT_TERTIARY;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid $TEXT_TERTIARY;
}

QSlider::groove:horizontal {
    height: 4px;
    background-color: $BG_INPUT;
    border-radius: 2px;
}
QSlider::sub-page:horizontal { background-color: $ACCENT; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 16px; height: 16px;
    margin: -7px 0;
    border-radius: 8px;
    background-color: $ACCENT;
}
QSlider::handle:horizontal:hover { background-color: $ACCENT_HOVER; }
QSlider::handle:horizontal:disabled { background-color: $TEXT_DISABLED; }

/* ---------- 复选 / 单选 ---------- */
QCheckBox, QRadioButton { spacing: 8px; color: $TEXT_PRIMARY; background: transparent; }
QCheckBox:disabled, QRadioButton:disabled { color: $TEXT_DISABLED; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px; height: 16px;
    background-color: $BG_INPUT;
    border: 1px solid $BORDER_STRONG;
}
QCheckBox::indicator { border-radius: 3px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $ACCENT; }
QCheckBox::indicator:checked {
    background-color: $ACCENT; border-color: $ACCENT;
    image: url($CHECK_ICON_URL);
}
QRadioButton::indicator:checked {
    background-color: $ACCENT; border-color: $ACCENT;
    image: url($RADIO_ICON_URL);
}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background-color: $BG_PANEL; border-color: $BORDER;
}

/* ---------- 列表 / 树 / 表格 ---------- */
QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView {
    background-color: $BG_PANEL;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_MD}px;
    outline: 0;
    alternate-background-color: $BG_CARD;
    gridline-color: $BORDER;
}
QListWidget::item, QTreeWidget::item { padding: 4px 8px; border-radius: ${RADIUS_SM}px; }
QListWidget::item:hover, QTreeWidget::item:hover, QTableWidget::item:hover { background-color: $BG_HOVER; }
QListWidget::item:selected, QTreeWidget::item:selected {
    background-color: $ACCENT_WASH;
    color: $TEXT_PRIMARY;
}
QTableWidget::item, QTableView::item { padding: 4px 8px; border: none; }
QTableWidget::item:selected, QTableView::item:selected {
    background-color: $ACCENT_WASH;
    color: $TEXT_PRIMARY;
}
QListWidget#sidebar {
    background-color: $BG_PANEL;
    border: none;
    border-right: 1px solid $BORDER;
    border-radius: 0;
}
QListWidget#sidebar::item {
    padding: 0;
    border-radius: 0;
}
QListWidget#dashboardActivityList {
    padding: ${SPACE_SM}px;
}
QListWidget#dashboardActivityList::item {
    padding: ${SPACE_SM}px ${SPACE_MD}px;
    border-radius: 0;
}
QHeaderView::section {
    background-color: $BG_CARD;
    color: $TEXT_SECONDARY;
    font-weight: 600;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid $BORDER;
    border-bottom: 1px solid $BORDER;
}

/* ---------- 分组框 / 卡片 / 分隔线 ---------- */
QGroupBox {
    background-color: $BG_CARD;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_MD}px;
    margin-top: 26px;
    padding: ${SPACE_MD}px;
    padding-top: ${SPACE_LG}px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: ${SPACE_MD}px;
    top: 7px;
    color: $TEXT_SECONDARY;
}
QFrame[kind="card"] {
    background-color: $BG_CARD;
    border: 1px solid $BORDER;
    border-radius: ${RADIUS_MD}px;
}
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    color: $BORDER;
    background-color: $BORDER;
    border: none;
    max-height: 1px;
    max-width: none;
}
QFrame[frameShape="5"] { max-height: none; max-width: 1px; }

/* ---------- 状态栏 / 菜单 ---------- */
QStatusBar {
    background-color: $BG_PANEL;
    border-top: 1px solid $BORDER;
    color: $TEXT_TERTIARY;
    font-size: ${FONT_SMALL}px;
}
QStatusBar::item { border: none; }
QMenuBar {
    background-color: $BG_PANEL;
    border-bottom: 1px solid $BORDER;
}
QMenuBar::item { padding: 5px 10px; background: transparent; border-radius: ${RADIUS_SM}px; }
QMenuBar::item:selected { background-color: $BG_HOVER; }
QMenu {
    background-color: $BG_TOOLTIP;
    border: 1px solid $BORDER_STRONG;
    border-radius: ${RADIUS_MD}px;
    padding: 4px;
}
QMenu::item { padding: 5px 24px 5px 12px; border-radius: ${RADIUS_SM}px; color: $TEXT_PRIMARY; }
QMenu::item:selected { background-color: $ACCENT_WASH; }
QMenu::item:disabled { color: $TEXT_DISABLED; }
QMenu::separator { height: 1px; background-color: $BORDER; margin: 4px 8px; }
QMenu::indicator { width: 14px; height: 14px; }

/* ---------- 标签页 ---------- */
QTabWidget::pane { border: 1px solid $BORDER; border-radius: ${RADIUS_MD}px; top: -1px; }
QTabBar::tab {
    background-color: $BG_PANEL;
    color: $TEXT_SECONDARY;
    padding: 6px 14px;
    margin-right: 2px;
    border: 1px solid $BORDER;
    border-bottom: none;
    border-top-left-radius: ${RADIUS_MD}px;
    border-top-right-radius: ${RADIUS_MD}px;
}
QTabBar::tab:selected {
    background-color: $BG_BASE;
    color: $TEXT_PRIMARY;
    border-color: $ACCENT_BORDER;
}
QTabBar::tab:hover:!selected { color: $TEXT_PRIMARY; background-color: $BG_HOVER; }

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {
    background: transparent; width: 12px; margin: 2px;
}
QScrollBar::handle:vertical {
    background-color: $BORDER_STRONG; min-height: 30px; border-radius: 4px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background-color: $TEXT_TERTIARY; }
QScrollBar:horizontal {
    background: transparent; height: 12px; margin: 2px;
}
QScrollBar::handle:horizontal {
    background-color: $BORDER_STRONG; min-width: 30px; border-radius: 4px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background-color: $TEXT_TERTIARY; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }

/* ---------- 工具提示 / 进度条 / 分割器 ---------- */
QToolTip {
    background-color: $BG_TOOLTIP;
    color: $TEXT_PRIMARY;
    border: 1px solid $BORDER_STRONG;
    border-radius: ${RADIUS_SM}px;
    padding: 4px 8px;
    font-size: ${FONT_SMALL}px;
}
QProgressBar {
    background-color: $BG_INPUT;
    border: none;
    border-radius: ${RADIUS_SM}px;
    height: 8px;
    text-align: center;
    color: $TEXT_TERTIARY;
    font-size: ${FONT_CAPTION}px;
}
QProgressBar::chunk { background-color: $ACCENT; border-radius: ${RADIUS_SM}px; }
QSplitter::handle { background-color: $BG_BASE; }
QSplitter::handle:horizontal { width: 2px; }
QSplitter::handle:vertical { height: 2px; }
QSplitter::handle:hover { background-color: $ACCENT_BORDER; }

/* ---------- 状态指示（色点/状态文本，动态 status 属性） ---------- */
QLabel[status="online"] { color: $SUCCESS_TEXT; }
QLabel[status="offline"] { color: $DANGER_TEXT; }
QLabel[status="warning"] { color: $WARNING_TEXT; }
QLabel[status="unknown"] { color: $TEXT_TERTIARY; }

/* ---------- 降级横幅 ---------- */
QLabel#WarningBanner {
    background-color: $WARNING_WASH;
    color: $WARNING_TEXT;
    border: none;
    border-bottom: 1px solid $WARNING;
    padding: 6px 12px;
    font-size: ${FONT_SMALL}px;
    font-weight: 600;
}

/* ---------- 空状态 ---------- */
#EmptyState { color: $TEXT_TERTIARY; background: transparent; }
#EmptyState QLabel { color: $TEXT_TERTIARY; }
""")


def build_qss() -> str:
    """生成全量主题 QSS（每次调用实时从 token 渲染，便于测试/调试）。"""
    icon_dir = Path(__file__).resolve().parent / "resources" / "icons"
    extra = {
        # 勾选/单选指示图：SVG 中 currentColor 无 CSS 上下文时渲染为黑色，
        # 恰好形成「ACCENT 底色 + 深色勾」的对比
        "CHECK_ICON_URL": (icon_dir / "check.svg").as_posix(),
        "RADIO_ICON_URL": (icon_dir / "record.svg").as_posix(),
    }
    return _QSS_TEMPLATE.substitute({**vars(T), **extra})


def apply_theme(app_or_widget, *, base_font_size: int | None = None) -> str:
    """对 QApplication（推荐）或单个顶层 widget 应用主题。

    返回生成的 QSS（测试与调试用）。

    - 传入 QApplication：设置 Fusion 风格 + 全局字体 + 全局样式表
    - 传入普通 widget：仅设置该 widget 的样式表（用于子进程独立 GUI）
    """
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    qss = build_qss()
    size = base_font_size or T.FONT_BODY

    if isinstance(app_or_widget, QApplication):
        app = app_or_widget
        app.setStyle("Fusion")
        app.setFont(QFont("Microsoft YaHei UI", size))
        app.setStyleSheet(qss)
    else:
        app_or_widget.setStyleSheet(qss)
    return qss


def set_kind(widget, kind: str) -> None:
    """设置控件 kind 动态属性并刷新样式（primary/danger/ghost/card）。"""
    _set_property_and_repolish(widget, "kind", kind)


def set_text_role(widget, role: str) -> None:
    """设置 QLabel 文本角色（secondary/tertiary/caption/title/heading/...）。"""
    _set_property_and_repolish(widget, "textRole", role)


def set_invalid(widget, invalid: bool) -> None:
    """设置/清除输入框 invalid 状态（红色边框，配合 patterns.md 表单校验）。"""
    _set_property_and_repolish(widget, "invalid", "true" if invalid else "false")


def set_status(widget, status: str) -> None:
    """设置 QLabel 状态指示色（online/offline/warning/unknown，配合色点/状态文本）。"""
    _set_property_and_repolish(widget, "status", status)


def _set_property_and_repolish(widget, name: str, value: str) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
