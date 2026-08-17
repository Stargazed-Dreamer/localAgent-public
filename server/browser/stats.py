"""浏览器选择器调用统计（评估文档 P2 第 3 项）+ selector_stats 端点。

记录 browser_action 的每次调用结果（domain + target_type + target_value + success + ts），
支持按站点查询选择器成功率，让 agent 在已知稳定的 selector 上重复使用。

从 server/browser_stats.py 迁移至 server/browser/ 包（Ticket 03）。
代码与原定义完全一致，仅调整 import 路径：
- get_browser_config：`from .config import` → `from ..config import`（包嵌套层级变化）
- 默认 db_path 路径：`Path(__file__).parent.parent` → `Path(__file__).parent.parent.parent`
  （stats.py 位于 server/browser/，比原 browser_stats.py 深一层）

同时迁移 browser_selector_stats 端点（从 server/browser.py）：
- Pydantic 模型：BrowserSelectorStatsRequest/Response
- 端点函数：browser_selector_stats（暂不注册到 router，Ticket 04 处理）

架构：
- BrowserStatsStore 单例（get_stats_store()）：长连接 SQLite，WAL 模式
- record_selector_call(...)：异步 fire-and-forget 写入（不阻塞响应）
- query_stats(...)：按 domain 聚合，返回每个 selector 的成功率 + 最近验证日期

数据库：默认 data/browser_stats.db（可经 [browser].stats_db_path 配置）
线程模型：所有方法可在 async/sync 上下文调用；内部用 threading.Lock 保护写入。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.browser_stats")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS browser_selector_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_value TEXT NOT NULL,
    action TEXT NOT NULL,
    success INTEGER NOT NULL,  -- 0/1
    error_code TEXT,
    elapsed_ms INTEGER,
    ts INTEGER NOT NULL  -- unix timestamp
);
CREATE INDEX IF NOT EXISTS idx_bss_domain_value ON browser_selector_stats(domain, target_value);
CREATE INDEX IF NOT EXISTS idx_bss_domain_success ON browser_selector_stats(domain, success);
CREATE INDEX IF NOT EXISTS idx_bss_ts ON browser_selector_stats(ts);

-- summary 表：归档已清理明细的聚合统计（方案 Y 清理时归档）
-- 主键 (domain, target_type, target_value)，永久保留
-- aggregate_and_cleanup() 把待删明细聚合 UPSERT（累加）到本表后再删除明细
CREATE TABLE IF NOT EXISTS browser_selector_stats_summary (
    domain TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_value TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_success_ts INTEGER NOT NULL DEFAULT 0,
    last_fail_ts INTEGER NOT NULL DEFAULT 0,
    last_call_ts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, target_type, target_value)
);
"""


