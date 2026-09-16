"""入站网关调用日志：SQLite 落库 + 后台队列（fail-open）+ 聚合查询

统计口径（spec 2026-09-01 勘误）：面板 Token 统计页的全部数字由本模块算出，
不复用 pool.project_stats / per-key 统计（pool.stream() 不记 project usage，
流式调用在池侧零统计）。p50/p95 由 Python 侧对查询结果排序取分位（SQLite 无
内置 percentile，30 天数据量在万级，可控）。

写入: 单后台线程 batch 落库；队列满/写失败一律丢弃并计数（fail-open，
绝不影响请求）。flush 周期顺带做 key_store 落盘与 30 天懒清理。
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.inbound_gateway.key_store import get_key_store

logger = logging.getLogger(__name__)


def _resolve_against_project_root(p: Path | str) -> Path:
    """相对路径基于项目根目录解析（不依赖 cwd）；绝对路径原样返回（5-7）。

    与 server/llm_pool/key_store.py 同名模式一致；本模块在 server/inbound_gateway/
    下，3 级 parent 到项目根。cwd 不在项目根时调用日志会写到错误位置。
    """
    p = Path(p)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent.parent.parent / p


DB_PATH = _resolve_against_project_root(Path("data") / "inbound_calls.db")
RETENTION_DAYS = 30          # spec 默认值（design-decisions #5）
QUEUE_MAX = 10_000           # 队列上限，满即丢（fail-open）
_BATCH_SIZE = 100            # 攒批条数
_FLUSH_INTERVAL = 2.0        # 秒；不足一批也强制落盘
_CLEANUP_INTERVAL = 3600.0   # 秒；懒清理周期

# ---- 请求/响应对明细留存（detail logging，可开关）----
# 默认值写在代码里（load_config() 只读 config.toml，config.toml 仅放与默认不同的覆盖项）。
# config.example.toml 同步提供文档化段（[inbound.detail_logging]）供他机参考。
_DETAIL_CFG_DEFAULTS: dict[str, Any] = {
    "enabled": False,                       # 默认关（正文可能含用户敏感内容）
    "max_records": 50,                      # 明细窗口：整体最多保留条数（超出丢最老让位）
    "max_total_bytes": 500 * 1024 * 1024,   # 明细窗口：整体正文最大字节数（500 MB）
    "max_single_bytes": 100 * 1024 * 1024,  # 单条正文最大字节数（超限写入前截断）
}
_OMIT_SUFFIX = "\u2026[truncated]"          # 单条被截断时尾部追加的省略提示

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_id TEXT NOT NULL,
    key_name TEXT NOT NULL,
    stream INTEGER NOT NULL,
    model_requested TEXT NOT NULL,
    model_used TEXT NOT NULL,
    upstream_key TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    ttft_ms INTEGER,
    ttft_kind TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    http_status INTEGER DEFAULT 200,
    retries INTEGER DEFAULT 0,
    client_ip TEXT DEFAULT '',
    error TEXT DEFAULT '',
    resolved_model TEXT DEFAULT '',
    substituted INTEGER DEFAULT 0,
    prompt_chars INTEGER DEFAULT 0,
    completion_chars INTEGER DEFAULT 0,
    usage_source TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_creation_tokens INTEGER,
    finish_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON inbound_calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_key_ts ON inbound_calls(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_upstream_ts ON inbound_calls(upstream_key, ts);

-- 请求/响应正文明细（旁路表，独立快照）。正文不进统计主表，不依赖主表自增 id，
-- 与主表 30 天懒清理解耦：此表只受"条数/总字节"窗口让位管理（detail logging，可开关）。
-- 自带定位快照（ts/key/model/status/error），使"某次调用的正文 + 成败"可独立回看。
-- 读取：不暴露给任何 REST/MCP 端点；需要时自写脚本直连本库 SELECT。
CREATE TABLE IF NOT EXISTS inbound_call_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,            -- 请求时刻（窗口按最老让位）
    key_id          TEXT NOT NULL,
    key_name        TEXT NOT NULL,
    stream          INTEGER NOT NULL,
    model_requested TEXT NOT NULL,
    model_used      TEXT NOT NULL,
    status          TEXT NOT NULL,            -- 该次调用最终 ok/error
    error           TEXT DEFAULT '',          -- 错误串（若有）
    request_body    TEXT NOT NULL,            -- 请求 JSON 原文（stringified，单条超限截断）
    response_body   TEXT NOT NULL DEFAULT '', -- 响应正文（非流式=content；流式=聚合正文含 thinking）
    response_type   TEXT NOT NULL DEFAULT 'non_stream',  -- 'non_stream' | 'stream'
    request_bytes   INTEGER NOT NULL,         -- request_body 截断前字节数
    response_bytes  INTEGER NOT NULL,         -- response_body 截断前字节数
    truncated       INTEGER NOT NULL DEFAULT 0,   -- 1=单条正文超限被截断
    truncated_what  TEXT NOT NULL DEFAULT '',     -- 'request'|'response'|'both'
    session_id      TEXT NOT NULL DEFAULT '',     -- opencode 会话亲和 ID（独立回看会话归类）
    usage_json      TEXT,                         -- provider 原始 usage 原样 JSON（兜底落盘：
                                                  --  归一白名单漏掉的字段可离线补统计）
    stored_at       REAL NOT NULL             -- 落库时刻
);
CREATE INDEX IF NOT EXISTS idx_details_ts ON inbound_call_details(ts);
"""

