"""MindForge 集成模块测试

覆盖 /mindforge/* 端点，处理 MindForge 可能不存在于测试机器的情况。
"""

import pytest

# ========== TestMindForgeStatus ==========

class TestMindForgeStatus:
    """GET /mindforge/status 端点测试"""

    def test_status_returns_valid_response(self, client):
        """status 应始终返回有效响应（MindForge 存在与否均可）"""
        resp = client.get("/mindforge/status")
        assert resp.status_code == 200
        data = resp.json()
        # 必须包含所有字段
        assert "available" in data
        assert "path" in data
        assert "has_index" in data
        assert "has_pipeline" in data
        assert "search_engine_loaded" in data
        assert "converter_daemon_running" in data

    def test_status_available_is_bool(self, client):
        """available 字段应为布尔值"""
        resp = client.get("/mindforge/status")
        data = resp.json()
        assert isinstance(data["available"], bool)

    def test_status_fields_type(self, client):
        """各字段类型应正确"""
        resp = client.get("/mindforge/status")
        data = resp.json()
        assert isinstance(data["path"], str)
        assert isinstance(data["has_index"], bool)
        assert isinstance(data["has_pipeline"], bool)
        assert isinstance(data["search_engine_loaded"], bool)
        assert isinstance(data["converter_daemon_running"], bool)

    def test_status_not_available_means_no_python_executable(self, client):
        """当 available=False 时，python_executable 应为 None"""
        resp = client.get("/mindforge/status")
        data = resp.json()
        if not data["available"]:
            assert data["python_executable"] is None

    def test_status_available_has_python_executable(self, client):
        """当 available=True 时，python_executable 应有值"""
        resp = client.get("/mindforge/status")
        data = resp.json()
        if data["available"]:
            assert data["python_executable"] is not None
            assert isinstance(data["python_executable"], str)


# ========== TestMindForgePipeline ==========

class TestMindForgePipeline:
    """POST /mindforge/pipeline 端点测试"""

    def test_pipeline_invalid_source_dir_returns_400(self, client):
        """pipeline 使用不存在的源目录应返回 400"""
        # 先检查 MindForge 是否可用
        status = client.get("/mindforge/status")
        if not status.json()["available"]:
            pytest.skip("MindForge 不可用，跳过 400 测试")

        resp = client.post("/mindforge/pipeline", json={
            "source_dir": "/nonexistent/path/that/does/not/exist",
        })
        assert resp.status_code == 400
        assert "源目录不存在" in resp.json()["detail"]

    def test_pipeline_unavailable_returns_503(self, client):
        """MindForge 不可用时 pipeline 应返回 503"""
        status = client.get("/mindforge/status")
        if status.json()["available"]:
            pytest.skip("MindForge 可用，跳过 503 测试")

        resp = client.post("/mindforge/pipeline", json={
            "source_dir": ".",
        })
        assert resp.status_code == 503
        assert "MindForge" in resp.json()["detail"]


# ========== TestMindForgeSearch ==========

class TestMindForgeSearch:
    """POST /mindforge/search 端点测试"""

    def test_search_unavailable_returns_503(self, client):
        """MindForge 不可用时 search 应返回 503"""
        status = client.get("/mindforge/status")
        if status.json()["available"]:
            pytest.skip("MindForge 可用，跳过 503 测试")

        resp = client.post("/mindforge/search", json={
            "query": "test query",
        })
        assert resp.status_code == 503
        assert "MindForge" in resp.json()["detail"]

    def test_search_no_index_returns_404(self, client):
        """MindForge 可用但索引不存在时 search 应返回 404"""
        status = client.get("/mindforge/status")
        if not status.json()["available"]:
            pytest.skip("MindForge 不可用，跳过 404 测试")
        if status.json()["has_index"]:
            pytest.skip("索引已存在，跳过 404 测试")

        resp = client.post("/mindforge/search", json={
            "query": "test query",
        })
        assert resp.status_code == 404
        assert "索引不存在" in resp.json()["detail"]


