"""L1 处理层 Ticket 15：P4 事件聚合器 + process_recording_package seam 测试

测试策略（spec-l1.md Testing Decisions）：
- 用真实的录制包目录结构测试（构造 events.jsonl + frames/）
- P4 事件聚合器：构造 events.jsonl（含 mouse_click + keyboard_input + focus_change）
- 验证 P4 产出正确的 blocks.json（类型/时间戳/截图关联）
- idle 块生成（两操作间隔 >2s 产生 idle 块，<2s 不产生）
- 截图关联（操作前后有帧则关联，无帧则 frame_path=None）
- process_recording_package seam：编排 P4（P5/P1 留空跳过）

覆盖 Ticket 15 acceptance criteria：
- [x] P4 事件聚合器：读取 events.jsonl，每个底层事件转为操作块
- [x] P4 块类型：mouse_click / mouse_scroll / mouse_drag / keyboard_input / focus_change
- [x] P4 idle 块生成：间隔 >2s 插入 idle 块
- [x] P4 截图关联：操作前后 200ms/300ms 内最近帧
- [x] P4 复用 L0 聚合结果：不再次聚合 keystroke
- [x] blocks.json 格式符合 02-block-design.md
- [x] 单测：构造 events.jsonl，验证 P4 产出正确
- [x] 单测：idle 块生成
- [x] 单测：截图关联
- [x] 端到端测试：给定录制包目录 → 跑 process_recording_package → 验证 blocks.json
"""

import json
from pathlib import Path

import pytest

from lib.recorder.processor import process_recording_package
from lib.recorder.processor.p4_event_aggregator import (
    aggregate_events,
    run_p4,
)

# ============================ 测试辅助函数 ============================


def _make_event(ts: float, kind: str, payload: dict) -> dict:
    """构造 L0 事件 dict。"""
    return {
        "timestamp": ts,
        "abs_timestamp": 1700000000.0 + ts,
        "kind": kind,
        "payload": payload,
    }


def _make_frame_event(ts: float, frame_path: str) -> dict:
    """构造 screen_frame 事件。"""
    return _make_event(ts, "screen_frame", {
        "frame_path": frame_path,
        "frame_seq": 0,
        "mode": "fullscreen",
    })


def _write_events_jsonl(package_root: Path, events: list[dict]) -> Path:
    """把事件列表写入录制包的 events.jsonl。"""
    package_root.mkdir(parents=True, exist_ok=True)
    events_file = package_root / "events.jsonl"
    lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_file


# ============================ P4 纯函数测试 ============================


