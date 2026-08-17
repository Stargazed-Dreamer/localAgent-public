"""Annotation 数据模型 + 17 个 action 枚举（D045/D046 + unmark/untrim 扩展）。

每条 annotation = {id, target_block_id, action, payload, created_at, undone}
- id: annotation 唯一 ID（a001/a002...）
- target_block_id: 目标块 ID（b001/c001/...，insert_* 时为父块 ID）
- action: 17 个枚举值之一
- payload: 随 action 变化的字典
- created_at: ISO 8601 时间戳
- undone: 是否已撤销（undo/redo 栈用，merge 时跳过）

unmark/untrim 系列为 2026-07-31 recorder-gui-redesign 新增，对应 P1「标记不幂等」
详见 temp/sdd/recorder-gui-redesign/02-editor.md §1。schema 版本不变，仅枚举扩展；
旧 annotations.json（无 unmark 记录）重放结果与改动前完全一致。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    """17 个 action 枚举（D046 + unmark/untrim）。"""

    MARK_KEY = "mark_key"
    MARK_ANOMALY = "mark_anomaly"
    MARK_AUTOMATABLE = "mark_automatable"
    UNMARK_KEY = "unmark_key"
    UNMARK_ANOMALY = "unmark_anomaly"
    UNMARK_AUTOMATABLE = "unmark_automatable"
    TRIM = "trim"
    RESTORE = "restore"
    UNTRIM = "untrim"
    INSERT_NOTE = "insert_note"
    INSERT_CHAPTER = "insert_chapter"
    INSERT_IMAGE = "insert_image"
    INSERT_TEXT = "insert_text"
    RENAME_CHAPTER = "rename_chapter"
    MERGE_BLOCKS = "merge_blocks"
    SPLIT_BLOCK = "split_block"
    MOVE_SUPPLEMENT = "move_supplement"


@dataclass
class Annotation:
    """单条 annotation 记录（D045 扁平 action 枚举型）。"""

    id: str
    target_block_id: str
    action: Action
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    undone: bool = False

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().astimezone().isoformat()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Annotation:
        return cls(
            id=d["id"],
            target_block_id=d["target_block_id"],
            action=Action(d["action"]),
            payload=d.get("payload", {}),
            created_at=d.get("created_at", ""),
            undone=d.get("undone", False),
        )


def next_annotation_id(existing: list[Annotation]) -> str:
    """生成下一个 annotation ID（a001/a002...）。"""
    max_n = 0
    for a in existing:
        if a.id.startswith("a") and a.id[1:].isdigit():
            max_n = max(max_n, int(a.id[1:]))
    return f"a{max_n + 1:03d}"


def next_inserted_block_id(existing_ids: list[str]) -> str:
    """生成下一个插入块 ID（u001/u002...，D046 inserted_block_id 生成规则）。"""
    max_n = 0
    for bid in existing_ids:
        if bid.startswith("u") and bid[1:].isdigit():
            max_n = max(max_n, int(bid[1:]))
    return f"u{max_n + 1:03d}"
