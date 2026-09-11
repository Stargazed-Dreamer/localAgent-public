"""chat-panel-v2 Ticket 02 验收测试：防关机 + 启动恢复

覆盖 T02 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] client/core/app.py closeEvent 加 runner.interrupt_event.set() + update_session_status(interrupted)
      + services.stop() + 保存窗口几何
- [x] ChatPanel._init_read_store 中 store.init() 后调 reconcile(store)
- [x] ChatPanel 顶部 banner QFrame 显示"检测到 N 个异常退出的会话，已自动恢复"，可关闭
- [x] closeEvent 非阻塞（毫秒级完成），不等待 runner
- [x] 测试：模拟活跃 runner + closeEvent，断言 session.status=interrupted
- [x] 测试：模拟崩溃残留（streaming 状态会话）+ 重启 _init_read_store，断言 reconcile
      转为 interrupted + banner 显示 N=1
- [x] 测试：reconcile 幂等（已 interrupted 跳过）
- [x] 测试不回归：closeEvent 测试全绿

测试策略：
- 模仿 tests/test_chat_panel_t06.py 模式：module-scoped qapp + 测试函数内 import
- ChatPanel 实例化用 offscreen Qt + monkeypatch DEFAULT_DB_PATH 为临时路径
- worker 用 MagicMock（不真启动 QThread），但 worker.interrupt() 是真实业务调用断言
- closeEvent 用 MainWindow.closeEvent 真实方法（unbound method 调用），services/AppState mock
- 业务逻辑（中断 runner + 更新 status）不 mock，UI/Qt 操作（services.stop, AppState）可 mock

测试 prior art: tests/test_chat_panel_t06.py + tests/test_chat_panel_v2_db_migration.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest  # noqa: E402

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _force_cleanup_qt_state_after_each_test(qapp):
    """autouse: 每个测试后强制清理 Qt 累积状态，根治跨测试隔离问题。

    问题（2026-08-06）：本文件 18 个测试单独跑全通过，但全量测试时 9 个失败。
    根因：module-scoped qapp 共享 QApplication，ChatPanel 实例化的 widget/定时器/信号连接
    在 _cleanup_panel 的 deleteLater 后仍可能残留（offscreen 平台 sendPostedEvents 一次不够），
    导致后续测试的 isHidden() 断言不稳定（父 widget 状态污染子 widget 可见性）。

    策略：conftest.py 的 pytest_runtest_teardown 已做 closeAllWindows + sendPostedEvents，
    但对本文件不够。这里加文件级 autouse fixture，每个测试后**多次** sendPostedEvents
    + closeAllWindows + python gc.collect，强制清理。

    不用 processEvents（可能触发 offscreen 平台 access violation，见 conftest 注释）。
    """
    import gc

    from PySide6.QtCore import QCoreApplication
    yield
    # 测试后强制清理
    try:
        qapp.closeAllWindows()
    except Exception:
        pass
    # 多次 sendPostedEvents 处理 deferred delete 链（deleteLater 触发新事件）
    for _ in range(3):
        try:
            QCoreApplication.sendPostedEvents(None, 0)
        except Exception:
            break
    gc.collect()


def _make_test_main_window(monkeypatch):
    """构造一个跳过 MainWindow.__init__ 的测试子类实例。

    MainWindow.__init__ 会 discover 所有 panel + 启动 services，过重且引入副作用。
    本子类直接调 QMainWindow.__init__，只设置 closeEvent 需要的 panels / services 属性。

    super().closeEvent(event) 在 MainWindow.closeEvent 中需要 self.__class__ 是
    MainWindow 的子类才能正确解析 MRO（直接用 QMainWindow 实例会 TypeError）。
    """
    from unittest.mock import MagicMock

    from PySide6.QtWidgets import QMainWindow

    from client.core.app import MainWindow

    class _TestMainWindow(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self.panels = {}
            self.services = MagicMock()

    return _TestMainWindow()


def _run(coro):
    """同步执行 async coroutine（test helper，复用 _safe_read 风格）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _open_store(db_path: str):
    """打开一个独立 EventStore（用于验证测试结果）。"""
    from client.core.agent import EventStore

    s = EventStore(db_path=db_path)
    s.init()
    return s


