"""时间线页 — 表格 + 筛选 + 详情

Ticket 07：替换 Ticket 03 占位页，接入真实功能：
- 顶部筛选条：时间范围 QCheckBox + QDateTimeEdit 起 / 止 + source 下拉（全部/tool/agent/user）
  + 数量 QSpinBox（1-500，默认 50）+ 查询按钮 + 重置按钮
- 中间 TableWithDetail：5 列表格（时间 / 来源 / 角色 / 预览 / 操作）
  - 注：本页操作列空（仅展示，无编辑/删除）
- 下方详情区：解析 content JSON 分字段展示
  - 工具调用：显示工具名 / 参数 / 结果 / 耗时
  - 事实：显示 name/title/summary/description/content
  - 其他：显示原文
- 列点击排序

业务术语中文化：source 值 tool/agent/user 保留英文无括注（技术术语）
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.http_worker import HttpWorker
from client.panels.memory._shared import TableWithDetail
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

_SOURCES = ["", "tool", "agent", "user"]  # 空串 = 全部


class TimelinePage(QWidget):
    """时间线页"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._workers: set = set()
        self._all_rows: list[dict] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                                   tokens.SPACE_LG, tokens.SPACE_LG)
        layout.setSpacing(tokens.SPACE_MD)

        title = QLabel("时间线")
        set_text_role(title, "heading")
        layout.addWidget(title)

        # 筛选条
        filter_row = QHBoxLayout()
        filter_row.setSpacing(tokens.SPACE_SM)

        filter_row.addWidget(self._label("时间范围"))
        self._time_check = QCheckBox()
        self._time_check.toggled.connect(self._on_time_toggled)
        filter_row.addWidget(self._time_check)

        self._time_from = QDateTimeEdit(QDateTime.currentDateTime().addDays(-7))
        self._time_from.setCalendarPopup(True)
        self._time_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._time_from.setEnabled(False)
        filter_row.addWidget(self._time_from)

        filter_row.addWidget(self._label("至"))
        self._time_to = QDateTimeEdit(QDateTime.currentDateTime())
        self._time_to.setCalendarPopup(True)
        self._time_to.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._time_to.setEnabled(False)
        filter_row.addWidget(self._time_to)

        filter_row.addWidget(self._label("来源"))
        self._source_combo = QComboBox()
        for s in _SOURCES:
            self._source_combo.addItem("全部" if s == "" else s, s)
        filter_row.addWidget(self._source_combo)

        filter_row.addWidget(self._label("数量"))
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 500)
        self._limit_spin.setValue(50)
        filter_row.addWidget(self._limit_spin)

        self._search_btn = QPushButton("查询")
        set_kind(self._search_btn, "primary")
        self._search_btn.clicked.connect(self.refresh)
        filter_row.addWidget(self._search_btn)

        self._reset_btn = QPushButton("重置")
        set_kind(self._reset_btn, "ghost")
        self._reset_btn.clicked.connect(self._on_reset)
        filter_row.addWidget(self._reset_btn)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # 表格 + 详情
        self._table = TableWithDetail()
        self._table.set_columns([
            {"key": "created_at_label", "label": "时间", "width": 160},
            {"key": "source", "label": "来源", "width": 80},
            {"key": "role", "label": "角色", "width": 80},
            {"key": "preview", "label": "预览", "width": 300},
        ])
        self._table.row_activated.connect(self._on_row_activated)
        layout.addWidget(self._table, 1)

        self._status_label = QLabel("等待查询")
        set_text_role(self._status_label, "tertiary")
        layout.addWidget(self._status_label)

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        set_text_role(lbl, "secondary")
        return lbl

    def _on_time_toggled(self, on: bool) -> None:
        self._time_from.setEnabled(on)
        self._time_to.setEnabled(on)

    def _on_reset(self) -> None:
        self._time_check.setChecked(False)
        self._source_combo.setCurrentIndex(0)
        self._limit_spin.setValue(50)
        self.refresh()

    # —— 公共 API ——

    def refresh(self) -> None:
        """触发 GET /memory/timeline（异步 HttpWorker）"""
        params: dict[str, Any] = {"limit": self._limit_spin.value()}
        if self._time_check.isChecked():
            params["since"] = self._time_from.dateTime().toSecsSinceEpoch()
            params["until"] = self._time_to.dateTime().toSecsSinceEpoch()
        source = self._source_combo.currentData()
        if source:
            params["source"] = source
        worker = HttpWorker("get", "/memory/timeline", params=params,
                             http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_timeline_done)
        worker.failed.connect(self._on_timeline_failed)
        worker.start()
        self._status_label.setText("正在查询时间线 ...")

    def apply_jump_params(self, params: dict) -> None:
        """接收 overview 跳转参数（如 time_enabled=True + time_from/to）"""
        if not params:
            self.refresh()
            return
        if "time_enabled" in params:
            self._time_check.setChecked(bool(params["time_enabled"]))
        if "source" in params:
            idx = self._source_combo.findData(params["source"])
            if idx >= 0:
                self._source_combo.setCurrentIndex(idx)
        self.refresh()

    # —— 数据回调 ——

    def _on_timeline_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._status_label.setText("返回数据格式异常")
            return
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            self._status_label.setText("返回数据格式异常")
            return
        self._all_rows = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            m["created_at_label"] = _format_ts(m.get("created_at"))
            m["preview"] = _truncate(m.get("content", ""), 60)
            self._all_rows.append(m)
        self._status_label.setText(f"已加载 {len(self._all_rows)} 条消息")
        self._table.set_rows(self._all_rows)

    def _on_timeline_failed(self, err: str) -> None:
        self._status_label.setText(f"查询失败：{err[:80]}")
        self._all_rows = []
        self._table.clear()

    def _on_row_activated(self, row: dict) -> None:
        """行点击 → 直接渲染 detail（无需额外 HTTP，content 已在 list 返回）"""
        md = _render_timeline_detail_md(row)
        self._table.set_detail_markdown(md)


