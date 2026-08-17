---
name: prototype
description: |
  在承诺方案前构建一次性原型来细化设计。根据问题在两个分支之间选择：用于状态或业务逻辑问题的可运行终端应用，或在同一路由中切换的多个显著不同 UI 变体。
  触发场景：用户想做原型、检查数据模型或状态机、模拟 UI、探索设计选项，或说 "prototype this"、"let me play with it"、"try a few designs"、"让我看看效果"、"试几种方案"。
  关键词：原型、prototype、试一下、看看效果、设计变体、UI 选项
task_type: dev.prototype
---

# Prototype（原型探索）

Prototype 是**用来回答一个问题的 throwaway code**。问题决定形状。

## Pick a branch

先识别正在回答哪个问题：来自用户 prompt、周围代码，或在用户在线时直接询问：

- **"Does this logic / state model feel right?"** → [LOGIC.md](references/LOGIC.md)。构建一个很小的交互式 terminal app，推动 state machine 跑过纸面上难以推理的 cases。
- **"What should this look like?"** → [UI.md](references/UI.md)。在单一路由上生成几种差异很大的 UI variations，并通过 URL search param 和浮动底栏切换。

这两个分支会产出非常不同的 artifacts；选错会浪费整个 prototype。如果问题确实模糊且用户不可达，默认选择更匹配周围代码的分支（backend module → logic；page 或 component → UI），并在 prototype 顶部说明假设。

## Rules that apply to both

1. **从第一天就是 throwaway，并明确标记。** Prototype code 要靠近它实际会被使用的位置（放在被 prototype 的 module 或 page 旁边），这样上下文清楚；但命名要让随手读代码的人看出它是 prototype，不是 production。对 throwaway UI routes，遵守项目现有 routing convention；不要发明新的顶层结构。
2. **一个命令即可运行。** 使用项目现有 task runner 支持的东西：`uv run python <path>`、`pnpm <name>`、`bun <path>` 等。用户必须能不动脑地启动它。
3. **默认不持久化。** State 保存在内存中。Persistence 是 prototype 要 _检查_ 的东西，不该成为依赖。如果问题明确涉及 database，就用 scratch DB 或带有清晰 "PROTOTYPE — wipe me" 名称的本地文件。
4. **跳过 polish。** 不写 tests，不做超过"能跑起来"所需的 error handling，不做 abstractions。重点是快速学到东西。
5. **暴露 state。** 每次 action（logic）或每次 variant switch（UI）后，打印或渲染完整相关 state，让用户看到发生了什么变化。
6. **完成后 capture。** 把验证过的 decision 折进真实 code，然后把 prototype 本身作为 **primary source** 保存到 `temp/prototype_<name>/`（或 throwaway git branch）。Main branch 只保留验证过的 decision，避免 prototype code 腐烂误导后续读者。

## 何时使用本 skill

| 场景 | 用 prototype | 不用 prototype |
|------|-------------|---------------|
| "我想看看这几种 layout 哪个对" | ✓ UI 分支 | — |
| "这 state machine 能处理 X then Y 吗" | ✓ LOGIC 分支 | — |
| "试一下这个数据模型能不能表达业务" | ✓ LOGIC 分支 | — |
| "我想直接实现一个完整功能" | — | 走 SDD 链（grill-me → spec → plan → tasks → implement） |
| "修个 bug" | — | 走 anti_hallucination 或 diagnosing-bugs |
| "重构现有代码" | — | 走 anti_hallucination 重构模式 |

## 与其他 skill 的关系

| Skill | 关系 |
|-------|------|
| `html-dev-debug` | UI 分支可与 `html-dev-debug` 配合：用 CDP 9222 调试浏览器驱动 prototype 页面、截图、抓 console 验证 variant 渲染 |
| `anti_hallucination` | prototype 完成后回归编码仍要走 anti_hallucination 防幻觉流程 |
| `dev/wayfinder` | wayfinder 的 prototype ticket 类型会路由到本 skill |
| `dev/tdd` | prototype 是"快速试错"，明确不走 tdd；prototype 验证过的 decision 折进真实 code 时，再用 tdd 写正式实现 |

## 项目内工具

- **LOGIC 分支终端执行**：用 `exec_python` 跑临时 TUI 脚本
- **UI 分支浏览器验证**：CDP 9222 调试端口（参考 `html-dev-debug` skill）
- **HTML 临时预览**：`temp/prototype_<name>/index.html` + `OpenPreview`
