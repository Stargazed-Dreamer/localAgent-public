---
name: wayfinder
description: >
  把单个 agent session 装不下的大块工作规划成 wip_tasks 上的调查议题共享地图，并一次
  解决一个议题，直到通往目标的路径清晰。当用户说"规划大任务"、"wayfinder"、"路径规划"、
  "这个太大了一次做不完"、"分多次 session 推进"时使用。适用于大型重构、跨模块迁移、
  多步决策类工作。触发词：wayfinder、路径规划、大任务规划、map、destination、fog of war、
  frontier、多 session 规划。
task_type: dev.wayfinder
---

# Wayfinder (wayfinder)

一个松散想法出现了：它太大，单个 agent session 装不下，而且被 fog 包围；从这里到 **destination** 的路还看不见。Wayfinding 的目标是找到这条路，而不是朝 destination 猛冲。这个 skill 会把路径绘制成 `wip_tasks` 表上的 **shared map**，然后一次处理一个 ticket，直到路线清晰。

不同 effort 的 destination 不同，而为它命名是 charting 的第一个动作；它塑造每个 ticket。它可能是一份要 hand off 并迭代的 spec、一个必须在 planning 前确定的 decision，或 data-structure migration 之类原地完成的 change。Map 与领域无关：engineering work、course content，或任何符合这个形状的事项都可以。

## 改造说明（原版 → 本版）

原版（mattpocock/skills）在 issue tracker 上创建 map issue（`wayfinder:map` label）+ child ticket issues（`wayfinder:<type>` label），用 native blocking edges。本版改造为：

- **map 存储**：`workspace/wayfinder/<map-slug>.md`（本地 markdown 文件）+ `wip_create(type=wayfinder_map)` 注册
- **ticket 存储**：`workspace/wayfinder/<map-slug>/tickets/<NN>-<slug>.md` + `wip_create(type=wayfinder_ticket)` 注册
- **ticket type**：`extra_data.ticket_type`（`research` / `prototype` / `grilling` / `task`）
- **blocking edges**：`extra_data.blocked_by`（依赖的 ticket wip_ids 列表）
- **frontier 查询**：`wip_list(type=wayfinder_ticket, status=active)` + 按 `blocked_by` 过滤
- **/grilling, /domain-modeling**：引用 `dev/grill-me` 和 `dev/domain-modeling` skill
- **/prototype**：引用 `dev/prototype`（UI 变体 / 状态机终端）或 `html-dev-debug` skill

## Plan, don't do

Wayfinder 默认用于 **planning**：每个 ticket 解决一个 decision；当别人动手前已经没有任何事情需要决定、路径完全清晰时，map 才算完成。想直接做工作的冲动通常表示你已经到达 map 边缘，该 hand off 了。Effort 可以在 **Notes** 中覆盖这个默认值，把 execution 纳入 map；否则只产出 decisions，不产出 deliverables。

## Refer by name

每张 map 和每个 ticket 都是 `wip_tasks` 表的一条记录，因此都有一个 **name**：它的 title。在所有给人看的内容里，包括叙述和 map 的 Decisions-so-far，都用 name 引用它，不要只写裸 wip_id 或 number。一堵 `#42, #43, #44` 很难读；name 一眼就能看懂。wip_id 不会消失，它们被包在 name 的引用里，但不单独替代 name。

## The Map

Map 是 `wip_tasks` 表中一条 `type=wayfinder_map` 的记录，是 canonical artifact。它的 tickets 是 map 的 child records（`type=wayfinder_ticket`，`extra_data.map_id` 指向 map 的 wip_id）。

Map 是 **index**，不是 store。它列出已经做出的 decisions，并指向保存细节的 tickets；一个 decision 只存在一个地方，也就是它的 ticket。因此 map 不复述细节，只给 gist 和 link。

### The map body（workspace/wayfinder/<map-slug>.md）

Map 是低分辨率的全局视图，每个 session 加载一次。Open tickets 不列在里面；它们是 open child tickets，通过 `wip_list` 查询找到。

```markdown
# <Map 标题>

**wip_id:** <map_wip_id>
**Status:** active

## Destination

<what reaching the end of this map looks like — the spec, decision, or change this effort is finding its way to. One or two lines; every session orients to it before choosing a ticket.>

## Notes

<domain; skills every session should consult; standing preferences for this effort>

## Decisions so far

<!-- the index — one line per closed ticket: enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [<closed ticket title>](workspace/wayfinder/<map-slug>/tickets/<NN>-<slug>.md) — <one-line gist of the answer>

## Not yet specified

<!-- see "Fog of war": in-scope fog you can't ticket yet; graduates as the frontier advances -->

## Out of scope

<!-- see "Out of scope": work ruled beyond the destination; closed, never graduates -->
```

### Tickets（workspace/wayfinder/<map-slug>/tickets/<NN>-<slug>.md）

