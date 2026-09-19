# MCP 架构详解
> 本文档是 `AGENTS.md` 中 MCP 相关内容的详细展开。AGENTS.md 只保留 MCP 接入配置和工具优先原则。

## 三层 MCP 架构（advanced_tool + template_tool 网关）

借鉴 Unity MCP 的设计，突破 MCP 工具数量上限，同时减少 AI 编写重复代码：

```
第一层：直接暴露高频工具（见 `server/mcp_whitelist.py` TOOL_CATEGORIES）  ←  IDE 直接看到
    ↓
第二层 A：localagent_advanced_tool（网关）  ←  通过 tool 参数路由到低频/运维子工具
    ↓
第二层 B：localagent_template_tool（模板网关）  ←  通过 template 参数执行 11 个预定义工作流
    ↓
localagent_list_tools / localagent_list_templates  ←  运行时发现可用工具及参数
```

### advanced_tool 网关（低频工具路由）

- **用途**：低频/运维操作（模型管理/MindForge 运维/记账/输出查看等）
- **调用**：`localagent_advanced_tool(tool=<name>, params={...})`
- **发现**：`localagent_list_tools(category="ocr")` 返回该分类下所有子工具及参数定义
- **未来扩展**：新增工具可加入网关，不受 MCP 工具数量限制

示例：
```
localagent_advanced_tool(tool="ocr_set_keep_models", params={"keep": true})
localagent_advanced_tool(tool="mindforge_preload", params={})
localagent_advanced_tool(tool="exec_output_full", params={"exec_id": "exec_xxx", "channel": "stdout"})
```

### template_tool 模板网关（预定义工作流）

- **用途**：常见工作流模式（截图+OCR、浏览器导航+等待、游戏等待+点击等），避免重复编写 exec_python 代码
- **调用**：`localagent_template_tool(template=<name>, params={...})`
- **发现**：`localagent_list_templates(category="screen")` 返回该分类下所有模板及参数定义
- **模板分类**：
  - `screen` (3个): screenshot_and_ocr, screenshot_inline_for_llm, screen_diff
  - `browser` (3个): browser_navigate_and_wait, browser_extract_article, browser_scroll_and_collect
  - `game` (2个): game_wait_and_click, game_auto_click_loop
  - `file` (2个): file_search_content, file_list_recent
  - `system` (1个): system_window_focus
- **每个模板含 `prompt_hint`**：告诉 AI 何时使用此模板

示例：
```
localagent_template_tool(template="screenshot_and_ocr", params={"mode":"window","window_title":"游戏"})
localagent_template_tool(template="browser_navigate_and_wait", params={"url":"https://example.com"})
localagent_template_tool(template="game_wait_and_click", params={"wait_text":"确认","click_x":500,"click_y":400})
localagent_template_tool(template="file_search_content", params={"directory":"server","pattern":"def capture","file_glob":"*.py"})
```

### Tool Annotations（MCP 协议语义标记）

MCP 工具列表响应中每个工具携带 `annotations` 字段（4 个 hint），帮助 agent 理解工具副作用语义：

| hint | 含义 | 示例 |
|------|------|------|
| `readOnlyHint` | 纯查询，无副作用，可安全重复调用 | `memory_get`, `agent_guide` |
| `destructiveHint` | 破坏数据/状态（删除/终止/覆盖） | `exec_kill`, `memory_delete` |
| `idempotentHint` | 相同参数重复调用效果相同 | `memory_set`, `wait` |
| `openWorldHint` | 与外部世界交互（网络/文件系统/进程） | `exec_python`, `browser_navigate` |

未列出的工具默认全为 false（保守策略，agent 需自行判断）。定义见 `server/mcp_whitelist.py` 的 `TOOL_ANNOTATIONS`。

### 文档与语义端点（MCP-only agent 补全）

仅通过 MCP 连接的 agent（无法直接读项目文件）可通过以下端点获取工具使用文档和网站方法论：

**`localagent_tool_docs`** — 工具 7 段语义说明
- 返回 `data/client/tool_specs.yaml` 中手工编写的 purpose/when_to_use/when_not_to_use/inputs/outputs/errors/examples
- 弥补 MCP `tools/list` 只有裸 docstring 的不足
- 用法：`localagent_tool_docs(tool_name="exec_python")` 查单个；省略参数返回清单

