"""Loop 自动触发系统 — 后端定时任务调度

把"hook"拆成 Trigger（什么时候做）+ Action（做什么）两个概念：
    IntervalTrigger / CronTrigger  →  CollectWindowsAction / ScreenVLAction / ...

LoopManager 在 FastAPI startup 时启动 asyncio 后台协程，按 trigger 调度 action。
任务状态持久化到 data/loops/tasks.json，后端重启后恢复 last_run_at。

已落地的 Action 在 loop_actions.py 中实现，通过 register_loop_actions() 注册到 LoopManager：
    CollectWindowsAction / ScreenVLDescribeAction / HourlySummarizeAction
    <data_drive>:/DownloadscanAction / CleanupActivityAction
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema
from server.config import get_loops_config

logger = logging.getLogger("localagent.loop_manager")

router = APIRouter(prefix="/loop", tags=["loop"])

STATE_FILE = Path(__file__).parent.parent.parent / "data" / "loops" / "tasks.json"
_FAIL_THRESHOLD_DEFAULT = 5  # 默认连续失败 N 次自动暂停，可被 config 段的 fail_threshold 覆盖


# ==================== Trigger ====================

class Trigger:
    """触发器基类：计算下次应运行的时间戳"""

    def next_run_at(self, last_run: float | None, now: float) -> float | None:
        """返回下次应运行的时间戳（epoch 秒），None 表示不再触发"""
        raise NotImplementedError


class IntervalTrigger(Trigger):
    """固定间隔触发（如每 60 秒）。

    first_run_delay: 后端启动后首次触发的延迟（秒）。last_run=None 时使用，
        让任务在启动后 N 秒内执行一次（而非等满整个 interval）。
        默认 0 = 启动后立即首次触发。

    注意：首次触发时刻以实例化时间（≈后端启动时间）为基准，不能用 now。
    next_run_at 会被主循环每秒调用，若用 now+delay 计算，nxt 会随 now 不断
    漂移、永远在未来，导致 last_run=None 的任务永不触发（collect_windows 等
    所有 IntervalTrigger 任务 run_count=0 的死锁 bug）。
    """

    def __init__(self, interval: int, first_run_delay: int = 0):
        self.interval = max(1, int(interval))
        self.first_run_delay = max(0, int(first_run_delay))
        # 记录实例化时刻（≈后端启动），作为首次触发的固定基准，避免随 now 漂移
        self._created_at = time.time()

    def next_run_at(self, last_run: float | None, now: float) -> float | None:
        # 首次触发：last_run=None → 启动时间 + first_run_delay（固定基准，不随 now 漂移）
        if last_run is None:
            return self._created_at + self.first_run_delay
        base = last_run
        nxt = base + self.interval
        # 若已严重滞后（如后端重启后），立即触发一次，不补跑历史
        if nxt < now - self.interval:
            nxt = now
        return nxt

    def __repr__(self):
        if self.first_run_delay > 0:
            return f"IntervalTrigger({self.interval}s, first_delay={self.first_run_delay}s)"
        return f"IntervalTrigger({self.interval}s)"


class CronTrigger(Trigger):
    """简化版 cron 触发器，支持常见格式：
        "0 * * * *"      每小时整点
        "*/N * * * *"    每 N 分钟
        "M H * * *"      每天 H:M
    首版不实现完整 cron 语义，只解析前两个字段（分钟、小时）。
    timezone_name 指定触发时区（如 "Asia/Shanghai"），与 config 的 [loops] timezone 一致。

    catchup=True 时，启动时检测 last_run_at 到 now 之间错过的触发时刻并补跑。
    适用于"过期应补"的任务（如 hourly_summarize）；抓屏类任务不需要补跑，保持 catchup=False。
    """

    def __init__(self, expr: str, timezone_name: str = "Asia/Shanghai", catchup: bool = False):
        self.expr = expr.strip()
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron 表达式必须是 5 段: {expr}")
        self.minute, self.hour, self.dom, self.month, self.dow = parts
        self.tz = ZoneInfo(timezone_name)
        self.catchup = catchup

    def _match_field(self, field_expr: str, value: int) -> bool:
        """匹配单个 cron 字段（支持 * / N / 具体数字）"""
        if field_expr == "*":
            return True
        if field_expr.startswith("*/"):
            try:
                step = int(field_expr[2:])
                return value % step == 0
            except ValueError:
                return False
        try:
            return value == int(field_expr)
        except ValueError:
            return False

    def next_run_at(self, last_run: float | None, now: float) -> float | None:
        # 从 last_run（或 now）向后找第一个匹配 cron 的分钟（按 config 指定时区）
        base = datetime.fromtimestamp(now, tz=self.tz)
        if last_run:
            base = max(base, datetime.fromtimestamp(last_run, tz=self.tz))
        # 从 base 当前分钟开始搜索（不 +1 分钟，避免跳过整点边界）
        candidate = base.replace(second=0, microsecond=0)
        for _ in range(1440):  # 搜索 24 小时，覆盖所有 minute+hour 组合
            if self._match_field(self.minute, candidate.minute) and \
               self._match_field(self.hour, candidate.hour):
                # 跳过已执行过的时刻（避免重复触发）
                if last_run and candidate.timestamp() <= last_run:
                    candidate += timedelta(minutes=1)
                    continue
                return candidate.timestamp()
            candidate += timedelta(minutes=1)
        return None

    def missed_runs(self, last_run: float | None, now: float) -> list[float]:
        """返回 last_run 到 now 之间错过的触发时刻列表（catchup=True 时使用）。

        不包含 last_run 本身（已执行过），不包含 now 之后的时刻。
        最多返回 24 个（避免启动时补跑过多）。

        last_run=None（从未运行过）时，补跑**最近一次**错过的触发时刻（最多 1 个）。
        修复 daily_summarize 死锁 bug：daily cron `30 5 * * *` 的 next_run_at(None, now)
        永远返回下一个 05:30（不会回到今天已过的 05:30），导致后端晚于 05:30 启动时
        daily_summarize 永远卡在"从未运行"状态。此处补跑最近一次错过的触发时刻，让
        last_run_at 推进到该时刻，后续 next_run_at 才能正确计算下次触发。
        hourly_summarize 同理：首次启动补跑最近一个整点（最多 1 个，不会一次补 24 个）。

        注意：不复用 next_run_at()，因为后者内部 `base = max(now, last_run)` 会让
        last_run < now 时直接从 now 开始找，错过 last_run→now 之间的整点。
        此处独立遍历分钟，确保能找到 last_run→now 之间所有匹配 cron 的时刻。
        """
        if not self.catchup:
            return []
        if last_run is None:
            # 从未运行过：从 now 当前分钟往前找最近的匹配 cron 的时刻（最多 1 个）
            # 不会和 next_run_at 的当前分钟触发重复：catchup 先跑，last_run_at 推进后，
            # _run_loop 的 next_run_at 会跳过 <= last_run 的时刻
            now_dt = datetime.fromtimestamp(now, tz=self.tz)
            cursor_dt = now_dt.replace(second=0, microsecond=0)
            for _ in range(24 * 60):
                if self._match_field(self.minute, cursor_dt.minute) and \
                   self._match_field(self.hour, cursor_dt.hour):
                    return [cursor_dt.timestamp()]
                cursor_dt -= timedelta(minutes=1)
            return []
        missed: list[float] = []
        # 从 last_run 下一分钟开始搜索（不含 last_run 本身）
        cursor_dt = datetime.fromtimestamp(last_run, tz=self.tz).replace(
            second=0, microsecond=0
        ) + timedelta(minutes=1)
        end_dt = datetime.fromtimestamp(now, tz=self.tz)
        count = 0
        # 遍历 last_run→now 之间的每一分钟，收集匹配 cron 的时刻
        # 最多扫描 24*60 分钟（24 小时），避免极端情况无限循环
        for _ in range(24 * 60):
            if cursor_dt > end_dt or count >= 24:
                break
            if self._match_field(self.minute, cursor_dt.minute) and \
               self._match_field(self.hour, cursor_dt.hour):
                missed.append(cursor_dt.timestamp())
                count += 1
            cursor_dt += timedelta(minutes=1)
        return missed

    def __repr__(self):
        suffix = ", catchup=True" if self.catchup else ""
        return f"CronTrigger('{self.expr}'{suffix})"


# ==================== Action ====================

class Action:
    """动作基类。子类实现 execute()。"""

    action_type: str = "base"

    async def execute(self, context: dict) -> dict:
        """执行动作。返回 {"success": bool, "error": str?, ...}。

        若内部调用同步阻塞函数（如 requests.get），应用 asyncio.to_thread 包装。
        context 包含: task_id, config（该任务的 config 段字典）, store（inbox store 引用）
        """
        raise NotImplementedError


class NoopAction(Action):
    """空动作，用于测试调度器"""

    action_type = "noop"

    async def execute(self, context: dict) -> dict:
        return {"success": True, "msg": "noop", "ts": time.time()}


# ==================== 任务定义 ====================

@dataclass
class LoopTask:
    task_id: str
    trigger: Trigger
    action: Action
    config: dict
    source_segment: str  # 来源 config 段名（如 activity_tracker）
    enabled: bool = True
    last_run_at: float | None = None
    last_result: dict | None = None
    fail_count: int = 0
    paused: bool = False  # 连续失败自动暂停或手动暂停
    paused_reason: str | None = None  # "auto_failed" | "manual" | None
    run_count: int = 0
    fail_threshold: int = _FAIL_THRESHOLD_DEFAULT  # 可被 config 段 fail_threshold 覆盖
    running: bool = False  # 运行时标志，防止并发重复执行（不持久化）
    last_inbox_error: str | None = None  # 上次推送到 inbox 的 error 文本，用于失败去重

    def status_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "source_segment": self.source_segment,
            "enabled": self.enabled,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "running": self.running,
            "trigger": repr(self.trigger),
            "action_type": self.action.action_type,
            "last_run_at": datetime.fromtimestamp(self.last_run_at).strftime(
                "%Y-%m-%d %H:%M:%S") if self.last_run_at else None,
            "last_result": self.last_result,
            "fail_count": self.fail_count,
            "run_count": self.run_count,
            "fail_threshold": self.fail_threshold,
            "next_run_at": (
                lambda nxt: datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M:%S") if nxt else None
            )(self.trigger.next_run_at(self.last_run_at, time.time())),
        }


# config 段 → 子任务构建规则
# 每个规则: sub_id, action_type, trigger_factory(cfg_dict) -> Trigger
TASK_BUILDERS: dict[str, list[dict]] = {
    "activity_tracker": [
        {"sub_id": "collect_windows", "action": "collect_windows",
         "trigger": lambda c, tz: IntervalTrigger(c.get("collect_windows_interval", 60))},
        {"sub_id": "screen_vl", "action": "screen_vl",
         "trigger": lambda c, tz: IntervalTrigger(c.get("screen_vl_interval", 300))},
        {"sub_id": "hourly_summarize", "action": "hourly_summarize",
         "trigger": lambda c, tz: CronTrigger(c.get("hourly_summarize_cron", "0 * * * *"), tz, catchup=True)},
        {"sub_id": "daily_summarize", "action": "daily_summarize",
         "trigger": lambda c, tz: CronTrigger(c.get("daily_summarize_cron", "30 5 * * *"), tz, catchup=True)},
        {"sub_id": "cleanup", "action": "cleanup_activity",
         "trigger": lambda c, tz: CronTrigger(c.get("cleanup_cron", "30 0 * * *"), tz)},
        # 今日总结提醒不在调度表自动注入（ScreenSummaryTriggerAction 类保留在
        # loop_actions.py，未注册调度，需要时再挂回此条目）。
    ],
    "download_watcher": [
        {"sub_id": "scan", "action": "download_scan",
         "trigger": lambda c, tz: IntervalTrigger(c.get("scan_interval", 300))},
    ],
    "todos_trigger": [
        {"sub_id": "trigger_check", "action": "todos_trigger_check",
         "trigger": lambda c, tz: IntervalTrigger(c.get("check_interval", 300))},
    ],
    "memory": [
        {"sub_id": "maintain", "action": "memory_maintain",
         "trigger": lambda c, tz: IntervalTrigger(
             c.get("maintain_interval", 21600),
             first_run_delay=c.get("maintain_first_run_delay", 300))},
    ],
    "smart_limit": [
        # 智能配额上限调整建议器：每天运行一次（默认凌晨 4 点，避开整点 hourly_summarize）
        # 只读模式：分析 429 日志 → 推 inbox 建议，不写 config
        {"sub_id": "check", "action": "smart_limit_check",
         "trigger": lambda c, tz: CronTrigger(c.get("check_cron", "0 4 * * *"), tz)},
    ],
    "key_health": [
        # key 级健康检查：每 12 小时检查 should_run_health_check()，true 则跑 check_health()
        # 解决健康检查纯按需触发（/health 端点被调时才检查）的问题
        {"sub_id": "check", "action": "key_health_check",
         "trigger": lambda c, tz: IntervalTrigger(
             c.get("check_interval", 43200),
             first_run_delay=c.get("first_run_delay", 300))},
    ],
    "secret_backup": [
        # 密钥备份：每 5 天把 keys.json / secrets.toml / config.toml 备份到
        # 项目内 backups/secrets/ + 项目外目录双位置（防 agent 误删 gitignored 文件后无法恢复）
        # 路径配置在 [secret_backup] 段，调度配置在此段
        {"sub_id": "run", "action": "secret_backup",
         "trigger": lambda c, tz: IntervalTrigger(
             c.get("interval_seconds", 432000),  # 默认 5 天
             first_run_delay=c.get("first_run_delay", 300))},
    ],
    "workspace_backup": [
        # workspace 关键数据备份：按各组件 manifest [backup] 段声明备份
        # 路径配置在 [workspace_backup] 段，备份目标声明在 workspace/<comp>/manifest.toml
        # 与 secret_backup 对称设计但独立目录和保留策略，避免相互挤占（ADR-0029）
        # first_run_delay 默认 600s（比 secret_backup 晚 5 分钟避免并发 IO 压力）
        {"sub_id": "run", "action": "workspace_backup",
         "trigger": lambda c, tz: IntervalTrigger(
             c.get("interval_seconds", 432000),  # 默认 5 天
             first_run_delay=c.get("first_run_delay", 600))},
    ],
    "headless_session": [
        # headless-agent-session Ticket 04：后端自主启动 agent 会话
        # 配置段 [loops.headless_session] 含 trigger_text / cron / one_shot / 资源限制等
        # 默认每天 9 点跑一次（用户在 config 覆盖 cron）
        # 一次性任务设 one_shot=true，执行后自动 paused
        {"sub_id": "run", "action": "headless_session",
         "trigger": lambda c, tz: CronTrigger(c.get("cron", "0 9 * * *"), tz)},
    ],
}


def _load_optional_loop_defs() -> dict[str, list[dict]]:
    """加载 workspace 下所有可选组件的 loop 任务定义。

    双轨策略：
    1. 优先读 manifest 的 loop_tasks 入口字段（manifest 声明的组件）
    2. 回退旧机制扫 workspace/*/loop_actions.py 读 LOOP_TASK_DEFS（向后兼容）

    manifest 优先：若组件已通过 manifest 加载，旧机制跳过该组件避免重复。
    返回的字典结构与 TASK_BUILDERS 相同：{segment_name: [rule, ...]}。

    主代码库不直接 import 任何 workspace 组件，删除 workspace/<component>/
    后 manifest 消失，loop 任务自动注销。
    """
    import importlib

    from server.component_manifest import load_manifests

    result: dict[str, list[dict]] = {}
    loaded_components: set[str] = set()

    # 1. 优先从 manifest 加载（新机制）
    try:
        manifests = load_manifests()
    except Exception:
        manifests = {}
    for name, m in manifests.items():
        if not m.enabled or m.loop_tasks is None:
            continue
        module_name = f"workspace.{name}.{m.loop_tasks.file[:-3]}"
        try:
            mod = importlib.import_module(module_name)
        except ImportError as e:
            logger.warning("manifest 组件 %s loop_tasks 模块加载失败 (%s): %s", name, module_name, e)
            continue
        defs = getattr(mod, m.loop_tasks.entries_var, None)
        if isinstance(defs, dict) and defs:
            result.update(defs)
            loaded_components.add(name)

    # 2. 回退旧机制：扫 workspace/*/loop_actions.py，跳过已通过 manifest 加载的组件
    # 使用 component_manifest._WORKSPACE_DIR 而非 workspace.__path__，
    # 让 monkeypatch _WORKSPACE_DIR 的测试能同时影响 manifest 加载和 fallback 扫描。
    from server.component_manifest import _WORKSPACE_DIR as _ws_dir
    if not _ws_dir.exists():
        return result
    for child in sorted(_ws_dir.iterdir()):
        if not child.is_dir() or child.name.startswith('_') or child.name.startswith('.'):
            continue
        if child.name in loaded_components:
            continue
        try:
            mod = importlib.import_module(f'workspace.{child.name}.loop_actions')
        except ImportError:
            continue
        defs = getattr(mod, 'LOOP_TASK_DEFS', None)
        if isinstance(defs, dict) and defs:
            result.update(defs)

    return result


