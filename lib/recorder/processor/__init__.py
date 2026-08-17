"""L1 处理层（spec-l1.md）

L1 处理层入口：process_recording_package。输入录制包目录 + 选项字典，
编排 P1/P4/P5 产出 blocks.json + keyframes.json + transcript.json。

模块结构（D019 公用库分层）：
- pipeline.py：process_recording_package seam（编排 P1/P4/P5）
- p4_event_aggregator.py：P4 事件聚合器（events.jsonl → blocks.json）
- p5_keyframe_extractor.py：P5 关键帧抽取器（frames/ → keyframes.json）
- p1_stt.py：P1 STT 转换器（mic.wav → transcript.json）
- p2_uia.py：P2 UIA 结构化器接口定义（不实现）
- p3_vl.py：P3 VL 标注器接口定义（不实现）

L1 不走后端 API（延续 D018），L3 编辑器启动时自动跑 P4/P5，
P1 STT 等用户配好 VAD 参数后手动触发。
"""

from lib.recorder.processor.p2_uia import UIASnapshotter, run_p2
from lib.recorder.processor.p3_vl import VLAnnotator, run_p3
from lib.recorder.processor.pipeline import (
    ProcessingResult,
    process_recording_package,
)

__all__ = [
    "process_recording_package",
    "ProcessingResult",
    "UIASnapshotter",
    "run_p2",
    "VLAnnotator",
    "run_p3",
]
