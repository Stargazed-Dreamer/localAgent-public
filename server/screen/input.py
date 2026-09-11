"""键鼠操控：SendInput 注入、参数防呆、操作执行。

拆分自原 server/screen.py。依赖 windows._ADMIN_STATUS。
外部模块通过 `from server.screen import _execute_action, _validate_action_params, SUPPORTED_ACTIONS` 访问。
"""

import ctypes
import logging
import time

from server.screen.windows import _ADMIN_STATUS

logger = logging.getLogger("localagent.screen")


# ========== Windows SendInput 结构体（用于原生键鼠事件注入，绕过 pyautogui 的 IME/双击问题） ==========

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]
    _pack_ = 8


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]
    _pack_ = 8


class _INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
        ]
        _pack_ = 8

    _anonymous_ = ("_union",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("_union", _UNION),
    ]
    _pack_ = 8


# ========== SendInput 注入函数 ==========

def _send_vk(vk_code: int) -> bool:
    """用 SendInput 发送虚拟键码（按下+抬起）。返回是否成功注入。"""
    import win32con
    extra = ctypes.pointer(ctypes.c_ulong(0))
    inputs = (_INPUT * 2)()
    inputs[0].type = win32con.INPUT_KEYBOARD
    inputs[0].ki = _KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=0, time=0, dwExtraInfo=extra)
    inputs[1].type = win32con.INPUT_KEYBOARD
    inputs[1].ki = _KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=win32con.KEYEVENTF_KEYUP, time=0, dwExtraInfo=extra)
    sent = ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))
    if sent != 2:
        logger.warning(f"_send_vk(vk={vk_code}) 注入失败，SendInput 返回 {sent}（可能被 UIPI 阻止）")
        return False
    return True


def _send_unicode_text(text: str, interval: float = 0.0):
    """用 SendInput + KEYEVENTF_UNICODE 逐字符注入文本，绕过输入法。

    参考"粘贴小工具.py"的 send_unicode：直接发送 Unicode 码点，不走键盘布局
    映射，不触发 IME 组合，不覆盖剪贴板。控制字符（\\n/\\t）用虚拟键发送。

    逐字符发送（而非批量）是刻意设计：interval 参数提供 per-char 节流，某些
    应用（如终端、远程桌面）处理快速批量 Unicode 输入时会丢字符。批量优化
    会破坏 interval 语义，因此保留逐字符。

    第二轮评估 P0-2 修复：
    - SendInput 的 wScan 是 16 位（USHORT），无法承载 > 0xFFFF 的 code point。
    - 对 emoji（U+1F600）和 CJK 扩展 B（U+20000-2A6D6）等辅助平面字符，
      必须拆成 UTF-16 代理对（高代理 + 低代理）两次 SendInput 发送，
      Windows 内核会自动组合为完整 Unicode 字符。
    - 修复"中E2E 中文  文 café "实际输入乱码/重复字符的问题。
    """
    import win32con
    extra = ctypes.pointer(ctypes.c_ulong(0))
    for char in text:
        if char == '\n':
            _send_vk(win32con.VK_RETURN)
            if interval > 0:
                time.sleep(interval)
            continue
        if char == '\t':
            _send_vk(win32con.VK_TAB)
            if interval > 0:
                time.sleep(interval)
            continue

        code_point = ord(char)

        # 孤立代理字符（不应出现于合法 Unicode 字符串，但防御性跳过）
        if 0xD800 <= code_point <= 0xDFFF:
            logger.warning(f"_send_unicode_text: 跳过孤立代理字符 U+{code_point:04X}")
            continue

        if code_point <= 0xFFFF:
            # BMP 字符：单次 SendInput 即可
            scan_codes = [code_point]
        else:
            # 辅助平面字符（> 0xFFFF）：拆成 UTF-16 代理对
            # 公式来自 RFC 2781：
            #   high_surrogate = 0xD800 + ((cp - 0x10000) >> 10)
            #   low_surrogate  = 0xDC00 + ((cp - 0x10000) & 0x3FF)
            high_surrogate = 0xD800 + ((code_point - 0x10000) >> 10)
            low_surrogate = 0xDC00 + ((code_point - 0x10000) & 0x3FF)
            scan_codes = [high_surrogate, low_surrogate]

        # 为每个 scan code 发送 keydown + keyup（代理对时两次 SendInput）
        for sc in scan_codes:
            inputs = (_INPUT * 2)()
            inputs[0].type = win32con.INPUT_KEYBOARD
            inputs[0].ki = _KEYBDINPUT(
                wVk=0, wScan=sc,
                dwFlags=win32con.KEYEVENTF_UNICODE,
                time=0, dwExtraInfo=extra,
            )
            inputs[1].type = win32con.INPUT_KEYBOARD
            inputs[1].ki = _KEYBDINPUT(
                wVk=0, wScan=sc,
                dwFlags=win32con.KEYEVENTF_UNICODE | win32con.KEYEVENTF_KEYUP,
                time=0, dwExtraInfo=extra,
            )
            ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))

        if interval > 0:
            time.sleep(interval)


