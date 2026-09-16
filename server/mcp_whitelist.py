"""MCP 直连工具白名单 + 分桶架构（v3 精简版）

架构设计（基于调用计数 + 用户决策）：
- 第一层（DIRECT_TOOLS）：高频/核心工具（数量见 DIRECT_TOOLS 长度，勿在注释写死），按 6 桶分组
- 第二层（advanced_tool 网关）：其余 REST operation 自动收进网关
- GATEWAY_EXCLUDE：完全不进 MCP（含 REST-only form 端点、零调用端点、基础设施端点）

v3 调整（基于调用计数 2618 次）：
- 下沉 14 个罕用/零调用工具到网关
- 升入 2 个 UIA 语义层工具（screen_accessibility_snapshot, screen_semantic_action）
- 升入 4 个常用状态查询工具（memory_status, ocr_status, vision_status, exec_cmd）
- browser 新接口推广期保留在第一层（13 个 0 调用但需要让 agent 发现）
- 当前任务授权 request/release 作为 Computer Use 核心入口升入第一层

6 桶设计（从 14 桶合并）：
  core / state / exec / perception / browser / llm
"""

# ============================================================================
# 工具分桶定义（6 桶；直连工具数量见 DIRECT_TOOLS 长度，勿在注释写死）
# 桶内工具按"推荐使用频率"排序，高频在前；桶间按 CATEGORY_ORDER 顺序输出
# ============================================================================

TOOL_CATEGORIES: dict[str, list[str]] = {
    "core": [
        # 入口 + 审批 + 文档（高频核心）
        "agent_guide",                       # 61 次
        "localagent_list_tools",             # 46 次
        "localagent_advanced_tool",          # 165 次（万能网关）
        "localagent_tool_docs",              # 工具语义文档（7 段说明，MCP-only agent 补全）
        "localagent_get_docs",              # 项目文档（AGENTS.md/tools-guide 等，MCP-only agent 补全）
        "command_guard_request_approval",    # 43 次（审批请求）
    ],
    "state": [
        # 记忆 + 待办 + 执行状态（只读查询为主）
        "memory_get",                        # 60 次
        "memory_set",                        # 49 次
        "memory_list",                       # 13 次
        "memory_status",                     # 13 次（v3 升入：记忆系统状态查询）
        "todos_due",                         # 6 次
        "wip_list",                          # 28 次
        "wip_get",                           # 20 次
        "exec_status",                       # 89 次（执行状态查询）
    ],
    "exec": [
        # 代码执行 + 终端会话（spawn+input+kill 核心组合）
        "exec_python",                       # 1232 次（最高频）
        "exec_apply_patch",                  # 114 次
        "exec_cmd",                          # 13 次（v3 升入：一次性命令执行）
        "exec_terminal_spawn",               # 63 次
        "exec_terminal_detail",              # 165 次
        "exec_terminal_input",               # 3 次（核心功能保留）
        "exec_terminal_kill",                # 12 次
        # T00 新增：exec_python 统一 terminal 机制 + 3 分钟唤醒 LLM
        "exec_inspect",                      # 阻塞 inspect（新输出/结束/N秒到任一触发）
        "exec_kill",                         # agent 主动 kill（SIGKILL 等价）
        "exec_send_input",                   # agent 向 stdin 发送文本
        "wait",                              # 纯 sleep（通用工具，不查 terminal 状态）
    ],
    "perception": [
        # 屏幕控制 + OCR + 视觉 AI + UIA 语义层
        # Computer Use P0-5: UIA 升入第一层，与 browser_snapshot 对齐
        "screen_request_control",           # 当前任务授权（用户介导）
        "screen_release_control",           # 主动收回当前任务授权
        "capture_screen",                    # 15 次
        "list_windows",                      # 39 次
        "focus_window",                      # 23 次
        "execute_action",                    # 52 次
        "batch_actions",                     # 25 次
        "screen_snapshot",                   # 18 次
        "ocr_file",                          # 17 次
        "ocr_status",                        # 17 次（v3 升入：OCR 状态查询）
        "understand_image",                  # 41 次（VL 视觉理解）
        "vision_locate",                     # 17 次（视觉定位）
        "vision_status",                     # 22 次（v3 升入：VL 状态查询）
        "screen_accessibility_snapshot",     # UIA 语义快照（P0-5，0 次但核心）
        "screen_semantic_action",            # UIA 语义操作（P0-5，0 次但核心）
        # 软件经验闭环（方法论自更新）：读 + 写（与 browser_match_site/browser_write_lesson 对齐）
        "screen_match_app",                  # 查询 apps/*.md 软件经验（UIA 坑/快捷键/菜单路径）
        "screen_write_lesson",               # 写入 apps/<process_name>.md（agent 自更新方法论）
    ],
    "browser": [
        # 浏览器操作（新接口推广期，13 个 0 调用但保留让 agent 发现）
        "browser_status",                    # 11 次
        "browser_list_tabs",                 # 22 次
        "browser_session_create",            # 0 次（新接口）
        "browser_session_close",             # 0 次（新接口）
        "browser_snapshot",                  # 0 次（新接口，ARIA/DOM 快照）
        "browser_action",                    # 0 次（新接口，语义操作）
        "browser_navigate",                  # 0 次（新接口）
        "browser_wait_for",                  # 0 次（新接口）
        "browser_screenshot",                # 0 次（新接口）
        # 站点经验闭环（方法论自更新）：读 + 写
        "browser_match_site",                # 查询 sites/*.md 经验（DOM 坑/反爬规则）
        "browser_write_lesson",              # 写入 sites/<domain>.md（agent 自更新方法论）
    ],
    "llm": [
        # LLM 对话
        "agent_chat",                        # 12 次
    ],
}

