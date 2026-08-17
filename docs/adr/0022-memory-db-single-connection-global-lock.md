# memory.db 单连接 + 全局写锁

memory.db 被 6 模块共享（MemoryStore/TodosStore/EvidenceLedger/SearchTracer/Recorder/Maintainer），写锁不一致导致 `last_insert_rowid` 错乱（batch 6 C1+H1+H3）。决策：单连接 + `MemoryStore._write_lock` 全局写锁串行化所有写操作。6 模块共享单连接 + 全局锁是最简单可靠的方案，避免拆分多文件的 ATTACH 复杂度。T06 已实施。
