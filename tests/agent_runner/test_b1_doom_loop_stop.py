"""B1 · DoomLoop 停止 + sys 系统消息（red_green 测试，D10 重构）

spec D10：检测到 DoomLoop → 停止（不 retry）+ 写 sys 系统消息 + 返回 interrupted。

新行为：
- doom_loop.py：保留检测逻辑，移除 DOOM_LOOP_AVOID_REPETITION_PROMPT 注入
- runner.py：检测到 DoomLoop → 不 retry，改为：
  1. 写 sys 系统消息（source="system_warning", visible=True）到 messages 末尾
  2. sys 消息 content："检测到模型循环，建议新开对话。如需继续，请换种问法或补充新信息"
  3. 返回 RunOutcome(status="interrupted", stop_reason="doom_loop_detected")
- _build_request 时过滤 source="system_warning" 的消息（LLM 看不到 sys 消息）

测试策略：
1. 红测试：mock LLM 返回 stop_reason="doom_loop"，断言 sys 消息写入 + interrupted（修复前会 retry，失败）
2. 绿测试：修复后断言全部条件满足
3. 回归：sys 消息不进 LLM 请求
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    EventStore,
    MockLLM,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.doom_loop import DoomLoopConfig  # noqa: E402
from client.core.agent.types import (  # noqa: E402
    LLMResponse,
    Message,
)


def _make_store(tmp_path) -> EventStore:
    db_path = str(tmp_path / "test_b1.db")
    store = EventStore(db_path)
    store.init()
    return store


class TestB1DoomLoopStop:
    """B1: DoomLoop 检测后停止 + 写 sys 系统消息（D10 重构）。"""

    def test_doom_loop_writes_sys_message_and_interrupts(self, tmp_path):
        """DoomLoop 命中 → 写 sys 系统消息 + 返回 interrupted。

        修复前：DoomLoop 命中 → retry budget + backoff + nudge + 重新调 LLM（行为错）
        修复后：DoomLoop 命中 → 不 retry + 写 sys 消息 + 返回 interrupted
        """
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s1"))
            asyncio.run(store.append_message("s1", Message(role="user", content="hi")))

            # MockLLM 返回 stop_reason="doom_loop"（模拟 DoomLoopDetector 命中）
            llm = MockLLM(callable=lambda req: LLMResponse(
                content="partial thinking...",
                thinking="repeated pattern " * 10,
                stop_reason="doom_loop",
            ))

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    doom_loop_config=DoomLoopConfig(enabled=True, max_retries=3),
                ),
                config=RunnerConfig(session_id="s1", max_iterations=5, use_stream=False),
            )
            outcome = asyncio.run(runner.run())

            # 修复前：retry 路径会调多次 LLM，最终 fail
            # 修复后：1 次 LLM 调用 + sys 消息 + interrupted
            assert outcome.status == "interrupted", (
                f"DoomLoop 应返回 interrupted（不 retry 不 fail），实际 status={outcome.status}"
            )
            assert outcome.stop_reason == "doom_loop_detected", (
                f"stop_reason 应为 doom_loop_detected，实际={outcome.stop_reason}"
            )
            # LLM 只被调用 1 次（不 retry）
            assert llm.call_count == 1, (
                f"DoomLoop 应停止不 retry，LLM 调用应只 1 次，实际={llm.call_count}"
            )

            # 验证 sys 系统消息写入 messages 末尾
            messages = asyncio.run(store.load_messages("s1"))
            sys_msgs = [m for m in messages if m.source == "system_warning"]
            assert len(sys_msgs) == 1, (
                f"应写入 1 条 source=system_warning 消息，实际={len(sys_msgs)}"
            )
            assert sys_msgs[0].visible is True, "sys 消息应 visible=True（用户可见）"
            assert "循环" in sys_msgs[0].content or "loop" in sys_msgs[0].content.lower(), (
                f"sys 消息 content 应含循环提示，实际={sys_msgs[0].content!r}"
            )
        finally:
            store.close()

    def test_doom_loop_sys_message_not_sent_to_llm(self, tmp_path):
        """sys 系统消息不应被发送给 LLM（_build_request 过滤 source=system_warning）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s2"))
            # 预置：1 条 user 消息 + 1 条 system_warning 消息
            asyncio.run(store.append_message("s2", Message(role="user", content="hi")))
            asyncio.run(store.append_message("s2", Message(
                role="user",
                content="检测到模型循环，建议新开对话",
                source="system_warning",
                visible=True,
            )))

            captured_messages: list[list[Message]] = []

            def llm_callable(req):
                captured_messages.append(list(req.messages))
                return LLMResponse(content="ok", stop_reason="end_turn")

            llm = MockLLM(callable=llm_callable)
            runner = SessionRunner(
                deps=RunnerDeps(llm_gateway=llm, event_store=store),
                config=RunnerConfig(session_id="s2", max_iterations=1, use_stream=False),
            )
            asyncio.run(runner.run())

            assert len(captured_messages) >= 1, "LLM 应被调用至少 1 次"
            req_msgs = captured_messages[0]
            # LLM 请求中不应含 source=system_warning 消息
            sys_in_req = [m for m in req_msgs if getattr(m, "source", None) == "system_warning"]
            assert len(sys_in_req) == 0, (
                f"system_warning 消息不应发给 LLM，但请求中含 {len(sys_in_req)} 条"
            )
        finally:
            store.close()

    def test_doom_loop_no_retry_no_nudge_message(self, tmp_path):
        """DoomLoop 命中后不应注入 doom_loop_nudge 消息（D10 重构后废弃）。"""
        store = _make_store(tmp_path)
        try:
            asyncio.run(store.create_session(session_id="s3"))
            asyncio.run(store.append_message("s3", Message(role="user", content="hi")))

            llm = MockLLM(callable=lambda req: LLMResponse(
                content="",
                thinking="x" * 200,
                stop_reason="doom_loop",
            ))
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm,
                    event_store=store,
                    doom_loop_config=DoomLoopConfig(enabled=True, max_retries=3),
                ),
                config=RunnerConfig(session_id="s3", max_iterations=5, use_stream=False),
            )
            asyncio.run(runner.run())

            messages = asyncio.run(store.load_messages("s3"))
            nudge_msgs = [m for m in messages if m.source == "doom_loop_nudge"]
            assert len(nudge_msgs) == 0, (
                f"D10 重构后不应注入 doom_loop_nudge 消息，实际={len(nudge_msgs)}"
            )
        finally:
            store.close()