_COLUMNS = ("ts", "key_id", "key_name", "stream", "model_requested", "model_used",
            "upstream_key", "prompt_tokens", "completion_tokens", "total_tokens",
            "ttft_ms", "ttft_kind", "duration_ms", "status", "http_status", "retries",
            "client_ip", "error", "resolved_model", "substituted",
            "prompt_chars", "completion_chars", "usage_source",
            "session_id", "cached_tokens", "reasoning_tokens",
            "cache_creation_tokens", "finish_reason")

_DETAIL_COLUMNS = ("ts", "key_id", "key_name", "stream", "model_requested", "model_used",
                   "status", "error", "request_body", "response_body", "response_type",
                   "request_bytes", "response_bytes", "truncated", "truncated_what",
                   "session_id", "usage_json", "stored_at")


def _detail_cfg() -> dict[str, Any]:
    """[inbound.detail_logging] 配置（代码内置默认，config.toml 覆盖）。"""
    merged = dict(_DETAIL_CFG_DEFAULTS)
    try:
        from lib.config_reader import load_config
        cfg = load_config().get("inbound", {}).get("detail_logging", {})
        if isinstance(cfg, dict):
            merged.update(cfg)
    except Exception:  # 配置读取失败用内置默认（fail-closed 到关，安全）
        pass
    return merged


def detail_logging_enabled() -> bool:
    """detail logging 主开关（router 每请求入口查一次）。"""
    return bool(_detail_cfg().get("enabled", False))


def _truncate_body(text: str, limit_bytes: int) -> tuple[str, bool]:
    """把正文按字节上限截断，UTF-8 码点安全切分，尾部追加省略提示。

    返回 (截断后文本, 是否发生截断)。limit_bytes 须 ≥ 省略提示字节数，否则结果可能为空。
    """
    data = text.encode("utf-8")
    if len(data) <= limit_bytes:
        return text, False
    tail = _OMIT_SUFFIX.encode("utf-8")
    keep = max(0, limit_bytes - len(tail))
    cut = data[:keep]
    # 回退到合法 UTF-8 字符边界（跳过不完整多字节的续字节）
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + _OMIT_SUFFIX, True


