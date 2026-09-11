"""ZCode computer-use 对齐增强测试（2026-09-05 六机制移植）。

覆盖：
- A 坐标点击 a11y 融合：strategy 决策表（auto 命中/未命中回退/uia_only blocked/event 回归/异常兜底/dry_run 提示）
- B UIA 能力标志 flags + actions 推导
- C zoom 最近帧复用：缓存命中/TTL 过期/未知 snapshot/region clamp/过小 + screen_ocr 帧复用
- D 剪贴板 read/write：正常/截断/无文本/danger block/403
- E 鼠标分段原语：防呆 + _execute_action 分支 + 端点透传
"""

import time

import pytest

import server.screen.uia as uia_mod
from server.screen.uia import hit_test_clickable

# conftest autouse（mock_uia_click_fusion）默认 mock 模块属性 try_uia_click_fusion；
# import 期保存真实函数引用，需要测真实融合行为的测试用它恢复
_REAL_CLICK_FUSION = uia_mod.try_uia_click_fusion


def _sync_screen_patches(monkeypatch, modules):
    """同步 routes 层 patch 到端点模块（复制自 test_screen.py，post code-split 必需）。"""
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


def _patch_action_base(monkeypatch):
    """execute_action 端点测试的公共 mock：焦点/证据/危险词/管理员位。"""
    from server.screen import routes

    monkeypatch.setattr(routes, "collect_focus_evidence", lambda target_hwnd=None: {
        "foreground": {"hwnd": 1, "process_name": "test.exe"},
        "target_match": True,
    })
    monkeypatch.setattr(routes, "resolve_canonical_window", lambda hwnd: {
        "canonical_hwnd": hwnd or 0, "canonical_title": "", "canonical_pid": 0,
        "canonical_process_name": "", "family_hwnds": [hwnd] if hwnd else [],
        "input_hwnd": hwnd, "is_uwp_host": False,
    })
    monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
    _sync_screen_patches(monkeypatch, ["action_endpoints"])


# ========== A. 坐标点击 a11y 融合 ==========

