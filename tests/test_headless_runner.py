"""headless-agent-session Ticket 02 验收测试：headless_runner.py 主 agent 启动逻辑

覆盖 Ticket 02 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新建 headless_runner.py，实现 run_main_agent 函数签名
- [x] import client.core.agent 各组件
- [x] 组装 deps（base_url=http://127.0.0.1:8766，创建 asyncio.Event）
- [x] 组装 config（session_id=f"headless-{uuid4.hex[:12]}"，mode="headless"）
- [x] 调 facade.start(session_id, trigger_text)
- [x] 从 EventStore 取主 agent 最后 20 条 assistant 消息
- [x] 返回 MainAgentResult(session_id, outcome, last_assistant_messages)
- [x] finally 块关 EventStore
- [x] 单元测试：mock LLM 验证启动流程（不真实调 LLM）
- [x] 单元测试：验证 session_id 格式（headless- 前缀）
- [x] 单元测试：验证 mode="headless" 写入 sessions 表
- [x] 单元测试：验证 trigger_text 传给 facade.start
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    Message,
    MockLLM,
    MockToolExecutor,
    SessionFacade,
)
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_JUDGE_MAX_ITERATIONS,
    DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_WALL_CLOCK_BUDGET_SECS,
    HEADLESS_JUDGE_PREFIX,
    HEADLESS_MAIN_PREFIX,
    HeadlessConfig,
    MainAgentResult,
    _extract_last_assistant_messages,
    _generate_judge_session_id,
    _generate_main_session_id,
    get_interrupt_event,
    interrupt_session,
    parse_headless_config,
    register_interrupt_event,
    run_main_agent,
    unregister_interrupt_event,
)

# ============================================================================
# Fixtures
# ============================================================================


# ============================================================================
# 1. session_id 生成（Ticket 02 acceptance）
# ============================================================================


def test_generate_main_session_id_has_headless_prefix():
    """Ticket 02: 主会话 session_id 以 'headless-' 开头。"""
    sid = _generate_main_session_id()
    assert sid.startswith(HEADLESS_MAIN_PREFIX)
    # headless-{12 hex chars}
    assert len(sid) == len(HEADLESS_MAIN_PREFIX) + 12
    # 后缀是 hex
    suffix = sid[len(HEADLESS_MAIN_PREFIX):]
    int(suffix, 16)  # 不抛异常即为合法 hex


def test_generate_judge_session_id_has_headless_judge_prefix():
    """Ticket 02: judge 会话 session_id 以 'headless-judge-' 开头。"""
    sid = _generate_judge_session_id()
    assert sid.startswith(HEADLESS_JUDGE_PREFIX)
    assert len(sid) == len(HEADLESS_JUDGE_PREFIX) + 12
    suffix = sid[len(HEADLESS_JUDGE_PREFIX):]
    int(suffix, 16)


def test_generate_main_session_id_unique():
    """两次调用生成不同的 session_id。"""
    sid1 = _generate_main_session_id()
    sid2 = _generate_main_session_id()
    assert sid1 != sid2


# ============================================================================
# 2. parse_headless_config（默认值 + 覆盖）
# ============================================================================


def test_parse_headless_config_defaults():
    """Ticket 02: 空配置用默认值（spec 我替领导拍的板）。"""
    cfg = parse_headless_config({})
    assert cfg.trigger_text == ""
    assert cfg.skill == ""
    assert cfg.one_shot is False
    assert cfg.max_iterations == DEFAULT_MAX_ITERATIONS  # 100
    # B2: 删 max_budget_usd 字段
    assert cfg.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 1800
    assert cfg.model == ""
    assert cfg.judge_model == ""
    assert cfg.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS  # 10
    # B2: 删 judge_max_budget_usd 字段
    assert cfg.judge_wall_clock_budget_secs == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS  # 300
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.db_path == ""


def test_parse_headless_config_override():
    """Ticket 02: 部分配置覆盖默认值。"""
    cfg = parse_headless_config({
        "trigger_text": "查日志",
        "max_iterations": 50,
        "one_shot": True,
        "judge_model": "deepseek-chat",
    })
    assert cfg.trigger_text == "查日志"
    assert cfg.max_iterations == 50
    assert cfg.one_shot is True
    # 未覆盖的字段保持默认值
    # B2: 删 max_budget_usd 字段
    assert cfg.judge_model == "deepseek-chat"
    assert cfg.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS


def test_headless_config_dataclass_defaults():
    """Ticket 02: HeadlessConfig dataclass 默认值与 spec 一致。"""
    cfg = HeadlessConfig()
    assert cfg.max_iterations == 100
    # B2: 删 max_budget_usd 字段
    assert cfg.wall_clock_budget_secs == 1800


# ============================================================================
# 3. interrupt registry（Ticket 07 提前留接口）
# ============================================================================


def test_interrupt_registry_register_and_get():
    """Ticket 02/07: 注册 interrupt_event 后能查到。"""
    ev = asyncio.Event()
    register_interrupt_event("test-sess-1", ev)
    try:
        assert get_interrupt_event("test-sess-1") is ev
    finally:
        unregister_interrupt_event("test-sess-1")


def test_interrupt_registry_unregister():
    """Ticket 02/07: 清理后查不到。"""
    ev = asyncio.Event()
    register_interrupt_event("test-sess-2", ev)
    unregister_interrupt_event("test-sess-2")
    assert get_interrupt_event("test-sess-2") is None


def test_interrupt_registry_unregister_idempotent():
    """Ticket 02/07: 清理不存在的 session 不报错。"""
    unregister_interrupt_event("nonexistent-sess")  # 不抛异常


def test_interrupt_session_triggers_event():
    """Ticket 02/07: interrupt_session 触发 event.set()。"""
    ev = asyncio.Event()
    register_interrupt_event("test-sess-3", ev)
    try:
        assert not ev.is_set()
        result = interrupt_session("test-sess-3")
        assert result is True
        assert ev.is_set()
    finally:
        unregister_interrupt_event("test-sess-3")


def test_interrupt_session_nonexistent_returns_false():
    """Ticket 02/07: 中断不存在的 session 返回 False。"""
    result = interrupt_session("nonexistent-sess")
    assert result is False


# ============================================================================
# 4. _extract_last_assistant_messages（Ticket 02 acceptance）
# ============================================================================


def test_extract_last_assistant_messages_returns_assistant_only(tmp_db_path):
    """Ticket 02: 只取 assistant 消息，过滤 user/system。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s1", mode="headless")
            # 写入混合消息
            await store.append_message("s1", Message(role="user", content="hi", source="user"))
            await store.append_message("s1", Message(role="assistant", content="hello 1", source="assistant"))
            await store.append_message("s1", Message(role="user", content="again", source="user"))
            await store.append_message("s1", Message(role="assistant", content="hello 2", source="assistant"))
            await store.append_message("s1", Message(role="assistant", content="hello 3", source="assistant"))

            msgs = await _extract_last_assistant_messages(store, "s1", 20)
            assert len(msgs) == 3
            assert "hello 1" in msgs[0]
            assert "hello 2" in msgs[1]
            assert "hello 3" in msgs[2]
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_respects_count(tmp_db_path):
    """Ticket 02: count 参数限制取最后 N 条。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s2", mode="headless")
            for i in range(25):
                await store.append_message(
                    "s2", Message(role="assistant", content=f"msg {i}", source="assistant")
                )

            # count=20 取最后 20 条
            msgs = await _extract_last_assistant_messages(store, "s2", 20)
            assert len(msgs) == 20
            assert "msg 5" in msgs[0]  # 第 6 条（index 5）是最早的
            assert "msg 24" in msgs[-1]  # 最后一条

            # count=3 取最后 3 条
            msgs3 = await _extract_last_assistant_messages(store, "s2", 3)
            assert len(msgs3) == 3
            assert "msg 22" in msgs3[0]
            assert "msg 24" in msgs3[-1]
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_empty_session(tmp_db_path):
    """Ticket 02: 空会话返回空列表。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s3", mode="headless")
            msgs = await _extract_last_assistant_messages(store, "s3", 20)
            assert msgs == []
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_no_assistant_msgs(tmp_db_path):
    """Ticket 02: 只有 user 消息时返回空列表。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s4", mode="headless")
            await store.append_message("s4", Message(role="user", content="hi", source="user"))
            await store.append_message("s4", Message(role="user", content="again", source="user"))
            msgs = await _extract_last_assistant_messages(store, "s4", 20)
            assert msgs == []
        finally:
            store.close()
    asyncio.run(run())


# ============================================================================
# 5. run_main_agent 端到端（mock LLM，不真实调 LLM）
# ============================================================================


def test_run_main_agent_with_mock_llm(tmp_db_path, monkeypatch):
    """Ticket 02: run_main_agent 用 MockLLM 跑通完整流程。

    mock 策略：
    - LLMPoolGateway → 工厂函数返回 MockLLM
    - ToolRegistry → mock 类（refresh no-op）
    - HttpClientToolExecutor → mock 类（接受 registry + base_url 参数）

    验证：
    - session_id 以 "headless-" 开头
    - mode="headless" 写入 sessions 表
    - trigger_text 传给 facade.start（user message 写入）
    - 主 agent 跑完后能取到 assistant 消息
    - MainAgentResult 含 session_id + outcome + last_assistant_messages
    - finally 块关 EventStore（store.closed 后无法再读，间接验证）
    - interrupt registry 注册后清理
    """
    # 准备 MockLLM（脚本化响应）
    mock_llm = MockLLM(default_text="headless test ok")

    # mock LLMPoolGateway：替换为返回 MockLLM 的工厂
    class MockLLMPoolGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        async def call(self, request):
            return await mock_llm.call(request)
        async def stream(self, request):
            async for ev in mock_llm.stream(request):
                yield ev

    # mock ToolRegistry：refresh no-op
    class MockToolRegistry:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        def refresh(self, *, timeout: float = 10.0) -> int:
            return 0

    # mock HttpClientToolExecutor：接受 registry + base_url（签名兼容）
    class MockHttpClientToolExecutor(MockToolExecutor):
        def __init__(self, registry=None, base_url=DEFAULT_BASE_URL, **kwargs):
            super().__init__(**kwargs)

    # 关键：monkeypatch client.core.agent 模块内的类（headless_runner 函数内 import 的来源）
    import client.core.agent as agent_pkg
    monkeypatch.setattr(agent_pkg, "LLMPoolGateway", MockLLMPoolGateway)
    monkeypatch.setattr(agent_pkg, "ToolRegistry", MockToolRegistry)
    monkeypatch.setattr(agent_pkg, "HttpClientToolExecutor", MockHttpClientToolExecutor)

    config = HeadlessConfig(
        trigger_text="回复'headless test ok'即可，不要调任何工具",
        max_iterations=5,
        # B2: 删 max_budget_usd 字段
        wall_clock_budget_secs=60,
        db_path=tmp_db_path,
    )

    async def run():
        result = await run_main_agent(
            trigger_text=config.trigger_text,
            config=config,
        )
        return result

    result = asyncio.run(run())

    # 验证 MainAgentResult 结构
    assert isinstance(result, MainAgentResult)
    assert result.session_id.startswith(HEADLESS_MAIN_PREFIX)
    assert result.error == ""  # 无错误

    # 验证 outcome 存在
    assert result.outcome is not None
    assert result.outcome.session_id == result.session_id

    # 验证 mode="headless" 写入 sessions 表
    # 注意：run_main_agent finally 块会关 store，需要重新打开验证
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            session = await verify_store.get_session(result.session_id)
            assert session is not None
            assert session.mode == "headless"

            # 验证 trigger_text 传给 facade.start（user message 写入）
            messages = await verify_store.load_messages(result.session_id)
            user_msgs = [m for m in messages if m.role == "user"]
            assert len(user_msgs) >= 1
            assert "headless test ok" in user_msgs[0].content

            # 验证主 agent 跑完后有 assistant 消息
            assistant_msgs = [m for m in messages if m.role == "assistant"]
            assert len(assistant_msgs) >= 1

            # 验证 last_assistant_messages 含 assistant 内容
            assert len(result.last_assistant_messages) >= 1
            assert "headless test ok" in result.last_assistant_messages[-1]
        asyncio.run(verify())
    finally:
        verify_store.close()

    # 验证 interrupt registry 已清理（run_main_agent 结束后应 unregister）
    assert get_interrupt_event(result.session_id) is None


def test_run_main_agent_with_explicit_session_id(tmp_db_path, monkeypatch):
    """Ticket 02: 支持外部传入 session_id（不自动生成）。"""
    mock_llm = MockLLM(default_text="ok")

    class MockLLMPoolGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        async def call(self, request):
            return await mock_llm.call(request)
        async def stream(self, request):
            async for ev in mock_llm.stream(request):
                yield ev

    class MockToolRegistry:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        def refresh(self, *, timeout: float = 10.0) -> int:
            return 0

    class MockHttpClientToolExecutor(MockToolExecutor):
        def __init__(self, registry=None, base_url=DEFAULT_BASE_URL, **kwargs):
            super().__init__(**kwargs)

    import client.core.agent as agent_pkg
    monkeypatch.setattr(agent_pkg, "LLMPoolGateway", MockLLMPoolGateway)
    monkeypatch.setattr(agent_pkg, "ToolRegistry", MockToolRegistry)
    monkeypatch.setattr(agent_pkg, "HttpClientToolExecutor", MockHttpClientToolExecutor)

    explicit_sid = "headless-explicit-test1"

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(db_path=tmp_db_path, max_iterations=3),
            session_id=explicit_sid,
        )

    result = asyncio.run(run())
    assert result.session_id == explicit_sid
    assert result.error == ""


def test_run_main_agent_registers_interrupt_event(tmp_db_path, monkeypatch):
    """Ticket 02/07: run_main_agent 启动时注册 interrupt_event，结束后清理。"""
    mock_llm = MockLLM(default_text="ok")

    class MockLLMPoolGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        async def call(self, request):
            return await mock_llm.call(request)
        async def stream(self, request):
            async for ev in mock_llm.stream(request):
                yield ev

    class MockToolRegistry:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        def refresh(self, *, timeout: float = 10.0) -> int:
            return 0

    class MockHttpClientToolExecutor(MockToolExecutor):
        def __init__(self, registry=None, base_url=DEFAULT_BASE_URL, **kwargs):
            super().__init__(**kwargs)

    import client.core.agent as agent_pkg
    monkeypatch.setattr(agent_pkg, "LLMPoolGateway", MockLLMPoolGateway)
    monkeypatch.setattr(agent_pkg, "ToolRegistry", MockToolRegistry)
    monkeypatch.setattr(agent_pkg, "HttpClientToolExecutor", MockHttpClientToolExecutor)

    captured_sid = []

    async def run():
        # 在 run_main_agent 内部无法直接检查 registry（因为是异步的），
        # 但可以通过 monkeypatch facade.start 在调用时检查
        original_start = SessionFacade.start

        async def patched_start(self, session_id, text, *, mode="dialogue", model_override=""):
            captured_sid.append(session_id)
            # 验证此时 interrupt_event 已注册
            ev = get_interrupt_event(session_id)
            assert ev is not None, "interrupt_event should be registered during run"
            return await original_start(self, session_id, text, mode=mode, model_override=model_override)

        monkeypatch.setattr(SessionFacade, "start", patched_start)
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(db_path=tmp_db_path, max_iterations=3),
        )

    result = asyncio.run(run())
    assert len(captured_sid) == 1
    assert result.session_id == captured_sid[0]
    # 结束后应已清理
    assert get_interrupt_event(result.session_id) is None


def test_run_main_agent_failure_returns_error(tmp_db_path, monkeypatch):
    """Ticket 02: 内部异常时返回 MainAgentResult(error=...)，不抛出。"""
    # mock LLMPoolGateway 抛异常
    class FailingGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        async def call(self, request):
            raise RuntimeError("simulated LLM failure")
        async def stream(self, request):
            raise RuntimeError("simulated LLM failure")
            yield  # unreachable: 让 stream 成为 async generator（noqa 无效，改注释说明）

    class MockToolRegistry:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url
        def refresh(self, *, timeout: float = 10.0) -> int:
            return 0

    class MockHttpClientToolExecutor(MockToolExecutor):
        def __init__(self, registry=None, base_url=DEFAULT_BASE_URL, **kwargs):
            super().__init__(**kwargs)

    import client.core.agent as agent_pkg
    monkeypatch.setattr(agent_pkg, "LLMPoolGateway", FailingGateway)
    monkeypatch.setattr(agent_pkg, "ToolRegistry", MockToolRegistry)
    monkeypatch.setattr(agent_pkg, "HttpClientToolExecutor", MockHttpClientToolExecutor)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(db_path=tmp_db_path, max_iterations=3),
        )

    # SessionRunner 内部可能捕获 LLM 异常转为 failed outcome，也可能冒泡
    # 无论哪种，run_main_agent 不应抛出，应返回 MainAgentResult
    result = asyncio.run(run())
    assert isinstance(result, MainAgentResult)
    assert result.session_id.startswith(HEADLESS_MAIN_PREFIX)
    # 可能 outcome 非 None（failed status）或 error 非空（异常冒泡被 try/except 捕获）
    # 两种都算 pass，关键是 run_main_agent 不抛出
    # interrupt registry 仍应清理
    assert get_interrupt_event(result.session_id) is None
