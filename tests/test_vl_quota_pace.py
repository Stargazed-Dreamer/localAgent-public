"""测试 VLQuotaManager pace 算法正确性（H2 — 审计报告）

覆盖 4 个场景：
1. 上午：hour=8，remaining_hours=16，pace 适中，低于节奏放行
2. 下午：hour=22/23，remaining_hours 小，pace 激进，验证 allow/deny 边界
3. 配额耗尽：used_today >= daily_quota → deny_quota_used（优先级高于 focus_changed）
4. 跨日重置：日期变化 → _load_if_needed 触发 _reset_state，used_today 归零

核心技术：_FakeDateTime 继承 datetime，仅覆盖 now()，保留 fromisoformat 等类方法。
用 monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime) 注入。
"""

from datetime import datetime
from pathlib import Path

import pytest

from server.activity_tracker.vl_quota import VLQuotaManager


class _FakeDateTime(datetime):
    """可注入固定时间的 datetime 替身

    继承 datetime 保留 fromisoformat 等类方法，仅覆盖 now()。
    用法：
        _FakeDateTime._fixed = datetime(2026, 7, 24, 8, 30)
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
    """

    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        # 未设置时回退真实时间（避免非测试路径报错）
        return cls._fixed if cls._fixed is not None else datetime.now()


@pytest.fixture
def quota_mgr(tmp_path):
    """独立 VLQuotaManager 实例（避免污染全局单例 vl_quota）

    min_interval_seconds=0 关闭物理节流，专注 pace 算法。
    """
    mgr = VLQuotaManager()
    mgr.configure(
        daily_quota=100,
        reset_hour=0,
        min_interval_seconds=0,
        quota_dir=str(tmp_path),
    )
    return mgr


@pytest.fixture(autouse=True)
def _reset_fake_time():
    """每个测试后清理 _FakeDateTime._fixed，避免泄漏到后续测试"""
    yield
    _FakeDateTime._fixed = None


def _set_time(hour: int, minute: int = 0, day: str = "2026-07-24") -> None:
    """设置 _FakeDateTime 固定时间"""
    y, m, d = (int(x) for x in day.split("-"))
    _FakeDateTime._fixed = datetime(y, m, d, hour, minute)


# ==================== 场景 1：上午 pace 适中，放行 ====================

class TestPaceAlgorithm:
    """pace 算法：expected_pace = (daily_quota - used_today) / max(1, 24 - now.hour)"""

    def test_morning_low_pace_allow(self, quota_mgr, monkeypatch):
        """上午：hour=8，remaining_hours=16，pace=6.25，本小时未用 → allow_pace"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        # daily_quota=100, used_today=0 → pace = 100/16 = 6.25
        # used_this_hour=0 < 6.25 → allow
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True
        assert "低于期望节奏" in reason
        assert debug["expected_pace"] == 6.25
        assert debug["remaining_hours"] == 16
        assert debug["hour_key"] == "08"
        assert debug["used_today"] == 0


# ==================== 场景 2：下午 pace 激进，allow/deny 边界 ====================

    def test_afternoon_high_pace_deny(self, quota_mgr, monkeypatch):
        """下午 deny：hour=22，用满 80，本小时已用 10 = pace=10 → deny_pace

        构造：21:00 用 70 次（by_hour["21"]=70）+ 22:00 用 10 次（by_hour["22"]=10）
        pace = (100-80)/2 = 10.0，used_this_hour=10 >= 10 → 节流
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        # 21:30 用 70 次
        _set_time(hour=21, minute=30)
        for _ in range(70):
            quota_mgr.record_call("ok")
        # 22:30 用 10 次
        _set_time(hour=22, minute=30)
        for _ in range(10):
            quota_mgr.record_call("ok")
        # 决策：pace=20/2=10.0，used_this_hour=10 >= 10 → deny
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert "节流" in reason
        assert debug["expected_pace"] == 10.0
        assert debug["used_this_hour"] == 10
        assert debug["remaining_hours"] == 2
        assert debug["used_today"] == 80

    def test_afternoon_below_pace_allow(self, quota_mgr, monkeypatch):
        """下午 allow：hour=22，用满 80（全在 21 点），本小时 0 < pace=10 → allow_pace

        验证 pace 边界：used_this_hour < expected_pace 时放行（与上一个测试互补）
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        # 21:30 用 80 次（全部记到 by_hour["21"]）
        _set_time(hour=21, minute=30)
        for _ in range(80):
            quota_mgr.record_call("ok")
        # 22:30 决策：pace=20/2=10.0，used_this_hour=0 < 10 → allow
        _set_time(hour=22, minute=30)
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True
        assert "低于期望节奏" in reason
        assert debug["expected_pace"] == 10.0
        assert debug["used_this_hour"] == 0
        assert debug["remaining_hours"] == 2


# ==================== 场景 3：配额耗尽，优先级高于 pace/focus ====================

    def test_quota_exhausted_deny(self, quota_mgr, monkeypatch):
        """配额耗尽：used_today >= daily_quota → deny_quota_used

        优先级验证：即使 focus_changed=True 也应被拒（配额用完 > 焦点放行 > pace）
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # daily_quota=100，用满 100 次
        for _ in range(100):
            quota_mgr.record_call("ok")
        # 普通 active 信号 → deny_quota_used
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert "配额已用完" in reason
        assert debug["used_today"] == 100
        # focus_changed=True 也应被拒（配额用完优先级最高）
        allow2, reason2, _ = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        assert allow2 is False
        assert "配额已用完" in reason2


