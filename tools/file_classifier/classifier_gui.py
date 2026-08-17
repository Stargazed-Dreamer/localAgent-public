"""文件自动分类工具 - PySide6 可视化 GUI

主入口：uv run python tools/file_classifier/classifier_gui.py

功能流程：
1. 扫描指定目录的文件（按时间排序）
2. 用户手动将部分文件拖入分类（创建分类映射）
3. 点击"AI预测空缺项分类"将映射+文件信息发给 LLM 预测
4. 在审核对话框中覆盖预测或跳过文件
5. 确认后将虚拟分类映射到真实目录路径并移动文件

设计要点：
- 左侧文件列表（QTableWidget）+ 右侧分类拖放区（QListWidget）
- 支持多选、反选、右键菜单
- QThread 执行 LLM 预测，避免冻结 GUI
- 文件冲突处理（覆盖/跳过/重命名）
"""
import copy
import json
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QMimeData, QFileInfo
from PySide6.QtGui import QColor, QAction, QActionGroup, QDrag, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QListWidget, QListWidgetItem, QCheckBox, QComboBox,
    QDialog, QProgressBar, QMenu, QAbstractItemView, QGroupBox,
    QInputDialog, QStyle, QFrame, QFileIconProvider, QToolButton, QTabBar,
)

# 添加当前目录到路径，以便导入 predictor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from predictor import (
    predict_categories, load_categories, save_categories,
    save_sample, load_sample, check_backend_available,
    classify_folder,
)
from state_manager import StateManager
from type_descriptor import describe_file_type
from file_table_widget import FileTableWidget
from details_dialog import DetailsDialog
from worker_threads import FileScanThread, PredictThread, MoveFilesThread
from category_widget import CategoryListWidget
from stream_review_dialog import StreamReviewDialog
from step_review_dialog import StepReviewDialog
from session_store import (
    SessionState, derive_session_id, save_session, list_sessions, delete_session,
)

# ── 配置 ──
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "categories.json")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DEFAULT_SAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_sample.json")

# ── 主题配色（与项目其他 GUI 一致的暗色主题） ──
BG_DARK = "#272a29"
BG_SURFACE = "#313534"
BG_HOVER = "#3b3f3e"
BG_INPUT = "#2b2e2d"
BORDER = "#4a4e4d"
BORDER_LIGHT = "#5c605f"
TEXT_PRIMARY = "#ecf0ee"
TEXT_SECONDARY = "#a8b0ac"
TEXT_DIM = "#747c78"
ACCENT = "#5cb88a"
ACCENT_HOVER = "#72d4a4"
WARNING = "#ffe66d"
DANGER = "#ff6b6b"
SUCCESS = "#4ecdc4"

# 置信度颜色阈值
CONFIDENCE_HIGH = 0.8
CONFIDENCE_LOW = 0.5

