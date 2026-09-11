"""T2 测试：OcrDriver 接入 —— 门控 / 增强卸载 / 503 转译 / 消费方语义

设计真源：temp/sdd/model-lifecycle-manager/design.md §5.1/§7/§8
（继承 ocr-vram-guard §5.4 门控 / §5.5 增强卸载 / §6 消费方矩阵）

覆盖：
- get_ocr 门控三条硬规则（_unloading 拒绝 / 锁内复查 / 局部变量）
- 增强卸载（排空 / forced / 延迟补释放 / 结构化返回）
- ModelUnavailableError → 503 处理器
- _verify_postcondition "unavailable" 态
- desktop-transaction "unavailable" → committed_unverified 不回滚（P0 回归护栏）
- wait-for 快速失败（success=False + error_code）
- loop OCR 兜底拒绝计 skipped（不累计失败）
- OcrDriver 协议符合性与委托
"""

import asyncio
import io
import sys
import threading
import time
import types

import pytest

from server.model_manager.types import ModelDriver, ModelUnavailableError

# ==================== 测试辅助 ====================

class FakeManager:
    """伪造的模型存活管理器：记录 admit/成功/失败调用"""

    def __init__(self, refuse_reason=None, refuse_from_call=1):
        self.admit_calls = 0
        self.refuse_reason = refuse_reason
        self.refuse_from_call = refuse_from_call
        self.success = []
        self.failures = []

    def admit(self, model_id, manual=False):
        self.admit_calls += 1
        if self.refuse_reason and self.admit_calls >= self.refuse_from_call:
            raise ModelUnavailableError(model_id, self.refuse_reason)

    def note_load_success(self, model_id):
        self.success.append(model_id)

    def note_load_failure(self, model_id, detail=""):
        self.failures.append((model_id, detail))
        return {"fail_count": len(self.failures)}


def _make_manager(monkeypatch, fake_manager=None, load_raises=None):
    """构造带 FakePaddleOCR 的 ModelManager，并伪造管理器单例入口。

    Returns: (ModelManager 实例, FakeManager 实例, 构造计数字典)
    """
    from server.ocr import ModelManager

    constructed = {"count": 0}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            constructed["count"] += 1
            if load_raises is not None:
                raise load_raises

    fm = fake_manager or FakeManager()
    monkeypatch.setattr("server.model_manager.get_model_manager", lambda: fm)
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))

    m = ModelManager()
    monkeypatch.setattr(m, "_ensure_env", lambda: None)
    monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
    monkeypatch.setattr(m, "_detect_paddle_device", lambda: "cpu")
    return m, fm, constructed


def _fake_paddle_module(cuda=False, recorder=None):
    """伪造 paddle 模块：避免单测触碰真实 CUDA"""
    def empty_cache():
        if recorder is not None:
            recorder.append("empty_cache")
    return types.SimpleNamespace(
        is_compiled_with_cuda=lambda: cuda,
        device=types.SimpleNamespace(cuda=types.SimpleNamespace(empty_cache=empty_cache)),
    )


def _run(coro):
    return asyncio.run(coro)


# ==================== get_ocr 门控 ====================

