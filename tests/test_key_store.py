"""v8 key_store 模块测试。

覆盖：
- USE_CASE_REGISTRY 完整性（17 个 use_case + tier 范围 + sensitive 标志）
- resolve_keys() 路由层筛选逻辑
"""

import pytest

from server import config
from server.llm_pool.key_store import (
    USE_CASE_REGISTRY,
    KeyRecord,
    ResolvedKey,
    UseCaseDef,
    resolve_key,
    resolve_keys,
)

# ==================== USE_CASE_REGISTRY ====================

@pytest.fixture(autouse=True)
def _disable_sensitive_privacy_override(monkeypatch):
    """路由测试不受本机隐私放行配置影响。"""
    monkeypatch.setattr(
        config,
        "get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )


class TestUseCaseRegistry:
    def test_registry_has_17_entries(self):
        """验证 17 个 use_case 全部存在。"""
        expected = {
            "agent_chat", "community_summarize", "memory_compress",
            "memory_validate", "hourly_summarize", "download_watcher",
            "vl_ocr", "vl_vision", "vl_activity_tracker",
            "command_guard", "aigc_image_gen", "aigc_image_edit",
            "aigc_video_gen",
            "stock_advisor_opening", "stock_advisor_closing",
            "stock_advisor_evening", "stock_advisor_query",
        }
        assert set(USE_CASE_REGISTRY.keys()) == expected
        assert len(USE_CASE_REGISTRY) == 17

    def test_sensitive_flags_are_correct(self):
        """验证 sensitive 标志：memory_compress/validate/download_watcher + VL 3 个 = 6 个 sensitive"""
        sensitive_cases = {name for name, uc in USE_CASE_REGISTRY.items() if uc.sensitive}
        expected_sensitive = {
            "memory_compress", "memory_validate", "download_watcher",
            "vl_ocr", "vl_vision", "vl_activity_tracker",
        }
        assert sensitive_cases == expected_sensitive

        non_sensitive = {name for name, uc in USE_CASE_REGISTRY.items() if not uc.sensitive}
        expected_non_sensitive = {
            "agent_chat", "community_summarize", "hourly_summarize",
            "command_guard", "aigc_image_gen", "aigc_image_edit",
            "aigc_video_gen",
            "stock_advisor_opening", "stock_advisor_closing",
            "stock_advisor_evening", "stock_advisor_query",
        }
        assert non_sensitive == expected_non_sensitive

    def test_entries_have_required_fields(self):
        """每条 UseCaseDef 含 name/scope/sensitive/default_tier/desc 字段"""
        for name, uc in USE_CASE_REGISTRY.items():
            assert isinstance(uc, UseCaseDef)
            assert uc.name == name
            assert uc.scope in ("llm", "vl", "aigc_image", "aigc_video")
            assert isinstance(uc.sensitive, bool)
            assert isinstance(uc.default_tier, tuple)
            assert len(uc.default_tier) == 2
            assert 1 <= uc.default_tier[0] <= uc.default_tier[1] <= 5
            assert isinstance(uc.desc, str) and len(uc.desc) > 0

    def test_vl_use_cases_all_sensitive(self):
        """VL 三个 use_case 全部 sensitive=true（预防性设计）"""
        vl_cases = {name: uc for name, uc in USE_CASE_REGISTRY.items() if uc.scope == "vl"}
        assert set(vl_cases.keys()) == {"vl_ocr", "vl_vision", "vl_activity_tracker"}
        for name, uc in vl_cases.items():
            assert uc.sensitive is True, f"{name} 应该是 sensitive"


# ==================== resolve_keys() 路由层 ====================

def _make_record(
    key_id: str = "test_key",
    label: str = "test",
    key: str = "sk-test",
    base_url: str = "https://api.test.com/v1",
    models: list = None,
    scope: list = None,
    privacy_warning: str = "",
    allowed_uses: list = None,
    enabled: bool = True,
    works: bool = True,
) -> KeyRecord:
    """构造测试用 KeyRecord"""
    model_scope = scope or ["llm"]
    default_tier = 5 if "vl" in model_scope else 3
    normalized_models = []
    for model in (models or ["test-model"]):
        if isinstance(model, dict):
            normalized_models.append(model)
        else:
            normalized_models.append({
                "name": model,
                "scope": list(model_scope),
                "tier": default_tier,
                "enabled": True,
            })
    return KeyRecord(
        id=key_id,
        label=label,
        key=key,
        base_url=base_url,
        models=normalized_models,
        privacy_warning=privacy_warning,
        allowed_uses=allowed_uses or [],
        enabled=enabled,
        status={"works": works, "fail_count": 0, "last_health_check": "",
                "last_check_status": "", "last_check_detail": ""},
    )


class TestResolveKeys:
    """resolve_keys() 路由筛选逻辑测试"""

    def test_returns_empty_for_unknown_use_case(self, monkeypatch):
        """未知 use_case 返回空列表（不抛异常）"""
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [_make_record()],
        )
        result = resolve_keys("unknown_use_case")
        assert result == []

    def test_returns_empty_when_no_matching_scope(self, monkeypatch):
        """use_case=vl_ocr 但无 scope 含 vl 的 key → 返回空"""
        llm_only_record = _make_record(scope=["llm"])
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [llm_only_record],
        )
        result = resolve_keys("vl_ocr")
        assert result == []

    def test_filters_sensitive_keys_with_privacy_warning(self, monkeypatch):
        """sensitive use_case + key 有 privacy_warning → 跳过该 key"""
        unsafe_key = _make_record(
            key_id="unsafe", privacy_warning="免费模型，数据可能被用于训练",
        )
        safe_key = _make_record(key_id="safe", base_url="https://api.safe.com/v1")
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [unsafe_key, safe_key],
        )
        # memory_compress 是 sensitive use_case
        result = resolve_keys("memory_compress")
        key_ids = [rk.key_id for rk in result]
        assert "unsafe" not in key_ids
        assert "safe" in key_ids

    def test_non_sensitive_use_case_keeps_unsafe_key(self, monkeypatch):
        """非 sensitive use_case（如 agent_chat）可使用带 privacy_warning 的 key"""
        unsafe_key = _make_record(
            key_id="unsafe", privacy_warning="免费模型",
        )
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [unsafe_key],
        )
        result = resolve_keys("agent_chat")
        assert len(result) == 1
        assert result[0].key_id == "unsafe"
        assert result[0].privacy_warning == "免费模型"

    def test_respects_allowed_uses_whitelist(self, monkeypatch):
        """key.allowed_uses 非空且不含 use_case → 跳过"""
        restricted_key = _make_record(
            key_id="restricted",
            allowed_uses=["agent_chat"],  # 只允许 agent_chat
        )
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [restricted_key],
        )
        # agent_chat 在 allowed_uses 中 → 通过
        assert len(resolve_keys("agent_chat")) == 1
        # community_summarize 不在 allowed_uses 中 → 被过滤
        assert resolve_keys("community_summarize") == []

    def test_skips_disabled_keys(self, monkeypatch):
        """enabled=False 的 key 被跳过"""
        disabled = _make_record(key_id="disabled", enabled=False)
        enabled = _make_record(key_id="enabled", base_url="https://api.enabled.com/v1")
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [disabled, enabled],
        )
        result = resolve_keys("agent_chat")
        key_ids = [rk.key_id for rk in result]
        assert "disabled" not in key_ids
        assert "enabled" in key_ids

    def test_skips_broken_keys(self, monkeypatch):
        """status.works=False 的 key 被跳过"""
        broken = _make_record(key_id="broken", works=False)
        working = _make_record(key_id="working", base_url="https://api.working.com/v1")
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [broken, working],
        )
        result = resolve_keys("agent_chat")
        key_ids = [rk.key_id for rk in result]
        assert "broken" not in key_ids
        assert "working" in key_ids

    def test_resolved_key_has_all_fields(self, monkeypatch):
        """ResolvedKey dataclass 含全部字段"""
        rec = _make_record(
            key_id="k1",
            label="MyKey",
            base_url="https://api.test.com/v1",
            models=["m1", "m2"],
        )
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [rec],
        )
        result = resolve_keys("agent_chat")
        assert len(result) == 1
        rk = result[0]
        assert isinstance(rk, ResolvedKey)
        assert rk.key_id == "k1"
        assert rk.api_key == "sk-test"
        assert rk.base_url == "https://api.test.com/v1"
        assert rk.label == "MyKey"
        assert rk.models == ["m1", "m2"]
        assert rk.model in ["m1", "m2"]  # _pick_model 选其中一个
        assert rk.source == "unified_json"


