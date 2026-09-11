"""L0 采集层 Ticket 02：MouseSensor 单测 + 集成测试

测试策略（spec M1 / 03-input-detection.md 第四节）：
- _normalize_button：纯函数测试
- MouseSensor：不传 listener_factory，测试直接调 _on_click / _on_scroll / _on_move 模拟事件
- click：press+release 同位置 → mouse_click
- drag：press+move+release 位移>阈值 → mouse_drag
- scroll：_on_scroll → mouse_scroll
- on_click_callback：click 时调 callback；drag 时不调
- 集成：注册到 RecordingController，验证 events.jsonl 含正确事件
"""

from types import SimpleNamespace

import pytest

from lib.recorder.controller import RecordingController
from lib.recorder.sensors.mouse import MouseSensor, _normalize_button

# ==================== Fake pynput button 工具 ====================

def fake_button(name: str) -> SimpleNamespace:
    """构造 pynput Button 枚举风格的对象。"""
    return SimpleNamespace(name=name)


# ==================== _normalize_button 单测 ====================

class TestNormalizeButton:
    def test_left(self):
        assert _normalize_button(fake_button("left")) == "left"

    def test_right(self):
        assert _normalize_button(fake_button("right")) == "right"

    def test_middle(self):
        assert _normalize_button(fake_button("middle")) == "middle"


# ==================== MouseSensor 生命周期 ====================

class TestMouseSensorLifecycle:
    def test_start_without_controller_raises(self, tmp_path):
        """controller=None 时 start() 抛异常"""
        sensor = MouseSensor(controller=None)
        with pytest.raises(RuntimeError):
            sensor.start()

    def test_start_stop_idempotent(self, tmp_path):
        """start/stop 可重复调用"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller)
        sensor.start()
        sensor.start()  # 重复
        sensor.stop()
        sensor.stop()  # 重复
        controller.stop()

    def test_stop_without_start_does_not_crash(self, tmp_path):
        """未 start 直接 stop 不崩溃"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensor = MouseSensor(controller=controller)
        sensor.stop()


# ==================== mouse_click 事件 ====================

