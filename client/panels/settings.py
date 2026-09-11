"""Settings 面板 — config.toml 树形浏览 + 字段编辑

数据源：GET /config（脱敏 dict）+ POST /config（点分隔路径 + 值）
危险字段（api_key/password/secret/token/host/port）编辑时弹 ConfirmDialog 二次确认。
编辑后立即 GET /health 验证后端可用，失败提示回滚；脱敏字段无法精确回滚时明确告知。
"""

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.panel_base import PanelBase, PanelMeta
from client.widgets.confirm_dialog import ConfirmDialog
from lib.ui import tokens
from lib.ui.theme import set_text_role

_DESC_CACHE: dict | None = None


def _load_descriptions() -> dict:
    global _DESC_CACHE
    if _DESC_CACHE is not None:
        return _DESC_CACHE
    path = Path(__file__).resolve().parent.parent.parent / "data" / "config_descriptions.json"
    try:
        _DESC_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _DESC_CACHE = {}
    # _DESC_CACHE 此时必为 dict（上面两条赋值路径都给了 dict）
    assert _DESC_CACHE is not None
    return _DESC_CACHE


def _lookup_desc(path: str) -> tuple[str, str]:
    """查找配置字段的说明和示例，支持 * 通配符。"""
    descs = _load_descriptions()
    if path in descs:
        d = descs[path]
        return d.get("desc", ""), d.get("example", "")
    parts = path.split(".")
    for i in range(1, len(parts)):
        wildcard_path = ".".join(parts[:i] + ["*"] + parts[i+1:])
        if wildcard_path in descs:
            d = descs[wildcard_path]
            return d.get("desc", ""), d.get("example", "")
    return "", ""


def _format_desc(path: str) -> str:
    desc, example = _lookup_desc(path)
    if desc and example:
        return f"{desc}  [示例: {example}]"
    if desc:
        return desc
    if example:
        return f"[示例: {example}]"
    return ""


# 危险字段关键词（编辑时弹二次确认）
_DANGEROUS_KEYS = {"api_key", "secret", "password", "token", "host", "port"}


# ===== 审批严格性四级端点清单（与 server/route_tags.py 保持同步）=====
# 每个级别列出该级别下会触发 approval_required 拦截的端点（agent 调用时弹审批窗）
_APPROVAL_LEVEL_ENDPOINTS: dict[str, list[str]] = {
    "strict": [
        "系统: /shutdown, /config, /system/keep-awake, /mcp/stats/reset",
        "API Key 写: /apikey/keys (POST/PUT/DELETE)",
        "Loop 控制: /loop/tasks/* (run/pause/resume/create)",
        "活动追踪: /activity/daily/* (update/review/delete)",
        "浏览器: /browser/close",
        "代码执行: /exec/python, /exec/apply-patch, /exec/cmd",
        "终端: /terminals/spawn",
        "网关运行: /advanced/run, /templates/run",
        "MindForge 运维: /mindforge/{build-index,convert,pipeline,unload,daemon/stop}",
        "用户消息: /user/message (POST/DELETE)",
        "模型卸载: /ocr/models/{unload,keep}, /vision/models/unload",
    ],
    "moderate": [
        "系统: /shutdown, /config, /system/keep-awake, /mcp/stats/reset",
        "API Key 写: /apikey/keys (POST/PUT/DELETE)",
        "代码执行: /exec/python, /exec/apply-patch, /exec/cmd",
        "终端: /terminals/spawn",
        "网关运行: /advanced/run, /templates/run",
        "MindForge 运维: /mindforge/{build-index,convert,pipeline,unload,daemon/stop}",
        "用户消息: /user/message (POST/DELETE)",
        "（已放行：Loop 任务 / 活动日报 / 浏览器关闭 / OCR 与 Vision 模型卸载与 keep）",
    ],
    "loose": [
        "代码执行: /exec/python, /exec/apply-patch, /exec/cmd",
        "（其他端点全部放行，包括 PUT/DELETE 默认审批）",
    ],
    "none": [
        "（不拦截任何端点；仅 dcg 二进制预检查仍独立生效，受 enabled 控制）",
    ],
}

_APPROVAL_LEVEL_LABELS = {
    "strict": "严格",
    "moderate": "适中",
    "loose": "宽松",
    "none": "无",
}


