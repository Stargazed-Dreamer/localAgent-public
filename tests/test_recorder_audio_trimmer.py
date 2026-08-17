"""L0 录制器 Ticket 10：audio_trimmer 单测

测试策略：
- 用 numpy 生成正弦波 + 静音段构造 WAV（不依赖真实录音）
- 验证：静音删除、audio_segments.json 格式、无静音不变、全静音占位、
  键鼠受保护区不删除、瞬态噪声衰减、裁剪后 WAV 可被 wave 读回、RMS 计算正确
"""

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from lib.recorder.audio_trimmer import (
    FRAME_DURATION,
    INT16_MAX,
    SILENCE_THRESHOLD_DB,
    _compute_rms_db_per_frame,
    trim_audio,
)
from lib.recorder.types import KIND_KEYBOARD_INPUT, KIND_MOUSE_CLICK, make_event

SAMPLERATE = 16000
INT16_PEAK = 32767


# ============================ 测试辅助函数 ============================


def _write_wav(path: Path, samples: np.ndarray, samplerate: int = SAMPLERATE) -> None:
    """写 mono / int16 WAV。"""
    with wave.open(str(path), "wb") as wf:  # noqa: SIM115
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _read_wav_samples(path: Path) -> tuple[np.ndarray, int]:
    """读 WAV 返回 (int16 样本, 采样率)。"""
    with wave.open(str(path), "rb") as wf:  # noqa: SIM115
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.int16), sr


