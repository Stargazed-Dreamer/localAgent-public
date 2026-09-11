"""chat-panel-v2 Ticket 01 验收测试：Schema 迁移（D10 + D31）

覆盖 T01 acceptance（temp/sdd/chat-panel-v2/tickets.md）：
- [x] EventStore 加幂等 ALTER TABLE 迁移：sessions.group_name TEXT、sessions.pinned INTEGER DEFAULT 0、messages.model TEXT
- [x] Message dataclass 新增 `model: str = ""` 字段
- [x] Message.to_db / from_db 序列化 model 字段（旧 schema 无 model 列时 from_db 兜底空字符串）
- [x] 迁移幂等：重复启动不报错（PRAGMA table_info 检查列已存在）
- [x] append_message 持久化 model 字段 + load_messages 读出 model 字段

测试 prior art: tests/test_event_store_mode.py（headless-agent-session T01 迁移测试模式）
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import EventStore, Message  # noqa: E402

# ============================================================================
# Fixtures（沿用 test_event_store_mode.py 模式）
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    """已初始化的 EventStore（测试结束自动关闭）。"""
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回指定表的所有列名（用 PRAGMA table_info）。"""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _build_legacy_db_without_chat_panel_v2_columns(db_path: str) -> None:
    """构造一个"旧 DB"：sessions 表无 group_name/pinned 列，messages 表无 model 列。

    模拟 chat-panel-v2 T01 之前的 schema（schema v4）。
    其他表（events/tool_calls/schema_info）保持新 schema 避免 init() 报错。
    """
    conn = sqlite3.connect(db_path)
    # 建旧版 sessions 表（无 group_name / pinned 列）
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            mode TEXT DEFAULT 'dialogue',
            status TEXT DEFAULT 'idle',
            created_at REAL,
            updated_at REAL,
            prompt_index INTEGER DEFAULT 0,
            last_compaction_prompt_index INTEGER
        )
    """)
    # 插入一行旧数据
    conn.execute(
        "INSERT INTO sessions(id, title, mode, status, created_at, updated_at) "
        "VALUES('legacy-1', 'legacy session', 'dialogue', 'completed', 1000.0, 1000.0)"
    )
    # 建旧版 messages 表（无 model 列）
    conn.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            tool_call_id TEXT,
            source TEXT DEFAULT 'user',
            visible INTEGER DEFAULT 1,
            created_at REAL,
            tool_calls_json TEXT,
            thinking_json TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    # 插入一行旧 messages 数据（无 model 列）
    conn.execute(
        "INSERT INTO messages(id, session_id, seq, role, content_json, source, visible, created_at) "
        "VALUES('msg-legacy-1', 'legacy-1', 1, 'user', '\"hello\"', 'user', 1, 1000.0)"
    )
    # 建其他表（新 schema，避免 init() 报错）
    conn.executescript("""
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


# ============================================================================
# 1. 新库初始化：三列存在 + pinned 默认 0
# ============================================================================


def test_new_db_has_sessions_group_name_column(store):
    """T01: 新建库 sessions 表含 group_name 列。"""
    cols = _table_columns(store.conn, "sessions")
    assert "group_name" in cols, "新库 sessions 表应有 group_name 列"


def test_new_db_has_sessions_pinned_column(store):
    """T01: 新建库 sessions 表含 pinned 列。"""
    cols = _table_columns(store.conn, "sessions")
    assert "pinned" in cols, "新库 sessions 表应有 pinned 列"


def test_new_db_has_messages_model_column(store):
    """T01: 新建库 messages 表含 model 列。"""
    cols = _table_columns(store.conn, "messages")
    assert "model" in cols, "新库 messages 表应有 model 列"


def test_new_db_pinned_defaults_to_zero(store):
    """T01: 新建库 sessions.pinned 默认值为 0（D10）。"""
    async def run():
        await store.create_session(session_id="s-1", title="test default pinned")
        row = store.conn.execute(
            "SELECT pinned FROM sessions WHERE id = ?", ("s-1",)
        ).fetchone()
        assert row["pinned"] == 0
    asyncio.run(run())


def test_new_db_group_name_defaults_to_null(store):
    """T01: 新建库 sessions.group_name 默认值为 NULL（未分组，D10）。"""
    async def run():
        await store.create_session(session_id="s-1", title="test default group")
        row = store.conn.execute(
            "SELECT group_name FROM sessions WHERE id = ?", ("s-1",)
        ).fetchone()
        assert row["group_name"] is None
    asyncio.run(run())


# ============================================================================
# 2. 旧库迁移：旧 schema → init() → 新列存在 + 已有行默认值
# ============================================================================


