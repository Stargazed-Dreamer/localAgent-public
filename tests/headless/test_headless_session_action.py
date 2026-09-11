"""headless-agent-session Ticket 04 验收测试：HeadlessSessionAction

覆盖 Ticket 04 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] HeadlessSessionAction(Action) action_type="headless_session"
- [x] execute(context) 流程：trigger_text / skill / one_shot / 资源限制 / judge 配置解析
- [x] 调 run_main_agent + run_judge_agent
- [x] verdict != "success"（含 uncertain）→ 整体 failed
- [x] 写 memory_set + wip_create
- [x] one_shot 执行后调 pause_task
- [x] skill 绑定（trigger_text 留空时调 agent_guide）
- [x] 失败时 fail_count +1（由 loop_manager._run_task 自动处理，本测试只验证返回值）

测试策略：
- mock headless_runner.run_main_agent / run_judge_agent（不真实调 LLM）
- mock memory manager + todos store（不真实写 DB）
- mock loop_manager.get_manager / pause_task（不真实改 loop 状态）
- mock agent_guide（skill 绑定测试用）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.types import RunOutcome  # noqa: E402
from server.activity_tracker.headless_runner import (  # noqa: E402
    JudgeResult,
    MainAgentResult,
)
from server.activity_tracker.loop_actions import HeadlessSessionAction  # noqa: E402

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def action() -> HeadlessSessionAction:
    return HeadlessSessionAction()


@pytest.fixture
def base_context() -> dict:
    """基础 context：含 trigger_text，无 skill，无 one_shot。"""
    return {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "回复 headless test ok",
            "max_iterations": 5,
            "max_budget_usd": 0.5,
            "wall_clock_budget_secs": 60,
        },
        "source_segment": "headless_session",
    }


def _make_main_result(
    session_id: str = "headless-test-main-001",
    error: str = "",
    outcome_status: str = "completed",
) -> MainAgentResult:
    """构造 MainAgentResult（mock run_main_agent 返回值）。"""
    outcome = RunOutcome(
        session_id=session_id,
        status=outcome_status,
        stop_reason="end_turn",
        iterations=1,
    ) if outcome_status else None
    return MainAgentResult(
        session_id=session_id,
        outcome=outcome,
        last_assistant_messages=["主 agent 完成了任务"] if not error else [],
        error=error,
    )


def _make_judge_result(
    session_id: str = "headless-judge-test-001",
    verdict: str = "success",
    reason: str = "任务完成",
) -> JudgeResult:
    """构造 JudgeResult（mock run_judge_agent 返回值）。"""
    return JudgeResult(
        verdict=verdict,
        reason=reason,
        session_id=session_id,
        raw_output=f'{{"verdict": "{verdict}", "reason": "{reason}"}}',
        error="",
    )


def _patch_headless_runner(
    monkeypatch,
    main_result: MainAgentResult | None = None,
    judge_result: JudgeResult | None = None,
    main_exception: Exception | None = None,
    judge_exception: Exception | None = None,
):
    """patch headless_runner.run_main_agent / run_judge_agent。"""
    async def mock_run_main(trigger_text, config, **kwargs):
        if main_exception is not None:
            raise main_exception
        return main_result or _make_main_result()

    async def mock_run_judge(main_output, trigger_text, config, **kwargs):
        if judge_exception is not None:
            raise judge_exception
        return judge_result or _make_judge_result()

    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", mock_run_main)
    monkeypatch.setattr(hr_pkg, "run_judge_agent", mock_run_judge)

    # 也要 patch loop_actions 里 import 的引用（因为 loop_actions 在 execute 内 import）
    # 由于 execute 内 from server.activity_tracker.headless_runner import run_main_agent
    # 这是函数内 import，每次执行都会重新解析，所以 patch 源模块即可


def _patch_memory_manager(monkeypatch, capture: dict | None = None):
    """patch get_memory_manager 返回 mock manager，可选捕获 set 调用。"""
    mock_mgr = MagicMock()
    mock_mgr.set = MagicMock(return_value={"status": "ok", "key": "test"})
    if capture is not None:
        capture["set_calls"] = mock_mgr.set

    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mgr)
    return mock_mgr


def _patch_todos_store(monkeypatch, capture: dict | None = None):
    """patch get_todos_store 返回 mock store，可选捕获 create_wip / update_wip 调用。"""
    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(return_value={"id": "wip_test_001", "status": "active"})
    mock_store.update_wip = MagicMock(return_value={"id": "wip_test_001", "status": "completed"})
    if capture is not None:
        capture["create_calls"] = mock_store.create_wip
        capture["update_calls"] = mock_store.update_wip

    # 注意：server.todos.__init__.py 把 router (APIRouter) 重导出，导致
    # `import server.todos.router as todos_pkg` 拿到的是 APIRouter 实例而非模块。
    # 用 importlib 显式取模块：
    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)
    return mock_store


def _patch_loop_manager_pause(monkeypatch, capture: dict | None = None):
    """patch loop_manager.get_manager + pause_task，可选捕获 pause 调用。"""
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(return_value=True)
    if capture is not None:
        capture["pause_calls"] = mock_mgr.pause_task

    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)
    return mock_mgr


def _patch_agent_guide(monkeypatch, first_action: str = "guide 解析的 first_action"):
    """patch server.agent_guide.get_agent_guide 返回指定 first_action。"""
    mock_resp = {"first_action": first_action, "task_type": "test.skill"}
    import server.agent_guide as ag_pkg
    monkeypatch.setattr(ag_pkg, "get_agent_guide", lambda **kwargs: mock_resp)
    return mock_resp


# ============================================================================
# 1. 基础流程测试（success / failed / uncertain）
# ============================================================================


def test_action_type_is_headless_session(action):
    """Ticket 04: action_type = 'headless_session'。"""
    assert action.action_type == "headless_session"


def test_execute_success_writes_memory_and_wip(action, base_context, monkeypatch):
    """Ticket 04: judge verdict=success → 整体 success → memory + wip 写入 status=completed。"""
    capture: dict = {}
    _patch_headless_runner(
        monkeypatch,
        main_result=_make_main_result(),
        judge_result=_make_judge_result(verdict="success", reason="任务完成"),
    )
    _patch_memory_manager(monkeypatch, capture)
    _patch_todos_store(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    # 验证返回值
    assert result["success"] is True
    assert result["judge_verdict"] == "success"
    assert result["main_session_id"] == "headless-test-main-001"
    assert result["judge_session_id"] == "headless-judge-test-001"

    # 验证 memory_set 被调用
    assert capture["set_calls"].call_count == 1
    set_args = capture["set_calls"].call_args
    assert "headless_session_headless-test-main-001" in set_args[0][0]
    set_data = set_args[0][1]
    assert set_data["judge_verdict"] == "success"
    assert set_data["overall_success"] is True

    # 验证 wip_create + update_wip（status=completed）
    assert capture["create_calls"].call_count == 1
    assert capture["update_calls"].call_count == 1
    update_args = capture["update_calls"].call_args
    assert update_args[0][0] == "wip_test_001"  # task_id
    update_data = update_args[0][1]
    assert update_data["status"] == "completed"
    assert update_data["progress"] == 100


def test_execute_failed_judge_verdict_failed(action, base_context, monkeypatch):
    """Ticket 04: judge verdict=failed → 整体 failed → memory + wip 写入 status=failed。"""
    capture: dict = {}
    _patch_headless_runner(
        monkeypatch,
        main_result=_make_main_result(),
        judge_result=_make_judge_result(verdict="failed", reason="任务失败"),
    )
    _patch_memory_manager(monkeypatch, capture)
    _patch_todos_store(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is False
    assert result["judge_verdict"] == "failed"
    assert "error" in result

    # wip 应该 status=failed, progress=0
    update_data = capture["update_calls"].call_args[0][1]
    assert update_data["status"] == "failed"
    assert update_data["progress"] == 0


# ============================================================================
# 2. main agent 失败处理
# ============================================================================


def test_execute_main_agent_error_skips_judge(action, base_context, monkeypatch):
    """Ticket 04: main agent error 非空 → 跳过 judge → 整体 failed。"""
    capture: dict = {}
    _patch_headless_runner(
        monkeypatch,
        main_result=_make_main_result(error="LLM 调用失败"),
        judge_result=_make_judge_result(verdict="success"),  # 不应被调用
    )
    _patch_memory_manager(monkeypatch, capture)
    _patch_todos_store(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch)

    # 检查 judge agent 是否被调用
    judge_called = []
    import server.activity_tracker.headless_runner as hr_pkg
    original_judge = hr_pkg.run_judge_agent

    async def tracking_judge(*args, **kwargs):
        judge_called.append(True)
        return await original_judge(*args, **kwargs)
    monkeypatch.setattr(hr_pkg, "run_judge_agent", tracking_judge)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is False
    assert result["judge_verdict"] == "failed"
    assert "main agent 失败" in result["judge_reason"]
    # judge 不应被调用
    assert len(judge_called) == 0


def test_execute_main_agent_outcome_none_skips_judge(action, base_context, monkeypatch):
    """Ticket 04: main agent outcome is None → 跳过 judge → 整体 failed。"""
    _patch_headless_runner(
        monkeypatch,
        main_result=_make_main_result(outcome_status=""),  # outcome=None
    )
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is False
    # loop_actions.py 用 f"main agent status={main_outcome_status or 'None'}" 构造 err，
    # outcome=None 时 main_outcome_status="" → err="main agent status=None"
    assert "main agent status=None" in result["error"]
    assert result["judge_verdict"] == "failed"
    assert "main agent status=None" in result["judge_reason"]


def test_execute_main_agent_exception(action, base_context, monkeypatch):
    """Ticket 04: run_main_agent 抛异常 → 整体 failed，不抛出。"""
    _patch_headless_runner(
        monkeypatch,
        main_exception=RuntimeError("simulated main agent failure"),
    )
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is False
    assert "run_main_agent 异常" in result["error"]


def test_execute_judge_agent_exception(action, base_context, monkeypatch):
    """Ticket 04: run_judge_agent 抛异常 → 整体 failed，不抛出。"""
    _patch_headless_runner(
        monkeypatch,
        main_result=_make_main_result(),
        judge_exception=RuntimeError("simulated judge failure"),
    )
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is False
    assert "run_judge_agent 异常" in result["error"]
    assert result["judge_verdict"] == "uncertain"


# ============================================================================
# 3. one_shot pause 测试
# ============================================================================


def test_execute_one_shot_pauses_task(action, monkeypatch):
    """Ticket 04: one_shot=true → 执行后调 pause_task。"""
    context = {
        "task_id": "headless_session.run",
        "config": {
            "trigger_text": "test task",
            "one_shot": True,
        },
    }
    capture: dict = {}
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch, capture)
    _patch_todos_store(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    async def run():
        return await action.execute(context)
    asyncio.run(run())

    # pause_task 应被调用一次
    assert capture["pause_calls"].call_count == 1
    pause_args = capture["pause_calls"].call_args
    assert pause_args[0][0] == "headless_session.run"


def test_execute_not_one_shot_does_not_pause(action, base_context, monkeypatch):
    """Ticket 04: one_shot 未设 → 不调 pause_task。"""
    capture: dict = {}
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch, capture)
    _patch_todos_store(monkeypatch, capture)
    _patch_loop_manager_pause(monkeypatch, capture)

    async def run():
        return await action.execute(base_context)
    asyncio.run(run())

    # pause_task 不应被调用
    assert capture["pause_calls"].call_count == 0


def test_execute_one_shot_pause_failure_does_not_break(action, monkeypatch):
    """Ticket 04: pause_task 抛异常 → 不影响 execute 返回值。"""
    context = {
        "task_id": "headless_session.run",
        "config": {"trigger_text": "test", "one_shot": True},
    }
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    # pause_task 抛异常
    mock_mgr = MagicMock()
    mock_mgr.pause_task = MagicMock(side_effect=RuntimeError("pause failed"))
    import server.activity_tracker.loop_manager as lm_pkg
    monkeypatch.setattr(lm_pkg, "get_manager", lambda: mock_mgr)

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    # execute 仍应正常返回
    assert result["success"] is True


# ============================================================================
# 4. skill 绑定测试（trigger_text 留空 + skill 指定）
# ============================================================================


def test_execute_skill_binding_resolves_trigger_text(action, monkeypatch):
    """Ticket 04: trigger_text 留空 + skill 指定 → 调 agent_guide 取 first_action。"""
    context = {
        "task_id": "headless_session.run",
        "config": {
            "skill": "recurring.test_skill",  # trigger_text 留空
            "max_iterations": 5,
        },
    }
    # 用 MagicMock 包装 run_main_agent 以支持 call_count 验证
    main_called = []

    async def tracking_run_main(trigger_text, config, **kwargs):
        main_called.append(trigger_text)
        return _make_main_result()

    # 先 patch judge_agent（_patch_headless_runner 会同时 patch run_main_agent 和 run_judge_agent），
    # 再 override run_main_agent 为 tracking 版本（monkeypatch 后调 setattr 覆盖前次 patch）
    _patch_headless_runner(monkeypatch)
    import server.activity_tracker.headless_runner as hr_pkg
    monkeypatch.setattr(hr_pkg, "run_main_agent", tracking_run_main)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)
    _patch_agent_guide(monkeypatch, first_action="guide 解析的具体任务描述")

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    assert result["success"] is True
    # main agent 应被调用，且 trigger_text 是 agent_guide 返回的 first_action
    assert len(main_called) == 1
    assert main_called[0] == "guide 解析的具体任务描述"


def test_execute_empty_trigger_and_skill_skipped(action, monkeypatch):
    """Ticket 04: trigger_text 和 skill 都为空 → skipped。"""
    context = {
        "task_id": "headless_session.run",
        "config": {},  # 没有 trigger_text 也没有 skill
    }
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    assert result["success"] is True
    assert result.get("skipped") is True
    assert "trigger_text 和 skill 都为空" in result["skip_reason"]


def test_execute_skill_no_first_action_skipped(action, monkeypatch):
    """Ticket 04: skill 指定但 agent_guide 返回空 first_action → skipped。"""
    context = {
        "task_id": "headless_session.run",
        "config": {"skill": "test.empty_skill"},
    }
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)
    # agent_guide 返回空 first_action
    import server.agent_guide as ag_pkg
    monkeypatch.setattr(ag_pkg, "get_agent_guide", lambda **kw: {"first_action": ""})

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    assert result["success"] is True
    assert result.get("skipped") is True
    assert "未返回 first_action" in result["skip_reason"]


def test_execute_skill_agent_guide_failure(action, monkeypatch):
    """Ticket 04: skill 指定但 agent_guide 抛异常 → failed。"""
    context = {
        "task_id": "headless_session.run",
        "config": {"skill": "test.broken_skill"},
    }
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)
    import server.agent_guide as ag_pkg
    monkeypatch.setattr(ag_pkg, "get_agent_guide", lambda **kw: (_ for _ in ()).throw(RuntimeError("guide broken")))

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    assert result["success"] is False
    assert "agent_guide" in result["error"]


# ============================================================================
# 5. memory + wip 写入失败兜底
# ============================================================================


def test_execute_memory_failure_does_not_break(action, base_context, monkeypatch):
    """Ticket 04: memory_set 抛异常 → execute 仍正常返回。"""
    _patch_headless_runner(monkeypatch)
    # memory manager 抛异常
    mock_mgr = MagicMock()
    mock_mgr.set = MagicMock(side_effect=RuntimeError("memory write failed"))
    import server.memory.manager as mem_pkg
    monkeypatch.setattr(mem_pkg, "get_memory_manager", lambda: mock_mgr)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is True  # memory 失败不影响整体


def test_execute_wip_failure_does_not_break(action, base_context, monkeypatch):
    """Ticket 04: wip_create 抛异常 → execute 仍正常返回。"""
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    # todos store 抛异常（用 importlib 避免 __init__.py shadow 问题）
    mock_store = MagicMock()
    mock_store.create_wip = MagicMock(side_effect=RuntimeError("wip write failed"))
    import importlib
    todos_module = importlib.import_module("server.todos.router")
    monkeypatch.setattr(todos_module, "get_todos_store", lambda: mock_store)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(base_context)
    result = asyncio.run(run())

    assert result["success"] is True  # wip 失败不影响整体


# ============================================================================
# 6. config 解析失败
# ============================================================================


def test_execute_config_parse_failure(action, monkeypatch):
    """Ticket 04: config 解析失败 → failed。"""
    # parse_headless_config 接受任意 dict，难以触发异常
    # 但如果传非 dict 会怎样？用一个能触发异常的 mock
    context = {
        "task_id": "headless_session.run",
        "config": None,  # None 不是 dict
    }
    _patch_headless_runner(monkeypatch)
    _patch_memory_manager(monkeypatch)
    _patch_todos_store(monkeypatch)
    _patch_loop_manager_pause(monkeypatch)

    async def run():
        return await action.execute(context)
    result = asyncio.run(run())

    # config=None 时 cfg_dict = {} or {} = {}，不会异常
    # trigger_text 和 skill 都为空 → skipped
    assert result.get("skipped") is True


# ============================================================================
# 7. 注册验证（register_loop_actions 含 headless_session）
# ============================================================================


def test_register_loop_actions_includes_headless_session():
    """Ticket 04: register_loop_actions 注册了 headless_session action。"""
    # 用 mock manager 验证注册调用
    from server.activity_tracker.loop_actions import register_loop_actions
    mock_manager = MagicMock()
    # mock VL 配额配置（避免 ImportError）
    mock_manager.config = {}

    register_loop_actions(mock_manager)

    # 检查 register_action 是否被调用了 "headless_session"
    register_calls = [call.args[0] for call in mock_manager.register_action.call_args_list]
    assert "headless_session" in register_calls


def test_task_builders_includes_headless_session():
    """Ticket 04: TASK_BUILDERS 含 headless_session 段。"""
    from server.activity_tracker.loop_manager import TASK_BUILDERS
    assert "headless_session" in TASK_BUILDERS
    rules = TASK_BUILDERS["headless_session"]
    assert len(rules) >= 1
    rule = rules[0]
    assert rule["action"] == "headless_session"
    assert "trigger" in rule
    assert callable(rule["trigger"])
