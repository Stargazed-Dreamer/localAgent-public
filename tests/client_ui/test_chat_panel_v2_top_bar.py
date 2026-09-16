"""chat-panel-v2 Ticket 10 验收测试：顶栏重设计（D30 + D47）

覆盖 T10 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] 顶栏布局：last_updated + title(居中) + status + cost/token + 打字机 + refresh
- [x] 删除：copy_last_btn / thinking_toggle_btn / model_selector（顶栏位置）
- [x] 模型选择器移到输入区发送按钮左侧（_messages_view + _StartPage 都有）
- [x] last_updated QTimer 60s 刷新（HH:MM 格式）
- [x] title_label 截断超 32 字符 + 省略号
- [x] cost + token 显示规则（cost=0 不显示；cost<0.01 只显示 token；正常显示费用·token）
- [x] token 单位换算（B/K/M/B/T，整数 ≤3 位 + 1 位小数）
- [x] 打字机按钮文字加「打字机效果：」前缀

测试策略：
- 源码静态扫描：grep 新顶栏元素 + 移除的旧元素
- 运行时实例化：ChatPanel 实例化后验证顶栏 widget 存在 + 旧 widget 为 None
- 单元测试：_format_token_count / _truncate_title 单独测
- 行为测试：_update_status 不同 cost/token 组合的显示文本
- 计时器测试：_last_updated_timer interval=60000

测试 prior art: tests/test_chat_panel_v2_modes.py + test_chat_panel_t06.py
"""

from __future__ import annotations

import sys
from pathlib import Path

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

    db_path = str(tmp_path / "top_bar_test.db")
    monkeypatch.setattr(chat_module, "DEFAULT_DB_PATH", db_path)
    return ChatPanel()


def _cleanup_panel(panel, qapp):
    """清理 panel 资源。"""
    panel._session_list_timer.stop()
    panel._poll_timer.stop()
    # T10: 停止 last_updated_timer
    if hasattr(panel, "_last_updated_timer") and panel._last_updated_timer is not None:
        panel._last_updated_timer.stop()
    if panel._read_store is not None:
        try:
            panel._read_store.close()
        except Exception:
            pass
    panel.deleteLater()
    qapp.processEvents()
# ============================================================================
# Part 2: _format_token_count 单位换算
# ============================================================================


class TestFormatTokenCount:
    """_format_token_count 单位换算逻辑（D47：B/K/M/B/T，整数 ≤3 位 + 1 位小数）。"""

    def test_zero(self):
        from client.panels.chat import _format_token_count
        assert _format_token_count(0) == "0"

    def test_small_value_no_unit(self):
        """< 1000 用原值。"""
        from client.panels.chat import _format_token_count
        assert _format_token_count(1) == "1"
        assert _format_token_count(999) == "999"

    def test_thousands_use_k(self):
        """1000-999999 用 K（整数 ≤3 位 + 1 位小数）。"""
        from client.panels.chat import _format_token_count
        assert _format_token_count(1000) == "1.0K"
        assert _format_token_count(1500) == "1.5K"
        assert _format_token_count(10000) == "10.0K"
        assert _format_token_count(100000) == "100K"  # >= 100 取整

    def test_millions_use_m(self):
        """1M-999M 用 M。"""
        from client.panels.chat import _format_token_count
        assert _format_token_count(1_000_000) == "1.0M"
        assert _format_token_count(1_200_000) == "1.2M"
        assert _format_token_count(12_000_000) == "12.0M"
        assert _format_token_count(120_000_000) == "120M"

    def test_billions_use_b(self):
        """1B-999B 用 B。"""
        from client.panels.chat import _format_token_count
        assert _format_token_count(1_000_000_000) == "1.0B"
        assert _format_token_count(1_500_000_000) == "1.5B"

    def test_trillions_use_t(self):
        """>= 1T 用 T。"""
        from client.panels.chat import _format_token_count
        assert _format_token_count(1_000_000_000_000) == "1.0T"
        assert _format_token_count(1_200_000_000_000) == "1.2T"