class TestResolveKey:
    """resolve_key() 便捷函数（取第一个）"""

    def test_returns_first_match(self, monkeypatch):
        """返回匹配的第一个 key"""
        rec1 = _make_record(key_id="first", base_url="https://api.first.com/v1")
        rec2 = _make_record(key_id="second", base_url="https://api.second.com/v1")
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [rec1, rec2],
        )
        result = resolve_key("agent_chat")
        assert result is not None
        assert result.key_id == "first"

    def test_returns_none_when_no_match(self, monkeypatch):
        """无匹配时返回 None"""
        monkeypatch.setattr(
            "server.llm_pool.key_store._load_keys_cached",
            lambda: [_make_record(scope=["llm"])],
        )
        # vl_ocr 需要 scope=vl，但只有 llm scope 的 key
        assert resolve_key("vl_ocr") is None


# ==================== v14 schema: pool 段 + 迁移 ====================

class TestKeyRecordPoolField:
    """v14 KeyRecord.pool 字段读写测试"""

    def test_pool_defaults_to_empty_dict(self):
        """新建 KeyRecord 的 pool 默认为空字典"""
        rec = _make_record()
        assert rec.pool == {}

    def test_to_record_reads_pool(self):
        """_to_record 从 dict 读取 pool 段"""
        from server.llm_pool.key_store import _to_record
        item = {
            "id": "test", "label": "test", "key": "sk-test",
            "base_url": "https://api.test.com/v1",
            "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}],
            "pool": {"retry_count": 10, "rate_limit_cooldown_seconds": 30.0},
        }
        rec = _to_record(item)
        assert rec.pool == {"retry_count": 10, "rate_limit_cooldown_seconds": 30.0}

    def test_to_record_pool_missing_is_empty(self):
        """_to_record 无 pool 段时为空字典"""
        from server.llm_pool.key_store import _to_record
        item = {
            "id": "test", "label": "test", "key": "sk-test",
            "base_url": "https://api.test.com/v1",
            "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}],
        }
        rec = _to_record(item)
        assert rec.pool == {}

    def test_to_record_pool_invalid_type_is_empty(self):
        """_to_record pool 非 dict 时为空字典"""
        from server.llm_pool.key_store import _to_record
        item = {
            "id": "test", "label": "test", "key": "sk-test",
            "base_url": "https://api.test.com/v1",
            "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}],
            "pool": "invalid",
        }
        rec = _to_record(item)
        assert rec.pool == {}

    def test_to_dict_writes_pool_when_non_empty(self):
        """_to_dict 非空 pool 时写入"""
        from server.llm_pool.key_store import _to_dict
        rec = _make_record()
        rec.pool = {"retry_count": 5}
        d = _to_dict(rec)
        assert "pool" in d
        assert d["pool"] == {"retry_count": 5}

    def test_to_dict_omits_pool_when_empty(self):
        """_to_dict 空 pool 时不写入（保持 schema 简洁）"""
        from server.llm_pool.key_store import _to_dict
        rec = _make_record()
        d = _to_dict(rec)
        assert "pool" not in d

    def test_pool_roundtrip(self):
        """pool 段 _to_record → _to_dict 往返一致"""
        from server.llm_pool.key_store import _to_dict, _to_record
        original = {
            "id": "test", "label": "test", "key": "sk-test",
            "base_url": "https://api.test.com/v1",
            "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}],
            "pool": {
                "retry_count": 4,
                "retry_base_delay": 2.0,
                "retry_max_delay": 30.0,
                "retry_jitter": 0.2,
                "rate_limit_cooldown_seconds": 5.0,
                "rate_limit_backoff_multiplier": 2.0,
                "rate_limit_max_cooldown": 300.0,
            },
        }
        rec = _to_record(original)
        d = _to_dict(rec)
        assert d["pool"] == original["pool"]


