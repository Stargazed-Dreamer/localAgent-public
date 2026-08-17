"""Standalone PySide6 dialog for destructive command approval.

改造说明（Ticket 03）：
- 加内部 QTimer 倒计时（从 payload.gui_timeout_seconds 读取，默认 180s）
- 加 QProgressBar 可视化剩余秒数
- feedback QTextEdit 的 textChanged 信号 + dialog keyPressEvent 重置倒计时
- 倒计时归零自动 deny 关闭返回
- server 端 gui_timeout 作为兜底（稍大于内部超时，防 subprocess 卡死）

payload 可选新增 gui_timeout_seconds 字段（int）。未传时默认 180s，向后兼容。
"""

from __future__ import annotations

import json
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)


def main() -> int:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    # QApplication 需保持引用防 GC（QApplication.instance() 内部也持有，但显式赋值更安全）
    _app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    dialog = ApprovalDialog(payload)
    dialog.exec()
    sys.stdout.buffer.write(json.dumps(dialog.result, ensure_ascii=False).encode("utf-8"))
    return 0


class ApprovalDialog(QDialog):
    """审批弹窗（含内部倒计时 + textChanged/keyPress 重置）。"""

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LocalAgent 命令安全确认")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(720, 560)

        # 超时配置（秒），payload 可选传入，默认 180
        self._timeout_seconds: int = int(payload.get("gui_timeout_seconds", 180))
        self._remaining: int = self._timeout_seconds

        # 结果（默认 deny，超时/拒绝时保持 deny）
        self.result: dict = {"decision": "deny", "feedback": ""}

        self._build_ui(payload)
        self._start_timer()

    # ========== UI 构建 ==========

    def _build_ui(self, payload: dict) -> None:

        layout = QVBoxLayout(self)

        # LLM 审查意见（仅 LLM DENY 直接人审时存在）
        llm_opinion = payload.get("llm_opinion") or ""
        if llm_opinion:
            llm_label = QLabel("🤖 LLM 审查意见（拒绝理由）：")
            llm_opinion_view = QPlainTextEdit(llm_opinion)
            llm_opinion_view.setReadOnly(True)
            llm_opinion_view.setMaximumHeight(80)
            llm_opinion_view.setStyleSheet(
                "QPlainTextEdit { border: 1px solid #FFB300; background: #2a2008; color: #FFD54F; }"
            )
            layout.addWidget(llm_label)
            layout.addWidget(llm_opinion_view)

        layout.addWidget(QLabel("Agent 说明的执行原因："))
        reason_view = QPlainTextEdit(payload.get("agent_reason") or "Agent 未提供执行原因")
        reason_view.setReadOnly(True)
        reason_view.setMaximumHeight(90)
        layout.addWidget(reason_view)

        req_type = payload.get("type", "shell")
        if req_type == "http":
            method = payload.get("method", "")
            path = payload.get("path", "")
            body_preview = payload.get("body_preview", "")
            header = f"{method} {path}".strip()
            layout.addWidget(QLabel("待审批的 HTTP 请求："))
            command_view = QPlainTextEdit(header + ("\n\n" + body_preview if body_preview else ""))
        elif req_type == "advanced_tool":
            operation_id = payload.get("operation_id", "")
            params_preview = payload.get("params_preview", "")
            guard_reason = payload.get("guard_reason", "")
            agent_reason = payload.get("agent_reason", "")
            header = f"工具：{operation_id}".strip()
            sections = [header]
            if params_preview:
                sections.append(f"参数预览：\n{params_preview}")
            if guard_reason:
                sections.append(f"审批原因：{guard_reason}")
            if agent_reason:
                sections.append(f"Agent 理由：{agent_reason}")
            layout.addWidget(QLabel("待审批的网关工具调用："))
            command_view = QPlainTextEdit("\n\n".join(sections))
        else:
            layout.addWidget(QLabel("准备执行的完整命令："))
            command_view = QPlainTextEdit(payload.get("command", ""))
        command_view.setReadOnly(True)
        command_view.setMaximumHeight(220)
        layout.addWidget(command_view)

        rule_label = QLabel(f"拦截原因：{payload.get('guard_reason') or '命中破坏性命令规则'}")
        rule_label.setWordWrap(True)
        layout.addWidget(rule_label)

        layout.addWidget(QLabel("可选：补充批准或拒绝理由（将返回给 Agent）："))
        self._feedback = QTextEdit()
        self._feedback.setPlaceholderText("例如：允许，因为目标是已审核的临时目录；或拒绝，请改用非破坏性方案。")
        # textChanged 信号重置倒计时（用户在打字时不超时）
        self._feedback.textChanged.connect(self._reset_countdown)
        layout.addWidget(self._feedback)

        # 倒计时进度条
        self._progress = QProgressBar()
        self._progress.setMaximum(self._timeout_seconds)
        self._progress.setValue(self._remaining)
        self._progress.setFormat("剩余 %v 秒（%m 秒总时长）")
        self._progress.setTextVisible(True)
        layout.addWidget(self._progress)

        # 按钮行
        buttons = QHBoxLayout()
        buttons.addStretch()
        deny = QPushButton("拒绝")
        deny.setStyleSheet("background: #b42318; color: white; padding: 8px 18px;")
        approve = QPushButton("批准一次")
        approve.setStyleSheet("background: #067647; color: white; padding: 8px 18px;")
        buttons.addWidget(deny)
        buttons.addWidget(approve)
        layout.addLayout(buttons)

        approve.clicked.connect(lambda: self._finish("approve"))
        deny.clicked.connect(lambda: self._finish("deny"))
        self.rejected.connect(
            lambda: self.result.update(decision="deny", feedback=self._feedback.toPlainText().strip())
        )

    # ========== 倒计时逻辑 ==========

    def _start_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(1000)  # 1s 间隔

    def _on_tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._progress.setValue(0)
            self._timer.stop()
            # 超时自动 deny
            self.result["decision"] = "deny"
            self.result["feedback"] = "（超时自动拒绝）"
            self.accept()
            return
        self._progress.setValue(self._remaining)
        # 接近超时时变色提醒
        if self._remaining <= 30:
            self._progress.setStyleSheet(
                "QProgressBar::chunk { background-color: #b42318; }"
            )

    def _reset_countdown(self) -> None:
        """重置倒计时（用户有操作时调用）。"""
        self._remaining = self._timeout_seconds
        self._progress.setValue(self._remaining)
        self._progress.setStyleSheet("")  # 恢复默认色

    def keyPressEvent(self, event) -> None:
        """任意按键重置倒计时（兜底，覆盖未来其他可输入控件）。"""
        self._reset_countdown()
        super().keyPressEvent(event)

    # ========== 决策提交 ==========

    def _finish(self, decision: str) -> None:
        self._timer.stop()
        self.result["decision"] = decision
        self.result["feedback"] = self._feedback.toPlainText().strip()
        self.accept()


if __name__ == "__main__":
    raise SystemExit(main())
