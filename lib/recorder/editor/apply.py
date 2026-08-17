"""apply_annotation：apply 单条 annotation 到块列表（纯函数，易测试）。

输入：blocks（timeline.json 的 blocks 列表）+ annotation
输出：apply 后的 blocks（新列表，不修改原列表）

17 个 action 的 apply 逻辑：
- mark_key/mark_anomaly/mark_automatable：更新对应块的 status 字段为 True
- unmark_key/unmark_anomaly/unmark_automatable：更新对应块的 status 字段为 False
  （对未标记块 unmark 是合法 no-op，幂等不报错）
- trim：块 status.is_trimmed = true
- restore / untrim：块 status.is_trimmed = false（UNTRIM 是 2026-07-31 新增别名，
  与 RESTORE 行为一致，仅用于 UI toggle 语义对称；旧包无 UNTRIM 记录，重放结果不变）
- insert_note/insert_chapter/insert_image/insert_text：在指定位置插入新块
- rename_chapter：更新章节块的 primary.name
- merge_blocks：合并多个块为一个（保留主块，其他标记为 merged）
- split_block：在指定时间戳拆分块为两个
- move_supplement：更新素材块的 supplements.parent_block_id + 时间戳跟随新父块
"""

from __future__ import annotations

import copy
from typing import Any

from lib.recorder.editor.annotation import Action, Annotation


def _find_block_index(blocks: list[dict[str, Any]], block_id: str) -> int:
    """找块 ID 对应的索引，找不到返回 -1。"""
    for i, b in enumerate(blocks):
        if b.get("id") == block_id:
            return i
    return -1


