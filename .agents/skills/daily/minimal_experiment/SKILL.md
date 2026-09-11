---
name: minimal_experiment
description: >
  用最小实验替代空想：当纸上谈兵无法再让你更清晰时，找出最需要验证的 3 个假设，
  选出最可能改变结论的那一个，设计一个低成本、可逆、7 天内能完成的最小实验。
  触发词：最小实验、最小可行实验、用实验替代空想、试一下再说、低成本验证、
  minimal experiment、验证假设。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.minimal_experiment
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 决策
  prompt_index: 9
---

# 用最小实验替代空想 (minimal_experiment)

## 触发词
最小实验、最小可行实验、用实验替代空想、试一下再说、低成本验证、minimal experiment、验证假设

## 概述
现实世界中有些决定，继续纸上谈兵也不会更清晰。本 skill 找出决策背后最需要验证的
3 个假设，选出最可能改变结论的那一个，设计一个低成本、可逆、能在 7 天或用户可接受
周期内完成的最小实验，明确观察指标和支持/停止的判定标准，最后给明天就能开始的
第一个动作。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/minimal_experiment/prompt.md`
2. **让用户填入纠结**：`AskUserQuestion` 问用户的选择或想法
3. **按 Prompt 执行**：
   - 找出决策背后最需要验证的 3 个假设
   - 选出最可能改变最终结论的那一个
   - 围绕这个假设，设计一个低成本、可逆、能在 7 天或用户可接受周期内完成的
     最小实验
   - 写清 6 项：
     1. 具体要做什么
     2. 需要投入多少时间和资源
     3. 观察什么指标
     4. 什么结果支持继续
     5. 什么结果提醒停止
     6. 实验结束后能获得什么新信息
   - 最后告诉用户：明天就能开始的第一个动作是什么

## 关键约束

- **必须低成本、可逆**：不要设计需要大投入或不可逆的实验
- **必须有明确的停止条件**：不要"看看再说"
- **必须给明天的第一个动作**：不要停留在抽象实验设计

## 兄弟工具（按需组合，非必须）

本 skill 属于"决策"层，常作为钢人之后的下一步：

- `daily.steel_man_decision`：钢人后若仍犹豫，用最小实验获取现实反馈
- `daily.first_principles`：先拆到本质再设计实验，避免验证错假设
- `daily.cross_domain_borrowing`：借解后用最小实验验证迁移效果
- `dev.prototype`：软件原型验证走 dev.prototype（更专业的软件原型工具）
- `daily.decision_protocol`：重大决策走完整协议，本 skill 是其中"执行"环节的候选工具

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户的选择或想法）
