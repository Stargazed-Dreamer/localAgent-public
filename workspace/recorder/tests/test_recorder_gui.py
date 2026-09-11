"""tests/test_recorder_gui.py — L0 采集层 Ticket 06 GUI 测试

覆盖：
- workspace/recorder/tools/config.py: RecordingConfig dataclass + make_detailed_config/make_coarse_config + list_available_windows
- workspace/recorder/tools/config_dialog.py: ConfigDialog（windows_provider 注入 + 模式选择 + 范围选择 + 参数校验）
- workspace/recorder/tools/hotkey_manager.py: HotkeyManager（listener_factory 注入 + _on_hotkey 直接触发）
- workspace/recorder/tools/floating_bar.py: FloatingBar（set_recording / update_duration / update_event_count / ...）
- workspace/recorder/tools/recorder_app.py: RecorderApp（sensor_factory 注入 fake + 30 分钟自动停止 + 状态访问器 + 端到端）

加载策略：
- tools/ 在 ruff extend-exclude 中，测试文件用 importlib 动态加载各模块并注册到 sys.modules["workspace.recorder.tools.X"]
- 这样模块内部的 `from workspace.recorder.tools.config import ...` 也能从 sys.modules 取到
- 参考 tests/test_folder_classifier.py 的动态加载模式

设计偏离说明：
- 05-gui-design.md 设计稿含"暂停"按钮，本 ticket 不实现暂停（spec user stories 未强制要求）
- 测试中不验证暂停相关行为
"""

import importlib.util
import sys
import time
import types
from pathlib import Path

# ========== 动态加载 workspace/recorder/tools/ 下 5 个模块 ==========
# tools/ 在 ruff extend-exclude 中，用 importlib 加载并注册到 sys.modules
# 这样模块内部的 `from workspace.recorder.tools.config import ...` 也能找到
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RECORDER_DIR = _PROJECT_ROOT / "workspace" / "recorder" / "tools"


def _ensure_pkg(name: str, path: Path) -> None:
    """确保 sys.modules 中存在包占位（用于 tools 和 workspace.recorder.tools）。"""
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(path)]
        sys.modules[name] = pkg


def _load_module(module_name: str, file_path: Path):
    """通过 importlib 加载模块并注册到 sys.modules。"""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 确保包占位（必须在加载子模块之前）
_ensure_pkg("tools", _PROJECT_ROOT / "tools")
_ensure_pkg("workspace.recorder.tools", _RECORDER_DIR)

# 按依赖顺序加载（config 是其他模块的依赖，必须最先）
config_mod = _load_module("workspace.recorder.tools.config", _RECORDER_DIR / "config.py")
config_dialog_mod = _load_module("workspace.recorder.tools.config_dialog", _RECORDER_DIR / "config_dialog.py")
hotkey_mod = _load_module("workspace.recorder.tools.hotkey_manager", _RECORDER_DIR / "hotkey_manager.py")
floating_bar_mod = _load_module("workspace.recorder.tools.floating_bar", _RECORDER_DIR / "floating_bar.py")
recorder_app_mod = _load_module("workspace.recorder.tools.recorder_app", _RECORDER_DIR / "recorder_app.py")

# 便捷别名（测试中直接引用）
RecordingConfig = config_mod.RecordingConfig
make_detailed_config = config_mod.make_detailed_config
make_coarse_config = config_mod.make_coarse_config
list_available_windows = config_mod.list_available_windows
ConfigDialog = config_dialog_mod.ConfigDialog
HotkeyManager = hotkey_mod.HotkeyManager
DEFAULT_HOTKEYS = hotkey_mod.DEFAULT_HOTKEYS
FloatingBar = floating_bar_mod.FloatingBar
RecorderApp = recorder_app_mod.RecorderApp
WARNING_LEAD_SECONDS = recorder_app_mod.WARNING_LEAD_SECONDS


# ========== Fixtures ==========

def _fake_windows_provider(windows=None):
    """构造 fake windows_provider，返回固定窗口列表（避免真实 win32 API）。"""
    windows = windows or []
    return lambda: list(windows)


