# 架构与设计决策

本文档是 LocalAgent 的架构总览和关键设计决策深度论述。适合希望理解项目设计哲学、模块组织、数据流和"为什么这样设计"的读者。

> 如果只想快速部署使用，看 [README_public.md](../README_public.md) 和 [deployment.md](deployment.md) 就够了。本文档面向架构读者。

## 概述

LocalAgent 是一个跑在 Windows 上的本地 Agent 系统，由四个核心组件构成：

1. **FastAPI 后端**（端口 8766）—— Agent 的"大脑"和"状态仓库"。提供 200+ REST 路由 + MCP 网关，承载所有业务逻辑、记忆系统、LLM 池、Skill 路由。
2. **PySide6 GUI 客户端**—— 桌面可视化层。多分组面板（对话/概览/待办/工具/监控/设置等，由 `PanelRegistry` 扫 `client/panels/` 自动发现，另加载 workspace manifest 声明的组件面板）通过 HTTP + SSE 直连后端，附带 v6-lite 对话引擎。
3. **Chromium 调试浏览器实例**（CDP 9222）—— 浏览器自动化执行器。独立用户数据目录，与工作浏览器隔离。
4. **`.agents/skills/` + `workspace/`** —— 60+ Skill 定义和任务工作区，是 Agent 的"经验库"和"工作台"。

**核心契约**：所有组件通过 MCP（Streamable HTTP）或 REST 通信，没有进程间共享状态。Agent 的"状态"——记忆、WIP、待办、偏好——全部存在后端 SQLite。

## 架构总览

```
┌──────────────┐   MCP (Streamable HTTP)   ┌──────────────────────────┐
│  IDE / 桌面  │ ───────────────────────── │      FastAPI 后端        │
│  客户端里的  │                           │  (port 8766, /mcp 端点)   │
│    LLM       │ ◀─────────────────────────│                          │
└──────────────┘     工具调用结果回灌       │  ┌──────────────────┐    │
                                              │  │  agent_guide     │ ← 任务路由入口
┌──────────────┐   HTTP / SSE                │  │  memory (×3 层)  │    │
│  PySide6 GUI │ ────────────────────────── │  │  llm_pool        │    │
│  (多分组面板)│                            │  │  ocr / vl / exec │    │
└──────────────┘                            │  │  browser (CDP)   │    │
                                              │  │  screen / uia    │    │
┌──────────────┐                            │  │  todos / wip     │    │
│  Chromium    │ ◀── CDP 9222 ──────────────│  │  loop / inbox    │    │
│  调试浏览器  │                            │  │  ...             │    │
└──────────────┘                            │  └──────────────────┘    │
                                              └──────────────────────────┘
                                                                       │
                                                                       ▼
                                                              .agents/skills/ (60+ Skill)
                                                              workspace/      (任务工作区)
```

### 后端模块清单

| 模块 | 路径 | 职责 |
|------|------|------|
| **agent_guide** | [server/agent_guide.py](../server/agent_guide.py) | 任务路由入口，返回候选 Skill + first_action + 自动注入记忆 + 工具优先级 |
| **memory** | [server/memory/](../server/memory/) | 三层记忆系统：Recent + SQLite + 向量语义 + BM25 + EvidenceLedger + SearchTracer |
| **llm_pool** | [server/llm_pool/](../server/llm_pool/) | LLM 并发池：多 key / tier / 评分路由 / 三层独立熔断 / 真 SSE 流式 |
| **ocr** | [server/ocr.py](../server/ocr.py) | PaddleOCR 3.7（PP-OCRv6），bbox 三档分辨率回归误差 1-3px |
| **vl** | [server/vl/](../server/vl/) | 远程 VL（ModelScope Qwen3-VL），布局/状态描述和文档解析 |
| **exec** | [server/exec.py](../server/exec.py) | Python 代码执行（统一 terminal + 3 分钟唤醒 + 内联等待） |
| **browser** | [server/browser/](../server/browser/) | Chromium CDP 9222 控制（持久 session + DOM locator + 弹窗处理） |
| **screen** | [server/screen/](../server/screen/) | 截图 / 窗口 / 键鼠 / UIA / SessionManager（三档权限模式） |
| **todos** | [server/todos/](../server/todos/) | 待办系统（recurring / phased_recurring / triggered 三类型） |
| **activity_tracker** | [server/activity_tracker/](../server/activity_tracker/) | Loop 自动触发系统（活动追踪 + 下载监控 + 记忆维护 + 镜像监控） |
| **advanced** | [server/advanced.py](../server/advanced.py) | MCP 网关第二层：`localagent_advanced_tool` 统一入口 |
| **templates** | [server/templates.py](../server/templates.py) | MCP 网关第三层：预定义工作流模板 |
| **core/mcp_gateway** | [server/core/mcp_gateway.py](../server/core/mcp_gateway.py) | MCP 三层审批 + 工具暴露控制 + 响应大小保护 |
| **command_guard** | [server/command_guard.py](../server/command_guard.py) | 破坏性命令拦截（dcg 二进制预检查 + LLM 预审 + 人审三层递进） |
| **approval_review** | [server/approval_review.py](../server/approval_review.py) | 审批 token 管理（指纹绑定 + 一次性使用 + TTL） |

