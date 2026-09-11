# AGENTS.md — AI Agent 协作指引（dev_toolkit 模板）

> 本文件是 AI Agent 的"宪法"，IDE 扫描项目后自动注入到每次会话上下文。
> 搜索 `TODO_DEPLOY` 定位所有占位符，替换为本项目实际内容后使用。

---

## ⚠️ 部署初始化（首次使用必读）

> 本文件是 **dev_toolkit 模板**，包含 `<!-- TODO_DEPLOY -->` 标记的占位符。
> 部署到新项目后，**第一件事是填写所有占位符并按项目裁剪**，不是修改 skill 方法论文件。

### "完善 toolkit"是什么意思？

当用户说"dev toolkit 刚部署""完善一下""初始化 toolkit"时，正确理解是：

1. **填写占位符**：把所有 `<!-- TODO_DEPLOY: ... -->` 标记处替换为项目实际信息（搜索 `TODO_DEPLOY` 可定位全部占位符）
2. **裁剪 skill**：按项目类型删除不需要的 skill（如纯后端项目删 `ui-ux-pro-max` / `dev/impeccable` / `dev/hallmark`）
3. **适配技术栈**：把 skill 中涉及技术栈的示例代码换成项目实际用的语言/框架
4. **配置项目结构**：把下方"项目结构"章节替换为实际目录结构

**错误理解**（agent 常犯的错）：修改 `.agents/skills/` 下 SKILL.md 的方法论内容、给 skill 加功能、重写流程图。**这些是成熟的方法论蓝图，不是待完善的内容。**

### 禁止事项

- ❌ 不要修改 `.agents/skills/` 下任何 SKILL.md 的方法论内容（流程、步骤、原则）
- ❌ 不要给 skill"加功能"或"完善流程"——它们是经过验证的方法论蓝图
- ❌ 不要重命名/移动 skill 文件夹
- ✅ 可以按项目技术栈替换 skill 中的示例代码（如把 Python 示例换成 TypeScript）
- ✅ 可以删除项目不需要的 skill（同时更新 `_index.md`）

### 部署后 Checklist

详见 [DEPLOY.md](DEPLOY.md)。核心步骤：

- [ ] 搜索 `TODO_DEPLOY`，填写 AGENTS.md 所有占位符
- [ ] 按项目类型裁剪不需要的 skill（参考 DEPLOY.md 的推荐表）
- [ ] 更新 `_index.md` 反映裁剪后的 skill 清单
- [ ] 配置 `.gitignore`（`temp/`、`.agents/memory/`、`.agents/wip/`）
- [ ] 确认 `checklist.md` 的检查项适用于本项目
- [ ] 删除本章节（部署初始化完成后，此章节不再需要）

---

## 项目定位

<!-- TODO_DEPLOY: 一句话定位——这个项目做什么，目标平台/用户是什么。替换本行为项目实际定位。 -->

## 技术栈

<!-- TODO_DEPLOY: 技术栈——替换下方占位符为本项目实际技术栈 -->
- **语言**: <!-- TODO_DEPLOY: 如 C# / TypeScript / Python -->
- **框架/引擎**: <!-- TODO_DEPLOY: 如 Unity 2022 LTS / React 18 / Django 4 -->
- **包管理**: <!-- TODO_DEPLOY: 如 UPM / npm / pip -->
- **版本控制**: Git<!-- TODO_DEPLOY: 如需 LFS 则追加 + Git LFS -->
- **测试**: <!-- TODO_DEPLOY: 如 Unity Test Framework / Jest / pytest -->
- **CI**: <!-- TODO_DEPLOY: 如 GitHub Actions / 不使用 -->

## 编码前必读（强制）

开始任何编码任务前，**必须**先读以下规则文件：

1. [`.agents/rules/coding_principles.md`](.agents/rules/coding_principles.md) — 编码行为准则
   - **Think Before Coding**：写代码前先想清楚要做什么、改哪些文件、有什么副作用
   - **7 级决策阶梯**：YAGNI → 复用 → stdlib → 平台特性 → 已装依赖 → 一行代码 → 最少代码
   - **Surgical Changes**：外科手术式修改，不顺手重构、不扩大改动范围
   - **Goal-Driven Execution**：每一步都问"这步是否服务于任务目标"
   - **不能偷懒的区域**：错误处理、边界条件、资源释放、并发安全
2. [`.agents/rules/skill_design.md`](.agents/rules/skill_design.md) — 创建/修改 skill 时必读

