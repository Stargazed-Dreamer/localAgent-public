# Architecture Decision Records (ADR)

本目录记录 LocalAgent 项目的架构决策。每个 ADR 是一个不可变的历史文档，捕获"为什么做出了某个决定"——不是 spec，不是 CHANGELOG，而是未来读者会问"为什么这样设计"时可以查阅的真源。

## 索引

| 编号 | 标题 | 主题域 | 摘要 |
|------|------|--------|------|
| [0001](0001-auto-scoring-routing.md) | Auto-Scoring Routing | LLM 池 | 6 因子加权评分替代 round-robin 主选路（health/quota/latency/cost/tier/lkgp） |
| [0002](0002-llm-pool-module-reorganization.md) | llm_pool 模块重组 | LLM 池 | USE_CASE_REGISTRY 迁入 types.py 打破循环依赖，删除 key_manager.py compat shim |
| [0003](0003-vendor-skill-absorption-criteria.md) | 外部 skill 吸收判断标准 | Skill 体系 | 4 项标准（方法论普适性/未来场景/dev_toolkit 适配/融合成本）避免"前端不紧密就放弃" |
| [0004](0004-prefilter-llm-max-tokens.md) | 预检 LLM max_tokens 必须给足 | 内容审核 | 思考模型 `<think>` 吃光 300 token 导致预检失效，max_tokens=16384 与主调用对齐 |
| [0005](0005-approval-domain-consolidation.md) | 审批领域文件整合 | 审批/守卫 | approval_log → approval_review，command_guard_router → command_guard，5 文件减为 3 |
| [0006](0006-v6-lite-scope-cut.md) | v6-lite 砍范围开工 | 对话引擎 | v6 全量 13 文档降级为参考库，v6-lite.md 唯一开工依据，EventStore 砍到 4 表 |
| [0007](0007-omniparser-removal.md) | OmniParser 移除 | 视觉/OCR | 2026-07-31 移除 OmniParser，文字定位主路径改 PaddleOCR bbox + 远程 VL 兜底 |
| [0008](0008-memory-sqlite-gentlemans-agreement.md) | 记忆系统沿用 SQLite + 君子协议 | 记忆系统 | 不重写为 markdown 文件系统，fact_type closed taxonomy 但写入端君子协议 |
| [0009](0009-toolresult-five-variant-enum.md) | ToolResult 5 变体 enum | 对话引擎 | 从二态 is_error 改为 SUCCESS/ERROR/USER_DENIED/APPROVAL_REQUIRED/TIMEOUT |
| [0010](0010-tool-list-four-categories.md) | 工具清单分四类呈现 | 对话引擎 | 内置基本工具 + 核心 MCP 直暴露 + MCP 子桶 + REST 物化，避免 116 工具平铺 |
| [0011](0011-computer-use-session-manager.md) | computer use 会话管理层独立 | 屏幕操控 | SessionManager 替代 ControlGrantManager，GUIClient 拆为 OverlayClient |
| [0012](0012-session-manager-in-memory-only.md) | 会话管理层纯内存不持久化 | 屏幕操控 | 后端重启重置为无权限，避免"幽灵授权"风险 |
| [0013](0013-release-engine-compiler-architecture.md) | release engine 编译器式架构 | 发布工具 | prepare_release() → build_release() 双接口，审计与构建消费同一 PreparedRelease |
| [0014](0014-digest-bound-approval.md) | digest 绑定 > 人工审批 | 发布工具 | SHA-256 digest 绑定审批，digest 不匹配 fail closed 拒绝构建 |
| [0015](0015-watchdog-shutdown-via-auto-shutdown.md) | watchdog 关机预授权走 auto_shutdown | 屏幕操控 | agent 不持有 shutdown 权限，走 auto_shutdown 模块 + SessionManager 双重校验 |
| [0016](0016-chat-engine-bookkeeping-only.md) | chat 引擎只记账不控制 | 对话引擎 | 删 cost 字段，wall_clock_budget_secs 是唯一预算控制 |
| [0017](0017-secret-unified-gateway.md) | lib/secret 密钥读取统一中转层 | 密钥管理 | 路径真源 + 非 LLM 密钥集中到 secrets.toml，保留 key_store.py 委托路径获取 |
| [0018](0018-route-tags-fail-open-secondary-defense.md) | route_tags 保持 fail-open + 二级防护 | 审批/守卫 | 默认暴露给 agent，靠 classify_safety/approval_level/中间件三层防护，不靠 x-agent-callable 标志 |
| [0019](0019-pydantic-extra-forbid-policy.md) | Pydantic extra='forbid' 全项目策略 | 数据校验 | BaseModel 继承 BaseSchema，传错字段名立即报错避免静默吞导致误报成功 |
| [0020](0020-async-http-httpx-asyncclient.md) | async HTTP 客户端选型 httpx.AsyncClient | HTTP 客户端 | 后端 httpx.AsyncClient 单例 + Qt QThread + httpx.Client，弃 requests/aiohttp |
| [0021](0021-eventstore-connection-pool-wal.md) | EventStore 连接池 + SQLite WAL 模式 | 对话引擎 | 连接池 + WAL 替代单连接 Lock，并发读 + 串行写，为多 agent 并发预留 |
| [0022](0022-memory-db-single-connection-global-lock.md) | memory.db 单连接 + 全局写锁 | 记忆系统 | 6 模块共享单连接 + MemoryStore._write_lock 全局写锁串行化所有写操作 |
| [0023](0023-advanced-tool-dangerous-tools-permission.md) | 网关端点级权限检查清单 DANGEROUS_TOOLS | 审批/守卫 | DANGEROUS_TOOLS 集合，confirm 级强制走 command_guard 审批，其他放行 |
| [0024](0024-multi-process-stats-atomic-write.md) | 多进程并发写 stats 原子写 + last-writer-wins | 统计/并发 | tmp + os.replace 原子写，last-writer-wins 不留半截文件 |
| [0025](0025-uia-semantic-action-focus-exemption.md) | UIA 语义动作豁免焦点校验 | 屏幕操控 | UIA COM 隔离窗口焦点，invoke pattern 不需要 SetForegroundWindow |
| [0026](0026-watchdog-skip-danger-confirm.md) | watchdog 模式跳过 danger=confirm | 屏幕操控 | watchdog 授权即同意 confirm 范围，跳过弹窗保持自动执行体验 |
| [0027](0027-client-server-dependency-boundary.md) | Client-Server 模块依赖边界 | 分层架构 | 提取 lib/config_reader + lib/component_manifest，物理隔离 + 单一真源 |
| [0028](0028-agent-guide-semantic-embedding.md) | Agent Guide 语义向量化匹配 | Agent Guide | 复用 EmbeddingEngine 对 GUIDE_REGISTRY 摘要嵌入，弱匹配时余弦加分，双条件 strong_match |
| [0029](0029-workspace-backup-manifest-decoupled.md) | workspace 数据备份方案 | 备份/恢复 | 新增 [backup] 段独立于 [watch] 段，与 [secret_backup] 对称设计但独立配置目录和保留策略 |
| [0030](0030-model-lifecycle-manager-architecture-decisions.md) | Model Lifecycle Manager 架构决策（5 项） | 模型生命周期 | 薄编排 vs 所有权重构 / 进程内优先 / VRAM 主动 + RAM 预留 / 守护内部卸载绕过审批 / 热路径嵌入默认钉住 |
| [0031](0031-inbound-gateway-own-sqlite-stats.md) | 入站网关统计自持 | 入站网关 | 网关 SQLite 是调用统计唯一真源，不复用池统计；TTFT 网关层计时。⚠️ 2026-09-02 修订：流式 `upstream_key` 缺失限制已解除、池子树只读边界纯增量放宽（见文末 Amendment）。⚠️ 原决策语境「`pool.stream()` 池侧零统计」已被 [ADR-0033](0033-stream-usage-collector-not-token-counter.md) 改变（流式现已记池侧 token），但「网关不复用池统计」核心结论不变 |
| [0032](0032-keys-json-hot-reload-guard.md) | keys.json 热重载守护 | LLM 池 | 20s 轮询 mtime + 在途让位重建池，替代"手动 reload"；启动/重载自检 `inbound_gateway` 授权覆盖，独立于池子树 |
| [0033](0033-stream-usage-collector-not-token-counter.md) | 流式 token 记账 = usage 采集器（非计数器） | 统计/并发 | 流式 token 只采 provider 权威 usage，缺失诚实标 `provider_missing` 记 0 + 精确字符数，**绝不本地估算**；补齐 `pool.stream()` 池侧记账 + Anthropic 原生 SSE；升格 ADR-0031 决策 #4「不估算」为跨池/网关全局原则。**Correction**：曾一度基于"聚合代理丢 SSE usage"的错误前提给网关加了默认关的「缓冲伪流式」开关，后原始 SSE dump 推翻该前提——真因是 `_stream_openai_sse` 看到 `finish_reason` 就 break、漏读其后的尾部 usage chunk（本端 bug，已修：读到 `[DONE]` 再统一收尾，仿 Anthropic）；据此缓冲开关已**彻底移除**，真流式**同时**得到逐字直播思维 + provider 精确 token（实测 total=395 / reasoning 529 字），网关真流式并把 provider 的 usage 尾 chunk 转发给外部客户端、透传真实 `finish_reason` |