# 桶展示顺序（影响 MCP tools/list 输出顺序，agent 第一眼看到的结构）
CATEGORY_ORDER: list[str] = [
    "core",
    "state",
    "exec",
    "perception",
    "browser",
    "llm",
]

# 反向映射：tool_name -> category（供 agent_guide 分桶展示）
TOOL_TO_CATEGORY: dict[str, str] = {
    tool: cat for cat, tools in TOOL_CATEGORIES.items() for tool in tools
}

# DIRECT_TOOLS 是 TOOL_CATEGORIES 按 CATEGORY_ORDER 展开的派生字段
# 向后兼容：外部模块仍可 `from server.mcp_whitelist import DIRECT_TOOLS`
DIRECT_TOOLS: list[str] = []
for _cat in CATEGORY_ORDER:
    DIRECT_TOOLS.extend(TOOL_CATEGORIES[_cat])
del _cat


GATEWAY_EXCLUDE: set[str] = {
    "localagent_list_tools", "localagent_advanced_tool",
    "localagent_list_templates", "localagent_template_tool",
    # REST-only form endpoints (file upload).
    # ocr_base64/vl_base64 JSON 版已删除（零调用）；Form 版 ocr_base64_form 保留供脚本调用。
    "vl_file_form",
    "ocr_file_form", "ocr_base64_form",
    "exec_terminal_stream",
    # 4-6: browser_set_http_credentials 在 Playwright 1.44+ 下必然失败（API 已移除，
    # 调用即 EXECUTION_ERROR），摘出 MCP 网关避免 agent 反复调用；REST 端点保留，
    # 待凭证注入方案落地后移回
    "browser_set_http_credentials",

    # === browser legacy 接口删除说明（Ticket 07） ===
    # 原 6 个 legacy 接口（browser_open / browser_click_element / browser_fill_input /
    # browser_wait_for_load / browser_extract_text / browser_screenshot_element）已删除，
    # 不再出现在 DIRECT_TOOLS、GATEWAY_EXCLUDE 或 advanced_tool 网关中。
    # 功能由新端点完全替代：browser_action / browser_navigate / browser_screenshot /
    # browser_evaluate / browser_wait_for / browser_snapshot / browser_console_logs。
    # 桶间展示顺序见 TOOL_CATEGORIES，新接口位于 browser 桶。

    # === 以下为零调用工具，从 MCP 网关排除（REST 仍可用） ===

    # A. 基础设施/系统（非 agent 使用：shutdown/health/config/llm_pool/mcp_stats/keep_awake/skip_cache）
    "shutdown_server",
    "health_health_get",
    "mcp_stats", "mcp_stats_reset",
    "get_config_config_get", "update_config_api_config_post",
    "llm_pool_call", "llm_pool_call_simple", "llm_pool_cleanup",
    "llm_pool_health_check",
    "llm_pool_init", "llm_pool_stats", "llm_pool_status",
    "llm_pool_chat_tools",  # v6-lite T05: 引擎 LLMGateway 专用，不进 agent tool catalog（防递归）
    "llm_pool_stream",      # v6-lite T08: SSE 伪流式端点，引擎专用（防递归）
    # v10：监控面板专用（per-model 详情 / 近期调用历史），agent 用 /health 获取聚合
    "llm_pool_models", "llm_pool_recent_calls",
    "set_keep_awake",
    "clear_skip_cache",

    # B. GUI/脚本专用（activity_daily 由日报系统管理，user_message 由 GUI 管理）
    "activity_daily_list", "activity_daily_read",
    "activity_daily_review", "activity_daily_update",
    "user_message_clear_all", "user_message_clear_one",
    "user_message_list", "user_message_send",
    # inbox 批量管理由监控面板调用，不进 MCP（防膨胀；agent 用 inbox_list/inbox_get 单条查询足够）
    "inbox_batch",

    # D. 零调用状态端点（可通过 /health 获取聚合状态）
    "system_status", "keep_awake_status",
    "docviewer_status", "memory_maintain_status",

    # E. v3 记忆系统观测性/审计端点（agent 偶尔查询，走 advanced_tool 网关；
    #    监控面板/调试主用，避免膨胀直连 MCP 列表）
    #    SearchTracer — 检索过程追踪
    "memory_search_traces_list", "memory_search_traces_detail",
    "memory_search_traces_cleanup",
    #    EvidenceLedger — 证据集审计
    "memory_evidence_recent", "memory_evidence_detail",
    "memory_evidence_cleanup",

    # === 入站网关（inbound-gateway SDD）：/v1/* 与 /inbound/* 不进 MCP 网关 ===
    # OpenAI 兼容入站面是给外部 harness 用的，管理面是给 client 面板用的；
    # MCP 为 fail-open（新端点默认进 advanced 网关），必须逐个 operation_id 显式排除。
    "inbound_v1_models", "inbound_v1_chat_completions",
    "inbound_keys_list", "inbound_keys_create",
    "inbound_keys_update", "inbound_keys_delete",
    "inbound_calls_list", "inbound_stats",
}


