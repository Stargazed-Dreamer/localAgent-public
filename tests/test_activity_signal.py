"""测试 activity_signal.compute_signal 状态判定逻辑

覆盖 5 个状态场景：
1. unknown       — 无采集记录
2. idle          — 距上次记录 > idle_threshold_minutes
3. focus_changed — 前台窗口变化（高优先级，VL 必跑）
4. passive       — 窗口数量变化但前台不变
5. active        — 前台不变且窗口不变（用户在工作但没切换）

设计要点：
- 用 tmp_path 隔离 raw_dir，避免污染真实 data/activity/raw
- 记录 ts 用相对 now 的偏移（datetime.now() - timedelta），无需 mock datetime.now()
- 每条记录含 is_foreground 标记，供 _find_foreground 识别前台窗口
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from server.activity_tracker.activity_signal import compute_signal


def _write_windows_jsonl(raw_dir: Path, records: list[dict]) -> None:
    """写入 windows_YYYYMMDD.jsonl（覆盖模式，便于测试隔离）"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = raw_dir / f"windows_{today}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_record(ts: datetime, windows: list[dict]) -> dict:
    """构造一条 windows 采集记录"""
    return {"ts": ts.isoformat(timespec="seconds"), "windows": windows}


def _make_window(title: str, is_foreground: bool = False) -> dict:
    """构造一个窗口条目"""
    return {"title": title, "process": "test", "is_foreground": is_foreground}


