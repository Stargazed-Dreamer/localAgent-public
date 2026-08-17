"""Ticket 06：UI 动效常量 + 会话标题 + 模型选择器前端

覆盖 spec D5（淡入+工具卡展开过渡常量）/ D7（会话标题前 20 字）/ D10（模型选择器）：
- chat.py 定义 _FADE_IN_DURATION_MS / _TOOL_CARD_EXPAND_DURATION_MS 常量
- chat.py 定义 _apply_fade_in 方法和 _SESSION_TITLE_MAX_LEN 常量
- _ChatWorker 接受 model_override 参数 + 透传给 facade.start
- EventStore.update_session_title 方法存在 + 写入 DB 后 title 非空
- SessionFacade.start 接受 model_override 参数 + 新建会话首句更新 title
- ChatPanel 顶栏有 QComboBox 模型选择器（objectName=modelSelector）
- 模型选择器切换不打断当前 run（_on_model_changed 只更新 self._current_model）

注：原 _MessageBubble 淡入 / _ToolCallCard 展开动画运行时测试已随死代码删除
（_MessageBubble / _ToolCallCard 已移除）。新块类 _TimelineBlock 仍保留
_apply_fade_in 方法与 _FADE_IN_DURATION_MS 常量，新块类的动画运行时覆盖
见 tests/test_chat_panel_v2_timeline.py。

测试策略：
- 源码静态扫描：grep 关键 import / 方法定义 / 常量
- EventStore 单元测试：临时 DB 写入 title 后查询验证
- SessionFacade 单元测试：mock deps 验证 model_override 透传 + title 更新调用
- ChatPanel 实例化：QComboBox 模型选择器存在 + 切换不打断 run

无 pytest-qt 依赖，用 QApplication.instance() or QApplication([]) 模式（参考 test_ui_theme.py）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHAT_PY = PROJECT_ROOT / "client" / "panels" / "chat.py"
FACADE_PY = PROJECT_ROOT / "client" / "core" / "agent" / "facade.py"
EVENT_STORE_PY = PROJECT_ROOT / "client" / "core" / "agent" / "event_store.py"


# ============================================================================
# Part 1: 源码静态扫描 — imports / 方法定义存在
# ============================================================================


class TestSourceImportsAndDefinitions:
    """chat.py / facade.py / event_store.py 源码层验证关键 import 和方法定义。"""

    def test_chat_py_imports_qproperty_animation(self):
        """chat.py 应 import QPropertyAnimation（spec D5 动效必需）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "QPropertyAnimation" in source, (
            "chat.py 应 import QPropertyAnimation（spec D5 淡入+展开过渡）"
        )

    def test_chat_py_imports_qgraphics_opacity_effect(self):
        """chat.py 应 import QGraphicsOpacityEffect（spec D5 淡入必需）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "QGraphicsOpacityEffect" in source, (
            "chat.py 应 import QGraphicsOpacityEffect（spec D5 淡入必需）"
        )

    def test_chat_py_imports_qcombobox(self):
        """chat.py 应 import QComboBox（spec D10 模型选择器必需）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "QComboBox" in source, "chat.py 应 import QComboBox（spec D10 模型选择器）"

    def test_chat_py_has_apply_fade_in_method(self):
        """chat.py 应有 _apply_fade_in 方法定义（chat-panel-v2 后属 _TimelineBlock 基类）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _apply_fade_in" in source, (
            "chat.py 应有 _apply_fade_in 方法定义（_TimelineBlock._apply_fade_in，spec D5 淡入）"
        )

    def test_chat_py_has_load_models_method(self):
        """ChatPanel 应有 _load_models 方法定义。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _load_models" in source, "ChatPanel 应有 _load_models 方法（spec D10）"

    def test_chat_py_has_on_model_changed_method(self):
        """ChatPanel 应有 _on_model_changed 方法定义。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "def _on_model_changed" in source, (
            "ChatPanel 应有 _on_model_changed 方法（spec D10 切换模型回调）"
        )

    def test_chat_py_has_fade_in_duration_constant(self):
        """chat.py 应有淡入动效时长常量（spec D5 150ms）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_FADE_IN_DURATION_MS" in source, (
            "chat.py 应定义 _FADE_IN_DURATION_MS 常量（spec D5 淡入 150ms）"
        )

    def test_chat_py_has_tool_card_expand_duration_constant(self):
        """chat.py 应有工具卡展开时长常量（spec D5 200ms）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_TOOL_CARD_EXPAND_DURATION_MS" in source, (
            "chat.py 应定义 _TOOL_CARD_EXPAND_DURATION_MS 常量（spec D5 工具卡展开 200ms）"
        )

    def test_chat_py_has_session_title_max_len_constant(self):
        """chat.py 应有会话标题长度常量（spec D7 前 20 字）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "_SESSION_TITLE_MAX_LEN" in source, (
            "chat.py 应定义 _SESSION_TITLE_MAX_LEN 常量（spec D7 前 20 字）"
        )

    def test_chat_py_worker_accepts_model_override(self):
        """_ChatWorker.__init__ 应接受 model_override 参数（spec D10）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        # __init__ 签名应含 model_override: str = ""
        assert re.search(
            r"def __init__\([^)]*model_override\s*:\s*str\s*=\s*\"\"",
            source,
            re.DOTALL,
        ), "_ChatWorker.__init__ 应接受 model_override: str = \"\" 参数（spec D10）"

    def test_chat_py_worker_passes_model_override_to_facade(self):
        """_ChatWorker 应把 model_override 透传给 facade.start（spec D10）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "model_override=self._model_override" in source, (
            "_ChatWorker 应把 model_override=self._model_override 传给 facade.start"
        )

    def test_chat_py_on_send_passes_model_override(self):
        """_on_send 应把 self._current_model 传给 _ChatWorker（spec D10）。"""
        source = CHAT_PY.read_text(encoding="utf-8")
        assert "model_override=self._current_model" in source, (
            "_on_send 应把 model_override=self._current_model 传给 _ChatWorker"
        )

    def test_facade_py_start_accepts_model_override(self):
        """SessionFacade.start 应接受 model_override 参数（spec D10）。"""
        source = FACADE_PY.read_text(encoding="utf-8")
        assert "model_override" in source, (
            "SessionFacade.start 应接受 model_override 参数（spec D10）"
        )

    def test_facade_py_start_updates_title(self):
        """SessionFacade.start 应调 update_session_title 更新会话标题（spec D7）。"""
        source = FACADE_PY.read_text(encoding="utf-8")
        assert "update_session_title" in source, (
            "SessionFacade.start 应调 store.update_session_title（spec D7）"
        )

    def test_event_store_has_update_session_title(self):
        """EventStore 应有 update_session_title 方法定义（spec D7）。"""
        source = EVENT_STORE_PY.read_text(encoding="utf-8")
        assert "async def update_session_title" in source, (
            "EventStore 应有 async def update_session_title 方法（spec D7）"
        )


