---
name: cangjie_extraction
description: >
  把书/长视频转写/播客文字稿/课程/访谈/长文蒸馏成一组原子化、可被 agent 调用的方法论 skill。
  当用户说"拆书"、"蒸馏一本书"、"把 XX 书做成 skill"、"提取方法论"、"抽象出理论"、
  "把视频/播客/课程蒸馏成 skill"时使用，或 agent 在任务中识别到可复用方法论时主动调用。
  基于 RIA-TV++ 流水线（Adler 理解→并行提取→三重验证→RIA++→Zettelkasten→压力测试→交付）。
task_type: daily.cangjie_extraction
trust: adapted
upstream_repo: https://github.com/kangarooking/cangjie-skill
upstream_path: .agents/skills/daily/cangjie_extraction/upstream/
upstream_fused_at: 2026-07-21
---

# cangjie_extraction — 蒸馏长内容为可执行方法论 skill 包

## 何时调用此 skill

**用户显式触发**：
- "帮我拆《穷查理宝典》"
- "把毛选蒸馏成 skill"
- "把这个 B 站视频/播客/课程蒸馏成 skill"
- "distill this book into skills: <path>"
- "我想把这本书的方法论做成可用的 skill"
- "提取一下这套方法论"
- "把这套流程抽象成 skill"

**Agent 主动触发**（任务执行中识别到机会）：
- 用户在任务中分享了一本书的核心观点，且这些观点明显可复用为决策框架
- 任务执行过程中浮现出一套非显然的方法论，值得抽象为可复用 skill
- 会话中产生的方法论既不是代码也不是简单事实，而是结构化思维工具
- 用户多次提到同一类决策模式，暗示需要抽象出指导框架

## 与 nuwa-skill / darwin-skill 的边界

- **nuwa-skill**（外部）：蒸馏人（思维方式 / 表达 DNA）— 模仿一个人
- **cangjie_extraction**（本 skill）：蒸馏书 / 长内容（方法论 / 框架 / 原则）— 抽象出可复用工具
- **darwin-skill**（外部）：进化任意 skill — 持续优化已存在的 skill

本 skill 不做：书摘、读后感、作者人设角色扮演。后者请引导用户用其他工具。

## 输入要求

开始前**必须**从用户处确认：

1. **内容文本来源**：PDF / EPUB / TXT / 字幕文件 / 转写稿路径，或可访问的纯文本。**不要**在没有文本的情况下"凭记忆"蒸馏 — 宁可停下来问用户要。
   - 视频/播客建议先用 `web_archive` 或第三方转写工具拿到文本
2. **内容元信息**：书籍是"书名 + 作者 + 出版年"；视频/播客/课程是"标题 + 作者(UP主/主播/讲者) + 发布时间"。用于目录命名和审计。
3. **是否首次试点**：如果用户是第一次用此 skill，建议先蒸馏 1 份内容验证流程再批量。

**非书籍内容的字段映射**：`source_chapter` 等"章节"字段对视频填时间戳或分 P，对播客填集数，对课程填讲次 — 保证可追溯即可。

## 完整流水线（RIA-TV++）

**完整工作流文档位于本地 upstream/ 档案**：`.agents/skills/daily/cangjie_extraction/upstream/SKILL.md`（含 7 阶段详细规范）。本 wrapper 提供路径适配和产物路由决策，具体阶段执行细节请读对应文件。

```
阶段 0: Adler 整书理解     → BOOK_OVERVIEW.md         ← methodology/01-stage0-adler.md
阶段 1: 5 个 agent 并行提取 → 候选方法论单元池          ← extractors/*.md
阶段 1.5: 三重验证筛选       → 通过的单元 (用户轻确认)    ← methodology/03-stage1.5-triple-verify.md
阶段 2: RIA++ 构造 skill     → 每个 skill 的 SKILL.md    ← methodology/04-stage2-ria-plus.md + templates/SKILL.md.template
阶段 3: Zettelkasten 链接    → INDEX.md + GLOSSARY.md    ← methodology/05-stage3-zettelkasten.md
阶段 4: 压力测试 (darwin 兼容) → test-prompts.json + 回炉淘汰 ← methodology/06-stage4-pressure-test.md
阶段 5: 交付                 → DIGEST.md + 安装到 skills 目录 ← methodology/07-stage5-deliver.md
```

