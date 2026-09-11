"""UI Automation 语义层（评估文档 P0-5）。

提供 Windows UIA 树快照与语义动作接口，让 agent 像 Browser Use 一样从结构化
accessibility tree 中找元素、直接 invoke/toggle/set_value，而不是依赖坐标点击。

评估文档（docs/evaluations/2026-07-20-computer-use-vs-localagent.md）第 9 节：
- 新增 `screen_accessibility_snapshot`：role/control_type、name、value、enabled、
  checked、selected、bounds、automation_id、element_id
- 新增 `screen_semantic_action`：invoke、select、toggle、set_value、
  expand/collapse、scroll
- element id 绑定 window token + snapshot version，过期返回 STALE_ELEMENT
- UIA 不可用时回退 OCR/视觉，明确 `fallback_reason`

第二轮评估（docs/evaluations/2026-07-20-computer-use-vs-localagent-rerun2.md）P0：
- 所有 UIA 入口线程显式 CoInitialize（避免 WinError -2147221008 "尚未调用 CoInitialize"）
- 失败时返回可操作 fallback（UIA_NOT_AVAILABLE），不返回空 elements

依赖：
- uiautomation>=2.0（comtypes 封装 Windows UIA API）
- pywin32（win32gui 用于窗口验证）

外部模块通过 `from server.screen.uia import take_uia_snapshot, invoke_element_by_id, ...`
访问。
"""

import logging
import sys
import threading
import time
import uuid
from collections import deque

logger = logging.getLogger("localagent.screen.uia")

# 非 Windows 平台直接禁用
_PLATFORM_WIN = sys.platform == "win32"


# ========== COM 初始化（线程级，第二轮评估 P0-1） ==========
# FastAPI 线程池模式下每个请求可能落在不同线程，UIA 依赖 COM 必须在线程入口初始化。
# pythoncom.CoInitialize 等价于 CoInitializeEx(COINIT_APARTMENTTHREADED)。
# 重复调用幂等（COM 内部计数），但 CoUninitialize 必须配对——此处只 Init 不 Uninit，
# 让线程结束时由 OS 清理（避免误Uninit影响后续同线程调用）。

_com_initialized_threads: set = set()
_com_init_lock = threading.Lock()


def _ensure_com_initialized() -> tuple[bool, str]:
    """确保当前线程已调用 CoInitialize（线程级幂等）。

    Returns:
        (ok, reason) — ok=True 时 COM 已就绪；ok=False 时 reason 描述失败原因
    """
    if not _PLATFORM_WIN:
        return False, "non-windows platform"
    tid = threading.get_ident()
    with _com_init_lock:
        if tid in _com_initialized_threads:
            return True, ""
    try:
        # 优先用 pythoncom（pywin32 自带，更稳定）
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            # 回退到 ctypes 直接调 ole32
            import ctypes
            # COINIT_APARTMENTTHREADED = 0x2
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            # S_OK (0) / S_FALSE (1) 都算成功（S_FALSE 表示已初始化）
            if hr not in (0, 1):
                return False, f"CoInitializeEx 返回 0x{hr & 0xFFFFFFFF:08X}"
    except Exception as e:
        return False, f"CoInitialize 异常: {e}"
    with _com_init_lock:
        _com_initialized_threads.add(tid)
    return True, ""


# ========== 状态码常量 ==========

STALE_ELEMENT = "STALE_ELEMENT"
"""UIA element_id 失效——snapshot 过期/element 不存在/窗口已重建。agent 必须重新 take_uia_snapshot。"""

UIA_NOT_AVAILABLE = "UIA_NOT_AVAILABLE"
"""UIA 不可用（uiautomation 库未安装或 COM 初始化失败）。需回退 OCR/视觉定位。"""


# ========== Snapshot 缓存（element_id 解析依赖） ==========

_SNAPSHOT_TTL_SECONDS = 300  # 5 分钟（与 capture_screen 的 snapshot 一致）

# snapshot_id → {elements: list[dict], canonical_hwnd: int, captured_at: float}
_uia_snapshots: dict[str, dict] = {}
_uia_lock = threading.Lock()


def _gc_expired_snapshots() -> None:
    """懒清理过期 UIA snapshot（避免内存泄漏）。"""
    now = time.time()
    with _uia_lock:
        expired = [k for k, v in _uia_snapshots.items()
                   if now - v.get("captured_at", 0) > _SNAPSHOT_TTL_SECONDS]
        for k in expired:
            _uia_snapshots.pop(k, None)