class TestComputeSignal:
    """compute_signal 状态判定 5 场景覆盖"""

    def test_unknown_no_records(self, tmp_path):
        """无采集记录 → state=unknown，minutes_since=-1"""
        signal = compute_signal(raw_dir=tmp_path)
        assert signal["state"] == "unknown"
        assert signal["focus_changed"] is False
        assert signal["current_foreground"] == ""
        assert signal["previous_foreground"] == ""
        assert signal["minutes_since_last_record"] == -1
        assert signal["window_count_delta"] == 0

    def test_idle(self, tmp_path):
        """距上次记录 > idle_threshold_minutes → state=idle

        单条记录 10 分钟前，idle_threshold=5 → minutes_since=10 > 5 → idle。
        单条记录无 prev，focus_changed=False，但仍优先判定 idle（时间阈值优先）。
        """
        old_ts = datetime.now() - timedelta(minutes=10)
        _write_windows_jsonl(tmp_path, [
            _make_record(old_ts, [_make_window("AppA", True)])
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        assert signal["state"] == "idle"
        assert signal["minutes_since_last_record"] >= 9  # 容忍 1 分钟边界
        assert signal["focus_changed"] is False
        assert signal["current_foreground"] == "AppA"

    def test_focus_changed(self, tmp_path):
        """前台窗口变化 → state=focus_changed（VL 高优先级放行依据）

        prev 前台 AppA，latest 前台 AppB，minutes_since=1 ≤ 5 不 idle，
        focus_changed=True → state=focus_changed。
        """
        now = datetime.now()
        prev_ts = now - timedelta(minutes=2)
        latest_ts = now - timedelta(minutes=1)
        _write_windows_jsonl(tmp_path, [
            _make_record(prev_ts, [
                _make_window("AppA", True),
                _make_window("AppB"),
            ]),
            _make_record(latest_ts, [
                _make_window("AppA"),
                _make_window("AppB", True),
            ]),
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        assert signal["state"] == "focus_changed"
        assert signal["focus_changed"] is True
        assert signal["current_foreground"] == "AppB"
        assert signal["previous_foreground"] == "AppA"
        assert signal["minutes_since_last_record"] <= 2

    def test_passive(self, tmp_path):
        """窗口数量变化但前台不变 → state=passive

        prev 1 窗口，latest 2 窗口（新增 AppB），前台都是 AppA。
        focus_changed=False，count_delta=1 != 0 → state=passive。
        """
        now = datetime.now()
        prev_ts = now - timedelta(minutes=2)
        latest_ts = now - timedelta(minutes=1)
        _write_windows_jsonl(tmp_path, [
            _make_record(prev_ts, [_make_window("AppA", True)]),
            _make_record(latest_ts, [
                _make_window("AppA", True),
                _make_window("AppB"),
            ]),
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        assert signal["state"] == "passive"
        assert signal["focus_changed"] is False
        assert signal["current_foreground"] == "AppA"
        assert signal["window_count_delta"] == 1

    def test_active(self, tmp_path):
        """前台不变且窗口数量不变 → state=active

        prev 2 窗口前台 AppA，latest 2 窗口前台 AppA。
        minutes_since=1 ≤ 5 不 idle，focus_changed=False，count_delta=0，
        current_fg="AppA" 非空 → state=active。
        """
        now = datetime.now()
        prev_ts = now - timedelta(minutes=2)
        latest_ts = now - timedelta(minutes=1)
        _write_windows_jsonl(tmp_path, [
            _make_record(prev_ts, [
                _make_window("AppA", True),
                _make_window("AppB"),
            ]),
            _make_record(latest_ts, [
                _make_window("AppA", True),
                _make_window("AppB"),
            ]),
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        assert signal["state"] == "active"
        assert signal["focus_changed"] is False
        assert signal["current_foreground"] == "AppA"
        assert signal["window_count_delta"] == 0


class TestComputeSignalEdgeCases:
    """边界场景补充"""

    def test_idle_threshold_boundary(self, tmp_path):
        """idle_threshold 边界：minutes_since 恰好等于阈值不算 idle（> 才算）

        latest 记录 5 分钟前，threshold=5 → minutes_since≈5，不 > 5 → 非 idle。
        若有前台窗口 → active。
        """
        ts = datetime.now() - timedelta(minutes=5)
        _write_windows_jsonl(tmp_path, [
            _make_record(ts, [_make_window("AppA", True)])
        ])
        # 单条记录无 prev，count_delta=0，current_fg 非空 → active
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        # minutes_since 可能是 4 或 5（取决于执行耗时），都不 > 5
        assert signal["state"] != "idle"

    def test_single_record_with_foreground(self, tmp_path):
        """单条记录有前台窗口 → state=active（无 prev 无法判定 focus 变化）

        prev=None → focus_changed=False，count_delta=0（prev_count 取 latest_count），
        current_fg 非空 → active。
        """
        ts = datetime.now() - timedelta(minutes=1)
        _write_windows_jsonl(tmp_path, [
            _make_record(ts, [_make_window("AppA", True)])
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=5)
        assert signal["state"] == "active"
        assert signal["previous_foreground"] == ""

    def test_custom_idle_threshold(self, tmp_path):
        """自定义 idle_threshold：3 分钟即判定 idle"""
        ts = datetime.now() - timedelta(minutes=4)
        _write_windows_jsonl(tmp_path, [
            _make_record(ts, [_make_window("AppA", True)])
        ])
        signal = compute_signal(raw_dir=tmp_path, idle_threshold_minutes=3)
        assert signal["state"] == "idle"


class TestComputeIntensity:
    """compute_intensity 活动强度计算测试

    覆盖高/中/低强度三种场景 + 空 jsonl + focus_count_10min 统计。
    强度分数 0.0-2.0：高≥1.5、中~1.0、低≤0.5。
    """

    def test_empty_records(self, tmp_path):
        """无采集记录 → score=0.0, focus_count 全 0"""
        from server.activity_tracker.activity_signal import compute_intensity
        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=60)
        assert score == 0.0
        assert fc_10 == 0
        assert fc_1h == 0
        assert dist == {} or dist == {"active": 0, "passive": 0, "focus_changed": 0, "idle": 0}

    def test_high_intensity(self, tmp_path):
        """高强度：近 1 小时 focus 切换 ≥10 次 → score ≥ 1.5

        构造 12 条记录（每 5 分钟一条，覆盖 60 分钟），每条前台窗口变化。
        focus_count_1h=11（12 条记录有 11 个相邻对），归一化到每小时 11 ≥ 10 → 高强度。
        """
        from server.activity_tracker.activity_signal import compute_intensity
        now = datetime.now()
        records = []
        # 12 条记录，每 5 分钟一条，前台窗口在 AppA/AppB/AppC 间轮换
        titles = ["AppA", "AppB", "AppC"] * 4
        for i, title in enumerate(titles):
            ts = now - timedelta(minutes=(11 - i) * 5)
            records.append(_make_record(ts, [_make_window(title, True)]))
        _write_windows_jsonl(tmp_path, records)

        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=60)
        # 11 次 focus 变化（每对相邻记录都变化）
        assert fc_1h == 11
        # 归一化到每小时 11 次 ≥ 10 → 高强度
        assert score >= 1.5, f"高强度应 score>=1.5，实际 {score}"
        # state 分布应有 11 个 focus_changed
        assert dist.get("focus_changed", 0) == 11

    def test_medium_intensity(self, tmp_path):
        """中强度：近 1 小时 focus 切换 3-9 次 → score ~1.0

        构造 12 条记录，5 次 focus 变化：A A B B C C D D A A A A
        变化点：A→B(2), B→C(4), C→D(6), D→A(8) = 4 次变化
        归一化到每小时 4 次，在 3-9 范围 → 中强度。
        """
        from server.activity_tracker.activity_signal import compute_intensity
        now = datetime.now()
        titles = ["AppA", "AppA", "AppB", "AppB", "AppC", "AppC",
                  "AppD", "AppD", "AppA", "AppA", "AppA", "AppA"]
        records = []
        for i, title in enumerate(titles):
            ts = now - timedelta(minutes=(11 - i) * 5)
            records.append(_make_record(ts, [_make_window(title, True)]))
        _write_windows_jsonl(tmp_path, records)

        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=60)
        # 4 次变化（A→B, B→C, C→D, D→A）
        assert fc_1h == 4, f"预期 4 次变化，实际 {fc_1h}"
        # 归一化到每小时 4 次，在 3-9 范围 → 中强度
        assert 0.8 <= score <= 1.2, f"中强度应 score~1.0，实际 {score}"

    def test_low_intensity(self, tmp_path):
        """低强度：近 1 小时 focus 切换 <3 次 → score ≤ 0.5

        构造 12 条记录，全部前台 AppA 不变 → 0 次变化。
        """
        from server.activity_tracker.activity_signal import compute_intensity
        now = datetime.now()
        records = []
        for i in range(12):
            ts = now - timedelta(minutes=(11 - i) * 5)
            records.append(_make_record(ts, [_make_window("AppA", True)]))
        _write_windows_jsonl(tmp_path, records)

        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=60)
        assert fc_1h == 0
        # 0 次变化 < 3 → 低强度
        assert score <= 0.5, f"低强度应 score<=0.5，实际 {score}"
        # 全部 active（前台不变且窗口不变）：12 条记录 = 1 个第一条(active) + 11 个后续(active)
        assert dist.get("active", 0) == 12

    def test_focus_count_10min(self, tmp_path):
        """focus_count_10min 只统计近 10 分钟内的 focus 变化

        构造 12 条记录（每 5 分钟一条，覆盖 60 分钟），全部前台变化。
        近 10 分钟 = 最近 2 条记录 = 1 个相邻对 → fc_10=1。
        近 60 分钟 = 12 条记录 = 11 个相邻对 → fc_1h=11。
        """
        from server.activity_tracker.activity_signal import compute_intensity
        now = datetime.now()
        titles = ["AppA", "AppB"] * 6  # 每对都变化
        records = []
        for i, title in enumerate(titles):
            ts = now - timedelta(minutes=(11 - i) * 5)
            records.append(_make_record(ts, [_make_window(title, True)]))
        _write_windows_jsonl(tmp_path, records)

        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=60)
        assert fc_1h == 11
        # 近 10 分钟 = 最近 2 条记录（5 分钟前和现在），1 个相邻对
        # 但第 11 条（ts=now-5min）和第 12 条（ts=now）之间，第 11 条 ts >= now-10min
        # 实际上第 11 条 ts = now - 5min，第 12 条 ts = now，第 11→12 的变化在近 10 分钟内
        # 第 10 条 ts = now - 10min，第 11 条 ts = now - 5min，第 10→11 的变化也在近 10 分钟内（边界）
        # 所以 fc_10 应该是 1 或 2（取决于边界处理）
        assert 1 <= fc_10 <= 2, f"近 10 分钟 focus 变化应为 1-2 次，实际 {fc_10}"

    def test_window_minutes_param(self, tmp_path):
        """window_minutes 参数控制计算窗口

        构造 12 条记录（60 分钟），window_minutes=30 只统计近 30 分钟（6 条记录）。
        """
        from server.activity_tracker.activity_signal import compute_intensity
        now = datetime.now()
        titles = ["AppA", "AppB"] * 6  # 每对都变化
        records = []
        for i, title in enumerate(titles):
            ts = now - timedelta(minutes=(11 - i) * 5)
            records.append(_make_record(ts, [_make_window(title, True)]))
        _write_windows_jsonl(tmp_path, records)

        # window_minutes=30 → 只看近 30 分钟 = 6 条记录（第 7-12 条）
        score, fc_10, fc_1h, dist = compute_intensity(raw_dir=tmp_path, window_minutes=30)
        # 近 30 分钟 6 条记录，5 个相邻对都变化
        assert fc_1h == 5, f"window=30min 应 5 次变化，实际 {fc_1h}"
        # 归一化到每小时：5 * (60/30) = 10 ≥ 10 → 高强度
        assert score >= 1.5, f"归一化后应高强度，实际 {score}"
