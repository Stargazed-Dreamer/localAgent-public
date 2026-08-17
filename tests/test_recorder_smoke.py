"""tests/test_recorder_smoke.py — L0 采集层 Ticket 08 端到端冒烟测试

设计：
- 用 MockSensor + 短时长（3 秒）模拟 3 分钟端到端录制（避免 CI 跑 3 分钟）
- 验证录制包所有数据文件可解析：
  - events.jsonl JSON 合法 + 按时间戳排序
  - frames/ 下 PNG 可被 PIL 打开
  - audio/mic.wav 可被 wave 模块读回
  - focus.jsonl JSON 合法
  - meta.json 含开始/结束时间/时长/事件数/帧数/版本号

覆盖 Ticket 08 acceptance criteria:
- [x] 3 分钟端到端冒烟测试脚本（本测试用 3 秒模拟，real 3 分钟可由 main.py --no-gui --max-duration 180 触发）
- [x] 冒烟测试验证：events.jsonl JSON 合法 + 按时间戳排序
- [x] 冒烟测试验证：frames/ 下 PNG 可被 PIL 打开
- [x] 冒烟测试验证：audio/mic.wav 可被 wave 模块读回
- [x] 冒烟测试验证：focus.jsonl JSON 合法
- [x] 冒烟测试验证：meta.json 含开始/结束时间/时长/事件数/帧数/版本号
"""

import importlib.util
import json
import os
import struct
import sys
import types
import wave
from pathlib import Path

# Qt offscreen 模式（必须在 import PySide6 之前设置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# ========== 动态加载 workspace/recorder/tools/ 下模块（与 test_recorder_gui.py 一致） ==========

_PROJECT_ROOT = Path(__file__).parent.parent
_RECORDER_DIR = _PROJECT_ROOT / "workspace" / "recorder" / "tools"


def _ensure_pkg(name: str, path: Path) -> None:
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(path)]
        sys.modules[name] = pkg


