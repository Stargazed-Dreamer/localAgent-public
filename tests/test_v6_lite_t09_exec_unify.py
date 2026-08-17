"""T02-exec-unify 测试：3 分钟唤醒 + terminal 生命周期管理。

验证 spec Decision 6 + 11：
- D6: exec_python 包装 3 分钟唤醒（SessionRunner 识别 exec_python）
- D11: terminal 生命周期三层保障（session 结束自动 kill）

测试策略（与 test_v6_lite_t01.py 一致，用 asyncio.run 不用 pytest-asyncio）：
- Mock terminal_killer / terminal_inspector，避免真起子进程
- 用 MockLLM + MockToolExecutor 模拟工具调用
- 验证 terminal 追踪 + 清理 + 唤醒逻辑
"""
import asyncio
import json
import time

from client.core.agent.mock_llm import MockLLM
from client.core.agent.runner import SessionRunner
from client.core.agent.types import (
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    ToolCall,
    ToolResult,
)

# ============================================================================
# Test helpers
# ============================================================================

class MockToolExecutor:
    """记录工具调用 + 返回预设结果的 mock 执行器。"""

    def __init__(self, results: dict[str, str] | None = None):
        self.results = results or {}
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        self.calls.append(tool_call)
        content = self.results.get(tool_call.name, '{"success": true}')
        return ToolResult(
            tool_call_id=tool_call.id,
            content=content,
            is_error=False,
            created_at=time.time(),
        )


class MockEventStore:
    """最小化 EventStore mock，只实现 runner 用到的方法。"""

    def __init__(self):
        self.session_status = "streaming"
        self.messages: list[Message] = []
        self.tool_calls: list[ToolCall] = []
        self.tool_results: list[ToolResult] = []
        self.events: list[dict] = []
        self._next_seq = 0

    async def update_session_status(self, sid, status):
        self.session_status = status

    async def append_message(self, sid, msg):
        # 分配 seq 并返回（模拟真实 EventStore 行为）
        self._next_seq += 1
        # Message 是 dataclass，创建带 seq 的新实例
        from dataclasses import replace
        appended = replace(msg, seq=self._next_seq)
        self.messages.append(appended)
        return appended

    async def append_tool_call(self, sid, tc):
        self.tool_calls.append(tc)

    async def append_tool_result_message(self, sid, result):
        self.tool_results.append(result)

    async def update_tool_call_status(self, tc_id, status, **kwargs):
        pass

    async def append_event(self, sid, event_type, data, **kwargs):
        self._next_seq += 1
        self.events.append({"type": event_type, "data": data, "seq": self._next_seq})
        return self._next_seq

    async def invalidate_events(self, sid, seqs):
        """T01 streaming 事件 invalidate（mock：no-op，仅签名兼容）。"""
        pass

    async def finalize_session(self, sid):
        self.session_status = "finalized"

    async def load_messages(self, sid):
        return list(self.messages)


# ============================================================================
# T02 tests（用 asyncio.run，不用 pytest-asyncio）
# ============================================================================

def test_exec_python_terminal_tracked():
    """exec_python 返回 terminal_id 后加入 _session_terminals，session 结束时 kill。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_test_001",
            "status": "running", "pid": 12345,
        }),
    })
    store = MockEventStore()
    killed_terminals: list[str] = []

    async def killer(tid: str) -> bool:
        killed_terminals.append(tid)
        return True

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_killer=killer,
        )
        runner = SessionRunner(deps, RunnerConfig(session_id="test1"))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert "term_test_001" in killed_terminals


def test_non_exec_python_not_tracked():
    """非 exec_python 工具不加入 terminal 追踪。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "memory_get", "arguments": '{"key": "test"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={"memory_get": '{"value": "test"}'})
    store = MockEventStore()
    killed_terminals: list[str] = []

    async def killer(tid: str) -> bool:
        killed_terminals.append(tid)
        return True

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_killer=killer,
        )
        runner = SessionRunner(deps, RunnerConfig(session_id="test2"))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert len(killed_terminals) == 0