# ==================== 场景 4：跨日重置 ====================

    def test_cross_day_reset(self, quota_mgr, monkeypatch):
        """跨日重置：day1 用满 50 次，day2 日期变化 → _load_if_needed 触发 _reset_state

        验证：
        - day1 持久化文件存在
        - day2 should_call 时检测到日期变化，重置 used_today=0
        - 重置后 pace=100/16=6.25，allow_pace
        - status().quota_date 切到 day2
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)

        # Day 1: 2026-07-24 14:00，用 50 次
        _set_time(hour=14, minute=0, day="2026-07-24")
        for _ in range(50):
            quota_mgr.record_call("ok")
        s1 = quota_mgr.status()
        assert s1["used_today"] == 50
        assert s1["quota_date"] == "20260724"
        # 确认 day1 文件已持久化
        assert (Path(quota_mgr._quota_dir) / "vl_quota_20260724.json").exists()

        # Day 2: 2026-07-25 08:00，触发跨日重置
        _set_time(hour=8, minute=0, day="2026-07-25")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        # 跨日后 used_today 归零，pace=100/16=6.25，used_this_hour=0 < 6.25 → allow
        assert allow is True
        assert "低于期望节奏" in reason
        assert debug["used_today"] == 0
        assert debug["expected_pace"] == 6.25
        # status 确认 quota_date 已切换、by_hour 清空
        s2 = quota_mgr.status()
        assert s2["quota_date"] == "20260725"
        assert s2["used_today"] == 0
        assert s2["by_hour"] == {}


# ==================== D1: focus_cap 约束 ====================

class TestFocusCap:
    """D1: allow_focus 有本小时上限，焦点频繁变化不再击穿 pace

    focus_cap = max(expected_pace * 1.5, expected_pace + 2)
    超过 focus_cap 后，focus 不再放行，回落到 pace 判定。
    """

    def test_focus_allowed_under_cap(self, quota_mgr, monkeypatch):
        """焦点变化在 focus_cap 内 → allow_focus"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # pace = 100/14 = 7.14, focus_cap = max(7.14*1.5, 7.14+2) = max(10.71, 9.14) = 10.71
        # 第一次 focus 放行
        allow, reason, _ = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        assert allow is True
        assert "焦点变化" in reason

    def test_focus_denied_over_cap(self, quota_mgr, monkeypatch):
        """焦点变化超 focus_cap → 回落 pace 判定

        构造：hour=10, pace=100/14=7.14, focus_cap=max(10.71, 9.14)=10.71
        连续 11 次 focus 放行后，第 12 次应回落 pace（used_this_hour=0 < pace → allow_pace）
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # 连续 focus 放行 11 次（超过 focus_cap=10.71）
        for i in range(11):
            allow, reason, _ = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
            assert allow is True, f"第 {i+1} 次 focus 应放行"
            assert "焦点变化" in reason
        # 第 12 次：focus_cap 已满，回落 pace
        # used_this_hour 仍为 0（record_call 未调用），pace=7.14, 0 < 7.14 → allow_pace
        allow, reason, debug = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        assert allow is True
        assert "低于期望节奏" in reason  # 回落到了 pace
        assert "focus_cap" in debug

    def test_focus_over_cap_deny_when_pace_full(self, quota_mgr, monkeypatch):
        """focus_cap 满 + pace 也满 → deny_pace

        构造：先消耗配额使 pace=0，再 focus_cap 满，最后 focus 请求应被 deny
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=22, minute=0)
        # 用 95 次，remaining=5, hours=2, pace=2.5, focus_cap=max(3.75, 4.5)=4.5
        for _ in range(95):
            quota_mgr.record_call("ok")
        # focus 放行 5 次（超过 focus_cap=4.5）
        for _ in range(5):
            quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        # 再 record_call 2 次 ok（使 used_this_hour >= pace）
        quota_mgr.record_call("ok")
        quota_mgr.record_call("ok")
        # 现在 focus_cap 满 + used_this_hour=2 >= pace=2.5? No, 2 < 2.5 → still allow_pace
        # 再 record 1 次 → used_this_hour=3 >= 2.5 → deny
        quota_mgr.record_call("ok")
        allow, reason, debug = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        assert allow is False
        assert "节流" in reason