FONT_FAMILY = "Microsoft YaHei, Segoe UI, sans-serif"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG_DARK};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
}}
QPushButton {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 6px 16px;
    border-radius: 4px;
    font-size: 12px;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    background-color: {BG_SURFACE};
}}
QPushButton#primary {{
    background-color: {ACCENT};
    color: white;
    border-color: {ACCENT};
    font-weight: bold;
}}
QPushButton#primary:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#danger {{
    background-color: {DANGER};
    color: white;
    border-color: {DANGER};
}}
QLineEdit {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 5px 8px;
    border-radius: 4px;
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QTableWidget {{
    background-color: {BG_SURFACE};
    alternate-background-color: {BG_HOVER};
    color: {TEXT_PRIMARY};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 4px;
    font-size: 12px;
}}
QTableWidget::item {{
    padding: 4px 8px;
}}
QTableWidget::item:selected {{
    background-color: {ACCENT};
}}
QHeaderView::section {{
    background-color: {BG_SURFACE};
    color: {TEXT_SECONDARY};
    padding: 6px 8px;
    border: none;
    border-bottom: 2px solid {ACCENT};
    font-size: 12px;
    font-weight: bold;
}}
QListWidget {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    font-size: 12px;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-bottom: 1px solid {BORDER};
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: white;
}}
QListWidget::item:hover {{
    background-color: {BG_HOVER};
}}
QLabel {{
    color: {TEXT_PRIMARY};
}}
QMenu {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
}}
QGroupBox {{
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}}
QComboBox {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    border-radius: 4px;
}}
QComboBox::drop-down {{
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT};
}}
QProgressBar {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT_PRIMARY};
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QScrollBar:vertical {{
    background-color: {BG_SURFACE};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background-color: {BORDER_LIGHT};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background-color: {BG_SURFACE};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background-color: {BORDER_LIGHT};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {ACCENT};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QSplitter::handle {{
    background-color: {BORDER};
    width: 3px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}
QDialog {{
    background-color: {BG_DARK};
}}
QToolTip {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    padding: 4px 8px;
}}
"""


def human_readable_size(size: int) -> str:
    """将字节数转为人类可读格式"""
    if size < 0:
        return "未知"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_time(timestamp: float) -> str:
    """格式化时间戳为字符串"""
    if not timestamp:
        return "未知"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "未知"


def confidence_color(confidence: float) -> QColor:
    """根据置信度返回颜色"""
    if confidence >= CONFIDENCE_HIGH:
        return QColor(SUCCESS)  # 绿色 - 高置信度
    elif confidence >= CONFIDENCE_LOW:
        return QColor(WARNING)  # 黄色 - 中等置信度
    else:
        return QColor(TEXT_DIM)  # 灰色 - 低置信度


class FileClassifierGUI(QMainWindow):
    """文件自动分类工具主窗口"""

    # 表格列定义
    COL_CHECK = 0      # 复选框
    COL_NAME = 1       # 文件名
    COL_TYPE = 2       # 类型（系统图标 + 类型描述）
    COL_SIZE = 3       # 大小
    COL_MODIFIED = 4   # 修改时间
    COL_CREATED = 5    # 创建时间
    COL_CURRENT = 6    # 当前分类（用户手动指定）
    COL_PREDICTED = 7  # 预测分类
    COL_CONFIDENCE = 8 # 置信度
    COL_REVIEWED = 9   # 已审（ticket I：✓/空，仅由 R2 审核通过产生）
    COL_HEADERS = ["选", "文件名", "类型", "大小", "修改时间", "创建时间", "当前分类", "预测分类", "置信度", "已审"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("文件自动分类工具")
        self.setGeometry(100, 100, 1400, 850)
        self.setMinimumSize(1100, 650)

        # 数据
        self.files = []          # 所有文件信息 [{"path", "filename", "size", "modified", "created", "is_dir"}, ...]
        self.categories = []     # 分类配置 [{"name", "path", "extensions"}, ...]
        self.predictions = {}    # filename -> {"category", "confidence"}
        self.current_categories = {}  # filename -> category_name（用户手动指定）
        self.skipped_files = set()    # 跳过的文件名集合
        self.reviewed_files = set()   # ticket I：已审文件名集合（仅 R2 通过产生）

        # ticket I：多列排序状态——logical 列号 → 是否升序（默认 True）
        self._col_sort_directions = {}
        # 用户首次点列头/拖列序前不启用排序（保留"文件夹置顶+时间倒序"的扫描顺序）
        self._sort_active = False

        # 导航历史（ticket 05）：当前扫描目录 + 返回历史栈
        self.current_scan_dir = ""   # 当前正在浏览的目录绝对路径
        self._nav_history = []       # 导航历史栈，用于"上一级"逐级返回

        # 滚动位置记忆：目录路径 → {scroll: 滚动值, focus: 进入子目录时焦点文件名}
        # 返回上一级时恢复滚动位置 + 高亮进入子目录的那一行
        self._nav_memory = {}
        self._pending_scroll_restore = ""  # 扫描完成后需恢复滚动位置的目录

        # 状态管理器（列宽记忆、上次源目录、分类配置路径、分类即已处理）
        self.state_manager = StateManager(STATE_PATH)
        # 系统图标提供器（QFileIconProvider 获取文件/文件夹的系统图标）
        self._icon_provider = QFileIconProvider()

        # 线程
        self.scan_thread = None
        self.predict_thread = None
        self.move_thread = None

        # ── ticket F：R1 流式审核窗口（模态） ──
        self._stream_dialog = None     # 当前打开的 StreamReviewDialog
        self._stream_stats = {"accepted": 0, "adjusted": 0, "skipped": 0}
        # 已关闭流式窗口的强引用保活列表（PySide6 崩溃防护）：
        # exec 返回后 Qt 内部可能仍有指向对话框的 DeferredDelete 事件待投递，
        # 若 Python wrapper 先行被 GC，事件投递时访问违规。持有强引用直到
        # C++ 对象真正销毁（destroyed 信号）再释放。
        self._stream_dialog_refs = []

        # ── ticket E：会话机制（标签页 + 每会话一文件持久化） ──
        self.sessions = {}            # session_id -> SessionState
        self._tab_ids = []            # tab 下标 -> session_id（与 QTabBar 顺序一致）
        self._current_session_id = ""  # 当前活动会话 ID
        self._switching_tabs = False   # 程序化增删 tab 时抑制 currentChanged 处理

        # 加载默认分类配置
        self._load_default_categories()

        # ticket E：已有会话文件时直接走会话恢复；旧版 state.json/file_sample.json
        # 的恢复逻辑仅在没有任何会话时作为"首个会话"的种子（平滑迁移）
        existing_sessions = list_sessions()
        if not existing_sessions:
            self._restore_last_state()

        self._setup_ui()
        self._refresh_category_list()

        # ticket E：初始化会话标签页并载入活动会话（含自动扫描）
        self._init_sessions(existing_sessions)

    def _setup_ui(self):
        """构建界面"""
        self.setStyleSheet(STYLESHEET)
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ── 会话标签栏（ticket E：每会话=一个源文件夹） ──
        main_layout.addLayout(self._build_session_bar())

        # ── 顶部工具栏 ──
        main_layout.addLayout(self._build_top_bar())

        # ticket E：Ctrl+S 保存当前会话（与"保存会话"按钮等价）
        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        save_shortcut.activated.connect(self._save_current_session)

        # ── 中间主体（左右分栏） ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([900, 400])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter, 1)

        # ── 底部操作栏 ──
        main_layout.addLayout(self._build_bottom_bar())

    def _build_session_bar(self) -> QHBoxLayout:
        """会话标签栏（ticket E）：浏览器式标签页 + 「+」新建会话

        每个会话 = 一个源文件夹；切换标签即切换工作会话。
        预测/移动进行中禁用切换（多会话不允许同时触发）。
        """
        layout = QHBoxLayout()
        layout.setSpacing(4)

        self.tab_bar = QTabBar()
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.setElideMode(Qt.ElideRight)
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        layout.addWidget(self.tab_bar, 1)

        self.add_session_btn = QPushButton("＋ 新会话")
        self.add_session_btn.setToolTip("新建一个会话（每个会话对应一个源文件夹）")
        self.add_session_btn.clicked.connect(self._new_session)
        layout.addWidget(self.add_session_btn)

        return layout

    def _build_top_bar(self) -> QHBoxLayout:
        """顶部工具栏：目录选择 + 扫描 + 加载分类"""
        layout = QHBoxLayout()
        layout.setSpacing(6)

        layout.addWidget(QLabel("源目录:"))
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("选择要分类的文件目录...")
        self.dir_edit.returnPressed.connect(self.scan_files)
        layout.addWidget(self.dir_edit, 1)

        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self.browse_directory)
        layout.addWidget(browse_btn)

        scan_btn = QPushButton("扫描文件")
        scan_btn.setObjectName("primary")
        scan_btn.clicked.connect(self.scan_files)
        layout.addWidget(scan_btn)

        load_cat_btn = QPushButton("加载分类配置")
        load_cat_btn.clicked.connect(self.load_categories_file)
        layout.addWidget(load_cat_btn)

        return layout

    def _build_left_panel(self) -> QWidget:
        """左侧面板：文件列表"""
        group = QGroupBox("文件列表（文件夹置顶，其余按修改时间倒序）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 14, 6, 6)

        # 面包屑导航栏（ticket 05）：上一级按钮 + 当前路径分段可点击
        nav_row = QHBoxLayout()
        nav_row.setSpacing(4)

        self.up_btn = QPushButton("上一级")
        self.up_btn.setToolTip("返回上一级目录")
        self.up_btn.setEnabled(False)  # 初始无历史
        self.up_btn.clicked.connect(self._navigate_up)
        nav_row.addWidget(self.up_btn)

        # 面包屑容器：用 QHBoxLayout 动态填充路径段按钮
        self.breadcrumb_layout = QHBoxLayout()
        self.breadcrumb_layout.setSpacing(2)
        self.breadcrumb_layout.addStretch()
        nav_row.addLayout(self.breadcrumb_layout, 1)

        layout.addLayout(nav_row)

        # 操作按钮行（ticket D：多选模式复选框紧邻反选右侧，统计信息移到表格下方）
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        select_all_btn = QPushButton("全选")
        select_all_btn.clicked.connect(self.select_all)
        btn_row.addWidget(select_all_btn)

        deselect_all_btn = QPushButton("取消全选")
        deselect_all_btn.clicked.connect(self.deselect_all)
        btn_row.addWidget(deselect_all_btn)

        invert_btn = QPushButton("反选")
        invert_btn.clicked.connect(self.invert_selection)
        btn_row.addWidget(invert_btn)

        self.multi_select_check = QCheckBox("多选模式")
        self.multi_select_check.setChecked(False)
        self.multi_select_check.setToolTip(
            "关闭（默认·普通模式）：Windows 资源管理器行为\n"
            "  · 单击 = 选中当前项，取消其他\n"
            "  · Ctrl+单击 = 翻转当前项（不影响其他）\n"
            "  · Shift+单击 = 选中锚点到当前项的范围\n"
            "开启（多选模式）：\n"
            "  · 单击 = 翻转当前项（不取消其他）\n"
            "  · Shift+单击 = 范围内逐项取反\n"
            "  · Ctrl+单击 = 打开对象（与双击同义）"
        )
        self.multi_select_check.stateChanged.connect(self._on_multi_select_mode_changed)
        btn_row.addWidget(self.multi_select_check)

        btn_row.addStretch()

        layout.addLayout(btn_row)

        # 文件表格（自定义子类：单击翻转/shift批量取反/ctrl+单击打开/拖拽携勾选行）
        # selectionMode=SingleSelection + SelectRows 已在 FileTableWidget.__init__ 设置，
        # selectionModel 仅用于焦点/视觉高亮，不再用于"选中"（选中=复选框勾选）
        self.file_table = FileTableWidget(check_col=self.COL_CHECK, name_col=self.COL_NAME)
        self.file_table.setColumnCount(len(self.COL_HEADERS))
        self.file_table.setHorizontalHeaderLabels(self.COL_HEADERS)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # ticket I：禁用内置单列排序，改用自定义多列排序（排序优先级=可视列顺序）
        self.file_table.setSortingEnabled(False)
        self.file_table.setDragEnabled(True)
        self.file_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_table.customContextMenuRequested.connect(self._show_context_menu)
        self.file_table.itemChanged.connect(self._on_table_item_changed)
        # 双击/Enter/ctrl+单击 → 打开对象（文件用默认程序，文件夹由 ticket 05 接管导航）
        self.file_table.open_requested.connect(self._open_file_row)
        # ctrl+enter → 弹出详情弹窗
        self.file_table.details_requested.connect(self._show_details_for_row)
        # Backspace → 返回上一级目录
        self.file_table.navigate_up_requested.connect(self._navigate_up)

        # 列宽设置：COL_CHECK 固定 40px，其余 Interactive（可拖拽+双击自适应）
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(self.COL_CHECK, QHeaderView.Fixed)
        header.resizeSection(self.COL_CHECK, 40)
        for col in [self.COL_NAME, self.COL_TYPE, self.COL_SIZE, self.COL_MODIFIED,
                    self.COL_CREATED, self.COL_CURRENT, self.COL_PREDICTED,
                    self.COL_CONFIDENCE, self.COL_REVIEWED]:
            header.setSectionResizeMode(col, QHeaderView.Interactive)

        # 允许拖动列头重排列顺序
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        # ticket I：排序指示器（初始隐藏，首次排序后显示；主排序键=可视第 0 列）
        header.setSortIndicatorShown(False)
        header.setSortIndicator(self.COL_NAME, Qt.AscendingOrder)

        # 恢复记忆列宽，无记忆时用默认值（COL_NAME 占较大比例）
        saved_widths = self.state_manager.get_column_widths()
        default_widths = self._default_column_widths()
        for col in range(len(self.COL_HEADERS)):
            key = f"col_{col}"
            width = saved_widths.get(key, default_widths.get(key, 100))
            header.resizeSection(col, width)

        # 恢复记忆列顺序（visual index → logical index 映射）
        saved_order = self.state_manager.get_column_order()
        if saved_order and len(saved_order) == len(self.COL_HEADERS):
            for visual_idx, logical_idx in enumerate(saved_order):
                current_visual = header.visualIndex(logical_idx)
                if current_visual != visual_idx:
                    header.moveSection(current_visual, visual_idx)

        # 列宽/列顺序变化时防抖保存到 state.json（拖拽过程中不频繁写盘）
        self._column_save_timer = QTimer(self)
        self._column_save_timer.setSingleShot(True)
        self._column_save_timer.setInterval(500)
        self._column_save_timer.timeout.connect(self._save_column_state)
        header.sectionResized.connect(lambda *_: self._column_save_timer.start())
        # ticket I：拖动列序 = 指定多列排序优先级 → 立即按新列序重排，并防抖保存
        header.sectionMoved.connect(self._on_section_moved)
        # ticket I：点击列头 = 切换该列升/降序
        header.sectionClicked.connect(self._on_header_clicked)

        layout.addWidget(self.file_table, 1)

        # ticket D：底部状态行（表格下方，左对齐，不用拉伸推右）
        status_row = QHBoxLayout()
        status_row.addStretch()
        status_row.setSpacing(16)

        self.selection_count_label = QLabel("已选择 0 项")
        self.selection_count_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        status_row.addWidget(self.selection_count_label)

        self.mode_label = QLabel("模式：普通")
        self.mode_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        status_row.addWidget(self.mode_label)

        self.file_count_label = QLabel("0 个文件")
        self.file_count_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        status_row.addWidget(self.file_count_label)

        layout.addLayout(status_row)

        return group

    def _default_column_widths(self) -> dict:
        """默认列宽配置（COL_NAME 占较大比例）"""
        return {
            "col_0": 40,    # COL_CHECK
            "col_1": 400,   # COL_NAME（较大比例）
            "col_2": 110,   # COL_TYPE
            "col_3": 80,    # COL_SIZE
            "col_4": 140,   # COL_MODIFIED
            "col_5": 140,   # COL_CREATED
            "col_6": 100,   # COL_CURRENT
            "col_7": 100,   # COL_PREDICTED
            "col_8": 80,    # COL_CONFIDENCE
            "col_9": 50,    # COL_REVIEWED
        }

    def _save_column_state(self) -> None:
        """将当前列宽和列顺序保存到 state.json"""
        header = self.file_table.horizontalHeader()
        widths = {}
        for col in range(len(self.COL_HEADERS)):
            widths[f"col_{col}"] = header.sectionSize(col)
        self.state_manager.save_column_widths(widths)
        # 保存列视觉顺序（logical index 列表，按 visual position 排列）
        order = []
        for visual_idx in range(len(self.COL_HEADERS)):
            order.append(header.logicalIndex(visual_idx))
        self.state_manager.save_column_order(order)

    # ── 多列排序（ticket I：排序优先级 = 可视列顺序） ──

    def _on_section_moved(self, *args):
        """列头被拖动：按新的可视列序重排 + 防抖保存列状态"""
        self._sort_table()
        self._column_save_timer.start()

    def _on_header_clicked(self, logical_index: int):
        """点击列头：首次点击按升序，之后每次点击切换该列升/降序，然后重排"""
        cur = self._col_sort_directions.get(logical_index)
        if cur is None:
            self._col_sort_directions[logical_index] = True  # 首次：升序
        else:
            self._col_sort_directions[logical_index] = not cur
        self._sort_table()

    def _sort_key_for_item(self, col: int, item):
        """提取单个单元格的排序键（同列内保证类型一致）"""
        if item is None:
            return 0 if col in (self.COL_CHECK, self.COL_REVIEWED, self.COL_SIZE,
                                self.COL_MODIFIED, self.COL_CREATED, self.COL_CONFIDENCE) else ""
        # 数值列：大小/时间/置信度用 UserRole 里的原始数值
        if col in (self.COL_SIZE, self.COL_MODIFIED, self.COL_CREATED, self.COL_CONFIDENCE):
            v = item.data(Qt.UserRole)
            return float(v) if isinstance(v, (int, float)) else 0.0
        # 复选框/已审：勾选态 / 0-1 标记
        if col == self.COL_CHECK:
            """ #不参与排序
            state = item.checkState()
            if state == Qt.Checked:
                return 2
            if state == Qt.PartiallyChecked:
                return 1
            """
            return 0
        if col == self.COL_REVIEWED:
            v = item.data(Qt.UserRole)
            return int(v) if isinstance(v, int) else 0
        # 其余文本列：不区分大小写
        return item.text().lower()

    def _sort_table(self):
        """多列稳定排序：从可视顺序最后一列（最低优先级）到第一列（最高优先级）
        逐列做稳定排序叠加，每列方向独立（点击列头切换）。"""
        table = self.file_table
        header = table.horizontalHeader()
        col_count = table.columnCount()
        row_count = table.rowCount()
        if row_count <= 1:
            return
        table.blockSignals(True)
        # 取出全部行（takeItem 移除并返回 item，保留勾选态/图标/颜色等属性）
        rows = []
        for r in range(row_count):
            rows.append([table.takeItem(r, c) for c in range(col_count)])
        # 稳定排序叠加：最低优先级键先排，最高优先级键最后排
        for visual in range(col_count - 1, -1, -1):
            logical = header.logicalIndex(visual)
            ascending = self._col_sort_directions.get(logical, True)
            rows.sort(
                key=lambda items, lg=logical: self._sort_key_for_item(lg, items[lg]),
                reverse=not ascending,
            )
        # 填回表格
        for r, items in enumerate(rows):
            for c, item in enumerate(items):
                if item is not None:
                    table.setItem(r, c, item)
        table.blockSignals(False)
        # 排序被激活（用户首次触发后，后续填充/刷新都维持排序）
        self._sort_active = True
        # 排序指示器标在主排序键（可视第 0 列）上
        header.setSortIndicatorShown(True)
        primary_logical = header.logicalIndex(0)
        primary_ascending = self._col_sort_directions.get(primary_logical, True)
        header.setSortIndicator(
            primary_logical,
            Qt.AscendingOrder if primary_ascending else Qt.DescendingOrder)

    def _refresh_table_reviewed(self):
        """刷新表格中的"已审"列（ticket I，与 H 联动）"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            name_item = self.file_table.item(row, self.COL_NAME)
            reviewed_item = self.file_table.item(row, self.COL_REVIEWED)
            if not name_item or not reviewed_item:
                continue
            filename = name_item.data(Qt.UserRole)
            reviewed = filename in self.reviewed_files
            reviewed_item.setText("✓" if reviewed else "")
            reviewed_item.setData(Qt.UserRole, 1 if reviewed else 0)
            reviewed_item.setForeground(QColor(SUCCESS) if reviewed else QColor(TEXT_DIM))
        self.file_table.blockSignals(False)

    def _build_right_panel(self) -> QWidget:
        """右侧面板：分类拖放区"""
        group = QGroupBox("分类目标（拖拽文件到此）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 14, 6, 6)

        # 分类列表（拖放目标）
        self.category_list = CategoryListWidget()
        self.category_list.files_dropped.connect(self._on_files_dropped_to_category)
        # ticket C：右键重命名分类
        self.category_list.rename_requested.connect(self.rename_category)
        layout.addWidget(self.category_list, 1)

        # 分类操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        add_btn = QPushButton("添加分类")
        add_btn.clicked.connect(self.add_category)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("删除分类")
        remove_btn.clicked.connect(self.remove_category)
        btn_row.addWidget(remove_btn)

        set_path_btn = QPushButton("设置路径")
        set_path_btn.clicked.connect(self.set_category_path)
        btn_row.addWidget(set_path_btn)

        layout.addLayout(btn_row)

        # 提示
        hint = QLabel("提示：从左侧拖拽文件到分类，或右键分配\n绿色=高置信度  黄色=中等  灰色=低/未分配")
        hint.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        return group

    def _build_bottom_bar(self) -> QHBoxLayout:
        """底部操作栏"""
        layout = QHBoxLayout()
        layout.setSpacing(6)

        self._predict_scope = "all"  # ticket B："all"=全部未分类，"selected"=仅勾选项

        self.predict_btn = QPushButton("AI预测空缺项分类")
        self.predict_btn.setObjectName("primary")
        self.predict_btn.clicked.connect(lambda: self.predict_all())
        self.predict_btn.setToolTip("将分类映射+文件信息发给 LLM，预测未手动分类的文件/文件夹归属")
        layout.addWidget(self.predict_btn)

        # ticket B：预测范围下拉（紧邻主按钮）
        self.predict_scope_btn = QToolButton()
        self.predict_scope_btn.setText("▼")
        self.predict_scope_btn.setToolTip("选择 AI 预测范围")
        scope_menu = QMenu(self)
        self._scope_all_action = scope_menu.addAction("AI预测全部未分类")
        self._scope_selected_action = scope_menu.addAction("AI预测勾选项")
        self._scope_all_action.setCheckable(True)
        self._scope_selected_action.setCheckable(True)
        self._scope_all_action.setChecked(True)
        scope_group = QActionGroup(self)
        scope_group.addAction(self._scope_all_action)
        scope_group.addAction(self._scope_selected_action)
        scope_group.setExclusive(True)
        scope_menu.triggered.connect(self._on_predict_scope_changed)
        self.predict_scope_btn.setMenu(scope_menu)
        self.predict_scope_btn.setPopupMode(QToolButton.InstantPopup)
        layout.addWidget(self.predict_scope_btn)

        # ticket G+H：「审核并移动」拆为「审核」+「移动」两步
        self.review_btn = QPushButton("审核")
        self.review_btn.clicked.connect(self._open_step_review)
        self.review_btn.setToolTip(
            "打开 R2 专门审核界面，逐类核对已分类条目（可跳过、不可改分类）。\n"
            "审核通过的标「已审」，跳过的清除分类。")
        layout.addWidget(self.review_btn)

        self.move_btn = QPushButton("移动")
        self.move_btn.clicked.connect(self._confirm_and_move)
        self.move_btn.setToolTip(
            "仅移动标为「已审」的条目（未审项不动）。\n"
            "暂存分类（path 为空）只标记已处理、不移动。")
        layout.addWidget(self.move_btn)

        save_sample_btn = QPushButton("保存分类映射")
        save_sample_btn.clicked.connect(self.save_sample_config)
        save_sample_btn.setToolTip("保存当前手动分类为映射文件，供未来复用（不限于 LLM 预测，可直接加载使用）")
        layout.addWidget(save_sample_btn)

        load_sample_btn = QPushButton("加载分类映射")
        load_sample_btn.clicked.connect(self.load_sample_config)
        layout.addWidget(load_sample_btn)

        save_session_btn = QPushButton("保存会话")
        save_session_btn.clicked.connect(self._save_current_session)
        save_session_btn.setToolTip("保存当前会话进度到会话文件（Ctrl+S）。\n落盘时机仅三种：手动保存 / 关闭程序 / 删除会话前")
        layout.addWidget(save_session_btn)

        help_btn = QPushButton("帮助")
        help_btn.clicked.connect(self._open_help)
        help_btn.setToolTip("打开操作指引文档")
        layout.addWidget(help_btn)

        layout.addStretch()

        # 分隔竖线
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {BORDER};")
        sep.setFixedWidth(1)
        layout.addWidget(sep)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        return layout

    # ── 会话机制（ticket E：B2 标签页 + 每会话一文件） ──

    def _init_sessions(self, existing_sessions: list):
        """启动时恢复所有会话标签页（无会话时用旧版状态种子创建首个会话）

        Args:
            existing_sessions: list_sessions() 结果 [(path, SessionState)]，按更新时间倒序
        """
        self._switching_tabs = True
        if existing_sessions:
            for _path, state in existing_sessions:
                if not state.session_id:
                    state.session_id = (derive_session_id(state.source_dir)
                                        if state.source_dir else f"new_{uuid4().hex[:8]}")
                self.sessions[state.session_id] = state
                self._tab_ids.append(state.session_id)
                idx = self.tab_bar.addTab(state.title)
                self.tab_bar.setTabToolTip(idx, state.source_dir or "（尚未选择源目录）")
        else:
            # 平滑迁移：没有任何会话文件时，把旧版恢复的状态作为首个会话
            state = SessionState(source_dir=self.state_manager.get_last_source_dir())
            state.session_id = (derive_session_id(state.source_dir)
                                if state.source_dir else f"new_{uuid4().hex[:8]}")
            state.categories = copy.deepcopy(self.categories)
            state.current_categories = dict(self.current_categories)
            self.sessions[state.session_id] = state
            self._tab_ids.append(state.session_id)
            idx = self.tab_bar.addTab(state.title)
            self.tab_bar.setTabToolTip(idx, state.source_dir or "（尚未选择源目录）")
        self._switching_tabs = False

        # 激活最近保存的会话
        self.tab_bar.setCurrentIndex(0)
        self._current_session_id = self._tab_ids[0]
        self._apply_session_state(self.sessions[self._current_session_id])

    def _new_session(self):
        """新建会话标签页（继承当前分类配置，分类映射为空）"""
        # 先把当前 UI 状态收进旧会话（内存）
        if self._current_session_id in self.sessions:
            self.sessions[self._current_session_id] = self._collect_current_state()
        state = SessionState(session_id=f"new_{uuid4().hex[:8]}", source_dir="")
        # 新会话继承当前分类配置（否则空分类列表无法使用）
        state.categories = copy.deepcopy(self.categories)
        self.sessions[state.session_id] = state
        self._tab_ids.append(state.session_id)
        self._switching_tabs = True
        idx = self.tab_bar.addTab(state.title)
        self.tab_bar.setTabToolTip(idx, "（尚未选择源目录）")
        self.tab_bar.setCurrentIndex(idx)
        self._switching_tabs = False
        self._current_session_id = state.session_id
        self._apply_session_state(state)

    def _on_tab_changed(self, index: int):
        """切换标签页：保存当前会话状态（内存）→ 载入目标会话"""
        if self._switching_tabs or index < 0 or index >= len(self._tab_ids):
            return
        new_id = self._tab_ids[index]
        if new_id == self._current_session_id:
            return
        # 收起当前 UI 状态到旧会话（仅内存，不落盘——落盘时机严格限定三种）
        if self._current_session_id in self.sessions:
            self.sessions[self._current_session_id] = self._collect_current_state()
        self._current_session_id = new_id
        self._apply_session_state(self.sessions[new_id])

    def _on_tab_close_requested(self, index: int):
        """关闭标签页 = 删除会话（已确认决策：先弹窗确认）

        仅删除会话保存的进度与状态，已移动到目标目录的文件结果不变。
        """
        if index < 0 or index >= len(self._tab_ids):
            return
        sid = self._tab_ids[index]
        state = self.sessions.get(sid)
        title = state.title if state else sid
        reply = QMessageBox.question(
            self, "删除会话",
            f"确定删除会话「{title}」吗？\n"
            f"仅删除该会话保存的进度与状态，已移动到目标目录的文件不受影响。",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        # 删除活动会话时先解除当前绑定，避免 _on_tab_changed 把状态收进将被删除的会话
        was_current = (sid == self._current_session_id)
        if was_current:
            self._current_session_id = ""
        delete_session(sid)
        self.sessions.pop(sid, None)
        self._tab_ids.pop(index)
        self._switching_tabs = True
        self.tab_bar.removeTab(index)
        self._switching_tabs = False
        if not self._tab_ids:
            # 至少保留一个会话标签
            self._new_session()
        elif was_current:
            # 载入 Qt 自动选中的新当前标签（删非活动标签时当前会话不变，无需重扫）
            self._current_session_id = self._tab_ids[self.tab_bar.currentIndex()]
            self._apply_session_state(self.sessions[self._current_session_id])

    def _collect_current_state(self) -> SessionState:
        """把当前 UI 状态收集为 SessionState（用于会话切换/保存）"""
        sid = self._current_session_id
        old = self.sessions.get(sid)
        state = SessionState(session_id=sid,
                             source_dir=old.source_dir if old else "")
        state.categories = copy.deepcopy(self.categories)
        state.current_categories = dict(self.current_categories)
        state.predictions = copy.deepcopy(self.predictions)
        state.skipped = set(self.skipped_files)
        state.reviewed = set(self.reviewed_files)
        header = self.file_table.horizontalHeader()
        state.column_order = [header.logicalIndex(v) for v in range(len(self.COL_HEADERS))]
        state.sort_directions = {str(k): v for k, v in self._col_sort_directions.items()}
        state.sort_active = self._sort_active
        return state

    def _apply_session_state(self, state: SessionState):
        """把会话状态载入主界面，并重新扫描其源目录"""
        self.categories = copy.deepcopy(state.categories)
        self.current_categories = dict(state.current_categories)
        self.predictions = copy.deepcopy(state.predictions)
        self.skipped_files = set(state.skipped)
        self.reviewed_files = set(state.reviewed)
        self._col_sort_directions = {int(k): v for k, v in state.sort_directions.items()}
        self._sort_active = state.sort_active

        # 清空表格与导航状态
        self.files = []
        self.file_table.setRowCount(0)
        self.current_scan_dir = ""
        self._nav_history = []
        self._nav_memory = {}
        self._pending_scroll_restore = ""
        self._refresh_up_button_state()
        self._refresh_breadcrumb()
        self.file_count_label.setText("0 个文件")

        self._refresh_category_list()
        # 分类配置快照即当前配置（持久化到 categories.json 保持一致）
        self._save_categories()

        # 恢复该会话记忆的列序（ticket I/Q12：每会话独立）
        header = self.file_table.horizontalHeader()
        if state.column_order and len(state.column_order) == len(self.COL_HEADERS):
            for visual_idx, logical_idx in enumerate(state.column_order):
                current_visual = header.visualIndex(logical_idx)
                if current_visual != visual_idx:
                    header.moveSection(current_visual, visual_idx)
        self._column_save_timer.stop()  # moveSection 触发的防抖保存无需执行

        if state.source_dir and os.path.isdir(state.source_dir):
            self.dir_edit.setText(state.source_dir)
            # keep_categories=True：保留刚载入的分类映射
            self.scan_files(keep_categories=True)
        else:
            self.dir_edit.setText("")
            self._update_selection_status()

    def _save_current_session(self):
        """手动保存当前会话（Ctrl+S / 保存会话按钮）"""
        if self._current_session_id not in self.sessions:
            return
        state = self._collect_current_state()
        if not state.source_dir:
            self._update_status("会话尚未绑定源目录，请先扫描文件夹后再保存")
            return
        self.sessions[self._current_session_id] = state
        try:
            path = save_session(state)
            self._update_status(f"会话已保存: {os.path.basename(path)}")
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"保存会话失败:\n{e}")

    def _set_session_switching_enabled(self, enabled: bool):
        """预测/移动进行中禁用会话切换（多会话不允许同时触发）"""
        self.tab_bar.setEnabled(enabled)
        self.add_session_btn.setEnabled(enabled)

    def _bind_session_source(self, directory: str) -> bool:
        """扫描时把当前会话绑定到源目录（必要时更换 session_id）

        Returns:
            False 表示该目录已属于其它会话，已切换过去（调用方应中止本次扫描）
        """
        abs_dir = os.path.abspath(directory)
        target_id = derive_session_id(abs_dir)
        # 该源目录已有其它会话 → 切换过去，在那里扫描
        if target_id in self.sessions and target_id != self._current_session_id:
            idx = self._tab_ids.index(target_id)
            self._switching_tabs = True
            self.tab_bar.setCurrentIndex(idx)
            self._switching_tabs = False
            self._on_tab_changed(idx)
            return False
        # 绑定/重绑当前会话
        old_id = self._current_session_id
        state = self.sessions.get(old_id) or SessionState(old_id, "")
        state.source_dir = abs_dir
        if target_id != old_id:
            self.sessions.pop(old_id, None)
            state.session_id = target_id
            self.sessions[target_id] = state
            if old_id in self._tab_ids:
                self._tab_ids[self._tab_ids.index(old_id)] = target_id
            self._current_session_id = target_id
        cur = self.tab_bar.currentIndex()
        if cur >= 0:
            self.tab_bar.setTabText(cur, state.title)
            self.tab_bar.setTabToolTip(cur, abs_dir)
        return True

    def closeEvent(self, event):
        """应用关闭：静默保存所有活动会话（已确认 Q N1，不弹确认框）"""
        # 等待进行中的扫描线程收尾（预测线程由 ticket F 的中断机制处理）
        if self.predict_thread is not None and self.predict_thread.isRunning():
            if hasattr(self.predict_thread, "request_stop"):
                self.predict_thread.request_stop()
            self.predict_thread.wait(3000)
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.scan_thread.wait(3000)
        # 收集当前 UI 状态后，保存所有有源目录的会话
        if self._current_session_id in self.sessions:
            self.sessions[self._current_session_id] = self._collect_current_state()
        for state in self.sessions.values():
            if not state.source_dir:
                continue  # 空会话不落盘
            try:
                save_session(state)
            except OSError:
                pass  # 静默保存失败不打断退出
        event.accept()

    # ── 分类配置管理 ──

    def _load_default_categories(self):
        """加载默认分类配置"""
        try:
            self.categories = load_categories(CONFIG_PATH)
        except (FileNotFoundError, json.JSONDecodeError):
            self.categories = []

    def _restore_last_state(self):
        """从 state.json 恢复上次的分类配置和分类映射

        启动时自动恢复，避免用户每次手动点"加载分类配置"和"加载分类映射"。
        恢复顺序：先分类配置 → 再分类映射（映射可能依赖配置中的分类名）。
        """
        # 恢复上次加载的分类配置文件
        cat_config = self.state_manager.get_loaded_categories_config()
        if cat_config and os.path.exists(cat_config):
            try:
                self.categories = load_categories(cat_config)
            except Exception:
                pass  # 加载失败则保持默认分类

        # 恢复上次的分类映射（sample）
        # 优先用 state.json 中记录的路径；无记录时回退到工具目录下的默认 file_sample.json
        sample_path = self.state_manager.get_last_sample_path()
        if not sample_path or not os.path.exists(sample_path):
            sample_path = DEFAULT_SAMPLE_PATH if os.path.exists(DEFAULT_SAMPLE_PATH) else ""
        if sample_path:
            try:
                data = load_sample(sample_path)
                # 分类映射文件中可能包含分类配置，优先使用
                if data.get("categories"):
                    self.categories = data["categories"]
                # 恢复手动分类映射
                for sample in data.get("sample_files", []):
                    self.current_categories[sample["filename"]] = sample["category"]
            except Exception:
                pass  # 加载失败则保持默认状态

    def _save_loaded_categories_config(self, config_path: str):
        """记忆已加载的分类配置路径到 state.json"""
        self.state_manager.save_loaded_categories_config(config_path)

    def _refresh_category_list(self):
        """刷新右侧分类列表显示"""
        self.category_list.clear()
        for cat in self.categories:
            name = cat["name"]
            path = cat.get("path", "")
            count = sum(1 for c in self.current_categories.values() if c == name)
            # 显示分类名、文件数、路径状态
            path_status = f" → {path}" if path else " (未设路径)"
            text = f"{name}  [{count} 个文件]{path_status}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, name)
            # 未设路径的分类用警告色
            if not path:
                item.setForeground(QColor(WARNING))
            self.category_list.addItem(item)

    def load_categories_file(self):
        """从文件加载分类配置"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择分类配置文件", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            self.categories = load_categories(path)
            self._save_loaded_categories_config(path)
            self._refresh_category_list()
            self._update_status(f"已加载分类配置: {len(self.categories)} 个分类")
        except Exception as e:
            QMessageBox.warning(self, "加载失败", f"加载分类配置失败:\n{e}")

    def add_category(self):
        """添加新分类"""
        name, ok = QInputDialog.getText(self, "添加分类", "分类名称:")
        if not ok or not name.strip():
            return
        name = name.strip()
        # 检查重名
        if any(c["name"] == name for c in self.categories):
            QMessageBox.warning(self, "重复", f"分类 '{name}' 已存在")
            return
        self.categories.append({"name": name, "path": "", "extensions": []})
        self._refresh_category_list()
        self._save_categories()

    def remove_category(self):
        """删除选中的分类"""
        item = self.category_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        reply = QMessageBox.question(
            self, "确认删除", f"确定删除分类 '{name}' 吗？\n已分配到此分类的文件将变为未分配。",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.categories = [c for c in self.categories if c["name"] != name]
        # 清理已分配的文件
        for fname in [k for k, v in self.current_categories.items() if v == name]:
            del self.current_categories[fname]
        self._refresh_category_list()
        self._refresh_table_categories()
        self._save_categories()

    def set_category_path(self):
        """设置分类的真实目录路径"""
        item = self.category_list.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        cat = next((c for c in self.categories if c["name"] == name), None)
        if not cat:
            return
        path = QFileDialog.getExistingDirectory(
            self, f"选择 '{name}' 分类的目标目录", cat.get("path", ""))
        if path:
            cat["path"] = path
            self._refresh_category_list()
            self._save_categories()

    def rename_category(self, old_name: str):
        """重命名分类（ticket C：右键分类列表触发）

        同步更新：categories 配置 / current_categories（手动分类映射）/
        predictions（预测分类）/ state.json classified_files（已处理记录）。
        """
        name, ok = QInputDialog.getText(
            self, "重命名分类", "新分类名称:", text=old_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "分类名称不能为空")
            return
        if name == old_name:
            return
        if any(c["name"] == name for c in self.categories):
            QMessageBox.warning(self, "重复", f"分类 '{name}' 已存在")
            return
        # 1. 改分类配置
        cat = next((c for c in self.categories if c["name"] == old_name), None)
        if not cat:
            return
        cat["name"] = name
        # 2. 同步手动分类映射
        for fname, cname in self.current_categories.items():
            if cname == old_name:
                self.current_categories[fname] = name
        # 3. 同步预测结果
        for pred in self.predictions.values():
            if isinstance(pred, dict) and pred.get("category") == old_name:
                pred["category"] = name
        # 4. 同步 state.json 中的已处理记录（后端扫描依赖分类名展示）
        self.state_manager.rename_category_in_classified(old_name, name)
        # 5. 持久化 + 刷新界面
        self._save_categories()
        self._refresh_category_list()
        self._refresh_table_categories()
        self._update_status(f"分类 '{old_name}' 已重命名为 '{name}'")

    def _save_categories(self):
        """保存分类配置到默认文件"""
        try:
            save_categories(CONFIG_PATH, self.categories)
        except Exception as e:
            print(f"保存分类配置失败: {e}")

    # ── 文件扫描 ──

    def browse_directory(self):
        """浏览选择目录"""
        path = QFileDialog.getExistingDirectory(self, "选择源目录", self.dir_edit.text() or "")
        if path:
            self.dir_edit.setText(path)

    def scan_files(self, keep_categories: bool = False):
        """扫描源目录文件（ticket 05：同时作为导航入口，初始化导航历史）

        Args:
            keep_categories: True 时保留 current_categories/skipped_files
                （启动自动扫描用，避免清掉刚从 state.json 恢复的分类映射）；
                False 时清空全部数据（用户手动点"扫描文件"时，视为全新开始）。
        """
        directory = self.dir_edit.text().strip()
        if not directory:
            QMessageBox.warning(self, "提示", "请先选择源目录")
            return
        if not os.path.isdir(directory):
            QMessageBox.warning(self, "错误", f"目录不存在: {directory}")
            return

        # ticket E：把当前会话绑定到源目录；若该目录已有其它会话则切换过去扫描
        if not self._bind_session_source(directory):
            return

        # 记忆上次源目录
        self.state_manager.save_last_source_dir(directory)

        # ticket 05：从源目录扫描视为导航起点，重置历史栈
        self.current_scan_dir = os.path.abspath(directory)
        self._nav_history = []
        self._pending_scroll_restore = ""  # 全新扫描，不恢复滚动位置

        if keep_categories:
            # 仅清空文件列表和预测，保留分类映射（启动自动扫描场景）
            self.files = []
            self.predictions = {}
            self.file_table.setRowCount(0)
        else:
            # 清空旧数据（用户手动扫描，全新开始）
            self._reset_data_for_new_scan()
        self._start_scan_thread(self.current_scan_dir)

    def _save_nav_memory(self, focus_filename: str = ""):
        """保存当前目录的滚动位置和焦点文件名到 _nav_memory

        Args:
            focus_filename: 进入子目录时，该子目录的文件名（用于返回后高亮该行）
        """
        if not self.current_scan_dir:
            return
        scrollbar = self.file_table.verticalScrollBar()
        self._nav_memory[self.current_scan_dir] = {
            "scroll": scrollbar.value(),
            "focus": focus_filename,
        }

    def _restore_nav_state(self):
        """扫描完成后恢复滚动位置和高亮行（若 _pending_scroll_restore 指定了目录）

        由 QTimer.singleShot(0, ...) 延迟调用，确保表格布局已完成、scrollbar 范围正确。
        """
        target = self._pending_scroll_restore
        self._pending_scroll_restore = ""  # 用完即清
        if not target or target not in self._nav_memory:
            return
        mem = self._nav_memory[target]
        # 恢复滚动位置
        scrollbar = self.file_table.verticalScrollBar()
        scrollbar.setValue(mem["scroll"])
        # 高亮进入子目录时的那一行（selectRow 同时设焦点+选中，显示高亮色）
        focus_name = mem.get("focus", "")
        if focus_name:
            for row in range(self.file_table.rowCount()):
                name_item = self.file_table.item(row, self.COL_NAME)
                if name_item and name_item.data(Qt.UserRole) == focus_name:
                    self.file_table.selectRow(row)
                    break

    def _navigate_to_subdir(self, path: str):
        """双击/Enter/ctrl+单击文件夹：进入子文件夹浏览（ticket 05）

        - 保存当前目录的滚动位置 + 记录进入的子目录名
        - 把当前目录压入历史栈
        - 更新 current_scan_dir 和 dir_edit
        - 重新扫描（不清空 current_categories 等用户已分类数据，
          因为同一次会话内分类状态应跨导航保持）
        """
        if not path or not os.path.isdir(path):
            return
        # 保存当前目录的滚动位置 + 记录进入的子目录名
        subfolder_name = os.path.basename(os.path.abspath(path))
        self._save_nav_memory(focus_filename=subfolder_name)
        # 压入历史
        if self.current_scan_dir:
            self._nav_history.append(self.current_scan_dir)
        self.current_scan_dir = os.path.abspath(path)
        self.dir_edit.setText(self.current_scan_dir)
        # 进入子目录不恢复滚动位置（从顶部开始）
        self._pending_scroll_restore = ""
        # 不清空 current_categories/predictions/skipped_files（跨导航保持）
        self.files = []
        self.file_table.setRowCount(0)
        self._start_scan_thread(self.current_scan_dir)

    def _navigate_up(self):
        """上一级按钮/Backspace：从历史栈弹出上一级目录并重新扫描（ticket 05）

        返回后恢复离开时的滚动位置，并高亮进入子目录的那一行。
        """
        if not self._nav_history:
            return
        parent_dir = self._nav_history.pop()
        self.current_scan_dir = parent_dir
        self.dir_edit.setText(parent_dir)
        # 标记扫描完成后恢复 parent_dir 的滚动位置
        self._pending_scroll_restore = parent_dir
        self.files = []
        self.file_table.setRowCount(0)
        self._start_scan_thread(parent_dir)

    def _navigate_to_breadcrumb(self, path: str):
        """面包屑点击：跳转到指定层级（ticket 05）

        - 弹出历史栈直到匹配路径
        - 重置当前目录并扫描
        - 恢复目标层级的滚动位置
        """
        if not path or path == self.current_scan_dir:
            return
        # 弹出历史直到找到目标（或历史栈空）
        while self._nav_history:
            popped = self._nav_history.pop()
            if os.path.abspath(popped) == os.path.abspath(path):
                # 目标在历史栈中，重新压回（因为它是我们要去的）
                break
        self.current_scan_dir = os.path.abspath(path)
        self.dir_edit.setText(self.current_scan_dir)
        # 标记扫描完成后恢复该层级的滚动位置
        self._pending_scroll_restore = self.current_scan_dir
        self.files = []
        self.file_table.setRowCount(0)
        self._start_scan_thread(self.current_scan_dir)

    def _reset_data_for_new_scan(self):
        """从源目录重新扫描时清空所有用户数据"""
        self.files = []
        self.predictions = {}
        self.current_categories = {}
        self.skipped_files = set()
        self.reviewed_files = set()  # ticket I：已审状态随新扫描清空
        self.file_table.setRowCount(0)

    def _start_scan_thread(self, directory: str):
        """启动 FileScanThread 扫描指定目录（复用入口）"""
        self._show_progress(True)
        self._update_status(f"正在扫描: {directory}")
        self._refresh_breadcrumb()
        self._refresh_up_button_state()

        self.scan_thread = FileScanThread(directory)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.finished_signal.connect(self._on_scan_finished)
        self.scan_thread.start()

    def _refresh_breadcrumb(self):
        """刷新面包屑导航栏（ticket 05）"""
        # 清空旧按钮
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.current_scan_dir:
            self.breadcrumb_layout.addStretch()
            return

        # 按路径分隔符分段，每段一个可点击按钮
        path = self.current_scan_dir
        # Windows 路径：splitdrive + split
        drive, rest = os.path.splitdrive(path)
        parts = []
        if drive:
            parts.append(drive)
        if rest:
            # 去掉开头的分隔符后 split
            rest = rest.lstrip(os.sep)
            parts.extend([p for p in rest.split(os.sep) if p])

        # 累积路径
        cumulative = drive + os.sep if drive else ""
        for i, part in enumerate(parts):
            if i == 0 and drive:
                # 第一段是盘符
                cumulative = drive + os.sep
            else:
                cumulative = os.path.join(cumulative, part) if cumulative else part
            btn = QPushButton(part)
            btn.setFlat(True)
            btn.setStyleSheet(f"color: {ACCENT}; text-decoration: underline; padding: 2px 4px;")
            btn.setToolTip(cumulative)
            # 用默认参数捕获 cumulative 值
            btn.clicked.connect(lambda checked=False, p=cumulative: self._navigate_to_breadcrumb(p))
            self.breadcrumb_layout.addWidget(btn)
            # 加分隔符 ">"（最后一段不加）
            if i < len(parts) - 1:
                sep = QLabel(">")
                sep.setStyleSheet(f"color: {TEXT_DIM};")
                self.breadcrumb_layout.addWidget(sep)

        self.breadcrumb_layout.addStretch()

    def _refresh_up_button_state(self):
        """上一级按钮可用状态：有历史栈时可点"""
        self.up_btn.setEnabled(bool(self._nav_history))

    def _on_scan_progress(self, current, total, message):
        """扫描进度回调"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
        self._update_status(message)

    def _on_scan_finished(self, files):
        """扫描完成"""
        self.files = files
        self._populate_table()
        self._show_progress(False)
        self.file_count_label.setText(f"{len(files)} 个文件")
        self._update_status(f"扫描完成: {len(files)} 个文件")
        self._update_selection_status()  # 表格重填后刷新选中计数（blockSignals 阻止了 itemChanged）
        # 延迟恢复滚动位置和高亮行：表格布局（排序、行高计算）需要事件循环处理
        # 完成后 scrollbar 范围才正确，直接调 setValue 会被布局覆盖
        QTimer.singleShot(0, self._restore_nav_state)

    def _populate_table(self):
        """填充文件表格"""
        self.file_table.blockSignals(True)
        self.file_table.setSortingEnabled(False)
        self.file_table.setRowCount(len(self.files))

        for row, f in enumerate(self.files):
            # 复选框
            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check_item.setCheckState(Qt.Unchecked)
            self.file_table.setItem(row, self.COL_CHECK, check_item)

            # 文件名
            name_item = QTableWidgetItem(f["filename"])
            name_item.setData(Qt.UserRole, f["filename"])
            name_item.setToolTip(f["path"])
            self.file_table.setItem(row, self.COL_NAME, name_item)

            # 类型（系统图标 + 类型描述）
            is_dir = f.get("is_dir", False)
            type_text = describe_file_type(f["filename"], is_dir=is_dir)
            type_item = QTableWidgetItem(type_text)
            type_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            # QFileIconProvider 根据 QFileInfo 返回系统关联图标
            qfi = QFileInfo(f["path"])
            icon = self._icon_provider.icon(qfi)
            if not icon.isNull():
                type_item.setIcon(icon)
            self.file_table.setItem(row, self.COL_TYPE, type_item)

            # 大小
            size_item = QTableWidgetItem(human_readable_size(f["size"]))
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            size_item.setData(Qt.UserRole, f["size"])  # 用于排序
            self.file_table.setItem(row, self.COL_SIZE, size_item)

            # 修改时间
            mod_item = QTableWidgetItem(format_time(f["modified"]))
            mod_item.setData(Qt.UserRole, f["modified"])
            self.file_table.setItem(row, self.COL_MODIFIED, mod_item)

            # 创建时间
            created_item = QTableWidgetItem(format_time(f["created"]))
            created_item.setData(Qt.UserRole, f["created"])
            self.file_table.setItem(row, self.COL_CREATED, created_item)

            # 当前分类
            current_cat = self.current_categories.get(f["filename"], "")
            cur_item = QTableWidgetItem(current_cat)
            self.file_table.setItem(row, self.COL_CURRENT, cur_item)

            # 预测分类
            pred = self.predictions.get(f["filename"], {})
            pred_item = QTableWidgetItem(pred.get("category", ""))
            self.file_table.setItem(row, self.COL_PREDICTED, pred_item)

            # 置信度
            conf = pred.get("confidence", 0)
            conf_item = QTableWidgetItem(f"{conf:.0%}" if conf > 0 else "-")
            conf_item.setTextAlignment(Qt.AlignCenter)
            conf_item.setForeground(confidence_color(conf))
            conf_item.setData(Qt.UserRole, conf)  # ticket I：数值排序键
            self.file_table.setItem(row, self.COL_CONFIDENCE, conf_item)

            # 已审（ticket I：✓/空，仅 R2 审核通过产生）
            reviewed = f["filename"] in self.reviewed_files
            reviewed_item = QTableWidgetItem("✓" if reviewed else "")
            reviewed_item.setTextAlignment(Qt.AlignCenter)
            reviewed_item.setData(Qt.UserRole, 1 if reviewed else 0)
            reviewed_item.setForeground(QColor(SUCCESS) if reviewed else QColor(TEXT_DIM))
            self.file_table.setItem(row, self.COL_REVIEWED, reviewed_item)

            # 整行颜色根据置信度
            self._apply_row_color(row, pred.get("category", ""), conf)

        self.file_table.blockSignals(False)
        # ticket I：若排序已被用户激活，按当前可视列序维持排序（否则保留扫描顺序）
        if self._sort_active:
            self._sort_table()

    def _apply_row_color(self, row: int, predicted_cat: str, confidence: float):
        """根据预测状态设置行颜色"""
        # 不直接改背景色（会与选中色冲突），改用文字颜色提示
        # 置信度颜色已在置信度列体现
        pass

    def _refresh_table_categories(self):
        """刷新表格中的分类列"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            filename = self.file_table.item(row, self.COL_NAME).data(Qt.UserRole)
            # 当前分类
            current_cat = self.current_categories.get(filename, "")
            self.file_table.item(row, self.COL_CURRENT).setText(current_cat)
        self.file_table.blockSignals(False)
        # ticket I：分类列参与排序，值变化后重排（仅在排序已激活时）
        if self._sort_active:
            self._sort_table()

    def _refresh_table_predictions(self):
        """刷新表格中的预测列"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            filename = self.file_table.item(row, self.COL_NAME).data(Qt.UserRole)
            pred = self.predictions.get(filename, {})
            pred_item = self.file_table.item(row, self.COL_PREDICTED)
            pred_item.setText(pred.get("category", ""))
            # ticket J：冲突项在主表格提示（不改文本，避免影响排序键）
            if pred.get("conflict"):
                pred_item.setForeground(QColor(WARNING))
                tip = (f"⚠ 相似文件名冲突：与「{pred.get('conflict_with', '')}」"
                       f"高度相似但分类不同，请重点核查")
                if pred.get("reason"):
                    tip += f"\nLLM 理由：{pred['reason']}"
                pred_item.setToolTip(tip)
            else:
                pred_item.setForeground(QColor(TEXT_PRIMARY))
                pred_item.setToolTip("")
            conf = pred.get("confidence", 0)
            conf_item = self.file_table.item(row, self.COL_CONFIDENCE)
            conf_item.setText(f"{conf:.0%}" if conf > 0 else "-")
            conf_item.setData(Qt.UserRole, conf)
            conf_item.setForeground(confidence_color(conf))
        self.file_table.blockSignals(False)
        # ticket I：预测列参与排序，值变化后重排（仅在排序已激活时）
        if self._sort_active:
            self._sort_table()

    # ── 选择操作 ──

    def _on_table_item_changed(self, item: QTableWidgetItem):
        """表格项变化（复选框切换）→ 更新文件列表操作行状态显示"""
        if item.column() == self.COL_CHECK:
            self._update_selection_status()

    def _open_file_row(self, row: int):
        """ctrl+单击打开对象（FileTableWidget.open_requested 信号处理）

        - 文件：os.startfile 用系统默认程序打开
        - 文件夹：ticket 05 进入子文件夹浏览（_navigate_to_subdir）
        """
        if row < 0 or row >= self.file_table.rowCount():
            return
        name_item = self.file_table.item(row, self.COL_NAME)
        if not name_item:
            return
        filename = name_item.data(Qt.UserRole)
        if not filename:
            return
        # 通过 self.files 查找完整路径和 is_dir 标记
        path = None
        is_dir = False
        for f in self.files:
            if f.get("filename") == filename:
                path = f.get("path")
                is_dir = f.get("is_dir", False)
                break
        if not path:
            QMessageBox.warning(self, "无法打开", f"找不到文件路径：{filename}")
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, "无法打开", f"对象不存在：{path}")
            return
        # 文件夹 → 进入子文件夹浏览（ticket 05）
        if is_dir:
            self._navigate_to_subdir(path)
            return
        # 文件 → os.startfile 默认程序打开
        try:
            os.startfile(path)
        except OSError as e:
            QMessageBox.warning(self, "打开失败", f"无法打开 {filename}：{e}")

    def select_all(self):
        """全选"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            self.file_table.item(row, self.COL_CHECK).setCheckState(Qt.Checked)
        self.file_table.blockSignals(False)
        self._update_selection_status()  # blockSignals 阻止了 itemChanged，手动更新

    def deselect_all(self):
        """取消全选"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            self.file_table.item(row, self.COL_CHECK).setCheckState(Qt.Unchecked)
        self.file_table.blockSignals(False)
        self._update_selection_status()

    def invert_selection(self):
        """反选"""
        self.file_table.blockSignals(True)
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, self.COL_CHECK)
            new_state = Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked
            item.setCheckState(new_state)
        self.file_table.blockSignals(False)
        self._update_selection_status()

    def _get_checked_filenames(self) -> list:
        """获取所有勾选的文件名"""
        result = []
        for row in range(self.file_table.rowCount()):
            if self.file_table.item(row, self.COL_CHECK).checkState() == Qt.Checked:
                filename = self.file_table.item(row, self.COL_NAME).data(Qt.UserRole)
                result.append(filename)
        return result

    def _get_selected_filenames(self) -> list:
        """获取当前选中（高亮）的文件名"""
        result = []
        for row in self.file_table.selectionModel().selectedRows():
            filename = self.file_table.item(row.row(), self.COL_NAME).data(Qt.UserRole)
            result.append(filename)
        return result

    # ── 右键菜单 ──

    def _show_context_menu(self, pos):
        """显示右键菜单（4 组 10 项）

        新交互模型（ticket 03）：复选框勾选=真选中，selectionModel=焦点。
        右键菜单优先作用于勾选行；无勾选时把光标所在行设为焦点行作为回退目标。

        菜单结构（ticket 04）：
        - 组1 打开：打开 / 在资源管理器中显示 / 查看详情
        - 组2 分类：分配到分类 ▶ / 重置分类
        - 组3 跳过：跳过·取消跳过 / 清除分类
        - 组4 复制：复制路径 / 复制文件名
        """
        # 把光标所在行设为当前行（焦点），确保无勾选时回退到该行
        row_at = self.file_table.rowAt(pos.y())
        if row_at >= 0:
            self.file_table.setCurrentCell(row_at, 0)

        menu = QMenu(self)

        # ── 组1：打开 ──
        open_action = menu.addAction("打开")
        open_action.triggered.connect(self._open_current_row)

        explorer_action = menu.addAction("在资源管理器中显示")
        explorer_action.triggered.connect(self._show_in_explorer)

        details_action = menu.addAction("查看详情")
        details_action.triggered.connect(lambda: self._show_details_for_row(self.file_table.currentRow()))

        menu.addSeparator()

        # ── 组2：分类 ──
        assign_menu = menu.addMenu("分配到分类")
        for cat in self.categories:
            action = assign_menu.addAction(cat["name"])
            action.triggered.connect(lambda checked, n=cat["name"]: self._assign_selected(n))

        reset_action = menu.addAction("重置分类")
        reset_action.triggered.connect(self._reset_classification)

        menu.addSeparator()

        # ── 组3：跳过 ──
        skip_action = menu.addAction("跳过/忽略（本轮不处理）")
        skip_action.triggered.connect(self._skip_selected)

        unskip_action = menu.addAction("取消跳过")
        unskip_action.triggered.connect(self._unskip_selected)

        clear_action = menu.addAction("清除当前分类")
        clear_action.triggered.connect(lambda: self._assign_selected(""))

        menu.addSeparator()

        # ── 组4：复制 ──
        copy_path_action = menu.addAction("复制路径")
        copy_path_action.triggered.connect(self._copy_path)

        copy_name_action = menu.addAction("复制文件名")
        copy_name_action.triggered.connect(self._copy_filename)

        menu.exec(self.file_table.viewport().mapToGlobal(pos))

    def _show_details_for_row(self, row: int):
        """ctrl+enter / 右键"查看详情"：弹出 DetailsDialog 显示对象详情"""
        if row < 0 or row >= self.file_table.rowCount():
            return
        name_item = self.file_table.item(row, self.COL_NAME)
        if not name_item:
            return
        filename = name_item.data(Qt.UserRole)
        if not filename:
            return
        # 查找完整 file_info
        file_info = None
        for f in self.files:
            if f.get("filename") == filename:
                file_info = f
                break
        if not file_info:
            QMessageBox.warning(self, "无法显示详情", f"找不到文件信息：{filename}")
            return
        # 收集分类/预测/状态信息
        current_cat = self.current_categories.get(filename, "")
        pred = self.predictions.get(filename, {})
        predicted_cat = pred.get("category", "") if isinstance(pred, dict) else ""
        confidence = pred.get("confidence", 0.0) if isinstance(pred, dict) else 0.0
        # 状态文本
        if filename in self.skipped_files:
            status = "已跳过"
        elif self.state_manager.is_processed(file_info.get("path", "")):
            status = "已处理"
        else:
            status = "未处理"
        dlg = DetailsDialog(
            file_info=file_info,
            current_category=current_cat,
            predicted_category=predicted_cat,
            confidence=confidence,
            status=status,
            parent=self,
        )
        dlg.exec()

    def _open_current_row(self):
        """右键"打开"：复用 ctrl+单击打开逻辑"""
        self._open_file_row(self.file_table.currentRow())

    def _show_in_explorer(self):
        """右键"在资源管理器中显示"：subprocess 调 explorer /select"""
        import subprocess
        path = self._get_current_row_path()
        if not path or not os.path.exists(path):
            return
        try:
            subprocess.Popen(["explorer", "/select,", path])
        except (OSError, subprocess.SubprocessError) as e:
            QMessageBox.warning(self, "失败", f"无法打开资源管理器：{e}")

    def _reset_classification(self):
        """右键"重置分类"：清除已处理状态，让文件重新被扫描

        调 state_manager.reset_classification(path) 清除 state.json 中的已处理标记。
        同时清除当前分类和预测，让文件回到"未处理"状态。
        """
        filenames = self._get_checked_filenames() or self._get_selected_filenames()
        if not filenames:
            QMessageBox.information(self, "提示", "请先选择文件")
            return
        reset_count = 0
        for fname in filenames:
            path = self._find_path_by_filename(fname)
            if path:
                self.state_manager.reset_classification(path)
            self.current_categories.pop(fname, None)
            self.predictions.pop(fname, None)
            self.skipped_files.discard(fname)
            reset_count += 1
        self._refresh_table_categories()
        self._refresh_table_predictions()
        self._update_status(f"已重置 {reset_count} 个文件的分类状态")

    def _copy_path(self):
        """右键"复制路径"：复制完整路径到剪贴板"""
        path = self._get_current_row_path()
        if not path:
            return
        cb = QApplication.clipboard()
        if cb:
            cb.setText(path)
            self._update_status(f"已复制路径：{path}")

    def _copy_filename(self):
        """右键"复制文件名"：复制文件名到剪贴板"""
        row = self.file_table.currentRow()
        if row < 0:
            return
        name_item = self.file_table.item(row, self.COL_NAME)
        if not name_item:
            return
        filename = name_item.data(Qt.UserRole)
        if not filename:
            return
        cb = QApplication.clipboard()
        if cb:
            cb.setText(filename)
            self._update_status(f"已复制文件名：{filename}")

    def _get_current_row_path(self):
        """获取当前焦点行的完整文件路径"""
        row = self.file_table.currentRow()
        if row < 0:
            return None
        name_item = self.file_table.item(row, self.COL_NAME)
        if not name_item:
            return None
        filename = name_item.data(Qt.UserRole)
        if not filename:
            return None
        return self._find_path_by_filename(filename)

    def _find_path_by_filename(self, filename: str):
        """通过文件名查找完整路径（self.files 中 filename 作为 key）"""
        for f in self.files:
            if f.get("filename") == filename:
                return f.get("path")
        return None

    def _assign_selected(self, category: str):
        """将选中的文件分配到指定分类

        优先级（ticket 03 交互模型）：复选框勾选行 > 焦点行（selectionModel）
        """
        filenames = self._get_checked_filenames() or self._get_selected_filenames()
        if not filenames:
            QMessageBox.information(self, "提示", "请先选择文件")
            return
        for fname in filenames:
            if category:
                self.current_categories[fname] = category
            else:
                self.current_categories.pop(fname, None)
        self._refresh_table_categories()
        self._refresh_category_list()
        self._update_status(f"已分配 {len(filenames)} 个文件到 '{category or '未分配'}'")

    def _skip_selected(self):
        """标记选中文件为跳过（优先勾选行，回退焦点行）"""
        filenames = self._get_checked_filenames() or self._get_selected_filenames()
        if not filenames:
            return
        for fname in filenames:
            self.skipped_files.add(fname)
        self._update_status(f"已标记 {len(filenames)} 个文件为跳过")

    def _unskip_selected(self):
        """取消跳过标记（优先勾选行，回退焦点行）"""
        filenames = self._get_checked_filenames() or self._get_selected_filenames()
        if not filenames:
            return
        for fname in filenames:
            self.skipped_files.discard(fname)
        self._update_status(f"已取消 {len(filenames)} 个文件的跳过标记")

    # ── 拖放处理 ──

    def _on_files_dropped_to_category(self, category: str, filenames: list):
        """文件被拖拽到分类"""
        for fname in filenames:
            self.current_categories[fname] = category
        self._refresh_table_categories()
        self._refresh_category_list()
        self._update_status(f"已分配 {len(filenames)} 个文件到 '{category}'")

    # ── LLM 预测 ──

    def _on_predict_scope_changed(self, action):
        """ticket B：切换预测范围（全部未分类 / 仅勾选项）"""
        if action == self._scope_selected_action:
            self._predict_scope = "selected"
            self._update_status("预测范围：仅勾选项")
        else:
            self._predict_scope = "all"
            self._update_status("预测范围：全部未分类")

    def predict_all(self, scope: str = None):
        """预测未手动分类的文件和文件夹（ticket 07：文件夹走 classify_folder）

        Args:
            scope: "all"=全部未分类（默认），"selected"=仅勾选项（ticket B）。
                   None 时使用当前下拉选择的 self._predict_scope。
        """
        if scope is None:
            scope = self._predict_scope

        if not self.files:
            QMessageBox.warning(self, "提示", "请先扫描文件")
            return

        # ticket B：仅勾选项模式下，先取勾选集（空则提示返回）
        checked_names = None
        if scope == "selected":
            checked_names = set(self._get_checked_filenames())
            if not checked_names:
                QMessageBox.information(self, "提示", "请先勾选要预测的文件/文件夹")
                return

        # 检查后端
        if not check_backend_available():
            reply = QMessageBox.question(
                self, "后端不可用",
                "后端 LLM 服务不可用，是否使用规则匹配（基于扩展名）？",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # 构建分类映射（用户手动分类的文件作为 LLM 参考映射，文件夹不作为映射）
        sample_files = []
        all_files = []
        folder_entries = []
        for f in self.files:
            fname = f["filename"]
            if f.get("is_dir", False):
                # 文件夹：若未手动分类则加入待预测文件夹列表
                if fname not in self.current_categories:
                    if checked_names is None or fname in checked_names:
                        folder_entries.append(f)
            else:
                file_info = {"filename": fname, "size": f["size"]}
                if fname in self.current_categories:
                    file_info["category"] = self.current_categories[fname]
                    sample_files.append(file_info)
                else:
                    # ticket B：勾选项模式只保留勾选的文件（注意键是 "filename"）
                    if checked_names is None or fname in checked_names:
                        all_files.append(file_info)

        if not all_files and not folder_entries:
            if checked_names is not None:
                QMessageBox.information(
                    self, "提示", "勾选项中没有任何待预测条目（勾选的文件都已手动分类）")
            else:
                QMessageBox.information(self, "提示", "没有需要预测的条目（所有文件已手动分类）")
            return

        if not sample_files and all_files:
            reply = QMessageBox.question(
                self, "无分类映射",
                "您还没有手动分类任何文件作为映射参考。\nLLM 将仅根据文件名进行预测，是否继续？",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # 启动预测线程 + 打开模态流式审核窗口（ticket F：R1 流式审核）
        self.predict_btn.setEnabled(False)
        self.predict_scope_btn.setEnabled(False)
        self._set_session_switching_enabled(False)  # ticket E：预测中禁止切换会话
        scope_text = "勾选项" if scope == "selected" else "全部未分类项"
        self._update_status(f"正在预测（{scope_text}）...")

        # ticket J：收集其它会话摘要（跨会话样本借补用；排除当前会话）
        other_sessions = []
        try:
            cur_src = self.dir_edit.text().strip()
            cur_id = derive_session_id(cur_src) if cur_src else ""
            for _path, st in list_sessions():
                if cur_id and st.session_id == cur_id:
                    continue
                if not st.current_categories:
                    continue
                other_sessions.append({
                    "session_id": st.session_id,
                    "title": st.title,
                    "source_dir": st.source_dir,
                    "current_categories": dict(st.current_categories),
                })
        except Exception:
            other_sessions = []

        self.predict_thread = PredictThread(sample_files, all_files,
                                            self.categories, folder_entries,
                                            source_dir=self.dir_edit.text().strip(),
                                            other_sessions=other_sessions)

        # ticket F：模态流式审核窗口（阻塞主窗口；用户可中断；逐条接受/调整/跳过）
        file_info_map = {f["filename"]: f for f in self.files}
        category_names = [c["name"] for c in self.categories]
        self._stream_stats = {"accepted": 0, "adjusted": 0, "skipped": 0}
        dialog = StreamReviewDialog(category_names, file_info_map, parent=self)
        self._stream_dialog = dialog

        self.predict_thread.progress.connect(dialog.set_progress)
        self.predict_thread.item_predicted.connect(self._on_stream_prediction)
        self.predict_thread.finished_signal.connect(self._on_stream_finished)
        self.predict_thread.error_signal.connect(self._on_stream_error)
        dialog.decision_made.connect(self._on_stream_decision)
        dialog.stop_requested.connect(self.predict_thread.request_stop)

        self.predict_thread.start()
        dialog.exec()  # 模态阻塞：直到预测结束（或中断）且用户关闭窗口

        # exec 返回后兜底：回收仍在运行的线程（如用户异常关闭路径）
        if self.predict_thread is not None and self.predict_thread.isRunning():
            self.predict_thread.request_stop()
            self.predict_thread.wait(3000)

        # 崩溃防护：保活对话框 wrapper 直至其 C++ 销毁（见 __init__ 处注释）。
        # 若不保活，本函数返回后 wrapper 引用归零被 GC，而队列中 Qt 内部投递的
        # DeferredDelete 事件稍后送达 → 访问违规（实测确定性段错误）。
        self._stream_dialog_refs.append(dialog)
        try:
            def _release_on_destroyed(_obj=None, d=dialog):
                # 兜底：应用退出销毁链中主窗口 wrapper 可能已失效，忽略一切异常
                try:
                    self._release_stream_dialog_ref(d)
                except Exception:
                    pass
            dialog.destroyed.connect(_release_on_destroyed)
        except RuntimeError:
            pass
        self._stream_dialog = None
        self._refresh_table_predictions()
        self.predict_btn.setEnabled(True)
        self.predict_scope_btn.setEnabled(True)
        self._set_session_switching_enabled(True)
        s = self._stream_stats
        stopped = bool(getattr(self.predict_thread, "stopped", False))
        prefix = "预测已中断" if stopped else "预测完成"
        self._update_status(
            f"{prefix}: 接受 {s['accepted']}，调整 {s['adjusted']}，跳过 {s['skipped']}")

    def _release_stream_dialog_ref(self, dialog):
        """ticket F：对话框 C++ 对象销毁后释放其保活引用（崩溃防护配套）"""
        self._stream_dialog_refs = [d for d in self._stream_dialog_refs
                                    if d is not dialog]

    def _on_stream_prediction(self, pred: dict):
        """ticket F：单条预测到达——记录预测 + 渲染到流式审核窗口"""
        fname = pred.get("filename", "")
        if not fname:
            return
        record = {
            "category": pred.get("category", ""),
            "confidence": pred.get("confidence", 0.0),
            "is_folder": pred.get("is_folder", False),
            "degraded": pred.get("degraded", False),
        }
        # ticket J：保留冲突标记/理由（供主表格与审核展示）
        if pred.get("conflict"):
            record["conflict"] = True
            if pred.get("conflict_with"):
                record["conflict_with"] = pred["conflict_with"]
        if pred.get("reason"):
            record["reason"] = pred["reason"]
        self.predictions[fname] = record
        if self._stream_dialog is not None:
            self._stream_dialog.add_prediction(pred)

    def _on_stream_decision(self, filename: str, action: str, category: str):
        """ticket F：流式窗口用户决策回写当前会话

        accept/adjust → current_categories（R1 只是暂定分类，不标"已审"，
        已审仅由 R2 产生，见已确认决策 Q9）；skip → skipped_files。
        """
        if action == "skip":
            self.skipped_files.add(filename)
            self.current_categories.pop(filename, None)
            self._stream_stats["skipped"] += 1
        elif category:
            self.current_categories[filename] = category
            self.skipped_files.discard(filename)
            if action == "adjust":
                self._stream_stats["adjusted"] += 1
            else:
                self._stream_stats["accepted"] += 1

    def _on_stream_finished(self, predictions: list):
        """ticket F：预测线程结束——兜底合并预测 + 刷新主表格 + 通知流式窗口"""
        for pred in predictions:
            fname = pred.get("filename", "")
            if fname and fname not in self.predictions:
                record = {
                    "category": pred.get("category", ""),
                    "confidence": pred.get("confidence", 0.0),
                    "is_folder": pred.get("is_folder", False),
                    "degraded": pred.get("degraded", False),
                }
                # ticket J：保留冲突标记/理由
                if pred.get("conflict"):
                    record["conflict"] = True
                    if pred.get("conflict_with"):
                        record["conflict_with"] = pred["conflict_with"]
                if pred.get("reason"):
                    record["reason"] = pred["reason"]
                self.predictions[fname] = record
        self._refresh_table_predictions()
        stopped = bool(getattr(self.predict_thread, "stopped", False))
        if self._stream_dialog is not None:
            self._stream_dialog.on_all_finished(stopped)

    def _on_stream_error(self, error: str):
        """ticket F：预测出错 → 转给流式窗口显示"""
        if self._stream_dialog is not None:
            self._stream_dialog.on_error(error)
        self._update_status(f"预测失败: {error}")

    # ── 审核与应用 ──

    def _collect_review_items(self) -> list:
        """收集所有有分类归属的条目供 R2 审核（ticket G：暂存分类也进入审核）

        Returns:
            [{"filename", "path", "size", "modified", "is_dir", "category"}, ...]
            分类取手动 current_categories 优先，其次预测；跳过 skipped_files 与无分类项。
        """
        items = []
        for f in self.files:
            fname = f["filename"]
            if fname in self.skipped_files:
                continue
            category = (self.current_categories.get(fname)
                        or self.predictions.get(fname, {}).get("category"))
            if not category:
                continue
            cat = next((c for c in self.categories if c["name"] == category), None)
            if not cat:
                continue
            items.append({
                "filename": fname,
                "path": f["path"],
                "size": f.get("size", 0),
                "modified": f.get("modified", 0),
                "is_dir": f.get("is_dir", False),
                "category": category,
            })
        return items

    def _open_step_review(self):
        """ticket G+H：打开 R2 专门审核界面（逐类核对）并回写结果

        - 未跳过 → 标"已审"（已审仅由 R2 产生，Q9）
        - 跳过 → 清除分类（手动+预测），无所属（Q8）
        """
        if not self.files:
            QMessageBox.warning(self, "提示", "请先扫描文件")
            return
        items = self._collect_review_items()
        if not items:
            QMessageBox.information(
                self, "提示", "没有可审核的条目（请先手动分类或 AI 预测）")
            return

        dialog = StepReviewDialog(items, self.categories, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        results = dialog.get_results()
        reviewed = set(results.get("reviewed", []))
        skipped = set(results.get("skipped", []))

        # 跳过 → 清除分类（无所属，Q8）：同时移除手动分类与预测记录
        for fname in skipped:
            self.current_categories.pop(fname, None)
            self.predictions.pop(fname, None)
            self.reviewed_files.discard(fname)
        # 未跳过 → 标"已审"（Q9）
        self.reviewed_files |= reviewed

        # 刷新主表格（当前分类/预测列/已审列）
        self._refresh_table_categories()
        self._refresh_table_predictions()
        self._refresh_table_reviewed()
        if self._sort_active:
            self._sort_table()
        self._update_status(
            f"审核完成：{len(reviewed)} 项已审，{len(skipped)} 项跳过（已清除分类）")

    def _confirm_and_move(self):
        """ticket H：移动——仅移动标为"已审"的条目（Q10）；暂存分类标记已处理（ticket G）

        弹窗仅询问确认，不展示其它信息（Q10）。
        """
        if not self.files:
            QMessageBox.warning(self, "提示", "请先扫描文件")
            return

        moves = []
        staging = []  # 已审但分类 path 为空（暂存）：仅标记已处理
        for f in self.files:
            fname = f["filename"]
            if fname not in self.reviewed_files:
                continue  # 未审项不动（Q10）
            category = (self.current_categories.get(fname)
                        or self.predictions.get(fname, {}).get("category"))
            cat = (next((c for c in self.categories if c["name"] == category), None)
                   if category else None)
            if not cat:
                continue
            if not cat.get("path"):
                staging.append({"path": f["path"], "category": category})
                continue
            moves.append({
                "src": f["path"],
                "dst": os.path.join(cat["path"], fname),
                "filename": fname,
                "category": category,
                "is_folder": f.get("is_dir", False),
            })

        if not moves and not staging:
            QMessageBox.information(
                self, "提示", "没有可移动的已审条目\n（请先用「审核」按钮完成审核）")
            return

        # 仅确认弹窗（Q10：不再展示其它信息）
        reply = QMessageBox.question(
            self, "确认移动", "是否移动所有已审文件？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # ticket G：暂存已审条目仅标记已处理（不移动）
        for entry in staging:
            self.state_manager.mark_classified(entry["path"], entry["category"])

        if moves:
            self._execute_moves(moves, "ask")
        else:
            QMessageBox.information(
                self, "完成", f"暂存已审 {len(staging)} 项已标记已处理（不移动）")
            self._update_status(f"暂存已审 {len(staging)} 项已标记已处理")

    def _execute_moves(self, moves: list, conflict_mode: str):
        """执行文件/文件夹移动（ticket 07：保存 moves 供完成后标记已处理）"""
        self._show_progress(True)
        self._update_status(f"正在移动 {len(moves)} 个条目...")
        self._pending_moves = moves  # 保存供 _on_move_finished 标记已处理
        self._set_session_switching_enabled(False)  # ticket E：移动中禁止切换会话

        self.move_thread = MoveFilesThread(moves, conflict_mode)
        self.move_thread.progress.connect(self._on_move_progress)
        self.move_thread.finished_signal.connect(self._on_move_finished)
        self.move_thread.error_signal.connect(self._on_move_error)
        self.move_thread.conflict_signal.connect(self._on_move_conflict)
        self.move_thread.start()

    def _on_move_progress(self, current, total, message):
        """移动进度"""
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
        self._update_status(message)

    def _on_move_conflict(self, src: str, dst: str, is_folder: bool):
        """文件/文件夹冲突，询问用户（ticket 07：文件夹显示 merge 选项）"""
        dialog = ConflictDialog(os.path.basename(src), dst, is_folder, self)
        dialog.exec()
        self.move_thread.set_conflict_resolution(dialog.resolution, dialog.apply_to_all)

    def _on_move_finished(self, moved: int, skipped: int):
        """移动完成（ticket 07：调 state_manager.mark_classified 标记已处理）"""
        self._show_progress(False)
        self._set_session_switching_enabled(True)
        # 标记成功移动的条目为已处理（"分类即已处理"机制）
        if hasattr(self, "_pending_moves"):
            for mv in self._pending_moves:
                # 简化处理：所有 pending_moves 都标记已处理
                # （移动失败已在 MoveFilesThread 内部统计为 skipped，但源文件仍在）
                # 这里仅标记分类已处理，避免后端 <data_drive>:/DownloadscanAction 重复扫描
                self.state_manager.mark_classified(mv["src"], mv.get("category", ""))
            self._pending_moves = []
        QMessageBox.information(
            self, "完成", f"移动完成\n成功: {moved}\n跳过: {skipped}")
        self._update_status(f"移动完成: 成功 {moved}，跳过 {skipped}")
        # 重新扫描刷新列表
        if self.dir_edit.text():
            self.scan_files()

    def _on_move_error(self, error: str):
        """移动出错"""
        self._show_progress(False)
        self._set_session_switching_enabled(True)
        self._update_status(f"移动出错: {error}")

    # ── 分类映射保存/加载 ──

    def save_sample_config(self):
        """保存当前分类映射"""
        if not self.current_categories:
            QMessageBox.warning(self, "提示", "当前没有手动分类的文件，无法保存分类映射")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存分类映射", "file_sample.json", "JSON 文件 (*.json)")
        if not path:
            return
        sample_files = [
            {"filename": fname, "category": cat}
            for fname, cat in self.current_categories.items()
        ]
        try:
            save_sample(path, sample_files, self.categories, self.dir_edit.text())
            self.state_manager.save_last_sample_path(path)
            self._update_status(f"分类映射已保存: {path}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"保存分类映射失败:\n{e}")

    def load_sample_config(self):
        """加载分类映射"""
        last_path = self.state_manager.get_last_sample_path()
        open_dir = os.path.dirname(last_path) if last_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "加载分类映射", open_dir, "JSON 文件 (*.json)")
        if not path:
            return
        try:
            data = load_sample(path)
            self.state_manager.save_last_sample_path(path)
            # 加载分类配置
            if data.get("categories"):
                self.categories = data["categories"]
                self._refresh_category_list()
                self._save_categories()
            # 加载分类映射
            for sample in data.get("sample_files", []):
                self.current_categories[sample["filename"]] = sample["category"]
            self._refresh_table_categories()
            self._refresh_category_list()
            # 加载源目录
            if data.get("source_dir"):
                self.dir_edit.setText(data["source_dir"])
            self._update_status(f"分类映射已加载: {len(data.get('sample_files', []))} 个映射")
        except Exception as e:
            QMessageBox.warning(self, "加载失败", f"加载分类映射失败:\n{e}")

    def _open_help(self):
        """打开操作指引文档（ticket 09：用系统默认程序打开 tools/file_classifier/file_classifier_guide.md）"""
        guide_path = Path(__file__).parent / "file_classifier_guide.md"
        if not guide_path.exists():
            QMessageBox.warning(self, "文档缺失", f"操作指引文档不存在:\n{guide_path}")
            return
        try:
            os.startfile(str(guide_path))
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开操作指引:\n{e}")

    # ── 工具方法 ──

    def _show_progress(self, show: bool):
        """显示/隐藏进度条"""
        self.progress_bar.setVisible(show)
        if show:
            self.progress_bar.setValue(0)

    def _update_status(self, message: str):
        """更新状态栏"""
        self.status_label.setText(message)

    def _on_multi_select_mode_changed(self, state):
        """多选模式复选框切换：同步表格行为并更新文件列表操作行状态"""
        # stateChanged 传 int：0=Unchecked, 2=Checked；用 bool 兼容 PySide6 的枚举/整数信号值
        self.file_table.set_multi_select_mode(bool(state))
        self._update_selection_status()

    def _update_selection_status(self):
        """更新底部状态行：已选择数量 + 当前模式（ticket D：拆分标签）"""
        mode_text = "多选" if self.file_table.is_multi_select_mode() else "普通"
        count = len(self._get_checked_filenames())
        self.selection_count_label.setText(f"已选择 {count} 项")
        self.mode_label.setText(f"模式：{mode_text}")



class ConflictDialog(QDialog):
    """文件/文件夹冲突对话框（ticket 07：文件夹显示 merge 选项，隐藏 overwrite）"""

    def __init__(self, filename: str, dst_path: str, is_folder: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件夹冲突" if is_folder else "文件冲突")
        self.setMinimumWidth(450)
        self.resolution = "skip"
        self.apply_to_all = False
        self.is_folder = is_folder

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"目标{'文件夹' if is_folder else '文件'}已存在:"))
        name_label = QLabel(filename)
        name_label.setStyleSheet(f"font-weight: bold; color: {WARNING};")
        layout.addWidget(name_label)
        layout.addWidget(QLabel(f"路径: {dst_path}"))

        # 选项（根据 is_folder 显示不同按钮）
        if is_folder:
            # 文件夹冲突：merge / skip / rename（不支持 overwrite）
            self.merge_btn = QPushButton("合并（将源文件夹内容合并到目标）")
            self.merge_btn.clicked.connect(lambda: self._choose("merge"))
            layout.addWidget(self.merge_btn)

            self.skip_btn = QPushButton("跳过")
            self.skip_btn.clicked.connect(lambda: self._choose("skip"))
            layout.addWidget(self.skip_btn)

            self.rename_btn = QPushButton("重命名（添加编号后缀）")
            self.rename_btn.clicked.connect(lambda: self._choose("rename"))
            layout.addWidget(self.rename_btn)
        else:
            # 文件冲突：overwrite / skip / rename
            self.overwrite_btn = QPushButton("覆盖")
            self.overwrite_btn.clicked.connect(lambda: self._choose("overwrite"))
            layout.addWidget(self.overwrite_btn)

            self.skip_btn = QPushButton("跳过")
            self.skip_btn.clicked.connect(lambda: self._choose("skip"))
            layout.addWidget(self.skip_btn)

            self.rename_btn = QPushButton("重命名（添加编号）")
            self.rename_btn.clicked.connect(lambda: self._choose("rename"))
            layout.addWidget(self.rename_btn)

        # 应用到所有
        self.apply_all_check = QCheckBox("应用到所有后续冲突")
        layout.addWidget(self.apply_all_check)

    def _choose(self, resolution: str):
        self.resolution = resolution
        self.apply_to_all = self.apply_all_check.isChecked()
        self.accept()


def main():
    """程序入口"""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        window = FileClassifierGUI()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        traceback.print_exc()
        input(f"\n错误: {e}\n按回车退出...")
        sys.exit(1)


if __name__ == "__main__":
    main()