# ============================================================================
# Part 3: _truncate_title 标题截断
# ============================================================================


class TestTruncateTitle:
    """_truncate_title 截断逻辑（D47：超 32 字符 + 省略号）。"""

    def test_empty_returns_empty(self):
        from client.panels.chat import _truncate_title
        assert _truncate_title("") == ""

    def test_short_title_unchanged(self):
        from client.panels.chat import _truncate_title
        assert _truncate_title("短标题") == "短标题"
        assert _truncate_title("a" * 32) == "a" * 32

    def test_long_title_truncated_with_ellipsis(self):
        from client.panels.chat import _truncate_title
        long_title = "a" * 50
        result = _truncate_title(long_title, max_chars=32)
        assert result == "a" * 32 + "…"
        assert len(result) == 33  # 32 + 省略号

    def test_custom_max_chars(self):
        from client.panels.chat import _truncate_title
        assert _truncate_title("abcdef", max_chars=3) == "abc…"
        assert _truncate_title("abc", max_chars=3) == "abc"


# ============================================================================
# Part 4: ChatPanel 顶栏 widget 实例化
# ============================================================================


class TestTopBarWidgets:
    """ChatPanel 顶栏 widget 实例化验证。"""

    def test_last_updated_label_exists(self, qapp, monkeypatch, tmp_path):
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._last_updated_label is not None
            # 初始有内容（当前时间 HH:MM）
            text = panel._last_updated_label.text()
            # HH:MM 格式（5 字符，含冒号）
            assert ":" in text
        finally:
            _cleanup_panel(panel, qapp)

    def test_title_label_exists(self, qapp, monkeypatch, tmp_path):
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._title_label is not None
            # 初始无活跃会话 → 空文本
            assert panel._title_label.text() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_cost_label_exists(self, qapp, monkeypatch, tmp_path):
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._cost_label is not None
            # 初始无 cost → 空文本
            assert panel._cost_label.text() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_typewriter_btn_has_prefix(self, qapp, monkeypatch, tmp_path):
        """打字机按钮文字应以「打字机效果：」开头（D47）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            text = panel._typewriter_btn.text()
            assert text.startswith("打字机效果：")
            # 默认档位是 fast（快速）
            assert "快速" in text or "关闭" in text or "正常" in text
        finally:
            _cleanup_panel(panel, qapp)

    def test_refresh_btn_exists(self, qapp, monkeypatch, tmp_path):
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._refresh_btn is not None
            assert panel._refresh_btn.toolTip()
        finally:
            _cleanup_panel(panel, qapp)

    def test_iter_label_is_none(self, qapp, monkeypatch, tmp_path):
        """_iter_label 应为 None（顶栏已移除，向后兼容）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._iter_label is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_thinking_toggle_btn_is_none(self, qapp, monkeypatch, tmp_path):
        """_thinking_toggle_btn 应为 None（顶栏已移除）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._thinking_toggle_btn is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_copy_last_btn_is_none(self, qapp, monkeypatch, tmp_path):
        """_copy_last_btn 应为 None（顶栏已移除）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._copy_last_btn is None
        finally:
            _cleanup_panel(panel, qapp)

    def test_model_selector_in_input_area(self, qapp, monkeypatch, tmp_path):
        """_model_selector 应在 input_bar 的 layout 中（不是 top_bar）。"""
        from PySide6.QtWidgets import QComboBox

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            # findChild 仍能找到（在 input_bar 内）
            selector = panel.findChild(QComboBox, "modelSelector")
            assert selector is not None
            # 父链应是 input_bar（objectName=chatInputBar），不是顶栏
            parent = selector.parentWidget()
            # 找最近的 objectName 为 chatInputBar 的祖先
            while parent is not None and parent.objectName() != "chatInputBar":
                parent = parent.parentWidget()
            assert parent is not None, "model_selector 应在 chatInputBar 内"
        finally:
            _cleanup_panel(panel, qapp)

    def test_start_page_has_model_selector(self, qapp, monkeypatch, tmp_path):
        """_StartPage 应有 _model_selector（D30：发送按钮左侧）。"""
        from PySide6.QtWidgets import QComboBox

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            selector = panel._start_page.findChild(QComboBox, "startPageModelSelector")
            assert selector is not None
        finally:
            _cleanup_panel(panel, qapp)

    def test_last_updated_timer_interval(self, qapp, monkeypatch, tmp_path):
        """_last_updated_timer interval=60000ms（60s）。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._last_updated_timer.interval() == 60000
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 5: _update_status token 显示规则（B2：删除 cost，只显示 token）
# ============================================================================


class TestUpdateStatusCostTokenDisplay:
    """_update_status token 显示规则（B2 后：只显示 token，不算 cost）。"""

    def test_cost_zero_token_zero_no_display(self, qapp, monkeypatch, tmp_path):
        """token=0 → 不显示。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("idle", iterations=0, total_tokens=0)
            assert panel._cost_label.text() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_token_displayed(self, qapp, monkeypatch, tmp_path):
        """token>0 → 只显示 token。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("idle", iterations=0, total_tokens=1500)
            text = panel._cost_label.text()
            assert "token" in text
            assert "1.5K" in text
            assert "$" not in text  # B2：不再显示 cost
        finally:
            _cleanup_panel(panel, qapp)

    def test_token_normal_displayed(self, qapp, monkeypatch, tmp_path):
        """token>0 → 显示「token {value}{单位}」。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("completed", iterations=3, total_tokens=1200)
            text = panel._cost_label.text()
            assert "token" in text
            assert "1.2K" in text
            assert "$" not in text  # B2：不再显示 cost
            assert "·" not in text  # B2：无分隔符（只有一个值）
        finally:
            _cleanup_panel(panel, qapp)

    def test_token_zero_no_display(self, qapp, monkeypatch, tmp_path):
        """token=0 → 不显示。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("completed", iterations=1, total_tokens=0)
            text = panel._cost_label.text()
            assert text == ""  # B2：token=0 不显示
        finally:
            _cleanup_panel(panel, qapp)

    def test_iterations_in_tooltip(self, qapp, monkeypatch, tmp_path):
        """iterations 信息应合并到 status_label tooltip。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("completed", iterations=5, total_tokens=100)
            assert panel._status_label.toolTip() == "5 轮"
        finally:
            _cleanup_panel(panel, qapp)

    def test_status_text_and_color(self, qapp, monkeypatch, tmp_path):
        """status_label 显示 _SESSION_STATUS_TEXT 映射。"""
        from client.panels.chat import _SESSION_STATUS_TEXT

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_status("streaming", iterations=0, total_tokens=0)
            assert panel._status_label.text() == _SESSION_STATUS_TEXT["streaming"]
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 6: 打字机按钮文字循环
# ============================================================================


