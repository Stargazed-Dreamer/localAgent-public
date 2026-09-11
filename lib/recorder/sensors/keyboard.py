"""L0 M1 键鼠监听器 - KeyboardSensor

设计（spec M1 / 03-input-detection.md / D021 / D022 / D025）：
- pynput.keyboard.Listener 全局钩子监听物理按键
- 300ms 时间窗口聚合 keyboard_input 块（开始=第一次非修饰键 down，结束=300ms 无新按键或焦点离开）
- 中文输入法三层检测：
  1. UIA ValuePattern 差值（主路径）：输入块开始+结束各取一次 UIA 快照，对比焦点控件 Value 变化得最终文字
  2. OCR 差异（兜底）：L0 不实现（性能开销大，spec 明确说 L0 不跑 VL/OCR），留 TODO 给 L1
  3. 物理按键序列（最低保底）：UIA 失败时 primary.text="" + supplements.physical_keys 记录原始按键
- 密码框保护（D021）：IsPassword=True 时发 password_masked 事件，不记录任何文本/按键内容
- 物理按键序列保留（D022）：即使 UIA 取到文字，physical_keys 仍保留（能看出拼音 vs 五笔、选了第几个候选词）
- 特殊按键识别：
  - Ctrl+C/V/X → hotkey 事件（独立于 keyboard_input 块）
  - Shift+Space / Ctrl+Space → ime_switch 事件
  - Alt+Tab → 不处理（WindowFocusSensor 会捕获 focus_change）

线程模型：
- pynput Listener 内部起 daemon 线程，回调 _on_press / _on_release
- _on_press 中处理：modifier 跟踪 / 特殊组合识别 / 块管理
- threading.Timer 实现 300ms 超时（每次按键 cancel+restart）
- _lock 保护块状态（_block_active / _physical_keys / _start_snapshot / _held_modifiers）
- stop() 停 listener + cancel timer + flush 当前块

注入点（测试 seam）：
- listener_factory: 返回带 start()/stop() 的对象（默认用 pynput.keyboard.Listener）
- uia_snapshot_fn: 返回 {value, is_password} 或 None（默认用 lib.uia.take_focused_value_snapshot）
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from lib.uia import take_focused_value_snapshot

logger = logging.getLogger(__name__)

# 默认时间窗口（秒）
DEFAULT_TIME_WINDOW = 0.3  # 300ms

# 修饰键集合（pynput Key.name 归一化后）
_MODIFIER_KEYS = {"ctrl", "alt", "shift", "cmd"}

# 热键组合：Ctrl + C/V/X → hotkey 事件
_HOTKEY_MAP = {
    frozenset(["ctrl", "c"]): "copy",
    frozenset(["ctrl", "v"]): "paste",
    frozenset(["ctrl", "x"]): "cut",
}

# 输入法切换组合：Shift+Space / Ctrl+Space → ime_switch 事件
_IME_SWITCH_KEYS = {
    frozenset(["shift", "space"]),
    frozenset(["ctrl", "space"]),
}


def _normalize_key(key: Any) -> str:
    """把 pynput key 归一化为简单字符串。

    - Key.ctrl_l / Key.ctrl_r → "ctrl"
    - Key.alt_l / Key.alt_r → "alt"
    - Key.shift / Key.shift_l / Key.shift_r → "shift"
    - Key.cmd_l / Key.cmd_r → "cmd"
    - Key.space → "space"
    - Key.tab → "tab"
    - Key.enter → "enter"
    - Key.backspace → "backspace"
    - Key.caps_lock → "caps_lock"
    - KeyCode(char='c') / KeyCode(char='C') → "c"（统一小写，shift 状态由 _held_modifiers 跟踪）
    - 其它特殊键 → key.name（如 "f1", "left", "delete"）
    - 无法识别 → str(key)
    """
    # KeyCode（字符键）
    char = getattr(key, "char", None)
    if char is not None and len(char) == 1:
        return char.lower()
    # Key 枚举（特殊键）
    name = getattr(key, "name", None)
    if name is not None:
        # 归一化左右修饰键
        if name in ("ctrl_l", "ctrl_r"):
            return "ctrl"
        if name in ("alt_l", "alt_r", "alt_gr"):
            return "alt"
        if name in ("shift", "shift_l", "shift_r"):
            return "shift"
        if name in ("cmd_l", "cmd_r", "cmd"):
            return "cmd"
        return name
    return str(key)


class KeyboardSensor:
    """M1 键盘监听器。

    Args:
        controller: RecordingController 引用
        time_window: 时间窗口（秒），默认 0.3（300ms）
        listener_factory: 返回带 start()/stop() 的 listener 对象。
                          None 时默认用 pynput.keyboard.Listener。
                          测试可注入 fake listener 直接调 _on_press / _on_release。
        uia_snapshot_fn: 返回 {"value": str, "is_password": bool} 或 None。
                         None 时默认用 lib.uia.take_focused_value_snapshot。
                         测试可注入 fake 返回固定值。
    """

    def __init__(
        self,
        controller,
        time_window: float = DEFAULT_TIME_WINDOW,
        listener_factory: Callable[[], Any] | None = None,
        uia_snapshot_fn: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.controller = controller
        self.time_window = float(time_window)
        self._listener_factory = listener_factory
        self._uia_snapshot_fn = uia_snapshot_fn or take_focused_value_snapshot
        # 状态
        self._listener: Any | None = None
        self._lock = threading.Lock()
        self._held_modifiers: set[str] = set()
        self._block_active = False
        self._block_start_snapshot: dict[str, Any] | None = None
        self._physical_keys: list[str] = []
        self._block_start_mono: float = 0.0
        self._timer: threading.Timer | None = None
        self._started = False

    def start(self) -> None:
        """启动 listener。"""
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("KeyboardSensor.controller 未注入")
        self._started = True
        # lazy 创建 listener（测试可不传 factory，直接调 _on_press）
        if self._listener_factory is not None:
            listener = self._listener_factory()
            self._listener = listener
            listener.start()

    def stop(self) -> None:
        """停止 listener + cancel timer + flush 当前块。幂等。"""
        if not self._started:
            return
        self._started = False
        # 停 listener
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        # cancel timer
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        # flush 当前块
        self._flush_block()

    def notify_focus_change(self) -> None:
        """外部（如 WindowFocusSensor）通知焦点变化，立即 flush 当前块。

        用于"焦点离开当前控件"结束输入块的场景（spec 第六节）。
        """
        self._flush_block()

    # ========== pynput 回调（listener 调用，测试也可直接调） ==========

    def _on_press(self, key: Any) -> None:
        """按键按下回调。"""
        if not self._started:
            return
        norm = _normalize_key(key)
        # 1. 修饰键：跟踪状态，不单独触发块（但块激活时加入 physical_keys）
        if norm in _MODIFIER_KEYS:
            with self._lock:
                self._held_modifiers.add(norm)
                if self._block_active:
                    self._physical_keys.append(norm)
                    self._reset_timer_locked()
            return
        # 2. 特殊组合检测（modifier + 当前键）
        combo = frozenset(self._held_modifiers | {norm})
        if combo in _HOTKEY_MAP:
            # Ctrl+C/V/X → hotkey 事件（先 flush 当前块，再发 hotkey）
            self._flush_block()
            self._emit_hotkey(list(combo), _HOTKEY_MAP[combo])
            return
        if combo in _IME_SWITCH_KEYS:
            # Shift+Space / Ctrl+Space → ime_switch 事件
            self._flush_block()
            self._emit_ime_switch(list(combo))
            return
        # 3. 普通按键：加入当前块
        with self._lock:
            if not self._block_active:
                self._start_block_locked(norm)
            else:
                self._physical_keys.append(norm)
                self._reset_timer_locked()

    def _on_release(self, key: Any) -> None:
        """按键释放回调。"""
        if not self._started:
            return
        norm = _normalize_key(key)
        if norm in _MODIFIER_KEYS:
            with self._lock:
                self._held_modifiers.discard(norm)

    # ========== 块管理（必须持锁调用） ==========

    def _start_block_locked(self, first_key: str) -> None:
        """开始新的 keyboard_input 块。调用方必须持 self._lock。"""
        self._block_active = True
        self._physical_keys = []
        # 加入当前持有的修饰键（能看出 Shift+字母 等组合）
        for mod in sorted(self._held_modifiers):
            self._physical_keys.append(mod)
        self._physical_keys.append(first_key)
        # 取块开始 UIA 快照（用于差值检测）
        try:
            self._block_start_snapshot = self._uia_snapshot_fn()
        except Exception:
            self._block_start_snapshot = None
        # 记录块开始时间
        try:
            self._block_start_mono = self.controller.timestamp.now()[0]
        except Exception:
            self._block_start_mono = 0.0
        # 启动 300ms 超时 timer
        self._reset_timer_locked()

    def _reset_timer_locked(self) -> None:
        """重置 300ms 超时 timer。调用方必须持 self._lock。"""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.time_window, self._flush_block)
        self._timer.daemon = True
        self._timer.start()

    def _flush_block(self) -> None:
        """flush 当前 keyboard_input 块（发事件 + 重置状态）。线程安全。"""
        with self._lock:
            if not self._block_active:
                # 清理可能残留的 timer
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                return
            physical_keys = list(self._physical_keys)
            start_snapshot = self._block_start_snapshot
            # 重置块状态
            self._block_active = False
            self._physical_keys = []
            self._block_start_snapshot = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        # 发事件（不持锁，避免与 controller._lock 死锁）
        self._emit_keyboard_input(physical_keys, start_snapshot)

    # ========== 事件发射 ==========

    def _safe_emit(self, kind: str, payload: dict[str, Any]) -> None:
        """发事件到 controller，IO 异常兜底不崩调用线程。

        pynput 回调线程 / Timer 线程里 controller.emit_event 抛 IO 异常
        （磁盘满/权限）会沿回调冒泡，导致 pynput listener 静默停止，
        后续按键全部丢失；此处与 clipboard/screen sensor 同款保护。
        """
        try:
            self.controller.emit_event(kind, payload)
        except Exception:
            logger.exception("keyboard 事件 %s 写入失败", kind)

    def _emit_keyboard_input(
        self,
        physical_keys: list[str],
        start_snapshot: dict[str, Any] | None,
    ) -> None:
        """发 keyboard_input 或 password_masked 事件。

        密码框检测（D021）：
        - 若 start_snapshot.is_password=True → 发 password_masked，不记录内容
        - 若 start_snapshot=None（UIA 不可用）→ 无法判断密码框，按正常处理

        文字检测（三层策略，spec 第三节）：
        - 取块结束 UIA 快照
        - 若 start/end 都有 value → diff 得最终文字，detection_method="uia_value_diff"
        - 若 UIA 不可用 → primary.text=""，detection_method="physical_keys"（OCR 兜底留 L1）
        """
        # 取结束快照
        try:
            end_snapshot = self._uia_snapshot_fn()
        except Exception:
            end_snapshot = None

        # 密码框保护（D021）：start 或 end 任一为密码框都保护
        is_password = bool(
            (start_snapshot and start_snapshot.get("is_password"))
            or (end_snapshot and end_snapshot.get("is_password"))
        )
        if is_password:
            payload = {
                "is_password": True,
                "physical_keys": [],  # 不记录密码内容
                "detection_method": "password_masked",
            }
            self._safe_emit("password_masked", payload)
            return

        # 文字检测（Strategy 2: UIA ValuePattern 差值）
        text = ""
        detection_method = "physical_keys"  # 默认保底
        if start_snapshot is not None and end_snapshot is not None:
            start_val = start_snapshot.get("value", "") or ""
            end_val = end_snapshot.get("value", "") or ""
            # diff：end_val 比 start_val 多出的部分就是本次输入
            if end_val.startswith(start_val):
                text = end_val[len(start_val):]
            elif start_val.startswith(end_val):
                # 用户删除了内容（Backspace），diff 为空
                text = ""
            else:
                # 完全不同（光标移动/替换），取 end_val 作为近似
                text = end_val
            detection_method = "uia_value_diff"
        # TODO(L1): 若 UIA 不可用，加 OCR 差异检测作为 Strategy 3 兜底

        payload = {
            "text": text,
            "physical_keys": physical_keys,
            "detection_method": detection_method,
            "is_password": False,
        }
        self.controller.emit_event("keyboard_input", payload)

    def _emit_hotkey(self, keys: list[str], combo: str) -> None:
        """发 hotkey 事件（Ctrl+C/V/X）。"""
        payload = {
            "keys": keys,
            "combo": combo,  # "copy" / "paste" / "cut"
        }
        self._safe_emit("hotkey", payload)

    def _emit_ime_switch(self, keys: list[str]) -> None:
        """发 ime_switch 事件（Shift+Space / Ctrl+Space）。"""
        payload = {
            "keys": keys,
            "physical_keys": keys,
        }
        self._safe_emit("ime_switch", payload)
