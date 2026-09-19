"""SpaceSniffer .sns 快照解析与内存树模型。

本模块是 `tools/spacesniffer/` 的底座，**刻意保持自包含**：不 import
`workspace/disk_manager/scripts/parse_sns.py`。原因是 `workspace/disk_manager/**`
属于发布产物（manifest.toml 的 `exports.runtime`），而 `tools/` 是开发期工具、
不参与发布；让发布产物依赖开发工具会破坏分发边界。两份实现对同一份格式规范
（`workspace/disk_manager/references/sns_format.md`）负责，一致性由
`tests/files_tools/test_spacesniffer.py::test_cross_check_with_disk_manager` 交叉校验守护。

格式要点（little-endian 顺序流式，详见上述规范）：

    记录内偏移  长度  含义
    +0          1     固定魔数 0x02
    +1          1     类型 01=卷 02=文件 03=目录 04=可用空间 05=未知空间
    +2          4     uint32 Base64 文件名长度
    +6          len   Base64(ASCII) 文件名
    +6+len      8     int64 逻辑大小
    …+8         4     uint32 簇尾浪费
    …+12        4     uint32 Windows FILE_ATTRIBUTE
    …+16        24    3×int64 FILETIME
    …+40        2     00 00 结束标志
    …+42        2×k   01 00 × k：弹出最近 k 个层级
"""

from __future__ import annotations

import base64
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

sys.setrecursionlimit(20_000)  # 快照层级实测可达 26 层；留足余量但不必 100k

# --- 记录类型 ---------------------------------------------------------------
SNS_MAGIC = 0x02
TYPE_VOLUME = 1
TYPE_FILE = 2
TYPE_DIR = 3
TYPE_FREE = 4
TYPE_UNKNOWN = 5
LEAF_TYPES = (TYPE_FILE, TYPE_FREE, TYPE_UNKNOWN)

# FILETIME(100ns，1601 纪元) → Unix(秒，1970 纪元)
_FT_TO_UNIX = 116444736000000000

# FILE_ATTRIBUTE_* 位（用于 A: 过滤条件与详情面板展示）
ATTR_READONLY = 0x0001
ATTR_HIDDEN = 0x0002
ATTR_SYSTEM = 0x0004
ATTR_DIRECTORY = 0x0010
ATTR_ARCHIVE = 0x0020
ATTR_COMPRESSED = 0x0800
ATTR_SPARSE = 0x0200

_ATTR_NAMES = [
    (ATTR_READONLY, "只读"),
    (ATTR_HIDDEN, "隐藏"),
    (ATTR_SYSTEM, "系统"),
    (ATTR_ARCHIVE, "存档"),
    (ATTR_COMPRESSED, "压缩"),
    (ATTR_SPARSE, "稀疏"),
]

# 无扩展名文件的归类标签（避免各处重复字面量）
NO_EXT = "(无扩展名)"

# 顶层同名冲突：文件与目录可能同名（Windows 上同一目录内不允许，跨层级不冲突）


class SnsParseError(ValueError):
    """快照结构违例（截断 / 栈不平衡 / 魔数错误）。绝不用部分数据兜底。"""


def decode_name(raw: bytes) -> str:
    """Base64 名称解码。

    `b64decode(validate=False)` 自动丢弃 MIME 换行（快照里可能嵌 `\\r\\n`）。
    编码尝试顺序：utf-8 → gbk → utf-16-le → latin-1(replace)。
    本机实测中文文件名为 GBK，utf-8 优先是为兼容更早的导出。
    """
    blob = base64.b64decode(raw)
    for enc in ("utf-8", "gbk", "utf-16-le"):
        try:
            return blob.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return blob.decode("latin-1", "replace")


def filetime_to_str(ft: int) -> Optional[str]:
    """FILETIME → 'YYYY-MM-DD HH:MM'。目录时间戳全 0，返回 None。"""
    if ft <= 0:
        return None
    try:
        return datetime.fromtimestamp((ft - _FT_TO_UNIX) / 1e7).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return None


def split_ext(name: str) -> str:
    """取小写扩展名（不含点）。无扩展名 → ''。`.gitignore` 这类点开头文件视为无扩展名。"""
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return ""
    return name[dot + 1 :].lower()


def attr_names(attrs: int) -> List[str]:
    """FILE_ATTRIBUTE 位 → 中文名列表（按固定顺序，未知位忽略）。"""
    return [label for bit, label in _ATTR_NAMES if attrs & bit]


