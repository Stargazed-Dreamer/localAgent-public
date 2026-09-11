"""L0 采集层 Ticket 05：WindowFocusSensor 单测 + 集成测试

测试策略：
- mock win32gui / win32process 模块，用假 GetForegroundWindow 模拟窗口切换
- 不真调系统 API（CI 环境无桌面交互）
- 验证：focus.jsonl 含切换事件 + events.jsonl 也含 + hwnd 变化才发事件
"""

import json
import threading
import time

import pytest

from lib.recorder.controller import RecordingController
from lib.recorder.sensors.window import WindowFocusSensor

# ==================== 假 win32gui / win32process 模块 ====================

class FakeWin32Gui:
    """模拟 win32gui，GetForegroundWindow 按预设序列循环返回 hwnd。"""

    def __init__(self):
        self._hwnds = [0, 100, 100, 200, 200, 300]  # 模拟切换序列
        self._idx = 0
        self._lock = threading.Lock()

    def GetForegroundWindow(self):
        with self._lock:
            hwnd = self._hwnds[self._idx % len(self._hwnds)]
            self._idx += 1
            return hwnd

    def GetWindowText(self, hwnd):
        # 根据 hwnd 返回固定标题
        titles = {0: "", 100: "WindowA", 200: "WindowB", 300: "WindowC"}
        return titles.get(hwnd, f"Unknown_{hwnd}")


class FakeWin32Process:
    """模拟 win32process，GetWindowThreadProcessId 返回 (thread_id, pid)。"""

    def GetWindowThreadProcessId(self, hwnd):
        pids = {0: 0, 100: 1000, 200: 2000, 300: 3000}
        return (1, pids.get(hwnd, 0))


@pytest.fixture
def fake_win32(monkeypatch):
    """mock win32gui / win32process 模块。

    直接 patch sensor 模块的引用（monkeypatch.setitem 只影响后续 import，
    但 sensor 顶部已 import win32gui，必须 patch 模块属性）。
    """
    import lib.recorder.sensors.window as window_mod
    fake_gui = FakeWin32Gui()
    fake_proc = FakeWin32Process()
    monkeypatch.setattr(window_mod, "win32gui", fake_gui)
    monkeypatch.setattr(window_mod, "win32process", fake_proc)
    return fake_gui, fake_proc


# ==================== WindowFocusSensor 单测 ====================

class TestWindowFocusSensor:
    def test_start_polls_foreground_window(self, tmp_path, fake_win32):
        """start() 后启动轮询线程，能取到前台窗口"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = WindowFocusSensor(controller=controller, poll_interval=0.05)
        sensor.start()
        # 让轮询跑一会儿
        time.sleep(0.2)
        sensor.stop()
        controller.stop()
        # 应该有事件被写入（hwnd 从 0 → 100 → 200 → 300）
        events = controller.package.read_events()
        assert len(events) > 0

    def test_emits_event_only_on_hwnd_change(self, tmp_path, fake_win32):
        """hwnd 不变时不发事件，变化时才发"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = WindowFocusSensor(controller=controller, poll_interval=0.05)
        sensor.start()
        # fake_win32 序列：[0, 100, 100, 200, 200, 300] 循环
        # 第一次 poll：hwnd=0（初始 _last_hwnd=None，0 != None → 发事件）
        # 第二次 poll：hwnd=100（100 != 0 → 发事件）
        # 第三次 poll：hwnd=100（100 == 100 → 不发）
        # 第四次 poll：hwnd=200（200 != 100 → 发）
        # 第五次 poll：hwnd=200（200 == 200 → 不发）
        # 第六次 poll：hwnd=300（300 != 200 → 发）
        # 第七次 poll 之后循环回 0（0 != 300 → 发）
        time.sleep(0.4)  # 跑 8 次 poll
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        # hwnd 变化次数（不变化的 poll 不发事件）
        # 至少 3 次（0→100, 100→200, 200→300）
        assert len(events) >= 3
        # 所有事件 kind 都是 focus_change
        for ev in events:
            assert ev["kind"] == "focus_change"

    def test_focus_payload_contains_hwnd_title_pid(self, tmp_path, fake_win32):
        """focus_change 事件 payload 含 hwnd / title / pid"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = WindowFocusSensor(controller=controller, poll_interval=0.05)
        sensor.start()
        time.sleep(0.15)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        assert len(events) > 0
        for ev in events:
            payload = ev["payload"]
            assert "hwnd" in payload
            assert "title" in payload
            assert "pid" in payload
            # hwnd=0 时 title 可能为空，但 hwnd>0 时 title 应非空
            if payload["hwnd"] > 0:
                assert payload["title"] != ""

    def test_stop_is_idempotent(self, tmp_path, fake_win32):
        """stop() 可重复调用不报错"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = WindowFocusSensor(controller=controller, poll_interval=0.05)
        sensor.start()
        sensor.stop()
        sensor.stop()  # 重复
        controller.stop()

    def test_stop_without_start_does_not_crash(self, tmp_path, fake_win32):
        """stop() 在未 start() 状态下调用不崩溃"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensor = WindowFocusSensor(controller=controller, poll_interval=0.05)
        sensor.stop()  # 不应抛异常


# ==================== WindowFocusSensor + Controller 集成 ====================

class TestWindowFocusSensorIntegration:
    def test_focus_events_written_to_both_files(self, tmp_path, fake_win32):
        """focus_change 事件同时写 events.jsonl 和 focus.jsonl"""
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[WindowFocusSensor(controller=None, poll_interval=0.05)],
        )
        controller.sensors[0].controller = controller
        controller.start()
        time.sleep(0.25)
        controller.stop()

        # events.jsonl 含 focus_change 事件
        events = controller.package.read_events()
        focus_events = [e for e in events if e["kind"] == "focus_change"]
        assert len(focus_events) >= 2  # 至少 2 次切换

        # focus.jsonl 也含相同事件
        focus_lines = controller.package.focus_file.read_text(encoding="utf-8").strip().split("\n")
        focus_from_file = [json.loads(line) for line in focus_lines if line.strip()]
        assert len(focus_from_file) == len(focus_events)
        # 两个文件的事件 payload 一致
        for a, b in zip(focus_events, focus_from_file, strict=True):
            assert a["payload"] == b["payload"]
            assert a["kind"] == b["kind"]

    def test_window_switch_3_times_produces_3_events(self, tmp_path, fake_win32):
        """切换窗口 3 次产生至少 3 条 focus_change 事件"""
        # fake_win32 序列 [0, 100, 100, 200, 200, 300] → 3 次切换（0→100, 100→200, 200→300）
        controller = RecordingController(
            base_dir=tmp_path,
            sensors=[WindowFocusSensor(controller=None, poll_interval=0.05)],
        )
        controller.sensors[0].controller = controller
        controller.start()
        # 跑 6 次 poll（300ms）覆盖完整序列
        time.sleep(0.35)
        controller.stop()

        events = controller.package.read_events()
        # 至少 3 次（0→100, 100→200, 200→300），可能更多（循环回 0）
        hwnds = [ev["payload"]["hwnd"] for ev in events]
        # 应该能看到 100 / 200 / 300 都出现
        assert 100 in hwnds
        assert 200 in hwnds
        assert 300 in hwnds
