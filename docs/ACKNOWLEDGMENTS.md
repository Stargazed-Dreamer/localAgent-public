# 致谢与第三方组件声明

本文件是 LocalAgent 项目引用的第三方 skill、工具与设计思路的完整清单。项目站在 AI 编程社区肩膀上，所有第三方组件保留原作者著作权，按各自 License 使用。

> 精简版致谢见 [README_public.md](../README_public.md) 的"致谢"段。本文件是完整清单与 License 声明。

## A. 代码与方法论引用

以下 6 个仓库的 skill 已融合到项目 `.agents/skills/` 或 `workspace/dev_toolkit/` 中，按 MIT / Apache-2.0 许可证吸收。吸收标准与流程见 [ADR-0003](adr/0003-vendor-skill-absorption-criteria.md)。

### 1. mattpocock/skills

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/mattpocock/skills |
| 中文 fork | https://github.com/vinvcn/mattpocock-skills-zh-CN（同步用） |
| 作者 | Matt Pocock |
| License | MIT |
| 上游 commit | 391a270（同步到 2026-07-12） |
| 改编程度 | 改造接入（元数据 / 路由 / issue tracker 本地化，保留原版核心工作流和渐进披露结构） |

**项目内对应**（14 个 skill）：

| # | 项目内路径 | 原版桶位 |
|---|-----------|---------|
| 1 | `.agents/skills/dev/tdd/` | engineering/tdd |
| 2 | `.agents/skills/dev/codebase-design/` | engineering/codebase-design |
| 3 | `.agents/skills/dev/domain-modeling/` | engineering/domain-modeling |
| 4 | `.agents/skills/dev/resolving-merge-conflicts/` | engineering/resolving-merge-conflicts |
| 5 | `.agents/skills/dev/to-spec/` | engineering/to-spec |
| 6 | `.agents/skills/dev/to-tickets/` | engineering/to-tickets |
| 7 | `.agents/skills/dev/wayfinder/` | engineering/wayfinder |
| 8 | `.agents/skills/dev/prototype/` | engineering/prototype |
| 9 | `.agents/skills/dev/implement/` | engineering/implement |
| 10 | `.agents/skills/dev/triage/` | engineering/triage |
| 11 | `.agents/skills/dev/grill-me/` | productivity/grill-me（按用途归 dev 桶） |
| 12 | `.agents/skills/dev/grilling/` | productivity/grilling（按用途归 dev 桶） |
| 13 | `.agents/skills/dev/grill-with-docs/` | engineering/grill-with-docs |
| 14 | `.agents/skills/daily/teach/` | productivity/teach |

**改造原则**：

1. 元数据对齐（YAML frontmatter + `task_type`）
2. issue tracker → `wip_tasks` 表 + `workspace/specs/`
3. 斜杠命令 `/name` → `agent_guide(task_type=...)` 路由
4. 保留原版核心工作流和渐进披露结构

详见 [.agents/skills/dev/README.md](../.agents/skills/dev/README.md) 和 [.agents/skills/daily/README.md](../.agents/skills/daily/README.md)。

### 2. KKKKhazix/khazix-skills

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/KKKKhazix/khazix-skills |
| 作者 | 数字生命卡兹克 |
| License | Apache-2.0 |
| 改编程度 | 改编（保留核心方法论，适配无 `/goal` 模式的项目环境） |

**项目内对应**（2 个 skill）：

| skill | 项目内路径 | 改编说明 |
|-------|-----------|---------|
| **leader** | `.agents/skills/dev/leader/` | 保留七问 + Harness 心法方法论，产物写入 `temp/sdd/<slug>/spec.md` 而非原版 `/goal` 任务书。来源标注在 SKILL.md 正文 blockquote |
| **neat-freak** | `.agents/skills/neat-freak.md` | 保留"知识编辑 vs 知识记录仪"核心哲学 + 反膨胀四原则 + 三类知识分层，措辞和审查流程本地化适配项目文档体系（AGENTS.md / _index.md / CHANGELOG.md 等） |

