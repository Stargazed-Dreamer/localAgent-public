"""Model Lifecycle Manager — 管理器主体（design §4/§5.5/§7）

职责：
- 准入（Gate）：按目标资源压力态放行/拒绝模块加载
- 逐出（Evictor）：REFUSING 期间电平式评估，串行执行，线程内卸载
- 观察：轮询 is_loaded() 为模块自主加载代记 loaded_at（design §5.5）
- 手动控制：load/unload/pause/resume（端点在 T5 接入）
- 失败语义：per-model 冷却；连续 ≥3 次失败 → reload_degraded（design §7）

管理器是决策权威，模块是执行者：模块保留单例与入口，加载前调 admit()。
"""

from __future__ import annotations

import asyncio
import logging
import time

from server.model_manager.config import (
    get_model_cooldown,
    get_model_manager_config,
    is_model_evictable,
    is_restore_preload,
    set_model_override,
)
from server.model_manager.monitor import PressureMonitor
from server.model_manager.registry import ModelRegistry
from server.model_manager.types import (
    ModelDriver,
    ModelUnavailableError,
    PressureState,
)

logger = logging.getLogger("localagent.model_manager")

# 连续加载失败达到该次数 → 标记 reload_degraded（design §7，继承 ocr-vram-guard §5.6）
_RELOAD_DEGRADED_THRESHOLD = 3
# 逐出前 in_flight 排空的最长等待（秒）；卸载本身另有 unload_timeout 预算
_DRAIN_TIMEOUT_SEC = 10.0