## SDD 主流程（防返工核心，强制）

> **核心原则**：写代码前先想清楚。直接写代码容易返工，SDD（Spec-Driven Development）用四台阶把"想清楚"固化成流程。
> **防呆机制**：agent 接到开发任务时主动引导走 SDD，而非被动等待调用。

### 四台阶

```
用户说"做个X""实现X""加个X"
        │
        ▼
┌─────────────────────────────────────────────┐
│  grill-me（需求澄清）                        │
│  沿设计树逐枝丫提问，一次一题，给推荐答案      │
│  产出：澄清摘要                              │
└──────────────────┬──────────────────────────┘
                   │ 用户确认摘要
                   ▼
┌─────────────────────────────────────────────┐
│  spec（规约）                                │
│  只谈需求不谈技术，产出 .agents/specs/{feature}/spec.md │
│  含：目标 / 用户角色 / 功能清单DoD / 非功能需求 / 明确不做 │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  plan（技术方案）                            │
│  基于 spec 跑 7 级决策阶梯，产出 plan.md      │
│  含：技术栈 / 模块划分 / 接口设计 / 数据模型 / 风险 │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  tasks（任务拆解）                           │
│  拆成 2-5 分钟一条的可执行小任务，产出 tasks.md │
│  每条：改哪些文件 + 做什么 + 怎么验证          │
└──────────────────┬──────────────────────────┘
                   │ 可选：analyze 兜底交叉比对
                   ▼
┌─────────────────────────────────────────────┐
│  implement（实现）★ 防呆门守门员              │
│  开头必查：.agents/specs/ 是否有规约？         │
│  - 无规约 + 中等以上任务 → 拦截，建议走 SDD    │
│  - 无规约 + 小修小补 → 放行                   │
│  - 用户坚持 → 放行但提示风险                  │
│  逐条实现 tasks.md，逐条验证+勾选             │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  verify（验证）                              │
│  读 spec DoD → 逐条验证 → 运行测试 → 端到端走核心流程 │
│  声明"完成"前必须本 skill 通过                │
└─────────────────────────────────────────────┘
```

### 防呆触发规则（agent 必须遵守）

1. **接到"做/实现/加/改/重构"类任务时**：先评估任务规模
   - 小修小补（改个 bug、调个参数、加一行）→ 直接 implement
   - 中等以上（新功能、跨模块改动、架构调整）→ **主动建议走 SDD**，从 grill-me 开始
2. **implement skill 是防呆门**：开头检查 `.agents/specs/` 是否有对应规约
   - 无规约 + 中等任务 → 拦截，输出建议话术
   - 用户坚持跳过 SDD → 放行，但提示"返工风险自负"
3. **声明"完成"前**：必须走 verify skill 逐条验证 DoD
4. **遇到 bug**：简单 bug 用 anti-hallucination Step 5 轻量 log 调试；复杂 bug（并发/非确定性/性能回退/3 轮未定位）转 diagnosing-bugs skill 6 阶段

### 何时可以跳过 SDD

- 改 typo、调参数、改样式、修明显 bug → 直接 implement
- 探索性 spike（就是要试错）→ 直接 implement，但事后补 spec
- 用户明确说"别走流程，直接写" → 放行，但提示风险

### 日常编码安全网：anti-hallucination

当任务**不走 SDD**（小修小补）时，用 `anti-hallucination` skill 作为防幻觉安全网：

- **6 步流程**：定位起点 → 建立上下文 → 压缩 → 对齐确认 → 可选 log 调试 → 提方案
- **4 种模式**：Bug / 需求 / 重构 / 理解（Step 4 按模式切换对齐模板）
- **核心原则**：索引先于动手，读到再用，禁止编造，方案先确认

implement 防呆门放行小任务时，可引导用 anti-hallucination 替代"裸写"。

### 上下文压缩防护（长程任务硬规则）

长程任务（goal_engineering 执行阶段、多 ticket 实现）极易触发上下文压缩。压缩后 agent 容易误判"压缩的历史是次要的"，导致重新拆任务或重写计划书——这会丢弃已审核的方案。

**硬规则**：
- 上下文压缩后，agent 醒来第一步必须读 `.agents/specs/{feature}/spec.md` 和 `tasks.md`，对照当前任务状态续跑
- 禁止因压缩而重新拆任务或重写方案，除非用户明确要求"重新规划"
- 执行阶段遇到不确定，读 spec 的 Anti-Cheat 节自行决策，不要回头问用户
- 每完成一个任务，立刻更新 tasks.md 的状态字段作为恢复锚点

