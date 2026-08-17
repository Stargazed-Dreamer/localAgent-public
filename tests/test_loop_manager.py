"""LoopManager 调度器测试

重点覆盖 Trigger 逻辑，这两个触发器曾出现过 3 次 bug：
1. IntervalTrigger 滞后恢复：nxt 永远 > now，任务永不触发（已修复，用 nxt=now）
2. CronTrigger 整点跳过：candidate +1 分钟导致整点边界被跳过（已修复，不 +1）
3. CronTrigger 搜索范围不足：1440 分钟覆盖测试

以及 LoopManager 的任务加载、状态持久化、失败自动暂停、手动触发。
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from server.activity_tracker.loop_manager import (
    Action,
    CronTrigger,
    IntervalTrigger,
    LoopManager,
    LoopTask,
    NoopAction,
    reset_manager,
)

_TEST_TZ = ZoneInfo("Asia/Shanghai")  # 与 CronTrigger 默认时区一致，不依赖系统时区


# ========== IntervalTrigger ==========

class TestIntervalTrigger:
    def test_basic_interval(self):
        """固定间隔：last_run + interval"""
        t = IntervalTrigger(60)
        assert t.next_run_at(100, 120) == 160

    def test_first_run_no_last_run(self):
        """首次运行 last_run=None → 启动时间 + first_run_delay（默认 0 = 立即触发）

        回归测试：曾用 now+interval，导致主循环每秒重算 next_run_at(None, now)
        时 nxt 随 now 漂移、永远 > now，任务永不触发。改用实例化时间作固定基准。
        """
        t = IntervalTrigger(60)
        now = time.time()
        nxt = t.next_run_at(None, now)
        # first_run_delay=0 → 返回实例化时间（已过去），立即可触发
        assert nxt == t._created_at
        assert nxt <= now

    def test_first_run_no_drift_deadlock(self):
        """回归测试：last_run=None 时主循环每秒重算，nxt 不能随 now 漂移

        生产 bug：collect_windows / screen_vl / memory_maintain 等所有
        IntervalTrigger 任务 run_count=0，因 next_run_at(None, now) 返回
        now+interval，每秒重算后 nxt 永远 = now+interval > now，任务永不触发。
        本测试模拟主循环 70 秒逐秒重算，断言最终能触发（nxt <= now）。
        """
        t = IntervalTrigger(60, first_run_delay=0)
        base = time.time()
        triggered = False
        for i in range(70):
            now = base + i
            nxt = t.next_run_at(None, now)
            if nxt is not None and nxt <= now:
                triggered = True
                break
        assert triggered, "IntervalTrigger 首次触发死锁：last_run=None 时 nxt 永远漂移在未来"

    def test_first_run_with_delay_no_drift(self):
        """first_run_delay>0 时，首次触发固定在 启动时间+delay，不随 now 漂移

        回归测试：旧实现在 first_run_delay>0 分支返回 now+delay，同样漂移死锁
        （screen_summary_trigger delay=60、memory_maintain delay=300 均未触发）。
        新实现用 _created_at+delay 作固定基准：delay 期内不触发，到期后触发。
        """
        t = IntervalTrigger(60, first_run_delay=30)
        created = t._created_at
        # delay 期内：nxt = created+30 > now，不触发
        assert t.next_run_at(None, created + 10) == created + 30
        # delay 到期：nxt = created+30 <= now，触发（固定值，不漂移）
        assert t.next_run_at(None, created + 30) == created + 30
        assert t.next_run_at(None, created + 100) == created + 30

    def test_lag_recovery_immediate(self):
        """滞后恢复：last_run 远在过去，应立即触发（nxt=now）

        回归测试：曾出现 nxt = now + interval 的 bug，导致
        nxt > now 永远成立，任务永远不触发。
        """
        t = IntervalTrigger(60)
        # last_run=100, now=1000 → 严重滞后
        # nxt = 100+60=160, 160 < 1000-60=940 → nxt=now=1000
        assert t.next_run_at(100, 1000) == 1000

    def test_interval_minimum_1(self):
        """间隔最小为 1"""
        assert IntervalTrigger(0).interval == 1
        assert IntervalTrigger(-5).interval == 1

    def test_not_yet_due(self):
        """未到时间：nxt > now"""
        t = IntervalTrigger(60)
        assert t.next_run_at(100, 110) == 160

    def test_just_due(self):
        """刚好到时间：nxt == now"""
        t = IntervalTrigger(60)
        assert t.next_run_at(100, 160) == 160

    def test_repr(self):
        assert "60s" in repr(IntervalTrigger(60))


# ========== CronTrigger ==========

def _local_dt(year, month, day, hour, minute, second=0):
    """构造 Shanghai 时区 datetime 并返回 timestamp（与 CronTrigger 默认时区一致）"""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=_TEST_TZ)
    return dt.timestamp()


def _nxt_local(ts):
    """timestamp → Shanghai 时区 datetime"""
    return datetime.fromtimestamp(ts, tz=_TEST_TZ)


class TestCronTrigger:
    def test_every_hour(self):
        """0 * * * * 每小时整点"""
        t = CronTrigger("0 * * * *")
        now_ts = _local_dt(2026, 7, 12, 14, 30)
        nxt = _nxt_local(t.next_run_at(None, now_ts))
        assert nxt.minute == 0
        assert nxt.hour == 15

    def test_hourly_on_the_hour(self):
        """整点边界：当前正好是整点，应匹配当前小时

        回归测试：曾出现 candidate +1 分钟导致整点被跳过的 bug。
        """
        t = CronTrigger("0 * * * *")
        now_ts = _local_dt(2026, 7, 12, 14, 0, 0)
        nxt = _nxt_local(t.next_run_at(None, now_ts))
        # 应匹配 14:00，而不是跳到 15:00
        assert nxt.hour == 14
        assert nxt.minute == 0

    def test_every_5_minutes(self):
        """*/5 * * * * 每 5 分钟"""
        t = CronTrigger("*/5 * * * *")
        now_ts = _local_dt(2026, 7, 12, 14, 3)
        nxt = _nxt_local(t.next_run_at(None, now_ts))
        assert nxt.minute == 5
        assert nxt.hour == 14

    def test_specific_time_daily(self):
        """30 0 * * * 每天 00:30"""
        t = CronTrigger("30 0 * * *")
        now_ts = _local_dt(2026, 7, 12, 14, 0)
        nxt = _nxt_local(t.next_run_at(None, now_ts))
        assert nxt.hour == 0
        assert nxt.minute == 30
        assert nxt.day == 13  # 明天

    def test_last_run_skips_past(self):
        """last_run 跳过已执行过的时刻"""
        t = CronTrigger("0 * * * *")
        now_ts = _local_dt(2026, 7, 12, 14, 0, 0)
        # last_run = 当前时刻（刚执行过 14:00）
        nxt = _nxt_local(t.next_run_at(now_ts, now_ts))
        assert nxt.hour == 15
        assert nxt.minute == 0

    def test_invalid_expr_segments(self):
        """非 5 段表达式应报错"""
        with pytest.raises(ValueError):
            CronTrigger("0 * * *")
        with pytest.raises(ValueError):
            CronTrigger("0 0")

    def test_search_range_covers_rare_schedule(self):
        """搜索范围 1440 分钟（24h）覆盖所有 minute+hour 组合

        回归测试：搜索范围不足会导致某些 cron 表达式永远找不到匹配。
        """
        t = CronTrigger("0 3 * * *")  # 每天 03:00
        # 从 14:00 搜索，03:00 在 13 小时后（780 分钟），在 1440 范围内
        now_ts = _local_dt(2026, 7, 12, 14, 0)
        nxt = t.next_run_at(None, now_ts)
        assert nxt is not None
        nxt_dt = _nxt_local(nxt)
        assert nxt_dt.hour == 3
        assert nxt_dt.minute == 0

    def test_match_field_star(self):
        """* 匹配任意值"""
        t = CronTrigger("0 * * * *")
        assert t._match_field("*", 5) is True
        assert t._match_field("*", 23) is True

    def test_match_field_step(self):
        """*/N 匹配"""
        t = CronTrigger("*/5 * * * *")
        assert t._match_field("*/5", 0) is True
        assert t._match_field("*/5", 10) is True
        assert t._match_field("*/5", 3) is False

    def test_match_field_exact(self):
        """具体数字匹配"""
        t = CronTrigger("30 0 * * *")
        assert t._match_field("30", 30) is True
        assert t._match_field("30", 15) is False

    def test_repr(self):
        assert "0 * * * *" in repr(CronTrigger("0 * * * *"))


class TestCronTriggerMissedRuns:
    """CronTrigger.missed_runs 补跑逻辑（H3 — 审计报告）

    missed_runs 是 2026-07-20 重写的关键函数，独立于 next_run_at，避免后者
    `base = max(now, last_run)` 丢失 last_run→now 之间的整点。catchup 补跑是
    "触发颗粒度"的关键补救机制，逻辑错误会导致整点永久缺失或重复补跑。
    """

    def test_no_last_run_catchup_returns_recent(self):
        """场景 1：last_run=None + catchup=True → 补跑最近一次错过的触发时刻

        修复 daily_summarize 死锁 bug：daily cron `30 5 * * *` 的 next_run_at(None, now)
        永远返回下一个 05:30（不会回到今天已过的 05:30），后端晚于 05:30 启动时
        daily_summarize 卡在"从未运行"。修复后 missed_runs(None, now) 返回最近一次
        错过的触发时刻（最多 1 个），让 last_run_at 推进，后续 next_run_at 正常计算。
        """
        t = CronTrigger("0 * * * *", catchup=True)
        now_ts = _local_dt(2026, 7, 12, 14, 30)
        missed = t.missed_runs(None, now_ts)
        # 往前找最近的整点 = 14:00（仅 1 个，不会一次补 24 个）
        assert len(missed) == 1
        assert _nxt_local(missed[0]).hour == 14
        assert _nxt_local(missed[0]).minute == 0

    def test_no_last_run_catchup_daily_cron(self):
        """场景 1b：daily cron last_run=None → 补跑最近一次 05:30（bug 核心场景）

        cron="30 5 * * *" 每天 05:30
        后端 14:30 启动（已过今天 05:30），missed_runs 应返回今天 05:30
        DailySummarizeAction 收到 catchup_target_ts=今天 05:30 → daily_date=昨天
        生成昨天的日报，last_run_at 推进到今天 05:30，next_run_at 返回明天 05:30
        """
        t = CronTrigger("30 5 * * *", catchup=True)
        now_ts = _local_dt(2026, 7, 12, 14, 30)  # 后端 14:30 启动
        missed = t.missed_runs(None, now_ts)
        assert len(missed) == 1
        # 应返回今天 05:30（不是昨天 05:30）
        ts_dt = _nxt_local(missed[0])
        assert ts_dt.year == 2026
        assert ts_dt.month == 7
        assert ts_dt.day == 12
        assert ts_dt.hour == 5
        assert ts_dt.minute == 30

    def test_catchup_false_no_catchup(self):
        """场景 4：catchup=False 不补跑（即使 last_run 存在）"""
        t = CronTrigger("0 * * * *", catchup=False)
        last_run = _local_dt(2026, 7, 12, 10, 0)
        now_ts = _local_dt(2026, 7, 12, 14, 30)
        assert t.missed_runs(last_run, now_ts) == []

    def test_multiple_hourly_missed(self):
        """场景 2：last_run→now 之间多整点补跑

        cron="0 * * * *" 每小时整点
        last_run=10:00（已执行），now=14:30
        应补跑 11:00, 12:00, 13:00, 14:00（4 个）
        不含 10:00（已执行），不含 15:00（now 之后）
        """
        t = CronTrigger("0 * * * *", catchup=True)
        last_run = _local_dt(2026, 7, 12, 10, 0)
        now_ts = _local_dt(2026, 7, 12, 14, 30)
        missed = t.missed_runs(last_run, now_ts)
        assert len(missed) == 4
        # 验证每个时间戳是整点，且小时依次为 11/12/13/14
        missed_hours = [_nxt_local(ts).hour for ts in missed]
        assert missed_hours == [11, 12, 13, 14]
        for ts in missed:
            assert _nxt_local(ts).minute == 0

    def test_max_24_cap(self):
        """场景 3：最多返回 24 个（避免启动时补跑过多）

        cron="0 * * * *" 每小时整点
        last_run=2 天前 00:00，now=今天 14:30（间隔约 62 小时，理论 62 个整点）
        应封顶返回 24 个
        """
        t = CronTrigger("0 * * * *", catchup=True)
        last_run = _local_dt(2026, 7, 10, 0, 0)   # 2 天前
        now_ts = _local_dt(2026, 7, 12, 14, 30)   # 现在
        missed = t.missed_runs(last_run, now_ts)
        assert len(missed) == 24
        # 所有时间戳都应在 last_run < ts <= now 范围内
        for ts in missed:
            assert last_run < ts <= now_ts

    def test_every_5min_multiple_missed(self):
        """补充：*/5 分钟 cron 的补跑（验证非整点 cron 也工作）

        cron="*/5 * * * *" 每 5 分钟
        last_run=14:00，now=14:17
        应补跑 14:05, 14:10, 14:15（3 个）
        """
        t = CronTrigger("*/5 * * * *", catchup=True)
        last_run = _local_dt(2026, 7, 12, 14, 0)
        now_ts = _local_dt(2026, 7, 12, 14, 17)
        missed = t.missed_runs(last_run, now_ts)
        assert len(missed) == 3
        missed_mins = [_nxt_local(ts).minute for ts in missed]
        assert missed_mins == [5, 10, 15]


# ========== LoopManager ==========

@pytest.fixture
def isolated_manager(monkeypatch, tmp_path):
    """隔离的 LoopManager：state 文件指向临时目录，inbox mock，不污染真实数据。

    inbox mock 返回通用 MagicMock：默认拦截所有 _push_*_inbox 调用。
    需要断言 inbox 推送内容的测试（如 test_auto_pause_after_threshold）可额外
    monkeypatch get_store 返回自己的 mock_store，会覆盖此处的默认 mock。
    """
    state_file = tmp_path / "tasks.json"
    monkeypatch.setattr("server.activity_tracker.loop_manager.STATE_FILE", state_file)
    monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
    yield
    reset_manager()


class TestLoopManager:
    def test_load_tasks_from_config(self, isolated_manager):
        """从 config 加载任务"""
        config = {
            "enabled": True,
            "tasks": {
                "activity_tracker": {
                    "enabled": True,
                    "collect_windows_interval": 60,
                },
            },
        }
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        assert "activity_tracker.collect_windows" in mgr.tasks
        task = mgr.tasks["activity_tracker.collect_windows"]
        assert task.enabled is True
        assert task.action.action_type == "noop"

    def test_load_tasks_skips_unregistered_action(self, isolated_manager):
        """action 未注册时跳过任务"""
        config = {
            "enabled": True,
            "tasks": {"activity_tracker": {"enabled": True}},
        }
        mgr = LoopManager(config)
        mgr.load_tasks()  # 未注册任何 action
        assert len(mgr.tasks) == 0

    def test_load_tasks_disabled_segment(self, isolated_manager):
        """config 段 enabled=False 时任务 disabled"""
        config = {
            "enabled": True,
            "tasks": {"activity_tracker": {"enabled": False, "collect_windows_interval": 60}},
        }
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        assert mgr.tasks["activity_tracker.collect_windows"].enabled is False

    def test_state_persistence(self, isolated_manager):
        """状态保存和恢复"""
        config = {
            "enabled": True,
            "tasks": {"activity_tracker": {"enabled": True, "collect_windows_interval": 60}},
        }
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        task_id = "activity_tracker.collect_windows"
        mgr.tasks[task_id].last_run_at = 12345.0
        mgr.tasks[task_id].run_count = 3
        mgr._save_state()

        # 新 manager 从同一 state 文件恢复
        mgr2 = LoopManager(config)
        mgr2.register_action("collect_windows", NoopAction())
        mgr2.load_tasks()
        assert mgr2.tasks[task_id].last_run_at == 12345.0
        assert mgr2.tasks[task_id].run_count == 3

    def test_state_persistence_paused(self, isolated_manager):
        """暂停状态恢复"""
        config = {
            "enabled": True,
            "tasks": {"activity_tracker": {"enabled": True, "collect_windows_interval": 60}},
        }
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        task_id = "activity_tracker.collect_windows"
        mgr.pause_task(task_id)
        mgr._save_state()

        mgr2 = LoopManager(config)
        mgr2.register_action("collect_windows", NoopAction())
        mgr2.load_tasks()
        assert mgr2.tasks[task_id].paused is True

    def test_run_task_success(self, isolated_manager):
        """执行任务成功 → fail_count 重置"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        task = mgr.tasks["activity_tracker.collect_windows"]
        task.fail_count = 3
        asyncio.run(mgr._run_task(task))

        assert task.last_run_at is not None
        assert task.run_count == 1
        assert task.fail_count == 0

    def test_run_task_failure(self, isolated_manager):
        """执行任务失败 → fail_count 递增"""
        class FailAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                return {"success": False, "error": "test failure"}

        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", FailAction())
        mgr.load_tasks()

        task = mgr.tasks["activity_tracker.collect_windows"]
        asyncio.run(mgr._run_task(task))

        assert task.fail_count == 1
        assert task.run_count == 1
        assert task.last_result["success"] is False

    def test_run_task_exception(self, isolated_manager):
        """执行任务抛异常 → 计为失败"""
        class CrashAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                raise RuntimeError("boom")

        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", CrashAction())
        mgr.load_tasks()

        task = mgr.tasks["activity_tracker.collect_windows"]
        asyncio.run(mgr._run_task(task))

        assert task.fail_count == 1
        assert "boom" in task.last_result["error"]

    def test_auto_pause_after_threshold(self, isolated_manager, monkeypatch):
        """连续失败 5 次自动暂停 + 推收件箱"""
        class FailAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                return {"success": False, "error": "persistent failure"}

        # mock inbox 推送，避免依赖真实 DB
        mock_store = MagicMock()
        monkeypatch.setattr("server.inbox.get_store", lambda: mock_store)

        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", FailAction())
        mgr.load_tasks()

        task = mgr.tasks["activity_tracker.collect_windows"]
        for _ in range(5):
            asyncio.run(mgr._run_task(task))

        assert task.fail_count == 5
        assert task.paused is True
        # 收件箱被推送
        assert mock_store.create.called

    def test_no_pause_below_threshold(self, isolated_manager, monkeypatch):
        """失败未达阈值不暂停"""
        class FailAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                return {"success": False, "error": "fail"}

        monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", FailAction())
        mgr.load_tasks()

        task = mgr.tasks["activity_tracker.collect_windows"]
        for _ in range(4):
            asyncio.run(mgr._run_task(task))

        assert task.fail_count == 4
        assert task.paused is False

    def test_pause_resume_task(self, isolated_manager):
        """暂停和恢复任务"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        task_id = "activity_tracker.collect_windows"
        assert mgr.pause_task(task_id) is True
        assert mgr.tasks[task_id].paused is True

        assert mgr.resume_task(task_id) is True
        assert mgr.tasks[task_id].paused is False
        assert mgr.tasks[task_id].fail_count == 0

    def test_pause_nonexistent(self, isolated_manager):
        """暂停不存在的任务返回 False"""
        mgr = LoopManager({"enabled": True, "tasks": {}})
        assert mgr.pause_task("nonexistent") is False
        assert mgr.resume_task("nonexistent") is False

    def test_run_task_now(self, isolated_manager):
        """手动触发任务"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        result = asyncio.run(mgr.run_task_now("activity_tracker.collect_windows"))
        assert result["success"] is True

    def test_run_task_now_nonexistent(self, isolated_manager):
        """手动触发不存在的任务"""
        mgr = LoopManager({"enabled": True, "tasks": {}})
        with pytest.raises(KeyError):
            asyncio.run(mgr.run_task_now("nonexistent"))

    def test_get_status(self, isolated_manager):
        """获取状态"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()

        status = mgr.get_status()
        assert status["enabled"] is True
        assert status["task_count"] == 1
        assert status["active_count"] == 1
        assert status["paused_count"] == 0
        assert "noop" in status["registered_actions"]

    def test_start_disabled_global(self, isolated_manager):
        """全局禁用时不启动"""
        mgr = LoopManager({"enabled": False, "tasks": {}})
        asyncio.run(mgr.start())
        assert mgr._running is False

    def test_start_no_tasks(self, isolated_manager):
        """无任务时不启动调度循环"""
        mgr = LoopManager({"enabled": True, "tasks": {}})
        asyncio.run(mgr.start())
        assert mgr._running is False


# ========== LoopTask.status_dict ==========

class TestLoopTaskStatus:
    def test_status_dict_with_no_runs(self, isolated_manager):
        """未运行过的任务 status_dict"""
        task = LoopTask(
            task_id="test.task",
            trigger=IntervalTrigger(60),
            action=NoopAction(),
            config={},
            source_segment="test",
        )
        d = task.status_dict()
        assert d["task_id"] == "test.task"
        assert d["last_run_at"] is None
        assert d["run_count"] == 0
        assert d["fail_count"] == 0
        assert d["paused"] is False

    def test_status_dict_with_runs(self, isolated_manager):
        """有运行记录的 status_dict"""
        task = LoopTask(
            task_id="test.task",
            trigger=IntervalTrigger(60),
            action=NoopAction(),
            config={},
            source_segment="test",
        )
        task.last_run_at = time.time()
        task.run_count = 5
        d = task.status_dict()
        assert d["last_run_at"] is not None
        assert d["run_count"] == 5
        assert d["next_run_at"] is not None


# ========== CronTrigger 时区 ==========

class TestCronTriggerTimezone:
    def test_default_timezone_shanghai(self):
        """默认时区为 Asia/Shanghai"""
        t = CronTrigger("0 * * * *")
        assert str(t.tz) == "Asia/Shanghai"

    def test_custom_timezone_utc(self):
        """自定义时区 UTC"""
        t = CronTrigger("0 * * * *", "UTC")
        assert t.tz == ZoneInfo("UTC")

    def test_timezone_affects_trigger_time(self):
        """不同时区触发时间不同：UTC 14:30 → 0 * * * * 下次触发是 UTC 15:00"""
        t_utc = CronTrigger("0 * * * *", "UTC")
        utc_now = datetime(2026, 7, 12, 14, 30, tzinfo=ZoneInfo("UTC")).timestamp()
        nxt = datetime.fromtimestamp(t_utc.next_run_at(None, utc_now), tz=ZoneInfo("UTC"))
        assert nxt.hour == 15
        assert nxt.minute == 0


# ========== fail_threshold 分段配置 ==========

class TestFailThreshold:
    def test_default_fail_threshold(self, isolated_manager):
        """config 未设 fail_threshold 时用默认值 5"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        assert task.fail_threshold == 5

    def test_custom_fail_threshold(self, isolated_manager):
        """config 段设 fail_threshold=3 时生效"""
        config = {"enabled": True, "tasks": {
            "activity_tracker": {"enabled": True, "fail_threshold": 3}
        }}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        assert task.fail_threshold == 3

    def test_auto_pause_at_custom_threshold(self, isolated_manager, monkeypatch):
        """自定义 fail_threshold=3 时，失败 3 次自动暂停"""
        class FailAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                return {"success": False, "error": "fail"}

        monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
        config = {"enabled": True, "tasks": {
            "activity_tracker": {"enabled": True, "fail_threshold": 3}
        }}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", FailAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        for _ in range(3):
            asyncio.run(mgr._run_task(task))
        assert task.fail_count == 3
        assert task.paused is True
        assert task.paused_reason == "auto_failed"


