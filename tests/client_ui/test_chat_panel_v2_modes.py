"""chat-panel-v2 Ticket 09 验收测试：对话控制三模式（D34 + D34a + D35 + D36 + D36a + D37）

覆盖 T09 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] 输入框上方 3 tab widget：发送 / 队列 / 引导
- [x] runner 空闲态：只显示发送 tab
- [x] runner 运行态：3 tab 全显示，默认选中引导 tab（每次重置，不持久化）
- [x] 运行态切 tab 不影响 runner，仅决定下次点发送键的行为
- [x] 单一发送按钮：按当前 tab 触发
  - 发送：facade.interrupt() + start 新 run
  - 队列：消息存排队区单条（已有排队时队列 tab 禁用）
  - 引导：facade.steer()，steer 消息以 user 样式 + 小字"用户引导"标记
- [x] 排队区：输入框上方一行，单条，超长截断 + tooltip 全文，右侧编辑按钮
- [x] 编辑按钮：取消排队 + 消息内容追加到输入框末尾（输入框有内容时空行连接）
- [x] runner 完成检测：主线程轮询发现 run 结束 → 检查排队区 → 若有排队 → 清空排队 + facade.start 启动新 run

测试策略：
- 源码静态扫描：grep mode 常量 + 3 tab 按钮 + 排队区 widget
- 运行时实例化：ChatPanel 实例化后验证初始状态（空闲态 + 只显示发送 tab）
- 行为测试：_set_running_state 切换、_on_send dispatch、queue CRUD、edit cancel
- mock worker：不真启动 QThread，用 MagicMock 模拟 worker.isRunning()

测试 prior art: tests/test_chat_panel_v2_sticky.py + test_chat_panel_v2_shutdown_recovery.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))



# ============================================================================
# Fixtures
# ============================================================================


def _make_chat_panel(monkeypatch, tmp_path):
    """构造 ChatPanel 实例（patch DEFAULT_DB_PATH 为临时路径）。"""
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    db_path = str(tmp_path / "modes_test.db")
    monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
    return ChatPanel()


def _cleanup_panel(panel, qapp):
    """清理 panel 资源。"""
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
# Part 2: 初始状态（空闲态）
# ============================================================================


class TestIdleState:
    """ChatPanel 初始状态：空闲态，只显示发送 tab（D34）。"""

    def test_initial_mode_is_send(self, qapp, monkeypatch, tmp_path):
        """初始化 → _get_current_mode() == MODE_SEND。"""
        from client.panels.chat import MODE_SEND

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._get_current_mode() == MODE_SEND
            assert panel._is_running_state is False
        finally:
            _cleanup_panel(panel, qapp)

    def test_idle_state_only_send_tab_visible(self, qapp, monkeypatch, tmp_path):
        """空闲态：只显示发送 tab，队列/引导 tab 隐藏。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            # 队列/引导 tab 在空闲态隐藏
            assert panel._mode_queue_btn.isVisible() is False
            assert panel._mode_steer_btn.isVisible() is False
        finally:
            _cleanup_panel(panel, qapp)

    def test_idle_state_send_tab_checked(self, qapp, monkeypatch, tmp_path):
        """空闲态：发送 tab 被选中。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._mode_send_btn.isChecked() is True
            assert panel._mode_queue_btn.isChecked() is False
            assert panel._mode_steer_btn.isChecked() is False
        finally:
            _cleanup_panel(panel, qapp)

    def test_idle_state_queue_bar_hidden(self, qapp, monkeypatch, tmp_path):
        """空闲态：排队区隐藏。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._queue_bar.isVisible() is False
            assert panel._queue_bar.maximumHeight() == 0
            assert panel._queued_text == ""
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 3: 运行态切换（_set_running_state）
# ============================================================================