### 一次 agent 任务的完整数据流

以"识别账单图片并记到 Obsidian"为例：

1. **用户在 IDE 输入任务**："识别这张账单图片并记到 Obsidian"
2. **IDE 把消息发给 LLM**，LLM 决定调用 MCP 工具
3. **LLM 调用 `agent_guide(task='识别账单图片并记到 Obsidian')`**：
   - `agent_guide` 匹配 `recurring.accounting` skill
   - 返回 `first_action`（具体到第一条要执行的动作）
   - 自动注入相关记忆（上次记账偏好、用户账单分类习惯、踩过的坑）
   - 返回工具优先级（`mcp_tools_priority`）和环境提示
4. **LLM 按 `first_action` 调用 OCR 工具**（如 `localagent_advanced_tool(tool="ocr_file", params={...})`）：
   - MCP 网关 `_patched_execute_api_tool` 检查 `safety_map`（OCR 是 pass）
   - 路由到 `server/ocr.py` 执行 PaddleOCR
   - 返回 `{text, details[].box}`（响应大小保护：截断到 50000 字符）
5. **LLM 解析 OCR 结果，调用记账 skill 的 workspace 脚本**：
   - 通过 `exec_python` 执行 `workspace/accounting/bill_converter.py`
   - 命令通过 `command_guard`（dcg 预检查 + LLM 预审 + 人审）
   - 子进程在 10 秒内完成则直接返回完整结果，否则返回 terminal_id 让 LLM 决策
6. **LLM 调用 task_closure 收尾**：
   - 评估任务状态（完成 / 中断）
   - 经验提炼（识别值得保留的经验，填 `consumption_contexts` + `trigger_keywords`）
   - 写入三层记忆系统
   - 文档自查（路由注册 / `/health` / CHANGELOG / ADR 评估）
   - 向用户报告

整个过程在 IDE 看来只是几次 MCP 工具调用，但后端实际经历了路由 / 审批 / 执行 / 持久化 / 经验沉淀五个阶段。

## 关键设计决策

下面 4 个决策是项目里最值得讲的，每个都包含了"为什么这么写"比"怎么写的"更值得读的部分。

### 1. Agent 状态在后端，IDE 只是壳

这是项目里最值得讲的架构决策之一，也是其他三个决策的根因。

**问题**：如果 Agent 的状态（记忆、WIP、偏好、待办）存在 IDE 里，会发生什么？

- 换 IDE（Trae → Claude Code → Codex）→ 状态全丢，agent 退化成无记忆的通用 ChatGPT。
- 多个 IDE 同时连同一个后端 → 状态不一致。
- IDE 升级 / 崩溃 / 重启 → 状态可能丢失。
- 新的 IDE 出现时 → 需要写适配层迁移状态。

**决策**：Agent 的"状态"——记忆、WIP 留档、待办、偏好、项目结构基线、活动追踪记录——全部存在后端 SQLite，不存在 IDE 里。IDE 里那只 LLM 是无状态的：它只是 MCP 客户端，发请求、收工具结果、再发请求。

**收益**：

- **多 IDE 复用**：今天用 Trae 做了一半的记账任务，明天切到 Claude Code 继续，agent 读到的 WIP 和记忆完全一样。
- **新 IDE 零成本接入**：只要它支持 MCP（直连 Streamable HTTP 或通过 STDIO 桥接），就立刻享有项目全部能力——不需要写适配层，不需要重新训练模型，不需要迁移数据。
- **多 IDE 同时连同一个后端也能跑**（虽然实际很少这么用）。

**代价**：后端必须自己解决"Agent 应该做什么"这个本来由 IDE / 用户上下文回答的问题，于是就有了：

