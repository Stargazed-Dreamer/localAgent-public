"""后端健康检查端点测试

测试 /health 接口返回的状态信息，验证各模块状态字段是否存在。
"""



class TestHealth:
    """后端健康检查"""

    def test_health_endpoint(self, client):
        """GET /health 应返回状态"""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "uptime_seconds" in data

    def test_health_has_ocr_module(self, client):
        """health 应包含 ocr 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "ocr" in data
        assert "vl_available" in data["ocr"]
        assert "ocr_loaded" in data["ocr"]

    def test_health_has_agent_module(self, client):
        """health 应包含 agent 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "agent" in data
        assert "configured" in data["agent"]
        assert "models" in data["agent"]

    def test_health_has_browser_module(self, client):
        """health 应包含 browser 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "browser" in data
        assert "connected" in data["browser"]

    def test_health_has_exec_module(self, client):
        """health 应包含 exec 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "exec" in data
        assert "temp_dir" in data["exec"]

    def test_health_has_screen_module(self, client):
        """health 应包含 screen 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "screen" in data
        assert "capture_available" in data["screen"]

    def test_health_has_vision_module(self, client):
        """health 应包含 vision 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "vision" in data
        assert "vl_model_enabled" in data["vision"]
        # OmniParser 字段应已移除
        assert "omniparser_enabled" not in data["vision"]

    def test_health_vision_exposes_provider_fallback_fields(self, client):
        """health vision 应暴露 provider 级 fallback 状态字段

        覆盖 VL Provider 级额度耗尽 Fallback 改进的端到端暴露：
        - providers: 完整 provider 列表
        - provider_exhausted: 单 provider 耗尽状态 dict
        - daily_exhausted: 聚合状态（所有 provider 都耗尽时 True）
        - daily_exhausted_at / daily_exhausted_reason: 聚合状态元数据

        回归防护：spec Ticket 03 V5 health_status 曾因测试直接调
        vl_client.status() 而非 /health 端到端，导致 vl_status_fields()
        未透传 provider_exhausted 字段，/health 看不到 provider 状态。
        """
        resp = client.get("/health")
        data = resp.json()
        vision = data["vision"]
        assert "providers" in vision, "vision 应暴露 providers 列表"
        assert "provider_exhausted" in vision, "vision 应暴露 provider_exhausted dict"
        assert "daily_exhausted" in vision, "vision 应暴露 daily_exhausted 聚合状态"
        assert "daily_exhausted_at" in vision
        assert "daily_exhausted_reason" in vision
        # 类型契约
        assert isinstance(vision["providers"], list)
        assert isinstance(vision["provider_exhausted"], dict)
        assert isinstance(vision["daily_exhausted"], bool)

    def test_health_has_apikey_module(self, client):
        """health 应包含 apikey 模块状态"""
        resp = client.get("/health")
        data = resp.json()
        assert "apikey" in data
        assert "supported_vendors" in data["apikey"]
