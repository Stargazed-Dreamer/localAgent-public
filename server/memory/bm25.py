"""BM25 搜索引擎

使用 jieba 中文分词 + Okapi BM25 算法实现关键词检索。
无需 API key，无需模型下载，开箱即用。
"""

import logging
import math

logger = logging.getLogger(__name__)

# BM25 参数
K1 = 1.5   # 词频饱和参数
B = 0.75   # 文档长度归一化参数


def _tokenize(text: str) -> list[str]:
    """中文分词，优先使用 jieba，降级为字符级切分"""
    try:
        import jieba
        return list(jieba.cut(text))
    except ImportError:
        # 降级：简单的字符级切分
        return _char_tokenize(text)


def _char_tokenize(text: str) -> list[str]:
    """简单的字符级切分（降级方案）"""
    tokens = []
    current = ""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            # 中文字符，逐字切分
            if current:
                tokens.append(current.lower())
                current = ""
            tokens.append(ch)
        elif ch.isalnum():
            current += ch
        else:
            if current:
                tokens.append(current.lower())
                current = ""
    if current:
        tokens.append(current.lower())
    return tokens


class BM25Index:
    """BM25 倒排索引，数据存储在 SQLite 中"""

    def __init__(self, store):
        """Args:
            store: MemoryStore 实例
        """
        self.store = store

    def index_message(self, message_id: int, content: str) -> int:
        """为一条消息建立 BM25 索引，返回索引词数"""
        tokens = _tokenize(content)
        # 过滤停用词和单字符（保留中文单字）
        filtered = [t for t in tokens if len(t) > 1 or ('\u4e00' <= t <= '\u9fff')]

        # 统计词频
        tf_map: dict[str, int] = {}
        for t in filtered:
            tf_map[t] = tf_map.get(t, 0) + 1

        # 写入倒排索引
        for term, tf in tf_map.items():
            self.store.upsert_bm25_term(term, message_id, tf)

        # 更新文档统计
        self._update_stats(len(filtered))

        return len(filtered)

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """BM25 搜索

        Returns:
            [{"message_id": int, "score": float, "content": str}, ...]
        """
        tokens = _tokenize(query)
        filtered = [t for t in tokens if len(t) > 1 or ('\u4e00' <= t <= '\u9fff')]

        if not filtered:
            return []

        stats = self.store.get_bm25_stats()
        N = stats["total_docs"]
        avg_dl = stats["avg_dl"]

        if N == 0:
            return []

        # 收集候选文档和词频
        doc_scores: dict[int, float] = {}
        doc_lengths: dict[int, int] = {}

        for term in set(filtered):
            posting = self.store.get_bm25_posting(term)
            if not posting:
                continue

            # IDF 计算
            df = len(posting)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)

            for entry in posting:
                mid = entry["message_id"]
                tf = entry["tf"]

                # 获取文档长度（缓存）
                if mid not in doc_lengths:
                    doc_lengths[mid] = self.store.get_message_length(mid)
                dl = doc_lengths[mid]

                # BM25 评分
                tf_norm = (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / max(avg_dl, 1)))
                doc_scores[mid] = doc_scores.get(mid, 0.0) + idf * tf_norm

        # 排序
        ranked = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # 获取消息内容
        results = []
        for mid, score in ranked:
            msg = self.store.get_message(mid)
            if msg:
                results.append({
                    "message_id": mid,
                    "score": round(score, 4),
                    "content": msg["content"][:500],
                    "timestamp": msg["timestamp"],
                    "source": msg["source"],
                })

        return results

    def _update_stats(self, doc_length: int) -> None:
        """更新 BM25 文档统计"""
        stats = self.store.get_bm25_stats()
        N = stats["total_docs"] + 1
        old_avg = stats["avg_dl"]
        new_avg = (old_avg * (N - 1) + doc_length) / N
        self.store.update_bm25_stats(N, new_avg)

    def rebuild_stats(self) -> dict:
        """重建 BM25 统计（维护操作）"""
        conn = self.store.conn
        cur = conn.execute("SELECT COUNT(DISTINCT message_id) FROM bm25_inverted")
        total_docs = cur.fetchone()[0]

        if total_docs == 0:
            self.store.update_bm25_stats(0, 0.0)
            return {"total_docs": 0, "avg_dl": 0.0}

        # 计算平均文档长度
        cur = conn.execute("""
            SELECT AVG(dl) FROM (
                SELECT m.id, LENGTH(m.content) as dl
                FROM messages m
                WHERE m.id IN (SELECT DISTINCT message_id FROM bm25_inverted)
            )
        """)
        avg_dl = cur.fetchone()[0] or 0.0

        self.store.update_bm25_stats(total_docs, avg_dl)
        return {"total_docs": total_docs, "avg_dl": avg_dl}