- **`agent_guide`**：任务路由入口，从用户描述推出候选 Skill + first_action。
- **三层记忆系统**：跨会话保留偏好和经验，下次任务自动注入。
- **WIP 留档**：跨会话保留"上次做到哪"。
- **`task_closure`**：任务收尾 6 步流程，自动评估 / WIP 处理 / 经验提炼 / 文档自查。
- **项目结构 baseline**：`data/project_structure.json` 程序化映射 + 漂移检测。

便宜了 IDE，累的是后端——但收益是项目不再绑定任何一家 IDE 厂商。

**与 IDE 的契约只有一层 MCP**：

- **直连 Streamable HTTP**：Trae / CatPaw / CodeBuddy 以及任何原生支持 MCP Streamable HTTP 的客户端，配 5 行 JSON 即可接入。
- **STDIO 桥接**：Claude Code / Codex / 任何只支持 STDIO MCP 的客户端，通过 `tools/mcp_bridge.js`（基于 `mcp-remote`）把 STDIO 桥到 Streamable HTTP，后端零改动。
- **桌面客户端**：项目自带的 PySide6 GUI 通过 HTTP + SSE 直连后端（面板按 main / monitor / advanced 分组，由 `PanelRegistry` 自动发现）。

**`agent_guide` 是统一的路由契约**：不管哪个 IDE 进来，调 `agent_guide(task='...')` 拿到的都是同一份候选 Skill 清单 + 同一份 `first_action` + 同一份自动注入的记忆。

### 2. `agent_guide` + 记忆消费闭环：不是"路由"，是"上下文重建"

每个任务第一步必须调 `agent_guide(task='用户原始描述')`。它返回的不只是"用哪个 skill"，而是：

- **候选 Skill 列表**（按强匹配 / 弱匹配排序）
- 该 Skill 的 `first_action`（具体到第一条要执行的动作）
- **自动注入的相关记忆**——这部分是关键：每条记忆写入时都带了 `consumption_contexts`（哪些 task_type 应该读它）和 `trigger_keywords`（任务描述出现什么词时读它），所以 `agent_guide` 会在 `first_action` 前自动注入"【必读记忆】"指令
- 工具优先级（`mcp_tools_priority`）和环境提示（双屏配置 / 管理员权限 / Computer Use 铁律）

**这不是"skill discovery"，是"上下文重建"**：让一个新会话的 agent 立刻拥有"上次做到哪 + 用户偏好 + 这类任务曾经踩过什么坑"。

**记忆消费闭环的工程实现**：

```python
# server/agent_guide_data.py 的 GUIDE_REGISTRY 结构
{
    "task_type": "recurring.accounting",
    "skill_file": ".agents/skills/accounting/SKILL.md",
    "first_action": "调 ocr_file 识别账单图片，然后执行 workspace/accounting/bill_converter.py",
    "consumption_contexts": ["recurring.accounting"],  # 哪些 task_type 应读相关记忆
    "trigger_keywords": ["账单", "记账", "bill"],       # 任务描述出现这些词时读
    "mcp_tools_priority": ["ocr_file", "exec_python", "memory_get"],
    "environment_notes": "双屏配置 / 管理员权限 / Computer Use 铁律",
}
```

写入记忆时强制填两个消费场景字段：

```json
{
  "key": "accounting_refund_handling",
  "data": {"preference": "退款单独标记，不与消费对冲"},
  "consumption_contexts": ["recurring.accounting"],
  "trigger_keywords": ["退款", "账单对冲"]
}
```

下次调 `agent_guide(task_type='recurring.accounting')` 时，后端会自动返回所有 `consumption_contexts` 匹配的记忆清单，并在 `first_action` 前注入"【必读记忆】"指令——agent 不需要记得读哪些记忆，guide 会主动推送。

**这个约束的代价**：每次写入记忆都要填两个消费场景字段——但凡写了没人读的记忆，就是噪音。这个约束逼着经验提炼时就想清楚"这条经验未来在什么场景下会被消费"。

**这个约束的收益**：

- 避免了"存了没人用"的记忆垃圾。
- agent 不需要主动决定"该读哪些记忆"——guide 替它决策。
- 经验提炼者必须站在"未来消费者"角度思考，提升了经验质量。

详见 [docs/agent-guide.md](agent-guide.md) 和 [docs/memory-system.md](memory-system.md)。

### 3. SDD 流程作为上下文压缩防护

长任务（多 ticket 实现、跨会话开发）很容易触发上下文压缩。压缩后 LLM 有一个不好的倾向：**认为压缩的历史是次要的、当前要求是主要的，于是重新拆任务或重写方案**——这会丢掉已审核的 spec 和已完成的工作。

**真源层级**（从高到低）：

