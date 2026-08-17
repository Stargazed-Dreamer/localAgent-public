"""EvidenceLedger 测试 — 证据组织层

测试 EvidenceLedger 的三源合并、冲突检测、佐证计数、持久化、查询端点，
以及与 MemoryManager.search() 的集成。
"""

import os
import tempfile

import pytest

from server.memory.config import MemoryConfig
from server.memory.evidence import EvidenceLedger, _jaccard_similarity
from server.memory.store import MemoryStore

# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        for suffix in ("", "-wal", "-shm"):
            f = path + suffix
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()
    import gc
    gc.collect()


@pytest.fixture
def evidence_config():
    """测试用 MemoryConfig"""
    cfg = MemoryConfig()
    cfg.evidence_top_k = 5
    cfg.evidence_corroboration_threshold = 0.7
    cfg.evidence_conflict_window_minutes = 5
    cfg.evidence_audit_keep_days = 30
    return cfg


@pytest.fixture
def ledger(store, evidence_config):
    return EvidenceLedger(store=store, config=evidence_config)


@pytest.fixture
def memory_manager():
    """获取全局 MemoryManager 单例"""
    from server.memory.manager import get_memory_manager

    mgr = get_memory_manager()

    def _cleanup():
        try:
            # 清理测试数据
            mgr.store.conn.execute(
                "DELETE FROM evidence_ledger WHERE query LIKE 'test_%'"
            )
            mgr.store.conn.execute(
                "DELETE FROM facts WHERE key LIKE 'test_evidence_%'"
            )
            mgr.store.conn.execute(
                "DELETE FROM messages WHERE key LIKE 'test_evidence_%'"
            )
            mgr.store.conn.execute(
                "DELETE FROM search_traces WHERE query LIKE 'test_%'"
            )
            mgr.store.conn.commit()
        except Exception:
            pass

    _cleanup()
    yield mgr
    _cleanup()


# ==================== Jaccard 相似度 ====================

class TestJaccard:
    """Jaccard 相似度工具函数"""

    def test_identical_tokens(self):
        assert _jaccard_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    def test_no_overlap(self):
        assert _jaccard_similarity(["a", "b"], ["c", "d"]) == 0.0

    def test_partial_overlap(self):
        # |A∩B| = 1 ({b}), |A∪B| = 3 ({a,b,c}) → 1/3
        assert _jaccard_similarity(["a", "b"], ["b", "c"]) == 1 / 3
        # 更明显的部分重叠：|A∩B| = 2, |A∪B| = 4 → 0.5
        assert _jaccard_similarity(["a", "b", "c"], ["b", "c", "d"]) == 0.5

    def test_empty_tokens(self):
        assert _jaccard_similarity([], ["a"]) == 0.0
        assert _jaccard_similarity(["a"], []) == 0.0
        assert _jaccard_similarity([], []) == 0.0


# ==================== 基础组织 ====================

class TestOrganizeEvidence:
    """organize_evidence 基础测试"""

    def test_only_messages(self, ledger):
        """只有 messages 结果时应正常组织"""
        messages = [
            {"message_id": 1, "score": 0.9, "content": "hello world", "timestamp": 1000.0,
             "source": "manual"},
        ]
        result = ledger.organize_evidence(query="hello", message_results=messages)
        assert len(result) == 1
        e = result[0]
        assert e["source_type"] == "message"
        assert e["source_id"] == 1
        assert e["content_preview"] == "hello world"
        assert e["retrieval_score"] == 0.9
        assert e["matched_by"] == ""  # 没有 bm25_score/vector_score 字段

    def test_three_sources_merge(self, ledger):
        """三源合并：messages + facts + summaries"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "msg content",
             "timestamp": 1000.0, "source": "manual"},
        ]
        facts = [
            {"id": 1, "key": "test_fact", "value": "fact value", "confidence": 0.9,
             "updated_at": "2026-07-18 10:00:00"},
        ]
        summaries = [
            {"id": 1, "summary": "summary text", "end_time": 2000.0},
        ]
        result = ledger.organize_evidence(
            query="test", message_results=messages,
            fact_results=facts, summary_results=summaries,
        )
        # 至少应包含三源各一条
        source_types = {e["source_type"] for e in result}
        assert "message" in source_types
        assert "fact" in source_types
        assert "summary" in source_types

    def test_top_k_limit(self, ledger):
        """返回数量不应超过 evidence_top_k"""
        messages = [
            {"message_id": i, "score": 0.5 + i * 0.01, "content": f"msg{i}",
             "timestamp": 1000.0 + i, "source": "manual"}
            for i in range(20)
        ]
        result = ledger.organize_evidence(query="msg", message_results=messages)
        assert len(result) <= 5  # evidence_config.evidence_top_k = 5

    def test_score_clamped_to_01(self, ledger):
        """retrieval_score 应被限制在 [0, 1]"""
        messages = [
            {"message_id": 1, "score": 1.5, "content": "x", "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": -0.5, "content": "y", "timestamp": 1000.0, "source": "manual"},
        ]
        result = ledger.organize_evidence(query="x", message_results=messages)
        for e in result:
            assert 0.0 <= e["retrieval_score"] <= 1.0

    def test_matched_by_detection(self, ledger):
        """根据 bm25_score/vector_score 字段判断 matched_by"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "x", "timestamp": 1000.0,
             "source": "manual", "bm25_score": 0.7, "vector_score": 0.9},
            {"message_id": 2, "score": 0.8, "content": "y", "timestamp": 1000.0,
             "source": "manual", "bm25_score": 0.7},
            {"message_id": 3, "score": 0.8, "content": "z", "timestamp": 1000.0,
             "source": "manual", "vector_score": 0.9},
        ]
        result = ledger.organize_evidence(query="x", message_results=messages)
        matched_by_set = {e["matched_by"] for e in result}
        assert "both" in matched_by_set
        assert "bm25" in matched_by_set
        assert "vector" in matched_by_set


