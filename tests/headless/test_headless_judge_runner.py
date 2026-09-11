"""headless-agent-session Ticket 03 验收测试：judge agent + JUDGE_PROMPT_TEMPLATE

覆盖 Ticket 03 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] JUDGE_PROMPT_TEMPLATE 常量定义
- [x] run_judge_agent 函数实现
- [x] 创建 judge session（mode="headless_judge"，session_id=headless-judge-{uuid}）
- [x] judge config：max_iterations=10, max_budget_usd=0.5, wall_clock_budget_secs=300
- [x] 调 facade.start(judge_session_id, judge_prompt)
- [x] 解析 JSON verdict {"verdict": ..., "reason": ...}
- [x] 降级规则：JSON 解析失败 / judge session 失败 / max_iterations / 空输出 → uncertain

测试分组：
1. _parse_judge_verdict 纯函数测试（无 mock）
2. run_judge_agent 端到端测试（mock SessionFacade.start）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    Message,
    MockToolExecutor,
    SessionFacade,
)
from client.core.agent.types import RunOutcome  # noqa: E402
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_BASE_URL,
    HEADLESS_JUDGE_PREFIX,
    JUDGE_PROMPT_TEMPLATE,
    VALID_VERDICTS,
    HeadlessConfig,
    JudgeResult,
    _parse_judge_verdict,
    run_judge_agent,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def base_config(tmp_db_path) -> HeadlessConfig:
    """基础 judge 测试 config（资源限制用默认值）。"""
    return HeadlessConfig(
        trigger_text="测试任务",
        db_path=tmp_db_path,
        judge_max_iterations=5,
        judge_wall_clock_budget_secs=60,
    )


# ============================================================================
# 1. _parse_judge_verdict 纯函数测试（Ticket 03 acceptance）
# ============================================================================


class TestParseJudgeVerdict:
    """_parse_judge_verdict 各种输入场景。"""

    def test_valid_json_success(self):
        """合法 JSON verdict=success → 返回 success。"""
        raw = '{"verdict": "success", "reason": "任务完成"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "success"
        assert reason == "任务完成"

    def test_valid_json_failed(self):
        """合法 JSON verdict=failed → 返回 failed。"""
        raw = '{"verdict": "failed", "reason": "任务失败"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "failed"
        assert "任务失败" in reason

    def test_valid_json_uncertain(self):
        """合法 JSON verdict=uncertain → 返回 uncertain。"""
        raw = '{"verdict": "uncertain", "reason": "输出含糊"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "uncertain"
        assert "输出含糊" in reason

    def test_verdict_uppercase_normalized_to_lower(self):
        """verdict 大写自动转小写（SUCCESS → success）。"""
        raw = '{"verdict": "SUCCESS", "reason": "ok"}'
        verdict, _ = _parse_judge_verdict(raw)
        assert verdict == "success"

    def test_verdict_with_whitespace_trimmed(self):
        """verdict 含空白自动 trim。"""
        raw = '{"verdict": "  success  ", "reason": "ok"}'
        verdict, _ = _parse_judge_verdict(raw)
        assert verdict == "success"

    def test_missing_reason_field_uses_default(self):
        """缺 reason 字段时用默认值 f"verdict={verdict}"。"""
        raw = '{"verdict": "success"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "success"
        assert "success" in reason

    def test_empty_output_degrades_to_uncertain(self):
        """空 output → 降级 uncertain。"""
        verdict, reason = _parse_judge_verdict("")
        assert verdict == "uncertain"
        assert "empty" in reason.lower()

    def test_whitespace_only_output_degrades_to_uncertain(self):
        """纯空白 output → 降级 uncertain。"""
        verdict, reason = _parse_judge_verdict("   \n\t  ")
        assert verdict == "uncertain"
        assert "empty" in reason.lower()

    def test_invalid_json_degrades_to_uncertain(self):
        """非法 JSON → 降级 uncertain。"""
        raw = "this is not JSON at all"
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "uncertain"
        assert "not valid JSON" in reason

    def test_missing_verdict_field_degrades_to_uncertain(self):
        """缺 verdict 字段 → 降级 uncertain。"""
        raw = '{"reason": "no verdict here"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "uncertain"
        assert "invalid verdict" in reason.lower()

    def test_invalid_verdict_value_degrades_to_uncertain(self):
        """verdict 值不在 VALID_VERDICTS → 降级 uncertain。"""
        raw = '{"verdict": "maybe", "reason": "啥"}'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "uncertain"
        assert "invalid verdict" in reason.lower()

    def test_json_in_code_block_extracted(self):
        """JSON 包在 ```json ... ``` 中也能提取。"""
        raw = '```json\n{"verdict": "success", "reason": "ok"}\n```'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "success"

    def test_json_embedded_in_text_extracted(self):
        """JSON 嵌入在文本中（如 judge 输出有前言）也能提取。"""
        raw = '判定结果如下：\n{"verdict": "failed", "reason": "工具调用失败"}\n以上。'
        verdict, reason = _parse_judge_verdict(raw)
        assert verdict == "failed"
        assert "工具调用失败" in reason


# ============================================================================
# 2. JUDGE_PROMPT_TEMPLATE 常量测试
# ============================================================================


class TestJudgePromptTemplate:
    """JUDGE_PROMPT_TEMPLATE 常量基本验证。"""

    def test_template_contains_required_sections(self):
        """模板含任务描述/主 agent 输出/判定规则/输出格式。"""
        assert "{task}" in JUDGE_PROMPT_TEMPLATE
        assert "{main_output}" in JUDGE_PROMPT_TEMPLATE
        assert "{msg_count}" in JUDGE_PROMPT_TEMPLATE

    def test_template_contains_three_verdicts(self):
        """模板说明三种 verdict：success/failed/uncertain。"""
        assert "success" in JUDGE_PROMPT_TEMPLATE
        assert "failed" in JUDGE_PROMPT_TEMPLATE
        assert "uncertain" in JUDGE_PROMPT_TEMPLATE

    def test_template_can_be_formatted(self):
        """模板能 format 不抛 KeyError。"""
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            task="测试任务",
            msg_count=3,
            main_output="主 agent 输出",
        )
        assert "测试任务" in prompt
        assert "主 agent 输出" in prompt


# ============================================================================
# 3. run_judge_agent 端到端测试（mock SessionFacade.start）
# ============================================================================


def _make_outcome(
    session_id: str,
    status: str = "completed",
    stop_reason: str = "end_turn",
) -> RunOutcome:
    """构造 RunOutcome（用于 mock facade.start 返回值）。"""
    return RunOutcome(
        session_id=session_id,
        status=status,
        stop_reason=stop_reason,
        iterations=1,
    )


def _patch_facade_start_with_judge_output(
    monkeypatch,
    judge_response_text: str,
    outcome: RunOutcome | None = None,
    fail_with_exception: Exception | None = None,
):
    """patch SessionFacade.start 让它写一条 judge assistant 消息后返回指定 outcome。

    Args:
        monkeypatch: pytest monkeypatch fixture
        judge_response_text: judge 写入 EventStore 的 assistant 消息 content
        outcome: facade.start 返回的 RunOutcome（None 时构造默认 completed）
        fail_with_exception: 若非 None，facade.start 抛此异常
    """
    async def patched_start(self, session_id, text, *, mode="dialogue", model_override=""):
        if fail_with_exception is not None:
            raise fail_with_exception
        # 先创建 session 行（真实 facade.start 会调 create_session）
        await self._deps.event_store.create_session(
            session_id=session_id, mode=mode, title=text[:20] if text else "",
        )
        # 写一条 judge assistant 消息到 EventStore（facade 内部 store 与 deps 共享）
        await self._deps.event_store.append_message(
            session_id,
            Message(
                role="assistant",
                content=judge_response_text,
                source="assistant",
                session_id=session_id,
            ),
        )
        if outcome is None:
            return _make_outcome(session_id)
        return outcome

    monkeypatch.setattr(SessionFacade, "start", patched_start)


def _patch_gateway_registry_executor(monkeypatch):
    """patch LLMPoolGateway / ToolRegistry / HttpClientToolExecutor（不真实调 LLM/HTTP）。"""
    class MockLLMPoolGateway:
        def __init__(self, base_url=DEFAULT_BASE_URL):
            self.base_url = base_url

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


# ---------- 合法 JSON verdict 三种 ----------


def test_run_judge_agent_success(base_config, monkeypatch):
    """Ticket 03: judge 返回 verdict=success → JudgeResult.verdict=success。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "任务完成"}',
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 完成了任务"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert isinstance(result, JudgeResult)
    assert result.verdict == "success"
    assert "任务完成" in result.reason
    assert result.session_id.startswith(HEADLESS_JUDGE_PREFIX)
    assert result.error == ""
    assert "verdict" in result.raw_output


