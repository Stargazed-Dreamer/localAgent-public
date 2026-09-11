"""OCR GPU→CPU 条件降级测试（2026-08-31，交接文档 temp/handover_ocr_cpu_fallback.md §七）

覆盖：
- _decide_device：GPU REFUSING + 资源达标 → 降级 "cpu"；资源不足 → 维持拒绝（"gpu"）
- 只对 pressure_refusing（GPU monitor REFUSING 态）降级；cooldown 等模型自身状态不触发
- 迟滞恢复：保持期未过 / GPU 水位未回落到 low_watermark → 延续 "cpu"；
  两条件同时满足 → 回 "gpu"
- _can_fallback_to_cpu：psutil 阈值判定（内存 / CPU 两维）
- 已降级时 _select_victim("gpu") 不包含 OCR（防 GPU 逐出器死循环）
- unload_ocr 后 _active_device 复位为 config 期望设备（迟滞状态有意保留）
- get_ocr 集成：降级决策先行 → admit 经 cpu monitor 放行 → 引擎以 device="cpu" 创建
"""

import sys
import time
import types

import pytest

from server.model_manager.monitor import ResourceMonitorConfig
from server.model_manager.types import PressureState

# ==================== 测试辅助 ====================

_FALLBACK_CFG = {
    "paddle_device": "gpu",
    "gpu_fallback_min_avail_gb": 2.0,
    "gpu_fallback_max_cpu_pct": 70.0,
    "gpu_fallback_hold_sec": 300.0,
    "paddleocr_dir": "weights/paddlex",  # ModelManager.__init__ 需要
}


class StubGpuMonitor:
    """伪造 GPU monitor：直接给定状态与水位（不经采样循环）"""

    def __init__(self, state=PressureState.NORMAL, ratio=None, enabled=True,
                 low_watermark=0.70):
        self.cfg = ResourceMonitorConfig(resource="gpu", enabled=enabled,
                                         low_watermark=low_watermark)
        self.state = state
        self._ratio = ratio

    @property
    def ratio(self):
        return self._ratio


class StubManager:
    """伪造 ModelLifecycleManager：只暴露 _decide_device 消费的接口"""

    def __init__(self, gpu_mon=None, enabled=True):
        self.enabled = enabled
        self.monitors = {"gpu": gpu_mon} if gpu_mon is not None else {}


def _make_manager(monkeypatch, cfg=None):
    """构造 ModelManager，config 指向降级测试参数（默认 GPU 意图）"""
    from server.ocr import ModelManager

    monkeypatch.setattr("server.config.get_models_config", lambda: dict(cfg or _FALLBACK_CFG))
    return ModelManager()


def _as_gpu_intent(m, monkeypatch):
    """_detect_paddle_device 归并为 GPU 意图（绕开真实 paddle/auto 检测）"""
    monkeypatch.setattr(m, "_detect_paddle_device", lambda: "gpu")


# ==================== _decide_device 决策 ====================