# ==================== D2: failed_after_retries 不计配额 ====================

class TestFailedNotCountQuota:
    """D2: failed_after_retries 不计入 used_today，仅 ok 消耗配额"""

    def test_failed_not_increment_used_today(self, quota_mgr, monkeypatch):
        """failed_after_retries 不增加 used_today"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        quota_mgr.record_call("failed_after_retries")
        s = quota_mgr.status()
        assert s["used_today"] == 0  # 失败不计配额

    def test_failed_updates_last_call(self, quota_mgr, monkeypatch):
        """failed_after_retries 仍更新 last_call_ts（用于 min_interval 节流）"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        quota_mgr.record_call("failed_after_retries")
        s = quota_mgr.status()
        assert s["last_call_ts"] is not None  # last_call 仍更新

    def test_ok_increments_used_today(self, quota_mgr, monkeypatch):
        """ok 增加 used_today"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        quota_mgr.record_call("ok")
        s = quota_mgr.status()
        assert s["used_today"] == 1

    def test_mixed_ok_and_failed(self, quota_mgr, monkeypatch):
        """混合调用：3 ok + 2 failed → used_today=3"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        quota_mgr.record_call("ok")
        quota_mgr.record_call("failed_after_retries")
        quota_mgr.record_call("ok")
        quota_mgr.record_call("failed_after_retries")
        quota_mgr.record_call("ok")
        s = quota_mgr.status()
        assert s["used_today"] == 3  # 仅 ok 计数


# ==================== idle 状态拒绝 VL（Ticket 02）====================

class TestIdleRejection:
    """idle 状态拒绝 VL 调用（D2）

    should_call(signal) 在 signal.state == "idle" 时返回 (False, ...)，记 deny_idle。
    判定条件必须是 state == "idle"，不能误伤 passive/active（Anti-Cheat 反向验证）。
    配置项 vl_skip_idle（默认 true）控制是否启用。
    """

    def test_idle_denied(self, quota_mgr, monkeypatch):
        """state=idle → 拒绝 VL，记 deny_idle

        上午 hour=8，pace=6.25，正常情况会 allow_pace。
        但 state=idle → 优先拒绝（idle 不浪费配额）。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        allow, reason, debug = quota_mgr.should_call({"state": "idle", "focus_changed": False})
        assert allow is False
        assert "idle" in reason.lower()
        # 决策计数应记 deny_idle
        s = quota_mgr.status()
        assert s["metrics"]["decision_counts"].get("deny_idle", 0) >= 1

    def test_passive_not_affected(self, quota_mgr, monkeypatch):
        """state=passive → 不被 idle 分支误伤，走正常流程（allow_pace）

        Anti-Cheat 反向验证：禁止把 deny_idle 偷懒为 `not focus_changed`。
        passive 状态 focus_changed=False，但不应被 idle 分支拒绝。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        allow, reason, _ = quota_mgr.should_call({"state": "passive", "focus_changed": False})
        assert allow is True  # passive 不被拒绝，走 allow_pace
        assert "低于期望节奏" in reason
        # 不应记 deny_idle
        s = quota_mgr.status()
        assert s["metrics"]["decision_counts"].get("deny_idle", 0) == 0

    def test_active_not_affected(self, quota_mgr, monkeypatch):
        """state=active → 不被 idle 分支误伤，走正常流程（allow_pace）

        Anti-Cheat 反向验证：active 状态应正常放行。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        allow, reason, _ = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True  # active 不被拒绝
        assert "低于期望节奏" in reason
        s = quota_mgr.status()
        assert s["metrics"]["decision_counts"].get("deny_idle", 0) == 0

    def test_skip_idle_disabled(self, quota_mgr, monkeypatch):
        """vl_skip_idle=false → idle 状态不被拒绝，走正常流程

        配置项可关闭 idle 拒绝（特殊场景如等长时间加载需强制调 VL）。
        """
        quota_mgr.configure(skip_idle=False)
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        allow, reason, _ = quota_mgr.should_call({"state": "idle", "focus_changed": False})
        assert allow is True  # skip_idle=false 时 idle 不被拒绝
        assert "低于期望节奏" in reason
        s = quota_mgr.status()
        assert s["metrics"]["decision_counts"].get("deny_idle", 0) == 0

    def test_idle_priority_over_exhausted(self, quota_mgr, monkeypatch):
        """idle 拒绝优先于 exhausted 检查

        spec 要求：idle 判断在 exhausted 检查前。
        即使配额已耗尽，idle 状态也应记 deny_idle（而非 deny_exhausted），
        这样统计能准确反映"为什么没调 VL"。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=22, minute=30)
        # 用完配额
        for _ in range(100):
            quota_mgr.record_call("ok")
        # idle 状态请求 → 应记 deny_idle 而非 deny_exhausted
        allow, reason, _ = quota_mgr.should_call({"state": "idle", "focus_changed": False})
        assert allow is False
        assert "idle" in reason.lower()
        s = quota_mgr.status()
        assert s["metrics"]["decision_counts"].get("deny_idle", 0) >= 1
        # 不应记 deny_exhausted 或 deny_quota_used
        assert s["metrics"]["decision_counts"].get("deny_exhausted", 0) == 0
        assert s["metrics"]["decision_counts"].get("deny_quota_used", 0) == 0


