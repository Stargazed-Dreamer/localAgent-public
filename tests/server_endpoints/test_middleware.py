"""HTTP 中间件测试 — MCP 统计 + 用户消息注入

middleware 注册两个 http 中间件：
1. mcp_stats_middleware — 拦截 /mcp POST，记录工具调用统计到 mcp_stats
2. user_message_inject_middleware — 将待发送用户消息注入非工作端点响应

测试通过 TestClient 发送请求验证中间件行为。
"""


import pytest

import server.user_message as um

# ========== 用户消息注入中间件 ==========

class TestUserMessageInjection:
    """用户消息注入到非工作端点的 JSON 响应"""

    @pytest.fixture(autouse=True)
    def clean_messages(self):
        """每个测试前后清空消息"""
        um.clear_all()
        yield
        um.clear_all()

    def test_inject_into_json_response(self, client):
        """消息注入到 JSON 响应的 _user_supplement 字段"""
        um.add_message("开始新任务")
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "_user_supplement" in data
        assert "开始新任务" in data["_user_supplement"]

    def test_inject_header(self, client):
        """消息注入到 X-User-Supplement header"""
        um.add_message("header 测试")
        resp = client.get("/health")
        assert "x-user-supplement" in {k.lower() for k in resp.headers}

    def test_no_injection_when_no_messages(self, client):
        """无消息时不注入"""
        resp = client.get("/health")
        data = resp.json()
        assert "_user_supplement" not in data

    def test_no_injection_into_excluded_paths(self, client):
        """排除路径不注入"""
        um.add_message("不应出现")
        # /static 是排除路径
        resp = client.get("/static/index.html")
        # 即使返回 200/404，也不应有 _user_supplement
        if resp.headers.get("content-type", "").startswith("application/json"):
            assert "_user_supplement" not in resp.json()

    def test_injection_consumes_messages(self, client):
        """注入后消息被消费"""
        um.add_message("只出现一次")
        client.get("/health")  # 第一次注入
        resp = client.get("/health")  # 第二次不应有
        data = resp.json()
        assert "_user_supplement" not in data

    def test_inject_multiple_messages(self, client):
        """多条消息合并注入"""
        um.add_message("第一条")
        um.add_message("第二条")
        resp = client.get("/health")
        data = resp.json()
        supplement = data["_user_supplement"]
        assert "第一条" in supplement
        assert "第二条" in supplement


# ========== MCP 统计中间件 ==========

class TestMCpStatsMiddleware:
    """/mcp POST 请求的统计记录"""

    def test_mcp_post_records_stats(self, client, monkeypatch):
        """POST /mcp tools/call 触发统计记录"""
        recorded = []

        def fake_record_call(tool_name, duration_ms, success):
            recorded.append({
                "tool": tool_name,
                "duration_ms": duration_ms,
                "success": success,
            })

        # middleware 模块在模块级定义 record_call（原 mcp_stats.py 已归并），
        # 直接 patch middleware 模块中的引用即可
        monkeypatch.setattr("server.core.middleware.record_call", fake_record_call)

        # 模拟 MCP JSON-RPC tools/call 请求
        client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "test_tool", "arguments": {}},
            "id": 1,
        })

        # 中间件应记录了调用（即使端点返回错误）
        assert len(recorded) == 1
        assert recorded[0]["tool"] == "test_tool"
        assert recorded[0]["success"] in [True, False]

    def test_mcp_get_no_stats(self, client, monkeypatch):
        """GET /mcp 不记录统计"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.get("/mcp")
        assert len(recorded) == 0

    def test_non_mcp_path_no_stats(self, client, monkeypatch):
        """非 /mcp 路径不记录 MCP 统计"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.get("/health")
        client.post("/ocr/path/json", json={"path": "nonexistent.png"})
        assert len(recorded) == 0

    def test_mcp_non_tools_call_no_record(self, client, monkeypatch):
        """POST /mcp 但非 tools/call 方法不记录"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
            "id": 1,
        })
        assert len(recorded) == 0


# ========== 中间件集成 ==========

class TestMiddlewareIntegration:
    """中间件与实际端点的集成测试"""

    def test_health_works_with_middleware(self, client):
        """中间件不破坏正常请求"""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_supplement_does_not_break_response(self, client):
        """注入 _user_supplement 不破坏原有响应字段"""
        um.add_message("补充")
        resp = client.get("/health")
        data = resp.json()
        # 原有字段仍在
        assert data["status"] == "ok"
        # 补充字段也在
        assert "_user_supplement" in data