**`localagent_get_docs`** — 项目使用文档（9 份，2 类）
- core(5 份，如何用工具): agents / tools-guide / skills-index / computer-use-skill(桌面操作铁律) / browser-lessons-skill(浏览器方法论)
- system(4 份，机制参考): mcp-reference / memory-system / computer-use-reference(UIA/DPI 详解) / operations-manual(浏览器经验记录强制章节)
- 超长文档自动截断，从"外部项目仅通过 MCP 使用本系统"角度筛选，不含项目内部开发/维护文档
- 用法：`localagent_get_docs(doc_name="browser-lessons-skill")` 查单个；`localagent_get_docs(category="core")` 按分类过滤；省略参数返回清单

### 浏览器方法论自更新闭环

agent 通过以下三个端点形成"读 → 操作 → 写"闭环，无需直接访问文件系统：

**`browser_match_site`** — 读站点经验（直连 MCP 工具，browser 桶）
- 按 domain 查询 `.agents/skills/browser_lessons/sites/*.md`，返回命中文件正文（去 frontmatter）
- 用法：`browser_match_site(domain="xiaoheihe.cn")`

**`browser_session_create`** — 自动注入 site_lessons
- 创建 session 时按 URL 自动匹配 sites 文件，响应中 `site_lessons` 数组 + `site_lessons_hint` 提示
- 后续 `browser_snapshot` / `browser_action` 响应也会带 `site_lessons_hint`（仅当 session 有 lessons 时）

**`browser_write_lesson`** — 写站点经验（直连 MCP 工具，闭环关键）
- agent 摸索出新方法/踩坑后调用，自动按 domain 规范化（点改横线）、按 `_template.md` 9 段结构创建/追加 sites/<domain>.md
- 自动合并 frontmatter aliases、更新"最后更新"日期、追加"修改历史"行
- section 必须是 9 段之一（网站概况/浏览器方案/反爬风控/DOM 结构与提取规则/URL 规则/图片资源加载/评论动态内容/已知坑/遗留问题），content ≤50KB
- 用法：`browser_write_lesson(domain="xiaoheihe.cn", section="已知坑", content="| 列表懒加载未触发 | 直接 querySelector | 先 scroll 500px 再查 | 2026-08-08 |")`

闭环流程：访问网站 → `browser_session_create` 自动注入或主动 `browser_match_site` 读经验 → `browser_action` 操作 → 踩坑/找到最佳实践 → `browser_write_lesson` 写入 → 下次访问自动加载。

`browser_navigate` / `browser_snapshot` / `browser_action` / `browser_session_create` 端点描述中均含 "Tip" 提示，让 agent 不读 AGENTS.md 也能发现 sites 经验可用。

### Computer Use 软件经验闭环（screen_match_app + screen_write_lesson）

与浏览器 `browser_match_site` / `browser_write_lesson` 对称的桌面软件经验闭环：

**`screen_match_app`** — 查软件经验（直连 MCP 工具，perception 桶）
- 按 process_name 查询 `.agents/skills/computer_use/apps/*.md`，返回命中文件正文（去 frontmatter）+ staleness 信息
- 用法：`screen_match_app(process_name="qbittorrent.exe")`
- 10 段结构：软件概况/软件识别/UIA 友好度与定位策略/常用快捷键/菜单路径/对话框处理/已知坑/遗留问题/修改历史

**`screen_write_lesson`** — 写软件经验（直连 MCP 工具，闭环关键）
- agent 在桌面软件踩坑或发现最佳实践后调用，自动按 process_name 规范化（去扩展名小写，仅 [a-z0-9-]）、按 10 段结构创建/追加 apps/<process_name>.md
- 自动合并 frontmatter aliases、更新"最后更新"日期、追加"修改历史"行
- section 必须是 10 段之一，content ≤50KB
- 用法：`screen_write_lesson(process_name="qbittorrent.exe", section="已知坑", content="| 配置改 ini 被界面覆盖 | 直接改 ini | 通过界面操作保存 | 2026-08-08 |")`

闭环流程：操作桌面软件 → `list_windows`/`screen_app_list` 自动注入 `app_lessons_hint` 或主动 `screen_match_app` 读经验 → `execute_action`/`batch_actions` 操作 → 踩坑/找到最佳实践 → `screen_write_lesson` 写入 → 下次操作自动加载。

`execute_action` / `batch_actions` / `focus_window` / `screen_accessibility_snapshot` 端点描述中均含 "Tip" 提示，让 agent 不读 AGENTS.md 也能发现 apps 经验可用。

### 知识老化规则（防陈旧经验腐化任务）

browser sites 和 computer_use apps 经验文件均含"最后更新"日期，match 响应和 hint 注入含 `staleness_level`：