## Skill 系统

Skill 定义在 `.agents/skills/` 目录，每个 skill 是一个文件夹（`SKILL.md` + 按需的 `references/scripts/assets`）。索引在 [`.agents/skills/_index.md`](.agents/skills/_index.md)。

### Skill 清单（34 个）

| 类别 | Skill | 用途 |
|------|-------|------|
| **SDD 链** | grill-me | 需求澄清入口，审问式提问 |
| | spec | 第1阶：规约（只谈需求） |
| | plan | 第2阶：技术方案（跑7级阶梯） |
| | tasks | 第3阶：任务拆解（2-5分钟一条） |
| | analyze | 兜底：交叉比对找矛盾 |
| | implement | 第4阶：实现（防呆门守门员） |
| **日常编码安全网** | anti-hallucination | 反幻觉工作流（小任务防幻觉：先读懂再动手）+ references/diagnosing-bugs 衔接 |
| **验证调试** | verify | 产品验证（DoD逐条验证） |
| | diagnosing-bugs | 棘手 bug 6 阶段诊断（构建 feedback loop → 复现 → 假设 → 插桩 → 修复 → 复盘） |
| **元 Skill** | neat-freak | 文档洁癖（会话收尾审查） |
| | wip-tracker | 任务延续（留档/恢复） |
| | skill-creator | skill 编写规范引导 |
| **工程化开发（dev/）** | dev/prototype | 原型探索（UI 多变体 / 状态机终端） |
| | dev/tdd | 测试驱动开发（红绿循环） |
| | dev/codebase-design | 代码库设计（deep modules / seams / design-it-twice） |
| | dev/domain-modeling | 领域建模（CONTEXT.md 词汇表 / ADR） |
| | dev/resolving-merge-conflicts | 解决合并冲突（逐 hunk 解析，禁 --abort） |
| | dev/leader | 目标工程方法论源（七问 + 五种死法 + Harness 心法），来源 KKKKhazix/khazix-skills (Apache-2.0) |
| | dev/goal_engineering | 开发任务统一入口（三阶段闸门：询问→写方案→执行），引用 dev/leader 心法 |
| **UI/UX 设计** | ui-ux-pro-max | 第三方技能：UI/UX 设计智能（配色/字体/风格/设计系统生成） |
| **前端设计工艺（vendor 融合）** | dev/impeccable | 前端设计工艺（23 命令 + Absolute Bans + OKLCH + Mobile 4 档必检），来源 pbakaus/impeccable (Apache-2.0) |
| | dev/hallmark | 反 AI-slop 设计（4 动词 + 6 大 disciplines + 20 主题），来源 nutlope/hallmark (MIT) |
| **思维工具集（daily/）** | daily/socratic_questioning | 苏格拉底提问：最多 6 个追问澄清"嘴上问的和心里想的不一致" |
| | daily/dual_layer_explanation | 双层解释：小白+专家双视角解释陌生概念 |
| | daily/reverse_decomposition | 反向拆解：拆解优秀作品为什么有效+可复用规律 |
| | daily/fact_checking | 事实核查：拆三层（事实/结论/价值判断）+ 联网核查 5 档可信度 |
| | daily/expert_consultation | 专家会诊：3 种互补视角重新定义问题+互相质疑 |
| | daily/first_principles | 第一性原理：拆到本质（基本事实/习惯性假设/真正目标/现实约束） |
| | daily/cross_domain_borrowing | 跨领域借解：从历史案例和至少 3 个距离较远领域找相似解法 |
| | daily/steel_man_decision | 钢人决策：双向钢人论证二选一，找真正分歧点 |
| | daily/minimal_experiment | 最小实验：低成本可逆 7 天内验证最关键假设 |
| | daily/talent_mining | 天赋挖掘：资深生涯咨询师对话找被压抑的天赋 |
| | daily/decision_protocol | 决策协议：预烘焙组合（对齐+双向钢人+执行纪律），重大人生抉择专用 |

### 思维工具集（daily/，11 个通用思维 Prompt）

来源：文章[《都 Agent 时代了，我还是想分享给你这 12 个我最常用的 Prompt》](https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ)（作者：数字生命卡兹克）。12 个 Prompt 中 2 个已整合到现有 skill（横纵分析法→`deep_research`、人生设计术→`life_design`），其余 10 个+用户自定义组合版共 11 个独立 skill。