class ModelLifecycleManager:
    """模型存活管理器（进程内单例，经 get_model_manager() 获取）"""

    def __init__(
        self,
        cfg: dict | None = None,
        registry: ModelRegistry | None = None,
        monitors: dict[str, PressureMonitor] | None = None,
    ):
        self.cfg = cfg or get_model_manager_config()
        self.registry = registry or ModelRegistry()
        if monitors is not None:
            self.monitors = monitors
        else:
            self.monitors = {
                "gpu": PressureMonitor(self.cfg["gpu"]),
                "cpu": PressureMonitor(self.cfg["cpu"]),
            }
        self.enabled = bool(self.cfg.get("enabled", True))

        # per-model 失败/冷却/降级状态
        self._cooldown_until: dict[str, float] = {}
        self._fail_count: dict[str, int] = {}
        self._reload_degraded: set[str] = set()
        # 模块自主加载的观察时间戳（design §5.5）
        self._observed_loaded_at: dict[str, float] = {}
        # 卸载失败记录（下一周期重试提示）
        self._unload_errors: dict[str, str] = {}

        self._evict_lock = asyncio.Lock()
        self._evict_busy = False
        self._loop_task: asyncio.Task | None = None
        self._stopped = False

    # ========== 生命周期 ==========

    async def start(self) -> None:
        """启动监控：先注册后监控（design §13）；启动即时采样堵失明窗口（design §6）"""
        if not self.enabled:
            logger.info("ModelLifecycleManager enabled=false，纯透传模式（不做自动监控）")
            return
        # 启动即时采样：首样本必须先于任何准入判定存在
        for monitor in self.monitors.values():
            if not monitor.cfg.enabled:
                continue
            sample = await monitor.sample()
            if sample is None:
                monitor.on_probe_failure()
            else:
                monitor.observe(monitor.ratio or 0.0)
        self._stopped = False
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info(
            f"ModelLifecycleManager 已启动: 注册模型={len(self.registry)}, "
            f"gpu={self.monitors['gpu'].state.value}, cpu={self.monitors['cpu'].state.value}"
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None

    async def _run_loop(self) -> None:
        interval = float(self.cfg.get("poll_interval_sec", 5.0))
        while not self._stopped:
            await asyncio.sleep(interval)
            try:
                await self._tick()
            except Exception as e:
                logger.warning(f"ModelLifecycleManager 采样周期异常: {e}", exc_info=True)

    async def _tick(self) -> None:
        for monitor in self.monitors.values():
            if not monitor.cfg.enabled:
                continue
            sample = await monitor.sample()
            if sample is None:
                monitor.on_probe_failure()
            else:
                monitor.observe(monitor.ratio or 0.0)
            if monitor.eviction_active():
                await self._maybe_evict(monitor)
        self._observe_autonomous_loads()

    # ========== 准入（Gate） ==========

    def check_admission(self, model_id: str, manual: bool = False) -> tuple[bool, str]:
        """查询准入结果（不抛异常）。返回 (是否放行, 原因码)。

        manual=True（手动端点）：豁免冷却与降级标记（操作者显式重试），
        但仍受压力态约束（强制放行请先 /models/pause）。
        """
        if not self.enabled:
            return True, "manager_disabled"
        driver = self.registry.get(model_id)
        if driver is None:
            return True, "unmanaged_model"
        monitor = self.monitors.get(driver.resource)
        if monitor is None or not monitor.cfg.enabled:
            return True, "monitor_disabled"
        if not monitor.has_first_sample:
            return False, "awaiting_first_sample"
        if not monitor.admission_allowed():
            if monitor.state is PressureState.PROBE_DEGRADED:
                return False, "probe_degraded"
            return False, "pressure_refusing"
        if not manual:
            if self._in_cooldown(model_id):
                return False, "cooldown"
            if model_id in self._reload_degraded:
                return False, "reload_degraded"
        return True, "ok"

    def admit(self, model_id: str, manual: bool = False) -> None:
        """准入门控：拒绝时抛 ModelUnavailableError（消费端转 503）"""
        allowed, reason = self.check_admission(model_id, manual=manual)
        if not allowed:
            raise ModelUnavailableError(model_id, reason)

    # ========== 失败语义 ==========

    def note_load_success(self, model_id: str) -> None:
        """加载成功：清失败计数/冷却/降级标记；ARMED → NORMAL（懒恢复闭环）"""
        self._fail_count.pop(model_id, None)
        self._cooldown_until.pop(model_id, None)
        self._reload_degraded.discard(model_id)
        self._unload_errors.pop(model_id, None)
        driver = self.registry.get(model_id)
        if driver is not None:
            monitor = self.monitors.get(driver.resource)
            if monitor is not None:
                monitor.mark_recovered()

    def note_load_failure(self, model_id: str, detail: str = "") -> dict:
        """加载失败：进入 per-model 冷却；连续 ≥3 次 → reload_degraded（design §7）"""
        count = self._fail_count.get(model_id, 0) + 1
        self._fail_count[model_id] = count
        cooldown = get_model_cooldown(self.cfg, model_id)
        self._cooldown_until[model_id] = time.time() + cooldown
        degraded = False
        if count >= _RELOAD_DEGRADED_THRESHOLD:
            self._reload_degraded.add(model_id)
            degraded = True
            logger.error(
                f"模型 {model_id} 连续 {count} 次加载失败，标记 reload_degraded"
                f"（建议重启后端）。最近失败: {detail}"
            )
        else:
            logger.warning(
                f"模型 {model_id} 加载失败（{count}/{_RELOAD_DEGRADED_THRESHOLD}），"
                f"冷却 {cooldown:.0f}s。detail={detail}"
            )
        return {"fail_count": count, "cooldown_sec": cooldown, "reload_degraded": degraded}

    def _in_cooldown(self, model_id: str) -> bool:
        return time.time() < self._cooldown_until.get(model_id, 0.0)

    # ========== 观察协议（design §5.5） ==========

    def effective_loaded_at(self, model_id: str) -> float | None:
        """驱动自记时间戳优先；模块自主加载时用监控首次观察值兜底"""
        driver = self.registry.get(model_id)
        if driver is not None:
            ts = driver.loaded_at()
            if ts is not None:
                return ts
        return self._observed_loaded_at.get(model_id)

    def _observe_autonomous_loads(self) -> None:
        """轮询 is_loaded()：为模块自主加载（请求路径内现载）代记/清除时间戳"""
        now = time.time()
        for driver in self.registry.all():
            try:
                loaded = driver.is_loaded()
            except Exception:
                continue
            if loaded and self.effective_loaded_at(driver.model_id) is None:
                self._observed_loaded_at[driver.model_id] = now
            elif not loaded and driver.model_id in self._observed_loaded_at:
                del self._observed_loaded_at[driver.model_id]

    # ========== 逐出（Evictor） ==========

    def is_evictable(self, driver: ModelDriver) -> bool:
        """配置覆盖优先，未配置用驱动默认值（embedding 默认钉住）"""
        return is_model_evictable(self.cfg, driver.model_id, driver.evictable)

    def _select_victim(self, resource: str) -> ModelDriver | None:
        """逐出选择：已加载、可逐出、过保护期、不在冷却；
        按 (priority 升, footprint 降, reload_cost 升) 取最小（design §7）"""
        now = time.time()
        min_loaded = float(self.cfg.get(f"{resource}_min_loaded_seconds", 60))
        candidates = []
        for driver in self.registry.of_resource(resource):
            if not self.is_evictable(driver):
                continue
            try:
                if not driver.is_loaded():
                    continue
            except Exception:
                continue
            loaded_at = self.effective_loaded_at(driver.model_id)
            if loaded_at is not None and (now - loaded_at) < min_loaded:
                continue
            if self._in_cooldown(driver.model_id):
                continue
            candidates.append(driver)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda d: (d.priority, -d.footprint_mb, d.reload_cost_sec),
        )

    async def _maybe_evict(self, monitor: PressureMonitor) -> None:
        """电平式触发：REFUSING 期间每周期评估；执行串行、异步不阻塞采样"""
        if self._evict_busy:
            return
        victim = self._select_victim(monitor.cfg.resource)
        if victim is None:
            # 无可选 victim（全钉住/保护期/冷却）→ 维持 REFUSING 仅拒绝（design §7）
            return
        self._evict_busy = True
        asyncio.create_task(self._evict_one(victim, monitor))

    async def _evict_one(self, driver: ModelDriver, monitor: PressureMonitor) -> None:
        try:
            async with self._evict_lock:
                unload_timeout = float(
                    self.cfg.get(f"{driver.resource}_unload_timeout_sec", 15)
                )
                logger.info(
                    f"逐出模型 {driver.model_id}（resource={driver.resource}, "
                    f"priority={driver.priority}, footprint≈{driver.footprint_mb}MB）"
                )
                # 排空 in_flight（通用兜底；驱动内部可有更精细排空）
                drain_deadline = time.time() + _DRAIN_TIMEOUT_SEC
                while driver.in_flight() > 0 and time.time() < drain_deadline:
                    await asyncio.sleep(0.5)
                if driver.in_flight() > 0:
                    logger.warning(
                        f"模型 {driver.model_id} 排空超时（{driver.in_flight()} 个在途），仍尝试卸载"
                    )
                # 卸载必须走线程（严禁事件循环线程同步卸载，design §7）
                await asyncio.wait_for(
                    asyncio.to_thread(driver.unload), timeout=unload_timeout
                )
                self._unload_errors.pop(driver.model_id, None)
                monitor.force_normal_after_eviction()
                logger.info(f"模型 {driver.model_id} 逐出完成")
        except TimeoutError:
            self._unload_errors[driver.model_id] = "unload_timeout"
            logger.error(f"模型 {driver.model_id} 卸载超时，下一周期重试")
        except Exception as e:
            self._unload_errors[driver.model_id] = str(e)[:200]
            logger.error(f"模型 {driver.model_id} 卸载失败: {e}")
        finally:
            self._evict_busy = False

    # ========== 手动控制（端点 T5 接入） ==========

    async def manual_load(self, model_id: str) -> dict:
        """手动加载：受压力态约束，豁免冷却/降级（操作者显式重试）"""
        driver = self.registry.get(model_id)
        if driver is None:
            return {"status": "error", "detail": f"未知模型: {model_id}"}
        allowed, reason = self.check_admission(model_id, manual=True)
        if not allowed:
            return {"status": "refused", "reason": reason}
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(driver.load)
        except Exception as e:  # LoadFailureError 亦为 Exception 子类
            info = self.note_load_failure(model_id, str(e)[:200])
            return {"status": "error", "detail": str(e)[:200], **info}
        self.note_load_success(model_id)
        return {
            "status": "ok",
            "load_ms": int((time.perf_counter() - t0) * 1000),
        }

    async def manual_unload(self, model_id: str) -> dict:
        """手动卸载：不走压力门控（操作者显式动作）；仍走线程与超时保护"""
        driver = self.registry.get(model_id)
        if driver is None:
            return {"status": "error", "detail": f"未知模型: {model_id}"}
        if not driver.is_loaded():
            return {"status": "ok", "detail": "already_unloaded"}
        unload_timeout = float(self.cfg.get(f"{driver.resource}_unload_timeout_sec", 15))
        try:
            await asyncio.wait_for(
                asyncio.to_thread(driver.unload), timeout=unload_timeout + _DRAIN_TIMEOUT_SEC
            )
        except TimeoutError:
            self._unload_errors[model_id] = "unload_timeout"
            return {"status": "error", "detail": "unload_timeout"}
        except Exception as e:
            self._unload_errors[model_id] = str(e)[:200]
            return {"status": "error", "detail": str(e)[:200]}
        self._unload_errors.pop(model_id, None)
        return {"status": "ok"}

    def pause(self, resource: str | None = None) -> list[dict]:
        """手动超驰：暂停守护（准入放行且暂停逐出）。resource=None → 全部"""
        events = []
        for res, monitor in self.monitors.items():
            if resource is not None and res != resource:
                continue
            if not monitor.cfg.enabled:
                continue
            ev = monitor.pause()
            if ev:
                events.append(ev)
        return events

    def resume(self, resource: str | None = None) -> list[dict]:
        events = []
        for res, monitor in self.monitors.items():
            if resource is not None and res != resource:
                continue
            if not monitor.cfg.enabled:
                continue
            ev = monitor.resume()
            if ev:
                events.append(ev)
        return events

    # ========== 报告 ==========

    def models_report(self) -> list[dict]:
        out = []
        for driver in self.registry.all():
            try:
                loaded = driver.is_loaded()
                in_flight = driver.in_flight()
            except Exception:
                loaded, in_flight = False, 0
            out.append({
                "model_id": driver.model_id,
                "resource": driver.resource,
                "loaded": loaded,
                "footprint_mb": driver.footprint_mb,
                "priority": driver.priority,
                "evictable": self.is_evictable(driver),
                "in_flight": in_flight,
                "loaded_at": self.effective_loaded_at(driver.model_id),
                "last_load_ms": driver.load_duration_ms(),
                "reload_degraded": driver.model_id in self._reload_degraded,
                "cooldown_remaining_sec": max(
                    0.0, round(self._cooldown_until.get(driver.model_id, 0.0) - time.time(), 1)
                ) or None,
                "unload_error": self._unload_errors.get(driver.model_id),
            })
        return out

    def pressure_report(self) -> dict:
        return {
            res: {
                **monitor.summary(),
                "recent_events": list(monitor.events)[-10:],
            }
            for res, monitor in self.monitors.items()
        }

    def health_summary(self) -> dict:
        """/health.model_manager 低频枚举（design §10，不放高频计数器）"""
        transitions = [
            m.last_transition_ts for m in self.monitors.values() if m.cfg.enabled
        ]
        return {
            "enabled": self.enabled,
            "gpu_state": self.monitors["gpu"].state.value if "gpu" in self.monitors else "disabled",
            "cpu_state": self.monitors["cpu"].state.value if "cpu" in self.monitors else "disabled",
            "loaded_ids": sorted(
                d.model_id for d in self.registry.all() if _safe_is_loaded(d)
            ),
            "reload_degraded_ids": sorted(self._reload_degraded),
            "last_transition_ts": max(transitions) if transitions else None,
        }

    # ========== keep_models 写回链（design §9，D4 强制）==========

    def set_keep_models(self, model_id: str, keep: bool) -> None:
        """keep_models 写回入口（design §9）

        把模块的 keep_models 旋钮翻译为管理器 per-model 配置：
        - keep=True  → restore_preload=True（驱逐后压力回落时自动重载，常驻倾向）
        - keep=False → restore_preload=False（不主动重载，等下次真实请求触发懒加载）

        不修改 evictable：OCR/MindForge 默认 evictable=True 仍可被策略逐出；
        keep_models 只控制"逐出后是否主动恢复"，不改变"能否被逐出"。
        embedding 默认 evictable=False 不受此方法影响（仍需手动卸载）。
        """
        set_model_override(self.cfg, model_id, "restore_preload", bool(keep))
        logger.info(
            f"keep_models 写回: model={model_id} keep={keep} "
            f"→ restore_preload={'true' if keep else 'false'}"
        )

    def keep_models(self, model_id: str) -> bool:
        """keep_models 读出口（供 /health.ocr、/ocr/status、GUI 等消费者回读生效值）"""
        return is_restore_preload(self.cfg, model_id, default=True)


def _safe_is_loaded(driver: ModelDriver) -> bool:
    try:
        return driver.is_loaded()
    except Exception:
        return False
