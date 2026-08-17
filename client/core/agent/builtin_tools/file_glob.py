"""T14: file_glob 工具（文件名模式匹配）"""

from __future__ import annotations

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult


class FileGlobTool(BuiltinTool):
    """文件名模式匹配（pathlib.glob 封装）。"""

    operation_id = "file_glob"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        pattern = args.get("pattern", "*")
        path = args.get("path", ".")

        if not pattern:
            return self._error(tool_call, "pattern is required", "invalid_argument")

        # path 安全校验（允许相对路径）
        if path != ".":
            safe, reason = is_path_safe(path)
            if not safe:
                return self._error(
                    tool_call, f"Path not safe: {reason}", "path_forbidden"
                )

        try:
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                return self._error(
                    tool_call, f"Path not found: {path}", "not_found"
                )
            # glob 递归匹配
            matches = list(p.glob(pattern))
            # 按修改时间排序（最新的在前）
            matches.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
            # 限制 200 条
            truncated = len(matches) > 200
            matches = matches[:200]
            # 转字符串
            lines = [str(m) for m in matches]
            output = "\n".join(lines) if lines else "(no matches)"
            if truncated:
                output += "\n\n[truncated at 200 results, refine pattern if needed]"
            return self._success(
                tool_call, output, output_truncated=truncated,
            )
        except PermissionError:
            return self._error(
                tool_call, f"Permission denied: {path}", "permission_denied"
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Glob failed: {type(e).__name__}: {e}",
                "glob_error",
            )
