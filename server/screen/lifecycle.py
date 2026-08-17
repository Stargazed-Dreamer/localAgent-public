"""窗口生命周期管理：app_list/app_launch/app_wait/window_resolve + 窗口操作 + token 失效检测。

评估文档（docs/evaluations/2026-07-20-computer-use-vs-localagent.md）P1-B 改进：
1. 增加 app_list/app_launch/app_wait/window_resolve 端点
2. window resolve 支持 title/process/pid/automation_id，多匹配返回候选而非随便选择
3. token 在窗口重建后应明确失效（不默默指向同标题新窗口）
4. 最小化、恢复、置前、关闭分别返回操作后状态

外部模块通过 `from server.screen.lifecycle import (list_apps, launch_app, wait_for_window,
resolve_window, minimize_window, restore_window, raise_window, close_window,
verify_window_token, WINDOW_TOKEN_INVALID)` 访问。
"""

import logging
import subprocess
import sys
import time

from server.screen.focus import resolve_canonical_window
from server.screen.windows import _enum_windows, _force_focus_window

logger = logging.getLogger("localagent.screen")


# ========== 状态码常量 ==========

WINDOW_TOKEN_INVALID = "WINDOW_TOKEN_INVALID"
"""窗口 token 失效：hwnd 已销毁 / pid 不匹配 / 进程已重启（创建时间变化）。
agent 必须重新 window_resolve 获取新 token，不能继续用旧 token 操作。"""

WINDOW_AMBIGUOUS = "WINDOW_AMBIGUOUS"
"""window resolve 命中多个窗口，需用户/agent 提供更精确条件缩小范围。"""

WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
"""window resolve 未命中任何窗口。"""


# ========== App 列表（按进程名聚合） ==========

def list_apps() -> list[dict]:
    """列出当前运行的应用（按 process_name 聚合，含窗口数/pid 列表/代表性标题）。

    与 /screen/windows 不同：windows 列每个窗口，apps 按 process_name 聚合，
    便于 agent 决定调用 app_launch 还是直接 window_resolve。

    Returns:
        [{"process_name", "pid", "window_count", "sample_title", "hwnds": [...]}]
    """
    windows = _enum_windows()
    by_process: dict[str, dict] = {}
    for w in windows:
        pname = w.get("process_name") or "(unknown)"
        pid = w.get("pid", 0)
        key = f"{pname}@{pid}"
        if key not in by_process:
            by_process[key] = {
                "process_name": pname,
                "pid": pid,
                "window_count": 0,
                "sample_title": w.get("title", ""),
                "hwnds": [],
            }
        entry = by_process[key]
        entry["window_count"] += 1
        entry["hwnds"].append(w.get("hwnd", 0))
        # 用第一个非空标题作为 sample
        if not entry["sample_title"] and w.get("title"):
            entry["sample_title"] = w["title"]
    return list(by_process.values())


# ========== App 启动 ==========

def launch_app(
    command: str,
    args: list[str] | None = None,
    working_dir: str | None = None,
) -> dict:
    """启动应用（非阻塞，返回进程信息；如需等待窗口出现用 wait_for_window）。

    Args:
        command: 可执行文件路径或名称（如 "notepad.exe" / "C:\\Program Files\\...")
        args: 命令行参数列表
        working_dir: 工作目录（None 时用 command 所在目录）

    Returns:
        {"success", "pid", "message"}
    """
    args = args or []
    try:
        # 用 Popen 启动，capture pid；CREATE_NEW_CONSOLE 不需要（应用自带窗口）
        # shell=False 避免 command 注入；用户传入完整路径或 PATH 中的可执行文件名
        creationflags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS：子进程独立于父进程控制台，避免后端关闭时连累
            creationflags = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            [command] + args,
            cwd=working_dir,
            creationflags=creationflags,
            close_fds=True,
        )
        return {
            "success": True,
            "pid": proc.pid,
            "message": f"已启动 {command} (pid={proc.pid})",
        }
    except FileNotFoundError as e:
        return {"success": False, "pid": 0, "message": f"找不到可执行文件: {command} ({e})"}
    except Exception as e:
        return {"success": False, "pid": 0, "message": f"启动失败: {e}"}


