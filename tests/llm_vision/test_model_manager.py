"""Model Lifecycle Manager 骨架单测（T1）

不碰真 GPU：全部用 fake 驱动 + 脚本化探测函数（design §14）。

覆盖：
- registry 注册/查重/资源过滤
- 状态机：sustain 计数、临界单样本、恢复、再飙高、暂停/恢复、探测降级保持姿态
- 门控：首样本前拒绝、压力拒绝、冷却（per-model 隔离）、reload_degraded、manual 豁免、enabled=false 透传
- 逐出：选择排序、钉住/保护期跳过、电平式执行、无 victim 维持拒绝
- 观察协议：自主加载代记/清除时间戳
- 手动 load/unload、报告契约
"""

import asyncio
import time

import pytest

from server.model_manager.manager import ModelLifecycleManager
from server.model_manager.monitor import PressureMonitor, ResourceMonitorConfig
from server.model_manager.registry import ModelRegistry
from server.model_manager.types import (
    LoadFailureError,
    ModelUnavailableError,
    PressureState,
)

# ==================== 测试工具 ====================

class FakeDriver:
    """实现 ModelDriver 协议的可控假驱动"""

    def __init__(self, model_id, resource="gpu", footprint=500, priority=50,
                 reload_cost=2.0, evictable=True, loaded=True,
                 loaded_at_age=120.0, load_duration=1234):
        self.model_id = model_id
        self.resource = resource
        self.footprint_mb = footprint
        self.priority = priority
        self.reload_cost_sec = reload_cost
        self.evictable = evictable
        self._loaded = loaded
        self._loaded_at = (time.time() - loaded_at_age) if loaded else None
        self._loaded_at_age_default = loaded_at_age
        self._in_flight = 0
        self._load_duration = load_duration
        self.load_calls = 0
        self.unload_calls = 0
        self.load_should_fail = False

    def is_loaded(self):
        return self._loaded

    def load(self):
        self.load_calls += 1
        if self.load_should_fail:
            raise LoadFailureError("simulated load failure")
        self._loaded = True
        self._loaded_at = time.time()

    def unload(self):
        self.unload_calls += 1
        self._loaded = False
        self._loaded_at = None

    def in_flight(self):
        return self._in_flight

    def loaded_at(self):
        return self._loaded_at

    def load_duration_ms(self):
        return self._load_duration


class ScriptedProbe:
    """按脚本返回 (used, total) 的假探测；耗尽后重复最后一个值"""

    TOTAL = 8 * 1024 ** 3

    def __init__(self, ratios):
        self.ratios = list(ratios)
        self.i = 0

    def __call__(self):
        ratio = self.ratios[self.i] if self.i < len(self.ratios) else self.ratios[-1]
        self.i += 1
        return int(self.TOTAL * ratio), self.TOTAL


class FailingProbe:
    """连续失败的探测（模拟 nvidia-smi 不可用）"""

    def __call__(self):
        raise RuntimeError("nvidia-smi not found")


def make_cfg(enabled=True):
    """构造与 get_model_manager_config() 同形的测试配置"""
    poll = 0.01
    return {
        "enabled": enabled,
        "poll_interval_sec": poll,
        "reload_fail_cooldown_sec": 30,
        "gpu": ResourceMonitorConfig(
            resource="gpu", enabled=True, poll_interval_sec=poll,
            high_watermark=0.90, high_sustain=3, critical_watermark=0.97,
            low_watermark=0.70, low_sustain=6,
        ),
        "cpu": ResourceMonitorConfig(resource="cpu", enabled=False),
        "gpu_min_loaded_seconds": 60,
        "cpu_min_loaded_seconds": 60,
        "gpu_unload_timeout_sec": 5,
        "cpu_unload_timeout_sec": 5,
        "models": {},
    }


def make_manager(probe_ratios=None, probe=None, cfg=None, drivers=()):
    cfg = cfg or make_cfg()
    gpu_probe = probe or ScriptedProbe(probe_ratios or [0.5])
    monitors = {
        "gpu": PressureMonitor(cfg["gpu"], probe=gpu_probe),
        "cpu": PressureMonitor(cfg["cpu"], probe=ScriptedProbe([0.3])),
    }
    m = ModelLifecycleManager(cfg=cfg, monitors=monitors)
    for d in drivers:
        m.registry.register(d)
    return m