class TestRunningStateTransition:
    """_set_running_state(True/False) 切换（D34 + D34a）。"""

    def test_running_state_shows_all_tabs(self, qapp, monkeypatch, tmp_path):
        """运行态：3 tab 全显示。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            # _messages_view 在 QStackedWidget 中（初始显示 start_page），
            # isVisible()/isVisibleTo(panel) 受父链隐藏影响恒返回 False。
            # isHidden() 只检查 setVisible(False) 是否被显式调用，正确反映逻辑可见性。
            assert not panel._mode_send_btn.isHidden()
            assert not panel._mode_queue_btn.isHidden()
            assert not panel._mode_steer_btn.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_running_state_defaults_to_steer_tab(self, qapp, monkeypatch, tmp_path):
        """运行态：默认选中引导 tab（D34a，每次重置）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            assert panel._mode_steer_btn.isChecked() is True
            assert panel._mode_send_btn.isChecked() is False
            assert panel._mode_queue_btn.isChecked() is False
            assert panel._get_current_mode() == "steer"
        finally:
            _cleanup_panel(panel, qapp)

    def test_back_to_idle_resets_to_send_tab(self, qapp, monkeypatch, tmp_path):
        """从运行态切回空闲态：重置到发送 tab。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            assert panel._mode_steer_btn.isChecked() is True
            panel._set_running_state(False)
            assert panel._mode_send_btn.isChecked() is True
            assert panel._mode_queue_btn.isVisible() is False
            assert panel._mode_steer_btn.isVisible() is False
        finally:
            _cleanup_panel(panel, qapp)

    def test_running_state_resets_each_time(self, qapp, monkeypatch, tmp_path):
        """每次进入运行态都重置到引导 tab（不持久化）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            # 第一次进入运行态
            panel._set_running_state(True)
            assert panel._mode_steer_btn.isChecked() is True
            # 用户切到发送 tab
            panel._mode_send_btn.setChecked(True)
            panel._on_mode_tab_clicked("send")
            assert panel._mode_send_btn.isChecked() is True
            # 切回空闲态再切运行态
            panel._set_running_state(False)
            panel._set_running_state(True)
            # 应再次重置到引导 tab
            assert panel._mode_steer_btn.isChecked() is True
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 4: 排队区 CRUD（D36 + D36a）
# ============================================================================


