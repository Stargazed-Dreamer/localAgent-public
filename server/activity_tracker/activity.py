"""Activity 数据查看/编辑/审核 API

提供 daily 报告的 CRUD + 审核功能：
    GET    /activity/daily              列出所有 daily 文件（含 reviewed/）
    GET    /activity/daily/{name}       读取文件内容
    PUT    /activity/daily/{name}       编辑文件内容
    POST   /activity/daily/{name}/review 审核标记（移到 reviewed/ 子目录，永久保留）
    DELETE /activity/daily/{name}       删除文件（走 Windows 回收站，可恢复）

文件名安全：只允许 YYYYMMDD.md 或 YYYYMMDD_vN.md 格式（daily 文件）。
hourly 文件（YYYYMMDD_HH.md）由 loop_actions.py 直接写入 data/activity/hourly/，
不走本端点，因此不受此正则约束。

删除安全：DELETE 端点强制走 Windows 回收站（SHFileOperationW + FOF_ALLOWUNDO），
不使用 os.remove / unlink 等直接删除。符合项目硬约束"删除必走回收站"。
"""

import ctypes
import re
import shutil
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema
from server.config import get_loops_config

router = APIRouter(prefix="/activity", tags=["activity"])

# 只验证 daily 文件名（YYYYMMDD.md 或 YYYYMMDD_vN.md），防止路径遍历
# hourly 文件（YYYYMMDD_HH.md）不通过本端点访问
_NAME_RE = re.compile(r"^\d{8}(_v\d+)?\.md$")


# ===== Windows 回收站删除（SHFileOperationW）=====
# 项目硬约束：删除必走回收站。用 ctypes 调 Win32 API，无新依赖。
# FO_DELETE + FOF_ALLOWUNDO 把文件送到回收站，可通过资源管理器恢复。

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),  # FILEOP_FLAGS 是 WORD
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040  # 允许撤销（送到回收站而不是永久删除）
_FOF_NOCONFIRMATION = 0x0010  # 不弹系统确认对话框（GUI 已确认过）
_FOF_SILENT = 0x0004  # 不显示进度对话框
_FOF_NOERRORUI = 0x0400  # 不弹错误对话框（失败由调用方处理）


def _delete_to_recycle_bin(path: Path) -> None:
    """把文件送到 Windows 回收站（可恢复）。

    项目硬约束：删除必走回收站，不得用 os.remove / unlink。
    用 SHFileOperationW + FOF_ALLOWUNDO 实现，无新依赖（纯 ctypes）。

    Raises:
        OSError: Win32 API 调用失败（返回非 0）或用户取消（fAnyOperationsAborted）。
        RuntimeError: 非 Windows 平台（SHFileOperationW 不可用）。
    """
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")

    # pFrom 需要双 null 终止（Win32 API 约定）
    # 用绝对路径避免歧义
    p_from = str(path.resolve()) + "\0\0"

    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = p_from
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        # Win32 错误码（非 0 即失败）
        raise OSError(f"SHFileOperationW 失败，错误码: {result} (0x{result:08X})")
    if op.fAnyOperationsAborted:
        raise OSError("用户取消了删除操作")


def _daily_dir() -> Path:
    cfg = get_loops_config()
    tasks = cfg.get("tasks", {})
    at = tasks.get("activity_tracker", {})
    p = Path(at.get("daily_data_dir", "private_vault/activity/daily"))
    # 相对路径相对于项目根目录解析（activity.py 在 server/activity_tracker/，需 3 级 parent 到项目根）
    return p if p.is_absolute() else Path(__file__).resolve().parent.parent.parent / p


def _reviewed_dir() -> Path:
    return _daily_dir() / "reviewed"


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"文件名格式无效，应为 YYYYMMDD.md 或 YYYYMMDD_vN.md: {name}",
        )
    return name


def _find_file(name: str) -> Path | None:
    daily = _daily_dir()
    reviewed = _reviewed_dir()
    for d in (reviewed, daily):
        p = d / name
        if p.exists():
            return p
    return None


class DailyUpdateRequest(BaseSchema):
    content: str


@router.get("/daily", operation_id="activity_daily_list")
async def list_daily():
    """列出所有 daily 报告文件（含 reviewed/ 子目录）"""
    daily = _daily_dir()
    reviewed = _reviewed_dir()
    files = []

    for d, is_reviewed in [(daily, False), (reviewed, True)]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md"), reverse=True):
            stat = f.stat()
            files.append({
                "name": f.name,
                "reviewed": is_reviewed,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            })

    return {"total": len(files), "files": files}


@router.get("/daily/{name}", operation_id="activity_daily_read")
async def read_daily(name: str):
    """读取 daily 报告内容"""
    _validate_name(name)
    p = _find_file(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")
    content = p.read_text(encoding="utf-8")
    reviewed = p.parent.name == "reviewed"
    return {"name": name, "content": content, "reviewed": reviewed}


@router.put("/daily/{name}", operation_id="activity_daily_update")
async def update_daily(name: str, req: DailyUpdateRequest):
    """编辑 daily 报告内容"""
    _validate_name(name)
    p = _find_file(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")
    p.write_text(req.content, encoding="utf-8")
    return {"name": name, "size": len(req.content), "updated": True}


@router.post("/daily/{name}/review", operation_id="activity_daily_review")
async def review_daily(name: str):
    """审核标记：将 daily 文件移到 reviewed/ 子目录（永久保留，不被自动清理）"""
    _validate_name(name)
    p = _find_file(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")
    if p.parent.name == "reviewed":
        return {"name": name, "reviewed": True, "msg": "已在 reviewed/ 中"}
    reviewed = _reviewed_dir()
    reviewed.mkdir(parents=True, exist_ok=True)
    dest = reviewed / name
    if dest.exists():
        raise HTTPException(
            status_code=409,
            detail=f"reviewed/ 中已存在同名文件: {name}",
        )
    shutil.move(str(p), str(dest))
    return {"name": name, "reviewed": True, "moved_to": str(dest)}


@router.delete("/daily/{name}", operation_id="activity_daily_delete")
async def delete_daily(name: str):
    """删除 daily 报告文件（走 Windows 回收站，可恢复）

    项目硬约束：删除必走回收站。用 SHFileOperationW + FOF_ALLOWUNDO 实现，
    文件被送到回收站，可通过资源管理器恢复。不使用 os.remove / unlink。

    支持 daily/ 和 reviewed/ 子目录下的文件删除。
    """
    _validate_name(name)
    p = _find_file(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")

    location = "reviewed" if p.parent.name == "reviewed" else "daily"

    try:
        _delete_to_recycle_bin(p)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}") from None
    except OSError as e:
        # Win32 API 失败或用户取消
        raise HTTPException(
            status_code=500,
            detail=f"删除失败（回收站操作）: {e}",
        ) from e

    return {
        "name": name,
        "deleted": True,
        "recycled": True,
        "from_location": location,
        "msg": "文件已移到回收站，可通过资源管理器恢复",
    }