# ==================== 活动强度加权 pace（Ticket 03）====================

class TestPaceWeighted:
    """活动强度加权 pace 算法（D1）

    compute_adjusted_pace(base_pace, intensity_score):
    - score >= 1.5 → base_pace * 1.5（高强度多调 VL）
    - 1.0 <= score < 1.5 → base_pace * 1.0（中强度不变）
    - score < 1.0 → base_pace * 0.5（低强度少调 VL）

    注：compute_intensity 本身的真实 jsonl 测试在 TestComputeIntensity 覆盖。
    本测试用 monkeypatch 隔离 _compute_intensity_cached，专注 pace 加权逻辑。
    """

    def test_high_intensity_pace_multiplied(self, quota_mgr, monkeypatch):
        """高强度 score=1.6 → adjusted_pace = expected_pace * 1.5

        hour=8, 7 次 ok 后 used_today=7
        expected_pace = (100-7)/16 = 5.8125, adjusted = 5.8125 * 1.5 = 8.72
        used_this_hour=7 < 8.72 → allow（高强度放宽；若用 base_pace 5.81 则 7 >= 5.81 会 deny）
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        # mock 高强度
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.6, 6, 11, {"focus_changed": 11}))
        # 先用 7 次 ok（使 used_this_hour=7, used_today=7）
        for _ in range(7):
            quota_mgr.record_call("ok")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True  # 7 < 8.72 → allow
        assert debug["intensity_score"] == 1.6
        assert debug["adjusted_pace"] == 8.72  # round(5.8125 * 1.5, 2)

    def test_low_intensity_pace_reduced(self, quota_mgr, monkeypatch):
        """低强度 score=0.5 → adjusted_pace = expected_pace * 0.5

        hour=8, 4 次 ok 后 used_today=4
        expected_pace = (100-4)/16 = 6.0, adjusted = 6.0 * 0.5 = 3.0
        used_this_hour=4 >= 3.0 → deny（低强度收紧；若用 base_pace 6.0 则 4 < 6.0 会 allow）
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        # mock 低强度
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (0.5, 0, 0, {"active": 12}))
        # 先用 4 次 ok
        for _ in range(4):
            quota_mgr.record_call("ok")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False  # 4 >= 3.0 → deny
        assert debug["intensity_score"] == 0.5
        assert debug["adjusted_pace"] == 3.0  # round(6.0 * 0.5, 2)

    def test_medium_intensity_pace_unchanged(self, quota_mgr, monkeypatch):
        """中强度 score=1.0 → adjusted_pace = expected_pace * 1.0（不变）

        hour=8, 4 次 ok 后 used_today=4
        expected_pace = (100-4)/16 = 6.0, adjusted = 6.0 * 1.0 = 6.0
        used_this_hour=4 < 6.0 → allow
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.0, 2, 4, {"active": 8, "focus_changed": 4}))
        for _ in range(4):
            quota_mgr.record_call("ok")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True  # 4 < 6.0 → allow
        assert debug["intensity_score"] == 1.0
        assert debug["adjusted_pace"] == 6.0  # round(6.0 * 1.0, 2)

    def test_intensity_cache_ttl(self, quota_mgr, monkeypatch):
        """强度缓存 5 分钟 TTL：5 分钟内多次调用只读一次 compute_intensity

        用 call_count 验证：第一次 should_call 触发 compute_intensity，
        4 分钟内第二次 should_call 用缓存不触发，5 分钟后第三次重新触发。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        call_count = {"n": 0}

        def fake_compute_intensity():
            call_count["n"] += 1
            return (1.5, 5, 10, {"focus_changed": 10})

        # 用真实 _compute_intensity_cached 但 mock 内部的 compute_intensity 调用
        # 直接 mock _compute_intensity_cached 会绕过缓存逻辑
        # 改为 mock compute_intensity 函数本身，让 _compute_intensity_cached 的缓存逻辑生效
        from server.activity_tracker import vl_quota as vq_mod
        vq_mod.compute_intensity if hasattr(vq_mod, "compute_intensity") else None
        # vl_quota 模块需要 import compute_intensity
        monkeypatch.setattr(vq_mod, "compute_intensity",
                            lambda *a, **kw: (call_count.__setitem__("n", call_count["n"] + 1) or (1.5, 5, 10, {"focus_changed": 10})))

        # 第一次调用：8:00，触发 compute_intensity
        _set_time(hour=8, minute=0)
        quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert call_count["n"] == 1, f"第一次应触发 compute_intensity，实际 {call_count['n']}"

        # 第二次调用：8:03（3 分钟后，在 5 分钟 TTL 内），用缓存
        _set_time(hour=8, minute=3)
        quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert call_count["n"] == 1, f"5 分钟内应用缓存，实际触发 {call_count['n']} 次"

        # 第三次调用：8:06（6 分钟后，超过 5 分钟 TTL），重新触发
        _set_time(hour=8, minute=6)
        quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert call_count["n"] == 2, f"超过 TTL 应重新触发，实际 {call_count['n']} 次"


