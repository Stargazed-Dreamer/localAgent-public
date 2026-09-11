"""v6-v8 Key Schema 单元测试：model 级 scope/tier/enabled。

测试覆盖：
- KeyRecord / _to_record / _to_dict：v8 格式
- _key_scope：从 models 聚合 scope
- migrate_v5_to_v6 / migrate_v6_to_v7：迁移 + 幂等检测
- _pick_model_for_tier：按 enabled/scope/tier 过滤候选 model
- resolve_keys：VL/AIGC use_case 选对应 scope model；privacy_warning 隔离敏感 use_case
"""

import json

import pytest

from server.llm_pool.key_store import (
    KeyRecord,
    ResolvedKey,
    _key_scope,
    _pick_model_for_tier,
    _to_dict,
    _to_record,
    migrate_v5_to_v6,
    migrate_v6_to_v7,
)


@pytest.fixture(autouse=True)
def _invalidate_key_store_cache():
    """每个测试前后清理 key_store 全局缓存，防止 monkeypatch 测试污染后续测试

    根因：test_resolve_keys_model_level_scope / test_resolve_keys_aigc_scope 用
    monkeypatch.setattr 替换 _get_unified_keys_path，monkeypatch 还原函数后 _cache
    全局仍持有测试 fixture 数据（5s TTL），导致后续非 monkeypatch 测试（如
    test_resolve_keys_privacy_warning_blocks_sensitive）读到无 privacy_warning 的
    测试数据而错误 skip。
    """
    from server.llm_pool import key_store
    key_store._invalidate_cache()
    yield
    key_store._invalidate_cache()


def test_to_record_v6():
    """旧 dict → KeyRecord 时补齐 v8 模型默认字段。"""
    item = {
        "id": "t1",
        "label": "test",
        "key": "sk-x",
        "base_url": "https://api.test/v1",
        "models": [
            {"name": "m1", "scope": ["llm"]},
            {"name": "m2", "scope": ["vl"]},
        ],
    }
    rec = _to_record(item)
    assert rec.models == [
        {"name": "m1", "scope": ["llm"], "tier": 3, "enabled": True},
        {"name": "m2", "scope": ["vl"], "tier": 3, "enabled": True},
    ]
    assert not hasattr(rec, "scope"), "KeyRecord 不应有 scope 字段"
    assert not hasattr(rec, "limits"), "KeyRecord 不应有 limits 字段"


def test_to_dict_v6_no_scope():
    """KeyRecord → dict：v8 格式，不含 limits/scope 字段"""
    rec = KeyRecord(
        id="t", label="t", key="k", base_url="b",
        models=[{"name": "m1", "scope": ["llm"]},
                {"name": "m2", "scope": ["vl"]}],
    )
    d = _to_dict(rec)
    assert d["models"] == [
        {"name": "m1", "scope": ["llm"], "tier": 3, "enabled": True},
        {"name": "m2", "scope": ["vl"], "tier": 3, "enabled": True},
    ]
    assert "scope" not in d, "dict 不应含 key 级 scope"
    assert "limits" not in d, "dict 不应含 key 级 limits"


def test_key_scope_aggregate():
    """_key_scope 从 models 聚合 scope"""
    rec = KeyRecord(
        id="t", label="t", key="k", base_url="b",
        models=[{"name": "m1", "scope": ["llm"]}, {"name": "m2", "scope": ["vl"]}],
    )
    assert _key_scope(rec) == ["llm", "vl"]

    rec_single = KeyRecord(
        id="t2", label="t", key="k", base_url="b",
        models=[{"name": "m1", "scope": ["llm"]}],
    )
    assert _key_scope(rec_single) == ["llm"]


