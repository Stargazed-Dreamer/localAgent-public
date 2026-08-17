"""L0 录制器音频裁剪模块：删除静音段 + 键鼠声瞬态衰减

设计（D027 / D033）：
- RMS 按帧分块计算（帧大小 0.1s），静音判定：RMS_dB < -40 且持续 > 1.5s
- 删除静音段，拼接保留段，记录 audio_segments.json 时间戳映射表
- 键鼠声可选处理：扫描 events.jsonl 的 mouse_click/keyboard_input 时间戳，
  标记 ±100ms 区间为受保护区（不删除），区间内 RMS 高于阈值的瞬态噪声衰减（×0.1）
- 纯 numpy + 标准库 wave/json，无新依赖
- WAV 格式：16kHz / mono / int16（与 AudioSensor 一致）

audio_segments.json 格式：
    {
      "original_duration": 62.5,
      "cropped_duration": 45.3,
      "segments": [
        {"original_start": 0.0, "original_end": 15.2,
         "cropped_start": 0.0, "cropped_end": 15.2},
        ...
      ]
    }
"""

import json
import logging
import math
import wave
from pathlib import Path

import numpy as np

from lib.recorder.types import KIND_KEYBOARD_INPUT, KIND_MOUSE_CLICK

logger = logging.getLogger(__name__)

# ============================ 裁剪参数（D027 / D033） ============================
# 默认阈值 -45dB（mic-2 修复：原 -40dB 过于激进，正常说话 -20~-30dB，麦克风偏弱时
# 可能落到 -38~-42dB 被误判为静音。改为 -45dB 给麦克风容差，用户可在 config.toml 调整）。
FRAME_DURATION = 0.1  # RMS 计算帧大小（秒）
DEFAULT_SILENCE_THRESHOLD_DB = -45.0  # 静音判定默认阈值（dBFS，相对 int16 满量程）
SILENCE_MIN_DURATION = 1.5  # 静音段最短持续时间（秒），超过此长度才删除
PROTECT_HALF_WINDOW = 0.1  # 键鼠事件 ±100ms 受保护区半窗
TRANSIENT_ATTENUATION = 0.1  # 瞬态噪声衰减系数
PLACEHOLDER_DURATION = 0.5  # 全静音时的最小占位时长（秒）
INT16_MAX = 32768  # int16 满量程（用于 dBFS 计算）
# 极小 dB 值，用于 RMS=0 时占位，确保 < 阈值
_SILENCE_DB_FLOOR = -120.0

# 向后兼容：保留旧常量名（= 默认阈值），仅供旧测试引用；新代码应通过 trim_audio 参数传入
SILENCE_THRESHOLD_DB = DEFAULT_SILENCE_THRESHOLD_DB
# 瞬态噪声判定阈值 = 当前裁剪阈值（非静音即视为瞬态噪声），由 trim_audio 内部计算


