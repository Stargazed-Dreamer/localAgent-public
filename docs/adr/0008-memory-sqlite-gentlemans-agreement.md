# 记忆系统沿用 SQLite + 君子协议（不重写为 markdown 文件系统）

v6-07 设计了"markdown 文件系统"记忆路线，但现有 SQLite v3 已具备 BM25 + 向量 + EvidenceLedger + SearchTracer 的较强检索能力，推倒重来代价大。本决策在 SQLite 基础上增量改造，不重写为 markdown 文件系统；`fact_type` 改 closed taxonomy 但写入端用"君子协议"（Pydantic Literal 推荐而非强制），WHAT_NOT_TO_SAVE 写入 system prompt 警告但后端不拒绝，敏感信息也走君子协议。不实现 autoMemoryPath 安全校验（SQLite 路径硬编码无逃逸）。

## Status

accepted (2026-08-05)

## Context

v6-07（记忆/Skill 模块设计）规划了基于 markdown 文件系统的记忆架构，包含 MEMORY.md index 双 cap、autoMemoryPath 安全校验等机制。但现有记忆系统已是 SQLite v3（9 表 + 向量语义检索 + BM25 + HMS 风格三源召回 + EvidenceLedger + SearchTracer），检索能力完备且封装良好。

重写为 markdown 文件系统的代价：
- 现有 SQLite 的 BM25/向量/EvidenceLedger/SearchTracer 能力需在 markdown 路线下重新实现或放弃。
- 架构偏差大，使用体感不一致。
- v6-07 的 closed taxonomy / WHAT_NOT_TO_SAVE / autoMemoryPath 等约束若硬性拦截（拒绝保存），可能引发用户不满——用户存的数据被后端拒绝会打断工作流。

## Decision

1. **在现有 SQLite v3 基础上增量改造，不重写为 markdown 文件系统**。沿用 `server/memory/` 的 manager/store/schema/bm25/semantic/evidence/search_tracer 模块结构。
2. **`fact_type` 改 closed taxonomy（5 类：`user`/`feedback`/`project`/`reference`/`experience`），但写入端用君子协议**：Pydantic 加 `Literal[...]` 约束作为推荐，未知类型不强制拒绝，而是落 `NULL` + log warning。取消 `transaction` 类（事务走专门业务表如 todos/wip），`preference` 改名 `user`，新增 `feedback` 类。
3. **WHAT_NOT_TO_SAVE 禁清单写入 system prompt 警告但后端不拒绝**：禁清单只禁"代码模式/架构/文件路径/git history/CLAUDE.md 已有内容/ephemeral task details"，不禁"debug 解决方案/坑点/最佳实践"（这些归 `experience` 类，不可从代码推导）。后端不硬性拦截，依赖模型自觉。
4. **敏感信息走君子协议，不强制脱敏**：强制脱敏会损失语义（如 API key 上下文），依赖 system prompt 警告 + 模型自觉。
5. **不实现 autoMemoryPath 安全校验**：SQLite 路径硬编码在 `server/memory/`，无路径逃逸问题，markdown 路线的 autoMemoryPath 校验无对应场景。
6. **staleness 警告按 `fact_type` 分类阈值**（`user`>90 天 / `feedback`>30 天 / `project`>7 天 / `reference`>30 天 / `experience`>14 天），`memory_get`/`memory_search` 返回时超阈值追加 `staleness_warning` 字段 + "请用 Read/Grep 验证"提示，但后端不自动跑验证（成本高，模型自己调更灵活）。

## Considered Options

1. **按 v6-07 重写为 markdown 文件系统**——被拒：现有 SQLite 已有较强检索能力（BM25 + 向量 + EvidenceLedger + SearchTracer），推倒重来代价大；markdown 路线与现有架构偏差大，使用体感不一致。
2. **硬性拦截拒绝保存（违反 WHAT_NOT_TO_SAVE / 未知 fact_type / 含敏感信息）**——被拒：硬性拦截可能引发用户不满，用户存的数据被后端拒绝会打断工作流；君子协议更柔性，依赖模型自觉 + 提示引导。
3. **强制脱敏敏感信息**——被拒：脱敏会损失语义（如 API key 的用途上下文），且脱敏规则难以覆盖所有场景；君子协议 + staleness 警告 + TRUSTING_RECALL 提示已能控制风险。

## Consequences

**正面**：
- 复用现有 SQLite 检索能力，零迁移成本，无功能回退。
- 君子协议柔性约束，不打断用户工作流。
- staleness 警告（硬信号）+ "请验证"提示（软引导）配合，控制过时记忆风险。

**负面**：
- 君子协议依赖模型自觉，可能漏存（模型不存该存的）或误存（模型存了 WHAT_NOT_TO_SAVE 禁的）——靠 staleness + TRUSTING_RECALL 兜底，但非机制级保障。
- closed taxonomy 的"物理位置"（Pydantic Literal）与"逻辑执行"（君子协议不拒绝）分离，新读者可能困惑为何约束不强制——靠本文档解释。

**回退路径**：如需从君子协议升级为硬性拦截，后端 `memory_set` 加拒绝逻辑即可（schema 不变，`fact_type` 字段已存在）；如需重写为 markdown 文件系统，需重新实现 BM25/向量/EvidenceLedger，代价大但 SQLite 数据可导出。

## References

- 决策来源：[`temp/sdd/memory-prompt-tool-refactor/00-decisions.md`](file:///<project_root>/temp/sdd/memory-prompt-tool-refactor/00-decisions.md) D1（增量改造 SQLite 君子协议）、D4/D7（5 类 closed taxonomy）、D16（experience 类 vs WHAT_NOT_TO_SAVE）、D9/D14（staleness 警告 + TRUSTING_RECALL）、D15（不做 MEMORY.md 双 cap）
- 关键代码路径：[server/memory/manager.py](file:///<project_root>/server/memory/manager.py)、[server/memory/store.py](file:///<project_root>/server/memory/store.py)、[server/memory/config.py](file:///<project_root>/server/memory/config.py)（staleness 阈值常量）、[server/memory/schema.py](file:///<project_root>/server/memory/schema.py)
- 相关 ADR：无（独立模块决策）
