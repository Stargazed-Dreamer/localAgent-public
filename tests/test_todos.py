"""测试待办模块（todos）

覆盖：CRUD、mark_done 计算 next_due、get_due 逻辑、迁移幂等。
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from server.todos.models import TodoCreate
from server.todos.store import TodosStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TodosStore(path)
    s.initialize()
    yield s
    s.close()
    if os.path.exists(path):
        os.unlink(path)


class TestTodoCRUD:
    def test_create_todo(self, store):
        todo = store.create_todo({
            "title": "测试任务",
            "skill": "test_skill",
            "frequency": "weekly",
        })
        assert todo["title"] == "测试任务"
        assert todo["status"] == "pending"
        assert todo["id"]

    def test_get_todo(self, store):
        created = store.create_todo({"title": "查询测试", "frequency": "daily"})
        fetched = store.get_todo(created["id"])
        assert fetched["title"] == "查询测试"

    def test_list_todos(self, store):
        store.create_todo({"title": "任务1", "frequency": "daily"})
        store.create_todo({"title": "任务2", "frequency": "weekly"})
        todos = store.list_todos()
        assert len(todos) >= 2

    def test_update_todo(self, store):
        created = store.create_todo({"title": "原标题", "frequency": "daily"})
        updated = store.update_todo(created["id"], {"title": "新标题"})
        assert updated["title"] == "新标题"

    def test_delete_todo(self, store):
        created = store.create_todo({"title": "待删除", "frequency": "daily"})
        assert store.delete_todo(created["id"]) is True
        assert store.get_todo(created["id"]) is None


class TestMarkDone:
    def test_mark_done_weekly_computes_next_due_plus_7(self, store):
        created = store.create_todo({"title": "周任务", "frequency": "weekly"})
        done = store.mark_done(created["id"], "2026-07-10")
        assert done["last_done_at"] == "2026-07-10"
        assert done["next_due_at"] == "2026-07-17"

    def test_mark_done_monthly_natural_month(self, store):
        created = store.create_todo({"title": "月任务", "frequency": "monthly"})
        done = store.mark_done(created["id"], "2026-07-10")
        # 自然月：7月10日 → 8月10日（不再是 +30 天的 8月9日）
        assert done["next_due_at"] == "2026-08-10"

    def test_mark_done_monthly_month_end_rollover(self, store):
        """月末兜底：1月31日 → 2月28日（非闰年）"""
        created = store.create_todo({"title": "月末任务", "frequency": "monthly"})
        done = store.mark_done(created["id"], "2026-01-31")
        assert done["next_due_at"] == "2026-02-28"

    def test_mark_done_monthly_december_rollover(self, store):
        """跨年：12月15日 → 次年1月15日"""
        created = store.create_todo({"title": "跨年任务", "frequency": "monthly"})
        done = store.mark_done(created["id"], "2026-12-15")
        assert done["next_due_at"] == "2027-01-15"

    def test_mark_done_daily_plus_1(self, store):
        created = store.create_todo({"title": "日任务", "frequency": "daily"})
        done = store.mark_done(created["id"], "2026-07-10")
        assert done["next_due_at"] == "2026-07-11"

    def test_mark_done_resets_status_to_pending(self, store):
        created = store.create_todo({"title": "任务", "frequency": "weekly"})
        done = store.mark_done(created["id"])
        assert done["status"] == "pending"


class TestGetDue:
    def test_never_done_is_due(self, store):
        store.create_todo({"title": "新任务", "frequency": "weekly"})
        due = store.get_due_todos()
        assert any(t["title"] == "新任务" for t in due)

    def test_past_next_due_is_due(self, store):
        created = store.create_todo({"title": "过期任务", "frequency": "weekly"})
        store.conn.execute(
            "UPDATE todos SET last_done_at = ?, next_due_at = ? WHERE id = ?",
            ("2026-06-01", "2026-06-08", created["id"]),
        )
        store.conn.commit()
        due = store.get_due_todos()
        assert any(t["id"] == created["id"] for t in due)

    def test_future_next_due_not_due(self, store):
        created = store.create_todo({"title": "未到期", "frequency": "weekly"})
        future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        store.conn.execute(
            "UPDATE todos SET last_done_at = ?, next_due_at = ? WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d"), future, created["id"]),
        )
        store.conn.commit()
        due = store.get_due_todos()
        assert not any(t["id"] == created["id"] for t in due)

    def test_conditional_task_has_flag(self, store):
        store.create_todo({
            "title": "条件任务", "frequency": "weekly", "condition": "找工作期间"
        })
        due = store.get_due_todos()
        match = [t for t in due if t["title"] == "条件任务"]
        assert match
        assert match[0].get("condition_check_needed") is True

    def test_conditional_task_defaults_to_active(self, store):
        created = store.create_todo({
            "title": "条件任务", "frequency": "weekly", "condition": "找工作期间"
        })
        assert created["condition_status"] == "active"

    def test_inactive_conditional_task_skipped(self, store):
        created = store.create_todo({
            "title": "已失效条件任务", "frequency": "weekly", "condition": "找工作期间"
        })
        store.update_todo(created["id"], {"condition_status": "inactive"})
        due = store.get_due_todos()
        assert not any(t["id"] == created["id"] for t in due)

    def test_reactivate_conditional_task(self, store):
        created = store.create_todo({
            "title": "重新激活", "frequency": "weekly", "condition": "找工作期间"
        })
        store.update_todo(created["id"], {"condition_status": "inactive"})
        store.update_todo(created["id"], {"condition_status": "active"})
        due = store.get_due_todos()
        assert any(t["id"] == created["id"] for t in due)


class TestWipCRUD:
    def test_create_wip(self, store):
        task = store.create_wip({
            "title": "WIP 任务",
            "goal": "测试目标",
            "next_steps": ["步骤1", "步骤2"],
        })
        assert task["title"] == "WIP 任务"
        assert task["status"] == "active"
        assert task["next_steps"] == ["步骤1", "步骤2"]

    def test_update_wip_progress(self, store):
        created = store.create_wip({"title": "任务", "goal": "g"})
        updated = store.update_wip(created["id"], {"progress": 50})
        assert updated["progress"] == 50

    def test_list_wip_by_status(self, store):
        store.create_wip({"title": "活跃", "goal": "g"})
        t2 = store.create_wip({"title": "暂停", "goal": "g"})
        store.update_wip(t2["id"], {"status": "paused"})
        active = store.list_wip(status="active")
        assert all(t["status"] == "active" for t in active)

    def test_create_wip_with_extra_data(self, store):
        task = store.create_wip({
            "title": "带富字段的任务",
            "goal": "测试 extra_data",
            "extra_data": {"software": ["VS", "IntelliJ"], "scan_source": "temp/scan"},
        })
        assert task["extra_data"]["software"] == ["VS", "IntelliJ"]
        assert task["extra_data"]["scan_source"] == "temp/scan"

    def test_update_wip_extra_data(self, store):
        created = store.create_wip({"title": "任务", "goal": "g"})
        updated = store.update_wip(created["id"], {
            "extra_data": {"custom_field": "value"}
        })
        assert updated["extra_data"]["custom_field"] == "value"

    def test_wip_extra_data_defaults_empty(self, store):
        task = store.create_wip({"title": "无富字段", "goal": "g"})
        assert task["extra_data"] == {}


class TestGetStats:
    def test_stats_returns_counts(self, store):
        store.create_todo({"title": "t1", "frequency": "daily"})
        store.create_wip({"title": "w1", "goal": "g"})
        stats = store.get_stats()
        assert stats["available"] is True
        assert stats["todos_total"] >= 1
        assert stats["wip_total"] >= 1
        assert "todos_due" in stats
        assert "wip_active" in stats


class TestOneshotRejection:
    def test_oneshot_type_rejected(self):
        with pytest.raises(ValidationError):
            TodoCreate(title="一次性任务", type="oneshot")

    def test_recurring_type_accepted(self):
        todo = TodoCreate(title="周期任务", type="recurring")
        assert todo.type == "recurring"

    def test_default_type_is_recurring(self):
        todo = TodoCreate(title="默认类型")
        assert todo.type == "recurring"

    def test_phased_recurring_type_accepted(self):
        todo = TodoCreate(
            title="阶段性任务", type="phased_recurring", frequency="weekly",
            start_date="2026-07-13", end_date="2026-12-31",
        )
        assert todo.type == "phased_recurring"
        assert todo.start_date == "2026-07-13"
        assert todo.end_date == "2026-12-31"

    def test_phased_recurring_missing_fields_rejected(self):
        with pytest.raises(ValidationError):
            TodoCreate(title="缺日期", type="phased_recurring", frequency="weekly")
        with pytest.raises(ValidationError):
            TodoCreate(
                title="缺 frequency", type="phased_recurring",
                start_date="2026-07-13", end_date="2026-12-31",
            )

    def test_triggered_type_accepted(self):
        todo = TodoCreate(
            title="文件触发", type="triggered",
            trigger_condition='{"event":"file_arrived","watch_dir":"E:/test","pattern":"*.csv"}',
        )
        assert todo.type == "triggered"
        assert "file_arrived" in todo.trigger_condition

    def test_triggered_missing_condition_rejected(self):
        with pytest.raises(ValidationError):
            TodoCreate(title="缺条件", type="triggered")


class TestPhasedRecurring:
    """phased_recurring 类型：阶段性周期任务"""

    def test_compute_next_due_in_range(self):
        """区间内递推：正常返回下周日期"""
        nxt = TodosStore._compute_next_due(
            "weekly", "2026-07-10",
            start_date="2026-07-01", end_date="2026-12-31",
            todo_type="phased_recurring",
        )
        assert nxt == "2026-07-17"

    def test_compute_next_due_beyond_end_date_returns_none(self):
        """超出 end_date 返回 None（调用方应置 archived）"""
        nxt = TodosStore._compute_next_due(
            "weekly", "2026-12-28",
            start_date="2026-07-01", end_date="2026-12-31",
            todo_type="phased_recurring",
        )
        assert nxt is None  # 12-28 + 7天 = 2027-01-04 > end_date

    def test_mark_done_archived_when_beyond_end_date(self, store):
        """mark_done 超出 end_date → status=archived"""
        created = store.create_todo({
            "title": "阶段性", "type": "phased_recurring", "frequency": "weekly",
            "start_date": "2026-07-01", "end_date": "2026-12-31",
        })
        done = store.mark_done(created["id"], "2026-12-28")
        assert done["status"] == "archived"
        assert done["next_due_at"] is None

    def test_mark_done_normal_in_range(self, store):
        """区间内 mark_done → 正常 next_due + status=pending"""
        created = store.create_todo({
            "title": "阶段性", "type": "phased_recurring", "frequency": "weekly",
            "start_date": "2026-07-01", "end_date": "2026-12-31",
        })
        done = store.mark_done(created["id"], "2026-07-10")
        assert done["status"] == "pending"
        assert done["next_due_at"] == "2026-07-17"

    def test_get_due_skips_archived(self, store):
        """archived 状态不进 due 列表"""
        created = store.create_todo({
            "title": "已归档", "type": "phased_recurring", "frequency": "weekly",
            "start_date": "2026-07-01", "end_date": "2026-12-31",
        })
        store.mark_done(created["id"], "2026-12-28")
        due = store.get_due_todos()
        assert not any(t["id"] == created["id"] for t in due)

    def test_get_due_skips_before_start_date(self, store):
        """start_date 在未来时不 due"""
        future_start = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        future_end = (datetime.now() + timedelta(days=100)).strftime("%Y-%m-%d")
        store.create_todo({
            "title": "未开始", "type": "phased_recurring", "frequency": "weekly",
            "start_date": future_start, "end_date": future_end,
        })
        due = store.get_due_todos()
        assert not any(t["title"] == "未开始" for t in due)

    def test_get_due_includes_phased_recurring_in_range(self, store):
        """start_date 已到、未归档 → 进 due 列表"""
        store.create_todo({
            "title": "进行中", "type": "phased_recurring", "frequency": "weekly",
            "start_date": "2026-01-01", "end_date": "2026-12-31",
        })
        due = store.get_due_todos()
        assert any(t["title"] == "进行中" for t in due)


class TestTriggered:
    """triggered 类型：触发式重复任务"""

    def test_check_trigger_file_arrived_match(self, store):
        """file_arrived 事件匹配 → triggered=True"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*.csv"}',
        })
        result = store.check_trigger(created["id"], {
            "event": "file_arrived",
            "path": "E:/test/data.csv",
            "filename": "data.csv",
        })
        assert result["triggered"] is True
        # 触发后 next_due_at 应更新为 today
        todo = store.get_todo(created["id"])
        assert todo["next_due_at"] == datetime.now().strftime("%Y-%m-%d")

    def test_check_trigger_pattern_mismatch(self, store):
        """pattern 不匹配 → triggered=False"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*.csv"}',
        })
        result = store.check_trigger(created["id"], {
            "event": "file_arrived",
            "path": "E:/test/data.txt",
            "filename": "data.txt",
        })
        assert result["triggered"] is False

    def test_check_trigger_watch_dir_mismatch(self, store):
        """path 不在 watch_dir 下 → triggered=False"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*"}',
        })
        result = store.check_trigger(created["id"], {
            "event": "file_arrived",
            "path": "E:/other/data.csv",
            "filename": "data.csv",
        })
        assert result["triggered"] is False

    def test_check_trigger_event_type_mismatch(self, store):
        """event 类型不匹配 → triggered=False"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*"}',
        })
        result = store.check_trigger(created["id"], {
            "event": "cron_tick",
            "path": "E:/test/data.csv",
            "filename": "data.csv",
        })
        assert result["triggered"] is False

    def test_check_trigger_not_triggered_type(self, store):
        """对非 triggered 类型调用 → triggered=False"""
        created = store.create_todo({"title": "周期", "type": "recurring", "frequency": "weekly"})
        result = store.check_trigger(created["id"], {"event": "file_arrived", "path": "x", "filename": "y"})
        assert result["triggered"] is False

    def test_check_trigger_no_repeat_before_mark_done(self, store):
        """已触发未 mark_done → 不重复触发"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*.csv"}',
        })
        # 第一次触发
        r1 = store.check_trigger(created["id"], {
            "event": "file_arrived", "path": "E:/test/a.csv", "filename": "a.csv",
        })
        assert r1["triggered"] is True
        # 第二次（未 mark_done）→ 不重复
        r2 = store.check_trigger(created["id"], {
            "event": "file_arrived", "path": "E:/test/b.csv", "filename": "b.csv",
        })
        assert r2["triggered"] is False

    def test_mark_done_resets_triggered(self, store):
        """triggered mark_done 后重置 next_due_at=NULL，可再次触发"""
        created = store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*.csv"}',
        })
        store.check_trigger(created["id"], {
            "event": "file_arrived", "path": "E:/test/a.csv", "filename": "a.csv",
        })
        done = store.mark_done(created["id"])
        assert done["next_due_at"] is None
        # 可再次触发
        r = store.check_trigger(created["id"], {
            "event": "file_arrived", "path": "E:/test/b.csv", "filename": "b.csv",
        })
        assert r["triggered"] is True

    def test_triggered_not_in_due_list(self, store):
        """triggered 类型不进 get_due_todos"""
        store.create_todo({
            "title": "文件触发", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived","watch_dir":"E:/test","pattern":"*"}',
        })
        due = store.get_due_todos()
        assert not any(t["type"] == "triggered" for t in due)


class TestMigration:
    """DB 迁移幂等性"""

    def test_old_db_without_new_columns(self, store):
        """旧 DB（无新列）initialize 后能读写新字段"""
        created = store.create_todo({
            "title": "新字段测试", "type": "phased_recurring", "frequency": "weekly",
            "start_date": "2026-07-01", "end_date": "2026-12-31",
        })
        fetched = store.get_todo(created["id"])
        assert fetched["start_date"] == "2026-07-01"
        assert fetched["end_date"] == "2026-12-31"

    def test_repeated_initialize_idempotent(self, store):
        """重复 initialize 幂等"""
        store.initialize()
        store.initialize()
        created = store.create_todo({
            "title": "幂等测试", "type": "triggered",
            "trigger_condition": '{"event":"file_arrived"}',
        })
        assert created["id"]