| staleness_level | 条件 | 含义 | agent 行为 |
|-----------------|------|------|-----------|
| `fresh` | <90 天 | 经验新鲜 | 可直接参考 |
| `warn` | 90-180 天 | 可能过时 | 验证后再用；验证通过后用 write_lesson 更新"最近验证日期"列 |
| `critical` | >180 天 | 高度可能过时 | 必须验证关键坑是否仍适用后再参考 |

陈旧的"坑"记录反而会阻碍任务（如网站/软件已修复某 bug 但经验仍标注为坑）。`browser_write_lesson` / `screen_write_lesson` 写入会自动重置 staleness 为 fresh。"已知坑"表格含"最近验证日期"列，agent 验证某条坑仍适用时可用 write_lesson 更新该列。

## REST 路径 → MCP 工具名映射规则

**核心规则：MCP 工具名 = FastAPI 路由的 `operation_id` 参数**（不是路径去斜杠！）。所有路由在 `server/*.py` 中显式指定了 `operation_id`，IDE 中工具名显示为 `mcp_localagent_<operation_id>`（`mcp_localagent_` 是 server_name 前缀，调用时省略）。

**命名规律**：大部分 `operation_id` 遵循 `<模块>_<动作>` 模式，可直接从路径推断：

| REST 路径 | operation_id | MCP 工具名 | 规律 |
|-----------|--------------|-----------|------|
| `GET /apikey/status` | `apikey_status` | `apikey_status` | 模块_动作 |
| `POST /apikey/test` | `apikey_test` | `apikey_test` | 模块_动作 |
| `GET /memory/list` | `memory_list` | `memory_list` | 模块_动作 |
| `POST /memory/{key}` | `memory_set` | `memory_set` | 模块_动作（动作语义化） |
| `GET /ocr/status` | `ocr_status` | `ocr_status` | 模块_动作 |
| `POST /ocr/path/json` | `ocr_path` | `ocr_path` | 模块_输入方式（/json 后缀省略） |
| `POST /exec/python` | `exec_python` | `exec_python` | 模块_动作 |
| `GET /browser/tabs` | `browser_list_tabs` | `browser_list_tabs` | 模块_动作_对象 |
| `POST /mindforge/search` | `mindforge_search` | `mindforge_search` | 模块_动作 |

**常见例外**（operation_id 与路径字面不一致，需查代码确认）：

| REST 路径 | operation_id | 说明 |
|-----------|--------------|------|
| `POST /screen/capture` | `capture_screen` | 语序反转（动作_模块），format=inline 返回 ImageContent |
| `POST /screen/ocr` | `screen_ocr` | 截图+OCR 一体化；文字 bbox 定位首选，走 advanced_tool 网关 |
| `POST /screen/control/request` | `screen_request_control` | 当前任务授权入口，直连 MCP |
| `POST /screen/control/release` | `screen_release_control` | 当前任务授权收回，直连 MCP |
| `GET /screen/windows` | `list_windows` | 加 `list_` 前缀，省略 `screen_` |
| `GET /screen/snapshot` | `screen_snapshot` | 模块_动作 |
| `POST /screen/action` | `execute_action` | 动作语义化（非 `screen_action`） |
| `POST /screen/focus-window` | `focus_window` | kebab-case 路径，snake_case 有兼容别名 |
| `POST /screen/batch-actions` | `batch_actions` | 同上 |
| `POST /screen/wait-for` | `screen_wait_for` | 同上 |
| `POST /vision/understand` | `understand_image` | 不是 `understand_screen` |
| `POST /shutdown` | `shutdown_server` | 全局接口，无模块前缀 |

**`_form` 后缀含义**：OCR 模块部分接口有 `*_form` 版本（Form 上传，给 curl/网页用），JSON 版本路径加 `/json` 后缀（给 MCP 用）。MCP 直连工具和高级网关都不收录 `_form` 版本。例如 `ocr_file` 是 MCP 工具（路径 `/ocr/file/json`），`ocr_file_form` 是 REST 专用（路径 `/ocr/file`）。

**非直连工具如何访问**：`server/main.py` 的 `_mcp_include` 是直连白名单；未进入白名单且适合 MCP 调用的 REST operation 会收录到高级网关，可通过以下方式访问：

```
localagent_advanced_tool(tool="<operation_id>", params={...})
# 例：localagent_advanced_tool(tool="ocr_set_keep_models", params={"keep": true})
# 例：localagent_advanced_tool(tool="mindforge_preload", params={})
# 例：localagent_advanced_tool(tool="apikey_history", params={})
# 例：localagent_advanced_tool(tool="todos_get", params={"todo_id": "todo_xxx"})  # GET 类免审批
```

