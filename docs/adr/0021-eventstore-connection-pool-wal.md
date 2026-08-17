# EventStore 连接池 + SQLite WAL 模式

C7 已实施单连接 + Lock 串行化所有 DB 操作；v6-lite 单 agent 单 session 架构，并发量 1-2 ops/s。决策：重构为连接池 + SQLite WAL 模式，每个 worker 独立 connection，WAL 允许并发读 + 串行写。用户决策接受重构成本，为未来多 agent 并发预留扩展空间。

## Consequences

需重构 `client/core/agent/event_store.py`，重写 C7 测试，处理 SQLite WAL 在 Windows 的边界 case（网络盘不支持 WAL 需 fallback 到 DELETE 模式）。

## 实施修正

原设计用 `BEGIN IMMEDIATE + busy_timeout` 串行化写操作（替代 threading.Lock）。但实施后发现 Python sqlite3 多线程（asyncio.to_thread + 连接池）下 `BEGIN IMMEDIATE` 锁等待不可靠——多线程同时 `BEGIN IMMEDIATE` 立即抛 "database is locked" 而非等待 busy_timeout（5s）。

修正后方案：WAL + DELETE 模式统一用 `_write_lock`（threading.Lock）串行化进程内写操作，WAL 仍保留作为并发读优化（读不阻塞写、写不阻塞读）。跨进程保护靠 SQLite 内置文件锁（WAL 模式下多进程写串行）。`BEGIN IMMEDIATE` 被移除，因为它在 Python sqlite3 多线程下的锁等待行为与文档描述不符。
