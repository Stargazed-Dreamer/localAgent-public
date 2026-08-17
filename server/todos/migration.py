"""从记忆系统迁移待办数据到 todos 模块

幂等：检查 schema_info 中 'todos_migrated' 标志，已迁移则跳过。
extra_data 回填：检查 'wip_extra_data_backfilled' 标志，幂等。
"""

import json
import logging
import os
import glob
import shutil
import sqlite3

logger = logging.getLogger("localagent.todos.migration")

# wip_tasks 表的标准字段（不进入 extra_data）
_WIP_STANDARD_FIELDS = {
    "id", "title", "status", "priority", "goal", "progress",
    "tags", "next_steps", "related_skills", "related_files",
    "current_state", "blocked_reason", "related_memory_keys",
    "created_at", "updated_at", "extra_data",
}


def migrate_from_memory(memory_manager) -> dict:
    """从 memory keys (task_reminders, wip_index) + .agents/wip/*.json 迁移到 todos/wip_tasks 表。

    幂等：检查 schema_info 中 'todos_migrated' 标志。

    Returns:
        {"todos_migrated": int, "wip_migrated": int, "skipped": bool}
    """
    store_conn = memory_manager.store.conn

    # 确保 schema_info 表存在（由 memory 创建，这里兜底防止 memory 未初始化）
    store_conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT)"
    )

    # 已迁移则跳过
    cur = store_conn.execute(
        "SELECT value FROM schema_info WHERE key = 'todos_migrated'"
    )
    if cur.fetchone():
        return {"todos_migrated": 0, "wip_migrated": 0, "skipped": True}

    # 确保 todos/wip_tasks 表已创建（TodosStore 是懒加载的，迁移可能在表创建之前运行）
    from server.todos.store import TODOS_SCHEMA_SQL
    store_conn.executescript(TODOS_SCHEMA_SQL)
    store_conn.commit()

    todos_count = 0
    wip_count = 0
    now_sql = "datetime('now', 'localtime')"

    # 1. 迁移 task_reminders → todos 表
    try:
        reminders = memory_manager.get("task_reminders")
        if reminders and isinstance(reminders, dict):
            tasks = reminders.get("tasks", [])
            for task in tasks:
                # 统一 fallback 策略：id → skill → title（与 wip_index/wip 文件保持一致）
                todo_id = task.get("id") or task.get("skill") or task.get("title")
                if not todo_id:
                    logger.warning(f"task_reminders 跳过无 id/skill/title 的任务: {task}")
                    continue
                existing = store_conn.execute(
                    "SELECT id FROM todos WHERE id = ?", (todo_id,)
                ).fetchone()
                if existing:
                    continue
                store_conn.execute(
                    """INSERT INTO todos (id, title, skill, type, status, frequency,
                           last_done_at, next_due_at, condition, condition_status, notes,
                           related_memory_keys, created_at, updated_at)
                       VALUES (?, ?, ?, 'recurring', 'pending', ?, ?, NULL, ?, ?, ?,
                           '["task_reminders"]', """ + now_sql + """, """ + now_sql + """)""",
                    (
                        todo_id,
                        task.get("title", todo_id),
                        task.get("skill"),
                        task.get("frequency"),
                        task.get("last_done_at"),
                        task.get("condition"),
                        "active" if task.get("condition") else None,
                        task.get("notes", ""),
                    ),
                )
                todos_count += 1
    except Exception as e:
        logger.warning(f"迁移 task_reminders 失败: {e}")

    # 2. 迁移 wip_index → wip_tasks 表（仅索引，字段较少）
    try:
        wip_index = memory_manager.get("wip_index")
        if wip_index and isinstance(wip_index, dict):
            tasks = wip_index.get("tasks", [])
            for task in tasks:
                # 统一 fallback 策略：id → title（wip_index 任务通常没有 skill 字段）
                task_id = task.get("id") or task.get("title")
                if not task_id:
                    logger.warning(f"wip_index 跳过无 id/title 的任务: {task}")
                    continue
                existing = store_conn.execute(
                    "SELECT id FROM wip_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if existing:
                    continue
                store_conn.execute(
                    """INSERT INTO wip_tasks (id, title, status, priority, goal, progress,
                           tags, next_steps, related_skills, related_files,
                           current_state, blocked_reason, related_memory_keys,
                           created_at, updated_at)
                       VALUES (?, ?, ?, 'medium', '', 0, '[]', '[]', '[]', '[]', '{}',
                           NULL, '["wip_index"]', """ + now_sql + """, """ + now_sql + """)""",
                    (
                        task_id,
                        task.get("title", task_id),
                        _map_wip_status(task.get("status", "paused")),
                    ),
                )
                wip_count += 1
    except Exception as e:
        logger.warning(f"迁移 wip_index 失败: {e}")

    # 3. 迁移 .agents/wip/*.json → wip_tasks 表（字段更完整）
    try:
        wip_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            ".agents", "wip",
        )
        if os.path.isdir(wip_dir):
            for fpath in glob.glob(os.path.join(wip_dir, "*.json")):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # 统一 fallback 策略：id → title → 文件名（文件名作最后兜底）
                    file_stem = os.path.splitext(os.path.basename(fpath))[0]
                    task_id = data.get("id") or data.get("title") or file_stem
                    if not task_id:
                        logger.warning(f"wip 文件跳过（无 id/title 且文件名空）: {fpath}")
                        continue
                    existing = store_conn.execute(
                        "SELECT id FROM wip_tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if existing:
                        # 已存在（可能从 wip_index 迁移了），用更完整的字段更新
                        _update_wip_from_file(store_conn, task_id, data, now_sql)
                        continue
                    related_keys = data.get("related_memory_keys") or ["wip_index"]
                    store_conn.execute(
                        """INSERT INTO wip_tasks (id, title, status, priority, goal, progress,
                               tags, next_steps, related_skills, related_files,
                               current_state, blocked_reason, related_memory_keys, extra_data,
                               created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               """ + now_sql + """, """ + now_sql + """)""",
                        (
                            task_id,
                            data.get("title", task_id),
                            _map_wip_status(data.get("status", "paused")),
                            data.get("priority", "medium"),
                            data.get("goal", ""),
                            int(data.get("progress", 0)),
                            json.dumps(data.get("tags", []), ensure_ascii=False),
                            json.dumps(data.get("next_steps", []), ensure_ascii=False),
                            json.dumps(data.get("related_skills", []), ensure_ascii=False),
                            json.dumps(data.get("related_files", []), ensure_ascii=False),
                            json.dumps(data.get("current_state", {}), ensure_ascii=False),
                            data.get("blocked_reason"),
                            json.dumps(related_keys, ensure_ascii=False),
                            json.dumps(
                                {k: v for k, v in data.items()
                                 if k not in _WIP_STANDARD_FIELDS},
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    wip_count += 1
                except Exception as e:
                    logger.warning(f"迁移 WIP 文件 {fpath} 失败: {e}")
    except Exception as e:
        logger.warning(f"扫描 .agents/wip/ 失败: {e}")

    # 标记已迁移
    store_conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
        ("todos_migrated", "true"),
    )
    store_conn.commit()

    logger.info(f"待办迁移完成: {todos_count} todos, {wip_count} wip_tasks")
    return {"todos_migrated": todos_count, "wip_migrated": wip_count, "skipped": False}


def _update_wip_from_file(store_conn, task_id: str, data: dict, now_sql: str) -> None:
    """用 .json 文件的完整字段更新已存在的 wip_task（来自 wip_index 的简略记录）"""
    extra = {k: v for k, v in data.items() if k not in _WIP_STANDARD_FIELDS}
    # related_memory_keys：优先用 .json 中显式声明的，否则保留 wip_index 迁移时写死的 ["wip_index"]
    related_keys = data.get("related_memory_keys") or ["wip_index"]
    store_conn.execute(
        """UPDATE wip_tasks SET
               title = ?, status = ?, priority = ?, goal = ?, progress = ?,
               tags = ?, next_steps = ?, related_skills = ?, related_files = ?,
               current_state = ?, blocked_reason = ?, related_memory_keys = ?, extra_data = ?,
               updated_at = """ + now_sql + """
           WHERE id = ?""",
        (
            data.get("title", task_id),
            _map_wip_status(data.get("status", "paused")),
            data.get("priority", "medium"),
            data.get("goal", ""),
            int(data.get("progress", 0)),
            json.dumps(data.get("tags", []), ensure_ascii=False),
            json.dumps(data.get("next_steps", []), ensure_ascii=False),
            json.dumps(data.get("related_skills", []), ensure_ascii=False),
            json.dumps(data.get("related_files", []), ensure_ascii=False),
            json.dumps(data.get("current_state", {}), ensure_ascii=False),
            data.get("blocked_reason"),
            json.dumps(related_keys, ensure_ascii=False),
            json.dumps(extra, ensure_ascii=False),
            task_id,
        ),
    )


def _map_wip_status(status: str) -> str:
    """将旧 wip_index/wip 文件的状态映射到 wip_tasks 表的状态。

    旧状态值（来自 .agents/wip/*.json）：
      - open / in_progress → active
      - blocked → blocked
      - completed / done / closed / resolved / superseded → completed
      - paused → paused
    """
    s = (status or "").lower().strip()
    if s in ("open", "in_progress", "active"):
        return "active"
    if s == "blocked":
        return "blocked"
    if s in ("completed", "done", "closed", "finish", "finished",
             "resolved", "superseded"):
        return "completed"
    return "paused"


def backfill_extra_data(memory_manager) -> dict:
    """回填 wip_tasks.extra_data 字段，从 .agents/wip/*.json 读取富字段。

    幂等：检查 schema_info 中 'wip_extra_data_backfilled' 标志。
    完成后将 .json 文件移动到 temp/wip_migrated_backup/，DB 成为唯一数据源。

    Returns:
        {"backfilled": int, "files_moved": int, "skipped": bool}
    """
    store_conn = memory_manager.store.conn

    # 确保 schema_info 表存在（与 migrate_from_memory 同样的兜底）
    store_conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT)"
    )

    cur = store_conn.execute(
        "SELECT value FROM schema_info WHERE key = 'wip_extra_data_backfilled'"
    )
    if cur.fetchone():
        return {"backfilled": 0, "files_moved": 0, "skipped": True}

    # 确保 extra_data 列存在（幂等）
    try:
        store_conn.execute("ALTER TABLE wip_tasks ADD COLUMN extra_data TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass

    wip_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        ".agents", "wip",
    )

    if not os.path.isdir(wip_dir):
        store_conn.execute(
            "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
            ("wip_extra_data_backfilled", "true"),
        )
        store_conn.commit()
        return {"backfilled": 0, "files_moved": 0, "skipped": False}

    backfill_count = 0
    moved_files = []

    for fpath in glob.glob(os.path.join(wip_dir, "*.json")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            task_id = data.get("id") or os.path.splitext(
                os.path.basename(fpath)
            )[0]
            extra = {k: v for k, v in data.items()
                     if k not in _WIP_STANDARD_FIELDS}
            if extra:
                store_conn.execute(
                    "UPDATE wip_tasks SET extra_data = ? WHERE id = ?",
                    (json.dumps(extra, ensure_ascii=False), task_id),
                )
                backfill_count += 1
            moved_files.append(fpath)
        except Exception as e:
            logger.warning(f"回填 extra_data 失败 {fpath}: {e}")

    if moved_files:
        backup_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "temp", "wip_migrated_backup",
        )
        os.makedirs(backup_dir, exist_ok=True)
        for fpath in moved_files:
            try:
                shutil.move(fpath, os.path.join(backup_dir, os.path.basename(fpath)))
            except Exception as e:
                logger.warning(f"移动文件失败 {fpath}: {e}")

    store_conn.execute(
        "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
        ("wip_extra_data_backfilled", "true"),
    )
    store_conn.commit()

    logger.info(
        f"extra_data 回填完成: {backfill_count} 条, 移动 {len(moved_files)} 个文件到 temp/wip_migrated_backup/"
    )
    return {
        "backfilled": backfill_count,
        "files_moved": len(moved_files),
        "skipped": False,
    }