**断点续跑**：开始前先检查 `workspace/cangjie/<slug>/PIPELINE_STATE.md` 是否存在。存在则从记录的阶段续跑，不要从头重来。每完成一个阶段更新该文件。

## 产物路径适配（与原版的差异）

原版 cangjie-skill 默认输出到 `books/<book-slug>/`。本适配版按项目约定改为：

| 原版路径 | 本项目路径 | 说明 |
|---------|-----------|------|
| `books/<slug>/` | `workspace/cangjie/<slug>/` | 蒸馏工作目录（所有阶段产物） |
| `books/<slug>/PIPELINE_STATE.md` | `workspace/cangjie/<slug>/PIPELINE_STATE.md` | 流水线状态（断点续跑用） |
| `books/<slug>/BOOK_OVERVIEW.md` | `workspace/cangjie/<slug>/BOOK_OVERVIEW.md` | 阶段 0 产出 |
| `books/<slug>/candidates/` | `workspace/cangjie/<slug>/candidates/` | 阶段 1 候选池（审计） |
| `books/<slug>/rejected/` | `workspace/cangjie/<slug>/rejected/` | 阶段 1.5 淘汰单元（审计） |
| `books/<slug>/verified.md` | `workspace/cangjie/<slug>/verified.md` | 阶段 1.5 通过单元 |
| `books/<slug>/<skill-slug>/SKILL.md` | `workspace/cangjie/<slug>/<skill-slug>/SKILL.md` | 单个蒸馏出的 skill |
| `books/<slug>/INDEX.md` | `workspace/cangjie/<slug>/INDEX.md` | skill 总览 + 引用图 |
| `books/<slug>/GLOSSARY.md` | `workspace/cangjie/<slug>/GLOSSARY.md` | 共享术语词典 |
| `books/<slug>/DIGEST.md` | `workspace/cangjie/<slug>/DIGEST.md` | 面向读者的精华长文 |
| `~/.claude/skills/` 或 `.claude/skills/` | 见下方"产物路由决策" | 安装位置 |

## 产物路由决策（核心适配点）

原版阶段 5 简单询问"装到 `~/.claude/skills/` 还是项目级"。本适配版按产物通用性做四向路由，agent 在阶段 5 完成后必须按下表决策：

| 产物特征 | 路由目的地 | 操作 |
|---------|-----------|------|
| **通用方法论**（适用于多数项目 / 跨领域复用，如"反向思考清单"、"二阶决策框架"） | `.agents/skills/<scope>/<skill-slug>/SKILL.md` | scope 选 `daily/` 或 `dev/`；写入后更新 `_index.md` + `agent_guide.py` GUIDE_REGISTRY + 运行 `tools/check_skills` 验证 |
| **领域特定方法论**（仅对本项目或当前任务有用，如"异环抽卡数据清洗方法论"） | `workspace/cangjie/<slug>/<skill-slug>/SKILL.md` | 保留在工作目录，不进项目 skills；可在 wip 中引用路径 |
| **可消费性达标的洞察事实**（符合 `memory_generation` 的"为什么 + 如何应用"标准，且不与已有记忆重复） | 写入 memory（`memory_set`） | 用 `cangjie_extraction.<slug>` 作为 key 前缀；type=`reference`；必填 `consumption_contexts` 和 `trigger_keywords` |
| **未完成的蒸馏任务 / 后续要测试的 skill / 待用户决策的安装** | wip（`wip_create`） | `extra_data` 字段存 `workspace/cangjie/<slug>/` 路径；status=`active` 或 `blocked`（等用户决策） |

**多目的地并存**：一次蒸馏可能同时产出多种特征的方法论，agent 应分别路由，不要"一刀切"。例如《穷查理宝典》可能既产出通用 skill（"反向思考"）也产出领域 skill（"投资决策清单"）。

