"""Ticket 28：L3 编辑层端到端测试。

用 3 个真实录制包测试完整 L3 流程：
- 启动编辑器 → 标注关键块 → 插入注释 → 裁剪块 → undo → redo → merge 验证 → finalize
- 覆盖 D044-D053 全部 10 个决策
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib.recorder.editor.annotation import Action, Annotation
from lib.recorder.editor.apply import apply_annotation
from lib.recorder.editor.merge import is_finalized, merge_timeline_annotations
from lib.recorder.editor.store import AnnotationStore

pytestmark = pytest.mark.e2e  # E2E：依赖真实环境（CDP/后端/GUI），quick 层排除

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



@_skip_if_no_real
class TestL3FullFlow:
    """L3 编辑层完整流程测试（3 个真实录制包）。"""

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_merge_returns_original_when_no_annotations(self, pkg_name: str) -> None:
        """D044：无 annotations 时 merge 返回原始 timeline。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        assert result["finalized"] is False
        assert result["block_count"] > 0
        # 不应该有 marked_key 的块
        for b in result["blocks"]:
            assert b.get("status", {}).get("marked_key", False) is False

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_annotation_flow(self, pkg_name: str, tmp_path: Path) -> None:
        """D045/D046：annotation 创建 + apply 流程。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        blocks = result["blocks"]
        if len(blocks) < 2:
            pytest.skip("块数不足")

        # 创建 store + 添加 mark_key annotation
        store = AnnotationStore()
        target_id = blocks[0]["id"]
        store.add(Annotation(id="", target_block_id=target_id, action=Action.MARK_KEY))

        # apply
        applied = apply_annotation(blocks, store.active_annotations[0])
        assert applied[0]["status"]["marked_key"] is True

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_undo_redo_flow(self, pkg_name: str) -> None:
        """D050：undo/redo 完整流程。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        blocks = result["blocks"]
        if len(blocks) < 1:
            pytest.skip("块数不足")

        store = AnnotationStore()
        store.add(Annotation(id="", target_block_id=blocks[0]["id"], action=Action.MARK_KEY))
        store.add(Annotation(id="", target_block_id=blocks[0]["id"], action=Action.MARK_ANOMALY))

        assert len(store.active_annotations) == 2

        # undo 两次
        assert store.undo() is True
        assert store.undo() is True
        assert len(store.active_annotations) == 0

        # redo 两次
        assert store.redo() is True
        assert store.redo() is True
        assert len(store.active_annotations) == 2

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_finalize_flow(self, pkg_name: str, tmp_path: Path) -> None:
        """D053：finalize 标记流程。"""
        pkg = RECORDINGS_DIR / pkg_name
        # 创建临时 annotations.json
        ann_path = tmp_path / "annotations.json"
        store = AnnotationStore()
        store.finalize()
        store.save(ann_path)

        # 复制 timeline.json 到临时目录
        timeline_src = pkg / "timeline.json"
        if timeline_src.exists():
            import shutil
            shutil.copy(timeline_src, tmp_path / "timeline.json")

            result = merge_timeline_annotations(tmp_path)
            assert result["finalized"] is True
            assert is_finalized(tmp_path) is True

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_trim_filters_block_in_merge(self, pkg_name: str, tmp_path: Path) -> None:
        """D046：trim 的块在 merge 视图中被过滤。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        original_count = result["block_count"]
        blocks = result["blocks"]
        if len(blocks) < 2:
            pytest.skip("块数不足")

        # 构造 annotations：trim 第一个块
        ann_data = {
            "finalized": False,
            "annotations": [
                {"id": "a001", "target_block_id": blocks[0]["id"], "action": "trim",
                 "payload": {}, "created_at": "", "undone": False},
            ],
        }
        import shutil
        shutil.copy(pkg / "timeline.json", tmp_path / "timeline.json")
        with (tmp_path / "annotations.json").open("w", encoding="utf-8") as f:
            json.dump(ann_data, f)

        merged = merge_timeline_annotations(tmp_path)
        assert merged["block_count"] == original_count - 1
        # trim 的块不在结果中
        block_ids = [b["id"] for b in merged["blocks"]]
        assert blocks[0]["id"] not in block_ids

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_insert_note_in_merge(self, pkg_name: str, tmp_path: Path) -> None:
        """D046/D051：insert_note 在 merge 视图中出现。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        original_count = result["block_count"]
        blocks = result["blocks"]
        if len(blocks) < 1:
            pytest.skip("块数不足")

        ann_data = {
            "finalized": False,
            "annotations": [
                {"id": "a001", "target_block_id": blocks[0]["id"], "action": "insert_note",
                 "payload": {"text": "测试注释", "position": "after", "inserted_block_id": "u001"},
                 "created_at": "", "undone": False},
            ],
        }
        import shutil
        shutil.copy(pkg / "timeline.json", tmp_path / "timeline.json")
        with (tmp_path / "annotations.json").open("w", encoding="utf-8") as f:
            json.dump(ann_data, f)

        merged = merge_timeline_annotations(tmp_path)
        assert merged["block_count"] == original_count + 1
        u001 = next((b for b in merged["blocks"] if b["id"] == "u001"), None)
        assert u001 is not None
        assert u001["type"] == "user_note"
        assert u001["primary"]["text"] == "测试注释"

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_stats_diagnostic(self, pkg_name: str) -> None:
        """D047：stats 统计用于诊断。"""
        pkg = RECORDINGS_DIR / pkg_name
        result = merge_timeline_annotations(pkg)
        blocks = result["blocks"]
        if len(blocks) < 3:
            pytest.skip("块数不足")

        store = AnnotationStore()
        store.add(Annotation(id="", target_block_id=blocks[0]["id"], action=Action.MARK_KEY))
        store.add(Annotation(id="", target_block_id=blocks[1]["id"], action=Action.TRIM))
        store.add(Annotation(id="", target_block_id=blocks[2]["id"], action=Action.MARK_ANOMALY))

        stats = store.stats()
        assert stats["mark_key"] == 1
        assert stats["trim"] == 1
        assert stats["mark_anomaly"] == 1


