# Model Lifecycle Manager 架构决策（5 项）

2026-08-24 引入 Model Lifecycle Manager（`server/model_manager/`）作为统一管理后端进程内 4 个本地模型（PaddleOCR / memory_embedding / guide_embedding / mindforge_searcher）生命周期的薄编排层。设计文档 `temp/sdd/model-lifecycle-manager/design.md`（v2 二轮对抗性审查合并 1 P0 + 7 P1 + 9 P2）。本 ADR 记录 5 项核心架构决策，全部通过 3/3 评分（hard to reverse / surprising without context / real trade-off）。

## Decision 1：薄编排 vs 所有权重构

管理器只做"决策权威"（准入/逐出/状态聚合），模块保留单例与公开入口，驱动适配器把统一接口翻译到各模块既有单例。**不做所有权重构**——不重写 5 种单例姿势（模块级全局 / 实例挂 MemoryManager / 类级 + 模块级双层 / SearchEngineManager 单例 / 5 模块各自实现）。

### Considered Options

被拒绝的替代方案：

1. **所有权全量重构**（把 5 模块单例合并到 ModelManager）
   - 拒绝理由：5 种单例姿势一次性重构高风险（每个模块有自己的懒加载/初始化守卫/双层单例，强行合并可能破坏现有调用方契约）
   - 一次性改动面过大，且 ocr-vram-guard 已验证"薄编排 + 驱动适配器"模式可达成核心目标（自动卸载/拒绝/懒恢复）

2. **不抽象，每模块各自实现生命周期**（OCR 已有 ocr-vram-guard，其余不管）
   - 拒绝理由：embedding/MindForge 无统一卸载/状态接口，手动操作只能用模块各自端点；新增本地模型需重写一套

### Consequences

- 未来读者看到 `OcrDriver._models()` / `MemoryEmbeddingDriver._engine()` 懒绑定会困惑"为何不直接持有引用"——本 ADR 解释：模块可能在管理器注册后才创建（如 MemoryManager 在 lifecycle todos 迁移中创建），懒绑定避免循环依赖
- 驱动协议 `ModelDriver` 是未来新本地模型接入契约（如本地 VL 回归、图标检测），新模型只需实现驱动并 `registry.register()`，无需动管理器

## Decision 2：进程内优先、进程外暂缓

管理器只管**后端进程内**模型（驱动 #1-#4）。MindForge 转换器守护（独立子进程 ~2-4GB）、录制器 STT（库代码按需）在独立进程，模型级不可控——本期范围外，文档提及；未来可加"进程监督者"扩展位（接口预留，不实现）。

### Considered Options

被拒绝的替代方案：

1. **跨进程模型级管理**（扩展管理器到子进程）
   - 拒绝理由：跨进程模型级不可控——子进程内单例姿势、生命周期、卸载协议都不可见，强行管理会引入"控制面与执行面状态不一致"风险
   - MindForge 转换器守护已有 start/stop 接口（进程级），模型级管理收益不明
   - 录制器 STT 无生产调用方，本期不值得投入

2. **子进程模型卸载委托**（子进程暴露 unload RPC）
   - 拒绝理由：每个子进程都要实现一遍 unload RPC，重复造轮子；且子进程崩溃后状态丢失，管理器要维护"假设存活/已死"两套语义

### Consequences

- 未来读者看到 `tools/mindforge/converter_daemon.py` 的 ~2-4GB 占用不被管理时会困惑——本 ADR 解释：进程级管理已通过 start/stop 实现，模型级暂缓
- 接口预留位 `ProcessSupervisorDriver`（未实现）记录在 `temp/sdd/model-lifecycle-manager/design.md` §12，未来需要时实现

## Decision 3：通用层只主动管 VRAM、RAM 框架预留

抽象支持 `gpu`/`cpu` 两类资源，**主动策略只开 GPU（VRAM）**——那是真实痛点。CPU 内存监控与逐出**实现但默认关闭**（阈值给到几乎不触发），因为 8GB+ 主机内存下 ~2GB 搜索引擎不构成压力，且误杀代价高。

### Considered Options

被拒绝的替代方案：

1. **CPU 监控也默认开**
   - 拒绝理由：8GB+ 主机内存下 ~2GB MindForge 不构成压力；误杀代价高（搜索引擎冷启动 10-30s）；阈值定到几乎不触发反而无意义
   - MindForge 是当前唯一 `evictable=True` 的 CPU 模型，但默认关 CPU 监控等于 MindForge 永远不被自动逐出，仅手动卸载可达——这是设计选择

2. **不实现 CPU 监控，纯 GPU 专用**
   - 拒绝理由：未来可能有 CPU 密集型本地模型（如本地 whisper 大模型），框架不预留会限制扩展性
   - 框架预留成本极低（`PressureMonitor` 类已通用化），不预留会强行把未来模型塞到 GPU 桶里

### Consequences

- 未来读者看到 `[model_manager.cpu] enabled = false` 会困惑"为何实现了不用"——本 ADR 解释：框架预留为未来 CPU 模型准备
- MindForge 默认不会被自动逐出（CPU 监控关），仅手动卸载可达；这是 D2 决策的合理后果

## Decision 4：守护内部卸载绕过审批层

自动逐出（压力态触发）和手动卸载（用户显式调用）的语义不同：**自动逐出绕过审批**（守护内部决策，无用户在场），**手动卸载必须走审批**（用户/agent 显式操作，纳入 `_APPROVAL_POST_PATTERNS`）。