def _load_module(module_name: str, file_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_ensure_pkg("tools", _PROJECT_ROOT / "tools")
_ensure_pkg("workspace.recorder.tools", _RECORDER_DIR)

# 按依赖顺序加载
config_mod = _load_module("workspace.recorder.tools.config", _RECORDER_DIR / "config.py")
config_loader_mod = _load_module("workspace.recorder.tools.config_loader", _RECORDER_DIR / "config_loader.py")
recorder_app_mod = _load_module("workspace.recorder.tools.recorder_app", _RECORDER_DIR / "recorder_app.py")

RecordingConfig = config_mod.RecordingConfig
get_recording_config = config_loader_mod.get_recording_config
resolve_base_dir = config_loader_mod.resolve_base_dir
RecorderApp = recorder_app_mod.RecorderApp


# ========== Smoke test 用的 Mock 传感器（生成所有类型的事件 + 帧 + 音频） ==========


class _SmokeMockSensor:
    """冒烟测试 Mock 传感器：start 时同步发多种事件 + 写假帧 + 写假音频。

    覆盖所有数据类型：
    - keyboard_input / mouse_click / focus_change / screen_frame / hotkey 事件
    - 假 PNG 帧（写一个最小合法 PNG）
    - 假 WAV 音频（写一个最小合法 WAV）
    """

    def __init__(self, controller, events=None):
        self.controller = controller
        self.events = events or []
        self._stopped = False

    def start(self):
        # 发预设事件
        for kind, payload in self.events:
            if kind == "focus_change":
                self.controller.emit_focus_change(payload)
            else:
                self.controller.emit_event(kind, payload)
                # mouse_click 同时触发假帧
                if kind == "mouse_click":
                    self._write_fake_frame()
                    self.controller.increment_frame_count(1)

        # 写一个假 WAV 音频文件
        self._write_fake_wav()

    def stop(self):
        self._stopped = True

    def _write_fake_frame(self) -> None:
        """写一个最小合法 PNG 到 frames/ 目录。"""
        import struct as _struct
        import zlib

        frames_dir = self.controller.package.frames_dir
        frames_dir.mkdir(parents=True, exist_ok=True)
        seq = self.controller.frame_count
        frame_path = frames_dir / f"frame_00000000_{seq:06d}.png"

        # 最小合法 PNG：1x1 像素 RGB（手写 PNG 字节避免依赖 PIL 写盘）
        # PNG signature
        sig = b"\x89PNG\r\n\x1a\n"
        # IHDR chunk: width=1, height=1, bit_depth=8, color_type=2 (RGB)
        ihdr_data = _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
        ihdr = _struct.pack(">I", 13) + b"IHDR" + ihdr_data + _struct.pack(">I", ihdr_crc)
        # IDAT chunk: 1 行 = filter byte (0) + 3 bytes RGB
        raw = b"\x00\xff\x00\x00"  # filter=0 + R=255 G=0 B=0
        compressed = zlib.compress(raw)
        idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
        idat = _struct.pack(">I", len(compressed)) + b"IDAT" + compressed + _struct.pack(">I", idat_crc)
        # IEND chunk
        iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
        iend = _struct.pack(">I", 0) + b"IEND" + _struct.pack(">I", iend_crc)

        frame_path.write_bytes(sig + ihdr + idat + iend)

    def _write_fake_wav(self) -> None:
        """写一个最小合法 WAV 到 audio/ 目录。"""
        audio_dir = self.controller.package.audio_dir
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav_path = self.controller.package.audio_file

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            # 写 16000 个采样（1 秒）— 正弦波
            import math
            samples = [int(32767 * math.sin(i * 0.01)) for i in range(16000)]
            wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _smoke_sensor_factory(controller, config):
    """冒烟测试 sensor_factory：返回一个 _SmokeMockSensor，发预设事件。"""
    events = [
        ("keyboard_input", {"text": "hello", "physical_keys": ["h", "e", "l", "l", "o"]}),
        ("mouse_click", {"x": 100, "y": 200, "button": "left", "clicks": 1}),
        ("focus_change", {"title": "Notepad", "hwnd": 12345, "pid": 1000}),
        ("hotkey", {"combo": "copy"}),
        ("mouse_click", {"x": 300, "y": 400, "button": "right", "clicks": 1}),
        ("keyboard_input", {"text": "world", "physical_keys": ["w", "o", "r", "l", "d"]}),
        ("focus_change", {"title": "Browser", "hwnd": 67890, "pid": 2000}),
    ]
    return [_SmokeMockSensor(controller=controller, events=events)]


# ========== 冒烟测试 ==========


class TestSmokeRecording:
    """端到端冒烟测试：3 秒录制 + 验证所有数据文件可解析。"""

    def test_smoke_recording_produces_valid_package(self, tmp_path):
        """3 秒冒烟录制 → 验证录制包所有数据文件可解析。"""
        # 构造配置（3 秒自动停止）
        config = RecordingConfig(
            range_type="fullscreen",
            mode="detailed",
            capture_interval=0.5,
            burst_intervals=(0.05, 0.10, 0.15),
            audio_enabled=True,
            max_duration_seconds=3,
        )
        base_dir = tmp_path / "recordings"
        base_dir.mkdir(parents=True, exist_ok=True)

        # 创建 RecorderApp + 注入 smoke sensor_factory
        app = RecorderApp(
            config=config,
            base_dir=base_dir,
            sensor_factory=_smoke_sensor_factory,
        )
        app.start()

        # 等待自动停止（3 秒 + 容差）
        import time
        deadline = time.time() + 10.0
        while app.is_running and time.time() < deadline:
            time.sleep(0.1)
        assert not app.is_running, "录制未在预期时间内自动停止"

        # 获取录制包路径
        package_root = app.recording_package_root
        assert package_root is not None
        assert package_root.exists(), f"录制包目录不存在：{package_root}"

        # ========== 验证 1：events.jsonl JSON 合法 + 按时间戳排序 ==========
        events_file = package_root / "events.jsonl"
        assert events_file.exists(), "events.jsonl 不存在"
        events = []
        with events_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    events.append(ev)
                except json.JSONDecodeError as e:
                    pytest.fail(f"events.jsonl 第 {line_num} 行 JSON 不合法：{e}")

        assert len(events) >= 5, f"事件数太少：{len(events)}（预期 ≥5）"

        # 验证按时间戳排序（单调非递减）
        timestamps = [ev.get("timestamp", 0.0) for ev in events]
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1], (
                f"事件 {i} 时间戳 {timestamps[i]} < 前一个 {timestamps[i-1]}，未按时间戳排序"
            )

        # 验证事件含必要字段
        for i, ev in enumerate(events):
            assert "timestamp" in ev, f"事件 {i} 缺 timestamp 字段"
            assert "kind" in ev, f"事件 {i} 缺 kind 字段"
            assert "payload" in ev, f"事件 {i} 缺 payload 字段"

        # ========== 验证 2：frames/ 下 PNG 可被 PIL 打开 ==========
        frames_dir = package_root / "frames"
        if frames_dir.exists():
            frame_files = sorted(frames_dir.glob("*.png"))
            assert len(frame_files) > 0, "frames/ 目录无 PNG 文件"

            from PIL import Image

            for frame_file in frame_files:
                try:
                    with Image.open(frame_file) as img:
                        img.load()  # 强制读取像素数据
                        assert img.format == "PNG", f"帧 {frame_file.name} 格式不是 PNG：{img.format}"
                except Exception as e:
                    pytest.fail(f"帧 {frame_file.name} 无法被 PIL 打开：{e}")

        # ========== 验证 3：audio/mic.wav 可被 wave 模块读回 ==========
        audio_file = package_root / "audio" / "mic.wav"
        if audio_file.exists():
            try:
                with wave.open(str(audio_file), "rb") as wf:
                    channels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    framerate = wf.getframerate()
                    n_frames = wf.getnframes()
                    frames_data = wf.readframes(n_frames)
                    assert channels >= 1, f"声道数异常：{channels}"
                    assert sampwidth == 2, f"采样宽度应为 2（int16），实际 {sampwidth}"
                    assert framerate > 0, f"采样率异常：{framerate}"
                    assert len(frames_data) > 0, "音频数据为空"
            except Exception as e:
                pytest.fail(f"audio/mic.wav 无法被 wave 模块读回：{e}")

        # ========== 验证 4：focus.jsonl JSON 合法 ==========
        focus_file = package_root / "focus.jsonl"
        assert focus_file.exists(), "focus.jsonl 不存在"
        focus_events = []
        with focus_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    focus_events.append(ev)
                except json.JSONDecodeError as e:
                    pytest.fail(f"focus.jsonl 第 {line_num} 行 JSON 不合法：{e}")

        assert len(focus_events) >= 1, f"focus.jsonl 事件数太少：{len(focus_events)}"
        for i, ev in enumerate(focus_events):
            assert ev.get("kind") == "focus_change", (
                f"focus.jsonl 事件 {i} kind 不是 focus_change：{ev.get('kind')}"
            )

        # ========== 验证 5：meta.json 含开始/结束时间/时长/事件数/帧数/版本号 ==========
        meta_file = package_root / "meta.json"
        assert meta_file.exists(), "meta.json 不存在"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            pytest.fail(f"meta.json JSON 不合法：{e}")

        # 验证必要字段
        required_fields = [
            "package_name",
            "start_time",
            "end_time",
            "duration_seconds",
            "event_count",
            "frame_count",
            "sensors",
            "version",
            "format",
        ]
        for field in required_fields:
            assert field in meta, f"meta.json 缺字段：{field}"

        # 验证字段值合理性
        assert meta["package_name"], "package_name 为空"
        assert meta["start_time"], "start_time 为空"
        assert meta["end_time"], "end_time 为空"
        assert meta["end_time"] >= meta["start_time"], "end_time < start_time"
        assert meta["duration_seconds"] >= 0, f"duration_seconds 异常：{meta['duration_seconds']}"
        assert meta["event_count"] == len(events), (
            f"meta.event_count={meta['event_count']} != 实际事件数 {len(events)}"
        )
        assert meta["frame_count"] >= 0, f"frame_count 异常：{meta['frame_count']}"
        assert isinstance(meta["sensors"], list), "sensors 不是 list"
        assert meta["version"], "version 为空"
        assert meta["format"], "format 为空"


