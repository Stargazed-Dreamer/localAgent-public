"""主窗口 + 侧栏导航

启动流程：
1. PanelRegistry.discover() 扫描 client/panels/ 下所有 PanelBase 子类
2. 按 category 分组构建左侧导航
3. 每个面板一个 QStackedWidget 页面
4. 启动 ServiceManager 周期轮询后端状态

切换面板时调旧面板 on_hide() + 新面板 on_show()，并持久化最近面板 ID。
后端状态变化通过 ServiceManager.backend_status_changed 广播到所有面板。
"""


from typing import cast

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase
from client.core.panel_registry import PanelRegistry
from client.core.services import ServiceManager
from client.core.state import AppState
from client.panels.chat import ChatPanel
from client.panels.dashboard import DashboardPanel
from client.panels.llm_pool import ModelPoolPanel
from client.panels.monitoring import MonitoringPanel
from client.panels.tasks import TasksPanel
from lib.ui import icons as _icons
from lib.ui import tokens as _T
from lib.ui.theme import set_text_role


class _SidebarItemWidget(QWidget):
    """侧边栏面板项 widget：左 icon+title，右纯数字（右对齐）。

    icon 参数语义（T4 起向后兼容）：
    - 优先按 SVG 图标名渲染（lib/ui/resources/icons/<name>.svg）
    - 名字未命中则按 emoji 文本显示（旧面板过渡期 / 第三方面板回退）
    """

    def __init__(self, icon: str, title: str, parent=None):
        super().__init__(parent)
        # UIA 可达性：自定义行 widget 在无障碍树里默认无名（row 9/10...），
        # 显式给出面板名（C14 最小集）
        self.setAccessibleName(title)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(8)
        self._icon_label = QLabel()
        self._icon_label.setFixedWidth(22)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            # 优先 SVG 图标名渲染（T4：emoji 全部迁移到 lib/ui/icons）
            pm = _icons.icon_pixmap(icon, _T.ICON_DEFAULT, 18)
            self._icon_label.setPixmap(pm)
        except KeyError:
            # 未命中 SVG name → 当 emoji 文本显示（向后兼容）
            self._icon_label.setText(icon)
        layout.addWidget(self._icon_label)
        self._label = QLabel(title)
        self._label.setAccessibleName(title)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label)
        layout.addStretch()
        self._count_label = QLabel("")
        set_text_role(self._count_label, "warning")
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._count_label)
        self.setMinimumHeight(40)

    def set_count(self, count: int) -> None:
        self._count_label.setText(str(count) if count > 0 else "")


class _SidebarCategoryWidget(QWidget):
    """Square, visually distinct category heading for the sidebar."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setAccessibleName(title)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        label = QLabel(title)
        label.setAccessibleName(title)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        set_text_role(label, "title")
        layout.addWidget(label)


class Sidebar(QListWidget):
    """左侧导航：按 category 分组显示面板列表"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(180)
        self.setObjectName("sidebar")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 按分类分组的项
        self._panel_indexes: dict[str, int] = {}  # panel_id -> row index
        self._panel_base_texts: dict[str, str] = {}  # panel_id -> base text (icon + title)
        self._panel_badges: dict[str, int] = {}  # panel_id -> badge count
        self._panel_widgets: dict[str, _SidebarItemWidget] = {}  # panel_id -> widget

    def add_category_header(self, label: str) -> None:
        item = QListWidgetItem()
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
        item.setData(Qt.ItemDataRole.UserRole, "__header__")
        item.setData(Qt.ItemDataRole.AccessibleTextRole, label)
        item.setSizeHint(QSize(0, 32 if label else 12))
        self.addItem(item)
        if label:
            self.setItemWidget(item, _SidebarCategoryWidget(label))

    def add_panel_item(self, panel_id: str, icon: str, title: str) -> int:
        base_text = f"{icon}  {title}"
        self._panel_base_texts[panel_id] = base_text
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, panel_id)
        # AccessibleTextRole：只喂无障碍树、不参与界面绘制——
        # 解决"item.setText 有名但文字透出行 widget 成重影 / 不设则 UIA 无名"的互斥
        item.setData(Qt.ItemDataRole.AccessibleTextRole, title)
        item.setSizeHint(QSize(0, 40))
        self.addItem(item)
        row = self.count() - 1
        self._panel_indexes[panel_id] = row
        widget = _SidebarItemWidget(icon, title)
        self.setItemWidget(item, widget)
        self._panel_widgets[panel_id] = widget
        return row

    def select_panel(self, panel_id: str) -> bool:
        row = self._panel_indexes.get(panel_id)
        if row is None:
            return False
        self.setCurrentRow(row)
        return True

    def update_badge(self, panel_id: str, count: int) -> None:
        """更新面板的数字徽章（纯数字右对齐，无红点）"""
        self._panel_badges[panel_id] = count
        widget = self._panel_widgets.get(panel_id)
        if widget is not None:
            widget.set_count(count)


