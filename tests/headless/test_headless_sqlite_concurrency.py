"""headless-agent-session Ticket 08 验收测试：SQLite 跨进程写测试

覆盖 Ticket 08 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新建 tests/test_headless_sqlite_concurrency.py
- [x] 测试用例 1：用 threading.Thread 模拟 client 写 agent.db 期间，后端 headless session 写
- [x] 测试用例 2：同时启动主会话 + judge 会话写 agent.db
- [x] 验证不报 SQLITE_BUSY
- [x] 验证数据一致性（两边写入的数据都正确）

设计依据：
- EventStore 已启用 WAL 模式（PRAGMA journal_mode=WAL + synchronous=NORMAL）
  - WAL 模式下：读不阻塞写、写不阻塞读、多写并发由 WAL 框架串行化
  - 单写锁仍然存在：同时刻只能一个写事务，另一个等（默认 timeout 5s）
- spec Bounds 节止损规则：3 天没找到就掉头改独立 DB
- 本测试只验证 WAL 模式下不报 SQLITE_BUSY（事务串行化等待，不抛异常）

测试策略：
- 用 asyncio.gather 并发执行多个 EventStore 写入
- 用 threading.Thread 模拟跨进程写（client 进程 + 后端 headless 进程同时写）
- 验证所有写入都成功，数据一致
- 不验证写入顺序（WAL 串行化由 SQLite 决定）
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import EventStore, Message  # noqa: E402

# ============================================================================
# 1. asyncio 并发写（同进程多 EventStore 实例，模拟主会话 + judge 会话同时写）
# ============================================================================


def test_concurrent_async_writes_no_busy(tmp_db_path):
    """Ticket 08：同时启动主会话 + judge 会话写 agent.db，不报 SQLITE_BUSY。

    模拟场景：
    - 主会话 EventStore 写 messages
    - judge 会话 EventStore 同时写 messages
    - 两边各自有自己的连接，但写同一个 DB 文件

    WAL 模式下应：
    - 两个写事务串行化（一个先写，另一个等）
    - 不报 SQLITE_BUSY（默认 busy_timeout 5s）
    - 两边写入的数据都正确
    """
    async def write_session(store: EventStore, session_id: str, n: int) -> int:
        """向指定 session 写 n 条消息，返回写入数。"""
        await store.create_session(session_id=session_id, mode="headless")
        for i in range(n):
            await store.append_message(
                session_id,
                Message(role="assistant", content=f"{session_id}-msg-{i}", source="assistant"),
            )
        return n

    async def run():
        # 两个独立 EventStore 实例（模拟主会话 + judge 会话各自连接）
        store_main = EventStore(db_path=tmp_db_path)
        store_judge = EventStore(db_path=tmp_db_path)
        store_main.init()
        store_judge.init()
        try:
            # 并发写（asyncio.gather）
            results = await asyncio.gather(
                write_session(store_main, "headless-concurrent-1", 10),
                write_session(store_judge, "headless-judge-concurrent-1", 5),
            )
            return results
        finally:
            store_main.close()
            store_judge.close()

    results = asyncio.run(run())
    assert results == [10, 5]

    # 验证数据一致性：用新连接查询
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            # 主会话 10 条 assistant 消息
            main_msgs = await verify_store.load_messages("headless-concurrent-1")
            main_assistant = [m for m in main_msgs if m.role == "assistant"]
            assert len(main_assistant) == 10
            # judge 会话 5 条 assistant 消息
            judge_msgs = await verify_store.load_messages("headless-judge-concurrent-1")
            judge_assistant = [m for m in judge_msgs if m.role == "assistant"]
            assert len(judge_assistant) == 5
            # 内容正确
            main_contents = [m.content for m in main_assistant]
            for i in range(10):
                assert f"headless-concurrent-1-msg-{i}" in main_contents
        asyncio.run(verify())
    finally:
        verify_store.close()


def test_concurrent_async_writes_many_sessions_no_busy(tmp_db_path):
    """Ticket 08：5 个 session 同时写 agent.db，不报 SQLITE_BUSY。

    加压测试：5 个并发写事务，验证 WAL 串行化能处理。
    """
    async def write_one(store: EventStore, session_id: str, n: int) -> int:
        await store.create_session(session_id=session_id, mode="headless")
        for i in range(n):
            await store.append_message(
                session_id,
                Message(role="assistant", content=f"{session_id}-{i}", source="assistant"),
            )
        return n

    async def run():
        stores = [EventStore(db_path=tmp_db_path) for _ in range(5)]
        for s in stores:
            s.init()
        try:
            tasks = [
                write_one(stores[i], f"headless-pressure-{i}", 3 + i)
                for i in range(5)
            ]
            return await asyncio.gather(*tasks)
        finally:
            for s in stores:
                s.close()

    results = asyncio.run(run())
    assert results == [3, 4, 5, 6, 7]

    # 验证数据一致
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            for i in range(5):
                sid = f"headless-pressure-{i}"
                msgs = await verify_store.load_messages(sid)
                assistant_msgs = [m for m in msgs if m.role == "assistant"]
                assert len(assistant_msgs) == 3 + i
        asyncio.run(verify())
    finally:
        verify_store.close()


# ============================================================================
# 2. 跨线程写（模拟 client 进程 + 后端 headless 进程同时写）
# ============================================================================


def test_cross_thread_writes_no_busy(tmp_db_path):
    """Ticket 08：跨线程（模拟跨进程）同时写 agent.db，不报 SQLITE_BUSY。

    模拟场景（真实生产时序）：
    - 主线程（模拟 client 进程）先 init agent.db（设置 WAL）
    - 子线程（模拟后端 headless 进程）后 init agent.db（WAL 已设置，PRAGMA 幂等）
    - 两边各自有自己的连接，并发写不同 session 的消息

    SQLite WAL 模式下：
    - 多个读 + 一个写可并发
    - 多个写串行化（不并发），但通过 busy_timeout 等待而非抛 SQLITE_BUSY
    - PRAGMA journal_mode=WAL 需要排它锁，不能两个连接同时执行（所以 init 分阶段）
    """
    # 主线程先 init（独占，设置 WAL）
    main_store = EventStore(db_path=tmp_db_path)
    main_store.init()

    # 同步屏障：主线程 init 完后启动子线程，子线程 init 完后两边并发写
    main_inited = threading.Event()
    child_inited = threading.Event()
    write_started = threading.Event()

    def thread_write(db_path: str, session_id: str, n: int, errors: list):
        """子线程写函数。"""
        try:
            # 等主线程 init 完成
            main_inited.wait(timeout=5.0)
            # 子线程 init（此时 WAL 已设置，PRAGMA journal_mode=WAL 幂等返回 wal）
            store = EventStore(db_path=db_path)
            store.init()
            child_inited.set()

            # 等主线程发令，两边并发写
            write_started.wait(timeout=5.0)

            async def write():
                await store.create_session(session_id=session_id, mode="dialogue")
                for i in range(n):
                    await store.append_message(
                        session_id,
                        Message(role="user", content=f"{session_id}-user-{i}", source="user"),
                    )
            asyncio.run(write())
            store.close()
        except Exception as e:
            errors.append(f"thread {session_id}: {type(e).__name__}: {e}")

    errors: list = []
    # 启动子线程（模拟后端 headless 进程）
    t = threading.Thread(
        target=thread_write,
        args=(tmp_db_path, "client-thread-session", 5, errors),
        daemon=True,
    )
    t.start()

    # 通知子线程主线程已 init
    main_inited.set()
    # 等子线程 init 完成
    assert child_inited.wait(timeout=5.0), "子线程 init 超时"

    # 两边并发写
    write_started.set()

    async def main_write():
        await main_store.create_session(session_id="headless-thread-main", mode="headless")
        for i in range(5):
            await main_store.append_message(
                "headless-thread-main",
                Message(role="assistant", content=f"main-{i}", source="assistant"),
            )
    asyncio.run(main_write())
    main_store.close()

    # 等子线程结束
    t.join(timeout=10.0)
    assert not t.is_alive(), "子线程超时未结束"

    # 验证无 SQLITE_BUSY 错误
    assert not errors, f"子线程写入出错: {errors}"

    # 验证数据一致
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            # 主线程写入
            main_msgs = await verify_store.load_messages("headless-thread-main")
            assert len([m for m in main_msgs if m.role == "assistant"]) == 5
            # 子线程写入
            client_msgs = await verify_store.load_messages("client-thread-session")
            assert len([m for m in client_msgs if m.role == "user"]) == 5
        asyncio.run(verify())
    finally:
        verify_store.close()


# ============================================================================
# 3. WAL 模式验证
# ============================================================================


def test_db_uses_wal_journal_mode(tmp_db_path):
    """Ticket 08：验证 EventStore 启用 WAL 模式（spec v6-01 §1）。

    ADR-0021：store.conn 返回 init_conn（外部只读访问兼容 C7 旧 API）。
    """
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        # PRAGMA journal_mode 返回当前模式（store.conn 在非 _run_sync 上下文返回 init_conn）
        result = store.conn.execute("PRAGMA journal_mode").fetchone()
        mode = result[0].lower() if result else ""
        assert mode == "wal", f"journal_mode 应为 wal，实际为 {mode!r}"
        # ADR-0021：journal_mode property 也应返回 'wal'
        assert store.journal_mode == "wal"
    finally:
        store.close()


def test_db_busy_timeout_set(tmp_db_path):
    """Ticket 08：验证 busy_timeout 已设置（避免 SQLITE_BUSY）。

    EventStore 应设置 busy_timeout ≥ 5000ms（5s），让并发写等待而非立即抛 BUSY。
    ADR-0021：init_conn + 池连接都设了 busy_timeout，这里验证 init_conn。
    """
    store = EventStore(db_path=tmp_db_path)
    store.init()
    try:
        result = store.conn.execute("PRAGMA busy_timeout").fetchone()
        timeout_ms = result[0] if result else 0
        assert timeout_ms >= 5000, f"busy_timeout 应 ≥ 5000ms，实际为 {timeout_ms}ms"
    finally:
        store.close()


# ============================================================================
# 4. 长事务期间并发写（更接近真实场景）
# ============================================================================


def test_long_transaction_concurrent_writes_no_busy(tmp_db_path):
    """Ticket 08：长事务期间另一个连接写，不报 SQLITE_BUSY。

    模拟场景：
    - 主会话写入 100 条消息（耗时较长）
    - 期间 judge 会话同时写 10 条消息
    - WAL + busy_timeout 应让 judge 等待而非报 BUSY
    """
    async def write_many(store: EventStore, session_id: str, n: int, mode: str) -> int:
        await store.create_session(session_id=session_id, mode=mode)
        for i in range(n):
            await store.append_message(
                session_id,
                Message(role="assistant", content=f"{session_id}-{i}", source="assistant"),
            )
        return n

    async def run():
        store_main = EventStore(db_path=tmp_db_path)
        store_judge = EventStore(db_path=tmp_db_path)
        store_main.init()
        store_judge.init()
        try:
            # 主会话写 100 条 + judge 会话写 10 条，并发
            results = await asyncio.gather(
                write_many(store_main, "headless-long-main", 100, "headless"),
                write_many(store_judge, "headless-judge-long", 10, "headless_judge"),
            )
            return results
        finally:
            store_main.close()
            store_judge.close()

    start = time.time()
    results = asyncio.run(run())
    elapsed = time.time() - start
    assert results == [100, 10]

    # 验证数据一致
    verify_store = EventStore(db_path=tmp_db_path)
    verify_store.init()
    try:
        async def verify():
            main_msgs = await verify_store.load_messages("headless-long-main")
            assert len([m for m in main_msgs if m.role == "assistant"]) == 100
            judge_msgs = await verify_store.load_messages("headless-judge-long")
            assert len([m for m in judge_msgs if m.role == "assistant"]) == 10
        asyncio.run(verify())
    finally:
        verify_store.close()

    # 不应有 SQLITE_BUSY（测试通过即说明没报错）
    # 性能 sanity check：100+10 条消息应在 30s 内完成（WAL 串行化）
    assert elapsed < 30.0, f"并发写耗时过长: {elapsed:.1f}s"
