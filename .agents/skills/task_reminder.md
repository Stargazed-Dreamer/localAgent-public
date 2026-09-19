---
name: task_reminder
description: >
  周期性任务提醒与待办汇总。触发词：有什么任务、该做什么、提醒我、任务清单、
  待办清单、check tasks、what to do、今天做啥、这周做啥、有什么没做、任务提醒。
  当用户询问有哪些任务、需要提醒、查询待办、或会话开始想了解当前该做什么时触发。
  另覆盖机会型跑批：用户说"额度很多/额度有余/额外额度/长期自动化/顺便跑/能跑什么"
  等表示空闲额度时，按 docs/periodic-task-inventory.md 台账逐项核对上次活动时间后提议。
  通过 /todos/due 检查周期性任务是否到期，通过 /wip 汇总未完成待办。
task_type: system.task_reminder
---

# 任务提醒 (task_reminder)

## 触发词
有什么任务、该做什么、提醒我、任务清单、待办清单、check tasks、what to do、今天做啥、这周做啥、有什么没做、任务提醒、提醒
空闲额度类（走第 4 节自查）：额度很多、额度有余、额度多余、额外额度、空闲额度、长期自动化、顺便跑、能跑什么

## 概述
通过待办模块（`/todos` API）管理周期性任务。当用户询问"有什么任务"时，检查哪些任务到期，同时汇总 WIP 待办，给出一份"当前该做的事"的综合报告。

> **数据源已迁移**：旧记忆 key `task_reminders` 已迁移到 `todos` 表（`server/todos/`），旧 key 保留作备份。所有读写改用 `/todos` API。

## 核心原则

1. **时间感知**：所有提醒基于"是否到期"判断，不是无脑列出所有任务
2. **频率尊重**：daily=1天周期，weekly=7天周期，monthly=自然月（如下月同日，月末兜底），到期才提醒
3. **待办联动**：周期任务 + WIP 待办 一起汇报，不遗漏断头工作
4. **完成记录**：用户确认完成后，调 `todos_mark_done` 更新 `last_done_at` + 计算 `next_due_at`
5. **条件任务**：如"找工作期间"等有生效条件的任务，先判断条件是否满足

## 待办表结构（todos 表）

每条周期任务对应 `todos` 表的一行：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 唯一标识（通常与 skill 名一致） |
| title | string | 简短标题 |
| skill | string | 关联的 skill 名称 |
| type | string | `recurring`（周期任务）/ `phased_recurring`（阶段性周期）/ `triggered`（触发式）；一次性任务请用 `wip_create` |
| status | string | `pending`（待执行）/ `archived`（phased_recurring 超出 end_date 后归档） |
| frequency | string | `daily` / `weekly` / `monthly`（recurring 和 phased_recurring 必填） |
| last_done_at | string\|null | 上次完成日期（YYYY-MM-DD），null=从未完成 |
| next_due_at | string\|null | 下次到期日期（mark_done 时自动计算；triggered 触发时置 today） |
| condition | string\|null | 生效条件描述，如"找工作期间"。null=无条件 |
| notes | string | 补充说明 |
| start_date | string\|null | phased_recurring 起始日期（该类型必填） |
| end_date | string\|null | phased_recurring 结束日期（超出后自动 archived；该类型必填） |
| trigger_condition | string\|null | triggered 类型的触发条件 JSON（该类型必填），如 `{"event":"file_arrived","watch_dir":"E:/path","pattern":"*.csv"}` |

## 工作流

### 1. 查询任务（用户问"有什么任务"时）

1. **获取到期任务**：`GET /todos/due`（MCP: `todos_due`）— 返回所有到期的周期任务
2. **获取 WIP 待办**：`GET /wip`（MCP: `wip_list`）— 返回所有未完成的 WIP 任务
3. **汇总报告**：

**报告格式：**

```
## 当前任务清单（{日期}）

### 🔔 到期的周期任务
- [每周] 记账（上次——{日期或"从未"}，已过 {N} 天）
  - 说明：每周导出微信/支付宝账单审核
- [每月] 异环抽卡记录（上次——{日期或"从未"}，已过 {N} 天）
  - 说明：增量采集异环抽卡记录

### ⏳ 未到期的周期任务（仅供参考，不强制）
- [每周] 文档同步（上次——{日期}，还剩 {N} 天）

### 📋 未完成待办（来自 wip_tracker）
- {标题}（进度 {N}%，状态——{status}）
  - 下一步——{next_steps[0]}
- ...

### 💡 建议
- 优先处理到期的周期任务
- {根据待办紧急程度给出建议}
```