def test_cleanup_terminals_best_effort():
    """kill 失败不抛异常（best-effort）。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_fail_kill", "status": "running",
        }),
    })
    store = MockEventStore()

    async def failing_killer(tid: str) -> bool:
        raise RuntimeError("kill failed")

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_killer=failing_killer,
        )
        runner = SessionRunner(deps, RunnerConfig(session_id="test3"))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"


def test_short_task_no_wakeup():
    """短任务：inspect 立即返回 status=done，不唤醒 LLM。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_short", "status": "running",
        }),
    })
    store = MockEventStore()
    inspect_calls: list[tuple] = []

    async def inspector(tid: str, timeout: int) -> dict:
        inspect_calls.append((tid, timeout))
        return {
            "success": True, "tid": tid, "status": "done",
            "exit_code": 0, "elapsed": 0.5,
            "stdout_so_far": "1\n", "stderr_so_far": "",
        }

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_inspector=inspector,
        )
        runner = SessionRunner(deps, RunnerConfig(
            session_id="test_short", long_tool_first_check_secs=180,
        ))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert len(inspect_calls) == 1
    assert inspect_calls[0] == ("term_short", 180)
    assert llm.call_count == 2  # exec_python + finalize，无唤醒


def test_long_task_wakes_llm():
    """长任务：3 分钟到子进程仍 running → 唤醒 LLM 决策。"""
    llm = MockLLM(script=[
        # 1. exec_python 工具调用
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "import time; time.sleep(999)"}'},
        }]),
        # 2. 唤醒后 LLM 决定 exec_kill
        LLMResponse(tool_calls=[{
            "id": "tc_wakeup", "type": "function",
            "function": {"name": "exec_kill", "arguments": '{"tid": "term_long"}'},
        }]),
        # 3. finalize
        LLMResponse(content="killed"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_long", "status": "running",
        }),
        "exec_kill": json.dumps({"success": True, "tid": "term_long", "status": "killed"}),
    })
    store = MockEventStore()

    async def inspector(tid: str, timeout: int) -> dict:
        return {
            "success": True, "tid": tid, "status": "running",
            "exit_code": None, "elapsed": 180.0,
            "stdout_so_far": "still running...\n", "stderr_so_far": "",
        }

    async def killer(tid: str) -> bool:
        return True

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_inspector=inspector, terminal_killer=killer,
        )
        runner = SessionRunner(deps, RunnerConfig(
            session_id="test_long", long_tool_first_check_secs=1,
        ))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert llm.call_count == 3  # exec_python + wakeup + finalize
    assert len(executor.calls) == 2
    assert executor.calls[0].name == "exec_python"
    assert executor.calls[1].name == "exec_kill"


