# 开发类 Skill 桶（dev/）

用于本项目代码开发的 skill：server 重构、client PySide6 面板开发、工具脚本编写等。

## 分类规则

- 涉及代码编写、测试、审查、调试、架构设计、规划拆分的 skill 归此桶
- 与"日常事务类"（daily/）的区别：产出是代码或代码相关制品（spec/tickets/架构文档）

## 来源

部分 skill 从 [mattpocock/skills](https://github.com/mattpocock/skills) 中文版（vinvcn fork）改造接入，原版参考见 `_vendor/mattpocock-zh/skills/engineering/`。

改造原则：
- 元数据格式对齐本项目（YAML frontmatter + task_type 字段）
- issue tracker 依赖改造为 `wip_tasks` 表 + `workspace/specs/` 目录
- 斜杠命令 `/name` 改造为 `agent_guide(task_type=...)` 路由
- 保留原版核心工作流和渐进披露结构（SKILL.md + references/）

## 当前 skill 列表

- `codebase-design`：架构设计原则（deep modules、seams、deletion test、design-it-twice）
- `domain-modeling`：领域建模、CONTEXT.md 词汇表、ADR
- `grill-me`：拷问计划入口，路由到 `grilling`（按用途归入 dev：开发决策压力测试）
- `grilling`：追问访谈核心逻辑（一次一个问题 + 推荐答案，design tree 逐个解决决策依赖）
- `grill-with-docs`：边拷问边沉淀 ADR + 项目词汇表（路由到 `grilling` + `domain-modeling`）
- `implement`：6 步实现 ticket 流程
- `prototype`：UI 变体 / 状态机终端原型探索
- `resolving-merge-conflicts`：git 合并冲突解决流程
- `tdd`：TDD 红绿循环
- `to-spec`：把讨论综合成 spec
- `to-tickets`：spec 拆 tickets（tracer-bullet 垂直切片）
- `triage`：issue 分诊状态机
- `wayfinder`：fog of war 寻路（建图 + 执行）
