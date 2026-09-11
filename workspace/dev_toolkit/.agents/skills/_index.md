# Skill 索引

> 本文件是所有 skill 的唯一索引。agent 每次会话都会看到本文件的清单（占 context 的 1%）。
> 新增/修改/删除 skill 后必须同步本文件。

## Skill 清单（34 个）

### SDD 链（规约驱动开发）

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **grill-me** | 当用户要开始新功能/新项目/较大改动，或说"做个X""实现X""加个X"时使用。写代码前先审问式澄清需求，沿设计树逐枝丫一次问一题。防返工。关键词：做、实现、加、改、重构、想做个。 | 需求澄清 |
| **spec** | 当用户确认需求后、要写技术方案前使用。产出 .agents/specs/{feature}/spec.md，只谈需求不谈技术，含 Harness 融合节（完成态/证据/反作弊/边界/取舍）。关键词：写规约、spec、需求文档、功能定义、DoD。 | 代码脚手架 |
| **plan** | 当 spec.md 完成后使用。基于规约跑 7 级决策阶梯定技术方案，产出 plan.md。关键词：技术方案、plan、架构设计、选型、模块划分。 | 代码脚手架 |
| **tasks** | 当 plan.md 完成后使用。把方案拆成 2-5 分钟一条的可执行小任务，产出 tasks.md。关键词：拆任务、tasks、任务清单、排期、todo。 | 代码脚手架 |
| **analyze** | 当 spec/plan/tasks 都完成后、开始 implement 前可选使用。交叉比对三份文档找矛盾和遗漏。关键词：分析、审查、交叉比对、找矛盾、analyze。 | 代码质量与审查 |
| **implement** | 当用户要写代码、实现功能、修改源码时使用。★防呆门：开头检查 .agents/specs/ 是否有规约，无规约的中等任务拦截建议走 SDD。关键词：实现、写代码、改代码、编码、开发。 | 代码脚手架 |

### 日常编码安全网

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **anti-hallucination** | 当用户要改代码/修 bug/加功能/重构/理解代码，且任务规模为小修小补（不走 SDD）时使用。6步防幻觉：定位起点→建立上下文→压缩→对齐确认→可选log调试→提方案。复杂 bug 转 references/diagnosing-bugs。关键词：改、修、bug、加功能、重构、排查、定位。 | 代码质量与审查 |

### 验证与调试

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **verify** | 当用户说"完成了""搞定""测试一下"或声明任务完成前使用。读 spec DoD 逐条验证+运行测试+端到端走核心流程。声明完成前必走。关键词：验证、测试、完成、DoD、冒烟测试。 | **产品验证** |
| **diagnosing-bugs** | 当遇到复杂 bug（并发/异步/竞态/非确定性复现/性能回退）或 anti-hallucination Step 5 加 log 3 轮未定位根因时使用。6 阶段：构建 tight feedback loop → 复现+最小化 → 假设 → 插桩 → 修复+回归测试 → 清理+复盘。关键词：复杂 bug、并发、竞态、性能回退、non-deterministic、feedback loop。 | Runbook 排障 |

