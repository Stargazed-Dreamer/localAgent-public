"""T3 测试：Embedding 可卸载性 + EmbeddingDriver

设计真源：temp/sdd/model-lifecycle-manager/design.md §5.2 / §7 / §14

覆盖：
- EmbeddingEngine.unload()：置空 session/tokenizer/st_model、_ready=False、
  重置 _use_st_fallback、不重置 _download_attempted
- embed() 局部快照：unload 与推理竞态时 _session 为 None → 返回零向量
  （不抛 AttributeError → search 不再 500），ST 路径同款
- unload 后可重新 initialize（_use_st_fallback 重置后路径干净）
- EmbeddingDriver 默认 evictable=False（design §5.2 钉住）
- EmbeddingDriver 懒绑定：目标未创建时 is_loaded=False / load 抛 LoadFailureError /
  unload 安全 no-op（不假成功）
- initialize()==False → LoadFailureError（design §5.2：吞异常路径必须转异常）
- is_loaded 不触发 MemoryManager / AgentGuideEmbedder 创建（避免每采样周期触发重初始化）
- 管理器 manual_load / manual_unload 经 EmbeddingDriver 工作端到端
- 钉住的 embedding 在 REFUSING 期间不被逐出
"""

import asyncio
from unittest.mock import MagicMock

import numpy as np
import pytest

from server.memory.embeddings import EmbeddingEngine
from server.model_manager.drivers import (
    GuideEmbeddingDriver,
    MemoryEmbeddingDriver,
)
from server.model_manager.manager import ModelLifecycleManager
from server.model_manager.monitor import PressureMonitor, ResourceMonitorConfig
from server.model_manager.registry import ModelRegistry
from server.model_manager.types import LoadFailureError

# ==================== EmbeddingEngine.unload() ====================

class TestEmbeddingEngineUnload:
    """design §5.2：unload 释放资源 + 重置降级标志 + 不重置 download_attempted"""

    def test_unload_clears_onnx_resources(self):
        engine = EmbeddingEngine()
        # 模拟 ONNX 加载完成状态
        engine._session = object()
        engine._tokenizer = object()
        engine._ready = True
        engine._use_st_fallback = False

        engine.unload()

        assert engine._session is None
        assert engine._tokenizer is None
        assert engine._ready is False
        assert engine.ready is False

    def test_unload_clears_st_resources(self):
        engine = EmbeddingEngine()
        engine._st_model = object()
        engine._use_st_fallback = True
        engine._ready = True

        engine.unload()

        assert engine._st_model is None
        assert engine._ready is False

    def test_unload_resets_use_st_fallback(self):
        """design §5.2 关键修复：必须重置 _use_st_fallback
        否则下次 initialize 误走降级路径（_load_model 路径由 _has_local_st_structure
        重判，残留标志会让 embed() 路由到 _embed_st 但 _st_model 已 None → AttributeError）
        """
        engine = EmbeddingEngine()
        engine._use_st_fallback = True
        engine._ready = True

        engine.unload()

        assert engine._use_st_fallback is False

    def test_unload_does_not_reset_download_attempted(self):
        """design §5.2：_download_attempted 不重置，避免每次 unload/load 循环重复下载"""
        engine = EmbeddingEngine()
        engine._download_attempted = True
        engine._ready = True

        engine.unload()

        assert engine._download_attempted is True

    def test_unload_then_reinitialize(self, monkeypatch):
        """unload 后可重新 initialize（design §5.2：可无损重初始化）"""
        engine = EmbeddingEngine()
        # mock _load_model：真实 _load_onnx/_setup_st 都会在成功时置 _ready=True，
        # 模拟必须同步此副作用，否则 initialize() 返回 True 但 ready 仍 False
        def fake_load_model():
            engine._ready = True
            return True
        monkeypatch.setattr(engine, "_load_model", fake_load_model)

        assert engine.initialize() is True
        engine.unload()
        assert engine.ready is False
        # 重新 initialize
        assert engine.initialize() is True
        assert engine.ready is True


# ==================== embed() 局部快照（竞态微修） ====================

