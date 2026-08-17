"""L2 时间轴层 Ticket 19：T1 骨架 + 操作块合并测试

测试策略（spec-l2.md Testing Decisions）：
- 用构造的录制包目录测试（含 meta.json + blocks.json）
- 操作块合并：读 blocks.json → 保持原结构和 ID
- 时间戳排序：乱序 blocks 排序后正确
- 缺失文件降级：blocks.json / meta.json 缺失时 errors 记录 + 降级处理
- TimelineResult 字段验证
- timeline.json 格式验证（recording_id / duration_seconds / block_count / tracks / blocks）

覆盖 Ticket 19 acceptance criteria：
- [x] build_timeline(package_path, options=None) -> TimelineResult 入口函数
- [x] 读 blocks.json → 操作块（保持原结构和 ID）
- [x] 读 meta.json → recording_id + duration_seconds
- [x] 生成 timeline.json 骨架（recording_id / duration_seconds / block_count / tracks / blocks 排序）
- [x] TimelineResult dataclass（package_root / timeline_path / annotations_path / block_count / chapter_count / errors）
- [x] 缺失文件处理：blocks.json 缺失时 blocks 为空数组 + errors 记录
- [x] 单测：操作块合并 + 时间戳排序 + 缺失文件降级 + TimelineResult 字段
"""

import json
from pathlib import Path

import pytest

from lib.recorder.timeline import TimelineResult, build_timeline

# ============================ 测试辅助函数 ============================


