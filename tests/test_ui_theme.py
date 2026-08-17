"""lib/ui 共享主题基础设施测试。

运行：QT_QPA_PLATFORM=offscreen uv run pytest tests/test_ui_theme.py
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lib.ui import tokens as T  # noqa: E402
from lib.ui.icons import available_icons, icon_path, render_svg  # noqa: E402
from lib.ui.theme import build_qss  # noqa: E402

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """会话级 QApplication：QPixmap 渲染必须先有 QGuiApplication 实例。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

class TestTokens:
    REQUIRED = [
        "BG_BASE", "BG_PANEL", "BG_CARD", "BG_INPUT", "BG_HOVER", "BG_PRESSED",
        "BG_TOOLTIP", "BG_OVERLAY", "BORDER", "BORDER_STRONG", "BORDER_FOCUS",
        "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_TERTIARY", "TEXT_DISABLED",
        "TEXT_ON_ACCENT", "ACCENT", "ACCENT_HOVER", "ACCENT_PRESSED",
        "ACCENT_WASH", "ACCENT_WASH_HOVER", "ACCENT_BORDER",
        "DANGER", "DANGER_TEXT", "DANGER_WASH", "WARNING", "WARNING_TEXT",
        "WARNING_WASH", "SUCCESS", "SUCCESS_TEXT", "INFO", "INFO_TEXT", "REC",
        "ICON_DEFAULT", "ICON_ACTIVE", "ICON_DANGER", "ICON_WARNING", "ICON_REC",
    ]

    def test_required_tokens_exist_and_are_hex(self):
        for name in self.REQUIRED:
            value = getattr(T, name)
            assert HEX_RE.match(value), f"{name}={value!r} 不是 #RRGGBB 格式"

    def test_font_tokens_exist(self):
        assert "sans-serif" in T.FONT_FAMILY
        assert "monospace" in T.FONT_MONO

    def test_block_colors_cover_timeline_types(self):
        # 与 timeline_view 渲染的类型对齐（docs/recorder-guide.md L2 块类型）
        for t in ("mouse_click", "mouse_scroll", "mouse_drag", "keyboard",
                  "focus", "idle", "screenshot", "stt_segment", "user_note", "chapter"):
            assert t in T.BLOCK_COLORS
            assert HEX_RE.match(T.BLOCK_COLORS[t])

    def test_spacing_grid_multiple_of_4(self):
        for name in ("SPACE_XS", "SPACE_SM", "SPACE_MD", "SPACE_LG", "SPACE_XL", "SPACE_XXL"):
            assert getattr(T, name) % 4 == 0


# ---------------------------------------------------------------------------
# QSS
# ---------------------------------------------------------------------------

class TestQss:
    def test_build_qss_no_unresolved_placeholders(self):
        qss = build_qss()
        assert "$" not in qss, "QSS 中存在未替换的 Template 占位符"

    def test_build_qss_contains_core_selectors(self):
        qss = build_qss()
        for sel in ("QPushButton", "QPushButton[kind=\"primary\"]", "QPushButton[kind=\"danger\"]",
                    "QPushButton:checkable:checked", "QToolButton:checked", "QLineEdit",
                    "QComboBox", "QCheckBox", "QRadioButton", "QSlider", "QTableWidget",
                    "QHeaderView::section", "QTreeWidget", "QListWidget", "QGroupBox",
                    "QStatusBar", "QMenu", "QTabBar::tab", "QScrollBar", "QToolTip",
                    "QProgressBar", "QSplitter::handle", "#EmptyState",
                    "QLabel[textRole=\"secondary\"]"):
            assert sel in qss, f"QSS 缺少选择器 {sel}"

    def test_build_qss_uses_token_values(self):
        qss = build_qss()
        assert T.ACCENT in qss
        assert T.BG_BASE in qss
        assert T.TEXT_PRIMARY in qss


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------

class TestIcons:
    RECORDER_EDITOR_NEEDED = [
        # 编辑器工具栏
        "eye-off", "eye", "merge", "scissors", "star", "alert-triangle",
        "bot", "plus", "undo", "redo", "sliders", "check-circle", "play",
        # 录制器
        "record", "square", "pause", "mic", "mic-off", "save", "trash",
        "refresh", "monitor", "app-window", "bookmark",
        # 查看器/通用
        "zoom-in", "zoom-out", "maximize", "chevron-left", "chevron-right",
        "x", "image", "check", "info", "pencil", "list", "grip-vertical",
        "copy",
    ]

    def test_required_icons_exist(self):
        names = available_icons()
        for n in self.RECORDER_EDITOR_NEEDED:
            assert n in names, f"缺少图标 {n}"

    def test_all_svgs_use_current_color(self):
        for n in available_icons():
            text = icon_path(n).read_text(encoding="utf-8")
            assert "currentColor" in text, f"{n}.svg 未使用 currentColor，无法按主题着色"

    def test_render_pixmap(self):
        pm = render_svg("star", T.ICON_ACTIVE, 16, 1.0)
        assert not pm.isNull()
        assert pm.width() == 16 and pm.height() == 16

    def test_render_unknown_icon_raises(self):
        with pytest.raises(KeyError):
            render_svg("no-such-icon", T.ICON_DEFAULT, 16)


# ---------------------------------------------------------------------------
# apply_theme（offscreen 冒烟）
# ---------------------------------------------------------------------------

class TestApplyTheme:
    def test_apply_to_application(self):
        from PySide6.QtWidgets import QApplication

        from lib.ui.theme import apply_theme
        app = QApplication.instance() or QApplication([])
        qss = apply_theme(app)
        assert app.styleSheet() == qss
        assert "YaHei" in app.font().family()

    def test_set_text_role_and_kind(self):
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        from lib.ui.theme import set_kind, set_text_role
        QApplication.instance() or QApplication([])
        label = QLabel("x")
        set_text_role(label, "tertiary")
        assert label.property("textRole") == "tertiary"
        btn = QPushButton("y")
        set_kind(btn, "primary")
        assert btn.property("kind") == "primary"
