"""后台服务管理

周期性轮询后端 /health、Fake Proxy /api/stats、/todos/due，通过信号广播状态变化。
HTTP 调用在后台 daemon 线程中执行，通过 Qt 信号自动排队回主线程，不阻塞 UI。

信号 diff 检查：backend_health_changed / fake_proxy_status_changed 只在关键字段
变化时 emit（排除 uptime_seconds 等每次轮询都变的字段），避免 UI 每 5s/10s 重建。
"""

import hashlib
import json
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from client.core.constants import (
    BACKEND_FAIL_THRESHOLD,
    BACKEND_HEALTH_TIMEOUT_S,
    BACKEND_POLL_INTERVAL_MS,
    BACKEND_RETRY_DELAY_MS,
    FAKE_PROXY_POLL_INTERVAL_MS,
    TODOS_POLL_INTERVAL_MS,
)
from client.core.http_client import HttpClient
from client.core.logging_config import get_logger

logger = get_logger("services")


def _health_fingerprint(health: dict) -> str:
    """计算 health dict 的指纹（排除 uptime_seconds 等高频变化字段）。

    用于 diff 检查，避免每次轮询都 emit backend_health_changed。
    """
    if not isinstance(health, dict):
        return ""
    # 深拷贝并移除高频变化字段
    h = dict(health)
    # uptime 每秒变，exec.terminals 可能随时变（但不影响面板显示决策）
    h.pop("uptime_seconds", None)
    # 序列化排序确保稳定
    try:
        return hashlib.sha256(
            json.dumps(h, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        return ""


def _fake_stats_fingerprint(stats: dict | None) -> str:
    """计算 fake proxy stats 的指纹（只看关键字段）。"""
    if not isinstance(stats, dict):
        return ""
    key_fields = {
        "total": stats.get("total", 0),
        "stream": stats.get("stream", 0),
        "non_stream": stats.get("non_stream", 0),
        "recent_count": stats.get("recent_count", 0),
    }
    try:
        return hashlib.sha256(
            json.dumps(key_fields, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    except Exception:
        return ""


class ServiceManager(QObject):
    """后台轮询服务管理器

    HTTP 调用在 daemon 线程中执行，结果通过内部信号回传主线程
    （Qt 自动对跨线程信号使用 QueuedConnection）。不会阻塞 UI 主线程。

    信号：
        backend_status_changed(bool)          — 后端在线状态变化（连续失败达阈值才切 offline）
        backend_degraded(bool)                — 后端降级状态变化（单次失败→True，恢复→False）
        backend_health_changed(dict)         — 完整 /health 响应（仅关键字段变化时）
        fake_proxy_status_changed(dict|None)  — Fake Proxy 统计变化（仅变化时）
        todos_due_changed(list)              — 到期任务列表变化
    """
    # 公开信号
    backend_status_changed = Signal(bool)
    backend_degraded = Signal(bool)
    backend_health_changed = Signal(dict)
    fake_proxy_status_changed = Signal(object)  # dict or None
    todos_due_changed = Signal(list)

    # 内部信号（后台线程 emit，Qt 自动排队到主线程 slot）
    _health_fetched = Signal(object)
    _fake_fetched = Signal(object)
    _todos_fetched = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._backend_online: bool = False
        self._backend_degraded: bool = False       # 单次失败的降级状态
        self._backend_fail_count: int = 0           # 连续失败计数（含重试）
        self._backend_in_flight: bool = False       # /health 请求在途标志（防堆积）
        self._fake_proxy_online: bool = False
        self._last_health: dict | None = None
        self._last_fake_stats: dict | None = None
        self._last_todos: list | None = None
        self._last_health_fp: str = ""
        self._last_fake_fp: str = ""

        self._backend_timer = QTimer(self)
        self._backend_timer.timeout.connect(self._poll_backend)
        self._backend_timer.setInterval(BACKEND_POLL_INTERVAL_MS)

        self._fake_timer = QTimer(self)
        self._fake_timer.timeout.connect(self._poll_fake_proxy)
        self._fake_timer.setInterval(FAKE_PROXY_POLL_INTERVAL_MS)

        self._todos_timer = QTimer(self)
        self._todos_timer.timeout.connect(self._poll_todos)
        self._todos_timer.setInterval(TODOS_POLL_INTERVAL_MS)

        # 内部信号 → 主线程 slot
        self._health_fetched.connect(self._on_backend_fetched)
        self._fake_fetched.connect(self._on_fake_fetched)
        self._todos_fetched.connect(self._on_todos_fetched)

        logger.info("ServiceManager initialized: backend=%dms, fake=%dms, todos=%dms",
                    BACKEND_POLL_INTERVAL_MS, FAKE_PROXY_POLL_INTERVAL_MS, TODOS_POLL_INTERVAL_MS)

    def start(self) -> None:
        """启动所有轮询"""
        logger.info("starting all polls")
        self._poll_backend()
        self._poll_fake_proxy()
        self._poll_todos()
        self._backend_timer.start()
        self._fake_timer.start()
        self._todos_timer.start()

    def stop(self) -> None:
        logger.info("stopping all polls")
        self._backend_timer.stop()
        self._fake_timer.stop()
        self._todos_timer.stop()

    def force_refresh(self) -> None:
        """立即强制刷新所有状态"""
        logger.debug("force_refresh all")
        self._poll_backend()
        self._poll_fake_proxy()
        self._poll_todos()

    def force_refresh_fake(self) -> None:
        self._poll_fake_proxy()

    def force_refresh_todos(self) -> None:
        self._poll_todos()

    @property
    def backend_online(self) -> bool:
        return self._backend_online

    @property
    def fake_proxy_online(self) -> bool:
        return self._fake_proxy_online

    @property
    def last_health(self) -> dict | None:
        return self._last_health

    @property
    def last_fake_stats(self) -> dict | None:
        return self._last_fake_stats

    @property
    def last_todos(self) -> list | None:
        return self._last_todos

    # —— 内部轮询（HTTP 在后台线程执行）——

    def _poll_backend(self) -> None:
        if self._backend_in_flight:
            logger.debug("poll_backend: skipped (in-flight)")
            return
        self._backend_in_flight = True

        def fetch():
            t0 = time.perf_counter()
            health = None
            try:
                health = self._http.get("/health", timeout=BACKEND_HEALTH_TIMEOUT_S)
            except Exception as e:
                logger.exception("poll_backend: unexpected exception: %s", e)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug("poll_backend: elapsed=%.0fms, ok=%s", elapsed_ms, health is not None)
            self._health_fetched.emit(health)
        threading.Thread(target=fetch, daemon=True).start()

    def _poll_fake_proxy(self) -> None:
        def fetch():
            t0 = time.perf_counter()
            stats = None
            try:
                stats = self._http.fake_proxy_stats()
            except Exception as e:
                logger.exception("poll_fake_proxy: unexpected exception: %s", e)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug("poll_fake_proxy: elapsed=%.0fms, ok=%s", elapsed_ms, stats is not None)
            self._fake_fetched.emit(stats)
        threading.Thread(target=fetch, daemon=True).start()

    def _poll_todos(self) -> None:
        if not self._backend_online:
            logger.debug("poll_todos: skipped (backend offline)")
            return

        def fetch():
            t0 = time.perf_counter()
            todos = None
            try:
                todos = self._http.get("/todos/due")
            except Exception as e:
                logger.exception("poll_todos: unexpected exception: %s", e)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug("poll_todos: elapsed=%.0fms, ok=%s", elapsed_ms, todos is not None)
            self._todos_fetched.emit(todos)
        threading.Thread(target=fetch, daemon=True).start()

    # —— 以下 slot 在主线程执行 ——

    @Slot(object)
    def _on_backend_fetched(self, health) -> None:
        self._backend_in_flight = False
        online = health is not None and health.get("status") == "ok"

        if online:
            # 成功：重置失败计数，清除降级，恢复在线
            self._backend_fail_count = 0
            if self._backend_degraded:
                self._backend_degraded = False
                logger.info("backend degraded cleared")
                self.backend_degraded.emit(False)
            if not self._backend_online:
                self._backend_online = True
                logger.info("backend status changed: online=True")
                self.backend_status_changed.emit(True)
                self._last_todos = None
                self._poll_todos()
            # diff 检查：只在关键字段变化时 emit（排除 uptime_seconds）
            fp = _health_fingerprint(health)
            if fp != self._last_health_fp:
                self._last_health = health
                self._last_health_fp = fp
                logger.debug("backend_health_changed: fingerprint=%s (emit)", fp[:8])
                self.backend_health_changed.emit(health)
            else:
                logger.debug("backend_health_changed: fingerprint unchanged (skip)")
        else:
            # 失败：累加失败计数，按阈值决定降级/离线
            self._backend_fail_count += 1
            logger.warning("backend health failed: fail_count=%d/%d",
                          self._backend_fail_count, BACKEND_FAIL_THRESHOLD)
            if self._backend_fail_count >= BACKEND_FAIL_THRESHOLD:
                # 连续失败达阈值 → 切离线
                if self._backend_degraded:
                    self._backend_degraded = False
                    self.backend_degraded.emit(False)
                if self._backend_online:
                    self._backend_online = False
                    logger.info("backend status changed: online=False (fail_count=%d)",
                               self._backend_fail_count)
                    self.backend_status_changed.emit(False)
                # 离线后不重试，等下一个轮询周期
            elif self._backend_online:
                # 单次失败（在线状态下）→ 降级 + 快速重试
                if not self._backend_degraded:
                    self._backend_degraded = True
                    logger.info("backend degraded: single failure, retrying")
                    self.backend_degraded.emit(True)
                QTimer.singleShot(BACKEND_RETRY_DELAY_MS, self._poll_backend)

    @Slot(object)
    def _on_fake_fetched(self, stats) -> None:
        online = stats is not None
        if online != self._fake_proxy_online:
            self._fake_proxy_online = online
            logger.info("fake_proxy status changed: online=%s", online)
        self._last_fake_stats = stats
        # diff 检查：只在 stats 关键字段变化时 emit
        fp = _fake_stats_fingerprint(stats)
        if fp != self._last_fake_fp:
            self._last_fake_fp = fp
            logger.debug("fake_proxy_status_changed: fingerprint=%s (emit)", fp[:8])
            self.fake_proxy_status_changed.emit(stats)
        else:
            logger.debug("fake_proxy_status_changed: fingerprint unchanged (skip)")

    @Slot(object)
    def _on_todos_fetched(self, todos) -> None:
        if todos is None:
            return
        # /todos/due 返回 {"due": [...], "total": N}；兼容旧格式 {"todos": [...]} / {"items": [...]}
        todo_list = todos.get("due") or todos.get("todos") or todos.get("items") or []
        if todo_list != self._last_todos:
            self._last_todos = todo_list
            logger.info("todos_due_changed: count=%d (emit)", len(todo_list))
            self.todos_due_changed.emit(todo_list)
