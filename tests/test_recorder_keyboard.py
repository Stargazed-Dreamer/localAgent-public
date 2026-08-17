"""L0 采集层 Ticket 02：KeyboardSensor 单测 + 集成测试

测试策略（spec M1 / 03-input-detection.md）：
- _normalize_key：纯函数测试，验证 pynput Key 归一化
- KeyboardSensor：注入 fake uia_snapshot_fn（返回固定 value/is_password），不传 listener_factory，
  测试直接调 sensor._on_press / sensor._on_release 模拟按键
- 时间窗口聚合：用短 time_window（0.05s）+ sleep 验证 timer 触发
- 密码框检测：fake uia_snapshot_fn 返回 is_password=True → 发 password_masked
- 热键识别：Ctrl+C/V/X → hotkey 事件
- 输入法切换：Shift+Space / Ctrl+Space → ime_switch 事件
- UIA 差值：start value="" + end value="你好" → text="你好"
- 集成：注册到 RecordingController，验证 events.jsonl 含正确事件
"""

import time
from types import SimpleNamespace

import pytest

from lib.recorder.controller import RecordingController
from lib.recorder.sensors.keyboard import KeyboardSensor, _normalize_key

# ==================== Fake pynput key 工具 ====================

def char_key(c: str) -> SimpleNamespace:
    """构造 pynput KeyCode 风格的 char key。"""
    return SimpleNamespace(char=c, name=None)


def special_key(name: str) -> SimpleNamespace:
    """构造 pynput Key 枚举风格的特殊键。"""
    return SimpleNamespace(char=None, name=name)


# ==================== _normalize_key 单测 ====================

class TestNormalizeKey:
    def test_char_key_lowercased(self):
        """char key 归一化为小写"""
        assert _normalize_key(char_key("c")) == "c"
        assert _normalize_key(char_key("C")) == "c"
        assert _normalize_key(char_key("n")) == "n"

    def test_ctrl_l_r_normalized(self):
        """ctrl_l / ctrl_r → ctrl"""
        assert _normalize_key(special_key("ctrl_l")) == "ctrl"
        assert _normalize_key(special_key("ctrl_r")) == "ctrl"

    def test_alt_l_r_normalized(self):
        """alt_l / alt_r / alt_gr → alt"""
        assert _normalize_key(special_key("alt_l")) == "alt"
        assert _normalize_key(special_key("alt_r")) == "alt"
        assert _normalize_key(special_key("alt_gr")) == "alt"

    def test_shift_variants_normalized(self):
        """shift / shift_l / shift_r → shift"""
        assert _normalize_key(special_key("shift")) == "shift"
        assert _normalize_key(special_key("shift_l")) == "shift"
        assert _normalize_key(special_key("shift_r")) == "shift"

    def test_space_tab_enter_backspace(self):
        """常见特殊键保留 name"""
        assert _normalize_key(special_key("space")) == "space"
        assert _normalize_key(special_key("tab")) == "tab"
        assert _normalize_key(special_key("enter")) == "enter"
        assert _normalize_key(special_key("backspace")) == "backspace"

    def test_cmd_normalized(self):
        """cmd_l / cmd_r / cmd → cmd"""
        assert _normalize_key(special_key("cmd_l")) == "cmd"
        assert _normalize_key(special_key("cmd_r")) == "cmd"
        assert _normalize_key(special_key("cmd")) == "cmd"


# ==================== KeyboardSensor 生命周期 ====================

class TestKeyboardSensorLifecycle:
    def test_start_without_controller_raises(self, tmp_path):
        """controller=None 时 start() 抛异常"""
        sensor = KeyboardSensor(controller=None)
        with pytest.raises(RuntimeError):
            sensor.start()

    def test_start_stop_idempotent(self, tmp_path):
        """start/stop 可重复调用"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(controller=controller, time_window=0.05)
        sensor.start()
        sensor.start()  # 重复
        sensor.stop()
        sensor.stop()  # 重复
        controller.stop()

    def test_stop_without_start_does_not_crash(self, tmp_path):
        """未 start 直接 stop 不崩溃"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensor = KeyboardSensor(controller=controller)
        sensor.stop()


# ==================== 密码框检测（D021） ====================

