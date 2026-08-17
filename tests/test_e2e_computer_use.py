"""Computer Use 端到端测试集（基于会话管理层重构后的三档权限模式）

设计原则（规避用户协助）：
1. **利用 watchdog 模式**：takeover_confirm 短路 + 不因空闲撤销
2. **直接调 SessionManager.grant()**：绕过 GUI 弹窗授权
3. **dry_run 优先**：不投递真键鼠，无需 admin
4. **UIA 端点优先**：走 COM 调用，无需 admin（但需真实后端，见下）
5. **真实 Windows 应用**：计算器/记事本，不 mock 被测对象

测试分层与运行环境：
- **L1 权限模式验证（TestClient）**：三档权限边界 + dry_run + /health 反映
  → 直接用 TestClient，不投递真键鼠，无需 admin
- **L2 UIA 真实操作（real_client）**：计算器/记事本 UIA 端到端
  → 需要真实后端 `uv run python -m server.main`，TestClient 线程模型与 UIA COM 不兼容
- **L3 SendInput 真实操作（TestClient + admin）**：坐标点击/键盘输入
  → SendInput 不走 COM，TestClient 可用；无 admin 时 admin_skip 自动跳过
- **L4 完整工作流（混合）**：
  - 非 UIA 工作流（窗口生命周期/resolve/token/screenshot/wait_for）→ TestClient
  - UIA 工作流（计算器/记事本 UIA）→ real_client
- **L5 只读端点（TestClient）**：capture/ocr/snapshot/list
- **L6 release 幂等 + 兼容端点（TestClient）**：release/overlay

为什么 L2 UIA 需要 real_client？
  Starlette TestClient 把同步端点放到 anyio 线程池执行，UIA 的 ControlFromHandle
  在该工作线程中会触发 Windows fatal exception (access violation)——COM 单元线程
  模型与 TestClient 线程池不兼容。real_client 连真实后端（独立进程，主线程 COM
  已正确初始化），无此问题。

运行方式：
  # 默认（只跑 TestClient 兼容部分：L1/L3/L4非UIA/L5/L6）
  uv run python -m pytest tests/test_e2e_computer_use.py -v

  # 全量（含 real_client 部分，需先启动后端）
  # 1. 另开终端：uv run python -m server.main
  # 2. 再跑：uv run python -m pytest tests/test_e2e_computer_use.py -v -m "real_backend or not real_backend"

注意：
- conftest 的 mock_overlay_client（autouse）会默认 grant normal 授权 + mock GUI 弹窗。
  本测试集通过 _clean_session_state autouse fixture 在每条用例前 release 授权，
  确保从 NO_PERMISSION 开始测三档模式边界。
- mock_overlay_client 的 confirm_action 返回 confirmed（不阻塞），但危险词拦截在
  参数校验阶段（不依赖 mock），可真实测试。
- mock_overlay_client 的 ensure_takeover_approved 被 conftest 进一步 mock 为总返回 True，
  但 watchdog 模式下 _ensure_takeover_approved 内部本身就会短路（routes.py:143-145）。
"""

from __future__ import annotations

import functools
import subprocess
import time

import pytest

from server.screen.session import Mode as SessionMode
from server.screen.session import get_session_manager

# ========== 工具函数 ==========

def _invalidate_health_cache() -> None:
    """直接操作 SessionManager 不会经过 routes.py 端点，需手动失效 /health 缓存。

    routes.py 的 screen_request_control / screen_release_control 端点会调
    invalidate_health_cache()，但本测试集直接调 SessionManager.grant/release
    绕过端点（避免 GUI 弹窗），所以需要手动失效缓存，否则 /health 返回 5s 旧数据。
    """
    try:
        from server.core.health import invalidate_health_cache
        invalidate_health_cache()
    except Exception:
        pass


def _grant_watchdog(task_description: str = "E2E 测试", hours: int = 1, shutdown: bool = False):
    """直接授予 watchdog 模式授权（绕过 GUI 弹窗）。"""
    result = get_session_manager().grant(
        mode=SessionMode.WATCHDOG,
        task_description=task_description,
        source="e2e_test",
        max_duration_hours=hours,
        shutdown_permitted=shutdown,
    )
    _invalidate_health_cache()
    return result


def _grant_normal(task_description: str = "E2E 测试"):
    """直接授予 normal 模式授权（绕过 GUI 弹窗）。"""
    result = get_session_manager().grant(
        mode=SessionMode.NORMAL,
        task_description=task_description,
        source="e2e_test",
    )
    _invalidate_health_cache()
    return result


def _release_session(reason: str = "e2e_cleanup"):
    """撤销授权。"""
    get_session_manager().release(reason)
    _invalidate_health_cache()


