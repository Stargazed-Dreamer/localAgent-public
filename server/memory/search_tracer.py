"""检索可观测性 — SearchTracer

记录每次 search 的完整过程（query/params/子检索结果/scores/latency/error），
便于事后诊断：为什么某条记忆没被召回？为什么某条无关记忆被排在前？

采样率控制：sample_rate=1.0 全采样，0.1 仅采 10%。
retention：超过 retention_days 的旧 trace 由 maintainer 自动清理。

生命周期（线程内串行）：
    trace_id = tracer.start_trace(query, params)
    if trace_id:
        tracer.record_subsearch(trace_id, "bm25", results, latency_ms)
        tracer.record_subsearch(trace_id, "vector", results, latency_ms)
        tracer.record_fusion(trace_id, combined)
        tracer.finish_trace(trace_id, final_results, mode="hybrid")

使用 threading.local 隔离不同线程的 trace，避免并发污染。
"""

import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta

from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class SearchTracer:
    """检索过程追踪器"""

    def __init__(self, store: MemoryStore, retention_days: int = 30,
                 sample_rate: float = 1.0, candidates_to_store: int = 20,
                 enabled: bool = True):
        self.store = store
        self.retention_days = retention_days
        self.sample_rate = sample_rate
        self.candidates_to_store = candidates_to_store
        self.enabled = enabled
        # threading.local 隔离不同线程的 trace
        self._local = threading.local()

    def start_trace(self, query: str, params: dict) -> int | None:
        """开始一次 trace。

        采样率控制：random() > sample_rate 时跳过，返回 None。
        enabled=False 时也返回 None。
        出错时返回 None（不阻塞主流程）。

        T06：INSERT + lastrowid + commit 包入 MemoryStore._write_lock，避免并发 lastrowid 串扰。
        """
        if not self.enabled:
            return None
        if random.random() > self.sample_rate:
            return None
        try:
            ts = datetime.now().isoformat(timespec="seconds")
            params_json = json.dumps(params, ensure_ascii=False, default=str)
            # T06：包入 _write_lock，防 lastrowid 串扰
            with self.store._write_lock:
                cur = self.store.conn.execute(
                    """INSERT INTO search_traces
                       (ts, query, params, mode, latency_ms,
                        bm25_count, vector_count, final_count,
                        candidates, final_results, error)
                       VALUES (?, ?, ?, '', 0, 0, 0, 0, '[]', '[]', NULL)""",
                    (ts, query, params_json),
                )
                trace_id = cur.lastrowid
                self.store.conn.commit()
            self._local.current = {
                "trace_id": trace_id,
                "query": query,
                "params": params,
                "subsearches": [],
                "fusion": None,
                "start_time": time.perf_counter(),
            }
            return trace_id
        except Exception as e:
            logger.warning(f"SearchTracer.start_trace 失败: {e}")
            self._local.current = None
            return None

    def record_subsearch(self, trace_id: int | None, kind: str,
                         results: list[dict], latency_ms: int) -> None:
        """记录子检索结果（bm25 / vector）

        results 在此阶段先缓存到 threading.local，finish_trace 时统一持久化。
        这样避免每次 subsearch 都写 DB，性能更好。
        """
        if not trace_id:
            return
        try:
            current = getattr(self._local, "current", None)
            if not current or current.get("trace_id") != trace_id:
                return
            current["subsearches"].append({
                "kind": kind,
                "count": len(results),
                "latency_ms": latency_ms,
                # 预览，避免 candidates JSON 过大
                "results_preview": results[:self.candidates_to_store],
            })
        except Exception as e:
            logger.warning(f"SearchTracer.record_subsearch 失败: {e}")

    def record_fusion(self, trace_id: int | None, combined: list[dict]) -> None:
        """记录融合后候选集"""
        if not trace_id:
            return
        try:
            current = getattr(self._local, "current", None)
            if not current or current.get("trace_id") != trace_id:
                return
            current["fusion"] = combined[:self.candidates_to_store]
        except Exception as e:
            logger.warning(f"SearchTracer.record_fusion 失败: {e}")

    def finish_trace(self, trace_id: int | None, final_results: list[dict],
                     mode: str, error: str | None = None) -> None:
        """完成 trace，写入 search_traces 表

        mode: 'hybrid' | 'bm25_only' | 'vector_only' | 'error'
        """
        if not trace_id:
            return
        try:
            current = getattr(self._local, "current", None)
            if not current or current.get("trace_id") != trace_id:
                return

            latency_ms = int((time.perf_counter() - current["start_time"]) * 1000)

            # 汇总子检索计数
            bm25_count = sum(
                s["count"] for s in current["subsearches"] if s["kind"] == "bm25"
            )
            vector_count = sum(
                s["count"] for s in current["subsearches"] if s["kind"] == "vector"
            )

            # candidates: 子检索结果 + 融合结果（JSON 字符串）
            candidates_obj = {
                "subsearches": current["subsearches"],
                "fusion": current["fusion"],
            }
            candidates_json = json.dumps(candidates_obj, ensure_ascii=False, default=str)

            # final_results: 截断后存（避免单条过大）
            final_json = json.dumps(
                final_results[:self.candidates_to_store],
                ensure_ascii=False, default=str,
            )

            # T06：UPDATE + commit 包入 _write_lock
            with self.store._write_lock:
                self.store.conn.execute(
                    """UPDATE search_traces
                       SET mode = ?, latency_ms = ?, bm25_count = ?, vector_count = ?,
                           final_count = ?, candidates = ?, final_results = ?, error = ?
                       WHERE id = ?""",
                    (mode, latency_ms, bm25_count, vector_count,
                     len(final_results), candidates_json, final_json, error, trace_id),
                )
                self.store.conn.commit()
        except Exception as e:
            logger.warning(f"SearchTracer.finish_trace 失败: {e}")
        finally:
            # 清理线程本地状态
            self._local.current = None

    def cleanup_old(self) -> dict:
        """清理超过 retention_days 的旧 trace

        由 MemoryMaintainer.run() 定期调用，也可手动触发。
        """
        cutoff_dt = datetime.now() - timedelta(days=self.retention_days)
        cutoff = cutoff_dt.isoformat(timespec="seconds")
        # T06：DELETE + commit 包入 _write_lock
        with self.store._write_lock:
            cur = self.store.conn.execute(
                "DELETE FROM search_traces WHERE ts < ?", (cutoff,)
            )
            self.store.conn.commit()
        return {
            "deleted": cur.rowcount,
            "cutoff": cutoff,
            "retention_days": self.retention_days,
        }

    def get_recent(self, limit: int = 20, since_id: int | None = None) -> list[dict]:
        """最近 trace 列表（不含 candidates/final_results 详情，避免响应过大）

        Args:
            limit: 返回数量上限
            since_id: 游标分页（仅返回 id > since_id 的记录）
        """
        if since_id:
            cur = self.store.conn.execute(
                """SELECT id, ts, query, params, mode, latency_ms,
                          bm25_count, vector_count, final_count, error
                   FROM search_traces
                   WHERE id > ?
                   ORDER BY id DESC LIMIT ?""",
                (since_id, limit),
            )
        else:
            cur = self.store.conn.execute(
                """SELECT id, ts, query, params, mode, latency_ms,
                          bm25_count, vector_count, final_count, error
                   FROM search_traces
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        results: list[dict] = []
        for row in rows:
            d = dict(zip(columns, row, strict=False))
            # params 解析为 dict（便于客户端读取）
            v = d.get("params")
            if isinstance(v, str):
                try:
                    d["params"] = json.loads(v)
                except json.JSONDecodeError:
                    pass
            results.append(d)
        return results

    def get_detail(self, trace_id: int) -> dict | None:
        """单条 trace 详情（含 candidates、final_results 完整 JSON）

        返回 None 表示 trace_id 不存在。
        """
        cur = self.store.conn.execute(
            "SELECT * FROM search_traces WHERE id = ?",
            (trace_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        result = dict(zip(columns, row, strict=False))
        # 解析 JSON 字段（params / candidates / final_results）
        for field in ("params", "candidates", "final_results"):
            v = result.get(field)
            if isinstance(v, str):
                try:
                    result[field] = json.loads(v)
                except json.JSONDecodeError:
                    pass  # 保留原始字符串
        return result