# ========== 等待窗口出现 ==========

def wait_for_window(
    title: str | None = None,
    process_name: str | None = None,
    pid: int | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> dict:
    """轮询等待窗口出现（agent 启动应用后用此函数等窗口就绪）。

    任意条件命中即返回。多匹配时返回所有候选 + ambiguous=True（agent 应再 window_resolve 缩小范围）。

    Returns:
        {
            "success": bool,        # 是否在 timeout 内找到
            "status": "found" | "timeout" | "ambiguous",
            "matches": list[dict],  # 命中的窗口列表
            "elapsed_ms": int,
            "message": str,
        }
    """
    t0 = time.perf_counter()
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        windows = _enum_windows()
        matches = []
        for w in windows:
            if title and title.lower() not in (w.get("title") or "").lower():
                continue
            if process_name and process_name.lower() not in (w.get("process_name") or "").lower():
                continue
            if pid and w.get("pid") != pid:
                continue
            matches.append(w)
        if matches:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if len(matches) > 1:
                return {
                    "success": True,
                    "status": "ambiguous",
                    "matches": matches,
                    "elapsed_ms": elapsed_ms,
                    "message": f"找到 {len(matches)} 个匹配窗口（多匹配，请用更精确条件）",
                }
            return {
                "success": True,
                "status": "found",
                "matches": matches,
                "elapsed_ms": elapsed_ms,
                "message": f"找到窗口: {matches[0].get('title', '')}",
            }
        time.sleep(poll_interval)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "success": False,
        "status": "timeout",
        "matches": [],
        "elapsed_ms": elapsed_ms,
        "message": f"等待超时（{timeout}s）未找到匹配窗口",
    }


# ========== 窗口解析（多匹配候选 + token 颁发） ==========

def resolve_window(
    title: str | None = None,
    process_name: str | None = None,
    pid: int | None = None,
    automation_id: str | None = None,
) -> dict:
    """按 title/process/pid/automation_id 解析窗口，多匹配返回候选。

    与 _find_window 区别：
    - _find_window 模糊匹配后随便选一个返回；本函数返回所有匹配 + ambiguous 标记
    - 本函数颁发 window_token（canonical_hwnd + pid + process_create_time），供后续操作验证

    automation_id 通过 UIA 查询（如 uiautomation 可用）；若 uiautomation 不可用则忽略此条件。

    Returns:
        {
            "success": bool,
            "status": "found" | "ambiguous" | "not_found",
            "matches": list[dict],  # 每个 dict 含 hwnd/title/process_name/pid/canonical_window/window_token
            "message": str,
        }
    """
    windows = _enum_windows()
    matches = []
    for w in windows:
        if title and title.lower() not in (w.get("title") or "").lower():
            continue
        if process_name and process_name.lower() not in (w.get("process_name") or "").lower():
            continue
        if pid and w.get("pid") != pid:
            continue
        if automation_id:
            # UIA 查询窗口的 AutomationId（仅顶层窗口）
            if not _match_automation_id(w.get("hwnd", 0), automation_id):
                continue
        matches.append(w)

    if not matches:
        return {
            "success": False,
            "status": WINDOW_NOT_FOUND,
            "matches": [],
            "message": "未找到匹配窗口",
        }

    # 为每个匹配窗口附加 canonical_window + window_token
    for m in matches:
        hwnd = m.get("hwnd", 0)
        canonical = resolve_canonical_window(hwnd)
        # 简化 canonical（去掉 family_hwnds 和 input_hwnd，避免响应过大）
        m["canonical_window"] = {
            "canonical_hwnd": canonical["canonical_hwnd"],
            "canonical_title": canonical["canonical_title"],
            "canonical_pid": canonical["canonical_pid"],
            "canonical_process_name": canonical["canonical_process_name"],
            "is_uwp_host": canonical["is_uwp_host"],
        }
        m["window_token"] = _build_window_token(canonical)

    if len(matches) > 1:
        return {
            "success": True,
            "status": WINDOW_AMBIGUOUS,
            "matches": matches,
            "message": f"找到 {len(matches)} 个匹配窗口（ambiguous），请用更精确条件或选择特定 hwnd",
        }
    return {
        "success": True,
        "status": "found",
        "matches": matches,
        "message": f"找到窗口: {matches[0].get('title', '')}",
    }


