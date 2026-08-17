"""B3 · 压缩相关 regression 测试（spec D2/D3 关联）

测试目标：
1. 验证 compactor.py 无 cost_usd / max_budget_usd 引用（B2 清理彻底）
2. 验证 runner.py 压缩路径（reactive_compact_retry / has_attempted_reactive_compact）保留且无 cost 累积
3. 验证 413 ContextOverflow → 触发 reactive_compact_retry（首次压缩重试）
4. 验证二次 413 → context_overflow_after_compact（不再重试）

测试策略（spec D12）：regression_only（压缩逻辑保留，仅验证 B2 清理后仍工作）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    Compactor,
    ContextOverflow,
    EventStore,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.types import LLMRequest, LLMResponse, Message  # noqa: E402

# ============================================================================
# 1. compactor.py 无 cost 引用（B2 清理彻底）
# ============================================================================


class TestCompactorNoCostReferences:
    """B3: compactor.py 不应含 cost_usd / max_budget_usd 引用（B2 清理）。"""

    def test_compactor_no_cost_usd(self):
        """compactor.py 源码中无 cost_usd 引用。"""
        path = PROJECT_ROOT / "client" / "core" / "agent" / "compactor.py"
        content = path.read_text(encoding="utf-8")
        non_comment = [
            line for line in content.splitlines()
            if "cost_usd" in line and not line.strip().startswith("#")
        ]
        assert non_comment == [], f"compactor.py 仍有 cost_usd 引用: {non_comment}"

    def test_compactor_no_max_budget_usd(self):
        """compactor.py 源码中无 max_budget_usd 引用。"""
        path = PROJECT_ROOT / "client" / "core" / "agent" / "compactor.py"
        content = path.read_text(encoding="utf-8")
        non_comment = [
            line for line in content.splitlines()
            if "max_budget_usd" in line and not line.strip().startswith("#")
        ]
        assert non_comment == [], f"compactor.py 仍有 max_budget_usd 引用: {non_comment}"


# ============================================================================
# 2. runner.py 压缩路径无 cost 累积（B2 清理彻底）
# ============================================================================


class TestRunnerCompactPathNoCost:
    """B3: runner.py 压缩路径（reactive_compact_retry）不应含 cost 累积。"""

    def test_runner_no_cost_accumulation_in_compact_path(self):
        """runner.py 压缩路径不应含 cost_usd 累积逻辑。"""
        path = PROJECT_ROOT / "client" / "core" / "agent" / "runner.py"
        content = path.read_text(encoding="utf-8")
        # 在压缩相关代码段（reactive_compact_retry 块）内不应有 cost 累积
        # 简单检查：整文件中 cost_usd 仅出现在 B2 注释中
        non_comment = [
            line for line in content.splitlines()
            if "cost_usd" in line
            and not line.strip().startswith("#")
            and "B2" not in line
        ]
        assert non_comment == [], f"runner.py 仍有 cost_usd 非注释非 B2 引用: {non_comment}"


# ============================================================================
# 3. reactive_compact_retry 仍工作（413 → 压缩 → 重试）
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _OverflowThenSuccessLLM:
    """第一次 call 抛 ContextOverflow，第二次返回正常响应。

    注：compactor 内部也调 llm_gateway.call 做摘要，所以 LLM 调用顺序：
    - 第 1 次：主循环调（抛 413）
    - 第 2 次：compactor 摘要调（成功）
    - 第 3 次：主循环重试调（成功）
    """

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if self.call_count == 1:
            raise ContextOverflow("simulated 413", status_code=413)
        return LLMResponse(
            content="recovered after compact" if self.call_count == 3 else "summary",
            usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            stop_reason="end_turn",
        )

    async def stream(self, request: LLMRequest):
        # 非流式测试用
        response = await self.call(request)
        if response.content:
            yield {"type": "text_delta", "delta": response.content}
        yield {"type": "done", "finish_reason": "stop"}


class TestReactiveCompactRetryStillWorks:
    """B3: 413 ContextOverflow → 触发 reactive_compact_retry（保留 B2 前的行为）。"""

    def test_first_overflow_triggers_compact_retry(self, tmp_path):
        """首次 413 → compactor.compact_with_result → 重试 → completed。"""
        store = EventStore(db_path=str(tmp_path / "b3_compact.db"))
        store.init()
        try:
            _run(store.create_session(session_id="s1"))
            # 预置足够多的消息以触发压缩（> tail_keep=6）
            for i in range(10):
                _run(store.append_message("s1", Message(
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"msg {i} " * 50,  # 长消息
                    source="user" if i % 2 == 0 else "assistant",
                )))

            llm = _OverflowThenSuccessLLM()
            compactor = Compactor(max_context_tokens=8000, tail_keep=6)
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="s1", max_iterations=5, use_stream=False),
            )
            outcome = _run(runner.run())

            # 首次 413 → 压缩 → 第二次成功
            assert outcome.status == "completed", (
                f"首次 413 应压缩重试后 completed，实际={outcome.status}"
            )
            # LLM 被调 3 次：1 主循环 413 + 2 compactor 摘要 + 3 主循环重试成功
            assert llm.call_count == 3, f"LLM 应被调 3 次（413+summary+success），实际={llm.call_count}"
            # has_attempted_reactive_compact 应为 True（已尝试压缩）
            assert runner.has_attempted_reactive_compact is True
            # B2 验证：total_tokens 只在主循环 LLM 成功调用时累计（不含 compactor 内部摘要调用）
            # 第 1 次 413 不返回 usage；第 2 次 summary 是 compactor 内部调用不累计；第 3 次重试返回 80
            assert outcome.total_tokens == 80
        finally:
            store.close()


class _OverflowSuccessOverflowLLM:
    """主循环调用抛 413，compactor 摘要调用成功，重试再抛 413。

    调用顺序：
    - 第 1 次：主循环调（抛 413）
    - 第 2 次：compactor 摘要调（成功）
    - 第 3 次：主循环重试调（抛 413）→ has_attempted_reactive_compact=True → failed
    """

    def __init__(self):
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if self.call_count in (1, 3):
            raise ContextOverflow(f"simulated 413 call {self.call_count}", status_code=413)
        # 第 2 次：compactor 摘要调
        return LLMResponse(
            content="summary",
            usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            stop_reason="end_turn",
        )

    async def stream(self, request: LLMRequest):
        raise ContextOverflow("always overflow stream", status_code=413)


class TestSecondOverflowFails:
    """B3: 二次 413 → context_overflow_after_compact（不再重试）。"""

    def test_second_overflow_fails_no_loop(self, tmp_path):
        """已 has_attempted_reactive_compact=True 时再次 413 → failed。"""
        store = EventStore(db_path=str(tmp_path / "b3_second.db"))
        store.init()
        try:
            _run(store.create_session(session_id="s2"))
            for i in range(10):
                _run(store.append_message("s2", Message(
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"msg {i} " * 50,
                    source="user" if i % 2 == 0 else "assistant",
                )))

            llm = _OverflowSuccessOverflowLLM()
            compactor = Compactor(max_context_tokens=8000, tail_keep=6)
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    compactor=compactor,
                ),
                config=RunnerConfig(session_id="s2", max_iterations=5, use_stream=False),
            )
            outcome = _run(runner.run())

            # 首次 413 → 压缩（第 2 次 summary 成功）→ 重试 → 二次 413 → failed
            assert outcome.status == "failed", (
                f"二次 413 应 failed，实际={outcome.status}"
            )
            assert outcome.stop_reason == "context_overflow_after_compact"
            # LLM 被调 3 次：1 主 413 + 2 summary + 3 主 413
            assert llm.call_count == 3
            # has_attempted_reactive_compact 为 True（已尝试）
            assert runner.has_attempted_reactive_compact is True
            # B2 验证：total_tokens=0（主循环两次都抛 413，不累计；compactor summary 不计入主循环）
            assert outcome.total_tokens == 0
        finally:
            store.close()