class BrowserStatsStore:
    """浏览器选择器统计存储（单例）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        """初始化连接 + 建表（幂等）。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        # 确保目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._conn.executescript(_SCHEMA_SQL)
        # 旧版数据库可能缺少 last_fail_ts 列（CREATE TABLE IF NOT EXISTS 不会补列）
        # 修复 2026-08-07: cleanup 任务因 schema 不匹配连续失败 5 次被自动暂停
        self._migrate_summary_schema()
        self._conn.commit()
        logger.info("BrowserStats: 已初始化 %s", self.db_path)

    def _migrate_summary_schema(self) -> None:
        """补齐 browser_selector_stats_summary 缺失的列（向前兼容旧库）"""
        assert self._conn is not None
        cols = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(browser_selector_stats_summary)"
        ).fetchall()}
        # summary 表所有应存在列 → 默认值（与 _SCHEMA_SQL 中定义一致）
        expected = {
            "last_success_ts": 0,
            "last_fail_ts": 0,
            "last_call_ts": 0,
            "updated_at": "",
        }
        for col, default in expected.items():
            if col not in cols:
                # SQLite ALTER TABLE ADD COLUMN 不支持 NOT NULL 无默认值，故加 DEFAULT
                col_type = "TEXT" if col == "updated_at" else "INTEGER"
                col_default = "''" if col == "updated_at" else str(default)
                self._conn.execute(
                    f"ALTER TABLE browser_selector_stats_summary "
                    f"ADD COLUMN {col} {col_type} NOT NULL DEFAULT {col_default}"
                )
                logger.info("BrowserStats: 迁移补列 browser_selector_stats_summary.%s", col)

    def _ensure_connected(self) -> sqlite3.Connection | None:
        if self._conn is None:
            return None
        try:
            self._conn.execute("SELECT 1")
        except sqlite3.Error:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.executescript(_SCHEMA_SQL)
                self._conn.commit()
            except Exception as e:
                logger.warning("BrowserStats: 重连失败 %s", e)
                return None
        return self._conn

    def record_selector_call(
        self,
        url: str,
        target_type: str,
        target_value: str,
        action: str,
        success: bool,
        error_code: str | None = None,
        elapsed_ms: int = 0,
    ) -> None:
        """记录一次 selector 调用。同步方法，调用方应在后台线程或同步上下文调用。

        失败不抛异常，只记日志（统计是辅助功能，不能影响主流程）。
        """
        conn = self._ensure_connected()
        if conn is None:
            return
        try:
            domain = self._extract_domain(url)
            with self._lock:
                conn.execute(
                    """INSERT INTO browser_selector_stats
                       (domain, target_type, target_value, action, success, error_code, elapsed_ms, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        domain, target_type, target_value, action,
                        1 if success else 0, error_code, elapsed_ms, int(time.time()),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug("BrowserStats: 记录失败 %s", e)

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取 hostname；失败返回 'unknown'。"""
        if not url:
            return "unknown"
        try:
            host = urlparse(url).hostname or ""
            return host or "unknown"
        except Exception:
            return "unknown"

    def query_stats(
        self,
        domain: str | None = None,
        target_type: str | None = None,
        since_days: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """查询选择器统计，按 (domain, target_type, target_value) 聚合。

        分流逻辑：
        - since_days is not None → 查明细表（时间精确，agent 日常场景）
        - since_days is None → 合并 summary + 明细表（全量统计，含已归档数据）

        返回每个 selector 的：
        - total: 总调用次数
        - success_count / fail_count
        - success_rate: 0.0–1.0
        - last_success_ts / last_fail_ts: unix 时间戳（0 表示无）
        - last_verified_at: 最近一次成功调用的时间戳
        """
        conn = self._ensure_connected()
        if conn is None:
            return []
        try:
            # 共用的 domain/target_type 过滤条件
            filter_parts = []
            filter_params: list = []
            if domain:
                filter_parts.append("domain = ?")
                filter_params.append(domain)
            if target_type:
                filter_parts.append("target_type = ?")
                filter_params.append(target_type)
            filter_clause = ("WHERE " + " AND ".join(filter_parts)) if filter_parts else ""

            if since_days is not None:
                # 走明细表（时间精确）
                cutoff = int(time.time()) - since_days * 86400
                where_parts = list(filter_parts)
                params = list(filter_params)
                where_parts.append("ts >= ?")
                params.append(cutoff)
                where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                sql = f"""
                    SELECT
                        domain, target_type, target_value,
                        COUNT(*) AS total,
                        SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail_count,
                        MAX(CASE WHEN success=1 THEN ts ELSE 0 END) AS last_success_ts,
                        MAX(CASE WHEN success=0 THEN ts ELSE 0 END) AS last_fail_ts,
                        MAX(ts) AS last_call_ts
                    FROM browser_selector_stats
                    {where_clause}
                    GROUP BY domain, target_type, target_value
                    ORDER BY total DESC
                    LIMIT ?
                """
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            else:
                # 合并 summary + 明细（全量统计）
                # summary 子查询用 filter_clause，明细子查询也用 filter_clause（无时间过滤）
                sql = f"""
                    SELECT
                        domain, target_type, target_value,
                        SUM(total) AS total,
                        SUM(success_count) AS success_count,
                        SUM(fail_count) AS fail_count,
                        MAX(last_success_ts) AS last_success_ts,
                        MAX(last_fail_ts) AS last_fail_ts,
                        MAX(last_call_ts) AS last_call_ts
                    FROM (
                        SELECT domain, target_type, target_value,
                               COUNT(*) AS total,
                               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success_count,
                               SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail_count,
                               MAX(CASE WHEN success=1 THEN ts ELSE 0 END) AS last_success_ts,
                               MAX(CASE WHEN success=0 THEN ts ELSE 0 END) AS last_fail_ts,
                               MAX(ts) AS last_call_ts
                        FROM browser_selector_stats
                        {filter_clause}
                        GROUP BY domain, target_type, target_value
                        UNION ALL
                        SELECT domain, target_type, target_value,
                               total, success_count, fail_count,
                               last_success_ts, last_fail_ts, last_call_ts
                        FROM browser_selector_stats_summary
                        {filter_clause}
                    )
                    GROUP BY domain, target_type, target_value
                    ORDER BY total DESC
                    LIMIT ?
                """
                # filter_params 用于两个子查询，需要传两遍
                params = list(filter_params) + list(filter_params) + [limit]
                rows = conn.execute(sql, params).fetchall()

            out = []
            for r in rows:
                total = r["total"]
                success_count = r["success_count"]
                rate = (success_count / total) if total > 0 else 0.0
                out.append({
                    "domain": r["domain"],
                    "target_type": r["target_type"],
                    "target_value": r["target_value"],
                    "total": total,
                    "success_count": success_count,
                    "fail_count": r["fail_count"],
                    "success_rate": round(rate, 3),
                    "last_success_ts": r["last_success_ts"],
                    "last_fail_ts": r["last_fail_ts"],
                    "last_call_ts": r["last_call_ts"],
                    "last_verified_at": r["last_success_ts"],
                })
            return out
        except Exception as e:
            logger.warning("BrowserStats: 查询失败 %s", e)
            return []

    def list_domains(self) -> list[str]:
        """列出所有已记录的 domain（按调用次数降序，合并明细+summary）。"""
        conn = self._ensure_connected()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """SELECT domain, SUM(cnt) AS total_cnt FROM (
                       SELECT domain, COUNT(*) AS cnt FROM browser_selector_stats GROUP BY domain
                       UNION ALL
                       SELECT domain, total AS cnt FROM browser_selector_stats_summary
                   ) GROUP BY domain ORDER BY total_cnt DESC"""
            ).fetchall()
            return [r["domain"] for r in rows]
        except Exception:
            return []

    def aggregate_and_cleanup(self, retention_days: int = 30) -> dict:
        """聚合过期明细到 summary 表后删除（方案 Y 清理时归档）。

        流程：
        1. 查 ts < now - retention_days 的明细，按 (domain, target_type, target_value) 聚合
        2. UPSERT（累加）到 browser_selector_stats_summary
        3. 删除这些过期明细

        返回 {"aggregated": <聚合行数>, "deleted": <删除明细数>, "retention_days": N}
        """
        conn = self._ensure_connected()
        if conn is None:
            return {"aggregated": 0, "deleted": 0, "retention_days": retention_days}
        try:
            cutoff = int(time.time()) - retention_days * 86400
            with self._lock:
                # 1. 聚合过期明细到 summary（UPSERT 累加）
                cur = conn.execute(
                    """INSERT INTO browser_selector_stats_summary
                       (domain, target_type, target_value, total, success_count,
                        fail_count, last_success_ts, last_fail_ts, last_call_ts, updated_at)
                       SELECT
                           domain, target_type, target_value,
                           COUNT(*) AS total,
                           SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success_count,
                           SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fail_count,
                           MAX(CASE WHEN success=1 THEN ts ELSE 0 END) AS last_success_ts,
                           MAX(CASE WHEN success=0 THEN ts ELSE 0 END) AS last_fail_ts,
                           MAX(ts) AS last_call_ts,
                           datetime('now', 'localtime') AS updated_at
                       FROM browser_selector_stats
                       WHERE ts < ?
                       GROUP BY domain, target_type, target_value
                       ON CONFLICT(domain, target_type, target_value) DO UPDATE SET
                           total = browser_selector_stats_summary.total + excluded.total,
                           success_count = browser_selector_stats_summary.success_count + excluded.success_count,
                           fail_count = browser_selector_stats_summary.fail_count + excluded.fail_count,
                           last_success_ts = MAX(browser_selector_stats_summary.last_success_ts, excluded.last_success_ts),
                           last_fail_ts = MAX(browser_selector_stats_summary.last_fail_ts, excluded.last_fail_ts),
                           last_call_ts = MAX(browser_selector_stats_summary.last_call_ts, excluded.last_call_ts),
                           updated_at = excluded.updated_at""",
                    (cutoff,),
                )
                aggregated = cur.rowcount
                # 2. 删除过期明细
                cur = conn.execute(
                    "DELETE FROM browser_selector_stats WHERE ts < ?",
                    (cutoff,),
                )
                deleted = cur.rowcount
                conn.commit()
            logger.info(
                "BrowserStats: aggregate_and_cleanup retention=%ddays aggregated=%d deleted=%d",
                retention_days, aggregated, deleted,
            )
            return {
                "aggregated": aggregated,
                "deleted": deleted,
                "retention_days": retention_days,
            }
        except Exception as e:
            logger.warning("BrowserStats: aggregate_and_cleanup 失败 %s", e)
            return {"aggregated": 0, "deleted": 0, "retention_days": retention_days, "error": str(e)}


# 单例
_store: BrowserStatsStore | None = None
_store_lock = threading.Lock()


def get_stats_store() -> BrowserStatsStore:
    """获取 BrowserStatsStore 单例。首次调用时初始化。"""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            from ..config import get_browser_config
            cfg = get_browser_config()
            db_path = cfg.get("stats_db_path", "")
            if not db_path:
                # 默认 data/browser_stats.db
                # stats.py 位于 server/browser/，比原 browser_stats.py 深一层，
                # 需要向上两级才到项目根目录
                db_path = str(Path(__file__).parent.parent.parent / "data" / "browser_stats.db")
            _store = BrowserStatsStore(db_path)
            try:
                _store.initialize()
            except Exception as e:
                logger.warning("BrowserStats: 初始化失败 %s（统计功能将不可用）", e)
    return _store


def record_selector_call(
    url: str,
    target_type: str,
    target_value: str,
    action: str,
    success: bool,
    error_code: str | None = None,
    elapsed_ms: int = 0,
) -> None:
    """便捷封装：获取单例并记录调用。失败静默。"""
    try:
        store = get_stats_store()
        store.record_selector_call(
            url=url, target_type=target_type, target_value=target_value,
            action=action, success=success, error_code=error_code, elapsed_ms=elapsed_ms,
        )
    except Exception as e:
        logger.debug("BrowserStats: record_selector_call 失败 %s", e)


# ========== selector_stats 端点（@router 装饰器注册到 routes.py 的 router） ==========

from .routes import router  # noqa: E402


class BrowserSelectorStatsRequest(BaseSchema):
    """选择器成功率查询请求（评估文档 P2 第 3 项）。

    browser_action 的每次调用都会被记录到 browser_stats.db，此接口按
    (domain, target_type, target_value) 聚合返回成功率。

    - domain: 站点域名（如 "www.bilibili.com"）；不传则全部
    - target_type: css/role/label/placeholder/testid/text；不传则全部
    - since_days: 只查最近 N 天；不传则不限
    - limit: 最多返回条数（默认 50）
    """
    domain: str | None = None
    target_type: str | None = None
    since_days: int | None = None
    limit: int = 50


class BrowserSelectorStatsResponse(BaseSchema):
    """选择器成功率查询响应。

    - stats: 每个 selector 的聚合统计，按总调用次数降序：
      - domain, target_type, target_value
      - total: 总调用次数
      - success_count / fail_count
      - success_rate: 0.0–1.0
      - last_success_ts / last_fail_ts / last_call_ts: unix 时间戳（0=无）
      - last_verified_at: 最近一次成功调用的时间戳（agent 可据此判断 selector 新鲜度）
    - available_domains: 已记录的所有 domain 列表（按调用次数降序）
    """
    success: bool
    stats: list[dict]
    available_domains: list[str]
    elapsed_ms: int


@router.post("/selector_stats", response_model=BrowserSelectorStatsResponse, operation_id="browser_selector_stats")
async def browser_selector_stats(req: BrowserSelectorStatsRequest):
    """Query selector success-rate statistics aggregated by domain+target.

    Records every browser_action call (success or failure) to browser_stats.db.
    Use this to find selectors with high success rates and avoid known-bad ones.

    - domain: site hostname (e.g. "www.bilibili.com"); omit for all.
    - target_type: css/role/label/placeholder/testid/text; omit for all.
    - since_days: only count calls within last N days; omit for all time.
    - limit: max entries (default 50).

    Each entry includes total/success_count/fail_count/success_rate and
    last_verified_at (timestamp of last successful call) — agents can use
    last_verified_at to judge selector freshness.

    Example:
      browser_selector_stats(domain="www.bilibili.com", since_days=7)
      browser_selector_stats(target_type="testid", limit=20)
    """
    import time as _time
    start = _time.perf_counter()
    store = get_stats_store()
    stats = store.query_stats(
        domain=req.domain, target_type=req.target_type,
        since_days=req.since_days, limit=req.limit,
    )
    available_domains = store.list_domains()
    elapsed = int((_time.perf_counter() - start) * 1000)
    return BrowserSelectorStatsResponse(
        success=True, stats=stats, available_domains=available_domains,
        elapsed_ms=elapsed,
    )
