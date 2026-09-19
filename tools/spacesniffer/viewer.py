"""SpaceSniffer 复刻主窗口：treemap 画布、面包屑导航、过滤、标记、详情面板。

数据来源只有一种 —— SpaceSniffer 导出的 `.sns` 快照。本工具是**快照浏览器**，
不扫描磁盘：原版的实时扫描、文件系统事件同步、NTFS ADS 扫描这类功能在离线快照
上没有意义，刻意不做。能复刻的是它全部的分析类操作。

界面骨架（不走 .ui 文件，全部代码构建）::

    菜单栏 / 工具栏
    面包屑栏
    ┌─ TreemapView ───────────────┬─ DetailsPanel ──┐
    │  自绘方块图                  │  选中项信息      │
    │                             │  内容列表        │
    │                             │  类型分布        │
    └─────────────────────────────┴─────────────────┘
    状态栏：悬停项 | 卷容量 | 计数 | 过滤状态
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

# --- sys.path 引导（必须在导入同级模块之前） --------------------------------
# tools/ 与 tools/spacesniffer/ 都不是包（项目约定：tools 下无 __init__.py），
# 同级模块走扁平导入。这里同时保证 `lib.ui`、`tools.disk.recycle` 可导入。
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QPoint,
    QRectF,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import filetypes  # noqa: E402
import filtering  # noqa: E402
import report  # noqa: E402
import tagging  # noqa: E402
import treemap  # noqa: E402
from filtering import Filter, FilterError, FilterResult  # noqa: E402
from lib.ui import EmptyState, apply_theme, icon, icon_button, tokens  # noqa: E402
from lib.ui.theme import set_invalid, set_kind, set_text_role  # noqa: E402
from sns_model import Entry, Snapshot, human_size, parse_file  # noqa: E402

FILTER_DEBOUNCE_MS = 400  # 过滤/类型统计的防抖
RESIZE_DEBOUNCE_MS = 120  # 窗口拖动时的重排防抖
CHILD_ROW_LIMIT = 300  # 详情面板内容列表最多显示多少行
SNP_FILE_FILTER = "SpaceSniffer 快照 (*.sns);;所有文件 (*)"

_T = TypeVar("_T")  # 后台任务：work 的返回类型 → on_done 的参数类型


def _qcolor(value: str) -> QColor:
    return QColor(value)


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


class TaskWorker(QThread):
    """把纯计算丢到后台线程跑，避免阻塞界面。

    只用于**不碰 Qt 控件**的纯函数（解析、过滤、类型统计、写 CSV）。
    结果通过 `done(object)` 回主线程。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[], object], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:  # pragma: no cover - 线程体
        try:
            self.done.emit(self._work())
        except Exception as exc:  # noqa: BLE001 - 需要把任意后端错误回显给用户
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Treemap 画布
# ---------------------------------------------------------------------------


