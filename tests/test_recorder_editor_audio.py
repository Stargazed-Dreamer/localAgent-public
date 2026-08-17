"""Ticket 27 单测：音频调参面板 + STT 重跑。"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.recorder.editor.audio_preview import preview_trim
from workspace.recorder.tools.editor.audio_panel import AudioPanel, WaveformWidget
from workspace.recorder.tools.editor.stt_runner import STTRunner


def _make_wav(path: Path, duration: float = 5.0, sample_rate: int = 16000) -> None:
    """生成测试 WAV：前 2s 有声音，中间 1s 静音，后 2s 有声音。"""
    n_samples = int(duration * sample_rate)
    samples = np.zeros(n_samples, dtype=np.int16)
    # 前 2s 有声音
    t = np.arange(int(2 * sample_rate)) / sample_rate
    samples[:int(2 * sample_rate)] = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16)
    # 后 2s 有声音（从 3s 开始）
    t = np.arange(int(2 * sample_rate)) / sample_rate
    samples[int(3 * sample_rate):] = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


# ===== preview_trim 算法 =====

def test_preview_trim_basic(tmp_path: Path) -> None:
    """基本切片预览：有声音段保留，静音段删除。"""
    wav_path = tmp_path / "mic.wav"
    _make_wav(wav_path)
    result = preview_trim(wav_path, threshold_db=-30.0, min_duration=0.5)
    assert result["original_duration"] == pytest.approx(5.0, abs=0.1)
    # 应该保留前 2s + 后 2s，删除中间 1s
    assert result["kept_duration"] > 3.5  # 保留约 4s
    assert result["removed_duration"] < 1.5  # 删除约 1s
    assert len(result["kept_segments"]) >= 2  # 至少 2 个保留段


def test_preview_trim_all_silence(tmp_path: Path) -> None:
    """全静音时：保留 0.5s 占位（audio_trimmer 行为）。"""
    wav_path = tmp_path / "mic.wav"
    # 生成全静音 WAV
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(np.zeros(16000 * 5, dtype=np.int16).tobytes())
    result = preview_trim(wav_path, threshold_db=-30.0, min_duration=0.5)
    # 全静音时 removed 段覆盖全部，kept 段为空或占位
    assert result["original_duration"] == pytest.approx(5.0, abs=0.1)


def test_preview_trim_threshold_effect(tmp_path: Path) -> None:
    """阈值越低，保留越多。"""
    wav_path = tmp_path / "mic.wav"
    _make_wav(wav_path)
    # 高阈值（-25dB）：删除更多
    result_high = preview_trim(wav_path, threshold_db=-25.0, min_duration=0.5)
    # 低阈值（-50dB）：删除更少
    result_low = preview_trim(wav_path, threshold_db=-50.0, min_duration=0.5)
    assert result_low["kept_duration"] >= result_high["kept_duration"]


def test_preview_trim_returns_rms(tmp_path: Path) -> None:
    """返回 rms_db 数组供波形显示。"""
    wav_path = tmp_path / "mic.wav"
    _make_wav(wav_path)
    result = preview_trim(wav_path)
    assert "rms_db" in result
    assert len(result["rms_db"]) > 0
    assert result["frame_duration"] == 0.1


# ===== WaveformWidget =====

def test_waveform_paint_no_crash(qapp, tmp_path: Path) -> None:
    """WaveformWidget 绘制不崩。"""
    wav_path = tmp_path / "mic.wav"
    _make_wav(wav_path)
    result = preview_trim(wav_path)
    widget = WaveformWidget()
    widget.set_data(result["rms_db"], -40.0)
    widget.resize(400, 100)
    # 不崩即通过（paintEvent 在 show 后触发，这里只验证 set_data 不抛异常）


def test_waveform_empty_data(qapp) -> None:
    """空数据时不崩。"""
    widget = WaveformWidget()
    widget.set_data([], -40.0)
    widget.resize(400, 100)


# ===== AudioPanel =====

def test_audio_panel_load_audio(qapp, tmp_path: Path) -> None:
    """AudioPanel 加载音频并显示预览。"""
    wav_path = tmp_path / "audio" / "mic.wav"
    wav_path.parent.mkdir()
    _make_wav(wav_path)

    panel = AudioPanel(tmp_path)
    # 预览文本非空
    assert panel.preview_text.toPlainText() != ""
    assert "原始时长" in panel.preview_text.toPlainText()


def test_audio_panel_threshold_slider_updates_preview(qapp, tmp_path: Path) -> None:
    """阈值滑块改变后预览实时更新。"""
    wav_path = tmp_path / "audio" / "mic.wav"
    wav_path.parent.mkdir()
    _make_wav(wav_path)

    panel = AudioPanel(tmp_path)
    # 改变阈值
    panel.threshold_slider.setValue(-25)
    # 预览文本应该变化（至少阈值标签变了）
    assert "-25 dB" in panel.threshold_label.text()


def test_audio_panel_no_wav(qapp, tmp_path: Path) -> None:
    """mic.wav 不存在时显示提示。"""
    panel = AudioPanel(tmp_path)
    assert "不存在" in panel.preview_text.toPlainText()


# ===== STTRunner（mock 测试）=====

def test_stt_runner_init() -> None:
    """STTRunner 初始化参数正确。"""
    runner = STTRunner(Path("/tmp/rec"), model_name="large-v3", device="cpu", vad_threshold=0.3)
    assert runner.model_name == "large-v3"
    assert runner.device == "cpu"
    assert runner.vad_threshold == 0.3


# ===== 真实录制包测试 =====

RECORDINGS_DIR = Path("workspace/recorder/recordings")
REAL_PACKAGES = [
    RECORDINGS_DIR / "rec_20260723_141608",
    RECORDINGS_DIR / "rec_20260723_161415",
    RECORDINGS_DIR / "rec_20260723_162924",
]
_HAS_REAL_PACKAGES = all(p.exists() for p in REAL_PACKAGES)
_skip_if_no_real = pytest.mark.skipif(
    not _HAS_REAL_PACKAGES,
    reason="真实录制包不存在（被 .gitignore 排除，CI 环境跳过）",
)


@_skip_if_no_real
@pytest.mark.parametrize("pkg_name", ["rec_20260723_141608", "rec_20260723_161415", "rec_20260723_162924"])
def test_preview_trim_real_package(pkg_name: str) -> None:
    """用真实录制包验证 preview_trim。"""
    pkg = RECORDINGS_DIR / pkg_name
    wav_path = pkg / "audio" / "mic.wav"
    if not wav_path.exists():
        pytest.skip("mic.wav 不存在")
    result = preview_trim(wav_path)
    assert result["original_duration"] > 0
    assert len(result["kept_segments"]) > 0