class TestDecideDeviceFallback:
    """GPU REFUSING 时的降级判定"""

    def test_gpu_refusing_and_resources_ok_falls_back(self, monkeypatch):
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        monkeypatch.setattr(m, "_can_fallback_to_cpu", lambda: True)
        mgr = StubManager(StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95))
        assert m._decide_device(mgr) == "cpu"
        assert m._active_device == "cpu"
        assert m._fallback_active is True
        assert m._fallback_hold_until >= time.time() + 299  # 保持期已设置

    def test_gpu_refusing_but_resources_insufficient_keeps_refusal(self, monkeypatch):
        """内存不足 → 不降级（维持拒绝，admit 会抛 pressure_refusing）"""
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        monkeypatch.setattr(m, "_can_fallback_to_cpu", lambda: False)
        mgr = StubManager(StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95))
        assert m._decide_device(mgr) == "gpu"
        assert m._active_device == "gpu"
        assert m._fallback_active is False

    def test_gpu_normal_no_episode_returns_gpu(self, monkeypatch):
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=0.5))
        assert m._decide_device(mgr) == "gpu"
        assert m._fallback_active is False

    def test_cooldown_alone_does_not_trigger_fallback(self, monkeypatch):
        """cooldown/reload_degraded 是模型自身状态：GPU 未 REFUSING 就不降级
        （这些拒绝由 admit() 原样抛出，换设备解决不了）"""
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=0.6))
        assert m._decide_device(mgr) == "gpu"
        assert m._fallback_active is False

    def test_config_cpu_bypasses_pressure_logic(self, monkeypatch):
        """config 明确 cpu 时完全跳过压力逻辑（manager 可为任意对象）"""
        cfg = dict(_FALLBACK_CFG, paddle_device="cpu")
        m = _make_manager(monkeypatch, cfg=cfg)
        assert m._decide_device(StubManager()) == "cpu"
        assert m._active_device == "cpu"

    def test_manager_disabled_returns_gpu(self, monkeypatch):
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        assert m._decide_device(StubManager(enabled=False)) == "gpu"

    def test_gpu_monitor_disabled_returns_gpu(self, monkeypatch):
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        mgr = StubManager(StubGpuMonitor(enabled=False, ratio=0.99))
        assert m._decide_device(mgr) == "gpu"

    def test_no_gpu_monitor_returns_gpu(self, monkeypatch):
        """管理器无 monitors（如测试桩 / 未注册）→ 不降级，走原门控语义"""
        m = _make_manager(monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        assert m._decide_device(StubManager()) == "gpu"


class TestDecideDeviceHysteresis:
    """迟滞恢复：防水位临界反复横跳"""

    def _episode(self, m, monkeypatch):
        m._fallback_active = True
        m._active_device = "cpu"

    def test_stays_cpu_while_ratio_high(self, monkeypatch):
        """保持期已过但 GPU 水位仍在 low_watermark 之上 → 延续 cpu"""
        m = _make_manager(monkeypatch)
        self._episode(m, monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        m._fallback_hold_until = time.time() - 1  # 保持期已过
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=0.85))
        assert m._decide_device(mgr) == "cpu"

    def test_stays_cpu_during_hold(self, monkeypatch):
        """水位已回落但保持期未过 → 延续 cpu"""
        m = _make_manager(monkeypatch)
        self._episode(m, monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        m._fallback_hold_until = time.time() + 120
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=0.50))
        assert m._decide_device(mgr) == "cpu"

    def test_recovers_after_hold_and_low_ratio(self, monkeypatch):
        """保持期已过 + 水位 < low_watermark → 回 gpu，episode 结束"""
        m = _make_manager(monkeypatch)
        self._episode(m, monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        m._fallback_hold_until = time.time() - 1
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=0.50))
        assert m._decide_device(mgr) == "gpu"
        assert m._fallback_active is False
        assert m._active_device == "gpu"

    def test_episode_survives_while_gpu_refusing_again(self, monkeypatch):
        """episode 延续期间 GPU 再次 REFUSING + 资源达标 → 继续降级（刷新保持期）"""
        m = _make_manager(monkeypatch)
        self._episode(m, monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        monkeypatch.setattr(m, "_can_fallback_to_cpu", lambda: True)
        mgr = StubManager(StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95))
        assert m._decide_device(mgr) == "cpu"
        assert m._fallback_hold_until >= time.time() + 299

    def test_episode_ratio_unknown_stays_cpu(self, monkeypatch):
        """水位数据缺失（ratio=None）→ 保守延续 cpu（不满足恢复条件）"""
        m = _make_manager(monkeypatch)
        self._episode(m, monkeypatch)
        _as_gpu_intent(m, monkeypatch)
        m._fallback_hold_until = time.time() - 1
        mgr = StubManager(StubGpuMonitor(state=PressureState.NORMAL, ratio=None))
        assert m._decide_device(mgr) == "cpu"