def test_migrate_legacy_db_adds_sessions_group_name(tmp_db_path):
    """T01: 旧 DB（无 group_name 列）init() 后自动加列。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        cols = _table_columns(store.conn, "sessions")
        assert "group_name" in cols, "迁移后 sessions 表应有 group_name 列"
    finally:
        store.close()


def test_migrate_legacy_db_adds_sessions_pinned_with_default_zero(tmp_db_path):
    """T01: 旧 DB（无 pinned 列）init() 后自动加列，已有行 pinned=0。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        cols = _table_columns(store.conn, "sessions")
        assert "pinned" in cols, "迁移后 sessions 表应有 pinned 列"
        # 已有行 pinned 取 DEFAULT 0
        row = store.conn.execute(
            "SELECT pinned FROM sessions WHERE id = ?", ("legacy-1",)
        ).fetchone()
        assert row["pinned"] == 0
    finally:
        store.close()


def test_migrate_legacy_db_adds_messages_model(tmp_db_path):
    """T01: 旧 DB（无 model 列）init() 后自动加列。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        cols = _table_columns(store.conn, "messages")
        assert "model" in cols, "迁移后 messages 表应有 model 列"
    finally:
        store.close()


def test_migrate_legacy_db_messages_model_defaults_to_null(tmp_db_path):
    """T01: 旧 DB 迁移后，已有 messages 行 model 为 NULL（ALTER TABLE ADD COLUMN 默认 NULL）。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        row = store.conn.execute(
            "SELECT model FROM messages WHERE id = ?", ("msg-legacy-1",)
        ).fetchone()
        # ALTER TABLE ADD COLUMN model TEXT 不带 DEFAULT，已有行为 NULL
        assert row["model"] is None
    finally:
        store.close()


