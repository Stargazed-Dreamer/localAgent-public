"""v6-lite T06 验收测试：W3 审批桥接 + 中断 + 启动恢复

覆盖 T06 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] T06a: 路径 A（exec_* 同步阻塞审批拒绝）：403 user_denied + user_feedback → feedback 回灌
- [x] T06b: 路径 B（非审查端点审批挑战）：403 approval_id → /command-guard/request-approval → token → 重试
- [x] T06c: 中断（interrupt）：interrupt_event.set() → transition(user_interrupted) + session interrupted
- [x] T06d: 启动 reconciliation：streaming→interrupted + yieldMissingToolResultBlocks
- [x] T06e: 端到端验收

测试策略：
- T06a/T06b: mock requests.Session 模拟 server 响应（不依赖真实 server）
- T06c: asyncio.Event + MockLLM callable 模拟中途中断
- T06d: 真实 EventStore + 手动构造悬挂状态 + reconcile()
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    HttpClientToolExecutor,
    MockLLM,
    MockToolExecutor,
    ReconcileResult,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolCall,
    ToolRegistry,
    ToolResult,
    reconcile,
)
from client.core.agent.types import (  # noqa: E402
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_INTERRUPTED,
    TOOL_STATUS_PENDING,
    TOOL_STATUS_RUNNING,
    TRANSITION_USER_INTERRUPTED,
    LLMResponse,
    Message,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_test_openapi_spec() -> dict:
    """构造测试用 OpenAPI spec（同 T05）。"""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/test/list-dir": {
                "get": {
                    "operationId": "list_dir",
                    "summary": "List directory contents",
                    "x-agent-callable": True,
                    "x-tool-safety": "read_only",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                },
            },
            "/exec/python": {
                "post": {
                    "operationId": "exec_python",
                    "summary": "Execute Python code",
                    "x-agent-callable": True,
                    "x-tool-safety": "approval_required",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                    },
                                    "required": ["code"],
                                }
                            }
                        }
                    },
                },
            },
        },
    }


def _make_registry() -> ToolRegistry:
    """构造测试用 ToolRegistry。"""
    reg = ToolRegistry()
    reg._entries = reg._materialize(_make_test_openapi_spec())
    return reg


def _make_mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str | None = None,
) -> MagicMock:
    """构造 mock requests.Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
        if text is None:
            text = json.dumps(json_data)
    resp.text = text or ""
    return resp


def _make_store(tmp_path) -> EventStore:
    """构造已 init 的 EventStore（临时 DB）。"""
    db_path = str(tmp_path / "test_t06.db")
    store = EventStore(db_path)
    store.init()
    return store


# ============================================================================
# T06a: 路径 A — exec_* 同步阻塞审批拒绝（403 user_denied + user_feedback）
# ============================================================================


class TestT06aPathAUserDenied:
    """T06a: 路径 A 审批拒绝——403 error=user_denied + user_feedback → feedback 回灌模型。"""

    def test_user_denied_returns_feedback_as_tool_result(self):
        """403 + error=user_denied + user_feedback → is_error=True, content 含 feedback。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=403,
            json_data={
                "error": "user_denied",
                "user_feedback": "不允许执行此代码，可能有风险",
            },
        )

        with patch.object(ex._session, "post", return_value=mock_resp):
            tc = ToolCall(id="c1", name="exec_python", args={"code": "os.remove('C:/')"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "denied" in result.content.lower()
        assert "不允许执行此代码" in result.content

    def test_user_denied_empty_feedback_uses_placeholder(self):
        """403 + error=user_denied + user_feedback="" → 用占位符。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=403,
            json_data={
                "error": "user_denied",
                "user_feedback": "",
            },
        )

        with patch.object(ex._session, "post", return_value=mock_resp):
            tc = ToolCall(id="c2", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "denied" in result.content.lower()
        assert "(no feedback)" in result.content

    def test_user_denied_feedback_truncated_to_1000_chars(self):
        """超长 feedback 截断到 1000 字符。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        long_feedback = "A" * 2000
        mock_resp = _make_mock_response(
            status_code=403,
            json_data={
                "error": "user_denied",
                "user_feedback": long_feedback,
            },
        )

        with patch.object(ex._session, "post", return_value=mock_resp):
            tc = ToolCall(id="c3", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        # feedback 被 [:1000] 截断
        assert "A" * 1000 in result.content
        assert "A" * 1001 not in result.content

    def test_user_denied_does_not_trigger_approval_challenge(self):
        """路径 A 拒绝不触发路径 B（不调 /command-guard/request-approval）。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=403,
            json_data={
                "error": "user_denied",
                "user_feedback": "denied",
            },
        )

        with patch.object(ex._session, "post", return_value=mock_resp) as mock_post:
            tc = ToolCall(id="c4", name="exec_python", args={"code": "print(1)"})
            asyncio.run(ex.execute(tc))

        # 只调了 1 次 post（原请求），没有第二次调 request-approval
        assert mock_post.call_count == 1


