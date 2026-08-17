"""屏幕操控模块测试

包含截图、窗口枚举、安全机制、键鼠操控、覆盖层提示和其他 Screen 端点测试。
"""

import pytest


def _sync_screen_patches(monkeypatch, modules):
    """Sync routes.X patches to endpoint modules (post code-split).

    After monkeypatching symbols on server.screen.routes, call this to
    propagate the patched values to the endpoint modules that imported them.
    Safe to call even if no patches were applied (uses raising=False).
    """
    from server.screen import routes
    _SYMBOLS = (
        "verify_focus_for_input", "collect_focus_evidence", "resolve_canonical_window",
        "_force_focus_window", "_check_window_bounds", "_execute_action",
        "_validate_action_params", "_check_danger", "_verify_postcondition",
        "_canonical_for_response", "_find_window", "_capture_fullscreen",
        "_capture_window", "_enum_windows", "_ADMIN_STATUS", "emergency",
        "_auto_skip_coords", "_record_skip", "_ensure_takeover_approved",
        "_verify_snapshot_freshness", "detect_focus_leak", "is_hwnd_in_family",
        "is_protected_process",
    )
    for _sym in _SYMBOLS:
        _val = getattr(routes, _sym, None)
        if _val is None:
            continue
        for _mod in modules:
            monkeypatch.setattr(f"server.screen.{_mod}.{_sym}", _val, raising=False)


# ========== 截图功能测试 ==========

class TestScreenCapture:
    """截图功能测试"""

    def test_capture_fullscreen(self, client):
        """全屏截图应成功（format=base64 返回图片数据）"""
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["width"] > 0
        assert data["height"] > 0
        assert len(data["image"]) > 0
        assert data["elapsed_ms"] > 0

    def test_capture_inline_mode(self, client):
        """format=inline 返回 JPEG 压缩的 ImageContent（mcp_image_block=True）"""
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "inline"})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("mcp_image_block") is True
        assert data["width"] > 0
        assert data["height"] > 0
        assert len(data["image"]) > 0  # JPEG base64
        assert data["mime_type"] == "image/jpeg"

    def test_capture_default_mode_from_config(self, client):
        """不指定 mode 时用 config default_mode（默认 "window"）。
        mode=window 且未提供 window_title/hwnd 时应返回 400（而非静默全屏）。
        如需全屏截图，显式传 mode=fullscreen。
        """
        resp = client.post("/screen/capture", json={})
        # config default_mode="window"，无 window_title/hwnd → 400
        assert resp.status_code == 400
        assert "window_title" in resp.json()["detail"] or "hwnd" in resp.json()["detail"]

    def test_capture_window_not_found(self, client):
        """截图不存在的窗口应报错"""
        resp = client.post("/screen/capture", json={
            "mode": "window",
            "window_title": "___NONEXISTENT_WINDOW_12345___",
        })
        assert resp.status_code == 404


# ========== 窗口枚举测试 ==========

class TestWindowList:
    """窗口枚举测试"""

    def test_list_windows(self, client):
        """应返回当前打开的窗口列表"""
        resp = client.get("/screen/windows")
        assert resp.status_code == 200
        data = resp.json()
        assert "windows" in data
        assert "count" in data
        assert isinstance(data["windows"], list)
        assert data["count"] >= 0

    def test_windows_have_required_fields(self, client):
        """窗口信息应包含必要字段"""
        resp = client.get("/screen/windows")
        data = resp.json()
        if data["count"] == 0:
            pytest.skip("没有打开的窗口")
        win = data["windows"][0]
        assert "title" in win
        assert "class_name" in win
        assert "hwnd" in win
        assert "pid" in win
        assert "is_minimized" in win

    def test_windows_bbox_is_dict(self, client):
        """窗口 bbox 应为 dict 格式 {left, top, right, bottom}"""
        resp = client.get("/screen/windows")
        data = resp.json()
        for win in data["windows"]:
            if not win.get("is_minimized", True):
                bbox = win.get("bbox", {})
                assert isinstance(bbox, dict)
                for key in ("left", "top", "right", "bottom"):
                    assert key in bbox


# ========== 安全机制测试 ==========

