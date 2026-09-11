"""MemoryStore 单元测试

覆盖：消息/摘要/事实 CRUD、向量索引、BM25 倒排索引、统计、
_row_to_dict 白名单校验（P0-5 SQL 注入防护）。

不覆盖：MemoryMaintainer（见 test_maintainer.py）、API 端点（见 test_memory.py）、
向量相似度计算（需 numpy + 模型，属集成测试）。
"""

import os
import tempfile

import pytest

from server.memory.store import MemoryStore

# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()


# ==================== 消息 CRUD ====================

class TestMessages:
    def test_insert_and_get(self, store):
        msg_id = store.insert_message(content="hello", source="agent", role="assistant")
        assert isinstance(msg_id, int)
        assert msg_id > 0

        msg = store.get_message(msg_id)
        assert msg is not None
        assert msg["id"] == msg_id
        assert msg["content"] == "hello"
        assert msg["source"] == "agent"
        assert msg["role"] == "assistant"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_message(99999) is None

    def test_insert_with_metadata(self, store):
        msg_id = store.insert_message(
            content="test",
            metadata={"tool": "ocr", "latency": 1.5},
        )
        msg = store.get_message(msg_id)
        import json
        meta = json.loads(msg["metadata"])
        assert meta["tool"] == "ocr"
        assert meta["latency"] == 1.5

    def test_insert_with_custom_timestamp(self, store):
        ts = 1000000.0
        msg_id = store.insert_message(content="timed", timestamp=ts)
        msg = store.get_message(msg_id)
        assert msg["timestamp"] == ts

    def test_query_by_source(self, store):
        store.insert_message(content="a", source="agent")
        store.insert_message(content="b", source="user")
        store.insert_message(content="c", source="agent")

        results = store.query_messages(source="agent", limit=10)
        assert len(results) == 2
        assert all(r["source"] == "agent" for r in results)

    def test_query_by_key(self, store):
        store.insert_message(content="x", key="session1")
        store.insert_message(content="y", key="session2")
        store.insert_message(content="z", key="session1")

        results = store.query_messages(key="session1", limit=10)
        assert len(results) == 2
        assert all(r["key"] == "session1" for r in results)

    def test_query_by_time_range(self, store):
        store.insert_message(content="old", timestamp=1000.0)
        store.insert_message(content="mid", timestamp=2000.0)
        store.insert_message(content="new", timestamp=3000.0)

        results = store.query_messages(since=1500.0, until=2500.0, limit=10)
        assert len(results) == 1
        assert results[0]["content"] == "mid"

    def test_query_ordered_by_timestamp_desc(self, store):
        store.insert_message(content="first", timestamp=1000.0)
        store.insert_message(content="second", timestamp=2000.0)
        store.insert_message(content="third", timestamp=3000.0)

        results = store.query_messages(limit=10)
        assert results[0]["content"] == "third"
        assert results[-1]["content"] == "first"

    def test_count_uncompressed(self, store):
        store.insert_message(content="a")
        store.insert_message(content="b")
        store.insert_message(content="c")
        assert store.count_uncompressed() == 3

    def test_mark_compressed(self, store):
        id1 = store.insert_message(content="a")
        id2 = store.insert_message(content="b")
        store.mark_compressed([id1, id2])
        assert store.count_uncompressed() == 0

    def test_mark_compressed_empty_list(self, store):
        store.insert_message(content="a")
        store.mark_compressed([])
        assert store.count_uncompressed() == 1

    def test_delete_message(self, store):
        msg_id = store.insert_message(content="delete-me")
        store.delete_message(msg_id)
        assert store.get_message(msg_id) is None


# ==================== 摘要 ====================

class TestSummaries:
    def test_insert_and_query(self, store):
        sid = store.insert_summary(
            start_time=1000.0, end_time=2000.0,
            summary="period A", message_count=5,
        )
        assert isinstance(sid, int)
        assert sid > 0

        results = store.query_summaries()
        assert len(results) == 1
        assert results[0]["summary"] == "period A"
        assert results[0]["message_count"] == 5

    def test_query_by_time_range(self, store):
        store.insert_summary(start_time=1000.0, end_time=2000.0, summary="A", message_count=1)
        store.insert_summary(start_time=3000.0, end_time=4000.0, summary="B", message_count=2)

        results = store.query_summaries(since=2500.0)
        assert len(results) == 1
        assert results[0]["summary"] == "B"

    def test_query_with_model(self, store):
        store.insert_summary(start_time=1.0, end_time=2.0, summary="x", message_count=1, model="gpt-4")
        results = store.query_summaries()
        assert results[0]["model"] == "gpt-4"

    def test_query_limit(self, store):
        for i in range(10):
            store.insert_summary(start_time=float(i), end_time=float(i+1), summary=f"s{i}", message_count=1)
        results = store.query_summaries(limit=3)
        assert len(results) == 3