### 工程化开发（dev/）

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **dev/prototype** | 在承诺方案前构建一次性原型细化设计。UI 问题走 `?variant=` URL 切换的多变体；Logic 问题走终端 TUI 按键驱动状态机。完成后 winner 折进真实 code，原型移到 temp/。关键词：原型、prototype、试一下、看看效果、设计变体。 | 代码脚手架 |
| **dev/tdd** | 测试驱动开发：先写失败测试→写最小实现→重构。RED-GREEN-REFACTOR 循环。每轮只处理一个测试用例。关键词：TDD、测试驱动、红绿循环、red-green-refactor、先写测试。 | 代码质量与审查 |
| **dev/codebase-design** | 代码库架构设计原则：deep modules、seams、deletion test、design-it-twice。识别 shallow modules 并深化。关键词：代码库设计、架构设计、deep module、shallow module、seam、design it twice、深化机会。 | 代码质量与审查 |
| **dev/domain-modeling** | 构建领域模型、维护 CONTEXT.md 词汇表、记录 ADR（架构决策记录）。关键词：领域模型、通用语言、ubiquitous language、术语表、CONTEXT.md、ADR、领域术语。 | 代码脚手架 |
| **dev/resolving-merge-conflicts** | 解决 git 合并冲突的规范流程：逐 hunk 解析，理解双方意图，绝不 `git merge --abort`。关键词：合并冲突、merge conflict、resolve conflict、解决冲突、rebase 冲突、git 冲突。 | 业务流程自动化 |
| **dev/leader** | 目标工程方法论源：Commander's Intent + 目标七问 + Harness 心法（什么不能做比做什么更重要）。被 goal_engineering 引用，也可独立触发做目标定义辅导。来源 KKKKhazix/khazix-skills (Apache-2.0)。关键词：leader、目标工程、帮我定目标、写目标任务书、定义目标、让 agent 自己跑。 | 需求澄清 |
| **dev/goal_engineering** | 开发任务统一入口：把模糊想法编排成"询问→写方案→执行"三阶段闸门流程。路由 grill-me/spec/tasks/implement，引用 dev/leader 心法，执行阶段不再询问。关键词：goal engineering、目标工程、做个功能、重构、开发、帮我定目标、开发新功能。 | 需求澄清 |

### 元 Skill

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **neat-freak** | 当用户说"整理一下""同步文档""收尾""这个阶段做完了""/sync""/neat"或会话结束时使用。对项目文档做洁癖级审查与同步。关键词：整理、同步、收尾、洁癖、文档更新、梳理。 | 代码质量与审查 |
| **wip-tracker** | 当用户说"留档""存档""断头工作""继续之前的工作""有哪些未完成""WIP"或任务中断需要交接时使用。未完成任务追踪：留档、查询、恢复、去重。关键词：留档、存档、继续、未完成、中断、接手。 | 业务流程自动化 |
| **skill-creator** | 当用户说"创建一个 skill""新 skill""修改 skill""skill 怎么写"或需要新建/修改 skill 文件夹时使用。skill 编写规范引导。关键词：skill、创建、编写、规范。 | 代码脚手架 |

### UI/UX 设计（第三方）

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **ui-ux-pro-max** | 当用户要做 UI/UX 设计、建页面、选配色/字体、做组件、审查界面体验时使用。内置 67 风格+161 配色+57 字体配对的本地 searchable 数据库，--design-system 一键生成完整设计系统。关键词：UI、UX、设计、配色、字体、页面、landing、组件、dashboard。 | 库和 API 参考 |

### 前端设计工艺（vendor 融合）

| name | description（≤250字符） | 类别 |
|------|-------------------------|------|
| **dev/impeccable** | 当用户要 design/redesign/shape/critique/audit/polish/clarify/distill/harden/optimize/adapt/animate/colorize/extract 或改进前端界面时使用。覆盖网站/落地页/仪表盘/产品 UI/组件/表单/设置/onboarding/空状态。23 命令 + Absolute Bans（side-stripe/gradient text/glassmorphism/hero-metric/identical card grids/tiny uppercase eyebrow）+ OKLCH 强制 + Mobile 4 档必检（320/375/414/768px）。关键词：design、redesign、shape、critique、audit、polish、前端设计。 | 代码脚手架 |
| **dev/hallmark** | 当用户要 build/audit/redesign/study 网页时使用，目标是让 UI"看起来是手工做的，不是 AI 生成的"。4 动词（default/audit/redesign/study）+ 6 大 disciplines（pre-emit self-critique / honest copy / locked tokens / re-drawn chrome forbidden / mobile 4 档 / typography purity）+ 20 内置主题。关键词：build、audit、redesign、study、AI-slop、反 AI 生成风格。 | 代码脚手架 |

## SDD 主流程图

