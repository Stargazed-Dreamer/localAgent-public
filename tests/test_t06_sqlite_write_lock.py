"""T06 SQLite 统一写锁验收测试

覆盖 temp/sdd/code-review-p0/tickets.md T06 Anti-Cheat 项：
- 多线程并发 insert_message 返回的 lastrowid 与写入内容一一对应（不串扰）
- 并发写不报 "database is locked"

设计依据：
- 修复前：insert_message 用 `SELECT last_insert_rowid()` 在多线程共享 connection 下
  会返回其他线程刚插入的 id，导致语义索引建到错误消息上（批次 6 C1）
- 修复后：用同一 cursor 的 `cur.lastrowid` + INSERT 包入 `_write_lock`，
  保证 lastrowid 与 INSERT 在同一临界区内，不会串扰
"""

from __future__ import annotations

import gc
import os
import tempfile
import threading
from collections import Counter

import pytest

from server.memory.store import MemoryStore

# ==================== Fixtures ====================

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Windows 上 sqlite 连接未关闭会导致文件无法删除
    gc.collect()
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.unlink(p)
            except (PermissionError, OSError):
                pass


@pytest.fixture
def store(tmp_db):
    s = MemoryStore(tmp_db)
    s.initialize()
    yield s
    s.close()
    gc.collect()


# ==================== T06 验收测试 ====================


class TestConcurrentInsertLastrowid:
    """T06 Anti-Cheat：多线程并发 insert lastrowid 正确性"""

    def test_concurrent_insert_returns_correct_lastrowid(self, store):
        """多线程并发 insert_message，每个线程拿到的 id 必须对应自己写入的内容。

        修复前会失败：`SELECT last_insert_rowid()` 在共享 connection 下返回
        最后一个线程插入的 id，导致所有线程拿到同一个 id（或串扰）。
        修复后：`cur.lastrowid` 在 _write_lock 临界区内取同一 cursor 的 id，
        每个线程拿到自己 INSERT 的 id。
        """
        num_threads = 8
        per_thread = 20
        barrier = threading.Barrier(num_threads)
        results: list[tuple[int, str]] = []
        results_lock = threading.Lock()
        errors: list[str] = []

        def worker(tid: int):
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                errors.append(f"thread-{tid}: barrier broken")
                return
            for i in range(per_thread):
                content = f"thread-{tid}-msg-{i}"
                msg_id = store.insert_message(content=content, source="agent")
                with results_lock:
                    results.append((msg_id, content))

        threads = [threading.Thread(target=worker, args=(t,), daemon=True)
                   for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert not errors, f"线程出错: {errors}"
        assert not any(t.is_alive() for t in threads), "有线程超时未结束"

        # 1. id 全局唯一（无重复）
        ids = [r[0] for r in results]
        id_counts = Counter(ids)
        duplicates = {i: c for i, c in id_counts.items() if c > 1}
        assert not duplicates, f"出现重复 id（lastrowid 串扰）: {duplicates}"

        # 2. 每个 id 对应的内容可通过 get_message 查回且匹配
        #    （证明 lastrowid 没指向错误消息）
        for msg_id, expected_content in results:
            msg = store.get_message(msg_id)
            assert msg is not None, f"id={msg_id} 查不到消息"
            assert msg["content"] == expected_content, (
                f"id={msg_id} 内容不匹配: 期望 {expected_content!r}, 实际 {msg['content']!r}"
            )

        # 3. 总数 = num_threads * per_thread
        assert len(results) == num_threads * per_thread

    def test_concurrent_insert_no_database_locked(self, store):
        """T06 Anti-Cheat：并发写不报 "database is locked"。

        _write_lock 串行化写事务 + WAL 模式 busy_timeout，
        并发写应等待而非抛 SQLITE_BUSY。
        """
        num_threads = 6
        per_thread = 15
        barrier = threading.Barrier(num_threads)
        errors: list[str] = []
        write_count = [0] * num_threads

        def worker(tid: int):
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                errors.append(f"thread-{tid}: barrier broken")
                return
            for i in range(per_thread):
                try:
                    store.insert_message(
                        content=f"lock-test-{tid}-{i}",
                        source="agent",
                    )
                    write_count[tid] += 1
                except Exception as e:
                    msg = str(e).lower()
                    if "locked" in msg or "busy" in msg:
                        errors.append(f"thread-{tid}: {type(e).__name__}: {e}")
                    else:
                        raise

        threads = [threading.Thread(target=worker, args=(t,), daemon=True)
                   for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert not errors, f"并发写报 database locked/busy: {errors}"
        assert sum(write_count) == num_threads * per_thread


class TestConcurrentMixedWrites:
    """T06：混合写操作（insert + update + delete）并发不冲突"""

    def test_concurrent_insert_update_delete_no_error(self, store):
        """insert / mark_compressed / delete 三类写操作并发执行不报错。

        验证 _write_lock 对所有写操作生效（不只 insert）。
        """
        # 预先插入一批消息供 update/delete 使用
        pre_ids = [store.insert_message(content=f"pre-{i}", source="agent")
                   for i in range(20)]

        barrier = threading.Barrier(3)
        errors: list[str] = []

        def inserter():
            try:
                barrier.wait(timeout=5.0)
                for i in range(10):
                    store.insert_message(content=f"ins-{i}", source="agent")
            except Exception as e:
                errors.append(f"inserter: {type(e).__name__}: {e}")

        def updater():
            try:
                barrier.wait(timeout=5.0)
                # mark_compressed 批量更新
                for i in range(0, 20, 5):
                    store.mark_compressed(pre_ids[i:i + 5])
            except Exception as e:
                errors.append(f"updater: {type(e).__name__}: {e}")

        def deleter():
            try:
                barrier.wait(timeout=5.0)
                for i in range(10, 20):
                    store.delete_message(pre_ids[i])
            except Exception as e:
                errors.append(f"deleter: {type(e).__name__}: {e}")

        threads = [
            threading.Thread(target=inserter, daemon=True),
            threading.Thread(target=updater, daemon=True),
            threading.Thread(target=deleter, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert not errors, f"混合并发写出错: {errors}"
        assert not any(t.is_alive() for t in threads), "有线程超时未结束"
