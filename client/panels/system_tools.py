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

# 中性动作按钮样式：文案表示“即将进行的操作”，真实状态由旁边文本单独展示，
# 不再用按钮背景色表达开关状态（避免“文案是动作、颜色是状态”的冲突）。
_ACTION_BTN_STYLE = (
    f"QPushButton {{ background: {tokens.BG_INPUT}; color: {tokens.TEXT_PRIMARY};"
    f" padding: 6px 12px; border: 1px solid {tokens.BORDER}; }}"
    f"QPushButton:disabled {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_TERTIARY}; }}"
)


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

        # OCR 常驻状态
        self._ocr_keep: bool = False
        self._ocr_loaded: bool = False

        # 电脑操作许可状态（/health.screen.takeover_persistent）
        self._health: dict | None = None
        self._perm_configured: bool = True
        self._perm_active: bool = False
        self._perm_mode: str = "no_permission"
        self._perm_source: str = ""
        self._perm_remaining: int = 0
        self._perm_revoked: str = ""
        self._perm_busy: bool = False
        self._perm_thread = None

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

        # OCR 常驻管理分区
        self._ocr_card = _SectionCard("OCR 常驻管理")
        self._build_ocr_section(self._ocr_card.body_layout)
        container_layout.addWidget(self._ocr_card)

        # 电脑操作许可分区
        self._perm_card = _SectionCard("电脑操作许可")
        self._build_permission_section(self._perm_card.body_layout)
        container_layout.addWidget(self._perm_card)

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
        enable_row.setSpacing(10)
        enable_label = QLabel("防休眠开关")
        set_text_role(enable_label, "secondary")
        enable_row.addWidget(enable_label)
        enable_row.addStretch()
        self._ka_enable_btn = QPushButton("关闭")
        self._ka_enable_btn.setFixedWidth(90)
        self._ka_enable_btn.clicked.connect(self._on_enable_toggled)
        self._ka_enable_btn.setStyleSheet(_ACTION_BTN_STYLE)
        enable_row.addWidget(self._ka_enable_btn)
        self._ka_enable_status = QLabel("当前：已关闭")
        self._ka_enable_status.setFixedWidth(120)
        self._ka_enable_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        set_text_role(self._ka_enable_status, "tertiary")
        enable_row.addWidget(self._ka_enable_status)
        parent_layout.addLayout(enable_row)

        # 阻止显示器休眠开关
        display_row = QHBoxLayout()
        display_row.setSpacing(10)
        display_label = QLabel("阻止显示器休眠")
        set_text_role(display_label, "secondary")
        display_row.addWidget(display_label)
        display_row.addStretch()
        self._ka_display_btn = QPushButton("关闭")
        self._ka_display_btn.setFixedWidth(90)
        self._ka_display_btn.clicked.connect(self._on_display_toggled)
        self._ka_display_btn.setStyleSheet(_ACTION_BTN_STYLE)
        display_row.addWidget(self._ka_display_btn)
        self._ka_display_status = QLabel("当前：已禁用")
        self._ka_display_status.setFixedWidth(120)
        self._ka_display_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        set_text_role(self._ka_display_status, "tertiary")
        display_row.addWidget(self._ka_display_status)
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

    def _build_ocr_section(self, parent_layout: QVBoxLayout) -> None:
        # 常驻状态
        keep_row = QHBoxLayout()
        keep_label = QLabel("常驻状态")
        set_text_role(keep_label, "secondary")
        keep_row.addWidget(keep_label)
        keep_row.addStretch()
        self._ocr_keep_label = QLabel("—")
        self._ocr_keep_label.setStyleSheet("font-weight: bold;")
        keep_row.addWidget(self._ocr_keep_label)
        parent_layout.addLayout(keep_row)

        # 内存中
        loaded_row = QHBoxLayout()
        loaded_label = QLabel("是否在内存中")
        set_text_role(loaded_label, "secondary")
        loaded_row.addWidget(loaded_label)
        loaded_row.addStretch()
        self._ocr_loaded_label = QLabel("—")
        self._ocr_loaded_label.setStyleSheet("font-weight: bold;")
        loaded_row.addWidget(self._ocr_loaded_label)
        parent_layout.addLayout(loaded_row)

        # 常驻开关（文案=即将进行的操作；真实状态由上面两行文本展示）
        toggle_row = QHBoxLayout()
        toggle_label = QLabel("常驻开关")
        set_text_role(toggle_label, "secondary")
        toggle_row.addWidget(toggle_label)
        toggle_row.addStretch()
        self._ocr_toggle_btn = QPushButton("关闭常驻")
        self._ocr_toggle_btn.setFixedWidth(110)
        self._ocr_toggle_btn.clicked.connect(self._on_ocr_toggle)
        self._ocr_toggle_btn.setStyleSheet(_ACTION_BTN_STYLE)
        toggle_row.addWidget(self._ocr_toggle_btn)
        parent_layout.addLayout(toggle_row)

        # 提示
        hint = QLabel(
            "关闭常驻会立即释放 PaddleOCR 显存（打游戏更流畅），下次 OCR 调用会冷启动 10–30s；"
            "开启会重新预热，恢复秒级响应。"
        )
        set_text_role(hint, "tertiary")
        hint.setWordWrap(True)
        parent_layout.addWidget(hint)

    def _build_permission_section(self, parent_layout: QVBoxLayout) -> None:
        """电脑操作许可：用户手动开关 agent 的屏幕操控授权（默认关）。"""
        # 开关行（文案=即将进行的操作；真实状态由状态文本展示，与防休眠/OCR 卡约定一致）
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(10)
        toggle_label = QLabel("操作许可")
        set_text_role(toggle_label, "secondary")
        toggle_row.addWidget(toggle_label)
        toggle_row.addStretch()
        self._perm_toggle_btn = QPushButton("开启许可")
        self._perm_toggle_btn.setFixedWidth(110)
        self._perm_toggle_btn.clicked.connect(self._on_permission_toggle)
        self._perm_toggle_btn.setStyleSheet(_ACTION_BTN_STYLE)
        toggle_row.addWidget(self._perm_toggle_btn)
        self._perm_status_label = QLabel("当前：未授权")
        self._perm_status_label.setFixedWidth(140)
        self._perm_status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        set_text_role(self._perm_status_label, "tertiary")
        toggle_row.addWidget(self._perm_status_label)
        parent_layout.addLayout(toggle_row)

        # 详情行（来源/剩余时间/撤销原因）
        self._perm_detail_label = QLabel("默认关闭。开启后 agent 可在授权窗口期内操控电脑。")
        set_text_role(self._perm_detail_label, "secondary")
        self._perm_detail_label.setWordWrap(True)
        parent_layout.addWidget(self._perm_detail_label)

        # 错误信息（弹窗取消/请求失败）
        self._perm_error_label = QLabel("")
        set_text_role(self._perm_error_label, "danger")
        self._perm_error_label.setWordWrap(True)
        self._perm_error_label.setVisible(False)
        parent_layout.addWidget(self._perm_error_label)

        # 提示
        hint = QLabel(
            "开启会弹出后端确认窗，确认后生效（勾选\"看门狗模式\"可获长期授权，"
            "不因空闲撤销）；普通授权空闲 30 分钟自动关闭。"
            "任何时候 Ctrl+` 可紧急停止 agent 的键鼠操作。"
        )
        set_text_role(hint, "tertiary")
        hint.setWordWrap(True)
        parent_layout.addWidget(hint)

    def _on_permission_toggle(self) -> None:
        """操作许可开关：开启=请求授权（后端弹窗确认后生效），关闭=立即收回。"""
        if self._perm_thread is not None and self._perm_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        if self._perm_active:
            path = "/screen/control/release"
            payload: dict = {}
            timeout: float | None = None
        else:
            path = "/screen/control/request"
            payload = {
                "task_description": "用户系统工具面板授权",
                "source": "gui_panel",
                "mode": "watchdog",
            }
            # 后端确认窗超时 + 网络缓冲（用户点弹窗需要时间）
            takeover = (self._health or {}).get("screen", {}).get(
                "takeover_confirm", {}
            )
            try:
                timeout = max(5.0, float(takeover.get("timeout_seconds", 30)) + 5.0)
            except (TypeError, ValueError):
                timeout = 35.0

        class PermissionThread(QThread):
            def __init__(self, http, path, payload, timeout):
                super().__init__()
                self._http = http
                self._path = path
                self._payload = payload
                self._timeout = timeout
                self.success = False
                self.error = ""
                self.status = ""

            def run(self):
                try:
                    resp = self._http.post(
                        self._path, json=self._payload, timeout=self._timeout
                    )
                    self.success = bool(resp and resp.get("success"))
                    self.status = resp.get("status", "") if resp else ""
                    if not self.success:
                        msg = resp.get("message", "请求未获批准") if resp else "请求失败"
                        reason = resp.get("user_reason", "") if resp else ""
                        self.error = f"{msg}（用户反馈：{reason}）" if reason else msg
                except Exception as e:
                    self.error = str(e)

        self._perm_thread = PermissionThread(self._http, path, payload, timeout)
        self._perm_thread.finished.connect(self._on_permission_done)
        self._perm_busy = True
        self._perm_toggle_btn.setEnabled(False)
        self._perm_error_label.setVisible(False)
        self._perm_thread.start()

    def _on_permission_done(self) -> None:
        """授权端点调用完成：刷新状态（/health 为准）。"""
        t = self._perm_thread
        self._perm_busy = False
        if t is not None and not t.success and t.error:
            self._perm_error_label.setText(t.error)
            self._perm_error_label.setVisible(True)
        elif t is not None and t.status == "mode_downgraded":
            self._perm_error_label.setText(
                "看门狗模式未确认（确认窗未勾选复选框），已授予普通授权"
                "（空闲 30 分钟自动撤销）"
            )
            self._perm_error_label.setVisible(True)
        self._refresh_async()

    def _update_permission_ui(self) -> None:
        """根据 /health.screen.takeover_persistent 更新许可卡显示。"""
        if self._perm_toggle_btn is None:
            return
        if self._perm_active:
            mode_text = "看门狗" if self._perm_mode == "watchdog" else "普通"
            self._perm_status_label.setText(f"当前：已授权·{mode_text}")
            remaining = max(0, int(self._perm_remaining or 0))
            if self._perm_mode == "watchdog":
                hours, rem = divmod(remaining, 3600)
                minutes, seconds = divmod(rem, 60)
                time_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                minutes, seconds = divmod(remaining, 60)
                time_text = f"{minutes:02d}:{seconds:02d}"
            detail = (
                f"已授权 · 来源 {self._perm_source or '未知'} · 剩余 {time_text}。"
                "agent 侧收回或空闲超时后此处自动回弹为关闭。"
            )
            self._perm_detail_label.setText(detail)
            self._perm_toggle_btn.setText("关闭许可")
        else:
            self._perm_status_label.setText("当前：未授权")
            revoked = f"上次撤销原因：{self._perm_revoked}。" if self._perm_revoked else ""
            self._perm_detail_label.setText(
                f"默认关闭。{revoked}开启后 agent 可在授权窗口期内操控电脑。"
            )
            self._perm_toggle_btn.setText("开启许可")
        self._perm_toggle_btn.setEnabled(self._perm_configured and not self._perm_busy)
        if not self._perm_configured:
            self._perm_status_label.setText("当前：已被配置禁用")

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
                self.health = None

            def run(self):
                self.messages = self._http.get("/user/message")
                self.keep_awake = self._http.get("/system/keep-awake")
                self.health = self._http.get("/health")

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

        # OCR 常驻（/health.ocr）
        if t.health and isinstance(t.health, dict):
            self._health = t.health
            ocr = t.health.get("ocr", {}) or {}
            self._ocr_keep = bool(ocr.get("keep_models", False))
            self._ocr_loaded = bool(ocr.get("ocr_loaded", False))
            self._update_ocr_ui()

        # 电脑操作许可（/health.screen.takeover_persistent）
        if t.health and isinstance(t.health, dict):
            persistent = (t.health.get("screen", {}) or {}).get(
                "takeover_persistent", {}
            ) or {}
            self._perm_configured = bool(persistent.get("configured", True))
            self._perm_active = bool(
                persistent.get("active", persistent.get("enabled", False))
            )
            self._perm_mode = persistent.get("mode", "no_permission")
            self._perm_source = persistent.get("source", "") or ""
            try:
                self._perm_remaining = int(persistent.get("remaining_seconds", 0))
            except (TypeError, ValueError):
                self._perm_remaining = 0
            self._perm_revoked = persistent.get("revoked_reason", "") or ""
        else:
            self._perm_active = False
        self._update_permission_ui()

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
            if item is None:
                continue
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

        # 按钮文案 = 即将进行的操作（动作）；真实状态用旁边文本单独展示
        self._ka_enable_btn.setText("禁用" if self._keep_awake_enabled else "启用")
        self._ka_enable_status.setText("当前：已开启" if self._keep_awake_enabled else "已关闭")
        set_text_role(self._ka_enable_status, "success" if self._keep_awake_enabled else "tertiary")

        self._ka_display_btn.setText("禁用显示器休眠" if not self._keep_awake_keep_display else "恢复显示器休眠")
        self._ka_display_btn.setEnabled(self._keep_awake_enabled)
        if self._keep_awake_enabled:
            self._ka_display_status.setText("当前：已启用" if self._keep_awake_keep_display else "已禁用")
            set_text_role(self._ka_display_status, "success" if self._keep_awake_keep_display else "tertiary")
        else:
            self._ka_display_status.setText("不适用")
            set_text_role(self._ka_display_status, "tertiary")

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
        enable = not self._keep_awake_enabled
        reason = self._ka_reason_input.text().strip() or None
        keep_display = self._keep_awake_keep_display

        worker = self._make_worker("post", "/system/keep-awake", json={
            "enable": enable,
            "keep_display_on": keep_display,
            "reason": reason,
        })
        worker.done.connect(lambda resp: self._on_enable_done(enable, resp))
        worker.failed.connect(lambda _err: self._on_enable_done(enable, None))
        worker.start()

    def _on_enable_done(self, enable: bool, resp: dict | None) -> None:
        if resp is None or not resp.get("success"):
            msg = resp.get("message", "后端返回错误") if resp else "后端返回错误"
            QMessageBox.warning(self, "失败", f"设置失败：{msg}")
        # 刷新以同步状态（失败也刷新，自动回滚显示）
        self._refresh_async()

    def _on_display_toggled(self) -> None:
        # 仅当防休眠已开启时有效
        if not self._keep_awake_enabled:
            return
        keep_display = not self._keep_awake_keep_display
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
        self._refresh_async()

    # —— OCR 常驻操作 ——

    def _update_ocr_ui(self) -> None:
        # 常驻状态
        self._ocr_keep_label.setText("开启" if self._ocr_keep else "关闭")
        self._ocr_keep_label.setStyleSheet(
            f"color: {tokens.SUCCESS if self._ocr_keep else tokens.TEXT_TERTIARY}; font-weight: bold;"
        )
        # 是否在内存中
        self._ocr_loaded_label.setText("是" if self._ocr_loaded else "否")
        self._ocr_loaded_label.setStyleSheet(
            f"color: {tokens.SUCCESS if self._ocr_loaded else tokens.TEXT_TERTIARY}; font-weight: bold;"
        )
        # 开关文案 = 即将进行的操作（动作）
        self._ocr_toggle_btn.setText("关闭常驻" if self._ocr_keep else "开启常驻")
        self._ocr_toggle_btn.setEnabled(True)

    def _on_ocr_toggle(self) -> None:
        turn_on = not self._ocr_keep
        self._ocr_toggle_btn.setEnabled(False)
        self._ocr_toggle_btn.setText("处理中...")

        if turn_on:
            # 开启：先置常驻标志，再预热（恢复“在内存中”状态）
            self._ocr_run_chain([
                ("post", "/ocr/models/keep/json", {"keep": True}),
                ("post", "/ocr/models/preload/json", {"engine": "ocr"}),
            ])
        else:
            # 关闭：先卸载释放显存，再置常驻标志为关闭（状态与显示保持一致）
            self._ocr_run_chain([
                ("post", "/ocr/models/unload/json", {"engine": "ocr"}),
                ("post", "/ocr/models/keep/json", {"keep": False}),
            ])

    def _ocr_run_chain(self, steps: list) -> None:
        """依次执行多个 HTTP 步骤，全部完成后以 /health 真相刷新 UI"""
        def _step(i: int) -> None:
            if i >= len(steps):
                self._refresh_async()
                return
            method, path, json_body = steps[i]
            worker = self._make_worker(method, path, json=json_body)
            worker.done.connect(lambda _r, n=i + 1: _step(n))
            worker.failed.connect(lambda _e: self._ocr_op_failed())
            worker.start()
        _step(0)

    def _ocr_op_failed(self) -> None:
        QMessageBox.warning(self, "失败", "OCR 常驻设置失败（后端返回错误）")
        self._refresh_async()
