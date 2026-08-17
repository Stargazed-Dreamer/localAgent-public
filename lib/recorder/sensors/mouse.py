"""L0 M1 鼠标监听器 - MouseSensor

设计（spec M1 / 03-input-detection.md 第四节）：
- pynput.mouse.Listener 全局钩子监听鼠标事件（click / scroll / move）
- 事件类型：
  - mouse_click：单击/双击/右键，payload 含 button + clicks + 坐标
  - mouse_scroll：滚轮，payload 含 dx + dy + 坐标
  - mouse_drag：拖拽，payload 含 start + end + 轨迹点
- click 事件触发 ScreenCaptureSensor.trigger_burst（通过 on_click_callback 注入，避免直接依赖）
- 拖拽检测：press 时记录起点 + 开始跟踪 move；release 时若位移超过阈值则发 drag 事件，否则发 click 事件

线程模型：
- pynput Listener 内部起 daemon 线程，回调 _on_click / _on_scroll / _on_move
- _lock 保护拖拽状态（_drag_button / _drag_start / _drag_track）
- stop() 停 listener + 清理状态

注入点（测试 seam）：
- listener_factory: 返回带 start()/stop() 的对象（默认用 pynput.mouse.Listener）
- on_click_callback: click 事件时回调（默认 None，由集成层注入 ScreenCaptureSensor.trigger_burst）
"""

import threading
from collections.abc import Callable
from typing import Any

# 拖拽检测阈值（像素）——位移小于此值视为单击，大于等于视为拖拽
DEFAULT_DRAG_THRESHOLD = 5.0


def _normalize_button(button: Any) -> str:
    """把 pynput Button 枚举归一化为字符串。

    Button.left → "left"
    Button.right → "right"
    Button.middle → "middle"
    其它 → str(button)
    """
    name = getattr(button, "name", None)
    if name is not None:
        return name
    return str(button)


class MouseSensor:
    """M1 鼠标监听器。

    Args:
        controller: RecordingController 引用
        drag_threshold: 拖拽检测阈值（像素），默认 5.0
        listener_factory: 返回带 start()/stop() 的 listener 对象。
                          None 时默认用 pynput.mouse.Listener。
                          测试可注入 fake listener 直接调 _on_click / _on_scroll / _on_move。
        on_click_callback: click 事件时回调（无参数），用于触发 ScreenCaptureSensor.trigger_burst。
                           None 时不触发 burst。测试可注入 fake 验证调用。
    """

    def __init__(
        self,
        controller,
        drag_threshold: float = DEFAULT_DRAG_THRESHOLD,
        listener_factory: Callable[[], Any] | None = None,
        on_click_callback: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.drag_threshold = float(drag_threshold)
        self._listener_factory = listener_factory
        self._on_click_callback = on_click_callback
        # 状态
        self._listener: Any | None = None
        self._lock = threading.Lock()
        # 拖拽跟踪
        self._drag_button: str | None = None  # None=未按下，"left"/"right"/"middle"=按下中
        self._drag_start: tuple[int, int] = (0, 0)
        self._drag_track: list[tuple[int, int]] = []
        self._drag_click_count: int = 0  # 同一位置的连续点击次数（双击检测）
        self._started = False

    def start(self) -> None:
        """启动 listener。"""
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("MouseSensor.controller 未注入")
        self._started = True
        if self._listener_factory is not None:
            self._listener = self._listener_factory()
            self._listener.start()

    def stop(self) -> None:
        """停止 listener。幂等。"""
        if not self._started:
            return
        self._started = False
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        with self._lock:
            self._drag_button = None
            self._drag_track = []

    # ========== pynput 回调（listener 调用，测试也可直接调） ==========

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        """鼠标点击回调。

        pynput 在 press 和 release 时都调此方法（pressed=True/False）。
        """
        if not self._started:
            return
        btn = _normalize_button(button)
        if pressed:
            # press：记录起点，开始跟踪
            with self._lock:
                self._drag_button = btn
                self._drag_start = (int(x), int(y))
                self._drag_track = [(int(x), int(y))]
        else:
            # release：判断是 click 还是 drag
            with self._lock:
                start_x, start_y = self._drag_start
                track = list(self._drag_track)
                self._drag_button = None
                self._drag_track = []
            # 计算位移
            dx = x - start_x
            dy = y - start_y
            distance = (dx * dx + dy * dy) ** 0.5
            if distance >= self.drag_threshold and len(track) >= 2:
                # 拖拽
                self._emit_drag(btn, (start_x, start_y), (int(x), int(y)), track)
            else:
                # 单击/双击
                self._emit_click(btn, int(x), int(y))
                # 触发 burst（click 后屏幕多帧采样）
                if self._on_click_callback is not None:
                    try:
                        self._on_click_callback()
                    except Exception:
                        pass

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """鼠标滚轮回调。"""
        if not self._started:
            return
        payload = {
            "x": int(x),
            "y": int(y),
            "dx": int(dx),
            "dy": int(dy),
        }
        self.controller.emit_event("mouse_scroll", payload)

    def _on_move(self, x: int, y: int) -> None:
        """鼠标移动回调（仅拖拽时记录轨迹）。"""
        if not self._started:
            return
        with self._lock:
            if self._drag_button is not None:
                self._drag_track.append((int(x), int(y)))

    # ========== 事件发射 ==========

    def _emit_click(self, button: str, x: int, y: int) -> None:
        """发 mouse_click 事件。

        简化版：每次 release 发一个 click 事件，clicks=1。
        双击检测留给 L1 聚合器（需要时间窗口分析连续 click）。
        """
        payload = {
            "button": button,
            "clicks": 1,
            "x": x,
            "y": y,
        }
        self.controller.emit_event("mouse_click", payload)

    def _emit_drag(
        self,
        button: str,
        start: tuple[int, int],
        end: tuple[int, int],
        track: list[tuple[int, int]],
    ) -> None:
        """发 mouse_drag 事件。"""
        payload = {
            "button": button,
            "start": {"x": start[0], "y": start[1]},
            "end": {"x": end[0], "y": end[1]},
            "track": [{"x": p[0], "y": p[1]} for p in track],
        }
        self.controller.emit_event("mouse_drag", payload)
