---
name: goal_engineering
description: >
  开发任务统一入口：把模糊想法编排成"询问→写方案→执行"三阶段闸门流程。
  当用户说"做个功能""重构XX""开发XX""帮我定目标""goal engineering"、
  任何涉及开发/重构/新功能的需求时使用。先调研追问（路由 grilling），再写融合
  Harness 的 spec（路由 to-spec），审核通过后拆 tickets 执行到底（路由 to-tickets
  + implement），执行阶段不再询问。引用 dev.leader 的目标七问和五种死法心法。
  触发词：goal engineering、目标工程、做个功能、重构、开发、帮我定目标、定义目标、
  让 agent 自己跑、开发新功能。
task_type: dev.goal_engineering
---

# Goal Engineering · 目标工程编排入口

开发任务的统一入口。把"模糊想法→自主完成"编排成三阶段闸门流程，每阶段有明确完成态，阶段间用 AskUserQuestion 做硬闸门。

## 核心信念

**Harness 比 Goal 更重要。** 详见 `dev/leader` skill。本编排把 Leader 的目标七问 + 五种死法融入流程，确保产出的 spec 既有 Goal 也有 Harness。

## 前置判断：是否走本流程

接到开发需求时先判断（参考 GeneralGuide 的 dev_entry_points 决策树）：

- **目标模糊、需澄清** → 走本 skill 全流程
- **目标清晰、要自主跑** → 走本 skill（跳过部分追问，直接写方案）
- **小修小补**（改 bug、调参数、加一行）→ 走 `dev.anti_hallucination`，不走本 skill
- **无关本项目开发**（开发网站/游戏等）→ 走 `dev.cross_workspace_advisor`

## 三阶段流程

### 阶段 1：询问阶段（Ask）

**目标**：把模糊想法变成方向取舍记录。

**1.1 调研**（Leader 心法：自己能查的一律不问）
- 有代码库就实测：命令是否存在、基线数字、文档与实际差异（README 写的命令不存在、lint 是 echo 占位的假绿灯、没被 import 的文件从覆盖率消失——都是真坑）
- 行业知识联网查，查不到标"假设，未验证"
- 摸不到环境就把自测写成任务 0

**1.2 追问**（路由到 `dev.grilling`）
- **一次只问一个问题，强制用 `AskUserQuestion` 工具，且每次调用只放 1 个问题**（工具虽支持批量 1-4 个，但 grilling 阶段严禁一次塞多个；问完一个等回答再问下一个）
- 每个问题附推荐答案（推荐项放第一位并标注"（推荐）"）
- 沿 design tree 逐个解决决策依赖
- 只问查不到且会改变任务书的：方向取舍、验收裁量、风险偏好、时间盒

**1.3 拍板**（Leader 心法：用问题问，该做/不该做各一波）
- 每个问题给 2-4 选项加推荐
- 用户不在场按默认走、标"猜的"、写进"我替领导拍的板"一节——沉默替领导拍板是越权，摆到明面是尽职

**完成态**：方向取舍记录写入 `temp/sdd/<slug>/design-decisions.md`

**闸门**：AskUserQuestion 确认"方向已清晰，进入写方案"——选项「继续写方案（推荐）」/「再追问几个问题」/「取消」

### 阶段 2：写方案阶段（Spec）

**目标**：产出融合 Harness 的 spec 文档。

**步骤**（路由到 `dev.to_spec`，产物结构见该 skill 的融合模板）：
1. 按 Leader 六节（目的/完成态/证据/反作弊/边界/取舍）+ to-spec 模板写 spec
2. 写入 `temp/sdd/<slug>/spec.md`
3. 自检（Leader 发出前自检）：
   - 分型对吗？命令亲手跑过？没问的都写进"我替领导拍的板"带默认值？
   - 验收全是命令？防作弊、反向验证、三道止损、PROGRESS.md/BLOCKED.md 机制齐？
   - 全文无"来找我"？大白话？零多余玩笑？

