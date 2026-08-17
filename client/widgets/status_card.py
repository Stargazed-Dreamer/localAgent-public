"""StatusCard — 服务状态卡片组件"""


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)

from lib.ui import tokens
from lib.ui.theme import set_kind, set_status, set_text_role


class StatusCard(QFrame):
    """状态卡片：标题 + 状态指示灯 + 副标题 + 详情区

    status 取值：
        "online"   — 绿
        "offline"  — 红
        "warning"  — 黄（后端在线但无可用资源）
        "unknown"  — 灰（默认）
    """

    STATUS_COLORS = {
        "online": tokens.SUCCESS_TEXT,
        "offline": tokens.DANGER_TEXT,
        "warning": tokens.WARNING_TEXT,
        "unknown": tokens.TEXT_TERTIARY,
    }
    STATUS_LABELS = {
        "online": "在线",
        "offline": "离线",
        "warning": "警告",
        "unknown": "未知",
    }

    def __init__(self, title: str, subtitle: str = "",
                 status: str = "unknown", details: dict | None = None,
                 parent=None):
        super().__init__(parent)
        set_kind(self, "card")
        self.setObjectName("statusCard")
        self.setMinimumWidth(220)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._title = title
        self._subtitle = subtitle
        self._status = status
        self._details = details or {}

        self._build_ui()
        self._apply_status()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        # 标题行
        title_label = QLabel(self._title)
        set_text_role(title_label, "title")
        layout.addWidget(title_label)

        # 状态行：圆点 + 状态文字 + 副标题
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setFixedWidth(18)
        status_row.addWidget(self._dot)

        self._status_label = QLabel(self.STATUS_LABELS.get(self._status, "未知"))
        set_text_role(self._status_label, "title")
        status_row.addWidget(self._status_label)

        status_row.addStretch()
        if self._subtitle:
            sub_label = QLabel(self._subtitle)
            set_text_role(sub_label, "tertiary")
            status_row.addWidget(sub_label)
        layout.addLayout(status_row)

        # 详情区
        self._form = QFormLayout()
        self._form.setSpacing(2)
        self._form.setContentsMargins(0, 4, 0, 0)
        self._details_widget = QLabel("")
        set_text_role(self._details_widget, "tertiary")
        self._details_widget.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._details_widget.setWordWrap(True)
        layout.addWidget(self._details_widget)

        self._refresh_details()

    def _apply_status(self) -> None:
        set_status(self._dot, self._status)
        self._status_label.setText(self.STATUS_LABELS.get(self._status, "未知"))
        set_status(self._status_label, self._status)

    def _refresh_details(self) -> None:
        if not self._details:
            self._details_widget.setText("")
            return
        lines = []
        for k, v in self._details.items():
            lines.append(f"{k}: {v}")
        self._details_widget.setText("\n".join(lines))

    # —— 公共 API ——

    def set_status(self, online: bool) -> None:
        self._status = "online" if online else "offline"
        self._apply_status()

    def set_status_value(self, status: str) -> None:
        """直接设置状态值（online/offline/unknown）"""
        if status in self.STATUS_COLORS:
            self._status = status
            self._apply_status()

    def set_details(self, details: dict) -> None:
        self._details = details or {}
        self._refresh_details()

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle = subtitle
        # 重建 UI（简化处理）
        clear_layout(self.layout())
        self._build_ui()
        self._apply_status()


def clear_layout(layout) -> None:
    """递归清空 layout 内所有 widget"""
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        else:
            sub = item.layout()
            if sub is not None:
                clear_layout(sub)