def _build_window_token(canonical: dict) -> dict:
    """构建窗口 token：canonical_hwnd + pid + 进程创建时间（用于检测进程重启）。

    进程重启后 pid 可能被 OS 复用，但创建时间必然不同——通过 create_time 检测 token 失效。
    """
    pid = canonical.get("canonical_pid", 0)
    create_time = None
    if pid:
        try:
            import psutil
            create_time = int(psutil.Process(pid).create_time())
        except Exception:
            pass
    return {
        "canonical_hwnd": canonical.get("canonical_hwnd", 0),
        "pid": pid,
        "process_create_time": create_time,
        "process_name": canonical.get("canonical_process_name", ""),
        "title": canonical.get("canonical_title", ""),
    }


def verify_window_token(token: dict) -> tuple[bool, str]:
    """验证 window_token 是否仍有效（hwnd 存在 + pid 一致 + 进程未重启）。

    Returns:
        (valid, reason)
        valid=True 时 token 仍可用
        valid=False 时返回 WINDOW_TOKEN_INVALID 原因，agent 必须重新 resolve_window
    """
    if not token or not token.get("canonical_hwnd"):
        return False, "token 缺少 canonical_hwnd"
    canonical_hwnd = token["canonical_hwnd"]
    expected_pid = token.get("pid", 0)
    expected_create_time = token.get("process_create_time")

    if sys.platform != "win32":
        return True, ""  # 非 Windows 不校验

    try:
        import win32gui
        import win32process
    except ImportError:
        return True, ""  # pywin32 不可用不校验

    # 1. hwnd 是否仍存在
    try:
        if not win32gui.IsWindow(canonical_hwnd):
            return False, "hwnd 已销毁——窗口已关闭"
    except Exception as e:
        return False, f"hwnd 检查失败: {e}"

    # 2. hwnd 的 pid 是否仍为 expected_pid
    try:
        _, cur_pid = win32process.GetWindowThreadProcessId(canonical_hwnd)
        if expected_pid and cur_pid != expected_pid:
            return False, f"pid 不匹配（token={expected_pid}, 当前={cur_pid}）——hwnd 被复用"
    except Exception as e:
        return False, f"pid 检查失败: {e}"

    # 3. 进程是否重启（创建时间变化）
    if expected_create_time and expected_pid:
        try:
            import psutil
            cur_create_time = int(psutil.Process(expected_pid).create_time())
            if cur_create_time != expected_create_time:
                return False, (
                    f"进程已重启（token create_time={expected_create_time}, "
                    f"当前={cur_create_time}）——必须重新 window_resolve"
                )
        except psutil.NoSuchProcess:
            return False, "进程已退出"
        except Exception:
            pass  # psutil 异常时不阻断（已有 hwnd + pid 校验作为兜底）

    return True, ""


def _match_automation_id(hwnd: int, expected_automation_id: str) -> bool:
    """检查窗口的 UIA AutomationId 是否匹配（仅顶层窗口，uiautomation 不可用时返回 False）。"""
    if not hwnd or not expected_automation_id:
        return False
    try:
        import uiautomation as ua
        from uiautomation.uiautomation import Logger
        Logger.SetLogFile('')   # 兜底：禁用 @AutomationLog.txt（与 uia_available() 同源）
        ctrl = ua.ControlFromHandle(hwnd)
        if ctrl is None:
            return False
        return (ctrl.AutomationId or "") == expected_automation_id
    except ImportError:
        return False
    except Exception:
        return False


# ========== 窗口操作（最小化/恢复/置前/关闭） ==========

def _window_post_state(hwnd: int) -> dict:
    """收集窗口操作后的状态（用于响应返回，便于 agent 验证操作生效）。"""
    state = {
        "hwnd": hwnd,
        "exists": False,
        "is_visible": False,
        "is_minimized": False,
        "is_foreground": False,
        "title": "",
        "bbox": None,
    }
    if sys.platform != "win32" or not hwnd:
        return state
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return state
        state["exists"] = True
        state["is_visible"] = bool(win32gui.IsWindowVisible(hwnd))
        state["is_minimized"] = bool(win32gui.IsIconic(hwnd))
        state["title"] = win32gui.GetWindowText(hwnd) or ""
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            state["bbox"] = {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)}
        except Exception:
            pass
        try:
            state["is_foreground"] = (win32gui.GetForegroundWindow() == hwnd)
        except Exception:
            pass
    except Exception:
        pass
    return state


