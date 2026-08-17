# 工具清单分四类呈现（内置基本 + 核心直暴露 + MCP 子桶 + REST 物化）

116 个 MCP 工具被压缩成 1 个 `localagent_advanced_tool` 网关端点，模型无法选择具体子工具；工具描述只有 summary/description 无 when_to_use/when_not_to_use，模型瞎调；agent 缺通用能力（文件读写）过度依赖 `exec_python` 间接实现。本决策将工具清单分四类呈现：(A) 10 个内置基本工具自实现；(B) 19 个项目核心 MCP 工具直接暴露（去前缀 + 完整 7 段说明书）；(C) 本项目 MCP 自动拆一层子桶，其他 MCP server 给桶概览；(D) server REST 端点从 `/openapi.json` 物化。对模型不强调"这是 MCP 工具"。

## Status

accepted (2026-08-05)

## Context

v6-lite 引擎的工具清单面临三重问题：

1. **116 个 MCP 工具平铺 = context 爆炸**：现有 MCP 网关把 116 个工具压缩成 1 个 `localagent_advanced_tool` 端点，模型只知道"有个网关"但不知道有哪些子工具、何时该调哪个。若 116 个全平铺进 system prompt（每个按 7 段说明书 ~400 token），约 46k token，撑爆上下文。
2. **工具描述不足，模型瞎调**：现有工具描述只有 summary/description，无 when_to_use/when_not_to_use，模型不知道何时该用、何时该换其他工具。模糊工具名（如 `todos_due`/`wip_list`/`agent_guide`/`screen_ocr`）尤其严重。
3. **agent 缺通用能力**：用户反馈"基本工具也要给，不能全是我们的 mcp，文件查看、编写等等"——agent 需要文件读写/搜索/网页抓取等通用能力，不应只依赖项目 MCP。过度依赖 `exec_python` 间接实现文件操作效率低且需审批。

用户明确要求："不强调 mcp"——对模型来说工具就是工具，不应让模型关心是 MCP 还是 REST。

## Decision

工具清单分四类呈现，在 stable prefix 的 Tools 段组装：

### A. 内置基本工具（10 个，agent 自带不依赖 server，直接调用）

在 `client/core/agent/builtin_tools/` 下自实现，注册到 `ToolRegistry`：`file_read`（支持 offset/limit 精细读取，防超大文件撑爆上下文）/ `file_write` / `file_edit`（精确字符串替换）/ `file_grep`（ripgrep 封装）/ `file_glob`（文件名模式匹配）/ `file_ls` / `file_delete` / `ask_user`（结构化选项提问）/ `web_search` / `web_fetch`。

不加 `run_command`（与 `exec_cmd` 重叠且需严格审批）。`file_read` 默认 offset=1/limit=None 读全文，超大文件（>1MB 或 >2000 行）模型应主动分段读取，配合 `file_grep` 先定位行号再精读。

### B. 项目核心 MCP 工具（19 个，直接暴露，去前缀 + 完整 7 段说明书）

从 116 个 MCP 工具中筛选 19 个高频核心工具直接物化为独立 `ToolEntry`：
- memory（5）：`memory_get`/`memory_set`/`memory_list`/`memory_search`/`memory_status`
- guide（1）：`agent_guide`
- exec（3）：`exec_python`/`exec_cmd`/`exec_status`
- browser（4）：`browser_navigate`/`browser_snapshot`/`browser_click`/`browser_type`
- screen（2）：`screen_ocr`/`capture_screen`
- todos（2）：`todos_due`/`todos_mark_done`
- wip（2）：`wip_list`/`wip_get`

工具名去掉 `mcp_localagent_` 前缀（直接用 `memory_get` 而不是 `mcp_localagent_memory_get`），`ToolRegistry` 内部映射回实际调用名。给完整 7 段说明书（purpose/when_to_use/when_not_to_use/inputs/outputs/error_cases/examples），按工具名自描述程度自适应详略（明确的工具少写，模糊的工具多写）。

### C. MCP 工具桶（本项目 MCP 自动拆一层子桶，其他 MCP server 给桶概览）

