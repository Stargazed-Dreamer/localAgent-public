"""SearchTracer 测试 — 检索可观测性

测试 SearchTracer 的生命周期（start_trace → record_subsearch → record_fusion →
finish_trace）、采样率、清理、查询端点，以及与 SemanticSearch 的集成。
"""

import os
import tempfile

import pytest

from server.memory.search_tracer import SearchTracer
from server.memory.store import MemoryStore

# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    """创建临时 DB 文件路径"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        # WAL 模式可能产生 -wal/-shm 文件，尝试清理
        for suffix in ("", "-wal", "-shm"):
            f = path + suffix
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass


@pytest.fixture
def store(tmp_db):
    """创建已初始化的 MemoryStore"""
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()
    # 强制 GC 清理 Windows 上的 WAL/SHM 文件
    import gc
    gc.collect()


@pytest.fixture
def memory_manager():
    """获取全局 MemoryManager 单例（依赖已启动的 app）

    清理策略：
    - 测试前/后删除所有 query 以 "test_" 开头的 trace
    - 测试前/后删除所有 key 以 "test_search_tracer_" 开头的 messages 和 facts
    避免污染真实数据。
    """
    from server.memory.manager import get_memory_manager

    mgr = get_memory_manager()

    def _cleanup():
        try:
            mgr.store.conn.execute("DELETE FROM search_traces WHERE query LIKE 'test_%'")
            mgr.store.conn.execute(
                "DELETE FROM messages WHERE key LIKE 'test_search_tracer_%'"
            )
            mgr.store.conn.execute(
                "DELETE FROM facts WHERE key LIKE 'test_search_tracer_%'"
            )
            mgr.store.conn.commit()
        except Exception:
            pass

    _cleanup()
    yield mgr
    _cleanup()


# ==================== 基础生命周期 ====================

