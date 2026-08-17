---
name: wip_archive
description: 归档已完成 WIP 到项目记忆（用户说"归档 WIP"/"清理已完成 WIP"/"把完成的 WIP 转记忆"时触发）。批量筛选 completed WIP → 提炼经验 → 写入结构化记忆 → 物理删除 WIP 记录。解决 WIP 表膨胀阻碍网页审查和 agent 查询。
task_type: system.wip_archive
---

# WIP 归档 (wip_archive)

将 `wip_tasks` 表中 `status='completed'` 的记录批量归档到项目记忆系统，然后物理删除记录。

## 何时触发

- 用户说"归档 WIP"/"清理已完成 WIP"/"把完成的 WIP 转记忆"
- WIP 数量过多（≥15 条）影响网页审查面板渲染或 `wip_list` 查询可读性时
- task_closure 完成后用户主动追加"把刚完成的归档掉"

## 工作流（4 步）

### step_1: 筛选 completed WIP

```
wip_list(summary=true)  # 只看摘要：id/title/status/priority/progress
```

筛选 `status='completed'` 的条目。若没有 completed 的，告知用户无需归档并结束。

> 注意 `wip_list` 直连 MCP 工具，免审批。但 `wip_get`/`wip_delete` 走 `localagent_advanced_tool` 网关。

### step_2: 逐条提炼经验

对每条 completed WIP：

1. `localagent_advanced_tool(tool="wip_get", params={"task_id": "<id>"})` 读详情
2. 评估可归档性：
   - **可归档**：含跨会话复用价值的项目状态、决策约束、踩坑经验、工程约定
   - **不归档**：一次性临时任务、已被代码/git 记录覆盖、AGENTS.md 已记录的内容
3. 合并相似 WIP：多条 WIP 经验主题重合时（如"VL 架构"+ "VL 弃用"）合并为一条记忆
4. 填两个字段（强制约束，缺失则跳过不写入）：
   - `consumption_contexts`: 哪些 task_type 应读取此记忆（具体如 `["recurring.accounting"]`，通用用 `["*"]`）
   - `trigger_keywords`: 任务描述/用户消息出现这些词时读取（如 `["退款", "curl"]`）

### step_3: 生成清单 → 用户审核 → 写入

**生成清单表格**（不直接写入，先让用户审核）：

| WIP ID | 拟写入 key | 类型 | consumption_contexts | trigger_keywords |
|--------|-----------|------|---------------------|------------------|
| wip_xxx | approval_system_behavior | project | ["*"] | ["approval", "exec_python"] |
| wip_yyy | llm_pool_sensitive | project | ["adhoc.*"] | ["privacy_warning", "vl"] |

**用户审核选项**：
- 整体批准 → 全部写入
- 逐条修改 → 调整 key/contexts/keywords 后写入
- 跳过某条 → 该 WIP 不写记忆但仍删除（用户认为无价值）

**写入**：
```
memory_set(key="<拟用 key>", data={...结构化记忆...}, merge=true)
```

- `merge=true`：同 key 已有记忆时合并而非覆盖
- key 不允许点号（`^[a-zA-Z0-9_-]+$`），用下划线
- 不加前缀（不写 `wip_archive_xxx`，直接用语义化 key）

### step_4: 删除已归档 WIP

写入记忆后，物理删除 WIP 记录：

```
localagent_advanced_tool(tool="wip_delete", params={"task_id": "<id>"})
```

返回 `success=true, status_code=200` 即删除成功。

> ⚠️ DELETE 方法的 `advanced_tool` 网关调用曾因 httpx `delete()` 不支持 `json` 参数触发 500（已于 2026-07-15 修复，见 CHANGELOG）。若再次出现 500，临时绕过：用 `exec_python` 调 `requests.delete(f'{API}/wip/{wid}')` 直接 HTTP 调用，并立即向用户报告 bug。

## 与其他 skill 的关系

| skill | 触发 | 职责 | WIP 处理 |
|-------|------|------|---------|
| **wip_tracker** | 用户说"留档" | 中断任务留档/查询/恢复 | 单条 CRUD，completed 保留在表 |
| **wip_archive**（本 skill） | 用户说"归档 WIP" | 批量 completed WIP → 记忆 | 批量物理删除 |
| **task_closure** | 任务完成/中断 | 收尾入口（WIP+经验+文档+报告） | step_2 单条关闭 WIP |
| **memory_generation** | 用户说"生成记忆" | 仅做经验提取+写入 | 不涉及 WIP |

**协调原则**：
- task_closure 是任务结束时的单条收尾（`wip_update(status=completed)`）
- wip_archive 是周期性的批量归档（`wip_delete`），用户在 WIP 堆积时主动触发
- wip_tracker 是日常 WIP 操作（留档/查询/恢复/更新）

## WipStatus 状态机（参考）

```
active ──暂停──> paused
   │                │
   │   ──恢复──>    │
   │                │
   └──阻塞──> blocked
   │
   └──完成──> completed ──wip_archive──> 物理删除（DELETE FROM wip_tasks）
```

无 `archived` 状态——归档 = 写入记忆 + 物理删除。

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 待办模块 | `wip_list`（MCP 直连，免审批） | 筛选 completed WIP |
| 待办模块 | `localagent_advanced_tool(wip_get)` （网关，GET 免审批） | 读 WIP 详情 |
| 记忆模块 | `memory_set`（MCP 直连） | 写入结构化记忆 |
| 待办模块 | `localagent_advanced_tool(wip_delete)`（网关，DELETE） | 物理删除 WIP 记录 |

## 注意事项

- **不归档未完成 WIP**：`status != 'completed'` 的 WIP 永远不归档（用户决策"先归档这条"例外，但默认拒绝并提示用 wip_update 改为 completed 后再归档）
- **用户审核是硬约束**：step_3 生成清单后必须等待用户明确批准，不擅自写入或删除
- **记忆消费闭环**：写入记忆必须填 `consumption_contexts` + `trigger_keywords`，下次调 `agent_guide(task_type='...')` 时后端自动返回匹配记忆
- **memory_maintainer 循环**：归档后的记忆由每 6h 运行的 memory_maintainer 老化+验证（这就是"慢慢跟着记忆循环走"的实现）
- **物理删除不可恢复**：WIP 删除后只能从记忆系统恢复关键经验，原始 next_steps/current_state/related_files 等结构化字段会丢失——归档前确认记忆已捕获关键信息

## 反模式

- ❌ 不审核直接批量写入+删除（违反"用户审核是硬约束"）
- ❌ 不填 `consumption_contexts`/`trigger_keywords` 就写入（违反可消费性自检）
- ❌ 给已完成但无价值的 WIP 强行造记忆（"存了没人用=垃圾"）
- ❌ 用 `exec_python` 发 HTTP 调 `wip_list`/`wip_get`/`wip_delete`（反模式，应走 MCP 工具或网关，见 AGENTS.md）
- ❌ 给 key 加 `wip_archive_` 前缀（key 应语义化，不加来源前缀）
