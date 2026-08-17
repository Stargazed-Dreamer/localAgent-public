"""心跳上报定时器。

QTimer 10s 间隔 POST /approvals/heartbeat。
失败时通过信号通知 panel 更新状态栏。
"""
from __future__ import annotations

import requests
from PySide6.QtCore import QObject, QTimer, Signal

SERVER_URL = "http://127.0.0.1:8766"
HEARTBEAT_INTERVAL_MS = 10_000  # 10s
HEARTBEAT_TIMEOUT_S = 3.0
PANEL_VERSION = "0.1.0"


class HeartbeatWorker(QObject):
    """心跳上报 worker。

    信号：
        heartbeat_ok()         — 心跳成功
        heartbeat_fail(str)    — 心跳失败（server 不可达等），携带错误信息
    """

    heartbeat_ok = Signal()
    heartbeat_fail = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_INTERVAL_MS)
        self._timer.timeout.connect(self._send)
        self._consecutive_failures = 0

    def start(self) -> None:
        """启动心跳定时器，立即发一次。"""
        self._send()  # 立即发一次，让 server 尽快感知在线
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _send(self) -> None:
        try:
            resp = requests.post(
                f"{SERVER_URL}/approvals/heartbeat",
                json={"panel_version": PANEL_VERSION},
                timeout=HEARTBEAT_TIMEOUT_S,
            )
            if resp.status_code == 200:
                self._consecutive_failures = 0
                self.heartbeat_ok.emit()
            else:
                self._consecutive_failures += 1
                self.heartbeat_fail.emit(f"HTTP {resp.status_code}")
        except requests.RequestException as e:
            self._consecutive_failures += 1
            self.heartbeat_fail.emit(str(e))