> 注：neat-freak 早期融合时漏标 upstream，本文件补齐归属。

### 3. kangarooking/cangjie-skill

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/kangarooking/cangjie-skill |
| 作者 | kangarooking |
| License | MIT |
| 同步时间 | 2026-07-20 同步，2026-07-21 融合 |
| 改编程度 | 改编（6 阶段 RIA-TV++ 流水线保留，路径适配 `workspace/cangjie/<slug>/`） |

**项目内对应**：`.agents/skills/daily/cangjie_extraction/`

本地 upstream 档案保留在 `.agents/skills/daily/cangjie_extraction/upstream/`（含 GITHUB_REPO.md / LICENSE / README.md / SKILL.md）。

### 4. pbakaus/impeccable

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/pbakaus/impeccable |
| 作者 | pbakaus（维护者 pbakaus + abdulwahabone） |
| License | Apache-2.0 |
| 上游版本 | 3.9.1（融合于 2026-07-21） |
| 上游 commit | 84135db（同步到 2026-05-23） |
| 改编程度 | 改编（保留 reference/ 30 命令规范 + scripts/ 11 类方法论，删除 13 个 harness 副本和 cli/extension/site，体积 43.84 MB → 2.35 MB） |

**项目内对应**：`.agents/skills/dev/impeccable/`

本地 upstream 档案保留在 `.agents/skills/dev/impeccable/upstream/`（含 GITHUB_REPO.md / LICENSE / NOTICE.md / AGENTS.md / CLAUDE.md / DESIGN.md / PRODUCT.md / README.md / SKILL.md + docs/ 5 文档）。

### 5. nutlope/hallmark

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/nutlope/hallmark |
| 作者 | nutlope（Hassan El Mghari） |
| License | MIT |
| 上游版本 | 1.1.0（融合于 2026-07-21） |
| 上游 commit | aeb42fb（同步到 2026-06-05） |
| 改编程度 | 改编（保留 references/ 25+ 主题 + 5 子目录，删除 site/ 和 docs/screenshots/，体积 12.90 MB → 0.70 MB） |

**项目内对应**：`.agents/skills/dev/hallmark/`

本地 upstream 档案保留在 `.agents/skills/dev/hallmark/upstream/`（含 GITHUB_REPO.md / LICENSE / README.md / ROADMAP.md / SKILL.md + docs/ 3 文档）。

### 6. nextlevelbuilder/ui-ux-pro-max-skill

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/nextlevelbuilder/ui-ux-pro-max-skill |
| 作者 | Next Level Builder |
| License | MIT |
| Copyright | Copyright (c) 2024 Next Level Builder |
| 改编程度 | 原样引用（SKILL.md 顶部明确标注"第三方技能"，保留原版 LICENSE 文件） |

**项目内对应**：仅在 `workspace/dev_toolkit/.agents/skills/ui-ux-pro-max/`（本体 `.agents/skills/` 未收入）

**用途**：UI/UX 设计智能（67 风格 + 161 配色 + 57 字体配对 + 16 技术栈 + BM25 搜索引擎 + `--design-system` 设计系统生成器）。

## B. 设计哲学借鉴

以下来源未做代码级拷贝，仅在项目 skill 的 SKILL.md 正文明确标注借鉴了其设计哲学或方法论。

### 1. iOfficeAI/OfficeCLI

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/iOfficeAI/OfficeCLI |
| 作者 | iOfficeAI |
| License | Apache-2.0 |
| 借鉴内容 | Help-First Rule / Incremental Execution / Scene-layer inheritance / QA Delivery Gate / CFO 4-color / Three-zone architecture / 6 anti-AI-slop 设计原则 / Visual Floor |

**项目内对应**：`.agents/skills/office_docs/` 全套（docx / xlsx / pptx / pdf）

各 SKILL.md 概述段明确标注"借鉴 OfficeCli 项目的设计哲学"。本项目 office_docs 是纯 Python 实现（python-docx / openpyxl / python-pptx），不依赖 OfficeCli 的 C# 二进制。

