"""Vision 模块测试（OmniParser 已于 2026-07-31 移除，仅保留远程 VL）

测试覆盖：
- Pydantic 模型
- GET /vision/status 端点
- POST /vision/understand（VL 未启用/不可用/正常）
- POST /vision/locate（VL 未启用/找到/未找到）
- POST /vision/parse 应返回 404（端点已删除）
"""

from server.vl.vision import (
    LocateRequest,
    LocateResponse,
    UnderstandRequest,
    UnderstandResponse,
    VisionStatusResponse,
)

# ==================== Pydantic 模型 ====================

class TestPydanticModels:
    def test_vision_status_response(self):
        resp = VisionStatusResponse(
            vl_model_enabled=False,
            vl_provider="",
            vl_model="",
            vl_available=False,
        )
        assert resp.vl_model_enabled is False
        assert resp.vl_available is False

    def test_understand_request(self):
        req = UnderstandRequest(image="abc", question="What is this?")
        assert req.bbox is None

    def test_understand_response(self):
        resp = UnderstandResponse(success=True, answer="hello", elapsed_ms=50)
        assert resp.answer == "hello"

    def test_locate_request(self):
        req = LocateRequest(target="确认按钮", image="abc")
        assert req.window_title is None
        assert req.process_name is None

    def test_locate_response(self):
        resp = LocateResponse(success=True, found=True, x=10, y=20, elapsed_ms=30)
        assert resp.found is True
        assert resp.x == 10
        assert resp.y == 20


# ==================== GET /vision/status ====================

class TestVisionStatus:
    def test_returns_200(self, client):
        resp = client.get("/vision/status")
        assert resp.status_code == 200

    def test_response_schema(self, client):
        data = client.get("/vision/status").json()
        # OmniParser 字段应已移除
        assert "omniparser_enabled" not in data
        assert "icon_detect_loaded" not in data
        assert "icon_caption_loaded" not in data
        assert "keep_models" not in data
        assert "weights_dir" not in data
        # VL 字段应保留
        assert "vl_model_enabled" in data
        assert "vl_provider" in data
        assert "vl_model" in data
        assert "vl_available" in data

    def test_vl_fields_present(self, client):
        """VL 相关字段存在且有合理默认值"""
        data = client.get("/vision/status").json()
        assert isinstance(data["vl_model_enabled"], bool)
        assert isinstance(data["vl_available"], bool)
        assert isinstance(data["vl_provider"], str)
        assert isinstance(data["vl_model"], str)


# ==================== POST /vision/parse（端点已删除） ====================

class TestVisionParseRemoved:
    def test_parse_endpoint_returns_404(self, client):
        """OmniParser 移除后 /vision/parse 端点应返回 404"""
        resp = client.post("/vision/parse", json={"image": "abc"})
        assert resp.status_code == 404


# ==================== POST /vision/understand（VL 未启用） ====================

