"""Windows 任务栏闪烁（FlashWindowEx 封装）。

非 Windows 平台 no-op，不抛异常（跨平台兼容）。

行为：
- flash_window(hwnd, count=5)：闪 5 次，结束后任务栏按钮保持黄色高亮（Windows 默认）
- stop_flash(hwnd)：取消闪烁和高亮
"""
from __future__ import annotations

import sys

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    FLASHW_STOP = 0
    FLASHW_CAPTION = 0x00000001
    FLASHW_TRAY = 0x00000002
    FLASHW_ALL = FLASHW_CAPTION | FLASHW_TRAY

    class _FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    def flash_window(hwnd: int, count: int = 5) -> None:
        """闪烁任务栏按钮 count 次，结束后保持黄色高亮。

        Args:
            hwnd: 窗口句柄（int）
            count: 闪烁次数
        """
        if not hwnd:
            return
        info = _FLASHWINFO(
            cbSize=ctypes.sizeof(_FLASHWINFO),
            hwnd=hwnd,
            dwFlags=FLASHW_ALL,
            uCount=count,
            dwTimeout=0,  # 0 = 使用默认光标闪烁率
        )
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))

    def stop_flash(hwnd: int) -> None:
        """停止闪烁并取消高亮。"""
        if not hwnd:
            return
        info = _FLASHWINFO(
            cbSize=ctypes.sizeof(_FLASHWINFO),
            hwnd=hwnd,
            dwFlags=FLASHW_STOP,
            uCount=0,
            dwTimeout=0,
        )
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
else:
    def flash_window(hwnd: int, count: int = 5) -> None:
        """非 Windows 平台 no-op。"""
        return

    def stop_flash(hwnd: int) -> None:
        """非 Windows 平台 no-op。"""
        return


def flash_window_5_times(hwnd: int) -> None:
    """便捷封装：闪 5 次（spec 默认值）。"""
    flash_window(hwnd, count=5)