# ==================== 佐证计数 ====================

class TestCorroboration:
    """佐证计数测试"""

    def test_corroboration_for_similar_evidence(self, ledger):
        """相似度 >= threshold 的证据应互相佐证"""
        # 两条几乎相同的内容（Jaccard 应接近 1.0）
        messages = [
            {"message_id": 1, "score": 0.8, "content": "hello world foo bar",
             "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": 0.7, "content": "hello world foo bar",
             "timestamp": 1000.0, "source": "manual"},
        ]
        result = ledger.organize_evidence(query="hello", message_results=messages)
        # 两条都应有 corroboration_count >= 1（互相佐证）
        for e in result:
            assert e["corroboration_count"] >= 1

    def test_no_corroboration_for_dissimilar(self, ledger):
        """完全不同的证据不应互相佐证"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "alpha bravo charlie",
             "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": 0.7, "content": "xray yankee zulu",
             "timestamp": 1000.0, "source": "manual"},
        ]
        result = ledger.organize_evidence(query="alpha", message_results=messages)
        # 完全不同的内容 → 不应互相佐证
        for e in result:
            assert e["corroboration_count"] == 0


# ==================== 冲突检测 ====================

class TestConflictDetection:
    """冲突检测测试"""

    def test_conflict_for_same_time_low_jaccard_with_query_keyword(self, ledger):
        """同时间窗口 + Jaccard 低 + 都含 query 关键词 → 标记冲突"""
        # 两条同时（timestamp 相同），内容不同（Jaccard 低），都含 "test"
        messages = [
            {"message_id": 1, "score": 0.8, "content": "test alpha",
             "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": 0.7, "content": "test zulu",
             "timestamp": 1000.0, "source": "manual"},
        ]
        result = ledger.organize_evidence(query="test", message_results=messages)
        # 两条都应有 conflicting_ids 非空
        for e in result:
            assert len(e["conflicting_ids"]) >= 1

    def test_no_conflict_for_different_time(self, ledger):
        """时间窗口外的不应标记冲突"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "test alpha",
             "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": 0.7, "content": "test zulu",
             "timestamp": 1000.0 + 3600, "source": "manual"},  # 1小时后
        ]
        result = ledger.organize_evidence(query="test", message_results=messages)
        # 时间窗口外 → 不应标记冲突
        for e in result:
            assert len(e["conflicting_ids"]) == 0

    def test_no_conflict_for_similar_content(self, ledger):
        """Jaccard 高（相似度高）的不应标记冲突（被佐证逻辑吃掉）"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "test alpha bravo",
             "timestamp": 1000.0, "source": "manual"},
            {"message_id": 2, "score": 0.7, "content": "test alpha bravo",
             "timestamp": 1000.0, "source": "manual"},
        ]
        result = ledger.organize_evidence(query="test", message_results=messages)
        # 内容相同 → 走佐证逻辑，不应标记冲突
        for e in result:
            assert len(e["conflicting_ids"]) == 0


# ==================== 持久化与查询 ====================

class TestEvidencePersistence:
    """持久化和查询测试"""

    def test_evidence_persisted_to_db(self, ledger):
        """organize_evidence 应将候选写入 evidence_ledger 表"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "persist test",
             "timestamp": 1000.0, "source": "manual"},
        ]
        ledger.organize_evidence(query="persist_test", message_results=messages)

        recent = ledger.get_recent(limit=10)
        assert len(recent) >= 1
        # 最新一条应是刚写入的
        latest = recent[0]
        assert latest["query"] == "persist_test"
        assert latest["source_type"] == "message"
        assert latest["source_id"] == 1

    def test_get_detail_returns_full_record(self, ledger):
        """get_detail 应返回完整记录（含 conflicting_ids 解析）"""
        messages = [
            {"message_id": 42, "score": 0.9, "content": "detail test",
             "timestamp": 1000.0, "source": "manual"},
        ]
        ledger.organize_evidence(query="detail_test", message_results=messages)

        recent = ledger.get_recent(limit=1)
        eid = recent[0]["id"]
        detail = ledger.get_detail(eid)
        assert detail is not None
        assert detail["id"] == eid
        assert detail["query"] == "detail_test"
        assert detail["source_id"] == 42
        # conflicting_ids 应为 list（即使是空）
        assert isinstance(detail["conflicting_ids"], list)

    def test_get_detail_nonexistent(self, ledger):
        """get_detail 不存在的 id 应返回 None"""
        assert ledger.get_detail(99999999) is None

    def test_cleanup_old(self, ledger):
        """cleanup_old 应删除旧证据"""
        messages = [
            {"message_id": 1, "score": 0.8, "content": "old evidence",
             "timestamp": 1000.0, "source": "manual"},
        ]
        ledger.organize_evidence(query="cleanup_test", message_results=messages)

        # 手动把 created_at 改成 60 天前
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
        ledger.store.conn.execute(
            "UPDATE evidence_ledger SET created_at = ? WHERE query = ?",
            (old_ts, "cleanup_test"),
        )
        ledger.store.conn.commit()

        # 清理（30 天 retention）
        result = ledger.cleanup_old()
        assert result["deleted"] >= 1

        # 应已删除
        recent = ledger.get_recent(limit=100)
        cleaned = [e for e in recent if e["query"] == "cleanup_test"]
        assert len(cleaned) == 0