class TestQueueArea:
    """排队区行为（D36 + D36a）。"""

    def test_show_queue_displays_text(self, qapp, monkeypatch, tmp_path):
        """_show_queue 显示排队区 + 截断文本 + tooltip 全文。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            text = "请帮我分析这个数据"
            panel._show_queue(text)
            assert panel._queued_text == text
            # _queue_bar 在 _messages_view（QStackedWidget 中隐藏），用 isHidden() 判断逻辑可见性
            assert not panel._queue_bar.isHidden()
            assert panel._queue_bar.maximumHeight() != 0
            assert panel._queue_label.text() == text
            assert panel._queue_label.toolTip() == text
        finally:
            _cleanup_panel(panel, qapp)

    def test_show_queue_truncates_long_text(self, qapp, monkeypatch, tmp_path):
        """超长文本截断 + tooltip 保留全文。"""
        from client.panels.chat import _QUEUE_TEXT_TRUNCATE

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            long_text = "x" * (_QUEUE_TEXT_TRUNCATE + 50)
            panel._show_queue(long_text)
            display = panel._queue_label.text()
            assert len(display) <= _QUEUE_TEXT_TRUNCATE + 3  # 截断 + "..."
            assert display.endswith("...")
            assert panel._queue_label.toolTip() == long_text
        finally:
            _cleanup_panel(panel, qapp)

    def test_show_queue_disables_queue_tab(self, qapp, monkeypatch, tmp_path):
        """D36a：已有排队时禁用队列 tab。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)  # 运行态才能看到队列 tab
            panel._show_queue("queued text")
            assert panel._mode_queue_btn.isEnabled() is False
        finally:
            _cleanup_panel(panel, qapp)

    def test_clear_queue_resets_state(self, qapp, monkeypatch, tmp_path):
        """_clear_queue 清空排队区 + 重新启用队列 tab。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            panel._show_queue("queued text")
            assert panel._queued_text == "queued text"
            panel._clear_queue()
            assert panel._queued_text == ""
            assert panel._queue_bar.isVisible() is False
            assert panel._queue_bar.maximumHeight() == 0
            assert panel._mode_queue_btn.isEnabled() is True
        finally:
            _cleanup_panel(panel, qapp)

    def test_edit_queue_appends_to_input(self, qapp, monkeypatch, tmp_path):
        """D36：编辑按钮 = 取消排队 + 消息追加到输入框末尾。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._show_queue("queued message")
            panel._input_edit.setPlainText("existing input")
            panel._on_edit_queue()
            # 排队已清空
            assert panel._queued_text == ""
            # 输入框 = existing input + "\n" + queued message
            assert panel._input_edit.toPlainText() == "existing input\nqueued message"
        finally:
            _cleanup_panel(panel, qapp)

    def test_edit_queue_appends_to_empty_input(self, qapp, monkeypatch, tmp_path):
        """输入框为空时直接放入。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._show_queue("queued message")
            panel._input_edit.clear()
            panel._on_edit_queue()
            assert panel._input_edit.toPlainText() == "queued message"
            assert panel._queued_text == ""
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 5: 发送 dispatch（_on_send）
# ============================================================================


class TestSendDispatch:
    """_on_send 按当前 mode dispatch（D34）。"""

    def test_send_in_queue_mode_shows_queue(self, qapp, monkeypatch, tmp_path):
        """队列模式：消息存排队区 + 清空输入框。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            # 切到队列 tab
            panel._mode_queue_btn.setChecked(True)
            panel._on_mode_tab_clicked("queue")
            # 输入消息 + 发送
            panel._input_edit.setPlainText("next message")
            # mock worker（不真启动）
            panel._worker = MagicMock()
            panel._worker.isRunning.return_value = True
            panel._on_send()
            # 排队区显示消息
            assert panel._queued_text == "next message"
            assert not panel._queue_bar.isHidden()
            # 输入框清空
            assert panel._input_edit.toPlainText() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_send_in_steer_mode_calls_worker_steer(self, qapp, monkeypatch, tmp_path):
        """引导模式：调 worker.steer + 显示 steer 预览 + 清空输入框。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            # 切到引导 tab
            panel._mode_steer_btn.setChecked(True)
            panel._on_mode_tab_clicked("steer")
            # 输入消息
            panel._input_edit.setPlainText("steer this way")
            # mock worker
            panel._worker = MagicMock()
            panel._worker.isRunning.return_value = True
            panel._on_send()
            # 调了 worker.steer
            panel._worker.steer.assert_called_once_with("steer this way")
            # 输入框清空
            assert panel._input_edit.toPlainText() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_send_in_steer_mode_no_worker_noop(self, qapp, monkeypatch, tmp_path):
        """引导模式无运行 worker → no-op。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            panel._mode_steer_btn.setChecked(True)
            panel._on_mode_tab_clicked("steer")
            panel._input_edit.setPlainText("steer this way")
            # 不 mock worker（无 worker）
            panel._worker = None
            panel._on_send()
            # 输入框仍保留（未清空）
            assert panel._input_edit.toPlainText() == "steer this way"
        finally:
            _cleanup_panel(panel, qapp)

    def test_send_in_queue_mode_already_queued_noop(self, qapp, monkeypatch, tmp_path):
        """D36a：已有排队时，再次进队列模式应 no-op。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            panel._show_queue("first queue")
            # 模拟用户切到队列 tab（虽然 tab 已禁用）
            panel._mode_queue_btn.setChecked(True)
            panel._on_mode_tab_clicked("queue")
            panel._input_edit.setPlainText("second queue")
            panel._on_send()
            # 排队区仍是第一条
            assert panel._queued_text == "first queue"
            # 输入框保留（_send_in_queue_mode 直接 return）
            assert panel._input_edit.toPlainText() == "second queue"
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 6: run 完成后自动 start 排队消息（D36）
# ============================================================================


class TestQueueAutoStart:
    """_on_worker_finished → _check_queue_after_run → 自动 start 排队消息。"""

    def test_check_queue_after_run_no_queue_noop(self, qapp, monkeypatch, tmp_path):
        """无排队消息 → no-op。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._check_queue_after_run()
            # 无副作用
            assert panel._queued_text == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_check_queue_after_run_no_session_noop(self, qapp, monkeypatch, tmp_path):
        """有排队但无活跃会话 → no-op（不丢消息）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._show_queue("queued text")
            panel._current_session_id = ""  # 无会话
            panel._check_queue_after_run()
            # 排队消息仍保留
            assert panel._queued_text == "queued text"
        finally:
            _cleanup_panel(panel, qapp)

    def test_set_running_state_false_triggers_check(self, qapp, monkeypatch, tmp_path):
        """_set_running_state(False) 应触发 _check_queue_after_run。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._show_queue("queued text")
            panel._current_session_id = "test-session"
            # mock _on_send 避免真启动 worker
            called = []
            panel._on_send = lambda: called.append("started")
            panel._set_running_state(False)
            # 应自动触发 _on_send
            assert len(called) == 1
            # 排队已清空（_check_queue_after_run 内 _clear_queue）
            assert panel._queued_text == ""
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 7: 运行态切 tab 不影响 runner（D34）
# ============================================================================