class TestAggregateEventsBasic:
    """P4 基本聚合：每个底层事件转为一个操作块。"""

    def test_mouse_click_to_block(self):
        """mouse_click 事件 → operation/mouse_click 块。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 100, "y": 200}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        b = blocks[0]
        assert b["category"] == "operation"
        assert b["type"] == "mouse_click"
        assert b["timestamp"] == 1.0
        assert b["primary"]["x"] == 100
        assert b["primary"]["y"] == 200
        assert b["primary"]["button"] == "left"
        assert b["primary"]["clicks"] == 1
        assert b["id"].startswith("b")

    def test_mouse_scroll_to_block(self):
        """mouse_scroll 事件 → operation/mouse_scroll 块。"""
        events = [
            _make_event(1.0, "mouse_scroll", {"x": 50, "y": 60, "dx": 0, "dy": -3}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "mouse_scroll"
        assert blocks[0]["primary"]["dy"] == -3

    def test_mouse_drag_to_block(self):
        """mouse_drag 事件 → operation/mouse_drag 块。"""
        events = [
            _make_event(1.0, "mouse_drag", {
                "button": "left",
                "start": {"x": 10, "y": 20},
                "end": {"x": 100, "y": 200},
                "track": [{"x": 10, "y": 20}, {"x": 50, "y": 100}],
            }),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "mouse_drag"
        assert blocks[0]["primary"]["start"] == {"x": 10, "y": 20}
        assert blocks[0]["primary"]["end"] == {"x": 100, "y": 200}
        assert len(blocks[0]["primary"]["track"]) == 2

    def test_keyboard_input_to_block(self):
        """keyboard_input 事件 → operation/keyboard_input 块。"""
        events = [
            _make_event(1.0, "keyboard_input", {
                "text": "hello",
                "physical_keys": ["h", "e", "l", "l", "o"],
                "detection_method": "uia_value_diff",
                "is_password": False,
            }),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "keyboard_input"
        assert blocks[0]["primary"]["text"] == "hello"
        assert blocks[0]["primary"]["detection_method"] == "uia_value_diff"
        assert blocks[0]["primary"]["is_password"] is False

    def test_focus_change_to_block(self):
        """focus_change 事件 → operation/focus_change 块。"""
        events = [
            _make_event(1.0, "focus_change", {"hwnd": 12345, "title": "记事本", "pid": 6789}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "focus_change"
        assert blocks[0]["primary"]["hwnd"] == 12345
        assert blocks[0]["primary"]["title"] == "记事本"
        assert blocks[0]["primary"]["pid"] == 6789

    def test_hotkey_maps_to_keyboard_input(self):
        """hotkey 事件 → operation/keyboard_input 块（detection_method=hotkey）。"""
        events = [
            _make_event(1.0, "hotkey", {"keys": ["ctrl", "c"], "combo": "copy"}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "keyboard_input"
        assert blocks[0]["primary"]["detection_method"] == "hotkey"
        assert blocks[0]["primary"]["text"] == "copy"

    def test_ime_switch_maps_to_keyboard_input(self):
        """ime_switch 事件 → operation/keyboard_input 块。"""
        events = [
            _make_event(1.0, "ime_switch", {"keys": ["shift", "space"]}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "keyboard_input"
        assert blocks[0]["primary"]["detection_method"] == "ime_switch"

    def test_password_masked_maps_to_keyboard_input(self):
        """password_masked 事件 → operation/keyboard_input 块（is_password=true，text 脱敏）。"""
        events = [
            _make_event(1.0, "password_masked", {
                "physical_keys": ["p", "a", "s", "s"],
                "detection_method": "password_masked",
            }),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "keyboard_input"
        assert blocks[0]["primary"]["is_password"] is True
        assert blocks[0]["primary"]["text"] == ""  # 脱敏

    def test_screen_frame_does_not_produce_block(self):
        """screen_frame 事件不生成操作块（用于截图关联）。"""
        events = [
            _make_frame_event(1.0, "frames/frame_00001.png"),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 0

    def test_multiple_events_produce_multiple_blocks_in_order(self):
        """多个操作事件 → 多个块，按时间戳顺序。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 10, "y": 20}),
            _make_event(2.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
            _make_event(3.0, "focus_change", {"hwnd": 1, "title": "A", "pid": 1}),
        ]
        blocks = aggregate_events(events)
        assert len(blocks) == 3
        assert [b["type"] for b in blocks] == ["mouse_click", "keyboard_input", "focus_change"]
        assert [b["timestamp"] for b in blocks] == [1.0, 2.0, 3.0]
        # id 递增
        assert blocks[0]["id"] == "b001"
        assert blocks[1]["id"] == "b002"
        assert blocks[2]["id"] == "b003"

    def test_block_has_required_fields(self):
        """块结构符合 02-block-design.md：id/category/type/timestamp/duration/primary/supplements/status。"""
        events = [_make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0})]
        blocks = aggregate_events(events)
        b = blocks[0]
        assert "id" in b
        assert "category" in b
        assert "type" in b
        assert "timestamp" in b
        assert "duration" in b
        assert "primary" in b
        assert "supplements" in b
        assert "status" in b
        # supplements 子字段
        assert "before_frame" in b["supplements"]
        assert "after_frame" in b["supplements"]
        assert "uia_snapshot" in b["supplements"]
        assert "linked_text" in b["supplements"]
        # status 子字段
        assert "marked_key" in b["status"]
        assert "marked_anomaly" in b["status"]
        assert "marked_automatable" in b["status"]
        assert "is_trimmed" in b["status"]

    def test_instant_events_have_zero_duration(self):
        """瞬时事件 duration=0.0。"""
        events = [_make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0})]
        blocks = aggregate_events(events)
        assert blocks[0]["duration"] == 0.0


