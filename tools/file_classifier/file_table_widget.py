"""file_table_widget.py — 自定义 QTableWidget 子类，重写交互模型

支持两种选择模式（由顶部"多选模式"复选框切换，默认普通模式）：

【普通模式（默认，Windows 资源管理器行为）】
- 单击释放 = 取消其他勾选 + 勾选当前行；锚点更新为当前行
- shift+单击 = 取消其他勾选 + 勾选锚点行到当前行的范围
- ctrl+单击 = 翻转当前行复选框（不影响其他行）
- 双击 / Enter = 打开对象（文件夹→进入列表，文件→os.startfile 默认打开）
- Backspace = 返回上一级目录（navigate_up_requested 信号）
- Space = 翻转当前焦点行的复选框
- ctrl+Enter = 弹出详情弹窗（details_requested 信号）

【多选模式（开启时）】
- 单击释放 = 翻转该行复选框（不取消其他）；锚点更新为当前行
- shift+单击 = 从锚点行到当前行逐行取反（每行复选框状态翻转）
- ctrl+单击 = 打开对象（与双击/Enter 相同语义）
- 双击 / Enter / Backspace / Space / ctrl+Enter 行为同普通模式

通用规则：
- toggle 在 mouseReleaseEvent 弹起时触发，不是 mousePressEvent 按下时
  （拖拽时 press → move → startDrag → release，release 检测到 _drag_started 跳过 toggle）
- 表格 selectionModel 仅用于焦点/视觉高亮，不用于"选中"
- 拖拽携带范围 = 复选框勾选的行；点击行未勾选则临时只拖该行
- 拖拽视觉 = 小虚线框 pixmap（不显示整行内容，避免大行遮挡分类列表）

拆分原因：classifier_gui.py 已约 1500 行，将交互逻辑抽到独立模块避免文件膨胀。
"""

from PySide6.QtCore import Qt, Signal, QPoint, QMimeData
from PySide6.QtGui import (
    QKeyEvent, QDrag, QPixmap, QPainter, QPen, QColor,
)
from PySide6.QtWidgets import QTableWidget, QAbstractItemView


