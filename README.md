# LocalAgent

> 一个本地运行的个人 Agent 系统：自己天天在用，也作为各 agent 平台之间切换时的兜底层。

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Version](https://img.shields.io/badge/version-0.37.0-green)

## 这是什么

LocalAgent 是一个跑在 Windows 上的本地 Agent 系统，由 FastAPI 后端、PySide6 桌面客户端、Chromium 调试浏览器实例和一批 Skill 工作流组成。它通过 MCP（Streamable HTTP）把后端能力暴露给 IDE / 桌面客户端里的 LLM，让模型可以"看见屏幕、操作浏览器、跑代码、读写文档、调外部 API"，从而把"打开浏览器抓面经并打分"、"识别账单图片并记到 Obsidian"、"扫描抽卡记录并合并去重"这类琐事自动化掉。

它不是框架，也不是 SDK——更接近一个长期在跑、不停迭代的个人自动化系统。

## 定位

**主要自己用，也作为各 agent 平台之间切换时的兜底层**。

这个项目从 2026 年 6 月开始就是为自己写的，自己天天在用——记账、扫描抽卡记录、整理磁盘、扫描面经、自动签到，全在跑。感觉比较完善之后想试试看公开，但深度调研了竞品之后转变了定位：

**每个独立功能都有强得多、深得多的对应产品**。代码能力比不上 Claude Code / Codex CLI；浏览器自动化比不上专门的 RPA 工具；OCR 比不上商用 API；记忆系统比不上专门的知识库产品。

**但它们要么不开源，要么只解决单点问题**。本项目的价值不在某个功能"更强"，而在三点：

- **聚合**：60+ Skill 把日常琐事（账单识别、抽卡采集、社群总结、磁盘清理、面经筛选、签到自动化…）打包到一个系统里，跑在同一份记忆上。
- **MCP 协议快速接入**：通过标准 MCP（Streamable HTTP），可以随便接入任何支持 MCP 的 agent 产品（Trae / Claude Code / Codex / ChatGPT Desktop 等）。你喜欢的 agent 缺什么功能，我可能能顶上。
- **跨 agent 切换的上下文持久化**：Agent 状态在后端，IDE 只是壳——你今天用 Trae，明天换 Claude Code，后天用 Codex，记忆、WIP 留档、待办、偏好一个字节都不丢。

**适合谁**：愿意折腾、对 token 成本敏感、在多个 agent 平台之间切换、不想每次都从头交代背景的人。如果你只用一个 agent 平台且很满意，本项目对你的价值有限——但你仍可以拆走其中的 Skill 设计、记忆架构或 Computer Use 安全模式作为参考。

## 它和别的 Agent 项目不同的地方

- **Agent 状态在后端，IDE 只是壳**。记忆、WIP 留档、待办、偏好都存在后端，IDE 里的 LLM 是无状态的 MCP 客户端。换 IDE 不丢状态，新 IDE 接入零成本——这是"跨 agent 切换不丢上下文"的物理基础。
- **60+ Skill 系统**。每次重复做某类任务，做法稳定下来后就写成 Skill（一段 Markdown + workspace 脚本 + 后端端点 + 记忆 key），下次 `agent_guide` 自动路由。
- **三层记忆系统**。Recent 滑动窗口 + SQLite 时间索引 + 向量语义检索 + BM25 + EvidenceLedger + SearchTracer，跨会话保留偏好和经验。
- **MCP 三层兜底**。直连白名单 + `localagent_advanced_tool` 网关 + 预定义工作流模板，在 token 成本和能力覆盖之间找平衡。**对外接入零成本**：任何支持 MCP 的 agent 产品都能把本项目当工具库用，缺什么补什么。
- **Windows 原生 Computer Use**。截图 / 窗口枚举 / 键鼠操控走 Win32 + UIA 语义层，不依赖 Electron 或浏览器扩展——这是 Windows 单平台取舍换来的精度。
- **独立调试浏览器 agent**。Chromium CDP 9222 调试实例配独立用户数据目录，DOM locator 优先于 OCR / VL，自动化行为不影响用户正在用的实际浏览器。
- **"和 AI 一起写代码"实验**。AGENTS.md 是行为合同，Skill 是经验固化，记忆是经验沉淀——AI 是一个能持续继承上次进步的协作方。

## 能力一览

按 Skill 类型分桶（完整清单见 [.agents/skills/_index.md](.agents/skills/_index.md)）：

- **日常自动化（recurring）**：记账、社群帖子总结、面经打分、多个手游抽卡记录采集、GKD 签到规则采集与回归、GitHub 私有镜像监控、人生设计对话等。
- **按需工具（adhoc）**：磁盘清理与备份、文件 / 图片 / 媒体分类、API Key 测试、网页存档、文档查看、ModelScope 模型库更新、自动关机、期末复习资料整理等。
- **系统层（system）**：任务提醒、WIP 留档、任务收尾、记忆生成、文档洁癖审查、Skill 路由（`agent_guide`）。
- **工程化开发（dev）**：SDD 流程编排（grill-me → to-spec → to-tickets → implement）、防幻觉小修补、TDD、代码库探索、领域建模、原型设计、合并冲突处理——这一桶 Skill 是用来开发 LocalAgent 自己的。

**底层能力**：

- **OCR**：PaddleOCR 3.7（PP-OCRv6），bbox 三档分辨率回归误差 1–3px，可作为 Computer Use 文字定位主路径。
- **Computer Use**：截图 / 窗口枚举 / 键鼠操控 / UIA 语义层 / 紧急停止 / 任务级授权。任何副作用操作前必须截图思考一遍。
- **浏览器**：Chromium 内核 CDP 9222 调试实例，独立用户数据目录，与工作浏览器隔离；DOM locator 优先于 OCR / VL。
- **视觉 AI**：远程 VL（ModelScope Qwen3-VL）做布局/状态描述和文档解析，配额感知 + provider 级 fallback + 活动强度加权节流。
- **LLM 池**：多 provider 多 key 轮换、tier 硬匹配 + model 软偏好、per-project token 统计、真 SSE 流式 + DoomLoop 检测 + 三层独立熔断。
- **三层记忆**：Recent + SQLite + 向量语义 + BM25 + EvidenceLedger + SearchTracer。
- **MCP 网关**：直连白名单工具 + `localagent_advanced_tool` 网关 + `localagent_template_tool` 预定义工作流，逐层兜底。

## 快速开始

详细部署步骤见 [docs/deployment.md](docs/deployment.md)。这里只给最简流程。

> **诚实提示**：部署不算简单——需要 Python 3.12+、PaddleOCR（GPU 推荐，CPU 也能跑但慢）、Windows 管理员权限、若干 LLM API Key。愿意折腾的话半小时到一小时能跑起核心后端，完整跑通所有 Skill 还需要配 ModelScope / tushare / GitHub 等密钥。本项目主要为自己使用设计，部署文档可能不全。完整门槛清单见[局限性](#局限性)段。

```bash
# 1. 安装依赖
uv sync

# 2. 复制配置模板，填写 LLM API Key
cp config.example.toml config.toml
# 编辑 config.toml；按 docs/deployment.md 创建 data/llm/keys.json 和 data/secret/secrets.toml

# 3. 启动后端（管理员权限，键鼠操控需要）
start.bat
# 或：uv run python -m server.main
```

后端运行在 http://127.0.0.1:8766，MCP 端点 http://127.0.0.1:8766/mcp。

启动调试浏览器（涉及浏览器操作时）：

```bash
uv run python tools/browser/start_debug_browser.py
```

启动 GUI 客户端：

```bash
start_client.bat
```

## 接入 IDE

后端通过 MCP（Streamable HTTP）暴露能力。三种接入方式：

**A. 直连 Streamable HTTP**（Trae / CatPaw / CodeBuddy 等原生支持 MCP Streamable HTTP 的客户端）

```json
{
  "mcpServers": {
    "localagent": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

**B. STDIO 桥接**（Claude Code / Codex 等只支持 STDIO MCP 的客户端）

通过 [tools/mcp_bridge.js](tools/mcp_bridge.js)（基于 `mcp-remote`）把 STDIO 桥到 Streamable HTTP，后端零改动。前置：`npm install -g mcp-remote`。

**C. 桌面 GUI**（PySide6 客户端）

`start_client.bat` 启动自带 GUI，多分组面板（对话 / 概览 / 待办 / 工具 / 监控 / 设置等）通过 HTTP + SSE 直连后端。

## 配置概览

三个配置文件分工（详见 [docs/deployment.md](docs/deployment.md)）：

| 文件 | 用途 | 模板 |
|------|------|------|
| `config.toml` | 非敏感配置（端口 / 路由策略 / Loop 调度 / 浏览器 / 屏幕安全） | `config.example.toml` |
| `data/llm/keys.json` | LLM / VL / AIGC 密钥 + tier 系统 + provider 配置 | 见 `config.example.toml` 注释 |
| `data/secret/secrets.toml` | 非 LLM 密钥（tushare_token / github_token 等） | 见 `config.example.toml` 注释 |

`config.toml` 也可通过 GUI Settings 面板在线编辑，敏感字段自动脱敏。

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

`agent_guide(task='...')` 是所有任务的入口：返回候选 Skill + `first_action` + 自动注入相关记忆 + 工具优先级。**跳过它就等于丢掉项目积累的所有偏好和经验**。

架构深度论述（模块清单 / 数据流 / 关键设计决策完整版）见 [docs/architecture.md](docs/architecture.md)。

## 核心概念

| 术语 | 定义 |
|------|------|
| **Skill** | 一段 Markdown 工作流定义 + 对应的 workspace 脚本 / 后端端点 + 记忆 key，由 `agent_guide` 路由 |
| **workspace** | 任务工作区，每个 Skill 在 `workspace/<name>/` 下放脚本、数据、配置、SKILL.md |
| **Loop** | 后台轮询任务（活动追踪 / VL 描述 / 内存维护 / 镜像监控），独立于 Agent 主循环 |
| **SDD** | Spec-Driven Development，项目的开发方法论，产物在 `temp/sdd/<slug>/` |
| **agent_guide** | 任务路由入口，返回候选 Skill + first_action + 自动注入记忆 + 工具优先级 |
| **三层记忆** | Recent 滑动窗口 + SQLite 时间索引 + 向量语义检索 + BM25 + EvidenceLedger |
| **MCP 三层** | 直连白名单工具 → `localagent_advanced_tool` 网关 → `localagent_template_tool` 预定义工作流 |
| **WIP** | Work-In-Progress 留档，跨会话保留"上次做到哪"，由 `task_closure` 自动管理 |

## 使用方式

启动后端 + 接入 IDE 后，三种典型用法：

**A. 让 Agent 干活（最常见）**

在 IDE 里用自然语言描述任务，例如：
- "识别这张账单图片并记到 Obsidian"
- "扫描异环抽卡记录并合并去重"
- "整理下载文件夹"

Agent 会调 `agent_guide(task='你的描述')` 路由到对应 Skill，按 `first_action` 执行，过程中自动注入相关记忆。

**B. 通过 GUI 面板查状态/改配置/审核**

启动 `start_client.bat`，常用面板：
- **概览 / 状态监控**：后端模块状态总览 + 在线配置编辑（敏感字段自动脱敏）
- **工具**：工具脚本启动器
- **密钥**：LLM 密钥管理
- **对话**：v6-lite 对话引擎（真 SSE 流式 + 打字机 + 工具调用块 + steer 引导）

（面板由 `PanelRegistry` 自动发现，按 main / monitor / advanced 分组，完整清单以 `client/panels/` 为准。）

**C. 通过 headless_session 跑定时任务**

后端可自主启动 agent 会话，IDE 关了也能跑。配置见 `config.toml [loops.headless_session]`。

## 如何添加新功能 / 维护项目

**新功能流程**（详见 [AGENTS.md](AGENTS.md) 和 [.agents/rules/project_rules.md](.agents/rules/project_rules.md)）：

1. **先查现有 skill**：调 `agent_guide(task='用户任务描述')` 看是否已有 Skill 覆盖。读 [.agents/skills/_index.md](.agents/skills/_index.md) 查看完整清单。
2. **查 MCP 工具优先级**：按 4 层结构（直连白名单 → `localagent_advanced_tool` 网关 → `localagent_template_tool` → `exec_python`），只有 MCP 无法满足才写自定义脚本。
3. **中等以上复杂度走 SDD**：`grill-me` → `to-spec` → `to-tickets` → `implement`。SDD 产物放 `temp/sdd/<slug>/`（不进 git）。

**Skill 编写规范**：见 [.agents/skills/skill-creator.md](.agents/skills/skill-creator.md)。

**关键检查清单**（每次代码变更后必查，完整清单见 [project_rules.md](.agents/rules/project_rules.md)）：

- 新增路由是否在 `server/main.py` 注册？
- 新增密钥是否存到 `secrets.toml` 或 `keys.json`（而非 config.toml 或代码中）？
- `/health` 是否反映新模块状态？
- `CHANGELOG.md` 是否在 `[Unreleased]` 段添加条目？
- 架构层变更是否触发 ADR 评估（见 [docs/adr/README.md](docs/adr/README.md)）？
- 测试是否覆盖（危险操作必须 mock，详见 `project_rules.md` 测试铁律）？

**测试**：

```bash
uv run python -m pytest tests/ -v --tb=short
```

## 项目结构

精简目录树（完整映射由程序维护，见 `data/project_structure.json`）：

```
server/                # FastAPI 后端（200+ REST 路由 + MCP 网关）
  core/                # 健康检查 / 配置 / 生命周期 / MCP 网关
  ocr.py               # PaddleOCR 路由
  browser/             # 浏览器 CDP 控制（含 session 持久化）
  screen/              # 截图 / 窗口 / 键鼠 / UIA / SessionManager
  vl/                  # 远程 VL（Qwen3-VL）
  llm_pool/            # LLM 并发池（多 key / tier / 熔断 / 评分路由）
  memory/              # 三层记忆系统（SQLite + 向量 + BM25 + EvidenceLedger）
  activity_tracker/    # 活动追踪 Loop
  todos/               # 待办系统
client/                # PySide6 GUI（多分组面板 + 对话面板）
  core/agent/          # v6-lite 对话引擎核心库（SessionRunner / EventStore / Compactor 等）
lib/                   # 共享库（recorder 录制器 / ui 设计系统 / secret 密钥中转 / uia）
.agents/skills/        # 60+ Skill 定义（按 scope 分桶：dev/daily/_vendor/_deprecated）
docs/                  # 二级文档（架构 / API / MCP / 记忆 / LLM 池 / Computer Use / ADR 等）
tools/                 # 通用工具脚本（browser / debug / disk / release 等）
workspace/             # 任务工作区（按任务分目录，含专属脚本和数据）
tests/                 # 测试（文件数与用例数随迭代增长，跑法见下）
data/                  # 运行时数据（gitignore，仅 project_structure.json 进仓库）
temp/                  # 临时脚本 + SDD 产物 + HTML 展示（gitignore）
release/               # 源码分发引擎（profile 驱动 + 4 静态 gate + 4 构建期 gate）
```

完整目录说明见 [AGENTS.md](AGENTS.md) 的"项目结构"段。

## 关键设计决策

3 个最值得讲的设计决策（完整论述见 [docs/architecture.md](docs/architecture.md)）：

### `agent_guide` + 记忆消费闭环：不是"路由"，是"上下文重建"

每个任务第一步必须调 `agent_guide(task='用户原始描述')`。它返回的不只是"用哪个 skill"，而是候选 Skill + `first_action` + 自动注入的相关记忆（每条记忆写入时都带了 `consumption_contexts` 和 `trigger_keywords`）+ 工具优先级 + 环境提示。这让一个新会话的 agent 立刻拥有"上次做到哪 + 用户偏好 + 这类任务曾经踩过什么坑"。

### SDD 流程作为上下文压缩防护

长任务极易触发上下文压缩。压缩后 LLM 容易误判"压缩的历史是次要的"，重新拆任务或重写方案，丢掉已审核的 spec。项目的硬规则是：真源是 `temp/sdd/<slug>/spec.md` + `tickets.md`，压缩后 agent 醒来第一步必须读它们续跑，禁止因压缩而重写。这是"为 LLM 的局限性做工程"。

### 三层 MCP 兜底：在 token 成本和能力覆盖之间找平衡

后端有 200+ 个 REST 路由，但全塞进 LLM 工具目录会炸上下文。所以分三层：直连白名单（高频 GET 工具，免审批，清单见 `server/mcp_whitelist.py`）→ `localagent_advanced_tool` 网关（其余工具统一入口，LLM 看到的工具目录只有一个）→ `localagent_template_tool` 预定义工作流。路由通过 `x-agent-callable` 标记决定是否进入 Agent 工具目录，默认 fail-open（新端点自动暴露），安全靠 `safety_map` + 审批级别的第二道防线兜底（见 ADR-0018）。

## 关于这个项目

**Agent 状态在后端，IDE 只是壳**。这是项目里最值得讲的架构决策之一。Agent 的"状态"——记忆、WIP 留档、待办、偏好、项目结构基线——全部存在后端，不存在 IDE 里。IDE 里那只 LLM 是无状态的：它只是 MCP 客户端，发请求、收工具结果、再发请求。换一个 IDE，状态一个字节都不丢。代价是后端必须自己解决"Agent 应该做什么"这个本来由 IDE / 用户上下文回答的问题，于是就有了 `agent_guide` + 三层记忆 + WIP 留档 + 任务收尾机制这一整套元数据基础设施。收益是项目不再绑定任何一家 IDE 厂商。

**"和 AI 一起写代码"实验**。这个项目是一个长期跑着的"人 + AI 配对编程"实验。契约是 [AGENTS.md](AGENTS.md)——它定义了每次新会话的启动检查清单、交互规范、任务结束的 6 步收尾流程、关键约束。Skill 是"做过 2-3 次就值得固化"的工作流。记忆是"经验沉淀"——每次任务收尾时 `task_closure` 评估有没有值得保留的经验，写入三层记忆系统并标注消费场景。两个月、30+ 个 release、60+ Skill、3000+ 测试用例，大部分代码是和 AI 一起写的——但更像是 AI 是一个能持续继承上次进步的协作方，训练的成果编码在 AGENTS.md / Skill / 记忆里。

**时间线与 Git 历史**。项目从 2026 年 6 月开始开发。仓库的 Git 历史是重新起步的独立历史，不包含早期开发过程中的 commit——开发过程夹杂了大量个人数据、API key 试错记录、私有路径和半成品想法，直接公开会泄露隐私。完整的演进轨迹保留在 [CHANGELOG.md](CHANGELOG.md) 与 [docs/changelog-archive.md](docs/changelog-archive.md) 中，按 [Keep a Changelog](https://keepachangelog.com/) 格式逐版本记录，30+ 个 release 段、上百条变更。想看"这东西怎么长成现在这样的"，读 CHANGELOG 比读 commit 历史更清楚。

## 局限性

- **Windows-only**。键鼠操控走 Win32 API + UIA，OCR 走 PaddlePaddle-GPU CUDA 12.6，浏览器调试实例路径假设 Windows 文件系统。换平台不是改几行 path 能解决的，需要重写一整层。
- **浏览器是独立的调试浏览器，不是你正在用的实际浏览器**。Chromium 调试实例配独立用户数据目录，登录态、扩展、书签都和你的工作浏览器完全隔离——这是为了不污染你日常用的浏览器。如果你期待 agent 直接操作你正在用的浏览器（带你的登录态和扩展），本项目会让你失望。这个取舍是有意的：稳定可控 > 贴近真实用户环境。
- **非生产级**。按"一个人用、跑在自己机器上"的尺度设计：没有多租户、没有鉴权、没有水平扩展、没有 SLA、没有可观测性栈。`/health` 端点主要是给 Agent 自己看的，不是给 Prometheus 抓的。
- **部署门槛不低**。需要 Python 3.12+、PaddleOCR（GPU 推荐）、Windows 管理员权限、若干 LLM API Key，完整跑通所有 Skill 还要配 ModelScope / tushare / GitHub 等密钥。本项目主要为自己使用设计，部署文档可能不全——愿意折腾能跑起来，不愿意的话也许暂时不适合你。
- **代码风格个人化**。没有刻意遵循社区规范，文档密度远高于一般项目（因为大部分代码是和 AI 一起写的，需要给 AI 留够上下文）。请把它当作一份带详细批注的个人作品而不是参考实现。
- **自迭代中**。当前很多功能在建，部署指导因此暂缓。如果你看到某处明显没写完，那大概率是真的没写完。

## 安全风险登记

项目按"个人单机使用"尺度设计。已知风险登记在 [SECURITY-RISKS.md](SECURITY-RISKS.md)（条目随代码审查持续追加，以该文件为准）：

- **高危（个人用暂不修）**：后端无认证、凭据明文存储、路径白名单缺失。
- **查证后确认安全**：MCP 审批、memory 路径校验、token 重放防御（已补回归测试）。
- **已修复/迁移消除**：SSRF 防护、artifact 文件权限、密钥迁移到 lib/secret 统一管理。

## 文档导航

按读者画像分组的入口路标：

**给普通用户（部署 + 使用）**：
- [快速开始](#快速开始) + [docs/deployment.md](docs/deployment.md) — 部署
- [接入 IDE](#接入-ide) — 三种接入方式
- [配置概览](#配置概览) — 三个配置文件分工
- [使用方式](#使用方式) — 典型任务示例

**给项目维护者（开发 + 扩展）**：
- [如何添加新功能 / 维护项目](#如何添加新功能--维护项目)
- [AGENTS.md](AGENTS.md) — 项目指引、会话规范、核心陷阱、关键约束
- [.agents/rules/project_rules.md](.agents/rules/project_rules.md) — 功能变更检查清单
- [docs/dev-workflow.md](docs/dev-workflow.md) — 工程流程、CHANGELOG 维护、发版流程
- [.agents/skills/skill-creator.md](.agents/skills/skill-creator.md) — Skill 编写规范

**给架构读者（深度决策）**：
- [docs/architecture.md](docs/architecture.md) — 架构总览 + 3 个深度设计决策论述
- [docs/adr/](docs/adr/) — ADR（架构决策记录，数量随迭代增长，索引见目录内 README）
- [CHANGELOG.md](CHANGELOG.md) + [docs/changelog-archive.md](docs/changelog-archive.md) — 完整开发轨迹

**给 AI Agent**（接入项目的 LLM）：
- [AGENTS.md](AGENTS.md) — 行为合同，每次会话必读
- [.agents/skills/_index.md](.agents/skills/_index.md) — Skill 索引
- [docs/operations-manual.md](docs/operations-manual.md) — 运维手册
- [docs/mcp-reference.md](docs/mcp-reference.md) — MCP 三层架构与工具清单

## License 与分发

[Apache License 2.0](LICENSE)。

允许商用、修改和再分发；需保留版权声明与许可证文本。第三方组件和依赖仍遵循各自许可证（MIT / Apache-2.0 等）。

源码分发通过 release engine 生成（profile 驱动 + 4 静态 gate + 4 构建期 gate），不直接发 ZIP。详见 [docs/release-policy.md](docs/release-policy.md)。

## 致谢

这个项目站在 AI 编程社区肩膀上。`dev/` 桶的工程化开发 skill 集、`office_docs` 的设计哲学、`neat-freak` 的文档洁癖思路，都来自社区开源工作。完整清单与 License 声明见 [docs/ACKNOWLEDGMENTS.md](docs/ACKNOWLEDGMENTS.md)。

**代码与方法论引用**（已融合到项目 skill 体系）：

| 来源 | 作者 | License | 项目内对应 |
|------|------|---------|-----------|
| [mattpocock/skills](https://github.com/mattpocock/skills) | Matt Pocock | MIT | dev/ 桶 13 个工程化 skill + daily/teach |
| [KKKKhazix/khazix-skills](https://github.com/KKKKhazix/khazix-skills) | 数字生命卡兹克 | Apache-2.0 | dev/leader、neat-freak |
| [kangarooking/cangjie-skill](https://github.com/kangarooking/cangjie-skill) | kangarooking | MIT | daily/cangjie_extraction |
| [pbakaus/impeccable](https://github.com/pbakaus/impeccable) | pbakaus | Apache-2.0 | dev/impeccable |
| [nutlope/hallmark](https://github.com/nutlope/hallmark) | nutlope | MIT | dev/hallmark |
| [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) | Next Level Builder | MIT | dev_toolkit/ui-ux-pro-max |

**设计哲学借鉴**（无代码拷贝，仅参考思路）：[OfficeCli](https://github.com/iOfficeAI/OfficeCLI)（office_docs 设计原则）、[Anthropic 官方 skills](https://github.com/anthropics/skills)（Skill 形态范本）、[Hermes Agent](https://github.com/NousResearch/hermes-agent)、[OpenAI Codex](https://github.com/openai/codex)、Claude Code（同类工具对比与设计参考）。

所有第三方组件保留原作者著作权，按各自 License（MIT / Apache-2.0）使用。完整吸收标准与流程见 [ADR-0003](docs/adr/0003-vendor-skill-absorption-criteria.md)。

## 贡献

主要自己用 + 展示能力，Apache-2.0 license 下欢迎 PR 和 Issue。但这是一个人的项目，不保证回复速度。

接受的 Issue / PR 类型：
- Bug 报告
- 改进建议
- 指出某段代码写得很蠢
- 想商用某段代码（Apache-2.0 允许商用，可直接用）
