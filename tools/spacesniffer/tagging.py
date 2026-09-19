"""四色标记（SpaceSniffer 的 color tags）。

原版用 Ctrl+1~4 给文件/目录打临时标记，再用 `:red` 之类的过滤条件筛出来，
是「先标记、后清理」工作流的核心。标记**只存在内存里**，不改动磁盘上任何东西，
关掉程序就没了 —— 这正是它的定位：一次清理会话内的临时便签。

颜色直接复用语义色 token（红/黄/绿/蓝各对应 danger/warning/success/info），
不新造一组近似色。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from lib.ui import tokens

TAG_NAMES: List[str] = ["red", "yellow", "green", "blue"]

TAG_LABELS: Dict[str, str] = {
    "red": "红",
    "yellow": "黄",
    "green": "绿",
    "blue": "蓝",
}

# 快捷键 Ctrl+1 ~ Ctrl+4 的对应顺序
TAG_HOTKEY_ORDER: List[str] = ["red", "yellow", "green", "blue"]

TAG_COLORS: Dict[str, str] = {
    "red": tokens.DANGER,
    "yellow": tokens.WARNING,
    "green": tokens.SUCCESS,
    "blue": tokens.INFO,
}

TAG_COLORS_TEXT: Dict[str, str] = {
    "red": tokens.DANGER_TEXT,
    "yellow": tokens.WARNING_TEXT,
    "green": tokens.SUCCESS_TEXT,
    "blue": tokens.INFO_TEXT,
}


def color_of(tag: Optional[str]) -> Optional[str]:
    if tag is None:
        return None
    return TAG_COLORS.get(tag)


def label_of(tag: Optional[str]) -> str:
    if tag is None:
        return "无标记"
    return TAG_LABELS.get(tag, tag)


def hotkey_of(tag: str) -> str:
    """返回该标记的快捷键文案（`Ctrl+1` ~ `Ctrl+4`）。"""
    try:
        return f"Ctrl+{TAG_HOTKEY_ORDER.index(tag) + 1}"
    except ValueError:
        return ""


def apply_tag(entry, tag: Optional[str]) -> None:
    """给条目打标记。传 None 清除。"""
    entry.tag = tag


def toggle_tag(entry, tag: str) -> Optional[str]:
    """打标记，若已是该色则清除（幂等开关，见 patterns.md §1）。返回新标记值。"""
    if entry.tag == tag:
        entry.tag = None
    else:
        entry.tag = tag
    return entry.tag


def clear_tags(root) -> int:
    """清空子树里的所有标记，返回清除数量。"""
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag is not None:
            node.tag = None
            count += 1
        for child in node.children or ():
            stack.append(child)
    return count


def count_tags(root) -> Dict[str, int]:
    """统计子树里各颜色标记的数量。"""
    counts = {name: 0 for name in TAG_NAMES}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag in counts:
            counts[node.tag] += 1
        for child in node.children or ():
            stack.append(child)
    return counts