# ============================================================================
# T06b: 路径 B — 非审查端点审批挑战（403 approval_id → request-approval → token → 重试）
# ============================================================================


class TestT06bPathBApprovalChallenge:
    """T06b: 路径 B 审批挑战——403 approval_id → /command-guard/request-approval → token → 重试。"""

    def test_approval_approved_then_retry_succeeds(self):
        """批准 → 拿 token → 重试成功 → is_error=False。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        # 第一次请求返回 403 approval_required
        resp_403 = _make_mock_response(
            status_code=403,
            json_data={
                "error": "approval_required",
                "approval_id": "appr_456",
                "message": "needs approval",
            },
        )
        # 审批端点返回批准 + token
        resp_approval = _make_mock_response(
            status_code=200,
            json_data={
                "approved": True,
                "approval_token": "tok_abc",
                "expires_in_seconds": 300,
                "feedback": "",
            },
        )
        # 重试请求返回成功
        resp_retry = _make_mock_response(
            status_code=200,
            json_data={"result": "code executed", "exit_code": 0},
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, resp_approval, resp_retry]
        ) as mock_post:
            tc = ToolCall(id="c1", name="exec_python", args={"code": "print('hi')"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is False
        assert "code executed" in result.content
        # 3 次 post：原请求 → 审批端点 → 重试
        assert mock_post.call_count == 3

        # 验证第二次调的是审批端点
        approval_call = mock_post.call_args_list[1]
        assert "/command-guard/request-approval" in approval_call.args[0]
        assert approval_call.kwargs["json"]["approval_id"] == "appr_456"
        assert "agent_reason" in approval_call.kwargs["json"]

        # 验证第三次调用含 X-Approval-Token 头
        retry_call = mock_post.call_args_list[2]
        assert retry_call.kwargs["headers"]["X-Approval-Token"] == "tok_abc"
        assert retry_call.kwargs["headers"]["X-Agent-Caller"] == "v6-lite-agent"

    def test_approval_denied_returns_feedback(self):
        """拒绝 → is_error=True, content 含 feedback。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={
                "approval_id": "appr_789",
                "message": "needs approval",
            },
        )
        resp_approval_denied = _make_mock_response(
            status_code=200,
            json_data={
                "approved": False,
                "decision": "deny",
                "feedback": "User said no",
            },
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, resp_approval_denied]
        ) as mock_post:
            tc = ToolCall(id="c2", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "denied" in result.content.lower()
        assert "User said no" in result.content
        # 只调了 2 次（原请求 + 审批端点），没有重试
        assert mock_post.call_count == 2

    def test_approval_granted_but_no_token_returns_error(self):
        """批准但无 approval_token → is_error=True。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={"approval_id": "appr_no_token", "message": "needs approval"},
        )
        resp_approval_no_token = _make_mock_response(
            status_code=200,
            json_data={
                "approved": True,
                # 缺 approval_token
                "feedback": "",
            },
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, resp_approval_no_token]
        ):
            tc = ToolCall(id="c3", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "no approval_token" in result.content.lower()

    def test_approval_request_timeout_returns_error(self):
        """审批请求超时 → is_error=True（不抛异常）。"""
        import requests as req_module

        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={"approval_id": "appr_timeout", "message": "needs approval"},
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, req_module.exceptions.Timeout()]
        ):
            tc = ToolCall(id="c4", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "timed out" in result.content.lower()

    def test_approval_request_network_error_returns_error(self):
        """审批请求网络错误 → is_error=True（不抛异常）。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={"approval_id": "appr_net", "message": "needs approval"},
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, ConnectionError("network down")]
        ):
            tc = ToolCall(id="c5", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "Approval request failed" in result.content

    def test_approved_retry_still_fails_returns_error(self):
        """批准后重试仍返回 HTTP 错误 → is_error=True。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={"approval_id": "appr_retry_fail", "message": "needs approval"},
        )
        resp_approval = _make_mock_response(
            status_code=200,
            json_data={"approved": True, "approval_token": "tok_retry", "feedback": ""},
        )
        resp_retry_fail = _make_mock_response(
            status_code=500,
            json_data={"error": "internal server error"},
            text='{"error": "internal server error"}',
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, resp_approval, resp_retry_fail]
        ):
            tc = ToolCall(id="c6", name="exec_python", args={"code": "print(1)"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "500" in result.content
        assert "Approved request still failed" in result.content

    def test_agent_reason_contains_tool_name_and_safety(self):
        """agent_reason 包含工具名和 safety 等级。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        resp_403 = _make_mock_response(
            status_code=403,
            json_data={"approval_id": "appr_reason", "message": "needs approval"},
        )
        resp_approval = _make_mock_response(
            status_code=200,
            json_data={"approved": False, "feedback": "no"},
        )

        with patch.object(
            ex._session, "post", side_effect=[resp_403, resp_approval]
        ) as mock_post:
            tc = ToolCall(id="c7", name="exec_python", args={"code": "print(1)"})
            asyncio.run(ex.execute(tc))

        approval_call = mock_post.call_args_list[1]
        agent_reason = approval_call.kwargs["json"]["agent_reason"]
        assert "exec_python" in agent_reason
        assert "approval_required" in agent_reason


# ============================================================================
# T06c: 中断（interrupt）— interrupt_event + transition(user_interrupted)
# ============================================================================


class TestT06cInterrupt:
    """T06c: 中断机制——interrupt_event.set() → 写 transition(user_interrupted) → 终止。"""

    def test_interrupt_event_none_backward_compatible(self, tmp_path):
        """interrupt_event=None → 不检查中断，正常完成（向后兼容 T01-T05）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            asyncio.run(store.append_message("s1", Message(role="user", content="hi")))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hello!"),
                    event_store=store,
                    # 不注入 interrupt_event
                ),
                config=RunnerConfig(session_id="s1"),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "completed"
            assert runner.last_transition_reason is None  # 没有 transition 事件
        finally:
            store.close()

    def test_interrupt_set_before_run_immediate_interrupt(self, tmp_path):
        """interrupt_event 在 run() 前已 set → 第一轮检查即中断（iterations=0）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            asyncio.run(store.append_message("s2", Message(role="user", content="hi")))

            ev = asyncio.Event()
            ev.set()  # 预先 set

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hello!"),
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s2"),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "user_interrupted"
            assert outcome.iterations == 0  # 没有执行任何迭代

            # 验证 session 状态
            session = asyncio.run(store.get_session("s2"))
            assert session.status == "interrupted"

            # 验证 transition 事件
            events = asyncio.run(store.load_events("s2"))
            transition_events = [e for e in events if e.type == "transition"]
            assert len(transition_events) == 1
            assert transition_events[0].payload["reason"] == TRANSITION_USER_INTERRUPTED
        finally:
            store.close()

    def test_interrupt_set_during_run_mid_iteration(self, tmp_path):
        """interrupt_event 在第一轮 LLM 返回后 set → A1 工具循环开始前检测到中断。

        A1（spec D9）新语义：interrupt 在工具循环开始前已 set → 第一个工具不执行
        → 直接写 transition(user_interrupted) + 返回 interrupted。
        旧语义（工具执行完 + next_turn + 下一轮顶部中断）已被 A1 替代，因为 spec D9
        要求"用户 interrupt 后，任何工具执行必须立即阻断"。
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s3"))
            asyncio.run(store.append_message("s3", Message(role="user", content="do task")))

            ev = asyncio.Event()
            call_count = [0]
            tool_executed = []  # 追踪工具是否执行

            def llm_callable(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一轮返回 tool_calls → 触发工具执行
                    # 返回后 set interrupt_event（工具循环开始前 set）
                    ev.set()
                    return LLMResponse(
                        content="Let me check.",
                        tool_calls=[
                            {
                                "id": "tc_1",
                                "type": "function",
                                "function": {
                                    "name": "list_dir",
                                    "arguments": '{"path": "."}',
                                },
                            }
                        ],
                        stop_reason="tool_use",
                    )
                # 不应该到这里（第一轮工具循环前就中断了）
                return LLMResponse(content="should not reach")

            def tool_callable(tc):
                tool_executed.append(tc.name)
                return ToolResult(tool_call_id=tc.id, content='{"files": []}', is_error=False)

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(callable=llm_callable),
                    event_store=store,
                    tool_executor=MockToolExecutor(callable=tool_callable),
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s3", max_iterations=10, use_stream=False),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "user_interrupted"
            assert outcome.iterations == 1  # 第一轮顶部已计数

            # A1 新语义：工具循环开始前检测到 interrupt → 工具未执行
            assert tool_executed == [], (
                f"A1 修复后工具不应执行，实际执行了：{tool_executed}"
            )

            # 只有 user_interrupted transition，没有 next_turn（工具未执行）
            events = asyncio.run(store.load_events("s3"))
            transition_events = [e for e in events if e.type == "transition"]
            reasons = [e.payload["reason"] for e in transition_events]
            assert TRANSITION_USER_INTERRUPTED in reasons
            assert "next_turn" not in reasons  # A1：工具未执行，无 next_turn
        finally:
            store.close()

    def test_interrupt_not_set_normal_completion(self, tmp_path):
        """interrupt_event 注入但未 set → 正常完成。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s4"))
            asyncio.run(store.append_message("s4", Message(role="user", content="hi")))

            ev = asyncio.Event()  # 未 set

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="done!"),
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s4"),
            )
            outcome = asyncio.run(runner.run())

            assert outcome.status == "completed"
            assert runner.last_transition_reason is None
        finally:
            store.close()

    def test_interrupt_writes_correct_transition_payload(self, tmp_path):
        """transition(user_interrupted) 事件 payload 含 reason + iterations + tool_calls_count=0。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s5"))
            asyncio.run(store.append_message("s5", Message(role="user", content="hi")))

            ev = asyncio.Event()
            ev.set()

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hi"),
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s5"),
            )
            asyncio.run(runner.run())

            events = asyncio.run(store.load_events("s5"))
            interrupt_events = [
                e for e in events
                if e.type == "transition"
                and e.payload.get("reason") == TRANSITION_USER_INTERRUPTED
            ]
            assert len(interrupt_events) == 1
            payload = interrupt_events[0].payload
            assert payload["reason"] == TRANSITION_USER_INTERRUPTED
            assert payload["tool_calls_count"] == 0
            assert "iterations" in payload
        finally:
            store.close()

    def test_interrupt_does_not_raise_exception(self, tmp_path):
        """中断不依赖异常传播——不抛任何异常。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s6"))
            asyncio.run(store.append_message("s6", Message(role="user", content="hi")))

            ev = asyncio.Event()
            ev.set()

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(default_text="hi"),
                    event_store=store,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s6"),
            )
            # 不应抛异常
            outcome = asyncio.run(runner.run())
            assert outcome.status == "interrupted"
            assert outcome.error == ""  # 无错误信息
        finally:
            store.close()


