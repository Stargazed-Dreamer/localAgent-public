"""焦点安全：canonical window token、焦点证据、焦点泄漏检测。

评估文档（docs/evaluations/2026-07-20-computer-use-vs-localagent.md）P0 改进：
1. 键盘动作必须拒绝未知焦点（FOCUS_NOT_VERIFIED）
2. 规范化 UWP/WinUI 窗口句柄（GetAncestor GA_ROOTOWNER 解析）
3. 每次动作返回焦点证据（foreground_before/after、target_match 等）
4. 保护 ChatGPT/Codex/终端窗口（FOCUS_LEAK_PREVENTED）
5. 即使用户批准（allow_unfocused_input=true），也必须返回真实前台窗口

外部模块通过 `from server.screen.focus import verify_focus_for_input, collect_focus_evidence, resolve_canonical_window` 访问。
"""

import logging
import sys

logger = logging.getLogger("localagent.screen")


# ========== 状态码常量（动作响应中返回，便于 agent 编程判断） ==========

FOCUS_NOT_VERIFIED = "FOCUS_NOT_VERIFIED"
"""目标窗口已指定但前台不匹配且未启用 allow_unfocused_input——零按键被发送。"""

FOCUS_LEAK_PREVENTED = "FOCUS_LEAK_PREVENTED"
"""检测到 ChatGPT/Codex/终端等高影响窗口在前台但目标不是它——立即阻断，避免输入泄漏到 agent 宿主。"""

STALE_COORDINATES = "STALE_COORDINATES"
"""截图 snapshot 后窗口位置/大小/DPI 变化，坐标失效——agent 必须重新截图定位。"""

EXECUTED_UNVERIFIED = "executed_unverified"
"""动作已执行但无 postcondition 验证（OCR/UIA/像素未声明或失败）——不是 success。"""


# ========== 受保护进程（默认：agent 宿主窗口，输入泄漏高影响目标） ==========

DEFAULT_PROTECTED_PROCESSES = [
    "ChatGPT.exe",
    "Codex.exe",
    "trae.exe",         # Trae IDE（用户使用的 agent 宿主之一）
    "Trae.exe",
    "cursor.exe",       # Cursor
    "Cursor.exe",
]


# ========== Canonical Window Token ==========

def resolve_canonical_window(hwnd: int) -> dict:
    """把任意 HWND 解析为 canonical window token + 同族 HWND 集合。

    UWP/WinUI 应用存在 ApplicationFrameHost（外框）、核心子窗口、可见根窗口多层句柄，
    标题相同但 HWND 不同。本函数通过 GetAncestor(GA_ROOTOWNER) + GetAncestor(GA_ROOT)
    找到根，把同进程下所有可见 HWND 视为同族。

    Returns:
        {
            "canonical_hwnd": int,         # 根 owner HWND（作为 token 主键）
            "canonical_title": str,
            "canonical_pid": int,
            "canonical_process_name": str,
            "family_hwnds": list[int],     # 同族所有可见 HWND（含自身）
            "input_hwnd": int,             # 原始传入 hwnd（用于调试）
            "is_uwp_host": bool,           # 是否为 ApplicationFrameHost 等 host 进程
        }
    """
    result = {
        "canonical_hwnd": hwnd,
        "canonical_title": "",
        "canonical_pid": 0,
        "canonical_process_name": "",
        "family_hwnds": [hwnd],
        "input_hwnd": hwnd,
        "is_uwp_host": False,
    }
    if sys.platform != "win32" or not hwnd:
        return result

    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return result

    try:
        # 解析根 owner 和根 ancestor（UWP host/child 都会指向同一个根）
        root_owner = win32gui.GetAncestor(hwnd, win32con.GA_ROOTOWNER)
        root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        # canonical 选 root owner（处理 Owner 关系）；若与原 hwnd 相同则用 root
        canonical = root_owner if root_owner else root
        if not canonical:
            canonical = hwnd
        result["canonical_hwnd"] = canonical

        # 取 canonical 元数据
        result["canonical_title"] = win32gui.GetWindowText(canonical) or ""
        _, pid = win32process.GetWindowThreadProcessId(canonical)
        result["canonical_pid"] = pid
        if pid:
            try:
                result["canonical_process_name"] = psutil.Process(pid).name()
            except Exception:
                pass
        result["is_uwp_host"] = result["canonical_process_name"] in (
            "ApplicationFrameHost.exe",   # UWP 外框
            "ApplicationFrameHostWrapper.exe",
        )

        # 收集同族 HWND：遍历所有可见窗口，pid 与 canonical 相同的视为同族
        # （UWP host 和 child 通常同进程；跨进程 owned 窗口不在 family 内，避免误吸）
        family = []
        canonical_pid_final = pid

        def _collect(callback_hwnd, _):
            try:
                if not win32gui.IsWindowVisible(callback_hwnd):
                    return True
                _, cb_pid = win32process.GetWindowThreadProcessId(callback_hwnd)
                if cb_pid == canonical_pid_final:
                    family.append(callback_hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_collect, None)
        except Exception:
            family = [canonical]
        # 始终包含 canonical 自身
        if canonical not in family:
            family.append(canonical)
        result["family_hwnds"] = family
    except Exception as e:
        logger.debug(f"resolve_canonical_window 解析失败 hwnd={hwnd}: {e}")

    return result


