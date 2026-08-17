"""SQLite 数据库 schema 和迁移"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

SCHEMA_SQL = """
-- 原始消息（时间索引）
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent',   -- 'agent', 'user', 'tool', 'system', 'manual'
    role TEXT NOT NULL DEFAULT 'assistant', -- 'user', 'assistant', 'tool', 'system'
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    session_id TEXT,
    key TEXT,                               -- 向后兼容 KV 记忆的 key
    compressed INTEGER DEFAULT 0,           -- 是否已被压缩
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source);
CREATE INDEX IF NOT EXISTS idx_messages_key ON messages(key);
CREATE INDEX IF NOT EXISTS idx_messages_compressed ON messages(compressed);

-- 压缩摘要
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_time_range ON summaries(start_time, end_time);

-- 事实（提取的关键信息）
-- v3 新增列：fact_type / occurred_at / mentioned_at / consumption_contexts / trigger_keywords
-- 这些列允许 NULL，向后兼容 v2 数据（迁移时不回填，新写入自动填）
-- v6.1 T29：fact_type 收敛为 5 类 closed（user/feedback/project/reference/experience）
--   - 旧值 'preference' 由 migrate_fact_type_v6_1.py 迁移为 'user'
--   - 旧值 'transaction' 归档后删除（业务数据走 todos/wip）
--   - 写入端 Pydantic 加 Literal 约束（君子协议：未知类型落 NULL + log warning，不强制拒绝）
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',    -- 'auto', 'manual'
    confidence REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    fact_type TEXT,                          -- v6.1 closed: 'user' | 'feedback' | 'project' | 'reference' | 'experience'（NULL=未知/旧数据）
    occurred_at TEXT,                       -- ISO 时间，事实发生时间（区别于写入时间）
    mentioned_at TEXT,                       -- ISO 时间，最近被引用时间
    consumption_contexts TEXT,              -- JSON 数组字符串，如 ["recurring.accounting", "adhoc.web_archive"]
    trigger_keywords TEXT                   -- JSON 数组字符串，如 ["退款", "curl"]
);

CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
CREATE INDEX IF NOT EXISTS idx_facts_mentioned ON facts(mentioned_at);

-- 向量索引（元数据 + 向量 BLOB）
CREATE TABLE IF NOT EXISTS vector_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,                   -- numpy 数组序列化
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vector_message ON vector_index(message_id);
CREATE INDEX IF NOT EXISTS idx_vector_hash ON vector_index(text_hash);

-- BM25 倒排索引
CREATE TABLE IF NOT EXISTS bm25_inverted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tf INTEGER NOT NULL DEFAULT 1,          -- 词频
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_bm25_term ON bm25_inverted(term);
CREATE INDEX IF NOT EXISTS idx_bm25_message ON bm25_inverted(message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bm25_term_message ON bm25_inverted(term, message_id);

-- BM25 文档统计
CREATE TABLE IF NOT EXISTS bm25_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id = 1),
    total_docs INTEGER NOT NULL DEFAULT 0,
    avg_dl REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Schema 版本
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 记忆元数据（维护器写入：老化标记 + LLM 验证结果）
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
    stale_score REAL DEFAULT 0,           -- 距上次更新的天数
    last_validated_at TEXT,               -- 上次 LLM 验证时间 (ISO)
    validation_status TEXT DEFAULT 'pending',  -- pending/keep/update/archive
    validation_note TEXT DEFAULT ''        -- LLM 给出的原因
);
CREATE INDEX IF NOT EXISTS idx_memory_meta_status ON memory_meta(validation_status);

-- Evidence Ledger (v3) — 证据组织层
-- 记录每次 memory_search 返回的证据集，用于事后审计
-- organize_evidence() 把多源召回（messages + facts + summaries）组织成结构化证据
CREATE TABLE IF NOT EXISTS evidence_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,                       -- 触发查询
    search_trace_id INTEGER,                   -- 关联 search_traces.id
    source_type TEXT NOT NULL,                 -- 'message' | 'fact' | 'summary'
    source_id INTEGER NOT NULL,                -- 对应表的主键 id
    content_preview TEXT NOT NULL,             -- 内容前 500 字符
    timestamp REAL,                            -- 消息时间戳 / fact.updated_at epoch
    retrieval_score REAL NOT NULL,             -- 检索打分 0-1
    source_tag TEXT,                           -- 来源标签 (vl_summary/bill_summary 等)
    confidence REAL DEFAULT 1.0,
    corroboration_count INTEGER DEFAULT 0,    -- 佐证数（其他证据支持此条）
    conflicting_ids TEXT,                     -- JSON 数组：冲突证据 id 列表
    matched_by TEXT,                           -- 'bm25' | 'vector' | 'both' | 'fact_kv' | 'summary'
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_query ON evidence_ledger(query);
CREATE INDEX IF NOT EXISTS idx_evidence_ts ON evidence_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_evidence_trace_id ON evidence_ledger(search_trace_id);