## 主题域分组

- **LLM 池**：0001, 0002, 0032
- **对话引擎**：0006, 0009, 0010, 0016, 0021
- **记忆系统**：0008, 0022
- **屏幕操控**：0011, 0012, 0015, 0025, 0026
- **审批/守卫**：0005, 0018, 0023
- **视觉/OCR**：0007
- **内容审核**：0004
- **Skill 体系**：0003
- **发布工具**：0013, 0014
- **密钥管理**：0017
- **数据校验**：0019
- **HTTP 客户端**：0020
- **统计/并发**：0024, 0033
- **Agent Guide**：0028
- **分层架构**：0027
- **备份/恢复**：0029
- **模型生命周期**：0030
- **入站网关**：0031

## 何时写 ADR

只有以下三项**全部**成立时才写 ADR（详见 [ADR-FORMAT.md](../../.agents/skills/dev/domain-modeling/ADR-FORMAT.md)）：

1. **Hard to reverse** - 反转成本有意义（改一个 if-block 不算）
2. **Surprising without context** - 未来读者会问"为什么这样设计"
3. **Real trade-off** - 确实有被拒绝的替代方案

反模式（**不写 ADR**，CHANGELOG 足矣）：bug 修复、安全补丁、性能优化、可逆代码调整、字段语义显式化、内存泄漏修复、单点焦点校验。

## 如何写 ADR

1. 扫描本目录最高编号，递增一作为新编号
2. 文件名格式：`NNNN-kebab-case-slug.md`
3. 遵循 [ADR-FORMAT.md](../../.agents/skills/dev/domain-modeling/ADR-FORMAT.md) 模板
4. 参考现有 ADR（如 0001、0002）的结构：标题 + 摘要 + Context + Decision + Considered Options + Consequences + References
5. 代码路径用 `file:///` 绝对路径链接
6. 写完后在本 README 索引表格追加一行

## 编号冲突历史

2026-08-06 修复了一次编号冲突：`0001-approval-domain-consolidation.md` 与 `0001-auto-scoring-routing.md` 同号。因 `auto-scoring-routing` 被 18 处代码/文档引用为 ADR-0001（config.example.toml / server/config.py / server/llm_pool/pool.py / scoring.py 等），保留其编号不变；`approval-domain-consolidation` 重编号为 0005。新 ADR 从 0017 起编号。
