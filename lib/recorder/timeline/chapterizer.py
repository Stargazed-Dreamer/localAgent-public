"""T2 章节切分器（spec-l2.md 第三节 / 02-block-design.md）

扫描操作块序列，按 focus_change + idle > 阈值 切分章节（D042）。

切分规则：
1. focus_change 块 → 切章节（章节名 = primary.title，如"TRAE Work CN"）
2. idle 块 duration > idle_threshold → 切章节（章节名 = "idle {duration}s"）
3. 第一个章节块在 timestamp=0.0 插入（章节名 = 第一个 focus_change 的标题，或"开始"）
4. 章节名去重：连续同名章节只保留第一个

章节块格式（c001/c002...，category=text, type=chapter）：
    {
        "id": "c001",
        "category": "text",
        "type": "chapter",
        "timestamp": 0.0,
        "duration": 0.0,
        "primary": {"name": "TRAE Work CN"},
        "supplements": {},
        "status": {"marked_key": false, ...}
    }

注：章节块只基于操作块序列生成（focus_change 和 idle 都是操作块），
不基于 image/text 块。章节块的 timestamp = 切分点的时间戳。
"""

# idle 块默认阈值（秒）：duration 超过此值才切章节
DEFAULT_IDLE_THRESHOLD = 2.0


def generate_chapters(
    blocks: list[dict],
    idle_threshold: float = DEFAULT_IDLE_THRESHOLD,
) -> list[dict]:
    """扫描操作块序列，生成章节块列表。

    Args:
        blocks: 操作块列表（已按 timestamp 升序），含 focus_change 和 idle 块
        idle_threshold: idle 块切章节的持续时间阈值（秒）

    Returns:
        章节块列表（c001/c002...，category=text, type=chapter）
    """
    # 无操作块时不生成章节（空录制包/降级场景）
    if not blocks:
        return []

    # ---- 1. 收集所有切分点（timestamp + 章节名）----
    raw_chapters: list[dict] = []

    # 第一个章节块 timestamp=0.0
    first_focus = _find_first_focus_change(blocks)
    first_name = (
        first_focus["primary"].get("title", "") if first_focus else "开始"
    )
    raw_chapters.append({"timestamp": 0.0, "name": first_name})

    # 扫描操作块找切分点
    for b in blocks:
        btype = b.get("type", "")
        if btype == "focus_change":
            title = b.get("primary", {}).get("title", "")
            ts = float(b.get("timestamp", 0.0))
            raw_chapters.append({"timestamp": ts, "name": title})
        elif btype == "idle":
            duration = float(b.get("duration", 0.0))
            if duration > idle_threshold:
                ts = float(b.get("timestamp", 0.0))
                raw_chapters.append(
                    {"timestamp": ts, "name": f"idle {duration:.1f}s"}
                )

    # ---- 2. 章节名去重：连续同名只保留第一个 ----
    deduped: list[dict] = []
    for ch in raw_chapters:
        if not deduped or deduped[-1]["name"] != ch["name"]:
            deduped.append(ch)

    # ---- 3. 转为 chapter 块（c001/c002...）----
    chapter_blocks: list[dict] = []
    for seq, ch in enumerate(deduped, start=1):
        chapter_blocks.append(
            _make_chapter_block(seq, ch["timestamp"], ch["name"])
        )
    return chapter_blocks


def _find_first_focus_change(blocks: list[dict]) -> dict | None:
    """在操作块序列中找第一个 focus_change 块。

    Args:
        blocks: 操作块列表

    Returns:
        第一个 focus_change 块 dict，没有时返回 None
    """
    for b in blocks:
        if b.get("type") == "focus_change":
            return b
    return None


def _make_chapter_block(seq: int, timestamp: float, name: str) -> dict:
    """构造章节块。

    Args:
        seq: 块序号（1-based）
        timestamp: 章节起始时间戳
        name: 章节名

    Returns:
        chapter 块 dict（id=c001..., category=text, type=chapter）
    """
    return {
        "id": f"c{seq:03d}",
        "category": "text",
        "type": "chapter",
        "timestamp": timestamp,
        "duration": 0.0,
        "primary": {"name": name},
        "supplements": {},
        "status": {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }
