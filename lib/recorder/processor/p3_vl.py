"""P3 VL 标注器（接口定义，不实现）

spec-l1.md 第 86-90 行：
    按需调用接口（调 understand_image MCP 工具）
    L4 agent 消费时调用，L1 不实时跑 VL
    L1 SDD 只定义接口签名，不实现

07-agent-consumption.md 第一节核心原则：
    "vl和stt都不应该实时，因为它们很慢。截图考虑采集去重后不处理，
     等agent开始理解过程时按需说明关注焦点再vl，
     因为直接给vl模型一张图，模型很可能不知道需要描述什么"

L1 SDD 范围：仅定义接口签名 + Protocol，不实现具体 VL 调用。
后续 L4 agent 按需调用时，遵守本接口契约。

调用时机（未来）：
- L1 不实时跑 P3（spec-l1.md 明确"L1 不走 VL/UIA 等慢操作"）
- L4 agent 消费录制包时，根据全局认知选关键帧 + 带问题调 VL（07-agent-consumption.md Round 2）
- 多轮 VL 协议：Round 1 建立全局认知 → Round 2 选关键帧 + 带问题 → Round 3 收敛
- 调用走现有 understand_image MCP 工具（07-agent-consumption.md 第三节"走现有端点"）

带问题看图（必须）：
    每次 VL 调用必须带 question 参数，明确告诉 VL 要看什么。
    "直接给vl模型一张图，模型很可能不知道需要描述什么，图像的东西很多"

agent 原生 VL 支持：
    录制包里的截图就是普通 PNG 文件，agent 如果原生支持 VL（如 GPT-4V），
    可以直接读图片文件看图，跳过 understand_image 端点。
    "录制包不做 VL 预处理，不预存 VL 结果，agent 按需调用或直接看图"
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class VLAnnotator(Protocol):
    """P3 VL 标注器接口契约（L1 SDD 只定义不实现）。

    实现方需遵守：
    1. 接收截图路径 + 问题（必须），调用 understand_image 或类似 VL 服务
    2. 返回结构化标注（文字描述 + 可选 bbox 坐标）
    3. 不抛异常（失败返回 None 或降级描述）
    4. 不预存结果（07-agent-consumption.md "录制包不做 VL 预处理"）

    实现参考：后端 server/vision.py 的 understand_image MCP 工具
    （带 question 参数，返回 ≤200 字画面描述）
    """

    def describe(
        self,
        image_path: Path,
        question: str,
        *,
        max_chars: int = 200,
    ) -> str | None:
        """对指定截图带问题调 VL 标注。

        Args:
            image_path: 截图文件路径（录制包 frames/ 下的 PNG 文件）
            question: 必须带的问题（"登录按钮在哪里？是否可点击？"），不能为空
            max_chars: 返回描述最大字符数，默认 200（与 understand_image 一致）

        Returns:
            VL 标注文字（≤max_chars），调用失败时返回 None

        Raises:
            不应抛异常（实现方内部 try/except 兜底，失败返回 None）

        调用约束：
        - question 必须非空（07-agent-consumption.md "带问题看图"）
        - 同一图片不同问题可调多次（多轮 VL 协议）
        - 不缓存结果（每次调用都重新跑 VL，便于换模型重试）
        """
        ...


def run_p3(
    image_path: Path,
    question: str,
    *,
    annotator: VLAnnotator | None = None,
    max_chars: int = 200,
) -> str | None:
    """P3 入口（L1 SDD 占位，未实现）。

    Args:
        image_path: 截图文件路径
        question: 必须带的问题
        annotator: VL 标注器实例，None 时返回 None（L1 不提供默认实现）
        max_chars: 返回描述最大字符数

    Returns:
        VL 标注文字，未实现时返回 None

    Raises:
        ValueError: question 为空时（"带问题看图"是硬约束）
    """
    if not question or not question.strip():
        raise ValueError(
            "P3 VL 调用必须带 question 参数（07-agent-consumption.md 带问题看图原则）"
        )
    if annotator is None:
        # L1 SDD 不提供默认实现（spec-l1.md Out of Scope：P3 VL 实现）
        # L4 agent 消费时自行注入 annotator（封装 understand_image MCP 调用）
        # 或直接用 agent 原生 VL 能力读图（跳过本接口）
        return None
    return annotator.describe(image_path, question, max_chars=max_chars)