# ========== UIA 可用性检查 ==========

_uia_available_cache: bool | None = None


def uia_available() -> bool:
    """检查 UIA 是否可用（uiautomation 库 + COM 初始化）。

    结果缓存（避免每次调用都 try import）。
    """
    global _uia_available_cache
    if _uia_available_cache is not None:
        return _uia_available_cache
    if not _PLATFORM_WIN:
        _uia_available_cache = False
        return False
    try:
        import uiautomation  # noqa: F401
        from uiautomation.uiautomation import Logger
        Logger.SetLogFile('')   # 禁用 @AutomationLog.txt 文件写入（错误仍走 stdout/项目 logging）
        _uia_available_cache = True
    except Exception as e:
        logger.info(f"uiautomation 库不可用，UIA 语义层降级: {e}")
        _uia_available_cache = False
    return _uia_available_cache


def reset_uia_available_cache() -> None:
    """测试用：重置可用性缓存（mock 后强制重新检查）。"""
    global _uia_available_cache
    _uia_available_cache = None


# ========== UIA Pattern 统一获取 ==========

def get_pattern(ctrl, pattern_name: str):
    """按名称取控件的 UIA pattern，不支持/异常时统一返回 None。

    uiautomation 2.0.29 的 Control 类没有 GetValuePattern / GetInvokePattern
    等 GetXxxPattern 便捷方法（调用即 AttributeError），只有通用
    GetPattern(PatternId)；且目标 pattern 不受支持时 GetPattern 还可能抛
    COMError 或返回 None。此处把三种失败形态统一收敛为 None，
    调用方按"元素不支持该 pattern"处理。
    """
    try:
        import uiautomation as ua
        pattern_id = getattr(ua.PatternId, pattern_name)
        return ctrl.GetPattern(pattern_id)
    except Exception:
        return None


# ========== UIA 元素遍历（私有） ==========

def _control_to_element_dict(ctrl, depth: int, parent_index: int | None,
                              element_index: int, snapshot_id: str,
                              canonical_hwnd: int) -> dict | None:
    """把 uiautomation.Control 转为响应 element dict。

    返回 None 表示该元素应跳过（无 role/name/value 等有效信息且 interesting_only=True 时）。
    """
    try:
        import uiautomation as ua  # noqa: F401  保留 import 触发库加载（control_type 映射依赖 _init_control_type_map）
    except Exception:
        return None

    try:
        # ControlType：uiautomation 返回 int，需映射到字符串名
        control_type_int = ctrl.ControlType
        role = _control_type_name(control_type_int)

        name = ctrl.Name or ""
        automation_id = ctrl.AutomationId or ""

        # Value
        value = None
        editable = False
        try:
            vp = get_pattern(ctrl, "ValuePattern")
            if vp:
                value = vp.Value
                # ZCode 风格 editable：ValuePattern 存在且非只读（只读 Text/Document 也有 Value）
                try:
                    editable = not bool(vp.IsReadOnly)
                except Exception:
                    editable = False
        except Exception:
            pass

        # Enabled
        enabled = True
        try:
            enabled = bool(ctrl.IsEnabled)
        except Exception:
            pass

        # ToggleState: 0=Off 1=On 2=Indeterminate
        checked = None
        toggleable = False
        try:
            tp = get_pattern(ctrl, "TogglePattern")
            if tp:
                ts = tp.ToggleState
                checked = (ts == 1)
                toggleable = True
        except Exception:
            pass

        # SelectionItem
        selected = None
        selectable = False
        try:
            sp = get_pattern(ctrl, "SelectionItemPattern")
            if sp:
                selected = bool(sp.IsSelected)
                selectable = True
        except Exception:
            pass

        # ZCode 风格能力标志：补探测 Invoke / ExpandCollapse（各 1 次轻量 GetPattern）
        pressable = False
        try:
            pressable = get_pattern(ctrl, "InvokePattern") is not None
        except Exception:
            pass
        expandable = False
        try:
            expandable = get_pattern(ctrl, "ExpandCollapsePattern") is not None
        except Exception:
            pass
        focused = False
        try:
            focused = bool(getattr(ctrl, "HasKeyboardFocus", False)) and enabled
        except Exception:
            pass

        # BoundingRectangle: (left, top, right, bottom) in physical pixels
        bounds = [0, 0, 0, 0]
        try:
            rect = ctrl.BoundingRectangle
            if rect:
                bounds = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
        except Exception:
            pass

        # RuntimeId：用于在源 UIA tree 中重新定位（窗口重建后失效）
        runtime_id: tuple = ()
        try:
            rid = ctrl.GetRuntimeId()
            if rid:
                runtime_id = tuple(int(x) for x in rid)
        except Exception:
            pass

        element_id = f"{canonical_hwnd}:{snapshot_id[:8]}:{element_index}"

        # 能力标志（ZCode computer-use 风格）：空格连接的短串省 token；
        # actions 对齐 execute_semantic_action 的动作枚举，让 agent 一次观察就知道能做什么
        enabled_caps = enabled  # 禁用元素不宣称能力
        flag_parts: list[str] = []
        if pressable and enabled_caps:
            flag_parts.append("pressable")
        if editable and enabled_caps:
            flag_parts.append("editable")
        if toggleable and enabled_caps:
            flag_parts.append("toggleable")
        if selectable and enabled_caps:
            flag_parts.append("selectable")
        if expandable and enabled_caps:
            flag_parts.append("expandable")
        if focused:
            flag_parts.append("focused")

        actions: list[str] = []
        if "pressable" in flag_parts:
            actions.append("invoke")
        if "toggleable" in flag_parts:
            actions.append("toggle")
        if "selectable" in flag_parts:
            actions.append("select")
        if "editable" in flag_parts:
            actions.append("set_value")
        if "expandable" in flag_parts:
            actions.extend(["expand", "collapse"])

        return {
            "element_id": element_id,
            "role": role,
            "name": name,
            "value": value,
            "enabled": enabled,
            "checked": checked,
            "selected": selected,
            "bounds": bounds,
            "automation_id": automation_id or None,
            "element_index": element_index,
            "depth": depth,
            "parent_index": parent_index,
            "children_indices": [],  # 稍后填充
            "flags": " ".join(flag_parts),
            "actions": actions,
            "runtime_id": runtime_id,
        }
    except Exception as e:
        logger.debug(f"convert control to dict failed: {e}")
        return None


