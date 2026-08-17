# 多进程并发写 stats 原子写 + last-writer-wins

activity_tracker stats 被 llm_pool/vl_quota/loop_manager 多进程并发写，read-modify-write race（batch 6b H1+H2）。决策：tmp + `os.replace` 原子写 + last-writer-wins（最后一个写的胜出，读-改-写 race 仍存在但不留半截文件）。多进程 SQLite 写锁竞争可能产生 database is locked，原子写 + last-writer-wins 是最简单可靠的方案。T06 已实施。