def minimize_window(hwnd: int) -> dict:
    """最小化窗口。返回操作后状态。"""
    if sys.platform == "win32":
        try:
            import win32con
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return {"success": False, "message": "hwnd 不存在", "post_state": _window_post_state(hwnd)}
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            return {"success": True, "message": "已最小化", "post_state": _window_post_state(hwnd)}
        except Exception as e:
            return {"success": False, "message": f"最小化失败: {e}", "post_state": _window_post_state(hwnd)}
    return {"success": False, "message": "非 Windows 平台", "post_state": _window_post_state(hwnd)}


def restore_window(hwnd: int) -> dict:
    """恢复窗口（从最小化还原）。返回操作后状态。"""
    if sys.platform == "win32":
        try:
            import win32con
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return {"success": False, "message": "hwnd 不存在", "post_state": _window_post_state(hwnd)}
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            return {"success": True, "message": "已恢复", "post_state": _window_post_state(hwnd)}
        except Exception as e:
            return {"success": False, "message": f"恢复失败: {e}", "post_state": _window_post_state(hwnd)}
    return {"success": False, "message": "非 Windows 平台", "post_state": _window_post_state(hwnd)}


def raise_window(hwnd: int) -> dict:
    """置前窗口（激活到前台）。返回操作后状态。"""
    if sys.platform == "win32":
        try:
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return {"success": False, "message": "hwnd 不存在", "post_state": _window_post_state(hwnd)}
            ok = _force_focus_window(hwnd)
            if ok:
                return {"success": True, "message": "已置前", "post_state": _window_post_state(hwnd)}
            return {"success": False, "message": "置前失败（UIPI 阻止或窗口不可激活）", "post_state": _window_post_state(hwnd)}
        except Exception as e:
            return {"success": False, "message": f"置前失败: {e}", "post_state": _window_post_state(hwnd)}
    return {"success": False, "message": "非 Windows 平台", "post_state": _window_post_state(hwnd)}


