"""SessionManager — 会话管理单例（升级自 ControlGrantManager）。

职责：
- 持有当前 SessionState（内存，不持久化）
- grant/release/extend/transition 接口
- 权限检查 require_mode / can_operate / can_shutdown
- worker 线程周期检查过期（normal idle 两阶段 / watchdog max_duration）
- 事件总线发布状态变更

依赖反转：
- 路由层 / health / security 通过本单例访问授权状态
- overlay_client 订阅本单位事件渲染横条
- 不 import overlay_client / core.health（避免循环依赖）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from server.screen.session.events import EventBus, SessionEvent
from server.screen.session.modes import Mode
from server.screen.session.policy import TimePolicy
from server.screen.session.state import SessionState


class SessionManager:
    """会话管理单例，持有一次 process-local 授权。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        policy: TimePolicy | None = None,
        start_worker: bool = True,
    ) -> None:
        self._clock = clock
        self._policy = policy or TimePolicy()
        self._start_worker = start_worker
        self._lock = threading.RLock()
        self._state: SessionState | None = None
        self._stop_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._last_revoked_reason = ""
        self._last_revoked_at = 0.0
        # warning 事件是否已发布（避免重复触发），grant 时重置
        self._warning_published = False
        self._event_bus = EventBus()

    # ========== 事件订阅 ==========

    def subscribe(self, callback: Callable[[SessionEvent], None]) -> Callable[[], None]:
        """订阅会话状态变更事件，返回取消订阅函数。"""
        return self._event_bus.subscribe(callback)

    def _publish(self, event_type: str, state: dict | None = None) -> None:
        """发布事件（在锁外调用，避免 callback 死锁）。"""
        if state is None:
            state = self.status()
        self._event_bus.publish(SessionEvent(type=event_type, state=state))

    # ========== 授权生命周期 ==========

    def grant(
        self,
        *,
        mode: Mode,
        task_description: str,
        source: str = "agent",
        max_duration_hours: int | None = None,
        shutdown_permitted: bool = False,
    ) -> dict:
        """授权进入指定模式。

        Args:
            mode: NORMAL / WATCHDOG
            task_description: 任务描述（≤30 字，超出截断）
            source: 授权来源（默认 "agent"）
            max_duration_hours: watchdog 时长（小时），仅 watchdog 模式有效
            shutdown_permitted: 是否允许关机，仅 watchdog 模式有效

        Returns:
            status() 快照
        """
        now = self._clock()
        # 截断 task_description 到 30 字
        description = (task_description or "").strip() or "Computer Use task"
        if len(description) > 30:
            description = description[:29] + "…"

        # 兼容字符串转 Mode 枚举（外部脚本可能传 "normal"/"watchdog"）
        if isinstance(mode, str):
            mode = Mode(mode)

        # 构造 SessionState
        if mode == Mode.WATCHDOG:
            hours = self._policy.clamp_watchdog_hours(
                max_duration_hours if max_duration_hours is not None
                else self._policy.watchdog_default_hours
            )
            max_duration_seconds = float(hours * 3600)
            shutdown = bool(shutdown_permitted)
        else:
            max_duration_seconds = None
            shutdown = False

        with self._lock:
            self._stop_worker_locked()
            self._state = SessionState(
                mode=mode,
                task_description=description,
                source=(source or "agent").strip(),
                granted_at=now,
                last_activity_at=now,
                idle_warning_seconds=self._policy.idle_warning_seconds,
                idle_grace_seconds=self._policy.idle_grace_seconds,
                max_duration_seconds=max_duration_seconds,
                shutdown_permitted=shutdown,
            )
            self._last_revoked_reason = ""
            self._last_revoked_at = 0.0
            self._warning_published = False
            self._start_worker_locked()
            status = self._status_locked(now)

        # 在锁外发布事件
        self._publish("grant", status)
        return status

    def release(self, reason: str = "manual") -> bool:
        """手动撤销授权（任何模式 → NO_PERMISSION）。"""
        with self._lock:
            if self._state is None:
                return False
            expired_status = self._status_locked(self._clock())
            expired_status["active"] = False
            expired_status["remaining_seconds"] = 0
            expired_status["revoked_reason"] = reason
            expired_status["revoked_at"] = self._clock()
            self._state = None
            self._last_revoked_reason = reason
            self._last_revoked_at = self._clock()
            worker = self._stop_worker_locked()

        self._join_worker(worker)
        # 在锁外发布事件
        self._publish("release", expired_status)
        return True

    def extend(self) -> bool:
        """重置 idle 计时（任何端点调用都重置）。

        watchdog 模式下不重置 max_duration，只更新 last_activity_at（用于 idle 检查兼容）。
        """
        # 先检查是否过期（避免延长已过期的授权）
        if self._check_expiry():
            return False
        now = self._clock()
        with self._lock:
            if self._state is None:
                return False
            state = self._state
            self._state = SessionState(
                mode=state.mode,
                task_description=state.task_description,
                source=state.source,
                granted_at=state.granted_at,
                last_activity_at=now,
                idle_warning_seconds=state.idle_warning_seconds,
                idle_grace_seconds=state.idle_grace_seconds,
                max_duration_seconds=state.max_duration_seconds,
                shutdown_permitted=state.shutdown_permitted,
            )
            return True

    def transition(self, new_mode: Mode) -> bool:
        """模式转换（仅 watchdog → normal 支持，idle 重置为 0）。

        其他转换通过 grant / release 完成。
        """
        now = self._clock()
        with self._lock:
            if self._state is None:
                return False
            if self._state.mode != Mode.WATCHDOG or new_mode != Mode.NORMAL:
                return False
            # watchdog → normal：保留 task_description，重置 idle
            self._state = SessionState(
                mode=Mode.NORMAL,
                task_description=self._state.task_description,
                source=self._state.source,
                granted_at=now,  # 重置 granted_at（normal 不用此字段算 remaining）
                last_activity_at=now,
                idle_warning_seconds=self._policy.idle_warning_seconds,
                idle_grace_seconds=self._policy.idle_grace_seconds,
                max_duration_seconds=None,
                shutdown_permitted=False,
            )
            self._warning_published = False
            status = self._status_locked(now)

        # 在锁外发布事件
        self._publish("transition", status)
        return True

    # ========== 权限检查 ==========

    def require_mode(self, required: Mode) -> bool:
        """检查是否满足所需权限级别。

        NO_PERMISSION: 任何模式都满足（无要求）
        NORMAL: mode == NORMAL 或 WATCHDOG
        WATCHDOG: mode == WATCHDOG
        """
        if self._check_expiry():
            return False
        with self._lock:
            if self._state is None:
                return required == Mode.NO_PERMISSION
            current = self._state.mode
            if required == Mode.NO_PERMISSION:
                return True
            if required == Mode.NORMAL:
                return current in (Mode.NORMAL, Mode.WATCHDOG)
            if required == Mode.WATCHDOG:
                return current == Mode.WATCHDOG
            return False

    def can_operate(self) -> bool:
        """副作用端点检查：mode != NO_PERMISSION。"""
        return self.require_mode(Mode.NORMAL)

    def can_shutdown(self) -> bool:
        """关机权限检查：mode == WATCHDOG 且 shutdown_permitted。"""
        if self._check_expiry():
            return False
        with self._lock:
            return (
                self._state is not None
                and self._state.mode == Mode.WATCHDOG
                and self._state.shutdown_permitted
            )

    def is_active(self) -> bool:
        """当前是否有授权（任何模式）。"""
        if self._check_expiry():
            return False
        with self._lock:
            return self._state is not None

    # ========== 过期检查（两阶段降级） ==========

    def _check_expiry(self) -> bool:
        """检查并触发过期（warning 事件 / expired / transition）。返回是否已过期。"""
        # watchdog 到期降级到 normal：降级本身就是过期事件，返回 True
        # 不再继续检查 normal idle（降级时 last_activity_at 已重置为 now，正常不会立即过期）
        if self._expire_if_max_duration():
            return True
        return self._expire_if_idle()

    def _expire_if_idle(self) -> bool:
        """normal 模式两阶段降级。

        - 0 ~ idle_warning_seconds: phase=normal，不触发事件
        - idle_warning_seconds ~ idle_total_seconds: phase=warning，触发 warning 事件（仅一次）
        - idle_total_seconds: 降级到 NO_PERMISSION，触发 expired 事件
        """
        publish_warning = False
        expired_status: dict | None = None
        with self._lock:
            if self._state is None:
                return False
            # watchdog 模式不检查 idle
            if self._state.mode == Mode.WATCHDOG:
                return False
            now = self._clock()
            idle_elapsed = now - self._state.last_activity_at
            # 阶段 2 检查：总 idle 超时 → 降级
            if idle_elapsed >= self._state.idle_total_seconds:
                expired_status = self._status_locked(now)
                expired_status["active"] = False
                expired_status["remaining_seconds"] = 0
                expired_status["revoked_reason"] = "idle_timeout"
                expired_status["revoked_at"] = now
                self._state = None
                self._last_revoked_reason = "idle_timeout"
                self._last_revoked_at = now
                worker = self._stop_worker_locked()
            # 阶段 1 检查：warning 阶段开始 → 触发 warning 事件（仅一次）
            elif idle_elapsed >= self._state.idle_warning_seconds and not self._warning_published:
                self._warning_published = True
                publish_warning = True

        if expired_status is not None:
            self._join_worker(worker)  # type: ignore[name-defined]  # 仅在 expired 分支有 worker
            self._publish("expired", expired_status)
            return True

        if publish_warning:
            self._publish("warning")
        return False

    def _expire_if_max_duration(self) -> bool:
        """watchdog 模式硬上限检查。到期降级到 NORMAL（而非撤销）。"""
        with self._lock:
            if self._state is None:
                return False
            if self._state.mode != Mode.WATCHDOG:
                return False
            if self._state.max_duration_seconds is None:
                return False
            now = self._clock()
            elapsed = now - self._state.granted_at
            if elapsed < self._state.max_duration_seconds:
                return False

        # 到期 → 降级到 normal（transition 内部会 publish 事件）
        return self.transition(Mode.NORMAL)

    # ========== 状态查询 ==========

    def status(self) -> dict:
        """查询当前会话状态（兼容旧 ControlGrantManager.status 字段 + 新字段）。"""
        # 先检查过期
        self._check_expiry()
        with self._lock:
            return self._status_locked(self._clock())

    def _status_locked(self, now: float) -> dict:
        if self._state is None:
            return {
                "active": False,
                "mode": Mode.NO_PERMISSION.value,
                "task_description": "",
                "source": "",
                "granted_at": 0.0,
                "last_activity_at": 0.0,
                # 旧字段兼容
                "idle_timeout_seconds": 0,
                "max_duration_seconds": None,
                "expires_at": 0.0,
                "remaining_seconds": 0,
                "revoked_reason": self._last_revoked_reason,
                "revoked_at": self._last_revoked_at,
                # 新字段
                "phase": "inactive",
                "idle_warning_seconds": self._policy.idle_warning_seconds,
                "idle_grace_seconds": self._policy.idle_grace_seconds,
                "shutdown_permitted": False,
            }

        state = self._state
        if state.mode == Mode.WATCHDOG and state.max_duration_seconds is not None:
            # watchdog 模式：remaining 按硬上限计算
            expires_at = state.granted_at + state.max_duration_seconds
            remaining = max(0, int(expires_at - now))
            phase = "watchdog"
            # 旧字段兼容：idle_timeout_seconds 在 watchdog 下为 0（不因空闲撤销）
            idle_timeout = 0
        else:
            # normal 模式：按 idle 总时长计算 remaining
            idle_elapsed = now - state.last_activity_at
            expires_at = state.last_activity_at + state.idle_total_seconds
            remaining = max(0, int(expires_at - now))
            # phase 区分 normal / warning
            if idle_elapsed >= state.idle_warning_seconds:
                phase = "warning"
                # warning 阶段 remaining 按 grace 剩余计算
                grace_expires = state.last_activity_at + state.idle_total_seconds
                remaining = max(0, int(grace_expires - now))
            else:
                phase = "normal"
            idle_timeout = state.idle_total_seconds

        return {
            "active": True,
            "mode": state.mode.value,
            "task_description": state.task_description,
            "source": state.source,
            "granted_at": state.granted_at,
            "last_activity_at": state.last_activity_at,
            # 旧字段兼容
            "idle_timeout_seconds": idle_timeout,
            "max_duration_seconds": state.max_duration_seconds,
            "expires_at": expires_at,
            "remaining_seconds": remaining,
            "revoked_reason": "",
            "revoked_at": 0.0,
            # 新字段
            "phase": phase,
            "idle_warning_seconds": state.idle_warning_seconds,
            "idle_grace_seconds": state.idle_grace_seconds,
            "shutdown_permitted": state.shutdown_permitted,
        }

    # ========== 生命周期 ==========

    def shutdown(self) -> None:
        """后端关闭时调用，撤销授权 + 停止 worker。"""
        self.release("shutdown")
        with self._lock:
            self._stop_worker_locked()

    # ========== worker 线程（复用 ControlGrantManager 设计） ==========

    def _start_worker_locked(self) -> None:
        if not self._start_worker or self._state is None:
            return
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._worker = threading.Thread(
            target=self._worker_loop,
            args=(stop_event,),
            daemon=True,
            name="computer-use-session-manager",
        )
        self._worker.start()

    def _stop_worker_locked(self) -> threading.Thread | None:
        worker = self._worker
        if self._stop_event is not None:
            self._stop_event.set()
        self._stop_event = None
        self._worker = None
        return worker

    @staticmethod
    def _join_worker(worker: threading.Thread | None) -> None:
        if worker is None or worker is threading.current_thread():
            return
        worker.join(timeout=1.0)

    def _worker_loop(self, stop_event: threading.Event) -> None:
        """周期检查过期（normal idle 两阶段 / watchdog max_duration）。"""
        check_interval = max(0.05, self._policy.check_interval_seconds)
        while not stop_event.wait(check_interval):
            if self._check_expiry():
                return


# ========== 模块级单例 ==========

_session_manager: SessionManager | None = None
_singleton_lock = threading.Lock()


def get_session_manager() -> SessionManager:
    """获取 SessionManager 单例。

    首次调用时从 config 读取 TimePolicy 并启动 worker。
    测试中可通过直接构造 SessionManager(start_worker=False) 绕过单例。
    """
    global _session_manager
    if _session_manager is None:
        with _singleton_lock:
            if _session_manager is None:
                _session_manager = SessionManager(
                    policy=TimePolicy.from_config(),
                    start_worker=True,
                )
    return _session_manager


def reset_session_manager() -> None:
    """重置单例（测试隔离用）。"""
    global _session_manager
    with _singleton_lock:
        if _session_manager is not None:
            _session_manager.shutdown()
        _session_manager = None