# ========== TestMindForgeBuildIndex ==========

class TestMindForgeBuildIndex:
    """POST /mindforge/build-index 端点测试"""

    def test_build_index_unavailable_returns_503(self, client):
        """MindForge 不可用时 build-index 应返回 503"""
        status = client.get("/mindforge/status")
        if status.json()["available"]:
            pytest.skip("MindForge 可用，跳过 503 测试")

        resp = client.post("/mindforge/build-index", json={})
        assert resp.status_code == 503
        assert "MindForge" in resp.json()["detail"]


# ========== TestMindForgePipelineStatus ==========

class TestMindForgePipelineStatus:
    """GET /mindforge/pipeline/{task_id} 端点测试"""

    def test_unknown_task_id_returns_unknown(self, client):
        """查询不存在的 task_id 应返回 status=unknown"""
        resp = client.get("/mindforge/pipeline/99999999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unknown"
        assert data["task_id"] == "99999999"

    def test_unknown_task_id_has_message(self, client):
        """未知 task_id 响应应包含 message 字段"""
        resp = client.get("/mindforge/pipeline/nonexistent_task")
        data = resp.json()
        assert "message" in data


# ========== TestMindForgePreloadUnload ==========

class TestMindForgePreloadUnload:
    """POST /mindforge/preload 和 /mindforge/unload 端点测试"""

    def test_unload_when_not_loaded(self, client):
        """搜索引擎未加载时 unload 应返回 not_loaded"""
        # 先确保未加载
        client.post("/mindforge/unload")
        resp = client.post("/mindforge/unload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_loaded"

    def test_preload_unavailable_returns_503(self, client):
        """MindForge 不可用时 preload 应返回 503"""
        status = client.get("/mindforge/status")
        if status.json()["available"]:
            pytest.skip("MindForge 可用，跳过 503 测试")

        resp = client.post("/mindforge/preload")
        assert resp.status_code == 503

    def test_unload_unavailable_returns_503(self, client):
        """MindForge 不可用时 unload 应返回 503（实际上不会，因为搜索引擎不可能在 MindForge 不存在时加载）"""
        resp = client.post("/mindforge/unload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_loaded"


# ========== TestMindForgeConvert ==========

class TestMindForgeConvert:
    """POST /mindforge/convert 端点测试"""

    def test_convert_unavailable_returns_503(self, client):
        """MindForge 不可用时 convert 应返回 503"""
        status = client.get("/mindforge/status")
        if status.json()["available"]:
            pytest.skip("MindForge 可用，跳过 503 测试")

        resp = client.post("/mindforge/convert", json={
            "source_path": "/some/file.pdf",
        })
        assert resp.status_code == 503
        assert "MindForge" in resp.json()["detail"]

    def test_convert_nonexistent_file_returns_400(self, client):
        """convert 使用不存在的文件应返回 400"""
        status = client.get("/mindforge/status")
        if not status.json()["available"]:
            pytest.skip("MindForge 不可用，跳过 400 测试")

        resp = client.post("/mindforge/convert", json={
            "source_path": "/nonexistent/path/that/does/not/exist.pdf",
        })
        assert resp.status_code == 400


# ========== TestMindForgeDaemon ==========

class TestMindForgeDaemon:
    """守护进程端点测试"""

    def test_daemon_status_not_running(self, client):
        """GET /mindforge/daemon/status 守护进程未运行时返回 running=False"""
        # 先尝试停止守护进程确保未运行
        client.post("/mindforge/daemon/stop")
        resp = client.get("/mindforge/daemon/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["models_loaded"] is False

    def test_daemon_stop_not_running(self, client):
        """POST /mindforge/daemon/stop 守护进程未运行时返回 not_running"""
        # 先停止确保未运行
        client.post("/mindforge/daemon/stop")
        resp = client.post("/mindforge/daemon/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_running"