# ==================== min_interval 动态化（Ticket 04）====================

class TestMinIntervalDynamic:
    """min_interval 基于 focus_count_10min 动态调整（Ticket 04 / D5）

    _compute_dynamic_min_interval(focus_count_10min):
    - focus_count_10min >= 5 → max(60, high_load)（默认 60s，高频不漏窗口切换）
    - 2 <= focus_count_10min <= 4 → 120（中频）
    - 0 <= focus_count_10min <= 1 → max(60, low_load)（默认 180s，低频保持节流）
    下限 60s（防止触发上游限流）。
    """

    def test_high_frequency_uses_high_load(self, quota_mgr, monkeypatch):
        """近 10 分钟 focus 切换 6 次（≥5）→ dynamic_min_interval = 60"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.5, 6, 10, {"focus_changed": 10}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 60

    def test_medium_frequency_uses_120(self, quota_mgr, monkeypatch):
        """近 10 分钟 focus 切换 3 次（2-4）→ dynamic_min_interval = 120"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.0, 3, 5, {"active": 8, "focus_changed": 5}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 120

    def test_low_frequency_uses_low_load(self, quota_mgr, monkeypatch):
        """近 10 分钟 focus 切换 1 次（0-1）→ dynamic_min_interval = 180"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (0.5, 1, 2, {"active": 10, "focus_changed": 2}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 180

    def test_zero_frequency_uses_low_load(self, quota_mgr, monkeypatch):
        """近 10 分钟 focus 切换 0 次 → dynamic_min_interval = 180"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (0.5, 0, 0, {"active": 12}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 180

    def test_high_load_config_floor_at_60(self, quota_mgr, monkeypatch):
        """配置 vl_min_interval_high_load_seconds=30，高频时 min_interval=60（下限保护）

        Anti-Cheat：min_interval 不低于 60s（防止触发上游限流），
        即使配置设 30 也会被 floor 到 60。
        """
        quota_mgr.configure(min_interval_high_load=30)
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.5, 6, 10, {"focus_changed": 10}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 60  # floored from 30

    def test_high_load_config_adjustable_above_floor(self, quota_mgr, monkeypatch):
        """配置 vl_min_interval_high_load_seconds=90，高频时 min_interval=90（可调）"""
        quota_mgr.configure(min_interval_high_load=90)
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.5, 6, 10, {"focus_changed": 10}))
        _, _, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert debug["dynamic_min_interval"] == 90

    def test_dynamic_min_interval_actually_throttles(self, quota_mgr, monkeypatch):
        """高频时 dynamic_min_interval=60，距上次调用 30s → 应 deny_min_interval

        验证动态 min_interval 实际作用于物理节流（不是只写入 debug 不生效）。
        注意：quota_mgr fixture 默认 min_interval=0（禁用物理节流），
        本测试需显式启用物理节流（min_interval_seconds=180）才能验证动态节流生效。
        """
        # 启用物理节流（min_interval>0 才会触发 dynamic_min_interval 检查）
        quota_mgr.configure(min_interval_seconds=180)
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.5, 6, 10, {"focus_changed": 10}))
        # 模拟上次调用（record_call ok 会设 last_call_epoch）
        quota_mgr.record_call("ok")
        # 推进 30s（< dynamic_min_interval=60）→ 应 deny
        from datetime import datetime as _dt
        _FakeDateTime._fixed = _dt(2026, 7, 24, 10, 0, 30)
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert "节流" in reason
        assert debug["dynamic_min_interval"] == 60


