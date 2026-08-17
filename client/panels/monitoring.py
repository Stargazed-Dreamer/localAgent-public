"""Monitoring 面板 — 状态监控与当前任务授权控制

抄 web 界面状态监控页面的卡片式布局。除 Computer Use 当前任务授权的请求和
收回控件外，其余模块只展示后端运行状态。

终端管理已拆到独立 Terminal 面板；用户补充指令和防休眠已拆到高级分类的
系统工具面板。

数据源：GET /health + Fake Proxy stats（异步刷新，不阻塞 UI）。
"""

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

# 模块状态显示顺序
_HEALTH_MODULES = [
    ("ocr", "OCR"),
    ("vision", "Vision"),
    ("memory", "Memory"),
    ("browser", "Browser"),
    ("exec", "Exec"),
    ("screen", "Screen"),
    ("mindforge", "MindForge"),
    ("loops", "Loops"),
    ("apikey", "ApiKey"),
    ("agent_guide", "AgentGuide"),
    ("inbox", "Inbox"),
    ("user_message", "UserMsg"),
]

# 每个 health 模块对应的可用性字段；None 表示无状态字段，显示 "·"
_MODULE_STATUS_FIELD: dict[str, str | None] = {
    "ocr":          "model_ready",       # 比 ocr_loaded 更精确（ocr_loaded 在模型失败后仍 True）
    "vision":       "vl_available",
    "memory":       "available",
    "browser":      "connected",
    "exec":         None,                # 无 bool 状态字段（temp_dir/history_count/terminals_*）
    "screen":       "capture_available",  # health 中始终 True
    "mindforge":    "available",
    "loops":        "available",
    "apikey":       None,                # 仅 supported_vendors/last_test，无状态 bool
    "agent_guide":  "available",
    "inbox":        "available",
    "user_message": None,                # 仅 pending_count/pending_messages，无状态 bool
}


def _module_status_text(mod_key: str, mod_data: dict) -> str:
    """从模块状态 dict 提取可用性指示文本（显式映射，无状态模块显示 "·"）。"""
    if not isinstance(mod_data, dict) or not mod_data:
        return "—"
    field = _MODULE_STATUS_FIELD.get(mod_key)
    if field is None:
        return "·"  # 无状态字段
    return "✓" if mod_data.get(field) else "✗"


def _bool_text(v) -> str:
    """bool → 是/否"""
    if v is None:
        return "—"
    return "是" if v else "否"


def _safe_str(v, default="—") -> str:
    if v is None or v == "":
        return default
    return str(v)


