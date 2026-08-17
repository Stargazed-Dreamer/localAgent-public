"""STT 重试策略（照抄 stt-main main.py:86-244，Ticket 17）

源码：E:\\<data_drive>:\<working_root>\\- 工作中项目\\视频配字幕\\stt-main\\main.py

照抄内容：
- normalize_lufs：LUFS 归一化（pyloudnorm）
- normalize_percentile：百分位归一化
- is_junk_text：垃圾文本检测（JUNK_PHRASES 占比 > JUNK_THRESHOLD=0.3 判定）
- transcribe_with_fallback：渐进式归一化重试

照抄时改动（最小化）：
- 移除 normalize_max_peak（stt-main 已注释掉，不照抄）
- 移除 load_audio（P1 入口用 wave 模块读 WAV，不需要 PyAV）
- 移除 print 日志（用 logging）
- 常量改为模块级（JUNK_PHRASES / JUNK_THRESHOLD / TARGET_LUFS / LUFS_ATTEMPT / LUFS_STEP）
- transcribe_with_fallback 接受 transcriber + model_name + audio_array + language + sample_rate
- _do_transcribe 返回 list[dict]（json 输出），不是 srt 字符串
- is_junk_text 接受 list[dict]（json 输出），不是 srt 字符串

重试策略（D038）：
1. 原始音频转写 → is_junk_text 检测垃圾文本占比
2. LUFS 归一化重试（最多 4 次，每次 +3.0 LUFS，初始 -15.0 → -12 → -9 → -6）
3. 25% 分位归一化（LUFS 全失败后）
4. 全部失败 → 跳过并告警，返回 None

原作者所有权：本文件照抄自 stt-main 项目（main.py），原作者所有权保留。
"""

import logging
import time
from typing import Any

import numpy as np
import pyloudnorm as pyln

# ========== 默认参数（照抄 stt-main config.py） ==========
# faster-whisper 在音频过小时会持续输出以下垃圾语句
JUNK_PHRASES: list[str] = [
    "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
    "欢迎订阅 转发 打赏支持明镜与点点栏目",
    "中文简体",
]
JUNK_THRESHOLD = 0.3      # 垃圾文本字符占比超过此阈值时触发归一化
TARGET_LUFS = -15.0       # LUFS 归一化的初始目标响度
LUFS_ATTEMPT = 4          # LUFS 归一化最大重试次数
LUFS_STEP = 3.0           # 每次 LUFS 归一化目标响度增量

# 25% 分位归一化参数
PERCENTILE_NORMALIZE = 0.25

_log = logging.getLogger("stt_retry")


def normalize_lufs(
    audio_array: np.ndarray,
    sample_rate: int,
    target_lufs: float = TARGET_LUFS,
) -> np.ndarray:
    """将音频归一化到目标 LUFS（照抄 stt-main normalize_lufs）。

    Args:
        audio_array: numpy 数组（float32, mono）
        sample_rate: 采样率
        target_lufs: 目标 LUFS 响度

    Returns:
        归一化后的音频数组（float32）；归一化失败时返回原数组
    """
    try:
        meter = pyln.Meter(sample_rate)
        current_lufs = meter.integrated_loudness(audio_array)
        if current_lufs == -float("inf") or np.isnan(current_lufs) or current_lufs < -70:
            _log.warning("音频响度极低，无法测量 LUFS，跳过 LUFS 归一化")
            return audio_array
        normalized = pyln.normalize.loudness(audio_array, current_lufs, target_lufs)
        if np.any(np.isnan(normalized)) or np.any(np.isinf(normalized)):
            _log.warning("LUFS 归一化产生异常值，跳过")
            return audio_array
        return normalized.astype(np.float32)
    except Exception as e:
        _log.error(f"LUFS 归一化失败: {e}")
        return audio_array


def normalize_percentile(
    audio_array: np.ndarray,
    percentile: float = PERCENTILE_NORMALIZE,
) -> np.ndarray:
    """归一化使指定百分比的采样点达到 1.0（照抄 stt-main normalize_percentile）。

    Args:
        audio_array: numpy 数组
        percentile: 百分比（0.25 = 25% 分位）

    Returns:
        归一化后的音频数组（float32，允许裁剪）
    """
    abs_values = np.abs(audio_array)
    threshold = np.percentile(abs_values, (1 - percentile) * 100)
    if threshold == 0:
        return audio_array
    return (audio_array / threshold).astype(np.float32)


