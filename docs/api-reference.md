# 后端 API 速查

服务地址：`localhost:8766`（FastAPI）| OpenAPI 文档：`http://127.0.0.1:8766/docs`

> agent 优先用 MCP 工具名调用（`mcp_localagent_*`），REST 路径仅用于 curl/脚本。完整端点列表调 `agent_guide(task='...')` 或访问 `/docs`。

## 全局

| 路径 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 全局健康检查（含所有模块状态） |
| `/config` | GET/POST | 获取/更新配置 |
| `/shutdown` | POST | 优雅关闭服务器 |
| `/mcp/stats` | GET | 查询 MCP 工具调用统计 |
| `/mcp/stats/reset` | POST | 重置 MCP 工具调用统计 |

## OCR

| 路径 | 方法 | 说明 |
|------|------|------|
| `/ocr/status` | GET | OCR 模型状态 |
| `/ocr/file/json` | POST | 经典 OCR（JSON Body，MCP 直连） |
| `/ocr/base64` | POST | 经典 OCR（Form 上传） |
| `/ocr/path/json` | POST | 经典 OCR（本地路径） |
| `/ocr/vl/file/json` | POST | 远程 VL 文档解析（JSON Body） |
| `/ocr/vl/path/json` | POST | 远程 VL（本地路径） |
| `/ocr/models/keep/json` | POST | OCR 模型管理：保留指定模型 |
| `/ocr/models/unload/json` | POST | OCR 模型管理：卸载指定模型 |
| `/ocr/models/preload/json` | POST | OCR 模型管理：预加载指定模型 |

## Agent / Guide

| 路径 | 方法 | 说明 |
|------|------|------|
| `/agent/status` | GET | Agent 配置状态（模型层级） |
| `/guide` | GET | Agent Guide（task 参数获取任务指导） |
| `/guide/usage` | GET | Agent Guide 使用统计 |
| `/agent/score` | POST | LLM 内容评分（6 维度） |
| `/agent/chat` | POST | LLM 通用对话 |

## 代码执行

| 路径 | 方法 | 说明 |
|------|------|------|
| `/exec/status` | GET | Exec 模块状态 |
| `/exec/python` | POST | 执行 Python 代码（万能回退） |
| `/exec/apply-patch` | POST | 应用补丁（支持 dry-run、回滚） |
| `/exec/cmd` | POST | 执行 Shell 命令 |
| `/terminals/spawn` | POST | 启动后台终端会话 |
| `/terminals/{tid}` | GET/DELETE | 查看/删除终端 |
| `/terminals/{tid}/output` | GET | 增量读取终端输出 |
| `/terminals/{tid}/input` | POST | 向终端发送输入 |
| `/terminals/{tid}/kill` | POST | 终止终端 |
| `/output` | GET | 列出缓冲的 exec 输出 |
| `/output/{exec_id}` | POST | 查看 exec 输出（full/range/search） |

## 屏幕控制

| 路径 | 方法 | 说明 |
|------|------|------|
| `/screen/status` | GET | 屏幕状态（管理员权限、紧急停止） |
| `/screen/windows` | GET | 列出所有可见窗口 |
| `/screen/capture` | POST | 截图（inline=ImageContent / path / base64；返回 snapshot_id，帧缓存供 zoom/局部 OCR 复用） |
| `/screen/ocr` | POST | 截图+本地 OCR，返回文字 bbox（Computer Use 首选；支持 snapshot_id 帧复用 + region 局部 OCR） |
| `/screen/zoom` | POST | 最近帧局部裁剪放大（不重截图；inline=ImageContent / path） |
| `/screen/action` | POST | 键鼠操作（含安全检查；click 默认 UIA 融合 strategy=auto；支持 mouse_down/up/move 分段原语；支持 verify_prompt） |
| `/screen/focus-window` | POST | 强力激活窗口 |
| `/screen/batch-actions` | POST | 批量键鼠操作（click 步骤支持 strategy 融合） |
| `/screen/clipboard` | GET/POST | 剪贴板读（截断 2000）/写（danger block 拦截；受会话权限保护） |
| `/screen/scroll-capture` | POST | 滚动截图拼接 |
| `/screen/wait-for` | POST | 条件等待（OCR/VL 检测文字出现） |
| `/screen/snapshot` | POST | 桌面快照（窗口列表+OCR文本，无 base64） |
| `/screen/analyze` | POST | 图像特征分析（颜色/亮度/异常告警） |
| `/screen/preview/action` | POST | 点击位置预览（红点标记） |
| `/screen/overlay` | POST | 显示操作提示覆盖层（持久显示，文案/颜色/倒计时由 SessionManager.status 推送 tick 自动刷新；传 message 字段已弃用） |
| `/screen/control/request` | POST | 请求用户介导的当前任务授权（确认窗复选框默认未勾选） |
| `/screen/control/release` | POST | 主动收回当前任务授权（幂等） |