# ============================ idle 块生成测试 ============================


class TestIdleBlockGeneration:
    """P4 idle 块生成：操作间隔 > idle_threshold 插入 idle 块。"""

    def test_gap_above_threshold_inserts_idle(self):
        """两操作间隔 >2s → 中间插入 idle 块。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(5.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
        ]
        blocks = aggregate_events(events, idle_threshold=2.0)
        # 期望：click + idle + keyboard
        assert len(blocks) == 3
        assert blocks[0]["type"] == "mouse_click"
        assert blocks[1]["type"] == "idle"
        assert blocks[2]["type"] == "keyboard_input"
        # idle 块属性
        idle_block = blocks[1]
        assert idle_block["duration"] == pytest.approx(4.0)  # 5.0 - 1.0
        assert idle_block["timestamp"] == 1.0  # idle 从上一操作时刻开始
        assert idle_block["primary"]["duration"] == pytest.approx(4.0)

    def test_gap_below_threshold_no_idle(self):
        """两操作间隔 <2s → 不插入 idle 块。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(2.5, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
        ]
        blocks = aggregate_events(events, idle_threshold=2.0)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "mouse_click"
        assert blocks[1]["type"] == "keyboard_input"

    def test_gap_equal_threshold_no_idle(self):
        """两操作间隔 =2s → 不插入 idle 块（> 阈值才插入，= 不插入）。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(3.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
        ]
        blocks = aggregate_events(events, idle_threshold=2.0)
        assert len(blocks) == 2  # 间隔 = 2.0，不 > 2.0，不插 idle

    def test_custom_idle_threshold(self):
        """可调 idle_threshold：阈值 5s 时间隔 3s 不插 idle，5.5s 插 idle。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(4.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
            _make_event(10.0, "focus_change", {"hwnd": 1, "title": "A", "pid": 1}),
        ]
        blocks = aggregate_events(events, idle_threshold=5.0)
        # 1.0 → 4.0（间隔 3s < 5s，无 idle）
        # 4.0 → 10.0（间隔 6s > 5s，插 idle）
        assert len(blocks) == 4  # click + keyboard + idle + focus
        assert blocks[2]["type"] == "idle"
        assert blocks[2]["duration"] == pytest.approx(6.0)

    def test_multiple_idle_blocks(self):
        """多个长间隔 → 多个 idle 块。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(5.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
            _make_event(10.0, "focus_change", {"hwnd": 1, "title": "A", "pid": 1}),
        ]
        blocks = aggregate_events(events, idle_threshold=2.0)
        # click + idle(4s) + keyboard + idle(5s) + focus
        assert len(blocks) == 5
        assert blocks[1]["type"] == "idle"
        assert blocks[3]["type"] == "idle"

    def test_no_idle_for_single_event(self):
        """单个操作事件 → 无 idle 块（无前一操作）。"""
        events = [_make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0})]
        blocks = aggregate_events(events)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "mouse_click"


# ============================ 截图关联测试 ============================


class TestFrameAssociation:
    """P4 截图关联：操作块关联前后截图。"""

    def test_before_frame_within_window(self):
        """操作前 200ms 内有帧 → 关联为 before_frame。"""
        events = [
            _make_frame_event(0.85, "frames/frame_001.png"),  # 操作前 150ms
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        blocks = aggregate_events(events, before_frame_window=0.2)
        assert blocks[0]["supplements"]["before_frame"] == "frames/frame_001.png"

    def test_before_frame_picks_closest(self):
        """操作前 200ms 内有多帧 → 取最近的一帧（timestamp 最大）。"""
        events = [
            _make_frame_event(0.82, "frames/frame_001.png"),  # 操作前 180ms
            _make_frame_event(0.95, "frames/frame_002.png"),  # 操作前 50ms（更近）
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        blocks = aggregate_events(events, before_frame_window=0.2)
        assert blocks[0]["supplements"]["before_frame"] == "frames/frame_002.png"

    def test_before_frame_outside_window(self):
        """操作前 200ms 外的帧 → 不关联（before_frame=None）。"""
        events = [
            _make_frame_event(0.7, "frames/frame_001.png"),  # 操作前 300ms（超出 200ms 窗口）
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        blocks = aggregate_events(events, before_frame_window=0.2)
        assert blocks[0]["supplements"]["before_frame"] is None

    def test_after_frame_within_window(self):
        """操作后 300ms 内有帧 → 关联为 after_frame。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_frame_event(1.2, "frames/frame_001.png"),  # 操作后 200ms
        ]
        blocks = aggregate_events(events, after_frame_window=0.3)
        assert blocks[0]["supplements"]["after_frame"] == "frames/frame_001.png"

    def test_after_frame_picks_closest(self):
        """操作后 300ms 内有多帧 → 取最近的一帧（timestamp 最小）。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_frame_event(1.05, "frames/frame_001.png"),  # 操作后 50ms（更近）
            _make_frame_event(1.25, "frames/frame_002.png"),  # 操作后 250ms
        ]
        blocks = aggregate_events(events, after_frame_window=0.3)
        assert blocks[0]["supplements"]["after_frame"] == "frames/frame_001.png"

    def test_after_frame_outside_window(self):
        """操作后 300ms 外的帧 → 不关联。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_frame_event(1.4, "frames/frame_001.png"),  # 操作后 400ms（超出 300ms）
        ]
        blocks = aggregate_events(events, after_frame_window=0.3)
        assert blocks[0]["supplements"]["after_frame"] is None

    def test_no_frames_both_none(self):
        """无截图事件 → before_frame=None + after_frame=None。"""
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        blocks = aggregate_events(events)
        assert blocks[0]["supplements"]["before_frame"] is None
        assert blocks[0]["supplements"]["after_frame"] is None

    def test_both_before_and_after_frames(self):
        """操作前后都有帧 → 同时关联 before + after。"""
        events = [
            _make_frame_event(0.9, "frames/before.png"),   # 操作前 100ms
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_frame_event(1.2, "frames/after.png"),    # 操作后 200ms
        ]
        blocks = aggregate_events(events, before_frame_window=0.2, after_frame_window=0.3)
        assert blocks[0]["supplements"]["before_frame"] == "frames/before.png"
        assert blocks[0]["supplements"]["after_frame"] == "frames/after.png"


