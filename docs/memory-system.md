# 记忆系统详解
> 本文档是 `AGENTS.md` 中记忆系统的详细展开。日常使用原则见 AGENTS.md "会话启动检查清单"。

后端提供三层记忆系统（`/memory/*`），供 agent 跨会话保存任务状态和用户偏好。
旧 KV 记忆（`/memory/{key}`）只读兼容，新数据写入 SQLite（`data/memory/memory.db`）。旧 KV 目录已清理，迁移已完成。

## 三层架构

| 层级 | 存储 | 用途 | 查询方式 |
|------|------|------|----------|
| L1 Recent | 内存 deque + JSON | 近期对话滑动窗口 | 顺序读取 |
| L2 Time-indexed | SQLite | 按时间范围查询消息 | 时间线查询 |
| L3 Semantic | 向量索引 + BM25 倒排 | 语义检索历史记忆 | 混合搜索 |

## 语义检索

- **向量嵌入**：ONNX Runtime + `BAAI/bge-small-zh-v1.5`（本地推理，无需 API Key）
- **BM25 关键词**：jieba 中文分词 + Okapi BM25 评分
- **混合召回**：alpha 加权融合（`hybrid_alpha=0.5`，0=纯BM25，1=纯向量）
- **降级模式**：嵌入模型不可用时自动降级为 BM25-only

> **禁用语义检索**：在 `config.toml` 的 `[memory]` 段设置 `enable_semantic = false` 可完全跳过嵌入模型下载与加载。
> - **不受影响**：KV 存储（`/memory/{key}`）、时间线查询（`/memory/timeline`）、Recent 滑动窗口、记忆压缩、自动记录、BM25 关键词检索
> - **不可用**：向量语义搜索（`/memory/search` 退化为 BM25-only）、语义索引构建（`/memory/reindex` 跳过向量部分）
> - **适用场景**：网络受限无法下载模型、计划改用简单文本存储替代向量检索

## 记忆压缩

- 定期用 cheap 模型压缩旧消息为摘要（无 API Key 时用规则提取关键点）
- 压缩触发条件：未压缩消息数超过 `compress_threshold`（默认 200）
- 手动触发：`POST /memory/compress`

## 自动记录

- MCP 中间件自动记录每次工具调用到记忆系统
- 每 N 次交互（`maintenance_interval`，默认 10）触发一次维护（保存待写入、压缩、清理）
- 排除低价值工具（如 `memory_status`、`exec_status`）

## 使用原则

- **任务完成时写记忆**：每个 Skill 工作流结束后，将任务进度、关键结果写入记忆（如 `bilibili_gacha` 记录上次扫描时间）
- **会话开始时读记忆**：处理用户请求前，先 `GET /memory/{key}` 了解上次做到哪了
- **语义搜索**：用 `POST /memory/search` 按语义查找相关记忆，而非仅靠 key
- **记忆要清洗**：定期检查记忆是否过期，手动调用 `POST /memory/cleanup/orphaned` 清理超过 `orphan_cleanup_days`（默认30天）的已压缩消息和超过 `summary_cleanup_days`（默认180天）的旧摘要

## 记忆 key 规范

- 每个 Skill 对应一个 key，如 `bilibili_gacha`、`yihuan_gacha`、`accounting`
- 跨 Skill 的用户偏好用 `preferences` key
- key 只允许字母、数字、下划线、连字符

## API

| 路径 | 方法 | 说明 |
|------|------|------|
| `/memory/status` | GET | 记忆系统状态（含三层统计） |
| `/memory/list` | GET | 列出所有记忆 key |
| `/memory/{key}` | GET | 读取记忆（兼容旧 KV） |
| `/memory/{key}` | POST | 写入/更新记忆（深度合并） |
| `/memory/{key}` | DELETE | 删除记忆 |
| `/memory/search` | POST | 语义搜索（向量+BM25混合） |
| `/memory/timeline` | GET | 时间线查询 |
| `/memory/compress` | POST | 手动触发记忆压缩 |
| `/memory/record` | POST | 手动记录交互 |
| `/memory/stats` | GET | 详细记忆统计 |
| `/memory/reindex` | POST | 重建语义索引 |
| `/memory/cleanup/orphaned` | POST | 清理超过 orphan_cleanup_days 的已压缩消息 + 超过 summary_cleanup_days 的旧摘要 |
| `/memory/maintain` | POST | 手动触发记忆维护（force=true 强制运行） |
| `/memory/maintain/status` | GET | 记忆维护器状态（上次运行/stale数量/验证分布） |

## 记忆维护器（maintainer）

