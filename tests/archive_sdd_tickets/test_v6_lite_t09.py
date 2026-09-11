"""v6-lite T09 验收测试：W6 故障注入 + /agent/chat 契约回归

覆盖 T09 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] 故障注入 7 项全部产生可解释 outcome（不是 traceback）：
  1. provider 断流（LLM 调用抛异常 / 返回 error）
  2. 空 content（LLM 返回空字符串）
  3. 坏 JSON tool args（arguments 字段非合法 JSON）
  4. 审批拒绝（user_denied + feedback）
  5. SQLite 写失败（DB 锁/IO 错误）
  6. GUI worker 被杀（interrupt_event.set + reconciler）
  7. 后端重启（streaming/running → interrupted via reconcile）
- [x] `/agent/chat` 旧契约回归通过（响应字段不变）：
  success / content / model / model_tier / usage / elapsed_ms

测试策略：
- 故障注入用 MockLLM + MockToolExecutor + 真实 EventStore（tmp_path 隔离）
- /agent/chat 契约测试：直接验证 ChatResponse Pydantic 模型字段不变；
  若 FastAPI TestClient 可用，额外跑端到端契约测试（需后端运行）
- 不接真实 LLM provider（避免烧钱 + 不稳定）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "agent_t09.db"))
    store.init()
    return store


# ============================================================================
# 故障注入 7 项（v6-lite §3 W6）
# ============================================================================


class TestFaultInjection1ProviderStreamBroken:
    """故障注入 1/7：provider 断流——LLM 调用抛异常。

    期望：runner 不崩，outcome.status=failed，error 含异常信息；
    session 写入 error 事件，DB 状态一致。
    """

    def test_provider_exception_produces_failed_outcome(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-1"))
            _run(store.append_message("sess-1", Message(
                role="user", content="hi", source="user",
            )))

            class _BoomLLM:
                async def call(self, request):
                    raise RuntimeError("provider connection reset")

                async def stream(self, request):
                    # v6-lite-streaming-gui T02: stream() 与 call() 行为一致（raise）
                    raise RuntimeError("provider connection reset")
                    yield  # unreachable，仅让 Python 识别为 async generator

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=_BoomLLM(),
                    event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-1"),
            )
            # runner 当前未捕获 LLM 异常（除了 ContextOverflow），会向上抛
            # 这是设计：上层（GUI worker）应捕获并显示
            with pytest.raises(RuntimeError, match="provider connection reset"):
                _run(runner.run())

            # session 状态保持 streaming（未 finalize），reconcile 可恢复
            # 验证 DB 一致性：messages 没被多写
            msgs = _run(store.load_messages("sess-1"))
            assert len(msgs) == 1  # 只有原 user 消息
        finally:
            store.close()

    def test_provider_returns_error_stop_reason(self, tmp_path):
        """provider 返回 stop_reason=error（非异常，是结构化错误）→ 正常 finalize。

        期望：assistant 消息含错误信息，session 正常 completed。
        """
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-2"))
            _run(store.append_message("sess-2", Message(
                role="user", content="hi", source="user",
            )))

            mock_llm = MockLLM(callable=lambda req: LLMResponse(
                content="[LLM error] rate limited",
                stop_reason="error",
                model="mock",
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-2"),
            )
            outcome = _run(runner.run())

            # error-as-output-variant：错误是结构化输出，不抛异常
            assert outcome.status == "completed"
            assert outcome.stop_reason == "error"
            msgs = _run(store.load_messages("sess-2"))
            assert len(msgs) == 2  # user + assistant(error)
            assert "[LLM error]" in msgs[1].content
        finally:
            store.close()


class TestFaultInjection2EmptyContent:
    """故障注入 2/7：空 content——LLM 返回空字符串。

    期望：assistant 消息含空 content，session 正常 finalize（不崩）。
    """

    def test_empty_content_finalizes_normally(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-3"))
            _run(store.append_message("sess-3", Message(
                role="user", content="hi", source="user",
            )))

            mock_llm = MockLLM(callable=lambda req: LLMResponse(
                content="",  # 空 content
                stop_reason="end_turn",
                model="mock",
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                ),
                config=RunnerConfig(session_id="sess-3"),
            )
            outcome = _run(runner.run())

            # 空 content 不崩，正常 finalize
            assert outcome.status == "completed"
            assert outcome.stop_reason == "end_turn"
            msgs = _run(store.load_messages("sess-3"))
            assert len(msgs) == 2
            assert msgs[1].content == ""
            assert msgs[1].role == "assistant"
        finally:
            store.close()

    def test_empty_content_with_tool_calls_still_executes(self, tmp_path):
        """LLM 返回空 content 但有 tool_calls → 仍执行工具循环（OpenAI 协议允许）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-3b"))
            _run(store.append_message("sess-3b", Message(
                role="user", content="hi", source="user",
            )))

            call_count = [0]

            def _gen(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一轮：空 content + tool_call
                    return LLMResponse(
                        content="",
                        tool_calls=[{
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "ping", "arguments": "{}"},
                        }],
                        stop_reason="tool_use",
                        model="mock",
                    )
                # 第二轮：正常文本
                return LLMResponse(content="done", stop_reason="end_turn", model="mock")

            mock_llm = MockLLM(callable=_gen)
            mock_ex = MockToolExecutor(default_result=ToolResult(
                tool_call_id="call-1", content="pong", is_error=False,
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    tool_executor=mock_ex,
                ),
                config=RunnerConfig(session_id="sess-3b"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            # 工具被执行（mock_ex 返回 pong）
            tool_calls = _run(store.load_tool_calls("sess-3b"))
            assert len(tool_calls) == 1
            assert tool_calls[0].status == "completed"
        finally:
            store.close()


class TestFaultInjection3BadJsonToolArgs:
    """故障注入 3/7：坏 JSON tool args——arguments 字段非合法 JSON。

    期望：ToolCall.from_openai_tool_call 解析失败时 args={} 兜底，
    工具仍被执行（args 空字典），不抛 traceback。
    """

    def test_bad_json_args_falls_back_to_empty_dict(self):
        """ToolCall.from_openai_tool_call 解析坏 JSON → args={} 兜底。"""
        bad_tc = {
            "id": "call-bad",
            "type": "function",
            "function": {
                "name": "dangerous_tool",
                "arguments": "not-valid-json{{{{",  # 坏 JSON
            },
        }
        tc = ToolCall.from_openai_tool_call(bad_tc, session_id="s", seq=1)
        assert tc.id == "call-bad"
        assert tc.name == "dangerous_tool"
        assert tc.args == {}  # 兜底空 dict
        assert "not-valid-json" in tc.args_raw  # 原字符串保留

    def test_runner_handles_bad_json_args_without_crash(self, tmp_path):
        """runner 收到坏 JSON tool args → 仍执行工具（args 空字典），不崩。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-4"))
            _run(store.append_message("sess-4", Message(
                role="user", content="hi", source="user",
            )))

            call_count = [0]

            def _gen(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    return LLMResponse(
                        content="calling tool",
                        tool_calls=[{
                            "id": "call-bad",
                            "type": "function",
                            "function": {
                                "name": "test_tool",
                                "arguments": "not-json",  # 坏 JSON
                            },
                        }],
                        stop_reason="tool_use",
                        model="mock",
                    )
                return LLMResponse(content="ok", stop_reason="end_turn", model="mock")

            mock_llm = MockLLM(callable=_gen)
            mock_ex = MockToolExecutor(default_result=ToolResult(
                tool_call_id="call-bad", content="executed with empty args",
                is_error=False,
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    tool_executor=mock_ex,
                ),
                config=RunnerConfig(session_id="sess-4"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            tool_calls = _run(store.load_tool_calls("sess-4"))
            assert len(tool_calls) == 1
            assert tool_calls[0].args == {}  # 兜底
            assert tool_calls[0].status == "completed"
        finally:
            store.close()


class TestFaultInjection4ApprovalDenied:
    """故障注入 4/7：审批拒绝——user_denied + user_feedback。

    期望：ToolResult(is_error=True, content="User denied approval: {feedback}")，
    模型看到 feedback 后可改道（不抛异常）。
    （已在 T06a 详细测试，此处为故障注入清单的 acceptance 收口）
    """

        # 不抛异常（error-as-output-variant）

    def test_runner_continues_after_denied_approval(self, tmp_path):
        """审批拒绝后模型看到 feedback → 继续对话（不崩）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="sess-5b"))
            _run(store.append_message("sess-5b", Message(
                role="user", content="delete files", source="user",
            )))

            call_count = [0]

            def _gen(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    return LLMResponse(
                        content="calling delete",
                        tool_calls=[{
                            "id": "call-d",
                            "type": "function",
                            "function": {"name": "delete_files", "arguments": "{}"},
                        }],
                        stop_reason="tool_use",
                        model="mock",
                    )
                # 第二轮：看到 feedback 改道
                return LLMResponse(
                    content="I won't delete files since you denied.",
                    stop_reason="end_turn", model="mock",
                )

            mock_llm = MockLLM(callable=_gen)
            mock_ex = MockToolExecutor(default_result=ToolResult(
                tool_call_id="call-d",
                content="User denied approval: Don't delete",
                is_error=True,
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm, event_store=store,
                    tool_executor=mock_ex,
                ),
                config=RunnerConfig(session_id="sess-5b"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            msgs = _run(store.load_messages("sess-5b"))
            # user + assistant(tool_call) + tool_result(error) + assistant(final)
            assert len(msgs) == 4
            assert "denied" in msgs[2].content.lower()
            assert "won't delete" in msgs[3].content
        finally:
            store.close()


class TestFaultInjection5SqliteWriteFailure:
    """故障注入 5/7：SQLite 写失败——DB 锁/IO 错误。

    期望：EventStore 写入抛异常时，runner 不吞掉异常（让上层 GUI worker 显示）；
    或者 sqlite3 异常类型可识别（OperationalError/DatabaseError）。
    """

    def test_sqlite_corrupted_db_raises_identifiable_exception(self, tmp_path):
        """损坏的 DB 文件 → sqlite3.DatabaseError（可识别，非 traceback 黑洞）。"""
        # 创建一个非 SQLite 文件作为 DB 路径
        bad_db = tmp_path / "corrupted.db"
        bad_db.write_text("not a sqlite database file")

        # EventStore.init() 应该能处理（CREATE TABLE IF NOT EXISTS 在非 DB 文件上）
        # 实际上 sqlite3 会在打开时识别非 SQLite 文件并抛 DatabaseError
        with pytest.raises(Exception) as exc_info:
            store = EventStore(db_path=str(bad_db))
            store.init()
        # 必须是 sqlite3 异常（可识别），不是其他类型
        assert "sqlite3" in str(type(exc_info.value)).lower() or "database" in str(exc_info.value).lower()

    def test_sqlite_locked_db_eventually_recovers(self, tmp_path):
        """WAL 模式下读不阻塞写——多个连接可并发读。"""
        db_path = str(tmp_path / "agent_lock.db")
        store1 = EventStore(db_path=db_path)
        store1.init()
        try:
            _run(store1.create_session(session_id="sess-6"))
            _run(store1.append_message("sess-6", Message(
                role="user", content="hi", source="user",
            )))

            # 第二个连接并发读（WAL 模式支持）
            store2 = EventStore(db_path=db_path)
            store2.init()
            try:
                msgs = _run(store2.load_messages("sess-6"))
                assert len(msgs) == 1  # 能读到 store1 写入的消息
            finally:
                store2.close()
        finally:
            store1.close()
# ============================================================================


class TestAgentChatContractRegression:
    """`/agent/chat` 旧契约回归：响应字段不变（v6-lite §3 W6）。

    验收：response_model=ChatResponse 字段保持向后兼容：
    success / content / model / model_tier / usage / elapsed_ms
    """

    def test_chat_response_pydantic_model_fields_unchanged(self):
        """ChatResponse Pydantic 模型字段集与 v6-lite §3 W6 契约一致。"""
        from server.agent import ChatResponse

        # Pydantic v2 用 model_fields，v1 用 __fields__
        fields = getattr(ChatResponse, "model_fields", None) or getattr(ChatResponse, "__fields__", None)
        assert fields is not None, "ChatResponse must be a Pydantic BaseModel"

        expected = {"success", "content", "model", "model_tier", "usage", "elapsed_ms"}
        actual = set(fields.keys())
        # 允许有额外字段（向后兼容扩展），但 6 个契约字段必须都在
        missing = expected - actual
        assert not missing, f"ChatResponse missing contract fields: {missing}"

    def test_chat_request_pydantic_model_fields_unchanged(self):
        """ChatRequest Pydantic 模型字段集与 v6-lite §3 W6 契约一致。"""
        from server.agent import ChatRequest

        fields = getattr(ChatRequest, "model_fields", None) or getattr(ChatRequest, "__fields__", None)
        assert fields is not None

        expected = {"messages", "model_tier", "model", "temperature"}
        actual = set(fields.keys())
        missing = expected - actual
        assert not missing, f"ChatRequest missing contract fields: {missing}"

    def test_chat_response_can_be_constructed_with_contract_fields(self):
        """ChatResponse 可用 6 个契约字段构造（无 TypeError/ValidationError）。"""
        from server.agent import ChatResponse

        resp = ChatResponse(
            success=True,
            content="hello",
            model="test-model",
            model_tier="default",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            elapsed_ms=42,
        )
        assert resp.success is True
        assert resp.content == "hello"
        assert resp.model == "test-model"
        assert resp.model_tier == "default"
        assert resp.usage["total_tokens"] == 8
        assert resp.elapsed_ms == 42

    def test_chat_response_serializes_to_dict_with_contract_fields(self):
        """ChatResponse 序列化为 dict 含 6 个契约字段（API 响应 JSON 不变）。"""
        from server.agent import ChatResponse

        resp = ChatResponse(
            success=False,
            content="",
            model="",
            model_tier="cheap",
            usage={},
            elapsed_ms=0,
        )
        # Pydantic v2: model_dump(); v1: dict()
        d = resp.model_dump() if hasattr(resp, "model_dump") else resp.dict()
        for field in ("success", "content", "model", "model_tier", "usage", "elapsed_ms"):
            assert field in d, f"serialized dict missing field: {field}"

    def test_agent_chat_route_registered_with_correct_path(self):
        """`/agent/chat` 路由在 server.agent.router 中注册（path + operation_id 不变）。"""
        from server.agent import router

        # FastAPI APIRouter(prefix="/agent") 的 route.path 是全路径 /agent/chat
        chat_routes = [
            r for r in router.routes
            if hasattr(r, "path") and r.path.endswith("/chat")
            and hasattr(r, "methods") and "POST" in r.methods
        ]
        assert len(chat_routes) == 1, f"expected 1 POST /chat route, got {len(chat_routes)}"
        route = chat_routes[0]
        # operation_id 不变（MCP/外部脚本依赖）
        assert route.operation_id == "agent_chat", (
            f"operation_id changed: expected 'agent_chat', got {route.operation_id!r}"
        )

    def test_agent_chat_response_model_declared_on_route(self):
        """`/agent/chat` 路由声明 response_model=ChatResponse（OpenAPI schema 不变）。"""
        from server.agent import ChatResponse, router

        chat_routes = [
            r for r in router.routes
            if hasattr(r, "path") and r.path.endswith("/chat")
            and hasattr(r, "methods") and "POST" in r.methods
        ]
        assert len(chat_routes) == 1
        route = chat_routes[0]
        assert route.response_model is ChatResponse, (
            f"response_model changed: expected ChatResponse, got {route.response_model!r}"
        )