> 第一次副作用 Computer Use 操作前优先调 `screen_request_control(task_description=..., source="agent")`。授权只对当前任务普通控制生效，空闲 300 秒自动撤销；危险操作和安全阻断不绕过。`verify_prompt` 仅在高风险/状态难判断时使用。详见 `computer_use.md`。

## 视觉AI

| 路径 | 方法 | 说明 |
|------|------|------|
| `/vision/status` | GET | 视觉模型状态 |
| `/vision/understand` | POST | VL 图像描述/状态理解（非坐标定位） |
| `/vision/locate` | POST | 无文字元素坐标兜底（自动反归一化） |
| `/vision/providers` | GET | VL provider 列表与运行时状态 |

## 记忆

| 路径 | 方法 | 说明 |
|------|------|------|
| `/memory/status` | GET | 记忆模块状态 |
| `/memory/list` | GET | 列出所有记忆 key |
| `/memory/{key}` | GET/POST/DELETE | 读取/写入/删除记忆 |
| `/memory/search` | POST | 语义搜索（三源召回+证据组织） |
| `/memory/maintain` | POST | 手动触发记忆维护 |
| `/memory/maintain/status` | GET | 维护器状态 |
| `/memory/search/traces` | GET | 检索过程追踪列表 |
| `/memory/search/traces/{trace_id}` | GET | 单条检索详情 |
| `/memory/search/traces/cleanup` | DELETE | 清理过期 trace |
| `/memory/evidence/recent` | GET | 最近证据集列表 |
| `/memory/evidence/{evidence_id}` | GET | 单条证据详情 |
| `/memory/evidence/cleanup` | DELETE | 清理过期证据 |

> POST `/memory/{key}` 支持 v3 结构化字段：`fact_type`/`occurred_at`/`consumption_contexts`/`trigger_keywords`。写记忆时必须填后两个字段确保消费闭环。

## 待办 / WIP

| 路径 | 方法 | 说明 |
|------|------|------|
| `/todos` | GET/POST | 列出/创建待办 |
| `/todos/due` | GET | 获取到期周期任务 |
| `/todos/{id}` | GET/PUT/DELETE | 待办详情/更新/删除 |
| `/todos/{id}/done` | POST | 标记完成（自动算 next_due_at） |
| `/todos/{id}/check_trigger` | POST | 检查 triggered 任务触发条件 |
| `/wip` | GET/POST | 列出/创建 WIP 任务 |
| `/wip/{id}` | GET/PUT/DELETE | WIP 详情/更新/删除 |

## 浏览器

> 重构后共 17 个路由，原 6 个 legacy 端点（`/browser/open`、`/browser/click_element`、`/browser/fill_input`、`/browser/wait_for_load`、`/browser/extract_text`、`/browser/screenshot_element`）已删除，功能由新端点替代。详见 `server/browser/routes.py`。

### Session 管理

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/status` | GET | 浏览器连接状态（page/target_count/context_count） |
| `/browser/tabs` | GET | 列出标签页（含稳定 target_id / opener_target_id / opener_url） |
| `/browser/close` | POST | 关闭标签页（urls 模糊匹配 + tab_ids 精确匹配，all_matches 控制单/批量） |
| `/browser/sessions` | POST | 创建持久会话（热态 <250ms，自动注入站点经验） |
| `/browser/sessions` | GET | 列出活跃 session |
| `/browser/sessions/close` | POST | 关闭 session（close_page=false 默认只释放句柄） |

### 核心操作

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/snapshot` | POST | ARIA 可访问性快照（role/name/value，返回 node_id 供 action 重放） |
| `/browser/action` | POST | 统一动作（click/double_click/fill/type/press/select/check/uncheck/hover/scroll_into_view） |
| `/browser/wait_for` | POST | 状态等待（selector/text/url/load_state/popup/filechooser/dialog） |
| `/browser/wait_and_action` | POST | 原子 wait+trigger（popup/filechooser/dialog 场景同请求完成，仅 session 模式） |
| `/browser/navigate` | POST | 导航（goto/back/forward/reload，分离 navigation_completed 与 wait_timeout；reload + force_beforeunload=true 触发 beforeunload dialog） |
| `/browser/handle_dialog` | POST | 手动处理 JS dialog（accept/dismiss + prompt 文本输入，2026-08-06 新增） |
| `/browser/set_http_credentials` | POST | 动态设置/清除 HTTP Basic Auth 凭证（context 级，2026-08-06 新增） |
| `/browser/grant_permissions` | POST | 显式预授权权限（geolocation/notifications/camera 等，避免弹原生对话框，2026-08-06 新增） |

### 辅助功能

