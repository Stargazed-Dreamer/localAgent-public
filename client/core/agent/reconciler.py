"""v6-lite 启动 reconciliation（T06d）

设计依据：
- v6-lite §3 W3：启动 reconciliation——streaming/running → interrupted；
  yieldMissingToolResultBlocks——重启后为 dangling tool_use 补 is_error=true 的 tool_result
  （防 provider 400，cc_src 真实踩坑）
- v6-lite §4.2：yieldMissingToolResultBlocks——abort/重启后为 dangling tool_use 补 error result
- v6-01 §3：启动恢复（reconciliation）
- v6-lite-streaming-gui T01 D06：streaming 事件合并为完整 Message

职责：
1. 扫描 sessions status=streaming/awaiting_tools → 标记 interrupted（进程异常退出时残留的活跃会话）
2. 扫描 tool_calls status=pending/running →
   a. 若无对应 role=tool 结果消息 → 补 is_error=true 的 tool_result（yieldMissingToolResultBlocks）
   b. 标记 tool_call status=interrupted
3. 扫描未合并的 streaming 事件 → 合并为完整 Message → 标记 invalidated（v6-lite-streaming-gui T01）

幂等性：可重复运行（已 interrupted 的 session 跳过；已有 tool_result 的 tool_call 跳过；
已 invalidated 的 streaming 事件跳过）。

用法：
    store = EventStore(db_path)
    store.init()
    result = await reconcile(store)
    # result.sessions_interrupted / result.tool_calls_interrupted / result.tool_results_filled
    # result.streaming_messages_merged
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from client.core.agent.event_store import EventStore
from client.core.agent.types import (
    TOOL_STATUS_INTERRUPTED,
    TOOL_STATUS_PENDING,
    TOOL_STATUS_RUNNING,
    Message,
    ToolCall,
    ToolResult,
)

logger = logging.getLogger("localagent.agent.reconciler")

# 进程异常退出时残留的"活跃"会话状态（需标记为 interrupted）
_ACTIVE_SESSION_STATUSES = ("streaming", "awaiting_tools")

# 悬挂的 tool_call 状态（进程退出时正在执行或待执行）
_DANGLING_TOOL_CALL_STATUSES = [TOOL_STATUS_PENDING, TOOL_STATUS_RUNNING]

# 补 tool_result 时使用的错误消息（模型看到后可改道）
_INTERRUPTED_TOOL_RESULT_CONTENT = (
    "Tool call interrupted: session was active when the process restarted. "
    "Please retry or choose a different approach."
)

# streaming 事件类型常量（v6-lite-streaming-gui T01 D06）
_STREAMING_TEXT_DELTA = "streaming_text_delta"
_STREAMING_THINKING_DELTA = "streaming_thinking_delta"
_STREAMING_TOOL_CALL = "streaming_tool_call"

# C2（spec D7/D11）：reconcile 活跃性判断阈值
# 若 session 最近事件距今 < 此秒数，视为仍活跃（runner 可能还在跑），跳过不标 interrupted
# 默认 60 秒：streaming 事件通常每秒多个，60 秒无事件可认为 runner 已卡死/退出
RECONCILE_ACTIVE_THRESHOLD_SECS = 60.0


@dataclass
class ReconcileResult:
    """reconcile() 的返回值，记录本次恢复做了什么。"""

    sessions_interrupted: list[str] = field(default_factory=list)
    tool_calls_interrupted: list[str] = field(default_factory=list)
    tool_results_filled: list[str] = field(default_factory=list)  # tool_call_id list
    streaming_messages_merged: int = 0  # v6-lite-streaming-gui T01: 合并的 streaming Message 数
    skipped_sessions_already_interrupted: int = 0
    skipped_tool_calls_already_has_result: int = 0
    # C2：跳过的近期活跃 session 数（runner 可能还在跑）
    skipped_sessions_active_recent: int = 0

    @property
    def total_actions(self) -> int:
        return (
            len(self.sessions_interrupted)
            + len(self.tool_calls_interrupted)
            + len(self.tool_results_filled)
            + self.streaming_messages_merged
        )


async def reconcile(store: EventStore, *, force: bool = False) -> ReconcileResult:
    """启动恢复：扫描残留活跃会话 + 悬挂 tool_calls + 未合并 streaming 事件。

    幂等：可重复运行，已处理的跳过。

    Args:
        store: 已 init() 的 EventStore
        force: True = 跳过活跃性检查，强制标记所有 streaming/awaiting_tools session
               （用于测试 / 用户手动强制清理；生产启动恢复用默认 False，
               避免误判正在跑的 session）

    Returns:
        ReconcileResult：本次恢复的操作摘要
    """
    result = ReconcileResult()
    now = time.time()

    # ------------------------------------------------------------------
    # 1. 扫描残留活跃会话 → 标记 interrupted
    #    C2（spec D7/D11）：跳过近期有事件的活跃 session（runner 可能还在跑）
    # ------------------------------------------------------------------
    active_sessions: list[str] = []
    for status in _ACTIVE_SESSION_STATUSES:
        sessions = await store.list_sessions_by_status(status)
        active_sessions.extend(s.id for s in sessions)

    for session_id in active_sessions:
        # 跳过已经是 interrupted 的（幂等，理论上 list_sessions_by_status 不会返回）
        session = await store.get_session(session_id)
        if session is None:
            continue
        if session.status == "interrupted":
            result.skipped_sessions_already_interrupted += 1
            continue
        # C2：检查最近 runner-activity 事件时间，判断 session 是否仍活跃
        # - force=True → 跳过活跃性检查，直接标 interrupted（测试/手动清理用）
        # - 无活跃事件 → 视为陈旧（异常场景，理论不应发生），标 interrupted
        # - 最近活跃事件距今 < RECONCILE_ACTIVE_THRESHOLD_SECS → 视为活跃，跳过
        # - 最近活跃事件距今 >= 阈值 → 视为残留（runner 已退出/卡死），标 interrupted
        if not force:
            last_event_ts = await store.get_last_event_time(session_id)
            if last_event_ts is not None:
                idle_secs = now - last_event_ts
                if idle_secs < RECONCILE_ACTIVE_THRESHOLD_SECS:
                    result.skipped_sessions_active_recent += 1
                    logger.info(
                        "Reconcile skipped active session %s: last event %.1fs ago < threshold %.0fs",
                        session_id, idle_secs, RECONCILE_ACTIVE_THRESHOLD_SECS,
                    )
                    continue
        else:
            last_event_ts = await store.get_last_event_time(session_id)
        await store.update_session_status(session_id, "interrupted")
        await store.append_event(
            session_id, "session_reconciled",
            {
                "from_status": session.status,
                "to_status": "interrupted",
                "reason": "startup_reconciliation",
                "ts": now,
                "idle_secs": (now - last_event_ts) if last_event_ts else None,
                "force": force,
            },
        )
        result.sessions_interrupted.append(session_id)
        logger.info(
            "Reconciled session %s: %s → interrupted (force=%s)",
            session_id, session.status, force,
        )

    # ------------------------------------------------------------------
    # 2. 扫描悬挂 tool_calls → 补 tool_result + 标记 interrupted
    # ------------------------------------------------------------------
    dangling_calls = await store.list_tool_calls_by_status(_DANGLING_TOOL_CALL_STATUSES)

    for tc in dangling_calls:
        # 检查是否已有 tool_result 消息（幂等：已有则跳过补消息）
        has_result = await store.has_tool_result(tc.id)
        if has_result:
            result.skipped_tool_calls_already_has_result += 1
            # 但仍需确保 tool_call 状态是 interrupted（可能 pending/running 但有结果，
            # 理论上不应发生，但防御性处理）
            if tc.status in _DANGLING_TOOL_CALL_STATUSES:
                await store.update_tool_call_status(
                    tc.id, TOOL_STATUS_INTERRUPTED, ended_at=now,
                )
                result.tool_calls_interrupted.append(tc.id)
            continue

        # 补 is_error=true 的 tool_result（yieldMissingToolResultBlocks，防 provider 400）
        error_result = ToolResult(
            tool_call_id=tc.id,
            content=_INTERRUPTED_TOOL_RESULT_CONTENT,
            is_error=True,
            created_at=now,
        )
        await store.append_tool_result_message(tc.session_id, error_result)
        result.tool_results_filled.append(tc.id)

        # 标记 tool_call 状态为 interrupted
        await store.update_tool_call_status(
            tc.id, TOOL_STATUS_INTERRUPTED, ended_at=now,
        )
        result.tool_calls_interrupted.append(tc.id)

        logger.info(
            "Reconciled tool_call %s (session %s): %s → interrupted, filled error result",
            tc.id, tc.session_id, tc.status,
        )

    # ------------------------------------------------------------------
    # 3. 扫描未合并的 streaming 事件 → 合并为完整 Message（v6-lite-streaming-gui T01 D06）
    # ------------------------------------------------------------------
    await _reconcile_streaming_events(store, result, now)

    if result.total_actions > 0 or result.skipped_sessions_active_recent > 0:
        logger.info(
            "Reconciliation complete: %d sessions interrupted, %d tool_calls interrupted, "
            "%d tool_results filled, %d streaming messages merged, "
            "%d sessions skipped (already interrupted), %d tool_calls skipped, "
            "%d sessions skipped (active recent)",
            len(result.sessions_interrupted),
            len(result.tool_calls_interrupted),
            len(result.tool_results_filled),
            result.streaming_messages_merged,
            result.skipped_sessions_already_interrupted,
            result.skipped_tool_calls_already_has_result,
            result.skipped_sessions_active_recent,
        )
    else:
        logger.info("Reconciliation complete: no actions needed")

    return result


async def _reconcile_streaming_events(
    store: EventStore, result: ReconcileResult, now: float,
) -> None:
    """合并未合并的 streaming 事件为完整 Message（v6-lite-streaming-gui T01 D06）。

    流程：
    1. 遍历所有 session 的未合并 streaming 事件
    2. 按 trace_id 分组（trace_id 为 None 时按 session_id 分组）
    3. 每组合并 text_delta → 完整 text, thinking_delta → 完整 thinking, tool_call → tool_calls 列表
    4. 写完整 assistant Message（含 thinking + tool_calls）
    5. tool_calls 写入 tool_calls 表（status=interrupted，因为未执行）
    6. 标记 streaming 事件 invalidated
    """
    # 收集所有有未合并 streaming 事件的 session_id
    # 精确匹配 3 种 streaming 增量类型，避免误捞审计事件 `streaming_events_merged`
    # T06：DB 操作走 _run_sync（asyncio.to_thread + Lock），避免阻塞 event loop + 跨线程不安全
    def _collect_streaming_sessions():
        return store.conn.execute(
            "SELECT DISTINCT session_id FROM events "
            "WHERE invalidated_seq IS NULL "
            "AND type IN ('streaming_text_delta', 'streaming_thinking_delta', 'streaming_tool_call')",
        ).fetchall()
    rows = await store._run_sync(_collect_streaming_sessions)
    session_ids = [r["session_id"] for r in rows]

    for session_id in session_ids:
        events = await store.load_streaming_events(session_id)
        if not events:
            continue

        # 按 trace_id 分组（None 作为独立组）
        groups: dict[str | None, list] = defaultdict(list)
        for ev in events:
            groups[ev.trace_id].append(ev)

        for trace_id, group_events in groups.items():
            merged_text = ""
            merged_thinking = ""
            tool_calls_data: list[dict] = []
            seqs_to_invalidate: list[int] = []

            for ev in group_events:
                seqs_to_invalidate.append(ev.seq)
                if ev.type == _STREAMING_TEXT_DELTA:
                    merged_text += ev.payload.get("delta", "")
                elif ev.type == _STREAMING_THINKING_DELTA:
                    merged_thinking += ev.payload.get("delta", "")
                elif ev.type == _STREAMING_TOOL_CALL:
                    tc = ev.payload.get("tool_call")
                    if tc:
                        tool_calls_data.append(tc)

            # 只有有内容时才写 Message（避免空 Message）
            if not merged_text and not merged_thinking and not tool_calls_data:
                # 仍标记 invalidated（清理孤儿事件）
                await store.invalidate_events(session_id, seqs_to_invalidate)
                continue

            # 写完整 assistant Message
            msg = Message(
                role="assistant",
                content=merged_text,
                source="assistant",
                visible=True,
                tool_calls=tool_calls_data,
                thinking=merged_thinking,
            )
            appended = await store.append_message(session_id, msg)

            # tool_calls 写入 tool_calls 表（status=interrupted，未执行就崩溃）
            for tc_dict in tool_calls_data:
                tc = ToolCall.from_openai_tool_call(tc_dict, session_id=session_id)
                tc.status = TOOL_STATUS_INTERRUPTED
                tc.ended_at = now
                try:
                    await store.append_tool_call(session_id, tc)
                    # append_tool_call 默认 status=pending，需更新为 interrupted
                    await store.update_tool_call_status(
                        tc.id, TOOL_STATUS_INTERRUPTED, ended_at=now,
                    )
                except Exception:
                    logger.warning(
                        "Failed to write tool_call %s during streaming reconciliation",
                        tc.id, exc_info=True,
                    )

            # 标记 streaming 事件 invalidated
            await store.invalidate_events(session_id, seqs_to_invalidate)

            # 写审计 event
            await store.append_event(
                session_id, "streaming_events_merged",
                {
                    "message_id": appended.id,
                    "message_seq": appended.seq,
                    "text_len": len(merged_text),
                    "thinking_len": len(merged_thinking),
                    "tool_calls_count": len(tool_calls_data),
                    "streaming_events_count": len(seqs_to_invalidate),
                    "trace_id": trace_id,
                    "reason": "startup_reconciliation",
                    "ts": now,
                },
            )

            result.streaming_messages_merged += 1
            logger.info(
                "Reconciled streaming events in session %s: merged %d events → "
                "Message seq=%d (text=%d chars, thinking=%d chars, tool_calls=%d)",
                session_id, len(seqs_to_invalidate), appended.seq,
                len(merged_text), len(merged_thinking), len(tool_calls_data),
            )
