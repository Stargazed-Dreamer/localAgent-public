"""模型池监控面板 — 统一展示 LLM / VL / OCR / AIGC 等模型类别状态

顶部下拉框切换子类，每个子类有独立的卡片布局和数据源：
- LLM: /health.llm_pool + /llm/pool/{status,models,recent-calls}
- VL : /health.vision + /health.loops.vl_quota + /vl/providers
- OCR: /health.ocr
- AIGC: 暂无数据，占位

异步刷新（QThread + Signal），不阻塞 UI。
"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

# Tier 颜色
_TIER_COLORS = {
    1: tokens.TEXT_TERTIARY,
    2: tokens.INFO_TEXT,
    3: tokens.TEXT_PRIMARY,
    4: tokens.WARNING_TEXT,
    5: tokens.ACCENT,
}


def _tier_color(tier: int) -> str:
    return _TIER_COLORS.get(tier, tokens.TEXT_PRIMARY)


def _safe(v, default="—"):
    if v is None or v == "":
        return default
    return v


def _section_label(text: str) -> QLabel:
    """每行卡片左侧的小字说明（如 'Key 情况'）"""
    lbl = QLabel(text)
    set_text_role(lbl, "title")
    lbl.setStyleSheet("padding: 4px 0;")
    return lbl


class ModelPoolPanel(PanelBase):
    """模型池监控面板（含 LLM/VL/OCR/AIGC 子面板）"""

    PANEL_META = PanelMeta(
        id="llm_pool",  # id 保持，避免 app.py 信号连接断
        title="模型池",  # 改名
        icon="bot",
        order=43,
        category="monitor",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._health: dict | None = None
        # LLM 数据
        self._pool_status: dict | None = None
        self._pool_models: dict | None = None
        self._pool_recent_calls: dict | None = None
        # VL 数据
        self._vl_providers: dict | None = None
        self._refresh_btn: QPushButton | None = None
        self._refresh_thread = None
        # 摘要 labels（按子面板分组）
        self._llm_summary_labels: dict[str, QLabel] = {}
        self._vl_summary_labels: dict[str, QLabel] = {}
        self._ocr_summary_labels: dict[str, QLabel] = {}
        # 子面板切换
        self._subcombo: QComboBox | None = None
        self._sub_stack: QStackedWidget | None = None

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
        set_text_role(label, "heading")
        layout.addWidget(label)
        return w

    def _build_normal_ui(self, root: QWidget) -> None:
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部标题行 + 子面板下拉框 + 刷新按钮
        header_row = QHBoxLayout()
        title = QLabel("模型池监控")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        sub_label = QLabel("类别:")
        set_text_role(sub_label, "secondary")
        header_row.addWidget(sub_label)
        self._subcombo = QComboBox()
        self._subcombo.addItem("LLM", userData="llm")
        self._subcombo.addItem("VL", userData="vl")
        self._subcombo.addItem("OCR", userData="ocr")
        self._subcombo.addItem("AIGC", userData="aigc")
        self._subcombo.currentIndexChanged.connect(self._on_subcombo_changed)
        header_row.addWidget(self._subcombo)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)
        layout.addLayout(header_row)

        # 子面板 stack
        self._sub_stack = QStackedWidget()
        self._sub_stack.addWidget(self._build_llm_subpanel())  # idx 0
        self._sub_stack.addWidget(self._build_vl_subpanel())   # idx 1
        self._sub_stack.addWidget(self._build_ocr_subpanel())  # idx 2
        self._sub_stack.addWidget(self._build_aigc_subpanel()) # idx 3
        layout.addWidget(self._sub_stack, 1)

    def _make_summary_card(self, key: str, label_text: str, store: dict) -> QWidget:
        """构造一个摘要小卡片（label + value）"""
        v = QLabel("—")
        set_text_role(v, "title")
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(label_text)
        set_text_role(lbl, "secondary")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box = QVBoxLayout()
        box.setSpacing(2)
        box.setContentsMargins(8, 6, 8, 6)
        box.addWidget(lbl)
        box.addWidget(v)
        wrap = QFrame()
        wrap.setLayout(box)
        set_kind(wrap, "card")
        store[key] = v
        return wrap

    # ===== LLM 子面板 =====

    def _build_llm_subpanel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 行 1: Key 情况 + 4 张卡（总数/可用/冷却/隐私不安全-Paid）
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(_section_label("Key 情况"))
        for key, label_text in [
            ("keys", "总数"),
            ("available", "可用"),
            ("cooldown", "冷却"),
            ("privacy_paid", "隐私不安全 / 安全"),
        ]:
            row1.addWidget(self._make_summary_card(key, label_text, self._llm_summary_labels))
        row1.addStretch()
        layout.addLayout(row1)

        # 行 2: Model 情况 + 4 张卡（模型总数/禁用模型/当前并发/近期调用）
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(_section_label("Model 情况"))
        for key, label_text in [
            ("models_total", "模型总数"),
            ("disabled_models", "禁用"),
            ("concurrency", "当前并发"),
            ("recent_calls", "近期调用"),
        ]:
            row2.addWidget(self._make_summary_card(key, label_text, self._llm_summary_labels))
        row2.addStretch()
        layout.addLayout(row2)

        # 行 3: 全局 + 2 张卡（上次检查/总并发上限）
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        row3.addWidget(_section_label("全局"))
        for key, label_text in [
            ("last_check", "上次检查"),
            ("max_concurrency", "总并发上限"),
        ]:
            row3.addWidget(self._make_summary_card(key, label_text, self._llm_summary_labels))
        row3.addStretch()
        layout.addLayout(row3)

        # 三 Tab
        self._llm_tabs = QTabWidget()
        self._llm_tabs.addTab(self._build_keys_tab(), "Keys & Models")
        self._llm_tabs.addTab(self._build_models_tab(), "Models 聚合")
        self._llm_tabs.addTab(self._build_calls_tab(), "近期调用")
        layout.addWidget(self._llm_tabs, 1)
        return w

    def _build_keys_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        hint = QLabel("双击 key 行展开/折叠其下 model 详情（标记为隐私不安全的 key/model）")
        set_text_role(hint, "caption")
        layout.addWidget(hint)

        self._keys_tree = QTreeWidget()
        self._keys_tree.setColumnCount(7)
        self._keys_tree.setHeaderLabels([
            "Key / Model", "状态", "协议", "Tier", "并发", "调用统计", "Tokens",
        ])
        self._keys_tree.setAlternatingRowColors(True)
        header = self._keys_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(1, 80)
        header.resizeSection(2, 80)
        header.resizeSection(3, 60)
        header.resizeSection(4, 80)
        header.resizeSection(5, 180)
        header.resizeSection(6, 100)
        layout.addWidget(self._keys_tree, 1)
        return w

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        hint = QLabel("跨 key 聚合的 per-model 调用统计（按总调用次数倒序；标记为隐私不安全）")
        set_text_role(hint, "caption")
        layout.addWidget(hint)

        self._models_table = QTableWidget(0, 13)
        self._models_table.setHorizontalHeaderLabels([
            "Model (展示名)", "连接名", "Tier", "隐私", "Keys", "可用",
            "状态", "连续失败",
            "总调用", "成功", "失败", "429", "Tokens",
        ])
        self._models_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._models_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._models_table.setAlternatingRowColors(True)
        hdr = self._models_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.resizeSection(2, 50)
        hdr.resizeSection(3, 50)
        hdr.resizeSection(4, 60)
        hdr.resizeSection(5, 50)
        hdr.resizeSection(6, 60)   # v15: 状态
        hdr.resizeSection(7, 70)   # v15: 连续失败
        hdr.resizeSection(8, 70)
        hdr.resizeSection(9, 70)
        hdr.resizeSection(10, 60)
        hdr.resizeSection(11, 60)
        hdr.resizeSection(12, 100)
        self._models_table.verticalHeader().setVisible(False)
        layout.addWidget(self._models_table, 1)
        return w

    def _build_calls_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        hint = QLabel("最近 20 次调用记录（最新在前）")
        set_text_role(hint, "caption")
        layout.addWidget(hint)

        self._calls_table = QTableWidget(0, 7)
        self._calls_table.setHorizontalHeaderLabels([
            "时间", "Key", "Model", "状态", "Tokens", "耗时(ms)", "错误",
        ])
        self._calls_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._calls_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._calls_table.setAlternatingRowColors(True)
        hdr = self._calls_table.horizontalHeader()
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        hdr.resizeSection(0, 160)
        hdr.resizeSection(1, 160)
        hdr.resizeSection(2, 200)
        hdr.resizeSection(3, 50)
        hdr.resizeSection(4, 70)
        hdr.resizeSection(5, 80)
        self._calls_table.verticalHeader().setVisible(False)
        layout.addWidget(self._calls_table, 1)
        return w

    # ===== VL 子面板 =====

    def _build_vl_subpanel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 行 1: VL 启用状态 + 5 张卡
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(_section_label("VL 状态"))
        for key, label_text in [
            ("vl_enabled", "启用"),
            ("vl_available", "可用"),
            ("vl_sensitive", "敏感可用"),
            ("vl_provider", "Provider"),
            ("vl_model", "Model"),
        ]:
            row1.addWidget(self._make_summary_card(key, label_text, self._vl_summary_labels))
        row1.addStretch()
        layout.addLayout(row1)

        # 行 2: 配额情况 + 5 张卡
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(_section_label("配额（今日）"))
        for key, label_text in [
            ("vl_used_today", "已用"),
            ("vl_remaining", "剩余"),
            ("vl_skipped", "已跳过"),
            ("vl_exhausted", "今日耗尽"),
            ("vl_last_call", "上次调用"),
        ]:
            row2.addWidget(self._make_summary_card(key, label_text, self._vl_summary_labels))
        row2.addStretch()
        layout.addLayout(row2)

        # Tab：providers 表 + 小时分布表
        self._vl_tabs = QTabWidget()
        self._vl_tabs.addTab(self._build_vl_providers_tab(), "Providers 详情")
        self._vl_tabs.addTab(self._build_vl_hours_tab(), "小时调用分布")
        layout.addWidget(self._vl_tabs, 1)
        return w

    def _build_vl_providers_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        hint = QLabel("VL providers 详情（来自 /vision/providers；标记为隐私不安全）")
        set_text_role(hint, "caption")
        layout.addWidget(hint)

        self._vl_providers_table = QTableWidget(0, 8)
        self._vl_providers_table.setHorizontalHeaderLabels([
            "Label", "Model", "可用", "并发", "冷却(s)", "隐私", "Base URL", "最近错误",
        ])
        self._vl_providers_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._vl_providers_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._vl_providers_table.setAlternatingRowColors(True)
        hdr = self._vl_providers_table.horizontalHeader()
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.resizeSection(0, 140)
        hdr.resizeSection(2, 60)
        hdr.resizeSection(3, 80)
        hdr.resizeSection(4, 80)
        hdr.resizeSection(5, 60)
        hdr.resizeSection(6, 200)
        self._vl_providers_table.verticalHeader().setVisible(False)
        layout.addWidget(self._vl_providers_table, 1)
        return w

    def _build_vl_hours_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        hint = QLabel("今日 VL 调用按小时分布（来自 vl_quota.by_hour）")
        set_text_role(hint, "caption")
        layout.addWidget(hint)

        self._vl_hours_table = QTableWidget(0, 2)
        self._vl_hours_table.setHorizontalHeaderLabels(["小时", "调用次数"])
        self._vl_hours_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._vl_hours_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._vl_hours_table.setAlternatingRowColors(True)
        hdr = self._vl_hours_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._vl_hours_table.verticalHeader().setVisible(False)
        layout.addWidget(self._vl_hours_table, 1)
        return w

    # ===== OCR 子面板 =====

    def _build_ocr_subpanel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 行 1: OCR 加载状态
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(_section_label("OCR 模块"))
        for key, label_text in [
            ("ocr_loaded", "OCR 加载"),
            ("keep_models", "Keep Models"),
            ("ocr_model", "OCR 模型"),
        ]:
            row1.addWidget(self._make_summary_card(key, label_text, self._ocr_summary_labels))
        row1.addStretch()
        layout.addLayout(row1)

        layout.addStretch()
        return w

    # ===== AIGC 子面板（占位） =====

    def _build_aigc_subpanel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("AIGC 子面板\n\n项目当前未配置 AIGC（图像生成等）能力\n如有需要，请在后端添加相关模块后回到此面板查看")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(label, "secondary")
        layout.addWidget(label)
        return w

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        if self._health:
            self._stack.setCurrentWidget(self._normal_widget)
            self._update_all()
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
            self._pool_status = None
            self._pool_models = None
            self._pool_recent_calls = None
            self._vl_providers = None

    @Slot(dict)
    def on_health_changed(self, health: dict) -> None:
        """ServiceManager.backend_health_changed 信号的 slot"""
        self._health = health
        self._update_llm_summary_from_health()
        self._update_vl_summary_from_health()
        self._update_ocr_summary_from_health()

    # —— 子面板切换 ——

    def _on_subcombo_changed(self, _idx: int) -> None:
        if self._sub_stack is None or self._subcombo is None:
            return
        idx = self._subcombo.currentIndex()
        if 0 <= idx < self._sub_stack.count():
            self._sub_stack.setCurrentIndex(idx)

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
                self.pool_status = None
                self.pool_models = None
                self.pool_recent_calls = None
                self.vl_providers = None

            def run(self):
                self.health = self._http.get("/health")
                if self.health:
                    self.pool_status = self._http.get("/llm/pool/status")
                    self.pool_models = self._http.get("/llm/pool/models")
                    self.pool_recent_calls = self._http.get(
                        "/llm/pool/recent-calls", params={"limit": 20}
                    )
                    self.vl_providers = self._http.get("/vision/providers")

        self._refresh_thread = RefreshThread(self._http)
        self._refresh_thread.finished.connect(self._on_refresh_done)
        self._set_loading(True)
        self._refresh_thread.start()

    def _on_refresh_done(self) -> None:
        self._set_loading(False)
        t = self._refresh_thread
        # 初始化不变量：_refresh_thread 仅由 _refresh_async 创建，finished 触发时必非 None；
        # health None 表示后端离线（RefreshThread 内已判定）
        if t is None or t.health is None:
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._stack.setCurrentWidget(self._normal_widget)
        self._health = t.health
        self._pool_status = t.pool_status
        self._pool_models = t.pool_models
        self._pool_recent_calls = t.pool_recent_calls
        self._vl_providers = t.vl_providers
        self._update_all()

    # —— 更新 UI ——

    def _update_all(self) -> None:
        if not self._health:
            return
        self._update_llm_summary_from_health()
        self._update_llm_keys_tree()
        self._update_llm_models_table()
        self._update_llm_calls_table()
        self._update_vl_summary_from_health()
        self._update_vl_providers_table()
        self._update_vl_hours_table()
        self._update_ocr_summary_from_health()

    # ===== LLM 更新 =====

    def _update_llm_summary_from_health(self) -> None:
        """从 /health.llm_pool 更新 LLM 子面板顶部摘要栏"""
        if not self._health:
            return
        lp = self._health.get("llm_pool", {}) or {}
        if not isinstance(lp, dict):
            return
        total_keys = lp.get("total_keys", 0)
        active_keys = lp.get("active_keys", lp.get("available_keys", 0))
        cooldown_keys = total_keys - active_keys
        current_active = lp.get("current_active", 0)
        max_concurrency = lp.get("total_max_concurrency", 0)
        # 隐私不安全 = is_free=True 的 key（后端基于 model 名 + privacy_warning 关键词推断）
        privacy_keys = lp.get("free_keys", 0)
        safe_keys = lp.get("paid_keys", 0)
        total_models = lp.get("total_models", 0)
        recent_calls_count = lp.get("recent_calls_count", 0)
        last_check = lp.get("last_health_check", "") or "—"

        self._llm_summary_labels["keys"].setText(f"{total_keys}")
        self._llm_summary_labels["available"].setText(f"{active_keys}")
        self._llm_summary_labels["available"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if active_keys > 0 else f"color: {tokens.DANGER_TEXT};"
        )
        self._llm_summary_labels["cooldown"].setText(f"{cooldown_keys}")
        self._llm_summary_labels["cooldown"].setStyleSheet(
            f"color: {tokens.WARNING_TEXT};" if cooldown_keys > 0 else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._llm_summary_labels["privacy_paid"].setText(f"{privacy_keys} / {safe_keys}")
        self._llm_summary_labels["privacy_paid"].setStyleSheet(
            f"color: {tokens.WARNING_TEXT};" if privacy_keys > 0 else f"color: {tokens.SUCCESS_TEXT};"
        )
        self._llm_summary_labels["models_total"].setText(f"{total_models}")
        # v15: 禁用模型数（来自 /health.llm_pool.model_health_disabled_count）
        disabled_models = lp.get("model_health_disabled_count", 0) or 0
        self._llm_summary_labels["disabled_models"].setText(f"{disabled_models}")
        self._llm_summary_labels["disabled_models"].setStyleSheet(
            f"color: {tokens.DANGER_TEXT};" if disabled_models > 0 else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._llm_summary_labels["concurrency"].setText(f"{current_active} / {max_concurrency}")
        self._llm_summary_labels["concurrency"].setStyleSheet(
            f"color: {tokens.WARNING_TEXT};" if current_active >= max_concurrency and max_concurrency > 0
            else f"color: {tokens.SUCCESS_TEXT};"
        )
        self._llm_summary_labels["recent_calls"].setText(f"{recent_calls_count}")
        self._llm_summary_labels["recent_calls"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if recent_calls_count > 0 else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._llm_summary_labels["last_check"].setText(str(last_check))
        self._llm_summary_labels["max_concurrency"].setText(f"{max_concurrency}")

    def _update_llm_keys_tree(self) -> None:
        """更新 LLM Keys & Models 树"""
        tree = self._keys_tree
        tree.clear()
        ps = self._pool_status or {}
        if not ps.get("initialized"):
            self._add_tree_placeholder_row(tree, "（池未初始化）")
            return
        keys = ps.get("keys", []) or []
        if not keys:
            self._add_tree_placeholder_row(tree, "（无 key）")
            return
        for k in keys:
            key_item = self._build_key_item(k)
            tree.addTopLevelItem(key_item)
            key_item.setExpanded(True)

    def _add_tree_placeholder_row(self, tree: QTreeWidget, text: str) -> None:
        item = QTreeWidgetItem([text, "", "", "", "", "", ""])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        tree.addTopLevelItem(item)

    def _build_key_item(self, k: dict) -> QTreeWidgetItem:
        name = k.get("name", "?")
        is_free = k.get("is_free", False)  # 后端字段名保持，UI 层重命名为"隐私不安全"
        is_available = k.get("is_available", False)
        active = k.get("active_count", 0)
        max_c = k.get("max_concurrency", 0)
        cooldown = k.get("cooldown_remaining", 0)
        expired = k.get("expired", False)
        protocol = (k.get("protocol", "openai") or "openai").lower()
        if protocol not in ("openai", "anthropic"):
            protocol = "openai"
        stats = k.get("stats", {}) or {}
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        rl = stats.get("rate_limited", 0)
        tok = stats.get("total_tokens", 0)
        pw = k.get("privacy_warning", "") or ""

        # 免费源标记为隐私不安全（后端 is_free=True），安全源打 ✓
        privacy_tag = "" if is_free else " ✓"
        proto_tag = f" [{protocol}]" if protocol != "openai" else ""
        title = f"{name}{privacy_tag}{proto_tag}"
        if pw:
            title += "（隐私）"

        if expired:
            status_text = "✗ 失效"
            status_color = tokens.DANGER_TEXT
        elif cooldown > 0:
            status_text = f"冷却 {cooldown}s"
            status_color = tokens.WARNING_TEXT
        elif is_available:
            status_text = "✓ 可用"
            status_color = tokens.SUCCESS_TEXT
        else:
            status_text = "—"
            status_color = tokens.TEXT_TERTIARY

        # v15: 检测所有 model 都 disabled（key 全禁用警告）
        models = k.get("models", []) or []
        all_models_disabled = bool(models) and all(m.get("disabled", False) for m in models)
        if all_models_disabled and not expired:
            status_text = "已全禁用"
            status_color = tokens.DANGER_TEXT

        concurrency = f"{active}/{max_c}"
        call_stats = f"ok={ok} fail={fail} 429={rl}"

        item = QTreeWidgetItem([
            title, status_text, protocol, "—", concurrency, call_stats, f"{tok}"
        ])
        item.setForeground(0, QColor(tokens.TEXT_PRIMARY))
        item.setForeground(1, QColor(status_color))
        item.setForeground(2, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(4, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(5, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(6, QColor(tokens.TEXT_SECONDARY))
        tip_parts = []
        if pw:
            tip_parts.append(f"隐私不安全: {pw}")
        err = stats.get("last_error", "")
        if err:
            tip_parts.append(f"最近错误: {err[:120]}")
        if all_models_disabled:
            tip_parts.append("注意：该 key 的所有 model 都被 disabled，等待探针恢复")
        if tip_parts:
            item.setToolTip(0, "\n".join(tip_parts))

        for m in models:
            child = self._build_model_item(m)
            item.addChild(child)
        return item

    def _build_model_item(self, m: dict) -> QTreeWidgetItem:
        connect_name = m.get("name", "?") or "?"
        display_name = (m.get("display_name") or "").strip() or connect_name
        suffix = f"  ({connect_name})" if display_name != connect_name else ""
        title = f"└ {display_name}{suffix}"
        tier = m.get("tier", "?")
        m_free = m.get("is_free", False)  # 隐私不安全
        # v15: 改用后端的 disabled / in_model_cooldown / probe_in_flight 字段
        # （原代码读 m.enabled，但后端从未输出该字段，导致始终 True）
        m_disabled = m.get("disabled", False)
        m_in_cooldown = m.get("in_model_cooldown", False)
        m_probe = m.get("probe_in_flight", False)
        ms = m.get("stats", {}) or {}
        m_ok = ms.get("ok", 0)
        m_fail = ms.get("fail", 0)
        m_rl = ms.get("rate_limited", 0)
        m_tok = ms.get("total_tokens", 0)
        m_consec = m.get("consecutive_fails", 0) or 0

        # v15: 状态列——✓可用 / 冷却 / 禁用（探针中用浅色）
        if m_disabled:
            if m_probe:
                status = "探针"
                status_color = tokens.WARNING_TEXT
            else:
                status = "禁用"
                status_color = tokens.DANGER_TEXT
        elif m_in_cooldown:
            status = "冷却"
            status_color = tokens.WARNING_TEXT
        elif m_free:
            status = "隐私"
            status_color = tokens.WARNING_TEXT
        else:
            status = "✓"
            status_color = tokens.SUCCESS_TEXT

        # v15: 调用统计追加连续失败数（>0 时显示）
        if m_consec > 0:
            call_stats = f"ok={m_ok} fail={m_fail} 429={m_rl} consec={m_consec}"
        else:
            call_stats = f"ok={m_ok} fail={m_fail} 429={m_rl}"
        item = QTreeWidgetItem([
            title, status, "—", f"T{tier}", "—", call_stats, f"{m_tok}"
        ])
        item.setForeground(0, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(1, QColor(status_color))
        item.setForeground(3, QColor(_tier_color(tier if isinstance(tier, int) else 3)))
        item.setForeground(5, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(6, QColor(tokens.TEXT_SECONDARY))
        # v15: tooltip 补充 disabled 详情
        tip_parts = []
        if m_disabled:
            di = m.get("disabled_info")
            if isinstance(di, dict):
                tip_parts.append(
                    f"disabled @ {di.get('disabled_at', '?')}, "
                    f"上次探针 {di.get('last_probe_at', '?')}"
                )
            if m_probe:
                tip_parts.append("探针进行中（等待结果恢复或继续保持 disabled）")
        if m_consec > 0:
            tip_parts.append(f"consecutive_fails={m_consec}（达阈值 {getattr(self, '_model_health_threshold', 5)} 触发 disabled）")
        if tip_parts:
            item.setToolTip(0, "\n".join(tip_parts))
        return item

    def _update_llm_models_table(self) -> None:
        """更新跨 key 聚合的 Models 表"""
        table = self._models_table
        table.setRowCount(0)
        pm = self._pool_models or {}
        if not pm.get("initialized"):
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（池未初始化）"))
            return
        models = pm.get("models", []) or []
        if not models:
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（无 model）"))
            return
        table.setRowCount(len(models))
        for i, m in enumerate(models):
            name = m.get("name", "?")
            display_name = (m.get("display_name") or "").strip() or name
            tier = m.get("tier", "?")
            is_free = m.get("is_free", False)  # 隐私不安全
            keys_list = m.get("keys", []) or []
            avail_now = m.get("available_now", False)
            stats = m.get("stats", {}) or {}
            ok = stats.get("ok", 0)
            fail = stats.get("fail", 0)
            rl = stats.get("rate_limited", 0)
            tok = stats.get("total_tokens", 0)
            total = m.get("total_attempts", 0)
            # v15: 跨 key 聚合的降级状态
            disabled_count = m.get("disabled_count", 0) or 0
            probe_count = m.get("probe_in_flight_count", 0) or 0
            consec_max = m.get("consecutive_fails_max", 0) or 0
            total_keys_for_model = len(keys_list)

            disp_text = display_name if display_name != name else name
            # 隐私不安全（空文本），✓ 安全
            privacy_text = "" if is_free else "✓"

            # v15: 状态列——✓可用 / 冷却 / 禁用 / 全禁
            if avail_now:
                status_text = "✓"
                status_color = tokens.SUCCESS_TEXT
            elif total_keys_for_model > 0 and disabled_count >= total_keys_for_model:
                # 所有 key 都把该 model 标 disabled → 完全不可用
                status_text = "全禁"
                status_color = tokens.DANGER_TEXT
            elif disabled_count > 0:
                # 部分 disabled，其他可能在 cooldown
                status_text = f"禁{disabled_count}"
                status_color = tokens.WARNING_TEXT
            else:
                status_text = "冷却"
                status_color = tokens.WARNING_TEXT

            # v15: 连续失败列
            consec_text = f"{consec_max}"
            consec_color = tokens.DANGER_TEXT if consec_max > 0 else tokens.TEXT_TERTIARY

            cells = [
                disp_text,
                name,
                f"T{tier}",
                privacy_text,
                f"{len(keys_list)}",
                "✓" if avail_now else "✗",
                status_text,
                consec_text,
                f"{total}",
                f"{ok}",
                f"{fail}",
                f"{rl}",
                f"{tok}",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 0:
                    item.setForeground(QColor(tokens.TEXT_PRIMARY))
                elif col == 2:
                    item.setForeground(QColor(_tier_color(tier if isinstance(tier, int) else 3)))
                elif col == 3:
                    item.setForeground(QColor(tokens.WARNING_TEXT if is_free else tokens.SUCCESS_TEXT))
                elif col == 5:
                    item.setForeground(QColor(tokens.SUCCESS_TEXT if avail_now else tokens.TEXT_TERTIARY))
                elif col == 6:
                    # v15: 状态列颜色
                    item.setForeground(QColor(status_color))
                elif col == 7:
                    # v15: 连续失败列颜色
                    item.setForeground(QColor(consec_color))
                elif col == 8:
                    item.setForeground(QColor(tokens.TEXT_SECONDARY if total > 0 else tokens.TEXT_TERTIARY))
                elif col == 10 and fail > 0:
                    item.setForeground(QColor(tokens.DANGER_TEXT))
                elif col == 11 and rl > 0:
                    item.setForeground(QColor(tokens.WARNING_TEXT))
                elif col == 12:
                    item.setForeground(QColor(tokens.TEXT_SECONDARY if tok > 0 else tokens.TEXT_TERTIARY))
                table.setItem(i, col, item)
                # v15: tooltip 补充探针信息
                if col == 6 and probe_count > 0:
                    item.setToolTip(f"探针进行中: {probe_count}/{disabled_count} 个 disabled model 正在探针恢复")

    def _update_llm_calls_table(self) -> None:
        """更新近期调用表"""
        table = self._calls_table
        table.setRowCount(0)
        prc = self._pool_recent_calls or {}
        if not prc.get("initialized"):
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（池未初始化）"))
            return
        calls = prc.get("calls", []) or []
        if not calls:
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（暂无调用历史）"))
            return
        table.setRowCount(len(calls))
        for i, c in enumerate(calls):
            ts = c.get("ts_str", "") or ""
            key_name = c.get("key_name", "?") or "?"
            model = c.get("model", "?") or "?"
            success = c.get("success", False)
            tokens_val = c.get("tokens", 0)
            duration = c.get("duration_ms", 0)
            error = c.get("error", "") or ""

            cells = [
                ts,
                key_name,
                model,
                "✓" if success else "✗",
                f"{tokens_val}",
                f"{duration}",
                error,
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 3:
                    item.setForeground(QColor(tokens.SUCCESS_TEXT if success else tokens.DANGER_TEXT))
                elif col in (0, 1, 2):
                    item.setForeground(QColor(tokens.TEXT_SECONDARY))
                elif col == 4:
                    item.setForeground(QColor(tokens.TEXT_SECONDARY if tokens_val > 0 else tokens.TEXT_TERTIARY))
                elif col == 5:
                    item.setForeground(QColor(tokens.TEXT_SECONDARY))
                elif col == 6:
                    item.setForeground(QColor(tokens.DANGER_TEXT if error else tokens.TEXT_TERTIARY))
                table.setItem(i, col, item)

    # ===== VL 更新 =====

    def _update_vl_summary_from_health(self) -> None:
        """从 /health.vision + /health.loops.vl_quota 更新 VL 子面板摘要"""
        if not self._health:
            return
        vis = self._health.get("vision", {}) or {}
        if not isinstance(vis, dict):
            vis = {}
        loops = self._health.get("loops", {}) or {}
        if not isinstance(loops, dict):
            loops = {}
        vl_quota = loops.get("vl_quota", {}) or {}
        if not isinstance(vl_quota, dict):
            vl_quota = {}

        # VL 状态
        vl_enabled = bool(vis.get("vl_model_enabled", False))
        vl_available = bool(vis.get("vl_available", False))
        vl_sensitive = bool(vis.get("vl_usable_for_sensitive", False))
        vl_provider = vis.get("vl_provider", "") or "—"
        vl_model = vis.get("vl_model", "") or "—"

        # 配额
        used_today = vl_quota.get("used_today", 0)
        remaining = vl_quota.get("remaining_quota", 0)
        skipped = vl_quota.get("skipped_today", 0)
        exhausted = bool(vl_quota.get("daily_exhausted", False))
        last_call = vl_quota.get("last_call_ts", "") or "—"

        self._vl_summary_labels["vl_enabled"].setText("是" if vl_enabled else "否")
        self._vl_summary_labels["vl_enabled"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if vl_enabled else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._vl_summary_labels["vl_available"].setText("✓" if vl_available else "✗")
        self._vl_summary_labels["vl_available"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if vl_available else f"color: {tokens.DANGER_TEXT};"
        )
        self._vl_summary_labels["vl_sensitive"].setText("✓" if vl_sensitive else "✗")
        self._vl_summary_labels["vl_sensitive"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if vl_sensitive else f"color: {tokens.WARNING_TEXT};"
        )
        self._vl_summary_labels["vl_provider"].setText(str(vl_provider))
        self._vl_summary_labels["vl_model"].setText(str(vl_model))
        self._vl_summary_labels["vl_used_today"].setText(f"{used_today}")
        self._vl_summary_labels["vl_used_today"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if used_today > 0 else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._vl_summary_labels["vl_remaining"].setText(f"{remaining}")
        self._vl_summary_labels["vl_remaining"].setStyleSheet(
            f"color: {tokens.DANGER_TEXT};" if remaining == 0 else f"color: {tokens.SUCCESS_TEXT};"
        )
        self._vl_summary_labels["vl_skipped"].setText(f"{skipped}")
        self._vl_summary_labels["vl_exhausted"].setText("是" if exhausted else "否")
        self._vl_summary_labels["vl_exhausted"].setStyleSheet(
            f"color: {tokens.DANGER_TEXT};" if exhausted else f"color: {tokens.SUCCESS_TEXT};"
        )
        self._vl_summary_labels["vl_last_call"].setText(str(last_call))

    def _update_vl_providers_table(self) -> None:
        """更新 VL providers 表"""
        table = self._vl_providers_table
        table.setRowCount(0)
        vp = self._vl_providers or {}
        if not vp.get("enabled"):
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（VL 未启用）"))
            return
        providers = vp.get("providers", []) or []
        if not providers:
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（无 VL provider）"))
            return
        table.setRowCount(len(providers))
        for i, p in enumerate(providers):
            label = p.get("label") or p.get("key_id", "?")
            model = p.get("model", "?") or "?"
            available = bool(p.get("available", False))
            active = p.get("active_count", 0)
            max_c = p.get("max_concurrency", 0)
            cooldown = p.get("cooldown_remaining", 0)
            privacy = bool(p.get("privacy_warning", ""))
            base_url = p.get("base_url", "") or ""
            last_error = p.get("last_error", "") or ""

            cells = [
                label,
                model,
                "✓" if available else "✗",
                f"{active}/{max_c}",
                f"{cooldown}",
                "⚠️" if privacy else "✓",
                base_url,
                last_error,
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                if col == 0:
                    item.setForeground(QColor(tokens.TEXT_PRIMARY))
                elif col == 2:
                    item.setForeground(QColor(tokens.SUCCESS_TEXT if available else tokens.DANGER_TEXT))
                elif col == 4 and cooldown > 0:
                    item.setForeground(QColor(tokens.WARNING_TEXT))
                elif col == 5:
                    item.setForeground(QColor(tokens.WARNING_TEXT if privacy else tokens.SUCCESS_TEXT))
                elif col == 7 and last_error:
                    item.setForeground(QColor(tokens.DANGER_TEXT))
                else:
                    item.setForeground(QColor(tokens.TEXT_SECONDARY))
                table.setItem(i, col, item)

    def _update_vl_hours_table(self) -> None:
        """更新 VL 小时分布表"""
        table = self._vl_hours_table
        table.setRowCount(0)
        if not self._health:
            return
        loops = self._health.get("loops", {}) or {}
        if not isinstance(loops, dict):
            return
        vl_quota = loops.get("vl_quota", {}) or {}
        if not isinstance(vl_quota, dict):
            return
        by_hour: dict = vl_quota.get("by_hour", {}) or {}
        if not by_hour:
            table.setRowCount(1)
            table.setItem(0, 0, self._placeholder_item("（暂无调用记录）"))
            return
        # 按小时排序
        items = sorted(by_hour.items(), key=lambda x: x[0])
        table.setRowCount(len(items))
        for i, (hour, count) in enumerate(items):
            h_item = QTableWidgetItem(str(hour))
            c_item = QTableWidgetItem(str(count))
            h_item.setForeground(QColor(tokens.TEXT_SECONDARY))
            c_item.setForeground(
                QColor(tokens.SUCCESS_TEXT if count > 0 else tokens.TEXT_TERTIARY)
            )
            table.setItem(i, 0, h_item)
            table.setItem(i, 1, c_item)

    # ===== OCR 更新 =====

    def _update_ocr_summary_from_health(self) -> None:
        """从 /health.ocr + /health.vision 更新 OCR 子面板摘要"""
        if not self._health:
            return
        ocr = self._health.get("ocr", {}) or {}
        if not isinstance(ocr, dict):
            ocr = {}
        vis = self._health.get("vision", {}) or {}
        if not isinstance(vis, dict):
            vis = {}

        ocr_loaded = bool(ocr.get("ocr_loaded", False))
        keep_models = bool(ocr.get("keep_models", False))
        ocr_model = ocr.get("ocr_model", "") or "—"

        self._ocr_summary_labels["ocr_loaded"].setText("是" if ocr_loaded else "否")
        self._ocr_summary_labels["ocr_loaded"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if ocr_loaded else f"color: {tokens.DANGER_TEXT};"
        )
        self._ocr_summary_labels["keep_models"].setText("是" if keep_models else "否")
        self._ocr_summary_labels["keep_models"].setStyleSheet(
            f"color: {tokens.SUCCESS_TEXT};" if keep_models else f"color: {tokens.TEXT_TERTIARY};"
        )
        self._ocr_summary_labels["ocr_model"].setText(str(ocr_model))

    # —— 通用 ——

    def _placeholder_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setForeground(QColor(tokens.TEXT_TERTIARY))
        return item
