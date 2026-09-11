"""高级工具网关 - 双层 MCP 架构的第二层

将低频/专业工具收拢到单个网关工具 localagent_advanced_tool，
通过 tool 参数路由到具体端点。配合 localagent_list_tools 发现可用工具。

设计参考 Unity MCP 的 advanced_tool 网关模式：
  直接暴露 ~55 个高频工具（第一层）
      ↓
  localagent_advanced_tool（1个网关）  ← 通过 tool 参数路由到 30+ 子工具
      ↓
  localagent_list_tools  ← 运行时发现可用子工具及参数

这样既突破 MCP 工具数量上限，又让所有 REST 端点都能通过 MCP 访问。

ADR-0023 DANGEROUS_TOOLS 权限检查：
网关是内部函数调用（不经过 HTTP 中间件），对 DANGEROUS_TOOLS 集合中的
敏感端点（apikey/memory_delete/shutdown 等破坏性操作）强制走 command_guard 审批。
其他端点默认放行（HTTP 中间件已对 approval_required 端点做拦截）。
"""

import logging
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from lib.schema import BaseSchema
from server.config import get_server_config

logger = logging.getLogger("localagent.advanced")


# ========== ADR-0023 DANGEROUS_TOOLS 权限检查 ==========

# 危险工具集合：需二级审批（operation_id 来自各路由 operation_id 声明）
# 收录标准：
# - 破坏性写操作（删除敏感数据）
# - 系统级操作（关机/重启/服务停止）
# - 凭证管理（API key 增删改）
# 注：memory_delete 虽在 _SAFE_WRITE_PATTERNS（route_tags 标 safe），
# 但语义上是数据破坏，spec 明确要求纳入 DANGEROUS_TOOLS。
# 注：memory_set（POST /memory/{key}）不纳入：写记忆是 agent 日常操作，
# 每次写都弹窗审批太烦；覆盖敏感数据风险靠 key 命名空间 + 审计日志防。
DANGEROUS_TOOLS: set[str] = {
    # 系统级
    "auto_shutdown_trigger",  # POST /auto-shutdown/trigger
    "shutdown_server",        # POST /shutdown
    # 凭证管理（apikey 增删改）
    "apikey_create_key",      # POST /apikey/keys
    "apikey_update_key",      # PUT /apikey/keys/{key_id}
    "apikey_delete_key",      # DELETE /apikey/keys/{key_id}
    # 记忆破坏性操作（仅删除；写记忆不纳入，见上方说明）
    "memory_delete",          # DELETE /memory/{key}
}


def _is_dangerous_tool(operation_id: str) -> bool:
    """判断 operation_id 是否属于危险工具（需二级审批）。

    参数:
        operation_id: 工具的 operation_id（即 AdvancedToolRequest.tool）

    返回:
        bool: True 表示需要审批，False 表示默认放行
    """
    return operation_id in DANGEROUS_TOOLS


async def _check_dangerous_tool_approval(operation_id: str, params: dict) -> tuple[bool, str]:
    """对 DANGEROUS_TOOLS 中的工具调用走 GUI 审批弹窗。

    复用 command_guard.run_gui_dialog 共享函数（与 shell/http 审批同弹窗）。
    审批结果决定是否放行网关调用。

    参数:
        operation_id: 危险工具的 operation_id
        params: 工具调用参数（用于审批弹窗展示）

    返回:
        (approved, reason)：approved=True 时放行；approved=False 时 reason 含拒绝原因
    """
    from server.command_guard import run_gui_dialog

    approval_id = f"advanced_approval_{uuid.uuid4().hex[:16]}"
    # 截断 params 预览，避免大 payload 撑爆弹窗
    params_preview = repr(params)
    if len(params_preview) > 500:
        params_preview = params_preview[:500] + "...(truncated)"
    payload = {
        "type": "advanced_tool",
        "operation_id": operation_id,
        "params_preview": params_preview,
        "guard_reason": f"危险工具 {operation_id} 需用户审批（ADR-0023 DANGEROUS_TOOLS）",
        "agent_reason": f"调用危险工具 {operation_id}",
    }
    try:
        result = await run_gui_dialog(payload, approval_id=approval_id)
    except (TimeoutError, RuntimeError) as exc:
        logger.warning("危险工具 %s 审批失败: %s", operation_id, exc)
        return False, f"审批流程失败: {exc}"

    decision = result.get("decision", "deny")
    if decision == "approve":
        return True, ""
    return False, f"用户拒绝审批 (decision={decision})"