def _is_dangerous(path: str) -> bool:
    """路径任意段落在 _DANGEROUS_KEYS 中即为危险字段"""
    parts = path.lower().split(".")
    return any(p in _DANGEROUS_KEYS for p in parts)


def _is_masked(value: Any) -> bool:
    """判断值是否为脱敏字符串（含 ****）"""
    return isinstance(value, str) and "****" in value


def _infer_type(value: Any) -> str:
    """推断值类型：bool/int/float/str"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _coerce_value(raw: str, target_type: str) -> Any:
    """字符串 → 目标类型。失败抛 ValueError。"""
    raw = raw.strip()
    if target_type == "bool":
        low = raw.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"无法解析为 bool: {raw!r}")
    if target_type == "int":
        return int(raw)
    if target_type == "float":
        return float(raw)
    return raw


def _format_value(value: Any) -> str:
    """值格式化为显示文本"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "(empty)"
    return str(value)


class _EditDialog(QDialog):
    """字段编辑对话框

    根据原值类型自动选择控件：
    - bool: QComboBox(true/false)
    - int: QSpinBox
    - float: QDoubleSpinBox
    - str: QLineEdit
    - 脱敏字段: QLineEdit + 提示"输入完整新值"
    """

    def __init__(self, path: str, old_value: Any, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑配置字段")
        self.setMinimumWidth(480)
        self._path = path
        self._old_value = old_value
        self._masked = _is_masked(old_value)
        # 脱敏字段无法推断类型，按 str 处理（用户必须输入完整新值）
        self._target_type = "str" if self._masked else _infer_type(old_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        path_label = QLabel(f"<b>路径</b>: <code>{path}</code>")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)

        type_label = QLabel(f"<b>类型</b>: {self._target_type}")
        layout.addWidget(type_label)

        if self._masked:
            cur_text = f"{old_value} <span style='color:{tokens.TEXT_SECONDARY}'>(脱敏)</span>"
        else:
            cur_text = f"<code>{_format_value(old_value)}</code>"
        cur_label = QLabel(f"<b>当前值</b>: {cur_text}")
        cur_label.setWordWrap(True)
        cur_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(cur_label)

        layout.addWidget(QLabel("<b>新值</b>:"))
        self._input_widget = self._build_input()
        layout.addWidget(self._input_widget)

        if self._masked:
            hint = QLabel(
                "敏感字段：当前显示为脱敏值。请输入完整新值，提交后将原样存储。"
            )
            set_text_role(hint, "warning")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        if _is_dangerous(self._path):
            warn = QLabel("危险字段：修改可能影响后端可用性。提交前会再次确认。")
            set_text_role(warn, "danger")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _build_input(self):
        t = self._target_type
        if t == "bool":
            w = QComboBox()
            w.addItem("true", True)
            w.addItem("false", False)
            cur = self._old_value if isinstance(self._old_value, bool) else True
            w.setCurrentIndex(0 if cur else 1)
            return w
        if t == "int":
            w = QSpinBox()
            w.setRange(-2**31, 2**31 - 1)
            try:
                w.setValue(int(self._old_value))
            except Exception:
                pass
            return w
        if t == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e12, 1e12)
            w.setDecimals(6)
            try:
                w.setValue(float(self._old_value))
            except Exception:
                pass
            return w
        # str
        w = QLineEdit()
        if not self._masked and self._old_value is not None:
            w.setText(str(self._old_value))
        w.setPlaceholderText(
            "输入完整新值（覆盖脱敏值）" if self._masked else "输入新值..."
        )
        return w

    def get_value(self) -> Any:
        """从控件取值并转换。失败抛 ValueError。"""
        w = self._input_widget
        if isinstance(w, QComboBox):
            return w.currentData()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return w.value()
        # QLineEdit
        return _coerce_value(w.text(), self._target_type)


class _LoadConfigThread(QThread):
    """异步加载 config"""

    config_loaded = Signal(object)  # dict or None

    def __init__(self, http: HttpClient):
        super().__init__()
        self._http = http

    def run(self):
        resp = self._http.get("/config")
        if resp is None or not isinstance(resp, dict):
            self.config_loaded.emit(None)
        else:
            self.config_loaded.emit(resp.get("config"))