# ==================== 与 MemoryManager.search 集成 ====================

class TestEvidenceLedgerIntegration:
    """与 MemoryManager.search() 集成测试"""

    def test_search_returns_evidence_format(self, memory_manager):
        """启用 EvidenceLedger 时，search 应返回证据格式（含 source_type 等字段）"""
        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        # 确认 EvidenceLedger 已注入
        assert mgr.evidence_ledger is not None
        # 确认配置启用
        assert mgr.config.enable_evidence_ledger is True

        # 写入一个 fact（用 test_ 前缀便于清理）
        mgr.set("test_evidence_fact", {"content": "hello evidence test"}, structured={"fact_type": "experience"})

        # 搜索匹配的 query
        results = mgr.search("test_evidence", top_k=5)
        assert isinstance(results, list)
        # 启用 EvidenceLedger 时，每条结果应含 source_type 字段
        for r in results:
            assert "source_type" in r
            assert r["source_type"] in ("message", "fact", "summary")

    def test_search_creates_evidence_audit_records(self, memory_manager):
        """search 应在 evidence_ledger 表创建审计记录"""
        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        # 写入一个 fact
        mgr.set("test_evidence_audit", {"content": "audit test"})

        # 搜索
        mgr.search("test_evidence_audit", top_k=5)

        # 应有审计记录
        recent = mgr.evidence_ledger.get_recent(limit=20)
        audit_records = [r for r in recent if r["query"] == "test_evidence_audit"]
        assert len(audit_records) >= 1


# ==================== HTTP 端点 ====================

class TestEvidenceLedgerHTTP:
    """HTTP 端点测试"""

    def test_recent_endpoint(self, client):
        """GET /memory/evidence/recent 应返回证据列表"""
        resp = client.get("/memory/evidence/recent", params={"limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "evidences" in data
        assert "total" in data
        assert isinstance(data["evidences"], list)

    def test_detail_404(self, client):
        """GET /memory/evidence/{id} 不存在应返回 404"""
        resp = client.get("/memory/evidence/99999999")
        assert resp.status_code == 404

    def test_cleanup_endpoint(self, client):
        """DELETE /memory/evidence/cleanup 应正常返回"""
        resp = client.delete("/memory/evidence/cleanup", params={"days": 365})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "result" in data
        assert "deleted" in data["result"]
