"""SpaceSniffer 快照浏览器（tools/spacesniffer/）单元测试。

覆盖四层纯逻辑，全部离线可跑、不依赖真实快照：

1. `sns_model` —— 用**自造 .sns 字节流**驱动解析器（含栈语义、伪记录、各类违例）
2. `sns_model` × `workspace/disk_manager/scripts/parse_sns.py` —— 交叉校验，防止两份
   实现对同一份格式规范漂移（这是 `sns_model` docstring 里承诺的守护点）
3. `treemap` —— squarified 面积/顺序/覆盖、边界、命中测试与暴力法对拍
4. `filtering` / `filetypes` / `tagging` / `report` —— 过滤语义、分类配色、标记、导出

测试原则：不 mock 被测对象；对自造的 .sns 字节流做**双向**断言（能解析出期望结构 +
结构违例必须报错，绝不用部分数据兜底）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SN_DIR = REPO_ROOT / "tools" / "spacesniffer"
WORKSPACE_PARSER = REPO_ROOT / "workspace" / "disk_manager" / "scripts" / "parse_sns.py"

for _path in (str(REPO_ROOT), str(SN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 下面这几个模块靠上面运行时的 sys.path 注入才能导入（tools/spacesniffer 不是包，
# 项目约定如此）。pyright 看不到运行时注入，所以逐行关掉 missingImports ——
# 保留逐行而不是整文件关，是为了让组名写错这类真错误仍然报出来。
import filetypes  # pyright: ignore[reportMissingImports]  # noqa: E402
import filtering  # pyright: ignore[reportMissingImports]  # noqa: E402
import report  # pyright: ignore[reportMissingImports]  # noqa: E402
import sns_model  # pyright: ignore[reportMissingImports]  # noqa: E402
import tagging  # pyright: ignore[reportMissingImports]  # noqa: E402
import treemap  # pyright: ignore[reportMissingImports]  # noqa: E402
from lib.ui import tokens  # noqa: E402

# ---------------------------------------------------------------------------
# 自造 .sns：编码器
# ---------------------------------------------------------------------------

TYPE_VOLUME, TYPE_FILE, TYPE_DIR, TYPE_FREE, TYPE_UNKNOWN = 1, 2, 3, 4, 5

_FT_TO_UNIX = 116444736000000000  # FILETIME(100ns, 1601 纪元) → Unix(秒, 1970 纪元)

ATTR_HIDDEN = 0x02
ATTR_ARCHIVE = 0x20


def filetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """构造 FILETIME，方便在测试里写人类可读的时间（本地时区，与解析口径一致）。"""
    unix = datetime(year, month, day, hour, minute).timestamp()
    return int(unix * 1e7) + _FT_TO_UNIX


def record(
    rec_type: int,
    name: str,
    size: int = 0,
    *,
    attrs: int = 0,
    mtime_ft: int = 0,
    waste: int = 0,
    pops: int = 0,
) -> bytes:
    """按格式规范拼一条记录。名称一律按 GBK 编码 —— 本机快照就是这个口径。"""
    encoded = base64.b64encode(name.encode("gbk"))
    out = struct.pack("<BBI", 0x02, rec_type, len(encoded)) + encoded
    out += struct.pack("<qII", size, waste, attrs)
    out += struct.pack("<qqq", 0, 0, mtime_ft)
    out += b"\x00\x00"
    return out + b"\x01\x00" * pops


# 节点规格：
#   ("volume", 名, [子项], 已用字节)
#   ("dir",    名, [子项], 目录大小)
#   ("file",   名, 大小[, attrs[, mtime_ft]])
#   ("free"|"unknown", 名, 大小)
def emit(node) -> bytes:
    """递归把节点规格编成记录序列。

    pops 的分配规则：每个节点负责在末尾闭合自己，因此**最后一个子项**要多带 1 个 pop
    去关父级。这样每个子树都是自包含的字节段，拼接天然正确。
    """
    kind = node[0]
    name = node[1]

    if kind == "file":
        attrs = node[3] if len(node) > 3 else 0
        mtime_ft = node[4] if len(node) > 4 else 0
        return record(TYPE_FILE, name, node[2], attrs=attrs, mtime_ft=mtime_ft, pops=1)

    if kind in ("free", "unknown"):
        return record(TYPE_FREE if kind == "free" else TYPE_UNKNOWN, name, node[2], pops=1)

    rec_type = TYPE_VOLUME if kind == "volume" else TYPE_DIR
    kids = node[2]
    size = node[3] if len(node) > 3 else 0
    if not kids:
        return record(rec_type, name, size, pops=1)

    out = record(rec_type, name, size, pops=0)
    for index, kid in enumerate(kids):
        chunk = emit(kid)
        if index == len(kids) - 1:
            chunk += b"\x01\x00"
        out += chunk
    return out


def build(spec) -> bytes:
    return emit(spec)


# --- 样例树（字节数刻意凑成「目录大小 = 子树求和」，便于断言口径） -----------
_FILE_DOCX = 4_000_000
_FILE_MP3 = 9_000_000
_FILE_README = 1_024
_FILE_HIDDEN = 2_048
_FILE_NTFS = 500_000
_FILE_PAGEFILE = 5_000_000_000

_ADMIN_SIZE = _FILE_DOCX + _FILE_MP3 + _FILE_README + _FILE_HIDDEN  # 13_003_072
_USERS_SIZE = _ADMIN_SIZE
_WINDOWS_SIZE = _FILE_NTFS
_VOLUME_USED = _USERS_SIZE + _WINDOWS_SIZE + _FILE_PAGEFILE + 86_496_928  # 留出 NTFS 元数据差额
_VOLUME_FREE = 15_000_000_000

SAMPLE_TREE = (
    "volume",
    "C:\\",
    [
        (
            "dir",
            "Users",
            [
                (
                    "dir",
                    "admin",
                    [
                        ("file", "季度报告.docx", _FILE_DOCX, ATTR_ARCHIVE, filetime(2026, 8, 1, 10, 30)),
                        ("file", "音乐.mp3", _FILE_MP3, ATTR_ARCHIVE, filetime(2024, 1, 15, 8, 0)),
                        ("file", "~隐藏文件.txt", _FILE_HIDDEN, ATTR_HIDDEN, filetime(2026, 9, 1, 0, 0)),
                        ("file", "README", _FILE_README, ATTR_ARCHIVE, filetime(2025, 12, 31, 23, 59)),
                    ],
                    _ADMIN_SIZE,
                ),
                ("dir", "public", [], 0),
            ],
            _USERS_SIZE,
        ),
        (
            "dir",
            "Windows",
            [
                ("dir", "System32", [("dir", "drivers", [("file", "ntfs.sys", _FILE_NTFS, 0x20)], _FILE_NTFS)], _FILE_NTFS),
            ],
            _WINDOWS_SIZE,
        ),
        ("file", "pagefile.sys", _FILE_PAGEFILE, ATTR_HIDDEN),
        ("free", "可用空间", _VOLUME_FREE),
        ("unknown", "未知 (尚未扫描) 空间", 0),
    ],
    _VOLUME_USED,
)

# 目录记录条数（不含卷）：Users / admin / public / Windows / System32 / drivers
SAMPLE_N_DIRS = 6
SAMPLE_N_FILES = 6


def parse_sample() -> sns_model.Snapshot:
    return sns_model.parse_bytes(build(SAMPLE_TREE))


# ===========================================================================
# 1. sns_model —— 解析器与树模型
# ===========================================================================


class TestSnsModel:
    def test_tree_shape_and_sorting(self):
        root = parse_sample().root
        assert root.name == "C:\\"
        assert root.is_dir
        assert [c.name for c in root.children] == [
            "pagefile.sys",
            "Users",
            "Windows",
        ], "子项必须按 size 降序（treemap 布局依赖这个顺序）"

        users = root.children[1]
        assert users.parent is root
        assert [c.name for c in users.children] == ["admin", "public"]

        admin = users.children[0]
        assert [c.name for c in admin.children] == [
            "音乐.mp3",
            "季度报告.docx",
            "~隐藏文件.txt",
            "README",
        ]

    def test_volume_stats_and_pseudo_records(self):
        snap = parse_sample()
        assert snap.used_bytes == _VOLUME_USED
        assert snap.free_bytes == _VOLUME_FREE
        assert snap.unknown_bytes == 0
        assert snap.is_complete
        assert snap.total_bytes == _VOLUME_USED + _VOLUME_FREE
        assert snap.volume_label == "C:"
        # 伪记录不进树
        names = {c.name for c in snap.root.children}
        assert "可用空间" not in names and "未知 (尚未扫描) 空间" not in names

    def test_incomplete_snapshot_flag(self):
        spec = ("volume", "D:\\", [("file", "a.bin", 10), ("free", "free", 90), ("unknown", "unknown", 50)], 10)
        snap = sns_model.parse_bytes(build(spec))
        assert not snap.is_complete
        assert snap.unknown_bytes == 50
        assert snap.free_bytes == 90

    def test_counts(self):
        snap = parse_sample()
        assert snap.n_files == SAMPLE_N_FILES
        assert snap.n_dirs == SAMPLE_N_DIRS
        assert snap.root.n_files == SAMPLE_N_FILES
        assert snap.root.n_dirs == SAMPLE_N_DIRS

    def test_names_are_gbk_decoded(self):
        admin = parse_sample().root.children[1].children[0]
        names = [c.name for c in admin.children]
        assert "季度报告.docx" in names
        assert "音乐.mp3" in names

    def test_full_path(self):
        snap = parse_sample()
        target = snap.root.children[2].children[0].children[0].children[0]
        assert target.name == "ntfs.sys"
        assert target.full_path() == "C:\\Windows\\System32\\drivers\\ntfs.sys"
        assert snap.root.full_path() == "C:\\"

    def test_ext_and_type_label(self):
        admin = parse_sample().root.children[1].children[0]
        by_name = {c.name: c for c in admin.children}
        assert by_name["季度报告.docx"].ext == "docx"
        assert by_name["音乐.mp3"].ext == "mp3"
        assert by_name["README"].ext == ""
        assert by_name["README"].type_label == sns_model.NO_EXT
        assert parse_sample().root.type_label == "目录"

    def test_mtime_comes_from_third_filetime(self):
        admin = parse_sample().root.children[1].children[0]
        by_name = {c.name: c for c in admin.children}
        assert by_name["季度报告.docx"].mtime == "2026-08-01 10:30"
        assert by_name["音乐.mp3"].mtime == "2024-01-15 08:00"
        # 目录时间戳全 0 → None（时间过滤据此跳过目录）
        assert parse_sample().root.mtime is None

    def test_attrs(self):
        admin = parse_sample().root.children[1].children[0]
        by_name = {c.name: c for c in admin.children}
        assert by_name["~隐藏文件.txt"].attrs & ATTR_HIDDEN
        assert not by_name["季度报告.docx"].attrs & ATTR_HIDDEN
        assert sns_model.attr_names(ATTR_HIDDEN | ATTR_ARCHIVE) == ["隐藏", "存档"]

    def test_empty_dir_is_kept(self):
        public = parse_sample().root.children[1].children[1]
        assert public.name == "public"
        assert public.is_dir
        assert public.children == []


class TestSnsModelErrors:
    """结构违例必须抛错 —— 不允许用部分数据兜底。"""

    def test_bad_magic(self):
        data = bytearray(build(SAMPLE_TREE))
        data[0] = 0x03
        with pytest.raises(sns_model.SnsParseError, match="记录头非法"):
            sns_model.parse_bytes(bytes(data))

    def test_truncated_record(self):
        data = build(SAMPLE_TREE)
        with pytest.raises(sns_model.SnsParseError):
            sns_model.parse_bytes(data[: len(data) // 2])

    def test_missing_end_marker(self):
        blob = bytearray(record(TYPE_VOLUME, "C:\\", pops=0))
        blob[-1] = 0x01  # 结束标志 00 00 → 00 01
        with pytest.raises(sns_model.SnsParseError, match="缺少记录结束标志"):
            sns_model.parse_bytes(bytes(blob))

    def test_file_outside_dir(self):
        with pytest.raises(sns_model.SnsParseError, match="文件记录出现在任何目录之外"):
            sns_model.parse_bytes(record(TYPE_FILE, "x.txt", 1, pops=1))

    def test_pop_on_empty_stack(self):
        # 卷记录自己 pops=2 → 第 1 个关自己，第 2 个弹空栈
        with pytest.raises(sns_model.SnsParseError, match="空栈上弹出"):
            sns_model.parse_bytes(record(TYPE_VOLUME, "C:\\", pops=2))

    def test_unclosed_at_eof(self):
        with pytest.raises(sns_model.SnsParseError, match="仍有未闭合记录"):
            sns_model.parse_bytes(record(TYPE_VOLUME, "C:\\", pops=0))

    def test_second_root_record(self):
        # 先把第一个卷闭合（空目录 pops=2 关掉自己 + 卷），栈清空后再来一个卷记录
        blob = (
            record(TYPE_VOLUME, "C:\\", pops=0)
            + record(TYPE_DIR, "Empty", pops=2)
            + record(TYPE_VOLUME, "D:\\", pops=1)
        )
        with pytest.raises(sns_model.SnsParseError, match="第二个根级目录记录"):
            sns_model.parse_bytes(blob)

    def test_no_volume_record(self):
        with pytest.raises(sns_model.SnsParseError, match="没有卷记录"):
            sns_model.parse_bytes(b"")

    def test_leaf_pseudo_records_do_not_pop_the_volume(self):
        """伪记录在流中间时不该弹卷 —— pops=1 的第 1 个 pop 归它自己。"""
        spec = (
            "volume",
            "C:\\",
            [
                ("file", "a.bin", 10),
                ("free", "free", 90),
                ("unknown", "unknown", 0),
                ("file", "b.bin", 20),
            ],
            30,
        )
        snap = sns_model.parse_bytes(build(spec))
        assert [c.name for c in snap.root.children] == ["b.bin", "a.bin"]
        assert snap.free_bytes == 90


class TestSnsHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (1024**3, "1.0 GB"),
            (1024**4, "1.0 TB"),
        ],
    )
    def test_human_size(self, value, expected):
        assert sns_model.human_size(value) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", None),
            ("abc", None),
            ("2048", 2048),
            ("500mb", 500 * 1024**2),
            ("1.5gb", int(1.5 * 1024**3)),
            ("2 TB", 2 * 1024**4),
            ("10kb", 10 * 1024),
        ],
    )
    def test_parse_size_text(self, text, expected):
        assert sns_model.parse_size_text(text) == expected

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("a.txt", "txt"),
            ("a.TXT", "txt"),
            ("archive.tar.gz", "gz"),
            ("README", ""),
            (".gitignore", ""),
            ("trailing.", ""),
        ],
    )
    def test_split_ext(self, name, expected):
        assert sns_model.split_ext(name) == expected


# ===========================================================================
# 2. 交叉校验 —— 与 workspace/disk_manager 的解析器对拍
# ===========================================================================


def _load_workspace_parser():
    spec = importlib.util.spec_from_file_location("disk_manager_parse_sns", WORKSPACE_PARSER)
    assert spec is not None and spec.loader is not None, f"无法加载 {WORKSPACE_PARSER}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flatten_mine(entry) -> dict:
    """把 sns_model 的树拍成 {路径: (size, is_dir)}，便于跨实现比对。"""
    out: dict = {}
    stack = [(entry, entry.name.rstrip("\\/"))]
    while stack:
        node, path = stack.pop()
        out[path] = (node.size, node.is_dir)
        for child in node.children or ():
            stack.append((child, path + "\\" + child.name))
    return out


def _flatten_theirs(node, prefix: str = "") -> dict:
    """把 workspace 解析器的 dict 树拍成同一形状。

    那边把文件放在 `files_top`、目录放在 `children`，这里合并后比对。
    """
    path = prefix + node["name"].rstrip("\\/") if not prefix else prefix + "\\" + node["name"]
    out = {path: (node["size"], True)}
    for child in node["children"]:
        out.update(_flatten_theirs(child, path))
    for item in node["files_top"]:
        out[path + "\\" + item["name"]] = (item["size"], False)
    return out


def test_cross_check_with_disk_manager():
    """两份实现解析同一份字节流必须得到同一棵树。

    这是防漂移的关键用例：格式规范改了两边都得改，否则这里红。
    """
    theirs = _load_workspace_parser()
    data = bytes(build(SAMPLE_TREE))

    mine = sns_model.parse_bytes(data)
    their_root, their_meta = theirs.parse_sns(data)
    theirs.finalize(their_root, 0)  # keep_files=0 → 全量保留文件节点

    flat_mine = _flatten_mine(mine.root)
    flat_theirs = _flatten_theirs(their_root)

    assert set(flat_mine) == set(flat_theirs), (
        f"路径集合不一致：仅我有 {sorted(set(flat_mine) - set(flat_theirs))}，"
        f"仅它有 {sorted(set(flat_theirs) - set(flat_mine))}"
    )
    for path in flat_mine:
        assert flat_mine[path] == flat_theirs[path], f"{path} 的大小/类型不一致"

    assert mine.n_files == their_meta["files"]
    assert mine.free_bytes == their_meta["free_bytes"]
    assert mine.unknown_bytes == their_meta["unknown_bytes"]
    assert mine.used_bytes == their_root["size"]


def test_cross_check_agrees_on_rejection():
    """违例数据两份实现都必须拒绝（防一边悄悄放宽兜底）。"""
    theirs = _load_workspace_parser()
    bad = record(TYPE_FILE, "orphan.txt", 1, pops=1)

    with pytest.raises(sns_model.SnsParseError):
        sns_model.parse_bytes(bad)
    with pytest.raises(ValueError):
        theirs.parse_sns(bad)


# ===========================================================================
# 3. treemap —— squarified 布局
# ===========================================================================


class TestSquarify:
    def test_covers_rect_exactly(self):
        rects = treemap.squarify([50, 30, 20], (0, 0, 100, 100))
        assert len(rects) == 3
        assert sum(w * h for _, _, w, h in rects) == pytest.approx(100 * 100, rel=1e-9)

    def test_order_aligned_with_input(self):
        """返回值必须与入参一一对应 —— 调用方按下标取 Entry，错位就会张冠李戴。"""
        rects = treemap.squarify([1, 100, 10], (0, 0, 300, 300))
        areas = [w * h for _, _, w, h in rects]
        assert areas[1] == max(areas)
        assert areas[0] == min(areas)

    def test_zero_weight_keeps_position(self):
        rects = treemap.squarify([5, 0, 5], (0, 0, 100, 100))
        assert rects[1] == (0, 0, 0.0, 0.0), "零权重返回零矩形，而不是被丢弃后错位"
        assert rects[0][2] * rects[0][3] == pytest.approx(5000)
        assert rects[2][2] * rects[2][3] == pytest.approx(5000)

    def test_degenerate_inputs(self):
        assert treemap.squarify([1, 2], (0, 0, 0, 100)) == [(0, 0, 0.0, 0.0)] * 2
        assert treemap.squarify([0, 0], (0, 0, 10, 10)) == [(0, 0, 0.0, 0.0)] * 2
        assert treemap.squarify([], (0, 0, 10, 10)) == []

    def test_many_equal_weights_tile_without_gaps(self):
        rects = treemap.squarify([1] * 400, (0, 0, 800, 600))
        assert sum(w * h for _, _, w, h in rects) == pytest.approx(800 * 600, rel=1e-9)
        assert all(w > 0 and h > 0 for _, _, w, h in rects)


def _entry_tree() -> sns_model.Entry:
    """造一棵可直接喂给布局引擎的 Entry 树（目录 size 显式给值）。"""
    root = sns_model.Entry("C:\\", 1000, is_dir=True)
    big = sns_model.Entry("Big", 600, is_dir=True)
    big.parent = root
    for index in range(6):
        leaf = sns_model.Entry(f"f{index}.bin", 100, is_dir=False)
        leaf.parent = big
        assert big.children is not None
        big.children.append(leaf)

    small = sns_model.Entry("Small", 300, is_dir=True)
    small.parent = root
    for index in range(3):
        leaf = sns_model.Entry(f"s{index}.log", 100, is_dir=False)
        leaf.parent = small
        assert small.children is not None
        small.children.append(leaf)

    empty = sns_model.Entry("Empty", 0, is_dir=True)
    empty.parent = root
    assert root.children is not None
    root.children.extend([big, small, empty])
    return root


def _overlap_area(a, b) -> float:
    """QRectF.intersects 把「共边」也算相交，所以要自己算重叠面积。"""
    dx = min(a.right(), b.right()) - max(a.left(), b.left())
    dy = min(a.bottom(), b.bottom()) - max(a.top(), b.top())
    return dx * dy if dx > 0 and dy > 0 else 0.0


class TestBuildLayout:
    def test_tiles_inside_viewport(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        assert layout.tiles
        for tile in layout.tiles:
            assert tile.rect.left() >= -1e-6
            assert tile.rect.top() >= -1e-6
            assert tile.rect.right() <= 800 + 1e-6
            assert tile.rect.bottom() <= 600 + 1e-6

    def test_direct_children_cover_viewport(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        realized = [t for t in layout.roots if not t.is_gap]
        assert [t.label for t in realized] == ["Big", "Small"], "Empty 面积为 0 被跳过"
        assert len(layout.roots) == 3, "目录自身 1000 > 子项之和 900，差额应变成一个聚合块"
        covered = sum(t.rect.width() * t.rect.height() for t in layout.roots)
        assert covered == pytest.approx(800 * 600, rel=1e-6)

    def test_roots_do_not_overlap(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        for i in range(len(layout.roots)):
            for j in range(i + 1, len(layout.roots)):
                assert _overlap_area(layout.roots[i].rect, layout.roots[j].rect) == 0.0

    def test_siblings_do_not_overlap_recursively(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        containers = [t for t in layout.tiles if t.children]
        assert containers
        for tile in containers:
            for i in range(len(tile.children)):
                for j in range(i + 1, len(tile.children)):
                    assert _overlap_area(tile.children[i].rect, tile.children[j].rect) == 0.0

    def test_depth_parent_and_children_links(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        assert all(t.depth == 0 and t.parent is None for t in layout.roots)
        inner = [t for t in layout.tiles if t.depth == 1]
        assert inner
        for tile in inner:
            assert tile.parent in layout.roots
            assert tile in tile.parent.children

    def test_header_reserves_space_for_children(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600, treemap.LayoutConfig(show_headers=True))
        headered = [t for t in layout.tiles if t.has_header]
        assert headered
        for tile in headered:
            assert tile.rect.height() >= treemap.HEADER_MIN_H
            for child in tile.children:
                assert child.rect.top() >= tile.rect.top() + treemap.HEADER_H - 1e-6

    def test_headers_can_be_disabled(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600, treemap.LayoutConfig(show_headers=False))
        assert all(not t.has_header for t in layout.tiles)

    def test_min_side_prunes_tiny_tiles(self):
        loose = treemap.build_layout(_entry_tree(), 400, 400)
        strict = treemap.build_layout(_entry_tree(), 400, 400, treemap.LayoutConfig(min_side=120.0))
        assert len(strict.tiles) < len(loose.tiles), "放大下限后应当有瓦片被剪掉"
        for tile in strict.tiles:
            assert tile.rect.width() >= 120 - 1e-6
            assert tile.rect.height() >= 120 - 1e-6

    def test_gap_tile_absorbs_unaccounted_bytes(self):
        """目录自身大小 > 子项之和时（NTFS 元数据），差额要变成聚合块而不是留白。"""
        root = sns_model.Entry("C:\\", 1000, is_dir=True)
        child = sns_model.Entry("only", 400, is_dir=False)
        child.parent = root
        assert root.children is not None
        root.children.append(child)

        layout = treemap.build_layout(root, 400, 400)
        gaps = [t for t in layout.tiles if t.is_gap]
        assert len(gaps) == 1
        ratio = (gaps[0].rect.width() * gaps[0].rect.height()) / (400 * 400)
        assert ratio == pytest.approx(0.6, rel=1e-6)

    def test_dropped_small_entries_fold_into_gap_tile(self):
        root = sns_model.Entry("C:\\", 10002, is_dir=True)
        for name, size in (("huge.bin", 10000), ("tiny1.bin", 1), ("tiny2.bin", 1)):
            child = sns_model.Entry(name, size, is_dir=False)
            child.parent = root
            assert root.children is not None
            root.children.append(child)

        layout = treemap.build_layout(root, 400, 400, treemap.LayoutConfig(min_side=40.0))
        assert [t.label for t in layout.tiles] == ["huge.bin"], "两个 1 字节条目应被剪枝"
        # 被剪掉的 2 字节 + 目录自身与子项之和的差额，合并成一个聚合块（面积过小未落图）

    def test_empty_dir_yields_no_tiles(self):
        root = sns_model.Entry("C:\\", 0, is_dir=True)
        layout = treemap.build_layout(root, 400, 400)
        assert layout.tiles == []
        assert layout.hit_test(200, 200) is None

    def test_zero_viewport(self):
        assert treemap.build_layout(_entry_tree(), 0, 0).tiles == []


class TestHitTest:
    def test_matches_brute_force(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        for x in range(0, 800, 17):
            for y in range(0, 600, 13):
                expected = None
                for tile in reversed(layout.tiles):
                    rect = tile.rect
                    if rect.left() <= x < rect.right() and rect.top() <= y < rect.bottom():
                        expected = tile
                        break
                assert layout.hit_test(x, y) is expected, f"({x},{y}) 命中结果与暴力法不一致"

    def test_outside_returns_none(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        assert layout.hit_test(-5, -5) is None
        assert layout.hit_test(900, 300) is None

    def test_returns_deepest_tile(self):
        layout = treemap.build_layout(_entry_tree(), 800, 600)
        deepest = max(layout.tiles, key=lambda t: t.depth)
        center = deepest.rect.center()
        hit = layout.hit_test(center.x(), center.y())
        assert hit is not None
        assert hit.depth >= deepest.depth


# ===========================================================================
# 4. filtering —— 过滤 DSL
# ===========================================================================


def _filter_sample():
    """带标记、属性、时间、扩展名的样例树（内容与 SAMPLE_TREE 不同，专供过滤用）。"""
    root = sns_model.Entry("C:\\", 10_000, is_dir=True)
    docs = sns_model.Entry("Docs", 6_000, is_dir=True)
    docs.parent = root
    media = sns_model.Entry("Media", 3_500, is_dir=True)
    media.parent = root
    empty = sns_model.Entry("Empty", 500, is_dir=True)
    empty.parent = root

    rows = [
        (docs, "a.jpg", 1_000, ATTR_ARCHIVE, "2026-01-10 10:00", "red"),
        (docs, "b.JPG", 2_000, ATTR_ARCHIVE, "2020-05-05 10:00", None),
        (docs, "c.png", 3_000, ATTR_HIDDEN, "2026-09-01 10:00", "blue"),
        (media, "d.mp4", 3_000, ATTR_ARCHIVE, "2019-01-01 10:00", None),
        (media, "e.txt", 500, ATTR_ARCHIVE, "2026-08-20 10:00", None),
    ]
    for parent, name, size, attrs, mtime, tag in rows:
        node = sns_model.Entry(name, size, mtime=mtime, attrs=attrs, is_dir=False)
        node.parent = parent
        node.tag = tag
        assert parent.children is not None
        parent.children.append(node)

    assert root.children is not None
    root.children.extend([docs, media, empty])
    return root


class TestFilterParsing:
    def test_empty_expression(self):
        assert filtering.Filter("").is_empty
        assert filtering.Filter("   ").is_empty
        assert filtering.Filter(";;").is_empty

    def test_extension_shorthand(self):
        flt = filtering.Filter("jpg")
        assert "jpg" in flt.ext_include
        assert flt.matches(sns_model.Entry("x.jpg", 1))

    def test_mask_include_is_or(self):
        flt = filtering.Filter("*.jpg;*.png")
        assert flt.matches(sns_model.Entry("a.jpg", 1))
        assert flt.matches(sns_model.Entry("b.png", 1))
        assert not flt.matches(sns_model.Entry("c.gif", 1))

    def test_mask_exclude(self):
        flt = filtering.Filter("|*.dll")
        assert flt.matches(sns_model.Entry("a.jpg", 1))
        assert not flt.matches(sns_model.Entry("a.dll", 1))

    def test_mask_is_case_insensitive(self):
        assert filtering.Filter("*.JPG").matches(sns_model.Entry("a.jpg", 1))

    def test_multi_segment_wildcard_goes_through_regex(self):
        flt = filtering.Filter("*.tar.gz")
        assert flt.matches(sns_model.Entry("x.tar.gz", 1))
        assert not flt.matches(sns_model.Entry("x.gz", 1))

    def test_name_terms(self):
        flt = filtering.Filter("N:download")
        assert flt.matches(sns_model.Entry("<data_drive>:/Downloads", 1, is_dir=True))
        assert not flt.matches(sns_model.Entry("Docs", 1, is_dir=True))

    def test_name_exclude(self):
        flt = filtering.Filter("|N:node_modules")
        assert not flt.matches(sns_model.Entry("node_modules", 1, is_dir=True))
        assert flt.matches(sns_model.Entry("src", 1, is_dir=True))

    def test_size_bounds(self):
        flt = filtering.Filter(">500mb;<1gb")
        # `>` / `<` 是严格比较，内部按 ±1 字节折算上/下界
        assert flt.min_size == 500 * 1024**2 + 1
        assert flt.max_size == 1024**3 - 1
        assert flt.matches(sns_model.Entry("x", 600 * 1024**2))
        assert not flt.matches(sns_model.Entry("x", 400 * 1024**2))

    def test_time_bounds(self):
        flt = filtering.Filter(">2years")
        assert flt.older_than_days and flt.older_than_days >= 730
        assert flt.needs_mtime
        # 目录没有 mtime → 不参与时间过滤
        assert not flt.matches(sns_model.Entry("d", 1, is_dir=True))

    def test_tag_filters(self):
        assert filtering.Filter(":red").tags_include == {"red"}
        assert filtering.Filter(":all").any_tag_only
        assert filtering.Filter("|:blue").tags_exclude == {"blue"}

    def test_attr_required_and_forbidden(self):
        required = filtering.Filter("A:hidden")
        assert required.attr_required == ATTR_HIDDEN
        assert required.matches(sns_model.Entry("x", 1, attrs=ATTR_HIDDEN))
        assert not required.matches(sns_model.Entry("x", 1, attrs=ATTR_ARCHIVE))

        forbidden = filtering.Filter("|A:hidden")
        assert forbidden.attr_forbidden == ATTR_HIDDEN
        assert not forbidden.matches(sns_model.Entry("x", 1, attrs=ATTR_HIDDEN))
        assert forbidden.matches(sns_model.Entry("x", 1, attrs=ATTR_ARCHIVE))

    @pytest.mark.parametrize("expression", [">abc", ":purple", "A:xyz", "|", "N:", ">", "<"])
    def test_errors(self, expression):
        with pytest.raises(filtering.FilterError):
            filtering.Filter(expression)

    def test_can_prune_only_uses_min_size(self):
        assert filtering.Filter(">1gb").can_prune(sns_model.Entry("small", 100, is_dir=True))
        assert not filtering.Filter("*.jpg").can_prune(sns_model.Entry("small", 100, is_dir=True))

    def test_describe_is_human_readable(self):
        assert filtering.describe(filtering.Filter("")) == "无过滤"
        text = filtering.describe(filtering.Filter("*.jpg;>1mb;:red;|N:temp"))
        assert "*.jpg" in text and "red" in text and "temp" in text


def _iter_view_nodes(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(filtering.view_children(current))


def _view_leaves(node):
    return [n for n in _iter_view_nodes(node) if not n.entry.is_dir]


class TestBuildView:
    def test_extension_filter_keeps_only_matching_files(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.jpg"), root.n_files)
        assert {n.entry.ext for n in _view_leaves(result.root)} == {"jpg"}

    def test_dirs_without_hits_are_dropped(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.mp4"), root.n_files)
        names = {n.entry.name for n in _iter_view_nodes(result.root) if n.entry.is_dir}
        assert names == {"C:\\", "Media"}

    def test_hits_are_consistent(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.jpg"), root.n_files)
        assert result.file_hits == 2
        assert result.byte_hits == 3_000

    def test_exclude_leaves_no_matches(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("|*.jpg"), root.n_files)
        assert {n.entry.name for n in _view_leaves(result.root)} == {"c.png", "d.mp4", "e.txt"}

    def test_no_match_returns_none_root(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.iso"), root.n_files)
        assert result.root is None
        assert result.file_hits == 0
        assert result.byte_hits == 0

    def test_tag_writes_through_to_real_entry(self):
        """在过滤视图里打标记必须落到真实 Entry 上 —— 退出过滤不能丢。"""
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.png"), root.n_files)
        leaf = _view_leaves(result.root)[0]
        leaf.tag = "green"
        assert root.children[0].children[2].tag == "green"  # c.png

    def test_min_size_prunes_whole_subtrees(self):
        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter(">2500"), root.n_files)
        assert sorted(n.entry.size for n in _view_leaves(result.root)) == [3000, 3000]
        names = {n.entry.name for n in _iter_view_nodes(result.root) if n.entry.is_dir}
        assert "Empty" not in names, "目录自身小于下界 → 整棵子树剪掉"


# ===========================================================================
# 5. filetypes / tagging / report
# ===========================================================================


class TestFileTypes:
    @pytest.mark.parametrize(
        "ext,category",
        [
            ("mp4", "video"),
            ("mp3", "audio"),
            ("jpg", "image"),
            ("docx", "document"),
            ("zip", "archive"),
            ("exe", "executable"),
            ("sys", "system"),
            ("py", "code"),
            ("ttf", "font"),
            ("db", "database"),
            ("iso", "disk_image"),
            ("xyz_unknown", "other"),
            ("", "other"),
        ],
    )
    def test_category_of(self, ext, category):
        assert filetypes.category_of(ext) == category

    def test_normalizes_case_and_leading_dot(self):
        assert filetypes.category_of("MP4") == "video"
        assert filetypes.category_of(".jpg") == "image"
        assert filetypes.color_of("MP4") == filetypes.color_of("mp4")

    def test_every_category_has_label_and_color(self):
        for category in filetypes.CATEGORY_ORDER:
            assert filetypes.CATEGORY_LABELS.get(category), f"{category} 缺中文名"
            color = filetypes.color_of_category(category)
            assert color.startswith("#") and color.upper() != "#000000", category
        assert set(filetypes.CATEGORY_ORDER) == set(filetypes.CATEGORY_LABELS)

    @pytest.mark.parametrize("ext,category", [("jpg", "image"), ("mp4", "video"), ("xyz", "other")])
    def test_color_of_extension_matches_category_color(self, ext, category):
        assert filetypes.color_of(ext) == filetypes.color_of_category(category)

    def test_palette_is_registered_in_tokens(self):
        """配色必须走设计系统的 token，不能散落字面量。"""
        for category in filetypes.CATEGORY_ORDER:
            assert category in tokens.FILE_TYPE_COLORS, f"{category} 未登记进 lib/ui/tokens.py"
            assert tokens.FILE_TYPE_COLORS[category] == filetypes.color_of_category(category)

    def test_legend_rows(self):
        rows = filetypes.legend()
        assert [row[0] for row in rows] == filetypes.CATEGORY_ORDER
        assert all(len(row) == 3 for row in rows)

    def test_sub_extensions_keeps_declaration_order(self):
        """代表扩展名要按常见程度排，不能按字母序（否则 image 会以 ai/arw 打头）。"""
        exts = filetypes.sub_extensions("image")
        assert exts[0] == "jpg"
        assert "png" in exts
        assert filetypes.sub_extensions("image", limit=3) == exts[:3]
        assert filetypes.sub_extensions("nope") == []


class TestTagging:
    def test_color_lookup(self):
        assert tagging.color_of(None) is None
        assert tagging.color_of("red") == tokens.DANGER
        assert tagging.color_of("green") == tokens.SUCCESS

    def test_apply_and_toggle(self):
        entry = sns_model.Entry("x.txt", 1)
        tagging.apply_tag(entry, "red")
        assert entry.tag == "red"
        tagging.toggle_tag(entry, "red")  # 同色再按一次 = 取消
        assert entry.tag is None
        tagging.toggle_tag(entry, "blue")
        assert entry.tag == "blue"
        tagging.apply_tag(entry, None)
        assert entry.tag is None

    def test_count_and_clear_tags(self):
        root = sns_model.Entry("C:\\", 0, is_dir=True)
        for index, tag in enumerate(("red", "red", "blue", None, "yellow")):
            child = sns_model.Entry(f"f{index}", 1, is_dir=False)
            child.tag = tag
            child.parent = root
            assert root.children is not None
            root.children.append(child)

        counts = tagging.count_tags(root)
        assert counts == {"red": 2, "yellow": 1, "green": 0, "blue": 1}
        assert tagging.clear_tags(root) == 4
        assert set(tagging.count_tags(root).values()) == {0}

    def test_label_and_hotkey(self):
        assert tagging.label_of("red") == "红"
        assert tagging.label_of(None) == "无标记"
        assert tagging.hotkey_of("red") == "Ctrl+1"
        assert tagging.hotkey_of("blue") == "Ctrl+4"
        assert tagging.hotkey_of("nope") == ""
        assert set(tagging.TAG_HOTKEY_ORDER) == set(tagging.TAG_COLORS)


class TestReport:
    def _snapshot(self, tmp_path) -> sns_model.Snapshot:
        path = tmp_path / "sample.sns"
        path.write_bytes(bytes(build(SAMPLE_TREE)))
        return sns_model.parse_file(path)

    def test_text_report_mentions_volume_and_focus(self, tmp_path):
        snap = self._snapshot(tmp_path)
        text = report.build_text_report(snap, top=5)
        assert "C:" in text
        assert "Users" in text
        assert sns_model.human_size(snap.used_bytes) in text
        assert "季度报告.docx" in text, "全树最大文件一段应列出样例里最大的 docx"

        scoped = report.build_text_report(snap, focus=snap.root.children[1], top=5)
        assert "C:\\Users" in scoped

    def test_csv_export_rows_and_header(self, tmp_path):
        snap = self._snapshot(tmp_path)
        out = tmp_path / "out.csv"
        written = report.write_csv(snap.root, out, total=snap.n_files)
        # 卷自身 + 所有目录 + 所有文件（表头行不计入返回值）
        assert written == 1 + snap.n_files + snap.n_dirs

        body = out.read_text(encoding="utf-8-sig")
        lines = body.splitlines()
        assert "路径" in lines[0]
        assert "季度报告.docx" in body
        assert len(lines) == written + 1, "表头行 + 数据行"

    def test_csv_is_excel_friendly(self, tmp_path):
        """utf-8-sig 是刻意的：没有 BOM 时 Excel 双击打开中文表头会变问号。"""
        snap = self._snapshot(tmp_path)
        out = tmp_path / "bom.csv"
        report.write_csv(snap.root, out, total=snap.n_files)
        assert out.read_bytes()[:3] == b"\xef\xbb\xbf"

    def test_write_text_report_writes_bom_file(self, tmp_path):
        target = tmp_path / "report.txt"
        report.write_text_report(target, "内容")
        assert target.read_bytes() == b"\xef\xbb\xbf" + "内容".encode("utf-8")

    def test_type_distribution(self, tmp_path):
        snap = self._snapshot(tmp_path)
        distribution = report.type_distribution(snap.root)
        by_category = {row[0]: row for row in distribution}
        assert set(by_category) == {"audio", "document", "system", "other"}
        assert by_category["audio"][1] == _FILE_MP3  # (category, bytes, count)
        assert by_category["audio"][2] == 1
        assert by_category["document"][2] == 2  # docx + txt
        sizes = [row[1] for row in distribution]
        assert sizes == sorted(sizes, reverse=True)

    def test_largest_files_and_dirs(self, tmp_path):
        snap = self._snapshot(tmp_path)
        files = report.largest_files(snap.root, limit=2)
        assert [path.rsplit("\\", 1)[-1] for _, path in files] == ["pagefile.sys", "音乐.mp3"]

        dirs = report.largest_dirs(snap.root, limit=2)
        assert [path for _, path in dirs] == ["C:\\Users", "C:\\Users\\admin"]

    def test_category_column(self):
        assert report.category_column(sns_model.Entry("d", 0, is_dir=True)) == "目录"
        assert report.category_column(sns_model.Entry("a.jpg", 1)) == filetypes.label_of_category("image")

    def test_grouped_csv(self, tmp_path):
        snap = self._snapshot(tmp_path)
        out = tmp_path / "top.csv"
        written = report.write_grouped_csv(report.largest_files(snap.root, limit=3), out)
        assert written == 3
        assert "完整路径" in out.read_text(encoding="utf-8-sig")


def test_report_type_distribution_is_json_safe():
    """类型分布要跨线程经 Qt 信号传递，必须是纯内建类型。"""
    snap = parse_sample()
    distribution = report.type_distribution(snap.root)
    for row in distribution:
        assert isinstance(row, tuple) and len(row) == 3
        assert isinstance(row[0], str) and isinstance(row[1], int) and isinstance(row[2], int)
    json.dumps([{"k": row[0], "n": row[1], "b": row[2]} for row in distribution])


# ===========================================================================
# 6. DetailsPanel —— 「当前视图口径」回归
# ===========================================================================


class TestDetailsPanelViewScale:
    """详情面板的大小/占比必须用视图口径（`node.size`）。

    纯逻辑测试抓不到这类问题：`ViewNode.size` 和 `Entry.size` 都叫 `size`，
    面板里挑错哪一个不会报错，只会静默显示荒谬的数字。实测过滤后曾出现
    「占当前视图 54505.5%」—— 面板拿全量的 entry.size 去除过滤后的 focus_total。
    这里直接断言渲染出来的文本，防止再次挑错。
    """

    @staticmethod
    def _viewer():
        """按需导入主窗口模块（依赖 Qt widgets，重）。

        同样受运行时 sys.path 注入影响，pyright 看不到，故关掉该行的 missingImports。
        """
        import viewer  # pyright: ignore[reportMissingImports]

        return viewer

    @staticmethod
    def _rows(panel) -> list:
        return [
            [panel._children.topLevelItem(i).text(c) for c in range(4)]
            for i in range(panel._children.topLevelItemCount())
        ]

    def test_unfiltered_view_uses_full_size_without_extra_note(self, qapp):
        viewer = self._viewer()

        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter(""), root.n_files)
        panel = viewer.DetailsPanel()
        panel.show_entry(result.root, result.root.size, result.root.children)

        facts = panel._facts_label.text()
        assert sns_model.human_size(root.size) in facts
        assert "全量" not in facts, "无过滤时两个口径相同，不该多出一行"
        rows = self._rows(panel)
        assert rows and rows[0][1] == sns_model.human_size(root.children[0].size)

    def test_filtered_view_uses_view_size_and_shows_full_size_as_note(self, qapp):
        viewer = self._viewer()

        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter("*.jpg"), root.n_files)
        assert result.root is not None
        assert result.root.size == 3_000 < root.size  # 过滤后口径确实变小了

        panel = viewer.DetailsPanel()
        panel.show_entry(result.root, result.root.size, result.root.children)

        facts = panel._facts_label.text()
        assert sns_model.human_size(result.root.size) in facts
        assert "100.0%" in facts, "当前视图根节点占自己 100%"
        assert "全量" in facts and sns_model.human_size(root.size) in facts

    def test_filtered_child_percentages_sum_to_about_100(self, qapp):
        viewer = self._viewer()

        root = _filter_sample()
        result = filtering.build_view(root, filtering.Filter(">1"), root.n_files)
        panel = viewer.DetailsPanel()
        panel.show_entry(result.root, result.root.size, result.root.children)

        rows = self._rows(panel)
        assert rows
        total = sum(float(row[2].rstrip("%")) for row in rows)
        assert total == pytest.approx(100.0, abs=0.5), (
            f"各行占比之和应为 100%，实际 {total}（分母挑错了就会偏）"
        )
        # 每行的大小也必须是视图口径，不能是 Entry 的全量大小
        by_name = {row[0]: row for row in rows}
        assert by_name["Docs"][1] == sns_model.human_size(1_000 + 2_000 + 3_000)

    def test_clearing_selection_resets_panel(self, qapp):
        viewer = self._viewer()

        panel = viewer.DetailsPanel()
        panel.show_entry(None, 1, [])
        assert panel._facts_label.text() == ""
        assert panel._children.topLevelItemCount() == 0
