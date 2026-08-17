"""审批面板主窗口。

Ticket 04：骨架（标题 + 状态栏 + 空卡片容器）
Ticket 05：卡片渲染（poller pending → diff → 新增/移除卡片）
Ticket 06：窗口激活信号 + 托盘模式关窗分流
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.approval_panel import settings as panel_settings
from client.approval_panel.card import ApprovalCard
from lib.ui import tokens as T


class ApprovalPanel(QMainWindow):
    """审批面板主窗口。

    布局：
    - 顶部状态标签（在线/离线 + pending 计数）
    - 中间滚动区域（卡片容器，Ticket 04 为空）
    - 底部状态栏

    信号：
        window_activated() — 窗口被用户激活（用于停止托盘闪烁）
    """

    window_activated = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("LocalAgent 审批面板")
        self.setMinimumSize(480, 320)
        # 不抢焦点（不在启动时激活窗口，任务栏出现即可）
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)

        self._build_ui()
        self._restore_geometry()

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        # 状态标签
        self._status_label = QLabel("正在连接 server...")
        self._status_label.setStyleSheet("padding: 8px; font-size: 14px;")
        layout.addWidget(self._status_label)

        # 卡片滚动区域（Ticket 04 为空，Ticket 05 填充）
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._card_container = QWidget()
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.addStretch()  # 卡片从上往下排，底部弹簧
        self._scroll.setWidget(self._card_container)
        layout.addWidget(self._scroll, stretch=1)

        # 卡片索引：approval_id → ApprovalCard
        self._cards: dict[str, ApprovalCard] = {}

        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _restore_geometry(self) -> None:
        geo = panel_settings.get_window_geometry()
        if geo is not None:
            self.restoreGeometry(geo)

    # ========== 事件 ==========

    def changeEvent(self, event: QEvent) -> None:
        """窗口激活时发信号（用于停止托盘闪烁 + 取消任务栏高亮）。"""
        if event.type() == QEvent.Type.WindowActivate:
            self.window_activated.emit()
        super().changeEvent(event)

    # ========== 信号回调 ==========

    def on_heartbeat_ok(self) -> None:
        self._status_label.setText("● 在线")
        self._status_label.setStyleSheet(f"padding: 8px; font-size: 14px; color: {T.SUCCESS};")
        self.statusBar().showMessage("已连接 server")

    def on_heartbeat_fail(self, error: str) -> None:
        self._status_label.setText(f"● 离线 — {error}")
        self._status_label.setStyleSheet(f"padding: 8px; font-size: 14px; color: {T.DANGER};")
        self.statusBar().showMessage(f"server 不可达：{error}")

    def on_pending_updated(self, pending: list) -> None:
        """pending 列表更新：diff 现有卡片，新增/移除/更新。"""
        if not isinstance(pending, list):
            return

        current_ids = set(self._cards.keys())
        new_ids = {item["approval_id"] for item in pending if "approval_id" in item}

        # 移除不再 pending 的卡片（已决策/已超时被 server 移除）
        for aid in current_ids - new_ids:
            self._remove_card(aid)

        # 新增或更新卡片
        for item in pending:
            aid = item.get("approval_id")
            if not aid:
                continue
            if aid in self._cards:
                # 更新现有卡片（seconds_left 变化）
                self._cards[aid].update_from_pending(item)
            else:
                # 新增卡片（插入到顶部，在 stretch 之前）
                self._add_card(item)

        count = len(pending)
        self.statusBar().showMessage(f"待审批：{count} 项")

    def _add_card(self, item: dict) -> None:
        """新增一张审批卡片，插入到布局顶部（stretch 之前）。"""
        card = ApprovalCard(item, parent=self._card_container)
        card.card_closed.connect(self._remove_card)
        card.activity_occurred.connect(self._on_card_activity)
        # 插入到 stretch（最后一个 item）之前
        self._card_layout.insertWidget(self._card_layout.count() - 1, card)
        self._cards[card.approval_id] = card

    def _remove_card(self, approval_id: str) -> None:
        """移除一张卡片。"""
        card = self._cards.pop(approval_id, None)
        if card is not None:
            self._card_layout.removeWidget(card)
            card.deleteLater()

    def _on_card_activity(self, approval_id: str) -> None:
        """卡片上报用户操作（打字/按键）→ POST /approvals/{id}/activity。"""
        # 异步上报，不阻塞 UI（简单实现：直接 requests，timeout 短）
        import threading

        def _post():
            try:
                import requests as req

                req.post(
                    f"http://127.0.0.1:8766/approvals/{approval_id}/activity",
                    timeout=3.0,
                )
            except req.RequestException:
                pass

        threading.Thread(target=_post, daemon=True).start()

    def on_poll_fail(self, error: str) -> None:
        self.statusBar().showMessage(f"轮询失败：{error}")

    # ========== 关窗处理 ==========

    def closeEvent(self, event) -> None:
        """关窗分流：
        - show_in_tray=True：保存 geometry → hide() → ignore 事件（最小化到托盘）
        - show_in_tray=False：保存 geometry → 正常关闭
        """
        panel_settings.save_window_geometry(self.saveGeometry())
        if panel_settings.get_show_in_tray():
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)
