"""L1 处理层 Ticket 16：P5 关键帧抽取器测试

测试策略（spec-l1.md Testing Decisions）：
- 用真实的 frames/ 目录 + events.jsonl 测试（构造 PNG 文件）
- P5 关键帧抽取器：操作帧保留 + 定时截图全局 pHash 去重
- 验证 keyframes.json 格式 + 保留原因 + 去重效果

覆盖 Ticket 16 acceptance criteria：
- [x] P5 扫描 frames/ + 读 events.jsonl 获取帧时间戳
- [x] P5 保留所有操作帧（retain_reason="operation"）
- [x] P5 对定时截图做全局 pHash 去重（retain_reason="timer_unique"）
- [x] P5 pHash 算法复用 L0 D035（compute_phash / phash_distance）
- [x] P5 去重阈值可配置
- [x] keyframes.json 格式：[{frame_path, timestamp, retain_reason}, ...]
- [x] 单测：相似帧序列全局去重只留第一张
- [x] 单测：操作帧全部保留
- [x] 单测：定时截图去重效果
- [x] 接 process_recording_package seam
"""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from lib.recorder.processor import process_recording_package
from lib.recorder.processor.p5_keyframe_extractor import (
    extract_keyframes,
    run_p5,
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


def _make_click_event(ts: float) -> dict:
    """构造 mouse_click 事件。"""
    return _make_event(ts, "mouse_click", {"button": "left", "clicks": 1, "x": 100, "y": 200})


def _make_focus_event(ts: float) -> dict:
    """构造 focus_change 事件。"""
    return _make_event(ts, "focus_change", {"hwnd": 12345, "title": "test", "pid": 1})


def _write_events_jsonl(package_root: Path, events: list[dict]) -> Path:
    """把事件列表写入录制包的 events.jsonl。"""
    package_root.mkdir(parents=True, exist_ok=True)
    events_file = package_root / "events.jsonl"
    lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_file


def _make_noise_image(seed: int, size=(32, 32)) -> Image.Image:
    """生成随机噪声图（每张 pHash 差异大）。"""
    rng = np.random.RandomState(seed)
    arr = rng.randint(0, 256, size=(*size, 3), dtype=np.uint8)
    return Image.fromarray(arr, "RGB")


def _make_solid_image(color=(100, 100, 100), size=(32, 32)) -> Image.Image:
    """生成纯色图（所有纯色图 pHash 相同 = 0xFFFFFFFFFFFFFFFF）。"""
    return Image.new("RGB", size, color)


def _write_frame(frames_dir: Path, name: str, img: Image.Image) -> str:
    """写一帧 PNG 到 frames/ 目录，返回相对路径。"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frames_dir / name
    img.save(frame_path, format="PNG")
    return f"frames/{name}"


# ============================ 操作帧保留测试 ============================


class TestOperationFrameRetention:
    """P5 保留所有操作帧（不受全局去重影响）。"""

    def test_click_frame_retained_as_operation(self, tmp_path):
        """click 后 200ms 内的帧 → retain_reason="operation"。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "frame_001.png", _make_noise_image(1))
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.1, frame_path),  # click 后 100ms
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "operation"
        assert keyframes[0]["frame_path"] == frame_path
        assert keyframes[0]["timestamp"] == 1.1

    def test_drag_frame_retained_as_operation(self, tmp_path):
        """drag 后 200ms 内的帧 → retain_reason="operation"。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "frame_001.png", _make_noise_image(1))
        events = [
            _make_event(1.0, "mouse_drag", {
                "button": "left",
                "start": {"x": 0, "y": 0},
                "end": {"x": 100, "y": 100},
                "track": [],
            }),
            _make_frame_event(1.15, frame_path),  # drag 后 150ms
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "operation"

    def test_focus_change_frame_retained_as_operation(self, tmp_path):
        """focus_change 后 200ms 内的帧 → retain_reason="operation"。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "frame_001.png", _make_noise_image(1))
        events = [
            _make_focus_event(1.0),
            _make_frame_event(1.18, frame_path),  # focus 后 180ms
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "operation"

    def test_operation_frame_outside_window_is_timer(self, tmp_path):
        """操作事件后 >200ms 的帧 → 视为定时截图（timer_unique）。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "frame_001.png", _make_noise_image(1))
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.3, frame_path),  # click 后 300ms（超出 200ms 窗口）
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "timer_unique"

    def test_multiple_operation_frames_all_retained(self, tmp_path):
        """多个操作帧（即使相似）全部保留。"""
        frames_dir = tmp_path / "frames"
        # 用相同的纯色图（pHash 相同），操作帧应全部保留不受去重影响
        f1 = _write_frame(frames_dir, "f1.png", _make_solid_image())
        f2 = _write_frame(frames_dir, "f2.png", _make_solid_image())
        f3 = _write_frame(frames_dir, "f3.png", _make_solid_image())
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.05, f1),  # 操作帧 1
            _make_click_event(2.0),
            _make_frame_event(2.05, f2),  # 操作帧 2（与帧 1 相同但保留）
            _make_click_event(3.0),
            _make_frame_event(3.05, f3),  # 操作帧 3（与帧 1/2 相同但保留）
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 3
        assert all(kf["retain_reason"] == "operation" for kf in keyframes)

    def test_keyboard_input_does_not_trigger_operation_frame(self, tmp_path):
        """keyboard_input 不触发 burst，其后的帧不视为操作帧。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "frame_001.png", _make_noise_image(1))
        events = [
            _make_event(1.0, "keyboard_input", {
                "text": "hi", "detection_method": "physical_keys", "is_password": False,
            }),
            _make_frame_event(1.1, frame_path),  # keyboard_input 后 100ms
        ]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "timer_unique"  # 不是操作帧


