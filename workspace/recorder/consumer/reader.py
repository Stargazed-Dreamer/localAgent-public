"""C2 reader：get_merged_view / get_block_detail / get_chapters。

agent 深入读录制包内容的入口。get_merged_view 调 L3 的
merge_timeline_annotations 获取最终视图（apply 所有非 undone annotation）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib.recorder.editor.merge import merge_timeline_annotations


def get_merged_view(package_path: Path | str) -> dict[str, Any]:
    """读 timeline + annotations merge 后的最终视图。

    调 L3 的 merge_timeline_annotations（apply 所有非 undone annotation +
    过滤 trimmed 块）。

    Args:
        package_path: 录制包根目录

    Returns:
        dict: {
            "finalized": bool,
            "recording_id": str,
            "duration_seconds": float,
            "block_count": int,
            "tracks": list[str],
            "blocks": list[dict],  # merge 后的块列表（排除 trimmed）
        }
    """
    return merge_timeline_annotations(Path(package_path))


def get_block_detail(package_path: Path | str, block_id: str) -> dict[str, Any] | None:
    """单个块详情，含 supplements 路径解析（相对路径 → 绝对路径）。

    Args:
        package_path: 录制包根目录
        block_id: 块 ID（如 b001/c001/f001/t001）

    Returns:
        块 dict（supplements 中的 frame_path/uia_snapshot 已解析为绝对路径），
        找不到块返回 None
    """
    package_path = Path(package_path)
    merged = get_merged_view(package_path)
    for block in merged.get("blocks", []):
        if block.get("id") == block_id:
            return _resolve_supplement_paths(block, package_path)
    return None


def get_chapters(package_path: Path | str) -> list[dict[str, Any]]:
    """章节列表（从 merged view 过滤 chapter 块）。

    Args:
        package_path: 录制包根目录

    Returns:
        chapter 块列表（按时间戳排序）
    """
    merged = get_merged_view(package_path)
    return [b for b in merged.get("blocks", []) if b.get("type") == "chapter"]


def _resolve_supplement_paths(block: dict[str, Any], package_path: Path) -> dict[str, Any]:
    """把 block 的 supplements 中的相对路径解析为绝对路径。

    处理 before_frame / after_frame / linked_frame / uia_snapshot 等字段，
    把 "frames/xxx.png" 解析为 "/abs/path/frames/xxx.png"。

    Args:
        block: 块 dict（不会被修改）
        package_path: 录制包根目录

    Returns:
        新的 block dict（supplements 路径已解析）
    """
    block = dict(block)  # 浅拷贝，不修改原 dict
    supplements = dict(block.get("supplements", {}))
    for key, value in supplements.items():
        if not isinstance(value, str):
            continue
        # 处理 "frames/xxx.png" 和 "uia_snapshots/xxx.json" 相对路径
        if value.startswith("frames/") or value.startswith("uia_snapshots/"):
            supplements[key] = str((package_path / value).resolve())
    block["supplements"] = supplements
    return block
