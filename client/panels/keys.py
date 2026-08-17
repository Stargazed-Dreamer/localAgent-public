"""Keys 面板 — LLM/VL/AIGC API Key 统一管理

QTreeWidget 按 key 分组：
- 顶层 = key 行（标签 / URL / Key掩码 / 并发 / 启用 / 状态 / 操作2×2）
- 子层 = model 行（model名 / 输入输出类型 / 信息安全 / 允许用途 / 启用 / Tier / 空）

交互：
- 双击 key 行 → 编辑 key 信息（KeyForm key_only 模式）
- 双击 model 行 → 批量编辑 model（_BatchEditModelsDialog）
- 右键 key 行 → 菜单：编辑 / 批量编辑 model / 测试 / 复制 / 删除
- 右键 model 行 → 菜单：批量编辑 model / 编辑父 key
- 操作列 2×2 按钮：测试 / 编辑 / 复制 / 删除

筛选：scope 复选框（LLM/VL/AIGC图/AIGC视频）+ 仅可用
不暴露厂商和分组字段（后端 schema 仍支持，UI 不显示）。
"""


from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from client.core.state import AppState
from client.widgets.key_form import KeyForm, _BatchEditModelsDialog, _scope_to_types
from lib.ui import icon_button, tokens
from lib.ui.theme import set_text_role

# 状态颜色
_STATUS_COLORS = {
    "ok": tokens.SUCCESS_TEXT,
    "fail": tokens.DANGER_TEXT,
    "unknown": tokens.TEXT_TERTIARY,
}
_STATUS_LABELS = {
    "ok": "● 可用",
    "fail": "● 失败",
    "unknown": "● 未测",
}


# 列索引常量
COL_NAME = 0       # key: 标签；model: model 名
COL_URL = 1        # key: base_url；model: 输入输出类型
COL_KEY = 2        # key: 掩码 key；model: 信息安全
COL_CONCURRENCY = 3  # key: 并发数；model: 允许用途
COL_ENABLED = 4    # 启用 checkbox
COL_STATUS = 5     # 状态（仅 key 行）
COL_OPS = 6        # 操作


def _status_key(status: dict) -> str:
    """从 status dict 推断状态 key"""
    works = status.get("works")
    if works is True:
        return "ok"
    if works is False:
        return "fail"
    return "unknown"


def _mask_key(key_val: str) -> str:
    """脱敏 key 显示（首4 + **** + 尾4）"""
    if not key_val or not isinstance(key_val, str):
        return "—"
    if "****" in key_val:
        return key_val  # 已经是脱敏格式
    if len(key_val) <= 10:
        return "****"
    return f"{key_val[:4]}****{key_val[-4:]}"


def _privacy_label(privacy_warning: str) -> str:
    """隐私安全标签"""
    if privacy_warning:
        return "不安全"
    return "安全"


def _allowed_uses_label(allowed: list) -> str:
    """允许用途显示"""
    if not allowed:
        return "全部"
    return f"{len(allowed)} 项"


# Tier 标签 + 颜色（v8: 1-5）
_TIER_LABELS = {
    1: ("T1 轻量", tokens.TEXT_TERTIARY),
    2: ("T2 简单", tokens.INFO_TEXT),
    3: ("T3 默认", tokens.TEXT_PRIMARY),
    4: ("T4 较强", tokens.WARNING_TEXT),
    5: ("T5 强大", tokens.ACCENT),
}


def _tier_label(tier: int) -> tuple[str, str]:
    """根据 tier 返回 (显示文本, 颜色)"""
    return _TIER_LABELS.get(tier, ("T3 默认", tokens.TEXT_PRIMARY))


def _centered_checkbox(checked: bool, tooltip: str = "") -> tuple[QWidget, QCheckBox]:
    """创建与表格单元格等宽、居中的复选框，避免右侧出现不可用黑色空带。"""
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    checkbox = QCheckBox()
    checkbox.setChecked(checked)
    checkbox.setMinimumSize(24, 26)
    if tooltip:
        checkbox.setToolTip(tooltip)
    layout.addWidget(checkbox)
    return wrapper, checkbox


