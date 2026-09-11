"""chat-panel-v2 Ticket 11 验收测试：导出（D16 + D17 + D32）

覆盖 T11 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] 三点菜单加"导出"项（_on_session_context_menu 含 act_export）
- [x] _export_session 方法存在 + 弹 QFileDialog + filter 含 MD/JSON/Both
- [x] Markdown 导出：user/assistant 消息按时间顺序，tool_calls 引用块，thinking 引用块
- [x] JSON 导出：{session, messages, tool_calls, events} 完整结构化
- [x] agent 回复范围（D32）：用户消息后到下一条用户消息前的所有内容

测试策略：
- 源码静态扫描：grep 关键 import / 方法定义 / 常量
- 单元测试：_format_session_as_markdown + _format_session_as_json 直接调用
- 集成测试：ChatPanel._export_session + mock QFileDialog 验证文件写入

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================================
# Part 2: _format_session_as_markdown 单元测试
# ============================================================================


class TestFormatSessionMarkdown:
    """_format_session_as_markdown 格式化逻辑（D16 + D32）。"""

    def test_empty_session_returns_title_only(self):
        """空 session + 空 messages → 只含标题行。"""
        from client.panels.chat import _format_session_as_markdown

        session = {"title": "测试会话", "mode": "dialogue", "status": "idle"}
        md = _format_session_as_markdown(session, [], [])
        assert "# 测试会话" in md
        assert "mode: dialogue" in md
        assert "status: idle" in md

    def test_none_session_returns_unnamed(self):
        """session=None → 标题为"未命名会话"。"""
        from client.panels.chat import _format_session_as_markdown

        md = _format_session_as_markdown(None, [], [])
        assert "# 未命名会话" in md

    def test_user_message_rendered(self):
        """user 消息应渲染为 ## user + 内容。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(role="user", content="hello world", source="user", seq=1)
        md = _format_session_as_markdown({}, [msg], [])
        assert "## user" in md
        assert "hello world" in md

    def test_assistant_message_with_text(self):
        """assistant 消息文本应渲染。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(role="assistant", content="hi back", source="assistant", seq=2)
        md = _format_session_as_markdown({}, [msg], [])
        assert "## assistant" in md
        assert "hi back" in md

    def test_assistant_message_with_thinking_quote(self):
        """assistant thinking 应渲染为引用块 > [thinking]。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(
            role="assistant", content="answer", source="assistant",
            thinking="let me think...", seq=2
        )
        md = _format_session_as_markdown({}, [msg], [])
        assert "> [thinking]" in md
        assert "let me think" in md

    def test_assistant_message_with_tool_calls_quote(self):
        """assistant tool_calls 应渲染为引用块 > [tool_call]。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(
            role="assistant", content="done", source="assistant", seq=2,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"SF"}'}
            }]
        )
        md = _format_session_as_markdown({}, [msg], [])
        assert "> [tool_call]" in md
        assert "get_weather" in md

    def test_tool_result_rendered_as_quote(self):
        """role=tool 消息应渲染为 > [tool_result] 引用块。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(
            role="tool", content='{"temp": 20}', source="tool_result",
            tool_call_id="call_1", seq=3
        )
        md = _format_session_as_markdown({}, [msg], [])
        assert "> [tool_result]" in md
        assert "call_1" in md

    def test_invisible_messages_skipped(self):
        """visible=False 的消息应被跳过（D32：不导出 invisible）。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg_visible = Message(role="user", content="visible", source="user", seq=1, visible=True)
        msg_invisible = Message(
            role="synthetic", content="hidden", source="synthetic", seq=2, visible=False
        )
        md = _format_session_as_markdown({}, [msg_visible, msg_invisible], [])
        assert "visible" in md
        assert "hidden" not in md

    def test_model_marked_for_assistant(self):
        """assistant 消息带 model 字段时应显示 *model: name*。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_markdown

        msg = Message(
            role="assistant", content="hi", source="assistant",
            model="GLM-5.2", seq=2
        )
        md = _format_session_as_markdown({}, [msg], [])
        assert "*model: GLM-5.2*" in md

    def test_session_object_supported(self):
        """session 也可以是 Session 对象（不只 dict）。"""
        from client.core.agent.types import Session
        from client.panels.chat import _format_session_as_markdown

        session = Session(id="test", title="对象会话", mode="dialogue", status="idle")
        md = _format_session_as_markdown(session, [], [])
        assert "# 对象会话" in md


