"""P5 关键帧抽取器（spec-l1.md / 02-block-design.md / Ticket 16）

输入：L0 的 frames/ 目录（已做相邻帧 pHash 去重，D035）+ events.jsonl
输出：keyframes.json（关键帧列表：帧路径 + 时间戳 + 保留原因）

pHash 全局去重：对保留帧做全局 pHash 对比，检测跨时间相似帧
（如滚动后回到原位置），相似帧只留第一张。

保留策略：
- 保留所有操作帧（mouse_click / mouse_drag / focus_change 时刻 200ms 内的截图，
  retain_reason="operation"）—— 操作帧不受全局去重影响
- 对定时截图做全局 pHash 去重（retain_reason="timer_unique"）：
  与已保留帧（operation + timer_unique）汉明距离 ≤ 阈值则跳过

pHash 算法：复用 lib.recorder.sensors.screen.compute_phash / phash_distance
（8×8 灰度 + 均值哈希 + 汉明距离，与 L0 D035 一致）

目标：360 帧降到 ~50 帧（08-roadmap.md 验收标准）
"""

import json
from pathlib import Path

from PIL import Image

from lib.recorder.sensors.screen import (
    DEFAULT_PHASH_THRESHOLD,
    compute_phash,
    phash_distance,
)
from lib.recorder.types import (
    KIND_FOCUS_CHANGE,
    KIND_MOUSE_CLICK,
    KIND_MOUSE_DRAG,
    KIND_SCREEN_FRAME,
)

# 操作帧关联窗口（秒）：帧在操作事件后此时间内视为操作帧
DEFAULT_OPERATION_FRAME_WINDOW = 0.2

# 触发 burst 截图的操作事件 kind（mouse_click / mouse_drag / focus_change）
# keyboard_input / hotkey / ime_switch / password_masked 不触发 burst
_OPERATION_FRAME_TRIGGERS = {KIND_MOUSE_CLICK, KIND_MOUSE_DRAG, KIND_FOCUS_CHANGE}


def extract_keyframes(
    events: list[dict],
    frames_dir: Path,
    phash_threshold: int = DEFAULT_PHASH_THRESHOLD,
    operation_frame_window: float = DEFAULT_OPERATION_FRAME_WINDOW,
) -> list[dict]:
    """从事件流 + frames/ 目录抽取关键帧。

    Args:
        events: L0 events.jsonl 解析后的事件列表
        frames_dir: frames/ 目录路径
        phash_threshold: pHash 全局去重阈值（汉明距离 ≤ 此值视为相似）
        operation_frame_window: 操作帧关联窗口（秒），帧在操作事件后此时间内为操作帧

    Returns:
        关键帧列表 [{frame_path, timestamp, retain_reason}, ...]
    """
    # 分离 screen_frame 事件和操作事件
    frame_entries: list[tuple[float, str]] = []  # [(timestamp, frame_path), ...]
    operation_timestamps: list[float] = []
    for ev in events:
        if ev["kind"] == KIND_SCREEN_FRAME:
            frame_path = ev.get("payload", {}).get("frame_path")
            if frame_path is not None:
                frame_entries.append((float(ev["timestamp"]), frame_path))
        elif ev["kind"] in _OPERATION_FRAME_TRIGGERS:
            operation_timestamps.append(float(ev["timestamp"]))

    frame_entries.sort(key=lambda x: x[0])
    operation_timestamps.sort()

    keyframes: list[dict] = []
    # 已保留帧的 pHash 集合（用于全局去重）
    kept_phashes: list[int] = []

    for frame_ts, frame_path in frame_entries:
        is_operation = _is_operation_frame(frame_ts, operation_timestamps, operation_frame_window)
        if is_operation:
            # 操作帧：直接保留，不做 pHash 去重
            keyframes.append({
                "frame_path": frame_path,
                "timestamp": frame_ts,
                "retain_reason": "operation",
            })
            # 仍计算 pHash 加入 kept_phashes（供后续 timer 帧对比）
            phash = _compute_frame_phash(frames_dir, frame_path)
            if phash is not None:
                kept_phashes.append(phash)
        else:
            # 定时截图：全局 pHash 去重
            phash = _compute_frame_phash(frames_dir, frame_path)
            if phash is None:
                # 无法计算 pHash（文件不存在/损坏）→ 保留（保守策略）
                keyframes.append({
                    "frame_path": frame_path,
                    "timestamp": frame_ts,
                    "retain_reason": "timer_unique",
                })
                continue
            # 与已保留帧对比
            is_duplicate = False
            for kept_h in kept_phashes:
                if phash_distance(phash, kept_h) <= phash_threshold:
                    is_duplicate = True
                    break
            if not is_duplicate:
                keyframes.append({
                    "frame_path": frame_path,
                    "timestamp": frame_ts,
                    "retain_reason": "timer_unique",
                })
                kept_phashes.append(phash)

    return keyframes


