---
name: memory_generation
description: >
  记忆生成与检查。触发词：生成记忆、记忆生成、记忆检查、提取记忆、更新记忆、
  记忆维护、保存经验、记忆总结。当用户要求从当前会话提取记忆、检查记忆质量、
  或会话结束前需要保存经验时触发。后端维护器只做老化+验证，生成本身由 agent 执行。
task_type: recurring.memory_generation
---

# 记忆生成 (memory_generation)

## 触发词
生成记忆、记忆生成、记忆检查、提取记忆、更新记忆、记忆维护、保存经验、记忆总结

## 概述
从当前会话中提取结构化记忆事实，写入三层记忆系统。**用户触发，agent 执行**。
后端维护器（maintainer）仅做自动老化+验证，不负责生成。

## 核心原则（来自 Claude Code memory docs）
1. **结构化优于自由文本**：每条记忆有 type（preference/project/reference）
2. **索引常驻，内容按需加载**：agent_guide 返回 memory_index（key+摘要+stale标记），不含完整内容
3. **cheap 模型做选择题**：验证用 LLM 返回 JSON {is_accurate, suggested_action}，不做摘要
4. **时间感知注入**：>7天未更新的记忆标记为 stale
5. **不存储可 grep 的内容**：代码结构/git历史/已修复bug 留在代码库中

## 记忆结构化格式

每条记忆的 value 是一个 JSON 对象：

```json
{
  "type": "preference|project|reference",
  "name": "简短名称",
  "description": "一句话描述",
  "content": "实际内容/事实",
  "why": "为什么重要",
  "how_to_apply": "如何在未来应用",
  "consumption_contexts": ["recurring.accounting", "adhoc.web_archive"],
  "trigger_keywords": ["退款", "账单对冲", "curl"],
  "created_at": "YYYY-MM-DD",
  "source_task": "触发本次记忆生成的任务名"
}
```

### consumption_contexts（消费场景，必填）

声明哪些 task_type 应读取此记忆。后端 `find_consumable_memories(task_type)` 会反向查询所有声明的记忆，在 `agent_guide` 返回时自动推送给相关任务。

- 通用偏好用 `["*"]`（所有任务都该读）
- scope 通配用 `["recurring.*"]`（所有周期任务该读）
- 具体任务用 `["recurring.accounting", "adhoc.web_archive"]`
- 纯 project 状态记忆填对应 skill 的 task_type（如 `["recurring.yihuan_gacha"]`）

**软强制**：无法明确消费场景的候选 → 跳过不写入（存了没人用 = 垃圾）。例外：纯 project 状态记忆填对应 task_type 后可写入。

### trigger_keywords（触发关键词，必填）

任务描述/用户消息出现这些词时应读取此记忆。后端 `find_consumable_memories(task_query=...)` 会做子串匹配。

- 如 `["退款", "账单对冲"]` / `["curl", "HTTP"]` / `["PaddleOCR", "bbox"]`
- 关键词应选具有任务辨识度的词，避免过于通用（如 `["任务"]` 无意义）

### type 说明

| 类型 | 含义 | 示例 |
|------|------|------|
| preference | 用户偏好/习惯 | 用户喜欢用 curl.exe 而非 PowerShell curl |
| project | 项目状态/进度 | 异环抽卡清洗脚本 v3 新增道具映射 |
| reference | 参考资料 | ModelScope VL API 限流 RPM≈5 |

## 工作流

### 步骤 1：回顾任务
回顾当前会话中完成的工作。识别候选记忆：
- 用户偏好（preference）
- 项目状态（project）
- 参考资料（reference）

**不存储**：
- 代码结构（可 grep 代码库获取）
- git 历史（可 git log 获取）
- 已修复的 bug（修复后不再需要）
- 临时调试信息
- AGENTS.md / _index.md 中已记录的内容
- config.toml 中已配置的值

### 步骤 2：分类候选
对每个候选记忆，确定类型并填写结构化字段。
每条记忆必须能回答：这是什么？为什么重要？如何应用？

### 步骤 3：查重
对每个候选记忆：
1. `memory_list` 查看现有 key
2. 如果 key 已存在，用 `memory_get(key)` 读取现有内容
3. 比较新旧内容：
   - 只是更新 → 用 merge 模式覆盖
   - 完全重复 → 跳过
   - 新增信息 → 合并后写入

### 步骤 4：写入
用 `memory_set(key, {data: <structured_value>, merge: true})` 写入。
key 命名规范：`{scope}.{name}`，如 `preferences.curl_usage`, `project.yihuan_gacha`

已有的 key（如 accounting, wip_index）保持原名，用 merge 模式更新。

### 步骤 5：可选触发维护
如果写入了 3 条以上新记忆，可调用 `localagent_advanced_tool(memory_maintain, {force: true})`。

### 自检（可选）
写入后自检：
1. 每条记忆是否有 type 字段？
2. 是否有可 grep 的内容？→ 删除
3. 是否重复了已有记忆？→ memory_list 交叉检查
4. 不确定是否该保留 → 保留

## key 命名规范
- 偏好：`preferences.{name}`
- 项目：`project.{name}`
- 参考：`reference.{name}`
- Skill 进度：沿用 skill 名（如 `accounting`, `yihuan_gacha`）
- 跨 Skill 偏好：`preferences`

## 与维护器的关系

| 维度 | memory_generation（本 skill） | maintainer（后端自动） |
|------|-------------------------------|----------------------|
| 触发 | 用户说"生成记忆" | 每6小时自动 / 手动 POST /memory/maintain |
| 职责 | 提取+写入新记忆 | 老化+验证已有记忆 |
| LLM | agent 自己（用 agent_chat） | cheap 模型做选择题 |
| 输出 | 新记忆写入 facts 表 | memory_meta 表标记 stale + 验证结果 |

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 记忆系统 | memory_list / memory_get / memory_set | 读写记忆 |
| 维护器 | localagent_advanced_tool(memory_maintain) | 可选触发验证 |