# ControlType int → name 映射（uiautomation 库的常量值）
# 来源：uiautomation.py 中的 ControlType 定义
_CONTROL_TYPE_MAP: dict[int, str] = {}

# 第二轮评估 P1-4：硬编码 UIA ControlType 稳定映射（兜底，避免 dir() 遍历依赖库版本）
# 来源：Microsoft UIAutomation Core ControlType Idl（值固定，跨版本稳定）
_CONTROL_TYPE_HARDCODED: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
    50044: "ComboBoxItem",  # Win10+
}


def _init_control_type_map() -> None:
    """初始化 ControlType int → name 映射（懒加载，避免 import 时崩溃）。

    优先用硬编码表（稳定），再用 uiautomation 库常量补充（覆盖新增类型）。
    """
    if _CONTROL_TYPE_MAP:
        return
    # 先填硬编码表（兜底，确保跨版本稳定）
    _CONTROL_TYPE_MAP.update(_CONTROL_TYPE_HARDCODED)
    try:
        import uiautomation as ua
        # 遍历 ua 模块中以 "ButtonControl" / "EditControl" 等结尾的常量
        # 补充硬编码表中没有的新类型（如未来 uiautomation 新增）
        for name in dir(ua):
            if name.endswith("Control") and not name.startswith("_"):
                val = getattr(ua, name, None)
                if isinstance(val, int) and val not in _CONTROL_TYPE_MAP:
                    _CONTROL_TYPE_MAP[val] = name[:-len("Control")]  # "ButtonControl" → "Button"
    except Exception:
        pass


def _control_type_name(ctrl_type_int: int) -> str:
    """ControlType int → 字符串名（如 50000 → "Button"）。未知返回 "Unknown_<int>"。

    第二轮评估 P1-4：优先用硬编码映射，避免 Unknown_500xx 这种不友好角色。
    """
    _init_control_type_map()
    return _CONTROL_TYPE_MAP.get(ctrl_type_int, f"Unknown_{ctrl_type_int}")