class TestEmbedRaceSnapshot:
    """design §5.2：embed() 推理前对 _session 做局部快照

    场景：embed() 检查 _ready=True → 进入 _embed_onnx/_embed_st →
    unload() 清空 _session/_st_model → 推理访问 self._session → 旧代码 AttributeError
    抛到 _hybrid_search → search() re-raise → 500。

    修复：在 _embed_onnx/_embed_st 入口取局部快照；快照为 None 时返回零向量降级。
    """

    def test_embed_onnx_returns_zeros_when_session_nulled(self):
        """ONNX 路径：_ready=True 但 _session=None（unload 进行中）→ 返回零向量"""
        engine = EmbeddingEngine()
        engine._ready = True
        engine._session = None  # 模拟 unload 已清空（race window）
        engine._tokenizer = None

        result = engine.embed(["测试文本"])

        assert result.shape == (1, engine.dim)
        assert (result == 0).all()
        # 关键：不抛 AttributeError

    def test_embed_st_returns_zeros_when_st_model_nulled(self):
        """ST 路径：_use_st_fallback=True 但 _st_model=None → 返回零向量"""
        engine = EmbeddingEngine()
        engine._ready = True
        engine._use_st_fallback = True
        engine._st_model = None  # 模拟 unload 已清空

        result = engine.embed(["测试文本"])

        assert result.shape == (1, engine.dim)
        assert (result == 0).all()

    def test_embed_uses_snapshot_not_live_attribute(self):
        """快照在推理期间不被 unload 影响：session 引用被局部持有，推理可完成

        竞态场景：_embed_onnx 第一次访问 self._session（get_inputs）后，
        unload 把 self._session=None；无快照代码第二次访问 self._session.run
        会拿到 None → AttributeError。快照代码用局部变量 session，两次访问
        都是同一引用。
        """
        engine = EmbeddingEngine()
        recorded = {"run_called": False}

        class FakeSession:
            def get_inputs(self):
                # 模拟 unload 在 get_inputs 与 run 之间发生
                # 无快照代码：第二次 self._session.run() 拿到 None → AttributeError
                # 快照代码：session.run() 用局部引用，仍可调用
                engine._session = None
                return []

            def run(self, *_args, **_kwargs):
                recorded["run_called"] = True
                return [np.zeros((1, 8, engine.dim), dtype=np.float32)]

        class FakeTokenizer:
            def encode_batch(self, texts):
                class _E:
                    def __init__(self, ids, mask):
                        self.ids = ids
                        self.attention_mask = mask
                # 长度 8 与 FakeSession.run 返回的 (1, 8, dim) 对齐
                return [_E([1] * 8, [1] * 8)]

        engine._session = FakeSession()
        engine._tokenizer = FakeTokenizer()
        engine._ready = True

        result = engine.embed(["测试"])

        # 快照路径：FakeSession.run 被调用（局部快照持有引用），推理完成
        assert recorded["run_called"] is True
        assert result.shape == (1, engine.dim)

    def test_embed_st_uses_snapshot_not_live_attribute(self):
        """ST 路径同款：快照持有引用，推理期间 unload 不影响"""
        engine = EmbeddingEngine()
        recorded = {"encode_called": False}

        class FakeST:
            def encode(self, texts, normalize_embeddings=True):
                # 模拟 unload 在 encode 调用前发生（self._st_model=None）
                # 无快照代码：self._st_model.encode → None.encode → AttributeError
                # 快照代码：st.encode 用局部引用，仍可调用
                engine._st_model = None
                recorded["encode_called"] = True
                return np.zeros((1, engine.dim), dtype=np.float32)

        engine._st_model = FakeST()
        engine._use_st_fallback = True
        engine._ready = True

        result = engine.embed(["测试"])

        assert recorded["encode_called"] is True
        assert result.shape == (1, engine.dim)


# ==================== EmbeddingDriver 默认姿态 ====================

