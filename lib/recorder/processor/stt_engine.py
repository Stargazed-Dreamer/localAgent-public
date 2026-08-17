"""STT 引擎（照抄 stt-main whisperTranscriber.py，Ticket 17）

源码：E:\\<data_drive>:\<working_root>\\- 工作中项目\\视频配字幕\\stt-main\\whisperTranscriber.py

照抄原因（D011）：stt-main 的 WhisperTool 经过用户多次调整，含工程优化（线程安全 + 任务进度查询
+ 词级时间戳拆分 + 模型加载/卸载）。直接照抄保留原作者所有权（项目硬约束）。

照抄内容：
- WhisperTool 类：模型加载/卸载/进度查询/词级时间戳拆分
- _split_segment_by_words：按词级时间戳拆分过长 segment
- _process_task：内部任务处理（异步线程）
- process_array：处理 numpy 数组（faster-whisper 标准输入）
- get_progress / get_error：任务进度/错误查询

照抄时改动（最小化）：
- 移除 logging 配置（用 stdlib logging，不强制 set up handlers，由调用方配）
- 移除 cfg 全局变量（改为 WhisperTool 构造器参数 + 默认值常量）
- 移除 process_file（P1 STT 只处理 numpy 数组，不处理文件，文件读取在 P1 入口做）
- 移除 output_format='text'/'srt' 分支（P1 STT 只用 'json' 输出，含词级时间戳）
- 默认参数：device='cuda' + compute_type='float16' + beam_size=5 + vad_filter=True
- 增加 vad_parameters 注入点（用户配的 VAD 阈值传给 faster-whisper）

原作者所有权：本文件照抄自 stt-main 项目（whisperTranscriber.py），原作者所有权保留。
"""

import html
import logging
import re
import threading
import uuid
from typing import Any

from faster_whisper import WhisperModel

# ========== 默认参数（照抄 stt-main whisperTranscriber.py cfg 字典） ==========
DEFAULT_DEVICE = "cuda"
DEFAULT_COMPUTE_TYPE_CUDA = "float16"
DEFAULT_COMPUTE_TYPE_CPU = "float32"
DEFAULT_BEAM_SIZE = 5
DEFAULT_BEST_OF = 5
DEFAULT_VAD_FILTER = True
DEFAULT_TEMPERATURE = 0
DEFAULT_CONDITION_ON_PREVIOUS_TEXT = False

# 多语言 initial_prompt（照抄 stt-main whisperTranscriber.py 80-91 行）
INITIAL_PROMPTS: dict[str, str] = {
    "zh": "转录为中文简体。",
    "en": "Transcribed into English.",
    "fr": "Transcrit en français.",
    "de": "Transkribiert ins Deutsche.",
    "ja": "日本語に転写。",
    "ko": "한국어로 전사.",
    "ru": "Транскрибировано на русский.",
    "es": "Transcrito al español.",
    "th": "เขียนเป็นภาษาไทย。",
    "it": "Trascritto in italiano.",
    "pt": "Transcrito para português.",
    "vi": "Chuyển ngữ sang tiếng Việt.",
    "ar": "تم النسخ إلى العربية.",
    "tr": "Türkçeye yazıldı.",
    "hu": "Magyarra átírva.",
}


