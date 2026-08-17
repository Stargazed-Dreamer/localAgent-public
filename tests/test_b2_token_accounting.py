"""B2 · token 记账 regression 测试（spec D2/D3）

测试目标：
1. 验证 cost 相关字段已删除（引用报 AttributeError）
2. 验证 RunOutcome.total_tokens 正确累计
3. 验证 chat.py 不再传 max_budget_usd
4. 验证 headless_runner 不再传 max_budget_usd
5. 验证 headless config 不再有 max_budget_usd / judge_max_budget_usd 字段

测试策略（spec D12）：regression_only（不算 cost 后无新功能，仅验证清理彻底）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    RunOutcome,
    SessionRunner,
)
from client.core.agent.types import LLMResponse, Message  # noqa: E402

# ============================================================================
# 1. cost 字段已删除（types.py）
# ============================================================================


class TestCostFieldsRemoved:
    """B2: types.py 中 cost 相关字段已删除，引用应报 AttributeError。"""

    def test_llm_response_no_cost_usd(self):
        """LLMResponse 不再接受 cost_usd 参数。"""
        with pytest.raises(TypeError):
            LLMResponse(content="x", cost_usd=0.5)  # type: ignore[call-arg]

    def test_runner_config_no_max_budget_usd(self):
        """RunnerConfig 不再接受 max_budget_usd 参数。"""
        with pytest.raises(TypeError):
            RunnerConfig(session_id="s1", max_budget_usd=1.0)  # type: ignore[call-arg]

    def test_run_outcome_no_total_cost_usd(self):
        """RunOutcome 不再接受 total_cost_usd 参数。"""
        with pytest.raises(TypeError):
            RunOutcome(session_id="s1", status="completed", total_cost_usd=0.5)  # type: ignore[call-arg]

    def test_run_outcome_has_total_tokens(self):
        """RunOutcome 仍接受 total_tokens 参数。"""
        outcome = RunOutcome(session_id="s1", status="completed", total_tokens=100)
        assert outcome.total_tokens == 100


# ============================================================================
# 2. total_tokens 累计（runner.py）
# ============================================================================


def _seed_session(store, session_id: str):
    """创建 session + 一条 user 消息。"""
    asyncio.run(store.create_session(session_id=session_id))
    asyncio.run(store.append_message(
        session_id, Message(role="user", content="hi", source="user"),
    ))


class TestTotalTokensAccumulated:
    """B2: runner.py 主循环累计 total_tokens（prompt + completion）。"""

    def test_single_turn_tokens_accumulated(self, tmp_db_path):
        """单轮对话：total_tokens = LLMResponse.usage.total_tokens。"""
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            _seed_session(store, "s1")
            mock = MockLLM(script=[
                LLMResponse(
                    content="hello",
                    usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    stop_reason="end_turn",
                ),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock, event_store=store),
                config=RunnerConfig(session_id="s1"),
            )
            outcome = asyncio.run(runner.run())
            assert outcome.status == "completed"
            assert outcome.total_tokens == 150
        finally:
            store.close()

    def test_multi_turn_tokens_accumulated(self, tmp_db_path):
        """多轮工具对话：total_tokens 累计每轮 LLM usage。"""
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            _seed_session(store, "s2")
            # 第一轮：返回 tool_calls → 触发下一轮
            # 第二轮：返回纯文本 → completed
            mock = MockLLM(script=[
                LLMResponse(
                    content="调用工具",
                    tool_calls=[{
                        "id": "c1", "type": "function",
                        "function": {"name": "noop", "arguments": "{}"},
                    }],
                    usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    stop_reason="tool_use",
                ),
                LLMResponse(
                    content="完成",
                    usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
                    stop_reason="end_turn",
                ),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock, event_store=store,
                    tool_executor=MockToolExecutor(default_result="ok"),
                ),
                config=RunnerConfig(session_id="s2", max_iterations=5),
            )
            outcome = asyncio.run(runner.run())
            assert outcome.status == "completed"
            assert outcome.iterations == 2
            # 150 + 300 = 450
            assert outcome.total_tokens == 450
        finally:
            store.close()

    def test_zero_usage_when_missing(self, tmp_db_path):
        """LLMResponse.usage 缺失时 total_tokens 累计 0（不报错）。"""
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            _seed_session(store, "s3")
            mock = MockLLM(script=[
                LLMResponse(content="ok", usage={}, stop_reason="end_turn"),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=mock, event_store=store),
                config=RunnerConfig(session_id="s3"),
            )
            outcome = asyncio.run(runner.run())
            assert outcome.status == "completed"
            assert outcome.total_tokens == 0
        finally:
            store.close()


# ============================================================================
# 3. chat.py 不再传 max_budget_usd（D4 白名单第一类）
# ============================================================================


class TestChatPanelBudgetRemoved:
    """B2: chat.py 不再传 max_budget_usd 给 RunnerConfig。"""

    def test_chat_py_no_max_budget_usd_in_config(self):
        """chat.py 源码中不再出现 max_budget_usd 字面量（B4 注释除外）。"""
        chat_path = PROJECT_ROOT / "client" / "panels" / "chat.py"
        content = chat_path.read_text(encoding="utf-8")
        # 注释行允许出现（说明删除原因），但 "max_budget_usd": 1.0 之类的传参不允许
        # 检查是否有 max_budget_usd= 或 "max_budget_usd": 传参
        # 应该为 0（除了纯注释行）
        non_comment_matches = [
            line for line in content.splitlines()
            if "max_budget_usd" in line
            and not line.strip().startswith("#")
            and "B2" not in line  # B2 注释允许
        ]
        assert non_comment_matches == [], (
            f"chat.py 仍有 max_budget_usd 非注释引用: {non_comment_matches}"
        )

    def test_chat_py_no_total_cost_usd_in_outcome_reading(self):
        """chat.py 不再读 outcome.total_cost_usd。"""
        chat_path = PROJECT_ROOT / "client" / "panels" / "chat.py"
        content = chat_path.read_text(encoding="utf-8")
        # 不应出现 getattr(outcome, "total_cost_usd", ...)
        non_comment_matches = [
            line for line in content.splitlines()
            if "total_cost_usd" in line
            and not line.strip().startswith("#")
            and "B2" not in line
        ]
        assert non_comment_matches == [], (
            f"chat.py 仍有 total_cost_usd 非注释引用: {non_comment_matches}"
        )


# ============================================================================
# 4. headless_runner 不再传 max_budget_usd
# ============================================================================


class TestHeadlessRunnerBudgetRemoved:
    """B2: headless_runner.py 不再有 max_budget_usd 字段或常量。"""

    def test_no_default_max_budget_usd_constant(self):
        """server.activity_tracker.headless_runner 模块不再导出 DEFAULT_MAX_BUDGET_USD。"""
        from server.activity_tracker import headless_runner as hr
        assert not hasattr(hr, "DEFAULT_MAX_BUDGET_USD"), (
            "B2: DEFAULT_MAX_BUDGET_USD 常量应已删除"
        )
        assert not hasattr(hr, "DEFAULT_JUDGE_MAX_BUDGET_USD"), (
            "B2: DEFAULT_JUDGE_MAX_BUDGET_USD 常量应已删除"
        )

    def test_headless_config_no_budget_fields(self):
        """HeadlessConfig 不再有 max_budget_usd / judge_max_budget_usd 字段。"""
        from server.activity_tracker.headless_runner import HeadlessConfig
        cfg = HeadlessConfig()
        assert not hasattr(cfg, "max_budget_usd"), (
            "B2: HeadlessConfig.max_budget_usd 应已删除"
        )
        assert not hasattr(cfg, "judge_max_budget_usd"), (
            "B2: HeadlessConfig.judge_max_budget_usd 应已删除"
        )

    def test_parse_headless_config_ignores_budget_keys(self):
        """parse_headless_config 对 config_dict 中的 max_budget_usd 静默忽略。"""
        from server.activity_tracker.headless_runner import parse_headless_config
        # config_dict 含 max_budget_usd（用户旧配置残留），不应报错
        cfg = parse_headless_config({
            "trigger_text": "test",
            "max_budget_usd": 5.0,  # 旧字段，应被忽略
            "judge_max_budget_usd": 0.5,
        })
        assert cfg.trigger_text == "test"
        # max_budget_usd 不存在于 cfg 上
        assert not hasattr(cfg, "max_budget_usd")
        assert not hasattr(cfg, "judge_max_budget_usd")


# ============================================================================
# 5. probe/runner 不再读 cost_usd
# ============================================================================


class TestProbeRunnerCostRemoved:
    """B2: server/probe/runner.py 不再读 LLMResponse.cost_usd。"""

    def test_probe_runner_no_cost_usd_in_llm_dict(self):
        """probe runner 的 _llm_response_to_dict 不再输出 cost_usd。"""
        probe_path = PROJECT_ROOT / "server" / "probe" / "runner.py"
        content = probe_path.read_text(encoding="utf-8")
        # 不应出现 cost_usd 字段引用（B2 注释除外）
        non_comment_matches = [
            line for line in content.splitlines()
            if "cost_usd" in line
            and not line.strip().startswith("#")
            and "B2" not in line
        ]
        assert non_comment_matches == [], (
            f"probe/runner.py 仍有 cost_usd 非注释引用: {non_comment_matches}"
        )