class KeysPanel(PanelBase):
    """密钥管理面板：QTreeWidget 按 key 分组 + 双击/右键编辑。"""

    PANEL_META = PanelMeta(
        id="keys",
        title="密钥",
        icon="key",
        order=51,
        category="advanced",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._state = AppState.instance()
        # 始终默认隐藏密钥（不读取上次状态，避免敏感信息默认展示）
        self._unmasked = False
        self._keys: list[dict] = []
        self._usage_data: dict = {}
        self._reload_thread = None
        self._test_thread = None
        self._use_cases_thread = None
        # 全部测试轮询定时器
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._on_poll_timeout)
        self._poll_count = 0
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
        self._offline_widget = QWidget()
        offline_layout = QVBoxLayout(self._offline_widget)
        offline_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        offline_label = QLabel("后端离线\n\n请先启动后端服务（start.bat）")
        offline_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(offline_label, "secondary")
        offline_layout.addWidget(offline_label)
        self._stack.addWidget(self._offline_widget)

        self._build_normal_ui(self._normal_widget)

    def _build_normal_ui(self, root: QWidget) -> None:
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶栏
        top_bar = QHBoxLayout()
        title = QLabel("LLM/VL 密钥管理")
        set_text_role(title, "heading")
        top_bar.addWidget(title)
        top_bar.addStretch()

        self._add_btn = QPushButton("添加")
        self._add_btn.clicked.connect(self._on_add_clicked)
        top_bar.addWidget(self._add_btn)

        self._health_check_btn = QPushButton("全部测试")
        self._health_check_btn.clicked.connect(self._on_health_check_all_clicked)
        top_bar.addWidget(self._health_check_btn)

        self._unmasked_btn = QPushButton(
            "隐藏key细节" if self._unmasked else "展示key"
        )
        self._unmasked_btn.setCheckable(True)
        self._unmasked_btn.setChecked(self._unmasked)
        self._unmasked_btn.clicked.connect(self._on_toggle_unmasked)
        top_bar.addWidget(self._unmasked_btn)
        layout.addLayout(top_bar)

        # 筛选行（保留 scope 筛选 + 仅可用；删除 group 筛选）
        filter_row = QHBoxLayout()
        filter_label = QLabel("筛选:")
        set_text_role(filter_label, "secondary")
        filter_row.addWidget(filter_label)

        self._scope_llm_cb = QCheckBox("LLM")
        self._scope_llm_cb.setChecked(True)
        self._scope_llm_cb.stateChanged.connect(self._apply_filters)
        filter_row.addWidget(self._scope_llm_cb)

        self._scope_vl_cb = QCheckBox("VL")
        self._scope_vl_cb.setChecked(True)
        self._scope_vl_cb.stateChanged.connect(self._apply_filters)
        filter_row.addWidget(self._scope_vl_cb)

        self._scope_aigc_image_cb = QCheckBox("AIGC图")
        self._scope_aigc_image_cb.setChecked(True)
        self._scope_aigc_image_cb.stateChanged.connect(self._apply_filters)
        filter_row.addWidget(self._scope_aigc_image_cb)

        self._scope_aigc_video_cb = QCheckBox("AIGC视频")
        self._scope_aigc_video_cb.setChecked(True)
        self._scope_aigc_video_cb.stateChanged.connect(self._apply_filters)
        filter_row.addWidget(self._scope_aigc_video_cb)

        filter_row.addSpacing(12)
        self._only_available_cb = QCheckBox("仅可用")
        self._only_available_cb.stateChanged.connect(self._apply_filters)
        filter_row.addWidget(self._only_available_cb)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # 树
        self._tree = QTreeWidget()
        self._tree.setColumnCount(7)
        self._tree.setHeaderLabels(
            ["名称", "URL / 类型", "Key / 安全", "并发 / 用途", "启用", "Tier / 状态", "操作"]
        )
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setExpandsOnDoubleClick(False)  # 禁用双击展开/折叠（双击仅触发编辑）
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.setStyleSheet(
            f"QTreeWidget {{ font-family: {tokens.FONT_MONO}; }}"
        )
        header = self._tree.header()
        # 全部列改为 Interactive 模式，允许用户拖动调整列宽
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 初始宽度：名称列加宽，URL/Key 列大幅缩短
        header.resizeSection(COL_NAME, 240)
        header.resizeSection(COL_URL, 200)
        header.resizeSection(COL_KEY, 130)
        header.resizeSection(COL_CONCURRENCY, 90)
        header.resizeSection(COL_ENABLED, 84)
        header.resizeSection(COL_STATUS, 100)
        header.resizeSection(COL_OPS, 140)
        header.setStretchLastSection(False)
        layout.addWidget(self._tree, 1)

        # 底栏
        self._summary_label = QLabel("加载中...")
        set_text_role(self._summary_label, "secondary")
        layout.addWidget(self._summary_label)

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        self._load_use_cases_async()
        if self._keys:
            self._fill_tree()
            self._apply_filters()
        self._reload_keys_async()

    def _load_use_cases_async(self) -> None:
        """异步加载 use case 定义，不阻塞 UI"""
        if self._use_cases_thread is not None and self._use_cases_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class UseCasesThread(QThread):
            def __init__(self, http):
                super().__init__()
                self._http = http

            def run(self):
                KeyForm.load_use_cases(self._http)

        self._use_cases_thread = UseCasesThread(self._http)
        self._use_cases_thread.start()

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            self._reload_keys_async()
        else:
            self._stack.setCurrentWidget(self._offline_widget)

    # —— 数据加载 ——

    def _reload_keys_async(self) -> None:
        """异步刷新 keys 列表，不阻塞 UI"""
        if self._reload_thread is not None and self._reload_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class ReloadThread(QThread):
            def __init__(self, http, unmasked):
                super().__init__()
                self._http = http
                self._unmasked = unmasked
                self.keys = None
                self.usage = None

            def run(self):
                params = {"unmasked": "true"} if self._unmasked else None
                self.keys = self._http.get("/apikey/keys", params=params)
                self.usage = self._http.get("/apikey/keys/usage")

        self._reload_thread = ReloadThread(self._http, self._unmasked)
        self._reload_thread.finished.connect(self._on_reload_done)
        self._reload_thread.start()

    def _on_reload_done(self) -> None:
        if self._reload_thread.keys is None:
            self._summary_label.setText("加载失败（后端不可达）")
            return
        self._keys = self._reload_thread.keys.get("keys", [])
        self._usage_data = self._reload_thread.usage or {}
        self._fill_tree()
        self._apply_filters()

    # —— 树填充 ——

    def _fill_tree(self) -> None:
        self._tree.clear()
        # 构建 key_id → used_by 映射
        usage_keys = {}
        if isinstance(self._usage_data, dict):
            for k in self._usage_data.get("keys", []):
                usage_keys[k.get("id", "")] = k.get("used_by", [])

        for k in self._keys:
            key_id = k.get("id", "")
            key_item = QTreeWidgetItem()
            # UserRole 存 key_id 和 "key" 标记
            key_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "key", "id": key_id})
            # v11：读取 protocol（非默认 openai 时在标签后追加）
            protocol = (k.get("protocol") or "openai").lower()
            if protocol not in ("openai", "anthropic"):
                protocol = "openai"
            # 列 0: 标签（非 openai 协议后缀）
            label_text = k.get("label", "")
            privacy = k.get("privacy_warning", "")
            if protocol != "openai":
                label_text = f"{label_text}  [{protocol.upper()}]"
            key_item.setText(COL_NAME, label_text)
            tooltip = f"ID: {key_id}\nProtocol: {protocol}"
            if privacy:
                tooltip += f"\n隐私警告: {privacy}"
            key_item.setToolTip(COL_NAME, tooltip)
            # 列 1: base_url
            base_url = k.get("base_url", "")
            key_item.setText(COL_URL, base_url)
            key_item.setToolTip(COL_URL, base_url)
            # 列 2: 掩码 key
            key_val = k.get("key", "")
            mask_display = key_val if self._unmasked else _mask_key(key_val)
            key_item.setText(COL_KEY, mask_display)
            key_item.setToolTip(COL_KEY, "双击编辑查看完整 key")
            # 列 3: 并发
            conc = str(k.get("max_concurrency", 3))
            key_item.setText(COL_CONCURRENCY, conc)
            key_item.setTextAlignment(COL_CONCURRENCY, Qt.AlignmentFlag.AlignCenter)
            # 列 4: 启用（cellWidget 在 setItemWidget 中处理）
            # 列 5: 状态
            status = k.get("status", {})
            sk = _status_key(status)
            status_text = _STATUS_LABELS[sk]
            key_item.setText(COL_STATUS, status_text)
            key_item.setForeground(COL_STATUS, QBrush(QColor(_STATUS_COLORS[sk])))
            detail = status.get("last_check_detail", "")
            if detail:
                key_item.setToolTip(COL_STATUS,
                    f"状态: {sk}\n时间: {status.get('last_health_check', '')}\n详情:\n{detail[:500]}")
            else:
                key_item.setToolTip(COL_STATUS,
                    f"状态: {sk}\n时间: {status.get('last_health_check', '无')}")

            # 添加到树
            self._tree.addTopLevelItem(key_item)
            key_item.setSizeHint(COL_NAME, QSize(0, 44))

            # 设置 cellWidget（必须在 item 添加到树后）
            # 列 4: 启用 checkbox
            enabled_wrapper, enabled_cb = _centered_checkbox(k.get("enabled", True))
            enabled_cb.stateChanged.connect(
                lambda state, kid=key_id: self._on_toggle_enabled(kid, bool(state))
            )
            self._tree.setItemWidget(key_item, COL_ENABLED, enabled_wrapper)
            # 列 7: 操作 2×2
            ops_widget = self._build_key_ops_widget(key_id)
            self._tree.setItemWidget(key_item, COL_OPS, ops_widget)

            # 添加 model 子项
            models = k.get("models", [])
            for m in models:
                if not isinstance(m, dict) or not m.get("name"):
                    continue
                model_item = QTreeWidgetItem()
                model_name = m.get("name", "")
                # v11：display_name 优先（缺失时 fallback 到 model_name）
                model_display = (m.get("display_name") or "").strip() or model_name
                model_scope = m.get("scope", ["llm"])
                # v8: tier (1-5)，未设置时默认 3
                try:
                    model_tier = int(m.get("tier", 3))
                    if not 1 <= model_tier <= 5:
                        model_tier = 3
                except (TypeError, ValueError):
                    model_tier = 3
                model_item.setData(0, Qt.ItemDataRole.UserRole,
                                   {"type": "model", "id": key_id, "name": model_name})
                # 列 0: model 展示名（v11：优先用 display_name；若与 name 不同则 tooltip 标注）
                display_label = f"  └ {model_display}"
                # 若 display_name 与 name 不同，在文本后追加 (name) 提示连接名
                if model_display != model_name:
                    display_label += f"  ({model_name})"
                model_item.setText(COL_NAME, display_label)
                # tooltip 同时展示连接名和展示名
                if model_display != model_name:
                    model_item.setToolTip(COL_NAME,
                        f"展示名: {model_display}\n连接名: {model_name}")
                else:
                    model_item.setToolTip(COL_NAME, model_name)
                # 列 1: 输入输出类型
                types_text = _scope_to_types(model_scope)
                model_item.setText(COL_URL, types_text)
                model_item.setToolTip(COL_URL, f"scope: {', '.join(model_scope)}")
                # 列 2: 信息安全
                privacy_text = _privacy_label(privacy)
                model_item.setText(COL_KEY, privacy_text)
                if privacy:
                    model_item.setForeground(COL_KEY, QBrush(QColor(tokens.WARNING_TEXT)))
                else:
                    model_item.setForeground(COL_KEY, QBrush(QColor(tokens.SUCCESS_TEXT)))
                # 列 3: 允许用途
                allowed = k.get("allowed_uses", [])
                uses_text = _allowed_uses_label(allowed)
                model_item.setText(COL_CONCURRENCY, uses_text)
                model_item.setTextAlignment(COL_CONCURRENCY, Qt.AlignmentFlag.AlignCenter)
                model_item.setToolTip(COL_CONCURRENCY,
                                       "\n".join(allowed) if allowed else "未限制（全部 use_case 可用）")
                # 列 4: 模型独立启用开关；父 Key 仍作为总开关
                model_wrapper, model_cb = _centered_checkbox(m.get("enabled", True),
                    "此开关仅控制当前模型；父 Key 开关关闭时所有模型都会停用")
                model_cb.setToolTip(
                    "此开关仅控制当前模型；父 Key 开关关闭时所有模型都会停用"
                )
                model_cb.stateChanged.connect(
                    lambda state, kid=key_id, name=model_name:
                        self._on_toggle_model_enabled(kid, name, bool(state))
                )
                # 列 5: Tier（model 行显示 tier 替代 "—"）
                tier_label, tier_color = _tier_label(model_tier)
                model_item.setText(COL_STATUS, tier_label)
                model_item.setTextAlignment(COL_STATUS, Qt.AlignmentFlag.AlignCenter)
                model_item.setForeground(COL_STATUS, QBrush(QColor(tier_color)))
                model_item.setToolTip(COL_STATUS,
                    f"Model Tier: {model_tier}\n"
                    f"1=最轻量  2=轻量  3=默认  4=较强  5=最强大\n"
                    f"use_case.default_tier 决定选用哪个 tier 的 model"
                )
                key_item.addChild(model_item)
                model_item.setSizeHint(COL_NAME, QSize(0, 40))
                # cellWidget 必须在添加到父后设置
                self._tree.setItemWidget(model_item, COL_ENABLED, model_wrapper)

            # 默认展开
            key_item.setExpanded(True)

        self._update_summary()

    def _build_key_ops_widget(self, key_id: str) -> QWidget:
        """key 行操作列：紧凑按钮（测试/编辑/复制/删除）。"""
        w = QWidget()
        w.setMinimumWidth(132)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        test_btn = icon_button("check-circle", "测试 Key", color=tokens.SUCCESS_TEXT)
        test_btn.clicked.connect(lambda checked, kid=key_id: self._on_test_clicked(kid))
        layout.addWidget(test_btn)

        edit_btn = icon_button("pencil", "编辑 Key")
        edit_btn.clicked.connect(lambda checked, kid=key_id: self._on_edit_key_clicked(kid))
        layout.addWidget(edit_btn)

        copy_btn = icon_button("copy", "复制 Key")
        copy_btn.clicked.connect(lambda checked, kid=key_id: self._on_copy_clicked(kid))
        layout.addWidget(copy_btn)

        del_btn = icon_button("trash", "删除 Key", color=tokens.ICON_DANGER)
        del_btn.clicked.connect(lambda checked, kid=key_id: self._on_delete_clicked(kid))
        layout.addWidget(del_btn)
        return w

    def _update_summary(self) -> None:
        total = len(self._keys)
        ok = sum(1 for k in self._keys if _status_key(k.get("status", {})) == "ok")
        fail = sum(1 for k in self._keys if _status_key(k.get("status", {})) == "fail")
        unknown = total - ok - fail
        disabled = sum(1 for k in self._keys if k.get("enabled", True) is False)
        summary = f"共 {total} 个 key（{ok} 可用 / {fail} 失败 / {unknown} 未测"
        if disabled:
            summary += f" / {disabled} 已禁用"
        summary += "）"
        self._summary_label.setText(summary)

    def _on_toggle_enabled(self, key_id: str, enabled: bool) -> None:
        """切换 Key 总开关，不改写各模型的独立开关。异步 PUT，不阻塞 UI。"""
        worker = self._make_worker("put", f"/apikey/keys/{key_id}", json={"enabled": enabled})
        worker.done.connect(lambda resp: self._on_toggle_enabled_done(key_id, enabled, resp))
        worker.failed.connect(lambda _err: self._on_toggle_enabled_done(key_id, enabled, None))
        worker.start()

    def _on_toggle_enabled_done(self, key_id: str, enabled: bool, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", f"{'启用' if enabled else '禁用'}失败：后端返回错误")
            self._reload_keys_async()
            return
        # 更新本地状态
        for k in self._keys:
            if k.get("id") == key_id:
                k["enabled"] = enabled
                break
        # 只同步顶层 Key 开关；模型开关保留各自状态
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            data = top.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("id") == key_id:
                # 顶层 key 行的 checkbox
                key_widget = self._tree.itemWidget(top, COL_ENABLED)
                key_cb = key_widget.findChild(QCheckBox) if key_widget else None
                if key_cb:
                    key_cb.blockSignals(True)
                    key_cb.setChecked(enabled)
                    key_cb.blockSignals(False)
                break
        self._update_summary()

    def _on_toggle_model_enabled(
        self, key_id: str, model_name: str, enabled: bool
    ) -> None:
        """只切换一个模型，提交完整 models 列表以保持 API 的部分更新语义。异步 PUT。"""
        key_record = next((k for k in self._keys if k.get("id") == key_id), None)
        if key_record is None:
            self._reload_keys_async()
            return

        models = []
        found = False
        for model in key_record.get("models", []):
            updated = dict(model)
            if updated.get("name") == model_name:
                updated["enabled"] = enabled
                found = True
            models.append(updated)
        if not found:
            self._reload_keys_async()
            return

        worker = self._make_worker("put", f"/apikey/keys/{key_id}", json={"models": models})
        worker.done.connect(
            lambda resp, kr=key_record, m=models: self._on_toggle_model_done(kr, m, resp, enabled)
        )
        worker.failed.connect(
            lambda _err, kr=key_record, m=models: self._on_toggle_model_done(kr, m, None, enabled)
        )
        worker.start()

    def _on_toggle_model_done(
        self, key_record: dict, models: list, resp: dict | None, enabled: bool
    ) -> None:
        if resp is None:
            QMessageBox.warning(
                self, "失败", f"{'启用' if enabled else '禁用'}模型失败：后端返回错误"
            )
            self._reload_keys_async()
            return
        key_record["models"] = models
        self._update_summary()

    # —— 筛选 ——

    def _apply_filters(self) -> None:
        scope_cbs = [
            ("llm", self._scope_llm_cb),
            ("vl", self._scope_vl_cb),
            ("aigc_image", self._scope_aigc_image_cb),
            ("aigc_video", self._scope_aigc_video_cb),
        ]
        only_available = self._only_available_cb.isChecked()
        any_scope_checked = any(cb.isChecked() for _, cb in scope_cbs)

        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            k = self._keys[i] if i < len(self._keys) else {}
            # scope 筛选
            hidden = False
            if any_scope_checked:
                models = k.get("models", [])
                scope_set: set[str] = set()
                for m in models:
                    for s in m.get("scope", ["llm"]):
                        scope_set.add(s)
                has_match = any(cb.isChecked() and scope in scope_set
                               for scope, cb in scope_cbs)
                if not has_match:
                    hidden = True
            # 仅可用筛选
            if not hidden and only_available and _status_key(k.get("status", {})) != "ok":
                hidden = True
            top.setHidden(hidden)

    # —— 双击/右键 ——

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("type") == "key":
            self._on_edit_key_clicked(data["id"])
        elif data.get("type") == "model":
            self._on_batch_edit_models_clicked(data["id"])

    def _on_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        key_id = data.get("id")
        if not key_id:
            return
        menu = QMenu(self)
        if data.get("type") == "key":
            act_edit = QAction("编辑 key 信息", self)
            act_edit.triggered.connect(lambda: self._on_edit_key_clicked(key_id))
            menu.addAction(act_edit)
            act_batch = QAction("批量编辑 model", self)
            act_batch.triggered.connect(lambda: self._on_batch_edit_models_clicked(key_id))
            menu.addAction(act_batch)
            menu.addSeparator()
            act_test = QAction("测试", self)
            act_test.triggered.connect(lambda: self._on_test_clicked(key_id))
            menu.addAction(act_test)
            act_copy = QAction("复制", self)
            act_copy.triggered.connect(lambda: self._on_copy_clicked(key_id))
            menu.addAction(act_copy)
            menu.addSeparator()
            act_del = QAction("删除", self)
            act_del.triggered.connect(lambda: self._on_delete_clicked(key_id))
            menu.addAction(act_del)
        elif data.get("type") == "model":
            act_batch = QAction("批量编辑 model", self)
            act_batch.triggered.connect(lambda: self._on_batch_edit_models_clicked(key_id))
            menu.addAction(act_batch)
            act_edit = QAction("编辑父 key 信息", self)
            act_edit.triggered.connect(lambda: self._on_edit_key_clicked(key_id))
            menu.addAction(act_edit)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # —— 操作回调 ——

    def _on_add_clicked(self) -> None:
        result = KeyForm.open_dialog(self, edit_mode="full")
        if result is None:
            return
        worker = self._make_worker("post", "/apikey/keys", json=result)
        worker.done.connect(self._on_add_done)
        worker.failed.connect(lambda _err: self._on_add_done(None))
        worker.start()

    def _on_add_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "添加失败：后端返回错误")
            return
        self._reload_keys_async()

    def _on_edit_key_clicked(self, key_id: str) -> None:
        """编辑 key 信息（不含 model）。异步 GET → dialog → 异步 PUT。"""
        worker = self._make_worker("get", f"/apikey/keys/{key_id}", params={"unmasked": "true"})
        worker.done.connect(lambda k: self._on_edit_key_loaded(key_id, k))
        worker.failed.connect(lambda _err: self._on_edit_key_loaded(key_id, None))
        worker.start()

    def _on_edit_key_loaded(self, key_id: str, k: dict | None) -> None:
        if k is None:
            QMessageBox.warning(self, "失败", "获取 key 详情失败")
            return
        result = KeyForm.open_dialog(self, existing=k, edit_mode="key_only")
        if result is None:
            return
        worker = self._make_worker("put", f"/apikey/keys/{key_id}", json=result)
        worker.done.connect(self._on_edit_key_done)
        worker.failed.connect(lambda _err: self._on_edit_key_done(None))
        worker.start()

    def _on_edit_key_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "更新失败：后端返回错误")
            return
        self._reload_keys_async()

    def _on_batch_edit_models_clicked(self, key_id: str) -> None:
        """批量编辑一个 key 的所有 model。异步 GET → dialog → 异步 PUT。"""
        worker = self._make_worker("get", f"/apikey/keys/{key_id}", params={"unmasked": "true"})
        worker.done.connect(lambda k: self._on_batch_edit_loaded(key_id, k))
        worker.failed.connect(lambda _err: self._on_batch_edit_loaded(key_id, None))
        worker.start()

    def _on_batch_edit_loaded(self, key_id: str, k: dict | None) -> None:
        if k is None:
            QMessageBox.warning(self, "失败", "获取 key 详情失败")
            return
        existing_models = k.get("models", [])
        if not existing_models:
            QMessageBox.information(self, "提示", "此 key 下没有 model，请通过「编辑 key 信息」添加")
            return
        new_models = _BatchEditModelsDialog.open_dialog(self, existing_models=existing_models)
        if new_models is None:
            return
        worker = self._make_worker("put", f"/apikey/keys/{key_id}", json={"models": new_models})
        worker.done.connect(self._on_batch_edit_done)
        worker.failed.connect(lambda _err: self._on_batch_edit_done(None))
        worker.start()

    def _on_batch_edit_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "更新失败：后端返回错误")
            return
        self._reload_keys_async()

    def _on_copy_clicked(self, key_id: str) -> None:
        """复制 key：预填原 key 字段（含 models），key 留空。异步 GET → dialog → 异步 POST。"""
        worker = self._make_worker("get", f"/apikey/keys/{key_id}", params={"unmasked": "true"})
        worker.done.connect(lambda k: self._on_copy_loaded(key_id, k))
        worker.failed.connect(lambda _err: self._on_copy_loaded(key_id, None))
        worker.start()

    def _on_copy_loaded(self, key_id: str, k: dict | None) -> None:
        if k is None:
            QMessageBox.warning(self, "失败", "获取 key 详情失败")
            return
        existing = dict(k)
        existing["key"] = ""
        base_label = k.get("label", "")
        existing["label"] = f"{base_label} (副本)" if base_label else ""
        existing.pop("id", None)
        existing.pop("status", None)
        result = KeyForm.open_dialog(self, existing=existing, edit_mode="full")
        if result is None:
            return
        worker = self._make_worker("post", "/apikey/keys", json=result)
        worker.done.connect(self._on_copy_done)
        worker.failed.connect(lambda _err: self._on_copy_done(None))
        worker.start()

    def _on_copy_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "复制失败：后端返回错误")
            return
        self._reload_keys_async()

    def _on_delete_clicked(self, key_id: str) -> None:
        label = ""
        for k in self._keys:
            if k.get("id") == key_id:
                label = k.get("label", key_id)
                break
        btn = QMessageBox.question(
            self, "确认删除",
            f"确定删除密钥「{label}」吗？\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        worker = self._make_worker("delete", f"/apikey/keys/{key_id}")
        worker.done.connect(self._on_delete_done)
        worker.failed.connect(lambda _err: self._on_delete_done(None))
        worker.start()

    def _on_delete_done(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "删除失败：后端返回错误")
            return
        self._reload_keys_async()

    def _on_test_clicked(self, key_id: str) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        from PySide6.QtCore import QThread

        class TestThread(QThread):
            def __init__(self, http, kid):
                super().__init__()
                self._http = http
                self._kid = kid
                self.result = None

            def run(self):
                self.result = self._http.post(f"/apikey/keys/{self._kid}/test")

        self._test_thread = TestThread(self._http, key_id)
        self._test_thread.finished.connect(
            lambda: self._on_single_test_done(key_id, getattr(self._test_thread, "result", None))
        )
        self._test_thread.start()
        # 更新状态为"测试中"
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            data = top.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("id") == key_id:
                top.setText(COL_STATUS, "测试中...")
                top.setForeground(COL_STATUS, QBrush(QColor(tokens.WARNING_TEXT)))
                break

    def _on_single_test_done(self, key_id: str, result: dict | None) -> None:
        if result is None:
            QMessageBox.warning(self, "失败", "测试失败：后端返回错误")
            self._reload_keys_async()
            return
        status = result.get("status", {})
        if not status.get("works", False):
            detail = status.get("last_check_detail", "无详情")
            label = ""
            for k in self._keys:
                if k.get("id") == key_id:
                    label = k.get("label", key_id)
                    break
            QMessageBox.warning(
                self, f"测试失败 — {label}",
                f"状态: 失败\n"
                f"时间: {status.get('last_health_check', '')}\n\n"
                f"详情:\n{detail}",
            )
        self._reload_keys_async()

    def _on_health_check_all_clicked(self) -> None:
        worker = self._make_worker("post", "/apikey/keys/health-check-all")
        worker.done.connect(self._on_health_check_started)
        worker.failed.connect(lambda _err: self._on_health_check_started(None))
        worker.start()

    def _on_health_check_started(self, resp: dict | None) -> None:
        if resp is None:
            QMessageBox.warning(self, "失败", "启动健康检查失败")
            return
        self._poll_count = 0
        self._health_check_btn.setEnabled(False)
        self._health_check_btn.setText("测试中...")
        self._poll_timer.start(3000)

    def _on_poll_timeout(self) -> None:
        self._poll_count += 1
        self._reload_keys_async()
        if self._poll_count >= 10:
            self._stop_polling()

    def _stop_polling(self) -> None:
        self._poll_timer.stop()
        self._health_check_btn.setEnabled(True)
        self._health_check_btn.setText("全部测试")

    # —— 脱敏切换 ——

    def _on_toggle_unmasked(self) -> None:
        self._unmasked = self._unmasked_btn.isChecked()
        # 不持久化展示状态，每次启动默认隐藏（安全考虑）
        self._unmasked_btn.setText("隐藏key细节" if self._unmasked else "展示key")
        self._reload_keys_async()
