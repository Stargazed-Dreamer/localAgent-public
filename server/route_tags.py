"""路由自动打 tag：x-agent-callable + x-tool-safety

启动时遍历所有已注册路由，按 method + path 规则自动标注 openapi_extra。
避免手动修改 146 个 decorator，所有规则集中在此文件审计。

x-agent-callable（fail-open，T09 修正：与 _is_agent_callable 代码一致）：
- 默认 True（新端点自动暴露给 agent，符合"加 MCP 端点的目的就是给 agent 用"的设计意图）
- 排除清单 = operation_id IN mcp_whitelist.GATEWAY_EXCLUDE（显式排除基础设施/GUI 专用/零调用端点）
  （与 MCP 网关收编逻辑对齐：DIRECT_TOOLS 直连 + 其余非排除项经 advanced_tool 网关）
- DIRECT_TOOLS 中的 operation_id 优先判定为 True（直连暴露）
- 装饰器 openapi_extra={"x-agent-callable": bool} 显式声明优先（允许手动覆盖）
- 启动时打印 callable=False 的路由清单作为审计报告，不阻断启动
- 运行时消费方：W2 起的 ToolRegistry（从 /openapi.json 物化 catalog，只收 callable=true）
- MCP 网关工具收编仍由 mcp_whitelist.DIRECT_TOOLS + GATEWAY_EXCLUDE 独立控制，不受此影响
- 安全防护靠 classify_safety → approval_level → HTTP 中间件拦截，不靠 x-agent-callable 标志
- 详见 SECURITY-RISKS.md §14 "route_tags fail-open 默认暴露"

safety 四级：
- read_only: GET 查询，无副作用
- safe: POST 但不破坏数据（OCR、截图、LLM 对话等）
- approval_required: 写操作/破坏性操作，agent 调用时需 X-Approval-Token
- agent_blocked: agent 永不可调（当前无此级别端点）

审批严格性 approval_level（运行时生效，由 config.toml [command_guard].approval_level 控制）：
- strict   : 当前完整审批清单（系统/loop/activity/browser/exec/terminals/mindforge/user_message/模型卸载 + PUT/DELETE 默认审批）
- moderate : strict 减去 6 个低风险运维端点（loop 任务/活动日报/浏览器关闭/OCR 模型卸载与保持）
- loose    : 仅拦截代码执行类（exec_python / exec_apply_patch / exec_cmd），PUT/DELETE 全放行
- none     : 不拦截任何端点（仅 dcg 二进制预检查仍独立生效，受 enabled 控制）

注：auto_tag_routes() 仍按 strict 完整清单写入 OpenAPI 元数据（展示理论最大审批面）；
    build_safety_lookup() / build_operation_safety_map() 才按当前 approval_level 过滤，
    供 HTTP 中间件和 MCP 网关实际生效。
"""

from __future__ import annotations

import logging
import re

from starlette.routing import compile_path

logger = logging.getLogger("localagent.route_tags")

# PUT/DELETE/PATCH 中标记为 safe 的 path 子串（数据增删改但不破坏系统）
_SAFE_WRITE_PATTERNS: list[str] = [
    # todos / wip / inbox 写操作（任务管理，无系统风险）
    "/todos", "/wip", "/inbox",
    # memory 写操作（记忆增删，无系统风险）
    "/memory",
    # 终端 DELETE（kill/delete/input 由端点内部 owner_token 验证，spawn 仍 approval_required）
    "/terminals",
]

# POST 端点中需要 approval_required 的 path 子串（strict 完整清单）
_APPROVAL_POST_PATTERNS: list[str] = [
    # config / system
    "/shutdown", "/config", "/system/keep-awake",
    "/mcp/stats/reset",
    # apikey 写操作
    "/apikey/keys",  # POST (create), PUT/DELETE 由 method 判断
    # loop 控制
    "/loop/tasks",
    # activity 写操作
    "/activity/daily",
    # browser 关闭
    "/browser/close",
    # exec / terminals（已过 dcg，HTTP 审批是第二道防线）
    "/exec/python", "/exec/apply-patch", "/exec/cmd",
    # 仅 spawn 需审批；kill/delete/input 由端点内部 owner_token 验证（agent 自建自删免审）
    "/terminals/spawn",
    # advanced / templates 运行
    "/advanced/run", "/templates/run",
    # mindforge 运维
    "/mindforge/build-index", "/mindforge/convert", "/mindforge/pipeline",
    "/mindforge/unload", "/mindforge/daemon/stop",
    # user_message（用户指令不应被 agent 改）
    "/user/message",
    # model unload（OCR）
    "/ocr/models/unload", "/ocr/models/keep",
]

