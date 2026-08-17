"""活动信号检测 — 分析最近的窗口采集记录，判断用户活跃状态

输入：windows_YYYYMMDD.jsonl（由 CollectWindowsAction 每分钟写入）
输出：信号 dict
    {
        "state": "idle" | "passive" | "active" | "focus_changed" | "unknown",
        "focus_changed": bool,                  # 前台窗口是否变化（True 时 VL 应优先放行）
        "current_foreground": str,              # 当前前台窗口标题
        "previous_foreground": str,             # 上一次前台窗口标题
        "minutes_since_last_record": int,       # 距上次 windows 采集的分钟数
        "window_count_delta": int,              # 窗口列表数量变化（打开/关闭）
    }

判定逻辑：
- 读最近 2 条 windows 记录
- 如果 idle_seconds > idle_threshold_minutes*60（默认 300 秒）→ state="idle"
  （idle_seconds 来自 Win32 GetLastInputInfo，真实反映用户输入空闲时间）
- 兜底：如果 idle_seconds 不可用（<0），用记录间隔 > idle_threshold_minutes 判 idle
  （此分支仅在后端停止采集时触发，不能检测用户是否操作）
- 如果 foreground 变化 → state="focus_changed"
- 如果窗口数量有变化但 foreground 不变 → state="passive"
- 否则 → state="active"（用户在用，但没切换窗口）

设计目标：让 VL 配额决策知道"是否值得花一次 VL"
- idle：用户可能在挂机/睡觉 → 跳过 VL
- passive：后台有变化但前台没动 → 低优先级
- active：用户在前台工作 → 中优先级
- focus_changed：用户切换了窗口 → 高优先级（必跑 VL）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("localagent.activity_signal")

_DEFAULT_IDLE_THRESHOLD_MINUTES = 5
_DEFAULT_RAW_DIR = "data/activity/raw"
_DEFAULT_INTENSITY_WINDOW_MINUTES = 60
_DEFAULT_INTENSITY_IDLE_THRESHOLD = 5  # 与 compute_signal 一致：相邻记录间隔 > 5min 视为 idle 段


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _resolve_dir(p: str | Path) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else _project_root() / pp


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning("读取 windows jsonl 失败 %s: %s", path, e)
    return records


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _find_foreground(record: dict) -> str:
    """从一条 windows 记录中找出前台窗口标题

    CollectWindowsAction 会把前台窗口标记为 is_foreground=true。
    兼容旧记录（无此字段）：返回空字符串。
    """
    for w in record.get("windows", []):
        if w.get("is_foreground"):
            return w.get("title", "")
    return ""


def compute_signal(
    raw_dir: str | Path = _DEFAULT_RAW_DIR,
    idle_threshold_minutes: int = _DEFAULT_IDLE_THRESHOLD_MINUTES,
) -> dict:
    """计算当前活动信号

    Args:
        raw_dir: raw 数据目录（windows_YYYYMMDD.jsonl 所在）
        idle_threshold_minutes: 超过这么久无采集记录视为 idle

    Returns:
        信号 dict（见模块 docstring）
    """
    raw_path = _resolve_dir(raw_dir)
    today = datetime.now().strftime("%Y%m%d")
    windows_path = raw_path / f"windows_{today}.jsonl"
    records = _read_jsonl(windows_path)

    if not records:
        return {
            "state": "unknown",
            "focus_changed": False,
            "current_foreground": "",
            "previous_foreground": "",
            "minutes_since_last_record": -1,
            "window_count_delta": 0,
        }

    latest = records[-1]
    prev = records[-2] if len(records) >= 2 else None

    latest_ts = _parse_ts(latest.get("ts", ""))
    minutes_since = int((datetime.now() - latest_ts).total_seconds() / 60) if latest_ts else -1

    current_fg = _find_foreground(latest)
    previous_fg = _find_foreground(prev) if prev else ""
    focus_changed = bool(prev and current_fg and previous_fg and current_fg != previous_fg)

    latest_count = len(latest.get("windows", []))
    prev_count = len(prev.get("windows", [])) if prev else latest_count
    count_delta = latest_count - prev_count

    # 状态判定
    # 优先用 idle_seconds（Win32 GetLastInputInfo，真实用户输入空闲时间）
    # 旧逻辑（minutes_since > threshold）作为兜底：仅后端停止采集时触发
    idle_seconds = latest.get("idle_seconds", -1.0)
    idle_threshold_seconds = idle_threshold_minutes * 60

    if idle_seconds >= 0 and idle_seconds > idle_threshold_seconds:
        state = "idle"
    elif idle_seconds < 0 and minutes_since > idle_threshold_minutes:
        # 兜底：idle_seconds 不可用（旧记录/非 Win32）时用采集间隔
        state = "idle"
    elif focus_changed:
        state = "focus_changed"
    elif count_delta != 0:
        state = "passive"
    elif current_fg:
        state = "active"
    else:
        state = "unknown"

    return {
        "state": state,
        "focus_changed": focus_changed,
        "current_foreground": current_fg,
        "previous_foreground": previous_fg,
        "minutes_since_last_record": minutes_since,
        "window_count_delta": count_delta,
    }


def compute_intensity(
    raw_dir: str | Path = _DEFAULT_RAW_DIR,
    window_minutes: int = _DEFAULT_INTENSITY_WINDOW_MINUTES,
) -> tuple[float, int, int, dict]:
    """计算近 N 分钟活动强度分数（供 VL 配额 pace 加权和 min_interval 动态化使用）

    读 windows_YYYYMMDD.jsonl，对近 window_minutes 分钟的相邻记录对逐一判定 state
    （与 compute_signal 一致的逻辑），统计 focus 变化次数和 state 分布，归一化为 0.0-2.0 分数。

    强度分数映射（基于 focus_per_hour 归一化值）：
    - 高强度（≥10/h）→ base 1.5
    - 中强度（3-9/h）→ base 1.0
    - 低强度（<3/h）→ base 0.5
    state 调整：idle 段占比 >50% → base ×0.7；active+focus 占比 >50% → base ×1.1

    Args:
        raw_dir: raw 数据目录（windows_YYYYMMDD.jsonl 所在）
        window_minutes: 计算窗口（分钟），默认 60

    Returns:
        (score, focus_count_10min, focus_count_1h, state_dist)
        - score: 0.0-2.0 强度分数
        - focus_count_10min: 近 10 分钟 focus 变化次数（供 min_interval 动态化）
        - focus_count_1h: 近 window_minutes 分钟 focus 变化次数
        - state_dist: state 分布 dict {"active":N, "passive":N, "focus_changed":N, "idle":N}
    """
    raw_path = _resolve_dir(raw_dir)
    today = datetime.now().strftime("%Y%m%d")
    windows_path = raw_path / f"windows_{today}.jsonl"
    records = _read_jsonl(windows_path)

    empty_dist = {"active": 0, "passive": 0, "focus_changed": 0, "idle": 0}
    if not records:
        return (0.0, 0, 0, dict(empty_dist))

    now = datetime.now()
    window_start = now - timedelta(minutes=window_minutes)
    ten_min_start = now - timedelta(minutes=10)

    # 筛选近 window_minutes 分钟内的记录（按 ts）
    recent: list[tuple[datetime, dict]] = []
    for r in records:
        ts = _parse_ts(r.get("ts", ""))
        if ts and ts >= window_start:
            recent.append((ts, r))

    if not recent:
        return (0.0, 0, 0, dict(empty_dist))

    state_dist = {"active": 0, "passive": 0, "focus_changed": 0, "idle": 0}
    focus_count_1h = 0
    focus_count_10min = 0

    for i, (ts, r) in enumerate(recent):
        if i == 0:
            # 第一条无 prev，无法判定变化，算 active（有前台窗口在工作）
            state_dist["active"] += 1
            continue

        prev_ts, prev_r = recent[i - 1]
        current_fg = _find_foreground(r)
        previous_fg = _find_foreground(prev_r)
        focus_changed = bool(current_fg and previous_fg and current_fg != previous_fg)

        latest_count = len(r.get("windows", []))
        prev_count = len(prev_r.get("windows", []))
        count_delta = latest_count - prev_count

        # 判定 state（与 compute_signal 一致的逻辑）
        # 优先用 idle_seconds（真实用户输入空闲），旧逻辑（采集间隔）兜底
        minutes_gap = (ts - prev_ts).total_seconds() / 60.0
        idle_sec = r.get("idle_seconds", -1.0)
        idle_threshold_sec = _DEFAULT_INTENSITY_IDLE_THRESHOLD * 60

        if idle_sec >= 0 and idle_sec > idle_threshold_sec:
            state_dist["idle"] += 1
        elif idle_sec < 0 and minutes_gap > _DEFAULT_INTENSITY_IDLE_THRESHOLD:
            # 兜底：idle_seconds 不可用时用采集间隔
            state_dist["idle"] += 1
        elif focus_changed:
            state_dist["focus_changed"] += 1
            focus_count_1h += 1
            # 变化时间点以当前记录 ts 为准（检测到变化的时刻）
            if ts >= ten_min_start:
                focus_count_10min += 1
        elif count_delta != 0:
            state_dist["passive"] += 1
        else:
            state_dist["active"] += 1

    total = sum(state_dist.values())
    if total == 0:
        return (0.0, 0, 0, dict(empty_dist))

    # 归一化 focus_count_1h 到每小时
    focus_per_hour = focus_count_1h * (60.0 / max(1, window_minutes))

    if focus_per_hour >= 10:
        base = 1.5
    elif focus_per_hour >= 3:
        base = 1.0
    else:
        base = 0.5

    # state 调整：idle 段占比高 → 降分；active 段占比高 → 加分（仅对中/高强度生效，低强度保持低分）
    idle_ratio = state_dist["idle"] / total
    active_ratio = (state_dist["active"] + state_dist["focus_changed"]) / total
    if idle_ratio > 0.5:
        base *= 0.7
    elif active_ratio > 0.5 and base >= 1.0:
        base *= 1.1

    score = max(0.0, min(2.0, base))
    return (round(score, 2), focus_count_10min, focus_count_1h, state_dist)
