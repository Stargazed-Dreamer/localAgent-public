"""Ticket 30 单测：C2 vl_helper + log（VL 协议辅助）。

测试覆盖：
- select_vl_candidates：各 focus 模式（all/key/anomaly/automatable/trimmed）
- select_vl_candidates：去重（同帧路径合并 reason）
- select_vl_candidates：package_path 提供时解析为绝对路径
- select_vl_candidates：非法 focus 抛 ValueError
- build_vl_question：各块类型模板（mouse_click/keyboard_input/focus_change/chapter/screenshot）
- build_vl_question：stt_transcript 返回空
- build_vl_question：context 追加自定义问题
- log_consumption：JSONL 追加写入
- read_consumption_log：解析为 list
- read_consumption_log：文件不存在返回空 list
- read_consumption_log：跳过损坏行
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from workspace.recorder.consumer.log import (
    CONSUMPTION_LOG_FILENAME,
    log_consumption,
    read_consumption_log,
)
from workspace.recorder.consumer.types import VLCandidate
from workspace.recorder.consumer.vl_helper import (
    build_vl_question,
    select_vl_candidates,
)

# ============================ 测试数据构造 ============================


def _make_block(
    block_id: str,
    btype: str,
    timestamp: float,
    category: str = "operation",
    primary: dict | None = None,
    supplements: dict | None = None,
    status: dict | None = None,
) -> dict:
    """构造一个块 dict。"""
    return {
        "id": block_id,
        "category": category,
        "type": btype,
        "timestamp": timestamp,
        "duration": 0.0,
        "primary": primary or {},
        "supplements": supplements or {},
        "status": status or {
            "marked_key": False,
            "marked_anomaly": False,
            "marked_automatable": False,
            "is_trimmed": False,
        },
    }


def _make_merged_view(blocks: list[dict]) -> dict:
    """构造一个 merged_view dict。"""
    return {
        "finalized": False,
        "recording_id": "test_rec",
        "duration_seconds": 100.0,
        "block_count": len(blocks),
        "tracks": ["operation", "image", "chapter"],
        "blocks": blocks,
    }


def _screenshot_block(
    block_id: str,
    timestamp: float,
    frame_path: str,
) -> dict:
    """构造一个 image/screenshot 块。"""
    return _make_block(
        block_id,
        "screenshot",
        timestamp,
        category="image",
        primary={"frame_path": frame_path, "retain_reason": "timer_unique"},
    )


def _click_block(
    block_id: str,
    timestamp: float,
    before_frame: str | None = None,
    after_frame: str | None = None,
    marked_key: bool = False,
    marked_anomaly: bool = False,
    marked_automatable: bool = False,
    is_trimmed: bool = False,
) -> dict:
    """构造一个 mouse_click 块。"""
    supplements: dict = {
        "before_frame": before_frame,
        "after_frame": after_frame,
        "uia_snapshot": None,
        "linked_text": [],
    }
    status = {
        "marked_key": marked_key,
        "marked_anomaly": marked_anomaly,
        "marked_automatable": marked_automatable,
        "is_trimmed": is_trimmed,
    }
    return _make_block(
        block_id,
        "mouse_click",
        timestamp,
        primary={"x": 100, "y": 200, "button": "left", "clicks": 1},
        supplements=supplements,
        status=status,
    )


# ============================ select_vl_candidates：focus 模式 ============================


def test_select_vl_candidates_focus_all_screenshot_blocks() -> None:
    """focus=all 时，所有 image/screenshot 块的 frame_path 加入候选。"""
    view = _make_merged_view([
        _screenshot_block("f001", 1.0, "frames/frame_00000001_000000.png"),
        _screenshot_block("f002", 5.0, "frames/frame_00000005_000000.png"),
    ])
    candidates = select_vl_candidates(view, focus="all")
    assert len(candidates) == 2
    assert candidates[0].block_id == "f001"
    assert candidates[0].reason == "screenshot_frame"
    assert candidates[0].frame_path == "frames/frame_00000001_000000.png"
    assert candidates[1].block_id == "f002"


def test_select_vl_candidates_focus_all_includes_marked_blocks() -> None:
    """focus=all 时，marked_key/marked_anomaly 块的关联帧也加入。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            after_frame="frames/after_001.png",
            marked_key=True,
        ),
        _click_block(
            "b002", 5.0,
            before_frame="frames/before_002.png",
            marked_anomaly=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="all")
    # b001: before+after=2 帧；b002: before=1 帧，共 3 帧
    assert len(candidates) == 3
    reasons = {c.reason for c in candidates}
    assert "marked_key" in reasons
    assert "marked_anomaly" in reasons