def _walk_uia_tree(root_ctrl, max_depth: int, interesting_only: bool,
                    max_elements: int, snapshot_id: str,
                    canonical_hwnd: int) -> list[dict]:
    """深度优先遍历 UIA 树，返回扁平化的 element 列表。"""
    elements: list[dict] = []
    # 用 deque 模拟 BFS 队列：(ctrl, depth, parent_index)
    # deque.popleft/appendleft 都是 O(1)，避免 list.pop(0)/insert(0,...) 的 O(n) 操作
    stack: deque[tuple] = deque([(root_ctrl, 0, None)])
    next_parent_indices: dict[int, list[int]] = {}  # 父 index → 子 index 列表

    while stack and len(elements) < max_elements:
        ctrl, depth, parent_idx = stack.popleft()  # BFS（广度优先，更符合 UI 结构）
        if depth > max_depth:
            continue

        elem_dict = _control_to_element_dict(
            ctrl, depth, parent_idx, len(elements), snapshot_id, canonical_hwnd
        )
        if elem_dict is None:
            continue

        # interesting_only 过滤：role="Unknown" 且 name="" 且 value=None 跳过
        if interesting_only:
            role = elem_dict.get("role", "")
            name = elem_dict.get("name", "")
            value = elem_dict.get("value")
            automation_id = elem_dict.get("automation_id")
            if role.startswith("Unknown") and not name and value is None and not automation_id:
                continue

        current_index = len(elements)
        elements.append(elem_dict)

        # 记录父子关系
        if parent_idx is not None:
            next_parent_indices.setdefault(parent_idx, []).append(current_index)

        # 子元素入队
        if depth < max_depth:
            try:
                children = ctrl.GetChildren() or []
                # 反转入队顺序（BFS popleft 时保持原顺序）
                for child in reversed(children):
                    stack.appendleft((child, depth + 1, current_index))
            except Exception as e:
                logger.debug(f"GetChildren failed at depth {depth}: {e}")

    # 填充 children_indices
    for parent_idx, child_indices in next_parent_indices.items():
        if 0 <= parent_idx < len(elements):
            elements[parent_idx]["children_indices"] = child_indices

    return elements


# ========== Snapshot 主入口 ==========

def take_uia_snapshot(hwnd: int, max_depth: int = 8,
                       interesting_only: bool = True,
                       max_elements: int = 500) -> dict:
    """对指定窗口取 UIA accessibility snapshot。

    Args:
        hwnd: 目标窗口句柄（必须）
        max_depth: 遍历最大深度（默认 8）
        interesting_only: True=只返回有 role/name/value 的元素（默认 True）
        max_elements: 最多返回元素数（防 UIA 树过大撑爆上下文，默认 500）

    Returns:
        {
            "success": bool,
            "snapshot_id": str,
            "elements": list[dict],
            "element_count": int,
            "canonical_hwnd": int,
            "fallback_reason": str | None,  # UIA_NOT_AVAILABLE 等错误码
            "elapsed_ms": int,
            "message": str,
        }
    """
    t0 = time.perf_counter()

    # 1. UIA 可用性检查
    if not uia_available():
        return {
            "success": False,
            "snapshot_id": "",
            "elements": [],
            "element_count": 0,
            "canonical_hwnd": hwnd,
            "fallback_reason": UIA_NOT_AVAILABLE,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": "uiautomation 库不可用，UIA 语义层降级。请回退 OCR/视觉定位",
        }

    # 1.5 第二轮评估 P0-1：线程级 CoInitialize（避免 WinError -2147221008）
    com_ok, com_reason = _ensure_com_initialized()
    if not com_ok:
        return {
            "success": False,
            "snapshot_id": "",
            "elements": [],
            "element_count": 0,
            "canonical_hwnd": hwnd,
            "fallback_reason": UIA_NOT_AVAILABLE,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"COM 初始化失败: {com_reason}。请回退 OCR/视觉定位",
        }

    # 2. 解析 canonical window token（UWP host/child 同族统一）
    from server.screen.focus import resolve_canonical_window
    canonical = resolve_canonical_window(hwnd)
    canonical_hwnd = canonical.get("canonical_hwnd", hwnd)

    # 3. 从 hwnd 获取 UIA root control
    try:
        import uiautomation as ua
        # 再次确保 COM 已初始化（防御性，uiautomation 内部可能 ResetCOM）
        _ensure_com_initialized()
        root_ctrl = ua.ControlFromHandle(canonical_hwnd)
        if root_ctrl is None:
            # 尝试用原始 hwnd
            root_ctrl = ua.ControlFromHandle(hwnd)
        if root_ctrl is None:
            return {
                "success": False,
                "snapshot_id": "",
                "elements": [],
                "element_count": 0,
                "canonical_hwnd": canonical_hwnd,
                "fallback_reason": "uia_no_target",
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "message": f"UIA 无法从 hwnd={hwnd} 获取 Control（窗口可能已关闭或无 accessibility 实现）",
            }
    except Exception as e:
        return {
            "success": False,
            "snapshot_id": "",
            "elements": [],
            "element_count": 0,
            "canonical_hwnd": canonical_hwnd,
            "fallback_reason": "uia_snapshot_failed",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"UIA 初始化失败: {e}",
        }

    # 4. 遍历 UIA 树
    snapshot_id = uuid.uuid4().hex
    try:
        elements = _walk_uia_tree(
            root_ctrl, max_depth, interesting_only, max_elements,
            snapshot_id, canonical_hwnd,
        )
    except Exception as e:
        return {
            "success": False,
            "snapshot_id": "",
            "elements": [],
            "element_count": 0,
            "canonical_hwnd": canonical_hwnd,
            "fallback_reason": "uia_snapshot_failed",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"UIA 树遍历失败: {e}",
        }

    # 5. 缓存 snapshot（供后续 element_id 解析）
    _gc_expired_snapshots()
    with _uia_lock:
        _uia_snapshots[snapshot_id] = {
            "elements": elements,
            "canonical_hwnd": canonical_hwnd,
            "captured_at": time.time(),
        }

    return {
        "success": True,
        "snapshot_id": snapshot_id,
        "elements": elements,
        "element_count": len(elements),
        "canonical_hwnd": canonical_hwnd,
        "fallback_reason": None,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "message": f"UIA snapshot 完成：{len(elements)} 个元素，深度≤{max_depth}",
    }


