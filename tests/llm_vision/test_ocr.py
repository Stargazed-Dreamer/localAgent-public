"""PaddleOCR 功能测试

测试 OCR 模块的状态查询、图片识别、模型管理等功能。
需要 PaddlePaddle + torch 环境（@pytest.mark.gpu）。
"""

import sys
import types

import pytest


def test_screenshot_ocr_disables_uvdoc_unwarping(monkeypatch):
    """Returned OCR boxes must stay in the source screenshot coordinate system."""
    from server.ocr import ModelManager

    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    manager = ModelManager()
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
    monkeypatch.setattr(manager, "_ensure_env", lambda: None)
    monkeypatch.setattr(manager, "_apply_pir_patch", lambda: None)
    monkeypatch.setattr(manager, "_detect_paddle_device", lambda: "cpu")

    manager.get_ocr()

    assert captured["use_doc_unwarping"] is False


@pytest.mark.gpu
class TestOCR:
    """PaddleOCR 功能测试"""

    def test_ocr_status(self, client):
        """GET /ocr/status 应返回状态"""
        resp = client.get("/ocr/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "vl_available" in data
        assert "ocr_loaded" in data
        assert "keep_models" in data

    def test_ocr_base64_small_image(self, client, small_image_base64):
        """OCR 对小图片应返回结果

        依赖 paddleocr + 完整的 cv2。conftest.py 的 gpu mark 检查 HAS_PADDLE_OCR
        （不只检查 torch），paddleocr/cv2 不可用时整个 TestOCR 类会 skip。
        """
        resp = client.post("/ocr/base64", data={"data": small_image_base64})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data.get("text", ""), str)

    def test_ocr_base64_invalid_data(self, client):
        """OCR 对无效base64应报错"""
        resp = client.post("/ocr/base64", data={"data": "not_valid_base64!!!"})
        assert resp.status_code == 400

    def test_ocr_path_not_found(self, client):
        """OCR 对不存在的路径应报错"""
        resp = client.post("/ocr/path/json", json={"path": "C:\\nonexistent_file_12345.png"})
        assert resp.status_code == 404

    def test_ocr_models_keep(self, client):
        """设置模型常驻内存"""
        resp = client.post("/ocr/models/keep/json", json={"keep": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_ocr_models_unload(self, client, monkeypatch):
        """卸载模型API应返回成功

        v0.41.0 起 unload 走 ModelLifecycleManager.manual_unload("ocr")，
        测试环境不跑 startup（registry 未注册 OcrDriver），需注入 FakeManager。
        """
        self._patch_mlm(monkeypatch)
        resp = client.post("/ocr/models/unload/json", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # 注意：卸载后重新加载可能因PaddlePaddle PIR bug失败
        # 不在这里预加载，避免PIR bug影响测试

    def test_ocr_models_unload_specific(self, client, monkeypatch):
        """卸载指定模型API应返回成功"""
        self._patch_mlm(monkeypatch)
        resp = client.post("/ocr/models/unload/json", json={"engine": "ocr"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    @staticmethod
    def _patch_mlm(monkeypatch):
        """注入 FakeManager：manual_unload 返回 ok（对齐 test_model_manager_t5_acceptance 模式）。"""

        class FakeManager:
            async def manual_unload(self, model_id):
                return {"status": "ok"}

        monkeypatch.setattr("server.model_manager.get_model_manager", lambda: FakeManager())


# ========== P1-A OCR 可观测性测试 ==========

class TestOCRObservability:
    """P1-A：OCR 可观测性字段测试（model_ready/cold_start/loading/failed + 加载/推理耗时）"""

    def test_initial_state_is_cold_start(self):
        """新 ModelManager 初始状态应为 cold_start=True, model_ready=False"""
        from server.ocr import ModelManager
        m = ModelManager()
        assert m.cold_start is True
        assert m.model_ready is False
        assert m.loading is False
        assert m.failed is False
        assert m._load_elapsed_ms is None
        assert m._last_inference_ms is None
        assert m._inference_count == 0
        assert m._last_error is None

    def test_record_inference_updates_count_and_latency(self):
        """record_inference 应累加推理次数并更新最近推理耗时"""
        from server.ocr import ModelManager
        m = ModelManager()
        m.record_inference(150)
        m.record_inference(200)
        assert m._inference_count == 2
        assert m._last_inference_ms == 200

    def test_get_ocr_success_sets_model_ready_and_load_elapsed(self, monkeypatch):
        """get_ocr 成功后 model_ready=True, load_elapsed_ms 非空, cold_start=False"""
        import sys
        import types

        from server.ocr import ModelManager

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                pass

        m = ModelManager()
        monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
        monkeypatch.setattr(m, "_ensure_env", lambda: None)
        monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
        monkeypatch.setattr(m, "_detect_paddle_device", lambda: "cpu")

        m.get_ocr()

        assert m.model_ready is True
        assert m.cold_start is False
        assert m.loading is False
        assert m.failed is False
        assert m._load_elapsed_ms is not None
        assert m._load_elapsed_ms >= 0
        assert m._last_error is None

    def test_get_ocr_failure_sets_failed_and_last_error(self, monkeypatch):
        """get_ocr 失败后 failed=True, last_error 非空, _load_elapsed_ms 非空"""
        import sys
        import types

        from server.ocr import ModelManager

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                raise RuntimeError("fake load failure")

        m = ModelManager()
        monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
        monkeypatch.setattr(m, "_ensure_env", lambda: None)
        monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
        monkeypatch.setattr(m, "_detect_paddle_device", lambda: "cpu")

        with pytest.raises(RuntimeError, match="fake load failure"):
            m.get_ocr()

        assert m.model_ready is False
        assert m.failed is True
        assert m._last_error is not None
        assert "fake load failure" in m._last_error
        assert m._load_elapsed_ms is not None

    def test_unload_resets_load_stats(self, monkeypatch):
        """unload_ocr 后应重置加载统计，使下次调用重新计为 cold_start"""
        import sys
        import types

        from server.ocr import ModelManager

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                pass

        m = ModelManager()
        monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
        monkeypatch.setattr(m, "_ensure_env", lambda: None)
        monkeypatch.setattr(m, "_apply_pir_patch", lambda: None)
        monkeypatch.setattr(m, "_detect_paddle_device", lambda: "cpu")

        m.get_ocr()
        assert m.cold_start is False
        assert m._load_elapsed_ms is not None

        m.unload_ocr()
        assert m.cold_start is True
        assert m.model_ready is False
        assert m._load_elapsed_ms is None
        assert m._load_started_at is None
        assert m._load_completed_at is None

    def test_ocr_status_endpoint_returns_p1a_fields(self, client):
        """GET /ocr/status 应返回 P1-A 可观测性字段"""
        resp = client.get("/ocr/status")
        assert resp.status_code == 200
        data = resp.json()
        # P1-A 新增字段
        for field in ("model_ready", "cold_start", "loading", "failed",
                      "load_elapsed_ms", "last_inference_ms", "inference_count", "last_error"):
            assert field in data, f"缺少 P1-A 字段: {field}"
        # 类型检查
        assert isinstance(data["model_ready"], bool)
        assert isinstance(data["cold_start"], bool)
        assert isinstance(data["loading"], bool)
        assert isinstance(data["failed"], bool)
        assert isinstance(data["inference_count"], int)
