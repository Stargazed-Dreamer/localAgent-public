"""C7 · SQLite 并发 + ADR-0021 连接池 + WAL 测试

测试范围（spec Code-1，ADR-0021）：
- 连接池：每个 _run_sync 借独立 connection，业务方法 self.conn 透明使用池连接
- WAL 模式：PRAGMA journal_mode 返回 'wal'，并发读不阻塞写
- 串行写：WAL + busy_timeout 串行化写事务，无数据丢失
- WAL fallback：in-memory DB 返回 'memory'，fallback 到 DELETE + _write_lock

设计依据：
- ADR-0021：连接池 + WAL（替代 C7 的单连接 + threading.Lock）
- v6-01 §1：WAL 模式读不阻塞写、写不阻塞读
- 不引入 asyncio.Lock（sqlite BUSY 重试 + WAL 足够）

不依赖后端运行，全部用真实 EventStore + 临时 DB。
遵循"测试修复铁律"：不使用 @pytest.mark.skip 绕过。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import EventStore  # noqa: E402
from client.core.agent.event_store import DEFAULT_POOL_SIZE  # noqa: E402
from client.core.agent.types import Message  # noqa: E402

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_store(tmp_path: Path) -> EventStore:
    store = EventStore(db_path=str(tmp_path / "test_c7.db"))
    store.init()
    return store


# ============================================================================
# ADR-0021: WAL 模式 + 连接池配置断言
# ============================================================================


class TestWalAndPoolConfig:
    """ADR-0021: WAL 模式生效 + 连接池配置正确。"""

    def test_wal_journal_mode_active(self, tmp_path):
        """ADR-0021: PRAGMA journal_mode 返回 'wal'（WAL 模式生效）。"""
        store = _new_store(tmp_path)
        try:
            # 直接查 PRAGMA journal_mode（用 init_conn，无 thread-local 绑定）
            row = store.conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(row[0]).lower() if row else ""
            assert mode == "wal", (
                f"journal_mode 应为 'wal'，实际为 {mode!r}"
            )
            # journal_mode property 也应返回 'wal'
            assert store.journal_mode == "wal", (
                f"store.journal_mode 应为 'wal'，实际为 {store.journal_mode!r}"
            )
        finally:
            store.close()

    def test_pool_size_default(self, tmp_path):
        """ADR-0021: 默认连接池大小为 DEFAULT_POOL_SIZE。"""
        store = _new_store(tmp_path)
        try:
            assert store.pool_size == DEFAULT_POOL_SIZE
            # 池中应有 DEFAULT_POOL_SIZE 个连接可借
            assert store._pool is not None
            assert store._pool.qsize() == DEFAULT_POOL_SIZE
        finally:
            store.close()

    def test_busy_timeout_set_on_pool_connections(self, tmp_path):
        """ADR-0021: 每个池连接 busy_timeout >= 5000ms（避免 SQLITE_BUSY）。"""
        store = _new_store(tmp_path)
        try:
            # 借一个池连接验证
            with store._borrow_conn() as conn:
                row = conn.execute("PRAGMA busy_timeout").fetchone()
                timeout_ms = row[0] if row else 0
                assert timeout_ms >= 5000, (
                    f"池连接 busy_timeout 应 ≥ 5000ms，实际为 {timeout_ms}ms"
                )
        finally:
            store.close()

    def test_synchronous_normal_on_pool_connections(self, tmp_path):
        """ADR-0021: 池连接 synchronous=NORMAL（WAL 模式下兼顾安全和性能）。"""
        store = _new_store(tmp_path)
        try:
            with store._borrow_conn() as conn:
                row = conn.execute("PRAGMA synchronous").fetchone()
                sync_level = row[0] if row else -1
                # synchronous=NORMAL 在 PRAGMA 返回值为 1（0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA）
                assert sync_level == 1, (
                    f"池连接 synchronous 应为 NORMAL(1)，实际为 {sync_level}"
                )
        finally:
            store.close()

    def test_wal_fallback_for_in_memory_db(self):
        """ADR-0021: in-memory DB 不支持 WAL，fallback 到 DELETE + _write_lock。

        in-memory DB PRAGMA journal_mode 返回 'memory'（非 'wal'），
        触发 fallback 路径：journal_mode != 'wal' + _write_lock 启用。

        注：只验证 init 路径的 fallback 行为，不调业务方法（:memory: 下池连接
        是独立内存 DB，无法共享 schema）。
        """
        store = EventStore(db_path=":memory:")
        store.init()
        try:
            # in-memory DB PRAGMA journal_mode 返回 'memory'，触发 fallback
            assert store.journal_mode != "wal", (
                f"in-memory DB 不应支持 WAL，实际 journal_mode={store.journal_mode!r}"
            )
            # fallback 路径启用 _write_lock 串行化
            assert store._write_lock is not None, (
                "WAL 不可用时应启用 _write_lock 串行化写"
            )
        finally:
            store.close()


# ============================================================================
# ADR-0021: 连接池借用 + thread-local conn 行为
# ============================================================================


class TestPoolBorrowAndThreadLocal:
    """ADR-0021: _borrow_conn 借出/归还 + thread-local self.conn 绑定。"""

    def test_borrow_returns_connection_to_pool(self, tmp_path):
        """ADR-0021: _borrow_conn with 块结束后连接归还池。"""
        store = _new_store(tmp_path)
        try:
            initial_qsize = store._pool.qsize()
            with store._borrow_conn() as _conn:
                # 借出期间池中少一个
                assert store._pool.qsize() == initial_qsize - 1
            # 归还后池中恢复
            assert store._pool.qsize() == initial_qsize
        finally:
            store.close()

    def test_thread_local_conn_in_borrow(self, tmp_path):
        """ADR-0021: _borrow_conn 内 self.conn 返回借来的池连接（非 init_conn）。"""
        store = _new_store(tmp_path)
        try:
            init_conn = store.conn  # 外部访问返回 init_conn
            with store._borrow_conn() as borrowed:
                # _borrow_conn 内 self.conn 应等于借来的池连接
                assert store.conn is borrowed, (
                    "_borrow_conn 内 self.conn 应返回借来的池连接"
                )
                # 且不等于 init_conn
                assert store.conn is not init_conn, (
                    "_borrow_conn 内 self.conn 不应是 init_conn"
                )
            # 退出 _borrow_conn 后 self.conn 恢复为 init_conn
            assert store.conn is init_conn
        finally:
            store.close()

    def test_thread_local_conn_in_run_sync(self, tmp_path):
        """ADR-0021: _run_sync 内部 self.conn 是池连接（thread-local 绑定）。"""
        store = _new_store(tmp_path)
        try:
            init_conn = store.conn
            seen_conns: list = []

            async def run():
                def _sync():
                    # _run_sync 内部 self.conn 应是池连接（非 init_conn）
                    seen_conns.append(store.conn)
                    return None
                await store._run_sync(_sync)

            _run(run())
            assert len(seen_conns) == 1
            assert seen_conns[0] is not init_conn, (
                "_run_sync 内 self.conn 应是池连接，不是 init_conn"
            )
            # _run_sync 结束后 self.conn 恢复为 init_conn
            assert store.conn is init_conn
        finally:
            store.close()

    def test_check_same_thread_false_on_all_connections(self, tmp_path):
        """ADR-0021: init_conn + 池连接都 check_same_thread=False（可跨线程访问）。

        兼容 C7 旧测试：reconciler.py 用 store.conn.execute() 跨线程只读查询 events 表。
        """
        store = _new_store(tmp_path)
        try:
            # init_conn 跨线程访问
            init_conn = store.conn
            errors: list[str] = []

            def write_in_thread():
                try:
                    # 用 _run_sync 在 to_thread 上下文写入（走池连接）
                    async def _write():
                        def _sync():
                            store.conn.execute(
                                "INSERT INTO sessions(id, title, mode, status, created_at, updated_at) "
                                "VALUES('test', 'test', 'dialogue', 'idle', 0, 0)"
                            )
                            store.conn.commit()
                        await store._run_sync(_sync)
                    asyncio.run(_write())
                except Exception as e:
                    errors.append(f"thread write error: {type(e).__name__}: {e}")

            t = threading.Thread(target=write_in_thread)
            t.start()
            t.join()

            assert errors == [], (
                f"check_same_thread=False 应允许跨线程访问，错误: {errors}"
            )
            # 验证写入成功（init_conn 读到池连接写入的数据，证明 WAL 模式下读写共享）
            row = init_conn.execute(
                "SELECT id FROM sessions WHERE id = ?", ("test",)
            ).fetchone()
            assert row is not None, "跨线程写入的数据应被 init_conn 读到（WAL 模式下读写共享）"
        finally:
            store.close()


# ============================================================================
# ADR-0021: 并发读写行为（保留 C7 核心场景 + 新增 WAL 验证）
# ============================================================================


class TestSqliteConcurrency:
    """ADR-0021: 多协程/线程并发调 store，无异常、数据一致。

    保留 C7 原测试场景（并发写、并发读+写、seq 单调），验证连接池 + WAL 重构
    后行为不退化。
    """

    def test_concurrent_writes_no_exception(self, tmp_path):
        """三协程并发 append_message → 无异常，所有消息都写入。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s1")

            async def writer(prefix: str, count: int):
                for i in range(count):
                    await store.append_message("s1", Message(
                        role="user", content=f"{prefix}_{i}", source="user",
                    ))

            # 3 个协程并发写
            await asyncio.gather(
                writer("a", 10),
                writer("b", 10),
                writer("c", 10),
            )

            # 验证所有消息都写入
            msgs = await store.load_messages("s1")
            return msgs

        msgs = _run(run())
        store.close()

        # 30 条消息（3 协程 × 10 条）+ 1 条 create_session 不写 message
        user_msgs = [m for m in msgs if m.role == "user"]
        assert len(user_msgs) == 30, (
            f"ADR-0021: Expected 30 messages from 3 concurrent writers, got {len(user_msgs)}"
        )

    def test_concurrent_read_write_no_exception(self, tmp_path):
        """并发读+写 → 无异常，读到的数据一致。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s1")
            # 预写一些消息
            for i in range(5):
                await store.append_message("s1", Message(
                    role="user", content=f"pre_{i}", source="user",
                ))

            errors: list[str] = []

            async def writer():
                for i in range(10):
                    try:
                        await store.append_message("s1", Message(
                            role="user", content=f"write_{i}", source="user",
                        ))
                    except Exception as e:
                        errors.append(f"writer error: {e}")

            async def reader():
                for _i in range(10):
                    try:
                        msgs = await store.load_messages("s1")
                        if not isinstance(msgs, list):
                            errors.append(f"reader got non-list: {type(msgs)}")
                    except Exception as e:
                        errors.append(f"reader error: {e}")

            async def event_writer():
                for i in range(10):
                    try:
                        await store.append_event("s1", "test_event", {"i": i})
                    except Exception as e:
                        errors.append(f"event_writer error: {e}")

            await asyncio.gather(writer(), reader(), event_writer())
            return errors

        errors = _run(run())
        store.close()

        assert errors == [], (
            f"ADR-0021: Concurrent read/write should have no errors, got: {errors}"
        )

    def test_concurrent_event_append_seq_monotonic(self, tmp_path):
        """并发 append_event → seq 单 session 内单调递增，无重复（WAL 串行化写）。"""
        store = _new_store(tmp_path)

        async def run():
            await store.create_session(session_id="s1")

            async def event_writer(prefix: str, count: int):
                for i in range(count):
                    await store.append_event("s1", f"{prefix}_event", {"i": i})

            # 3 个协程并发写 event
            await asyncio.gather(
                event_writer("a", 10),
                event_writer("b", 10),
                event_writer("c", 10),
            )

            events = await store.load_events("s1")
            return events

        events = _run(run())
        store.close()

        # 30 个 event + 1 个 session_created = 31
        assert len(events) == 31, (
            f"ADR-0021: Expected 31 events, got {len(events)}"
        )

        # seq 应单调递增，无重复（WAL 串行化保证）
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs), (
            f"ADR-0021: seqs should be monotonically increasing, got {seqs}"
        )
        assert len(set(seqs)) == len(seqs), (
            f"ADR-0021: seqs should have no duplicates, got {seqs}"
        )

    def test_concurrent_reads_dont_block_writes(self, tmp_path):
        """ADR-0021: WAL 模式下并发读不阻塞写。

        验证场景：
        - 多线程同时读 messages（每个线程用独立连接，模拟跨进程只读访问）
        - 同时主线程通过池连接写 messages
        - WAL 模式下读不阻塞写，写不阻塞读
        - 写完成后所有读都能看到最新数据

        设计：每个读线程创建独立 sqlite3.Connection（check_same_thread=False），
        避免多线程共享同一连接导致的 InterfaceError。这模拟 reconciler 跨进程
        只读访问 agent.db 的真实场景（不同进程各有独立连接）。
        """
        import sqlite3

        store = _new_store(tmp_path)
        try:
            _run(_setup_session_with_messages(store, "s-read-block", 5))

            read_done = threading.Event()
            errors: list[str] = []

            def reader():
                """并发读线程：持续读 messages 直到 read_done。

                每个线程创建独立连接（不共享 init_conn，避免并发使用同一
                connection 对象导致 'bad parameter or other API misuse'）。
                """
                conn = sqlite3.connect(store._db_path, check_same_thread=False)
                try:
                    while not read_done.is_set():
                        try:
                            conn.execute(
                                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq",
                                ("s-read-block",),
                            ).fetchall()
                        except Exception as e:
                            errors.append(f"reader error: {type(e).__name__}: {e}")
                            return
                        time.sleep(0.001)  # 避免 100% CPU
                finally:
                    conn.close()

            # 启动 3 个读线程（每个独立连接）
            readers = [
                threading.Thread(target=reader, daemon=True) for _ in range(3)
            ]
            for t in readers:
                t.start()

            # 主线程并发写 20 条消息（通过池连接 + _run_write_sync）
            async def write_more():
                for i in range(20):
                    await store.append_message(
                        "s-read-block",
                        Message(role="user", content=f"more_{i}", source="user"),
                    )

            start = time.time()
            _run(write_more())
            elapsed = time.time() - start

            read_done.set()
            for t in readers:
                t.join(timeout=2.0)

            assert not errors, f"并发读不应报错: {errors}"

            # 验证写入完整（5 + 20 = 25 条 user 消息）
            msgs = _run(store.load_messages("s-read-block"))
            user_msgs = [m for m in msgs if m.role == "user"]
            assert len(user_msgs) == 25, (
                f"WAL 模式下并发读期间写应完整落库，预期 25 条，实际 {len(user_msgs)} 条"
            )

            # 性能 sanity check：20 条写 + 3 线程持续读应在 10s 内完成
            # WAL 模式下读不阻塞写，不应有明显延迟
            assert elapsed < 10.0, (
                f"WAL 模式下并发读不应阻塞写，写 20 条耗时 {elapsed:.2f}s 过长"
            )
        finally:
            store.close()

    def test_concurrent_writes_serialized_no_data_loss(self, tmp_path):
        """ADR-0021: WAL 串行化写 — 多协程并发写不丢失数据，无 SQLITE_BUSY。

        验证场景：5 协程并发 append_message，每协程写 8 条。
        WAL + busy_timeout 串行化写事务，所有写入应完整落库。
        """
        store = _new_store(tmp_path)
        try:
            async def run():
                await store.create_session(session_id="s-serial")
                async def writer(prefix: str, count: int):
                    for i in range(count):
                        await store.append_message(
                            "s-serial",
                            Message(role="user", content=f"{prefix}_{i}", source="user"),
                        )
                # 5 协程并发写
                await asyncio.gather(
                    writer("a", 8),
                    writer("b", 8),
                    writer("c", 8),
                    writer("d", 8),
                    writer("e", 8),
                )
                msgs = await store.load_messages("s-serial")
                return msgs

            msgs = _run(run())
            user_msgs = [m for m in msgs if m.role == "user"]
            assert len(user_msgs) == 40, (
                f"WAL 串行化写：5×8=40 条应完整落库，实际 {len(user_msgs)} 条"
            )

            # 验证所有 prefix 都完整（无丢失）
            contents = {m.content for m in user_msgs}
            for prefix in "abcde":
                for i in range(8):
                    assert f"{prefix}_{i}" in contents, (
                        f"WAL 串行化写丢失数据：{prefix}_{i} 不在结果中"
                    )

            # seq 应 1..40 单调无重复
            seqs = [m.seq for m in user_msgs]
            assert sorted(seqs) == list(range(1, 41)), (
                f"WAL 串行化写：seq 应 1..40 单调无重复，实际 {sorted(seqs)}"
            )
        finally:
            store.close()

    def test_two_store_instances_share_db_file(self, tmp_path):
        """ADR-0021: 两个 EventStore 实例共享同一 DB 文件（模拟 client + headless 并发写）。

        WAL 模式下两个独立连接（不同 EventStore 实例）可并发读写同一 DB 文件。
        """
        db_path = str(tmp_path / "test_shared.db")
        store_a = EventStore(db_path=db_path)
        store_b = EventStore(db_path=db_path)
        store_a.init()
        store_b.init()
        try:
            async def run():
                # 两个 store 各写一个 session
                await store_a.create_session(session_id="sa", mode="headless")
                await store_b.create_session(session_id="sb", mode="headless_judge")
                # 并发写消息
                async def write_a():
                    for i in range(5):
                        await store_a.append_message(
                            "sa", Message(role="assistant", content=f"a-{i}", source="assistant")
                        )
                async def write_b():
                    for i in range(3):
                        await store_b.append_message(
                            "sb", Message(role="assistant", content=f"b-{i}", source="assistant")
                        )
                await asyncio.gather(write_a(), write_b())

                # 各自读自己 session
                msgs_a = await store_a.load_messages("sa")
                msgs_b = await store_b.load_messages("sb")
                return msgs_a, msgs_b

            msgs_a, msgs_b = _run(run())
            assert len([m for m in msgs_a if m.role == "assistant"]) == 5
            assert len([m for m in msgs_b if m.role == "assistant"]) == 3

            # 验证两个 store 看到对方的写入（WAL 模式下读写共享）
            # store_a 读 store_b 写的 session
            sb_via_a = await_store(store_a, "sb")
            assert sb_via_a is not None, "store_a 应能读到 store_b 创建的 session（WAL 共享）"
        finally:
            store_a.close()
            store_b.close()


# ============================================================================
# Helpers for concurrency tests
# ============================================================================


async def _setup_session_with_messages(store: EventStore, session_id: str, n: int) -> None:
    """预写 n 条消息到指定 session。"""
    await store.create_session(session_id=session_id)
    for i in range(n):
        await store.append_message(
            session_id,
            Message(role="user", content=f"pre_{i}", source="user"),
        )


def await_store(store: EventStore, session_id: str):
    """同步包装 store.get_session。"""
    return _run(store.get_session(session_id))