def close_window(hwnd: int, force: bool = False) -> dict:
    """关闭窗口。

    第三轮评估后续修复：之前窗口仍存在时返回 success=True 仅靠 message 警告，是谎报。
    现在严格按 post_state.exists 判定 success。

    - force=False（默认，优雅关闭）：
        1) 发 WM_CLOSE
        2) 轮询 ~1.5s 等待窗口消失
        3) 若仍存在，检测是否弹出"是否保存"模态对话框（Notepad/记事本/写字板等通用模式）
           - 检测到 → 通过 UIA 找"不保存(N)"按钮 invoke；UIA 不可用则 SendMessage WM_COMMAND ID=7 (IDNO)
           - 未检测到 → 返回 modal_blocking 让 agent 决策
        4) 再等待 ~0.5s 复查
        5) success=True ⟺ post_state.exists=False

    - force=True（强制杀进程，可能丢失未保存数据）：
        通过 GetWindowThreadProcessId 拿 pid，用 OpenProcess(PROCESS_TERMINATE) + TerminateProcess
        直接终止进程（不再走 DestroyWindow，跨进程本就失败）。force 永远走杀进程路径，
        即使窗口当前没有模态对话框也直接杀。
    """
    if sys.platform != "win32":
        return {"success": False, "message": "非 Windows 平台",
                "status": "failed", "post_state": _window_post_state(hwnd)}

    try:
        import win32con
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return {"success": False, "message": "hwnd 不存在",
                    "status": "failed", "post_state": _window_post_state(hwnd)}

        # ===== force=True：直接杀进程 =====
        if force:
            return _close_window_force_kill(hwnd)

        # ===== force=False：优雅关闭 + 模态对话框处理 =====
        # 1. 发 WM_CLOSE
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

        # 2. 轮询 ~1.5s 等窗口消失（每 100ms 检查一次）
        post = _window_post_state(hwnd)
        for _ in range(15):
            if not post["exists"]:
                return {"success": True, "status": "executed",
                        "message": "已关闭", "post_state": post}
            time.sleep(0.1)
            post = _window_post_state(hwnd)

        if not post["exists"]:
            return {"success": True, "status": "executed",
                    "message": "已关闭", "post_state": post}

        # 3. 窗口仍存在 → 等待模态对话框弹出并尝试处理
        # 第三轮评估后续修复：Win11 Notepad 等应用弹模态可能有延迟，
        # 轮询 ~2s 检测模态（每 200ms 重试一次）
        modal_result = None
        for _attempt in range(10):
            time.sleep(0.2)
            modal_result = _try_dismiss_save_modal(hwnd)
            if modal_result["dismissed"]:
                # 模态已处理（选了"不保存"），再轮询 ~1s 复查窗口是否消失
                for _ in range(10):
                    time.sleep(0.1)
                    post = _window_post_state(hwnd)
                    if not post["exists"]:
                        return {"success": True, "status": "executed",
                                "message": f"已关闭（已自动选择'不保存'：{modal_result['message']}）",
                                "post_state": post}
                # 模态处理后窗口仍存在 → 失败
                return {"success": False, "status": "modal_blocking",
                        "message": f"已处理模态对话框但窗口仍存在：{modal_result['message']}",
                        "post_state": post,
                        "modal_info": modal_result}
            # 模态未检测到 → 继续等下一次轮询
            post = _window_post_state(hwnd)
            if not post["exists"]:
                # 窗口在轮询中消失了（可能模态自动关闭）
                return {"success": True, "status": "executed",
                        "message": "已关闭", "post_state": post}

        # 4. 2s 内未检测到可处理的模态 → modal_blocking 让 agent 决策
        return {"success": False, "status": "modal_blocking",
                "message": (f"WM_CLOSE 已发送但窗口仍存在，~2s 内未识别到可自动处理的"
                            f"模态对话框。可重试或改用 force=true 直接杀进程（会丢失未保存数据）。"
                            f"modal_info={modal_result}"),
                "post_state": post,
                "modal_info": modal_result}

    except Exception as e:
        return {"success": False, "status": "failed",
                "message": f"关闭失败: {e}", "post_state": _window_post_state(hwnd)}


def _close_window_force_kill(hwnd: int) -> dict:
    """force=True 路径：直接 TerminateProcess 杀进程。

    明确语义：force 就是杀进程，不是 DestroyWindow（跨进程本就失败），
    也不是发 WM_CLOSE（可能弹模态）。会丢失未保存数据，由调用方负责。
    """
    try:
        import win32api
        import win32gui
        import win32process

        if not win32gui.IsWindow(hwnd):
            # 窗口已不存在，视作已关闭
            return {"success": True, "status": "executed",
                    "message": "窗口已不存在", "post_state": _window_post_state(hwnd)}

        # 拿 pid
        _, pid = win32process.GetWindowThreadProcessId(hwnd)

        # OpenProcess + TerminateProcess
        PROCESS_TERMINATE = 0x0001
        try:
            h_process = win32api.OpenProcess(PROCESS_TERMINATE, False, pid)
        except Exception as e:
            return {"success": False, "status": "failed",
                    "message": f"OpenProcess 失败（pid={pid}，可能权限不足）: {e}",
                    "post_state": _window_post_state(hwnd)}

        try:
            win32api.TerminateProcess(h_process, 1)
        finally:
            win32api.CloseHandle(h_process)

        # 等待进程退出 + 窗口销毁（轮询 ~1s）
        post = _window_post_state(hwnd)
        for _ in range(10):
            if not post["exists"]:
                return {"success": True, "status": "executed",
                        "message": f"已强制杀进程（pid={pid}）",
                        "post_state": post}
            time.sleep(0.1)
            post = _window_post_state(hwnd)

        # 进程仍在
        return {"success": False, "status": "failed",
                "message": f"TerminateProcess 已调用但进程仍存在（pid={pid}）",
                "post_state": post}

    except Exception as e:
        return {"success": False, "status": "failed",
                "message": f"force kill 失败: {e}",
                "post_state": _window_post_state(hwnd)}