class TestSearchTracerLifecycle:
    """基础生命周期测试"""

    def test_start_trace_returns_id(self, store: MemoryStore):
        """start_trace 应返回 trace_id"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        trace_id = tracer.start_trace(query="hello", params={"top_k": 10})
        assert trace_id is not None
        assert isinstance(trace_id, int)
        assert trace_id > 0
        # 清理线程本地状态
        tracer.finish_trace(trace_id, [], mode="bm25_only")

    def test_start_trace_sample_rate_zero(self, store: MemoryStore):
        """sample_rate=0 时 start_trace 返回 None"""
        tracer = SearchTracer(store=store, sample_rate=0.0)
        trace_id = tracer.start_trace(query="hello", params={"top_k": 10})
        assert trace_id is None

    def test_start_trace_disabled(self, store: MemoryStore):
        """enabled=False 时 start_trace 返回 None"""
        tracer = SearchTracer(store=store, sample_rate=1.0, enabled=False)
        trace_id = tracer.start_trace(query="hello", params={"top_k": 10})
        assert trace_id is None

    def test_full_lifecycle_persists_to_db(self, store: MemoryStore):
        """完整生命周期应持久化到 search_traces 表"""
        tracer = SearchTracer(store=store, sample_rate=1.0)

        trace_id = tracer.start_trace(query="test_query", params={"top_k": 5})
        assert trace_id is not None

        tracer.record_subsearch(trace_id, "bm25", [
            {"message_id": 1, "score": 0.9},
            {"message_id": 2, "score": 0.8},
        ], latency_ms=5)
        tracer.record_subsearch(trace_id, "vector", [
            {"message_id": 1, "score": 0.85},
        ], latency_ms=20)
        tracer.record_fusion(trace_id, [
            {"message_id": 1, "score": 0.88, "content": "result1"},
        ])

        final = [{"message_id": 1, "score": 0.88}]
        tracer.finish_trace(trace_id, final, mode="hybrid")

        # 验证写入
        detail = tracer.get_detail(trace_id)
        assert detail is not None
        assert detail["query"] == "test_query"
        assert detail["mode"] == "hybrid"
        assert detail["bm25_count"] == 2
        assert detail["vector_count"] == 1
        assert detail["final_count"] == 1
        assert detail["latency_ms"] >= 0
        assert detail["error"] is None
        # params 应被解析为 dict
        assert isinstance(detail["params"], dict)
        assert detail["params"]["top_k"] == 5
        # candidates 应被解析为 dict，含 subsearches 和 fusion
        assert isinstance(detail["candidates"], dict)
        assert "subsearches" in detail["candidates"]
        assert "fusion" in detail["candidates"]
        assert len(detail["candidates"]["subsearches"]) == 2
        assert detail["candidates"]["subsearches"][0]["kind"] == "bm25"
        assert detail["candidates"]["subsearches"][1]["kind"] == "vector"
        # final_results 应被解析为 list
        assert isinstance(detail["final_results"], list)
        assert detail["final_results"][0]["message_id"] == 1


# ==================== 错误 trace ====================

class TestSearchTracerError:
    """错误情况测试"""

    def test_finish_trace_with_error(self, store: MemoryStore):
        """finish_trace 时 error 不为空"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        trace_id = tracer.start_trace(query="boom", params={})
        tracer.finish_trace(trace_id, [], mode="error", error="something failed")

        detail = tracer.get_detail(trace_id)
        assert detail is not None
        assert detail["mode"] == "error"
        assert detail["error"] == "something failed"
        assert detail["final_count"] == 0

    def test_record_subsearch_with_none_trace_id(self, store: MemoryStore):
        """trace_id=None 时 record_subsearch 应静默跳过"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        # 不应抛出异常
        tracer.record_subsearch(None, "bm25", [], 0)
        tracer.record_fusion(None, [])
        tracer.finish_trace(None, [], mode="bm25_only")


# ==================== 查询端点 ====================

class TestSearchTracerQuery:
    """查询方法测试"""

    def test_get_recent_returns_list(self, store: MemoryStore):
        """get_recent 应返回最近 trace 列表（不含 candidates）"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        for i in range(3):
            tid = tracer.start_trace(query=f"q{i}", params={"i": i})
            tracer.finish_trace(tid, [], mode="bm25_only")

        recent = tracer.get_recent(limit=10)
        assert len(recent) == 3
        # 应按 id DESC 排序
        assert recent[0]["id"] > recent[1]["id"] > recent[2]["id"]
        # 不应包含 candidates 字段
        for r in recent:
            assert "candidates" not in r
            assert "final_results" not in r
            # params 已被解析为 dict
            assert isinstance(r["params"], dict)

    def test_get_recent_with_since_id(self, store: MemoryStore):
        """since_id 游标分页应只返回 id > since_id 的记录"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        ids = []
        for i in range(5):
            tid = tracer.start_trace(query=f"q{i}", params={})
            tracer.finish_trace(tid, [], mode="bm25_only")
            ids.append(tid)

        # 取 id > ids[2] 的记录
        recent = tracer.get_recent(limit=10, since_id=ids[2])
        assert len(recent) == 2
        assert all(r["id"] > ids[2] for r in recent)

    def test_get_detail_nonexistent(self, store: MemoryStore):
        """get_detail 不存在的 trace_id 应返回 None"""
        tracer = SearchTracer(store=store, sample_rate=1.0)
        detail = tracer.get_detail(999999)
        assert detail is None


# ==================== 清理 ====================

class TestSearchTracerCleanup:
    """cleanup_old 测试"""

    def test_cleanup_old_deletes_old_traces(self, store: MemoryStore):
        """cleanup_old 应删除超过 retention_days 的 trace"""
        tracer = SearchTracer(store=store, retention_days=1, sample_rate=1.0)
        # 写入一条 trace
        tid = tracer.start_trace(query="old", params={})
        tracer.finish_trace(tid, [], mode="bm25_only")

        # 手动把 ts 改成 2 天前
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        store.conn.execute("UPDATE search_traces SET ts = ? WHERE id = ?", (old_ts, tid))
        store.conn.commit()

        result = tracer.cleanup_old()
        assert result["deleted"] == 1
        assert result["retention_days"] == 1

        # 应已删除
        detail = tracer.get_detail(tid)
        assert detail is None

    def test_cleanup_old_keeps_recent(self, store: MemoryStore):
        """cleanup_old 不应删除 retention 内的 trace"""
        tracer = SearchTracer(store=store, retention_days=30, sample_rate=1.0)
        tid = tracer.start_trace(query="recent", params={})
        tracer.finish_trace(tid, [], mode="bm25_only")

        result = tracer.cleanup_old()
        assert result["deleted"] == 0

        # trace 仍存在
        detail = tracer.get_detail(tid)
        assert detail is not None


# ==================== 与 SemanticSearch 集成 ====================

class TestSearchTracerIntegration:
    """与 SemanticSearch 集成测试"""

    def test_search_creates_trace(self, memory_manager):
        """SemanticSearch.search() 应自动创建 trace"""
        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        # 确认 tracer 已注入
        assert mgr.search_tracer is not None
        assert mgr.semantic.tracer is mgr.search_tracer

        # 写入一些消息（用 test_ 前缀 key，便于 fixture 清理）
        msg1_id = mgr.store.insert_message(
            content="hello world foo bar", source="manual", key="test_search_tracer_msg1"
        )
        mgr.semantic.index_message(msg1_id, "hello world foo bar")
        msg2_id = mgr.store.insert_message(
            content="another message", source="manual", key="test_search_tracer_msg2"
        )
        mgr.semantic.index_message(msg2_id, "another message")

        # 触发搜索（query 用 test_ 前缀，便于 fixture 清理）
        results = mgr.semantic.search("test_hello", top_k=5)
        assert isinstance(results, list)

        # 应该有 trace 被创建
        recent = mgr.search_tracer.get_recent(limit=20)
        # 过滤出 test_ 前缀的
        test_traces = [t for t in recent if t["query"].startswith("test_")]
        assert len(test_traces) >= 1
        latest = test_traces[0]
        assert latest["query"] == "test_hello"
        assert latest["mode"] in ("hybrid", "bm25_only", "vector_only", "error")
        assert latest["error"] is None

    def test_search_trace_records_subsearches(self, memory_manager):
        """search trace 的 candidates 应包含子检索信息"""
        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        msg_id = mgr.store.insert_message(
            content="alpha bravo charlie", source="manual", key="test_search_tracer_msg3"
        )
        mgr.semantic.index_message(msg_id, "alpha bravo charlie")

        mgr.semantic.search("test_alpha", top_k=5)

        recent = mgr.search_tracer.get_recent(limit=20)
        test_traces = [t for t in recent if t["query"].startswith("test_")]
        assert len(test_traces) >= 1
        trace_id = test_traces[0]["id"]

        detail = mgr.search_tracer.get_detail(trace_id)
        assert detail is not None
        # candidates 应含 subsearches 列表
        candidates = detail["candidates"]
        assert "subsearches" in candidates
        assert isinstance(candidates["subsearches"], list)
        assert len(candidates["subsearches"]) >= 1
        # 每条 subsearch 应有 kind/count/latency_ms
        for s in candidates["subsearches"]:
            assert "kind" in s
            assert "count" in s
            assert "latency_ms" in s


# ==================== HTTP 端点 ====================

class TestSearchTracerHTTP:
    """HTTP 端点测试"""

    def test_list_traces_endpoint(self, client):
        """GET /memory/search/traces 应返回 traces 列表"""
        resp = client.get("/memory/search/traces", params={"limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data
        assert "total" in data
        assert isinstance(data["traces"], list)

    def test_get_detail_404(self, client):
        """GET /memory/search/traces/{id} 不存在应返回 404"""
        resp = client.get("/memory/search/traces/99999999")
        assert resp.status_code == 404

    def test_cleanup_endpoint(self, client):
        """DELETE /memory/search/traces/cleanup 应正常返回

        使用 days=365（1年）避免删除近期 trace。
        """
        resp = client.delete("/memory/search/traces/cleanup", params={"days": 365})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "result" in data
        assert "deleted" in data["result"]

    def test_full_flow_via_http(self, client):
        """完整流程：写入消息 → 搜索 → 查询 trace → 查询详情"""
        # 1. 写入消息
        client.post("/memory/test_search_tracer_http", json={
            "data": {"content": "hello world from http test"},
        })
        # 2. 搜索（触发 trace）
        resp = client.post("/memory/search", json={
            "query": "test_http_query", "top_k": 5,
        })
        assert resp.status_code == 200
        # 3. 列出 traces，找我们刚创建的
        resp = client.get("/memory/search/traces", params={"limit": 50})
        assert resp.status_code == 200
        traces = resp.json()["traces"]
        test_traces = [t for t in traces if t["query"] == "test_http_query"]
        if test_traces:
            trace_id = test_traces[0]["id"]
            # 4. 查询详情
            resp = client.get(f"/memory/search/traces/{trace_id}")
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["id"] == trace_id
            assert detail["query"] == "test_http_query"
            assert "candidates" in detail
            assert "final_results" in detail
        # 5. 清理
        client.delete("/memory/test_search_tracer_http")
        # 清理 traces（用直接 SQL，避免 HTTP 端点清理整个 DB）
        from server.memory.manager import get_memory_manager
        mgr = get_memory_manager()
        mgr.store.conn.execute("DELETE FROM search_traces WHERE query = 'test_http_query'")
        mgr.store.conn.commit()
