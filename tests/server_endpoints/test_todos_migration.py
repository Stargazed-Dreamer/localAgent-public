"""测试待办模块迁移逻辑（migration.py）

覆盖：三个数据源迁移、幂等、状态映射、_update_wip_from_file 全字段更新、
backfill_extra_data 文件移动。
"""

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from server.todos.migration import (
    _WIP_STANDARD_FIELDS,
    _map_wip_status,
    _update_wip_from_file,
    backfill_extra_data,
    migrate_from_memory,
)


class _FakeMemoryManager:
    """最小化的 memory_manager mock，提供 store.conn 和 get(key) 接口"""

    def __init__(self, db_path: str, memory_data: dict | None = None):
        self.store = SimpleNamespace(conn=sqlite3.connect(db_path))
        self.store.conn.row_factory = sqlite3.Row
        self.store.conn.execute("PRAGMA journal_mode=WAL")
        self._memory_data = memory_data or {}

    def get(self, key: str):
        return self._memory_data.get(key)

    def close(self):
        self.store.conn.close()


@pytest.fixture
def memory_manager(tmp_path):
    db_path = str(tmp_path / "memory.db")
    mgr = _FakeMemoryManager(db_path)
    yield mgr
    mgr.close()


class TestMapWipStatus:
    def test_open_to_active(self):
        assert _map_wip_status("open") == "active"

    def test_in_progress_to_active(self):
        assert _map_wip_status("in_progress") == "active"

    def test_active_passthrough(self):
        assert _map_wip_status("active") == "active"

    def test_blocked_passthrough(self):
        assert _map_wip_status("blocked") == "blocked"

    def test_paused_passthrough(self):
        assert _map_wip_status("paused") == "paused"

    @pytest.mark.parametrize("status", ["completed", "done", "closed", "finish", "finished", "resolved", "superseded"])
    def test_completed_aliases(self, status):
        assert _map_wip_status(status) == "completed"

    def test_unknown_defaults_to_paused(self):
        assert _map_wip_status("unknown_status") == "paused"

    def test_none_defaults_to_paused(self):
        assert _map_wip_status(None) == "paused"

    def test_case_insensitive(self):
        assert _map_wip_status("ACTIVE") == "active"
        assert _map_wip_status("  Blocked  ") == "blocked"


