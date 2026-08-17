"""T16: file_delete 工具（删文件，支持多文件）

T05 安全加固：
- dry_run 默认 True：返回将删文件清单（不删）
- 实际删除走 Windows 回收站（ctypes SHFileOperationW + FOF_ALLOWUNDO）
- agent 必须显式传 dry_run=False 才真删（等同显式确认）
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path

from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.types import ToolCall, ToolResult

logger = logging.getLogger("localagent.file_delete")


# ========== Windows SHFileOperationW 回收站删除 ==========

# SHFILEOPSTRUCTW 结构体（Windows Shell API）
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040      # 允许撤销（删到回收站）
FOF_NOCONFIRMATION = 0x0010  # 不弹确认对话框（agent 调用无需 GUI 交互）
FOF_SILENT = 0x0004          # 不显示进度对话框
FOF_NOERRORUI = 0x0400       # 不显示错误 UI


def _delete_to_recycle_bin(file_path: str) -> tuple[bool, str | None]:
    """删除单个文件到回收站（Windows SHFileOperationW）。

    返回 (success, error)。success=True 表示已放入回收站（可恢复）。
    非 Windows 平台返回 (False, "recycle bin only supported on Windows")。
    """
    if sys.platform != "win32":
        return False, "recycle bin only supported on Windows (project is Windows-only)"
    try:
        # pFrom 需要双 \0 终止（MSDN 要求）
        path_buf = file_path + "\0\0"
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path_buf
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            return False, f"SHFileOperationW error code: {result}"
        # 检查是否被用户中止（fAnyOperationsAborted）
        if op.fAnyOperationsAborted:
            return False, "operation aborted by user"
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class FileDeleteTool(BuiltinTool):
    """删文件（支持多文件）。

    T05 安全加固：
    - dry_run 默认 True：返回将删文件清单（不真删）
    - 实际删除（dry_run=False）走回收站（SHFileOperationW + FOF_ALLOWUNDO）
    - 不再调用 Path.unlink() 直接删除
    """

    operation_id = "file_delete"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        file_paths = args.get("file_paths", [])
        # T05：dry_run 默认 True，agent 必须显式传 dry_run=False 才真删
        dry_run = args.get("dry_run", True)

        if not file_paths:
            return self._error(tool_call, "file_paths is required", "invalid_argument")

        # 统一成 list
        if isinstance(file_paths, str):
            file_paths = [file_paths]

        deleted: list[str] = []
        would_delete: list[str] = []  # dry_run 模式下"将删除"的文件
        errors: list[str] = []

        for fp in file_paths:
            safe, reason = is_path_safe(fp)
            if not safe:
                errors.append(f"{fp}: {reason}")
                continue
            try:
                p = Path(fp)
                if not p.exists():
                    errors.append(f"{fp}: not_found")
                    continue
                if p.is_dir():
                    errors.append(f"{fp}: is_directory (use rm command, not file_delete)")
                    continue

                if dry_run:
                    # T05：dry_run 模式只记录将删的文件，不真删
                    would_delete.append(fp)
                else:
                    # T05：实际删除走回收站（可恢复），不再用 Path.unlink()
                    ok, err = _delete_to_recycle_bin(fp)
                    if ok:
                        deleted.append(fp)
                    else:
                        errors.append(f"{fp}: {err}")
            except PermissionError:
                errors.append(f"{fp}: permission_denied")
            except Exception as e:
                errors.append(f"{fp}: {type(e).__name__}: {e}")

        # 构造输出
        parts: list[str] = []
        if dry_run:
            if would_delete:
                parts.append(
                    f"[DRY RUN] Would delete {len(would_delete)} file(s):\n"
                    + "\n".join(f"  - {fp}" for fp in would_delete)
                )
            parts.append(
                "[DRY RUN] No files were deleted. Pass dry_run=false to actually delete "
                "(files will be moved to recycle bin, recoverable)."
            )
        else:
            if deleted:
                parts.append(
                    f"Deleted {len(deleted)} file(s) to recycle bin (recoverable):\n"
                    + "\n".join(f"  - {fp}" for fp in deleted)
                )
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        output = "\n\n".join(parts) if parts else "(no action)"

        # 有错误且无成功删除时返回 ERROR variant
        if errors and not deleted and not would_delete:
            return self._error(tool_call, output, "delete_failed")
        return self._success(tool_call, output)
