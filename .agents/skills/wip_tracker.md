---
name: wip_tracker
description: >
  未完成任务留档与追踪。触发词：留档、存档、标记未完成、WIP、断头工作、继续之前的工作、
  有哪些未完成、工作进度、记一下、别忘了、待办、TODO。当任务中断需要留档、查询未完成任务、
  用户提到之前做过一半的事情、或用户要求记录/备忘某事时触发。通过 /wip API 管理。
task_type: system.wip_tracking
---

# 未完成任务追踪 (wip_tracker)

## 触发词
留档、存档、标记未完成、WIP、断头工作、继续之前的工作、有哪些未完成、工作进度、之前做过的、记一下、别忘了、待办、TODO

## 概述
通过待办模块（`/wip` API）管理未完成工作（Work In Progress）的留档、查询和恢复。确保任何新 agent 接手时能快速了解目标、进度和下一步，避免重复探索。

> **数据源已迁移**：旧记忆 key `wip_index` 和 `.agents/wip/*.json` 文件已迁移到 `wip_tasks` 表（`server/todos/`），迁移后 .json 文件移至 `temp/wip_migrated_backup/` 作备份。
>
> **关于 `.agents/wip/*.md`**：这些 Markdown 文件是迁移前与 .json 双写的人类可读副本，仅作阅读参考。**DB（`wip_tasks` 表）是程序唯一真源**，所有读写必须通过 `/wip` API，不要直接读写 `.md` 文件。新增留档一律 `wip_create`，不再产生 `.md` 文件。

## 核心原则

1. **新 agent 零成本上手**：留档内容必须自包含，不需要翻历史对话
2. **目标-过程-结论三段式**：每份留档必须说清"要做什么"、"做了什么"、"发现了什么"
3. **可检索**：通过 `wip_list` 查询时能找齐所有断头工作
4. **不丢不重**：完成时标记关闭，重复提需求时能识别已有探索

## WIP 任务表结构（wip_tasks 表）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | string | 是 | 唯一标识（自动生成 `wip_xxxx` 或自定义） |
| title | string | 是 | 简短标题 |
| status | string | 是 | `active` / `paused` / `blocked` / `completed` |
| priority | string | 是 | `high` / `medium` / `low` |
| goal | string | 是 | 一句话说清目标 |
| progress | int | 是 | 进度百分比 0-100 |
| tags | list | 否 | 搜索标签（JSON 数组） |
| next_steps | list | 是 | 下一步具体操作（JSON 数组，可执行级别） |
| related_skills | list | 否 | 关联的 Skill 名称（JSON 数组） |
| related_files | list | 否 | 关联的输出文件路径（JSON 数组） |
| current_state | object | 否 | 当前状态快照（JSON 对象） |
| blocked_reason | string | 否 | 当 status=blocked 时，说明阻塞原因 |
| extra_data | object | 否 | 富字段（JSON 对象），存储非标准字段（如软件清单、统计报告等迁移前的完整上下文） |

### status 含义

| 状态 | 含义 |
|------|------|
| active | 正在进行中 |
| paused | 暂停，随时可恢复 |
| blocked | 被外部条件阻塞（如等用户确认、等 API） |
| completed | 已完成，记录保留作历史 |

## 工作流

### 留档（任务中断时）

1. 调用 `POST /wip`（MCP 网关: `localagent_advanced_tool(wip_create)`）：

```json
{
  "title": "四游戏卡池数据获取",
  "priority": "medium",
  "goal": "获取四个游戏的完整卡池信息：名称、时间段、UP干员",
  "progress": 40,
  "tags": ["明日方舟", "鸣潮", "终末地", "异环", "卡池"],
  "next_steps": [
    "明日方舟：解析PRTS主卡池一览页面获取完整历史",
    "鸣潮：从日文WikiWiki抓取完整集音履歴"
  ],
  "related_skills": ["arknights_gacha", "wuwa_gacha", "endfield_gacha", "yihuan_gacha"],
  "related_files": ["workspace/_shared/all_game_pool_registry.json"],
  "current_state": {"arknights": "done", "wuwa": "in_progress"}
}
```

2. 告知用户已留档，返回任务 ID

### 查询（用户问"有哪些未完成"时）

1. 调用 `GET /wip`（MCP: `wip_list`）— 一次获取所有 WIP 任务
2. 可选按状态过滤：`GET /wip?status=active`
3. 汇总报告：每个任务的标题、进度、状态、下一步

### 恢复（用户说"继续之前的工作"时）

1. 调用 `wip_list` 获取所有 WIP 任务
2. 调用 `GET /wip/{task_id}`（MCP 网关: `localagent_advanced_tool(wip_get)`）获取详情
3. 从 `next_steps` 继续执行

### 去重（用户重复提需求时）

1. 调用 `wip_list` 检查是否有 tags 匹配的已有任务
2. 如果有，告知用户已有探索并展示进度
3. 询问是继续还是重新开始

### 更新进度

调用 `PUT /wip/{task_id}`（MCP 网关: `localagent_advanced_tool(wip_update)`）：

```json
{
  "progress": 60,
  "next_steps": ["新的下一步1", "新的下一步2"],
  "current_state": {"arknights": "done", "wuwa": "done", "endfield": "in_progress"}
}
```

### 完成（任务完成时）

1. 调用 `wip_update`，设置 `status: "completed"`, `progress: 100`
2. 记录保留在 `wip_tasks` 表中作历史

## 示例

```json
{
  "id": "wip_a1b2c3d4",
  "title": "四游戏卡池数据获取",
  "status": "paused",
  "priority": "medium",
  "goal": "获取四个游戏（明日方舟/鸣潮/终末地/异环）的完整卡池信息",
  "progress": 40,
  "tags": ["明日方舟", "鸣潮", "终末地", "异环", "卡池", "Wiki"],
  "next_steps": [
    "明日方舟：解析PRTS主卡池一览页面获取完整历史",
    "鸣潮：从日文WikiWiki抓取完整集音履歴",
    "终末地/异环：随版本更新自然增长"
  ],
  "related_skills": ["arknights_gacha", "wuwa_gacha", "endfield_gacha", "yihuan_gacha"],
  "related_files": ["workspace/_shared/all_game_pool_registry.json"],
  "current_state": {"arknights": "done", "wuwa": "in_progress"}
}
```

## 规则

- **留档必须及时**：任务中断、会话结束前、用户说"先这样"时，立即留档
- **结构化数据要完整**：`next_steps` 必须是可执行数组，`current_state` 是状态快照对象
- **completed WIP 处理**：completed 的任务默认保留在表中作为历史记录；当 WIP 表膨胀影响查询时，用户可触发 `wip_archive` skill 批量归档（提炼经验写入记忆 + 物理删除记录），见 `.agents/skills/wip_archive.md`
- **ID 要语义化**：用任务内容命名，或用系统生成的 `wip_xxxx`

## 与 task_reminder 的关系

| 维度 | task_reminder | wip_tracker |
|------|---------------|-------------|
| 关注 | 周期性、重复执行的任务 | 一次性、断头的工作 |
| 触发 | 用户问"有什么任务" | 用户说"留档"/"继续之前的工作" |
| 存储 | `todos` 表 | `wip_tasks` 表 |
| 完成 | `todos_mark_done`（任务不消失） | `wip_update`（status→completed） |

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 待办模块 | `wip_list` | MCP 直连，获取所有 WIP 任务 |
| 待办模块 | `localagent_advanced_tool(wip_create/wip_get/wip_update/wip_delete)` | MCP 网关，CRUD 操作 |