class TestConfigLoader:
    """config_loader.py 单元测试。"""

    def test_get_recording_config_returns_defaults_when_no_config(self, tmp_path):
        """config.toml 不存在时返回默认值。"""
        config = get_recording_config(config_path=tmp_path / "nonexistent.toml")
        assert config["default_mode"] == "small"
        assert config["default_range"] == "fullscreen"
        assert config["default_detail_level"] == "detailed"
        assert config["max_duration_seconds"] == 1800
        assert config["audio_enabled"] is True
        assert config["base_dir"] == "workspace/recorder/recordings"
        assert "capture" in config
        assert "audio" in config
        assert "stt" in config

    def test_get_recording_config_loads_from_toml(self, tmp_path):
        """从 config.toml 加载 [recording] 段。"""
        config_file = tmp_path / "test_config.toml"
        config_file.write_text(
            """
[recording]
default_mode = "large"
default_range = "monitor"
default_monitor_index = 1
max_duration_seconds = 600
audio_enabled = false
base_dir = "/tmp/test_recordings"

[recording.capture]
interval_detailed = 1.0

[recording.audio]
sample_rate = 44100
""",
            encoding="utf-8",
        )
        config = get_recording_config(config_path=config_file)
        assert config["default_mode"] == "large"
        assert config["default_range"] == "monitor"
        assert config["default_monitor_index"] == 1
        assert config["max_duration_seconds"] == 600
        assert config["audio_enabled"] is False
        assert config["base_dir"] == "/tmp/test_recordings"
        # 子段合并（未指定的字段用默认值）
        assert config["capture"]["interval_detailed"] == 1.0
        assert config["capture"]["interval_coarse"] == 2.0  # 默认值
        assert config["audio"]["sample_rate"] == 44100
        assert config["audio"]["channels"] == 1  # 默认值

    def test_get_recording_config_invalid_toml_returns_defaults(self, tmp_path):
        """config.toml 损坏时返回默认值。"""
        config_file = tmp_path / "bad_config.toml"
        config_file.write_text("invalid toml content [[[", encoding="utf-8")
        config = get_recording_config(config_path=config_file)
        assert config["default_mode"] == "small"
        assert config["default_range"] == "fullscreen"

    def test_resolve_base_dir_relative(self):
        """相对路径解析为项目根下的绝对路径。"""
        result = resolve_base_dir("workspace/recorder/recordings")
        assert result.is_absolute()
        assert "workspace" in str(result)
        assert "recordings" in str(result)

    def test_resolve_base_dir_absolute(self):
        """绝对路径直接返回。"""
        result = resolve_base_dir("/tmp/test_recordings")
        assert result.is_absolute()
        # Windows 下 /tmp 会被 Path 解析为当前盘符的 \tmp，仍是绝对路径
        assert "test_recordings" in str(result)

    def test_resolve_base_dir_none_uses_default(self):
        """base_dir=None 时用默认值。"""
        result = resolve_base_dir(None)
        assert result.is_absolute()
        assert "recordings" in str(result)


