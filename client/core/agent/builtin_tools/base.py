"""T09: 内置工具基类 + 路径安全校验（回收 v6-07:289-339）"""

from __future__ import annotations

from pathlib import Path

from client.core.agent.types import ToolCall, ToolResult, ToolResultVariant

# 项目根目录（builtin_tools/base.py 在 client/core/agent/builtin_tools/，向上 4 层）
PROJECT_ROOT = Path(__file__).resolve().parents[4]


def is_path_safe(file_path: str, allowed_roots: list[Path] | None = None) -> tuple[bool, str]:
    r"""路径安全 4 重校验（回收 v6-07:289-339）。

    返回 (is_safe, reason)。is_safe=False 时 reason 含拒绝原因。

    检查项：
    1. 拒绝 relative path（必须能解析为绝对路径）
    2. 拒绝 root path（如 / / C:\）
    3. 拒绝 UNC path（如 \\server\share）
    4. 拒绝 null byte
    5. 拒绝 URL-encoded traversal（%2e%2e 解码后是 ..）
    6. 拒绝全 width Unicode（．． NFKC 后是 ..）
    7. realpath 解析后再次校验（防 symlink escape）
    8. 必须在 allowed_roots 之一内（默认 [PROJECT_ROOT]）
    """
    if not file_path or not isinstance(file_path, str):
        return False, "empty path"

    # 4. null byte
    if "\x00" in file_path:
        return False, "null byte in path"

    # 5. URL-encoded traversal（%2e%2e / %2E%2E）
    from urllib.parse import unquote
    decoded = unquote(file_path)
    if ".." in decoded and ".." not in file_path:
        return False, "URL-encoded traversal detected"

    # 6. 全 width Unicode（NFKC normalization）
    import unicodedata
    normalized = unicodedata.normalize("NFKC", file_path)
    if ".." in normalized and ".." not in file_path:
        return False, "Unicode fullwidth traversal detected"

    # 1. relative path（必须绝对路径）
    p = Path(file_path)
    if not p.is_absolute():
        return False, "relative path not allowed (must be absolute)"

    # 2. root path（如 / / C:\ / C:/）
    try:
        # Path.anchor 是驱动器+根（如 'C:\\' 或 '/'）
        if str(p.resolve()) == str(p.anchor) or file_path.rstrip("/\\") == str(p.anchor).rstrip("/\\"):
            return False, "root path not allowed"
    except (OSError, ValueError):
        return False, "path resolution failed"

    # 3. UNC path（\\server\share）
    if file_path.startswith("\\\\") or file_path.startswith("//"):
        return False, "UNC path not allowed"

    # 7. realpath 解析（防 symlink escape）
    try:
        real = p.resolve()
    except (OSError, RuntimeError):
        return False, "realpath resolution failed"

    # 8. allowed_roots 检查（默认项目根目录）
    roots = allowed_roots if allowed_roots else [PROJECT_ROOT]
    for root in roots:
        try:
            real.relative_to(root.resolve())
            return True, "ok"
        except ValueError:
            continue
    return False, f"path outside allowed roots (project root: {PROJECT_ROOT})"


class BuiltinTool:
    """内置工具 Protocol（duck typing，子类实现 execute + 元数据属性）。

    子类必须定义：
    - operation_id: str — 工具名（如 "file_read"）
    - async execute(self, tool_call: ToolCall) -> ToolResult — 执行逻辑

    7 段说明书字段从 tool_specs.yaml 加载（BuiltinToolExecutor 负责注入）。
    """

    operation_id: str = ""

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        raise NotImplementedError

    def _error(self, tool_call: ToolCall, content: str, error_type: str) -> ToolResult:
        """构造错误 ToolResult。"""
        return ToolResult(
            tool_call_id=tool_call.id,
            content=content,
            variant=ToolResultVariant.ERROR,
            error_type=error_type,
        )

    def _success(self, tool_call: ToolCall, content: str, **kwargs) -> ToolResult:
        """构造成功 ToolResult。"""
        return ToolResult(
            tool_call_id=tool_call.id,
            content=content,
            variant=ToolResultVariant.SUCCESS,
            **kwargs,
        )