# ==================== _can_fallback_to_cpu 阈值判定 ====================

class TestCanFallbackToCpu:
    def _patch_psutil(self, monkeypatch, avail_gb, cpu_pct):
        import psutil
        monkeypatch.setattr(psutil, "virtual_memory",
                            lambda: types.SimpleNamespace(available=int(avail_gb * 1024**3)))
        monkeypatch.setattr(psutil, "cpu_percent", lambda interval=0.5: cpu_pct)

    def test_pass_when_both_within_thresholds(self, monkeypatch):
        m = _make_manager(monkeypatch)
        self._patch_psutil(monkeypatch, avail_gb=14.5, cpu_pct=23.0)
        assert m._can_fallback_to_cpu() is True

    def test_fail_when_memory_low(self, monkeypatch):
        """实测游戏满载 0.46GB：降级必然 swap/OOM，必须拒绝"""
        m = _make_manager(monkeypatch)
        self._patch_psutil(monkeypatch, avail_gb=0.46, cpu_pct=23.0)
        assert m._can_fallback_to_cpu() is False

    def test_fail_when_cpu_high(self, monkeypatch):
        m = _make_manager(monkeypatch)
        self._patch_psutil(monkeypatch, avail_gb=14.5, cpu_pct=85.0)
        assert m._can_fallback_to_cpu() is False

    def test_thresholds_from_config(self, monkeypatch):
        cfg = dict(_FALLBACK_CFG, gpu_fallback_min_avail_gb=8.0, gpu_fallback_max_cpu_pct=50.0)
        m = _make_manager(monkeypatch, cfg=cfg)
        self._patch_psutil(monkeypatch, avail_gb=5.0, cpu_pct=45.0)
        assert m._can_fallback_to_cpu() is False  # 内存 5GB < 配置阈值 8GB


# ==================== 卸载复位 / 迟滞保留 ====================

class TestUnloadResetsDevice:
    def test_unload_resets_active_device_keeps_hysteresis(self, monkeypatch):
        """卸载后 _active_device 复位为 config 期望设备；迟滞状态跨卸载保留"""

        m = _make_manager(monkeypatch)
        monkeypatch.setitem(sys.modules, "paddle", types.SimpleNamespace(
            is_compiled_with_cuda=lambda: False,
        ))
        m._ocr_engine = object()  # 假引擎，走主卸载路径
        m._active_device = "cpu"
        m._fallback_active = True
        m._fallback_hold_until = time.time() + 100
        result = m.unload_ocr(drain_timeout=0.1)
        assert result["unloaded"] is True
        assert m._active_device == "gpu"  # 复位（config paddle_device=gpu）
        assert m._fallback_active is True  # 迟滞有意保留
        assert m._fallback_hold_until > time.time()

    def test_init_device_follows_config(self, monkeypatch):
        cfg = dict(_FALLBACK_CFG, paddle_device="cpu")
        m = _make_manager(monkeypatch, cfg=cfg)
        assert m.active_device == "cpu"


# ==================== 逐出器隔离 ====================

class TestVictimSelection:
    def test_degraded_ocr_excluded_from_gpu_victims(self, monkeypatch):
        """已降级（_active_device=cpu）时 _select_victim("gpu") 不包含 OCR——
        否则死循环：卸载 CPU 版释放不了显存 → 水位不降 → 继续 refusing → 再降级 → 再被逐出"""
        from server.model_manager.config import get_model_manager_config
        from server.model_manager.drivers import OcrDriver
        from server.model_manager.manager import ModelLifecycleManager
        from server.model_manager.registry import ModelRegistry

        registry = ModelRegistry()
        registry.register(OcrDriver())
        # monitor 注入空集（disabled），避免采样循环干扰
        monitors = {
            "gpu": StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95),
            "cpu": StubGpuMonitor(state=PressureState.NORMAL, ratio=0.5, enabled=False),
        }
        mgr = ModelLifecycleManager(cfg=get_model_manager_config(),
                                    registry=registry, monitors=monitors)
        driver = registry.get("ocr")
        monkeypatch.setattr(OcrDriver, "is_loaded", lambda self: True)

        # 未降级（GPU）→ 是候选
        monkeypatch.setattr("server.ocr.models._active_device", "gpu", raising=False)
        assert driver.resource == "gpu"
        assert mgr._select_victim("gpu") is driver

        # 已降级（CPU）→ 不在 gpu 候选，victim 为 None（维持拒绝而非误逐出）
        monkeypatch.setattr("server.ocr.models._active_device", "cpu", raising=False)
        assert driver.resource == "cpu"
        assert registry.of_resource("gpu") == []
        assert mgr._select_victim("gpu") is None