**工具选择优先级**：直连工具 → advanced_tool 网关 → template_tool → exec_python。
**禁止**用 exec_python 发 HTTP 调本地 API（触发审批且绕过 safety 分类），详见 AGENTS.md"工具选择决策树"。
`advanced_tool` 调 GET 类端点**免审批**（safety 绑定目标 operation，GET=read_only）；`exec_python` 是 approval_required，每次都触发审批。

**Computer Use 定位优先级**：浏览器 DOM locator → UIA 语义层 → `screen_ocr`/`ocr_path` bbox → `vision_locate`。`understand_image` 默认用于布局和状态描述，`vision_locate` 仅作无文字元素坐标兜底。

**不确定时如何查询**：
- 查 `server/<模块>.py` 中路由的 `operation_id` 参数（最准确）
- 调用 `localagent_list_tools(category="<模块>")` 运行时发现网关子工具
- 调用 `localagent_list_templates(category="<分类>")` 运行时发现模板

## MCP 工具排除清单

以下 REST API 端点未直接暴露为 MCP 工具（为控制直接工具总数），但**可通过 `localagent_advanced_tool` 网关或 REST API 调用**；`_form` 和 SSE 流式端点只保留 REST，不进入网关。

### 模型管理（低频运维操作）

| 排除的 MCP 工具名 | REST API | 说明 |
|------------------|----------|------|
| `ocr_set_keep_models` | `POST /ocr/models/keep` | 设置 OCR 模型常驻内存（写回 ModelLifecycleManager `restore_preload` 配置） |
| `ocr_unload_models` | `POST /ocr/models/unload` | 卸载 OCR 模型（委托 `manager.manual_unload("ocr")`） |
| `vision_set_keep_models` | `POST /vision/models/keep` | 设置视觉模型常驻内存 |
| `vision_unload_models` | `POST /vision/models/unload` | 卸载视觉模型 |
| `mindforge_preload` | `POST /mindforge/preload` | 预加载搜索引擎（委托 `manager.manual_load("mindforge_searcher")`） |
| `mindforge_unload` | `POST /mindforge/unload` | 卸载搜索引擎（委托 `manager.manual_unload("mindforge_searcher")`） |
| `mindforge_convert` | `POST /mindforge/convert` | 单文档转换（守护进程，未启动时自动拉起） |
| `mindforge_daemon_stop` | `POST /mindforge/daemon/stop` | 停止守护进程 |
| `mindforge_daemon_status` | `GET /mindforge/daemon/status` | 守护进程状态 |

### Model Lifecycle Manager 统一控制面（`/models/*`）

**新增**：统一管理后端进程内 4 个本地模型（PaddleOCR / memory_embedding / guide_embedding / mindforge_searcher）生命周期的控制面。设计文档：`temp/sdd/model-lifecycle-manager/design.md`。环境约束与显存释放实测详见 `docs/environment-constraints.md` 的 "Model Lifecycle Manager 显存释放实测" 段。

**审批级别**（已在 `server/route_tags.py` 的 `_APPROVAL_POST_PATTERNS` 增 `/models` 前缀 + `_MODERATE_EXCLUDE_PATTERNS` 镜像，**所有 POST 端点判为 `approval_required`**，避免 fail-open 让 agent 无审批卸载 OCR 瘫痪 Computer Use 主路径）：

| 端点 | 方法 | 审批级别 | 说明 |
|------|------|----------|------|
| `/models` | GET | read_only | 列出全部注册模型状态（model_id/resource/loaded/footprint_mb/priority/evictable/in_flight/loaded_at/last_load_ms） |
| `/models/{model_id}/load` | POST | approval_required | 手动加载模型（豁免冷却/降级，仍受压力态约束） |
| `/models/{model_id}/unload` | POST | approval_required | 手动卸载模型 |
| `/models/pause` | POST | approval_required | 暂停压力监控（手动超驰通道；PAUSED 期间准入放行且暂停逐出） |
| `/models/resume` | POST | approval_required | 恢复压力监控 |
| `/models/pressure` | GET | read_only | 各资源压力态 + used/total + 最近迁移事件 |

**支持的 model_id**：`ocr` / `memory_embedding` / `guide_embedding` / `mindforge_searcher`。

**MCP 调用方式**：通过 `localagent_advanced_tool` 网关（GET 类免审批，POST 类触发审批弹窗）：