**组合哲学**：这些 skill 是积木不是流水线，发散优先、举例非穷尽、agent 自行判断组合方式、拿不准列给用户选。详见 [daily/README.md](.agents/skills/daily/README.md) 组合哲学段。

**与 dev/grill-me 的关系**：grill-me 是决策前拷问计划（dev/），steel_man_decision 是决策中二选一（daily/），decision_protocol 是重大决策走完整协议（daily/）。三者可串联：grill-me 澄清→steel-man 决策，或直接走 decision_protocol。

### UI/UX 设计任务（优先用 ui-ux-pro-max）

当任务涉及**界面设计、页面布局、配色/字体选择、组件样式、UX 审查**时，优先使用 `ui-ux-pro-max` 技能而非凭经验硬写：

- **生成设计系统**（推荐起点）：
  ```bash
  python3 .agents/skills/ui-ux-pro-max/scripts/search.py "<产品类型> <行业> <关键词>" --design-system [-p "项目名"]
  ```
- **搜索特定维度**：`--domain style|color|typography|chart|ux|landing`
- **技术栈指南**：`--stack react|nextjs|vue|svelte|swiftui|flutter|...`（共 16 个栈）
- **前置条件**：Python 3.x（脚本仅用标准库，无第三方依赖、无网络请求）

详见 [`.agents/skills/ui-ux-pro-max/SKILL.md`](.agents/skills/ui-ux-pro-max/SKILL.md)。

### Skill 触发机制

agent 靠清单里每个 skill 的 `description`（≤250字符）匹配用户意图。所以：
- 用户说法要能命中 description 里的关键词
- 触发词章节是给 agent 读 SKILL.md 后确认用的
- 新增/修改 skill 后必须同步 `_index.md`

### 对话延续性（提问即改进意图）

用户在对话中提出实质性问题时（非纯事实查询），默认假设有改进意图。

**规则**：
- 回答完问题后，用 AskUserQuestion 工具问下一步，提供两类选项：①「仅了解，暂不行动」②「继续推进改进」（附具体计划）
- 决策点必须在情况清晰后确认，不在问答中途拍板
- 纯事实查询可不触发，但拿不准时宁可触发

## 记忆系统

记忆存储在 `.agents/memory/` 目录，每个 key 一个 JSON 文件。Agent 直接用文件读写工具操作。

### key 规范
- 每个功能模块一个 key：`{feature}_progress`（如 `auth_progress`）
- 设计决策：`design_decisions`
- 用户偏好：`preferences`
- WIP 索引：`wip_index`

### 读写方式
- 读取：`read_file` 读 `.agents/memory/{key}.json`
- 写入：先读后合并再 `write_to_file`
- 每条记忆自带 `_updated_at`、`_description` 字段

### 使用节奏
- **会话开始**：读相关 key 了解上次进度
- **任务执行中**：关键节点写入更新
- **会话结束**：neat-freak 审查记忆是否过期

## WIP 系统

未完成任务留档在 `.agents/wip/` 目录，双格式：
- `{task_id}.json` — 机器可读（id/title/status/goal/progress/next_steps）
- `{task_id}.md` — 人类可读（目标/历程/教训/结论/下一步）

索引在 `.agents/memory/wip_index.json`。

- 任务中断 → 说"留档"触发 wip-tracker
- 恢复任务 → 说"继续之前的工作"或"继续 wip_xxx"
- 查询未完成 → 说"有哪些未完成"

## 项目结构