### Considered Options

被拒绝的替代方案：

1. **自动逐出也走审批**
   - 拒绝理由：自动逐出是压力态触发的应急操作（VRAM 即将爆满），走审批意味着等待用户响应——但用户可能不在场，审批超时期间 VRAM 已耗尽导致系统崩溃
   - ocr-vram-guard 已验证"自动卸载 + 用户事后查 `/models/pressure`"是合理模式

2. **手动卸载也绕过审批**（统一语义）
   - 拒绝理由：用户/agent 显式卸载是有意识操作，应该有审批门槛（design §10 P0：fail-open 默认会让 agent 无审批卸载 OCR 瘫瘓 Computer Use 主路径）
   - design §10 v2 修订明确：`_APPROVAL_POST_PATTERNS` 增 `/models` 前缀覆盖所有 POST 端点

### Consequences

- 未来读者看到 `_maybe_evict()` 直接调 `driver.unload()` 不走审批时会困惑——本 ADR 解释：这是守护内部决策，与用户显式操作语义不同
- 自动逐出有详细日志 + `/models/pressure` 端点暴露迁移历史，用户事后可查
- 安全姿态不回退：新控制面端点（`/models/*` POST）全部纳入审批分类，自动逐出继续绕过审批是设计取舍

## Decision 5：热路径嵌入默认钉住（逐出决策让位于恢复成本）

memory_embedding 与 guide_embedding 默认 `evictable=False`（钉住），仅手动卸载可达（走审批门槛）。**依据**：记忆 embedding 在每个 MCP 请求路径上（`/mcp` tools/call → recorder.record_tool_call → semantic.index_message → embed），逐出后必然立即被下一请求拉回形成"逐出→拉回→60s 后再逐出"的慢循环，且**卸载窗口内写入的消息永久缺失向量索引**——逐出收益为负。

### Considered Options

被拒绝的替代方案：

1. **memory_embedding 也 `evictable=True`**（与 OCR 对称）
   - 拒绝理由：逐出后必然立即被下一请求拉回（每个 MCP 请求都会触发 embed），形成慢循环
   - 卸载窗口内写入的消息会永久缺失向量索引（`semantic.py:58/:92` 无错误、无 503——消费者都先查 `ready`，不会写入零向量）
   - 恢复需要 `POST /models/memory_embedding/load` + `POST /memory/rebuild_vector_index` 补缺口，恢复成本远高于 OCR 重载

2. **引入 `SemanticSearch.ensure_ready()` 自动恢复收敛点**
   - 拒绝理由：超出 D3 薄编排范围（自动恢复收敛点要在 semantic 模块加新逻辑）
   - 钉住后自动恢复不再必要（显式决策，记录在案）；手动卸载是显式操作，恢复责任在操作者

3. **memory_embedding 与 guide_embedding 合并为单一共享实例**（省 ~90MB×2 冗余）
   - 拒绝理由：合并是 §12 改进机会记录在案，本期不合并（避免扩大改动面）；合并需把 `workspace/stock_advisor/engines/wisdom_embedder.py:189` 第三份实例一并纳入视野

### Consequences

- 未来读者看到 memory_embedding `evictable=False` 而 OCR `evictable=True` 会困惑"同为本地模型为何不同"——本 ADR 解释：恢复成本不同（OCR 热重载 ~1.5-2s 无副作用 vs memory_embedding 永久缺失向量索引）
- 手动卸载后果文档化：`docs/operations-manual.md` "手动卸载记忆 embedding 的恢复步骤" 段写明恢复流程
- 默认钉住规避了热路径逐出-恢复慢循环风险，是设计 v2 第二轮审查 P1-1 的修订结果

## References

- 设计文档：[temp/sdd/model-lifecycle-manager/design.md](file:///<project_root>/temp/sdd/model-lifecycle-manager/design.md)（v2 二轮对抗性审查 1 P0 + 7 P1 + 9 P2 全部合并，附录 A 含审查发现→修订映射表）
- 实现：[server/model_manager/](file:///<project_root>/server/model_manager/)（`__init__.py` / `types.py` / `config.py` / `registry.py` / `monitor.py` / `manager.py` / `drivers.py` / `routes.py`）
- 集成：[server/core/lifecycle.py](file:///<project_root>/server/core/lifecycle.py) 启停注册 + [server/core/health.py](file:///<project_root>/server/core/health.py) Health 聚合 + [server/route_tags.py](file:///<project_root>/server/route_tags.py) 审批注册
- 兼容委托：[server/ocr.py](file:///<project_root>/server/ocr.py) + [server/mindforge.py](file:///<project_root>/server/mindforge.py)
- 文档：[docs/environment-constraints.md](file:///<project_root>/docs/environment-constraints.md) "Model Lifecycle Manager 显存释放实测" 段 + [docs/mcp-reference.md](file:///<project_root>/docs/mcp-reference.md) "Model Lifecycle Manager 统一控制面" 段 + [docs/operations-manual.md](file:///<project_root>/docs/operations-manual.md) "模型生命周期管理" 段
- 相关 ADR：ADR-0018 route_tags fail-open + 二级防护（审批暴露面策略一致性）+ ADR-0022 memory.db 单连接（memory_embedding 钉住规避的下游消费者）+ ADR-0028 agent_guide 语义向量化（guide_embedding 钉住规避的下游消费者）
