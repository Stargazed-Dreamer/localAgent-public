"""音频切片预览算法（复用 audio_trimmer.py 的 RMS + 静音段识别，但不写文件）。

供 L3 编辑器 AudioPanel 实时预览用：用户调阈值滑块时，preview_trim 返回保留段/删除段列表。
"""

from __future__ import annotations

from pathlib import Path

from lib.recorder.audio_trimmer import (
    FRAME_DURATION,
    SILENCE_MIN_DURATION,
    _complement_segments,
    _compute_rms_db_per_frame,
    _find_removed_segments,
    _read_wav,
)


def preview_trim(
    wav_path: Path,
    threshold_db: float = -40.0,
    min_duration: float = SILENCE_MIN_DURATION,
) -> dict:
    """预览音频裁剪结果（不写文件，只返回切片信息）。

    Args:
        wav_path: mic.wav 路径
        threshold_db: 静音阈值（dBFS）
        min_duration: 最小静音段时长（秒）

    Returns:
        dict: {
            "original_duration": float,
            "kept_duration": float,
            "removed_duration": float,
            "kept_segments": list[dict],  # [{original_start, original_end}, ...]
            "removed_segments": list[dict],
            "rms_db": list[float],  # 每帧 RMS dBFS 值（供波形显示）
            "frame_duration": float,
        }
    """
    samples, samplerate = _read_wav(wav_path)
    original_duration = len(samples) / samplerate

    # 计算 RMS
    rms_db = _compute_rms_db_per_frame(samples, samplerate, FRAME_DURATION)
    silent_mask = rms_db < threshold_db

    # 找删除段（不应用键鼠声保护，预览用简化版）
    frame_size = max(1, int(FRAME_DURATION * samplerate))
    removed_segments = _find_removed_segments(
        silent_mask, frame_size, samplerate, min_duration
    )
    kept_segments = _complement_segments(removed_segments, original_duration)

    kept_duration = sum(e - s for s, e in kept_segments)
    removed_duration = sum(e - s for s, e in removed_segments)

    return {
        "original_duration": original_duration,
        "kept_duration": kept_duration,
        "removed_duration": removed_duration,
        "kept_segments": [{"original_start": s, "original_end": e} for s, e in kept_segments],
        "removed_segments": [{"original_start": s, "original_end": e} for s, e in removed_segments],
        "rms_db": rms_db.tolist(),
        "frame_duration": FRAME_DURATION,
    }
