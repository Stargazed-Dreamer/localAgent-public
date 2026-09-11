"""回收站删除公共模块 — 项目内统一的「可恢复删除」入口。

背景
----
本机 PowerShell 的 `Add-Type` 被安全策略拦截：

    Error: Command blocked for security:
    Add-Type compiles and loads .NET code at runtime

因此 `Microsoft.VisualBasic.FileIO.FileSystem::DeleteFile(..., SendToRecycleBin)`
这条常见路线不可用。所有需要「可恢复删除」的场景一律走本模块 ——
纯 ctypes 调用 Win32 `SHFileOperationW` + `FOF_ALLOWUNDO`，无子进程、无 .NET 依赖。

用法
----
作为库：

    from tools.disk.recycle import send_to_recycle, recycle_batch
    ok, err = send_to_recycle(r"F:\\path\\to\\file.txt")

    result = recycle_batch(paths, on_error="abort")   # abort | continue

作为命令行：

    uv run python tools/disk/recycle.py <path> [<path> ...]
    uv run python tools/disk/recycle.py --list paths.txt
    cat paths.txt | uv run python tools/disk/recycle.py --stdin

注意
----
- 只支持 Windows（本项目即 Windows-only），其他平台一律返回失败，绝不静默硬删
- `FOF_ALLOWUNDO` 是「进回收站」的关键标志，缺失即为永久删除
- `pFrom` 必须双 `\\0` 结尾（MSDN 要求）
- 本模块只做「删除」这一件事；扫描、分类、清单生成属上层 skill 职责

去重说明
--------
历史上项目内存在多份等价实现：

- `client/core/agent/builtin_tools/file_delete.py::_delete_to_recycle_bin`（产品代码）
- `workspace/disk_manager/scripts/cleanup.py::send_to_recycle`（工具脚本）

`client/` 是打包分发的产品代码，不能依赖 `tools/`（开发工具不参与发布），
故该文件保留自包含实现，属有意为之。**新增工具脚本请一律复用本模块。**
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

__all__ = [
    "send_to_recycle",
    "recycle_batch",
    "RecycleResult",
    "RecycleError",
]

# ========== Win32 常量 ==========

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040  # 允许撤销 —— 进回收站的关键，缺失即永久删除
FOF_NOCONFIRMATION = 0x0010  # 不弹确认对话框
FOF_SILENT = 0x0004  # 不显示进度对话框
FOF_NOERRORUI = 0x0400  # 不显示错误 UI

_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI


class RecycleError(RuntimeError):
    """回收站操作失败。"""


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """Windows Shell API 文件操作结构体。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RecycleError(
            "recycle bin only supported on Windows "
            f"(current platform: {sys.platform})"
        )


def send_to_recycle(path: str) -> Tuple[bool, str]:
    """送单个文件/目录到回收站。

    返回 `(ok, err_msg)`：
      - 成功 → `(True, "")`
      - 失败 → `(False, "<原因>")`，绝不抛异常，便于上层批量处理

    签名与 `workspace/disk_manager/scripts/cleanup.py` 的历史实现保持一致，
    可直接 drop-in 替换。
    """
    try:
        _require_windows()

        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path + "\0\0"  # MSDN 要求双 \0 结尾
        op.pTo = None
        op.fFlags = _FLAGS
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None

        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if res != 0:
            return False, f"SHFileOperationW 返回 {res}"
        if op.fAnyOperationsAborted:
            return False, "操作被中止"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 统一转成 (ok, err) 契约
        return False, str(exc)[:200]


@dataclass
class RecycleResult:
    """批量回收站删除的结果。"""

    succeeded: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (path, err_msg)
    aborted: bool = False

    @property
    def ok_count(self) -> int:
        return len(self.succeeded)

    @property
    def fail_count(self) -> int:
        return len(self.failed)

    def __str__(self) -> str:
        tail = " (已中止)" if self.aborted else ""
        return (
            f"回收站: 成功 {self.ok_count} 项, 失败 {self.fail_count} 项{tail}"
        )


def recycle_batch(
    paths: Iterable[str],
    on_error: str = "abort",
    progress_every: int = 0,
    progress_cb=None,
) -> RecycleResult:
    """批量送回收站。

    Args:
        paths: 待删除路径（文件或目录）
        on_error: `"abort"` 遇错即停；`"continue"` 跳过失败项继续
        progress_every: 每处理 N 项回调一次进度，0 表示不回调
        progress_cb: `callable(processed: int, result: RecycleResult)`

    Returns:
        RecycleResult
    """
    if on_error not in ("abort", "continue"):
        raise ValueError("on_error 必须是 'abort' 或 'continue'")

    result = RecycleResult()
    for i, path in enumerate(paths, 1):
        if not path or not path.strip():
            continue
        path = path.strip()

        ok, err = send_to_recycle(path)
        if ok:
            result.succeeded.append(path)
        else:
            result.failed.append((path, err))
            if on_error == "abort":
                result.aborted = True
                break

        if progress_every and i % progress_every == 0 and progress_cb:
            progress_cb(i, result)

    return result


def _read_lines(source: str) -> List[str]:
    if source == "-":
        return [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    with open(source, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="把文件/目录送入 Windows 回收站（可恢复）"
    )
    parser.add_argument("paths", nargs="*", help="待删除的路径")
    parser.add_argument("--list", metavar="FILE", help="从文件读取路径列表（每行一个）")
    parser.add_argument(
        "--stdin", action="store_true", help="从 stdin 读取路径列表（每行一个）"
    )
    parser.add_argument(
        "--on-error",
        choices=["abort", "continue"],
        default="abort",
        help="遇错即停（默认）还是跳过继续",
    )
    args = parser.parse_args(argv)

    if args.stdin:
        paths = _read_lines("-")
    elif args.list:
        paths = _read_lines(args.list)
    else:
        paths = list(args.paths)

    paths = [p for p in paths if p and p.strip()]
    if not paths:
        parser.error("未提供任何路径（用位置参数、--list 或 --stdin）")

    # 前置校验：路径必须存在，避免「假成功」
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        for p in missing[:10]:
            print(f"  [跳过] 不存在: {p}", file=sys.stderr)
        if len(missing) > 10:
            print(f"  ... 另有 {len(missing) - 10} 个不存在", file=sys.stderr)
        paths = [p for p in paths if p not in set(missing)]
        if not paths:
            return 1

    def _progress(n: int, res: RecycleResult) -> None:
        print(f"  已处理 {n}: 成功 {res.ok_count}, 失败 {res.fail_count}")

    result = recycle_batch(
        paths,
        on_error=args.on_error,
        progress_every=10,
        progress_cb=_progress,
    )

    print(result)
    for path, err in result.failed:
        print(f"  FAIL: {path} :: {err}", file=sys.stderr)

    return 0 if result.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