# ============================ 定时截图全局 pHash 去重测试 ============================


class TestTimerFrameGlobalDedup:
    """P5 对定时截图做全局 pHash 去重。"""

    def test_similar_timer_frames_deduped(self, tmp_path):
        """相似定时帧（纯色图 pHash 相同）→ 只留第一张。"""
        frames_dir = tmp_path / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_solid_image())
        f2 = _write_frame(frames_dir, "f2.png", _make_solid_image())
        f3 = _write_frame(frames_dir, "f3.png", _make_solid_image())
        events = [
            _make_frame_event(1.0, f1),
            _make_frame_event(2.0, f2),  # 与 f1 相似
            _make_frame_event(3.0, f3),  # 与 f1 相似
        ]
        keyframes = extract_keyframes(events, frames_dir, phash_threshold=5)
        assert len(keyframes) == 1  # 只留第一张
        assert keyframes[0]["frame_path"] == f1
        assert keyframes[0]["retain_reason"] == "timer_unique"

    def test_different_timer_frames_all_retained(self, tmp_path):
        """不同定时帧（随机噪声 pHash 差异大）→ 全部保留。"""
        frames_dir = tmp_path / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(2))
        f3 = _write_frame(frames_dir, "f3.png", _make_noise_image(3))
        events = [
            _make_frame_event(1.0, f1),
            _make_frame_event(2.0, f2),
            _make_frame_event(3.0, f3),
        ]
        keyframes = extract_keyframes(events, frames_dir, phash_threshold=5)
        assert len(keyframes) == 3
        assert all(kf["retain_reason"] == "timer_unique" for kf in keyframes)

    def test_scroll_back_scenario(self, tmp_path):
        """滚动后回到原位置：第 1 帧和第 3 帧相似 → 第 3 帧被去重。"""
        frames_dir = tmp_path / "frames"
        # 模拟滚动：状态 A → 状态 B → 状态 A（回到原位置）
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(10))  # 状态 A
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(20))  # 状态 B
        f3 = _write_frame(frames_dir, "f3.png", _make_noise_image(10))  # 状态 A（与 f1 相似）
        events = [
            _make_frame_event(1.0, f1),
            _make_frame_event(2.0, f2),
            _make_frame_event(3.0, f3),  # 与 f1 相似 → 去重
        ]
        keyframes = extract_keyframes(events, frames_dir, phash_threshold=5)
        assert len(keyframes) == 2  # f1 + f2（f3 被去重）
        assert keyframes[0]["frame_path"] == f1
        assert keyframes[1]["frame_path"] == f2

    def test_custom_phash_threshold(self, tmp_path):
        """可调 phash_threshold：阈值 0 = 严格去重（仅完全相同跳过）。"""
        frames_dir = tmp_path / "frames"
        # 用两张不同的噪声图
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(2))
        events = [
            _make_frame_event(1.0, f1),
            _make_frame_event(2.0, f2),
        ]
        # threshold=0：仅完全相同（距离=0）才去重，不同噪声图距离 >0 → 全保留
        keyframes = extract_keyframes(events, frames_dir, phash_threshold=0)
        assert len(keyframes) == 2

    def test_timer_frame_similar_to_operation_frame_deduped(self, tmp_path):
        """定时帧与已保留的操作帧相似 → 定时帧被去重。"""
        frames_dir = tmp_path / "frames"
        # 操作帧和定时帧用相同的纯色图
        op_frame = _write_frame(frames_dir, "op.png", _make_solid_image())
        timer_frame = _write_frame(frames_dir, "timer.png", _make_solid_image())
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.05, op_frame),     # 操作帧（保留）
            _make_frame_event(5.0, timer_frame),    # 定时帧（与操作帧相似 → 去重）
        ]
        keyframes = extract_keyframes(events, frames_dir, phash_threshold=5)
        assert len(keyframes) == 1  # 只有操作帧
        assert keyframes[0]["retain_reason"] == "operation"
        assert keyframes[0]["frame_path"] == op_frame


# ============================ keyframes.json 格式测试 ============================


