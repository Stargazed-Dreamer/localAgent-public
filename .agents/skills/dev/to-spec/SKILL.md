---
name: to-spec
description: >
  把当前对话转成 spec（你可能称之为 PRD）并存档。当用户说"写 spec"、"整理成 spec"、
  "把刚才讨论的记下来"、"to-spec"、"出个 PRD"时使用。不做访谈，只综合已经讨论的内容。
  本版融合 dev/leader 的 Harness 心法：spec 模板新增 Done/Proof/Anti-Cheat/Bounds/Trade
  六节，确保 spec 既有 Goal 也有 Harness（什么不能做）。适用于 server 重构、client 面板开发、
  工具脚本设计等需要明确方案的场景。触发词：to-spec、写 spec、整理 spec、出 PRD、把讨论记下来、规格说明。
task_type: dev.to_spec
---

# To Spec (to-spec)

把当前对话转成 spec 并存档。**不要**访谈用户，只综合你已经知道的内容。

## 改造说明（原版 → 本版）

原版（mattpocock/skills）把 spec 发布到 issue tracker（GitHub Issues / Linear 等），依赖 `/setup-matt-pocock-skills` 配置 tracker。本版改造为：

- **spec 存储**：`temp/sdd/<feature-slug>/spec.md`（本地 markdown 文件，与 design-decisions.md / tickets.md / checklist.md 同目录）
- **不写 wip 记录**：不调 `wip_create`，spec 文件本身是真源，implement skill 直接从路径读取
- **无需配置**：不依赖外部 issue tracker，也不依赖 wip_tasks 表

## Process

1. 如果还没有探索 repo，先探索它以理解 codebase 当前状态。在 spec 中始终使用项目 domain glossary vocabulary（见 `dev/domain-modeling` skill 维护的 `CONTEXT.md`），并遵守相关 ADRs（`docs/adr/`）。

2. 草拟你准备在哪些 seams 上测试这个 feature。优先使用现有 seams，而不是新增 seams。使用尽可能高层的 seam。如果确实需要新增 seams，尽可能在最高层提出。

   Seams 越少越好，理想数量是一个。与用户确认这些 seams 是否符合预期。Seam 概念见 `dev/codebase-design` skill。

3. 使用下面模板写 spec，保存到 `temp/sdd/<feature-slug>/spec.md`（`<feature-slug>` 用英文小写+连字符，如 `screen-refactor`）。同目录下通常还会有 `design-decisions.md`（grilling 产出）、`tickets.md`（to-tickets 产出）、`checklist.md`。

4. **不调 `wip_create`**。spec 文件本身是真源，后续 `to-tickets` 直接从 `temp/sdd/<feature-slug>/spec.md` 读取。

## spec 模板（融合 Leader 六节 + Harness 心法）

> 本模板吸收 `dev/leader` 的目标七问（Why/Done/Proof/Anti/Bounds/Trade/Unknown）和五种死法心法。
> 前六节（Problem Statement ~ Trade）是 Leader 六节，后续节继承原 to-spec 结构。
> 详见 `dev/leader/references/anatomy.md` 的结构规格。

