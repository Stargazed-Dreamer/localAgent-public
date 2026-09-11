"""记忆库页 — 表格 + 筛选 + 详情 + 编辑跳转 + 删除

Ticket 06：替换 Ticket 03 占位页，接入真实功能：
- 顶部 FilterBar：fact_type 下拉 / source 下拉 / 过时复选 / 时间范围 / 关键词
- 中间 TableWithDetail：5 列表格（key / 类型 / 来源 / 更新时间 / 过时状态）+ 下方 Markdown 详情区
- 行点击：row_activated → 异步拉 GET /memory/{key} → 渲染完整字段 markdown
- 编辑按钮（中等黄色）：emit jump_to_write_page(key) 信号由主 MemoryPanel 转发到写入页（Ticket 10 接收）
- 删除按钮（危险红色，折叠「高级操作」）：弹 ConfirmDialog danger → DELETE /memory/{key} → 刷新列表
- 跳转链接接收：MemoryPanel 转发 overview 的 jump_to("memory_list", {"stale": true}) → 应用筛选 + 刷新列表

业务术语中文化对照（spec「中文化术语对照表」节）：
- stale → 过时；auto → 自动；manual → 手动
- 技术术语保留英文无括注：fact_type / source 值（tool/agent/user）/ key
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from client.core.http_client import HttpClient
from client.core.http_worker import HttpWorker
from client.panels.memory._shared import (
    ActionButtonsBar,
    FilterBar,
    TableWithDetail,
)
from lib.ui import tokens
from lib.ui.theme import set_text_role

# spec D14 staleness 阈值（镜像 server/memory/router.py STALENESS_THRESHOLDS_DAYS）
STALENESS_THRESHOLDS_DAYS = {
    "user": 90,
    "feedback": 30,
    "project": 7,
    "reference": 30,
    "experience": 14,
}


def _compute_stale(fact_type: str | None, updated_at: str | None) -> bool:
    """客户端 staleness 计算（仅用于 UI 显示「过时/正常」标签）

    与服务端 _compute_staleness 一致：fact_type 不在 closed 内或 updated_at 无法解析 → False
    """
    if not fact_type or fact_type not in STALENESS_THRESHOLDS_DAYS:
        return False
    if not updated_at:
        return False
    try:
        # facts 表 updated_at 格式：'YYYY-MM-DD HH:MM:SS'
        dt = datetime.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
        days_since = (datetime.now() - dt).days
        return days_since > STALENESS_THRESHOLDS_DAYS[fact_type]
    except (ValueError, TypeError):
        return False


def _fact_source_label(value: str | None) -> str:
    """fact_source 值中文化：auto → 自动 / manual → 手动 / 其他 → 原值"""
    if value == "auto":
        return "自动"
    if value == "manual":
        return "手动"
    return value or ""


class MemoryListPage(QWidget):
    """记忆库页

    信号：
        jump_to_write_page(str)：编辑按钮点击时发，参数为 key。
            由主 MemoryPanel 转发到写入页（Ticket 10 接收预填）。
    """

    jump_to_write_page = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._workers: set = set()
        self._all_rows: list[dict] = []      # 原始 list 数据（未筛选）
        self._current_key: str | None = None  # 当前选中行的 key
        self._pending_highlight_key: str | None = None  # 刷新后需高亮的 key
        self._build_ui()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                                   tokens.SPACE_LG, tokens.SPACE_LG)
        layout.setSpacing(tokens.SPACE_MD)

        title = QLabel("记忆库")
        set_text_role(title, "heading")
        layout.addWidget(title)

        # 筛选条
        self._filter_bar = FilterBar()
        self._filter_bar.filters_changed.connect(self._on_filters_changed)
        layout.addWidget(self._filter_bar)

        # 表格 + 详情
        self._table = TableWithDetail()
        self._table.set_columns([
            {"key": "key", "label": "key", "width": 180},
            {"key": "fact_type", "label": "类型", "width": 100},
            {"key": "fact_source_label", "label": "来源", "width": 80},
            {"key": "updated_at", "label": "更新时间", "width": 160},
            {"key": "stale_label", "label": "过时状态", "width": 80},
        ])
        self._table.row_activated.connect(self._on_row_activated)
        layout.addWidget(self._table, 1)

        # 操作按钮条
        self._action_bar = ActionButtonsBar()
        self._edit_btn = self._action_bar.add_medium_action("编辑", self._on_edit_clicked)
        self._edit_btn.setEnabled(False)
        self._action_bar.add_danger_action(
            "删除",
            self._on_delete_clicked,
            confirm_title="删除记忆",
            confirm_message=lambda: f"确认删除记忆 '{self._current_key}'？此操作不可撤销！",
            risk_level="danger",
        )
        layout.addWidget(self._action_bar)

        self._status_label = QLabel("等待查询")
        set_text_role(self._status_label, "tertiary")
        layout.addWidget(self._status_label)

    # —— 公共 API ——

    def refresh_list(self) -> None:
        """触发 GET /memory/list（异步 HttpWorker）"""
        worker = HttpWorker("get", "/memory/list", http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_list_done)
        worker.failed.connect(self._on_list_failed)
        worker.start()
        self._status_label.setText("正在查询记忆列表 ...")

    def refresh_and_highlight(self, key: str) -> None:
        """刷新列表 + 刷新后选中并高亮指定 key 的行

        用于 WritePage 提交成功后跳回记忆库页场景：
        emit jump_to_memory_list(highlight_key=key) → MemoryPanel 转发 → 调本方法

        Args:
            key: 刷新后需选中并高亮的记忆 key
        """
        self._pending_highlight_key = key
        self.refresh_list()
        self._status_label.setText(f"正在刷新并定位 '{key}' ...")

    def apply_jump_params(self, params: dict) -> None:
        """接收 overview 跳转带来的筛选参数并应用

        支持的 params keys（参考 _parse_jump_to）：
            - stale: True → 勾选「仅看过时」
            - fact_type: str → 设置类型下拉
            - source: str → 设置来源下拉
            - keyword: str → 设置关键词
        """
        if not params:
            self.refresh_list()
            return
        if "stale" in params:
            self._filter_bar.set_filter("stale_only", bool(params["stale"]))
        if "fact_type" in params:
            self._filter_bar.set_filter("fact_type", params["fact_type"])
        if "source" in params:
            self._filter_bar.set_filter("source", params["source"])
        if "keyword" in params:
            self._filter_bar.set_filter("keyword", params["keyword"])
        # 应用筛选后立即刷新（apply filters + 拉数据）
        self._on_filters_changed(self._filter_bar.filters())
        self.refresh_list()

    # —— 数据拉取 ——

    def _on_list_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._status_label.setText("返回数据格式异常")
            return
        keys_data = data.get("keys") or []
        if not isinstance(keys_data, list):
            self._status_label.setText("返回数据格式异常")
            return
        # 预处理：补充 source_label 和 stale_label 字段供表格显示
        self._all_rows = []
        for row in keys_data:
            if not isinstance(row, dict):
                continue
            # source 统一字段名（原 list facts 分支用 fact_source，现已统一为 source）
            row["fact_source_label"] = _fact_source_label(row.get("source"))
            row["stale_label"] = (
                "过时" if _compute_stale(row.get("fact_type"), row.get("updated_at"))
                else "正常"
            )
            self._all_rows.append(row)
        self._status_label.setText(f"已加载 {len(self._all_rows)} 条记忆")
        self._apply_filters(self._filter_bar.filters())
        # 刷新后应用 pending highlight（WritePage 提交成功跳回场景）
        if self._pending_highlight_key:
            target = self._pending_highlight_key
            self._pending_highlight_key = None
            found = self._table.select_row_by_field("key", target)
            if found:
                self._status_label.setText(
                    f"已定位 '{target}'（共 {len(self._all_rows)} 条记忆）"
                )
            else:
                self._status_label.setText(
                    f"未找到 '{target}'（共 {len(self._all_rows)} 条记忆）"
                )

    def _on_list_failed(self, err: str) -> None:
        self._status_label.setText(f"查询失败：{err[:80]}")
        self._all_rows = []
        self._table.clear()

    # —— 筛选 ——

    def _on_filters_changed(self, filters: dict) -> None:
        self._apply_filters(filters)

    def _apply_filters(self, filters: dict) -> None:
        rows = list(self._all_rows)
        fact_type = filters.get("fact_type", "")
        source = filters.get("source", "")
        stale_only = filters.get("stale_only", False)
        keyword = (filters.get("keyword", "") or "").lower()

        filtered = []
        for row in rows:
            if fact_type and row.get("fact_type") != fact_type:
                continue
            if source and row.get("source") != source:
                continue
            if stale_only and row.get("stale_label") != "过时":
                continue
            if keyword:
                row_key = (row.get("key") or "").lower()
                row_summary = (row.get("summary") or "").lower()
                if keyword not in row_key and keyword not in row_summary:
                    continue
            filtered.append(row)
        self._table.set_rows(filtered)

    # —— 行选中 ——

    def _on_row_activated(self, row: dict) -> None:
        """行点击 → 异步拉 GET /memory/{key} 详情 + 渲染"""
        key = row.get("key") if isinstance(row, dict) else None
        self._current_key = key
        self._edit_btn.setEnabled(key is not None)
        if not key:
            self._table.set_detail_markdown("")
            return
        worker = HttpWorker("get", f"/memory/{key}", http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_detail_done)
        worker.failed.connect(self._on_detail_failed)
        worker.start()

    def _on_detail_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._table.set_detail_markdown("*详情数据格式异常*")
            return
        md = self._render_detail_md(data)
        self._table.set_detail_markdown(md)

    def _on_detail_failed(self, err: str) -> None:
        self._table.set_detail_markdown(f"*拉取详情失败：{err}*")

    @staticmethod
    def _render_detail_md(data: dict) -> str:
        """把 /memory/{key} 返回 dict 渲染为分字段 markdown"""
        lines: list[str] = []
        # 标题
        key = data.get("key", "")
        lines.append(f"# {key}")
        lines.append("")
        # 元数据卡片
        lines.append("## 元数据")
        lines.append("")
        lines.append("| 字段 | 值 |")
        lines.append("|---|---|")
        for k in ("source", "fact_type", "updated_at",
                  "occurred_at", "memory_age", "staleness_warning"):
            v = data.get(k)
            if v is None or v == "":
                continue
            v_str = str(v).replace("|", "\\|")
            if len(v_str) > 100:
                v_str = v_str[:97] + "..."
            lines.append(f"| {k} | {v_str} |")
        lines.append("")
        # 消费场景 + 触发关键词
        contexts = data.get("consumption_contexts") or []
        if isinstance(contexts, list) and contexts:
            lines.append("## 消费场景")
            lines.append("")
            for ctx in contexts:
                lines.append(f"- `{ctx}`")
            lines.append("")
        triggers = data.get("trigger_keywords") or []
        if isinstance(triggers, list) and triggers:
            lines.append("## 触发关键词")
            lines.append("")
            for kw in triggers:
                lines.append(f"- `{kw}`")
            lines.append("")
        # 数据字段（除元数据外的所有 dict 顶层键）
        reserved = {"source", "fact_type", "updated_at",
                    "occurred_at", "memory_age", "staleness_warning",
                    "trust_recall_hint", "consumption_contexts",
                    "trigger_keywords", "key"}
        extra = [(k, v) for k, v in data.items() if k not in reserved]
        if extra:
            lines.append("## 数据字段")
            lines.append("")
            import json as _json
            for k, v in extra:
                try:
                    v_str = _json.dumps(v, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    v_str = str(v)
                if len(v_str) > 400:
                    v_str = v_str[:397] + "..."
                lines.append(f"**{k}**")
                lines.append("```json")
                lines.append(v_str)
                lines.append("```")
                lines.append("")
        return "\n".join(lines)

    # —— 编辑 / 删除 ——

    def _on_edit_clicked(self) -> None:
        if not self._current_key:
            return
        self.jump_to_write_page.emit(self._current_key)

    def _on_delete_clicked(self) -> None:
        if not self._current_key:
            return
        key = self._current_key
        worker = HttpWorker("delete", f"/memory/{key}", http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(lambda d, k=key: self._on_delete_done(d, k))
        worker.failed.connect(self._on_delete_failed)
        worker.start()
        self._status_label.setText(f"正在删除记忆 '{key}' ...")

    def _on_delete_done(self, data: Any, key: str) -> None:
        self._status_label.setText(f"已删除记忆 '{key}'")
        self._current_key = None
        self._edit_btn.setEnabled(False)
        self._table.set_detail_markdown("")
        # 刷新列表
        self.refresh_list()

    def _on_delete_failed(self, err: str) -> None:
        self._status_label.setText(f"删除失败：{err[:80]}")
