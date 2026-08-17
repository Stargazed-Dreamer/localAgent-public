"""v6-lite T05: ToolRegistry — 从 /openapi.json 物化工具 catalog

设计依据：
- v6-lite §3 W2：ToolRegistry 从 /openapi.json 物化 catalog
- v6-lite §2：只收 `x-agent-callable=true` 路由（依赖 T00 fail-closed）
- v6-lite §4.1：完整 tool call 解析后再执行（不流式执行工具）
- v6-lite §4.10：工具副作用前先落 durable record（runner 负责）

职责：
1. 拉取 server /openapi.json
2. 过滤 `x-agent-callable=true` 路由，构造 ToolEntry（含 method/path/safety/parameters）
3. 暴露 `to_openai_tools()` 给 LLMGateway 传给模型
4. 暴露 `lookup(operation_id)` 给 HttpClientToolExecutor 用（拼 path + 解析参数）

不依赖 Qt（纯 Python，可在 CLI 脚本和 GUI 中复用）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from client.core.agent.types import (
    TOOL_SAFETY_AGENT_BLOCKED,
    TOOL_SAFETY_APPROVAL_REQUIRED,
    TOOL_SAFETY_READ_ONLY,
    TOOL_SAFETY_SAFE,
)

logger = logging.getLogger("localagent.agent.tool_registry")


# ============================================================================
# ToolEntry：单个工具的元数据（从 /openapi.json 一条 path+method 物化）
# ============================================================================


@dataclass
class ToolEntry:
    """单个工具的元数据。

    operation_id 是幂等键（与 MCP 工具的 operationId 一致）。
    safety 从 /openapi.json 的 x-tool-safety 取，未声明时按 method 推断：
      GET → read_only；POST/PUT/DELETE/PATCH → approval_required（保守）。

    parameters 是 OpenAPI requestBody 的 JSON Schema（用于构造 OpenAI tools 的 function.parameters）。
    若无 requestBody，parameters 为空 object schema（模型可传空 args）。

    T06（spec D12）：7 段说明书字段（自适应详略，空字段跳过不输出）：
    - purpose / when_to_use / when_not_to_use / inputs_desc / outputs_desc / error_cases / examples
    - 来源：data/client/tool_specs.yaml 手工编写

    T06（spec D8）：source 字段标识工具来源分桶：
    - "builtin"（内置基本工具，client 本地执行）
    - "core_mcp"（项目核心 MCP 工具，直接暴露）
    - "mcp_bucket"（MCP 工具桶成员，不直接暴露）
    - "rest"（server REST 端点，从 /openapi.json 物化）

    回收 v6-05 ToolCapability 7 字段（决定并发性 + fail-closed 默认值）。
    """

    operation_id: str
    method: str  # GET / POST / PUT / DELETE / PATCH
    path: str  # /todos/due 之类（含 {path_param} 占位符）
    summary: str = ""
    description: str = ""
    safety: str = TOOL_SAFETY_READ_ONLY
    parameters: dict = field(default_factory=dict)  # JSON Schema
    # path 参数定义（{xxx} 占位符的 schema）
    path_params: list[dict] = field(default_factory=list)
    # T06: 7 段说明书字段（自适应详略，空字符串表示"未配置"）
    purpose: str = ""
    when_to_use: str = ""
    when_not_to_use: str = ""
    inputs_desc: str = ""
    outputs_desc: str = ""
    error_cases: str = ""
    examples: str = ""
    # T06: 工具来源分桶标识
    source: str = "rest"  # "builtin" / "core_mcp" / "mcp_bucket" / "rest"
    mcp_server: str | None = None       # core_mcp/mcp_bucket 时填 MCP server 名
    mcp_sub_bucket: str | None = None   # mcp_bucket 时填子桶名（browser/screen/exec/...）
    # 回收 v6-05 ToolCapability 7 字段（fail-closed 默认值）
    side_effect: str = "unknown"          # "none"/"reversible"/"irreversible"/"unknown"
    idempotent: bool = False              # 是否幂等
    resource_keys: list[str] = field(default_factory=list)  # 资源锁键
    timeout_seconds: float = 30.0         # 工具超时
    max_output_bytes: int = 8192          # 最大输出字节（超则L0落盘）
    requires_admin: bool = False          # 是否需要管理员权限
    is_concurrency_safe: bool = False     # D4（spec D14）：默认 fail-closed（串行），
    #   新工具未声明时默认不可并发；read-only 内置工具在 builtin_tool_executor
    #   显式设 True 覆盖。v6-05 设计：默认串行更安全。

    def to_openai_tool(self) -> dict:
        """转 OpenAI tools 数组中的一项。

        格式：
            {"type": "function",
             "function": {"name": operation_id, "description": "...", "parameters": {...}}}

        description 里附加 safety 标识（让模型知道哪些工具需要审批）。

        T06: 自适应详略组装 7 段说明书。非空字段才输出，空字段跳过。
        """
        # 拼描述：7 段说明书优先（如已配置），否则回退到 summary/description
        parts: list[str] = []
        if self.purpose:
            parts.append(f"Purpose: {self.purpose}")
        elif self.summary:
            parts.append(self.summary)
        if self.when_to_use:
            parts.append(f"When to use: {self.when_to_use}")
        if self.when_not_to_use:
            parts.append(f"When NOT to use: {self.when_not_to_use}")
        if self.inputs_desc:
            parts.append(f"Inputs: {self.inputs_desc}")
        if self.outputs_desc:
            parts.append(f"Outputs: {self.outputs_desc}")
        if self.error_cases:
            parts.append(f"Error cases: {self.error_cases}")
        if self.examples:
            parts.append(f"Examples: {self.examples}")
        # 若 7 段都未配置，回退到 description（REST 端点从 openapi 来）
        if not parts and self.description and self.description != self.summary:
            parts.append(self.description)
        # 加 safety 提示（让模型理解工具的副作用等级）
        safety_hint = {
            TOOL_SAFETY_READ_ONLY: "[read-only, no side effects]",
            TOOL_SAFETY_SAFE: "[safe, minor side effects]",
            TOOL_SAFETY_APPROVAL_REQUIRED: "[requires user approval]",
            TOOL_SAFETY_AGENT_BLOCKED: "[blocked from agent]",
        }.get(self.safety, "")
        if safety_hint:
            parts.append(safety_hint)
        # 加 HTTP method + path 提示（仅 REST 端点；内置/core_mcp 不加）
        if self.source == "rest":
            parts.append(f"HTTP {self.method} {self.path}")
        desc = "\n".join(parts)

        # parameters：合并 path_params 和 requestBody 的 JSON Schema
        # OpenAI 期望 parameters 是一个 JSON Schema object（含 properties / required）
        params = self._build_merged_parameters()
        return {
            "type": "function",
            "function": {
                "name": self.operation_id,
                "description": desc,
                "parameters": params,
            },
        }

    def _build_merged_parameters(self) -> dict:
        """合并 path_params 和 requestBody 成一个 JSON Schema。

        path_params 转成 properties 的字符串字段（required=true）；
        requestBody 的 JSON Schema 直接合并进 properties。
        """
        if not self.path_params and not self.parameters:
            return {"type": "object", "properties": {}}

        properties: dict[str, Any] = {}
        required: list[str] = []

        # path 参数
        for p in self.path_params:
            name = p.get("name", "")
            if not name:
                continue
            properties[name] = {
                "type": "string",
                "description": p.get("description", f"Path parameter: {name}"),
            }
            if p.get("required", True):
                required.append(name)

        # requestBody 的 JSON Schema 合并
        if self.parameters:
            body_props = self.parameters.get("properties", {}) or {}
            body_required = self.parameters.get("required", []) or []
            for k, v in body_props.items():
                properties[k] = v
            for k in body_required:
                if k not in required:
                    required.append(k)

        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def build_request(self, args: dict) -> tuple[str, dict | None, dict | None]:
        """根据 args 构造 HTTP 请求的 path / query / json body。

        Args:
            args: 模型传过来的参数 dict

        Returns:
            (final_path, query_params, json_body)
            - final_path: 把 path 参数替换到 path 模板后的完整 path
            - query_params: GET 时的 query 参数（其他 method 为 None）
            - json_body: POST/PUT/PATCH 时的 body（GET/DELETE 为 None）
        """
        # 提取 path 参数并替换
        final_path = self.path
        path_param_names = {p["name"] for p in self.path_params if p.get("name")}
        path_args: dict = {}
        for name in path_param_names:
            if name in args:
                path_args[name] = args[name]
                # URL-encode path 参数值（防特殊字符）
                final_path = final_path.replace(
                    "{" + name + "}", quote(str(args[name]), safe="")
                )
        # 剩余参数作为 query 或 body
        remaining = {k: v for k, v in args.items() if k not in path_param_names}
        if self.method == "GET":
            return final_path, (remaining or None), None
        if self.method == "DELETE":
            # DELETE 通常无 body，但允许 query
            return final_path, (remaining or None), None
        # POST / PUT / PATCH：剩余参数作为 JSON body
        return final_path, None, (remaining or None)


# ============================================================================
# ToolRegistry：catalog 物化器
# ============================================================================


class ToolRegistry:
    """从 server /openapi.json 物化工具 catalog。

    用法：
        reg = ToolRegistry(base_url="http://127.0.0.1:8766")
        reg.refresh()  # 拉取并物化
        tools = reg.to_openai_tools()  # 传给 LLM
        entry = reg.lookup("todos_due")  # 查单个工具元数据
    """

    # T08: tool_specs.yaml 路径（data/client/tool_specs.yaml）
    _DEFAULT_SPECS_PATH = str(
        Path(__file__).resolve().parents[3] / "data" / "client" / "tool_specs.yaml"
    )

    def __init__(self, base_url: str = "http://127.0.0.1:8766", specs_path: str | None = None):
        self.base_url = base_url.rstrip("/")
        # T21-T24: 4 类工具分桶存储（lookup 优先级：builtin > core_mcp > rest）
        self._builtin_tools: dict[str, ToolEntry] = {}  # 内置基本工具
        self._core_mcp_tools: dict[str, ToolEntry] = {}  # 项目核心 MCP 工具
        self._localagent_sub_buckets: dict[str, list[ToolEntry]] = {}  # 本项目 MCP 子桶
        self._other_mcp_buckets: dict[str, list[ToolEntry]] = {}  # 其他 MCP server 桶
        self._entries: dict[str, ToolEntry] = {}  # REST 端点（旧字段保留）
        self._raw_spec: dict = {}
        self._last_refresh_ts: float = 0.0
        # T08: 加载 tool_specs.yaml（说明书配置）
        self._specs_path = specs_path or self._DEFAULT_SPECS_PATH
        self._tool_specs: dict = {}  # {source: {operation_id: {purpose, when_to_use, ...}}}
        self._load_tool_specs()

    # ------------------------------------------------------------------
    # 物化
    # ------------------------------------------------------------------

    def refresh(self, *, timeout: float = 10.0) -> int:
        """拉取 /openapi.json，物化 catalog，返回收录的工具数。

        失败时保留旧 catalog（fail-open，已有数据仍可用）。
        """
        import requests

        url = self.base_url + "/openapi.json"
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code != 200:
                logger.warning(
                    "ToolRegistry refresh failed: GET %s → %d", url, resp.status_code
                )
                return len(self._entries)
            spec = resp.json()
        except Exception as e:
            logger.warning("ToolRegistry refresh error: %s", e)
            return len(self._entries)

        self._raw_spec = spec
        self._entries = self._materialize(spec)
        # T08: 物化后应用 tool_specs.yaml 的说明书（覆盖默认 summary/description）
        self._apply_tool_specs()
        import time as _time
        self._last_refresh_ts = _time.time()
        logger.info(
            "ToolRegistry refreshed: %d tools materialized", len(self._entries)
        )
        return len(self._entries)

    def _load_tool_specs(self) -> None:
        """T08: 加载 tool_specs.yaml。失败时 fail-open（空 specs，工具用默认描述）。"""
        try:
            import yaml
        except ImportError:
            logger.warning("yaml not available, tool_specs.yaml not loaded")
            return
        try:
            with open(self._specs_path, encoding="utf-8") as f:
                self._tool_specs = yaml.safe_load(f) or {}
            logger.info(
                "tool_specs.yaml loaded: %d sources (builtin=%d, core_mcp=%d, rest=%d)",
                len(self._tool_specs),
                len(self._tool_specs.get("builtin", {})),
                len(self._tool_specs.get("core_mcp", {})),
                len(self._tool_specs.get("rest", {})),
            )
        except FileNotFoundError:
            logger.warning("tool_specs.yaml not found at %s (fail-open)", self._specs_path)
        except Exception as e:
            logger.warning("tool_specs.yaml load failed (fail-open): %s", e)

    def _apply_tool_specs(self) -> None:
        """T08: 按 operation_id 匹配，把 tool_specs.yaml 的说明书字段填充到 ToolEntry。

        匹配顺序：core_mcp → rest（builtin 工具在 BuiltinToolExecutor 注册时单独处理）。
        未匹配的工具保持默认空字符串（不影响功能）。
        """
        if not self._tool_specs:
            return
        # core_mcp 段：按 operation_id 匹配（去掉 mcp_localagent_ 前缀的简短名）
        core_mcp_specs = self._tool_specs.get("core_mcp", {}) or {}
        for op_id, spec in core_mcp_specs.items():
            entry = self._entries.get(op_id)
            if entry is None:
                # 尝试带前缀匹配（server openapi 可能用 mcp_localagent_xxx）
                entry = self._entries.get(f"mcp_localagent_{op_id}")
            if entry is None or not isinstance(spec, dict):
                continue
            self._fill_spec_fields(entry, spec, source="core_mcp")
        # rest 段：按 path 匹配
        rest_specs = self._tool_specs.get("rest", {}) or {}
        for path_key, spec in rest_specs.items():
            if not isinstance(spec, dict):
                continue
            for entry in self._entries.values():
                if entry.path == path_key:
                    self._fill_spec_fields(entry, spec, source="rest")
                    break

    @staticmethod
    def _fill_spec_fields(entry: ToolEntry, spec: dict, *, source: str) -> None:
        """把 spec dict 的字段填充到 ToolEntry（空值跳过）。"""
        entry.source = source
        for field_name in (
            "purpose", "when_to_use", "when_not_to_use",
            "inputs_desc", "outputs_desc", "error_cases", "examples",
        ):
            val = spec.get(field_name, "")
            if val:
                setattr(entry, field_name, str(val))

    def _materialize(self, spec: dict) -> dict[str, ToolEntry]:
        """从 OpenAPI spec 物化 ToolEntry 字典。

        只收 `x-agent-callable=true` 的 path+method。
        """
        entries: dict[str, ToolEntry] = {}
        paths = spec.get("paths") or {}
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method in ("get", "post", "put", "delete", "patch"):
                op = path_item.get(method)
                if not isinstance(op, dict):
                    continue
                # x-agent-callable 必须显式 true（fail-closed）
                extra = op.get("x-agent-callable")
                if extra is not True:
                    continue
                operation_id = op.get("operationId") or ""
                if not operation_id:
                    # 无 operationId，跳过（无法幂等标识）
                    continue
                # safety：从 x-tool-safety 取，缺失时按 method 推断
                safety = op.get("x-tool-safety")
                if not safety:
                    safety = (
                        TOOL_SAFETY_READ_ONLY
                        if method.upper() == "GET"
                        else TOOL_SAFETY_APPROVAL_REQUIRED
                    )
                # 跳过 agent_blocked
                if safety == TOOL_SAFETY_AGENT_BLOCKED:
                    continue
                # path 参数
                path_params = [
                    {
                        "name": p.get("name", ""),
                        "description": (p.get("description") or p.get("schema", {}).get("description", "")),
                        "required": p.get("required", True),
                    }
                    for p in (op.get("parameters") or [])
                    if p.get("in") == "path"
                ]
                # requestBody 的 JSON Schema
                parameters_schema: dict = {}
                request_body = op.get("requestBody")
                if request_body:
                    content = request_body.get("content") or {}
                    json_media = content.get("application/json") or {}
                    parameters_schema = json_media.get("schema") or {}
                    # 去掉 $ref 解析（简化：保留原样，模型不需要完整 schema 也能传参数）
                    parameters_schema = _resolve_schema(parameters_schema, spec)
                summary = op.get("summary", "")
                description = op.get("description", "")
                entries[operation_id] = ToolEntry(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=path,
                    summary=summary,
                    description=description,
                    safety=safety,
                    parameters=parameters_schema,
                    path_params=path_params,
                )
        return entries

    # ------------------------------------------------------------------
    # T21-T24: 4 类工具注册方法
    # ------------------------------------------------------------------

    def register_builtin_tools(self, executor) -> int:
        """T21: 从 BuiltinToolExecutor 拉取工具清单注册为内置工具。

        Args:
            executor: BuiltinToolExecutor 实例

        Returns:
            注册的工具数
        """
        entries = executor.list_tool_entries()
        for entry in entries:
            self._builtin_tools[entry.operation_id] = entry
        logger.info("Registered %d builtin tools", len(entries))
        return len(entries)

    def register_core_mcp_tools(self) -> int:
        """T22: 从 REST 端点中筛选已配置为 core_mcp 的工具，移到 _core_mcp_tools。

        依赖 tool_specs.yaml 的 core_mcp 段配置。
        必须在 refresh() 之后调用（_entries 已物化）。

        Returns:
            注册的核心 MCP 工具数
        """
        core_mcp_specs = self._tool_specs.get("core_mcp", {}) or {}
        count = 0
        for op_id in list(self._entries.keys()):
            spec = core_mcp_specs.get(op_id)
            if spec is None:
                # 尝试带前缀匹配
                if op_id.startswith("mcp_localagent_"):
                    short_name = op_id[len("mcp_localagent_"):]
                    spec = core_mcp_specs.get(short_name)
                    if spec is not None:
                        # 重命名为简短名
                        entry = self._entries.pop(op_id)
                        entry.operation_id = short_name
                        entry.source = "core_mcp"
                        entry.mcp_server = "mcp_localagent"
                        # 填充 7 段说明书
                        self._fill_spec_fields(entry, spec, source="core_mcp")
                        # 保存原始 operation_id 用于 HTTP 调用映射
                        entry.path = f"/mcp/{op_id}"  # 占位，实际调用走网关
                        self._core_mcp_tools[short_name] = entry
                        count += 1
                        continue
            if spec is not None:
                entry = self._entries.pop(op_id)
                entry.source = "core_mcp"
                entry.mcp_server = "mcp_localagent"
                self._fill_spec_fields(entry, spec, source="core_mcp")
                self._core_mcp_tools[op_id] = entry
                count += 1
        logger.info("Registered %d core MCP tools", count)
        return count

    def register_mcp_buckets(self) -> int:
        """T23-T24: 把剩余 REST 端点中的 MCP 工具按 server_name 归类到桶。

        本项目 MCP（mcp_localagent）按前缀分 7 个子桶：
        browser/screen/exec/ocr/vision/memory/miscellaneous
        其他 MCP server 整体作为一个桶。

        Returns:
            注册的桶数
        """
        # 剩余的 _entries 中可能有 mcp_localagent_* 工具，按前缀分桶
        remaining = list(self._entries.keys())
        bucket_count = 0
        for op_id in remaining:
            if not op_id.startswith("mcp_localagent_"):
                continue
            short_name = op_id[len("mcp_localagent_"):]
            # 已在 core_mcp 的跳过
            if short_name in self._core_mcp_tools or op_id in self._core_mcp_tools:
                continue
            sub_bucket = self._classify_localagent_tool(short_name)
            entry = self._entries.pop(op_id)
            entry.operation_id = short_name
            entry.source = "mcp_bucket"
            entry.mcp_server = "mcp_localagent"
            entry.mcp_sub_bucket = sub_bucket
            self._localagent_sub_buckets.setdefault(sub_bucket, []).append(entry)
        # 统计桶数
        bucket_count = len(self._localagent_sub_buckets) + len(self._other_mcp_buckets)
        logger.info(
            "Registered MCP buckets: %d localagent sub-buckets (%s), %d other buckets",
            len(self._localagent_sub_buckets),
            list(self._localagent_sub_buckets.keys()),
            len(self._other_mcp_buckets),
        )
        return bucket_count

    @staticmethod
    def _classify_localagent_tool(short_name: str) -> str:
        """T23: 按前缀归类本项目 MCP 工具到子桶。"""
        for prefix in ("browser_", "screen_", "exec_", "ocr_", "vision_", "memory_"):
            if short_name.startswith(prefix):
                return prefix.rstrip("_")
        # todos_/wip_/agent_guide 已在 core_mcp，这里不会到
        return "miscellaneous"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def to_openai_tools(self) -> list[dict]:
        """转 OpenAI tools 数组（传给 LLM 的 tools 参数）。

        T25: 按顺序组装：A 内置 → B 核心 MCP → C 本项目 MCP 子桶 → D REST 端点桶。
        MCP 子桶按桶概览呈现（避免 100+ 工具平铺）。
        D8 设计：REST 端点"按需调用"——折叠成 1 个桶，不展示清单，agent 需要时自己查。
        """
        tools: list[dict] = []
        # A. 内置基本工具（每个完整说明书）
        for entry in self._builtin_tools.values():
            tools.append(entry.to_openai_tool())
        # B. 核心 MCP 工具（每个完整说明书）
        for entry in self._core_mcp_tools.values():
            tools.append(entry.to_openai_tool())
        # C. 本项目 MCP 子桶（每桶一个概览工具）
        for bucket_name, entries in self._localagent_sub_buckets.items():
            tools.append(self._build_bucket_tool(bucket_name, entries, server="mcp_localagent"))
        # D. 其他 MCP server 桶
        for server_name, entries in self._other_mcp_buckets.items():
            tools.append(self._build_bucket_tool(server_name, entries, server=server_name, show_tools=False))
        # E. REST 端点桶（D8: 按需调用，折叠成 1 个桶，不展示清单）
        if self._entries:
            tools.append(self._build_rest_bucket_tool())
        return tools

    def _build_rest_bucket_tool(self) -> dict:
        """组装 REST 端点桶工具（1 个虚拟工具代表所有 REST 端点）。

        D8 设计：REST 端点"按需调用 + 完整 7 段说明书"，但实际 130+ 个端点大部分
        无说明书，全量塞给 LLM 是噪音。改为折叠成 1 个桶：
        - 不展示子工具清单（agent 看不到具体 operation_id）
        - 描述里给分类概览（按 path 前缀归类），让 agent 知道大致有什么类别
        - 查询入口：web_fetch /openapi.json 或 agent_guide(include_structure=true)
        - 调用方式：agent 知道具体 operation_id 后直接调（lookup 仍可查 _entries）

        lookup() 不变：agent 调具体 operation_id 时仍能查到 ToolEntry 执行。
        """
        # 按 path 前缀归类（/apikey/* /mindforge/* /loop/* 等）
        categories: dict[str, int] = {}
        for entry in self._entries.values():
            path = entry.path or "/"
            # 取 path 第一段作为分类
            parts = path.strip("/").split("/")
            cat = parts[0] if parts and parts[0] else "root"
            categories[cat] = categories.get(cat, 0) + 1
        # 拼分类概览
        cat_lines = [f"  - {cat} ({cnt} 个)" for cat, cnt in sorted(categories.items(), key=lambda x: -x[1])]
        cat_overview = "\n".join(cat_lines) if cat_lines else "  (无)"

        desc = "\n".join([
            f"Purpose: server REST 端点桶（{len(self._entries)} 个端点，按路径分类）",
            "When to use: 需要调用 server REST 端点时（健康检查/apikey 管理/mindforge 知识库/loop 任务/inbox/probe 等）",
            "When NOT to use: 不确定端点名时，先查清单再调",
            f"分类概览:\n{cat_overview}",
            f"查清单: web_fetch('{self.base_url}/openapi.json') 查完整 OpenAPI spec，或调 agent_guide(task=..., include_structure=true) 获取推荐",
            "调用方式: 知道具体 operation_id 后直接调（如 /health / apikey_status / mindforge_status），参数按 OpenAPI schema 填",
            f"[bucket: {len(self._entries)} REST endpoints, call by operation_id directly]",
        ])
        return {
            "type": "function",
            "function": {
                "name": "rest_endpoints",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation_id": {"type": "string", "description": "具体端点的 operationId（查 openapi.json 获取）"},
                        "args": {"type": "object", "description": "端点参数（按 OpenAPI schema 填）"},
                    },
                    "required": ["operation_id"],
                },
            },
        }

    @staticmethod
    def _build_bucket_tool(
        bucket_name: str,
        entries: list[ToolEntry],
        *,
        server: str,
        show_tools: bool = True,
    ) -> dict:
        """组装桶概览工具（一个虚拟工具代表整个桶）。

        show_tools=True 时在 description 中列出子工具清单（本项目 MCP）。
        show_tools=False 时不列（其他 MCP，按需 list_tools）。
        """
        tool_names = [e.operation_id for e in entries]
        parts = [
            f"Purpose: MCP bucket '{bucket_name}' ({server}) — {len(entries)} tools",
            f"When to use: 需要 {bucket_name} 相关能力时，调 localagent_advanced_tool(tool_name=...) 网关",
            "When NOT to use: 不确定工具名时先调 localagent_list_tools 查清单",
        ]
        if show_tools:
            # 列出子工具名（每行一个，限制 30 个）
            preview = tool_names[:30]
            tools_list = "\n".join(f"  - {n}" for n in preview)
            if len(tool_names) > 30:
                tools_list += f"\n  ... ({len(tool_names) - 30} more)"
            parts.append(f"Available tools:\n{tools_list}")
        parts.append("[bucket: call via localagent_advanced_tool(tool_name=..., params=...)]")
        desc = "\n".join(parts)
        return {
            "type": "function",
            "function": {
                "name": f"mcp_bucket_{bucket_name}",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string", "description": "子工具名"},
                        "params": {"type": "object", "description": "子工具参数"},
                    },
                    "required": ["tool_name"],
                },
            },
        }

    def lookup(self, operation_id: str) -> ToolEntry | None:
        """按 operation_id 查单个工具元数据。

        T21-T22: 优先查 builtin → core_mcp → rest。
        MCP bucket 成员不直接 lookup（通过网关调用）。
        """
        return (
            self._builtin_tools.get(operation_id)
            or self._core_mcp_tools.get(operation_id)
            or self._entries.get(operation_id)
        )

    def list_tools(self) -> list[ToolEntry]:
        """返回所有工具（按 operation_id 字母序）。"""
        return [self._entries[k] for k in sorted(self._entries.keys())]

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def last_refresh_ts(self) -> float:
        return self._last_refresh_ts

    # ------------------------------------------------------------------
    # 按安全等级筛选
    # ------------------------------------------------------------------

    def filter_by_safety(self, safety: str) -> list[ToolEntry]:
        """按 safety 等级筛选工具。"""
        return [e for e in self._entries.values() if e.safety == safety]

    def read_only_tools(self) -> list[ToolEntry]:
        """只读工具（safety=read_only）。"""
        return self.filter_by_safety(TOOL_SAFETY_READ_ONLY)

    def approval_required_tools(self) -> list[ToolEntry]:
        """需要审批的工具（safety=approval_required）。"""
        return self.filter_by_safety(TOOL_SAFETY_APPROVAL_REQUIRED)


# ============================================================================
# Schema 解析工具（简化版 $ref 解析，避免引入 openapi-spec-validator 等重依赖）
# ============================================================================


def _resolve_schema(schema: dict, root_spec: dict, _depth: int = 0) -> dict:
    """递归解析 schema 中的 $ref（防循环引用，最多 5 层）。

    OpenAPI $ref 格式："#/components/schemas/Foo"
    解析后返回 schema 的副本（含 properties / required / type 等）。
    """
    if _depth > 5:
        return schema
    if not isinstance(schema, dict):
        return schema
    # 复制一份避免修改原 schema
    out = dict(schema)
    # 解析 $ref
    ref = out.get("$ref")
    if ref and isinstance(ref, str) and ref.startswith("#/"):
        parts = ref[2:].split("/")
        target: Any = root_spec
        for p in parts:
            if isinstance(target, dict) and p in target:
                target = target[p]
            else:
                # 无法解析的 ref，保留原 schema
                return out
        if isinstance(target, dict):
            # 递归解析 target 内部的 $ref
            resolved = _resolve_schema(target, root_spec, _depth + 1)
            out.update(resolved)
            out.pop("$ref", None)
    # 递归处理 properties
    props = out.get("properties")
    if isinstance(props, dict):
        new_props = {}
        for k, v in props.items():
            new_props[k] = _resolve_schema(v, root_spec, _depth + 1) if isinstance(v, dict) else v
        out["properties"] = new_props
    # 处理 items（数组类型）
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _resolve_schema(items, root_spec, _depth + 1)
    return out
