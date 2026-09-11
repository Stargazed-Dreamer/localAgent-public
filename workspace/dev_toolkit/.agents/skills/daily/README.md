# 日常事务类 Skill 桶（daily/）

非代码工作流的 skill：思维工具、学习教学、计划梳理、审问式决策等。

## 分类规则

- 不涉及代码编写的通用工作流工具归此桶
- 与"开发类"（dev/）的区别：产出是思维制品（计划、决策、教学记录）而非代码

## 来源

部分 skill 从 [mattpocock/skills](https://github.com/mattpocock/skills) 中文版（vinvcn fork）改造接入，原版参考见 `_vendor/mattpocock-zh/skills/productivity/`。

## 当前 skill 列表

### 持久化工作区类

- `teach`：教学工作区（多 session 状态化）
- `cangjie_extraction`：蒸馏长内容为方法论 skill（RIA-TV++ 流水线）

### 思维工具集（12 个通用思维 Prompt）

来源：文章[《都 Agent 时代了，我还是想分享给你这 12 个我最常用的 Prompt》](https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ)（作者：数字生命卡兹克，2026-08-21 由用户用 `web_archive` 抓取后整合）。

12 个 Prompt 分 5 个场景，其中 2 个已整合到现有 skill（横纵分析法→`adhoc.deep_research`、人生设计术→`recurring.life_design`），其余 10 个+用户自定义的 1 个组合版共 11 个独立 skill：

| # | skill | 场景 | 说明 |
|---|---|---|---|
| 1 | `socratic_questioning` | 问清问题 | 苏格拉底式问诊，最多 6 问找到真正值得回答的问题 |
| 2 | `dual_layer_explanation` | 学习 | 双层解释法：小白版+专家版+检查问题 |
| 3 | `reverse_decomposition` | 学习 | 反向拆解优秀作品：5 项分析+规律+操作清单+小练习 |
| 4 | `fact_checking` | 学习 | 事实核查：三层拆解+联网核查 5 档+推理链 5 项漏洞 |
| 5 | `expert_consultation` | 解决问题 | 专家会诊：3 互补视角+互相质疑+综合方案 |
| 6 | `first_principles` | 解决问题 | 第一性原理：拆 4 层+重新推导路径 |
| 7 | `cross_domain_borrowing` | 解决问题 | 跨领域借解：剥术语+3 个远领域+翻译方案 |
| 8 | `steel_man_decision` | 决策 | 双向钢人论证：双方最强论证+只问一个关键问题 |
| 9 | `minimal_experiment` | 决策 | 用最小实验替代空想：低成本可逆 7 天实验 |
| 10 | `talent_mining` | 认识自己 | 挖掘隐藏天赋：10 问产出万字天赋说明书 |
| 11 | `decision_protocol` | 决策 | 重大决策协议（预烘焙组合：对齐+钢人+执行纪律） |

每个 skill 目录结构：`SKILL.md`（路由元数据+工作流+兄弟工具）+ `prompt.md`（文章原文 Prompt 逐字保存，不改写）。

---

## 思维工具组合哲学（必读）

**这些 skill 是积木，不是流水线。** 任何复杂问题都可能需要 2-5 个工具轮番上阵或融合使用。以下原则取代任何固定组合配方：

### 1. 发散优先

遇到复杂思考任务，先扫一遍本桶所有工具，把"看起来相关"的都掏出来，宁可多带不要漏带。

### 2. 组合方式不固定

可串行（A→B→C）、并行（A+B 同时用）、迭代（A→B→回到 A 深挖）、嵌套（A 的某步内部调 B）。

### 3. 举例永远是局限的

本文件给的任何组合示例都是为了说明思路，不是配方。agent 必须基于问题特征自行判断。

### 4. 拿不准就让用户选

当 agent 不确定该用哪几个、怎么组合时，把候选工具+组合方式列给用户，让用户拍板。

### 5. decision_protocol 是预烘焙的组合

它是"对齐+钢人+执行纪律"的固定套餐，适合重大决策走完整流程。但 agent 也可以不走它，自行组合更贴合场景的工具。

### 6. 单工具也行

简单问题用一个工具就够，不要为了组合而组合。

### 工具间关系（发散式，非树状）

```
问清问题层:  socratic_questioning ←→ (任何工具的前置)
学习层:      dual_layer_explanation, reverse_decomposition, fact_checking
             ↑ 互相可串联（先拆解→再双层解释→再核查事实）
解决问题层:   expert_consultation, first_principles, cross_domain_borrowing
             ↑ 三者互补（专家多视角 / 拆本质 / 跨领域借解）
决策层:       steel_man_decision, minimal_experiment, decision_protocol
             ↑ decision_protocol 是预烘焙套餐；另两个可独立或组合
自我认知层:   talent_mining (向后看), recurring.life_design (向前看)
             ↑ 天赋挖掘+人生设计可前后衔接

跨层组合（发散，举例非穷尽）:
  socratic → first_principles → steel_man → minimal_experiment
  expert_consultation → cross_domain_borrowing → steel_man
  reverse_decomposition → dual_layer → fact_checking
  talent_mining → recurring.life_design → decision_protocol
  ... (agent 自行判断)
```

---

> 历史上的 `grill-me` / `grilling` / `grill-with-docs` 已于 2026-07-21 迁出到 `dev/`：三者主要用于开发决策压力测试，归入 dev 桶更合适。
> 注意：`dev.grill_me` 与 `daily.steel_man_decision` / `daily.decision_protocol` 可串联使用（grill-me 决策前拷问计划，钢人决策中二选一）。