class _CollapsibleCard(QFrame):
    """可折叠卡片：标题栏点击切换展开/收起，内容区用 QFormLayout 展示字段。

    纯展示组件，不含任何输入控件。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("monitorCard")
        self._expanded = True
        self._field_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏（可点击折叠）
        self._header = QFrame()
        self._header.setObjectName("monitorCardHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            f"QFrame#monitorCardHeader {{ background: {tokens.BG_CARD}; border: 1px solid {tokens.BORDER};"
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
        # 鼠标点击事件
        self._header.mousePressEvent = lambda _e: self.toggle()
        layout.addWidget(self._header)

        # 内容区
        self._body = QFrame()
        self._body.setObjectName("monitorCardBody")
        self._body.setStyleSheet(
            f"QFrame#monitorCardBody {{ background: {tokens.BG_PANEL}; border: 1px solid {tokens.BORDER};"
            f" border-top: none; }}"
        )
        self._form = QFormLayout(self._body)
        self._form.setContentsMargins(14, 10, 14, 10)
        self._form.setSpacing(6)
        layout.addWidget(self._body)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow_label.setText("▼" if self._expanded else "▶")

    def add_field(self, key: str, label: str) -> None:
        """添加一个状态字段"""
        value_label = QLabel("—")
        set_text_role(value_label, "secondary")
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        label_widget = QLabel(label)
        set_text_role(label_widget, "tertiary")
        self._form.addRow(label_widget, value_label)
        self._field_labels[key] = value_label

    def set_field(self, key: str, value: str, color: str = tokens.TEXT_SECONDARY) -> None:
        """更新字段值"""
        label = self._field_labels.get(key)
        if label is not None:
            label.setText(value)
            label.setStyleSheet(f"color: {color};")


class MonitoringPanel(PanelBase):
    """纯展示状态监控面板"""

    PANEL_META = PanelMeta(
        id="monitoring",
        title="监控",
        icon="bar-chart",
        order=40,
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._health: dict | None = None
        self._fake_stats: dict | None = None
        self._refresh_btn: QPushButton | None = None
        self._task_authorization_status_label: QLabel | None = None
        self._persistent_request_btn: QPushButton | None = None
        self._persistent_release_btn: QPushButton | None = None
        self._persistent_thread = None
        self._refresh_thread = None

        self._build_ui()
        self._task_authorization_timer = QTimer(self)
        self._task_authorization_timer.setInterval(1000)
        self._task_authorization_timer.timeout.connect(
            self._tick_task_authorization
        )
        self._task_authorization_timer.start()

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
        title = QLabel("状态监控")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setToolTip("刷新后端与授权状态")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        authorization_row = QHBoxLayout()
        self._task_authorization_status_label = QLabel("当前任务未授权")
        self._task_authorization_status_label.setWordWrap(True)
        set_text_role(self._task_authorization_status_label, "secondary")
        authorization_row.addWidget(self._task_authorization_status_label, 1)

        self._persistent_request_btn = QPushButton("请求任务授权")
        self._persistent_request_btn.setToolTip(
            "请求当前任务的普通 Computer Use 操作授权"
        )
        self._persistent_request_btn.setStatusTip(
            "授权后普通操作免重复确认，危险操作仍需确认"
        )
        set_kind(self._persistent_request_btn, "primary")
        self._persistent_request_btn.clicked.connect(
            self._on_persistent_request_clicked
        )
        authorization_row.addWidget(self._persistent_request_btn)

        self._persistent_release_btn = QPushButton("收回授权")
        self._persistent_release_btn.setToolTip("立即收回当前任务授权")
        self._persistent_release_btn.setStatusTip("当前没有可收回的任务授权")
        self._persistent_release_btn.clicked.connect(
            self._on_persistent_release_clicked
        )
        authorization_row.addWidget(self._persistent_release_btn)
        layout.addLayout(authorization_row)

        # 滚动区域包含所有卡片
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(10)

        self._cards: dict[str, _CollapsibleCard] = {}
        for card_id, card_title in self._card_definitions():
            card = _CollapsibleCard(card_title)
            self._cards[card_id] = card
            container_layout.addWidget(card)

        container_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _card_definitions(self) -> list[tuple[str, str]]:
        """返回 (card_id, title) 列表"""
        return [
            ("backend", "后端服务"),
            ("ocr", "OCR 模块"),
            ("vision", "Vision 模块"),
            ("browser", "Browser 模块"),
            ("exec", "Exec 模块"),
            ("screen", "Screen 模块"),
            ("memory", "Memory 模块（三层记忆 v2）"),
            ("maintainer", "记忆维护器"),
            ("mindforge", "MindForge 模块"),
            ("accounting", "Accounting 模块"),
            ("docviewer", "DocViewer 模块"),
            ("apikey", "ApiKey 模块"),
            ("llm_pool", "LLM 并发池"),
            ("todos", "待办模块"),
            ("mcp", "MCP 架构"),
            ("fake_proxy", "Fake LLM Proxy"),
        ]

    def _init_card_fields(self) -> None:
        """初始化所有卡片的字段（仅执行一次）"""
        if getattr(self, "_fields_initialized", False):
            return
        self._fields_initialized = True

        c = self._cards
        c["backend"].add_field("version", "版本")
        c["backend"].add_field("uptime", "运行时长")
        c["backend"].add_field("modules", "模块状态")
        c["backend"].add_field("terminals", "终端")
        c["backend"].add_field("mcp_tools", "MCP 工具")

        c["ocr"].add_field("ocr_state", "OCR 状态")
        c["ocr"].add_field("load_ms", "加载耗时")
        c["ocr"].add_field("last_inf_ms", "最近推理")
        c["ocr"].add_field("inf_count", "推理次数")
        c["ocr"].add_field("last_error", "最近错误")
        c["ocr"].add_field("vl", "远程 VL")
        c["ocr"].add_field("vl_provider", "VL Provider")
        c["ocr"].add_field("vl_model", "VL 模型")
        c["ocr"].add_field("vl_sensitive", "敏感可用")

        c["vision"].add_field("vl", "远程 VL")
        c["vision"].add_field("vl_provider", "VL Provider")
        c["vision"].add_field("vl_model", "VL 模型")
        c["vision"].add_field("vl_sensitive", "敏感可用")

        c["browser"].add_field("connected", "连接状态")
        c["browser"].add_field("port", "调试端口")
        c["browser"].add_field("tab_count", "标签页数量")

        c["exec"].add_field("temp_dir", "临时目录")
        c["exec"].add_field("exec_count", "执行历史数")
        c["exec"].add_field("term_total", "终端总数")
        c["exec"].add_field("term_running", "运行中终端")

        c["screen"].add_field("capture", "截图可用")
        c["screen"].add_field("emergency", "紧急停止")
        c["screen"].add_field("confirm", "确认模式")
        c["screen"].add_field("skip_cache", "跳过缓存")
        c["screen"].add_field("task_authorization", "当前任务授权")
        c["screen"].add_field("authorization_timeout", "空闲超时")

        c["memory"].add_field("available", "可用性")
        c["memory"].add_field("messages", "消息数")
        c["memory"].add_field("facts", "事实数")
        c["memory"].add_field("summaries", "摘要数")
        c["memory"].add_field("embedding", "语义嵌入")
        c["memory"].add_field("db_size", "DB 大小")

        c["maintainer"].add_field("last_run", "上次运行")
        c["maintainer"].add_field("stale_count", "stale 数量")
        c["maintainer"].add_field("validate_dist", "验证分布")

        c["mindforge"].add_field("available", "可用性")
        c["mindforge"].add_field("index", "知识库索引")
        c["mindforge"].add_field("engine", "搜索引擎")
        c["mindforge"].add_field("daemon", "转换守护进程")

        c["accounting"].add_field("available", "审核数据")
        c["accounting"].add_field("items", "待审条目数")

        c["docviewer"].add_field("available", "可用性")
        c["docviewer"].add_field("formats", "支持格式")

        c["apikey"].add_field("vendors", "支持厂商数")
        c["apikey"].add_field("last_test", "最近测试")

        c["llm_pool"].add_field("init", "初始化状态")
        c["llm_pool"].add_field("keys", "可用 Key")
        c["llm_pool"].add_field("free_keys", "免费/Paid Key")
        c["llm_pool"].add_field("models_summary", "模型总数")
        c["llm_pool"].add_field("concurrency", "当前并发")
        c["llm_pool"].add_field("recent_calls", "近期调用数")
        c["llm_pool"].add_field("health", "健康检查")
        c["llm_pool"].add_field("last_check", "上次检查")

        c["todos"].add_field("available", "可用性")
        c["todos"].add_field("total", "待办总数")
        c["todos"].add_field("due_count", "到期数")
        c["todos"].add_field("wip_total", "WIP 总数")
        c["todos"].add_field("wip_active", "活跃 WIP")

        c["mcp"].add_field("direct", "直接工具数")
        c["mcp"].add_field("gateway", "网关子工具数")
        c["mcp"].add_field("template", "操作模板数")
        c["mcp"].add_field("categories", "网关分类")
        c["mcp"].add_field("image_patch", "ImageContent 补丁")

        c["fake_proxy"].add_field("status", "状态")
        c["fake_proxy"].add_field("total", "请求数")
        c["fake_proxy"].add_field("stream", "流式")
        c["fake_proxy"].add_field("non_stream", "非流式")
        c["fake_proxy"].add_field("recent", "最近记录")

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._init_card_fields()
        if self._health:
            self._stack.setCurrentWidget(self._normal_widget)
            self._update_all_cards()
        self._refresh_async()

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
            self._health = None
            self._fake_stats = None

    @Slot(dict)
    def on_health_changed(self, health: dict) -> None:
        """ServiceManager.backend_health_changed 信号的 slot"""
        self._health = health
        self._update_all_cards()
        self._update_task_authorization_controls()

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
                self.health = None
                self.fake_stats = None

            def run(self):
                self.health = self._http.get("/health")
                self.fake_stats = self._http.fake_proxy_stats()

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        if t.health is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)
        self._health = t.health
        self._fake_stats = t.fake_stats
        self._update_all_cards()
        self._update_task_authorization_controls()

    # —— 当前任务授权 ——

    def _update_task_authorization_controls(self) -> None:
        """根据 /health 更新任务授权摘要和按钮状态。

        新会话状态字段（spec 决策 14）：
        - mode: no_permission / normal / watchdog
        - phase: normal 正常 / warning 告警 / inactive / watchdog 全程
        - shutdown_permitted: watchdog 模式下用户是否勾选允许关机
        """
        if not self._health:
            return
        screen_data = self._health.get("screen", {})
        persistent = screen_data.get("takeover_persistent", {})
        configured = bool(persistent.get("configured", True))
        active = bool(persistent.get("active", persistent.get("enabled", False)))
        mode = persistent.get("mode", "no_permission")
        phase = persistent.get("phase", "inactive")
        is_watchdog = mode == "watchdog"
        shutdown_permitted = bool(persistent.get("shutdown_permitted", False))
        if self._task_authorization_status_label:
            if active:
                remaining = max(0, int(persistent.get("remaining_seconds", 0)))
                task = _safe_str(persistent.get("task_description"), "未命名任务")
                source = _safe_str(persistent.get("source"), "未知来源")
                if is_watchdog:
                    # watchdog 模式：HH:MM:SS 格式（硬上限可能 >1 小时）+ 关机权限
                    hours, rem = divmod(remaining, 3600)
                    minutes, seconds = divmod(rem, 60)
                    shutdown_text = "允许关机" if shutdown_permitted else "禁止关机"
                    self._task_authorization_status_label.setText(
                        f"看门狗模式 · {task} · 来源 {source} · "
                        f"剩余 {hours:02d}:{minutes:02d}:{seconds:02d} · {shutdown_text}"
                    )
                    set_text_role(self._task_authorization_status_label, "warning")
                elif phase == "warning":
                    # normal 告警阶段（10-30 分钟）：黄色 + MM:SS
                    minutes, seconds = divmod(remaining, 60)
                    self._task_authorization_status_label.setText(
                        f"即将降级 · {task} · 来源 {source} · 剩余 {minutes:02d}:{seconds:02d}"
                    )
                    set_text_role(self._task_authorization_status_label, "warning")
                else:
                    # normal 正常阶段（0-10 分钟）：浅蓝 + MM:SS
                    minutes, seconds = divmod(remaining, 60)
                    self._task_authorization_status_label.setText(
                        f"普通执行模式 · {task} · 来源 {source} · 剩余 {minutes:02d}:{seconds:02d}"
                    )
                    set_text_role(self._task_authorization_status_label, "success")
            elif configured:
                self._task_authorization_status_label.setText("当前任务未授权")
                set_text_role(self._task_authorization_status_label, "secondary")
            else:
                self._task_authorization_status_label.setText("当前任务授权已被配置禁用")
                set_text_role(self._task_authorization_status_label, "warning")
        if self._persistent_request_btn:
            self._persistent_request_btn.setEnabled(configured and not active)
            self._persistent_request_btn.setStatusTip(
                "授权功能已被配置禁用"
                if not configured
                else "已有任务授权，请先收回当前授权"
                if active
                else "授权后普通操作免重复确认，危险操作仍需确认"
            )
        if self._persistent_release_btn:
            self._persistent_release_btn.setEnabled(active)
            self._persistent_release_btn.setStatusTip(
                "立即收回当前任务授权"
                if active
                else "当前没有可收回的任务授权"
            )

    def _update_persistent_buttons(self) -> None:
        """Compatibility alias for older callers and tests."""
        self._update_task_authorization_controls()

    def _tick_task_authorization(self) -> None:
        """Advance the visible countdown between health refreshes."""
        if not self._health:
            return
        persistent = self._health.get("screen", {}).get(
            "takeover_persistent", {}
        )
        if not persistent.get("active", persistent.get("enabled", False)):
            return
        remaining = max(0, int(persistent.get("remaining_seconds", 0)) - 1)
        persistent["remaining_seconds"] = remaining
        self._update_task_authorization_controls()
        if remaining == 0:
            self._refresh_async()

    def _on_persistent_request_clicked(self) -> None:
        """打开用户确认流程，请求当前任务授权。

        弹出自定义 QDialog 让用户输入任务描述 + 选择看门狗模式（复选框）。
        看门狗模式：不因空闲撤销，硬上限到期撤销（默认 10h），takeover_confirm 禁用。
        用户取消则不发请求。
        """
        from datetime import datetime

        takeover = (self._health or {}).get("screen", {}).get(
            "takeover_confirm", {}
        )
        try:
            prompt_timeout = float(takeover.get("timeout_seconds", 30))
        except (TypeError, ValueError):
            prompt_timeout = 30.0

        # 自定义 QDialog：任务描述输入 + 看门狗模式复选框
        default_desc = f"GUI 请求 - {datetime.now().strftime('%H:%M:%S')}"
        dialog = QDialog(self)
        dialog.setWindowTitle("请求任务授权")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("任务描述（用于授权历史回溯）："))
        desc_edit = QLineEdit(default_desc)
        layout.addWidget(desc_edit)

        watchdog_check = QCheckBox(
            "看门狗模式（持续监控，不因空闲撤销，10h 后自动收回，危险操作仍拦截）"
        )
        watchdog_check.setChecked(False)
        layout.addWidget(watchdog_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return  # 用户取消，不发请求

        task_desc = desc_edit.text().strip()
        if not task_desc:
            return

        payload = {
            "task_description": task_desc,
            "source": "gui",
        }
        if watchdog_check.isChecked():
            payload["mode"] = "watchdog"

        self._persistent_async_call(
            "/screen/control/request",
            "当前任务授权已开启",
            payload=payload,
            timeout=max(5.0, prompt_timeout + 5.0),
        )

    def _on_persistent_release_clicked(self) -> None:
        """立即收回当前任务授权。"""
        self._persistent_async_call(
            "/screen/control/release", "当前任务授权已收回"
        )

    def _persistent_async_call(
        self,
        path: str,
        success_msg: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> None:
        """异步调用任务授权端点，完成后刷新面板。"""
        if self._persistent_thread is not None and self._persistent_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class PersistentThread(QThread):
            def __init__(
                self,
                http,
                path: str,
                payload: dict,
                timeout: float | None,
            ):
                super().__init__()
                self._http = http
                self._path = path
                self._payload = payload
                self._timeout = timeout
                self.success = False
                self.error = ""
                # ISSUE-004：保留完整响应字段，供回调显示用户反馈/状态/模式
                self.response: dict = {}
                self.user_reason = ""
                self.status = ""
                self.mode = ""
                self.requested_mode = ""

            def run(self):
                try:
                    response = self._http.post(
                        self._path,
                        json=self._payload,
                        timeout=self._timeout,
                    )
                    self.response = response or {}
                    self.success = bool(response and response.get("success"))
                    self.user_reason = response.get("user_reason", "") if response else ""
                    self.status = response.get("status", "") if response else ""
                    self.mode = response.get("mode", "") if response else ""
                    self.requested_mode = response.get("requested_mode", "") if response else ""
                    if not self.success:
                        # 失败时优先用 message + user_reason 组合，让用户反馈可见
                        msg = response.get("message", "请求未获批准") if response else "请求失败"
                        if self.user_reason:
                            self.error = f"{msg}（用户反馈：{self.user_reason}）"
                        else:
                            self.error = msg
                except Exception as e:
                    self.error = str(e)

        self._persistent_thread = PersistentThread(
            self._http, path, payload or {}, timeout
        )
        self._persistent_thread.finished.connect(
            lambda: self._on_persistent_call_done(success_msg)
        )
        # 禁用两个按钮防止重复点击
        if self._persistent_request_btn:
            self._persistent_request_btn.setEnabled(False)
        if self._persistent_release_btn:
            self._persistent_release_btn.setEnabled(False)
        self._persistent_thread.start()

    def _on_persistent_call_done(self, success_msg: str) -> None:
        """持久授权端点调用完成"""
        t = self._persistent_thread
        if t and (t.error or not t.success):
            # 失败：恢复按钮状态（根据当前 health）
            self._update_task_authorization_controls()
            # ISSUE-004：失败时显示用户反馈/错误，让用户知道为什么失败
            if self._task_authorization_status_label and t.error:
                self._task_authorization_status_label.setText(t.error)
                set_text_role(self._task_authorization_status_label, "warning")
            return
        # 成功：刷新面板拉取最新 persistent_mode 状态
        self._refresh_async()

    # —— 卡片更新 ——

    def _update_all_cards(self) -> None:
        if not self._health:
            return
        self._init_card_fields()
        h = self._health
        c = self._cards

        # 后端服务
        c["backend"].set_field("version", _safe_str(h.get("version", "?")))
        c["backend"].set_field("uptime", format_uptime(h.get("uptime_seconds", 0)))
        mod_lines = []
        for key, name in _HEALTH_MODULES:
            mod_data = h.get(key, {})
            mod_lines.append(f"{name} {_module_status_text(key, mod_data)}")
        c["backend"].set_field("modules", "  ".join(mod_lines))
        exec_data = h.get("exec", {})
        if isinstance(exec_data, dict):
            term_total = exec_data.get("terminals_total", 0)
            term_running = exec_data.get("terminals_running", 0)
        else:
            term_total = term_running = 0
        c["backend"].set_field("terminals", f"{term_running} 运行 / {term_total} 总数")
        mcp_data = h.get("mcp", {})
        if isinstance(mcp_data, dict):
            direct = mcp_data.get("direct_tools_count", "?")
            gateway = mcp_data.get("gateway_tools_count", "?")
        else:
            direct = gateway = "?"
        c["backend"].set_field("mcp_tools", f"直连 {direct} / 网关 {gateway}")

        # OCR（P1-A：含 model_ready/cold_start/loading/failed 状态 + 加载/推理耗时）
        ocr = h.get("ocr", {}) or {}
        # OCR 状态文本：ready / cold / loading / failed
        if ocr.get("model_ready"):
            ocr_state_text = "ready"
        elif ocr.get("failed"):
            ocr_state_text = "failed"
        elif ocr.get("loading"):
            ocr_state_text = "loading"
        elif ocr.get("cold_start"):
            ocr_state_text = "cold"
        else:
            ocr_state_text = "—"
        c["ocr"].set_field("ocr_state", ocr_state_text)
        load_ms = ocr.get("load_elapsed_ms")
        c["ocr"].set_field("load_ms", f"{load_ms} ms" if load_ms is not None else "—")
        last_inf = ocr.get("last_inference_ms")
        c["ocr"].set_field("last_inf_ms", f"{last_inf} ms" if last_inf is not None else "—")
        inf_count = ocr.get("inference_count", 0)
        c["ocr"].set_field("inf_count", str(inf_count) if inf_count else "0")
        last_err = ocr.get("last_error")
        c["ocr"].set_field("last_error", _safe_str(last_err) if last_err else "—")
        c["ocr"].set_field("vl", _bool_text(ocr.get("vl_available")))
        c["ocr"].set_field("vl_provider", _safe_str(ocr.get("vl_provider")))
        c["ocr"].set_field("vl_model", _safe_str(ocr.get("vl_model")))
        c["ocr"].set_field("vl_sensitive", _bool_text(ocr.get("vl_usable_for_sensitive")))

        # Vision
        vis = h.get("vision", {}) or {}
        c["vision"].set_field("vl", _bool_text(vis.get("vl_available")))
        c["vision"].set_field("vl_provider", _safe_str(vis.get("vl_provider")))
        c["vision"].set_field("vl_model", _safe_str(vis.get("vl_model")))
        c["vision"].set_field("vl_sensitive", _bool_text(vis.get("vl_usable_for_sensitive")))

        # Browser
        br = h.get("browser", {}) or {}
        c["browser"].set_field("connected", _bool_text(br.get("connected")))
        c["browser"].set_field("port", _safe_str(br.get("debug_port")))
        c["browser"].set_field("tab_count", _safe_str(br.get("tab_count")))

        # Exec
        ex = h.get("exec", {}) or {}
        c["exec"].set_field("temp_dir", _safe_str(ex.get("temp_dir")))
        c["exec"].set_field("exec_count", _safe_str(ex.get("history_count")))
        c["exec"].set_field("term_total", _safe_str(term_total))
        c["exec"].set_field("term_running", _safe_str(term_running))

        # Screen
        sc = h.get("screen", {}) or {}
        c["screen"].set_field("capture", _bool_text(sc.get("capture_available")))
        c["screen"].set_field("emergency", _bool_text(sc.get("emergency_stopped")))
        c["screen"].set_field("confirm", _bool_text(sc.get("require_confirm_default")))
        skip_cache_size = sc.get("auto_skip_cache_size", 0)
        c["screen"].set_field("skip_cache", str(skip_cache_size) if skip_cache_size else "0")
        authorization = sc.get("takeover_persistent", {}) or {}
        if authorization.get("active", authorization.get("enabled", False)):
            task = _safe_str(authorization.get("task_description"), "未命名任务")
            source = _safe_str(authorization.get("source"), "未知来源")
            mode = authorization.get("mode", "normal")
            if mode == "watchdog":
                shutdown_permitted = "允许关机" if authorization.get("shutdown_permitted") else "禁止关机"
                authorization_text = f"看门狗 · {task} · {source} · {shutdown_permitted}"
            elif authorization.get("phase") == "warning":
                authorization_text = f"即将降级 · {task} · {source}"
            else:
                authorization_text = f"普通执行 · {task} · {source}"
        elif authorization.get("configured", True):
            authorization_text = "未授权"
        else:
            authorization_text = "配置禁用"
        c["screen"].set_field("task_authorization", authorization_text)
        timeout_seconds = (
            authorization.get("configured_idle_timeout_seconds")
            or authorization.get("idle_timeout_seconds")
            or 0
        )
        c["screen"].set_field(
            "authorization_timeout",
            f"{int(timeout_seconds)} 秒" if timeout_seconds else "—",
        )

        # Memory
        mem = h.get("memory", {}) or {}
        c["memory"].set_field("available", _bool_text(mem.get("available")))
        c["memory"].set_field("messages", _safe_str(mem.get("messages")))
        c["memory"].set_field("facts", _safe_str(mem.get("facts")))
        c["memory"].set_field("summaries", _safe_str(mem.get("summaries")))
        c["memory"].set_field("embedding", _bool_text(mem.get("embedding_ready")))
        db_size_mb = mem.get("db_size_mb")
        c["memory"].set_field("db_size", f"{db_size_mb} MB" if db_size_mb is not None else "—")

        # 记忆维护器（嵌套在 h["memory"]["maintainer"]）
        mt = h.get("memory", {}).get("maintainer", {}) or {}
        c["maintainer"].set_field("last_run", _safe_str(mt.get("last_run")))
        c["maintainer"].set_field("stale_count", _safe_str(mt.get("stale_total")))
        status_counts = mt.get("status_counts") or {}
        if isinstance(status_counts, dict) and status_counts:
            validate_text = f"keep={status_counts.get('keep', 0)} update={status_counts.get('update', 0)} archive={status_counts.get('archive', 0)}"
        else:
            validate_text = "—"
        c["maintainer"].set_field("validate_dist", validate_text)

        # MindForge
        mf = h.get("mindforge", {}) or {}
        c["mindforge"].set_field("available", _bool_text(mf.get("available")))
        c["mindforge"].set_field("index", _bool_text(mf.get("has_index")))
        c["mindforge"].set_field("engine", _bool_text(mf.get("search_engine_loaded")))
        c["mindforge"].set_field("daemon", _bool_text(mf.get("converter_daemon_running")))

        # Accounting
        acc = h.get("accounting", {}) or {}
        c["accounting"].set_field("available", _bool_text(acc.get("review_data_available")))
        c["accounting"].set_field("items", _safe_str(acc.get("item_count")))

        # DocViewer
        dv = h.get("docviewer", {}) or {}
        c["docviewer"].set_field("available", _bool_text(dv.get("available")))
        c["docviewer"].set_field("formats", _safe_str(dv.get("supported_formats")))

        # ApiKey
        ak = h.get("apikey", {}) or {}
        c["apikey"].set_field("vendors", _safe_str(ak.get("supported_vendors")))
        c["apikey"].set_field("last_test", _safe_str(ak.get("last_test")))

        # LLM Pool
        lp = h.get("llm_pool", {}) or {}
        c["llm_pool"].set_field("init", _bool_text(lp.get("initialized")))
        total_keys = lp.get("total_keys", 0)
        avail_keys = lp.get("active_keys", 0)
        c["llm_pool"].set_field("keys", f"{avail_keys} 可用 / {total_keys} 总数")
        # v10：免费/paid key 分布
        free_keys = lp.get("free_keys", 0)
        paid_keys = lp.get("paid_keys", 0)
        c["llm_pool"].set_field(
            "free_keys", f"免费 {free_keys} / Paid {paid_keys}",
            tokens.SUCCESS_TEXT if free_keys > 0 else tokens.TEXT_SECONDARY,
        )
        # v10：模型总数（含免费模型数）
        total_models = lp.get("total_models", 0)
        free_models = lp.get("free_models", 0)
        c["llm_pool"].set_field(
            "models_summary", f"{total_models} 总数 / {free_models} 免费",
        )
        c["llm_pool"].set_field("concurrency", _safe_str(lp.get("current_active")))
        # v10：近期调用数
        recent_calls_count = lp.get("recent_calls_count", 0)
        c["llm_pool"].set_field(
            "recent_calls", str(recent_calls_count),
            tokens.SUCCESS_TEXT if recent_calls_count > 0 else tokens.TEXT_TERTIARY,
        )
        c["llm_pool"].set_field("health", _bool_text(lp.get("needs_health_check") is False))
        c["llm_pool"].set_field("last_check", _safe_str(lp.get("last_health_check")))

        # Todos
        td = h.get("todos", {}) or {}
        c["todos"].set_field("available", _bool_text(td.get("available")))
        c["todos"].set_field("total", _safe_str(td.get("todos_total")))
        c["todos"].set_field("due_count", _safe_str(td.get("todos_due")))
        c["todos"].set_field("wip_total", _safe_str(td.get("wip_total")))
        c["todos"].set_field("wip_active", _safe_str(td.get("wip_active")))

        # MCP
        mc = h.get("mcp", {}) or {}
        c["mcp"].set_field("direct", _safe_str(mc.get("direct_tools_count")))
        c["mcp"].set_field("gateway", _safe_str(mc.get("gateway_tools_count")))
        c["mcp"].set_field("template", _safe_str(mc.get("template_count")))
        c["mcp"].set_field("categories", _safe_str(mc.get("gateway_categories")))
        c["mcp"].set_field("image_patch", _bool_text(mc.get("image_content_patch")))

        # Fake Proxy
        if self._fake_stats:
            fp = self._fake_stats
            c["fake_proxy"].set_field("status", "在线", tokens.SUCCESS_TEXT)
            c["fake_proxy"].set_field("total", _safe_str(fp.get("total", fp.get("total_requests", 0))))
            c["fake_proxy"].set_field("stream", _safe_str(fp.get("stream", fp.get("streaming_requests", 0))))
            c["fake_proxy"].set_field("non_stream", _safe_str(fp.get("non_stream", fp.get("non_streaming_requests", 0))))
            c["fake_proxy"].set_field("recent", _safe_str(fp.get("recent_count", fp.get("recent", 0))))
        else:
            c["fake_proxy"].set_field("status", "未启动", tokens.TEXT_TERTIARY)
            for k in ("total", "stream", "non_stream", "recent"):
                c["fake_proxy"].set_field(k, "—")
