"""截图功能：全屏/窗口截图、滚动拼接、颜色命名。

拆分自原 server/screen.py。无内部依赖（_get_mss 自包含，复用线程本地 mss 实例）。
外部模块通过 `from server.screen import _capture_fullscreen, _capture_window, _get_fullscreen_offset, _images_equal, _find_overlap_and_stitch, _color_name` 访问。
"""

import io
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

from PIL import Image

logger = logging.getLogger("localagent.screen")


# ========== 模块常量（避免魔法数散落各处） ==========

_WINDOW_RESTORE_DELAY = 0.3      # 恢复最小化窗口后等待秒数
_FOREGROUND_RESTORE_DELAY = 0.2  # 提窗到前台后等待秒数
_STITCH_STRIP_RATIO = 0.3        # 滚动拼接时取前图底部 30% 作为模板
_STITCH_STRIP_MIN_H = 10         # 模板最小高度
_STITCH_DIFF_THRESHOLD = 15      # 拼接匹配阈值（平均像素差 < 此值视为重叠）
_STITCH_MIN_OVERLAP_RATIO = 0.05 # 最小重叠比例


# ========== mss 实例复用（线程本地） ==========

_mss_local = threading.local()


def _get_mss():
    """获取当前线程的 mss 实例（复用，避免每次截图重新初始化 DC/bitmap）"""
    sct = getattr(_mss_local, "sct", None)
    if sct is None:
        import mss
        sct = mss.mss()
        _mss_local.sct = sct
    return sct


# ========== 截图函数 ==========

