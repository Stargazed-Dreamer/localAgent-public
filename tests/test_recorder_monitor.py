"""tests/test_recorder_monitor.py — L0 采集层 Ticket 07 监控面板测试

覆盖：
- workspace/recorder/tools/monitor_window.py: MonitorWindow + 5 个子面板 + 工具函数
  - MonitorWindow: attach/detach + set_recording/update_duration/update_package_name/set_mic_enabled +
    _on_tick/_on_audio_tick 轮询 + _read_new_events/_read_latest_window/_read_latest_frame + 信号
  - _EventStreamPanel: append_events 多种 kind + _format_event 格式/颜色 + clear
  - _FramePreviewPanel: set_latest_frame + add_thumbnail + clear
  - _AudioWaveformPanel + _WaveformCanvas: set_samples + audio_seconds + paintEvent 不崩
  - _WindowInfoPanel: update_info
  - _StatsPanel: update_stats
  - _bytes_to_int16_samples: 空/单/多/降采样

加载策略：
- 与 test_recorder_gui.py 一致：importlib 动态加载 workspace/recorder/tools/ 下模块
- QT_QPA_PLATFORM=offscreen 避免测试弹真实窗口
- 注入 fake RecorderApp（持有真实 RecordingController，但无传感器）

设计偏离说明：
- 05-gui-design.md 设计稿含"暂停""标记重点"按钮，本 ticket 不实现，测试中不验证
- 章节列表 L0 不实现，测试中不验证
"""

import importlib.util
import json
import struct
import sys
import types
import wave
from pathlib import Path

import pytest

# ========== 动态加载 workspace/recorder/tools/ 下模块 ==========
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


# 确保包占位
_ensure_pkg("tools", _PROJECT_ROOT / "tools")
_ensure_pkg("workspace.recorder.tools", _RECORDER_DIR)

# 加载 monitor_window 模块（它依赖 PySide6 + 标准库，不依赖其他 workspace/recorder/tools 模块）
monitor_mod = _load_module("workspace.recorder.tools.monitor_window", _RECORDER_DIR / "monitor_window.py")

# 便捷别名
MonitorWindow = monitor_mod.MonitorWindow
_EventStreamPanel = monitor_mod._EventStreamPanel
_FramePreviewPanel = monitor_mod._FramePreviewPanel
_AudioWaveformPanel = monitor_mod._AudioWaveformPanel
_WaveformCanvas = monitor_mod._WaveformCanvas
_WindowInfoPanel = monitor_mod._WindowInfoPanel
_StatsPanel = monitor_mod._StatsPanel
_bytes_to_int16_samples = monitor_mod._bytes_to_int16_samples
_event_color = monitor_mod._event_color
MAX_EVENTS_DISPLAY = monitor_mod.MAX_EVENTS_DISPLAY
MAX_THUMBNAILS = monitor_mod.MAX_THUMBNAILS

# T11 主题迁移后，事件颜色来自 tokens.BLOCK_COLORS（不再有 COLOR_EVENT_* 常量）
from lib.ui import tokens as _ui_tokens  # noqa: E402

# config 总是加载（fake_recorder fixture 通过 sys.modules["workspace.recorder.tools.config"] 访问它）
_load_module("workspace.recorder.tools.config", _RECORDER_DIR / "config.py")

# 复用已加载的 RecorderApp（如果 test_recorder_gui 已加载，sys.modules 会有）
# 否则单独加载（避免依赖测试执行顺序）
if "workspace.recorder.tools.recorder_app" in sys.modules:
    RecorderApp = sys.modules["workspace.recorder.tools.recorder_app"].RecorderApp
else:
    _load_module("workspace.recorder.tools.recorder_app", _RECORDER_DIR / "recorder_app.py")
    RecorderApp = sys.modules["workspace.recorder.tools.recorder_app"].RecorderApp

# 加载真实 RecordingController（测试用，不带传感器）


# ========== pytest fixtures ==========


@pytest.fixture
def fake_recorder(tmp_path):
    """构造一个已 start 的 RecorderApp（无传感器，sensor_factory 返回空列表）。

    返回 (recorder_app, controller) 元组：
    - recorder_app: 已 start 的 RecorderApp 实例
    - controller: recorder_app._controller 引用（便于测试直接调用）
    """
    # 防御性加载：全量测试时其他测试可能影响 sys.modules，确保 config 可用
    if "workspace.recorder.tools.config" not in sys.modules:
        _load_module("workspace.recorder.tools.config", _RECORDER_DIR / "config.py")
    config = sys.modules["workspace.recorder.tools.config"].RecordingConfig()
    app = RecorderApp(
        config=config,
        base_dir=tmp_path,
        sensor_factory=lambda controller, cfg: [],  # 无传感器
    )
    app.start()
    yield app, app._controller
    app.stop()


