"""单卡片 widget：展示一个审批请求 + 倒计时 + 决策按钮。

布局：
- 标题（type + approval_id 摘要）
- 内容预览（QPlainTextEdit 只读）
- LLM 审查意见（如有）
- Agent 说明原因
- 倒计时进度条（QProgressBar）
- feedback 输入框（QTextEdit）
- 按钮行（批准 / 拒绝 / 收到[超时态]）

状态：
- pending：正常可操作，倒计时进行中
- timeout：已超时只读，按钮变为"收到"，feedback 禁用，进度条变红
"""
from __future__ import annotations

import requests
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lib.ui import tokens as T

SERVER_URL = "http://127.0.0.1:8766"
HTTP_TIMEOUT_S = 5.0


class ApprovalCard(QFrame):
    """单张审批卡片。

    信号：
        card_closed(str)  — 卡片应被移除，携带 approval_id
        activity_occurred(str) — 用户有操作（打字/按键），需上报 server 重置 deadline
    """

    card_closed = Signal(str)
    activity_occurred = Signal(str)

    def __init__(self, item: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self._approval_id = item["approval_id"]
        self._item_type = item.get("type", "shell")
        self._total_seconds = max(1, item.get("seconds_left", 180))
        self._remaining = self._total_seconds
        self._is_timeout = item.get("status") == "timeout"

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._build_ui(item)
        self._start_timer()

    # ========== UI 构建 ==========

    def _build_ui(self, item: dict) -> None:
        layout = QVBoxLayout(self)

        # 标题行
        title = f"[{self._item_type.upper()}] {self._approval_id[:20]}"
        layout.addWidget(QLabel(f"<b>{title}</b>"))

        # 内容预览（根据类型展示不同字段）
        if self._item_type == "http":
            header = f"{item.get('method', '')} {item.get('path', '')}".strip()
            body = item.get("body_preview", "")
            preview_text = header + (f"\n\n{body}" if body else "")
        elif self._item_type == "advanced_tool":
            operation_id = item.get("operation_id", "")
            params_preview = item.get("params_preview", "")
            guard_reason = item.get("guard_reason", "")
            agent_reason = item.get("agent_reason", "")
            header = f"工具：{operation_id}".strip()
            sections = [header]
            if params_preview:
                sections.append(f"参数预览：\n{params_preview}")
            if guard_reason:
                sections.append(f"审批原因：{guard_reason}")
            if agent_reason:
                sections.append(f"Agent 理由：{agent_reason}")
            preview_text = "\n\n".join(sections)
        else:
            preview_text = item.get("command", "")
        preview = QPlainTextEdit(preview_text or "(无内容)")
        preview.setReadOnly(True)
        preview.setMaximumHeight(100)
        layout.addWidget(QLabel("待审批内容："))
        layout.addWidget(preview)

        # LLM 审查意见（如有）
        llm_opinion = item.get("llm_opinion", "")
        if llm_opinion:
            llm_view = QPlainTextEdit(llm_opinion)
            llm_view.setReadOnly(True)
            llm_view.setMaximumHeight(60)
            llm_view.setStyleSheet(
                f"QPlainTextEdit {{ border: 1px solid {T.WARNING};"
                f" background: {T.WARNING_WASH}; color: {T.WARNING_TEXT}; }}"
            )
            layout.addWidget(QLabel("LLM 审查意见："))
            layout.addWidget(llm_view)

        # Agent 说明
        agent_reason = item.get("agent_reason", "")
        if agent_reason:
            reason_view = QPlainTextEdit(agent_reason)
            reason_view.setReadOnly(True)
            reason_view.setMaximumHeight(60)
            layout.addWidget(QLabel("Agent 说明："))
            layout.addWidget(reason_view)

        # 拦截原因
        guard_reason = item.get("guard_reason", "")
        if guard_reason:
            layout.addWidget(QLabel(f"拦截原因：{guard_reason}"))

        # 倒计时进度条
        self._progress = QProgressBar()
        self._progress.setMaximum(self._total_seconds)
        self._progress.setValue(self._remaining)
        self._progress.setFormat("剩余 %v 秒")
        layout.addWidget(self._progress)

        # feedback 输入框
        self._feedback = QTextEdit()
        self._feedback.setPlaceholderText("可选：补充批准或拒绝理由（将返回给 Agent）")
        self._feedback.setMaximumHeight(80)
        # textChanged 重置倒计时 + 上报 activity
        self._feedback.textChanged.connect(self._on_user_activity)
        layout.addWidget(QLabel("补充理由（可选）："))
        layout.addWidget(self._feedback)

        # 按钮行
        self._button_row = QHBoxLayout()
        self._button_row.addStretch()
        self._deny_btn = QPushButton("拒绝")
        self._deny_btn.setStyleSheet(f"background: {T.DANGER}; color: {T.TEXT_PRIMARY}; padding: 6px 16px;")
        self._approve_btn = QPushButton("批准一次")
        self._approve_btn.setStyleSheet(f"background: {T.SUCCESS}; color: {T.TEXT_PRIMARY}; padding: 6px 16px;")
        self._ack_btn = QPushButton("收到")
        self._ack_btn.setStyleSheet(f"background: {T.TEXT_TERTIARY}; color: {T.TEXT_PRIMARY}; padding: 6px 16px;")
        self._button_row.addWidget(self._deny_btn)
        self._button_row.addWidget(self._approve_btn)
        self._button_row.addWidget(self._ack_btn)
        layout.addLayout(self._button_row)

        self._approve_btn.clicked.connect(lambda: self._submit_decision("approve"))
        self._deny_btn.clicked.connect(lambda: self._submit_decision("deny"))
        self._ack_btn.clicked.connect(self._ack_timeout)

        # 根据状态切换按钮
        self._update_button_visibility()

    def _update_button_visibility(self) -> None:
        """根据卡片状态切换按钮可见性。"""
        if self._is_timeout:
            self._approve_btn.setVisible(False)
            self._deny_btn.setVisible(False)
            self._ack_btn.setVisible(True)
            self._feedback.setReadOnly(True)
            self._feedback.setPlaceholderText("（已超时，不可操作）")
            self._progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.DANGER}; }}")
            self._progress.setFormat("已超时")
        else:
            self._approve_btn.setVisible(True)
            self._deny_btn.setVisible(True)
            self._ack_btn.setVisible(False)
            self._feedback.setReadOnly(False)

    # ========== 倒计时 ==========

    def _start_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(1000)  # 1s
        self._timer.timeout.connect(self._on_tick)
        if not self._is_timeout:
            self._timer.start()

    def _on_tick(self) -> None:
        if self._is_timeout:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._progress.setValue(0)
            self._timer.stop()
            self._on_countdown_expired()
            return
        self._progress.setValue(self._remaining)
        # 接近超时变色
        if self._remaining <= 30:
            self._progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {T.DANGER}; }}")

    def _on_countdown_expired(self) -> None:
        """倒计时归零：上报 timeout，转为只读态。"""
        try:
            requests.post(
                f"{SERVER_URL}/approvals/{self._approval_id}/decision",
                json={"decision": "timeout", "feedback": ""},
                timeout=HTTP_TIMEOUT_S,
            )
        except requests.RequestException:
            pass  # 网络失败不阻塞 UI 状态转换
        self._is_timeout = True
        self._update_button_visibility()

    def _on_user_activity(self) -> None:
        """用户打字/按键时重置倒计时 + 上报 activity。"""
        if self._is_timeout:
            return
        self._remaining = self._total_seconds
        self._progress.setValue(self._remaining)
        self._progress.setStyleSheet("")  # 恢复默认色
        self.activity_occurred.emit(self._approval_id)

    def keyPressEvent(self, event) -> None:
        """任意按键重置倒计时（兜底）。"""
        if not self._is_timeout:
            self._on_user_activity()
        super().keyPressEvent(event)

    # ========== 决策提交 ==========

    def _submit_decision(self, decision: str) -> None:
        """提交 approve/deny 决策。"""
        feedback = self._feedback.toPlainText().strip()
        try:
            resp = requests.post(
                f"{SERVER_URL}/approvals/{self._approval_id}/decision",
                json={"decision": decision, "feedback": feedback},
                timeout=HTTP_TIMEOUT_S,
            )
            if resp.status_code == 200:
                self._timer.stop()
                self.card_closed.emit(self._approval_id)
            elif resp.status_code == 409:
                # 状态已变更（可能已超时），转为超时态
                self._is_timeout = True
                self._update_button_visibility()
        except requests.RequestException:
            pass  # 网络失败暂不关闭卡片，用户可重试

    def _ack_timeout(self) -> None:
        """点"收到"移除已超时卡片。"""
        try:
            resp = requests.post(
                f"{SERVER_URL}/approvals/{self._approval_id}/ack",
                timeout=HTTP_TIMEOUT_S,
            )
            if resp.status_code == 200:
                self._timer.stop()
                self.card_closed.emit(self._approval_id)
        except requests.RequestException:
            pass

    # ========== 公共方法 ==========

    @property
    def approval_id(self) -> str:
        return self._approval_id

    def update_from_pending(self, item: dict) -> None:
        """从新的 pending 数据更新卡片（如 seconds_left 变化）。"""
        if self._is_timeout:
            return
        new_remaining = item.get("seconds_left", self._remaining)
        if new_remaining != self._remaining:
            self._remaining = max(0, new_remaining)
            self._progress.setValue(self._remaining)

    def mark_timeout(self) -> None:
        """外部（panel）标记此卡片为超时态（server 端已 timeout）。"""
        if self._is_timeout:
            return
        self._timer.stop()
        self._is_timeout = True
        self._update_button_visibility()