# 适中级从 strict 移除的端点（用户决策：这些操作风险低，放行）
_MODERATE_EXCLUDE_PATTERNS: list[str] = [
    "/loop/tasks",
    "/activity/daily",
    "/browser/close",
    "/ocr/models/unload",
    "/ocr/models/keep",
]

# 宽松级仅保留代码执行类（用户决策：只拦 python/apply/cmd）
_LOOSE_APPROVAL_PATTERNS: list[str] = [
    "/exec/python",
    "/exec/apply-patch",
    "/exec/cmd",
]

VALID_LEVELS = ("strict", "moderate", "loose", "none")
_DEFAULT_LEVEL = "strict"


def get_approval_level() -> str:
    """读取当前审批严格性级别（从 config.toml）。

    在 server/config.py 的 get_command_guard_config() 中已校验并返回，
    这里二次校验防止其他调用路径绕过。无效值回退到 strict（安全优先）。
    """
    try:
        from server.config import get_command_guard_config
        level = get_command_guard_config().get("approval_level", _DEFAULT_LEVEL)
    except Exception:
        return _DEFAULT_LEVEL
    return level if level in VALID_LEVELS else _DEFAULT_LEVEL


def get_active_post_patterns() -> list[str]:
    """根据当前 approval_level 返回生效的 POST 审批 patterns。"""
    level = get_approval_level()
    if level == "strict":
        return list(_APPROVAL_POST_PATTERNS)
    if level == "moderate":
        return [p for p in _APPROVAL_POST_PATTERNS if p not in _MODERATE_EXCLUDE_PATTERNS]
    if level == "loose":
        return list(_LOOSE_APPROVAL_PATTERNS)
    return []  # none


def _path_matches_prefix(path: str, pattern: str) -> bool:
    """精确前缀匹配：path 等于 pattern，或 path 以 pattern（去尾斜杠）+ '/' 开头。

    避免 '/memory_meta' 误匹配 '/memory' 这类字符串包含问题。
    """
    if path == pattern:
        return True
    return path.startswith(pattern.rstrip("/") + "/")


def classify_safety(method: str, path: str) -> str:
    """按 method + path 分类端点安全级别（理论最大值，用于 OpenAPI 元数据）。

    不考虑 approval_level——所有路由的 OpenAPI tag 都按 strict 完整清单标注，
    便于审计和 GUI 展示"该端点可能需要审批"。运行时实际生效的过滤在
    build_safety_lookup() / build_operation_safety_map() 中完成。
    """
    if method == "GET":
        return "read_only"
    path_lower = path.lower()
    if method in ("DELETE", "PUT", "PATCH"):
        for pattern in _SAFE_WRITE_PATTERNS:
            if _path_matches_prefix(path_lower, pattern):
                return "safe"
        return "approval_required"
    if method == "POST":
        for pattern in _APPROVAL_POST_PATTERNS:
            if _path_matches_prefix(path_lower, pattern):
                return "approval_required"
        return "safe"
    return "read_only"  # HEAD/OPTIONS 等


def classify_safety_runtime(method: str, path: str) -> str:
    """按 method + path + 当前 approval_level 分类（运行时实际生效）。

    供 build_safety_lookup() / build_operation_safety_map() 使用。
    与 classify_safety() 的差异：根据 approval_level 把当前级别下不拦的端点
    从 approval_required 降级为 safe。
    """
    if method == "GET":
        return "read_only"
    level = get_approval_level()
    if level == "none":
        return "safe"  # 全部放行
    path_lower = path.lower()
    if method in ("DELETE", "PUT", "PATCH"):
        # _SAFE_WRITE_PATTERNS 永远 safe（todos/wip/inbox/memory/terminals）
        for pattern in _SAFE_WRITE_PATTERNS:
            if _path_matches_prefix(path_lower, pattern):
                return "safe"
        # 宽松级：PUT/DELETE/PATCH 全放行（只拦代码执行类 POST）
        if level == "loose":
            return "safe"
        return "approval_required"
    if method == "POST":
        active_patterns = get_active_post_patterns()
        for pattern in active_patterns:
            if _path_matches_prefix(path_lower, pattern):
                return "approval_required"
        return "safe"
    return "read_only"


