"""会话事件总线（GUI / health 订阅状态变更）。

事件类型：
- grant: 授权成功
- release: 手动撤销 / 紧急停止
- transition: watchdog 到期降级到 normal
- tick: 每秒倒计时刷新（GUI QTimer 订阅）
- warning: normal 模式进入告警阶段（10 分钟到）
- expired: normal 30 分钟降级到无权限
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SessionEvent:
    """会话状态变更事件。"""

    type: str  # grant / release / transition / tick / warning / expired
    state: dict  # SessionManager.status() 快照
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """同步事件总线（RLock 保护，callback 同步调用）。

    不引入 asyncio，保持简单。callback 数量少（GUI + health），同步调用可靠。
    callback 内部不应阻塞（如 GUI IPC 推送应非阻塞）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[SessionEvent], None]] = []

    def subscribe(self, callback: Callable[[SessionEvent], None]) -> Callable[[], None]:
        """订阅事件，返回取消订阅函数。"""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: SessionEvent) -> None:
        """同步发布事件到所有订阅者。

        一个 callback 异常不影响其他 callback（fail-fast 会丢失后续通知）。
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # 订阅者异常不阻塞发布，但记录到 logging
                import logging

                logging.getLogger("localagent.session.events").exception(
                    "Session event subscriber failed: type=%s", event.type
                )