# ==================== get_ocr 集成（降级路径） ====================

class TestGetOcrFallbackIntegration:
    def test_fallback_loads_on_cpu_through_admission(self, monkeypatch):
        """GPU REFUSING + 资源达标 → 决策先行切 cpu → admit（cpu monitor disabled 放行）
        → 引擎以 device="cpu" 创建"""
        from server.ocr import ModelManager

        created = []

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                created.append(kwargs)

        class FakeManager:
            def __init__(self):
                self.admit_calls = 0
                self.success = []
                self.gpu_mon = StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95)
                self.monitors = {"gpu": self.gpu_mon}
                self.enabled = True

            def admit(self, model_id, manual=False):
                self.admit_calls += 1  # 全程放行（降级后 resource=cpu）

            def note_load_success(self, model_id):
                self.success.append(model_id)

            def note_load_failure(self, model_id, detail=""):
                return {}

        fm = FakeManager()
        monkeypatch.setattr("server.model_manager.get_model_manager", lambda: fm)
        monkeypatch.setattr("server.config.get_models_config", lambda: dict(_FALLBACK_CFG))
        monkeypatch.setitem(sys.modules, "paddleocr",
                            types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
        m = ModelManager()
        monkeypatch.setattr(m, "_ensure_env", lambda: None)
        monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
        monkeypatch.setattr(m, "_detect_paddle_device", lambda: "gpu")
        monkeypatch.setattr(m, "_can_fallback_to_cpu", lambda: True)

        engine = m.get_ocr()
        assert engine is not None
        assert created and created[0]["device"] == "cpu"
        assert m._active_device == "cpu"
        assert fm.success == ["ocr"]
        assert m.active_device == "cpu"

    def test_insufficient_resources_still_refused(self, monkeypatch):
        """资源不达标 → 维持 gpu 意图 → admit 按 pressure_refusing 拒绝（原 503 语义）"""
        from server.model_manager.types import ModelUnavailableError
        from server.ocr import ModelManager

        class FakeManager:
            enabled = True
            monitors = {"gpu": StubGpuMonitor(state=PressureState.REFUSING, ratio=0.95)}

            def admit(self, model_id, manual=False):
                # 未降级（resource 仍 gpu）→ 真实管理器按 gpu monitor REFUSING 拒绝
                raise ModelUnavailableError(model_id, "pressure_refusing")

            def note_load_success(self, model_id):
                pass

            def note_load_failure(self, model_id, detail=""):
                return {}

        monkeypatch.setattr("server.model_manager.get_model_manager", lambda: FakeManager())
        monkeypatch.setattr("server.config.get_models_config", lambda: dict(_FALLBACK_CFG))
        m = ModelManager()
        monkeypatch.setattr(m, "_ensure_env", lambda: None)
        monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
        monkeypatch.setattr(m, "_detect_paddle_device", lambda: "gpu")
        monkeypatch.setattr(m, "_can_fallback_to_cpu", lambda: False)

        with pytest.raises(ModelUnavailableError) as exc_info:
            m.get_ocr()
        assert exc_info.value.reason == "pressure_refusing"
        assert m._active_device == "gpu"  # 未误降级
