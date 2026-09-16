---
name: task_closure
description: >
  任务收尾 guide。触发词：收尾、任务结束、做完了、总结一下、这个任务完成了、
  收尾guide、task closure、结束了。当任务完成、中断、或会话结束前，agent 调
  agent_guide(task_type='system.task_closure') 获取统一收尾指引。整合 WIP 更新、
  经验提炼（含可消费性自检）、文档自查、收尾报告。不替代 memory_generation/
  neat-freak/wip_tracker，而是编排入口。
task_type: system.task_closure
---

# 任务收尾 (task_closure)

## 触发词
收尾、任务结束、做完了、总结一下、这个任务完成了、收尾guide、task closure、结束了

## 概述
任务结束时的统一收尾入口。**agent 自主触发**（无需用户显式要求），整合 WIP 处理、经验提炼、文档自查、收尾报告。

**第一性原理**：agent 自动总结经验，更好地执行任务。闭环 = 写入时约束可消费性 + 读取时强制消费。

## 与现有 skill 的关系（并存+编排）

| skill | 触发 | 职责 | 与 task_closure 关系 |
|-------|------|------|---------------------|
| task_closure（本 skill） | agent 自主/用户说"收尾" | 编排入口，整合 WIP/经验/文档/报告 | 入口 |
| memory_generation | 用户说"生成记忆" | 仅做经验提取+写入 | task_closure step_3-4 复用其逻辑 |
| neat-freak | 用户说"整理文档" | 完整文档审查（跨文件一致性/监控面板/测试） | task_closure step_5 只做本轮快速自查，完整审查由用户触发 |
| wip_tracker | 用户说"留档" | 仅做 WIP 留档/查询/恢复 | task_closure step_2 复用其逻辑 |

## task_closure_workflow（6 步）

### 步骤 1：评估任务状态（step_1_assess）

判断当前任务的状态，决定后续步骤集：

- **任务完成**：目标达成，有实质产出 → 走 step_2(wip关闭) → step_3-4(经验) → step_5(文档自查) → step_6
- **任务中断**：未完成但需要中断（用户切换话题/会话结束） → 走 step_2(wip留档) → step_5(文档自查) → step_6（跳过经验提取，因为还没做完）
- **无实质产出**：纯对话/简单查询，无可总结内容 → 直接 step_6（提示跳过，避免无意义记忆）

**判断标准**：
- 有代码/文档/数据产出？→ 完成 or 中断
- 用户明确说"做完了"/"先这样"/"换一个"？→ 完成 or 中断
- 只是回答问题/查资料？→ 无实质产出

**当前任务授权兜底收回**：
任务结束（完成或中断）时，若当前任务授权仍为 active，agent **必须**调 `screen_release_control` 主动收回，不把 300 秒自动撤销当正常收尾路径。
- 调用方式：`POST /screen/control/release`（operation_id=`screen_release_control`）
- 收回后：覆盖层隐藏、内存授权清除、授权 worker 停止
- 自动兜底：即使遗漏主动收回，最后一次成功控制操作后空闲 300 秒也会自动撤销

### 步骤 2：WIP 处理（step_2_wip）

**任务完成时**：
1. `wip_list` 检查是否有对应的 WIP 任务（按 tags 或 title 匹配）
2. 有则 `wip_update(id, {status: "completed", progress: 100})` 关闭
3. 无则跳过（不是所有任务都有 WIP）

**任务中断时**：
1. `wip_list` 检查是否已有对应 WIP
2. 有则 `wip_update` 更新 progress + next_steps + current_state
3. 无则 `wip_create` 创建新 WIP，必填：
   - `title`: 任务标题
   - `goal`: 一句话说清目标
   - `progress`: 当前进度百分比
   - `next_steps`: 下一步具体操作（可执行级别，数组）
   - `current_state`: 当前状态快照（dict）
   - `related_skills`: 关联的 skill 名
   - `related_files`: 关联的输出文件路径

### 步骤 3：经验提炼 + 可消费性自检（step_3_extract）

回顾当前任务，识别值得保留的经验候选：

**候选类型**：
- **preference**：用户偏好（如"用户喜欢简洁报告"）
- **project**：项目状态（如"记账脚本 v3 新增退款对冲"）
- **reference**：参考资料（如"ModelScope VL API 限流 RPM≈5"）

**不存储**（来自 memory_generation）：
- 代码结构（可 grep）、git 历史、已修复 bug、临时调试信息
- AGENTS.md / _index.md 中已记录的内容
- config.toml 中已配置的值

**可消费性自检**（核心约束，避免"存了没人用"）：

对每条候选记忆，**必须填写**：
- `consumption_contexts: list[str]`：哪些 task_type 应读取此记忆
  - 通用偏好用 `["*"]`（所有任务都该读）
  - scope 通配用 `["recurring.*"]`（所有周期任务该读）
  - 具体任务用 `["recurring.accounting", "adhoc.web_archive"]`
  - 纯 project 状态记忆填对应 skill 的 task_type（如 `["recurring.yihuan_gacha"]`）
- `trigger_keywords: list[str]`：任务描述/用户消息出现这些词时应读取
  - 如 `["退款", "账单对冲"]` / `["curl", "HTTP"]` / `["PaddleOCR", "bbox"]`

