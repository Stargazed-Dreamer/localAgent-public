"""L1 处理层 pipeline（spec-l1.md seam）

process_recording_package：L1 处理器入口。
输入录制包目录 + 选项字典，编排 P1/P4/P5 产出 blocks.json + keyframes.json + transcript.json。

选项字典（options）：
    run_p4 (bool, default True)：是否跑 P4 事件聚合
    run_p5 (bool, default True)：是否跑 P5 关键帧抽取
    run_p1 (bool, default False)：是否跑 P1 STT（默认关，需用户手动触发）
    p4_idle_threshold (float, default 2.0)：P4 idle 块阈值（秒）
    p4_before_frame_window (float, default 0.2)：P4 前截图窗口（秒）
    p4_after_frame_window (float, default 0.3)：P4 后截图窗口（秒）
    p5_phash_threshold (int, default 5)：P5 全局 pHash 阈值
    p1_vad_threshold (float, default 0.5)：P1 VAD 阈值
    p1_language (str, default "zh")：P1 语言
    p1_model (str, default "large-v3")：P1 STT 模型名
    p1_model_dir (str, default None)：P1 模型存放目录
    p1_transcriber (WhisperTool, default None)：已加载模型的 WhisperTool 实例（避免重复加载）

L1 不走后端 API（延续 D018），L3 编辑器启动时自动跑 P4/P5，
P1 STT 等用户配好 VAD 参数后手动触发。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib.recorder.processor.p4_event_aggregator import (
    DEFAULT_AFTER_FRAME_WINDOW,
    DEFAULT_BEFORE_FRAME_WINDOW,
    DEFAULT_IDLE_THRESHOLD,
    run_p4,
)


@dataclass
class ProcessingResult:
    """L1 处理结果。

    Attributes:
        package_root: 录制包根目录
        blocks_path: blocks.json 路径（P4 产出），未跑时为 None
        keyframes_path: keyframes.json 路径（P5 产出），未跑时为 None
        transcript_path: transcript.json 路径（P1 产出），未跑时为 None
        block_count: 操作块数量
        keyframe_count: 关键帧数量
        transcript_segment_count: 转写段数
        errors: 各阶段错误信息（key=阶段名，value=错误描述）
    """

    package_root: Path
    blocks_path: Path | None = None
    keyframes_path: Path | None = None
    transcript_path: Path | None = None
    block_count: int = 0
    keyframe_count: int = 0
    transcript_segment_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)


# 默认选项
_DEFAULT_OPTIONS: dict[str, Any] = {
    "run_p4": True,
    "run_p5": True,
    "run_p1": False,
    "p4_idle_threshold": DEFAULT_IDLE_THRESHOLD,
    "p4_before_frame_window": DEFAULT_BEFORE_FRAME_WINDOW,
    "p4_after_frame_window": DEFAULT_AFTER_FRAME_WINDOW,
    "p5_phash_threshold": 5,
    "p1_vad_threshold": 0.5,
    "p1_language": "zh",
    "p1_model": "large-v3",
    "p1_model_dir": None,
    "p1_transcriber": None,
    "p1_device": None,  # None=默认 cuda，可设 "cpu" 在显存不足时降级
}


def process_recording_package(
    package_path: Path | str,
    options: dict | None = None,
) -> ProcessingResult:
    """L1 处理器入口：编排 P1/P4/P5 处理录制包。

    Args:
        package_path: 录制包根目录路径
        options: 选项字典（见模块 docstring），None 用默认值

    Returns:
        ProcessingResult 处理结果
    """
    package_root = Path(package_path)
    opts = {**_DEFAULT_OPTIONS, **(options or {})}

    result = ProcessingResult(package_root=package_root)

    # ========== P4 事件聚合 ==========
    if opts["run_p4"]:
        try:
            blocks = run_p4(
                package_root,
                idle_threshold=opts["p4_idle_threshold"],
                before_frame_window=opts["p4_before_frame_window"],
                after_frame_window=opts["p4_after_frame_window"],
            )
            result.blocks_path = package_root / "blocks.json"
            result.block_count = len(blocks)
        except Exception as e:
            result.errors["p4"] = f"{type(e).__name__}: {e}"

    # ========== P5 关键帧抽取 ==========
    if opts["run_p5"]:
        try:
            from lib.recorder.processor.p5_keyframe_extractor import run_p5

            keyframes = run_p5(
                package_root,
                phash_threshold=opts["p5_phash_threshold"],
            )
            result.keyframes_path = package_root / "keyframes.json"
            result.keyframe_count = len(keyframes)
        except ImportError:
            # p5_keyframe_extractor.py 尚未实现（Ticket 16）→ 跳过
            result.errors["p5"] = "not_implemented"
        except Exception as e:
            result.errors["p5"] = f"{type(e).__name__}: {e}"

    # ========== P1 STT ==========
    if opts["run_p1"]:
        try:
            from lib.recorder.processor.p1_stt import run_p1

            transcript = run_p1(
                package_root,
                vad_threshold=opts["p1_vad_threshold"],
                language=opts["p1_language"],
                model_name=opts["p1_model"],
                model_dir=opts["p1_model_dir"],
                device=opts.get("p1_device"),
                transcriber=opts["p1_transcriber"],
            )
            result.transcript_path = package_root / "transcript.json"
            result.transcript_segment_count = len(transcript)
        except ImportError:
            # p1_stt.py 尚未实现（Ticket 17）→ 跳过
            result.errors["p1"] = "not_implemented"
        except Exception as e:
            result.errors["p1"] = f"{type(e).__name__}: {e}"

    return result
