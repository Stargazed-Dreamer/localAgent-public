"""v6-lite-streaming-gui T04: DoomLoopDetector — thinking_delta 尾重复检测。

设计依据（spec D10/D11）：
- 检测 thinking_delta 流的尾重复（LLM 卡在重复推理循环中）
- 检测窗口：最近 N 字符（tail_size，默认 2000）
- 命中条件：tail 末尾有长度 ≥ min_repeat_len 的 pattern 连续重复 ≥ repeat_threshold 次
- 命中后：mid-stream abort + retry budget disarm + backoff + 重新请求（注入"避免重复"提示）
- 最多重试 3 次，连续命中写 error event 终止

算法（D10）：滑动窗口 O(n²)，tail_size=2000 可接受。
对于 L 从 max_L 到 min_repeat_len，取末尾 L 字符作为 pattern，检查 tail 是否以 pattern * N 结尾。

不扩展到 text_delta / 工具指纹（Anti-Cheat 第 8 条，用户明确选仅 thinking_delta）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("localagent.agent.doom_loop")


@dataclass(frozen=True)
class DoomLoopConfig:
    """DoomLoop 检测配置（spec D10）。

    Attributes:
        enabled: 总开关，False 时不检测
        tail_size: 检测窗口大小（最近 N 字符），默认 2000
        min_repeat_len: 最小重复单元长度，默认 50
        repeat_threshold: 连续重复次数阈值，默认 3
        max_retries: DoomLoop 命中后最多重试次数，默认 3（spec D11）
        backoff_base_ms: 基础退避毫秒，默认 200（spec D11: 200ms + jitter）
        backoff_jitter_ms: 退避抖动毫秒，默认 300（spec D11: random(0, 300ms)）
    """
    enabled: bool = True
    tail_size: int = 2000
    min_repeat_len: int = 50
    repeat_threshold: int = 3
    max_retries: int = 3
    backoff_base_ms: int = 200
    backoff_jitter_ms: int = 300


# DoomLoop 命中时 LLMResponse 的 stop_reason 标记
# runner 主循环检查此 stop_reason 走 DoomLoop retry 路径
DOOM_LOOP_STOP_REASON = "doom_loop"

# 注入给 LLM 的"避免重复"系统提示（spec D11 第 4 点）
DOOM_LOOP_AVOID_REPETITION_PROMPT = (
    "注意：检测到你的 thinking 出现重复循环。请换一个思路，避免重复相同的推理路径。"
)


class DoomLoopDetector:
    """检测 thinking_delta 尾重复（spec D10）。

    用法：
        detector = DoomLoopDetector(config)
        for delta in thinking_deltas:
            if detector.on_thinking_delta(delta):
                # 命中死循环，abort + retry
                break
        detector.reset()  # 新 LLM 调用前重置

    线程安全：非线程安全（单 session 单线程使用）。
    """

    def __init__(self, config: DoomLoopConfig):
        self.config = config
        self.tail_window: str = ""

    def on_thinking_delta(self, delta: str) -> bool:
        """处理 thinking_delta 增量，返回 True 表示检测到死循环。

        Args:
            delta: thinking_delta 事件的增量文本

        Returns:
            True 如果检测到尾重复（应 abort + retry），False 继续
        """
        if not self.config.enabled:
            return False
        if not delta:
            return False

        self.tail_window = (self.tail_window + delta)[-self.config.tail_size:]
        return self._detect_tail_repetition()

    def _detect_tail_repetition(self) -> bool:
        """检测 tail_window 末尾是否有 pattern * N 模式。

        算法：对于 L 从 max_L 到 min_repeat_len，取末尾 L 字符作为 pattern，
        检查 tail 是否以 pattern * repeat_threshold 结尾。
        从大 L 开始尝试，找到第一个匹配就返回（找到最大的重复单元）。

        Returns:
            True 如果检测到尾重复
        """
        tail = self.tail_window
        n = len(tail)
        min_len = self.config.min_repeat_len
        threshold = self.config.repeat_threshold

        # tail 长度不足以形成 pattern * threshold
        if n < min_len * threshold:
            return False

        max_L = n // threshold
        for L in range(max_L, min_len - 1, -1):
            pattern = tail[-L:]
            repeated = pattern * threshold
            if tail.endswith(repeated):
                logger.warning(
                    "DoomLoop detected: pattern_len=%d, repeat_count=%d, tail_len=%d",
                    L, threshold, n,
                )
                return True
        return False

    def reset(self) -> None:
        """重置检测器（每次新 LLM 调用前调用）。"""
        self.tail_window = ""
