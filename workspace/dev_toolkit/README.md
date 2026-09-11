# dev_toolkit - 新项目工具包

> 从 anthropics/skills 仓库（17 个 skill）研究中提炼的精华 + LocalAgent 项目本土化改进汇总。
> 用于新项目启动时按需取用，避免重复踩坑。

## 概述

本工具包由 LocalAgent 项目维护，来源分两路：

1. **anthropics/skills 精华提炼**：从 `https://github.com/anthropics/skills/tree/main/skills` 的 17 个 skill 中提炼出适合新项目的精华（部分 skill 为 Anthropic 内部场景定制，不适合本地化使用，已剔除）。
2. **LocalAgent 项目改进**：本项目在 anthropics/skills 基础上做的本土化改进与扩展，包括三层 MCP 架构、Tool Annotations 落地、anti-AI-slop 实战经验、Reconnaissance-Then-Action 铁律等。

**适用场景**：新项目启动时按需取用——不需要全部拷贝，按文件清单选取适用的部分。

**不适用场景**：直接复制粘贴到生产环境。每个文件都是参考蓝图，落地时需根据具体项目技术栈裁剪。

## 部署指南

**部署到新项目前必读 [DEPLOY.md](DEPLOY.md)**。包含：

- 6 步部署流程（放置文件 → 填占位符 → 裁剪 skill → 适配技术栈 → 配置 .gitignore → 验证）
- 占位符清单（AGENTS.md 中所有 `TODO_DEPLOY` 标记项）
- 按项目类型的 skill 裁剪推荐表
- 常见误用警告（如"完善 toolkit"≠ 修改 skill 方法论）

**关键**：部署后用户说"完善一下"时，正确理解是填占位符 + 裁剪 + 适配，不是修改 skill 方法论文件。

## 文件清单