def trim_audio(
    wav_path: Path | str,
    events_jsonl_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    silence_threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
) -> tuple[Path, Path]:
    """裁剪音频静音段，生成裁剪后 WAV + audio_segments.json。

    Args:
        wav_path: 输入 WAV 文件路径（16kHz / mono / int16）
        events_jsonl_path: 事件流 JSONL 路径（用于键鼠声处理），None 则跳过
        output_dir: 输出目录，None 则与 wav_path 同目录
        silence_threshold_db: 静音判定阈值（dBFS），默认 -45dB。
            麦克风偏弱时建议 -50 ~ -45；噪音大时建议 -40 ~ -35。
            config.toml [recording.audio] silence_threshold_db 可调。

    Returns:
        (cropped_wav_path, audio_segments_json_path)
    """
    wav_path = Path(wav_path)
    events_jsonl_path = Path(events_jsonl_path) if events_jsonl_path is not None else None
    output_dir = Path(output_dir) if output_dir is not None else wav_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    cropped_wav_path = output_dir / f"{wav_path.stem}_cropped.wav"
    segments_json_path = output_dir / "audio_segments.json"

    # 瞬态噪声阈值 = 静音阈值（非静音即瞬态噪声）
    transient_threshold_db = silence_threshold_db

    # 1. 读取 WAV（16kHz / mono / int16）
    samples, samplerate = _read_wav(wav_path)
    original_duration = len(samples) / samplerate if samplerate > 0 else 0.0

    # 2. 按帧计算 RMS（dBFS）
    rms_db = _compute_rms_db_per_frame(samples, samplerate, FRAME_DURATION)

    # 3. 静音帧掩码
    silent_frame_mask = rms_db < silence_threshold_db

    # 4. 读取键鼠事件 → 受保护区段
    protected_intervals: list[tuple[float, float]] = []
    if events_jsonl_path is not None and events_jsonl_path.exists():
        timestamps = _read_event_timestamps(
            events_jsonl_path, {KIND_MOUSE_CLICK, KIND_KEYBOARD_INPUT}
        )
        protected_intervals = _build_protected_intervals(
            timestamps, PROTECT_HALF_WINDOW, original_duration
        )

    # 5. 帧级受保护掩码（受保护的帧即使静音也不删除）
    frame_size = max(1, int(FRAME_DURATION * samplerate))
    n_frames = len(rms_db)
    protected_frame_mask = _intervals_to_frame_mask(
        protected_intervals, n_frames, frame_size, samplerate
    )

    # 6. 可删除帧 = 静音 且 未受保护
    deletable_frame_mask = silent_frame_mask & ~protected_frame_mask

    # 7. 分组可删除帧为运行段，仅保留持续时间 > 1.5s 的作为删除段
    removed_segments = _find_removed_segments(
        deletable_frame_mask, frame_size, samplerate, SILENCE_MIN_DURATION
    )

    # 8. 保留段 = 删除段的补集
    kept_segments = _complement_segments(removed_segments, original_duration)

    # 9. 全静音占位：保留段为空时保留 0.5s 占位
    if not kept_segments:
        placeholder_end = min(PLACEHOLDER_DURATION, original_duration)
        kept_segments = [(0.0, placeholder_end)]
        logger.info("全静音 WAV，保留 %.2fs 占位", placeholder_end)

    # 10. 拼接保留段 + 应用瞬态衰减
    cropped_samples, segment_records = _build_cropped_samples(
        samples,
        samplerate,
        kept_segments,
        protected_intervals,
        rms_db,
        frame_size,
        transient_threshold_db,
    )

    cropped_duration = len(cropped_samples) / samplerate if samplerate > 0 else 0.0

    # 11. 写裁剪后 WAV
    _write_wav(cropped_wav_path, cropped_samples, samplerate)

    # 12. 写 audio_segments.json
    _write_segments_json(
        segments_json_path, segment_records, original_duration, cropped_duration
    )

    logger.info(
        "音频裁剪完成: 原始 %.2fs → 裁剪 %.2fs (%d 段), 输出 %s",
        original_duration, cropped_duration, len(segment_records), cropped_wav_path,
    )
    return cropped_wav_path, segments_json_path


# ============================ 内部工具函数 ============================


def _read_wav(wav_path: Path) -> tuple[np.ndarray, int]:
    """读取 WAV 文件，返回 (int16 样本数组, 采样率)。

    仅支持 mono / int16（与 AudioSensor 输出一致）。
    """
    with wave.open(str(wav_path), "rb") as wf:  # noqa: SIM115
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        samplerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2:
        raise ValueError(f"仅支持 int16 (sampwidth=2) WAV，当前 sampwidth={sampwidth}")
    if n_channels != 1:
        raise ValueError(f"仅支持 mono WAV，当前 channels={n_channels}")

    # <i2 = 小端 int16；astype(int16) 生成可写副本（frombuffer 返回只读数组）
    samples = np.frombuffer(raw, dtype="<i2").astype(np.int16)
    return samples, samplerate


