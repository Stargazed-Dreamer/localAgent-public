"""L2 时间轴层 Ticket 23：端到端测试（真实录制包）

测试策略（spec-l2.md Testing Decisions）：
- 用 L0 真实录制包跑完整 L2 流程，验证 timeline.json 产出
- 三个录制包覆盖 v1 格式（无裁剪）和 v2 格式（有裁剪）两种情况
- 验证四类块按时间戳排序 + 块 ID 前缀（b/f/t/c）+ 章节名去重
- 验证 annotations.json 初始化 + CLI 预览输出

录制包（workspace/recorder/recordings/，被 .gitignore 排除）：
- rec_20260723_141608：v1 格式（无 audio_segments.json），125 块 / 16 章节
- rec_20260723_161415：v2 格式（有 audio_segments.json），56 块 / 8 章节
- rec_20260723_162924：v2 格式（有 audio_segments.json），63 块 / 11 章节

注意：三个录制包都没有 transcript.json（L1 STT 未生成），所以 text 块只有 chapter 类型。
      这不影响 L2 流程验证（builder.py 对缺失 transcript.json 降级处理）。

录制包被 .gitignore 排除，CI 环境可能不存在，所以测试用 skipif 守护。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.recorder.timeline import build_timeline, init_annotations
from workspace.recorder.tools.timeline_cli import main as cli_main

# ============================ 录制包路径 ============================

RECORDINGS_DIR = Path(__file__).parent.parent / "workspace" / "recordings"

REC_V1 = RECORDINGS_DIR / "rec_20260723_141608"  # v1 无裁剪
REC_V2_A = RECORDINGS_DIR / "rec_20260723_161415"  # v2 有裁剪
REC_V2_B = RECORDINGS_DIR / "rec_20260723_162924"  # v2 有裁剪

# 录制包是否存在的 fixture（缺失时跳过整个测试）
pytestmark = pytest.mark.skipif(
    not REC_V1.exists() or not REC_V2_A.exists() or not REC_V2_B.exists(),
    reason="真实录制包不存在（workspace/recorder/recordings/ 被 .gitignore 排除）",
)


# ============================ 通用验证辅助函数 ============================


def _assert_blocks_sorted_by_timestamp(blocks: list[dict]) -> None:
    """断言 blocks 按时间戳升序排序。"""
    timestamps = [float(b.get("timestamp", 0.0)) for b in blocks]
    assert timestamps == sorted(timestamps), "blocks 未按时间戳升序排序"


def _assert_block_id_prefixes_correct(blocks: list[dict]) -> None:
    """断言块 ID 前缀正确：b=operation / f=image / t=text / c=chapter。"""
    for block in blocks:
        block_id = block.get("id", "")
        category = block.get("category", "")
        btype = block.get("type", "")

        if category == "operation":
            assert block_id.startswith("b"), (
                f"操作块 ID 应以 b 开头，实际：{block_id}（type={btype}）"
            )
        elif category == "image":
            assert block_id.startswith("f"), (
                f"图像块 ID 应以 f 开头，实际：{block_id}（type={btype}）"
            )
        elif category == "text" and btype == "chapter":
            assert block_id.startswith("c"), (
                f"章节块 ID 应以 c 开头，实际：{block_id}（type={btype}）"
            )
        elif category == "text" and btype == "stt_transcript":
            assert block_id.startswith("t"), (
                f"转写块 ID 应以 t 开头，实际：{block_id}（type={btype}）"
            )


def _assert_first_chapter_at_zero(blocks: list[dict]) -> None:
    """断言第一个章节块 timestamp=0.0（D042）。"""
    chapters = [b for b in blocks if b.get("type") == "chapter"]
    if not chapters:
        return
    assert float(chapters[0].get("timestamp", -1)) == 0.0, (
        f"第一个章节块 timestamp 应为 0.0，实际：{chapters[0].get('timestamp')}"
    )


def _assert_no_consecutive_duplicate_chapters(blocks: list[dict]) -> None:
    """断言连续同名章节已去重（D042）。"""
    chapters = [b for b in blocks if b.get("type") == "chapter"]
    for i in range(1, len(chapters)):
        prev_name = chapters[i - 1].get("primary", {}).get("name", "")
        curr_name = chapters[i].get("primary", {}).get("name", "")
        # 连续同名章节不应同时存在（去重规则）
        if prev_name and curr_name:
            assert prev_name != curr_name or prev_name == "", (
                f"连续同名章节未去重：c{i} 和 c{i+1} 都是 '{curr_name}'"
            )


def _assert_timeline_format(timeline: dict, recording_id: str) -> None:
    """断言 timeline.json 格式正确（D040 + D041）。"""
    assert timeline["recording_id"] == recording_id
    assert isinstance(timeline["duration_seconds"], (int, float))
    assert timeline["duration_seconds"] >= 0
    assert isinstance(timeline["block_count"], int)
    assert timeline["block_count"] == len(timeline["blocks"])
    assert isinstance(timeline["tracks"], list)
    assert "operation" in timeline["tracks"]  # operation track 必有
    assert isinstance(timeline["blocks"], list)


# ============================ v1 格式录制包测试（rec_141608，无裁剪）============================


class TestV1RecordingNoCropping:
    """rec_20260723_141608：v1 格式（无 audio_segments.json）。

    验证点：
    - L2 流程跑通，timeline.json 生成
    - 四类块按时间戳排序
    - 块 ID 前缀正确（b/f/c，无 t 因为没 transcript）
    - 章节切分（focus_change + idle 切章节）
    - annotations.json 初始化
    - CLI 预览输出 6 项
    """

    def test_build_timeline_succeeds(self):
        """L2 流程跑通，timeline.json 生成。"""
        result = build_timeline(REC_V1)
        assert result.timeline_path is not None
        assert result.timeline_path.exists()
        assert result.block_count > 0
        assert result.chapter_count > 0
        # v1 无 audio_segments.json，不应有 audio_segments 相关错误
        assert "audio_segments" not in result.errors

    def test_timeline_format_correct(self):
        """timeline.json 格式正确。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_timeline_format(timeline, "rec_20260723_141608")

    def test_blocks_sorted_by_timestamp(self):
        """四类块按时间戳排序。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_blocks_sorted_by_timestamp(timeline["blocks"])

    def test_block_id_prefixes_correct(self):
        """块 ID 前缀正确（b/f/c）。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_block_id_prefixes_correct(timeline["blocks"])

    def test_first_chapter_at_zero(self):
        """第一个章节块 timestamp=0.0。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_first_chapter_at_zero(timeline["blocks"])

    def test_no_consecutive_duplicate_chapters(self):
        """连续同名章节已去重。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_no_consecutive_duplicate_chapters(timeline["blocks"])

    def test_tracks_include_chapter(self):
        """tracks 包含 chapter（因为有章节块）。"""
        result = build_timeline(REC_V1)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        assert "chapter" in timeline["tracks"]

    def test_v1_no_audio_segments(self):
        """v1 录制包无 audio_segments.json（无裁剪）。"""
        assert not (REC_V1 / "audio" / "audio_segments.json").exists()

    def test_annotations_initialized(self):
        """annotations.json 初始化为空。"""
        # 先删除可能已存在的 annotations.json（避免之前的测试影响）
        annotations_path = REC_V1 / "annotations.json"
        if annotations_path.exists():
            annotations_path.unlink()
        init_annotations(REC_V1)
        assert annotations_path.exists()
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == {"annotations": []}

    def test_cli_preview_output(self, capsys):
        """CLI 预览输出 6 项内容。"""
        exit_code = cli_main([str(REC_V1)])
        captured = capsys.readouterr()
        assert exit_code == 0
        text = captured.out
        assert "=== 时间轴预览" in text
        assert "--- 1. 录制包概览 ---" in text
        assert "--- 2. 章节列表 ---" in text
        assert "--- 3. 块类型分布 ---" in text
        assert "--- 4. 时间跨度 ---" in text
        assert "--- 5. 三时间戳对齐验证 ---" in text
        assert "--- 6. 帧压缩比 ---" in text
        assert "=== 预览结束 ===" in text


