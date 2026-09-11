"""文件分类工具的分类列表组件

从 classifier_gui.py 拆出。
包含：CategoryListWidget（支持作为拖放目标，接收 FileTableWidget 拖来的文件名）
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QMenu, QTableWidget


class CategoryListWidget(QListWidget):
    """分类列表组件，支持作为拖放目标 + 右键重命名分类（ticket C）"""

    files_dropped = Signal(str, list)  # category_name, filenames
    rename_requested = Signal(str)     # old_category_name（ticket C：右键重命名）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        # ticket C：右键菜单（重命名分类）
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        """右键菜单：对命中的分类项提供「重命名」"""
        item = self.itemAt(pos)
        if not item:
            return
        category_name = item.data(Qt.UserRole)
        if not category_name:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("重命名")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self.rename_requested.emit(category_name)

    def dragEnterEvent(self, event):
        if event.source():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.source():
            event.acceptProposedAction()

    def dropEvent(self, event):
        source = event.source()
        # 接受 FileTableWidget（及其子类）作为拖拽源
        if not source or not isinstance(source, QTableWidget):
            return
        # 获取目标分类名
        item = self.itemAt(event.position().toPoint())
        if not item:
            return
        category_name = item.data(Qt.UserRole)
        if not category_name:
            return
        # 获取被拖拽的文件名：优先用 FileTableWidget.get_drag_filenames()
        # （携带复选框勾选的行；无勾选则临时只拖当前行），避免多选连累
        if hasattr(source, "get_drag_filenames"):
            filenames = source.get_drag_filenames()
        else:
            # 兼容回退：selectionModel 高亮行
            filenames = []
            for row in source.selectionModel().selectedRows():
                filename = source.item(row.row(), 1).data(Qt.UserRole)
                if filename:
                    filenames.append(filename)
        if filenames:
            self.files_dropped.emit(category_name, filenames)
        event.acceptProposedAction()