def _try_dismiss_save_modal(parent_hwnd: int) -> dict:
    """检测并尝试处理"是否保存"模态对话框（Notepad/记事本/写字板等通用模式）。

    第三轮评估后续修复：实测发现 Win11 Notepad 的"是否保存"对话框虽然是 #32770 class，
    但它是 notepad 主窗口的 **owned window** 而非 child window，跨进程 EnumChildWindows
    枚举不到（只返回 Edit 和 msctls_statusbar32）。改为以下策略：

    1) 优先用 UIA 在 parent_hwnd 的子树里 BFS 找 Button，name 匹配"不保存"/"Don't Save"
       → invoke（UIA 能跨进程访问 owned window）
    2) UIA 不可用 / 未找到按钮 → 用 EnumWindows 找同 pid 的 #32770 对话框，
       PostMessage WM_COMMAND IDNO (wParam=7)（Win32 标准 IDNO=7）

    Returns:
        {
            "dismissed": bool,
            "message": str,
            "dialog_count": int,
            "dialogs": [{"hwnd": int, "title": str, "action": str}, ...]
        }
    """
    result = {"dismissed": False, "message": "", "dialog_count": 0, "dialogs": []}

    # ===== 方案 A：UIA 在 parent_hwnd 子树找"不保存"按钮（最可靠） =====
    uia_result = _uia_click_dont_save_button(parent_hwnd)
    if uia_result["clicked"]:
        result["dismissed"] = True
        result["dialog_count"] = 1
        result["dialogs"].append({
            "hwnd": parent_hwnd,
            "title": "(通过 UIA 子树查找)",
            "action": f"UIA invoke '不保存' button: {uia_result['message']}",
        })
        result["message"] = (f"已通过 UIA 在主窗口子树找到并点击'不保存'按钮"
                             f"（button_name={uia_result.get('button_name')!r}）")
        return result

    # ===== 方案 B：EnumWindows 找同 pid 的 #32770 对话框，回退 WM_COMMAND IDNO =====
    try:
        import win32gui
        import win32process

        # 拿 parent 的 pid
        try:
            _, parent_pid = win32process.GetWindowThreadProcessId(parent_hwnd)
        except Exception:
            parent_pid = 0

        dialogs: list[tuple[int, str]] = []

        def _enum_proc(hwnd_child, _):
            try:
                if not win32gui.IsWindow(hwnd_child):
                    return True
                class_name = win32gui.GetClassName(hwnd_child)
                if class_name != "#32770":
                    return True
                # 检查是否是 parent 的子对话框（owned window）：
                # - 同 pid，且
                # - GetWindow(GW_OWNER) == parent_hwnd，或 parent_hwnd 是它的祖先
                try:
                    _, child_pid = win32process.GetWindowThreadProcessId(hwnd_child)
                except Exception:
                    child_pid = 0
                if parent_pid and child_pid != parent_pid:
                    return True

                # 检查 owner 关系
                try:
                    owner = win32gui.GetWindow(hwnd_child, win32con.GW_OWNER)
                except Exception:
                    owner = 0
                if owner != parent_hwnd:
                    # 也接受 owner=0 但同 pid 的 #32770（部分应用对话框无 owner）
                    # 但要排除其他应用的同 class 对话框，所以严格匹配 pid
                    if owner != 0:
                        return True  # 有其他 owner，跳过

                title = win32gui.GetWindowText(hwnd_child) or ""
                dialogs.append((hwnd_child, title))
            except Exception:
                pass
            return True

        import win32con
        win32gui.EnumWindows(_enum_proc, None)

        result["dialog_count"] = len(dialogs)
        if not dialogs:
            result["message"] = (
                f"UIA 未找到'不保存'按钮（{uia_result['message']}），"
                f"EnumWindows 也未找到同 pid 的 #32770 对话框"
            )
            return result

        # 对每个 dialog 尝试 WM_COMMAND IDNO
        for dlg_hwnd, dlg_title in dialogs:
            dlg_info = {"hwnd": dlg_hwnd, "title": dlg_title, "action": ""}
            try:
                win32gui.PostMessage(dlg_hwnd, win32con.WM_COMMAND, 7, 0)
                dlg_info["action"] = "PostMessage WM_COMMAND IDNO (wParam=7)"
                result["dialogs"].append(dlg_info)
                result["dismissed"] = True
                result["message"] = (
                    f"已发送 WM_COMMAND IDNO 到对话框（dialog={dlg_title!r}，"
                    f"UIA 路径失败原因：{uia_result['message']}）"
                )
                return result
            except Exception as e:
                dlg_info["action"] = f"WM_COMMAND IDNO 失败: {e}"
                result["dialogs"].append(dlg_info)

        result["message"] = (
            f"UIA 未找到'不保存'按钮（{uia_result['message']}），"
            f"检测到 {len(dialogs)} 个对话框但 WM_COMMAND 都失败: "
            + "; ".join(d["action"] for d in result["dialogs"])
        )
        return result

    except Exception as e:
        result["message"] = f"检测模态对话框异常: {e}（UIA 路径: {uia_result['message']}）"
        return result