def test_migrate_v5_to_v6():
    """v5 (list[str] + scope) → v6 (list[dict])：每个 model 继承 key 级 scope"""
    data = {
        "version": 5,
        "keys": [
            {"models": ["m1", "m2"], "scope": ["llm"]},
            {"models": ["m3"], "scope": ["llm", "vl"]},
        ],
    }
    new_data = migrate_v5_to_v6(data)
    assert new_data["version"] == 6
    assert new_data["keys"][0]["models"] == [
        {"name": "m1", "scope": ["llm"]},
        {"name": "m2", "scope": ["llm"]},
    ]
    assert "scope" not in new_data["keys"][0]
    assert new_data["keys"][1]["models"] == [{"name": "m3", "scope": ["llm", "vl"]}]
    assert "scope" not in new_data["keys"][1]


def test_migrate_v5_to_v6_idempotent():
    """幂等检测：已是 v6 格式则跳过（不破坏数据）"""
    data = {
        "version": 6,
        "keys": [{"models": [{"name": "m", "scope": ["llm"]}]}],
    }
    new_data = migrate_v5_to_v6(data)
    assert new_data["keys"][0]["models"] == [{"name": "m", "scope": ["llm"]}]
    assert "scope" not in new_data["keys"][0]


def test_pick_model_scope_filter():
    """_pick_model_for_tier 按 required_scope 过滤候选 model。"""
    rec = KeyRecord(
        id="t", label="t", key="k", base_url="b",
        models=[
            {"name": "llm-m", "scope": ["llm"]},
            {"name": "vl-m", "scope": ["vl"]},
        ],
    )
    # 不限制 tier，只按 scope 过滤
    assert _pick_model_for_tier(rec, None, "llm") == "llm-m"
    assert _pick_model_for_tier(rec, None, "vl") == "vl-m"
    # preferred_model 在候选中 → 优先选
    assert _pick_model_for_tier(rec, None, "vl", "vl-m") == "vl-m"
    # preferred_model 不在候选中 → fallback 到候选第一个
    assert _pick_model_for_tier(rec, None, "vl", "nonexistent") == "vl-m"
    # 无匹配候选 → 空字符串
    assert _pick_model_for_tier(rec, None, "nonexistent") == ""


def test_resolve_keys_model_level_scope(monkeypatch, tmp_path):
    """验证 VL use_case 选 VL model 而非 LLM model（核心场景）"""
    test_data = {
        "version": 6,
        "keys": [{
            "id": "k1",
            "label": "test",
            "key": "sk-x",
            "base_url": "https://api.test/v1",
            "models": [
                {"name": "llm-1", "scope": ["llm"], "tier": 3},
                {"name": "vl-1", "scope": ["vl"], "tier": 5},
            ],
            "max_concurrency": 3,
            "privacy_warning": "",
            "group": "",
            "enabled": True,
            "allowed_uses": [],
            "status": {"works": True, "fail_count": 0,
                       "last_health_check": "", "last_check_status": "", "last_check_detail": ""},
        }],
    }
    test_file = tmp_path / "keys.json"
    test_file.write_text(json.dumps(test_data), encoding="utf-8")

    from server.llm_pool import key_store
    monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: test_file)
    key_store._invalidate_cache()

    # LLM use_case 选 llm-1
    resolved_llm = key_store.resolve_keys("agent_chat")
    assert len(resolved_llm) == 1
    assert resolved_llm[0].model == "llm-1"
    assert not hasattr(resolved_llm[0], "scope"), "ResolvedKey 不应有 scope 字段"

    # VL use_case 选 vl-1（不选 llm-1）
    resolved_vl = key_store.resolve_keys("vl_ocr")
    assert len(resolved_vl) == 1
    assert resolved_vl[0].model == "vl-1"


def test_resolved_key_no_scope_field():
    """ResolvedKey 数据结构无 scope 字段"""
    fields = set(ResolvedKey.__dataclass_fields__.keys())
    assert "scope" not in fields
    assert "model" in fields
    assert "models" in fields


# ========== v7 测试：AIGC scope ==========


def test_to_record_v7_limits_stripped():
    """v14: _to_record 不再解析 limits，即使输入含 limits 也不出现在输出中"""
    item = {
        "id": "t7", "label": "test", "key": "sk-x", "base_url": "https://api.test/v1",
        "models": [{"name": "m1", "scope": ["llm"], "limits": {"rpm": 20}}],
        "limits": {"rpm": 20},
    }
    rec = _to_record(item)
    assert "limits" not in rec.models[0], "model dict 不应含 limits"
    assert not hasattr(rec, "limits"), "KeyRecord 不应有 limits 字段"


