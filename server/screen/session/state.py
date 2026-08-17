"""会话状态数据模型（内存，不持久化）。"""

from __future__ import annotations

from dataclasses import dataclass

from server.screen.session.modes import Mode


@dataclass(frozen=True)
class SessionState:
    """一次授权会话的不可变状态快照。

    替代旧 ControlGrant，新增字段：
    - idle_warning_seconds: normal 模式正常阶段时长（浅蓝）
    - idle_grace_seconds: normal 模式告警阶段时长（黄色）
    - shutdown_permitted: watchdog 模式下是否允许关机
    """

    mode: Mode
    task_description: str
    source: str
    granted_at: float
    last_activity_at: float
    # normal 模式时间策略
    idle_warning_seconds: int
    idle_grace_seconds: int
    # watchdog 模式硬上限（normal 时为 None）
    max_duration_seconds: float | None
    # watchdog 模式关机权限（normal 时为 False）
    shutdown_permitted: bool

    @property
    def idle_total_seconds(self) -> int:
        """normal 模式总 idle 时长 = warning + grace。"""
        return self.idle_warning_seconds + self.idle_grace_seconds
