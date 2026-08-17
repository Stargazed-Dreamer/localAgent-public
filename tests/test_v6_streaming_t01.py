"""v6-lite-streaming-gui T01 单元测试：EventStore streaming 事件 + reconciler 合并

测试范围（spec D06）：
- EventStore 新增 streaming 事件类型支持
- EventStore.invalidate_events 标记
- EventStore.load_streaming_events 查询
- reconciler 合并 streaming_text_delta 序列为完整 Message
- reconciler 合并 streaming_thinking_delta
- reconciler 合并 streaming_tool_call → tool_calls 表
- 合并后 streaming 事件 invalidated_seq 已标记
- Message.thinking 字段存储/读取
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 确保项目根在 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.event_store import EventStore  # noqa: E402
from client.core.agent.reconciler import reconcile  # noqa: E402
from client.core.agent.types import Message  # noqa: E402


def _make_store(tmp_dir: str) -> EventStore:
    """创建临时 DB 的 EventStore"""
    db_path = os.path.join(tmp_dir, "test_agent.db")
    store = EventStore(db_path)
    store.init()
    return store


def test_append_streaming_events():
    """streaming 事件能 append 到 EventStore"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1", title="test")
            # append streaming events
            await store.append_event("s1", "streaming_text_delta", {"delta": "Hello"})
            await store.append_event("s1", "streaming_text_delta", {"delta": " world"})
            await store.append_event("s1", "streaming_thinking_delta", {"delta": "thinking..."})
            await store.append_event("s1", "streaming_tool_call", {"tool_call": {"id": "tc1", "function": {"name": "foo", "arguments": "{}"}}})

            events = await store.load_events("s1")
            # session_created + 4 streaming = 5 events
            assert len(events) == 5
            streaming = [e for e in events if e.type.startswith("streaming_")]
            assert len(streaming) == 4

        asyncio.run(run())
        store.close()


def test_load_streaming_events():
    """load_streaming_events 只返回未 invalidated 的 streaming 事件"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            await store.append_event("s1", "streaming_text_delta", {"delta": "a"})
            await store.append_event("s1", "streaming_text_delta", {"delta": "b"})
            await store.append_event("s1", "session_status_changed", {"status": "streaming"})

            # 加载 streaming events
            events = await store.load_streaming_events("s1")
            assert len(events) == 2
            assert all(e.type.startswith("streaming_") for e in events)

            # invalidate 第一个
            await store.invalidate_events("s1", [events[0].seq])
            # 再加载，应该只有 1 个
            events2 = await store.load_streaming_events("s1")
            assert len(events2) == 1

        asyncio.run(run())
        store.close()


def test_invalidate_events():
    """invalidate_events 标记 invalidated_seq"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            seq1 = await store.append_event("s1", "streaming_text_delta", {"delta": "a"})
            seq2 = await store.append_event("s1", "streaming_text_delta", {"delta": "b"})
            seq3 = await store.append_event("s1", "streaming_text_delta", {"delta": "c"})

            # invalidate seq1 和 seq2
            await store.invalidate_events("s1", [seq1, seq2])

            events = await store.load_events("s1")
            # session_created(1) + 3 streaming = 4 events
            assert len(events) == 4
            ev1 = [e for e in events if e.seq == seq1][0]
            ev2 = [e for e in events if e.seq == seq2][0]
            ev3 = [e for e in events if e.seq == seq3][0]
            assert ev1.invalidated_seq is not None
            assert ev2.invalidated_seq is not None
            assert ev3.invalidated_seq is None

        asyncio.run(run())
        store.close()


def test_message_thinking_field():
    """Message.thinking 字段存储和读取"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            msg = Message(
                role="assistant",
                content="answer",
                source="assistant",
                thinking="my thinking process",
            )
            await store.append_message("s1", msg)

            messages = await store.load_messages("s1")
            assert len(messages) == 1
            assert messages[0].thinking == "my thinking process"

        asyncio.run(run())
        store.close()


def test_message_thinking_empty():
    """Message.thinking 为空时正常存储和读取"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            msg = Message(
                role="assistant",
                content="answer",
                source="assistant",
            )
            await store.append_message("s1", msg)

            messages = await store.load_messages("s1")
            assert len(messages) == 1
            assert messages[0].thinking == ""

        asyncio.run(run())
        store.close()


def test_reconciler_merges_streaming_text():
    """reconciler 合并 streaming_text_delta 序列为完整 Message"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1", title="test")
            # 模拟进程崩溃：写 streaming events 但没写完整 Message
            await store.append_event("s1", "streaming_text_delta", {"delta": "Hello"}, trace_id="t1")
            await store.append_event("s1", "streaming_text_delta", {"delta": " world"}, trace_id="t1")

            # reconcile
            result = await reconcile(store)
            assert result.streaming_messages_merged == 1

            # 完整 Message 已写入
            messages = await store.load_messages("s1")
            assert len(messages) == 1
            assert messages[0].role == "assistant"
            assert messages[0].content == "Hello world"
            assert messages[0].source == "assistant"

            # streaming events 已 invalidated
            streaming = await store.load_streaming_events("s1")
            assert len(streaming) == 0

        asyncio.run(run())
        store.close()


def test_reconciler_merges_streaming_thinking():
    """reconciler 合并 streaming_thinking_delta"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            await store.append_event("s1", "streaming_text_delta", {"delta": "answer"}, trace_id="t1")
            await store.append_event("s1", "streaming_thinking_delta", {"delta": "step1 "}, trace_id="t1")
            await store.append_event("s1", "streaming_thinking_delta", {"delta": "step2"}, trace_id="t1")

            result = await reconcile(store)
            assert result.streaming_messages_merged == 1

            messages = await store.load_messages("s1")
            assert len(messages) == 1
            assert messages[0].content == "answer"
            assert messages[0].thinking == "step1 step2"

        asyncio.run(run())
        store.close()


