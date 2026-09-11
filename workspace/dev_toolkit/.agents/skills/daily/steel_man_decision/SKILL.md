---
name: steel_man_decision
description: >
  双向钢人论证：两个选项间犹豫不决时，分别构造双方最强论证（不是稻草人），
  找出真正的分歧和最可能改变结论的关键变量，只问一个最关键的问题，再给判断。
  触发词：钢人论证、双向钢人、犹豫不决、两个选项选哪个、难以决定选哪个、
  steel man、决策二选一、纠结选哪个。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.steel_man_decision
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 决策
  prompt_index: 8
---

# 双向钢人论证 (steel_man_decision)

## 触发词
钢人论证、双向钢人、犹豫不决、两个选项选哪个、难以决定选哪个、steel man、决策二选一、纠结选哪个

## 概述
和 grill-me 不同：grill-me 是帮你更好地提出问题（决策前），钢人是当你有答案后
不知道选哪个时做决策用的。本 skill 用最完整有力的方式重述选择，分别给两个方向的
最强论证（不是故意弱化任何一方），找出真正的分歧，只问一个最可能改变结论的问题，
用户回答后再给判断+理由+适用条件+下一步行动。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/steel_man_decision/prompt.md`
2. **让用户填入决策**：`AskUserQuestion` 问用户：问题、两个选项、目标、现实约束
3. **按 Prompt 执行**：
   - **先别急着回答**，也别默认用户已经把问题想清楚
   - **双向钢人论证**：
     1. 用最完整、有力的方式，重述用户真正需要做出的选择
     2. 分别给出支持两个方向的：
        - 最强理由
        - 适用条件
        - 最大收益
        - 最大风险
        - 最难回答的反对意见
     3. 找出 3 项：
        - 双方真正的分歧
        - 最可能改变结论的关键变量
        - 还需要补充的信息
     4. **只问一个最可能改变结论的问题**（用 `AskUserQuestion`）
4. **等用户回答后**：给出明确判断、理由、适用条件和下一步行动

## 关键约束

- **必须构造双方最强论证**：不要故意弱化任何一方（这是钢人，不是稻草人）
- **先别急着回答**：先走完钢人论证再给判断
- **只问一个问题**：最可能改变结论的那个
- **等用户回答后再判断**：不要预先下结论

## 与 dev.grill_me 的区别

- `dev.grill_me`：决策前，帮用户更好地提出问题（拷问计划）
- `daily.steel_man_decision`：决策中，两个选项间犹豫不决时做决策
- 两者可串联：先 grill-me 澄清，再 steel-man 决策

## 兄弟工具（按需组合，非必须）

本 skill 属于"决策"层：

- `daily.socratic_questioning`：问题模糊时先澄清再做钢人
- `dev.grill_me`：先压力测试计划再钢人决策
- `daily.first_principles`：先拆到本质再钢人，避免在表面选项上打转
- `daily.expert_consultation`：先多视角输入再钢人，避免视角盲区
- `daily.minimal_experiment`：钢人后若仍犹豫，用最小实验获取现实反馈
- `daily.decision_protocol`：重大人生抉择走完整协议（对齐+钢人+执行纪律）

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户决策场景 + 最后只问一个关键问题）
