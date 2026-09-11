"""记忆系统管理器 — 统一入口

初始化并协调三层记忆系统，提供全局单例。
"""

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from server.memory.compress import CompressionPipeline
from server.memory.config import MemoryConfig, get_memory_config
from server.memory.embeddings import EmbeddingEngine
from server.memory.evidence import EvidenceLedger
from server.memory.maintainer import MemoryMaintainer
from server.memory.recent import RecentMemory
from server.memory.recorder import InteractionRecorder
from server.memory.search_tracer import SearchTracer
from server.memory.semantic import SemanticSearch
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# 全局单例
_instance: Optional["MemoryManager"] = None
_instance_lock = threading.Lock()


class MemoryManager:
    """记忆系统管理器"""

    def __init__(self, config: MemoryConfig | None = None):
        """初始化内存管理器的实例。

        参数:
            config (Optional[MemoryConfig], optional): 配置对象，用于设置内存管理器的各项参数。默认为None，将使用默认配置。

        返回:
            None
        """
        self.config = config or get_memory_config()  # 如果config为None，则获取默认配置
        # 相对路径相对于项目根目录解析（不依赖 cwd；manager.py 在 server/memory/，3 级 parent 到项目根）
        db_path = Path(self.config.db_path)
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent.parent.parent / db_path
        self.store = MemoryStore(str(db_path))  # 初始化内存存储对象，使用配置中的数据库路径
        self.recent = RecentMemory(  # 初始化最近记忆模块
            window_size=self.config.recent_window_size,  # 设置最近记忆的窗口大小
            save_dir=self.config.recent_dir,  # 设置保存目录
            save_interval=self.config.recent_save_interval,  # 设置保存间隔
        )
        self.embedding = EmbeddingEngine(  # 初始化嵌入引擎
            model_name=self.config.embedding_model,  # 设置嵌入模型名称
            cache_dir=self.config.embedding_cache_dir,  # 设置缓存目录
            hf_mirror=self.config.hf_mirror,  # 设置Hugging Face镜像地址
        )
        # v3: SearchTracer — 检索可观测性
        self.search_tracer = SearchTracer(
            store=self.store,
            retention_days=self.config.search_tracer_retention_days,
            sample_rate=self.config.search_tracer_sample_rate,
            candidates_to_store=self.config.search_tracer_candidates_to_store,
            enabled=self.config.enable_search_tracer,
        )
        self.semantic = SemanticSearch(  # 初始化语义搜索模块
            store=self.store,  # 传入存储对象
            embedding_engine=self.embedding,  # 传入嵌入引擎
            alpha=self.config.hybrid_alpha,  # 设置混合搜索的alpha参数
            vector_top_k=self.config.vector_top_k,  # 设置向量搜索的top_k值
            bm25_top_k=self.config.bm25_top_k,  # 设置BM25搜索的top_k值
            tracer=self.search_tracer,  # v3: 注入 SearchTracer
        )
        # v3: EvidenceLedger — 证据组织层
        self.evidence_ledger = EvidenceLedger(store=self.store, config=self.config)
        self.compress = CompressionPipeline(  # 初始化压缩管道
            store=self.store,  # 传入存储对象
            threshold=self.config.compress_threshold,  # 设置压缩阈值
            batch_size=self.config.compress_batch_size,  # 设置批处理大小
        )
        self.maintainer = MemoryMaintainer(  # 初始化记忆维护器
            store=self.store,  # 传入存储对象
            config=self.config,  # 传入配置对象
        )
        # v3: 注入 SearchTracer 和 EvidenceLedger 到 maintainer，用于定期清理过期数据
        # maintainer 未声明这两个属性，走 Any 承载避免 pyright 校验
        maintainer_dynamic: Any = self.maintainer
        maintainer_dynamic._search_tracer = self.search_tracer
        maintainer_dynamic._evidence_ledger = self.evidence_ledger
        self.recorder = InteractionRecorder(  # 初始化交互记录器
            store=self.store,  # 传入存储对象
            recent=self.recent,  # 传入最近记忆模块
            semantic=self.semantic,  # 传入语义搜索模块
            compress=self.compress,  # 传入压缩管道
            config=self.config,  # 传入配置对象
            maintainer=self.maintainer,  # 传入维护器
        )
        self._initialized = False  # 初始化完成标志，设置为False表示未初始化

    def initialize(self) -> None:
        """初始化所有组件"""
        if self._initialized:
            return

        logger.info("正在初始化三层记忆系统...")

        # 1. 初始化 SQLite 存储
        self.store.initialize()
        logger.info("SQLite 存储层就绪")

        # 2. 初始化近期记忆
        self.recent.initialize()
        logger.info(f"近期记忆层就绪 (窗口={self.config.recent_window_size})")

        # 3. 初始化嵌入引擎（可能失败，降级为 BM25-only）
        if self.config.enable_semantic:
            try:
                success = self.embedding.initialize()
                if success:
                    logger.info(f"嵌入引擎就绪: {self.config.embedding_model}")
                else:
                    logger.info("嵌入引擎不可用，使用 BM25-only 模式")
            except Exception as e:
                logger.warning(f"嵌入引擎初始化失败: {e}，使用 BM25-only 模式")
        else:
            logger.info("语义检索已禁用")

        self._initialized = True
        logger.info("三层记忆系统初始化完成")

    # ─── 向后兼容 KV 接口 ──────────────────────────────────

    def get(self, key: str) -> dict | None:
        """读取记忆（兼容旧 KV 接口）

        查找顺序：facts 表 → messages 表（key 匹配）

        v3 行为：
        - facts 表的 value 是 JSON 字符串，解析后合并到返回 dict
          （客户端可直接访问 data["name"]，同时附带 source/updated_at 元数据）
        - 同时返回 v3 结构化字段（fact_type/occurred_at/mentioned_at/
          consumption_contexts/trigger_keywords），如果有值
        """
        import json

        # 先查 facts 表
        fact = self.store.get_fact(key)
        if fact:
            result: dict = {
                "source": fact["source"],
                "updated_at": fact["updated_at"],
            }
            # v3: 解析 value JSON，如果是 dict 则合并到 result（保持向后兼容）
            # 这样客户端可以直接访问 data["name"]，同时也能访问元数据 data["source"]
            value_str = fact.get("value", "")
            try:
                value_parsed = json.loads(value_str)
                if isinstance(value_parsed, dict):
                    result.update(value_parsed)
                else:
                    result["value"] = value_str
            except (json.JSONDecodeError, TypeError):
                result["value"] = value_str
            # v3: 附带结构化字段（如果有值）
            for field in (
                "fact_type", "occurred_at", "mentioned_at",
                "consumption_contexts", "trigger_keywords",
                "access_count", "confidence", "created_at",
            ):
                val = fact.get(field)
                if val is not None:
                    result[field] = val
            return result

        # 再查 messages 表
        messages = self.store.query_messages(key=key, limit=1)
        if messages:
            msg = messages[0]
            try:
                import json
                data = json.loads(msg["content"])
                if isinstance(data, dict):
                    data["_id"] = msg["id"]
                    # source 统一字段名（原 _source 与 facts 分支 source 不一致导致客户端详情页该行缺失）
                    data["source"] = msg["source"]
                    return data
            except (json.JSONDecodeError, TypeError):
                return {"value": msg["content"], "_id": msg["id"],
                        "source": msg["source"]}

        return None

    def set(self, key: str, data: dict, structured: dict | None = None) -> dict:
        """写入记忆（兼容旧 KV 接口）

        同时写入 messages 表和 facts 表。

        Args:
            key: 记忆 key
            data: 记忆数据（dict）
            structured: v3 结构化字段，可选。
                支持的键：fact_type / occurred_at / consumption_contexts / trigger_keywords
                如果提供，会覆盖 data 中相同字段提取的值。
                mentioned_at 不在此处设置（由 get_fact 自动维护）。
        """
        import json

        # 写入 messages 表
        content = json.dumps(data, ensure_ascii=False)
        msg_id = self.store.insert_message(
            content=content,
            source="manual",
            role="system",
            key=key,
        )

        # 提取事实（v3 双写策略：整体 + 结构化字段 + 旧标量拆分）
        # 如果 data 内部含 fact_type/occurred_at/consumption_contexts/trigger_keywords，
        # extract_facts_from_kv 会自动提取到独立列
        self.store.extract_facts_from_kv(key, data)

        # v3: 显式传入的 structured 字段优先（覆盖 data 内部提取的值）
        if structured:
            prepared: dict = {}
            for k, v in structured.items():
                if v is None:
                    continue
                # list 字段需要序列化为 JSON 字符串（与 facts 表存储格式一致）
                if k in ("consumption_contexts", "trigger_keywords") and isinstance(v, list):
                    prepared[k] = json.dumps(v, ensure_ascii=False)
                else:
                    prepared[k] = v
            if prepared:
                self.store._update_fact_structured_fields(key, prepared)

        # 建立语义索引
        self.semantic.index_message(msg_id, content)

        # 写入 Recent 层
        self.recent.add(content, source="manual", role="system", key=key)

        return {"key": key, "id": msg_id, "status": "ok"}

    def delete(self, key: str) -> bool:
        """删除记忆"""
        # 删除 facts
        fact_deleted = self.store.delete_fact(key)

        # 删除 messages（通过 key 匹配）；vector_index/bm25_inverted 由 ON DELETE CASCADE 自动清理
        messages = self.store.query_messages(key=key)
        for msg in messages:
            self.store.delete_message(msg["id"])

        return fact_deleted or len(messages) > 0

    def list_keys(self) -> list[dict]:
        """列出所有记忆 key

        v3 行为：facts 表是主源（v3 extract_facts_from_kv 写入整个 dict 到 facts），
        messages 表只列出 facts 表中不存在的 key（避免重复）。
        """
        results = []
        seen_keys: set[str] = set()

        # 从 facts 表（主源）
        facts = self.store.list_facts(limit=200)
        for f in facts:
            # v3: 解析 value 取 summary（name/description/title 优先，content 兜底）
            summary = ""
            try:
                import json as _json
                _parsed = _json.loads(f.get("value", ""))
                if isinstance(_parsed, dict):
                    summary = (
                        _parsed.get("name")
                        or _parsed.get("title")
                        or _parsed.get("description")
                        or _parsed.get("summary")
                        or ""
                    )
                    # reference 类 fact 只有 content（markdown 正文），取前 80 字作摘要
                    if not summary and _parsed.get("content"):
                        summary = str(_parsed["content"])[:80].replace("\n", " ")
            except (ValueError, TypeError):
                pass
            results.append({
                "key": f["key"],
                # source 统一为 'auto' | 'manual'（谁写入的），与 get 端点 facts 分支对齐
                # 客户端用 fact_type 字段是否存在判断是 fact 还是 message（messages 分支不返回 fact_type）
                "source": f.get("source"),
                "fact_type": f.get("fact_type"),        # preference/project/reference/...
                "summary": summary or f["value"][:60],  # 解析后的可读摘要，非裸 JSON
                "value_preview": f["value"][:100],      # 向后兼容
                "updated_at": f["updated_at"],
            })
            seen_keys.add(f["key"])

        # 从 messages 表（补充：只列 facts 中没有的 key）
        cur = self.store.conn.execute(
            """SELECT key, source, content, created_at FROM messages
               WHERE id IN (SELECT MAX(id) FROM messages WHERE key IS NOT NULL GROUP BY key)"""
        )
        for row in cur.fetchall():
            if row[0] in seen_keys:
                continue
            results.append({
                "key": row[0],
                "source": row[1],
                "value_preview": row[2][:100],
                "updated_at": row[3],
            })
            seen_keys.add(row[0])

        return results

    def get_memory_index(self, keys: list) -> list:
        """返回记忆索引信息（不含完整内容，不增加 access_count）。

        供 agent_guide 增强：返回 [{key, summary, updated_at, stale, days_since_update}, ...]
        """
        import json as _json
        from datetime import datetime as _dt

        results = []
        for key in keys:
            meta = self.store.get_fact_meta(key)
            if meta:
                updated_at = meta.get("updated_at")
                days_since = None
                stale = False
                try:
                    if isinstance(updated_at, str):  # 收窄为 str 再 strptime，规避 Optional
                        updated_dt = _dt.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                        days_since = (_dt.now() - updated_dt).days
                        stale = days_since >= self.config.maintain_stale_days
                except (ValueError, TypeError):
                    pass

                value = meta.get("value", "")
                summary = value[:80] if value else ""
                try:
                    parsed = _json.loads(value)
                    if isinstance(parsed, dict):
                        if parsed.get("name"):
                            summary = parsed["name"]
                        elif parsed.get("description"):
                            summary = parsed["description"]
                        elif parsed.get("title"):
                            summary = parsed["title"]
                except (ValueError, TypeError):
                    pass

                results.append({
                    "key": key,
                    "summary": summary,
                    "updated_at": updated_at,
                    "stale": stale,
                    "days_since_update": days_since,
                })
            else:
                results.append({
                    "key": key,
                    "summary": None,
                    "updated_at": None,
                    "stale": False,
                    "days_since_update": None,
                })
        return results

    def find_consumable_memories(self, task_type: str, task_query: str | None = None) -> list[dict]:
        """反向查询：找出所有声明了 consumption_contexts 匹配 task_type 的记忆。

        v3 优化：优先用 SQL 索引过滤 consumption_contexts 列（LIKE 模式匹配），
        对于旧数据（consumption_contexts 列为 NULL）回退到全表扫描 + JSON 解析。

        遍历候选 facts，解析 value JSON，检查：
        1. consumption_contexts 是否含 task_type（支持 "*" 通配和 "scope.*" 通配）
        2. trigger_keywords 是否出现在 task_query 中

        返回 [{key, summary, updated_at, stale, days_since_update, matched_by}, ...]
        matched_by: "context" | "keyword" | "context+keyword"
        不增加 access_count（用 get_fact_meta 而非 get_fact）。
        """
        import json as _json
        from datetime import datetime as _dt

        results: list[dict] = []
        task_query_lower = task_query.lower() if task_query else ""
        task_scope = task_type.split(".", 1)[0] if "." in task_type else task_type

        # v3 优化路径：用 SQL LIKE 过滤 consumption_contexts 列
        # 匹配三种模式：
        #   1. '"*"' — 全场景通配
        #   2. '"task_type"' — 精确匹配（如 "recurring.accounting"）
        #   3. '"scope.*"' — scope 通配（如 "recurring.*"）
        # 注意：LIKE 模式中的 % 匹配任意字符，所以查询字符串中的特殊字符需转义
        # 但 task_type 通常只含字母、数字、点、下划线、连字符，无需转义
        sql_patterns = [
            '%"*"%',                       # 全场景通配
            f'%"{task_type}"%',            # 精确匹配
            f'%"{task_scope}.*"%',         # scope 通配
        ]
        where_clause = " OR ".join(["consumption_contexts LIKE ?"] * len(sql_patterns))

        try:
            cur = self.store.conn.execute(
                f"""SELECT key, value, updated_at, fact_type, occurred_at, mentioned_at,
                          consumption_contexts, trigger_keywords
                   FROM facts
                   WHERE {where_clause}""",
                sql_patterns,
            )
            rows = cur.fetchall()
        except Exception as e:
            logger.debug(f"find_consumable_memories: SQL 索引查询失败，回退全表扫描: {e}")
            rows = None

        # v3 优化路径未命中或旧数据：回退到全表扫描 + JSON 解析
        if not rows:
            try:
                all_facts = self.store.list_facts(limit=500)
            except Exception as e:
                logger.debug(f"find_consumable_memories: list_facts 失败: {e}")
                return results
            rows = [
                {
                    "key": f.get("key"),
                    "value": f.get("value", ""),
                    "updated_at": f.get("updated_at"),
                    "fact_type": f.get("fact_type"),
                    "occurred_at": f.get("occurred_at"),
                    "mentioned_at": f.get("mentioned_at"),
                    "consumption_contexts": f.get("consumption_contexts"),
                    "trigger_keywords": f.get("trigger_keywords"),
                }
                for f in all_facts
            ]
        else:
            # 把 SQL 结果转成 dict 列表
            new_rows = []
            for row in rows:
                # row 是 tuple，按 SELECT 顺序对应
                new_rows.append({
                    "key": row[0],
                    "value": row[1] or "",
                    "updated_at": row[2],
                    "fact_type": row[3],
                    "occurred_at": row[4],
                    "mentioned_at": row[5],
                    "consumption_contexts": row[6],  # JSON 字符串
                    "trigger_keywords": row[7],  # JSON 字符串
                })
            rows = new_rows

        for fact in rows:
            key = fact.get("key")
            value_str = fact.get("value", "")
            if not value_str:
                continue

            # 优先用独立列的 consumption_contexts/trigger_keywords，回退到 value JSON 解析
            contexts_raw = fact.get("consumption_contexts")
            keywords_raw = fact.get("trigger_keywords")

            contexts: list = []
            keywords: list = []

            if contexts_raw:
                try:
                    parsed = _json.loads(contexts_raw)
                    if isinstance(parsed, list):
                        contexts = parsed
                except (ValueError, TypeError):
                    pass

            if keywords_raw:
                try:
                    parsed = _json.loads(keywords_raw)
                    if isinstance(parsed, list):
                        keywords = parsed
                except (ValueError, TypeError):
                    pass

            # 旧数据无独立列，回退到 value JSON 解析
            if not contexts and not keywords:
                try:
                    value = _json.loads(value_str)
                    if isinstance(value, dict):
                        contexts = value.get("consumption_contexts") or []
                        keywords = value.get("trigger_keywords") or []
                except (ValueError, TypeError):
                    continue
            else:
                # 有独立列数据，但可能还需要从 value 解析 summary
                try:
                    value = _json.loads(value_str)
                    if not isinstance(value, dict):
                        value = {}
                except (ValueError, TypeError):
                    value = {}

            if not contexts and not keywords:
                continue

            # 检查 consumption_contexts 匹配
            matched_context = False
            for ctx in contexts:
                if ctx == "*":
                    matched_context = True
                    break
                if ctx == task_type:
                    matched_context = True
                    break
                if ctx.endswith(".*"):
                    ctx_scope = ctx[:-2]
                    if ctx_scope == task_scope:
                        matched_context = True
                        break

            # 检查 trigger_keywords 匹配
            matched_keyword = False
            if task_query_lower and keywords:
                for kw in keywords:
                    if kw.lower() in task_query_lower:
                        matched_keyword = True
                        break

            if not matched_context and not matched_keyword:
                continue

            # 构建索引项（复用 get_memory_index 的摘要逻辑）
            updated_at = fact.get("updated_at")
            days_since = None
            stale = False
            try:
                if isinstance(updated_at, str):  # 收窄为 str 再 strptime，规避 Optional
                    updated_dt = _dt.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (_dt.now() - updated_dt).days
                    stale = days_since >= self.config.maintain_stale_days
            except (ValueError, TypeError):
                pass

            summary = value.get("name") or value.get("description") or value.get("title") or value_str[:80]

            if matched_context and matched_keyword:
                matched_by = "context+keyword"
            elif matched_context:
                matched_by = "context"
            else:
                matched_by = "keyword"

            results.append({
                "key": key,
                "summary": summary,
                "updated_at": updated_at,
                "stale": stale,
                "days_since_update": days_since,
                "matched_by": matched_by,
                "fact_type": fact.get("fact_type"),
                "occurred_at": fact.get("occurred_at"),
            })

        return results

    # ─── 新接口 ─────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10,
               since: float | None = None, until: float | None = None,
               source: str | None = None) -> list[dict]:
        """语义搜索

        v3 改进：启用 EvidenceLedger 时，组织三源召回（messages + facts + summaries）
        为结构化证据集返回；未启用时回退到原 messages-only 行为。
        """
        # 1. 原 messages 搜索（SemanticSearch 内部已记录 trace）
        msg_results = self.semantic.search(query, top_k=top_k * 2, since=since, until=until, source=source)

        # 2. 启用 EvidenceLedger 时，从 facts 和 summaries 也搜一遍，然后组织证据
        if self.config.enable_evidence_ledger:
            fact_results = self._search_facts(query, top_k=top_k)
            summary_results = self._search_summaries(query, top_k=top_k)
            # 拿到 trace_id 用于审计关联（单线程安全）
            trace_id = getattr(self.semantic, "_last_trace_id", None)
            evidence = self.evidence_ledger.organize_evidence(
                query=query,
                message_results=msg_results,
                fact_results=fact_results,
                summary_results=summary_results,
                trace_id=trace_id,
            )
            return evidence[:top_k]

        # 未启用 EvidenceLedger，回退原行为
        return msg_results[:top_k]

    def _search_facts(self, query: str, top_k: int = 10) -> list[dict]:
        """从 facts 表搜索匹配 query 的事实

        简单实现：用 SQL LIKE 匹配 key 或 value，按 updated_at DESC 排序。
        不调 LLM，性能可控。
        """
        try:
            # 用 %query% 模糊匹配 key 或 value
            pattern = f"%{query}%"
            cur = self.store.conn.execute(
                """SELECT id, key, value, source, confidence, fact_type, occurred_at,
                          updated_at
                   FROM facts
                   WHERE key LIKE ? OR value LIKE ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (pattern, pattern, top_k),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"_search_facts 失败: {e}")
            return []

    def _search_summaries(self, query: str, top_k: int = 10) -> list[dict]:
        """从 summaries 表搜索匹配 query 的摘要

        简单实现：用 SQL LIKE 匹配 summary 字段，按 end_time DESC 排序。
        """
        try:
            pattern = f"%{query}%"
            cur = self.store.conn.execute(
                """SELECT id, start_time, end_time, summary, message_count, model,
                          created_at
                   FROM summaries
                   WHERE summary LIKE ?
                   ORDER BY end_time DESC
                   LIMIT ?""",
                (pattern, top_k),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"_search_summaries 失败: {e}")
            return []

    def timeline(self, since: float | None = None, until: float | None = None,
                 source: str | None = None, limit: int = 50) -> list[dict]:
        """时间线查询"""
        return self.store.query_messages(
            since=since, until=until, source=source, limit=limit
        )

    def get_status(self) -> dict:
        """获取记忆系统状态"""
        stats = self.store.get_stats()
        stats["recent_count"] = self.recent.count
        stats["embedding_ready"] = self.embedding.ready
        stats["embedding_model"] = self.config.embedding_model if self.embedding.ready else "unavailable"
        stats["interaction_count"] = self.recorder.interaction_count
        stats["pending_messages"] = len(self.recorder._pending_messages)
        stats["initialized"] = self._initialized
        stats["maintainer"] = self.maintainer.get_status()
        # Ticket 01：DB 大小历史采样（7 天滚动，in-memory，向后兼容字段）
        stats["db_size_history"] = self.store.query_db_size_history()
        return stats

    def shutdown(self) -> None:
        """关闭记忆系统"""
        self.recorder.force_flush()
        self.recent.flush()
        self.store.close()
        self._initialized = False
        logger.info("记忆系统已关闭")


def get_memory_manager() -> MemoryManager:
    """获取全局记忆管理器实例"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemoryManager()
            _instance.initialize()
        return _instance


def reset_memory_manager() -> None:
    """重置全局实例（测试用）"""
    global _instance
    with _instance_lock:
        if _instance:
            _instance.shutdown()
        _instance = None
