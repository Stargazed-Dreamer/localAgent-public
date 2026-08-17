"""下方详情预览：两栏布局（02-editor §5）。

左栏：前/后帧按钮 + 截图预览（点击开查看器） + EmptyState 占位。
右栏：基本信息网格 + 状态标记 chips（与工具栏双向同步） + 可编辑内容 + 折叠原始数据。

视觉规范：docs/ui/ + temp/sdd/recorder-gui-redesign/02-editor.md §5。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lib.ui import icon, icon_pixmap, tokens
from lib.ui.theme import set_kind, set_text_role

# 块类型 → 内容区可编辑的文本字段（02-editor §5.2「内容」区）
_EDITABLE_TYPES = {"stt_transcript", "user_note", "reference_text"}

# 已知 primary 字段（已在基本信息或截图区展示，不进原始数据折叠区）
_KNOWN_PRIMARY = {"text", "name", "frame_path", "title", "window"}
# 已知 supplements 字段（已在截图区或别处展示，不进原始数据折叠区）
_KNOWN_SUPPLEMENTS = {
    "before_frame",
    "after_frame",
    "window_title",
    "parent_block_id",
    "merged_block_ids",
    "merged_into",
    "split_from",
}


class ClickableFrame(QFrame):
    """点击即发出 ``clicked`` 信号的 QFrame（截图预览容器）。"""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class DetailPanel(QWidget):
    """详情预览面板（两栏布局）。

    信号：
        open_viewer_requested(frame_paths: list[Path], current_index: int)
            截图区点击时发出，由父窗口连接到 ScreenshotViewer。
        chip_toggled(chip_name: str, checked: bool)
            状态 chip 切换时发出，chip_name ∈ {"key","anomaly","automatable"}。
            父窗口负责追加 MARK_*/UNMARK_* 标注并刷新视图。
    """

    open_viewer_requested = Signal(list, int)
    chip_toggled = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._block: dict[str, Any] | None = None
        self._package_path: Path | None = None
        self._frame_paths: list[Path] = []
        self._frame_index = 0
        self._chips_syncing = False
        self._build_ui()

    # ===== UI 构建 =====

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_screenshot_area(), stretch=0)
        layout.addWidget(self._build_info_area(), stretch=1)

    def _build_screenshot_area(self) -> QWidget:
        """左栏：前/后帧按钮 + 截图预览（点击开查看器）。"""
        container = QWidget()
        container.setFixedWidth(340)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 顶部：前/后帧按钮 + 帧计数
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(4)

        self._prev_frame_btn = QPushButton()
        self._prev_frame_btn.setIcon(icon("chevron-left", tokens.ICON_DEFAULT))
        self._prev_frame_btn.setToolTip("前帧")
        self._prev_frame_btn.setEnabled(False)
        self._prev_frame_btn.clicked.connect(self._on_prev_frame)
        nav_row.addWidget(self._prev_frame_btn)

        self._frame_count_label = QLabel("—")
        set_text_role(self._frame_count_label, "tertiary")
        self._frame_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav_row.addWidget(self._frame_count_label, stretch=1)

        self._next_frame_btn = QPushButton()
        self._next_frame_btn.setIcon(icon("chevron-right", tokens.ICON_DEFAULT))
        self._next_frame_btn.setToolTip("后帧")
        self._next_frame_btn.setEnabled(False)
        self._next_frame_btn.clicked.connect(self._on_next_frame)
        nav_row.addWidget(self._next_frame_btn)

        layout.addLayout(nav_row)

        # 截图预览容器（QFrame + kind=card 边框，可点击）
        self._frame_container = ClickableFrame()
        set_kind(self._frame_container, "card")
        self._frame_container.setFixedSize(320, 200)
        self._frame_container.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._frame_container.clicked.connect(self._on_frame_click)

        frame_layout = QVBoxLayout(self._frame_container)
        frame_layout.setContentsMargins(0, 0, 0, 0)

        # frame_label 保留为公开属性（测试与外部按名访问）
        self.frame_label = QLabel("选中块后显示详情")
        self.frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_label.setObjectName("FramePreview")
        set_text_role(self.frame_label, "tertiary")
        self.frame_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        frame_layout.addWidget(self.frame_label)

        # 角标：放大提示
        self._zoom_hint = QLabel(self._frame_container)
        self._zoom_hint.setPixmap(icon_pixmap("maximize", tokens.TEXT_TERTIARY, 14))
        self._zoom_hint.setToolTip("点击放大 (⤢)")
        self._zoom_hint.move(296, 8)
        self._zoom_hint.setVisible(False)

        layout.addWidget(self._frame_container, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)
        return container

    def _build_info_area(self) -> QWidget:
        """右栏：基本信息 + 状态 chips + 内容 + 折叠原始数据。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 1. 基本信息（键值网格）
        info_grid = QGridLayout()
        info_grid.setHorizontalSpacing(16)
        info_grid.setVerticalSpacing(4)

        def add_info(row: int, col: int, label_text: str) -> QLabel:
            lab = QLabel(label_text)
            set_text_role(lab, "secondary")
            val = QLabel("-")
            set_text_role(val, "mono")
            info_grid.addWidget(lab, row, col * 2)
            info_grid.addWidget(val, row, col * 2 + 1)
            return val

        self.ts_label = add_info(0, 0, "时间")
        self.duration_label = add_info(0, 1, "时长")
        self.type_label = add_info(1, 0, "类型")
        self.window_label = add_info(1, 1, "窗口")
        self.window_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.id_label = QLabel("-")
        set_text_role(self.id_label, "mono")
        id_caption = QLabel("ID")
        set_text_role(id_caption, "secondary")
        info_grid.addWidget(id_caption, 2, 0)
        info_grid.addWidget(self.id_label, 2, 1)

        layout.addLayout(info_grid)

        # 2. 状态 chips（3 个 checkable QPushButton）
        chips_row = QHBoxLayout()
        chips_row.setContentsMargins(0, 4, 0, 4)
        chips_row.setSpacing(6)

        chips_caption = QLabel("状态标记")
        set_text_role(chips_caption, "secondary")
        chips_row.addWidget(chips_caption)

        self._chips: dict[str, QPushButton] = {}
        chip_specs = [
            ("key", "标关键", "star", tokens.BLOCK_KEY),
            ("anomaly", "标异常", "alert-triangle", tokens.ICON_DANGER),
            ("automatable", "标可自动化", "bot", tokens.ICON_ACTIVE),
        ]
        for name, text, icon_name, color in chip_specs:
            btn = QPushButton(text)
            btn.setIcon(icon(icon_name, color))
            btn.setCheckable(True)
            btn.setToolTip(f"{text}（点击切换）")
            btn.setEnabled(False)
            btn.toggled.connect(
                lambda checked, n=name: self._on_chip_toggled(n, checked)
            )
            chips_row.addWidget(btn)
            self._chips[name] = btn

        chips_row.addStretch(1)
        layout.addLayout(chips_row)

        # 3. 内容区（可编辑 QPlainTextEdit）
        content_caption = QLabel("内容")
        set_text_role(content_caption, "secondary")
        layout.addWidget(content_caption)

        # text_edit 保留为公开属性（测试与外部按名访问）
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlaceholderText("块文本内容（转写/注释）")
        layout.addWidget(self.text_edit, stretch=1)

        # 4. 原始数据折叠区
        self._raw_toggle = QToolButton()
        self._raw_toggle.setText("▸ 原始数据")
        self._raw_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._raw_toggle.setCheckable(True)
        self._raw_toggle.toggled.connect(self._on_raw_toggle)
        self._raw_toggle.setVisible(False)
        layout.addWidget(self._raw_toggle)

        self._raw_edit = QPlainTextEdit()
        self._raw_edit.setReadOnly(True)
        self._raw_edit.setMaximumHeight(160)
        self._raw_edit.setVisible(False)
        layout.addWidget(self._raw_edit)

        return container

    # ===== 公开 API =====

    def show_block(self, block: dict[str, Any], package_path: Path) -> None:
        """显示选中块的详情。"""
        self._block = block
        self._package_path = package_path

        # 基本信息
        self.id_label.setText(block.get("id", "-"))
        self.type_label.setText(block.get("type", "-"))
        ts = block.get("timestamp", 0.0)
        self.ts_label.setText(f"{ts:.3f}s")
        dur = block.get("duration", 0.0)
        self.duration_label.setText(f"{dur:.3f}s")

        # 窗口名（focus_change 块有 primary.title）
        primary = block.get("primary", {})
        supplements = block.get("supplements", {})
        window_name = (
            primary.get("title", "")
            or primary.get("window", "")
            or supplements.get("window_title", "")
        )
        if window_name:
            self.window_label.setText(self._elide(str(window_name), 40))
            self.window_label.setToolTip(str(window_name))
        else:
            self.window_label.setText("-")
            self.window_label.setToolTip("")

        # 截图
        self._load_frame_paths(block, package_path)
        self._show_current_frame()

        # 状态 chips（与工具栏同步）
        status = block.get("status", {})
        is_trimmed = bool(status.get("is_trimmed", False))
        self._sync_chips(
            {
                "key": bool(status.get("marked_key", False)),
                "anomaly": bool(status.get("marked_anomaly", False)),
                "automatable": bool(status.get("marked_automatable", False)),
            }
        )
        # 已移除块禁用 chips（无标记意义）
        for btn in self._chips.values():
            btn.setEnabled(not is_trimmed)

        # 内容区
        self._populate_content(block)

        # 原始数据
        self._populate_raw(block)

    def sync_chips(self, status: dict[str, Any]) -> None:
        """外部（工具栏动作）触发后同步 chips 状态（不重发 chip_toggled）。"""
        self._sync_chips(
            {
                "key": bool(status.get("marked_key", False)),
                "anomaly": bool(status.get("marked_anomaly", False)),
                "automatable": bool(status.get("marked_automatable", False)),
            }
        )

    def set_chips_enabled(self, enabled: bool) -> None:
        """禁用/启用所有状态 chip（无选中块或 trimmed 块时禁用）。"""
        for btn in self._chips.values():
            btn.setEnabled(enabled)

    # ===== 内部：截图 =====

    def _load_frame_paths(self, block: dict[str, Any], package_path: Path) -> None:
        """收集块关联的所有帧路径（去重 + 按出现顺序排序）。"""
        primary = block.get("primary", {})
        supplements = block.get("supplements", {})
        candidates = [
            primary.get("frame_path", ""),
            supplements.get("before_frame", ""),
            supplements.get("after_frame", ""),
        ]
        seen: set[str] = set()
        paths: list[Path] = []
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            p = Path(candidate)
            full = p if p.is_absolute() else package_path / p
            if full.exists():
                paths.append(full)
        self._frame_paths = paths
        self._frame_index = 0

    def _show_current_frame(self) -> None:
        """显示当前索引的帧（缩略图）或 EmptyState 占位。"""
        if not self._frame_paths:
            # EmptyState 占位（components §12：图标 + 一句话现状）
            self.frame_label.setPixmap(icon_pixmap("image", tokens.TEXT_TERTIARY, 48))
            self.frame_label.setText("")
            self._frame_count_label.setText("本块无截图")
            set_text_role(self._frame_count_label, "tertiary")
            self._prev_frame_btn.setEnabled(False)
            self._next_frame_btn.setEnabled(False)
            self._zoom_hint.setVisible(False)
            self._frame_container.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            return

        path = self._frame_paths[self._frame_index]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.frame_label.setPixmap(QPixmap())
            self.frame_label.setText("[截图加载失败]")
            set_text_role(self.frame_label, "tertiary")
        else:
            scaled = pixmap.scaled(
                312,
                184,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.frame_label.setPixmap(scaled)
            self.frame_label.setText("")

        self._frame_count_label.setText(
            f"{self._frame_index + 1}/{len(self._frame_paths)} · {path.name}"
        )
        self._prev_frame_btn.setEnabled(self._frame_index > 0)
        self._next_frame_btn.setEnabled(self._frame_index < len(self._frame_paths) - 1)
        self._zoom_hint.setVisible(True)
        self._frame_container.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def _on_prev_frame(self) -> None:
        if self._frame_index > 0:
            self._frame_index -= 1
            self._show_current_frame()

    def _on_next_frame(self) -> None:
        if self._frame_index < len(self._frame_paths) - 1:
            self._frame_index += 1
            self._show_current_frame()

    def _on_frame_click(self) -> None:
        """点击截图区 → 请求打开查看器（02-editor §5.1）。"""
        if not self._frame_paths:
            return
        self.open_viewer_requested.emit(list(self._frame_paths), self._frame_index)

    # ===== 内部：chips =====

    def _sync_chips(self, states: dict[str, bool]) -> None:
        """同步 chips 状态（不触发 chip_toggled）。"""
        self._chips_syncing = True
        try:
            for name, checked in states.items():
                self._chips[name].setChecked(checked)
        finally:
            self._chips_syncing = False

    def _on_chip_toggled(self, name: str, checked: bool) -> None:
        if self._chips_syncing:
            return
        self.chip_toggled.emit(name, checked)

    # ===== 内部：内容 =====

    def _populate_content(self, block: dict[str, Any]) -> None:
        """填充内容区：STT/备注类块可编辑，其他显示只读文本（02-editor §5.2）。"""
        block_type = block.get("type", "")
        primary = block.get("primary", {})
        text = primary.get("text", "") or primary.get("name", "")

        editable = block_type in _EDITABLE_TYPES
        self.text_edit.setReadOnly(not editable)
        self.text_edit.setPlainText(str(text) if text else "")

    # ===== 内部：原始数据 =====

    def _populate_raw(self, block: dict[str, Any]) -> None:
        """填充原始数据区（默认收起，patterns §8）。"""
        primary = block.get("primary", {})
        supplements = block.get("supplements", {})

        extra_primary = {
            k: v
            for k, v in primary.items()
            if k not in _KNOWN_PRIMARY and v not in (None, "", [], {})
        }
        extra_supplements = {
            k: v
            for k, v in supplements.items()
            if k not in _KNOWN_SUPPLEMENTS and v not in (None, "", [], {})
        }
        extras: dict[str, Any] = {}
        if extra_primary:
            extras["primary"] = extra_primary
        if extra_supplements:
            extras["supplements"] = extra_supplements

        if not extras:
            self._raw_toggle.setVisible(False)
            self._raw_edit.setVisible(False)
            self._raw_toggle.setChecked(False)
            return

        rendered = json.dumps(extras, ensure_ascii=False, indent=2, default=str)
        if len(rendered) > 12_000:
            rendered = rendered[:12_000] + "\n...（详情过长，已截断）"
        self._raw_edit.setPlainText(rendered)
        self._raw_toggle.setVisible(True)
        # 默认收起
        self._raw_toggle.setChecked(False)
        self._raw_edit.setVisible(False)

    def _on_raw_toggle(self, checked: bool) -> None:
        """折叠/展开原始数据区。"""
        self._raw_edit.setVisible(checked)
        self._raw_toggle.setText("▾ 原始数据" if checked else "▸ 原始数据")

    # ===== 工具 =====

    @staticmethod
    def _elide(text: str, max_len: int) -> str:
        """长文本中间省略（patterns §12）。"""
        if len(text) <= max_len:
            return text
        half = max_len // 2
        return f"{text[:half]}…{text[-half:]}"