def _compute_rms_db_per_frame(
    samples: np.ndarray, samplerate: int, frame_duration: float
) -> np.ndarray:
    """按帧分块计算 RMS（dBFS）。

    帧大小 = frame_duration * samplerate，尾部不足一帧的样本忽略。
    RMS = sqrt(mean(samples^2))，dBFS = 20*log10(RMS / 32768)。
    RMS=0（纯静音）→ 返回 -120 dB（确保判为静音）。

    Returns:
        rms_db: shape (n_frames,)，每帧的 RMS dBFS 值
    """
    if len(samples) == 0:
        return np.zeros(0, dtype=np.float64)

    frame_size = max(1, int(frame_duration * samplerate))
    n_samples = len(samples)
    n_frames = n_samples // frame_size
    usable = n_frames * frame_size
    if usable == 0:
        # 样本不足一帧：用全部样本算一个 RMS
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return np.array([_scalar_rms_to_dbfs(rms)], dtype=np.float64)

    frames = samples[:usable].astype(np.float64).reshape(n_frames, frame_size)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    return _rms_to_dbfs(rms)


def _rms_to_dbfs(rms: np.ndarray) -> np.ndarray:
    """RMS 数组转 dBFS（相对 int16 满量程 32768）。RMS<=0 → -120 dB。"""
    arr = np.asarray(rms, dtype=np.float64)
    safe = np.where(arr > 0, arr, 1.0)
    db = 20.0 * np.log10(safe / INT16_MAX)
    return np.where(arr > 0, db, _SILENCE_DB_FLOOR)


def _scalar_rms_to_dbfs(rms: float) -> float:
    """标量 RMS 转 dBFS。RMS<=0 → -120 dB。"""
    if rms <= 0:
        return _SILENCE_DB_FLOOR
    return 20.0 * math.log10(rms / INT16_MAX)


def _read_event_timestamps(events_jsonl_path: Path, kinds: set[str]) -> list[float]:
    """读取 events.jsonl 中指定 kind 的事件 timestamp（秒，从录制开始算）。"""
    timestamps: list[float] = []
    with events_jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") in kinds:
                ts = event.get("timestamp")
                if isinstance(ts, int | float):
                    timestamps.append(float(ts))
    return timestamps


