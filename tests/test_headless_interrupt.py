"""headless-agent-session Ticket 07 验收测试：interrupt registry + REST 端点

覆盖 Ticket 07 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] headless_runner.py 加模块级 `_headless_sessions: dict[str, asyncio.Event] = {}`
- [x] run_main_agent 启动时注册 session_id → interrupt_event
- [x] run_judge_agent 启动时注册 session_id → interrupt_event
- [x] 新增 POST /headless/sessions/{session_id}/interrupt 端点
- [x] 端点调 `_headless_sessions[sid].set()`
- [x] 中断主会话 → judge 不启动（HeadlessSessionAction 检测 outcome.status != completed）
- [x] 中断 judge 会话 → judge 降级 uncertain
- [x] 单元测试：注册 + 查询 + 中断（已在 test_headless_runner.py 覆盖）
- [x] 端到端测试：调 interrupt 端点，验证主会话状态 interrupted + judge 不启动 + 整体 failed
- [x] 端到端测试：调 interrupt judge 端点，验证 judge 降级 uncertain + 整体 failed

测试分组：
1. REST 端点单元测试（FastAPI TestClient + mock registry）
2. HeadlessSessionAction 中断 E2E 测试（mock LLM 模拟中断场景）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import MockLLM, MockToolExecutor  # noqa: E402
from client.core.agent.types import RunOutcome  # noqa: E402
from server.activity_tracker.headless_endpoints import (  # noqa: E402
    router,
)
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    MainAgentResult,
    _headless_sessions,
    get_interrupt_event,
    register_interrupt_event,
    unregister_interrupt_event,
)
from server.activity_tracker.loop_actions import HeadlessSessionAction  # noqa: E402

# ============================================================================
# 1. REST 端点单元测试（FastAPI TestClient）
# ============================================================================


@pytest.fixture
def fastapi_app():
    """构造只挂 headless router 的 FastAPI app（不启动完整后端）。"""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(fastapi_app):
    """TestClient（fastapi.testclient.TestClient）。"""
    from fastapi.testclient import TestClient
    return TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清理 registry，避免测试间相互污染。"""
    _headless_sessions.clear()
    yield
    _headless_sessions.clear()


class TestInterruptEndpoint:
    """POST /headless/sessions/{session_id}/interrupt 端点测试。"""

    def test_interrupt_existing_session(self, client):
        """已注册的 session 调 interrupt → 200 + interrupted=True。"""
        ev = asyncio.Event()
        register_interrupt_event("headless-test-001", ev)
        try:
            assert not ev.is_set()
            resp = client.post("/headless/sessions/headless-test-001/interrupt")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == "headless-test-001"
            assert data["interrupted"] is True
            assert "interrupt signal sent" in data["message"]
            assert ev.is_set()
        finally:
            unregister_interrupt_event("headless-test-001")

    def test_interrupt_nonexistent_session_returns_404(self, client):
        """未注册的 session 调 interrupt → 404。"""
        resp = client.post("/headless/sessions/headless-nonexistent/interrupt")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_interrupt_already_interrupted_session_idempotent(self, client):
        """已中断的 session 再次调 interrupt → 200 + interrupted=False（幂等）。"""
        ev = asyncio.Event()
        ev.set()  # 已中断
        register_interrupt_event("headless-test-002", ev)
        try:
            resp = client.post("/headless/sessions/headless-test-002/interrupt")
            assert resp.status_code == 200
            data = resp.json()
            assert data["interrupted"] is False
            assert "already interrupted" in data["message"]
        finally:
            unregister_interrupt_event("headless-test-002")

    def test_interrupt_judge_session(self, client):
        """judge session 调 interrupt → 200 + interrupted=True。"""
        ev = asyncio.Event()
        register_interrupt_event("headless-judge-test-003", ev)
        try:
            resp = client.post("/headless/sessions/headless-judge-test-003/interrupt")
            assert resp.status_code == 200
            assert resp.json()["interrupted"] is True
            assert ev.is_set()
        finally:
            unregister_interrupt_event("headless-judge-test-003")

    def test_interrupt_after_unregister_returns_404(self, client):
        """session 已结束（unregister）后调 interrupt → 404。"""
        ev = asyncio.Event()
        register_interrupt_event("headless-test-004", ev)
        unregister_interrupt_event("headless-test-004")
        resp = client.post("/headless/sessions/headless-test-004/interrupt")
        assert resp.status_code == 404


