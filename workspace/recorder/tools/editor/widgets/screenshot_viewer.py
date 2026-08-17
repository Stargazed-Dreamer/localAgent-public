"""截图查看器（02-editor §6）：QGraphicsView 缩放/平移/前后帧导航。

模态 QDialog，背景 ``BG_OVERLAY``。

能力：
- Ctrl+滚轮缩放（10%-800%，以光标为锚点）
- 左键拖拽平移（QGraphicsView.ScrollHandDrag）
- 双击 或 maximize 按钮：适应窗口 ↔ 100% 切换
- ←/→ 键 或 chevron 按钮：前/后帧导航，标题显示 N/M
- Esc / x 按钮关闭

入口：``ScreenshotViewer(frame_paths, current_index, parent).exec()``
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from lib.ui import icon, tokens
from lib.ui.theme import set_text_role

# 缩放边界（02-editor §6）
MIN_ZOOM = 0.10
MAX_ZOOM = 8.00
# 每次滚轮缩放倍率
ZOOM_STEP = 1.15


class ScreenshotViewer(QDialog):
    """截图查看器：模态对话框，支持缩放/平移/前后帧导航。

    Args:
        frame_paths: 帧 图片路径列表（已去重排序）。
        current_index: 初始显示的帧索引（0-based）。
    """

    # 当前帧变化时发出（供调用方同步详情面板）
    frame_changed = Signal(int)

    def __init__(
        self,
        frame_paths: list[Path],
        current_index: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._frame_paths = list(frame_paths)
        self._index = max(0, min(current_index, len(self._frame_paths) - 1)) if self._frame_paths else 0
        self._fit_mode = True  # 适应窗口模式；双击切到 100%
        self._pixmap_item: QGraphicsPixmapItem | None = None

        self.setWindowTitle("截图查看器")
        self.setModal(True)
        # 覆盖层背景（深色），稍大于普通对话框
        self.resize(960, 720)
        self.setStyleSheet(f"background-color: {tokens.BG_OVERLAY};")

        self._build_ui()
        self._load_current_frame()
        # 在视图上安装事件过滤器，使 Ctrl+滚轮在图片上方也能缩放
        self._view.viewport().installEventFilter(self)

    # ===== UI 构建 =====

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部工具栏：帧标题 + 缩放控件 + 关闭
        toolbar = QToolBar(self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        layout.addWidget(toolbar)

        self._title_label = QLabel()
        set_text_role(self._title_label, "secondary")
        toolbar.addWidget(self._title_label)

        toolbar.addSeparator()

        # 缩小
        self._zoom_out_btn = QPushButton()
        self._zoom_out_btn.setIcon(icon("zoom-out", tokens.ICON_DEFAULT))
        self._zoom_out_btn.setToolTip("缩小 (Ctrl-滚轮)")
        self._zoom_out_btn.clicked.connect(self._zoom_out)
        toolbar.addWidget(self._zoom_out_btn)

        # 百分比标签
        self._zoom_label = QLabel("100%")
        set_text_role(self._zoom_label, "mono")
        self._zoom_label.setMinimumWidth(56)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        toolbar.addWidget(self._zoom_label)

        # 放大
        self._zoom_in_btn = QPushButton()
        self._zoom_in_btn.setIcon(icon("zoom-in", tokens.ICON_DEFAULT))
        self._zoom_in_btn.setToolTip("放大 (Ctrl+滚轮)")
        self._zoom_in_btn.clicked.connect(self._zoom_in)
        toolbar.addWidget(self._zoom_in_btn)

        # 适应/100% 切换
        self._fit_btn = QPushButton()
        self._fit_btn.setIcon(icon("maximize", tokens.ICON_DEFAULT))
        self._fit_btn.setToolTip("适应窗口 / 100% 切换 (双击)")
        self._fit_btn.clicked.connect(self._toggle_fit)
        toolbar.addWidget(self._fit_btn)

        toolbar.addSeparator()

        # 前一帧
        self._prev_btn = QPushButton()
        self._prev_btn.setIcon(icon("chevron-left", tokens.ICON_DEFAULT))
        self._prev_btn.setToolTip("上一帧 (←)")
        self._prev_btn.clicked.connect(self._prev_frame)
        toolbar.addWidget(self._prev_btn)

        # 后一帧
        self._next_btn = QPushButton()
        self._next_btn.setIcon(icon("chevron-right", tokens.ICON_DEFAULT))
        self._next_btn.setToolTip("下一帧 (→)")
        self._next_btn.clicked.connect(self._next_frame)
        toolbar.addWidget(self._next_btn)

        # 弹性间距
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        # 关闭
        self._close_btn = QPushButton()
        self._close_btn.setIcon(icon("x", tokens.ICON_DEFAULT))
        self._close_btn.setToolTip("关闭 (Esc)")
        self._close_btn.clicked.connect(self.accept)
        toolbar.addWidget(self._close_btn)

        # 中部：QGraphicsView
        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene, self)
        self._view.setBackgroundBrush(Qt.GlobalColor.black)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setFrameShape(QGraphicsView.Shape.NoFrame)
        layout.addWidget(self._view, stretch=1)

        self._update_nav_buttons()

    # ===== 帧加载与显示 =====

    def _load_current_frame(self) -> None:
        """加载当前索引的帧到场景。"""
        if not self._frame_paths:
            self._title_label.setText("（无截图）")
            if self._pixmap_item is not None:
                self._scene.removeItem(self._pixmap_item)
                self._pixmap_item = None
            return

        path = self._frame_paths[self._index]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            # 加载失败：保留占位
            self._title_label.setText(f"{path.name} · 第 {self._index + 1}/{len(self._frame_paths)} 帧 · [加载失败]")
        else:
            if self._pixmap_item is None:
                self._pixmap_item = self._scene.addPixmap(pixmap)
            else:
                self._pixmap_item.setPixmap(pixmap)
            self._title_label.setText(
                f"{path.name} · 第 {self._index + 1}/{len(self._frame_paths)} 帧"
            )
            if self._fit_mode:
                self._fit_to_view()
            self._update_zoom_label()

        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        """根据当前索引启用/禁用前后帧按钮。"""
        has_frames = bool(self._frame_paths)
        self._prev_btn.setEnabled(has_frames and self._index > 0)
        self._next_btn.setEnabled(has_frames and self._index < len(self._frame_paths) - 1)

    def _update_zoom_label(self) -> None:
        """刷新缩放百分比标签。"""
        zoom = self._view.transform().m11()  # 水平缩放因子
        self._zoom_label.setText(f"{int(round(zoom * 100))}%")

    # ===== 缩放 =====

    def _zoom_at(self, factor: float, anchor: QPointF | None = None) -> None:
        """以 anchor（视图坐标）为锚点缩放。"""
        if self._pixmap_item is None:
            return
        current = self._view.transform().m11()
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, current * factor))
        if abs(new_zoom / current - 1.0) < 1e-3:
            return  # 边界，不再调整

        if anchor is not None:
            scene_anchor = self._view.mapToScene(anchor.toPoint())
            self._view.scale(new_zoom / current, new_zoom / current)
            # 调整视图中心使锚点保持在原位置
            delta = scene_anchor - self._view.mapToScene(anchor.toPoint())
            self._view.centerOn(self._view.mapToScene(self._view.viewport().rect().center()) + delta)
        else:
            self._view.scale(new_zoom / current, new_zoom / current)

        self._fit_mode = False
        self._update_zoom_label()

    def _zoom_in(self) -> None:
        self._zoom_at(ZOOM_STEP)

    def _zoom_out(self) -> None:
        self._zoom_at(1.0 / ZOOM_STEP)

    def _fit_to_view(self) -> None:
        """适应窗口：缩放并居中使整图可见。"""
        if self._pixmap_item is None:
            return
        pixmap = self._pixmap_item.pixmap()
        if pixmap.isNull():
            return
        view_rect = self._view.viewport().rect()
        if view_rect.width() < 10 or view_rect.height() < 10:
            return
        sx = view_rect.width() / max(1, pixmap.width())
        sy = view_rect.height() / max(1, pixmap.height())
        scale = min(sx, sy)
        scale = max(MIN_ZOOM, min(MAX_ZOOM, scale))
        self._view.resetTransform()
        self._view.scale(scale, scale)
        self._view.centerOn(self._pixmap_item)
        self._fit_mode = True
        self._update_zoom_label()

    def _set_100_percent(self) -> None:
        """1:1 显示。"""
        self._view.resetTransform()
        if self._pixmap_item is not None:
            self._view.centerOn(self._pixmap_item)
        self._fit_mode = False
        self._update_zoom_label()

    def _toggle_fit(self) -> None:
        """适应窗口 ↔ 100% 切换。"""
        if self._fit_mode:
            self._set_100_percent()
        else:
            self._fit_to_view()

    # ===== 帧导航 =====

    def _prev_frame(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._load_current_frame()
            self.frame_changed.emit(self._index)

    def _next_frame(self) -> None:
        if self._index < len(self._frame_paths) - 1:
            self._index += 1
            self._load_current_frame()
            self.frame_changed.emit(self._index)

    def current_index(self) -> int:
        """当前帧索引（0-based）。"""
        return self._index

    def current_frame_path(self) -> Path | None:
        """当前帧路径。"""
        if 0 <= self._index < len(self._frame_paths):
            return self._frame_paths[self._index]
        return None

    # ===== 事件 =====

    def eventFilter(self, obj, event) -> bool:
        """拦截 QGraphicsView viewport 的滚轮事件：Ctrl+滚轮缩放，否则交给视图滚动。"""
        if obj is self._view.viewport() and event.type() == event.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta != 0:
                    factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
                    # 光标位置作为锚点（视图坐标）
                    cursor_pos = self._view.mapFromGlobal(event.globalPosition().toPoint())
                    self._zoom_at(factor, QPointF(cursor_pos))
                    return True  # 事件已处理，阻止视图滚动
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:
        """对话框空白处的 Ctrl+滚轮也缩放。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta != 0:
                factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
                cursor_pos = self._view.mapFrom(self, event.position().toPoint())
                self._zoom_at(factor, QPointF(cursor_pos))
                event.accept()
                return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Left:
            self._prev_frame()
            event.accept()
        elif key == Qt.Key.Key_Right:
            self._next_frame()
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self.accept()
            event.accept()
        elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
            self._zoom_in()
            event.accept()
        elif key == Qt.Key.Key_Minus:
            self._zoom_out()
            event.accept()
        elif key == Qt.Key.Key_0:
            self._set_100_percent()
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """双击空白处切换适应/100%。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_fit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化时若处于适应模式则重新适配。"""
        super().resizeEvent(event)
        if self._fit_mode:
            self._fit_to_view()
