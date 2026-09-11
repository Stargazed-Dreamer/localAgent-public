"""写入记忆页 — 结构化表单 + JSON 编辑器 + 被预填 + 提交回 06

Ticket 10：替换 Ticket 03 占位页，接入真实功能：
- 表单字段：
  - key 输入（QLineEdit，必填，校验 ^[a-zA-Z0-9_-]{1,128}$，编辑模式只读）
  - data JSON 编辑器（QTextEdit 等宽字体，校验合法 JSON dict）
  - fact_type 下拉（QComboBox，5 类 + 「不指定」：user/feedback/project/reference/experience）
  - occurred_at 时间选择器（QDateTimeEdit + QCheckBox，可选）
  - consumption_contexts 输入（QLineEdit 逗号分隔，如 "recurring.*, adhoc.*"）
  - trigger_keywords 输入（QLineEdit 逗号分隔，如 "主题, 深色"）
  - merge 复选（QCheckBox，默认勾选）
- 提交前预览区：MarkdownViewer 显示解析后的完整 JSON
- 校验：key 格式不合法 / data 不是合法 JSON dict → 禁用提交按钮 + 红色提示
- 提交按钮（中等黄色）：点击调 POST /memory/{key} body={data, merge, fact_type, occurred_at, consumption_contexts, trigger_keywords}
- 被预填入口：prefill_from_key(key) → 调 GET /memory/{key} 拉现有数据预填表单（key 只读，其他可改）
- 提交成功后 emit jump_to_memory_list(highlight_key=key) 信号由主 MemoryPanel 转发到记忆库页

业务术语中文化：本页无业务术语（结构化字段名都是技术术语），保留英文无括注
（key / data / fact_type / occurred_at / consumption_contexts / trigger_keywords / merge）
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from PySide6.QtCore import QDate, QDateTime, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.http_worker import HttpWorker
from client.panels.memory._shared import FACT_TYPES
from client.widgets.markdown_viewer import MarkdownViewer
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class WritePage(QWidget):
    """写入记忆页

    信号：
        jump_to_memory_list(str)：提交成功后发，参数为刚写入的 key。
            由主 MemoryPanel 转发到记忆库页（Ticket 06 接收 + 刷新 + 高亮）。
    """

    jump_to_memory_list = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._workers: set = set()
        self._edit_mode: bool = False  # 编辑模式（prefill 后）：key 只读
        self._build_ui()
        self._update_preview_and_validation()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                                   tokens.SPACE_LG, tokens.SPACE_LG)
        layout.setSpacing(tokens.SPACE_MD)

        title = QLabel("写入记忆")
        set_text_role(title, "heading")
        layout.addWidget(title)

        # 表单
        form = QFormLayout()
        form.setSpacing(tokens.SPACE_SM)

        # key
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText(
            "必填，仅字母/数字/下划线/连字符，1-128 字符"
        )
        self._key_edit.textChanged.connect(self._on_form_changed)
        form.addRow("key", self._key_edit)

        # data JSON editor
        self._data_edit = QTextEdit()
        self._data_edit.setFont("Consolas")  # 等宽字体
        self._data_edit.setPlaceholderText(
            '必填，合法 JSON dict，如 {"name": "深色主题", "value": "dark"}'
        )
        self._data_edit.setMinimumHeight(120)
        self._data_edit.textChanged.connect(self._on_form_changed)
        form.addRow("data", self._data_edit)

        # fact_type
        self._fact_type_combo = QComboBox()
        self._fact_type_combo.addItem("（不指定）", None)
        for t in FACT_TYPES:
            self._fact_type_combo.addItem(t, t)
        form.addRow("fact_type", self._fact_type_combo)

        # occurred_at
        occurred_row = QHBoxLayout()
        self._occurred_check = QCheckBox("使用")
        self._occurred_check.toggled.connect(self._on_occurred_toggled)
        occurred_row.addWidget(self._occurred_check)
        self._occurred_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self._occurred_edit.setCalendarPopup(True)
        self._occurred_edit.setDisplayFormat("yyyy-MM-ddTHH:mm:ss")
        self._occurred_edit.setEnabled(False)
        occurred_row.addWidget(self._occurred_edit)
        occurred_row.addStretch()
        occurred_container = QWidget()
        occurred_container.setLayout(occurred_row)
        form.addRow("occurred_at", occurred_container)

        # consumption_contexts
        self._contexts_edit = QLineEdit()
        self._contexts_edit.setPlaceholderText(
            '逗号分隔，如 "recurring.*, adhoc.*"'
        )
        form.addRow("consumption_contexts", self._contexts_edit)

        # trigger_keywords
        self._keywords_edit = QLineEdit()
        self._keywords_edit.setPlaceholderText(
            '逗号分隔，如 "主题, 深色"'
        )
        form.addRow("trigger_keywords", self._keywords_edit)

        # merge
        self._merge_check = QCheckBox("深度合并（已存在时合并旧数据）")
        self._merge_check.setChecked(True)
        form.addRow("merge", self._merge_check)

        layout.addLayout(form)

        # 预览
        preview_label = QLabel("预览（解析后提交 body）")
        set_text_role(preview_label, "secondary")
        layout.addWidget(preview_label)

        self._preview = MarkdownViewer()
        self._preview.set_readonly(True)
        self._preview.setMinimumHeight(120)
        self._preview.setPlaceholderText("填写表单后此处显示提交 body 预览")
        layout.addWidget(self._preview, 1)

        # 校验状态
        self._validation_label = QLabel("")
        self._validation_label.setStyleSheet(
            f"color: {tokens.DANGER_TEXT};"
        )
        layout.addWidget(self._validation_label)

        # 操作按钮
        action_row = QHBoxLayout()
        action_row.addStretch()
        self._submit_btn = QPushButton("提交")
        set_kind(self._submit_btn, "primary")  # 主操作用 primary（spec 说"中等黄色"但无 warning kind）
        self._submit_btn.clicked.connect(self._on_submit_clicked)
        action_row.addWidget(self._submit_btn)

        self._clear_btn = QPushButton("清空")
        set_kind(self._clear_btn, "ghost")
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        action_row.addWidget(self._clear_btn)

        layout.addLayout(action_row)

        self._status_label = QLabel("等待填写")
        set_text_role(self._status_label, "tertiary")
        layout.addWidget(self._status_label)

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        set_text_role(lbl, "secondary")
        return lbl

    def _on_occurred_toggled(self, on: bool) -> None:
        self._occurred_edit.setEnabled(on)
        self._on_form_changed()

    def _on_form_changed(self) -> None:
        """表单变更 → 更新预览 + 校验状态"""
        self._update_preview_and_validation()

    def _on_clear_clicked(self) -> None:
        """清空表单（保留 merge 默认勾选）"""
        self._key_edit.clear()
        self._data_edit.clear()
        self._fact_type_combo.setCurrentIndex(0)
        self._occurred_check.setChecked(False)
        self._occurred_edit.setDateTime(QDateTime.currentDateTime())
        self._contexts_edit.clear()
        self._keywords_edit.clear()
        self._merge_check.setChecked(True)
        self._edit_mode = False
        self._key_edit.setReadOnly(False)
        self._status_label.setText("表单已清空")

    # —— 校验 + 预览 ——

    def _update_preview_and_validation(self) -> None:
        """根据表单当前内容更新预览和校验状态

        校验规则：
        - key 必须匹配 ^[a-zA-Z0-9_-]{1,128}$
        - data 必须是合法 JSON dict（{} 也算）
        """
        errors: list[str] = []

        # key 校验
        key = self._key_edit.text().strip()
        if not key:
            errors.append("key 不能为空")
        elif not _KEY_PATTERN.match(key):
            errors.append("key 仅允许字母/数字/下划线/连字符，长度 1-128")

        # data 校验
        data_text = self._data_edit.toPlainText().strip()
        parsed_data: dict | None = None
        if not data_text:
            errors.append("data 不能为空")
        else:
            try:
                parsed = json.loads(data_text)
                if not isinstance(parsed, dict):
                    errors.append("data 必须是 JSON dict（对象）")
                else:
                    parsed_data = parsed
            except json.JSONDecodeError as e:
                errors.append(f"data JSON 解析失败：{e}")

        # 更新校验状态
        if errors:
            self._validation_label.setText("；".join(errors))
            self._submit_btn.setEnabled(False)
        else:
            self._validation_label.setText("✓ 校验通过")
            self._validation_label.setStyleSheet(
                f"color: {tokens.SUCCESS_TEXT};"
            )
            self._submit_btn.setEnabled(True)

        # 更新预览（即使校验失败也显示，方便用户调试）
        body = self._build_body(parsed_data)
        preview_md = "```json\n" + json.dumps(body, ensure_ascii=False, indent=2) \
                     + "\n```"
        self._preview.set_markdown(preview_md)

    def _build_body(self, parsed_data: dict | None) -> dict:
        """根据表单当前内容构建提交 body（用于预览 + 实际提交）

        Args:
            parsed_data: 解析后的 data dict，校验失败时为 None（用 {} 占位预览）
        """
        body: dict[str, Any] = {
            "data": parsed_data if parsed_data is not None else {},
            "merge": self._merge_check.isChecked(),
        }
        # fact_type
        fact_type = self._fact_type_combo.currentData()
        if fact_type:
            body["fact_type"] = fact_type
        # occurred_at
        if self._occurred_check.isChecked():
            body["occurred_at"] = self._occurred_edit.dateTime() \
                .toString("yyyy-MM-ddTHH:mm:ss")
        # consumption_contexts
        contexts_text = self._contexts_edit.text().strip()
        if contexts_text:
            body["consumption_contexts"] = [
                s.strip() for s in contexts_text.split(",") if s.strip()
            ]
        # trigger_keywords
        keywords_text = self._keywords_edit.text().strip()
        if keywords_text:
            body["trigger_keywords"] = [
                s.strip() for s in keywords_text.split(",") if s.strip()
            ]
        return body

    # —— 提交 ——

    def _on_submit_clicked(self) -> None:
        """提交按钮 → 校验通过后调 POST /memory/{key}"""
        key = self._key_edit.text().strip()
        if not key or not _KEY_PATTERN.match(key):
            self._status_label.setText("key 校验失败")
            return
        data_text = self._data_edit.toPlainText().strip()
        try:
            parsed_data = json.loads(data_text)
            if not isinstance(parsed_data, dict):
                raise ValueError("data must be a dict")
        except (json.JSONDecodeError, ValueError) as e:
            self._status_label.setText(f"data 解析失败：{e}")
            return

        body = self._build_body(parsed_data)
        worker = HttpWorker(
            "post",
            f"/memory/{key}",
            json=body,
            http_client=self._http,
        )
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_submit_done)
        worker.failed.connect(self._on_submit_failed)
        worker.start()
        self._submit_btn.setEnabled(False)
        self._status_label.setText(f"正在写入记忆 '{key}' ...")

    def _on_submit_done(self, data: Any) -> None:
        """提交成功 → emit jump_to_memory_list(key) 信号"""
        key = self._key_edit.text().strip()
        if not isinstance(data, dict):
            self._status_label.setText("写入成功（返回数据格式异常）")
        else:
            status = data.get("status", "ok")
            msg_id = data.get("id", "")
            self._status_label.setText(
                f"写入成功：key={key} status={status} id={msg_id}"
            )
        self._submit_btn.setEnabled(True)
        # emit 信号由 MemoryPanel 转发到记忆库页
        if key:
            self.jump_to_memory_list.emit(key)

    def _on_submit_failed(self, err: str) -> None:
        self._status_label.setText(f"写入失败：{err[:80]}")
        self._submit_btn.setEnabled(True)

    # —— 预填 ——

    def prefill_from_key(self, key: str) -> None:
        """从记忆库页「编辑」按钮跳转过来时调用：拉取现有数据预填表单

        Args:
            key: 待编辑的记忆 key
        """
        if not key or not _KEY_PATTERN.match(key):
            self._status_label.setText(f"无效的 key：{key}")
            return
        # 进入编辑模式：key 只读
        self._edit_mode = True
        self._key_edit.setText(key)
        self._key_edit.setReadOnly(True)
        self._status_label.setText(f"正在拉取 '{key}' 现有数据 ...")
        worker = HttpWorker("get", f"/memory/{key}", http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_prefetch_done)
        worker.failed.connect(self._on_prefetch_failed)
        worker.start()

    def _on_prefetch_done(self, data: Any) -> None:
        """GET /memory/{key} 完成后预填表单字段"""
        if not isinstance(data, dict):
            self._status_label.setText("拉取数据格式异常，无法预填")
            return
        # 构造 data dict：剔除元数据字段
        metadata_keys = {
            "source", "updated_at", "fact_type",
            "occurred_at", "consumption_contexts", "trigger_keywords",
            "memory_age", "staleness_warning", "trust_recall_hint",
            "access_count", "confidence", "created_at",
        }
        data_dict: dict[str, Any] = {}
        for k, v in data.items():
            if k in metadata_keys or k.startswith("_"):
                continue
            data_dict[k] = v
        # 预填 data JSON 编辑器
        try:
            self._data_edit.setPlainText(
                json.dumps(data_dict, ensure_ascii=False, indent=2)
            )
        except (TypeError, ValueError):
            self._data_edit.setPlainText(str(data_dict))

        # 预填 fact_type
        fact_type = data.get("fact_type")
        if fact_type:
            idx = self._fact_type_combo.findData(fact_type)
            if idx >= 0:
                self._fact_type_combo.setCurrentIndex(idx)

        # 预填 occurred_at
        occurred_at = data.get("occurred_at")
        if occurred_at:
            try:
                # 兼容 ISO 字符串（含或不含时区）
                dt_str = str(occurred_at)[:19]
                dt = datetime.fromisoformat(dt_str)
                # PySide6 桩不接受 datetime 直接构造 QDateTime，按组件构建
                qdt = QDateTime(
                    QDate(dt.year, dt.month, dt.day),
                    QTime(dt.hour, dt.minute, dt.second),
                )
                self._occurred_edit.setDateTime(qdt)
                self._occurred_check.setChecked(True)
            except (ValueError, TypeError):
                pass

        # 预填 consumption_contexts
        contexts = data.get("consumption_contexts")
        if isinstance(contexts, list):
            self._contexts_edit.setText(", ".join(str(c) for c in contexts))

        # 预填 trigger_keywords
        keywords = data.get("trigger_keywords")
        if isinstance(keywords, list):
            self._keywords_edit.setText(", ".join(str(k) for k in keywords))

        self._status_label.setText("已预填现有数据，可编辑后提交")
        # 触发一次预览 + 校验更新
        self._update_preview_and_validation()

    def _on_prefetch_failed(self, err: str) -> None:
        self._status_label.setText(f"拉取失败：{err[:80]}（可手动填写新建）")
        # 拉取失败不阻塞填写，保持 key 只读（因为编辑模式意图明确）