def test_run_judge_agent_failed(base_config, monkeypatch):
    """Ticket 03: judge 返回 verdict=failed → JudgeResult.verdict=failed。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "failed", "reason": "任务未完成"}',
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 失败了"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "failed"
    assert "任务未完成" in result.reason


def test_run_judge_agent_uncertain(base_config, monkeypatch):
    """Ticket 03: judge 返回 verdict=uncertain → JudgeResult.verdict=uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "uncertain", "reason": "输出含糊"}',
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 说不确定"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "输出含糊" in result.reason


# ---------- 降级规则 ----------


def test_run_judge_agent_invalid_json_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：judge 输出非 JSON → uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text="我觉得任务好像完成了吧",  # 非 JSON
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    # reason 文本 lower 后含 "not valid json" 或 "invalid"
    reason_lower = result.reason.lower()
    assert "not valid json" in reason_lower or "invalid" in reason_lower


def test_run_judge_agent_empty_output_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：judge 输出为空 → uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text="",  # 空输出
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "empty" in result.reason.lower()


def test_run_judge_agent_missing_verdict_field_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：JSON 缺 verdict 字段 → uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"reason": "no verdict"}',  # 缺 verdict
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "invalid verdict" in result.reason.lower()


def test_run_judge_agent_session_failed_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：judge session status != completed → uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    # status="failed" 而非 "completed"
    failed_outcome = RunOutcome(
        session_id="will-be-replaced",
        status="failed",
        stop_reason="provider_error",
        iterations=1,
    )
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "不应到达"}',  # 不应被解析
        outcome=failed_outcome,
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "not completed" in result.reason.lower() or "failed" in result.reason.lower()