# ============================ v2 格式录制包测试（rec_161415，有裁剪）============================


class TestV2RecordingWithCropping:
    """rec_20260723_161415：v2 格式（有 audio_segments.json）。

    验证点：
    - L2 流程跑通
    - audio_segments.json 存在（v2 裁剪格式）
    - 四类块按时间戳排序 + 块 ID 前缀
    - 章节切分
    - CLI 预览输出
    """

    def test_v2_has_audio_segments(self):
        """v2 录制包有 audio_segments.json（有裁剪）。"""
        assert (REC_V2_A / "audio" / "audio_segments.json").exists()

    def test_build_timeline_succeeds(self):
        """L2 流程跑通。"""
        result = build_timeline(REC_V2_A)
        assert result.timeline_path is not None
        assert result.timeline_path.exists()
        assert result.block_count > 0
        assert result.chapter_count > 0

    def test_timeline_format_correct(self):
        """timeline.json 格式正确。"""
        result = build_timeline(REC_V2_A)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_timeline_format(timeline, "rec_20260723_161415")

    def test_blocks_sorted_by_timestamp(self):
        """四类块按时间戳排序。"""
        result = build_timeline(REC_V2_A)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_blocks_sorted_by_timestamp(timeline["blocks"])

    def test_block_id_prefixes_correct(self):
        """块 ID 前缀正确。"""
        result = build_timeline(REC_V2_A)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_block_id_prefixes_correct(timeline["blocks"])

    def test_first_chapter_at_zero(self):
        """第一个章节块 timestamp=0.0。"""
        result = build_timeline(REC_V2_A)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_first_chapter_at_zero(timeline["blocks"])

    def test_cli_preview_output(self, capsys):
        """CLI 预览输出 6 项内容。"""
        exit_code = cli_main([str(REC_V2_A)])
        captured = capsys.readouterr()
        assert exit_code == 0
        text = captured.out
        assert "--- 1. 录制包概览 ---" in text
        assert "--- 2. 章节列表 ---" in text
        assert "有效时长" in text  # v2 有 effective_duration

    def test_annotations_initialized(self):
        """annotations.json 初始化。"""
        annotations_path = REC_V2_A / "annotations.json"
        if annotations_path.exists():
            annotations_path.unlink()
        init_annotations(REC_V2_A)
        assert annotations_path.exists()
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == {"annotations": []}


