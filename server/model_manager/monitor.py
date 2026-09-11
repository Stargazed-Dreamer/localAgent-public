"""Model Lifecycle Manager — 资源压力监控（design §6）

每资源（gpu / cpu）一个 PressureMonitor：采样 + 五态状态机。
监控是被动状态机，采样循环由 ModelLifecycleManager 驱动（避免双循环同步问题）。

实测约束（design §2）：WDDM 下 per-process VRAM 不可得，gpu 只能读整卡
memory.used；策略语义是"缓解整卡压力"，不是显存归因。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass

from server.model_manager.types import PressureState

logger = logging.getLogger("localagent.model_manager.monitor")

# 连续探测失败达到该次数后进入 PROBE_DEGRADED（保持最后保护姿态）
_PROBE_FAIL_THRESHOLD = 3


@dataclass
class ResourceMonitorConfig:
    """单资源监控参数（默认值同 design §9）"""

    resource: str                      # "gpu" | "cpu"
    enabled: bool = True
    poll_interval_sec: float = 5.0
    high_watermark: float = 0.90       # 高压阈值（占用比例）
    high_sustain: int = 3              # 连续 N 个样本越限 → REFUSING
    critical_watermark: float = 0.97   # 单样本逃生门（立即 REFUSING）
    low_watermark: float = 0.70        # 低压阈值
    low_sustain: int = 6               # 连续 N 个样本低于阈值 → ARMED


def probe_gpu_memory() -> tuple[int, int]:
    """nvidia-smi 整卡显存采样，返回 (used, total) 字节。失败抛异常。"""
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi rc={result.returncode}: {result.stderr.strip()[:200]}")
    line = (result.stdout or "").strip().splitlines()
    if not line:
        raise RuntimeError("nvidia-smi 无输出")
    used_s, total_s = (part.strip() for part in line[0].split(","))
    mib = 1024 * 1024
    return int(float(used_s)) * mib, int(float(total_s)) * mib


def probe_cpu_memory() -> tuple[int, int]:
    """psutil 内存采样，返回 (used, total) 字节。失败抛异常。"""
    import psutil

    vm = psutil.virtual_memory()
    return int(vm.total - vm.available), int(vm.total)


class PressureMonitor:
    """单资源压力状态机

    状态迁移（design §6）：
      NORMAL/ARMED → REFUSING  高压 ×high_sustain，或 ≥critical 单样本
      REFUSING     → ARMED     低压 ×low_sustain
      ARMED        → NORMAL    下次加载成功（管理器调 mark_recovered）
      逐出完成     → NORMAL    （管理器调 force_normal_after_eviction）
      PAUSED（手动）：采样继续、保护态冻结；resume 后按最新样本重评估
      PROBE_DEGRADED：连续探测失败 ≥3，保持降级前保护姿态；探测恢复后还原
    """

    def __init__(self, cfg: ResourceMonitorConfig, probe=None):
        self.cfg = cfg
        # probe 可注入（单测用）；默认按资源选择
        self._probe = probe or (probe_gpu_memory if cfg.resource == "gpu" else probe_cpu_memory)

        self.state: PressureState = PressureState.NORMAL
        self.last_used: int | None = None
        self.last_total: int | None = None
        self.samples_taken: int = 0
        self.last_transition_ts: float = time.time()
        self.events: deque[dict] = deque(maxlen=50)

        self._high_streak = 0
        self._low_streak = 0
        self._probe_fail_streak = 0
        self._degraded_posture: PressureState | None = None  # 降级前的状态
        self._pre_pause_state: PressureState | None = None

    # ---------- 采样 ----------

    async def sample(self) -> tuple[int, int] | None:
        """异步采样（阻塞探测走线程）。失败返回 None。"""
        try:
            used, total = await asyncio.to_thread(self._probe)
        except Exception as e:
            logger.debug(f"[{self.cfg.resource}] 压力探测失败: {e}")
            return None
        self.last_used, self.last_total = int(used), int(total)
        self.samples_taken += 1
        self._probe_fail_streak = 0
        return self.last_used, self.last_total

    # ---------- 状态机 ----------

    @property
    def ratio(self) -> float | None:
        if not self.last_total:
            return None
        return (self.last_used or 0) / self.last_total

    def observe(self, ratio: float) -> dict | None:
        """喂入一个新样本，推进状态机；返回迁移事件（无迁移返回 None）"""
        # 探测恢复：从 PROBE_DEGRADED 还原降级前状态，再继续常规评估
        if self.state is PressureState.PROBE_DEGRADED:
            restored = self._degraded_posture or PressureState.NORMAL
            self._degraded_posture = None
            self._high_streak = 0
            self._low_streak = 0
            event = self._transition(restored, "probe_recovered")
            # 还原后继续评估本样本（可能立刻再迁移）
            self._evaluate(ratio)
            return event

        if self.state is PressureState.PAUSED:
            return None  # 保护态冻结，只记录样本

        return self._evaluate(ratio)

    def _evaluate(self, ratio: float) -> dict | None:
        cfg = self.cfg
        if self.state in (PressureState.NORMAL, PressureState.ARMED):
            if ratio >= cfg.critical_watermark:
                self._high_streak = 0
                self._low_streak = 0
                return self._transition(PressureState.REFUSING, "critical_sample")
            if ratio >= cfg.high_watermark:
                self._high_streak += 1
                self._low_streak = 0
                if self._high_streak >= cfg.high_sustain:
                    self._high_streak = 0
                    return self._transition(PressureState.REFUSING, "high_sustain")
            else:
                self._high_streak = 0
            return None

        if self.state is PressureState.REFUSING:
            if ratio <= cfg.low_watermark:
                self._low_streak += 1
                if self._low_streak >= cfg.low_sustain:
                    self._low_streak = 0
                    return self._transition(PressureState.ARMED, "low_sustain")
            else:
                self._low_streak = 0
            return None

        return None  # NORMAL after restore 等：无动作

    def on_probe_failure(self) -> dict | None:
        """探测失败一次；连续达到阈值进入 PROBE_DEGRADED（保持最后保护姿态）"""
        self._probe_fail_streak += 1
        if self.state is PressureState.PROBE_DEGRADED:
            return None
        if self._probe_fail_streak >= _PROBE_FAIL_THRESHOLD:
            self._degraded_posture = self.state
            return self._transition(PressureState.PROBE_DEGRADED, "probe_degraded")
        return None

    def mark_recovered(self) -> dict | None:
        """ARMED → NORMAL：懒恢复加载成功（管理器在加载成功后调用）"""
        if self.state is PressureState.ARMED:
            return self._transition(PressureState.NORMAL, "reload_success")
        return None

    def force_normal_after_eviction(self) -> dict | None:
        """逐出完成后回 NORMAL（信任卸载已释放压力；仍高会由后续采样重新触发）"""
        if self.state is PressureState.REFUSING:
            self._high_streak = 0
            self._low_streak = 0
            return self._transition(PressureState.NORMAL, "eviction_complete")
        return None

    def pause(self) -> dict | None:
        if self.state is PressureState.PAUSED:
            return None
        self._pre_pause_state = self.state if self.state is not PressureState.PROBE_DEGRADED else (
            self._degraded_posture or PressureState.NORMAL
        )
        return self._transition(PressureState.PAUSED, "manual_pause")

    def resume(self) -> dict | None:
        if self.state is not PressureState.PAUSED:
            return None
        resume_to = self._pre_pause_state or PressureState.NORMAL
        self._pre_pause_state = None
        self._high_streak = 0
        self._low_streak = 0
        event = self._transition(resume_to, "manual_resume")
        # 安全：暂停期间压力可能已变化，若有最新样本立即按逃生门重评估
        ratio = self.ratio
        if ratio is not None and ratio >= self.cfg.critical_watermark:
            self._transition(PressureState.REFUSING, "critical_sample")
        return event

    def _transition(self, new_state: PressureState, cause: str) -> dict:
        old = self.state
        self.state = new_state
        self.last_transition_ts = time.time()
        event = {
            "ts": self.last_transition_ts,
            "resource": self.cfg.resource,
            "from": old.value,
            "to": new_state.value,
            "cause": cause,
            "ratio": self.ratio,
        }
        self.events.append(event)
        logger.info(
            f"[{self.cfg.resource}] 压力状态迁移 {old.value} → {new_state.value} "
            f"({cause}, ratio={self.ratio if self.ratio is None else round(self.ratio, 3)})"
        )
        return event

    # ---------- 姿态判定 ----------

    @property
    def has_first_sample(self) -> bool:
        return self.samples_taken > 0

    def admission_allowed(self) -> bool:
        """当前姿态是否放行新加载（design §7 准入）"""
        if self.state is PressureState.PAUSED:
            return True
        if self.state is PressureState.REFUSING:
            return False
        if self.state is PressureState.PROBE_DEGRADED:
            # 保持降级前的最后保护姿态
            return self._degraded_posture not in (PressureState.REFUSING,)
        return True  # NORMAL / ARMED

    def eviction_active(self) -> bool:
        """是否应电平式评估逐出：仅 REFUSING（有实时数据）；降级/暂停不主动逐出"""
        return self.state is PressureState.REFUSING

    # ---------- 报告 ----------

    def summary(self) -> dict:
        return {
            "resource": self.cfg.resource,
            "enabled": self.cfg.enabled,
            "state": self.state.value,
            "degraded_posture": self._degraded_posture.value if self._degraded_posture else None,
            "used_mb": round(self.last_used / (1024 * 1024)) if self.last_used is not None else None,
            "total_mb": round(self.last_total / (1024 * 1024)) if self.last_total is not None else None,
            "ratio": round(self.ratio, 4) if self.ratio is not None else None,
            "samples_taken": self.samples_taken,
            "last_transition_ts": self.last_transition_ts,
        }
