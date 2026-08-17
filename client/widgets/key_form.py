"""KeyForm — 添加/编辑 API Key 的模态对话框

字段与 server/apikey.py 的 KeyCreateRequest/KeyUpdateRequest 对齐：
label / base_url / key / models / max_concurrency / privacy_warning / enabled / allowed_uses

v8 schema: models 为 list[dict]，每个 model 含 name + scope + tier(1-5).
  - tier 是 1-5 数字（1=最轻量，5=最强大），用于 use_case → tier 硬匹配 key/model。
  - 同 tier 内的 model 可互为 fallback（model 偏好是软匹配）。

edit_mode 控制显示哪些字段：
- "full"（默认，新建/复制）：所有字段含 models 列表
- "key_only"（编辑 key 信息）：不含 models（model 由批量编辑对话框单独管理）

USE_CASE_DEFINITIONS 启动时由 load_use_cases() 从后端 GET /apikey/use-cases 拉取填充。

用法：
    # 新建
    result = KeyForm.open_dialog(parent, edit_mode="full")
    # 编辑 key 信息（不含 model）
    result = KeyForm.open_dialog(parent, edit_mode="key_only", existing=key_dict)
    # 批量编辑 model
    new_models = _BatchEditModelsDialog.open_dialog(parent, existing_models=key["models"])
"""


from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

# 用途定义：启动时由 KeyForm.load_use_cases() 从后端 GET /apikey/use-cases 拉取填充
# 格式: [(id, 显示名, 说明, scope, sensitive), ...]
USE_CASE_DEFINITIONS: list[tuple] = []


# scope → 输入输出类型描述
_SCOPE_TYPE_MAP = {
    "llm": "文本→文本",
    "vl": "文本+图像→文本",
    "aigc_image": "文本→图像",
    "aigc_video": "文本→视频",
}


def _scope_to_types(scope: list[str]) -> str:
    """根据 scope 推断输入输出类型，多 scope 用 / 分隔"""
    if not scope:
        return "—"
    parts = [_SCOPE_TYPE_MAP.get(s, s) for s in scope]
    return " / ".join(parts)


