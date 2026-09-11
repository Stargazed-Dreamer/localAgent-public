"""L1 处理层 Ticket 17：P1 STT 转换器 + 重试策略测试

测试策略（spec-l1.md Testing Decisions）：
- mock WhisperTool（避免依赖 GPU 和模型）
- 测试 transcribe_with_fallback 重试逻辑（原始成功 / 垃圾文本触发 LUFS 归一化 / LUFS 全失败触发 25% 分位 / 全失败跳过）
- 测试 is_junk_text 垃圾文本检测（JUNK_PHRASES 占比 > 0.3 判定为垃圾）
- 测试 normalize_lufs / normalize_percentile 归一化效果
- 测试 P1 STT 转换器端到端（mock WhisperTool 返回固定结果，验证 transcript.json 格式）

覆盖 Ticket 17 acceptance criteria：
- [x] 安装 pyloudnorm + scipy
- [x] 照抄 stt-main WhisperTool 到 stt_engine.py
- [x] 照抄 transcribe_with_fallback + is_junk_text + normalize_lufs/normalize_percentile 到 stt_retry.py
- [x] P1 STT 转换器：输入 mic.wav + VAD 阈值，输出 transcript.json
- [x] P1 VAD 阈值传入 faster-whisper vad_parameters
- [x] P1 多语言支持：initial_prompt 按语言配置
- [x] P1 transcript.json 格式符合 04-stt-pipeline.md
- [x] P1 模型权重复用 stt-main/models/（D012），config.toml [recording.stt] model_dir 指向该路径
- [x] P1 默认模型 large-v3（D037）
- [x] config.toml [recording.stt] 新增字段
- [x] 单测：transcribe_with_fallback 重试逻辑
- [x] 单测：is_junk_text 垃圾文本检测
- [x] 单测：normalize_lufs / normalize_percentile
- [x] 单测：P1 端到端（mock WhisperTool）
"""

import json
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from lib.recorder.processor.p1_stt import (
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    DEFAULT_VAD_THRESHOLD,
    load_wav_as_array,
    run_p1,
    write_transcript_json,
)
from lib.recorder.processor.stt_engine import (
    INITIAL_PROMPTS,
    WhisperTool,
)
from lib.recorder.processor.stt_retry import (
    JUNK_PHRASES,
    JUNK_THRESHOLD,
    PERCENTILE_NORMALIZE,
    _do_transcribe,
    is_junk_text,
    normalize_lufs,
    normalize_percentile,
    transcribe_with_fallback,
)

# ============================ 测试辅助函数 ============================