# operation_id → (method, path) 反查表（启动时由 init_op_path_map 构建）
_op_to_path: dict[str, tuple[str, str]] = {}


def init_op_path_map(app) -> None:
    """从 app.routes 构建 operation_id → (method, path) 映射。

    在 main.py 启动时调用一次，供 classify_safety_runtime_by_op() 反查。
    """
    _op_to_path.clear()
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        operation_id = getattr(route, "operation_id", None) or getattr(route, "name", None)
        if not operation_id:
            continue
        for method in route.methods:
            if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                _op_to_path[operation_id] = (method, route.path)
                break


def classify_safety_runtime_by_op(operation_id: str) -> str:
    """按 operation_id + 当前 approval_level 分类（运行时实际生效）。

    供 MCP 网关使用（它只有 operation_id，没有 method+path）。
    内部通过 _op_to_path 反查 method+path 后调 classify_safety_runtime()。
    未找到映射时返回 "approval_required"（安全优先）。
    """
    method_path = _op_to_path.get(operation_id)
    if not method_path:
        return "approval_required"  # 未知 operation，安全优先
    method, path = method_path
    return classify_safety_runtime(method, path)


def _is_agent_callable(operation_id: str | None) -> bool:
    """判断 operation_id 是否应被标记为 agent 可调（fail-open 默认暴露）。

    真源：server/mcp_whitelist.py 的 DIRECT_TOOLS + GATEWAY_EXCLUDE。
    规则：
    - operation_id 为 None（路由无 operationId）→ False
    - operation_id 在 DIRECT_TOOLS 中 → True（直连暴露，优先判定）
    - operation_id 在 GATEWAY_EXCLUDE 中 → False（基础设施/GUI 专用/零调用/REST-only form）
    - 其余 → True（经 localagent_advanced_tool 网关间接访问）

    T09 说明：此函数采用 fail-open 策略（新端点默认暴露给 agent）。
    设计意图：加 MCP 端点的目的就是给 agent 用，新端点自动可调是 feature 不是 bug。
    安全防护靠 classify_safety → approval_level → HTTP 中间件拦截，不靠此标志。
    详见 SECURITY-RISKS.md "route_tags fail-open" 条目。

    注：localagent_list_tools / localagent_advanced_tool 同时出现在 DIRECT_TOOLS 和
    GATEWAY_EXCLUDE 中——GATEWAY_EXCLUDE 在此仅表示"不重复进 advanced 网关"（去重），
    它们仍是 DIRECT_TOOLS 直连工具。因此 DIRECT_TOOLS 判定必须优先。
    """
    if not operation_id:
        return False
    try:
        from server.mcp_whitelist import DIRECT_TOOLS, GATEWAY_EXCLUDE
    except Exception:
        # mcp_whitelist 不可用时 fail-closed：全部拒绝
        return False
    if operation_id in DIRECT_TOOLS:
        return True
    return operation_id not in GATEWAY_EXCLUDE