### 2. 标记任务完成

当用户完成某项周期任务后（用户说"记账做完了"或 agent 执行完任务）：

1. 调用 `POST /todos/{todo_id}/done`（MCP 网关: `localagent_advanced_tool(todos_mark_done)`）
2. 系统自动更新 `last_done_at` 为今天 + 计算 `next_due_at`（daily+1/weekly+7/monthly=下月同日，月末兜底如 1月31日→2月28日）
3. 简短确认："已记录 {任务名} 完成，下次提醒时间 {next_due_at}"

### 3. 增减周期任务

**新增**：`POST /todos`（MCP 网关: `localagent_advanced_tool(todos_create)`）

```json
{
  "title": "任务标题",
  "skill": "skill_name",
  "type": "recurring",
  "frequency": "weekly",
  "condition": null,
  "notes": "说明"
}
```

**删除**：`DELETE /todos/{todo_id}`（MCP 网关: `localagent_advanced_tool(todos_delete)`）

### 4. 空闲额度自查（机会型跑批）

用户说"我现在额度很多 / 想跑一下长期自动化 / 有什么可以顺便跑的"时，**不要凭印象回答，也不要现扫 manifest**：

1. 读 `docs/periodic-task-inventory.md` 的「空闲额度自查流程」台账（真源），取每项的阈值与"上次活动"核对信号
2. 逐项实测比较：文件 mtime / `GET /loop/tasks` 的 `last_run_at` / `GET /inbox?status=new` / 报告文件名日期
3. 一次性报告**超期项**，每项附一句"跑它要什么、大概多久、有什么风险"，**等用户点单才执行**
4. 有意不注册提醒的项（如 `official_gacha` 因风控）只作为"有空可跑"候选提出，不得自动跑
5. 跑完按第 2 节 `todos_mark_done` 回写对应 todo（有 todo 的那些）

> 判据说明：这类任务没有系统级到期机制——`frequency` 只有 `daily/weekly/monthly/quarterly`，`condition` 的自动判定依赖 `todos_trigger_check`（已注册但无实例），所以"额度有余才提"只能由 agent 在被问时现场核对。

## 当前周期任务清单

当前所有周期任务存储于 `todos` 表中，**请调用 `todos_list` 获取最新清单**，不要依赖任何硬编码快照（任务会随增删变化）。

## 判断条件任务是否生效

对于带 `condition` 的任务（如"找工作期间"），判断方式：

1. **优先询问用户**：如果不确定，直接问"你现在还在找工作期间吗？"
2. **根据上下文**：如果近期对话中提到面试、投简历等，视为生效
3. **默认不生效**：如果无法判断且用户未提及，跳过该任务并在报告中注明"条件未确认"

> `/todos/due` 返回的条件任务会带 `condition_check_needed: true` 标记。

### 条件任务状态回写

当用户明确表示条件已失效（如"我不找工作了"）时，调用 `todos_update` 设置 `condition_status: "inactive"`，该任务将不再出现在 `todos_due` 中。若条件重新生效（如"又开始找工作了"），设置 `condition_status: "active"` 恢复提醒。

## 与 wip_tracker 的关系

| 维度 | task_reminder | wip_tracker |
|------|---------------|-------------|
| 关注 | 周期性、重复执行的任务 | 一次性、断头的工作 |
| 触发 | 用户问"有什么任务" | 用户说"留档"/"继续之前的工作" |
| 存储 | `todos` 表 | `wip_tasks` 表 |
| 完成 | `todos_mark_done`（任务不消失，等下次到期） | `wip_update`（status→completed，保留记录） |

**查询任务时两者都要看**：周期任务（`todos_due`）+ 一次性待办（`wip_list`）。

## 规则

- **到期判断由后端完成**：`/todos/due` 已计算好到期状态，agent 只需调用
- **完成记录必须及时**：用户说做完了，立即调 `todos_mark_done`
- **不重复提醒**：未到期的任务只在"仅供参考"区域列出，不强调
- **条件任务谨慎**：不确定条件是否满足时，宁可问一句也不要瞎提醒
- **报告要可执行**：每个到期任务附带对应的 skill 名和简短说明，用户能直接说"做记账"触发

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 待办模块 | `todos_due` / `wip_list` | MCP 直连，获取到期任务和 WIP |
| 待办模块 | `localagent_advanced_tool(todos_create/todos_mark_done/todos_delete)` | MCP 网关，增删改任务 |
| WIP 追踪 | `wip_tracker` skill | 提供一次性待办清单 |
