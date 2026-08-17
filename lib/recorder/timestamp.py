"""L0 采集层时间戳服务

时间戳方案（D025）：
- 录制开始时记录 time.time() 作为绝对基准 base_abs
- 所有事件用 time.monotonic() 偏移（从录制开始算）
- monotonic 不受 NTP 调整影响，适合事件相对时序
- abs_timestamp = base_abs + monotonic_offset，用于跨录制包对齐和展示

L0 落盘时同时写 timestamp（偏移）和 abs_timestamp（绝对时间），
L1+ 时间轴层可任选基准对齐多路素材。
"""

import time


class TimestampService:
    """时间戳服务：所有传感器共享同一基准。

    在 RecordingController.start() 时构造，stop() 后失效。
    线程安全：time.time() / time.monotonic() 本身线程安全，无需加锁。
    """

    def __init__(self) -> None:
        self._base_abs: float = time.time()
        self._base_mono: float = time.monotonic()

    @property
    def base_abs(self) -> float:
        """录制开始的绝对时间戳（time.time()），用于 meta.json"""
        return self._base_abs

    @property
    def base_mono(self) -> float:
        """录制开始的 monotonic 时间戳，用于内部计算"""
        return self._base_mono

    def now(self) -> tuple[float, float]:
        """获取当前时间戳。

        Returns:
            (monotonic_offset, abs_time) 二元组
            - monotonic_offset: 从录制开始算的秒数（float）
            - abs_time: 当前 time.time() 绝对时间（float）
        """
        mono_offset = time.monotonic() - self._base_mono
        abs_time = self._base_abs + mono_offset
        return mono_offset, abs_time
