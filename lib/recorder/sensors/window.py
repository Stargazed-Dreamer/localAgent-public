"""L0 M4 窗口焦点追踪器：pywin32 轮询 GetForegroundWindow

设计（D025）：
- 用 pywin32 轮询 GetForegroundWindow（详细 300ms / 粗略 1s）
- 窗口句柄变化时记录 focus_change 事件（窗口标题 + 进程名 + 句柄）
- focus_change 事件同时写 events.jsonl 和 focus.jsonl（focus.jsonl 便于 L4 快速定位章节）
- 轮询线程：daemon 线程，stop_event 控制

线程模型：
- start() 起一个 daemon 线程循环轮询
- 每次轮询取 GetForegroundWindow，与 _last_hwnd 对比，不同则发事件
- stop() 设置 stop_event + join 线程
"""

import threading
from collections.abc import Callable

import win32gui
import win32process

# 轮询间隔默认值（秒）
DEFAULT_POLL_INTERVAL_DETAILED = 0.3  # 详细模式 300ms
DEFAULT_POLL_INTERVAL_COARSE = 1.0    # 粗略模式 1s


class WindowFocusSensor:
    """M4 窗口焦点追踪器。

    Args:
        controller: RecordingController 引用
        poll_interval: 轮询间隔（秒），默认 0.3（详细模式）
        on_focus_change_callback: 焦点变化时的回调（无参数），用于通知其它传感器
            （如 KeyboardSensor.notify_focus_change）立即 flush 当前块。
            回调异常 try/except 兜底不崩 sensor。
    """

    def __init__(
        self,
        controller,
        poll_interval: float = DEFAULT_POLL_INTERVAL_DETAILED,
        on_focus_change_callback: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.poll_interval = float(poll_interval)
        self._on_focus_change_callback = on_focus_change_callback
        self._thread = None
        self._stop_event = threading.Event()
        self._last_hwnd: int | None = None
        self._started = False

    def start(self) -> None:
        """启动轮询线程。"""
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("WindowFocusSensor.controller 未注入")
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """轮询循环：取前台窗口 hwnd，变化时发事件。"""
        while not self._stop_event.is_set():
            try:
                hwnd = win32gui.GetForegroundWindow()
            except Exception:
                hwnd = 0
            if hwnd != self._last_hwnd:
                self._emit_focus_change(hwnd)
                self._last_hwnd = hwnd
            # 用 wait 代替 sleep，便于响应 stop_event
            self._stop_event.wait(self.poll_interval)

    def _emit_focus_change(self, hwnd: int) -> None:
        """发 focus_change 事件到 controller（同时写 events.jsonl 和 focus.jsonl）。"""
        title = ""
        pid = 0
        if hwnd:
            try:
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                title = ""
            try:
                # GetWindowThreadProcessId 返回 (thread_id, process_id)
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
        payload = {
            "hwnd": int(hwnd),
            "title": title,
            "pid": int(pid),
        }
        self.controller.emit_focus_change(payload)
        # 通知其它传感器（如 KeyboardSensor）焦点已变化，立即 flush 当前块
        if self._on_focus_change_callback is not None:
            try:
                self._on_focus_change_callback()
            except Exception:
                pass

    def stop(self) -> None:
        """停止轮询线程。幂等。"""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