# ============================================================================
# Part 2: 运行时实例化 — ChatPanel 模型选择器 / 动画方法
# ============================================================================


class TestChatPanelModelSelector:
    """ChatPanel 顶栏 QComboBox 模型选择器（spec D10）。"""

    def test_model_selector_qcombobox_exists(self, qapp):
        """ChatPanel 应有 QComboBox 模型选择器。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            combos = panel.findChildren(QComboBox)
            assert len(combos) >= 1, "ChatPanel 应至少有一个 QComboBox（模型选择器）"
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_model_selector_object_name(self, qapp):
        """模型选择器 objectName 应为 modelSelector（便于 findChild 定位）。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            selector = panel.findChild(QComboBox, "modelSelector")
            assert selector is not None, (
                "ChatPanel 应有 objectName=modelSelector 的 QComboBox"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_model_selector_has_default_item(self, qapp):
        """模型选择器初始化时应含"（默认）"项（让用户能回退到默认 tier）。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            selector = panel.findChild(QComboBox, "modelSelector")
            assert selector is not None
            assert selector.count() >= 1, "模型选择器应至少有 1 项（默认项）"
            # 第一项应是"（默认）"，data 为空字符串
            assert selector.itemText(0) == "（默认）", (
                f"模型选择器第一项应是 '（默认）'，实际 {selector.itemText(0)!r}"
            )
            assert selector.itemData(0) == "", (
                f"默认项 data 应为空字符串，实际 {selector.itemData(0)!r}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_current_model_initial_empty(self, qapp):
        """ChatPanel._current_model 初始为空（用 config 默认 tier）。"""
        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            assert panel._current_model == "", (
                "_current_model 初始应为空（用 config 默认 tier）"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()

    def test_on_model_changed_updates_current_model(self, qapp):
        """_on_model_changed 应更新 self._current_model（spec D10 切换模型回调）。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            # 模拟添加一个 model 项
            selector = panel.findChild(QComboBox, "modelSelector")
            assert selector is not None
            selector.blockSignals(True)
            try:
                selector.addItem("test-model  [tier 3]", "test-model-123")
            finally:
                selector.blockSignals(False)
            # 切换到 test-model-123
            selector.setCurrentIndex(selector.count() - 1)
            # _on_model_changed 应已被触发（currentIndexChanged 信号）
            assert panel._current_model == "test-model-123", (
                f"切换后 _current_model 应为 'test-model-123'，实际 {panel._current_model!r}"
            )
        finally:
            panel.deleteLater()
            qapp.processEvents()


# ============================================================================
# Part 3: EventStore.update_session_title 单元测试
# ============================================================================


class TestEventStoreUpdateSessionTitle:
    """EventStore.update_session_title（spec D7）。

    用临时 DB 文件验证：
    - 方法存在且可调用
    - 写入后 get_session 返回的 title 非空
    - 写入后 list_sessions 返回的会话含正确 title
    """

    @pytest.fixture
    def tmp_store(self, tmp_path):
        """临时 EventStore（每个测试独立 DB 文件）。"""

        from client.core.agent.event_store import EventStore

        db_path = str(tmp_path / "test_agent_t06.db")
        store = EventStore(db_path=db_path)
        store.init()
        yield store
        store.close()

    def test_update_session_title_writes_to_db(self, tmp_store):
        """update_session_title 写入后 get_session 返回正确 title。"""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            # 先创建会话
            asyncio.set_event_loop(loop)
            loop.run_until_complete(tmp_store.create_session(session_id="sess-test-1"))
            # 更新 title
            loop.run_until_complete(
                tmp_store.update_session_title("sess-test-1", "测试标题前20字")
            )
            # 查询验证
            sess = loop.run_until_complete(tmp_store.get_session("sess-test-1"))
            assert sess is not None, "get_session 应返回非 None"
            assert sess.title == "测试标题前20字", (
                f"get_session.title 应为 '测试标题前20字'，实际 {sess.title!r}"
            )
        finally:
            loop.close()

    def test_update_session_title_overwrites(self, tmp_store):
        """update_session_title 第二次调用应覆盖第一次的 title。"""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(tmp_store.create_session(session_id="sess-test-2"))
            loop.run_until_complete(
                tmp_store.update_session_title("sess-test-2", "第一个标题")
            )
            loop.run_until_complete(
                tmp_store.update_session_title("sess-test-2", "第二个标题")
            )
            sess = loop.run_until_complete(tmp_store.get_session("sess-test-2"))
            assert sess is not None
            assert sess.title == "第二个标题", (
                f"二次更新后 title 应为 '第二个标题'，实际 {sess.title!r}"
            )
        finally:
            loop.close()

    def test_update_session_title_appears_in_list_sessions(self, tmp_store):
        """update_session_title 后 list_sessions 返回的会话含正确 title。"""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(tmp_store.create_session(session_id="sess-test-3"))
            loop.run_until_complete(
                tmp_store.update_session_title("sess-test-3", "列表中的标题")
            )
            sessions = loop.run_until_complete(tmp_store.list_sessions())
            assert len(sessions) >= 1
            target = next((s for s in sessions if s.id == "sess-test-3"), None)
            assert target is not None, "list_sessions 应含 sess-test-3"
            assert target.title == "列表中的标题", (
                f"list_sessions 中 title 应为 '列表中的标题'，实际 {target.title!r}"
            )
        finally:
            loop.close()


# ============================================================================
# Part 4: SessionFacade.start model_override + title 更新 单元测试
# ============================================================================


class TestSessionFacadeStartModelOverrideAndTitle:
    """SessionFacade.start model_override 透传 + 自动更新 title（spec D7/D10）。"""

    @pytest.fixture
    def tmp_store(self, tmp_path):
        """临时 EventStore。"""
        from client.core.agent.event_store import EventStore

        db_path = str(tmp_path / "test_facade_t06.db")
        store = EventStore(db_path=db_path)
        store.init()
        yield store
        store.close()

    def test_start_accepts_model_override_param(self, tmp_store):
        """SessionFacade.start 应接受 model_override 关键字参数（不报错）。"""
        import inspect

        from client.core.agent.facade import SessionFacade

        sig = inspect.signature(SessionFacade.start)
        param_names = list(sig.parameters.keys())
        assert "model_override" in param_names, (
            f"SessionFacade.start 应有 model_override 参数，实际参数：{param_names}"
        )

    def test_start_passes_model_override_to_runner_config(self, tmp_store):
        """start(model_override=X) 应让最终 runner.config.model == X。"""
        import asyncio

        from client.core.agent.facade import SessionFacade
        from client.core.agent.types import RunnerConfig, RunnerDeps

        # Mock LLM gateway：use_stream=False 时 runner 走 call()（一轮结束）
        class _MockGateway:
            async def call(self, request):
                from client.core.agent.types import LLMResponse
                return LLMResponse(content="ok", stop_reason="end_turn")

        class _MockToolRegistry:
            def to_openai_tools(self):
                return []

        # use_stream=False：runner 走 call() 而非 stream()，避免 async generator 复杂性
        config = RunnerConfig(
            session_id="sess-facade-1", model="original-model", use_stream=False
        )
        deps = RunnerDeps(
            llm_gateway=_MockGateway(),
            event_store=tmp_store,
            tool_registry=_MockToolRegistry(),
        )
        facade = SessionFacade(deps=deps, config=config)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                facade.start("sess-facade-1", "hello", model_override="override-model")
            )
            # runner 应已创建，且 config.model 应为 override-model
            assert facade._runner is not None, "start 后 runner 应已创建"
            assert facade._runner.config.model == "override-model", (
                f"runner.config.model 应为 'override-model'，"
                f"实际 {facade._runner.config.model!r}"
            )
        finally:
            loop.close()

    def test_start_no_model_override_keeps_original(self, tmp_store):
        """start() 不传 model_override 时 runner.config.model 应保持原值。"""
        import asyncio

        from client.core.agent.facade import SessionFacade
        from client.core.agent.types import RunnerConfig, RunnerDeps

        class _MockGateway:
            async def call(self, request):
                from client.core.agent.types import LLMResponse
                return LLMResponse(content="ok", stop_reason="end_turn")

        class _MockToolRegistry:
            def to_openai_tools(self):
                return []

        config = RunnerConfig(
            session_id="sess-facade-2", model="original-model", use_stream=False
        )
        deps = RunnerDeps(
            llm_gateway=_MockGateway(),
            event_store=tmp_store,
            tool_registry=_MockToolRegistry(),
        )
        facade = SessionFacade(deps=deps, config=config)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(facade.start("sess-facade-2", "hello"))
            assert facade._runner is not None
            assert facade._runner.config.model == "original-model", (
                f"不传 model_override 时 config.model 应保持 'original-model'，"
                f"实际 {facade._runner.config.model!r}"
            )
        finally:
            loop.close()

    def test_start_updates_title_for_new_session(self, tmp_store):
        """start() 新建会话首句入库后 sessions.title 应为前 20 字（spec D7）。"""
        import asyncio

        from client.core.agent.facade import SessionFacade
        from client.core.agent.types import RunnerConfig, RunnerDeps

        class _MockGateway:
            async def call(self, request):
                from client.core.agent.types import LLMResponse
                return LLMResponse(content="ok", stop_reason="end_turn")

        class _MockToolRegistry:
            def to_openai_tools(self):
                return []

        config = RunnerConfig(session_id="sess-title-test", use_stream=False)
        deps = RunnerDeps(
            llm_gateway=_MockGateway(),
            event_store=tmp_store,
            tool_registry=_MockToolRegistry(),
        )
        facade = SessionFacade(deps=deps, config=config)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            user_text = "这是一个测试首句用于验证title前20字功能"
            loop.run_until_complete(facade.start("sess-title-test", user_text))
            # 查 DB 验证 title
            sess = loop.run_until_complete(tmp_store.get_session("sess-title-test"))
            assert sess is not None
            assert sess.title == user_text[:20], (
                f"new session title 应为前 20 字 {user_text[:20]!r}，"
                f"实际 {sess.title!r}"
            )
        finally:
            loop.close()

    def test_start_does_not_overwrite_title_on_existing_session(self, tmp_store):
        """start() 已存在会话（续轮）不应覆盖已有 title（spec D7 只新建会话触发）。"""
        import asyncio

        from client.core.agent.facade import SessionFacade
        from client.core.agent.types import RunnerConfig, RunnerDeps

        class _MockGateway:
            async def call(self, request):
                from client.core.agent.types import LLMResponse
                return LLMResponse(content="ok", stop_reason="end_turn")

        class _MockToolRegistry:
            def to_openai_tools(self):
                return []

        # 预先创建会话并写 title
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                tmp_store.create_session(session_id="sess-existing")
            )
            loop.run_until_complete(
                tmp_store.update_session_title("sess-existing", "已有标题")
            )

            config = RunnerConfig(session_id="sess-existing", use_stream=False)
            deps = RunnerDeps(
                llm_gateway=_MockGateway(),
                event_store=tmp_store,
                tool_registry=_MockToolRegistry(),
            )
            facade = SessionFacade(deps=deps, config=config)
            loop.run_until_complete(facade.start("sess-existing", "新消息不应覆盖标题"))

            sess = loop.run_until_complete(tmp_store.get_session("sess-existing"))
            assert sess is not None
            assert sess.title == "已有标题", (
                f"续轮时 title 不应被覆盖，应保持 '已有标题'，实际 {sess.title!r}"
            )
        finally:
            loop.close()

    def test_start_title_truncates_to_20_chars(self, tmp_store):
        """start() 首句超过 20 字时 title 应截断到 20 字（spec D7）。"""
        import asyncio

        from client.core.agent.facade import SessionFacade
        from client.core.agent.types import RunnerConfig, RunnerDeps

        class _MockGateway:
            async def call(self, request):
                from client.core.agent.types import LLMResponse
                return LLMResponse(content="ok", stop_reason="end_turn")

        class _MockToolRegistry:
            def to_openai_tools(self):
                return []

        config = RunnerConfig(session_id="sess-truncate-test", use_stream=False)
        deps = RunnerDeps(
            llm_gateway=_MockGateway(),
            event_store=tmp_store,
            tool_registry=_MockToolRegistry(),
        )
        facade = SessionFacade(deps=deps, config=config)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            long_text = "a" * 50  # 50 个字符，应截断到 20
            loop.run_until_complete(facade.start("sess-truncate-test", long_text))
            sess = loop.run_until_complete(tmp_store.get_session("sess-truncate-test"))
            assert sess is not None
            assert len(sess.title) == 20, (
                f"title 长度应为 20，实际 {len(sess.title)}（值 {sess.title!r}）"
            )
            assert sess.title == "a" * 20
        finally:
            loop.close()