# ============================================================================
# T06d: 启动 reconciliation — streaming→interrupted + yieldMissingToolResultBlocks
# ============================================================================


class TestT06dReconciliation:
    """T06d: 启动恢复——扫描残留活跃会话 + 悬挂 tool_calls。"""

    def test_reconcile_empty_db_no_actions(self, tmp_path):
        """空 DB → 无操作。"""
        store = _make_store(tmp_path)
        try:
            result = asyncio.run(reconcile(store, force=True))
            assert result.total_actions == 0
            assert result.sessions_interrupted == []
            assert result.tool_calls_interrupted == []
            assert result.tool_results_filled == []
        finally:
            store.close()

    def test_reconcile_streaming_session_marked_interrupted(self, tmp_path):
        """status=streaming 的会话 → 标记 interrupted。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            # 手动设为 streaming（模拟进程退出时残留）
            asyncio.run(store.update_session_status("s1", "streaming"))

            result = asyncio.run(reconcile(store, force=True))

            assert "s1" in result.sessions_interrupted
            session = asyncio.run(store.get_session("s1"))
            assert session.status == "interrupted"

            # 验证写了 session_reconciled 事件
            events = asyncio.run(store.load_events("s1"))
            reconciled_events = [e for e in events if e.type == "session_reconciled"]
            assert len(reconciled_events) == 1
            assert reconciled_events[0].payload["from_status"] == "streaming"
            assert reconciled_events[0].payload["to_status"] == "interrupted"
        finally:
            store.close()

    def test_reconcile_awaiting_tools_session_marked_interrupted(self, tmp_path):
        """status=awaiting_tools 的会话 → 标记 interrupted。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            asyncio.run(store.update_session_status("s2", "awaiting_tools"))

            result = asyncio.run(reconcile(store, force=True))

            assert "s2" in result.sessions_interrupted
            session = asyncio.run(store.get_session("s2"))
            assert session.status == "interrupted"
        finally:
            store.close()

    def test_reconcile_completed_session_not_touched(self, tmp_path):
        """status=completed 的会话 → 不动。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s3"))
            asyncio.run(store.update_session_status("s3", "completed"))

            result = asyncio.run(reconcile(store, force=True))

            assert "s3" not in result.sessions_interrupted
            session = asyncio.run(store.get_session("s3"))
            assert session.status == "completed"
        finally:
            store.close()

    def test_reconcile_idle_session_not_touched(self, tmp_path):
        """status=idle 的会话 → 不动。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s4"))

            result = asyncio.run(reconcile(store, force=True))

            assert "s4" not in result.sessions_interrupted
            session = asyncio.run(store.get_session("s4"))
            assert session.status == "idle"
        finally:
            store.close()

    def test_reconcile_pending_tool_call_fills_error_result(self, tmp_path):
        """status=pending 的 tool_call + 无 tool_result → 补 is_error=true tool_result。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s5"))
            # 手动插入一个 pending tool_call（模拟进程退出时残留）
            tc = ToolCall(id="tc_dangling_1", session_id="s5", name="list_dir",
                          args={"path": "."}, status=TOOL_STATUS_PENDING)
            asyncio.run(store.append_tool_call("s5", tc))

            result = asyncio.run(reconcile(store, force=True))

            assert "tc_dangling_1" in result.tool_calls_interrupted
            assert "tc_dangling_1" in result.tool_results_filled

            # 验证 tool_call 状态变为 interrupted
            tool_calls = asyncio.run(store.load_tool_calls("s5"))
            assert tool_calls[0].status == TOOL_STATUS_INTERRUPTED

            # 验证补了 role=tool 消息
            messages = asyncio.run(store.load_messages("s5", include_invisible=True))
            tool_msgs = [m for m in messages if m.role == "tool"]
            assert len(tool_msgs) == 1
            assert tool_msgs[0].tool_call_id == "tc_dangling_1"
            assert "interrupted" in tool_msgs[0].content.lower()
        finally:
            store.close()

    def test_reconcile_running_tool_call_fills_error_result(self, tmp_path):
        """status=running 的 tool_call + 无 tool_result → 补 is_error=true tool_result。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s6"))
            tc = ToolCall(id="tc_dangling_2", session_id="s6", name="exec_python",
                          args={"code": "print(1)"}, status=TOOL_STATUS_RUNNING)
            asyncio.run(store.append_tool_call("s6", tc))
            # 手动改为 running（append_tool_call 默认写 pending）
            asyncio.run(store.update_tool_call_status("tc_dangling_2", TOOL_STATUS_RUNNING))

            result = asyncio.run(reconcile(store, force=True))

            assert "tc_dangling_2" in result.tool_calls_interrupted
            assert "tc_dangling_2" in result.tool_results_filled

            # 验证补了 tool_result 消息
            has_result = asyncio.run(store.has_tool_result("tc_dangling_2"))
            assert has_result is True
        finally:
            store.close()

    def test_reconcile_completed_tool_call_not_touched(self, tmp_path):
        """status=completed 的 tool_call → 不动。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s7"))
            tc = ToolCall(id="tc_done", session_id="s7", name="list_dir",
                          args={"path": "."}, status=TOOL_STATUS_COMPLETED)
            asyncio.run(store.append_tool_call("s7", tc))
            asyncio.run(store.update_tool_call_status("tc_done", TOOL_STATUS_COMPLETED))

            result = asyncio.run(reconcile(store, force=True))

            assert "tc_done" not in result.tool_calls_interrupted
            assert "tc_done" not in result.tool_results_filled
        finally:
            store.close()

    def test_reconcile_tool_call_with_existing_result_skips_fill(self, tmp_path):
        """tool_call 已有 tool_result → 跳过补消息（幂等）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s8"))
            tc = ToolCall(id="tc_has_result", session_id="s8", name="list_dir",
                          args={"path": "."}, status=TOOL_STATUS_PENDING)
            asyncio.run(store.append_tool_call("s8", tc))
            # 先补一个 tool_result（模拟之前已执行）
            asyncio.run(store.append_tool_result_message("s8", ToolResult(
                tool_call_id="tc_has_result", content='{"files": []}', is_error=False,
            )))

            result = asyncio.run(reconcile(store, force=True))

            # 跳过补消息
            assert "tc_has_result" not in result.tool_results_filled
            assert result.skipped_tool_calls_already_has_result >= 1
            # 但仍标记 interrupted
            assert "tc_has_result" in result.tool_calls_interrupted
        finally:
            store.close()

    def test_reconcile_idempotent(self, tmp_path):
        """运行两次 reconcile → 第二次无新操作。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s9"))
            asyncio.run(store.update_session_status("s9", "streaming"))
            tc = ToolCall(id="tc_idem", session_id="s9", name="list_dir",
                          args={"path": "."}, status=TOOL_STATUS_PENDING)
            asyncio.run(store.append_tool_call("s9", tc))

            # 第一次 reconcile
            result1 = asyncio.run(reconcile(store, force=True))
            assert len(result1.sessions_interrupted) == 1
            assert len(result1.tool_calls_interrupted) == 1
            assert len(result1.tool_results_filled) == 1

            # 第二次 reconcile
            result2 = asyncio.run(reconcile(store, force=True))
            assert len(result2.sessions_interrupted) == 0
            assert len(result2.tool_calls_interrupted) == 0
            assert len(result2.tool_results_filled) == 0
        finally:
            store.close()

    def test_reconcile_multiple_sessions_and_tool_calls(self, tmp_path):
        """多会话 + 多 tool_calls 混合场景。"""
        store = _make_store(tmp_path)
        try:
            # s10: streaming + 1 pending tool_call
            asyncio.run(store.create_session(session_id="s10"))
            asyncio.run(store.update_session_status("s10", "streaming"))
            tc1 = ToolCall(id="tc_multi_1", session_id="s10", name="list_dir",
                           args={"path": "."})
            asyncio.run(store.append_tool_call("s10", tc1))

            # s11: awaiting_tools + 1 running tool_call
            asyncio.run(store.create_session(session_id="s11"))
            asyncio.run(store.update_session_status("s11", "awaiting_tools"))
            tc2 = ToolCall(id="tc_multi_2", session_id="s11", name="exec_python",
                           args={"code": "print(1)"})
            asyncio.run(store.append_tool_call("s11", tc2))
            asyncio.run(store.update_tool_call_status("tc_multi_2", TOOL_STATUS_RUNNING))

            # s12: completed（不动）
            asyncio.run(store.create_session(session_id="s12"))
            asyncio.run(store.update_session_status("s12", "completed"))

            result = asyncio.run(reconcile(store, force=True))

            assert set(result.sessions_interrupted) == {"s10", "s11"}
            assert set(result.tool_calls_interrupted) == {"tc_multi_1", "tc_multi_2"}
            assert set(result.tool_results_filled) == {"tc_multi_1", "tc_multi_2"}

            # s12 不动
            session12 = asyncio.run(store.get_session("s12"))
            assert session12.status == "completed"
        finally:
            store.close()

    def test_reconcile_returns_reconcile_result_type(self, tmp_path):
        """reconcile() 返回 ReconcileResult 类型。"""
        store = _make_store(tmp_path)
        try:
            result = asyncio.run(reconcile(store, force=True))
            assert isinstance(result, ReconcileResult)
            assert hasattr(result, "sessions_interrupted")
            assert hasattr(result, "tool_calls_interrupted")
            assert hasattr(result, "tool_results_filled")
            assert hasattr(result, "total_actions")
        finally:
            store.close()


# ============================================================================
# T06e: 端到端验收 — 审批拒绝 + 中断 + 重启恢复 全链路
# ============================================================================


class TestT06eEndToEnd:
    """T06e: 端到端验收——审批拒绝 → 中断 → 重启恢复 全链路。"""

    def test_e2e_approval_denied_then_interrupt_then_reconcile(self, tmp_path):
        """完整链路：
        1. MockLLM 返回 tool_calls
        2. MockToolExecutor 返回 user_denied 错误（模拟审批拒绝）
        3. 模型看到 feedback 后再次返回 tool_calls
        4. 此时中断
        5. 重启后 reconcile 恢复
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s_e2e"))
            asyncio.run(store.append_message(
                "s_e2e", Message(role="user", content="执行危险代码"),
            ))

            ev = asyncio.Event()
            call_count = [0]

            def llm_callable(req):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一轮：返回 tool_calls
                    return LLMResponse(
                        content="好的，我来执行。",
                        tool_calls=[{
                            "id": "tc_e2e_1",
                            "type": "function",
                            "function": {
                                "name": "exec_python",
                                "arguments": '{"code": "os.remove(\'C:/\')"}',
                            },
                        }],
                        stop_reason="tool_use",
                    )
                elif call_count[0] == 2:
                    # 第二轮：看到审批拒绝后，再次尝试
                    # 此时 set interrupt_event
                    ev.set()
                    return LLMResponse(
                        content="审批被拒绝了，我换个方法试试。",
                        tool_calls=[{
                            "id": "tc_e2e_2",
                            "type": "function",
                            "function": {
                                "name": "list_dir",
                                "arguments": '{"path": "."}',
                            },
                        }],
                        stop_reason="tool_use",
                    )
                return LLMResponse(content="should not reach")

            # MockToolExecutor：第一次返回审批拒绝，第二次不会被调用（中断）
            mock_ex = MockToolExecutor(fixtures={
                "tc_e2e_1": ToolResult(
                    tool_call_id="tc_e2e_1",
                    content="User denied approval: 此代码有风险，不允许执行",
                    is_error=True,
                ),
            })

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=MockLLM(callable=llm_callable),
                    event_store=store,
                    tool_executor=mock_ex,
                    interrupt_event=ev,
                ),
                config=RunnerConfig(session_id="s_e2e", max_iterations=10, use_stream=False),  # call 模式轮次间中断
            )
            outcome = asyncio.run(runner.run())

            # 验证中断
            assert outcome.status == "interrupted"
            assert outcome.stop_reason == "user_interrupted"

            # 验证审批拒绝的 feedback 在 messages 中
            messages = asyncio.run(store.load_messages("s_e2e", include_invisible=True))
            tool_msgs = [m for m in messages if m.role == "tool"]
            assert len(tool_msgs) >= 1
            assert "denied" in tool_msgs[0].content.lower()
            assert "此代码有风险" in tool_msgs[0].content

            # 验证有 next_turn + user_interrupted 两个 transition
            events = asyncio.run(store.load_events("s_e2e"))
            transition_events = [e for e in events if e.type == "transition"]
            reasons = [e.payload["reason"] for e in transition_events]
            assert "next_turn" in reasons  # 第一轮工具回灌后
            assert TRANSITION_USER_INTERRUPTED in reasons  # 第二轮中断

            # === 模拟重启 ===
            # 此时 session 状态应该是 interrupted（runner 已写）
            session = asyncio.run(store.get_session("s_e2e"))
            assert session.status == "interrupted"

            # 但假设进程在 streaming 时被杀（手动改回 streaming 模拟）
            # 且 tc_e2e_2 可能还在 pending（第二轮的 tool_call 被中断前已写入）
            # 检查是否有悬挂的 tool_call
            all_tool_calls = asyncio.run(store.load_tool_calls("s_e2e"))
            pending_or_running = [
                tc for tc in all_tool_calls
                if tc.status in (TOOL_STATUS_PENDING, TOOL_STATUS_RUNNING)
            ]

            # E5（spec D7）行为变更：runner 在 interrupt 路径已调 _repair_dangling_tool_calls
            # 为悬挂 tool_call 补 is_error=true tool_result，reconciler 不需要再补。
            # 验证：tc_e2e_2 已有 tool_result（由 E5 在 interrupt 时补全）
            if pending_or_running:
                for tc in pending_or_running:
                    has_result = asyncio.run(store.has_tool_result(tc.id))
                    assert has_result, (
                        f"E5: 悬挂 tool_call {tc.id} 应在 interrupt 时已被 _repair_dangling_tool_calls "
                        f"补 is_error=true tool_result（reconciler 不再需要补）"
                    )

                # 模拟进程被杀：session 改回 streaming
                asyncio.run(store.update_session_status("s_e2e", "streaming"))

                result = asyncio.run(reconcile(store, force=True))
                assert "s_e2e" in result.sessions_interrupted
                # E5 后：tool_results_filled 为空（reconciler 跳过已有 result 的 tool_call）
                # 验证 reconciler 正确跳过（skipped_tool_calls_already_has_result >= 1）
                assert result.skipped_tool_calls_already_has_result >= 1, (
                    "E5: reconciler 应跳过已被 runner 修复的 tool_call "
                    f"(skipped_tool_calls_already_has_result={result.skipped_tool_calls_already_has_result})"
                )

            # 最终验证：session 是 interrupted
            session = asyncio.run(store.get_session("s_e2e"))
            assert session.status == "interrupted"
        finally:
            store.close()

    def test_e2e_interrupt_mid_tool_execution_then_reconcile(self, tmp_path):
        """中断发生在工具执行后、transition 写入前 → 重启后补 tool_result。

        场景：LLM 返回 tool_calls → tool_call 写入 pending → 工具执行 →
        但进程在 update_status(completed) 前被杀 → 重启时 tool_call 状态仍是 running。
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s_e2e2"))
            asyncio.run(store.append_message(
                "s_e2e2", Message(role="user", content="list files"),
            ))

            # 手动构造"进程被杀"场景：
            # 1. session 状态 = streaming
            # 2. assistant 消息含 tool_calls
            # 3. tool_call 状态 = running（执行中被杀）
            # 4. 无 tool_result 消息（没来得及写）
            asyncio.run(store.update_session_status("s_e2e2", "streaming"))

            assistant_msg = Message(
                role="assistant",
                content="Let me check.",
                source="assistant",
                tool_calls=[{
                    "id": "tc_e2e2_1",
                    "type": "function",
                    "function": {
                        "name": "list_dir",
                        "arguments": '{"path": "."}',
                    },
                }],
            )
            asyncio.run(store.append_message("s_e2e2", assistant_msg))

            tc = ToolCall(
                id="tc_e2e2_1", session_id="s_e2e2", name="list_dir",
                args={"path": "."}, status=TOOL_STATUS_PENDING,
            )
            asyncio.run(store.append_tool_call("s_e2e2", tc))
            asyncio.run(store.update_tool_call_status("tc_e2e2_1", TOOL_STATUS_RUNNING))

            # === 重启 reconcile ===
            result = asyncio.run(reconcile(store, force=True))

            # session 被标记 interrupted
            assert "s_e2e2" in result.sessions_interrupted

            # tool_call 被补 tool_result + 标记 interrupted
            assert "tc_e2e2_1" in result.tool_calls_interrupted
            assert "tc_e2e2_1" in result.tool_results_filled

            # 验证 tool_call 状态
            tool_calls = asyncio.run(store.load_tool_calls("s_e2e2"))
            assert tool_calls[0].status == TOOL_STATUS_INTERRUPTED

            # 验证补了 is_error=true 的 tool_result
            messages = asyncio.run(store.load_messages("s_e2e2", include_invisible=True))
            tool_msgs = [m for m in messages if m.role == "tool"]
            assert len(tool_msgs) == 1
            assert tool_msgs[0].tool_call_id == "tc_e2e2_1"
            assert "interrupted" in tool_msgs[0].content.lower()

            # session 最终状态
            session = asyncio.run(store.get_session("s_e2e2"))
            assert session.status == "interrupted"
        finally:
            store.close()

    def test_e2e_reconcile_allows_session_resumption(self, tmp_path):
        """reconcile 后会话可继续对话（dangling tool_call 已补 result，provider 不 400）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s_e2e3"))
            asyncio.run(store.update_session_status("s_e2e3", "streaming"))

            # 构造悬挂 tool_call
            tc = ToolCall(
                id="tc_e2e3_1", session_id="s_e2e3", name="list_dir",
                args={"path": "."}, status=TOOL_STATUS_PENDING,
            )
            asyncio.run(store.append_tool_call("s_e2e3", tc))

            # reconcile 补 tool_result（C2：force=True 跳过活跃性检查，测试模拟进程已死）
            asyncio.run(reconcile(store, force=True))

            # 验证：现在消息序列是合法的（assistant 含 tool_calls → tool result 配对）
            messages = asyncio.run(store.load_messages("s_e2e3", include_invisible=True))
            tool_msgs = [m for m in messages if m.role == "tool"]
            assert len(tool_msgs) == 1
            # 每个悬挂 tool_call 都有对应的 tool_result → provider 不会 400
            assert tool_msgs[0].tool_call_id == "tc_e2e3_1"

            # 可以继续对话（session 状态 = interrupted，用户可手动恢复或新建）
            session = asyncio.run(store.get_session("s_e2e3"))
            assert session.status == "interrupted"
        finally:
            store.close()
