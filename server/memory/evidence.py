"""证据组织层 — Evidence Ledger

借鉴 HMS（Holographic Memory System）项目的"多源召回交叉核对"理念，
在 memory_search 与 agent 之间插入证据组织层。

工作流程：
1. 接收三源候选：messages (来自 BM25/向量搜索) + facts (KV 检索) + summaries (摘要检索)
2. 统一转换为 Evidence 对象
3. 冲突检测：同时间窗口（±5 min）且内容矛盾（Jaccard < 0.3）且都含 query 关键词 → conflicting_ids
4. 佐证计数：相似度 ≥ corroboration_threshold 的证据相互 corroboration_count++
5. 写入 evidence_ledger 表（保留前 N 条候选供审计）
6. 按 retrieval_score * (1 + corroboration_count * 0.1) 排序，返回前 top_k 条给 agent

设计目标：
- 让 agent 看到证据的"来源"和"可信度"，而不是黑盒 score
- 暴露冲突：相同时间窗口内矛盾的证据相互标记，agent 可自主判断
- 持久化审计：evidence_ledger 表保留候选集，便于事后诊断"为什么没召回？"
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.memory.bm25 import _tokenize
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class Evidence:
    """单条证据

    source_type: 'message' | 'fact' | 'summary'
    source_id: 对应表（messages/facts/summaries）的主键 id
    content_preview: 前 500 字符（避免存储过大）
    timestamp: 消息时间戳 / fact.updated_at epoch / summary.end_time
    retrieval_score: 检索打分 0-1（messages 用 BM25/向量分，facts 用 confidence，summaries 用固定中位分）
    source_tag: 自定义来源标签（如 'manual'、'vl_summary'、'bill_summary'）
    confidence: 来源可信度（默认 1.0，facts 用自身 confidence 字段）
    corroboration_count: 其他证据佐证此条的数量
    conflicting_ids: 与此条冲突的证据 source_id 列表
    matched_by: 'bm25' | 'vector' | 'both' | 'fact_kv' | 'summary'
    """
    source_type: str
    source_id: int
    content_preview: str
    timestamp: float | None
    retrieval_score: float
    source_tag: str | None = None
    confidence: float = 1.0
    corroboration_count: int = 0
    conflicting_ids: list[int] = field(default_factory=list)
    matched_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _jaccard_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
    """Jaccard 相似度 = |A∩B| / |A∪B|"""
    if not tokens_a or not tokens_b:
        return 0.0
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _parse_iso_to_epoch(iso_str: str | None) -> float | None:
    """ISO 时间字符串或 'YYYY-MM-DD HH:MM:SS' 转 epoch"""
    if not iso_str:
        return None
    try:
        # 兼容 'YYYY-MM-DD HH:MM:SS' 和 'YYYY-MM-DDTHH:MM:SS'
        t = iso_str.replace("T", " ")
        # 截断到秒（去掉可能的毫秒）
        if "." in t:
            t = t.split(".", 1)[0]
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


class EvidenceLedger:
    """证据组织层"""

    def __init__(self, store: MemoryStore, config: Any):
        """
        Args:
            store: MemoryStore 实例
            config: MemoryConfig 实例（读取 evidence_top_k 等配置）
        """
        self.store = store
        self.config = config

    def organize_evidence(
        self,
        query: str,
        message_results: list[dict],
        fact_results: list[dict] | None = None,
        summary_results: list[dict] | None = None,
        trace_id: int | None = None,
    ) -> list[dict]:
        """组织证据集

        流程见模块 docstring。返回前 top_k 条证据（dict 列表）。

        Args:
            query: 触发查询（用于"都含 query 关键词"检查）
            message_results: 来自 SemanticSearch 的消息搜索结果
            fact_results: 来自 _search_facts 的事实搜索结果（可选）
            summary_results: 来自 _search_summaries 的摘要搜索结果（可选）
            trace_id: 关联的 search_traces.id（可选，用于审计）
        """
        # 1. 三源候选合并为 Evidence 列表
        evidences: list[Evidence] = []
        for r in message_results:
            evidences.append(Evidence(
                source_type="message",
                source_id=int(r.get("message_id", 0)),
                content_preview=str(r.get("content", ""))[:500],
                timestamp=r.get("timestamp"),
                retrieval_score=self._clamp_score(r.get("score", 0.0)),
                source_tag=r.get("source"),
                matched_by=self._determine_matched_by(r),
            ))
        if fact_results:
            for r in fact_results:
                # fact 的 timestamp 用 updated_at 转 epoch
                ts = _parse_iso_to_epoch(r.get("updated_at"))
                confidence = float(r.get("confidence", 1.0))
                evidences.append(Evidence(
                    source_type="fact",
                    source_id=int(r.get("id", 0)),
                    content_preview=str(r.get("value", ""))[:500],
                    timestamp=ts,
                    retrieval_score=confidence,  # fact 用 confidence 作为 retrieval_score
                    source_tag=r.get("key"),
                    confidence=confidence,
                    matched_by="fact_kv",
                ))
        if summary_results:
            for r in summary_results:
                evidences.append(Evidence(
                    source_type="summary",
                    source_id=int(r.get("id", 0)),
                    content_preview=str(r.get("summary", ""))[:500],
                    timestamp=r.get("end_time"),
                    retrieval_score=0.7,  # 摘要没有打分，给固定中位分
                    source_tag=None,
                    matched_by="summary",
                ))

        # 2. 冲突检测 + 佐证计数（O(n²) 但 n ≤ 50）
        self._detect_conflicts_and_corroborations(evidences, query)

        # 3. 写入 evidence_ledger 表（前 N 条候选供审计）
        self._persist_to_db(query, evidences, trace_id)

        # 4. 按 retrieval_score * (1 + corroboration_count * 0.1) 排序
        # 佐证多的证据优先级提升
        evidences.sort(
            key=lambda e: e.retrieval_score * (1 + e.corroboration_count * 0.1),
            reverse=True,
        )

        # 5. 返回前 top_k 条
        top_k = self.config.evidence_top_k
        return [e.to_dict() for e in evidences[:top_k]]

    @staticmethod
    def _clamp_score(score: float) -> float:
        """将 score 限制在 [0, 1] 范围内"""
        try:
            s = float(score)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, s))

    @staticmethod
    def _determine_matched_by(r: dict) -> str:
        """根据消息搜索结果字段判断匹配类型"""
        bm25_score = r.get("bm25_score")
        vec_score = r.get("vector_score")
        if bm25_score is not None and vec_score is not None:
            return "both"
        elif vec_score is not None:
            return "vector"
        elif bm25_score is not None:
            return "bm25"
        return ""

    def _detect_conflicts_and_corroborations(
        self, evidences: list[Evidence], query: str
    ) -> None:
        """冲突检测 + 佐证计数

        冲突：时间窗口 ±conflict_window_minutes + Jaccard < 0.3 + 都含 query 关键词
        佐证：Jaccard >= corroboration_threshold
        """
        if len(evidences) < 2:
            return

        threshold = self.config.evidence_corroboration_threshold
        window_sec = self.config.evidence_conflict_window_minutes * 60

        # 预分词并缓存（局部缓存，避免实例状态泄漏）
        token_cache: list[list[str]] = [_tokenize(e.content_preview) for e in evidences]
        query_tokens = set(_tokenize(query)) if query else set()

        for i in range(len(evidences)):
            for j in range(i + 1, len(evidences)):
                e_i = evidences[i]
                e_j = evidences[j]
                tokens_i = token_cache[i]
                tokens_j = token_cache[j]
                sim = _jaccard_similarity(tokens_i, tokens_j)

                # 佐证计数：相似度 >= threshold
                if sim >= threshold:
                    e_i.corroboration_count += 1
                    e_j.corroboration_count += 1
                    continue  # 不会同时是冲突

                # 冲突检测：时间窗口 + Jaccard 低 + 都含 query 关键词
                if e_i.timestamp is None or e_j.timestamp is None:
                    continue
                if abs(e_i.timestamp - e_j.timestamp) > window_sec:
                    continue
                # "都含 query 关键词"：至少有一个 query token 出现在内容中
                if not query_tokens:
                    continue
                tokens_i_set = set(tokens_i)
                tokens_j_set = set(tokens_j)
                if (query_tokens & tokens_i_set) and (query_tokens & tokens_j_set):
                    e_i.conflicting_ids.append(e_j.source_id)
                    e_j.conflicting_ids.append(e_i.source_id)

    def _persist_to_db(
        self, query: str, evidences: list[Evidence], trace_id: int | None
    ) -> None:
        """将候选证据写入 evidence_ledger 表（仅审计用途）

        T06：循环 INSERT + commit 包入 _write_lock，避免并发写交叉。
        """
        # 限制存储数量，避免 audit 表过大
        to_store = evidences[:50]
        try:
            # T06：整个 INSERT 循环 + commit 包入 _write_lock
            with self.store._write_lock:
                for e in to_store:
                    self.store.conn.execute(
                        """INSERT INTO evidence_ledger
                           (query, search_trace_id, source_type, source_id,
                            content_preview, timestamp, retrieval_score,
                            source_tag, confidence, corroboration_count,
                            conflicting_ids, matched_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            query,
                            trace_id,
                            e.source_type,
                            e.source_id,
                            e.content_preview,
                            e.timestamp,
                            e.retrieval_score,
                            e.source_tag,
                            e.confidence,
                            e.corroboration_count,
                            json.dumps(e.conflicting_ids, ensure_ascii=False),
                            e.matched_by,
                        ),
                    )
                self.store.conn.commit()
        except Exception as ex:
            logger.warning(f"evidence_ledger 写入失败: {ex}")
            try:
                self.store.conn.rollback()
            except Exception:
                pass

    # ─── 审计查询接口 ────────────────────────────────────────────

    def get_recent(self, limit: int = 20) -> list[dict]:
        """最近证据集列表（按 created_at DESC）"""
        cur = self.store.conn.execute(
            """SELECT id, query, search_trace_id, source_type, source_id,
                      content_preview, timestamp, retrieval_score, source_tag,
                      confidence, corroboration_count, conflicting_ids, matched_by,
                      created_at
               FROM evidence_ledger
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        columns = [desc[0] for desc in cur.description]
        results: list[dict] = []
        for row in cur.fetchall():
            d = dict(zip(columns, row, strict=False))
            # conflicting_ids 解析为 list
            v = d.get("conflicting_ids")
            if isinstance(v, str):
                try:
                    d["conflicting_ids"] = json.loads(v)
                except json.JSONDecodeError:
                    pass
            results.append(d)
        return results

    def get_detail(self, evidence_id: int) -> dict | None:
        """单条证据详情"""
        cur = self.store.conn.execute(
            "SELECT * FROM evidence_ledger WHERE id = ?",
            (evidence_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        d = dict(zip(columns, row, strict=False))
        v = d.get("conflicting_ids")
        if isinstance(v, str):
            try:
                d["conflicting_ids"] = json.loads(v)
            except json.JSONDecodeError:
                pass
        return d

    def cleanup_old(self, days: int | None = None) -> dict:
        """清理超过 evidence_audit_keep_days 的旧证据"""
        retention = days if days is not None else self.config.evidence_audit_keep_days
        cutoff_dt = datetime.now() - timedelta(days=retention)
        # 5-12: created_at 由 datetime('now','localtime') 生成（空格分隔）；此前
        # isoformat 用 "T" 分隔，' '(0x20) < 'T'(0x54) 导致 cutoff 当天整日记录被提前删除
        cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        # T06：DELETE + commit 包入 _write_lock
        with self.store._write_lock:
            cur = self.store.conn.execute(
                "DELETE FROM evidence_ledger WHERE created_at < ?", (cutoff,)
            )
            self.store.conn.commit()
        return {
            "deleted": cur.rowcount,
            "cutoff": cutoff,
            "retention_days": retention,
        }