class TestPasswordMasked:
    def test_password_field_emits_password_masked(self, tmp_path):
        """IsPassword=True → 发 password_masked 事件，不记录内容"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        # fake uia：始终返回密码框
        def fake_snapshot():
            return {"value": "secret", "is_password": True}
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=fake_snapshot,
        )
        sensor.start()
        # 模拟输入密码
        sensor._on_press(char_key("a"))
        sensor._on_press(char_key("b"))
        sensor._on_press(char_key("c"))
        # 等 timer 触发
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        # 应该有 1 个 password_masked 事件
        pwd_events = [e for e in events if e["kind"] == "password_masked"]
        assert len(pwd_events) == 1
        # payload 不含密码内容
        payload = pwd_events[0]["payload"]
        assert payload["is_password"] is True
        assert payload["physical_keys"] == []
        assert payload["detection_method"] == "password_masked"
        # 不应该有 keyboard_input 事件泄露密码
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 0


# ==================== 热键识别 ====================

class TestHotkeyDetection:
    def test_ctrl_c_emits_hotkey_copy(self, tmp_path):
        """Ctrl+C → hotkey 事件，combo=copy"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,  # UIA 不可用
        )
        sensor.start()
        sensor._on_press(special_key("ctrl_l"))
        sensor._on_press(char_key("c"))
        sensor._on_release(special_key("ctrl_l"))
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        hotkeys = [e for e in events if e["kind"] == "hotkey"]
        assert len(hotkeys) == 1
        payload = hotkeys[0]["payload"]
        assert payload["combo"] == "copy"
        assert set(payload["keys"]) == {"ctrl", "c"}

    def test_ctrl_v_emits_hotkey_paste(self, tmp_path):
        """Ctrl+V → hotkey 事件，combo=paste"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(special_key("ctrl_r"))
        sensor._on_press(char_key("v"))
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        hotkeys = [e for e in events if e["kind"] == "hotkey"]
        assert len(hotkeys) == 1
        assert hotkeys[0]["payload"]["combo"] == "paste"

    def test_ctrl_x_emits_hotkey_cut(self, tmp_path):
        """Ctrl+X → hotkey 事件，combo=cut"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(special_key("ctrl_l"))
        sensor._on_press(char_key("x"))
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        hotkeys = [e for e in events if e["kind"] == "hotkey"]
        assert len(hotkeys) == 1
        assert hotkeys[0]["payload"]["combo"] == "cut"


# ==================== 输入法切换识别 ====================

class TestImeSwitchDetection:
    def test_shift_space_emits_ime_switch(self, tmp_path):
        """Shift+Space → ime_switch 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(special_key("shift_l"))
        sensor._on_press(special_key("space"))
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        ime_events = [e for e in events if e["kind"] == "ime_switch"]
        assert len(ime_events) == 1
        payload = ime_events[0]["payload"]
        assert set(payload["keys"]) == {"shift", "space"}

    def test_ctrl_space_emits_ime_switch(self, tmp_path):
        """Ctrl+Space → ime_switch 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(special_key("ctrl_l"))
        sensor._on_press(special_key("space"))
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        ime_events = [e for e in events if e["kind"] == "ime_switch"]
        assert len(ime_events) == 1
        assert set(ime_events[0]["payload"]["keys"]) == {"ctrl", "space"}


# ==================== 时间窗口聚合 ====================

