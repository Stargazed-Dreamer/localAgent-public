"""L0 采集层 Ticket 06：录制器应用协调类（RecorderApp）

设计：
- 协调 RecordingController + 5 个传感器（M1 键鼠 / M2 屏幕 / M3 音频 / M4 焦点 / M5 剪贴板）
- 接收 RecordingConfig，按配置实例化传感器（mode/范围/参数）
- 30 分钟自动停止（threading.Timer，不依赖 Qt 事件循环）
- 接近上限时通过 callback 通知 GUI（默认 max-120s 时触发）
- 暴露 elapsed_seconds / current_event_count / current_frame_count 供 GUI 实时显示

设计偏离说明：
- 05-gui-design.md 设计稿含"暂停"按钮，本 ticket 不实现暂停
  （spec user stories 未强制要求；AudioSensor/ScreenCaptureSensor 暂停状态保存复杂度高）
- 暂停功能留待未来 ticket 实现，本 ticket 只实现开始/停止

sensor_factory 注入：
- 默认 _default_sensor_factory 实例化真实 M1-M5 传感器（pynput/mss/sounddevice/pywin32/win32clipboard）
- 测试可注入 fake sensor_factory 返回 MockSensor 列表，避免真实系统 API
- 与 ConfigDialog 类似的注入模式

线程模型：
- start() 在主线程实例化 controller + 传感器 + 启动（传感器内部起 daemon 线程）
- 30 分钟自动停止用 threading.Timer（daemon）
- 接近上限提示用 threading.Timer（daemon）
- stop() 在主线程取消计时器 + 停 controller（停所有传感器 + 写 meta.json）

截图隐藏 FloatingBar（hide-on-capture 修复）：
- capture_overlay_callbacks: (hide_fn, show_fn) 元组，截图前后由 ScreenCaptureSensor 调用。
- 用于截图瞬间隐藏 FloatingBar，避免它出现在截图中。
- 跨线程安全：hide_fn/show_fn 必须自行保证线程安全（如用 Qt 信号跨线程 emit 到主线程）。
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

from lib.recorder.controller import RecordingController
from workspace.recorder.tools.config import RecordingConfig

# 接近上限提前告警秒数（默认在 max - 120s 时触发，即 30 分钟模式 28 分钟时提示）
WARNING_LEAD_SECONDS = 120


class RecorderApp:
    """录制器应用协调类。

    Args:
        config: 录制配置（来自 ConfigDialog.get_config）
        base_dir: 录制包根目录的父目录（如 workspace/recorder/recordings/）
        sensor_factory: 传感器工厂 (controller, config, overlay_callbacks) → list[Sensor]。
            None 时用默认 _default_sensor_factory（真实 M1-M5）。
            测试可注入 fake 返回 MockSensor 列表。
        on_auto_stop: 自动停止时的回调（无参数），如 GUI 收到后更新 UI。
        on_auto_stop_warning: 接近上限时的回调，参数是剩余秒数（int），
            GUI 收到后弹出"即将自动停止"提示。
        capture_overlay_callbacks: (hide_fn, show_fn) 元组，截图前后由 ScreenCaptureSensor
            调用，用于截图瞬间隐藏 FloatingBar（避免出现在截图中）。
            None 时不隐藏。hide_fn/show_fn 必须线程安全（如用 Qt 信号跨线程 emit）。
    """

    def __init__(
        self,
        config: RecordingConfig,
        base_dir: Path,
        sensor_factory: Callable[[RecordingController, RecordingConfig], list] | None = None,
        on_auto_stop: Callable[[], None] | None = None,
        on_auto_stop_warning: Callable[[int], None] | None = None,
        capture_overlay_callbacks: tuple[Callable[[], None], Callable[[], None]] | None = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self._sensor_factory = sensor_factory or self._default_sensor_factory
        self._on_auto_stop = on_auto_stop
        self._on_auto_stop_warning = on_auto_stop_warning
        self._capture_overlay_callbacks = capture_overlay_callbacks
        # 运行时状态
        self._controller: RecordingController | None = None
        self._sensors: list = []
        self._start_time: float | None = None
        self._auto_stop_timer: threading.Timer | None = None
        self._warning_timer: threading.Timer | None = None
        self._is_running = False
        self._stop_lock = threading.Lock()

    # ========== 生命周期 ==========

    def start(self) -> None:
        """启动录制：实例化 controller + 传感器 + 启动 + 启动自动停止计时器。"""
        if self._is_running:
            return
        # 实例化 controller（sensors 暂空，下面注入；silence_threshold_db 从 config 注入）
        self._controller = RecordingController(
            base_dir=self.base_dir,
            sensors=[],
            silence_threshold_db=self.config.silence_threshold_db,
        )
        # 实例化传感器（通过 sensor_factory，可能注入 fake）
        # 真实 factory 接受 overlay_callbacks 参数（用于截图隐藏 FloatingBar）
        self._sensors = self._call_sensor_factory(self._controller, self.config)
        self._controller.sensors = list(self._sensors)
        # 启动 controller（会调每个 sensor.start）
        self._controller.start()
        self._start_time = time.time()
        self._is_running = True
        # 启动 30 分钟自动停止计时器
        # 注意：方法名必须避开 self._on_auto_stop 属性（构造器注入的 callback），
        # 否则实例属性会覆盖方法，导致 self.stop() 永远不被调用
        max_dur = max(1, int(self.config.max_duration_seconds))
        self._auto_stop_timer = threading.Timer(max_dur, self._handle_auto_stop)
        self._auto_stop_timer.daemon = True
        self._auto_stop_timer.start()
        # 启动接近上限提示计时器（max - 120s 时触发，最少 5s 才有意义）
        warn_at = max(0.0, float(max_dur) - WARNING_LEAD_SECONDS)
        if warn_at >= 5.0:
            self._warning_timer = threading.Timer(warn_at, self._handle_warning)
            self._warning_timer.daemon = True
            self._warning_timer.start()

    def _call_sensor_factory(
        self,
        controller: RecordingController,
        config: RecordingConfig,
    ) -> list:
        """调用 sensor_factory，兼容新旧两种签名。

        旧签名：(controller, config) → list
        新签名：(controller, config, overlay_callbacks) → list

        通过 try/except 兼容 fake factory（测试代码不传 overlay_callbacks）。
        """
        if self._capture_overlay_callbacks is not None:
            try:
                return self._sensor_factory(controller, config, self._capture_overlay_callbacks)
            except TypeError:
                # fake factory 不接受 overlay_callbacks，回退到旧签名
                return self._sensor_factory(controller, config)
        return self._sensor_factory(controller, config)

    def stop(self) -> None:
        """停止录制：取消计时器 + 停 controller（写 meta.json）。幂等。"""
        with self._stop_lock:
            if not self._is_running:
                return
            self._is_running = False
        # 取消计时器
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None
        if self._warning_timer is not None:
            self._warning_timer.cancel()
            self._warning_timer = None
        # 停 controller（停所有传感器 + 写 meta.json）
        if self._controller is not None:
            self._controller.stop()

    def _handle_auto_stop(self) -> None:
        """30 分钟自动停止：内部 stop + 通知 GUI。

        方法名用 _handle_* 前缀避免与构造器注入的 self._on_auto_stop callback 属性冲突
        （Python 实例属性会覆盖类方法，导致 self.stop() 永远不被调用）。
        """
        self.stop()
        if self._on_auto_stop is not None:
            try:
                self._on_auto_stop()
            except Exception:
                pass

    def _handle_warning(self) -> None:
        """接近上限时回调：通知 GUI 剩余秒数。"""
        if self._on_auto_stop_warning is not None:
            remaining = max(0, int(self.config.max_duration_seconds) - WARNING_LEAD_SECONDS)
            try:
                self._on_auto_stop_warning(remaining)
            except Exception:
                pass

    # ========== 状态访问器 ==========

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def elapsed_seconds(self) -> int:
        """已录制时长（秒），未运行时返回 0。"""
        if self._start_time is None or not self._is_running:
            return 0
        return int(time.time() - self._start_time)

    @property
    def current_event_count(self) -> int:
        """当前事件计数（读 controller.event_count）。"""
        return self._controller.event_count if self._controller is not None else 0

    @property
    def current_frame_count(self) -> int:
        """当前帧数（读 controller.frame_count）。"""
        return self._controller.frame_count if self._controller is not None else 0

    @property
    def recording_package_root(self) -> Path | None:
        """录制包根目录（启动后才有，未启动返回 None）。"""
        if self._controller is None:
            return None
        return self._controller.package.root

    @property
    def recording_package_name(self) -> str | None:
        """录制包目录名（如 rec_20260723_143000）。"""
        if self._controller is None:
            return None
        return self._controller.package.name

    # ========== 默认传感器工厂（生产环境用真实 M1-M4） ==========

    def _default_sensor_factory(
        self,
        controller: RecordingController,
        config: RecordingConfig,
        overlay_callbacks: tuple[Callable[[], None], Callable[[], None]] | None = None,
    ) -> list:
        """默认传感器工厂：实例化 M1-M5 全部真实传感器。

        生产环境用这个；测试用 sensor_factory 参数注入 fake 传感器。

        传感器 wire 关系：
        - MouseSensor.on_click_callback → ScreenCaptureSensor.trigger_burst
          （click 后触发多帧采样）
        - WindowFocusSensor.on_focus_change_callback → KeyboardSensor.notify_focus_change
          （窗口切换时立即 flush 当前 keyboard_input 块）
        - ScreenCaptureSensor.on_before_capture/on_after_capture → overlay hide/show
          （截图瞬间隐藏 FloatingBar，避免出现在截图中）

        Args:
            controller: 录制控制器
            config: 录制配置
            overlay_callbacks: (hide_fn, show_fn) 元组，截图前后调用，None 时不隐藏。
        """
        from pynput import keyboard as pynput_keyboard
        from pynput import mouse as pynput_mouse

        from lib.recorder.sensors.audio import AudioSensor
        from lib.recorder.sensors.keyboard import KeyboardSensor
        from lib.recorder.sensors.mouse import MouseSensor
        from lib.recorder.sensors.screen import ScreenCaptureSensor
        from lib.recorder.sensors.window import (
            DEFAULT_POLL_INTERVAL_COARSE,
            DEFAULT_POLL_INTERVAL_DETAILED,
            WindowFocusSensor,
        )

        # ScreenCaptureSensor（先创建，MouseSensor 的 on_click_callback 要引用它）
        # 注入 overlay_callbacks 用于截图瞬间隐藏 FloatingBar
        hide_fn, show_fn = (None, None)
        if overlay_callbacks is not None and len(overlay_callbacks) == 2:
            hide_fn, show_fn = overlay_callbacks
        screen_sensor = ScreenCaptureSensor(
            controller=controller,
            mode=config.range_type,
            monitor_index=config.monitor_index,
            window_hwnd=config.window_hwnd,
            interval=config.capture_interval,
            burst_intervals=config.burst_intervals,
            on_before_capture=hide_fn,
            on_after_capture=show_fn,
            phash_threshold=config.phash_threshold,
        )

        # KeyboardSensor（pynput listener_factory 闭包引用 self._on_press/_on_release）
        keyboard_sensor = KeyboardSensor(
            controller=controller,
            listener_factory=lambda: pynput_keyboard.Listener(
                on_press=keyboard_sensor._on_press,
                on_release=keyboard_sensor._on_release,
            ),
        )

        # MouseSensor（pynput listener_factory + on_click_callback 触发 screen burst）
        mouse_sensor = MouseSensor(
            controller=controller,
            listener_factory=lambda: pynput_mouse.Listener(
                on_click=mouse_sensor._on_click,
                on_scroll=mouse_sensor._on_scroll,
                on_move=mouse_sensor._on_move,
            ),
            on_click_callback=screen_sensor.trigger_burst,
        )

        # WindowFocusSensor（on_focus_change_callback 通知 keyboard_sensor flush）
        poll_interval = (
            DEFAULT_POLL_INTERVAL_DETAILED if config.mode == "detailed"
            else DEFAULT_POLL_INTERVAL_COARSE
        )
        window_sensor = WindowFocusSensor(
            controller=controller,
            poll_interval=poll_interval,
            on_focus_change_callback=keyboard_sensor.notify_focus_change,
        )

        sensors = [keyboard_sensor, mouse_sensor, screen_sensor, window_sensor]

        # AudioSensor 可选（config.audio_enabled 控制）
        # mic-1 修复：传入 audio_device 避免选错麦克风
        if config.audio_enabled:
            audio_sensor = AudioSensor(
                controller=controller,
                device=config.audio_device,
            )
            sensors.append(audio_sensor)

        # ClipboardSensor 可选（config.clipboard_enabled 控制，默认关）
        if config.clipboard_enabled:
            from lib.recorder.sensors.clipboard import ClipboardSensor
            clipboard_sensor = ClipboardSensor(controller=controller)
            sensors.append(clipboard_sensor)

        return sensors
