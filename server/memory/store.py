"""SQLite 存储层 — 消息、摘要、事实的 CRUD"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

from server.memory.schema import init_db

logger = logging.getLogger(__name__)


class MemoryStore:
    """SQLite 存储层，线程安全（WAL 模式 + check_same_thread=False + 写锁）"""

    _write_lock: threading.Lock

    def __init__(self, db_path: str):
            """初始化数据库连接管理器。

            该方法为数据库连接管理器类的构造函数，负责设置数据库路径并初始化连接对象。
            主要用于后续建立和管理SQLite数据库连接。

            Args:
                db_path (str): SQLite数据库文件路径，用于后续建立连接。

            Returns:
                None: 该构造函数不返回任何值，仅初始化实例属性。
            """
            self.db_path = db_path  # 存储数据库文件路径，供后续连接使用
            self._conn: sqlite3.Connection | None = None  # 初始化数据库连接对象为None，表示尚未建立连接
            self._write_lock = threading.Lock()
            self._table_columns_cache: dict[str, list[str]] = {}  # 表名列名缓存（实例级）
            # Ticket 01：DB 大小采样历史（in-memory 7 天滚动，不新增 sqlite 表）
            self._db_size_history: dict[str, float] = {}

    def _commit(self) -> None:
        """线程安全的 commit（串行化写提交，避免并发 database is locked）"""
        with self._write_lock:
            self.conn.commit()

    def initialize(self) -> None:
        """初始化数据库连接和 schema（init_db 内部会按需调用 migrate_db）"""
        self._conn = init_db(self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        """
        获取数据库连接对象。

        该方法用于获取当前实例的 SQLite 数据库连接。如果连接尚未初始化（为 None），
        则会先调用 initialize 方法进行初始化，再返回连接对象。

        参数:
            self: 类实例自身，用于访问实例属性。

        返回值:
            sqlite3.Connection - 一个 SQLite 数据库连接对象。
        """
        if self._conn is None:  # 检查数据库连接是否已存在
            self.initialize()  # 若连接为空，则初始化数据库连接
        assert self._conn is not None  # initialize() 后必有连接，收窄返回类型
        return self._conn  # 返回数据库连接对象

        # ─── 消息 ───────────────────────────────────────────────

    def insert_message(
        self,
        content: str,
        source: str = "agent",
        role: str = "assistant",
        key: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
        timestamp: float | None = None,
    ) -> int:
        """插入一条消息，返回 id

        T06：INSERT + commit 包入 _write_lock，避免并发写导致 lastrowid 错乱。
        用 cur.lastrowid 替代 SELECT last_insert_rowid()（防多线程串扰）。
        """
        ts = timestamp or time.time()
        with self._write_lock:
            cur = self.conn.execute(
                """INSERT INTO messages (timestamp, source, role, content, metadata, session_id, key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, source, role, content, json.dumps(metadata or {}, ensure_ascii=False), session_id, key),
            )
            self.conn.commit()
            return cur.lastrowid or 0  # T06：用 cursor.lastrowid 而非 SELECT last_insert_rowid()

    def get_message(self, msg_id: int) -> dict | None:
        """
        根据消息ID从数据库中获取消息。

        参数:
            msg_id (int): 消息的ID。

        返回:
            Optional[dict]: 如果找到消息，返回表示消息的字典；否则返回None。
        """
        # 执行SQL查询以获取指定ID的消息
        cur = self.conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        # 从查询结果中获取一行
        row = cur.fetchone()
        # 如果没有找到行，返回None
        if not row:
            return None
        # 将行数据转换为字典并返回
        return self._row_to_dict("messages", row)

    def query_messages(
        self,
        source: str | None = None,
        key: str | None = None,
        session_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        compressed: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """按条件查询消息"""
        conditions = []
        params: list[Any] = []

        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if key is not None:
            conditions.append("key = ?")
            params.append(key)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)
        if compressed is not None:
            conditions.append("compressed = ?")
            params.append(compressed)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM messages WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cur = self.conn.execute(sql, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def count_uncompressed(self) -> int:
            """统计未压缩的消息数量

            查询数据库中messages表，计算compressed字段值为0（即未压缩）的记录总数。

            Args:
                self: 类实例本身，通过self.conn访问数据库连接

            Returns:
                int: 返回未压缩消息的数量
            """
            # 执行SQL查询，统计messages表中compressed=0的记录数量
            cur = self.conn.execute("SELECT COUNT(*) FROM messages WHERE compressed = 0")
            # 从查询结果中提取第一行第一列的值，即查询到的数量
            return cur.fetchone()[0]

    def mark_compressed(self, msg_ids: list[int]) -> None:
        """
        功能：将指定的消息ID列表标记为已压缩状态。

        参数：
        msg_ids (list[int]): 消息ID的列表。

        返回值：
        None
        """
        if not msg_ids:  # 如果消息ID列表为空，则直接返回
            return
        placeholders = ",".join("?" * len(msg_ids))  # 创建SQL查询的占位符字符串，用于防止SQL注入
        # T06：execute + commit 包入 _write_lock，避免并发写交叉
        with self._write_lock:
            self.conn.execute(
                f"UPDATE messages SET compressed = 1 WHERE id IN ({placeholders})",
                msg_ids,
            )
            self.conn.commit()

    def delete_message(self, msg_id: int) -> None:
        """删除数据库中指定ID的消息。

        功能：从消息表中删除给定ID的消息记录。
        参数：
        msg_id (int): 要删除的消息ID。
        返回值：无（None）。
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            self.conn.commit()

        # ─── 摘要 ───────────────────────────────────────────────

    def insert_summary(
        self,
        start_time: float,
        end_time: float,
        summary: str,
        message_count: int,
        model: str | None = None,
    ) -> int:
            """向数据库中插入一条摘要记录并返回新记录的行ID。

            Args:
                start_time (float): 摘要覆盖的时间段的开始时间戳。
                end_time (float): 摘要覆盖的时间段的结束时间戳。
                summary (str): 摘要文本内容。
                message_count (int): 摘要所覆盖的消息数量。
                model (Optional[str]): 生成摘要的模型名称，可为None。

            Returns:
                int: 新插入记录在数据库中的行ID。
            """
            # T06：INSERT + commit 包入 _write_lock，用 cur.lastrowid 替代 SELECT last_insert_rowid()
            with self._write_lock:
                cur = self.conn.execute(
                    """INSERT INTO summaries (start_time, end_time, summary, message_count, model)
                       VALUES (?, ?, ?, ?, ?)""",
                    (start_time, end_time, summary, message_count, model),
                )
                self.conn.commit()
                return cur.lastrowid or 0  # T06：cursor.lastrowid 而非 SELECT last_insert_rowid()

    def query_summaries(
        self,
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
            """根据时间范围查询数据库中的总结记录。

            参数:
                self: 类实例，用于访问数据库连接。
                since (Optional[float]): 查询开始时间的时间戳，如果提供，则筛选结束时间大于等于该值的记录。
                until (Optional[float]): 查询结束时间的时间戳，如果提供，则筛选开始时间小于等于该值的记录。
                limit (int): 查询结果的最大数量，默认为50。

            返回值:
                list[dict]: 一个字典列表，每个字典代表一条总结记录，包含数据库中的所有列。
            """
            conditions = []  # 存储SQL WHERE子句的条件列表
            params: list[Any] = []  # 存储SQL查询的参数列表，用于防止SQL注入
            if since is not None:  # 如果指定了开始时间
                conditions.append("end_time >= ?")  # 添加条件：记录结束时间必须大于等于指定时间
                params.append(since)  # 将开始时间添加到参数列表
            if until is not None:  # 如果指定了结束时间
                conditions.append("start_time <= ?")  # 添加条件：记录开始时间必须小于等于指定时间
                params.append(until)  # 将结束时间添加到参数列表
            where = " AND ".join(conditions) if conditions else "1=1"  # 如果有条件，则用AND连接；否则使用默认条件"1=1"（表示无条件）
            sql = f"SELECT * FROM summaries WHERE {where} ORDER BY start_time DESC LIMIT ?"  # 构造SQL查询语句，按开始时间降序排列，并限制结果数量
            params.append(limit)  # 将限制数量添加到参数列表
            cur = self.conn.execute(sql, params)  # 执行SQL查询，获取游标对象
            columns = [desc[0] for desc in cur.description]  # 从游标描述中提取列名，构建列名列表
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]  # 将查询结果转换为字典列表返回，每个字典对应一条记录

    def delete_summaries_before(self, end_time: float) -> int:
        """删除 end_time 早于给定时间戳的摘要记录，返回删除行数。

        5-9: router 此前直接 conn.execute(DELETE FROM summaries) 绕过 _write_lock
        约定，抽成 store 方法与其他写方法一致地包锁。
        """
        with self._write_lock:
            cur = self.conn.execute(
                "DELETE FROM summaries WHERE end_time < ?", (end_time,)
            )
            self.conn.commit()
            return cur.rowcount

            # ─── 事实 ───────────────────────────────────────────────

    def upsert_fact(
        self,
        key: str,
        value: str,
        source: str = "auto",
        confidence: float = 1.0,
        fact_type: str | None = None,
        occurred_at: str | None = None,
        consumption_contexts: list[str] | None = None,
        trigger_keywords: list[str] | None = None,
    ) -> None:
        """插入或更新一条事实记录。

        v3 新增：fact_type / occurred_at / consumption_contexts / trigger_keywords
        这些字段为可选，不传时保持现有值（更新场景）或 NULL（新建场景）。
        mentioned_at 由 get_fact 自动更新，不在此处设置。

        Args:
            key: 事实的唯一键
            value: 事实的值（通常为 JSON 字符串）
            source: 'auto' 或 'manual'
            confidence: 置信度 0-1
            fact_type: 事实类型 'preference'|'project'|'reference'|'experience'|'transaction'
            occurred_at: 事实发生时间 ISO 字符串
            consumption_contexts: 消费场景列表（写入时序列化为 JSON 字符串）
            trigger_keywords: 触发关键词列表（写入时序列化为 JSON 字符串）
        """
        contexts_json = json.dumps(consumption_contexts, ensure_ascii=False) if consumption_contexts is not None else None
        keywords_json = json.dumps(trigger_keywords, ensure_ascii=False) if trigger_keywords is not None else None
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO facts (key, value, source, confidence, fact_type, occurred_at,
                                       consumption_contexts, trigger_keywords, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       source = excluded.source,
                       confidence = excluded.confidence,
                       fact_type = COALESCE(excluded.fact_type, facts.fact_type),
                       occurred_at = COALESCE(excluded.occurred_at, facts.occurred_at),
                       consumption_contexts = COALESCE(excluded.consumption_contexts, facts.consumption_contexts),
                       trigger_keywords = COALESCE(excluded.trigger_keywords, facts.trigger_keywords),
                       updated_at = datetime('now', 'localtime')""",
                (key, value, source, confidence, fact_type, occurred_at, contexts_json, keywords_json),
            )
            self.conn.commit()

    def _update_fact_structured_fields(self, key: str, fields: dict) -> None:
        """更新 facts 表的 v3 结构化字段（fact_type/occurred_at/consumption_contexts/trigger_keywords）。

        仅更新非 None 字段，NULL 字段保留原值。用于 extract_facts_from_kv 双写策略。
        mentioned_at 不在此处更新（由 get_fact 自动维护）。
        """
        set_clauses = []
        params: list[Any] = []
        field_map = {
            "fact_type": fields.get("fact_type"),
            "occurred_at": fields.get("occurred_at"),
            "consumption_contexts": fields.get("consumption_contexts"),  # 已序列化为 JSON 字符串
            "trigger_keywords": fields.get("trigger_keywords"),  # 已序列化为 JSON 字符串
        }
        for col, val in field_map.items():
            if val is not None:
                set_clauses.append(f"{col} = ?")
                params.append(val)
        if not set_clauses:
            return
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.append(key)
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute(
                f"UPDATE facts SET {', '.join(set_clauses)} WHERE key = ?",
                params,
            )
            self.conn.commit()

    def get_fact(self, key: str) -> dict | None:
        """根据给定的key查询事实，并更新该事实的访问计数和 mentioned_at，然后返回事实的字典形式。

        v3 新增：mentioned_at 字段在每次访问时自动更新为当前时间。
        返回的 dict 包含更新后的 mentioned_at 和 access_count 值。

        T06：SELECT + UPDATE + commit + 重新 SELECT 包入 _write_lock，保证访问计数递增原子性。
        """
        # T06：整个读-改-写序列包入 _write_lock，避免并发 access_count 丢失更新
        with self._write_lock:
            cur = self.conn.execute("SELECT * FROM facts WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            now_iso = datetime.now().isoformat(timespec="seconds")
            self.conn.execute(
                "UPDATE facts SET access_count = access_count + 1, mentioned_at = ? WHERE key = ?",
                (now_iso, key),
            )
            self.conn.commit()
            cur = self.conn.execute("SELECT * FROM facts WHERE key = ?", (key,))
            row = cur.fetchone()
            return self._row_to_dict("facts", row)

    def get_fact_meta(self, key: str) -> dict | None:
        """读取 fact 元数据（不增加 access_count，用于索引构建）"""
        cur = self.conn.execute("SELECT * FROM facts WHERE key = ?", (key,))
        row = cur.fetchone()
        return self._row_to_dict("facts", row) if row else None

    def list_facts(self, source: str | None = None, limit: int = 100) -> list[dict]:
        """
        从数据库中获取事实（facts）记录列表。

        此方法根据给定的可选过滤条件（来源）和数量限制，查询并返回存储的事实记录。
        如果指定了来源，则仅返回该来源的记录；否则返回所有来源的记录。结果按更新时间降序排列。

        参数:
            source (Optional[str]): 可选参数，用于筛选特定来源的事实记录。默认为 None（不限制来源）。
            limit (int): 返回记录的最大数量，默认为 100。

        返回:
            list[dict]: 一个字典列表，每个字典代表一条事实记录，键为数据库表的列名。
        """
        if source: # 如果提供了来源参数，则执行带来源筛选的查询
            cur = self.conn.execute(
                "SELECT * FROM facts WHERE source = ? ORDER BY updated_at DESC LIMIT ?",
                (source, limit),
            )
        else: # 未提供来源参数，查询所有记录
            cur = self.conn.execute(
                "SELECT * FROM facts ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        # 从游标（cursor）的描述信息中提取列名，作为后续字典的键
        columns = [desc[0] for desc in cur.description]
        # 使用列表推导式，将每一行查询结果与列名配对，转换成字典列表
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def delete_fact(self, key: str) -> bool:
            """删除指定键对应的事实数据。

            从数据库的facts表中删除匹配给定键的记录。

            参数:
                key (str): 要删除的记录的键。

            返回:
                bool: 如果成功删除至少一条记录，返回True；否则返回False。
            """
            # T06：execute + commit 包入 _write_lock
            with self._write_lock:
                cur = self.conn.execute("DELETE FROM facts WHERE key = ?", (key,))
                self.conn.commit()
                return cur.rowcount > 0

        # ─── 向量索引 ───────────────────────────────────────────

    def insert_vector(self, message_id: int, text_hash: str, vector_blob: bytes,
                      model_name: str, dim: int) -> int:
        """
        向数据库插入一条向量索引记录。

        参数：
        - message_id: 消息的唯一标识符。
        - text_hash: 文本内容的哈希值。
        - vector_blob: 向量数据的二进制表示。
        - model_name: 生成向量的模型名称。
        - dim: 向量的维度。

        返回值：
        - 插入记录的唯一ID。
        """
        # T06：INSERT + commit 包入 _write_lock，用 cur.lastrowid 替代 SELECT last_insert_rowid()
        with self._write_lock:
            cur = self.conn.execute(
                """INSERT INTO vector_index (message_id, text_hash, vector, model_name, dim)
                   VALUES (?, ?, ?, ?, ?)""",
                (message_id, text_hash, vector_blob, model_name, dim),
            )
            self.conn.commit()
            return cur.lastrowid or 0  # T06：cursor.lastrowid 而非 SELECT last_insert_rowid()

    def get_vectors_for_search(self, model_name: str | None = None) -> list[dict]:
        """获取所有向量用于搜索"""
        if model_name:
            cur = self.conn.execute(
                """SELECT v.id, v.message_id, v.vector, v.model_name, v.dim, m.content, m.timestamp
                   FROM vector_index v JOIN messages m ON v.message_id = m.id
                   WHERE v.model_name = ?""",
                (model_name,),
            )
        else:
            cur = self.conn.execute(
                """SELECT v.id, v.message_id, v.vector, v.model_name, v.dim, m.content, m.timestamp
                   FROM vector_index v JOIN messages m ON v.message_id = m.id""",
            )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

    def delete_vectors_by_message(self, message_id: int) -> None:
        """根据消息ID删除对应的向量索引记录。

        Args:
            message_id (int): 需要删除关联向量的消息ID。

        Returns:
            None: 此函数无返回值。
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute("DELETE FROM vector_index WHERE message_id = ?", (message_id,))
            self.conn.commit()

        # ─── BM25 索引 ─────────────────────────────────────────

    def upsert_bm25_term(self, term: str, message_id: int, tf: int = 1) -> None:
        """向BM25倒排索引表中插入或更新一条记录。

        如果指定的 (term, message_id) 组合已存在，则将其词频(tf)增加传入的值；
        如果不存在，则插入一条新记录。

        Args:
            term (str): 需要索引的词汇/词项。
            message_id (int): 该词汇所属消息的唯一标识符。
            tf (int, optional): 该词项在本次操作中的词频（出现次数）。默认为1。

        Returns:
            None
        """
        # T06：execute + commit 包入 _write_lock
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO bm25_inverted (term, message_id, tf)
                   VALUES (?, ?, ?)
                   ON CONFLICT(term, message_id) DO UPDATE SET tf = tf + ?""",
                (term, message_id, tf, tf),
            )
            self.conn.commit()

    def get_bm25_posting(self, term: str) -> list[dict]:
            """获取指定术语的BM25倒排索引列表。

            该函数通过SQLite数据库查询给定术语的倒排索引信息，
            包括出现该术语的消息ID和词频。

            Args:
                term (str): 需要查询的目标术语。

            Returns:
                list[dict]: 包含字典的列表，每个字典包含两个字段：
                    - "message_id" (int): 消息ID。
                    - "tf" (int): 该术语在该消息中的词频。
            """
            # 使用参数化查询在bm25_inverted表中查找匹配的术语记录
            cur = self.conn.execute(
                "SELECT message_id, tf FROM bm25_inverted WHERE term = ?",
                (term,),
            )
            # 将查询结果转换为包含message_id和tf字段的字典列表
            return [{"message_id": r[0], "tf": r[1]} for r in cur.fetchall()]

    def get_bm25_stats(self) -> dict:
        """查询并返回 BM25 统计数据。

        通过数据库连接执行 SQL，从 bm25_stats 表中获取索引的统计数据。
        如果表中存在记录（id=1），则返回查询到的数据；否则返回默认值。

        参数:
            self: 类实例，包含数据库连接 self.conn。

        返回:
            dict: 包含统计数据的字典，键包括 'total_docs' 和 'avg_dl'。
                  如果查询无结果，返回 {"total_docs": 0, "avg_dl": 0.0}。
        """
        # 从 bm25_stats 表中查询总文档数和平均文档长度
        cur = self.conn.execute("SELECT total_docs, avg_dl FROM bm25_stats WHERE id = 1")
        row = cur.fetchone()
        if row:
            # 查询到记录，返回包含统计数据的字典
            return {"total_docs": row[0], "avg_dl": row[1]}
        # 未查询到记录，返回默认值字典
        return {"total_docs": 0, "avg_dl": 0.0}

    def update_bm25_stats(self, total_docs: int, avg_dl: float) -> None:
            """
            更新BM25统计信息。

            功能：更新数据库中的BM25统计信息，设置文档总数、平均文档长度，并自动更新修改时间为本地当前时间。

            参数：
                total_docs (int): 文档总数。
                avg_dl (float): 平均文档长度。

            返回值：
                无。
            """
            # T06：execute + commit 包入 _write_lock
            with self._write_lock:
                self.conn.execute(
                    "UPDATE bm25_stats SET total_docs = ?, avg_dl = ?, updated_at = datetime('now', 'localtime') WHERE id = 1",
                    (total_docs, avg_dl),
                )
                self.conn.commit()

    def get_message_length(self, message_id: int) -> int:
        """获取消息的词数（文档长度）"""
        cur = self.conn.execute("SELECT content FROM messages WHERE id = ?", (message_id,))
        row = cur.fetchone()
        return len(row[0]) if row else 0

    # ─── 统计 ───────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        获取数据库的统计信息。

        参数：
            无（除了self，是类实例引用）

        返回：
            dict: 包含以下键的字典：
                - "messages": 消息总数
                - "earliest": 最早消息的时间戳
                - "latest": 最新消息的时间戳
                - "summaries": 摘要总数
                - "facts": 事实总数
                - "vectors": 向量索引总数
                - "bm25_terms": BM25倒排索引的术语总数
                - "db_size_bytes": 数据库文件大小（字节）
                - "db_size_mb": 数据库文件大小（MB）
        """
        # 从messages表查询消息总数、最早和最新时间戳
        cur1 = self.conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages")
        row1 = cur1.fetchone()  # 获取查询结果

        # 从summaries表查询摘要总数
        cur2 = self.conn.execute("SELECT COUNT(*) FROM summaries")
        row2 = cur2.fetchone()

        # 从facts表查询事实总数
        cur3 = self.conn.execute("SELECT COUNT(*) FROM facts")
        row3 = cur3.fetchone()

        # 从vector_index表查询向量索引总数
        cur4 = self.conn.execute("SELECT COUNT(*) FROM vector_index")
        row4 = cur4.fetchone()

        # 从bm25_inverted表查询BM25倒排索引的术语总数
        cur5 = self.conn.execute("SELECT COUNT(*) FROM bm25_inverted")
        row5 = cur5.fetchone()

        # 检查数据库文件是否存在，若存在则获取大小，否则设为0
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        # 返回统计信息字典
        return {
            "messages": row1[0],      # 消息总数
            "earliest": row1[1],      # 最早时间戳
            "latest": row1[2],        # 最新时间戳
            "summaries": row2[0],     # 摘要总数
            "facts": row3[0],         # 事实总数
            "vectors": row4[0],       # 向量索引总数
            "bm25_terms": row5[0],    # BM25术语总数
            "db_size_bytes": db_size, # 数据库大小（字节）
            "db_size_mb": round(db_size / 1024 / 1024, 2),  # 转换为MB并保留两位小数
        }

    def query_db_size_history(self) -> list[dict]:
        """返回最近 7 天 DB 大小采样数组

        Ticket 01：最简实现，in-memory cache + 当天采样。
        不新增 sqlite 表（如需持久化采样走后续 ticket）。

        每次 /memory/status 被调用时更新当天采样值，反映最新 DB 大小。
        历史采样在内存中滚动保留 7 天（按日期字符串升序，超过 7 条删最旧的）。

        Returns:
            list[dict]: 每条 {date: "YYYY-MM-DD", size_mb: float}，按 date 升序，
                       最少返回当天 1 条采样。
        """
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 当天采样（每次调用都更新当天值，反映最新 DB 大小）
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        size_mb = round(db_size / 1024 / 1024, 2)
        self._db_size_history[today_str] = size_mb

        # 滚动保留最近 7 天，删过期采样
        sorted_dates = sorted(self._db_size_history.keys())
        if len(sorted_dates) > 7:
            for old_date in sorted_dates[:-7]:
                del self._db_size_history[old_date]

        # 返回 list[dict]，按 date 升序
        return [
            {"date": d, "size_mb": self._db_size_history[d]}
            for d in sorted(self._db_size_history.keys())
        ]

        # ─── 旧 KV 迁移 ────────────────────────────────────────

    def migrate_kv_memory(self, kv_dir: str) -> int:
        """从旧 KV JSON 文件迁移到 SQLite"""
        import glob
        count = 0
        for fpath in glob.glob(os.path.join(kv_dir, "*.json")):
            key = os.path.splitext(os.path.basename(fpath))[0]
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                # 检查是否已迁移
                existing = self.conn.execute(
                    "SELECT id FROM messages WHERE key = ? AND source = 'manual' LIMIT 1",
                    (key,),
                ).fetchone()
                if existing:
                    continue
                content = json.dumps(data, ensure_ascii=False)
                self.insert_message(
                    content=content,
                    source="manual",
                    role="system",
                    key=key,
                )
                # 同时写入 facts 表
                self.extract_facts_from_kv(key, data)
                count += 1
            except Exception as e:
                logger.warning(f"迁移 {fpath} 失败: {e}")
                continue
        return count

    def extract_facts_from_kv(self, key: str, data: dict) -> None:
        """从 KV 数据中提取事实（v3 修复版）

        v2 bug：原实现只把 dict 的标量子字段拆成独立 fact，导致：
        - consumption_contexts/trigger_keywords 这种 list 字段被丢弃
        - 整个 dict 没有作为单条 fact 存入
        - find_consumable_memories 无法匹配通过 memory_set 写入的消费场景

        v3 修复：采用"整体 + 拆分"双写策略：
        1. 整体写入一条 fact（key 不变，value 是整个 dict 的 JSON）
        2. 提取结构化字段（fact_type/occurred_at/consumption_contexts/trigger_keywords）
           到 facts 表的独立列，供 find_consumable_memories 用 SQL 索引过滤

        保留旧的标量子字段拆分行为（向后兼容），但拆分时跳过 v3 结构化字段，
        避免重复写入。
        """
        # v3 结构化字段名集合（这些字段不参与旧的标量拆分）
        V3_STRUCTURED_FIELDS = {
            "fact_type", "occurred_at", "consumption_contexts",
            "trigger_keywords", "mentioned_at",
        }

        # 1. 整体写入一条 fact（key 不变，value 是整个 dict 的 JSON）
        # 这是 v3 修复的核心：把整个 dict 作为一个 fact 存入
        self.upsert_fact(
            key=key,
            value=json.dumps(data, ensure_ascii=False),
            source="manual",
        )

        # 2. 提取 v3 结构化字段到独立列
        # 注意：当 memory_set 端点做深度合并时，get() 返回的 consumption_contexts
        # 已经是 JSON 字符串（从 facts 独立列读取），merged data 中也是 JSON 字符串。
        # 此时直接使用，避免再次 json.dumps 导致双重编码。
        cc = data.get("consumption_contexts")
        if cc is None:
            cc_json = None
        elif isinstance(cc, str):
            # 已是 JSON 字符串（来自 get() 返回值的合并），直接使用
            cc_json = cc
        else:
            cc_json = json.dumps(cc, ensure_ascii=False)

        tk = data.get("trigger_keywords")
        if tk is None:
            tk_json = None
        elif isinstance(tk, str):
            tk_json = tk
        else:
            tk_json = json.dumps(tk, ensure_ascii=False)

        structured = {
            "fact_type": data.get("fact_type"),
            "occurred_at": data.get("occurred_at"),
            "consumption_contexts": cc_json,
            "trigger_keywords": tk_json,
        }
        # 仅更新非 None 字段
        non_none = {k: v for k, v in structured.items() if v is not None}
        if non_none:
            self._update_fact_structured_fields(key, non_none)

        # v4: 移除旧的标量子字段拆分逻辑（原第 3 步）。
        # 原实现把 dict 的每个标量字段拆成 key.field 的独立 fact，
        # 导致每条记忆膨胀成 N+1 条 fact（1 整体 + N 子字段），
        # 维护器 LLM 逐条验证时无法识别冗余，且 find_consumable_memories
        # 已改用 facts 表独立列（consumption_contexts/trigger_keywords）查询，
        # 不依赖子字段拆分。整体写入 + 结构化字段列已覆盖所有读取路径。

    # ─── 内部工具 ───────────────────────────────────────────

    _ALLOWED_TABLES = frozenset({
        "messages", "facts", "summaries", "vector_index", "bm25_inverted",
        "bm25_stats", "schema_info", "memory_meta",
        "evidence_ledger", "search_traces",  # v3 新增
    })

    def _row_to_dict(self, table: str, row: tuple) -> dict:
        """
        将数据库查询返回的一行数据转换为字典。
        该函数通过查询表结构获取列名，并将列名与行数据一一对应，
        返回以列名为键、行数据为值的字典。

        参数:
            table (str): 数据库表名（必须在 _ALLOWED_TABLES 白名单内）。
            row (tuple): 一行数据，作为元组。

        返回:
            dict: 以列名为键、对应数据为值的字典。
        """
        if table not in self._ALLOWED_TABLES:
            raise ValueError(f"非法表名: {table}（不在白名单 {self._ALLOWED_TABLES}）")
        columns = self._table_columns_cache.get(table)
        if columns is None:
            cur = self.conn.execute(f"SELECT * FROM {table} LIMIT 0")
            columns = [desc[0] for desc in cur.description]
            self._table_columns_cache[table] = columns
        return dict(zip(columns, row, strict=False))

    def close(self) -> None:
        """关闭数据库连接，并释放连接资源。

        该方法用于安全地关闭当前对象持有的数据库连接。
        如果连接存在，则执行关闭操作并将连接属性重置为None，避免重复关闭。

        Args:
            无（除了self）。

        Returns:
            None
        """
        if self._conn:  # 检查连接是否存在，避免对None进行操作
            self._conn.close()  # 执行实际的连接关闭操作
            self._conn = None  # 将连接引用重置为None，防止重复关闭并帮助垃圾回收