class TestModeTabSwitchDoesNotAffectRunner:
    """运行态切 tab 仅更新 UI 样式，不触发 worker 调用（D34）。"""

    def test_switch_tab_does_not_call_worker(self, qapp, monkeypatch, tmp_path):
        """切 tab 不调 worker.interrupt / worker.steer。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            # mock worker
            panel._worker = MagicMock()
            panel._worker.isRunning.return_value = True
            # 切到发送 tab
            panel._mode_send_btn.setChecked(True)
            panel._on_mode_tab_clicked("send")
            # 切到队列 tab
            panel._mode_queue_btn.setChecked(True)
            panel._on_mode_tab_clicked("queue")
            # 切到引导 tab
            panel._mode_steer_btn.setChecked(True)
            panel._on_mode_tab_clicked("steer")
            # worker.interrupt / steer 未被调用
            panel._worker.interrupt.assert_not_called()
            panel._worker.steer.assert_not_called()
        finally:
            _cleanup_panel(panel, qapp)

    def test_switch_tab_updates_active_styles(self, qapp, monkeypatch, tmp_path):
        """切 tab 更新 active 样式（active tab 主块背景为 ACCENT_WASH）。"""
        from lib.ui import tokens

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._set_running_state(True)
            # 切到队列 tab
            panel._mode_queue_btn.setChecked(True)
            panel._on_mode_tab_clicked("queue")
            # 队列 tab 主块 background 应是 ACCENT_WASH（active）
            assert _main_block_bg(panel._mode_queue_btn.styleSheet()) == tokens.ACCENT_WASH
            # 发送 tab 主块 background 应是 transparent（inactive）
            # 注意：不能检查整段 stylesheet，因为 :hover 块也含 ACCENT_WASH
            assert _main_block_bg(panel._mode_send_btn.styleSheet()) == "transparent"
        finally:
            _cleanup_panel(panel, qapp)


def _main_block_bg(style: str) -> str:
    """提取 QPushButton { ... } 主块（不含 :hover/:disabled）的 background 值。

    _mode_tab_stylesheet 输出形如：
        "QPushButton { background: <bg>; ... } QPushButton:hover { ... } QPushButton:disabled { ... }"
    主块 = 开头到第一个 "QPushButton:" 之前。
    """
    main_block = style.split("QPushButton:")[0]
    if "background:" not in main_block:
        return ""
    after_bg = main_block.split("background:", 1)[1]
    # 取到下一个 ";" 为止
    return after_bg.split(";", 1)[0].strip()


def tokens_accent_wash_in(style: str) -> bool:
    """检查 stylesheet 是否含 ACCENT_WASH（active 标志）。

    注意：inactive 按钮的 :hover 块也含 ACCENT_WASH，因此判断 active/inactive
    应改用 _main_block_bg 比较主块 background。本函数仅用于粗略检查。
    """
    from lib.ui import tokens

    return tokens.ACCENT_WASH in style


# ============================================================================
# Part 8: 排队消息持久化（T09：source='queue' 写入 EventStore + 重启恢复）
# ============================================================================


def _run(coro):
    """同步执行 async 协程（测试用）。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestQueuePersistence:
    """排队消息持久化：_show_queue 写库 / _clear_queue 删库（T09）。"""

    def test_show_queue_persists_to_event_store(self, qapp, monkeypatch, tmp_path):
        """_show_queue(text) 写入 source='queue' visible=0 消息到 EventStore。"""
        from client.core.agent import EventStore

        db_path = str(tmp_path / "queue_persist.db")
        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 预创建会话
            store = EventStore(db_path=db_path)
            store.init()
            _run(store.create_session(session_id="s-queue-1"))
            store.close()

            panel._current_session_id = "s-queue-1"
            panel._show_queue("persisted queue text")

            # 验证 EventStore 中有 source='queue' 的消息
            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 1
            assert str(pending[0].content) == "persisted queue text"
            assert pending[0].source == "queue"
            assert pending[0].visible is False
            # _queued_msg_id 已记录
            assert panel._queued_msg_id == pending[0].id
            store.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_show_queue_no_session_skips_persist(self, qapp, monkeypatch, tmp_path):
        """无 current_session_id 时 _show_queue 不写库（仅 in-memory）。"""
        from client.core.agent import EventStore

        db_path = str(tmp_path / "queue_nosess.db")
        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            panel._current_session_id = ""
            panel._show_queue("text without session")

            # 无持久化
            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 0
            store.close()
            # in-memory 仍有
            assert panel._queued_text == "text without session"
            assert panel._queued_msg_id is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_clear_queue_deletes_persisted(self, qapp, monkeypatch, tmp_path):
        """_clear_queue 删除 EventStore 中的排队消息。"""
        from client.core.agent import EventStore

        db_path = str(tmp_path / "queue_clear.db")
        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            store = EventStore(db_path=db_path)
            store.init()
            _run(store.create_session(session_id="s-clear-1"))
            store.close()

            panel._current_session_id = "s-clear-1"
            panel._show_queue("to be cleared")
            assert panel._queued_msg_id is not None

            panel._clear_queue()

            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 0
            store.close()
            assert panel._queued_msg_id is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_edit_queue_deletes_persisted(self, qapp, monkeypatch, tmp_path):
        """_on_edit_queue 取消排队时删除持久化消息。"""
        from client.core.agent import EventStore

        db_path = str(tmp_path / "queue_edit.db")
        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            store = EventStore(db_path=db_path)
            store.init()
            _run(store.create_session(session_id="s-edit-1"))
            store.close()

            panel._current_session_id = "s-edit-1"
            panel._show_queue("queue to edit")
            assert panel._queued_msg_id is not None

            panel._on_edit_queue()

            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 0
            store.close()
            # 输入框有内容
            assert panel._input_edit.toPlainText() == "queue to edit"
        finally:
            _cleanup_panel(panel, qapp)