def _build_protected_intervals(
    timestamps: list[float], half_window: float, total_duration: float
) -> list[tuple[float, float]]:
    """构建受保护区段列表 [(start, end), ...]，合并重叠区间。

    每个时间戳扩展为 [t-half_window, t+half_window]，并裁剪到 [0, total_duration]。
    """
    if not timestamps:
        return []
    raw = [
        (max(0.0, t - half_window), min(total_duration, t + half_window))
        for t in timestamps
    ]
    raw.sort()
    merged = [raw[0]]
    for start, end in raw[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _intervals_to_frame_mask(
    intervals: list[tuple[float, float]], n_frames: int, frame_size: int, samplerate: int
) -> np.ndarray:
    """将时间区间列表转为帧级布尔掩码（True = 该帧受保护）。

    一个帧只要与任一区间有重叠即标记为受保护。
    """
    mask = np.zeros(n_frames, dtype=bool)
    if not intervals or n_frames == 0:
        return mask
    for start, end in intervals:
        start_frame = max(0, int(start * samplerate / frame_size))
        end_frame = min(n_frames, int(math.ceil(end * samplerate / frame_size)))
        if end_frame > start_frame:
            mask[start_frame:end_frame] = True
    return mask


def _find_removed_segments(
    deletable_frame_mask: np.ndarray,
    frame_size: int,
    samplerate: int,
    min_duration: float,
) -> list[tuple[float, float]]:
    """从可删除帧掩码中找出持续时间 > min_duration 的删除段。

    Returns:
        [(start_sec, end_sec), ...] 删除段列表（秒）
    """
    if len(deletable_frame_mask) == 0:
        return []
    removed: list[tuple[float, float]] = []
    # 找连续 True 段：diff 在起点为 +1，终点为 -1
    diff = np.diff(deletable_frame_mask.astype(np.int8), prepend=0, append=0)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    for s, e in zip(starts, ends, strict=True):
        duration = (e - s) * frame_size / samplerate
        if duration > min_duration:
            removed.append((s * frame_size / samplerate, e * frame_size / samplerate))
    return removed


def _complement_segments(
    removed: list[tuple[float, float]], total_duration: float
) -> list[tuple[float, float]]:
    """删除段的补集 = 保留段 [(start, end), ...]。"""
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in removed:
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration:
        kept.append((cursor, total_duration))
    return kept


def _build_cropped_samples(
    samples: np.ndarray,
    samplerate: int,
    kept_segments: list[tuple[float, float]],
    protected_intervals: list[tuple[float, float]],
    rms_db: np.ndarray,
    frame_size: int,
    transient_threshold_db: float,
) -> tuple[np.ndarray, list[dict]]:
    """拼接保留段，应用瞬态衰减，返回 (裁剪后样本, 段映射记录)。

    段映射记录格式：{original_start, original_end, cropped_start, cropped_end}
    """
    pieces: list[np.ndarray] = []
    records: list[dict] = []
    cropped_cursor = 0.0
    total = len(samples)

    for orig_start, orig_end in kept_segments:
        s_idx = max(0, min(int(orig_start * samplerate), total))
        e_idx = max(s_idx, min(int(math.ceil(orig_end * samplerate)), total))
        chunk = samples[s_idx:e_idx].copy()

        # 受保护区段内瞬态噪声衰减（×0.1）
        if protected_intervals and len(chunk) > 0:
            chunk = _apply_transient_attenuation(
                chunk, s_idx, samplerate, protected_intervals, rms_db, frame_size,
                transient_threshold_db,
            )

        pieces.append(chunk)
        seg_duration = (e_idx - s_idx) / samplerate
        records.append(
            {
                "original_start": round(float(orig_start), 6),
                "original_end": round(float(orig_end), 6),
                "cropped_start": round(float(cropped_cursor), 6),
                "cropped_end": round(float(cropped_cursor + seg_duration), 6),
            }
        )
        cropped_cursor += seg_duration

    cropped = np.concatenate(pieces).astype(np.int16) if pieces else np.zeros(0, dtype=np.int16)
    return cropped, records


def _apply_transient_attenuation(
    chunk: np.ndarray,
    chunk_start_idx: int,
    samplerate: int,
    protected_intervals: list[tuple[float, float]],
    rms_db: np.ndarray,
    frame_size: int,
    transient_threshold_db: float,
) -> np.ndarray:
    """对 chunk 中受保护区间内的瞬态噪声帧衰减 ×0.1。

    判定：样本在受保护区间内，且所属帧 RMS_dB >= 阈值（非静音=瞬态噪声）→ ×0.1。
    受保护区间内的静音帧保持不变（无瞬态噪声可衰减）。
    """
    chunk_len = len(chunk)
    if chunk_len == 0:
        return chunk

    # 样本级受保护掩码
    sample_protected = np.zeros(chunk_len, dtype=bool)
    for start, end in protected_intervals:
        s_sample = max(0, int(start * samplerate) - chunk_start_idx)
        e_sample = min(chunk_len, int(math.ceil(end * samplerate)) - chunk_start_idx)
        if e_sample > s_sample:
            sample_protected[s_sample:e_sample] = True

    if not sample_protected.any():
        return chunk

    # 样本级"高 RMS"（非静音）掩码：基于所属帧的 RMS
    abs_indices = np.arange(chunk_len) + chunk_start_idx
    frame_indices = abs_indices // frame_size
    valid = frame_indices < len(rms_db)
    loud_at_sample = np.zeros(chunk_len, dtype=bool)
    loud_at_sample[valid] = rms_db[frame_indices[valid]] >= transient_threshold_db

    attenuate_mask = sample_protected & loud_at_sample
    if not attenuate_mask.any():
        return chunk

    out = chunk.astype(np.float64)
    out[attenuate_mask] *= TRANSIENT_ATTENUATION
    out = np.clip(out, -32768, 32767)
    return out.astype(np.int16)


def _write_wav(wav_path: Path, samples: np.ndarray, samplerate: int) -> None:
    """写 WAV 文件（mono / int16）。"""
    with wave.open(str(wav_path), "wb") as wf:  # noqa: SIM115
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(samplerate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _write_segments_json(
    json_path: Path,
    segment_records: list[dict],
    original_duration: float,
    cropped_duration: float,
) -> None:
    """写 audio_segments.json 时间戳映射表。"""
    data = {
        "original_duration": round(float(original_duration), 6),
        "cropped_duration": round(float(cropped_duration), 6),
        "segments": segment_records,
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