# ========== element_id 解析与动作执行 ==========

def resolve_element(element_id: str, snapshot_id: str) -> tuple[dict | None, str]:
    """解析 element_id → (element_dict, error_reason)。

    校验：
    1. snapshot_id 存在且未过期
    2. element_id 在 snapshot 中
    3. element_id 的 canonical_hwnd 部分与 snapshot 一致（防伪造）

    Returns:
        (element, "") — 成功
        (None, "snapshot_not_found") — snapshot_id 不存在或过期
        (None, "element_not_found") — element_id 不在 snapshot 中
        (None, "hwnd_mismatch") — canonical_hwnd 不匹配
    """
    _gc_expired_snapshots()
    with _uia_lock:
        snap = _uia_snapshots.get(snapshot_id)
        if not snap:
            return None, "snapshot_not_found"
        elements = snap.get("elements", [])
        canonical_hwnd = snap.get("canonical_hwnd", 0)

    # element_id 格式：{canonical_hwnd}:{snapshot_id_short}:{element_index}
    try:
        parts = element_id.split(":")
        if len(parts) != 3:
            return None, "element_not_found"
        elem_hwnd = int(parts[0])
        elem_index = int(parts[2])
        if elem_hwnd != canonical_hwnd:
            return None, "hwnd_mismatch"
        if elem_index < 0 or elem_index >= len(elements):
            return None, "element_not_found"
        return elements[elem_index], ""
    except (ValueError, IndexError):
        return None, "element_not_found"


def _find_uia_control_by_runtime_id(canonical_hwnd: int, runtime_id: tuple):
    """通过 RuntimeId 在源 UIA tree 中重新定位 Control。

    RuntimeId 是 UIA 元素的稳定标识（窗口内不变，窗口重建后变化）。
    """
    if not uia_available() or not runtime_id:
        return None
    try:
        import uiautomation as ua
        root_ctrl = ua.ControlFromHandle(canonical_hwnd)
        if root_ctrl is None:
            return None
        # BFS 遍历查找匹配 RuntimeId 的元素
        stack = [root_ctrl]
        visited = 0
        while stack and visited < 5000:  # 防止无限循环
            ctrl = stack.pop(0)
            visited += 1
            try:
                rid = ctrl.GetRuntimeId()
                if rid and tuple(int(x) for x in rid) == runtime_id:
                    return ctrl
            except Exception:
                pass
            try:
                children = ctrl.GetChildren() or []
                stack.extend(children)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"find uia control by runtime_id failed: {e}")
    return None


# ========== 坐标 → 可点击元素 hit-test（ZCode computer-use 风格融合） ==========

# 点击类 pattern → semantic action 映射（探测优先级从高到低：
# Button 同时支持 Invoke 时优先 invoke；CheckBox 优先 toggle，以此类推）
_PATTERN_ACTION_PRIORITY: list[tuple[str, str]] = [
    ("InvokePattern", "invoke"),
    ("TogglePattern", "toggle"),
    ("SelectionItemPattern", "select"),
    ("ExpandCollapsePattern", "expand"),
]