```
# 列出所有模型状态（GET 免审批）
localagent_advanced_tool(tool="models_list", params={})

# 查询压力态（GET 免审批）
localagent_advanced_tool(tool="models_pressure", params={})

# 手动加载 OCR（POST 触发审批）
localagent_advanced_tool(tool="models_load", params={"model_id": "ocr"})

# 手动卸载 MindForge 搜索引擎（POST 触发审批）
localagent_advanced_tool(tool="models_unload", params={"model_id": "mindforge_searcher"})

# 暂停压力监控（POST 触发审批，PAUSED 期间准入放行且暂停逐出）
localagent_advanced_tool(tool="models_pause", params={"resource": "gpu"})
```

**手动卸载记忆 embedding 的恢复步骤**（重要：默认钉住规避，手动卸载走审批门槛）：

1. `POST /models/memory_embedding/load` 重新加载 embedding engine
2. `POST /memory/rebuild_vector_index` 补卸载窗口内缺失的向量索引（卸载期间写入的消息会永久缺失向量索引，记忆语义检索静默降级为 BM25-only）

**Health 聚合**：`/health.model_manager` 低频枚举（不放高频计数器避免 client 指纹漂移），返回 `{enabled, gpu_state, cpu_state, loaded_ids, reload_degraded_ids, last_transition_ts}`；`HealthResponse` schema 已显式声明 `model_manager` 字段。

### 远程 VL 与网关策略

| 排除的 MCP 工具名 | 替代方案 | 说明 |
|------------------|----------|------|
| `vl_file` | `ocr_file` / `ocr_path` | 远程 VL 文档解析，走 ModelScope API-Inference（默认模型见 `GET /vision/status`） |
| `vl_path` | `ocr_file` / `ocr_path` | 同上 |

> **历史**：`vl_base64`（base64 输入版）和 `ocr_base64`（JSON 版）已删除，零 MCP 调用。Form 版 `ocr_base64_form` 保留供脚本调用，但不在 MCP 直连白名单中。

### 零调用工具排除（REST 仍可用，MCP 网关不暴露）

基于 `mcp_stats.json` 调用统计，以下零调用工具从 MCP 网关排除（REST 端点仍可正常调用）：

| 分类 | 排除工具 | 原因 |
|------|---------|------|
| **基础设施/系统** | `shutdown_server`, `health_health_get`, `mcp_stats`, `mcp_stats_reset`, `get_config_config_get`, `update_config_api_config_post`, `llm_pool_*`(7), `set_keep_awake`, `clear_skip_cache` | 非 agent 使用：监控/运维/脚本驱动 |
| **v6-lite 引擎专用** | `llm_pool_chat_tools`, `llm_pool_stream` | v6-lite 对话引擎 LLMGateway/SSE 专用端点，不进 agent tool catalog（防递归，REST 仍可用） |
| **GUI/脚本专用** | `activity_daily_*`(4), `user_message_*`(4) | 由 GUI 面板和日报系统管理 |
| **零调用状态** | `system_status`, `keep_awake_status`, `docviewer_status`, `memory_maintain_status` | 通过 `/health` 获取聚合状态 |
| **入站网关** | `inbound_v1_models`, `inbound_v1_chat_completions`, `inbound_keys_list`, `inbound_keys_create`, `inbound_keys_update`, `inbound_keys_delete`, `inbound_calls_list`, `inbound_stats` | OpenAI 兼容中转端点，消费者是外部 harness（Cline/Cherry Studio）与「入站管理」GUI 面板，不进 agent 工具列表 |

### 脚本驱动功能（非 agent 直接调用）

