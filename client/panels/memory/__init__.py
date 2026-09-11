"""记忆面板包 — 左侧 nav + 右侧 QStackedWidget

Ticket 03：包骨架 + 6 占位页（概览 / 记忆库 / 时间线 / 检索追踪 / 证据账本 / 写入记忆）。

PANEL_META 与原 client/panels/memory.py 单文件保持一致，注册不变：
    PanelRegistry.discover() 扫描 client/panels/ 时发现此包，按 __init__.py
    中直接定义的 MemoryPanel 类注册（id=memory, title=记忆, icon=brain, order=42）。

注：MemoryPanel 必须定义在 __init__.py 内（不能从 _shell.py 再导入），
否则 PanelRegistry 的 `attr.__module__ == module.__name__` 校验会失败。

后续 Ticket 04-10 在此骨架上接入真实功能：
- 04：_shared.py 共享组件（TableWithDetail / FilterBar / ActionButtonsBar）
- 05：概览页接入 health_score 算法 + GET /memory/status
- 06：记忆库页接入 GET /memory/list + search + delete
- 07：时间线页接入 GET /memory/timeline
- 08：检索追踪页接入 GET /memory/search/traces
- 09：证据账本页接入 GET /memory/evidence
- 10：写入记忆页接入 POST /memory/{key}
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.core.panel_base import PanelBase, PanelMeta
from client.panels.memory.evidence_page import EvidencePage
from client.panels.memory.memory_list_page import MemoryListPage
from client.panels.memory.overview_page import OverviewPage
from client.panels.memory.search_traces_page import SearchTracesPage
from client.panels.memory.timeline_page import TimelinePage
from client.panels.memory.write_page import WritePage
from lib.ui import icon, tokens
from lib.ui.theme import set_text_role


class MemoryPanel(PanelBase):
    """记忆面板 — 左侧 nav + 右侧 QStackedWidget"""

    PANEL_META = PanelMeta(
        id="memory",
        title="记忆",
        icon="brain",
        order=42,            # monitor 类，紧挨 terminal(41)
        category="monitor",
        requires_backend=True,
    )

    # jump_to 信号 page_name → nav 行号映射
    _JUMP_PAGE_TO_ROW = {
        "overview": 0,
        "memory_list": 1,
        "timeline": 2,
        "search_traces": 3,
        "evidence": 4,
        "write": 5,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nav: QListWidget | None = None
        self._pages: QStackedWidget | None = None
        self._overview_page: OverviewPage | None = None
        # 占位页引用（Ticket 03 仅占位，后续 ticket 接入真实功能）
        self._memory_list_page: MemoryListPage | None = None
        self._timeline_page: TimelinePage | None = None
        self._search_traces_page: SearchTracesPage | None = None
        self._evidence_page: EvidencePage | None = None
        self._write_page: WritePage | None = None
        # 离线 / 正常 widget 引用
        self._normal_widget: QWidget | None = None
        self._offline_widget: QWidget | None = None
        self._stack: QStackedWidget | None = None

        # 60s 概览自动刷新定时器（仅概览页生效，遵守 ADR-0022 避免并发触发）
        self._timer = QTimer(self)
        self._timer.setInterval(60000)
        self._timer.timeout.connect(self._on_timer_timeout)

        self._build_ui()

        # 接入概览页 jump_to 信号 → 切 nav + 应用筛选
        if self._overview_page is not None:
            self._overview_page.jump_to.connect(self._on_jump_to)
        # 接入记忆库页 jump_to_write_page 信号 → 切写入页 + 预填（Ticket 10 实现预填入口）
        if self._memory_list_page is not None:
            self._memory_list_page.jump_to_write_page.connect(self._on_jump_to_write_page)

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
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # —— 左侧 nav ——
        self._nav = QListWidget()
        self._nav.setObjectName("sidebar")
        self._nav.setFixedWidth(160)
        self._nav.setUniformItemSizes(True)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self._nav)

        # —— 右侧 QStackedWidget ——
        self._pages = QStackedWidget()
        layout.addWidget(self._pages, 1)

        # 6 个 nav 条目：标题 / 图标 / 页 widget
        # 注：lib/ui/icons 现无 database / search / shield，用近似语义替代：
        #   database → folder（存储容器）
        #   search → zoom-in（放大镜检索）
        #   shield → check-circle（已验证证据）
        nav_defs = [
            ("概览", "brain", OverviewPage()),
            ("记忆库", "folder", MemoryListPage()),
            ("时间线", "clock", TimelinePage()),
            ("检索追踪", "zoom-in", SearchTracesPage()),
            ("证据账本", "check-circle", EvidencePage()),
            ("写入记忆", "pencil", WritePage()),
        ]
        for title, icon_name, page in nav_defs:
            item = QListWidgetItem(title)
            item.setIcon(icon(icon_name, tokens.ICON_DEFAULT, tokens.ICON_SIZE_MD))
            self._nav.addItem(item)
            self._pages.addWidget(page)
            # 持有 page 引用避免 GC（按 nav 顺序存到对应属性）
            if title == "概览":
                self._overview_page = page
            elif title == "记忆库":
                self._memory_list_page = page
            elif title == "时间线":
                self._timeline_page = page
            elif title == "检索追踪":
                self._search_traces_page = page
            elif title == "证据账本":
                self._evidence_page = page
            elif title == "写入记忆":
                self._write_page = page

        # 默认选中概览
        self._nav.setCurrentRow(0)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._timer.start()
        # 进入面板时立即拉一次概览状态（不等 60s 定时器）
        if self._overview_page is not None:
            self._overview_page.refresh_status()

    def on_hide(self) -> None:
        self._timer.stop()

    def on_refresh(self) -> None:
        # 刷新按钮 → 当前页 refresh（概览页有 refresh_status，其他页 Ticket 06-10 接入）
        if self._nav is None:
            return
        row = self._nav.currentRow()
        if row == 0 and self._overview_page is not None:
            self._overview_page.refresh_status()

    def on_loading_changed(self, loading: bool) -> None:
        # Ticket 05 不接入 loading UI（操作状态走概览页底部 _action_status 标签）
        pass

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            if self._stack is not None and self._normal_widget is not None:
                self._stack.setCurrentWidget(self._normal_widget)
            # 后端上线立即拉一次概览状态
            if self._overview_page is not None:
                self._overview_page.refresh_status()
        else:
            if self._timer is not None:
                self._timer.stop()
            if self._stack is not None and self._offline_widget is not None:
                self._stack.setCurrentWidget(self._offline_widget)

    # —— 内部 ——

    def _on_nav_changed(self, row: int) -> None:
        """nav 切换 → 切换右侧 QStackedWidget 当前页 + 触发对应页刷新"""
        if self._pages is None or row < 0 or row >= self._pages.count():
            return
        self._pages.setCurrentIndex(row)
        # 各页切换时拉一次数据（遵守 ADR-0022：避免与其他页操作并发）
        if row == 0 and self._overview_page is not None:
            self._overview_page.refresh_status()
        elif row == 1 and self._memory_list_page is not None:
            self._memory_list_page.refresh_list()
        elif row == 2 and self._timeline_page is not None:
            ref = getattr(self._timeline_page, "refresh", None)
            if callable(ref):
                ref()
        elif row == 3 and self._search_traces_page is not None:
            ref = getattr(self._search_traces_page, "refresh", None)
            if callable(ref):
                ref()
        elif row == 4 and self._evidence_page is not None:
            ref = getattr(self._evidence_page, "refresh", None)
            if callable(ref):
                ref()

    def _on_timer_timeout(self) -> None:
        """60s 定时器：仅当当前是概览页时刷新（遵守 ADR-0022 避免并发触发）"""
        if self._nav is None or self._nav.currentRow() != 0:
            return
        if self._overview_page is not None:
            self._overview_page.refresh_status()

    def _on_jump_to(self, page_name: str, params: dict) -> None:
        """概览页 concerns 卡片「去查看」按钮 → 切 nav + 应用筛选参数

        Args:
            page_name: overview / memory_list / timeline / search_traces / evidence / write
            params: 筛选参数（如 {"stale": True}），转发到目标页（Ticket 06-10 接入 set_filter）
        """
        row = self._JUMP_PAGE_TO_ROW.get(page_name)
        if row is None or self._nav is None:
            return
        self._nav.setCurrentRow(row)
        # 切换后转发参数到对应页（其他 ticket 实现接收入口，目前概览页无参数需要接收）
        page = self._pages.widget(row) if self._pages is not None else None
        if page is None:
            return
        # 各页 Ticket 06-10 实现 apply_jump_params(params) 方法时接入；本期 no-op
        apply = getattr(page, "apply_jump_params", None)
        if callable(apply):
            apply(params)

    def _on_jump_to_write_page(self, key: str) -> None:
        """记忆库页「编辑」按钮 → 切到写入页 + 调预填接口（Ticket 10 实现）

        Args:
            key: 待编辑的记忆 key
        """
        if self._nav is None:
            return
        self._nav.setCurrentRow(self._JUMP_PAGE_TO_ROW["write"])
        if self._write_page is None:
            return
        # Ticket 10 实现 prefill_from_key(key) 入口；本期 no-op（写入页仍为占位）
        prefill = getattr(self._write_page, "prefill_from_key", None)
        if callable(prefill):
            prefill(key)


__all__ = ["MemoryPanel"]
