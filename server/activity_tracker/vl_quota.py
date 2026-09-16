"""VL 配额管理器 — 在有限配额下智能分配 VL 调用时机

第一性原理：免费 VL 每日额度有限（默认 100 次），需要把调用集中在用户实际
活跃+焦点变化的时段，避免挂机/睡眠/自动脚本运行时浪费配额。

数据流：
    [loop] screen_vl 触发 → quota.should_call(signal)
        → allow: 调 VL → quota.record_call(vl_status)
        → deny : 跳过 VL（可跑 OCR 兜底）→ quota.record_call("skipped_quota")

配额文件（每日一个）：data/activity/vl_quota_YYYYMMDD.json
    {
        "date": "20260717",
        "used_today": 12,
        "by_hour": {"08": 5, "09": 7, ...},
        "skipped_today": 3,
        "daily_exhausted": false,
        "daily_exhausted_at": null,
        "daily_exhausted_reason": null,
        "last_call_ts": "2026-07-17T09:30:00"
    }

动态配额算法（不预分配按小时，按"剩余时间均摊"动态决定）：
    expected_remaining_hours = max(1, 24 - 当前小时)
    expected_pace = (daily_quota - used_today) / expected_remaining_hours
    used_this_hour = by_hour[当前小时]

    if daily_exhausted: deny "今日 VL 已耗尽"
    elif used_today >= daily_quota: deny "今日配额用完"
    elif signal.focus_changed: allow "焦点变化，优先放行"
    elif used_this_hour < expected_pace: allow "低于期望节奏(%.1f/h)" % expected_pace
    else: deny "本小时已用 %d 次，节流" % used_this_hour

自适应场景：
- 8 点才开始用电脑：00-07 时 used=0，8 点时 pace=(100-0)/16=6.25/h（高于 baseline 4.17）
- 23 点关机：剩余 1 小时，pace=(100-used)/1，激进
- 前半小时没动：本小时 used=0 < pace=4.17，allow
- 以后放宽到 500/天：baseline 涨到 20.8/h，逻辑自适应
- 配额降到 50/天：同逻辑收紧
"""

from __future__ import annotations

import atexit
import json
import logging
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from server.activity_tracker.activity_signal import compute_intensity

logger = logging.getLogger("localagent.vl_quota")

# 默认值：可通过 config.toml [loops.activity_tracker] 覆盖
_DEFAULT_DAILY_QUOTA = 100
_DEFAULT_QUOTA_RESET_HOUR = 0  # 0=00:00 自然日重置
_DEFAULT_MIN_INTERVAL_SECONDS = 180  # 最快每 3 分钟一次（物理节流上限）
_DEFAULT_QUOTA_DIR = "data/activity"
# Ticket 03: 活动强度缓存 TTL（5 分钟，避免每次 should_call 都读 jsonl）
_INTENSITY_CACHE_TTL_SECONDS = 300
# Ticket 06: 批量写盘阈值（用户指定 5×4=20，原 5 次决策写盘一次降为 1/4 频次）
_DEFAULT_PERSIST_BATCH_THRESHOLD = 20


def _project_root() -> Path:
    """项目根目录（server/activity_tracker/vl_quota.py → 上 3 级）"""
    return Path(__file__).resolve().parent.parent.parent


