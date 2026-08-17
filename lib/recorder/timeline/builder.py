"""T1 时间轴引擎（spec-l2.md 第三节）

build_timeline：L2 时间轴层入口。输入录制包目录，
合并 L1 产出为统一 timeline.json（blocks 数组按时间戳排序）。

Ticket 19：T1 骨架 + 操作块合并
- 读 blocks.json → 操作块（保持原结构和 ID b001/b002...）
- 读 meta.json → recording_id + duration_seconds
- 生成 timeline.json 骨架（tracks=["operation"]）
- TimelineResult dataclass
- 缺失文件降级处理

Ticket 20：关键帧块 + 转写块 + 音频时间戳映射
- 读 keyframes.json → image/screenshot 块（f001/f002...，D040）
- 读 transcript.json → text/stt_transcript 块（t001/t002...）
- 音频时间戳映射（D039）：有 audio_segments.json 时映射，无时直接使用
- tracks 扩展为 ["operation", "image", "text"]

Ticket 21：章节块（c001...）插入
- generate_chapters 扫描操作块序列（focus_change + idle>阈值）切章节（D042）
- 章节块 category=text, type=chapter, id=c001/c002...
- 第一个章节 timestamp=0.0，章节名取第一个 focus_change 的 title 或"开始"
- 连续同名章节去重
- tracks：有章节块时独立 "chapter" track（章节块 category=text 但 type=chapter）
- chapter_count 字段反映章节数量

后续 ticket 扩展点：
- Ticket 22：annotations.json 初始化

timeline.json 格式（06-recording-format.md）：
    {
        "recording_id": "rec_20260723_143000",
        "duration_seconds": 184.5,
        "block_count": 67,
        "tracks": ["operation", "image", "text"],
        "blocks": [
            {"id": "b001", "category": "operation", "type": "focus_change", ...},
            {"id": "f001", "category": "image", "type": "screenshot", ...},
            {"id": "t001", "category": "text", "type": "stt_transcript", ...}
        ]
    }

块 ID 前缀（D041）：b=operation / f=image(frame) / t=text / c=chapter

音频时间戳映射（D039）：
- transcript.json 的 start/end 是音频相对秒数（STT 在裁剪音频上跑的时间轴）
- 如果有 audio_segments.json（D034 前的裁剪包）→ 映射到录制包时间轴
- 如果没有 audio_segments.json（D034 后 mic.wav 完整保留）→ 直接使用
- 映射方向：cropped_time → original_time
    在 segments 中找 cropped_start <= time <= cropped_end 的段，
    original_time = original_start + (cropped_time - cropped_start)
- 注：spec-l2.md D039 中的公式 `cropped_start + (audio_time - original_start)` 方向写反，
  正确方向是 original_start + (cropped_time - cropped_start)
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from lib.recorder.timeline.chapterizer import (
    DEFAULT_IDLE_THRESHOLD,
    generate_chapters,
)


@dataclass
class TimelineResult:
    """L2 时间轴构建结果。

    Attributes:
        package_root: 录制包根目录
        timeline_path: timeline.json 路径，未生成时为 None
        annotations_path: annotations.json 路径（Ticket 22 才初始化文件内容）
        block_count: timeline.json 中的块总数（含操作/图像/文本/章节四类）
        chapter_count: 章节块数量（Ticket 21 才生成，本 ticket 为 0）
        errors: 各阶段错误信息（key=阶段名，value=错误描述）
    """

    package_root: Path
    timeline_path: Path | None = None
    annotations_path: Path | None = None
    block_count: int = 0
    chapter_count: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def build_timeline(
    package_path: Path | str,
    options: dict | None = None,
) -> TimelineResult:
    """L2 时间轴层入口：合并 L1 产出为 timeline.json。

    合并三类块（操作/图像/文本）按时间戳排序，生成统一 timeline.json。
    Ticket 21 会在本函数基础上追加章节块。

    Args:
        package_path: 录制包根目录路径
        options: 选项字典（后续 ticket 扩展，Ticket 19/20 暂未使用），
            None 用默认值

    Returns:
        TimelineResult 构建结果
    """
    package_root = Path(package_path)
    result = TimelineResult(package_root=package_root)

    # 读 meta.json → recording_id + duration_seconds
    recording_id, duration_seconds = _read_meta(package_root, result)

    # 读 blocks.json → 操作块（保持原结构和 ID b001/b002...）
    operation_blocks = _read_operation_blocks(package_root, result)

    # 读 keyframes.json → image/screenshot 块（f001/f002...，D040）
    image_blocks = _build_image_blocks(package_root, result)

    # 读 transcript.json → text/stt_transcript 块（t001/t002...）+ 音频时间戳映射 D039
    text_blocks = _build_text_blocks(package_root, result)

    # 生成章节块（c001/c002...，D042）：focus_change + idle>阈值 切章节
    idle_threshold = DEFAULT_IDLE_THRESHOLD
    if isinstance(options, dict):
        idle_threshold = float(options.get("idle_threshold", idle_threshold))
    chapter_blocks = generate_chapters(operation_blocks, idle_threshold=idle_threshold)

    # 合并四类块 + 按时间戳排序
    all_blocks = operation_blocks + image_blocks + text_blocks + chapter_blocks
    all_blocks.sort(key=lambda b: float(b.get("timestamp", 0.0)))

    # tracks 根据实际有的块类型构建（D041：chapter 独立 track，虽然 category=text）
    tracks = ["operation"]
    if image_blocks:
        tracks.append("image")
    if text_blocks:
        tracks.append("text")
    if chapter_blocks:
        tracks.append("chapter")

    # 生成 timeline.json
    timeline = {
        "recording_id": recording_id,
        "duration_seconds": duration_seconds,
        "block_count": len(all_blocks),
        "tracks": tracks,
        "blocks": all_blocks,
    }

    timeline_path = package_root / "timeline.json"
    timeline_path.write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result.timeline_path = timeline_path
    result.annotations_path = package_root / "annotations.json"  # Ticket 22 才初始化文件
    result.block_count = len(all_blocks)
    result.chapter_count = len(chapter_blocks)

    return result


def _read_meta(
    package_root: Path, result: TimelineResult
) -> tuple[str, float]:
    """读 meta.json 获取 recording_id 和 duration_seconds。

    recording_id 取 meta.json 的 package_name 字段（L0 RecordingPackage.write_meta 写入）。

    Returns:
        (recording_id, duration_seconds)，缺失/出错时返回默认值 + errors 记录
    """
    meta_path = package_root / "meta.json"
    if not meta_path.exists():
        result.errors["meta"] = f"meta.json 不存在：{meta_path}"
        return "", 0.0

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        recording_id = str(meta.get("package_name", ""))
        duration_seconds = float(meta.get("duration_seconds", 0.0))
        return recording_id, duration_seconds
    except Exception as e:
        result.errors["meta"] = f"{type(e).__name__}: {e}"
        return "", 0.0


def _read_operation_blocks(
    package_root: Path, result: TimelineResult
) -> list[dict]:
    """读 blocks.json 获取操作块列表。

    blocks.json 是 L1 P4 产出的 list[dict]（非嵌套结构），
    每个块保持原结构（id=bxxx, category=operation, type, timestamp, ...）。

    Returns:
        操作块列表，缺失/出错时返回空数组 + errors 记录
    """
    blocks_path = package_root / "blocks.json"
    if not blocks_path.exists():
        result.errors["blocks"] = f"blocks.json 不存在：{blocks_path}"
        return []

    try:
        blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
        if not isinstance(blocks, list):
            result.errors["blocks"] = (
                f"blocks.json 不是数组：{type(blocks).__name__}"
            )
            return []
        return blocks
    except Exception as e:
        result.errors["blocks"] = f"{type(e).__name__}: {e}"
        return []


# ============================ 关键帧块（image/screenshot，D040）============================


def _build_image_blocks(
    package_root: Path, result: TimelineResult
) -> list[dict]:
    """读 keyframes.json → 转为 image/screenshot 块（f001/f002...）。

    keyframes.json 是 L1 P5 产出的 list[dict]，每项 {frame_path, timestamp, retain_reason}。
    转为 image 块：category=image, type=screenshot, primary.frame_path, primary.retain_reason。
    缺失时返回空数组（不报错，keyframes 是可选产出）。

    Returns:
        image 块列表，缺失时为空
    """
    keyframes_path = package_root / "keyframes.json"
    if not keyframes_path.exists():
        return []  # keyframes.json 缺失不报错，只跳过

    try:
        keyframes = json.loads(keyframes_path.read_text(encoding="utf-8"))
        if not isinstance(keyframes, list):
            return []
    except Exception:
        return []

    blocks: list[dict] = []
    for seq, kf in enumerate(keyframes, start=1):
        blocks.append(_keyframe_to_block(seq, kf))
    return blocks


def _keyframe_to_block(seq: int, keyframe: dict) -> dict:
    """把 keyframe 转为 image/screenshot 块。

    Args:
        seq: 块序号（1-based）
        keyframe: keyframes.json 的一项 {frame_path, timestamp, retain_reason}

    Returns:
        image 块 dict（category=image, type=screenshot, id=f001...）
    """
    return {
        "id": f"f{seq:03d}",
        "category": "image",
        "type": "screenshot",
        "timestamp": float(keyframe.get("timestamp", 0.0)),
        "duration": 0.0,
        "primary": {
            "frame_path": keyframe.get("frame_path", ""),
            "retain_reason": keyframe.get("retain_reason", ""),
        },
        "supplements": {},
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


# ============================ 转写块（text/stt_transcript）+ 音频时间戳映射 D039 ============================


def _build_text_blocks(
    package_root: Path, result: TimelineResult
) -> list[dict]:
    """读 transcript.json → 转为 text/stt_transcript 块（t001/t002...）。

    transcript.json 是 L1 P1 产出的 list[dict]，每项 {line, start, end, text}。
    start/end 是音频相对秒数，需要用 D039 音频时间戳映射到录制包时间轴。
    缺失时返回空数组（不报错，transcript 是可选产出）。

    Returns:
        text 块列表，缺失时为空
    """
    transcript_path = package_root / "transcript.json"
    if not transcript_path.exists():
        return []  # transcript.json 缺失不报错，只跳过

    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        if not isinstance(transcript, list):
            return []
    except Exception:
        return []

    # 读 audio_segments.json（D034 前的裁剪包才有，用于音频时间戳映射 D039）
    audio_segments = _read_audio_segments(package_root)

    blocks: list[dict] = []
    for seq, seg in enumerate(transcript, start=1):
        blocks.append(_transcript_to_block(seq, seg, audio_segments))
    return blocks


def _transcript_to_block(
    seq: int, segment: dict, audio_segments: list[dict] | None
) -> dict:
    """把 transcript 段转为 text/stt_transcript 块。

    Args:
        seq: 块序号（1-based）
        segment: transcript.json 的一项 {line, start, end, text}
        audio_segments: audio_segments.json 的 segments 列表，None 时直接使用音频时间

    Returns:
        text 块 dict（category=text, type=stt_transcript, id=t001...）
    """
    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", 0.0))
    text = str(segment.get("text", ""))

    # 音频时间戳映射 D039：cropped_time → original_time
    if audio_segments is not None:
        mapped_start = _map_audio_time(start, audio_segments)
        mapped_end = _map_audio_time(end, audio_segments)
    else:
        # D034 后新录制包：音频相对秒数直接等于录制包时间轴
        mapped_start = start
        mapped_end = end

    return {
        "id": f"t{seq:03d}",
        "category": "text",
        "type": "stt_transcript",
        "timestamp": mapped_start,
        "duration": max(0.0, mapped_end - mapped_start),
        "primary": {
            "text": text,
            "source": "stt",
        },
        "supplements": {},
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _read_audio_segments(package_root: Path) -> list[dict] | None:
    """读 audio/audio_segments.json 获取音频裁剪映射表。

    audio_segments.json 是 L0 音频裁剪（D027/D031）产出的，
    记录每段的 original_start/end（录制包时间轴）→ cropped_start/end（裁剪音频时间轴）。
    D034 后新录制包无此文件，返回 None（transcript 时间戳直接使用）。

    Returns:
        segments 列表，缺失时为 None
    """
    segments_path = package_root / "audio" / "audio_segments.json"
    if not segments_path.exists():
        return None

    try:
        data = json.loads(segments_path.read_text(encoding="utf-8"))
        segments = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(segments, list):
            return None
        return segments
    except Exception:
        return None


def _map_audio_time(cropped_time: float, segments: list[dict]) -> float:
    """音频时间戳映射 D039：cropped_time → original_time。

    在 segments 列表中找 cropped_start <= time <= cropped_end 的段，
    用 original_start + (cropped_time - cropped_start) 映射到录制包时间轴。
    找不到匹配段时返回原值（防御性降级）。

    Args:
        cropped_time: 裁剪音频时间轴的时间戳（transcript.json 的 start/end）
        segments: audio_segments.json 的 segments 列表

    Returns:
        录制包时间轴时间戳
    """
    for seg in segments:
        cropped_start = float(seg.get("cropped_start", 0.0))
        cropped_end = float(seg.get("cropped_end", 0.0))
        if cropped_start <= cropped_time <= cropped_end:
            original_start = float(seg.get("original_start", 0.0))
            return original_start + (cropped_time - cropped_start)
    # 找不到匹配段，返回原值（防御性降级）
    return cropped_time
