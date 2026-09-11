"""Model Lifecycle Manager — 类型定义

设计真源：temp/sdd/model-lifecycle-manager/design.md v2（§5 协议 / §7 准入与逐出）。

本模块只放纯类型与异常，不依赖任何模块单例，方便单测直接导入。
"""

from __future__ import annotations

import enum
from typing import Literal, Protocol, runtime_checkable


class PressureState(enum.StrEnum):
    """单资源压力状态机状态（每资源独立一份）"""

    NORMAL = "normal"
    REFUSING = "refusing"        # 高压持续越限：拒绝该资源新加载 + 电平式逐出
    ARMED = "armed"              # 已确认稳定低压：等待下次真实请求懒恢复
    PAUSED = "paused"            # 手动超驰：准入放行且暂停逐出（监控继续采样）
    PROBE_DEGRADED = "probe_degraded"  # 探测失败：保持降级前的最后保护姿态


class ModelUnavailableError(Exception):
    """准入门控拒绝加载时抛出（消费端转 503）

    Attributes:
        model_id: 被拒绝的模型
        reason: 结构化原因码（"pressure_refusing" / "probe_degraded" /
                "cooldown" / "awaiting_first_sample" / "reload_degraded"）
        detail: 人类可读描述
    """

    def __init__(self, model_id: str, reason: str, detail: str = ""):
        self.model_id = model_id
        self.reason = reason
        super().__init__(detail or f"model '{model_id}' unavailable: {reason}")


class LoadFailureError(Exception):
    """驱动 load() 失败时抛出（含模块返回 False 的吞异常场景）

    管理器据此 note_load_failure()：进入 per-model 冷却，连续 ≥3 次标记
    reload_degraded（design §7）。
    """


@runtime_checkable
class ModelDriver(Protocol):
    """模型驱动协议：管理器统一接口 → 模块既有单例的适配器（design §5）

    驱动不改变模块内部实现，只翻译接口。所有方法可能在任意线程被调用：
    `load`/`unload` 由管理器在 `asyncio.to_thread` 中执行（阻塞允许），
    其余方法必须轻量无阻塞。
    """

    model_id: str                       # 全局唯一："ocr" | "memory_embedding" | ...
    footprint_mb: int                   # 估计占用（逐出权重 + /models 展示）
    priority: int                       # 越大越想保留（逐出从低到高选）
    reload_cost_sec: float              # 估计重载耗时（廉价者优先逐出）
    evictable: bool                     # True=允许策略逐出；False=钉住，仅手动卸载

    @property
    def resource(self) -> Literal["gpu", "cpu"]:
        """压力归属维度（只读协议成员：OcrDriver 用动态 property 跟随实际设备）"""
        ...

    def is_loaded(self) -> bool:
        """当前是否已加载（必须容忍目标实例尚未懒创建，返回 False）"""
        ...

    def load(self) -> None:
        """阻塞加载。失败必须抛 LoadFailureError（模块返回 False 也算失败）"""
        ...

    def unload(self) -> None:
        """阻塞卸载。必须真正释放资源（各驱动实现约束见 design §5）"""
        ...

    def in_flight(self) -> int:
        """进行中的推理数（逐前排空用；无排空能力的驱动恒 0）"""
        ...

    def loaded_at(self) -> float | None:
        """加载完成时间戳（time.time() 基准）；未知返回 None。

        模块自主加载（请求路径内现载）时，管理器监控首次观察到 is_loaded()
        为 True 即代记时间戳（design §5.5 观察协议）。
        """
        ...

    def load_duration_ms(self) -> int | None:
        """最近一次加载耗时（毫秒）；无数据来源返回 None（/models 契约可空）"""
        ...