class FileTableWidget(QTableWidget):
    """文件表格，重写交互模型实现反选语义 + 双击/Enter打开 + 拖拽虚线框

    Signals:
        open_requested: 双击/Enter/ctrl+单击时发出，参数为行号。主窗口连接此信号
                        决定打开文件（os.startfile）或进入文件夹。
        details_requested: ctrl+enter 按下时发出，参数为行号。主窗口
                           连接此信号弹出 DetailsDialog 显示详情。
        navigate_up_requested: Backspace 按下时发出（无参数）。主窗口
                               连接此信号执行返回上一级目录。
    """

    open_requested = Signal(int)        # row 索引
    details_requested = Signal(int)     # row 索引
    navigate_up_requested = Signal()    # 无参数

    # 拖拽虚线框 pixmap 的尺寸和颜色
    _DRAG_PIXMAP_SIZE = 28
    _DRAG_PIXMAP_COLOR = "#5cb88a"

    def __init__(self, check_col: int = 0, name_col: int = 1, parent=None):
        """
        Args:
            check_col: 复选框所在列号
            name_col: 文件名所在列号（UserRole 存文件名）
        """
        super().__init__(parent)
        self._check_col = check_col
        self._name_col = name_col
        self._anchor_row = -1  # shift 批量操作的锚点行

        # 拖拽/点击状态跟踪
        self._drag_started = False       # startDrag 被调用时置 True
        self._press_pos = QPoint()       # 鼠标按下位置
        self._press_modifiers = Qt.NoModifier  # 鼠标按下时的修饰键
        self._press_row = -1             # 鼠标按下的行号（ticket B1：拖拽单击语义用）
        self._double_click_active = False  # 双击时阻止第二次 release 的 toggle

        # 选择模式：False=普通模式（Windows 资源管理器行为，默认），
        # True=多选模式（单击翻转不取消其他，ctrl+单击打开对象）
        self._multi_select_mode = False

        # selectionModel 仅用于焦点/视觉高亮，不用于"选中"
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)

    def set_multi_select_mode(self, enabled: bool) -> None:
        """切换选择模式：True=多选模式，False=普通模式"""
        self._multi_select_mode = enabled

    def is_multi_select_mode(self) -> bool:
        """返回当前是否为多选模式"""
        return self._multi_select_mode

    # ── 鼠标事件 ──

    def mousePressEvent(self, event):
        """鼠标按下：记录位置和修饰键，不 toggle（toggle 在 release 时触发）

        调用 super().mousePressEvent 让 Qt 处理 selectionModel 焦点更新
        和内部拖拽检测（move 超过阈值时自动调 startDrag）。
        """
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return super().mousePressEvent(event)

        # 记录按下状态（供 mouseReleaseEvent / startDrag 使用）
        self._drag_started = False
        self._press_pos = event.position().toPoint()
        self._press_modifiers = event.modifiers()
        self._press_row = index.row()

        # 不在此处 toggle — 等 release 时再决定
        return super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """鼠标释放：根据选择模式执行不同操作

        关键：startDrag 被调用时 _drag_started=True，此时 release 跳过 toggle，
        避免拖拽操作误取消选中。

        多选模式：
        - ctrl+单击 → 请求打开对象
        - shift+单击 → 从锚点到当前行逐行取反
        - 单击释放 → 翻转当前行复选框 + 更新锚点

        普通模式（Windows 资源管理器行为）：
        - ctrl+单击 → 翻转当前行（不影响其他）+ 更新锚点
        - shift+单击 → 清除其他勾选 + 勾选锚点到当前行
        - 单击释放 → 清除其他勾选 + 勾选当前行 + 更新锚点
        """
        if event.button() != Qt.LeftButton:
            return super().mouseReleaseEvent(event)

        # 双击的第二次 release — 不 toggle（第一次 release 已 toggle，
        # 双击事件已触发 open，第二次 release 需跳过避免在重绘后的表格上误 toggle）
        if self._double_click_active:
            self._double_click_active = False
            return super().mouseReleaseEvent(event)

        # 拖拽刚结束 — 不 toggle
        if self._drag_started:
            self._drag_started = False
            return super().mouseReleaseEvent(event)

        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return super().mouseReleaseEvent(event)

        row = index.row()
        modifiers = self._press_modifiers

        if self._multi_select_mode:
            # ── 多选模式 ──
            # ctrl+单击 → 请求打开对象
            if modifiers & Qt.ControlModifier:
                self.open_requested.emit(row)
                return

            # shift+单击 → 从锚点到当前行逐行取反
            if modifiers & Qt.ShiftModifier and self._anchor_row >= 0:
                start = min(self._anchor_row, row)
                end = max(self._anchor_row, row)
                for r in range(start, end + 1):
                    self._toggle_row_check(r)
                return super().mouseReleaseEvent(event)

            # 单击释放 → 翻转当前行复选框 + 更新锚点
            self._toggle_row_check(row)
            self._anchor_row = row
            return super().mouseReleaseEvent(event)

        # ── 普通模式（Windows 资源管理器行为） ──
        # ctrl+单击 → 翻转当前行（不影响其他）+ 更新锚点
        if modifiers & Qt.ControlModifier:
            self._toggle_row_check(row)
            self._anchor_row = row
            return

        # shift+单击 → 清除其他勾选 + 勾选锚点到当前行
        if modifiers & Qt.ShiftModifier and self._anchor_row >= 0:
            self._clear_all_checks()
            start = min(self._anchor_row, row)
            end = max(self._anchor_row, row)
            for r in range(start, end + 1):
                self._set_row_check(r, True)
            return super().mouseReleaseEvent(event)

        # 单击释放 → 清除其他勾选 + 勾选当前行 + 更新锚点
        self._clear_all_checks()
        self._set_row_check(row, True)
        self._anchor_row = row
        return super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击：请求打开对象（文件夹→进入，文件→默认程序打开）

        设置 _double_click_active 标志，阻止紧随其后的第二次 mouseReleaseEvent
        触发 toggle（避免在导航重绘后的表格上误 toggle 不相关行）。
        """
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                self._double_click_active = True
                self.open_requested.emit(index.row())
                return
        super().mouseDoubleClickEvent(event)

    # ── 拖拽 ──

    def startDrag(self, supportedActions):
        """重写拖拽启动：设置 _drag_started 标志 + 自定义小虚线框 pixmap

        默认 Qt 行为会创建整行内容的半透明 pixmap 跟随光标，
        行太大时遮挡分类列表。此处替换为 28x28 的小虚线框。
        """
        self._drag_started = True

        # ticket B1：拖拽 = 对被拖项触发一次"单击"语义（已勾选的不管）
        # - 多选模式：只勾上被拖项，不动其它勾选
        # - 普通模式：清掉其它勾选，再勾被拖项
        row = self._press_row if self._press_row >= 0 else self.currentRow()
        if row >= 0:
            if self._multi_select_mode:
                self._set_row_check(row, True)
            else:
                self._clear_all_checks()
                self._set_row_check(row, True)

        drag = QDrag(self)
        mime_data = QMimeData()
        filenames = self.get_drag_filenames()
        # 用 text 存储，同时用自定义 MIME type 以备扩展
        mime_data.setText("\n".join(filenames))
        mime_data.setData("application/x-file-classifier", "\n".join(filenames).encode("utf-8"))
        drag.setMimeData(mime_data)

        # 创建小虚线框 pixmap
        size = self._DRAG_PIXMAP_SIZE
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self._DRAG_PIXMAP_COLOR))
        pen.setStyle(Qt.DashLine)
        pen.setWidth(2)
        painter.setPen(pen)
        margin = 3
        painter.drawRoundedRect(margin, margin, size - 2 * margin, size - 2 * margin, 4, 4)
        painter.end()

        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(size // 2, size // 2))
        drag.exec(Qt.CopyAction)

    # ── 键盘事件 ──

    def keyPressEvent(self, event: QKeyEvent):
        """重写键盘事件

        - ctrl+enter → 弹出详情弹窗
        - Enter/Return → 打开对象（双击的键盘等价）
        - Backspace → 返回上一级目录
        - Space → 翻转当前行复选框
        """
        # ctrl+enter（Key_Return 或 Key_Enter）→ 请求详情
        if event.modifiers() & Qt.ControlModifier and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            row = self.currentRow()
            if row >= 0:
                self.details_requested.emit(row)
            return

        # Enter/Return → 打开对象（文件夹进入/文件打开）
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            row = self.currentRow()
            if row >= 0:
                self.open_requested.emit(row)
            return

        # Backspace → 返回上一级目录
        if event.key() == Qt.Key_Backspace:
            self.navigate_up_requested.emit()
            return

        # Space → 翻转当前焦点行复选框
        if event.key() == Qt.Key_Space:
            row = self.currentRow()
            if row >= 0:
                self._toggle_row_check(row)
                self._anchor_row = row
            return

        super().keyPressEvent(event)

    # ── 复选框操作 ──

    def _toggle_row_check(self, row: int) -> None:
        """翻转指定行的复选框状态"""
        item = self.item(row, self._check_col)
        if item is None:
            return
        if item.checkState() == Qt.Checked:
            item.setCheckState(Qt.Unchecked)
        else:
            item.setCheckState(Qt.Checked)

    def _set_row_check(self, row: int, checked: bool) -> None:
        """设置指定行的复选框状态（普通模式下使用）"""
        item = self.item(row, self._check_col)
        if item is None:
            return
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _clear_all_checks(self) -> None:
        """清除所有行的复选框勾选（普通模式下使用）"""
        for row in range(self.rowCount()):
            item = self.item(row, self._check_col)
            if item is not None:
                item.setCheckState(Qt.Unchecked)

    def get_checked_rows(self) -> list:
        """返回所有复选框勾选的行索引"""
        checked = []
        for row in range(self.rowCount()):
            item = self.item(row, self._check_col)
            if item and item.checkState() == Qt.Checked:
                checked.append(row)
        return checked

    def get_checked_filenames(self) -> list:
        """返回所有复选框勾选行的文件名"""
        filenames = []
        for row in range(self.rowCount()):
            check_item = self.item(row, self._check_col)
            if check_item and check_item.checkState() == Qt.Checked:
                name_item = self.item(row, self._name_col)
                if name_item:
                    filename = name_item.data(Qt.UserRole)
                    if filename:
                        filenames.append(filename)
        return filenames

    def get_drag_filenames(self) -> list:
        """返回拖拽应携带的文件名列表

        策略（ticket 03 决策）：
        - 有勾选行 → 携带所有勾选行
        - 无勾选行 → 临时只拖当前行（currentRow）
        """
        checked = self.get_checked_filenames()
        if checked:
            return checked
        # 无勾选行：临时只拖当前焦点行
        current = self.currentRow()
        if current >= 0:
            name_item = self.item(current, self._name_col)
            if name_item:
                filename = name_item.data(Qt.UserRole)
                if filename:
                    return [filename]
        return []
