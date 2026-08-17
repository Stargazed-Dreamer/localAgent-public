---
name: to-tickets
description: >
  把 plan、spec 或当前对话拆成一组 tracer-bullet tickets，每个 ticket 声明 blocking edges。
  当用户说"拆 tickets"、"拆成任务"、"to-tickets"、"把 spec 拆开"、"拆分实现步骤"时使用。
  适用于 spec 写完后拆分成可执行的任务单元。触发词：to-tickets、拆 tickets、拆任务、
  tracer bullet、blocking edges、任务拆分、实现步骤拆分。
task_type: dev.to_tickets
---

# To Tickets (to-tickets)

把 plan、spec 或 conversation 拆成一组 **tickets**：tracer-bullet vertical slices，每个 ticket 都声明 **block** 它的 tickets。

## 改造说明（原版 → 本版）

原版（mattpocock/skills）把 tickets 发布到 issue tracker（GitHub Issues / Linear 等），用 native blocking links 或 local files（`.scratch/<feature>/issues/`）。本版改造为：

- **ticket 存储**：`temp/sdd/<feature-slug>/tickets.md`（单文件汇总所有 ticket，与 spec.md / design-decisions.md / checklist.md 同目录）
- **不写 wip 记录**：不调 `wip_create`，tickets.md 的 blocked_by 用 ticket 编号引用（如 "Blocked by: Ticket 01"），不依赖 wip_id
- **frontier 查询**：agent 直接读 tickets.md，按依赖图找当前可执行的 ticket（blockers 都完成的）
- **无需配置**：不依赖外部 issue tracker，也不依赖 wip_tasks 表

## Process

### 1. Gather context

使用 conversation context 中已经存在的内容。如果用户把 spec 路径作为参数传入，获取并完整读取 spec 文件（路径形如 `temp/sdd/<feature-slug>/spec.md`）。

### 2. Explore the codebase (optional)

如果还没有探索 codebase，先了解 code 当前状态。Ticket title 和 description 应使用项目 domain glossary vocabulary（见 `dev/domain-modeling`），并遵守相关 ADRs（`docs/adr/`）。

寻找 prefactor code、让 implementation 更容易的机会。"Make the change easy, then make the easy change."

### 3. Draft vertical slices

把工作拆成 **tracer bullet** tickets。

<vertical-slice-rules>

- 每个 slice 都要贯穿每一层（schema、API、UI、tests）形成窄而完整的路径；必须是 vertical slice，不是某一层的 horizontal slice
- 完成的 slice 可独立 demo 或 verify
- 每个 slice 的大小必须能放进一个 fresh context window
- 任何 prefactoring 都应先完成

</vertical-slice-rules>

为每个 ticket 给出 **blocking edges**：它开始前必须完成的其他 tickets。没有 blockers 的 ticket 可以立即开始。

**Wide refactors 是 vertical slicing 的例外。** **Wide refactor** 是一个影响整个 codebase 的 mechanical change，例如 rename column 或 retype shared symbol；一次 edit 会破坏成千上万 call sites，无法让任何 vertical slice 独立保持 green。不要强行做成 tracer bullet；应按 **expand–contract** 排序。先 expand：在旧形式旁加入新形式，保持一切正常。再按 blast radius 分批迁移 call sites（按 package、directory 等），每批一个 ticket，并被 expand block；旧形式仍存在，因此 CI 每批都保持 green。最后 contract：在所有 migrate batches block 的 ticket 中删除旧形式。

### 4. Quiz the user

把建议的拆分作为 numbered list 展示。每个 ticket 包含：

- **Title**：简短的描述性名称
- **Blocked by**：必须先完成的其他 tickets（如有）
- **What it delivers**：这个 ticket 打通的 end-to-end behaviour

询问用户：

- Granularity 是否合适（太粗或太细）？
- Blocking edges 是否正确，每个 ticket 是否只依赖真正 gate 它的 tickets？
- 是否应继续合并或拆分 tickets？

迭代到用户批准拆分。

### 5. Publish the tickets

发布已批准的 tickets：

1. 在 `temp/sdd/<feature-slug>/tickets.md` 单文件中汇总所有 ticket，按 dependency order（blockers 优先）从 `01` 编号。每个 ticket 用 `## Ticket NN — <title>` 二级标题分隔。

2. **不调 `wip_create`**。tickets.md 文件本身是真源，implement skill 直接从路径读取，按依赖图找 frontier ticket。

## ticket 文件模板（tickets.md 内每个 ticket 的格式）

```markdown
## Ticket NN — <Ticket title>

**What to build:** 这个 ticket 从用户视角打通的 end-to-end behaviour，而不是逐层 implementation list。

**Blocked by:** gate 这个 ticket 的 numbers/titles，或 "None — can start immediately"。

**Status:** ready-for-agent

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2
```

## 关键规则

- **vertical slice**：每个 ticket 贯穿所有层，不是 horizontal slicing
- **blocking edges 明确**：每个 ticket 声明 blocked_by（用 ticket 编号引用，如 "Ticket 01"），frontier = blockers 都 completed 的
- **不写具体 file paths**：ticket 是方案不是实现，file paths 很快过时
- **不写 wip 记录**：tickets.md 文件写完即完成，不调 `wip_create`；implement skill 直接从路径读取，按依赖图找 frontier

## 与其他 skill 的关系

- **上游**：`to-spec` 产出 spec → `to-tickets` 拆分；或直接从对话拆分
- **下游**：`implement` 从 tickets.md 读取，按依赖图找 frontier ticket 执行
- **查询**：直接读 `temp/sdd/<feature-slug>/tickets.md`
