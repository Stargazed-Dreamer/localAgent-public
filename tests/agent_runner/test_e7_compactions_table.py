"""E7 · compactions 表（spec D24）。

D24 决策：做 compactions 表，不做 E8 CompletionBoundary。
schema：id/session_id/source_seq/summary/recent_json/prompt_hash/created_at

测试策略（regression_only，spec 标注）：
1. EventStore: compactions 表存在 + 字段完整 + schema 版本 9
2. append_compaction 写入 + load_compactions 读取
3. runner 触发 reactive_compact 后 compactions 表有记录
4. 多次压缩（同 session 多次）顺序正确
5. compaction_occurred=False 时不写记录（skip / fail-open）
"""

from __future__ import annotations

import asyncio
import json
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
from client.core.agent.compactor import Compactor  # noqa: E402
from client.core.agent.types import (  # noqa: E402
    LLMRequest,
    LLMResponse,
    Message,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_e7.db"))
    store.init()
    return store


# ============================================================================
# Part 1: EventStore schema
# ============================================================================


class TestCompactionsTable:
    """compactions 表存在 + 字段完整 + schema 版本 9。"""

    def test_table_exists_after_init(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            cur = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='compactions'"
            )
            assert cur.fetchone() is not None, "E7: compactions 表应存在"
        finally:
            store.close()

    def test_table_columns(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            cols = {r[1] for r in store.conn.execute(
                "PRAGMA table_info(compactions)"
            ).fetchall()}
            expected = {
                "id", "session_id", "source_seq", "summary",
                "recent_json", "prompt_hash", "created_at",
            }
            assert expected.issubset(cols), (
                f"E7: compactions 缺字段，expected={expected}, got={set(cols)}"
            )
        finally:
            store.close()

    def test_schema_version_is_9(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            row = store.conn.execute(
                "SELECT value FROM schema_info WHERE key='version'"
            ).fetchone()
            assert int(row[0]) >= 9, f"E7: schema 版本应 >= 9, got {row[0]}"
        finally:
            store.close()


# ============================================================================
# Part 2: append / load
# ============================================================================


class TestAppendLoadCompactions:
    """append_compaction 写入 + load_compactions 读取。"""

    def test_append_and_load(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s1")
                cid = await store.append_compaction(
                    session_id="s1",
                    source_seq=3,
                    summary="summary of messages 1-10",
                    recent_json=json.dumps([{"role": "user", "content": "tail1"}]),
                    prompt_hash="abc123",
                )
                assert cid > 0, "append_compaction 应返回 id"

                records = await store.load_compactions("s1")
                assert len(records) == 1
                r = records[0]
                assert r["session_id"] == "s1"
                assert r["source_seq"] == 3
                assert r["summary"] == "summary of messages 1-10"
                assert "tail1" in r["recent_json"]
                assert r["prompt_hash"] == "abc123"
                assert r["created_at"] > 0
            _run(run())
        finally:
            store.close()

    def test_multiple_compactions_ordered_by_id(self, tmp_path):
        """同 session 多次压缩 → load 按 id 升序返回。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s2")
                await store.append_compaction(
                    session_id="s2", source_seq=2,
                    summary="first compaction",
                    recent_json="[]", prompt_hash="h1",
                )
                await store.append_compaction(
                    session_id="s2", source_seq=5,
                    summary="second compaction",
                    recent_json="[]", prompt_hash="h2",
                )
                records = await store.load_compactions("s2")
                assert len(records) == 2
                assert records[0]["summary"] == "first compaction"
                assert records[1]["summary"] == "second compaction"
                assert records[0]["id"] < records[1]["id"]
            _run(run())
        finally:
            store.close()

    def test_load_empty_for_no_record(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s3")
                records = await store.load_compactions("s3")
                assert records == []
            _run(run())
        finally:
            store.close()

    def test_filter_by_session(self, tmp_path):
        """不同 session 的 compactions 不互相干扰。"""
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s4a")
                await store.create_session(session_id="s4b")
                await store.append_compaction(
                    session_id="s4a", source_seq=1,
                    summary="a summary", recent_json="[]", prompt_hash="ha",
                )
                await store.append_compaction(
                    session_id="s4b", source_seq=1,
                    summary="b summary", recent_json="[]", prompt_hash="hb",
                )
                a_records = await store.load_compactions("s4a")
                b_records = await store.load_compactions("s4b")
                assert len(a_records) == 1
                assert len(b_records) == 1
                assert a_records[0]["summary"] == "a summary"
                assert b_records[0]["summary"] == "b summary"
            _run(run())
        finally:
            store.close()


# ============================================================================
# Part 3: runner reactive_compact 后写 compactions 表
# ============================================================================


class _CompactionTriggerLLM:
    """LLM mock：第一次 call 抛 ContextOverflow（413），第二次（压缩后）正常返回。"""

    def __init__(self):
        self.call_count = {"n": 0}
        self.summary_call_count = {"n": 0}

    async def call(self, request: LLMRequest) -> LLMResponse:
        self.call_count["n"] += 1
        # 检测是否是压缩请求（system prompt 含 "Summarize"）
        system_text = request.system or ""
        if "Summarize" in system_text or "summarize" in system_text.lower():
            self.summary_call_count["n"] += 1
            return LLMResponse(
                content="这是上下文摘要。",
                stop_reason="stop",
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
        # 第一次主 LLM 调用 → 抛 ContextOverflow
        if self.call_count["n"] == 1:
            from client.core.agent.compactor import ContextOverflow
            raise ContextOverflow(
                "simulated 413 context overflow",
                status_code=413,
            )
        # 第二次（压缩后重试）→ 正常返回
        return LLMResponse(
            content="done after compaction",
            stop_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        )


class TestRunnerWritesCompaction:
    """runner 触发 reactive_compact 成功后，compactions 表应有记录。"""

    def test_reactive_compact_writes_record(self, tmp_path):
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_compact")
                # 写入 > tail_keep 条消息触发压缩
                for i in range(15):
                    await store.append_message("s_compact", Message(
                        role="user" if i % 2 == 0 else "assistant",
                        content=f"message {i} " * 100,  # 长 content 加速触发
                        source="user" if i % 2 == 0 else "assistant",
                    ))

                llm = _CompactionTriggerLLM()
                compactor = Compactor(max_context_tokens=1000, tail_keep=4)

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        compactor=compactor,
                    ),
                    config=RunnerConfig(
                        session_id="s_compact", max_iterations=5,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()
                # 压缩成功 → completed
                assert outcome.status == "completed", f"expected completed, got {outcome.status}"

                # E7: compactions 表应有记录
                records = await store.load_compactions("s_compact")
                assert len(records) >= 1, (
                    f"E7: reactive_compact 成功后应写 compactions 表，got {len(records)} records"
                )
                r = records[0]
                assert r["session_id"] == "s_compact"
                assert r["source_seq"] >= 1
                assert "摘要" in r["summary"] or r["summary"]  # 含摘要内容
                assert r["prompt_hash"]  # 非空
                assert r["recent_json"]  # 非空（tail messages）
            _run(run())
        finally:
            store.close()

    def test_compaction_skipped_no_record(self, tmp_path):
        """compaction_occurred=False（messages 太少）时不写 compactions 记录。"""

        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s_skip")
                # 只写 1 条消息，不会触发压缩（len <= tail_keep）
                await store.append_message("s_skip", Message(
                    role="user", content="hi", source="user",
                ))

                # LLM 直接正常返回
                llm = MockLLM(default_text="hello")
                compactor = Compactor(max_context_tokens=1000, tail_keep=4)

                runner = SessionRunner(
                    deps=RunnerDeps(
                        llm_gateway=llm,
                        event_store=store,
                        compactor=compactor,
                    ),
                    config=RunnerConfig(
                        session_id="s_skip", max_iterations=3,
                        use_stream=False,
                    ),
                )
                outcome = await runner.run()
                assert outcome.status == "completed"

                # E7: 未触发压缩 → compactions 表应无记录
                records = await store.load_compactions("s_skip")
                assert len(records) == 0, (
                    f"E7: 未触发压缩时不应写 compactions 表，got {len(records)} records"
                )
            _run(run())
        finally:
            store.close()