def _backend_url() -> str:
    """后端根 URL（advanced 网关自身就在后端进程内，直接读 config）"""
    cfg = get_server_config()
    return f"http://{cfg['host']}:{cfg['port']}"


router = APIRouter(prefix="/advanced", tags=["高级工具网关"])


# ========== 分类映射（按 operation_id 前缀） ==========

_CATEGORY_PREFIXES = [
    ("ocr_", "ocr"), ("vl_", "ocr"),
    ("vision_", "vision"),
    ("mindforge_", "mindforge"),
    ("exec_", "exec"),
    ("apikey_", "apikey"),
    ("memory_", "memory"),
    ("browser_", "browser"),
    ("screen_", "screen"),
    ("agent_", "agent"),
    ("docviewer_", "docviewer"),
    ("system_", "system"),
    ("keep_awake_", "system"),
    ("set_keep_awake", "system"),
    ("shutdown_", "system"),
    ("mcp_stats", "system"),
    ("todos_", "todos"),
    ("wip_", "todos"),
]


def _categorize(operation_id: str) -> str:
    """
    根据操作ID的前缀进行分类。

    参数:
    operation_id (str): 需要分类的操作ID。

    返回:
    str: 对应的类别字符串，如果操作ID不匹配任何前缀，则返回"other"。
    """
    # 遍历前缀和类别映射
    for prefix, cat in _CATEGORY_PREFIXES:
        # 检查操作ID是否以当前前缀开头
        if operation_id.startswith(prefix):
            # 如果是，返回对应的类别
            return cat
    # 如果没有匹配的前缀，返回默认类别"other"
    return "other"


# ========== 注册表：在启动时从 OpenAPI schema 构建 ==========

# operation_id -> {path, method, summary, category, parameters}
_advanced_registry: dict[str, dict] = {}


def get_status() -> dict:
    """Advanced 网关注册表状态概览（供 /health 调用，不暴露 raw 私有 dict）"""
    return {
        "gateway_tools_count": len(_advanced_registry),
        "gateway_categories": sorted({info["category"] for info in _advanced_registry.values()}),
    }


def build_registry_from_app(app, excluded_ops: list[str]) -> int:
    """从 FastAPI 应用的 OpenAPI schema 构建高级工具注册表。

    只收录 excluded_ops 中列出的操作（即未直接暴露为 MCP 的工具）。
    返回收录的工具数量。
    """
    global _advanced_registry
    _advanced_registry = {}
    try:
        schema = app.openapi()
        paths = schema.get("paths", {})
        excluded_set = set(excluded_ops)
        for path, methods in paths.items():
            for method, info in methods.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                op_id = info.get("operationId")
                if not op_id or op_id not in excluded_set:
                    continue
                # 跳过 MCP 内部端点和静态资源
                if op_id in ("mcp_connection", "mcp_messages"):
                    continue
                params = []
                for p in info.get("parameters", []):
                    params.append({
                        "name": p.get("name"),
                        "in": p.get("in"),  # path | query | header
                        "required": p.get("required", False),
                        "description": p.get("description", ""),
                    })
                # 请求体参数（JSON body）
                has_body = "requestBody" in info
                _advanced_registry[op_id] = {
                    "path": path,
                    "method": method.upper(),
                    "summary": info.get("summary", "") or info.get("description", "").split("\n")[0],
                    "description": info.get("description", ""),
                    "category": _categorize(op_id),
                    "parameters": params,
                    "has_body": has_body,
                }
    except Exception as e:
        logger.warning(f"构建高级工具注册表失败: {e}")
    logger.info(f"高级工具注册表已构建: {len(_advanced_registry)} 个工具")
    return len(_advanced_registry)


# ========== Pydantic 模型 ==========

