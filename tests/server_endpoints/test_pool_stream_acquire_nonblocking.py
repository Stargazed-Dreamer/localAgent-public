"""pool.stream() 等槽位不得阻塞事件循环（回归测试）

背景（2026-09-01，inbound-gateway T6 验收时发现）：
stream() 在事件循环里同步调 _acquire()，而 _acquire 用
threading.Condition.wait() 阻塞等槽位（最长 300 秒）。并发流超过
provider max_concurrency 时，排队中的流把整个后端事件循环冻死
（Loop 任务 / MCP / /health 全停摆，py-spy 抓栈实锤）。

修复：_acquire 包进 asyncio.to_thread。

测试策略：真实池（单 key，max_concurrency=1），monkeypatch
_stream_openai_sse 为占槽 0.4s 的慢流。两路并发：A 先拿槽，
B 必须排队等槽；等待期间心跳协程每 10ms 计数。
- 修复前：B 的同步等待冻住事件循环 → A 无法推进 → 整体死锁到超时。
- 修复后：B 在工作线程等，心跳不停，A 完成后 B 接力，总时长 ≈0.85s。
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from server.llm_pool.pool import LLMPool  # noqa: E402
from server.llm_pool.types import LLMKey  # noqa: E402

HOLD_SECONDS = 0.4
HEARTBEAT_INTERVAL = 0.01


def _make_pool(tmp_path: Path) -> LLMPool:
    key = LLMKey(key="sk-test", base_url="https://api.test.com/v1",
                 models=["m1"], name="k1", max_concurrency=1)
    key.model_tiers = {"m1": 3}
    return LLMPool([key], stats_file=str(tmp_path / "stats.json"))


def test_stream_acquire_does_not_block_event_loop(tmp_path):
    """并发流排队等槽时，事件循环保持响应（心跳不停），且两路流先后完成。"""
    pool = _make_pool(tmp_path)

    async def slow_sse(**kwargs):
        await asyncio.sleep(HOLD_SECONDS)
        yield {"type": "text_delta", "delta": "x"}
        yield {"type": "usage", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        yield {"type": "done", "finish_reason": "stop"}

    pool._stream_openai_sse = lambda **kw: slow_sse(**kw)

    async def consume(label: str, out: list):
        events = []
        async for ev in pool.stream(messages=[{"role": "user", "content": label}],
                                    model="m1", project="test"):
            events.append(ev)
        out.append((label, events))

    async def run():
        ticks = 0
        heartbeat_stop = False

        async def heartbeat():
            nonlocal ticks
            while not heartbeat_stop:
                ticks += 1
                await asyncio.sleep(HEARTBEAT_INTERVAL)

        out: list = []
        hb = asyncio.create_task(heartbeat())
        task_a = asyncio.create_task(consume("A", out))
        await asyncio.sleep(0.05)  # 确保 A 先拿到唯一的槽
        task_b = asyncio.create_task(consume("B", out))
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)
        heartbeat_stop = True
        await hb
        return out, ticks

    out, ticks = asyncio.run(run())

    # 两路流都正常完成（B 排队后接力，而不是 no available keys）
    assert len(out) == 2
    for label, events in out:
        assert any(e.get("type") == "done" and e.get("finish_reason") == "stop"
                   for e in events), f"stream {label} 未完成: {events}"
        assert not any(e.get("type") == "provider_error" for e in events), \
            f"stream {label} 出现 provider_error: {events}"

    # B 等槽期间（≥0.35s）心跳至少应跳 15 次（10ms 间隔 → 理论 ~35 次）
    assert ticks >= 15, f"事件循环疑似被阻塞：{ticks} 次心跳（期望 ≥15）"