class KeyForm(QDialog):
    """添加/编辑 key 的模态对话框。

    构造函数：
        KeyForm(parent, existing=None, edit_mode="full")
        - existing: dict，编辑模式预填的 key record
        - edit_mode: "full"（含 models）/ "key_only"（不含 models）
    """

    @classmethod
    def load_use_cases(cls, http) -> None:
        """启动时从 GET /apikey/use-cases 拉取填充 USE_CASE_DEFINITIONS"""
        global USE_CASE_DEFINITIONS
        try:
            resp = http.get("/apikey/use-cases")
            if resp and resp.get("use_cases"):
                USE_CASE_DEFINITIONS = [
                    (uc["name"], uc["name"], uc["desc"], uc["scope"], uc["sensitive"])
                    for uc in resp["use_cases"]
                ]
        except Exception:
            pass

    def __init__(self, parent=None,
                 existing: dict | None = None,
                 edit_mode: str = "full"):
        super().__init__(parent)
        self.setWindowTitle("编辑密钥" if existing else "添加密钥")
        self.setMinimumSize(920, 680)
        self.resize(1040, 840)
        self._existing = existing or {}
        self._edit_mode = edit_mode
        self._result: dict | None = None

        self._build_ui()
        self._fill_from_existing()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # QScrollArea 包裹所有内容，防止对话框过长无法显示
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # Label
        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("如：ModelScope 主号")
        form.addRow("标签 *", self._label_edit)

        # Base URL
        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self._base_url_edit)

        # v11：连接协议（默认 openai，支持 anthropic）
        self._protocol_combo = QComboBox()
        self._protocol_combo.addItems(["openai", "anthropic"])
        self._protocol_combo.setToolTip(
            "openai: OpenAI 兼容协议（POST /chat/completions + Bearer token，默认）\n"
            "anthropic: Anthropic Claude 协议（POST /v1/messages + x-api-key + anthropic-version）\n"
            "调用时按此协议分发，UI 展示用 model.display_name"
        )
        form.addRow("连接协议", self._protocol_combo)

        # API Key
        key_row = QHBoxLayout()
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("sk-... / ms-...")
        key_row.addWidget(self._key_edit)
        self._toggle_key_btn = QPushButton("显示")
        self._toggle_key_btn.setFixedWidth(36)
        self._toggle_key_btn.setCheckable(True)
        self._toggle_key_btn.clicked.connect(self._on_toggle_key_visible)
        key_row.addWidget(self._toggle_key_btn)
        form.addRow("API Key *", key_row)

        # Models（仅 full 模式显示）
        if self._edit_mode == "full":
            models_container = QVBoxLayout()
            models_container.setSpacing(4)
            models_container.setContentsMargins(0, 0, 0, 0)
            self._models_list = QListWidget()
            self._models_list.setMinimumHeight(180)
            self._models_list.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            models_container.addWidget(self._models_list)
            model_input_row = QHBoxLayout()
            model_input_row.setSpacing(4)
            self._model_input = QLineEdit()
            self._model_input.setPlaceholderText("如：deepseek-v4-flash，回车添加")
            self._model_input.returnPressed.connect(self._on_add_model)
            model_input_row.addWidget(self._model_input, 1)
            add_model_btn = QPushButton("+")
            add_model_btn.setFixedSize(34, 30)
            add_model_btn.clicked.connect(self._on_add_model)
            model_input_row.addWidget(add_model_btn)
            del_model_btn = QPushButton("−")
            del_model_btn.setFixedSize(34, 30)
            del_model_btn.clicked.connect(self._on_del_model)
            model_input_row.addWidget(del_model_btn)
            clear_models_btn = QPushButton("清空")
            clear_models_btn.setMinimumWidth(64)
            clear_models_btn.clicked.connect(self._on_clear_models)
            model_input_row.addWidget(clear_models_btn)
            models_container.addLayout(model_input_row)
            models_wrapper = QWidget()
            models_wrapper.setLayout(models_container)
            form.addRow("模型列表 *", models_wrapper)

        # Max Concurrency（per-key 生效，非 provider 级）
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 10)
        self._concurrency_spin.setValue(3)
        self._concurrency_spin.setToolTip(
            "per-key 生效：此 key 在 pool 中的最大并发请求数。\n"
            "非 provider 级——同一 provider 的不同 key 各自独立计数。\n"
            "config.toml 无此字段，仅在 keys.json 中配置。"
        )
        form.addRow("最大并发", self._concurrency_spin)

        # Privacy Warning（冲突优先级说明）
        self._privacy_edit = QTextEdit()
        self._privacy_edit.setMinimumHeight(64)
        self._privacy_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._privacy_edit.setPlaceholderText("如：免费模型数据可能被用于训练，请勿传输敏感信息")
        self._privacy_edit.setToolTip(
            "冲突优先级——privacy_warning 优先于 allowed_uses：\n"
            "• 敏感 use_case + privacy_warning 非空 → 拒绝此 key（除非全局开关 allow_privacy_warning_for_sensitive=true）\n"
            "• 非 sensitive use_case → privacy_warning 不影响，由 allowed_uses 决定\n"
            "即：即使 allowed_uses 勾选了敏感用途，只要 privacy_warning 非空且开关关闭，敏感用途仍会跳过此 key"
        )
        form.addRow("隐私警告", self._privacy_edit)

        # Enabled
        self._enabled_cb = QCheckBox("启用（取消勾选则所有端点跳过此 key）")
        self._enabled_cb.setChecked(True)
        form.addRow("启用", self._enabled_cb)

        # Allowed Uses
        form.addRow("允许用途", self._build_allowed_uses_widget())

        # Pool Key（共享上游池标识）
        self._pool_key_edit = QLineEdit()
        self._pool_key_edit.setPlaceholderText("留空 = 独立池（按 key 个体计数）")
        self._pool_key_edit.setToolTip(
            "共享上游池标识：同一 pool_key 的多个 key 共享上游配额池。\n"
            "留空 = 独立池（每个 key 各自计数并发和限流）。\n"
            "用途：同一供应商的多个 key 共享同一上游配额时，设置相同的 pool_key 避免超配。"
        )
        form.addRow("Pool Key", self._pool_key_edit)

        content_layout.addLayout(form)

        # Pool 策略（折叠区域，留空 = 用全局默认 [llm.pool]）
        content_layout.addWidget(self._build_pool_section())

        # Vision 参数（仅对含 VL scope model 的 key 生效）
        content_layout.addWidget(self._build_vision_section())

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # 按钮栏
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        ok_btn = button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("确定")
        cancel_btn = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText("取消")
        layout.addWidget(button_box)

    def _build_allowed_uses_widget(self) -> QWidget:
        """允许用途：说明 + 可勾选 QListWidget + 全选/清空按钮"""
        container = QVBoxLayout()
        container.setSpacing(4)
        container.setContentsMargins(0, 0, 0, 0)

        hint = QLabel(
            "全不选 = 全部允许；勾选后仅允许选中的用途\n"
            "冲突说明：敏感用途（⚠）受 privacy_warning + 全局开关影响——\n"
            "若 privacy_warning 非空且开关关闭，即使勾选了敏感用途也会跳过此 key"
        )
        set_text_role(hint, "caption")
        hint.setWordWrap(True)
        container.addWidget(hint)

        self._uses_list = QListWidget()
        self._uses_list.setMinimumHeight(220)
        self._uses_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        for uc_id, uc_name, uc_desc, uc_scope, uc_sensitive in USE_CASE_DEFINITIONS:
            label_text = f"{uc_name}  ({uc_id})"
            if uc_sensitive:
                label_text += "  ⚠ 敏感"
            item = QListWidgetItem(label_text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, uc_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, uc_scope)
            item.setData(Qt.ItemDataRole.UserRole + 2, uc_sensitive)
            tip = f"{uc_id}\n{uc_desc}\nscope: {uc_scope}"
            if uc_sensitive:
                tip += "\n（敏感用途 — 禁用带 privacy_warning 的 key）"
            item.setToolTip(tip)
            if uc_sensitive:
                item.setForeground(QColor(tokens.WARNING_TEXT))
            self._uses_list.addItem(item)
        container.addWidget(self._uses_list)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        select_all_btn = QPushButton("全选")
        select_all_btn.setMinimumWidth(64)
        select_all_btn.clicked.connect(lambda: self._set_all_uses_checked(True))
        btn_row.addWidget(select_all_btn)
        clear_all_btn = QPushButton("清空")
        clear_all_btn.setMinimumWidth(64)
        clear_all_btn.clicked.connect(lambda: self._set_all_uses_checked(False))
        btn_row.addWidget(clear_all_btn)
        btn_row.addStretch()
        container.addLayout(btn_row)

        wrapper = QWidget()
        wrapper.setLayout(container)
        return wrapper

    def _build_pool_section(self) -> QGroupBox:
        """Pool 策略折叠区域：7 个 per-key 重试/限流参数。

        未勾选 = 用全局默认 [llm.pool]（pool 段为空字典）。
        勾选后可覆盖各参数，保存时收集为 pool dict。
        """
        self._pool_group = QGroupBox("Pool 策略（per-key 重试/限流）")
        self._pool_group.setCheckable(True)
        self._pool_group.setChecked(False)
        self._pool_group.setToolTip(
            "per-key pool 策略：覆盖全局默认 [llm.pool] 的重试和限流参数。\n"
            "未勾选 = 用全局默认（pool 段为空）。\n"
            "勾选 = 各字段填入全局默认值，可按需修改。\n"
            "冲突优先级：key 级 pool 段 > config.toml [llm.pool] 全局默认"
        )
        self._pool_group.toggled.connect(self._on_pool_group_toggled)

        form = QFormLayout(self._pool_group)
        form.setSpacing(4)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 全局默认值（与 config.toml [llm.pool] 一致）
        _POOL_DEFAULTS = {
            "retry_count": (3, 0, 20, "重试次数（0=不重试）"),
            "retry_base_delay": (2.0, 0.0, 60.0, "重试基础延迟（秒）"),
            "retry_max_delay": (30.0, 0.0, 600.0, "重试最大延迟（秒）"),
            "retry_jitter": (0.2, 0.0, 1.0, "重试抖动比例（0-1，随机化延迟避免惊群）"),
            "rate_limit_cooldown_seconds": (60.0, 0.0, 3600.0, "429 冷却时间（秒）"),
            "rate_limit_backoff_multiplier": (2.0, 1.0, 10.0, "429 退避倍数（每次冷却 ×此值）"),
            "rate_limit_max_cooldown": (300.0, 0.0, 3600.0, "429 最大冷却时间（秒）"),
        }

        self._pool_spins: dict[str, QSpinBox | QDoubleSpinBox] = {}
        for key, (default, vmin, vmax, tooltip) in _POOL_DEFAULTS.items():
            if isinstance(default, int):
                spin = QSpinBox()
                spin.setRange(int(vmin), int(vmax))
                spin.setValue(int(default))
            else:
                spin = QDoubleSpinBox()
                spin.setRange(vmin, vmax)
                spin.setSingleStep(0.1)
                spin.setDecimals(1)
                spin.setValue(float(default))
            spin.setToolTip(tooltip)
            self._pool_spins[key] = spin
            form.addRow(key, spin)

        # 初始状态：未勾选时禁用内部控件
        self._on_pool_group_toggled(False)
        return self._pool_group

    def _on_pool_group_toggled(self, checked: bool) -> None:
        """Pool 策略折叠：未勾选时禁用内部控件"""
        for spin in self._pool_spins.values():
            spin.setEnabled(checked)

    def _build_vision_section(self) -> QGroupBox:
        """Vision 参数区域：VL provider 级参数（仅对含 VL scope model 的 key 生效）。

        3 个参数：max_concurrency / timeout / rate_limit_cooldown。
        缺失时后端使用默认值 (1/60/60.0)。
        """
        self._vision_group = QGroupBox("Vision 参数（仅 VL scope key 生效）")
        self._vision_group.setCheckable(True)
        self._vision_group.setChecked(False)
        self._vision_group.setToolTip(
            "VL provider 级参数：仅当 key 含 vl scope model 时生效。\n"
            "未勾选 = 用默认值 (max_concurrency=1, timeout=60s, rate_limit_cooldown=60s)。\n"
            "勾选 = 可自定义各参数。"
        )
        self._vision_group.toggled.connect(self._on_vision_group_toggled)

        form = QFormLayout(self._vision_group)
        form.setSpacing(4)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        _VISION_DEFAULTS = {
            "max_concurrency": (1, 1, 10, "VL 请求最大并发数（默认 1，VL 请求较重不建议太高）"),
            "timeout": (60, 5, 600, "VL 请求超时（秒，默认 60）"),
            "rate_limit_cooldown": (60.0, 0.0, 3600.0, "VL 429 冷却时间（秒，默认 60）"),
        }

        self._vision_spins: dict[str, QSpinBox | QDoubleSpinBox] = {}
        for key, (default, vmin, vmax, tooltip) in _VISION_DEFAULTS.items():
            if isinstance(default, int):
                spin = QSpinBox()
                spin.setRange(int(vmin), int(vmax))
                spin.setValue(int(default))
            else:
                spin = QDoubleSpinBox()
                spin.setRange(vmin, vmax)
                spin.setSingleStep(0.1)
                spin.setDecimals(1)
                spin.setValue(float(default))
            spin.setToolTip(tooltip)
            self._vision_spins[key] = spin
            form.addRow(key, spin)

        self._on_vision_group_toggled(False)
        return self._vision_group

    def _on_vision_group_toggled(self, checked: bool) -> None:
        """Vision 参数折叠：未勾选时禁用内部控件"""
        for spin in self._vision_spins.values():
            spin.setEnabled(checked)

    def _build_model_item(self, name: str, scope: list[str],
                          tier: int = 3,
                          enabled: bool = True,
                          display_name: str = "") -> tuple[QListWidgetItem, QWidget]:
        """构建一个 model 条目：name + 展示名 + 独立启用开关 + Tier + scope

        Args:
            tier: 1-5 数字，1=最轻量，5=最强大。默认 3。
            display_name: v11 展示名（UI 显示用，缺失则用 name）
        """
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)
        # v11：name 改为可编辑输入框（用户可改连接名）+ 展示名输入框
        name_label = QLabel("连接名:")
        set_text_role(name_label, "secondary")
        layout.addWidget(name_label)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("连接名（调用时用）")
        name_edit.setMinimumWidth(180)
        layout.addWidget(name_edit)
        dn_label = QLabel("展示名:")
        set_text_role(dn_label, "secondary")
        layout.addWidget(dn_label)
        display_name_edit = QLineEdit(display_name)
        display_name_edit.setPlaceholderText("展示名（UI 显示，留空=用连接名）")
        display_name_edit.setMinimumWidth(180)
        layout.addWidget(display_name_edit)
        enabled_cb = QCheckBox("启用")
        enabled_cb.setMinimumHeight(28)
        enabled_cb.setChecked(enabled)
        enabled_cb.setToolTip("仅控制此模型；Key 总开关位于下方")
        layout.addWidget(enabled_cb)
        # Tier 下拉（v8: 1-5）
        tier_label = QLabel("T")
        set_text_role(tier_label, "secondary")
        layout.addWidget(tier_label)
        tier_combo = QComboBox()
        tier_combo.addItems(["1", "2", "3", "4", "5"])
        tier_combo.setToolTip(
            "Tier 1=最轻量(免费/简单任务)  2=轻量  3=默认  4=较强  5=最强大(复杂推理/VL/AIGC)\n"
            "use_case 的 default_tier 决定选哪个 tier 的 key/model"
        )
        try:
            t = int(tier)
            if not 1 <= t <= 5:
                t = 3
        except (TypeError, ValueError):
            t = 3
        tier_combo.setCurrentIndex(t - 1)
        tier_combo.setFixedWidth(56)
        tier_combo.setMinimumHeight(30)
        layout.addWidget(tier_combo)
        scope_defs = [("llm", "LLM"), ("vl", "VL"),
                      ("aigc_image", "AIGC图"), ("aigc_video", "AIGC视频")]
        scope_cbs: dict[str, QCheckBox] = {}
        for scope_name, label in scope_defs:
            cb = QCheckBox(label)
            cb.setMinimumHeight(28)
            cb.setChecked(scope_name in scope)
            layout.addWidget(cb)
            scope_cbs[scope_name] = cb
        item.setData(Qt.ItemDataRole.UserRole, {
            "name_label": name_edit,  # v11: 改为 QLineEdit
            "display_name_edit": display_name_edit,  # v11: 新增
            "scope_cbs": scope_cbs,
            "tier_combo": tier_combo,
            "enabled_cb": enabled_cb,
        })
        item.setSizeHint(widget.sizeHint())
        return item, widget

    def _fill_from_existing(self) -> None:
        if not self._existing:
            return
        self._label_edit.setText(self._existing.get("label", ""))
        self._base_url_edit.setText(self._existing.get("base_url", ""))
        # v11：读取 protocol（默认 openai）
        protocol = (self._existing.get("protocol") or "openai").lower()
        if protocol not in ("openai", "anthropic"):
            protocol = "openai"
        idx = self._protocol_combo.findText(protocol)
        if idx >= 0:
            self._protocol_combo.setCurrentIndex(idx)
        # 编辑模式下 key 字段：脱敏值清空，让用户重填；非脱敏保留
        key_val = self._existing.get("key", "")
        if isinstance(key_val, str) and "****" in key_val:
            self._key_edit.setText("")
            self._key_edit.setPlaceholderText(f"当前: {key_val}（输入新值覆盖）")
        else:
            self._key_edit.setText(key_val)

        # models（仅 full 模式）
        if self._edit_mode == "full":
            models = self._existing.get("models", [])
            self._models_list.clear()
            for m in models:
                if not isinstance(m, dict) or not m.get("name"):
                    continue
                item, widget = self._build_model_item(
                    m["name"], m.get("scope", ["llm"]),
                    tier=m.get("tier", 3), enabled=m.get("enabled", True),
                    display_name=(m.get("display_name") or "").strip())  # v11
                self._models_list.addItem(item)
                self._models_list.setItemWidget(item, widget)

        self._concurrency_spin.setValue(int(self._existing.get("max_concurrency", 3)))

        self._privacy_edit.setPlainText(self._existing.get("privacy_warning", ""))
        self._enabled_cb.setChecked(self._existing.get("enabled", True))

        # 允许用途勾选
        allowed = self._existing.get("allowed_uses", [])
        if isinstance(allowed, list):
            allowed_set = set(allowed)
            for i in range(self._uses_list.count()):
                item = self._uses_list.item(i)
                uc_id = item.data(Qt.ItemDataRole.UserRole)
                if uc_id in allowed_set:
                    item.setCheckState(Qt.CheckState.Checked)

        # v14: pool_key
        self._pool_key_edit.setText(self._existing.get("pool_key", "") or "")

        # v14: pool 段（非空时勾选并填入值）
        existing_pool = self._existing.get("pool", {})
        if isinstance(existing_pool, dict) and existing_pool:
            self._pool_group.setChecked(True)
            for key, spin in self._pool_spins.items():
                if key in existing_pool:
                    val = existing_pool[key]
                    if isinstance(spin, QSpinBox):
                        spin.setValue(int(val))
                    else:
                        spin.setValue(float(val))

        # v9: vision 段（非空时勾选并填入值）
        existing_vision = self._existing.get("vision", {})
        if isinstance(existing_vision, dict) and existing_vision:
            self._vision_group.setChecked(True)
            for key, spin in self._vision_spins.items():
                if key in existing_vision:
                    val = existing_vision[key]
                    if isinstance(spin, QSpinBox):
                        spin.setValue(int(val))
                    else:
                        spin.setValue(float(val))

    # —— 事件处理 ——

    def _on_toggle_key_visible(self, checked: bool) -> None:
        if checked:
            self._key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_key_btn.setText("隐藏")
        else:
            self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_key_btn.setText("显示")

    def _on_add_model(self) -> None:
        text = self._model_input.text().strip()
        if not text:
            return
        existing = [self._models_list.item(i).data(Qt.ItemDataRole.UserRole)["name_label"].text()
                    for i in range(self._models_list.count())]
        if text in existing:
            self._model_input.clear()
            return
        item, widget = self._build_model_item(text, ["llm"])
        self._models_list.addItem(item)
        self._models_list.setItemWidget(item, widget)
        self._model_input.clear()
        self._model_input.setFocus()

    def _on_del_model(self) -> None:
        row = self._models_list.currentRow()
        if row >= 0:
            self._models_list.takeItem(row)

    def _on_clear_models(self) -> None:
        self._models_list.clear()

    def _set_all_uses_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._uses_list.count()):
            item = self._uses_list.item(i)
            item.setCheckState(state)

    def _on_accept(self) -> None:
        if not self._validate():
            return
        result: dict = {
            "label": self._label_edit.text().strip(),
            "key": self._key_edit.text().strip(),
            "base_url": self._base_url_edit.text().strip(),
            "max_concurrency": self._concurrency_spin.value(),
            "privacy_warning": self._privacy_edit.toPlainText().strip(),
            "enabled": self._enabled_cb.isChecked(),
            "allowed_uses": [],
            "protocol": self._protocol_combo.currentText(),  # v11
        }

        # models（仅 full 模式收集）
        if self._edit_mode == "full":
            models = []
            for i in range(self._models_list.count()):
                item = self._models_list.item(i)
                data = item.data(Qt.ItemDataRole.UserRole)
                name = data["name_label"].text().strip()  # v11: QLineEdit
                if not name:
                    continue
                scope = [s for s, cb in data["scope_cbs"].items() if cb.isChecked()]
                tier_combo = data.get("tier_combo")
                try:
                    tier = int(tier_combo.currentText()) if tier_combo else 3
                except (TypeError, ValueError):
                    tier = 3
                if not 1 <= tier <= 5:
                    tier = 3
                # v11：收集 display_name（非空才加入）
                dn_edit = data.get("display_name_edit")
                display_name = dn_edit.text().strip() if dn_edit else ""
                m_dict = {"name": name, "scope": scope,
                          "tier": tier,
                          "enabled": data.get("enabled_cb").isChecked()}
                if display_name and display_name != name:
                    m_dict["display_name"] = display_name
                models.append(m_dict)
            result["models"] = models

        # 允许用途
        allowed_uses = []
        for i in range(self._uses_list.count()):
            item = self._uses_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                uc_id = item.data(Qt.ItemDataRole.UserRole)
                if uc_id:
                    allowed_uses.append(uc_id)
        result["allowed_uses"] = allowed_uses

        # v14: pool_key
        result["pool_key"] = self._pool_key_edit.text().strip()

        # v14: pool 段（勾选时收集各参数，未勾选传空字典 = 用全局默认）
        if self._pool_group.isChecked():
            pool_dict = {}
            for key, spin in self._pool_spins.items():
                if isinstance(spin, QSpinBox):
                    pool_dict[key] = spin.value()
                else:
                    pool_dict[key] = round(spin.value(), 1)
            result["pool"] = pool_dict
        else:
            result["pool"] = {}

        # v9: vision 段（勾选时收集各参数，未勾选传空字典 = 用默认值）
        if self._vision_group.isChecked():
            vision_dict = {}
            for key, spin in self._vision_spins.items():
                if isinstance(spin, QSpinBox):
                    vision_dict[key] = spin.value()
                else:
                    vision_dict[key] = round(spin.value(), 1)
            result["vision"] = vision_dict
        else:
            result["vision"] = {}

        # 编辑模式下，若 key 为空（用户没改脱敏值），不传 key 字段（保留原值）
        if self._edit_mode == "key_only" and not result["key"]:
            result.pop("key")

        self._result = result
        self.accept()

    def _validate(self) -> bool:
        if not self._label_edit.text().strip():
            QMessageBox.warning(self, "验证失败", "标签不能为空")
            self._label_edit.setFocus()
            return False
        # key_only 模式下允许 key 为空（保留原脱敏值）
        if self._edit_mode == "full" and not self._key_edit.text().strip():
            QMessageBox.warning(self, "验证失败", "API Key 不能为空")
            self._key_edit.setFocus()
            return False
        if self._edit_mode == "full":
            if self._models_list.count() == 0:
                QMessageBox.warning(self, "验证失败", "模型列表不能为空（至少添加一个 model）")
                self._model_input.setFocus()
                return False
            for i in range(self._models_list.count()):
                item = self._models_list.item(i)
                data = item.data(Qt.ItemDataRole.UserRole)
                if not any(cb.isChecked() for cb in data["scope_cbs"].values()):
                    name = data["name_label"].text()
                    QMessageBox.warning(self, "验证失败",
                                        f"模型 {name} 至少选择一个用途范围（LLM/VL/AIGC图/AIGC视频）")
                    return False
        return True

    # —— 公共 API ——

    def get_result(self) -> dict | None:
        return self._result

    @staticmethod
    def open_dialog(parent=None,
                    existing: dict | None = None,
                    edit_mode: str = "full") -> dict | None:
        """弹出 KeyForm 对话框，返回 result dict 或 None（取消）"""
        dlg = KeyForm(parent, existing=existing, edit_mode=edit_mode)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.get_result()
        return None


