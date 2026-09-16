"""Terminal 面板 — 终端会话管理

从 Monitoring 面板拆出，独立为监控分类下的二级入口。

功能：
- 终端会话列表（TID/标签/状态/PID/耗时/输出字节数）
- 选中终端查看 stdout/stderr 详情
- 新建终端会话（输入命令 + 标签）
- 自动刷新（15s）

数据源：GET /terminals + GET /terminals/{tid}。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.core.format_utils import format_bytes, format_elapsed
from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


class TerminalPanel(PanelBase):
    """终端会话管理面板"""

    PANEL_META = PanelMeta(
        id="terminal",
        title="终端",
        icon="monitor",
        order=41,
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._terminals: list[dict] = []
        self._selected_tid: str | None = None
        self._owner_tokens: dict[str, str] = {}  # tid -> owner_token（GUI 自建终端免审）
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None

        # 终端列表自动刷新（15s）
        self._terminals_timer = QTimer(self)
        self._terminals_timer.setInterval(15000)
        self._terminals_timer.timeout.connect(self._refresh_terminals)

        self._build_ui()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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
        title = QLabel("终端会话")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)

        self._btn_term_stop = QPushButton("停止")
        self._btn_term_stop.setEnabled(False)
        self._btn_term_stop.clicked.connect(self._stop_terminal)
        header_row.addWidget(self._btn_term_stop)

        self._btn_term_delete = QPushButton("删除")
        self._btn_term_delete.setEnabled(False)
        self._btn_term_delete.clicked.connect(self._delete_terminal)
        set_kind(self._btn_term_delete, "danger")
        header_row.addWidget(self._btn_term_delete)

        btn_term_new = QPushButton("新终端")
        btn_term_new.clicked.connect(self._new_terminal)
        header_row.addWidget(btn_term_new)

        layout.addLayout(header_row)

        # 统计行
        self._term_count_label = QLabel("")
        set_text_role(self._term_count_label, "secondary")
        layout.addWidget(self._term_count_label)

        # 终端表格 + 详情（垂直分割）
        splitter = QSplitter(Qt.Orientation.Vertical)

        self._term_table = QTableWidget(0, 6)
        self._term_table.setHorizontalHeaderLabels(
            ["TID", "标签", "状态", "PID", "耗时", "输出"]
        )
        self._term_table.verticalHeader().setVisible(False)
        self._term_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._term_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._term_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._term_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # TID
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)          # 标签
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # 状态
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)  # PID
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # 耗时
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # 输出
        self._term_table.itemSelectionChanged.connect(self._on_terminal_selected)
        splitter.addWidget(self._term_table)

        # 详情区
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        self._term_detail_title = QLabel("选择终端查看详情")
        set_text_role(self._term_detail_title, "secondary")
        detail_layout.addWidget(self._term_detail_title)

        self._term_detail = QPlainTextEdit()
        self._term_detail.setReadOnly(True)
        self._term_detail.setStyleSheet(f"font-family: {tokens.FONT_MONO};")
        self._term_detail.setMinimumHeight(120)
        detail_layout.addWidget(self._term_detail, 1)
        splitter.addWidget(detail_widget)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._terminals_timer.start()
        if self._terminals:
            self._fill_terminals()
        self._refresh_terminals()

    def on_hide(self) -> None:
        self._terminals_timer.stop()

    def on_refresh(self) -> None:
        self._refresh_terminals()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            self._refresh_terminals()
        else:
            self._stack.setCurrentWidget(self._offline_widget)
            self._terminals = []
            self._fill_terminals()

    # —— 数据刷新 ——

    def _on_refresh_clicked(self) -> None:
        self._refresh_terminals()

    def _refresh_terminals(self) -> None:
        # 8-7: HTTP 移到 worker 线程——原实现在主线程同步 GET /terminals（8s 超时），
        # 由 15s QTimer + on_show + 后端恢复三处触发，后端 hang 时每 15s 冻结 UI 至多 8s。
        # 同 due_todos._refresh_async 的 isRunning 模式：在飞则跳过本轮
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class TerminalsRefreshThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.data = None

            def run(self):
                self.data = self._http.get("/terminals")

        self._refresh_thread = TerminalsRefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_terminals_done)
        self._refresh_thread.start()

    def _on_refresh_terminals_done(self) -> None:
        t = self._refresh_thread
        if t is None or t.data is None or not isinstance(t.data, dict):
            self._term_count_label.setText("（无法获取终端列表）")
            return
        self._terminals = t.data.get("terminals", []) or []
        running = t.data.get("running", 0)
        total = t.data.get("count", len(self._terminals))
        self._term_count_label.setText(f"共 {total} 个 / {running} 运行中")
        self._fill_terminals()

    def _fill_terminals(self) -> None:
        self._term_table.setRowCount(len(self._terminals))
        for row, t in enumerate(self._terminals):
            tid = t.get("tid", "")
            label = t.get("label", "")
            status = t.get("status", "")
            pid = t.get("pid", "")
            elapsed = format_elapsed(t.get("elapsed", 0))
            out_bytes = t.get("stdout_bytes", 0) + t.get("stderr_bytes", 0)
            output_text = format_bytes(out_bytes)

            tid_item = QTableWidgetItem(tid)
            tid_item.setToolTip(tid)
            self._term_table.setItem(row, 0, tid_item)

            label_item = QTableWidgetItem(label)
            label_item.setToolTip(t.get("cmd", ""))
            self._term_table.setItem(row, 1, label_item)

            status_item = QTableWidgetItem(status)
            if status == "running":
                status_item.setForeground(Qt.GlobalColor.green)
            elif status in ("done", "killed", "timeout", "error"):
                status_item.setForeground(
                    Qt.GlobalColor.red if status in ("killed", "error", "timeout")
                    else Qt.GlobalColor.gray
                )
            self._term_table.setItem(row, 2, status_item)

            self._term_table.setItem(row, 3, QTableWidgetItem(str(pid)))
            self._term_table.setItem(row, 4, QTableWidgetItem(elapsed))
            self._term_table.setItem(row, 5, QTableWidgetItem(output_text))

            if tid == self._selected_tid:
                self._term_table.selectRow(row)

        if not self._selected_tid or not any(
            t.get("tid") == self._selected_tid for t in self._terminals
        ):
            self._term_detail_title.setText("选择终端查看详情")
            self._term_detail.clear()
            self._selected_tid = None

    def _on_terminal_selected(self) -> None:
        rows = self._term_table.selectionModel().selectedRows()
        if not rows:
            self._btn_term_stop.setEnabled(False)
            self._btn_term_delete.setEnabled(False)
            return
        row = rows[0].row()
        tid_item = self._term_table.item(row, 0)
        if not tid_item:
            return
        tid = tid_item.text()
        self._selected_tid = tid
        self._load_terminal_detail(tid)

        # 更新停止/删除按钮状态
        terminal = None
        for t in self._terminals:
            if t.get("tid") == tid:
                terminal = t
                break
        if terminal:
            is_running = terminal.get("status") == "running"
            self._btn_term_stop.setEnabled(is_running)
            self._btn_term_delete.setEnabled(True)  # 任何状态都可删除

    def _stop_terminal(self) -> None:
        if not self._selected_tid:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认停止终端 {self._selected_tid}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 带 owner_token（GUI 自建终端免审）
        token = self._owner_tokens.get(self._selected_tid, "")
        resp = self._http.post(
            f"/terminals/{self._selected_tid}/kill",
            json={"owner_token": token},
        )
        if resp is None or not resp.get("success"):
            err = resp.get("error", "未知错误") if resp else "后端无响应"
            QMessageBox.warning(self, "失败", f"停止终端失败：{err}")
            return
        self._refresh_terminals()

    def _delete_terminal(self) -> None:
        if not self._selected_tid:
            return
        ret = QMessageBox.question(
            self, "确认", f"确认删除终端 {self._selected_tid} 的记录？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 带 owner_token（GUI 自建终端免审）
        token = self._owner_tokens.get(self._selected_tid, "")
        resp = self._http.delete(
            f"/terminals/{self._selected_tid}",
            json={"owner_token": token},
        )
        if resp is None or not resp.get("success"):
            err = resp.get("error", "未知错误") if resp else "后端无响应"
            QMessageBox.warning(self, "失败", f"删除终端失败：{err}")
            return
        # 清理本地 owner_token
        self._owner_tokens.pop(self._selected_tid, None)
        self._selected_tid = None
        self._refresh_terminals()

    def _load_terminal_detail(self, tid: str) -> None:
        detail = self._http.get(f"/terminals/{tid}", params={"tail": 4000})
        if detail is None or detail.get("error"):
            self._term_detail_title.setText(f"终端 {tid} — 详情获取失败")
            self._term_detail.clear()
            return
        cmd = detail.get("cmd", "")
        status = detail.get("status", "")
        elapsed = format_elapsed(detail.get("elapsed", 0))
        exit_code = detail.get("exit_code")
        self._term_detail_title.setText(
            f"终端 {tid} — {status} / {elapsed}"
            + (f" / exit={exit_code}" if exit_code is not None else "")
        )
        stdout = detail.get("stdout", "") or ""
        stderr = detail.get("stderr", "") or ""
        text = f"$ {cmd}\n\n--- stdout ---\n{stdout}"
        if stderr:
            text += f"\n\n--- stderr ---\n{stderr}"
        self._term_detail.setPlainText(text)

    def _new_terminal(self) -> None:
        cmd, ok = QInputDialog.getText(
            self, "新终端会话", "输入命令：",
            text="echo hello",
        )
        if not ok or not cmd.strip():
            return
        resp = self._http.post("/terminals/spawn", json={"cmd": cmd.strip(), "label": cmd.strip()[:60]})
        if resp is None:
            QMessageBox.warning(self, "失败", "启动终端失败（后端返回错误或被 dcg 拦截）")
            return
        if resp.get("blocked"):
            QMessageBox.warning(
                self, "被拦截",
                f"命令被 dcg 拦截：\n{resp.get('guard_reason', '未知原因')}\n\n"
                f"approval_id: {resp.get('approval_id', '')}",
            )
            return
        if not resp.get("success"):
            QMessageBox.warning(self, "失败", f"启动终端失败：{resp.get('error', '未知错误')}")
            return
        tid = resp.get("tid", "")
        # 保存 owner_token，后续 kill/delete/input 凭此 token 免审
        token = resp.get("owner_token", "")
        if tid and token:
            self._owner_tokens[tid] = token
        self._selected_tid = tid
        self._refresh_terminals()