**软强制规则**：
- 无法明确消费场景的候选 → **跳过，不写入**（存了没人用 = 垃圾）
- 例外：纯 project 状态记忆，消费场景就是对应 skill 的 memory_key，可填 task_type 后写入
- agent 可显式跳过并说明理由（如"这是临时状态，无长期价值"）

### 步骤 4：查重 + 写入（step_4_dedup_write）

对每条通过自检的候选记忆：

1. `memory_list` 查看现有 key
2. 如果 key 已存在，`memory_get(key)` 读取现有内容
3. 比较新旧内容：
   - 只是更新 → 用 merge 模式覆盖
   - 完全重复 → 跳过
   - 新增信息 → 合并后写入
4. `memory_set(key, {data: <structured_value>, merge: true})` 写入

**结构化 value 格式**（扩展自 memory_generation）：

```json
{
  "type": "preference|project|reference",
  "name": "简短名称",
  "description": "一句话描述",
  "content": "实际内容/事实",
  "why": "为什么重要",
  "how_to_apply": "如何在未来应用",
  "consumption_contexts": ["recurring.accounting"],
  "trigger_keywords": ["退款", "账单对冲"],
  "created_at": "YYYY-MM-DD",
  "source_task": "触发本次记忆生成的任务名"
}
```

**key 命名规范**：
- 偏好：`preferences.{name}` 或沿用 `preferences`
- 项目：`project.{name}` 或沿用 skill 名（如 `accounting`）
- 参考：`reference.{name}`

### 步骤 5：文档自查（step_5_doc_sync）

**不询问用户**，agent 自行对照 `.agents/rules/project_rules.md` 检查清单，检查本轮变更是否需要文档同步：

**自查范围**（本轮变更相关）：
- [ ] 新增的路由是否在 `server/main.py` 中注册？
- [ ] 新增的 Pydantic 模型是否定义完整？
- [ ] 新增的接口是否在 `/health` 中反映状态？
- [ ] 是否有硬编码的路径/端口/密钥？→ 移入 `config.toml`
- [ ] `.agents/skills/_index.md` 是否需要同步？
- [ ] `AGENTS.md` 是否需要同步？
- [ ] `CHANGELOG.md` 是否已记录本轮变更？
- [ ] `config.example.toml` 是否需要新增模板？
- [ ] `tools_manifest.json` 是否需要更新？
- [ ] **浏览器经验记录**：本次是否操作了浏览器且遇到非显然行为？→ `browser_write_lesson` 写入 `sites/<domain>.md`
- [ ] **桌面软件经验记录**：本次是否操作了桌面软件且踩坑/发现最佳实践？→ `screen_write_lesson` 写入 `apps/<process_name>.md`

**处理方式**：
- 发现问题 → 直接修复（小改动）或报告用户（大改动）
- 无变更 → 跳过
- **完整的 neat-freak 审查**（跨文件一致性、监控面板、测试覆盖）由用户主动触发，task_closure 只做本轮快速自查

### 步骤 6：收尾报告（step_6_closure_report）

向用户报告收尾结果：

```
## 任务收尾报告

### 任务状态
[完成/中断/无实质产出]

### WIP 处理
- [更新了 WIP xxx / 创建了 WIP xxx / 无 WIP]

### 经验提炼
- 写入 N 条记忆：
  - {key1}: {summary1}（消费场景：{consumption_contexts1}）
  - {key2}: {summary2}（消费场景：{consumption_contexts2}）
- 跳过 M 条候选：
  - [原因1：无明确消费场景]
  - [原因2：重复已有记忆]

### 文档自查
- [无需同步 / 已同步：{文件列表} / 发现问题：{问题描述}]

### 下一步建议
[如有]
```

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| agent_guide | `agent_guide(task_type='system.task_closure')` | 获取本 workflow |
| WIP 模块 | `wip_list` / `wip_create` / `wip_update` | step_2 WIP 处理 |
| 记忆系统 | `memory_list` / `memory_get` / `memory_set` | step_3-4 经验提炼+写入 |
| 文档清单 | `.agents/rules/project_rules.md` | step_5 文档自查对照 |
| neat-freak | `.agents/skills/neat-freak.md` | 完整文档审查（用户主动触发） |

## 与 agent_guide 的关系

- `agent_guide(task_type='system.task_closure')` 返回 task_closure_workflow（6 步详细指引）
- `agent_guide()` 无参调用返回 `session_closure` 字段，提示"任务结束时调 task_closure"
- `agent_guide(task='收尾/做完了/...')` 关键词匹配到 task_closure

## 核心原则

1. **agent 自主触发**：任务结束时不等用户说"收尾"，agent 主动调 task_closure
2. **可消费性优先**：写入记忆前必须明确消费场景，否则跳过（避免存了没人用）
3. **文档自查不询问**：agent 自行对照检查清单，发现问题直接修或报告
4. **编排不替代**：task_closure 是入口，内部复用 memory_generation/wip_tracker 逻辑
5. **无实质产出跳过**：纯对话/简单查询不强制收尾，避免无意义记忆