def _send_double_click(x: int, y: int) -> bool:
    """用 SendInput 一次性发送 4 个鼠标事件（down/up/down/up），系统按时间
    间隔自动识别为原生双击。比 pyautogui.doubleClick 更可靠，能让 Qt 等框架
    正确触发 mouseDoubleClickEvent。
    """
    import win32con
    extra = ctypes.pointer(ctypes.c_ulong(0))
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    flags_seq = [
        win32con.MOUSEEVENTF_LEFTDOWN,
        win32con.MOUSEEVENTF_LEFTUP,
        win32con.MOUSEEVENTF_LEFTDOWN,
        win32con.MOUSEEVENTF_LEFTUP,
    ]
    inputs = (_INPUT * len(flags_seq))()
    for i, f in enumerate(flags_seq):
        inputs[i].type = win32con.INPUT_MOUSE
        inputs[i].mi = _MOUSEINPUT(
            dx=0, dy=0, mouseData=0,
            dwFlags=f, time=0, dwExtraInfo=extra,
        )
    sent = ctypes.windll.user32.SendInput(len(flags_seq), inputs, ctypes.sizeof(_INPUT))
    return sent == len(flags_seq)


def _send_mouse_button(x: int | None, y: int | None, button: str = "left", down: bool = True) -> bool:
    """SendInput 单个鼠标按下/释放事件（mouse_down/mouse_up 分段原语）。

    与 _send_double_click 同一事件结构。x/y 为 None 时在当前位置按键。
    返回是否成功注入。
    """
    import win32con
    if x is not None and y is not None:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.02)
    if button == "right":
        flag = win32con.MOUSEEVENTF_RIGHTDOWN if down else win32con.MOUSEEVENTF_RIGHTUP
    else:
        flag = win32con.MOUSEEVENTF_LEFTDOWN if down else win32con.MOUSEEVENTF_LEFTUP
    extra = ctypes.pointer(ctypes.c_ulong(0))
    inputs = (_INPUT * 1)()
    inputs[0].type = win32con.INPUT_MOUSE
    inputs[0].mi = _MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=extra,
    )
    sent = ctypes.windll.user32.SendInput(1, inputs, ctypes.sizeof(_INPUT))
    if sent != 1:
        logger.warning(f"_send_mouse_button({button}, down={down}) 注入失败，SendInput 返回 {sent}（可能被 UIPI 阻止）")
        return False
    return True


# ========== 支持的操作类型及说明（用于防呆提示） ==========

SUPPORTED_ACTIONS = {
    "click": "单击（需要 x, y）",
    "double_click": "双击（需要 x, y）",
    "right_click": "右键点击（需要 x, y）",
    "type": "输入文本（SendInput 注入，绕过 IME，支持中文/Unicode；带 20ms 间隔更稳；需要 text；可选 x, y 先点击再输入）",
    "type_immediate": "快速输入文本（同 type 但无间隔，更快；需要 text；可选 x, y 先点击再输入）",
    "hotkey": "组合键（需要 keys 列表，如 ['ctrl', 'c']）",
    "scroll": "滚动（需要 x, y；可选 direction, amount）",
    "drag": "拖拽（需要 x, y, dx, dy）",
    "mouse_down": "按下鼠标键不释放（SendInput；需要 x, y；可选 button=left/right；与 mouse_up 配对实现分段拖拽/长按）",
    "mouse_up": "释放鼠标键（可选 x, y 先移动再释放；可选 button=left/right；必须与 mouse_down 配对）",
    "mouse_move": "移动鼠标指针到指定位置（SetCursorPos，不点击；用于 hover 悬停/分段拖拽）",
}


# ========== 参数防呆检查 ==========

