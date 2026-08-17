"""AnnotationStore：管理 annotations.json 读写 + undo/redo 栈 + finalize 标记（D050）。

- add(annotation)：添加 annotation + 压入 undo 栈 + 清空 redo 栈
- undo()：标记最后一条 annotation 为 undone=true + 压入 redo 栈
- redo()：取消 redo 栈顶的 undone 标记 + 压回 undo 栈
- finalize()：设置 finalized=true
- save(path)：写 annotations.json（过滤 undone 记录，保持干净）
- load(path)：读 annotations.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib.recorder.editor.annotation import Annotation, next_annotation_id


class AnnotationStore:
    """annotations.json 管理器（D045/D050/D053）。"""

    def __init__(self) -> None:
        self._annotations: list[Annotation] = []
        self._undo_stack: list[Annotation] = []
        self._redo_stack: list[Annotation] = []
        self._finalized: bool = False

    @property
    def annotations(self) -> list[Annotation]:
        """所有 annotation（含 undone）。"""
        return list(self._annotations)

    @property
    def active_annotations(self) -> list[Annotation]:
        """未 undone 的 annotation（merge 时用）。"""
        return [a for a in self._annotations if not a.undone]

    @property
    def finalized(self) -> bool:
        return self._finalized

    def add(self, annotation: Annotation) -> Annotation:
        """添加 annotation + 压入 undo 栈 + 清空 redo 栈。

        若 annotation.id 为空，自动生成。
        """
        if not annotation.id:
            annotation.id = next_annotation_id(self._annotations)
        self._annotations.append(annotation)
        self._undo_stack.append(annotation)
        self._redo_stack.clear()
        return annotation

    def undo(self) -> bool:
        """撤销最后一条 annotation：标记为 undone=true + 压入 redo 栈。

        Returns: True 若成功撤销，False 若 undo 栈为空。
        """
        if not self._undo_stack:
            return False
        ann = self._undo_stack.pop()
        ann.undone = True
        self._redo_stack.append(ann)
        return True

    def redo(self) -> bool:
        """重做 redo 栈顶：取消 undone 标记 + 压回 undo 栈。

        Returns: True 若成功重做，False 若 redo 栈为空。
        """
        if not self._redo_stack:
            return False
        ann = self._redo_stack.pop()
        ann.undone = False
        self._undo_stack.append(ann)
        return True

    def finalize(self) -> None:
        """设置 finalized=true（D053）。"""
        self._finalized = True

    def unfinalize(self) -> None:
        """取消就绪标记（recorder-gui-redesign T3：finalize 改 checkable 后允许回退）。"""
        self._finalized = False

    def to_dict(self) -> dict[str, Any]:
        """转为 dict（含 finalized + annotations 数组，过滤 undone 记录）。"""
        return {
            "finalized": self._finalized,
            "annotations": [a.to_dict() for a in self._annotations if not a.undone],
        }

    def save(self, path: Path) -> None:
        """写 annotations.json（过滤 undone 记录，D050 保存时保持干净）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def load(self, path: Path) -> None:
        """读 annotations.json。"""
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self._finalized = data.get("finalized", False)
        self._annotations = [Annotation.from_dict(a) for a in data.get("annotations", [])]
        # 加载时不恢复 undo/redo 栈（历史已丢失）
        self._undo_stack = list(self._annotations)
        self._redo_stack.clear()

    def stats(self) -> dict[str, int]:
        """统计信息（D047 诊断用）：各 action 类型的数量。"""
        counts: dict[str, int] = {}
        for a in self.active_annotations:
            counts[a.action.value] = counts.get(a.action.value, 0) + 1
        return counts
