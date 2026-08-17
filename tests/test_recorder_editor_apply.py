"""Ticket 24 单测：apply_annotation（13 个 action 各自的 apply 逻辑）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.recorder.editor.annotation import Action, Annotation
from lib.recorder.editor.apply import apply_annotation


def _make_blocks() -> list[dict]:
    """构造测试用块列表。"""
    return [
        {"id": "c001", "category": "text", "type": "chapter", "timestamp": 0.0, "duration": 0.0,
         "primary": {"name": "登录"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        {"id": "b001", "category": "operation", "type": "mouse_click", "timestamp": 1.2, "duration": 0.05,
         "primary": {"x": 100, "y": 200}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        {"id": "b002", "category": "operation", "type": "keyboard_input", "timestamp": 1.8, "duration": 0.3,
         "primary": {"text": "admin"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
        {"id": "f001", "category": "image", "type": "screenshot", "timestamp": 1.5, "duration": 0.0,
         "primary": {"frame_path": "frames/001.png"}, "supplements": {}, "status": {"marked_key": False, "marked_anomaly": False, "marked_automatable": False, "is_trimmed": False}},
    ]


def _make_ann(action: Action, target: str, payload: dict | None = None, undone: bool = False) -> Annotation:
    return Annotation(id="a001", target_block_id=target, action=action, payload=payload or {}, undone=undone)


# ===== mark_key / mark_anomaly / mark_automatable =====

def test_mark_key() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.MARK_KEY, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_key"] is True
    assert result[1]["status"]["marked_anomaly"] is False
    # 原列表不修改
    assert blocks[1]["status"]["marked_key"] is False


def test_mark_anomaly() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.MARK_ANOMALY, "b002")
    result = apply_annotation(blocks, ann)
    assert result[2]["status"]["marked_anomaly"] is True


def test_mark_automatable() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.MARK_AUTOMATABLE, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_automatable"] is True


def test_mark_multiple_states_coexist() -> None:
    """D015：多状态可叠加不互斥。"""
    blocks = _make_blocks()
    blocks = apply_annotation(blocks, _make_ann(Action.MARK_KEY, "b001"))
    blocks = apply_annotation(blocks, _make_ann(Action.MARK_ANOMALY, "b001", {}, undone=False))
    blocks = apply_annotation(blocks, _make_ann(Action.MARK_AUTOMATABLE, "b001"))
    assert blocks[1]["status"]["marked_key"] is True
    assert blocks[1]["status"]["marked_anomaly"] is True
    assert blocks[1]["status"]["marked_automatable"] is True


# ===== trim / restore / untrim =====

def test_trim() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.TRIM, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["is_trimmed"] is True


def test_restore() -> None:
    blocks = _make_blocks()
    blocks[1]["status"]["is_trimmed"] = True
    ann = _make_ann(Action.RESTORE, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["is_trimmed"] is False


def test_trim_then_restore() -> None:
    blocks = _make_blocks()
    blocks = apply_annotation(blocks, _make_ann(Action.TRIM, "b001"))
    blocks = apply_annotation(blocks, _make_ann(Action.RESTORE, "b001"))
    assert blocks[1]["status"]["is_trimmed"] is False


def test_untrim() -> None:
    """UNTRIM 与 RESTORE 行为一致（recorder-gui-redesign T1 新增）。"""
    blocks = _make_blocks()
    blocks[1]["status"]["is_trimmed"] = True
    ann = _make_ann(Action.UNTRIM, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["is_trimmed"] is False


def test_trim_then_untrim() -> None:
    """trim → untrim 往返恢复。"""
    blocks = _make_blocks()
    blocks = apply_annotation(blocks, _make_ann(Action.TRIM, "b001"))
    blocks = apply_annotation(blocks, _make_ann(Action.UNTRIM, "b001"))
    assert blocks[1]["status"]["is_trimmed"] is False


def test_untrim_idempotent_on_untrimmed() -> None:
    """对未 trim 的块 untrim 是合法 no-op（幂等）。"""
    blocks = _make_blocks()
    ann = _make_ann(Action.UNTRIM, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["is_trimmed"] is False


# ===== unmark_key / unmark_anomaly / unmark_automatable =====

def test_unmark_key() -> None:
    """标记后 unmark 取消（recorder-gui-redesign T1 新增）。"""
    blocks = _make_blocks()
    blocks[1]["status"]["marked_key"] = True
    ann = _make_ann(Action.UNMARK_KEY, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_key"] is False


def test_unmark_anomaly() -> None:
    blocks = _make_blocks()
    blocks[2]["status"]["marked_anomaly"] = True
    ann = _make_ann(Action.UNMARK_ANOMALY, "b002")
    result = apply_annotation(blocks, ann)
    assert result[2]["status"]["marked_anomaly"] is False


def test_unmark_automatable() -> None:
    blocks = _make_blocks()
    blocks[1]["status"]["marked_automatable"] = True
    ann = _make_ann(Action.UNMARK_AUTOMATABLE, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_automatable"] is False


def test_mark_then_unmark_roundtrip() -> None:
    """mark → unmark → mark 序列重放后 status 正确（T1 验收点）。"""
    blocks = _make_blocks()
    blocks = apply_annotation(blocks, _make_ann(Action.MARK_KEY, "b001"))
    assert blocks[1]["status"]["marked_key"] is True
    blocks = apply_annotation(blocks, _make_ann(Action.UNMARK_KEY, "b001"))
    assert blocks[1]["status"]["marked_key"] is False
    blocks = apply_annotation(blocks, _make_ann(Action.MARK_KEY, "b001"))
    assert blocks[1]["status"]["marked_key"] is True


def test_unmark_idempotent_on_unmarked() -> None:
    """对未标记块 unmark 是合法 no-op（幂等，T1 验收点）。"""
    blocks = _make_blocks()
    # b001 marked_key=False 初始状态
    ann = _make_ann(Action.UNMARK_KEY, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_key"] is False


def test_unmark_does_not_affect_other_marks() -> None:
    """unmark_key 不影响 anomaly/automatable 状态（多状态可叠加不互斥）。"""
    blocks = _make_blocks()
    blocks[1]["status"]["marked_key"] = True
    blocks[1]["status"]["marked_anomaly"] = True
    blocks[1]["status"]["marked_automatable"] = True
    ann = _make_ann(Action.UNMARK_KEY, "b001")
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_key"] is False
    assert result[1]["status"]["marked_anomaly"] is True
    assert result[1]["status"]["marked_automatable"] is True


def test_out_of_order_idempotent() -> None:
    """乱序幂等：连续两次 unmark 等价于一次 unmark。"""
    blocks = _make_blocks()
    blocks[1]["status"]["marked_key"] = True
    blocks = apply_annotation(blocks, _make_ann(Action.UNMARK_KEY, "b001"))
    blocks = apply_annotation(blocks, _make_ann(Action.UNMARK_KEY, "b001"))
    assert blocks[1]["status"]["marked_key"] is False


# ===== insert_note / insert_chapter / insert_image / insert_text =====

def test_insert_note_after() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.INSERT_NOTE, "b001",
                    {"text": "注释内容", "position": "after", "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    assert len(result) == 5
    assert result[2]["id"] == "u001"
    assert result[2]["type"] == "user_note"
    assert result[2]["primary"]["text"] == "注释内容"
    assert abs(result[2]["timestamp"] - 1.201) < 1e-6  # b001 timestamp + 0.001


def test_insert_note_supplement() -> None:
    """supplement 位置：附属到父块，时间戳=父块时间戳。"""
    blocks = _make_blocks()
    ann = _make_ann(Action.INSERT_NOTE, "b001",
                    {"text": "注释", "position": "supplement", "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    assert len(result) == 5
    inserted = result[2]
    assert inserted["id"] == "u001"
    assert inserted["timestamp"] == 1.2  # 父块时间戳
    assert inserted["supplements"]["parent_block_id"] == "b001"


def test_insert_note_timestamp() -> None:
    """timestamp 位置：按时间戳插入正确位置（找到第一个 timestamp > 目标的位置插入）。"""
    blocks = _make_blocks()
    # _make_blocks 定义顺序：c001(0.0) b001(1.2) b002(1.8) f001(1.5)
    # 插入 1.6 时，第一个 >1.6 的是 b002(1.8)，所以 u001 插在 b002 前面
    ann = _make_ann(Action.INSERT_NOTE, "b001",
                    {"text": "注释", "position": "timestamp", "timestamp": 1.6, "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    assert len(result) == 5
    inserted_idx = next(i for i, b in enumerate(result) if b["id"] == "u001")
    assert abs(result[inserted_idx]["timestamp"] - 1.6) < 1e-6
    # 前一个是 b001(1.2)，后一个是 b002(1.8)
    assert result[inserted_idx - 1]["id"] == "b001"
    assert result[inserted_idx + 1]["id"] == "b002"


def test_insert_chapter() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.INSERT_CHAPTER, "b001",
                    {"name": "新章节", "position": "after", "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    inserted = next(b for b in result if b["id"] == "u001")
    assert inserted["type"] == "chapter"
    assert inserted["primary"]["name"] == "新章节"


def test_insert_image() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.INSERT_IMAGE, "b001",
                    {"image_path": "refs/img.png", "position": "after", "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    inserted = next(b for b in result if b["id"] == "u001")
    assert inserted["category"] == "image"
    assert inserted["type"] == "reference_image"
    assert inserted["primary"]["frame_path"] == "refs/img.png"


def test_insert_text() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.INSERT_TEXT, "b001",
                    {"text": "参考文本", "source_file": "refs/note.txt", "position": "after", "inserted_block_id": "u001"})
    result = apply_annotation(blocks, ann)
    inserted = next(b for b in result if b["id"] == "u001")
    assert inserted["type"] == "reference_text"
    assert inserted["primary"]["text"] == "参考文本"
    assert inserted["primary"]["source_file"] == "refs/note.txt"


# ===== rename_chapter =====

def test_rename_chapter() -> None:
    blocks = _make_blocks()
    ann = _make_ann(Action.RENAME_CHAPTER, "c001", {"new_name": "登录操作"})
    result = apply_annotation(blocks, ann)
    assert result[0]["primary"]["name"] == "登录操作"


# ===== merge_blocks =====

def test_merge_blocks() -> None:
    """合并 b001 + b002 → 保留 b001，b002 标记 trimmed + merged_into。"""
    blocks = _make_blocks()
    ann = _make_ann(Action.MERGE_BLOCKS, "b001", {"merged_block_ids": ["b002"]})
    result = apply_annotation(blocks, ann)
    # 主块 supplements 记录 merged_block_ids
    assert result[1]["supplements"]["merged_block_ids"] == ["b002"]
    # b002 标记 trimmed + merged_into
    b002 = next(b for b in result if b["id"] == "b002")
    assert b002["status"]["is_trimmed"] is True
    assert b002["supplements"]["merged_into"] == "b001"


# ===== split_block =====

def test_split_block() -> None:
    """在 timestamp=2.0 拆分 b002（原 1.8-2.1）。"""
    blocks = _make_blocks()
    blocks[2]["duration"] = 0.3  # b002 duration = 0.3 (1.8-2.1)
    ann = _make_ann(Action.SPLIT_BLOCK, "b002", {"split_timestamp": 2.0})
    result = apply_annotation(blocks, ann)
    assert len(result) == 5
    # 原块 duration 改为 0.2 (1.8-2.0)
    b002 = next(b for b in result if b["id"] == "b002")
    assert abs(b002["duration"] - 0.2) < 1e-6
    # 新块 b002_split 从 2.0 开始
    b002_split = next(b for b in result if b["id"] == "b002_split")
    assert abs(b002_split["timestamp"] - 2.0) < 1e-6
    assert abs(b002_split["duration"] - 0.1) < 1e-6
    assert b002_split["supplements"]["split_from"] == "b002"


# ===== move_supplement =====

def test_move_supplement() -> None:
    """D046：素材块时间戳跟随新父块。"""
    blocks = _make_blocks()
    # f001 时间戳 1.5，移到 b002（1.8）下
    ann = _make_ann(Action.MOVE_SUPPLEMENT, "f001", {"new_parent_block_id": "b002"})
    result = apply_annotation(blocks, ann)
    f001 = next(b for b in result if b["id"] == "f001")
    assert f001["timestamp"] == 1.8  # 跟随新父块 b002
    assert f001["supplements"]["parent_block_id"] == "b002"


# ===== undone 跳过 =====

def test_undone_annotation_skipped() -> None:
    """undone=True 的 annotation apply 时返回原列表。"""
    blocks = _make_blocks()
    ann = _make_ann(Action.MARK_KEY, "b001", undone=True)
    result = apply_annotation(blocks, ann)
    assert result[1]["status"]["marked_key"] is False


# ===== 找不到块时安全降级 =====

def test_target_block_not_found() -> None:
    """目标块不存在时 apply 不报错，返回原列表。"""
    blocks = _make_blocks()
    ann = _make_ann(Action.MARK_KEY, "b999")
    result = apply_annotation(blocks, ann)
    assert len(result) == 4
    # 所有块状态不变
    for b in result:
        assert b["status"]["marked_key"] is False
