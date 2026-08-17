"""v6-lite-chat-fix T01 验收测试：runner system prompt 自动注入

覆盖 Ticket 01 acceptance（temp/sdd/v6-lite-chat-fix/tickets.md）：
- [x] runner `_build_request` 首轮构建的 LLMRequest.system 非空，含 "LocalAgent" / "agent_guide" / "5:00"
- [x] `compute_today(now)` 纯函数：01:18→前一天，14:00→当天，05:00→当天，04:59→前一天
- [x] 首轮调 agent_guide(task=用户首句) 一次（spy 验证），第二轮 _build_request 不调
- [x] guide 调用失败降级：system 仍含 stable prefix + "guide 调用失败"标记，不阻断对话
- [x] stable prefix（AGENTS.md 全文 + project_rules 全文 + 身份铁律）session 内缓存复用

Anti-Cheat 约束（spec）：
- 测试用 MockLLM + mock HTTP（不起真后端）
- spy 必须验证 guide 真被调（不能 mock 后跳过调用断言）
- 断言字符串 "LocalAgent" / "agent_guide" / "5:00" / "guide 调用失败" 定稿，不许改
- 反向验证：临时改 system="" → 测试失败 → 恢复 → 绿
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# 确保 PROJECT_ROOT 在 sys.path（conftest.py 已加，但单独运行时兜底）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    Message,
    MockLLM,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent import runner as runner_mod  # noqa: E402
from client.core.agent.runner import compute_today  # noqa: E402

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


async def _prepare_session(store, user_text: str, session_id: str = "sess-inject") -> None:
    """helper：创建会话 + 写入一条 user 消息。"""
    await store.create_session(session_id=session_id, title="T01 inject test")
    await store.append_message(
        session_id, Message(role="user", content=user_text, source="user")
    )


def _make_runner(store, *, mock_text: str = "MockLLM ok") -> tuple[SessionRunner, MockLLM]:
    """构造一个 runner + MockLLM，便于测试断言。"""
    mock = MockLLM(default_text=mock_text)
    runner = SessionRunner(
        deps=RunnerDeps(llm_gateway=mock, event_store=store),
        config=RunnerConfig(session_id="sess-inject"),
    )
    return runner, mock


# ============================================================================
# 1. system prompt 非空 + 含关键字符串
# ============================================================================


def test_system_not_empty(store):
    """T01: 首轮 _build_request 的 LLMRequest.system 非空，含 LocalAgent / agent_guide / 5:00。"""
    async def run():
        await _prepare_session(store, "你好，请帮我记账", session_id="sess-inject")
        runner, _ = _make_runner(store)
        messages = await store.load_messages("sess-inject")
        request = runner._build_request(messages)
        assert request.system, "system prompt 不能为空（system='' 是 v6-lite-chat-fix 要修的根病根）"
        # stable prefix 提供 "LocalAgent"（身份铁律 + AGENTS.md 全文）
        assert "LocalAgent" in request.system, "system 必须含 LocalAgent 身份"
        # ephemeral 提供 "agent_guide"（自动调用注入声明）
        assert "agent_guide" in request.system, "system 必须含 agent_guide 自动调用注入声明"
        # ephemeral 提供 "5:00"（今天日期按 5:00 分界线算出）
        assert "5:00" in request.system, "system 必须含 5:00 分界线说明"
    asyncio.run(run())


# ============================================================================
# 2. compute_today 5:00 分界线纯函数
# ============================================================================


def test_compute_today_5am_boundary():
    """T01: compute_today 5:00 分界线：01:18→前一天，14:00→当天，05:00→当天，04:59→前一天。"""
    # 14:00 → 当天
    dt = datetime(2026, 8, 2, 14, 0, 0)
    assert compute_today(dt) == date(2026, 8, 2), "14:00 应为当天"
    # 05:00 → 当天（边界含等号）
    dt = datetime(2026, 8, 2, 5, 0, 0)
    assert compute_today(dt) == date(2026, 8, 2), "05:00 应为当天"
    # 01:18 → 前一天
    dt = datetime(2026, 8, 2, 1, 18, 0)
    assert compute_today(dt) == date(2026, 8, 1), "01:18 应为前一天"
    # 04:59 → 前一天
    dt = datetime(2026, 8, 2, 4, 59, 0)
    assert compute_today(dt) == date(2026, 8, 1), "04:59 应为前一天"


# ============================================================================
# 3. agent_guide 自动调用首轮一次（spy 验证）
# ============================================================================


def test_guide_auto_called_first_turn(store):
    """T01: 首轮 _build_request 调 agent_guide 一次，第二轮不调。

    Anti-Cheat: 用 spy 验证 runner 真的调了 _call_agent_guide_http，不能 mock 后跳过断言。
    """
    async def run():
        await _prepare_session(store, "你好，请帮我记账", session_id="sess-inject")
        runner, _ = _make_runner(store)
        # mock _call_agent_guide_http（避免真起后端），但 spy 验证调用
        with patch.object(runner_mod, "_call_agent_guide_http", return_value={"task_type": "adhoc.test"}) as mock_guide:
            messages = await store.load_messages("sess-inject")
            # 首轮
            runner._build_request(messages)
            assert mock_guide.call_count == 1, (
                f"首轮应调 agent_guide 一次，实际 {mock_guide.call_count}"
            )
            # 验证调用参数：task=用户首句
            call_args = mock_guide.call_args
            # task 是第一个位置参数
            task_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("task", "")
            assert "记账" in task_arg, f"guide 调用应传用户首句，实际传了 {task_arg!r}"

            # 第二轮：追加 assistant + 再调 _build_request
            await store.append_message(
                "sess-inject", Message(role="assistant", content="好的，我帮你记账", source="assistant")
            )
            messages2 = await store.load_messages("sess-inject")
            runner._build_request(messages2)
            assert mock_guide.call_count == 1, (
                f"第二轮不应再调 agent_guide，实际调用次数 {mock_guide.call_count}"
            )
    asyncio.run(run())


# ============================================================================
# 4. guide 调用失败降级（仍注入 stable prefix + 降级标记）
# ============================================================================


def test_guide_failure_degrades(store):
    """T01: guide 调用失败时，system 仍含 stable prefix + "guide 调用失败"标记，不阻断对话。"""
    async def run():
        await _prepare_session(store, "你好", session_id="sess-inject")
        runner, _ = _make_runner(store)
        # mock guide 抛异常（模拟后端挂了）
        with patch.object(runner_mod, "_call_agent_guide_http", side_effect=Exception("network down")):
            messages = await store.load_messages("sess-inject")
            # 不应抛异常
            request = runner._build_request(messages)
            assert request.system, "guide 失败时 system 仍应非空（含 stable prefix）"
            # stable prefix 仍注入
            assert "LocalAgent" in request.system, (
                "guide 失败时 stable prefix 仍应注入（LocalAgent 身份）"
            )
            # 降级标记
            assert "guide 调用失败" in request.system, (
                "guide 失败时 system 必须含 'guide 调用失败' 降级标记"
            )
    asyncio.run(run())


# ============================================================================
# 5. stable prefix 缓存复用（AGENTS.md 文件只读一次）
# ============================================================================


def test_stable_prefix_cached(store):
    """T01: stable prefix session 内缓存复用，AGENTS.md 文件只读一次。

    spec D2: stable prefix 缓存——session 内首次构建后缓存字符串，后续轮次复用（不重复读 AGENTS.md 文件）
    """
    async def run():
        await _prepare_session(store, "你好", session_id="sess-inject")
        runner, _ = _make_runner(store)
        # spy _read_stable_prefix_files（用 wraps 保留真实行为，仅记录调用次数）
        # mock guide 避免真起后端；合并 with 满足 ruff SIM117
        with patch.object(runner_mod, "_read_stable_prefix_files",
                          wraps=runner_mod._read_stable_prefix_files) as spy_read, \
            patch.object(runner_mod, "_call_agent_guide_http", return_value={"task_type": "x"}):
            messages = await store.load_messages("sess-inject")
            # 首轮 _build_request
            runner._build_request(messages)
            # 第二轮 _build_request
            await store.append_message(
                "sess-inject", Message(role="assistant", content="hi", source="assistant")
            )
            messages2 = await store.load_messages("sess-inject")
            runner._build_request(messages2)
            # 第三轮 _build_request（验证更彻底）
            await store.append_message(
                "sess-inject", Message(role="user", content="继续", source="user")
            )
            messages3 = await store.load_messages("sess-inject")
            runner._build_request(messages3)

            assert spy_read.call_count == 1, (
                f"stable prefix 应只读一次（session 内缓存复用），"
                f"实际调用 {spy_read.call_count} 次"
            )
    asyncio.run(run())