class CallLog:
    """SQLite 调用日志（单写线程 + 读连接按需新建）。"""

    def __init__(self, db_path: Path | str = DB_PATH, retention_days: int = RETENTION_DAYS):
        self._path = Path(db_path)
        self._retention = retention_days
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_MAX)
        self._detail_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_MAX)
        self._dropped = 0          # fail-open 丢弃计数（可观测）
        self._dropped_detail = 0   # detail 队列丢弃计数（可观测）
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._init_db()

    # ---------- 写入路径 ----------

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        try:
            conn.executescript(_SCHEMA)
            # 前向迁移：老库补列（新库由 _SCHEMA 直接建好）。ADD COLUMN 幂等靠
            # 先查 PRAGMA，避免重复列报错。
            existing = {row[1] for row in conn.execute("PRAGMA table_info(inbound_calls)")}
            _added = (("resolved_model", "TEXT DEFAULT ''"),
                      ("substituted", "INTEGER DEFAULT 0"),
                      ("prompt_chars", "INTEGER DEFAULT 0"),
                      ("completion_chars", "INTEGER DEFAULT 0"),
                      ("usage_source", "TEXT DEFAULT ''"),
                      ("ttft_kind", "TEXT DEFAULT ''"),
                      ("session_id", "TEXT DEFAULT ''"),
                      ("cached_tokens", "INTEGER"),
                      ("reasoning_tokens", "INTEGER"),
                      ("cache_creation_tokens", "INTEGER"),
                      ("finish_reason", "TEXT DEFAULT ''"))
            for col, decl in _added:
                if col not in existing:
                    conn.execute(f"ALTER TABLE inbound_calls ADD COLUMN {col} {decl}")
            # 明细表同款前向迁移（此前只迁移主表；session_id 2026-09-08 加入，
            # usage_json 同日加入——provider 原始 usage 兜底落盘）
            detail_existing = {row[1] for row in
                               conn.execute("PRAGMA table_info(inbound_call_details)")}
            for col, decl in (("session_id", "TEXT NOT NULL DEFAULT ''"),
                              ("usage_json", "TEXT")):
                if col not in detail_existing:
                    conn.execute(f"ALTER TABLE inbound_call_details ADD COLUMN {col} {decl}")
            conn.commit()
        finally:
            conn.close()

    def record(self, entry: dict[str, Any]) -> None:
        """异步入队。绝不抛异常（fail-open）；队列满丢弃并计数。"""
        try:
            self._q.put_nowait(entry)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("inbound 调用日志队列已满，累计丢弃 %s 条", self._dropped)

    def record_detail(self, snap: dict[str, Any], *, request_body: str,
                      response_body: str, response_type: str,
                      usage_json: str | None = None) -> None:
        """异步入队请求/响应正文明细（独立快照表 inbound_call_details）。

        snap 是主表 entry（router 的 _base_entry + 终态回填），这里复制其定位/成败字段，
        使明细能独立回看（不 join 主表，生命周期完全解耦）。
        fail-open：队列满/异常一律丢弃，绝不影响请求路径。
        单条正文超 max_single_bytes 在入队前截断并置 truncated 标记（码点安全切分），
        绝不整条丢弃，也不让超限大串进入写线程 / DB。
        usage_json：provider 原始 usage 的 JSON 串（兜底落盘，None = 未拿到/未透出）。
        """
        try:
            cfg = _detail_cfg()
            lim = int(cfg.get("max_single_bytes") or 0) or _DETAIL_CFG_DEFAULTS["max_single_bytes"]
            req_bytes = len(request_body.encode("utf-8"))
            resp_bytes = len(response_body.encode("utf-8"))
            req, req_cut = _truncate_body(request_body, lim)
            resp, resp_cut = _truncate_body(response_body, lim)
            if req_cut and resp_cut:
                truncated_what = "both"
            elif req_cut:
                truncated_what = "request"
            elif resp_cut:
                truncated_what = "response"
            else:
                truncated_what = ""
            entry = {
                "ts": float(snap.get("ts") or time.time()),
                "key_id": str(snap.get("key_id", "")),
                "key_name": str(snap.get("key_name", "")),
                "stream": 1 if snap.get("stream") else 0,
                "model_requested": str(snap.get("model_requested", "")),
                "model_used": str(snap.get("model_used", "")),
                "status": str(snap.get("status", "error")),
                "error": str(snap.get("error", ""))[:500],
                "request_body": req,
                "response_body": resp,
                "response_type": str(response_type),
                "request_bytes": req_bytes,
                "response_bytes": resp_bytes,
                "truncated": 1 if truncated_what else 0,
                "truncated_what": truncated_what,
                "session_id": str(snap.get("session_id", "")),
                "usage_json": usage_json,
                "stored_at": time.time(),
            }
            self._detail_q.put_nowait(entry)
        except queue.Full:
            with self._lock:
                self._dropped_detail += 1
            if self._dropped_detail % 100 == 1:
                logger.warning("inbound 明细日志队列已满，累计丢弃 %s 条", self._dropped_detail)
        except Exception:
            # 明细截断/入队异常一律静默丢弃（fail-open）
            pass

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            last_cleanup = time.monotonic()
            while not self._stop.is_set():
                batch: list[dict[str, Any]] = []
                try:
                    item = self._q.get(timeout=_FLUSH_INTERVAL)
                    batch.append(item)
                    while len(batch) < _BATCH_SIZE:
                        batch.append(self._q.get_nowait())
                except queue.Empty:
                    pass
                except Exception:
                    pass
                if batch:
                    self._write_batch(conn, batch)
                    self._maybe_flush_keystore()
                self._drain_detail(conn)
                now = time.monotonic()
                if now - last_cleanup >= _CLEANUP_INTERVAL:
                    try:
                        self.cleanup(conn)
                    except Exception as e:
                        logger.error("inbound 日志清理失败: %s", e)
                    last_cleanup = now
            # stop 后把队列残留写完
            while True:
                batch = []
                try:
                    batch.append(self._q.get_nowait())
                    while len(batch) < _BATCH_SIZE:
                        batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
                if batch:
                    self._write_batch(conn, batch)
                    self._maybe_flush_keystore()
                self._drain_detail(conn)
                if not batch:
                    break
        finally:
            conn.close()

    def _write_batch(self, conn: sqlite3.Connection, batch: list[dict[str, Any]]) -> None:
        rows = []
        for e in batch:
            rows.append((
                float(e.get("ts") or time.time()),
                str(e.get("key_id", "")),
                str(e.get("key_name", "")),
                1 if e.get("stream") else 0,
                str(e.get("model_requested", "")),
                str(e.get("model_used", "")),
                str(e.get("upstream_key", "")),
                int(e.get("prompt_tokens") or 0),
                int(e.get("completion_tokens") or 0),
                int(e.get("total_tokens") or 0),
                e.get("ttft_ms"),
                str(e.get("ttft_kind", "")),
                int(e.get("duration_ms") or 0),
                str(e.get("status", "error")),
                int(e.get("http_status") or 0),
                int(e.get("retries") or 0),
                str(e.get("client_ip", "")),
                str(e.get("error", ""))[:500],
                str(e.get("resolved_model", "")),
                int(e.get("substituted") or 0),
                int(e.get("prompt_chars") or 0),
                int(e.get("completion_chars") or 0),
                str(e.get("usage_source", "")),
                str(e.get("session_id", "")),
                (int(e["cached_tokens"])
                 if e.get("cached_tokens") is not None else None),
                (int(e["reasoning_tokens"])
                 if e.get("reasoning_tokens") is not None else None),
                (int(e["cache_creation_tokens"])
                 if e.get("cache_creation_tokens") is not None else None),
                str(e.get("finish_reason", "")),
            ))
        placeholders = ",".join("?" * len(_COLUMNS))
        sql = f"INSERT INTO inbound_calls ({','.join(_COLUMNS)}) VALUES ({placeholders})"
        try:
            with conn:
                conn.executemany(sql, rows)
        except sqlite3.Error as e:
            logger.error("inbound 调用日志写入失败（fail-open 丢弃 %d 条）: %s", len(rows), e)

    @staticmethod
    def _maybe_flush_keystore() -> None:
        try:
            get_key_store().flush()  # last_used_at 脏数据随日志周期落盘
        except Exception:
            pass

    # ---------- 明细（detail logging）写入与窗口 ----------

    def _drain_detail(self, conn: sqlite3.Connection) -> None:
        """drain 明细队列现存全部并落库，随后执行窗口让位。"""
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._detail_q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        self._write_detail_batch(conn, batch)
        try:
            self._enforce_detail_window(conn)
        except sqlite3.Error as e:
            logger.error("inbound 明细窗口清理失败: %s", e)

    def _write_detail_batch(self, conn: sqlite3.Connection,
                            batch: list[dict[str, Any]]) -> None:
        rows = []
        for e in batch:
            rows.append((
                float(e["ts"]),
                str(e["key_id"]),
                str(e["key_name"]),
                int(e["stream"] or 0),
                str(e["model_requested"]),
                str(e["model_used"]),
                str(e["status"]),
                str(e["error"]),
                str(e["request_body"]),
                str(e["response_body"]),
                str(e["response_type"]),
                int(e["request_bytes"]),
                int(e["response_bytes"]),
                1 if e.get("truncated") else 0,
                str(e.get("truncated_what", "")),
                str(e.get("session_id", "")),
                e.get("usage_json"),
                float(e.get("stored_at") or time.time()),
            ))
        sql = (f"INSERT INTO inbound_call_details ({','.join(_DETAIL_COLUMNS)}) "
               f"VALUES ({','.join('?' * len(_DETAIL_COLUMNS))})")
        try:
            with conn:
                conn.executemany(sql, rows)
        except sqlite3.Error as e:
            logger.error("inbound 明细日志写入失败（fail-open 丢弃 %d 条）: %s", len(rows), e)

    def _enforce_detail_window(self, conn: sqlite3.Connection) -> None:
        """按明细窗口上限让位：整体条数 / 整体体积任一超限即删最老（不联动主表）。

        语义（用户约束）：整体超 50 条或超 500MB 就丢最老给新请求让位；
        单条超限已在 record_detail 入队前截断，此处只处理整体维度。
        """
        cfg = _detail_cfg()
        max_records = int(cfg.get("max_records") or 0) or _DETAIL_CFG_DEFAULTS["max_records"]
        max_total = int(cfg.get("max_total_bytes") or 0) or _DETAIL_CFG_DEFAULTS["max_total_bytes"]
        if max_records <= 0 and max_total <= 0:
            return
        # ① 条数让位：只保留最新 max_records 条（ts 相同时以更高 id 为新）
        if max_records > 0:
            with conn:
                conn.execute(
                    "DELETE FROM inbound_call_details WHERE id IN ("
                    "SELECT id FROM inbound_call_details "
                    "ORDER BY ts DESC, id DESC LIMIT -1 OFFSET ?)",
                    (max_records,))
        # ② 体积让位：逐条删最老直到总正文 ≤ max_total（writer conn 无 row_factory，用索引）
        if max_total > 0:
            while True:
                row = conn.execute(
                    "SELECT COALESCE(SUM(request_bytes + response_bytes),0) AS tot, "
                    "MIN(id) AS oldest_id FROM inbound_call_details"
                ).fetchone()
                if not row or (row[0] or 0) <= max_total or row[1] is None:
                    break
                with conn:
                    conn.execute("DELETE FROM inbound_call_details WHERE id = ?",
                                 (row[1],))


    def cleanup(self, conn: sqlite3.Connection | None = None) -> int:
        """删除超过保留期的行，返回删除数。"""
        cutoff = time.time() - self._retention * 86400
        own = conn is None
        if own:
            conn = sqlite3.connect(self._path)
        try:
            with conn:
                cur = conn.execute("DELETE FROM inbound_calls WHERE ts < ?", (cutoff,))
                return cur.rowcount
        finally:
            if own:
                conn.close()

    def flush(self) -> None:
        """同步写空队列（测试/集成确认用；生产路径靠后台线程批量写）。

        同时 drain 明细队列并执行窗口让位，保证 record_detail 后可立即断言。
        """
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            batch: list[dict[str, Any]] = []
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if batch:
                self._write_batch(conn, batch)
            self._drain_detail(conn)
        finally:
            conn.close()

    # ---------- 读取路径 ----------

    def _connect_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, *, key_id: str | None = None, model: str | None = None,
              status: str | None = None, stream: bool | None = None,
              date_from: str | None = None, date_to: str | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
        """按条件查调用明细（面板「调用日志」页）。"""
        conds, params = [], []
        if key_id:
            conds.append("key_id = ?")
            params.append(key_id)
        if model:
            conds.append("(model_requested = ? OR model_used = ?)")
            params.extend([model, model])
        if status in ("ok", "error"):
            conds.append("status = ?")
            params.append(status)
        if stream is not None:
            conds.append("stream = ?")
            params.append(1 if stream else 0)
        if date_from:
            try:
                ts = datetime.strptime(date_from, "%Y-%m-%d").timestamp()
                conds.append("ts >= ?")
                params.append(ts)
            except ValueError:
                pass
        if date_to:
            try:
                ts = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).timestamp()
                conds.append("ts < ?")
                params.append(ts)
            except ValueError:
                pass
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        sql = f"SELECT * FROM inbound_calls {where} ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        try:
            conn = self._connect_read()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error("inbound 日志查询失败: %s", e)
            return []

    @staticmethod
    def _percentile(sorted_vals: list[int], p: float) -> int | None:
        """排序后线性插值取分位；空返回 None。"""
        if not sorted_vals:
            return None
        if len(sorted_vals) == 1:
            return sorted_vals[0]
        k = (len(sorted_vals) - 1) * p / 100.0
        lo = int(k)
        hi = min(lo + 1, len(sorted_vals) - 1)
        frac = k - lo
        return int(round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac))

    def aggregate(self, *, days: int = 1, date_from: str | None = None,
                  date_to: str | None = None) -> dict[str, Any]:
        """Token 统计页全部数字（概览卡 + 按连接 + 按转发目标）。

        date_from/date_to（YYYY-MM-DD，两端闭区间）优先于 days；
        格式与跨度校验由调用方（router）负责。
        """
        until: float | None = None
        if date_from or date_to:
            base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            d0 = datetime.strptime(date_from, "%Y-%m-%d") if date_from else base
            d1 = datetime.strptime(date_to, "%Y-%m-%d") if date_to else base
            since = d0.timestamp()
            until = (d1 + timedelta(days=1)).timestamp()
        else:
            since = time.time() - days * 86400
        empty = {
            "overview": {"requests": 0, "failed": 0, "retries": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "total_tokens": 0,
                         "ttft_p50_ms": None, "ttft_p95_ms": None, "ttft_samples": 0,
                         "success_rate": None, "substituted": 0,
                         "cached_tokens": 0, "cached_samples": 0,
                         "reasoning_tokens": 0, "reasoning_samples": 0,
                         "cache_creation_tokens": 0, "cache_creation_samples": 0},
            "by_key": [], "by_upstream": [], "days": days,
        }
        try:
            conn = self._connect_read()
        except sqlite3.Error as e:
            logger.error("inbound 统计查询失败: %s", e)
            return empty
        try:
            sql = ("SELECT key_id, key_name, upstream_key, status, stream, retries, "
                   "total_tokens, prompt_tokens, completion_tokens, ttft_ms, duration_ms, "
                   "substituted, cached_tokens, reasoning_tokens, cache_creation_tokens "
                   "FROM inbound_calls WHERE ts >= ?")
            params: list[Any] = [since]
            if until is not None:
                sql += " AND ts < ?"
                params.append(until)
            rows = conn.execute(sql + " LIMIT 200000", params).fetchall()
        except sqlite3.Error as e:
            logger.error("inbound 统计查询失败: %s", e)
            return empty
        finally:
            conn.close()

        # ---- 概览 ----
        ttfts = sorted(r["ttft_ms"] for r in rows if r["ttft_ms"] is not None)
        n_req = len(rows)
        n_fail = sum(1 for r in rows if r["status"] != "ok")
        # 缓存命中：只聚合 provider 上报过 cached_tokens 的调用（None = 未上报，不计入）
        cached_vals = [r["cached_tokens"] for r in rows if r["cached_tokens"] is not None]
        reasoning_vals = [r["reasoning_tokens"] for r in rows
                          if r["reasoning_tokens"] is not None]
        ccreate_vals = [r["cache_creation_tokens"] for r in rows
                        if r["cache_creation_tokens"] is not None]
        overview = {
            "requests": n_req,
            "failed": n_fail,
            "retries": sum(r["retries"] or 0 for r in rows),
            "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
            "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
            "total_tokens": sum(r["total_tokens"] or 0 for r in rows),
            "ttft_p50_ms": self._percentile(ttfts, 50),
            "ttft_p95_ms": self._percentile(ttfts, 95),
            "ttft_samples": len(ttfts),
            "success_rate": round(100.0 * (n_req - n_fail) / n_req, 1) if n_req else None,
            "substituted": sum(1 for r in rows if r["substituted"]),
            "cached_tokens": sum(cached_vals),
            "cached_samples": len(cached_vals),
            "reasoning_tokens": sum(reasoning_vals),
            "reasoning_samples": len(reasoning_vals),
            "cache_creation_tokens": sum(ccreate_vals),
            "cache_creation_samples": len(ccreate_vals),
        }

        # ---- 按连接 ----
        def _pct(ok: int, total: int) -> float | None:
            return round(100.0 * ok / total, 1) if total else None

        by_key_acc: dict[str, dict[str, Any]] = {}
        by_up_acc: dict[str, dict[str, Any]] = {}
        for r in rows:
            for bucket, kfield in ((by_key_acc, "key_id"), (by_up_acc, "upstream_key")):
                k = r[kfield] or "(unknown)"
                acc = bucket.setdefault(k, {
                    "label": r["key_name"] if kfield == "key_id" else k,
                    "requests": 0, "ok": 0, "tokens": 0, "ttfts": [], "durations": [],
                    "substituted": 0,
                })
                acc["requests"] += 1
                if r["status"] == "ok":
                    acc["ok"] += 1
                if r["substituted"]:
                    acc["substituted"] += 1
                acc["tokens"] += r["total_tokens"] or 0
                if r["ttft_ms"] is not None:
                    acc["ttfts"].append(r["ttft_ms"])
                if kfield == "upstream_key":
                    acc["durations"].append(r["duration_ms"] or 0)

        by_key = []
        for k, acc in sorted(by_key_acc.items(), key=lambda x: -x[1]["tokens"]):
            tfts = sorted(acc["ttfts"])
            by_key.append({
                "key_id": k, "key_name": acc["label"],
                "requests": acc["requests"], "tokens": acc["tokens"],
                "ttft_p50_ms": self._percentile(tfts, 50),
                "success_rate": _pct(acc["ok"], acc["requests"]),
                "substituted": acc["substituted"],
            })
        by_upstream = []
        for k, acc in sorted(by_up_acc.items(), key=lambda x: -x[1]["tokens"]):
            durs = sorted(acc["durations"])
            by_upstream.append({
                "upstream_key": k,
                "requests": acc["requests"], "tokens": acc["tokens"],
                "p95_duration_ms": self._percentile(durs, 95),
                "success_rate": _pct(acc["ok"], acc["requests"]),
            })

        return {"overview": overview, "by_key": by_key,
                "by_upstream": by_upstream, "days": days}

    def today_count_by_key(self) -> dict[str, int]:
        """当日（本地时区 0 点起）各连接调用数（面板「今日调用」列）。"""
        day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp()
        try:
            conn = self._connect_read()
            try:
                rows = conn.execute(
                    "SELECT key_id, COUNT(*) AS c FROM inbound_calls "
                    "WHERE ts >= ? GROUP BY key_id", (day_start,)
                ).fetchall()
                return {r["key_id"]: r["c"] for r in rows}
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error("今日调用统计失败: %s", e)
            return {}

    def count_all(self) -> int:
        try:
            conn = self._connect_read()
            try:
                return conn.execute("SELECT COUNT(*) FROM inbound_calls").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return -1

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._writer_loop,
                                        name="inbound-call-log", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def dropped_detail(self) -> int:
        with self._lock:
            return self._dropped_detail