def test_wakeup_disabled():
    """long_tool_first_check_secs=0 → 禁用唤醒，exec_python 直接返回。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_disabled", "status": "running",
        }),
    })
    store = MockEventStore()
    inspect_calls: list[tuple] = []

    async def inspector(tid: str, timeout: int) -> dict:
        inspect_calls.append((tid, timeout))
        return {"status": "done", "stdout_so_far": "1\n", "stderr_so_far": ""}

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_inspector=inspector,
        )
        runner = SessionRunner(deps, RunnerConfig(
            session_id="test_disabled", long_tool_first_check_secs=0,
        ))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert len(inspect_calls) == 0


def test_inspector_none_skip_wakeup():
    """terminal_inspector=None → 跳过唤醒（向后兼容）。"""
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_no_inspector", "status": "running",
        }),
    })
    store = MockEventStore()

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
        )
        runner = SessionRunner(deps, RunnerConfig(session_id="test_no_inspector"))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    assert llm.call_count == 2


def test_extract_terminal_id_valid():
    runner = SessionRunner(
        RunnerDeps(llm_gateway=MockLLM()),
        RunnerConfig(session_id="test"),
    )
    result = ToolResult(
        tool_call_id="tc1",
        content='{"success": true, "terminal_id": "term_abc123", "status": "running"}',
    )
    assert runner._extract_terminal_id(result) == "term_abc123"


def test_extract_terminal_id_invalid_json():
    runner = SessionRunner(
        RunnerDeps(llm_gateway=MockLLM()),
        RunnerConfig(session_id="test"),
    )
    result = ToolResult(tool_call_id="tc1", content="not json")
    assert runner._extract_terminal_id(result) is None


def test_extract_terminal_id_missing():
    runner = SessionRunner(
        RunnerDeps(llm_gateway=MockLLM()),
        RunnerConfig(session_id="test"),
    )
    result = ToolResult(
        tool_call_id="tc1",
        content='{"success": true, "status": "running"}',
    )
    assert runner._extract_terminal_id(result) is None


# ============================================================================
# v16 inline-wait 短路测试（spec Proof 5）
# ============================================================================

def test_exec_python_done_skips_inspector():
    """v16: exec_python 返回 status=done → _execute_with_wakeup 不调 inspector，直接用 result。

    后端内联等待完成时 exec_python 已含完整 stdout/stderr/exit_code，
    SessionRunner 应短路跳过 inspect 包装（省一次 HTTP 往返）。
    """
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "print(1+1)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    # 模拟 v16 后端内联等待完成返回的 done 响应
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True,
            "terminal_id": "term_inline_done",
            "status": "done",
            "stdout": "2\n",
            "stderr": "",
            "exit_code": 0,
            "elapsed": 0.05,
        }),
    })
    store = MockEventStore()
    inspect_calls: list[tuple] = []

    async def inspector(tid: str, timeout: int) -> dict:
        inspect_calls.append((tid, timeout))
        return {"status": "done", "stdout_so_far": "should not be used", "stderr_so_far": ""}

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_inspector=inspector,
        )
        runner = SessionRunner(deps, RunnerConfig(
            session_id="test_inline_done", long_tool_first_check_secs=180,
        ))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    # 关键断言：inspector 未被调用（done 短路）
    assert len(inspect_calls) == 0
    assert llm.call_count == 2  # exec_python + finalize，无唤醒


def test_exec_python_running_still_uses_inspector():
    """v16 回归：exec_python 返回 status=running → 走原 inspect 包装逻辑（短路不影响 running 路径）。

    确保短路只对 status=done 生效，status=running 仍调 inspector。
    """
    llm = MockLLM(script=[
        LLMResponse(tool_calls=[{
            "id": "tc1", "type": "function",
            "function": {"name": "exec_python", "arguments": '{"code": "import time; time.sleep(999)"}'},
        }]),
        LLMResponse(content="done"),
    ])
    executor = MockToolExecutor(results={
        "exec_python": json.dumps({
            "success": True, "terminal_id": "term_still_running", "status": "running",
        }),
    })
    store = MockEventStore()
    inspect_calls: list[tuple] = []

    async def inspector(tid: str, timeout: int) -> dict:
        inspect_calls.append((tid, timeout))
        return {
            "success": True, "tid": tid, "status": "done",
            "exit_code": 0, "elapsed": 0.5,
            "stdout_so_far": "done\n", "stderr_so_far": "",
        }

    async def run():
        deps = RunnerDeps(
            llm_gateway=llm, event_store=store, tool_executor=executor,
            terminal_inspector=inspector,
        )
        runner = SessionRunner(deps, RunnerConfig(
            session_id="test_running", long_tool_first_check_secs=180,
        ))
        return await runner.run()

    outcome = asyncio.run(run())
    assert outcome.status == "completed"
    # 关键断言：status=running 时 inspector 被调用（短路未生效）
    assert len(inspect_calls) == 1
    assert inspect_calls[0] == ("term_still_running", 180)
