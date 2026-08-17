"""T11: file_write 工具（覆盖写入，自动创建父目录）"""

from __future__ import annotations

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult


class FileWriteTool(BuiltinTool):
    """写文件（覆盖模式），自动创建父目录。"""

    operation_id = "file_write"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        safe, reason = is_path_safe(file_path)
        if not safe:
            return self._error(tool_call, f"Path not safe: {reason}", "path_forbidden")

        try:
            from pathlib import Path
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            byte_count = len(content.encode("utf-8"))
            return self._success(
                tool_call,
                f"Wrote {byte_count} bytes to {file_path}",
            )
        except PermissionError:
            return self._error(
                tool_call, f"Permission denied: {file_path}", "permission_denied"
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Write failed: {type(e).__name__}: {e}",
                "write_error",
            )