_log: CallLog | None = None
_log_lock = threading.Lock()


def get_call_log() -> CallLog:
    """进程级单例；首次调用自动 start() 后台线程并做一次启动懒清理。"""
    global _log
    with _log_lock:
        if _log is None:
            _log = CallLog()
            try:
                _log.cleanup()
            except Exception as e:
                logger.error("inbound 日志启动清理失败: %s", e)
            _log.start()
        return _log


def reset_call_log(db_path: Path | str | None = None) -> CallLog:
    """重置单例（测试用）。"""
    global _log
    with _log_lock:
        if _log is not None:
            _log.stop()
        _log = CallLog(db_path) if db_path is not None else CallLog()
        return _log


def record(entry: dict[str, Any]) -> None:
    """模块级便捷入口（router 调用）。"""
    get_call_log().record(entry)


def record_detail(snap: dict[str, Any], *, request_body: str,
                  response_body: str, response_type: str,
                  usage_json: str | None = None) -> None:
    """模块级便捷入口：落请求/响应正文明细（router 调用）。

    仅在 detail_logging_enabled() 为真时调用（router 已判）；此函数不再二次查开关。
    snap 为主表 entry（含终态 status/error/usage），用于明细独立定位。
    usage_json：provider 原始 usage 的 JSON 串（兜底落盘，None = 未拿到）。
    """
    get_call_log().record_detail(snap, request_body=request_body,
                                 response_body=response_body, response_type=response_type,
                                 usage_json=usage_json)


def query(**kw: Any) -> list[dict[str, Any]]:
    """模块级便捷入口（router 调用）。"""
    return get_call_log().query(**kw)


def aggregate(**kw: Any) -> dict[str, Any]:
    """模块级便捷入口（router 调用）。"""
    return get_call_log().aggregate(**kw)


def today_count_by_key() -> dict[str, int]:
    """模块级便捷入口（router 调用）。"""
    return get_call_log().today_count_by_key()
