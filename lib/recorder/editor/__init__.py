"""L3 编辑层公用库（annotation 引擎 + merge 逻辑）。

供 L3 编辑器（tools/recorder/editor/）和 L4 agent 共享。
"""

from lib.recorder.editor.annotation import Action, Annotation
from lib.recorder.editor.apply import apply_annotation
from lib.recorder.editor.merge import is_finalized, merge_timeline_annotations
from lib.recorder.editor.store import AnnotationStore

__all__ = [
    "Action",
    "Annotation",
    "AnnotationStore",
    "apply_annotation",
    "is_finalized",
    "merge_timeline_annotations",
]
