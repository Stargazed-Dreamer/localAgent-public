"""L0 采集层事件类型协议

事件统一用 dict 表示（JSONL 友好），通过 make_event 工厂函数构造。
未来若需强类型校验，可升级为 pydantic Model，但 L0 阶段先用 dict 保持简单。

事件结构（对应 02-block-design.md 的块模型，L0 只采集原始事件，块聚合在 L1）：

    {
        "timestamp": float,        # monotonic 偏移（秒，从录制开始算）
        "abs_timestamp": float,    # time.time() 绝对时间
        "kind": str,               # 事件类型（见 KIND_* 常量）
        "payload": dict,           # 事件负载，结构因 kind 而异
    }

L0 事件 kind 取值（M1-M5）：
- mouse_click / mouse_scroll / mouse_drag        (M1)
- keyboard_input / hotkey / ime_switch           (M1)
- focus_change                                   (M1 / M4)
- screen_frame                                   (M2)
- audio_chunk                                    (M3，仅写 WAV 不写 events.jsonl）
- password_masked                                (M1，密码框保护）
- clipboard_change                               (M5，剪贴板变化，默认关）
"""

from typing import Any

# 事件 kind 常量（M1 键鼠）
KIND_MOUSE_CLICK = "mouse_click"
KIND_MOUSE_SCROLL = "mouse_scroll"
KIND_MOUSE_DRAG = "mouse_drag"
KIND_KEYBOARD_INPUT = "keyboard_input"
KIND_HOTKEY = "hotkey"
KIND_IME_SWITCH = "ime_switch"
KIND_FOCUS_CHANGE = "focus_change"
KIND_PASSWORD_MASKED = "password_masked"

# M2 屏幕
KIND_SCREEN_FRAME = "screen_frame"

# M3 音频（不写 events.jsonl，直接写 WAV；保留 kind 用于内部事件总线）
KIND_AUDIO_CHUNK = "audio_chunk"

# M5 剪贴板（默认关闭，需在 config.toml [recording] clipboard_enabled=true 启用）
KIND_CLIPBOARD_CHANGE = "clipboard_change"


def make_event(timestamp: float, abs_timestamp: float, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """构造事件 dict。

    所有事件必须通过此函数构造，确保字段完整。

    Args:
        timestamp: monotonic 偏移（秒，从录制开始算）
        abs_timestamp: time.time() 绝对时间
        kind: 事件类型，见 KIND_* 常量
        payload: 事件负载

    Returns:
        事件 dict，可直接 json.dumps
    """
    return {
        "timestamp": float(timestamp),
        "abs_timestamp": float(abs_timestamp),
        "kind": str(kind),
        "payload": dict(payload),
    }