**避免重复造轮子**：写入项目 `.agents/skills/` 前，先用 `memory_list` 和 `_index.md` 查重，避免与已有 skill 重复（如 `anti_hallucination` 已有的"先理解再动手"原则）。

## 执行流程要点

### 阶段 0 — 整书理解

1. 读取用户提供的文本（大文件分块）
2. 执行 `methodology/01-stage0-adler.md` 中的 Adler 四步（结构 / 解释 / 批判 / 应用）
3. 按 `templates/BOOK_OVERVIEW.md.template` 填充，写入 `workspace/cangjie/<slug>/BOOK_OVERVIEW.md`
4. 展示给用户确认："骨架我理解对了吗？有没有你希望重点突出的方向？" 得到确认再进入阶段 1

### 阶段 1 — 5 个 sub-agent 并行提取

**并行** spawn 5 个 Task sub-agents（一次调用中发起 5 个）：

| sub-agent | 读取的 prompt | 产出 |
|---|---|---|
| 框架提取器 | `extractors/framework-extractor.md` | 决策框架 / 思维模型 |
| 原则提取器 | `extractors/principle-extractor.md` | 原则 / 清单 / 规则 |
| 案例提取器 | `extractors/case-extractor.md` | 作者在书中亲自使用过的实例 |
| 反例提取器 | `extractors/counter-example-extractor.md` | 书中警告的失败模式 |
| 术语提取器 | `extractors/glossary-extractor.md` | 关键概念词典 |

每个 sub-agent 独立读书、独立提取、独立输出到 `workspace/cangjie/<slug>/candidates/<type>.md`。

- **长文本**：超出单个 sub-agent 上下文的内容，按 `methodology/02-stage1-parallel-extract.md` 的分块策略处理
- **降级方案**：当前环境不支持并行 sub-agent 时，用同样 5 个 extractor prompt **串行**执行，产出格式不变

### 阶段 1.5 — 三重验证筛选

读取 `methodology/03-stage1.5-triple-verify.md`，对每个候选单元执行：

- **V1 跨域**：书中至少 2 个独立段落有佐证？
- **V2 预测力**：能用它回答一个书里没明说的新问题吗？
- **V3 独特性**：不是任何聪明人都会说的常识吗？

通过的写入 `verified.md`，不通过的写入 `rejected/` 并附原因。

**用户轻确认 ★**：把"通过的 N 个候选标题 + 淘汰的 M 个"列表展示给用户确认后再进入阶段 2 — 避免最耗时的阶段 2-4 大量返工。

### 阶段 2 — RIA++ 构造 skill

对每个通过的单元，按 `templates/SKILL.md.template` 填充：

- **R (Reading)**：原文引用 ≤150 字/段（英文原文 ≤100 词/段）
- **I (Interpretation)**：用自己的话重写方法论骨架（避免照搬译本）
- **A1 (Past Application)**：书中作者用过的案例
- **A2 (Future Trigger)** ★：用户在什么情境下会需要这个 → skill 的 `description` 字段
- **E (Execution)**：1-2-3 可执行步骤
- **B (Boundary)**：什么时候不适用 / 来自阶段 0 批判阶段的作者盲点

细则见 `methodology/04-stage2-ria-plus.md`。

### 阶段 3 — Zettelkasten 链接

按 `methodology/05-stage3-zettelkasten.md`：

1. 找出 skill 之间的引用关系（A 依赖 B / A 对比 B / A 组合 B）
2. 在每个 SKILL.md 末尾补"相关 skills"段，并回填 A2 的"与相邻 skill 的区分"
3. 按 `templates/INDEX.md.template` 生成 `INDEX.md`（含引用图 mermaid）
4. 把 `candidates/glossary.md` 整理成 `GLOSSARY.md`

### 阶段 4 — 压力测试（darwin 兼容）

对每个 skill 按 `methodology/06-stage4-pressure-test.md`：

