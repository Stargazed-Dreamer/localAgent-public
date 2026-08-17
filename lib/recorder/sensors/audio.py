"""L0 M3 音频录制器：sounddevice 录音 + wave 流式写 WAV

设计（D009 / D025）：
- sounddevice.InputStream 持续录麦克风，dtype='int16'（标准 PCM，wave 模块直接支持）
- 采样率 16kHz / mono（faster-whisper 标准输入；faster-whisper 读 WAV 时自动转 float32）
- wave 模块流式写 WAV（实时落盘，回调里直接 writeframes）
- L0 不分段，连续录一个大 WAV（分段是 L1 STT 的事）
- 音频数据不写 events.jsonl（直接写 WAV），但 start/stop 由 controller 协调

线程模型：
- sounddevice 内部起回调线程，回调里把 indata 写到 wave 文件
- 主线程只需 start/stop
- 用 threading.Lock 保护 wave 文件写入（避免 stop 时与回调竞争）

为什么 dtype='int16' 而非 spec 写的 'float32'：
- spec 的"float32"指 faster-whisper 输入格式，不是 WAV 文件格式
- 标准 WAV 是 PCM int16，所有播放器/wave 模块/faster-whisper 都能读
- 用 int16 直接写省一次类型转换，WAV 文件大小是 float32 的 1/2
- faster-whisper 读 WAV 时会自动转 float32
"""

import threading
import wave

import sounddevice as sd


class AudioSensor:
    """M3 音频录制器。

    Args:
        controller: RecordingController 引用（用来取录制包路径）
        samplerate: 采样率，默认 16000（faster-whisper 标准）
        channels: 声道数，默认 1（mono）
        device: sounddevice 输入设备索引（int）或名称（str）。
            None 时用系统默认输入设备（可能选错麦克风，建议在 ConfigDialog 显式选择）。
            通过 list_available_microphones() 获取可用设备列表。
    """

    def __init__(
        self,
        controller,
        samplerate: int = 16000,
        channels: int = 1,
        device: int | str | None = None,
    ) -> None:
        self.controller = controller
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self._stream = None
        self._wav_file = None
        self._lock = threading.Lock()
        self._frame_count = 0  # 采样帧数（不是截图帧数）
        self._started = False

    def start(self) -> None:
        """启动录音：打开 WAV 文件 + 启动 sounddevice 流。

        支持暂停后恢复（pause/resume）：如果 WAV 文件已存在且非空，
        以追加模式打开，保留之前录制的音频。
        """
        if self._started:
            return
        if self.controller is None:
            raise RuntimeError("AudioSensor.controller 未注入")
        self._started = True
        # 打开 WAV 文件写（支持 resume 追加）
        wav_path = self.controller.package.audio_file
        if wav_path.exists() and wav_path.stat().st_size > 0:
            # 追加模式：保留已有音频（pause/resume 场景）
            self._wav_file = wave.open(str(wav_path), "ab")  # noqa: SIM115
        else:
            # 新建文件
            self._wav_file = wave.open(str(wav_path), "wb")  # noqa: SIM115
            self._wav_file.setnchannels(self.channels)
            self._wav_file.setsampwidth(2)  # int16 = 2 bytes
            self._wav_file.setframerate(self.samplerate)
        # 启动 sounddevice 输入流（device=None 用系统默认；显式指定避免选错麦克风）
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
            device=self.device,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        """sounddevice 回调：把数据写到 WAV 文件。

        Args:
            indata: numpy 数组 shape (frames, channels)，dtype int16
            frames: 本次回调的采样帧数
            time_info: sounddevice 时间信息（不用）
            status: sounddevice 状态标志（不用）
        """
        with self._lock:
            if self._wav_file is not None:
                self._wav_file.writeframes(indata.tobytes())
                self._frame_count += frames

    def stop(self) -> None:
        """停止录音：关闭 sounddevice 流 + 关闭 WAV 文件。幂等。"""
        if not self._started:
            return
        self._started = False
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            if self._wav_file is not None:
                try:
                    self._wav_file.close()
                except Exception:
                    pass
                self._wav_file = None

    @property
    def duration_seconds(self) -> float:
        """已录制音频时长（秒）= frame_count / samplerate。"""
        if self.samplerate <= 0:
            return 0.0
        return self._frame_count / self.samplerate

    @property
    def frame_count(self) -> int:
        """已录制的采样帧数。"""
        return self._frame_count