def _probe_click_action(ctrl) -> tuple[str, str] | None:
    """探测控件支持的"点击类"语义动作。返回 (pattern_name, action) 或 None。"""
    for pattern_name, action in _PATTERN_ACTION_PRIORITY:
        if get_pattern(ctrl, pattern_name) is not None:
            return pattern_name, action
    return None


def hit_test_clickable(x: int, y: int, max_ancestor_depth: int = 5) -> dict | None:
    """屏幕坐标 → 最近可点击 UIA 元素。

    从坐标点下的元素开始沿祖先链（≤ max_ancestor_depth 层，覆盖"点到按钮
    内部 Text"的情况）向上找第一个支持点击类 pattern 的元素。只认 pattern
    探测结果——role 相似但无 pattern 的元素 invoke 必然失败，不做融合。

    Returns:
        {"ctrl", "role", "name", "pattern", "action", "depth"} 或
        None（UIA 不可用 / 坐标无元素 / 链上无可点击元素）
    """
    if not _PLATFORM_WIN or not uia_available():
        return None
    com_ok, _ = _ensure_com_initialized()
    if not com_ok:
        return None
    try:
        import uiautomation as ua
        ctrl = ua.ControlFromPoint(int(x), int(y))
    except Exception as e:
        logger.debug(f"hit_test_clickable: ControlFromPoint({x},{y}) 失败: {e}")
        return None
    if ctrl is None:
        return None

    current = ctrl
    for depth in range(max_ancestor_depth + 1):
        try:
            role = _control_type_name(current.ControlType)
            name = current.Name or ""
        except Exception:
            break
        probe = _probe_click_action(current)
        if probe is not None:
            return {"ctrl": current, "role": role, "name": name,
                    "pattern": probe[0], "action": probe[1], "depth": depth}
        try:
            parent = current.GetParentControl()
        except Exception:
            break
        if parent is None:
            break
        current = parent
    return None