def _fake_monitors_provider(monitors=None):
    """构造 fake monitors_provider，返回固定显示器列表（避免真实 mss 调用）。

    默认返回 2 个显示器（主屏 + 副屏），测试稳定。
    """
    if monitors is None:
        monitors = [
            {"index": 1, "width": 1920, "height": 1080, "left": 0, "top": 0,
             "is_primary": True, "label": "显示器 1 (1920x1080, 主屏)"},
            {"index": 2, "width": 1920, "height": 1080, "left": 1920, "top": 0,
             "is_primary": False, "label": "显示器 2 (1920x1080, 副屏)"},
        ]
    return lambda: list(monitors)


class _FocusAwareMockSensor:
    """对 focus_change 事件调 emit_focus_change，其它走 emit_event。

    比 MockSensor 更接近真实行为（focus_change 同时写 events.jsonl 和 focus.jsonl）。
    """

    def __init__(self, controller, events):
        self.controller = controller
        self.events = list(events)
        self._stopped = False

    def start(self):
        for kind, payload in self.events:
            if kind == "focus_change":
                self.controller.emit_focus_change(payload)
            else:
                self.controller.emit_event(kind, payload)

    def stop(self):
        self._stopped = True


def _make_fake_sensor_factory(events=None, use_focus_aware=True):
    """构造 fake sensor_factory，返回 MockSensor 列表。"""
    events = events or []

    def factory(controller, config):
        if use_focus_aware:
            return [_FocusAwareMockSensor(controller=controller, events=events)]
        from lib.recorder.sensors.mock import MockSensor
        return [MockSensor(controller=controller, events=events)]

    return factory


# ==================== RecordingConfig 测试 ====================

class TestRecordingConfig:
    def test_default_values(self):
        c = RecordingConfig()
        assert c.range_type == "fullscreen"
        assert c.monitor_index == 0
        assert c.window_hwnd is None
        assert c.mode == "detailed"
        assert c.capture_interval == 0.5
        assert c.burst_intervals == (0.05, 0.10, 0.15)
        assert c.audio_enabled is True
        assert c.uia_enabled is True
        assert c.max_duration_seconds == 1800

    def test_make_detailed_config(self):
        c = make_detailed_config()
        assert c.mode == "detailed"
        assert c.capture_interval == 0.5
        assert c.burst_intervals == (0.05, 0.10, 0.15)
        assert c.audio_enabled is True
        assert c.uia_enabled is True
        assert c.max_duration_seconds == 1800

    def test_make_coarse_config(self):
        c = make_coarse_config()
        assert c.mode == "coarse"
        assert c.capture_interval == 2.0
        assert c.burst_intervals == (0.10, 0.20, 0.30)

    def test_make_detailed_config_with_range(self):
        c = make_detailed_config(range_type="monitor")
        assert c.range_type == "monitor"

    def test_make_coarse_config_with_range(self):
        c = make_coarse_config(range_type="window")
        assert c.range_type == "window"

    def test_custom_config_full_fields(self):
        c = RecordingConfig(
            range_type="window",
            window_hwnd=12345,
            mode="custom",
            capture_interval=1.0,
            burst_intervals=(0.1, 0.2),
            audio_enabled=False,
            uia_enabled=False,
            max_duration_seconds=600,
        )
        assert c.range_type == "window"
        assert c.window_hwnd == 12345
        assert c.mode == "custom"
        assert c.capture_interval == 1.0
        assert c.audio_enabled is False
        assert c.uia_enabled is False
        assert c.max_duration_seconds == 600


# ==================== list_available_windows 测试 ====================

class TestListAvailableWindows:
    def test_returns_list_type(self):
        """总是返回 list（即使内部 _enum_windows 不可用）"""
        result = list_available_windows()
        assert isinstance(result, list)

    def test_returns_empty_on_exception(self, monkeypatch):
        """_enum_windows 抛异常时返回空列表（try/except 兜底）"""
        import server.screen.windows as sw

        def boom():
            raise RuntimeError("simulated")

        monkeypatch.setattr(sw, "_enum_windows", boom)
        assert list_available_windows() == []


# ==================== ConfigDialog 测试 ====================

