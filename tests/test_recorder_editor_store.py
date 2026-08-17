"""Ticket 24 单测：AnnotationStore（add/undo/redo/finalize/save/load/stats）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.recorder.editor.annotation import Action, Annotation
from lib.recorder.editor.store import AnnotationStore


def _make_ann(action: Action = Action.MARK_KEY, target: str = "b001") -> Annotation:
    return Annotation(id="", target_block_id=target, action=action, payload={})


# ===== add =====

def test_add_generates_id() -> None:
    store = AnnotationStore()
    ann = store.add(_make_ann())
    assert ann.id == "a001"
    ann2 = store.add(_make_ann())
    assert ann2.id == "a002"


def test_add_pushes_undo_stack_clears_redo() -> None:
    store = AnnotationStore()
    store.add(_make_ann())
    store.undo()
    assert len(store._redo_stack) == 1
    assert len(store._undo_stack) == 0  # undo 弹出后 undo_stack 为空
    # add 后 redo 栈清空
    store.add(_make_ann(Action.MARK_ANOMALY))
    assert len(store._redo_stack) == 0
    assert len(store._undo_stack) == 1  # 只有新 add 的这条


# ===== undo / redo =====

def test_undo_marks_undone() -> None:
    store = AnnotationStore()
    store.add(_make_ann())
    assert store.undo() is True
    assert store.annotations[0].undone is True
    assert len(store.active_annotations) == 0


def test_undo_empty_stack_returns_false() -> None:
    store = AnnotationStore()
    assert store.undo() is False


def test_redo_restores() -> None:
    store = AnnotationStore()
    store.add(_make_ann())
    store.undo()
    assert store.redo() is True
    assert store.annotations[0].undone is False
    assert len(store.active_annotations) == 1


def test_redo_empty_stack_returns_false() -> None:
    store = AnnotationStore()
    assert store.redo() is False


def test_undo_redo_sequence() -> None:
    store = AnnotationStore()
    store.add(_make_ann(Action.MARK_KEY, "b001"))
    store.add(_make_ann(Action.MARK_ANOMALY, "b002"))
    # undo 两次
    assert store.undo() is True  # 撤销 b002
    assert store.undo() is True  # 撤销 b001
    assert len(store.active_annotations) == 0
    # redo 两次
    assert store.redo() is True  # 恢复 b001
    assert store.redo() is True  # 恢复 b002
    assert len(store.active_annotations) == 2
    assert store.active_annotations[0].action == Action.MARK_KEY
    assert store.active_annotations[1].action == Action.MARK_ANOMALY


# ===== finalize =====

def test_finalize() -> None:
    store = AnnotationStore()
    assert store.finalized is False
    store.finalize()
    assert store.finalized is True


# ===== save / load =====

def test_save_load_roundtrip(tmp_path: Path) -> None:
    store = AnnotationStore()
    store.add(_make_ann(Action.MARK_KEY, "b001"))
    store.add(_make_ann(Action.MARK_ANOMALY, "b002"))
    store.finalize()
    ann_path = tmp_path / "annotations.json"
    store.save(ann_path)

    # 加载验证
    store2 = AnnotationStore()
    store2.load(ann_path)
    assert store2.finalized is True
    assert len(store2.annotations) == 2
    assert store2.annotations[0].action == Action.MARK_KEY
    assert store2.annotations[1].action == Action.MARK_ANOMALY


def test_save_filters_undone(tmp_path: Path) -> None:
    """D050：保存时过滤 undone 记录，保持干净。"""
    store = AnnotationStore()
    store.add(_make_ann(Action.MARK_KEY, "b001"))
    store.add(_make_ann(Action.MARK_ANOMALY, "b002"))
    store.undo()  # 撤销 b002
    ann_path = tmp_path / "annotations.json"
    store.save(ann_path)

    with ann_path.open(encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["annotations"]) == 1
    assert data["annotations"][0]["action"] == "mark_key"


def test_load_nonexistent_file(tmp_path: Path) -> None:
    """缺失 annotations.json 时 load 不报错。"""
    store = AnnotationStore()
    store.load(tmp_path / "nonexistent.json")
    assert len(store.annotations) == 0
    assert store.finalized is False


# ===== stats（D047 诊断用）=====

def test_stats() -> None:
    store = AnnotationStore()
    store.add(_make_ann(Action.MARK_KEY, "b001"))
    store.add(_make_ann(Action.MARK_KEY, "b002"))
    store.add(_make_ann(Action.TRIM, "b003"))
    stats = store.stats()
    assert stats["mark_key"] == 2
    assert stats["trim"] == 1


def test_stats_excludes_undone() -> None:
    store = AnnotationStore()
    store.add(_make_ann(Action.MARK_KEY, "b001"))
    store.add(_make_ann(Action.MARK_ANOMALY, "b002"))
    store.undo()  # 撤销 b002
    stats = store.stats()
    assert "mark_key" in stats
    assert "mark_anomaly" not in stats
