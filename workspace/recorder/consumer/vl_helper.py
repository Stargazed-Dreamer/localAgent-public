"""C2 vl_helper：select_vl_candidates / build_vl_question（Ticket 30）。

多轮 VL 协议辅助函数：
- select_vl_candidates：从 merged_view 选 VL 候选帧（focus 模式过滤 + 去重）
- build_vl_question：根据块类型生成 VL question 模板

VLCandidate 是 agent 调 understand_image 时的入参封装。

典型用法（多轮 VL 协议 Round 2）：

    from workspace.recorder.consumer import get_merged_view, select_vl_candidates, build_vl_question

    view = get_merged_view(package_path)
    candidates = select_vl_candidates(view, focus="anomaly", package_path=package_path)
    for cand in candidates:
        # 调 VL 看图
        answer = understand_image(cand.frame_path, cand.suggested_question)
        # 记录消费日志
        log_consumption(package_path, "vl_call", {
            "block_id": cand.block_id,
            "frame": cand.frame_path,
            "question": cand.suggested_question,
        })
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workspace.recorder.consumer.types import VLCandidate

# focus 模式 → status 字段名映射（key/anomaly/automatable/trimmed 四种过滤模式）
_FOCUS_STATUS_MAP: dict[str, str] = {
    "key": "marked_key",
    "anomaly": "marked_anomaly",
    "automatable": "marked_automatable",
    "trimmed": "is_trimmed",
}

# 合法的 focus 模式集合
_VALID_FOCUS_MODES = {"all", "key", "anomaly", "automatable", "trimmed"}


def select_vl_candidates(
    merged_view: dict[str, Any],
    focus: str = "all",
    package_path: Path | str | None = None,
) -> list[VLCandidate]:
    """从 merged_view 选 VL 候选帧。

    Args:
        merged_view: get_merged_view 返回的最终视图（含 blocks 数组）
        focus: 候选选择模式
            - "all": 所有 image/screenshot 块 + 所有 marked_key/marked_anomaly 块的关联帧
            - "key": 仅 marked_key 块的关联帧（before_frame/after_frame）
            - "anomaly": 仅 marked_anomaly 块的关联帧（agent 诊断 bug 时优先看这些）
            - "automatable": 仅 marked_automatable 块的关联帧（agent 生成自动化脚本时优先看）
            - "trimmed": 仅 is_trimmed 块的关联帧（merged_view 默认排除 trimmed 块，通常返回空）
        package_path: 录制包根目录，提供时 frame_path 解析为绝对路径

    Returns:
        VLCandidate 列表（按时间戳升序），同一帧路径去重 + reason 合并（用 + 连接）

    Raises:
        ValueError: focus 不是合法模式
    """
    if focus not in _VALID_FOCUS_MODES:
        raise ValueError(
            f"无效的 focus 模式: {focus}，合法值: {sorted(_VALID_FOCUS_MODES)}"
        )

    package_root = Path(package_path) if package_path else None
    blocks = merged_view.get("blocks", [])

    # 收集原始候选: (block_id, frame_path, timestamp, reason)
    raw: list[tuple[str, str, float, str]] = []

    for block in blocks:
        block_id = str(block.get("id", ""))
        timestamp = float(block.get("timestamp", 0.0))
        status = block.get("status", {}) or {}
        btype = str(block.get("type", ""))
        category = str(block.get("category", ""))

        # focus="all" 时，image/screenshot 块的 frame_path 直接加入
        if focus == "all" and category == "image" and btype == "screenshot":
            frame_path = str((block.get("primary") or {}).get("frame_path", ""))
            if frame_path:
                raw.append((block_id, frame_path, timestamp, "screenshot_frame"))

        # focus="all" 时，marked_key/marked_anomaly 块的关联帧也加入
        if focus == "all":
            for status_key, reason in (
                ("marked_key", "marked_key"),
                ("marked_anomaly", "marked_anomaly"),
            ):
                if status.get(status_key):
                    raw.extend(
                        _extract_related_frames(block, block_id, timestamp, reason)
                    )

        # focus=key/anomaly/automatable/trimmed 时，按 status 字段过滤
        elif focus in _FOCUS_STATUS_MAP:
            status_key = _FOCUS_STATUS_MAP[focus]
            if status.get(status_key):
                # reason 用完整 status 名（marked_key/marked_anomaly/...），
                # trimmed 块在 merged_view 中通常不存在（merge 时已过滤），
                # 这里保留逻辑以备 merged_view 包含 trimmed 块的情况
                reason = "trimmed" if focus == "trimmed" else status_key
                raw.extend(
                    _extract_related_frames(block, block_id, timestamp, reason)
                )

    # 去重：同一帧路径只出现一次，reason 合并（用 + 连接）
    deduped: dict[str, VLCandidate] = {}

    for block_id, frame_path, timestamp, reason in raw:
        abs_path = (
            str((package_root / frame_path).resolve()) if package_root else frame_path
        )

        if abs_path in deduped:
            existing = deduped[abs_path]
            # 合并 reason（去重 + 排序 + 用 + 连接）
            reasons = set(existing.reason.split("+"))
            reasons.add(reason)
            existing.reason = "+".join(sorted(reasons))
        else:
            block = next((b for b in blocks if b.get("id") == block_id), {})
            deduped[abs_path] = VLCandidate(
                block_id=block_id,
                frame_path=abs_path,
                timestamp=timestamp,
                reason=reason,
                suggested_question=build_vl_question(block),
            )

    # 按时间戳升序排序
    return sorted(deduped.values(), key=lambda c: c.timestamp)


def _extract_related_frames(
    block: dict[str, Any],
    block_id: str,
    timestamp: float,
    reason: str,
) -> list[tuple[str, str, float, str]]:
    """从块的 supplements 提取关联帧（before_frame / after_frame）。

    Args:
        block: 块 dict
        block_id: 块 ID
        timestamp: 块时间戳
        reason: 选择原因（marked_key / marked_anomaly / marked_automatable / trimmed）

    Returns:
        [(block_id, frame_path, timestamp, reason), ...]
        只提取值为 "frames/xxx.png" 的字段
    """
    result: list[tuple[str, str, float, str]] = []
    supplements = block.get("supplements", {}) or {}

    for supp_key in ("before_frame", "after_frame", "linked_frame"):
        value = supplements.get(supp_key)
        if isinstance(value, str) and value.startswith("frames/"):
            result.append((block_id, value, timestamp, reason))

    return result


def build_vl_question(block: dict[str, Any], context: str = "") -> str:
    """根据块类型生成 VL question 模板。

    模板（spec C2）：
    - mouse_click: "这个界面的可点击元素在哪里？点击位置 (x, y) 是什么控件？点击前后的视觉变化是什么？"
    - keyboard_input: "这个界面的输入框在哪里？当前输入了什么内容？"
    - focus_change: "这个窗口/界面的标题是什么？主要功能区域有哪些？"
    - chapter: "这个章节的界面主要在做什么操作？"
    - stt_transcript: 返回空字符串（文本已有，无需 VL）
    - screenshot / 其他: 通用 question

    Args:
        block: 块 dict
        context: 追加的自定义问题，非空时拼接到模板末尾

    Returns:
        VL question 字符串；stt_transcript 块返回空字符串
    """
    btype = str(block.get("type", ""))
    primary = block.get("primary") or {}

    # stt_transcript 块不调 VL（文本已有）
    if btype == "stt_transcript":
        return ""

    templates: dict[str, str] = {
        "mouse_click": (
            "这个界面的可点击元素在哪里？点击位置 ({x}, {y}) 是什么控件？"
            "点击前后的视觉变化是什么？"
        ),
        "keyboard_input": "这个界面的输入框在哪里？当前输入了什么内容？",
        "focus_change": "这个窗口/界面的标题是什么？主要功能区域有哪些？",
        "chapter": "这个章节的界面主要在做什么操作？",
        "screenshot": "这个截图展示了什么内容？主要界面元素有哪些？",
        "idle": "这个时间段界面有什么变化？是否有加载或等待状态？",
        "mouse_scroll": "这个界面的滚动区域在哪里？滚动后的视觉变化是什么？",
    }

    template = templates.get(btype, "这个界面有什么值得注意的内容？")

    # 填充 mouse_click 的坐标
    if btype == "mouse_click":
        x = primary.get("x", "?")
        y = primary.get("y", "?")
        template = template.format(x=x, y=y)

    # 追加 context
    if context:
        template = f"{template}\n\n追加问题：{context}"

    return template