```markdown
# <Feature 名>

## Problem Statement（目的 · Why）

为什么干这活。从用户视角描述问题，一句话意图：为什么干 + 干完世界什么样。
书里没写到的情况，执行者靠它自己裁。来自军事传统的 Commander's Intent——意图不变，手段随机应变。

## Done（完成态）

船回港时甲板上该有什么。具体到靠岸一刻就能判断的程度（出去转一圈不是完成态，带回三船香料才是）。

## Proof（证据 · 验收）

谁来清点货舱、怎么算数。验收全是命令（明卷），机器可判的判定。不只说"做完了"，要贴实际命令输出。

## Anti-Cheat（反作弊 · Harness）★ 核心

- **基线不可退**：测试数/覆盖率 ≥ 基线、skipped 0
- **点名禁止具体偷懒姿势**：.skip/todo、放宽断言、mock 被测对象、删测试、改阈值或验收脚本、`|| true`——全算失败
- **判卷标准冻结**：测试/验收脚本/CI 配置碰都不许碰
- **暗卷**：2-3 条执行者看不见的抽查，自留在会话侧 scratchpad，不进 spec
- **反向验证**：坏了谁会知道？答"没人"就要配反向验证——亲手制造一次失败证明会响，贴红→绿输出
- **三道止损**：同一验收连败 3 次换项、结果比基线差回滚如实报告、量出数字对不上就停
- **富规格优先**：仓库已有的测试套件/schema/验收脚本直接写路径当规格，别用散文复述

## Bounds（边界）

只许走这些航线，其他海域不准进。白名单：只允许改哪些路径 + 新建文件，其余只读（用白名单不用黑名单）。粮食够吃 N 天，第 M 天没找到就掉头。

## Trade（取舍）

风暴里保货还是保船。两个要求打架时的让步顺序（如"算得对 > 做得全 > 做得快"）。冲突时优先级不提前说，执行者只能猜，猜错了整趟废了。

## Solution（解决方案）

问题的解决方案，从用户视角描述。

## User Stories

一份很长的编号 user stories 列表。每条 user story 使用以下格式：

1. As an <actor>, I want a <feature>, so that <benefit>

## Implementation Decisions

已作出的 implementation decisions 列表。可以包括：

- 将 build/modify 的 modules
- 将 modify 的 module interfaces
- 来自 developer 的技术澄清
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

不要包含具体 file paths 或 code snippets。它们可能很快过时。

例外：如果 prototype 产出的 snippet 比 prose 更精确地编码了某个决策（state machine、reducer、schema、type shape），可以内联到相关 decision 中，并简短说明它来自 prototype。只保留决策密集部分，不要放完整 working demo。

## Testing Decisions

已作出的 testing decisions 列表。包括：

- 什么是好测试的描述（只测试 external behavior，不测试 implementation details）
- 哪些 modules 会被测试
- 测试的 prior art（即 codebase 中类似类型的 tests）

## Out of Scope

本 spec 范围外事项的描述。

## 我替领导拍的板（默认值决策）

没问出口的每个决定一行：问题 → 默认值（标"猜的"）｜猜错的代价。领导发出前可改；执行者按默认走，不停下来等。
写不出机器可判命令的目标（文风、体验类）也记在这里：改成抽查点 + 领导亲验，注明这活只能半托。

## Further Notes

关于 feature 的其他 notes。
```

## 关键规则

- **不做访谈**：只综合当前对话已经讨论的内容，不主动提问引导用户（访谈由 `dev.grill_me`/`dev.grilling` 负责，本 skill 接收已澄清的内容）
- **使用 domain glossary**：spec 中的术语要与 `CONTEXT.md` 一致
- **seams 最少化**：理想数量是一个，与用户确认后再写 spec
- **不写具体 file paths**：spec 是方案不是实现，file paths 很快过时
- **不写 wip 记录**：spec 文件写完即完成，不调 `wip_create`；后续 `to-tickets` 直接从 `temp/sdd/<feature-slug>/spec.md` 读取
- **富规格优先**：仓库已有的规格性文件（测试套件、schema、验收脚本、设计稿）直接写路径当规格，别用散文复述——"测试名本身就是业务要求"
- **法与情报分家**："不许"是法（违反即不合格，每条溯源到一次实测或一次领导裁决）；"建议"是情报（执行者有更好的路可走，在 `PROGRESS.md` 记一句为什么）。把情报写成法，是替执行者做它临场更懂的决定
- **先分型**：动笔前能写出验收命令的是**执行型**，全套照走；领导要答案本身（调研/选型/该不该做 X）的是**探索型**——硬指标只会收到凑数的答案，改四处（完成条件换学习目标、预算换可承受损失、防作弊主防编造、此路不通=合格交付），详见 `dev/leader/references/anatomy.md`
- **Harness 不可省**：Anti-Cheat 节是 spec 的核心，点名禁止偷懒姿势 + 反向验证 + 三道止损缺一不可。没有 Harness 的 spec，执行者永远会找到你没想到的捷径

## 与其他 skill 的关系

- **上游**：用户对话 → `to-spec` 产出 spec
- **下游**：`to-tickets` 把 spec 拆成 tickets；`implement` 按 spec/tickets 实现
- **词汇支持**：`dev/domain-modeling` 维护 CONTEXT.md；`dev/codebase-design` 提供 seam 词汇
