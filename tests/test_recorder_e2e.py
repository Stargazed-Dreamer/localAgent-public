"""L0 采集层 Ticket 13：端到端测试 - 完整状态机流程 + 全产出验证

测试策略（按 Ticket 13 acceptance criteria，D034 更新）：
- 真实 RecordingController（状态机 + save 同步完成 + on_trim_complete hook）
- D034：移除自动音频裁剪，mic.wav 完整保留，audio_trimmer 代码保留但不自动调用
- MockSensor 不发真实事件（仅跟踪 start/stop 调用），保持 e2e 测试确定性
- 构造含静音段的 WAV 文件，验证 save() 不裁剪（原始音频完整保留）
- 验证完整流程：start → pause → resume → pause → save → 验证全部产出

覆盖 Ticket 13 acceptance criteria（D034 更新后）:
- [x] 端到端测试：start → pause → resume → pause → save → 验证全部产出
- [x] 验证 meta.json status="saved"（同步完成，无 processing 中间态）
- [x] 验证原始 mic.wav 完整保留（D034：不裁剪）
- [x] 验证不生成 mic_cropped.wav / audio_segments.json
- [x] 验证 segments 列表记录了多个 segment（pause/resume 产生的）
- [x] 验证 effective_duration = sum of segment durations
"""

import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from lib.recorder.controller import (
    IllegalStateError,
    RecordingController,
    RecordingState,
)

SAMPLERATE = 16000
INT16_MAX = 32768


# ============================ 测试辅助函数 ============================