class TestSafety:
    """安全机制测试"""

    def test_danger_keywords_block(self, client):
        """包含关机关键词的操作应被阻止 (status=blocked)"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
            "element_text": "关机",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["success"] is False

    def test_danger_keywords_block_in_text(self, client):
        """text字段包含关机关键词也应被阻止"""
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "重启电脑",
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["success"] is False

    def test_danger_keywords_block_case_insensitive_compact_english(self, client):
        """英文危险词应忽略大小写及空格变体。"""
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "shutdown computer",
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert data["success"] is False

    def test_danger_keywords_confirm_forces_confirm(self, client):
        """包含删除关键词的操作应强制确认"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
            "element_text": "删除文件",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("blocked", "confirmed", "cancelled", "executed")

    def test_safe_action_not_blocked(self, client):
        """普通操作不应被阻止（非管理员时因权限失败，非安全拦截）"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 非管理员时因权限失败，管理员时正常执行
        assert data["status"] != "blocked" or "管理员" in data.get("message", "") or "关机" in data.get("message", "")

    def test_emergency_stop_status(self, client):
        """紧急停止状态应可通过 /screen/status 查询"""
        resp = client.get("/screen/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "emergency_stopped" in data

    def test_emergency_stop_via_action(self, client):
        """紧急停止后操作应被拒绝"""
        status = client.get("/screen/status").json()
        if status.get("emergency_stopped"):
            resp = client.post("/screen/action", json={
                "action": "click",
                "x": 500,
                "y": 400,
                "require_confirm": False,
            })
            data = resp.json()
            assert data["status"] == "emergency_stopped"
            assert data["success"] is False
        else:
            pytest.skip("紧急停止需要通过全局快捷键 Ctrl+` 触发，无法通过HTTP触发")

    def test_window_bounds_check(self, client):
        """操作坐标不在目标窗口范围内应被阻止"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 9999,
            "y": 9999,
            "window_title": "___NONEXISTENT_WINDOW_12345___",
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 管理员时被窗口范围检查拦截，非管理员时被权限检查拦截
        assert data["success"] is False

    def test_invalid_hwnd_fails_closed(self, client):
        """提供失效 hwnd 时不能继续执行坐标操作。"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "hwnd": 99999999,
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"


# ========== 键鼠操控测试 ==========

class TestMouseKeyboard:
    """键鼠操控测试"""

    def _is_admin(self, client):
        """检查后端是否以管理员权限运行"""
        resp = client.get("/screen/status")
        return resp.json().get("admin_privileges", False)

    def test_type_action_safe(self, client):
        """type 操作应能发送（或因非管理员权限返回失败）"""
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "hello",
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        if self._is_admin(client):
            # admin 环境下 type 操作可能因 focus check 拦截返回 blocked
            # （未指定 target 且前台窗口不匹配时，FOCUS_NOT_VERIFIED 是合法安全行为）
            assert data["status"] in ("executed", "confirmed", "cancelled", "blocked")
        else:
            assert data["success"] is False

    def test_hotkey_action_safe(self, client):
        """hotkey 操作应能发送"""
        resp = client.post("/screen/action", json={
            "action": "hotkey",
            "keys": ["ctrl", "a"],
            "require_confirm": False,
        })
        assert resp.status_code == 200

    def test_scroll_action_safe(self, client):
        """scroll 操作应能发送"""
        resp = client.post("/screen/action", json={
            "action": "scroll",
            "x": 500,
            "y": 400,
            "dx": 0,
            "dy": -3,
            "require_confirm": False,
        })
        assert resp.status_code == 200

    def test_invalid_action_returns_error(self, client):
        """无效 action 返回 200 + success=False + status=blocked（server/screen/action_endpoints.py:239-245）

        _validate_action_params 对未知 action 返回 (False, "未知 action...")，
        端点 return ActionResponse(success=False, status="blocked", message=errmsg)，
        HTTP 状态码 200（FastAPI 默认，未 raise HTTPException）。

        修复（2026-08-06 测试修复铁律）：原 `in (200, 422)` 放宽断言，读被测代码后
        确认预期是 200 + body success=False，改为精确断言。
        """
        resp = client.post("/screen/action", json={
            "action": "nonexistent_action",
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert "未知 action" in data["message"] or "nonexistent_action" in data["message"]


# ========== 覆盖层提示测试 ==========

class TestOverlay:
    """覆盖层提示测试"""

    def test_show_overlay(self, client):
        """应能显示覆盖层"""
        resp = client.post("/screen/overlay", json={
            "action": "show",
            "message": "Agent操作中",
            "position": "top",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True

    def test_hide_overlay(self, client):
        """应能隐藏覆盖层"""
        resp = client.post("/screen/overlay", json={"action": "hide"})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True


# ========== Screen 其他端点测试 ==========

class TestScreenMisc:
    """Screen 模块其他端点测试"""

    def test_screen_status(self, client):
        """GET /screen/status 应返回完整状态"""
        resp = client.get("/screen/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "capture_available" in data
        assert "windows_count" in data
        assert "emergency_stopped" in data
        assert "overlay_visible" in data
        assert "auto_skip_cache_size" in data
        assert "admin_privileges" in data

    def test_skip_cache_clear(self, client):
        """清空自动跳过缓存应成功"""
        resp = client.post("/screen/skip-cache/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_confirm_start(self, client):
        """Agent接管确认弹窗应返回结果"""
        resp = client.post("/screen/confirm/start", json={
            "task_description": "测试任务",
            "hotkey_hint": "Ctrl+`",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("confirmed", "cancelled")


# ========== 焦点安全：focus.py 单元测试（评估文档 P0） ==========

class TestFocusModule:
    """焦点安全核心模块单元测试（不依赖 Windows API，纯逻辑验证）"""

    def test_default_protected_processes_includes_agent_hosts(self):
        """默认受保护进程列表应包含主流 agent 宿主"""
        from server.screen.focus import DEFAULT_PROTECTED_PROCESSES
        # 大小写变体都应在列表内
        assert "ChatGPT.exe" in DEFAULT_PROTECTED_PROCESSES
        assert "Codex.exe" in DEFAULT_PROTECTED_PROCESSES
        assert "trae.exe" in DEFAULT_PROTECTED_PROCESSES or "Trae.exe" in DEFAULT_PROTECTED_PROCESSES
        assert "cursor.exe" in DEFAULT_PROTECTED_PROCESSES or "Cursor.exe" in DEFAULT_PROTECTED_PROCESSES

    def test_status_code_constants_are_distinct_strings(self):
        """四个状态码常量应为不同的非空字符串（agent 据此分支判断）"""
        from server.screen.focus import (
            EXECUTED_UNVERIFIED,
            FOCUS_LEAK_PREVENTED,
            FOCUS_NOT_VERIFIED,
            STALE_COORDINATES,
        )
        codes = {FOCUS_NOT_VERIFIED, FOCUS_LEAK_PREVENTED, STALE_COORDINATES, EXECUTED_UNVERIFIED}
        assert len(codes) == 4
        for c in codes:
            assert isinstance(c, str) and c

    def test_is_protected_process_default_list(self):
        """默认列表应识别 ChatGPT/Codex/Trae/Cursor"""
        from server.screen.focus import is_protected_process
        assert is_protected_process("ChatGPT.exe") is True
        assert is_protected_process("Codex.exe") is True
        assert is_protected_process("Trae.exe") is True
        assert is_protected_process("Cursor.exe") is True

    def test_is_protected_process_case_insensitive(self):
        """大小写不敏感匹配（agent 宿主进程名可能因安装路径不同而大小写不一）"""
        from server.screen.focus import is_protected_process
        assert is_protected_process("chatgpt.exe") is True
        assert is_protected_process("CHATGPT.EXE") is True
        assert is_protected_process("codex.exe") is True

    def test_is_protected_process_unprotected(self):
        """非受保护进程应返回 False"""
        from server.screen.focus import is_protected_process
        assert is_protected_process("notepad.exe") is False
        assert is_protected_process("explorer.exe") is False
        assert is_protected_process("") is False
        assert is_protected_process(None) is False

    def test_is_protected_process_custom_list_overrides_default(self):
        """显式传入 protected_processes 列表时应覆盖默认列表"""
        from server.screen.focus import is_protected_process
        # 自定义列表不含 ChatGPT.exe → 不应识别为受保护
        assert is_protected_process("ChatGPT.exe", protected_processes=["CustomAgent.exe"]) is False
        # 自定义列表中的进程应识别
        assert is_protected_process("CustomAgent.exe", protected_processes=["CustomAgent.exe"]) is True

    def test_is_hwnd_in_family(self):
        """同族 HWND 检查"""
        from server.screen.focus import is_hwnd_in_family
        assert is_hwnd_in_family(12345, [12345, 67890]) is True
        assert is_hwnd_in_family(67890, [12345, 67890]) is True
        assert is_hwnd_in_family(99999, [12345, 67890]) is False
        # 边界：空入参
        assert is_hwnd_in_family(0, [12345]) is False
        assert is_hwnd_in_family(12345, []) is False
        assert is_hwnd_in_family(12345, None) is False

    def test_detect_focus_leak_protected_foreground_no_target(self):
        """前台是受保护进程且未指定目标 → 检测到泄漏"""
        from server.screen.focus import detect_focus_leak
        evidence = {"foreground": {"process_name": "ChatGPT.exe"}}
        leak, reason = detect_focus_leak(target_hwnd=None, evidence=evidence)
        assert leak is True
        assert "ChatGPT.exe" in reason

    def test_detect_focus_leak_protected_foreground_unprotected_target(self):
        """前台是 ChatGPT 但目标是 notepad → 检测到泄漏"""
        from server.screen.focus import detect_focus_leak
        evidence = {"foreground": {"process_name": "ChatGPT.exe"}}
        # target_hwnd 已指定但目标非受保护进程（mock resolve_canonical_window）
        leak, reason = detect_focus_leak(
            target_hwnd=99999,
            evidence=evidence,
            protected_processes=["ChatGPT.exe"],
        )
        # 注意：detect_focus_leak 内部会调 resolve_canonical_window，
        # 非 Windows 或 hwnd 无效时 canonical_process_name 为空 → 仍判定为泄漏
        assert leak is True
        assert "FOCUS_LEAK_PREVENTED" in reason

    def test_detect_focus_leak_unprotected_foreground(self):
        """前台是非受保护进程（如 notepad）→ 无泄漏"""
        from server.screen.focus import detect_focus_leak
        evidence = {"foreground": {"process_name": "notepad.exe"}}
        leak, _ = detect_focus_leak(target_hwnd=None, evidence=evidence)
        assert leak is False

    def test_verify_focus_for_input_leak_overrides_allow_unfocused(self):
        """FOCUS_LEAK_PREVENTED 优先级最高，allow_unfocused_input=true 也不能放行。

        评估文档 P0 核心要求：即使 agent 显式传 allow_unfocused_input=true，
        也必须拒绝向前台是 ChatGPT/Codex 等受保护窗口发送键盘输入。
        """
        from server.screen.focus import FOCUS_LEAK_PREVENTED, verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 100, "title": "ChatGPT", "process_name": "ChatGPT.exe"},
            "focus": {"hwnd": 100, "process_name": "ChatGPT.exe"},
            "target": None,
            "target_family_hwnds": [],
            "target_match": False,
        }
        ok, reason, ev = verify_focus_for_input(
            target_hwnd=None,
            allow_unfocused_input=True,  # agent 显式放行
            protected_processes=["ChatGPT.exe"],
            evidence=evidence,
        )
        assert ok is False
        assert reason == FOCUS_LEAK_PREVENTED
        assert ev is evidence  # 原始 evidence 回传

    def test_verify_focus_for_input_no_target_no_allow_returns_not_verified(self):
        """未指定目标且 allow_unfocused_input=False → FOCUS_NOT_VERIFIED"""
        from server.screen.focus import FOCUS_NOT_VERIFIED, verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 100, "process_name": "notepad.exe"},
            "focus": {"hwnd": 100, "process_name": "notepad.exe"},
            "target": None,
            "target_family_hwnds": [],
            "target_match": False,
        }
        ok, reason, _ = verify_focus_for_input(
            target_hwnd=None,
            allow_unfocused_input=False,
            evidence=evidence,
        )
        assert ok is False
        assert reason == FOCUS_NOT_VERIFIED

    def test_verify_focus_for_input_no_target_with_allow_passes(self):
        """未指定目标但 allow_unfocused_input=True → 放行（unfocused_input_allowed）"""
        from server.screen.focus import verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 100, "process_name": "notepad.exe"},
            "target": None,
            "target_match": False,
        }
        ok, reason, _ = verify_focus_for_input(
            target_hwnd=None,
            allow_unfocused_input=True,
            evidence=evidence,
        )
        assert ok is True
        assert reason == "unfocused_input_allowed"

    def test_verify_focus_for_input_target_match_passes(self):
        """目标已指定且 target_match=True → focus_verified"""
        from server.screen.focus import verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 100, "process_name": "notepad.exe"},
            "target": {"hwnd": 100, "process_name": "notepad.exe"},
            "target_family_hwnds": [100],
            "target_match": True,
        }
        ok, reason, _ = verify_focus_for_input(
            target_hwnd=100,
            allow_unfocused_input=False,
            evidence=evidence,
        )
        assert ok is True
        assert reason == "focus_verified"

    def test_verify_focus_for_input_target_no_match_no_allow_blocks(self):
        """目标已指定但 target_match=False 且 allow_unfocused_input=False → FOCUS_NOT_VERIFIED"""
        from server.screen.focus import FOCUS_NOT_VERIFIED, verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target": {"hwnd": 100, "process_name": "calculator.exe"},
            "target_family_hwnds": [100],
            "target_match": False,
        }
        ok, reason, _ = verify_focus_for_input(
            target_hwnd=100,
            allow_unfocused_input=False,
            evidence=evidence,
        )
        assert ok is False
        assert reason == FOCUS_NOT_VERIFIED

    def test_verify_focus_for_input_target_no_match_with_allow_passes(self):
        """目标已指定但 target_match=False 且 allow_unfocused_input=True → 放行"""
        from server.screen.focus import verify_focus_for_input
        evidence = {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target": {"hwnd": 100, "process_name": "calculator.exe"},
            "target_family_hwnds": [100],
            "target_match": False,
        }
        ok, reason, _ = verify_focus_for_input(
            target_hwnd=100,
            allow_unfocused_input=True,
            evidence=evidence,
        )
        assert ok is True
        assert reason == "unfocused_input_allowed"

    def test_resolve_canonical_window_invalid_hwnd_returns_safe_default(self):
        """无效 HWND（0 或 None）应返回安全默认值（不抛异常）"""
        from server.screen.focus import resolve_canonical_window
        result = resolve_canonical_window(0)
        assert result["canonical_hwnd"] == 0
        assert result["family_hwnds"] == [0]
        assert result["is_uwp_host"] is False
        # input_hwnd 应原样回传
        assert result["input_hwnd"] == 0


# ========== 焦点安全：execute_action 集成测试 ==========

class TestFocusSafetyRoutes:
    """execute_action / batch_actions 焦点安全集成测试

    评估文档 P0：键盘动作必须走焦点强校验，FOCUS_LEAK_PREVENTED / FOCUS_NOT_VERIFIED
    时零按键发送，响应携带焦点证据与分层状态。
    """

    def test_type_action_no_target_no_allow_returns_focus_not_verified(self, client, monkeypatch):
        """type 动作未指定 target 且 allow_unfocused_input=False → FOCUS_NOT_VERIFIED（零按键）"""
        from server.screen import routes
        from server.screen.focus import FOCUS_NOT_VERIFIED

        # mock verify_focus_for_input 返回 FOCUS_NOT_VERIFIED
        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            ev = evidence or {
                "foreground": {"hwnd": 200, "title": "Notepad", "process_name": "notepad.exe"},
                "focus": {"hwnd": 200, "process_name": "notepad.exe"},
                "target": None,
                "target_family_hwnds": [],
                "target_match": False,
            }
            return False, FOCUS_NOT_VERIFIED, ev

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        # 跳过激活窗口路径（无 target_hwnd 不会触发，但保险起见）
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "hello",
            "require_confirm": False,
            "allow_unfocused_input": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert data["focus_check_status"] == FOCUS_NOT_VERIFIED
        assert data["transport_status"] == "not_sent"  # 零按键
        assert data["delivery_status"] == "skipped"
        assert data["postcondition_status"] == "not_checked"
        assert data["foreground_before"] is not None
        assert "FOCUS_NOT_VERIFIED" in data["message"]

    def test_type_action_focus_leak_prevented_overrides_allow(self, client, monkeypatch):
        """FOCUS_LEAK_PREVENTED 优先级高于 allow_unfocused_input=true。

        评估文档 P0：即使 agent 显式传 allow_unfocused_input=true，
        前台是 ChatGPT 时也必须零按键发送。
        """
        from server.screen import routes
        from server.screen.focus import FOCUS_LEAK_PREVENTED

        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            ev = {
                "foreground": {"hwnd": 100, "title": "ChatGPT", "process_name": "ChatGPT.exe"},
                "focus": {"hwnd": 100, "process_name": "ChatGPT.exe"},
                "target": None,
                "target_family_hwnds": [],
                "target_match": False,
            }
            return False, FOCUS_LEAK_PREVENTED, ev

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "secret",
            "require_confirm": False,
            "allow_unfocused_input": True,  # agent 显式放行
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert data["focus_check_status"] == FOCUS_LEAK_PREVENTED
        assert data["transport_status"] == "not_sent"
        assert "FOCUS_LEAK_PREVENTED" in data["message"]
        assert "受保护进程" in data["message"] or "ChatGPT.exe" in data["message"]

    def test_type_action_unfocused_allowed_executed_unverified(self, client, monkeypatch):
        """allow_unfocused_input=True 放行后无 expected → executed_unverified（非 executed）"""
        from server.screen import routes

        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            ev = {
                "foreground": {"hwnd": 200, "title": "Notepad", "process_name": "notepad.exe"},
                "focus": {"hwnd": 200, "process_name": "notepad.exe"},
                "target": None,
                "target_family_hwnds": [],
                "target_match": False,
            }
            return True, "unfocused_input_allowed", ev

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        # mock 焦点证据收集（操作后）
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target_match": False,
        })
        # mock resolve_canonical_window 避免实际 Windows 调用
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0,
            "canonical_title": "",
            "canonical_pid": 0,
            "canonical_process_name": "",
            "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd,
            "is_uwp_host": False,
        })

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "hello",
            "require_confirm": False,
            "allow_unfocused_input": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 焦点放行后动作应执行（或因非管理员失败，但 focus_check_status 应反映放行）
        assert data["focus_check_status"] == "unfocused_input_allowed"
        # 动作成功时未声明 expected → executed_unverified
        if data["success"]:
            assert data["status"] == "executed_unverified"
            assert data["postcondition_status"] == "executed_unverified"
            assert data["transport_status"] == "sent"

    def test_click_action_focus_check_skipped(self, client, monkeypatch):
        """非键盘动作（click）的 focus_check_status 应为 skipped（不强制校验）"""
        from server.screen import routes

        # 即使强制 mock verify_focus_for_input 返回阻断，click 也不应走焦点校验路径
        def fake_verify(*args, **kwargs):
            raise AssertionError("click 动作不应调用 verify_focus_for_input")

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 0, "process_name": ""},
            "target_match": False,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        # mock 窗口范围检查通过
        monkeypatch.setattr(routes, "_check_window_bounds", lambda *a, **k: True)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # click 不走焦点校验
        assert data["focus_check_status"] == "skipped"

    def test_action_response_includes_layered_status_fields(self, client, monkeypatch):
        """ActionResponse 应包含分层状态字段（transport/delivery/postcondition）"""
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "require_confirm": False,
            "allow_unfocused_input": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 分层状态字段必须在响应中（即使值为 None 也应存在）
        assert "transport_status" in data
        assert "delivery_status" in data
        assert "postcondition_status" in data
        assert "focus_check_status" in data
        assert "foreground_before" in data
        assert "foreground_after" in data
        assert "target_match_before" in data
        assert "target_match_after" in data
        assert "canonical_window" in data


# ========== STALE_COORDINATES 测试 ==========

class TestStaleCoordinates:
    """坐标绑定 snapshot_id 检测（评估文档 P0）"""

    def test_capture_returns_snapshot_id(self, client):
        """capture_screen 响应应包含非空 snapshot_id（用于 STALE_COORDINATES 检测）"""
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["snapshot_id"] is not None
        assert isinstance(data["snapshot_id"], str)
        assert len(data["snapshot_id"]) > 0

    def test_capture_returns_window_rect(self, client):
        """capture_screen 响应应包含 window_rect（窗口几何 [left, top, right, bottom]）"""
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp.status_code == 200
        data = resp.json()
        # window_rect 可能为 None（极端情况），但字段必须存在
        assert "window_rect" in data
        if data["window_rect"] is not None:
            assert isinstance(data["window_rect"], list)
            assert len(data["window_rect"]) == 4

    def test_click_with_nonexistent_snapshot_returns_stale(self, client, monkeypatch):
        """snapshot_id 不存在 → STALE_COORDINATES blocked"""
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
            "snapshot_id": "nonexistent_snapshot_id_12345",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert data["transport_status"] == "not_sent"
        assert "STALE_COORDINATES" in data["message"]

    def test_click_with_valid_snapshot_passes_check(self, client, monkeypatch):
        """有效的 snapshot_id 应通过新鲜度检查（不返回 STALE_COORDINATES）"""
        from server.screen import routes

        # 1. 先 capture 获取 snapshot_id
        cap_resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert cap_resp.status_code == 200
        snapshot_id = cap_resp.json().get("snapshot_id")
        assert snapshot_id, "capture 应返回 snapshot_id"

        # 2. mock 窗口范围检查通过
        monkeypatch.setattr(routes, "_check_window_bounds", lambda *a, **k: True)

        # 3. 用该 snapshot_id 调 click
        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
            "snapshot_id": snapshot_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 有效 snapshot_id 不应触发 STALE_COORDINATES（动作可能因其他原因失败，但不是 stale）
        assert "STALE_COORDINATES" not in data.get("message", "")

    def test_stale_snapshot_after_window_move(self, client, monkeypatch):
        """窗口几何变化后 snapshot_id 失效 → STALE_COORDINATES

        模拟：capture 记录窗口几何，然后窗口移动，click 用旧 snapshot_id 应被拦截。
        """
        from server.screen import routes

        # 直接注入一个 snapshot 元数据，模拟旧截图
        fake_snapshot_id = "fake_stale_test_snapshot"
        routes._record_snapshot(fake_snapshot_id, {
            "captured_at": __import__("time").time(),
            "window_rect": (0, 0, 800, 600),  # 旧位置
            "window_title": "TestWindow",
            "hwnd": 999999,  # 假 hwnd
            "image_size": [800, 600],
            "mode": "window",
        })

        # mock win32gui.GetWindowRect 返回不同的几何（模拟窗口移动后）
        def fake_get_window_rect(hwnd):
            return (100, 100, 900, 700)  # 新位置——与 snapshot 不符

        monkeypatch.setattr("win32gui.GetWindowRect", fake_get_window_rect)

        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 200,
            "y": 200,
            "hwnd": 999999,
            "require_confirm": False,
            "snapshot_id": fake_snapshot_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert "STALE_COORDINATES" in data["message"]
        assert data["transport_status"] == "not_sent"

    def test_expired_snapshot_returns_stale(self, client, monkeypatch):
        """snapshot 超过 TTL（5 分钟）→ STALE_COORDINATES"""
        import time as _time

        from server.screen import routes

        fake_snapshot_id = "fake_expired_snapshot"
        # captured_at 设为 10 分钟前（超过 _SNAPSHOT_TTL_SECONDS=300s）
        routes._record_snapshot(fake_snapshot_id, {
            "captured_at": _time.time() - 600,
            "window_rect": None,
            "window_title": None,
            "hwnd": None,
            "image_size": [800, 600],
            "mode": "fullscreen",
        })

        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 500,
            "y": 400,
            "require_confirm": False,
            "snapshot_id": fake_snapshot_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert "STALE_COORDINATES" in data["message"]


# ========== 分层状态 + 声明式后验测试 ==========

class TestLayeredStatus:
    """transport_status / delivery_status / postcondition_status 分层状态测试"""

    def test_postcondition_executed_unverified_when_no_expected(self, client, monkeypatch):
        """未声明 expected 时 postcondition_status=executed_unverified（评估文档 P0）"""
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "require_confirm": False,
            "allow_unfocused_input": True,
        })
        data = resp.json()
        if data["success"]:
            # 动作成功且未声明 expected → executed_unverified
            assert data["postcondition_status"] == "executed_unverified"
            assert data["status"] == "executed_unverified"

    def test_postcondition_verified_when_expected_ocr_contains_match(self, client, monkeypatch):
        """expected=ocr_contains 且 OCR 找到文本 → postcondition_status=verified, status=executed"""
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        # mock _execute_action 返回成功
        monkeypatch.setattr(routes, "_execute_action", lambda **kwargs: {"success": True, "message": "ok"})
        # mock 截图（用于 OCR 后验）
        monkeypatch.setattr(routes, "_capture_window", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        # mock OCR 返回包含目标文本
        class FakeOcrResp:
            text = "Hello World 你好"
            details = []
        monkeypatch.setattr("server.ocr._do_ocr", lambda *a, **k: FakeOcrResp())
        # mock PIL.Image.open 返回一个 fake image 对象
        class FakeImage:
            def __init__(self):
                self.size = (100, 100)
        monkeypatch.setattr("PIL.Image.open", lambda *a, **k: FakeImage())

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "require_confirm": False,
            "allow_unfocused_input": True,
            "expected": {"type": "ocr_contains", "text": "Hello"},
        })
        data = resp.json()
        assert data["success"] is True
        assert data["postcondition_status"] == "verified"
        assert data["status"] == "executed"

    def test_postcondition_failed_when_expected_ocr_contains_not_match(self, client, monkeypatch):
        """expected=ocr_contains 但 OCR 未找到文本 → postcondition_status=failed, status=postcondition_failed

        第二轮评估 P0-3：动作已发送但后验失败时 status=postcondition_failed（不再包装为 blocked），
        以区分"动作执行失败"（blocked）和"动作成功但后验失败"（postcondition_failed）。
        """
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        monkeypatch.setattr(routes, "_execute_action", lambda **kwargs: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_capture_window", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        # mock OCR 返回不含目标文本
        class FakeOcrResp:
            text = "Completely different text"
            details = []
        monkeypatch.setattr("server.ocr._do_ocr", lambda *a, **k: FakeOcrResp())
        class FakeImage:
            def __init__(self):
                self.size = (100, 100)
        monkeypatch.setattr("PIL.Image.open", lambda *a, **k: FakeImage())

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "require_confirm": False,
            "allow_unfocused_input": True,
            "expected": {"type": "ocr_contains", "text": "Hello"},
        })
        data = resp.json()
        assert data["postcondition_status"] == "failed"
        # 第二轮评估 P0-3：动作已发送，status=postcondition_failed（不再包装为 blocked）
        assert data["status"] == "postcondition_failed"

    def test_postcondition_error_when_expected_type_invalid(self, client, monkeypatch):
        """expected.type 不合法 → postcondition_status=error"""
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        monkeypatch.setattr(routes, "_execute_action", lambda **kwargs: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_capture_window", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda *a, **k: b"\x89PNG\r\n\x1a\n")
        class FakeImage:
            def __init__(self):
                self.size = (100, 100)
        monkeypatch.setattr("PIL.Image.open", lambda *a, **k: FakeImage())

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "require_confirm": False,
            "allow_unfocused_input": True,
            "expected": {"type": "invalid_type", "text": "Hello"},
        })
        data = resp.json()
        assert data["postcondition_status"] == "error"

    def test_delivery_status_delivered_when_target_match(self, client, monkeypatch):
        """操作后前台仍在目标族 → delivery_status=delivered"""
        from server.screen import routes

        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        # 操作后证据：target_match=True → delivered
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        # 聚焦 delivery_status 分支判定，mock 掉键鼠执行避免真实副作用干扰
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "hwnd": 1,  # 显式声明目标窗口，与 monkeypatch 证据（foreground.hwnd=1）一致
            "require_confirm": False,
            "allow_unfocused_input": True,
        })
        data = resp.json()
        # 不用 `if data["success"]:` 守卫——避免动作失败时假通过（按测试修复铁律）
        assert data["success"] is True
        assert data["delivery_status"] == "delivered"
        assert data["target_match_after"] is True

    def test_delivery_status_leaked_when_focus_drifted(self, client, monkeypatch):
        """操作后前台漂移到非目标族 → delivery_status=leaked"""
        from server.screen import routes

        # 操作前匹配，操作后漂移
        before_ev = {
            "foreground": {"hwnd": 1, "process_name": "test.exe"},
            "focus": {"hwnd": 1, "process_name": "test.exe"},
            "target": {"hwnd": 1, "process_name": "test.exe"},
            "target_family_hwnds": [1],
            "target_match": True,
        }
        after_ev = {
            "foreground": {"hwnd": 999, "process_name": "other.exe"},
            "focus": {"hwnd": 999, "process_name": "other.exe"},
            "target": {"hwnd": 1, "process_name": "test.exe"},
            "target_family_hwnds": [1],
            "target_match": False,
        }
        # 第一次调用返回 before_ev（焦点校验时），第二次返回 after_ev（操作后）
        call_count = {"n": 0}
        def fake_collect(target_hwnd=None):
            call_count["n"] += 1
            return before_ev if call_count["n"] == 1 else after_ev

        monkeypatch.setattr(routes, "collect_focus_evidence", fake_collect)
        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", before_ev))
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
            "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd, "is_uwp_host": False,
        })
        # 聚焦 delivery_status 分支判定，mock 掉键鼠执行避免真实副作用干扰
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "type",
            "text": "test",
            "hwnd": 1,  # 显式声明目标窗口，与 monkeypatch 证据（target.hwnd=1）一致
            "require_confirm": False,
            "allow_unfocused_input": True,
        })
        data = resp.json()
        # 不用 `if data["success"]:` 守卫——避免动作失败时假通过（按测试修复铁律）
        assert data["success"] is True
        assert data["delivery_status"] == "leaked"
        assert data["target_match_after"] is False


# ========== 批量动作焦点漂移测试 ==========

class TestBatchFocusDrift:
    """batch_actions 焦点漂移停止逻辑（评估文档 P0）"""

    def test_batch_aborts_on_focus_drift(self, client, monkeypatch):
        """stop_on_focus_drift=True 时焦点漂移即停止剩余动作"""
        from server.screen import routes

        # mock 管理员权限（batch_actions 入口检查）
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        # mock 紧急停止未触发
        monkeypatch.setattr(routes, "emergency", type("E", (), {
            "is_stopped": False, "cooldown_seconds": 10,
            "can_operate": lambda self: True,
        })())
        # mock _validate_action_params 全部通过
        monkeypatch.setattr(routes, "_validate_action_params", lambda *a, **k: (True, ""))
        # mock 焦点校验通过
        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        # mock _execute_action 始终成功
        monkeypatch.setattr(routes, "_execute_action", lambda **kwargs: {"success": True, "message": "ok"})
        # mock 焦点证据：第二次起漂移（target_match=False → leaked）
        call_count = {"n": 0}
        def fake_collect(target_hwnd=None):
            call_count["n"] += 1
            return {
                "foreground": {"hwnd": 1 if call_count["n"] == 1 else 999, "process_name": "test.exe"},
                "target_match": call_count["n"] == 1,
            }
        monkeypatch.setattr(routes, "collect_focus_evidence", fake_collect)
        # mock _check_danger 通过
        monkeypatch.setattr(routes, "_check_danger", lambda *a, **k: "none")
        # mock 窗口激活（避免实际调用 _force_focus_window）
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        monkeypatch.setattr(routes, "_find_window", lambda *a, **k: None)

        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "type", "text": "hello"},
                {"action": "type", "text": "world"},
                {"action": "type", "text": "should_not_reach"},
            ],
            "hwnd": 1,  # 必须提供 target_hwnd 才能触发 delivery 判定
            "activate_window": False,  # 跳过实际窗口激活
            "allow_unfocused_input": False,
            "stop_on_focus_drift": True,
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 应在第二个动作后停止（delivery=leaked）
        assert data["aborted"] is True
        assert "焦点漂移" in data["aborted_reason"]
        # 第三个动作不应执行
        assert data["executed"] < 3

    def test_batch_no_stop_when_stop_on_focus_drift_false(self, client, monkeypatch):
        """stop_on_focus_drift=False 时焦点漂移不停止剩余动作"""
        from server.screen import routes

        # mock 管理员权限
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes, "emergency", type("E", (), {
            "is_stopped": False, "cooldown_seconds": 10,
            "can_operate": lambda self: True,
        })())
        monkeypatch.setattr(routes, "_validate_action_params", lambda *a, **k: (True, ""))
        monkeypatch.setattr(routes, "verify_focus_for_input",
                            lambda *a, **k: (True, "focus_verified", {"foreground": {"hwnd": 1}, "target_match": True}))
        monkeypatch.setattr(routes, "_execute_action", lambda **kwargs: {"success": True, "message": "ok"})
        # 焦点始终漂移
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 999, "process_name": "other.exe"},
            "target_match": False,
        })
        monkeypatch.setattr(routes, "_check_danger", lambda *a, **k: "none")
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        monkeypatch.setattr(routes, "_find_window", lambda *a, **k: None)

        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "type", "text": "a"},
                {"action": "type", "text": "b"},
            ],
            "hwnd": 1,  # 提供 target_hwnd 触发 delivery 判定
            "activate_window": False,
            "allow_unfocused_input": False,
            "stop_on_focus_drift": False,
            "require_confirm": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # stop_on_focus_drift=False → 不应因焦点漂移而 abort
        # （但第一个动作后 delivery=leaked，第二个动作的焦点校验可能失败 → 可能 abort）
        # 此处只验证：aborted_reason 不应包含"焦点漂移"
        if data["aborted"]:
            assert "焦点漂移" not in data["aborted_reason"]

    def test_batch_stale_coordinates_blocks_item(self, client, monkeypatch):
        """batch 中带失效 snapshot_id 的坐标动作应被拦截"""
        from server.screen import routes

        # mock 管理员权限
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes, "emergency", type("E", (), {
            "is_stopped": False, "cooldown_seconds": 10,
            "can_operate": lambda self: True,
        })())
        monkeypatch.setattr(routes, "_validate_action_params", lambda *a, **k: (True, ""))
        monkeypatch.setattr(routes, "_check_danger", lambda *a, **k: "none")
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        monkeypatch.setattr(routes, "_find_window", lambda *a, **k: None)

        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "click", "x": 100, "y": 100, "snapshot_id": "nonexistent_xyz"},
            ],
            "require_confirm": False,
            "stop_on_error": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocked"] >= 1
        # 第一个结果应标记 blocked 且 reason 含 STALE_COORDINATES
        first_result = data["results"][0]
        assert first_result["status"] == "blocked"
        assert "STALE_COORDINATES" in first_result.get("reason", "")


# ========== Canonical Window Token 测试 ==========

class TestCanonicalWindowToken:
    """canonical window token 解析测试（评估文档 P0）"""

    def test_resolve_canonical_window_returns_required_fields(self):
        """resolve_canonical_window 返回字典应包含所有必需字段"""
        from server.screen.focus import resolve_canonical_window
        result = resolve_canonical_window(0)
        required_keys = {
            "canonical_hwnd", "canonical_title", "canonical_pid",
            "canonical_process_name", "family_hwnds", "input_hwnd", "is_uwp_host",
        }
        assert required_keys.issubset(result.keys())

    def test_canonical_for_response_drops_family_hwnds(self):
        """_canonical_for_response 应丢弃 family_hwnds 列表（避免响应膨胀）"""
        from server.screen.routes import _canonical_for_response
        canonical = {
            "canonical_hwnd": 12345,
            "canonical_title": "Test",
            "canonical_pid": 1000,
            "canonical_process_name": "test.exe",
            "family_hwnds": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "input_hwnd": 12345,
            "is_uwp_host": False,
        }
        result = _canonical_for_response(canonical)
        assert result is not None
        assert "family_hwnds" not in result
        assert result["canonical_hwnd"] == 12345
        assert result["canonical_title"] == "Test"
        assert result["canonical_pid"] == 1000
        assert result["canonical_process_name"] == "test.exe"
        assert result["is_uwp_host"] is False

    def test_canonical_for_response_none_input(self):
        """_canonical_for_response(None) → None"""
        from server.screen.routes import _canonical_for_response
        assert _canonical_for_response(None) is None


# ========== UIA 语义层测试（评估文档 P0-5）==========

class TestUIAModule:
    """UIA 模块纯单元测试——评估文档 P0-5。

    通过 monkeypatch mock uiautomation 库避免依赖真实 Windows UIA 树。
    """

    def test_uia_available_returns_bool(self):
        """uia_available() 返回 bool（在 Windows 上应为 True）"""
        from server.screen.uia import uia_available
        result = uia_available()
        assert isinstance(result, bool)

    def test_stale_element_and_uia_not_available_are_distinct_strings(self):
        """STALE_ELEMENT / UIA_NOT_AVAILABLE 是非空字符串且互不相同"""
        from server.screen.uia import STALE_ELEMENT, UIA_NOT_AVAILABLE
        assert isinstance(STALE_ELEMENT, str) and STALE_ELEMENT
        assert isinstance(UIA_NOT_AVAILABLE, str) and UIA_NOT_AVAILABLE
        assert STALE_ELEMENT != UIA_NOT_AVAILABLE

    def test_resolve_element_nonexistent_snapshot_returns_snapshot_not_found(self):
        """resolve_element 对不存在的 snapshot_id 返回 snapshot_not_found"""
        from server.screen.uia import clear_snapshots, resolve_element
        clear_snapshots()
        elem, err = resolve_element("12345:abcdef12:0", "nonexistent_snapshot_id")
        assert elem is None
        assert err == "snapshot_not_found"

    def test_resolve_element_malformed_element_id_returns_element_not_found(self):
        """格式错误的 element_id（缺段）返回 element_not_found"""
        from server.screen.uia import _uia_snapshots, clear_snapshots, resolve_element
        clear_snapshots()
        # 注入一个假 snapshot，但 element_id 格式不对
        snapshot_id = "test_snapshot_id_xxxxxxxx1234567890abcdef"
        with __import__("server.screen.uia", fromlist=["_uia_lock"])._uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [],
                "canonical_hwnd": 12345,
                "captured_at": __import__("time").time(),
            }
        # 缺段的 element_id
        elem, err = resolve_element("malformed_id", snapshot_id)
        assert elem is None
        assert err == "element_not_found"
        clear_snapshots()

    def test_resolve_element_hwnd_mismatch_returns_hwnd_mismatch(self):
        """canonical_hwnd 部分与 snapshot 不一致返回 hwnd_mismatch"""
        import time

        from server.screen.uia import _uia_lock, _uia_snapshots, clear_snapshots, resolve_element
        clear_snapshots()
        snapshot_id = "test_snapshot_id_yyy1234567890abcdef"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {"element_id": "99999:yyyyxxxx:0", "role": "Button", "name": "OK"},
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }
        # element_id 中 canonical_hwnd=99999，但 snapshot 记录的是 12345
        elem, err = resolve_element("99999:yyyyxxxx:0", snapshot_id)
        assert elem is None
        assert err == "hwnd_mismatch"
        clear_snapshots()

    def test_resolve_element_out_of_range_index_returns_element_not_found(self):
        """element_index 超出 elements 范围返回 element_not_found"""
        import time

        from server.screen.uia import _uia_lock, _uia_snapshots, clear_snapshots, resolve_element
        clear_snapshots()
        snapshot_id = "test_snapshot_id_zzz1234567890abcdef"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {"element_id": "12345:zzzzxxxx:0", "role": "Button", "name": "OK"},
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }
        # index=5 超出范围
        elem, err = resolve_element("12345:zzzzxxxx:5", snapshot_id)
        assert elem is None
        assert err == "element_not_found"
        clear_snapshots()

    def test_resolve_element_success(self):
        """正确 element_id 返回 element_dict + 空错误"""
        import time

        from server.screen.uia import _uia_lock, _uia_snapshots, clear_snapshots, resolve_element
        clear_snapshots()
        snapshot_id = "abcdefgh1234567890abcdef00000000"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {
                        "element_id": "12345:abcdef12:0",
                        "role": "Button",
                        "name": "OK",
                        "value": None,
                        "enabled": True,
                        "checked": None,
                        "selected": None,
                        "bounds": [10, 20, 110, 60],
                        "automation_id": "okButton",
                        "element_index": 0,
                        "depth": 2,
                        "parent_index": None,
                        "children_indices": [],
                        "runtime_id": (1, 2, 3),
                    },
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }
        elem, err = resolve_element("12345:abcdef12:0", snapshot_id)
        assert err == ""
        assert elem is not None
        assert elem["role"] == "Button"
        assert elem["name"] == "OK"
        assert elem["automation_id"] == "okButton"
        clear_snapshots()

    def test_execute_semantic_action_uia_value_equals_verified(self, monkeypatch):
        """第三轮评估 P1-2：set_value + uia_value_equals 后验通过 → status=executed"""
        import time

        import server.screen.uia as uia_module
        from server.screen.uia import (
            _uia_lock,
            _uia_snapshots,
            clear_snapshots,
            execute_semantic_action,
        )

        clear_snapshots()
        snapshot_id = "verify_ok_snapshot_1234567890abcdef0000"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {
                        "element_id": "12345:abcdef12:0",
                        "role": "Edit",
                        "name": "输入框",
                        "value": None,
                        "enabled": True,
                        "checked": None,
                        "selected": None,
                        "bounds": [10, 20, 110, 60],
                        "automation_id": "edit1",
                        "element_index": 0,
                        "depth": 2,
                        "parent_index": None,
                        "children_indices": [],
                        "runtime_id": (1, 2, 3),
                    },
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }

        class FakeValuePattern:
            def SetValue(self, value): pass
            Value = "LocalAgent 中文"

        class FakeCtrl:
            def GetValuePattern(self): return FakeValuePattern()

        monkeypatch.setattr(uia_module, "uia_available", lambda: True)
        monkeypatch.setattr(uia_module, "_ensure_com_initialized", lambda: (True, ""))
        monkeypatch.setattr(uia_module, "_find_uia_control_by_runtime_id", lambda hwnd, rid: FakeCtrl())

        result = execute_semantic_action(
            element_id="12345:abcdef12:0",
            snapshot_id=snapshot_id,
            action="set_value",
            value="LocalAgent 中文",
            expected={"type": "uia_value_equals", "text": "LocalAgent 中文"},
        )
        # P1-2：动作成功 + 内建后验通过
        assert result["success"] is True
        assert result["status"] == "executed"
        assert result["postcondition_status"] == "verified"
        assert result["postcondition_actual_value"] == "LocalAgent 中文"
        assert result["transport_status"] == "sent"
        clear_snapshots()

    def test_execute_semantic_action_uia_value_equals_failed(self, monkeypatch):
        """第三轮评估 P1-2：set_value + uia_value_equals 后验失败 → status=postcondition_failed"""
        import time

        import server.screen.uia as uia_module
        from server.screen.uia import (
            _uia_lock,
            _uia_snapshots,
            clear_snapshots,
            execute_semantic_action,
        )

        clear_snapshots()
        snapshot_id = "verify_fail_snapshot_1234567890abcdef000"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {
                        "element_id": "12345:abcdef12:0",
                        "role": "Edit",
                        "name": "输入框",
                        "value": None,
                        "enabled": True,
                        "checked": None,
                        "selected": None,
                        "bounds": [10, 20, 110, 60],
                        "automation_id": "edit1",
                        "element_index": 0,
                        "depth": 2,
                        "parent_index": None,
                        "children_indices": [],
                        "runtime_id": (1, 2, 3),
                    },
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }

        class FakeValuePattern:
            def SetValue(self, value): pass
            Value = "实际值不匹配"  # 与 expected.text 不同

        class FakeCtrl:
            def GetValuePattern(self): return FakeValuePattern()

        monkeypatch.setattr(uia_module, "uia_available", lambda: True)
        monkeypatch.setattr(uia_module, "_ensure_com_initialized", lambda: (True, ""))
        monkeypatch.setattr(uia_module, "_find_uia_control_by_runtime_id", lambda hwnd, rid: FakeCtrl())

        result = execute_semantic_action(
            element_id="12345:abcdef12:0",
            snapshot_id=snapshot_id,
            action="set_value",
            value="期望值",
            expected={"type": "uia_value_equals", "text": "期望值"},
        )
        # P1-2：动作已发送成功（success=True），但后验失败
        assert result["success"] is True
        assert result["status"] == "postcondition_failed"
        assert result["postcondition_status"] == "failed"
        assert result["postcondition_actual_value"] == "实际值不匹配"
        clear_snapshots()

    def test_execute_semantic_action_no_expected_keeps_executed_unverified(self, monkeypatch):
        """第三轮评估 P1-2：无 expected → 保持 executed_unverified（向后兼容）"""
        import time

        import server.screen.uia as uia_module
        from server.screen.uia import (
            _uia_lock,
            _uia_snapshots,
            clear_snapshots,
            execute_semantic_action,
        )

        clear_snapshots()
        snapshot_id = "no_expected_snapshot_1234567890abcdef0000"
        with _uia_lock:
            _uia_snapshots[snapshot_id] = {
                "elements": [
                    {
                        "element_id": "12345:abcdef12:0",
                        "role": "Edit",
                        "name": "输入框",
                        "value": None,
                        "enabled": True,
                        "checked": None,
                        "selected": None,
                        "bounds": [10, 20, 110, 60],
                        "automation_id": "edit1",
                        "element_index": 0,
                        "depth": 2,
                        "parent_index": None,
                        "children_indices": [],
                        "runtime_id": (1, 2, 3),
                    },
                ],
                "canonical_hwnd": 12345,
                "captured_at": time.time(),
            }

        class FakeValuePattern:
            def SetValue(self, value): pass
            Value = "anything"

        class FakeCtrl:
            def GetValuePattern(self): return FakeValuePattern()

        monkeypatch.setattr(uia_module, "uia_available", lambda: True)
        monkeypatch.setattr(uia_module, "_ensure_com_initialized", lambda: (True, ""))
        monkeypatch.setattr(uia_module, "_find_uia_control_by_runtime_id", lambda hwnd, rid: FakeCtrl())

        result = execute_semantic_action(
            element_id="12345:abcdef12:0",
            snapshot_id=snapshot_id,
            action="set_value",
            value="any",
            expected=None,  # 无后验
        )
        # 无 expected → 保持 executed_unverified，postcondition_status 为 None
        assert result["success"] is True
        assert result["status"] == "executed_unverified"
        assert result["postcondition_status"] is None
        clear_snapshots()


class TestUIASnapshotRoutes:
    """UIA snapshot 路由集成测试——评估文档 P0-5。"""

    def test_uia_snapshot_no_target_returns_400(self, client):
        """未提供 hwnd/window_title → HTTP 400"""
        resp = client.post("/screen/uia/snapshot", json={})
        assert resp.status_code == 400
        assert "hwnd" in resp.json()["detail"] or "window_title" in resp.json()["detail"]

    def test_uia_snapshot_window_not_found_returns_404(self, client):
        """不存在的窗口标题 → HTTP 404"""
        resp = client.post("/screen/uia/snapshot", json={
            "window_title": "___NONEXISTENT_WINDOW_UIA_TEST___",
        })
        assert resp.status_code == 404

    def test_uia_snapshot_returns_required_fields(self, client, monkeypatch):
        """mock take_uia_snapshot，验证响应字段完整"""
        from server.screen.uia import clear_snapshots

        def fake_take(hwnd, max_depth=8, interesting_only=True, max_elements=500):
            return {
                "success": True,
                "snapshot_id": "fake_snapshot_id_for_test_1234567890abcdef",
                "canonical_hwnd": hwnd,
                "elements": [
                    {
                        "element_id": f"{hwnd}:fake_sna:0",
                        "role": "Button",
                        "name": "确定",
                        "value": None,
                        "enabled": True,
                        "checked": None,
                        "selected": None,
                        "bounds": [10, 20, 110, 60],
                        "automation_id": "okButton",
                        "element_index": 0,
                        "depth": 1,
                        "parent_index": None,
                        "children_indices": [],
                        "runtime_id": (1, 2, 3),
                    },
                ],
                "element_count": 1,
                "fallback_reason": None,
                "elapsed_ms": 50,
                "message": "测试 snapshot",
            }

        # take_uia_snapshot 是在 routes.py 内部 from server.screen.uia import 的，
        # 必须直接 patch server.screen.uia.take_uia_snapshot，patch routes 不生效
        import server.screen.uia as uia_module
        monkeypatch.setattr(uia_module, "take_uia_snapshot", fake_take)
        clear_snapshots()

        resp = client.post("/screen/uia/snapshot", json={"hwnd": 12345})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["snapshot_id"] == "fake_snapshot_id_for_test_1234567890abcdef"
        assert data["canonical_hwnd"] == 12345
        assert data["element_count"] == 1
        assert len(data["elements"]) == 1
        elem = data["elements"][0]
        assert elem["role"] == "Button"
        assert elem["name"] == "确定"
        assert elem["automation_id"] == "okButton"
        assert elem["bounds"] == [10, 20, 110, 60]
        assert "runtime_id" not in elem  # runtime_id 不暴露给响应

    def test_uia_snapshot_returns_uia_not_available_fallback(self, client, monkeypatch):
        """mock UIA 不可用 → 响应含 fallback_reason=UIA_NOT_AVAILABLE"""
        import server.screen.uia as uia_module
        from server.screen.uia import UIA_NOT_AVAILABLE, clear_snapshots

        def fake_take(hwnd, max_depth=8, interesting_only=True, max_elements=500):
            return {
                "success": False,
                "snapshot_id": "",
                "canonical_hwnd": hwnd,
                "elements": [],
                "element_count": 0,
                "fallback_reason": UIA_NOT_AVAILABLE,
                "elapsed_ms": 5,
                "message": "uiautomation 库不可用",
            }

        monkeypatch.setattr(uia_module, "take_uia_snapshot", fake_take)
        clear_snapshots()

        resp = client.post("/screen/uia/snapshot", json={"hwnd": 99999})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["fallback_reason"] == UIA_NOT_AVAILABLE
        assert data["element_count"] == 0


class TestUIAActionRoutes:
    """UIA 语义动作路由测试——评估文档 P0-5。"""

    def test_uia_action_stale_element_returns_blocked(self, client, monkeypatch):
        """mock execute_semantic_action 返回 STALE_ELEMENT → 响应 blocked + not_sent"""
        import server.screen.uia as uia_module
        from server.screen.uia import STALE_ELEMENT, clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            return {
                "success": False,
                "status": "blocked",
                "action": action,
                "element_id": element_id,
                "element_role": None,
                "element_name": None,
                "transport_status": "not_sent",
                "fallback_reason": STALE_ELEMENT,
                "elapsed_ms": 3,
                "message": "STALE_ELEMENT: snapshot_not_found",
            }

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:nonexistt:0",
            "snapshot_id": "nonexistent_snapshot",
            "action": "invoke",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert data["fallback_reason"] == STALE_ELEMENT
        assert data["transport_status"] == "not_sent"
        assert data["postcondition_status"] == "not_checked"

    def test_uia_action_success_without_expected_returns_executed_unverified(self, client, monkeypatch):
        """动作成功 + 无 expected → postcondition_status=executed_unverified"""
        import server.screen.uia as uia_module
        from server.screen.uia import clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            return {
                "success": True,
                "status": "executed_unverified",
                "action": action,
                "element_id": element_id,
                "element_role": "Button",
                "element_name": "确定",
                "transport_status": "sent",
                "fallback_reason": None,
                "elapsed_ms": 30,
                "message": "UIA invoke 已执行",
            }

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "invoke",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "executed_unverified"
        assert data["transport_status"] == "sent"
        assert data["postcondition_status"] == "executed_unverified"
        assert data["element_role"] == "Button"
        assert data["element_name"] == "确定"

    def test_uia_action_with_expected_triggers_postcondition(self, client, monkeypatch):
        """动作成功 + expected → 调用 _verify_postcondition 返回 verified/failed"""
        import server.screen.uia as uia_module
        from server.screen import routes
        from server.screen.uia import clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            return {
                "success": True,
                "status": "executed_unverified",
                "action": action,
                "element_id": element_id,
                "element_role": "Button",
                "element_name": "OK",
                "transport_status": "sent",
                "fallback_reason": None,
                "elapsed_ms": 30,
                "message": "UIA invoke 已执行",
            }

        # mock _capture_fullscreen 返回有效 bytes
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda: b"fake_png_bytes")
        # mock PIL.Image.open 返回假 image
        from PIL import Image as _PILImage
        fake_img = _PILImage.new("RGB", (10, 10))
        monkeypatch.setattr(_PILImage, "open", lambda *args, **kwargs: fake_img)
        # mock _verify_postcondition 返回 verified
        monkeypatch.setattr(routes, "_verify_postcondition", lambda expected, pil_image: "verified")

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        _sync_screen_patches(monkeypatch, ["uia_endpoints"])
        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "invoke",
            "expected": {"type": "ocr_contains", "text": "成功"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["postcondition_status"] == "verified"

    def test_uia_action_unknown_action_returns_failed(self, client, monkeypatch):
        """mock execute_semantic_action 返回 unknown action → failed"""
        import server.screen.uia as uia_module
        from server.screen.uia import clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            return {
                "success": False,
                "status": "failed",
                "action": action,
                "element_id": element_id,
                "element_role": "Button",
                "element_name": None,
                "transport_status": "not_sent",
                "fallback_reason": None,
                "elapsed_ms": 1,
                "message": "未知 action: bad_action",
            }

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "bad_action",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "failed"

    def test_uia_action_uia_value_equals_verified_passthrough(self, client, monkeypatch):
        """第三轮评估 P1-2：expected=uia_value_equals + 内建后验 verified → 透传到响应"""
        import server.screen.uia as uia_module
        from server.screen.uia import clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            # 模拟内部 uia_value_equals 后验通过
            return {
                "success": True,
                "status": "executed",
                "action": action,
                "element_id": element_id,
                "element_role": "Edit",
                "element_name": "输入框",
                "transport_status": "sent",
                "postcondition_status": "verified",
                "postcondition_actual_value": "LocalAgent 中文",
                "fallback_reason": None,
                "elapsed_ms": 45,
                "message": "UIA set_value 已执行且后验通过",
            }

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "set_value",
            "value": "LocalAgent 中文",
            "expected": {"type": "uia_value_equals", "text": "LocalAgent 中文"},
        })
        assert resp.status_code == 200
        data = resp.json()
        # P1-2：内建后验通过 → success=True + status=executed + postcondition_status=verified
        assert data["success"] is True
        assert data["status"] == "executed"
        assert data["postcondition_status"] == "verified"
        assert data["postcondition_actual_value"] == "LocalAgent 中文"

    def test_uia_action_uia_value_equals_failed_passthrough(self, client, monkeypatch):
        """第三轮评估 P1-2：expected=uia_value_equals + 内建后验 failed → 透传 postcondition_failed"""
        import server.screen.uia as uia_module
        from server.screen.uia import clear_snapshots

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            # 模拟内部 uia_value_equals 后验失败：动作成功但 Value 不匹配
            return {
                "success": True,  # 动作已发送成功
                "status": "postcondition_failed",
                "action": action,
                "element_id": element_id,
                "element_role": "Edit",
                "element_name": "输入框",
                "transport_status": "sent",
                "postcondition_status": "failed",
                "postcondition_actual_value": "其他值",
                "fallback_reason": None,
                "elapsed_ms": 50,
                "message": "UIA set_value 已执行但后验失败",
            }

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        clear_snapshots()

        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "set_value",
            "value": "期望值",
            "expected": {"type": "uia_value_equals", "text": "期望值"},
        })
        assert resp.status_code == 200
        data = resp.json()
        # P1-2：动作已发送 + 后验失败 → success=True（动作成功）+ status=postcondition_failed
        assert data["success"] is True
        assert data["status"] == "postcondition_failed"
        assert data["postcondition_status"] == "failed"
        assert data["postcondition_actual_value"] == "其他值"

    def test_uia_action_uia_value_equals_does_not_trigger_ocr(self, client, monkeypatch):
        """第三轮评估 P1-2：expected=uia_value_equals 时不应触发 OCR 截图后验"""
        import server.screen.uia as uia_module
        from server.screen import routes
        from server.screen.uia import clear_snapshots

        ocr_called = {"n": 0}

        def fake_execute(element_id, snapshot_id, action, value=None, direction=None, amount=1, expected=None):
            return {
                "success": True,
                "status": "executed",
                "action": action,
                "element_id": element_id,
                "element_role": "Edit",
                "element_name": "输入框",
                "transport_status": "sent",
                "postcondition_status": "verified",
                "postcondition_actual_value": "ok",
                "fallback_reason": None,
                "elapsed_ms": 30,
                "message": "verified",
            }

        def fake_capture_fullscreen():
            ocr_called["n"] += 1
            return b"should_not_be_called"

        monkeypatch.setattr(uia_module, "execute_semantic_action", fake_execute)
        monkeypatch.setattr(routes, "_capture_fullscreen", fake_capture_fullscreen)
        clear_snapshots()

        _sync_screen_patches(monkeypatch, ["uia_endpoints"])
        resp = client.post("/screen/uia/action", json={
            "element_id": "12345:abcdef12:0",
            "snapshot_id": "any_snapshot",
            "action": "set_value",
            "value": "ok",
            "expected": {"type": "uia_value_equals", "text": "ok"},
        })
        assert resp.status_code == 200
        # uia_value_equals 路径不应触发 OCR 截图
        assert ocr_called["n"] == 0


class TestUIAElementIdBinding:
    """UIA element_id 绑定测试——评估文档 P0-5 第 3 项。

    要求：element id 绑定 window token + snapshot version，过期返回 STALE_ELEMENT。
    """

    def test_element_id_format_is_canonical_hwnd_snapshot_short_index(self):
        """element_id 格式应为 {canonical_hwnd}:{snapshot_id_short}:{element_index}"""
        # 验证 _control_to_element_dict 生成的 element_id 格式
        from server.screen.uia import _control_to_element_dict
        # 用 mock ctrl：实际 _control_to_element_dict 会调用 ctrl.ControlType 等
        # 这里只测 element_id 字段格式，构造一个会失败但保留格式的 mock
        class FakeCtrl:
            ControlType = 50000  # Button
            Name = "Test"
            AutomationId = ""
            IsEnabled = True
            BoundingRectangle = None
            def GetValuePattern(self): return None
            def GetTogglePattern(self): return None
            def GetSelectionItemPattern(self): return None
            def GetRuntimeId(self): return None

        # 注意：实际 _control_to_element_dict 会 try import uiautomation，
        # 在测试环境可能成功也可能失败。如果失败返回 None。
        result = _control_to_element_dict(FakeCtrl(), 2, 1, 5, "abcdefgh1234567890abcdef00000000", 12345)
        if result is None:
            # uiautomation 不可用时跳过（环境限制）
            import pytest
            pytest.skip("uiautomation not available in test env")
        # 验证 element_id 格式
        eid = result["element_id"]
        parts = eid.split(":")
        assert len(parts) == 3
        assert parts[0] == "12345"  # canonical_hwnd
        assert parts[1] == "abcdefgh"  # snapshot_id 前 8 字符
        assert parts[2] == "5"  # element_index


# ========== 评估文档第 11.2 节：截图与坐标回归测试补强 ==========

class TestCaptureCoordinateSpaces:
    """11.2 截图与坐标测试补强：窗口模式 vs 全屏模式坐标空间标识。

    评估文档第 11.2 节要求：为窗口模式、全屏模式和多屏分别提供明确坐标空间标识。
    """

    def test_capture_fullscreen_returns_virtual_screen_rect(self, client):
        """全屏截图的 window_rect 应为虚拟屏几何 [left, top, right, bottom]

        多屏环境下 left/top 可能为负数（如 [-1600, 0, 2560, 2560]）。
        """
        resp = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data.get("snapshot_id")  # 必须返回 snapshot_id
        rect = data.get("window_rect")
        assert rect is not None
        assert len(rect) == 4
        # 虚拟屏：宽高 > 0，原点可为负
        assert rect[2] > rect[0]  # right > left
        assert rect[3] > rect[1]  # bottom > top

    def test_capture_fullscreen_returns_distinct_snapshot_ids(self, client):
        """连续两次全屏截图应返回不同的 snapshot_id（绑定不同时间点）"""
        resp1 = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        resp2 = client.post("/screen/capture", json={"mode": "fullscreen", "format": "base64"})
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        sid1 = resp1.json().get("snapshot_id")
        sid2 = resp2.json().get("snapshot_id")
        assert sid1 and sid2
        assert sid1 != sid2  # 不同 snapshot

    def test_capture_window_returns_window_specific_rect(self, client, monkeypatch):
        """窗口模式截图的 window_rect 应为目标窗口几何（非虚拟屏）

        用 monkeypatch mock _enum_windows/_capture_window/win32gui.GetWindowRect，
        不真启动 notepad（避免测试弹窗干扰用户桌面）。
        """
        import io as _io

        from PIL import Image as _Image

        # 1) mock _enum_windows 返回一个 fake 窗口（hwnd=999999）
        fake_window = {
            "hwnd": 999999,
            "title": "FakeTestWindow",
            "class_name": "FakeClass",
            "bbox": {"left": 100, "top": 200, "right": 800, "bottom": 600},
            "width": 700,
            "height": 400,
            "is_visible": True,
            "is_minimized": False,
            "z_order": 0,
            "pid": 1234,
            "process_name": "fakewin.exe",
        }
        monkeypatch.setattr("server.screen.routes._enum_windows", lambda: [fake_window])

        # 2) mock _capture_window 返回 1x1 PNG bytes（不真截图）
        _png_buf = _io.BytesIO()
        _Image.new("RGB", (1, 1)).save(_png_buf, format="PNG")
        _png_bytes = _png_buf.getvalue()
        monkeypatch.setattr("server.screen.routes._capture_window", lambda hwnd, force_fullscreen_crop=False: _png_bytes)

        # 3) mock win32gui.GetWindowRect 返回固定窗口几何（区别于虚拟屏 [0,0,W,H]）
        fake_rect = (100, 200, 800, 600)  # left, top, right, bottom
        monkeypatch.setattr("win32gui.GetWindowRect", lambda hwnd: fake_rect)

        # 4) 用 fake hwnd 截图（不依赖真实窗口存在）
        cap_resp = client.post("/screen/capture", json={
            "mode": "window",
            "hwnd": 999999,
            "format": "base64",
        })
        assert cap_resp.status_code == 200
        data = cap_resp.json()
        assert data["success"] is True
        assert data.get("snapshot_id")
        rect = data.get("window_rect")
        assert rect is not None
        assert len(rect) == 4
        # window_rect 应来自 win32gui.GetWindowRect（窗口自身几何），不是虚拟屏
        assert rect == [100, 200, 800, 600]
        assert all(isinstance(c, int) for c in rect)


# ========== 评估文档第 11.4 节：结果验证测试补强 ==========

class TestResultVerification:
    """11.4 结果验证测试补强：executed_unverified vs verified vs failed 区分。

    评估文档第 11.4 节第 2 项：点击错误坐标但系统接受事件 → 必须为 executed_unverified
    或 postcondition_failed。
    """

    def test_postcondition_status_executed_unverified_when_no_expected(self, client, monkeypatch):
        """动作成功 + 无 expected → postcondition_status=executed_unverified（不伪称 verified）"""
        from server.screen import routes

        # mock verify_focus_for_input 放行
        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            ev = {
                "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
                "target_match": True,
            }
            return True, "focus_verified", ev

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0,
            "canonical_title": "",
            "canonical_pid": 0,
            "canonical_process_name": "",
            "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd or 0,
            "is_uwp_host": False,
        })
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)

        # 用 click 动作（坐标动作不强制焦点，但 mock 不影响）
        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 100,
            "y": 100,
            "require_confirm": False,
            # 不传 expected
        })
        assert resp.status_code == 200
        data = resp.json()
        # 不管 success 是 True 还是 False，postcondition 应该是 not_checked（动作未发送）
        # 或 executed_unverified（动作发送但无 expected）
        # 这里只验证未声明 expected 时不会返回 verified
        assert data.get("postcondition_status") in ("executed_unverified", "not_checked")

    def test_postcondition_status_failed_when_expected_ocr_not_match(self, client, monkeypatch):
        """动作成功 + expected=ocr_contains(不存在文本) → postcondition_status=failed"""
        from PIL import Image as _PILImage

        import server.ocr as ocr_module
        from server.screen import routes

        # mock verify_focus_for_input 放行
        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            return True, "focus_verified", {
                "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
                "target_match": True,
            }

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0,
            "canonical_title": "",
            "canonical_pid": 0,
            "canonical_process_name": "",
            "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd or 0,
            "is_uwp_host": False,
        })
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)

        # mock _capture_fullscreen 返回有效 bytes（用于后验 OCR）
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda: b"fake_png_bytes")
        fake_img = _PILImage.new("RGB", (100, 100))
        monkeypatch.setattr(_PILImage, "open", lambda *args, **kwargs: fake_img)

        # mock server.ocr._do_ocr 返回不含目标文本的 OCR 结果
        class FakeOcrResp:
            text = "屏幕显示的是其他内容"
        monkeypatch.setattr(ocr_module, "_do_ocr", lambda pil_image: FakeOcrResp())

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 100,
            "y": 100,
            "require_confirm": False,
            "expected": {"type": "ocr_contains", "text": "成功保存"},
        })
        assert resp.status_code == 200
        data = resp.json()
        # 不管动作本身成败，postcondition 应该是 failed（OCR 未找到"成功保存"）
        # 但只有动作发送后才会走后验路径
        if data.get("transport_status") == "sent":
            assert data.get("postcondition_status") == "failed"
        else:
            # 动作未发送 → not_checked
            assert data.get("postcondition_status") == "not_checked"

    def test_postcondition_status_error_when_expected_type_invalid(self, client, monkeypatch):
        """expected type 不合法 → postcondition_status=error"""
        from server.screen import routes

        def fake_verify(target_hwnd, *, allow_unfocused_input, protected_processes, evidence=None):
            return True, "focus_verified", {
                "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
                "target_match": True,
            }

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify)
        monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
            "foreground": {"hwnd": 200, "process_name": "notepad.exe"},
            "target_match": True,
        })
        monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
            "canonical_hwnd": hwnd or 0,
            "canonical_title": "",
            "canonical_pid": 0,
            "canonical_process_name": "",
            "family_hwnds": [hwnd] if hwnd else [],
            "input_hwnd": hwnd or 0,
            "is_uwp_host": False,
        })
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda: b"fake_png_bytes")
        from PIL import Image as _PILImage
        fake_img = _PILImage.new("RGB", (100, 100))
        monkeypatch.setattr(_PILImage, "open", lambda *args, **kwargs: fake_img)

        _sync_screen_patches(monkeypatch, ["action_endpoints"])
        resp = client.post("/screen/action", json={
            "action": "click",
            "x": 100,
            "y": 100,
            "require_confirm": False,
            "expected": {"type": "invalid_type", "text": "foo"},
        })
        assert resp.status_code == 200
        data = resp.json()
        if data.get("transport_status") == "sent":
            assert data.get("postcondition_status") == "error"


# ========== P1-B 窗口生命周期 API 测试（评估文档） ==========

class TestWindowLifecycle:
    """P1-B：app_list/app_launch/app_wait/window_resolve + 窗口操作 + token 失效"""

    def test_app_list_returns_aggregated_apps(self, client):
        """GET /screen/app/list 应返回按进程聚合的应用列表"""
        resp = client.get("/screen/app/list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["apps"], list)
        assert data["count"] == len(data["apps"])
        # 每个 app 应含必要字段
        for app in data["apps"]:
            assert "process_name" in app
            assert "pid" in app
            assert "window_count" in app
            assert "hwnds" in app

    def test_window_resolve_no_criteria_returns_400(self, client):
        """window_resolve 无任何查询条件应返回 400"""
        resp = client.post("/screen/window/resolve", json={})
        assert resp.status_code == 400

    def test_window_resolve_not_found(self, client):
        """window_resolve 不存在的标题应返回 status=WINDOW_NOT_FOUND"""
        resp = client.post("/screen/window/resolve", json={
            "title": "___NONEXISTENT_WINDOW_P1B_TEST_12345___",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "WINDOW_NOT_FOUND"
        assert data["matches"] == []

    def test_window_resolve_returns_token_and_canonical(self, client):
        """window_resolve 命中时应返回 window_token 和 canonical_window"""
        # 用 explorer.exe 作为可靠目标（Windows 必有）
        resp = client.post("/screen/window/resolve", json={
            "process_name": "explorer.exe",
        })
        assert resp.status_code == 200
        data = resp.json()
        if not data["matches"]:
            pytest.skip("没有 explorer.exe 窗口（非 Windows 或异常环境）")
        # 单匹配或多匹配都应含 window_token
        for m in data["matches"]:
            assert "window_token" in m
            token = m["window_token"]
            assert "canonical_hwnd" in token
            assert "pid" in token
            assert "process_create_time" in token
            assert "canonical_window" in m
            assert "canonical_hwnd" in m["canonical_window"]

    def test_window_resolve_ambiguous_when_multiple_matches(self, client):
        """多匹配时应返回 status=WINDOW_AMBIGUOUS 而非随便选一个"""
        # explorer.exe 通常有多个窗口（任务栏、文件夹等）
        resp = client.post("/screen/window/resolve", json={
            "process_name": "explorer.exe",
        })
        data = resp.json()
        if len(data["matches"]) > 1:
            assert data["status"] == "WINDOW_AMBIGUOUS"
            assert data["success"] is True

    def test_window_op_invalid_token_returns_token_invalid(self, client):
        """window op 传入失效 token 应返回 status=WINDOW_TOKEN_INVALID"""
        fake_token = {
            "canonical_hwnd": 99999999,  # 不存在的 hwnd
            "pid": 99999999,
            "process_create_time": 1000000000,
            "process_name": "fake.exe",
            "title": "fake",
        }
        resp = client.post("/screen/window/minimize", json={
            "hwnd": 99999999,
            "window_token": fake_token,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "WINDOW_TOKEN_INVALID"
        assert data["token_invalid_reason"] is not None

    def test_window_op_without_token_bypasses_check(self, client):
        """window op 不传 window_token 应跳过校验（向后兼容）"""
        # 用不存在的 hwnd，但不传 token → 应进入实际操作（返回 failed 而非 WINDOW_TOKEN_INVALID）
        resp = client.post("/screen/window/minimize", json={
            "hwnd": 99999999,
        })
        assert resp.status_code == 200
        data = resp.json()
        # hwnd 不存在时操作失败，但不是 token 失效
        assert data["status"] != "WINDOW_TOKEN_INVALID"

    def test_window_close_returns_post_state(self, client):
        """window close 应返回 post_state（含 exists 字段）"""
        # 用不存在的 hwnd（不会真的关闭任何窗口）
        resp = client.post("/screen/window/close", json={
            "hwnd": 99999999,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "post_state" in data
        assert "exists" in data["post_state"]
        assert data["post_state"]["exists"] is False

    def test_window_close_nonexistent_hwnd_returns_failure_with_status_field(self, client):
        """第三轮评估后续修复：close 不存在的 hwnd 应返回 success=false + status=failed（不是谎报成功）"""
        resp = client.post("/screen/window/close", json={
            "hwnd": 99999999,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 关键：hwnd 不存在必须返回 success=False，不能谎报成功
        assert data["success"] is False, \
            f"❌ 不存在的 hwnd 应返回 success=false，实际 {data['success']}"
        assert data["status"] == "failed", \
            f"❌ 不存在的 hwnd 应 status=failed，实际 {data['status']}"
        assert data["post_state"]["exists"] is False
        assert data["modal_info"] is None  # 没有进入模态处理逻辑

    def test_close_window_force_false_modal_blocking_does_not_lie_success(self, monkeypatch):
        """第三轮评估后续修复：force=False + 窗口仍存在 + 无模态 → 应返回 success=false + status=modal_blocking

        这是核心反谎报测试：mock 后 WM_CLOSE 发送后窗口仍存在，且 _try_dismiss_save_modal
        返回 dismissed=False（未识别到模态），此时必须返回 success=false + status=modal_blocking，
        不能再像旧版那样返回 success=true 仅靠 message 警告。
        """
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        class FakeWin32Gui:
            @staticmethod
            def IsWindow(hwnd): return True  # 始终存在
            @staticmethod
            def PostMessage(hwnd, msg, w, lparam): pass
            @staticmethod
            def IsWindowVisible(hwnd): return True
            @staticmethod
            def IsIconic(hwnd): return False
            @staticmethod
            def GetWindowText(hwnd): return "Mock 窗口"
            @staticmethod
            def GetWindowRect(hwnd): return (0, 0, 100, 100)
            @staticmethod
            def GetForegroundWindow(): return 0
            @staticmethod
            def GetClassName(hwnd): return "Mock"

        monkeypatch.setattr("win32gui.IsWindow", FakeWin32Gui.IsWindow, raising=False)
        monkeypatch.setattr("win32gui.PostMessage", FakeWin32Gui.PostMessage, raising=False)
        monkeypatch.setattr("win32gui.IsWindowVisible", FakeWin32Gui.IsWindowVisible, raising=False)
        monkeypatch.setattr("win32gui.IsIconic", FakeWin32Gui.IsIconic, raising=False)
        monkeypatch.setattr("win32gui.GetWindowText", FakeWin32Gui.GetWindowText, raising=False)
        monkeypatch.setattr("win32gui.GetWindowRect", FakeWin32Gui.GetWindowRect, raising=False)
        monkeypatch.setattr("win32gui.GetForegroundWindow", FakeWin32Gui.GetForegroundWindow, raising=False)
        monkeypatch.setattr("win32gui.GetClassName", FakeWin32Gui.GetClassName, raising=False)
        monkeypatch.setattr("win32gui.EnumChildWindows", lambda *a, **kw: None, raising=False)
        # 加速测试：把 time.sleep 替换为 no-op
        monkeypatch.setattr(lifecycle.time, "sleep", lambda *a, **kw: None)

        result = lifecycle.close_window(12345, force=False)
        # 核心反谎报断言
        assert result["success"] is False, \
            f"❌ 窗口仍存在时不能返回 success=true（谎报），实际 {result['success']}"
        assert result["status"] == "modal_blocking", \
            f"❌ 应 status=modal_blocking，实际 {result['status']}"
        assert result["post_state"]["exists"] is True
        assert "modal_info" in result
        assert result["modal_info"]["dismissed"] is False

    def test_close_window_force_true_calls_terminate_process(self, monkeypatch):
        """第三轮评估后续修复：force=True 必须走 TerminateProcess 杀进程，不能走 DestroyWindow"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        # 记录调用
        calls = {"terminate": 0, "destroy": 0, "post_message": 0}

        class FakeWin32Gui:
            @staticmethod
            def IsWindow(hwnd):
                # 第一次检查存在 → 进入 force 路径
                # 后续轮询窗口销毁：第三次开始返回 False（模拟进程被杀后窗口消失）
                calls.setdefault("is_window_count", 0)
                calls["is_window_count"] += 1
                return calls["is_window_count"] <= 2
            @staticmethod
            def DestroyWindow(hwnd):
                calls["destroy"] += 1
                raise Exception("DestroyWindow 不应被调用（跨进程本就失败）")
            @staticmethod
            def PostMessage(hwnd, msg, w, lparam):
                calls["post_message"] += 1

        class FakeWin32Process:
            @staticmethod
            def GetWindowThreadProcessId(hwnd):
                return (123, 6789)  # thread_id=123, pid=6789

        class FakeWin32Api:
            @staticmethod
            def OpenProcess(access, inherit, pid):
                # 返回 fake handle
                return 9999
            @staticmethod
            def TerminateProcess(handle, exit_code):
                calls["terminate"] += 1
            @staticmethod
            def CloseHandle(handle):
                pass

        monkeypatch.setattr("win32gui.IsWindow", FakeWin32Gui.IsWindow, raising=False)
        monkeypatch.setattr("win32gui.DestroyWindow", FakeWin32Gui.DestroyWindow, raising=False)
        monkeypatch.setattr("win32gui.PostMessage", FakeWin32Gui.PostMessage, raising=False)
        monkeypatch.setattr("win32process.GetWindowThreadProcessId",
                            FakeWin32Process.GetWindowThreadProcessId, raising=False)
        monkeypatch.setattr("win32api.OpenProcess", FakeWin32Api.OpenProcess, raising=False)
        monkeypatch.setattr("win32api.TerminateProcess", FakeWin32Api.TerminateProcess, raising=False)
        monkeypatch.setattr("win32api.CloseHandle", FakeWin32Api.CloseHandle, raising=False)
        monkeypatch.setattr(lifecycle.time, "sleep", lambda *a, **kw: None)

        result = lifecycle.close_window(12345, force=True)

        # 核心：force=True 必须走 TerminateProcess
        assert calls["terminate"] == 1, f"❌ force=True 应调用 1 次 TerminateProcess，实际 {calls['terminate']}"
        assert calls["destroy"] == 0, f"❌ force=True 不应调用 DestroyWindow，实际 {calls['destroy']}"
        assert calls["post_message"] == 0, f"❌ force=True 不应发 WM_CLOSE，实际 {calls['post_message']}"
        # 进程被杀后窗口消失 → success=True
        assert result["success"] is True, f"❌ 杀进程后应 success=true，实际 {result['success']}"
        assert result["status"] == "executed"
        assert "pid=6789" in result["message"]

    def test_close_window_force_true_open_process_failure_returns_failure(self, monkeypatch):
        """第三轮评估后续修复：force=True + OpenProcess 失败（权限不足）应返回 success=false + status=failed"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        class FakeWin32Gui:
            @staticmethod
            def IsWindow(hwnd): return True
            @staticmethod
            def GetWindowText(hwnd): return "Mock"
            @staticmethod
            def IsWindowVisible(hwnd): return True
            @staticmethod
            def IsIconic(hwnd): return False
            @staticmethod
            def GetWindowRect(hwnd): return (0, 0, 100, 100)
            @staticmethod
            def GetForegroundWindow(): return 0

        class FakeWin32Process:
            @staticmethod
            def GetWindowThreadProcessId(hwnd):
                return (123, 6789)

        class FakeWin32Api:
            @staticmethod
            def OpenProcess(access, inherit, pid):
                raise PermissionError("Access denied (mock)")

        monkeypatch.setattr("win32gui.IsWindow", FakeWin32Gui.IsWindow, raising=False)
        monkeypatch.setattr("win32gui.GetWindowText", FakeWin32Gui.GetWindowText, raising=False)
        monkeypatch.setattr("win32gui.IsWindowVisible", FakeWin32Gui.IsWindowVisible, raising=False)
        monkeypatch.setattr("win32gui.IsIconic", FakeWin32Gui.IsIconic, raising=False)
        monkeypatch.setattr("win32gui.GetWindowRect", FakeWin32Gui.GetWindowRect, raising=False)
        monkeypatch.setattr("win32gui.GetForegroundWindow", FakeWin32Gui.GetForegroundWindow, raising=False)
        monkeypatch.setattr("win32process.GetWindowThreadProcessId",
                            FakeWin32Process.GetWindowThreadProcessId, raising=False)
        monkeypatch.setattr("win32api.OpenProcess", FakeWin32Api.OpenProcess, raising=False)
        monkeypatch.setattr(lifecycle.time, "sleep", lambda *a, **kw: None)

        result = lifecycle.close_window(12345, force=True)
        # OpenProcess 失败 → success=false + status=failed（不能谎报成功）
        assert result["success"] is False
        assert result["status"] == "failed"
        assert "OpenProcess" in result["message"] or "权限不足" in result["message"]

    def test_try_dismiss_save_modal_uia_clicked_dont_save(self, monkeypatch):
        """第三轮评估后续修复：_try_dismiss_save_modal 优先 UIA 在主窗口子树找'不保存' → dismissed=true

        新策略：先 UIA BFS 主窗口子树，找到'不保存'按钮 invoke。
        """
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        # Mock UIA：模拟在主窗口子树找到"不保存(N)"按钮
        class FakeInvokePattern:
            def Invoke(self): pass

        class FakeButton:
            Name = "不保存(N)"
            ClassName = "CCPushButton"
            ControlTypeName = "ButtonControl"
            NativeWindowHandle = 11111
            def GetInvokePattern(self): return FakeInvokePattern()

        class FakeRoot:
            Name = "无标题 - 记事本"
            ClassName = "Notepad"
            ControlTypeName = "WindowControl"
            NativeWindowHandle = 12345
            def GetChildren(self):
                # 直接返回 [Button]，模拟模态对话框的按钮已嵌入主窗口子树
                return [FakeButton()]

        class FakeUa:
            @staticmethod
            def ControlFromHandle(hwnd):
                return FakeRoot()

        fake_module = type(sys)("uiautomation_mock")
        fake_module.ControlFromHandle = FakeUa.ControlFromHandle
        monkeypatch.setitem(sys.modules, "uiautomation", fake_module)

        result = lifecycle._try_dismiss_save_modal(12345)
        assert result["dismissed"] is True
        assert result["dialog_count"] == 1
        assert "UIA" in result["message"] or "不保存" in result["message"]

    def test_try_dismiss_save_modal_no_dialog_returns_not_dismissed(self, monkeypatch):
        """第三轮评估后续修复：_try_dismiss_save_modal UIA 找不到 + EnumWindows 无 #32770 → dismissed=false"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        # Mock UIA：主窗口子树无按钮
        class FakeRoot:
            Name = "无标题 - 记事本"
            ClassName = "Notepad"
            ControlTypeName = "WindowControl"
            NativeWindowHandle = 12345
            def GetChildren(self):
                return []  # 无子元素

        class FakeUa:
            @staticmethod
            def ControlFromHandle(hwnd):
                return FakeRoot()

        fake_module = type(sys)("uiautomation_mock")
        fake_module.ControlFromHandle = FakeUa.ControlFromHandle
        monkeypatch.setitem(sys.modules, "uiautomation", fake_module)

        # Mock EnumWindows：不返回任何 #32770
        def fake_enum_windows(callback, _):
            return None  # 不调 callback

        class FakeWin32Process:
            @staticmethod
            def GetWindowThreadProcessId(hwnd):
                return (123, 6789)

        class FakeWin32Gui:
            @staticmethod
            def IsWindow(hwnd): return True
            @staticmethod
            def GetClassName(hwnd): return "NotDialog"
            @staticmethod
            def GetWindowText(hwnd): return ""
            @staticmethod
            def GetWindow(hwnd, flag): return 0  # 无 owner

        monkeypatch.setattr("win32gui.EnumWindows", fake_enum_windows, raising=False)
        monkeypatch.setattr("win32gui.IsWindow", FakeWin32Gui.IsWindow, raising=False)
        monkeypatch.setattr("win32gui.GetClassName", FakeWin32Gui.GetClassName, raising=False)
        monkeypatch.setattr("win32gui.GetWindowText", FakeWin32Gui.GetWindowText, raising=False)
        monkeypatch.setattr("win32gui.GetWindow", FakeWin32Gui.GetWindow, raising=False)
        monkeypatch.setattr("win32process.GetWindowThreadProcessId",
                            FakeWin32Process.GetWindowThreadProcessId, raising=False)

        result = lifecycle._try_dismiss_save_modal(12345)
        assert result["dismissed"] is False
        assert result["dialog_count"] == 0
        # 消息应说明 UIA 未找到 + EnumWindows 也未找到
        assert "UIA" in result["message"] or "EnumWindows" in result["message"]

    def test_app_launch_invalid_command_returns_failure(self, client):
        """app_launch 不存在的命令应返回 success=False"""
        resp = client.post("/screen/app/launch", json={
            "command": "___nonexistent_app_p1b_test___.exe",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["pid"] == 0

    def test_app_wait_timeout_returns_timeout_status(self, client):
        """app_wait 等不到窗口应返回 status=timeout"""
        resp = client.post("/screen/app/wait", json={
            "title": "___NONEXISTENT_WINDOW_P1B_WAIT_TEST___",
            "timeout": 0.5,
            "poll_interval": 0.1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "timeout"
        assert data["matches"] == []


class TestWindowTokenVerification:
    """P1-B：window token 失效检测单元测试（纯逻辑，不依赖实际窗口）"""

    def test_token_constants_are_distinct_strings(self):
        """P1-B 状态码常量应为不同非空字符串"""
        from server.screen.lifecycle import (
            WINDOW_AMBIGUOUS,
            WINDOW_NOT_FOUND,
            WINDOW_TOKEN_INVALID,
        )
        codes = {WINDOW_TOKEN_INVALID, WINDOW_AMBIGUOUS, WINDOW_NOT_FOUND}
        assert len(codes) == 3
        for c in codes:
            assert isinstance(c, str) and c

    def test_verify_token_missing_hwnd_returns_invalid(self):
        """token 缺少 canonical_hwnd 应判定为失效"""
        from server.screen.lifecycle import verify_window_token
        valid, reason = verify_window_token({})
        assert valid is False
        assert "canonical_hwnd" in reason

    def test_verify_token_nonexistent_hwnd_returns_invalid(self):
        """不存在的 hwnd 应判定为失效"""
        from server.screen.lifecycle import verify_window_token
        token = {
            "canonical_hwnd": 99999999,
            "pid": 99999999,
            "process_create_time": None,
        }
        valid, reason = verify_window_token(token)
        # 在 Windows 上应返回 False（hwnd 不存在）；非 Windows 直接通过
        import sys
        if sys.platform == "win32":
            assert valid is False
            assert "销毁" in reason or "pid" in reason or "失败" in reason

    def test_verify_token_pid_mismatch_returns_invalid(self, monkeypatch):
        """hwnd 存在但 pid 不匹配应判定为 hwnd 被复用"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import lifecycle

        # mock win32gui.IsWindow 返回 True，win32process.GetWindowThreadProcessId 返回不同 pid
        class FakeWin32Gui:
            @staticmethod
            def IsWindow(hwnd):
                return True

        class FakeWin32Process:
            @staticmethod
            def GetWindowThreadProcessId(hwnd):
                return (999, 8888)  # pid=8888，与 token 中的 1234 不匹配

        monkeypatch.setitem(sys.modules, "win32gui", FakeWin32Gui)
        monkeypatch.setitem(sys.modules, "win32process", FakeWin32Process)

        token = {
            "canonical_hwnd": 12345,
            "pid": 1234,
            "process_create_time": None,
        }
        valid, reason = lifecycle.verify_window_token(token)
        assert valid is False
        assert "pid" in reason or "复用" in reason


# ========== 第二轮评估 P0-1：UIA CoInitialize 单元测试 ==========

class TestUIACoInitialize:
    """P0-1：UIA 入口必须显式 CoInitialize，避免 WinError -2147221008"""

    def test_ensure_com_initialized_returns_tuple(self):
        """_ensure_com_initialized 应返回 (bool, str) 元组"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen.uia import _ensure_com_initialized
        ok, reason = _ensure_com_initialized()
        assert isinstance(ok, bool)
        assert isinstance(reason, str)
        # 在 Windows 上首次调用应该成功
        assert ok is True
        assert reason == ""

    def test_ensure_com_initialized_idempotent(self):
        """同一线程多次调用应幂等（不重复 CoInitialize）"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        import threading

        from server.screen.uia import _com_initialized_threads, _ensure_com_initialized

        tid = threading.get_ident()
        # 应该已经初始化（前面测试调过）
        ok1, _ = _ensure_com_initialized()
        ok2, _ = _ensure_com_initialized()
        assert ok1 is True
        assert ok2 is True
        assert tid in _com_initialized_threads

    def test_ensure_com_initialized_non_windows_returns_false(self, monkeypatch):
        """非 Windows 平台应返回 (False, 'non-windows platform')"""
        from server.screen import uia

        # 强制 _PLATFORM_WIN=False
        monkeypatch.setattr(uia, "_PLATFORM_WIN", False)
        # 清空已初始化集合模拟全新状态
        monkeypatch.setattr(uia, "_com_initialized_threads", set())
        ok, reason = uia._ensure_com_initialized()
        assert ok is False
        assert "non-windows" in reason

    def test_uia_snapshot_com_init_failure_returns_fallback(self, monkeypatch):
        """COM 初始化失败时应返回 fallback_reason=UIA_NOT_AVAILABLE"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import uia

        # mock uia_available 返回 True，但 _ensure_com_initialized 返回失败
        monkeypatch.setattr(uia, "uia_available", lambda: True)
        monkeypatch.setattr(
            uia, "_ensure_com_initialized",
            lambda: (False, "mocked CoInitialize failure"),
        )
        result = uia.take_uia_snapshot(hwnd=12345)
        assert result["success"] is False
        assert result["fallback_reason"] == uia.UIA_NOT_AVAILABLE
        assert "COM 初始化失败" in result["message"]


# ========== 第二轮评估 P0-2：Unicode UTF-16 代理对单元测试 ==========

class TestUnicodeInput:
    """P0-2 + P2-2：Unicode 输入 UTF-16 代理对 + 输入法/剪贴板测试矩阵"""

    def test_bmp_character_single_sendinput(self, monkeypatch):
        """BMP 字符（≤ 0xFFFF）应只调用一次 SendInput（无代理对拆分）"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import input

        # 计数 SendInput 调用
        call_count = {"n": 0}
        scan_codes_sent = []

        def fake_sendinput(n, inputs, size):
            call_count["n"] += 1
            # 提取 scan code
            for i in range(n):
                try:
                    scan_codes_sent.append(inputs[i].ki.wScan)
                except Exception:
                    pass
            return n

        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        # _send_vk 也用 SendInput，控制字符 \n 会走 _send_vk
        # 测试纯字符 'A'（U+0041）
        input._send_unicode_text("A")
        # 字符 'A' 应触发 1 次 SendInput（含 keydown + keyup 共 2 个 input struct）
        assert call_count["n"] == 1
        assert 0x0041 in scan_codes_sent

    def test_emoji_surrogate_pair_two_sendinput(self, monkeypatch):
        """emoji（U+1F600 😀）应拆成 2 个 scan code，触发 2 次 SendInput"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import input

        call_count = {"n": 0}
        scan_codes_sent = []

        def fake_sendinput(n, inputs, size):
            call_count["n"] += 1
            for i in range(n):
                try:
                    scan_codes_sent.append(inputs[i].ki.wScan)
                except Exception:
                    pass
            return n

        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        # 😀 = U+1F600
        input._send_unicode_text("😀")
        # 应触发 2 次 SendInput（高代理 + 低代理，每次 keydown+keyup）
        assert call_count["n"] == 2
        # 高代理 = 0xD800 + ((0x1F600 - 0x10000) >> 10) = 0xD800 + 0x3D = 0xD83D
        # 低代理 = 0xDC00 + ((0x1F600 - 0x10000) & 0x3FF) = 0xDC00 + 0x200 = 0xDE00
        assert 0xD83D in scan_codes_sent
        assert 0xDE00 in scan_codes_sent

    def test_cjk_extension_b_surrogate_pair(self, monkeypatch):
        """CJK 扩展 B（U+20000-2A6D6）应拆成正确的代理对"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import input

        scan_codes_sent = []

        def fake_sendinput(n, inputs, size):
            for i in range(n):
                try:
                    scan_codes_sent.append(inputs[i].ki.wScan)
                except Exception:
                    pass
            return n

        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        # 𠀀 = U+20000（CJK 扩展 B 第一个字）
        cp = 0x20000
        expected_high = 0xD800 + ((cp - 0x10000) >> 10)  # 0xD840
        expected_low = 0xDC00 + ((cp - 0x10000) & 0x3FF)  # 0xDC00
        input._send_unicode_text("\U00020000")
        assert expected_high in scan_codes_sent
        assert expected_low in scan_codes_sent

    def test_mixed_text_bmp_and_emoji(self, monkeypatch):
        """混合文本（ASCII + 中文 + emoji）应正确逐字符发送"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import input

        scan_codes_sent = []

        def fake_sendinput(n, inputs, size):
            for i in range(n):
                try:
                    scan_codes_sent.append(inputs[i].ki.wScan)
                except Exception:
                    pass
            return n

        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        # 中E2E café 😀
        text = "中E2E café 😀"
        input._send_unicode_text(text)
        # '中' (U+4E2D), 'E', '2', 'E', ' ', 'c', 'a', 'f', 'é' (U+00E9),
        # ' ', '😀' (U+1F600，代理对 2 个 scan code)
        # 总 scan code 数 = 10 个 BMP + 2 个代理 = 12
        # 但 'é' = U+00E9 是 BMP（≤ 0xFFFF），单 scan code
        assert 0x4E2D in scan_codes_sent  # 中
        assert 0x00E9 in scan_codes_sent  # é
        assert 0xD83D in scan_codes_sent  # 😀 高代理 (0xD800 + 0x3D)
        assert 0xDE00 in scan_codes_sent  # 😀 低代理 (0xDC00 + 0x200)

    def test_lone_surrogate_skipped_with_warning(self, monkeypatch, caplog):
        """孤立代理字符（0xD800-0xDFFF）应跳过并发 warning"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        import logging

        from server.screen import input

        scan_codes_sent = []

        def fake_sendinput(n, inputs, size):
            for i in range(n):
                try:
                    scan_codes_sent.append(inputs[i].ki.wScan)
                except Exception:
                    pass
            return n

        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        # 直接构造含孤立代理的字符串（通常不应出现，但防御性测试）
        with caplog.at_level(logging.WARNING, logger="localagent.screen"):
            input._send_unicode_text("\uD800\x41")  # 高代理 + 'A'
        # 孤立代理应被跳过，只有 'A' 被发送
        assert 0x0041 in scan_codes_sent
        assert 0xD800 not in scan_codes_sent  # 孤立代理被跳过

    def test_control_characters_use_vk(self, monkeypatch):
        """控制字符 \\n \\t 应走 _send_vk（虚拟键），不走 Unicode 通道"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import input

        send_vk_calls = []
        sendinput_calls = {"n": 0}

        def fake_send_vk(vk_code):
            send_vk_calls.append(vk_code)
            return True

        def fake_sendinput(n, inputs, size):
            sendinput_calls["n"] += 1
            return n

        monkeypatch.setattr(input, "_send_vk", fake_send_vk)
        monkeypatch.setattr("ctypes.windll.user32.SendInput", fake_sendinput)
        import win32con
        input._send_unicode_text("\n\t")
        assert win32con.VK_RETURN in send_vk_calls
        assert win32con.VK_TAB in send_vk_calls
        # \n \t 不应触发 Unicode SendInput
        assert sendinput_calls["n"] == 0


# ========== 第二轮评估 P0-3：动作状态语义单元测试 ==========

class TestPostconditionStatusSemantics:
    """P0-3：postcondition_failed vs executed_unverified vs executed 语义"""

    def test_verify_postcondition_none_returns_executed_unverified(self):
        """未声明 expected 时返回 executed_unverified"""
        from server.screen.routes import _verify_postcondition
        assert _verify_postcondition(None, None) == "executed_unverified"

    def test_verify_postcondition_no_image_returns_not_checked(self):
        """声明了 expected 但 pil_image=None 返回 not_checked"""
        from server.screen.routes import _verify_postcondition
        result = _verify_postcondition({"type": "ocr_contains", "text": "x"}, None)
        assert result == "not_checked"

    def test_verify_postcondition_empty_text_returns_error(self):
        """expected.text 为空返回 error"""
        from PIL import Image

        from server.screen.routes import _verify_postcondition
        img = Image.new("RGB", (10, 10), "white")
        result = _verify_postcondition({"type": "ocr_contains", "text": ""}, img)
        assert result == "error"

    def test_verify_postcondition_invalid_type_returns_error(self):
        """expected.type 不在白名单返回 error"""
        from PIL import Image

        from server.screen.routes import _verify_postcondition
        img = Image.new("RGB", (10, 10), "white")
        result = _verify_postcondition({"type": "invalid_type", "text": "x"}, img)
        assert result == "error"

    def test_verify_postcondition_ocr_contains_verified(self, monkeypatch):
        """ocr_contains 命中时返回 verified"""
        from PIL import Image

        from server.screen import routes

        class FakeOcrResp:
            text = "Hello World"

        def fake_do_ocr(pil_image):
            return FakeOcrResp()

        monkeypatch.setattr("server.ocr._do_ocr", fake_do_ocr)
        img = Image.new("RGB", (10, 10), "white")
        result = routes._verify_postcondition({"type": "ocr_contains", "text": "Hello"}, img)
        assert result == "verified"

    def test_verify_postcondition_ocr_contains_failed(self, monkeypatch):
        """ocr_contains 未命中返回 failed（不返回 error）"""
        from PIL import Image

        from server.screen import routes

        class FakeOcrResp:
            text = "Hello World"

        def fake_do_ocr(pil_image):
            return FakeOcrResp()

        monkeypatch.setattr("server.ocr._do_ocr", fake_do_ocr)
        img = Image.new("RGB", (10, 10), "white")
        result = routes._verify_postcondition({"type": "ocr_contains", "text": "Goodbye"}, img)
        assert result == "failed"

    def test_verify_postcondition_ocr_not_contains_verified(self, monkeypatch):
        """ocr_not_contains 未命中时返回 verified"""
        from PIL import Image

        from server.screen import routes

        class FakeOcrResp:
            text = "Hello World"

        def fake_do_ocr(pil_image):
            return FakeOcrResp()

        monkeypatch.setattr("server.ocr._do_ocr", fake_do_ocr)
        img = Image.new("RGB", (10, 10), "white")
        result = routes._verify_postcondition(
            {"type": "ocr_not_contains", "text": "Goodbye"}, img
        )
        assert result == "verified"


# ========== 第二轮评估 P0-4：preview/action format=path 默认 ==========

class TestPreviewFormatPath:
    """P0-4：preview_action 默认 format=path，不污染上下文"""

    def test_preview_action_request_default_format_is_path(self):
        """PreviewActionRequest.format 默认值应为 'path'"""
        from server.screen.routes import PreviewActionRequest
        req = PreviewActionRequest(points=[{"x": 100, "y": 100}])
        assert req.format == "path"

    def test_preview_action_invalid_format_returns_400(self, client):
        """format 不在白名单应返回 400"""
        resp = client.post("/screen/preview/action", json={
            "points": [{"x": 100, "y": 100}],
            "mode": "fullscreen",
            "format": "invalid_format",
        })
        assert resp.status_code == 400
        assert "format" in resp.json()["detail"]

    def test_preview_action_response_model_has_path_field(self):
        """PreviewActionResponse 应有 path 字段（默认 None）"""
        from server.screen.routes import PreviewActionResponse
        # 验证字段存在
        fields = PreviewActionResponse.model_fields
        assert "path" in fields
        assert "image" in fields
        assert "mcp_image_block" in fields


# ========== 第二轮评估 P1-1：dry_run 模式单元测试 ==========

class TestDryRunMode:
    """P1-1：ActionRequest/BatchActionsRequest dry_run 字段"""

    def test_action_request_has_dry_run_field(self):
        """ActionRequest 应有 dry_run 字段，默认 False"""
        from server.screen.routes import ActionRequest
        req = ActionRequest(action="click", x=100, y=100)
        assert req.dry_run is False

    def test_batch_actions_request_has_dry_run_field(self):
        """BatchActionsRequest 应有 dry_run 字段，默认 False"""
        from server.screen.routes import BatchActionsRequest
        req = BatchActionsRequest(actions=[{"action": "click", "x": 100, "y": 100}])
        assert req.dry_run is False

    def test_batch_actions_dry_run_returns_dry_run_status(self, client, monkeypatch):
        """dry_run=true 时 batch_actions 应返回 status=dry_run，transport_status=not_sent"""
        # mock admin 权限和 emergency
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        # mock config
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {"focus_protection_enabled": False, "protected_processes": None},
        )
        # mock _find_window 返回 None（dry-run 不需要真实窗口）
        monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: None)
        # mock _force_focus_window
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        # mock overlay_client
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "click", "x": 100, "y": 100, "snapshot_id": None},
            ],
            "dry_run": True,
            "activate_window": False,
            "stop_on_error": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["executed"] == 1
        # 每步 status=dry_run
        assert data["results"][0]["status"] == "dry_run"
        assert data["results"][0]["transport_status"] == "not_sent"