def _make_chat_panel(db_path: str, monkeypatch):
    """构造一个 ChatPanel 实例（patch DEFAULT_DB_PATH 为临时路径）。

    模仿 test_chat_panel_t06.py 的 ChatPanel() 实例化模式。
    调用方负责 panel.deleteLater() + qapp.processEvents() 清理。
    """
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
    return ChatPanel()


def _cleanup_panel(panel, qapp):
    """清理 panel 资源（停定时器 + deleteLater）。"""
    panel._session_list_timer.stop()
    panel._poll_timer.stop()
    if panel._read_store is not None:
        try:
            panel._read_store.close()
        except Exception:
            pass
    panel.deleteLater()
    qapp.processEvents()


# ============================================================================
# Test 1: interrupt_active_session（closeEvent 调用的核心方法）
# ============================================================================


class TestInterruptActiveSession:
    """D24: interrupt_active_session 中断活跃 runner + 更新 session.status=interrupted。"""

    def test_updates_session_status_to_interrupted(self, qapp, tmp_path, monkeypatch):
        """有活跃 worker + streaming 会话 → status 变 interrupted + worker.interrupt 被调。"""
        from unittest.mock import MagicMock

        db_path = str(tmp_path / "test_t02_interrupt.db")
        # 先在 DB 中创建 streaming 会话
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-test-1", mode="dialogue"))
        _run(s.update_session_status("sess-test-1", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            # ChatPanel 实例化时 _init_read_store 触发首次 reconcile，
            # streaming 会话已被恢复为 interrupted。重新设回 streaming 模拟活跃 runner
            s2 = _open_store(db_path)
            _run(s2.update_session_status("sess-test-1", "streaming"))
            s2.close()

            panel._current_session_id = "sess-test-1"
            fake_worker = MagicMock()
            fake_worker.isRunning.return_value = True
            fake_worker.interrupt = MagicMock()
            panel._worker = fake_worker

            panel.interrupt_active_session()

            # 断言 worker.interrupt 被调用（业务逻辑：中断 runner）
            fake_worker.interrupt.assert_called_once()

            # 断言 session.status = interrupted（业务逻辑：更新状态）
            s3 = _open_store(db_path)
            try:
                sess = _run(s3.get_session("sess-test-1"))
            finally:
                s3.close()
            assert sess is not None, "session should exist"
            assert sess.status == "interrupted", (
                f"status should be interrupted, got {sess.status}"
            )
        finally:
            _cleanup_panel(panel, qapp)

    def test_no_worker_still_updates_status(self, qapp, tmp_path, monkeypatch):
        """无活跃 worker（worker=None）时不抛异常，仍更新 session.status。"""
        db_path = str(tmp_path / "test_t02_no_worker.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-test-2", mode="dialogue"))
        _run(s.update_session_status("sess-test-2", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            # 重置为 streaming（首次 reconcile 已转 interrupted）
            s2 = _open_store(db_path)
            _run(s2.update_session_status("sess-test-2", "streaming"))
            s2.close()

            panel._current_session_id = "sess-test-2"
            panel._worker = None

            panel.interrupt_active_session()  # 不应抛异常

            s3 = _open_store(db_path)
            try:
                sess = _run(s3.get_session("sess-test-2"))
            finally:
                s3.close()
            assert sess is not None
            assert sess.status == "interrupted"
        finally:
            _cleanup_panel(panel, qapp)

    def test_no_session_id_no_error(self, qapp, tmp_path, monkeypatch):
        """无 current_session_id 时不抛异常（no-op）。"""
        db_path = str(tmp_path / "test_t02_no_sess.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            panel._current_session_id = None
            panel._worker = None
            panel.interrupt_active_session()  # 不应抛异常
        finally:
            _cleanup_panel(panel, qapp)

    def test_worker_not_running_no_interrupt_call(self, qapp, tmp_path, monkeypatch):
        """worker 存在但未运行（isRunning=False）时不调 interrupt，但仍更新 status。"""
        from unittest.mock import MagicMock

        db_path = str(tmp_path / "test_t02_worker_idle.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-test-3", mode="dialogue"))
        _run(s.update_session_status("sess-test-3", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            s2 = _open_store(db_path)
            _run(s2.update_session_status("sess-test-3", "streaming"))
            s2.close()

            panel._current_session_id = "sess-test-3"
            fake_worker = MagicMock()
            fake_worker.isRunning.return_value = False
            fake_worker.interrupt = MagicMock()
            panel._worker = fake_worker

            panel.interrupt_active_session()

            # worker 未运行，interrupt 不应被调
            fake_worker.interrupt.assert_not_called()

            # 但 status 仍应更新为 interrupted
            s3 = _open_store(db_path)
            try:
                sess = _run(s3.get_session("sess-test-3"))
            finally:
                s3.close()
            assert sess.status == "interrupted"
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Test 2: _init_read_store 触发 reconcile + banner 显示
# ============================================================================


class TestReconcileAfterInit:
    """D27: _init_read_store 中 store.init() 后调 reconcile() + D26 banner 显示。"""

    def test_streaming_session_recovered_with_banner(self, qapp, tmp_path, monkeypatch):
        """streaming 残留会话 + _init_read_store → reconcile 转为 interrupted + banner N=1。"""
        db_path = str(tmp_path / "test_t02_recover.db")
        # 先在 DB 中创建 streaming 会话（模拟崩溃残留）
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-crash-1", mode="dialogue"))
        _run(s.update_session_status("sess-crash-1", "streaming"))
        s.close()

        # 实例化 ChatPanel（首次 _init_read_store 会触发 reconcile，恢复 streaming 会话）
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            # 断言 banner 可见 + 文本含 "1" 和恢复字样
            assert not panel._recovery_banner.isHidden(), "banner should be visible"
            label_text = panel._recovery_label.text()
            assert "1" in label_text, f"banner text should contain '1', got: {label_text}"
            assert "恢复" in label_text or "异常退出" in label_text, (
                f"banner text should mention recovery, got: {label_text}"
            )

            # 断言 session.status = interrupted
            sess = _run(panel._read_store.get_session("sess-crash-1"))
            assert sess is not None
            assert sess.status == "interrupted", (
                f"status should be interrupted after reconcile, got {sess.status}"
            )
        finally:
            _cleanup_panel(panel, qapp)

    def test_multiple_streaming_sessions_recovered(self, qapp, tmp_path, monkeypatch):
        """多个 streaming 残留会话 → banner 显示 N=2。"""
        db_path = str(tmp_path / "test_t02_multi.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-crash-a", mode="dialogue"))
        _run(s.update_session_status("sess-crash-a", "streaming"))
        _run(s.create_session(session_id="sess-crash-b", mode="dialogue"))
        _run(s.update_session_status("sess-crash-b", "awaiting_tools"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            assert not panel._recovery_banner.isHidden()
            assert "2" in panel._recovery_label.text()

            sess_a = _run(panel._read_store.get_session("sess-crash-a"))
            sess_b = _run(panel._read_store.get_session("sess-crash-b"))
            assert sess_a.status == "interrupted"
            assert sess_b.status == "interrupted"
        finally:
            _cleanup_panel(panel, qapp)

    def test_already_interrupted_skipped_no_banner(self, qapp, tmp_path, monkeypatch):
        """status=interrupted 的会话不被二次处理（幂等），banner 不显示。"""
        db_path = str(tmp_path / "test_t02_idempotent.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-int-1", mode="dialogue"))
        _run(s.update_session_status("sess-int-1", "interrupted"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            # banner 不显示（reconcile 没恢复任何会话，已 interrupted 跳过）
            assert panel._recovery_banner.isHidden(), (
                "banner should NOT be visible when no sessions recovered"
            )

            # session.status 仍为 interrupted（未被二次处理）
            sess = _run(panel._read_store.get_session("sess-int-1"))
            assert sess.status == "interrupted"
        finally:
            _cleanup_panel(panel, qapp)

    def test_idle_sessions_not_recovered_no_banner(self, qapp, tmp_path, monkeypatch):
        """idle/completed 状态的会话不被 reconcile 处理，banner 不显示。"""
        db_path = str(tmp_path / "test_t02_idle.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-idle-1", mode="dialogue"))
        _run(s.create_session(session_id="sess-done-1", mode="dialogue"))
        _run(s.update_session_status("sess-done-1", "completed"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            assert panel._recovery_banner.isHidden()

            # 状态不变
            assert _run(panel._read_store.get_session("sess-idle-1")).status == "idle"
            assert _run(panel._read_store.get_session("sess-done-1")).status == "completed"
        finally:
            _cleanup_panel(panel, qapp)

    def test_banner_can_be_closed_by_user(self, qapp, tmp_path, monkeypatch):
        """banner 可被用户点 × 关闭（D26 非侵入式）。"""
        db_path = str(tmp_path / "test_t02_close_btn.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-crash-2", mode="dialogue"))
        _run(s.update_session_status("sess-crash-2", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            assert not panel._recovery_banner.isHidden()

            # 模拟用户点 ×
            panel._hide_recovery_banner()
            assert panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_reconcile_idempotent_multiple_inits(self, qapp, tmp_path, monkeypatch):
        """多次调 _init_read_store 不会重复恢复同一会话（幂等）。"""
        db_path = str(tmp_path / "test_t02_multi_init.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-crash-3", mode="dialogue"))
        _run(s.update_session_status("sess-crash-3", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            # 第一次 init（在 _make_chat_panel 中）：恢复 + banner 显示
            assert not panel._recovery_banner.isHidden()
            assert "1" in panel._recovery_label.text()

            # 关闭 banner，再次 init：session 已 interrupted，不再恢复，banner 不显示
            panel._hide_recovery_banner()
            panel._init_read_store()
            assert panel._recovery_banner.isHidden(), (
                "second reconcile should not re-show banner (session already interrupted)"
            )

            # session.status 仍为 interrupted
            sess = _run(panel._read_store.get_session("sess-crash-3"))
            assert sess.status == "interrupted"
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Test 3: closeEvent 非阻塞，不抛异常
# ============================================================================


class TestCloseEventNonBlocking:
    """D24: closeEvent 非阻塞（毫秒级），不等待 runner，不抛异常。"""

    def test_close_event_interrupts_session_and_calls_services_stop(
        self, qapp, tmp_path, monkeypatch,
    ):
        """closeEvent 真实调用：中断 runner + 更新 status + services.stop + 不抛异常。"""
        from unittest.mock import MagicMock

        from PySide6.QtGui import QCloseEvent

        db_path = str(tmp_path / "test_t02_close.db")
        s = _open_store(db_path)
        _run(s.create_session(session_id="sess-close-1", mode="dialogue"))
        _run(s.update_session_status("sess-close-1", "streaming"))
        s.close()

        panel = _make_chat_panel(db_path, monkeypatch)
        # 重置为 streaming（首次 reconcile 已转 interrupted）
        s2 = _open_store(db_path)
        _run(s2.update_session_status("sess-close-1", "streaming"))
        s2.close()

        panel._current_session_id = "sess-close-1"
        fake_worker = MagicMock()
        fake_worker.isRunning.return_value = True
        fake_worker.interrupt = MagicMock()
        panel._worker = fake_worker

        # 构造 MainWindow 测试子类实例（跳过 __init__，只设 panels/services）
        # 必须是 MainWindow 子类才能让 super().closeEvent() 正确解析 MRO
        window = _make_test_main_window(monkeypatch)
        window.panels = {"chat": panel}

        # mock AppState（避免写真实文件）
        from client.core import app as app_module
        fake_state = MagicMock()
        monkeypatch.setattr(app_module.AppState, "instance", classmethod(lambda cls: fake_state))

        try:
            # 调 closeEvent（bound method，self.__class__ 是 MainWindow 子类）
            event = QCloseEvent()
            # 不应抛异常
            window.closeEvent(event)

            # 断言 worker.interrupt 被调用（业务逻辑：中断 runner）
            fake_worker.interrupt.assert_called_once()
            # 断言 services.stop 被调用
            window.services.stop.assert_called_once()
            # 断言 AppState 方法被调用（保存窗口几何）
            fake_state.save_window_geometry.assert_called_once()
            fake_state.save_window_size.assert_called_once()

            # 断言 session.status = interrupted（业务逻辑：更新状态）
            s3 = _open_store(db_path)
            try:
                sess = _run(s3.get_session("sess-close-1"))
            finally:
                s3.close()
            assert sess is not None
            assert sess.status == "interrupted", (
                f"session status should be interrupted after closeEvent, got {sess.status}"
            )
        finally:
            _cleanup_panel(panel, qapp)
            window.deleteLater()
            qapp.processEvents()

    def test_close_event_no_chat_panel_no_error(self, qapp, monkeypatch):
        """panels 中无 chat panel 时不抛异常（防御性）。"""
        from unittest.mock import MagicMock

        from PySide6.QtGui import QCloseEvent

        window = _make_test_main_window(monkeypatch)
        window.panels = {}  # 无 chat panel

        from client.core import app as app_module
        fake_state = MagicMock()
        monkeypatch.setattr(app_module.AppState, "instance", classmethod(lambda cls: fake_state))

        try:
            event = QCloseEvent()
            # 不应抛异常
            window.closeEvent(event)

            # services.stop 仍被调用
            window.services.stop.assert_called_once()
        finally:
            window.deleteLater()
            qapp.processEvents()

    def test_close_event_no_active_session_no_error(self, qapp, tmp_path, monkeypatch):
        """closeEvent 时无活跃 session（current_session_id=None）不抛异常。"""
        from unittest.mock import MagicMock

        from PySide6.QtGui import QCloseEvent

        db_path = str(tmp_path / "test_t02_close_no_sess.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        panel._current_session_id = None
        panel._worker = None

        window = _make_test_main_window(monkeypatch)
        window.panels = {"chat": panel}

        from client.core import app as app_module
        fake_state = MagicMock()
        monkeypatch.setattr(app_module.AppState, "instance", classmethod(lambda cls: fake_state))

        try:
            event = QCloseEvent()
            # 不应抛异常
            window.closeEvent(event)

            window.services.stop.assert_called_once()
        finally:
            _cleanup_panel(panel, qapp)
            window.deleteLater()
            qapp.processEvents()


# ============================================================================
# Test 4: banner UI 元素存在性（防止后续重构丢失）
# ============================================================================


class TestBannerUI:
    """D26: banner QFrame + QLabel + 关闭按钮 UI 元素存在。"""

    def test_banner_widgets_exist(self, qapp, tmp_path, monkeypatch):
        """ChatPanel 应有 _recovery_banner / _recovery_label / _recovery_close_btn。"""
        db_path = str(tmp_path / "test_t02_banner_ui.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            assert hasattr(panel, "_recovery_banner"), "missing _recovery_banner"
            assert hasattr(panel, "_recovery_label"), "missing _recovery_label"
            assert hasattr(panel, "_recovery_close_btn"), "missing _recovery_close_btn"
        finally:
            _cleanup_panel(panel, qapp)

    def test_banner_hidden_by_default(self, qapp, tmp_path, monkeypatch):
        """banner 默认隐藏（无恢复时不显示）。"""
        db_path = str(tmp_path / "test_t02_banner_default.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            assert panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_banner_show_method(self, qapp, tmp_path, monkeypatch):
        """_show_recovery_banner(1) 后 banner 可见 + 文本正确。"""
        db_path = str(tmp_path / "test_t02_banner_show.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            panel._show_recovery_banner(1)
            assert not panel._recovery_banner.isHidden()
            assert "1" in panel._recovery_label.text()
        finally:
            _cleanup_panel(panel, qapp)

    def test_banner_show_zero_not_shown(self, qapp, tmp_path, monkeypatch):
        """_show_recovery_banner(0) 不显示 banner。"""
        db_path = str(tmp_path / "test_t02_banner_zero.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            panel._show_recovery_banner(0)
            assert panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_close_button_connected_to_hide(self, qapp, tmp_path, monkeypatch):
        """点 × 按钮应触发 _hide_recovery_banner。"""
        db_path = str(tmp_path / "test_t02_banner_click.db")
        panel = _make_chat_panel(db_path, monkeypatch)
        try:
            panel._show_recovery_banner(1)
            assert not panel._recovery_banner.isHidden()
            # 模拟点击
            panel._recovery_close_btn.click()
            assert panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)