后端内置 `MemoryMaintainer`，由 `[loops.memory]` 定时循环自动运行（间隔见该配置段，默认每 6 小时），另有记忆 API 调用时的惰性触发兜底。职责：

### aging（老化）

- 查询 `facts` 表中 `updated_at < now - 7天` 的记忆
- 在 `memory_meta` 表写入 `stale_score`（天数差）和 `validation_status='pending'`
- 幂等：已存在的 `memory_meta` 记录只更新 `stale_score`

### validate（验证）

- 对 `validation_status='pending'` 的记忆调用 cheap 模型
- LLM 做选择题（不做摘要），返回 JSON：

```json
{"is_accurate": true, "reason": "仍然准确", "suggested_action": "keep"}
```

- `suggested_action` 取值：`keep`（保留）/ `update`（需更新）/ `archive`（归档）
- LLM 失败时记忆保持 `pending`，下个周期重试（不确定时保留）

### 触发机制

- **定时循环**：`[loops.memory]` 注册为 Loop 定时任务（APScheduler `IntervalTrigger`），按 `maintain_interval` 周期直接调 `maintainer.run()`
- **惰性兜底**：每次记忆 API 调用时检查 `schema_info['maintainer_last_run']`，距上次超过间隔则在后台 `_async_maintenance` 中触发
- **手动**：`POST /memory/maintain`（`force=true` 忽略时间检查）

### 配置

```toml
[memory.maintain]
interval_hours = 6                              # 运行间隔
stale_days = 7                                  # 过期阈值
enable_validate = true                          # 启用 LLM 验证
model_tier = "cheap"                            # 验证用模型层级（get_key_for_use 用此层级选 key）
```

## 记忆生成（memory_generation）

**用户与 agent 的对话不存在后端**，记忆生成是**用户触发、agent 执行**的工作流。维护器只做老化+验证。

### 触发方式

用户说"生成记忆"、"记忆生成"、"保存经验"等关键词时，agent 调用 `agent_guide(task_type='recurring.memory_generation')`，返回 `memory_generation_workflow`（5步）+ `do_not_store` 清单，agent 按步骤执行。

### 结构化记忆格式

```json
{
  "fact_type": "user|feedback|project|reference|experience",
  "name": "简短名称",
  "description": "一句话描述",
  "content": "实际内容",
  "why": "为什么重要",
  "how_to_apply": "如何在未来应用",
  "consumption_contexts": ["recurring.accounting", "adhoc.web_archive"],
  "trigger_keywords": ["退款", "账单对冲", "curl"],
  "created_at": "YYYY-MM-DD",
  "source_task": "触发本次记忆生成的任务名"
}
```

> **v6.1 closed taxonomy**：`fact_type` 收敛为 5 类 closed（user/feedback/project/reference/experience）。
> 旧值 `preference` 已由 `tools/migrate_fact_type_v6_1.py` 迁移为 `user`；旧值 `transaction` 已归档删除（业务数据走 todos/wip）。
> 君子协议：未知类型不强制拒绝，Pydantic validator 落 NULL + log warning。

### 记忆消费闭环（核心约束）

写入记忆时必须填 `consumption_contexts` + `trigger_keywords`，确保未来任务能消费：

- `consumption_contexts`：哪些 task_type 应读取此记忆
  - 通用偏好用 `["*"]`
  - scope 通配用 `["recurring.*"]`
  - 具体任务用 `["recurring.accounting", "adhoc.web_archive"]`
- `trigger_keywords`：任务描述/用户消息出现这些词时应读取

下次调 `agent_guide(task_type='recurring.accounting')` 时，后端通过 `find_consumable_memories()` 反向查询所有 `consumption_contexts` 匹配的记忆，并在 `first_action` 前注入"【必读记忆】"指令——agent 不需要记得读哪些记忆，guide 会主动推送。

无法明确消费场景的候选 → 跳过不写入（存了没人用=垃圾）。

### fact_type 分类（v6.1 closed taxonomy）

| 类型 | 含义 | staleness 阈值 | 示例 |
|------|------|----------------|------|
| user | 用户偏好/身份 | 90 天 | 用户喜欢用 curl.exe 而非 PowerShell curl |
| feedback | 用户反馈 | 30 天 | "你这报告太啰嗦了，下次精简点" |
| project | 项目状态/进度/约定 | 7 天 | 异环抽卡清洗脚本 v3 新增道具映射 |
| reference | 参考资料/链接/命令 | 30 天 | ModelScope VL API 限流较严，需低频串行调用 |
| experience | debug 解决方案/坑点/最佳实践 | 14 天 | sqlite WAL 模式下并发写需开 shared_cache |

