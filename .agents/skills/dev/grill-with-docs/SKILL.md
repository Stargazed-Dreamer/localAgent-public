---
name: grill-with-docs
description: >
  对计划或设计进行持续追问式访谈，并在过程中沉淀文档（ADR 和项目词汇表 CONTEXT.md）。
  当用户说"grill with docs"、"边拷问边记录"、"边追问边出 ADR"、"边打磨边沉淀术语"时使用。
  比 grill-me 更重，适合需要长期决策记录的场景。触发词：grill-with-docs、grill with docs、
  边追问边记录、边拷问边出 ADR、边打磨边沉淀文档。
task_type: dev.grill_with_docs
---

运行一次 grilling session（参见 `dev/grilling` skill，通过 agent_guide 路由加载），
同时使用 `dev/domain-modeling` skill（通过 agent_guide 路由加载）来沉淀 ADR 和项目词汇表。