class _BatchEditModelsDialog(QDialog):
    """批量编辑一个 key 下所有 model 的启用状态 / Tier / scope

    用表格列出所有 model，每行：
    - model name（只读）
    - Tier 下拉（1-5）
    - 4 个 scope 复选框（LLM/VL/AIGC图/AIGC视频）

    顶部提供"统一应用到所有"功能：
    - 统一 scope 模板（4 个复选框 + 应用按钮）
    - 统一 Tier（下拉 + 应用按钮）
    """

    def __init__(self, parent=None, existing_models: list | None = None):
        super().__init__(parent)
        self.setWindowTitle("批量编辑模型")
        self.setMinimumSize(1080, 600)
        self.resize(1180, 780)
        self._models = [dict(m) for m in (existing_models or [])]
        self._result: list | None = None

        self._build_ui()
        self._fill_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 顶部说明
        hint = QLabel("批量编辑此 key 下所有 model 的 Tier / scope。修改后点击「确定」一次性保存。")
        set_text_role(hint, "secondary")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 统一应用区
        apply_group = QWidget()
        apply_layout = QGridLayout(apply_group)
        apply_layout.setContentsMargins(0, 0, 0, 0)
        apply_layout.setSpacing(8)

        apply_layout.addWidget(QLabel("统一 scope:"), 0, 0)
        self._tpl_scope_cbs: dict[str, QCheckBox] = {}
        for scope_name, label in [("llm", "LLM"), ("vl", "VL"),
                                  ("aigc_image", "AIGC图"), ("aigc_video", "AIGC视频")]:
            cb = QCheckBox(label)
            self._tpl_scope_cbs[scope_name] = cb
            apply_layout.addWidget(cb, 0, len(self._tpl_scope_cbs))
        apply_scope_btn = QPushButton("应用到所有")
        apply_scope_btn.setMinimumHeight(32)
        apply_scope_btn.clicked.connect(self._on_apply_scope_all)
        apply_layout.addWidget(apply_scope_btn, 0, 5)

        apply_layout.addWidget(QLabel("统一 Tier:"), 1, 0)
        self._tpl_tier_combo = QComboBox()
        self._tpl_tier_combo.setMinimumHeight(32)
        self._tpl_tier_combo.addItems(["1", "2", "3", "4", "5"])
        self._tpl_tier_combo.setCurrentIndex(2)  # 默认 3
        self._tpl_tier_combo.setToolTip(
            "Tier 1=最轻量  2=轻量  3=默认  4=较强  5=最强大"
        )
        apply_layout.addWidget(self._tpl_tier_combo, 1, 1, 1, 2)
        apply_tier_btn = QPushButton("应用到所有")
        apply_tier_btn.setMinimumHeight(32)
        apply_tier_btn.clicked.connect(self._on_apply_tier_all)
        apply_layout.addWidget(apply_tier_btn, 1, 3)

        apply_layout.setColumnStretch(6, 1)
        layout.addWidget(apply_group)

        # 表格（v11：含"展示名"列，共 8 列）
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["连接名", "展示名", "启用", "Tier", "LLM", "VL", "AIGC图", "AIGC视频"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setDefaultSectionSize(46)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 240)
        header.resizeSection(1, 240)
        header.resizeSection(2, 78)
        header.resizeSection(3, 92)
        header.resizeSection(4, 82)
        header.resizeSection(5, 82)
        header.resizeSection(6, 112)
        header.resizeSection(7, 128)
        header.setStretchLastSection(False)
        layout.addWidget(self._table, 1)

        # 模型增删按钮行
        model_ops_row = QHBoxLayout()
        model_ops_row.setSpacing(6)
        add_model_btn = QPushButton("添加模型")
        add_model_btn.setMinimumHeight(34)
        add_model_btn.clicked.connect(self._on_add_model_row)
        model_ops_row.addWidget(add_model_btn)
        del_model_btn = QPushButton("删除选中模型")
        del_model_btn.setMinimumHeight(34)
        del_model_btn.clicked.connect(self._on_del_model_row)
        set_kind(del_model_btn, "danger")
        model_ops_row.addWidget(del_model_btn)
        model_ops_row.addStretch()
        self._model_count_label = QLabel("")
        set_text_role(self._model_count_label, "secondary")
        model_ops_row.addWidget(self._model_count_label)
        layout.addLayout(model_ops_row)

        # 按钮栏
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        ok_btn = button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("确定")
        cancel_btn = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText("取消")
        layout.addWidget(button_box)

    def _fill_table(self) -> None:
        self._table.setRowCount(len(self._models))
        self._scope_cbs: list[dict[str, QCheckBox]] = []
        self._enabled_cbs: list[QCheckBox] = []
        self._tier_combos: list[QComboBox] = []
        self._display_name_edits: list[QLineEdit] = []  # v11

        for row, m in enumerate(self._models):
            name = m.get("name", "")
            scope = m.get("scope", ["llm"])
            tier = m.get("tier", 3)
            try:
                tier = int(tier)
                if not 1 <= tier <= 5:
                    tier = 3
            except (TypeError, ValueError):
                tier = 3
            display_name = (m.get("display_name") or "").strip()  # v11

            # 列 0: 连接名（只读）
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(name)
            self._table.setItem(row, 0, name_item)

            # 列 1: 展示名（v11，可编辑）
            dn_edit = QLineEdit(display_name)
            dn_edit.setPlaceholderText("留空=用连接名")
            dn_edit.setMinimumHeight(30)
            dn_edit.setToolTip(
                "展示名用于 UI 显示；调用时仍用「连接名」。\n"
                "留空则展示连接名本身。"
            )
            self._table.setCellWidget(row, 1, dn_edit)
            self._display_name_edits.append(dn_edit)

            # 列 2: 启用开关
            enabled_cb = QCheckBox()
            enabled_cb.setMinimumSize(24, 26)
            enabled_cb.setChecked(m.get("enabled", True))
            enabled_cb.setToolTip("仅控制此模型，不影响同一 Key 下的其他模型")
            enabled_wrapper = QWidget()
            enabled_layout = QHBoxLayout(enabled_wrapper)
            enabled_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            enabled_layout.setContentsMargins(0, 0, 0, 0)
            enabled_layout.addWidget(enabled_cb)
            self._table.setCellWidget(row, 2, enabled_wrapper)
            self._enabled_cbs.append(enabled_cb)

            # 列 3: Tier 下拉
            tier_combo = QComboBox()
            tier_combo.setMinimumHeight(30)
            tier_combo.addItems(["1", "2", "3", "4", "5"])
            tier_combo.setCurrentIndex(tier - 1)
            tier_combo.setToolTip(
                "Tier 1=最轻量(免费/简单任务)  2=轻量  3=默认  4=较强  5=最强大(复杂推理/VL/AIGC)"
            )
            self._table.setCellWidget(row, 3, tier_combo)
            self._tier_combos.append(tier_combo)

            # 列 4-7: scope 复选框（LLM/VL/AIGC图/AIGC视频）
            cbs: dict[str, QCheckBox] = {}
            for i, (scope_name, _) in enumerate([("llm", ""), ("vl", ""),
                                                 ("aigc_image", ""), ("aigc_video", "")]):
                cb = QCheckBox()
                cb.setMinimumSize(24, 26)
                cb.setChecked(scope_name in scope)
                wrapper = QWidget()
                wl = QHBoxLayout(wrapper)
                wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                wl.setContentsMargins(0, 0, 0, 0)
                wl.addWidget(cb)
                self._table.setCellWidget(row, i + 4, wrapper)  # v11: 索引偏移 1
                cbs[scope_name] = cb
            self._scope_cbs.append(cbs)

        self._update_model_count()

    def _update_model_count(self) -> None:
        """更新模型计数标签"""
        if hasattr(self, "_model_count_label"):
            self._model_count_label.setText(f"共 {len(self._models)} 个模型")

    def _on_add_model_row(self) -> None:
        """添加新模型：弹出输入框，输入模型名后添加到表格末尾"""
        name, ok = QInputDialog.getText(
            self, "添加模型", "模型连接名:", QLineEdit.EchoMode.Normal, ""
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        # 查重
        for m in self._models:
            if m.get("name") == name:
                QMessageBox.warning(self, "重复", f"模型「{name}」已存在")
                return
        # 默认值：scope=llm, tier=3, enabled=True, display_name=""
        self._models.append({
            "name": name,
            "scope": ["llm"],
            "tier": 3,
            "enabled": True,
            "display_name": "",  # v11：默认空（用连接名）
        })
        self._fill_table()
        # 选中新增的行
        self._table.selectRow(len(self._models) - 1)

    def _on_del_model_row(self) -> None:
        """删除选中行的模型"""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在表格中选择要删除的模型行")
            return
        row = rows[0].row()
        if row >= len(self._models):
            return
        name = self._models[row].get("name", "")
        if len(self._models) == 1:
            QMessageBox.warning(self, "无法删除", "至少保留一个 model")
            return
        btn = QMessageBox.question(
            self, "确认删除",
            f"确定删除模型「{name}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._models.pop(row)
        self._fill_table()

    def _on_apply_scope_all(self) -> None:
        """将统一 scope 模板应用到所有 model 行"""
        tpl_scopes = [s for s, cb in self._tpl_scope_cbs.items() if cb.isChecked()]
        if not tpl_scopes:
            QMessageBox.information(self, "提示", "请先在统一 scope 模板中勾选至少一项")
            return
        for cbs in self._scope_cbs:
            for scope_name, cb in cbs.items():
                cb.setChecked(scope_name in tpl_scopes)

    def _on_apply_tier_all(self) -> None:
        """将统一 Tier 应用到所有 model 行"""
        tier = int(self._tpl_tier_combo.currentText())
        for combo in self._tier_combos:
            combo.setCurrentIndex(tier - 1)

    def _on_accept(self) -> None:
        result = []
        for row, m in enumerate(self._models):
            name = m.get("name", "")
            if not name:
                continue
            scope = [s for s, cb in self._scope_cbs[row].items() if cb.isChecked()]
            try:
                tier = int(self._tier_combos[row].currentText())
            except (TypeError, ValueError, IndexError):
                tier = 3
            if not 1 <= tier <= 5:
                tier = 3
            # v11：收集 display_name（非空且与 name 不同才输出）
            display_name = self._display_name_edits[row].text().strip()
            new_m = {"name": name, "scope": scope,
                     "tier": tier,
                     "enabled": self._enabled_cbs[row].isChecked()}
            if display_name and display_name != name:
                new_m["display_name"] = display_name
            result.append(new_m)
        if not result:
            QMessageBox.warning(self, "验证失败", "至少保留一个 model")
            return
        # 校验每个 model 至少一个 scope
        for m in result:
            if not m["scope"]:
                QMessageBox.warning(self, "验证失败",
                                    f"模型 {m['name']} 至少选择一个 scope")
                return
        self._result = result
        self.accept()

    def get_result(self) -> list | None:
        return self._result

    @staticmethod
    def open_dialog(parent=None, existing_models: list | None = None) -> list | None:
        dlg = _BatchEditModelsDialog(parent, existing_models=existing_models)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.get_result()
        return None
