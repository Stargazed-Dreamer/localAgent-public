"""L0 采集层 Ticket 06：全局热键管理（HotkeyManager）

设计（05-gui-design.md + spec user story 5）：
- 用 pynput.keyboard.GlobalHotKeys 注册全局热键（不通过 KeyboardSensor，避免干扰）
- 默认热键：
  - <ctrl>+<alt>+r → start/stop toggle（开始/停止录制）
- 热键在录制器进程内全局生效，无论焦点在哪个窗口
- 回调在 pynput listener 线程中执行，调用方需注意线程安全（GUI 操作需通过 signal 抛回主线程）

线程模型：
- start() 启动 pynput keyboard.GlobalHotKeys listener（daemon 线程）
- stop() 停止 listener
- 回调异常 try/except 兜底不崩 listener

测试策略：
- listener_factory 注入：测试不传则不创建真实 listener，直接调 _on_hotkey 验证回调
- 与 KeyboardSensor 类似的注入模式
"""

from collections.abc import Callable

# 默认热键配置（pynput keyboard.GlobalHotKeys 格式）
DEFAULT_HOTKEYS: dict[str, str] = {
    "<ctrl>+<alt>+r": "start_stop",  # Ctrl+Alt+R → 开始/停止录制
}


class HotkeyManager:
    """全局热键管理器。

    Args:
        on_start_stop: 开始/停止热键回调（无参数），由调用方实现具体的 toggle 逻辑。
            回调在 pynput listener 线程中执行，GUI 操作需通过 signal 抛回主线程。
        hotkeys: 热键配置（pynput 格式 → action 名），默认 DEFAULT_HOTKEYS。
            扩展时加新条目即可（如 "<ctrl>+<alt>+p": "pause"），需在 _dispatch 中处理。
        listener_factory: listener 工厂，None 时用 pynput.keyboard.GlobalHotKeys。
            测试时传 fake 验证回调逻辑（不真实启动 listener）。
    """

    def __init__(
        self,
        on_start_stop: Callable[[], None] | None = None,
        hotkeys: dict[str, str] | None = None,
        listener_factory: Callable | None = None,
    ) -> None:
        self._on_start_stop = on_start_stop
        self._hotkeys = dict(hotkeys) if hotkeys is not None else dict(DEFAULT_HOTKEYS)
        self._listener_factory = listener_factory
        self._listener = None
        self._started = False

    def start(self) -> None:
        """启动全局热键监听。幂等。"""
        if self._started:
            return
        self._started = True
        # 构造 hotkey → callback 映射
        hotkey_map = {combo: lambda action=action: self._dispatch(action)
                      for combo, action in self._hotkeys.items()}
        # 用 listener_factory 或默认 pynput.keyboard.GlobalHotKeys
        if self._listener_factory is not None:
            self._listener = self._listener_factory(hotkey_map)
        else:
            try:
                from pynput import keyboard
                self._listener = keyboard.GlobalHotKeys(hotkey_map)
            except Exception:
                self._listener = None
                self._started = False
                return
        if self._listener is not None:
            try:
                self._listener.start()
            except Exception:
                self._listener = None
                self._started = False

    def stop(self) -> None:
        """停止全局热键监听。幂等。"""
        if not self._started:
            return
        self._started = False
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def _dispatch(self, action: str) -> None:
        """热键触发时分发到对应回调。

        Args:
            action: 热键配置中的 action 名（如 "start_stop"）
        """
        try:
            if action == "start_stop" and self._on_start_stop is not None:
                self._on_start_stop()
        except Exception:
            # 回调异常不应崩 listener
            pass

    # 便于测试直接调用
    def _on_hotkey(self, action: str) -> None:
        """测试用：直接触发 action（绕过 pynput listener）。"""
        self._dispatch(action)

    @property
    def is_running(self) -> bool:
        return self._started
