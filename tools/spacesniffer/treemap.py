"""Squarified treemap 布局引擎（Bruls / Huizing / van Wijk, 2000）。

本模块只做「几何」：把一棵 Entry 树铺成一组矩形，产出的是一份**扁平 Tile 列表**，
供绘制与命中测试消费。刻意不依赖 QWidget/QPainter —— 只用 `QRectF` 这种纯粹的
值类型，因此布局可以放到工作线程里跑，且单元测试不需要 QApplication。

三处关键设计，共同决定了几十万节点规模下能不能跑得动：

1. **布局只在视图变化时重算**（缩放 / 过滤 / 改窗口大小），结果缓存在
   `TreemapLayout.tiles`；`paintEvent` 只做一次廉价的列表遍历。
2. **面积预剪枝** —— 单看某一块的面积占比就知道它在屏幕上不足 `min_side` 像素，
   于是直接不参与 squarify，合并成一个「其他」块。这让一个含 5000 个文件的目录
   也只产生十几个瓦片，而不是 5000 个。
3. **增量式 squarify** —— 逐项累加 `sum/max/min`，避免朴素写法里反复对整行求和
   导致的 O(n²) 退化（实测含数千子项的目录是主要耗时来源）。

一棵 85 万节点的树，在 1400×900 视口下典型产出 3k~8k 个瓦片、耗时几十毫秒。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QRectF

from sns_model import Entry

# --- 几何常量（treemap 自身的几何参数；颜色一律走 tokens） -------------------
HEADER_H = 15.0  # 目录标题条高度
HEADER_MIN_H = 26.0  # 瓦片低于此高度不放标题条（放不下标题还白占地方）
HEADER_MIN_W = 44.0  # 标题条最小宽度（再窄画不出可读文字）
MIN_SIDE = 4.0  # 小于此边长的瓦片不再切分/绘制
LABEL_MIN_W = 34.0  # 显示文字所需的最小宽高
LABEL_MIN_H = 13.0
GAP_LABEL = "未计入（NTFS 元数据 / 未分配）"


class Tile:
    """一个待绘制的矩形。`entry` 为 None 时表示聚合块（过小内容或未分配空间）。

    `children` / `parent` 维持一棵**层级树**，与扁平的 `tiles` 列表并存：
    前者给命中测试用（自顶向下逐层下降，几十次比较就够），后者给绘制用
    （一次顺序遍历）。若只有扁平列表，鼠标每移动一像素都要倒序扫上千个瓦片。
    """

    __slots__ = ("rect", "entry", "depth", "has_header", "label", "children", "parent")

    def __init__(
        self,
        rect: QRectF,
        entry: Optional[Entry],
        depth: int,
        has_header: bool = False,
        label: str = "",
    ) -> None:
        self.rect = rect
        self.entry = entry
        self.depth = depth
        self.has_header = has_header
        self.label = label or (entry.name if entry is not None else "")
        self.children: List["Tile"] = []
        self.parent: Optional["Tile"] = None

    @property
    def is_gap(self) -> bool:
        return self.entry is None

    @property
    def is_dir(self) -> bool:
        """是否为一个「容器」瓦片（目录或聚合块）。文件瓦片没有子瓦片。"""
        return self.entry is None or self.entry.is_dir

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Tile d={self.depth} {self.label!r} "
            f"{self.rect.width():.0f}x{self.rect.height():.0f}>"
        )


class LayoutConfig:
    """布局调参。默认值面向「宽屏 + 鼠标」。"""

    __slots__ = ("min_side", "max_tiles", "max_depth", "show_headers")

    def __init__(
        self,
        min_side: float = MIN_SIDE,
        max_tiles: int = 40_000,
        max_depth: int = 16,
        show_headers: bool = True,
    ) -> None:
        self.min_side = min_side
        self.max_tiles = max_tiles
        # 深度上限是「视觉」而非「数据」限制：1400×900 视口下超过十来层就只剩几像素，
        # 画出来读不到信息。深处的节点仍可通过双击逐层放大访问。
        self.max_depth = max_depth
        self.show_headers = show_headers


class TreemapLayout:
    """一次布局的产物：扁平瓦片列表（绘制用）+ 层级树（命中测试用）。"""

    __slots__ = ("tiles", "roots", "focus", "width", "height", "truncated")

    def __init__(
        self,
        tiles: List[Tile],
        roots: List[Tile],
        focus: Entry,
        width: float,
        height: float,
        truncated: bool = False,
    ) -> None:
        self.tiles = tiles
        self.roots = roots
        self.focus = focus
        self.width = width
        self.height = height
        self.truncated = truncated

    def hit_test(self, x: float, y: float) -> Optional[Tile]:
        """返回覆盖该点的**最深**瓦片。

        自顶向下逐层下降：每一层里至多一个瓦片包含该点，所以比较次数是
        「深度 × 每层兄弟数」（几十到几百次），与瓦片总数无关。
        """
        current: Optional[Tile] = None
        candidates = self.roots
        while candidates:
            found: Optional[Tile] = None
            for tile in candidates:
                r = tile.rect
                if r.left() <= x < r.right() and r.top() <= y < r.bottom():
                    found = tile
                    break
            if found is None:
                break
            current = found
            candidates = found.children
        return current


# ---------------------------------------------------------------------------
# squarified 主算法
# ---------------------------------------------------------------------------


def squarify(
    weights: Sequence[float],
    rect: Tuple[float, float, float, float],
) -> List[Tuple[float, float, float, float]]:
    """把一组权重铺满给定矩形，返回与输入**等长且同序**的矩形列表。

    权重可以是任意正数（直接传字节数即可）：内部按 `矩形面积 / 权重总和` 统一缩放。
    **这个缩放是必需的** —— 教科书算法假定「权重之和 == 矩形面积」，直接拿字节数
    当面积会让条带厚度算成 sum/边长 的错误量级（实测退化成 1px 宽的细条）。

    注意：算法内部必须按权重降序处理（先放大块、小块填缝，这是 squarified 效果
    的来源），但**返回值按输入顺序对齐**，因为调用方要拿它和原始子项列表对位。
    权重 <= 0 的条目返回零矩形，位置保持不变。
    """
    x, y, w, h = rect
    count = len(weights)
    blank = [(x, y, 0.0, 0.0)] * count
    if w <= 0 or h <= 0 or count == 0:
        return blank

    weight_total = sum(v for v in weights if v > 0)
    if weight_total <= 0:
        return blank

    scale = (w * h) / weight_total
    order = sorted(
        (i for i in range(count) if weights[i] > 0),
        key=lambda i: weights[i],
        reverse=True,
    )
    items = [weights[i] * scale for i in order]  # 已降序，可直接当作待消费队列
    produced: List[Tuple[float, float, float, float]] = []

    while items and w > 0 and h > 0:
        side = min(w, h)
        side_sq = side * side

        # --- 贪心组行：增量维护 sum/max/min，避免 O(n²) ---
        # items 已降序，故新加入的项永远是最小值，最大值始终是行首。
        row_total = items[0]
        row_max = items[0]
        row_min = items[0]
        row_len = 1
        row_cost = max(
            side_sq * row_max / (row_total * row_total),
            (row_total * row_total) / (side_sq * row_min),
        )

        while row_len < len(items):
            value = items[row_len]
            cand_total = row_total + value
            cand_cost = max(
                side_sq * row_max / (cand_total * cand_total),
                (cand_total * cand_total) / (side_sq * value),
            )
            if cand_cost > row_cost:
                break  # 再加下去长宽比变差，本行到此为止
            row_total = cand_total
            row_min = value
            row_cost = cand_cost
            row_len += 1

        row = items[:row_len]
        del items[:row_len]

        if row_total <= 0:
            break

        if w >= h:
            # 剩余矩形偏宽 → 行沿左边竖排一列
            strip = row_total / h
            if strip <= 0:
                break
            cursor = y
            for area in row:
                item_h = area / strip
                produced.append((x, cursor, strip, item_h))
                cursor += item_h
            x += strip
            w -= strip
        else:
            # 剩余矩形偏高 → 行沿上边横排一行
            strip = row_total / w
            if strip <= 0:
                break
            cursor = x
            for area in row:
                item_w = area / strip
                produced.append((cursor, y, item_w, strip))
                cursor += item_w
            y += strip
            h -= strip

    # 内部是降序产出的，这里按输入顺序回填
    result = list(blank)
    for position, original_index in enumerate(order):
        if position >= len(produced):
            break
        result[original_index] = produced[position]
    return result


# ---------------------------------------------------------------------------
# 树 → 瓦片
# ---------------------------------------------------------------------------


def build_layout(
    focus: Entry,
    width: float,
    height: float,
    config: Optional[LayoutConfig] = None,
) -> TreemapLayout:
    """把 focus 目录的内容铺满 width×height 的视口。

    视口即代表 focus 自身，focus 不出现在瓦片里 —— 它就是画布。
    """
    cfg = config or LayoutConfig()
    tiles: List[Tile] = []
    roots: List[Tile] = []
    state = _State(cfg)
    if width > 0 and height > 0 and focus.children:
        _layout_children(
            focus, 0.0, 0.0, float(width), float(height), 0, None, tiles, roots, state
        )
    return TreemapLayout(
        tiles, roots, focus, float(width), float(height), state.truncated
    )


class _State:
    __slots__ = ("cfg", "truncated")

    def __init__(self, cfg: LayoutConfig) -> None:
        self.cfg = cfg
        self.truncated = False


def _register(
    tile: Tile,
    parent: Optional[Tile],
    tiles: List[Tile],
    roots: List[Tile],
) -> None:
    tiles.append(tile)
    tile.parent = parent
    if parent is None:
        roots.append(tile)
    else:
        parent.children.append(tile)


def _layout_children(
    node: Entry,
    x: float,
    y: float,
    w: float,
    h: float,
    depth: int,
    parent: Optional[Tile],
    out: List[Tile],
    roots: List[Tile],
    state: _State,
) -> None:
    """在给定矩形内铺开 node 的子项，并对目录子项递归。"""
    cfg = state.cfg
    if w < cfg.min_side or h < cfg.min_side or depth >= cfg.max_depth:
        return
    if len(out) >= cfg.max_tiles:
        state.truncated = True
        return

    children = [c for c in (node.children or []) if c.size > 0]
    if not children:
        return

    total = float(sum(c.size for c in children))
    if total <= 0:
        return

    area = w * h
    scale = area / total
    min_area = cfg.min_side * cfg.min_side

    # 面积预剪枝：屏幕上不足 min_side 像素的子项不参与 squarify，
    # 否则一个含数千文件的目录会白白生成数千个 1px 瓦片。
    kept: List[Entry] = []
    kept_weights: List[float] = []
    dropped_bytes = 0.0
    dropped_count = 0
    for child in children:
        if child.size * scale >= min_area:
            kept.append(child)
            kept_weights.append(float(child.size))
        else:
            dropped_bytes += float(child.size)
            dropped_count += 1

    # 两种「补齐」合并成一个聚合块：
    #   gap     = 目录自身大小 − 子项之和（NTFS 元数据等未计入项）
    #   dropped = 被剪枝的过小内容
    gap_bytes = float(node.size) - total
    filler = max(0.0, gap_bytes) + dropped_bytes

    weights = list(kept_weights)
    filler_index = -1
    if filler > 0:
        filler_index = len(weights)
        weights.append(filler)

    if not weights:
        return

    rects = squarify(weights, (x, y, w, h))

    for index, rect in enumerate(rects):
        if len(out) >= cfg.max_tiles:
            state.truncated = True
            return
        rx, ry, rw, rh = rect
        if rw < cfg.min_side or rh < cfg.min_side:
            continue
        tile_rect = QRectF(rx, ry, rw, rh)

        if index == filler_index:
            _register(
                Tile(tile_rect, None, depth, False, _filler_label(gap_bytes, dropped_count)),
                parent,
                out,
                roots,
            )
            continue

        child = kept[index]
        if not child.is_dir:
            _register(Tile(tile_rect, child, depth, False), parent, out, roots)
            continue

        has_header = cfg.show_headers and rh >= HEADER_MIN_H and rw >= HEADER_MIN_W
        tile = Tile(tile_rect, child, depth, has_header)
        _register(tile, parent, out, roots)
        inner_y = ry + HEADER_H if has_header else ry
        inner_h = rh - HEADER_H if has_header else rh
        _layout_children(
            child, rx, inner_y, rw, inner_h, depth + 1, tile, out, roots, state
        )


def _filler_label(gap_bytes: float, dropped_count: int) -> str:
    if dropped_count and gap_bytes > 0:
        return f"其他（{dropped_count} 项过小内容 + 未分配空间）"
    if dropped_count:
        return f"其他（{dropped_count} 项过小内容）"
    return GAP_LABEL
