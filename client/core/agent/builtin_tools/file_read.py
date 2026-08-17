"""T10: file_read 工具（支持 offset/limit）"""

from __future__ import annotations

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult


class FileReadTool(BuiltinTool):
    """读文件，支持 offset/limit 分段读取大文件。"""

    operation_id = "file_read"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        file_path = args.get("file_path", "")
        offset = max(1, int(args.get("offset", 1)))
        limit = args.get("limit")  # None = 全读

        safe, reason = is_path_safe(file_path)
        if not safe:
            return self._error(tool_call, f"Path not safe: {reason}", "path_forbidden")

        try:
            from pathlib import Path
            p = Path(file_path)
            if p.is_dir():
                return self._error(
                    tool_call,
                    f"Path is a directory, not a file: {file_path}",
                    "is_directory",
                )
            if not p.exists():
                return self._error(
                    tool_call, f"File not found: {file_path}", "not_found"
                )

            # 读全部行
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            # 应用 offset（1-based）
            start = offset - 1
            if limit is not None:
                end = start + int(limit)
                selected = lines[start:end]
                truncated = (start + int(limit)) < total_lines
            else:
                selected = lines[start:]
                # 超大文件提示分段读取
                if total_lines - start > 2000:
                    truncated = True
                    selected = selected[:2000]
                else:
                    truncated = False

            # 格式化：{line_num}→{content}
            formatted_lines = []
            for i, line in enumerate(selected):
                line_num = start + i + 1
                # 去掉末尾换行符后重新加（保持格式一致）
                content = line.rstrip("\n").rstrip("\r")
                formatted_lines.append(f"{line_num}→{content}")
            content = "\n".join(formatted_lines)

            # 超大文件提示
            next_action_hint = None
            if limit is None and total_lines - start > 2000:
                next_hint = (
                    f"文件共 {total_lines} 行，已返回第 {start+1}-{start+2000} 行。"
                    f"如需继续，用 offset={start+2001} 读取后续内容。"
                )
                content = content + f"\n\n[truncated] {next_hint}"
                next_action_hint = next_hint

            return self._success(
                tool_call, content,
                next_action_hint=next_action_hint,
                output_truncated=truncated,
            )
        except PermissionError:
            return self._error(
                tool_call, f"Permission denied: {file_path}", "permission_denied"
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Read failed: {type(e).__name__}: {e}",
                "read_error",
            )