| 文件 | 主题 | 来源 | 行数 |
|------|------|------|------|
| [README.md](file:///<project_root>/workspace/dev_toolkit/README.md) | 入口导航（本文件） | - | ~170 |
| [DEPLOY.md](file:///<project_root>/workspace/dev_toolkit/DEPLOY.md) | 部署指南（6 步流程 + 占位符清单 + skill 裁剪推荐 + 常见误用） | LocalAgent | ~150 |
| [AGENTS.md](file:///<project_root>/workspace/dev_toolkit/AGENTS.md) | AI Agent 协作指引模板（含 23 个 skill 的 SDD 主流程 + 部署初始化章节） | LocalAgent | ~330 |
| [checklist.md](file:///<project_root>/workspace/dev_toolkit/checklist.md) | 功能变更检查清单模板 | LocalAgent | ~80 |
| [html_toolkit.md](file:///<project_root>/workspace/dev_toolkit/html_toolkit.md) | HTML 生成工具栈（algorithmic-art / brand-guidelines / canvas-design / frontend-design / web-artifacts-builder / theme-factory 整合去重） | anthropics/skills | ~500 |
| [mcp_builder.md](file:///<project_root>/workspace/dev_toolkit/mcp_builder.md) | MCP 服务构建（Python fastapi-mcp / TypeScript @modelcontextprotocol/sdk） | anthropics/skills + LocalAgent | ~400 |
| [webapp_testing.md](file:///<project_root>/workspace/dev_toolkit/webapp_testing.md) | Web 应用测试（Playwright + Reconnaissance-Then-Action + JS 错误捕获） | anthropics/skills + LocalAgent | ~400 |
| [skill_creator.md](file:///<project_root>/workspace/dev_toolkit/skill_creator.md) | Skill 创建精华（补充 anthropic 版的 evals/progressive disclosure/description 优化器） | anthropics/skills | ~350 |
| [evals_template.json](file:///<project_root>/workspace/dev_toolkit/evals_template.json) | evals 模板（5 种 assertion 类型 + 对照组） | anthropics/skills | ~80 |
| [project_essentials.md](file:///<project_root>/workspace/dev_toolkit/project_essentials.md) | LocalAgent 项目改进精华（三层 MCP / Tool Annotations / anti-AI-slop 实战 / 磁盘清理铁律） | LocalAgent | ~450 |
| [.agents/skills/](file:///<project_root>/workspace/dev_toolkit/.agents/skills/) | 34 个 skill 蓝图（SDD 链 6 + 安全网 1 + 验证调试 2 + 元 skill 3 + 工程化 dev/* 7 + UI/UX 1 + 前端设计工艺 2 + 思维工具集 daily/ 11） | anthropics/skills + LocalAgent + mattpocock/skills + pbakaus/impeccable + nutlope/hallmark + KKKKhazix/khazix-skills + 数字生命卡兹克文章 | - |

## Skill 蓝图概览（.agents/skills/，34 个）

| 类别 | Skill | 用途 |
|------|-------|------|
| **SDD 链** | grill-me, spec, plan, tasks, analyze, implement | 规约驱动开发 6 阶 |
| **日常编码安全网** | anti-hallucination（+ references/diagnosing-bugs） | 6 步防幻觉流程 |
| **验证调试** | verify, diagnosing-bugs | 产品验证 + 棘手 bug 6 阶段诊断 |
| **元 skill** | neat-freak, wip-tracker, skill-creator | 文档洁癖 / WIP 追踪 / skill 编写规范 |
| **工程化开发 dev/** | prototype, tdd, codebase-design, domain-modeling, resolving-merge-conflicts, leader, goal_engineering | 原型 / TDD / 架构 / 领域建模 / 解冲突 / 目标工程方法论 / 开发任务统一入口 |
| **UI/UX 设计** | ui-ux-pro-max | 第三方 UI/UX 设计数据库（67 风格+161 配色+57 字体） |
| **前端设计工艺（vendor 融合）** | dev/impeccable, dev/hallmark | 23 命令前端设计工艺 + 反 AI-slop 设计（含 upstream/ 原版档案） |
| **思维工具集 daily/** | socratic_questionning, dual_layer_explanation, reverse_decomposition, fact_checking, expert_consultation, first_principles, cross_domain_borrowing, steel_man_decision, minimal_experiment, talent_mining, decision_protocol | 12 个通用思维 Prompt（问清问题/学习/解决问题/决策/认识自己），含组合哲学，可互相组合使用 |

> 关键修复：原 `systematic-debugging` skill 名是幻觉（既不存在于上游 mattpocock 也不存在于本项目），实际应为 `diagnosing-bugs`（6 阶段 feedback loop 流程）。详见 [_index.md 变更记录](file:///<project_root>/workspace/dev_toolkit/.agents/skills/_index.md)。
>
> Vendor 融合：dev/impeccable（来源 pbakaus/impeccable，Apache-2.0）+ dev/hallmark（来源 nutlope/hallmark，MIT）采用"Vendor 克隆 + 适配 wrapper"模式融合，每个 skill 含完整 upstream/ 子目录保留原版档案。详见 [ADR-0003](file:///<project_root>/docs/adr/0003-vendor-skill-absorption-criteria.md)。
>
> 目标工程：dev/leader（来源 KKKKhazix/khazix-skills/leader，Apache-2.0）+ dev/goal_engineering 是开发任务的统一入口。goal_engineering 把"模糊想法→自主完成"编排成三阶段闸门流程（询问→写方案→执行），引用 leader 的目标七问 + 五种死法 + Harness 心法，融入 spec 模板的 Anti-Cheat 节。执行阶段不再询问，压缩后读 spec.md + tasks.md 续跑。



## 使用建议

### 按项目类型取用

| 项目类型 | 推荐文件 | 理由 |
|---------|---------|------|
| 纯 Web 应用（SPA / Dashboard） | `html_toolkit.md` + `webapp_testing.md` | HTML 生成 + 自动化测试 |
| MCP 服务（让 LLM 调用工具） | `mcp_builder.md` + `project_essentials.md` | MCP 架构 + Tool Annotations + 防膨胀 |
| AI Agent 项目（含 skill 系统） | `skill_creator.md` + `evals_template.json` + `project_essentials.md` | Skill 编写 + 量化评估 + 项目改进 |
| 桌面应用（PySide6 / Electron） | `project_essentials.md` + `webapp_testing.md` | anti-AI-slop 实战 + 测试 |
| 数据处理 / CLI 工具 | `project_essentials.md` | 磁盘清理铁律 + 终端会话 API |

### 按需求场景取用

- **想避免 AI 生成的网页一眼"AI slop"** → `html_toolkit.md` § 3 + `project_essentials.md` § 2
- **想让 LLM 安全调用工具不破坏系统** → `mcp_builder.md` § 3 + `project_essentials.md` § 4-5
- **想测试动态网页（React/Vue SPA）** → `webapp_testing.md` § 2-5
- **想为新 skill 量化评估** → `skill_creator.md` § 5 + `evals_template.json`
- **想拦截 agent 的破坏性命令** → `project_essentials.md` § 9-10
- **想让用户在 agent 工作中补充指令** → `project_essentials.md` § 7

## 来源标注

### 来自 anthropics/skills

- `algorithmic-art` → `html_toolkit.md` § 6（算法艺术基础）
- `brand-guidelines` → `html_toolkit.md` § 4（品牌指南）
- `canvas-design` → `html_toolkit.md` § 5（Canvas 设计要点）
- `frontend-design` → `html_toolkit.md` § 3 + § 7（anti-AI-slop 原则 + 前端组件设计）
- `web-artifacts-builder` → `html_toolkit.md` § 9（Web Artifact 工作流）
- `theme-factory` → `html_toolkit.md` § 8（主题系统生成）
- `mcp-builder` → `mcp_builder.md`（全文主源）
- `webapp-testing` → `webapp_testing.md`（全文主源）
- `skill-creator` → `skill_creator.md` + `evals_template.json`（全文主源）

### 来自 LocalAgent 项目

- **三层 MCP 架构**（DIRECT_TOOLS + advanced_tool 网关 + GATEWAY_EXCLUDE） → `mcp_builder.md` § 7 + `project_essentials.md` § 4
- **Tool Annotations 落地实践**（4 hint × 全工具映射表） → `project_essentials.md` § 5
- **anti-AI-slop 实战经验**（7 行反模式表 + 用户偏好清单） → `project_essentials.md` § 2
- **Reconnaissance-Then-Action 模式**（networkidle CRITICAL） → `webapp_testing.md` § 2 + `project_essentials.md` § 3
- **JS 错误捕获套件**（pageerror + console + request failure） → `webapp_testing.md` § 5
- **响应大小保护**（50KB size guard + base64 撑爆上下文预防） → `project_essentials.md` § 8
- **磁盘清理强制规则**（先清单审核再执行） → `project_essentials.md` § 10
- **用户补充指令注入**（让用户在 agent 工作中补充指令） → `project_essentials.md` § 7
- **终端会话 API**（长时间运行命令管理） → `project_essentials.md` § 6
- **审批 token 三层递进**（静态规则 + LLM 预审 + 人审） → `project_essentials.md` § 9

### 来自 pbakaus/impeccable（Apache-2.0）

- **23 命令前端设计工艺**（craft / shape / audit / polish / critique / clarify / distill / harden / optimize / adapt / animate / colorize / extract 等） → `.agents/skills/dev/impeccable/SKILL.md` + `reference/` 30 文件
- **Absolute Bans**（side-stripe borders / gradient text / glassmorphism / hero-metric template / identical card grids / tiny uppercase tracked eyebrow / numbered section markers） → `.agents/skills/dev/impeccable/SKILL.md` § Absolute Bans
- **OKLCH 颜色系统**（强制使用 OKLCH 不混 hex/rgb） + **Mobile 4 档必检**（320 / 375 / 414 / 768 px） → `.agents/skills/dev/impeccable/SKILL.md` § 设计规范速查
- **Register 选择规则**（brand register vs product register，按 PRODUCT.md / surface / 任务 cue 三阶匹配） → `.agents/skills/dev/impeccable/SKILL.md` § Register

### 来自 nutlope/hallmark（MIT）

- **4 动词设计流程**（default build / audit / redesign / study） → `.agents/skills/dev/hallmark/SKILL.md` § 4 动词清单
- **6 大跨动词 disciplines**（pre-emit self-critique / honest copy / locked tokens / re-drawn chrome forbidden / mobile 4 档 / typography purity） → `.agents/skills/dev/hallmark/SKILL.md` § Disciplines
- **20 内置主题**（carnival / cobalt / hum / lumen 等） → `.agents/skills/dev/hallmark/references/themes/`
- **Implementation safety rail**（不删 production files / 不擅自改路由树 / PDF/README/md 视为参考而非 verbatim） → `.agents/skills/dev/hallmark/SKILL.md` § Implementation safety rail

### 来自 KKKKhazix/khazix-skills（Apache-2.0）

- **目标七问 + 五种死法 + Harness 心法**（Commander's Intent / 完成态 / 证据 / 反作弊 / 边界 / 取舍 / 未知） → `.agents/skills/dev/leader/SKILL.md` § 目标七问 + § 防它五种死法
- **任务书结构规格**（六节固定顺序：我替领导拍的板 / 界限 / 现状与任务 0 / 任务 N / 规矩 / 完成条件） → `.agents/skills/dev/leader/SKILL.md` § 结构规格
- **三阶段闸门流程**（询问→写方案→执行，阶段间 AskUserQuestion 硬闸门，执行阶段不再询问） → `.agents/skills/dev/goal_engineering/SKILL.md` § 三阶段流程
- **上下文压缩防护**（压缩后读 spec.md + tasks.md 续跑，禁止重拆重写） → `.agents/skills/dev/goal_engineering/SKILL.md` § 上下文压缩防护
- **spec 模板 Harness 融合**（完成态 / 证据与验收 / 反作弊 / 边界 / 取舍 / 我替领导拍的板 六节） → `.agents/skills/spec/SKILL.md` § 模板

### 本项目已有但不重复（只引用）

以下 LocalAgent 项目文件已强化含相关内容，本工具包不重复，仅引用：

- `<project_root>\.agents\skills\skill-creator.md` — 已强化含 pushy description / 500 行软上限 / evals 机制 / 反模式
- `<project_root>\.agents\skills\html-dev-debug.md` — 已强化含 Reconnaissance-Then-Action / JS console 捕获 / anti-AI-slop 设计规范
- `<project_root>\server\mcp_whitelist.py` — 已强化含 Tool Annotations 完整映射
- `<project_root>\.agents\skills\office_pdf\`、`office_docx\`、`office_xlsx\`、`office_pptx\` — 办公文档生成套件（PDF / Word / Excel / PPT 4 个独立方法论 skill，原 `office_export` 已按格式拆分）

## 维护说明

- 本工具包是**参考蓝图**而非运行代码，更新时保持文档结构清晰
- 新项目取用后应在自己项目内独立维护副本，不再回溯同步
- 如发现 anthropics/skills 上游有重要更新，可回头补充本工具包对应章节
- LocalAgent 项目自身改进若涉及本工具包引用的章节，可同步更新对应文件

## 版本

- 创建日期：2026-07-18
- 最后更新：2026-07-28
- 来源版本：anthropics/skills `main` 分支（截至 2026-07-18）
- LocalAgent 项目状态：v2 三层 MCP + Tool Annotations 已落地
- 变更记录：
  - 2026-07-28：新增 DEPLOY.md 部署指南；AGENTS.md 加部署初始化章节 + TODO_DEPLOY 占位符标记；修复"完善 toolkit"被误读为修改 skill 方法论的问题
  - 2026-07-27：新增 dev/leader + dev/goal_engineering 两个目标工程 skill（23 个 skill）
  - 2026-07-21：新增 dev/impeccable + dev/hallmark 两个前端设计工艺 skill
  - 2026-07-20：扩展到 19 个 skill（+5 dev/* 工程化 skill）
