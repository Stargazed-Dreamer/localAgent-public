"""Ticket T02 — /llm/pool/models 端点模型清单测试

验证：
1. pool.list_models() 返回正确结构（按 tier 分组 + default_tier + default_model）
2. grouped_by_tier 按 tier 正确分组，每个条目含 model + provider
3. GET /llm/pool/models 端点返回 200，body 含 grouped_by_tier 字段
4. 响应含 default_tier 和 default_model

Anti-Cheat 约束：
- list_models() 必须真跑，不 mock pool 返回假数据
- 用测试 LLMKey 构造 pool（测试数据初始化，非 mock 被测对象）
- TestClient 测试 mock is_initialized/get_pool 返回测试 pool，但 pool.list_models() 真跑
"""

import pytest

from server.llm_pool import LLMKey, LLMPool, ProviderPolicy
from server.llm_pool.types import TIER_NAMES

# ==================== 测试 fixtures ====================

def _make_test_key(
    name: str,
    base_url: str,
    models_with_tier: list[tuple[str, int]],
    protocol: str = "openai",
    privacy_warning: str = "",
    is_free: bool = False,
) -> LLMKey:
    """构造测试用 LLMKey

    Args:
        models_with_tier: [(model_name, tier), ...]
    """
    models = [m for m, _ in models_with_tier]
    model_tiers = dict(models_with_tier)
    return LLMKey(
        key=f"sk-test-{name}",
        base_url=base_url,
        models=models,
        name=name,
        model_tiers=model_tiers,
        default_model=models[0] if models else "",
        protocol=protocol,
        privacy_warning=privacy_warning,
        is_free=is_free,
    )


def _make_test_pool(keys: list[LLMKey]) -> LLMPool:
    """构造测试用 LLMPool（用 ProviderPolicy 默认值，stats_file 用临时路径）"""
    import os
    import tempfile
    stats_file = os.path.join(tempfile.gettempdir(), f"test_llm_pool_models_{os.getpid()}.json")
    return LLMPool(keys=keys, stats_file=stats_file, default_policy=ProviderPolicy(name="test"))


@pytest.fixture
def test_pool():
    """构造含 3 个 key、覆盖 tier 1/2/3/4/5 的测试池"""
    keys = [
        _make_test_key(
            "provider-a",
            "https://api.a.com/v1",
            [
                ("cheap-a", 1),
                ("light-a", 2),
                ("medium-a", 3),
            ],
            is_free=True,
        ),
        _make_test_key(
            "provider-b",
            "https://api.b.com/v1",
            [
                ("medium-b", 3),
                ("strong-b", 4),
            ],
        ),
        _make_test_key(
            "provider-c",
            "https://api.c.com/v1",
            [
                ("powerful-c", 5),
            ],
        ),
    ]
    return _make_test_pool(keys)


# ==================== pool.list_models() 单元测试 ====================

class TestListModelsMethod:
    """测试 LLMPool.list_models() 方法"""

    def test_list_models_method_returns_correct_structure(self, test_pool):
        """list_models() 返回 dict，含 grouped_by_tier / default_tier / default_model"""
        result = test_pool.list_models()

        assert isinstance(result, dict)
        # 必需字段
        assert "grouped_by_tier" in result, "响应必须含 grouped_by_tier 字段"
        assert "default_tier" in result, "响应必须含 default_tier 字段"
        assert "default_model" in result, "响应必须含 default_model 字段"

        # grouped_by_tier 是 dict
        grouped = result["grouped_by_tier"]
        assert isinstance(grouped, dict), "grouped_by_tier 必须是 dict"

        # 至少有一个 tier 分组
        assert len(grouped) > 0, "grouped_by_tier 不能为空"

        # 每个分组是 list
        for tier_name, entries in grouped.items():
            assert isinstance(tier_name, str), f"tier 名必须是 str: {tier_name}"
            assert isinstance(entries, list), f"tier {tier_name} 的条目必须是 list"
            # 每个条目含 model + provider
            for entry in entries:
                assert isinstance(entry, dict), "model 条目必须是 dict"
                assert "model" in entry, f"条目缺少 model 字段: {entry}"
                assert "provider" in entry, f"条目缺少 provider 字段: {entry}"

    def test_list_models_grouped_by_tier(self, test_pool):
        """grouped_by_tier 按 tier 正确分组（tier 1 model 在 tier1-lightest 下，等）"""
        result = test_pool.list_models()
        grouped = result["grouped_by_tier"]

        # tier 1 model 应在 "tier1-lightest" 下
        tier1_models = grouped.get("tier1-lightest", [])
        tier1_model_names = [e["model"] for e in tier1_models]
        assert "cheap-a" in tier1_model_names, f"tier1-lightest 应含 cheap-a: {tier1_models}"

        # tier 3 model 应在 "tier3-medium" 下（来自两个 provider）
        tier3_models = grouped.get("tier3-medium", [])
        tier3_model_names = [e["model"] for e in tier3_models]
        assert "medium-a" in tier3_model_names, f"tier3-medium 应含 medium-a: {tier3_models}"
        assert "medium-b" in tier3_model_names, f"tier3-medium 应含 medium-b: {tier3_models}"

        # tier 5 model 应在 "tier5-powerful" 下
        tier5_models = grouped.get("tier5-powerful", [])
        tier5_model_names = [e["model"] for e in tier5_models]
        assert "powerful-c" in tier5_model_names, f"tier5-powerful 应含 powerful-c: {tier5_models}"

        # 验证条目含 provider 字段
        for entry in tier1_models:
            if entry["model"] == "cheap-a":
                assert entry["provider"] == "provider-a", \
                    f"cheap-a 的 provider 应为 provider-a: {entry}"

    def test_list_models_default_tier_from_agent_chat(self, test_pool):
        """default_tier 取自 USE_CASE_REGISTRY['agent_chat'].default_tier 的 min

        agent_chat.default_tier = (3, 5)，min=3，对应 TIER_NAMES[3]='tier3-medium'
        """
        result = test_pool.list_models()
        # agent_chat default_tier min=3，对应 'tier3-medium'
        assert result["default_tier"] == "tier3-medium", \
            f"default_tier 应为 tier3-medium (agent_chat.min_tier): {result['default_tier']}"

    def test_list_models_default_model_in_default_tier(self, test_pool):
        """default_model 应是 default_tier 范围内的最低 tier model"""
        result = test_pool.list_models()
        default_tier = result["default_tier"]  # "tier3-medium"
        default_model = result["default_model"]

        # default_model 应在 default_tier 分组中
        tier_models = result["grouped_by_tier"].get(default_tier, [])
        tier_model_names = [e["model"] for e in tier_models]
        assert default_model in tier_model_names, \
            f"default_model {default_model} 应在 {default_tier} 分组中: {tier_model_names}"

    def test_list_models_tier_names_match_tier_names_dict(self, test_pool):
        """grouped_by_tier 的键名应与 TIER_NAMES 字典的值一致"""
        result = test_pool.list_models()
        grouped = result["grouped_by_tier"]

        # 所有 tier 名应在 TIER_NAMES 的值集合中
        valid_tier_names = set(TIER_NAMES.values())
        for tier_name in grouped:
            assert tier_name in valid_tier_names, \
                f"tier 名 {tier_name} 不在 TIER_NAMES 值集合中: {valid_tier_names}"