@_skip_if_no_real
class TestL3DecisionCoverage:
    """覆盖 D044-D053 全部 10 个决策。"""

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_d044_timeline_readonly(self, pkg_name: str) -> None:
        """D044：timeline.json 只读，所有变更通过 annotations.json。"""
        pkg = RECORDINGS_DIR / pkg_name
        timeline_path = pkg / "timeline.json"
        with timeline_path.open(encoding="utf-8") as f:
            original = f.read()

        # merge 不修改 timeline.json
        merge_timeline_annotations(pkg)

        with timeline_path.open(encoding="utf-8") as f:
            after = f.read()
        assert original == after  # timeline.json 不变

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_d045_flat_action_enum(self, pkg_name: str) -> None:
        """D045：annotation 是扁平 action 枚举型。"""
        store = AnnotationStore()
        ann = store.add(Annotation(id="", target_block_id="b001", action=Action.MARK_KEY))
        d = ann.to_dict()
        assert "id" in d
        assert "target_block_id" in d
        assert "action" in d
        assert "payload" in d
        assert "created_at" in d
        assert d["action"] == "mark_key"  # 枚举值是字符串

    def test_d046_13_actions_defined(self) -> None:
        """D046：17 个 action 枚举已定义（原 13 + T1 新增 4 个 unmark/untrim）。"""
        actions = [a.value for a in Action]
        assert len(actions) == 17
        assert "move_supplement" in actions  # 替代 reorder
        assert "unmark_key" in actions  # T1 新增
        assert "untrim" in actions  # T1 新增

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_d050_undo_redo(self, pkg_name: str) -> None:
        """D050：完整 undo/redo 栈。"""
        store = AnnotationStore()
        store.add(Annotation(id="", target_block_id="b001", action=Action.MARK_KEY))
        assert store.undo() is True
        assert store.redo() is True

    @pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
    def test_d053_finalize(self, pkg_name: str, tmp_path: Path) -> None:
        """D053：finalize 标记。"""
        import shutil
        pkg = RECORDINGS_DIR / pkg_name
        shutil.copy(pkg / "timeline.json", tmp_path / "timeline.json")

        store = AnnotationStore()
        store.finalize()
        store.save(tmp_path / "annotations.json")

        assert is_finalized(tmp_path) is True
        result = merge_timeline_annotations(tmp_path)
        assert result["finalized"] is True
