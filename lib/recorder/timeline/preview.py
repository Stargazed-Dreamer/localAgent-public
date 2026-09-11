"""L2 时间轴预览（spec-l2.md Ticket 22）

生成时间轴摘要文本，供 CLI 预览输出。6 项预览：
1. 录制包概览（duration/effective_duration/event_count/frame_count/status）
2. 章节列表（章节名 + 时间范围 + 块数）
3. 块类型分布（operation 各 type + image + text + chapter）
4. 时间跨度（首尾时间戳 + 总时长）
5. 三时间戳对齐验证（blocks/keyframes/transcript 时间戳范围是否在录制包时间轴内）
6. 帧压缩比（原始帧 → 关键帧 → image 块）
"""

import json
from pathlib import Path


def format_preview(package_path: Path | str) -> str:
    """生成时间轴摘要文本。

    Args:
        package_path: 录制包根目录路径

    Returns:
        摘要文本（多行字符串）
    """
    package_root = Path(package_path)
    lines: list[str] = []

    lines.append(f"=== 时间轴预览：{package_root.name} ===")
    lines.append("")

    # 读 meta.json
    meta = _read_dict(package_root / "meta.json")
    _format_overview(meta, lines)

    # 读 timeline.json
    timeline = _read_dict(package_root / "timeline.json")
    if timeline is None:
        lines.append("[警告] timeline.json 不存在，请先运行 build_timeline")
        return "\n".join(lines)

    blocks = timeline.get("blocks", [])
    _format_chapters(blocks, lines)
    _format_block_distribution(blocks, lines)
    _format_time_span(blocks, timeline, lines)

    # 三时间戳对齐验证
    keyframes = _read_list(package_root / "keyframes.json")
    transcript = _read_list(package_root / "transcript.json")
    _format_timestamp_alignment(blocks, keyframes, transcript, meta, lines)

    # 帧压缩比
    _format_frame_compression(package_root, keyframes, blocks, meta, lines)

    return "\n".join(lines)


