---
name: socratic_questioning
description: >
  苏格拉底式提问：当用户困惑模糊、嘴上问的和心里想的不一致时，通过最多 6 个追问
  帮用户找到真正值得回答的问题。每次只问一个，根据回答决定下一问。
  触发词：苏格拉底提问、苏格拉底式问诊、澄清困惑、找到真正的问题、问清问题、
  我到底想问什么、socratic questioning。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.socratic_questioning
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 问清问题
  prompt_index: 1
---

# 苏格拉底式提问 (socratic_questioning)

## 触发词
苏格拉底提问、苏格拉底式问诊、澄清困惑、找到真正的问题、问清问题、我到底想问什么、socratic questioning

## 概述
人类经常嘴上问的和心里真正想表达的不是一回事。本 skill 通过最多 6 个逐个追问，
帮用户把问题想清楚，补上 AI 需要的上下文，最终整理出"真正值得回答的新问题"。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取，不要凭记忆复述。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/socratic_questioning/prompt.md`
2. **让用户填入困惑**：用 `AskUserQuestion` 请用户描述"尽量具体地描述发生了什么、
   你怎么理解，以及你卡在哪里"
3. **逐个追问**（核心）：
   - **每次只问一个问题**，根据回答决定下一问，不要提前给一整套问卷
   - 优先区分用户说的是：可验证的事实 / 对事实的解释 / 价值判断 / 希望实现的目标
   - 检查关键词是否含糊、默认了哪些前提、证据来自哪里、有没有相反解释
   - 每次提问前，用一句话说明上一条回答让你更新了什么判断
   - 只问可能改变结论的问题；信息足够时立刻停止，不必凑满 6 个
4. **问诊整理**：结束后整理出 6 项：
   1. 用户最开始问的问题
   2. 用户真正想解决的问题
   3. 已经确认的事实
   4. 仍未验证的假设
   5. 最可能改变结论的关键变量
   6. 一个准确、具体、可以继续行动的新问题
5. **等用户确认新问题**后，再给出判断、理由和下一步行动

## 关键约束

- **每次只问一个问题**（用 `AskUserQuestion`，1 个问题）
- **先不要给建议**：本 skill 的目标是澄清问题，不是解决问题
- **不凑满 6 个**：信息足够时立刻停止
- **每次提问前说一句判断更新**：让用户知道 agent 在认真听

## 兄弟工具（按需组合，非必须）

本 skill 属于"问清问题"层，通常是其他思维工具的前置。组合方式不固定，举例仅说明思路：

- `daily.steel_man_decision`：澄清问题后若涉及二选一决策，路由到钢人
- `dev.grill_me`：先澄清问题再压力测试计划
- `daily.first_principles`：澄清后若需拆本质，路由到第一性原理
- `daily.expert_consultation`：澄清后若需多视角输入，路由到专家会诊

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（逐个追问，每次 1 个问题）