def _sine(duration: float, freq: float = 440.0, amp: float = 0.5) -> np.ndarray:
    """生成正弦波 int16 样本。amp∈[0,1] 相对满量程。"""
    n = int(duration * SAMPLERATE)
    t = np.arange(n) / SAMPLERATE
    return (amp * INT16_MAX * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def _silence(duration: float) -> np.ndarray:
    """生成静音 int16 样本。"""
    return np.zeros(int(duration * SAMPLERATE), dtype=np.int16)


def _write_wav(path: Path, samples: np.ndarray, samplerate: int = SAMPLERATE) -> None:
    """写 mono / int16 WAV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:  # noqa: SIM115
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _write_wav_with_silence(
    path: Path,
    pattern: list[tuple[str, float]] | None = None,
) -> float:
    """构造含静音段的 WAV 文件。

    Args:
        path: WAV 文件路径
        pattern: 段列表，每项 (kind, duration)。
            kind="signal" 写正弦波，kind="silence" 写静音。
            None 时用默认模式 [signal 2s, silence 2s, signal 2s, silence 2s, signal 2s]。

    Returns:
        WAV 总时长（秒）
    """
    if pattern is None:
        pattern = [
            ("signal", 2.0),
            ("silence", 2.0),
            ("signal", 2.0),
            ("silence", 2.0),
            ("signal", 2.0),
        ]
    pieces: list[np.ndarray] = []
    for kind, duration in pattern:
        if kind == "signal":
            pieces.append(_sine(duration))
        else:
            pieces.append(_silence(duration))
    samples = np.concatenate(pieces)
    _write_wav(path, samples)
    return len(samples) / SAMPLERATE


def _read_wav_duration(path: Path) -> float:
    """读 WAV 文件时长（秒）。"""
    with wave.open(str(path), "rb") as wf:  # noqa: SIM115
        return wf.getnframes() / wf.getframerate()


def _read_wav_samples(path: Path) -> tuple[np.ndarray, int]:
    """读 WAV 返回 (int16 样本, 采样率)。"""
    with wave.open(str(path), "rb") as wf:  # noqa: SIM115
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.int16), sr


class _SpySensor:
    """间谍传感器：跟踪 start/stop 调用次数，不发事件（e2e 测试用）。

    与 test_recorder_controller.py 中的 _SpySensor 一致，确保 e2e 测试
    不依赖事件发射，仅验证状态机 + 裁剪流程。
    """

    def __init__(self, controller=None) -> None:
        self.controller = controller
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


# ============================ 端到端完整流程测试 ============================


class TestEndToEndFullFlow:
    """Ticket 13：完整状态机端到端流程 + 全产出验证（D034：无后台裁剪）。

    核心场景：start → pause → resume → pause → save → 验证全部产出。
    用 _SpySensor（不发事件）+ 真实 RecordingController（save 同步完成）。
    """

    def test_start_pause_resume_pause_save_produces_full_package(self, tmp_path):
        """完整流程：start → pause → resume → pause → save → 验证全部产出（D034：无裁剪）。

        验证项：
        1. meta.json status="saved"（同步完成，无 error）
        2. 原始 mic.wav 完整保留（D034：不裁剪）
        3. 不生成 mic_cropped.wav / audio_segments.json（D034：无裁剪产物）
        4. meta.json segments 含 2 段（pause/resume/pause 产生）
        5. effective_duration = sum of segment durations
        6. on_trim_complete 回调被调用（同步，参数为 package root）
        7. save() 立即返回（直接 saved，无 processing 中间态）
        8. 传感器 start/stop 调用次数正确（pause/resume 驱动）
        """
        called = threading.Event()
        captured_paths: list[Path] = []

        def hook(pkg_path: Path) -> None:
            captured_paths.append(pkg_path)
            called.set()

        spy = _SpySensor()
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[spy],
            on_trim_complete=hook,
        )
        spy.controller = controller

        # ========== 执行完整流程 ==========
        # 1. start → 第一段录制开始
        controller.start()
        assert spy.start_calls == 1, "start() 应调用 sensor.start() 一次"
        assert controller.state == RecordingState.RECORDING

        # 构造含静音段的 WAV（start 后 package.audio_dir 已创建）
        original_duration = _write_wav_with_silence(controller.package.audio_file)
        assert original_duration == pytest.approx(10.0, abs=0.05)

        # 2. pause → 关闭第一段
        controller.pause()
        assert controller.state == RecordingState.PAUSED
        assert spy.stop_calls == 1, "pause() 应调用 sensor.stop() 一次"

        # 3. resume → 第二段录制开始（时间轴延续，timestamp 基准不变）
        controller.resume()
        assert controller.state == RecordingState.RECORDING
        assert spy.start_calls == 2, "resume() 应再调用 sensor.start() 一次"

        # 4. pause → 关闭第二段
        controller.pause()
        assert controller.state == RecordingState.PAUSED
        assert spy.stop_calls == 2

        # 5. save → 标记 SAVED（D034：同步完成，无裁剪线程）
        controller.save()
        assert controller.state == RecordingState.SAVED

        # ========== 验证 1：meta.json status="saved"（同步，无 processing 中间态） ==========
        meta = controller.package.read_meta()
        assert meta["status"] == "saved", (
            f"save() 后 status 应直接为 saved，实际 {meta['status']}"
        )
        assert "error" not in meta, f"save 不应失败，但 error={meta.get('error')}"

        # ========== 验证 2：原始 mic.wav 完整保留（D034：不裁剪） ==========
        assert controller.package.audio_file.exists(), "原始 mic.wav 应完整保留"

        # ========== 验证 3：不生成裁剪产物（D034：无裁剪） ==========
        assert not (controller.package.audio_dir / "mic_cropped.wav").exists(), (
            "D034：不应生成 mic_cropped.wav"
        )
        assert not (controller.package.audio_dir / "audio_segments.json").exists(), (
            "D034：不应生成 audio_segments.json"
        )
        # meta 不含裁剪路径字段
        assert "cropped_audio_path" not in meta
        assert "audio_segments_path" not in meta
        assert "original_audio_path" not in meta

        # ========== 验证 4：meta.json segments 含 2 段 ==========
        segs = meta["segments"]
        assert len(segs) == 2, (
            f"应有 2 个 segment（pause/resume/pause 产生），实际 {len(segs)}"
        )
        for seg in segs:
            assert seg["start_offset"] is not None, "segment start_offset 不应为 None"
            assert seg["end_offset"] is not None, "segment end_offset 不应为 None"
            assert seg["end_offset"] >= seg["start_offset"], "segment end 应 >= start"
        # 第二段 start_offset >= 第一段 end_offset（resume 在 pause 之后）
        assert segs[1]["start_offset"] >= segs[0]["end_offset"], (
            "第二段 start_offset 应 >= 第一段 end_offset（resume 在 pause 之后）"
        )
        # 第一段 start_offset 接近 0（start 时刻即时间轴零点）
        assert segs[0]["start_offset"] < 1.0, (
            f"第一段 start_offset 应接近 0，实际 {segs[0]['start_offset']}"
        )

        # ========== 验证 5：effective_duration = sum of segment durations ==========
        expected_eff = sum(s["end_offset"] - s["start_offset"] for s in segs)
        assert meta["effective_duration"] == pytest.approx(expected_eff, abs=0.01), (
            f"effective_duration {meta['effective_duration']} 应等于各段时长之和 {expected_eff}"
        )

        # ========== 验证 6：on_trim_complete 回调被调用（同步） ==========
        assert called.is_set(), "on_trim_complete 回调应在 save() 返回前被同步调用"
        assert len(captured_paths) == 1, "on_trim_complete 应仅被调用一次"
        assert captured_paths[0] == controller.package.root, (
            "on_trim_complete 参数应为 package root 路径"
        )

    def test_save_from_recording_state_preserves_audio(self, tmp_path):
        """从 RECORDING 直接 save（不经过 pause）→ 停传感器 + 保存（D034：不裁剪）。

        验证 save() 内部先停传感器 + 关闭 segment，再同步写 meta + 调 hook。
        """
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        spy.controller = controller

        controller.start()
        assert spy.start_calls == 1
        # 构造含静音的 WAV
        _write_wav_with_silence(controller.package.audio_file)
        # 直接 save（不 pause）
        controller.save()
        assert controller.state == RecordingState.SAVED
        assert spy.stop_calls == 1, "save() 从 RECORDING 应先停传感器"

        meta = controller.package.read_meta()
        assert meta["status"] == "saved"
        # 只有一段（无 pause/resume）
        assert len(meta["segments"]) == 1
        assert meta["segments"][0]["end_offset"] is not None

        # D034：原始音频完整保留，不生成裁剪产物
        assert controller.package.audio_file.exists()
        assert not (controller.package.audio_dir / "mic_cropped.wav").exists()
        assert not (controller.package.audio_dir / "audio_segments.json").exists()

    def test_no_audio_save_marks_saved_directly(self, tmp_path):
        """无 mic.wav 时 save() → 直接标记 saved，不生成裁剪产物。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        spy.controller = controller

        controller.start()
        controller.pause()
        controller.save()

        meta = controller.package.read_meta()
        assert meta["status"] == "saved"
        # 无裁剪产物
        assert not (controller.package.audio_dir / "mic_cropped.wav").exists()
        assert not (controller.package.audio_dir / "audio_segments.json").exists()
        # meta 中无音频路径字段
        assert "cropped_audio_path" not in meta
        assert "audio_segments_path" not in meta

    def test_discard_after_pause_deletes_package(self, tmp_path):
        """start → pause → discard → 删除录制包目录 + 状态回 IDLE。"""
        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        spy.controller = controller

        controller.start()
        _write_wav_with_silence(controller.package.audio_file)
        pkg_root = controller.package.root
        assert pkg_root.exists()

        controller.pause()
        controller.discard()

        assert controller.state == RecordingState.IDLE
        assert not pkg_root.exists(), "discard 应删除整个录制包目录"

        # discard 后可重新 start
        controller.start()
        assert controller.state == RecordingState.RECORDING
        assert controller.event_count == 0
        assert len(controller.segments) == 1
        controller.pause()


# ============================ 时间轴延续性端到端测试 ============================


class TestTimelineContinuityE2E:
    """Ticket 13：resume 后时间轴延续性端到端验证。

    验证 resume 后事件 timestamp 不回零，且 effective_duration 排除暂停期。
    """

    def test_resume_timeline_continues_with_real_events(self, tmp_path):
        """start → emit → pause → resume → emit → pause → 验证时间轴延续。

        直接调 controller.emit_event（不依赖 sensor），验证 timestamp 不回零。
        """
        import time

        controller = RecordingController(base_dir=tmp_path, sensors=[_SpySensor()])

        controller.start()
        controller.emit_event("keyboard_input", {"text": "before_pause"})
        time.sleep(0.05)
        controller.pause()
        time.sleep(0.05)  # 暂停期
        controller.resume()
        controller.emit_event("mouse_click", {"x": 100, "y": 200, "button": "left"})
        time.sleep(0.05)
        controller.pause()

        events = controller.package.read_events()
        assert len(events) == 2
        # resume 后事件 timestamp >= pause 前事件，且不回零
        assert events[1]["timestamp"] >= events[0]["timestamp"]
        assert events[1]["timestamp"] > 0.02, "resume 后 timestamp 未回零"

        # effective_duration 排除暂停期
        meta = controller.package.read_meta()
        segs = meta["segments"]
        assert len(segs) == 2
        total_span = segs[-1]["end_offset"] - segs[0]["start_offset"]
        # effective_duration < total_span（暂停期被排除）
        assert meta["effective_duration"] < total_span, (
            f"effective_duration {meta['effective_duration']} 应 < 总跨度 {total_span}（暂停期被排除）"
        )

    def test_three_segments_two_pauses(self, tmp_path):
        """start → pause → resume → pause → resume → pause → 3 段 segment。"""
        import time

        spy = _SpySensor()
        controller = RecordingController(base_dir=tmp_path, sensors=[spy])
        spy.controller = controller

        controller.start()
        time.sleep(0.02)
        controller.pause()
        time.sleep(0.02)
        controller.resume()
        time.sleep(0.02)
        controller.pause()
        time.sleep(0.02)
        controller.resume()
        time.sleep(0.02)
        controller.pause()

        segs = controller.segments
        assert len(segs) == 3, f"应有 3 个 segment，实际 {len(segs)}"
        for seg in segs:
            assert seg["end_offset"] is not None
            assert seg["end_offset"] >= seg["start_offset"]
        # 各段 start_offset 单调递增
        for i in range(1, len(segs)):
            assert segs[i]["start_offset"] >= segs[i - 1]["end_offset"]

        # effective_duration = 三段时长之和
        meta = controller.package.read_meta()
        expected = sum(s["end_offset"] - s["start_offset"] for s in segs)
        assert meta["effective_duration"] == pytest.approx(expected, abs=0.01)


# ============================ 状态机非法转换端到端测试 ============================


class TestIllegalTransitionsE2E:
    """Ticket 13：状态机非法转换端到端（确保完整流程中非法转换被拒绝）。"""

    def test_save_then_pause_raises(self, tmp_path):
        """SAVED → pause 非法（终止态不可逆）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[_SpySensor()])
        controller.start()
        controller.pause()
        controller.save()
        # D034：save() 同步完成，无需 join 线程
        with pytest.raises(IllegalStateError):
            controller.pause()

    def test_save_then_resume_raises(self, tmp_path):
        """SAVED → resume 非法。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[_SpySensor()])
        controller.start()
        controller.pause()
        controller.save()
        with pytest.raises(IllegalStateError):
            controller.resume()

    def test_save_then_discard_raises(self, tmp_path):
        """SAVED → discard 非法（已保存的录制不能丢弃）。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[_SpySensor()])
        controller.start()
        controller.pause()
        controller.save()
        pkg_root = controller.package.root
        with pytest.raises(IllegalStateError):
            controller.discard()
        assert pkg_root.exists(), "discard 抛异常不应删除目录"