def is_hwnd_in_family(target_hwnd: int, family_hwnds: list[int]) -> bool:
    """检查 HWND 是否在 canonical family 内（UWP host/child 任一作为输入都视为同族）。"""
    if not target_hwnd or not family_hwnds:
        return False
    return target_hwnd in family_hwnds


# ========== 焦点证据收集 ==========

def _hwnd_info(hwnd: int) -> dict:
    """收集单个 HWND 的标题/进程信息（用于焦点证据）"""
    info = {"hwnd": hwnd, "title": "", "pid": 0, "process_name": ""}
    if not hwnd:
        return info
    try:
        import psutil
        import win32gui
        import win32process
        info["title"] = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        info["pid"] = pid
        if pid:
            try:
                info["process_name"] = psutil.Process(pid).name()
            except Exception:
                pass
    except Exception:
        pass
    return info


def collect_focus_evidence(target_hwnd: int | None = None) -> dict:
    """收集当前焦点证据：foreground/focused control + 目标匹配判断。

    Returns:
        {
            "foreground": {hwnd, title, pid, process_name},
            "focus": {hwnd, title, pid, process_name},       # GUI thread focus（GetFocus）
            "target": {hwnd, title, pid, process_name} or None,
            "target_family_hwnds": list[int],
            "target_match": bool,                             # foreground 是否属于 target family
            "collected_at": float,                            # time.time()
        }
    """
    import time

    evidence = {
        "foreground": _hwnd_info(0),
        "focus": _hwnd_info(0),
        "target": _hwnd_info(target_hwnd) if target_hwnd else None,
        "target_family_hwnds": [],
        "target_match": False,
        "collected_at": time.time(),
    }

    if sys.platform != "win32":
        return evidence

    try:
        import win32gui
    except ImportError:
        return evidence

    # 1. foreground
    try:
        fg = win32gui.GetForegroundWindow()
        evidence["foreground"] = _hwnd_info(fg)
    except Exception:
        pass

    # 2. focus（GUI thread 焦点控件；与 foreground 不同——foreground 是顶层窗口，
    #    focus 是窗口内的子控件，对判断键盘事件去向更准确）
    try:
        # GetFocus 只对调用线程有效； AttachThreadInput 后可读其他线程的 focus
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd:
            import ctypes

            import win32process
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
            if cur_tid != target_tid:
                ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, True)
                try:
                    focused = win32gui.GetFocus()
                    evidence["focus"] = _hwnd_info(focused)
                finally:
                    ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, False)
            else:
                focused = win32gui.GetFocus()
                evidence["focus"] = _hwnd_info(focused)
    except Exception as e:
        logger.debug(f"collect_focus_evidence GetFocus 失败: {e}")

    # 3. target family + target_match
    if target_hwnd:
        canonical = resolve_canonical_window(target_hwnd)
        evidence["target_family_hwnds"] = canonical["family_hwnds"]
        # target_match：foreground 或 focus 落在 target family 内即视为匹配
        # （focus 比 foreground 更严格——子控件也必须在 family 内）
        fg_match = is_hwnd_in_family(evidence["foreground"]["hwnd"], canonical["family_hwnds"])
        focus_match = is_hwnd_in_family(evidence["focus"]["hwnd"], canonical["family_hwnds"])
        evidence["target_match"] = fg_match or focus_match

    return evidence