每个 ticket 都是 map 的 **child record**（`wip_create(type=wayfinder_ticket, extra_data={map_id: <map_wip_id>})`）；wip_id 是它的 identity。Body 是一个问题，大小控制在一个 100K token agent session 内：

```markdown
# <NN> — <Ticket title>

**wip_id:** <ticket_wip_id>
**Type:** research | prototype | grilling | task
**Blocked by:** [<wip_id1>, <wip_id2>] 或 "None — can start immediately"
**Status:** ready-for-agent

## Question

<the decision or investigation this ticket resolves>
```

每个 ticket 的 `extra_data.ticket_type` 取值为 `research`、`prototype`、`grilling`、`task`（见 [Ticket Types](#ticket-types)）。

Session **claim** ticket 的方式，是在任何工作开始前先 `wip_update(wip_id=<id>, extra_data={claimed_by: "<session_id>", claimed_at: "<ISO>"})`。这个 claim 就是占用：open 且 unclaimed 的 ticket 才是可拿。

Blocking 使用 `extra_data.blocked_by`（依赖的 ticket wip_ids 列表）；一个 ticket 的所有 blockers 都 `status=completed` 后，它就是 **unblocked**；**frontier** 是 open、unblocked、unclaimed 的 children，也就是已知世界的边缘。

答案不写进 body，而是在 resolution 时记录（见 [Work through the map](#work-through-the-map)）。解决 ticket 时产生的 assets（prototype 代码、research summary）从 ticket 文件链接出去，不粘贴进 body。

## Ticket Types

每个 ticket 都是 **HITL**（human in the loop，与能代表自己发言的人类一起处理）或 **AFK**（agent 独立驱动）。HITL ticket 只能通过 live exchange 解决；agent 绝不能替人类回答。一旦 grilling agent 自问自答，它就已经坏了。

- **Research**（AFK）：阅读 documentation、third-party APIs，或 knowledge bases 等 local resources。创建 markdown summary 作为 linked asset。当需要当前 working directory 外的知识时使用。
- **Prototype**（HITL）：通过 cheap、rough、concrete artifact 提高讨论 fidelity，例如 outline、rough take、stub，或通过 `dev/prototype`（UI 变体 / 状态机终端）或 `html-dev-debug` skill 写 UI/logic code。Prototype 作为 asset 链接。当核心问题是 "how should it look" 或 "how should it behave" 时使用。
- **Grilling**（HITL）：通过 `dev/grill-me` 和 `dev/domain-modeling` skills 进行 conversation，一次问一个问题。默认类型。
- **Task**（HITL 或 AFK）：做出 *decision* 前必须完成、但本身没有要 decide、prototype 或 research 的 manual work。例如注册服务以评估其 API、配置访问权限、移动数据以看清 shape。这是唯一会 *do* 而不是 decide 的类型；它凭借解锁 decision 而存在，而不是交付 destination。Agent 能独立完成时采用 AFK，否则给人类精确 checklist（HITL）。工作完成后 resolved；答案记录做了什么，以及后续 tickets 依赖的事实（credentials location、new URLs、row counts 等）。

## Fog of war

Map 是 _有意_ 不完整的：不要描绘你还看不见的东西。Tickets 之外是 fog：那些你能感觉到以后会来的 decisions 和 investigations，但它们悬在仍未解决的问题之上，暂时还无法钉住。解决一个 ticket 会清掉它前方的一片 fog，把现在已经能说明的问题升级成新的 tickets；一次一个，直到通向目标的路清楚且没有 tickets 剩下。

Map 的 **Not yet specified** section 用来记录这种朦胧视野：怀疑中的问题、之后要回访的区域。这里是通往 destination、尚未探索的 frontier；所有内容都在 scope 内，只是还不够清晰，无法成为 ticket。可以按视野允许的粗细来写；它也是协作者阅读这个 effort 走向时的路标。

**Fog or ticket?** 测试标准是你现在能不能把问题说清楚，而不是现在能不能回答它。

- **Ticket when** 问题已经清晰，即使它被 blocked、现在不能处理。
- **Not yet specified when** 你还不能把它说得那么清楚。不要把 fog 预先切成 ticket-sized pieces：fog 比 ticket 粗，frontier 到达后，一片 fog 可能升级成多个 tickets，也可能一个都没有。

**Not yet specified** 排除已经决定的内容（Decisions so far）、已经是 live ticket 的内容，以及 out of scope 的内容。

## Out of scope

Fog 只会聚集在通往 destination 的方向。Destination 固定 scope，因此超出它的工作是 **out of scope**，不是 fog，也不属于 **Not yet specified**。它写进 map 单独的 **Out of scope** section：你有意识地排除在这个 effort 之外的工作。决定它属于这里的是 scope，而不是 sharpness。

Out-of-scope work 永远不会 graduate；frontier 会停在 destination。只有重画 destination 时它才会回来，而且应成为新的 effort，不是 resumption。

把某事排除出 scope 是 scoping act，不是 route 上的一步。如果已有 ticket 被发现位于 destination 之外，应 **close it**（`wip_update(status=completed, extra_data={closed_reason: "out_of_scope"})`），并在 **Out of scope** 中留一行 gist、原因和 closed ticket link。不要把它放进 **Decisions so far**；后者只记录真正走过的路线。

## Invocation

两种模式。无论哪种，**每个 session 绝不要 resolve 超过一个 ticket。**

### Chart the map

用户带着松散想法调用。

1. **Name the destination.** 运行 `dev/grill-me` 和 `dev/domain-modeling` session，确定 map 要找到的 spec、decision 或 change。Destination 固定 scope，所以先解决它。

2. **Map the frontier.** 再 grill 一次，这次采用 **breadth-first**：覆盖整个空间，而不是深入一条 thread，浮现 open decisions 和现在可开始的 first steps。**如果没有 fog**，说明路径已经清晰，整个 journey 一个 session 就能完成，你不需要 map。停止并询问用户如何继续。

3. **Create the map**：
   - 写 map body 到 `workspace/wayfinder/<map-slug>.md`（填好 Destination 和 Notes，Decisions-so-far 为空，把 fog 勾勒进 **Not yet specified**）
   - `wip_create(title="<map 标题>", type="wayfinder_map", extra_data={map_path: "workspace/wayfinder/<map-slug>.md", status: "active"})` 注册，返回 map_wip_id

4. **Create the tickets you can specify now** 作为 map 的 child records，然后第二遍再 wire blocking edges（tickets 需要 wip_ids 后才能互相引用）。Wiring 会把它们分成 frontier 和 blocked；现在还说不清的都留在 **Not yet specified**。
   - 每个 ticket：写文件到 `workspace/wayfinder/<map-slug>/tickets/<NN>-<slug>.md` + `wip_create(title=..., type="wayfinder_ticket", extra_data={map_id: map_wip_id, ticket_type: ..., blocked_by: [...], status: "ready-for-agent"})`

5. 停止。Charting the map 是一个 session 的工作；不要同时 resolve tickets。

### Work through the map

用户用 map（wip_id 或 `workspace/wayfinder/` 路径）调用。Ticket 是 **optional**；没有 ticket 时，你选择下一个 decision，而不是用户选择。

1. 加载 **map**：`wip_get(wip_id=<map_id>)` + 读 `workspace/wayfinder/<map-slug>.md`（低分辨率视图，而不是每个 ticket body）。

2. 选择 ticket。用户点名就用它；否则按顺序拿第一个 frontier ticket。**Claim it**：`wip_update(wip_id=<id>, extra_data={claimed_by: "<session_id>", claimed_at: "<ISO>"})`。

3. Resolve it：按需 **zoom**（`wip_get` + 读 ticket 文件），只在需要时获取相关或已关闭 ticket 的完整 body；调用 map `## Notes` block 提到的 skills。不确定时用 `dev/grill-me` 和 `dev/domain-modeling`。

4. 记录 resolution：把答案作为 **resolution comment** 追加到 ticket 文件，`wip_update(wip_id=<id>, status="completed", extra_data={resolved_at: "<ISO>", resolution_gist: "<一句话摘要>"})`，并向 map 文件的 Decisions-so-far 追加 context pointer（title + link + one-line gist）。

5. 添加新浮现的 tickets（create-then-wire）；把答案已经说清的 fog graduate 成 ticket，并从 map 文件的 **Not yet specified** 清掉每个已升级 patch，让它只作为新 ticket 存在。如果答案表明这个或其他 ticket 位于 destination 之外，将其 **rule out of scope**，而不是当作路线的一部分解决。如果这个 decision 使 map 其他部分失效，更新或删除那些 tickets（`wip_update(status=completed, extra_data={closed_reason: "invalidated_by:<ticket_id>"})`）。

用户可能并行运行 unblocked tickets，所以要预期其他 sessions 同时修改 `wip_tasks` 表。

## 关键规则

- **每个 session 最多 resolve 一个 ticket**：避免 context 污染和决策疲劳
- **Plan, don't do**：默认只产出 decisions，不产出 deliverables；想执行需在 Notes 中覆盖
- **Refer by name**：给人看的内容里用 title 引用，不写裸 wip_id
- **Fog or ticket 测试**：问题清晰就是 ticket，不清晰就是 fog
- **wip_create 注册**：map 和每个 ticket 都必须调 `wip_create` 注册，否则查询找不到
- **claim 机制**：开始工作前必须 `wip_update` claim ticket，避免多 session 冲突

## 与其他 skill 的关系

- **内部调用**：`dev/grill-me`（grilling ticket）+ `dev/domain-modeling`（术语对齐）+ `dev/prototype` 或 `html-dev-debug`（prototype ticket）
- **下游**：map 完成后可 hand off 给 `to-spec` + `to-tickets` + `implement` 执行具体工作
- **查询**：`wip_list(type=wayfinder_map)` 看所有 map；`wip_list(type=wayfinder_ticket, status=active)` 看所有 open ticket