class TestKeyframesJsonFormat:
    """keyframes.json 格式验证。"""

    def test_keyframe_has_required_fields(self, tmp_path):
        """每个关键帧含 frame_path / timestamp / retain_reason。"""
        frames_dir = tmp_path / "frames"
        frame_path = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        events = [_make_frame_event(1.0, frame_path)]
        keyframes = extract_keyframes(events, frames_dir)
        assert len(keyframes) == 1
        kf = keyframes[0]
        assert "frame_path" in kf
        assert "timestamp" in kf
        assert "retain_reason" in kf

    def test_retain_reason_values(self, tmp_path):
        """retain_reason 只能是 "operation" 或 "timer_unique"。"""
        frames_dir = tmp_path / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(2))
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.05, f1),  # operation
            _make_frame_event(5.0, f2),   # timer_unique
        ]
        keyframes = extract_keyframes(events, frames_dir)
        reasons = {kf["retain_reason"] for kf in keyframes}
        assert reasons <= {"operation", "timer_unique"}

    def test_keyframes_ordered_by_timestamp(self, tmp_path):
        """关键帧按时间戳升序。"""
        frames_dir = tmp_path / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(2))
        f3 = _write_frame(frames_dir, "f3.png", _make_noise_image(3))
        events = [
            _make_frame_event(3.0, f3),
            _make_frame_event(1.0, f1),
            _make_frame_event(2.0, f2),
        ]
        keyframes = extract_keyframes(events, frames_dir)
        timestamps = [kf["timestamp"] for kf in keyframes]
        assert timestamps == sorted(timestamps)


# ============================ run_p5 + process_recording_package 端到端 ============================


class TestRunP5EndToEnd:
    """P5 端到端：给定录制包目录 → run_p5 → 验证 keyframes.json 产出。"""

    def test_run_p5_writes_keyframes_json(self, tmp_path):
        """run_p5 读 events.jsonl + frames/ → 写 keyframes.json。"""
        package_root = tmp_path / "rec_test"
        frames_dir = package_root / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        f2 = _write_frame(frames_dir, "f2.png", _make_noise_image(2))
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.05, f1),  # operation
            _make_frame_event(5.0, f2),   # timer_unique
        ]
        _write_events_jsonl(package_root, events)

        keyframes = run_p5(package_root)
        assert len(keyframes) == 2

        # keyframes.json 文件存在且可读
        keyframes_file = package_root / "keyframes.json"
        assert keyframes_file.exists()
        loaded = json.loads(keyframes_file.read_text(encoding="utf-8"))
        assert loaded == keyframes

        assert loaded[0]["retain_reason"] == "operation"
        assert loaded[1]["retain_reason"] == "timer_unique"

    def test_run_p5_missing_events_jsonl_raises(self, tmp_path):
        """events.jsonl 不存在 → FileNotFoundError。"""
        package_root = tmp_path / "rec_empty"
        package_root.mkdir(parents=True)
        (package_root / "frames").mkdir()
        with pytest.raises(FileNotFoundError):
            run_p5(package_root)

    def test_run_p5_missing_frames_dir_raises(self, tmp_path):
        """frames/ 目录不存在 → FileNotFoundError。"""
        package_root = tmp_path / "rec_no_frames"
        _write_events_jsonl(package_root, [_make_click_event(1.0)])
        with pytest.raises(FileNotFoundError):
            run_p5(package_root)

    def test_run_p5_empty_events_produces_empty_keyframes(self, tmp_path):
        """空 events.jsonl → 空 keyframes.json。"""
        package_root = tmp_path / "rec_empty_events"
        _write_events_jsonl(package_root, [])
        (package_root / "frames").mkdir()
        keyframes = run_p5(package_root)
        assert keyframes == []

    def test_pipeline_p5_now_implemented(self, tmp_path):
        """process_recording_package run_p5=True → P5 实现，不再 not_implemented。"""
        package_root = tmp_path / "rec_pipeline"
        frames_dir = package_root / "frames"
        f1 = _write_frame(frames_dir, "f1.png", _make_noise_image(1))
        events = [
            _make_click_event(1.0),
            _make_frame_event(1.05, f1),
        ]
        _write_events_jsonl(package_root, events)

        result = process_recording_package(
            package_root,
            options={"run_p4": True, "run_p5": True, "run_p1": True},
        )
        # P4 成功
        assert result.blocks_path is not None
        # P5 成功（不再 not_implemented）
        assert result.keyframes_path is not None
        assert result.keyframes_path.exists()
        assert result.keyframe_count >= 1
        assert "p5" not in result.errors
        # P1 因 audio/mic.wav 不存在报 FileNotFoundError（Ticket 17 已实现 run_p1）
        assert "p1" in result.errors
        assert "FileNotFoundError" in result.errors["p1"]

    def test_dedup_effect_many_similar_frames(self, tmp_path):
        """去重效果：多张相似定时帧 → 降到 1 张。"""
        package_root = tmp_path / "rec_dedup"
        frames_dir = package_root / "frames"
        # 10 张相同纯色图（定时截图）
        frame_paths = []
        for i in range(10):
            fp = _write_frame(frames_dir, f"f{i:02d}.png", _make_solid_image())
            frame_paths.append(fp)
        events = [_make_frame_event(float(i), fp) for i, fp in enumerate(frame_paths)]
        _write_events_jsonl(package_root, events)

        keyframes = run_p5(package_root, phash_threshold=5)
        # 10 张相似图 → 只留 1 张
        assert len(keyframes) == 1
        assert keyframes[0]["retain_reason"] == "timer_unique"
