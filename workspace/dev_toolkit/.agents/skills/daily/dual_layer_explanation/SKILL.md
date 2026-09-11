---
name: dual_layer_explanation
description: >
  双层解释法：学陌生概念时，分别从小白和专家两个角度解释一遍，避免"好像懂了"
  的错觉。小白版用生活化语言+具体例子，专家版用准确术语讲清机制/边界/误解。
  触发词：双层解释、双层解释法、小白专家两版解释、学一个概念、听不懂的概念、
  用两层解释帮我学、dual layer explanation。
  本 skill 是 daily/ 思维工具集的一员，可与其他思维工具组合使用，
  详见 daily/README.md 组合哲学。
task_type: daily.dual_layer_explanation
source:
  article: 都Agent时代了，我还是想分享给你这12个我最常用的Prompt
  author: 数字生命卡兹克
  url: https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ
  category: 学习
  prompt_index: 2
---

# 双层解释法 (dual_layer_explanation)

## 触发词
双层解释、双层解释法、小白专家两版解释、学一个概念、听不懂的概念、用两层解释帮我学、dual layer explanation

## 概述
"把我当小学生解释"容易停留在类比层，真正机制还是雾。本 skill 让 AI 分别从小白
和专家两个角度解释一遍，再列出对应关系、最易理解错的地方、3 个检查理解的问题。

## Prompt 真源
**完整 Prompt 逐字保存于** [prompt.md](prompt.md)。执行前必须读取。

## 工作流

1. **读 Prompt**：`Read .agents/skills/daily/dual_layer_explanation/prompt.md`
2. **让用户填入学习对象**：`AskUserQuestion` 问用户想学什么概念或问题
3. **按 Prompt 执行**：
   - 第一层：小白版（生活化语言 + 一个具体例子）
   - 第二层：专家版（准确术语 + 核心机制 + 适用边界 + 常见误解）
   - 整理 3 项：
     1. 小白说法与专业术语的对应关系
     2. 用户最容易理解错的地方
     3. 3 个用于检查是否真正理解的问题

## 关键约束

- **必须分两层**：不要只给一层解释
- **小白版必须有具体例子**：不只是类比
- **专家版必须讲适用边界和常见误解**：不只是定义

## 兄弟工具（按需组合，非必须）

本 skill 属于"学习"层，与同层工具可串联：

- `daily.reverse_decomposition`：学成品时先反向拆解，再双层解释其中概念
- `daily.fact_checking`：学完后若涉及外部说法，路由到事实核查
- `daily.teach`：若需多 session 系统化教学，路由到 teach（持久化教学工作区）
- `daily.cross_domain_borrowing`：学完后若想看跨领域类比，路由到跨领域借解

组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学。

## 依赖

- `Read`（读 prompt.md）
- `AskUserQuestion`（问用户想学什么）
