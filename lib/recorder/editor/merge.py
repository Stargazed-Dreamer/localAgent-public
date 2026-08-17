"""E4 merge 逻辑：merge_timeline_annotations（L4 agent 核心入口）。

读 timeline.json + annotations.json，apply 所有非 undone 的 annotation，返回 merge 后的视图。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib.recorder.editor.annotation import Annotation
from lib.recorder.editor.apply import apply_annotation


def merge_timeline_annotations(package_path: Path) -> dict[str, Any]:
    """merge timeline.json + annotations.json，返回 L4 agent 读的最终视图。

    Args:
        package_path: 录制包根目录

    Returns:
        dict: {
            "finalized": bool,
            "recording_id": str,
            "duration_seconds": float,
            "block_count": int,
            "tracks": list[str],
            "blocks": list[dict],  # merge 后的块列表
        }
    """
    timeline_path = package_path / "timeline.json"
    annotations_path = package_path / "annotations.json"

    # 读 timeline.json
    if not timeline_path.exists():
        return {
            "finalized": False,
            "recording_id": "",
            "duration_seconds": 0.0,
            "block_count": 0,
            "tracks": [],
            "blocks": [],
        }
    with timeline_path.open(encoding="utf-8") as f:
        timeline = json.load(f)

    # 读 annotations.json
    finalized = False
    annotations: list[Annotation] = []
    if annotations_path.exists():
        with annotations_path.open(encoding="utf-8") as f:
            ann_data = json.load(f)
        finalized = ann_data.get("finalized", False)
        annotations = [Annotation.from_dict(a) for a in ann_data.get("annotations", [])]

    # apply 所有非 undone 的 annotation（按顺序）
    blocks = timeline.get("blocks", [])
    for ann in annotations:
        if not ann.undone:
            blocks = apply_annotation(blocks, ann)

    # 过滤掉被合并/移除的块（is_trimmed=True 的块不出现在最终视图）
    # 注意：trim 是"软删除"，merge 视图默认排除 trimmed 块；L4 agent 如需查看可读原始 timeline
    merged_blocks = [b for b in blocks if not b.get("status", {}).get("is_trimmed", False)]

    return {
        "finalized": finalized,
        "recording_id": timeline.get("recording_id", ""),
        "duration_seconds": timeline.get("duration_seconds", 0.0),
        "block_count": len(merged_blocks),
        "tracks": timeline.get("tracks", []),
        "blocks": merged_blocks,
    }


def is_finalized(package_path: Path) -> bool:
    """快速检查录制包是否已标记就绪（D053）。"""
    annotations_path = package_path / "annotations.json"
    if not annotations_path.exists():
        return False
    with annotations_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("finalized", False)