class TestMigrateV12ToV14:
    """v12/v13 → v14 迁移测试"""

    def test_migrate_v12_to_v14_upgrades_version(self):
        """v12 数据迁移到 v14"""
        from server.llm_pool.key_store import migrate_v12_to_v14
        data = {"version": 12, "keys": [{"id": "test", "key": "sk-test"}]}
        result = migrate_v12_to_v14(data)
        assert result["version"] == 14
        assert "updated_at" in result

    def test_migrate_v13_to_v14_upgrades_version(self):
        """v13 数据迁移到 v14（v12 和 v13 schema 等价）"""
        from server.llm_pool.key_store import migrate_v12_to_v14
        data = {"version": 13, "keys": [{"id": "test", "key": "sk-test"}]}
        result = migrate_v12_to_v14(data)
        assert result["version"] == 14

    def test_migrate_v12_to_v14_preserves_keys(self):
        """迁移不丢失 key 数据"""
        from server.llm_pool.key_store import migrate_v12_to_v14
        keys = [{"id": "a", "key": "sk-a"}, {"id": "b", "key": "sk-b"}]
        data = {"version": 12, "keys": keys}
        result = migrate_v12_to_v14(data)
        assert len(result["keys"]) == 2
        assert result["keys"][0]["id"] == "a"

    def test_migrate_v12_to_v14_idempotent(self):
        """迁移幂等：v14 数据不再迁移"""
        from server.llm_pool.key_store import _maybe_migrate
        data = {"version": 14, "keys": [{"id": "test", "key": "sk-test"}]}
        result = _maybe_migrate(data)
        assert result["version"] == 14

    def test_maybe_migrate_v12_to_v14(self, monkeypatch):
        """_maybe_migrate 将 v12 数据迁移到 v14"""
        from server.llm_pool import key_store
        from server.llm_pool.key_store import _maybe_migrate
        # mock 文件写入，避免破坏真实 keys.json
        monkeypatch.setattr(key_store, "_write_raw_keys_file", lambda *a, **kw: None)
        monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: type("P", (), {"exists": lambda self: False})())
        data = {
            "version": 12,
            "keys": [{"id": "test", "key": "sk-test", "base_url": "http://x",
                       "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}]}],
        }
        result = _maybe_migrate(data)
        assert result["version"] == 14

    def test_migrate_v12_to_v14_strips_limits(self):
        """v14 迁移剥离 limits 字段（key 级和 model 级）"""
        from server.llm_pool.key_store import migrate_v12_to_v14
        data = {
            "version": 12,
            "keys": [{
                "id": "test", "key": "sk-test", "base_url": "http://x",
                "limits": {"rpm": 20},
                "models": [{"name": "m", "scope": ["llm"], "tier": 3,
                            "limits": {"rpm": 10}, "enabled": True}],
            }],
        }
        result = migrate_v12_to_v14(data)
        key = result["keys"][0]
        assert "limits" not in key, "key 级 limits 应被剥离"
        assert "limits" not in key["models"][0], "model 级 limits 应被剥离"

    def test_maybe_migrate_v13_to_v14(self, monkeypatch):
        """_maybe_migrate 将 v13 数据迁移到 v14"""
        from server.llm_pool import key_store
        from server.llm_pool.key_store import _maybe_migrate
        # mock 文件写入，避免破坏真实 keys.json
        monkeypatch.setattr(key_store, "_write_raw_keys_file", lambda *a, **kw: None)
        monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: type("P", (), {"exists": lambda self: False})())
        data = {
            "version": 13,
            "keys": [{"id": "test", "key": "sk-test", "base_url": "http://x",
                       "models": [{"name": "m", "scope": ["llm"], "tier": 3, "enabled": True}]}],
        }
        result = _maybe_migrate(data)
        assert result["version"] == 14