def _write_wav(path: Path, audio: np.ndarray, sample_rate: int = 16000, sample_width: int = 2) -> None:
    """写 int16 PCM WAV 文件（模拟 L0 AudioSensor 输出）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        # float32 [-1.0, 1.0] → int16 [-32768, 32767]
        int16_data = (audio * 32767).astype(np.int16)
        wav.writeframes(int16_data.tobytes())


class _FakeTranscriber:
    """Fake WhisperTool，模拟 process_array + get_progress + get_error。

    用法：
        fake = _FakeTranscriber(results=[result1, result2, ...])
        fake.process_array() 返回 task_id，get_progress 按顺序返回 results

    results 是一个列表，每项是一次 _do_transcribe 调用的返回值：
    - list[dict]：成功，返回该转写结果
    - None：失败（模拟出错）
    """

    def __init__(self, results: list | None = None):
        # results 始终视为"每次调用的返回值"列表
        if results is None:
            self._results_queue = []
        else:
            self._results_queue = list(results)
        self._task_counter = 0
        self._tasks: dict[str, dict] = {}
        # 预加载 test_model，测试用
        self.models: dict[str, dict] = {"test_model": {"instance": None, "busy": False}}
        self.process_array_calls: list[dict] = []
        self.load_calls: list[tuple] = []

    def load(self, model_name, device=None, model_path=None):
        self.load_calls.append((model_name, device, model_path))
        self.models[model_name] = {"instance": None, "busy": False}

    def unload(self, model_name):
        if model_name in self.models:
            del self.models[model_name]

    def is_model_loaded(self, model_name):
        return model_name in self.models

    def is_model_busy(self, model_name):
        return self.models.get(model_name, {}).get("busy", False)

    def process_array(self, model_name, audio_array, language="zh", vad_threshold=None, **kwargs):
        self._task_counter += 1
        task_id = f"task_{self._task_counter}"
        self._tasks[task_id] = {"progress": 0.0, "result": None, "error": None}
        # 兼容未预加载的模型名（测试可能用 large-v3）
        if model_name not in self.models:
            self.models[model_name] = {"instance": None, "busy": False}
        self.models[model_name]["busy"] = True
        self.process_array_calls.append({
            "model_name": model_name,
            "language": language,
            "vad_threshold": vad_threshold,
            "audio_shape": audio_array.shape if hasattr(audio_array, "shape") else None,
        })
        # 取出下一个结果
        result = self._results_queue.pop(0) if self._results_queue else []
        # 模拟异步完成：直接设为完成
        if result is None:
            # 模拟出错
            self._tasks[task_id]["progress"] = -1
            self._tasks[task_id]["error"] = RuntimeError("transcribe failed")
        else:
            self._tasks[task_id]["progress"] = 1.0
            self._tasks[task_id]["result"] = result
        self.models[model_name]["busy"] = False
        return task_id

    def get_progress(self, task_id):
        task = self._tasks[task_id]
        if task["progress"] == -1:
            return (-1.0, None)
        return (task["progress"], task["result"])

    def get_error(self, task_id):
        task = self._tasks[task_id]
        if task["error"] is not None:
            raise task["error"]
        raise ValueError("Task is not in error state")


# ============================ is_junk_text 测试 ============================

class TestIsJunkText:
    """垃圾文本检测（D038：JUNK_PHRASES 占比 > JUNK_THRESHOLD=0.3 判定为垃圾）。"""

    def test_empty_transcript_not_junk(self):
        """空转写结果不是垃圾。"""
        assert is_junk_text([]) is False
        assert is_junk_text(None) is False

    def test_normal_text_not_junk(self):
        """正常文本不是垃圾。"""
        transcript = [
            {"text": "现在我要点击登录按钮"},
            {"text": "然后输入用户名和密码"},
        ]
        assert is_junk_text(transcript) is False

    def test_pure_junk_phrase_is_junk(self):
        """纯垃圾短语是垃圾（占比 100% > 0.3）。"""
        transcript = [{"text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        assert is_junk_text(transcript) is True

    def test_junk_above_threshold_is_junk(self):
        """垃圾短语占比 > 0.3 判定为垃圾。"""
        # 总字符 100，垃圾短语 40 字符（占比 0.4 > 0.3）
        junk = "中文简体" * 10  # 40 字符
        normal = "x" * 60
        transcript = [{"text": junk + normal}]
        assert is_junk_text(transcript) is True

    def test_junk_below_threshold_not_junk(self):
        """垃圾短语占比 ≤ 0.3 不判定为垃圾。"""
        # 总字符 100，垃圾短语 20 字符（占比 0.2 ≤ 0.3）
        junk = "中文简体" * 5  # 20 字符
        normal = "x" * 80
        transcript = [{"text": junk + normal}]
        assert is_junk_text(transcript) is False

    def test_custom_threshold(self):
        """自定义阈值。"""
        # 占比 0.4
        junk = "中文简体" * 10
        normal = "x" * 60
        transcript = [{"text": junk + normal}]
        # 阈值 0.5 → 不判定为垃圾
        assert is_junk_text(transcript, threshold=0.5) is False
        # 阈值 0.3 → 判定为垃圾
        assert is_junk_text(transcript, threshold=0.3) is True

    def test_multiple_junk_phrases(self):
        """多个垃圾短语都被识别。"""
        transcript = [
            {"text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"},
            {"text": "欢迎订阅 转发 打赏支持明镜与点点栏目"},
        ]
        assert is_junk_text(transcript) is True

    def test_junk_threshold_default_value(self):
        """JUNK_THRESHOLD 默认值 0.3。"""
        assert JUNK_THRESHOLD == 0.3

    def test_junk_phrases_list_not_empty(self):
        """JUNK_PHRASES 列表非空。"""
        assert len(JUNK_PHRASES) >= 3
        assert "中文简体" in JUNK_PHRASES


# ============================ normalize_lufs 测试 ============================

class TestNormalizeLufs:
    """LUFS 归一化（pyloudnorm）。"""

    def test_normalize_changes_audio(self):
        """LUFS 归一化会改变音频（音量低的会被放大）。"""
        sample_rate = 16000
        # 生成低音量音频（0.01 振幅正弦波）
        t = np.linspace(0, 1.0, sample_rate, endpoint=False)
        audio_low = (0.01 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        normalized = normalize_lufs(audio_low, sample_rate, target_lufs=-15.0)
        # 归一化后振幅应该变大
        assert np.max(np.abs(normalized)) > np.max(np.abs(audio_low))

    def test_normalize_preserves_length(self):
        """归一化保持音频长度不变。"""
        sample_rate = 16000
        audio = np.random.RandomState(42).randn(sample_rate).astype(np.float32) * 0.1
        normalized = normalize_lufs(audio, sample_rate, target_lufs=-15.0)
        assert len(normalized) == len(audio)

    def test_normalize_silence_returns_original(self):
        """静音音频（响度极低）返回原数组。"""
        sample_rate = 16000
        audio = np.zeros(sample_rate, dtype=np.float32)
        normalized = normalize_lufs(audio, sample_rate, target_lufs=-15.0)
        # 静音返回原数组
        assert np.array_equal(normalized, audio)

    def test_normalize_returns_float32(self):
        """归一化返回 float32 类型。"""
        sample_rate = 16000
        t = np.linspace(0, 1.0, sample_rate, endpoint=False)
        audio = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        normalized = normalize_lufs(audio, sample_rate, target_lufs=-15.0)
        assert normalized.dtype == np.float32


# ============================ normalize_percentile 测试 ============================

class TestNormalizePercentile:
    """百分位归一化。"""

    def test_normalize_changes_audio(self):
        """百分位归一化会改变音频。"""
        audio = np.random.RandomState(42).randn(16000).astype(np.float32) * 0.1
        normalized = normalize_percentile(audio, 0.25)
        # 归一化后最大值应该变大
        assert np.max(np.abs(normalized)) > np.max(np.abs(audio))

    def test_normalize_preserves_length(self):
        """归一化保持长度不变。"""
        audio = np.random.RandomState(42).randn(16000).astype(np.float32) * 0.1
        normalized = normalize_percentile(audio, 0.25)
        assert len(normalized) == len(audio)

    def test_normalize_silence_returns_original(self):
        """静音返回原数组（threshold=0）。"""
        audio = np.zeros(16000, dtype=np.float32)
        normalized = normalize_percentile(audio, 0.25)
        assert np.array_equal(normalized, audio)

    def test_percentile_default_value(self):
        """PERCENTILE_NORMALIZE 默认 0.25。"""
        assert PERCENTILE_NORMALIZE == 0.25


# ============================ transcribe_with_fallback 测试 ============================

class TestTranscribeWithFallback:
    """重试策略测试（D038：原始 → LUFS 归一化 × 4 → 25% 分位 → 跳过）。"""

    def test_original_success_no_retry(self):
        """原始音频转写成功且非垃圾文本 → 不重试。"""
        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常文本内容"}]
        fake = _FakeTranscriber(results=[normal_result])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(fake, "test_model", audio, "zh")
        assert result == normal_result
        # 只调用了一次 process_array（原始音频）
        assert len(fake.process_array_calls) == 1

    def test_junk_text_triggers_lufs_retry(self):
        """原始是垃圾文本 → 触发 LUFS 归一化重试，第二次成功。"""
        junk_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常文本"}]
        fake = _FakeTranscriber(results=[junk_result, normal_result])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(fake, "test_model", audio, "zh")
        assert result == normal_result
        # 调用了两次：原始 + LUFS 1 次
        assert len(fake.process_array_calls) == 2

    def test_all_lufs_fail_triggers_percentile(self):
        """原始 + 4 次 LUFS 都是垃圾 → 触发 25% 分位归一化。"""
        junk_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常文本"}]
        # 原始 + 4 次 LUFS 都是垃圾，第 6 次（25% 分位）成功
        fake = _FakeTranscriber(results=[junk_result] * 5 + [normal_result])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(fake, "test_model", audio, "zh")
        assert result == normal_result
        # 1 原始 + 4 LUFS + 1 百分位 = 6 次
        assert len(fake.process_array_calls) == 6

    def test_all_attempts_fail_returns_none(self):
        """全部归一化尝试都无效 → 返回 None。"""
        junk_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        # 6 次都是垃圾（1 原始 + 4 LUFS + 1 百分位）
        fake = _FakeTranscriber(results=[junk_result] * 6)
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(fake, "test_model", audio, "zh")
        assert result is None
        assert len(fake.process_array_calls) == 6

    def test_transcribe_error_returns_none(self):
        """转写出错（_do_transcribe 返回 None）→ 直接返回 None 不重试。"""
        # 第一次就返回 None（出错）
        fake = _FakeTranscriber(results=[None])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(fake, "test_model", audio, "zh")
        assert result is None
        # 只调用一次（出错不重试）
        assert len(fake.process_array_calls) == 1

    def test_vad_threshold_passed_through(self):
        """VAD 阈值传给 transcriber.process_array。"""
        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常"}]
        fake = _FakeTranscriber(results=[normal_result])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        transcribe_with_fallback(fake, "test_model", audio, "zh", vad_threshold=0.3)
        assert fake.process_array_calls[0]["vad_threshold"] == 0.3

    def test_custom_lufs_params(self):
        """自定义 LUFS 参数生效。"""
        junk_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常"}]
        # 自定义：只重试 2 次 LUFS
        fake = _FakeTranscriber(results=[junk_result, junk_result, normal_result])
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = transcribe_with_fallback(
            fake, "test_model", audio, "zh",
            target_lufs=-20.0, lufs_attempt=2, lufs_step=5.0,
        )
        assert result == normal_result
        # 1 原始 + 2 LUFS = 3 次
        assert len(fake.process_array_calls) == 3


# ============================ _do_transcribe 测试 ============================

class TestDoTranscribe:
    """_do_transcribe 单次转写测试。"""

    def test_success_returns_result(self):
        """转写成功返回结果。"""
        result_data = [{"line": 1, "start": 0.0, "end": 1.0, "text": "test"}]
        fake = _FakeTranscriber(results=[result_data])
        audio = np.zeros(16000, dtype=np.float32)
        result = _do_transcribe(fake, "test_model", audio, "zh", None)
        assert result == result_data

    def test_error_returns_none(self):
        """转写出错返回 None。"""
        fake = _FakeTranscriber(results=[None])
        audio = np.zeros(16000, dtype=np.float32)
        result = _do_transcribe(fake, "test_model", audio, "zh", None)
        assert result is None


# ============================ WhisperTool 测试（不依赖真实模型） ============================

class TestWhisperToolLifecycle:
    """WhisperTool 生命周期测试（不加载真实模型）。"""

    def test_load_raises_on_duplicate(self):
        """重复加载同一模型抛 RuntimeError。"""
        tool = WhisperTool(print_log=False)
        # mock WhisperModel 避免真实加载
        with patch("lib.recorder.processor.stt_engine.WhisperModel") as mock_cls:
            mock_cls.return_value = MagicMock()
            tool.load("test_model")
            with pytest.raises(RuntimeError, match="already loaded"):
                tool.load("test_model")

    def test_unload_removes_model(self):
        """卸载模型后 is_model_loaded 返回 False。"""
        tool = WhisperTool(print_log=False)
        with patch("lib.recorder.processor.stt_engine.WhisperModel") as mock_cls:
            mock_cls.return_value = MagicMock()
            tool.load("test_model")
            assert tool.is_model_loaded("test_model")
            tool.unload("test_model")
            assert not tool.is_model_loaded("test_model")

    def test_unload_not_loaded_raises(self):
        """卸载未加载的模型抛 KeyError。"""
        tool = WhisperTool(print_log=False)
        with pytest.raises(KeyError):
            tool.unload("not_loaded")

    def test_process_array_not_loaded_raises(self):
        """process_array 未加载模型抛 ValueError。"""
        tool = WhisperTool(print_log=False)
        audio = np.zeros(16000, dtype=np.float32)
        with pytest.raises(ValueError, match="not loaded"):
            tool.process_array("not_loaded", audio)

    def test_process_array_busy_raises(self):
        """模型忙时抛 RuntimeError。"""
        tool = WhisperTool(print_log=False)
        with patch("lib.recorder.processor.stt_engine.WhisperModel") as mock_cls:
            mock_cls.return_value = MagicMock()
            tool.load("test_model")
            tool.models["test_model"]["busy"] = True
            audio = np.zeros(16000, dtype=np.float32)
            with pytest.raises(RuntimeError, match="currently processing"):
                tool.process_array("test_model", audio)

    def test_get_progress_unknown_task_raises(self):
        """get_progress 未知 task_id 抛 KeyError。"""
        tool = WhisperTool(print_log=False)
        with pytest.raises(KeyError):
            tool.get_progress("unknown_task")

    def test_get_error_no_error_raises(self):
        """get_error 任务未出错抛 ValueError。"""
        tool = WhisperTool(print_log=False)
        tool.tasks["task1"] = {"progress": 0.0, "result": None, "error": None}
        with pytest.raises(ValueError, match="not in error state"):
            tool.get_error("task1")

    def test_initial_prompts_contains_zh_and_en(self):
        """initial_prompts 包含中英文。"""
        assert "zh" in INITIAL_PROMPTS
        assert "en" in INITIAL_PROMPTS
        assert INITIAL_PROMPTS["zh"] == "转录为中文简体。"
        assert INITIAL_PROMPTS["en"] == "Transcribed into English."


# ============================ load_wav_as_array 测试 ============================

class TestLoadWavAsArray:
    """WAV 文件读取测试。"""

    def test_read_int16_wav(self, tmp_path):
        """读取 int16 PCM WAV。"""
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        wav_path = tmp_path / "test.wav"
        _write_wav(wav_path, audio, sample_rate=sample_rate, sample_width=2)

        loaded, sr = load_wav_as_array(wav_path)
        assert sr == sample_rate
        assert len(loaded) == sample_rate
        assert loaded.dtype == np.float32
        # int16 → float32 转换有精度损失，但大致一致
        np.testing.assert_allclose(loaded, audio, atol=1e-4)

    def test_read_nonexistent_raises(self, tmp_path):
        """读取不存在的文件抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            load_wav_as_array(tmp_path / "nonexistent.wav")

    def test_read_stereo_takes_first_channel(self, tmp_path):
        """多声道取第一声道。"""
        sample_rate = 16000
        wav_path = tmp_path / "stereo.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            # 写立体声数据（左声道 0.5，右声道 0.1）
            left = (0.5 * 32767 * np.ones(sample_rate)).astype(np.int16)
            right = (0.1 * 32767 * np.ones(sample_rate)).astype(np.int16)
            stereo = np.empty(sample_rate * 2, dtype=np.int16)
            stereo[0::2] = left
            stereo[1::2] = right
            wav.writeframes(stereo.tobytes())

        loaded, sr = load_wav_as_array(wav_path)
        assert sr == sample_rate
        assert len(loaded) == sample_rate
        # 取第一声道（左声道 0.5）
        np.testing.assert_allclose(np.abs(loaded), 0.5, atol=1e-3)