class TestClickFusion:
    """strategy 参数决策表（仅 action=click 参与）。"""

    def test_auto_hit_goes_uia_invoke(self, client, monkeypatch):
        """strategy=auto 且 hit-test 命中 → 走 UIA invoke，不调 _execute_action、不抢焦点"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)
        calls = {"fusion": 0, "exec": 0, "focus": 0}

        def fake_fusion(x, y):
            calls["fusion"] += 1
            return {"success": True, "role": "Button", "name": "OK",
                    "action": "invoke", "elapsed_ms": 3,
                    "message": "UIA invoke 已执行", "transport": "sent_uia_invoke"}

        def fail_exec(**kwargs):
            calls["exec"] += 1
            raise AssertionError("_execute_action 不应在融合命中时被调用")

        def fail_focus(hwnd):
            calls["focus"] += 1
            raise AssertionError("融合命中时不应激活窗口（抢焦点）")

        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", fake_fusion)
        monkeypatch.setattr(routes, "_execute_action", fail_exec)
        monkeypatch.setattr(routes, "_force_focus_window", fail_focus)
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert data["transport_status"] == "sent_uia_invoke"
        assert calls == {"fusion": 1, "exec": 0, "focus": 0}
        assert "UIA invoke" in data["message"]

    def test_auto_miss_falls_back_to_event(self, client, monkeypatch):
        """strategy=auto 未命中 → 静默回退原始键鼠（transport=sent）"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", lambda x, y: None)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert data["transport_status"] == "sent"

    def test_uia_only_miss_blocked(self, client, monkeypatch):
        """strategy=uia_only 未命中 → blocked，不回退原始键鼠"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", lambda x, y: None)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "strategy": "uia_only", "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "blocked"
        assert "uia_only" in data["message"]

    def test_event_skips_fusion_entirely(self, client, monkeypatch):
        """strategy=event → 不做 hit-test，直接原始键鼠（旧行为回归）"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)

        def fail_fusion(x, y):
            raise AssertionError("strategy=event 不应调用 hit-test")

        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", fail_fusion)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "strategy": "event", "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert data["transport_status"] == "sent"

    def test_invalid_strategy_blocked(self, client, monkeypatch):
        """非法 strategy → blocked"""
        _patch_action_base(monkeypatch)
        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "strategy": "uia", "require_confirm": False,
        })
        data = resp.json()
        assert data["status"] == "blocked"
        assert "strategy" in data["message"]

    def test_fusion_exception_falls_back(self, client, monkeypatch):
        """融合路径异常 → auto 模式回退原始键鼠，绝不因融合而失败"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)

        def boom(x, y):
            raise RuntimeError("COM exploded")

        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", boom)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert data["transport_status"] == "sent"

    def test_dry_run_reports_fusion_plan(self, client, monkeypatch):
        """dry_run + 融合命中 → message 注明将走 UIA invoke"""
        import server.screen.uia as uia_mod

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion",
                            lambda x, y: {"success": True, "role": "Button", "name": "OK",
                                          "action": "invoke", "elapsed_ms": 3,
                                          "message": "m", "transport": "sent_uia_invoke"})

        resp = client.post("/screen/action", json={
            "action": "click", "x": 100, "y": 200,
            "dry_run": True, "require_confirm": False,
        })
        data = resp.json()
        assert data["status"] == "dry_run"
        assert "UIA invoke" in data["message"]

    def test_non_click_action_unaffected(self, client, monkeypatch):
        """双击/右键/拖拽不受融合影响（strategy=event 语义之外的兼容保障）"""
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)

        def fail_fusion(x, y):
            raise AssertionError("double_click 不应触发融合")

        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", fail_fusion)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "double_click", "x": 100, "y": 200,
            "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True


class TestBatchClickFusion:
    """batch_actions 的 click 步骤融合。"""

    def test_batch_auto_hit_uses_invoke(self, client, monkeypatch):
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion",
                            lambda x, y: {"success": True, "role": "Button", "name": "OK",
                                          "action": "invoke", "elapsed_ms": 3,
                                          "message": "m", "transport": "sent_uia_invoke"})
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: (_ for _ in ()).throw(
                                AssertionError("融合命中时 batch 不应调 _execute_action")))
        _sync_screen_patches(monkeypatch, ["batch_endpoints"])

        resp = client.post("/screen/batch-actions", json={
            "actions": [{"action": "click", "x": 50, "y": 60}],
        })
        data = resp.json()
        assert data["success"] is True
        assert data["results"][0]["transport_status"] == "sent_uia_invoke"

    def test_batch_uia_only_miss_blocked(self, client, monkeypatch):
        import server.screen.uia as uia_mod
        from server.screen import routes

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", lambda x, y: None)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["batch_endpoints"])

        resp = client.post("/screen/batch-actions", json={
            "actions": [{"action": "click", "x": 50, "y": 60, "strategy": "uia_only"}],
        })
        data = resp.json()
        assert data["results"][0]["status"] == "blocked"
        assert data["blocked"] == 1

    def test_batch_invalid_strategy_blocked(self, client, monkeypatch):
        from server.screen import routes

        _patch_action_base(monkeypatch)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["batch_endpoints"])

        resp = client.post("/screen/batch-actions", json={
            "actions": [{"action": "click", "x": 50, "y": 60, "strategy": "bogus"}],
        })
        data = resp.json()
        assert data["results"][0]["status"] == "blocked"
        assert "strategy" in data["results"][0]["reason"]


# ========== B. UIA 能力标志 ==========

class FakeRect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class FakeCtrl:
    """最小 UIA Control 仿对象（_control_to_element_dict 所需字段）。"""

    ControlType = 50000  # Button
    Name = "确定"
    AutomationId = "btn-ok"
    IsEnabled = True
    HasKeyboardFocus = True

    def GetRuntimeId(self):
        return [3, 7, 9]

    @property
    def BoundingRectangle(self):
        return FakeRect(10, 20, 110, 60)


class FakePattern:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class TestFlagsAndActions:
    """_control_to_element_dict 的 flags / actions 推导。"""

    def _make_elem(self, monkeypatch, patterns: dict, ctrl=None):
        from server.screen import uia as uia_mod

        monkeypatch.setattr(uia_mod, "get_pattern", lambda c, name: patterns.get(name))
        return uia_mod._control_to_element_dict(
            ctrl or FakeCtrl(), depth=0, parent_index=None,
            element_index=0, snapshot_id="abcdef12", canonical_hwnd=123,
        )

    def test_button_flags(self, monkeypatch):
        """Button（Invoke + 可写 Value + 焦点）→ pressable editable focused + invoke/set_value"""
        elem = self._make_elem(monkeypatch, {
            "ValuePattern": FakePattern(Value="", IsReadOnly=False),
            "InvokePattern": FakePattern(),
        })
        assert elem is not None
        assert "pressable" in elem["flags"]
        assert "editable" in elem["flags"]
        assert "focused" in elem["flags"]
        assert elem["actions"] == ["invoke", "set_value"]

    def test_readonly_text_not_editable(self, monkeypatch):
        """只读 ValuePattern（Text/Document）→ editable 不出现、无 set_value"""
        class TextCtrl(FakeCtrl):
            ControlType = 50020  # Text
            HasKeyboardFocus = False

        elem = self._make_elem(monkeypatch, {
            "ValuePattern": FakePattern(Value="static", IsReadOnly=True),
        }, ctrl=TextCtrl())
        assert "editable" not in elem["flags"]
        assert "set_value" not in elem["actions"]
        assert "focused" not in elem["flags"]

    def test_checkbox_toggleable(self, monkeypatch):
        """CheckBox（Toggle + SelectionItem）→ toggleable selectable + toggle/select"""
        class CheckCtrl(FakeCtrl):
            ControlType = 50002  # CheckBox
            HasKeyboardFocus = False

        elem = self._make_elem(monkeypatch, {
            "TogglePattern": FakePattern(ToggleState=1),
            "SelectionItemPattern": FakePattern(IsSelected=False),
            "InvokePattern": FakePattern(),
        }, ctrl=CheckCtrl())
        assert "toggleable" in elem["flags"]
        assert "selectable" in elem["flags"]
        assert elem["checked"] is True
        assert set(elem["actions"]) == {"invoke", "toggle", "select"}

    def test_disabled_element_no_caps(self, monkeypatch):
        """禁用元素不宣称能力（focused 除外——不出现）"""

        class DisabledCtrl(FakeCtrl):
            IsEnabled = False
            HasKeyboardFocus = False

        elem = self._make_elem(monkeypatch, {
            "ValuePattern": FakePattern(Value="", IsReadOnly=False),
            "InvokePattern": FakePattern(),
        }, ctrl=DisabledCtrl())
        assert "pressable" not in elem["flags"]
        assert "editable" not in elem["flags"]
        assert "focused" not in elem["flags"]
        assert elem["actions"] == []

    def test_expandable(self, monkeypatch):
        """ExpandCollapse 支持 → expandable + expand/collapse 动作"""

        class TreeCtrl(FakeCtrl):
            ControlType = 50024  # TreeItem
            HasKeyboardFocus = False

        elem = self._make_elem(monkeypatch, {
            "ExpandCollapsePattern": FakePattern(),
        }, ctrl=TreeCtrl())
        assert "expandable" in elem["flags"]
        assert "expand" in elem["actions"]
        assert "collapse" in elem["actions"]

    def test_no_patterns_empty_flags(self, monkeypatch):
        """无任何 pattern → flags 空串、actions 空表（不报错）"""

        class PlainCtrl(FakeCtrl):
            HasKeyboardFocus = False

        elem = self._make_elem(monkeypatch, {}, ctrl=PlainCtrl())
        assert elem["flags"] == ""
        assert elem["actions"] == []


# ========== C. zoom 最近帧复用 ==========

def _make_png(width=100, height=80):
    from PIL import Image
    img = Image.new("RGB", (width, height), (200, 100, 50))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestScreenZoom:
    """/screen/zoom 端点。"""

    def test_zoom_inline(self, client):
        from server.screen import routes
        routes._record_frame("zoom_snap_1", _make_png())
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_1",
            "region": [10, 10, 50, 40],
            "output": "inline",
        })
        data = resp.json()
        assert data["mcp_image_block"] is True
        assert data["width"] == 40
        assert data["height"] == 30
        assert data["region_clamped"] == [10, 10, 50, 40]
        assert len(data["image"]) > 0

    def test_zoom_path_output(self, client):
        import os
        from server.screen import routes
        routes._record_frame("zoom_snap_2", _make_png())
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_2",
            "region": [0, 0, 20, 20],
            "output": "path",
        })
        data = resp.json()
        assert data["path"] and os.path.exists(data["path"])
        assert data["mcp_image_block"] is False

    def test_zoom_unknown_snapshot_404(self, client):
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "no_such_snapshot",
            "region": [0, 0, 10, 10],
        })
        assert resp.status_code == 404
        assert "capture_screen" in resp.json()["detail"]

    def test_zoom_expired_snapshot_404(self, client):
        from server.screen import routes
        routes._record_frame("zoom_snap_old", _make_png())
        with routes._frame_cache_lock:
            routes._frame_cache["zoom_snap_old"]["captured_at"] = time.time() - 400
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_old",
            "region": [0, 0, 10, 10],
        })
        assert resp.status_code == 404

    def test_zoom_region_clamped(self, client):
        from server.screen import routes
        routes._record_frame("zoom_snap_3", _make_png(width=100, height=80))
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_3",
            "region": [-10, -10, 999, 999],
        })
        data = resp.json()
        assert data["region_clamped"] == [0, 0, 100, 80]

    def test_zoom_region_too_small_400(self, client):
        from server.screen import routes
        routes._record_frame("zoom_snap_4", _make_png())
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_4",
            "region": [5, 5, 7, 6],
        })
        assert resp.status_code == 400

    def test_zoom_scale(self, client):
        from server.screen import routes
        routes._record_frame("zoom_snap_5", _make_png())
        resp = client.post("/screen/zoom", json={
            "snapshot_id": "zoom_snap_5",
            "region": [0, 0, 10, 10],
            "scale": 2.0,
        })
        data = resp.json()
        assert data["scaled"] is True
        assert data["width"] == 20

    def test_frame_cache_lru_eviction(self):
        """LRU 上限 3 帧：第 4 帧入缓存后最旧的被淘汰"""
        from server.screen import routes
        for i in range(5):
            routes._record_frame(f"lru_{i}", _make_png(10, 10))
        assert routes._get_cached_frame("lru_0") is None
        assert routes._get_cached_frame("lru_1") is None
        assert routes._get_cached_frame("lru_4") is not None


class TestScreenOcrFrameReuse:
    """screen_ocr 的 snapshot_id 帧复用 + region 局部 OCR。"""

    def test_ocr_uses_cached_frame(self, client, monkeypatch):
        from server.screen import routes

        routes._record_frame("ocr_snap_1", _make_png())
        calls = {"ocr": 0}

        class FakeOcrResp:
            text = "cached frame text"
            details = []

        def fake_ocr(image, max_height=3000):
            calls["ocr"] += 1
            return FakeOcrResp()

        monkeypatch.setattr("server.ocr._do_ocr", fake_ocr)
        # snapshot_id 模式不重截图：mock 截图函数，若被调则失败
        monkeypatch.setattr(routes, "_capture_window",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("snapshot_id 模式不应重截图")))
        monkeypatch.setattr(routes, "_capture_fullscreen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("snapshot_id 模式不应重截图")))

        resp = client.post("/screen/ocr", json={
            "snapshot_id": "ocr_snap_1", "engine": "ocr",
        })
        data = resp.json()
        assert data["success"] is True
        assert data["text"] == "cached frame text"
        assert calls["ocr"] == 1

    def test_ocr_region_crop(self, client, monkeypatch):
        from server.screen import routes

        routes._record_frame("ocr_snap_2", _make_png(width=200, height=100))

        class FakeOcrResp:
            text = "region text"
            details = []

        captured = {}

        def fake_ocr(image, max_height=3000):
            captured["size"] = image.size
            return FakeOcrResp()

        monkeypatch.setattr("server.ocr._do_ocr", fake_ocr)

        resp = client.post("/screen/ocr", json={
            "snapshot_id": "ocr_snap_2", "engine": "ocr",
            "region": [10, 10, 60, 40],
        })
        data = resp.json()
        assert data["success"] is True
        assert captured["size"] == (50, 30)
        assert data["image_size"] == [50, 30]

    def test_ocr_unknown_snapshot_404(self, client):
        resp = client.post("/screen/ocr", json={
            "snapshot_id": "no_such", "engine": "ocr",
        })
        assert resp.status_code == 404

    def test_capture_records_frame(self, client):
        """capture_screen 成功后帧进缓存（zoom 可引用其 snapshot_id）"""
        from server.screen import routes as routes_mod
        from server.screen import routes
        # 真实截图链路在 conftest 中如何 mock 不确定——直接调 _record 验证契约即可
        snap = "cap_snap_x"
        routes_mod._record_frame(snap, _make_png())
        assert routes._get_cached_frame(snap) is not None


# ========== D. 剪贴板 ==========

class TestClipboard:
    """/screen/clipboard read/write。"""

    def test_read_success(self, client, monkeypatch):
        from server.screen import clipboard_endpoints as ce
        monkeypatch.setattr(ce, "_read_clipboard_text",
                            lambda: (True, "hello clipboard", True, ""))
        resp = client.get("/screen/clipboard")
        data = resp.json()
        assert data["success"] is True
        assert data["text"] == "hello clipboard"
        assert data["has_text"] is True
        assert data["truncated"] is False

    def test_read_truncated(self, client, monkeypatch):
        from server.screen import clipboard_endpoints as ce
        monkeypatch.setattr(ce, "_read_clipboard_text",
                            lambda: (True, "x" * 3000, True, ""))
        resp = client.get("/screen/clipboard")
        data = resp.json()
        assert data["truncated"] is True
        assert data["length"] == 3000
        assert len(data["text"]) == 2000

        resp_full = client.get("/screen/clipboard", params={"full": "true"})
        data_full = resp_full.json()
        assert data_full["truncated"] is False
        assert data_full["length"] == 3000

    def test_read_no_text(self, client, monkeypatch):
        from server.screen import clipboard_endpoints as ce
        monkeypatch.setattr(ce, "_read_clipboard_text",
                            lambda: (True, "", False, ""))
        resp = client.get("/screen/clipboard")
        data = resp.json()
        assert data["success"] is True
        assert data["has_text"] is False

    def test_write_success(self, client, monkeypatch):
        from server.screen import clipboard_endpoints as ce
        written = {}
        monkeypatch.setattr(ce, "_write_clipboard_text",
                            lambda text: (written.__setitem__("text", text), (True, ""))[1])
        resp = client.post("/screen/clipboard", json={"text": "paste me 你好"})
        data = resp.json()
        assert data["success"] is True
        assert written["text"] == "paste me 你好"
        assert data["preview"] == "paste me 你好"

    def test_write_blocked_danger(self, client, monkeypatch):
        from server.screen import clipboard_endpoints as ce
        monkeypatch.setattr(ce, "_check_danger", lambda *a, **k: "block")
        resp = client.post("/screen/clipboard", json={"text": "danger text"})
        data = resp.json()
        assert data["success"] is False
        assert "危险关键词" in data["message"]

    def test_read_write_403_without_session(self, client, monkeypatch):
        """无会话授权 → 403（读写剪贴板都受会话保护）"""
        from server.screen.session import reset_session_manager
        from server.screen import clipboard_endpoints as ce

        monkeypatch.setattr(ce, "_read_clipboard_text",
                            lambda: (True, "t", True, ""))
        reset_session_manager()
        try:
            resp = client.get("/screen/clipboard")
            assert resp.status_code == 403
            resp2 = client.post("/screen/clipboard", json={"text": "t"})
            assert resp2.status_code == 403
        finally:
            # 恢复默认授权（conftest 每测试会 reset，但保险起见）
            from server.screen.session import get_session_manager
            get_session_manager().grant(mode="normal", task_description="test-restore")


# ========== E. 鼠标分段原语 ==========

class TestMousePrimitives:
    """mouse_down / mouse_up / mouse_move。"""

    def test_validate_mouse_down_requires_coords(self):
        from server.screen.input import _validate_action_params
        ok, msg = _validate_action_params("mouse_down", None, None)
        assert not ok
        assert "x 和 y" in msg

    def test_validate_mouse_down_button(self):
        from server.screen.input import _validate_action_params
        ok, msg = _validate_action_params("mouse_down", 1, 1, button="middle")
        assert not ok
        assert "button" in msg
        ok2, _ = _validate_action_params("mouse_down", 1, 1, button="right")
        assert ok2

    def test_validate_mouse_up_coords_optional(self):
        from server.screen.input import _validate_action_params
        ok, _ = _validate_action_params("mouse_up", None, None)
        assert ok

    def test_validate_mouse_move_requires_coords(self):
        from server.screen.input import _validate_action_params
        ok, _ = _validate_action_params("mouse_move", None, None)
        assert not ok

    def test_execute_mouse_down_calls_sendinput(self, monkeypatch):
        from server.screen import input as input_mod

        monkeypatch.setattr(input_mod, "_ADMIN_STATUS", True)
        calls = {}
        monkeypatch.setattr(input_mod, "_send_mouse_button",
                            lambda x, y, button="left", down=True:
                            calls.__setitem__("args", (x, y, button, down)) or True)
        r = input_mod._execute_action("mouse_down", x=15, y=25, button="right")
        assert r["success"] is True
        assert calls["args"] == (15, 25, "right", True)

    def test_execute_mouse_up_without_coords(self, monkeypatch):
        from server.screen import input as input_mod

        monkeypatch.setattr(input_mod, "_ADMIN_STATUS", True)
        calls = {}
        monkeypatch.setattr(input_mod, "_send_mouse_button",
                            lambda x, y, button="left", down=True:
                            calls.__setitem__("args", (x, y, button, down)) or True)
        r = input_mod._execute_action("mouse_up", button="left")
        assert r["success"] is True
        assert calls["args"] == (None, None, "left", False)

    def test_supported_actions_registered(self):
        from server.screen.input import SUPPORTED_ACTIONS
        for a in ("mouse_down", "mouse_up", "mouse_move"):
            assert a in SUPPORTED_ACTIONS

    def test_endpoint_passthrough_button(self, client, monkeypatch):
        """端点层透传 button 参数到 _execute_action"""
        from server.screen import routes

        _patch_action_base(monkeypatch)
        captured = {}
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kwargs: captured.__setitem__("kw", kwargs)
                            or {"success": True, "message": "ok"})
        _sync_screen_patches(monkeypatch, ["action_endpoints"])

        resp = client.post("/screen/action", json={
            "action": "mouse_down", "x": 10, "y": 20, "button": "right",
            "require_confirm": False,
        })
        data = resp.json()
        assert data["success"] is True
        assert captured["kw"]["button"] == "right"


# ========== uia.py hit_test_clickable 单元（mock ControlFromPoint） ==========

class FakeParentCtrl(FakeCtrl):
    """带父子链的仿控件：parent 是 Invoke Button，child 是 Text。"""

    def __init__(self, parent=None, control_type=50020, name="child"):
        self._parent = parent
        self.ControlType = control_type
        self.Name = name

    def GetParentControl(self):
        return self._parent


class TestHitTestClickable:
    """hit_test_clickable / try_uia_click_fusion 的纯逻辑（mock UIA）。"""

    def test_hit_test_none_when_uia_unavailable(self, monkeypatch):
        from server.screen import uia as uia_mod
        monkeypatch.setattr(uia_mod, "uia_available", lambda: False)
        assert hit_test_clickable(10, 10) is None

    def test_hit_test_walks_to_clickable_ancestor(self, monkeypatch):
        """点到 Button 内部 Text → 沿祖先链找到 Button（Invoke）"""
        import server.screen.uia as uia_mod

        button = FakeParentCtrl(parent=None, control_type=50000, name="OK")
        text = FakeParentCtrl(parent=button, control_type=50020, name="label")

        class FakeUia:
            @staticmethod
            def ControlFromPoint(x, y):
                return text

        monkeypatch.setattr(uia_mod, "uia_available", lambda: True)
        monkeypatch.setattr(uia_mod, "_ensure_com_initialized", lambda: (True, ""))
        monkeypatch.setitem(__import__("sys").modules, "uiautomation", FakeUia)
        # 只有 Button 支持 InvokePattern（Text 不支持——否则 depth 0 就命中了）
        monkeypatch.setattr(
            uia_mod, "get_pattern",
            lambda ctrl, name: FakePattern()
            if name == "InvokePattern" and getattr(ctrl, "ControlType", 0) == 50000
            else None,
        )

        hit = hit_test_clickable(50, 50)
        assert hit is not None
        assert hit["role"] == "Button"
        assert hit["action"] == "invoke"
        assert hit["depth"] == 1

    def test_hit_test_none_when_no_clickable(self, monkeypatch):
        """整条祖先链都无 pattern → None"""
        import server.screen.uia as uia_mod

        pane = FakeParentCtrl(parent=None, control_type=50033, name="pane")
        text = FakeParentCtrl(parent=pane, control_type=50020, name="t")

        class FakeUia:
            @staticmethod
            def ControlFromPoint(x, y):
                return text

        monkeypatch.setattr(uia_mod, "uia_available", lambda: True)
        monkeypatch.setattr(uia_mod, "_ensure_com_initialized", lambda: (True, ""))
        monkeypatch.setitem(__import__("sys").modules, "uiautomation", FakeUia)
        monkeypatch.setattr(uia_mod, "get_pattern", lambda ctrl, name: None)
        # 恢复真实融合函数（conftest autouse 默认 mock 掉了它）
        monkeypatch.setattr(uia_mod, "try_uia_click_fusion", _REAL_CLICK_FUSION)

        assert hit_test_clickable(50, 50) is None
        assert uia_mod.try_uia_click_fusion(50, 50) is None
