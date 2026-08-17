"""ConfirmDialog — 危险操作确认对话框

三级风险：info（蓝）/ warning（橙）/ danger（红）。
供 Monitoring 面板"重启后端"、Settings 面板编辑危险字段、Keys 面板删除 key 复用。
"""


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from lib.ui import tokens
from lib.ui.icons import icon_pixmap
from lib.ui.theme import set_kind, set_text_role

_RISK_STYLES = {
    "info": {
        "color": tokens.INFO_TEXT,
        "icon": "info",
        "button_kind": "primary",
    },
    "warning": {
        "color": tokens.WARNING_TEXT,
        "icon": "alert-triangle",
        "button_kind": "primary",
    },
    "danger": {
        "color": tokens.DANGER_TEXT,
        "icon": "alert-triangle",
        "button_kind": "danger",
    },
}


class ConfirmDialog(QDialog):
    """危险操作确认对话框。

    用法：
        if ConfirmDialog.confirm(self, "重启后端", "将发送 POST /shutdown ...", "danger"):
            # 用户确认，执行
    """

    def __init__(self, title: str, message: str,
                 risk_level: str = "warning",
                 detail: str | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self._confirmed = False

        style = _RISK_STYLES.get(risk_level, _RISK_STYLES["warning"])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部色条
        bar = QFrame()
        bar.setFixedHeight(4)
        bar.setStyleSheet(f"background-color: {style['color']};")
        outer.addWidget(bar)

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(12)

        # 标题行：图标 + 标题
        head_row = QHBoxLayout()
        head_row.setSpacing(10)
        icon_label = QLabel()
        icon_label.setPixmap(icon_pixmap(style["icon"], style["color"], 28))
        head_row.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        title_label = QLabel(title)
        set_text_role(title_label, "heading")
        title_label.setWordWrap(True)
        head_row.addWidget(title_label, 1)
        body.addLayout(head_row)

        # 消息正文
        msg_label = QLabel(message)
        set_text_role(msg_label, "secondary")
        msg_label.setWordWrap(True)
        msg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(msg_label)

        # 详情区（可选，折叠式）
        if detail:
            detail_frame = QFrame()
            set_kind(detail_frame, "card")
            detail_layout = QVBoxLayout(detail_frame)
            detail_layout.setContentsMargins(8, 8, 8, 8)
            detail_label = QLabel(detail)
            set_text_role(detail_label, "mono")
            detail_label.setWordWrap(True)
            detail_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextBrowserInteraction
            )
            detail_layout.addWidget(detail_label)
            body.addWidget(detail_frame)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumWidth(80)
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        confirm_text = "确认" if risk_level != "danger" else "确认删除"
        confirm_btn = QPushButton(confirm_text)
        confirm_btn.setMinimumWidth(80)
        set_kind(confirm_btn, style["button_kind"])
        confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(confirm_btn)

        body.addLayout(btn_row)
        outer.addLayout(body, 1)

    def _on_confirm(self) -> None:
        self._confirmed = True
        self.accept()

    def _on_cancel(self) -> None:
        self._confirmed = False
        self.reject()

    @staticmethod
    def confirm(parent, title: str, message: str,
                risk_level: str = "warning",
                detail: str | None = None) -> bool:
        """弹窗确认。返回 True=用户确认 / False=用户取消。"""
        dlg = ConfirmDialog(title, message, risk_level, detail, parent)
        dlg.exec()
        return dlg._confirmed