def _new_block(
    block_id: str,
    category: str,
    block_type: str,
    timestamp: float,
    primary: dict[str, Any],
) -> dict[str, Any]:
    """构造新块（用于 insert_*）。"""
    return {
        "id": block_id,
        "category": category,
        "type": block_type,
        "timestamp": timestamp,
        "duration": 0.0,
        "primary": primary,
        "supplements": {},
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def apply_annotation(blocks: list[dict[str, Any]], ann: Annotation) -> list[dict[str, Any]]:
    """apply 单条 annotation 到块列表，返回新列表（不修改原列表）。

    若 annotation.undone=True，直接返回原列表（merge 时跳过）。
    """
    if ann.undone:
        return list(blocks)

    new_blocks = copy.deepcopy(blocks)

    if ann.action in (Action.MARK_KEY, Action.MARK_ANOMALY, Action.MARK_AUTOMATABLE,
                      Action.UNMARK_KEY, Action.UNMARK_ANOMALY, Action.UNMARK_AUTOMATABLE):
        idx = _find_block_index(new_blocks, ann.target_block_id)
        if idx >= 0:
            field_map = {
                Action.MARK_KEY: "marked_key",
                Action.MARK_ANOMALY: "marked_anomaly",
                Action.MARK_AUTOMATABLE: "marked_automatable",
                Action.UNMARK_KEY: "marked_key",
                Action.UNMARK_ANOMALY: "marked_anomaly",
                Action.UNMARK_AUTOMATABLE: "marked_automatable",
            }
            value = ann.action not in (Action.UNMARK_KEY, Action.UNMARK_ANOMALY,
                                       Action.UNMARK_AUTOMATABLE)
            new_blocks[idx].setdefault("status", {})
            new_blocks[idx]["status"][field_map[ann.action]] = value

    elif ann.action == Action.TRIM:
        idx = _find_block_index(new_blocks, ann.target_block_id)
        if idx >= 0:
            new_blocks[idx].setdefault("status", {})
            new_blocks[idx]["status"]["is_trimmed"] = True

    elif ann.action in (Action.RESTORE, Action.UNTRIM):
        # UNTRIM 与 RESTORE 行为一致（仅语义对称），旧包无 UNTRIM 记录兼容
        idx = _find_block_index(new_blocks, ann.target_block_id)
        if idx >= 0:
            new_blocks[idx].setdefault("status", {})
            new_blocks[idx]["status"]["is_trimmed"] = False

    elif ann.action in (Action.INSERT_NOTE, Action.INSERT_CHAPTER, Action.INSERT_IMAGE, Action.INSERT_TEXT):
        _apply_insert(new_blocks, ann)

    elif ann.action == Action.RENAME_CHAPTER:
        idx = _find_block_index(new_blocks, ann.target_block_id)
        if idx >= 0:
            new_blocks[idx].setdefault("primary", {})
            new_blocks[idx]["primary"]["name"] = ann.payload.get("new_name", "")

    elif ann.action == Action.MERGE_BLOCKS:
        _apply_merge(new_blocks, ann)

    elif ann.action == Action.SPLIT_BLOCK:
        _apply_split(new_blocks, ann)

    elif ann.action == Action.MOVE_SUPPLEMENT:
        _apply_move_supplement(new_blocks, ann)

    return new_blocks


def _apply_insert(blocks: list[dict[str, Any]], ann: Annotation) -> None:
    """apply insert_* annotation。"""
    payload = ann.payload
    inserted_id = payload.get("inserted_block_id", "u000")
    position = payload.get("position", "after")
    parent_idx = _find_block_index(blocks, ann.target_block_id)

    # 构造新块
    if ann.action == Action.INSERT_NOTE:
        new_block = _new_block(
            inserted_id, "text", "user_note", 0.0,
            {"text": payload.get("text", ""), "source": "user"},
        )
    elif ann.action == Action.INSERT_CHAPTER:
        new_block = _new_block(
            inserted_id, "text", "chapter", 0.0,
            {"name": payload.get("name", "")},
        )
    elif ann.action == Action.INSERT_IMAGE:
        new_block = _new_block(
            inserted_id, "image", "reference_image", 0.0,
            {"frame_path": payload.get("image_path", ""), "source": "user"},
        )
    else:  # INSERT_TEXT
        new_block = _new_block(
            inserted_id, "text", "reference_text", 0.0,
            {"text": payload.get("text", ""), "source_file": payload.get("source_file", ""), "source": "user"},
        )

    # 根据 position 决定插入位置和时间戳
    if position == "supplement":
        # 附属到父块，无独立时间戳（时间戳=父块时间戳，但 supplements 标记 parent）
        if parent_idx >= 0:
            new_block["timestamp"] = blocks[parent_idx]["timestamp"]
            new_block["supplements"]["parent_block_id"] = ann.target_block_id
            blocks.insert(parent_idx + 1, new_block)
        else:
            blocks.append(new_block)
    elif position == "after":
        if parent_idx >= 0:
            new_block["timestamp"] = blocks[parent_idx]["timestamp"] + 0.001
            blocks.insert(parent_idx + 1, new_block)
        else:
            blocks.append(new_block)
    elif position == "timestamp":
        new_block["timestamp"] = payload.get("timestamp", 0.0)
        # 按时间戳插入正确位置
        insert_idx = len(blocks)
        for i, b in enumerate(blocks):
            if b.get("timestamp", 0) > new_block["timestamp"]:
                insert_idx = i
                break
        blocks.insert(insert_idx, new_block)
    else:
        blocks.append(new_block)


def _apply_merge(blocks: list[dict[str, Any]], ann: Annotation) -> None:
    """apply merge_blocks annotation：合并多个块为一个（保留主块，其他标记为 merged）。"""
    merged_ids = ann.payload.get("merged_block_ids", [])
    if not merged_ids:
        return
    # target_block_id 是主块，merged_ids 是被合并的块
    main_idx = _find_block_index(blocks, ann.target_block_id)
    if main_idx < 0:
        return
    # 合并 primary 字段（text 拼接，frame_path 合并）
    main_block = blocks[main_idx]
    main_block.setdefault("supplements", {})
    main_block["supplements"]["merged_block_ids"] = merged_ids
    # 标记被合并的块为 trimmed（不物理删除，merge 时由调用方过滤）
    for mid in merged_ids:
        idx = _find_block_index(blocks, mid)
        if idx >= 0:
            blocks[idx].setdefault("status", {})
            blocks[idx]["status"]["is_trimmed"] = True
            blocks[idx].setdefault("supplements", {})
            blocks[idx]["supplements"]["merged_into"] = ann.target_block_id


def _apply_split(blocks: list[dict[str, Any]], ann: Annotation) -> None:
    """apply split_block annotation：在指定时间戳拆分块为两个。"""
    split_ts = ann.payload.get("split_timestamp", 0.0)
    idx = _find_block_index(blocks, ann.target_block_id)
    if idx < 0:
        return
    original = blocks[idx]
    # 原块结束时间改为 split_ts
    original_dur = original.get("duration", 0.0)
    original["duration"] = split_ts - original.get("timestamp", 0.0)
    # 新块从 split_ts 开始
    new_block = copy.deepcopy(original)
    # 生成新 ID（原ID + _split）
    new_block["id"] = f"{original['id']}_split"
    new_block["timestamp"] = split_ts
    new_block["duration"] = original_dur - original["duration"]
    new_block.setdefault("supplements", {})
    new_block["supplements"]["split_from"] = ann.target_block_id
    blocks.insert(idx + 1, new_block)


def _apply_move_supplement(blocks: list[dict[str, Any]], ann: Annotation) -> None:
    """apply move_supplement annotation：素材块附属关系转移（D046）。

    素材丢失原时间戳，跟随新父块。
    """
    new_parent_id = ann.payload.get("new_parent_block_id", "")
    sup_idx = _find_block_index(blocks, ann.target_block_id)
    parent_idx = _find_block_index(blocks, new_parent_id)
    if sup_idx < 0 or parent_idx < 0:
        return
    # 更新素材块的时间戳为新父块时间戳
    blocks[sup_idx]["timestamp"] = blocks[parent_idx]["timestamp"]
    blocks[sup_idx].setdefault("supplements", {})
    blocks[sup_idx]["supplements"]["parent_block_id"] = new_parent_id