def _uia_click_dont_save_button(dialog_hwnd: int) -> dict:
    """用 UIA 在对话框内找"不保存"按钮并 invoke。

    Returns:
        {"clicked": bool, "message": str, "button_name": str | None}
    """
    try:
        import uiautomation as ua
        from uiautomation.uiautomation import Logger
        Logger.SetLogFile('')   # 兜底：禁用 @AutomationLog.txt（与 uia_available() 同源）

        ctrl = ua.ControlFromHandle(dialog_hwnd)
        if ctrl is None:
            return {"clicked": False, "message": "ControlFromHandle 返回 None",
                    "button_name": None}

        # BFS 找 Button，name 匹配"不保存" / "Don't Save"
        # 匹配规则：name 等于或以这些前缀开头（兼容 "不保存(N)"、"Don't Save (N)" 等）
        target_names = [
            "不保存", "不保存(N)", "不保存(&N)",
            "Don't Save", "Don't Save (N)", "Don't Save (&N)",
            "否", "否(N)", "否(&N)",  # 部分应用用"否"
        ]
        # 也接受以"不保存"或"Don't Save"开头的按钮
        def _match(name: str) -> bool:
            if not name:
                return False
            name_stripped = name.strip()
            if name_stripped in target_names:
                return True
            return (name_stripped.startswith("不保存") or
                    name_stripped.lower().startswith("don't save"))

        # 限制深度 5，避免遍历过大
        from collections import deque
        queue = deque([(ctrl, 0)])
        seen = set()
        while queue:
            node, depth = queue.popleft()
            if depth > 5:
                continue
            try:
                handle = node.NativeWindowHandle
                if handle in seen:
                    continue
                seen.add(handle)
            except Exception:
                pass

            try:
                name = node.Name or ""
                ctrl_type = node.ControlTypeName or ""
                if ctrl_type == "ButtonControl" and _match(name):
                    # 找到目标按钮 → invoke
                    try:
                        invoke_pat = node.GetInvokePattern()
                        if invoke_pat is not None:
                            invoke_pat.Invoke()
                            return {"clicked": True,
                                    "message": "Invoke 成功",
                                    "button_name": name}
                        # 没有 InvokePattern，尝试 Click
                        node.Click()
                        return {"clicked": True,
                                "message": "InvokePattern 不可用，已用 Click()",
                                "button_name": name}
                    except Exception as e:
                        return {"clicked": False,
                                "message": f"找到按钮 {name!r} 但 invoke 失败: {e}",
                                "button_name": name}
            except Exception:
                pass

            # 遍历子元素
            try:
                children = node.GetChildren() if hasattr(node, 'GetChildren') else []
                # GetChildren 可能返回 list（uiautomation 真实库）或带 .Count 的容器
                if hasattr(children, 'Count'):
                    count = children.Count
                elif isinstance(children, list):
                    count = len(children)
                else:
                    count = 0
                for i in range(count):
                    queue.append((children[i], depth + 1))
            except Exception:
                pass

        return {"clicked": False,
                "message": "未找到匹配的'不保存'/'Don't Save'/'否'按钮（深度 5 内）",
                "button_name": None}

    except ImportError:
        return {"clicked": False, "message": "uiautomation 库未安装",
                "button_name": None}
    except Exception as e:
        return {"clicked": False, "message": f"UIA 异常: {e}",
                "button_name": None}