- **本项目 MCP（localagent_advanced）**：116 个子工具按 `operation_id` 前缀自动归类为子桶（`browser_*`/`screen_*`/`exec_*`/`ocr_*`/`vision_*`/`memory_*`/杂项），每个子桶给 purpose + when_to_use + 子工具清单（仅 name + purpose）+ 调用方式。核心 19 个工具在子桶清单中标 `*` 表示已直接暴露。启动时 `ToolRegistry` 调 `localagent_list_tools` 拉取子工具清单自动归类。
- **其他 MCP server（如 lark-cli）**：给桶概览（桶名 + 工具数 + purpose + when_to_use + 调用方式 + `list_tools()` 查子工具），不自动展开。

### D. server REST 端点（从 `/openapi.json` 物化，完整 7 段说明书）

保留现有 `ToolRegistry` 从 `/openapi.json` 物化 `x-agent-callable=true` 路由的逻辑（route_tags fail-closed 后未显式声明的路由不进 catalog），给完整 7 段说明书。

### 对模型不强调"这是 MCP 工具"

工具说明书不标注"MCP 工具"字样，对模型来说四类工具都是普通工具，调用方式统一（ToolRegistry 内部处理映射）。

## Considered Options

1. **116 个工具平铺**——被拒：~46k token 撑爆上下文，模型注意力稀释反而瞎调。
2. **只给桶概览不给子桶清单**——被拒：模型知道"有个 browser 桶"但不知道桶里有 `browser_navigate`/`browser_click` 等子工具，无法选择具体子工具。本项目 MCP 是高频使用，必须拆一层子桶让模型看到子工具分类。
3. **不引入内置基本工具（依赖 `exec_python` 间接实现文件操作）**——被拒：用户明确要求"基本工具也要给"；`exec_python` 间接实现文件读写需审批、效率低、且每次都要写 Python 代码片段而非直接调专用工具。
4. **全 116 工具都给完整 7 段说明书**——被拒：~46k token；改为核心 19 个给完整 7 段，其余走子桶清单（仅 name + purpose），自适应详略省 token。

## Consequences

**正面**：
- agent 具备通用能力（文件读写/搜索/网页），不过度依赖 `exec_python`。
- 核心 19 个工具可直接调用，模型有完整 7 段说明书不会瞎调。
- 子桶分类避免 116 工具平铺的 context 爆炸，又比纯桶概览更可见。
- 对模型不强调 MCP，统一工具调用体验。

**负面**：
- 7 段说明书需在 `data/client/tool_specs.yaml` 手工编写维护，116 个工具的说明书是持续投入。
- 自适应详略规则（工具名自描述强弱）依赖人工判断，可能不一致。
- 四类工具的边界（何时算"核心"该直接暴露 vs 进子桶）需定期复审。

**回退路径**：`ToolRegistry` 配置可调整四类分类（增减核心工具、调整子桶归类），不影响工具实际可用性；内置基本工具可随时禁用回退到 `exec_python` 间接实现。

## References

- 决策来源：[`temp/sdd/memory-prompt-tool-refactor/00-decisions.md`](file:///f:/<project_root>/temp/sdd/memory-prompt-tool-refactor/00-decisions.md) D8（分桶清单 + 常用工具直接给 + MCP 简化包装 + 内置基本工具）、D8.1（10 个内置基本工具清单）、D8.2（19 个核心 MCP 工具筛选）、D8.3（本项目 MCP 自动拆一层子桶，其他 MCP 给桶概览）、D12（7 段说明书自适应详略）、D8.1.1（file_read 支持 offset/limit）
- 关键代码路径：[client/core/agent/builtin_tools/](file:///f:/<project_root>/client/core/agent/builtin_tools/)（10 个内置基本工具自实现）、[client/core/agent/tool_registry.py](file:///f:/<project_root>/client/core/agent/tool_registry.py)（ToolRegistry 物化 + 分类）、[server/agent_guide.py](file:///f:/<project_root>/server/agent_guide.py)（task_type 路由 + mcp_tools_priority）、[server/mcp_whitelist.py](file:///f:/<project_root>/server/mcp_whitelist.py)（直连工具白名单 + GATEWAY_EXCLUDE）
- 相关 ADR：[ADR-0006](0006-v6-lite-scope-cut.md)（v6-lite 引擎，ToolRegistry 从 /openapi.json 物化是 W2 切片）、[ADR-0009](0009-toolresult-five-variant-enum.md)（ToolResult 5 变体，内置基本工具也使用该结构）
