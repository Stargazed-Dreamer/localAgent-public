"""headless-agent-session Ticket 01 验收测试：EventStore sessions 表 mode 字段

覆盖 Ticket 01 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] sessions 表 schema 含 `mode TEXT DEFAULT 'dialogue'`（migration 幂等）
- [x] create_session 加 mode 参数（默认 "dialogue" 向后兼容）
- [x] list_sessions / get_session / list_sessions_by_status 返回 mode 字段
- [x] 单元测试：dialogue（默认）/ headless / headless_judge 三种 mode 的 create + list + get
- [x] 单元测试：已有数据迁移后 mode="dialogue"（旧 DB 无 mode 列 → init() 自动加列）
- [x] 验证 client chat 面板现有功能不破坏（mode 字段不影响 create_session 默认行为）

测试 prior art: tests/test_v6_lite_t01.py（EventStore T01 测试模式）
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import EventStore  # noqa: E402

# ============================================================================
# Fixtures（沿用 test_v6_lite_t01.py 模式）
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


# ============================================================================
# 1. 三种 mode 的 create + get（Ticket 01 核心）
# ============================================================================


def test_create_session_default_mode_is_dialogue(store):
    """Ticket 01: 不传 mode 参数，默认 "dialogue"（向后兼容现有 chat 会话）。"""
    async def run():
        session = await store.create_session(session_id="s-default", title="default mode test")
        assert session.mode == "dialogue"
        # 验证 DB 中确实写了 "dialogue"
        row = store.conn.execute(
            "SELECT mode FROM sessions WHERE id = ?", ("s-default",)
        ).fetchone()
        assert row["mode"] == "dialogue"
    asyncio.run(run())


def test_create_session_headless_mode(store):
    """Ticket 01: mode="headless" 创建后端主会话。"""
    async def run():
        session = await store.create_session(
            session_id="headless-abc123def456",
            title="headless main agent",
            mode="headless",
        )
        assert session.mode == "headless"
        # 验证 DB 中确实写了 "headless"
        row = store.conn.execute(
            "SELECT mode FROM sessions WHERE id = ?", ("headless-abc123def456",)
        ).fetchone()
        assert row["mode"] == "headless"
    asyncio.run(run())


def test_create_session_headless_judge_mode(store):
    """Ticket 01: mode="headless_judge" 创建 judge 会话。"""
    async def run():
        session = await store.create_session(
            session_id="headless-judge-xyz789abc012",
            title="judge: headless main agent",
            mode="headless_judge",
        )
        assert session.mode == "headless_judge"
        # 验证 DB 中确实写了 "headless_judge"
        row = store.conn.execute(
            "SELECT mode FROM sessions WHERE id = ?", ("headless-judge-xyz789abc012",)
        ).fetchone()
        assert row["mode"] == "headless_judge"
    asyncio.run(run())


# ============================================================================
# 2. get_session / list_sessions / list_sessions_by_status 返回 mode
# ============================================================================


def test_get_session_returns_mode_for_all_three_modes(store):
    """Ticket 01: get_session 对三种 mode 都返回正确字段。"""
    async def run():
        # 创建三种 mode 的会话
        await store.create_session(session_id="s-dialogue", mode="dialogue")
        await store.create_session(session_id="s-headless", mode="headless")
        await store.create_session(session_id="s-judge", mode="headless_judge")

        # get_session 验证
        d = await store.get_session("s-dialogue")
        h = await store.get_session("s-headless")
        j = await store.get_session("s-judge")

        assert d is not None and d.mode == "dialogue"
        assert h is not None and h.mode == "headless"
        assert j is not None and j.mode == "headless_judge"
    asyncio.run(run())


def test_list_sessions_returns_mode_field(store):
    """Ticket 01: list_sessions 返回所有会话，每条含 mode 字段。"""
    async def run():
        # 创建三种 mode 的会话
        await store.create_session(session_id="s-1", mode="dialogue")
        await store.create_session(session_id="s-2", mode="headless")
        await store.create_session(session_id="s-3", mode="headless_judge")

        sessions = await store.list_sessions()
        assert len(sessions) == 3

        # 按 id 索引验证 mode
        by_id = {s.id: s for s in sessions}
        assert by_id["s-1"].mode == "dialogue"
        assert by_id["s-2"].mode == "headless"
        assert by_id["s-3"].mode == "headless_judge"

        # 所有 Session 对象都有 mode 字段（类型检查）
        for s in sessions:
            assert isinstance(s.mode, str)
            assert s.mode in {"dialogue", "headless", "headless_judge"}
    asyncio.run(run())


def test_list_sessions_by_status_returns_mode_field(store):
    """Ticket 01: list_sessions_by_status 返回的会话含 mode 字段。"""
    async def run():
        # 创建会话并更新状态
        await store.create_session(session_id="s-1", mode="dialogue")
        await store.create_session(session_id="s-2", mode="headless")
        await store.create_session(session_id="s-3", mode="headless_judge")

        # 更新状态为 completed
        await store.update_session_status("s-2", "completed")
        await store.update_session_status("s-3", "completed")

        # 查 status=completed 的会话
        completed = await store.list_sessions_by_status("completed")
        assert len(completed) == 2

        by_id = {s.id: s for s in completed}
        assert by_id["s-2"].mode == "headless"
        assert by_id["s-3"].mode == "headless_judge"
    asyncio.run(run())


# ============================================================================
# 3. session_created event 含 mode（向后兼容验证）
# ============================================================================


def test_session_created_event_contains_mode_for_headless(store):
    """Ticket 01: headless 会话的 session_created event payload 含 mode="headless"。"""
    async def run():
        await store.create_session(
            session_id="headless-test-1",
            title="headless session",
            mode="headless",
        )
        events = await store.load_events("headless-test-1")
        assert len(events) >= 1
        created_event = events[0]
        assert created_event.type == "session_created"
        assert created_event.payload["mode"] == "headless"
    asyncio.run(run())


def test_session_created_event_contains_mode_for_judge(store):
    """Ticket 01: judge 会话的 session_created event payload 含 mode="headless_judge"。"""
    async def run():
        await store.create_session(
            session_id="headless-judge-1",
            title="judge session",
            mode="headless_judge",
        )
        events = await store.load_events("headless-judge-1")
        assert len(events) >= 1
        created_event = events[0]
        assert created_event.type == "session_created"
        assert created_event.payload["mode"] == "headless_judge"
    asyncio.run(run())


# ============================================================================
# 4. 迁移测试：旧 DB 无 mode 列 → init() 自动加列 + 已有行 mode='dialogue'
# ============================================================================


def _build_legacy_db_without_mode_column(db_path: str) -> None:
    """构造一个"旧 DB"：sessions 表没有 mode 列（模拟 v6-01 之前的 schema）。

    其他表（messages/events/tool_calls/schema_info）保持新 schema，
    避免 init() 因其他表缺失报错，只测 sessions.mode 迁移。
    """
    conn = sqlite3.connect(db_path)
    # 建旧版 sessions 表（无 mode 列）
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT DEFAULT 'idle',
            created_at REAL,
            updated_at REAL,
            prompt_index INTEGER DEFAULT 0,
            last_compaction_prompt_index INTEGER
        )
    """)
    # 插入一行旧数据（无 mode 列）
    conn.execute(
        "INSERT INTO sessions(id, title, status, created_at, updated_at) "
        "VALUES('legacy-1', 'legacy session', 'completed', 1000.0, 1000.0)"
    )
    # 建其他表（新 schema，避免 init() 报错）
    conn.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, role TEXT,
            content_json TEXT, tool_call_id TEXT, source TEXT, visible INTEGER,
            created_at REAL, tool_calls_json TEXT, thinking_json TEXT
        );
        CREATE TABLE events (
            seq INTEGER, session_id TEXT, type TEXT, payload_json TEXT,
            trace_id TEXT, prompt_index INTEGER, invalidated_seq INTEGER,
            parent_trace_id TEXT, depth INTEGER, created_at REAL,
            PRIMARY KEY (session_id, seq)
        );
        CREATE TABLE tool_calls (
            id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, name TEXT,
            args_json TEXT, safety TEXT, status TEXT, started_at REAL,
            ended_at REAL, trace_id TEXT
        );
        CREATE TABLE schema_info (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()
    conn.close()


def test_migrate_sessions_mode_adds_column_to_legacy_db(tmp_db_path):
    """Ticket 01: 旧 DB（无 mode 列）init() 后自动加 mode 列，已有行 mode='dialogue'。"""
    # 1. 构造旧 DB（sessions 表无 mode 列 + 一行旧数据）
    _build_legacy_db_without_mode_column(tmp_db_path)

    # 2. 用 EventStore init() 触发迁移
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        # 3. 验证 mode 列已加（直接查 PRAGMA table_info）
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "mode" in cols, "迁移后 sessions 表应有 mode 列"

        # 4. 验证旧行 mode='dialogue'（DEFAULT 生效）
        async def run():
            session = await store.get_session("legacy-1")
            assert session is not None
            assert session.id == "legacy-1"
            assert session.mode == "dialogue"  # DEFAULT 'dialogue' 对已有行生效
            assert session.title == "legacy session"
            assert session.status == "completed"
        asyncio.run(run())
    finally:
        store.close()


def test_migrate_sessions_mode_idempotent_for_new_db(tmp_db_path):
    """Ticket 01: 新 DB（已有 mode 列）init() 不报错，mode 列保持不变（幂等）。"""
    # 1. 第一次 init() 建新 DB（sessions 表已有 mode 列）
    store1 = EventStore(db_path=tmp_db_path)
    store1.init()
    async def create_session():
        await store1.create_session(
            session_id="new-1", mode="headless", title="before reinit"
        )
    asyncio.run(create_session())
    store1.close()

    # 2. 第二次 init()（模拟后端重启，DB 已存在且有 mode 列）
    store2 = EventStore(db_path=tmp_db_path)
    store2.init()
    try:
        # 3. 验证 mode 列仍在 + 数据不丢 + mode 值不变
        async def run():
            session = await store2.get_session("new-1")
            assert session is not None
            assert session.mode == "headless"  # 原值保留，未被迁移重置
            assert session.title == "before reinit"
        asyncio.run(run())

        # 4. 验证 mode 列仍存在
        cols = {r[1] for r in store2.conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "mode" in cols
    finally:
        store2.close()


# ============================================================================
# 5. 不破坏现有功能验证（create_session 不传 mode 等价于旧行为）
# ============================================================================


def test_create_session_without_mode_arg_still_works(store):
    """Ticket 01: 不传 mode 参数的旧调用方式仍正常工作（向后兼容）。

    验证 client chat 面板现有功能不破坏：ChatPanel._ChatWorker 调 create_session
    时不传 mode，行为应与加 mode 字段前完全一致。
    """
    async def run():
        # 模拟 chat 面板的调用方式（不传 mode）
        session = await store.create_session(session_id="chat-1", title="normal chat")
        assert session.mode == "dialogue"  # 默认值
        assert session.status == "idle"
        assert session.title == "normal chat"

        # 验证可以正常 append_message（现有 chat 流程不破坏）
        from client.core.agent import Message
        msg = await store.append_message(
            "chat-1", Message(role="user", content="hello", source="user")
        )
        assert msg.seq == 1
        assert msg.role == "user"
    asyncio.run(run())


def test_headless_session_id_naming_convention(store):
    """Ticket 01: headless 会话用 "headless-" / "headless-judge-" 前缀（spec 决策）。

    验证 session_id 命名约定与 spec.md 一致：
    - 主会话：headless-{uuid4.hex[:12]}
    - judge 会话：headless-judge-{uuid4.hex[:12]}

    本测试只验证 EventStore 能正确存储这种命名，不验证 uuid 生成逻辑（那是 Ticket 02）。
    """
    async def run():
        main_sid = "headless-abc123def456"
        judge_sid = "headless-judge-abc123def456"

        await store.create_session(session_id=main_sid, mode="headless", title="main")
        await store.create_session(session_id=judge_sid, mode="headless_judge", title="judge")

        # 验证两个会话都能被 list_sessions 返回
        sessions = await store.list_sessions()
        ids = {s.id for s in sessions}
        assert main_sid in ids
        assert judge_sid in ids

        # 验证 mode 正确关联
        by_id = {s.id: s for s in sessions}
        assert by_id[main_sid].mode == "headless"
        assert by_id[judge_sid].mode == "headless_judge"

        # 验证前缀约定
        assert main_sid.startswith("headless-")
        assert not main_sid.startswith("headless-judge-")  # 主会话不以 judge- 开头
        assert judge_sid.startswith("headless-judge-")
    asyncio.run(run())
