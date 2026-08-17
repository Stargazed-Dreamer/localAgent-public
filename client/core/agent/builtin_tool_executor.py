"""v6.1 T20: BuiltinToolExecutor — 内置工具执行器

设计依据：
- spec D8.1：10 个内置基本工具在 client 本地执行，不经 HTTP
- spec D8：工具名去掉前缀，对模型来说就是普通工具
- spec D12：7 段说明书从 tool_specs.yaml 加载（builtin 段）

职责：
1. 注册 10 个内置工具实例
2. 提供 list_tool_entries() 给 ToolRegistry 物化用（含 7 段说明书）
3. async execute(tool_call) -> ToolResult：按 tool_call.name 路由到对应工具

不依赖 Qt（AskUserTool 的 ask_callback 由 GUI 注入，未注入时 fail-closed）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from client.core.agent.builtin_tools import ALL_BUILTIN_TOOLS
from client.core.agent.builtin_tools.ask_user import AskUserTool
from client.core.agent.builtin_tools.base import BuiltinTool
from client.core.agent.builtin_tools.web_fetch import WebFetchTool
from client.core.agent.builtin_tools.web_search import WebSearchTool
from client.core.agent.tool_registry import ToolEntry
from client.core.agent.types import (
    TOOL_SAFETY_APPROVAL_REQUIRED,
    TOOL_SAFETY_READ_ONLY,
    TOOL_SAFETY_SAFE,
    ToolCall,
    ToolResult,
    ToolResultVariant,
)

logger = logging.getLogger("localagent.agent.builtin_executor")


# 内置工具默认 safety 映射（决定是否走审批）
_DEFAULT_SAFETY = {
    "file_read": TOOL_SAFETY_READ_ONLY,
    "file_write": TOOL_SAFETY_SAFE,           # 有副作用但可逆
    "file_edit": TOOL_SAFETY_SAFE,
    "file_grep": TOOL_SAFETY_READ_ONLY,
    "file_glob": TOOL_SAFETY_READ_ONLY,
    "file_ls": TOOL_SAFETY_READ_ONLY,
    "file_delete": TOOL_SAFETY_APPROVAL_REQUIRED,  # 不可逆，需审批
    "ask_user": TOOL_SAFETY_READ_ONLY,             # 无副作用
    "web_search": TOOL_SAFETY_READ_ONLY,
    "web_fetch": TOOL_SAFETY_READ_ONLY,
}

# 内置工具 parameters JSON Schema（传给 LLM 的 function.parameters）
# 让 LLM 知道每个工具的参数名、类型、是否必填，而不是从 description 文本里猜
_BUILTIN_PARAMETERS: dict[str, dict] = {
    "file_read": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径（必填）"},
            "offset": {"type": "integer", "description": "起始行号（从 1 开始，默认 1）"},
            "limit": {"type": "integer", "description": "读取行数（默认全读）"},
        },
        "required": ["file_path"],
    },
    "file_write": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径（必填，覆盖写入）"},
            "content": {"type": "string", "description": "写入内容（必填）"},
        },
        "required": ["file_path", "content"],
    },
    "file_edit": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径（必填）"},
            "old_string": {"type": "string", "description": "要替换的原文（必填，必须唯一匹配）"},
            "new_string": {"type": "string", "description": "替换为的新文本（必填）"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false）"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    "file_grep": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（必填）"},
            "path": {"type": "string", "description": "搜索目录的绝对路径（默认当前目录）"},
            "glob": {"type": "string", "description": "文件名过滤（如 *.py）"},
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": "输出模式（默认 files_with_matches）",
            },
        },
        "required": ["pattern"],
    },
    "file_glob": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式（如 **/*.py，必填）"},
            "path": {"type": "string", "description": "搜索目录的绝对路径（默认当前目录）"},
        },
        "required": ["pattern"],
    },
    "file_ls": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录绝对路径（必填）"},
            "ignore": {"type": "array", "items": {"type": "string"}, "description": "忽略的 glob 模式列表"},
        },
        "required": ["path"],
    },
    "file_delete": {
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要删除的文件绝对路径列表（必填，需审批）",
            },
        },
        "required": ["file_paths"],
    },
    "ask_user": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "object"},
                "description": "问题列表（每个含 question/header/options 字段，必填）",
            },
        },
        "required": ["questions"],
    },
    "web_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词（必填）"},
            "num": {"type": "integer", "description": "结果数（默认 5）"},
        },
        "required": ["query"],
    },
    "web_fetch": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整 URL（必填）"},
        },
        "required": ["url"],
    },
}


# tool_specs.yaml 默认路径（与 ToolRegistry 一致）
_DEFAULT_SPECS_PATH = str(
    Path(__file__).resolve().parents[3] / "data" / "client" / "tool_specs.yaml"
)


def _load_default_specs() -> dict:
    """加载 tool_specs.yaml 的 builtin 段（fail-open）。"""
    try:
        import yaml
        with open(_DEFAULT_SPECS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("tool_specs.yaml load failed (fail-open): %s", e)
        return {}


class BuiltinToolExecutor:
    """内置工具执行器：注册 10 个内置工具，按 name 路由执行。"""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8766",
        ask_callback: Callable[[list[dict]], list[dict]] | None = None,
        tool_specs: dict | None = None,
    ):
        """初始化内置工具集。

        Args:
            base_url: 后端 URL（web_search/web_fetch fallback 用）
            ask_callback: GUI 注入的提问回调（同步阻塞，返回用户选择）
            tool_specs: tool_specs.yaml 完整内容（含 builtin 段）。
                        None 时自动从默认路径加载。
        """
        self._tools: dict[str, BuiltinTool] = {}
        if tool_specs is None:
            tool_specs = _load_default_specs()
        self._tool_specs = (tool_specs or {}).get("builtin", {}) or {}

        # 实例化所有工具
        for tool_cls in ALL_BUILTIN_TOOLS:
            # 需要特殊参数的工具
            if tool_cls is AskUserTool:
                tool = tool_cls(ask_callback=ask_callback)
            elif tool_cls in (WebSearchTool, WebFetchTool):
                tool = tool_cls(base_url=base_url)
            else:
                tool = tool_cls()
            self._tools[tool.operation_id] = tool
            logger.debug("Registered builtin tool: %s", tool.operation_id)

    def list_tool_entries(self) -> list[ToolEntry]:
        """返回所有内置工具的 ToolEntry（含 7 段说明书，source="builtin"）。

        ToolRegistry 调用此方法注册内置工具。
        """
        entries = []
        for op_id, _tool in self._tools.items():
            spec = self._tool_specs.get(op_id, {}) or {}
            entries.append(ToolEntry(
                operation_id=op_id,
                method="CALL",  # 内置工具无 HTTP method
                path="",        # 内置工具无 path
                safety=_DEFAULT_SAFETY.get(op_id, TOOL_SAFETY_SAFE),
                # parameters JSON Schema（让 LLM 知道参数名/类型/必填）
                parameters=_BUILTIN_PARAMETERS.get(op_id, {}),
                # 7 段说明书
                purpose=spec.get("purpose", ""),
                when_to_use=spec.get("when_to_use", ""),
                when_not_to_use=spec.get("when_not_to_use", ""),
                inputs_desc=spec.get("inputs_desc", ""),
                outputs_desc=spec.get("outputs_desc", ""),
                error_cases=spec.get("error_cases", ""),
                examples=spec.get("examples", ""),
                # 来源标识
                source="builtin",
                # ToolCapability（内置工具默认值）
                side_effect="none" if _DEFAULT_SAFETY.get(op_id) == TOOL_SAFETY_READ_ONLY else "reversible",
                idempotent=op_id in ("file_read", "file_grep", "file_glob", "file_ls", "ask_user", "web_search", "web_fetch"),
                timeout_seconds=30.0,
                max_output_bytes=8192,
                is_concurrency_safe=True,
            ))
        return entries

    def has_tool(self, operation_id: str) -> bool:
        """判断是否是内置工具。"""
        return operation_id in self._tools

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """按 tool_call.name 路由到对应内置工具执行。"""
        tool = self._tools.get(tool_call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Builtin tool '{tool_call.name}' not found",
                variant=ToolResultVariant.ERROR,
                error_type="not_found",
            )
        try:
            return await tool.execute(tool_call)
        except Exception as e:
            logger.exception("Builtin tool %s raised: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Builtin tool '{tool_call.name}' raised: {type(e).__name__}: {e}",
                variant=ToolResultVariant.ERROR,
                error_type="builtin_exception",
            )