def _capture_fullscreen() -> bytes:
    """全屏截图，返回PNG bytes"""
    sct = _get_mss()
    monitor = sct.monitors[0]  # 全屏（所有显示器合集）
    screenshot = sct.grab(monitor)
    # 转为PNG bytes
    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def is_desktop_locked() -> bool:
    """检测 Windows 会话是否处于锁屏（Winlogon 安全桌面，输入桌面不可访问）。

    锁屏时 OpenInputDesktop 返回 NULL 且 GetLastError==ERROR_ACCESS_DENIED(5)。
    非 Windows / 调用异常 / 错误码非 5 一律返回 False —— 宁可误判为"未锁屏"，
    让上层维持截图失败可见，也绝不让非锁屏故障被误当成锁屏跳过（避免掩盖真问题）。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
        user32.CloseDesktop.restype = ctypes.c_bool
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        # dwDesiredAccess = DESKTOP_ENUMERATE(0x0040)；锁屏时安全桌面不可枚举
        hdesk = user32.OpenInputDesktop(0, False, 0x0040)
        if not hdesk:
            return ctypes.GetLastError() == 5
        user32.CloseDesktop(hdesk)
        return False
    except Exception:
        return False


def _get_fullscreen_offset() -> tuple[int, int]:
    """获取全屏截图（mss.monitors[0]）左上角相对于主显示器(0,0)的偏移。

    双屏时 monitors[0].left 可能非零：
    - 副显示器在主显示器左边 → left 为负数
    - 副显示器在右边 → left=0，但 width 包含两个屏

    画在截图上的坐标 = pyautogui坐标 - offset。
    """
    try:
        sct = _get_mss()
        mon = sct.monitors[0]
        return mon["left"], mon["top"]
    except Exception:
        return 0, 0


def _capture_via_fullscreen_crop(hwnd: int) -> bytes | None:
    """全屏截图 + GetWindowRect 裁剪。不需要提窗/管理员权限，适用于 DirectX 全屏游戏。"""
    try:
        import win32gui
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            raise ValueError(f"窗口尺寸无效: {w}x{h}")
        sct = _get_mss()
        monitor = sct.monitors[0]
        screenshot = sct.grab(monitor)
        full_img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        # 虚拟屏偏移修正（副屏在左边时 monitors[0].left 为负）
        off_x, off_y = monitor["left"], monitor["top"]
        crop_box = (
            max(0, left - off_x),
            max(0, top - off_y),
            min(full_img.size[0], right - off_x),
            min(full_img.size[1], bottom - off_y),
        )
        img = full_img.crop(crop_box)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"全屏截图+裁剪失败: {e}")
        return None


def _capture_window(hwnd: int, force_fullscreen_crop: bool = False) -> bytes | None:
    """指定窗口截图，返回PNG bytes。处理最小化和遮挡问题。

    force_fullscreen_crop=True 时跳过 PrintWindow（DirectX 全屏游戏假成功），
    直接用全屏截图+裁剪。
    """
    import win32con
    import win32gui
    from PIL import Image

    if force_fullscreen_crop:
        return _capture_via_fullscreen_crop(hwnd)

    # 检查是否最小化，先恢复
    was_minimized = win32gui.IsIconic(hwnd)
    if was_minimized:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(_WINDOW_RESTORE_DELAY)

    # 方法1: PrintWindow（后台截图，不干扰用户）
    hwnd_dc = None
    dc_obj = None
    compat_dc = None
    bitmap = None
    try:
        import ctypes

        import win32ui
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            raise ValueError(f"窗口尺寸无效: {w}x{h}")

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        dc_obj = win32ui.CreateDCFromHandle(hwnd_dc)
        compat_dc = dc_obj.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(dc_obj, w, h)
        compat_dc.SelectObject(bitmap)

        # PrintWindow with PW_RENDERFULLCONTENT
        result = ctypes.windll.user32.PrintWindow(hwnd, compat_dc.GetSafeHdc(), 2)

        if result:
            bmp_info = bitmap.GetInfo()
            bmp_bits = bitmap.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB",
                (bmp_info["bmWidth"], bmp_info["bmHeight"]),
                bmp_bits, "raw", "BGRX", 0, 1
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        # result=0: PrintWindow 失败，走 fallback
    except Exception as e:
        logger.debug(f"PrintWindow截图失败: {e}，尝试提窗截图")
    finally:
        # GDI 资源清理（无论成功/失败/异常）
        if compat_dc is not None:
            try:
                compat_dc.DeleteDC()
            except Exception:
                pass
        if dc_obj is not None:
            try:
                dc_obj.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass
        if bitmap is not None:
            try:
                # PyCBitmap stub 未声明 DeleteObject，Any 承载动态属性
                _bitmap_dynamic: Any = bitmap
                _bitmap_dynamic.DeleteObject()
            except Exception:
                pass

    # 方法2: 提窗到前台截图
    try:
        # 保存当前前台窗口
        try:
            old_foreground = win32gui.GetForegroundWindow()
        except Exception:
            old_foreground = None

        win32gui.SetForegroundWindow(hwnd)
        time.sleep(_FOREGROUND_RESTORE_DELAY)

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        sct = _get_mss()
        monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        screenshot = sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

        # 恢复前台窗口
        if old_foreground:
            try:
                win32gui.SetForegroundWindow(old_foreground)
            except Exception:
                pass

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"提窗截图失败: {e}，尝试全屏截图+裁剪")

    # 方法3: mss 全屏截图 + 裁剪（最终 fallback，复用 _capture_via_fullscreen_crop）
    return _capture_via_fullscreen_crop(hwnd)


# ========== 滚动截图拼接辅助 ==========

def _images_equal(img_a: bytes, img_b: bytes) -> bool:
    """快速判断两张 PNG bytes 是否相同(用文件哈希)。"""
    import hashlib
    return hashlib.md5(img_a).hexdigest() == hashlib.md5(img_b).hexdigest()


def _find_overlap_and_stitch(images: Sequence[Image.Image], min_overlap_ratio: float = _STITCH_MIN_OVERLAP_RATIO) -> Image.Image:
    """拼接多张图片为长图,自动检测重叠区域。

    算法:对相邻两张图 A(上) B(下),在 B 中搜索与 A 底部最匹配的行,
    裁剪 B 的匹配位置之前的内容后拼接。

    Args:
        images: PIL Image 列表(从上到下)
        min_overlap_ratio: 最小重叠比例(避免误匹配)

    Returns:
        拼接后的 PIL Image
    """
    import numpy as np
    from PIL import Image

    if not images:
        raise ValueError("images 为空")
    if len(images) == 1:
        return images[0]

    # 统一宽度(取最窄)
    min_width = min(img.width for img in images)
    images = [img if img.width == min_width else img.crop((0, 0, min_width, img.height)) for img in images]

    result = images[0]
    for i in range(1, len(images)):
        prev = result
        curr = images[i]

        # 在 curr 中搜索与 prev 底部最匹配的位置
        prev_arr = np.array(prev.convert("RGB"))
        curr_arr = np.array(curr.convert("RGB"))

        # 取 prev 底部固定比例作为模板
        strip_h = max(_STITCH_STRIP_MIN_H, int(prev.height * _STITCH_STRIP_RATIO))
        strip = prev_arr[-strip_h:, :, :].astype(np.int64)

        best_y = 0
        best_diff = float("inf")
        # 在 curr 中逐行搜索(步长 2 加速)
        for y in range(0, min(curr.height - strip_h, prev.height), 2):
            region = curr_arr[y:y + strip_h, :, :].astype(np.int64)
            diff = np.mean(np.abs(region - strip))
            if diff < best_diff:
                best_diff = diff
                best_y = y

        # 判断是否真的重叠(diff 足够小)
        min_overlap = max(5, int(curr.height * min_overlap_ratio))
        if best_diff < _STITCH_DIFF_THRESHOLD and best_y > min_overlap:
            # 有重叠,裁剪 curr 的重叠部分
            stitched = Image.new("RGB", (min_width, prev.height + curr.height - best_y))
            stitched.paste(prev, (0, 0))
            stitched.paste(curr, (0, prev.height - best_y))
        else:
            # 无重叠或匹配度低,直接拼接
            stitched = Image.new("RGB", (min_width, prev.height + curr.height))
            stitched.paste(prev, (0, 0))
            stitched.paste(curr, (0, prev.height))

        result = stitched

    return result


# ========== 颜色命名（图像特征分析辅助） ==========

def _color_name(r: int, g: int, b: int) -> str:
    """简单的颜色命名（中文）"""
    # 灰度判断
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx - mn < 20:
        if mx < 30:
            return "黑"
        if mx > 230:
            return "白"
        if mx < 80:
            return "深灰"
        if mx < 160:
            return "灰"
        return "浅灰"
    # 主色判断
    if r >= g and r >= b:
        if g > b and r - b > 50:
            if g > 150:
                return "橙"
            return "红"
        if b > g:
            return "粉/紫"
        return "红"
    if g >= r and g >= b:
        if r > b:
            return "黄绿"
        return "绿"
    if b >= r and b >= g:
        if r > g:
            return "紫"
        if g > 100:
            return "青"
        return "蓝"
    return "其他"
