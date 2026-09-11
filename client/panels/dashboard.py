"""Dashboard 面板 — 概览：状态卡片 + 快捷操作 + 最近活动"""

import subprocess
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from client.core.constants import (
    FAKE_PROXY_LOG_PATH,
    FAKE_PROXY_SCRIPT_PATH,
    PROJECT_ROOT,
    START_BAT_PATH,
    resolve_launch_python,
)
from client.core.format_utils import format_tokens, format_uptime
from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from client.widgets.status_card import StatusCard
from lib.ui import icon
from lib.ui.theme import set_text_role


def _stat_item(icon_name: str, text: str) -> QListWidgetItem:
    """当前数据行：SVG 图标 + 文本（替代 emoji 前缀，统一 lib/ui 图标体系）。"""
    item = QListWidgetItem(text)
    item.setIcon(icon(icon_name))
    return item

# Fake Proxy 启动等待上限（uvicorn 冷启动通常 <2s，留足余量）
FAKE_PROXY_STARTUP_TIMEOUT_S = 12.0


def _fake_stats_details(stats: dict) -> dict:
    """/api/stats → 卡片字段。

    脚本实际返回 total / stream / non_stream（见 tools/fake_llm_proxy.py api_stats），
    monitoring 面板用的就是这套；旧代码读 total_requests / streaming_requests，
    取不到值恒显示 0。这里两种命名都兼容。
    """
    return {
        "请求数": stats.get("total", stats.get("total_requests", stats.get("request_count", 0))),
        "流式": stats.get("stream", stats.get("streaming_requests", 0)),
        "非流式": stats.get("non_stream", stats.get("non_streaming_requests", 0)),
    }


class _FakeProxyOpThread(QThread):
    """启停 Fake Proxy 的后台线程：Popen 与就绪等待都不占 UI 线程。"""

    def __init__(self, http: HttpClient, op: str):
        super().__init__()
        self._http = http
        self._op = op
        self.stats: dict | None = None
        self.ok = False
        self.message = ""

    def run(self) -> None:
        if self._op == "stop":
            self._run_stop()
        else:
            self._run_start()

    def _run_start(self) -> None:
        # 幂等：已在运行就别再 Popen，否则 uvicorn 抢不到 9999 直接崩
        if self._http.fake_proxy_is_reachable():
            self.stats = self._http.fake_proxy_stats()
            self.ok = self.stats is not None
            self.message = "Fake Proxy 已在运行"
            return
        if not FAKE_PROXY_SCRIPT_PATH.exists():
            self.ok = False
            self.message = f"脚本不存在：{FAKE_PROXY_SCRIPT_PATH}"
            return

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            FAKE_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 子进程继承句柄后，父进程 with 退出即可释放自己的副本
            with open(str(FAKE_PROXY_LOG_PATH), "a", encoding="utf-8") as log_file:
                proc = subprocess.Popen(
                    [resolve_launch_python(), str(FAKE_PROXY_SCRIPT_PATH)],
                    cwd=str(PROJECT_ROOT),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **kwargs,
                )
        except Exception as e:
            self.ok = False
            self.message = f"启动进程失败：{e}"
            return

        deadline = time.time() + FAKE_PROXY_STARTUP_TIMEOUT_S
        while time.time() < deadline:
            if proc.poll() is not None:
                # 进程秒退：端口被占 / 依赖缺失都走这里，日志尾部带回真实原因
                self.ok = False
                self.message = f"进程退出（code={proc.returncode}）：{self._tail_log()}"
                return
            if self._http.fake_proxy_is_reachable():
                self.stats = self._http.fake_proxy_stats()
                self.ok = self.stats is not None
                self.message = f"Fake Proxy 已启动（pid={proc.pid}）" if self.ok else "进程已起但 /api/stats 无响应"
                return
            time.sleep(0.4)
        self.ok = False
        self.message = f"启动超时（{FAKE_PROXY_STARTUP_TIMEOUT_S:.0f}s）：{self._tail_log()}"

    def _run_stop(self) -> None:
        if not self._http.fake_proxy_is_reachable():
            self.ok = True
            self.message = "Fake Proxy 未在运行"
            return
        if not self._http.fake_proxy_shutdown():
            self.ok = False
            self.message = "停止请求失败"
            return
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not self._http.fake_proxy_is_reachable():
                self.ok = True
                self.message = "Fake Proxy 已停止"
                return
            time.sleep(0.3)
        self.ok = False
        self.message = "停止超时：9999 端口仍被占用"

    def _tail_log(self, n: int = 3) -> str:
        try:
            lines = FAKE_PROXY_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [ln for ln in lines[-n:] if ln.strip()]
            return " | ".join(tail) if tail else "无日志输出"
        except Exception:
            return "日志不可读"