```
<!-- TODO_DEPLOY: 项目名——替换为本项目实际名称 -->
<!-- TODO_DEPLOY: 源码目录——如 Assets/ 或 src/，替换为实际目录 -->
<!-- TODO_DEPLOY: 测试目录——如 tests/，替换为实际目录 -->
├── .agents/
│   ├── rules/                          # 编码准则、skill 设计规范
│   │   ├── coding_principles.md
│   │   └── skill_design.md
│   ├── skills/                         # 34 个 skill 文件夹
│   │   ├── _index.md                   # skill 索引（必读入口）
│   │   ├── grill-me/SKILL.md
│   │   ├── spec/SKILL.md
│   │   ├── plan/SKILL.md
│   │   ├── tasks/SKILL.md
│   │   ├── analyze/SKILL.md
│   │   ├── implement/SKILL.md
│   │   ├── anti-hallucination/         # 日常编码防幻觉安全网（folder + references/diagnosing-bugs）
│   │   ├── verify/SKILL.md
│   │   ├── diagnosing-bugs/SKILL.md    # 棘手 bug 6 阶段诊断（替代旧 systematic-debugging）
│   │   ├── neat-freak/SKILL.md
│   │   ├── wip-tracker/SKILL.md
│   │   ├── skill-creator/SKILL.md
│   │   ├── dev/                        # 工程化开发 skill 集
│   │   │   ├── prototype/              # 原型探索（UI 变体 / 状态机终端）
│   │   │   ├── tdd/                    # 测试驱动开发
│   │   │   ├── codebase-design/        # 代码库设计
│   │   │   ├── domain-modeling/        # 领域建模
│   │   │   ├── resolving-merge-conflicts/
│   │   │   ├── leader/                 # 目标工程方法论源（七问 + Harness 心法）
│   │   │   ├── goal_engineering/       # 开发任务统一编排入口（三阶段闸门流程）
│   │   │   ├── impeccable/             # 前端设计工艺（vendor 融合，含 upstream/ 原版档案）
│   │   │   └── hallmark/               # 反 AI-slop 设计（vendor 融合，含 upstream/ 原版档案）
│   │   ├── daily/                      # 思维工具集（11 个通用思维 Prompt + 组合哲学 README）
│   │   │   ├── README.md               # 组合哲学（发散优先 / 举例非穷尽 / agent 自行组合）
│   │   │   ├── socratic_questionning/  # 苏格拉底提问
│   │   │   ├── dual_layer_explanation/ # 双层解释
│   │   │   ├── reverse_decomposition/  # 反向拆解
│   │   │   ├── fact_checking/          # 事实核查
│   │   │   ├── expert_consultation/    # 专家会诊
│   │   │   ├── first_principles/       # 第一性原理
│   │   │   ├── cross_domain_borrowing/ # 跨领域借解
│   │   │   ├── steel_man_decision/     # 钢人决策（双向钢人）
│   │   │   ├── minimal_experiment/    # 最小实验
│   │   │   ├── talent_mining/          # 天赋挖掘
│   │   │   └── decision_protocol/      # 决策协议（重大人生抉择组合套餐）
│   │   └── ui-ux-pro-max/              # 第三方 UI/UX 设计技能
│   │       ├── SKILL.md
│   │       ├── data/                   # CSV 数据库（风格/配色/字体/UX准则...）
│   │       └── scripts/                # Python 搜索引擎（search.py 等）
│   ├── memory/                         # 文件记忆（JSON）
│   ├── specs/                          # SDD 产物（spec/plan/tasks/analysis）
│   └── wip/                            # 未完成任务留档
├── temp/                               # 临时文件（gitignore）
├── AGENTS.md                           # 本文件
├── checklist.md                        # 功能变更检查清单
└── README.md
```

## 关键约束

### <!-- TODO_DEPLOY: 关键约束1——如：修改 .unity 场景文件前必须关闭 Unity 编辑器，否则改动会被覆盖 -->
<!-- TODO_DEPLOY: 替换为本项目实际约束，或删除本节 -->

### <!-- TODO_DEPLOY: 关键约束2——如：提交前必须跑 npm test -->
<!-- TODO_DEPLOY: 替换为本项目实际约束，或删除本节 -->

### 临时文件规则
- 所有临时脚本写到 `temp/` 目录（已 gitignore）
- 不要在源码目录下创建临时文件

## 测试

<!-- TODO_DEPLOY: 测试——替换为本项目实际测试命令，或删除本节 -->
<!-- 如：
- 运行全部测试：`npm test`
- 运行单个文件：`npm test -- path/to/test.spec.js`
- 提交前必须通过所有测试
-->

## 功能变更检查

新增功能、修改模块、重构代码后，按 [`checklist.md`](checklist.md) 逐项检查。

## 会话节奏

| 时机 | 动作 |
|------|------|
| 会话开始 | Agent 自动读 AGENTS.md + 读相关记忆 + 检查 WIP |
| 接到开发任务 | 评估规模 → 小修补直接 implement / 中等以上走 SDD |
| 任务中断 | 说"留档"触发 wip-tracker |
| 会话结束 | 说"整理一下"触发 neat-freak 审查文档同步 |
| 新增功能 | 按 checklist.md 逐项检查 |