# ============================================================================
# Part 5: 模型选择器切换不打断当前 run（spec D10）
# ============================================================================


class TestModelSelectorDoesNotInterruptRun:
    """模型选择器切换不打断当前 run（spec D10）。"""

    def test_on_model_changed_does_not_touch_worker(self, qapp):
        """_on_model_changed 只更新 _current_model，不调 worker.interrupt 或 worker.stop。"""
        from PySide6.QtWidgets import QComboBox

        from client.panels.chat import ChatPanel

        panel = ChatPanel()
        try:
            selector = panel.findChild(QComboBox, "modelSelector")
            assert selector is not None
            # 模拟 worker 运行中
            class _FakeWorker:
                def __init__(self):
                    self.interrupt_called = False
                    self.stopped = False
                def interrupt(self):
                    self.interrupt_called = True

            panel._worker = _FakeWorker()
            # 切换模型
            selector.blockSignals(True)
            try:
                selector.addItem("new-model", "new-model-id")
            finally:
                selector.blockSignals(False)
            selector.setCurrentIndex(selector.count() - 1)
            # worker.interrupt 不应被调
            assert not panel._worker.interrupt_called, (
                "_on_model_changed 不应调 worker.interrupt（spec D10 不打断当前 run）"
            )
            # _current_model 应更新
            assert panel._current_model == "new-model-id", (
                f"_current_model 应为 'new-model-id'，实际 {panel._current_model!r}"
            )
        finally:
            panel._worker = None
            panel.deleteLater()
            qapp.processEvents()