class TestTypewriterButtonPrefix:
    """打字机按钮文字应始终含「打字机效果：」前缀（D47）。"""

    def test_initial_text_has_prefix(self, qapp, monkeypatch, tmp_path):
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            assert panel._typewriter_btn.text().startswith("打字机效果：")
        finally:
            _cleanup_panel(panel, qapp)

    def test_toggle_cycles_text_with_prefix(self, qapp, monkeypatch, tmp_path):
        """close→fast→normal→close 循环时文字始终有前缀。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            for _ in range(3):
                panel._on_toggle_typewriter()
                text = panel._typewriter_btn.text()
                assert text.startswith("打字机效果："), f"toggle 后文字缺前缀: {text!r}"
                # 后缀是三档之一
                suffix = text.replace("打字机效果：", "")
                assert suffix in ("关闭", "快速", "正常"), f"未知档位: {suffix!r}"
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 7: title_label + _update_title
# ============================================================================


class TestTitleLabelUpdate:
    """title_label 截断 + 更新。"""

    def test_update_title_short(self, qapp, monkeypatch, tmp_path):
        """短标题原样显示。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_title("测试会话")
            assert panel._title_label.text() == "测试会话"
            assert panel._title_label.toolTip() == "测试会话"
        finally:
            _cleanup_panel(panel, qapp)

    def test_update_title_long_truncated(self, qapp, monkeypatch, tmp_path):
        """超 32 字符截断 + 省略号，tooltip 保留全文。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            long_title = "a" * 50
            panel._update_title(long_title)
            text = panel._title_label.text()
            assert text.endswith("…")
            assert len(text) == 33  # 32 + 省略号
            assert panel._title_label.toolTip() == long_title
        finally:
            _cleanup_panel(panel, qapp)

    def test_update_title_empty(self, qapp, monkeypatch, tmp_path):
        """空标题清空 label。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_title("")
            assert panel._title_label.text() == ""
        finally:
            _cleanup_panel(panel, qapp)

    def test_update_title_none(self, qapp, monkeypatch, tmp_path):
        """None 当作空字符串。"""
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._update_title(None)  # type: ignore[arg-type]
            assert panel._title_label.text() == ""
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 8: _refresh_last_updated
# ============================================================================