# ============================================================================
# Tool Annotations（MCP 协议规范的工具语义标记）
# ============================================================================
# 参考：https://modelcontextprotocol.io/specification#tool-annotations
# 4 个 hint 帮助 agent 理解工具的副作用语义，优化工具选择决策：
#   - readOnlyHint: true 表示工具无副作用（纯查询），可安全重复调用
#   - destructiveHint: true 表示工具会破坏数据/状态（删除/终止/覆盖）
#   - idempotentHint: true 表示相同参数重复调用效果相同
#   - openWorldHint: true 表示工具与外部世界交互（网络/文件系统/进程）
# 未列出的工具默认全为 false（保守策略，agent 需自行判断）。
# 与 route_tags.py 的 safety 分类（read_only/safe/approval_required）互补：
#   safety 是"是否需要审批"的硬约束，annotations 是"工具语义"的软提示。

TOOL_ANNOTATIONS: dict[str, dict[str, bool]] = {
    # === core ===
    "agent_guide":               {"readOnlyHint": True,  "idempotentHint": True},
    "localagent_list_tools":     {"readOnlyHint": True,  "idempotentHint": True},
    "localagent_advanced_tool":  {"openWorldHint": True},
    "localagent_tool_docs":      {"readOnlyHint": True,  "idempotentHint": True},
    "localagent_get_docs":       {"readOnlyHint": True,  "idempotentHint": True},
    "command_guard_request_approval": {"openWorldHint": True},

    # === state ===
    "memory_get":                {"readOnlyHint": True,  "idempotentHint": True},
    "memory_list":               {"readOnlyHint": True,  "idempotentHint": True},
    "memory_set":                {"idempotentHint": True, "openWorldHint": True},
    "memory_status":             {"readOnlyHint": True,  "idempotentHint": True},
    "todos_due":                 {"readOnlyHint": True,  "idempotentHint": True},
    "wip_list":                  {"readOnlyHint": True,  "idempotentHint": True},
    "wip_get":                   {"readOnlyHint": True,  "idempotentHint": True},
    "exec_status":               {"readOnlyHint": True,  "idempotentHint": True},

    # === exec ===
    "exec_python":               {"openWorldHint": True},
    "exec_apply_patch":          {"openWorldHint": True},
    "exec_cmd":                  {"openWorldHint": True},
    "exec_terminal_spawn":       {"openWorldHint": True},
    "exec_terminal_detail":      {"readOnlyHint": True,  "idempotentHint": True},
    "exec_terminal_input":       {"openWorldHint": True},
    "exec_terminal_kill":        {"destructiveHint": True, "openWorldHint": True},
    # T00 新增工具
    "exec_inspect":              {"readOnlyHint": True,  "idempotentHint": True},
    "exec_kill":                 {"destructiveHint": True, "openWorldHint": True},
    "exec_send_input":           {"openWorldHint": True},
    "wait":                      {"idempotentHint": True},

    # === perception ===
    "screen_request_control":   {"openWorldHint": True},
    "screen_release_control":   {"idempotentHint": True, "openWorldHint": True},
    "capture_screen":            {"readOnlyHint": True,  "openWorldHint": True},
    "list_windows":              {"readOnlyHint": True,  "idempotentHint": True},
    "focus_window":              {"openWorldHint": True},
    "execute_action":            {"openWorldHint": True},
    "batch_actions":             {"openWorldHint": True},
    "screen_snapshot":           {"readOnlyHint": True,  "openWorldHint": True},
    "ocr_file":                  {"readOnlyHint": True,  "openWorldHint": True},
    "ocr_status":                {"readOnlyHint": True,  "idempotentHint": True},
    "understand_image":          {"readOnlyHint": True,  "openWorldHint": True},
    "vision_locate":             {"readOnlyHint": True,  "openWorldHint": True},
    "vision_status":             {"readOnlyHint": True,  "idempotentHint": True},
    "screen_accessibility_snapshot": {"readOnlyHint": True,  "idempotentHint": True},
    "screen_semantic_action":    {"openWorldHint": True},
    # ZCode 对齐新增（网关层工具的注解；直连升级按调用频次另行决策）
    "screen_zoom":               {"readOnlyHint": True,  "openWorldHint": True},
    "read_clipboard":            {"readOnlyHint": True,  "openWorldHint": True},
    "write_clipboard":           {"openWorldHint": True},

    # === browser ===
    "browser_status":            {"readOnlyHint": True,  "idempotentHint": True},
    "browser_list_tabs":         {"readOnlyHint": True,  "idempotentHint": True},
    "browser_session_create":    {"openWorldHint": True},
    "browser_session_close":     {"destructiveHint": True},
    "browser_snapshot":          {"readOnlyHint": True,  "idempotentHint": True},
    "browser_action":            {"openWorldHint": True},
    "browser_wait_for":          {"readOnlyHint": True,  "idempotentHint": True},
    "browser_navigate":          {"openWorldHint": True},
    "browser_screenshot":        {"readOnlyHint": True,  "openWorldHint": True},
    "browser_match_site":        {"readOnlyHint": True,  "idempotentHint": True},
    "browser_write_lesson":      {"openWorldHint": True},  # 写文件，非破坏性（追加而非覆盖）
    # === screen ===（软件经验闭环）
    "screen_match_app":          {"readOnlyHint": True,  "idempotentHint": True},
    "screen_write_lesson":       {"openWorldHint": True},  # 写文件，非破坏性（追加而非覆盖）

    # === llm ===
    "agent_chat":                {"openWorldHint": True},
}