def tick_n(m, n):
    async def _run():
        for _ in range(n):
            await m._tick()
    asyncio.run(_run())


# ==================== Registry ====================

def test_registry_register_get_and_duplicate():
    reg = ModelRegistry()
    d = FakeDriver("ocr")
    reg.register(d)
    assert reg.get("ocr") is d
    assert reg.get("nonexistent") is None
    assert len(reg) == 1
    with pytest.raises(ValueError):
        reg.register(FakeDriver("ocr"))


def test_registry_of_resource():
    reg = ModelRegistry()
    reg.register(FakeDriver("ocr", resource="gpu"))
    reg.register(FakeDriver("emb", resource="cpu"))
    assert [d.model_id for d in reg.of_resource("gpu")] == ["ocr"]
    assert [d.model_id for d in reg.of_resource("cpu")] == ["emb"]


# ==================== 状态机 ====================

def _bare_monitor(**over):
    cfg = ResourceMonitorConfig(resource="gpu", **over) if over else \
        ResourceMonitorConfig(resource="gpu")
    return PressureMonitor(cfg, probe=ScriptedProbe([0.5]))


def test_high_sustain_to_refusing():
    mon = _bare_monitor()
    assert mon.observe(0.92) is None          # streak 1
    assert mon.observe(0.93) is None          # streak 2
    ev = mon.observe(0.94)                    # streak 3 → REFUSING
    assert ev and ev["to"] == "refusing" and ev["cause"] == "high_sustain"
    assert mon.state is PressureState.REFUSING


def test_critical_single_sample():
    mon = _bare_monitor()
    ev = mon.observe(0.98)
    assert ev and ev["cause"] == "critical_sample"
    assert mon.state is PressureState.REFUSING


def test_refusing_to_armed_low_sustain():
    mon = _bare_monitor()
    mon.observe(0.98)
    for _ in range(5):
        assert mon.observe(0.6) is None       # low streak 1-5
    ev = mon.observe(0.6)                     # streak 6 → ARMED
    assert ev and ev["to"] == "armed" and ev["cause"] == "low_sustain"
    assert mon.state is PressureState.ARMED


def test_armed_to_refusing_again():
    mon = _bare_monitor()
    mon.observe(0.98)
    for _ in range(6):
        mon.observe(0.6)
    assert mon.state is PressureState.ARMED
    ev = mon.observe(0.99)
    assert ev and ev["to"] == "refusing"


def test_high_streak_resets_on_dip():
    mon = _bare_monitor()
    mon.observe(0.92)
    mon.observe(0.92)
    mon.observe(0.5)                          # 中断，streak 清零
    mon.observe(0.92)
    ev = mon.observe(0.92)
    assert ev is None                         # 只有连续 2 个，不足 3
    assert mon.state is PressureState.NORMAL


def test_low_streak_resets_in_refusing():
    mon = _bare_monitor()
    mon.observe(0.98)
    mon.observe(0.6)
    mon.observe(0.6)
    mon.observe(0.85)                         # 中断
    for _ in range(5):
        assert mon.observe(0.6) is None       # 重新计数 1-5
    ev = mon.observe(0.6)
    assert ev and ev["to"] == "armed"


def test_pause_freezes_and_resume():
    mon = _bare_monitor()
    mon.pause()
    assert mon.state is PressureState.PAUSED
    # 暂停期间高压不迁移（保护态冻结）
    for _ in range(5):
        assert mon.observe(0.95) is None
    assert mon.state is PressureState.PAUSED
    assert mon.admission_allowed() is True    # PAUSED 准入放行
    assert mon.eviction_active() is False     # 且不逐出
    mon.last_used, mon.last_total = int(0.95 * ScriptedProbe.TOTAL), ScriptedProbe.TOTAL
    mon.resume()
    assert mon.state is PressureState.NORMAL  # 0.95 < critical，未立即 REFUSING


def test_resume_with_critical_sample_refuses():
    mon = _bare_monitor()
    mon.pause()
    mon.last_used, mon.last_total = int(0.99 * ScriptedProbe.TOTAL), ScriptedProbe.TOTAL
    mon.resume()
    assert mon.state is PressureState.REFUSING  # 逃生门：恢复瞬间即拒绝


