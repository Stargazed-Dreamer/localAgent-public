"""D5 · usage 持久化（spec D20）：新增 usage 表 + append_usage/load_usage/get_total_tokens。

当前问题：
- usage 仅在 LLMResponse 返回时由 runner 内存累加（total_tokens += ...）
- 失败/断流时丢失，无历史查询能力
- 跨 session 统计、按模型聚合等都无法做

D5 修复：
- event_store.py：新增 usage 表（id/session_id/seq/prompt_tokens/completion_tokens/total_tokens/model/created_at）
- event_store.py：新增 append_usage / load_usage / get_total_tokens 方法
- schema 版本 5→6，迁移逻辑加 usage 表建表（CREATE TABLE IF NOT EXISTS 天然幂等）
- runner.py：LLM 返回后调 store.append_usage(session_id, seq, usage)
- RunOutcome.total_tokens 保持内存累计（与 DB SUM 一致）

测试策略（red-green）：
- 红测试：调 append_usage 前断言 usage 表无记录（修复前 method 不存在 → AttributeError）
- 绿测试：append_usage 后 load_usage 返回记录 / get_total_tokens 跨 session SUM 正确
- 失败/断流场景：LLM 返回 partial usage 时也落库
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from client.core.agent.event_store import EventStore  # noqa: E402

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_path):
    """临时 EventStore（schema v6，含 usage 表）。"""
    db_path = str(tmp_path / "test_d5.db")
    s = EventStore(db_path=db_path)
    s.init()
    yield s
    s.close()


def _create_session(store, session_id: str = "sess_d5_1"):
    """创建测试会话。"""
    asyncio.run(store.create_session(
        session_id=session_id,
        title="D5 test",
        mode="dialogue",
    ))


# ============================================================================
# Part 1: usage 表存在 + schema 版本
# ============================================================================


class TestUsageTableSchema:
    """usage 表 schema 验证。"""

    def test_usage_table_exists_after_init(self, store):
        """init() 后 usage 表应存在。"""
        cur = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage'"
        )
        assert cur.fetchone() is not None, "D5: usage 表应在 init() 后存在"

    def test_usage_table_columns(self, store):
        """usage 表应有 id/session_id/seq/prompt_tokens/completion_tokens/total_tokens/model/created_at。"""
        cur = store.conn.execute("PRAGMA table_info(usage)")
        cols = {r[1] for r in cur.fetchall()}
        expected = {
            "id", "session_id", "seq", "prompt_tokens",
            "completion_tokens", "total_tokens", "model", "created_at",
        }
        missing = expected - cols
        assert not missing, f"D5: usage 表缺列：{missing}"

    def test_schema_version_is_at_least_6(self, store):
        """schema_info.version 应 >= 6（D5 升版本到 6，后续 ticket 可能再升）。"""
        cur = store.conn.execute(
            "SELECT value FROM schema_info WHERE key='version'"
        )
        row = cur.fetchone()
        assert row is not None
        version = int(row[0])
        assert version >= 6, f"D5: schema 版本应 >= 6，当前 {row[0]}"


# ============================================================================
# Part 2: append_usage / load_usage
# ============================================================================


class TestAppendLoadUsage:
    """append_usage + load_usage 基础 CRUD。"""

    def test_append_usage_returns_id(self, store):
        """append_usage 返回 usage_id（非空字符串）。"""
        _create_session(store)
        usage_id = asyncio.run(store.append_usage(
            session_id="sess_d5_1",
            seq=1,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="deepseek-chat",
        ))
        assert usage_id, "D5: append_usage 应返回非空 usage_id"

    def test_load_usage_returns_records(self, store):
        """load_usage 返回该 session 的所有 usage 记录（按 seq 升序）。"""
        _create_session(store)
        asyncio.run(store.append_usage("sess_d5_1", 1, 100, 50, 150, "model-a"))
        asyncio.run(store.append_usage("sess_d5_1", 2, 200, 80, 280, "model-a"))

        records = asyncio.run(store.load_usage("sess_d5_1"))
        assert len(records) == 2
        assert records[0]["seq"] == 1
        assert records[0]["total_tokens"] == 150
        assert records[1]["seq"] == 2
        assert records[1]["total_tokens"] == 280

    def test_load_usage_empty_for_new_session(self, store):
        """无 usage 记录的 session → load_usage 返回空 list。"""
        _create_session(store)
        records = asyncio.run(store.load_usage("sess_d5_1"))
        assert records == []

    def test_load_usage_fields(self, store):
        """load_usage 返回的记录应含所有字段。"""
        _create_session(store)
        asyncio.run(store.append_usage("sess_d5_1", 1, 100, 50, 150, "deepseek-chat"))

        records = asyncio.run(store.load_usage("sess_d5_1"))
        r = records[0]
        assert r["session_id"] == "sess_d5_1"
        assert r["seq"] == 1
        assert r["prompt_tokens"] == 100
        assert r["completion_tokens"] == 50
        assert r["total_tokens"] == 150
        assert r["model"] == "deepseek-chat"
        assert "created_at" in r
        assert r["created_at"] > 0

    def test_load_usage_filter_by_session(self, store):
        """load_usage 只返回指定 session 的记录（不混入其他 session）。"""
        _create_session(store, "sess_a")
        _create_session(store, "sess_b")
        asyncio.run(store.append_usage("sess_a", 1, 100, 50, 150, "m"))
        asyncio.run(store.append_usage("sess_b", 1, 200, 80, 280, "m"))

        a_records = asyncio.run(store.load_usage("sess_a"))
        b_records = asyncio.run(store.load_usage("sess_b"))
        assert len(a_records) == 1
        assert len(b_records) == 1
        assert a_records[0]["total_tokens"] == 150
        assert b_records[0]["total_tokens"] == 280


# ============================================================================
# Part 3: get_total_tokens（跨 session SUM）
# ============================================================================


class TestGetTotalTokens:
    """get_total_tokens 跨 session SUM。"""

    def test_total_tokens_zero_for_no_records(self, store):
        """无 usage 记录 → get_total_tokens 返回 0。"""
        total = asyncio.run(store.get_total_tokens())
        assert total == 0

    def test_total_tokens_single_session(self, store):
        """单 session 多次 usage → SUM 总和。"""
        _create_session(store, "sess_x")
        asyncio.run(store.append_usage("sess_x", 1, 100, 50, 150, "m"))
        asyncio.run(store.append_usage("sess_x", 2, 200, 80, 280, "m"))

        total = asyncio.run(store.get_total_tokens())
        assert total == 150 + 280

    def test_total_tokens_cross_session(self, store):
        """跨 session SUM（D20 验收点：全局统计）。"""
        _create_session(store, "sess_a")
        _create_session(store, "sess_b")
        asyncio.run(store.append_usage("sess_a", 1, 100, 50, 150, "m"))
        asyncio.run(store.append_usage("sess_b", 1, 200, 80, 280, "m"))
        asyncio.run(store.append_usage("sess_a", 2, 50, 25, 75, "m"))

        total = asyncio.run(store.get_total_tokens())
        assert total == 150 + 280 + 75

    def test_total_tokens_filter_by_session(self, store):
        """get_total_tokens(session_id=...) → 只 SUM 该 session。"""
        _create_session(store, "sess_a")
        _create_session(store, "sess_b")
        asyncio.run(store.append_usage("sess_a", 1, 100, 50, 150, "m"))
        asyncio.run(store.append_usage("sess_b", 1, 200, 80, 280, "m"))

        a_total = asyncio.run(store.get_total_tokens(session_id="sess_a"))
        b_total = asyncio.run(store.get_total_tokens(session_id="sess_b"))
        assert a_total == 150
        assert b_total == 280


# ============================================================================
# Part 4: 失败/断流场景（partial usage 也落库）
# ============================================================================


class TestUsagePersistsOnFailure:
    """失败/断流场景：LLM 返回 partial usage 时也落库。"""

    def test_partial_usage_persists(self, store):
        """断流时 usage 已收到 → append_usage 仍可记录（runner 调用即可）。"""
        _create_session(store, "sess_fail")
        # 模拟断流：LLM 返回了 usage 但 stop_reason="user_interrupted"
        # runner 在中断路径仍应调 append_usage（spec D20）
        asyncio.run(store.append_usage(
            "sess_fail", 1, 100, 30, 130, "deepseek-chat",
        ))

        records = asyncio.run(store.load_usage("sess_fail"))
        assert len(records) == 1
        assert records[0]["total_tokens"] == 130

    def test_zero_usage_persists(self, store):
        """usage 全 0 也落库（模型未返回 usage 时不丢失记录）。"""
        _create_session(store, "sess_zero")
        asyncio.run(store.append_usage(
            "sess_zero", 1, 0, 0, 0, "unknown-model",
        ))

        records = asyncio.run(store.load_usage("sess_zero"))
        assert len(records) == 1
        assert records[0]["total_tokens"] == 0
        assert records[0]["model"] == "unknown-model"


# ============================================================================
# Part 5: 并发安全（C7 + D5 协作）
# ============================================================================


class TestUsageConcurrentSafe:
    """并发 append_usage 不出错（C7 threading.Lock 保护）。"""

    def test_concurrent_append_usage(self, store):
        """3 协程并发 append_usage → 全部落库，无异常。"""
        _create_session(store, "sess_concurrent")

        async def _run():
            tasks = [
                store.append_usage("sess_concurrent", i, 100, 50, 150, "m")
                for i in range(1, 31)
            ]
            await asyncio.gather(*tasks)

        asyncio.run(_run())

        records = asyncio.run(store.load_usage("sess_concurrent"))
        assert len(records) == 30
        total = sum(r["total_tokens"] for r in records)
        assert total == 30 * 150