def _is_operation_frame(
    frame_ts: float,
    operation_timestamps: list[float],
    window: float,
) -> bool:
    """判断帧是否为操作帧（在某个操作事件后 window 秒内）。

    Args:
        frame_ts: 帧时间戳
        operation_timestamps: 操作事件时间戳列表（升序）
        window: 关联窗口（秒）

    Returns:
        True 如果帧在某个操作事件后 [0, window] 秒内
    """
    for op_ts in operation_timestamps:
        delta = frame_ts - op_ts
        if 0 <= delta <= window:
            return True
        if delta < 0:
            # 帧在操作之前，且操作时间戳已超过 frame_ts（列表升序）
            break
    return False


def _compute_frame_phash(frames_dir: Path, frame_path: str) -> int | None:
    """计算帧文件的 pHash。

    Args:
        frames_dir: frames/ 目录路径
        frame_path: 帧相对路径（如 "frames/frame_00001.png"）或文件名

    Returns:
        pHash 值，文件不存在/损坏时返回 None
    """
    # frame_path 可能是 "frames/frame_xxx.png" 或纯文件名
    path = Path(frame_path)
    if path.is_absolute():
        full_path = path
    elif path.parts and path.parts[0] == "frames":
        # 相对于录制包根目录的路径
        full_path = frames_dir.parent / path
    else:
        # 纯文件名
        full_path = frames_dir / path

    if not full_path.exists():
        return None
    try:
        with Image.open(full_path) as img:
            return compute_phash(img)
    except Exception:
        return None


def write_keyframes_json(keyframes: list[dict], package_root: Path) -> Path:
    """把关键帧列表写入录制包的 keyframes.json。

    Args:
        keyframes: 关键帧列表
        package_root: 录制包根目录

    Returns:
        keyframes.json 文件路径
    """
    keyframes_path = package_root / "keyframes.json"
    keyframes_path.write_text(
        json.dumps(keyframes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return keyframes_path


def run_p5(
    package_root: Path,
    phash_threshold: int = DEFAULT_PHASH_THRESHOLD,
) -> list[dict]:
    """P5 关键帧抽取器入口：读 events.jsonl + frames/ → 抽取 → 写 keyframes.json。

    Args:
        package_root: 录制包根目录
        phash_threshold: pHash 全局去重阈值

    Returns:
        关键帧列表
    """
    events_file = package_root / "events.jsonl"
    if not events_file.exists():
        raise FileNotFoundError(f"events.jsonl 不存在：{events_file}")

    frames_dir = package_root / "frames"
    if not frames_dir.exists():
        raise FileNotFoundError(f"frames/ 目录不存在：{frames_dir}")

    events: list[dict] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    keyframes = extract_keyframes(
        events,
        frames_dir,
        phash_threshold=phash_threshold,
    )
    write_keyframes_json(keyframes, package_root)
    return keyframes