class TestBuildRecordingConfig:
    """main.py 的 build_recording_config 函数测试。"""

    def test_build_detailed_config(self):
        """detailed 模式构造正确。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        merged = {
            "default_detail_level": "detailed",
            "default_range": "fullscreen",
            "default_monitor_index": 0,
            "default_window_hwnd": 0,
            "max_duration_seconds": 1800,
            "audio_enabled": True,
        }
        cfg = main_mod.build_recording_config(merged)
        assert cfg.mode == "detailed"
        assert cfg.range_type == "fullscreen"
        assert cfg.capture_interval == 0.5
        assert cfg.burst_intervals == (0.05, 0.10, 0.15)
        assert cfg.max_duration_seconds == 1800
        assert cfg.audio_enabled is True

    def test_build_coarse_config(self):
        """coarse 模式构造正确。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        merged = {
            "default_detail_level": "coarse",
            "default_range": "monitor",
            "default_monitor_index": 1,
            "default_window_hwnd": 0,
            "max_duration_seconds": 600,
            "audio_enabled": False,
        }
        cfg = main_mod.build_recording_config(merged)
        assert cfg.mode == "coarse"
        assert cfg.range_type == "monitor"
        assert cfg.monitor_index == 1
        assert cfg.capture_interval == 2.0
        assert cfg.burst_intervals == (0.10, 0.20, 0.30)
        assert cfg.max_duration_seconds == 600
        assert cfg.audio_enabled is False

    def test_build_window_range(self):
        """window 范围构造正确。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        merged = {
            "default_detail_level": "detailed",
            "default_range": "window",
            "default_monitor_index": 0,
            "default_window_hwnd": 12345,
            "max_duration_seconds": 1800,
            "audio_enabled": True,
        }
        cfg = main_mod.build_recording_config(merged)
        assert cfg.range_type == "window"
        assert cfg.window_hwnd == 12345

    def test_build_with_custom_capture_params(self):
        """config.toml 自定义 capture 参数时正确覆盖。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        merged = {
            "default_detail_level": "detailed",
            "default_range": "fullscreen",
            "default_monitor_index": 0,
            "default_window_hwnd": 0,
            "max_duration_seconds": 1800,
            "audio_enabled": True,
            "capture": {
                "interval_detailed": 1.5,
                "burst_intervals_detailed": [0.2, 0.4, 0.6],
            },
        }
        cfg = main_mod.build_recording_config(merged)
        assert cfg.capture_interval == 1.5
        assert cfg.burst_intervals == (0.2, 0.4, 0.6)