def _validate_action_params(action: str, x: int | None = None, y: int | None = None,
                            text: str | None = None, keys: list[str] | None = None,
                            direction: str = "down", amount: int = 3,
                            dx: int = 0, dy: int = 0, button: str = "left") -> tuple[bool, str]:
    """防呆参数检查。返回 (ok, message)。

    ok=False 时 message 包含错误原因和推荐做法。
    """
    if action not in SUPPORTED_ACTIONS:
        supported = "; ".join(f"{k}({v})" for k, v in SUPPORTED_ACTIONS.items())
        return False, f"未知 action='{action}'。支持的 action: {supported}"

    if action in ("click", "double_click", "right_click", "scroll", "drag",
                  "mouse_down", "mouse_move"):
        if x is None or y is None:
            return False, f"action='{action}' 需要 x 和 y 坐标参数（当前 x={x}, y={y}）。推荐：先用 screen_snapshot 获取窗口位置，或用 list_windows 查看窗口 bbox"
    if action in ("type", "type_immediate"):
        if not text:
            return False, f"action='{action}' 需要 text 参数（当前为空）。type 和 type_immediate 均用 SendInput 注入（绕过 IME，支持中文/Unicode），区别是 type 带 20ms 间隔更稳、type_immediate 无间隔更快"
    if action == "hotkey":
        if not keys or not isinstance(keys, list) or len(keys) == 0:
            return False, "action='hotkey' 需要 keys 列表参数，如 keys=['ctrl', 'c'] 或 keys=['alt', 'tab']"
    if action == "drag":
        if dx == 0 and dy == 0:
            return False, f"action='drag' 需要 dx 或 dy 偏移量（当前 dx={dx}, dy={dy}）"
    if action == "scroll":
        # direction 仅允许 down/up（pyautogui 正数=向上，负数=向下）
        if direction not in ("down", "up"):
            return False, f"action='scroll' 的 direction 只能是 'down' 或 'up'（当前 '{direction}'）"
    if action in ("mouse_down", "mouse_up"):
        if button not in ("left", "right"):
            return False, f"action='{action}' 的 button 只支持 'left' 或 'right'（当前 '{button}'）"
    return True, ""


# ========== 操作执行 ==========

def _execute_action(action: str, x: int | None = None, y: int | None = None,
                    text: str | None = None, keys: list[str] | None = None,
                    direction: str = "down", amount: int = 3,
                    dx: int = 0, dy: int = 0, button: str = "left") -> dict:
    """执行键鼠操作，返回结果"""
    if not _ADMIN_STATUS:
        return {"success": False, "message": "键鼠操控需要管理员权限！请以管理员身份启动后端。"}

    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    try:
        if action == "click":
            if x is not None and y is not None:
                pyautogui.moveTo(x, y, duration=0.1)
                time.sleep(0.05)
            pyautogui.click()
        elif action == "double_click":
            if x is not None and y is not None:
                _send_double_click(int(x), int(y))
            else:
                cur = pyautogui.position()
                _send_double_click(cur.x, cur.y)
        elif action == "right_click":
            pyautogui.rightClick(x, y)
        elif action == "type":
            if x is not None and y is not None:
                pyautogui.click(x, y)
                time.sleep(0.1)
            if text is not None:
                _send_unicode_text(text, interval=0.02)
        elif action == "type_immediate":
            if x is not None and y is not None:
                pyautogui.click(x, y)
                time.sleep(0.1)
            if text is not None:
                _send_unicode_text(text, interval=0.0)
        elif action == "hotkey":
            pyautogui.hotkey(*(keys or []))
        elif action == "scroll":
            # pyautogui: 正数=向上滚，负数=向下滚（与 scroll_capture 的约定一致）
            clicks = -amount if direction == "down" else amount
            pyautogui.scroll(clicks, x, y)
        elif action == "drag":
            pyautogui.moveTo(x, y)
            pyautogui.drag(dx, dy, duration=0.5)
        elif action == "mouse_down":
            if x is None or y is None:
                return {"success": False, "message": "mouse_down 需要 x, y 参数"}
            ok_down = _send_mouse_button(int(x), int(y), button=button, down=True)
            if not ok_down:
                return {"success": False, "message": "mouse_down 注入失败（可能被 UIPI 阻止）"}
        elif action == "mouse_up":
            if x is not None and y is not None:
                ok_up = _send_mouse_button(int(x), int(y), button=button, down=False)
            else:
                ok_up = _send_mouse_button(None, None, button=button, down=False)
            if not ok_up:
                return {"success": False, "message": "mouse_up 注入失败（可能被 UIPI 阻止）"}
        elif action == "mouse_move":
            if x is None or y is None:
                return {"success": False, "message": "mouse_move 需要 x, y 参数"}
            ctypes.windll.user32.SetCursorPos(int(x), int(y))
        else:
            return {"success": False, "message": f"未知操作类型: {action}"}

        return {"success": True, "message": f"操作 {action} 执行成功"}
    except Exception as e:
        return {"success": False, "message": f"操作执行失败: {e}"}