def _read_json(path: Path) -> dict | list | None:
    """读 JSON 文件，不存在或损坏时返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_dict(path: Path) -> dict | None:
    """读 JSON 对象文件，类型不匹配/不存在/损坏时返回 None。"""
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def _read_list(path: Path) -> list | None:
    """读 JSON 数组文件，类型不匹配/不存在/损坏时返回 None。"""
    data = _read_json(path)
    return data if isinstance(data, list) else None


def _format_overview(meta: dict | None, lines: list[str]) -> None:
    """预览项 1：录制包概览。"""
    lines.append("--- 1. 录制包概览 ---")
    if meta is None:
        lines.append("  [meta.json 不存在]")
        lines.append("")
        return
    duration = meta.get("duration_seconds", 0.0)
    effective = meta.get("effective_duration")
    event_count = meta.get("event_count", 0)
    frame_count = meta.get("frame_count", 0)
    status = meta.get("status", "unknown")
    audio_seconds = meta.get("audio_seconds")

    lines.append(f"  时长: {_fmt_duration(duration)}")
    if effective is not None:
        lines.append(f"  有效时长: {_fmt_duration(effective)}")
    lines.append(f"  事件数: {event_count}")
    lines.append(f"  帧数: {frame_count}")
    if audio_seconds is not None:
        lines.append(f"  音频时长: {_fmt_duration(audio_seconds)}")
    lines.append(f"  状态: {status}")
    lines.append("")


def _format_chapters(blocks: list[dict], lines: list[str]) -> None:
    """预览项 2：章节列表。"""
    lines.append("--- 2. 章节列表 ---")
    chapters = [b for b in blocks if b.get("type") == "chapter"]
    if not chapters:
        lines.append("  [无章节]")
        lines.append("")
        return

    # 计算每个章节的时间范围和块数
    for i, ch in enumerate(chapters):
        ch_start = float(ch.get("timestamp", 0.0))
        ch_name = ch.get("primary", {}).get("name", "")
        # 章节结束时间 = 下一个章节的开始时间或最后一个块的时间戳
        if i + 1 < len(chapters):
            ch_end = float(chapters[i + 1].get("timestamp", 0.0))
        else:
            # 最后一个章节：找最后一个块的时间戳
            all_ts = [float(b.get("timestamp", 0.0)) for b in blocks]
            ch_end = max(all_ts) if all_ts else ch_start

        # 统计该章节内的块数（时间戳在 [ch_start, ch_end) 内的非 chapter 块）
        block_count = sum(
            1
            for b in blocks
            if b.get("type") != "chapter"
            and ch_start <= float(b.get("timestamp", 0.0)) < ch_end
        )

        lines.append(
            f"  {ch.get('id', '')}  [{_fmt_time(ch_start)} - {_fmt_time(ch_end)}]  "
            f"{ch_name}  ({block_count} 块)"
        )
    lines.append("")


def _format_block_distribution(blocks: list[dict], lines: list[str]) -> None:
    """预览项 3：块类型分布。"""
    lines.append("--- 3. 块类型分布 ---")
    # 按 category + type 统计
    dist: dict[str, int] = {}
    for b in blocks:
        cat = b.get("category", "unknown")
        btype = b.get("type", "unknown")
        key = f"{cat}/{btype}"
        dist[key] = dist.get(key, 0) + 1

    if not dist:
        lines.append("  [无块]")
    else:
        for key in sorted(dist.keys()):
            lines.append(f"  {key}: {dist[key]}")
    lines.append(f"  总计: {len(blocks)}")
    lines.append("")


def _format_time_span(
    blocks: list[dict], timeline: dict, lines: list[str]
) -> None:
    """预览项 4：时间跨度。"""
    lines.append("--- 4. 时间跨度 ---")
    if not blocks:
        lines.append("  [无块]")
        lines.append("")
        return

    timestamps = [float(b.get("timestamp", 0.0)) for b in blocks]
    first_ts = min(timestamps)
    last_ts = max(timestamps)
    span = last_ts - first_ts

    lines.append(f"  首块时间戳: {_fmt_time(first_ts)}")
    lines.append(f"  尾块时间戳: {_fmt_time(last_ts)}")
    lines.append(f"  时间跨度: {_fmt_duration(span)}")
    lines.append(
        f"  录制时长: {_fmt_duration(timeline.get('duration_seconds', 0.0))}"
    )
    lines.append("")


def _format_timestamp_alignment(
    blocks: list[dict],
    keyframes: list | None,
    transcript: list | None,
    meta: dict | None,
    lines: list[str],
) -> None:
    """预览项 5：三时间戳对齐验证。"""
    lines.append("--- 5. 三时间戳对齐验证 ---")
    duration = float(meta.get("duration_seconds", 0.0)) if meta else 0.0

    # blocks.json 时间戳范围
    if blocks:
        op_ts = [
            float(b.get("timestamp", 0.0))
            for b in blocks
            if b.get("category") == "operation"
        ]
        if op_ts:
            lines.append(
                f"  操作块: [{_fmt_time(min(op_ts))} - {_fmt_time(max(op_ts))}]"
            )
            _check_in_range(min(op_ts), max(op_ts), duration, "操作块", lines)
        else:
            lines.append("  操作块: [无]")
    else:
        lines.append("  操作块: [无]")

    # keyframes.json 时间戳范围
    if keyframes:
        kf_ts = [float(kf.get("timestamp", 0.0)) for kf in keyframes]
        if kf_ts:
            lines.append(
                f"  关键帧: [{_fmt_time(min(kf_ts))} - {_fmt_time(max(kf_ts))}]"
            )
            _check_in_range(min(kf_ts), max(kf_ts), duration, "关键帧", lines)
        else:
            lines.append("  关键帧: [无]")
    else:
        lines.append("  关键帧: [无 keyframes.json]")

    # transcript.json 时间戳范围
    if transcript:
        tr_ts = []
        for seg in transcript:
            tr_ts.append(float(seg.get("start", 0.0)))
            tr_ts.append(float(seg.get("end", 0.0)))
        if tr_ts:
            lines.append(
                f"  转写:   [{_fmt_time(min(tr_ts))} - {_fmt_time(max(tr_ts))}]"
            )
            _check_in_range(min(tr_ts), max(tr_ts), duration, "转写", lines)
        else:
            lines.append("  转写:   [无]")
    else:
        lines.append("  转写:   [无 transcript.json]")

    lines.append("")


def _format_frame_compression(
    package_root: Path,
    keyframes: list | None,
    blocks: list[dict],
    meta: dict | None,
    lines: list[str],
) -> None:
    """预览项 6：帧压缩比。"""
    lines.append("--- 6. 帧压缩比 ---")

    # 原始帧数：meta.json 的 frame_count 或 frames/ 目录文件数
    original_frames = 0
    if meta:
        original_frames = int(meta.get("frame_count", 0))
    if original_frames == 0:
        frames_dir = package_root / "frames"
        if frames_dir.exists():
            original_frames = len(list(frames_dir.glob("*.png")))

    # 关键帧数：keyframes.json 的长度
    keyframe_count = len(keyframes) if keyframes else 0

    # image 块数：timeline.json 中 category=image 的块
    image_block_count = sum(
        1 for b in blocks if b.get("category") == "image"
    )

    lines.append(f"  原始帧: {original_frames}")
    lines.append(f"  关键帧: {keyframe_count}")
    lines.append(f"  image 块: {image_block_count}")

    if original_frames > 0:
        ratio1 = keyframe_count / original_frames * 100
        lines.append(f"  原始→关键帧压缩: {ratio1:.1f}%")
    if keyframe_count > 0:
        ratio2 = image_block_count / keyframe_count * 100
        lines.append(f"  关键帧→image块: {ratio2:.1f}%")

    lines.append("")
    lines.append("=== 预览结束 ===")


def _check_in_range(
    min_ts: float,
    max_ts: float,
    duration: float,
    label: str,
    lines: list[str],
) -> None:
    """检查时间戳范围是否在录制包时间轴内。"""
    if duration <= 0:
        return
    if min_ts < 0:
        lines.append(f"    ⚠ {label} 起始时间戳 < 0（越界）")
    if max_ts > duration + 1.0:  # 1s 容差
        lines.append(
            f"    ⚠ {label} 结束时间戳 > 录制时长（越界: {_fmt_time(max_ts)} > {_fmt_time(duration)}）"
        )


def _fmt_time(seconds: float) -> str:
    """格式化时间戳为 MM:SS.sss。"""
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:06.3f}"


def _fmt_duration(seconds: float) -> str:
    """格式化时长为 H:MM:SS 或 S.ssss。"""
    if seconds < 60:
        return f"{seconds:.3f}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m}:{s:05.2f}"