# ==================== 事实 ====================

class TestFacts:
    def test_upsert_inserts_new(self, store):
        store.upsert_fact(key="user.name", value="alice", source="manual")
        fact = store.get_fact("user.name")
        assert fact is not None
        assert fact["value"] == "alice"
        assert fact["source"] == "manual"

    def test_upsert_updates_existing(self, store):
        store.upsert_fact(key="user.age", value="30")
        store.upsert_fact(key="user.age", value="31")
        fact = store.get_fact("user.age")
        assert fact["value"] == "31"

    def test_get_fact_increments_access_count(self, store):
        store.upsert_fact(key="k1", value="v1")
        store.get_fact("k1")
        store.get_fact("k1")
        fact = store.get_fact("k1")
        assert fact["access_count"] >= 2

    def test_get_fact_meta_no_increment(self, store):
        store.upsert_fact(key="k2", value="v2")
        store.get_fact_meta("k2")
        store.get_fact_meta("k2")
        fact = store.get_fact_meta("k2")
        assert fact["access_count"] == 0

    def test_get_nonexistent_fact_returns_none(self, store):
        assert store.get_fact("nonexistent") is None
        assert store.get_fact_meta("nonexistent") is None

    def test_list_facts_all(self, store):
        store.upsert_fact(key="a", value="1", source="auto")
        store.upsert_fact(key="b", value="2", source="manual")
        facts = store.list_facts()
        assert len(facts) == 2

    def test_list_facts_by_source(self, store):
        store.upsert_fact(key="a", value="1", source="auto")
        store.upsert_fact(key="b", value="2", source="manual")
        store.upsert_fact(key="c", value="3", source="auto")

        facts = store.list_facts(source="auto")
        assert len(facts) == 2
        assert all(f["source"] == "auto" for f in facts)

    def test_list_facts_limit(self, store):
        for i in range(10):
            store.upsert_fact(key=f"k{i}", value=str(i))
        facts = store.list_facts(limit=5)
        assert len(facts) == 5

    def test_delete_fact(self, store):
        store.upsert_fact(key="to-delete", value="v")
        assert store.delete_fact("to-delete") is True
        assert store.get_fact("to-delete") is None

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete_fact("nonexistent") is False


# ==================== 向量索引 ====================

class TestVectors:
    def test_insert_and_search(self, store):
        msg_id = store.insert_message(content="vectorized text")
        vid = store.insert_vector(
            message_id=msg_id,
            text_hash="abc123",
            vector_blob=b"\x00\x01\x02\x03",
            model_name="test-embed",
            dim=4,
        )
        assert isinstance(vid, int)
        assert vid > 0

        results = store.get_vectors_for_search(model_name="test-embed")
        assert len(results) == 1
        assert results[0]["message_id"] == msg_id
        assert results[0]["model_name"] == "test-embed"
        assert results[0]["dim"] == 4

    def test_get_all_vectors(self, store):
        msg_id = store.insert_message(content="text")
        store.insert_vector(msg_id, "h1", b"\x00", "model_a", 1)
        store.insert_vector(msg_id, "h2", b"\x01", "model_b", 1)

        results = store.get_vectors_for_search()
        assert len(results) == 2

    def test_delete_vectors_by_message(self, store):
        msg_id = store.insert_message(content="text")
        store.insert_vector(msg_id, "h1", b"\x00", "m", 1)
        store.insert_vector(msg_id, "h2", b"\x01", "m", 1)

        store.delete_vectors_by_message(msg_id)
        results = store.get_vectors_for_search()
        assert len(results) == 0


# ==================== BM25 ====================