def test_run_judge_agent_max_iterations_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：judge stop_reason 含 max_iterations → uncertain。"""
    _patch_gateway_registry_executor(monkeypatch)
    max_iter_outcome = RunOutcome(
        session_id="will-be-replaced",
        status="completed",  # status 可能是 completed 但 stop_reason 是 max_iterations
        stop_reason="max_iterations",
        iterations=base_config.judge_max_iterations,
    )
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "不应到达"}',
        outcome=max_iter_outcome,
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "max_iterations" in result.reason.lower()


def test_run_judge_agent_outcome_none_degrades_to_uncertain(base_config, monkeypatch):
    """Ticket 03 降级：facade.start 返回 None（异常场景）→ uncertain。

    通过让 facade.start 抛异常触发 except 分支（run_judge_agent 内部 catch → JudgeResult uncertain）。
    """
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text="unused",
        fail_with_exception=RuntimeError("simulated judge session failure"),
    )

    async def run():
        return await run_judge_agent(
            main_output=["主 agent 输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.verdict == "uncertain"
    assert "simulated judge session failure" in result.reason or "judge session failed" in result.reason.lower()


# ---------- session_id / mode / interrupt registry 验证 ----------


def test_run_judge_agent_generates_judge_session_id(base_config, monkeypatch):
    """Ticket 03: 不传 session_id 时自动生成 headless-judge-{uuid}。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "ok"}',
    )

    async def run():
        return await run_judge_agent(
            main_output=["输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert result.session_id.startswith(HEADLESS_JUDGE_PREFIX)
    assert len(result.session_id) == len(HEADLESS_JUDGE_PREFIX) + 12


def test_run_judge_agent_uses_explicit_session_id(base_config, monkeypatch):
    """Ticket 03: 支持外部传入 session_id。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "ok"}',
    )
    explicit_sid = "headless-judge-explicit1"

    async def run():
        return await run_judge_agent(
            main_output=["输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
            session_id=explicit_sid,
        )

    result = asyncio.run(run())
    assert result.session_id == explicit_sid


def test_run_judge_agent_writes_headless_judge_mode(base_config, monkeypatch):
    """Ticket 03: judge session 写入 sessions 表 mode='headless_judge'。"""
    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "ok"}',
    )

    captured_sid = []

    async def run():
        # 包一层 patched_start 捕获 session_id
        original_start = SessionFacade.start

        async def capture_start(self, session_id, text, *, mode="dialogue", model_override=""):
            captured_sid.append(session_id)
            return await original_start(self, session_id, text, mode=mode, model_override=model_override)

        monkeypatch.setattr(SessionFacade, "start", capture_start)
        return await run_judge_agent(
            main_output=["输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    assert len(captured_sid) == 1
    assert captured_sid[0] == result.session_id

    # 重新打开 EventStore 验证 mode
    verify_store = EventStore(db_path=base_config.db_path)
    verify_store.init()
    try:
        async def verify():
            session = await verify_store.get_session(result.session_id)
            assert session is not None
            assert session.mode == "headless_judge"
        asyncio.run(verify())
    finally:
        verify_store.close()


def test_run_judge_agent_cleans_interrupt_registry(base_config, monkeypatch):
    """Ticket 03/07: run_judge_agent 结束后清理 interrupt registry。"""
    from server.activity_tracker.headless_runner import get_interrupt_event

    _patch_gateway_registry_executor(monkeypatch)
    _patch_facade_start_with_judge_output(
        monkeypatch,
        judge_response_text='{"verdict": "success", "reason": "ok"}',
    )

    async def run():
        return await run_judge_agent(
            main_output=["输出"],
            trigger_text=base_config.trigger_text,
            config=base_config,
        )

    result = asyncio.run(run())
    # 结束后 interrupt registry 应已清理
    assert get_interrupt_event(result.session_id) is None


# ---------- 资源限制默认值 ----------


def test_valid_verdicts_set():
    """Ticket 03: VALID_VERDICTS 含三种合法 verdict。"""
    assert {"success", "failed", "uncertain"} == VALID_VERDICTS