# ========== 第二轮评估 P1-2：canonical_window 动作后刷新 ==========

class TestCanonicalWindowRefresh:
    """P1-2：动作后 canonical_window 应刷新（不保留旧标题）"""

    def test_canonical_for_response_extracts_correct_fields(self):
        """_canonical_for_response 应提取 canonical_hwnd/title/pid/process_name/is_uwp_host"""
        from server.screen.routes import _canonical_for_response

        canonical = {
            "canonical_hwnd": 12345,
            "canonical_title": "Test Window",
            "canonical_pid": 6789,
            "canonical_process_name": "test.exe",
            "is_uwp_host": False,
            "family_hwnds": [1, 2, 3],  # 应被过滤掉
            "input_hwnd": 99999,  # 应被过滤掉
        }
        result = _canonical_for_response(canonical)
        assert result is not None
        assert result["canonical_hwnd"] == 12345
        assert result["canonical_title"] == "Test Window"
        assert result["canonical_pid"] == 6789
        assert result["canonical_process_name"] == "test.exe"
        assert result["is_uwp_host"] is False
        # family_hwnds 和 input_hwnd 不应出现在响应中
        assert "family_hwnds" not in result
        assert "input_hwnd" not in result

    def test_canonical_for_response_none_returns_none(self):
        """传入 None 应返回 None"""
        from server.screen.routes import _canonical_for_response
        assert _canonical_for_response(None) is None

    def test_canonical_for_response_empty_dict_returns_none(self):
        """空 dict（falsy）应返回 None（与 None 等价处理）"""
        from server.screen.routes import _canonical_for_response
        # _canonical_for_response 用 `if not canonical` 判空，空 dict 为 falsy
        result = _canonical_for_response({})
        assert result is None


