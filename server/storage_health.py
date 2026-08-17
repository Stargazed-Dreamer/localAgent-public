"""存储膨胀告警：监控 memory facts 行数、todos archived 行数、memory.db 文件大小。

超阈值时推送 inbox 告警，同 source pending 去重（用户 resolve/ignore 后才可能再推）。
挂到 activity_tracker cleanup cron（每小时半点）。

监控对象（精准范围，不扩展全局）：
- memory.db facts 表行数（阈值 memory_facts_warn_rows，默认 5000）
- memory.db todos 表 status='archived' 记录数（阈值 todos_archived_warn_count，默认 1000）
- memory.db 文件大小（阈值 db_file_warn_size_mb，默认 50MB）

不监控：accounting.db（业务数据永久保留）、stock_history.duckdb（有 LRU）、
inbox.db/browser_stats.db（已加清理）、llm_pool_stats.json（累计状态非日志）。

注意：todos 表实际存在于 memory.db 中（非独立 todos.db），因此文件大小告警
只针对 memory.db 一个文件，source 为 storage_growth.db_size.memory。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from server.config import get_cleanup_config

logger = logging.getLogger("localagent.storage_health")


def _resolve_mem_db_path() -> Path:
    """获取 memory.db 路径，相对路径相对于项目根目录解析。"""
    from server.memory.config import get_memory_config
    p = Path(get_memory_config().db_path)
    if not p.is_absolute():
        # storage_health.py 在 server/，2 级 parent 到项目根
        p = Path(__file__).resolve().parent.parent / p
    return p


def _has_pending_alert(store, source: str) -> bool:
    """检查 inbox 是否已有同 source 的 pending 条目（去重）。

    查询失败时返回 False（宁可重复推也不要漏推）。
    """
    try:
        existing = store.list(status="pending", source=source, limit=1)
        return len(existing) > 0
    except Exception as e:
        logger.warning("storage_health: 查询 inbox pending 失败 source=%s err=%s", source, e)
        return False


def _push_alert(store, source: str, title: str, description: str, payload: dict) -> None:
    """推送一条 inbox 告警（调用方应先去重）。"""
    try:
        store.create({
            "source": source,
            "category": "system",
            "title": title,
            "description": description,
            "payload": payload,
        })
        logger.info("storage_health: 推送告警 source=%s title=%s", source, title)
    except Exception as e:
        logger.warning("storage_health: 推送告警失败 source=%s err=%s", source, e)


def _query_facts_count(db_path: Path) -> int | None:
    """查询 memory.db facts 表行数。失败返回 None。"""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM facts")
            return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("storage_health: 查询 facts 行数失败: %s", e)
        return None


def _query_todos_archived_count(db_path: Path) -> int | None:
    """查询 memory.db todos 表 status='archived' 记录数。失败返回 None。"""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM todos WHERE status = 'archived'")
            return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("storage_health: 查询 todos archived 行数失败: %s", e)
        return None


def _query_db_size_mb(db_path: Path) -> float | None:
    """查询 db 文件大小（MB）。文件不存在返回 None。"""
    try:
        if not db_path.exists():
            return None
        return db_path.stat().st_size / (1024 * 1024)
    except Exception as e:
        logger.warning("storage_health: 查询 db 文件大小失败: %s", e)
        return None


def check_storage_growth() -> dict:
    """检查 memory facts / todos archived / memory.db 文件大小，超阈值推 inbox 告警。

    去重：推送前查 inbox 同 source 的 pending 条目，有则跳过。用户 resolve 后若
    问题仍在，下次检查会再推（合理）；ignore 后在 inbox auto_cleanup 周期内
    （默认 10 天）不重复推。

    返回 {
        "checked": [{"metric": ..., "value": ..., "threshold": ...}, ...],
        "alerts_pushed": [source, ...],
        "alerts_skipped_dedup": [source, ...],
    }
    """
    cfg = get_cleanup_config()
    facts_warn = cfg["memory_facts_warn_rows"]
    todos_warn = cfg["todos_archived_warn_count"]
    db_size_warn_mb = cfg["db_file_warn_size_mb"]

    # 延迟导入避免循环依赖
    from server.inbox import get_store as get_inbox_store

    inbox_store = get_inbox_store()
    mem_db_path = _resolve_mem_db_path()

    result: dict = {"checked": [], "alerts_pushed": [], "alerts_skipped_dedup": []}

    # 1. memory facts 行数
    facts_count = _query_facts_count(mem_db_path)
    if facts_count is not None:
        result["checked"].append({
            "metric": "memory_facts",
            "value": facts_count,
            "threshold": facts_warn,
        })
        if facts_count > facts_warn:
            source = "storage_growth.memory_facts"
            if _has_pending_alert(inbox_store, source):
                result["alerts_skipped_dedup"].append(source)
            else:
                _push_alert(
                    inbox_store,
                    source,
                    title=f"记忆库 facts 行数超阈值 ({facts_count} > {facts_warn})",
                    description=(
                        f"memory.db facts 表当前 {facts_count} 行，超阈值 {facts_warn}。"
                        f"建议检查 memory maintainer 是否正常运行，或手动清理过期记忆。"
                    ),
                    payload={
                        "severity": "warning",
                        "metric": "memory_facts",
                        "current": facts_count,
                        "threshold": facts_warn,
                        "db_path": str(mem_db_path),
                        "suggestion": "检查 memory maintainer 是否正常运行，或手动清理 stale 记忆",
                    },
                )
                result["alerts_pushed"].append(source)

    # 2. todos archived 行数
    archived_count = _query_todos_archived_count(mem_db_path)
    if archived_count is not None:
        result["checked"].append({
            "metric": "todos_archived",
            "value": archived_count,
            "threshold": todos_warn,
        })
        if archived_count > todos_warn:
            source = "storage_growth.todos_archived"
            if _has_pending_alert(inbox_store, source):
                result["alerts_skipped_dedup"].append(source)
            else:
                _push_alert(
                    inbox_store,
                    source,
                    title=f"待办 archived 记录超阈值 ({archived_count} > {todos_warn})",
                    description=(
                        f"todos 表 archived 状态记录 {archived_count} 条，超阈值 {todos_warn}。"
                        f"建议手动清理历史 archived 待办。"
                    ),
                    payload={
                        "severity": "warning",
                        "metric": "todos_archived",
                        "current": archived_count,
                        "threshold": todos_warn,
                        "db_path": str(mem_db_path),
                        "suggestion": "考虑手动清理历史 archived 待办",
                    },
                )
                result["alerts_pushed"].append(source)

    # 3. memory.db 文件大小（todos 表也在 memory.db 中，无需单独监控 todos.db）
    size_mb = _query_db_size_mb(mem_db_path)
    if size_mb is not None:
        result["checked"].append({
            "metric": "db_size_memory",
            "value_mb": round(size_mb, 2),
            "threshold_mb": db_size_warn_mb,
        })
        if size_mb > db_size_warn_mb:
            source = "storage_growth.db_size.memory"
            if _has_pending_alert(inbox_store, source):
                result["alerts_skipped_dedup"].append(source)
            else:
                _push_alert(
                    inbox_store,
                    source,
                    title=f"memory.db 文件大小超阈值 ({size_mb:.1f}MB > {db_size_warn_mb}MB)",
                    description=(
                        f"memory.db 当前 {size_mb:.1f}MB，超阈值 {db_size_warn_mb}MB。"
                        f"建议检查是否需要 VACUUM 数据库或手动清理大表。"
                    ),
                    payload={
                        "severity": "warning",
                        "metric": "db_size_memory",
                        "current_mb": round(size_mb, 2),
                        "threshold_mb": db_size_warn_mb,
                        "db_path": str(mem_db_path),
                        "suggestion": "检查是否需要 VACUUM 数据库或手动清理大表",
                    },
                )
                result["alerts_pushed"].append(source)

    return result