class TestQueueRecoveryOnStartup:
    """启动恢复：_recover_pending_queue 从 EventStore 恢复排队消息 + banner（T09）。"""

    def test_recover_restores_latest_queue(self, qapp, monkeypatch, tmp_path):
        """启动时发现 source='queue' 消息 → 恢复到 UI + banner。"""
        from client.core.agent import EventStore, Message

        db_path = str(tmp_path / "queue_recover.db")
        # 预写一条排队消息
        store = EventStore(db_path=db_path)
        store.init()
        _run(store.create_session(session_id="s-recover-1"))
        _run(store.append_message("s-recover-1", Message(
            role="user", content="queued before crash", source="queue", visible=False,
        )))
        store.close()

        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()  # _init_read_store 会调 _recover_pending_queue
        try:
            # 排队区恢复
            assert panel._queued_text == "queued before crash"
            assert panel._queued_msg_id is not None
            # 切换到排队消息所属会话
            assert panel._current_session_id == "s-recover-1"
            # banner 显示
            assert not panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_recover_no_queue_no_banner(self, qapp, monkeypatch, tmp_path):
        """无排队消息 → 不显示 banner。"""
        from client.core.agent import EventStore

        db_path = str(tmp_path / "queue_none.db")
        store = EventStore(db_path=db_path)
        store.init()
        _run(store.create_session(session_id="s-noqueue-1"))
        store.close()

        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            assert panel._queued_text == ""
            assert panel._queued_msg_id is None
            assert panel._recovery_banner.isHidden()
        finally:
            _cleanup_panel(panel, qapp)

    def test_recover_multiple_deletes_orphans(self, qapp, monkeypatch, tmp_path):
        """多条排队消息：恢复最新 + 删除孤儿（UI 只支持单条）。"""
        from client.core.agent import EventStore, Message

        db_path = str(tmp_path / "queue_multi.db")
        store = EventStore(db_path=db_path)
        store.init()
        _run(store.create_session(session_id="s-multi-1"))
        _run(store.append_message("s-multi-1", Message(
            role="user", content="old queue", source="queue", visible=False,
        )))
        _run(store.append_message("s-multi-1", Message(
            role="user", content="new queue", source="queue", visible=False,
        )))
        store.close()

        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 恢复最新的（"new queue"）
            assert panel._queued_text == "new queue"
            # 孤儿已删
            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 1
            assert str(pending[0].content) == "new queue"
            store.close()
        finally:
            _cleanup_panel(panel, qapp)

    def test_recovered_queue_can_be_cleared(self, qapp, monkeypatch, tmp_path):
        """恢复后的排队消息可被 _clear_queue 正常清理（_queued_msg_id 正确）。"""
        from client.core.agent import EventStore, Message

        db_path = str(tmp_path / "queue_recover_clear.db")
        store = EventStore(db_path=db_path)
        store.init()
        _run(store.create_session(session_id="s-rc-1"))
        _run(store.append_message("s-rc-1", Message(
            role="user", content="recovered queue", source="queue", visible=False,
        )))
        store.close()

        monkeypatch.setattr("client.panels.chat.DEFAULT_DB_PATH", db_path)
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            assert panel._queued_msg_id is not None
            panel._clear_queue()

            store = EventStore(db_path=db_path)
            store.init()
            pending = _run(store.find_pending_queue_messages())
            assert len(pending) == 0
            store.close()
            assert panel._queued_msg_id is None
        finally:
            _cleanup_panel(panel, qapp)