def auto_tag_routes(app) -> int:
    """遍历 app.routes，自动打 x-agent-callable + x-tool-safety tag（fail-open）。

    在所有路由注册完成后调用（main.py 中 include_router 之后）。
    返回打 tag 的路由数。

    fail-open 策略（T09 修正：与 _is_agent_callable 代码一致）：
    - 默认 x-agent-callable = True（新端点自动暴露给 agent）
    - GATEWAY_EXCLUDE 中的端点 → False（显式排除）
    - DIRECT_TOOLS → True（直连暴露）
    - 装饰器级 openapi_extra={"x-agent-callable": bool} 显式声明优先（允许手动覆盖）
    - 安全防护靠 classify_safety → approval_level，不靠 x-agent-callable 标志

    注：x-agent-callable 当前仅写入 OpenAPI 元数据，运行时消费方为 W2 起的 ToolRegistry。
    MCP 网关的工具收编仍由 mcp_whitelist.DIRECT_TOOLS + GATEWAY_EXCLUDE 独立控制，不受此影响。
    """
    tagged = 0
    callable_count = 0
    blocked_records: list[tuple[str, str, str]] = []  # (method, path, operation_id)
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        if not route.methods:
            continue
        # 取第一个 HTTP method（通常每路由只有一个）
        method = next((m for m in route.methods if m in ("GET", "POST", "PUT", "DELETE", "PATCH")), None)
        if method is None:
            continue
        safety = classify_safety(method, route.path)
        existing = getattr(route, "openapi_extra", None) or {}
        # 用 unique_id（与 /openapi.json 的 operationId 一致）：
        # - 显式 operation_id 时 unique_id = operation_id（如 "shutdown_server"）
        # - 未显式时 FastAPI 生成 "{name}_{path_underscores}_{method}"（如 "health_health_get"）
        # GATEWAY_EXCLUDE / DIRECT_TOOLS 用 schema operationId 格式，必须用 unique_id 才能匹配。
        operation_id = getattr(route, "unique_id", None) or getattr(route, "operation_id", None) or getattr(route, "name", None)
        # x-tool-safety：不覆盖已有声明（允许手动覆盖）
        if "x-tool-safety" not in existing:
            existing["x-tool-safety"] = safety
        # x-agent-callable：fail-open（默认 True），允许装饰器显式覆盖
        if "x-agent-callable" in existing:
            # 装饰器显式声明优先
            is_callable = bool(existing["x-agent-callable"])
        else:
            # 未显式声明 → 查 allowlist（NOT in GATEWAY_EXCLUDE）
            is_callable = _is_agent_callable(operation_id)
            existing["x-agent-callable"] = is_callable
        route.openapi_extra = existing
        tagged += 1
        if is_callable:
            callable_count += 1
        else:
            blocked_records.append((method, route.path, operation_id or "<no-op-id>"))

    blocked_count = tagged - callable_count
    logger.info(
        "route_tags: tagged %d routes (callable=%d, blocked=%d) with x-agent-callable + x-tool-safety",
        tagged, callable_count, blocked_count,
    )
    # 审计报告：打印未标注（callable=False）路由清单，不阻断启动
    if blocked_records:
        logger.info("route_tags: fail-open audit — %d routes NOT agent-callable:", len(blocked_records))
        for method, path, op_id in blocked_records:
            logger.info("  [blocked] %-6s %-40s %s", method, path, op_id)
    return tagged


def build_safety_lookup(app) -> list[tuple[re.Pattern, str, str]]:
    """构建 (path_regex, method, safety) 查找表，供 HTTP 审批中间件使用。

    中间件在路由匹配之前执行，无法直接访问 route.openapi_extra。
    此函数将所有路由的 safety 级别预编译为 regex 查找表。

    使用 classify_safety() 按 strict 完整清单构建（不按 approval_level 过滤），
    因为 lookup 在启动时构建，不会随 level 变化重建。运行时由中间件调
    classify_safety_runtime() 二次确认当前 level 是否真的需要审批。
    """
    lookup: list[tuple[re.Pattern, str, str]] = []
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        path_regex, _path_format, _convertors = compile_path(route.path)
        for method in route.methods:
            if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                safety = classify_safety(method, route.path)
                lookup.append((path_regex, method, safety))
    return lookup


def build_operation_safety_map(app) -> dict[str, str]:
    """构建 {operation_id: safety_level} 映射，供 MCP gateway 审批检查使用。

    MCP 工具调用是内部函数调用（不经过 HTTP 中间件），需在 _execute_api_tool
    中检查 operation_id 对应的 safety 级别。

    使用 classify_safety() 按 strict 完整清单构建。运行时由 MCP 网关调
    classify_safety_runtime() 二次确认当前 level 是否真的需要审批。
    """
    mapping: dict[str, str] = {}
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        operation_id = getattr(route, "operation_id", None) or getattr(route, "name", None)
        if not operation_id:
            continue
        for method in route.methods:
            if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                mapping[operation_id] = classify_safety(method, route.path)
                break
    return mapping