# ========== 焦点泄漏检测 ==========

def is_protected_process(process_name: str, protected_processes: list[str] | None = None) -> bool:
    """检查进程名是否在受保护列表（agent 宿主窗口）。"""
    if not process_name:
        return False
    name_lower = process_name.lower()
    protected = protected_processes if protected_processes is not None else DEFAULT_PROTECTED_PROCESSES
    return any(p.lower() == name_lower for p in protected)


def detect_focus_leak(
    target_hwnd: int | None,
    evidence: dict,
    protected_processes: list[str] | None = None,
) -> tuple[bool, str]:
    """检测焦点泄漏：目标不是受保护窗口但受保护窗口在前台。

    Returns:
        (leak_detected, reason)
        leak_detected=True 时必须阻断键盘输入（即使 allow_unfocused_input=true 也不能放行）
    """
    fg_proc = evidence.get("foreground", {}).get("process_name", "")
    if not is_protected_process(fg_proc, protected_processes):
        return False, ""

    # 前台是受保护进程——检查目标是否就是它
    if target_hwnd is None:
        # 没指定目标，但前台是受保护窗口——视为泄漏（输入会进入 agent 宿主）
        return True, f"前台为受保护进程 {fg_proc}，未指定目标窗口，禁止盲输入"

    # 目标已指定——检查目标 family 的 canonical process 是否就是该受保护进程
    target_canonical_proc = ""
    try:
        canonical = resolve_canonical_window(target_hwnd)
        target_canonical_proc = canonical.get("canonical_process_name", "")
    except Exception:
        pass

    if is_protected_process(target_canonical_proc, protected_processes):
        # 目标本身是受保护进程，前台也是它——这是合法的（agent 主动操作宿主）
        return False, ""

    # 目标不是受保护进程但前台是——泄漏
    return True, f"目标窗口进程 {target_canonical_proc or '未知'}，前台受保护进程 {fg_proc} 抢占——FOCUS_LEAK_PREVENTED"


# ========== 焦点验证主入口 ==========

def verify_focus_for_input(
    target_hwnd: int | None,
    *,
    allow_unfocused_input: bool = False,
    protected_processes: list[str] | None = None,
    evidence: dict | None = None,
) -> tuple[bool, str, dict]:
    """键盘/剪贴板/快捷键输入前的焦点强校验。

    决策逻辑（评估文档 P0）：
    1. 收集焦点证据
    2. 检测焦点泄漏（FOCUS_LEAK_PREVENTED 优先级最高，allow_unfocused_input 也不能放行）
    3. 目标已指定且 target_match=True → 通过
    4. 目标已指定但 target_match=False：
       - allow_unfocused_input=True → 通过（标记 unfocused_input_allowed）
       - allow_unfocused_input=False → 阻断 FOCUS_NOT_VERIFIED
    5. 目标未指定：
       - allow_unfocused_input=True → 通过
       - allow_unfocused_input=False → 阻断 FOCUS_NOT_VERIFIED

    Returns:
        (ok, reason, evidence)
        ok=True 时可发送输入；ok=False 时零按键被发送
        evidence 永远填充，用于响应中返回真实前台窗口（评估文档要求"不能伪称目标成功"）
    """
    if evidence is None:
        evidence = collect_focus_evidence(target_hwnd)

    # 1. 焦点泄漏检测（最高优先级，allow_unfocused_input 也不能放行）
    leak, leak_reason = detect_focus_leak(target_hwnd, evidence, protected_processes)
    if leak:
        return False, FOCUS_LEAK_PREVENTED, evidence

    # 2. 目标已指定且匹配
    if target_hwnd is not None and evidence.get("target_match"):
        return True, "focus_verified", evidence

    # 3. 未匹配（目标未指定或失配）——看 allow_unfocused_input
    if allow_unfocused_input:
        return True, "unfocused_input_allowed", evidence

    return False, FOCUS_NOT_VERIFIED, evidence