def test_to_dict_v7_limits_stripped():
    """v14: _to_dict 不再写入 limits，即使 model dict 含 limits 也不出现在输出中"""
    rec = KeyRecord(
        id="t", label="t", key="k", base_url="b",
        models=[{"name": "m1", "scope": ["llm"], "limits": {"rpm": 20}}],
    )
    d = _to_dict(rec)
    assert "limits" not in d, "dict 不应含 key 级 limits"
    assert "limits" not in d["models"][0], "model dict 不应含 limits"


def test_migrate_v6_to_v7():
    """v6 (无 limits) → v7 (models + key 级均含 limits: {})"""
    data = {
        "version": 6,
        "keys": [{"models": [{"name": "m", "scope": ["llm"]}]}],
    }
    new_data = migrate_v6_to_v7(data)
    assert new_data["version"] == 7
    assert new_data["keys"][0]["models"][0]["limits"] == {}
    assert new_data["keys"][0]["limits"] == {}


def test_migrate_v6_to_v7_idempotent():
    """幂等检测：已是 v7 格式则跳过"""
    data = {
        "version": 7,
        "keys": [{"models": [{"name": "m", "scope": ["llm"], "limits": {"rpm": 20}}],
                   "limits": {"rpm": 20}}],
    }
    new_data = migrate_v6_to_v7(data)
    assert new_data["keys"][0]["models"][0]["limits"] == {"rpm": 20}
    assert new_data["keys"][0]["limits"] == {"rpm": 20}


def test_resolve_keys_aigc_scope(monkeypatch, tmp_path):
    """验证 AIGC use_case 选 AIGC model 而非 LLM model"""
    test_data = {
        "version": 7,
        "keys": [{
            "id": "k1", "label": "test", "key": "sk-x", "base_url": "https://api.test/v1",
            "models": [
                {"name": "llm-1", "scope": ["llm"]},
                {"name": "img-1", "scope": ["aigc_image"]},
            ],
            "max_concurrency": 3, "privacy_warning": "", "group": "",
            "enabled": True, "allowed_uses": [],
            "status": {"works": True, "fail_count": 0,
                       "last_health_check": "", "last_check_status": "", "last_check_detail": ""},
        }],
    }
    test_file = tmp_path / "keys.json"
    test_file.write_text(json.dumps(test_data), encoding="utf-8")
    from server.llm_pool import key_store
    monkeypatch.setattr(key_store, "_get_unified_keys_path", lambda: test_file)
    key_store._invalidate_cache()
    # AIGC image use_case 选 img-1
    resolved = key_store.resolve_keys("aigc_image_gen")
    assert len(resolved) == 1
    assert resolved[0].model == "img-1"
    # LLM use_case 选 llm-1
    resolved_llm = key_store.resolve_keys("agent_chat")
    assert resolved_llm[0].model == "llm-1"


def test_resolve_keys_privacy_warning_blocks_sensitive(monkeypatch):
    """验证 privacy_warning 的 key 被敏感 use_case 排除，但非敏感 use_case 可用

    依赖 keys.json 实际数据中的 agnes key（privacy_warning 非空）。
    如果 keys.json 不含 privacy_warning key 则跳过。
    """
    from server import config
    from server.llm_pool import key_store
    monkeypatch.setattr(
        config,
        "get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    records = key_store.load_keys()
    has_warning = any(r.privacy_warning for r in records)
    if not has_warning:
        pytest.skip("No key with privacy_warning in keys.json")
    # vl_ocr 是 sensitive，应排除带 privacy_warning 的 key
    resolved_vl = key_store.resolve_keys("vl_ocr")
    for rk in resolved_vl:
        rec = key_store.get_key_by_id(rk.key_id)
        assert not rec.privacy_warning, \
            f"敏感 use_case 选到了带 privacy_warning 的 key: {rk.label}"