def get_tool_annotations(operation_id: str) -> dict[str, bool]:
    """查询工具的 annotations。未列出的工具返回空字典（agent 需自行判断）。

    可用于：
    - MCP 工具列表响应中附加 annotations 元数据
    - agent_guide 响应中提示工具语义
    - 文档生成
    """
    return TOOL_ANNOTATIONS.get(operation_id, {})


def get_tools_by_hint(hint: str, value: bool = True) -> list[str]:
    """查询具有特定 hint 的所有工具。

    示例：
        get_tools_by_hint("readOnlyHint")  # 所有只读工具
        get_tools_by_hint("destructiveHint")  # 所有破坏性工具
    """
    return [
        op for op, anns in TOOL_ANNOTATIONS.items()
        if anns.get(hint) == value
    ]


def get_tool_category(operation_id: str) -> str | None:
    """查询工具所属的桶名（如 "browser"）。未分类返回 None。

    agent_guide 用此函数把 MCP 工具列表按桶分组展示，避免 agent 面对扁平 38 个工具无所适从。
    """
    return TOOL_TO_CATEGORY.get(operation_id)


def get_category_tools(category: str) -> list[str]:
    """查询指定桶下的所有工具。桶不存在返回空列表。"""
    return list(TOOL_CATEGORIES.get(category, []))


def list_categories() -> list[tuple[str, int]]:
    """按 CATEGORY_ORDER 顺序列出所有桶，返回 [(category_name, tool_count), ...]。

    用于 agent_guide 分桶展示和文档生成。
    """
    return [(cat, len(TOOL_CATEGORIES[cat])) for cat in CATEGORY_ORDER]