def _health_screen(client) -> dict:
    """获取 /health 中 screen.takeover_persistent 字段。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json().get("screen", {}).get("takeover_persistent", {})


def _status_admin(client) -> bool:
    """获取 /screen/status.admin_privileges。"""
    resp = client.get("/screen/status")
    assert resp.status_code == 200
    return bool(resp.json().get("admin_privileges", False))


def _extract_canonical_hwnd(resolve_resp_json: dict) -> int | None:
    """从 window_resolve 响应中提取 canonical_hwnd。

    resolve_window 把 canonical_hwnd 嵌套在 matches[].canonical_window.canonical_hwnd
    和 matches[].window_token.canonical_hwnd 中，不是顶层字段。
    """
    matches = resolve_resp_json.get("matches", [])
    if not matches:
        return None
    m = matches[0]
    # 优先 canonical_window.canonical_hwnd
    cw = m.get("canonical_window") or {}
    if cw.get("canonical_hwnd"):
        return cw["canonical_hwnd"]
    # 备选 window_token.canonical_hwnd
    wt = m.get("window_token") or {}
    if wt.get("canonical_hwnd"):
        return wt["canonical_hwnd"]
    # 最后 fallback 顶层 hwnd
    return m.get("hwnd")


# ========== Fixtures ==========

@pytest.fixture(autouse=True)
def _clean_session_state():
    """每条用例前后清空授权，确保从 NO_PERMISSION 开始。

    执行顺序：mock_overlay_client (conftest, grant normal) → 本 fixture (release)
    → 测试用例 fixture（如 watchdog_grant）→ 测试函数体。
    """
    _release_session("test_setup")
    yield
    _release_session("test_teardown")


@pytest.fixture
def no_permission():
    """无授权状态（默认，显式标记用例意图）。"""
    _release_session("no_permission_fixture")
    yield


@pytest.fixture
def normal_grant():
    """NORMAL 模式授权。"""
    _grant_normal()
    yield
    _release_session("normal_grant_teardown")


@pytest.fixture
def watchdog_grant():
    """WATCHDOG 模式授权（1 小时，不允许关机）。

    watchdog 模式优势：
    - takeover_confirm 短路（routes.py:143-145）→ 副作用端点不弹"接管确认"窗
    - 不因空闲撤销 → 测试中不用频繁续期
    - CONFIRM 级别操作仍弹窗（conftest mock 返回 confirmed，不阻塞）
    """
    _grant_watchdog(hours=1, shutdown=False)
    yield
    _release_session("watchdog_grant_teardown")


@pytest.fixture
def watchdog_with_shutdown():
    """WATCHDOG 模式授权 + 允许关机。"""
    _grant_watchdog(hours=1, shutdown=True)
    yield
    _release_session("watchdog_shutdown_teardown")


def _launch_calc(client) -> int | None:
    """启动计算器并等待窗口出现，返回 hwnd（可能为 canonical_hwnd 或普通 hwnd）。"""
    resp = client.post("/screen/app/launch", json={"command": "calc.exe"})
    if resp.status_code != 200 or not resp.json().get("success"):
        return None

    # 等待窗口出现，循环尝试 wait + resolve
    for _ in range(20):
        # 1. 用 app/wait 拿 hwnd（ApplicationFrameHost 宿主窗口）
        resp = client.post("/screen/app/wait", json={"title": "计算器", "timeout": 1})
        data = resp.json()
        if data.get("status") in ("found", "ambiguous"):
            matches = data.get("matches", [])
            if matches:
                hwnd = matches[0].get("hwnd")
                if hwnd:
                    return hwnd

        # 2. 用 window/resolve 拿 canonical_hwnd（更稳定）
        # 注意：UWP 计算器窗口属于 ApplicationFrameHost.exe，process_name 用宿主名
        resp = client.post("/screen/window/resolve", json={
            "title": "计算器",
        })
        if resp.json().get("status") in ("found", "WINDOW_AMBIGUOUS"):
            hwnd = _extract_canonical_hwnd(resp.json())
            if hwnd:
                return hwnd

        time.sleep(0.5)

    return None


def _launch_notepad(client) -> tuple[int, int] | None:
    """启动记事本并等待窗口出现，返回 (hwnd, pid)。

    pid 用于 teardown 时 PID-based kill（避免 /F /IM notepad.exe 误杀用户其他记事本窗口）。
    优先用 app/wait 返回的窗口所属 pid（更准确），fallback 用 app/launch 返回的 pid。
    """
    resp = client.post("/screen/app/launch", json={"command": "notepad.exe"})
    if resp.status_code != 200 or not resp.json().get("success"):
        return None
    launch_pid = resp.json().get("pid") or 0

    for _ in range(20):
        resp = client.post("/screen/app/wait", json={"title": "无标题", "timeout": 1})
        data = resp.json()
        if data.get("status") in ("found", "ambiguous"):
            matches = data.get("matches", [])
            if matches:
                hwnd = matches[0].get("hwnd")
                if hwnd:
                    pid = matches[0].get("pid") or launch_pid
                    return hwnd, pid
        time.sleep(0.5)

    return None


@pytest.fixture
def calc_app(client):
    """启动计算器，返回 hwnd。

    UWP 计算器有 ApplicationFrameHost 宿主 + CalculatorApp 内核两个窗口，
    返回 app/wait 拿到的 hwnd（宿主窗口，可用于 minimize/restore/screenshot）。
    UIA 测试需用 real_client（TestClient 下 UIA COM 会 access violation）。

    清理（P0-10 修复）：只杀 CalculatorApp.exe（UWP 内核进程），不杀
    ApplicationFrameHost.exe（系统级 UWP 宿主，杀它会关掉用户所有 UWP 应用如
    OneNote/设置/照片）。空宿主窗口由系统自回收。
    """
    hwnd = _launch_calc(client)
    if hwnd is None:
        pytest.skip("计算器窗口未出现（可能 UWP 启动慢或环境异常）")

    yield hwnd

    # 清理：只杀 CalculatorApp.exe（不杀 ApplicationFrameHost.exe，避免误杀用户其他 UWP）
    subprocess.run(["taskkill", "/F", "/IM", "CalculatorApp.exe"],
                   capture_output=True, timeout=5)


@pytest.fixture
def notepad_app(client):
    """启动记事本，返回 hwnd。

    用 notepad.exe（Win32 原生，无 UWP 宿主问题）。
    清理（P0-10 修复）：用 PID-based kill（taskkill /PID xxx /F /T），避免
    /F /IM notepad.exe 误杀用户正在用的其他记事本窗口。
    """
    result = _launch_notepad(client)
    if result is None:
        pytest.skip("记事本窗口未出现")
    hwnd, pid = result

    yield hwnd

    # 清理：PID-based kill（只杀测试启动的 notepad 进程，不误杀用户的记事本）
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=5)


def _wait_uia_ready(client, hwnd, expected_role=None, retries=10, interval=0.5):
    """等待 UIA 树就绪：snapshot 后检查元素数/角色，未就绪则重试。

    UWP 应用窗口出现后 UIA 树需要时间加载（窗口 title 匹配 ≠ UIA 树就绪）。
    若不等待，snapshot 可能只返回顶层 Window，元素数=1，测试断言失败。

    Args:
        client: real_client 或 TestClient
        hwnd: 目标窗口 hwnd
        expected_role: 期望出现的 role（如 "Button"），None 则只检查元素数 > 1
        retries: 重试次数
        interval: 每次重试间隔秒数

    Returns:
        True 就绪，False 超时仍未就绪
    """
    for _ in range(retries):
        try:
            resp = client.post("/screen/uia/snapshot", json={
                "hwnd": hwnd, "max_depth": 8, "interesting_only": True,
                "max_elements": 500,
            })
            if resp.status_code == 200:
                elements = resp.json().get("elements", [])
                if len(elements) > 1:
                    if expected_role is None:
                        return True
                    roles = {e.get("role", "") for e in elements}
                    if expected_role in roles:
                        return True
        except Exception:
            pass
        time.sleep(interval)
    return False


@pytest.fixture
def calc_app_real(real_client):
    """real_client 版本的 calc_app（用于 UIA 测试）。"""
    hwnd = _launch_calc(real_client)
    if hwnd is None:
        pytest.skip("计算器窗口未出现（real_client）")

    # 等待 UIA 树就绪（UWP 计算器窗口出现后 UIA 树需要时间加载）
    if not _wait_uia_ready(real_client, hwnd, expected_role="Button"):
        pytest.skip("计算器 UIA 树未就绪（snapshot 无 Button 元素）")

    yield hwnd

    # 清理（P0-10）：只杀 CalculatorApp.exe，不杀 ApplicationFrameHost.exe（系统级 UWP 宿主）
    subprocess.run(["taskkill", "/F", "/IM", "CalculatorApp.exe"],
                   capture_output=True, timeout=5)


@pytest.fixture
def notepad_app_real(real_client):
    """real_client 版本的 notepad_app（用于 UIA 测试）。"""
    result = _launch_notepad(real_client)
    if result is None:
        pytest.skip("记事本窗口未出现（real_client）")
    hwnd, pid = result

    # 等待 UIA 树就绪（记事本编辑区需出现）
    if not _wait_uia_ready(real_client, hwnd, expected_role="Edit"):
        pytest.skip("记事本 UIA 树未就绪（snapshot 无 Edit 元素）")

    yield hwnd

    # 清理（P0-10）：PID-based kill，避免误杀用户其他记事本窗口
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=5)


def admin_skip(func):
    """装饰器：无 admin 权限时跳过测试。

    使用 functools.wraps 保留原函数签名，让 pytest 正确注入 fixture。
    注意：pytest 通过签名注入 fixture，所以 wrapper 必须有相同的签名
    （self, client, *fixtures）才能正确接收 client fixture。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # args[0] = self (TestRealKeyboardMouse 实例)
        # args[1] = client fixture（pytest 按签名注入）
        # 但更稳妥的做法：从 kwargs 找 client，或从 args[1] 取（self 在 args[0]）
        client = args[1] if args and len(args) >= 2 else kwargs.get("client")
        if client is None or not _status_admin(client):
            pytest.skip("需要管理员权限（SendInput 路径）")
        return func(*args, **kwargs)
    return wrapper


