---
name: reverse_decomposition
description: >
  反向拆解：看到优秀作品（产品页面/网页/方案/流程/数据看板等）想学习它好在哪时，
  先说它解决了什么问题，再反向拆解为什么有效。提炼可复用规律+操作清单+小练习。
  触发词：反向拆解、拆解优秀作品、拆解范例、学习它好在哪、拆解这个产品、
  反向工程一个作品、reverse decomposition。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.reverse_decomposition
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 学习
  prompt_index: 3
---

# 反向拆解 (reverse_decomposition)

## 触发词
反向拆解、拆解优秀作品、拆解范例、学习它好在哪、拆解这个产品、反向工程一个作品、reverse decomposition

## 概述
看到好作品想学习它好在哪，不要停留在"感觉很厉害"。本 skill 让 AI 先说它解决了
什么问题，再反向拆解为什么有效，重点分析 5 项（服务谁/结构流程/关键选择/完成标准/
可迁移规律），最后给可复用规律+操作清单+小练习。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/reverse_decomposition/prompt.md`
2. **让用户提供材料**：`AskUserQuestion` 问用户：
   - 优秀范例（粘贴产品页面/网页/方案/流程说明/数据看板或其他成品）
   - 想学会什么
3. **按 Prompt 执行**：
   - 先用一句话说明它解决了什么问题
   - 重点分析 5 项：
     1. 它服务谁，目标是什么
     2. 它采用了什么结构或流程
     3. 哪些关键选择拉开了质量差距
     4. 它的完成标准是什么
     5. 哪些规律可以迁移，哪些细节只适合这个案例
   - 最后给 3 项：
     1. 提炼 3-5 条可复用规律
     2. 一份可以照着执行的操作清单
     3. 一个最值得先尝试的小练习

## 关键约束

- **先说解决了什么问题**：不要直接跳到拆解
- **区分可迁移规律 vs 案例细节**：不要把所有细节都当规律
- **必须给操作清单和小练习**：不只是分析

## 兄弟工具（按需组合，非必须）

本 skill 属于"学习"层：

- `daily.dual_layer_explanation`：拆解后若遇陌生概念，路由到双层解释
- `daily.fact_checking`：拆解中涉及的数据/说法，路由到事实核查
- `dev.impeccable` `distill` 命令：拆解 UI 设计 DNA 时可用（更专业的 UI 设计拆解）
- `dev.hallmark` `study` 命令：拆解反 AI 味设计时可用

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户范例 + 想学什么）
