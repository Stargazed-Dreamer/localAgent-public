"""P4 事件聚合器（spec-l1.md 第三节 / 02-block-design.md）

输入：L0 的 events.jsonl（已聚合的事件流——L0 KeyboardSensor 已做 300ms 窗口聚合）
输出：blocks.json（02-block-design.md 定义的块结构）

聚合规则：
- 每个底层操作事件转为一个操作块（mouse_click / mouse_scroll / mouse_drag /
  keyboard_input / focus_change）
- 任意操作之间空闲 > idle_threshold（默认 2.0s）插入 idle 块
- 操作块自动关联"前截图"（操作前 before_frame_window 内最近帧，默认 200ms）
  + "后截图"（操作后 after_frame_window 内最近帧，默认 300ms）
- 复用 L0 聚合结果：不再次聚合 keystroke（L0 KeyboardSensor 已做 300ms 窗口聚合）

事件 kind → 块类型映射：
- mouse_click → operation/mouse_click
- mouse_scroll → operation/mouse_scroll
- mouse_drag → operation/mouse_drag
- keyboard_input → operation/keyboard_input
- hotkey → operation/keyboard_input（hotkey 归类为键盘输入）
- ime_switch → operation/keyboard_input（输入法切换归类为键盘输入）
- password_masked → operation/keyboard_input（is_password=true，text 脱敏）
- focus_change → operation/focus_change
- screen_frame → 不生成块（用于截图关联）
- audio_chunk → 不生成块（L0 不写 events.jsonl）
- clipboard_change → 不生成块（默认关闭，L1 暂不处理）
"""

import json
from pathlib import Path

from lib.recorder.types import (
    KIND_FOCUS_CHANGE,
    KIND_HOTKEY,
    KIND_IME_SWITCH,
    KIND_KEYBOARD_INPUT,
    KIND_MOUSE_CLICK,
    KIND_MOUSE_DRAG,
    KIND_MOUSE_SCROLL,
    KIND_PASSWORD_MASKED,
    KIND_SCREEN_FRAME,
)

# idle 块默认阈值（秒）：操作间隔超过此值插入 idle 块
DEFAULT_IDLE_THRESHOLD = 2.0

# 截图关联窗口（秒）
DEFAULT_BEFORE_FRAME_WINDOW = 0.2   # 操作前 200ms
DEFAULT_AFTER_FRAME_WINDOW = 0.3    # 操作后 300ms

# 事件 kind → 块 type 映射（不在此表的 kind 不生成块）
_KIND_TO_BLOCK_TYPE: dict[str, str] = {
    KIND_MOUSE_CLICK: "mouse_click",
    KIND_MOUSE_SCROLL: "mouse_scroll",
    KIND_MOUSE_DRAG: "mouse_drag",
    KIND_KEYBOARD_INPUT: "keyboard_input",
    KIND_HOTKEY: "keyboard_input",
    KIND_IME_SWITCH: "keyboard_input",
    KIND_PASSWORD_MASKED: "keyboard_input",
    KIND_FOCUS_CHANGE: "focus_change",
}


def aggregate_events(
    events: list[dict],
    idle_threshold: float = DEFAULT_IDLE_THRESHOLD,
    before_frame_window: float = DEFAULT_BEFORE_FRAME_WINDOW,
    after_frame_window: float = DEFAULT_AFTER_FRAME_WINDOW,
) -> list[dict]:
    """把 L0 事件流聚合为操作块序列。

    Args:
        events: L0 events.jsonl 解析后的事件列表（按 timestamp 升序）
        idle_threshold: idle 块阈值（秒），操作间隔 > 此值插入 idle 块
        before_frame_window: 前截图窗口（秒），操作前此时间内最近帧关联
        after_frame_window: 后截图窗口（秒），操作后此时间内最近帧关联

    Returns:
        操作块列表（符合 02-block-design.md 块结构）
    """
    # 分离操作事件和截图事件
    operation_events: list[dict] = []
    frame_events: list[dict] = []
    for ev in events:
        if ev["kind"] == KIND_SCREEN_FRAME:
            frame_events.append(ev)
        elif ev["kind"] in _KIND_TO_BLOCK_TYPE:
            operation_events.append(ev)

    # 按时间戳排序（L0 已保证升序，这里防御性再排一次）
    operation_events.sort(key=lambda e: e["timestamp"])
    frame_events.sort(key=lambda e: e["timestamp"])

    blocks: list[dict] = []
    block_seq = 0
    last_op_timestamp: float | None = None

    for ev in operation_events:
        ts = float(ev["timestamp"])

        # idle 块生成：与上一操作间隔 > idle_threshold
        if last_op_timestamp is not None:
            gap = ts - last_op_timestamp
            if gap > idle_threshold:
                block_seq += 1
                blocks.append(_make_idle_block(block_seq, last_op_timestamp, gap))

        # 操作块
        block_seq += 1
        block = _event_to_block(block_seq, ev)
        # 截图关联
        before_frame, after_frame = _associate_frames(
            ts, frame_events, before_frame_window, after_frame_window
        )
        block["supplements"]["before_frame"] = before_frame
        block["supplements"]["after_frame"] = after_frame
        blocks.append(block)

        last_op_timestamp = ts

    return blocks


