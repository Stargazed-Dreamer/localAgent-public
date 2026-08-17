"""收件箱模块 — Loop 推送的待用户审查条目

SQLite 存储，REST CRUD 端点。Loop Action（如下载文件三分类）把需要用户确认的
条目推入收件箱，用户通过监控面板或 MCP 工具审查、确认、解决。

表结构 inbox_items:
    id, source, category, title, description, payload(JSON),
    status(pending/resolved/ignored), resolution(JSON),
    created_at, updated_at, resolved_at
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from lib.schema import BaseSchema
from server.config import get_inbox_config

logger = logging.getLogger("localagent.inbox")

router = APIRouter(prefix="/inbox", tags=["inbox"])


# ==================== Pydantic 模型 ====================

class InboxItemCreate(BaseSchema):
    source: str = Field(..., description="来源标识，如 loop task_id")
    category: str = Field(..., description="分类，如 move/inspect/unknown")
    title: str = Field(..., description="条目标题")
    description: str = Field("", description="条目描述")
    payload: dict = Field(default_factory=dict, description="详细信息（文件路径、分类建议等）")


class InboxItemUpdate(BaseSchema):
    status: str | None = Field(None, description="新状态：pending/resolved/ignored")
    resolution: dict | None = Field(None, description="解决结果（用户确认后的操作记录）")
    title: str | None = None
    description: str | None = None


class InboxBatchRequest(BaseSchema):
    """批量操作收件箱条目

    action:
        - resolve: 标记为已解决
        - ignore:  标记为已忽略
        - pending: 还原为待处理
        - delete:  删除
    ids: 目标条目 id 列表；与 filter 二选一
    filter: 作用域筛选（仅 source / category / status），与 ids 二选一，用于"全选匹配"
    """
    action: str = Field(..., description="resolve | ignore | pending | delete")
    ids: list[str] | None = Field(None, description="目标条目 id 列表")
    source: str | None = Field(None, description="按来源批量（与 ids 二选一）")
    category: str | None = Field(None, description="按分类批量（与 ids 二选一）")
    status: str | None = Field(None, description="按状态批量（与 ids 二选一）")


# ==================== 存储层 ====================

INBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inbox_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_items(status);
CREATE INDEX IF NOT EXISTS idx_inbox_source ON inbox_items(source);
CREATE INDEX IF NOT EXISTS idx_inbox_created ON inbox_items(created_at);
"""

_INBOX_UPDATE_FIELDS = {"status", "resolution", "title", "description"}
_INBOX_JSON_FIELDS = {"payload", "resolution"}