### 2. mattpocock/skills 的 writing-great-skills

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/mattpocock/skills |
| 作者 | Matt Pocock |
| License | MIT |
| 借鉴内容 | Predictability / Information hierarchy / Description 写法等 skill 编写原则概念 |

**项目内对应**：`.agents/skills/skill-creator.md`

仅参考思路，"不分 model/user-invoked，所有 skill 都可被 agent 自主触发"的决策是本地化调整。

### 3. Anthropic 官方 skills

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/anthropics/skills |
| 作者 | Anthropic |
| License | 见仓库 LICENSE |
| 借鉴内容 | Skill 形态范本（SKILL.md + scripts + references 结构） |

**项目内对应**：项目所有 skill 的文件结构（SKILL.md + references/ + scripts/）受 Anthropic 官方 skill 形态启发。

### 4. NousResearch/hermes-agent

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/NousResearch/hermes-agent |
| 作者 | Nous Research |
| License | MIT |
| 借鉴内容 | 同类本地 AI Agent 框架，作为对比与设计参考 |

### 5. OpenAI Codex

| 字段 | 值 |
|------|------|
| 仓库 | https://github.com/openai/codex |
| 作者 | OpenAI |
| License | Apache-2.0 |
| 借鉴内容 | 命令行编码代理设计，MCP STDIO 桥接的兼容目标之一 |

### 6. Claude Code

| 字段 | 值 |
|------|------|
| 作者 | Anthropic |
| 借鉴内容 | MCP 客户端设计参考，兼容目标之一 |

## C. 社区 curation 仓库

以下 awesome 仓库是 skill 发现与索引来源，未直接引用其代码：

- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) — 1000+ 智能体技能集合
- [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) — Claude Skills 生态精选目录

## D. License 兼容性说明

项目主体遵循 [Apache License 2.0](../LICENSE)。第三方组件按各自 License（MIT / Apache-2.0）使用，两者均允许在更严格许可证的项目中作为子组件保留原 License。

- **MIT License**：允许商用、修改、再分发，需保留版权声明。本项目保留所有 MIT 仓库的 LICENSE 文件（在对应 `upstream/LICENSE`）。
- **Apache-2.0**：允许商用、修改、再分发，需保留 NOTICE 和版权声明。本项目保留所有 Apache-2.0 仓库的 LICENSE 和 NOTICE 文件。

## E. 第三方来源维护流程

### 新增第三方 skill

1. 按 [ADR-0003](adr/0003-vendor-skill-absorption-criteria.md) 4 项标准评估（方法论普适性 / 未来场景预期 / dev_toolkit 适配性 / 融合成本可控）
2. 浅克隆上游到 `temp/` 做安全审查（依赖 / 网络请求 / 文件操作 / 入口脚本）
3. 融合到 `.agents/skills/` 对应桶，保留 `upstream/` 原版档案（GITHUB_REPO.md / LICENSE / README.md / SKILL.md）
4. 在 SKILL.md frontmatter 标注：`trust: adapted` + `upstream_repo` + `upstream_path` + `upstream_fused_at` + `upstream_license` + `upstream_version`
5. 在本文件追加条目
6. 同步到 `workspace/dev_toolkit/` 副本（4 处同步，按 ADR-0003）
7. 更新 [.agents/skills/_vendor/README.md](../.agents/skills/_vendor/README.md) 来源档案

### 跟进上游更新

详见 [.agents/skills/_vendor/README.md](../.agents/skills/_vendor/README.md) "未来更新流程"段。触发条件：用户主动说"跟进 xxx 上游"。

### 漏标 upstream 的补正

如发现已融合的第三方 skill 漏标 upstream（如 neat-freak 的情况），按以下步骤补正：

1. 确认来源（对比内容 / 联系原作者 / 查 git 历史）
2. 在 SKILL.md 正文或 frontmatter 补充来源标注
3. 在本文件追加条目
4. 如可能，补充 `upstream/` 原版档案