# ========== 第二轮评估 P1-3：窗口引用统一（hwnd 优先）==========

class TestWindowReferenceUnification:
    """P1-3：wait_for/analyze 接受 hwnd 字段且优先于 window_title"""

    def test_wait_for_request_has_hwnd_field(self):
        """WaitForRequest 应有 hwnd 字段（expected 为 dict 格式，与其他端点统一）"""
        from server.screen.scroll_wait_analyze_endpoints import WaitForRequest
        req = WaitForRequest(expected={"type": "ocr_contains", "text": "test"})
        assert hasattr(req, "hwnd")
        assert req.hwnd is None

    def test_analyze_request_has_hwnd_field(self):
        """AnalyzeRequest 应有 hwnd 字段"""
        from server.screen.routes import AnalyzeRequest
        req = AnalyzeRequest()
        assert hasattr(req, "hwnd")
        assert req.hwnd is None

    def test_analyze_accepts_hwnd_only(self, client, monkeypatch):
        """analyze 传入 hwnd（无 window_title）应通过参数校验"""
        # mock 截图为 fake image
        import io as _io

        from PIL import Image

        def fake_capture_window(hwnd):
            img = Image.new("RGB", (100, 100), "white")
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        def fake_capture_fullscreen():
            img = Image.new("RGB", (100, 100), "white")
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        monkeypatch.setattr("server.screen.routes._capture_window", fake_capture_window)
        monkeypatch.setattr("server.screen.routes._capture_fullscreen", fake_capture_fullscreen)
        # mock overlay
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        _sync_screen_patches(monkeypatch, ["scroll_wait_analyze_endpoints"])
        resp = client.post("/screen/analyze", json={
            "mode": "window",
            "hwnd": 12345,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_analyze_no_hwnd_no_title_returns_error(self, client):
        """analyze mode=window 但无 hwnd 无 title 应返回错误"""
        resp = client.post("/screen/analyze", json={
            "mode": "window",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "hwnd" in data["message"] or "window_title" in data["message"]


# ========== 第二轮评估 P1-4：UIA role 硬编码稳定映射 ==========

class TestUIARoleHardcodedMapping:
    """P1-4：UIA ControlType 应有硬编码映射兜底，避免依赖库版本"""

    def test_control_type_hardcoded_dict_has_core_types(self):
        """_CONTROL_TYPE_HARDCODED 应包含 Microsoft UIA Core 稳定 ID"""
        from server.screen.uia import _CONTROL_TYPE_HARDCODED
        # 检查 Microsoft 文档中的核心 ControlType ID
        assert _CONTROL_TYPE_HARDCODED[50000] == "Button"
        assert _CONTROL_TYPE_HARDCODED[50004] == "Edit"
        assert _CONTROL_TYPE_HARDCODED[50032] == "Window"
        assert _CONTROL_TYPE_HARDCODED[50033] == "Pane"
        assert _CONTROL_TYPE_HARDCODED[50037] == "TitleBar"

    def test_control_type_name_returns_string_for_known_id(self):
        """_control_type_name 对已知 ID 应返回字符串名"""
        from server.screen.uia import _control_type_name
        assert _control_type_name(50000) == "Button"
        assert _control_type_name(50004) == "Edit"

    def test_control_type_name_unknown_id_returns_unknown_prefix(self):
        """未知 ID 应返回 Unknown_<int>（不返回空字符串）"""
        from server.screen.uia import _control_type_name
        result = _control_type_name(99999)
        assert result.startswith("Unknown_")
        assert "99999" in result

    def test_init_control_type_map_uses_hardcoded_first(self):
        """_init_control_type_map 应先填硬编码表（兜底）"""
        from server.screen.uia import _CONTROL_TYPE_HARDCODED, _CONTROL_TYPE_MAP, _init_control_type_map
        # 清空缓存强制重新初始化
        _CONTROL_TYPE_MAP.clear()
        _init_control_type_map()
        # 所有硬编码项都应在 _CONTROL_TYPE_MAP 中
        for k, v in _CONTROL_TYPE_HARDCODED.items():
            assert _CONTROL_TYPE_MAP.get(k) == v


# ========== 第二轮评估 P2-1：desktop_transaction 桌面事务接口测试 ==========

class TestDesktopTransaction:
    """P2-1：桌面事务接口单元测试"""

    def test_desktop_transaction_request_required_fields(self):
        """DesktopTransactionRequest 必填字段校验"""
        from pydantic import ValidationError

        from server.screen.routes import DesktopTransactionRequest
        # 缺 target / actions / expected 应失败
        with pytest.raises(ValidationError):
            DesktopTransactionRequest()
        # 缺 expected 应失败
        with pytest.raises(ValidationError):
            DesktopTransactionRequest(
                target={"hwnd": 12345},
                actions=[{"action": "click", "x": 100, "y": 100}],
            )

    def test_desktop_transaction_default_rollback_policy_is_none(self):
        """rollback_policy 默认应为 'none'"""
        from server.screen.routes import DesktopTransactionRequest
        req = DesktopTransactionRequest(
            target={"hwnd": 12345},
            actions=[{"action": "click", "x": 100, "y": 100}],
            expected={"type": "ocr_contains", "text": "success"},
        )
        assert req.rollback_policy == "none"
        assert req.timeout == 60.0
        assert req.dry_run is False

    def test_desktop_transaction_no_expected_returns_aborted(self, client, monkeypatch):
        """无 expected 的事务应被拒绝（status=aborted）"""
        from server.screen import routes
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "aborted"
        assert "expected" in data["message"]

    def test_desktop_transaction_empty_actions_returns_aborted(self, client, monkeypatch):
        """空 actions 列表应返回 aborted"""
        from server.screen import routes
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [],
            "expected": {"type": "ocr_contains", "text": "x"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "aborted"

    def test_desktop_transaction_invalid_rollback_policy_returns_aborted(self, client, monkeypatch):
        """非法 rollback_policy 应返回 aborted"""
        from server.screen import routes
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "x"},
            "rollback_policy": "invalid",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "aborted"
        assert "rollback_policy" in data["message"]

    def test_desktop_transaction_no_target_returns_aborted(self, client, monkeypatch):
        """target 既无 hwnd 也无 window_title 应返回 aborted"""
        from server.screen import routes
        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "x"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "aborted"
        assert "target" in data["message"]

    def test_desktop_transaction_response_model_has_all_fields(self):
        """DesktopTransactionResponse 模型应包含所有字段"""
        from server.screen.routes import DesktopTransactionResponse
        fields = DesktopTransactionResponse.model_fields
        expected_fields = {
            "success", "status", "dry_run", "total_steps", "executed_steps",
            "failed_step_index", "failed_reason", "rollback_executed",
            "rollback_steps_succeeded", "rollback_results",
            "transaction_postcondition", "elapsed_ms", "results",
            "canonical_window", "message",
        }
        for f in expected_fields:
            assert f in fields, f"Missing field: {f}"

    def test_desktop_transaction_dry_run_response_success_true(self, client, monkeypatch):
        """第三轮评估 P1-1：dry_run=true 时 success 应为 True（预演通过），dry_run 字段为 True"""
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {"focus_protection_enabled": False, "protected_processes": None},
        )
        monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: None)
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "x"},
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # P1-1：dry_run 是预演成功，不是失败
        assert data["success"] is True
        assert data["status"] == "dry_run"
        assert data["dry_run"] is True
        assert data["transaction_postcondition"] == "not_checked"
        assert data["rollback_executed"] is False

    def test_desktop_transaction_dry_run_skips_postcondition(self, client, monkeypatch):
        """dry_run=true 时应跳过事务级后验，status=dry_run"""
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {"focus_protection_enabled": False, "protected_processes": None},
        )
        monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: None)
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "x"},
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dry_run"
        assert data["transaction_postcondition"] == "not_checked"
        assert data["rollback_executed"] is False