def _format_ts(ts: Any) -> str:
    """格式化时间戳为可读字符串"""
    if not ts:
        return ""
    try:
        # 时间戳可能是秒或毫秒
        ts_int = float(ts)
        if ts_int > 1e12:  # 毫秒
            ts_int = ts_int / 1000
        return datetime.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)[:19]


def _truncate(s: Any, n: int) -> str:
    """截断字符串到 n 字符"""
    if not s:
        return ""
    s_str = str(s)
    return s_str if len(s_str) <= n else s_str[:n - 1] + "…"


def _render_timeline_detail_md(row: dict) -> str:
    """渲染单条消息详情为 markdown"""
    lines: list[str] = []
    msg_id = row.get("id", "")
    lines.append(f"# 消息 #{msg_id}")
    lines.append("")
    lines.append("## 元数据")
    lines.append("")
    lines.append("| 字段 | 值 |")
    lines.append("|---|---|")
    for k in ("source", "role", "key", "session_id", "created_at",
              "created_at_label"):
        v = row.get(k)
        if v is None or v == "":
            continue
        v_str = str(v).replace("|", "\\|")
        if len(v_str) > 100:
            v_str = v_str[:97] + "..."
        lines.append(f"| {k} | {v_str} |")
    lines.append("")

    content = row.get("content", "")
    if not content:
        return "\n".join(lines)

    # 尝试解析 content 为 JSON
    parsed = None
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            parsed = None
    elif isinstance(content, (dict, list)):
        parsed = content

    if parsed is None:
        # 非 JSON，直接显示原文
        lines.append("## 内容")
        lines.append("")
        lines.append("```")
        lines.append(str(content))
        lines.append("```")
        return "\n".join(lines)

    # 解析为 dict：尝试识别工具调用 / 事实等结构
    if isinstance(parsed, dict):
        # 工具调用模式
        tool = parsed.get("tool") or parsed.get("function") or parsed.get("name")
        if tool and ("arguments" in parsed or "parameters" in parsed
                     or "result" in parsed or "args" in parsed):
            lines.append("## 工具调用")
            lines.append("")
            lines.append(f"**工具**：`{tool}`")
            args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("args")
            if args is not None:
                lines.append("")
                lines.append("**参数**：")
                lines.append("```json")
                try:
                    lines.append(json.dumps(args, ensure_ascii=False, indent=2))
                except (TypeError, ValueError):
                    lines.append(str(args))
                lines.append("```")
            result = parsed.get("result") or parsed.get("output")
            if result is not None:
                lines.append("")
                lines.append("**结果**：")
                lines.append("```")
                result_str = str(result)
                if len(result_str) > 500:
                    result_str = result_str[:497] + "..."
                lines.append(result_str)
                lines.append("```")
            duration = parsed.get("duration_ms") or parsed.get("duration")
            if duration is not None:
                lines.append("")
                lines.append(f"**耗时**：{duration} ms")
            return "\n".join(lines)

        # 事实模式
        fact_keys = ("name", "title", "summary", "description", "content")
        if any(k in parsed for k in fact_keys):
            lines.append("## 事实")
            lines.append("")
            for k in fact_keys:
                v = parsed.get(k)
                if v is None:
                    continue
                lines.append(f"**{k}**")
                lines.append("")
                lines.append(str(v))
                lines.append("")
            # 其他字段
            extra = [(k, v) for k, v in parsed.items()
                     if k not in fact_keys and not k.startswith("_")]
            if extra:
                lines.append("## 其他字段")
                lines.append("")
                lines.append("```json")
                try:
                    lines.append(json.dumps(dict(extra), ensure_ascii=False, indent=2))
                except (TypeError, ValueError):
                    lines.append(str(dict(extra)))
                lines.append("```")
            return "\n".join(lines)

    # 兜底：完整 JSON dump
    lines.append("## 完整内容")
    lines.append("")
    lines.append("```json")
    try:
        lines.append(json.dumps(parsed, ensure_ascii=False, indent=2)[:2000])
    except (TypeError, ValueError):
        lines.append(str(parsed)[:2000])
    lines.append("```")
    return "\n".join(lines)