def test_select_vl_candidates_focus_key_only() -> None:
    """focus=key 时，仅 marked_key 块的关联帧。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            after_frame="frames/after_001.png",
            marked_key=True,
        ),
        _click_block(
            "b002", 5.0,
            before_frame="frames/before_002.png",
            marked_anomaly=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="key")
    # 只 b001 的 before+after=2 帧
    assert len(candidates) == 2
    for c in candidates:
        assert c.reason == "marked_key"
        assert c.block_id == "b001"


def test_select_vl_candidates_focus_anomaly_only() -> None:
    """focus=anomaly 时，仅 marked_anomaly 块的关联帧。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            marked_key=True,
        ),
        _click_block(
            "b002", 5.0,
            before_frame="frames/before_002.png",
            after_frame="frames/after_002.png",
            marked_anomaly=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="anomaly")
    # 只 b002 的 before+after=2 帧
    assert len(candidates) == 2
    for c in candidates:
        assert c.reason == "marked_anomaly"
        assert c.block_id == "b002"


def test_select_vl_candidates_focus_automatable_only() -> None:
    """focus=automatable 时，仅 marked_automatable 块的关联帧。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            marked_automatable=True,
        ),
        _click_block(
            "b002", 5.0,
            before_frame="frames/before_002.png",
            marked_key=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="automatable")
    # 只 b001 的 before=1 帧
    assert len(candidates) == 1
    assert candidates[0].reason == "marked_automatable"
    assert candidates[0].block_id == "b001"


def test_select_vl_candidates_focus_trimmed_picks_trimmed_blocks() -> None:
    """focus=trimmed 时，若 merged_view 含 is_trimmed 块则选中（测函数逻辑）。

    注：生产环境 merged_view 会过滤 trimmed 块，所以实际返回空；
    这里测函数本身能正确识别 is_trimmed 标记。
    """
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            is_trimmed=True,
        ),
        _click_block(
            "b002", 5.0,
            before_frame="frames/before_002.png",
            marked_key=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="trimmed")
    # b001 is_trimmed=True，被选中；b002 不是 trimmed，不选
    assert len(candidates) == 1
    assert candidates[0].reason == "trimmed"
    assert candidates[0].block_id == "b001"


def test_select_vl_candidates_focus_trimmed_empty_when_no_trimmed() -> None:
    """focus=trimmed 时，merged_view 无 trimmed 块则返回空（生产环境典型场景）。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            marked_key=True,
        ),
    ])
    candidates = select_vl_candidates(view, focus="trimmed")
    assert candidates == []


# ============================ select_vl_candidates：去重 ============================


def test_select_vl_candidates_dedup_same_frame_merges_reason() -> None:
    """同帧路径去重，reason 合并（用 + 连接）。"""
    # b001 marked_key + b002 marked_anomaly，before_frame 是同一张图
    same_frame = "frames/shared_001.png"
    view = _make_merged_view([
        _click_block("b001", 1.0, before_frame=same_frame, marked_key=True),
        _click_block("b002", 2.0, before_frame=same_frame, marked_anomaly=True),
    ])
    candidates = select_vl_candidates(view, focus="all")
    # 同一帧只出现一次
    assert len(candidates) == 1
    c = candidates[0]
    # reason 合并（sorted 后用 + 连接）
    assert "+" in c.reason
    assert "marked_anomaly" in c.reason
    assert "marked_key" in c.reason


