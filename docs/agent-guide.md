# Agent Guide 任务路由系统

本文档描述 server 端任务路由系统的架构、GUIDE_REGISTRY 数据结构、三种调用模式、consumption_contexts 消费闭环、first_action 注入机制、workspace 动态扩展机制。keywords 编写规范见 [agent-guide-keywords.md](file:///f:/<project_root>/docs/agent-guide-keywords.md)。

## 1. 概述

`agent_guide` 是 server 端任务路由入口，agent 在执行任何任务前**强制前置调用**一次：

- 自然语言描述任务 → 关键词匹配返回候选清单 + 第一步动作指令
- 精确 task_type 查询 → 返回该 task_type 的完整指导（含必读记忆 + 工具优先级 + 关键陷阱）
- 无参调用 → 返回 GeneralGuide（全量分类 + 通用陷阱 + 文件位置）

路由层零侵入增强：返回的 `first_action` 会自动注入"【必读记忆】"块（基于 consumption_contexts 反向查询），agent 不需要自己记得读哪些记忆。

## 2. GUIDE_REGISTRY 结构

**定义位置**：[agent_guide.py L196](file:///f:/<project_root>/server/agent_guide.py)（浅拷贝静态版本 + 运行时 update 动态条目）

```python
GUIDE_REGISTRY: dict[str, dict] = dict(_STATIC_GUIDE_REGISTRY)  # L196
# ...
GUIDE_REGISTRY.update(_load_optional_guide_entries())  # L262
```

- 静态导入：从 [agent_guide_data.py](file:///f:/<project_root>/server/agent_guide_data.py) 导入 `_STATIC_GUIDE_REGISTRY`
- 动态合并：`_load_optional_guide_entries()` 加载 workspace 组件的 manifest 声明条目（详见第 8 节）
- 浅拷贝设计：测试通过 `importlib.reload(agent_guide)` 模拟组件删除/添加时，GUIDE_REGISTRY 重置为静态版本再 update 动态条目，避免污染

### entry 字段清单

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill` | str / None | 是 | skill 标识符（如 "deep_research"），None 表示工作流定义无独立 skill 文件 |
| `name` | str | 是 | 中文显示名 |
| `skill_file` | str / None | 是 | skill 文件相对路径（如 `.agents/skills/...`），None 表示无 |
| `keywords` | list[str] | 是 | 关键词列表（用于 match_task_candidates 匹配） |
| `description` | str | 是 | 一句话描述 |
| `first_action` | str | 是 | 第一步动作指令（agent 拿到后立即执行） |
| `memory_key` | str / None | 是 | 主关联记忆 key（如 "life_design"），None 表示无 |
| `workflow_summary` | str | 否 | 工作流摘要（include_workflow=true 时返回） |
| `mcp_tools_priority` | list[str] | 否 | MCP 工具优先级清单 |
| `key_pitfalls` | list[str] | 否 | 关键陷阱清单 |
| `prerequisites` | list[str] | 否 | 前置条件清单 |
| `level` | str | 是 | 详细级别（"minimal"/"standard"/"full"） |
| `related_memory_keys` | list[str] | 否 | 额外关联记忆 key |
| `domain_hints` | dict[str, int] | 否 | 域名加分提示（如 `{"xiaoheihe.cn": 10}`） |
| `memory_generation_workflow` | dict | 否 | 记忆生成工作流（仅 recurring.memory_generation） |
| `task_closure_workflow` | dict | 否 | 任务收尾工作流（仅 system.task_closure） |
| `wip_archive_workflow` | dict | 否 | WIP 归档工作流（仅 system.wip_archive） |
| `classification_table` | dict | 否 | 分类决策表（仅 system.temp_cleanup） |
| `do_not_store` | list[str] | 否 | 不应存储的内容清单 |
| `vendor_repo` | str | 否 | 上游仓库 URL（融合 skill 标注来源） |
| `vendor_path` | str | 否 | 上游 skill 本地路径 |

## 3. Scope 分类

代码中实际存在 6 个 scope：5 个静态 scope 定义在 [agent_guide_data.py](file:///f:/<project_root>/server/agent_guide_data.py)，第 6 个 `recording` 通过 workspace 动态加载。

| scope | 描述 | 代表性 task_type | 来源 |
|-------|------|------------------|------|
| `recurring` | 周期循环任务 | `recurring.daily_summary`、`recurring.life_design`、`recurring.memory_generation` | 静态 |
| `adhoc` | 一次性任务 | `adhoc.deep_research`、`adhoc.office_xlsx`、`adhoc.browser_automation`、`adhoc.headless_session` | 静态 |
| `system` | 系统元任务 | `system.task_closure`、`system.task_reminder`、`system.project_audit`、`system.release`、`system.public_distribution` | 静态 |
| `dev` | 开发任务 | `dev.goal_engineering`、`dev.implement`、`dev.anti_hallucination`、`dev.to_spec`、`dev.grill_me` | 静态 |
| `daily` | 日常事务 | `daily.teach`、`daily.cangjie_extraction` | 静态 |
| `recording` | 录制包消费（动态加载） | `recording.discover`、`recording.consume` | 动态（[workspace/recorder/loop_actions.py](file:///f:/<project_root>/workspace/recorder/loop_actions.py)） |

> task_type 完整清单见 [.agents/skills/_index.md](file:///f:/<project_root>/.agents/skills/_index.md) 核心 Skill 段 + 组件化模块段，数量随新增/重构变动。

## 4. 三种调用模式

**统一端点**：`GET /guide`（[agent_guide.py L810-966](file:///f:/<project_root>/server/agent_guide.py)，`operation_id="agent_guide"`）

**函数签名**：

```python
@router.get("", operation_id="agent_guide")
def get_agent_guide(
    task: str | None = Query(None, ...),          # 自然语言匹配
    task_type: str | None = Query(None, ...),     # 精确 task_type 查询
    include_structure: bool = Query(False, ...),  # 返回 project_structure 字段
    include_workflow: bool = Query(False, ...),   # 返回 workflow_summary/mcp_tools_priority/key_pitfalls
    context: str | None = Query(None, ...),       # 上下文信号（文件路径/URL/文本）
)
```

### 调用模式分支

| 模式 | 触发条件 | 代码位置 | 行为 |
|------|----------|----------|------|
| **task_type 精确命中** | 传 `task_type` 参数且在 GUIDE_REGISTRY 中 | L840-849 | 返回 TaskGuide（含 first_action + candidates=[]）；`task_type='system.task_closure'` 时自动注入 `structure_diff` |
| **task 关键词匹配** | 传 `task` 参数（无 task_type） | L871-928 | 调 `match_task_candidates` → 弱匹配走 `mode=low_confidence`（L882-915）→ 强匹配走 TaskGuide（L916-928） |
| **无参 GeneralGuide** | 不传 task 也不传 task_type | L949-966 | 返回全量分类清单 + 通用陷阱 + 文件位置 |

**额外端点**：
- `GET /guide/usage`（L1016-1055，`operation_id="agent_guide_usage"`）— 使用统计（检测 undertriggering）

## 5. consumption_contexts 消费闭环

记忆系统的"可消费性"机制：写入时声明消费场景 → guide 读取时反向查询自动注入。避免"存了没人用"的垃圾记忆。

### 5.1 数据库列定义

文件：[memory/schema.py L63-64](file:///f:/<project_root>/server/memory/schema.py)

```sql
consumption_contexts TEXT,  -- JSON 数组字符串，如 ["recurring.accounting", "adhoc.web_archive"]
trigger_keywords TEXT       -- JSON 数组字符串，如 ["退款", "curl"]
```

迁移逻辑：L233-289（`migrate_db` v3 添加 `consumption_contexts` 列）。

### 5.2 写入端（记忆写入时填字段）

文件：[memory/manager.py](file:///f:/<project_root>/server/memory/manager.py)

`set` 方法 L189+，关键字段处理在 L218-230：

```python
if structured:
    prepared: dict = {}
    for k, v in structured.items():
        if v is None:
            continue
        if k in ("consumption_contexts", "trigger_keywords") and isinstance(v, list):
            prepared[k] = json.dumps(v, ensure_ascii=False)
        else:
            prepared[k] = v
    if prepared:
        self.store._update_fact_structured_fields(key, prepared)
```

### 5.3 读取端（guide 读取时反向查询注入）

文件：[memory/manager.py](file:///f:/<project_root>/server/memory/manager.py)

`find_consumable_memories` 方法 L364-（完整实现延续到约 L545）：

**算法**：
1. **SQL 索引优先**（L392-407）：用 `LIKE` 模式匹配三种 pattern：
   - `'%"*"%'` — 全场景通配
   - `f'%"{task_type}"%'` — 精确匹配
   - `f'%"{task_scope}.*"%'` — scope 通配（如 `recurring.*`）
2. **回退全表扫描**（L413-431）：旧数据 `consumption_contexts` 列为 NULL 时
3. **逐条匹配判定**（L499-523）：
   - `matched_context`：`"*"` / 精确 task_type / `scope.*` 通配
   - `matched_keyword`：`trigger_keywords` 中任一词出现在 `task_query` 中
4. **返回字段**：`{key, summary, updated_at, stale, days_since_update, matched_by}`，`matched_by` 取值 `"context"` / `"keyword"` / `"context+keyword"`
5. **不增加 access_count**（用 `get_fact_meta` 而非 `get_fact`）

### 5.4 调用点（_build_task_guide 中）

文件：[agent_guide.py L700-716](file:///f:/<project_root>/server/agent_guide.py)

```python
# 动态关联：按 consumption_contexts / trigger_keywords 反向查询
dynamic_memories = []
if mgr:
    task_query = candidates[0].get("name", "") if candidates else None
    dynamic_memories = mgr.find_consumable_memories(task_type, task_query=task_query)
```

静态 `memory_key` + `related_memory_keys` 通过 `mgr.get_memory_index(static_keys)` 查询（L693-698），动态结果合并去重后追加 `matched_by` 字段（L710-716）。

### 5.5 staleness 字段注入

文件：[memory/router.py L535](file:///f:/<project_root>/server/memory/router.py)

`_enrich_with_staleness(data)` 在 `_build_task_guide` 中调用（L720-724），让首轮就能看到过时记忆警告。

### 5.6 写入约束（task_closure 中强调）

文件：[agent_guide_data.py](file:///f:/<project_root>/server/agent_guide_data.py)

`system.task_closure` 的 `step_3_extract.instruction` L446-453：

> 可消费性自检（核心约束）：每条候选必须填：
> - `consumption_contexts`: 哪些 task_type 应读取此记忆（通用用 `['*']`，具体用 `['recurring.accounting']`）
> - `trigger_keywords`: 任务描述出现这些词时应读取（如 `['退款','curl']`）
> 无法明确消费场景的候选 → 跳过不写入（存了没人用=垃圾）

同样约束出现在 `system.wip_archive` 的 `step_2_extract.instruction` L1446-1449。

## 6. first_action 注入"【必读记忆】"机制

文件：[agent_guide.py L726-736](file:///f:/<project_root>/server/agent_guide.py)

```python
# first_action 后处理注入"【必读记忆】"（零侵入路由层增强）
# 只对实际存在的记忆（updated_at 非 None）注入，避免对空 key 占位条目误注入
first_action = entry.get("first_action", "")
existing_memories = [m for m in memory_index if m.get("updated_at") is not None]
if existing_memories:
    must_read_lines = ["【必读记忆】以下记忆与当前任务相关，执行前先 memory_get 读取："]
    for m in existing_memories[:5]:
        summary = m.get("summary") or "(无摘要)"
        must_read_lines.append(f"- {m['key']}: {summary}")
    must_read_block = "\n".join(must_read_lines)
    first_action = must_read_block + "\n\n" + first_action
```

**注入逻辑**：
- 只对 `updated_at` 非 None 的记忆注入（跳过空 key 占位条目）
- 最多取前 5 条记忆
- 格式：`【必读记忆】` 标题 + `- {key}: {summary}` 列表 + 空行 + 原 first_action
- 注入后的 `first_action` 写入响应 result（L747）

## 7. match_task_candidates 五路加权

文件：[agent_guide.py L504-638](file:///f:/<project_root>/server/agent_guide.py)

### 五路加权（摘要级）

| 路 | 信号 | 分数 | 代码位置 |
|----|------|------|----------|
| 1 | **kw_exact** — entry keyword 完整子串出现在 task 中 | +10/词 | L556-559 |
| 2 | **bigram_overlap** — 中文 2-gram 集合交集 | +2/重叠 bigram，上限 16 | L562-566 |
| 3 | **synonym** — 同义词组命中 | +3/组基础 +2/组不同词 | L569-572（`_synonym_hits` L386-403） |
| 4 | **kw_fuzzy** — keyword 的 2-gram ≥50% 在 task 中 | +3/词 | L575-584 |
| 5 | **domain_hints** — context 含特定 domain 时加分 | +bonus（由 entry 声明） | L588-599 |

### 关键辅助函数

- `_char_bigrams` L336-345 — 中文 2-gram 分词
- `_strip_aux` L359-364 — 剥离语气助词（"一下/看看/试试"等，`_AUX_PARTICLES` L353-356）
- `_SYNONYM_GROUPS` L369-383 — 8 个同义词组（屏幕/识别/点击/输入/窗口/操作/文字/爬取-保存）
- `_extract_context_signals` L455-501 — 从 context 提取 URL domains + 中文 bigrams

### strong_match 判定（保守）

L614-629：只有 `kw_exact` 命中 OR `kw_fuzzy ratio ≥ 80%` 才算强匹配。纯 bigram 重叠的弱匹配走 `mode=low_confidence` 路径。

### 弱匹配阈值

`WEAK_MATCH_THRESHOLD = 15`（L411）— top-1 score 低于此值走精简响应。

> 关键词编写规范（5 大原则 + 标准流程 + 维护清单）详见 [agent-guide-keywords.md](file:///f:/<project_root>/docs/agent-guide-keywords.md)。

## 8. workspace 动态扩展

### 8.1 manifest.toml 示例

文件：[workspace/accounting/manifest.toml](file:///f:/<project_root>/workspace/accounting/manifest.toml)

```toml
[component]
name = "accounting"
version = "0.4.0"
description = "记账模块 - 账单识别+GUI审核+导出Obsidian，本地SQLite不走后端HTTP"
enabled = true

[agent_guide]
file = "loop_actions.py"
entries_var = "GUIDE_REGISTRY_ENTRIES"

[skill]
file = "SKILL.md"
task_type = "recurring.accounting"
index_section = "components"
```

每个 workspace 子目录一个 manifest.toml，通过 `[agent_guide]` 段声明 task_type 入口。

### 8.2 AgentGuideEntry dataclass

文件：[component_manifest.py L46-49](file:///f:/<project_root>/server/component_manifest.py)

```python
@dataclass
class AgentGuideEntry:
    """agent_guide 路由插入点入口"""
    file: str
    entries_var: str        # 如 GUIDE_REGISTRY_ENTRIES
```

### 8.3 加载流程

文件：[agent_guide.py L199-258](file:///f:/<project_root>/server/agent_guide.py) `_load_optional_guide_entries`

**双轨策略**：
1. **优先读 manifest**（L219-237）：调 `load_manifests()` → 对每个 enabled 组件读 `[agent_guide]` 段 → `importlib.import_module(f"workspace.{name}.{m.agent_guide.file[:-3]}")` → 取 `entries_var`（通常为 `GUIDE_REGISTRY_ENTRIES`）→ `result.update(entries)`
2. **回退旧机制**（L239-256）：扫 `workspace/*/loop_actions.py` → 跳过已通过 manifest 加载的组件 → 读 `GUIDE_REGISTRY_ENTRIES` 属性

主代码库不直接 import 任何 workspace 组件，删除 `workspace/<component>/` 后 manifest 消失，条目自动注销。

### 8.4 测试隔离设计

GUIDE_REGISTRY 用浅拷贝创建独立 dict（L196 `dict(_STATIC_GUIDE_REGISTRY)`），而非直接 import 引用。原因（L181-185 注释）：`test_component_e2e` 通过 `importlib.reload(agent_guide)` 模拟组件删除/添加，若直接 import 引用，reload 不会 reload `agent_guide_data`，导致 GUIDE_REGISTRY 保留上次的动态条目污染。浅拷贝确保 reload 时 GUIDE_REGISTRY 重置为静态版本，再由 update 合并动态条目。

## 9. 流程图

```mermaid
flowchart TB
    Start[Agent 调用 agent_guide] --> CheckArgs{参数判断}

    CheckArgs|task_type 精确| TypeBranch[查 GUIDE_REGISTRY]
    CheckArgs|task 自然语言| TaskBranch[match_task_candidates]
    CheckArgs|无参| GeneralBranch[返回 GeneralGuide]

    TypeBranch --> TypeFound{命中?}
    TypeFound|是| BuildType[_build_task_guide]
    TypeFound|否| NotFound[返回 not_found]

    TaskBranch --> FiveWay[五路加权<br/>kw_exact +10<br/>bigram +2<br/>synonym +3<br/>kw_fuzzy +3<br/>domain_hints]
    FiveWay --> Score{score ≥ 15?}
    Score|否| LowConf[mode=low_confidence<br/>精简响应]
    Score|是| Strong{kw_exact 命中<br/>OR kw_fuzzy ≥80%?}
    Strong|否| LowConf
    Strong|是| BuildTask[_build_task_guide]

    BuildType --> Inject[consumption_contexts<br/>反向查询<br/>find_consumable_memories]
    BuildTask --> Inject

    Inject --> FirstAction[first_action 后处理<br/>注入【必读记忆】<br/>最多 5 条]
    FirstAction --> Response[返回 TaskGuideResponse<br/>含 first_action +<br/>candidates + memories]

    GeneralBranch --> FullList[全量分类清单<br/>+ 通用陷阱<br/>+ 文件位置<br/>+ dev 入口决策树]
    FullList --> GeneralResp[返回 GeneralGuideResponse]

    subgraph "workspace 动态扩展"
        Manifest[workspace/*/manifest.toml] --> Load[_load_optional_guide_entries]
        Load -->|importlib + getattr| Update[GUIDE_REGISTRY.update]
        Update --> Registry[(GUIDE_REGISTRY)]
    end

    Registry --> TypeBranch
    Registry --> TaskBranch
```

## 10. 相关文档

- [agent-guide-keywords.md](file:///f:/<project_root>/docs/agent-guide-keywords.md) — keywords 编写规范（5 大原则 + 标准流程 + 维护清单）
- [.agents/skills/_index.md](file:///f:/<project_root>/.agents/skills/_index.md) — task_type 完整清单（核心 Skill 段 + 组件化模块段）
- [todos-wip.md](file:///f:/<project_root>/docs/todos-wip.md) — 待办与 WIP 系统（task_closure 整合）
- [chat-engine.md](file:///f:/<project_root>/docs/chat-engine.md) — v6-lite 对话引擎架构
- `server/agent_guide.py` — 主路由层
- `server/agent_guide_data.py` — 静态 GUIDE_REGISTRY 数据
