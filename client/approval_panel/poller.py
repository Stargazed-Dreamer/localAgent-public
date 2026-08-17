"""轮询 pending 列表。

QTimer 2s 间隔 GET /approvals/pending。
结果通过信号发射给 panel（Ticket 04 只打印日志，Ticket 05 连接卡片渲染）。
Ticket 06：新增 new_arrivals 信号，发射新增的 approval_id 列表（用于托盘提醒）。
"""
from __future__ import annotations

import requests
from PySide6.QtCore import QObject, QTimer, Signal

SERVER_URL = "http://127.0.0.1:8766"
POLL_INTERVAL_MS = 2_000  # 2s
POLL_TIMEOUT_S = 3.0


class Poller(QObject):
    """pending 列表轮询 worker。

    信号：
        pending_updated(list)   — pending 列表更新（list[dict]）
        new_arrivals(list)      — 新增的 approval_id 列表（list[str]），用于触发托盘提醒
        poll_fail(str)          — 轮询失败
    """

    pending_updated = Signal(list)
    new_arrivals = Signal(list)
    poll_fail = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._last_ids: set[str] = set()
        self._first_poll = True  # 首次轮询不触发提醒（启动时已有 pending 不算"新"）

    def start(self) -> None:
        """启动轮询定时器，立即查一次。"""
        self._poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        try:
            resp = requests.get(
                f"{SERVER_URL}/approvals/pending",
                timeout=POLL_TIMEOUT_S,
            )
            if resp.status_code != 200:
                self.poll_fail.emit(f"HTTP {resp.status_code}")
                return
            data = resp.json()
            pending = data.get("pending", [])
            self.pending_updated.emit(pending)
            # 检测新增
            current_ids = {
                item["approval_id"] for item in pending if "approval_id" in item
            }
            if not self._first_poll:
                new_ids = list(current_ids - self._last_ids)
                if new_ids:
                    self.new_arrivals.emit(new_ids)
            self._last_ids = current_ids
            self._first_poll = False
        except requests.RequestException as e:
            self.poll_fail.emit(str(e))