def test_select_vl_candidates_dedup_idempotent_reason() -> None:
    """同帧多次同 reason 只算一次。"""
    same_frame = "frames/shared_002.png"
    view = _make_merged_view([
        _click_block("b001", 1.0, before_frame=same_frame, marked_key=True),
        _click_block("b002", 2.0, before_frame=same_frame, marked_key=True),
    ])
    candidates = select_vl_candidates(view, focus="all")
    assert len(candidates) == 1
    # 只有一个 marked_key，不会出现 marked_key+marked_key
    assert candidates[0].reason == "marked_key"


# ============================ select_vl_candidates：package_path 绝对路径 ============================


def test_select_vl_candidates_package_path_resolves_absolute(tmp_path: Path) -> None:
    """提供 package_path 时，frame_path 解析为绝对路径。"""
    view = _make_merged_view([
        _screenshot_block("f001", 1.0, "frames/frame_00000001_000000.png"),
    ])
    candidates = select_vl_candidates(view, focus="all", package_path=tmp_path)
    assert len(candidates) == 1
    expected = str(tmp_path / "frames" / "frame_00000001_000000.png")
    assert candidates[0].frame_path == expected


def test_select_vl_candidates_no_package_path_keeps_relative() -> None:
    """不提供 package_path 时，frame_path 保持相对路径。"""
    view = _make_merged_view([
        _screenshot_block("f001", 1.0, "frames/frame_00000001_000000.png"),
    ])
    candidates = select_vl_candidates(view, focus="all")
    assert len(candidates) == 1
    assert candidates[0].frame_path == "frames/frame_00000001_000000.png"


# ============================ select_vl_candidates：排序与边界 ============================


def test_select_vl_candidates_sorted_by_timestamp() -> None:
    """候选按时间戳升序排序。"""
    view = _make_merged_view([
        _screenshot_block("f003", 10.0, "frames/f3.png"),
        _screenshot_block("f001", 1.0, "frames/f1.png"),
        _screenshot_block("f002", 5.0, "frames/f2.png"),
    ])
    candidates = select_vl_candidates(view, focus="all")
    assert [c.block_id for c in candidates] == ["f001", "f002", "f003"]


def test_select_vl_candidates_empty_view() -> None:
    """空 merged_view 返回空列表。"""
    view = _make_merged_view([])
    candidates = select_vl_candidates(view, focus="all")
    assert candidates == []


def test_select_vl_candidates_invalid_focus_raises() -> None:
    """非法 focus 模式抛 ValueError。"""
    view = _make_merged_view([])
    with pytest.raises(ValueError, match="无效的 focus 模式"):
        select_vl_candidates(view, focus="invalid")


def test_select_vl_candidates_skips_non_frames_paths() -> None:
    """supplements 值不是 frames/ 开头的字符串时跳过（如 null 或 uia_snapshots/）。"""
    block = _click_block("b001", 1.0)
    block["supplements"]["before_frame"] = None  # type: ignore[assignment]
    block["supplements"]["after_frame"] = "uia_snapshots/snap.json"  # 不是 frames/
    block["status"]["marked_key"] = True
    view = _make_merged_view([block])
    candidates = select_vl_candidates(view, focus="key")
    # 无 frames/ 路径，应返回空
    assert candidates == []


def test_vl_candidate_dataclass_fields() -> None:
    """VLCandidate dataclass 字段完整。"""
    c = VLCandidate(
        block_id="b001",
        frame_path="/abs/frames/x.png",
        timestamp=1.5,
        reason="marked_key",
        suggested_question="question?",
    )
    assert c.block_id == "b001"
    assert c.frame_path == "/abs/frames/x.png"
    assert c.timestamp == 1.5
    assert c.reason == "marked_key"
    assert c.suggested_question == "question?"