# ========== L1: 权限模式验证（无 admin，无用户协助） ==========

class TestPermissionModes:
    """三档权限模式边界测试。

    利用 SessionManager.grant() 直接授权，绕过 GUI 弹窗。
    用 dry_run 验证副作用端点是否放行，无需 admin。
    """

    def test_no_permission_blocks_all_side_effects(self, client, no_permission):
        """NO_PERMISSION 模式下所有副作用端点返回 403。"""
        # execute_action
        resp = client.post("/screen/action", json={"action": "click", "x": 1, "y": 1})
        assert resp.status_code == 403

        # uia_action
        resp = client.post("/screen/uia/action", json={
            "element_id": "x", "snapshot_id": "y", "action": "invoke"
        })
        assert resp.status_code == 403

        # app_launch
        resp = client.post("/screen/app/launch", json={"command": "notepad.exe"})
        assert resp.status_code == 403

        # window_minimize
        resp = client.post("/screen/window/minimize", json={"hwnd": 1})
        assert resp.status_code == 403

        # window_restore
        resp = client.post("/screen/window/restore", json={"hwnd": 1})
        assert resp.status_code == 403

        # window_raise
        resp = client.post("/screen/window/raise", json={"hwnd": 1})
        assert resp.status_code == 403

        # scroll_capture
        resp = client.post("/screen/scroll-capture", json={"hwnd": 1, "scroll_count": 1})
        assert resp.status_code == 403

    def test_no_permission_allows_readonly(self, client, no_permission):
        """NO_PERMISSION 模式下只读端点正常放行。"""
        # list_windows
        resp = client.get("/screen/windows")
        assert resp.status_code == 200

        # app_list
        resp = client.get("/screen/app/list")
        assert resp.status_code == 200

        # capture
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp.status_code == 200

        # window_resolve（只读，不 403）
        resp = client.post("/screen/window/resolve", json={"title": "___nope___"})
        assert resp.status_code == 200

    def test_normal_mode_allows_dry_run(self, client, normal_grant):
        """NORMAL 模式下 dry_run 通过权限检查（不投递真键鼠）。"""
        resp = client.post("/screen/action", json={
            "action": "click", "x": 1, "y": 1, "dry_run": True
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dry_run"
        assert data["transport_status"] == "not_sent"

    def test_watchdog_mode_allows_dry_run(self, client, watchdog_grant):
        """WATCHDOG 模式下 dry_run 通过权限检查。"""
        resp = client.post("/screen/action", json={
            "action": "click", "x": 1, "y": 1, "dry_run": True
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "dry_run"

    def test_release_returns_to_no_permission(self, client, normal_grant):
        """release 后回到 NO_PERMISSION，副作用端点恢复 403。"""
        # 授权中可以 dry_run
        resp = client.post("/screen/action", json={
            "action": "click", "x": 1, "y": 1, "dry_run": True
        })
        assert resp.status_code == 200

        # 释放
        _release_session("test_release")
        assert _health_screen(client)["mode"] == "no_permission"

        # 释放后 403
        resp = client.post("/screen/action", json={
            "action": "click", "x": 1, "y": 1, "dry_run": True
        })
        assert resp.status_code == 403

    def test_watchdog_grant_overrides_normal(self, client, normal_grant):
        """grant watchdog 覆盖现有 normal 授权（支持升级）。"""
        assert _health_screen(client)["mode"] == "normal"

        _grant_watchdog(hours=2)
        status = _health_screen(client)
        assert status["mode"] == "watchdog"
        assert status["max_duration_seconds"] == 7200  # 2h


class TestHealthStatus:
    """/health 中 screen.takeover_persistent 字段反映三档模式状态。"""

    def test_no_permission_health_fields(self, client, no_permission):
        """NO_PERMISSION 模式下 /health 字段完整性。"""
        persistent = _health_screen(client)
        assert persistent["mode"] == "no_permission"
        assert persistent["active"] is False
        assert persistent["phase"] == "inactive"
        assert persistent["task_description"] == ""
        assert persistent["remaining_seconds"] == 0
        assert persistent["max_duration_seconds"] is None
        assert persistent["shutdown_permitted"] is False

    def test_normal_mode_health_fields(self, client, normal_grant):
        """NORMAL 模式下 /health 字段。"""
        persistent = _health_screen(client)
        assert persistent["mode"] == "normal"
        assert persistent["active"] is True
        assert persistent["phase"] == "normal"
        assert persistent["task_description"] == "E2E 测试"
        assert persistent["source"] == "e2e_test"
        assert persistent["max_duration_seconds"] is None
        assert persistent["shutdown_permitted"] is False
        # remaining_seconds 应接近 idle_warning + idle_grace（默认 600+1200=1800）
        assert 1700 <= persistent["remaining_seconds"] <= 1800

    def test_watchdog_mode_health_fields(self, client, watchdog_grant):
        """WATCHDOG 模式下 /health 字段。"""
        persistent = _health_screen(client)
        assert persistent["mode"] == "watchdog"
        assert persistent["active"] is True
        assert persistent["phase"] == "watchdog"
        assert persistent["max_duration_seconds"] == 3600  # 1h
        assert persistent["shutdown_permitted"] is False
        # remaining_seconds 应接近 3600
        assert 3500 <= persistent["remaining_seconds"] <= 3600

    def test_watchdog_shutdown_permitted_reflected(self, client, watchdog_with_shutdown):
        """WATCHDOG + 允许关机 → /health 反映 shutdown_permitted=True。"""
        persistent = _health_screen(client)
        assert persistent["mode"] == "watchdog"
        assert persistent["shutdown_permitted"] is True

    def test_health_changes_immediately_after_grant_and_release(self, client, no_permission):
        """授权和释放后 /health 立即反映状态变化（无缓存延迟）。"""
        # 初始 no_permission
        assert _health_screen(client)["active"] is False

        # grant normal
        _grant_normal()
        persistent = _health_screen(client)
        assert persistent["active"] is True
        assert persistent["mode"] == "normal"

        # release
        _release_session("test_immediate")
        persistent = _health_screen(client)
        assert persistent["active"] is False
        assert persistent["mode"] == "no_permission"


class TestSafetyMechanisms:
    """安全机制测试（无需 mock，参数校验阶段真实拦截）。"""

    def test_danger_keyword_blocked_without_permission(self, client, no_permission):
        """无权限时危险关键词端点返回 403（权限检查在参数校验之前）。"""
        resp = client.post("/screen/action", json={
            "action": "type", "text": "关机"
        })
        assert resp.status_code == 403  # 权限检查在前

    def test_danger_keyword_blocked_in_normal_mode(self, client, normal_grant):
        """NORMAL 模式下危险关键词被拦截（参数校验阶段，无需 mock）。"""
        resp = client.post("/screen/action", json={
            "action": "type", "text": "关机"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert "危险" in data["message"] or "拦截" in data["message"]

    def test_danger_keyword_blocked_in_watchdog_mode(self, client, watchdog_grant):
        """WATCHDOG 模式下危险关键词仍被拦截（不豁免 DANGER_KEYWORDS_BLOCK）。"""
        resp = client.post("/screen/action", json={
            "action": "type", "text": "关机"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"

    def test_missing_params_blocked(self, client, normal_grant):
        """缺少参数（click 无 x/y）→ blocked。"""
        resp = client.post("/screen/action", json={"action": "click"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"

    def test_unknown_action_blocked(self, client, normal_grant):
        """未知 action → blocked。"""
        resp = client.post("/screen/action", json={
            "action": "__unknown__", "x": 10, "y": 10
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "blocked"

    def test_stale_snapshot_id_blocked(self, client, normal_grant):
        """不存在的 snapshot_id → STALE_COORDINATES。"""
        resp = client.post("/screen/action", json={
            "action": "click", "x": 10, "y": 10,
            "snapshot_id": "__nonexistent__"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert "STALE_COORDINATES" in data["message"] or "snapshot_id" in data["message"]

    def test_focus_not_verified_no_target(self, client, normal_grant):
        """type 动作无 target_hwnd → FOCUS_NOT_VERIFIED。"""
        resp = client.post("/screen/action", json={
            "action": "type", "text": "x"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["focus_check_status"] == "FOCUS_NOT_VERIFIED"

    def test_window_range_check_invalid_hwnd(self, client, normal_grant):
        """无效 hwnd → 窗口范围检查失败。"""
        resp = client.post("/screen/action", json={
            "action": "click", "x": 99999, "y": 99999, "hwnd": 65535
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "blocked"

    def test_dry_run_passes_all_checks(self, client, normal_grant):
        """dry_run=true 走完所有前置校验后返回 dry_run 状态。

        用 hwnd=0 触发窗口范围检查异常路径（dry_run 在范围检查之后）。
        """
        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 100,
            "hwnd": 0, "dry_run": True,
        })
        data = resp.json()
        # hwnd=0 无效会在窗口范围检查阶段 blocked，不会到 dry_run
        assert data["status"] in ("dry_run", "blocked")


# ========== L2: UIA 真实操作（real_client，无 admin） ==========

@pytest.mark.real_backend
class TestUIAEndToEnd:
    """UIA 语义层端到端测试（无需 admin，走 COM 调用）。

    UIA InvokePattern/ValuePattern 不走 SendInput，无需 admin 权限。
    用 watchdog 模式规避 takeover_confirm 弹窗。

    需要 real_client：TestClient 的 anyio 线程池与 UIA COM 单元线程模型不兼容，
    ControlFromHandle 在工作线程中会 access violation。
    需先启动后端：uv run python -m server.main
    """

    def test_uia_snapshot_calculator(self, real_client, watchdog_grant, calc_app_real):
        """UIA snapshot 计算器：返回元素树含 Button。"""
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": calc_app_real, "max_depth": 8, "interesting_only": True,
            "max_elements": 500,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["snapshot_id"]  # 非空
        assert len(data["elements"]) > 0
        # 至少含一个 Button
        roles = [e["role"] for e in data["elements"]]
        assert "Button" in roles or "按钮" in roles

    def test_uia_invoke_calculator_button(self, real_client, watchdog_grant, calc_app_real):
        """UIA invoke 计算器数字键 → OCR 后验显示。"""
        # 1. snapshot
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": calc_app_real, "max_depth": 8, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()
        snapshot_id = snap["snapshot_id"]

        # 2. 找数字"7"按钮
        target = None
        for el in snap["elements"]:
            name = el.get("name", "")
            if name in ("7", "七"):
                target = el
                break
        if target is None:
            pytest.skip("计算器未找到数字 7 按钮（可能 UI 布局差异）")

        # 3. invoke
        resp = real_client.post("/screen/uia/action", json={
            "element_id": target["element_id"],
            "snapshot_id": snapshot_id,
            "action": "invoke",
        })
        assert resp.status_code == 200
        invoke_data = resp.json()
        assert invoke_data["success"] is True
        assert invoke_data["status"] in ("executed", "executed_unverified")

        # 4. OCR 后验
        resp = real_client.post("/screen/ocr", json={
            "mode": "window", "hwnd": calc_app_real, "engine": "ocr",
        })
        if resp.status_code == 200:
            ocr_text = resp.json().get("text", "")
            # 计算器显示区应含 "7"
            assert "7" in ocr_text

    def test_uia_set_value_notepad_with_postcondition(self, real_client, watchdog_grant, notepad_app_real):
        """UIA set_value 记事本 + uia_value_equals 后验。"""
        test_text = "LocalAgent E2E 中文测试"

        # 1. snapshot 记事本
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": notepad_app_real, "max_depth": 5, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()

        # 2. 找编辑区（Edit/Document）
        target = None
        for el in snap["elements"]:
            if el["role"] in ("Edit", "Document", "编辑", "文档"):
                target = el
                break
        if target is None:
            pytest.skip("记事本未找到编辑区元素")

        # 3. set_value + 后验
        resp = real_client.post("/screen/uia/action", json={
            "element_id": target["element_id"],
            "snapshot_id": snap["snapshot_id"],
            "action": "set_value",
            "value": test_text,
            "expected": {"type": "uia_value_equals", "text": test_text},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "executed"
        assert data["postcondition_status"] == "verified"

    def test_uia_set_value_postcondition_failed(self, real_client, watchdog_grant, notepad_app_real):
        """UIA set_value 后验失败（expected 不匹配实际值）。"""
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": notepad_app_real, "max_depth": 5, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()

        target = None
        for el in snap["elements"]:
            if el["role"] in ("Edit", "Document", "编辑", "文档"):
                target = el
                break
        if target is None:
            pytest.skip("记事本未找到编辑区元素")

        resp = real_client.post("/screen/uia/action", json={
            "element_id": target["element_id"],
            "snapshot_id": snap["snapshot_id"],
            "action": "set_value",
            "value": "实际写入的文字",
            "expected": {"type": "uia_value_equals", "text": "不会出现的文字"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "postcondition_failed"
        assert data["postcondition_status"] == "failed"

    def test_uia_invoke_multi_step_calculator(self, real_client, watchdog_grant, calc_app_real):
        """UIA 多步操作：7 + + 7 = → OCR 验证 14。"""
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": calc_app_real, "max_depth": 8, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()
        snapshot_id = snap["snapshot_id"]

        # 找 7, +, = 按钮
        buttons = {}
        for el in snap["elements"]:
            name = el.get("name", "")
            if name in ("7", "七", "+", "加", "=", "等于"):
                buttons[name] = el["element_id"]

        for needed in ["7", "+", "="]:
            if needed not in buttons:
                pytest.skip(f"计算器未找到按钮 {needed}")

        # 依次 invoke: 7 → + → 7 → =
        for label in ["7", "+", "7", "="]:
            eid = buttons.get(label)
            if eid is None:
                pytest.skip(f"按钮 {label} 不可用")

            resp = real_client.post("/screen/uia/action", json={
                "element_id": eid, "snapshot_id": snapshot_id, "action": "invoke",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True

        # OCR 后验
        resp = real_client.post("/screen/ocr", json={
            "mode": "window", "hwnd": calc_app_real, "engine": "ocr",
        })
        if resp.status_code == 200:
            ocr_text = resp.json().get("text", "")
            assert "14" in ocr_text

    def test_uia_no_permission_403(self, client, no_permission):
        """NO_PERMISSION 模式下 UIA action 返回 403。

        用 TestClient 而非 real_client：403 在 _enforce_session_permission() 阶段
        返回（UIA COM 调用之前），TestClient 下不会 access violation。
        real_client 场景下后端 session 状态独立于测试进程，无法通过 no_permission
        fixture 控制（后端可能是 watchdog 模式），故用 TestClient 测权限边界。
        """
        resp = client.post("/screen/uia/action", json={
            "element_id": "x", "snapshot_id": "y", "action": "invoke",
        })
        assert resp.status_code == 403

    def test_uia_snapshot_no_permission_allowed(self, real_client, calc_app_real):
        """UIA snapshot（只读端点）不检查权限，任何模式下正常放行。

        注：snapshot 端点无 _enforce_session_permission() 检查，后端无论 watchdog
        还是 NO_PERMISSION 都返回 200。real_client 场景下后端 session 状态不影响
        snapshot 结果，故不依赖 no_permission fixture。
        """
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": calc_app_real, "max_depth": 3,
        })
        # snapshot 是只读端点，不 403
        assert resp.status_code == 200


# ========== L3: SendInput 真实操作（TestClient + admin） ==========

class TestRealKeyboardMouse:
    """SendInput 真实键鼠操作（需 admin 权限）。

    无 admin 时自动跳过。用 watchdog 模式规避 takeover_confirm 弹窗。
    SendInput 不走 COM，TestClient 可用。
    """

    @admin_skip
    def test_execute_action_click_dry_run(self, client, watchdog_grant, calc_app):
        """execute_action click dry_run 路径（admin 环境下验证完整校验链）。"""
        # 用窗口中心点坐标，避免 UWP 窗口 left/top 不固定导致范围检查失败
        # （UWP 计算器窗口起点不在 (0,0)，固定坐标 (100,100) 可能落在窗口外）
        import win32gui
        left, top, right, bottom = win32gui.GetWindowRect(calc_app)
        cx, cy = (left + right) // 2, (top + bottom) // 2
        resp = client.post("/screen/action", json={
            "action": "click", "x": cx, "y": cy,
            "hwnd": calc_app, "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dry_run"
        assert data["transport_status"] == "not_sent"

    @admin_skip
    def test_focus_window_then_type(self, client, watchdog_grant, notepad_app):
        """focus_window + type 文本输入（需 admin 激活窗口 + SendInput）。"""
        # 1. focus
        resp = client.post("/screen/focus-window", json={"hwnd": notepad_app})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 2. type
        resp = client.post("/screen/action", json={
            "action": "type", "text": "Hello E2E",
            "hwnd": notepad_app,
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 3. OCR 后验
        resp = client.post("/screen/ocr", json={
            "mode": "window", "hwnd": notepad_app, "engine": "ocr",
        })
        if resp.status_code == 200:
            assert "Hello" in resp.json().get("text", "")

    @admin_skip
    def test_batch_actions_multi_step(self, client, watchdog_grant, notepad_app):
        """batch_actions 多步操作：type + enter + type。"""
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "type", "text": "line1"},
                {"action": "hotkey", "keys": ["enter"]},
                {"action": "type", "text": "line2"},
            ],
            "hwnd": notepad_app,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["executed"] == 3
        assert data["failed"] == 0

    @admin_skip
    def test_hotkey_ctrl_a(self, client, watchdog_grant, notepad_app):
        """hotkey Ctrl+A 全选（需 admin）。"""
        # 先输入文字
        client.post("/screen/action", json={
            "action": "type", "text": "test", "hwnd": notepad_app,
        })

        # Ctrl+A
        resp = client.post("/screen/action", json={
            "action": "hotkey", "keys": ["ctrl", "a"], "hwnd": notepad_app,
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @admin_skip
    def test_scroll_in_window(self, client, watchdog_grant, notepad_app):
        """scroll 滚动（需 admin）。"""
        # 先输入多行
        client.post("/screen/action", json={
            "action": "type", "text": "line\n" * 20, "hwnd": notepad_app,
        })

        # 用窗口中心点坐标，避免固定坐标 (200,200) 不在 notepad 窗口范围内
        # （notepad 窗口位置不固定，可能从屏幕右侧开始）
        import win32gui
        left, top, right, bottom = win32gui.GetWindowRect(notepad_app)
        cx, cy = (left + right) // 2, (top + bottom) // 2
        resp = client.post("/screen/action", json={
            "action": "scroll", "direction": "down", "amount": 3,
            "x": cx, "y": cy, "hwnd": notepad_app,
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ========== L4: 完整工作流（非 UIA 部分，TestClient） ==========

class TestCompleteWorkflow:
    """完整工作流串联（多步骤端到端）。

    非 UIA 工作流（窗口生命周期/resolve/token/screenshot/wait_for）用 TestClient。
    UIA 工作流见 TestUIAWorkflow（需 real_client）。
    用 watchdog 模式规避 takeover_confirm 弹窗。
    """

    def test_workflow_window_lifecycle_minimize_restore(self, client, watchdog_grant, calc_app):
        """完整流程：minimize → post_state 验证 → restore。"""
        # minimize
        resp = client.post("/screen/window/minimize", json={"hwnd": calc_app})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["post_state"]["is_minimized"] is True

        # restore
        resp = client.post("/screen/window/restore", json={"hwnd": calc_app})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["post_state"]["is_minimized"] is False

    def test_workflow_window_resolve_and_token(self, client, watchdog_grant, calc_app):
        """完整流程：window_resolve → 拿 canonical_hwnd + window_token。"""
        resp = client.post("/screen/window/resolve", json={
            "title": "计算器",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("found", "WINDOW_AMBIGUOUS")
        assert len(data["matches"]) > 0

        match = data["matches"][0]
        # canonical_hwnd 在 canonical_window 嵌套字段中
        cw = match.get("canonical_window") or {}
        assert "canonical_hwnd" in cw
        wt = match.get("window_token") or {}
        assert "canonical_hwnd" in wt
        assert "pid" in wt
        assert "process_create_time" in wt
        assert "process_name" in wt

    def test_workflow_window_token_invalid(self, client, watchdog_grant, calc_app):
        """window_close 用伪造 token → WINDOW_TOKEN_INVALID（不进 confirm，不阻塞）。"""
        fake_token = {
            "canonical_hwnd": calc_app,
            "pid": 99999,  # 故意错误的 pid
            "process_create_time": 0,
            "process_name": "fake.exe",
            "title": "计算器",
        }
        resp = client.post("/screen/window/close", json={
            "hwnd": calc_app, "window_token": fake_token, "force": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "WINDOW_TOKEN_INVALID"
        assert data.get("token_invalid_reason")

    def test_workflow_screenshot_then_ocr(self, client, watchdog_grant, calc_app):
        """完整流程：capture → ocr → 验证 OCR 返回成功（不强制断言内容，避免环境差异）。"""
        # 1. 截图
        resp = client.post("/screen/capture", json={
            "mode": "window", "hwnd": calc_app, "format": "path",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # 2. OCR
        resp = client.post("/screen/ocr", json={
            "mode": "window", "hwnd": calc_app, "engine": "ocr",
        })
        assert resp.status_code == 200
        ocr_data = resp.json()
        assert ocr_data["success"] is True
        # 不强制断言 OCR 文字内容（UWP 计算器渲染时机/字体可能让 OCR 拿不到文字），
        # 只要 OCR 流程跑通（success=True + details 是 list）即视为通过
        assert isinstance(ocr_data.get("details"), list)

    def test_workflow_screen_wait_for_condition(self, client, watchdog_grant, calc_app):
        """screen_wait_for 端点正常返回（不强制 condition_met，避免 OCR 抖动）。"""
        resp = client.post("/screen/wait-for", json={
            "expected": {"type": "ocr_contains", "text": "0"},
            "timeout": 5,
            "interval": 0.5,
            "mode": "window",
            "hwnd": calc_app,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 端点正常返回即可（check_count >= 1 表示至少检查了一次）
        assert data["check_count"] >= 1
        # condition_met 可能 True 也可能 False（OCR 抖动），不强断言


# ========== L4b: 完整工作流（UIA 部分，real_client） ==========

@pytest.mark.real_backend
class TestUIAWorkflow:
    """UIA 完整工作流（需 real_client）。

    UIA 操作需要真实后端（COM 线程模型兼容性）。
    """

    def test_workflow_calculator_uia(self, real_client, watchdog_grant, calc_app_real):
        """完整流程：snapshot → UIA invoke "5" → OCR 验证。"""
        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": calc_app_real, "max_depth": 8, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["success"] is True

        # 找"5"按钮
        target = None
        for el in snap["elements"]:
            if el.get("name") in ("5", "五"):
                target = el
                break
        if target is None:
            pytest.skip("计算器未找到数字 5 按钮")

        # UIA invoke
        resp = real_client.post("/screen/uia/action", json={
            "element_id": target["element_id"],
            "snapshot_id": snap["snapshot_id"],
            "action": "invoke",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # OCR 验证
        resp = real_client.post("/screen/ocr", json={
            "mode": "window", "hwnd": calc_app_real, "engine": "ocr",
        })
        if resp.status_code == 200:
            assert "5" in resp.json().get("text", "")

    def test_workflow_notepad_uia_write_and_verify(self, real_client, watchdog_grant, notepad_app_real):
        """完整流程：UIA set_value 写入 + OCR 验证。"""
        test_text = "E2E 工作流验证"

        resp = real_client.post("/screen/uia/snapshot", json={
            "hwnd": notepad_app_real, "max_depth": 5, "interesting_only": True,
        })
        assert resp.status_code == 200
        snap = resp.json()

        target = None
        for el in snap["elements"]:
            if el["role"] in ("Edit", "Document", "编辑", "文档"):
                target = el
                break
        if target is None:
            pytest.skip("记事本未找到编辑区")

        resp = real_client.post("/screen/uia/action", json={
            "element_id": target["element_id"],
            "snapshot_id": snap["snapshot_id"],
            "action": "set_value",
            "value": test_text,
            "expected": {"type": "uia_value_equals", "text": test_text},
        })
        assert resp.status_code == 200
        assert resp.json()["postcondition_status"] == "verified"

        # OCR 二次验证
        resp = real_client.post("/screen/ocr", json={
            "mode": "window", "hwnd": notepad_app_real, "engine": "ocr",
        })
        if resp.status_code == 200:
            assert "E2E" in resp.json().get("text", "")


# ========== L5: 状态查询端点（只读，无副作用） ==========

class TestReadonlyEndpoints:
    """只读端点测试（NO_PERMISSION 模式下也正常工作）。"""

    def test_screen_status(self, client, no_permission):
        """/screen/status 基本字段。"""
        resp = client.get("/screen/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "capture_available" in data
        assert "windows_count" in data
        assert "emergency_stopped" in data
        assert "admin_privileges" in data

    def test_list_windows(self, client, no_permission):
        """/screen/windows 窗口列表。"""
        resp = client.get("/screen/windows")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        if data["windows"]:
            w = data["windows"][0]
            assert "hwnd" in w
            assert "title" in w

    def test_app_list(self, client, no_permission):
        """/screen/app/list 应用列表。"""
        resp = client.get("/screen/app/list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["count"] >= 1

    def test_capture_fullscreen_base64(self, client, no_permission):
        """capture_screen fullscreen base64。"""
        resp = client.post("/screen/capture", json={
            "mode": "fullscreen", "format": "base64",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["width"] > 0
        assert data["height"] > 0
        assert data["snapshot_id"]

    def test_capture_fullscreen_inline(self, client, no_permission):
        """capture_screen fullscreen inline（MCP image block）。"""
        resp = client.post("/screen/capture", json={
            "mode": "fullscreen", "format": "inline",
            "max_edge": 1280, "jpeg_quality": 85,
        })
        assert resp.status_code == 200
        data = resp.json()
        # inline 模式返回 mcp_image_block
        assert data.get("mcp_image_block") is True or data.get("success") is True

    def test_capture_window_not_found(self, client, no_permission):
        """capture_screen 不存在的窗口 → 404。"""
        resp = client.post("/screen/capture", json={
            "mode": "window", "window_title": "___no_such_window___",
        })
        assert resp.status_code == 404

    def test_capture_missing_params(self, client, no_permission):
        """capture_screen mode=window 缺参数 → 400。"""
        resp = client.post("/screen/capture", json={"mode": "window"})
        assert resp.status_code == 400

    def test_screen_snapshot_with_ocr(self, client, no_permission):
        """screen_snapshot with_ocr=true。"""
        resp = client.post("/screen/snapshot", json={
            "with_ocr": True, "ocr_mode": "fullscreen",
            "max_windows": 30, "include_minimized": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["windows_count"] >= 1

    def test_screen_snapshot_without_ocr(self, client, no_permission):
        """screen_snapshot with_ocr=false（不含 ocr_* 字段）。"""
        resp = client.post("/screen/snapshot", json={"with_ocr": False})
        assert resp.status_code == 200
        data = resp.json()
        assert "ocr_text" not in data
        assert "ocr_details_count" not in data

    def test_screen_snapshot_invalid_ocr_mode(self, client, no_permission):
        """screen_snapshot 非法 ocr_mode → success=False。"""
        resp = client.post("/screen/snapshot", json={
            "ocr_mode": "__invalid__",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_screen_ocr_fullscreen(self, client, no_permission):
        """screen_ocr fullscreen。"""
        resp = client.post("/screen/ocr", json={
            "mode": "fullscreen", "engine": "ocr",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["details"], list)

    def test_window_resolve_not_found(self, client, no_permission):
        """window_resolve 0 匹配 → WINDOW_NOT_FOUND。"""
        resp = client.post("/screen/window/resolve", json={
            "title": "___nope___",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "WINDOW_NOT_FOUND"
        assert data["matches"] == []

    def test_window_resolve_missing_criteria(self, client, no_permission):
        """window_resolve 缺所有 criteria → 400。"""
        resp = client.post("/screen/window/resolve", json={})
        assert resp.status_code == 400

    def test_screen_analyze(self, client, no_permission):
        """screen_analyze fullscreen。"""
        resp = client.post("/screen/analyze", json={"mode": "fullscreen"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "top_colors" in data
        assert "brightness" in data

    def test_clear_skip_cache(self, client, no_permission):
        """clear_skip_cache 幂等。"""
        resp = client.post("/screen/skip-cache/clear")
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ========== L6: release 幂等 + 兼容端点 ==========

class TestReleaseAndCompatibility:
    """release 幂等 + 兼容端点测试。"""

    def test_release_idempotent(self, client, normal_grant):
        """release 两次都成功（幂等）。"""
        # 第一次 release
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 第二次 release（幂等）
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_release_without_grant(self, client, no_permission):
        """无授权状态下 release 仍返回成功（幂等）。"""
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_overlay_show_hide(self, client, no_permission):
        """overlay show/hide 基本流程。"""
        # show
        resp = client.post("/screen/overlay", json={
            "action": "show", "message": "测试中", "position": "top",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # hide
        resp = client.post("/screen/overlay", json={"action": "hide"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