def _event_to_block(seq: int, event: dict) -> dict:
    """把单个 L0 事件转为操作块。

    Args:
        seq: 块序号（1-based）
        event: L0 事件 dict

    Returns:
        操作块 dict（符合 02-block-design.md 块结构）
    """
    kind = event["kind"]
    block_type = _KIND_TO_BLOCK_TYPE[kind]
    payload = event.get("payload", {})
    ts = float(event["timestamp"])

    primary = _build_primary(kind, block_type, payload)

    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": block_type,
        "timestamp": ts,
        "duration": 0.0,  # 瞬时事件 duration=0；idle 块单独设置
        "primary": primary,
        "supplements": {
            "before_frame": None,  # 由 _associate_frames 填充
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _build_primary(kind: str, block_type: str, payload: dict) -> dict:
    """根据事件 kind 构造块的 primary 字段。"""
    if kind == KIND_MOUSE_CLICK:
        return {
            "x": payload.get("x", 0),
            "y": payload.get("y", 0),
            "button": payload.get("button", "left"),
            "clicks": payload.get("clicks", 1),
        }
    if kind == KIND_MOUSE_SCROLL:
        return {
            "x": payload.get("x", 0),
            "y": payload.get("y", 0),
            "dx": payload.get("dx", 0),
            "dy": payload.get("dy", 0),
        }
    if kind == KIND_MOUSE_DRAG:
        return {
            "button": payload.get("button", "left"),
            "start": payload.get("start", {"x": 0, "y": 0}),
            "end": payload.get("end", {"x": 0, "y": 0}),
            "track": payload.get("track", []),
        }
    if kind == KIND_KEYBOARD_INPUT:
        is_password = payload.get("is_password", False)
        return {
            "text": payload.get("text", ""),
            "physical_keys": [] if is_password else payload.get("physical_keys", []),
            "detection_method": payload.get("detection_method", "physical_keys"),
            "is_password": is_password,
        }
    if kind == KIND_HOTKEY:
        return {
            "text": payload.get("combo", ""),  # hotkey 用 combo 作为 text（如 "copy"）
            "detection_method": "hotkey",
            "is_password": False,
            "keys": payload.get("keys", []),
        }
    if kind == KIND_IME_SWITCH:
        return {
            "text": "",
            "detection_method": "ime_switch",
            "is_password": False,
            "keys": payload.get("keys", []),
        }
    if kind == KIND_PASSWORD_MASKED:
        return {
            "text": "",  # 脱敏：不记录实际输入
            "detection_method": "password_masked",
            "is_password": True,
        }
    if kind == KIND_FOCUS_CHANGE:
        return {
            "hwnd": payload.get("hwnd", 0),
            "title": payload.get("title", ""),
            "pid": payload.get("pid", 0),
        }
    # 兜底（不应到达，_KIND_TO_BLOCK_TYPE 已过滤）
    return dict(payload)


def _make_idle_block(seq: int, start_ts: float, duration: float) -> dict:
    """构造 idle 块。

    Args:
        seq: 块序号
        start_ts: idle 开始时间戳（上一操作的 timestamp）
        duration: idle 持续时间（秒）

    Returns:
        idle 块 dict
    """
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": "idle",
        "timestamp": start_ts,  # idle 从上一操作时刻开始
        "duration": duration,
        "primary": {"duration": duration},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _associate_frames(
    op_ts: float,
    frame_events: list[dict],
    before_window: float,
    after_window: float,
) -> tuple[str | None, str | None]:
    """为操作块关联前后截图。

    Args:
        op_ts: 操作时间戳
        frame_events: 截图事件列表（按 timestamp 升序）
        before_window: 前截图窗口（秒），找 [op_ts - before_window, op_ts] 内最近帧
        after_window: 后截图窗口（秒），找 [op_ts, op_ts + after_window] 内最近帧

    Returns:
        (before_frame_path, after_frame_path)，无匹配时为 None
    """
    before_frame: str | None = None
    after_frame: str | None = None

    before_best_ts = float("-inf")
    after_best_ts = float("inf")

    for fev in frame_events:
        frame_ts = float(fev["timestamp"])
        frame_path = fev.get("payload", {}).get("frame_path")
        if frame_path is None:
            continue

        # 前截图：[op_ts - before_window, op_ts]，取最近的一帧（timestamp 最大）
        if (op_ts - before_window) <= frame_ts <= op_ts:
            if frame_ts > before_best_ts:
                before_best_ts = frame_ts
                before_frame = frame_path

        # 后截图：[op_ts, op_ts + after_window]，取最近的一帧（timestamp 最小）
        if op_ts <= frame_ts <= (op_ts + after_window):
            if frame_ts < after_best_ts:
                after_best_ts = frame_ts
                after_frame = frame_path

    return before_frame, after_frame


def write_blocks_json(blocks: list[dict], package_root: Path) -> Path:
    """把操作块序列写入录制包的 blocks.json。

    Args:
        blocks: 操作块列表
        package_root: 录制包根目录

    Returns:
        blocks.json 文件路径
    """
    blocks_path = package_root / "blocks.json"
    blocks_path.write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return blocks_path


def run_p4(
    package_root: Path,
    idle_threshold: float = DEFAULT_IDLE_THRESHOLD,
    before_frame_window: float = DEFAULT_BEFORE_FRAME_WINDOW,
    after_frame_window: float = DEFAULT_AFTER_FRAME_WINDOW,
) -> list[dict]:
    """P4 事件聚合器入口：读取 events.jsonl → 聚合 → 写 blocks.json。

    Args:
        package_root: 录制包根目录
        idle_threshold: idle 块阈值（秒）
        before_frame_window: 前截图窗口（秒）
        after_frame_window: 后截图窗口（秒）

    Returns:
        操作块列表
    """
    events_file = package_root / "events.jsonl"
    if not events_file.exists():
        raise FileNotFoundError(f"events.jsonl 不存在：{events_file}")

    events: list[dict] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    blocks = aggregate_events(
        events,
        idle_threshold=idle_threshold,
        before_frame_window=before_frame_window,
        after_frame_window=after_frame_window,
    )
    write_blocks_json(blocks, package_root)
    return blocks
