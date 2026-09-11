"""概览页 — 健康度评分 + 需关注 + 维护状态 + 高级操作

Ticket 05：替换 Ticket 04 的 demo 占位内容，接入真实功能：
- GET /memory/status 拉数据 → 调 _health_score.calculate_health_score 算分
- 渲染：80px 大字号评分（绿/黄/红）+ 需关注卡片列表（带跳转）+ 维护状态侧边卡片
- 主操作：触发维护 / 重建索引 / 压缩记忆（safe 绿）
- 高级操作折叠区：清理孤立（danger 红，弹 ConfirmDialog）
- 60s QTimer 自动刷新由主 MemoryPanel._on_timer_timeout 触发 refresh_status()
- 串行执行维护操作（self._action_worker 单一 worker，遵守 ADR-0022）

业务术语中文化对照（spec「中文化术语对照表」节）：
- stale → 过时；keep → 保留；archive → 归档；pending → 待处理；auto → 自动；manual → 手动
- 技术术语保留英文无括注：Embedding / DB / LLM / fact_type / source
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.http_worker import HttpWorker
from client.panels.memory._health_score import calculate_health_score
from client.panels.memory._shared import ActionButtonsBar
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role

# 健康度档位 → 颜色 + 中文标签
_LEVEL_STYLE = {
    "green": (tokens.SUCCESS_TEXT, "良好"),
    "yellow": (tokens.WARNING_TEXT, "需关注"),
    "red": (tokens.DANGER_TEXT, "紧急"),
}


class OverviewPage(QWidget):
    """概览页

    信号：
        jump_to(str, dict)：点击「去 XX 查看」链接时发，
            参数：(page_name, filter_params)。
            page_name 取值：overview / memory_list / timeline / search_traces /
            evidence / write。
            filter_params 例：{"stale_only": True}
    """

    jump_to = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._workers: set = set()              # 活跃 HttpWorker 引用，防 GC
        self._action_worker: HttpWorker | None = None  # 串行操作（ADR-0022）
        self._status: dict | None = None
        self._build_ui()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                                   tokens.SPACE_LG, tokens.SPACE_LG)
        layout.setSpacing(tokens.SPACE_MD)

        # —— 顶部：健康度评分 + 维护状态卡片 ——
        top_row = QHBoxLayout()
        top_row.setSpacing(tokens.SPACE_LG)
        top_row.addWidget(self._build_health_card(), 1)
        top_row.addWidget(self._build_maintainer_card(), 1)
        layout.addLayout(top_row)

        # —— 中部：需关注列表 ——
        concerns_title = QLabel("需关注")
        set_text_role(concerns_title, "heading")
        layout.addWidget(concerns_title)

        self._concerns_container = QVBoxLayout()
        self._concerns_container.setSpacing(tokens.SPACE_SM)
        layout.addLayout(self._concerns_container)
        # 初始空状态提示（首次 status 拉取前显示）
        initial_empty = QLabel("暂无需关注项 · 系统健康")
        set_text_role(initial_empty, "secondary")
        self._concerns_container.addWidget(initial_empty)
        layout.addStretch(1)

        # —— 底部：操作按钮分级条 ——
        action_title = QLabel("操作")
        set_text_role(action_title, "heading")
        layout.addWidget(action_title)

        self._action_bar = ActionButtonsBar()
        self._action_bar.add_safe_action("触发维护", self._on_trigger_maintain)
        self._action_bar.add_safe_action("重建索引", self._on_reindex)
        self._action_bar.add_safe_action("压缩记忆", self._on_compress)
        self._action_bar.add_danger_action(
            "清理孤立",
            self._on_cleanup_orphaned,
            confirm_title="清理孤立",
            confirm_message="将删除超过 30 天的已压缩消息和旧摘要，不可撤销！",
            risk_level="danger",
        )
        layout.addWidget(self._action_bar)

        self._action_status = QLabel("等待操作")
        set_text_role(self._action_status, "tertiary")
        layout.addWidget(self._action_status)

    def _build_health_card(self) -> QFrame:
        card = QFrame()
        set_kind(card, "card")
        v = QVBoxLayout(card)
        v.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                              tokens.SPACE_LG, tokens.SPACE_LG)
        v.setSpacing(tokens.SPACE_SM)

        title = QLabel("健康度评分")
        set_text_role(title, "title")
        v.addWidget(title)

        # 80px 大字号分数
        self._score_label = QLabel("--")
        self._score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._score_label.font()
        font.setPixelSize(80)
        font.setBold(True)
        self._score_label.setFont(font)
        v.addWidget(self._score_label, 1)

        self._level_label = QLabel("未加载")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_text_role(self._level_label, "heading")
        v.addWidget(self._level_label)
        return card

    def _build_maintainer_card(self) -> QFrame:
        card = QFrame()
        set_kind(card, "card")
        v = QVBoxLayout(card)
        v.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                              tokens.SPACE_LG, tokens.SPACE_LG)
        v.setSpacing(tokens.SPACE_SM)

        title = QLabel("维护状态")
        set_text_role(title, "title")
        v.addWidget(title)

        self._maintainer_label = QLabel("等待加载...")
        self._maintainer_label.setAlignment(Qt.AlignmentFlag.AlignTop
                                              | Qt.AlignmentFlag.AlignLeft)
        self._maintainer_label.setWordWrap(True)
        set_text_role(self._maintainer_label, "secondary")
        v.addWidget(self._maintainer_label, 1)
        return card

    # —— 数据拉取 ——

    def refresh_status(self) -> None:
        """触发 GET /memory/status（异步，HttpWorker）

        由 MemoryPanel._on_timer_timeout / on_show / on_refresh 调用。
        """
        if self._action_worker is not None:
            # 有维护操作在跑，跳过 status 刷新避免并发（ADR-0022）
            return
        worker = HttpWorker("get", "/memory/status", http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_status_done)
        worker.failed.connect(self._on_status_failed)
        worker.start()

    def _on_status_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._status = data
        self._render(data)

    def _on_status_failed(self, err: str) -> None:
        self._score_label.setText("--")
        self._level_label.setText(f"加载失败：{err[:60]}")
        self._maintainer_label.setText("无法获取维护状态")
        self._render_concerns([])

    # —— 渲染 ——

    def _render(self, status: dict) -> None:
        score, level, concerns = calculate_health_score(status)
        self._render_health(score, level)
        self._render_maintainer(status.get("maintainer") or {})
        self._render_concerns(concerns)

    def _render_health(self, score: int, level: str) -> None:
        color, label = _LEVEL_STYLE.get(level, (tokens.TEXT_TERTIARY, "未知"))
        self._score_label.setText(str(score))
        self._score_label.setStyleSheet(f"color: {color};")
        self._level_label.setText(label)
        self._level_label.setStyleSheet(f"color: {color};")

    def _render_maintainer(self, maint: dict) -> None:
        """业务术语中文化 labels"""
        last_run = maint.get("last_run") or "从未运行"
        stale_threshold = maint.get("stale_threshold_days", "--")
        interval_hours = maint.get("interval_hours", "--")
        validate_enabled = maint.get("validate_enabled", False)
        status_counts = maint.get("status_counts") or {}
        archive = status_counts.get("archive", 0)
        keep = status_counts.get("keep", 0)
        pending = status_counts.get("pending", 0)
        stale_total = maint.get("stale_total", 0)

        lines = [
            f"上次运行：{last_run}",
            f"过时阈值（天）：{stale_threshold}",
            f"运行间隔（小时）：{interval_hours}",
            f"LLM 验证：{'已启用' if validate_enabled else '已关闭'}",
            f"归档数：{archive}",
            f"保留数：{keep}",
            f"待处理数：{pending}",
        ]
        # 过时总数 > 0 红色高亮
        stale_line = f"过时总数：{stale_total}"
        if stale_total > 0:
            stale_line = f'<span style="color: {tokens.DANGER_TEXT};">{stale_line}</span>'
        lines.append(stale_line)
        # QLabel 的 AutoText 按开头判断富文本，纯文字开头会让 span 标签原样露出，
        # 显式 RichText + <br> 换行才能让红色高亮生效
        self._maintainer_label.setTextFormat(Qt.TextFormat.RichText)
        self._maintainer_label.setText("<br>".join(lines))

    def _render_concerns(self, concerns: list[dict]) -> None:
        """清空 + 重建 concerns 卡片"""
        # 清空旧卡片
        while self._concerns_container.count():
            item = self._concerns_container.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not concerns:
            empty = QLabel("暂无需关注项 · 系统健康")
            set_text_role(empty, "secondary")
            self._concerns_container.addWidget(empty)
            return

        for c in concerns:
            card = self._build_concern_card(c)
            self._concerns_container.addWidget(card)

    def _build_concern_card(self, concern: dict) -> QFrame:
        """单条 concern 卡片：severity 色点 + 维度 + 消息 + 跳转按钮"""
        card = QFrame()
        set_kind(card, "card")
        h = QHBoxLayout(card)
        h.setContentsMargins(tokens.SPACE_MD, tokens.SPACE_SM,
                              tokens.SPACE_MD, tokens.SPACE_SM)
        h.setSpacing(tokens.SPACE_SM)

        severity = concern.get("severity", "yellow")
        color = tokens.DANGER_TEXT if severity == "red" else tokens.WARNING_TEXT
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color};")
        h.addWidget(dot)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        dim_label = QLabel(concern.get("dimension", ""))
        set_text_role(dim_label, "title")
        info_col.addWidget(dim_label)
        msg_label = QLabel(concern.get("message", ""))
        set_text_role(msg_label, "secondary")
        msg_label.setWordWrap(True)
        info_col.addWidget(msg_label)
        h.addLayout(info_col, 1)

        # 跳转按钮
        jump_to = concern.get("jump_to", "")
        if jump_to:
            btn = QPushButton("去查看 →")
            set_kind(btn, "ghost")
            page_name, params = _parse_jump_to(jump_to)
            btn.clicked.connect(lambda _=False, p=page_name, f=params:
                                  self.jump_to.emit(p, f))
            h.addWidget(btn)
        return card

    # —— 维护操作（串行，ADR-0022）——

    def _on_trigger_maintain(self) -> None:
        self._start_action_worker("/memory/maintain", "触发维护")

    def _on_reindex(self) -> None:
        self._start_action_worker("/memory/reindex", "重建索引")

    def _on_compress(self) -> None:
        self._start_action_worker("/memory/compress", "压缩记忆")

    def _on_cleanup_orphaned(self) -> None:
        self._start_action_worker("/memory/cleanup/orphaned", "清理孤立")

    def _start_action_worker(self, path: str, label: str) -> None:
        """串行启动单一 action worker（ADR-0022 单线程模式）"""
        if self._action_worker is not None:
            self._action_status.setText(
                f"已有操作在执行（{label} 排队等待），遵守 ADR-0022 串行约束"
            )
            return
        self._action_status.setText(f"正在执行：{label} ...")
        worker = HttpWorker("post", path, http_client=self._http)
        self._action_worker = worker
        worker.done.connect(lambda d, lbl=label: self._on_action_done(d, lbl))
        worker.failed.connect(lambda e, lbl=label: self._on_action_failed(e, lbl))
        worker.finished.connect(self._on_action_finished)
        worker.start()

    def _on_action_done(self, data: Any, label: str) -> None:
        self._action_status.setText(f"{label} 完成")
        # 操作完成后自动刷新 status（拉到最新结果）
        self.refresh_status()

    def _on_action_failed(self, err: str, label: str) -> None:
        self._action_status.setText(f"{label} 失败：{err[:80]}")

    def _on_action_finished(self) -> None:
        """worker 结束（无论成功失败）→ 清空 _action_worker 引用"""
        if self._action_worker is not None:
            self._action_worker = None


def _parse_jump_to(jump_to: str) -> tuple[str, dict]:
    """解析 health_score 返回的 jump_to 字符串

    格式：
        "memory_list?stale=true"  → ("memory_list", {"stale": True})
        "overview"                 → ("overview", {})
    """
    if "?" not in jump_to:
        return jump_to, {}
    page, query = jump_to.split("?", 1)
    params: dict[str, Any] = {}
    for kv in query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            # 简单转换：true/false 转布尔，其他保留字符串
            if v.lower() == "true":
                params[k] = True
            elif v.lower() == "false":
                params[k] = False
            else:
                params[k] = v
    return page, params
