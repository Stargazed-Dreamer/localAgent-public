"""T13: file_grep 工具（ripgrep 封装）"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 (subprocess 用 capture_output=True, 无 shell=True)

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult


class FileGrepTool(BuiltinTool):
    """内容搜索（ripgrep 封装，支持正则）。"""

    operation_id = "file_grep"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        glob_filter = args.get("glob")
        output_mode = args.get("output_mode", "files_with_matches")

        if not pattern:
            return self._error(tool_call, "pattern is required", "invalid_argument")

        # path 安全校验（允许相对路径，因为是搜索目录不是写入）
        if path != ".":
            safe, reason = is_path_safe(path)
            if not safe:
                return self._error(
                    tool_call, f"Path not safe: {reason}", "path_forbidden"
                )

        # 检查 rg 是否可用
        rg = shutil.which("rg")
        if not rg:
            return self._error(
                tool_call,
                "ripgrep (rg) not installed. Install it or use file_glob + file_read instead.",
                "rg_not_available",
            )

        try:
            cmd = [rg, pattern, path]
            if glob_filter:
                cmd.extend(["--glob", glob_filter])
            if output_mode == "content":
                cmd.append("--line-number")  # 带行号
            elif output_mode == "count":
                cmd.append("--count-matches")
            # files_with_matches 是 rg 默认行为，不加参数

            # 限制输出（防超大结果）
            cmd.extend(["--max-count", "100"])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            # rg exit code: 0=有匹配, 1=无匹配, 2=错误
            if result.returncode == 2:
                return self._error(
                    tool_call,
                    f"rg error: {result.stderr.strip()}",
                    "invalid_pattern",
                )
            output = result.stdout.strip() if result.stdout else ""
            if not output:
                return self._success(tool_call, "(no matches)")

            # 截断保护
            if len(output) > 8192:
                output = output[:8192] + "\n\n[truncated, more matches exist...]"
                return self._success(
                    tool_call, output, output_truncated=True,
                    next_action_hint="结果被截断，可用更具体的 pattern 或缩小 path 范围",
                )
            return self._success(tool_call, output)
        except subprocess.TimeoutExpired:
            return self._error(
                tool_call, "rg timed out after 30s", "execution_timeout",
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Grep failed: {type(e).__name__}: {e}",
                "grep_error",
            )
