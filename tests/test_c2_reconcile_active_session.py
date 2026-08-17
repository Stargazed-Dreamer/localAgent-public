"""C2 · reconcile 误判活跃 streaming session（red_green 测试，spec D7/D11）

测试目标：
1. 验证 reconcile 不会把"近期有 streaming 事件"的活跃 session 误标为 interrupted
2. 验证 reconcile 仍然能把"长期无事件"的陈旧 session 标为 interrupted

测试策略（spec D12）：red_green: required
- 红测试：mock 活跃 streaming session（最近 1 秒有 streaming_text_delta 事件），
  断言 reconcile 把它误标为 interrupted（修复前失败）
- 绿测试：修复后断言活跃 session 跳过 + 陈旧 session 仍标记

场景覆盖：
1. 活跃 streaming session（recent event < threshold）→ 不应标记 interrupted
2. 陈旧 streaming session（last event > threshold）→ 应标记 interrupted
3. 活跃 awaiting_tools session（recent event < threshold）→ 不应标记 interrupted
4. 边界：无事件的 streaming session → 应标记 interrupted（无可参考时间，按陈旧处理）
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.event_store import EventStore  # noqa: E402
from client.core.agent.reconciler import reconcile  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_store(tmp_path):
    return EventStore(db_path=str(tmp_path / "c2_reconcile.db"))


# ============================================================================
# 1. 活跃 streaming session（recent event < threshold）→ 不应标记 interrupted
# ============================================================================


class TestActiveStreamingSessionNotMarkedInterrupted:
    """C2: 活跃 streaming session（最近有 streaming_text_delta 事件）不应被 reconcile 标 interrupted。"""

    def test_active_streaming_session_skipped(self, tmp_path):
        """红测试：活跃 streaming session 应跳过，不标 interrupted。"""
        store = _make_store(tmp_path)
        store.init()
        try:
            _run(store.create_session(session_id="s_active"))
            _run(store.update_session_status("s_active", "streaming"))
            # 模拟最近写入的 streaming_text_delta 事件（1 秒前）
            _run(store.append_event(
                "s_active", "streaming_text_delta",
                {"delta": "hello"}, trace_id="t1",
            ))

            result = _run(reconcile(store))

            # 修复前：sessions_interrupted 含 s_active（误判）
            # 修复后：sessions_interrupted 不含 s_active
            assert "s_active" not in result.sessions_interrupted, (
                f"活跃 streaming session（近期有事件）不应被标记 interrupted，"
                f"实际 sessions_interrupted={result.sessions_interrupted}"
            )
            # session 状态仍是 streaming（未被改）
            session = _run(store.get_session("s_active"))
            assert session.status == "streaming", (
                f"活跃 session 状态应保持 streaming，实际={session.status}"
            )
        finally:
            store.close()


# ============================================================================
# 2. 陈旧 streaming session（last event > threshold）→ 应标记 interrupted
# ============================================================================


class TestStaleStreamingSessionMarkedInterrupted:
    """C2: 陈旧 streaming session（长期无事件）应被 reconcile 标 interrupted。"""

    def test_stale_streaming_session_marked(self, tmp_path):
        """绿测试：陈旧 streaming session 仍应被标记 interrupted。"""
        store = _make_store(tmp_path)
        store.init()
        try:
            _run(store.create_session(session_id="s_stale"))
            _run(store.update_session_status("s_stale", "streaming"))
            # 写一个陈旧的 streaming_text_delta 事件（人为调时间戳，模拟 5 分钟前）
            # 直接 SQL 改 events.created_at（append_event 不接受 created_at 参数）
            _run(store.append_event(
                "s_stale", "streaming_text_delta",
                {"delta": "old"}, trace_id="t2",
            ))
            # 把事件时间戳改为 5 分钟前
            old_ts = time.time() - 300
            store.conn.execute(
                "UPDATE events SET created_at = ? WHERE session_id = ? AND type = ?",
                (old_ts, "s_stale", "streaming_text_delta"),
            )
            # 同时把 session.updated_at 改为陈旧（reconcile 也参考此字段）
            store.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (old_ts, "s_stale"),
            )
            store.conn.commit()

            result = _run(reconcile(store))

            assert "s_stale" in result.sessions_interrupted, (
                f"陈旧 streaming session 应被标记 interrupted，"
                f"实际 sessions_interrupted={result.sessions_interrupted}"
            )
            session = _run(store.get_session("s_stale"))
            assert session.status == "interrupted"
        finally:
            store.close()


# ============================================================================
# 3. 活跃 awaiting_tools session → 不应标记 interrupted
# ============================================================================


class TestActiveAwaitingToolsSessionNotMarkedInterrupted:
    """C2: 活跃 awaiting_tools session（最近有事件）不应被 reconcile 标 interrupted。"""

    def test_active_awaiting_tools_session_skipped(self, tmp_path):
        """活跃 awaiting_tools session 应跳过。"""
        store = _make_store(tmp_path)
        store.init()
        try:
            _run(store.create_session(session_id="s_awaiting"))
            _run(store.update_session_status("s_awaiting", "awaiting_tools"))
            # 模拟最近写入的事件（runner 刚把 tool_calls 写完，准备执行）
            _run(store.append_event(
                "s_awaiting", "transition",
                {"reason": "next_turn", "iterations": 1},
            ))

            result = _run(reconcile(store))

            assert "s_awaiting" not in result.sessions_interrupted, (
                f"活跃 awaiting_tools session 不应被标记 interrupted，"
                f"实际 sessions_interrupted={result.sessions_interrupted}"
            )
            session = _run(store.get_session("s_awaiting"))
            assert session.status == "awaiting_tools"
        finally:
            store.close()


# ============================================================================
# 4. 边界：无事件的 streaming session → 应标记 interrupted（无可参考时间）
# ============================================================================


class TestNoEventsStreamingSessionMarkedInterrupted:
    """C2: streaming session 但无任何事件（边界场景）→ 应标记 interrupted。

    场景：session 状态被改为 streaming 但无任何 event 写入
    （理论上不应发生，但防御性处理：无事件视为陈旧，标记 interrupted）。
    """

    def test_no_events_streaming_session_marked(self, tmp_path):
        """无事件的 streaming session 应被标记 interrupted（无法判断活跃性）。"""
        store = _make_store(tmp_path)
        store.init()
        try:
            _run(store.create_session(session_id="s_no_events"))
            _run(store.update_session_status("s_no_events", "streaming"))
            # 不写任何 event

            result = _run(reconcile(store))

            assert "s_no_events" in result.sessions_interrupted, (
                f"无事件的 streaming session 应被标记 interrupted（无法判断活跃性），"
                f"实际 sessions_interrupted={result.sessions_interrupted}"
            )
        finally:
            store.close()
