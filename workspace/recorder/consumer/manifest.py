"""C1 manifest.json 生成与读取（L4 录制包总索引）。

manifest.json 是 L4 新增的总索引文件，扫描录制包实际文件生成，
是 meta.json 的超集（含 files + stats + stt_status）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# L4 录制包格式版本号（L0-L3 最终化版本）
FORMAT_VERSION = "1.0"

# 已知的单文件（相对于录制包根目录）
_KNOWN_FILES = [
    "meta.json",
    "events.jsonl",
    "focus.jsonl",
    "timeline.json",
    "annotations.json",
    "transcript.json",
    "blocks.json",
    "keyframes.json",
    "audio/mic.wav",
    "audio/mic_cropped.wav",
    "audio/audio_segments.json",
]


def regenerate_manifest(package_path: Path | str) -> dict[str, Any]:
    """重新生成 manifest.json（扫描所有文件 + 统计 + 写文件）。

    Args:
        package_path: 录制包根目录

    Returns:
        manifest dict

    Raises:
        FileNotFoundError: 录制包目录不存在
    """
    package_path = Path(package_path)
    if not package_path.is_dir():
        raise FileNotFoundError(f"录制包目录不存在: {package_path}")

    # 读 meta.json
    meta = _read_json(package_path / "meta.json") or {}

    # 给 meta.json 加 format_version 字段（如果不存在，不改 L0 代码）
    if "format_version" not in meta:
        meta["format_version"] = FORMAT_VERSION
        _write_json(package_path / "meta.json", meta)

    # 扫描文件
    files = _scan_files(package_path)

    # 统计
    stats = _compute_stats(package_path)

    # 读 annotations.json finalized 标记
    annotations = _read_json(package_path / "annotations.json") or {}
    finalized = annotations.get("finalized", False)

    # 读 transcript.json stt 信息
    # transcript.json 是 segment 列表（L1 P1 产出），不是 dict，无 model 字段
    transcript = _read_json(package_path / "transcript.json")
    stt_status = "completed" if transcript is not None else "pending"
    stt_model = transcript.get("model") if isinstance(transcript, dict) else None

    # 构建 manifest
    manifest: dict[str, Any] = {
        "recording_id": meta.get("package_name", package_path.name),
        "format_version": meta.get("format_version", FORMAT_VERSION),
        "created_at": _meta_to_iso(meta),
        "duration_seconds": float(
            meta.get("effective_duration", meta.get("duration_seconds", 0.0))
        ),
        "finalized": finalized,
        "files": files,
        "stats": stats,
        "stt_status": stt_status,
        "stt_model": stt_model,
    }

    # 写 manifest.json
    _write_json(package_path / "manifest.json", manifest)
    return manifest


def read_manifest(package_path: Path | str) -> dict[str, Any] | None:
    """读 manifest.json，不存在返回 None。"""
    manifest_path = Path(package_path) / "manifest.json"
    if not manifest_path.exists():
        return None
    return _read_json(manifest_path)


def _scan_files(package_path: Path) -> dict[str, dict[str, Any]]:
    """扫描录制包所有文件，返回文件信息字典。"""
    files: dict[str, dict[str, Any]] = {}

    # 单文件
    for rel_path in _KNOWN_FILES:
        full_path = package_path / rel_path
        if full_path.exists():
            files[rel_path] = {"size": full_path.stat().st_size, "exists": True}

    # frames/ 目录
    frames_dir = package_path / "frames"
    if frames_dir.is_dir():
        frame_files = list(frames_dir.glob("*.png"))
        total_size = sum(f.stat().st_size for f in frame_files)
        files["frames/"] = {"count": len(frame_files), "size": total_size}

    # uia_snapshots/ 目录
    uia_dir = package_path / "uia_snapshots"
    if uia_dir.is_dir():
        uia_files = list(uia_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in uia_files)
        files["uia_snapshots/"] = {"count": len(uia_files), "size": total_size}

    return files


def _compute_stats(package_path: Path) -> dict[str, int]:
    """统计 block_count/frame_count/chapter_count/marked_*_count/trimmed_count。

    从 timeline.json 读块统计（不 merge，直接读原始 timeline，因为 manifest
    是录制包状态的快照，不是 agent 消费视图）。
    """
    stats = {
        "block_count": 0,
        "frame_count": 0,
        "chapter_count": 0,
        "marked_key_count": 0,
        "marked_anomaly_count": 0,
        "marked_automatable_count": 0,
        "trimmed_count": 0,
    }

    # 从 timeline.json 读块统计
    timeline = _read_json(package_path / "timeline.json")
    if timeline:
        blocks = timeline.get("blocks", [])
        stats["block_count"] = len(blocks)
        for block in blocks:
            if block.get("type") == "chapter":
                stats["chapter_count"] += 1
            status = block.get("status", {})
            if status.get("marked_key"):
                stats["marked_key_count"] += 1
            if status.get("marked_anomaly"):
                stats["marked_anomaly_count"] += 1
            if status.get("marked_automatable"):
                stats["marked_automatable_count"] += 1
            if status.get("is_trimmed"):
                stats["trimmed_count"] += 1

    # frame_count 从 frames/ 目录扫描
    frames_dir = package_path / "frames"
    if frames_dir.is_dir():
        stats["frame_count"] = len(list(frames_dir.glob("*.png")))

    return stats


def _meta_to_iso(meta: dict[str, Any]) -> str:
    """从 meta.json 的 start_time 转为 ISO 8601 时间戳。"""
    start_time = meta.get("start_time")
    if start_time is None:
        return ""
    try:
        dt = datetime.fromtimestamp(float(start_time), tz=UTC).astimezone()
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        return ""


def _read_json(path: Path) -> dict[str, Any] | None:
    """读 JSON 文件，不存在/解析失败返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """写 JSON 文件（ensure_ascii=False + indent=2）。"""
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