| 路径 | 方法 | 说明 |
|------|------|------|
| `/browser/evaluate` | POST | 只读 JS 执行（正则拦截 location/cookie/fetch/XHR/eval/innerHTML= 等写操作） |
| `/browser/console_logs` | POST | 控制台日志（session 模式持久缓冲 + since_cursor 增量读；无 session 一次性 capture_ms 抓取） |
| `/browser/screenshot` | POST | 截图（shot_type=viewport/full_page/element；return_base64 控制 base64/仅路径） |
| `/browser/match_site` | POST | 按域名匹配 `browser_lessons/sites/*.md` 站点经验库 |
| `/browser/find_url` | POST | 搜索调试浏览器书签/历史（auto_open=true 一键开 session） |
| `/browser/selector_stats` | POST | 查询选择器成功率（按 domain/target_type 聚合） |

> **关键约束**：
> - `browser_session_create(url=...)` 自动按 URL hostname 匹配 `browser_lessons/sites/*.md` 站点经验
> - snapshot 的 `node_id` 可直接传给 `browser_action`，缓存 60s 或 URL 变化后失效返回 `STALE_NODE`；agent 显式 `verify_dom_freshness=true` 可感知同 URL 下 DOM mutation
> - `dialog` 触发后由 `_on_dialog` 持久监听器存入 session 队列（不立即 dismiss）；agent 用 `browser_handle_dialog` 主动 accept/dismiss + 输入 prompt 文本；5 分钟未处理由超时兜底自动 dismiss
> - `popup`/`filechooser` 跨 HTTP 请求时序不可靠，必须用 `browser_wait_and_action` 原子 API
> - `browser_evaluate` 拦截写操作（location/cookie/fetch/XHR/eval/innerHTML= 等），写操作走 `browser_action`
> - SPA back/forward 可能 `wait_timeout=true` 但 `navigation_completed=true`（不是失败）
> - **dialog 阻塞检测双通道**（2026-08-06 新增）：`browser_status` 主动查询（聚合所有 session 的 `has_pending_dialog`/`pending_dialogs`）+ 所有操作端点预检（`browser_action`/`browser_navigate`/`browser_snapshot`/`browser_evaluate`/`browser_screenshot`/`browser_wait_for`/`browser_wait_and_action` 有 pending dialog 时返回 `blocked_by_dialog=true` + `blocking_dialog` 信息而不调 page 操作；例外：`wait_type="dialog"` 不预检）
> - **beforeunload 弹窗**：`browser_navigate(action="reload", force_beforeunload=true)` 用 `page.close(run_before_unload=True)` 触发 beforeunload dialog；agent 调 `handle_dialog(accept)` 后页面关闭，再调 `browser_navigate(action="goto", url=<原 url>)` 重开；`handle_dialog(dismiss)` 则页面保留
> - **HTTP Basic Auth**：`browser_set_http_credentials(session_id, username, password)` 预设凭证，避免 401 弹原生对话框阻塞；`clear=true` 清除（context 级，不落盘，密码不进日志）
> - **权限预授权**：`browser_grant_permissions(session_id, permissions=["geolocation", ...])` 显式预授权，避免站点请求权限弹原生对话框；`origin=None` 时用当前 session page 的 origin
> - 同 URL 多标签必须传 `tab_id`（从 `/browser/tabs` 的 `target_id` 拿），否则 `url_pattern` 静默选第一个匹配
> - `browser_action` 失败时通过 `error.error_code` 返回结构化错误（TAB_NOT_FOUND/ELEMENT_NOT_FOUND/STALE_NODE/MULTIPLE_MATCHES/INVALID_SELECTOR/UNSUPPORTED_ACTION/TIMEOUT/BROWSER_DISCONNECTED/EXECUTION_ERROR），phase 标识 connect/locate/act/wait/verify 阶段
> - `browser_selector_stats` 自动记录每次 `browser_action` 调用结果（fire-and-forget，DB 失败不阻塞 action）

## LLM 并发池

| 路径 | 方法 | 说明 |
|------|------|------|
| `/llm/pool/status` | GET | 池状态（per-key/per-model 详情，summary=true 精简） |
| `/llm/pool/models` | GET | 跨 key 聚合 per-model 统计 |
| `/llm/pool/recent-calls` | GET | 近期调用历史（上限 200 条） |
| `/llm/pool/init` | POST | 初始化池（从 keys.json 加载） |
| `/llm/pool/call` | POST | 通过池调用 LLM（完整版） |
| `/llm/pool/call-simple` | POST | 通过池调用 LLM（简化版，返回文本） |
| `/llm/pool/stats` | GET | per-project token 消耗统计 |
| `/llm/pool/health-check` | POST | 执行 key 健康检查 |
| `/llm/pool/health-status` | GET | 查询上次检查状态 |
| `/llm/pool/cleanup` | POST | 清理 works=false 的 key |