def test_reconciler_merges_streaming_tool_calls():
    """reconciler 合并 streaming_tool_call → tool_calls 表"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            tc1 = {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
            tc2 = {"id": "call_2", "type": "function", "function": {"name": "bar", "arguments": "{}"}}
            await store.append_event("s1", "streaming_text_delta", {"delta": "calling tools"}, trace_id="t1")
            await store.append_event("s1", "streaming_tool_call", {"tool_call": tc1}, trace_id="t1")
            await store.append_event("s1", "streaming_tool_call", {"tool_call": tc2}, trace_id="t1")

            result = await reconcile(store)
            assert result.streaming_messages_merged == 1

            # Message 有 tool_calls
            messages = await store.load_messages("s1")
            assert len(messages) == 1
            assert len(messages[0].tool_calls) == 2

            # tool_calls 表有记录（status=interrupted）
            from client.core.agent.types import TOOL_STATUS_INTERRUPTED
            tool_calls = await store.load_tool_calls("s1")
            assert len(tool_calls) == 2
            assert all(tc.status == TOOL_STATUS_INTERRUPTED for tc in tool_calls)

        asyncio.run(run())
        store.close()


def test_reconciler_groups_by_trace_id():
    """reconciler 按 trace_id 分组合并"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            # 第一组 streaming
            await store.append_event("s1", "streaming_text_delta", {"delta": "first"}, trace_id="t1")
            # 第二组 streaming
            await store.append_event("s1", "streaming_text_delta", {"delta": "second"}, trace_id="t2")

            result = await reconcile(store)
            assert result.streaming_messages_merged == 2

            messages = await store.load_messages("s1")
            assert len(messages) == 2
            contents = [m.content for m in messages]
            assert "first" in contents
            assert "second" in contents

        asyncio.run(run())
        store.close()


def test_reconciler_idempotent():
    """reconciler 幂等：再次运行不重复合并"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            await store.append_event("s1", "streaming_text_delta", {"delta": "hello"}, trace_id="t1")

            # 第一次 reconcile
            result1 = await reconcile(store)
            assert result1.streaming_messages_merged == 1

            # 第二次 reconcile（幂等）
            result2 = await reconcile(store)
            assert result2.streaming_messages_merged == 0

            messages = await store.load_messages("s1")
            assert len(messages) == 1

        asyncio.run(run())
        store.close()


def test_reconciler_empty_streaming_events():
    """无 streaming 事件时 reconcile 无操作"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)

        async def run():
            await store.create_session("s1")
            result = await reconcile(store)
            assert result.streaming_messages_merged == 0
            assert result.total_actions == 0

        asyncio.run(run())
        store.close()


def test_schema_migration_thinking_json():
    """旧 DB（无 thinking_json 列）迁移后支持 thinking"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_migration.db")

        # 创建旧 schema DB（v3，无 thinking_json）
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, mode TEXT DEFAULT 'dialogue',
                status TEXT DEFAULT 'idle', created_at REAL, updated_at REAL,
                prompt_index INTEGER DEFAULT 0, last_compaction_prompt_index INTEGER
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                role TEXT NOT NULL, content_json TEXT NOT NULL, tool_call_id TEXT,
                source TEXT DEFAULT 'user', visible INTEGER DEFAULT 1, created_at REAL,
                tool_calls_json TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE TABLE events (
                seq INTEGER, session_id TEXT NOT NULL, type TEXT NOT NULL,
                payload_json TEXT, trace_id TEXT, prompt_index INTEGER,
                invalidated_seq INTEGER, parent_trace_id TEXT, depth INTEGER DEFAULT 0,
                created_at REAL, PRIMARY KEY (session_id, seq)
            );
            CREATE TABLE tool_calls (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                name TEXT NOT NULL, args_json TEXT, safety TEXT DEFAULT 'read_only',
                status TEXT DEFAULT 'pending', started_at REAL, ended_at REAL, trace_id TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO schema_info VALUES('version', '3');
        """)
        conn.commit()
        conn.close()

        # 用 EventStore 打开（触发迁移）
        store = EventStore(db_path)
        store.init()

        # 验证 thinking_json 列已添加
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "thinking_json" in cols

        # 验证 thinking 字段可写入和读取
        async def run():
            await store.create_session("s1")
            msg = Message(role="assistant", content="hi", source="assistant", thinking="thoughts")
            await store.append_message("s1", msg)
            messages = await store.load_messages("s1")
            assert messages[0].thinking == "thoughts"

        asyncio.run(run())
        store.close()


if __name__ == "__main__":
    import traceback
    tests = [
        test_append_streaming_events,
        test_load_streaming_events,
        test_invalidate_events,
        test_message_thinking_field,
        test_message_thinking_empty,
        test_reconciler_merges_streaming_text,
        test_reconciler_merges_streaming_thinking,
        test_reconciler_merges_streaming_tool_calls,
        test_reconciler_groups_by_trace_id,
        test_reconciler_idempotent,
        test_reconciler_empty_streaming_events,
        test_schema_migration_thinking_json,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
