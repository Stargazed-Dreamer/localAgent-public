---
name: fact_checking
description: >
  事实核查：对任何说法（观点/结论/数据/方案）做笛卡尔式怀疑。先拆成事实/结论/价值
  判断三层，对事实部分联网核查并标记 5 档可信度，再检查推理链漏洞，最后给补强版本。
  触发词：事实核查、核查说法、验证观点、检查推理链、这个说法对吗、可信度评估、
  fact checking、笛卡尔怀疑。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.fact_checking
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 学习
  prompt_index: 4
---

# 事实核查 (fact_checking)

## 触发词
事实核查、核查说法、验证观点、检查推理链、这个说法对吗、可信度评估、fact checking、笛卡尔怀疑

## 概述
AI 有幻觉，人类幻觉往往更大。本 skill 对任何说法做三层拆解（事实/结论/价值判断），
对事实部分联网核查并标记 5 档可信度（已证实/基本成立需收窄/存在争议/证据不足/明显错误），
再检查推理链 5 项漏洞，最后给补强后的最合理版本。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/fact_checking/prompt.md`
2. **让用户提供待核查说法**：`AskUserQuestion` 问用户要核查什么（观点/结论/数据/方案）
3. **按 Prompt 执行**：
   - **拆三层**：可外部验证的事实 / 从事实推出的结论 / 价值判断
   - **事实部分联网核查**（用 WebSearch / WebFetch）：
     - 标记 5 档：1.已证实 2.基本成立需收窄 3.存在争议 4.证据不足 5.明显错误
   - **检查推理链 5 项**：
     1. 事实能否推出当前结论
     2. 是否藏着未经验证的假设
     3. 是否混淆相关性和因果关系
     4. 是否遗漏其他解释或关键信息
     5. 结论在什么条件下成立或失效
   - **最后输出 4 项**：
     1. 哪些事实可信，哪些需要修正
     2. 推理链中最关键的漏洞
     3. 补强后的最合理版本
     4. 用户目前可以相信到什么程度

## 关键约束

- **必须联网核查**：不能仅靠已有知识，事实核查的价值在验证
- **5 档可信度必须明确**：不要给"可能对可能错"的废话
- **事实/结论/价值判断必须分开**：不要混在一起核查
- **诚实标注暂缺**：找不到证据时明确写"暂未核实"，绝不编造

## 兄弟工具（按需组合，非必须）

本 skill 属于"学习"层：

- `daily.dual_layer_explanation`：核查后若涉及陌生概念，路由到双层解释
- `daily.expert_consultation`：核查后若需多专家视角，路由到专家会诊
- `daily.socratic_questioning`：核查中若发现用户问题本身模糊，路由到苏格拉底提问
- `adhoc.anti_hallucination`：代码相关的事实核查走 anti_hallucination（不是本 skill）

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户要核查什么）
- `WebSearch` / `WebFetch`（联网核查事实）