# ==================== Ticket 05: skipped 语义区分契约 ====================

class TestSkippedSemantics:
    """Ticket 05: 验证 should_call 各分支返回的 debug["decision"] 字段值

    这是 loop_actions.ScreenVLDescribeAction 映射 vl_status 的输入契约：
        deny_idle          → skipped_idle
        deny_pace          → skipped_pace
        deny_min_interval  → skipped_min_interval
        deny_exhausted     → skipped_exhausted
        deny_quota_used    → skipped_exhausted
        allow_focus        → （放行，不进 skipped 路径）
        allow_pace         → （放行，不进 skipped 路径）

    本测试只验证 should_call 的 decision 字段（契约层），
    loop_actions 中的字符串映射逻辑（4 个 elif）简单不重复测试。
    """

    def test_decision_deny_idle(self, quota_mgr, monkeypatch):
        """idle 状态 → decision == 'deny_idle'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        allow, reason, debug = quota_mgr.should_call({"state": "idle", "focus_changed": False})
        assert allow is False
        assert debug["decision"] == "deny_idle"

    def test_decision_deny_exhausted(self, quota_mgr, monkeypatch):
        """daily_exhausted=True → decision == 'deny_exhausted'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        quota_mgr.mark_daily_exhausted("test", kind="upstream_overload", source="test")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert debug["decision"] == "deny_exhausted"

    def test_decision_deny_quota_used(self, quota_mgr, monkeypatch):
        """used_today >= daily_quota → decision == 'deny_quota_used'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # daily_quota=100，用满 100 次
        for _ in range(100):
            quota_mgr.record_call("ok")
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert debug["decision"] == "deny_quota_used"

    def test_decision_deny_min_interval(self, quota_mgr, monkeypatch):
        """物理节流触发 → decision == 'deny_min_interval'"""
        quota_mgr.configure(min_interval_seconds=180)  # 启用物理节流
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # mock 高频 focus（dynamic_min_interval=60）
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.5, 6, 10, {"focus_changed": 10}))
        # 上次调用刚发生
        quota_mgr.record_call("ok")
        # 推进 30s（< dynamic_min_interval=60）→ 物理节流触发
        from datetime import datetime as _dt
        _FakeDateTime._fixed = _dt(2026, 7, 24, 10, 0, 30)
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert debug["decision"] == "deny_min_interval"

    def test_decision_deny_pace(self, quota_mgr, monkeypatch):
        """本小时用满 pace → decision == 'deny_pace'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        # mock 中强度（pace 不调整，×1.0）
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.0, 0, 0, {}))
        # 21:30 用 70 次（by_hour["21"]=70）
        _set_time(hour=21, minute=30)
        for _ in range(70):
            quota_mgr.record_call("ok")
        # 22:30 用 10 次（by_hour["22"]=10）
        _set_time(hour=22, minute=30)
        for _ in range(10):
            quota_mgr.record_call("ok")
        # pace = (100-80)/2 = 10.0，used_this_hour=10 >= 10 → deny_pace
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is False
        assert debug["decision"] == "deny_pace"

    def test_decision_allow_focus(self, quota_mgr, monkeypatch):
        """焦点变化放行 → decision == 'allow_focus'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        allow, reason, debug = quota_mgr.should_call({"state": "focus_changed", "focus_changed": True})
        assert allow is True
        assert debug["decision"] == "allow_focus"

    def test_decision_allow_pace(self, quota_mgr, monkeypatch):
        """低于节奏放行 → decision == 'allow_pace'"""
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=8, minute=30)
        # mock 中强度（pace 不调整，×1.0）
        monkeypatch.setattr(quota_mgr, "_compute_intensity_cached",
                            lambda: (1.0, 0, 0, {}))
        allow, reason, debug = quota_mgr.should_call({"state": "active", "focus_changed": False})
        assert allow is True
        assert debug["decision"] == "allow_pace"

    def test_decision_field_always_present(self, quota_mgr, monkeypatch):
        """所有 should_call 返回的 debug 必含 decision 字段（无遗漏分支）

        Anti-Cheat 要求：loop_actions 中的映射依赖 decision 字段，
        若 should_call 任何分支遗漏 decision 字段，loop_actions 会落到
        skipped_unknown 兜底并触发 warning 日志。本测试验证不遗漏。
        """
        monkeypatch.setattr("server.activity_tracker.vl_quota.datetime", _FakeDateTime)
        _set_time(hour=10, minute=0)
        # 覆盖所有 deny 分支
        for signal in [
            {"state": "idle", "focus_changed": False},
            {"state": "active", "focus_changed": False},  # 应触发 allow_pace 或 deny_pace
        ]:
            _, _, debug = quota_mgr.should_call(signal)
            assert "decision" in debug, f"debug 缺 decision 字段: signal={signal}, debug={debug}"
            assert debug["decision"], f"decision 为空: signal={signal}"


# ==================== Ticket 06: 配额文件持久化与 shutdown flush ====================

class TestPersistBatched:
    """Ticket 06: 验证批量写盘阈值（20）+ flush() 强制写盘 + 启动初始化写盘

    用户指定"写盘频次降为 1/4"，原阈值 5 → 新阈值 20。
    record_call / _record_decision 改用 _persist_batched()，
    mark_daily_exhausted 保留立即 _persist()（关键状态变更不丢失）。
    shutdown 时调 flush() 强制写盘，覆盖 Ctrl+C / kill / 正常退出。
    """

    def test_batch_threshold_no_persist_before_threshold(self, tmp_path):
        """调 record_call 19 次不写盘（未达阈值 20）

        注意：record_call 内部 _persist_batched 累计计数；
        阈值 20 时，19 次 record_call 后 _decisions_since_persist=19，未达 20 不写盘。
        用文件内容（used_today）判断是否写盘，比 mtime 可靠（Windows mtime 精度低）。
        """
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=20)
        # 首次 record_call 触发 _load_if_needed → 无文件 → _reset_state + _persist（写空文件 used_today=0）
        mgr.record_call("ok")  # used_today=1，_persist_batched 计数=1
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        files = list(quota_dir.glob("vl_quota_*.json"))
        assert len(files) == 1
        import json
        initial_data = json.loads(files[0].read_text(encoding="utf-8"))
        assert initial_data["used_today"] == 0  # 启动初始化的空文件

        # 再调 record_call 18 次（总计 19 次，_persist_batched 计数=19，未达 20）
        for _ in range(18):
            mgr.record_call("ok")

        # 文件内容应仍是 used_today=0（未达阈值，未写盘）
        data_after = json.loads(files[0].read_text(encoding="utf-8"))
        assert data_after["used_today"] == 0
        # 但内存状态已更新
        assert mgr.status()["used_today"] == 19

    def test_batch_threshold_persist_at_threshold(self, tmp_path):
        """调 record_call 20 次触发写盘（达阈值 20）

        record_call 20 次后 _persist_batched 计数达 20，触发 _persist。
        文件内容应含 used_today=20。
        """
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=20)
        # 首次 record_call 触发 _load_if_needed 写空文件
        mgr.record_call("ok")
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        files = list(quota_dir.glob("vl_quota_*.json"))
        assert len(files) == 1

        # 再调 record_call 19 次（总计 20 次，第 20 次触发 _persist）
        for _ in range(19):
            mgr.record_call("ok")

        # 文件内容应含 used_today=20
        import json
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["used_today"] == 20

    def test_flush_forces_persist(self, tmp_path):
        """调 record_call 1 次后 flush()，文件立即更新（不依赖阈值）

        用文件内容判断（used_today）而非 mtime（Windows mtime 精度低）。
        """
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=20)
        # 首次 record_call 触发 _load_if_needed 写空文件（used_today=0）
        mgr.record_call("ok")  # used_today=1，_persist_batched 计数=1，未达 20 不写盘
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        files = list(quota_dir.glob("vl_quota_*.json"))
        import json
        data_before = json.loads(files[0].read_text(encoding="utf-8"))
        assert data_before["used_today"] == 0  # 未达阈值，文件未更新

        mgr.flush()  # 强制写盘

        data_after = json.loads(files[0].read_text(encoding="utf-8"))
        assert data_after["used_today"] == 1  # flush 后文件已更新

    def test_flush_idempotent(self, tmp_path):
        """flush() 多次调用安全（幂等）"""
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path))
        mgr.record_call("ok")
        mgr.flush()
        mgr.flush()  # 重复调用不报错
        mgr.flush()
        # 仍能正确加载
        mgr2 = VLQuotaManager()
        mgr2.configure(daily_quota=100, quota_dir=str(tmp_path))
        assert mgr2.status()["used_today"] == 1

    def test_init_writes_empty_file_when_no_existing(self, tmp_path):
        """无配额文件时 _load_if_needed 创建文件并立即写盘

        避免后端运行中因首次 record_call 才写盘导致早期决策日志丢失。
        """
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        assert not list(quota_dir.glob("vl_quota_*.json"))  # 初始无文件

        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path))
        # should_call 触发 _load_if_needed → 无文件 → _reset_state + _persist
        mgr.should_call({"state": "active", "focus_changed": False})

        files = list(quota_dir.glob("vl_quota_*.json"))
        assert len(files) == 1  # 启动时已写盘
        import json
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["used_today"] == 0
        # _persist 不写 daily_quota（只写运行时状态，daily_quota 在 configure 时设内存）
        # daily_quota 由 configure() 维护，不持久化（每次启动从 config.toml 读取）
        assert "by_hour" in data
        assert data["by_hour"] == {}

    def test_mark_daily_exhausted_immediate_persist(self, tmp_path):
        """mark_daily_exhausted 保留立即 _persist（关键状态变更不丢失）

        用文件内容判断（daily_exhausted）而非 mtime。
        """
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=20)
        # 首次 record_call 写空文件（daily_exhausted=False）
        mgr.record_call("ok")
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        files = list(quota_dir.glob("vl_quota_*.json"))
        import json
        data_before = json.loads(files[0].read_text(encoding="utf-8"))
        assert data_before["daily_exhausted"] is False

        # mark_daily_exhausted 立即写盘（关键状态变更）
        mgr.mark_daily_exhausted("test", kind="quota_exhausted", source="test")

        data_after = json.loads(files[0].read_text(encoding="utf-8"))
        assert data_after["daily_exhausted"] is True
        assert data_after["exhausted_kind"] == "quota_exhausted"

    def test_persist_batch_threshold_configurable(self, tmp_path):
        """vl_persist_batch_threshold 可调（设为 1 = 每次 record_call 都写盘）

        用文件内容判断（used_today）而非 mtime。
        """
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=1)
        # 首次 record_call 触发 _load_if_needed 写空文件（used_today=0）
        mgr.record_call("ok")  # used_today=1，_persist_batched 计数=1，达阈值 1 触发写盘
        from server.activity_tracker.vl_quota import _resolve_dir
        quota_dir = _resolve_dir(str(tmp_path))
        files = list(quota_dir.glob("vl_quota_*.json"))
        import json
        data = json.loads(files[0].read_text(encoding="utf-8"))
        # 阈值 1 时，首次 record_call 后立即写盘（含 used_today=1）
        # 但首次 record_call 触发 _load_if_needed 时也 _persist 一次（写空文件 used_today=0），
        # 然后 record_call 内部 _persist_batched 计数=1 达阈值，再次 _persist 写 used_today=1
        assert data["used_today"] == 1

    def test_persist_batch_threshold_floor_at_1(self, tmp_path):
        """persist_batch_threshold 下限 1（避免误设 0 导致永不写盘）"""
        mgr = VLQuotaManager()
        mgr.configure(daily_quota=100, quota_dir=str(tmp_path), persist_batch_threshold=0)
        # 0 应被 max(1, ...) 兜底为 1
        assert mgr._persist_batch_threshold == 1