class TestConfigDialog:
    def test_default_detailed_fullscreen(self, qapp):
        """默认选详细+全屏，点开始按钮后 get_config 返回 detailed+fullscreen"""
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        # 模拟用户点"开始录制"按钮（走 _on_start_clicked 校验路径）
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg is not None
        assert cfg.range_type == "fullscreen"
        assert cfg.mode == "detailed"
        assert cfg.capture_interval == 0.5
        assert cfg.audio_enabled is True
        dialog.close()

    def test_select_coarse_mode(self, qapp):
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        dialog.rb_detailed.setChecked(False)
        dialog.rb_coarse.setChecked(True)
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg.mode == "coarse"
        assert cfg.capture_interval == 2.0
        assert cfg.burst_intervals == (0.10, 0.20, 0.30)
        dialog.close()

    def test_select_monitor_range(self, qapp):
        dialog = ConfigDialog(
            windows_provider=_fake_windows_provider(),
            monitors_provider=_fake_monitors_provider(),
        )
        dialog.rb_fullscreen.setChecked(False)
        dialog.rb_monitor.setChecked(True)
        dialog.cb_monitor.setCurrentIndex(1)  # 选第 2 个显示器
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg.range_type == "monitor"
        assert cfg.monitor_index == 2  # fake 第 2 个显示器 index=2
        dialog.close()

    def test_select_window_range(self, qapp):
        fake_windows = [
            {"hwnd": 12345, "title": "Notepad", "pid": 1000, "process_name": "notepad.exe"},
            {"hwnd": 67890, "title": "Browser", "pid": 2000, "process_name": "chrome.exe"},
        ]
        dialog = ConfigDialog(windows_provider=_fake_windows_provider(fake_windows))
        dialog.rb_fullscreen.setChecked(False)
        dialog.rb_window.setChecked(True)
        dialog.cb_window.setCurrentIndex(1)  # 选第二个窗口
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg.range_type == "window"
        assert cfg.window_hwnd == 67890
        dialog.close()

    def test_cancel_returns_none(self, qapp):
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        dialog.reject()
        cfg = dialog.get_config()
        assert cfg is None
        dialog.close()

    def test_custom_mode_parses_params(self, qapp):
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        dialog.rb_detailed.setChecked(False)
        dialog.rb_custom.setChecked(True)
        dialog.edit_capture_interval.setText("1.5")
        dialog.edit_burst_intervals.setText("0.1,0.2,0.3")
        dialog.edit_max_duration.setText("600")
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg.mode == "custom"
        assert cfg.capture_interval == 1.5
        assert cfg.burst_intervals == (0.1, 0.2, 0.3)
        assert cfg.max_duration_seconds == 600
        dialog.close()

    def test_refresh_windows_updates_combobox(self, qapp):
        """刷新按钮调用 windows_provider，更新下拉列表"""
        call_count = [0]

        def provider():
            call_count[0] += 1
            return [{"hwnd": 111, "title": "App1", "pid": 100, "process_name": "app1.exe"}]

        dialog = ConfigDialog(windows_provider=provider)
        # 构造时调过一次
        assert call_count[0] == 1
        assert dialog.cb_window.count() == 1
        # 手动刷新
        dialog._refresh_windows()
        assert call_count[0] == 2
        dialog.close()

    def test_audio_checkbox_toggles(self, qapp):
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        dialog.chk_audio.setChecked(False)
        dialog.btn_start.click()
        qapp.processEvents()
        cfg = dialog.get_config()
        assert cfg.audio_enabled is False
        dialog.close()

    def test_range_radio_toggles_enable_state(self, qapp):
        """范围 radio 切换时启用/禁用对应行"""
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        # 默认 fullscreen：monitor/window 行禁用
        assert not dialog._monitor_row_widget.isEnabled()
        assert not dialog._window_row_widget.isEnabled()
        # 切到 monitor
        dialog.rb_fullscreen.setChecked(False)
        dialog.rb_monitor.setChecked(True)
        assert dialog._monitor_row_widget.isEnabled()
        assert not dialog._window_row_widget.isEnabled()
        # 切到 window
        dialog.rb_monitor.setChecked(False)
        dialog.rb_window.setChecked(True)
        assert not dialog._monitor_row_widget.isEnabled()
        assert dialog._window_row_widget.isEnabled()
        dialog.close()

    def test_mode_radio_toggles_custom_box(self, qapp):
        """模式 radio 切换时启用/禁用自定义参数框"""
        dialog = ConfigDialog(windows_provider=_fake_windows_provider())
        assert not dialog.custom_box.isEnabled()  # 默认 detailed
        dialog.rb_detailed.setChecked(False)
        dialog.rb_custom.setChecked(True)
        assert dialog.custom_box.isEnabled()
        dialog.rb_custom.setChecked(False)
        dialog.rb_coarse.setChecked(True)
        assert not dialog.custom_box.isEnabled()
        dialog.close()