class TestTimeWindowAggregation:
    def test_continuous_keys_merge_into_one_block(self, tmp_path):
        """连续按键 → 一个 keyboard_input 块"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,  # 50ms 短窗口
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        # 快速连续按 n-i-h-a-o
        sensor._on_press(char_key("n"))
        sensor._on_press(char_key("i"))
        sensor._on_press(char_key("h"))
        sensor._on_press(char_key("a"))
        sensor._on_press(char_key("o"))
        # 等 timer 触发
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1  # 一个块
        # physical_keys 含 n-i-h-a-o
        keys = kb_events[0]["payload"]["physical_keys"]
        for ch in ["n", "i", "h", "a", "o"]:
            assert ch in keys

    def test_pause_splits_into_two_blocks(self, tmp_path):
        """按键→停顿→按键 → 两个 keyboard_input 块"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        # 第一段
        sensor._on_press(char_key("a"))
        sensor._on_press(char_key("b"))
        time.sleep(0.15)  # 等 timer 触发，flush 第一个块
        # 第二段
        sensor._on_press(char_key("c"))
        sensor._on_press(char_key("d"))
        time.sleep(0.15)  # 等 timer 触发，flush 第二个块
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 2

    def test_modifier_press_does_not_start_block(self, tmp_path):
        """单独按修饰键不启动块"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(special_key("shift_l"))
        sensor._on_release(special_key("shift_l"))
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        # 不应有 keyboard_input / hotkey / ime_switch 事件
        assert all(e["kind"] not in ("keyboard_input", "hotkey", "ime_switch") for e in events)


# ==================== UIA 差值检测（Strategy 2） ====================

class TestUiaValueDiff:
    def test_uia_diff_produces_text(self, tmp_path):
        """UIA 差值：start value="" + end value="你好" → text="你好" """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        # fake uia：第一次返回空字符串，第二次返回"你好"
        call_count = [0]
        values = ["", "你好"]
        def fake_snapshot():
            i = min(call_count[0], len(values) - 1)
            v = values[i]
            call_count[0] += 1
            return {"value": v, "is_password": False}
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=fake_snapshot,
        )
        sensor.start()
        sensor._on_press(char_key("n"))
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1
        payload = kb_events[0]["payload"]
        assert payload["text"] == "你好"
        assert payload["detection_method"] == "uia_value_diff"
        assert payload["is_password"] is False

    def test_uia_unavailable_falls_back_to_physical_keys(self, tmp_path):
        """UIA 不可用 → detection_method=physical_keys，text="" """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,  # UIA 不可用
        )
        sensor.start()
        sensor._on_press(char_key("a"))
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1
        payload = kb_events[0]["payload"]
        assert payload["text"] == ""
        assert payload["detection_method"] == "physical_keys"

    def test_physical_keys_preserved_when_uia_works(self, tmp_path):
        """UIA 取到文字时 physical_keys 仍保留（D022）"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        call_count = [0]
        values = ["", "你好"]
        def fake_snapshot():
            i = min(call_count[0], len(values) - 1)
            v = values[i]
            call_count[0] += 1
            return {"value": v, "is_password": False}
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=fake_snapshot,
        )
        sensor.start()
        # 模拟拼音输入
        for ch in ["n", "i", "h", "a", "o"]:
            sensor._on_press(char_key(ch))
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1
        payload = kb_events[0]["payload"]
        assert payload["text"] == "你好"
        assert payload["detection_method"] == "uia_value_diff"
        # physical_keys 仍保留
        keys = payload["physical_keys"]
        for ch in ["n", "i", "h", "a", "o"]:
            assert ch in keys


# ==================== notify_focus_change ====================

class TestNotifyFocusChange:
    def test_focus_change_flushes_current_block(self, tmp_path):
        """notify_focus_change 立即 flush 当前块"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=10.0,  # 长窗口，确保不会自动触发
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        sensor._on_press(char_key("a"))
        sensor._on_press(char_key("b"))
        # 主动通知焦点变化
        sensor.notify_focus_change()
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1
        keys = kb_events[0]["payload"]["physical_keys"]
        assert "a" in keys
        assert "b" in keys


# ==================== 集成测试 ====================

class TestKeyboardSensorIntegration:
    def test_mixed_typing_and_hotkey(self, tmp_path):
        """混合输入：typing → Ctrl+C → typing → 3 个事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        # 第一段 typing
        sensor._on_press(char_key("h"))
        sensor._on_press(char_key("i"))
        time.sleep(0.15)  # flush 第一个块
        # Ctrl+C
        sensor._on_press(special_key("ctrl_l"))
        sensor._on_press(char_key("c"))
        sensor._on_release(special_key("ctrl_l"))
        # 第二段 typing
        sensor._on_press(char_key("x"))
        time.sleep(0.15)  # flush 第三个块
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kinds = [e["kind"] for e in events]
        # 应含 2 个 keyboard_input + 1 个 hotkey
        assert kinds.count("keyboard_input") == 2
        assert kinds.count("hotkey") == 1

    def test_shift_held_during_typing(self, tmp_path):
        """Shift 持续按住打字：physical_keys 含 shift（一次性）"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = KeyboardSensor(
            controller=controller,
            time_window=0.05,
            uia_snapshot_fn=lambda: None,
        )
        sensor.start()
        # 先按 Shift
        sensor._on_press(special_key("shift_l"))
        # 然后打字（Shift 持续按住）
        sensor._on_press(char_key("H"))
        sensor._on_press(char_key("E"))
        sensor._on_press(char_key("L"))
        sensor._on_press(char_key("L"))
        sensor._on_press(char_key("O"))
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kb_events = [e for e in events if e["kind"] == "keyboard_input"]
        assert len(kb_events) == 1
        keys = kb_events[0]["payload"]["physical_keys"]
        # shift 应在 physical_keys 中（一次）
        assert "shift" in keys
        assert keys.count("shift") == 1
