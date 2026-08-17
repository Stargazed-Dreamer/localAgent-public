"""存储膨胀告警测试 — check_storage_growth()

验证：
- 阈值以下不推送
- memory facts 超阈值 → 推送 storage_growth.memory_facts
- todos archived 超阈值 → 推送 storage_growth.todos_archived
- db 文件大小超阈值 → 推送 storage_growth.db_size.memory
- 同 source pending 存在时跳过（去重）

测试用临时 memory.db（含 facts + todos 表）和临时 inbox.db，完全隔离。
"""

import sqlite3
from pathlib import Path

import pytest

from server.inbox import reset_store

# ========== 辅助函数 ==========

def _create_test_memory_db(db_path: Path, facts_count: int = 0, archived_count: int = 0) -> None:
    """创建测试用 memory.db，插入指定数量的 facts 和 archived todos。"""
    conn = sqlite3.connect(str(db_path))
    try:
        # 创建 facts 表（最小 schema，足够 COUNT(*) 查询）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL
            )
        """)
        # 创建 todos 表（最小 schema，足够 COUNT(*) WHERE status='archived' 查询）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        # 插入 facts
        for i in range(facts_count):
            conn.execute(
                "INSERT OR IGNORE INTO facts (key, value) VALUES (?, ?)",
                (f"fact_{i}", f"value_{i}"),
            )
        # 插入 archived todos
        for i in range(archived_count):
            conn.execute(
                "INSERT OR IGNORE INTO todos (id, title, status) VALUES (?, ?, 'archived')",
                (f"todo_archived_{i}", f"archived todo {i}"),
            )
        conn.commit()
    finally:
        conn.close()


def _make_cleanup_config(
    facts_warn: int = 5000,
    todos_warn: int = 1000,
    db_size_warn_mb: int = 50,
) -> dict:
    """构造 get_cleanup_config() 的 mock 返回值。"""
    return {
        "browser_stats_details_retention_days": 30,
        "approvals_log_max_bytes": 10485760,
        "approvals_log_backup_count": 5,
        "approval_audit_log_max_bytes": 10485760,
        "approval_audit_log_backup_count": 5,
        "memory_facts_warn_rows": facts_warn,
        "todos_archived_warn_count": todos_warn,
        "db_file_warn_size_mb": db_size_warn_mb,
    }


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    """隔离的存储环境：临时 memory.db + 临时 inbox.db。

    返回 (mem_db_path, inbox_store) 供测试使用。
    """
    mem_db_path = tmp_path / "memory.db"
    inbox_db_path = tmp_path / "inbox.db"

    # mock memory config 指向临时 db
    monkeypatch.setattr("server.memory.config.get_memory_config", lambda: type("Cfg", (), {
        "db_path": str(mem_db_path),
    })())
    # mock cleanup config
    monkeypatch.setattr("server.storage_health.get_cleanup_config", lambda: _make_cleanup_config())
    # mock inbox store 单例指向临时 db
    from server import inbox as inbox_mod
    monkeypatch.setattr(inbox_mod, "get_inbox_config", lambda: {
        "db_path": str(inbox_db_path), "auto_cleanup_days": 30,
    })
    reset_store()
    inbox_store = inbox_mod.get_store()

    yield mem_db_path, inbox_store

    reset_store()


# ========== 测试用例 ==========

class TestCheckStorageGrowth:
    """check_storage_growth() 告警逻辑测试"""

    def test_below_threshold_no_alert(self, isolated_storage):
        """所有指标在阈值以下 → 不推送告警"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        # 插入少量数据（远低于阈值 5000/1000/50MB）
        _create_test_memory_db(mem_db_path, facts_count=10, archived_count=5)

        result = check_storage_growth()

        assert len(result["checked"]) == 3  # facts + todos + db_size
        assert result["alerts_pushed"] == []
        assert result["alerts_skipped_dedup"] == []
        # inbox 无新增
        pending = inbox_store.list(status="pending")
        assert len(pending) == 0

    def test_memory_facts_exceed_threshold(self, monkeypatch, isolated_storage):
        """memory facts 行数超阈值 → 推送 storage_growth.memory_facts"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        # 用极小阈值便于触发
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(facts_warn=5),
        )
        _create_test_memory_db(mem_db_path, facts_count=10, archived_count=0)

        result = check_storage_growth()

        assert "storage_growth.memory_facts" in result["alerts_pushed"]
        # inbox 有一条 pending
        pending = inbox_store.list(status="pending", source="storage_growth.memory_facts")
        assert len(pending) == 1
        assert pending[0]["category"] == "system"
        assert "facts" in pending[0]["title"]
        # payload 含当前值/阈值/建议
        payload = pending[0]["payload"]
        assert payload["metric"] == "memory_facts"
        assert payload["current"] == 10
        assert payload["threshold"] == 5
        assert "suggestion" in payload

    def test_todos_archived_exceed_threshold(self, monkeypatch, isolated_storage):
        """todos archived 记录超阈值 → 推送 storage_growth.todos_archived"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(todos_warn=5),
        )
        _create_test_memory_db(mem_db_path, facts_count=0, archived_count=10)

        result = check_storage_growth()

        assert "storage_growth.todos_archived" in result["alerts_pushed"]
        pending = inbox_store.list(status="pending", source="storage_growth.todos_archived")
        assert len(pending) == 1
        assert "archived" in pending[0]["title"]
        payload = pending[0]["payload"]
        assert payload["current"] == 10
        assert payload["threshold"] == 5

    def test_db_size_exceed_threshold(self, monkeypatch, isolated_storage):
        """memory.db 文件大小超阈值 → 推送 storage_growth.db_size.memory"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        # 创建 db 并写入数据让文件有一定大小
        _create_test_memory_db(mem_db_path, facts_count=5, archived_count=0)
        # 用极小阈值（0.001MB = 1KB）触发文件大小告警
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(db_size_warn_mb=0.001),
        )

        result = check_storage_growth()

        assert "storage_growth.db_size.memory" in result["alerts_pushed"]
        pending = inbox_store.list(status="pending", source="storage_growth.db_size.memory")
        assert len(pending) == 1
        assert "memory.db" in pending[0]["title"]
        payload = pending[0]["payload"]
        assert "current_mb" in payload
        assert payload["threshold_mb"] == 0.001

    def test_dedup_skips_when_pending_exists(self, monkeypatch, isolated_storage):
        """同 source pending 存在时 → 跳过推送（去重）"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(facts_warn=5),
        )
        _create_test_memory_db(mem_db_path, facts_count=10, archived_count=0)

        # 第一次推送
        result1 = check_storage_growth()
        assert "storage_growth.memory_facts" in result1["alerts_pushed"]
        assert len(inbox_store.list(status="pending", source="storage_growth.memory_facts")) == 1

        # 第二次调用：同 source pending 已存在 → 跳过
        result2 = check_storage_growth()
        assert "storage_growth.memory_facts" in result2["alerts_skipped_dedup"]
        assert "storage_growth.memory_facts" not in result2["alerts_pushed"]
        # inbox 仍只有一条（未重复推送）
        assert len(inbox_store.list(status="pending", source="storage_growth.memory_facts")) == 1

    def test_dedup_resolved_allows_repush(self, monkeypatch, isolated_storage):
        """用户 resolve 后 pending 消失 → 下次检查会再推（问题仍在时）"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(facts_warn=5),
        )
        _create_test_memory_db(mem_db_path, facts_count=10, archived_count=0)

        # 第一次推送
        check_storage_growth()
        items = inbox_store.list(status="pending", source="storage_growth.memory_facts")
        assert len(items) == 1

        # 用户 resolve 该条目
        inbox_store.update(items[0]["id"], {"status": "resolved"})

        # 再次检查 → pending 已无 → 重新推送
        result = check_storage_growth()
        assert "storage_growth.memory_facts" in result["alerts_pushed"]
        assert len(inbox_store.list(status="pending", source="storage_growth.memory_facts")) == 1

    def test_multiple_alerts_pushed_together(self, monkeypatch, isolated_storage):
        """多个指标同时超阈值 → 都推送"""
        from server.storage_health import check_storage_growth

        mem_db_path, inbox_store = isolated_storage
        # 三个阈值都设为极小，让所有指标都超阈值
        monkeypatch.setattr(
            "server.storage_health.get_cleanup_config",
            lambda: _make_cleanup_config(
                facts_warn=1, todos_warn=1, db_size_warn_mb=0.001,
            ),
        )
        _create_test_memory_db(mem_db_path, facts_count=5, archived_count=5)

        result = check_storage_growth()

        assert "storage_growth.memory_facts" in result["alerts_pushed"]
        assert "storage_growth.todos_archived" in result["alerts_pushed"]
        assert "storage_growth.db_size.memory" in result["alerts_pushed"]
        # inbox 有 3 条 pending
        all_pending = inbox_store.list(status="pending")
        assert len(all_pending) == 3

    def test_db_not_exists_no_error(self, isolated_storage):
        """memory.db 不存在时不报错，checked 为空"""
        from server.storage_health import check_storage_growth

        mem_db_path, _ = isolated_storage
        # 不创建 memory.db
        assert not mem_db_path.exists()

        result = check_storage_growth()

        # db 不存在 → 三个查询都返回 None → checked 为空
        assert result["checked"] == []
        assert result["alerts_pushed"] == []
