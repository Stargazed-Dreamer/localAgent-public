"""Ticket 25 单测：merge_timeline_annotations（L4 agent 核心入口）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.recorder.editor.merge import is_finalized, merge_timeline_annotations

# 真实录制包路径（被 .gitignore 排除，CI 环境自动跳过）
RECORDINGS_DIR = Path("workspace/recorder/recordings")
REAL_PACKAGES = [
    RECORDINGS_DIR / "rec_20260723_141608",
    RECORDINGS_DIR / "rec_20260723_161415",
    RECORDINGS_DIR / "rec_20260723_162924",
]
_HAS_REAL_PACKAGES = all(p.exists() for p in REAL_PACKAGES)
_skip_if_no_real = pytest.mark.skipif(
    not _HAS_REAL_PACKAGES,
    reason="真实录制包不存在（被 .gitignore 排除，CI 环境跳过）",
)


def _write_timeline(tmp_path: Path, blocks: list[dict]) -> None:
    """写最小 timeline.json。"""
    timeline = {
        "recording_id": tmp_path.name,
        "duration_seconds": 10.0,
        "block_count": len(blocks),
        "tracks": ["operation", "image", "text", "chapter"],
        "blocks": blocks,
    }
    with (tmp_path / "timeline.json").open("w", encoding="utf-8") as f:
        json.dump(timeline, f)


def _write_annotations(tmp_path: Path, finalized: bool, annotations: list[dict]) -> None:
    """写 annotations.json。"""
    data = {"finalized": finalized, "annotations": annotations}
    with (tmp_path / "annotations.json").open("w", encoding="utf-8") as f:
        json.dump(data, f)


def _make_blocks() -> list[dict]:
    return [
        {"id": "c001", "category": "text", "type": "chapter", "timestamp": 0.0, "duration": 0.0,
         "primary": {"name": "登录"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        {"id": "b001", "category": "operation", "type": "mouse_click", "timestamp": 1.2, "duration": 0.05,
         "primary": {"x": 100, "y": 200}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        {"id": "b002", "category": "operation", "type": "keyboard_input", "timestamp": 1.8, "duration": 0.3,
         "primary": {"text": "admin"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
    ]


# ===== 基础 merge 逻辑 =====

def test_merge_no_annotations_returns_original(tmp_path: Path) -> None:
    """缺失 annotations.json 时返回原始 timeline + finalized=false。"""
    _write_timeline(tmp_path, _make_blocks())
    result = merge_timeline_annotations(tmp_path)
    assert result["finalized"] is False
    assert result["block_count"] == 3
    assert len(result["blocks"]) == 3


def test_merge_with_mark_key(tmp_path: Path) -> None:
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "b001", "action": "mark_key", "payload": {}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    b001 = next(b for b in result["blocks"] if b["id"] == "b001")
    assert b001["status"]["marked_key"] is True


def test_merge_filters_trimmed_blocks(tmp_path: Path) -> None:
    """trim 的块不出现在 merge 视图中。"""
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "b002", "action": "trim", "payload": {}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    assert result["block_count"] == 2
    block_ids = [b["id"] for b in result["blocks"]]
    assert "b002" not in block_ids


def test_merge_filters_undone_annotations(tmp_path: Path) -> None:
    """undone 的 annotation 不 apply。"""
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "b001", "action": "mark_key", "payload": {}, "created_at": "", "undone": True},
    ])
    result = merge_timeline_annotations(tmp_path)
    b001 = next(b for b in result["blocks"] if b["id"] == "b001")
    assert b001["status"]["marked_key"] is False


def test_merge_finalized_field(tmp_path: Path) -> None:
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, True, [])
    result = merge_timeline_annotations(tmp_path)
    assert result["finalized"] is True


def test_merge_rename_chapter(tmp_path: Path) -> None:
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "c001", "action": "rename_chapter", "payload": {"new_name": "登录操作"}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    c001 = next(b for b in result["blocks"] if b["id"] == "c001")
    assert c001["primary"]["name"] == "登录操作"


def test_merge_insert_note(tmp_path: Path) -> None:
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "b001", "action": "insert_note",
         "payload": {"text": "注释", "position": "after", "inserted_block_id": "u001"}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    assert result["block_count"] == 4
    u001 = next(b for b in result["blocks"] if b["id"] == "u001")
    assert u001["type"] == "user_note"


def test_merge_multiple_states_coexist(tmp_path: Path) -> None:
    """多 action 叠加：同一块上多个 mark_* 共存。"""
    _write_timeline(tmp_path, _make_blocks())
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "b001", "action": "mark_key", "payload": {}, "created_at": "", "undone": False},
        {"id": "a002", "target_block_id": "b001", "action": "mark_anomaly", "payload": {}, "created_at": "", "undone": False},
        {"id": "a003", "target_block_id": "b001", "action": "mark_automatable", "payload": {}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    b001 = next(b for b in result["blocks"] if b["id"] == "b001")
    assert b001["status"]["marked_key"] is True
    assert b001["status"]["marked_anomaly"] is True
    assert b001["status"]["marked_automatable"] is True


def test_merge_move_supplement(tmp_path: Path) -> None:
    """move_supplement：素材块时间戳跟随新父块。"""
    blocks = _make_blocks()
    blocks.append({"id": "f001", "category": "image", "type": "screenshot", "timestamp": 1.5, "duration": 0.0,
                   "primary": {"frame_path": "001.png"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}})
    _write_timeline(tmp_path, blocks)
    _write_annotations(tmp_path, False, [
        {"id": "a001", "target_block_id": "f001", "action": "move_supplement",
         "payload": {"new_parent_block_id": "b002"}, "created_at": "", "undone": False},
    ])
    result = merge_timeline_annotations(tmp_path)
    f001 = next(b for b in result["blocks"] if b["id"] == "f001")
    assert f001["timestamp"] == 1.8  # 跟随 b002


# ===== is_finalized =====

def test_is_finalized_no_file(tmp_path: Path) -> None:
    assert is_finalized(tmp_path) is False


def test_is_finalized_true(tmp_path: Path) -> None:
    _write_annotations(tmp_path, True, [])
    assert is_finalized(tmp_path) is True


def test_is_finalized_false(tmp_path: Path) -> None:
    _write_annotations(tmp_path, False, [])
    assert is_finalized(tmp_path) is False


# ===== 缺失 timeline.json =====

def test_merge_no_timeline(tmp_path: Path) -> None:
    """缺失 timeline.json 时返回空视图。"""
    result = merge_timeline_annotations(tmp_path)
    assert result["finalized"] is False
    assert result["block_count"] == 0
    assert result["blocks"] == []


# ===== 真实录制包测试 =====

@_skip_if_no_real
class TestMergeRealPackages:
    """用真实录制包验证 merge 逻辑。"""

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_merge_no_annotations(self, pkg_name: str) -> None:
        """无 annotations.json 时返回原始 timeline。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        assert result["finalized"] is False
        assert result["block_count"] > 0
        assert len(result["blocks"]) == result["block_count"]

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_merge_preserves_tracks(self, pkg_name: str) -> None:
        """merge 后 tracks 字段保留。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        assert "operation" in result["tracks"]
        assert "chapter" in result["tracks"]

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_merge_recording_id(self, pkg_name: str) -> None:
        """merge 后 recording_id 正确。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        assert result["recording_id"] == pkg_name