def _resolve_dir(p: str | Path) -> Path:
    """相对路径相对于项目根目录解析（不依赖 cwd）"""
    pp = Path(p)
    return pp if pp.is_absolute() else _project_root() / pp


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class VLQuotaManager:
    """VL 配额管理器（单例，线程安全）

    通过 vl_quota 单例访问。所有方法线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._daily_quota: int = _DEFAULT_DAILY_QUOTA
        self._reset_hour: int = _DEFAULT_QUOTA_RESET_HOUR
        self._min_interval: int = _DEFAULT_MIN_INTERVAL_SECONDS
        # Ticket 04: 动态 min_interval（基于近 10 分钟 focus 切换频率）
        self._min_interval_high_load: int = 60   # 高频（≥5 次/10min）时的 min_interval
        self._min_interval_low_load: int = 180   # 低频（0-1 次/10min）时的 min_interval
        self._quota_dir: Path = _resolve_dir(_DEFAULT_QUOTA_DIR)
        # D2: idle 状态拒绝 VL（默认启用，可通过 vl_skip_idle=false 关闭）
        self._skip_idle: bool = True
        # Ticket 03: 活动强度加权 pace（5 分钟 TTL 缓存，避免每次 should_call 读 jsonl）
        # _raw_dir_for_intensity 默认与 quota_dir 相同（config 中 raw_data_dir 既存 jsonl 也存配额文件）
        self._intensity_cache: dict = {"ts": 0.0, "score": 0.0, "focus_10min": 0, "focus_1h": 0, "state_dist": {}}
        self._intensity_window_minutes: int = 60
        self._raw_dir_for_intensity: Path = self._quota_dir
        # 当前日状态（内存缓存，懒加载）
        self._current_date: str = ""
        self._used_today: int = 0
        self._by_hour: dict[str, int] = {}
        self._skipped_today: int = 0
        self._daily_exhausted: bool = False
        self._daily_exhausted_at: str | None = None
        self._daily_exhausted_reason: str | None = None
        # 429 分类：真耗尽（quota_exhausted）vs 上游过载（upstream_overload）
        # 两者旧版都按 daily_exhausted 处理，导致恢复后浪费剩余配额。
        # 现在区分记录，便于事后回看配额管理器是否"误判耗尽"。
        self._exhausted_kind: str | None = None  # "quota_exhausted" | "upstream_overload"
        self._last_call_ts: str | None = None
        self._last_call_epoch: float = 0.0
        self._loaded: bool = False
        # ===== 评估指标（事后回看用，不影响决策）=====
        # 决策分类计数：allow_focus / allow_pace / deny_pace / deny_exhausted / deny_quota_used / deny_min_interval
        # 分别对应"焦点放行 / 节奏放行 / 节流 / 已耗尽 / 配额用完 / 物理节流"
        self._decision_counts: dict[str, int] = {}
        # 429 来源细分：来自 ModelScope / OpenAI / DeepSeek / 其他
        self._quota_429_sources: dict[str, int] = {}
        # 放行后实际结果分布：ok / failed_after_retries / skipped_*
        self._grant_outcomes: dict[str, int] = {}
        # 决策日志（最近 N 条，用于事后回看具体决策上下文，限制大小避免文件膨胀）
        self._decision_log: list[dict] = []
        # 上限：超过则丢弃最旧（FIFO）
        self._decision_log_max = 200
        # 自上次 _persist 以来的决策计数（Ticket 06: 阈值从 5 提到 20，写盘频次降为 1/4）
        self._decisions_since_persist: int = 0
        # Ticket 06: 批量写盘阈值（达到阈值才 _persist，避免每次 record_call 都写盘）
        self._persist_batch_threshold: int = _DEFAULT_PERSIST_BATCH_THRESHOLD
        # Ticket 06: dirty 标记（record_call 后置 True，flush/shutdown 时强制写盘）
        self._dirty: bool = False
        # Ticket 06: shutdown hooks 注册标记（避免重复注册，单例场景下只注册一次）
        self._shutdown_hooks_registered: bool = False
        # 6-3: 注册时保存的原信号 handler（flush 后恢复并重发信号，保住默认语义）
        # 值为 signal.Handlers 联合类型（SIG_DFL/default_int_handler/callable），用 Any 承载
        self._original_handlers: dict[int, Any] = {}
        # D1: 每小时 focus 放行次数（用于 focus_cap 约束，防止焦点频繁变化击穿 pace）
        self._focus_approvals_by_hour: dict[str, int] = {}

    def configure(
        self,
        daily_quota: int | None = None,
        reset_hour: int | None = None,
        min_interval_seconds: int | None = None,
        quota_dir: str | Path | None = None,
        skip_idle: bool | None = None,
        intensity_window_minutes: int | None = None,
        intensity_raw_dir: str | Path | None = None,
        min_interval_high_load: int | None = None,
        min_interval_low_load: int | None = None,
        persist_batch_threshold: int | None = None,
    ) -> None:
        """从配置更新参数。任何参数为 None 则保持原值。"""
        with self._lock:
            if daily_quota is not None:
                self._daily_quota = max(1, int(daily_quota))
            if reset_hour is not None:
                self._reset_hour = max(0, min(23, int(reset_hour)))
            if min_interval_seconds is not None:
                self._min_interval = max(0, int(min_interval_seconds))
            if min_interval_high_load is not None:
                self._min_interval_high_load = max(60, int(min_interval_high_load))
            if min_interval_low_load is not None:
                self._min_interval_low_load = max(60, int(min_interval_low_load))
            if quota_dir is not None:
                self._quota_dir = _resolve_dir(quota_dir)
                # 同步更新 intensity raw_dir（默认与 quota_dir 相同，除非显式指定）
                if intensity_raw_dir is None:
                    self._raw_dir_for_intensity = self._quota_dir
            if skip_idle is not None:
                self._skip_idle = bool(skip_idle)
            if intensity_window_minutes is not None:
                self._intensity_window_minutes = max(1, int(intensity_window_minutes))
            if intensity_raw_dir is not None:
                self._raw_dir_for_intensity = _resolve_dir(intensity_raw_dir)
            if persist_batch_threshold is not None:
                # 下限 1（避免误设 0 导致永不写盘）；上限无限制（用户可设很大强制走 flush）
                self._persist_batch_threshold = max(1, int(persist_batch_threshold))
            # 配置变了之后重新加载（可能日期也变了）
            self._loaded = False
            # Ticket 06: 首次 configure 时注册 shutdown hooks（atexit + SIGTERM + SIGINT）
            # Anti-Cheat 要求三者齐全，覆盖 Ctrl+C（SIGINT）和 kill（SIGTERM）和正常退出（atexit）
            if not self._shutdown_hooks_registered:
                self._register_shutdown_hooks()
                self._shutdown_hooks_registered = True

    def _quota_file(self, date_str: str) -> Path:
        return self._quota_dir / f"vl_quota_{date_str}.json"

    def _current_quota_date_str(self) -> str:
        """根据 reset_hour 计算当前配额日期字符串

        reset_hour=0：00:00-23:59 都是当天
        reset_hour=5：05:00-次日 04:59 都算"当天"（与 daily_summary 一致）
        """
        now = datetime.now()
        # 如果当前小时 < reset_hour，配额日 = 昨天
        if self._reset_hour > 0 and now.hour < self._reset_hour:
            # 跨日：昨日 reset_hour 到今日 reset_hour-1 算"昨日"
            # 例：reset_hour=5，现在 03:00 → 算昨日（昨日 05:00 - 今日 04:59）
            from datetime import timedelta
            quota_day = now - timedelta(days=1)
        else:
            quota_day = now
        return quota_day.strftime("%Y%m%d")

    def _load_if_needed(self) -> None:
        """懒加载当前配额文件；跨日时自动重置内存状态

        Ticket 06: 无配额文件时立即 _persist() 写盘初始化，避免后端运行中
        因首次 record_call 才写盘导致早期决策日志丢失。启动日志确认路径。
        """
        if self._loaded:
            # 检查是否跨日
            today = self._current_quota_date_str()
            if today != self._current_date:
                self._loaded = False
        if self._loaded:
            return
        today = self._current_quota_date_str()
        path = self._quota_file(today)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._current_date = data.get("date", today)
                self._used_today = int(data.get("used_today", 0))
                self._by_hour = dict(data.get("by_hour", {}))
                self._skipped_today = int(data.get("skipped_today", 0))
                self._daily_exhausted = bool(data.get("daily_exhausted", False))
                self._daily_exhausted_at = data.get("daily_exhausted_at")
                self._daily_exhausted_reason = data.get("daily_exhausted_reason")
                self._exhausted_kind = data.get("exhausted_kind")
                self._last_call_ts = data.get("last_call_ts")
                self._last_call_epoch = data.get("last_call_epoch", 0.0)
                # 评估指标（向后兼容：旧文件缺字段时取空）
                self._decision_counts = dict(data.get("decision_counts") or {})
                self._quota_429_sources = dict(data.get("quota_429_sources") or {})
                self._grant_outcomes = dict(data.get("grant_outcomes") or {})
                self._decision_log = list(data.get("decision_log") or [])[-self._decision_log_max:]
                self._focus_approvals_by_hour = dict(data.get("focus_approvals_by_hour") or {})
            except Exception as e:
                logger.warning("加载 VL 配额文件失败 %s: %s，重置", path, e)
                self._reset_state(today)
        else:
            # Ticket 06: 无文件时初始化并立即写盘，避免早期决策日志丢失
            logger.info("VL 配额文件不存在，初始化: %s", path)
            self._reset_state(today)
            self._persist()
        # Ticket 06: 启动日志确认配额文件路径（便于排查"配额文件未写盘"问题）
        logger.info("VL 配额文件路径: %s (used_today=%d, daily_quota=%d)",
                    self._quota_file(self._current_date), self._used_today, self._daily_quota)
        self._loaded = True

    def _reset_state(self, date_str: str) -> None:
        self._current_date = date_str
        self._used_today = 0
        self._by_hour = {}
        self._skipped_today = 0
        self._daily_exhausted = False
        self._daily_exhausted_at = None
        self._daily_exhausted_reason = None
        self._exhausted_kind = None
        self._last_call_ts = None
        self._last_call_epoch = 0.0
        # 评估指标重置
        self._decision_counts = {}
        self._quota_429_sources = {}
        self._grant_outcomes = {}
        self._decision_log = []
        self._decisions_since_persist = 0
        self._focus_approvals_by_hour = {}

    def _persist(self) -> None:
        """把当前状态写回配额文件（调用方需持锁）

        T06：原子写盘（tmp + os.replace），避免并发读看到半截 JSON。
        """
        import os as _os
        import tempfile as _tempfile

        self._quota_dir.mkdir(parents=True, exist_ok=True)
        path = self._quota_file(self._current_date)
        data = {
            "date": self._current_date,
            "used_today": self._used_today,
            "by_hour": self._by_hour,
            "skipped_today": self._skipped_today,
            "daily_exhausted": self._daily_exhausted,
            "daily_exhausted_at": self._daily_exhausted_at,
            "daily_exhausted_reason": self._daily_exhausted_reason,
            "exhausted_kind": self._exhausted_kind,
            "last_call_ts": self._last_call_ts,
            "last_call_epoch": self._last_call_epoch,
            # 评估指标
            "decision_counts": self._decision_counts,
            "quota_429_sources": self._quota_429_sources,
            "grant_outcomes": self._grant_outcomes,
            "decision_log": self._decision_log,
            "focus_approvals_by_hour": self._focus_approvals_by_hour,
        }
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            # T06：先写 tmp 文件，再 os.replace 原子替换，避免半截 JSON
            fd, tmp_path = _tempfile.mkstemp(
                prefix=".vl_quota_", suffix=".tmp", dir=str(self._quota_dir)
            )
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                _os.replace(tmp_path, path)
            except Exception:
                # 失败时清理 tmp 文件
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            # Ticket 06: 写盘成功后清 dirty 标记
            self._dirty = False
        except Exception as e:
            logger.warning("写 VL 配额文件失败 %s: %s", path, e)

    def _persist_batched(self) -> None:
        """Ticket 06: 批量写盘（按 _persist_batch_threshold 阈值决定是否实际写盘）

        累积决策计数达到阈值才 _persist，否则仅标记 _dirty=True。
        调用方需持锁。用于 record_call / _record_decision 路径，
        降低写盘频次到原来的 1/4（阈值 5→20）。
        shutdown 时调 flush() 强制写盘，避免 dirty 状态丢失。
        """
        self._decisions_since_persist = self._decisions_since_persist + 1
        self._dirty = True
        if self._decisions_since_persist >= self._persist_batch_threshold:
            self._decisions_since_persist = 0
            self._persist()

    def flush(self) -> None:
        """Ticket 06: 强制写盘（供 shutdown 调用，覆盖 Ctrl+C / kill / 正常退出场景）

        无论 _dirty 状态和 _decisions_since_persist 计数，强制把当前内存状态
        写到磁盘。线程安全（内部加锁）。幂等（多次调用安全）。
        """
        try:
            with self._lock:
                # 即使 _dirty=False 也写一次（shutdown 场景下确保最新状态落盘，
                # 代价是一次额外 IO，可接受）
                self._persist()
                self._decisions_since_persist = 0
        except Exception as e:
            logger.warning("VL 配额 flush 失败: %s", e)

    def _register_shutdown_hooks(self) -> None:
        """Ticket 06: 注册 shutdown hooks（atexit + SIGTERM + SIGINT）

        Anti-Cheat 要求三者齐全：
        - atexit：覆盖 Python 正常退出（sys.exit / 主线程结束）
        - SIGINT：覆盖 Ctrl+C（KeyboardInterrupt）
        - SIGTERM：覆盖 kill 命令（默认信号）

        6-3 修复：signal.signal 是"替换"不是"链式"——此前无条件覆盖既有 handler，
        且 handler 只 flush 不恢复默认行为，Ctrl+C 后 KeyboardInterrupt 不再抛出，
        uvicorn 优雅关停被破坏（表现为"按了没反应"）。现在：
        - 当前 handler 已是非默认（其他组件如 uvicorn 已注册）→ 不覆盖，只留 atexit flush
        - 当前是默认 → 注册本 handler；_signal_handler 在 flush 后恢复默认语义
        注意：signal.signal 只能在主线程调用，且 SIGTERM 在 Windows 上不被支持
        （Windows 只支持 SIGINT 和 SIGBREAK），需 try/except 兜底。
        """
        # atexit：最可靠的退出钩子，所有正常退出路径都会触发
        atexit.register(self.flush)
        # 6-3: SIGINT 的 Python 默认 handler 是 default_int_handler（非 SIG_DFL）
        _default_handlers = (signal.SIG_DFL, signal.default_int_handler, None)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                current = signal.getsignal(sig)
                if current not in _default_handlers:
                    logger.info(
                        "信号 %s 已有自定义 handler，跳过注册（atexit flush 仍兜底）", sig
                    )
                    continue
                self._original_handlers[sig] = current
                signal.signal(sig, self._signal_handler)
            except (ValueError, OSError, AttributeError) as e:
                # 非主线程 / Windows 限制 / 平台无该信号常量
                logger.warning("注册 %s handler 失败（不影响 atexit 兜底）: %s", sig, e)

    def _signal_handler(self, signum, frame) -> None:
        """Ticket 06: 信号处理器（SIGINT/SIGTERM 触发时强制 flush 后退出）

        注意：signal handler 中应避免复杂操作（如 IO），但本场景下配额文件
        丢失会导致当日 VL 配额统计失真，值得在 handler 中强制 flush。
        flush() 内部有 try/except 兜底，不会抛异常中断退出流程。

        6-3: flush 后恢复默认信号语义（能走到这里说明注册时是默认 handler）：
        - SIGINT 默认语义是抛 KeyboardInterrupt（uvicorn 优雅关停依赖它）。
          Windows 上 os.kill(pid, SIGINT) 等价 TerminateProcess 硬杀，不能重发，
          直接在 handler 内抛出等价异常。
        - SIGTERM（实际仅 posix 会被投递）恢复默认 handler 后重发信号走默认终止。
        """
        try:
            self.flush()
        except Exception as e:
            logger.warning("信号 handler 中 flush 失败: %s", e)
        original = self._original_handlers.get(signum)
        if original is not None:
            try:
                signal.signal(signum, original)
            except (ValueError, OSError) as e:
                logger.warning("恢复 %s 原 handler 失败: %s", signum, e)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        try:
            signal.raise_signal(signum)
        except (OSError, ValueError, AttributeError) as e:
            # 重发失败时不主动 sys.exit，atexit 仍会触发
            logger.warning("信号 %s 重发失败（atexit 兜底）: %s", signum, e)

    # ---------- 核心决策 ----------

    @staticmethod
    def compute_adjusted_pace(base_pace: float, intensity_score: float) -> float:
        """根据活动强度分数调整 pace（Ticket 03 / D1）

        - score >= 1.5 → base_pace * 1.5（高强度多调 VL，用户频繁切窗口时屏幕描述不缺失）
        - 1.0 <= score < 1.5 → base_pace * 1.0（中强度不变）
        - score < 1.0 → base_pace * 0.5（低强度少调 VL，避免浪费配额）
        """
        if intensity_score >= 1.5:
            return base_pace * 1.5
        elif intensity_score >= 1.0:
            return base_pace * 1.0
        else:
            return base_pace * 0.5

    def _compute_dynamic_min_interval(self, focus_count_10min: int) -> int:
        """根据近 10 分钟 focus 切换频率动态计算 min_interval（Ticket 04 / D5）

        - focus_count_10min >= 5（高频）→ self._min_interval_high_load（默认 60s）
        - 2 <= focus_count_10min <= 4（中频）→ 120s
        - 0 <= focus_count_10min <= 1（低频）→ self._min_interval_low_load（默认 180s）
        下限 60s（防止触发上游限流）。
        """
        if focus_count_10min >= 5:
            return max(60, self._min_interval_high_load)
        elif focus_count_10min >= 2:
            return 120
        else:
            return max(60, self._min_interval_low_load)

    def _compute_intensity_cached(self) -> tuple[float, int, int, dict]:
        """带 5 分钟 TTL 的 compute_intensity 缓存包装（调用方需持锁）

        避免每次 should_call 都读 windows jsonl。
        缓存失效后重新调用 compute_intensity，失败时返回默认 1.0（中强度，不调整）。
        """
        now_epoch = datetime.now().timestamp()
        cache = self._intensity_cache
        if cache["ts"] > 0 and (now_epoch - cache["ts"]) < _INTENSITY_CACHE_TTL_SECONDS:
            return (
                cache["score"],
                cache["focus_10min"],
                cache["focus_1h"],
                cache["state_dist"],
            )
        # 缓存过期，重新计算
        try:
            score, focus_10min, focus_1h, state_dist = compute_intensity(
                self._raw_dir_for_intensity,
                self._intensity_window_minutes,
            )
        except Exception as e:
            logger.warning("compute_intensity 调用失败: %s，返回默认 1.0", e)
            score, focus_10min, focus_1h, state_dist = 1.0, 0, 0, {}
        self._intensity_cache = {
            "ts": now_epoch,
            "score": score,
            "focus_10min": focus_10min,
            "focus_1h": focus_1h,
            "state_dist": state_dist,
        }
        return (score, focus_10min, focus_1h, state_dist)

    def should_call(self, signal: dict | None = None) -> tuple[bool, str, dict]:
        """判断当前是否应该调用 VL

        Args:
            signal: 活动信号 dict（来自 activity_signal.compute_signal）
                含 state ("idle"|"passive"|"active"|"focus_changed")，
                focus_changed: bool

        Returns:
            (allow, reason, debug_info)
            allow: True 允许调用，False 应跳过
            reason: 人类可读原因
            debug_info: 调试信息 dict（pace/used_today/used_this_hour 等）
        """
        with self._lock:
            self._load_if_needed()

            now = datetime.now()
            hour_key = now.strftime("%H")
            used_this_hour = self._by_hour.get(hour_key, 0)
            remaining_hours = max(1, 24 - now.hour)
            remaining_quota = self._daily_quota - self._used_today
            expected_pace = remaining_quota / remaining_hours if remaining_quota > 0 else 0.0

            focus_changed = bool(signal and signal.get("focus_changed"))
            signal_state = (signal or {}).get("state", "unknown")

            debug = {
                "daily_quota": self._daily_quota,
                "used_today": self._used_today,
                "remaining_quota": remaining_quota,
                "used_this_hour": used_this_hour,
                "expected_pace": round(expected_pace, 2),
                "remaining_hours": remaining_hours,
                "daily_exhausted": self._daily_exhausted,
                "signal_state": signal_state,
                "hour_key": hour_key,
            }

            # ===== Ticket 03+04: 活动强度计算（5 分钟 TTL 缓存）=====
            # 提前到物理节流之前，因为 Ticket 04 需要用 focus_10min 动态调整 min_interval。
            # 高强度 ×1.5（多调 VL）、中强度 ×1.0（不变）、低强度 ×0.5（少调 VL）
            # focus_cap 仍用 expected_pace（焦点变化是独立优先信号，不随强度收紧），
            # 仅 pace 判定用 adjusted_pace。
            intensity_score, focus_10min, focus_1h, _state_dist = self._compute_intensity_cached()
            adjusted_pace = self.compute_adjusted_pace(expected_pace, intensity_score)
            debug["intensity_score"] = intensity_score
            debug["adjusted_pace"] = round(adjusted_pace, 2)
            debug["intensity_focus_10min"] = focus_10min

            # ===== Ticket 04: 动态 min_interval =====
            # 基于近 10 分钟 focus 切换频率调整物理节流间隔：
            # 高频(≥5) → high_load（默认 60s，高峰期不漏窗口切换）
            # 中频(2-4) → 120s
            # 低频(0-1) → low_load（默认 180s，保持默认节流）
            # 下限 60s（防止触发上游限流）
            # 注意：min_interval=0 表示禁用物理节流（测试或显式关闭），此时也不动态节流
            dynamic_min_interval = self._compute_dynamic_min_interval(focus_10min)
            debug["dynamic_min_interval"] = dynamic_min_interval

            # 物理节流：上次调用距今太近则拒绝（避免短时间内多次 VL）
            # 仅在 self._min_interval > 0 时生效（min_interval=0 = 禁用物理节流）
            now_epoch = now.timestamp()
            if self._min_interval > 0 and self._last_call_epoch > 0:
                elapsed = now_epoch - self._last_call_epoch
                if elapsed < dynamic_min_interval:
                    wait = dynamic_min_interval - elapsed
                    debug["decision"] = "deny_min_interval"
                    self._record_decision(
                        decision="deny_min_interval", allow=False,
                        signal=signal_state, focus_changed=focus_changed,
                        pace=expected_pace, used_this_hour=used_this_hour,
                        reason=f"距上次调用仅 {int(elapsed)}s，节流",
                    )
                    return (
                        False,
                        f"距上次调用仅 {int(elapsed)}s，节流（最快每 {dynamic_min_interval}s 一次，需等 {int(wait)}s）",
                        debug,
                    )

            # 0. idle 状态拒绝（D2：用户挂机/睡觉时不浪费配额，优先于 exhausted 检查）
            #    判定条件必须是 state == "idle"，不能误伤 passive/active
            if self._skip_idle and signal_state == "idle":
                debug["decision"] = "deny_idle"
                self._record_decision(
                    decision="deny_idle", allow=False,
                    signal=signal_state, focus_changed=focus_changed,
                    pace=expected_pace, used_this_hour=used_this_hour,
                    reason="idle 状态跳过 VL（用户可能挂机/睡觉）",
                )
                return False, "idle 状态跳过 VL（用户可能挂机/睡觉）", debug

            # 1. 今日已耗尽（来自 429 检测）
            if self._daily_exhausted:
                debug["decision"] = "deny_exhausted"
                self._record_decision(
                    decision="deny_exhausted", allow=False,
                    signal=signal_state, focus_changed=focus_changed,
                    pace=expected_pace, used_this_hour=used_this_hour,
                    reason="今日 VL 已耗尽（ModelScope 返回 429）",
                )
                return False, "今日 VL 已耗尽（ModelScope 返回 429）", debug

            # 2. 配额用完
            if self._used_today >= self._daily_quota:
                debug["decision"] = "deny_quota_used"
                self._record_decision(
                    decision="deny_quota_used", allow=False,
                    signal=signal_state, focus_changed=focus_changed,
                    pace=expected_pace, used_this_hour=used_this_hour,
                    reason=f"今日配额已用完 ({self._used_today}/{self._daily_quota})",
                )
                return False, f"今日配额已用完 ({self._used_today}/{self._daily_quota})", debug

            # 3. 焦点变化 → 优先放行（即使用本小时已用满）
            #    D1: 增加 focus_cap 约束，防止焦点频繁变化击穿 pace 节流
            if focus_changed:
                focus_cap = max(expected_pace * 1.5, expected_pace + 2)
                focus_approvals_this_hour = self._focus_approvals_by_hour.get(hour_key, 0)
                if focus_approvals_this_hour < focus_cap:
                    self._focus_approvals_by_hour[hour_key] = focus_approvals_this_hour + 1
                    debug["decision"] = "allow_focus"
                    self._record_decision(
                        decision="allow_focus", allow=True,
                        signal=signal_state, focus_changed=focus_changed,
                        pace=expected_pace, used_this_hour=used_this_hour,
                        reason="窗口焦点变化，优先放行",
                    )
                    return True, "窗口焦点变化，优先放行", debug
                else:
                    # focus 放行次数已达上限，回落到 pace 判定
                    debug["focus_cap"] = round(focus_cap, 2)
                    debug["focus_approvals_this_hour"] = focus_approvals_this_hour
                    # 不 return，fall through 到 pace 检查（步骤 4）

            # 4. 低于期望节奏 → 放行（用 adjusted_pace：高强度放宽，低强度收紧）
            if used_this_hour < adjusted_pace:
                debug["decision"] = "allow_pace"
                self._record_decision(
                    decision="allow_pace", allow=True,
                    signal=signal_state, focus_changed=focus_changed,
                    pace=adjusted_pace, used_this_hour=used_this_hour,
                    reason=f"低于期望节奏 ({used_this_hour}/{adjusted_pace:.1f} 本小时)",
                )
                return True, f"低于期望节奏 ({used_this_hour}/{adjusted_pace:.1f} 本小时)", debug

            # 5. 节流
            debug["decision"] = "deny_pace"
            self._record_decision(
                decision="deny_pace", allow=False,
                signal=signal_state, focus_changed=focus_changed,
                pace=adjusted_pace, used_this_hour=used_this_hour,
                reason=f"本小时已用 {used_this_hour} 次（pace={adjusted_pace:.1f}），节流",
            )
            return False, f"本小时已用 {used_this_hour} 次（pace={adjusted_pace:.1f}），节流", debug

    def _record_decision(
        self,
        decision: str,
        allow: bool,
        signal: str,
        focus_changed: bool,
        pace: float,
        used_this_hour: int,
        reason: str,
    ) -> None:
        """记录一次配额决策（计数 + 日志，调用方需持锁）。

        不影响决策本身，纯事后回看用。
        - decision_counts 按 decision 分类计数（如 allow_focus=12, deny_pace=5）
        - decision_log 记录最近 N 条决策上下文，便于排查具体场景
        """
        self._decision_counts[decision] = self._decision_counts.get(decision, 0) + 1
        self._decision_log.append({
            "ts": _now_iso(),
            "decision": decision,
            "allow": allow,
            "signal": signal,
            "focus_changed": focus_changed,
            "pace": round(pace, 2),
            "used_this_hour": used_this_hour,
            "reason": reason[:120],
        })
        # FIFO 上限保护
        if len(self._decision_log) > self._decision_log_max:
            self._decision_log = self._decision_log[-self._decision_log_max:]
        # Ticket 06: 批量写盘（阈值从 5 提到 20，写盘频次降为 1/4）。
        # 纯 deny 时段（无 record_call 触发）的决策日志通过本方法批量写盘，
        # 避免后端重启就丢。shutdown 时 flush() 强制写盘兜底。
        # 调用方需持锁，本方法在 should_call 内 with self._lock 块里调用，安全。
        self._persist_batched()

    def record_call(self, vl_status: str) -> None:
        """记录一次 VL 调用结果（无论成功/失败/skipped）

        Args:
            vl_status: 调用结果状态，影响 grant_outcomes 统计：
                - "ok"：VL 调用成功，消耗配额
                - "failed_after_retries"：重试后失败，不消耗配额（D2）
                - "skipped_idle"：idle 状态拒绝（Ticket 05）
                - "skipped_pace"：本小时已达 pace，节流拒绝（Ticket 05）
                - "skipped_min_interval"：物理节流拒绝（Ticket 05）
                - "skipped_exhausted"：今日配额耗尽/用完（Ticket 05，替代旧 skipped_quota）
                - "skipped_high_load"：OCR 兜底因 CPU/GPU 高负载也被跳过（独立维度）
                - "skipped_unknown"：兜底（理论上不应到达）
                旧 "skipped_quota" 不再产生（硬切，不保留兼容）。
        """
        with self._lock:
            self._load_if_needed()
            now = datetime.now()
            hour_key = now.strftime("%H")
            now_iso = now.isoformat(timespec="seconds")
            now_epoch = now.timestamp()

            # 评估指标：放行后实际结果分布（与 decision_counts 对应，
            # 用于回看"放行后是否真的产生了有效调用"）
            self._grant_outcomes[vl_status] = self._grant_outcomes.get(vl_status, 0) + 1

            # 只在 VL 调用成功（ok）时增加 used_today 和 by_hour
            # D2: failed_after_retries 不计配额（API 失败不应导致日配额损失）
            # skipped_* 不消耗配额
            if vl_status.startswith("ok"):
                self._used_today += 1
                self._by_hour[hour_key] = self._by_hour.get(hour_key, 0) + 1
                self._last_call_ts = now_iso
                self._last_call_epoch = now_epoch
            elif vl_status.startswith("failed"):
                # 失败调用更新 last_call（用于 min_interval 物理节流），但不计配额
                self._last_call_ts = now_iso
                self._last_call_epoch = now_epoch
            elif vl_status.startswith("skipped"):
                self._skipped_today += 1
                # skipped 不更新 last_call（min_interval 只限制真实调用）
            # Ticket 06: 批量写盘（替代每次 record_call 都立即 _persist），
            # 降低写盘频次到原来的 1/4。shutdown 时 flush() 强制写盘兜底。
            self._persist_batched()

    def mark_daily_exhausted(self, reason: str, kind: str = "upstream_overload", source: str | None = None) -> None:
        """标记今日 VL 已耗尽（ModelScope 返回非并发 429 时调用）

        Args:
            reason: 人类可读原因
            kind: 耗尽类型，区分两种根因：
                - "quota_exhausted"：配额真用完（HTTP 429 + body 明示 quota exceeded）
                  后续即便上游恢复也不会自动可用，等到 reset_hour 重置
                - "upstream_overload"：上游临时过载（HTTP 429 + body 提示 rate_limited / concurrency）
                  上游恢复后理论可继续用，但旧版按 daily_exhausted 处理，浪费剩余配额
                记录 kind 便于事后回看是否"误判耗尽"
            source: 429 来源 provider 标识（如 "ModelScope" / "OpenAI"），用于 quota_429_sources 细分
        """
        with self._lock:
            self._load_if_needed()
            if not self._daily_exhausted:
                self._daily_exhausted = True
                self._daily_exhausted_at = _now_iso()
                self._daily_exhausted_reason = reason
                self._exhausted_kind = kind
                if source:
                    self._quota_429_sources[source] = self._quota_429_sources.get(source, 0) + 1
                self._persist()
                logger.warning("VL 今日已耗尽 (kind=%s, source=%s): %s", kind, source, reason)

    def is_daily_exhausted(self) -> bool:
        """检查今日是否已耗尽"""
        with self._lock:
            self._load_if_needed()
            return self._daily_exhausted

    def status(self) -> dict:
        """返回配额状态（供 /health 和监控面板使用）"""
        with self._lock:
            self._load_if_needed()
            now = datetime.now()
            hour_key = now.strftime("%H")
            remaining_hours = max(1, 24 - now.hour)
            remaining_quota = self._daily_quota - self._used_today

            # ===== 评估指标派生量 =====
            total_decisions = sum(self._decision_counts.values())
            allow_count = sum(
                v for k, v in self._decision_counts.items() if k.startswith("allow_")
            )
            deny_count = total_decisions - allow_count
            # 节流命中率：被 deny 的占比（越低越宽松，越高越收紧）
            deny_ratio = round(deny_count / total_decisions, 3) if total_decisions > 0 else 0.0
            # 放行有效率：放行后 vl_status=ok 的占比
            ok_count = self._grant_outcomes.get("ok", 0)
            grant_total = sum(self._grant_outcomes.values())
            ok_ratio = round(ok_count / grant_total, 3) if grant_total > 0 else 0.0
            # 浪费时长估算：daily_exhausted_at → 现在（或 reset_hour 重置点）
            # 用于回看"误判耗尽后浪费了多少本可用配额时间"
            wasted_minutes = 0
            if self._daily_exhausted and self._daily_exhausted_at:
                try:
                    exhausted_dt = datetime.fromisoformat(self._daily_exhausted_at)
                    wasted_minutes = int((now - exhausted_dt).total_seconds() / 60)
                    if wasted_minutes < 0:
                        wasted_minutes = 0
                except Exception:
                    pass

            return {
                "daily_quota": self._daily_quota,
                "used_today": self._used_today,
                "skipped_today": self._skipped_today,
                "remaining_quota": max(0, remaining_quota),
                "by_hour": dict(self._by_hour),
                "daily_exhausted": self._daily_exhausted,
                "daily_exhausted_at": self._daily_exhausted_at,
                "daily_exhausted_reason": self._daily_exhausted_reason,
                "exhausted_kind": self._exhausted_kind,  # "quota_exhausted" | "upstream_overload" | None
                "last_call_ts": self._last_call_ts,
                "reset_hour": self._reset_hour,
                "min_interval_seconds": self._min_interval,
                "min_interval_high_load_seconds": self._min_interval_high_load,
                "min_interval_low_load_seconds": self._min_interval_low_load,
                "expected_pace": round(remaining_quota / remaining_hours, 2) if remaining_quota > 0 else 0,
                "used_this_hour": self._by_hour.get(hour_key, 0),
                "quota_date": self._current_date,
                # Ticket 03: 活动强度（从缓存读最新值，供 /health 暴露）
                "intensity_score": self._intensity_cache.get("score", 0.0),
                "intensity_focus_10min": self._intensity_cache.get("focus_10min", 0),
                "intensity_focus_1h": self._intensity_cache.get("focus_1h", 0),
                # ===== 评估指标 =====
                "metrics": {
                    "decision_counts": dict(self._decision_counts),
                    "deny_ratio": deny_ratio,  # 节流命中率（被 deny 占比）
                    "allow_count": allow_count,
                    "deny_count": deny_count,
                    "total_decisions": total_decisions,
                    "grant_outcomes": dict(self._grant_outcomes),
                    "ok_ratio": ok_ratio,  # 放行有效率（放行后 ok 占比）
                    # Ticket 07: intensity_score 汇总到 metrics 子字段（顶层保留向后兼容）
                    "intensity_score": self._intensity_cache.get("score", 0.0),
                    "intensity_focus_10min": self._intensity_cache.get("focus_10min", 0),
                    "intensity_focus_1h": self._intensity_cache.get("focus_1h", 0),
                    "quota_429_sources": dict(self._quota_429_sources),
                    "exhausted_kind": self._exhausted_kind,
                    "wasted_minutes_after_exhausted": wasted_minutes,  # 耗尽后到现在浪费的分钟数
                    "decision_log_size": len(self._decision_log),
                    "decision_log_max": self._decision_log_max,
                },
            }


# 全局单例
vl_quota = VLQuotaManager()


def configure_from_loops_config(config: dict) -> None:
    """从 [loops.activity_tracker] 配置更新 VL 配额参数

    Args:
        config: get_loops_config()["tasks"]["activity_tracker"] 字典
    """
    vl_quota.configure(
        daily_quota=config.get("vl_daily_quota", _DEFAULT_DAILY_QUOTA),
        reset_hour=config.get("vl_quota_reset_hour", _DEFAULT_QUOTA_RESET_HOUR),
        min_interval_seconds=config.get("vl_min_interval_seconds", _DEFAULT_MIN_INTERVAL_SECONDS),
        quota_dir=config.get("raw_data_dir", _DEFAULT_QUOTA_DIR),
        skip_idle=config.get("vl_skip_idle", True),
        intensity_window_minutes=config.get("vl_activity_intensity_window_minutes", 60),
        min_interval_high_load=config.get("vl_min_interval_high_load_seconds", 60),
        min_interval_low_load=config.get("vl_min_interval_low_load_seconds", 180),
        persist_batch_threshold=config.get("vl_persist_batch_threshold", _DEFAULT_PERSIST_BATCH_THRESHOLD),
    )