# ==================== HotkeyManager 测试 ====================

class TestHotkeyManager:
    def test_default_hotkeys(self):
        assert DEFAULT_HOTKEYS == {"<ctrl>+<alt>+r": "start_stop"}

    def test_on_hotkey_triggers_callback(self):
        called = [0]

        def cb():
            called[0] += 1

        mgr = HotkeyManager(on_start_stop=cb, listener_factory=lambda mapping: None)
        mgr._on_hotkey("start_stop")
        assert called[0] == 1

    def test_dispatch_unknown_action_no_crash(self):
        """未知 action 不应崩溃"""
        mgr = HotkeyManager(on_start_stop=lambda: None, listener_factory=lambda mapping: None)
        mgr._dispatch("unknown")
        mgr._on_hotkey("unknown")

    def test_callback_exception_does_not_crash(self):
        """回调抛异常不应崩 listener（try/except 兜底）"""
        def boom():
            raise RuntimeError("simulated")

        mgr = HotkeyManager(on_start_stop=boom, listener_factory=lambda mapping: None)
        mgr._on_hotkey("start_stop")  # 不应抛异常

    def test_no_callback_does_not_crash(self):
        """未注册回调时也不应崩"""
        mgr = HotkeyManager(listener_factory=lambda mapping: None)
        mgr._on_hotkey("start_stop")

    def test_start_with_fake_listener(self):
        """start() 调用 listener_factory 并启动 listener"""
        started = [False]

        class FakeListener:
            def start(self):
                started[0] = True

            def stop(self):
                pass

        mgr = HotkeyManager(
            on_start_stop=lambda: None,
            listener_factory=lambda mapping: FakeListener(),
        )
        mgr.start()
        assert started[0] is True
        assert mgr.is_running is True
        mgr.stop()
        assert mgr.is_running is False

    def test_start_idempotent(self):
        """重复 start 幂等"""
        started_count = [0]

        class FakeListener:
            def start(self):
                started_count[0] += 1

            def stop(self):
                pass

        mgr = HotkeyManager(listener_factory=lambda mapping: FakeListener())
        mgr.start()
        mgr.start()
        assert started_count[0] == 1
        mgr.stop()

    def test_stop_idempotent(self):
        """未启动直接 stop 不应崩，重复 stop 幂等"""
        mgr = HotkeyManager(listener_factory=lambda mapping: None)
        mgr.stop()
        mgr.stop()
        assert mgr.is_running is False

    def test_start_no_listener_factory_uses_pynput(self, monkeypatch):
        """不传 listener_factory 时用 pynput.keyboard.GlobalHotKeys（mock 避免真实启动）"""
        fake_pynput_mod = types.ModuleType("pynput")
        fake_keyboard_mod = types.ModuleType("pynput.keyboard")

        class FakeListener:
            def __init__(self, mapping):
                self.mapping = mapping

            def start(self):
                pass

            def stop(self):
                pass

        fake_keyboard_mod.GlobalHotKeys = FakeListener
        fake_pynput_mod.keyboard = fake_keyboard_mod
        monkeypatch.setitem(sys.modules, "pynput", fake_pynput_mod)
        monkeypatch.setitem(sys.modules, "pynput.keyboard", fake_keyboard_mod)

        mgr = HotkeyManager(on_start_stop=lambda: None)
        mgr.start()
        assert mgr.is_running is True
        mgr.stop()

    def test_custom_hotkeys_override_defaults(self):
        """传入 hotkeys 参数覆盖默认"""
        custom = {"<ctrl>+<shift>+r": "start_stop"}
        mgr = HotkeyManager(on_start_stop=lambda: None, hotkeys=custom,
                            listener_factory=lambda mapping: None)
        assert mgr._hotkeys == custom


# ==================== FloatingBar 测试 ====================