# ============================ write_transcript_json 测试 ============================

class TestWriteTranscriptJson:
    """transcript.json 写入测试。"""

    def test_write_creates_file(self, tmp_path):
        """写入创建 transcript.json 文件。"""
        transcript = [
            {"line": 1, "start": 0.0, "end": 1.0, "text": "test"},
        ]
        path = write_transcript_json(transcript, tmp_path)
        assert path == tmp_path / "transcript.json"
        assert path.exists()

    def test_write_content_correct(self, tmp_path):
        """写入内容正确（JSON + 中文 + 缩进）。"""
        transcript = [
            {"line": 1, "start": 1.23, "end": 4.50, "text": "现在我要点击登录按钮"},
        ]
        path = write_transcript_json(transcript, tmp_path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == transcript
        assert "现在我要点击登录按钮" in path.read_text(encoding="utf-8")

    def test_write_empty_list(self, tmp_path):
        """写入空列表。"""
        path = write_transcript_json([], tmp_path)
        assert json.loads(path.read_text(encoding="utf-8")) == []


# ============================ run_p1 端到端测试 ============================

class TestRunP1EndToEnd:
    """P1 STT 转换器端到端测试（mock WhisperTool）。"""

    def test_run_p1_success(self, tmp_path):
        """run_p1 成功：读 WAV → 转写 → 写 transcript.json。"""
        # 准备录制包
        package_root = tmp_path / "rec_pkg"
        audio_dir = package_root / "audio"
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        _write_wav(audio_dir / "mic.wav", audio, sample_rate=sample_rate)

        # mock transcriber 返回固定结果
        transcript_result = [
            {"line": 1, "start": 0.0, "end": 1.0, "text": "测试转写内容"},
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = run_p1(package_root, model_name="test_model", transcriber=fake)
        assert result == transcript_result
        # transcript.json 已写入
        transcript_path = package_root / "transcript.json"
        assert transcript_path.exists()
        loaded = json.loads(transcript_path.read_text(encoding="utf-8"))
        assert loaded == transcript_result

    def test_run_p1_missing_wav_raises(self, tmp_path):
        """run_p1 缺少 audio/mic.wav 抛 FileNotFoundError。"""
        package_root = tmp_path / "rec_no_audio"
        package_root.mkdir(parents=True)
        fake = _FakeTranscriber(results=[])

        with pytest.raises(FileNotFoundError, match="audio/mic.wav"):
            run_p1(package_root, model_name="test_model", transcriber=fake)

    def test_run_p1_all_normalize_fail_writes_empty(self, tmp_path):
        """run_p1 全部归一化失败 → 写空 transcript.json。"""
        package_root = tmp_path / "rec_fail"
        audio_dir = package_root / "audio"
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        _write_wav(audio_dir / "mic.wav", audio, sample_rate=sample_rate)

        # 6 次都是垃圾文本（1 原始 + 4 LUFS + 1 百分位）
        junk_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
        fake = _FakeTranscriber(results=[junk_result] * 6)

        result = run_p1(package_root, model_name="test_model", transcriber=fake)
        assert result == []
        transcript_path = package_root / "transcript.json"
        assert transcript_path.exists()
        assert json.loads(transcript_path.read_text(encoding="utf-8")) == []

    def test_run_p1_passes_vad_threshold(self, tmp_path):
        """run_p1 把 VAD 阈值传给 transcriber。"""
        package_root = tmp_path / "rec_vad"
        audio_dir = package_root / "audio"
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        _write_wav(audio_dir / "mic.wav", audio, sample_rate=sample_rate)

        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "正常"}]
        fake = _FakeTranscriber(results=[normal_result])

        run_p1(package_root, model_name="test_model", vad_threshold=0.3, transcriber=fake)
        assert fake.process_array_calls[0]["vad_threshold"] == 0.3

    def test_run_p1_passes_language(self, tmp_path):
        """run_p1 把语言传给 transcriber。"""
        package_root = tmp_path / "rec_lang"
        audio_dir = package_root / "audio"
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        _write_wav(audio_dir / "mic.wav", audio, sample_rate=sample_rate)

        normal_result = [{"line": 1, "start": 0.0, "end": 1.0, "text": "hello"}]
        fake = _FakeTranscriber(results=[normal_result])

        run_p1(package_root, model_name="test_model", language="en", transcriber=fake)
        assert fake.process_array_calls[0]["language"] == "en"

    def test_run_p1_default_model_large_v3(self):
        """默认模型是 large-v3（D037）。"""
        assert DEFAULT_MODEL == "large-v3"

    def test_run_p1_default_language_zh(self):
        """默认语言是 zh。"""
        assert DEFAULT_LANGUAGE == "zh"

    def test_run_p1_default_vad_threshold(self):
        """默认 VAD 阈值 0.5。"""
        assert DEFAULT_VAD_THRESHOLD == 0.5