# 合并可选组件的 loop 任务定义（在模块加载时执行一次）
TASK_BUILDERS.update(_load_optional_loop_defs())


# ==================== LoopManager ====================

class LoopManager:
    """Loop 调度器：加载任务、调度执行、状态持久化"""

    def __init__(self, config: dict):
        self.config = config  # get_loops_config() 返回
        self.tasks: dict[str, LoopTask] = {}
        self._action_registry: dict[str, Action] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_save = 0.0
        self._register_builtin_actions()

    def _register_builtin_actions(self):
        self._action_registry["noop"] = NoopAction()

    def register_action(self, action_type: str, action: Action):
        """注册动作（Phase 2/3 的 loop_actions.py 调用）"""
        self._action_registry[action_type] = action
        logger.info(f"Loop action 已注册: {action_type}")

    def load_tasks(self):
        """从 config [loops.*] 段加载任务，实例化 trigger + action"""
        tasks_cfg = self.config.get("tasks", {})
        loaded = 0
        skipped = 0
        for segment, cfg in tasks_cfg.items():
            if not isinstance(cfg, dict):
                continue
            segment_enabled = cfg.get("enabled", True)
            builders = TASK_BUILDERS.get(segment, [])
            if not builders:
                logger.warning(f"Loop: config 段 '{segment}' 无对应 builder，跳过")
                continue
            for rule in builders:
                task_id = f"{segment}.{rule['sub_id']}"
                action_type = rule["action"]
                action = self._action_registry.get(action_type)
                if action is None:
                    logger.info(f"Loop 任务 {task_id} 跳过（action '{action_type}' 未注册）")
                    skipped += 1
                    continue
                try:
                    tz_name = self.config.get("timezone", "Asia/Shanghai")
                    trigger = rule["trigger"](cfg, tz_name)
                except Exception as e:
                    logger.warning(f"Loop 任务 {task_id} trigger 构建失败: {e}")
                    skipped += 1
                    continue
                task = LoopTask(
                    task_id=task_id,
                    trigger=trigger,
                    action=action,
                    config=cfg,
                    source_segment=segment,
                    enabled=segment_enabled,
                    fail_threshold=cfg.get("fail_threshold", _FAIL_THRESHOLD_DEFAULT),
                )
                self.tasks[task_id] = task
                loaded += 1
        self._load_state()
        logger.info(f"Loop 任务加载完成: {loaded} 个启用, {skipped} 个跳过（action 未实现）")

    async def start(self):
        """启动后台调度协程"""
        if self._running:
            return
        if not self.config.get("enabled", True):
            logger.info("Loop 系统全局禁用（loops.enabled=false）")
            return
        self.load_tasks()
        if not self.tasks:
            logger.info("Loop 无可运行任务（可能是 action 尚未实现）")
            return
        self._running = True
        # catchup: 补跑 CronTrigger catchup=True 任务的错过时刻（如后端重启期间错过的整点总结）
        await self._catchup_missed_runs()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Loop 调度器已启动，{len(self.tasks)} 个任务")

    async def stop(self):
        """停止调度"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._save_state()
        logger.info("Loop 调度器已停止")

    async def _catchup_missed_runs(self):
        """启动时补跑 CronTrigger catchup=True 任务的错过时刻。

        遍历所有启用且未暂停的任务，对 catchup=True 的 CronTrigger 任务，
        调用 missed_runs() 计算 last_run_at 到 now 之间错过的触发时刻，
        逐个调用 _run_task(catchup_target_ts=...) 补跑。
        补跑完成后保存状态，让 last_run_at 推进到最新补跑时刻。
        """
        now = time.time()
        any_catchup = False
        for task in list(self.tasks.values()):
            if not task.enabled or task.paused:
                continue
            if await self._catchup_task_missed_runs(task, now):
                any_catchup = True
        if any_catchup:
            self._save_state()

    async def _catchup_task_missed_runs(self, task: LoopTask, now: float | None = None) -> bool:
        """为单个任务补跑 catchup=True 的 CronTrigger 错过时刻。

        用于 loop_resume_task 后的定向补跑（仅对该任务，不动其他任务状态）。
        返回 True 表示发生了补跑。补跑过程中 last_run_at 推进到最新补跑时刻。

        若任务 action 在补跑中再次失败到 fail_threshold，会触发 auto_pause，
        此后剩余 missed 时刻不再补跑（避免无意义重试）。
        """
        if now is None:
            now = time.time()
        if not task.enabled or task.paused:
            return False
        trigger = task.trigger
        if not isinstance(trigger, CronTrigger) or not trigger.catchup:
            return False
        try:
            missed = trigger.missed_runs(task.last_run_at, now)
        except Exception as e:
            logger.warning(f"Loop catchup: {task.task_id} 计算错过时刻失败: {e}")
            return False
        if not missed:
            return False
        logger.info(
            f"Loop catchup: {task.task_id} 检测到 {len(missed)} 个错过时刻，开始补跑"
        )
        for target_ts in missed:
            # 补跑过程中若任务被 auto_pause（连续失败），停止后续补跑
            if task.paused:
                logger.warning(
                    f"Loop catchup: {task.task_id} 补跑中触发 auto_pause，"
                    f"剩余 {len(missed) - missed.index(target_ts)} 个时刻不再补跑"
                )
                break
            await self._run_task(task, catchup_target_ts=target_ts)
        logger.info(f"Loop catchup: {task.task_id} 补跑完成")
        self._save_state()
        return True

    async def _run_loop(self):
        """主调度循环，每秒检查 trigger"""
        while self._running:
            try:
                now = time.time()
                for task in list(self.tasks.values()):
                    if not task.enabled or task.paused:
                        continue
                    nxt = task.trigger.next_run_at(task.last_run_at, now)
                    if nxt and nxt <= now:
                        await self._run_task(task)
                # 每 30 秒保存一次状态
                if now - self._last_save > 30:
                    self._save_state()
                    self._last_save = now
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Loop 调度循环异常: {e}")
                await asyncio.sleep(30)  # 异常后退避，避免每秒刷爆日志

    async def _run_task(self, task: LoopTask, catchup_target_ts: float | None = None,
                        manual: bool = False):
        """执行单个任务，异常捕获 + 失败计数 + 自动暂停

        catchup_target_ts: 补跑模式下的目标触发时刻（epoch 秒）。None 表示正常触发。
            补跑模式下 context 会传入 catchup_target_ts，action 可据此处理历史时刻；
            last_run_at 取 max(target_ts, current)，避免补跑历史时刻时把进度回退
            （如正常 cron 已跑到 01:00，补跑 ts=23:00 不应让 last_run_at 倒退到 23:00）。
        manual: 手动触发（run_task_now / REST endpoint）标志。
            - running 冲突时不再静默 return，而是设置 task_busy 结果让调用方感知
              （旧实现只打日志，endpoint 返回旧 last_result 造成"已触发"假象）。
            - 不推进 last_run_at（P2-4）：手动补跑不应顺延 IntervalTrigger 的调度
              周期。last_result/run_count 照常更新。
        """
        if task.running:
            if manual:
                logger.warning(
                    f"Loop 任务 {task.task_id} 正在运行，手动触发被拒（task_busy）"
                )
                task.last_result = {
                    "success": False,
                    "error": "task_busy",
                    "message": "任务正在执行中（调度器触发），请稍后再试",
                }
                # busy 不计失败：不是 action 执行失败，是并发保护
                self._save_state()
                return
            logger.warning(f"Loop 任务 {task.task_id} 已在运行，跳过本次触发")
            return
        task.running = True
        if catchup_target_ts is not None:
            label = f"{task.task_id} (catchup={datetime.fromtimestamp(catchup_target_ts).strftime('%Y-%m-%d %H:%M')})"
        else:
            label = task.task_id
        logger.info(f"Loop 执行任务: {label} (action={task.action.action_type})")
        try:
            context = {
                "task_id": task.task_id,
                "config": task.config,
                "source_segment": task.source_segment,
            }
            if catchup_target_ts is not None:
                context["catchup_target_ts"] = catchup_target_ts
            result = await task.action.execute(context)
            # last_run_at 更新策略：
            # - catchup 模式：取 max(target_ts, current)，避免补跑历史时刻时把进度回退
            #   （如正常 cron 已跑到 01:00，补跑 ts=23:00 不应让 last_run_at 倒退到 23:00）
            # - 手动触发：不更新，避免顺延 IntervalTrigger 的下次调度（P2-4）
            # - 正常调度：更新为 now
            if catchup_target_ts is not None:
                task.last_run_at = max(catchup_target_ts, task.last_run_at or 0)
            elif manual:
                pass  # 手动触发不推进调度进度
            else:
                task.last_run_at = time.time()
            task.last_result = result
            task.run_count += 1
            # 三态判断：success / skipped / failed
            # skipped = 主动跳过（配额决策、节流等设计内行为），不计失败也不清 fail_count
            # D11: 统一 skip_reason 字段（兼容旧 action 的 reason / vl_status）
            if result.get("skipped"):
                skip_msg = (
                    result.get("skip_reason")
                    or result.get("reason")
                    or result.get("vl_status")
                    or "no reason"
                )
                logger.info(
                    f"Loop 任务 {task.task_id} 跳过（skipped）: {skip_msg}"
                )
            elif result.get("success"):
                # 成功：清空失败计数和去重缓存（让下次失败能再次推送）
                prev_fail = task.fail_count
                task.fail_count = 0
                task.last_inbox_error = None
                if prev_fail > 0:
                    logger.info(
                        f"Loop 任务 {task.task_id} 恢复正常（前 fail_count={prev_fail}）"
                    )
            else:
                task.fail_count += 1
                logger.warning(
                    f"Loop 任务 {task.task_id} 返回失败 "
                    f"(fail_count={task.fail_count}/{task.fail_threshold}): "
                    f"{result.get('error')}"
                )
                # 首次失败推送 inbox（同 error 去重），让用户立即看到
                self._push_first_failure_inbox(task, result.get("error", "unknown"))
        except Exception as e:
            # last_run_at 更新策略与成功路径一致：catchup 取 max；manual 不推进
            if catchup_target_ts is not None:
                task.last_run_at = max(catchup_target_ts, task.last_run_at or 0)
            elif not manual:
                task.last_run_at = time.time()
            task.last_result = {"success": False, "error": str(e)}
            task.fail_count += 1
            task.run_count += 1
            logger.exception(
                f"Loop 任务 {task.task_id} 执行异常 "
                f"(fail_count={task.fail_count}/{task.fail_threshold})"
            )
            # 异常路径也推送首次失败 inbox
            self._push_first_failure_inbox(task, str(e))
        finally:
            task.running = False

        # 连续失败超阈值，自动暂停 + 推收件箱（critical 级别）
        if task.fail_count >= task.fail_threshold and not task.paused:
            task.paused = True
            task.paused_reason = "auto_failed"
            logger.error(
                f"Loop 任务 {task.task_id} 连续失败 {task.fail_count} 次，已自动暂停"
            )
            self._push_fail_inbox(task)
        self._save_state()

    def _push_first_failure_inbox(self, task: LoopTask, error: str):
        """首次失败推送 inbox（warning 级别），同 error 去重避免刷屏。

        推送策略：
        - fail_count==1 且 last_inbox_error != error → 推送（首次或错误类型变化）
        - fail_count==ceil(threshold/2) → 推送（累计过半提醒）
        - 其他情况不推送，避免 inbox 噪音
        - last_inbox_error 记录本次推送的 error，成功后清空
        """
        import math
        should_push = False
        push_reason = ""
        if task.fail_count == 1 and task.last_inbox_error != error:
            should_push = True
            push_reason = "首次失败"
        elif task.fail_count == math.ceil(task.fail_threshold / 2):
            # 累计过半提醒（threshold=5 时在第 3 次推送，threshold=3 时在第 2 次推送）
            should_push = True
            push_reason = f"失败累计过半（{task.fail_count}/{task.fail_threshold}）"
        if not should_push:
            return
        try:
            from server.inbox import get_store
            get_store().create({
                "source": task.source_segment,
                "category": "task_failure_warning",
                "title": f"Loop 任务失败 ({push_reason}): {task.task_id}",
                "description": (
                    f"任务 {task.task_id} 返回失败 "
                    f"(fail_count={task.fail_count}/{task.fail_threshold})。\n"
                    f"错误: {error}\n"
                    f"连续失败 {task.fail_threshold} 次后将自动暂停。"
                ),
                "payload": {
                    "task_id": task.task_id,
                    "fail_count": task.fail_count,
                    "fail_threshold": task.fail_threshold,
                    "error": error,
                    "push_reason": push_reason,
                },
            })
            task.last_inbox_error = error
        except Exception as e:
            logger.warning(f"推送首次失败 inbox 失败: {e}")

    def _push_fail_inbox(self, task: LoopTask):
        """任务连续失败时推收件箱告警"""
        try:
            from server.inbox import get_store
            get_store().create({
                "source": task.source_segment,
                "category": "task_failed",
                "title": f"Loop 任务自动暂停: {task.task_id}",
                "description": f"连续失败 {task.fail_count} 次，已暂停。最后错误: "
                               f"{(task.last_result or {}).get('error', 'unknown')}",
                "payload": {
                    "task_id": task.task_id,
                    "fail_count": task.fail_count,
                    "last_result": task.last_result,
                },
            })
        except Exception as e:
            logger.warning(f"推送收件箱告警失败: {e}")

    # ─── 状态持久化 ───────────────────────────────────────────

    def _save_state(self):
        """保存任务状态到 data/loops/tasks.json

        T06：原子写盘（tmp + os.replace），避免并发读看到半截 JSON。
        """
        import os as _os
        import tempfile as _tempfile

        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            state = {
                task_id: {
                    "last_run_at": t.last_run_at,
                    "last_result": t.last_result,
                    "fail_count": t.fail_count,
                    "paused": t.paused,
                    "paused_reason": t.paused_reason,
                    "run_count": t.run_count,
                    "enabled": t.enabled,
                }
                for task_id, t in self.tasks.items()
            }
            payload = json.dumps(state, ensure_ascii=False, indent=2)
            # T06：先写 tmp 文件，再 os.replace 原子替换
            fd, tmp_path = _tempfile.mkstemp(
                prefix=".tasks_", suffix=".tmp", dir=str(STATE_FILE.parent)
            )
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                _os.replace(tmp_path, STATE_FILE)
            except Exception:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"Loop 状态保存失败: {e}")

    def _load_state(self):
        """从 data/loops/tasks.json 恢复任务状态"""
        try:
            if not STATE_FILE.exists():
                return
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            auto_resumed = []
            for task_id, s in state.items():
                if task_id in self.tasks:
                    t = self.tasks[task_id]
                    t.last_run_at = s.get("last_run_at")
                    t.last_result = s.get("last_result")
                    t.fail_count = s.get("fail_count", 0)
                    t.paused = s.get("paused", False)
                    t.paused_reason = s.get("paused_reason")
                    t.run_count = s.get("run_count", 0)
                    # enabled 以 config 为准，但允许运行时暂停状态保留
                    if s.get("paused"):
                        # 启动时自动恢复 auto_failed 任务：
                        #   - 用户可能已修复根因（如更新 token、VL 恢复），需要让任务重新尝试
                        #   - 若根因仍存在，会再次累积 fail_count 触发自动暂停（带去重推送）
                        #   - manual 暂停不自动恢复，保留用户意图
                        if t.paused_reason == "auto_failed":
                            t.paused = False
                            t.paused_reason = None
                            t.fail_count = 0
                            t.last_inbox_error = None
                            auto_resumed.append(task_id)
                        elif not t.paused_reason:
                            # 兜底：paused=true 但 paused_reason 缺失（历史 schema 遗留）视为 auto_failed
                            t.paused = False
                            t.paused_reason = None
                            t.fail_count = 0
                            auto_resumed.append(task_id)
            if auto_resumed:
                logger.info(
                    f"Loop 启动时自动恢复 {len(auto_resumed)} 个 auto_failed 任务: "
                    f"{auto_resumed}（manual 暂停的任务不会自动恢复）"
                )
                # 推 inbox 通知用户：被自动恢复的任务会立即进入调度
                self._push_auto_resume_inbox(auto_resumed)
            logger.info(f"Loop 状态已恢复: {len(state)} 条记录")
        except Exception as e:
            logger.warning(f"Loop 状态恢复失败: {e}")

    def _push_auto_resume_inbox(self, task_ids: list[str]):
        """启动时自动恢复 auto_failed 任务后，推 inbox 通知用户"""
        try:
            from server.inbox import get_store
            store = get_store()
            for tid in task_ids:
                store.create({
                    "source": "loop_manager",
                    "category": "auto_resumed",
                    "title": f"Loop 任务已自动恢复: {tid}",
                    "description": (
                        f"后端启动时检测到任务 {tid} 处于 auto_failed 状态，已自动恢复。"
                        f"若根因仍未解决，会重新累积失败计数后再次暂停。"
                    ),
                    "payload": {"task_id": tid, "event": "auto_resume_on_startup"},
                })
        except Exception as e:
            logger.warning(f"推送自动恢复 inbox 通知失败: {e}")

    # ─── 运行时控制 ───────────────────────────────────────────

    def pause_task(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t.paused = True
        t.paused_reason = "manual"
        self._save_state()
        return True

    def resume_task(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        was_paused = t.paused
        paused_reason = t.paused_reason
        t.paused = False
        t.paused_reason = None
        t.fail_count = 0  # 恢复时重置失败计数
        t.last_inbox_error = None
        # 推 inbox 通知用户：手动 resume 已生效
        if was_paused:
            try:
                from server.inbox import get_store
                get_store().create({
                    "source": t.source_segment,
                    "category": "task_resumed",
                    "title": f"Loop 任务已恢复: {task_id}",
                    "description": (
                    f"任务 {task_id} 已从暂停状态恢复"
                    f"（原暂停原因: {paused_reason or 'unknown'}）。"
                    f"fail_count 已清零。若是 catchup 任务的 CronTrigger，"
                    f"会异步补跑 pause 期间错过的整点时刻。"
                ),
                    "payload": {
                        "task_id": task_id,
                        "prev_paused_reason": paused_reason,
                        "event": "manual_resume",
                    },
                })
            except Exception as e:
                logger.warning(f"推送 resume inbox 通知失败: {e}")
        self._save_state()
        return True

    async def run_task_now(self, task_id: str, catchup_target_ts: float | None = None) -> dict:
        """手动触发一次任务执行。

        catchup_target_ts: 可选，补跑模式下的目标整点时刻（epoch 秒）。
            None（默认）：正常触发。注意：手动触发不推进 last_run_at，
            不影响正常调度周期（不会顺延 IntervalTrigger 的下次运行）。
            非 None：补跑模式，action 收到 catchup_target_ts context，
            last_run_at 取 max(target_ts, current)（不回退进度，让 next_run_at 计算正确）。
            用于手动补跑历史缺失的小时总结。

        若任务正在执行中（调度器触发），返回 {"success": False, "error": "task_busy"}，
        调用方（REST endpoint）应把 busy 状态透传给 GUI。
        """
        t = self.tasks.get(task_id)
        if not t:
            raise KeyError(task_id)
        await self._run_task(t, catchup_target_ts=catchup_target_ts, manual=True)
        result = t.last_result or {"success": False, "error": "no result"}
        if isinstance(result, dict) and result.get("error") == "task_busy":
            return {"task_busy": True, **result}
        return result

    def get_status(self) -> dict:
        """供 /health 和 /loop/tasks 调用"""
        # VL 配额状态（活动追踪专用，失败不影响主状态）
        vl_quota_status: dict = {}
        try:
            from server.activity_tracker.vl_quota import vl_quota
            vl_quota_status = vl_quota.status()
        except Exception:
            pass
        return {
            "enabled": self.config.get("enabled", True),
            "running": self._running,
            "task_count": len(self.tasks),
            "active_count": sum(1 for t in self.tasks.values() if t.enabled and not t.paused),
            "paused_count": sum(1 for t in self.tasks.values() if t.paused),
            "registered_actions": sorted(self._action_registry.keys()),
            "tasks": [t.status_dict() for t in self.tasks.values()],
            "vl_quota": vl_quota_status,
        }


# ==================== 单例 ====================

_manager: LoopManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> LoopManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LoopManager(get_loops_config())
        return _manager


def reset_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None


def get_status() -> dict:
    """供 /health 调用"""
    try:
        return get_manager().get_status()
    except Exception as e:
        return {"available": False, "error": str(e)}


# ==================== REST 端点 ====================

@router.get("/tasks", operation_id="loop_list_tasks")
async def list_tasks():
    """列出所有 Loop 任务及状态"""
    return get_manager().get_status()


@router.get("/status", operation_id="loop_status")
async def loop_status():
    """Loop 系统整体状态"""
    return get_manager().get_status()


class RunTaskRequest(BaseSchema):
    """loop_run_task 可选请求体：支持补跑指定时刻"""
    catchup_target_ts: float | None = None  # epoch 秒，补跑目标时刻（如 14:00 → 处理 13:00-14:00 小时）


@router.post("/tasks/{task_id}/run", operation_id="loop_run_task")
async def run_task(task_id: str, body: RunTaskRequest | None = None):
    """手动触发一次任务（不影响正常调度周期）

    可选 body 参数 `catchup_target_ts`（epoch 秒）：补跑指定时刻，action 会处理
    该时刻的上一小时数据（如 ts=14:00 → 生成 13:00-14:00 小时总结）。用于回填
    pause 期间缺失的 hourly 文件。不传则按当前时间触发（默认行为）。
    """
    mgr = get_manager()
    if task_id not in mgr.tasks:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    catchup_ts = body.catchup_target_ts if body else None
    result = await mgr.run_task_now(task_id, catchup_target_ts=catchup_ts)
    # busy 状态用 409 透传，GUI 可区分"执行失败"和"任务正在跑，稍后再试"
    if isinstance(result, dict) and result.get("task_busy"):
        raise HTTPException(status_code=409, detail=result.get("message", "任务正在执行中"))
    return {"task_id": task_id, "catchup_target_ts": catchup_ts, "result": result}


@router.post("/tasks/{task_id}/pause", operation_id="loop_pause_task")
async def pause_task(task_id: str):
    """暂停任务"""
    mgr = get_manager()
    if not mgr.pause_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return {"task_id": task_id, "paused": True}


@router.post("/tasks/{task_id}/resume", operation_id="loop_resume_task")
async def resume_task(task_id: str):
    """恢复任务

    恢复后若是 catchup=True 的 CronTrigger 任务，会异步触发一次定向补跑，
    回填 pause 期间错过的整点时刻（如 hourly_summarize 在 auto_failed 期间缺失的小时）。
    补跑在后台进行，此接口立即返回。
    """
    mgr = get_manager()
    if not mgr.resume_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    # 异步触发 catchup（fire-and-forget）：仅对该任务补跑错过的时刻
    task = mgr.tasks.get(task_id)
    if task is not None:
        from lib.async_utils import spawn_background_task
        spawn_background_task(
            mgr._catchup_task_missed_runs(task),
            name=f"loop_catchup_{task_id}",
        )
    return {"task_id": task_id, "paused": False, "catchup_scheduled": task is not None and isinstance(task.trigger, CronTrigger) and task.trigger.catchup}
