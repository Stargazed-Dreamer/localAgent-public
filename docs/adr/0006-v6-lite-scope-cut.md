# v6-lite 砍范围开工：v6 全量 13 文档降级为参考库

v6 对话引擎设计已迭代 5 代（v3→v3.2→v4→v5→v6），13 份文档 ~150 完成标志、估期 16-23 周，但 WIP 挂 19 天进度 5%、`client/core/agent/` 目录都不存在——实现为 0。本决策把 v6 全量降级为"参考库"，以 `planning_notes/v6/v6-lite.md` 为唯一开工依据，只买"必须自研"的最小对话引擎，其余全部走"有真实痛点再回收"的延后清单。

## Status

accepted (2026-07-31)

## Context

v6 全量设计的规模与现状严重脱节：

| 事实 | 证据 |
|---|---|
| v6 全量 = 13 文档 / ~150 完成标志 / 估期 16-23 周 | v6-12 §1-§7 |
| WIP 挂 19 天，进度 5%，`client/core/agent/` 不存在 | wip_ef09ec30 |
| 设计文档已迭代 5 代（v3→v3.2→v4→v5→v6），实现为 0 | `planning_notes/` |

与此同时，已核实的复用资产相当丰富：三层审批 + approval_level 四级（command_guard + http_guard + route_tags）、工具 catalog 元数据（`/openapi.json` 的 `x-agent-callable`/`x-tool-safety`）、token 估算 + 消息压缩（`server/llm_pool/compression.py`）、LLM 池、HTTP 客户端、面板自动发现、审批弹窗——这些都不需要重建。

判断结论（2026-07-31）：**对话循环是 commodity；生态闭环（guide/skill/memory/审批/GUI）才是壁垒**。lite 只买"必须自研"的部分——一个能驱动现有 100+ 工具、走现有审批、进现有 GUI 的最小对话引擎。

## Decision

1. **v6 全量 13 文档降级为"参考库"**，保留不删；`planning_notes/v6/v6-lite.md` 是唯一开工依据。两文件冲突时以 lite 为准。需要实现细节时回查 v6-XX 对应模块（如 RetryDecision 查 v6-03），不在 lite 复制设计。
2. **EventStore 砍到 4 表**（sessions/events/messages/tool_calls），v6-01 的 10 表 + 双枚举 + prompt_index 只保留 `events` 预留 `prompt_index`/`invalidated_seq` 列（只留字段不写逻辑，为 rewind 留门）。
3. **Permission 全部桥接现有审批**——不新建 PermissionPolicy（v6-05 的 8 source/12 reason/SSRF/沙箱全延后），`x-tool-safety=approval_required` 的工具调用走 server 现有审批链（http_guard + command_guard 三层 + approval_level），引擎轮询/等待审批结果。
4. **Skill/Memory 不动现有体系**（v6-07 的三层加载 + closed taxonomy 全延后）——`agent_guide` 已做路由 + 注入，记忆 v3 已覆盖检索。
5. **Verification/Hook/Rewind 全延后**（v6-08/v6-09/v6-11）。自用阶段人工验证；当前 0 个 hook 用户；events 预留字段不写逻辑。
6. **保留 v6 精华**（别人用真金白银的 bug 换来的，实现成本小必须留）：完整 tool call 解析后再执行、yieldMissingToolResultBlocks（abort/重启补 error result 防 provider 400）、死循环守卫标志位 retry 不重置、maxBudgetUsd 循环内检查、error-as-output-variant、synthetic 消息 visible=false、deps/config 拆分、启动 reconciliation、route_tags fail-closed、工具副作用前先落 durable record。
7. **6 周垂直切片**（W1-W6，每周可验收）替代 v6-12 的 P0-P6：EventStore+SessionRunner 骨架 → pool tools 透传 + 只读工具端到端 → 审批桥接 + 中断 + 启动恢复 → ChatPanel + 防死循环 → 流式 + L0/L2 压缩 → 故障注入 + dogfood + 文档。

## Considered Options

1. **v6 全量 13 文档 ~150 完成标志**——被拒：估期 16-23 周，WIP 已挂 19 天进度 5%，照此速度半年无法开工；设计迭代 5 代实现为 0，继续全量等于继续不动。
2. **v7 重写**——被拒：v6 文档已迭代 5 代，再起一代只会再产出一批文档而非代码；且现有审批/工具/GUI 资产会被无谓重造。
3. **扩充 v6-lite 范围**（把"这个也要加"的冲动直接并入 lite）——被拒：lite 铁律 3 明确"任何'这个也要加'的冲动，先记入 §6 延后清单并写触发条件，不改本文件"；扩充会重回 v6 全量的泥潭。

## Consequences

**正面**：
- 6 周切片每周可验收，W1 即有 MockLLM 跑通循环，W6 dogfood。
- 最大化复用现有资产（审批/工具 catalog/压缩/LLM 池/GUI），零重造。
- 延后清单（§6）每项配回收触发条件，"有真实痛点再回收"，避免提前设计。

**负面**：
- 功能裁剪激进，Rewind/Hook/Verification/SSRF/沙箱全部延后，自用阶段靠人工兜底。
- 延后清单长（13 项），需要纪律性地按触发条件回收，否则会永久搁置。
- v6 全量文档与 lite 并存，新读者可能误读全量文档为开工依据。

**回退路径**：v6 全量 13 份文档保留为参考库不删；W6 dogfood 结束后复盘，按 §6 触发条件决定从 v6 回收哪些模块，形成 v6.1（增量，不是重写）。两文件冲突时以 lite 为准。

## References

- 唯一开工依据：[`planning_notes/v6/v6-lite.md`](file:///f:/<project_root>/planning_notes/v6/v6-lite.md)
- 实现产物（v6-lite 引擎核心库）：[`client/core/agent/`](file:///f:/<project_root>/client/core/agent/)（[runner.py](file:///f:/<project_root>/client/core/agent/runner.py) / [event_store.py](file:///f:/<project_root>/client/core/agent/event_store.py) / [compactor.py](file:///f:/<project_root>/client/core/agent/compactor.py) / [tool_registry.py](file:///f:/<project_root>/client/core/agent/tool_registry.py) / [llm_pool_gateway.py](file:///f:/<project_root>/client/core/agent/llm_pool_gateway.py) / [reconciler.py](file:///f:/<project_root>/client/core/agent/reconciler.py) / [facade.py](file:///f:/<project_root>/client/core/agent/facade.py) / [doom_loop.py](file:///f:/<project_root>/client/core/agent/doom_loop.py)）
- 相关 ADR：[ADR-0009](0009-toolresult-five-variant-enum.md)（ToolResult 5 变体）、[ADR-0010](0010-tool-list-four-categories.md)（工具清单分四类）——均为 v6-lite 后续 v6.1 改造
- 切片落地记录：`docs/changelog-archive.md` [0.31.0] v6-lite 对话引擎核心库 / [0.32.0] v6-lite-streaming-gui