class AdvancedToolParam(BaseSchema):
    """这是一个高级工具参数的数据模型类，用于定义工具的参数信息。

    参数：
        name (str): 参数的名称。
        location (str): 参数的位置，可以是路径（path）、查询（query）或请求体（body）。
        required (bool): 该参数是否必需。
        description (str): 参数的描述信息。

    返回值：
        无返回值，这是一个数据模型类。
    """
    name: str  # 工具参数的名称
    location: str  # 参数位置，可选值为路径（path）、查询参数（query）或请求体（body）
    required: bool  # 标记该参数是否为必需项
    description: str  # 参数的详细描述信息

class AdvancedToolInfo(BaseSchema):
    """
    高级工具信息类，用于存储和管理工具的详细信息。

    参数：
    - name: str，工具名称，常作为tool参数传入。
    - category: str，工具类别。
    - summary: str，工具摘要。
    - path: str，工具路径。
    - method: str，工具方法。
    - parameters: list[AdvancedToolParam]，工具参数列表。
    - has_body: bool，是否包含请求体。

    返回值：
    该类的实例，包含所有指定的属性。
    """
    name: str  # operation_id，作为 tool 参数传入
    category: str
    summary: str
    path: str
    method: str
    parameters: list[AdvancedToolParam]
    has_body: bool

class ListAdvancedToolsResponse(BaseSchema):
    """返回高级工具列表的响应数据模型。

    该类用于封装工具列表查询的结果，包含工具总数和按分类组织的工具信息。

    参数:
        total (int): 查询结果中的工具总数。
        categories (dict[str, list[AdvancedToolInfo]]):
            按分类组织的工具信息字典。
            - 键 (str): 工具分类名称。
            - 值 (list[AdvancedToolInfo]): 该分类下的高级工具信息对象列表。

    返回值:
        无（这是一个数据模型类，用于构建响应对象）。
    """
    total: int  # 工具总数
    categories: dict[str, list[AdvancedToolInfo]]  # category -> tools  # 按分类组织的工具字典，键为分类名，值为该分类的工具信息列表

class AdvancedToolRequest(BaseSchema):
    """
    高级工具请求的数据模型。

    用于封装和管理工具请求的参数和工具标识。

    参数:
        tool (str): 目标工具的 operation_id。
        params (dict): 参数字典，合并了路径、查询和正文参数。默认为空字典。

    返回值:
        AdvancedToolRequest 实例。
    """
    tool: str  # 目标工具的 operation_id
    params: dict = Field(default_factory=dict)  # 参数（path/query/body 合并传入）

class AdvancedToolResponse(BaseSchema):
    """
    用于封装高级工具调用响应结果的数据模型。
    功能：整合工具调用的成功状态、来源工具、HTTP状态码、返回数据及错误信息。
    参数：
        success (bool): 工具调用是否成功。
        tool (str): 产生响应的工具名称。
        status_code (int): 对应的HTTP状态码。
        result (Optional[dict]): 目标端点返回的JSON响应数据，可能为None。
        error (Optional[str]): 工具调用过程中发生的错误描述，成功时为None。
    返回值：
        本类实例，包含上述所有字段的结构化响应对象。
    """
    success: bool  # 工具调用是否成功
    tool: str  # 响应来源的工具名称
    status_code: int  # HTTP状态码
    result: dict | None = None  # 目标端点返回的 JSON 响应数据
    error: str | None = None  # 错误信息，成功时应为 None


# ========== 路由 ==========