1. `temp/sdd/<slug>/spec.md` + `tickets.md` —— 方案与任务真源（最高权威，工作记忆，不进 git）
2. 压缩历史中的用户明确要求 —— 用户意图权威
3. 压缩历史中的 agent 中间产物 —— 可重建，非权威

**项目的硬规则**：

- 真源是 `temp/sdd/<slug>/spec.md` + `tickets.md`，**不进 git**，是工作记忆。
- 压缩后 agent 醒来**第一步必须读 spec 和 tickets**，对照当前 ticket 状态续跑。
- **禁止**因压缩而重新拆任务（重写 `tickets.md`）或重写方案（重写 `spec.md`），除非用户明确要求"重新规划"。
- 每完成一个 ticket 立刻更新 `tickets.md` 的 Status 字段（ready → in_progress → completed），作为压缩后的恢复锚点。
- 执行阶段遇到不确定，读 spec 的 Anti-Cheat 节和 Bounds 节自行决策，**不回头问用户**。
- 拿不准的写进 `temp/sdd/<slug>/BLOCKED.md`，跳过继续做别的，最后随交付提交。

**SDD 流程四阶段**：

1. **`grill-me`**：追问用户，把模糊需求变成清晰目标（输出 design-decisions 草稿）。
2. **`to-spec`**：把目标变成 spec.md（含 Anti-Cheat 节防作弊 + Bounds 节防范围蔓延）。
3. **`to-tickets`**：把 spec 拆成 tickets.md（每个 ticket 是 ready → in_progress → completed 三态）。
4. **`implement`**：按 ticket 顺序实现，每完成一个立刻更新 Status。

**这套规则的本质**：承认 LLM 在长任务里会忘，于是把"它应该记得什么"提前固化到文件里，把"它该怎么做"提前写到 Anti-Cheat 节里，让 LLM 在压缩后能自我对齐。这是项目里最像"为 LLM 的局限性做工程"的部分。

**反作弊约束（Anti-Cheat）**：

- spec 必须含 Anti-Cheat 节，列举"agent 容易偷懒的地方"和"如何检测"。
- 红测试（必须失败的测试）先行，绿测试（必须通过的测试）跟上。
- 每个 ticket 完成后必须更新 Status，禁止"批量更新"——这是恢复锚点的实时性要求。
- 禁止扩范围：ticket 没要求的不要做，做了也不算交付。

详见 [.agents/skills/dev/goal_engineering/SKILL.md](../.agents/skills/dev/goal_engineering/SKILL.md) 和 [.agents/skills/dev/to-spec/SKILL.md](../.agents/skills/dev/to-spec/SKILL.md)。

### 4. 三层 MCP 兜底：在 token 成本和能力覆盖之间找平衡

后端有 200+ 个 REST 路由，但全塞进 LLM 工具目录会炸上下文。所以分三层：

#### 第一层：直连白名单（`DIRECT_TOOLS`）

- 高频 + GET 类 + 无副作用的工具直接暴露给 LLM
- 免审批，fast path
- 比如 `memory_status` / `exec_status` / `wip_list` / `todos_due`
- 定义在 [server/mcp_whitelist.py](../server/mcp_whitelist.py)，按 core / state / exec / perception / browser / llm 分桶；数量随迭代变动，以该文件为准

#### 第二层：`localagent_advanced_tool` 网关

- 通过一个统一入口 tool 调用，参数里指定 `tool="xxx"` + `params={...}`，背后路由到其余全部可暴露端点
- 后端做统一 audit / approval，LLM 看到的工具目录只有一个
- 代价是每次调用多一层 JSON 嵌套
- 路由通过 `x-agent-callable` 标记决定是否进入 Agent 工具目录

```python
# LLM 看到的工具签名
localagent_advanced_tool(tool="screen_ocr", params={"mode": "window", "hwnd": 123})

# 后端实际调用
POST /screen/ocr  # 带完整审批流程
```

#### 第三层：`localagent_template_tool` 预定义工作流

- 多工具编排模板，一步调用一个完整流程
- 适合"打开浏览器 → 导航 → 截图 → OCR → 关闭"这种固定序列

**路由策略**：

- 通过 `x-agent-callable` 标记决定是否进入 Agent 工具目录（判定实现在 `server/route_tags.py::_is_agent_callable`，真源是 `DIRECT_TOOLS` + `GATEWAY_EXCLUDE`）
- **默认 fail-open**（新端点自动暴露给 agent）——这是 ADR-0018 明确保持的决策：加 MCP 端点的目的就是给 agent 用，fail-closed 会让每个新端点都要额外声明
- 安全防护不靠这个标志，靠第二道防线：`safety_map`（read_only / safe / approval_required）→ `approval_level` → HTTP 中间件拦截，逐级递进。见 [ADR-0018](adr/0018-route-tags-fail-open-secondary-defense.md) 与 [SECURITY-RISKS.md](../SECURITY-RISKS.md) 对应条目