> **记账审核**已从主后端剥离，迁移为独立服务（端口 8780）。原 `/accounting/*` 端点不再注册到主后端，因此无对应的 MCP 工具需排除。记账审核通过独立服务 Web 页面 `http://127.0.0.1:8780/` 操作，详见 [accounting skill](file:///<project_root>/.agents/skills/accounting.md)。

## v6-lite-streaming-gui：真流式 + DoomLoop + SessionFacade

**SDD 流程 `temp/sdd/v6-lite-streaming-gui/`**（7 ticket 全部 completed；产物目录已随 temp 清理，行为描述以本节和 `docs/chat-engine.md` 为准）。三项打包：后端真 SSE 流式 + DoomLoop thinking_delta 尾重复检测 + SessionFacade 5 方法封装。

### 后端真流式（D02/D03/D04/D14）

`/llm/pool/stream` 端点（operation_id=`llm_pool_stream`，`x-agent-callable=false` 防递归，在 `GATEWAY_EXCLUDE`）从 v6-lite W5 的"伪流式"（`pool.call()` 一次性拿完整响应再切片 yield）升级为真流式：

- `LLMPool.stream()` 方法对接 provider SSE 流（`stream=True`），复用 `_acquire/_release` + tier 硬匹配 + key 轮换
- provider SSE 事件透传 7 种类型：`text_delta` / `reasoning_delta`（统一为 `thinking_delta`） / `thinking_delta` / `tool_call_delta`（半包累积，完整 tool_call 才 yield） / `usage` / `provider_error` / `done`
- 客户端断开 → `await request.is_disconnected()` → 取消下游 provider 请求 → 释放 key/semaphore（`try/finally` 保证）

### DoomLoopDetector（D10/D11）

检测 `thinking_delta` 尾重复（spec D10/D11）。`client/core/agent/doom_loop.py` `DoomLoopDetector` 在 runner `_stream_llm` 内逐 chunk 喂入 thinking_delta，命中尾重复（`pattern*N` 模式，`tail_size=2000` / `min_repeat_len=50` / `repeat_threshold=3`）时（**B1 重构后不重试**）：

1. mid-stream abort（中断当前 SSE 流）
2. 已收到的 partial text 落库为 `source="partial"` 消息，streaming 事件标 `invalidated`
3. 写一条用户可见但 LLM 不可见的系统警告（`source="system_warning"`）
4. 写 `transition(doom_loop_detected)` 事件，会话状态置 `interrupted`，`stop_reason="doom_loop_detected"`

用户看到警告后自行决定换种问法或新开对话（用户消息即隐式 nudge）。`thinking_delta` 缺失时 DoomLoop 不生效（不降级到 `text_delta`）。

### SessionFacade（D12/D13）

`client/core/agent/facade.py` 纯 Python Facade 封装 SessionRunner（不依赖 Qt，GUI worker 通过它调用引擎）：

| 方法 | 行为 |
|------|------|
| `start(session_id, text, mode)` | 创建 store + runner + 写 user message + 调 runner.run() |
| `steer(session_id, text)` | 写 `source=steer` 消息（role=user），runner 当前工具批次结束后下一轮读到（不打断） |
| `interrupt(session_id)` | 调 runner.interrupt_event.set() |
| `approve(approval_id, decision, feedback)` | POST `/command-guard/decision` 提交审批决定 |
| `events(session_id, after_seq)` | 从 EventStore 读 after_seq 之后的事件（AsyncIterator） |

ChatPanel `_ChatWorker` 改用 SessionFacade（不再直接构造 SessionRunner）。steer UI（输入区"引导"按钮）+ thinking 显示 toggle（独立可折叠气泡）+ 打字机三档 toggle（close/fast/normal，默认 fast）。

### chat-panel-v2 重设计（UI 层升级）

**SDD 流程产物 `temp/sdd/chat-panel-v2/`（12 ticket 全部 completed）已随 temp 清理**，设计沉淀以本节和 `docs/chat-engine.md` 为准。基于 wip_45dd436b（防关机/崩溃丢失会话）扩展为完整对话面板重设计，484 项 chat 相关测试全过。

**EventStore 列扩展**（schema 版本以 `client/core/agent/event_store.py` 的 `_SCHEMA_VERSION` 为准）：
- sessions 表有 `group_name TEXT`（NULL=未分组）+ `pinned INTEGER DEFAULT 0`（0/1）两列
- messages 表有 `model TEXT` 列（assistant 消息模型名，气泡下方小字显示）
- 迁移幂等：ALTER TABLE ADD COLUMN（旧 DB 自动补默认值）

**UI 主体**（`client/panels/chat.py`）：
- **开始页**：模板 tab + 最近 5 对话 + 分组选择器
- **模板管理**：`data/chat_templates.json`（schema `{id, name, prompt, skills[], created_at, updated_at}`），左列表+右编辑区（全量 skill 复选框）+ 失焦自动保存
- **侧边栏树形**：QTreeWidget 三区（置顶/分组/未分组），三点菜单（删除/重命名/移动/置顶/导出），搜索框实时过滤
- **消息时间线块结构**：6 块类（_UserBubble/_AssistantTextBlock/_ThinkingBlock/_ToolCallBlock/_SystemBlock + _TimelineBlock 基类），每段独立块按 seq 时间顺序排列，sticky 标题栏显示当前可见可折叠块标题
- **对话控制三模式**：发送/队列/引导 3 tab，空闲态只显示发送，运行态默认引导；队列只允许 1 条（重复禁用队列 tab），超长截断+编辑取消；队列持久化（source="queue" + visible=0，启动恢复 banner）
- **顶栏重设计**：last_updated + 标题（截断+tooltip）+ status_label（语义着色）+ cost/token（_format_token_count 单位转换 B/K/M/T）+ 打字机效果切换 + 刷新按钮；移除 copy_last_btn/thinking_toggle_btn/model_selector（model_selector 移到输入区）
- **导出**：弹 QFileDialog 选路径+格式（Markdown/JSON/两者都导），MD 渲染 tool_calls/thinking 为引用块，JSON 含 session+messages+tool_calls+events 完整备份
- **hover 操作按钮**：_UserBubble 加 Copy/Delete 信号（source="user" 才显示），_AssistantTextBlock 加 Copy 信号（仅 finalize 后激活）

**防关机 closeEvent + 启动恢复**（`client/core/app.py` + `chat.py` `_reconcile_on_startup`）：
- closeEvent 中断活跃 runner + 更新会话状态为 interrupted（非阻塞毫秒级）
- EventStore WAL 每写即 commit，崩溃时 status 仍 streaming/awaiting_tools
- reconciler 在 `_init_read_store` init() 后立即调，静默处理残留 active sessions/hanging tool_calls/unmerged streaming events，顶部 banner 提示"检测到 N 个异常退出的会话，已自动恢复"

**lib/ui/tokens 新增语义底色**（`lib/ui/tokens.py`）：`INFO_WASH` + `SUCCESS_WASH`（暗色 wash，system summary 块用），修复 chat._SystemBlock hex 硬编码违规

### 配置项

`[runner]` 段新增：

- `typewriter_mode`：close / fast（默认） / normal
- `typewriter_normal_interval_ms`：normal 档 QTimer 间隔（默认 16ms = 60fps）
- `stream_idle_timeout_secs`：SSE 流空闲超时（默认 90s）
- `stall_detection_window_secs`：stall 检测窗口（**默认 0 = 禁用**；2026-09-12 起默认关闭——推理模型服务端静默思考期不吐 token 属常态，token 速率检测会误杀正常流，真死流由空闲超时兜底；需要时可显式配置开启）

`[runner.doom_loop]` 子段：`enabled` / `tail_size` / `min_repeat_len` / `repeat_threshold` / `max_retries` / `backoff_base_ms` / `backoff_jitter_ms`

---

## exec_python 统一 terminal 机制 + 3 分钟唤醒 LLM

**v15 起 exec_python 改为异步 terminal 机制**（SDD 流程 `temp/sdd/exec-terminal-unify/`）。exec_python 不再同步阻塞，而是写 `temp/exec_<uuid>.py` → 调 `terminal_spawn` 起后台子进程 → 返回 `{terminal_id, status, temp_file, pid}`。

**v16 内联等待**（`temp/sdd/exec-python-inline-wait/`）：exec_python 在 spawn 成功后默认内联等 `inline_wait_secs` 秒（`[server] exec_python_inline_wait_secs`，默认 10s）。子进程在 N 秒内结束 → 响应 `status="done"` + 完整 `stdout/stderr/exit_code/elapsed`（agent 一步搞定，无需再调 inspect）；超时仍 running → 响应 `status="running"` + `terminal_id`（同 v15，走两步流程）。`inline_wait_secs=0` 禁用回退 v15 立即返回行为。

### 5 个工具（全部 MCP 直连）

| 工具 | 参数 | 行为 | SessionRunner 包装 |
|------|------|------|---------------------|
| `exec_python` | `code`, `cwd?`, `environment?`, `python_path?` | 写 temp .py → 调 terminal_spawn → 默认内联等 10s：done 返回完整 stdout/exit_code，running 返回 terminal_id | **是**：status=done 短路直接用 result；status=running 自动 exec_inspect(tid, timeout=180) |
| `exec_inspect` | `tid`, `timeout?` | 不传 timeout：立即返回快照；`timeout>0`：**新输出/子进程结束/N秒到任一触发立即返回**（spec D4） | 否 |
| `exec_kill` | `tid` | 终止子进程（SIGKILL 等价，幂等） | 否 |
| `exec_send_input` | `tid`, `text` | 向 stdin 发送文本+换行 | 否 |
| `wait` | `seconds` | 纯 sleep（通用工具，不查 terminal 状态，spec Anti-Cheat 11） | 否 |

### v16 内联等待（exec_python 默认行为）

- **短任务（<10s）**：exec_python 内联等待返回 `status="done"` + `stdout/stderr/exit_code/elapsed`，agent 一步拿到结果
- **长任务（>10s）**：exec_python 超时返回 `status="running"` + `terminal_id`，agent 调 `exec_inspect(tid, timeout=N)` 等结果
- **禁用**：`[server] exec_python_inline_wait_secs = 0` 回退 v15 立即返回（status=running）
- **输出截断**：done 路径 stdout/stderr 超 8000 字符自动截断（复用 `_truncate_output`），含截断标记
- **SessionRunner 短路**：`_execute_with_wakeup` 检测 `status=="done"` 时直接用 result，跳过 inspect 包装

### 3 分钟唤醒机制（仅 v6-lite SessionRunner 场景，status=running 路径）

- **仅第一次**：exec_python 返回 status=running 后，SessionRunner 自动调 `exec_inspect(tid, timeout=180)` 等 3 分钟
- **3 分钟内子进程结束**：tool_result 含完整 stdout/stderr/exit_code，正常回灌
- **3 分钟到仍 running**：唤醒 LLM，构造 prompt 含：当前 stdout/stderr 尾部 + 已耗时 + 4 个干预工具清单 + wait/inspect 区别提示
- **LLM 决策后**：执行其 tool_calls（exec_inspect/exec_kill/exec_send_input/wait）直到 exec_kill 或子进程结束
- **后续不再自动唤醒**：LLM 自己决定何时 inspect/wait
- **配置**：`[runner] long_tool_first_check_secs = 180`（默认 3 分钟，0=禁用）

### exec_inspect 的 timeout 语义（spec D4）

`timeout>0` 时**任一触发立即返回**（不是"纯 N 秒阻塞"）：
1. 子进程结束（status 变为 done/killed/timeout/error）
2. stdout/stderr 有新输出（chars 数变化）
3. N 秒到期

实现用 `asyncio.sleep(0.2)` 轮询（每 200ms 检查一次），任一条件满足立即返回。已结束的 terminal 调用时立即返回，不阻塞。

### wait vs exec_inspect 区别

- **`wait(seconds)`**：纯 sleep，不查 terminal 状态。LLM 想纯等待时用 `wait(N) + exec_inspect(tid, timeout=0)` 两步流程
- **`exec_inspect(tid, timeout=N)`**：等待新输出或子进程结束或 N 秒到。LLM 想频繁看进展时用（新输出即返回）

### terminal 生命周期三层保障（spec D11）

1. **LLM 主动 exec_kill**：LLM 决定终止时调 `exec_kill(tid)`
2. **SessionRunner session 结束自动 kill**：session finalize/interrupted/failed 时，`_cleanup_terminals()` 遍历 `_session_terminals` kill 所有残留 terminal
3. **后端 TTL 清理**：`_terminals` dict 中 finished/killed 状态且 `last_activity_at` 超过 30 分钟的记录移除（running 不移除），关联的 .py 临时文件 + stdout/stderr 日志文件一起删

### 裸 agent 使用流程（非 v6-lite 场景）

裸 agent（直接调 MCP 工具，不走 SessionRunner）使用 exec_python：
- **短任务（<10s）**：`exec_python(code=...)` → 直接拿 `status="done"` + stdout（一步搞定）
- **长任务（>10s）**：`exec_python(code=...)` → 返回 `status="running"` + terminal_id → `exec_inspect(tid=terminal_id, timeout=N)` 等结果（N 秒内子进程结束或新输出即返回）

裸 agent 不享受 3 分钟自动唤醒机制，需自己决定何时 inspect/wait。

## 内联截图与 ImageContent（多模态 LLM 支持）

`capture_screen(format=inline)` 返回 MCP 标准 `ImageContent` 块（base64 压缩 JPEG 图片，~30-200KB），多模态 LLM（Claude/GPT-4o/Gemini）可直接"看"到截图。实现方式：fastapi-mcp 0.4.0 默认只返回 TextContent，已在 `server/core/mcp_gateway.py` 中 monkey-patch `_execute_api_tool`，检测响应中的 `mcp_image_block=True` 标记并转为 ImageContent。纯文本模型仍能从 TextContent 摘要获取元信息。

## MCP 工具调用统计

后端自动记录每次 MCP 工具调用，持久化到 `server/memory/mcp_stats.json`。

### 查询统计

- `mcp_stats` MCP 工具：返回每个工具的调用次数、平均耗时、错误率
- `GET /mcp/stats` REST API：同上
- `POST /mcp/stats/reset`：重置统计数据

### 用途

项目整理时用数据说话：
1. **识别低频工具**：调用次数接近 0 的工具考虑排除出 MCP
2. **检查远程 VL 状态**：VL 工具调用频繁时关注 `/health` 的 `vl_available`，确认远程 provider 是否冷却中
3. **发现高频错误**：error_count 高的工具可能有问题需要修复