@router.get("/tools", operation_id="localagent_list_tools")
async def list_advanced_tools(
    category: str | None = Query(None, description="按分类过滤: ocr/vision/mindforge/exec/apikey/memory/browser/screen/agent/docviewer/system/other"),
    search: str | None = Query(None, description="关键词搜索: 在 name 和 summary 中模糊匹配（不区分大小写）。如 search=screen 返回所有名称或摘要含 screen 的工具"),
    verbose: bool = Query(False, description="true=返回完整参数 schema；默认只返回 name+category+summary（推荐 agent 默认用精简模式）"),
    names_only: bool = Query(False, description="true=极简模式，只返回 {total, names:[...]} 工具名列表。适合 agent 快速扫描可用工具名，再按需查 verbose=true。与 verbose 互斥"),
):
    """List all advanced tools accessible via localagent_advanced_tool.

    运行时发现可用子工具及参数。支持三种粒度：

    1. **names_only=true**（极简）：只返回工具名列表，~1KB。agent 快速扫描可用工具名
       → `localagent_list_tools(params={"names_only": true})`
    2. **默认精简**（推荐）：name+category+summary，按分类分组。~5-8KB
       → `localagent_list_tools(params={})`
    3. **search="关键词"**：按 name/summary 模糊匹配过滤，避免返回全量 120 个工具
       → `localagent_list_tools(params={"search": "screen"})`
    4. **verbose=true**：含完整参数 schema。需查参数时用

    category 与 search 可组合（先按分类过滤再搜关键词）。
    这些是低频/专业工具，通过 localagent_advanced_tool(tool=<name>, params={...}) 调用。
    """
    # names_only 极简模式：只返回工具名列表
    if names_only:
        names = []
        for name, info in _advanced_registry.items():
            if category and info["category"] != category:
                continue
            if search:
                needle = search.lower()
                hay = (name + " " + info.get("summary", "")).lower()
                if needle not in hay:
                    continue
            names.append(name)
        return {"total": len(names), "names": names}

    # 精简/完整模式：按分类分组返回
    cats: dict[str, list[AdvancedToolInfo]] = {}
    for name, info in _advanced_registry.items():
        if category and info["category"] != category:
            continue
        if search:
            needle = search.lower()
            hay = (name + " " + info.get("summary", "")).lower()
            if needle not in hay:
                continue
        params = [
            AdvancedToolParam(
                name=p["name"], location=p["in"],
                required=p["required"], description=p["description"],
            ) for p in info["parameters"]
        ] if verbose else []
        entry = AdvancedToolInfo(
            name=name, category=info["category"], summary=info["summary"],
            path=info["path"], method=info["method"],
            parameters=params, has_body=info["has_body"],
        )
        cats.setdefault(info["category"], []).append(entry)
    return ListAdvancedToolsResponse(
        total=sum(len(v) for v in cats.values()), categories=cats,
    )


# ========== tool_specs 语义文档暴露（MCP-only agent 补全）==========

_tool_specs_cache: dict | None = None


def _load_tool_specs() -> dict:
    """加载 data/client/tool_specs.yaml，缓存结果。失败时返回空 dict（fail-open）。"""
    global _tool_specs_cache
    if _tool_specs_cache is not None:
        return _tool_specs_cache
    try:
        from pathlib import Path

        import yaml
        specs_path = Path(__file__).resolve().parents[1] / "data" / "client" / "tool_specs.yaml"
        with open(specs_path, encoding="utf-8") as f:
            _specs_loaded: dict = yaml.safe_load(f) or {}
        _tool_specs_cache = _specs_loaded
        logger.info(
            "tool_specs.yaml loaded: builtin=%d, core_mcp=%d, rest=%d",
            len(_specs_loaded.get("builtin", {})),
            len(_specs_loaded.get("core_mcp", {})),
            len(_specs_loaded.get("rest", {})),
        )
    except ImportError:
        logger.warning("yaml not available, tool_specs.yaml not loaded")
        _tool_specs_cache = {}
    except Exception as e:
        logger.warning("tool_specs.yaml load failed (fail-open): %s", e)
        _tool_specs_cache = {}
    return _tool_specs_cache