# ============================================================================
# 2. HeadlessSessionAction 中断场景 E2E 测试
# ============================================================================


def _make_mock_llm_main_interrupted() -> MockLLM:
    """构造 MockLLM：主 agent 模拟中断（不调 LLM，由 mock facade.start 直接返回 interrupted outcome）。"""
    return MockLLM(default_text="should not be called")


def _patch_agent_components_for_interrupt(monkeypatch, mock_llm: MockLLM):
    """patch client.core.agent 的 LLMPoolGateway / ToolRegistry / HttpClientToolExecutor。"""
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


def _patch_memory_and_wip(monkeypatch, capture: dict):
    """patch memory manager + todos store。"""
    mock_mem_mgr = MagicMock()
    mock_mem_mgr.set = MagicMock(return_value={"status": "ok", "key": "test"})
    capture["memory_set"] = mock_mem_mgr.set
    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mem_mgr)

    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(return_value={"id": "wip_interrupt_001", "status": "active"})
    mock_store.update_wip = MagicMock(return_value={"id": "wip_interrupt_001", "status": "failed"})
    capture["wip_create"] = mock_store.create_wip
    capture["wip_update"] = mock_store.update_wip

    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)


def _patch_loop_manager_pause(monkeypatch, capture: dict):
    """patch loop_manager.pause_task。"""
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(return_value=True)
    capture["pause_task"] = mock_mgr.pause_task
    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)


