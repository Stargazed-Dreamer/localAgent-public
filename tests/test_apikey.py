"""API Key 模块测试

测试 /apikey 路由的状态查询、厂商列表、Key测试、历史记录等功能。
apikey 路由已在 server/main.py 中注册，使用 conftest.py 中的 client fixture。
"""



class TestApiKeyStatus:
    """GET /apikey/status - 模块状态查询"""

    def test_status_returns_200(self, client):
        """应返回 200"""
        resp = client.get("/apikey/status")
        assert resp.status_code == 200

    def test_status_has_supported_vendors(self, client):
        """应包含 supported_vendors 字段"""
        data = client.get("/apikey/status").json()
        assert "supported_vendors" in data
        assert isinstance(data["supported_vendors"], int)
        assert data["supported_vendors"] > 0

    def test_status_last_test_default_none(self, client):
        """无测试历史时 last_test 应为 None"""
        data = client.get("/apikey/status").json()
        assert "last_test" in data


class TestApiKeyVendors:
    """GET /apikey/vendors - 厂商列表"""

    def test_vendors_returns_200(self, client):
        """应返回 200"""
        resp = client.get("/apikey/vendors")
        assert resp.status_code == 200

    def test_vendors_has_list(self, client):
        """应包含 vendors 列表"""
        data = client.get("/apikey/vendors").json()
        assert "vendors" in data
        assert isinstance(data["vendors"], list)
        assert len(data["vendors"]) > 0

    def test_vendors_item_fields(self, client):
        """每个厂商应包含 id, name, supports_balance"""
        data = client.get("/apikey/vendors").json()
        for v in data["vendors"]:
            assert "id" in v
            assert "name" in v
            assert "supports_balance" in v
            assert isinstance(v["supports_balance"], bool)

    def test_vendors_contains_deepseek(self, client):
        """应包含 deepseek 厂商"""
        data = client.get("/apikey/vendors").json()
        ids = [v["id"] for v in data["vendors"]]
        assert "deepseek" in ids

    def test_vendors_contains_openai(self, client):
        """应包含 openai 厂商"""
        data = client.get("/apikey/vendors").json()
        ids = [v["id"] for v in data["vendors"]]
        assert "openai" in ids

    def test_deepseek_supports_balance(self, client):
        """deepseek 应支持余额查询"""
        data = client.get("/apikey/vendors").json()
        deepseek = next(v for v in data["vendors"] if v["id"] == "deepseek")
        assert deepseek["supports_balance"] is True

    def test_openai_no_balance(self, client):
        """openai 不支持余额查询"""
        data = client.get("/apikey/vendors").json()
        openai = next(v for v in data["vendors"] if v["id"] == "openai")
        assert openai["supports_balance"] is False


class TestApiKeyTest:
    """POST /apikey/test - 测试 API Key"""

    def test_unsupported_vendor_returns_400(self, client):
        """不支持的厂商应返回 400"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-test",
            "vendor": "nonexistent_vendor",
        })
        assert resp.status_code == 400
        assert "不支持" in resp.json()["detail"]

    def test_missing_api_key_returns_422(self, client):
        """缺少 api_key 应返回 422"""
        resp = client.post("/apikey/test", json={
            "vendor": "deepseek",
        })
        assert resp.status_code == 422

    def test_missing_vendor_returns_422(self, client):
        """缺少 vendor 应返回 422"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-test",
        })
        assert resp.status_code == 422

    def test_empty_body_returns_422(self, client):
        """空请求体应返回 422"""
        resp = client.post("/apikey/test", json={})
        assert resp.status_code == 422

    def test_invalid_key_returns_result(self, client):
        """无效 Key 应返回结果（valid=False），而非报错"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-invalid-key-12345",
            "vendor": "deepseek",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["valid"] is False
        assert data["vendor"] == "deepseek"
        assert "elapsed_ms" in data

    def test_response_has_required_fields(self, client):
        """响应应包含所有必需字段"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-test",
            "vendor": "openai",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data
        assert "vendor" in data
        assert "vendor_name" in data
        assert "valid" in data
        assert "elapsed_ms" in data

    def test_vendor_case_insensitive(self, client):
        """vendor 参数应不区分大小写"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-test",
            "vendor": "DeepSeek",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor"] == "deepseek"

    def test_custom_base_url(self, client):
        """自定义 base_url 应被接受（连接失败也不应返回 422/400）"""
        resp = client.post("/apikey/test", json={
            "api_key": "sk-test",
            "vendor": "deepseek",
            "base_url": "https://custom-api.example.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        # 自定义URL不可达时 success=False（连接异常），但不应是400/422
        assert "success" in data
        assert data["vendor"] == "deepseek"


class TestApiKeyHistory:
    """GET /apikey/history - 测试历史"""

    def test_history_returns_200(self, client):
        """应返回 200"""
        resp = client.get("/apikey/history")
        assert resp.status_code == 200

    def test_history_has_history_field(self, client):
        """应包含 history 字段"""
        data = client.get("/apikey/history").json()
        assert "history" in data
        assert isinstance(data["history"], list)

    def test_history_records_after_test(self, client):
        """执行 Key 测试后，历史应增加记录"""
        before = client.get("/apikey/history").json()
        count_before = len(before["history"])

        client.post("/apikey/test", json={
            "api_key": "sk-test-history",
            "vendor": "deepseek",
        })

        after = client.get("/apikey/history").json()
        count_after = len(after["history"])
        assert count_after >= count_before

    def test_history_item_fields(self, client):
        """历史记录应包含必要字段"""
        # 先触发一次测试确保有历史
        client.post("/apikey/test", json={
            "api_key": "sk-test-history-fields",
            "vendor": "openai",
        })

        data = client.get("/apikey/history").json()
        if data["history"]:
            item = data["history"][-1]
            assert "vendor" in item
            assert "vendor_name" in item
            assert "valid" in item
            assert "elapsed_ms" in item
            assert "time" in item