```
goal_engineering（开发任务统一入口）
        │
        ▼
grill-me → spec → plan → tasks → (analyze) → implement → verify
          │         │                        │
   dev/leader      anti-hallucination       diagnosing-bugs
   （心法融合）     （小任务时）              （复杂 bug 时）
                                              ↓
                                       dev/prototype（探索设计时）
                                       dev/tdd（实现时）
                                       dev/codebase-design（架构时）
                                       dev/domain-modeling（领域建模时）
                                       dev/resolving-merge-conflicts（解冲突时）
```

## 使用统计

- 总计 34 个 skill
- SDD 链 6 个 / 日常编码安全网 1 个 / 验证调试 2 个 / 元 skill 3 个 / 工程化开发 7 个 / UI/UX 设计 1 个 / 前端设计工艺 2 个 / 思维工具集 11 个
- 按 Anthropic 9 类分类：代码脚手架 10 / 产品验证 1 / 代码质量审查 5 / Runbook 排障 1 / 业务自动化 2 / 需求澄清 3 / 库和 API 参考 1

## 维护规则

1. 新增 skill 后**必须**在此添加条目
2. 修改 skill 的 description 后**必须**同步此处的 description
3. 删除 skill 后**必须**从此移除条目
4. description 保持 ≤250 字符

### 思维工具集（daily/，11 个通用思维 Prompt）

来源：文章[《都 Agent 时代了，我还是想分享给你这 12 个我最常用的 Prompt》](https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ)（作者：数字生命卡兹克）。12 个 Prompt 中 2 个已整合到现有 skill（横纵分析法→`deep_research`、人生设计术→`life_design`），其余 10 个+用户自定义组合版共 11 个独立 skill。

**组合哲学**：这些 skill 是积木不是流水线，发散优先、举例非穷尽、agent 自行判断组合方式、拿不准列给用户选。详见 [daily/README.md](daily/README.md) 组合哲学段。

| name | description（≤250字符） | 场景 |
|------|-------------------------|------|
| **daily/socratic_questioning** | 当用户困惑模糊、嘴上问的和心里想的不一致时使用。最多 6 个逐个追问找到真正值得回答的问题。每次只问一个，信息足够时立即停止。关键词：苏格拉底提问、澄清困惑、问清问题。 | 问清问题 |
| **daily/dual_layer_explanation** | 当用户想学陌生概念时使用。分别从小白和专家两个角度解释一遍，避免"好像懂了"的错觉。关键词：双层解释、学一个概念、听不懂的概念。 | 学习 |
| **daily/reverse_decomposition** | 当用户看到优秀作品想学习它好在哪时使用。先说它解决了什么问题，再反向拆解为什么有效，最后给可复用规律+操作清单+小练习。关键词：反向拆解、拆解优秀作品、拆解范例。 | 学习 |
| **daily/fact_checking** | 当用户要对任何说法做核查时使用。拆三层（事实/结论/价值判断）+联网核查 5 档可信度+推理链 5 项漏洞+补强版本。关键词：事实核查、核查说法、笛卡尔怀疑。 | 学习 |
| **daily/expert_consultation** | 当问题需要多视角输入时使用。选 3 种真正互补的专业视角，各自重新定义问题+推荐路径，然后互相质疑找出真正分歧，最后综合输出推荐方案。关键词：专家会诊、多专家视角、三视角分析。 | 解决问题 |
| **daily/first_principles** | 当方案上各种打补丁、需要回到本质时使用。把问题拆回最底层（基本事实/习惯性假设/真正目标/现实约束），暂时放下现成方案，只从基本事实重新推导可行路径。关键词：第一性原理、拆到本质、回到本质。 | 解决问题 |
| **daily/cross_domain_borrowing** | 当本行业第一性原理后仍无好解时使用。把问题剥掉行业术语抽象成底层结构，从历史案例和至少 3 个距离较远的领域寻找相似解法，翻译成适合当前处境的方案。关键词：跨领域借解、跨领域类比、跨界借解。 | 解决问题 |
| **daily/steel_man_decision** | 当用户在两个选项间犹豫不决时使用。分别构造双方最强论证（不是稻草人），找出真正分歧，只问一个最关键的问题，再给判断。与 grill-me 区别：grill-me 决策前拷问计划，钢人决策中二选一。关键词：钢人论证、双向钢人、犹豫不决、两个选项选哪个。 | 决策 |
| **daily/minimal_experiment** | 当纸上谈兵无法更清晰时使用。找出最需要验证的 3 个假设，选最可能改变结论的，设计一个低成本、可逆、7 天内能完成的最小实验。关键词：最小实验、用实验替代空想、低成本验证。 | 决策 |
| **daily/talent_mining** | 当用户怀疑自己没天赋或想找人生方向时使用。agent 扮演资深生涯咨询师，通过多轮深度对话（最多 10 个主问题），在怪癖/缺点/嫉妒/无意识胜任区/能量模式里找到被压抑的天赋，最终产出万字《个人天赋使用说明书》。关键词：挖掘天赋、隐藏天赋、找天赋。 | 认识自己 |
| **daily/decision_protocol** | 当用户面临重大人生抉择（职业转型/关系抉择/价值观冲突）时使用。预烘焙组合套餐：启动前对齐（目标/成功标准/资源/限制/协作对象）+最强论证（双向钢人）+执行纪律。简单二选一不走本 skill 走 steel_man_decision。关键词：重大决策、人生抉择、决策协议。 | 决策 |