# ============================ build_vl_question：各块类型模板 ============================


def test_build_vl_question_mouse_click() -> None:
    """mouse_click 块的 question 含坐标。"""
    block = _click_block("b001", 1.0)
    q = build_vl_question(block)
    assert "点击位置 (100, 200)" in q
    assert "可点击元素" in q
    assert "视觉变化" in q


def test_build_vl_question_keyboard_input() -> None:
    """keyboard_input 块的 question 含输入框。"""
    block = _make_block(
        "b001", "keyboard_input", 1.0,
        primary={"text": "hello", "physical_keys": ["h", "e", "l", "l", "o"]},
    )
    q = build_vl_question(block)
    assert "输入框" in q
    assert "输入了什么内容" in q


def test_build_vl_question_focus_change() -> None:
    """focus_change 块的 question 含窗口标题。"""
    block = _make_block(
        "b001", "focus_change", 1.0,
        primary={"hwnd": 123, "title": "记事本", "pid": 456},
    )
    q = build_vl_question(block)
    assert "窗口/界面的标题" in q
    assert "功能区域" in q


def test_build_vl_question_chapter() -> None:
    """chapter 块的 question 含章节操作。"""
    block = _make_block(
        "c001", "chapter", 0.0,
        category="text",
        primary={"name": "登录"},
    )
    q = build_vl_question(block)
    assert "章节" in q
    assert "操作" in q


def test_build_vl_question_screenshot() -> None:
    """screenshot 块的通用 question。"""
    block = _screenshot_block("f001", 1.0, "frames/x.png")
    q = build_vl_question(block)
    assert "截图" in q
    assert "界面元素" in q


def test_build_vl_question_stt_transcript_returns_empty() -> None:
    """stt_transcript 块返回空字符串（无需 VL）。"""
    block = _make_block(
        "t001", "stt_transcript", 1.0,
        category="text",
        primary={"text": "你好", "source": "stt"},
    )
    q = build_vl_question(block)
    assert q == ""


def test_build_vl_question_unknown_type_uses_default() -> None:
    """未知块类型用默认 question。"""
    block = _make_block("x001", "unknown_type", 1.0)
    q = build_vl_question(block)
    assert "值得注意" in q


def test_build_vl_question_with_context() -> None:
    """context 参数追加自定义问题。"""
    block = _click_block("b001", 1.0)
    q = build_vl_question(block, context="这个按钮的颜色是什么？")
    assert "追加问题：这个按钮的颜色是什么？" in q
    # 原模板内容仍存在
    assert "可点击元素" in q


def test_build_vl_question_stt_transcript_ignores_context() -> None:
    """stt_transcript 块即使传 context 也返回空。"""
    block = _make_block(
        "t001", "stt_transcript", 1.0,
        category="text",
        primary={"text": "你好", "source": "stt"},
    )
    q = build_vl_question(block, context="自定义问题")
    assert q == ""


def test_build_vl_question_empty_block() -> None:
    """空 block dict 用默认 question，不抛异常。"""
    q = build_vl_question({})
    assert "值得注意" in q


# ============================ log_consumption：JSONL 追加写入 ============================


def test_log_consumption_appends_jsonl(tmp_path: Path) -> None:
    """log_consumption 追加写入 JSONL 格式。"""
    log_consumption(tmp_path, "vl_call", {"block_id": "b001", "question": "q1"})
    log_consumption(tmp_path, "read_view", {})

    log_path = tmp_path / CONSUMPTION_LOG_FILENAME
    assert log_path.exists()

    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

    entry1 = json.loads(lines[0])
    assert entry1["action"] == "vl_call"
    assert entry1["details"]["block_id"] == "b001"
    assert entry1["details"]["question"] == "q1"
    assert "timestamp" in entry1

    entry2 = json.loads(lines[1])
    assert entry2["action"] == "read_view"
    assert entry2["details"] == {}


