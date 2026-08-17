"""DailySummary 面板 — 活动日总结查看/编辑/审核

左右分栏：
- 左：日期列表（GET /activity/daily），已审打 ✓
- 右：MarkdownViewer 渲染选中日期的 markdown（GET /activity/daily/{name}）

操作：编辑/保存/取消、标记已审（移到 reviewed/）、删除（走回收站）、刷新
"""


from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.format_utils import format_bytes
from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from client.widgets.confirm_dialog import ConfirmDialog
from client.widgets.markdown_viewer import MarkdownViewer
from lib.ui.theme import set_text_role


class DailySummaryPanel(PanelBase):
    """日总结面板：查看/编辑/审核 daily 活动报告"""

    PANEL_META = PanelMeta(
        id="daily_summary",
        title="日总结",
        icon="calendar",
        order=50,
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._files: list[dict] = []
        self._current_name: str | None = None
        self._current_reviewed: bool = False

        self._build_ui()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # QStackedWidget 切换正常视图 / 离线占位
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._normal_widget = QWidget()
        self._stack.addWidget(self._normal_widget)
        self._offline_widget = self._build_offline_widget()
        self._stack.addWidget(self._offline_widget)

        self._build_normal_ui(self._normal_widget)

    def _build_offline_widget(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("后端离线\n\n请先启动后端服务（start.bat）")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(label, "secondary")
        layout.addWidget(label)
        return w

    def _build_normal_ui(self, root: QWidget) -> None:
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶栏
        header_row = QHBoxLayout()
        title = QLabel("日总结")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 主区域：左右分栏
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：文件列表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(6)

        list_header = QLabel("日期列表")
        set_text_role(list_header, "secondary")
        left_layout.addWidget(list_header)

        self._file_list = QListWidget()
        self._file_list.currentItemChanged.connect(self._on_file_selected)
        left_layout.addWidget(self._file_list, 1)

        self._count_label = QLabel("")
        set_text_role(self._count_label, "caption")
        left_layout.addWidget(self._count_label)

        splitter.addWidget(left_widget)

        # 右侧：MarkdownViewer + 操作按钮
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(6)

        # 右侧顶栏：文件名 + 状态 + 操作按钮
        self._detail_header = QHBoxLayout()
        self._detail_title = QLabel("选择左侧日期查看内容")
        set_text_role(self._detail_title, "secondary")
        self._detail_header.addWidget(self._detail_title, 1)
        right_layout.addLayout(self._detail_header)

        # 操作按钮行
        ops_row = QHBoxLayout()
        ops_row.setSpacing(6)

        self._btn_edit = QPushButton("编辑")
        self._btn_edit.setEnabled(False)
        self._btn_edit.clicked.connect(self._on_edit_clicked)
        ops_row.addWidget(self._btn_edit)

        self._btn_save = QPushButton("保存")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._on_save_clicked)
        ops_row.addWidget(self._btn_save)

        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        ops_row.addWidget(self._btn_cancel)

        self._btn_review = QPushButton("标记已审")
        self._btn_review.setEnabled(False)
        self._btn_review.clicked.connect(self._on_review_clicked)
        ops_row.addWidget(self._btn_review)

        self._btn_delete = QPushButton("删除")
        self._btn_delete.setEnabled(False)
        self._btn_delete.clicked.connect(self._on_delete_clicked)
        ops_row.addWidget(self._btn_delete)

        ops_row.addStretch()
        right_layout.addLayout(ops_row)

        # MarkdownViewer
        self._viewer = MarkdownViewer()
        right_layout.addWidget(self._viewer, 1)

        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([200, 600])
        layout.addWidget(splitter, 1)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._on_refresh_clicked()

    def on_hide(self) -> None:
        # 如果正在编辑，放弃编辑（不保存）
        if self._viewer.is_editing():
            self._viewer.discard_edit()
            self._update_button_states()

    def on_refresh(self) -> None:
        self._on_refresh_clicked()

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
        else:
            self._stack.setCurrentWidget(self._offline_widget)
            self._files = []
            self._file_list.clear()
            self._current_name = None
            self._viewer.set_markdown("")
            self._update_button_states()

    @Slot(dict)
    def on_health_changed(self, health: dict) -> None:
        # 后端在线时切换到正常视图
        if health and health.get("status") == "ok":
            self._stack.setCurrentWidget(self._normal_widget)

    # —— 数据加载 ——

    def _on_refresh_clicked(self) -> None:
        worker = self._make_worker("get", "/activity/daily")
        worker.done.connect(self._on_refresh_done)
        worker.failed.connect(lambda _err: self._on_refresh_done(None))
        worker.start()

    def _on_refresh_done(self, resp: dict | None) -> None:
        if resp is None or not isinstance(resp, dict):
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)
        self._files = resp.get("files", []) or []
        self._fill_file_list()
        self._count_label.setText(f"共 {len(self._files)} 个文件")

    def _fill_file_list(self) -> None:
        self._file_list.clear()
        prev_selected = self._current_name
        selected_row = -1

        for i, f in enumerate(self._files):
            name = f.get("name", "")
            reviewed = f.get("reviewed", False)
            size = f.get("size", 0)
            mtime = f.get("mtime", "")

            # 显示文本：已审/未审 + 文件名 + 大小
            mark = "已审 " if reviewed else "未审 "
            size_str = format_bytes(size)
            display = f"{mark}{name}  ({size_str})"

            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(f"{name}\n大小: {size_str}\n修改: {mtime}\n已审: {'是' if reviewed else '否'}")

            if reviewed:
                item.setForeground(Qt.GlobalColor.gray)
            else:
                item.setForeground(Qt.GlobalColor.white)

            self._file_list.addItem(item)

            if name == prev_selected:
                selected_row = i

        if selected_row >= 0:
            self._file_list.setCurrentRow(selected_row)
        elif self._file_list.count() > 0:
            self._file_list.setCurrentRow(0)

    # —— 文件选择 ——

    def _on_file_selected(self, current: QListWidgetItem, previous: QListWidgetItem) -> None:
        if self._viewer.is_editing():
            # 正在编辑时切换，提示保存
            reply = QMessageBox.question(
                self, "未保存的编辑",
                "当前有未保存的编辑，是否放弃？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # 恢复选择到之前的项
                if previous is not None:
                    self._file_list.blockSignals(True)
                    self._file_list.setCurrentItem(previous)
                    self._file_list.blockSignals(False)
                return
            self._viewer.discard_edit()
            self._update_button_states()

        if current is None:
            self._current_name = None
            self._detail_title.setText("选择左侧日期查看内容")
            self._viewer.set_markdown("")
            self._update_button_states()
            return

        name = current.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        self._load_file(name)

    def _load_file(self, name: str) -> None:
        worker = self._make_worker("get", f"/activity/daily/{name}")
        worker.done.connect(lambda resp: self._on_load_file_done(name, resp))
        worker.failed.connect(lambda _err: self._on_load_file_done(name, None))
        worker.start()

    def _on_load_file_done(self, name: str, resp: dict | None) -> None:
        if resp is None or resp.get("error"):
            self._detail_title.setText(f"加载失败: {name}")
            self._viewer.set_markdown("")
            self._current_name = None
            self._update_button_states()
            return

        self._current_name = name
        self._current_reviewed = resp.get("reviewed", False)
        content = resp.get("content", "")
        self._viewer.set_markdown(content)

        status_text = "已审" if self._current_reviewed else "未审"
        self._detail_title.setText(f"{name}  [{status_text}]")
        self._update_button_states()

    # —— 操作回调 ——

    def _on_edit_clicked(self) -> None:
        if not self._current_name:
            return
        self._viewer.set_readonly(False)
        self._update_button_states()

    def _on_save_clicked(self) -> None:
        if not self._current_name:
            return
        content = self._viewer.get_markdown()
        worker = self._make_worker(
            "put",
            f"/activity/daily/{self._current_name}",
            json={"content": content},
        )
        worker.done.connect(self._on_save_done)
        worker.failed.connect(lambda _err: self._on_save_done(None))
        worker.start()

    def _on_save_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "保存失败", "保存失败（后端返回错误）")
            return
        # 切换回只读模式（会捕获编辑结果并重新渲染）
        self._viewer.set_readonly(True)
        self._update_button_states()
        # 刷新文件列表（大小/mtime 可能变了）
        self._on_refresh_clicked()

    def _on_cancel_clicked(self) -> None:
        self._viewer.discard_edit()
        self._update_button_states()

    def _on_review_clicked(self) -> None:
        if not self._current_name:
            return
        if self._current_reviewed:
            QMessageBox.information(self, "已审核", f"{self._current_name} 已经在 reviewed/ 目录中")
            return

        if not ConfirmDialog.confirm(
            self, "标记已审",
            f"将 {self._current_name} 移到 reviewed/ 子目录？\n\n"
            "已审文件永久保留，不被自动清理。",
            risk_level="info",
        ):
            return

        worker = self._make_worker("post", f"/activity/daily/{self._current_name}/review")
        worker.done.connect(self._on_review_done)
        worker.failed.connect(lambda _err: self._on_review_done(None))
        worker.start()

    def _on_review_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "标记已审失败（后端返回错误）")
            return
        # 刷新列表和内容
        self._on_refresh_clicked()

    def _on_delete_clicked(self) -> None:
        if not self._current_name:
            return

        # 编辑中不允许删除（避免删除正在编辑的文件）
        if self._viewer.is_editing():
            QMessageBox.information(
                self, "无法删除",
                "正在编辑中，请先保存或取消编辑后再删除。",
            )
            return

        name = self._current_name
        location = "reviewed/" if self._current_reviewed else "daily/"

        if not ConfirmDialog.confirm(
            self, "删除日报",
            f"确定删除 {name}？\n\n"
            f"位置: {location}\n"
            f"操作: 移到 Windows 回收站（可通过资源管理器恢复）",
            risk_level="danger",
        ):
            return

        worker = self._make_worker("delete", f"/activity/daily/{name}")
        worker.done.connect(lambda resp: self._on_delete_done(name, resp))
        worker.failed.connect(lambda _err: self._on_delete_done(name, None))
        worker.start()

    def _on_delete_done(self, name: str, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "删除失败（后端返回错误）")
            return

        # 清空当前选中状态并刷新列表
        self._current_name = None
        self._current_reviewed = False
        self._viewer.set_markdown("")
        self._detail_title.setText("选择左侧日期查看内容")
        self._on_refresh_clicked()

        # 提示用户删除结果
        QMessageBox.information(
            self, "已删除",
            f"{name} 已移到回收站。\n可通过资源管理器恢复。",
        )

    # —— 按钮状态管理 ——

    def _update_button_states(self) -> None:
        has_file = self._current_name is not None
        is_editing = self._viewer.is_editing()

        self._btn_edit.setEnabled(has_file and not is_editing)
        self._btn_save.setEnabled(has_file and is_editing)
        self._btn_cancel.setEnabled(has_file and is_editing)
        self._btn_review.setEnabled(has_file and not is_editing and not self._current_reviewed)
        # 删除：有文件且未在编辑时启用（已审文件也允许删除）
        self._btn_delete.setEnabled(has_file and not is_editing)