# ==================== GET /llm/pool/models 端点测试 ====================

class TestModelsEndpoint:
    """测试 GET /llm/pool/models 端点"""

    @pytest.fixture
    def mocked_pool(self, test_pool, monkeypatch):
        """mock server.llm_pool 模块的 is_initialized/get_pool，返回测试 pool

        list_models() 真跑（不 mock），只是 pool 的 keys 用测试数据
        """
        import server.llm_pool as llm_pool_mod
        monkeypatch.setattr(llm_pool_mod, "is_initialized", lambda: True)
        monkeypatch.setattr(llm_pool_mod, "get_pool", lambda: test_pool)
        return test_pool

    def test_models_endpoint_returns_200(self, client, mocked_pool):
        """GET /llm/pool/models 返回 200，body 含 grouped_by_tier"""
        resp = client.get("/llm/pool/models")
        assert resp.status_code == 200, f"状态码应为 200: {resp.status_code}, body={resp.text}"
        body = resp.json()
        assert "grouped_by_tier" in body, f"响应应含 grouped_by_tier: {body.keys()}"
        # 保持向后兼容：现有字段也应存在
        assert "models" in body, "响应应保留现有 models 字段（向后兼容）"

    def test_models_endpoint_grouped_by_tier_correct(self, client, mocked_pool):
        """端点返回的 grouped_by_tier 按 tier 正确分组"""
        resp = client.get("/llm/pool/models")
        assert resp.status_code == 200
        body = resp.json()
        grouped = body["grouped_by_tier"]

        # tier 1 应含 cheap-a
        tier1 = grouped.get("tier1-lightest", [])
        tier1_names = [e["model"] for e in tier1]
        assert "cheap-a" in tier1_names, f"tier1-lightest 应含 cheap-a: {tier1}"

        # tier 5 应含 powerful-c
        tier5 = grouped.get("tier5-powerful", [])
        tier5_names = [e["model"] for e in tier5]
        assert "powerful-c" in tier5_names, f"tier5-powerful 应含 powerful-c: {tier5}"

        # 每个条目含 model + provider
        for tier_name, entries in grouped.items():
            for entry in entries:
                assert "model" in entry, f"{tier_name} 条目缺 model: {entry}"
                assert "provider" in entry, f"{tier_name} 条目缺 provider: {entry}"

    def test_models_endpoint_default_tier_present(self, client, mocked_pool):
        """响应含 default_tier 和 default_model"""
        resp = client.get("/llm/pool/models")
        assert resp.status_code == 200
        body = resp.json()

        assert "default_tier" in body, f"响应应含 default_tier: {body.keys()}"
        assert "default_model" in body, f"响应应含 default_model: {body.keys()}"
        assert body["default_tier"] == "tier3-medium", \
            f"default_tier 应为 tier3-medium: {body['default_tier']}"
        # default_model 应非空
        assert body["default_model"], "default_model 不应为空"

    def test_models_endpoint_preserves_existing_fields(self, client, mocked_pool):
        """端点保留现有字段（向后兼容 GUI 监控面板）"""
        resp = client.get("/llm/pool/models")
        body = resp.json()

        # 现有字段保留（被 client/panels/llm_pool.py 使用）
        assert "initialized" in body, "响应应保留 initialized 字段"
        assert "models" in body, "响应应保留 models 字段（统计聚合，向后兼容）"
        assert "total_models" in body, "响应应保留 total_models 字段"
        assert body["initialized"] is True, "initialized 应为 True"

    def test_models_endpoint_when_not_initialized(self, client, monkeypatch):
        """池未初始化时返回 initialized=False（不报 500）"""
        import server.llm_pool as llm_pool_mod
        monkeypatch.setattr(llm_pool_mod, "is_initialized", lambda: False)
        resp = client.get("/llm/pool/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("initialized") is False