@router.get("/docs/tool", operation_id="localagent_tool_docs")
async def get_tool_docs(
    tool_name: str | None = Query(None, description="工具名（operation_id）。传入则返回该工具的完整 7 段语义说明；省略则返回所有有语义说明的工具清单"),
    source: str | None = Query(None, description="按来源过滤: builtin(客户端内置)/core_mcp(MCP直连工具)/rest(REST端点)"),
):
    """Get semantic documentation for a tool (purpose/when_to_use/when_not_to_use/inputs/outputs/errors/examples).

    返回 tool_specs.yaml 中手工编写的 7 段语义说明，弥补 MCP tools/list 只有裸 docstring 的不足。
    MCP-only agent（不直接访问项目代码的 agent）可通过本工具理解每个工具的：
    - purpose: 用途
    - when_to_use: 何时用
    - when_not_to_use: 何时不用
    - inputs_desc: 输入说明
    - outputs_desc: 输出说明
    - error_cases: 错误情形
    - examples: 调用示例

    用法：
    1. 不传参数 → 返回所有有语义说明的工具清单（name + source + purpose）
    2. tool_name="exec_python" → 返回 exec_python 的完整 7 段说明
    3. source="core_mcp" → 只返回 MCP 直连工具的说明清单
    """
    specs = _load_tool_specs()
    if not specs:
        return {"error": "tool_specs.yaml not available", "tools": []}

    # 收集所有来源的工具说明
    sources = {"builtin", "core_mcp", "rest"}
    if source and source in sources:
        sources = {source}

    # 模式 1：查单个工具的完整说明
    if tool_name:
        for src in sources:
            section = specs.get(src, {})
            if tool_name in section:
                doc = section[tool_name]
                return {
                    "tool_name": tool_name,
                    "source": src,
                    "purpose": doc.get("purpose", ""),
                    "when_to_use": doc.get("when_to_use", ""),
                    "when_not_to_use": doc.get("when_not_to_use", ""),
                    "inputs_desc": doc.get("inputs_desc", ""),
                    "outputs_desc": doc.get("outputs_desc", ""),
                    "error_cases": doc.get("error_cases", ""),
                    "examples": doc.get("examples", ""),
                }
        return {"error": f"tool '{tool_name}' not found in tool_specs.yaml", "tool_name": tool_name}

    # 模式 2：返回工具清单
    tools = []
    for src in sorted(sources):
        section = specs.get(src, {})
        for name, doc in section.items():
            tools.append({
                "name": name,
                "source": src,
                "purpose": doc.get("purpose", ""),
            })
    return {"total": len(tools), "tools": tools}


# ========== 项目文档暴露（MCP-only agent 补全）==========

# 文档注册表：doc_name → {path, description, category, max_chars}
# 仅暴露预注册文档，不支持任意文件访问（安全）
#
# 设计原则：从"外部项目仅通过 MCP 使用本系统"的角度筛选文档。
# 只暴露帮助 agent 正确使用 MCP 工具的文档，不暴露项目内部开发/维护文档
# （dev-workflow/project-rules/changelog/adr-index/config-example/architecture
#  这些是给本项目维护者看的，对外部 agent 无用）。
_PROJECT_DOCS: list[dict] = [
    # ── 核心指南：如何使用工具 ──
    {"name": "agents", "path": "AGENTS.md",
     "description": "工具选择决策树、MCP 优先原则、API 常见陷阱（最关键的使用指南）",
     "category": "core", "max_chars": 30000},
    {"name": "tools-guide", "path": "docs/tools-guide.md",
     "description": "工具详细用法：exec_python/exec_cmd 机制、exec_* 工具表、参数说明",
     "category": "core", "max_chars": 20000},
    {"name": "skills-index", "path": ".agents/skills/_index.md",
     "description": "系统内置 60+ skill 清单（日报/股票/抽卡/记账/浏览器/屏幕操作等），agent_guide 可路由到这些 skill",
     "category": "core", "max_chars": 40000},
    {"name": "computer-use-skill", "path": ".agents/skills/computer_use/SKILL.md",
     "description": "Computer Use 核心流程：安全铁律、窗口操作铁律、标准操作流程、软件经验闭环（用 screen_* 工具操作桌面前必读）",
     "category": "core", "max_chars": 15000},
    {"name": "browser-lessons-skill", "path": ".agents/skills/browser_lessons/SKILL.md",
     "description": "浏览器方法论完整文档：核心理念、网站识别规则、sites 文件结构、跨网站通用踩坑表、非显然行为判定标准（用浏览器工具前必读）",
     "category": "core", "max_chars": 20000},
    # ── 系统参考：理解系统机制 ──
    {"name": "mcp-reference", "path": "docs/mcp-reference.md",
     "description": "MCP 3 层架构、直连工具白名单、网关路由、annotations 语义标记",
     "category": "system", "max_chars": 20000},
    {"name": "memory-system", "path": "docs/memory-system.md",
     "description": "记忆系统：fact_type 分类、staleness 检测、语义搜索（使用 memory_get/set/search 前必读）",
     "category": "system", "max_chars": 15000},
    {"name": "computer-use-reference", "path": "docs/computer-use-reference.md",
     "description": "Computer Use 详细参考：UIA 语义层、桌面事务、窗口生命周期、截图方案决策树、DPI 缩放、API 速查（screen_accessibility_snapshot/screen_semantic_action 用法深入）",
     "category": "system", "max_chars": 30000},
    {"name": "operations-manual", "path": "docs/operations-manual.md",
     "description": "运维手册：含浏览器操作经验记录强制章节（任务前/收尾时流程、非显然行为判定标准、7 类踩坑）",
     "category": "system", "max_chars": 25000},
]


