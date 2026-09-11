"""L0 采集层 Ticket 04：AudioSensor 单测 + 集成测试

测试策略：
- mock sounddevice.InputStream：用假 stream 在 start() 时启动一个线程模拟回调发数据
- 不真开麦克风（CI 环境无音频设备）
- 验证：WAV 文件可被 wave 模块读回 + 帧数/时长正确 + controller 集成
"""

import threading
import time
import wave

import numpy as np
import pytest

from lib.recorder.controller import RecordingController
from lib.recorder.sensors.audio import AudioSensor


class FakeInputStream:
    """模拟 sounddevice.InputStream，在 start 时启动线程发数据。

    sounddevice 真实回调签名：callback(indata, frames, time, status)
    indata 是 numpy 数组 shape (frames, channels)，dtype 由 stream 决定。
    """

    def __init__(self, samplerate, channels, dtype, callback, **kwargs):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.callback = callback
        self._thread = None
        self._stop_event = threading.Event()
        self._started = False

    def start(self):
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._started = False

    def close(self):
        if self._started:
            self.stop()

    def _run(self):
        """每 50ms 发一帧数据（800 samples @ 16kHz）。"""
        frames_per_chunk = int(self.samplerate * 0.05)  # 50ms = 800 samples @ 16kHz
        while not self._stop_event.is_set():
            # 生成静音数据（zeros）
            indata = np.zeros((frames_per_chunk, self.channels), dtype=self.dtype)
            self.callback(indata, frames_per_chunk, None, None)
            time.sleep(0.05)


@pytest.fixture
def fake_sounddevice(monkeypatch):
    """mock sounddevice 模块，让 AudioSensor 用 FakeInputStream。"""
    import sys
    fake_module = type(sys)('sounddevice_fake')
    fake_module.InputStream = FakeInputStream
    monkeypatch.setitem(sys.modules, 'sounddevice', fake_module)
    return fake_module


# ==================== AudioSensor 单测 ====================

class TestAudioSensor:
    def test_start_creates_wav_file(self, tmp_path, fake_sounddevice):
        """start() 后录制包 audio/mic.wav 文件存在"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = AudioSensor(controller=controller)
        sensor.start()
        assert controller.package.audio_file.exists()
        sensor.stop()
        controller.stop()

    def test_callback_writes_frames_to_wav(self, tmp_path, fake_sounddevice):
        """回调写入的帧数与 WAV 文件读回的帧数一致"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = AudioSensor(controller=controller)
        sensor.start()
        # 让 FakeInputStream 跑 200ms（4 个 50ms chunk = 3200 samples @ 16kHz）
        time.sleep(0.25)
        sensor.stop()
        controller.stop()

        # 用 wave 读回 WAV 文件验证
        with wave.open(str(controller.package.audio_file), 'rb') as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2  # int16 = 2 bytes
            n_frames = wf.getnframes()
            # 至少有 1 个 chunk 的数据（800 samples）
            assert n_frames >= 800
            # 不应超过预期太多（允许边界误差）
            assert n_frames <= 6400  # 最多 400ms 数据

    def test_stop_is_idempotent(self, tmp_path, fake_sounddevice):
        """stop() 可重复调用不报错"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = AudioSensor(controller=controller)
        sensor.start()
        sensor.stop()
        sensor.stop()  # 重复调用
        controller.stop()

    def test_stop_without_start_does_not_crash(self, tmp_path, fake_sounddevice):
        """stop() 在未 start() 状态下调用不崩溃"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensor = AudioSensor(controller=controller)
        sensor.stop()  # 不应抛异常

    def test_duration_seconds_calculated_correctly(self, tmp_path, fake_sounddevice):
        """duration_seconds = frame_count / samplerate"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = AudioSensor(controller=controller)
        sensor.start()
        time.sleep(0.25)  # ~200ms 数据
        sensor.stop()
        # duration_seconds 应在 0.1~0.5 秒之间
        assert 0.05 <= sensor.duration_seconds <= 0.5
        controller.stop()


# ==================== AudioSensor + Controller 集成 ====================

class TestAudioSensorIntegration:
    def test_audio_sensor_registered_to_controller(self, tmp_path, fake_sounddevice):
        """AudioSensor 作为 controller 的传感器，start/stop 由 controller 协调"""
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[AudioSensor(controller=None)],
        )
        # 注入 controller
        controller.sensors[0].controller = controller
        controller.start()
        time.sleep(0.2)
        controller.stop()

        # 验证 audio/mic.wav 存在且可读
        assert controller.package.audio_file.exists()
        with wave.open(str(controller.package.audio_file), 'rb') as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 16000
            assert wf.getnframes() > 0

    def test_audio_wav_readable_by_standard_player(self, tmp_path, fake_sounddevice):
        """WAV 文件格式标准，能被 wave 模块读回所有参数"""
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[AudioSensor(controller=None)],
        )
        controller.sensors[0].controller = controller
        controller.start()
        time.sleep(0.15)
        controller.stop()

        with wave.open(str(controller.package.audio_file), 'rb') as wf:
            # 标准 WAV 格式参数
            assert wf.getnchannels() == 1  # mono
            assert wf.getframerate() == 16000  # 16kHz
            assert wf.getsampwidth() == 2  # int16 = 2 bytes
            # 能读出数据
            data = wf.readframes(wf.getnframes())
            assert len(data) > 0
            assert len(data) % 2 == 0  # int16 = 2 bytes/sample

    def test_audio_sensor_does_not_write_to_events_jsonl(self, tmp_path, fake_sounddevice):
        """音频数据不写 events.jsonl（直接写 WAV），但启动/停止可上报事件"""
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[AudioSensor(controller=None)],
        )
        controller.sensors[0].controller = controller
        controller.start()
        time.sleep(0.15)
        controller.stop()

        # events.jsonl 应为空（AudioSensor 不调 emit_event 写音频数据）
        events = controller.package.read_events()
        assert len(events) == 0
        # 但 WAV 文件有数据
        assert controller.package.audio_file.exists()
        assert controller.package.audio_file.stat().st_size > 44  # WAV header ~44 bytes