**与 dev/grill-me 的关系**：grill-me 是决策前拷问计划（dev/），steel_man_decision 是决策中二选一（daily/），decision_protocol 是重大决策走完整协议（daily/）。三者可串联：grill-me 澄清→steel-man 决策，或直接走 decision_protocol。

---

## 变更记录

- 2026-07-20：从 13 个 skill 扩展到 19 个。新增 5 个 dev/* 工程化 skill（prototype/tdd/codebase-design/domain-modeling/resolving-merge-conflicts）。修复幻觉：原 `systematic-debugging` skill 名是错误的（既不存在于上游 mattpocock 也不存在于本项目），实际应为 `diagnosing-bugs`（6 阶段 feedback loop 流程，比原 4 阶段更系统）。anti-hallucination 升级为 folder 结构 + references/diagnosing-bugs.md 衔接路径。
- 2026-07-21：从 19 个 skill 扩展到 21 个。新增 2 个 vendor 融合的前端设计工艺 skill（dev/impeccable + dev/hallmark），来源 pbakaus/impeccable (Apache-2.0) + nutlope/hallmark (MIT)，按 "Vendor 克隆 + 适配 wrapper" 模式融合到 dev/ 桶。每个 skill 含完整 upstream/ 子目录（原版档案）+ wrapper SKILL.md（路径适配）。详见 [ADR-0003](../../../../docs/adr/0003-vendor-skill-absorption-criteria.md)。
- 2026-07-27：从 21 个 skill 扩展到 23 个。新增 2 个目标工程 skill（dev/leader + dev/goal_engineering），来源 KKKKhazix/khazix-skills/leader (Apache-2.0)。dev/leader 是方法论源（七问 + 五种死法 + Harness 心法），dev/goal_engineering 是开发任务统一编排入口（三阶段闸门流程）。spec skill 模板融合 Harness 六节（完成态/证据/反作弊/边界/取舍/我替领导拍的板）。SDD 流程图更新：goal_engineering 作为入口点。
- 2026-08-21：从 23 个 skill 扩展到 34 个。新增 daily/ 桶（11 个思维工具 skill），来源文章《都 Agent 时代了，我还是想分享给你这 12 个我最常用的 Prompt》（数字生命卡兹克）。10 个文章原版 Prompt + 1 个用户自定义组合版（decision_protocol = 对齐+钢人+执行纪律）。每个 skill 含 SKILL.md + prompt.md（原文逐字保存）。组合哲学：发散优先、举例非穷尽、agent 自行判断组合、拿不准列给用户选。详见 daily/README.md。