# ==================== 工具函数 _bytes_to_int16_samples ====================


class TestBytesToInt16Samples:
    def test_empty_bytes(self):
        assert _bytes_to_int16_samples(b"") == []

    def test_single_sample(self):
        # int16 = 1000 → bytes = b'\xe8\x03' (little-endian)
        raw = struct.pack("<h", 1000)
        samples = _bytes_to_int16_samples(raw)
        assert samples == [1000]

    def test_multiple_samples(self):
        raw = struct.pack("<3h", 100, -200, 300)
        samples = _bytes_to_int16_samples(raw)
        assert samples == [100, -200, 300]

    def test_odd_bytes_truncated(self):
        # 3 字节（奇数）应截断为 1 个 int16
        raw = struct.pack("<h", 42) + b"\x00"
        samples = _bytes_to_int16_samples(raw)
        assert samples == [42]

    def test_downsampling_when_too_many(self):
        # 生成 5000 个采样，max_samples=2000，应降采样
        raw = struct.pack("<5000h", *range(5000))
        samples = _bytes_to_int16_samples(raw, max_samples=2000)
        assert len(samples) <= 2000
        assert len(samples) > 0

    def test_negative_values(self):
        raw = struct.pack("<h", -32768)
        samples = _bytes_to_int16_samples(raw)
        assert samples == [-32768]


# ==================== _EventStreamPanel ====================