def is_junk_text(
    transcript: list[dict[str, Any]] | None,
    threshold: float = JUNK_THRESHOLD,
) -> bool:
    """检测转写内容是否为垃圾文本（音量过低的误识别结果）。

    照抄 stt-main is_junk_text，改为接受 list[dict]（json 输出）而非 srt 字符串。

    Args:
        transcript: WhisperTool 返回的 list[dict]（每项含 text 字段），None 视为非垃圾
        threshold: 垃圾文本字符占比阈值（默认 0.3）

    Returns:
        True 如果垃圾短语字符占比 > threshold
    """
    if not transcript:
        return False

    text_lines = [item.get("text", "") for item in transcript if item.get("text")]
    if not text_lines:
        return False

    all_text = "".join(text_lines)
    total_chars = len(all_text)
    if total_chars == 0:
        return False

    cleaned_text = all_text
    # 按短语长度降序排序（先匹配长短语避免短短语误删）
    sorted_phrases = sorted(JUNK_PHRASES, key=len, reverse=True)
    for phrase in sorted_phrases:
        cleaned_text = cleaned_text.replace(phrase, "")

    removed_chars = total_chars - len(cleaned_text)
    junk_ratio = removed_chars / total_chars
    return junk_ratio > threshold


def _do_transcribe(
    transcriber: Any,
    model_name: str,
    audio_array: np.ndarray,
    language: str,
    vad_threshold: float | None,
) -> list[dict[str, Any]] | None:
    """执行单次转录，返回 list[dict]（json 输出）。

    照抄 stt-main _do_transcribe，简化为同步等待（P1 STT 本身就是离线处理）。

    Args:
        transcriber: WhisperTool 实例
        model_name: 模型名
        audio_array: numpy 数组
        language: 语言代码
        vad_threshold: VAD 阈值

    Returns:
        转写结果 list[dict]；出错或无结果时返回 None
    """
    try:
        task_id = transcriber.process_array(
            model_name=model_name,
            audio_array=audio_array,
            language=language,
            vad_threshold=vad_threshold,
        )
        while True:
            progress, result = transcriber.get_progress(task_id)
            if progress == -1:
                try:
                    transcriber.get_error(task_id)
                except Exception as e:
                    _log.error(f"识别出错: {e}")
                return None
            elif progress == 1.0:
                return result
            else:
                _log.info(f"识别进度: {progress * 100:.1f}%")
                time.sleep(0.5)
    except Exception as e:
        _log.error(f"识别时发生错误: {e}")
        return None


def transcribe_with_fallback(
    transcriber: Any,
    model_name: str,
    audio_array: np.ndarray,
    language: str,
    sample_rate: int = 16000,
    vad_threshold: float | None = None,
    target_lufs: float = TARGET_LUFS,
    lufs_attempt: int = LUFS_ATTEMPT,
    lufs_step: float = LUFS_STEP,
) -> list[dict[str, Any]] | None:
    """带渐进式归一化的转录（照抄 stt-main transcribe_with_fallback）。

    重试策略（D038）：
    1. 原始音频转写 → is_junk_text 检测垃圾文本占比
    2. LUFS 归一化重试（最多 lufs_attempt 次，每次 +lufs_step LUFS）
    3. 25% 分位归一化（LUFS 全失败后）
    4. 全部失败 → 跳过并告警，返回 None

    Args:
        transcriber: WhisperTool 实例
        model_name: 模型名
        audio_array: numpy 数组（16kHz, mono, float32）
        language: 语言代码
        sample_rate: 采样率（默认 16000）
        vad_threshold: VAD 阈值
        target_lufs: LUFS 归一化初始目标响度（默认 -15.0）
        lufs_attempt: LUFS 归一化最大重试次数（默认 4）
        lufs_step: 每次 LUFS 目标响度增量（默认 3.0）

    Returns:
        转写结果 list[dict]；全部失败返回 None
    """
    _log.info("尝试使用原始音频识别...")
    transcript = _do_transcribe(transcriber, model_name, audio_array, language, vad_threshold)
    if transcript is None:
        return None
    if not is_junk_text(transcript):
        return transcript

    # LUFS 归一化重试
    current_target_lufs = target_lufs
    remaining = lufs_attempt
    while remaining > 0:
        _log.info(
            f"检测到垃圾文本占比过高（音频可能过小），"
            f"尝试 LUFS 归一化到 {current_target_lufs}..."
        )
        audio_norm = normalize_lufs(audio_array, sample_rate, target_lufs=current_target_lufs)
        transcript = _do_transcribe(transcriber, model_name, audio_norm, language, vad_threshold)
        if transcript is None:
            return None
        if not is_junk_text(transcript):
            _log.info(f"LUFS 归一化到 {current_target_lufs} 有效！")
            return transcript
        remaining -= 1
        current_target_lufs += lufs_step

    # 25% 分位归一化
    _log.info("LUFS 归一化无效，尝试 25% 分位归一化...")
    audio_norm = normalize_percentile(audio_array, PERCENTILE_NORMALIZE)
    transcript = _do_transcribe(transcriber, model_name, audio_norm, language, vad_threshold)
    if transcript is None:
        return None
    if not is_junk_text(transcript):
        _log.info("25% 分位归一化有效！")
        return transcript

    _log.warning("=" * 60)
    _log.warning("!!! 警告：所有归一化尝试均无效，跳过此文件 !!!")
    _log.warning("=" * 60)
    return None
