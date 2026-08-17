---
name: analyze
description: >
  当用户要做一致性检查、交叉比对，或 tasks 拆完后 implement 之前使用。把 spec/plan/tasks 放一块找打架和遗漏。关键词：一致性、交叉比对、检查、analyze、对不上、冲突。
---

# analyze — 一致性检查（对得上吗）

## 触发词
一致性、交叉比对、检查、analyze、对不上、冲突、矛盾、查漏、交叉检查

## 概述

把 spec.md、plan.md、tasks.md 放一块交叉比对，找出互相打架、对不上、遗漏的地方。在正式写代码**之前**跑，把"没想清楚""对不上"的坑提前填了。

SDD 的兜底环节。不是必跑，但正经活建议跑一遍。

## 何时该用

- tasks.md 刚拆完，写代码前
- 三份文档任一份改动后，重新对齐
- 用户怀疑"哪里对不上"

## 前置条件

- `.agents/specs/{feature}/` 下至少有 spec.md 和 plan.md，最好也有 tasks.md

## 工作流

### 1. 读三份文档

完整读 spec.md、plan.md、tasks.md。

### 2. 交叉比对

按以下维度逐一检查：

**spec → plan（需求有没有技术方案支撑）**
- spec 的每个功能，plan 有对应的模块/接口吗？
- spec 的非功能需求（性能/兼容/安全），plan 有对策吗？
- spec 的"明确不做"，plan 有没有偷偷设计了？

**plan → tasks（方案有没有任务覆盖）**
- plan 的每个模块，tasks 有对应的实现任务吗？
- plan 的每个接口，tasks 有"写这个接口"的任务吗？
- plan 的数据模型，tasks 有"建表/迁移"任务吗？

**tasks → spec（任务有没有覆盖需求）**
- tasks 全做完，spec 的所有功能 DoD 都能满足吗？
- 有没有 spec 要的功能，tasks 里一条都没有？

**互相矛盾**
- spec 说"实时更新"，plan 没提实时机制？
- plan 说"用 SQLite"，tasks 却有"配置 Redis 连接"？
- tasks 有任务依赖了不存在的文件/模块？

### 3. 产出检查报告

在 `.agents/specs/{feature}/analysis.md` 写入：

```markdown
# 一致性检查报告

## 严重问题（必须修）
- [矛盾/遗漏] {描述} → 建议改 {哪个文档的哪部分}

## 次要问题（建议修）
- ...

## 通过项
- spec 的 N 个功能均有 plan 支撑
- plan 的 N 个模块均有 tasks 覆盖
```

### 4. 报告给用户，修复后进 implement

把问题列给用户，逐条确认怎么改。改完后：
> 一致性已对齐。下一步进入 `implement` 照 tasks.md 实现。要开始吗？

若**无问题**：
> 一致性检查通过，无冲突遗漏。可以进入 `implement`。要开始吗？

## 坑点清单（Gotchas）

- **只读不比对**：三份都读了但没交叉对照 = 白跑。每条检查都要跨文档追。
- **漏查非功能需求**：只查功能，漏了 spec 里的性能/安全要求 plan 有没有对策。
- **不产出报告**：只在对话里说"没问题"，没存 analysis.md，无法追溯。
- **发现问题不修就往下走**：发现了矛盾却急着 implement，返工更大。

## 关键规则

- **必须**逐维度交叉比对（spec↔plan↔tasks），不要只读不比。
- **必须**产出 analysis.md 存档。
- 发现的严重问题**必须**修完才进 implement。
- 无问题时也要明确说"通过"，不要跳过。

## 参考

- 上游 skill：`tasks`（任务拆解）
- 下游 skill：`implement`（实现）
