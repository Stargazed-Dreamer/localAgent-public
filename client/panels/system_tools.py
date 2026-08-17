"""SystemTools 面板 — 系统工具（高级分类）

从 web 状态监控页面迁出两个含输入的功能子项：

1. **用户补充指令**：向工作中的 agent 发送补充指令
   - API: GET/POST/DELETE /user/message, DELETE /user/message/{id}

2. **防休眠**：通过 SetThreadExecutionState 阻止系统/显示器休眠
   - API: GET /system/keep-awake, POST /system/keep-awake

设计参考 GUI Monitoring 面板的状态监控卡片，但因含输入控件
而独立为高级分类下的功能面板（非纯展示）。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.format_utils import format_uptime
from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


class _SectionCard(QFrame):
    """可折叠功能分区卡片（含输入控件版本）

    与 monitoring.py 的 _CollapsibleCard 不同：本组件用于功能区，标题栏点击折叠，
    内容区可放任意控件（输入框、按钮、列表等）。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("sysCard")
        self._expanded = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏（可点击折叠）
        self._header = QFrame()
        self._header.setObjectName("sysCardHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            f"QFrame#sysCardHeader {{ background: {tokens.BG_CARD}; border: 1px solid {tokens.BORDER};"
            f" border-bottom: none; border-radius: 0; padding: 4px; }}"
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        self._title_label = QLabel(title)
        set_text_role(self._title_label, "title")
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()
        self._arrow_label = QLabel("▼")
        set_text_role(self._arrow_label, "secondary")
        header_layout.addWidget(self._arrow_label)
        self._header.mousePressEvent = lambda _e: self.toggle()
        layout.addWidget(self._header)

        # 内容区容器
        self._body = QFrame()
        self._body.setObjectName("sysCardBody")
        self._body.setStyleSheet(
            f"QFrame#sysCardBody {{ background: {tokens.BG_CARD}; border: 1px solid {tokens.BORDER};"
            f" border-top: none; }}"
        )
        self.body_layout = QVBoxLayout(self._body)
        self.body_layout.setContentsMargins(14, 10, 14, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self._body)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow_label.setText("▼" if self._expanded else "▶")


class SystemToolsPanel(PanelBase):
    """系统工具面板：用户补充指令 + 防休眠"""

    PANEL_META = PanelMeta(
        id="system_tools",
        title="系统工具",
        icon="sliders",
        order=52,
        category="advanced",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None

        # 用户补充指令状态
        self._pending_messages: list[dict] = []

        # 防休眠状态
        self._keep_awake_supported: bool = False
        self._keep_awake_enabled: bool = False
        self._keep_awake_keep_display: bool = False
        self._keep_awake_reason: str = ""
        self._keep_awake_duration: float | None = None

        # 周期刷新定时器（10s）
        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._refresh_async)

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
        title = QLabel("系统工具")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(10)

        # 用户补充指令分区
        self._msg_card = _SectionCard("用户补充指令")
        self._build_message_section(self._msg_card.body_layout)
        container_layout.addWidget(self._msg_card)

        # 防休眠分区
        self._ka_card = _SectionCard("防休眠")
        self._build_keep_awake_section(self._ka_card.body_layout)
        container_layout.addWidget(self._ka_card)

        container_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _build_message_section(self, parent_layout: QVBoxLayout) -> None:
        # 状态行
        self._msg_count_label = QLabel("待发送：0 条")
        set_text_role(self._msg_count_label, "secondary")
        parent_layout.addWidget(self._msg_count_label)

        # 输入行
        input_row = QHBoxLayout()
        self._msg_input = QLineEdit()
        self._msg_input.setPlaceholderText("输入补充指令，回车或点发送")
        self._msg_input.returnPressed.connect(self._send_message)
        self._msg_input.setStyleSheet(
            f"QLineEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 6px; }}"
        )
        input_row.addWidget(self._msg_input, 1)

        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self._send_message)
        input_row.addWidget(send_btn)

        clear_all_btn = QPushButton("清空全部")
        clear_all_btn.clicked.connect(self._clear_all_messages)
        input_row.addWidget(clear_all_btn)
        parent_layout.addLayout(input_row)

        # 待发送列表（自定义行：文本 + 删除按钮）
        self._msg_list_container = QWidget()
        self._msg_list_layout = QVBoxLayout(self._msg_list_container)
        self._msg_list_layout.setContentsMargins(0, 0, 0, 0)
        self._msg_list_layout.setSpacing(4)

        self._msg_list_scroll = QScrollArea()
        self._msg_list_scroll.setWidgetResizable(True)
        self._msg_list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._msg_list_scroll.setWidget(self._msg_list_container)
        self._msg_list_scroll.setMinimumHeight(120)
        self._msg_list_scroll.setStyleSheet(
            f"QScrollArea {{ background: {tokens.BG_INPUT}; border: 1px solid {tokens.BORDER}; }}"
        )
        parent_layout.addWidget(self._msg_list_scroll, 1)

        # 提示
        hint = QLabel(
            "指令暂存在后端，agent 下次调用非工作端点时自动收到并清除。"
            "工作端点（/llm/pool/*、/mcp/*、/docs 等）不注入。"
        )
        set_text_role(hint, "tertiary")
        hint.setWordWrap(True)
        parent_layout.addWidget(hint)

    def _build_keep_awake_section(self, parent_layout: QVBoxLayout) -> None:
        # 平台支持状态
        self._ka_support_label = QLabel("平台支持：检测中...")
        set_text_role(self._ka_support_label, "secondary")
        parent_layout.addWidget(self._ka_support_label)

        # 防休眠开关
        enable_row = QHBoxLayout()
        enable_label = QLabel("防休眠开关")
        set_text_role(enable_label, "secondary")
        enable_row.addWidget(enable_label)
        enable_row.addStretch()
        self._ka_enable_btn = QPushButton("关闭")
        self._ka_enable_btn.setCheckable(True)
        self._ka_enable_btn.setMinimumWidth(80)
        self._ka_enable_btn.clicked.connect(self._on_enable_toggled)
        self._ka_enable_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY}; padding: 6px 12px;"
            f" border: 1px solid {tokens.BORDER}; }}"
            f"QPushButton:checked {{ background: {tokens.SUCCESS}; color: {tokens.TEXT_ON_ACCENT}; }}"
            f"QPushButton:disabled {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_TERTIARY}; }}"
        )
        enable_row.addWidget(self._ka_enable_btn)
        parent_layout.addLayout(enable_row)

        # 阻止显示器休眠开关
        display_row = QHBoxLayout()
        display_label = QLabel("阻止显示器休眠")
        set_text_role(display_label, "secondary")
        display_row.addWidget(display_label)
        display_row.addStretch()
        self._ka_display_btn = QPushButton("关闭")
        self._ka_display_btn.setCheckable(True)
        self._ka_display_btn.setMinimumWidth(80)
        self._ka_display_btn.clicked.connect(self._on_display_toggled)
        self._ka_display_btn.setStyleSheet(
            f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY}; padding: 6px 12px;"
            f" border: 1px solid {tokens.BORDER}; }}"
            f"QPushButton:checked {{ background: {tokens.SUCCESS}; color: {tokens.TEXT_ON_ACCENT}; }}"
            f"QPushButton:disabled {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_TERTIARY}; }}"
        )
        display_row.addWidget(self._ka_display_btn)
        parent_layout.addLayout(display_row)

        # 启用原因
        reason_row = QHBoxLayout()
        reason_label = QLabel("启用原因")
        set_text_role(reason_label, "secondary")
        reason_row.addWidget(reason_label)
        self._ka_reason_input = QLineEdit()
        self._ka_reason_input.setPlaceholderText("如：爬虫运行中（可选）")
        self._ka_reason_input.setStyleSheet(
            f"QLineEdit {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER}; padding: 6px; }}"
        )
        reason_row.addWidget(self._ka_reason_input, 1)
        parent_layout.addLayout(reason_row)

        # 已启用时长
        self._ka_duration_label = QLabel("已启用时长：—")
        set_text_role(self._ka_duration_label, "secondary")
        parent_layout.addWidget(self._ka_duration_label)

        # 错误信息
        self._ka_error_label = QLabel("")
        set_text_role(self._ka_error_label, "danger")
        self._ka_error_label.setWordWrap(True)
        self._ka_error_label.setVisible(False)
        parent_layout.addWidget(self._ka_error_label)

        # 提示
        hint = QLabel(
            "enable=true 阻止系统休眠；keep_display_on=true 同时阻止显示器休眠（耗电）。"
            "进程退出时自动恢复系统默认休眠策略。"
        )
        set_text_role(hint, "tertiary")
        hint.setWordWrap(True)
        parent_layout.addWidget(hint)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._timer.start()
        self._refresh_async()

    def on_hide(self) -> None:
        self._timer.stop()

    def on_refresh(self) -> None:
        self._refresh_async()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            self._refresh_async()
        else:
            self._stack.setCurrentWidget(self._offline_widget)
            self._timer.stop()

    # —— 数据刷新 ——

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _refresh_async(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class RefreshThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.messages = None
                self.keep_awake = None

            def run(self):
                self.messages = self._http.get("/user/message")
                self.keep_awake = self._http.get("/system/keep-awake")

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        if t is None:
            return
        if t.messages is None and t.keep_awake is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)

        # 用户补充指令
        if t.messages and isinstance(t.messages, dict):
            self._pending_messages = t.messages.get("pending", []) or []
        else:
            self._pending_messages = []
        self._update_message_ui()

        # 防休眠
        if t.keep_awake and isinstance(t.keep_awake, dict):
            self._keep_awake_supported = True  # 能返回数据说明支持
            self._keep_awake_enabled = t.keep_awake.get("enabled", False)
            self._keep_awake_keep_display = t.keep_awake.get("keep_display_on", False)
            self._keep_awake_reason = t.keep_awake.get("reason") or ""
            self._update_keep_awake_ui()

    def _update_message_ui(self) -> None:
        count = len(self._pending_messages)
        self._msg_count_label.setText(f"待发送：{count} 条")
        set_text_role(
            self._msg_count_label,
            "warning" if count > 0 else "secondary",
        )

        # 清除旧行
        while self._msg_list_layout.count():
            item = self._msg_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        import datetime

        for msg in self._pending_messages:
            mid = msg.get("id", "?")
            text = msg.get("text", "")
            ts = msg.get("timestamp", 0)
            try:
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            except Exception:
                ts_str = "?"

            # 单行 widget：文本 + 删除按钮
            row = QFrame()
            row.setStyleSheet(
                f"QFrame {{ background: {tokens.BG_CARD}; border-bottom: 1px solid {tokens.BORDER}; }}"
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(8)

            msg_label = QLabel(f"[{ts_str}] {text}")
            msg_label.setWordWrap(True)
            msg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row_layout.addWidget(msg_label, 1)

            del_btn = QPushButton("删除")
            del_btn.setFixedSize(40, 24)
            del_btn.setToolTip("删除此条")
            set_kind(del_btn, "danger")
            del_btn.clicked.connect(lambda _e, _mid=mid: self._delete_message(_mid))
            row_layout.addWidget(del_btn)

            self._msg_list_layout.addWidget(row)

        self._msg_list_layout.addStretch()

    def _update_keep_awake_ui(self) -> None:
        # 平台支持
        if self._keep_awake_supported:
            self._ka_support_label.setText("平台支持：✓ Windows SetThreadExecutionState")
            set_text_role(self._ka_support_label, "success")
            self._ka_enable_btn.setEnabled(True)
            self._ka_display_btn.setEnabled(self._keep_awake_enabled)
        else:
            self._ka_support_label.setText("平台支持：✗ 当前平台不支持")
            set_text_role(self._ka_support_label, "danger")
            self._ka_enable_btn.setEnabled(False)
            self._ka_display_btn.setEnabled(False)
            return

        # 开关状态（blockSignals 避免触发 clicked）
        # 按钮显示即将进行的操作（而非当前状态）
        self._ka_enable_btn.blockSignals(True)
        self._ka_enable_btn.setChecked(self._keep_awake_enabled)
        self._ka_enable_btn.setText("禁用" if self._keep_awake_enabled else "启用")
        self._ka_enable_btn.blockSignals(False)

        self._ka_display_btn.blockSignals(True)
        self._ka_display_btn.setChecked(self._keep_awake_keep_display)
        self._ka_display_btn.setText("禁用显示器休眠" if not self._keep_awake_keep_display else "恢复显示器休眠")
        self._ka_display_btn.setEnabled(self._keep_awake_enabled)
        self._ka_display_btn.blockSignals(False)

        # 原因
        self._ka_reason_input.setText(self._keep_awake_reason)

        # 时长
        if self._keep_awake_enabled:
            # 重新查询以获取最新时长（status 端点提供更准确的 duration）
            self._refresh_duration_async()
        else:
            self._ka_duration_label.setText("已启用时长：—")

    def _refresh_duration_async(self) -> None:
        """异步刷新防休眠时长（避免阻塞 UI）"""
        from PySide6.QtCore import QThread

        class DurationThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.status = None

            def run(self):
                self.status = self._http.get("/system/status")

        self._duration_thread = DurationThread(self._http)
        self._duration_thread.finished.connect(self._on_duration_done)
        self._duration_thread.start()

    def _on_duration_done(self) -> None:
        t = getattr(self, "_duration_thread", None)
        if t is None or t.status is None:
            return
        s = t.status
        if not isinstance(s, dict):
            return
        duration = s.get("keep_awake_enabled_duration_seconds")
        if duration is not None:
            try:
                self._ka_duration_label.setText(
                    f"已启用时长：{format_uptime(float(duration))}"
                )
            except (TypeError, ValueError):
                pass
        err = s.get("keep_awake_last_error")
        if err:
            self._ka_error_label.setText(err)
            self._ka_error_label.setVisible(True)
        else:
            self._ka_error_label.setVisible(False)

    # —— 用户补充指令操作 ——

    def _send_message(self) -> None:
        text = self._msg_input.text().strip()
        if not text:
            return
        worker = self._make_worker("post", "/user/message", json={"text": text})
        worker.done.connect(self._on_send_message_done)
        worker.failed.connect(lambda _err: self._on_send_message_done(None))
        worker.start()

    def _on_send_message_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "发送失败（后端返回错误）")
            return
        self._msg_input.clear()
        self._refresh_async()

    def _delete_message(self, msg_id: str) -> None:
        """删除指定的单条用户补充指令"""
        if not msg_id:
            return
        worker = self._make_worker("delete", f"/user/message/{msg_id}")
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()

    def _clear_all_messages(self) -> None:
        if not self._pending_messages:
            return
        ret = QMessageBox.question(
            self, "确认", f"清空 {len(self._pending_messages)} 条待发送消息？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("delete", "/user/message")
        worker.done.connect(lambda _resp: self._refresh_async())
        worker.failed.connect(lambda _err: self._refresh_async())
        worker.start()

    # —— 防休眠操作 ——

    def _on_enable_toggled(self) -> None:
        enable = self._ka_enable_btn.isChecked()
        # 按钮显示即将进行的操作（按下后显示反向操作）
        self._ka_enable_btn.setText("禁用" if enable else "启用")
        self._ka_display_btn.setEnabled(enable)

        reason = self._ka_reason_input.text().strip() or None
        keep_display = self._ka_display_btn.isChecked()

        worker = self._make_worker("post", "/system/keep-awake", json={
            "enable": enable,
            "keep_display_on": keep_display,
            "reason": reason,
        })
        worker.done.connect(lambda resp: self._on_enable_done(enable, resp))
        worker.failed.connect(lambda _err: self._on_enable_done(enable, None))
        worker.start()

    def _on_enable_done(self, enable: bool, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "设置失败（后端返回错误）")
            # 回滚按钮状态
            self._ka_enable_btn.blockSignals(True)
            self._ka_enable_btn.setChecked(not enable)
            self._ka_enable_btn.setText("禁用" if not enable else "启用")
            self._ka_enable_btn.blockSignals(False)
            self._ka_display_btn.setEnabled(not enable)
            return
        if not resp.get("success"):
            err_msg = resp.get("message", "未知错误")
            QMessageBox.warning(self, "失败", f"设置失败：{err_msg}")
        # 刷新以同步状态
        self._refresh_async()

    def _on_display_toggled(self) -> None:
        # 仅当防休眠已开启时有效
        if not self._keep_awake_enabled:
            # 不应该触发，但防御性处理
            self._ka_display_btn.blockSignals(True)
            self._ka_display_btn.setChecked(False)
            self._ka_display_btn.setText("禁用显示器休眠")
            self._ka_display_btn.blockSignals(False)
            return
        keep_display = self._ka_display_btn.isChecked()
        self._ka_display_btn.setText("恢复显示器休眠" if keep_display else "禁用显示器休眠")

        reason = self._ka_reason_input.text().strip() or None
        worker = self._make_worker("post", "/system/keep-awake", json={
            "enable": True,
            "keep_display_on": keep_display,
            "reason": reason,
        })
        worker.done.connect(lambda resp: self._on_display_done(keep_display, resp))
        worker.failed.connect(lambda _err: self._on_display_done(keep_display, None))
        worker.start()

    def _on_display_done(self, keep_display: bool, resp: dict | None) -> None:
        if resp is None or not resp.get("success"):
            QMessageBox.warning(self, "失败", "设置显示器休眠策略失败")
            # 回滚
            self._ka_display_btn.blockSignals(True)
            self._ka_display_btn.setChecked(not keep_display)
            self._ka_display_btn.setText("恢复显示器休眠" if not keep_display else "禁用显示器休眠")
            self._ka_display_btn.blockSignals(False)
            return
        self._refresh_async()