**审批三层递进**（针对 approval_required 工具）：

1. **静态规则**：`safety_map` 按 operation_id 查表，命中规则直接 pass / block。
2. **LLM 预审**：对通用工具类端点（如 `exec_python` / `exec_cmd`），先 LLM 审查可自动放行合法场景。
3. **人审**：LLM 预审未通过或属于人审清单的，弹 PySide6 确认窗等用户决定。

**审批 token 机制**：

- 用户批准后签发一次性 token（120s TTL）
- token 绑定指纹（method + path + sha256(body)[:16] 或 command + shell + cwd）
- `dict.pop()` 原子取出并删除令牌，第二次调用必返回 None
- `_approved_cache` 缓存用户批准的代码指纹，下次相同代码自动放行（治本缓解 token 丢弃问题）

**这套设计的核心权衡**：LLM 的工具目录越小，决策越准、token 越省；但能力覆盖越窄。三层兜底让"高频工具"快、"低频工具"省、"复杂流程"封装好——不是最优雅，但在 token 价格还贵的当下是合理的。

详见 [docs/mcp-reference.md](mcp-reference.md) 和 [docs/adr/0010-tool-list-four-categories.md](adr/0010-tool-list-four-categories.md)。

## ADR 索引

项目维护一组架构决策记录（ADR），按主题域分类。ADR 是项目架构决策的真源，覆盖 LLM 池 / 对话引擎 / 记忆系统 / 屏幕操控 / 审批 / 视觉 / 发布工具等主题域。

完整索引见 [docs/adr/README.md](adr/README.md)。这里列举几个值得先读的：

| ADR | 主题 | 何时读 |
|-----|------|--------|
| [0001](adr/0001-auto-scoring-routing.md) | LLM 池 6 因子加权评分路由 | 理解 LLM 池如何选 key |
| [0006](adr/0006-v6-lite-scope-cut.md) | v6-lite 对话引擎范围裁剪 | 理解为什么没做完整 v6 |
| [0010](adr/0010-tool-list-four-categories.md) | 工具列表四分类 | 理解 MCP 三层架构 |
| [0011](adr/0011-computer-use-session-manager.md) | Computer Use SessionManager | 理解三档权限模式 |
| [0013](adr/0013-release-engine-compiler-architecture.md) | Release engine 编译器架构 | 理解源码分发流程 |
| [0017](adr/0017-secret-unified-gateway.md) | 密钥统一网关 lib/secret | 理解密钥管理 |

## 进一步阅读

按主题分组的深度文档：

**对话引擎**：
- [docs/chat-engine.md](chat-engine.md) — v6-lite 对话引擎 8 核心组件（SessionRunner / EventStore / Compactor 等）+ EventStore schema v9 + chat-panel-v2 关键设计 + QThread 桥接
- [docs/agent-guide.md](agent-guide.md) — 任务路由系统（GUIDE_REGISTRY 结构 / 6 scope / consumption_contexts 消费闭环 / first_action 注入 / workspace 动态扩展）
- [docs/agent-guide-keywords.md](agent-guide-keywords.md) — agent_guide 关键词编写规范（match_task_candidates 五路加权打分）

**记忆系统**：
- [docs/memory-system.md](memory-system.md) — 三层记忆系统架构 / API / 压缩 / 自动记录

**LLM 池**：
- [docs/llm-pool.md](llm-pool.md) — LLM 并发池架构

**MCP 与审批**：
- [docs/mcp-reference.md](mcp-reference.md) — MCP 三层架构 / 工具排除清单 / REST→MCP 映射规则 / 统计
- [docs/api-reference.md](api-reference.md) — 后端 API 速查（按功能分桶）

**屏幕操控**：
- [docs/computer-use-reference.md](computer-use-reference.md) — UIA 语义层 / 桌面事务 / DPI / API 速查

**工程流程**：
- [docs/dev-workflow.md](dev-workflow.md) — 功能变更检查清单 / CHANGELOG 维护 / 发版流程 / MCP 工具原则
- [.agents/rules/project_rules.md](../.agents/rules/project_rules.md) — 功能变更检查清单（每次代码变更后必查）

**Agent 行为合同**：
- [AGENTS.md](../AGENTS.md) — 项目指引 / 会话规范 / 核心陷阱 / 关键约束（每次会话必读）
