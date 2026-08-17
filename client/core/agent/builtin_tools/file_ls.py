"""T15: file_ls 工具（列目录）"""

from __future__ import annotations

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult


class FileLsTool(BuiltinTool):
    """列目录内容。"""

    operation_id = "file_ls"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        path = args.get("path", ".")
        ignore = args.get("ignore", [])

        if not path:
            return self._error(tool_call, "path is required", "invalid_argument")

        # path 安全校验
        if path != ".":
            safe, reason = is_path_safe(path)
            if not safe:
                return self._error(
                    tool_call, f"Path not safe: {reason}", "path_forbidden"
                )

        try:
            import fnmatch
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                return self._error(
                    tool_call, f"Path not found: {path}", "not_found"
                )
            if not p.is_dir():
                return self._error(
                    tool_call, f"Not a directory: {path}", "not_directory"
                )

            entries = []
            for entry in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                name = entry.name
                # 应用 ignore 过滤
                if ignore and any(fnmatch.fnmatch(name, pat) for pat in ignore):
                    continue
                kind = "DIR" if entry.is_dir() else "FILE"
                size = ""
                try:
                    if entry.is_file():
                        size = f" ({entry.stat().st_size} bytes)"
                except OSError:
                    pass
                entries.append(f"[{kind}] {name}{size}")

            output = "\n".join(entries) if entries else "(empty directory)"
            return self._success(tool_call, output)
        except PermissionError:
            return self._error(
                tool_call, f"Permission denied: {path}", "permission_denied"
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"ls failed: {type(e).__name__}: {e}",
                "ls_error",
            )