def test_probe_degraded_keeps_refusing_posture():
    mon = _bare_monitor()
    mon.observe(0.98)                         # → REFUSING
    assert mon.on_probe_failure() is None
    assert mon.on_probe_failure() is None
    ev = mon.on_probe_failure()               # 第 3 次 → PROBE_DEGRADED
    assert ev and ev["to"] == "probe_degraded"
    assert mon.admission_allowed() is False   # 保持拒绝姿态
    assert mon.eviction_active() is False     # 降级不主动逐出
    # 探测恢复 → 还原回 REFUSING
    ev = mon.observe(0.92)
    assert mon.state is PressureState.REFUSING


def test_probe_degraded_from_normal_allows():
    mon = _bare_monitor()
    for _ in range(3):
        mon.on_probe_failure()
    assert mon.state is PressureState.PROBE_DEGRADED
    assert mon.admission_allowed() is True    # 降级前是 NORMAL → 放行


def test_force_normal_after_eviction():
    mon = _bare_monitor()
    mon.observe(0.98)
    ev = mon.force_normal_after_eviction()
    assert ev and ev["cause"] == "eviction_complete"
    assert mon.state is PressureState.NORMAL
    # NORMAL 下重复调用无效
    assert mon.force_normal_after_eviction() is None


def test_mark_recovered_only_from_armed():
    mon = _bare_monitor()
    assert mon.mark_recovered() is None       # NORMAL 下无效
    mon.observe(0.98)
    for _ in range(6):
        mon.observe(0.6)
    ev = mon.mark_recovered()
    assert ev and ev["cause"] == "reload_success"
    assert mon.state is PressureState.NORMAL


# ==================== 门控（Gate） ====================

def test_admit_denied_before_first_sample():
    m = make_manager(drivers=[FakeDriver("ocr")])
    with pytest.raises(ModelUnavailableError) as ei:
        m.admit("ocr")
    assert ei.value.reason == "awaiting_first_sample"


def test_admit_denied_when_refusing():
    m = make_manager(probe_ratios=[0.92], drivers=[FakeDriver("ocr")])
    tick_n(m, 3)                              # 3 个高压样本 → REFUSING
    with pytest.raises(ModelUnavailableError) as ei:
        m.admit("ocr")
    assert ei.value.reason == "pressure_refusing"


def test_admit_allowed_normal_and_armed():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    m.admit("ocr")                            # NORMAL：放行（不抛即通过）
    # 手工推到 ARMED 再验证
    m.monitors["gpu"].observe(0.98)
    for _ in range(6):
        m.monitors["gpu"].observe(0.6)
    assert m.monitors["gpu"].state is PressureState.ARMED
    m.admit("ocr")


def test_admit_cooldown_and_manual_bypass():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    m.note_load_failure("ocr", "test")
    allowed, reason = m.check_admission("ocr")
    assert not allowed and reason == "cooldown"
    # 手动加载豁免冷却
    allowed, reason = m.check_admission("ocr", manual=True)
    assert allowed


def test_cooldown_per_model_isolation():
    a = FakeDriver("emb_a", resource="cpu")
    b = FakeDriver("emb_b", resource="cpu")
    cfg = make_cfg()
    cfg["cpu"] = ResourceMonitorConfig(resource="cpu", enabled=True)
    m = make_manager(cfg=cfg, drivers=[a, b])
    tick_n(m, 1)
    m.note_load_failure("emb_a")
    allowed_a, _ = m.check_admission("emb_a")
    allowed_b, _ = m.check_admission("emb_b")
    assert not allowed_a
    assert allowed_b                          # b 不受 a 的冷却牵连


def test_reload_degraded_after_three_failures():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    for _ in range(3):
        m.note_load_failure("ocr", "test")
    m._cooldown_until["ocr"] = 0.0            # 让冷却过期，单独验证降级拒绝
    allowed, reason = m.check_admission("ocr")
    assert not allowed and reason == "reload_degraded"
    # 加载成功清除降级标记
    m.note_load_success("ocr")
    allowed, reason = m.check_admission("ocr")
    assert allowed


def test_admit_unmanaged_model_allowed():
    m = make_manager(probe_ratios=[0.5])
    tick_n(m, 1)
    m.admit("not_registered")                 # 未注册模型不受管（透传放行）


def test_enabled_false_passthrough():
    m = make_manager(probe_ratios=[0.99], cfg=make_cfg(enabled=False),
                     drivers=[FakeDriver("ocr")])
    # 无首样本、无采样也放行
    allowed, reason = m.check_admission("ocr")
    assert allowed and reason == "manager_disabled"


