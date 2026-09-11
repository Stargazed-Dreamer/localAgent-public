"""T4 测试：MindForgeSearcherDriver 接入 + /mindforge/search|preload 同步路径包裹

设计真源：temp/sdd/model-lifecycle-manager/design.md §5.3 / §11.4 / §14

覆盖：
- 卸载后 load() 三条路径解析（last_index_dir 快照命中 / 从未加载走预载解析 / 索引缺失）
- unload() 快照 _index_dir 到 _last_index_dir（再 unload 不覆盖为 None）
- 协议符合性（resource/priority/evictable/in_flight/loaded_at/load_duration_ms）
- is_loaded 委托 search_engine.loaded
- 模块未配置时 load() 抛 LoadFailureError（不假成功）
- /mindforge/search 与 /mindforge/preload 在 sync 路径里调用 asyncio.to_thread
  （通过 monkeypatch search_engine.get_searcher 注入阻塞断言，验证不阻塞事件循环）
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.model_manager.drivers import MindForgeSearcherDriver
from server.model_manager.types import LoadFailureError

# ==================== 辅助：构造 fake SearchEngineManager ====================

class _FakeSearchEngine:
    """伪造 server.mindforge.search_engine 单例

    提供与真实 SearchEngineManager 一致的接口：
    - loaded / index_dir 属性
    - get_searcher(index_dir)：同步阻塞调用（用于测 to_thread 包裹）
    - unload()：清空 _searcher 与 _index_dir
    """

    def __init__(self, *, loaded: bool = False, index_dir: Path | None = None):
        self._loaded = loaded
        self._index_dir = index_dir
        self.get_searcher_calls: list[Path] = []
        self.unload_calls = 0

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def index_dir(self) -> Path | None:
        return self._index_dir

    def get_searcher(self, index_dir: Path):
        self.get_searcher_calls.append(Path(index_dir))
        # 模拟加载成功：置 loaded=True 并记 _index_dir
        self._loaded = True
        self._index_dir = Path(index_dir)
        return MagicMock(name="fake_searcher")

    def unload(self):
        self.unload_calls += 1
        self._loaded = False
        self._index_dir = None


@pytest.fixture
def patched_search_engine(monkeypatch):
    """注入 fake search_engine 到 server.mindforge 模块"""
    fake = _FakeSearchEngine()
    import server.mindforge as mf
    monkeypatch.setattr(mf, "search_engine", fake)
    # 驱动通过 from server.mindforge import search_engine 拿引用，
    # 这是模块级属性查找，monkeypatch 已生效；但驱动 _search_engine() 也用
    # `from server.mindforge import search_engine`，每次执行 import 语句会
    # 拿当前模块属性，所以 monkeypatch 完全生效。
    return fake


# ==================== 协议符合性 ====================

class TestProtocolConformance:
    """design §5 协议字段与方法签名"""

    def test_class_attributes_match_design(self):
        d = MindForgeSearcherDriver()
        assert d.model_id == "mindforge_searcher"
        assert d.resource == "cpu"
        assert d.footprint_mb == 1500
        assert d.priority == 30
        assert d.reload_cost_sec == 20.0
        assert d.evictable is True

    def test_in_flight_always_zero(self):
        """design §5.3 已知局限：无 in_flight 计数"""
        d = MindForgeSearcherDriver()
        assert d.in_flight() == 0

    def test_loaded_at_returns_none(self):
        """SearchEngineManager 不暴露 loaded_at 字段（design §5.5 协议允许 None）"""
        d = MindForgeSearcherDriver()
        assert d.loaded_at() is None

    def test_load_duration_ms_returns_none(self):
        d = MindForgeSearcherDriver()
        assert d.load_duration_ms() is None


# ==================== is_loaded 委托 ====================

class TestIsLoaded:
    """is_loaded() 直接委托 search_engine.loaded"""

    def test_is_loaded_false_when_search_engine_not_loaded(self, patched_search_engine):
        patched_search_engine._loaded = False
        d = MindForgeSearcherDriver()
        assert d.is_loaded() is False

    def test_is_loaded_true_when_search_engine_loaded(self, patched_search_engine):
        patched_search_engine._loaded = True
        d = MindForgeSearcherDriver()
        assert d.is_loaded() is True


# ==================== unload 快照 ====================

class TestUnloadSnapshot:
    """design §5.3：unload() 前快照 _index_dir 到 _last_index_dir"""

    def test_unload_snapshots_index_dir(self, patched_search_engine):
        """已加载时 unload 快照 _index_dir，下次 load 优先使用"""
        fake_index = Path("<external_project_root>/MindForge/kb_search/index_store")
        patched_search_engine._loaded = True
        patched_search_engine._index_dir = fake_index

        d = MindForgeSearcherDriver()
        assert d._last_index_dir is None

        d.unload()

        assert patched_search_engine.unload_calls == 1
        assert d._last_index_dir == fake_index

    def test_unload_does_not_overwrite_snapshot_when_index_dir_none(
        self, patched_search_engine
    ):
        """unload 时若 _index_dir 已 None（未加载），不覆盖已有快照"""
        d = MindForgeSearcherDriver()
        d._last_index_dir = Path("/previous/snapshot")

        patched_search_engine._loaded = False
        patched_search_engine._index_dir = None

        d.unload()

        # 快照保持
        assert d._last_index_dir == Path("/previous/snapshot")
        # unload 仍调用（no-op 安全）
        assert patched_search_engine.unload_calls == 1

    def test_unload_when_already_clean(self, patched_search_engine):
        """未加载且无快照 → unload 安全 no-op，不抛异常"""
        patched_search_engine._loaded = False
        patched_search_engine._index_dir = None

        d = MindForgeSearcherDriver()
        d.unload()
        assert patched_search_engine.unload_calls == 1
        assert d._last_index_dir is None


# ==================== load() 三条路径解析 ====================

class TestLoadPathResolution:
    """design §5.3：unload 后 load() 优先 last_index_dir，否则走预载解析"""

    def test_load_uses_last_index_dir_snapshot(self, patched_search_engine, monkeypatch):
        """路径 1：last_index_dir 快照命中 → 直接用，不调 _resolve_mindforge_dir"""
        snapshot = Path("<external_project_root>/MindForge/kb_search/index_store")

        # _resolve_mindforge_dir 若被调用则失败
        import server.mindforge as mf
        resolve_calls = {"count": 0}

        def _fail_resolve():
            resolve_calls["count"] += 1
            return None

        monkeypatch.setattr(mf, "_resolve_mindforge_dir", _fail_resolve)

        d = MindForgeSearcherDriver()
        d._last_index_dir = snapshot

        d.load()

        assert patched_search_engine.get_searcher_calls == [snapshot]
        assert resolve_calls["count"] == 0  # 未走预载解析

    def test_load_falls_back_to_preload_resolution(self, patched_search_engine, monkeypatch):
        """路径 2：从未加载 → 复刻 /mindforge/preload 解析（mf_dir + kb_search/index_store + meta.json 存在）"""
        import server.mindforge as mf

        mf_dir = Path("<external_project_root>/MindForge")
        expected_index_dir = mf_dir / "kb_search" / "index_store"

        # 真路径不需要真实文件存在；让 _resolve_mindforge_dir 返回 mf_dir，
        # meta.json 存在性检查用 monkeypatch Path.exists 拦截
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: mf_dir)

        # 拦截 (expected_index_dir / "meta.json").exists() → True
        real_exists = Path.exists

        def fake_exists(self):
            if self == expected_index_dir / "meta.json":
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        d = MindForgeSearcherDriver()
        assert d._last_index_dir is None

        d.load()

        assert patched_search_engine.get_searcher_calls == [expected_index_dir]
        # 快照已更新（来自 search_engine.index_dir）
        assert d._last_index_dir == expected_index_dir

    def test_load_raises_when_mindforge_dir_unconfigured(self, patched_search_engine, monkeypatch):
        """路径 3a：从未加载 + MindForge 项目目录不存在 → LoadFailureError"""
        import server.mindforge as mf
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: None)

        d = MindForgeSearcherDriver()
        assert d._last_index_dir is None

        with pytest.raises(LoadFailureError, match="项目目录不存在"):
            d.load()

        assert patched_search_engine.get_searcher_calls == []

    def test_load_raises_when_index_missing(self, patched_search_engine, monkeypatch):
        """路径 3b：mf_dir 存在但 meta.json 缺失 → 结构化错误"""
        import server.mindforge as mf

        mf_dir = Path("<external_project_root>/MindForge")
        expected_index_dir = mf_dir / "kb_search" / "index_store"
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: mf_dir)

        real_exists = Path.exists

        def fake_exists(self):
            if self == expected_index_dir / "meta.json":
                return False  # 索引缺失
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        d = MindForgeSearcherDriver()

        with pytest.raises(LoadFailureError, match="知识库索引不存在"):
            d.load()

        assert patched_search_engine.get_searcher_calls == []

    def test_load_wraps_search_engine_failure_as_load_failure(
        self, patched_search_engine, monkeypatch
    ):
        """get_searcher 抛异常 → 包装为 LoadFailureError（不抛裸异常）"""
        snapshot = Path("/some/index")
        d = MindForgeSearcherDriver()
        d._last_index_dir = snapshot

        # 让 fake get_searcher 抛异常
        def boom(index_dir):
            raise RuntimeError("FAISS 加载失败")

        patched_search_engine.get_searcher = boom

        with pytest.raises(LoadFailureError, match="加载失败"):
            d.load()

        # 失败时不清 last_index_dir（下次仍可尝试同一路径或重新解析）
        assert d._last_index_dir == snapshot

    def test_load_updates_snapshot_from_search_engine_index_dir(
        self, patched_search_engine, monkeypatch
    ):
        """load 成功后从 search_engine.index_dir 同步快照（get_searcher 内部可能换路径）"""
        snapshot = Path("/old/snapshot")
        new_dir = Path("/new/path/from/get_searcher")

        d = MindForgeSearcherDriver()
        d._last_index_dir = snapshot

        # 模拟 get_searcher 内部把 _index_dir 改成新路径
        original_get = patched_search_engine.get_searcher

        def get_with_new_path(index_dir):
            original_get(index_dir)
            patched_search_engine._index_dir = new_dir
            return MagicMock()

        patched_search_engine.get_searcher = get_with_new_path

        d.load()

        # 快照跟随 search_engine.index_dir
        assert d._last_index_dir == new_dir


# ==================== 管理器集成（manual_load / manual_unload 端到端） ====================

class TestManagerIntegration:
    """manual_load / manual_unload 经驱动工作端到端"""

    def test_manual_unload_then_load_roundtrip(self, patched_search_engine, monkeypatch):
        """unload → load 全链路：unload 快照 → load 用快照恢复"""
        from server.model_manager.manager import ModelLifecycleManager
        from server.model_manager.monitor import PressureMonitor, ResourceMonitorConfig

        index_dir = Path("/mf/kb_search/index_store")
        patched_search_engine._loaded = True
        patched_search_engine._index_dir = index_dir

        cfg = {
            "enabled": True,
            "poll_interval_sec": 0.01,
            "reload_fail_cooldown_sec": 30,
            "gpu": ResourceMonitorConfig(resource="gpu", enabled=False),
            "cpu": ResourceMonitorConfig(
                resource="cpu", enabled=False,  # 关监控避免采样干扰
                poll_interval_sec=0.01,
                high_watermark=0.95, high_sustain=3,
                critical_watermark=0.99, low_watermark=0.50, low_sustain=3,
            ),
            "gpu_min_loaded_seconds": 60,
            "cpu_min_loaded_seconds": 60,
            "gpu_unload_timeout_sec": 5,
            "cpu_unload_timeout_sec": 5,
            "models": {},
        }
        monitors = {
            "gpu": PressureMonitor(cfg["gpu"]),
            "cpu": PressureMonitor(cfg["cpu"]),
        }
        mgr = ModelLifecycleManager(cfg=cfg, monitors=monitors)
        driver = MindForgeSearcherDriver()
        mgr.registry.register(driver)

        try:
            asyncio.run(mgr.manual_unload("mindforge_searcher"))
            assert patched_search_engine.unload_calls == 1
            assert driver._last_index_dir == index_dir

            asyncio.run(mgr.manual_load("mindforge_searcher"))
            assert patched_search_engine.get_searcher_calls == [index_dir]
            assert patched_search_engine._loaded is True
        finally:
            asyncio.run(mgr.stop())


# ==================== /mindforge/search & /preload to_thread 包裹 ====================

class TestSearchEndpointAsyncToThread:
    """design §11.4 / T4-2：/mindforge/search 同步重路径经 asyncio.to_thread 包裹

    验证：sync get_searcher 在工作线程执行，不阻塞事件循环。
    """

    def test_search_runs_get_searcher_in_worker_thread(self, monkeypatch):
        """get_searcher 应在工作线程被调用（threading.get_ident != 主线程）"""
        # 用 FastAPI TestClient 跑端点
        import server.mindforge as mf

        main_thread_id = threading.get_ident()
        observed_thread_ids: list[int] = []

        fake_se = _FakeSearchEngine(loaded=False)

        def get_searcher_thread_recorder(index_dir):
            observed_thread_ids.append(threading.get_ident())
            # 模拟阻塞加载
            time.sleep(0.05)
            return fake_se.get_searcher(index_dir)

        fake_se.get_searcher = get_searcher_thread_recorder
        monkeypatch.setattr(mf, "search_engine", fake_se)

        mf_dir = Path("<external_project_root>/MindForge")
        index_dir = mf_dir / "kb_search" / "index_store"
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: mf_dir)

        real_exists = Path.exists

        def fake_exists(self):
            if self == index_dir / "meta.json":
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        # mock 搜索结果
        fake_searcher = MagicMock()
        fake_searcher.search.return_value = []
        # 覆盖 fake_se.get_searcher 的返回值（前一个 mock 已写 thread_recorder，
        # 这里改成返回 fake_searcher）
        def get_return_fake(index_dir):
            observed_thread_ids.append(threading.get_ident())
            time.sleep(0.01)
            return fake_searcher

        fake_se.get_searcher = get_return_fake

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/search", json={"query": "测试", "top_k": 5})

        assert resp.status_code == 200, resp.text
        assert observed_thread_ids, "get_searcher 应至少被调用一次"
        # 至少有一次不在主线程
        assert any(tid != main_thread_id for tid in observed_thread_ids), \
            "get_searcher 必须在工作线程执行（asyncio.to_thread）"

    def test_preload_runs_get_searcher_in_worker_thread(self, monkeypatch):
        """preload 端点的 get_searcher 应在工作线程执行"""
        import server.mindforge as mf

        main_thread_id = threading.get_ident()
        observed_thread_ids: list[int] = []

        fake_se = _FakeSearchEngine(loaded=False)

        def get_return_fake(index_dir):
            observed_thread_ids.append(threading.get_ident())
            return MagicMock()

        fake_se.get_searcher = get_return_fake
        monkeypatch.setattr(mf, "search_engine", fake_se)

        mf_dir = Path("<external_project_root>/MindForge")
        index_dir = mf_dir / "kb_search" / "index_store"
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: mf_dir)

        real_exists = Path.exists

        def fake_exists(self):
            if self == index_dir / "meta.json":
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        # T5-5 后 /mindforge/preload 委托 manager.manual_load("mindforge_searcher")，
        # 需在管理器注册表里登记驱动（生产由 lifecycle startup 完成；TestClient 不
        # 跑 lifespan，需测试内显式注册）
        from server.model_manager import get_model_manager
        from server.model_manager.drivers import MindForgeSearcherDriver
        mgr = get_model_manager()
        if mgr.registry.get("mindforge_searcher") is None:
            mgr.registry.register(MindForgeSearcherDriver())

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/preload")

        assert resp.status_code == 200, resp.text
        assert observed_thread_ids, "get_searcher 应被调用"
        assert any(tid != main_thread_id for tid in observed_thread_ids), \
            "preload 的 get_searcher 必须在工作线程执行"

    def test_search_returns_503_when_mindforge_unconfigured(self, monkeypatch):
        """MindForge 项目目录不存在 → 503（不阻塞事件循环）"""
        import server.mindforge as mf
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: None)

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/search", json={"query": "x"})
        assert resp.status_code == 503
        assert "MindForge" in resp.json()["detail"]

    def test_preload_returns_already_loaded_shortcut(self, monkeypatch):
        """search_engine.loaded=True → already_loaded 快速返回，不调 get_searcher"""
        import server.mindforge as mf

        fake_se = _FakeSearchEngine(loaded=True)
        # 如果走 get_searcher 路径会失败
        def fail(*args, **kwargs):
            raise AssertionError("get_searcher 不应被调用（已 loaded）")

        fake_se.get_searcher = fail
        monkeypatch.setattr(mf, "search_engine", fake_se)

        mf_dir = Path("<external_project_root>/MindForge")
        index_dir = mf_dir / "kb_search" / "index_store"
        monkeypatch.setattr(mf, "_resolve_mindforge_dir", lambda: mf_dir)

        real_exists = Path.exists

        def fake_exists(self):
            if self == index_dir / "meta.json":
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/preload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_loaded"


# ==================== /mindforge/unload 保持同步（轻量） ====================

class TestUnloadEndpointStaysSync:
    """design §5.3：unload() 只删属性 + gc.collect，轻量，保持同步"""

    def test_unload_endpoint_works_without_event_loop_block(self, monkeypatch):
        import server.mindforge as mf

        fake_se = _FakeSearchEngine(loaded=True)
        monkeypatch.setattr(mf, "search_engine", fake_se)

        # T5-5 后 /mindforge/unload 委托 manager.manual_unload("mindforge_searcher")，
        # 需在管理器注册表里登记驱动（生产由 lifecycle startup 完成；TestClient 不
        # 跑 lifespan，需测试内显式注册）
        from server.model_manager import get_model_manager
        from server.model_manager.drivers import MindForgeSearcherDriver
        mgr = get_model_manager()
        if mgr.registry.get("mindforge_searcher") is None:
            mgr.registry.register(MindForgeSearcherDriver())

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/unload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "unloaded"
        assert fake_se.unload_calls == 1

    def test_unload_when_not_loaded_returns_not_loaded(self, monkeypatch):
        import server.mindforge as mf

        fake_se = _FakeSearchEngine(loaded=False)
        monkeypatch.setattr(mf, "search_engine", fake_se)

        from fastapi.testclient import TestClient

        from server.main import app

        client = TestClient(app)
        resp = client.post("/mindforge/unload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_loaded"
        assert fake_se.unload_calls == 0