class TestMouseClick:
    def test_single_click_emits_mouse_click(self, tmp_path):
        """单击：press+release 同位置 → mouse_click 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller)
        sensor.start()
        # press
        sensor._on_click(100, 200, fake_button("left"), pressed=True)
        # release 同位置（位移=0）
        sensor._on_click(100, 200, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        click_events = [e for e in events if e["kind"] == "mouse_click"]
        assert len(click_events) == 1
        payload = click_events[0]["payload"]
        assert payload["button"] == "left"
        assert payload["clicks"] == 1
        assert payload["x"] == 100
        assert payload["y"] == 200

    def test_right_click_emits_mouse_click(self, tmp_path):
        """右键：press+release → mouse_click 事件，button=right"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller)
        sensor.start()
        sensor._on_click(50, 60, fake_button("right"), pressed=True)
        sensor._on_click(50, 60, fake_button("right"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        click_events = [e for e in events if e["kind"] == "mouse_click"]
        assert len(click_events) == 1
        assert click_events[0]["payload"]["button"] == "right"

    def test_small_movement_still_click(self, tmp_path):
        """位移小于阈值 → 仍是 click（不是 drag）"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, drag_threshold=5.0)
        sensor.start()
        # press
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        # move 2px（小于阈值 5）
        sensor._on_move(102, 101)
        # release
        sensor._on_click(102, 101, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        # 应是 click，不是 drag
        assert any(e["kind"] == "mouse_click" for e in events)
        assert not any(e["kind"] == "mouse_drag" for e in events)


# ==================== mouse_drag 事件 ====================

class TestMouseDrag:
    def test_drag_emits_mouse_drag(self, tmp_path):
        """拖拽：press+move+release 位移>阈值 → mouse_drag 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, drag_threshold=5.0)
        sensor.start()
        # press
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        # move 多个点（形成轨迹）
        sensor._on_move(110, 100)
        sensor._on_move(120, 105)
        sensor._on_move(130, 110)
        # release
        sensor._on_click(130, 110, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        drag_events = [e for e in events if e["kind"] == "mouse_drag"]
        assert len(drag_events) == 1
        payload = drag_events[0]["payload"]
        assert payload["button"] == "left"
        assert payload["start"] == {"x": 100, "y": 100}
        assert payload["end"] == {"x": 130, "y": 110}
        # 轨迹含所有 move 点（含起点）
        track = payload["track"]
        assert len(track) >= 3  # 起点 + 至少 2 个 move 点
        assert track[0] == {"x": 100, "y": 100}  # 起点

    def test_drag_does_not_emit_click(self, tmp_path):
        """拖拽时不发 mouse_click 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, drag_threshold=5.0)
        sensor.start()
        sensor._on_click(0, 0, fake_button("left"), pressed=True)
        sensor._on_move(50, 50)
        sensor._on_click(50, 50, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        assert any(e["kind"] == "mouse_drag" for e in events)
        assert not any(e["kind"] == "mouse_click" for e in events)


# ==================== mouse_scroll 事件 ====================

class TestMouseScroll:
    def test_scroll_emits_mouse_scroll(self, tmp_path):
        """滚轮 → mouse_scroll 事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller)
        sensor.start()
        sensor._on_scroll(100, 200, dx=0, dy=-3)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        scroll_events = [e for e in events if e["kind"] == "mouse_scroll"]
        assert len(scroll_events) == 1
        payload = scroll_events[0]["payload"]
        assert payload["x"] == 100
        assert payload["y"] == 200
        assert payload["dx"] == 0
        assert payload["dy"] == -3


# ==================== on_click_callback 触发 ====================

class TestOnClickCallback:
    def test_click_triggers_callback(self, tmp_path):
        """click 时调 on_click_callback（用于触发 ScreenCaptureSensor.trigger_burst）"""
        callback_count = [0]
        def on_click():
            callback_count[0] += 1

        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, on_click_callback=on_click)
        sensor.start()
        # 一次单击
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        sensor._on_click(100, 100, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        assert callback_count[0] == 1

    def test_drag_does_not_trigger_callback(self, tmp_path):
        """拖拽时不调 on_click_callback（只有 click 触发 burst）"""
        callback_count = [0]
        def on_click():
            callback_count[0] += 1

        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(
            controller=controller,
            drag_threshold=5.0,
            on_click_callback=on_click,
        )
        sensor.start()
        # 拖拽
        sensor._on_click(0, 0, fake_button("left"), pressed=True)
        sensor._on_move(50, 50)
        sensor._on_click(50, 50, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        assert callback_count[0] == 0

    def test_scroll_does_not_trigger_callback(self, tmp_path):
        """滚轮时不调 on_click_callback"""
        callback_count = [0]
        def on_click():
            callback_count[0] += 1

        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, on_click_callback=on_click)
        sensor.start()
        sensor._on_scroll(100, 100, dx=0, dy=-1)
        sensor.stop()
        controller.stop()

        assert callback_count[0] == 0

    def test_callback_exception_does_not_crash_sensor(self, tmp_path):
        """callback 抛异常不应崩掉 sensor"""
        def bad_callback():
            raise ValueError("test")

        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, on_click_callback=bad_callback)
        sensor.start()
        # 不应抛异常
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        sensor._on_click(100, 100, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        # click 事件仍应写入
        events = controller.package.read_events()
        assert any(e["kind"] == "mouse_click" for e in events)


# ==================== 集成测试 ====================

class TestMouseSensorIntegration:
    def test_mixed_click_and_scroll(self, tmp_path):
        """混合操作：click → scroll → click → 3 个事件"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller)
        sensor.start()
        # click 1
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        sensor._on_click(100, 100, fake_button("left"), pressed=False)
        # scroll
        sensor._on_scroll(100, 100, dx=0, dy=-1)
        # click 2（右键）
        sensor._on_click(200, 200, fake_button("right"), pressed=True)
        sensor._on_click(200, 200, fake_button("right"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kinds = [e["kind"] for e in events]
        assert kinds.count("mouse_click") == 2
        assert kinds.count("mouse_scroll") == 1

    def test_click_then_drag(self, tmp_path):
        """click → drag 序列"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = MouseSensor(controller=controller, drag_threshold=5.0)
        sensor.start()
        # click
        sensor._on_click(100, 100, fake_button("left"), pressed=True)
        sensor._on_click(100, 100, fake_button("left"), pressed=False)
        # drag
        sensor._on_click(200, 200, fake_button("left"), pressed=True)
        sensor._on_move(250, 250)
        sensor._on_click(250, 250, fake_button("left"), pressed=False)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        kinds = [e["kind"] for e in events]
        assert kinds.count("mouse_click") == 1
        assert kinds.count("mouse_drag") == 1