class TestFloatingBar:
    def test_initial_state_idle(self, qapp):
        bar = FloatingBar()
        assert bar.is_recording is False
        assert bar.mic_enabled is True
        assert "IDLE" in bar.lbl_status.text()
        assert bar.lbl_timer.text() == "00:00:00"
        assert bar.lbl_events.text() == "events: 0"
        assert bar.lbl_frames.text() == "frames: 0"
        bar.close()

    def test_set_recording_true(self, qapp):
        bar = FloatingBar()
        bar.set_recording(True)
        assert bar.is_recording is True
        assert "REC" in bar.lbl_status.text()
        assert "停止" in bar.btn_start_stop.text()
        bar.close()

    def test_set_recording_false(self, qapp):
        bar = FloatingBar()
        bar.set_recording(True)
        bar.set_recording(False)
        assert bar.is_recording is False
        assert "IDLE" in bar.lbl_status.text()
        assert "开始" in bar.btn_start_stop.text()
        bar.close()

    def test_update_duration(self, qapp):
        bar = FloatingBar()
        bar.update_duration(3661)  # 1h 1m 1s
        assert bar.lbl_timer.text() == "01:01:01"
        bar.close()

    def test_update_duration_zero(self, qapp):
        bar = FloatingBar()
        bar.update_duration(0)
        assert bar.lbl_timer.text() == "00:00:00"
        bar.close()

    def test_update_event_count(self, qapp):
        bar = FloatingBar()
        bar.update_event_count(42)
        assert bar.lbl_events.text() == "events: 42"
        bar.close()

    def test_update_frame_count(self, qapp):
        bar = FloatingBar()
        bar.update_frame_count(99)
        assert bar.lbl_frames.text() == "frames: 99"
        bar.close()

    def test_set_mic_enabled_no_signal_loop(self, qapp):
        """set_mic_enabled 用 blockSignals 避免触发 mic_toggled 信号"""
        toggle_count = [0]
        bar = FloatingBar()
        bar.mic_toggled.connect(lambda x: toggle_count.__setitem__(0, toggle_count[0] + 1))
        bar.set_mic_enabled(False)
        assert toggle_count[0] == 0
        assert bar.mic_enabled is False
        assert "关" in bar.btn_mic.text()
        bar.close()

    def test_set_mic_enabled_back_to_true(self, qapp):
        bar = FloatingBar()
        bar.set_mic_enabled(False)
        bar.set_mic_enabled(True)
        assert bar.mic_enabled is True
        assert "开" in bar.btn_mic.text()
        bar.close()

    def test_mic_button_click_emits_signal(self, qapp):
        """用户点麦克风按钮应发 mic_toggled 信号"""
        received = [None]
        bar = FloatingBar()
        bar.mic_toggled.connect(lambda x: received.__setitem__(0, x))
        bar.btn_mic.setChecked(False)
        qapp.processEvents()
        assert received[0] is False
        bar.close()

    def test_start_stop_button_emits_correct_signal(self, qapp):
        """空闲时点开始 → start_requested；录制中点停止 → stop_requested"""
        bar = FloatingBar()
        start_count = [0]
        stop_count = [0]
        bar.start_requested.connect(lambda: start_count.__setitem__(0, start_count[0] + 1))
        bar.stop_requested.connect(lambda: stop_count.__setitem__(0, stop_count[0] + 1))

        # 空闲点
        bar.btn_start_stop.click()
        qapp.processEvents()
        assert start_count[0] == 1
        assert stop_count[0] == 0

        # 切换到录制中
        bar.set_recording(True)
        bar.btn_start_stop.click()
        qapp.processEvents()
        assert start_count[0] == 1
        assert stop_count[0] == 1
        bar.close()


# ==================== FloatingBar 三按钮交互（Ticket 12）====================


