---
name: wip-tracker
description: >
  当用户说"留档""存档""断头工作""继续之前的工作""有哪些未完成""WIP"或任务中断需要交接时使用。
  未完成任务追踪：留档、查询、恢复、去重。关键词：留档、存档、继续、未完成、中断、接手。
---

# wip-tracker (任务延续)

## 触发词
留档、存档、WIP、断头工作、继续之前的工作、有哪些未完成、接手、任务中断、上次做到哪了

## 概述
长任务跨多次会话时的延续机制。任务中断时创建留档（机器可读 JSON + 人类可读 MD），新 agent 接手时读留档从 `next_steps` 继续。让中断的任务能被无缝接手，不丢失上下文。

## 前置条件
- `.agents/wip/` 目录存在（不存在则创建）
- `.agents/memory/wip_index.json` 存在（不存在则创建为 `[]`）

## 目录结构

```
.agents/wip/
  {task_id}.json    # 机器可读（id/title/status/goal/progress/next_steps/tags）
  {task_id}.md      # 人类可读（目标/探索历程/经验教训/中期结论/下一步）
```

- `task_id` 命名规范：`wip_` + 6位随机 hex（如 `wip_a3f2c9`），保证唯一
- JSON 给 agent 读，MD 给人看，**两者必须同步**，不能只改一边

## 记忆索引

`.agents/memory/wip_index.json` 存所有任务的摘要列表：

```json
[
  {
    "id": "wip_a3f2c9",
    "title": "用户登录模块改造",
    "status": "in_progress",
    "progress": 60,
    "updated_at": "2026-07-12T10:30:00",
    "tags": ["auth", "backend"]
  }
]
```

新增/更新留档后必须同步更新此索引。

## 工作流

### 场景 A：留档（任务中断/会话结束前）

1. **生成 task_id**：`wip_` + 6位随机 hex
2. **创建 JSON 留档** `.agents/wip/{task_id}.json`：
   ```json
   {
     "id": "wip_a3f2c9",
     "title": "任务标题（一句话）",
     "status": "in_progress",
     "goal": "这个任务要达成什么目标（1-2句）",
     "progress": 60,
     "current_state": "当前做到哪了，已完成哪些步骤",
     "next_steps": [
       "下一步1（具体可执行）",
       "下一步2（具体可执行）"
     ],
     "tags": ["模块标签"],
     "related_files": ["相关源码路径"],
     "related_skills": ["相关 skill 名"],
     "related_memory_keys": ["相关记忆 key"],
     "blocked_reason": null,
     "created_at": "2026-07-12T10:00:00",
     "updated_at": "2026-07-12T10:30:00"
   }
   ```
3. **创建 MD 留档** `.agents/wip/{task_id}.md`：
   ```markdown
   # {任务标题}

   ## 目标
   这个任务要达成什么目标。

   ## 探索历程
   - 尝试了 A 方案，因为 X 问题放弃了
   - 改用 B 方案，目前进展到 Y

   ## 经验教训
   - [踩过的坑、发现的约束]

   ## 中期结论
   - [阶段性结论]

   ## 下一步
   1. [具体可执行的下一步]
   2. [具体可执行的下一步]
   ```

4. **更新索引** `.agents/memory/wip_index.json`：追加/更新任务条目
5. **向用户确认**："已留档 {task_id}，下次说'继续 wip_a3f2c9'即可恢复"

### 场景 B：查询（用户问"有哪些未完成"）

1. 读取 `.agents/memory/wip_index.json`
2. 按 `status` 分组汇总：
   ```
   ## 未完成任务（X 个）

   ### 进行中
   - wip_a3f2c9 — 用户登录模块改造（60%）— 下一步：实现 token 刷新
   - wip_b7e1d4 — 数据导出功能（30%）— 下一步：写 Excel 导出逻辑

   ### 阻塞中
   - wip_c2f8a1 — 支付集成（10%）— 阻塞原因：等第三方 SDK 文档

   ### 已完成（最近 7 天）
   - wip_d9a3e5 — 用户注册验证（100%）— 2026-07-10 完成
   ```