class DashboardPanel(PanelBase):
    PANEL_META = PanelMeta(
        id="dashboard",
        title="概览",
        icon="home",
        order=10,
        category="main",
        requires_backend=False,  # 离线时仍显示卡片+重连按钮
    )

    switch_panel_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._cached_health: dict | None = None
        self._cached_token_stats: dict | None = None
        self._cached_config: dict | None = None
        self._refresh_btn = None
        self._start_backend_btn = None
        self._start_fake_btn = None
        self._stop_fake_btn = None
        self._refresh_thread = None
        self._fake_op_thread: _FakeProxyOpThread | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        # 顶部标题行
        header_row = QHBoxLayout()
        title = QLabel("概览")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 4 个状态卡片横排
        cards_layout = QGridLayout()
        cards_layout.setSpacing(10)
        self._backend_card = StatusCard(
            title="后端服务",
            subtitle="port 8766",
            status="unknown",
        )
        self._fake_card = StatusCard(
            title="Fake LLM Proxy",
            subtitle="port 9999",
            status="unknown",
        )
        self._llm_pool_card = StatusCard(
            title="LLM 池",
            subtitle="",
            status="unknown",
        )
        self._todos_card = StatusCard(
            title="到期任务",
            subtitle="",
            status="unknown",
        )
        cards_layout.addWidget(self._backend_card, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        cards_layout.addWidget(self._fake_card, 0, 1, alignment=Qt.AlignmentFlag.AlignTop)
        cards_layout.addWidget(self._llm_pool_card, 0, 2, alignment=Qt.AlignmentFlag.AlignTop)
        cards_layout.addWidget(self._todos_card, 0, 3, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(cards_layout)

        # 快捷操作区
        ops_label = QLabel("快捷操作")
        set_text_role(ops_label, "title")
        ops_label.setStyleSheet("margin-top: 8px;")
        layout.addWidget(ops_label)

        ops_row = QHBoxLayout()
        ops_row.setSpacing(8)

        start_backend_btn = QPushButton("启动后端")
        start_backend_btn.clicked.connect(self._start_backend)
        ops_row.addWidget(start_backend_btn)
        self._start_backend_btn = start_backend_btn

        start_fake_btn = QPushButton("启动 Fake Proxy")
        start_fake_btn.clicked.connect(self._start_fake_proxy)
        ops_row.addWidget(start_fake_btn)
        self._start_fake_btn = start_fake_btn

        stop_fake_btn = QPushButton("停止 Fake Proxy")
        stop_fake_btn.clicked.connect(self._stop_fake_proxy)
        ops_row.addWidget(stop_fake_btn)
        self._stop_fake_btn = stop_fake_btn

        ops_row.addStretch()
        layout.addLayout(ops_row)

        # 当前数据
        activity_label = QLabel("当前数据")
        set_text_role(activity_label, "title")
        activity_label.setStyleSheet("margin-top: 8px;")
        layout.addWidget(activity_label)

        self._activity_list = QListWidget()
        self._activity_list.setObjectName("dashboardActivityList")
        self._activity_list.setMinimumHeight(220)
        layout.addWidget(self._activity_list, 1)

        # 初始空状态
        self._backend_card.set_details({"提示": "等待健康检查..."})
        self._fake_card.set_details({"提示": "等待探测..."})
        self._llm_pool_card.set_details({"提示": "等待数据..."})
        self._todos_card.set_details({"提示": "等待数据..."})

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        # 先用缓存渲染（如果有），避免白屏
        if self._cached_health:
            self._backend_card.set_status(True)
            self._update_from_health(self._cached_health)
        # 异步刷新
        self._refresh_async()

    def on_refresh(self) -> None:
        self._refresh_async()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        """后端状态变化时只更新后端卡片，其他卡片由各自的数据信号驱动"""
        self._backend_card.set_status(online)
        if self._start_backend_btn:
            self._start_backend_btn.setText("重启后端" if online else "启动后端")
        if online:
            self._backend_card.set_details({"提示": "等待健康数据..."})
            self._llm_pool_card.set_status_value("unknown")
            self._llm_pool_card.set_details({"提示": "等待数据..."})
            self._todos_card.set_status_value("unknown")
            self._todos_card.set_details({"提示": "等待数据..."})
        else:
            self._cached_health = None
            self._cached_token_stats = None
            self._llm_pool_card.set_status_value("unknown")
            self._llm_pool_card.set_details({"提示": "后端不可达"})
            self._todos_card.set_status_value("unknown")
            self._todos_card.set_details({"提示": "后端不可达"})

    @Slot(dict)
    def on_health_changed(self, health: dict) -> None:
        """ServiceManager backend_health_changed 信号的 slot"""
        if not health:
            return
        self._cached_health = health
        self._update_from_health(health)

    def _update_from_health(self, health: dict) -> None:
        """从 health 数据更新后端卡片 + LLM 池卡片（不发额外 HTTP）"""
        version = health.get("version", "?")
        uptime = health.get("uptime_seconds", 0)
        uptime_str = format_uptime(uptime)
        self._backend_card.set_details({
            "version": version,
            "uptime": uptime_str,
        })
        llm_pool = health.get("llm_pool", {})
        if isinstance(llm_pool, dict):
            total = llm_pool.get("total_keys", llm_pool.get("key_count", 0))
            available = llm_pool.get("available_keys", llm_pool.get("active_keys", llm_pool.get("available", 0)))
            # v10：模型总数 + 免费模型数
            total_models = llm_pool.get("total_models", 0)
            free_models = llm_pool.get("free_models", 0)
            recent_calls_count = llm_pool.get("recent_calls_count", 0)
            if available > 0:
                self._llm_pool_card.set_status_value("online")
            else:
                self._llm_pool_card.set_status_value("warning")
            self._llm_pool_card.set_details({
                "密钥": f"{available}/{total} 可用",
                "模型": f"{total_models}（{free_models} 免费）",
                "调用": recent_calls_count,
            })

    @Slot(list)
    def on_todos_changed(self, todos: list) -> None:
        """ServiceManager todos_due_changed 信号的 slot"""
        count = len(todos) if todos else 0
        self._todos_card.set_status_value("online")
        self._todos_card.set_details({"到期": f"{count} 个"})

    @Slot(object)
    def on_fake_proxy_changed(self, stats) -> None:
        """ServiceManager fake_proxy_status_changed 信号的 slot"""
        self._fake_card.set_status(stats is not None)
        if stats:
            self._fake_card.set_details(_fake_stats_details(stats))
        else:
            self._fake_card.set_details({"提示": "未启动"})

    def _on_refresh_clicked(self) -> None:
        self._refresh_async()

    def _refresh_async(self) -> None:
        """异步刷新所有数据（不阻塞 UI，单次 /health 调用）"""
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        import datetime

        from PySide6.QtCore import QThread

        class RefreshThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http
                self.health = None
                self.mcp_stats = None
                self.daily = None
                self.fake_stats = None
                self.token_stats = None
                self.config = None

            def run(self):
                self.health = self._http.get("/health")
                if self.health:
                    self.mcp_stats = self._http.get("/mcp/stats")
                    self.token_stats = self._http.get("/llm/pool/stats")
                    self.config = self._http.get("/config")
                    today = datetime.date.today().strftime("%Y%m%d")
                    self.daily = self._http.get(f"/activity/daily/{today}")
                self.fake_stats = self._http.fake_proxy_stats()

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        assert t is not None  # 契约：finished 信号必在 start() 后发出，_refresh_thread 已赋值
        if t.health is None:
            self._backend_card.set_status(False)
            self._backend_card.set_details({"提示": "后端未启动"})
            self._fake_card.set_status(t.fake_stats is not None)
            if t.fake_stats:
                self._fake_card.set_details(_fake_stats_details(t.fake_stats))
            else:
                self._fake_card.set_details({"提示": "未启动"})
            return
        self._cached_health = t.health
        self._cached_token_stats = t.token_stats
        self._cached_config = t.config
        self._backend_card.set_status(True)
        self._update_from_health(t.health)
        # 数据列表（从 health + 额外数据源聚合）
        self._activity_list.clear()
        h = t.health

        # —— 后端运行状态 ——
        version = h.get("version", "?")
        uptime = format_uptime(h.get("uptime_seconds", 0))
        self._activity_list.addItem(
            _stat_item("monitor", f"后端 {version} 运行 {uptime}")
        )

        # —— 审批强度（来自 /config.command_guard.approval_level）——
        if t.config and isinstance(t.config, dict):
            # /config 响应带 {"config": {...}} 包装层，需先解包（与 settings 面板一致）
            cfg = t.config.get("config")
            cfg = cfg if isinstance(cfg, dict) else t.config
            cmd_guard = cfg.get("command_guard", {}) or {}
            approval_level = cmd_guard.get("approval_level", "—")
            level_zh = {
                "strict": "严格（strict）",
                "moderate": "中等（moderate）",
                "loose": "宽松（loose）",
                "none": "关闭（none）",
            }.get(approval_level, approval_level)
            self._activity_list.addItem(
                _stat_item("shield", f"审批强度：{level_zh}")
            )

        # —— MCP 调用统计 ——
        if t.mcp_stats:
            total = t.mcp_stats.get("total_calls", t.mcp_stats.get("total", 0))
            tools = t.mcp_stats.get("unique_tools", t.mcp_stats.get("tools_count", 0))
            self._activity_list.addItem(
                _stat_item("plug", f"MCP 调用：{total} 次 / {tools} 个工具")
            )

        # —— LLM 池状态（v10：含免费/paid 分布 + 模型数 + 近期调用）——
        llm_pool = h.get("llm_pool", {}) or {}
        if isinstance(llm_pool, dict):
            total_keys = llm_pool.get("total_keys", llm_pool.get("key_count", 0))
            avail_keys = llm_pool.get("active_keys", llm_pool.get("available_keys", llm_pool.get("available", 0)))
            cooldown = llm_pool.get("cooldown_keys", llm_pool.get("cooldown", 0))
            free_keys = llm_pool.get("free_keys", 0)
            paid_keys = llm_pool.get("paid_keys", 0)
            total_models = llm_pool.get("total_models", 0)
            free_models = llm_pool.get("free_models", 0)
            recent_calls_count = llm_pool.get("recent_calls_count", 0)
            self._activity_list.addItem(
                _stat_item(
                    "bot",
                    f"LLM 池：{avail_keys}/{total_keys} 可用 / {cooldown} 冷却 "
                    f"（免费 {free_keys}+Paid {paid_keys}）",
                )
            )
            self._activity_list.addItem(
                _stat_item(
                    "package",
                    f"模型：{total_models} 总数 / {free_models} 免费 / 近期调用 {recent_calls_count}",
                )
            )
            # token 消耗（仅展示统计，不算钱）
            if t.token_stats and isinstance(t.token_stats, dict):
                projects = t.token_stats.get("projects", {})
                if projects:
                    total_calls = sum(p.get("calls", 0) for p in projects.values())
                    total_input = sum(p.get("prompt_tokens", 0) for p in projects.values())
                    total_output = sum(p.get("completion_tokens", 0) for p in projects.values())
                    self._activity_list.addItem(
                        _stat_item(
                            "bar-chart",
                            f"Token：{format_tokens(total_input)} 输入 / "
                            f"{format_tokens(total_output)} 输出 / {total_calls} 次调用",
                        )
                    )

        # —— Loop 任务 ——
        loops = h.get("loops", {}) or {}
        if isinstance(loops, dict) and not loops.get("error"):
            task_count = loops.get("task_count", 0)
            active = loops.get("active_count", 0)
            paused = loops.get("paused_count", 0)
            if task_count > 0 or active > 0 or paused > 0:
                self._activity_list.addItem(
                    _stat_item("refresh", f"Loop：{active} 活跃 / {paused} 暂停 / {task_count} 总数")
                )

        # —— 待办/收件箱/WIP ——
        todos = h.get("todos", {}) or {}
        if isinstance(todos, dict) and todos.get("available", False):
            due_count = todos.get("todos_due", 0)
            wip_total = todos.get("wip_total", 0)
            wip_active = todos.get("wip_active", 0)
            if due_count > 0:
                self._activity_list.addItem(
                    _stat_item("clock", f"到期任务：{due_count} 个待处理")
                )
            if wip_active > 0:
                self._activity_list.addItem(
                    _stat_item("wrench", f"WIP 任务：{wip_active} 活跃 / {wip_total} 总数")
                )

        inbox = h.get("inbox", {}) or {}
        if isinstance(inbox, dict) and inbox.get("available", False):
            inbox_pending = inbox.get("pending", 0)
            if inbox_pending and inbox_pending > 0:
                self._activity_list.addItem(
                    _stat_item("inbox", f"收件箱：{inbox_pending} 条待处理")
                )

        # —— 终端会话 ——
        exec_data = h.get("exec", {}) or {}
        if isinstance(exec_data, dict):
            term_running = exec_data.get("terminals_running", 0)
            term_total = exec_data.get("terminals_total", 0)
            if term_total > 0:
                self._activity_list.addItem(
                    _stat_item("monitor", f"终端：{term_running} 运行 / {term_total} 总数")
                )

        # —— 浏览器 ——
        browser = h.get("browser", {}) or {}
        if isinstance(browser, dict) and browser.get("connected", False):
            tab_count = browser.get("tab_count", browser.get("tabs_count", 0))
            self._activity_list.addItem(
                _stat_item("app-window", f"浏览器：已连接 / {tab_count} 个标签页")
            )

        # —— 记忆系统 ——
        memory = h.get("memory", {}) or {}
        if isinstance(memory, dict) and memory.get("available", False):
            msgs = memory.get("message_count", memory.get("messages", 0))
            facts = memory.get("fact_count", memory.get("facts", 0))
            summaries = memory.get("summary_count", memory.get("summaries", 0))
            self._activity_list.addItem(
                _stat_item("brain", f"记忆：{msgs} 消息 / {facts} 事实 / {summaries} 摘要")
            )

        # —— 用户补充指令 ——
        user_msg = h.get("user_message", {}) or {}
        if isinstance(user_msg, dict):
            pending_msgs = user_msg.get("pending_count", user_msg.get("pending", 0))
            if pending_msgs and pending_msgs > 0:
                self._activity_list.addItem(
                    _stat_item("message-square", f"用户补充指令：{pending_msgs} 条待发送")
                )

        # —— 防休眠 ——
        system = h.get("system", {}) or {}
        if isinstance(system, dict):
            ka_enabled = system.get("enabled", system.get("keep_awake_enabled", False))
            if ka_enabled:
                ka_display = system.get("keep_display_on", system.get("keep_awake_keep_display_on", False))
                ka_reason = system.get("reason", system.get("keep_awake_reason", ""))
                mode = "系统+显示器" if ka_display else "仅系统"
                reason_text = f"（{ka_reason}）" if ka_reason else ""
                self._activity_list.addItem(
                    _stat_item("settings", f"防休眠：已开启 [{mode}]{reason_text}")
                )

        # —— Fake Proxy ——
        if t.fake_stats:
            fp_total = t.fake_stats.get("total_requests", t.fake_stats.get("request_count", 0))
            self._activity_list.addItem(
                _stat_item("bot", f"Fake Proxy：在线 / {fp_total} 次请求")
            )

        # —— 今日工作总结 ——
        if t.daily and t.daily.get("content"):
            preview = t.daily["content"][:80].replace("\n", " ")
            self._activity_list.addItem(
                _stat_item("calendar", f"今日工作总结：{preview}...")
            )

        # Fake Proxy 卡片
        self._fake_card.set_status(t.fake_stats is not None)
        if t.fake_stats:
            self._fake_card.set_details(_fake_stats_details(t.fake_stats))
        else:
            self._fake_card.set_details({"提示": "未启动"})

    # —— 快捷操作 ——

    def _start_backend(self) -> None:
        try:
            subprocess.Popen(
                ["cmd", "/c", "start.bat"],
                cwd=str(START_BAT_PATH.parent),
                shell=False,
            )
            self._activity_list.insertItem(0, QListWidgetItem("▶ 启动后端：已发送 start.bat"))
        except Exception as e:
            self._activity_list.insertItem(0, QListWidgetItem(f"✗ 启动后端失败：{e}"))

    def _start_fake_proxy(self) -> None:
        self._run_fake_op("start")

    def _stop_fake_proxy(self) -> None:
        self._run_fake_op("stop")

    def _run_fake_op(self, op: str) -> None:
        """启停 Fake Proxy：Popen + 就绪等待都在后台线程，UI 只显示最终结果"""
        if self._fake_op_thread is not None and self._fake_op_thread.isRunning():
            return
        self._set_fake_buttons_enabled(False)
        self._activity_list.insertItem(
            0, QListWidgetItem("⏳ Fake Proxy 启动中..." if op == "start" else "⏳ Fake Proxy 停止中...")
        )
        self._fake_op_thread = _FakeProxyOpThread(self._http, op)
        self._fake_op_thread.finished.connect(self._on_fake_op_done)
        self._fake_op_thread.start()

    @Slot()
    def _on_fake_op_done(self) -> None:
        self._set_fake_buttons_enabled(True)
        t = self._fake_op_thread
        if t is None:
            return
        mark = "✓" if t.ok else "✗"
        self._activity_list.insertItem(0, QListWidgetItem(f"{mark} {t.message}"))
        self._fake_card.set_status(t.stats is not None)
        if t.stats:
            self._fake_card.set_details(_fake_stats_details(t.stats))
        else:
            self._fake_card.set_details({"提示": "未启动"})

    def _set_fake_buttons_enabled(self, enabled: bool) -> None:
        for btn in (self._start_fake_btn, self._stop_fake_btn):
            if btn is not None:
                btn.setEnabled(enabled)