class TestFloatingBarThreeButtons:
    """Ticket 12：PAUSED 状态三按钮交互测试（继续录制/保存结束/丢弃重录）。"""

    def test_initial_three_buttons_hidden(self, qapp):
        """初始状态三按钮隐藏，btn_start_stop 可见。"""
        bar = FloatingBar()
        bar.show()
        qapp.processEvents()
        assert not bar.btn_resume.isVisible()
        assert not bar.btn_save.isVisible()
        assert not bar.btn_discard.isVisible()
        assert bar.btn_start_stop.isVisible()
        bar.close()

    def test_set_paused_true_shows_three_buttons(self, qapp):
        """set_paused(True) 显示三按钮，隐藏 btn_start_stop。"""
        bar = FloatingBar()
        bar.show()
        qapp.processEvents()
        bar.set_paused(True)
        qapp.processEvents()
        assert bar.btn_resume.isVisible()
        assert bar.btn_save.isVisible()
        assert bar.btn_discard.isVisible()
        assert not bar.btn_start_stop.isVisible()
        assert "PAUSE" in bar.lbl_status.text()
        bar.close()

    def test_set_paused_false_hides_three_buttons(self, qapp):
        """set_paused(False) 隐藏三按钮，显示 btn_start_stop。"""
        bar = FloatingBar()
        bar.show()
        qapp.processEvents()
        bar.set_paused(True)
        bar.set_paused(False)
        qapp.processEvents()
        assert not bar.btn_resume.isVisible()
        assert not bar.btn_save.isVisible()
        assert not bar.btn_discard.isVisible()
        assert bar.btn_start_stop.isVisible()
        bar.close()

    def test_set_recording_true_after_paused_hides_three_buttons(self, qapp):
        """从 PAUSED 切回 RECORDING（set_recording(True)）时隐藏三按钮。"""
        bar = FloatingBar()
        bar.show()
        qapp.processEvents()
        bar.set_paused(True)
        bar.set_recording(True)
        qapp.processEvents()
        assert not bar.btn_resume.isVisible()
        assert not bar.btn_save.isVisible()
        assert not bar.btn_discard.isVisible()
        assert bar.btn_start_stop.isVisible()
        assert "停止" in bar.btn_start_stop.text()
        bar.close()

    def test_set_recording_false_after_paused_hides_three_buttons(self, qapp):
        """从 PAUSED 切回 IDLE（set_recording(False)）时隐藏三按钮。"""
        bar = FloatingBar()
        bar.show()
        qapp.processEvents()
        bar.set_paused(True)
        bar.set_recording(False)
        qapp.processEvents()
        assert not bar.btn_resume.isVisible()
        assert not bar.btn_save.isVisible()
        assert not bar.btn_discard.isVisible()
        assert bar.btn_start_stop.isVisible()
        assert "开始" in bar.btn_start_stop.text()
        bar.close()

    def test_resume_button_emits_signal(self, qapp):
        """点击继续录制按钮发 resume_requested 信号。"""
        bar = FloatingBar()
        received = [0]
        bar.resume_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        bar.btn_resume.click()
        qapp.processEvents()
        assert received[0] == 1
        bar.close()

    def test_save_button_emits_signal(self, qapp):
        """点击保存结束按钮发 save_requested 信号。"""
        bar = FloatingBar()
        received = [0]
        bar.save_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        bar.btn_save.click()
        qapp.processEvents()
        assert received[0] == 1
        bar.close()

    def test_discard_button_confirmed_emits_signal(self, qapp, monkeypatch):
        """点击丢弃重录按钮 + 确认 → 发 discard_requested 信号。"""
        bar = FloatingBar()
        monkeypatch.setattr(bar, "_confirm_discard", lambda: True)
        received = [0]
        bar.discard_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        bar.btn_discard.click()
        qapp.processEvents()
        assert received[0] == 1
        bar.close()

    def test_discard_button_cancelled_no_signal(self, qapp, monkeypatch):
        """点击丢弃重录按钮 + 取消 → 不发信号，保持 PAUSED（D030）。"""
        bar = FloatingBar()
        monkeypatch.setattr(bar, "_confirm_discard", lambda: False)
        received = [0]
        bar.discard_requested.connect(lambda: received.__setitem__(0, received[0] + 1))
        bar.btn_discard.click()
        qapp.processEvents()
        assert received[0] == 0
        bar.close()

    def test_confirm_discard_returns_bool(self, qapp, monkeypatch):
        """_confirm_discard 返回 bool（monkeypatch 避免 real 弹窗）。"""
        bar = FloatingBar()
        monkeypatch.setattr(bar, "_confirm_discard", lambda: True)
        assert bar._confirm_discard() is True
        monkeypatch.setattr(bar, "_confirm_discard", lambda: False)
        assert bar._confirm_discard() is False
        bar.close()


# ==================== RecorderApp 测试 ====================