class Entry:
    """快照里的一个节点：文件或目录。

    用 `__slots__` 而非 dict：整盘快照有 85 万+ 节点，dict 每节点约 300+ 字节，
    slots 能省下一半以上内存，是本工具能吃下 67MB 快照的关键。
    """

    __slots__ = (
        "name",
        "size",
        "mtime",
        "attrs",
        "parent",
        "children",
        "is_dir",
        "ext",
        "tag",
        "n_files",
        "n_dirs",
    )

    def __init__(
        self,
        name: str,
        size: int,
        mtime: Optional[str] = None,
        attrs: int = 0,
        is_dir: bool = False,
    ) -> None:
        self.name = name
        self.size = size
        self.mtime = mtime
        self.attrs = attrs
        self.parent: Optional["Entry"] = None
        self.children: Optional[List["Entry"]] = [] if is_dir else None
        self.is_dir = is_dir
        self.ext = "" if is_dir else split_ext(name)
        self.tag: Optional[str] = None  # None / "red" / "yellow" / "green" / "blue"
        self.n_files = 0  # 子树文件总数（文件自身为 0）
        self.n_dirs = 0  # 子树目录总数（不含自身）

    # -- 便捷属性 -----------------------------------------------------------

    @property
    def display_name(self) -> str:
        """无扩展名文件加占位标签，便于类型面板统计对齐。"""
        return self.name

    @property
    def type_label(self) -> str:
        """用于界面展示的类型短标签。"""
        if self.is_dir:
            return "目录"
        return self.ext or NO_EXT

    def full_path(self) -> str:
        """完整 Windows 路径（反斜杠）。深度有限，逐级上溯开销可接受。"""
        parts: List[str] = []
        node: Optional[Entry] = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        parts.reverse()
        prefix = node.name if node is not None else ""
        prefix = prefix.rstrip("\\/")
        if not parts:
            return prefix + "\\"
        return prefix + "\\" + "\\".join(parts)

    def depth(self) -> int:
        d = 0
        node = self.parent
        while node is not None:
            d += 1
            node = node.parent
        return d

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = "D" if self.is_dir else "F"
        return f"<Entry {kind} {self.name!r} size={self.size}>"


