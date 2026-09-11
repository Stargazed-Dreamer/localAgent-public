"""clipboard 端点 — 剪贴板读写（ZCode computer-use 对齐项）。

ZCode computer-use 提供 read_clipboard / write_clipboard 原语；此前 LocalAgent
agent 只能靠 hotkey ctrl+c/ctrl+v 间接用剪贴板，无法直接读取内容做验证。
本模块补齐：
- GET  /screen/clipboard（read_clipboard）：读当前剪贴板文本（默认截断 2000 字符防上下文污染）
- POST /screen/clipboard（write_clipboard）：写文本到剪贴板（过 session 403 + 危险关键词 block）

实现用 ctypes 直调 Win32（OpenClipboard/GetClipboardData/SetClipboardData），
不引入新第三方依赖。注意 64 位下 HANDLE 返回值必须设 restype，否则指针截断。

导入本模块即触发 @router 注册，无需额外调用（routes.py 末尾统一 import）。
"""

import ctypes
import time
from ctypes import wintypes

from lib.schema import BaseSchema

# 从主模块复用共享件（router / 日志 / session 权限）
from server.screen.routes import (
    _enforce_session_permission,
    logger,
    router,
)

# 安全子模块（危险关键词 block 级拦截）
from server.screen.security import _check_danger

# ========== Win32 剪贴板常量 ==========

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_READ_TRUNCATE_CHARS = 2000  # 读响应默认截断长度（防大文本撑爆 LLM 上下文）
_WRITE_SUMMARY_CHARS = 100  # 写响应回显摘要长度


def _bind_signatures() -> tuple:
    """绑定 Win32 函数签名（64 位 HANDLE 防截断），返回 (user32, kernel32)。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return user32, kernel32


def _read_clipboard_text() -> tuple[bool, str, bool, str]:
    """读剪贴板文本。返回 (ok, text, has_text, error)。"""
    user32, kernel32 = _bind_signatures()
    if not user32.OpenClipboard(None):
        return False, "", False, "OpenClipboard 失败（剪贴板可能被其他进程占用，稍后重试）"
    try:
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return True, "", False, ""  # 剪贴板存在但无文本（如只复制了图片/文件）
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return False, "", False, "GetClipboardData 失败"
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False, "", False, "GlobalLock 失败"
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
        return True, text, True, ""
    finally:
        user32.CloseClipboard()


def _write_clipboard_text(text: str) -> tuple[bool, str]:
    """写文本到剪贴板。返回 (ok, error)。"""
    user32, kernel32 = _bind_signatures()
    if not user32.OpenClipboard(None):
        return False, "OpenClipboard 失败（剪贴板可能被其他进程占用，稍后重试）"
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)  # UTF-16 字节数（含结尾 null）
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False, "GlobalAlloc 失败"
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False, "GlobalLock 失败"
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            kernel32.GlobalUnlock(handle)
        # SetClipboardData 成功后系统接管该内存，禁止再 GlobalFree
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False, "SetClipboardData 失败"
        return True, ""
    finally:
        user32.CloseClipboard()


# ========== Pydantic 模型 ==========

class ClipboardWriteRequest(BaseSchema):
    text: str  # 要写入剪贴板的文本（过危险关键词 block 检查；确认级关键词不拦——写入本身不执行任何内容）


class ClipboardReadResponse(BaseSchema):
    success: bool
    text: str = ""  # 剪贴板文本（超长截断；full=true 时返回全文）
    has_text: bool = False  # False = 剪贴板无文本内容（可能是图片/文件）
    truncated: bool = False
    length: int = 0  # 完整文本长度（截断时供判断）
    full: bool = False
    elapsed_ms: int = 0
    message: str = ""


class ClipboardWriteResponse(BaseSchema):
    success: bool
    length: int = 0
    preview: str = ""  # 前 100 字符摘要（不回显全文，防上下文污染）
    elapsed_ms: int = 0
    message: str = ""


# ========== 端点 ==========

@router.get("/clipboard", response_model=ClipboardReadResponse, operation_id="read_clipboard")
def read_clipboard(full: bool = False):
    """读取当前剪贴板文本（ZCode computer-use 对齐原语）。

    典型用途：粘贴操作后读回验证内容、复制文本后直接取值（省 ctrl+c + OCR）。

    - 默认截断前 2000 字符（防大文本撑爆上下文），full=true 返回全文（调用方自担 token 风险）
    - has_text=false 表示剪贴板当前无文本内容（图片/文件等）
    - 读取剪贴板可能包含用户敏感信息，端点受会话权限保护（未授权返回 403）
    """
    _enforce_session_permission()
    t0 = time.perf_counter()
    ok, text, has_text, err = _read_clipboard_text()
    elapsed = int((time.perf_counter() - t0) * 1000)
    if not ok:
        return ClipboardReadResponse(
            success=False, elapsed_ms=elapsed, message=f"读取剪贴板失败: {err}",
        )
    truncated = len(text) > _READ_TRUNCATE_CHARS and not full
    out = text if not truncated else text[:_READ_TRUNCATE_CHARS]
    msg = "" if has_text else "剪贴板当前无文本内容（可能是图片/文件/空）"
    if truncated:
        msg = f"文本超长已截断（完整长度 {len(text)} 字符，full=true 可取全文）"
    return ClipboardReadResponse(
        success=True, text=out, has_text=has_text,
        truncated=truncated, length=len(text), full=full,
        elapsed_ms=elapsed, message=msg,
    )


@router.post("/clipboard", response_model=ClipboardWriteResponse, operation_id="write_clipboard")
def write_clipboard(req: ClipboardWriteRequest):
    """写文本到剪贴板（ZCode computer-use 对齐原语）。

    典型用途：长文本/特殊字符输入前置——写入剪贴板后 hotkey ctrl+v，
    绕过逐字符 SendInput 的 IME/丢字问题。

    安全：受会话权限保护（未授权 403）；文本过危险关键词 block 级拦截
    （确认级不拦——写入本身不执行任何内容）。响应只回前 100 字符摘要。
    """
    _enforce_session_permission()
    t0 = time.perf_counter()

    if _check_danger(req.text, None, None) == "block":
        return ClipboardWriteResponse(
            success=False, message="操作被安全策略拦截（文本包含危险关键词）",
        )

    ok, err = _write_clipboard_text(req.text)
    elapsed = int((time.perf_counter() - t0) * 1000)
    if not ok:
        logger.warning(f"写剪贴板失败: {err}")
        return ClipboardWriteResponse(
            success=False, elapsed_ms=elapsed, message=f"写入剪贴板失败: {err}",
        )
    return ClipboardWriteResponse(
        success=True, length=len(req.text),
        preview=req.text[:_WRITE_SUMMARY_CHARS], elapsed_ms=elapsed,
        message="已写入剪贴板",
    )