class WhisperTool:
    """Whisper 转写工具（照抄 stt-main，线程安全 + 异步任务 + 词级时间戳拆分）。

    Args:
        model_dir: 模型存放目录（默认 ~/.cache/huggingface/hub/）
        log_file: 日志文件路径，None 不写文件
        print_log: 是否控制台打印日志
    """

    def __init__(
        self,
        *,
        model_dir: str | None = None,
        log_file: str | None = None,
        print_log: bool = True,
    ) -> None:
        self.models: dict[str, dict[str, Any]] = {}  # {model_name: {"instance": obj, "busy": bool}}
        self.tasks: dict[str, dict[str, Any]] = {}   # {task_id: {"progress": float, "result": any, "error": Exception}}
        self.lock = threading.Lock()
        self.model_dir = model_dir
        self.log = logging.getLogger("WhisperTool")
        self._setup_logging(log_file, print_log)

    def _setup_logging(self, log_file: str | None = None, print_log: bool = True) -> None:
        """配置日志系统。"""
        self.log.handlers = []
        self.log.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        if print_log:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.log.addHandler(console_handler)
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            self.log.addHandler(file_handler)

    def load(
        self,
        model_name: str,
        device: str | None = None,
        model_path: str | None = None,
    ) -> None:
        """载入模型（照抄 stt-main load 方法）。

        Args:
            model_name: 模型名（如 'tiny' / 'base' / 'small' / 'medium' / 'large-v3' / 'large-v3-turbo'）
            device: 'cpu' 或 'cuda'，None 用默认 cuda
            model_path: 模型根目录，None 用 self.model_dir 或 huggingface 默认缓存

        Raises:
            RuntimeError: 模型已加载
            Exception: 模型加载失败
        """
        with self.lock:
            if model_name in self.models:
                raise RuntimeError(f"Model '{model_name}' is already loaded.")
            if device is None:
                device = DEFAULT_DEVICE
            if model_path is None:
                model_path = self.model_dir
            compute_type = DEFAULT_COMPUTE_TYPE_CPU if device == "cpu" else DEFAULT_COMPUTE_TYPE_CUDA

            self.log.info(f"Loading model '{model_name}' from {model_path} on {device} ({compute_type})...")
            try:
                kwargs: dict[str, Any] = {
                    "device": device,
                    "compute_type": compute_type,
                }
                if model_path:
                    kwargs["download_root"] = model_path
                model = WhisperModel(model_name, **kwargs)
                self.models[model_name] = {"instance": model, "busy": False}
                self.log.info(f"Model '{model_name}' loaded successfully.")
            except Exception as e:
                self.log.error(f"Failed to load model '{model_name}': {e}")
                raise

    def unload(self, model_name: str) -> None:
        """卸载模型并清缓存（照抄 stt-main unload 方法）。"""
        with self.lock:
            if model_name not in self.models:
                raise KeyError(f"Model '{model_name}' is not loaded.")
            self.log.info(f"Unloading model '{model_name}'...")
            del self.models[model_name]
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            self.log.info(f"Model '{model_name}' unloaded.")

    def is_model_loaded(self, model_name: str) -> bool:
        return model_name in self.models

    def is_model_busy(self, model_name: str) -> bool:
        if model_name not in self.models:
            return False
        return self.models[model_name]["busy"]

    def _split_segment_by_words(
        self,
        segment: Any,
        max_chars: int,
        min_duration: float = 0,
    ) -> list[tuple[float, float, str]]:
        """将过长的 segment 按词级时间戳拆分为多个子段（照抄 stt-main）。

        Returns:
            [(start, end, text), ...]
        """
        if max_chars <= 0 and min_duration <= 0:
            return [(segment.start, segment.end, segment.text.strip())]

        text = segment.text.strip()
        words = segment.words
        if not words:
            return [(segment.start, segment.end, text)]

        if max_chars <= 0:
            max_chars = 99999

        if len(text) <= max_chars and (segment.end - segment.start) >= min_duration:
            return [(segment.start, segment.end, text)]

        sub_segments: list[tuple[float, float, str]] = []
        current_text = ""
        current_start = words[0].start
        current_end = words[0].end

        for word in words:
            word_text = word.word.strip()
            if not word_text:
                continue
            candidate = (current_text + word_text).strip()
            candidate_duration = word.end - current_start
            should_cut = (
                len(candidate) > max_chars
                and current_text
                and candidate_duration >= min_duration
            )
            if should_cut:
                cleaned = current_text.strip()
                if cleaned:
                    sub_segments.append((current_start, current_end, cleaned))
                current_text = word_text
                current_start = word.start
                current_end = word.end
            else:
                if not current_text:
                    current_start = word.start
                current_text = candidate
                current_end = word.end

        cleaned = current_text.strip()
        if cleaned:
            sub_segments.append((current_start, current_end, cleaned))

        if not sub_segments:
            return [(segment.start, segment.end, text)]

        if min_duration > 0 and len(sub_segments) >= 2:
            merged: list[tuple[float, float, str]] = []
            buf_start, buf_end, buf_text = sub_segments[0]
            for i in range(1, len(sub_segments)):
                s_start, s_end, s_text = sub_segments[i]
                if (buf_end - buf_start) < min_duration:
                    buf_end = s_end
                    buf_text = buf_text + s_text
                else:
                    merged.append((buf_start, buf_end, buf_text))
                    buf_start, buf_end, buf_text = s_start, s_end, s_text
            merged.append((buf_start, buf_end, buf_text))
            sub_segments = merged

        return sub_segments

    def _process_task(
        self,
        task_id: str,
        model_name: str,
        audio_input: Any,
        language: str,
        vad_threshold: float | None,
        max_chars: int,
        min_duration: float,
        beam_size: int,
        best_of: int,
        **kwargs: Any,
    ) -> None:
        """内部任务处理线程函数（照抄 stt-main _process_task，简化为只输出 json）。"""
        model_obj: WhisperModel = self.models[model_name]["instance"]
        try:
            self.log.info(f"[{task_id}] Start processing...")

            # initial_prompt 按语言配置
            initial_prompt = kwargs.get("initial_prompt")
            if initial_prompt is None:
                initial_prompt = INITIAL_PROMPTS.get(language)

            # vad_parameters：用户配的 threshold 传给 faster-whisper（None 用默认）
            vad_parameters: dict[str, Any] = {"min_silence_duration_ms": 500}
            if vad_threshold is not None:
                vad_parameters["threshold"] = vad_threshold

            segments, info = model_obj.transcribe(
                audio_input,
                beam_size=beam_size,
                best_of=best_of,
                vad_filter=True,
                vad_parameters=vad_parameters,
                language=language if language and language != "auto" else None,
                initial_prompt=initial_prompt,
                word_timestamps=True,
            )

            total_duration = info.duration
            raw_subtitles: list[dict[str, Any]] = []

            for segment in segments:
                progress = min(segment.end / total_duration, 0.99) if total_duration > 0 else 0.99
                with self.lock:
                    self.tasks[task_id]["progress"] = progress

                text = segment.text.strip()
                text = html.unescape(text)
                # 去除无效字符（照抄 stt-main 正则）
                if not text or re.match(
                    r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text
                ) or len(text) <= 1:
                    continue

                sub_segments = self._split_segment_by_words(segment, max_chars, min_duration)
                for sub_start, sub_end, sub_text in sub_segments:
                    sub_text = html.unescape(sub_text)
                    if not sub_text or re.match(
                        r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', sub_text
                    ) or len(sub_text) <= 1:
                        continue
                    raw_subtitles.append({
                        "line": len(raw_subtitles) + 1,
                        "start": float(sub_start),
                        "end": float(sub_end),
                        "text": sub_text,
                    })

            with self.lock:
                self.tasks[task_id]["progress"] = 1.0
                self.tasks[task_id]["result"] = raw_subtitles
                self.models[model_name]["busy"] = False
            self.log.info(f"[{task_id}] Processing finished.")

        except Exception as e:
            self.log.error(f"[{task_id}] Error: {e}")
            with self.lock:
                self.tasks[task_id]["progress"] = -1
                self.tasks[task_id]["error"] = e
                self.models[model_name]["busy"] = False

    def process_array(
        self,
        model_name: str,
        audio_array: Any,
        language: str = "zh",
        vad_threshold: float | None = None,
        max_chars_per_line: int = 40,
        min_duration_per_line: float = 1.5,
        beam_size: int = DEFAULT_BEAM_SIZE,
        best_of: int = DEFAULT_BEST_OF,
        **kwargs: Any,
    ) -> str:
        """处理 numpy 数组（照抄 stt-main process_array，简化为只输出 json）。

        Args:
            model_name: 已加载的模型名
            audio_array: numpy.ndarray（16kHz, mono, float32）
            language: 语言代码（'zh' / 'en' / 'auto' 等）
            vad_threshold: VAD 阈值（0-1，None 用 faster-whisper 默认 0.5）
            max_chars_per_line: 单条字幕最大字符数（0=不拆分）
            min_duration_per_line: 单条字幕最短持续时间秒（0=不限）
            beam_size: 束搜索大小
            best_of: 候选序列数量
            **kwargs: 其他参数（initial_prompt 等）

        Returns:
            task_id（用 get_progress(task_id) 查进度）

        Raises:
            ValueError: 模型未加载
            RuntimeError: 模型忙
        """
        if model_name not in self.models:
            raise ValueError(f"Model '{model_name}' is not loaded.")
        if self.models[model_name]["busy"]:
            raise RuntimeError(f"Model '{model_name}' is currently processing another task.")

        task_id = uuid.uuid4().hex
        with self.lock:
            self.tasks[task_id] = {"progress": 0.0, "result": None, "error": None}
            self.models[model_name]["busy"] = True

        thread = threading.Thread(
            target=self._process_task,
            args=(
                task_id,
                model_name,
                audio_array,
                language,
                vad_threshold,
                max_chars_per_line,
                min_duration_per_line,
                beam_size,
                best_of,
            ),
            kwargs=kwargs,
        )
        thread.daemon = True
        thread.start()
        return task_id

    def get_progress(self, task_id: str) -> tuple[float, Any]:
        """查询任务进度（照抄 stt-main get_progress）。

        Returns:
            (progress, result)：progress 0.0~1.0 进行中 / -1.0 出错 / 1.0 完成
            result 完成时返回数据，否则 None

        Raises:
            KeyError: task_id 不存在
        """
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(f"Task ID '{task_id}' not found.")
            task = self.tasks[task_id]
            prog = task["progress"]
            if prog == -1:
                return (-1.0, None)
            return (prog, task["result"])

    def get_error(self, task_id: str) -> None:
        """获取任务错误信息（照抄 stt-main get_error）。

        Raises:
            Exception: 任务出错时抛具体错误
            ValueError: 任务未出错或不存在
        """
        with self.lock:
            if task_id not in self.tasks:
                raise ValueError(f"Task ID '{task_id}' not found.")
            task = self.tasks[task_id]
            if task["error"] is not None:
                raise task["error"]
            else:
                raise ValueError("Task is not in error state")