class TestRecorderApp:
    def test_start_creates_recording_package(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[
                ("mouse_click", {"button": "left", "x": 100, "y": 200}),
            ]),
            on_auto_stop=lambda: None,
            on_auto_stop_warning=lambda r: None,
        )
        app.start()
        assert app.is_running is True
        assert app.recording_package_root is not None
        assert app.recording_package_root.exists()
        assert app.recording_package_name is not None
        assert app.recording_package_name.startswith("rec_")
        # mock 同步发 1 个事件
        assert app.current_event_count == 1
        app.stop()
        assert app.is_running is False

    def test_stop_writes_meta_json(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[
                ("mouse_click", {"button": "left"}),
                ("keyboard_input", {"text": "abc"}),
            ]),
        )
        app.start()
        pkg_root = app.recording_package_root
        app.stop()
        # meta.json 应在 stop 后写入
        assert (pkg_root / "meta.json").exists()
        # events.jsonl 应有 2 条事件
        events_file = pkg_root / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        # meta.json 应含正确字段
        import json
        meta = json.loads((pkg_root / "meta.json").read_text(encoding="utf-8"))
        assert meta["event_count"] == 2
        assert meta["package_name"] == pkg_root.name
        assert "start_time" in meta
        assert "end_time" in meta
        assert "sensors" in meta

    def test_stop_is_idempotent(self, qapp, tmp_path):
        config = make_detailed_config()
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
        )
        app.start()
        app.stop()
        app.stop()  # 重复 stop 不应崩
        assert app.is_running is False

    def test_start_is_idempotent(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
        )
        app.start()
        first_pkg = app.recording_package_root
        app.start()  # 重复 start 不应创建新录制包
        assert app.recording_package_root == first_pkg
        app.stop()

    def test_auto_stop_triggers_callback(self, qapp, tmp_path):
        """max_duration 到期后自动 stop + 调用 on_auto_stop"""
        auto_stop_called = [False]
        config = make_detailed_config()
        config.max_duration_seconds = 1  # 1 秒后自动停
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
            on_auto_stop=lambda: auto_stop_called.__setitem__(0, True),
        )
        app.start()
        # 等待自动停止（最长 3 秒）
        for _ in range(30):
            if not app.is_running:
                break
            time.sleep(0.1)
        assert auto_stop_called[0] is True
        assert app.is_running is False

    def test_auto_stop_warning_triggers_callback(self, qapp, tmp_path):
        """接近上限时调用 on_auto_stop_warning，参数是剩余秒数"""
        warning_received = [None]
        config = make_detailed_config()
        # WARNING_LEAD_SECONDS=120，max=125 → warn_at=5（满足 >= 5）
        config.max_duration_seconds = 125
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
            on_auto_stop_warning=lambda r: warning_received.__setitem__(0, r),
        )
        app.start()
        # 等待 warning 触发（warn_at=5s）
        for _ in range(80):
            if warning_received[0] is not None:
                break
            time.sleep(0.1)
        assert warning_received[0] is not None
        assert warning_received[0] > 0
        app.stop()

    def test_warning_skipped_when_max_too_short(self, qapp, tmp_path):
        """max_duration - WARNING_LEAD_SECONDS < 5 时跳过 warning timer"""
        # warn_at = max(0, max - 120)，max=100 → warn_at=0 < 5 → 不启动 warning timer
        config = make_detailed_config()
        config.max_duration_seconds = 100
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
            on_auto_stop_warning=lambda r: None,
        )
        app.start()
        assert app._warning_timer is None
        app.stop()

    def test_elapsed_seconds_increases(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(),
        )
        app.start()
        time.sleep(0.5)
        elapsed = app.elapsed_seconds
        assert elapsed >= 0
        app.stop()
        # stop 后 elapsed_seconds 应为 0（is_running=False）
        assert app.elapsed_seconds == 0

    def test_current_event_count_reads_controller(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[
                ("mouse_click", {"button": "left"}),
                ("mouse_click", {"button": "right"}),
                ("keyboard_input", {"text": "x"}),
            ]),
        )
        app.start()
        assert app.current_event_count == 3
        app.stop()

    def test_current_frame_count_starts_zero(self, qapp, tmp_path):
        config = make_detailed_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[]),
        )
        app.start()
        # MockSensor 不发 screen_frame 事件，frame_count 应为 0
        assert app.current_frame_count == 0
        app.stop()

    def test_default_sensor_factory_audio_disabled(self, qapp, tmp_path):
        """_default_sensor_factory 在 audio_enabled=False 时不创建 AudioSensor"""
        config = make_detailed_config()
        config.audio_enabled = False
        app = RecorderApp(config=config, base_dir=tmp_path)
        # 直接调 _default_sensor_factory 验证传感器列表（不 start，避免真实系统 API）
        from lib.recorder.controller import RecordingController
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensors = app._default_sensor_factory(controller, config)
        sensor_types = [type(s).__name__ for s in sensors]
        assert "AudioSensor" not in sensor_types
        assert "KeyboardSensor" in sensor_types
        assert "MouseSensor" in sensor_types
        assert "ScreenCaptureSensor" in sensor_types
        assert "WindowFocusSensor" in sensor_types

    def test_default_sensor_factory_audio_enabled(self, qapp, tmp_path):
        """_default_sensor_factory 在 audio_enabled=True 时创建 AudioSensor"""
        config = make_detailed_config()
        config.audio_enabled = True
        app = RecorderApp(config=config, base_dir=tmp_path)
        from lib.recorder.controller import RecordingController
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensors = app._default_sensor_factory(controller, config)
        sensor_types = [type(s).__name__ for s in sensors]
        assert "AudioSensor" in sensor_types

    def test_default_sensor_factory_wire_relationships(self, qapp, tmp_path):
        """验证 wire 关系：MouseSensor.on_click_callback 指向 ScreenCaptureSensor.trigger_burst；
        WindowFocusSensor.on_focus_change_callback 指向 KeyboardSensor.notify_focus_change"""
        config = make_detailed_config()
        app = RecorderApp(config=config, base_dir=tmp_path)
        from lib.recorder.controller import RecordingController
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensors = app._default_sensor_factory(controller, config)
        sensor_map = {type(s).__name__: s for s in sensors}
        mouse = sensor_map["MouseSensor"]
        screen = sensor_map["ScreenCaptureSensor"]
        window = sensor_map["WindowFocusSensor"]
        keyboard = sensor_map["KeyboardSensor"]
        # MouseSensor.on_click_callback 应指向 ScreenCaptureSensor.trigger_burst
        assert mouse._on_click_callback == screen.trigger_burst
        # WindowFocusSensor._on_focus_change_callback 应指向 KeyboardSensor.notify_focus_change
        assert window._on_focus_change_callback == keyboard.notify_focus_change

    def test_end_to_end_with_mock_sensors(self, qapp, tmp_path):
        """端到端：start → mock 发事件 → stop → 验证录制包完整"""
        config = make_detailed_config()
        config.max_duration_seconds = 60

        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[
                ("mouse_click", {"button": "left", "x": 100, "y": 200}),
                ("keyboard_input", {"text": "hello"}),
                ("focus_change", {"hwnd": 12345, "title": "Test", "pid": 100}),
            ]),
        )
        app.start()
        pkg_root = app.recording_package_root
        pkg_name = app.recording_package_name
        assert pkg_root.exists()
        assert pkg_name.startswith("rec_")
        assert app.current_event_count == 3
        app.stop()

        # 验证录制包目录结构
        assert (pkg_root / "meta.json").exists()
        assert (pkg_root / "events.jsonl").exists()
        assert (pkg_root / "focus.jsonl").exists()
        assert (pkg_root / "frames").is_dir()
        assert (pkg_root / "audio").is_dir()

        # events.jsonl 应有 3 条事件（mouse + keyboard + focus 都走 events.jsonl）
        events = (pkg_root / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(events) == 3
        # focus.jsonl 应有 1 条 focus_change 事件（_FocusAwareMockSensor 调 emit_focus_change）
        focus_events = (pkg_root / "focus.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(focus_events) == 1

        # 验证 events.jsonl 每行是合法 JSON
        import json
        for line in events:
            obj = json.loads(line)
            assert "timestamp" in obj
            assert "kind" in obj
            assert "payload" in obj

    def test_coarse_mode_config(self, qapp, tmp_path):
        """粗略模式配置正确传递到 RecorderApp"""
        config = make_coarse_config()
        config.max_duration_seconds = 60
        app = RecorderApp(
            config=config,
            base_dir=tmp_path,
            sensor_factory=_make_fake_sensor_factory(events=[]),
        )
        app.start()
        assert app.config.mode == "coarse"
        assert app.config.capture_interval == 2.0
        app.stop()