class TestRefreshLastUpdated:
    """_refresh_last_updated 时间戳刷新。"""

    def test_no_active_session_shows_current_time(self, qapp, monkeypatch, tmp_path):
        """无活跃会话 → 显示当前系统时间 HH:MM。"""
        import time

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._current_session_id = None
            panel._refresh_last_updated()
            text = panel._last_updated_label.text()
            # 验证格式 HH:MM（5 字符）
            assert len(text) == 5
            assert text[2] == ":"
            # 与当前系统时间一致（允许 1 分钟偏差）
            now = time.strftime("%H:%M", time.localtime())
            assert text == now or text == now  # 同步执行应一致
        finally:
            _cleanup_panel(panel, qapp)

    def test_with_active_session_shows_session_time(self, qapp, monkeypatch, tmp_path):
        """有活跃会话 → 显示会话 updated_at 时间。"""
        import asyncio

        from client.core.agent import EventStore

        db_path = str(tmp_path / "top_bar_test.db")
        store = EventStore(db_path=db_path)
        store.init()

        async def _setup():
            sess = await store.create_session(session_id="sess-test-1", title="测试")
            # 更新 updated_at 到特定时间
            store.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (1700000000.0, "sess-test-1"),  # 固定时间戳
            )
            store.conn.commit()
            return sess

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_setup())
        finally:
            loop.close()
        store.close()

        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._current_session_id = "sess-test-1"
            panel._refresh_last_updated()
            text = panel._last_updated_label.text()
            # 1700000000 对应 2023-11-14 22:13:20 UTC（本地 2023-11-15），
            # 2026-09-13 起跨年会话显示完整日期时间（YYYY-MM-DD HH:MM），
            # 修复"只显示 HH:MM 对隔天/历史会话有歧义"
            assert len(text) == 16
            assert text[4] == "-" and text[7] == "-" and text[13] == ":"
            assert text.startswith("2023-11-")
        finally:
            _cleanup_panel(panel, qapp)


# ============================================================================
# Part 9: RunOutcome.total_tokens 字段
# ============================================================================


class TestRunOutcomeTotalTokens:
    """RunOutcome 应有 total_tokens 字段（T10 D47）。"""

    def test_total_tokens_default_zero(self):
        from client.core.agent.types import RunOutcome
        outcome = RunOutcome(session_id="s1", status="completed")
        assert outcome.total_tokens == 0

    def test_total_tokens_settable(self):
        from client.core.agent.types import RunOutcome
        outcome = RunOutcome(
            session_id="s1", status="completed",
            iterations=2,
            total_tokens=1500,  # B2: 删 total_cost_usd，只记账 token
        )
        assert outcome.total_tokens == 1500
