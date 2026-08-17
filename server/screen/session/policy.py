"""会话时间策略（从 config.toml 读取）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimePolicy:
    """会话时间策略。

    所有时间单位：秒（watchdog_default_hours 等运行时换算）。
    """

    # normal 模式正常阶段（浅蓝），默认 10 分钟
    idle_warning_seconds: int = 600
    # normal 模式告警阶段（黄色），默认 20 分钟
    idle_grace_seconds: int = 1200
    # watchdog 默认时长（小时），默认 10
    watchdog_default_hours: int = 10
    # watchdog 最小时长（小时）
    watchdog_min_hours: int = 1
    # watchdog 最大时长（小时）
    watchdog_max_hours: int = 999
    # worker 线程检查间隔（秒）
    check_interval_seconds: float = 5.0

    @property
    def idle_total_seconds(self) -> int:
        """normal 模式总 idle 时长 = warning + grace。"""
        return self.idle_warning_seconds + self.idle_grace_seconds

    @property
    def watchdog_default_seconds(self) -> float:
        """watchdog 默认时长（秒）。"""
        return float(self.watchdog_default_hours * 3600)

    def clamp_watchdog_hours(self, hours: int) -> int:
        """将 watchdog 时长约束到 [min, max] 范围。"""
        return max(self.watchdog_min_hours, min(self.watchdog_max_hours, int(hours)))

    @classmethod
    def from_config(cls) -> TimePolicy:
        """从 config.toml 读取时间策略，缺省时使用默认值。"""
        from server.config import get_screen_config

        cfg = get_screen_config()
        return cls(
            idle_warning_seconds=int(cfg.get("idle_warning_seconds", 600)),
            idle_grace_seconds=int(cfg.get("idle_grace_seconds", 1200)),
            watchdog_default_hours=int(cfg.get("watchdog_default_hours", 10)),
            watchdog_min_hours=int(cfg.get("watchdog_min_hours", 1)),
            watchdog_max_hours=int(cfg.get("watchdog_max_hours", 999)),
            check_interval_seconds=float(cfg.get("session_check_interval_seconds", 5.0)),
        )