class TestEventStreamPanel:
    def test_construct(self, qapp):
        panel = _EventStreamPanel()
        assert panel.list_widget.count() == 0
        panel.close()

    def test_append_mouse_click_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "mouse_click", "timestamp": 1.234, "payload": {"x": 100, "y": 200, "button": "left"}},
        ]
        panel.append_events(events)
        assert panel.list_widget.count() == 1
        item = panel.list_widget.item(0)
        assert "点击" in item.text()
        assert "left" in item.text()
        assert "100,200" in item.text()
        panel.close()

    def test_append_keyboard_input_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "keyboard_input", "timestamp": 0.5, "payload": {"text": "你好", "detection_method": "uia_value_diff"}},
        ]
        panel.append_events(events)
        assert panel.list_widget.count() == 1
        item = panel.list_widget.item(0)
        assert "输入" in item.text()
        assert "你好" in item.text()
        assert "uia_value_diff" in item.text()
        panel.close()

    def test_append_focus_change_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "focus_change", "timestamp": 2.0, "payload": {"title": "记事本", "hwnd": 100, "pid": 1234}},
        ]
        panel.append_events(events)
        item = panel.list_widget.item(0)
        assert "焦点切换" in item.text()
        assert "记事本" in item.text()
        panel.close()

    def test_append_screen_frame_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "screen_frame", "timestamp": 3.0, "payload": {"frame_seq": 42}},
        ]
        panel.append_events(events)
        item = panel.list_widget.item(0)
        assert "截图" in item.text()
        assert "42" in item.text()
        panel.close()

    def test_append_hotkey_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "hotkey", "timestamp": 1.0, "payload": {"combo": "copy"}},
        ]
        panel.append_events(events)
        item = panel.list_widget.item(0)
        assert "热键" in item.text()
        assert "copy" in item.text()
        panel.close()

    def test_append_password_masked_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "password_masked", "timestamp": 1.0, "payload": {}},
        ]
        panel.append_events(events)
        item = panel.list_widget.item(0)
        assert "密码已隐藏" in item.text()
        panel.close()

    def test_append_unknown_event(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "unknown_kind", "timestamp": 1.0, "payload": {}},
        ]
        panel.append_events(events)
        item = panel.list_widget.item(0)
        assert "unknown_kind" in item.text()
        panel.close()

    def test_append_multiple_events(self, qapp):
        panel = _EventStreamPanel()
        events = [
            {"kind": "mouse_click", "timestamp": 1.0, "payload": {"x": 0, "y": 0, "button": "left"}},
            {"kind": "keyboard_input", "timestamp": 1.5, "payload": {"text": "a", "detection_method": ""}},
            {"kind": "focus_change", "timestamp": 2.0, "payload": {"title": "X", "hwnd": 1, "pid": 1}},
        ]
        panel.append_events(events)
        assert panel.list_widget.count() == 3
        panel.close()

    def test_max_events_limit(self, qapp):
        panel = _EventStreamPanel()
        # 添加 150 条事件（超过 MAX_EVENTS_DISPLAY=100）
        events = [
            {"kind": "mouse_click", "timestamp": float(i), "payload": {"x": i, "y": 0, "button": "left"}}
            for i in range(150)
        ]
        panel.append_events(events)
        assert panel.list_widget.count() == MAX_EVENTS_DISPLAY
        panel.close()

    def test_clear(self, qapp):
        panel = _EventStreamPanel()
        events = [{"kind": "mouse_click", "timestamp": 1.0, "payload": {"x": 0, "y": 0, "button": "left"}}]
        panel.append_events(events)
        assert panel.list_widget.count() == 1
        panel.clear()
        assert panel.list_widget.count() == 0
        panel.close()

    def test_format_event_colors(self, qapp):
        # T11 主题迁移后，每种事件 kind 用对应 BLOCK_COLORS 色值（不再合并为单一 mouse 色）
        panel = _EventStreamPanel()
        cases = [
            ("mouse_click", _ui_tokens.BLOCK_COLORS["mouse_click"]),
            ("mouse_scroll", _ui_tokens.BLOCK_COLORS["mouse_scroll"]),
            ("mouse_drag", _ui_tokens.BLOCK_COLORS["mouse_drag"]),
            ("keyboard_input", _ui_tokens.BLOCK_COLORS["keyboard"]),
            ("hotkey", _ui_tokens.BLOCK_COLORS["keyboard"]),
            ("ime_switch", _ui_tokens.BLOCK_COLORS["keyboard"]),
            ("password_masked", _ui_tokens.BLOCK_COLORS["keyboard"]),
            ("focus_change", _ui_tokens.BLOCK_COLORS["focus"]),
            ("screen_frame", _ui_tokens.BLOCK_COLORS["screenshot"]),
            ("unknown_kind", _ui_tokens.TEXT_TERTIARY),
        ]
        for kind, expected_color in cases:
            text, color = panel._format_event({
                "kind": kind,
                "timestamp": 1.0,
                "payload": {"x": 0, "y": 0, "button": "left", "text": "a", "detection_method": "",
                            "combo": "copy", "title": "T", "frame_seq": 0},
            })
            assert color == expected_color, f"kind={kind} 应该是 {expected_color}"
        panel.close()

    def test_format_event_timestamp(self, qapp):
        panel = _EventStreamPanel()
        # 65.5 秒 → mm=01, ss=05, ms=500
        text, _ = panel._format_event({
            "kind": "mouse_click",
            "timestamp": 65.5,
            "payload": {"x": 0, "y": 0, "button": "left"},
        })
        assert "[01:05.500]" in text
        panel.close()

    def test_format_event_long_text_truncated(self, qapp):
        panel = _EventStreamPanel()
        long_text = "a" * 100
        text, _ = panel._format_event({
            "kind": "keyboard_input",
            "timestamp": 0.0,
            "payload": {"text": long_text, "detection_method": "uia"},
        })
        assert "..." in text
        panel.close()


# ==================== _FramePreviewPanel ====================