class TestGetOcrGate:
    """design §5.4 三条硬规则 + 管理器失败语义"""

    def test_admit_allowed_loads_and_fast_path_skips_admit(self, monkeypatch):
        m, fm, constructed = _make_manager(monkeypatch)
        engine = m.get_ocr()
        assert engine is not None
        # 冷启动路径：外层准入 + 锁内复查各一次（design §5.4 硬规则 2）
        assert fm.admit_calls == 2
        assert fm.success == ["ocr"]
        assert m._loaded_at is not None
        # 快速路径：已加载且非卸载窗口 → 不再 admit
        assert m.get_ocr() is engine
        assert fm.admit_calls == 2
        assert constructed["count"] == 1

    def test_admit_refused_raises_and_no_load(self, monkeypatch):
        fm = FakeManager(refuse_reason="pressure_refusing")
        m, fm, constructed = _make_manager(monkeypatch, fake_manager=fm)
        with pytest.raises(ModelUnavailableError) as exc_info:
            m.get_ocr()
        assert exc_info.value.reason == "pressure_refusing"
        assert exc_info.value.model_id == "ocr"
        assert constructed["count"] == 0
        assert m._ocr_engine is None

    def test_in_lock_recheck_refusal(self, monkeypatch):
        """第一次 admit 放行、锁内复查（第二次）拒绝 → 仍抛异常"""
        fm = FakeManager(refuse_reason="pressure_refusing", refuse_from_call=2)
        m, fm, constructed = _make_manager(monkeypatch, fake_manager=fm)
        with pytest.raises(ModelUnavailableError):
            m.get_ocr()
        assert constructed["count"] == 0

    def test_load_failure_notifies_manager(self, monkeypatch):
        m, fm, _ = _make_manager(monkeypatch, load_raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            m.get_ocr()
        assert len(fm.failures) == 1
        model_id, detail = fm.failures[0]
        assert model_id == "ocr"
        assert "boom" in detail
        assert fm.success == []

    def test_unloading_window_rejects_even_with_engine(self, monkeypatch):
        """硬规则 1：_unloading 置位后新获取一律拒绝（即使引擎对象还在）"""
        m, fm, _ = _make_manager(monkeypatch)
        m.get_ocr()
        m._unloading = True
        with pytest.raises(ModelUnavailableError) as exc_info:
            m.get_ocr()
        assert exc_info.value.reason == "unloading_in_progress"

    def test_real_manager_unmanaged_model_allowed(self, monkeypatch):
        """向后兼容：OcrDriver 注册前（T5 之前），真实管理器对 'ocr' 放行（unmanaged_model）"""
        import server.model_manager as mm

        mm.reset_model_manager()
        try:
            m, _, constructed = _make_manager(monkeypatch)
            # 还原真实 get_model_manager（撤销 _make_manager 的伪造）
            monkeypatch.setattr("server.model_manager.get_model_manager", mm.get_model_manager)
            engine = m.get_ocr()
            assert engine is not None
            assert constructed["count"] == 1
        finally:
            mm.reset_model_manager()


# ==================== 增强卸载 ====================

class TestEnhancedUnload:
    """design §8（继承 ocr-vram-guard §5.5）：排空 + del + gc + empty_cache + forced"""

    def test_unload_returns_struct_and_resets(self, monkeypatch):
        m, _, _ = _make_manager(monkeypatch)
        monkeypatch.setitem(sys.modules, "paddle", _fake_paddle_module(cuda=False))
        m.get_ocr()
        assert m.ocr_loaded is True
        result = m.unload_ocr()
        assert result["unloaded"] is True
        assert result["forced"] is False
        assert m._ocr_engine is None
        assert m._loaded_at is None
        assert m.cold_start is True
        assert m._unloading is False  # finally 清理

    def test_unload_when_not_loaded(self, monkeypatch):
        m, _, _ = _make_manager(monkeypatch)
        result = m.unload_ocr()
        assert result["unloaded"] is True
        assert result["detail"] == "already_unloaded"

    def test_unload_reentry_returns_already_unloading(self, monkeypatch):
        m, _, _ = _make_manager(monkeypatch)
        m.get_ocr()
        m._unloading = True  # 模拟另一线程正在卸载
        result = m.unload_ocr()
        assert result["unloaded"] is False
        assert result["detail"] == "already_unloading"

    def test_unload_drains_in_flight(self, monkeypatch):
        """排空成功：在途推理结束后才卸载，forced=False"""
        m, _, _ = _make_manager(monkeypatch)
        monkeypatch.setitem(sys.modules, "paddle", _fake_paddle_module(cuda=False))
        m.get_ocr()
        holder_ready = threading.Event()
        released = []

        def hold():
            with m.in_flight_scope():
                holder_ready.set()
                time.sleep(0.5)
            released.append(True)

        t = threading.Thread(target=hold)
        t.start()
        assert holder_ready.wait(2)
        t0 = time.perf_counter()
        result = m.unload_ocr(drain_timeout=5.0)
        elapsed = time.perf_counter() - t0
        assert result["forced"] is False
        assert elapsed >= 0.3  # 确实等待了排空
        assert released  # 在途推理跑完后才卸载返回
        t.join(2)

    def test_unload_forced_on_drain_timeout(self, monkeypatch):
        """排空超时 → forced=True + 安排延迟补释放"""
        m, _, _ = _make_manager(monkeypatch)
        monkeypatch.setitem(sys.modules, "paddle", _fake_paddle_module(cuda=False))
        m.get_ocr()
        scheduled = []
        monkeypatch.setattr(m, "_schedule_forced_release", lambda delay=30.0: scheduled.append(delay))
        holder_ready = threading.Event()

        def hold():
            with m.in_flight_scope():
                holder_ready.set()
                time.sleep(1.5)

        t = threading.Thread(target=hold)
        t.start()
        assert holder_ready.wait(2)
        result = m.unload_ocr(drain_timeout=0.3)
        assert result["forced"] is True
        assert scheduled == [30.0]
        t.join(3)

    def test_release_paddle_cache_calls_empty_cache_when_cuda(self, monkeypatch):
        m, _, _ = _make_manager(monkeypatch)
        recorder = []
        monkeypatch.setitem(sys.modules, "paddle", _fake_paddle_module(cuda=True, recorder=recorder))
        m._release_paddle_cache(forced=False)
        assert recorder == ["empty_cache"]

    def test_release_paddle_cache_cpu_only_safe(self, monkeypatch):
        """无 paddle 环境安全降级（不抛异常）"""
        m, _, _ = _make_manager(monkeypatch)
        monkeypatch.setitem(sys.modules, "paddle", None)  # import paddle → ImportError
        m._release_paddle_cache(forced=False)  # 不应抛出

    def test_in_flight_scope_counts_with_exception(self, monkeypatch):
        m, _, _ = _make_manager(monkeypatch)
        assert m.in_flight_count() == 0
        with m.in_flight_scope():
            assert m.in_flight_count() == 1
        assert m.in_flight_count() == 0
        with pytest.raises(RuntimeError), m.in_flight_scope():
            raise RuntimeError("x")
        assert m.in_flight_count() == 0  # finally 递减


# ==================== OcrDriver ====================

class TestOcrDriver:
    def test_protocol_conformance(self):
        from server.model_manager.drivers import OcrDriver
        assert isinstance(OcrDriver(), ModelDriver)

    def test_static_attributes(self):
        from server.model_manager.drivers import OcrDriver
        d = OcrDriver()
        assert d.model_id == "ocr"
        assert d.resource == "gpu"
        assert d.evictable is True
        assert d.priority > 0
        assert d.footprint_mb > 0

    def test_delegates_to_module_singleton(self, monkeypatch):
        from server.model_manager.drivers import OcrDriver

        calls = []

        class FakeModels:
            ocr_loaded = True
            _loaded_at = 123.0
            _load_elapsed_ms = 456

            def get_ocr(self):
                calls.append("load")

            def unload_ocr(self):
                calls.append("unload")
                return {"unloaded": True, "forced": False, "detail": "ok"}

            def in_flight_count(self):
                return 2

        monkeypatch.setattr("server.ocr.models", FakeModels())
        d = OcrDriver()
        assert d.is_loaded() is True
        d.load()
        d.unload()
        assert d.in_flight() == 2
        assert d.loaded_at() == 123.0
        assert d.load_duration_ms() == 456
        assert calls == ["load", "unload"]


# ==================== 503 转译 ====================

class TestModelUnavailable503:
    def test_handler_returns_503_structured(self):
        from server.main import model_unavailable_handler

        req = types.SimpleNamespace(url=types.SimpleNamespace(path="/ocr/file"))
        exc = ModelUnavailableError("ocr", "pressure_refusing")
        resp = _run(model_unavailable_handler(req, exc))
        assert resp.status_code == 503
        import json
        body = json.loads(resp.body)
        assert body["error"] == "model_unavailable"
        assert body["model_id"] == "ocr"
        assert body["reason"] == "pressure_refusing"
        assert "压力" in body["hint"]

    def test_handler_unknown_reason_generic_hint(self):
        from server.main import model_unavailable_handler

        req = types.SimpleNamespace(url=types.SimpleNamespace(path="/x"))
        exc = ModelUnavailableError("ocr", "some_new_reason")
        resp = _run(model_unavailable_handler(req, exc))
        import json
        body = json.loads(resp.body)
        assert resp.status_code == 503
        assert body["hint"]  # 兜底提示非空


# ==================== _verify_postcondition "unavailable" ====================

class TestVerifyPostconditionUnavailable:
    def test_unavailable_on_manager_refusal(self, monkeypatch):
        from server.screen.action_endpoints import _verify_postcondition

        def raiser(img, **kw):
            raise ModelUnavailableError("ocr", "pressure_refusing")

        monkeypatch.setattr("server.ocr._do_ocr", raiser)
        result = _verify_postcondition({"type": "ocr_contains", "text": "x"}, object())
        assert result == "unavailable"

    def test_generic_error_still_error(self, monkeypatch):
        """真异常仍是 'error'（与 'unavailable' 区分）"""
        from server.screen.action_endpoints import _verify_postcondition

        def raiser(img, **kw):
            raise RuntimeError("ocr crashed")

        monkeypatch.setattr("server.ocr._do_ocr", raiser)
        result = _verify_postcondition({"type": "ocr_contains", "text": "x"}, object())
        assert result == "error"


# ==================== desktop-transaction 不回滚（P0 回归护栏） ====================

def _patch_transaction_common(monkeypatch):
    """desktop-transaction 端点通用 mock（沿用 test_screen.py 的 dry-run 测试姿势）"""
    from server.screen import routes

    monkeypatch.setattr(routes, "_ADMIN_STATUS", True)
    monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
    monkeypatch.setattr(
        "server.config.get_screen_config",
        lambda: {"focus_protection_enabled": False, "protected_processes": None},
    )
    monkeypatch.setattr(routes, "_find_window", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_force_focus_window", lambda hwnd: True)
    monkeypatch.setattr("win32gui.GetWindowRect", lambda hwnd: (0, 0, 1000, 1000), raising=False)

    class FakeGui:
        overlay_visible = False
        def show_overlay(self): pass
        def hide_overlay(self): pass
    monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)


def _sync_patches(monkeypatch):
    """把 routes 上的补丁同步到 desktop_transaction_endpoints（复用 test_screen 的机制）"""
    from server.screen import routes
    symbols = (
        "_execute_action", "_validate_action_params", "_check_danger",
        "_verify_postcondition", "_capture_fullscreen", "_capture_window",
        "_ADMIN_STATUS", "emergency", "_ensure_takeover_approved",
        "_canonical_for_response",
    )
    import server.screen.desktop_transaction_endpoints as dte
    for sym in symbols:
        val = getattr(routes, sym, None)
        if val is None:
            continue
        monkeypatch.setattr(dte, sym, val, raising=False)


def _fake_png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    return buf.getvalue()


class TestTransactionUnavailableNoRollback:
    """P0：OCR 不可用 ≠ 后验失败 —— 动作已执行，不回滚"""

    def test_unavailable_commits_without_rollback(self, client, monkeypatch):
        from server.screen import routes

        _patch_transaction_common(monkeypatch)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kw: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_capture_window", lambda hwnd: _fake_png_bytes())
        monkeypatch.setattr(routes, "_capture_fullscreen", lambda: _fake_png_bytes())
        from PIL import Image as _PILImage
        monkeypatch.setattr(_PILImage, "open", lambda *a, **kw: _PILImage.new("RGB", (8, 8)))
        monkeypatch.setattr(routes, "_verify_postcondition", lambda expected, img: "unavailable")
        _sync_patches(monkeypatch)

        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "已提交"},
            "rollback_policy": "auto",
            "rollback_actions": [{"action": "wait", "wait": 0.01}],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "committed_unverified"
        assert data["success"] is True
        assert data["transaction_postcondition"] == "unavailable"
        assert data["rollback_executed"] is False  # 不回滚（P0 核心断言）
        assert data["rollback_results"] == []
        assert data["executed_steps"] == 1

    def test_failed_postcondition_still_rolls_back(self, client, monkeypatch):
        """对照组：真后验失败仍触发回滚（语义未被新分支破坏）"""
        from server.screen import routes

        _patch_transaction_common(monkeypatch)
        monkeypatch.setattr(routes, "_execute_action",
                            lambda **kw: {"success": True, "message": "ok"})
        monkeypatch.setattr(routes, "_capture_window", lambda hwnd: _fake_png_bytes())
        from PIL import Image as _PILImage
        monkeypatch.setattr(_PILImage, "open", lambda *a, **kw: _PILImage.new("RGB", (8, 8)))
        monkeypatch.setattr(routes, "_verify_postcondition", lambda expected, img: "failed")
        _sync_patches(monkeypatch)

        resp = client.post("/screen/desktop-transaction", json={
            "target": {"hwnd": 12345},
            "actions": [{"action": "click", "x": 100, "y": 100}],
            "expected": {"type": "ocr_contains", "text": "已提交"},
            "rollback_policy": "auto",
            "rollback_actions": [{"action": "wait", "wait": 0.01}],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rolled_back"
        assert data["success"] is False
        assert data["rollback_executed"] is True
        assert data["rollback_steps_succeeded"] == 1


# ==================== wait-for 快速失败 ====================

class TestWaitForFastFail:
    def _patch_wait_common(self, monkeypatch):
        from server.screen import routes
        monkeypatch.setattr(routes.emergency, "can_operate", lambda: True)
        import server.screen.scroll_wait_analyze_endpoints as swe
        monkeypatch.setattr(swe, "_capture_fullscreen", lambda: _fake_png_bytes())

        class FakeGui:
            overlay_visible = False
            def show_overlay(self): pass
            def hide_overlay(self): pass
        monkeypatch.setattr("server.overlay_client.overlay_client", FakeGui(), raising=False)
        return swe

    def test_ocr_unavailable_fast_fails(self, client, monkeypatch):
        self._patch_wait_common(monkeypatch)
        from PIL import Image as _PILImage
        monkeypatch.setattr(_PILImage, "open", lambda *a, **kw: _PILImage.new("RGB", (8, 8)))

        def raiser(img, **kw):
            raise ModelUnavailableError("ocr", "pressure_refusing")
        monkeypatch.setattr("server.ocr._do_ocr", raiser)

        t0 = time.perf_counter()
        resp = client.post("/screen/wait-for", json={
            "expected": {"type": "ocr_contains", "text": "加载完成"},
            "timeout": 5.0, "interval": 0.2, "mode": "fullscreen",
        })
        elapsed = time.perf_counter() - t0
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False  # 不得复用超时的 success=True 语义
        assert data["condition_met"] is False
        assert data["error_code"] == "ocr_unavailable"
        assert elapsed < 3.0  # 快速失败，不烧满 timeout
        assert data["check_count"] == 1

    def test_generic_ocr_error_keeps_timeout_semantics(self, client, monkeypatch):
        """回归护栏：普通异常仍走原逻辑（重试至超时，超时的 success=True 语义不变）"""
        self._patch_wait_common(monkeypatch)
        from PIL import Image as _PILImage
        monkeypatch.setattr(_PILImage, "open", lambda *a, **kw: _PILImage.new("RGB", (8, 8)))

        def raiser(img, **kw):
            raise RuntimeError("ocr crashed")
        monkeypatch.setattr("server.ocr._do_ocr", raiser)

        resp = client.post("/screen/wait-for", json={
            "expected": {"type": "ocr_contains", "text": "x"},
            "timeout": 0.5, "interval": 0.1, "mode": "fullscreen",
        })
        data = resp.json()
        assert data["success"] is True  # 超时语义保持（既有行为）
        assert data["condition_met"] is False
        assert data["error_code"] is None
        assert data["check_count"] >= 2  # 重试到超时而非快速失败


# ==================== loop OCR 兜底语义 ====================

class TestLoopOcrFallbackSemantics:
    def test_fallback_propagates_manager_refusal(self, monkeypatch):
        from server.activity_tracker.loop_actions import _ocr_image_fallback

        def raiser(img):
            raise ModelUnavailableError("ocr", "pressure_refusing")
        monkeypatch.setattr("server.ocr._run_classic_ocr", raiser)
        with pytest.raises(ModelUnavailableError):
            _ocr_image_fallback(object())

    def test_fallback_swallows_real_errors(self, monkeypatch):
        from server.activity_tracker.loop_actions import _ocr_image_fallback

        def raiser(img):
            raise RuntimeError("paddle crashed")
        monkeypatch.setattr("server.ocr._run_classic_ocr", raiser)
        assert _ocr_image_fallback(object()) is None

    def test_fallback_joins_texts(self, monkeypatch):
        from server.activity_tracker.loop_actions import _ocr_image_fallback

        monkeypatch.setattr("server.ocr._run_classic_ocr", lambda img: [
            {"text": "a"}, {"text": ""}, {"text": "b"},
        ])
        assert _ocr_image_fallback(object()) == "a | b"

    def _patch_screen_vl_common(self, monkeypatch, tmp_path, decision_allow):
        """ScreenVLDescribeAction.execute 通用 mock"""
        import server.activity_tracker.loop_actions as la
        from server.activity_tracker.vl_quota import vl_quota

        monkeypatch.setattr("server.screen._capture_fullscreen", lambda: _fake_png_bytes())
        monkeypatch.setattr("server.activity_tracker.activity_signal.compute_signal",
                            lambda raw_dir=None: {"state": "active"})

        if decision_allow:
            monkeypatch.setattr(vl_quota, "should_call", lambda signal: (True, "", {}))
        else:
            monkeypatch.setattr(vl_quota, "should_call",
                                lambda signal: (False, "pace", {"decision": "deny_pace"}))
        recorded = []
        monkeypatch.setattr(vl_quota, "record_call", lambda status: recorded.append(status))

        monkeypatch.setattr(la, "_get_system_load", lambda: (10.0, 10.0))

        context = {"config": {
            "raw_data_dir": str(tmp_path),
            "vl_retry_count": 1,
            "vl_retry_backoff_base": 0,  # 避免测试内真实等待
            "vl_ocr_fallback_enabled": True,
        }}
        return la, recorded, context

    def test_screen_vl_refusal_counts_skipped_not_failed(self, monkeypatch, tmp_path):
        """VL 失败 + OCR 被管理器拒绝 → skipped 三态（不累计失败，不自动暂停）"""
        la, recorded, context = self._patch_screen_vl_common(monkeypatch, tmp_path, decision_allow=True)

        # remote_vl 不可用 → 重试耗尽 → failed_after_retries
        # （available 是只读 property，直接替换模块级单例；execute 函数级 import 会取到假对象）
        monkeypatch.setattr("server.vl.vision.remote_vl", types.SimpleNamespace(
            available=False, reload_config=lambda: None,
        ))

        def raiser(img):
            raise ModelUnavailableError("ocr", "pressure_refusing")
        monkeypatch.setattr(la, "_ocr_image_fallback", raiser)

        result = _run(la.ScreenVLDescribeAction().execute(context))
        assert result["success"] is True
        assert result["skipped"] is True  # 三态：不计 fail_count
        assert result["skip_reason"].startswith("ocr_unavailable:")
        assert recorded == ["failed_after_retries"]  # VL 调用已发生，如实记录

    def test_screen_vl_deny_branch_refusal_stays_skipped(self, monkeypatch, tmp_path):
        """配额拒绝分支中 OCR 被管理器拒绝 → 保持 skipped（无兜底文本而已）"""
        la, recorded, context = self._patch_screen_vl_common(monkeypatch, tmp_path, decision_allow=False)

        def raiser(img):
            raise ModelUnavailableError("ocr", "pressure_refusing")
        monkeypatch.setattr(la, "_ocr_image_fallback", raiser)

        result = _run(la.ScreenVLDescribeAction().execute(context))
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["vl_status"] == "skipped_pace"
        assert result["ocr_fallback_used"] is False