def test_admit_denied_probe_degraded_refusing_posture():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    mon = m.monitors["gpu"]
    mon.observe(0.98)                         # REFUSING
    for _ in range(3):
        mon.on_probe_failure()                # → PROBE_DEGRADED（姿态=拒绝）
    allowed, reason = m.check_admission("ocr")
    assert not allowed and reason == "probe_degraded"


# ==================== 逐出（Evictor） ====================

def test_victim_selection_order():
    # priority 最低者先逐；同 priority 逐占用大者；再同级逐重载便宜者
    hi_prio = FakeDriver("a", priority=90, footprint=900)
    low_prio = FakeDriver("b", priority=10, footprint=100)
    m = make_manager(drivers=[hi_prio, low_prio])
    assert m._select_victim("gpu").model_id == "b"

    same_p = FakeDriver("c", priority=10, footprint=800)
    m.registry.register(same_p)
    assert m._select_victim("gpu").model_id == "c"   # 同 priority，逐大的

    cheap = FakeDriver("d", priority=10, footprint=800, reload_cost=1.0)
    m.registry.register(cheap)
    assert m._select_victim("gpu").model_id == "d"   # 同占用，逐重载便宜的


def test_victim_skips_pinned_and_protected():
    pinned = FakeDriver("pinned", evictable=False, footprint=900, priority=1)
    protected = FakeDriver("fresh", loaded_at_age=10.0)   # < min_loaded_seconds=60
    m = make_manager(drivers=[pinned, protected])
    assert m._select_victim("gpu") is None

    # 配置可覆盖钉住（[model_manager.models.<id>] evictable=true）
    m.cfg["models"]["pinned"] = {"evictable": True}
    assert m._select_victim("gpu").model_id == "pinned"


def test_victim_skips_unloaded_and_cooldown():
    unloaded = FakeDriver("u", loaded=False)
    m = make_manager(drivers=[unloaded])
    assert m._select_victim("gpu") is None

    d = FakeDriver("c")
    m2 = make_manager(drivers=[d])
    m2.note_load_failure("c")
    assert m2._select_victim("gpu") is None   # 冷却期内不逐


def test_eviction_executes_on_refusing():
    d = FakeDriver("ocr", footprint=500)
    m = make_manager(probe_ratios=[0.92], drivers=[d])

    async def _run():
        for _ in range(3):
            await m._tick()                   # 第 3 个样本 → REFUSING + 触发逐出
        assert m.monitors["gpu"].state is PressureState.REFUSING
        await asyncio.sleep(0.2)              # 等逐出任务完成

    asyncio.run(_run())
    assert d.unload_calls == 1
    assert d.is_loaded() is False
    # 逐出完成回 NORMAL
    assert m.monitors["gpu"].state is PressureState.NORMAL


def test_no_victim_keeps_refusing():
    pinned = FakeDriver("pinned", evictable=False)
    m = make_manager(probe_ratios=[0.92], drivers=[pinned])
    tick_n(m, 4)
    assert pinned.unload_calls == 0
    # 无可选 victim → 维持 REFUSING 仅拒绝（design §7）
    assert m.monitors["gpu"].state is PressureState.REFUSING
    allowed, reason = m.check_admission("pinned")
    assert not allowed and reason == "pressure_refusing"


def test_level_triggered_second_victim():
    """首个逐出后压力仍高 → 再次 REFUSING → 逐下一个（电平式，design §7）"""
    first = FakeDriver("first", priority=10, footprint=500)
    second = FakeDriver("second", priority=20, footprint=500)
    m = make_manager(probe_ratios=[0.92], drivers=[first, second])

    async def _run():
        for _ in range(3):
            await m._tick()
        await asyncio.sleep(0.2)              # first 被逐，状态回 NORMAL
        assert first.unload_calls == 1
        assert m.monitors["gpu"].state is PressureState.NORMAL
        # 压力仍高：再 3 个样本 → REFUSING → 逐 second
        for _ in range(3):
            await m._tick()
        await asyncio.sleep(0.2)

    asyncio.run(_run())
    assert second.unload_calls == 1


# ==================== 观察协议 ====================

