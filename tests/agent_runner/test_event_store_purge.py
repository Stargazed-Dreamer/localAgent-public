"""8-11（2026-09-13 code review）验证测试：EventStore.purge_invalidated_events

背景：streaming 每 delta 一条 events 插入，会话结束后只标记 invalidated_seq
永不物理删除，agent.db 单调膨胀。purge_invalidated_events(before_ts) 提供
维护入口（DELETE 已 invalidated 行，返回删除数）。

覆盖：
1. 插入 → invalidate → purge（无 before_ts）→ 返回值与剩余行数正确
2. 未 invalidated 的行（活跃 streaming / 审计事件）不被删除
3. before_ts 边界：只删 created_at < before_ts 的已 invalidated 行，
   created_at >= before_ts 的已 invalidated 行保留
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from client.core.agent import EventStore  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


def _count_events(store: EventStore, session_id: str, invalidated_only: bool) -> int:
    """统计 events 行数（invalidated_only=True 时只统计已 invalidated 行）。"""
    where = "invalidated_seq IS NOT NULL" if invalidated_only else "1=1"
    row = store.conn.execute(
        f"SELECT COUNT(*) AS n FROM events WHERE session_id = ? AND {where}",
        (session_id,),
    ).fetchone()
    return int(row["n"])


def test_purge_deletes_invalidated_and_returns_count(store):
    """插入 → invalidate → purge → 返回删除数，且行被物理删除。"""
    async def run():
        await store.create_session(session_id="s_purge")
        # 模拟 streaming 增量：5 条 delta（append_event 返回 seq）
        seqs = []
        for i in range(5):
            seq = await store.append_event(
                "s_purge", "streaming_text_delta", {"delta": f"d{i}"},
                trace_id="t1",
            )
            seqs.append(seq)
        assert _count_events(store, "s_purge", invalidated_only=False) == 6  # +session_created

        # 合并完成 → 标记 invalidated（reconciler / runner 收尾的真实路径）
        await store.invalidate_events("s_purge", seqs)

        deleted = await store.purge_invalidated_events()
        # 至少删掉 5 条 streaming 行（session_created 审计事件未 invalidated，保留）
        assert deleted == 5, f"应删除 5 条已 invalidated 行，实际 {deleted}"
        # 已 invalidated 行数归零
        assert _count_events(store, "s_purge", invalidated_only=True) == 0
        # 审计事件仍在
        assert _count_events(store, "s_purge", invalidated_only=False) == 1

    _run(run())


def test_purge_keeps_non_invalidated_events(store):
    """未 invalidated 的行（活跃 streaming + 审计事件）不被删除。"""
    async def run():
        await store.create_session(session_id="s_keep")
        # 一条已 invalidated（合并完的旧流）
        seq_old = await store.append_event(
            "s_keep", "streaming_text_delta", {"delta": "old"}, trace_id="t1",
        )
        await store.invalidate_events("s_keep", [seq_old])
        # 一条未 invalidated（模拟仍在写入的活跃流）
        await store.append_event(
            "s_keep", "streaming_text_delta", {"delta": "live"}, trace_id="t2",
        )

        deleted = await store.purge_invalidated_events()

        assert deleted == 1, f"只应删除 1 条已 invalidated 行，实际 {deleted}"
        # 活跃流 + session_created 仍在
        row = store.conn.execute(
            "SELECT type FROM events WHERE session_id = ? AND invalidated_seq IS NULL",
            ("s_keep",),
        ).fetchall()
        types = sorted(r["type"] for r in row)
        assert types == ["session_created", "streaming_text_delta"], (
            f"未 invalidated 的事件应保留，实际 {types}"
        )

    _run(run())


def test_purge_before_ts_boundary(store):
    """before_ts 边界：只删 created_at < before_ts 的已 invalidated 行。"""
    async def run():
        await store.create_session(session_id="s_ts")
        seq1 = await store.append_event(
            "s_ts", "streaming_text_delta", {"delta": "a"}, trace_id="t1",
        )
        seq2 = await store.append_event(
            "s_ts", "streaming_text_delta", {"delta": "b"}, trace_id="t1",
        )
        await store.invalidate_events("s_ts", [seq1, seq2])
        # 把 seq1 的 created_at 拨到过去，seq2 保持当前时间
        old_ts = 1000.0
        store.conn.execute(
            "UPDATE events SET created_at = ? WHERE session_id = ? AND seq = ?",
            (old_ts, "s_ts", seq1),
        )
        store.conn.commit()

        # 边界 1：before_ts <= old_ts → 什么都不删（created_at < before_ts 不成立）
        deleted = await store.purge_invalidated_events(before_ts=old_ts)
        assert deleted == 0, f"before_ts=created_at 时不应删除（严格小于），实际 {deleted}"

        # 边界 2：old_ts < before_ts <= now → 只删旧行
        deleted = await store.purge_invalidated_events(before_ts=old_ts + 1.0)
        assert deleted == 1, f"应只删除 created_at < before_ts 的 1 行，实际 {deleted}"
        # 较新的已 invalidated 行保留（仍可被 load_events 回放）
        kept = store.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE session_id = ? AND seq = ? "
            "AND invalidated_seq IS NOT NULL",
            ("s_ts", seq2),
        ).fetchone()
        assert int(kept["n"]) == 1, "较新的已 invalidated 行应保留"

        # 边界 3：不设 before_ts → 清掉剩余行
        deleted = await store.purge_invalidated_events()
        assert deleted == 1
        assert _count_events(store, "s_ts", invalidated_only=True) == 0

    _run(run())
