"""Schema v3 迁移测试

覆盖：
- 新库直接建 v3 schema（SCHEMA_VERSION=3）
- v2 → v3 迁移：facts 新列、evidence_ledger、search_traces 表
- 迁移幂等性（重复调用不报错）
- 旧 facts 数据在迁移后仍可查询

不覆盖：EvidenceLedger/SearchTracer 业务逻辑（见对应专门测试）。
"""

import gc
import os
import sqlite3
import tempfile

import pytest

from server.memory.schema import SCHEMA_VERSION, init_db, migrate_db


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Windows 上 sqlite 连接未关闭会导致文件无法删除
    # 强制 GC + 多次尝试删除（避免 WAL/SHM 文件占用）
    gc.collect()
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        if os.path.exists(p):
            try:
                os.unlink(p)
            except (PermissionError, OSError):
                pass


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """检查表是否存在指定列"""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _has_index(conn: sqlite3.Connection, index: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,))
    return cur.fetchone() is not None


# ==================== 新库直接 v3 ====================

class TestFreshDbV3:
    def test_fresh_db_has_version_3(self, tmp_db):
        conn = init_db(tmp_db)
        cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
        conn.close()

    def test_fresh_db_has_facts_new_columns(self, tmp_db):
        conn = init_db(tmp_db)
        for col in ["fact_type", "occurred_at", "mentioned_at", "consumption_contexts", "trigger_keywords"]:
            assert _has_column(conn, "facts", col), f"facts.{col} 不存在"
        conn.close()

    def test_fresh_db_has_evidence_ledger_table(self, tmp_db):
        conn = init_db(tmp_db)
        assert _has_table(conn, "evidence_ledger")
        for col in ["id", "query", "search_trace_id", "source_type", "source_id",
                    "content_preview", "timestamp", "retrieval_score", "source_tag",
                    "confidence", "corroboration_count", "conflicting_ids", "matched_by", "created_at"]:
            assert _has_column(conn, "evidence_ledger", col), f"evidence_ledger.{col} 不存在"
        conn.close()

    def test_fresh_db_has_search_traces_table(self, tmp_db):
        conn = init_db(tmp_db)
        assert _has_table(conn, "search_traces")
        for col in ["id", "ts", "query", "params", "mode", "latency_ms",
                    "bm25_count", "vector_count", "final_count",
                    "candidates", "final_results", "error"]:
            assert _has_column(conn, "search_traces", col), f"search_traces.{col} 不存在"
        conn.close()

    def test_fresh_db_has_new_indexes(self, tmp_db):
        conn = init_db(tmp_db)
        for idx in ["idx_facts_type", "idx_facts_mentioned",
                    "idx_evidence_query", "idx_evidence_ts", "idx_evidence_trace_id",
                    "idx_traces_ts"]:
            assert _has_index(conn, idx), f"索引 {idx} 不存在"
        conn.close()


# ==================== v2 → v3 迁移 ====================

def _build_v2_db(db_path: str) -> sqlite3.Connection:
    """手动构建一个 v2 schema 的库（不包含 v3 新增表/列），用于测试迁移"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    # 不调 init_db，直接手写 v2 schema
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent',
            role TEXT NOT NULL DEFAULT 'assistant',
            content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            session_id TEXT,
            key TEXT,
            compressed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            summary TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'auto',
            confidence REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE vector_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            text_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            model_name TEXT NOT NULL,
            dim INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE bm25_inverted (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            tf INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE bm25_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id = 1),
            total_docs INTEGER NOT NULL DEFAULT 0,
            avg_dl REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE memory_meta (
            key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
            stale_score REAL DEFAULT 0,
            last_validated_at TEXT,
            validation_status TEXT DEFAULT 'pending',
            validation_note TEXT DEFAULT ''
        );
        INSERT INTO bm25_stats (id, total_docs, avg_dl) VALUES (1, 0, 0.0);
        INSERT INTO schema_info (key, value) VALUES ('version', '2');
    """)
    # 插入一些旧数据
    conn.execute(
        "INSERT INTO facts (key, value, source) VALUES (?, ?, ?)",
        ("old_fact", '{"name":"test","consumption_contexts":["recurring.accounting"]}', "manual"),
    )
    conn.commit()
    return conn