def test_migrate_legacy_db_load_messages_returns_empty_model(tmp_db_path):
    """T01: 旧 DB 迁移后 load_messages 读出的旧消息 model=''（from_db 兜底）。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        async def run():
            msgs = await store.load_messages("legacy-1")
            assert len(msgs) == 1
            assert msgs[0].model == ""  # NULL → from_db 兜底空字符串
            assert msgs[0].role == "user"
            assert msgs[0].content == "hello"
        asyncio.run(run())
    finally:
        store.close()


# ============================================================================
# 3. 迁移幂等：init() 两次不报错 + 数据不丢
# ============================================================================


def test_migration_idempotent_init_twice(tmp_db_path):
    """T01: 重复 init() 不报错（幂等：PRAGMA table_info 检查列已存在）。"""
    # 第一次 init 建新库
    store1 = EventStore(db_path=tmp_db_path)
    store1.init()
    async def create_session():
        await store1.create_session(session_id="s-1", title="before reinit")
    asyncio.run(create_session())
    store1.close()

    # 第二次 init（模拟后端重启）
    store2 = EventStore(db_path=tmp_db_path)
    store2.init()
    try:
        # 验证列仍在 + 数据不丢
        sessions_cols = _table_columns(store2.conn, "sessions")
        messages_cols = _table_columns(store2.conn, "messages")
        assert "group_name" in sessions_cols
        assert "pinned" in sessions_cols
        assert "model" in messages_cols

        async def run():
            session = await store2.get_session("s-1")
            assert session is not None
            assert session.title == "before reinit"
        asyncio.run(run())
    finally:
        store2.close()


def test_migration_idempotent_after_legacy_migration(tmp_db_path):
    """T01: 旧 DB 迁移后再 init() 不报错（迁移幂等性第二层验证）。"""
    _build_legacy_db_without_chat_panel_v2_columns(tmp_db_path)

    # 第一次 init 触发迁移
    store1 = EventStore(db_path=tmp_db_path)
    store1.init()
    store1.close()

    # 第二次 init（迁移后）
    store2 = EventStore(db_path=tmp_db_path)
    store2.init()
    try:
        cols = _table_columns(store2.conn, "sessions")
        assert "group_name" in cols
        assert "pinned" in cols
        messages_cols = _table_columns(store2.conn, "messages")
        assert "model" in messages_cols
    finally:
        store2.close()


# ============================================================================
# 4. Message dataclass：model 字段默认值 + to_db/from_db 往返
# ============================================================================


def test_message_model_default_empty_string():
    """T01: Message 默认 model=''（D31）。"""
    msg = Message(role="user", content="hello")
    assert msg.model == ""


def test_message_to_db_includes_model_field():
    """T01: Message.to_db() 输出含 model 键。"""
    msg = Message(role="assistant", content="hi", model="GLM-5.2")
    row = msg.to_db()
    assert "model" in row
    assert row["model"] == "GLM-5.2"


def test_message_to_db_model_empty_serializes_to_none():
    """T01: Message.to_db() model='' 时序列化为 None（与 thinking_json 一致，落库 NULL）。"""
    msg = Message(role="user", content="hello", model="")
    row = msg.to_db()
    assert row["model"] is None


def test_message_from_db_roundtrip_with_model():
    """T01: Message.to_db → from_db 往返 model 字段一致。"""
    original = Message(role="assistant", content="response", model="GLM-5.2", source="assistant")
    row = original.to_db()
    # 模拟 load_messages 的 dict(r) 转换
    restored = Message.from_db(dict(row))
    assert restored.model == "GLM-5.2"
    assert restored.role == "assistant"
    assert restored.content == "response"


def test_message_from_db_roundtrip_empty_model():
    """T01: model='' 往返一致（to_db → None → from_db → ''）。"""
    original = Message(role="user", content="hello", model="")
    row = original.to_db()
    restored = Message.from_db(dict(row))
    assert restored.model == ""


def test_message_from_db_without_model_key_returns_empty_string():
    """T01: 旧 schema 无 model 列时 from_db 兜底空字符串（向后兼容）。

    模拟旧 DB 读出的 row 不含 model 键的场景。
    """
    legacy_row = {
        "id": "msg-1",
        "session_id": "s-1",
        "seq": 1,
        "role": "user",
        "content_json": '"hello"',
        "tool_call_id": None,
        "source": "user",
        "visible": 1,
        "created_at": 1000.0,
        "tool_calls_json": None,
        "thinking_json": None,
        # 注意：无 model 键
    }
    msg = Message.from_db(legacy_row)
    assert msg.model == ""
    assert msg.role == "user"
    assert msg.content == "hello"


def test_message_from_db_with_none_model_returns_empty_string():
    """T01: row['model'] = None 时 from_db 返回 ''（与旧库迁移后已有行一致）。"""
    row = {
        "id": "msg-1",
        "session_id": "s-1",
        "seq": 1,
        "role": "assistant",
        "content_json": '"hi"',
        "tool_call_id": None,
        "source": "assistant",
        "visible": 1,
        "created_at": 1000.0,
        "tool_calls_json": None,
        "thinking_json": None,
        "model": None,  # 旧库迁移后已有行的值
    }
    msg = Message.from_db(row)
    assert msg.model == ""


# ============================================================================
# 5. 集成：append_message 持久化 model + load_messages 读出 model
# ============================================================================


def test_append_message_persists_model_for_assistant(store):
    """T01: append_message 写入 assistant 消息时持久化 model 字段（D31）。"""
    async def run():
        await store.create_session(session_id="s-1", title="test model persist")
        msg = Message(
            role="assistant",
            content="hello from GLM",
            source="assistant",
            model="GLM-5.2",
        )
        saved = await store.append_message("s-1", msg)
        assert saved.model == "GLM-5.2"

        # 直接查 DB 验证 model 列写入
        row = store.conn.execute(
            "SELECT model FROM messages WHERE id = ?", (saved.id,)
        ).fetchone()
        assert row["model"] == "GLM-5.2"
    asyncio.run(run())


def test_append_message_persists_empty_model_for_user(store):
    """T01: user 消息 model='' 时落库 NULL（to_db 转 None）。"""
    async def run():
        await store.create_session(session_id="s-1", title="test user no model")
        msg = Message(role="user", content="hello", source="user")  # model 默认 ''
        saved = await store.append_message("s-1", msg)
        assert saved.model == ""

        row = store.conn.execute(
            "SELECT model FROM messages WHERE id = ?", (saved.id,)
        ).fetchone()
        assert row["model"] is None  # '' → None 落库
    asyncio.run(run())


def test_load_messages_returns_model_field(store):
    """T01: load_messages 读出消息时返回 model 字段（往返完整验证）。"""
    async def run():
        await store.create_session(session_id="s-1", title="test load model")
        # 写 user 消息（model=''）+ assistant 消息（model='GLM-5.2'）
        await store.append_message(
            "s-1", Message(role="user", content="hi", source="user")
        )
        await store.append_message(
            "s-1",
            Message(role="assistant", content="hello back", source="assistant", model="GLM-5.2"),
        )

        msgs = await store.load_messages("s-1")
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].model == ""  # user 消息无 model
        assert msgs[1].role == "assistant"
        assert msgs[1].model == "GLM-5.2"  # assistant 消息带 model
    asyncio.run(run())


def test_replace_messages_preserves_model_field(store):
    """T01: replace_messages（L2 压缩）保留 model 字段。

    v6-lite §3 W5 L2 压缩重建 visible messages 时，assistant 消息的 model 标记不应丢失。
    """
    async def run():
        await store.create_session(session_id="s-1", title="test replace model")
        # 写初始消息
        await store.append_message(
            "s-1", Message(role="user", content="hi", source="user")
        )
        await store.append_message(
            "s-1",
            Message(role="assistant", content="hello", source="assistant", model="GLM-5.2"),
        )

        # 模拟压缩后替换为新的消息列表（保留 model）
        new_messages = [
            Message(role="system", content="summary", source="summary"),
            Message(role="assistant", content="hello", source="assistant", model="GLM-5.2"),
        ]
        await store.replace_messages("s-1", new_messages)

        msgs = await store.load_messages("s-1")
        assert len(msgs) == 2
        assert msgs[0].role == "system"
        assert msgs[0].model == ""
        assert msgs[1].role == "assistant"
        assert msgs[1].model == "GLM-5.2"  # model 在 replace 后保留
    asyncio.run(run())
