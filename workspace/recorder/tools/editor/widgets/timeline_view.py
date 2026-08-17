"""中间时间轴：虚拟滚动块列表 + 4 列横向布局 + 状态边框 + 证据列缩略图。

使用 QListView + QStyledItemDelegate 实现虚拟滚动。
块行交互：单击选中 / Ctrl+多选 / Shift+范围选 / 右键菜单 / 双击编辑

证据列缩略图（02-editor §4）：
- 行高 36，缩略图 40×28 保持宽高比
- QThreadPool 异步加载（QImage 线程安全加载 → 主线程转 QPixmap）
- 未加载：``image`` 图标占位；加载失败：``alert-triangle`` 图标；无截图：留空
- 双击有截图的块 → 打开查看器（由 EditorWindow 处理）

视觉规范：docs/ui/style-guide.md（颜色全部引用 tokens）， 不含硬编码 hex。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate, QWidget

from lib.recorder.editor.apply import apply_annotation
from lib.recorder.editor.store import AnnotationStore
from lib.ui import icon_pixmap, tokens as T


def _block_summary(block: dict[str, Any]) -> str:
    """生成时间轴行的人类可读摘要。"""
    block_type = block.get("type", "")
    primary = block.get("primary", {})
    if block_type == "chapter":
        return str(primary.get("name", "")).strip() or "未命名章节"
    if block_type == "mouse_click":
        return f"点击 ({primary.get('x', 0)}, {primary.get('y', 0)})"
    if block_type == "keyboard_input":
        return f"输入: {primary.get('text', '')[:40]}"
    if block_type == "focus_change":
        title = str(primary.get("title", "")).strip()
        return f"切换窗口: {title}" if title else "切换窗口"
    if block_type == "mouse_drag":
        return "鼠标拖动"
    if block_type == "mouse_scroll":
        return "鼠标滚动"
    if block_type in ("screenshot", "reference_image"):
        return "截图"
    if block_type == "stt_transcript":
        return f"语音: {primary.get('text', '')[:40]}"
    if block_type == "user_note":
        return f"注释: {primary.get('text', '')[:40]}"
    if block_type == "reference_text":
        return f"参考文本: {primary.get('text', '')[:40]}"
    if block_type == "idle":
        return f"空闲 {block.get('duration', 0.0):.1f}s"
    return block_type or "未知事件"


def _resolve_frame_path(
    block: dict[str, Any], package_path: Path | None
) -> Path | None:
    """获取块的首个可用帧绝对路径（02-editor §4 缩略图用）。

    优先级：primary.frame_path > supplements.before_frame > supplements.after_frame。
    文件不存在的候选跳过；全部不存在返回 None。
    """
    primary = block.get("primary", {})
    supplements = block.get("supplements", {})
    for key in ("frame_path", "before_frame", "after_frame"):
        rel = primary.get(key, "") or supplements.get(key, "")
        if not rel:
            continue
        p = Path(rel)
        full = p if p.is_absolute() else (package_path / p if package_path else p)
        if full.exists():
            return full
    return None


class _ThumbnailSignals(QObject):
    """ThumbnailWorker 的信号载体（QObject 线程亲和性保证跨线程安全）。"""

    loaded = Signal(str, QImage)  # path_str, image（空 QImage 表示加载失败）


class ThumbnailWorker(QRunnable):
    """异步缩略图加载 worker（QImage 线程安全加载 → 信号回主线程）。

    QPixmap 不能在工作线程创建，故先加载 QImage（线程安全），
    到主线程后由 ThumbnailCache 转为 QPixmap。
    """

    def __init__(self, frame_path: Path, size: QSize) -> None:
        super().__init__()
        self._frame_path = frame_path
        self._size = size
        self.signals = _ThumbnailSignals()

    def run(self) -> None:  # noqa: D401 - QRunnable API
        image = QImage(str(self._frame_path))
        if image.isNull():
            self.signals.loaded.emit(str(self._frame_path), QImage())
            return
        scaled = image.scaled(
            self._size.width(),
            self._size.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.signals.loaded.emit(str(self._frame_path), scaled)


class ThumbnailCache(QObject):
    """缩略图异步加载缓存（02-editor §4）。

    - ``get(path)`` 返回缓存的 QPixmap，未加载返回 None
    - ``request(path)`` 触发异步加载（已缓存/已失败/加载中时跳过）
    - 加载完成后发出 ``thumbnail_loaded(path_str)`` 信号，调用方应重绘可见区域
    """

    thumbnail_loaded = Signal(str)

    def __init__(self, size: QSize = QSize(40, 28), parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._cache: dict[str, QPixmap] = {}
        self._failed: set[str] = set()
        self._loading: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)

    def get(self, frame_path: Path) -> QPixmap | None:
        """返回缓存的缩略图，未加载返回 None。"""
        return self._cache.get(str(frame_path))

    def is_failed(self, frame_path: Path) -> bool:
        """是否加载失败。"""
        return str(frame_path) in self._failed

    def request(self, frame_path: Path) -> None:
        """请求异步加载缩略图（已缓存/已失败/加载中时跳过）。"""
        key = str(frame_path)
        if key in self._cache or key in self._failed or key in self._loading:
            return
        if not frame_path.exists():
            self._failed.add(key)
            return
        self._loading.add(key)
        worker = ThumbnailWorker(Path(key), self._size)
        worker.signals.loaded.connect(self._on_loaded)
        self._pool.start(worker)

    def _on_loaded(self, path_str: str, image: QImage) -> None:
        """工作线程完成 → 主线程转 QPixmap 并缓存。"""
        self._loading.discard(path_str)
        if image.isNull():
            self._failed.add(path_str)
        else:
            self._cache[path_str] = QPixmap.fromImage(image)
        self.thumbnail_loaded.emit(path_str)

    def clear(self) -> None:
        """清空缓存（切换录制包时调用）。"""
        self._cache.clear()
        self._failed.clear()
        self._loading.clear()


class TimelineModel(QAbstractListModel):
    """时间轴数据模型。"""

    def __init__(self, timeline: dict[str, Any], store: AnnotationStore) -> None:
        super().__init__()
        self._timeline = timeline
        self._store = store
        self._refresh_blocks()

    def _refresh_blocks(self) -> None:
        """apply 所有非 undone annotation 到 blocks。"""
        blocks = self._timeline.get("blocks", [])
        for ann in self._store.active_annotations:
            blocks = apply_annotation(blocks, ann)
        # 编辑器保留软移除块，用户需要再次选中它才能执行恢复。
        # L4 merge 仍会过滤这些块，不影响最终消费视图。
        self._blocks = blocks

    def refresh(self) -> None:
        """刷新数据。"""
        self.beginResetModel()
        self._refresh_blocks()
        self.endResetModel()

    def set_timeline(self, timeline: dict[str, Any]) -> None:
        """替换底层时间轴并刷新模型。"""
        self.beginResetModel()
        self._timeline = timeline
        self._refresh_blocks()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._blocks)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._blocks)):
            return None
        block = self._blocks[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return block
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{block.get('id', '')}，{block.get('timestamp', 0.0):.1f}s，{_block_summary(block)}"
        if role == Qt.ItemDataRole.UserRole:
            return block.get("id", "")
        return None

    def block_at(self, row: int) -> dict | None:
        if 0 <= row < len(self._blocks):
            return self._blocks[row]
        return None

    def find_row(self, block_id: str) -> int:
        for i, b in enumerate(self._blocks):
            if b.get("id") == block_id:
                return i
        return -1

    def blocks(self) -> list[dict[str, Any]]:
        """返回当前已应用 annotation 的编辑视图。"""
        return list(self._blocks)


class BlockDelegate(QStyledItemDelegate):
    """块行绘制：4 列横向布局 + 状态边框（D015 叠加规则）+ 证据列缩略图。

    颜色全部引用 tokens（参见 docs/ui/style-guide.md §2.6 领域色）。
    """

    # 块类型配色（tokens.BLOCK_COLORS 映射；reference_* 与 stt_* 是 timeline 实际 type，
    # 与 BLOCK_COLORS 的 key 对齐）
    TYPE_COLORS = {
        "mouse_click": QColor(T.BLOCK_COLORS["mouse_click"]),
        "mouse_scroll": QColor(T.BLOCK_COLORS["mouse_scroll"]),
        "mouse_drag": QColor(T.BLOCK_COLORS["mouse_drag"]),
        "keyboard_input": QColor(T.BLOCK_COLORS["keyboard"]),
        "focus_change": QColor(T.BLOCK_COLORS["focus"]),
        "idle": QColor(T.BLOCK_COLORS["idle"]),
        "screenshot": QColor(T.BLOCK_COLORS["screenshot"]),
        "reference_image": QColor(T.BLOCK_COLORS["screenshot"]),
        "stt_transcript": QColor(T.BLOCK_COLORS["stt_segment"]),
        "user_note": QColor(T.BLOCK_COLORS["user_note"]),
        "reference_text": QColor(T.BLOCK_COLORS["user_note"]),
        "chapter": QColor(T.TEXT_PRIMARY),
    }

    STATE_BORDER_COLORS = {
        "marked_anomaly": QColor(T.BLOCK_ANOMALY),    # 红
        "marked_key": None,                            # 同色相深一档（动态计算）
        "marked_automatable": QColor(T.BLOCK_AUTOMABLE),  # 青虚线
    }

    # 证据列缩略图尺寸（02-editor §4）
    THUMBNAIL_SIZE = QSize(40, 28)
    # 行高（02-editor §4：36px）
    ROW_HEIGHT = 36
    # 证据列宽（02-editor §4：56px）
    EVIDENCE_W = 56
    # 时间戳列宽
    TIME_W = 78

    def __init__(
        self,
        thumbnail_cache: ThumbnailCache | None = None,
        package_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumbnail_cache = thumbnail_cache
        self._package_path = package_path

    def set_package_path(self, path: Path | None) -> None:
        """更新录制包路径（用于解析帧相对路径）。"""
        self._package_path = path

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        block = index.data(Qt.ItemDataRole.DisplayRole)
        if not block:
            return

        painter.save()

        status = block.get("status", {})

        # 背景：章节分组、软移除和选中态分层显示（颜色全部来自 tokens）。
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(T.ACCENT_WASH))
        elif status.get("is_trimmed"):
            painter.fillRect(option.rect, QColor(T.BG_PANEL))
        elif block.get("type") == "chapter":
            painter.fillRect(option.rect, QColor(T.BG_CARD))
        else:
            painter.fillRect(option.rect, QColor(T.BG_BASE))

        # 类型色条 + 状态边框（D015：anomaly > key > automatable）。
        border_x = option.rect.left()
        border_w = 3
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.TYPE_COLORS.get(block.get("type", ""), QColor(T.TEXT_TERTIARY)))
        painter.drawRect(border_x, option.rect.top(), border_w, option.rect.height())
        border_x += border_w + 1
        if status.get("marked_anomaly"):
            painter.setBrush(QColor(T.BLOCK_ANOMALY))
            painter.drawRect(border_x, option.rect.top(), border_w, option.rect.height())
            border_x += border_w + 1
        if status.get("marked_key"):
            base_color = self.TYPE_COLORS.get(block.get("type", ""), QColor(T.ACCENT))
            darker = base_color.darker(150)
            painter.setBrush(darker)
            painter.drawRect(border_x, option.rect.top(), border_w, option.rect.height())
            border_x += border_w + 1
        if status.get("marked_automatable"):
            pen = QPen(QColor(T.BLOCK_AUTOMABLE), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(border_x, option.rect.top(), border_w, option.rect.height())
            border_x += border_w + 1

        # 4 列内容：时间戳 / 动作主题 / 视觉证据 / 说明
        rect = option.rect.adjusted(border_x - option.rect.left() + 8, 0, -10, 0)
        time_w = self.TIME_W
        evidence_w = self.EVIDENCE_W
        remaining = max(0, rect.width() - time_w - evidence_w - 24)
        event_w = int(remaining * 0.6)
        note_w = remaining - event_w
        event_x = rect.left() + time_w + 8
        evidence_x = event_x + event_w + 8
        note_x = evidence_x + evidence_w + 8

        font = painter.font()
        font.setStrikeOut(status.get("is_trimmed", False))
        font.setBold(block.get("type") == "chapter")
        painter.setFont(font)
        metrics = painter.fontMetrics()

        is_trimmed = bool(status.get("is_trimmed", False))

        def draw_elided(column: QRect, text: str, color: QColor) -> None:
            # 已移除块统一降到 50% 透明度（02-editor §3）
            if is_trimmed:
                color = QColor(color)
                color.setAlpha(128)
            painter.setPen(color)
            painter.drawText(
                column,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, column.width())),
            )

        # 列 1：时间戳
        ts = block.get("timestamp", 0.0)
        draw_elided(QRect(rect.left(), rect.top(), time_w, rect.height()),
                    f"{ts:.1f}s", QColor(T.TEXT_TERTIARY))

        # 列 2：动作主题
        block_type = block.get("type", "")
        summary = _block_summary(block)
        if is_trimmed:
            summary = f"[已移除] {summary}"
        summary_color = QColor(T.ACCENT) if block_type == "chapter" else QColor(T.TEXT_PRIMARY)
        draw_elided(QRect(event_x, rect.top(), event_w, rect.height()), summary, summary_color)

        # 列 3：视觉证据（缩略图，02-editor §4）
        self._paint_evidence(painter, block, QRect(evidence_x, rect.top(), evidence_w, rect.height()))

        # 列 4：说明（转写/注释）
        note = ""
        if block_type in ("stt_transcript", "user_note", "reference_text"):
            note = str(block.get("primary", {}).get("text", ""))
        draw_elided(QRect(note_x, rect.top(), note_w, rect.height()),
                    note, QColor(T.BLOCK_COLORS["stt_segment"]))

        painter.setPen(QPen(QColor(T.BORDER), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        painter.restore()

    def _paint_evidence(
        self, painter: QPainter, block: dict[str, Any], rect: QRect
    ) -> None:
        """绘制证据列：缩略图 / 占位图标 / 失败图标 / 留空。"""
        frame_path = _resolve_frame_path(block, self._package_path)
        if frame_path is None:
            # 无截图块：留空（02-editor §4：不再显示"有截图/无截图"文字）
            return

        if self._thumbnail_cache is None:
            self._draw_evidence_icon(painter, rect, "image", T.TEXT_TERTIARY)
            return

        thumb = self._thumbnail_cache.get(frame_path)
        if thumb is not None:
            # 已加载：绘制缩略图居中
            x = rect.left() + (rect.width() - thumb.width()) // 2
            y = rect.top() + (rect.height() - thumb.height()) // 2
            painter.drawPixmap(x, y, thumb)
            return

        if self._thumbnail_cache.is_failed(frame_path):
            # 加载失败：alert-triangle 图标
            self._draw_evidence_icon(painter, rect, "alert-triangle", T.ICON_WARNING)
            return

        # 未加载：image 图标占位 + 触发异步加载
        self._draw_evidence_icon(painter, rect, "image", T.TEXT_TERTIARY)
        self._thumbnail_cache.request(frame_path)

    @staticmethod
    def _draw_evidence_icon(
        painter: QPainter, rect: QRect, icon_name: str, color: str
    ) -> None:
        """在证据列居中绘制图标。"""
        size = T.ICON_SIZE_LG  # 20px
        pm = icon_pixmap(icon_name, color, size)
        x = rect.left() + (rect.width() - pm.width()) // 2
        y = rect.top() + (rect.height() - pm.height()) // 2
        painter.drawPixmap(x, y, pm)

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        """稳定行高 36px（02-editor §4），避免不同内容导致列表抖动。"""
        return QSize(option.rect.width(), self.ROW_HEIGHT)


class TimelineView(QListView):
    """时间轴视图（中间，虚拟滚动）。"""

    block_selected = Signal(str)
    block_double_clicked = Signal(str)
    selection_changed = Signal()

    def __init__(
        self,
        timeline: dict[str, Any],
        store: AnnotationStore,
        package_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._package_path = package_path
        self._thumbnail_cache = ThumbnailCache(BlockDelegate.THUMBNAIL_SIZE, self)
        self._model = TimelineModel(timeline, store)
        self.setModel(self._model)
        self._delegate = BlockDelegate(self._thumbnail_cache, package_path, self)
        self.setItemDelegate(self._delegate)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.doubleClicked.connect(self._on_double_clicked)
        # 缩略图加载完成 → 重绘视口（虚拟滚动下只 paint 可见行）
        self._thumbnail_cache.thumbnail_loaded.connect(self._on_thumbnail_loaded)

    def _on_current_changed(self, index: QModelIndex, previous: QModelIndex) -> None:
        """当前项变化时同步详情，覆盖鼠标、键盘和程序化跳转。"""
        block = self._model.block_at(index.row())
        if block:
            self.block_selected.emit(block["id"])

    def _on_selection_changed(self, selected, deselected) -> None:
        """UIA 只改 selection 时，把新单选项同步为 current。"""
        indexes = self.selectedIndexes()
        current = self.currentIndex()
        if indexes and (not current.isValid() or current not in indexes):
            self.selectionModel().setCurrentIndex(
                indexes[0],
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        self.selection_changed.emit()

    def _on_double_clicked(self, index: QModelIndex) -> None:
        block = self._model.block_at(index.row())
        if block:
            self.block_double_clicked.emit(block["id"])

    def _on_thumbnail_loaded(self, path_str: str) -> None:
        """缩略图加载完成 → 重绘可见区域。"""
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    def refresh(self) -> None:
        """刷新视图（apply annotation 后调用）。"""
        selected_ids = self.selected_block_ids()
        current_id = self.selected_block_id()
        self._model.refresh()
        selection_model = self.selectionModel()
        if selection_model is None:
            return

        for block_id in selected_ids:
            row = self._model.find_row(block_id)
            if row >= 0:
                selection_model.select(
                    self._model.index(row),
                    QItemSelectionModel.SelectionFlag.Select,
                )
        if current_id:
            row = self._model.find_row(current_id)
            if row >= 0:
                selection_model.setCurrentIndex(
                    self._model.index(row),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )

    def set_timeline(self, timeline: dict[str, Any]) -> None:
        """替换时间轴数据，同时尽量保留当前选择。"""
        current_id = self.selected_block_id()
        self._model.set_timeline(timeline)
        if current_id:
            self.scroll_to_block(current_id)
        if self.selected_block_id() is None:
            self.select_first_block()

    def set_package_path(self, path: Path | None) -> None:
        """更新录制包路径 + 清空缩略图缓存（切换包时调用）。"""
        self._package_path = path
        self._delegate.set_package_path(path)
        self._thumbnail_cache.clear()
        self._model.refresh()
        viewport = self.viewport()
        if viewport is not None:
            viewport.update()

    def blocks(self) -> list[dict[str, Any]]:
        """当前编辑视图中的所有块。"""
        return self._model.blocks()

    def block_by_id(self, block_id: str) -> dict[str, Any] | None:
        """按 ID 查找当前编辑视图中的块。"""
        row = self._model.find_row(block_id)
        return self._model.block_at(row)

    def selected_block_id(self) -> str | None:
        """当前块 ID；多选时以 current index 为主块。"""
        current = self.currentIndex()
        if current.isValid():
            block = self._model.block_at(current.row())
            if block:
                return block["id"]
        indexes = self.selectedIndexes()
        if not indexes:
            return None
        block = self._model.block_at(indexes[0].row())
        return block["id"] if block else None

    def selected_block_ids(self) -> list[str]:
        """所有选中的块 ID，current index 排在首位。"""
        current_id = self.selected_block_id()
        ids = [current_id] if current_id else []
        for idx in self.selectedIndexes():
            block = self._model.block_at(idx.row())
            if block and block["id"] not in ids:
                ids.append(block["id"])
        return ids

    def scroll_to_block(self, block_id: str) -> None:
        """滚动到指定块。"""
        row = self._model.find_row(block_id)
        if row >= 0:
            index = self._model.index(row)
            self.scrollTo(index)
            self.setCurrentIndex(index)

    def select_first_block(self) -> None:
        """选择首个可用块。"""
        if self._model.rowCount() > 0:
            self.setCurrentIndex(self._model.index(0))