def _sine(
    duration: float, freq: float = 440.0, amp: float = 0.3, samplerate: int = SAMPLERATE
) -> np.ndarray:
    """生成正弦波 int16 样本。amp∈[0,1] 相对满量程。"""
    n = int(duration * samplerate)
    t = np.arange(n) / samplerate
    return (amp * INT16_MAX * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def _silence(duration: float, samplerate: int = SAMPLERATE) -> np.ndarray:
    """生成静音 int16 样本。"""
    return np.zeros(int(duration * samplerate), dtype=np.int16)


def _write_events(path: Path, events: list[dict]) -> None:
    """写 events.jsonl。"""
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _make_event_at(ts: float, kind: str) -> dict:
    """构造一个位于 ts 秒的事件。"""
    return make_event(timestamp=ts, abs_timestamp=ts, kind=kind, payload={})


# ============================ RMS 计算测试 ============================


class TestRmsComputation:
    def test_full_scale_sine_rms(self):
        """满量程正弦波 RMS_dB ≈ -3.01 dB（RMS = peak/√2）"""
        samples = _sine(1.0, amp=1.0)
        rms_db = _compute_rms_db_per_frame(samples, SAMPLERATE, FRAME_DURATION)
        assert len(rms_db) == 10  # 1s / 0.1s = 10 帧
        # 满量程正弦 RMS_dB = 20*log10(1/√2) ≈ -3.01
        for db in rms_db:
            assert abs(db - (-3.01)) < 0.1

    def test_silence_rms_below_threshold(self):
        """静音 RMS_dB 应 < -40 dB（判为静音）"""
        samples = _silence(1.0)
        rms_db = _compute_rms_db_per_frame(samples, SAMPLERATE, FRAME_DURATION)
        assert len(rms_db) == 10
        for db in rms_db:
            assert db < SILENCE_THRESHOLD_DB

    def test_frame_size_is_0_1s(self):
        """帧大小 = 0.1s（1600 samples @ 16kHz）"""
        samples = _sine(0.35)  # 3.5 帧
        rms_db = _compute_rms_db_per_frame(samples, SAMPLERATE, FRAME_DURATION)
        # 尾部 0.05s 不足一帧，应被忽略 → 3 帧
        assert len(rms_db) == 3

    def test_empty_samples_returns_empty(self):
        """空音频返回空 RMS 数组"""
        rms_db = _compute_rms_db_per_frame(np.zeros(0, dtype=np.int16), SAMPLERATE, FRAME_DURATION)
        assert len(rms_db) == 0


# ============================ 静音裁剪测试 ============================


class TestSilenceTrimming:
    def test_trim_removes_long_silence(self, tmp_path):
        """[信号 2s + 静音 2s + 信号 2s] → 静音删除，裁剪后 ~4s，2 段"""
        samples = np.concatenate([_sine(2.0), _silence(2.0), _sine(2.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)

        assert cropped_wav.exists()
        assert seg_json.exists()
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert data["original_duration"] == pytest.approx(6.0, abs=0.05)
        assert data["cropped_duration"] == pytest.approx(4.0, abs=0.15)
        assert len(data["segments"]) == 2
        # 第一段：原始 [0, 2] → 裁剪 [0, 2]
        assert data["segments"][0]["original_start"] == pytest.approx(0.0, abs=0.05)
        assert data["segments"][0]["original_end"] == pytest.approx(2.0, abs=0.05)
        assert data["segments"][0]["cropped_start"] == pytest.approx(0.0, abs=0.05)
        assert data["segments"][0]["cropped_end"] == pytest.approx(2.0, abs=0.05)
        # 第二段：原始 [4, 6] → 裁剪 [2, 4]
        assert data["segments"][1]["original_start"] == pytest.approx(4.0, abs=0.05)
        assert data["segments"][1]["original_end"] == pytest.approx(6.0, abs=0.05)
        assert data["segments"][1]["cropped_start"] == pytest.approx(2.0, abs=0.05)
        assert data["segments"][1]["cropped_end"] == pytest.approx(4.0, abs=0.05)

    def test_short_silence_not_removed(self, tmp_path):
        """短静音（< 1.5s）不删除，裁剪后不变"""
        samples = np.concatenate([_sine(1.0), _silence(1.0), _sine(1.0)])  # 静音 1.0s < 1.5s
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert data["cropped_duration"] == pytest.approx(3.0, abs=0.1)
        assert len(data["segments"]) == 1

    def test_no_silence_unchanged(self, tmp_path):
        """无静音段的 WAV → 裁剪后不变，单段全量"""
        samples = _sine(2.0)
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)
        cropped_samples, sr = _read_wav_samples(cropped_wav)
        assert sr == SAMPLERATE
        assert len(cropped_samples) == pytest.approx(len(samples), abs=SAMPLERATE // 10)

        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["segments"]) == 1
        assert data["segments"][0]["original_start"] == pytest.approx(0.0, abs=0.05)
        assert data["segments"][0]["original_end"] == pytest.approx(2.0, abs=0.05)
        assert data["segments"][0]["cropped_start"] == pytest.approx(0.0, abs=0.05)
        assert data["segments"][0]["cropped_end"] == pytest.approx(2.0, abs=0.05)

    def test_all_silence_keeps_placeholder(self, tmp_path):
        """全静音 WAV → 裁剪后保留 0.5s 占位"""
        samples = _silence(3.0)
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)
        cropped_samples, _ = _read_wav_samples(cropped_wav)
        # 0.5s = 8000 samples @ 16kHz
        assert len(cropped_samples) == pytest.approx(8000, abs=SAMPLERATE // 10)

        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert data["cropped_duration"] == pytest.approx(0.5, abs=0.05)
        assert len(data["segments"]) == 1
        assert data["segments"][0]["original_start"] == pytest.approx(0.0, abs=0.05)
        assert data["segments"][0]["original_end"] == pytest.approx(0.5, abs=0.05)


# ============================ audio_segments.json 格式测试 ============================


class TestSegmentsJsonFormat:
    def test_segments_json_structure(self, tmp_path):
        """audio_segments.json 包含 original_duration / cropped_duration / segments"""
        samples = np.concatenate([_sine(1.0), _silence(2.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        _, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)

        assert set(data.keys()) == {"original_duration", "cropped_duration", "segments"}
        assert isinstance(data["original_duration"], float)
        assert isinstance(data["cropped_duration"], float)
        assert isinstance(data["segments"], list)
        for seg in data["segments"]:
            assert set(seg.keys()) == {
                "original_start", "original_end", "cropped_start", "cropped_end"
            }
            # 裁剪后段连续且非负
            assert seg["cropped_end"] >= seg["cropped_start"]
            assert seg["original_end"] >= seg["original_start"]

    def test_cropped_segments_are_contiguous(self, tmp_path):
        """裁剪后各段 cropped_start/end 首尾相连"""
        samples = np.concatenate([
            _sine(1.0), _silence(2.0), _sine(1.0), _silence(2.0), _sine(1.0)
        ])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        _, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        segs = data["segments"]
        assert len(segs) == 3
        for i in range(1, len(segs)):
            assert segs[i]["cropped_start"] == pytest.approx(segs[i - 1]["cropped_end"], abs=1e-4)
        # 最后一段 cropped_end == cropped_duration
        assert segs[-1]["cropped_end"] == pytest.approx(data["cropped_duration"], abs=1e-4)


# ============================ 键鼠声处理测试 ============================


class TestKeyMouseProtection:
    def test_protected_interval_not_deleted(self, tmp_path):
        """键鼠事件 ±100ms 区间内的静音不被删除（即使周围静音 > 1.5s 被删）"""
        # [信号 1s + 静音 4s + 信号 1s]，键鼠事件在 t=3.0s（静音中部）
        samples = np.concatenate([_sine(1.0), _silence(4.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)
        events_path = tmp_path / "events.jsonl"
        _write_events(events_path, [_make_event_at(3.0, KIND_KEYBOARD_INPUT)])

        _, seg_json = trim_audio(wav_path, events_path)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)

        # 受保护区 [2.9, 3.1] 把 4s 静音切成两段各 1.9s（均 > 1.5s 被删）
        # 保留段：(0,1) + (2.9,3.1) + (5,6) → 3 段
        assert len(data["segments"]) == 3
        # 中间段是受保护区
        mid = data["segments"][1]
        assert mid["original_start"] == pytest.approx(2.9, abs=0.05)
        assert mid["original_end"] == pytest.approx(3.1, abs=0.05)
        assert data["cropped_duration"] == pytest.approx(2.2, abs=0.15)

    def test_protected_silence_with_mouse_click(self, tmp_path):
        """mouse_click 事件同样触发受保护"""
        samples = np.concatenate([_sine(1.0), _silence(3.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)
        events_path = tmp_path / "events.jsonl"
        _write_events(events_path, [_make_event_at(2.5, KIND_MOUSE_CLICK)])

        _, seg_json = trim_audio(wav_path, events_path)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        # 受保护 [2.4, 2.6]：左侧静音 [1,2.4]=1.4s 不删，右侧 [2.6,4]=1.4s 不删
        # → 整段静音保留，1 段
        assert len(data["segments"]) == 1

    def test_transient_noise_attenuated(self, tmp_path):
        """受保护区内高 RMS 瞬态噪声被衰减（×0.1），非保护区不变"""
        # 1s 连续信号（amp 0.5），键鼠事件在 t=0.5s → 保护 [0.4, 0.6]
        samples = _sine(1.0, amp=0.5)
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)
        events_path = tmp_path / "events.jsonl"
        _write_events(events_path, [_make_event_at(0.5, KIND_KEYBOARD_INPUT)])

        cropped_wav, _ = trim_audio(wav_path, events_path)
        cropped_samples, _ = _read_wav_samples(cropped_wav)

        # 保护区内样本 [6400, 9600] 应被衰减
        orig_protected_max = int(np.max(np.abs(samples[6400:9600])))
        crop_protected_max = int(np.max(np.abs(cropped_samples[6400:9600])))
        assert orig_protected_max > 1000
        assert crop_protected_max < 0.2 * orig_protected_max  # 衰减到 ~10%

        # 非保护区样本 [0, 6400] 应保持不变
        assert np.array_equal(cropped_samples[0:6400], samples[0:6400])

    def test_no_events_no_protection(self, tmp_path):
        """无 events.jsonl 时无受保护区，纯按静音裁剪"""
        samples = np.concatenate([_sine(1.0), _silence(2.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        # 2s 静音删除 → 2 段
        assert len(data["segments"]) == 2

    def test_missing_events_file_skips_protection(self, tmp_path):
        """events.jsonl 路径不存在时跳过键鼠处理（不报错）"""
        samples = _sine(1.0)
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)
        events_path = tmp_path / "nonexistent.jsonl"

        cropped_wav, _ = trim_audio(wav_path, events_path)
        assert cropped_wav.exists()


# ============================ WAV 可读性测试 ============================


class TestCroppedWavReadable:
    def test_cropped_wav_readable_by_wave(self, tmp_path):
        """裁剪后 WAV 可被 wave 模块读回，格式正确"""
        samples = np.concatenate([_sine(1.0), _silence(2.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, _ = trim_audio(wav_path, None)
        with wave.open(str(cropped_wav), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == SAMPLERATE
            assert wf.getsampwidth() == 2  # int16
            n_frames = wf.getnframes()
            assert n_frames > 0
            data = wf.readframes(n_frames)
            assert len(data) == n_frames * 2  # int16 = 2 bytes/sample

    def test_cropped_wav_duration_correct(self, tmp_path):
        """裁剪后 WAV 帧数与 cropped_duration 一致"""
        samples = np.concatenate([_sine(2.0), _silence(3.0), _sine(2.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        cropped_samples, sr = _read_wav_samples(cropped_wav)
        expected_frames = int(data["cropped_duration"] * sr)
        assert len(cropped_samples) == pytest.approx(expected_frames, abs=SAMPLERATE // 10)

    def test_cropped_wav_sample_values_valid(self, tmp_path):
        """裁剪后样本值在 int16 范围内且非全零（保留段含信号）"""
        samples = np.concatenate([_sine(1.0), _silence(2.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        cropped_wav, _ = trim_audio(wav_path, None)
        cropped_samples, _ = _read_wav_samples(cropped_wav)
        assert cropped_samples.dtype == np.int16
        assert int(np.max(np.abs(cropped_samples))) > 0  # 有信号
        assert int(np.max(np.abs(cropped_samples))) <= INT16_PEAK

    def test_output_paths_in_same_dir(self, tmp_path):
        """默认输出到输入 WAV 同目录，文件名正确"""
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, _sine(1.0))

        cropped_wav, seg_json = trim_audio(wav_path, None)
        assert cropped_wav == tmp_path / "mic_cropped.wav"
        assert seg_json == tmp_path / "audio_segments.json"

    def test_output_dir_override(self, tmp_path):
        """output_dir 参数可指定输出目录"""
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, _sine(1.0))
        out_dir = tmp_path / "out"

        cropped_wav, seg_json = trim_audio(wav_path, None, output_dir=out_dir)
        assert cropped_wav == out_dir / "mic_cropped.wav"
        assert seg_json == out_dir / "audio_segments.json"
        assert cropped_wav.exists()


# ============================ 边界情况测试 ============================


class TestEdgeCases:
    def test_multiple_silence_segments(self, tmp_path):
        """多个静音段都被删除"""
        samples = np.concatenate([
            _sine(1.0), _silence(2.0),
            _sine(1.0), _silence(2.0),
            _sine(1.0), _silence(2.0),
            _sine(1.0),
        ])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        _, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        # 3 段静音删除 → 4 段保留
        assert len(data["segments"]) == 4
        assert data["cropped_duration"] == pytest.approx(4.0, abs=0.2)

    def test_silence_at_start_and_end(self, tmp_path):
        """首尾静音被删除"""
        samples = np.concatenate([_silence(2.0), _sine(2.0), _silence(2.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        _, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["segments"]) == 1
        assert data["segments"][0]["original_start"] == pytest.approx(2.0, abs=0.05)
        assert data["segments"][0]["original_end"] == pytest.approx(4.0, abs=0.05)
        assert data["cropped_duration"] == pytest.approx(2.0, abs=0.1)

    def test_low_amplitude_signal_not_treated_as_silence(self, tmp_path):
        """低幅度但不静音的信号不被删除（amp 0.01 → RMS_dB ≈ -43dB 仍静音；amp 0.05 → ≈ -29dB 非静音）"""
        # amp=0.05: RMS = 0.05*32768/√2 ≈ 1158, dB = 20*log10(1158/32768) ≈ -29 dB > -40
        samples = _sine(2.0, amp=0.05)
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)

        _, seg_json = trim_audio(wav_path, None)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["segments"]) == 1
        assert data["cropped_duration"] == pytest.approx(2.0, abs=0.1)

    def test_dbfs_calculation_correctness(self):
        """验证 dBFS 公式：20*log10(rms/32768)"""
        # RMS = 32768（满量程方波等效）→ 0 dBFS
        # 构造一个所有样本 = 32767 的"方波"，RMS ≈ 32767，dBFS ≈ 0
        samples = np.full(int(0.1 * SAMPLERATE), 32767, dtype=np.int16)
        rms_db = _compute_rms_db_per_frame(samples, SAMPLERATE, FRAME_DURATION)
        assert len(rms_db) == 1
        # RMS = 32767, dBFS = 20*log10(32767/32768) ≈ -0.0003 dB
        assert abs(rms_db[0] - 0.0) < 0.01

    def test_protected_interval_merged(self, tmp_path):
        """多个相近键鼠事件合并受保护区"""
        # 两个事件 t=2.0 和 t=2.15，受保护区 [1.9,2.1] 和 [2.05,2.25] 重叠 → 合并 [1.9, 2.25]
        samples = np.concatenate([_sine(1.0), _silence(3.0), _sine(1.0)])
        wav_path = tmp_path / "mic.wav"
        _write_wav(wav_path, samples)
        events_path = tmp_path / "events.jsonl"
        _write_events(events_path, [
            _make_event_at(2.0, KIND_MOUSE_CLICK),
            _make_event_at(2.15, KIND_KEYBOARD_INPUT),
        ])

        _, seg_json = trim_audio(wav_path, events_path)
        with seg_json.open(encoding="utf-8") as f:
            data = json.load(f)
        # 合并后受保护区 [1.9, 2.25] = 0.35s，左侧静音 [1,1.9]=0.9s 不删，右侧 [2.25,4]=1.75s 删
        # 保留段：(0,1) + (1.9, 2.25) + ... 实际右侧 1.75s>1.5 删除
        segs = data["segments"]
        # 至少保留受保护段
        protected_segs = [s for s in segs if s["original_start"] <= 2.0 <= s["original_end"]]
        assert len(protected_segs) == 1