> `/llm/pool/models` 和 `/llm/pool/recent-calls` 已加入 `GATEWAY_EXCLUDE`（监控面板专用），agent 用 `/health.llm_pool` 获取聚合状态。详见 `docs/llm-pool.md`。

## 入站网关（Inbound Gateway）

OpenAI 兼容本地中转：外部 harness（Cline / Cherry Studio 等）把 `base_url` 指到 `http://127.0.0.1:8766/v1`、配 `sk-la-` 本地 key，即可用整个模型池。每次调用落 `data/inbound_calls.db`（key/模型映射/上游 key/tokens/TTFT/状态码），统计全部由网关自己的 SQLite 聚合（**不复用** `pool.project_stats`——`pool.stream()` 侧零统计）。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容对话（流式与非流式），需 `sk-la-` key |
| `/v1/models` | GET | OpenAI 兼容模型列表（需 `sk-la-` key） |
| `/inbound/keys` | GET/POST | 入站 key 列表 / 创建（仅 127.0.0.1，无鉴权） |
| `/inbound/keys/{key_id}` | PATCH/DELETE | 更新（启停/改名/限额）/ 删除入站 key |
| `/inbound/calls` | GET | 调用日志（支持 key_id/model/状态筛选分页） |
| `/inbound/stats` | GET | Token 统计聚合（`days` 参数，含 TTFT p50/p95、成功率） |

> 8 个 operation_id 全部在 `server/mcp_whitelist.py` 的 `GATEWAY_EXCLUDE`（不进 MCP 工具列表）。模型解析三段：key 别名 → 池内精确匹配 → 404 带可用列表（无 tier 兜底）。面板见 `client/panels/inbound.py`（入站管理，四页签）。

## 高级工具 / 模板网关

| 路径 | 方法 | 说明 |
|------|------|------|
| `/advanced/run` | POST | 调用低频/运维工具（116 个，通过 `localagent_advanced_tool`） |
| `/advanced/tools` | GET | 列出可用高级工具（含 7 段语义说明） |
| `/advanced/docs/tool` | GET | 单个工具的完整语义说明 |
| `/advanced/docs/project` | GET | 项目级文档索引 |
| `/templates/run` | POST | 执行工作流模板（通过 `localagent_template_tool`） |
| `/templates/list` | GET | 列出可用模板 |

## 其他模块

> 调用频率低，通过 `agent_guide(task='...')` 或 `/docs` 查看完整列表。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/loop/status` | GET | Loop 调度器状态 |
| `/loop/tasks` | GET | 列出所有 Loop 任务 |
| `/loop/tasks/{id}/run` | POST | 手动触发 Loop 任务 |
| `/inbox` | GET/POST | 列出/创建收件箱条目 |
| `/mindforge/status` | GET | MindForge 可用性 |
| `/mindforge/search` | POST | 搜索知识库 |
| `/docviewer/read` | POST | 读取文档内容 |

### 收件箱（inbox）

**inbox 是什么**：Loop 任务（后台周期任务）运行中产生的、需要用户审查/确认的条目队列。SQLite 存储（`data/inbox.db`），REST CRUD。

**典型来源**（`source` 字段）：
- `activity_tracker` — Loop 任务失败告警（首次失败 / 失败累计过半 / 连续失败 5 次自动暂停）
- `auto_shutdown:<task_id>` — 关机已触发 / 已取消通知
- `download_watcher` — 下载文件夹三分类（move/inspect/unknown）待确认
- `loop_manager` — Loop 调度器异常

**条目结构**：`id / source / category / title / description / payload(JSON) / status(pending/resolved/ignored) / resolution(JSON) / created_at / resolved_at`

**状态流转**：`pending`（待处理）→ `resolved`（已解决）/ `ignored`（已忽略）。已解决/忽略超过 10 天的条目由 `CleanupActivityAction` 自动清理（复用 `cleanup_cron`，默认每天 0:30）。

**agent 使用方式**：
- 查询待处理条目：`inbox_list`（MCP 直连，status=pending）/ `inbox_get`（单条详情）—— 走 `localagent_advanced_tool` 网关
- 批量管理（resolve/ignore/delete）：`inbox_batch` 端点，仅监控面板调用，不进 MCP（防膨胀）
- 当用户问"我的 inbox 咋回事 / 收件箱"时，调 `inbox_list(status=pending)` 按来源归类汇总

**注意**：测试 auto_shutdown 等模块时，必须 mock `_push_inbox`，否则测试会真往 inbox 写条目污染收件箱（见 `tests/approval_screen/test_auto_shutdown.py` 的 autouse fixture）。

## MCP

| 路径 | 说明 |
|------|------|
| `/mcp` | MCP 工具接口（39 直连 + 116 网关 + 11 模板） |