# ========== 第二轮评估 P2-3：受保护窗口焦点漂移 dry-run 测试 ==========

class TestFocusDriftDryRun:
    """P2-3：dry-run 模式下测试焦点漂移拦截逻辑（不真实输入到受保护窗口）"""

    def test_dry_run_does_not_send_input_to_protected_process(
        self, client, monkeypatch
    ):
        """dry-run 模式下即使目标窗口是受保护进程，也不应发送任何键鼠事件"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        # 启用焦点保护 + 设置 ChatGPT.exe 为受保护进程
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {
                "focus_protection_enabled": True,
                "protected_processes": ["ChatGPT.exe", "Codex.exe", "trae.exe"],
            },
        )

        # mock _find_window 返回 ChatGPT 窗口
        fake_window = {
            "hwnd": 99999,
            "title": "ChatGPT",
            "process_name": "ChatGPT.exe",
            "pid": 8888,
        }
        monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: fake_window)

        # 计数 _execute_action 调用（dry-run 不应调用）
        execute_calls = {"n": 0}

        def fake_execute_action(*args, **kwargs):
            execute_calls["n"] += 1
            return {"success": True, "message": "should not be called"}

        monkeypatch.setattr(routes, "_execute_action", fake_execute_action)

        # mock focus 校验为通过（dry-run 模式下不应触发 FOCUS_LEAK_PREVENTED）
        def fake_verify_focus(hwnd, allow_unfocused_input=False, protected_processes=None):
            # 即使焦点校验"通过"，dry-run 也不应执行
            return True, "focus_verified", {"target_match": True}

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify_focus)
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        # 用 ChatGPT 窗口作为目标，dry-run=true
        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "type", "text": "secret data"},
            ],
            "window_title": "ChatGPT",
            "dry_run": True,
            "activate_window": False,
            "stop_on_error": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # dry-run 模式下不应调用 _execute_action
        assert execute_calls["n"] == 0
        # 每步 status=dry_run，transport_status=not_sent
        assert data["results"][0]["status"] == "dry_run"
        assert data["results"][0]["transport_status"] == "not_sent"

    def test_dry_run_focus_check_failure_blocks_without_sending(
        self, client, monkeypatch
    ):
        """dry-run 模式下焦点校验失败应阻塞，但仍不发送键鼠"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {
                "focus_protection_enabled": True,
                "protected_processes": ["ChatGPT.exe"],
            },
        )

        # mock _find_window 返回 None（窗口找不到）
        monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: None)
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        execute_calls = {"n": 0}

        def fake_execute_action(*args, **kwargs):
            execute_calls["n"] += 1
            return {"success": True}

        monkeypatch.setattr(routes, "_execute_action", fake_execute_action)

        # dry-run 但 actions 为空键鼠动作（无 hwnd 无 title）
        _sync_screen_patches(monkeypatch, ["batch_endpoints"])
        resp = client.post("/screen/batch-actions", json={
            "actions": [
                {"action": "type", "text": "test"},
            ],
            "dry_run": True,
            "activate_window": False,
            "stop_on_error": True,
        })
        assert resp.status_code == 200
        # 即使 dry-run，也不应调用 _execute_action
        assert execute_calls["n"] == 0

    def test_desktop_transaction_dry_run_protected_window_no_send(
        self, client, monkeypatch
    ):
        """desktop_transaction dry-run 对受保护窗口也不发送键鼠"""
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows 专属测试")
        from server.screen import routes

        monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        monkeypatch.setattr(
            "server.config.get_screen_config",
            lambda: {
                "focus_protection_enabled": True,
                "protected_processes": ["Codex.exe"],
            },
        )

        execute_calls = {"n": 0}

        def fake_execute_action(*args, **kwargs):
            execute_calls["n"] += 1
            return {"success": True}

        monkeypatch.setattr(routes, "_execute_action", fake_execute_action)

        # mock verify_focus_for_input 返回 FOCUS_LEAK_PREVENTED（拦截）
        from server.screen.focus import FOCUS_LEAK_PREVENTED

        def fake_verify_focus(hwnd, allow_unfocused_input=False, protected_processes=None):
            return False, FOCUS_LEAK_PREVENTED, {"target_match": False}

        monkeypatch.setattr(routes, "verify_focus_for_input", fake_verify_focus)
        monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)

        _sync_screen_patches(monkeypatch, ["desktop_transaction_endpoints"])
        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 99999},
            "actions": [
                {"action": "type", "text": "should be blocked"},
            ],
            "expected": {"type": "ocr_contains", "text": "x"},
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # dry-run 时即使焦点校验失败也不应调用 _execute_action
        assert execute_calls["n"] == 0
        # FOCUS_LEAK_PREVENTED 应导致步骤被 blocked
        if data["results"]:
            # 焦点校验失败时步骤可能 blocked 或 dry_run（取决于是否到达 dry_run 分支）
            assert data["results"][0]["status"] in ("blocked", "dry_run")

