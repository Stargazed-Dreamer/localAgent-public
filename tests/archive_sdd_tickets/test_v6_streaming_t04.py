"""v6-lite-streaming-gui T04 验收测试：DoomLoopDetector

覆盖 T04 acceptance（temp/sdd/v6-lite-streaming-gui/tickets.md）：
- [x] DoomLoop 检测到 thinking_delta 尾重复（模拟注入）→ abort（B1 重构后：停止，不 retry）
- [x] enabled=false 时 DoomLoop 不生效
- [x] thinking_delta 缺失时 DoomLoop 不生效（不降级到 text_delta）

B1（spec D10）重构说明：
- 旧测试 test_doom_loop_triggers_retry_then_success / test_doom_loop_exhausted_3_retries_fails /
  test_doom_loop_nudge_message_injected / test_doom_loop_non_consecutive_resets_counter
  已删除（验证 retry/nudge 行为，B1 后不再 retry 不再注入 nudge，新行为由
  tests/test_b1_doom_loop_stop.py 覆盖：stop + sys_warning + interrupted）
- 保留的测试均为不触发 DoomLoop 或验证 has_attempted_reactive_compact 不被消耗

测试策略：
- DoomLoopDetector 单元测试：构造重复 thinking_delta → 检测命中
- DoomLoopDetector 单元测试：正常 thinking_delta → 不命中
- DoomLoopDetector 单元测试：enabled=false → 不检测
- DoomLoopDetector 单元测试：reset 清空 tail_window
- 集成测试：MockLLM 不触发 DoomLoop（disabled / no config / no thinking）→ 正常完成
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    DoomLoopConfig,
    DoomLoopDetector,
    EventStore,
    MockLLM,
    MockToolExecutor,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
)
from client.core.agent.types import LLMRequest, LLMResponse, Message  # noqa: E402

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "agent_t04.db"))
    store.init()
    return store


def _make_doom_llm(repeat_pattern: str = "A" * 60, repeat_count: int = 5, final_text: str = "done"):
    """构造一个 MockLLM，stream() 先 yield 重复的 thinking_delta，然后 yield done。

    repeat_pattern: 重复单元（长度 ≥ min_repeat_len=50）
    repeat_count: 重复次数（≥ repeat_threshold=3）
    final_text: done 事件前的 text_delta（如果 DoomLoop 未命中）
    """
    def callable(req):
        return LLMResponse(
            content=final_text,
            thinking=repeat_pattern * repeat_count,
            stop_reason="end_turn",
            model="mock-doom",
        )
    return MockLLM(callable=callable)


class _DoomThenSuccessLLM:
    """第一次 stream() yield 重复 thinking（触发 DoomLoop），第二次正常返回。

    call_count 共享，第一次 stream() 返回重复 thinking，第二次返回正常文本。
    """

    def __init__(self, repeat_pattern: str = "B" * 55, repeat_count: int = 4):
        self.repeat_pattern = repeat_pattern
        self.repeat_count = repeat_count
        self.call_count = 0
        self.requests: list[LLMRequest] = []

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 1:
            return LLMResponse(
                content="",
                thinking=self.repeat_pattern * self.repeat_count,
                stop_reason="end_turn",
                model="mock-doom",
            )
        return LLMResponse(
            content="recovered",
            stop_reason="end_turn",
            model="mock-doom",
        )

    async def stream(self, request: LLMRequest):
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 1:
            # 第一次：yield 重复 thinking_delta（分多个 chunk，模拟流式）
            full_thinking = self.repeat_pattern * self.repeat_count
            # 分 3 个 chunk yield，让 DoomLoopDetector 逐步累积
            chunk_size = len(full_thinking) // 3 + 1
            for i in range(0, len(full_thinking), chunk_size):
                yield {"type": "thinking_delta", "delta": full_thinking[i:i+chunk_size]}
            # 不 yield done（DoomLoop 应在 thinking_delta 阶段就 abort）
            return
        # 第二次：正常文本
        yield {"type": "text_delta", "delta": "recovered"}
        yield {"type": "done", "finish_reason": "stop"}


class _AlwaysDoomLLM:
    """每次 stream() 都 yield 重复 thinking（总是触发 DoomLoop）。"""

    def __init__(self, repeat_pattern: str = "C" * 50, repeat_count: int = 3):
        self.repeat_pattern = repeat_pattern
        self.repeat_count = repeat_count
        self.call_count = 0

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            content="",
            thinking=self.repeat_pattern * self.repeat_count,
            stop_reason="end_turn",
            model="mock-doom",
        )

    async def stream(self, request: LLMRequest):
        self.call_count += 1
        full_thinking = self.repeat_pattern * self.repeat_count
        chunk_size = len(full_thinking) // 3 + 1
        for i in range(0, len(full_thinking), chunk_size):
            yield {"type": "thinking_delta", "delta": full_thinking[i:i+chunk_size]}


# ============================================================================
# T04-A: DoomLoopDetector 单元测试
# ============================================================================


class TestDoomLoopDetectorUnit:
    """DoomLoopDetector 核心检测逻辑。"""

    def test_repeated_thinking_triggers_detection(self):
        """重复的 thinking_delta（pattern * N）→ 检测命中。"""
        cfg = DoomLoopConfig(tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        pattern = "X" * 60  # 60 字符的 pattern（≥ min_repeat_len=50）
        # 逐 chunk 喂入 pattern * 4
        for _ in range(4):
            hit = detector.on_thinking_delta(pattern)
        # 第 3 次（tail 有 3 个 pattern）应命中
        # 注意：前 2 次 tail 不足 3*50=150，不命中；第 3 次开始命中
        assert hit is True

    def test_normal_thinking_no_detection(self):
        """正常 thinking（无重复）→ 不命中。"""
        cfg = DoomLoopConfig(tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        # 喂入不同的 thinking 内容（不重复）
        contents = [
            "Let me think about this problem step by step. " * 3,
            "First, I need to understand the input. " * 4,
            "Then, I can apply the algorithm. " * 5,
        ]
        hit = False
        for c in contents:
            if detector.on_thinking_delta(c):
                hit = True
                break
        assert hit is False

    def test_short_tail_no_detection(self):
        """tail 不足 min_repeat_len * repeat_threshold → 不命中。"""
        cfg = DoomLoopConfig(tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        # 喂入短文本（< 150 字符）
        short = "A" * 40
        hit = detector.on_thinking_delta(short * 2)  # 80 字符
        assert hit is False

    def test_enabled_false_no_detection(self):
        """enabled=False → 不检测。"""
        cfg = DoomLoopConfig(enabled=False, tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        pattern = "X" * 60
        hit = detector.on_thinking_delta(pattern * 4)
        assert hit is False

    def test_reset_clears_tail_window(self):
        """reset() 清空 tail_window，之前能命中的重复现在不命中。"""
        cfg = DoomLoopConfig(tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        pattern = "X" * 60
        # 喂入重复 → 命中
        detector.on_thinking_delta(pattern * 3)
        # reset
        detector.reset()
        # 再喂入少量（不足以命中）
        hit = detector.on_thinking_delta(pattern)
        assert hit is False

    def test_empty_delta_no_detection(self):
        """空 delta → 不命中（不更新 tail_window）。"""
        cfg = DoomLoopConfig(tail_size=500, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        hit = detector.on_thinking_delta("")
        assert hit is False

    def test_tail_size_truncation(self):
        """tail_window 超过 tail_size 时截断，只保留最近 N 字符。"""
        cfg = DoomLoopConfig(tail_size=200, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        # 喂入 300 字符的非重复内容（超过 tail_size=200）
        detector.on_thinking_delta("A" * 300)
        # tail_window 应被截断到 200 字符
        assert len(detector.tail_window) == 200
        # 200 字符全是 A，是重复的，应命中
        # pattern="A"*50, pattern*3 = "A"*150, tail(200).endswith("A"*150) → True
        # 但我们已经在 on_thinking_delta 中检测过了，这里重新检测
        # 实际上 on_thinking_delta("A"*300) 已经触发检测
        # 让我们用不同字符避免提前命中
        cfg2 = DoomLoopConfig(tail_size=200, min_repeat_len=50, repeat_threshold=3)
        detector2 = DoomLoopDetector(cfg2)
        # 喂入非重复内容（不同字符），不会命中
        content = "".join(chr(65 + i % 26) for i in range(300))
        detector2.on_thinking_delta(content)
        assert len(detector2.tail_window) == 200

    def test_large_pattern_detection(self):
        """大 pattern（接近 tail_size/3）也能检测。"""
        cfg = DoomLoopConfig(tail_size=600, min_repeat_len=50, repeat_threshold=3)
        detector = DoomLoopDetector(cfg)
        pattern = "PATTERN_" * 25  # 200 字符的 pattern
        # pattern * 3 = 600 字符 = tail_size
        hit = detector.on_thinking_delta(pattern * 3)
        assert hit is True


# ============================================================================
# T04-B: Runner DoomLoop 集成测试（B1 重构后保留项）
# ============================================================================


class TestRunnerDoomLoopRetry:
    """Runner 集成 DoomLoopDetector（B1 重构后保留项）。

    B1 重构后 DoomLoop 命中 → 停止 + sys_warning + interrupted（不 retry）。
    新行为由 tests/test_b1_doom_loop_stop.py 覆盖。
    本类保留的测试均不触发 DoomLoop retry 路径（disabled / no config / no thinking），
    以及验证 has_attempted_reactive_compact 不被 DoomLoop 消耗。
    """

    # 注：原 test_doom_loop_triggers_retry_then_success / test_doom_loop_exhausted_3_retries_fails /
    # test_doom_loop_nudge_message_injected / test_doom_loop_non_consecutive_resets_counter
    # 已删除（B1 重构后不再 retry 不再注入 nudge，新行为由 test_b1_doom_loop_stop.py 覆盖）

    def test_doom_loop_retry_not_consume_reactive_compact_budget(self, tmp_path):
        """DoomLoop 命中后停止（B1），has_attempted_reactive_compact 保持 False。

        旧测试验证 DoomLoop retry 不消耗 reactive_compact budget；B1 后 DoomLoop 直接停止
        不进 retry 路径，has_attempted_reactive_compact 自然保持 False（更严格的不变式）。
        """
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="s-doom-3"))
            _run(store.append_message("s-doom-3", Message(role="user", content="hi")))

            llm = _DoomThenSuccessLLM(repeat_pattern="D" * 55, repeat_count=4)
            cfg = DoomLoopConfig(
                tail_size=1000, min_repeat_len=50, repeat_threshold=3,
                max_retries=3, backoff_base_ms=0, backoff_jitter_ms=0,
            )
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    doom_loop_config=cfg,
                ),
                config=RunnerConfig(session_id="s-doom-3"),
            )
            _run(runner.run())

            # B1 后 DoomLoop 命中即停止，reactive_compact 永远不触发
            assert runner.has_attempted_reactive_compact is False
        finally:
            store.close()

    def test_doom_loop_disabled_no_retry(self, tmp_path):
        """enabled=False → DoomLoop 不生效，正常完成（即使 thinking 重复）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="s-doom-4"))
            _run(store.append_message("s-doom-4", Message(role="user", content="hi")))

            llm = _AlwaysDoomLLM(repeat_pattern="E" * 50, repeat_count=3)
            cfg = DoomLoopConfig(
                enabled=False,  # 禁用
                tail_size=1000, min_repeat_len=50, repeat_threshold=3,
            )
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    doom_loop_config=cfg,
                ),
                config=RunnerConfig(session_id="s-doom-4"),
            )
            outcome = _run(runner.run())

            # DoomLoop 禁用，正常完成（thinking 被累积到 message，不触发 abort）
            assert outcome.status == "completed"
            assert llm.call_count == 1  # 只调用一次，没有 retry
        finally:
            store.close()

    def test_doom_loop_no_config_no_retry(self, tmp_path):
        """doom_loop_config=None → 不创建 detector，不触发 DoomLoop。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="s-doom-5"))
            _run(store.append_message("s-doom-5", Message(role="user", content="hi")))

            llm = _AlwaysDoomLLM(repeat_pattern="F" * 50, repeat_count=3)
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    # doom_loop_config 不注入
                ),
                config=RunnerConfig(session_id="s-doom-5"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert llm.call_count == 1
            assert runner._doom_loop_detector is None
        finally:
            store.close()

    def test_doom_loop_no_thinking_no_detection(self, tmp_path):
        """LLM 不返回 thinking_delta → DoomLoop 不生效（不降级到 text_delta）。"""
        store = _new_store(tmp_path)
        try:
            _run(store.create_session(session_id="s-doom-6"))
            _run(store.append_message("s-doom-6", Message(role="user", content="hi")))

            # MockLLM 只返回 text（无 thinking），即使 text 重复也不触发 DoomLoop
            repeat_text = "REPEAT" * 20
            llm = MockLLM(callable=lambda req: LLMResponse(
                content=repeat_text, stop_reason="end_turn", model="mock",
            ))
            cfg = DoomLoopConfig(
                tail_size=1000, min_repeat_len=50, repeat_threshold=3,
            )
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=llm, event_store=store,
                    tool_executor=MockToolExecutor(),
                    doom_loop_config=cfg,
                ),
                config=RunnerConfig(session_id="s-doom-6"),
            )
            outcome = _run(runner.run())

            assert outcome.status == "completed"
            assert llm.call_count == 1
        finally:
            store.close()

