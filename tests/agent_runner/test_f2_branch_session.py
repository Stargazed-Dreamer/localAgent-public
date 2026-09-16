"""分支会话 + 顶栏时间格式测试（2026-09-13 用户反馈轮新增）。

- branch_session：从某条 assistant 回复复制历史开新对话（用户需求"分支"），
  剥离 tool 消息与 tool_calls，保证新会话上下文对 provider 合法。
- _format_last_updated：顶栏时间今天/昨天/更早的显示格式（用户反馈"只有一个时间容易误解"）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.event_store import EventStore  # noqa: E402
from client.core.agent.types import Message  # noqa: E402
from client.panels.chat import _format_last_updated  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _make_store(tmp_path, name: str) -> EventStore:
    store = EventStore(db_path=str(tmp_path / name))
    store.init()
    return store


def test_branch_session_copies_history_strips_tools(tmp_path):
    """分支：复制 ≤seq 的 user/assistant，剥离 tool 消息与 tool_calls，seq 重编号。"""
    store = _make_store(tmp_path, "branch.db")
    try:
        sid = _run(store.create_session(title="原始对话")).id
        _run(store.append_message(sid, Message(role="user", content="问题一")))
        _run(store.append_message(
            sid,
            Message(role="assistant", content="回答一", thinking="思考",
                    tool_calls=[{"id": "tc1", "type": "function",
                                 "function": {"name": "ls", "arguments": "{}"}}]),
        ))
        # tool 消息与第二个 user/assistant 轮（seq 4/5，超出分支点 2）
        _run(store.append_message(
            sid, Message(role="tool", content="结果", tool_call_id="tc1")
        ))
        _run(store.append_message(sid, Message(role="user", content="问题二")))
        _run(store.append_message(sid, Message(role="assistant", content="回答二")))

        new_sid = _run(store.branch_session(sid, upto_seq=2))
        assert new_sid != sid

        new_session = _run(store.get_session(new_sid))
        assert new_session is not None
        assert new_session.title == "原始对话 (分支)"
        assert new_session.mode == "dialogue"

        msgs = _run(store.load_messages(new_sid))
        assert [m.seq for m in msgs] == [1, 2]
        assert msgs[0].role == "user" and msgs[0].content == "问题一"
        assert msgs[1].role == "assistant" and msgs[1].content == "回答一"
        # tool_calls 剥离（避免 provider 要求 tool_result 配对）
        assert not msgs[1].tool_calls

        # 源会话写入了审计事件
        events = _run(store.load_events(sid))
        assert any(e.type == "session_branched" for e in events)
    finally:
        store.close()


def test_branch_session_full_copy_with_huge_upto(tmp_path):
    """upto_seq 取极大值 → 复制全部历史（右键"复制会话"路径）。"""
    store = _make_store(tmp_path, "branch_all.db")
    try:
        sid = _run(store.create_session(title="全套")).id
        _run(store.append_message(sid, Message(role="user", content="u1")))
        _run(store.append_message(sid, Message(role="assistant", content="a1")))
        _run(store.append_message(sid, Message(role="user", content="u2")))
        _run(store.append_message(sid, Message(role="assistant", content="a2")))

        new_sid = _run(store.branch_session(sid, upto_seq=2**31 - 1))
        msgs = _run(store.load_messages(new_sid))
        assert [m.content for m in msgs] == ["u1", "a1", "u2", "a2"]
    finally:
        store.close()


def test_branch_session_rejects_bad_upto(tmp_path):
    """upto_seq <= 0 应拒绝（无意义分支）。"""
    store = _make_store(tmp_path, "branch_bad.db")
    try:
        sid = _run(store.create_session(title="t")).id
        try:
            _run(store.branch_session(sid, upto_seq=0))
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        store.close()


def test_format_last_updated_variants():
    """今天 HH:MM / 昨天 前缀 / 更早带日期 / 非法值空串。"""
    from datetime import date, datetime, timedelta

    now = datetime.now()
    today_ts = now.timestamp()
    yesterday_ts = (now - timedelta(days=1)).timestamp()
    older_ts = (now - timedelta(days=40)).timestamp()

    assert _format_last_updated(today_ts) == now.strftime("%H:%M")
    assert _format_last_updated(yesterday_ts).startswith("昨天 ")
    yester_hm = _format_last_updated(yesterday_ts)
    assert yester_hm.endswith(now.strftime("%H:%M"))
    older = _format_last_updated(older_ts)
    assert older.startswith(now.strftime("%m-%d")) or older.startswith(
        datetime.fromtimestamp(older_ts).strftime("%m-%d")
    )
    assert _format_last_updated(0) == ""
    # 跨年（构造 2 年前）
    old_year_ts = time.mktime(
        (date.today().year - 2, 1, 15, 10, 30, 0, 0, 0, -1)
    )
    assert _format_last_updated(old_year_ts).startswith(str(date.today().year - 2))