class TestEmbeddingDriverDefaults:
    """design §5.2：默认 evictable=False（钉住）；CPU；优先级合理"""

    def test_memory_driver_default_pinned(self):
        d = MemoryEmbeddingDriver()
        assert d.evictable is False
        assert d.resource == "cpu"
        assert d.model_id == "memory_embedding"
        assert d.footprint_mb == 90
        assert d.reload_cost_sec == 2.0
        assert d.priority == 80        # 热路径，同 OCR

    def test_guide_driver_default_pinned(self):
        d = GuideEmbeddingDriver()
        assert d.evictable is False
        assert d.resource == "cpu"
        assert d.model_id == "guide_embedding"
        assert d.footprint_mb == 90
        assert d.priority == 70        # 弱于 memory

    def test_drivers_have_no_load_duration_data(self):
        """design §5.5：无现成数据源 → load_duration_ms 恒 None"""
        assert MemoryEmbeddingDriver().load_duration_ms() is None
        assert GuideEmbeddingDriver().load_duration_ms() is None
        assert MemoryEmbeddingDriver().loaded_at() is None
        assert GuideEmbeddingDriver().loaded_at() is None

    def test_drivers_in_flight_always_zero(self):
        """协议允许 in_flight 恒 0（无排空能力，design §13 由 embed() 局部快照兜底）"""
        assert MemoryEmbeddingDriver().in_flight() == 0
        assert GuideEmbeddingDriver().in_flight() == 0

    def test_register_both_in_registry(self):
        """两个 EmbeddingDriver 可同时注册"""
        reg = ModelRegistry()
        reg.register(MemoryEmbeddingDriver())
        reg.register(GuideEmbeddingDriver())
        assert reg.get("memory_embedding") is not None
        assert reg.get("guide_embedding") is not None
        assert len(reg) == 2

    def test_duplicate_register_raises(self):
        reg = ModelRegistry()
        reg.register(MemoryEmbeddingDriver())
        with pytest.raises(ValueError):
            reg.register(MemoryEmbeddingDriver())


# ==================== 懒绑定（design §5.2） ====================

class TestLazyBinding:
    """design §5.2：驱动每次调用经动态解引用，容忍 None

    核心约束：is_loaded 不应触发 MemoryManager / AgentGuideEmbedder 的重初始化
    （is_loaded 每采样周期被调用，触发全套初始化代价过高）
    """

    def test_memory_driver_engine_peek_does_not_force_init(self, monkeypatch):
        """is_loaded 调用 _engine() 时不触发 get_memory_manager 全套初始化"""
        import server.memory.manager as mm

        # 强制 _instance=None，验证 _engine() 不调 get_memory_manager
        original_instance = mm._instance
        mm._instance = None
        # 把 get_memory_manager 替换成会 raise 的版本，确保不被调用
        guard_called = {"hit": False}

        def guard():
            guard_called["hit"] = True
            raise AssertionError("get_memory_manager 不应被 _engine() 调用")

        monkeypatch.setattr(mm, "get_memory_manager", guard)
        try:
            driver = MemoryEmbeddingDriver()
            assert driver._engine() is None
            assert guard_called["hit"] is False
        finally:
            mm._instance = original_instance

    def test_guide_driver_engine_peek_does_not_force_init(self, monkeypatch):
        """is_loaded 调用 _engine() 时不触发 AgentGuideEmbedder 创建"""
        from server.agent_guide_embedder import AgentGuideEmbedder

        original_instance = AgentGuideEmbedder._instance
        AgentGuideEmbedder._instance = None
        guard_called = {"hit": False}

        def guard():
            guard_called["hit"] = True
            raise AssertionError("get_embedder 不应被 _engine() 调用")

        # 同时 patch 模块级 get_embedder 与类级 __new__
        import server.agent_guide_embedder as age
        monkeypatch.setattr(age, "get_embedder", guard)
        try:
            driver = GuideEmbeddingDriver()
            assert driver._engine() is None
            assert guard_called["hit"] is False
        finally:
            AgentGuideEmbedder._instance = original_instance

    def test_memory_driver_is_loaded_false_when_target_none(self, monkeypatch):
        """目标未创建 → is_loaded=False"""
        import server.memory.manager as mm
        original = mm._instance
        mm._instance = None
        try:
            d = MemoryEmbeddingDriver()
            assert d.is_loaded() is False
        finally:
            mm._instance = original

    def test_guide_driver_is_loaded_false_when_target_none(self, monkeypatch):
        from server.agent_guide_embedder import AgentGuideEmbedder
        original = AgentGuideEmbedder._instance
        AgentGuideEmbedder._instance = None
        try:
            d = GuideEmbeddingDriver()
            assert d.is_loaded() is False
        finally:
            AgentGuideEmbedder._instance = original

    def test_memory_driver_unload_no_op_when_target_none(self, monkeypatch):
        """目标未创建 → unload 是安全 no-op（不抛异常，不假成功）"""
        import server.memory.manager as mm
        original = mm._instance
        mm._instance = None
        try:
            d = MemoryEmbeddingDriver()
            d.unload()  # 不抛异常即可
        finally:
            mm._instance = original

    def test_guide_driver_unload_no_op_when_target_none(self, monkeypatch):
        from server.agent_guide_embedder import AgentGuideEmbedder
        original = AgentGuideEmbedder._instance
        AgentGuideEmbedder._instance = None
        try:
            d = GuideEmbeddingDriver()
            d.unload()
        finally:
            AgentGuideEmbedder._instance = original


