"""待办模块存储层 — SQLite CRUD（共享 memory.db）"""

import calendar
import json
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("localagent.todos.store")

TODOS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    skill TEXT,
    type TEXT NOT NULL DEFAULT 'recurring',
    status TEXT NOT NULL DEFAULT 'pending',
    frequency TEXT,
    last_done_at TEXT,
    next_due_at TEXT,
    condition TEXT,
    condition_status TEXT,
    notes TEXT,
    related_memory_keys TEXT DEFAULT '[]',
    start_date TEXT,
    end_date TEXT,
    trigger_condition TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_todos_type ON todos(type);
CREATE INDEX IF NOT EXISTS idx_todos_next_due ON todos(next_due_at);

CREATE TABLE IF NOT EXISTS wip_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'medium',
    goal TEXT DEFAULT '',
    progress INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    next_steps TEXT DEFAULT '[]',
    related_skills TEXT DEFAULT '[]',
    related_files TEXT DEFAULT '[]',
    current_state TEXT DEFAULT '{}',
    blocked_reason TEXT,
    related_memory_keys TEXT DEFAULT '[]',
    extra_data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wip_status ON wip_tasks(status);
CREATE INDEX IF NOT EXISTS idx_wip_priority ON wip_tasks(priority);
"""

# ALTER TABLE 语句用于已有 DB 升级（幂等，列已存在时忽略错误）
_ALTER_STATEMENTS = [
    "ALTER TABLE todos ADD COLUMN condition_status TEXT",
    "ALTER TABLE wip_tasks ADD COLUMN extra_data TEXT DEFAULT '{}'",
    "ALTER TABLE todos ADD COLUMN start_date TEXT",
    "ALTER TABLE todos ADD COLUMN end_date TEXT",
    "ALTER TABLE todos ADD COLUMN trigger_condition TEXT",
]

# todos 表可更新字段（排除 id、created_at、updated_at 等自动字段）
_TODO_UPDATE_FIELDS = {
    "title", "skill", "type", "status", "frequency",
    "last_done_at", "next_due_at", "condition", "condition_status",
    "notes", "related_memory_keys",
    "start_date", "end_date", "trigger_condition",
}
# wip_tasks 表可更新字段
_WIP_UPDATE_FIELDS = {
    "title", "status", "priority", "goal", "progress", "tags",
    "next_steps", "related_skills", "related_files",
    "current_state", "blocked_reason", "related_memory_keys", "extra_data",
}

# JSON 数组/对象字段，读写时需要 json.loads/dumps
_TODO_JSON_FIELDS = {"related_memory_keys"}
_WIP_JSON_FIELDS = {
    "tags", "next_steps", "related_skills", "related_files",
    "current_state", "related_memory_keys", "extra_data",
}


class TodosStore:
    """待办存储层（共享 memory.db）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()

    def initialize(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(TODOS_SCHEMA_SQL)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """幂等升级已有 DB：添加新列（列已存在时忽略错误）。"""
        for stmt in _ALTER_STATEMENTS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # 列已存在

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        return self._conn

    # ─── 内部工具 ─────────────────────────────────────────────

    @staticmethod
    def _serialize_field(field: str, value, is_wip: bool) -> str:
        json_fields = _WIP_JSON_FIELDS if is_wip else _TODO_JSON_FIELDS
        if field in json_fields:
            return json.dumps(value if value is not None else [], ensure_ascii=False)
        return value if value is not None else ""

    @staticmethod
    def _deserialize_row(row, is_wip: bool) -> dict:
        d = dict(row) if hasattr(row, 'keys') else {}
        if not d:
            columns = [desc[0] for desc in row.cursor.description]
            d = dict(zip(columns, row))
        json_fields = _WIP_JSON_FIELDS if is_wip else _TODO_JSON_FIELDS
        for field in json_fields:
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = [] if field != "current_state" else {}
        return d

    # ─── Todos CRUD ──────────────────────────────────────────

    def create_todo(self, todo: dict) -> dict:
        import uuid
        todo_id = todo.get("id") or f"todo_{uuid.uuid4().hex[:8]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        condition = todo.get("condition")
        condition_status = todo.get("condition_status")
        if condition and not condition_status:
            condition_status = "active"
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO todos (id, title, skill, type, status, frequency,
                       last_done_at, next_due_at, condition, condition_status, notes,
                       related_memory_keys, start_date, end_date, trigger_condition,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    todo_id,
                    todo["title"],
                    todo.get("skill"),
                    todo.get("type", "recurring"),
                    todo.get("frequency"),
                    condition,
                    condition_status,
                    todo.get("notes", ""),
                    self._serialize_field("related_memory_keys",
                                          todo.get("related_memory_keys", []), is_wip=False),
                    todo.get("start_date"),
                    todo.get("end_date"),
                    todo.get("trigger_condition"),
                    now, now,
                ),
            )
            self.conn.commit()
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._deserialize_row(row, is_wip=False)

    def list_todos(self, status: Optional[str] = None,
                   todo_type: Optional[str] = None) -> list[dict]:
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if todo_type:
            conditions.append("type = ?")
            params.append(todo_type)
        where = " AND ".join(conditions) if conditions else "1=1"
        cur = self.conn.execute(
            f"SELECT * FROM todos WHERE {where} ORDER BY updated_at DESC", params
        )
        return [self._deserialize_row(r, is_wip=False) for r in cur.fetchall()]

    def update_todo(self, todo_id: str, updates: dict) -> Optional[dict]:
        if not updates:
            return self.get_todo(todo_id)
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _TODO_UPDATE_FIELDS:
                continue
            set_clauses.append(f"{field} = ?")
            params.append(self._serialize_field(field, value, is_wip=False))
        if not set_clauses:
            return self.get_todo(todo_id)
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.append(todo_id)
        with self._write_lock:
            self.conn.execute(
                f"UPDATE todos SET {', '.join(set_clauses)} WHERE id = ?", params
            )
            self.conn.commit()
        return self.get_todo(todo_id)

    def delete_todo(self, todo_id: str) -> bool:
        with self._write_lock:
            cur = self.conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def mark_done(self, todo_id: str, done_date: Optional[str] = None) -> Optional[dict]:
        today = done_date or datetime.now().strftime("%Y-%m-%d")
        todo = self.get_todo(todo_id)
        if not todo:
            return None
        todo_type = todo.get("type", "recurring")
        next_due = None
        new_status = "pending"
        if todo.get("frequency"):
            next_due = self._compute_next_due(
                todo["frequency"], today,
                start_date=todo.get("start_date"),
                end_date=todo.get("end_date"),
                todo_type=todo_type,
            )
            # phased_recurring 超出 end_date → next_due 返回 None → 置 archived
            if todo_type == "phased_recurring" and next_due is None:
                new_status = "archived"
        # triggered 类型 mark_done 时不计算 next_due（等下次事件触发）
        if todo_type == "triggered":
            next_due = None
            new_status = "pending"
        with self._write_lock:
            self.conn.execute(
                """UPDATE todos SET status=?, last_done_at=?, next_due_at=?,
                       updated_at=datetime('now', 'localtime') WHERE id=?""",
                (new_status, today, next_due, todo_id),
            )
            self.conn.commit()
        return self.get_todo(todo_id)

    def get_due_todos(self) -> list[dict]:
        """返回所有到期待办。

        - recurring：按 next_due_at <= today 或从未完成判断
        - phased_recurring：同 recurring，但 start_date > today 时不 due；status=archived 跳过
        - triggered：不进 due 列表（由 loop 轮询触发，触发时单独置 next_due_at=today）
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # 同时查 recurring 和 phased_recurring（triggered 不进 due 列表）
        todos = []
        for t in self.list_todos():
            if t.get("type") not in ("recurring", "phased_recurring"):
                continue
            if t.get("status") == "archived":
                continue
            todos.append(t)
        due = []
        for todo in todos:
            # phased_recurring 在 start_date 之前不算 due
            start_date = todo.get("start_date")
            if start_date and start_date > today:
                continue
            if todo.get("condition"):
                if todo.get("condition_status") == "inactive":
                    continue
                due.append({**todo, "condition_check_needed": True})
                continue
            if not todo.get("last_done_at"):
                due.append(todo)
                continue
            if todo.get("next_due_at") and todo["next_due_at"] <= today:
                due.append(todo)
        return due

    @staticmethod
    def _compute_next_due(frequency: str, done_date: str,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None,
                          todo_type: str = "recurring") -> Optional[str]:
        """计算下次到期日期。

        Args:
            frequency: daily/weekly/monthly
            done_date: 完成日期（ISO YYYY-MM-DD）
            start_date: phased_recurring 起始日期（仅用于语义校验，不影响递推）
            end_date: phased_recurring 结束日期；next_due > end_date 时返回 None
            todo_type: recurring / phased_recurring（triggered 不调此方法）

        Returns:
            ISO 日期字符串，或 None（phased_recurring 超出 end_date，调用方应置 archived）
        """
        dt = datetime.fromisoformat(done_date)
        if frequency == "daily":
            nxt = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        elif frequency == "weekly":
            nxt = (dt + timedelta(days=7)).strftime("%Y-%m-%d")
        elif frequency == "monthly":
            # 自然月：下个月同一天；月末兜底（如 1月31日 → 2月28/29日）
            month = dt.month
            year = dt.year
            if month == 12:
                next_month, next_year = 1, year + 1
            else:
                next_month, next_year = month + 1, year
            max_day = calendar.monthrange(next_year, next_month)[1]
            day = min(dt.day, max_day)
            nxt = datetime(next_year, next_month, day).strftime("%Y-%m-%d")
        elif frequency == "quarterly":
            # 自然季度：3 个月后同一天；月末兜底（如 11月30日 → 次年2月28/29日）
            month = dt.month
            year = dt.year
            new_month = month + 3
            if new_month > 12:
                next_month, next_year = new_month - 12, year + 1
            else:
                next_month, next_year = new_month, year
            max_day = calendar.monthrange(next_year, next_month)[1]
            day = min(dt.day, max_day)
            nxt = datetime(next_year, next_month, day).strftime("%Y-%m-%d")
        else:
            logger.warning(f"未知 frequency={frequency!r}，next_due 保留为 done_date")
            nxt = done_date
        # phased_recurring：超出 end_date → 返回 None（调用方置 archived）
        if todo_type == "phased_recurring" and end_date and nxt > end_date:
            return None
        return nxt

    def check_trigger(self, todo_id: str, event_payload: dict) -> dict:
        """检查 triggered 类型任务的触发条件是否满足。

        首版支持 event=file_arrived：检查 event_payload 中的文件路径是否匹配
        trigger_condition 的 watch_dir + pattern。

        触发时更新 next_due_at=today（作为"已触发待处理"标记，agent 通过
        todos_list(todo_type='triggered') 查询 next_due_at=today 的任务）。
        agent 处理完后调 mark_done 重置 next_due_at=NULL。

        Args:
            todo_id: triggered 类型的 todo id
            event_payload: 事件数据，如
                {"event": "file_arrived", "path": "E:/test/data.csv", "filename": "data.csv"}

        Returns:
            {"triggered": bool, "reason": str, "todo_id": str}
        """
        todo = self.get_todo(todo_id)
        if not todo:
            return {"triggered": False, "reason": f"todo {todo_id} 不存在", "todo_id": todo_id}
        if todo.get("type") != "triggered":
            return {"triggered": False, "reason": f"todo 类型非 triggered", "todo_id": todo_id}
        # 已触发未处理（next_due_at=today 且非 NULL）→ 不重复触发
        today = datetime.now().strftime("%Y-%m-%d")
        if todo.get("next_due_at") and todo["next_due_at"] <= today and todo.get("last_done_at") != today:
            return {"triggered": False, "reason": "已触发待处理，等 agent mark_done", "todo_id": todo_id}

        raw_cond = todo.get("trigger_condition")
        if not raw_cond:
            return {"triggered": False, "reason": "trigger_condition 为空", "todo_id": todo_id}
        try:
            cond = json.loads(raw_cond) if isinstance(raw_cond, str) else raw_cond
        except (json.JSONDecodeError, TypeError):
            return {"triggered": False, "reason": "trigger_condition JSON 解析失败", "todo_id": todo_id}

        event_type = cond.get("event", "")
        payload_event = event_payload.get("event", "")
        if event_type != payload_event:
            return {"triggered": False, "reason": f"event 不匹配 (cond={event_type}, payload={payload_event})", "todo_id": todo_id}

        if event_type == "file_arrived":
            watch_dir = cond.get("watch_dir", "")
            pattern = cond.get("pattern", "*")
            path = event_payload.get("path", "")
            filename = event_payload.get("filename", "")
            # 路径前缀匹配 + 文件名 glob 匹配
            if watch_dir and not path.replace("\\", "/").startswith(watch_dir.replace("\\", "/")):
                return {"triggered": False, "reason": f"path 不在 watch_dir 下", "todo_id": todo_id}
            if pattern and pattern != "*":
                import fnmatch
                if not fnmatch.fnmatch(filename, pattern):
                    return {"triggered": False, "reason": f"filename 不匹配 pattern={pattern}", "todo_id": todo_id}
            reason = f"file_arrived: {filename} 匹配 {pattern} in {watch_dir}"
        else:
            return {"triggered": False, "reason": f"暂不支持 event={event_type}", "todo_id": todo_id}

        # 触发：更新 next_due_at=today（标记已触发待处理）
        with self._write_lock:
            self.conn.execute(
                """UPDATE todos SET next_due_at=?, condition_status='active',
                       updated_at=datetime('now', 'localtime') WHERE id=?""",
                (today, todo_id),
            )
            self.conn.commit()
        return {"triggered": True, "reason": reason, "todo_id": todo_id}

    # ─── WipTasks CRUD ───────────────────────────────────────

    def create_wip(self, task: dict) -> dict:
        import uuid
        task_id = task.get("id") or f"wip_{uuid.uuid4().hex[:8]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO wip_tasks (id, title, status, priority, goal, progress,
                       tags, next_steps, related_skills, related_files,
                       current_state, blocked_reason, related_memory_keys, extra_data,
                       created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    task["title"],
                    task.get("priority", "medium"),
                    task.get("goal", ""),
                    self._serialize_field("tags", task.get("tags", []), is_wip=True),
                    self._serialize_field("next_steps", task.get("next_steps", []), is_wip=True),
                    self._serialize_field("related_skills", task.get("related_skills", []), is_wip=True),
                    self._serialize_field("related_files", task.get("related_files", []), is_wip=True),
                    self._serialize_field("current_state", task.get("current_state", {}), is_wip=True),
                    task.get("blocked_reason"),
                    self._serialize_field("related_memory_keys",
                                          task.get("related_memory_keys", []), is_wip=True),
                    self._serialize_field("extra_data", task.get("extra_data", {}), is_wip=True),
                    now, now,
                ),
            )
            self.conn.commit()
        return self.get_wip(task_id)

    def get_wip(self, task_id: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM wip_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._deserialize_row(row, is_wip=True)

    def list_wip(self, status: Optional[str] = None, summary: bool = False) -> list[dict]:
        """列出 WIP 任务。

        summary=True 时只返回轻量字段（id/title/status/priority/progress/updated_at），
        跳过 goal/next_steps/current_state/related_*/extra_data 等大字段和 JSON 反序列化，
        适用于 agent 会话启动时快速浏览有哪些未完成工作，避免长输出污染上下文。
        需要详情再用 get_wip(task_id) 单独取。
        """
        if summary:
            cols = "id, title, status, priority, progress, updated_at"
            if status:
                cur = self.conn.execute(
                    f"SELECT {cols} FROM wip_tasks WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                )
            else:
                cur = self.conn.execute(
                    f"SELECT {cols} FROM wip_tasks ORDER BY updated_at DESC"
                )
            return [dict(r) for r in cur.fetchall()]
        if status:
            cur = self.conn.execute(
                "SELECT * FROM wip_tasks WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM wip_tasks ORDER BY updated_at DESC"
            )
        return [self._deserialize_row(r, is_wip=True) for r in cur.fetchall()]

    def update_wip(self, task_id: str, updates: dict) -> Optional[dict]:
        if not updates:
            return self.get_wip(task_id)
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _WIP_UPDATE_FIELDS:
                continue
            set_clauses.append(f"{field} = ?")
            params.append(self._serialize_field(field, value, is_wip=True))
        if not set_clauses:
            return self.get_wip(task_id)
        set_clauses.append("updated_at = datetime('now', 'localtime')")
        params.append(task_id)
        with self._write_lock:
            self.conn.execute(
                f"UPDATE wip_tasks SET {', '.join(set_clauses)} WHERE id = ?", params
            )
            self.conn.commit()
        return self.get_wip(task_id)

    def delete_wip(self, task_id: str) -> bool:
        with self._write_lock:
            cur = self.conn.execute("DELETE FROM wip_tasks WHERE id = ?", (task_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # ─── 统计 ─────────────────────────────────────────────────

    def get_stats(self) -> dict:
        todos_total = self.conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
        todos_due = len(self.get_due_todos())
        wip_total = self.conn.execute("SELECT COUNT(*) FROM wip_tasks").fetchone()[0]
        wip_active = self.conn.execute(
            "SELECT COUNT(*) FROM wip_tasks WHERE status = 'active'"
        ).fetchone()[0]
        type_counts = {}
        for row in self.conn.execute(
            "SELECT type, COUNT(*) as cnt FROM todos GROUP BY type"
        ).fetchall():
            type_counts[row["type"]] = row["cnt"]
        archived_count = self.conn.execute(
            "SELECT COUNT(*) FROM todos WHERE status = 'archived'"
        ).fetchone()[0]
        return {
            "available": True,
            "todos_total": todos_total,
            "todos_due": todos_due,
            "todos_by_type": type_counts,
            "todos_archived": archived_count,
            "wip_total": wip_total,
            "wip_active": wip_active,
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
