"""L4 消费层公用库（lib/recorder/consumer/）。

封装 agent 读录制包的便捷 API：

- discovery: list_recordings / get_recording_summary  # 发现录制包
- reader: get_merged_view / get_block_detail / get_chapters  # 读内容
- vl_helper: select_vl_candidates / build_vl_question  # 多轮 VL 协议辅助
- manifest: regenerate_manifest / read_manifest  # 总索引
- log: log_consumption / read_consumption_log  # 消费日志
- types: RecordingSummary / VLCandidate  # 数据结构

典型用法：

    from workspace.recorder.consumer import (
        list_recordings, get_merged_view, select_vl_candidates,
        build_vl_question, log_consumption,
    )

    # 1. 发现所有录制包
    summaries = list_recordings()
    for s in summaries:
        print(s.recording_id, s.duration_seconds, s.finalized)

    # 2. 读某个录制包的 merge 后视图
    view = get_merged_view("workspace/recorder/recordings/rec_20260723_141608")

    # 3. 选 VL 候选帧（focus="anomaly" 优先看异常块）
    candidates = select_vl_candidates(
        view, focus="anomaly",
        package_path="workspace/recorder/recordings/rec_20260723_141608",
    )
    for cand in candidates:
        print(cand.block_id, cand.frame_path, cand.suggested_question)
        # 调 VL 后记录消费日志
        log_consumption(package_path, "vl_call", {
            "block_id": cand.block_id,
            "frame": cand.frame_path,
            "question": cand.suggested_question,
        })
"""

from workspace.recorder.consumer.discovery import get_recording_summary, list_recordings
from workspace.recorder.consumer.log import log_consumption, read_consumption_log
from workspace.recorder.consumer.manifest import read_manifest, regenerate_manifest
from workspace.recorder.consumer.reader import get_block_detail, get_chapters, get_merged_view
from workspace.recorder.consumer.types import RecordingSummary, VLCandidate
from workspace.recorder.consumer.vl_helper import build_vl_question, select_vl_candidates

__all__ = [
    "RecordingSummary",
    "VLCandidate",
    "list_recordings",
    "get_recording_summary",
    "get_merged_view",
    "get_block_detail",
    "get_chapters",
    "select_vl_candidates",
    "build_vl_question",
    "log_consumption",
    "read_consumption_log",
    "regenerate_manifest",
    "read_manifest",
]