class SettingsPanel(PanelBase):
    """设置面板：config.toml 树形浏览 + 字段编辑"""

    PANEL_META = PanelMeta(
        id="settings",
        title="设置",
        icon="settings",
        order=50,
        category="advanced",
        requires_backend=True,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._config: dict | None = None
        self._refresh_btn: QPushButton | None = None
        self._search_edit: QLineEdit | None = None
        self._tree: QTreeWidget | None = None
        self._edit_btn: QPushButton | None = None
        self._status_label: QLabel | None = None
        self._load_thread: _LoadConfigThread | None = None
        self._submit_thread = None
        self._rollback_thread = None
        # 审批严格性
        self._approval_combo: QComboBox | None = None
        self._approval_detail_label: QLabel | None = None
        self._approval_submit_thread = None
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
        label = QLabel("后端离线\n\nSettings 面板需要后端在线才能加载 config")
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
        title = QLabel("设置")
        set_text_role(title, "heading")
        header_row.addWidget(title)
        header_row.addStretch()

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索字段名 / 值 / 说明...")
        self._search_edit.setMinimumWidth(240)
        self._search_edit.textChanged.connect(self._apply_filter)
        header_row.addWidget(self._search_edit)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        header_row.addWidget(self._refresh_btn)

        layout.addLayout(header_row)

        # 审批严格性区域
        self._build_approval_level_row(layout)

        # 树
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["字段", "值", "说明"])
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background: {tokens.BG_PANEL}; color: {tokens.TEXT_PRIMARY};"
            f" border: 1px solid {tokens.BORDER};"
            f" font-family: {tokens.FONT_MONO}; font-size: 12px; }}"
            f"QHeaderView::section {{ background: {tokens.BG_CARD}; color: {tokens.TEXT_PRIMARY};"
            f" padding: 6px; border: none; border-bottom: 1px solid {tokens.ACCENT}; }}"
            "QTreeWidget::item { padding: 4px 2px; }"
            f"QTreeWidget::item:selected {{ background: {tokens.ACCENT_WASH}; color: {tokens.TEXT_PRIMARY}; }}"
        )
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 250)
        header.resizeSection(1, 200)
        header.resizeSection(2, 400)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        layout.addWidget(self._tree, 1)

        # 底栏
        bottom_row = QHBoxLayout()
        self._edit_btn = QPushButton("编辑")
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        bottom_row.addWidget(self._edit_btn)

        self._status_label = QLabel("")
        set_text_role(self._status_label, "secondary")
        bottom_row.addWidget(self._status_label, 1)
        layout.addLayout(bottom_row)

    # —— 审批严格性区域 ——

    def _build_approval_level_row(self, parent_layout: QVBoxLayout) -> None:
        """构建审批严格性下拉框 + 端点清单小字"""
        container = QWidget()
        container.setStyleSheet(f"""
            QWidget#approvalBox {{
                background: {tokens.BG_OVERLAY};
                border: 1px solid {tokens.ACCENT};
                border-radius: 6px;
            }}
        """)
        container.setObjectName("approvalBox")
        cl = QVBoxLayout(container)
        cl.setContentsMargins(12, 8, 12, 10)
        cl.setSpacing(6)

        # 第一行：标签 + 下拉框
        row = QHBoxLayout()
        row.setSpacing(10)
        title = QLabel("审批严格性")
        set_text_role(title, "accent")
        row.addWidget(title)

        self._approval_combo = QComboBox()
        for level in ("strict", "moderate", "loose", "none"):
            self._approval_combo.addItem(_APPROVAL_LEVEL_LABELS[level], level)
        self._approval_combo.setMinimumWidth(120)
        self._approval_combo.currentIndexChanged.connect(self._on_approval_level_changed)
        row.addWidget(self._approval_combo)
        row.addStretch()
        cl.addLayout(row)

        # 第二行：端点清单小字
        self._approval_detail_label = QLabel("")
        self._approval_detail_label.setWordWrap(True)
        self._approval_detail_label.setStyleSheet(
            f"color: {tokens.TEXT_TERTIARY}; font-size: 11px; font-family: {tokens.FONT_MONO};"
        )
        self._approval_detail_label.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self._approval_detail_label)

        parent_layout.addWidget(container)

        # 初始化小字内容
        self._update_approval_detail("strict")

    def _update_approval_detail(self, level: str) -> None:
        """根据级别更新端点清单小字"""
        label = self._approval_detail_label
        if label is None:
            return
        lines = _APPROVAL_LEVEL_ENDPOINTS.get(level, [])
        if not lines:
            label.setText("")
            return
        html_lines = []
        for line in lines:
            # 转义 HTML 特殊字符
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            # 已放行/说明行用不同颜色突出
            if safe.startswith("（"):
                html_lines.append(f"<span style='color:{tokens.TEXT_DISABLED};'>{safe}</span>")
            else:
                html_lines.append(f"<span style='color:{tokens.TEXT_TERTIARY};'>• {safe}</span>")
        label.setText("<br>".join(html_lines))

    def _on_approval_level_changed(self, _idx: int) -> None:
        """下拉框选择变化：更新小字 + 提交到后端"""
        if self._approval_combo is None:
            return
        level = self._approval_combo.currentData()
        self._update_approval_detail(level)
        self._set_status(f"提交审批严格性: {_APPROVAL_LEVEL_LABELS.get(level, level)}...")

        # 在后台线程提交，避免阻塞 UI
        class _LevelSubmitThread(QThread):
            done = Signal(object, str, object)  # (new_config, level, error)

            def __init__(self, http, lvl):
                super().__init__()
                self._http = http
                self._lvl = lvl

            def run(self):
                resp = self._http.post(
                    "/config",
                    json={"path": "command_guard.approval_level", "value": self._lvl},
                )
                if resp is None:
                    self.done.emit(None, self._lvl, "后端返回错误")
                else:
                    self.done.emit(resp.get("config"), self._lvl, None)

        # 防止用户快速切换时多个线程竞争
        if self._approval_submit_thread is not None and self._approval_submit_thread.isRunning():
            self._approval_submit_thread.quit()
            self._approval_submit_thread.wait(1000)
        self._approval_submit_thread = _LevelSubmitThread(self._http, level)
        self._approval_submit_thread.done.connect(self._on_approval_level_submitted)
        self._approval_submit_thread.start()

    @Slot(object, str, object)
    def _on_approval_level_submitted(self, new_config, level: str, error) -> None:
        if error is not None:
            self._set_status(f"审批严格性提交失败: {error}")
            QMessageBox.warning(self, "提交失败", f"修改审批严格性失败：\n{error}")
            return
        self._config = new_config
        self._populate_tree(new_config)
        self._set_status(f"审批严格性已更新: {_APPROVAL_LEVEL_LABELS.get(level, level)}（立即生效，无需重启）")

    def _sync_approval_combo_from_config(self) -> None:
        """加载 config 后同步下拉框选中项（不触发提交）"""
        if self._approval_combo is None or self._config is None:
            return
        guard = self._config.get("command_guard", {}) or {}
        level = guard.get("approval_level", "strict")
        if level not in _APPROVAL_LEVEL_LABELS:
            level = "strict"
        # 找到对应 index
        for i in range(self._approval_combo.count()):
            if self._approval_combo.itemData(i) == level:
                self._approval_combo.blockSignals(True)
                self._approval_combo.setCurrentIndex(i)
                self._approval_combo.blockSignals(False)
                self._update_approval_detail(level)
                break

    # —— PanelBase 钩子 ——

    def on_show(self) -> None:
        if self._config is None:
            self._load_config_async()
        else:
            self._stack.setCurrentWidget(self._normal_widget)

    def on_hide(self) -> None:
        pass

    def on_refresh(self) -> None:
        self._load_config_async()

    def on_loading_changed(self, loading: bool) -> None:
        if self._refresh_btn:
            self._refresh_btn.setText("刷新中..." if loading else "刷新")
            self._refresh_btn.setEnabled(not loading)

    def on_backend_status_change(self, online: bool) -> None:
        if online:
            self._stack.setCurrentWidget(self._normal_widget)
            if self._config is None:
                self._load_config_async()
        else:
            self._stack.setCurrentWidget(self._offline_widget)

    # —— 数据加载 ——

    def _on_refresh_clicked(self) -> None:
        self._load_config_async()

    def _load_config_async(self) -> None:
        if self._load_thread is not None and self._load_thread.isRunning():
            return
        self._set_loading(True)
        self._set_status("加载配置中...")
        self._load_thread = _LoadConfigThread(self._http)
        self._load_thread.config_loaded.connect(self._on_config_loaded)
        self._load_thread.start()

    @Slot(object)
    def _on_config_loaded(self, config) -> None:
        self._set_loading(False)
        if config is None:
            self._set_status("加载失败（后端返回错误）")
            self._stack.setCurrentWidget(self._offline_widget)
            return
        self._config = config
        self._stack.setCurrentWidget(self._normal_widget)
        self._populate_tree(config)
        self._sync_approval_combo_from_config()
        self._set_status(f"已加载 {len(config)} 个顶层 section")

    def _set_status(self, text: str) -> None:
        if self._status_label:
            self._status_label.setText(text)

    # —— 树填充 ——

    def _save_expansion_state(self) -> set[str]:
        """保存当前树展开状态，返回展开容器节点的路径集合"""
        expanded: set[str] = set()
        if self._tree is None:
            return expanded
        it = QTreeWidgetItemIterator(self._tree)
        while it.value() is not None:
            item = it.value()
            if item.childCount() > 0 and item.isExpanded():
                path = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(path, str):
                    expanded.add(path)
            it += 1
        return expanded

    def _restore_expansion_state(self, expanded_paths: set[str]) -> None:
        """恢复树展开状态"""
        if self._tree is None or not expanded_paths:
            return
        it = QTreeWidgetItemIterator(self._tree)
        while it.value() is not None:
            item = it.value()
            if item.childCount() > 0:
                path = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(path, str) and path in expanded_paths:
                    item.setExpanded(True)
            it += 1

    def _populate_tree(self, config: dict) -> None:
        global _DESC_CACHE
        _DESC_CACHE = None  # 强制重新加载说明映射
        tree = self._tree
        if tree is None:
            return
        # 保存展开状态，重建后恢复（避免编辑后全部折叠）
        expanded_paths = self._save_expansion_state()
        tree.clear()
        for section_name in sorted(config.keys()):
            value = config[section_name]
            is_container = isinstance(value, dict)
            label = section_name
            top_item = QTreeWidgetItem([label, "", ""])
            top_item.setData(0, Qt.ItemDataRole.UserRole, section_name)
            top_item.setData(1, Qt.ItemDataRole.UserRole, value)
            font = top_item.font(0)
            font.setBold(True)
            top_item.setFont(0, font)
            if is_container:
                self._style_container(top_item, len(value), section_name)
            elif value is None or (isinstance(value, str) and value == ""):
                self._style_empty(top_item, section_name)
            else:
                self._style_leaf_value(top_item, value, section_name)
            tree.addTopLevelItem(top_item)
            if is_container:
                self._fill_section(top_item, section_name, value)
        # 恢复展开状态
        self._restore_expansion_state(expanded_paths)

    def _fill_section(self, parent_item: QTreeWidgetItem, prefix: str, data: dict) -> None:
        for key in sorted(data.keys()):
            value = data[key]
            path = f"{prefix}.{key}"
            is_container = isinstance(value, dict)
            label = key
            child = QTreeWidgetItem([label, "", ""])
            child.setData(0, Qt.ItemDataRole.UserRole, path)
            child.setData(1, Qt.ItemDataRole.UserRole, value)
            parent_item.addChild(child)
            if is_container:
                self._style_container(child, len(value), path)
                self._fill_section(child, path, value)
            elif value is None or (isinstance(value, str) and value == ""):
                self._style_empty(child, path)
            elif _is_masked(value):
                child.setText(1, _format_value(value))
                child.setForeground(1, QColor(tokens.WARNING_TEXT))
                child.setText(2, _format_desc(path))
                child.setForeground(2, QColor(tokens.TEXT_SECONDARY))
                child.setToolTip(0, "敏感字段（已脱敏）— 双击输入完整新值")
            elif _is_dangerous(path):
                child.setText(1, _format_value(value))
                child.setForeground(1, QColor(tokens.DANGER_TEXT))
                child.setText(2, _format_desc(path))
                child.setForeground(2, QColor(tokens.TEXT_SECONDARY))
                child.setToolTip(0, "危险字段 — 双击编辑（需二次确认）")
            else:
                self._style_leaf_value(child, value, path)

    def _style_container(self, item: QTreeWidgetItem, child_count: int, path: str) -> None:
        """容器节点：Cyan 粗体 key + 子项数 + 行背景微亮"""
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(0, QColor(tokens.ACCENT))
        item.setText(1, f"({child_count} 项)")
        item.setForeground(1, QColor(tokens.TEXT_SECONDARY))
        item.setText(2, _format_desc(path))
        item.setForeground(2, QColor(tokens.TEXT_SECONDARY))
        bg = QColor(tokens.BG_BASE)
        item.setBackground(0, bg)
        item.setBackground(1, bg)
        item.setBackground(2, bg)
        item.setToolTip(0, f"容器节点 — 双击展开/收回 {child_count} 个子字段（不可直接编辑）")

    def _style_empty(self, item: QTreeWidgetItem, path: str) -> None:
        """空缺字段：灰色斜体 (empty) + 行背景微暗"""
        item.setText(1, "(empty)")
        item.setForeground(0, QColor(tokens.TEXT_SECONDARY))
        item.setForeground(1, QColor(tokens.TEXT_SECONDARY))
        item.setText(2, _format_desc(path))
        item.setForeground(2, QColor(tokens.TEXT_SECONDARY))
        font = item.font(1)
        font.setItalic(True)
        item.setFont(1, font)
        bg = QColor(tokens.BG_OVERLAY)
        item.setBackground(0, bg)
        item.setBackground(1, bg)
        item.setBackground(2, bg)
        item.setToolTip(0, "空值 — 双击设置")

    def _style_leaf_value(self, item: QTreeWidgetItem, value: Any, path: str) -> None:
        """普通可编辑叶子：Aurora Green 值色"""
        item.setText(1, _format_value(value))
        item.setForeground(1, QColor(tokens.ACCENT))
        item.setText(2, _format_desc(path))
        item.setForeground(2, QColor(tokens.TEXT_SECONDARY))
        item.setToolTip(0, "双击编辑")

    # —— 搜索过滤 ——

    def _apply_filter(self) -> None:
        if self._tree is None:
            return
        keyword = self._search_edit.text().strip().lower() if self._search_edit else ""
        if not keyword:
            # 恢复全部可见
            it = QTreeWidgetItemIterator(self._tree)
            while it.value() is not None:
                item = it.value()
                item.setHidden(False)
                it += 1
            return
        # 遍历所有节点，叶子匹配则显示父链
        self._filter_recursive(self._tree.invisibleRootItem(), keyword)

    def _filter_recursive(self, parent: QTreeWidgetItem, keyword: str) -> bool:
        """递归过滤。返回 True 表示该 parent 下有匹配节点（应保持可见）。"""
        any_match = False
        for i in range(parent.childCount()):
            child = parent.child(i)
            path = child.data(0, Qt.ItemDataRole.UserRole) or ""
            value_text = child.text(1) or ""
            key_text = child.text(0) or ""
            desc_text = child.text(2) or ""
            # 递归子节点
            child_match = self._filter_recursive(child, keyword) if child.childCount() > 0 else False
            # 自身匹配：路径、字段名、值或说明包含关键词
            self_match = (
                keyword in path.lower()
                or keyword in key_text.lower()
                or keyword in value_text.lower()
                or keyword in desc_text.lower()
            )
            if child_match or self_match:
                child.setHidden(False)
                child.setExpanded(True)
                any_match = True
            else:
                child.setHidden(True)
        return any_match

    # —— 编辑 ——

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._edit_item(item)

    def _on_edit_clicked(self) -> None:
        tree = self._tree
        if tree is None:
            return
        items = tree.selectedItems()
        if not items:
            QMessageBox.information(self, "提示", "请先选择一个字段")
            return
        self._edit_item(items[0])

    def _edit_item(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        value = item.data(1, Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        # dict 容器节点：交给 QTreeWidget 默认的 expandsOnDoubleClick 行为
        # 处理展开/收起（默认 True），不弹警告也不手动 setExpanded——
        # 手动 setExpanded 会与默认行为反转两次导致无效
        if isinstance(value, dict):
            return

        # 危险字段二次确认
        if _is_dangerous(path):
            msg = (
                f"即将修改危险字段：\n\n  {path}\n\n"
                f"当前值：{_format_value(value)}\n\n"
                f"修改 host/port 可能让后端不可达；"
                f"修改 api_key/secret/password/token 会影响对应模块鉴权。\n"
                f"提交后若后端不可用，将提示回滚。"
            )
            if not ConfirmDialog.confirm(
                self, f"修改危险字段 — {path}", msg,
                risk_level="warning",
                detail=f"路径: {path}\n类型: {_infer_type(value) if not _is_masked(value) else 'str (masked)'}",
            ):
                return

        # 编辑对话框
        dlg = _EditDialog(path, value, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            new_value = dlg.get_value()
        except ValueError as e:
            QMessageBox.warning(self, "输入错误", f"值转换失败：\n{e}")
            return

        # 再次确认（非危险字段也提示一下，因为修改 config 影响全局）
        # 注：危险字段已经在前面确认过，这里只对非危险字段轻提示
        if not _is_dangerous(path):
            if not ConfirmDialog.confirm(
                self, "确认修改",
                f"将修改 {path}：\n\n  {_format_value(value)}\n  →\n  {_format_value(new_value)}\n\n提交到后端？",
                risk_level="info",
            ):
                return

        self._submit_edit(path, value, new_value)

    def _submit_edit(self, path: str, old_value: Any, new_value: Any) -> None:
        self._set_status(f"提交中: {path} = {_format_value(new_value)}...")
        self._set_loading(True)

        # 在后台线程提交，避免阻塞 UI
        class _SubmitThread(QThread):
            done = Signal(object, object)  # (new_config, error)

            def __init__(self, http, path, value):
                super().__init__()
                self._http = http
                self._path = path
                self._value = value

            def run(self):
                resp = self._http.post("/config", json={"path": self._path, "value": self._value})
                if resp is None:
                    self.done.emit(None, "后端返回错误（可能是审批被拒）")
                else:
                    self.done.emit(resp.get("config"), None)

        self._submit_thread = _SubmitThread(self._http, path, new_value)
        self._submit_thread.done.connect(lambda cfg, err: self._on_submit_done(path, old_value, new_value, cfg, err))
        self._submit_thread.start()

    @Slot(str, object, object, object, object)
    def _on_submit_done(self, path: str, old_value: Any, new_value: Any,
                        new_config, error) -> None:
        self._set_loading(False)
        if error is not None:
            self._set_status(f"提交失败: {error}")
            QMessageBox.warning(self, "提交失败", f"修改 {path} 失败：\n{error}")
            return

        self._config = new_config
        self._populate_tree(new_config)
        self._set_status(f"已修改: {path}")

        # 编辑后健康检查（延迟 500ms 等 server 应用配置）
        QTimer.singleShot(500, lambda: self._check_health_after_edit(path, old_value))

    def _check_health_after_edit(self, path: str, old_value: Any) -> None:
        """GET /health 验证后端仍可用；不可用则提示回滚"""
        health = self._http.get("/health", timeout=5.0)
        if health is not None and health.get("status") == "ok":
            self._set_status(f"修改成功并已验证: {path}")
            return

        # 健康检查失败
        self._set_status(f"修改后后端不可用: {path}")
        if _is_masked(old_value):
            QMessageBox.critical(
                self, "后端不可用 — 无法自动回滚",
                f"修改 {path} 后后端不可用。\n\n"
                f"原值是敏感字段（已脱敏），无法自动回滚到精确值。\n"
                f"请手动编辑 config.toml 恢复原值，然后重启后端。",
            )
        else:
            rollback = QMessageBox.critical(
                self, "后端不可用 — 是否回滚",
                f"修改 {path} 后后端不可用。\n\n"
                f"原值：{_format_value(old_value)}\n\n"
                f"是否回滚到原值？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if rollback == QMessageBox.StandardButton.Yes:
                self._rollback(path, old_value)

    def _rollback(self, path: str, old_value: Any) -> None:
        self._set_status(f"回滚中: {path}...")
        self._set_loading(True)

        class _RollbackThread(QThread):
            done = Signal(object, object)

            def __init__(self, http, p, v):
                super().__init__()
                self._http = http
                self._p = p
                self._v = v

            def run(self):
                resp = self._http.post("/config", json={"path": self._p, "value": self._v})
                if resp is None:
                    self.done.emit(None, "回滚请求失败")
                else:
                    self.done.emit(resp.get("config"), None)

        self._rollback_thread = _RollbackThread(self._http, path, old_value)
        self._rollback_thread.done.connect(self._on_rollback_done)
        self._rollback_thread.start()

    @Slot(object, object)
    def _on_rollback_done(self, new_config, error) -> None:
        self._set_loading(False)
        if error is not None:
            self._set_status(f"回滚失败: {error}")
            QMessageBox.critical(self, "回滚失败", error)
            return
        self._config = new_config
        self._populate_tree(new_config)
        self._set_status("已回滚。建议重启后端确保配置生效。")
        QMessageBox.information(
            self, "已回滚",
            "配置已回滚到原值。\n\n建议重启后端（监控面板 → 重启后端）确保配置生效。",
        )