def _make_operation_block(
    seq: int,
    block_type: str = "mouse_click",
    timestamp: float = 0.0,
    duration: float = 0.0,
) -> dict:
    """构造一个操作块 dict（符合 L1 P4 产出结构）。"""
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": block_type,
        "timestamp": timestamp,
        "duration": duration,
        "primary": {"x": 100, "y": 200, "button": "left", "clicks": 1},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _write_meta(
    package_root: Path,
    package_name: str = "rec_test",
    duration_seconds: float = 60.0,
) -> Path:
    """写 meta.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    meta_path = package_root / "meta.json"
    meta = {
        "package_name": package_name,
        "start_time": 1700000000.0,
        "end_time": 1700000000.0 + duration_seconds,
        "duration_seconds": duration_seconds,
        "event_count": 10,
        "frame_count": 5,
        "audio_seconds": duration_seconds,
        "sensors": ["KeyboardSensor", "MouseSensor"],
        "status": "saved",
        "version": "0.1.0",
        "format": "L0-sensor-layer",
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta_path


def _write_blocks(package_root: Path, blocks: list[dict]) -> Path:
    """写 blocks.json（L1 P4 产出格式：list[dict]）。"""
    package_root.mkdir(parents=True, exist_ok=True)
    blocks_path = package_root / "blocks.json"
    blocks_path.write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return blocks_path


def _build_minimal_package(
    tmp_path: Path,
    blocks: list[dict] | None = None,
    package_name: str = "rec_test",
    duration_seconds: float = 60.0,
) -> Path:
    """构造一个最小录制包目录（meta.json + blocks.json）。"""
    package_root = tmp_path / package_name
    _write_meta(package_root, package_name, duration_seconds)
    if blocks is not None:
        _write_blocks(package_root, blocks)
    return package_root


# ============================ 操作块合并测试 ============================


class TestOperationBlockMerge:
    """读 blocks.json → 操作块合并到 timeline.json。"""

    def test_single_block(self, tmp_path):
        """单个操作块正确合并（含 1 个"开始"章节块）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.5)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        # 1 操作块 + 1 章节块（"开始" at 0.0）
        assert result.block_count == 2
        assert result.chapter_count == 1
        assert len(result.errors) == 0

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["recording_id"] == "rec_test"
        assert timeline["duration_seconds"] == 60.0
        assert timeline["block_count"] == 2
        assert timeline["tracks"] == ["operation", "chapter"]
        assert len(timeline["blocks"]) == 2

    def test_multiple_blocks_preserve_structure(self, tmp_path):
        """多个操作块保持原结构和 ID（章节块不干扰）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "keyboard_input", 2.5),
            _make_operation_block(3, "focus_change", 3.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 过滤出操作块验证结构
        op_blocks = [b for b in timeline["blocks"] if b["category"] == "operation"]
        assert len(op_blocks) == 3

        for i, block in enumerate(op_blocks):
            assert block["id"] == f"b{i + 1:03d}"
            assert block["category"] == "operation"
            assert "type" in block
            assert "timestamp" in block
            assert "duration" in block
            assert "primary" in block
            assert "supplements" in block
            assert "status" in block

    def test_block_id_prefix_b(self, tmp_path):
        """操作块 ID 前缀为 b（D041）。"""
        blocks = [_make_operation_block(1, "mouse_click", 0.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        op_blocks = [b for b in timeline["blocks"] if b["category"] == "operation"]
        assert op_blocks[0]["id"].startswith("b")

    def test_idle_block_preserved(self, tmp_path):
        """idle 块（category=operation, type=idle）保持原结构。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            {
                "id": "b002",
                "category": "operation",
                "type": "idle",
                "timestamp": 1.0,
                "duration": 5.0,
                "primary": {"duration": 5.0},
                "supplements": {
                    "before_frame": None,
                    "after_frame": None,
                    "uia_snapshot": None,
                    "linked_text": [],
                },
                "status": {
                    "marked_key": False,
                    "marked_anomaly": False,
                    "marked_automatable": False,
                    "is_trimmed": False,
                },
            },
            _make_operation_block(3, "mouse_click", 6.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 3 操作块 + 2 章节块（"开始"@0.0 + "idle 5.0s"@1.0）
        assert len(timeline["blocks"]) == 5
        idle_blocks = [b for b in timeline["blocks"] if b.get("type") == "idle"]
        assert len(idle_blocks) == 1
        idle_block = idle_blocks[0]
        assert idle_block["type"] == "idle"
        assert idle_block["category"] == "operation"
        assert idle_block["duration"] == 5.0


# ============================ 时间戳排序测试 ============================


class TestTimestampSorting:
    """timeline.json 的 blocks 按时间戳排序。"""

    def test_already_sorted(self, tmp_path):
        """已排序的 blocks 保持升序（含章节块 0.0）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "mouse_click", 2.0),
            _make_operation_block(3, "mouse_click", 3.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        timestamps = [b["timestamp"] for b in timeline["blocks"]]
        assert timestamps == [0.0, 1.0, 2.0, 3.0]

    def test_unsorted_sorted(self, tmp_path):
        """乱序 blocks 排序后正确（含章节块 0.0）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 3.0),
            _make_operation_block(2, "mouse_click", 1.0),
            _make_operation_block(3, "mouse_click", 2.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        timestamps = [b["timestamp"] for b in timeline["blocks"]]
        assert timestamps == [0.0, 1.0, 2.0, 3.0]

    def test_same_timestamp_stable(self, tmp_path):
        """相同时间戳的块保持原顺序（稳定排序，章节块在 0.0 不影响）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "keyboard_input", 1.0),
            _make_operation_block(3, "focus_change", 1.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 章节块 c001 在 0.0，操作块 b001/b002/b003 在 1.0 保持原顺序
        op_ids = [b["id"] for b in timeline["blocks"] if b["category"] == "operation"]
        assert op_ids == ["b001", "b002", "b003"]


# ============================ timeline.json 格式验证 ============================


class TestTimelineJsonFormat:
    """timeline.json 格式符合 spec-l2.md 定义。"""

    def test_top_level_fields(self, tmp_path):
        """timeline.json 包含 recording_id / duration_seconds / block_count / tracks / blocks。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(
            tmp_path, blocks, package_name="rec_20260723_143000", duration_seconds=184.5
        )

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert "recording_id" in timeline
        assert "duration_seconds" in timeline
        assert "block_count" in timeline
        assert "tracks" in timeline
        assert "blocks" in timeline
        assert timeline["recording_id"] == "rec_20260723_143000"
        assert timeline["duration_seconds"] == 184.5
        # 1 操作块 + 1 章节块
        assert timeline["block_count"] == 2
        assert timeline["tracks"] == ["operation", "chapter"]

    def test_recording_id_from_meta(self, tmp_path):
        """recording_id 取自 meta.json 的 package_name。"""
        blocks = [_make_operation_block(1, "mouse_click", 0.0)]
        pkg = _build_minimal_package(
            tmp_path, blocks, package_name="rec_20260723_162924"
        )

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["recording_id"] == "rec_20260723_162924"

    def test_block_count_matches(self, tmp_path):
        """block_count 等于 blocks 数组长度（含章节块）。"""
        blocks = [
            _make_operation_block(i, "mouse_click", float(i)) for i in range(1, 6)
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 5 操作块 + 1 章节块
        assert timeline["block_count"] == len(timeline["blocks"]) == 6

    def test_json_is_utf8_indented(self, tmp_path):
        """timeline.json 用 UTF-8 + indent=2 写入（含中文验证）。"""
        blocks = [
            {
                "id": "b001",
                "category": "operation",
                "type": "keyboard_input",
                "timestamp": 1.0,
                "duration": 0.0,
                "primary": {"text": "你好世界", "detection_method": "uia_value_diff"},
                "supplements": {"before_frame": None, "after_frame": None},
                "status": {
                    "marked_key": False,
                    "marked_anomaly": False,
                    "marked_automatable": False,
                    "is_trimmed": False,
                },
            }
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        raw = result.timeline_path.read_text(encoding="utf-8")
        assert "你好世界" in raw  # 中文不转义
        assert "\n  " in raw  # 有缩进


# ============================ 缺失文件降级测试 ============================


class TestMissingFileDegradation:
    """缺失文件时的降级处理。"""

    def test_missing_blocks_json(self, tmp_path):
        """blocks.json 缺失时 blocks 为空数组 + errors 记录。"""
        # 只有 meta.json，没有 blocks.json
        pkg = _build_minimal_package(tmp_path, blocks=None)

        result = build_timeline(pkg)

        assert result.block_count == 0
        assert "blocks" in result.errors
        assert "blocks.json 不存在" in result.errors["blocks"]

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["blocks"] == []
        assert timeline["block_count"] == 0
        assert timeline["tracks"] == ["operation"]

    def test_missing_meta_json(self, tmp_path):
        """meta.json 缺失时 recording_id="" + duration_seconds=0.0 + errors 记录。"""
        package_root = tmp_path / "rec_nometa"
        package_root.mkdir(parents=True)
        _write_blocks(package_root, [_make_operation_block(1, "mouse_click", 1.0)])

        result = build_timeline(package_root)

        assert "meta" in result.errors
        assert "meta.json 不存在" in result.errors["meta"]

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["recording_id"] == ""
        assert timeline["duration_seconds"] == 0.0
        # blocks 仍正常合并（1 操作块 + 1 章节块）
        assert len(timeline["blocks"]) == 2

    def test_missing_both(self, tmp_path):
        """meta.json 和 blocks.json 都缺失。"""
        package_root = tmp_path / "rec_empty"
        package_root.mkdir(parents=True)

        result = build_timeline(package_root)

        assert "meta" in result.errors
        assert "blocks" in result.errors
        assert result.block_count == 0

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["recording_id"] == ""
        assert timeline["duration_seconds"] == 0.0
        assert timeline["blocks"] == []
        assert timeline["block_count"] == 0

    def test_corrupt_blocks_json(self, tmp_path):
        """blocks.json 损坏（非法 JSON）时降级。"""
        pkg = _build_minimal_package(tmp_path, blocks=[])
        # 覆盖为非法 JSON
        (pkg / "blocks.json").write_text("{invalid json", encoding="utf-8")

        result = build_timeline(pkg)

        assert "blocks" in result.errors
        assert result.block_count == 0

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["blocks"] == []

    def test_blocks_not_list(self, tmp_path):
        """blocks.json 不是数组时降级。"""
        pkg = _build_minimal_package(tmp_path, blocks=[])
        # 覆盖为 dict
        (pkg / "blocks.json").write_text(
            '{"blocks": []}', encoding="utf-8"
        )

        result = build_timeline(pkg)

        assert "blocks" in result.errors
        assert "不是数组" in result.errors["blocks"]
        assert result.block_count == 0


# ============================ TimelineResult 字段测试 ============================


class TestTimelineResult:
    """TimelineResult dataclass 字段验证。"""

    def test_all_fields_present(self, tmp_path):
        """TimelineResult 包含所有必需字段。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        assert isinstance(result, TimelineResult)
        assert result.package_root == pkg
        assert result.timeline_path == pkg / "timeline.json"
        assert result.annotations_path == pkg / "annotations.json"
        # 1 操作块 + 1 章节块
        assert result.block_count == 2
        assert result.chapter_count == 1
        assert isinstance(result.errors, dict)

    def test_timeline_path_exists(self, tmp_path):
        """timeline.json 文件实际生成。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        assert result.timeline_path is not None
        assert result.timeline_path.exists()

    def test_annotations_path_not_initialized(self, tmp_path):
        """Ticket 19 不初始化 annotations.json 文件（Ticket 22 才做）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        # annotations_path 路径已设置，但文件不存在
        assert result.annotations_path == pkg / "annotations.json"
        assert not result.annotations_path.exists()

    def test_str_package_path(self, tmp_path):
        """package_path 接受 str（不只 Path）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(str(pkg))

        assert result.package_root == pkg
        # 1 操作块 + 1 章节块
        assert result.block_count == 2

    def test_none_options(self, tmp_path):
        """options=None 用默认值。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg, options=None)

        assert result.block_count == 2
        assert len(result.errors) == 0

    def test_empty_options(self, tmp_path):
        """options={} 用默认值。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg, options={})

        assert result.block_count == 2
        assert len(result.errors) == 0

    def test_empty_blocks(self, tmp_path):
        """空 blocks.json（空数组）正确处理。"""
        pkg = _build_minimal_package(tmp_path, blocks=[])

        result = build_timeline(pkg)

        assert result.block_count == 0
        assert len(result.errors) == 0

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["blocks"] == []
        assert timeline["block_count"] == 0


# ============================ Ticket 20: 关键帧块 + 转写块 + 音频映射 ============================


def _make_keyframe(
    frame_path: str = "frames/00001000.png",
    timestamp: float = 1.0,
    retain_reason: str = "timer_unique",
) -> dict:
    """构造 keyframes.json 的一项。"""
    return {
        "frame_path": frame_path,
        "timestamp": timestamp,
        "retain_reason": retain_reason,
    }


def _make_transcript_segment(
    line: int = 1,
    start: float = 1.0,
    end: float = 4.0,
    text: str = "测试转写文本",
) -> dict:
    """构造 transcript.json 的一项。"""
    return {"line": line, "start": start, "end": end, "text": text}


def _make_audio_segment(
    original_start: float, original_end: float,
    cropped_start: float, cropped_end: float,
) -> dict:
    """构造 audio_segments.json 的一个 segment。"""
    return {
        "original_start": original_start,
        "original_end": original_end,
        "cropped_start": cropped_start,
        "cropped_end": cropped_end,
    }


def _write_keyframes(package_root: Path, keyframes: list[dict]) -> Path:
    """写 keyframes.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    kf_path = package_root / "keyframes.json"
    kf_path.write_text(
        json.dumps(keyframes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return kf_path


def _write_transcript(package_root: Path, transcript: list[dict]) -> Path:
    """写 transcript.json。"""
    package_root.mkdir(parents=True, exist_ok=True)
    ts_path = package_root / "transcript.json"
    ts_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ts_path


def _write_audio_segments(
    package_root: Path, segments: list[dict]
) -> Path:
    """写 audio/audio_segments.json。"""
    audio_dir = package_root / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    seg_path = audio_dir / "audio_segments.json"
    data = {
        "original_duration": 60.0,
        "cropped_duration": 30.0,
        "segments": segments,
    }
    seg_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return seg_path


class TestImageBlockMerge:
    """读 keyframes.json → image/screenshot 块（f001/f002...，D040）。"""

    def test_single_keyframe_to_image_block(self, tmp_path):
        """单个 keyframe 转为 image/screenshot 块。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [_make_keyframe("frames/00001000.png", 2.0, "timer_unique")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        image_blocks = [b for b in timeline["blocks"] if b["category"] == "image"]
        assert len(image_blocks) == 1
        ib = image_blocks[0]
        assert ib["id"] == "f001"
        assert ib["category"] == "image"
        assert ib["type"] == "screenshot"
        assert ib["timestamp"] == 2.0
        assert ib["duration"] == 0.0
        assert ib["primary"]["frame_path"] == "frames/00001000.png"
        assert ib["primary"]["retain_reason"] == "timer_unique"

    def test_multiple_keyframes_sequential_ids(self, tmp_path):
        """多个 keyframe 的 ID 按 f001/f002... 递增。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_keyframes(pkg, [
            _make_keyframe("frames/a.png", 1.0, "timer_unique"),
            _make_keyframe("frames/b.png", 2.0, "operation"),
            _make_keyframe("frames/c.png", 3.0, "timer_unique"),
        ])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        image_blocks = [b for b in timeline["blocks"] if b["category"] == "image"]
        assert len(image_blocks) == 3
        assert [b["id"] for b in image_blocks] == ["f001", "f002", "f003"]

    def test_image_block_id_prefix_f(self, tmp_path):
        """image 块 ID 前缀为 f（D041）。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_keyframes(pkg, [_make_keyframe(timestamp=0.0)])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert all(b["id"].startswith("f") for b in timeline["blocks"] if b["category"] == "image")

    def test_keyframe_status_fields(self, tmp_path):
        """image 块的 status 字段初始化为全 false。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_keyframes(pkg, [_make_keyframe(timestamp=1.0)])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        ib = [b for b in timeline["blocks"] if b["category"] == "image"][0]
        assert ib["status"] == {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        }


class TestTextBlockMerge:
    """读 transcript.json → text/stt_transcript 块（t001/t002...）。"""

    def test_single_transcript_to_text_block(self, tmp_path):
        """单个 transcript 段转为 text/stt_transcript 块。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_transcript(pkg, [_make_transcript_segment(1, 1.0, 4.0, "你好世界")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        text_blocks = [b for b in timeline["blocks"] if b["category"] == "text"]
        assert len(text_blocks) == 1
        tb = text_blocks[0]
        assert tb["id"] == "t001"
        assert tb["category"] == "text"
        assert tb["type"] == "stt_transcript"
        assert tb["timestamp"] == 1.0
        assert tb["duration"] == 3.0  # 4.0 - 1.0
        assert tb["primary"]["text"] == "你好世界"
        assert tb["primary"]["source"] == "stt"

    def test_multiple_transcripts_sequential_ids(self, tmp_path):
        """多个 transcript 段的 ID 按 t001/t002... 递增。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_transcript(pkg, [
            _make_transcript_segment(1, 1.0, 2.0, "第一句"),
            _make_transcript_segment(2, 3.0, 5.0, "第二句"),
        ])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        text_blocks = [b for b in timeline["blocks"] if b["category"] == "text"]
        assert len(text_blocks) == 2
        assert [b["id"] for b in text_blocks] == ["t001", "t002"]

    def test_text_block_id_prefix_t(self, tmp_path):
        """text 块 ID 前缀为 t（D041）。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_transcript(pkg, [_make_transcript_segment(1, 0.0, 1.0, "test")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert all(b["id"].startswith("t") for b in timeline["blocks"] if b["category"] == "text")

    def test_transcript_without_audio_segments(self, tmp_path):
        """无 audio_segments.json 时（D034 后新录制包），transcript 时间戳直接使用。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_transcript(pkg, [_make_transcript_segment(1, 5.0, 8.0, "文本")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        tb = [b for b in timeline["blocks"] if b["category"] == "text"][0]
        assert tb["timestamp"] == 5.0  # 直接使用，不映射
        assert tb["duration"] == 3.0


class TestAudioTimestampMapping:
    """音频时间戳映射 D039：有 audio_segments.json 时映射。"""

    def test_map_with_audio_segments(self, tmp_path):
        """有 audio_segments.json 时，transcript 时间戳映射到录制包时间轴。"""
        pkg = _build_minimal_package(tmp_path, [])
        # segments: original [2.0-2.3] → cropped [0.0-0.3]
        #           original [4.0-6.2] → cropped [0.3-2.5]
        _write_audio_segments(pkg, [
            _make_audio_segment(2.0, 2.3, 0.0, 0.3),
            _make_audio_segment(4.0, 6.2, 0.3, 2.5),
        ])
        # transcript 的 start=0.1（cropped 时间轴）→ 映射到 original 2.1
        # transcript 的 end=0.2（cropped 时间轴）→ 映射到 original 2.2
        _write_transcript(pkg, [_make_transcript_segment(1, 0.1, 0.2, "测试")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        tb = [b for b in timeline["blocks"] if b["category"] == "text"][0]
        assert tb["timestamp"] == pytest.approx(2.1)  # 2.0 + (0.1 - 0.0)
        assert tb["duration"] == pytest.approx(0.1)   # 2.2 - 2.1

    def test_map_across_segments(self, tmp_path):
        """transcript 段跨多个 audio_segments 时分别映射。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_audio_segments(pkg, [
            _make_audio_segment(2.0, 2.3, 0.0, 0.3),
            _make_audio_segment(4.0, 6.2, 0.3, 2.5),
        ])
        # start=0.1 → segment 1 → original 2.1
        # end=1.0 → segment 2 → original 4.7 (4.0 + (1.0 - 0.3))
        _write_transcript(pkg, [_make_transcript_segment(1, 0.1, 1.0, "跨段")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        tb = [b for b in timeline["blocks"] if b["category"] == "text"][0]
        assert tb["timestamp"] == pytest.approx(2.1)
        assert tb["duration"] == pytest.approx(4.7 - 2.1)

    def test_map_boundary_values(self, tmp_path):
        """映射边界值：cropped_start 和 cropped_end 精确映射。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_audio_segments(pkg, [
            _make_audio_segment(10.0, 12.0, 0.0, 2.0),
        ])
        # start=0.0（cropped 边界）→ original 10.0
        # end=2.0（cropped 边界）→ original 12.0
        _write_transcript(pkg, [_make_transcript_segment(1, 0.0, 2.0, "边界")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        tb = [b for b in timeline["blocks"] if b["category"] == "text"][0]
        assert tb["timestamp"] == pytest.approx(10.0)
        assert tb["duration"] == pytest.approx(2.0)

    def test_map_no_matching_segment_fallback(self, tmp_path):
        """找不到匹配段时返回原值（防御性降级）。"""
        pkg = _build_minimal_package(tmp_path, [])
        _write_audio_segments(pkg, [
            _make_audio_segment(2.0, 2.3, 0.0, 0.3),
        ])
        # 5.0 不在任何 segment 的 cropped 范围内
        _write_transcript(pkg, [_make_transcript_segment(1, 5.0, 6.0, "越界")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        tb = [b for b in timeline["blocks"] if b["category"] == "text"][0]
        assert tb["timestamp"] == 5.0  # 原值降级
        assert tb["duration"] == 1.0


class TestMissingKeyframesTranscript:
    """keyframes.json / transcript.json 缺失时跳过不报错。"""

    def test_missing_keyframes_no_error(self, tmp_path):
        """keyframes.json 缺失时不报错，无 image 块。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        assert "keyframes" not in result.errors
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert not any(b["category"] == "image" for b in timeline["blocks"])

    def test_missing_transcript_no_error(self, tmp_path):
        """transcript.json 缺失时不报错，无 stt_transcript 块（章节块仍存在）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        assert "transcript" not in result.errors
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 无 stt_transcript 块（章节块 type=chapter 不算转写块）
        assert not any(b.get("type") == "stt_transcript" for b in timeline["blocks"])

    def test_missing_both_optional(self, tmp_path):
        """keyframes 和 transcript 都缺失，只有操作块 + 章节块。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        # 1 操作块 + 1 章节块
        assert result.block_count == 2
        assert len(result.errors) == 0
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 章节块独立 "chapter" track
        assert timeline["tracks"] == ["operation", "chapter"]

    def test_corrupt_keyframes_skipped(self, tmp_path):
        """keyframes.json 损坏时静默跳过。"""
        pkg = _build_minimal_package(tmp_path, [])
        (pkg / "keyframes.json").write_text("{invalid", encoding="utf-8")

        result = build_timeline(pkg)

        # 损坏不报错，只跳过
        assert "keyframes" not in result.errors

    def test_corrupt_transcript_skipped(self, tmp_path):
        """transcript.json 损坏时静默跳过。"""
        pkg = _build_minimal_package(tmp_path, [])
        (pkg / "transcript.json").write_text("{invalid", encoding="utf-8")

        result = build_timeline(pkg)

        assert "transcript" not in result.errors


class TestThreeWayMerge:
    """三类块（操作/图像/文本）合并后按时间戳排序。"""

    def test_three_way_merge_sorted(self, tmp_path):
        """三类块混合后按时间戳排序。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "keyboard_input", 5.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [
            _make_keyframe("frames/a.png", 2.0, "timer_unique"),
            _make_keyframe("frames/b.png", 4.0, "operation"),
        ])
        _write_transcript(pkg, [
            _make_transcript_segment(1, 3.0, 3.5, "中间语音"),
        ])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        timestamps = [b["timestamp"] for b in timeline["blocks"]]
        assert timestamps == sorted(timestamps)  # 升序

        # 验证三类块都有
        categories = {b["category"] for b in timeline["blocks"]}
        assert categories == {"operation", "image", "text"}

    def test_tracks_extended(self, tmp_path):
        """有三类块时 tracks 扩展为 ["operation", "image", "text", "chapter"]。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [_make_keyframe(timestamp=2.0)])
        _write_transcript(pkg, [_make_transcript_segment(1, 3.0, 4.0, "test")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert set(timeline["tracks"]) == {"operation", "image", "text", "chapter"}

    def test_tracks_only_operation_when_no_extras(self, tmp_path):
        """无 keyframes/transcript 时 tracks = ["operation", "chapter"]（章节独立 track）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 章节块独立 "chapter" track（无 transcript 时不含 "text"）
        assert timeline["tracks"] == ["operation", "chapter"]

    def test_tracks_operation_and_image(self, tmp_path):
        """有 keyframes 但无 transcript 时 tracks = ["operation", "image", "chapter"]。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [_make_keyframe(timestamp=2.0)])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert timeline["tracks"] == ["operation", "image", "chapter"]

    def test_block_count_includes_all_types(self, tmp_path):
        """block_count 包含四类块总数（操作/图像/文本/章节）。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_operation_block(2, "keyboard_input", 2.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [_make_keyframe(timestamp=1.5)])
        _write_transcript(pkg, [_make_transcript_segment(1, 1.2, 1.8, "text")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        # 2 操作 + 1 image + 1 text + 1 chapter = 5
        assert timeline["block_count"] == 5
        assert result.block_count == 5


# ============================ Ticket 21: 章节块（c001...，D042）============================


def _make_focus_change_block(
    seq: int, timestamp: float, title: str
) -> dict:
    """构造一个 focus_change 操作块（含 title 字段）。"""
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": "focus_change",
        "timestamp": timestamp,
        "duration": 0.0,
        "primary": {"title": title, "process": "app.exe", "pid": 1234},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _make_idle_block(seq: int, timestamp: float, duration: float) -> dict:
    """构造一个 idle 操作块。"""
    return {
        "id": f"b{seq:03d}",
        "category": "operation",
        "type": "idle",
        "timestamp": timestamp,
        "duration": duration,
        "primary": {"duration": duration},
        "supplements": {
            "before_frame": None,
            "after_frame": None,
            "uia_snapshot": None,
            "linked_text": [],
        },
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


class TestChapterGeneration:
    """Ticket 21：章节块生成（D042）。"""

    def test_first_chapter_at_zero(self, tmp_path):
        """第一个章节块 timestamp=0.0。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert len(chapters) >= 1
        assert chapters[0]["timestamp"] == 0.0

    def test_first_chapter_name_from_focus_change(self, tmp_path):
        """第一个章节名取自第一个 focus_change 的 title。"""
        blocks = [
            _make_focus_change_block(1, 0.5, "TRAE Work CN"),
            _make_operation_block(2, "mouse_click", 1.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert chapters[0]["primary"]["name"] == "TRAE Work CN"

    def test_first_chapter_name_default_start(self, tmp_path):
        """无 focus_change 时第一个章节名为"开始"。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert len(chapters) == 1
        assert chapters[0]["primary"]["name"] == "开始"

    def test_focus_change_creates_chapter(self, tmp_path):
        """focus_change 块切分新章节。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_focus_change_block(2, 5.0, "Chrome"),
            _make_operation_block(3, "mouse_click", 6.0),
            _make_focus_change_block(4, 10.0, "VSCode"),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        # c001@0.0 (first focus_change title "Chrome") + c002@5.0 "Chrome" + c003@10.0 "VSCode"
        # 去重：c001 name="Chrome", c002 name="Chrome" → 去重保留 c001
        # 所以 chapters = [c001@0.0 "Chrome", c003@10.0 "VSCode"]
        assert len(chapters) == 2
        assert chapters[0]["timestamp"] == 0.0
        assert chapters[0]["primary"]["name"] == "Chrome"
        assert chapters[1]["timestamp"] == 10.0
        assert chapters[1]["primary"]["name"] == "VSCode"

    def test_idle_above_threshold_creates_chapter(self, tmp_path):
        """idle 块 duration > 阈值切章节。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_idle_block(2, 2.0, 5.0),  # 5.0 > 2.0 阈值
            _make_operation_block(3, "mouse_click", 7.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        # c001@0.0 "开始" + c002@2.0 "idle 5.0s"
        assert len(chapters) == 2
        assert chapters[0]["primary"]["name"] == "开始"
        assert chapters[1]["primary"]["name"] == "idle 5.0s"
        assert chapters[1]["timestamp"] == 2.0

    def test_idle_below_threshold_no_chapter(self, tmp_path):
        """idle 块 duration <= 阈值不切章节。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_idle_block(2, 2.0, 1.5),  # 1.5 < 2.0 阈值
            _make_operation_block(3, "mouse_click", 3.5),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        # 只有 c001@0.0 "开始"，idle 1.5s 不切章节
        assert len(chapters) == 1
        assert chapters[0]["primary"]["name"] == "开始"

    def test_consecutive_same_name_dedup(self, tmp_path):
        """连续同名章节去重（只保留第一个）。"""
        blocks = [
            _make_focus_change_block(1, 1.0, "Chrome"),
            _make_operation_block(2, "mouse_click", 2.0),
            _make_focus_change_block(3, 3.0, "Chrome"),  # 同名，去重
            _make_operation_block(4, "mouse_click", 4.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        # c001@0.0 "Chrome" + c002@1.0 "Chrome" (dedup) + c003@3.0 "Chrome" (dedup)
        # → 只保留 c001@0.0
        assert len(chapters) == 1
        assert chapters[0]["primary"]["name"] == "Chrome"

    def test_chapter_block_format(self, tmp_path):
        """章节块格式正确（id=c001, category=text, type=chapter）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        ch = chapters[0]
        assert ch["id"] == "c001"
        assert ch["category"] == "text"
        assert ch["type"] == "chapter"
        assert ch["duration"] == 0.0
        assert "name" in ch["primary"]
        assert ch["supplements"] == {}
        assert ch["status"] == {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        }

    def test_chapter_id_prefix_c(self, tmp_path):
        """章节块 ID 前缀为 c（D041）。"""
        blocks = [
            _make_focus_change_block(1, 1.0, "App1"),
            _make_focus_change_block(2, 5.0, "App2"),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert all(ch["id"].startswith("c") for ch in chapters)

    def test_chapter_count_in_result(self, tmp_path):
        """result.chapter_count 反映章节数量。"""
        blocks = [
            _make_focus_change_block(1, 1.0, "App1"),
            _make_focus_change_block(2, 5.0, "App2"),
            _make_focus_change_block(3, 10.0, "App3"),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        # c001@0.0 "App1" + c002@5.0 "App2" + c003@10.0 "App3"
        assert result.chapter_count == 3

    def test_no_chapter_for_empty_blocks(self, tmp_path):
        """无操作块时不生成章节。"""
        pkg = _build_minimal_package(tmp_path, blocks=[])

        result = build_timeline(pkg)

        assert result.chapter_count == 0
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert len(chapters) == 0

    def test_chapter_idle_threshold_option(self, tmp_path):
        """options.idle_threshold 可调 idle 切章节阈值。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 1.0),
            _make_idle_block(2, 2.0, 3.0),  # 3.0 > 默认 2.0 会切，但调高到 5.0 就不切
            _make_operation_block(3, "mouse_click", 5.0),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)

        # 调高阈值到 5.0，idle 3.0s 不切章节
        result = build_timeline(pkg, options={"idle_threshold": 5.0})

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        assert len(chapters) == 1  # 只有 c001@0.0 "开始"
        assert chapters[0]["primary"]["name"] == "开始"

    def test_chapter_sorted_with_other_blocks(self, tmp_path):
        """章节块与其他块按时间戳统一排序。"""
        blocks = [
            _make_operation_block(1, "mouse_click", 5.0),
            _make_focus_change_block(2, 3.0, "NewApp"),
        ]
        pkg = _build_minimal_package(tmp_path, blocks)
        _write_keyframes(pkg, [_make_keyframe("frames/a.png", 2.0, "timer")])

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        timestamps = [b["timestamp"] for b in timeline["blocks"]]
        assert timestamps == sorted(timestamps)

    def test_chapter_track_is_text(self, tmp_path):
        """章节块独立 "chapter" track（虽然 category=text）。"""
        blocks = [_make_operation_block(1, "mouse_click", 1.0)]
        pkg = _build_minimal_package(tmp_path, blocks)

        result = build_timeline(pkg)

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert "chapter" in timeline["tracks"]