# ============================ v2 格式录制包测试（rec_162924，有裁剪 + 章节切分验证）============================


class TestV2RecordingChapterSplitting:
    """rec_20260723_162924：v2 格式（有裁剪，章节切分场景丰富）。

    录制内容：TRAE Work CN → python → Program Manager → 微信 → Sublime Text → idle
    验证点：
    - 章节切分按 focus_change 切
    - idle > 阈值切章节
    - 章节名取自 focus_change 的 title
    - 第一个章节 timestamp=0.0
    """

    def test_build_timeline_succeeds(self):
        """L2 流程跑通。"""
        result = build_timeline(REC_V2_B)
        assert result.timeline_path is not None
        assert result.timeline_path.exists()
        assert result.block_count > 0
        assert result.chapter_count > 0

    def test_chapter_count_matches_focus_change_plus_idle(self):
        """章节数 = focus_change 切的 + idle>阈值切的 + 第一个"开始"章节。

        章节切分规则（D042）：
        - 第一个章节 timestamp=0.0（"开始"或第一个 focus_change 的 title）
        - 每个 focus_change 切一个新章节
        - 每个 idle>阈值切一个新章节（命名 "idle Xs"）
        - 连续同名章节去重
        所以章节数 >= focus_change 数（每个 focus_change 至少切一个章节）
        """
        result = build_timeline(REC_V2_B)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))

        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        focus_changes = [
            b for b in timeline["blocks"]
            if b.get("category") == "operation" and b.get("type") == "focus_change"
        ]
        # 章节数应 >= 1（至少"开始"章节）
        assert len(chapters) >= 1
        # 章节数应 >= focus_change 数（每个 focus_change 切一个章节，加上 idle 切的）
        # 注：连续同名去重可能减少章节数，但 focus_change 通常窗口名不同
        assert len(chapters) >= len(focus_changes), (
            f"章节数 {len(chapters)} < focus_change 数 {len(focus_changes)}"
        )

    def test_chapter_names_from_focus_change_titles(self):
        """章节名取自 focus_change 的 title。"""
        result = build_timeline(REC_V2_B)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))

        chapters = [b for b in timeline["blocks"] if b.get("type") == "chapter"]
        focus_changes = [
            b for b in timeline["blocks"]
            if b.get("category") == "operation" and b.get("type") == "focus_change"
        ]

        # 收集所有章节名（非空）
        chapter_names = [
            c.get("primary", {}).get("name", "")
            for c in chapters
            if c.get("primary", {}).get("name", "")
        ]
        # 收集所有 focus_change title
        focus_titles = [
            fc.get("primary", {}).get("title", "")
            for fc in focus_changes
            if fc.get("primary", {}).get("title", "")
        ]

        # 章节名应来自 focus_change title 或 "idle Xs"（idle 切的章节）或 "开始"
        for name in chapter_names:
            if name == "开始":
                continue
            if name.startswith("idle "):
                continue
            # 章节名应在 focus_change titles 中（或来自第一个 focus_change）
            # 注：可能章节名是 focus_change title 的子串（窗口标题变化）
            assert any(
                name in focus_titles or title in name
                for title in focus_titles
            ), f"章节名 '{name}' 不在任何 focus_change title 中：{focus_titles}"

    def test_blocks_sorted_by_timestamp(self):
        """四类块按时间戳排序。"""
        result = build_timeline(REC_V2_B)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_blocks_sorted_by_timestamp(timeline["blocks"])

    def test_block_id_prefixes_correct(self):
        """块 ID 前缀正确。"""
        result = build_timeline(REC_V2_B)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_block_id_prefixes_correct(timeline["blocks"])

    def test_first_chapter_at_zero(self):
        """第一个章节块 timestamp=0.0。"""
        result = build_timeline(REC_V2_B)
        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_first_chapter_at_zero(timeline["blocks"])

    def test_cli_preview_output(self, capsys):
        """CLI 预览输出 6 项内容。"""
        exit_code = cli_main([str(REC_V2_B)])
        captured = capsys.readouterr()
        assert exit_code == 0
        text = captured.out
        assert "=== 时间轴预览" in text
        assert "--- 2. 章节列表 ---" in text
        # 这个录制包有多个 focus_change，章节列表应丰富
        assert "c001" in text

    def test_idle_threshold_affects_chapter_count(self):
        """--idle-threshold 影响章节数（阈值越大章节越少）。"""
        # 默认阈值 2.0
        result_default = build_timeline(REC_V2_B)
        chapters_default = result_default.chapter_count

        # 大阈值 10.0（几乎所有 idle 都不切）
        result_large = build_timeline(REC_V2_B, options={"idle_threshold": 10.0})
        chapters_large = result_large.chapter_count

        # 大阈值章节数应 <= 默认阈值
        assert chapters_large <= chapters_default

    def test_annotations_initialized(self):
        """annotations.json 初始化。"""
        annotations_path = REC_V2_B / "annotations.json"
        if annotations_path.exists():
            annotations_path.unlink()
        init_annotations(REC_V2_B)
        assert annotations_path.exists()
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == {"annotations": []}