def _invoke_hit_element(hit: dict) -> dict:
    """对 hit_test_clickable 的命中元素直接执行语义动作。

    与 execute_semantic_action 的区别：跳过 snapshot/RuntimeId 重定位
    （hit-test 拿到的就是活的 Control 引用），直接调 pattern。
    """
    t0 = time.perf_counter()
    ctrl = hit["ctrl"]
    action = hit["action"]
    try:
        pattern = get_pattern(ctrl, hit["pattern"])
        if pattern is None:
            return {"success": False, "message": f"元素不支持 {hit['pattern']}"}
        if action == "invoke":
            pattern.Invoke()
        elif action == "toggle":
            pattern.Toggle()
        elif action == "select":
            pattern.Select()
        elif action == "expand":
            pattern.Expand()
        else:
            return {"success": False, "message": f"未知融合动作: {action}"}
    except Exception as e:
        return {"success": False, "message": f"UIA {action} 执行异常: {e}"}
    return {
        "success": True,
        "role": hit["role"],
        "name": hit["name"],
        "action": action,
        "message": f"UIA {action} 已执行（role={hit['role']}, name={hit['name']!r}）",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def try_uia_click_fusion(x: int, y: int) -> dict | None:
    """坐标点击的 a11y 融合入口（execute_action / batch_actions 共用）。

    hit-test 命中可点击元素则直接执行语义动作：后台完成、不抢焦点、
    不发任何键鼠事件。

    Returns:
        命中且执行成功 → {"success": True, "role", "name", "action",
        "elapsed_ms", "message", "transport": "sent_uia_invoke"}
        其余情况（UIA 不可用 / 未命中 / invoke 失败）→ None，
        调用方应回退原始键鼠路径。
    """
    hit = hit_test_clickable(x, y)
    if hit is None:
        return None
    result = _invoke_hit_element(hit)
    if not result.get("success"):
        logger.debug(f"uia click fusion invoke 失败（回退键鼠）: {result.get('message')}")
        return None
    result["transport"] = "sent_uia_invoke"
    return result


def execute_semantic_action(element_id: str, snapshot_id: str,
                             action: str, value: str | None = None,
                             direction: str | None = None,
                             amount: int = 1,
                             expected: dict | None = None) -> dict:
    """对 UIA 元素执行语义动作。

    Args:
        element_id: 来自 snapshot 的元素 ID
        snapshot_id: 来自 snapshot 的 ID（必须引用同一 snapshot）
        action: invoke | select | toggle | set_value | expand | collapse | scroll
        value: set_value 时的目标值
        direction: scroll 时的方向（up/down/left/right）
        amount: scroll 时的量（1=小步 3=大步）
        expected: 第三轮评估 P1-2 内建后验。支持 {"type":"uia_value_equals","text":"..."}。
            当 action=set_value 且 expected.type=uia_value_equals 时，动作成功后立即用
            ValuePattern.Value 读回比对，通过则 status=executed+postcondition_status=verified，
            不等则 status=postcondition_failed+postcondition_status=failed。其他 expected 类型
            （ocr_contains 等）由路由层做 OCR 后验，本函数不处理。

    Returns:
        {
            "success": bool,
            "status": str,  # executed | executed_unverified | postcondition_failed | blocked | failed
            "action": str,
            "element_id": str,
            "element_role": str | None,
            "element_name": str | None,
            "transport_status": str,  # sent | not_sent | error
            "postcondition_status": str | None,  # verified | failed | not_checked（仅当 expected=uia_value_equals 时填充）
            "postcondition_actual_value": str | None,  # 后验时实际读回的 Value
            "fallback_reason": str | None,
            "elapsed_ms": int,
            "message": str,
        }
    """
    t0 = time.perf_counter()

    # 1. 解析 element_id
    element, err = resolve_element(element_id, snapshot_id)
    if err or element is None:
        return {
            "success": False,
            "status": "blocked",
            "action": action,
            "element_id": element_id,
            "element_role": None,
            "element_name": None,
            "transport_status": "not_sent",
            "fallback_reason": STALE_ELEMENT,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"STALE_ELEMENT: {err}（snapshot_id={snapshot_id[:8]}… 过期或 element_id 不匹配）。请重新 take_uia_snapshot",
        }

    canonical_hwnd = 0
    runtime_id = element.get("runtime_id") or ()
    element_role = element.get("role")
    element_name = element.get("name")

    # 2. 通过 RuntimeId 在源 UIA tree 中重新定位 Control
    # 解析 canonical_hwnd 从 element_id
    try:
        canonical_hwnd = int(element_id.split(":")[0])
    except (ValueError, IndexError):
        pass

    if not uia_available():
        return {
            "success": False,
            "status": "blocked",
            "action": action,
            "element_id": element_id,
            "element_role": element_role,
            "element_name": element_name,
            "transport_status": "not_sent",
            "fallback_reason": UIA_NOT_AVAILABLE,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": "uiautomation 库不可用，无法执行语义动作",
        }

    # 第二轮评估 P0-1：语义动作入口也需 CoInitialize（_find_uia_control_by_runtime_id 内部用 UIA）
    com_ok, com_reason = _ensure_com_initialized()
    if not com_ok:
        return {
            "success": False,
            "status": "blocked",
            "action": action,
            "element_id": element_id,
            "element_role": element_role,
            "element_name": element_name,
            "transport_status": "not_sent",
            "fallback_reason": UIA_NOT_AVAILABLE,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"COM 初始化失败: {com_reason}",
        }

    ctrl = _find_uia_control_by_runtime_id(canonical_hwnd, runtime_id)
    if ctrl is None:
        return {
            "success": False,
            "status": "blocked",
            "action": action,
            "element_id": element_id,
            "element_role": element_role,
            "element_name": element_name,
            "transport_status": "not_sent",
            "fallback_reason": STALE_ELEMENT,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": "STALE_ELEMENT: UIA 元素已不存在（窗口可能已重建或元素已销毁）。请重新 take_uia_snapshot",
        }

    # 3. 执行动作
    error_msg = ""
    try:
        if action == "invoke":
            pattern = get_pattern(ctrl, "InvokePattern")
            if pattern is None:
                error_msg = "元素不支持 InvokePattern"
            else:
                pattern.Invoke()

        elif action == "select":
            pattern = get_pattern(ctrl, "SelectionItemPattern")
            if pattern is None:
                error_msg = "元素不支持 SelectionItemPattern"
            else:
                pattern.Select()

        elif action == "toggle":
            pattern = get_pattern(ctrl, "TogglePattern")
            if pattern is None:
                error_msg = "元素不支持 TogglePattern"
            else:
                pattern.Toggle()

        elif action == "set_value":
            if value is None:
                error_msg = "set_value 动作必须提供 value 参数"
            else:
                pattern = get_pattern(ctrl, "ValuePattern")
                if pattern is None:
                    error_msg = "元素不支持 ValuePattern"
                else:
                    pattern.SetValue(value)

        elif action == "expand":
            pattern = get_pattern(ctrl, "ExpandCollapsePattern")
            if pattern is None:
                error_msg = "元素不支持 ExpandCollapsePattern"
            else:
                pattern.Expand()

        elif action == "collapse":
            pattern = get_pattern(ctrl, "ExpandCollapsePattern")
            if pattern is None:
                error_msg = "元素不支持 ExpandCollapsePattern"
            else:
                pattern.Collapse()

        elif action == "scroll":
            pattern = get_pattern(ctrl, "ScrollPattern")
            if pattern is None:
                error_msg = "元素不支持 ScrollPattern"
            else:
                # UIA ScrollAmount: 0=NoScroll 1=SmallDecrement 2=LargeDecrement
                #                  3=SmallIncrement 4=LargeIncrement
                scroll_amount_map = {
                    "up": 2,    # LargeDecrement
                    "down": 4,  # LargeIncrement
                    "left": 2,
                    "right": 4,
                }
                # 小步用 SmallIncrement/SmallDecrement
                if amount <= 1:
                    scroll_amount_map = {
                        "up": 1, "down": 3, "left": 1, "right": 3,
                    }
                sa = scroll_amount_map.get(direction or "down", 4)
                if direction in ("up", "down"):
                    pattern.Scroll(0, sa)  # vertical
                elif direction in ("left", "right"):
                    pattern.Scroll(sa, 0)  # horizontal
                else:
                    error_msg = f"direction 必须是 up/down/left/right，当前 {direction}"

        else:
            error_msg = f"未知 action: {action}（支持: invoke/select/toggle/set_value/expand/collapse/scroll）"

    except Exception as e:
        return {
            "success": False,
            "status": "failed",
            "action": action,
            "element_id": element_id,
            "element_role": element_role,
            "element_name": element_name,
            "transport_status": "error",
            "fallback_reason": None,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": f"UIA 动作执行异常: {e}",
        }

    if error_msg:
        return {
            "success": False,
            "status": "failed",
            "action": action,
            "element_id": element_id,
            "element_role": element_role,
            "element_name": element_name,
            "transport_status": "not_sent",
            "fallback_reason": None,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "message": error_msg,
        }

    # 第三轮评估 P1-2：UIA 内建后验 —— expected.type=uia_value_equals 时立即读回 Value 比对
    postcondition_status = None
    postcondition_actual_value = None
    final_status = "executed_unverified"
    final_message = f"UIA {action} 已执行（element_role={element_role}, name={element_name!r}）"

    if expected and isinstance(expected, dict) and expected.get("type") == "uia_value_equals":
        expected_text = expected.get("text")
        if expected_text is None:
            postcondition_status = "error"
            final_status = "executed_unverified"
            final_message = (
                f"UIA {action} 已执行，但 expected.uia_value_equals 缺少 text 字段，跳过后验"
            )
        else:
            try:
                value_pattern = get_pattern(ctrl, "ValuePattern")
                if value_pattern is None:
                    postcondition_status = "error"
                    final_status = "executed_unverified"
                    final_message = (
                        f"UIA {action} 已执行，但元素不支持 ValuePattern，无法做 uia_value_equals 后验"
                    )
                else:
                    actual = value_pattern.Value
                    # 转 str 以应对 None / 非字符串值
                    actual_str = "" if actual is None else str(actual)
                    postcondition_actual_value = actual_str
                    if actual_str == expected_text:
                        postcondition_status = "verified"
                        final_status = "executed"
                        final_message = (
                            f"UIA {action} 已执行且后验通过（Value 匹配 expected，"
                            f"actual={actual_str!r}）"
                        )
                    else:
                        postcondition_status = "failed"
                        final_status = "postcondition_failed"
                        final_message = (
                            f"UIA {action} 已执行但后验失败：Value={actual_str!r}，"
                            f"expected={expected_text!r}"
                        )
            except Exception as e:
                postcondition_status = "error"
                final_status = "executed_unverified"
                final_message = (
                    f"UIA {action} 已执行，但读回 Value 异常: {e}（无法做 uia_value_equals 后验）"
                )

    return {
        "success": True,
        "status": final_status,
        "action": action,
        "element_id": element_id,
        "element_role": element_role,
        "element_name": element_name,
        "transport_status": "sent",
        "postcondition_status": postcondition_status,
        "postcondition_actual_value": postcondition_actual_value,
        "fallback_reason": None,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "message": final_message,
    }


def clear_snapshots() -> None:
    """测试用：清空 snapshot 缓存。"""
    with _uia_lock:
        _uia_snapshots.clear()


def get_snapshot_cache_size() -> int:
    """测试/监控用：当前 snapshot 缓存大小。"""
    with _uia_lock:
        return len(_uia_snapshots)