# ==================== initialize()==False → LoadFailureError ====================

class TestLoadFailurePropagation:
    """design §5.2：EmbeddingEngine.initialize() 吞异常返回 False 时，
    驱动必须转 LoadFailureError，否则管理器无法进入冷却/降级（design §7）"""

    def test_memory_driver_load_raises_on_initialize_false(self):
        d = MemoryEmbeddingDriver()
        fake_engine = MagicMock()
        fake_engine.initialize.return_value = False
        d._engine_for_load = lambda: fake_engine

        with pytest.raises(LoadFailureError, match="returned False"):
            d.load()

    def test_guide_driver_load_raises_on_initialize_false(self):
        d = GuideEmbeddingDriver()
        fake_engine = MagicMock()
        fake_engine.initialize.return_value = False
        d._engine_for_load = lambda: fake_engine

        with pytest.raises(LoadFailureError, match="returned False"):
            d.load()

    def test_memory_driver_load_raises_when_engine_unavailable(self):
        """design §5.2: 目标未创建 → 不 no-op 假成功"""
        d = MemoryEmbeddingDriver()
        d._engine_for_load = lambda: None

        with pytest.raises(LoadFailureError, match="unavailable"):
            d.load()

    def test_guide_driver_load_raises_when_engine_unavailable(self):
        d = GuideEmbeddingDriver()
        d._engine_for_load = lambda: None

        with pytest.raises(LoadFailureError, match="unavailable"):
            d.load()

    def test_memory_driver_load_success_calls_initialize(self):
        d = MemoryEmbeddingDriver()
        fake_engine = MagicMock()
        fake_engine.initialize.return_value = True
        d._engine_for_load = lambda: fake_engine

        d.load()  # 不抛异常
        fake_engine.initialize.assert_called_once()

    def test_memory_driver_unload_calls_engine_unload(self):
        d = MemoryEmbeddingDriver()
        fake_engine = MagicMock()
        d._engine = lambda: fake_engine

        d.unload()
        fake_engine.unload.assert_called_once()


# ==================== 管理器集成 ====================

def _make_cfg(cpu_enabled=True):
    """构造测试配置：CPU 监控开/关可选（默认开，便于测钉住逐出场景）"""
    poll = 0.01
    return {
        "enabled": True,
        "poll_interval_sec": poll,
        "reload_fail_cooldown_sec": 30,
        "gpu": ResourceMonitorConfig(resource="gpu", enabled=False),
        "cpu": ResourceMonitorConfig(
            resource="cpu", enabled=cpu_enabled, poll_interval_sec=poll,
            high_watermark=0.90, high_sustain=3, critical_watermark=0.97,
            low_watermark=0.70, low_sustain=6,
        ),
        "gpu_min_loaded_seconds": 60,
        "cpu_min_loaded_seconds": 60,
        "gpu_unload_timeout_sec": 5,
        "cpu_unload_timeout_sec": 5,
        "models": {},
    }


class _StubProbe:
    """按脚本返回 (used, total)；用于把 CPU 监控推到 REFUSING"""

    TOTAL = 8 * 1024 ** 3

    def __init__(self, ratios):
        self.ratios = list(ratios)
        self.i = 0

    def __call__(self):
        r = self.ratios[self.i] if self.i < len(self.ratios) else self.ratios[-1]
        self.i += 1
        return int(self.TOTAL * r), self.TOTAL


class _FakeEmbeddingDriver:
    """简化版 EmbeddingDriver：可控 loaded / load 失败 / unload 记录

    直接造一个最小实现 ModelDriver 协议，便于在管理器集成测试中精确控制状态。
    """

    def __init__(self, model_id="memory_embedding", resource="cpu",
                 evictable=False, loaded=True, load_should_fail=False,
                 loaded_at_age=120.0):
        """loaded_at_age：模拟加载完成至今的秒数（默认 120s，过 60s 保护期）。
        None 表示不报告时间戳（由管理器观察协议代记）。"""
        import time as _time
        self.model_id = model_id
        self.resource = resource
        self.footprint_mb = 90
        self.priority = 80
        self.reload_cost_sec = 2.0
        self.evictable = evictable
        self._loaded = loaded
        self.load_should_fail = load_should_fail
        self._loaded_at = (_time.time() - loaded_at_age) if loaded and loaded_at_age else None
        self.load_calls = 0
        self.unload_calls = 0

    def is_loaded(self):
        return self._loaded

    def load(self):
        self.load_calls += 1
        if self.load_should_fail:
            raise LoadFailureError("stub load failure")

    def unload(self):
        self.unload_calls += 1
        self._loaded = False

    def in_flight(self):
        return 0

    def loaded_at(self):
        return self._loaded_at

    def load_duration_ms(self):
        return None


