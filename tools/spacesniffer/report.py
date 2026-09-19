"""快照报告导出：文本摘要 + 逐条 CSV。

对应 SpaceSniffer 的「Export Module」。原版支持自定义报表模板，这里收敛成两种
最有用的形态：一眼能看的文本摘要，和能丢进 Excel 继续算的全量 CSV。
"""

from __future__ import annotations

import csv
import heapq
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import filetypes
from sns_model import Entry, Snapshot, attr_names, human_size, iter_with_paths

# 进度回调签名： (已完成条目数, 总条目数) -> bool，返回 True 表示用户取消
ProgressFn = Callable[[int, int], bool]

FilePath = Union[str, Path]


class ExportCancelled(Exception):
    """用户在导出过程中取消。"""


def _check(progress: Optional[ProgressFn], done: int, total: int) -> None:
    if progress is not None and progress(done, total):
        raise ExportCancelled()


# ---------------------------------------------------------------------------
# 聚合查询
# ---------------------------------------------------------------------------


def type_distribution(root: Entry) -> List[Tuple[str, int, int]]:
    """按文件类型类别聚合整棵树：[(category, 字节数, 文件数)]，按字节降序。"""
    buckets: Dict[str, List[int]] = {}
    for node in _walk_files(root):
        category = filetypes.category_of(node.ext)
        slot = buckets.get(category)
        if slot is None:
            buckets[category] = [node.size, 1]
        else:
            slot[0] += node.size
            slot[1] += 1
    return sorted(
        ((cat, v[0], v[1]) for cat, v in buckets.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def largest_files(root: Entry, limit: int = 50) -> List[Tuple[Entry, str]]:
    """全树最大的文件：[(entry, 完整路径)]，按大小降序。

    用 `heapq.nlargest` 而非全排序：70 万文件时前者只维护 limit 大小的堆。
    """
    heap: List[Tuple[int, int, Entry]] = []
    counter = 0
    for node in _walk_files(root):
        if len(heap) < limit:
            heapq.heappush(heap, (node.size, counter, node))
            counter += 1
        elif node.size > heap[0][0]:
            heapq.heapreplace(heap, (node.size, counter, node))
            counter += 1
    top = sorted(heap, key=lambda item: item[0], reverse=True)
    return [(entry, entry.full_path()) for _, _, entry in top]


def largest_dirs(root: Entry, limit: int = 50, min_depth: int = 1) -> List[Tuple[Entry, str]]:
    """全树最大的目录（按子树大小）：[(entry, 完整路径)]，按大小降序。"""
    heap: List[Tuple[int, int, Entry]] = []
    counter = 0
    for node in _walk_dirs(root):
        if node is root or node.depth() < min_depth:
            continue
        if len(heap) < limit:
            heapq.heappush(heap, (node.size, counter, node))
            counter += 1
        elif node.size > heap[0][0]:
            heapq.heapreplace(heap, (node.size, counter, node))
            counter += 1
    top = sorted(heap, key=lambda item: item[0], reverse=True)
    return [(entry, entry.full_path()) for _, _, entry in top]


def _walk_files(root: Entry) -> Iterable[Entry]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.children is None:
            yield node
            continue
        for child in node.children:
            if child.children is None:
                yield child
            else:
                stack.append(child)


def _walk_dirs(root: Entry) -> Iterable[Entry]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in node.children or ():
            if child.children is not None:
                stack.append(child)


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------


def build_text_report(
    snapshot: Snapshot,
    focus: Optional[Entry] = None,
    top: int = 20,
    filter_expression: str = "",
) -> str:
    """生成文本摘要报告。

    `focus` 为 None 时报告整个卷；否则报告指定目录的子树。
    各段都以「人类可读大小 + 占比」开头，方便直接贴进笔记。
    """
    target = focus or snapshot.root
    total = max(target.size, 1)
    lines: List[str] = []

    lines.append("SpaceSniffer 快照报告")
    lines.append("=" * 60)
    lines.append(f"快照文件   : {snapshot.source_path}")
    lines.append(f"数据时点   : {snapshot.scanned_at() or '未知'}")
    lines.append(f"卷         : {snapshot.volume_label}")
    lines.append(
        f"卷容量     : 已用 {human_size(snapshot.used_bytes)}"
        f" / 可用 {human_size(snapshot.free_bytes)}"
        f" / 合计 {human_size(snapshot.total_bytes)}"
    )
    lines.append(
        f"扫描完整   : {'是' if snapshot.is_complete else '否（存在未扫描区域）'}"
        f"   文件 {snapshot.n_files:,}   目录 {snapshot.n_dirs:,}"
    )
    if filter_expression:
        lines.append(f"过滤条件   : {filter_expression}")
    if focus is not None and focus is not snapshot.root:
        lines.append(f"报告范围   : {focus.full_path()}  （{human_size(focus.size)}）")
    lines.append("")

    # --- 子目录 ---
    children = [c for c in (target.children or []) if c.is_dir]
    if children:
        lines.append(f"--- 子目录 TOP {top} ---")
        for child in children[:top]:
            pct = child.size / total * 100
            lines.append(
                f"{human_size(child.size):>12}  {pct:5.1f}%  "
                f"{child.name}  ({child.n_files:,} 文件 / {child.n_dirs:,} 子目录)"
            )
        if len(children) > top:
            rest = sum(c.size for c in children[top:])
            lines.append(
                f"{human_size(rest):>12}  {rest / total * 100:5.1f}%  "
                f"（其余 {len(children) - top} 个目录）"
            )
        lines.append("")

    # --- 直接大文件 ---
    direct_files = [c for c in (target.children or []) if not c.is_dir]
    if direct_files:
        lines.append(f"--- 本级最大文件 TOP {top} ---")
        for entry in direct_files[:top]:
            pct = entry.size / total * 100
            lines.append(
                f"{human_size(entry.size):>12}  {pct:5.1f}%  {entry.name}"
                + (f"   [{entry.mtime}]" if entry.mtime else "")
            )
        lines.append("")

    # --- 全树最大文件 ---
    lines.append(f"--- 全树最大文件 TOP {top} ---")
    for entry, path in largest_files(snapshot.root, top):
        lines.append(f"{human_size(entry.size):>12}  {path}")
    lines.append("")

    # --- 全树最大目录 ---
    lines.append(f"--- 全树最大目录 TOP {top} ---")
    for entry, path in largest_dirs(snapshot.root, top):
        lines.append(f"{human_size(entry.size):>12}  {path}")
    lines.append("")

    # --- 类型分布 ---
    lines.append("--- 文件类型分布 ---")
    distribution = type_distribution(snapshot.root)
    grand_total = max(sum(item[1] for item in distribution), 1)
    for category, size, count in distribution:
        label = filetypes.label_of_category(category)
        lines.append(f"{human_size(size):>12}  {size / grand_total * 100:5.1f}%  {label}  ({count:,} 个)")

    return "\n".join(lines) + "\n"


def write_text_report(path: FilePath, text: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# CSV 明细
# ---------------------------------------------------------------------------

CSV_HEADER = [
    "完整路径",
    "名称",
    "类型",
    "类别",
    "大小(字节)",
    "大小",
    "修改时间",
    "属性",
    "标记",
    "是否目录",
]


def category_column(entry: Entry) -> str:
    """CSV 的「类别」列：目录写「目录」，文件写类型中文名。"""
    if entry.is_dir:
        return "目录"
    return filetypes.label_of_category(filetypes.category_of(entry.ext))


def write_csv(root: Entry, path: FilePath, progress: Optional[ProgressFn] = None, total: int = 0) -> int:
    """把子树逐条写成 CSV。返回写出的行数。

    `utf-8-sig` 是为了 Excel 双击打开不乱码（无 BOM 的 UTF-8 中文表头会变问号）。
    """
    written = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for entry, win_path in iter_with_paths(root):
            writer.writerow(
                [
                    win_path,
                    entry.name,
                    entry.type_label,
                    category_column(entry),
                    entry.size,
                    human_size(entry.size),
                    entry.mtime or "",
                    "/".join(attr_names(entry.attrs)),
                    entry.tag or "",
                    "是" if entry.is_dir else "否",
                ]
            )
            written += 1
            if progress is not None and written % 5000 == 0:
                _check(progress, written, total or written)
    return written


def write_grouped_csv(
    entries: Iterable[Tuple[Entry, str]],
    path: FilePath,
) -> int:
    """把一组「(条目, 路径)」写成 CSV（供 Top N 列表导出）。"""
    written = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["完整路径", "名称", "大小(字节)", "大小", "修改时间"])
        for entry, win_path in entries:
            writer.writerow([win_path, entry.name, entry.size, human_size(entry.size), entry.mtime or ""])
            written += 1
    return written
