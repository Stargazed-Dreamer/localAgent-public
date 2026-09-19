"""SpaceSniffer 过滤表达式解析、匹配与「视图树」构建。

复刻 SpaceSniffer 的过滤框：一个文本框，用一套紧凑语法同时表达
「包含什么 / 排除什么 / 多大 / 多久 / 打了什么标记 / 叫什么名」。

语法（分号分隔多个条件，不同条件之间是 AND；同类掩码之间是 OR）：

    *.jpg                 只看 .jpg 文件（多个掩码用分号：`*.jpg;*.png`）
    *.jpg;*.png           只看 jpg 或 png
    |*.jpg                排除 jpg（竖线开头表示排除）
    |:red                 排除打了红色标记的
    >500mb                大于 500 MB
    <1gb                  小于 1 GB
    >2years                修改时间早于两年前
    <3months               修改时间在近三个月内
    :red / :yellow         只看红色 / 黄色标记
    :all                   只看打过任意标记的
    N:download             名称包含 download
    A:hidden               带隐藏属性
    *.jpg;>1mb;<3months;|:yellow    组合起来用

关于「目录怎么参与过滤」——语义在这里定义清楚，界面与文档都按这个来：

- 过滤**主要作用在文件上**。一个目录只要子树里有命中文件就会被保留，
  并按「子树内命中内容的合计大小」重新计量，方块图因此会如实收缩。
- 目录名本身也参与匹配（掩码 / `N:` / `A:`），所以 `N:node_modules`
  这类「找目录」的用法同样有效。此时若目录下没有命中子项，
  就按目录自身大小整体保留。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from fnmatch import translate
from typing import Dict, List, Optional, Pattern, Set, Tuple

from sns_model import Entry, human_size, parse_size_text

# 时间单位 → 天数
_DAY_UNITS: Dict[str, int] = {
    "day": 1, "days": 1, "d": 1,
    "week": 7, "weeks": 7, "w": 7,
    "month": 30, "months": 30, "mon": 30, "m": 30,
    "year": 365, "years": 365, "y": 365,
}

TAG_NAMES: Tuple[str, ...] = ("red", "yellow", "green", "blue")
_TAG_ALIASES = {
    "red": "red", "r": "red", "红": "red",
    "yellow": "yellow", "y": "yellow", "黄": "yellow",
    "green": "green", "g": "green", "绿": "green",
    "blue": "blue", "b": "blue", "蓝": "blue",
}

# 属性名 → 位掩码（与 sns_model 中的 ATTR_* 对应）
_ATTR_BITS: Dict[str, int] = {
    "readonly": 0x0001, "r": 0x0001,
    "hidden": 0x0002, "h": 0x0002,
    "system": 0x0004, "s": 0x0004,
    "archive": 0x0020, "a": 0x0020,
    "compressed": 0x0800, "c": 0x0800,
    "sparse": 0x0200,
}


class FilterError(ValueError):
    """表达式无法解析。界面需要把具体原因回显给用户，不能静默忽略。"""


class ViewNode:
    """过滤视图里的一个节点：包着真实 Entry，但大小与子项是「过滤后」的。

    为什么不直接复制一份 Entry：

    - `size` / `children` 必须能覆盖原值（过滤会改变计量口径）
    - `tag` / `name` / `ext` 必须**透传**回原 Entry，否则在过滤视图里打的
      颜色标记落不到真实对象上，退出过滤就丢了

    只包一层引用同时满足这两点，且比整树深拷贝省一个数量级的分配。
    """

    __slots__ = ("entry", "size", "children", "is_dir", "parent")

    def __init__(
        self,
        entry: Entry,
        size: int,
        children: Optional[List["ViewNode"]] = None,
    ) -> None:
        self.entry = entry
        self.size = size
        self.is_dir = entry.is_dir
        self.children = children if children is not None else ([] if entry.is_dir else None)
        self.parent: Optional["ViewNode"] = None

    # --- 透传给真实 Entry 的只读属性（treemap 只需要这几个） ----------------
    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def ext(self) -> str:
        return self.entry.ext

    @property
    def tag(self) -> Optional[str]:
        return self.entry.tag

    @tag.setter
    def tag(self, value: Optional[str]) -> None:
        self.entry.tag = value

    @property
    def mtime(self) -> Optional[str]:
        return self.entry.mtime

    @property
    def attrs(self) -> int:
        return self.entry.attrs

    @property
    def type_label(self) -> str:
        """界面展示用的类型短标签（目录 / 扩展名 / 无扩展名）。"""
        return self.entry.type_label

    def full_path(self) -> str:
        return self.entry.full_path()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = "D" if self.is_dir else "F"
        return f"<ViewNode {kind} {self.name!r} size={self.size}>"


def resolve(node) -> Entry:
    """把视图节点还原成真实 Entry。传 Entry 则原样返回 ——

    视图树只在该用的时候才构建（无过滤时直接用原树），所以这条路径必须两种都吃。
    """
    if isinstance(node, ViewNode):
        return node.entry
    return node


def view_children(node) -> List:
    """取子项列表，Entry 与 ViewNode 通用。"""
    return node.children or []


class Filter:
    """解析后的过滤表达式。空表达式视为「不过滤」。"""

    def __init__(self, expression: str = "") -> None:
        self.expression = (expression or "").strip()
        self.masks_include: List[Pattern[str]] = []
        self.masks_exclude: List[Pattern[str]] = []
        # 保留掩码原文：describe() 要回显给用户，从编译后的正则反解不可靠
        self.masks_include_src: List[str] = []
        self.masks_exclude_src: List[str] = []
        # `*.jpg` 这类「只有单个通配前缀」的掩码走扩展名快路（见 _literal_extension）。
        # 过滤要对整盘 85 万条目逐个判定，这条快路实测比通用正则快 3~4 倍。
        self.ext_include: Set[str] = set()
        self.ext_exclude: Set[str] = set()
        self._include_suffixes: Tuple[str, ...] = ()
        self._exclude_suffixes: Tuple[str, ...] = ()
        self.min_size: Optional[int] = None
        self.max_size: Optional[int] = None
        self.older_than_days: Optional[int] = None
        self.newer_than_days: Optional[int] = None
        self.tags_include: Set[str] = set()
        self.tags_exclude: Set[str] = set()
        self.any_tag_only = False
        self.name_terms_include: List[str] = []
        self.name_terms_exclude: List[str] = []
        self.attr_required: int = 0
        self.attr_forbidden: int = 0
        # 缓存时间阈值：'YYYY-MM-DD HH:MM' 字典序即时间序，直接比字符串比解析 datetime 快得多
        self._older_bound: Optional[str] = None
        self._newer_bound: Optional[str] = None
        self._now: Optional[datetime] = None
        self._parse()

    # -- 解析 ---------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return (
            not self.expression
            or not (
                self.masks_include
                or self.masks_exclude
                or self.ext_include
                or self.ext_exclude
                or self.min_size is not None
                or self.max_size is not None
                or self.older_than_days is not None
                or self.newer_than_days is not None
                or self.tags_include
                or self.tags_exclude
                or self.any_tag_only
                or self.name_terms_include
                or self.name_terms_exclude
                or self.attr_required
                or self.attr_forbidden
            )
        )

    @property
    def needs_mtime(self) -> bool:
        return self.older_than_days is not None or self.newer_than_days is not None

    def _parse(self) -> None:
        if not self.expression:
            return
        for raw_clause in self.expression.split(";"):
            clause = raw_clause.strip()
            if not clause:
                continue
            negated = clause.startswith("|")
            if negated:
                clause = clause[1:].strip()
                if not clause:
                    raise FilterError("竖线（排除）后面没有内容")
            self._parse_clause(clause, negated)
        self._include_suffixes = tuple(sorted("." + e for e in self.ext_include))
        self._exclude_suffixes = tuple(sorted("." + e for e in self.ext_exclude))
        if self.older_than_days is not None or self.newer_than_days is not None:
            self._now = datetime.now()
            if self.older_than_days is not None:
                self._older_bound = (self._now - timedelta(days=self.older_than_days)).strftime(
                    "%Y-%m-%d %H:%M"
                )
            if self.newer_than_days is not None:
                self._newer_bound = (self._now - timedelta(days=self.newer_than_days)).strftime(
                    "%Y-%m-%d %H:%M"
                )

    def _parse_clause(self, clause: str, negated: bool) -> None:
        head = clause[:2].lower()

        # 标签：:red / :all
        if clause.startswith(":"):
            value = clause[1:].strip().lower()
            if value in ("all", "*"):
                if negated:
                    # |:all 语义上等价于「不保留任何打了标记的项」，SpaceSniffer 允许
                    self.tags_exclude.update(TAG_NAMES)
                else:
                    self.any_tag_only = True
                return
            tag = _TAG_ALIASES.get(value)
            if tag is None:
                raise FilterError(f"未知标记颜色：{value}（可用 red/yellow/green/blue/all）")
            (self.tags_exclude if negated else self.tags_include).add(tag)
            return

        # 名称：N:xxx
        if head == "n:":
            term = clause[2:].strip().lower()
            if not term:
                raise FilterError("N: 后面没有内容")
            (self.name_terms_exclude if negated else self.name_terms_include).append(term)
            return

        # 属性：A:hidden
        if head == "a:":
            value = clause[2:].strip().lower()
            if not value:
                raise FilterError("A: 后面没有内容")
            bit = _ATTR_BITS.get(value)
            if bit is None:
                known = "、".join(sorted(set(_ATTR_BITS)))
                raise FilterError(f"未知属性：{value}（可用 {known}）")
            if negated:
                self.attr_forbidden |= bit
            else:
                self.attr_required |= bit
            return

        # 大小 / 时间：>500mb / <3months
        if clause[0] in "<>":
            self._parse_comparison(clause, negated)
            return

        # 其余一律当掩码：*.jpg / jpg / *.mp4
        pattern = clause.lower()
        if not pattern.startswith("*") and "." not in pattern:
            pattern = "*." + pattern  # 允许只写扩展名
        source = self.masks_exclude_src if negated else self.masks_include_src
        source.append(pattern)
        literal = _literal_extension(pattern)
        if literal is not None:
            # 快路：`*.jpg` 之类退化成扩展名集合 / 后缀匹配，
            # 免掉每个条目一次正则匹配（整盘 85 万次，实测差 3~4 倍）
            (self.ext_exclude if negated else self.ext_include).add(literal)
            return
        (self.masks_exclude if negated else self.masks_include).append(
            re.compile(translate(pattern))
        )

    def _parse_comparison(self, clause: str, negated: bool) -> None:
        operator = clause[0]
        operand = clause[1:].strip().lower().replace(" ", "")
        if not operand:
            raise FilterError(f"{operator} 后面没有内容")

        days = _parse_duration(operand)
        if days is not None:
            if operator == ">":
                value = days
                if negated:
                    self.newer_than_days = _min_opt(self.newer_than_days, value)
                else:
                    self.older_than_days = _max_opt(self.older_than_days, value)
            else:
                value = days
                if negated:
                    self.older_than_days = _max_opt(self.older_than_days, value)
                else:
                    self.newer_than_days = _min_opt(self.newer_than_days, value)
            return

        size = parse_size_text(operand)
        if size is None:
            raise FilterError(f"无法理解的数量：{clause}")
        # `|>500mb` 的语义是「不大于 500MB」，但「不大于」无法用单个下界/上界字段
        # 精确表达（等于的场景）。这里按 SpaceSniffer 的实用口径处理：取反即反向比较。
        if operator == ">":
            if negated:
                self.max_size = _min_opt(self.max_size, size - 1)
            else:
                self.min_size = _max_opt(self.min_size, size + 1)
        else:
            if negated:
                self.min_size = _max_opt(self.min_size, size)
            else:
                self.max_size = _min_opt(self.max_size, size - 1)

    # -- 匹配 ---------------------------------------------------------------

    def matches(self, entry: Entry) -> bool:
        """单个条目是否命中。目录与文件用同一套判定。

        热路径（整盘 85 万条目逐个过）—— 顺序与写法都是为速度定的：

        - 类型掩码优先走 `entry.ext` 集合查找（O(1)，零字符串分配）；
          目录没有 ext，退回 `name.lower().endswith(后缀元组)` 一次 C 调用。
        - `name.lower()` 只在真的要比较名称时才分配（延迟到分支内）。
        """
        if self.ext_include or self.masks_include:
            if entry.is_dir:
                name = entry.name.lower()
                hit = bool(self._include_suffixes) and name.endswith(self._include_suffixes)
                if not hit and self.masks_include:
                    hit = any(p.match(name) for p in self.masks_include)
            elif self.ext_include and entry.ext in self.ext_include:
                hit = True
            elif self.masks_include:
                hit = any(p.match(entry.name.lower()) for p in self.masks_include)
            else:
                hit = False
            if not hit:
                return False

        if self.ext_exclude or self.masks_exclude:
            if entry.is_dir:
                name = entry.name.lower()
                hit = bool(self._exclude_suffixes) and name.endswith(self._exclude_suffixes)
                if not hit and self.masks_exclude:
                    hit = any(p.match(name) for p in self.masks_exclude)
            elif self.ext_exclude and entry.ext in self.ext_exclude:
                hit = True
            elif self.masks_exclude:
                hit = any(p.match(entry.name.lower()) for p in self.masks_exclude)
            else:
                hit = False
            if hit:
                return False

        if self.name_terms_include or self.name_terms_exclude:
            name = entry.name.lower()
            for term in self.name_terms_include:
                if term not in name:
                    return False
            for term in self.name_terms_exclude:
                if term in name:
                    return False

        if self.attr_required and (entry.attrs & self.attr_required) != self.attr_required:
            return False
        if self.attr_forbidden and (entry.attrs & self.attr_forbidden):
            return False

        if self.tags_include and entry.tag not in self.tags_include:
            return False
        if self.tags_exclude and entry.tag in self.tags_exclude:
            return False
        if self.any_tag_only and entry.tag is None:
            return False

        if self.min_size is not None and entry.size < self.min_size:
            return False
        if self.max_size is not None and entry.size > self.max_size:
            return False

        if self._older_bound is not None or self._newer_bound is not None:
            stamp = entry.mtime
            if stamp is None:
                return False  # 目录与无时间戳的文件无法参与时间过滤
            if self._older_bound is not None and stamp >= self._older_bound:
                return False
            if self._newer_bound is not None and stamp <= self._newer_bound:
                return False

        return True

    def can_prune(self, node: Entry) -> bool:
        """能否整棵子树跳过。只有「最小大小」这一条能安全剪枝 ——

        子树里任何条目都不可能大于父目录的大小，所以父目录小于下界时，
        整棵子树必然全部不命中。实测这条剪枝让 `>500mb` 这类过滤从
        全树 85 万次判定降到几千次。
        """
        return self.min_size is not None and node.size < self.min_size


class FilterResult:
    """过滤产物：视图树根 + 统计。`root` 为 None 表示没有任何命中。"""

    __slots__ = ("root", "file_hits", "byte_hits", "total_files")

    def __init__(
        self,
        root: Optional[ViewNode],
        file_hits: int,
        byte_hits: int,
        total_files: int,
    ) -> None:
        self.root = root
        self.file_hits = file_hits
        self.byte_hits = byte_hits
        self.total_files = total_files


def build_view(root: Entry, flt: Filter, total_files: int = 0) -> FilterResult:
    """按过滤器构建视图树。返回 FilterResult。"""
    hits = [0, 0]
    view = _build(root, flt, hits)
    return FilterResult(view, hits[0], hits[1], total_files)


def _build(node: Entry, flt: Filter, hits: List[int]) -> Optional[ViewNode]:
    if node.is_dir:
        if flt.can_prune(node):
            return None
        kept: List[ViewNode] = []
        total = 0
        for child in node.children or ():
            view_child = _build(child, flt, hits)
            if view_child is not None:
                kept.append(view_child)
                total += view_child.size
        self_hit = flt.matches(node)
        if not kept and not self_hit:
            return None
        # 目录自身命中但子树无命中 → 按目录整体大小保留，否则按命中内容合计
        view = ViewNode(node, total if kept else node.size, kept)
        for child_view in kept:
            child_view.parent = view
        return view

    if flt.can_prune(node) or not flt.matches(node):
        return None
    hits[0] += 1
    hits[1] += node.size
    return ViewNode(node, node.size, None)


def _literal_extension(pattern: str) -> Optional[str]:
    """`*.jpg` → 'jpg'（可走扩展名快路）；其余通配形式返回 None（走通用正则）。

    只认「单个 `*.` 前缀 + 无其它通配符 + 单段后缀」。`*.tar.gz` 这类多段后缀
    刻意不走快路 —— `Entry.ext` 只存最后一段，用集合查会漏，交给正则更稳。
    """
    if not pattern.startswith("*."):
        return None
    suffix = pattern[1:]
    if any(ch in suffix for ch in "*?["):
        return None
    if suffix.count(".") != 1 or len(suffix) < 2:
        return None
    return suffix[1:]


def _parse_duration(operand: str) -> Optional[int]:
    """'2years' / '3months' / '7d' → 天数。不是时间表达式返回 None。"""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]+)", operand)
    if not match:
        return None
    unit = match.group(2)
    days_per_unit = _DAY_UNITS.get(unit)
    if days_per_unit is None:
        return None
    return max(1, int(float(match.group(1)) * days_per_unit))


def _max_opt(current: Optional[int], value: int) -> int:
    return value if current is None else max(current, value)


def _min_opt(current: Optional[int], value: int) -> int:
    return value if current is None else min(current, value)


def describe(flt: Filter) -> str:
    """把解析结果翻成中文摘要，给状态栏/帮助提示用。

    用户输入的是紧凑语法（`*.jpg;>1mb;<3months;|:yellow`），能一眼读懂的人不多；
    回显「解析成了什么」便于自查，也避免语法写错时被静默忽略。
    """
    if flt.is_empty:
        return "无过滤"
    parts: List[str] = []
    if flt.masks_include_src:
        parts.append("类型 " + " 或 ".join(flt.masks_include_src))
    if flt.name_terms_include:
        parts.append("名称含 " + "、".join(flt.name_terms_include))
    if flt.min_size is not None:
        parts.append(f"≥ {human_size(flt.min_size)}")
    if flt.max_size is not None:
        parts.append(f"≤ {human_size(flt.max_size)}")
    if flt.older_than_days:
        parts.append(f"早于 {flt.older_than_days} 天")
    if flt.newer_than_days:
        parts.append(f"近 {flt.newer_than_days} 天内")
    if flt.any_tag_only:
        parts.append("有标记")
    if flt.tags_include:
        parts.append("标记 " + "、".join(sorted(flt.tags_include)))
    if flt.attr_required:
        parts.append("属性命中")
    excludes: List[str] = []
    if flt.masks_exclude_src:
        excludes.append("类型 " + "、".join(flt.masks_exclude_src))
    if flt.name_terms_exclude:
        excludes.append("名称 " + "、".join(flt.name_terms_exclude))
    if flt.tags_exclude:
        excludes.append("标记 " + "、".join(sorted(flt.tags_exclude)))
    if excludes:
        parts.append("排除 " + " / ".join(excludes))
    return "；".join(parts)