> **staleness 警告**：`memory_get` / `memory_search` / `agent_guide` 返回的记忆会自动追加 `memory_age`（相对时间 today/yesterday/N days ago）+ `staleness_warning`（超阈值才填）+ `trust_recall_hint`（常驻"引用前请验证"）。
> 模型读到 `staleness_warning` 时应优先用 `file_read` / `file_grep` 验证文件/函数仍存在再引用（TRUSTING_RECALL 机制）。
> `fact_type=NULL`（旧数据或未知类型）不算 staleness（君子协议，不强制拒绝）。

### do_not_store 清单（WHAT_NOT_TO_SAVE）

以下内容不存储为记忆（代码本身可推导 / 可 grep / 已过时）：

- **代码模式 / 架构 / 文件路径 / git history**（代码本身可推导，存了反而过时）
- **AGENTS.md / project_rules.md 已有内容**（重复存储）
- **ephemeral task details**（临时任务细节，会话内解决）
- 代码结构（可 grep 代码库获取）
- git 历史（可 `git log` 获取）
- 已修复的 bug（修复后不再需要）
- 临时调试信息（如某次 OCR 返回的坐标）
- config.toml 中已配置的值

**可以写入的**：debug 解决方案 / 坑点 / 最佳实践（experience 类）、用户偏好 / 身份（user 类）、用户反馈（feedback 类）。
即使看似相关也不要保存上述禁清单内容（君子协议，后端不强制拒绝，但 prompt 强约束）。

### memory_generation vs maintainer

| 维度 | memory_generation（用户触发） | maintainer（后端自动） |
|------|-------------------------------|----------------------|
| 触发 | 用户说"生成记忆" | 每 6 小时自动 / 手动 `POST /memory/maintain` |
| 职责 | 提取+写入新记忆 | 老化+验证已有记忆 |
| LLM | agent 自己 | cheap 模型做选择题 |
| 输出 | 新记忆写入 facts 表 | memory_meta 表标记 stale + 验证结果 |

> 完整工作流见 `.agents/skills/memory_generation.md` 和 `agent_guide(task_type='recurring.memory_generation')`。

## 待办模块（todos）

独立模块 `server/todos/`，与记忆系统共享同一 SQLite 数据库但使用独立表。从旧记忆 key（`task_reminders`、`wip_index`）自动迁移。

### 表结构

**todos 表**（周期任务）：

| 字段 | 说明 |
|------|------|
| id / title / skill | 标识 + 关联 skill |
| type | recurring / oneshot |
| frequency | daily / weekly / monthly |
| last_done_at / next_due_at | 完成时间 + 下次到期（mark_done 时自动计算） |
| condition | 生效条件（如"找工作期间"） |

**wip_tasks 表**（未完成工作）：

| 字段 | 说明 |
|------|------|
| id / title | 标识 |
| status | active / paused / blocked / completed |
| priority | high / medium / low |
| goal / progress | 目标 + 进度百分比 |
| next_steps | 下一步操作（JSON 数组） |
| current_state | 状态快照（JSON 对象） |

### API

| 路径 | 方法 | operation_id | MCP 直连 |
|------|------|-------------|---------|
| `/todos` | GET | todos_list | ✓ |
| `/todos` | POST | todos_create | 网关 |
| `/todos/due` | GET | todos_due | ✓ |
| `/todos/{id}` | GET/PUT/DELETE | todos_get/update/delete | 网关 |
| `/todos/{id}/done` | POST | todos_mark_done | 网关 |
| `/wip` | GET/POST | wip_list/wip_create | ✓/网关 |
| `/wip/{id}` | GET/PUT/DELETE | wip_get/update/delete | 网关 |

> **wip_list summary 模式**：`GET /wip?summary=true` 只返回 `id/title/status/priority/progress/updated_at` 轻量字段，跳过 `goal/next_steps/current_state/related_*/extra_data` 等大字段和 JSON 反序列化。适用于 agent 会话启动或用户问"上次没做完什么"时快速浏览，避免长输出污染上下文。需要详情再用 `wip_get(id)` 单独取。

### 迁移

启动时自动执行（幂等，检查 `schema_info['todos_migrated']`）：
- `task_reminders` → `todos` 表
- `wip_index` → `wip_tasks` 表（基本字段）
- `.agents/wip/*.json` → `wip_tasks` 表（完整字段，更新已有记录）

> 旧 KV 记忆目录和 `.agents/wip/` 的 .json 文件已清理（备份在 `temp/memory_cleanup_backup_20260716/`，`.agents/wip/*.json` 迁移到 `temp/wip_migrated_backup/`）。所有数据以 SQLite 为唯一真源。