# ============================ pipeline P1 集成测试 ============================

class TestPipelineP1Integration:
    """pipeline.process_recording_package 的 P1 集成测试。"""

    def test_pipeline_p1_success(self, tmp_path):
        """pipeline run_p1=True + 注入 transcriber → P1 成功。"""
        from lib.recorder.processor import process_recording_package

        package_root = tmp_path / "rec_pipeline_p1"
        audio_dir = package_root / "audio"
        sample_rate = 16000
        audio = (np.random.RandomState(42).randn(sample_rate) * 0.1).astype(np.float32)
        _write_wav(audio_dir / "mic.wav", audio, sample_rate=sample_rate)

        transcript_result = [
            {"line": 1, "start": 0.0, "end": 1.0, "text": "pipeline 测试"},
        ]
        fake = _FakeTranscriber(results=[transcript_result])

        result = process_recording_package(
            package_root,
            options={
                "run_p4": False,
                "run_p5": False,
                "run_p1": True,
                "p1_transcriber": fake,
                "p1_model": "test_model",
            },
        )
        assert result.transcript_path is not None
        assert result.transcript_path.exists()
        assert result.transcript_segment_count == 1
        assert "p1" not in result.errors

    def test_pipeline_p1_missing_wav_records_error(self, tmp_path):
        """pipeline run_p1=True 但 audio/mic.wav 不存在 → P1 错误记录到 errors。"""
        from lib.recorder.processor import process_recording_package

        package_root = tmp_path / "rec_pipeline_p1_no_wav"
        package_root.mkdir(parents=True)
        fake = _FakeTranscriber(results=[])

        result = process_recording_package(
            package_root,
            options={
                "run_p4": False,
                "run_p5": False,
                "run_p1": True,
                "p1_transcriber": fake,
            },
        )
        assert "p1" in result.errors
        assert "FileNotFoundError" in result.errors["p1"]
        assert result.transcript_path is None

    def test_pipeline_p1_disabled_by_default(self, tmp_path):
        """默认 run_p1=False → P1 不执行。"""
        from lib.recorder.processor import process_recording_package

        package_root = tmp_path / "rec_pipeline_no_p1"
        package_root.mkdir(parents=True)

        result = process_recording_package(package_root)
        assert result.transcript_path is None
        assert "p1" not in result.errors