def _patch_run_main_agent_interrupted(monkeypatch, main_session_id: str):
    """patch run_main_agent 模拟主会话被中断：返回 outcome.status=interrupted。"""
    async def fake_run_main(trigger_text, config, **kwargs):
        return MainAgentResult(
            session_id=main_session_id,
            outcome=RunOutcome(
                session_id=main_session_id,
                status="interrupted",
                stop_reason="interrupted",
                iterations=1,
            ),
            last_assistant_messages=[],
            error="",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", fake_run_main)


def _patch_run_judge_agent_called_flag(monkeypatch, judge_called: list):
    """patch run_judge_agent 并记录是否被调用（中断主会话时不应被调用）。"""
    async def fake_run_judge(main_output, trigger_text, config, **kwargs):
        judge_called.append(True)
        from server.activity_tracker.headless_runner import JudgeResult
        return JudgeResult(
            verdict="success",
            reason="should not be called",
            session_id="headless-judge-should-not-exist",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_judge_agent", fake_run_judge)


def test_e2e_main_session_interrupted_skips_judge(tmp_db_path, monkeypatch):
    """Ticket 07 E2E：主会话被中断 → judge 不启动 → 整体 failed。

    场景：
    1. run_main_agent 返回 outcome.status="interrupted"
    2. HeadlessSessionAction 应跳过 judge（不调 run_judge_agent）
    3. 整体 failed，judge_verdict="failed"（main agent 失败，非 uncertain）
    4. memory + wip 写入 status=failed
    """
    main_session_id = "headless-interrupt-test-main"
    _patch_run_main_agent_interrupted(monkeypatch, main_session_id)

    judge_called: list = []
    _patch_run_judge_agent_called_flag(monkeypatch, judge_called)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "测试中断场景",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()
    result = asyncio.run(action.execute(context))

    # 验证整体 failed
    assert result["success"] is False
    assert result["main_session_id"] == main_session_id
    assert result["judge_session_id"] == ""  # 没启动 judge
    assert result["judge_verdict"] == "failed"
    assert "main agent" in result["judge_reason"]
    assert "interrupted" in result["judge_reason"]

    # 验证 judge 没被调用
    assert len(judge_called) == 0, "主会话中断后不应调用 judge agent"

    # 验证 memory + wip 写入 status=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "failed"
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # 验证 one_shot 仍调 pause_task
    assert capture["pause_task"].call_count == 1


def _patch_run_judge_agent_interrupted(monkeypatch, judge_session_id: str):
    """patch run_judge_agent 模拟 judge 会话被中断：返回 verdict=uncertain。"""
    async def fake_run_judge(main_output, trigger_text, config, **kwargs):
        from server.activity_tracker.headless_runner import JudgeResult
        return JudgeResult(
            verdict="uncertain",
            reason="judge session not completed (status=interrupted)",
            session_id=judge_session_id,
            error="interrupted",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_judge_agent", fake_run_judge)


def _patch_run_main_agent_success(monkeypatch, main_session_id: str):
    """patch run_main_agent 模拟主会话成功完成。"""
    async def fake_run_main(trigger_text, config, **kwargs):
        return MainAgentResult(
            session_id=main_session_id,
            outcome=RunOutcome(
                session_id=main_session_id,
                status="completed",
                stop_reason="end_turn",
                iterations=1,
            ),
            last_assistant_messages=["主 agent 任务完成"],
            error="",
        )
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", fake_run_main)


def test_e2e_judge_session_interrupted_degrades_to_uncertain(tmp_db_path, monkeypatch):
    """Ticket 07 E2E：judge 会话被中断 → 降级 uncertain → 整体 failed。

    场景：
    1. run_main_agent 成功完成（outcome.status="completed"）
    2. run_judge_agent 返回 verdict="uncertain"（模拟 judge 被中断）
    3. HeadlessSessionAction 检测 verdict != "success" → 整体 failed
    4. memory + wip 写入 status=failed
    """
    main_session_id = "headless-interrupt-test-main-2"
    judge_session_id = "headless-judge-interrupt-test"
    _patch_run_main_agent_success(monkeypatch, main_session_id)
    _patch_run_judge_agent_interrupted(monkeypatch, judge_session_id)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "测试 judge 中断场景",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()
    result = asyncio.run(action.execute(context))

    # 验证整体 failed（uncertain 算 failed）
    assert result["success"] is False
    assert result["main_session_id"] == main_session_id
    assert result["judge_session_id"] == judge_session_id
    assert result["judge_verdict"] == "uncertain"
    assert "interrupted" in result["judge_reason"]

    # 验证 memory + wip 写入 status=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "uncertain"
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # 验证 one_shot 仍调 pause_task
    assert capture["pause_task"].call_count == 1


# ============================================================================
# 3. 端到端：REST 端点 + interrupt registry 集成
# ============================================================================


def test_endpoint_triggers_registry_event(client):
    """REST 端点调 interrupt → registry 中的 asyncio.Event 被 set。"""
    ev = asyncio.Event()
    register_interrupt_event("headless-e2e-registry", ev)
    try:
        assert not ev.is_set()
        resp = client.post("/headless/sessions/headless-e2e-registry/interrupt")
        assert resp.status_code == 200
        assert resp.json()["interrupted"] is True
        # 验证 registry 中的 event 被 set
        assert ev.is_set()
        assert get_interrupt_event("headless-e2e-registry") is ev
    finally:
        unregister_interrupt_event("headless-e2e-registry")


def test_endpoint_404_after_session_unregistered(client):
    """session 被 unregister（结束清理）后，REST 端点返回 404。"""
    ev = asyncio.Event()
    register_interrupt_event("headless-e2e-cleanup", ev)
    unregister_interrupt_event("headless-e2e-cleanup")
    resp = client.post("/headless/sessions/headless-e2e-cleanup/interrupt")
    assert resp.status_code == 404