class TestManagerIntegration:
    """管理器与 EmbeddingDriver 集成（manual_load / manual_unload / 钉住防逐出）"""

    def _make_manager(self, drivers, probe_ratios=None):
        cfg = _make_cfg()
        cpu_probe = _StubProbe(probe_ratios or [0.5])
        monitors = {
            "gpu": PressureMonitor(cfg["gpu"], probe=_StubProbe([0.3])),
            "cpu": PressureMonitor(cfg["cpu"], probe=cpu_probe),
        }
        m = ModelLifecycleManager(cfg=cfg, monitors=monitors)
        for d in drivers:
            m.registry.register(d)
        return m

    def test_manual_load_via_driver_success(self):
        d = _FakeEmbeddingDriver(loaded=False)
        m = self._make_manager([d])

        async def _run():
            await m.monitors["cpu"].sample()  # 首样本
            m.monitors["cpu"].observe(0.5)
            return await m.manual_load("memory_embedding")

        result = asyncio.run(_run())
        assert result["status"] == "ok"
        assert d.load_calls == 1

    def test_manual_load_failure_enters_cooldown(self):
        d = _FakeEmbeddingDriver(loaded=False, load_should_fail=True)
        m = self._make_manager([d])

        async def _run():
            await m.monitors["cpu"].sample()
            m.monitors["cpu"].observe(0.5)
            return await m.manual_load("memory_embedding")

        result = asyncio.run(_run())
        assert result["status"] == "error"
        assert result["fail_count"] == 1
        assert m._in_cooldown("memory_embedding")

    def test_manual_unload_via_driver(self):
        d = _FakeEmbeddingDriver(loaded=True)
        m = self._make_manager([d])

        async def _run():
            return await m.manual_unload("memory_embedding")

        result = asyncio.run(_run())
        assert result["status"] == "ok"
        assert d.unload_calls == 1

        # 已卸载 → 幂等 already_unloaded
        async def _run2():
            return await m.manual_unload("memory_embedding")
        result2 = asyncio.run(_run2())
        assert result2["status"] == "ok"
        assert result2["detail"] == "already_unloaded"
        assert d.unload_calls == 1  # 没二次调用

    def test_pinned_driver_not_evicted_under_cpu_pressure(self):
        """design §5.2：钉住的 embedding 即使 CPU REFUSING 也不被逐出"""
        d = _FakeEmbeddingDriver(loaded=True, evictable=False)
        m = self._make_manager([d], probe_ratios=[0.95])  # 高压

        async def _run():
            # 3 个高压样本 → REFUSING + 触发逐出评估
            for _ in range(3):
                await m._tick()
            await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert m.monitors["cpu"].state.value == "refusing"
        # 关键：钉住的 driver 没被卸载
        assert d.unload_calls == 0
        assert d.is_loaded() is True

    def test_pinned_overridden_to_evictable_via_config(self):
        """design §5.2/§7: 配置可覆盖 evictable=True 后可被逐出"""
        d = _FakeEmbeddingDriver(loaded=True, evictable=False)
        m = self._make_manager([d], probe_ratios=[0.95])
        # 配置覆盖
        m.cfg["models"]["memory_embedding"] = {"evictable": True}

        async def _run():
            for _ in range(3):
                await m._tick()
            await asyncio.sleep(0.1)

        asyncio.run(_run())
        assert d.unload_calls == 1
        assert d.is_loaded() is False


# ==================== 静默降级文档化（design §5.2） ====================

class TestSilentDegradeContract:
    """design §5.2：手动卸载记忆 embedding 后语义检索静默降级为 BM25-only

    不引入运行时告警；恢复 = load + rebuild_vector_index（memory/router.py:256）。
    这里只验证 EmbeddingEngine.ready=False 时 embed 返回零向量（消费者先查 ready）。
    """

    def test_unloaded_engine_embed_returns_zeros(self):
        """卸载后 embed 走 _ready=False 分支返回零向量"""
        engine = EmbeddingEngine()
        # 模拟已加载 → 卸载
        engine._ready = True
        engine.unload()

        result = engine.embed(["test"])
        assert result.shape == (1, engine.dim)
        assert (result == 0).all()