class Snapshot:
    """一份解析完成的快照：根节点 + 卷级统计。"""

    __slots__ = ("root", "used_bytes", "free_bytes", "unknown_bytes", "n_files", "n_dirs", "source_path")

    def __init__(
        self,
        root: Entry,
        used_bytes: int,
        free_bytes: int,
        unknown_bytes: int,
        n_files: int,
        n_dirs: int,
        source_path: Path,
    ) -> None:
        self.root = root
        self.used_bytes = used_bytes
        self.free_bytes = free_bytes
        self.unknown_bytes = unknown_bytes
        self.n_files = n_files
        self.n_dirs = n_dirs
        self.source_path = source_path

    @property
    def total_bytes(self) -> int:
        return self.used_bytes + self.free_bytes

    @property
    def is_complete(self) -> bool:
        """unknown_bytes == 0 表示扫描完整（无未扫描区域）。"""
        return self.unknown_bytes == 0

    @property
    def volume_label(self) -> str:
        return self.root.name.rstrip("\\/")

    def scanned_at(self) -> Optional[str]:
        """快照文件的 mtime，作为「数据时点」。"""
        try:
            return datetime.fromtimestamp(self.source_path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
        except OSError:
            return None

    @property
    def file_size(self) -> Optional[int]:
        """快照文件本身体积（展示用）。文件已被删或不可读时返回 None。"""
        try:
            return self.source_path.stat().st_size
        except OSError:
            return None


def parse_bytes(data: bytes, source_path: Optional[Path] = None) -> Snapshot:
    """解析 .sns 字节流，返回 Snapshot。任何结构违例抛 SnsParseError。

    栈语义：每条记录逻辑入栈；记录尾部的 `01 00`×k 从栈顶弹出 k 层。
    - 文件 / 伪记录：第 1 个 pop 关闭自己（本就不入栈），多余的关闭祖先
    - 目录有子项：自身 pops=0，由最后一个子项记录的额外 pop 关闭
    - 空目录：自身 pops=1
    """
    n = len(data)
    root: Optional[Entry] = None
    stack: List[Entry] = []
    pos = 0
    n_files = 0
    n_dirs = 0
    free_bytes = 0
    unknown_bytes = 0

    while pos < n:
        if pos + 6 > n or data[pos] != SNS_MAGIC or data[pos + 1] not in (1, 2, 3, 4, 5):
            raise SnsParseError(f"记录头非法，偏移 {pos}")
        rec_type = data[pos + 1]
        (name_len,) = struct.unpack_from("<I", data, pos + 2)
        name_end = pos + 6 + name_len
        if name_end + 42 > n:
            raise SnsParseError(f"记录被截断，偏移 {pos}")

        name = decode_name(data[pos + 6 : name_end])
        (size,) = struct.unpack_from("<q", data, name_end)
        (attrs,) = struct.unpack_from("<I", data, name_end + 12)
        (mtime_ft,) = struct.unpack_from("<q", data, name_end + 32)

        if data[name_end + 40 : name_end + 42] != b"\x00\x00":
            raise SnsParseError(f"缺少记录结束标志，偏移 {pos}（name={name!r}）")

        cursor = name_end + 42
        pops = 0
        while cursor + 2 <= n and data[cursor : cursor + 2] == b"\x01\x00":
            pops += 1
            cursor += 2

        if rec_type == TYPE_FILE:
            if not stack:
                raise SnsParseError(f"文件记录出现在任何目录之外，偏移 {pos}（name={name!r}）")
            entry = Entry(name, size, filetime_to_str(mtime_ft), attrs, is_dir=False)
            entry.parent = stack[-1]
            assert stack[-1].children is not None
            stack[-1].children.append(entry)
            n_files += 1
        elif rec_type in (TYPE_FREE, TYPE_UNKNOWN):
            # 卷的直接子项伪记录：不进树，（避免污染 treemap 与统计）
            if rec_type == TYPE_FREE:
                free_bytes = size
            else:
                unknown_bytes = size
        else:  # 卷 / 目录
            entry = Entry(name, size, filetime_to_str(mtime_ft), attrs, is_dir=True)
            if stack:
                entry.parent = stack[-1]
                assert stack[-1].children is not None
                stack[-1].children.append(entry)
            elif root is None:
                root = entry
            else:
                raise SnsParseError(f"出现第二个根级目录记录，偏移 {pos}（name={name!r}）")
            stack.append(entry)
            if rec_type == TYPE_DIR:
                n_dirs += 1

        ancestor_pops = pops - 1 if rec_type in LEAF_TYPES else pops
        if ancestor_pops > len(stack):
            raise SnsParseError(
                f"空栈上弹出，偏移 {pos}（name={name!r}, pops={pops}）"
            )
        for _ in range(ancestor_pops):
            stack.pop()
        pos = cursor

    if root is None:
        raise SnsParseError("快照中没有卷记录")
    if stack:
        raise SnsParseError(f"EOF 处仍有未闭合记录（栈深 {len(stack)}）")
    if pos != n:
        raise SnsParseError(f"遍历停在第 {pos} 字节，共 {n} 字节")

    _post_process(root)

    return Snapshot(
        root=root,
        used_bytes=root.size,
        free_bytes=free_bytes,
        unknown_bytes=unknown_bytes,
        n_files=n_files,
        n_dirs=n_dirs,
        source_path=source_path or Path("<bytes>"),
    )


def parse_file(path: "Path | str") -> Snapshot:
    """解析 .sns 文件。`path` 接受 str 或 Path（CLI 传进来的是 str）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"快照文件不存在：{path}")
    return parse_bytes(path.read_bytes(), source_path=path)


def _post_process(root: Entry) -> None:
    """后序遍历：子项按 size 降序排列 + 回填子树文件/目录计数。

    排序让 treemap 布局稳定（大块优先、squarified 效果最好），
    计数供详情面板与状态栏展示。
    """
    for node in _walk_dirs(root):
        if node.children:
            node.children.sort(key=lambda e: e.size, reverse=True)


def _walk_dirs(root: Entry) -> Iterator[Entry]:
    """自底向上遍历所有目录节点（迭代实现，避免深树递归开销）。"""
    stack: List[tuple] = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if visited:
            total_files = 0
            total_dirs = 0
            if node.children:
                for child in node.children:
                    if child.is_dir:
                        total_files += child.n_files
                        total_dirs += child.n_dirs + 1
                    else:
                        total_files += 1
            node.n_files = total_files
            node.n_dirs = total_dirs
            yield node
        else:
            stack.append((node, True))
            if node.children:
                for child in node.children:
                    if child.is_dir:
                        stack.append((child, False))


def iter_subtree(node: Entry) -> Iterator[Entry]:
    """深度优先遍历子树（含 node 自身）。"""
    stack: List[Entry] = [node]
    while stack:
        cur = stack.pop()
        yield cur
        if cur.children:
            stack.extend(reversed(cur.children))


def iter_with_paths(root: Entry) -> Iterator[tuple]:
    """遍历子树并携带累积路径，返回 (entry, win_path)。

    比逐个 `Entry.full_path()` 快得多（后者每个节点都要上溯到根，
    整盘导出是 O(n×depth)）。整盘导出 85 万行实测差一个数量级。
    """
    root_prefix = root.name.rstrip("\\/")
    stack: List[tuple] = [(root, root_prefix + "\\")]
    while stack:
        node, path = stack.pop()
        yield node, path
        if node.children:
            for child in reversed(node.children):
                stack.append((child, path + child.name if path.endswith("\\") else path + "\\" + child.name))


def human_size(n: int) -> str:
    """字节 → 人类可读（二进制单位，1 位小数；与 scan_disk.py 口径一致）。"""
    sign = "-" if n < 0 else ""
    v = float(abs(n))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if v < 1024 or unit == "PB":
            if unit == "B":
                return f"{sign}{int(v)} {unit}"
            return f"{sign}{v:.1f} {unit}"
        v /= 1024
    return f"{sign}{v:.1f} PB"  # pragma: no cover - 不可达


def parse_size_text(text: str) -> Optional[int]:
    """'500mb' / '1.5gb' / '2048' → 字节数。无法解析返回 None。"""
    s = text.strip().lower().replace(" ", "")
    if not s:
        return None
    units = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    for suffix in ("tb", "gb", "mb", "kb", "b"):
        if s.endswith(suffix):
            num = s[: -len(suffix)]
            break
    else:
        num, suffix = s, "b"
    try:
        value = float(num)
    except ValueError:
        return None
    return int(value * units[suffix])