def test_observed_loaded_at_for_autonomous_load():
    class SelfLoadDriver(FakeDriver):
        def loaded_at(self):
            return None                       # 模块不记录时间戳（自主加载）

    d = SelfLoadDriver("ocr", loaded=True, loaded_at_age=0)
    m = make_manager(probe_ratios=[0.5], drivers=[d])
    assert m.effective_loaded_at("ocr") is None
    tick_n(m, 1)                              # 观察周期代记时间戳
    ts = m.effective_loaded_at("ocr")
    assert ts is not None and time.time() - ts < 5


def test_observed_cleared_on_unload():
    class SelfLoadDriver(FakeDriver):
        def loaded_at(self):
            return None

    d = SelfLoadDriver("ocr", loaded=True)
    m = make_manager(probe_ratios=[0.5], drivers=[d])
    tick_n(m, 1)
    assert m.effective_loaded_at("ocr") is not None
    d._loaded = False
    tick_n(m, 1)
    assert m.effective_loaded_at("ocr") is None


# ==================== 手动控制 ====================

def test_manual_load_success_and_failure():
    d = FakeDriver("ocr", loaded=False)
    m = make_manager(probe_ratios=[0.5], drivers=[d])
    tick_n(m, 1)

    result = asyncio.run(m.manual_load("ocr"))
    assert result["status"] == "ok" and d.load_calls == 1

    d.load_should_fail = True
    result = asyncio.run(m.manual_load("ocr"))
    assert result["status"] == "error" and result["fail_count"] == 1
    assert m._in_cooldown("ocr")

    # 压力拒绝时手动加载也被拒（强制放行需先 pause）
    m2 = make_manager(probe_ratios=[0.92], drivers=[FakeDriver("x", loaded=False)])
    tick_n(m2, 3)
    result = asyncio.run(m2.manual_load("x"))
    assert result["status"] == "refused" and result["reason"] == "pressure_refusing"


def test_manual_load_bypasses_cooldown():
    d = FakeDriver("ocr", loaded=False)
    m = make_manager(probe_ratios=[0.5], drivers=[d])
    tick_n(m, 1)
    m.note_load_failure("ocr")
    result = asyncio.run(m.manual_load("ocr"))
    assert result["status"] == "ok"           # manual 豁免冷却


def test_manual_unload():
    d = FakeDriver("ocr")
    m = make_manager(drivers=[d])
    result = asyncio.run(m.manual_unload("ocr"))
    assert result["status"] == "ok" and d.unload_calls == 1
    # 已卸载 → 幂等
    result = asyncio.run(m.manual_unload("ocr"))
    assert result["status"] == "ok"
    assert d.unload_calls == 1
    # 未知模型
    result = asyncio.run(m.manual_unload("nope"))
    assert result["status"] == "error"


def test_pause_resume_endpoints_semantics():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    m.pause()
    assert m.monitors["gpu"].state is PressureState.PAUSED
    # PAUSED 期间高压不逐出、准入放行
    for _ in range(5):
        m.monitors["gpu"].observe(0.95)
    assert m.check_admission("ocr")[0] is True
    m.resume()
    assert m.monitors["gpu"].state is PressureState.NORMAL


# ==================== 报告契约 ====================

def test_models_report_contract():
    d = FakeDriver("ocr", load_duration=None)
    m = make_manager(probe_ratios=[0.5], drivers=[d])
    tick_n(m, 1)
    rows = m.models_report()
    assert len(rows) == 1
    row = rows[0]
    for key in ("model_id", "resource", "loaded", "footprint_mb", "priority",
                "evictable", "in_flight", "loaded_at", "last_load_ms",
                "reload_degraded"):
        assert key in row
    assert row["last_load_ms"] is None        # 可空契约（design §5.5）
    assert row["loaded"] is True


def test_health_summary_low_freq_fields():
    m = make_manager(probe_ratios=[0.5], drivers=[FakeDriver("ocr")])
    tick_n(m, 1)
    s = m.health_summary()
    assert set(s) == {"enabled", "gpu_state", "cpu_state", "loaded_ids",
                      "reload_degraded_ids", "last_transition_ts"}
    assert s["gpu_state"] == "normal"
    assert s["loaded_ids"] == ["ocr"]


def test_pressure_report_shape():
    m = make_manager(probe_ratios=[0.5])
    tick_n(m, 1)
    rep = m.pressure_report()
    assert "gpu" in rep and "cpu" in rep
    assert rep["gpu"]["state"] == "normal"
    assert rep["gpu"]["ratio"] is not None
    assert isinstance(rep["gpu"]["recent_events"], list)