# ============================ run_p4 + process_recording_package 端到端 ============================


class TestRunP4EndToEnd:
    """P4 端到端：给定录制包目录 → run_p4 → 验证 blocks.json 产出。"""

    def test_run_p4_writes_blocks_json(self, tmp_path):
        """run_p4 读 events.jsonl → 写 blocks.json。"""
        package_root = tmp_path / "rec_test"
        events = [
            _make_frame_event(0.9, "frames/frame_001.png"),
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 100, "y": 200}),
            _make_event(1.5, "keyboard_input", {"text": "hi", "detection_method": "uia_value_diff", "is_password": False}),
            _make_frame_event(1.6, "frames/frame_002.png"),
        ]
        _write_events_jsonl(package_root, events)

        blocks = run_p4(package_root)
        assert len(blocks) == 2  # click + keyboard

        # blocks.json 文件存在且可读
        blocks_file = package_root / "blocks.json"
        assert blocks_file.exists()
        loaded = json.loads(blocks_file.read_text(encoding="utf-8"))
        assert loaded == blocks

        # 验证内容
        assert loaded[0]["type"] == "mouse_click"
        assert loaded[1]["type"] == "keyboard_input"
        # click 块有 before_frame（0.9 在 1.0 前 100ms 内）
        assert loaded[0]["supplements"]["before_frame"] == "frames/frame_001.png"
        # keyboard 块有 after_frame（1.6 在 1.5 后 100ms 内）
        assert loaded[1]["supplements"]["after_frame"] == "frames/frame_002.png"

    def test_run_p4_missing_events_jsonl_raises(self, tmp_path):
        """events.jsonl 不存在 → FileNotFoundError。"""
        package_root = tmp_path / "rec_empty"
        package_root.mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            run_p4(package_root)

    def test_run_p4_empty_events_jsonl_produces_empty_blocks(self, tmp_path):
        """空 events.jsonl → 空 blocks.json。"""
        package_root = tmp_path / "rec_empty_events"
        _write_events_jsonl(package_root, [])
        blocks = run_p4(package_root)
        assert blocks == []