1. 设计 5-10 条测试 prompt，按 `templates/test-prompts.json.template` 写入 `test-prompts.json`
2. 至少包括 3 类：**应调用** / **不应调用（诱饵）** / **边界模糊**。诱饵中至少 1 条必须是"应触发同书另一个 skill"的场景
3. 优先用独立 sub-agent 盲测，**未过的回炉重做阶段 2** — 不做"表面修补"
4. 每个 skill 的测试结果写入 `<skill-dir>/test-results.md`

### 阶段 5 — 交付（含产物路由）

按 `methodology/07-stage5-deliver.md`：

1. 生成 `workspace/cangjie/<slug>/DIGEST.md` — 面向读者的精华长文（按 `templates/DIGEST.md.template`）
2. **执行产物路由决策**（见上方"产物路由决策"表）：
   - 通用方法论 → 复制到 `.agents/skills/<scope>/<skill-slug>/`，更新 `_index.md` 和 `agent_guide.py`，运行 `tools/check_skills`
   - 领域方法论 → 保留在 `workspace/cangjie/<slug>/`
   - 洞察事实 → 写入 memory
   - 未完成 / 待决策 → wip
3. 告知用户："已完成，可一键喂给 darwin-skill 自动进化"（如用户安装了 darwin-skill）

## 质量红线（违反则阻止输出）

1. 每个 skill 必须通过**全部**三重验证
2. 每个 skill 必须有完整的 R / I / A1 / A2 / E / B 六段
3. 原文引用 ≤150 字/段（英文 ≤100 词/段）
4. 每个 skill 必须有 `test-prompts.json`，且包含诱饵测试（不应调用的场景），其中至少 1 条是同书兄弟 skill 的场景
5. `description` 字段必须明确 trigger 条件，不能只是"一个关于 X 的 skill"

## 调用惯例

- **永远先试点 1 本** — 除非用户明确说"批量"
- **阶段之间主动汇报进度** — 不要静默跑完再 dump 结果
- **不凭记忆拆书** — 没文本就停下来问
- **保留审计轨迹** — `candidates/` 和 `rejected/` 都要留
- **随时可续跑** — 每完成一个阶段就更新 `PIPELINE_STATE.md`，中断后从状态文件恢复
- **产物路由必执行** — 阶段 5 不能只装到 skills 目录就完事，必须按"产物路由决策"表分别路由到 workspace/memory/wip/项目 skills

## 与项目其他 skill 的联动

- **`memory_generation`**：阶段 5 路由到 memory 时，复用 memory_generation 的查重+写入流程
- **`wip_tracker`**：阶段 5 路由到 wip 时，用 `wip_create` 而非直接写文件
- **`skill-creator`**：装到 `.agents/skills/` 的 skill 应符合 skill-creator 的规范（YAML frontmatter + 触发词 + 工作流）
- **`neat-freak`**：会话收尾时若新增了项目级 skill，触发 neat-freak 同步 `_index.md` 和 `agent_guide.py`
- **`web_archive`**：若源是网页/文章，先用 web_archive 抓取纯文本
- **`task_closure`**：蒸馏任务结束时走 task_closure，记录经验并按产物路由更新记忆/wip

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 本地 upstream/ 档案 | `.agents/skills/daily/cangjie_extraction/upstream/` | RIA-TV++ 完整方法论、原版 README/LICENSE/GITHUB_REPO.md（融合自 cangjie-skill，2026-07-21） |
| Task sub-agent | Trae `Task` 工具（subagent_type=`general_purpose_task`） | 阶段 1 并行提取、阶段 4 盲测 |
| LLM 池 | `localagent_agent_chat` 或 `localagent_advanced_tool(llm_pool_call)` | 阶段 0-4 各阶段生成内容 |
| memory | `localagent_memory_*` | 阶段 5 路由到 memory 时使用 |
| wip | `localagent_wip_create` | 阶段 5 路由到 wip 时使用 |
| _index.md / GUIDE_REGISTRY | 写入项目级 skill 后同步 | 阶段 5 路由到 `.agents/skills/` 时使用 |
