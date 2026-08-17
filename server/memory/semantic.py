"""混合语义检索

向量嵌入 + BM25 混合召回，支持降级模式：
- 完整模式：向量 + BM25 加权融合
- 降级模式：BM25-only（嵌入模型不可用时自动降级）
"""

import logging
import time
from typing import TYPE_CHECKING

import numpy as np

from server.memory.bm25 import BM25Index
from server.memory.embeddings import EmbeddingEngine
from server.memory.store import MemoryStore

if TYPE_CHECKING:
    from server.memory.search_tracer import SearchTracer

logger = logging.getLogger(__name__)


class SemanticSearch:
    """混合语义检索引擎"""

    def __init__(self, store: MemoryStore, embedding_engine: EmbeddingEngine,
                 alpha: float = 0.5, vector_top_k: int = 10, bm25_top_k: int = 10,
                 tracer: "SearchTracer | None" = None):
        """
        Args:
            store: MemoryStore 实例
            embedding_engine: 嵌入引擎
            alpha: 向量检索权重 (0=纯BM25, 1=纯向量)
            vector_top_k: 向量检索返回数
            bm25_top_k: BM25 检索返回数
            tracer: SearchTracer 实例（可选，None 表示不追踪）
        """
        self.store = store
        self.embedding = embedding_engine
        self.bm25 = BM25Index(store)
        self.alpha = alpha
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.tracer = tracer  # 可选，None 表示不追踪
        # 最近一次 trace_id（供 EvidenceLedger 关联，单线程安全）
        self._last_trace_id: int | None = None

    def index_message(self, message_id: int, content: str) -> None:
        """为消息建立完整索引（BM25 + 向量）"""
        # BM25 索引（始终可用）
        try:
            self.bm25.index_message(message_id, content)
        except Exception as e:
            logger.warning(f"BM25 索引失败 (msg={message_id}): {e}")

        # 向量索引（可能不可用）
        if self.embedding.ready:
            try:
                self._index_vector(message_id, content)
            except Exception as e:
                logger.warning(f"向量索引失败 (msg={message_id}): {e}")

    def search(self, query: str, top_k: int = 10,
               since: float | None = None, until: float | None = None,
               source: str | None = None) -> list[dict]:
        """混合语义搜索

        Args:
            query: 搜索查询
            top_k: 返回结果数
            since: 起始时间戳
            until: 结束时间戳
            source: 消息来源过滤

        Returns:
            [{"message_id", "score", "content", "timestamp", "source"}, ...]
        """
        # SearchTracer：开始 trace（采样率决定是否记录）
        trace_id = None
        if self.tracer:
            trace_id = self.tracer.start_trace(
                query=query,
                params={
                    "top_k": top_k, "since": since, "until": until, "source": source,
                },
            )
        # 暴露给 EvidenceLedger 关联（单线程安全）
        self._last_trace_id = trace_id

        try:
            if self.embedding.ready and self.alpha > 0:
                results = self._hybrid_search(query, top_k, since, until, source, trace_id)
                mode = "hybrid"
            else:
                results = self._bm25_search(query, top_k, since, until, source, trace_id)
                mode = "bm25_only"

            if self.tracer and trace_id:
                self.tracer.finish_trace(trace_id, results, mode=mode)
            return results
        except Exception as e:
            if self.tracer and trace_id:
                self.tracer.finish_trace(trace_id, [], mode="error", error=str(e))
            raise

    def _hybrid_search(self, query: str, top_k: int,
                       since: float | None, until: float | None,
                       source: str | None,
                       trace_id: int | None = None) -> list[dict]:
        """向量 + BM25 混合搜索"""
        # BM25 检索
        t0 = time.perf_counter()
        bm25_results = self.bm25.search(query, self.bm25_top_k)
        bm25_latency = int((time.perf_counter() - t0) * 1000)
        if self.tracer and trace_id:
            self.tracer.record_subsearch(trace_id, "bm25", bm25_results, bm25_latency)
        bm25_scores = {r["message_id"]: r["score"] for r in bm25_results}

        # 向量检索
        t0 = time.perf_counter()
        vector_results = self._vector_search(query, self.vector_top_k)
        vec_latency = int((time.perf_counter() - t0) * 1000)
        if self.tracer and trace_id:
            self.tracer.record_subsearch(trace_id, "vector", vector_results, vec_latency)
        vector_scores = {r["message_id"]: r["score"] for r in vector_results}

        # 合并候选集
        all_ids = set(bm25_scores.keys()) | set(vector_scores.keys())

        # 归一化分数
        bm25_max = max(bm25_scores.values()) if bm25_scores else 1.0
        vec_max = max(vector_scores.values()) if vector_scores else 1.0

        # 加权融合
        combined = []
        for mid in all_ids:
            bm25_norm = bm25_scores.get(mid, 0.0) / max(bm25_max, 1e-9)
            vec_norm = vector_scores.get(mid, 0.0) / max(vec_max, 1e-9)
            score = self.alpha * vec_norm + (1 - self.alpha) * bm25_norm

            # 获取消息详情
            msg = self.store.get_message(mid)
            if not msg:
                continue

            # 时间过滤
            if since and msg["timestamp"] < since:
                continue
            if until and msg["timestamp"] > until:
                continue
            if source and msg["source"] != source:
                continue

            combined.append({
                "message_id": mid,
                "score": round(score, 4),
                "bm25_score": round(bm25_norm, 4),
                "vector_score": round(vec_norm, 4),
                "content": msg["content"][:500],
                "timestamp": msg["timestamp"],
                "source": msg["source"],
            })

        combined.sort(key=lambda x: x["score"], reverse=True)
        # 记录融合结果
        if self.tracer and trace_id:
            self.tracer.record_fusion(trace_id, combined)
        return combined[:top_k]

    def _vector_search(self, query: str, top_k: int) -> list[dict]:
        """向量相似度搜索"""
        if not self.embedding.ready:
            return []

        query_vec = self.embedding.embed([query])[0]

        # 加载所有向量
        rows = self.store.get_vectors_for_search(self.embedding.model_name)
        if not rows:
            return []

        # 计算余弦相似度
        scores = []
        for row in rows:
            vec = self.embedding.blob_to_vector(row["vector"])
            if len(vec) != len(query_vec):
                continue
            sim = float(np.dot(query_vec, vec))
            scores.append({
                "message_id": row["message_id"],
                "score": sim,
                "content": row["content"][:500] if row.get("content") else "",
                "timestamp": row.get("timestamp", 0),
            })

        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores[:top_k]

    def _bm25_search(self, query: str, top_k: int,
                     since: float | None, until: float | None,
                     source: str | None,
                     trace_id: int | None = None) -> list[dict]:
        """BM25-only 搜索（降级模式）"""
        t0 = time.perf_counter()
        results = self.bm25.search(query, top_k * 2)  # 多取一些再过滤
        bm25_latency = int((time.perf_counter() - t0) * 1000)
        if self.tracer and trace_id:
            self.tracer.record_subsearch(trace_id, "bm25", results, bm25_latency)

        filtered = []
        for r in results:
            if since and r.get("timestamp", 0) < since:
                continue
            if until and r.get("timestamp", 0) > until:
                continue
            if source and r.get("source") != source:
                continue
            filtered.append(r)

        # 记录融合结果（BM25-only 模式下融合 == 过滤后结果）
        if self.tracer and trace_id:
            self.tracer.record_fusion(trace_id, filtered)
        return filtered[:top_k]

    def _index_vector(self, message_id: int, content: str) -> None:
        """为消息建立向量索引"""
        text_hash = self.embedding.text_hash(content)

        # 检查是否已索引
        existing = self.store.conn.execute(
            "SELECT id FROM vector_index WHERE message_id = ? AND text_hash = ?",
            (message_id, text_hash),
        ).fetchone()
        if existing:
            return

        # 生成嵌入
        vec = self.embedding.embed([content])[0]
        blob = self.embedding.vector_to_blob(vec)

        self.store.insert_vector(message_id, text_hash, blob, self.embedding.model_name, self.embedding.dim)

    def rebuild_vector_index(self, batch_size: int = 100) -> dict:
        """重建向量索引（维护操作）"""
        if not self.embedding.ready:
            return {"error": "嵌入引擎不可用"}

        # 获取未索引的消息
        cur = self.store.conn.execute("""
            SELECT m.id, m.content FROM messages m
            WHERE m.id NOT IN (SELECT message_id FROM vector_index)
            AND m.content != ''
            ORDER BY m.timestamp DESC
        """)
        rows = cur.fetchall()

        indexed = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            texts = [r[1] for r in batch]
            vecs = self.embedding.embed(texts)

            for (msg_id, content), vec in zip(batch, vecs, strict=False):
                text_hash = self.embedding.text_hash(content)
                blob = self.embedding.vector_to_blob(vec)
                self.store.insert_vector(msg_id, text_hash, blob, self.embedding.model_name, self.embedding.dim)
                indexed += 1

        return {"indexed": indexed, "total": len(rows)}