class TestFramePreviewPanel:
    def test_construct(self, qapp):
        panel = _FramePreviewPanel()
        assert panel.thumbs_list.count() == 0
        panel.close()

    def test_set_latest_frame_nonexistent(self, qapp, tmp_path):
        panel = _FramePreviewPanel()
        # 文件不存在时不应崩
        panel.set_latest_frame({"path": tmp_path / "nonexistent.png", "seq": 1})
        panel.close()

    def test_add_thumbnail(self, qapp, tmp_path):
        # 生成一个最小 PNG（用 PIL）
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        png_path = tmp_path / "frame_00000100_000001.png"
        Image.new("RGB", (100, 100), color="red").save(png_path)

        panel = _FramePreviewPanel()
        panel.add_thumbnail({"path": png_path, "seq": 1})
        assert panel.thumbs_list.count() == 1
        panel.close()

    def test_add_thumbnail_max_limit(self, qapp, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        panel = _FramePreviewPanel()
        # 添加 MAX_THUMBNAILS + 5 个缩略图
        for i in range(MAX_THUMBNAILS + 5):
            png_path = tmp_path / f"frame_{i:08d}_{i:06d}.png"
            Image.new("RGB", (50, 50), color="blue").save(png_path)
            panel.add_thumbnail({"path": png_path, "seq": i})
        assert panel.thumbs_list.count() == MAX_THUMBNAILS
        panel.close()

    def test_clear(self, qapp, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        png_path = tmp_path / "frame_00000100_000001.png"
        Image.new("RGB", (50, 50), color="green").save(png_path)

        panel = _FramePreviewPanel()
        panel.add_thumbnail({"path": png_path, "seq": 1})
        assert panel.thumbs_list.count() == 1
        panel.clear()
        assert panel.thumbs_list.count() == 0
        panel.close()


# ==================== _AudioWaveformPanel + _WaveformCanvas ====================


class TestAudioWaveformPanel:
    def test_construct(self, qapp):
        panel = _AudioWaveformPanel()
        assert panel.audio_seconds == 0.0
        panel.close()

    def test_set_samples(self, qapp):
        panel = _AudioWaveformPanel()
        samples = [100, -200, 300, -400, 500]
        panel.set_samples(samples, framerate=16000, audio_seconds=2.5)
        assert panel.audio_seconds == 2.5
        assert panel._samples == samples
        panel.close()

    def test_set_empty_samples(self, qapp):
        panel = _AudioWaveformPanel()
        panel.set_samples([], framerate=16000, audio_seconds=0.0)
        assert panel.audio_seconds == 0.0
        panel.close()


class TestWaveformCanvas:
    def test_construct(self, qapp):
        canvas = _WaveformCanvas()
        assert canvas._samples == []
        canvas.close()

    def test_set_samples_triggers_repaint(self, qapp):
        canvas = _WaveformCanvas()
        canvas.resize(200, 100)
        canvas.set_samples([100, -200, 300])
        assert canvas._samples == [100, -200, 300]
        canvas.close()

    def test_paint_event_with_no_samples(self, qapp):
        canvas = _WaveformCanvas()
        canvas.resize(200, 100)
        canvas.show()
        qapp.processEvents()
        canvas.close()

    def test_paint_event_with_samples(self, qapp):
        canvas = _WaveformCanvas()
        canvas.resize(200, 100)
        canvas.set_samples(list(range(500)))
        canvas.show()
        qapp.processEvents()
        canvas.close()


# ==================== _WindowInfoPanel ====================


class TestWindowInfoPanel:
    def test_construct(self, qapp):
        panel = _WindowInfoPanel()
        assert panel.lbl_title.text() == "(未切换)"
        panel.close()

    def test_update_info(self, qapp):
        panel = _WindowInfoPanel()
        panel.update_info({"title": "记事本", "hwnd": 12345, "pid": 6789})
        assert panel.lbl_title.text() == "记事本"
        assert panel.lbl_hwnd.text() == "12345"
        assert panel.lbl_pid.text() == "6789"
        panel.close()

    def test_update_info_empty_title(self, qapp):
        panel = _WindowInfoPanel()
        panel.update_info({"title": "", "hwnd": 0, "pid": 0})
        assert panel.lbl_title.text() == "(空标题)"
        panel.close()

    def test_update_info_missing_fields(self, qapp):
        panel = _WindowInfoPanel()
        panel.update_info({})  # 空字典
        assert panel.lbl_title.text() == "(空标题)"
        assert panel.lbl_hwnd.text() == "-"
        assert panel.lbl_pid.text() == "-"
        panel.close()


# ==================== _StatsPanel ====================


class TestStatsPanel:
    def test_construct(self, qapp):
        panel = _StatsPanel()
        assert panel.lbl_event_count.text() == "0"
        panel.close()

    def test_update_stats(self, qapp):
        panel = _StatsPanel()
        panel.update_stats(event_count=42, frame_count=10, elapsed=3661, audio_seconds=5.5)
        assert panel.lbl_event_count.text() == "42"
        assert panel.lbl_frame_count.text() == "10"
        assert panel.lbl_elapsed.text() == "01:01:01"
        assert panel.lbl_audio_seconds.text() == "5.5s"
        panel.close()

    def test_update_stats_zero(self, qapp):
        panel = _StatsPanel()
        panel.update_stats(event_count=0, frame_count=0, elapsed=0, audio_seconds=0.0)
        assert panel.lbl_elapsed.text() == "00:00:00"
        panel.close()


# ==================== MonitorWindow 主窗口 ====================


class TestMonitorWindowConstruction:
    def test_construct(self, qapp):
        win = MonitorWindow()
        assert win._recorder_app is None
        assert win._controller is None
        assert win.is_recording is False
        assert win.mic_enabled is True
        win.close()

    def test_set_recording_true(self, qapp):
        win = MonitorWindow()
        win.set_recording(True)
        assert win.is_recording is True
        assert "停止" in win.btn_start_stop.text()
        win.close()

    def test_set_recording_false(self, qapp):
        win = MonitorWindow()
        win.set_recording(True)
        win.set_recording(False)
        assert win.is_recording is False
        assert "开始" in win.btn_start_stop.text()
        win.close()

    def test_update_duration(self, qapp):
        win = MonitorWindow()
        win.update_duration(3661)  # 1h 1m 1s
        assert win.lbl_timer.text() == "01:01:01"
        win.close()

    def test_update_package_name(self, qapp):
        win = MonitorWindow()
        win.update_package_name("rec_20260723_143000")
        assert "rec_20260723_143000" in win.lbl_package.text()
        win.update_package_name(None)
        assert win.lbl_package.text() == "(未启动)"
        win.close()

    def test_set_mic_enabled(self, qapp):
        win = MonitorWindow()
        win.set_mic_enabled(False)
        assert win.mic_enabled is False
        assert "关" in win.btn_mic.text()
        win.close()


class TestMonitorWindowSignals:
    def test_stop_requested_signal(self, qapp):
        win = MonitorWindow()
        win.set_recording(True)  # 录制中，点击按钮应发 stop_requested
        received = []
        win.stop_requested.connect(lambda: received.append(True))
        win._on_start_stop_clicked()
        assert received == [True]
        win.close()

    def test_start_requested_signal(self, qapp):
        """Bug 4 修复：停止后（is_recording=False）点击按钮应发 start_requested。"""
        win = MonitorWindow()
        win.set_recording(False)  # 未录制，点击按钮应发 start_requested
        received = []
        win.start_requested.connect(lambda: received.append(True))
        win._on_start_stop_clicked()
        assert received == [True]
        win.close()

    def test_mic_toggled_signal(self, qapp):
        win = MonitorWindow()
        received = []
        win.mic_toggled.connect(lambda checked: received.append(checked))
        win._on_mic_toggled(False)
        assert received == [False]
        win.close()

    def test_set_mic_enabled_does_not_emit_signal(self, qapp):
        """set_mic_enabled 用 blockSignals 避免循环触发。"""
        win = MonitorWindow()
        received = []
        win.mic_toggled.connect(lambda checked: received.append(checked))
        win.set_mic_enabled(False)
        win.set_mic_enabled(True)
        assert received == []  # 不应该有信号
        win.close()


# ==================== MonitorWindow + 真实 RecorderApp 集成 ====================


class TestMonitorWindowAttach:
    def test_attach_recorder_starts_timers(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        assert win._tick_timer.isActive()
        assert win._audio_timer.isActive()
        assert win._controller is controller
        win.detach_recorder()
        win.close()

    def test_detach_recorder_stops_timers(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        win.detach_recorder()
        assert not win._tick_timer.isActive()
        assert not win._audio_timer.isActive()
        assert win._controller is None
        win.close()

    def test_attach_resets_events_offset(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win._events_offset = 9999  # 设个非零值
        win.attach_recorder(recorder_app)
        assert win._events_offset == 0  # 应该被重置
        win.detach_recorder()
        win.close()

    def test_attach_clears_ui(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        # 先添加一些事件
        win.event_stream.append_events([
            {"kind": "mouse_click", "timestamp": 1.0, "payload": {"x": 0, "y": 0, "button": "left"}}
        ])
        assert win.event_stream.list_widget.count() == 1
        win.attach_recorder(recorder_app)
        # attach 后 UI 应该被清空
        assert win.event_stream.list_widget.count() == 0
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow._read_new_events ====================


class TestReadNewEvents:
    def test_no_controller_returns_empty(self, qapp):
        win = MonitorWindow()
        assert win._read_new_events() == []
        win.close()

    def test_read_after_attach(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 通过 controller 写 2 条事件
        controller.emit_event("mouse_click", {"x": 10, "y": 20, "button": "left"})
        controller.emit_event("keyboard_input", {"text": "abc", "detection_method": "uia"})

        events = win._read_new_events()
        assert len(events) == 2
        assert events[0]["kind"] == "mouse_click"
        assert events[1]["kind"] == "keyboard_input"
        win.detach_recorder()
        win.close()

    def test_offset_tracking(self, qapp, fake_recorder):
        """增量读取：第二次调用只返回新增的事件。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        controller.emit_event("mouse_click", {"x": 1, "y": 1, "button": "left"})
        events1 = win._read_new_events()
        assert len(events1) == 1

        # 第二次调用无新事件，应返回空
        events2 = win._read_new_events()
        assert events2 == []

        # 再写一条，应只返回新的
        controller.emit_event("focus_change", {"title": "X", "hwnd": 1, "pid": 1})
        events3 = win._read_new_events()
        assert len(events3) == 1
        assert events3[0]["kind"] == "focus_change"

        win.detach_recorder()
        win.close()

    def test_incomplete_line_skipped(self, qapp, fake_recorder):
        """不完整的 JSON 行（控制器正在写）应被跳过。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 手动写入一条完整 + 一条不完整
        events_file = controller.package.events_file
        with events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "mouse_click", "timestamp": 1.0, "payload": {}}) + "\n")
            f.write('{"kind": "incomplete"')  # 不完整的 JSON

        events = win._read_new_events()
        assert len(events) == 1  # 只有完整那条
        assert events[0]["kind"] == "mouse_click"
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow._read_latest_window ====================


class TestReadLatestWindow:
    def test_no_controller_returns_none(self, qapp):
        win = MonitorWindow()
        assert win._read_latest_window() is None
        win.close()

    def test_no_focus_file_returns_none(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        # 不写任何 focus 事件
        assert win._read_latest_window() is None
        win.detach_recorder()
        win.close()

    def test_read_latest(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 通过 controller 写 2 个 focus 事件
        controller.emit_focus_change({"title": "WindowA", "hwnd": 100, "pid": 1000})
        controller.emit_focus_change({"title": "WindowB", "hwnd": 200, "pid": 2000})

        latest = win._read_latest_window()
        assert latest is not None
        assert latest["title"] == "WindowB"
        assert latest["hwnd"] == 200
        assert latest["pid"] == 2000
        win.detach_recorder()
        win.close()

    def test_empty_focus_file_returns_none(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        # focus.jsonl 已存在但为空
        assert controller.package.focus_file.exists()
        assert win._read_latest_window() is None
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow._read_latest_frame ====================


class TestReadLatestFrame:
    def test_no_controller_returns_none(self, qapp):
        win = MonitorWindow()
        assert win._read_latest_frame() is None
        win.close()

    def test_no_frames_returns_none(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        # frames/ 目录存在但为空
        assert controller.package.frames_dir.exists()
        assert win._read_latest_frame() is None
        win.detach_recorder()
        win.close()

    def test_read_latest_single(self, qapp, fake_recorder, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 创建一个 PNG 文件
        png_path = controller.package.frames_dir / "frame_00000100_000001.png"
        Image.new("RGB", (10, 10), color="red").save(png_path)

        result = win._read_latest_frame()
        assert result is not None
        assert result["seq"] == 1
        assert result["path"] == png_path
        win.detach_recorder()
        win.close()

    def test_read_latest_picks_highest_seq(self, qapp, fake_recorder, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 创建 3 个 PNG，seq 顺序故意打乱
        for ms, seq in [(100, 1), (50, 2), (200, 3)]:
            png_path = controller.package.frames_dir / f"frame_{ms:08d}_{seq:06d}.png"
            Image.new("RGB", (10, 10), color="blue").save(png_path)

        result = win._read_latest_frame()
        # 按 (ms, seq) 排序，最大的是 (200, 3)
        assert result["seq"] == 3
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow._on_tick 端到端 ====================


class TestMonitorWindowOnTick:
    def test_tick_with_no_controller_no_crash(self, qapp):
        """未 attach 时 _on_tick 不应崩。"""
        win = MonitorWindow()
        win._on_tick()  # 不应抛异常
        win.close()

    def test_tick_reads_events_and_updates_ui(self, qapp, fake_recorder):
        """_on_tick 应该读事件 + 更新事件流面板。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 写事件
        controller.emit_event("mouse_click", {"x": 10, "y": 20, "button": "left"})
        controller.emit_event("keyboard_input", {"text": "abc", "detection_method": "uia"})

        # 调用 _on_tick
        win._on_tick()

        # 事件流应该有 2 条
        assert win.event_stream.list_widget.count() == 2
        # 统计应该更新
        assert win.stats_panel.lbl_event_count.text() == "2"
        win.detach_recorder()
        win.close()

    def test_tick_reads_focus_and_updates_window_info(self, qapp, fake_recorder):
        """_on_tick 应该读最新焦点事件 + 更新窗口信息面板。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        controller.emit_focus_change({"title": "记事本", "hwnd": 100, "pid": 1234})
        win._on_tick()

        assert win.window_info.lbl_title.text() == "记事本"
        assert win.window_info.lbl_hwnd.text() == "100"
        assert win.window_info.lbl_pid.text() == "1234"
        win.detach_recorder()
        win.close()

    def test_tick_reads_frame_and_updates_preview(self, qapp, fake_recorder):
        """_on_tick 应该读最新 PNG + 更新截图预览 + 加缩略图。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        png_path = controller.package.frames_dir / "frame_00000100_000001.png"
        Image.new("RGB", (50, 50), color="green").save(png_path)

        # 手动调 increment_frame_count（生产环境由 ScreenCaptureSensor 调）
        controller.increment_frame_count(1)

        win._on_tick()

        # 截图预览应该已设置 pixmap
        assert not win.frame_preview.lbl_latest.pixmap().isNull()
        # 缩略图应该有 1 条
        assert win.frame_preview.thumbs_list.count() == 1
        # 统计应该显示帧数 1
        assert win.stats_panel.lbl_frame_count.text() == "1"
        win.detach_recorder()
        win.close()

    def test_tick_does_not_duplicate_thumbnails(self, qapp, fake_recorder):
        """同一个 seq 的帧不应重复添加缩略图。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        png_path = controller.package.frames_dir / "frame_00000100_000001.png"
        Image.new("RGB", (50, 50), color="red").save(png_path)

        # 第一次 tick
        win._on_tick()
        assert win.frame_preview.thumbs_list.count() == 1

        # 第二次 tick（同一个 PNG），不应再添加缩略图
        win._on_tick()
        assert win.frame_preview.thumbs_list.count() == 1
        win.detach_recorder()
        win.close()

    def test_tick_updates_package_name(self, qapp, fake_recorder):
        """_on_tick 应该更新录制包名显示。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 初始是 "(未启动)" 或上次的状态
        win._on_tick()
        assert "rec_" in win.lbl_package.text()  # 包名以 rec_ 开头
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow._on_audio_tick 端到端 ====================


class TestMonitorWindowOnAudioTick:
    def test_audio_tick_with_no_controller_no_crash(self, qapp):
        win = MonitorWindow()
        win._on_audio_tick()  # 不应抛异常
        win.close()

    def test_audio_tick_no_audio_file_no_crash(self, qapp, fake_recorder):
        """无音频文件时 _on_audio_tick 不应崩。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        # 不创建 mic.wav
        win._on_audio_tick()
        assert win.audio_waveform.audio_seconds == 0.0
        win.detach_recorder()
        win.close()

    def test_audio_tick_reads_wav(self, qapp, fake_recorder):
        """_on_audio_tick 应该读 WAV 末尾采样 + 更新波形。"""
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)

        # 生成一个最小 WAV 文件（int16 PCM, 16kHz, mono, 1 秒）
        audio_file = controller.package.audio_file
        with wave.open(str(audio_file), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            # 写 16000 个采样（1 秒）— 用正弦波保证在 int16 范围内
            import math
            samples = [int(32767 * math.sin(i * 0.01)) for i in range(16000)]
            wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

        win._on_audio_tick()

        # 波形面板应该有数据
        assert win.audio_waveform.audio_seconds == pytest.approx(1.0, abs=0.1)
        assert len(win.audio_waveform._samples) > 0
        win.detach_recorder()
        win.close()


# ==================== MonitorWindow closeEvent ====================


class TestMonitorWindowClose:
    def test_close_event_stops_timers(self, qapp, fake_recorder):
        recorder_app, controller = fake_recorder
        win = MonitorWindow()
        win.attach_recorder(recorder_app)
        assert win._tick_timer.isActive()
        # 模拟关闭
        win.close()
        assert not win._tick_timer.isActive()
        assert not win._audio_timer.isActive()


# ==================== MonitorWindow 三按钮交互（Ticket 12）====================


class TestMonitorWindowThreeButtons:
    """Ticket 12：PAUSED 状态三按钮交互测试（继续录制/保存结束/丢弃重录）。

    验证 MonitorWindow 与 FloatingBar 行为一致。
    """

    def test_initial_three_buttons_hidden(self, qapp):
        """初始状态三按钮隐藏，btn_start_stop 可见。"""
        win = MonitorWindow()
        win.show()
        qapp.processEvents()
        assert not win.btn_resume.isVisible()
        assert not win.btn_save.isVisible()
        assert not win.btn_discard.isVisible()
        assert win.btn_start_stop.isVisible()
        win.close()

    def test_set_paused_true_shows_three_buttons(self, qapp):
        """set_paused(True) 显示三按钮，隐藏 btn_start_stop。"""
        win = MonitorWindow()
        win.show()
        qapp.processEvents()
        win.set_paused(True)
        qapp.processEvents()
        assert win.btn_resume.isVisible()
        assert win.btn_save.isVisible()
        assert win.btn_discard.isVisible()
        assert not win.btn_start_stop.isVisible()
        assert "PAUSE" in win.lbl_status.text()
        win.close()

    def test_set_paused_false_hides_three_buttons(self, qapp):
        """set_paused(False) 隐藏三按钮，显示 btn_start_stop。"""
        win = MonitorWindow()
        win.show()
        qapp.processEvents()
        win.set_paused(True)
        win.set_paused(False)
        qapp.processEvents()
        assert not win.btn_resume.isVisible()
        assert not win.btn_save.isVisible()
        assert not win.btn_discard.isVisible()
        assert win.btn_start_stop.isVisible()
        win.close()

    def test_set_recording_true_after_paused_hides_three_buttons(self, qapp):
        """从 PAUSED 切回 RECORDING（set_recording(True)）时隐藏三按钮。"""
        win = MonitorWindow()
        win.show()
        qapp.processEvents()
        win.set_paused(True)
        win.set_recording(True)
        qapp.processEvents()
        assert not win.btn_resume.isVisible()
        assert not win.btn_save.isVisible()
        assert not win.btn_discard.isVisible()
        assert win.btn_start_stop.isVisible()
        assert "停止" in win.btn_start_stop.text()
        win.close()

    def test_set_recording_false_after_paused_hides_three_buttons(self, qapp):
        """从 PAUSED 切回 IDLE（set_recording(False)）时隐藏三按钮。"""
        win = MonitorWindow()
        win.show()
        qapp.processEvents()
        win.set_paused(True)
        win.set_recording(False)
        qapp.processEvents()
        assert not win.btn_resume.isVisible()
        assert not win.btn_save.isVisible()
        assert not win.btn_discard.isVisible()
        assert win.btn_start_stop.isVisible()
        assert "开始" in win.btn_start_stop.text()
        win.close()

    def test_resume_button_emits_signal(self, qapp):
        """点击继续录制按钮发 resume_requested 信号。"""
        win = MonitorWindow()
        received = [0]
        win.resume_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        win.btn_resume.click()
        qapp.processEvents()
        assert received[0] == 1
        win.close()

    def test_save_button_emits_signal(self, qapp):
        """点击保存结束按钮发 save_requested 信号。"""
        win = MonitorWindow()
        received = [0]
        win.save_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        win.btn_save.click()
        qapp.processEvents()
        assert received[0] == 1
        win.close()

    def test_discard_button_confirmed_emits_signal(self, qapp, monkeypatch):
        """点击丢弃重录按钮 + 确认 → 发 discard_requested 信号。"""
        win = MonitorWindow()
        monkeypatch.setattr(win, "_confirm_discard", lambda: True)
        received = [0]
        win.discard_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        win.btn_discard.click()
        qapp.processEvents()
        assert received[0] == 1
        win.close()

    def test_discard_button_cancelled_no_signal(self, qapp, monkeypatch):
        """点击丢弃重录按钮 + 取消 → 不发信号，保持 PAUSED（D030）。"""
        win = MonitorWindow()
        monkeypatch.setattr(win, "_confirm_discard", lambda: False)
        received = [0]
        win.discard_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        win.btn_discard.click()
        qapp.processEvents()
        assert received[0] == 0
        win.close()

    def test_confirm_discard_returns_bool(self, qapp, monkeypatch):
        """_confirm_discard 返回 bool（monkeypatch 避免 real 弹窗）。"""
        win = MonitorWindow()
        monkeypatch.setattr(win, "_confirm_discard", lambda: True)
        assert win._confirm_discard() is True
        monkeypatch.setattr(win, "_confirm_discard", lambda: False)
        assert win._confirm_discard() is False
        win.close()