def test_log_consumption_none_details_becomes_empty_dict(tmp_path: Path) -> None:
    """details=None 时写入空 dict。"""
    log_consumption(tmp_path, "list_recordings", None)

    log_path = tmp_path / CONSUMPTION_LOG_FILENAME
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["details"] == {}


def test_log_consumption_nonexistent_dir_is_noop(tmp_path: Path) -> None:
    """录制包目录不存在时静默 no-op，不抛异常。"""
    nonexistent = tmp_path / "does_not_exist"
    log_consumption(nonexistent, "vl_call", {"q": "test"})
    # 不抛异常即通过，且不创建任何文件
    assert not nonexistent.exists()


# ============================ read_consumption_log：解析 ============================


def test_read_consumption_log_returns_entries_in_order(tmp_path: Path) -> None:
    """read_consumption_log 按写入顺序返回 list。"""
    log_consumption(tmp_path, "vl_call", {"block_id": "b001"})
    log_consumption(tmp_path, "read_view", {})
    log_consumption(tmp_path, "vl_call", {"block_id": "b002"})

    entries = read_consumption_log(tmp_path)
    assert len(entries) == 3
    assert entries[0]["details"]["block_id"] == "b001"
    assert entries[1]["action"] == "read_view"
    assert entries[2]["details"]["block_id"] == "b002"


def test_read_consumption_log_nonexistent_returns_empty(tmp_path: Path) -> None:
    """文件不存在时返回空 list。"""
    entries = read_consumption_log(tmp_path)
    assert entries == []


def test_read_consumption_log_skips_corrupted_lines(tmp_path: Path) -> None:
    """损坏行被跳过，不抛异常。"""
    log_path = tmp_path / CONSUMPTION_LOG_FILENAME
    # 写入：1 条正常 + 1 条损坏 + 1 条正常
    log_path.write_text(
        json.dumps({"action": "ok1", "details": {}, "timestamp": "t1"}) + "\n"
        + "this is not json\n"
        + json.dumps({"action": "ok2", "details": {}, "timestamp": "t2"}) + "\n",
        encoding="utf-8",
    )

    entries = read_consumption_log(tmp_path)
    assert len(entries) == 2
    assert entries[0]["action"] == "ok1"
    assert entries[1]["action"] == "ok2"


def test_read_consumption_log_skips_empty_lines(tmp_path: Path) -> None:
    """空行被跳过。"""
    log_path = tmp_path / CONSUMPTION_LOG_FILENAME
    log_path.write_text(
        "\n"
        + json.dumps({"action": "ok", "details": {}, "timestamp": "t"}) + "\n"
        + "\n",
        encoding="utf-8",
    )

    entries = read_consumption_log(tmp_path)
    assert len(entries) == 1
    assert entries[0]["action"] == "ok"


# ============================ 端到端：vl_helper + log 联动 ============================


def test_vl_helper_and_log_e2e(tmp_path: Path) -> None:
    """端到端：选 VL 候选 + 记录消费日志。"""
    view = _make_merged_view([
        _click_block(
            "b001", 1.0,
            before_frame="frames/before_001.png",
            after_frame="frames/after_001.png",
            marked_anomaly=True,
        ),
        _screenshot_block("f001", 2.0, "frames/frame_002.png"),
    ])

    # 选 anomaly 候选（package_path=tmp_path 解析绝对路径）
    candidates = select_vl_candidates(
        view, focus="anomaly", package_path=tmp_path,
    )
    assert len(candidates) == 2  # before + after

    # 模拟 agent 调 VL 并记录
    for cand in candidates:
        log_consumption(tmp_path, "vl_call", {
            "block_id": cand.block_id,
            "frame": cand.frame_path,
            "question": cand.suggested_question,
        })

    # 读消费日志验证
    entries = read_consumption_log(tmp_path)
    assert len(entries) == 2
    for entry in entries:
        assert entry["action"] == "vl_call"
        assert entry["details"]["block_id"] == "b001"
        assert tmp_path.name in entry["details"]["frame"]  # 绝对路径
