"""headless-agent-session Ticket 06 验收测试：端到端测试（一次性任务）

覆盖 Ticket 06 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新建 tests/test_headless_session_e2e.py
- [x] 测试 fixture：临时 EventStore DB + mock LLM
- [x] 测试用例 1：配置 trigger_text="回复'headless test ok'即可，不要调任何工具"
- [x] 验证 task.last_result 含 judge_verdict 字段
- [x] 验证主会话 + judge 会话都创建（2 个 session_id）
- [x] 验证主会话 mode="headless"，judge 会话 mode="headless_judge"
- [x] 验证主会话 assistant 消息含 "headless test ok"
- [x] 验证 judge 会话最后一条 assistant 消息含 JSON verdict
- [x] 验证 memory 记录含 main_session_id / judge_session_id / judge_verdict / judge_reason
- [x] 验证 wip 记录含 status（success→completed）
- [x] 测试用例 2：验证 client 进程不在线时后端仍能跑（action 不依赖 client 进程）

E2E 策略：
- MockLLM（callable 模式）：根据 request.messages 判断是 main agent 还是 judge agent 调用
  - main agent 调用：返回 "headless test ok"
  - judge agent 调用：返回 JSON verdict {"verdict": "success", "reason": "..."}
- Mock ToolRegistry / HttpClientToolExecutor（不真实调 HTTP）
- 真实 SessionFacade + EventStore（temp DB）
- 真实 HeadlessSessionAction.execute()（完整流程）
- Mock memory manager + todos store（捕获写入，不真实写 memory.db）
- 直接调 action.execute()，不经 LoopManager 调度（避免等 cron 触发）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    MockLLM,
    MockToolExecutor,
)
from client.core.agent.types import LLMRequest, LLMResponse  # noqa: E402
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
)
from server.activity_tracker.loop_actions import HeadlessSessionAction  # noqa: E402

# ============================================================================
# Fixtures 与辅助函数
# ============================================================================


def _make_mock_llm() -> MockLLM:
    """构造 MockLLM：根据 request 全文判断是 main agent 还是 judge agent。

    - main agent 调用（system+user 不含 "判定"/"verdict"）：返回 "headless test ok"
    - judge agent 调用（user message 含 JUDGE_PROMPT_TEMPLATE 的 "判定"/"verdict"）：返回 JSON verdict success

    关键：JUDGE_PROMPT_TEMPLATE 是作为 user message 传给 facade.start，
    不在 LLMRequest.system 字段（system 是 stable prefix + ephemeral context）。
    所以判断时必须检查 user message 内容，不能只看 system。
    """
    def _extract_full_text(request: LLMRequest) -> str:
        """提取 request 中所有可见文本（system + 所有 messages）。"""
        parts: list[str] = [request.system or ""]
        for msg in request.messages:
            content = msg.content
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
        return "\n".join(parts)

    def generate(request: LLMRequest) -> LLMResponse:
        full_text = _extract_full_text(request)

        if "你是任务成功判定" in full_text or "【判定规则】" in full_text:
            # judge agent 调用（JUDGE_PROMPT_TEMPLATE 唯一标记，
            # 不用 '判定'/'verdict' 关键词，避免 AGENTS.md 污染 system prompt 误判）
            return LLMResponse(
                content='{"verdict": "success", "reason": "主 agent 输出含 headless test ok，任务完成"}',
                model="mock-judge",
                usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                stop_reason="end_turn",
            )
        else:
            # main agent 调用
            return LLMResponse(
                content="headless test ok",
                model="mock-main",
                usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
                stop_reason="end_turn",
            )

    return MockLLM(callable=generate)


def _patch_agent_components(monkeypatch, mock_llm: MockLLM):
    """patch client.core.agent 的 LLMPoolGateway / ToolRegistry / HttpClientToolExecutor。

    让 headless_runner 函数内 import 的组件使用 mock 版本。
    """
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
    """patch memory manager + todos store，捕获写入。"""
    # memory manager
    mock_mem_mgr = MagicMock()
    mock_mem_mgr.set = MagicMock(return_value={"status": "ok", "key": "test"})
    capture["memory_set"] = mock_mem_mgr.set
    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mem_mgr)

    # todos store
    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(return_value={"id": "wip_e2e_001", "status": "active"})
    mock_store.update_wip = MagicMock(return_value={"id": "wip_e2e_001", "status": "completed"})
    capture["wip_create"] = mock_store.create_wip
    capture["wip_update"] = mock_store.update_wip

    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)


def _patch_loop_manager_pause(monkeypatch, capture: dict):
    """patch loop_manager.pause_task，捕获调用。"""
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(return_value=True)
    capture["pause_task"] = mock_mgr.pause_task
    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)


# ============================================================================
# 测试用例 1：完整 E2E 流程（success 路径）
# ============================================================================


def test_e2e_one_shot_success_full_flow(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: 一次性 headless session 完整流程（success 路径）。

    流程：
    1. 配置 trigger_text="回复'headless test ok'即可，不要调任何工具"
    2. MockLLM 根据调用方返回不同响应（main / judge）
    3. 调 HeadlessSessionAction.execute()
    4. 验证：
       - 返回值 success=True
       - main_session_id + judge_session_id 都存在
       - 主会话 mode="headless"，judge 会话 mode="headless_judge"
       - 主会话 assistant 消息含 "headless test ok"
       - judge 会话最后一条 assistant 消息含 JSON verdict
       - memory 记录含 main_session_id / judge_session_id / judge_verdict / judge_reason
       - wip 记录含 status=completed
       - one_shot=True 时调 pause_task
    """
    mock_llm = _make_mock_llm()
    _patch_agent_components(monkeypatch, mock_llm)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    # 配置一次性 headless session
    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复'headless test ok'即可，不要调任何工具",
            "one_shot": True,
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # ===== 验证返回值 =====
    assert result["success"] is True, f"expected success, got: {result}"
    assert "main_session_id" in result
    assert "judge_session_id" in result
    assert result["judge_verdict"] == "success"
    assert "headless test ok" in result["judge_reason"]

    main_session_id = result["main_session_id"]
    judge_session_id = result["judge_session_id"]
    assert main_session_id.startswith("headless-")
    assert judge_session_id.startswith("headless-judge-")
    assert main_session_id != judge_session_id

    # ===== 验证 sessions 表（mode 字段）=====
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            # 主会话 mode="headless"
            main_session = await verify_store.get_session(main_session_id)
            assert main_session is not None, f"主会话 {main_session_id} 不存在"
            assert main_session.mode == "headless", f"主会话 mode={main_session.mode}"

            # judge 会话 mode="headless_judge"
            judge_session = await verify_store.get_session(judge_session_id)
            assert judge_session is not None, f"judge 会话 {judge_session_id} 不存在"
            assert judge_session.mode == "headless_judge", f"judge 会话 mode={judge_session.mode}"

            # ===== 验证主会话 assistant 消息含 "headless test ok" =====
            main_messages = await verify_store.load_messages(main_session_id)
            main_assistant_msgs = [m for m in main_messages if m.role == "assistant"]
            assert len(main_assistant_msgs) >= 1, "主会话无 assistant 消息"
            main_last_assistant = main_assistant_msgs[-1]
            main_content = main_last_assistant.content
            if isinstance(main_content, list):
                main_content = " ".join(
                    p.get("text", "") for p in main_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            assert "headless test ok" in main_content, \
                f"主会话 assistant 消息不含 'headless test ok': {main_content!r}"

            # ===== 验证 judge 会话最后一条 assistant 消息含 JSON verdict =====
            judge_messages = await verify_store.load_messages(judge_session_id)
            judge_assistant_msgs = [m for m in judge_messages if m.role == "assistant"]
            assert len(judge_assistant_msgs) >= 1, "judge 会话无 assistant 消息"
            judge_last_assistant = judge_assistant_msgs[-1]
            judge_content = judge_last_assistant.content
            if isinstance(judge_content, list):
                judge_content = " ".join(
                    p.get("text", "") for p in judge_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            # 解析 JSON verdict
            judge_data = json.loads(judge_content)
            assert judge_data["verdict"] == "success"
            assert "reason" in judge_data

        asyncio.run(verify())
    finally:
        verify_store.close()

    # ===== 验证 memory 记录 =====
    assert capture["memory_set"].call_count == 1
    mem_call_args = capture["memory_set"].call_args
    mem_key = mem_call_args[0][0] if mem_call_args[0] else mem_call_args[1].get("key")
    mem_data = mem_call_args[0][1] if len(mem_call_args[0]) > 1 else mem_call_args[1].get("data")
    assert mem_key.startswith("headless_session_")
    assert mem_data["main_session_id"] == main_session_id
    assert mem_data["judge_session_id"] == judge_session_id
    assert mem_data["judge_verdict"] == "success"
    assert "judge_reason" in mem_data
    assert "trigger_text" in mem_data

    # ===== 验证 wip 记录 =====
    assert capture["wip_create"].call_count == 1
    assert capture["wip_update"].call_count == 1
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "completed"
    assert update_data["progress"] == 100

    # ===== 验证 one_shot pause_task =====
    assert capture["pause_task"].call_count == 1
    pause_args = capture["pause_task"].call_args
    assert pause_args[0][0] == "headless_session.run"


# ============================================================================
# 测试用例 2：client 进程不在线时后端仍能跑
# ============================================================================


def test_e2e_runs_without_client_process(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: client 进程不在线时后端仍能跑。

    验证点：
    - HeadlessSessionAction.execute() 不依赖任何 client 进程
    - 所有 LLM 调用通过 MockLLM（模拟后端 LLM Pool）
    - 所有工具调用通过 MockToolExecutor（模拟后端工具）
    - EventStore 写入 temp DB（后端进程内）
    - memory + wip 写入通过 mock（后端进程内）
    - 全程无 client 进程交互

    本测试 inherent 验证 client 不在线：
    - 不启动 client GUI
    - 不连接 client 进程
    - 不依赖 client 端任何组件
    - action.execute() 在后端 event loop 中跑完
    """
    mock_llm = _make_mock_llm()
    _patch_agent_components(monkeypatch, mock_llm)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复 ok 即可",
            "one_shot": False,  # 周期性任务（不 pause）
            "max_iterations": 3,
            "max_budget_usd": 0.3,
            "wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # 验证成功执行（不依赖 client 进程）
    assert result["success"] is True, f"client 不在线时执行失败: {result}"
    assert result["judge_verdict"] == "success"

    # 验证主会话 + judge 会话都创建（后端自主完成）
    main_session_id = result["main_session_id"]
    judge_session_id = result["judge_session_id"]
    assert main_session_id.startswith("headless-")
    assert judge_session_id.startswith("headless-judge-")

    # 验证 one_shot=False 时不调 pause_task（周期性任务继续）
    assert capture["pause_task"].call_count == 0

    # 验证 memory + wip 写入（后端自主留档）
    assert capture["memory_set"].call_count == 1
    assert capture["wip_create"].call_count == 1
    assert capture["wip_update"].call_count == 1


# ============================================================================
# 测试用例 3：judge verdict=failed 时整体 failed
# ============================================================================


def test_e2e_judge_failed_overall_failed(tmp_db_path, monkeypatch):
    """Ticket 06 E2E: judge verdict=failed → 整体 failed → wip status=failed。

    MockLLM 配置：
    - main agent 返回 "我无法完成这个任务"
    - judge agent 返回 {"verdict": "failed", "reason": "..."}
    """
    def _extract_full_text(request: LLMRequest) -> str:
        """提取 request 中所有可见文本（system + 所有 messages）。"""
        parts: list[str] = [request.system or ""]
        for msg in request.messages:
            content = msg.content
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
        return "\n".join(parts)

    def generate(request: LLMRequest) -> LLMResponse:
        full_text = _extract_full_text(request)

        if "你是任务成功判定" in full_text or "【判定规则】" in full_text:
            # judge agent 调用（JUDGE_PROMPT_TEMPLATE 唯一标记）
            return LLMResponse(
                content='{"verdict": "failed", "reason": "主 agent 明确报告无法完成"}',
                model="mock-judge",
                usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                stop_reason="end_turn",
            )
        else:
            return LLMResponse(
                content="我无法完成这个任务",
                model="mock-main",
                usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
                stop_reason="end_turn",
            )

    mock_llm = MockLLM(callable=generate)
    _patch_agent_components(monkeypatch, mock_llm)

    capture: dict = {}
    _patch_memory_and_wip(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复 ok 即可",
            "one_shot": True,
            "max_iterations": 3,
            "max_budget_usd": 0.3,
            "wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        },
        "source_segment": "headless_session",
    }

    action = HeadlessSessionAction()

    async def run():
        return await action.execute(context)

    result = asyncio.run(run())

    # judge verdict=failed → 整体 failed
    assert result["success"] is False
    assert result["judge_verdict"] == "failed"

    # wip status=failed
    update_data = capture["wip_update"].call_args[0][1]
    assert update_data["status"] == "failed"

    # memory 记录 judge_verdict=failed
    mem_data = capture["memory_set"].call_args[0][1]
    assert mem_data["judge_verdict"] == "failed"

    # one_shot 仍调 pause_task（无论成功失败）
    assert capture["pause_task"].call_count == 1