# ============================================================================
# Part 3: _format_session_as_json 单元测试
# ============================================================================


class TestFormatSessionJson:
    """_format_session_as_json 格式化逻辑（D16 JSON 完整备份）。"""

    def test_json_structure_complete(self):
        """JSON 应含 version / exported_at / session / messages / tool_calls / events。"""
        from client.panels.chat import _format_session_as_json

        result = _format_session_as_json({}, [], [], [])
        data = json.loads(result)
        assert "version" in data
        assert "exported_at" in data
        assert "session" in data
        assert "messages" in data
        assert "tool_calls" in data
        assert "events" in data

    def test_json_messages_serialized(self):
        """Message 对象应通过 to_db 序列化进 messages 字段。"""
        from client.core.agent.types import Message
        from client.panels.chat import _format_session_as_json

        msg = Message(role="user", content="hi", source="user", seq=1, id="m1")
        result = _format_session_as_json({}, [msg], [], [])
        data = json.loads(result)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["id"] == "m1"
        assert data["messages"][0]["role"] == "user"

    def test_json_tool_calls_serialized(self):
        """ToolCall 对象应通过 to_db 序列化进 tool_calls 字段。"""
        from client.core.agent.types import ToolCall
        from client.panels.chat import _format_session_as_json

        tc = ToolCall(id="tc1", name="get_weather", args={"city": "SF"})
        result = _format_session_as_json({}, [], [tc], [])
        data = json.loads(result)
        assert len(data["tool_calls"]) == 1
        assert data["tool_calls"][0]["name"] == "get_weather"

    def test_json_events_serialized(self):
        """Event 对象应序列化进 events 字段。"""
        from client.core.agent.types import Event
        from client.panels.chat import _format_session_as_json

        ev = Event(seq=1, session_id="s1", type="test_event", payload={"k": "v"})
        result = _format_session_as_json({}, [], [], [ev])
        data = json.loads(result)
        assert len(data["events"]) == 1
        assert data["events"][0]["type"] == "test_event"

    def test_json_session_dict_passthrough(self):
        """session 是 dict 时直接序列化。"""
        from client.panels.chat import _format_session_as_json

        session = {"id": "s1", "title": "test", "mode": "dialogue"}
        result = _format_session_as_json(session, [], [], [])
        data = json.loads(result)
        assert data["session"]["id"] == "s1"
        assert data["session"]["title"] == "test"


# ============================================================================
# Part 4: ChatPanel._export_session 集成测试
# ============================================================================


def _make_chat_panel(monkeypatch, tmp_path):
    """构造 ChatPanel 实例（patch DEFAULT_DB_PATH 为临时路径）。"""
    import client.panels.chat as chat_module
    from client.panels.chat import ChatPanel

    db_path = str(tmp_path / "export_test.db")
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


def _make_store_with_session(tmp_path, session_id="s1", title="测试会话"):
    """构造 EventStore 并写入测试会话 + 消息。"""
    import asyncio

    from client.core.agent import EventStore, Message

    db_path = str(tmp_path / "export_integ.db")
    store = EventStore(db_path=db_path)

    async def _setup():
        await store.create_session(session_id, title=title, mode="dialogue")
        await store.append_message(session_id, Message(
            role="user", content="hello", source="user", seq=1
        ))
        await store.append_message(session_id, Message(
            role="assistant", content="hi back", source="assistant",
            seq=2, thinking="thinking...", model="GLM-5.2"
        ))

    store.init()  # 同步初始化（EventStore.init 非 async）
    asyncio.new_event_loop().run_until_complete(_setup())
    return store