class TestBM25:
    def test_upsert_and_get_posting(self, store):
        msg_id = store.insert_message(content="hello world")
        store.upsert_bm25_term("hello", msg_id, tf=1)
        store.upsert_bm25_term("world", msg_id, tf=1)

        hello_postings = store.get_bm25_posting("hello")
        assert len(hello_postings) == 1
        assert hello_postings[0]["message_id"] == msg_id
        assert hello_postings[0]["tf"] == 1

    def test_upsert_accumulates_tf(self, store):
        msg_id = store.insert_message(content="repeat repeat")
        store.upsert_bm25_term("repeat", msg_id, tf=1)
        store.upsert_bm25_term("repeat", msg_id, tf=2)
        store.upsert_bm25_term("repeat", msg_id, tf=1)

        postings = store.get_bm25_posting("repeat")
        assert postings[0]["tf"] == 4

    def test_get_posting_nonexistent(self, store):
        assert store.get_bm25_posting("nonexistent") == []

    def test_bm25_stats_default(self, store):
        stats = store.get_bm25_stats()
        assert stats["total_docs"] == 0
        assert stats["avg_dl"] == 0.0

    def test_update_bm25_stats(self, store):
        store.update_bm25_stats(total_docs=10, avg_dl=5.5)
        stats = store.get_bm25_stats()
        assert stats["total_docs"] == 10
        assert stats["avg_dl"] == 5.5

    def test_get_message_length(self, store):
        msg_id = store.insert_message(content="hello world foo")
        assert store.get_message_length(msg_id) == 15  # len("hello world foo")

    def test_get_message_length_nonexistent(self, store):
        assert store.get_message_length(99999) == 0


# ==================== 统计 ====================

class TestStats:
    def test_empty_stats(self, store):
        stats = store.get_stats()
        assert stats["messages"] == 0
        assert stats["summaries"] == 0
        assert stats["facts"] == 0
        assert stats["vectors"] == 0
        assert stats["bm25_terms"] == 0
        assert stats["db_size_bytes"] > 0

    def test_stats_with_data(self, store):
        store.insert_message(content="msg1")
        store.insert_message(content="msg2")
        store.upsert_fact(key="k", value="v")
        store.insert_summary(start_time=1.0, end_time=2.0, summary="s", message_count=1)

        stats = store.get_stats()
        assert stats["messages"] == 2
        assert stats["facts"] == 1
        assert stats["summaries"] == 1


# ==================== _row_to_dict 白名单（P0-5） ====================

class TestRowToDictWhitelist:
    """P0-5: SQL 注入防护 — 表名白名单校验"""

    def test_allowed_tables_list(self):
        """白名单应包含所有合法表"""
        expected = {
            "messages", "facts", "summaries", "vector_index",
            "bm25_inverted", "bm25_stats", "schema_info", "memory_meta",
            "evidence_ledger", "search_traces",  # v3 新增
        }
        assert expected == MemoryStore._ALLOWED_TABLES

    def test_valid_table_does_not_raise(self, store):
        """合法表名应正常转换"""
        store.insert_message(content="row-test")
        row = store.conn.execute("SELECT * FROM messages LIMIT 1").fetchone()
        d = store._row_to_dict("messages", row)
        assert d is not None
        assert d["content"] == "row-test"

    def test_facts_table_works(self, store):
        store.upsert_fact(key="whitelist_test", value="v")
        row = store.conn.execute("SELECT * FROM facts WHERE key = ?", ("whitelist_test",)).fetchone()
        d = store._row_to_dict("facts", row)
        assert d["key"] == "whitelist_test"

    def test_invalid_table_raises_value_error(self, store):
        """非法表名应抛出 ValueError"""
        row = (1,)
        with pytest.raises(ValueError, match="非法表名"):
            store._row_to_dict("evil_table; DROP TABLE messages;", row)

    def test_sql_injection_table_name_rejected(self, store):
        """SQL 注入尝试应被白名单拦截"""
        injection_attempts = [
            "messages; DROP TABLE facts; --",
            "messages UNION SELECT * FROM facts",
            "messages'; DROP TABLE facts; --",
            "sqlite_master",
            "information_schema",
            "",
        ]
        for evil in injection_attempts:
            with pytest.raises(ValueError, match="非法表名"):
                store._row_to_dict(evil, (1,))

    def test_allowed_tables_is_frozenset(self):
        """白名单应为 frozenset（不可变，防止运行时篡改）"""
        assert isinstance(MemoryStore._ALLOWED_TABLES, frozenset)


# ==================== 连接管理 ====================

class TestConnectionManagement:
    def test_lazy_initialize(self, tmp_db):
        """conn 属性访问时才初始化"""
        s = MemoryStore(tmp_db)
        assert s._conn is None
        _ = s.conn
        assert s._conn is not None
        s.close()

    def test_close_resets_connection(self, store):
        store.close()
        assert store._conn is None

    def test_close_idempotent(self, store):
        store.close()
        store.close()  # 不应抛异常
        assert store._conn is None
