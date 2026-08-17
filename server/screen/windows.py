"""窗口管理：枚举、查找、管理员权限检查、强力焦点激活。

拆分自原 server/screen.py。无内部依赖（仅用 win32gui/win32process/psutil/ctypes）。
外部模块通过 `from server.screen import _enum_windows, _find_window, _ADMIN_STATUS, _force_focus_window` 访问。
"""

import ctypes
import logging
import sys
import time

logger = logging.getLogger("localagent.screen")


# ========== 窗口枚举与查找 ==========

def _enum_windows() -> list[dict]:
    """枚举所有可见窗口

    返回 dict 含 hwnd/title/class_name/bbox/is_minimized/z_order/pid/process_name/is_foreground
    is_foreground 字段：当前前台窗口（win32gui.GetForegroundWindow 返回的 hwnd）标记为 True。
    """

    # 依赖一次性导入（缺失则提前返回空列表，避免回调内重复 import 开销）
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        logger.warning("pywin32/psutil 未安装，窗口枚举不可用")
        return []

    windows = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True

        class_name = ""
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            pass

        pid = 0
        process_name = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid:
                process_name = psutil.Process(pid).name()
        except Exception:
            pass

        is_minimized = win32gui.IsIconic(hwnd)

        # 获取Z序
        z_order = 0
        try:
            z_order = win32gui.GetWindow(hwnd, win32gui.GW_HWNDPREV)
        except Exception:
            pass

        windows.append({
            "hwnd": hwnd,
            "title": title,
            "class_name": class_name,
            "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
            "width": right - left,
            "height": bottom - top,
            "is_visible": True,
            "is_minimized": bool(is_minimized),
            "z_order": z_order,
            "pid": pid,
            "process_name": process_name,
        })
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception as e:
        logger.warning(f"EnumWindows 失败: {e}")

    # 标记前台窗口（GetForegroundWindow 在 EnumWindows 之后调用一次，避免回调内重复调用）
    # 用于 activity_tracker 的活动信号检测：焦点变化时 VL 优先放行
    try:
        foreground_hwnd = win32gui.GetForegroundWindow()
        if foreground_hwnd:
            for w in windows:
                if w.get("hwnd") == foreground_hwnd:
                    w["is_foreground"] = True
                else:
                    w["is_foreground"] = False
    except Exception as e:
        logger.debug(f"GetForegroundWindow 失败: {e}")

    return windows


def _find_window(title: str, process_name: str | None = None) -> dict | None:
    """按标题模糊匹配窗口。可选用 process_name 过滤（避免多应用同名窗口冲突）。

    参数:
        title: 窗口标题（先精确匹配，再包含匹配）
        process_name: 进程名过滤（如 "qbittorrent.exe"），不区分大小写。
                      多个应用可能有同名窗口（如"选项"），用此参数避免误中。
    """
    windows = _enum_windows()
    # 按 process_name 过滤
    if process_name:
        windows = [w for w in windows if process_name.lower() in w.get("process_name", "").lower()]
    # 精确匹配
    for w in windows:
        if w["title"] == title:
            return w
    # 包含匹配
    for w in windows:
        if title.lower() in w["title"].lower():
            return w
    return None


# ========== 管理员权限检查 ==========

def _is_admin() -> bool:
    """检查当前进程是否以管理员权限运行"""
    if sys.platform != "win32":
        return True  # 非Windows系统不需要管理员权限
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_ADMIN_STATUS = _is_admin()
if not _ADMIN_STATUS:
    logger.warning("未以管理员权限运行！键鼠操控可能被Windows UIPI阻止。请以管理员身份启动后端。")


# ========== 系统空闲时间（用户输入活动检测）==========

class _LASTINPUTINFO(ctypes.Structure):
    """Win32 LASTINPUTINFO 结构体，用于 GetLastInputInfo"""
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds() -> float:
    """获取系统空闲时间（秒）—— 距最后一次鼠标/键盘输入的时间

    用 Win32 GetLastInputInfo + GetTickCount 计算。
    用于 activity_tracker 的 idle 状态判定（替代旧的"采集间隔 >5 分钟"逻辑，
    后者只能检测后端是否运行，不能检测用户是否在操作）。

    Returns:
        空闲秒数（float）。非 Windows 平台或调用失败返回 -1.0 表示不可用。
    """
    if sys.platform != "win32":
        return -1.0
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return -1.0
        now_ms = ctypes.windll.kernel32.GetTickCount()
        # GetTickCount 返回毫秒，会回绕（约 49 天），但差值仍正确
        elapsed_ms = now_ms - lii.dwTime
        return max(0.0, elapsed_ms / 1000.0)
    except Exception as e:
        logger.debug(f"GetLastInputInfo 失败: {e}")
        return -1.0


# ========== 窗口焦点强力激活 ==========

def _force_focus_window(hwnd: int) -> bool:
    """强力激活指定窗口到前台。

    Windows UIPI 默认阻止 SetForegroundWindow，需要组合多套技巧：
    1. AttachThreadInput（共享输入状态，使 SetForegroundWindow 可用）
    2. ShowWindow + BringWindowToTop（恢复最小化 + 提到 Z 序顶）
    3. SetForegroundWindow（实际激活）
    4. 兜底：WScript.Shell.AppActivate（COM 接口，对 Qt/Electron 等自绘窗口更可靠）

    Args:
        hwnd: 目标窗口句柄
    Returns:
        True 表示最终前台窗口即目标窗口
    """
    if sys.platform != "win32":
        return False

    import win32con
    import win32gui

    try:
        # 最小化时先恢复
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)

        cur_fg = win32gui.GetForegroundWindow()
        if cur_fg == hwnd:
            return True

        # 方法 1: AttachThreadInput + SetForegroundWindow
        try:
            cur_tid = win32gui.GetWindowThreadProcessId(cur_fg)[0]
            target_tid = win32gui.GetWindowThreadProcessId(hwnd)[0]
            ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, True)
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.BringWindowToTop(hwnd)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.15)
            finally:
                ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, False)
        except Exception as e:
            logger.debug(f"AttachThreadInput 激活失败: {e}")

        if win32gui.GetForegroundWindow() == hwnd:
            return True

        # 方法 2: WScript.Shell.AppActivate（COM 接口，对自绘窗口更可靠）
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            # AppActivate 接受窗口标题或 PID，这里用窗口标题
            title = win32gui.GetWindowText(hwnd)
            if title:
                ok = shell.AppActivate(title)
                if ok:
                    time.sleep(0.2)
                    if win32gui.GetForegroundWindow() == hwnd:
                        return True
        except Exception as e:
            logger.debug(f"WScript.Shell.AppActivate 激活失败: {e}")

        # 最终检查
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as e:
        logger.warning(f"_force_focus_window 失败 hwnd={hwnd}: {e}")
        return False
