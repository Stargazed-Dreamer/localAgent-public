"""审批面板进程骨架测试（Ticket 04）。

验证：
- 面板主窗口可实例化（QMainWindow 不崩）
- 心跳 worker 和轮询 worker 可创建
- QSettings 读写 show_in_tray / window_geometry
- 单实例锁逻辑

不测真实 HTTP 请求（mock requests）。
不测真实 server 在线（由 test_approval_panel_api.py 覆盖）。
"""

import pytest


@pytest.fixture(autouse=True)
def _qapp():
    """每个测试提供 QApplication（offscreen，不弹窗）。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _mock_requests(monkeypatch):
    """mock requests 避免 worker 真发 HTTP。"""

    class FakeResp:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {}

        def json(self):
            return self._json

    def fake_post(url, json=None, timeout=None):
        return FakeResp(200, {"ok": True})

    def fake_get(url, timeout=None):
        return FakeResp(200, {"pending": [], "count": 0})

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)


class TestPanelSkeleton:
    def test_panel_instantiates(self):
        """ApprovalPanel 可实例化。"""
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        assert panel.windowTitle() == "LocalAgent 审批面板"
        assert panel.minimumSize().width() >= 400
        panel.deleteLater()

    def test_panel_handles_heartbeat_signals(self):
        """面板能接收心跳信号并更新状态。"""
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_heartbeat_ok()
        assert "在线" in panel._status_label.text()
        panel.on_heartbeat_fail("timeout")
        assert "离线" in panel._status_label.text()
        panel.deleteLater()

    def test_panel_handles_pending_signal(self):
        """面板能接收 pending 列表信号。"""
        from client.approval_panel.panel import ApprovalPanel

        panel = ApprovalPanel()
        panel.on_pending_updated([{"approval_id": "x"}, {"approval_id": "y"}])
        assert "2" in panel.statusBar().currentMessage()
        panel.deleteLater()


class TestHeartbeatWorker:
    def test_worker_creates(self):
        """HeartbeatWorker 可创建。"""
        from client.approval_panel.heartbeat import HeartbeatWorker

        worker = HeartbeatWorker()
        assert worker._timer.interval() == 10_000
        worker.deleteLater()

    def test_worker_emits_ok(self, monkeypatch):
        """心跳成功时发射 heartbeat_ok 信号。"""
        from client.approval_panel.heartbeat import HeartbeatWorker

        worker = HeartbeatWorker()
        received = []
        worker.heartbeat_ok.connect(lambda: received.append(True))
        worker._send()
        assert received == [True]
        worker.deleteLater()

    def test_worker_emits_fail_on_exception(self, monkeypatch):
        """请求异常时发射 heartbeat_fail 信号。"""
        import requests

        from client.approval_panel.heartbeat import HeartbeatWorker

        def fake_post(url, json=None, timeout=None):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", fake_post)

        worker = HeartbeatWorker()
        received = []
        worker.heartbeat_fail.connect(lambda e: received.append(e))
        worker._send()
        assert len(received) == 1
        assert "refused" in received[0]
        worker.deleteLater()


class TestPoller:
    def test_poller_creates(self):
        """Poller 可创建。"""
        from client.approval_panel.poller import Poller

        poller = Poller()
        assert poller._timer.interval() == 2_000
        poller.deleteLater()

    def test_poller_emits_pending(self):
        """轮询成功时发射 pending_updated 信号。"""
        from client.approval_panel.poller import Poller

        poller = Poller()
        received = []
        poller.pending_updated.connect(lambda p: received.append(p))
        poller._poll()
        assert len(received) == 1
        assert isinstance(received[0], list)
        poller.deleteLater()


class TestSettings:
    def test_show_in_tray_default(self, monkeypatch):
        """show_in_tray 默认 True。"""
        # mock QSettings 返回默认值
        from client.approval_panel import settings as panel_settings

        class FakeSettings:
            def value(self, key, default=None, type=None):
                if key == "show_in_tray":
                    return default
                return None

            def setValue(self, key, value):
                pass

        monkeypatch.setattr(panel_settings, "get_settings", lambda: FakeSettings())
        assert panel_settings.get_show_in_tray() is True

    def test_show_in_tray_set(self, monkeypatch):
        """set_show_in_tray 持久化值。"""
        from client.approval_panel import settings as panel_settings

        store = {}

        class FakeSettings:
            def value(self, key, default=None, type=None):
                return store.get(key, default)

            def setValue(self, key, value):
                store[key] = value

        monkeypatch.setattr(panel_settings, "get_settings", lambda: FakeSettings())
        panel_settings.set_show_in_tray(False)
        assert panel_settings.get_show_in_tray() is False
        panel_settings.set_show_in_tray(True)
        assert panel_settings.get_show_in_tray() is True


class TestSingleInstance:
    def test_single_instance_first_succeeds(self):
        """首次获取单实例锁成功。"""
        from PySide6.QtCore import QSharedMemory
        from PySide6.QtWidgets import QApplication

        from client.approval_panel.main import _SINGLE_INSTANCE_KEY

        app = QApplication.instance()
        # 清理可能残留的共享内存
        existing = QSharedMemory(_SINGLE_INSTANCE_KEY)
        if existing.attach():
            existing.detach()

        mem = QSharedMemory(_SINGLE_INSTANCE_KEY, app)
        assert mem.create(1)
        assert mem.isAttached()
        mem.detach()