**完成态**：`temp/sdd/<slug>/spec.md` 存在且通过自检

**闸门**：AskUserQuestion 确认"方案审核通过，进入执行"——选项「通过，开始执行（推荐）」/「方案需修改」/「取消」。**这是关键闸门，通过后进入执行阶段不再询问。**

### 阶段 3：执行阶段（Execute）

**目标**：拆任务 + 实现到底，不再打扰用户。

**3.1 拆任务**（路由到 `dev.to_tickets`）
- 把 spec 拆成 tracer-bullet tickets（vertical slice，贯穿所有层）
- 写入 `temp/sdd/<slug>/tickets.md`，按依赖顺序编号，声明 blocked_by
- **不再询问**用户拆分粒度——按 to-tickets 的 vertical slice 规则自行决策

**3.2 实现**（路由到 `dev.implement`）
- 按依赖图领 frontier ticket（blockers 都 completed 的）
- TDD 实现（路由 `dev.tdd`），code-review（路由 `dev.code_review`），提交，更新 tickets.md 状态
- 循环领取下一个 frontier ticket，直到全部完成

**完成态**：所有 ticket 状态 = completed

**不再询问**：执行阶段遇到问题按 spec 的 Harness 规则自行决策：
- 三道止损：同一验收连败 3 次换项、结果比基线差回滚如实报告、量出数字对不上就停
- 拿不准的写进 `temp/sdd/<slug>/BLOCKED.md`，跳过继续做别的
- **绝不**回头问用户（执行阶段不再询问是硬规则）

## 上下文压缩防护（执行阶段硬规则）

执行阶段极易触发上下文压缩。压缩后 agent 容易误判"压缩历史次要"而重拆任务——这是严重错误。

**真源层级**（从高到低）：
1. `temp/sdd/<slug>/spec.md` + `tickets.md` —— 方案与任务真源
2. 压缩历史中的用户明确要求 —— 用户意图权威
3. 压缩历史中的 agent 中间产物 —— 可重建，非权威

**硬规则**：
- 上下文压缩后，醒来第一步读 `spec.md` 和 `tickets.md`，对照当前 ticket 状态续跑
- **禁止**因压缩而重写 spec.md 或重拆 tickets.md，除非用户明确要求"重新规划"
- 每完成一个 ticket，立刻更新 tickets.md 的 Status 字段（ready→in_progress→completed）作为恢复锚点
- 详见 AGENTS.md「上下文压缩防护」小节

## 阶段间路由总结

| 阶段 | 路由到 | 产物 | 闸门 |
|------|--------|------|------|
| 询问 | dev.grilling（追问）+ dev.leader（心法） | design-decisions.md | AskUserQuestion 确认方向 |
| 写方案 | dev.to_spec（融合 Harness） | spec.md | AskUserQuestion 审核通过 |
| 执行 | dev.to_tickets + dev.implement | tickets.md + 代码 | 无闸门，做到底 |

## 关键规则

- **Harness 不可省**：spec 必须有 Anti-Cheat 节，点名禁止偷懒姿势，配反向验证
- **执行阶段不询问**：方案审核通过后，不再用 AskUserQuestion 打断用户，除非全部完成或遇 BLOCKED
- **压缩后读真源**：压缩后先读 spec.md + tickets.md 续跑，不重拆不重写
- **法与情报分家**："不许"是法（违反即不合格），"建议"是情报（可走更好的路，PROGRESS.md 记一句）
- **富规格优先**：仓库已有的测试套件/schema/验收脚本直接写路径当规格，别用散文复述

## 与其他 skill 的关系

- **引用**：`dev.leader`（七问 + 五种死法心法）、`dev.grilling`（追问执行）、`dev.to_spec`（写 spec）、`dev.to_tickets`（拆 tickets）、`dev.implement`（实现）、`dev.tdd`（TDD）、`dev.code_review`（审查）
- **被路由**：GeneralGuide 的 dev_entry_points 决策树把"goal_clear_full_flow"指向本 skill
