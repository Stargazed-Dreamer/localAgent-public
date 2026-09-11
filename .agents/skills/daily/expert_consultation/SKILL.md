---
name: expert_consultation
description: >
  专家会诊：让 AI 为问题选择 3 种真正互补的专业视角，各自重新定义问题+推荐路径+
  忽略的风险+改变判断的证据，然后互相质疑找出真正的分歧，最后综合输出推荐方案。
  触发词：专家会诊、多专家视角、三视角分析、互补专家团、专家互相质疑、
  expert consultation、多视角会诊。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.expert_consultation
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 解决问题
  prompt_index: 5
---

# 专家会诊 (expert_consultation)

## 触发词
专家会诊、多专家视角、三视角分析、互补专家团、专家互相质疑、expert consultation、多视角会诊

## 概述
"你是一位 20 年经验专家"的 Prompt 对多领域问题不够用。本 skill 让 AI 自己组一个
真正互补的小型专家团（3 种视角），各自回答 4 项，然后互相质疑找出真正的分歧，
最后综合输出推荐方案+适用条件+最大风险+退出条件+第一步行动。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/expert_consultation/prompt.md`
2. **让用户填入问题**：`AskUserQuestion` 问用户的问题、已知事实、目标、现实约束
3. **按 Prompt 执行**：
   - **先不要直接给方案**
   - 选择 3 种真正互补的专业视角，说明每种视角为什么必要
   - 让每种视角分别回答 4 项：
     1. 它怎样重新定义这个问题
     2. 它最推荐的解决路径
     3. 其他视角最容易忽略的风险
     4. 什么新证据会让它改变判断
   - 让三种视角互相质疑，找出 3 项：
     1. 共同认可的事实
     2. 真正的分歧
     3. 分歧背后的不同假设
   - 综合输出 5 项：
     1. 综合后最推荐的方案
     2. 适用条件
     3. 最大风险
     4. 退出条件
     5. 第一步行动
4. **信息不足时**：先只问一个最关键的问题

## 关键约束

- **三视角必须真正互补**：不要选三个高度相似的身份
- **不模仿或编造真实人物观点**：自己构造视角
- **必须互相质疑**：真正的信息往往在分歧里，不是各自发言就结束
- **先不要直接给方案**：先走完三视角再综合

## 兄弟工具（按需组合，非必须）

本 skill 属于"解决问题"层：

- `daily.first_principles`：会诊后若需拆到本质，路由到第一性原理
- `daily.cross_domain_borrowing`：会诊后若需跨领域借解，路由到跨领域借解
- `daily.steel_man_decision`：会诊后若仍二选一犹豫，路由到钢人
- `daily.socratic_questioning`：问题模糊时先澄清再会诊

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户问题+事实+目标+约束）