class TestV2ToV3Migration:
    def test_v2_to_v3_adds_new_columns(self, tmp_db):
        conn = _build_v2_db(tmp_db)
        # 迁移前 facts 没有 fact_type 等列
        assert not _has_column(conn, "facts", "fact_type")
        # 触发迁移
        migrate_db(conn)
        # 迁移后新列存在
        for col in ["fact_type", "occurred_at", "mentioned_at", "consumption_contexts", "trigger_keywords"]:
            assert _has_column(conn, "facts", col), f"迁移后 facts.{col} 应存在"
        conn.close()

    def test_v2_to_v3_creates_new_tables(self, tmp_db):
        conn = _build_v2_db(tmp_db)
        assert not _has_table(conn, "evidence_ledger")
        assert not _has_table(conn, "search_traces")
        migrate_db(conn)
        assert _has_table(conn, "evidence_ledger")
        assert _has_table(conn, "search_traces")
        conn.close()

    def test_v2_to_v3_updates_version(self, tmp_db):
        conn = _build_v2_db(tmp_db)
        cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == 2
        migrate_db(conn)
        cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == 3
        conn.close()

    def test_v2_to_v3_preserves_old_data(self, tmp_db):
        """迁移不应丢失旧 facts 数据"""
        conn = _build_v2_db(tmp_db)
        migrate_db(conn)
        # 旧数据还在
        cur = conn.execute("SELECT value, source FROM facts WHERE key=?", ("old_fact",))
        row = cur.fetchone()
        assert row is not None
        assert "consumption_contexts" in row[0]
        assert row[1] == "manual"
        conn.close()

    def test_v2_to_v3_old_facts_have_null_new_columns(self, tmp_db):
        """迁移后旧 facts 的新列应为 NULL（不回填）"""
        conn = _build_v2_db(tmp_db)
        migrate_db(conn)
        cur = conn.execute(
            "SELECT fact_type, occurred_at, mentioned_at, consumption_contexts, trigger_keywords "
            "FROM facts WHERE key=?",
            ("old_fact",),
        )
        row = cur.fetchone()
        assert row == (None, None, None, None, None)
        conn.close()

    def test_v2_to_v3_creates_indexes(self, tmp_db):
        conn = _build_v2_db(tmp_db)
        migrate_db(conn)
        for idx in ["idx_facts_type", "idx_facts_mentioned",
                    "idx_evidence_query", "idx_evidence_ts", "idx_evidence_trace_id",
                    "idx_traces_ts"]:
            assert _has_index(conn, idx), f"迁移后索引 {idx} 应存在"
        conn.close()


# ==================== 迁移幂等性 ====================

class TestMigrationIdempotent:
    def test_migration_idempotent_v3(self, tmp_db):
        """v3 库再跑迁移不应报错"""
        conn = init_db(tmp_db)
        # 第一次迁移（已是 v3，应 noop）
        migrate_db(conn)
        cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == 3

        # 表和列都还在
        assert _has_table(conn, "evidence_ledger")
        assert _has_table(conn, "search_traces")
        assert _has_column(conn, "facts", "fact_type")
        conn.close()

    def test_migration_idempotent_after_v2_to_v3(self, tmp_db):
        """v2 → v3 迁移后，再跑 migrate_db 应幂等不报错"""
        conn = _build_v2_db(tmp_db)
        migrate_db(conn)  # v2 → v3
        migrate_db(conn)  # 再跑一次，应 noop
        cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == 3
        # 所有表和列都还在
        assert _has_table(conn, "evidence_ledger")
        assert _has_column(conn, "facts", "fact_type")
        conn.close()


# ==================== init_db 集成 ====================

class TestInitDbIntegration:
    def test_init_db_on_v2_db_migrates_to_v3(self, tmp_db):
        """init_db 对已有 v2 库应自动迁移到 v3"""
        # 先建 v2 库
        conn1 = _build_v2_db(tmp_db)
        conn1.close()
        # 用 init_db 打开
        conn2 = init_db(tmp_db)
        cur = conn2.execute("SELECT value FROM schema_info WHERE key='version'")
        assert int(cur.fetchone()[0]) == 3
        assert _has_column(conn2, "facts", "fact_type")
        assert _has_table(conn2, "evidence_ledger")
        # 旧数据保留
        cur = conn2.execute("SELECT value FROM facts WHERE key=?", ("old_fact",))
        assert cur.fetchone() is not None
        conn2.close()
