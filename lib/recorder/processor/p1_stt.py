"""P1 STT 转换器（spec-l1.md / 04-stt-pipeline.md / Ticket 17）

输入：原始 mic.wav + 用户配的 VAD 阈值
输出：transcript.json（含词级时间戳，04-stt-pipeline.md 定义的格式）

P1 入口：
- run_p1(package_root, vad_threshold, language, model_name, model_dir, ...)
  读 audio/mic.wav → wave 模块转 numpy 数组 → transcribe_with_fallback → 写 transcript.json

transcript.json 格式（04-stt-pipeline.md 第五节）：
    [
      {
        "line": 1,
        "start": 1.23,           # 起始秒（float）
        "end": 4.50,             # 结束秒（float）
        "text": "现在我要点击登录按钮"
      },
      ...
    ]

依赖：
- faster-whisper（已装）：WhisperModel 转写
- pyloudnorm（Ticket 17 装）：LUFS 归一化
- scipy（Ticket 17 装）：pyloudnorm 依赖
- numpy（已装）：音频数组
"""

import json
import logging
import wave
from pathlib import Path
from typing import Any

import numpy as np

from lib.recorder.processor.stt_engine import WhisperTool
from lib.recorder.processor.stt_retry import (
    LUFS_ATTEMPT,
    LUFS_STEP,
    TARGET_LUFS,
    transcribe_with_fallback,
)

_log = logging.getLogger("p1_stt")

# 默认参数（可被 config.toml [recording.stt] 覆盖）
DEFAULT_MODEL = "large-v3"          # D037：默认 large-v3
DEFAULT_LANGUAGE = "zh"             # 默认中文
DEFAULT_VAD_THRESHOLD = 0.5         # faster-whisper VAD 默认阈值
DEFAULT_MAX_CHARS_PER_LINE = 40     # 单条字幕最大字符数
DEFAULT_MIN_DURATION_PER_LINE = 1.5 # 单条字幕最短持续时间秒
DEFAULT_TARGET_LUFS = TARGET_LUFS   # LUFS 归一化初始目标响度
DEFAULT_LUFS_ATTEMPT = LUFS_ATTEMPT # LUFS 归一化最大重试次数
DEFAULT_LUFS_STEP = LUFS_STEP       # 每次 LUFS 目标响度增量


def load_wav_as_array(wav_path: Path) -> tuple[np.ndarray, int]:
    """用 wave 模块读 WAV 文件为 numpy 数组。

    Args:
        wav_path: WAV 文件路径

    Returns:
        (audio_array, sample_rate)：audio_array 是 float32, mono, 16kHz

    Raises:
        FileNotFoundError: WAV 文件不存在
        ValueError: WAV 文件格式不支持
    """
    if not wav_path.exists():
        raise FileNotFoundError(f"WAV 文件不存在：{wav_path}")

    with wave.open(str(wav_path), "rb") as wav:
        n_channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        n_frames = wav.getnframes()
        raw_data = wav.readframes(n_frames)

    # 支持 int16 PCM（L0 AudioSensor 输出格式）
    if sample_width == 2:
        audio_array = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        # int32 PCM
        audio_array = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的 WAV 采样宽度：{sample_width} 字节（仅支持 int16/int32 PCM）")

    # 多声道取第一声道（L0 AudioSensor 输出 mono，但兼容多声道输入）
    if n_channels > 1:
        audio_array = audio_array[::n_channels]

    return audio_array, sample_rate


def write_transcript_json(
    transcript: list[dict[str, Any]],
    package_root: Path,
) -> Path:
    """把转写结果写入录制包的 transcript.json。

    Args:
        transcript: 转写结果 list[dict]
        package_root: 录制包根目录

    Returns:
        transcript.json 文件路径
    """
    transcript_path = package_root / "transcript.json"
    transcript_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return transcript_path


def run_p1(
    package_root: Path,
    vad_threshold: float | None = DEFAULT_VAD_THRESHOLD,
    language: str = DEFAULT_LANGUAGE,
    model_name: str = DEFAULT_MODEL,
    model_dir: str | None = None,
    device: str | None = None,
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE,
    min_duration_per_line: float = DEFAULT_MIN_DURATION_PER_LINE,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    lufs_attempt: int = DEFAULT_LUFS_ATTEMPT,
    lufs_step: float = DEFAULT_LUFS_STEP,
    transcriber: WhisperTool | None = None,
) -> list[dict[str, Any]]:
    """P1 STT 转换器入口：读 audio/mic.wav → 转写 → 写 transcript.json。

    Args:
        package_root: 录制包根目录（含 audio/mic.wav）
        vad_threshold: VAD 阈值（0-1，None 用 faster-whisper 默认 0.5）
        language: 语言代码（'zh' / 'en' / 'auto' 等）
        model_name: 模型名（如 'large-v3' / 'large-v3-turbo'）
        model_dir: 模型存放目录，None 用 huggingface 默认缓存
        device: 'cpu' 或 'cuda'，None 用默认 cuda
        max_chars_per_line: 单条字幕最大字符数
        min_duration_per_line: 单条字幕最短持续时间秒
        target_lufs: LUFS 归一化初始目标响度
        lufs_attempt: LUFS 归一化最大重试次数
        lufs_step: 每次 LUFS 目标响度增量
        transcriber: 已加载模型的 WhisperTool 实例（测试注入用），None 时新建并加载模型

    Returns:
        转写结果 list[dict]

    Raises:
        FileNotFoundError: audio/mic.wav 不存在
    """
    audio_path = package_root / "audio" / "mic.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"audio/mic.wav 不存在：{audio_path}")

    # 读 WAV 为 numpy 数组
    audio_array, sample_rate = load_wav_as_array(audio_path)
    _log.info(f"读取 WAV: {len(audio_array) / sample_rate:.2f}s / {sample_rate}Hz / {len(audio_array)} samples")

    # 加载模型（若未注入 transcriber）
    owns_transcriber = transcriber is None
    if owns_transcriber:
        transcriber = WhisperTool(model_dir=model_dir)
        transcriber.load(model_name, device=device)

    try:
        # 转写 + 重试策略
        transcript = transcribe_with_fallback(
            transcriber=transcriber,
            model_name=model_name,
            audio_array=audio_array,
            language=language,
            sample_rate=sample_rate,
            vad_threshold=vad_threshold,
            target_lufs=target_lufs,
            lufs_attempt=lufs_attempt,
            lufs_step=lufs_step,
        )
    finally:
        # 卸载自建的 transcriber（注入的不卸载，由调用方管理）
        if owns_transcriber and transcriber.is_model_loaded(model_name):
            try:
                transcriber.unload(model_name)
            except Exception as e:
                _log.warning(f"卸载模型失败: {e}")

    if transcript is None:
        # 全部归一化尝试均无效，写空 transcript
        transcript = []
        _log.warning("STT 转写失败（全部归一化尝试均无效），transcript.json 写入空列表")

    write_transcript_json(transcript, package_root)
    _log.info(f"transcript.json 写入完成：{len(transcript)} 条字幕")
    return transcript