-- Search Traces (v3) — 检索可观测性
-- 记录每次 search 的完整过程（query/params/子检索结果/scores/latency/error）
-- 便于事后诊断：为什么某条记忆没被召回？为什么某条无关记忆被排在前？
CREATE TABLE IF NOT EXISTS search_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                          -- ISO 时间
    query TEXT NOT NULL,
    params TEXT NOT NULL,                      -- JSON: top_k/since/until/source
    mode TEXT NOT NULL,                        -- 'hybrid' | 'bm25_only' | 'vector_only' | 'error'
    latency_ms INTEGER NOT NULL,
    bm25_count INTEGER DEFAULT 0,
    vector_count INTEGER DEFAULT 0,
    final_count INTEGER NOT NULL,
    candidates TEXT NOT NULL,                  -- JSON: 前 N 条候选（含子检索分数）
    final_results TEXT NOT NULL,              -- JSON: 最终返回的结果
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON search_traces(ts);

-- 初始化 BM25 统计行
INSERT OR IGNORE INTO bm25_stats (id, total_docs, avg_dl) VALUES (1, 0, 0.0);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """初始化数据库，创建表和索引。

    新库：执行 SCHEMA_SQL 后直接写入当前版本号。
    旧库：先 migrate_db() 升级 schema（添加新列/新表/新索引），再执行 SCHEMA_SQL 幂等补建。
    顺序很重要：旧库的 facts 表可能缺 v3 新列，SCHEMA_SQL 中的 CREATE INDEX 会因列不存在而失败，
    所以必须先 migrate_db 添加列，再 executescript(SCHEMA_SQL)。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    # 检查 schema_info 是否存在（区分新库 vs 旧库）
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'"
    )
    schema_info_exists = cur.fetchone() is not None

    if schema_info_exists:
        # 旧库：先迁移（添加新列、创建新表、创建新索引）
        # 注意：此时旧库的 facts 表可能缺 v3 新列，不能直接 executescript(SCHEMA_SQL)
        # 因为 SCHEMA_SQL 中的 CREATE INDEX 会因列不存在而失败
        version_row = conn.execute(
            "SELECT value FROM schema_info WHERE key = 'version'"
        ).fetchone()
        existing_version = int(version_row[0]) if version_row else 0
        if existing_version < SCHEMA_VERSION:
            migrate_db(conn)

    # 执行 SCHEMA_SQL（幂等：新库创建所有表，旧库补建缺失部分）
    # 此时旧库的新列已存在，CREATE INDEX 不会因列缺失而失败
    conn.executescript(SCHEMA_SQL)

    # 新库写入版本号（旧库已在 migrate_db 中更新）
    if not schema_info_exists:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()
    return conn


def migrate_db(conn: sqlite3.Connection) -> None:
    """版本迁移入口"""
    cur = conn.execute("SELECT value FROM schema_info WHERE key = 'version'")
    row = cur.fetchone()
    version = int(row[0]) if row else 0

    if version < 2:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE,
                stale_score REAL DEFAULT 0,
                last_validated_at TEXT,
                validation_status TEXT DEFAULT 'pending',
                validation_note TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_memory_meta_status ON memory_meta(validation_status);
        """)
        logger.info("迁移到 schema v2: 添加 memory_meta 表（记忆维护器）")

    if version < 3:
        # 幂等添加 facts 新列（ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS）
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
        new_cols = [
            ("fact_type", "TEXT"),
            ("occurred_at", "TEXT"),
            ("mentioned_at", "TEXT"),
            ("consumption_contexts", "TEXT"),
            ("trigger_keywords", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col_name} {col_type}")
                logger.info(f"迁移到 schema v3: 添加 facts.{col_name} 列")

        # 新增 Evidence Ledger 和 Search Traces 表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS evidence_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                search_trace_id INTEGER,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                content_preview TEXT NOT NULL,
                timestamp REAL,
                retrieval_score REAL NOT NULL,
                source_tag TEXT,
                confidence REAL DEFAULT 1.0,
                corroboration_count INTEGER DEFAULT 0,
                conflicting_ids TEXT,
                matched_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_query ON evidence_ledger(query);
            CREATE INDEX IF NOT EXISTS idx_evidence_ts ON evidence_ledger(timestamp);
            CREATE INDEX IF NOT EXISTS idx_evidence_trace_id ON evidence_ledger(search_trace_id);

            CREATE TABLE IF NOT EXISTS search_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                query TEXT NOT NULL,
                params TEXT NOT NULL,
                mode TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                bm25_count INTEGER DEFAULT 0,
                vector_count INTEGER DEFAULT 0,
                final_count INTEGER NOT NULL,
                candidates TEXT NOT NULL,
                final_results TEXT NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_traces_ts ON search_traces(ts);

            CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
            CREATE INDEX IF NOT EXISTS idx_facts_mentioned ON facts(mentioned_at);
        """)
        logger.info("迁移到 schema v3: facts 结构化字段 + evidence_ledger + search_traces")

    if version < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()