class MainWindow(QMainWindow):
    """客户端主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LocalAgent Client")
        self.resize(1200, 760)
        self.setMinimumSize(900, 600)

        # 1. 发现所有面板
        PanelRegistry.discover()
        panel_classes = PanelRegistry.get_all_sorted()

        # 2. 创建实例 + 侧栏 + stack
        self.sidebar = Sidebar(self)
        self.stack = QStackedWidget(self)
        self.panels: dict[str, PanelBase] = {}
        self._panel_order: list[str] = []  # panel_id 列表，与 stack index 对应

        current_category = None
        for cls in panel_classes:
            meta = cls.meta()
            if meta.category != current_category:
                if current_category is not None:
                    # 分组间分隔
                    self.sidebar.add_category_header("")
                category_label = self._category_label(meta.category)
                self.sidebar.add_category_header(category_label)
                current_category = meta.category
            self.sidebar.add_panel_item(meta.id, meta.icon, meta.title)

            panel = cls(self)
            self.panels[meta.id] = panel
            self.stack.addWidget(panel)
            self._panel_order.append(meta.id)

        # 空状态
        if not self._panel_order:
            placeholder = QLabel("无可用面板\n请在 client/panels/ 下添加面板")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            set_text_role(placeholder, "tertiary")
            self.stack.addWidget(placeholder)

        # 3. 布局
        central = QWidget(self)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.sidebar)

        # 右侧：降级横幅 + 面板 stack
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self._backend_banner = QLabel()
        self._backend_banner.setObjectName("WarningBanner")
        self._backend_banner.setVisible(False)
        self._backend_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self._backend_banner)
        right_layout.addWidget(self.stack, 1)
        main_layout.addWidget(right_container, 1)
        self.setCentralWidget(central)

        # 4. 信号
        self.sidebar.currentItemChanged.connect(self._on_sidebar_changed)

        # 5. 启动后台服务
        self.services = ServiceManager(self)
        self.services.backend_status_changed.connect(self._on_backend_status)
        self.services.backend_degraded.connect(self._on_backend_degraded)
        self.services.start()

        # 6. 连接数据信号到需要它们的 panel
        # 初始化不变量：这些 panel_id 对应已注册面板，__init__ 时已实例化
        # （cast 收敛类型，让 pyright 能验证具体面板的信号方法）
        dashboard = cast(DashboardPanel, self.panels.get("dashboard"))
        if dashboard is not None:
            self.services.backend_health_changed.connect(dashboard.on_health_changed)
            self.services.todos_due_changed.connect(dashboard.on_todos_changed)
            self.services.fake_proxy_status_changed.connect(dashboard.on_fake_proxy_changed)

        monitoring = cast(MonitoringPanel, self.panels.get("monitoring"))
        if monitoring is not None:
            self.services.backend_health_changed.connect(monitoring.on_health_changed)

        # LLM 池监控面板：接收 health 变化信号以更新摘要栏
        llm_pool_panel = cast(ModelPoolPanel, self.panels.get("llm_pool"))
        if llm_pool_panel is not None:
            self.services.backend_health_changed.connect(llm_pool_panel.on_health_changed)

        # 待办总览面板：接收到期任务信号 + 切换子面板
        tasks = cast(TasksPanel, self.panels.get("tasks"))
        if tasks is not None:
            self.services.todos_due_changed.connect(tasks.on_todos_changed)
            tasks.switch_panel_requested.connect(self.switch_to_panel)

        # 到期任务面板：接收到期任务信号以自动刷新 badge
        due_todos = self.panels.get("due_todos")
        if due_todos is not None:
            self.services.todos_due_changed.connect(
                lambda todos: due_todos.on_show() if due_todos.isVisible() else None
            )

        # 7. 启动 badge 轮询（待办红点）
        from PySide6.QtCore import QTimer
        self._badge_timer = QTimer(self)
        self._badge_timer.setInterval(30000)  # 30s
        self._badge_timer.timeout.connect(self._refresh_badges)
        self._badge_timer.start()
        # 首次延迟刷新
        QTimer.singleShot(3000, self._refresh_badges)

        # 8. 恢复窗口几何 + 最近面板
        self._restore_state()

    def _category_label(self, category: str) -> str:
        return {
            "main": "主面板",
            "monitor": "监控",
            "advanced": "高级",
        }.get(category, category)

    def _restore_state(self) -> None:
        state = AppState.instance()
        geo = state.load_window_geometry()
        if not geo.isEmpty():
            self.restoreGeometry(geo)
        else:
            size = state.load_window_size()
            if size is not None:
                self.resize(size)
        last_panel = state.load_last_panel()
        if last_panel and last_panel in self.panels:
            self.switch_to_panel(last_panel)
        elif self._panel_order:
            self.switch_to_panel(self._panel_order[0])

    def switch_to_panel(self, panel_id: str) -> bool:
        """切换到指定面板"""
        if panel_id not in self.panels:
            return False
        # 调旧面板 on_hide
        current_widget = self.stack.currentWidget()
        if current_widget is not None and isinstance(current_widget, PanelBase):
            current_widget.on_hide()
        # 切换
        idx = self._panel_order.index(panel_id)
        self.stack.setCurrentIndex(idx)
        # 阻断信号避免循环触发
        self.sidebar.blockSignals(True)
        self.sidebar.select_panel(panel_id)
        self.sidebar.blockSignals(False)
        # 调新面板 on_show
        self.panels[panel_id].on_show()
        # 持久化
        AppState.instance().save_last_panel(panel_id)
        return True

    def _on_sidebar_changed(self, current: QListWidgetItem, _previous: QListWidgetItem) -> None:
        if current is None:
            return
        panel_id = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(panel_id, str) or panel_id == "__header__":
            return
        self.switch_to_panel(panel_id)

    def _on_backend_status(self, online: bool) -> None:
        for panel in self.panels.values():
            try:
                panel.on_backend_status_change(online)
            except Exception as e:
                print(f"[MainWindow] panel.on_backend_status_change 出错: {e}")

    def _on_backend_degraded(self, degraded: bool) -> None:
        """单次失败降级：显示橙色横幅，不切占位视图；恢复则隐藏横幅"""
        if degraded:
            self._backend_banner.setText("⚠ 后端响应异常，正在重试…")
            self._backend_banner.setVisible(True)
        else:
            self._backend_banner.setVisible(False)

    def _refresh_badges(self) -> None:
        """异步刷新 sidebar 数字徽章（待办/收件箱/到期任务/WIP/终端/Loop/日总结）"""
        from PySide6.QtCore import QThread

        # 后端离线时跳过
        http = HttpClient.instance()
        if not http.is_reachable():
            return

        # 8-4: 上一轮 BadgeThread 仍在跑（6 个串行请求各 8s 超时，最长 ~48s > 30s 周期）
        # 时跳过本轮：直接覆盖 self._badge_thread 会让运行中的 QThread 失去引用被 GC
        # （"QThread: Destroyed while thread is still running" 硬崩），且旧线程 finished
        # 晚到会读到新线程写到一半的计数（同 due_todos._refresh_async 的 isRunning 模式）
        if getattr(self, "_badge_thread", None) is not None and self._badge_thread.isRunning():
            return

        class BadgeThread(QThread):
            def __init__(self, h):
                super().__init__()
                self._http = h
                self.inbox_count = 0
                self.due_count = 0
                self.wip_count = 0
                self.terminal_count = 0
                self.loop_count = 0
                self.daily_pending = 0
                self.memory_data = None

            def run(self):
                # 收件箱待处理数
                resp = self._http.get("/inbox", params={"status": "pending", "limit": 1000})
                if resp and isinstance(resp, dict):
                    items = resp.get("items", []) or []
                    self.inbox_count = len(items)
                # 到期任务数
                resp = self._http.get("/todos/due")
                if resp and isinstance(resp, dict):
                    due = resp.get("due", []) or []
                    self.due_count = len(due)
                # WIP 任务数
                resp = self._http.get("/wip", params={"summary": "true"})
                if resp and isinstance(resp, dict):
                    tasks = resp.get("tasks", []) or []
                    self.wip_count = len(tasks)
                # 终端数
                resp = self._http.get("/terminals")
                if resp and isinstance(resp, dict):
                    self.terminal_count = resp.get("count", 0)
                # Loop 任务数
                resp = self._http.get("/loop/tasks")
                if resp and isinstance(resp, dict):
                    self.loop_count = resp.get("task_count", 0)
                # 日总结待审核数
                resp = self._http.get("/activity/daily")
                if resp and isinstance(resp, dict):
                    files = resp.get("files", []) or []
                    self.daily_pending = sum(
                        1 for f in files if not f.get("reviewed", False)
                    )
                # 记忆系统 stale 记忆数（用于记忆面板红点）
                self.memory_data = self._http.get("/memory/status")

        self._badge_thread = BadgeThread(http)
        self._badge_thread.finished.connect(self._on_badge_done)
        self._badge_thread.start()

    def _on_badge_done(self) -> None:
        t = getattr(self, "_badge_thread", None)
        if t is None:
            return
        inbox_count = t.inbox_count
        due_count = t.due_count
        total = inbox_count + due_count

        # 待办/收件箱/到期任务（原有）
        self.sidebar.update_badge("tasks", total)
        self.sidebar.update_badge("inbox", inbox_count)
        self.sidebar.update_badge("due_todos", due_count)

        # 新增面板数字
        self.sidebar.update_badge("wip_tasks", t.wip_count)
        self.sidebar.update_badge("terminal", t.terminal_count)
        self.sidebar.update_badge("loop", t.loop_count)
        self.sidebar.update_badge("daily_summary", t.daily_pending)

        # 记忆面板：stale 记忆数（红点提示需维护）
        memory_stale = 0
        if t.memory_data and isinstance(t.memory_data, dict):
            maint = t.memory_data.get("maintainer", {}) or {}
            memory_stale = maint.get("stale_total", 0) or 0
        self.sidebar.update_badge("memory", memory_stale)

        # 同步到 tasks 面板的子入口卡片
        tasks = cast(TasksPanel, self.panels.get("tasks"))
        if tasks is not None:
            tasks._inbox_count = inbox_count
            tasks._due_count = due_count
            tasks._inbox_card.set_count(inbox_count)
            tasks._inbox_card.set_subtitle(f"待处理 {inbox_count} 条")
            tasks._due_card.set_count(due_count)
            tasks._due_card.set_subtitle(f"到期 {due_count} 个")

    def closeEvent(self, event):
        # D24: 防关机丢会话 — 中断活跃 runner + 更新 session.status=interrupted（毫秒级，非阻塞）
        # auto_shutdown 关机时 OS 自动触发 closeEvent（WM_QUERYENDSESSION），无需额外 hook
        try:
            chat_panel = cast(ChatPanel, self.panels.get("chat"))
            if chat_panel is not None and hasattr(chat_panel, "interrupt_active_session"):
                chat_panel.interrupt_active_session()
        except Exception:
            pass
        # 8-4: 停 badge 轮询并限时等待在飞线程退出，避免退出时运行中 QThread 被销毁
        try:
            self._badge_timer.stop()
            _badge_t = getattr(self, "_badge_thread", None)
            if _badge_t is not None and _badge_t.isRunning():
                _badge_t.wait(2000)
        except Exception:
            pass
        # 阶段 5 会改为最小化到托盘；本期直接退出
        try:
            self.services.stop()
            state = AppState.instance()
            state.save_window_geometry(self.saveGeometry())
            state.save_window_size(self.size())
        except Exception:
            pass
        super().closeEvent(event)
