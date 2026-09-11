---
name: cross_domain_borrowing
description: >
  跨领域借解：把问题剥掉行业术语抽象成底层结构，从历史案例和至少 3 个距离较远
  的领域寻找底层结构相似的解法，翻译成适合当前处境的解决方案。适合在本行业
  第一性原理后仍无好解时扩散视角。
  触发词：跨领域借解、跨领域类比、其他行业怎么解决、跨领域借鉴、cross domain、
  跨界借解、底层结构相似。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.cross_domain_borrowing
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 解决问题
  prompt_index: 7
---

# 跨领域借解 (cross_domain_borrowing)

## 触发词
跨领域借解、跨领域类比、其他行业怎么解决、跨领域借鉴、cross domain、跨界借解、底层结构相似

## 概述
第一性原理是回到本质，跨领域借解是扩散视角看其他领域相通的解法。本 skill 把问题
剥掉行业术语抽象成底层结构，从历史案例+至少 3 个彼此距离较远的领域寻找相似问题，
每个案例说明 5 项，最后选出 3 种最值得借用的机制翻译成解决方案+推荐低成本可逆实验。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/cross_domain_borrowing/prompt.md`
2. **让用户填入困惑**：`AskUserQuestion` 问用户背景、当前做法、现实约束、具体卡点
3. **按 Prompt 执行**：
   - **剥掉行业术语**，抽象成人类在其他领域也可能遇到的问题
   - 找出 3 项：底层结构 / 核心矛盾 / 普通解法失效的原因
   - 从历史案例 + 至少 3 个彼此距离较远的领域寻找底层结构相似的问题
   - 每个案例说明 5 项：
     1. 那个领域遇到了什么问题
     2. 使用了什么解决机制
     3. 与我的问题相似在哪里
     4. 哪些部分可以迁移
     5. 什么条件下会失效
   - 最后选出最值得借用的 3 种机制，翻译成适合当前处境的解决方案
   - 推荐一个最值得先试的低成本、可逆实验

## 关键约束

- **至少 3 个彼此距离较远的领域**：不要选 3 个相近领域
- **必须说明"什么条件下会失效"**：不是所有机制都能迁移
- **最后必须给低成本可逆实验**：不只是理论分析

## 兄弟工具（按需组合，非必须）

本 skill 属于"解决问题"层：

- `daily.first_principles`：先拆到本质再跨领域借解，避免在表面借解
- `daily.expert_consultation`：跨领域后若需多视角验证，路由到专家会诊
- `daily.minimal_experiment`：借解后用最小实验验证迁移效果
- `daily.steel_man_decision`：借解后若仍二选一，路由到钢人

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户背景+做法+约束+卡点）
- `WebSearch`（可选，查历史案例和其他领域的解法）