3. 询问用户要继续哪个，或开始新任务

### 场景 C：恢复（用户说"继续 wip_xxx" 或 "继续之前的工作"）

1. 如果用户指定了 task_id：直接读 `.agents/wip/{task_id}.json` 和 `.md`
2. 如果用户说"继续之前的工作"：
   - 读 `.agents/memory/wip_index.json`
   - 找最近更新的 `in_progress` 任务
   - 如果多个，列出让用户选
3. 读取留档后向用户复述：
   ```
   ## 恢复任务：{标题}

   - 目标：{goal}
   - 上次进度：{progress}% — {current_state}
   - 下一步：
     1. {next_steps[0]}
     2. {next_steps[1]}

   从第 1 步继续吗？
   ```
4. 用户确认后从 `next_steps[0]` 开始执行
5. 执行过程中持续更新 JSON 的 `current_state` 和 `progress`
6. 完成后更新 `status: completed`

### 场景 D：去重（用户提了和已有 WIP 相似的需求）

1. 读取 `.agents/memory/wip_index.json`
2. 用 `tags` 和 `title` 关键词匹配已有任务
3. 如果匹配到相似任务：
   ```
   这个需求和已有 WIP 可能相关：
   - wip_a3f2c9 — 用户登录模块改造（tags: auth, backend）

   要继续这个任务，还是创建新任务？
   ```
4. 用户决定后走场景 C（继续）或场景 A（新建留档）

## 坑点清单（Gotchas）

- **JSON 和 MD 不同步**：只改了 JSON 忘了改 MD，或反过来。**两者必须同步**，下次 agent 读到不一致的信息会混乱
- **next_steps 太抽象**：写"完成登录功能"是垃圾留档，agent 无法直接执行。必须写"在 `auth.py` 第 45 行的 `login()` 函数中添加 token 刷新逻辑"
- **忘了更新 wip_index.json**：留档文件创建了但索引没更新，用户问"有哪些未完成"时看不到
- **status 字段混乱**：只用 `in_progress` / `blocked` / `completed` 三种，不要自创状态
- **长期不清理**：>90 天的已完成任务留档堆积，neat-freak 时应归档或删除
- **blocked_reason 留空**：status=blocked 时必须填 blocked_reason，否则 agent 不知道在等什么

## 关键规则
- 必须**双格式留档**：JSON（机器读）+ MD（人读），缺一不可
- 必须**更新 wip_index.json**：留档后索引必须同步
- `next_steps` 必须**具体可执行**：写明改哪个文件、做什么、怎么验证
- 绝不**覆盖已有留档**：更新时先读后改再写
- 必须**向用户确认留档**：留档后告知 task_id 和恢复方式

## 不要做
- 不要把整个会话日志塞进 MD 留档 — 只提炼目标/历程/教训/结论/下一步
- 不要在 `next_steps` 写"完成所有剩余功能"这种废话 — 拆成具体步骤
- 不要创建 task_id 时不检查是否已存在同名 — 用随机 hex 保证唯一
- 不要在留档里写敏感信息（密码、密钥）— 留档可能进版本控制

## 输入/输出
- **输入**：当前任务上下文（留档时）或 task_id/索引（查询/恢复时）
- **输出**：`.agents/wip/{task_id}.json` + `.agents/wip/{task_id}.md` + 更新 `.agents/memory/wip_index.json`

## 依赖
| 依赖 | 路径 | 说明 |
|------|------|------|
| WIP 目录 | `.agents/wip/` | 留档文件存放 |
| 索引文件 | `.agents/memory/wip_index.json` | 任务摘要列表 |
| 记忆目录 | `.agents/memory/` | 相关功能记忆（如 feature_progress.json） |
