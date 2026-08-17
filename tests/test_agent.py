"""LLM Agent 模块测试

测试 Agent 模块的状态查询、评分、对话等功能。
未配置 API Key 时应返回 400 错误。
"""

import pytest


class TestAgent:
    """LLM Agent 模块测试"""

    def test_agent_status(self, client):
        """GET /agent/status 应返回配置状态

        tier 名称映射（TIER_NAMES）：
          tier2-light    = 旧 cheap
          tier3-medium   = 旧 default
          tier5-powerful = 旧 powerful
        """
        resp = client.get("/agent/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "configured" in data
        assert "models" in data
        assert "tier2-light" in data["models"]
        assert "tier3-medium" in data["models"]
        assert "tier5-powerful" in data["models"]

    def test_agent_score_no_api_key(self, client):
        """未配置API Key时评分应报错"""
        # 先检查是否配置了API Key
        status = client.get("/agent/status").json()
        if status["configured"]:
            pytest.skip("已配置API Key，跳过无Key测试")
        resp = client.post("/agent/score", json={
            "content": "测试内容",
        })
        assert resp.status_code == 400

    def test_agent_chat_no_api_key(self, client):
        """未配置API Key时对话应报错"""
        status = client.get("/agent/status").json()
        if status["configured"]:
            pytest.skip("已配置API Key，跳过无Key测试")
        resp = client.post("/agent/chat", json={
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert resp.status_code == 400