class TestMergeCliAndConfig:
    """main.py 的 merge_cli_and_config 函数测试。"""

    def test_cli_overrides_config(self):
        """CLI 参数覆盖 config 值。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        parser = main_mod.build_arg_parser()
        args = parser.parse_args([
            "--mode", "large",
            "--range", "monitor",
            "--monitor-index", "2",
            "--detail-level", "coarse",
            "--max-duration", "300",
            "--no-audio",
        ])
        config_dict = {
            "default_mode": "small",
            "default_range": "fullscreen",
            "default_detail_level": "detailed",
            "default_monitor_index": 0,
            "default_window_hwnd": 0,
            "max_duration_seconds": 1800,
            "audio_enabled": True,
        }
        merged = main_mod.merge_cli_and_config(args, config_dict)
        assert merged["default_mode"] == "large"
        assert merged["default_range"] == "monitor"
        assert merged["default_monitor_index"] == 2
        assert merged["default_detail_level"] == "coarse"
        assert merged["max_duration_seconds"] == 300
        assert merged["audio_enabled"] is False

    def test_cli_none_uses_config(self):
        """CLI 参数为 None 时用 config 值。"""
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        parser = main_mod.build_arg_parser()
        args = parser.parse_args([])  # 不传任何参数
        config_dict = {
            "default_mode": "large",
            "default_range": "monitor",
            "default_detail_level": "coarse",
            "default_monitor_index": 1,
            "default_window_hwnd": 0,
            "max_duration_seconds": 600,
            "audio_enabled": False,
        }
        merged = main_mod.merge_cli_and_config(args, config_dict)
        assert merged["default_mode"] == "large"
        assert merged["default_range"] == "monitor"
        assert merged["default_detail_level"] == "coarse"
        assert merged["max_duration_seconds"] == 600
        assert merged["audio_enabled"] is False


class TestArgParser:
    """CLI 参数解析器测试。"""

    def test_default_args(self):
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        parser = main_mod.build_arg_parser()
        args = parser.parse_args([])
        assert args.mode is None
        assert args.range is None
        assert args.detail_level is None
        assert args.max_duration is None
        assert args.no_audio is False
        assert args.autostart is False
        assert args.no_gui is False

    def test_all_args(self):
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        parser = main_mod.build_arg_parser()
        args = parser.parse_args([
            "--mode", "large",
            "--range", "window",
            "--window-hwnd", "12345",
            "--detail-level", "coarse",
            "--max-duration", "600",
            "--no-audio",
            "--autostart",
            "--base-dir", "/tmp/test",
        ])
        assert args.mode == "large"
        assert args.range == "window"
        assert args.window_hwnd == 12345
        assert args.detail_level == "coarse"
        assert args.max_duration == 600
        assert args.no_audio is True
        assert args.autostart is True
        assert args.base_dir == "/tmp/test"

    def test_invalid_mode_rejected(self):
        main_mod = _load_module("workspace.recorder.tools.main", _RECORDER_DIR / "main.py")
        parser = main_mod.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--mode", "invalid"])