class TestVisionUnderstand:
    def test_vl_disabled_returns_503(self, client, monkeypatch):
        """VL 未启用时返回 503"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": False},
        )
        from server.vl import vision
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        resp = client.post("/vision/understand", json={
            "image": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "question": "What is this?",
        })
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "VL" in detail or "vl_enabled" in detail

    def test_missing_image_uses_fullscreen_source(self, client, monkeypatch):
        """省略 image 时按接口约定使用全屏截图来源。"""
        from unittest.mock import PropertyMock

        from PIL import Image

        from server.vl import vision

        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available", PropertyMock(return_value=True)
        )
        monkeypatch.setattr(
            vision, "_acquire_image", lambda *args, **kwargs: (Image.new("RGB", (8, 8), "white"), None)
        )
        monkeypatch.setattr(
            vision.remote_vl,
            "understand",
            lambda *args, **kwargs: {"status": "ok", "answer": "fullscreen"},
        )
        resp = client.post("/vision/understand", json={"question": "what?"})
        assert resp.status_code == 200
        assert resp.json()["answer"] == "fullscreen"

    def test_missing_question_returns_422(self, client):
        """缺 question 字段返回 422"""
        resp = client.post("/vision/understand", json={"image": "abc"})
        assert resp.status_code == 422

    def test_invalid_image_returns_400(self, client, monkeypatch):
        """VL 启用但图片无效时返回 400"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        from unittest.mock import PropertyMock

        from server.vl import vision
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available",
            PropertyMock(return_value=True),
        )
        resp = client.post("/vision/understand", json={
            "image": "not_valid_base64!!!",
            "question": "What?",
        })
        assert resp.status_code == 400

    def test_vl_unavailable_returns_503(self, client, monkeypatch):
        """VL 启用但所有 provider 不可用时返回 503"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        from unittest.mock import PropertyMock

        from server.vl import vision
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available",
            PropertyMock(return_value=False),
        )
        resp = client.post("/vision/understand", json={
            "image": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "question": "What?",
        })
        assert resp.status_code == 503
        assert "不可用" in resp.json()["detail"] or "provider" in resp.json()["detail"].lower()

    def test_bbox_is_not_forwarded_after_crop(self, client, monkeypatch, small_image_base64):
        """裁剪后的图片不能再携带原图 bbox，避免坐标系错配。"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        from unittest.mock import PropertyMock

        from server.vl import vision

        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available", PropertyMock(return_value=True)
        )
        captured = {}

        def fake_understand(img, question, bbox=None, timeout=300, use_case="vl_vision"):
            captured["size"] = img.size
            captured["bbox"] = bbox
            return {"status": "ok", "answer": "focused"}

        monkeypatch.setattr(vision.remote_vl, "understand", fake_understand)
        resp = client.post("/vision/understand", json={
            "image": small_image_base64,
            "bbox": [0.1, 0.1, 0.6, 0.8],
            "question": "What is in this region?",
        })
        assert resp.status_code == 200
        assert captured["bbox"] is None
        assert captured["size"][0] < 100 or captured["size"][1] < 300


# ==================== POST /vision/locate（VL 未启用） ====================

class TestVisionLocate:
    def test_vl_disabled_returns_503(self, client, monkeypatch):
        """VL 未启用时返回 503"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": False},
        )
        resp = client.post("/vision/locate", json={
            "target": "button",
            "image": "abc",
        })
        assert resp.status_code == 503

    def test_missing_target_returns_422(self, client):
        """缺 target 字段返回 422"""
        resp = client.post("/vision/locate", json={"image": "abc"})
        assert resp.status_code == 422

    def test_locate_found_true_returns_200(self, client, monkeypatch, small_image_base64):
        """VL 找到目标时返回 200 + 像素坐标"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        from unittest.mock import PropertyMock

        from server.vl import vision
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available",
            PropertyMock(return_value=True),
        )
        monkeypatch.setattr(vision.remote_vl, "locate", lambda img, target, timeout=300, use_case="vl_vision": {
            "status": "ok",
            "found": True,
            "x": 50,
            "y": 75,
            "nx": 500.0,
            "ny": 250.0,
            "image_size": [100, 300],
            "description": "下载按钮",
        })
        resp = client.post("/vision/locate", json={
            "target": "下载按钮",
            "image": small_image_base64,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["x"] == 50
        assert data["y"] == 75
        assert data["description"] == "下载按钮"

    def test_locate_found_false_returns_200(self, client, monkeypatch, small_image_base64):
        """VL 未找到目标时返回 200 + found=False"""
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {"vl_enabled": True},
        )
        from unittest.mock import PropertyMock

        from server.vl import vision
        monkeypatch.setattr(vision.remote_vl, "reload_config", lambda: None)
        monkeypatch.setattr(
            type(vision.remote_vl), "available",
            PropertyMock(return_value=True),
        )
        monkeypatch.setattr(vision.remote_vl, "locate", lambda img, target, timeout=300, use_case="vl_vision": {
            "status": "ok",
            "found": False,
            "description": "画面中未找到",
        })
        resp = client.post("/vision/locate", json={
            "target": "不存在的按钮",
            "image": small_image_base64,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is False
        assert data["description"] == "画面中未找到"


# ==================== POST /vision/models/*（端点已删除） ====================

class TestVisionModelsRemoved:
    def test_models_keep_returns_404(self, client):
        resp = client.post("/vision/models/keep?keep=true")
        assert resp.status_code == 404

    def test_unload_models_returns_404(self, client):
        resp = client.post("/vision/models/unload")
        assert resp.status_code == 404

    def test_preload_models_returns_404(self, client):
        resp = client.post("/vision/models/preload")
        assert resp.status_code == 404

    def test_download_weights_returns_404(self, client):
        resp = client.post("/vision/weights/download")
        assert resp.status_code == 404