class TestMigrateFromMemory:
    def test_skipped_if_already_migrated(self, memory_manager):
        # 预设 schema_info 标志
        memory_manager.store.conn.execute(
            "CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT)"
        )
        memory_manager.store.conn.execute(
            "INSERT INTO schema_info (key, value) VALUES ('todos_migrated', 'true')"
        )
        memory_manager.store.conn.commit()

        result = migrate_from_memory(memory_manager)
        assert result == {"todos_migrated": 0, "wip_migrated": 0, "skipped": True}

    def test_creates_schema_info_if_missing(self, memory_manager, tmp_path):
        # 不预设 schema_info 表，迁移函数应自行创建
        memory_manager._memory_data = {"task_reminders": {"tasks": []}}
        # 用一个空 .agents/wip 目录避免文件迁移干扰
        wip_dir = tmp_path / ".agents" / "wip"
        wip_dir.mkdir(parents=True)

        # monkeypatch os.path.dirname 以指向 tmp_path
        original_dirname = os.path.dirname

        def fake_dirname(path):
            # 让 migration.py 计算的 wip_dir 落在 tmp_path 下
            if "server" in path and "todos" in path:
                return str(tmp_path / "server" / "todos")
            return original_dirname(path)

        # 简单做法：直接调用迁移，不依赖 .agents/wip 目录扫描
        # （因为 .agents/wip/ 不存在时会跳过第 3 步）
        result = migrate_from_memory(memory_manager)
        # 验证 schema_info 表已创建
        cur = memory_manager.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'todos_migrated'"
        )
        assert cur.fetchone() is not None
        assert result["skipped"] is False

    def test_migrate_task_reminders(self, memory_manager):
        memory_manager._memory_data = {
            "task_reminders": {
                "tasks": [
                    {
                        "id": "accounting",
                        "title": "记账",
                        "skill": "accounting",
                        "frequency": "weekly",
                        "last_done_at": "2026-07-01",
                        "notes": "每周导出账单",
                    },
                    {
                        # 无 id，靠 skill fallback
                        "skill": "community_review",
                        "title": "小鹅通帖子",
                        "frequency": "weekly",
                    },
                ]
            }
        }
        result = migrate_from_memory(memory_manager)
        assert result["todos_migrated"] == 2
        assert result["skipped"] is False

        cur = memory_manager.store.conn.execute("SELECT * FROM todos ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        ids = [r["id"] for r in rows]
        assert "accounting" in ids
        assert "community_review" in ids

    def test_migrate_task_reminders_skips_no_id_skill_title(self, memory_manager):
        memory_manager._memory_data = {
            "task_reminders": {
                "tasks": [
                    {"frequency": "weekly"},  # 无 id/skill/title，应跳过
                    {"title": "仅标题任务"},  # 用 title fallback
                ]
            }
        }
        result = migrate_from_memory(memory_manager)
        assert result["todos_migrated"] == 1

    def test_migrate_wip_index(self, memory_manager):
        memory_manager._memory_data = {
            "wip_index": {
                "tasks": [
                    {"id": "wip_001", "title": "任务1", "status": "paused"},
                    {"id": "wip_002", "title": "任务2", "status": "open"},
                ]
            }
        }
        result = migrate_from_memory(memory_manager)
        assert result["wip_migrated"] == 2

        cur = memory_manager.store.conn.execute("SELECT * FROM wip_tasks ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        assert rows[0]["status"] == "paused"
        assert rows[1]["status"] == "active"  # open → active

    def test_migrate_wip_index_skips_no_id_title(self, memory_manager):
        memory_manager._memory_data = {
            "wip_index": {
                "tasks": [
                    {"status": "paused"},  # 无 id/title，应跳过
                ]
            }
        }
        result = migrate_from_memory(memory_manager)
        assert result["wip_migrated"] == 0

    def test_idempotent(self, memory_manager):
        memory_manager._memory_data = {
            "task_reminders": {"tasks": [{"id": "t1", "title": "T1", "frequency": "weekly"}]},
            "wip_index": {"tasks": [{"id": "w1", "title": "W1", "status": "paused"}]},
        }
        r1 = migrate_from_memory(memory_manager)
        assert r1["todos_migrated"] == 1
        assert r1["wip_migrated"] == 1

        # 第二次应跳过
        r2 = migrate_from_memory(memory_manager)
        assert r2 == {"todos_migrated": 0, "wip_migrated": 0, "skipped": True}

        # 数据不重复
        cur = memory_manager.store.conn.execute("SELECT COUNT(*) FROM todos")
        assert cur.fetchone()[0] == 1
        cur = memory_manager.store.conn.execute("SELECT COUNT(*) FROM wip_tasks")
        assert cur.fetchone()[0] == 1

    def test_json_insert_reads_related_memory_keys(self, memory_manager, tmp_path, monkeypatch):
        """INSERT 路径也应读取 .json 的 related_memory_keys（不仅 UPDATE 路径）"""
        # 创建临时 .agents/wip/ 目录和 .json 文件
        wip_dir = tmp_path / ".agents" / "wip"
        wip_dir.mkdir(parents=True)
        json_file = wip_dir / "feature_task.json"
        json_file.write_text(
            json.dumps({
                "id": "feature_task",
                "title": "功能任务",
                "status": "paused",
                "goal": "测试 INSERT 路径",
                "related_memory_keys": ["custom_key_1", "custom_key_2"],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # monkeypatch os.path.dirname 让 migration.py 计算的 wip_dir 落在 tmp_path 下
        original_dirname = os.path.dirname
        def fake_dirname(path):
            if path.endswith("migration.py"):
                return str(tmp_path / "server" / "todos")
            if path.endswith("todos") or path.endswith("todos" + os.sep):
                return str(tmp_path / "server")
            if path.endswith("server") or path.endswith("server" + os.sep):
                return str(tmp_path)
            return original_dirname(path)
        monkeypatch.setattr(os.path, "dirname", fake_dirname)

        result = migrate_from_memory(memory_manager)
        assert result["wip_migrated"] == 1

        cur = memory_manager.store.conn.execute(
            "SELECT related_memory_keys FROM wip_tasks WHERE id = ?", ("feature_task",)
        )
        row = cur.fetchone()
        assert row is not None
        # 关键断言：INSERT 路径应读取 .json 的 related_memory_keys，不再写死 '[]'
        assert json.loads(row[0]) == ["custom_key_1", "custom_key_2"]

    def test_json_insert_defaults_to_wip_index_when_keys_missing(self, memory_manager, tmp_path, monkeypatch):
        """INSERT 路径：.json 无 related_memory_keys 时默认 ["wip_index"]"""
        wip_dir = tmp_path / ".agents" / "wip"
        wip_dir.mkdir(parents=True)
        json_file = wip_dir / "no_keys_task.json"
        json_file.write_text(
            json.dumps({
                "id": "no_keys_task",
                "title": "无 keys 任务",
                "status": "paused",
                "goal": "测试默认值",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        original_dirname = os.path.dirname
        def fake_dirname(path):
            if path.endswith("migration.py"):
                return str(tmp_path / "server" / "todos")
            if path.endswith("todos") or path.endswith("todos" + os.sep):
                return str(tmp_path / "server")
            if path.endswith("server") or path.endswith("server" + os.sep):
                return str(tmp_path)
            return original_dirname(path)
        monkeypatch.setattr(os.path, "dirname", fake_dirname)

        migrate_from_memory(memory_manager)

        cur = memory_manager.store.conn.execute(
            "SELECT related_memory_keys FROM wip_tasks WHERE id = ?", ("no_keys_task",)
        )
        row = cur.fetchone()
        assert row is not None
        assert json.loads(row[0]) == ["wip_index"]


class TestUpdateWipFromFile:
    def test_updates_all_fields_including_related_memory_keys(self, memory_manager, tmp_path):
        """P0-1 修复验证：_update_wip_from_file 必须更新 related_memory_keys"""
        # 先创建 schema 和一条 wip_index 来源的简略记录
        from server.todos.store import TODOS_SCHEMA_SQL
        memory_manager.store.conn.executescript(TODOS_SCHEMA_SQL)
        memory_manager.store.conn.execute(
            "INSERT INTO wip_tasks (id, title, status, priority, goal, progress, "
            "tags, next_steps, related_skills, related_files, current_state, "
            "blocked_reason, related_memory_keys, extra_data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("wip_001", "原标题", "paused", "medium", "", 0, "[]", "[]", "[]", "[]",
             "{}", None, '["wip_index"]', "{}"),
        )
        memory_manager.store.conn.commit()

        # 用 .json 数据更新
        data = {
            "id": "wip_001",
            "title": "新标题",
            "status": "active",
            "priority": "high",
            "goal": "新目标",
            "progress": 50,
            "tags": ["tag1"],
            "next_steps": ["step1"],
            "related_skills": ["skill1"],
            "related_files": ["file1"],
            "current_state": {"k": "v"},
            "blocked_reason": None,
            "related_memory_keys": ["wip_index", "custom_key"],  # 关键：应被更新
            "custom_field": "custom_value",  # 应进入 extra_data
        }
        _update_wip_from_file(memory_manager.store.conn, "wip_001", data, "datetime('now')")

        cur = memory_manager.store.conn.execute("SELECT * FROM wip_tasks WHERE id = ?", ("wip_001",))
        row = dict(cur.fetchone())
        assert row["title"] == "新标题"
        assert row["status"] == "active"
        assert row["priority"] == "high"
        assert row["progress"] == 50
        # 关键断言：related_memory_keys 必须被更新为 .json 中的值
        assert json.loads(row["related_memory_keys"]) == ["wip_index", "custom_key"]
        # extra_data 应含非标准字段
        extra = json.loads(row["extra_data"])
        assert extra["custom_field"] == "custom_value"

    def test_fallback_to_wip_index_when_related_keys_missing(self, memory_manager):
        """.json 没显式 related_memory_keys 时，保留 ["wip_index"]"""
        from server.todos.store import TODOS_SCHEMA_SQL
        memory_manager.store.conn.executescript(TODOS_SCHEMA_SQL)
        memory_manager.store.conn.execute(
            "INSERT INTO wip_tasks (id, title, status, priority, goal, progress, "
            "tags, next_steps, related_skills, related_files, current_state, "
            "blocked_reason, related_memory_keys, extra_data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("wip_002", "T", "paused", "medium", "", 0, "[]", "[]", "[]", "[]",
             "{}", None, '["wip_index"]', "{}"),
        )
        memory_manager.store.conn.commit()

        data = {"id": "wip_002", "title": "更新标题"}  # 无 related_memory_keys
        _update_wip_from_file(memory_manager.store.conn, "wip_002", data, "datetime('now')")

        cur = memory_manager.store.conn.execute("SELECT related_memory_keys FROM wip_tasks WHERE id = ?", ("wip_002",))
        row = cur.fetchone()
        assert json.loads(row[0]) == ["wip_index"]


class TestBackfillExtraData:
    def test_skipped_if_already_backfilled(self, memory_manager):
        from server.todos.store import TODOS_SCHEMA_SQL
        memory_manager.store.conn.executescript(TODOS_SCHEMA_SQL)
        memory_manager.store.conn.execute(
            "CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT)"
        )
        memory_manager.store.conn.execute(
            "INSERT INTO schema_info (key, value) VALUES ('wip_extra_data_backfilled', 'true')"
        )
        memory_manager.store.conn.commit()

        result = backfill_extra_data(memory_manager)
        assert result == {"backfilled": 0, "files_moved": 0, "skipped": True}

    def test_no_wip_dir_skipped(self, memory_manager, tmp_path, monkeypatch):
        from server.todos.store import TODOS_SCHEMA_SQL
        memory_manager.store.conn.executescript(TODOS_SCHEMA_SQL)

        # monkeypatch 让 migration.py 计算 wip_dir 时落到 tmp_path 下不存在的子目录
        # 注意：必须保存原始 os.path.dirname 引用，避免 lambda 递归
        original_dirname = os.path.dirname

        def fake_dirname(path):
            if path.endswith("migration.py"):
                # migration.py 的 dirname → server/todos/ → 项目根 → 让它落到 tmp_path
                # migration.py 路径形如 .../server/todos/migration.py
                # dirname 两次得到 .../server/todos，再 dirname 得到 .../server，再 dirname 得到项目根
                # 我们让一次 dirname 就返回 tmp_path，后续 os.path.join(tmp_path, ".agents", "wip") 不存在
                return str(tmp_path)
            return original_dirname(path)

        monkeypatch.setattr("server.todos.migration.os.path.dirname", fake_dirname)

        result = backfill_extra_data(memory_manager)
        # .agents/wip/ 不存在时应跳过
        assert result["skipped"] is False
        assert result["backfilled"] == 0
        assert result["files_moved"] == 0


class TestWipStandardFields:
    def test_includes_extra_data(self):
        assert "extra_data" in _WIP_STANDARD_FIELDS

    def test_includes_related_memory_keys(self):
        assert "related_memory_keys" in _WIP_STANDARD_FIELDS

