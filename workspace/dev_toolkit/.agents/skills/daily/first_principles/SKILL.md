---
name: first_principles
description: >
  第一性原理：把问题拆回最底层，区分已确认的基本事实/习惯性假设/真正目标/现实约束，
  暂时放下行业惯例和现成方案，只从基本事实重新推导可行路径。适合处理路径依赖、
  打补丁式方案、组织流程/产品架构/复杂系统改造。
  触发词：第一性原理、拆到本质、回到本质、first principles、重新推导路径、
  打补丁不如重推、路径依赖拆解。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.first_principles
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 解决问题
  prompt_index: 6
---

# 第一性原理 (first_principles)

## 触发词
第一性原理、拆到本质、回到本质、first principles、重新推导路径、打补丁不如重推、路径依赖拆解

## 概述
方案上各种打补丁，不如用第一性原理直接找到最本质，然后推倒重来。本 skill 把问题
拆回最底层，区分 4 项（已确认基本事实/习惯性假设/真正目标/现实约束），暂时放下
行业惯例和现成方案，只从基本事实重新推导可行路径。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/first_principles/prompt.md`
2. **让用户填入问题**：`AskUserQuestion` 问用户要解决的问题
3. **按 Prompt 执行**：
   - 拆成 4 层：
     1. 已经确认、无法绕开的基本事实
     2. 习惯性接受、却没有验证过的假设
     3. 真正想实现的目标
     4. 现实中的资源与约束
   - 暂时放下行业惯例和现成方案
   - 只从基本事实、目标和约束出发，重新推导可行路径
   - 输出 4 项：
     1. 原方案中只在修补表面的部分
     2. 从基本事实重新推导出的新路径
     3. 这条路径成立的前提
     4. 验证它的第一步

## 关键约束

- **必须区分"基本事实"和"习惯性假设"**：很多"理所当然"其实是未验证的假设
- **必须暂时放下现成方案**：不要在旧方案上修补
- **新路径必须有成立前提和验证第一步**：不只是空想

## 兄弟工具（按需组合，非必须）

本 skill 属于"解决问题"层：

- `daily.cross_domain_borrowing`：拆完本质后若需跨领域借解，路由到跨领域借解
- `daily.expert_consultation`：拆完本质后若需多视角，路由到专家会诊
- `daily.steel_man_decision`：拆完后若仍二选一，路由到钢人
- `daily.minimal_experiment`：推导出新路径后，用最小实验验证

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户要解决的问题）