class TreemapView(QWidget):
    """自绘的方块图画布。

    只负责「画 + 命中」两件事，导航语义（缩放/选中）通过信号交给主窗口决定。
    瓦片列表由主窗口在视图变化时算好塞进来，`paintEvent` 只做一次顺序遍历。
    """

    hovered = Signal(object)  # Tile | None
    picked = Signal(object)  # Tile | None（左键单击）
    activated = Signal(object)  # Tile（左键双击）
    zoom_out_requested = Signal()
    blank_menu = Signal(object)  # QPoint（全局坐标）
    tile_menu = Signal(object, object)  # (Tile, QPoint 全局坐标)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._layout: Optional[treemap.TreemapLayout] = None
        self._hover: Optional[treemap.Tile] = None
        self._selected = None  # Entry | ViewNode | None
        self._hint = "打开一个 .sns 快照开始"
        self._truncated = False
        self._tag_mode = False

        self._font_label = QFont(self.font())
        self._font_label.setPixelSize(tokens.FONT_CAPTION)
        self._font_label.setWeight(QFont.Weight.DemiBold)
        self._font_dim = QFont(self.font())
        self._font_dim.setPixelSize(tokens.FONT_CAPTION)

        self._c_bg = _qcolor(tokens.BG_BASE)
        self._c_dir = _qcolor(tokens.TREEMAP_DIR_FILL)
        self._c_dir_head = _qcolor(tokens.TREEMAP_DIR_HEADER_FILL)
        self._c_gap = _qcolor(tokens.TREEMAP_GAP_FILL)
        self._c_label = _qcolor(tokens.TREEMAP_LABEL)
        self._c_label_dim = _qcolor(tokens.TREEMAP_LABEL_DIM)
        self._c_select = _qcolor(tokens.ACCENT)
        self._c_hover = _qcolor(tokens.ACCENT_BORDER)
        self._pen_border = QPen(_qcolor(tokens.TREEMAP_TILE_BORDER))
        self._pen_border.setWidth(1)
        self._pen_select = QPen(self._c_select)
        self._pen_select.setWidth(2)
        self._pen_hover = QPen(self._c_hover)
        self._pen_hover.setWidth(1)
        self._color_cache: Dict[str, QColor] = {}
        self._tag_cache: Dict[str, QColor] = {}

    # -- 外部接口 -----------------------------------------------------------

    def set_layout(self, layout: Optional[treemap.TreemapLayout]) -> None:
        self._layout = layout
        self._hover = None
        self.update()

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.update()

    def set_selected(self, node) -> None:
        self._selected = node
        self.update()

    def set_tag_mode(self, enabled: bool) -> None:
        self._tag_mode = enabled
        self.update()

    @property
    def selected(self):
        return self._selected

    # -- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._c_bg)
        layout = self._layout
        if layout is None or not layout.tiles:
            painter.setPen(self._c_label_dim)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._hint)
            painter.end()
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        selected_rect: Optional[QRectF] = None
        for tile in layout.tiles:
            self._paint_tile(painter, tile)
            if self._selected is not None and tile.entry is self._selected:
                selected_rect = tile.rect
        if selected_rect is not None:
            painter.setPen(self._pen_select)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selected_rect.adjusted(1, 1, -1, -1))
        if self._hover is not None:
            painter.setPen(self._pen_hover)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._hover.rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def _paint_tile(self, painter: QPainter, tile: treemap.Tile) -> None:
        rect = tile.rect
        entry = tile.entry
        if entry is None:  # 聚合块：没有对应条目，只有底色
            painter.fillRect(rect, self._c_gap)
            self._paint_gap_label(painter, tile)
        elif entry.is_dir:
            painter.fillRect(rect, self._c_dir)
            if tile.has_header:
                header = QRectF(rect.x(), rect.y(), rect.width(), treemap.HEADER_H)
                painter.fillRect(header, self._c_dir_head)
                metrics = QFontMetrics(self._font_label)
                text = metrics.elidedText(
                    tile.label, Qt.TextElideMode.ElideRight, int(rect.width()) - 8
                )
                painter.setFont(self._font_label)
                painter.setPen(self._c_label)
                painter.drawText(
                    header.adjusted(4, 0, -3, 0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    text,
                )
        else:
            painter.fillRect(rect, self._file_color(entry))

        painter.setPen(self._pen_border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(rect.x() + 0.5, rect.y() + 0.5, rect.width() - 1, rect.height() - 1))

        if entry is not None and entry.tag:
            painter.setPen(QPen(self._tag_color(entry.tag)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            if inner.width() > 0 and inner.height() > 0:
                painter.drawRect(inner)

        if entry is not None and not entry.is_dir:
            self._paint_file_label(painter, tile)

    def _paint_gap_label(self, painter: QPainter, tile: treemap.Tile) -> None:
        """给聚合块写上它是什么。

        整盘看下来，目录自身大小与子项之和常有几个百分点的差额（NTFS 元数据 `$MFT`
        等），在图上就是一块说不出名字的灰区 —— 原版这块也没有说明，实测很容易被
        误认为渲染 bug。够大就写出来，太小就留给 tooltip。
        """
        rect = tile.rect
        if rect.width() < treemap.LABEL_MIN_W * 2 or rect.height() < treemap.HEADER_MIN_H:
            return
        metrics = QFontMetrics(self._font_dim)
        painter.setFont(self._font_dim)
        painter.setPen(self._c_label_dim)
        painter.drawText(
            QRectF(rect.x() + 4, rect.y() + 3, rect.width() - 8, 13),
            int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
            metrics.elidedText(tile.label, Qt.TextElideMode.ElideRight, int(rect.width()) - 8),
        )

    def _paint_file_label(self, painter: QPainter, tile: treemap.Tile) -> None:
        entry = tile.entry
        if entry is None:
            return  # 只有聚合块会走到这里（调用方已过滤，这里是对类型做收窄）
        rect = tile.rect
        width = rect.width()
        height = rect.height()
        if width < treemap.LABEL_MIN_W or height < treemap.LABEL_MIN_H:
            return
        metrics = QFontMetrics(self._font_label)
        text = metrics.elidedText(tile.label, Qt.TextElideMode.ElideMiddle, int(width) - 6)
        painter.setFont(self._font_label)
        painter.setPen(self._c_label)
        row = QRectF(rect.x() + 3, rect.y() + 1, width - 6, treemap.LABEL_MIN_H)
        painter.drawText(
            row, int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft), text
        )
        if height >= 27:
            size_text = human_size(entry.size)
            dim = QFontMetrics(self._font_dim)
            painter.setFont(self._font_dim)
            painter.setPen(self._c_label_dim)
            painter.drawText(
                QRectF(rect.x() + 3, rect.y() + 14, width - 6, 12),
                int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
                dim.elidedText(size_text, Qt.TextElideMode.ElideRight, int(width) - 6),
            )

    def _file_color(self, entry) -> QColor:
        tag = entry.tag
        if self._tag_mode and tag:
            return self._tag_color(tag)
        ext = entry.ext
        cached = self._color_cache.get(ext)
        if cached is None:
            cached = _qcolor(filetypes.color_of(ext))
            self._color_cache[ext] = cached
        return cached

    def _tag_color(self, tag: str) -> QColor:
        cached = self._tag_cache.get(tag)
        if cached is None:
            cached = _qcolor(tagging.TAG_COLORS.get(tag, tokens.TEXT_TERTIARY))
            self._tag_cache[tag] = cached
        return cached

    # -- 交互 ---------------------------------------------------------------

    def _tile_at(self, pos: QPoint) -> Optional[treemap.Tile]:
        if self._layout is None:
            return None
        return self._layout.hit_test(float(pos.x()), float(pos.y()))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        tile = self._tile_at(event.position().toPoint())
        if tile is not self._hover:
            self._hover = tile
            self.hovered.emit(tile)
            self.update()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.hovered.emit(None)
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        tile = self._tile_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if tile is None:
                self.blank_menu.emit(event.globalPosition().toPoint())
            else:
                self.tile_menu.emit(tile, event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(tile)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        tile = self._tile_at(event.position().toPoint())
        if tile is not None:
            self.activated.emit(tile)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Backspace or key == Qt.Key.Key_Escape:
            self.zoom_out_requested.emit()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 面包屑
# ---------------------------------------------------------------------------


class BreadcrumbBar(QScrollArea):
    """可点击的路径条。点击任一段即缩放到该层。"""

    segment_clicked = Signal(object)  # Entry | ViewNode

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFixedHeight(tokens.CTRL_HEIGHT_LG)
        self._inner = QWidget()
        self._row = QHBoxLayout(self._inner)
        self._row.setContentsMargins(tokens.SPACE_SM, 0, tokens.SPACE_SM, 0)
        self._row.setSpacing(tokens.SPACE_XS)
        self._row.addStretch(1)
        self.setWidget(self._inner)

    def set_path(self, nodes: List) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            if item is None:  # 并发改动时布局可能已被清空
                break
            widget = item.widget()
            if widget is not None:
                # 必须 setParent(None) 立即摘除：只 deleteLater 的话控件会一直挂在
                # 父级上直到事件循环回收，期间旧路径与新路径会同时可见。
                widget.setParent(None)
                widget.deleteLater()
        for index, node in enumerate(nodes):
            if index:
                separator = QLabel("›")
                set_text_role(separator, "tertiary")
                self._row.addWidget(separator)
            button = QPushButton(node.name)
            button.setFlat(True)
            set_kind(button, "ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            full = node.full_path()
            button.setToolTip(full)
            if index == len(nodes) - 1:
                set_text_role(button, "title")
            button.clicked.connect(lambda _=False, n=node: self.segment_clicked.emit(n))
            self._row.addWidget(button)
        self._row.addStretch(1)
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().maximum())


# ---------------------------------------------------------------------------
# 详情面板
# ---------------------------------------------------------------------------


class DetailsPanel(QWidget):
    """右侧详情：选中项信息 + 内容列表 + 类型分布。"""

    entry_activated = Signal(object)
    entry_selected = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(320)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(tokens.SPACE_SM, tokens.SPACE_SM, tokens.SPACE_SM, tokens.SPACE_SM)
        outer.setSpacing(tokens.SPACE_SM)

        # --- 选中项 ---
        info_box = QGroupBox("选中项")
        info_layout = QVBoxLayout(info_box)
        info_layout.setSpacing(tokens.SPACE_XS)
        self._name_label = QLabel("未选中")
        set_text_role(self._name_label, "title")
        self._name_label.setWordWrap(True)
        info_layout.addWidget(self._name_label)

        self._path_label = QLabel("")
        set_text_role(self._path_label, "tertiary")
        self._path_label.setWordWrap(True)
        self._path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addWidget(self._path_label)

        self._facts_label = QLabel("")
        info_layout.addWidget(self._facts_label)
        outer.addWidget(info_box)

        # --- 内容列表 ---
        content_box = QGroupBox("内容")
        content_layout = QVBoxLayout(content_box)
        content_layout.setContentsMargins(
            tokens.SPACE_SM, tokens.SPACE_MD, tokens.SPACE_SM, tokens.SPACE_SM
        )
        self._children = QTreeWidget()
        self._children.setColumnCount(4)
        self._children.setHeaderLabels(["名称", "大小", "占比", "类型"])
        self._children.setRootIsDecorated(False)
        self._children.setAlternatingRowColors(True)
        self._children.setUniformRowHeights(True)
        header = self._children.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._children.itemDoubleClicked.connect(self._on_child_activated)
        self._children.itemSelectionChanged.connect(self._on_child_selected)
        content_layout.addWidget(self._children)
        self._content_hint = QLabel("")
        set_text_role(self._content_hint, "caption")
        content_layout.addWidget(self._content_hint)
        outer.addWidget(content_box, 3)

        # --- 类型分布（同时充当类型图例）---
        type_box = QGroupBox("类型分布")
        type_layout = QVBoxLayout(type_box)
        type_layout.setContentsMargins(
            tokens.SPACE_SM, tokens.SPACE_MD, tokens.SPACE_SM, tokens.SPACE_SM
        )
        self._types = QTreeWidget()
        self._types.setColumnCount(3)
        self._types.setHeaderLabels(["类型", "大小", "占比"])
        self._types.setRootIsDecorated(False)
        self._types.setAlternatingRowColors(True)
        self._types.setUniformRowHeights(True)
        theader = self._types.header()
        theader.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        theader.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        theader.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        type_layout.addWidget(self._types)
        outer.addWidget(type_box, 2)

    # -- 内容 ---------------------------------------------------------------

    def show_entry(self, node, focus_total: int, children: List) -> None:
        if node is None:
            self._name_label.setText("未选中")
            self._path_label.setText("点击方块图中的任意内容查看详情")
            self._facts_label.setText("")
            self._children.clear()
            self._content_hint.setText("")
            return

        entry = filtering.resolve(node)
        # 大小一律取「当前视图口径」= node.size：
        #   - 无过滤时节点就是 Entry，node.size == entry.size
        #   - 有过滤时是 ViewNode，size 按命中内容重算 —— 方块图的面积用的也是它
        # 之前这里用了 entry.size（全量口径）配上过滤后的 focus_total，过滤后会出现
        # 「占当前视图 54505%」这种数字，面板和图也对不上。
        shown_size = node.size
        self._name_label.setText(entry.name)
        set_text_role(self._name_label, "title")
        self._path_label.setText(entry.full_path())
        share = (shown_size / focus_total * 100) if focus_total else 0.0

        facts = [f"大小　{human_size(shown_size)}　（占当前视图 {share:.1f}%）"]
        if shown_size != entry.size:
            facts.append(f"全量　{human_size(entry.size)}　（未过滤时的口径）")
        if entry.is_dir:
            facts.append(f"目录　{entry.n_files:,} 文件 / {entry.n_dirs:,} 子目录")
        else:
            category = filetypes.category_of(entry.ext)
            facts.append(f"类型　{filetypes.label_of_category(category)}（{entry.type_label}）")
        if entry.mtime:
            facts.append(f"修改　{entry.mtime}")
        self._facts_label.setText("\n".join(facts))

        self._fill_children(node, children)
        self._style_facts(entry)

    def _style_facts(self, entry) -> None:
        if entry.tag:
            set_text_role(self._facts_label, "accent")
        else:
            set_text_role(self._facts_label, "secondary")

    def _fill_children(self, node, children: List) -> None:
        self._children.clear()
        source = children if children is not None else (node.children or [])
        # 分母同样用视图口径，否则过滤后各行占比之和会远小于 100%
        total = node.size or 1
        for child in source[:CHILD_ROW_LIMIT]:
            child_entry = filtering.resolve(child)
            row = QTreeWidgetItem(
                [
                    child_entry.name,
                    human_size(child.size),
                    f"{child.size / total * 100:.1f}%",
                    child_entry.type_label if not child_entry.is_dir else "目录",
                ]
            )
            row.setData(0, Qt.ItemDataRole.UserRole, child)
            row.setToolTip(0, child_entry.full_path())
            row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tag_color = tagging.TAG_COLORS.get(child_entry.tag or "", "")
            if tag_color:
                text_color = tagging.TAG_COLORS_TEXT.get(
                    child_entry.tag or "", tokens.TEXT_PRIMARY
                )
                row.setForeground(0, QBrush(_qcolor(text_color)))
            self._children.addTopLevelItem(row)
        if len(source) > CHILD_ROW_LIMIT:
            self._content_hint.setText(
                f"共 {len(source):,} 项，仅显示前 {CHILD_ROW_LIMIT} 项"
            )
        elif source:
            self._content_hint.setText(f"共 {len(source):,} 项")
        else:
            self._content_hint.setText("该位置没有子项")

    def show_types(self, distribution: List[Tuple[str, int, int]], pending: bool = False) -> None:
        self._types.clear()
        if pending:
            placeholder = QTreeWidgetItem(["正在统计…", "", ""])
            set_text_role(self._types, "tertiary")
            self._types.addTopLevelItem(placeholder)
            return
        total = max(sum(item[1] for item in distribution), 1)
        for category, size, count in distribution:
            row = QTreeWidgetItem(
                [
                    filetypes.label_of_category(category),
                    human_size(size),
                    f"{size / total * 100:.1f}%",
                ]
            )
            row.setIcon(0, _swatch_icon(filetypes.color_of_category(category)))
            colour = tokens.FILE_TYPE_COLORS.get(category, tokens.FILE_TYPE_OTHER)
            row.setToolTip(
                0,
                f"{filetypes.label_of_category(category)}　{count:,} 个文件\n"
                f"代表后缀：{'、'.join(filetypes.sub_extensions(category)) or '—'}",
            )
            row.setForeground(0, _brush(colour))
            row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._types.addTopLevelItem(row)

    # -- 事件 ---------------------------------------------------------------

    def _on_child_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if node is not None:
            self.entry_activated.emit(node)

    def _on_child_selected(self) -> None:
        items = self._children.selectedItems()
        if items:
            node = items[0].data(0, Qt.ItemDataRole.UserRole)
            if node is not None:
                self.entry_selected.emit(node)


def _brush(color: str):
    return QBrush(_qcolor(color))


def _swatch_icon(color: str):
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.fillRect(0, 0, 12, 12, _qcolor(color))
    painter.setPen(QPen(_qcolor(tokens.TREEMAP_TILE_BORDER)))
    painter.drawRect(0, 0, 11, 11)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class SpaceSnifferWindow(QMainWindow):
    """快照浏览器主窗口。"""

    def __init__(self, snapshot_path: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("SpaceSniffer 快照浏览器")
        self.setMinimumSize(960, 640)

        self._snapshot: Optional[Snapshot] = None
        self._view_root = None  # 视图树根（Entry 或 ViewNode）
        self._current = None  # 当前聚焦节点
        self._selected = None
        self._filter = Filter("")
        self._last_dir = str(Path.home())
        self._workers: List[TaskWorker] = []
        self._type_cache: Dict[int, List[Tuple[str, int, int]]] = {}
        self._types_dirty = True

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filter_from_box)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._resize_timer.timeout.connect(self._rebuild_layout)

        self._build_actions()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        if snapshot_path:
            QTimer.singleShot(0, lambda: self.load_snapshot(snapshot_path))

    # -- 构建 ---------------------------------------------------------------

    def _build_actions(self) -> None:
        self.act_open = QAction(icon("folder"), "打开快照…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.setToolTip("打开 .sns 快照 (Ctrl+O)")
        self.act_open.setStatusTip("选择一个 SpaceSniffer 导出的 .sns 快照文件")
        self.act_open.triggered.connect(self.on_open)

        self.act_reload = QAction(icon("refresh"), "重新加载", self)
        self.act_reload.setShortcut("F5")
        self.act_reload.setToolTip("重新加载当前快照 (F5)")
        self.act_reload.setStatusTip("重新解析当前 .sns 文件，丢弃全部标记")
        self.act_reload.setEnabled(False)
        self.act_reload.triggered.connect(self.on_reload)

        self.act_export_text = QAction(icon("file-text"), "导出文本报告…", self)
        self.act_export_text.setShortcut("Ctrl+E")
        self.act_export_text.setToolTip("导出文本报告 (Ctrl+E)")
        self.act_export_text.setStatusTip("导出当前视图的摘要报告（Top N 目录/文件、类型分布）")
        self.act_export_text.triggered.connect(self.on_export_text)

        self.act_export_csv = QAction(icon("upload"), "导出 CSV 明细…", self)
        self.act_export_csv.setShortcut("Ctrl+Shift+E")
        self.act_export_csv.setToolTip("导出 CSV 明细 (Ctrl+Shift+E)")
        self.act_export_csv.setStatusTip("把当前视图下的每一项写成 CSV（含路径/大小/类型/时间/标记）")
        self.act_export_csv.triggered.connect(self.on_export_csv)

        self.act_quit = QAction("退出", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.setStatusTip("关闭程序；标记不会保存")
        self.act_quit.triggered.connect(self.close)

        self.act_zoom_up = QAction(icon("chevron-left"), "返回上一层", self)
        self.act_zoom_up.setShortcut("Alt+Up")
        self.act_zoom_up.setToolTip("返回上一层 (Alt+Up / Backspace)")
        self.act_zoom_up.setStatusTip("把视图放大到当前目录的父目录")
        self.act_zoom_up.setEnabled(False)
        self.act_zoom_up.triggered.connect(self.zoom_up)

        self.act_zoom_root = QAction(icon("home"), "回到根目录", self)
        self.act_zoom_root.setShortcut("Alt+Home")
        self.act_zoom_root.setToolTip("回到根目录 (Alt+Home)")
        self.act_zoom_root.setStatusTip("把视图重置到卷根")
        self.act_zoom_root.setEnabled(False)
        self.act_zoom_root.triggered.connect(self.zoom_root)

        self.act_tag_mode = QAction(icon("star"), "标签着色", self)
        self.act_tag_mode.setCheckable(True)
        self.act_tag_mode.setToolTip("标签着色 (Ctrl+T)")
        self.act_tag_mode.setStatusTip("开启后，打了标记的方块改用标记色显示")
        self.act_tag_mode.toggled.connect(self._on_tag_mode_toggled)

        self.act_clear_tags = QAction(icon("x"), "清除全部标记", self)
        self.act_clear_tags.setStatusTip("清除当前快照里的所有颜色标记")
        self.act_clear_tags.triggered.connect(self.on_clear_tags)

        self.act_filter_syntax = QAction(icon("info"), "过滤语法…", self)
        self.act_filter_syntax.setStatusTip("查看过滤框支持的语法")
        self.act_filter_syntax.triggered.connect(self.on_filter_help)

        self.act_shortcuts = QAction(icon("keyboard"), "快捷键…", self)
        self.act_shortcuts.setStatusTip("查看全部快捷键")
        self.act_shortcuts.triggered.connect(self.on_shortcuts)

        self.act_about = QAction("关于", self)
        self.act_about.setStatusTip("关于本工具")
        self.act_about.triggered.connect(self.on_about)

    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏", self)
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.addAction(self.act_open)
        bar.addAction(self.act_reload)
        bar.addSeparator()
        bar.addAction(self.act_zoom_up)
        bar.addAction(self.act_zoom_root)
        bar.addSeparator()

        label = QLabel("过滤")
        set_text_role(label, "secondary")
        bar.addWidget(label)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("例如  *.mp4;>500mb;<6months;|:green")
        # 不开 setClearButtonEnabled：它会在框内再塞一个平台原生清除按钮，
        # 与工具栏右侧那个图标按钮功能重复，暗色主题下样式也不受控。
        self.filter_edit.setMinimumWidth(280)
        self.filter_edit.setToolTip(
            "过滤表达式，分号分隔多个条件（AND）；\n"
            "掩码 *.jpg / 排除 |*.jpg / 大小 >500mb / 时间 <3months / 标记 :red / 名称 N:xxx\n"
            "回车立即应用 (Ctrl+F)"
        )
        self.filter_edit.textChanged.connect(lambda _: self._filter_timer.start())
        self.filter_edit.returnPressed.connect(self._apply_filter_from_box)
        bar.addWidget(self.filter_edit)

        self.filter_help_btn = icon_button("info", "过滤语法说明")
        self.filter_help_btn.clicked.connect(self.on_filter_help)
        bar.addWidget(self.filter_help_btn)

        clear_btn = icon_button("x", "清除过滤 (Esc 于过滤框)")
        clear_btn.clicked.connect(lambda: self.filter_edit.setText(""))
        bar.addWidget(clear_btn)

        bar.addSeparator()
        bar.addAction(self.act_tag_mode)
        bar.addAction(self.act_clear_tags)
        self.addToolBar(bar)

        self.act_filter_shortcut = QAction("聚焦过滤框", self)
        self.act_filter_shortcut.setShortcut("Ctrl+F")
        self.act_filter_shortcut.triggered.connect(self._focus_filter)
        self.addAction(self.act_filter_shortcut)

        for index, tag in enumerate(tagging.TAG_HOTKEY_ORDER):
            action = QAction(f"标记为{tagging.TAG_LABELS[tag]}", self)
            action.setShortcut(f"Ctrl+{index + 1}")
            action.setStatusTip(f"给选中项打{tagging.TAG_LABELS[tag]}色标记（再按一次取消）")
            action.triggered.connect(lambda _=False, t=tag: self.on_tag(t))
            self.addAction(action)

        menu_file = self.menuBar().addMenu("文件")
        menu_file.addAction(self.act_open)
        menu_file.addAction(self.act_reload)
        menu_file.addSeparator()
        menu_file.addAction(self.act_export_text)
        menu_file.addAction(self.act_export_csv)
        menu_file.addSeparator()
        menu_file.addAction(self.act_quit)

        menu_view = self.menuBar().addMenu("视图")
        menu_view.addAction(self.act_zoom_up)
        menu_view.addAction(self.act_zoom_root)
        menu_view.addSeparator()
        menu_view.addAction(self.act_tag_mode)
        menu_view.addAction(self.act_clear_tags)

        menu_help = self.menuBar().addMenu("帮助")
        menu_help.addAction(self.act_filter_syntax)
        menu_help.addAction(self.act_shortcuts)
        menu_help.addSeparator()
        menu_help.addAction(self.act_about)

    def _build_body(self) -> None:
        self.breadcrumb = BreadcrumbBar()
        self.breadcrumb.segment_clicked.connect(self.zoom_to)

        self.treemap_view = TreemapView()
        self.treemap_view.hovered.connect(self._on_hover)
        self.treemap_view.picked.connect(self._on_picked)
        self.treemap_view.activated.connect(self._on_activated)
        self.treemap_view.zoom_out_requested.connect(self.zoom_up)
        self.treemap_view.blank_menu.connect(self._on_blank_menu)
        self.treemap_view.tile_menu.connect(self._on_tile_menu)

        self.details = DetailsPanel()
        self.details.entry_activated.connect(self._on_activated_node)
        self.details.entry_selected.connect(self._on_details_selected)

        self.empty_state = EmptyState(
            "hard-drive",
            "尚未打开快照",
            "用 SpaceSniffer 扫一盘后导出 .sns，再点工具栏的「打开快照」；\n"
            "也可以直接把 .sns 文件拖进这个窗口，或作为命令行参数传入。",
        )

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        center_layout.addWidget(self.breadcrumb)
        canvas_wrap = QWidget()
        canvas_layout = QVBoxLayout(canvas_wrap)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.addWidget(self.treemap_view)
        canvas_layout.addWidget(self.empty_state)
        center_layout.addWidget(canvas_wrap, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(center)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        # 详情面板宽度由内容决定（列宽全是 ResizeToContents），给足初始值免得
        # 名称列被挤成 "Us…"；面板本身有 minimumWidth 兜底，不会被压没
        splitter.setSizes([1020, 400])
        self.setCentralWidget(splitter)

        self.setAcceptDrops(True)
        self._show_loaded(False)

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self._hover_label = QLabel("")
        set_text_role(self._hover_label, "secondary")
        bar.addWidget(self._hover_label, 1)

        self._filter_label = QLabel("")
        set_text_role(self._filter_label, "tertiary")
        bar.addPermanentWidget(self._filter_label)

        self._volume_label = QLabel("")
        set_text_role(self._volume_label, "mono")
        bar.addPermanentWidget(self._volume_label)

        self._count_label = QLabel("")
        set_text_role(self._count_label, "mono")
        bar.addPermanentWidget(self._count_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setMaximumWidth(140)
        self._progress.setVisible(False)
        bar.addPermanentWidget(self._progress)

    # -- 快照加载 -----------------------------------------------------------

    def load_snapshot(self, path: str) -> None:
        self._set_busy(True, "正在解析快照…")
        target = Path(path)

        def work() -> Snapshot:
            return parse_file(target)

        self._run_task(
            work,
            on_done=self._on_snapshot_loaded,
            on_error=lambda msg: self._on_task_error(f"解析快照失败：{msg}"),
            busy_message="正在解析快照…",
        )

    def _on_snapshot_loaded(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self._last_dir = str(snapshot.source_path.parent)
        self._type_cache.clear()
        self._filter = Filter("")
        self.filter_edit.blockSignals(True)
        self.filter_edit.clear()
        self.filter_edit.blockSignals(False)
        set_invalid(self.filter_edit, False)

        self._view_root = snapshot.root
        self._current = snapshot.root
        self._selected = snapshot.root

        title = f"SpaceSniffer 快照浏览器 — {snapshot.volume_label}  {snapshot.source_path.name}"
        self.setWindowTitle(title)

        self._volume_label.setText(
            f"已用 {human_size(snapshot.used_bytes)} | 可用 {human_size(snapshot.free_bytes)}"
        )
        self._count_label.setText(f"文件 {snapshot.n_files:,} | 目录 {snapshot.n_dirs:,}")

        if not snapshot.is_complete:
            self.statusBar().showMessage(
                f"注意：快照存在未扫描区域 {human_size(snapshot.unknown_bytes)}，结果可能不完整",
                8000,
            )
        else:
            size = snapshot.file_size
            self.statusBar().showMessage(
                f"已加载 {snapshot.source_path.name}"
                f"（{human_size(size) if size else '体积未知'}，"
                f"数据时点 {snapshot.scanned_at() or '未知'}）",
                6000,
            )

        self.act_reload.setEnabled(True)
        self.act_zoom_up.setEnabled(True)
        self.act_zoom_root.setEnabled(True)
        self._show_loaded(True)
        self._refresh_view()

    def _show_loaded(self, loaded: bool) -> None:
        self.treemap_view.setVisible(loaded)
        self.empty_state.setVisible(not loaded)
        self.breadcrumb.setVisible(loaded)
        self.filter_edit.setEnabled(loaded)
        if not loaded:
            self.treemap_view.set_layout(None)

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 SpaceSniffer 快照", self._last_dir, SNP_FILE_FILTER
        )
        if path:
            self.load_snapshot(path)

    def on_reload(self) -> None:
        if self._snapshot is None:
            return
        if not self._confirm_discard_tags():
            return
        self.load_snapshot(str(self._snapshot.source_path))

    def _confirm_discard_tags(self) -> bool:
        counts = tagging.count_tags(self._snapshot.root) if self._snapshot else {}
        total = sum(counts.values())
        if not total:
            return True
        answer = QMessageBox.question(
            self,
            "放弃当前标记？",
            f"当前有 {total} 个标记，重新加载会全部丢失。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # -- 视图刷新 -----------------------------------------------------------

    def _refresh_view(self) -> None:
        if self._snapshot is None:
            return
        self._rebuild_layout()
        self._refresh_breadcrumb()
        self._refresh_details()
        self._schedule_type_stats()

    def _rebuild_layout(self) -> None:
        if self._current is None:
            return
        size = self.treemap_view.size()
        layout = treemap.build_layout(
            self._current, float(size.width()), float(size.height())
        )
        self.treemap_view.set_layout(layout)
        self.treemap_view.set_selected(self._selected)
        if layout.truncated:
            self.statusBar().showMessage(
                "内容过于密集，已省略部分极小方块（放大后可看到）", 4000
            )
        self.act_zoom_up.setEnabled(self._current.parent is not None)

    def _refresh_breadcrumb(self) -> None:
        nodes: List = []
        node = self._current
        while node is not None:
            nodes.append(node)
            node = node.parent
        nodes.reverse()
        self.breadcrumb.set_path(nodes)

    def _refresh_details(self) -> None:
        if self._selected is None:
            self.details.show_entry(None, 1, [])
            return
        focus_size = self._current.size if self._current is not None else 1
        self.details.show_entry(
            self._selected, focus_size, list(self._selected.children or [])
        )

    # -- 类型统计（异步） ---------------------------------------------------

    def _schedule_type_stats(self) -> None:
        if self._current is None:
            return
        key = id(self._current)
        cached = self._type_cache.get(key)
        if cached is not None:
            self.details.show_types(cached)
            return
        node = self._current
        self.details.show_types([], pending=True)

        def work() -> List[Tuple[str, int, int]]:
            return report.type_distribution(node)

        def done(distribution: List[Tuple[str, int, int]]) -> None:
            if len(self._type_cache) > 24:
                self._type_cache.clear()
            self._type_cache[id(node)] = distribution
            if self._current is node:
                self.details.show_types(distribution)

        self._run_task(
            work,
            on_done=done,
            on_error=lambda msg: self.statusBar().showMessage(f"类型统计失败：{msg}", 5000),
            quiet=True,
        )

    # -- 导航 ---------------------------------------------------------------

    @staticmethod
    def _descend_in_view(root, target):
        """在过滤视图树里找回与 target 对应的节点，按名称逐段下钻。

        过滤会整枝剪掉不命中的目录，目标有可能走不到 —— 此时停在最深的可达祖先，
        而不是把用户一脚踢回根目录。无过滤（视图树就是原树）时必然精确命中。
        """
        if root is None or target is None:
            return root
        names: List[str] = []
        node = target
        while node is not None:
            names.append(node.name)
            node = node.parent
        names.reverse()
        current = root
        for part in names[1:]:  # 首段是根名，root 已经是它
            found = None
            for child in current.children or ():
                if child.name == part:
                    found = child
                    break
            if found is None:
                break
            current = found
        return current

    def zoom_to(self, node) -> None:
        if node is None:
            return
        if not getattr(node, "is_dir", False):
            self._select(node)
            return
        self._current = node
        self._selected = node
        self._refresh_view()

    def zoom_up(self) -> None:
        if self._current is None:
            return
        parent = self._current.parent
        if parent is None:
            self.statusBar().showMessage("已经在根目录", 2500)
            return
        self.zoom_to(parent)

    def zoom_root(self) -> None:
        if self._view_root is None:
            return
        self.zoom_to(self._view_root)

    def _on_activated_node(self, node) -> None:
        """详情面板里双击一行：目录下钻，文件打开。"""
        if getattr(node, "is_dir", False):
            self.zoom_to(node)
        else:
            self._open_path(filtering.resolve(node).full_path())

    # -- 交互槽 -------------------------------------------------------------

    def _on_hover(self, tile) -> None:
        if tile is None or tile.entry is None:
            self._hover_label.setText("")
            return
        entry = filtering.resolve(tile.entry)
        focus_size = self._current.size if self._current is not None else 1
        share = entry.size / focus_size * 100 if focus_size else 0.0
        kind = "目录" if entry.is_dir else entry.type_label
        self._hover_label.setText(
            f"{entry.name}　{human_size(entry.size)}　{share:.1f}%　{kind}"
        )

    def _on_picked(self, tile) -> None:
        if tile is None or tile.entry is None:
            self._select(None)
            return
        self._select(tile.entry)

    def _on_activated(self, tile) -> None:
        if tile.entry is None:
            return
        entry = filtering.resolve(tile.entry)
        if entry.is_dir:
            self.zoom_to(tile.entry)
        else:
            self._open_path(entry.full_path())

    def _select(self, node) -> None:
        self._selected = node
        self.treemap_view.set_selected(node)
        self._refresh_details()

    def _on_details_selected(self, node) -> None:
        self._select(node)

    def _on_blank_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        action = menu.addAction(icon("chevron-left"), "返回上一层")
        action.setEnabled(self._current is not None and self._current.parent is not None)
        action.triggered.connect(self.zoom_up)
        home = menu.addAction(icon("home"), "回到根目录")
        home.setEnabled(self._view_root is not None)
        home.triggered.connect(self.zoom_root)
        menu.addSeparator()
        if self.filter_edit.text():
            clear = menu.addAction(icon("x"), "清除过滤")
            clear.triggered.connect(lambda: self.filter_edit.setText(""))
        export = menu.addAction(icon("file-text"), "导出文本报告…")
        export.triggered.connect(self.on_export_text)
        menu.exec(global_pos)

    def _on_tile_menu(self, tile, global_pos: QPoint) -> None:
        if tile.entry is None:
            self._on_blank_menu(global_pos)
            return
        entry = filtering.resolve(tile.entry)
        path = entry.full_path()
        exists = os.path.exists(path)

        menu = QMenu(self)
        if entry.is_dir:
            open_action = menu.addAction(icon("folder"), "在资源管理器中打开")
            open_action.setEnabled(exists)
            open_action.setStatusTip("用资源管理器打开该目录")
            open_action.triggered.connect(lambda: self._open_path(path))
        else:
            open_action = menu.addAction(icon("file-text"), "用默认程序打开")
            open_action.setEnabled(exists)
            open_action.setStatusTip("用系统关联的默认程序打开该文件")
            open_action.triggered.connect(lambda: self._open_path(path))

        reveal = menu.addAction(icon("target"), "在资源管理器中定位")
        reveal.setEnabled(exists)
        reveal.setStatusTip("打开所在目录并选中该项")
        reveal.triggered.connect(lambda: self._reveal_path(path))

        props = menu.addAction(icon("info"), "查看属性")
        props.setEnabled(exists)
        props.setStatusTip("打开 Windows 属性对话框")
        props.triggered.connect(lambda: self._show_properties(path))

        menu.addSeparator()
        here = menu.addAction(icon("hard-drive"), "以该目录为视图根")
        here.setEnabled(entry.is_dir)
        here.triggered.connect(lambda: self.zoom_to(tile.entry))

        copy_path = menu.addAction(icon("copy"), "复制完整路径")
        copy_path.setToolTip("复制完整路径 (Ctrl+C)")
        copy_path.triggered.connect(lambda: self._copy_text(path))
        copy_size = menu.addAction(icon("copy"), "复制大小")
        copy_size.triggered.connect(lambda: self._copy_text(human_size(entry.size)))

        menu.addSeparator()
        tag_menu = menu.addMenu(icon("star"), "标记颜色")
        for tag in tagging.TAG_HOTKEY_ORDER:
            label = tagging.TAG_LABELS[tag]
            hotkey = tagging.hotkey_of(tag)
            action = tag_menu.addAction(icon("star", tagging.TAG_COLORS[tag]), f"{label} ({hotkey})")
            action.setCheckable(True)
            action.setChecked(entry.tag == tag)
            action.triggered.connect(lambda _=False, t=tag: self._tag_entry(entry, t))
        if entry.tag:
            tag_menu.addSeparator()
            clear = tag_menu.addAction(icon("x"), "清除本项标记")
            clear.triggered.connect(lambda: self._tag_entry(entry, None))

        menu.addSeparator()
        delete_action = menu.addAction(icon("trash", tokens.ICON_DANGER), "删除到回收站…")
        delete_action.setEnabled(exists)
        delete_action.setStatusTip(
            "把该项移入回收站（可恢复）"
            if exists
            else "磁盘上已不存在该项（快照是历史时点的数据）"
        )
        delete_action.triggered.connect(lambda: self._delete_entry(path, entry.is_dir))

        menu.exec(global_pos)

    # -- 标记 ---------------------------------------------------------------

    def on_tag(self, tag: str) -> None:
        if self._selected is None:
            self.statusBar().showMessage("先选中一个方块再打标记", 3000)
            return
        self._tag_entry(filtering.resolve(self._selected), tag)

    def _tag_entry(self, entry: Entry, tag: Optional[str]) -> None:
        if tag is None:
            entry.tag = None
            self.statusBar().showMessage(f"已清除 {entry.name} 的标记", 3000)
        else:
            new_value = tagging.toggle_tag(entry, tag)
            if new_value is None:
                self.statusBar().showMessage(f"已取消 {entry.name} 的标记", 3000)
            else:
                self.statusBar().showMessage(
                    f"已把 {entry.name} 标为{tagging.TAG_LABELS[tag]}色"
                    f"（按 {tagging.hotkey_of(tag)} 取消；用 :{tag} 可筛出来）",
                    4000,
                )
        self._types_dirty = True
        self.treemap_view.set_selected(self._selected)
        self._refresh_details()
        self._refresh_tag_status()

    def on_clear_tags(self) -> None:
        if self._snapshot is None:
            return
        counts = tagging.count_tags(self._snapshot.root)
        total = sum(counts.values())
        if not total:
            self.statusBar().showMessage("当前没有标记", 2500)
            return
        answer = QMessageBox.question(
            self,
            "清除全部标记",
            f"确定清除全部 {total} 个颜色标记吗？标记只存在内存里，清除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        tagging.clear_tags(self._snapshot.root)
        self._refresh_details()
        self._refresh_tag_status()
        self.statusBar().showMessage(f"已清除 {total} 个标记", 3000)

    def _refresh_tag_status(self) -> None:
        """刷新状态栏的「过滤 / 标记」段。两个信息共用同一个常驻标签，

        所以统一在这里拼装 —— 之前试图从标签文本里反解前缀，改一次文案就崩。
        """
        parts: List[str] = []
        if not self._filter.is_empty:
            parts.append(f"过滤：{filtering.describe(self._filter)}")
        if self._snapshot is not None:
            counts = tagging.count_tags(self._snapshot.root)
            active = [f"{tagging.TAG_LABELS[k]} {v}" for k, v in counts.items() if v]
            if active:
                parts.append("标记 " + " / ".join(active))
        self._filter_label.setText("  |  ".join(parts))

    def _on_tag_mode_toggled(self, enabled: bool) -> None:
        self.act_tag_mode.setIcon(
            icon("star", tokens.ICON_ACTIVE if enabled else tokens.ICON_DEFAULT)
        )
        self.treemap_view.set_tag_mode(enabled)

    # -- 过滤 ---------------------------------------------------------------

    def _focus_filter(self) -> None:
        self.filter_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter_edit.selectAll()

    def _apply_filter_from_box(self) -> None:
        if self._snapshot is None:
            return
        expression = self.filter_edit.text()
        try:
            parsed = Filter(expression)
        except FilterError as exc:
            set_invalid(self.filter_edit, True)
            self.statusBar().showMessage(f"过滤表达式有误：{exc}", 6000)
            return
        set_invalid(self.filter_edit, False)

        root = self._snapshot.root
        total = self._snapshot.n_files
        used_bytes = self._snapshot.used_bytes
        # 过滤后要保持用户当前所在的层级（SpaceSniffer 的默认手感），
        # 所以先把当前位置记下来，等结果回来后在同一路径上重新定位。
        previous = self._current

        if parsed.is_empty:
            self._filter = parsed
            self._view_root = root
            self._current = self._descend_in_view(root, previous)
            self._selected = self._current
            self._refresh_tag_status()
            self._refresh_view()
            return

        def work() -> FilterResult:
            return filtering.build_view(root, parsed, total)

        def done(result: FilterResult) -> None:
            if result.root is None:
                self._filter = parsed
                self._view_root = None
                self._current = None
                self._selected = None
                self.treemap_view.set_layout(None)
                self.treemap_view.set_hint("没有内容匹配这个过滤条件")
                self.breadcrumb.set_path([])
                self.details.show_entry(None, 1, [])
                self.details.show_types([])
                self._refresh_tag_status()
                self.statusBar().showMessage("没有内容匹配这个过滤条件", 5000)
                return
            self._filter = parsed
            self._view_root = result.root
            self._current = self._descend_in_view(result.root, previous)
            self._selected = self._current
            self._type_cache.clear()
            self._refresh_tag_status()
            self._refresh_view()
            self.statusBar().showMessage(
                f"命中 {result.file_hits:,} 个文件，合计 {human_size(result.byte_hits)}"
                f"（占全盘 {result.byte_hits / max(used_bytes, 1) * 100:.1f}%）",
                6000,
            )

        self._run_task(work, on_done=done, busy_message="正在过滤…")

    def on_filter_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("过滤语法")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("过滤框语法（分号分隔多个条件，条件之间是「且」）")
        box.setInformativeText(
            "*.jpg              只看 jpg 文件\n"
            "*.jpg;*.png        只看 jpg 或 png\n"
            "|*.jpg             排除 jpg\n"
            "jpg                只写扩展名等同于 *.jpg\n"
            ">500mb             大于 500 MB（单位 b/kb/mb/gb/tb）\n"
            "<1gb               小于 1 GB\n"
            ">2years            修改时间早于两年前（day/week/month/year）\n"
            "<3months           修改时间在近三个月内\n"
            ":red               只看红色标记（:yellow / :green / :blue / :all）\n"
            "|:red              排除红色标记\n"
            "N:download         名称包含 download\n"
            "A:hidden           带隐藏属性\n"
            "\n"
            "组合示例：*.jpg;>1mb;<3months;|:yellow\n"
            "\n"
            "说明：过滤主要作用在文件上；目录只要子树里有命中内容就会被保留，\n"
            "并按命中内容的合计大小重新计量。目录名本身也参与掩码与 N: 匹配。"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("知道了")
        box.exec()

    def on_shortcuts(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("快捷键")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("全部快捷键")
        box.setInformativeText(
            "Ctrl+O        打开快照\n"
            "F5            重新加载\n"
            "Ctrl+E        导出文本报告\n"
            "Ctrl+Shift+E  导出 CSV 明细\n"
            "Ctrl+F        聚焦过滤框\n"
            "Ctrl+1~4      标记为红/黄/绿/蓝（再按一次取消）\n"
            "Ctrl+T        标签着色开关\n"
            "双击目录      放大到该目录\n"
            "双击文件      用默认程序打开\n"
            "Backspace     返回上一层（Esc 同）\n"
            "Alt+↑         返回上一层\n"
            "Alt+Home      回到根目录\n"
            "右键空白处    导航菜单\n"
            "右键方块      项目菜单（打开/定位/属性/标记/删除）"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("知道了")
        box.exec()

    def on_about(self) -> None:
        QMessageBox.about(
            self,
            "关于",
            "<b>SpaceSniffer 快照浏览器</b><br><br>"
            "读取 SpaceSniffer 导出的 <code>.sns</code> 快照，用方块图（treemap）"
            "直观呈现磁盘占用。<br>"
            "本工具只做<b>读取与浏览</b>，不扫描磁盘，也不修改快照。<br><br>"
            "删除操作一律走回收站，且需逐次确认。",
        )

    # -- 系统操作 -----------------------------------------------------------

    def _open_path(self, path: str) -> None:
        if not os.path.exists(path):
            self._warn_missing(path)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _reveal_path(self, path: str) -> None:
        if not os.path.exists(path):
            self._warn_missing(path)
            return
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        except OSError as exc:
            self._warn(f"无法打开资源管理器：{exc}")

    def _show_properties(self, path: str) -> None:
        if not os.path.exists(path):
            self._warn_missing(path)
            return
        try:
            ctypes.windll.shell32.ShellExecuteW(None, "properties", path, None, None, 1)
        except OSError as exc:
            self._warn(f"无法打开属性对话框：{exc}")

    def _warn_missing(self, path: str) -> None:
        QMessageBox.warning(
            self,
            "磁盘上找不到该项",
            f"{path}\n\n快照记录的是导出那一刻的状态，之后文件可能已被移动或删除。",
        )

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "操作失败", message)

    def _copy_text(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"已复制：{text}", 3000)

    def _delete_entry(self, path: str, is_dir: bool) -> None:
        if not os.path.exists(path):
            self._warn_missing(path)
            return
        try:
            from tools.disk.recycle import send_to_recycle
        except ImportError as exc:  # pragma: no cover - 环境缺文件时
            self._warn(f"无法加载回收站模块：{exc}")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("删除到回收站")
        box.setText(f"确定把{'目录' if is_dir else '文件'}「{os.path.basename(path)}」移到回收站吗？")
        box.setInformativeText(
            f"{path}\n\n"
            + ("目录会连同全部内容一起移入回收站。\n" if is_dir else "")
            + "移入回收站后仍可还原，但请确认这是你要删的东西。"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("移入回收站")
        box.button(QMessageBox.StandardButton.No).setText("取消")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        ok, error = send_to_recycle(path)
        if ok:
            self.statusBar().showMessage(f"已移入回收站：{path}", 5000)
        else:
            self._warn(f"删除失败：{error}")

    # -- 导出 ---------------------------------------------------------------

    def on_export_text(self) -> None:
        if self._snapshot is None or self._current is None:
            return
        suggested = str(
            Path(self._last_dir)
            / f"spacesniffer_{self._snapshot.volume_label.replace(':', '')}_报告.txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出文本报告", suggested, "文本文件 (*.txt)"
        )
        if not path:
            return
        snapshot = self._snapshot
        focus = filtering.resolve(self._current)
        expression = filtering.describe(self._filter) if not self._filter.is_empty else ""

        def work() -> str:
            return report.build_text_report(
                snapshot, focus, top=50, filter_expression=expression
            )

        def done(text: str) -> None:
            report.write_text_report(path, text)
            self.statusBar().showMessage(f"报告已导出：{path}", 6000)

        self._run_task(work, on_done=done, busy_message="正在生成报告…", cursor=True)

    def on_export_csv(self) -> None:
        if self._snapshot is None or self._current is None:
            return
        node = filtering.resolve(self._current)
        suggested = str(
            Path(self._last_dir)
            / f"spacesniffer_{self._snapshot.volume_label.replace(':', '')}_明细.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV 明细", suggested, "CSV 文件 (*.csv)"
        )
        if not path:
            return
        view_node = self._current

        def work() -> int:
            return report.write_csv(view_node, path)

        def done(count: int) -> None:
            self.statusBar().showMessage(f"已导出 {count:,} 行：{path}", 8000)

        self._run_task(work, on_done=done, busy_message="正在导出 CSV…", cursor=True)

    # -- 后台任务公共 -------------------------------------------------------

    def _run_task(
        self,
        work: Callable[[], _T],
        *,
        on_done: Callable[[_T], None],
        on_error: Optional[Callable[[str], None]] = None,
        busy_message: str = "",
        quiet: bool = False,
        cursor: bool = False,
    ) -> None:
        """把 work 丢进后台线程，成功后回主线程调 on_done。

        `work` 的返回类型由 `_T` 串到 `on_done` 的参数上 —— 各调用点写的
        `def done(snapshot: Snapshot)` / `def done(result: FilterResult)` 才能被检查，
        否则信号一律是 `object`，回调里把类型写错也没人拦。
        """
        worker = TaskWorker(work, self)
        self._workers.append(worker)

        def cleanup() -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            if not quiet:
                self._set_busy(False)
            if cursor:
                QApplication.restoreOverrideCursor()

        def handle_done(result: _T) -> None:
            cleanup()
            on_done(result)

        def handle_error(message: str) -> None:
            cleanup()
            if on_error is not None:
                on_error(message)
            else:
                self._warn(message)

        worker.done.connect(handle_done)
        worker.failed.connect(handle_error)
        if not quiet:
            self._set_busy(True, busy_message)
        if cursor:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        worker.start()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._progress.setVisible(busy)
        self.act_open.setEnabled(not busy)
        self.filter_edit.setEnabled(not busy and self._snapshot is not None)
        if busy:
            self.statusBar().showMessage(message)
        elif self._snapshot is not None:
            self.statusBar().showMessage("就绪", 2000)

    def _on_task_error(self, message: str) -> None:
        self._set_busy(False)
        if isinstance(message, str) and ("SnsParse" in message or "解析" in message):
            QMessageBox.critical(
                self,
                "快照无法解析",
                f"{message}\n\n"
                "快照可能已损坏或被截断。请不要基于不完整的数据做清理决策 —— "
                "重新用 SpaceSniffer 扫一遍再导出通常更快。",
            )
        else:
            self._warn(message)

    # -- 窗口事件 -----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_timer.start()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(".sns"):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            path = urls[0].toLocalFile()
            if path.lower().endswith(".sns"):
                self.load_snapshot(path)

    def closeEvent(self, event) -> None:  # noqa: N802
        for worker in list(self._workers):
            worker.wait(3000)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def launch(snapshot_path: Optional[str] = None, argv: Optional[List[str]] = None) -> int:
    """启动 GUI。返回进程退出码。"""
    args = list(sys.argv if argv is None else argv)
    app = QApplication.instance() or QApplication(args)
    apply_theme(app)

    window = SpaceSnifferWindow(snapshot_path)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover - 直接运行本文件
    target = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(launch(target))