class InboxStore:
    """收件箱存储层（独立 SQLite，data/inbox.db）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        import os
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")  # 防止并发写时 SQL locked
        self._conn.executescript(INBOX_SCHEMA_SQL)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        return self._conn

    @staticmethod
    def _deserialize_row(row) -> dict:
        d = dict(row) if hasattr(row, "keys") else {}
        if not d:
            columns = [desc[0] for desc in row.cursor.description]
            d = dict(zip(columns, row, strict=False))
        for field in _INBOX_JSON_FIELDS:
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = {}
        return d

    def create(self, item: dict) -> dict:
        item_id = item.get("id") or f"inbox_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """INSERT INTO inbox_items (id, source, category, title, description,
                   payload, status, resolution, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL,
                   datetime('now', 'localtime'), datetime('now', 'localtime'))""",
            (
                item_id,
                item["source"],
                item["category"],
                item["title"],
                item.get("description", ""),
                json.dumps(item.get("payload", {}), ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return self.get(item_id)

    def get(self, item_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        return self._deserialize_row(row) if row else None

    def list(self, status: str | None = None,
             source: str | None = None,
             limit: int = 200) -> list[dict]:
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        cur = self.conn.execute(
            f"SELECT * FROM inbox_items WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [self._deserialize_row(r) for r in cur.fetchall()]

    def update(self, item_id: str, updates: dict) -> dict | None:
        if not updates:
            return self.get(item_id)
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _INBOX_UPDATE_FIELDS:
                continue
            set_clauses.append(f"{field} = ?")
            if field in _INBOX_JSON_FIELDS:
                params.append(json.dumps(value, ensure_ascii=False) if value is not None else None)
            else:
                params.append(value)
        if not set_clauses:
            return self.get(item_id)
        # 若 status 变为 resolved/ignored，记录 resolved_at
        new_status = updates.get("status")
        if new_status in ("resolved", "ignored"):
            set_clauses.append("resolved_at = datetime('now', 'localtime')")
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.append(item_id)
        self.conn.execute(
            f"UPDATE inbox_items SET {', '.join(set_clauses)} WHERE id = ?", params
        )
        self.conn.commit()
        return self.get(item_id)

    def delete(self, item_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM inbox_items WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def cleanup_resolved(self, days: int = 10) -> int:
        """删除已解决/忽略超过 N 天的条目，返回删除数"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            """DELETE FROM inbox_items
               WHERE status IN ('resolved', 'ignored')
                 AND ((resolved_at IS NOT NULL AND resolved_at < ?)
                      OR (resolved_at IS NULL AND updated_at < ?))""",
            (cutoff, cutoff),
        )
        self.conn.commit()
        return cur.rowcount

    def batch_op(self, action: str,
                 ids: list[str] | None = None,
                 source: str | None = None,
                 category: str | None = None,
                 status: str | None = None) -> dict:
        """批量操作收件箱条目

        action: resolve | ignore | pending | delete
        ids:    指定 id 列表（优先）；为空时按 source/category/status 筛选
        返回 {"affected": N, "action": action}
        """
        valid_actions = {"resolve", "ignore", "pending", "delete"}
        if action not in valid_actions:
            raise ValueError(f"未知 action: {action}，允许 {valid_actions}")

        # 构造 WHERE 条件
        conditions: list[str] = []
        params: list = []
        if ids:
            if not isinstance(ids, list) or not ids:
                raise ValueError("ids 不能为空")
            placeholders = ",".join("?" * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(ids)
        else:
            # 至少需要一个筛选条件，避免误全表操作
            if not any([source, category, status]):
                raise ValueError("批量操作必须提供 ids 或至少一个筛选条件")
            if source:
                conditions.append("source = ?")
                params.append(source)
            if category:
                conditions.append("category = ?")
                params.append(category)
            if status:
                conditions.append("status = ?")
                params.append(status)
        where = " AND ".join(conditions) if conditions else "1=1"

        if action == "delete":
            cur = self.conn.execute(f"DELETE FROM inbox_items WHERE {where}", params)
            self.conn.commit()
            return {"affected": cur.rowcount, "action": action}

        # 状态变更：resolve→resolved, ignore→ignored, pending→pending
        new_status = "resolved" if action == "resolve" else (
            "ignored" if action == "ignore" else "pending"
        )
        set_clauses = ["status = ?", "updated_at = datetime('now', 'localtime')"]
        sql_params = [new_status] + params
        # resolve/ignore 记录 resolved_at；pending 还原时清空
        if action in ("resolve", "ignore"):
            set_clauses.append("resolved_at = datetime('now', 'localtime')")
        elif action == "pending":
            set_clauses.append("resolved_at = NULL")
        cur = self.conn.execute(
            f"UPDATE inbox_items SET {', '.join(set_clauses)} WHERE {where}",
            sql_params,
        )
        self.conn.commit()
        return {"affected": cur.rowcount, "action": action}

    def get_stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE status = 'pending'"
        ).fetchone()[0]
        resolved = self.conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE status = 'resolved'"
        ).fetchone()[0]
        ignored = self.conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE status = 'ignored'"
        ).fetchone()[0]
        return {
            "available": True,
            "total": total,
            "pending": pending,
            "resolved": resolved,
            "ignored": ignored,
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ==================== 单例 ====================

_store: InboxStore | None = None
_store_lock = threading.Lock()


def get_store() -> InboxStore:
    global _store
    with _store_lock:
        if _store is None:
            cfg = get_inbox_config()
            # 相对路径相对于项目根目录解析（不依赖 cwd）
            db_path = Path(cfg["db_path"])
            if not db_path.is_absolute():
                db_path = Path(__file__).resolve().parent.parent / db_path
            _store = InboxStore(str(db_path))
            _store.initialize()
        return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


def get_status() -> dict:
    """供 /health 调用"""
    try:
        return get_store().get_stats()
    except Exception as e:
        return {"available": False, "error": str(e)}


# ==================== REST 端点 ====================

@router.get("", operation_id="inbox_list")
async def list_items(
    status: str | None = Query(None, description="按状态过滤：pending/resolved/ignored"),
    source: str | None = Query(None, description="按来源过滤"),
    limit: int = Query(200, ge=1, le=1000),
):
    """列出收件箱条目"""
    items = await asyncio.to_thread(get_store().list, status=status, source=source, limit=limit)
    return {"items": items}


@router.get("/{item_id}", operation_id="inbox_get")
async def get_item(item_id: str):
    """获取单条收件箱条目"""
    item = await asyncio.to_thread(get_store().get, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@router.post("", operation_id="inbox_create")
async def create_item(item: InboxItemCreate):
    """创建收件箱条目（Loop Action 调用）"""
    return get_store().create(item.model_dump())


@router.patch("/{item_id}", operation_id="inbox_update")
async def update_item(item_id: str, updates: InboxItemUpdate):
    """更新收件箱条目（标记 resolved/ignored + resolution）"""
    existing = get_store().get(item_id)
    if not existing:
        raise HTTPException(status_code=404, detail="条目不存在")
    update_data = {k: v for k, v in updates.model_dump(exclude_unset=True).items() if v is not None}
    return get_store().update(item_id, update_data)


@router.delete("/{item_id}", operation_id="inbox_delete")
async def delete_item(item_id: str):
    """删除收件箱条目"""
    if not get_store().delete(item_id):
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"deleted": True, "id": item_id}


@router.post("/cleanup", operation_id="inbox_cleanup")
async def cleanup_items():
    """清理已解决/忽略超过 auto_cleanup_days 天的条目"""
    cfg = get_inbox_config()
    deleted = get_store().cleanup_resolved(days=cfg["auto_cleanup_days"])
    return {"deleted": deleted, "cleanup_days": cfg["auto_cleanup_days"]}


@router.post("/batch", operation_id="inbox_batch")
async def batch_items(req: InboxBatchRequest):
    """批量操作收件箱条目（监控面板多选管理用，不进 MCP 白名单）

    action: resolve | ignore | pending | delete
    ids:    指定 id 列表（优先）；为空时按 source/category/status 筛选
    """
    try:
        result = get_store().batch_op(
            action=req.action,
            ids=req.ids,
            source=req.source,
            category=req.category,
            status=req.status,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