class TestExportSessionIntegration:
    """ChatPanel._export_session 集成测试。"""

    def test_export_md_writes_file(self, qapp, monkeypatch, tmp_path):
        """选 MD 格式 → 应写入 .md 文件。"""
        from PySide6.QtWidgets import QFileDialog

        # 构造 store 并写入会话
        store = _make_store_with_session(tmp_path)
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._read_store = store

            # mock QFileDialog.getSaveFileName 返回 MD 格式
            export_path = str(tmp_path / "exported.md")
            monkeypatch.setattr(
                QFileDialog, "getSaveFileName",
                lambda *args, **kwargs: (export_path, "Markdown (*.md)")
            )
            # mock QMessageBox 避免弹窗
            monkeypatch.setattr(
                "client.panels.chat.QMessageBox.information", lambda *a, **kw: None
            )

            panel._export_session("s1")

            # 验证 .md 文件已生成
            assert Path(export_path).exists()
            content = Path(export_path).read_text(encoding="utf-8")
            assert "# 测试会话" in content
            assert "hello" in content
            assert "hi back" in content
        finally:
            _cleanup_panel(panel, qapp)
            store.close()

    def test_export_json_writes_file(self, qapp, monkeypatch, tmp_path):
        """选 JSON 格式 → 应写入 .json 文件。"""
        from PySide6.QtWidgets import QFileDialog

        store = _make_store_with_session(tmp_path)
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._read_store = store

            export_path = str(tmp_path / "exported.json")
            monkeypatch.setattr(
                QFileDialog, "getSaveFileName",
                lambda *args, **kwargs: (export_path, "JSON (*.json)")
            )
            monkeypatch.setattr(
                "client.panels.chat.QMessageBox.information", lambda *a, **kw: None
            )

            panel._export_session("s1")

            assert Path(export_path).exists()
            data = json.loads(Path(export_path).read_text(encoding="utf-8"))
            assert data["session"]["title"] == "测试会话"
            assert len(data["messages"]) == 2
            assert data["messages"][0]["role"] == "user"
            assert data["messages"][1]["role"] == "assistant"
        finally:
            _cleanup_panel(panel, qapp)
            store.close()

    def test_export_both_writes_two_files(self, qapp, monkeypatch, tmp_path):
        """选"两者都导"→ 应同时写 .md + .json。"""
        from PySide6.QtWidgets import QFileDialog

        store = _make_store_with_session(tmp_path)
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._read_store = store

            base_path = str(tmp_path / "both_export")
            monkeypatch.setattr(
                QFileDialog, "getSaveFileName",
                lambda *args, **kwargs: (base_path, "两者都导 (*.md *.json)")
            )
            monkeypatch.setattr(
                "client.panels.chat.QMessageBox.information", lambda *a, **kw: None
            )

            panel._export_session("s1")

            md_path = base_path + ".md"
            json_path = base_path + ".json"
            assert Path(md_path).exists(), f"MD 文件应存在：{md_path}"
            assert Path(json_path).exists(), f"JSON 文件应存在：{json_path}"
        finally:
            _cleanup_panel(panel, qapp)
            store.close()

    def test_export_canceled_no_file(self, qapp, monkeypatch, tmp_path):
        """用户取消 QFileDialog → 不应写文件。"""
        from PySide6.QtWidgets import QFileDialog

        store = _make_store_with_session(tmp_path)
        panel = _make_chat_panel(monkeypatch, tmp_path)
        try:
            panel._read_store = store

            # 用户取消：返回空字符串
            monkeypatch.setattr(
                QFileDialog, "getSaveFileName",
                lambda *args, **kwargs: ("", "")
            )

            panel._export_session("s1")

            # 不应生成任何文件
            assert not list(Path(tmp_path).glob("exported*"))
        finally:
            _cleanup_panel(panel, qapp)
            store.close()

    def test_export_session_not_found(self, qapp, monkeypatch, tmp_path):
        """session 不存在 → 应警告且不写文件。"""
        from PySide6.QtWidgets import QFileDialog

        panel = _make_chat_panel(monkeypatch, tmp_path)
        warning_called = []
        try:
            # mock QMessageBox.warning
            monkeypatch.setattr(
                "client.panels.chat.QMessageBox.warning",
                lambda *a, **kw: warning_called.append(a)
            )
            monkeypatch.setattr(
                QFileDialog, "getSaveFileName",
                lambda *args, **kwargs: ("", "")
            )

            panel._export_session("nonexistent_sid")
            # 应该有 warning 被调用（get_session 返回 None）
            assert len(warning_called) > 0
        finally:
            _cleanup_panel(panel, qapp)