# ========== task.running 防并发 ==========

class TestTaskRunning:
    def test_running_flag_reset_after_execution(self, isolated_manager):
        """执行后 running 标志重置为 False"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        asyncio.run(mgr._run_task(task))
        assert task.running is False
        assert task.run_count == 1

    def test_skip_when_already_running(self, isolated_manager):
        """running=True 时跳过执行，run_count 不递增"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        task.running = True  # 模拟正在运行
        asyncio.run(mgr._run_task(task))
        assert task.run_count == 0  # 被跳过，未执行


# ========== paused_reason ==========

class TestPausedReason:
    def test_manual_pause_sets_reason(self, isolated_manager):
        """手动暂停设 paused_reason='manual'"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task_id = "activity_tracker.collect_windows"
        mgr.pause_task(task_id)
        assert mgr.tasks[task_id].paused_reason == "manual"

    def test_resume_clears_reason(self, isolated_manager):
        """恢复时清空 paused_reason"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task_id = "activity_tracker.collect_windows"
        mgr.pause_task(task_id)
        mgr.resume_task(task_id)
        assert mgr.tasks[task_id].paused_reason is None

    def test_auto_pause_sets_reason(self, isolated_manager, monkeypatch):
        """自动暂停设 paused_reason='auto_failed'"""
        class FailAction(Action):
            action_type = "collect_windows"
            async def execute(self, context):
                return {"success": False, "error": "fail"}

        monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
        config = {"enabled": True, "tasks": {
            "activity_tracker": {"enabled": True, "fail_threshold": 2}
        }}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", FailAction())
        mgr.load_tasks()
        task = mgr.tasks["activity_tracker.collect_windows"]
        for _ in range(2):
            asyncio.run(mgr._run_task(task))
        assert task.paused is True
        assert task.paused_reason == "auto_failed"

    def test_paused_reason_persisted(self, isolated_manager):
        """paused_reason 持久化到 state 文件并恢复"""
        config = {"enabled": True, "tasks": {"activity_tracker": {"enabled": True}}}
        mgr = LoopManager(config)
        mgr.register_action("collect_windows", NoopAction())
        mgr.load_tasks()
        task_id = "activity_tracker.collect_windows"
        mgr.pause_task(task_id)
        mgr._save_state()

        mgr2 = LoopManager(config)
        mgr2.register_action("collect_windows", NoopAction())
        mgr2.load_tasks()
        assert mgr2.tasks[task_id].paused_reason == "manual"