class TestProcessRecordingPackageSeam:
    """process_recording_package seam 测试：编排 P4（P5/P1 留空跳过）。"""

    def test_run_p4_only_produces_blocks(self, tmp_path):
        """run_p4=True, run_p5=False, run_p1=False → 只产 blocks.json。"""
        package_root = tmp_path / "rec_p4_only"
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        _write_events_jsonl(package_root, events)

        result = process_recording_package(
            package_root,
            options={"run_p4": True, "run_p5": False, "run_p1": False},
        )
        assert result.blocks_path is not None
        assert result.blocks_path.exists()
        assert result.block_count == 1
        assert result.keyframes_path is None
        assert result.transcript_path is None
        assert "p4" not in result.errors

    def test_p1_not_implemented_recorded_as_error(self, tmp_path):
        """run_p5=True + run_p1=True → P5 成功（已实现），P1 因 audio/mic.wav 缺失报错。"""
        package_root = tmp_path / "rec_p5_p1"
        # P5 需要 frames/ 目录存在
        (package_root / "frames").mkdir(parents=True)
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
        ]
        _write_events_jsonl(package_root, events)

        result = process_recording_package(
            package_root,
            options={"run_p4": True, "run_p5": True, "run_p1": True},
        )
        # P4 成功
        assert result.blocks_path is not None
        assert result.block_count == 1
        # P5 成功（Ticket 16 已实现）
        assert result.keyframes_path is not None
        assert "p5" not in result.errors
        # P1 因 audio/mic.wav 不存在报 FileNotFoundError（Ticket 17 已实现 run_p1）
        assert "p1" in result.errors
        assert "FileNotFoundError" in result.errors["p1"]
        # P1 产出路径为 None（出错时不写 transcript.json）
        assert result.transcript_path is None

    def test_default_options_run_p4_and_p5(self, tmp_path):
        """默认 options（None）→ run_p4=True + run_p5=True，P4 成功，P5 成功。"""
        package_root = tmp_path / "rec_default"
        # P5 需要 frames/ 目录存在
        (package_root / "frames").mkdir(parents=True)
        events = [
            _make_event(1.0, "mouse_click", {"button": "left", "clicks": 1, "x": 0, "y": 0}),
            _make_event(5.0, "keyboard_input", {"text": "a", "detection_method": "physical_keys", "is_password": False}),
        ]
        _write_events_jsonl(package_root, events)

        result = process_recording_package(package_root)
        # P4 成功（click + idle + keyboard）
        assert result.blocks_path is not None
        assert result.block_count == 3  # click + idle(4s) + keyboard
        # P5 成功（Ticket 16 已实现）
        assert result.keyframes_path is not None
        assert "p5" not in result.errors

    def test_p4_error_recorded_not_crash(self, tmp_path):
        """P4 出错 → 记录到 errors，不崩溃整个 pipeline。"""
        package_root = tmp_path / "rec_no_events"
        package_root.mkdir(parents=True)
        # 不写 events.jsonl，P4 会 FileNotFoundError

        result = process_recording_package(
            package_root,
            options={"run_p4": True, "run_p5": False, "run_p1": False},
        )
        assert "p4" in result.errors
        assert "FileNotFoundError" in result.errors["p4"]
        assert result.blocks_path is None