@router.get("/docs/project", operation_id="localagent_get_docs")
async def get_project_docs(
    doc_name: str | None = Query(None, description="文档名（见 localagent_get_docs 列表）。传入则返回该文档内容；省略则返回可用文档清单"),
    category: str | None = Query(None, description="按分类过滤: core(如何使用工具)/system(系统机制参考)"),
):
    """Get project documentation for MCP-only agents.

    返回项目使用文档，让仅通过 MCP 连接的 agent（无法直接读项目文件的 agent）
    也能获取正确使用本系统所需的文档。

    设计原则：只暴露帮助 agent 使用 MCP 工具的文档，不暴露项目内部开发/维护文档。
    所有文档均为预注册安全清单，不支持任意文件访问。

    可用文档（9 份，2 类）：
    - core(5 份，如何使用工具): agents(决策树), tools-guide(工具用法), skills-index(60+ skill 清单),
      computer-use-skill(桌面操作铁律), browser-lessons-skill(浏览器方法论)
    - system(4 份，机制参考): mcp-reference(MCP 架构), memory-system(记忆系统),
      computer-use-reference(UIA/桌面事务/DPI 详解), operations-manual(浏览器经验记录强制章节)

    用法：
    1. 不传参数 → 返回可用文档清单（name + description + category）
    2. doc_name="agents" → 返回 AGENTS.md 内容
    3. category="core" → 只返回核心指南类文档清单

    超长文档自动截断（保留头部 + 尾部，中间标注截断位置）。
    """
    # 模式 1：查单个文档内容
    if doc_name:
        from pathlib import Path
        project_root = Path(__file__).resolve().parents[1]
        for doc in _PROJECT_DOCS:
            if doc["name"] == doc_name:
                file_path = project_root / doc["path"]
                try:
                    content = file_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    return {"error": f"file not found: {doc['path']}", "doc_name": doc_name}
                except Exception as e:
                    return {"error": f"read failed: {e}", "doc_name": doc_name}
                # 截断超长文档
                max_chars = doc.get("max_chars", 30000)
                truncated = False
                if len(content) > max_chars:
                    head = content[: max_chars - 500]
                    tail = content[-500:]
                    content = head + f"\n\n... [截断：原文 {len(content)} 字符，已保留头部 {max_chars - 500} + 尾部 500 字符] ...\n\n" + tail
                    truncated = True
                return {
                    "doc_name": doc_name,
                    "path": doc["path"],
                    "description": doc["description"],
                    "category": doc["category"],
                    "content": content,
                    "char_count": len(content),
                    "truncated": truncated,
                }
        return {"error": f"doc '{doc_name}' not found", "available": [d["name"] for d in _PROJECT_DOCS]}

    # 模式 2：返回文档清单
    docs = []
    for doc in _PROJECT_DOCS:
        if category and doc["category"] != category:
            continue
        docs.append({
            "name": doc["name"],
            "description": doc["description"],
            "category": doc["category"],
        })
    return {"total": len(docs), "docs": docs}


