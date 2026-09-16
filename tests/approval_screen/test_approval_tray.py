"""托盘 + 任务栏闪烁 + 新审批提醒测试（Ticket 06）。

覆盖：
- taskbar_flash: flash_window/stop_flash 可调用（非 Windows no-op，Windows hwnd=0 no-op）
- tray.TrayIcon: 实例化、start_alert/stop_alert 切换闪烁定时器、信号可发射
- poller: new_arrivals 信号（首次轮询不触发，新增 ID 触发）
- panel: window_activated 信号、closeEvent 分流（show_in_tray True/False）
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait_poll_done(poller, timeout_ms=3000):
    """8-8 Poller HTTP 线程化后等待单次轮询完成（worker 更新 _last_ids/_first_poll
    + 信号 queued 回主线程派发），保证顺序 _poll() 之间无竞态。"""
    import time

    from PySide6.QtCore import QCoreApplication

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if not poller._busy:
            QCoreApplication.processEvents()
            return True
        time.sleep(0.01)
    return False


# ========== taskbar_flash ==========


class TestTaskbarFlash:
    def test_flash_window_zero_hwnd_noop(self):
        """hwnd=0 时不崩溃（无窗口场景）。"""
        from client.approval_panel.taskbar_flash import flash_window

        flash_window(0, count=5)  # 不应抛异常

    def test_stop_flash_zero_hwnd_noop(self):
        from client.approval_panel.taskbar_flash import stop_flash

        stop_flash(0)

    def test_flash_window_5_times_calls_flash_window(self):
        """flash_window_5_times 应调用 flash_window(count=5)。"""
        from client.approval_panel import taskbar_flash

        calls = []

        def fake_flash(hwnd, count=5):
            calls.append((hwnd, count))

        with patch.object(taskbar_flash, "flash_window", fake_flash):
            taskbar_flash.flash_window_5_times(12345)

        assert calls == [(12345, 5)]

    def test_non_windows_platform_noop(self, monkeypatch):
        """非 Windows 平台调用不抛异常。"""
        from client.approval_panel import taskbar_flash

        # 模拟非 Windows
        monkeypatch.setattr(taskbar_flash, "_IS_WINDOWS", False)
        # 重新加载 no-op 实现
        import importlib

        importlib.reload(taskbar_flash)
        taskbar_flash.flash_window(0, count=5)
        taskbar_flash.stop_flash(0)
        # 恢复
        importlib.reload(taskbar_flash)


# ========== TrayIcon ==========


class TestTrayIcon:
    def test_tray_icon_instantiates(self):
        from client.approval_panel.tray import TrayIcon

        tray = TrayIcon()
        assert tray._flash_timer.interval() == 500
        assert tray._flash_timer.isActive() is False
        assert tray._is_alerting is False
        tray.deleteLater()

    def test_start_alert_starts_timer(self):
        from client.approval_panel.tray import TrayIcon

        tray = TrayIcon()
        # mock 托盘可见
        tray._tray.setVisible = lambda: True
        tray._tray.isVisible = lambda: True
        tray.start_alert()
        assert tray._is_alerting is True
        assert tray._flash_timer.isActive() is True
        tray.deleteLater()

    def test_start_alert_noop_when_hidden(self):
        """托盘不可见时 start_alert 不启动定时器。"""
        from client.approval_panel.tray import TrayIcon

        tray = TrayIcon()
        tray._tray.isVisible = lambda: False
        tray.start_alert()
        assert tray._is_alerting is False
        assert tray._flash_timer.isActive() is False
        tray.deleteLater()

    def test_stop_alert_stops_timer(self):
        from client.approval_panel.tray import TrayIcon

        tray = TrayIcon()
        tray._tray.isVisible = lambda: True
        tray.start_alert()
        assert tray._flash_timer.isActive() is True
        tray.stop_alert()
        assert tray._is_alerting is False
        assert tray._flash_timer.isActive() is False
        tray.deleteLater()

    def test_toggle_flash_icon_switches_state(self):
        from client.approval_panel.tray import TrayIcon

        tray = TrayIcon()
        initial = tray._flash_state
        tray._toggle_flash_icon()
        assert tray._flash_state != initial
        tray._toggle_flash_icon()
        assert tray._flash_state == initial
        tray.deleteLater()

# ========== Poller new_arrivals ==========


class TestPollerNewArrivals:
    def test_first_poll_does_not_emit_new_arrivals(self, monkeypatch):
        """首次轮询不触发 new_arrivals（启动时已有 pending 不算"新"）。"""
        from client.approval_panel.poller import Poller

        class FakeResp:
            status_code = 200

            def json(self):
                return {"pending": [{"approval_id": "a1"}]}

        def fake_get(url, timeout=None):
            return FakeResp()

        import client.approval_panel.poller as poller_mod

        monkeypatch.setattr(poller_mod.requests, "get", fake_get)

        poller = Poller()
        arrivals = []
        poller.new_arrivals.connect(lambda ids: arrivals.append(ids))
        poller._poll()
        assert _wait_poll_done(poller)
        assert arrivals == []
        poller.deleteLater()

    def test_new_id_emits_new_arrivals(self, monkeypatch):
        from client.approval_panel.poller import Poller

        responses = [
            {"pending": [{"approval_id": "a1"}]},
            {"pending": [{"approval_id": "a1"}, {"approval_id": "a2"}]},
        ]

        class FakeResp:
            def __init__(self, data):
                self.status_code = 200
                self._data = data

            def json(self):
                return self._data

        def fake_get(url, timeout=None):
            return FakeResp(responses.pop(0))

        import client.approval_panel.poller as poller_mod

        monkeypatch.setattr(poller_mod.requests, "get", fake_get)

        poller = Poller()
        arrivals = []
        poller.new_arrivals.connect(lambda ids: arrivals.append(ids))

        poller._poll()  # first poll: a1, no arrivals
        assert _wait_poll_done(poller)
        assert arrivals == []

        poller._poll()  # second poll: a1 + a2, a2 is new
        assert _wait_poll_done(poller)
        assert arrivals == [["a2"]]
        poller.deleteLater()

    def test_no_new_id_does_not_emit(self, monkeypatch):
        from client.approval_panel.poller import Poller

        responses = [
            {"pending": [{"approval_id": "a1"}]},
            {"pending": [{"approval_id": "a1"}]},  # same, no new
        ]

        class FakeResp:
            def __init__(self, data):
                self.status_code = 200
                self._data = data

            def json(self):
                return self._data

        def fake_get(url, timeout=None):
            return FakeResp(responses.pop(0))

        import client.approval_panel.poller as poller_mod

        monkeypatch.setattr(poller_mod.requests, "get", fake_get)

        poller = Poller()
        arrivals = []
        poller.new_arrivals.connect(lambda ids: arrivals.append(ids))

        poller._poll()
        assert _wait_poll_done(poller)
        poller._poll()
        assert _wait_poll_done(poller)
        assert arrivals == []
        poller.deleteLater()

    def test_removed_id_not_in_arrivals(self, monkeypatch):
        """ID 从 pending 消失再重新出现时算"新"。"""
        from client.approval_panel.poller import Poller

        responses = [
            {"pending": [{"approval_id": "a1"}]},
            {"pending": []},  # a1 消失
            {"pending": [{"approval_id": "a1"}]},  # a1 重新出现，算新
        ]

        class FakeResp:
            def __init__(self, data):
                self.status_code = 200
                self._data = data

            def json(self):
                return self._data

        def fake_get(url, timeout=None):
            return FakeResp(responses.pop(0))

        import client.approval_panel.poller as poller_mod

        monkeypatch.setattr(poller_mod.requests, "get", fake_get)

        poller = Poller()
        arrivals = []
        poller.new_arrivals.connect(lambda ids: arrivals.append(ids))

        poller._poll()  # a1, no arrivals (first)
        assert _wait_poll_done(poller)
        poller._poll()  # empty, no arrivals
        assert _wait_poll_done(poller)
        poller._poll()  # a1 again, arrivals
        assert _wait_poll_done(poller)
        assert arrivals == [["a1"]]
        poller.deleteLater()


# ========== Panel window_activated + closeEvent ==========


class TestPanelActivation:
    def test_window_activated_signal_emits(self):
        from PySide6.QtCore import QEvent

        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        received = []
        panel.window_activated.connect(lambda: received.append(True))
        # 模拟 WindowActivate 事件
        event = QEvent(QEvent.Type.WindowActivate)
        panel.changeEvent(event)
        assert received == [True]
        panel.deleteLater()

    def test_non_activate_event_does_not_emit(self):
        from PySide6.QtCore import QEvent

        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        received = []
        panel.window_activated.connect(lambda: received.append(True))
        event = QEvent(QEvent.Type.WindowDeactivate)
        panel.changeEvent(event)
        assert received == []
        panel.deleteLater()


class TestPanelCloseEvent:
    def test_close_in_tray_mode_hides_window(self, monkeypatch):
        """show_in_tray=True：关窗 → ignore + hide。"""
        from client.approval_panel import settings as panel_settings
        from client.approval_panel.panel import ApprovalPanel

        monkeypatch.setattr(panel_settings, "get_show_in_tray", lambda: True)
        panel = ApprovalPanel()
        panel.show()
        assert panel.isVisible()

        # 模拟 closeEvent
        from PySide6.QtGui import QCloseEvent

        event = QCloseEvent()
        panel.closeEvent(event)
        assert event.isAccepted() is False  # event.ignore()
        assert panel.isVisible() is False  # hidden
        panel.deleteLater()

    def test_close_in_non_tray_mode_accepts(self, monkeypatch):
        """show_in_tray=False：关窗 → 正常关闭。"""
        from client.approval_panel import settings as panel_settings
        from client.approval_panel.panel import ApprovalPanel

        monkeypatch.setattr(panel_settings, "get_show_in_tray", lambda: False)
        panel = ApprovalPanel()
        panel.show()

        from PySide6.QtGui import QCloseEvent

        event = QCloseEvent()
        panel.closeEvent(event)
        assert event.isAccepted() is True
        panel.deleteLater()