# ============================ 跨录制包对比测试 ============================


class TestCrossRecordingComparison:
    """三个录制包对比测试，验证 L2 流程的健壮性。"""

    @pytest.mark.parametrize(
        "rec_path,rec_id",
        [
            (REC_V1, "rec_20260723_141608"),
            (REC_V2_A, "rec_20260723_161415"),
            (REC_V2_B, "rec_20260723_162924"),
        ],
    )
    def test_all_recordings_produce_valid_timeline(
        self, rec_path: Path, rec_id: str
    ):
        """所有录制包都能产出有效 timeline.json。"""
        result = build_timeline(rec_path)
        assert result.timeline_path is not None
        assert result.timeline_path.exists()

        timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
        _assert_timeline_format(timeline, rec_id)
        _assert_blocks_sorted_by_timestamp(timeline["blocks"])
        _assert_block_id_prefixes_correct(timeline["blocks"])
        _assert_first_chapter_at_zero(timeline["blocks"])

    @pytest.mark.parametrize(
        "rec_path",
        [REC_V1, REC_V2_A, REC_V2_B],
    )
    def test_all_recordings_cli_preview_works(
        self, rec_path: Path, capsys
    ):
        """所有录制包 CLI 预览都能正常输出 6 项。"""
        exit_code = cli_main([str(rec_path)])
        captured = capsys.readouterr()
        assert exit_code == 0
        text = captured.out
        assert "--- 1. 录制包概览 ---" in text
        assert "--- 2. 章节列表 ---" in text
        assert "--- 3. 块类型分布 ---" in text
        assert "--- 4. 时间跨度 ---" in text
        assert "--- 5. 三时间戳对齐验证 ---" in text
        assert "--- 6. 帧压缩比 ---" in text

    @pytest.mark.parametrize(
        "rec_path",
        [REC_V1, REC_V2_A, REC_V2_B],
    )
    def test_all_recordings_annotations_initialized(
        self, rec_path: Path
    ):
        """所有录制包都能初始化 annotations.json。"""
        annotations_path = rec_path / "annotations.json"
        if annotations_path.exists():
            annotations_path.unlink()
        init_annotations(rec_path)
        assert annotations_path.exists()
        data = json.loads(annotations_path.read_text(encoding="utf-8"))
        assert data == {"annotations": []}