@router.post("/run", response_model=AdvancedToolResponse, operation_id="localagent_advanced_tool")
async def advanced_tool(req: AdvancedToolRequest):
    """Execute an advanced/specialized tool by name. Use localagent_list_tools to discover names.

    This is a GATEWAY tool (Unity MCP style) that routes to 30+ low-frequency REST
    endpoints not directly exposed as MCP tools. Pass tool=<operation_id> and params={...}.

    Examples:
    - localagent_advanced_tool(tool="ocr_set_keep_models", params={"keep": true})
    - localagent_advanced_tool(tool="mindforge_preload", params={})
    - localagent_advanced_tool(tool="exec_output", params={"exec_id": "exec_xxx", "action": "range", "start": 0, "end": 100})

    Use localagent_list_tools to see all available tools and their parameters.
    Returns the target endpoint's JSON response in `result`.
    """
    info = _advanced_registry.get(req.tool)
    if not info:
        available = sorted(_advanced_registry.keys())
        raise HTTPException(
            status_code=404,
            detail=f"未知工具: {req.tool}。用 localagent_list_tools 查看可用工具。已注册: {available[:20]}{'...' if len(available)>20 else ''}",
        )

    # ADR-0023 DANGEROUS_TOOLS 权限检查：网关是内部函数调用不经过 HTTP 中间件，
    # 对危险工具集合中的端点强制走 command_guard GUI 审批。
    # 审批拒绝/失败时返回 403（不下发到目标端点）。
    if _is_dangerous_tool(req.tool):
        params_for_approval = dict(req.params) if req.params else {}
        approved, reason = await _check_dangerous_tool_approval(req.tool, params_for_approval)
        if not approved:
            return AdvancedToolResponse(
                success=False, tool=req.tool, status_code=403,
                result=None, error=reason,
            )

    path = info["path"]
    method = info["method"].lower()
    params = dict(req.params) if req.params else {}

    # 分离 path / query / body 参数
    path_params = {}
    query_params = {}
    for p in info["parameters"]:
        pname = p["name"]
        if pname in params:
            if p["in"] == "path":
                path_params[pname] = params.pop(pname)
            elif p["in"] == "query":
                query_params[pname] = params.pop(pname)

    # 替换路径参数
    for k, v in path_params.items():
        path = path.replace(f"{{{k}}}", str(v))

    # 剩余参数：若有 body 则作为 JSON body，否则作为 query
    body = None
    if info["has_body"]:
        body = params if params else None
    else:
        # 没有声明 body 的端点，剩余参数放进 query
        query_params.update(params)

    base = _backend_url()
    url = f"{base}{path}"

    # 长耗时 tool 用大超时，避免 MCP 网关 502（默认 60s 不够 loop_run_task 跑 monitor 等）
    # loop_run_task：触发 loop 任务（如仓库监控跑多个 GitHub API ~90s）
    _LONG_TIMEOUT_TOOLS = {"loop_run_task"}
    _timeout = 180.0 if req.tool in _LONG_TIMEOUT_TOOLS else 60.0

    try:
        async with httpx.AsyncClient(timeout=_timeout) as client:
            if method == "get":
                resp = await client.get(url, params=query_params or None)
            elif method == "post":
                resp = await client.post(url, params=query_params or None, json=body)
            elif method == "put":
                resp = await client.put(url, params=query_params or None, json=body)
            elif method == "delete":
                # httpx AsyncClient.delete() 不支持 json 参数（HTTP DELETE 通常不带 body）
                # 传 json=None 也会触发 TypeError，导致 advanced_tool 网关 500
                resp = await client.delete(url, params=query_params or None)
            elif method == "patch":
                resp = await client.patch(url, params=query_params or None, json=body)
            else:
                raise HTTPException(status_code=400, detail=f"不支持的方法: {method}")

        try:
            result = resp.json()
        except Exception:
            result = {"text": resp.text[:2000]}

        success = 200 <= resp.status_code < 400
        return AdvancedToolResponse(
            success=success, tool=req.tool,
            status_code=resp.status_code,
            result=result if success else None,
            error=None if success else f"HTTP {resp.status_code}: {result}",
        )
    except httpx.RequestError as e:
        logger.error(f"高级工具调用失败 {req.tool}: {e}")
        raise HTTPException(status_code=502, detail=f"内部调用失败: {e}") from None
