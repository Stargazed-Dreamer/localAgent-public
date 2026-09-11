"""LLM 并发池模块测试

覆盖：数据结构（KeyStats/LLMKey/ProviderPolicy）、key 可用性判断、
round-robin 分配、冷却与过期、per-project token 统计、重试延迟计算、
key 文件解析（纯文本/JSON tested 格式）。

不覆盖实际 HTTP 调用（call/call_simple）——需要真实 API key，属于集成测试范畴。
"""

import json
import time

import pytest

from server.llm_pool import (
    KeyStats,
    LLMKey,
    LLMPool,
    ProviderPolicy,
    _collect_key_records_from_files,
    _extract_key_records_from_file,
    _normalize_list,
    _read_text_lines,
)

# ==================== 数据结构测试 ====================

class TestKeyStats:
    def test_default_values(self):
        s = KeyStats()
        assert s.ok == 0
        assert s.fail == 0
        assert s.rate_limited == 0
        assert s.total_tokens == 0
        assert s.expired is False
        assert s.last_error == ""
        assert s.last_used == 0.0


class TestLLMKey:
    def test_default_name_from_key(self):
        k = LLMKey(key="sk-abcdefghij123456", base_url="http://x", models=["m"])
        # key[:10] = "sk-abcdefg" (前10字符) + "..."
        assert k.name == "sk-abcdefg..."

    def test_explicit_name_not_overwritten(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"], name="my-key")
        assert k.name == "my-key"

    def test_is_available_when_fresh(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"], max_concurrency=3)
        assert k.is_available is True

    def test_not_available_when_expired(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"])
        k.stats.expired = True
        assert k.is_available is False

    def test_not_available_when_in_cooldown(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"])
        k.cooldown_until = time.time() + 60
        assert k.is_available is False

    def test_not_available_when_max_concurrency(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"], max_concurrency=2)
        k.active_count = 2
        assert k.is_available is False

    def test_is_expired_property(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"])
        assert k.is_expired is False
        k.stats.expired = True
        assert k.is_expired is True

    # ----- v5: use_case_eligible -----

    def test_use_case_eligible_unknown_use_case_passes(self):
        """未知 use_case 放行（向后兼容）"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   privacy_warning="免费模型")
        assert k.use_case_eligible("unknown_use_case") is True

    def test_use_case_eligible_non_sensitive_allows_unsafe_key(self):
        """非 sensitive use_case 允许带 privacy_warning 的 key"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   privacy_warning="免费模型")
        # agent_chat 是非 sensitive use_case
        assert k.use_case_eligible("agent_chat") is True

    def test_use_case_eligible_sensitive_blocks_unsafe_key(self, monkeypatch):
        """sensitive use_case 拒绝带 privacy_warning 的 key"""
        # 隔离配置：强制 allow_privacy_warning_for_sensitive=False（默认安全行为），
        # 不受 config.toml 实际配置影响
        monkeypatch.setattr("server.llm_pool.types._get_allow_privacy_warning", lambda: False)
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   privacy_warning="免费模型")
        # memory_compress 是 sensitive use_case
        assert k.use_case_eligible("memory_compress") is False
        # download_watcher 也是 sensitive
        assert k.use_case_eligible("download_watcher") is False

    def test_use_case_eligible_sensitive_allows_safe_key(self):
        """sensitive use_case 允许无 privacy_warning 的 key"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   privacy_warning="")
        assert k.use_case_eligible("memory_compress") is True
        assert k.use_case_eligible("vl_ocr") is True

    def test_use_case_eligible_respects_allowed_uses_whitelist(self):
        """key.allowed_uses 非空且不含 use_case → False"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   allowed_uses=["agent_chat"])
        assert k.use_case_eligible("agent_chat") is True
        assert k.use_case_eligible("community_summarize") is False

    def test_use_case_eligible_empty_allowed_uses_allows_all(self):
        """key.allowed_uses 为空 → 不限制（所有 use_case 放行）"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m"],
                   allowed_uses=[])
        assert k.use_case_eligible("agent_chat") is True
        assert k.use_case_eligible("memory_compress") is True


class TestProviderPolicy:
    def test_default_values(self):
        p = ProviderPolicy(name="test")
        assert p.retry_count == 4
        assert p.max_concurrency == 3
        assert p.rate_limit_max_cooldown == 300.0

    def test_custom_values(self):
        p = ProviderPolicy(name="test", retry_count=10, max_concurrency=5)
        assert p.retry_count == 10
        assert p.max_concurrency == 5


# ==================== 工具函数测试 ====================

class TestNormalizeList:
    def test_none_returns_empty(self):
        assert _normalize_list(None) == []

    def test_string_returns_single_item(self):
        assert _normalize_list("sk-abc") == ["sk-abc"]

    def test_list_filters_empty(self):
        assert _normalize_list(["a", "", "  ", "b"]) == ["a", "b"]

    def test_list_strips_whitespace(self):
        assert _normalize_list(["  a  ", "b"]) == ["a", "b"]

    def test_non_string_items_skipped(self):
        assert _normalize_list(["a", 123, None, "b"]) == ["a", "b"]


class TestReadTextLines:
    def test_nonexistent_file(self, tmp_path):
        assert _read_text_lines(tmp_path / "noexist.txt") == []

    def test_utf8_file(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\nsk-def\n", encoding="utf-8")
        assert _read_text_lines(p) == ["sk-abc", "sk-def"]

    def test_chinese_content(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\n# 注释\n", encoding="utf-8")
        result = _read_text_lines(p)
        assert "sk-abc" in result
        assert "# 注释" in result


class TestExtractKeyRecords:
    def test_nonexistent_file(self, tmp_path):
        assert _extract_key_records_from_file(tmp_path / "noexist.json") == []

    def test_plain_text_file(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\nsk-def\n# comment\n\n", encoding="utf-8")
        records = _extract_key_records_from_file(p)
        assert len(records) == 2
        assert records[0]["key"] == "sk-abc"
        assert records[1]["key"] == "sk-def"

    def test_json_tested_file(self, tmp_path):
        p = tmp_path / "tested.json"
        p.write_text(json.dumps([
            {"key": "sk-abc", "model": "m1", "works": True},
            {"key": "sk-def", "model": "m2", "works": False},
        ]), encoding="utf-8")
        records = _extract_key_records_from_file(p)
        assert len(records) == 2
        assert records[0]["key"] == "sk-abc"
        assert records[0]["model"] == "m1"

    def test_skips_invalid_json_lines(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\nnot-a-key\nsk-def\n", encoding="utf-8")
        records = _extract_key_records_from_file(p)
        assert len(records) == 2
        assert {r["key"] for r in records} == {"sk-abc", "sk-def"}

    def test_skips_placeholder_lines(self, tmp_path):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\npro\nplan\n月度\nsk-def\n", encoding="utf-8")
        records = _extract_key_records_from_file(p)
        assert len(records) == 2


class TestCollectKeyRecordsFromFiles:
    def test_dedup_across_files(self, tmp_path):
        p1 = tmp_path / "a.txt"
        p1.write_text("sk-abc\nsk-def\n", encoding="utf-8")
        p2 = tmp_path / "b.txt"
        p2.write_text("sk-abc\nsk-ghi\n", encoding="utf-8")
        records = _collect_key_records_from_files([str(p1), str(p2)])
        keys = {r["key"] for r in records}
        assert keys == {"sk-abc", "sk-def", "sk-ghi"}

    def test_relative_path_resolved(self, tmp_path, monkeypatch):
        p = tmp_path / "keys.txt"
        p.write_text("sk-abc\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        records = _collect_key_records_from_files(["keys.txt"])
        assert len(records) == 1
        assert records[0]["key"] == "sk-abc"


# ==================== LLMPool 测试 ====================

@pytest.fixture
def sample_keys():
    return [
        LLMKey(key="sk-key1", base_url="http://a", models=["m1"], max_concurrency=2),
        LLMKey(key="sk-key2", base_url="http://b", models=["m2"], max_concurrency=2),
        LLMKey(key="sk-key3", base_url="http://c", models=["m1"], max_concurrency=2),
    ]


@pytest.fixture
def tmp_stats_file(tmp_path):
    return str(tmp_path / "stats.json")


@pytest.fixture
def pool(sample_keys, tmp_stats_file):
    p = LLMPool(sample_keys, stats_file=tmp_stats_file)
    yield p


class TestPoolAcquire:
    def test_acquire_returns_key(self, pool):
        k = pool._acquire(timeout=1)
        assert k is not None
        assert k.active_count == 1

    def test_round_robin(self, pool):
        k1 = pool._acquire(timeout=1)
        k2 = pool._acquire(timeout=1)
        k3 = pool._acquire(timeout=1)
        assert k1.key != k2.key
        assert k2.key != k3.key

    def test_acquire_by_model(self, pool):
        k = pool._acquire(timeout=1, model="m2")
        assert k is not None
        assert k.model == "m2"

    def test_acquire_fallbacks_for_unknown_model(self, pool):
        # model 是软偏好：未知 model 时 fallback 到其它可用 key（同 tier 内可切换）
        # 详见 pool.py _acquire 中 "model 软偏好" 注释
        k = pool._acquire(timeout=0.1, model="nonexistent")
        assert k is not None  # fallback 到 sample_keys 中的某个 key
        assert k.key in ("sk-key1", "sk-key2", "sk-key3")

    def test_acquire_skips_expired(self, pool, sample_keys):
        sample_keys[0].stats.expired = True
        sample_keys[1].stats.expired = True
        k = pool._acquire(timeout=0.1)
        assert k is not None
        assert k.key == "sk-key3"

    def test_acquire_skips_cooldown(self, pool, sample_keys):
        sample_keys[0].cooldown_until = time.time() + 60
        k = pool._acquire(timeout=0.1)
        assert k is not None
        assert k.key != "sk-key1"

    def test_acquire_returns_none_when_all_expired(self, pool, sample_keys):
        for k in sample_keys:
            k.stats.expired = True
        assert pool._acquire(timeout=0.1) is None


class TestPoolRelease:
    def test_release_success_increments_ok(self, pool, sample_keys):
        k = sample_keys[0]
        k.active_count = 1
        pool._release(k, success=True, tokens=100)
        assert k.active_count == 0
        assert k.stats.ok == 1
        assert k.stats.total_tokens == 100

    def test_release_rate_limited_sets_cooldown(self, pool, sample_keys):
        k = sample_keys[0]
        k.active_count = 1
        old_cooldown = k.cooldown_until
        pool._release(k, rate_limited=True, cooldown=30)
        assert k.stats.rate_limited == 1
        assert k.cooldown_until > old_cooldown

    def test_release_expired_marks_key(self, pool, sample_keys):
        k = sample_keys[0]
        k.active_count = 1
        pool._release(k, expired=True)
        assert k.stats.expired is True
        assert k.stats.last_error == "expired"

    def test_release_failure_increments_fail(self, pool, sample_keys):
        k = sample_keys[0]
        k.active_count = 1
        pool._release(k, success=False)
        assert k.stats.fail == 1

    def test_release_never_negative_active(self, pool, sample_keys):
        k = sample_keys[0]
        k.active_count = 0
        pool._release(k, success=True)
        assert k.active_count == 0


class TestProjectStats:
    def test_record_usage_creates_project(self, pool):
        pool._record_usage("test_proj", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        stats = pool.get_project_stats()
        assert "test_proj" in stats["projects"]
        assert stats["projects"]["test_proj"]["total_tokens"] == 15
        assert stats["projects"]["test_proj"]["calls"] == 1

    def test_record_usage_accumulates(self, pool):
        pool._record_usage("p1", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        pool._record_usage("p1", {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})
        stats = pool.get_project_stats()
        assert stats["projects"]["p1"]["total_tokens"] == 45
        assert stats["projects"]["p1"]["calls"] == 2

    def test_record_usage_ignores_empty_project(self, pool):
        pool._record_usage("", {"total_tokens": 100})
        stats = pool.get_project_stats()
        assert "" not in stats["projects"]

    def test_record_usage_ignores_empty_usage(self, pool):
        pool._record_usage("p1", {})
        stats = pool.get_project_stats()
        assert "p1" not in stats["projects"]

    def test_import_project_stats(self, pool):
        pool.import_project_stats("imported", prompt_tokens=100, completion_tokens=50, total_tokens=150, calls=3)
        stats = pool.get_project_stats()
        assert stats["projects"]["imported"]["total_tokens"] == 150
        assert stats["projects"]["imported"]["calls"] == 3

    def test_stats_persisted_to_file(self, pool, tmp_stats_file):
        pool._record_usage("persist_test", {"total_tokens": 42})
        # 批量保存模式下需 flush 强制落盘
        pool.flush_stats()
        # 重新创建 pool 加载同一文件
        new_pool = LLMPool([], stats_file=tmp_stats_file)
        stats = new_pool.get_project_stats()
        assert "persist_test" in stats["projects"]
        assert stats["projects"]["persist_test"]["total_tokens"] == 42


class TestRetryDelay:
    def test_retry_after_overrides(self, pool):
        delay = pool._compute_retry_delay(attempt=0, retry_after=10.0)
        assert 5.0 <= delay <= 300.0

    def test_exponential_backoff(self):
        # 用零冷却+零抖动策略，让指数退避在低 attempt 就可见
        policy = ProviderPolicy(name="test", rate_limit_cooldown_seconds=0.0, retry_jitter=0.0)
        p = LLMPool([], provider_policy=policy, stats_file="")
        d0 = p._compute_retry_delay(attempt=0)
        d1 = p._compute_retry_delay(attempt=1)
        d2 = p._compute_retry_delay(attempt=2)
        assert d1 > d0
        assert d2 > d1

    def test_delay_capped_at_max(self, pool):
        delay = pool._compute_retry_delay(attempt=20)
        # jitter 在 retry_max_delay 封顶后追加（最多 +1.0），最终由 rate_limit_max_cooldown 封顶
        assert delay <= pool.provider_policy.rate_limit_max_cooldown

    def test_delay_at_least_min_cooldown(self, pool):
        delay = pool._compute_retry_delay(attempt=0)
        assert delay >= pool.provider_policy.rate_limit_cooldown_seconds


class TestPerKeyPolicy:
    """v14: per-key pool 策略测试"""

    def test_pool_init_fills_none_policy(self):
        """LLMPool.__init__ 为 pool_policy=None 的 key 填充 default_policy"""
        k = LLMKey(key="sk-test", base_url="http://x", models=["m"])
        assert k.pool_policy is None  # 构造时为 None
        default = ProviderPolicy(name="default", retry_count=7)
        LLMPool([k], stats_file="", default_policy=default)
        assert k.pool_policy is not None
        assert k.pool_policy.retry_count == 7  # 填充为 default_policy

    def test_pool_init_preserves_custom_policy(self):
        """LLMPool.__init__ 不覆盖 key 已有的 pool_policy"""
        custom = ProviderPolicy(name="custom", retry_count=10)
        k = LLMKey(key="sk-test", base_url="http://x", models=["m"], pool_policy=custom)
        default = ProviderPolicy(name="default", retry_count=3)
        LLMPool([k], stats_file="", default_policy=default)
        assert k.pool_policy is custom  # 保持引用不变
        assert k.pool_policy.retry_count == 10

    def test_compute_retry_delay_uses_key_policy(self):
        """_compute_retry_delay 传入 key 时用 key 的 pool_policy"""
        key_policy = ProviderPolicy(name="key-custom", retry_base_delay=100.0,
                                    retry_max_delay=200.0,
                                    rate_limit_cooldown_seconds=0.0, retry_jitter=0.0)
        k = LLMKey(key="sk-test", base_url="http://x", models=["m"], pool_policy=key_policy)
        default = ProviderPolicy(name="default", retry_base_delay=1.0)
        pool = LLMPool([k], stats_file="", default_policy=default)
        # 传入 key → 用 key_policy 的 retry_base_delay=100
        delay = pool._compute_retry_delay(attempt=0, key=k)
        assert delay >= 100.0  # base_delay=100, no jitter, no min_cooldown

    def test_compute_retry_delay_falls_back_to_default(self):
        """_compute_retry_delay 不传 key 或 key 无 policy 时用 default_policy"""
        default = ProviderPolicy(name="default", retry_base_delay=5.0,
                                 rate_limit_cooldown_seconds=0.0, retry_jitter=0.0)
        pool = LLMPool([], stats_file="", default_policy=default)
        # 不传 key → 用 default_policy
        delay = pool._compute_retry_delay(attempt=0)
        assert delay >= 5.0

    def test_two_keys_different_delay(self):
        """两个 key 配不同 retry_base_delay，_compute_retry_delay 返回不同延迟"""
        k1 = LLMKey(key="sk-1", base_url="http://a", models=["m"],
                    pool_policy=ProviderPolicy(name="k1", retry_base_delay=10.0,
                                               rate_limit_cooldown_seconds=0.0, retry_jitter=0.0))
        k2 = LLMKey(key="sk-2", base_url="http://b", models=["m"],
                    pool_policy=ProviderPolicy(name="k2", retry_base_delay=50.0,
                                               rate_limit_cooldown_seconds=0.0, retry_jitter=0.0))
        pool = LLMPool([k1, k2], stats_file="")
        d1 = pool._compute_retry_delay(attempt=0, key=k1)
        d2 = pool._compute_retry_delay(attempt=0, key=k2)
        assert d1 < d2  # k1 base_delay=10 < k2 base_delay=50

    def test_key_without_pool_segment_uses_global_default(self):
        """key 无 pool 段（pool_policy=None）时，LLMPool 填充后用全局默认"""
        k = LLMKey(key="sk-test", base_url="http://x", models=["m"])  # pool_policy=None
        default = ProviderPolicy(name="global", rate_limit_cooldown_seconds=42.0)
        pool = LLMPool([k], stats_file="", default_policy=default)
        # LLMPool.__init__ 已把 k.pool_policy 填充为 default
        delay = pool._compute_retry_delay(attempt=0, key=k)
        assert delay >= 42.0  # 至少为 rate_limit_cooldown_seconds

    def test_backward_compat_provider_policy_param(self):
        """LLMPool 仍接受 provider_policy= 参数（向后兼容）"""
        policy = ProviderPolicy(name="legacy", retry_count=5)
        pool = LLMPool([], provider_policy=policy, stats_file="")
        assert pool.default_policy.retry_count == 5
        # provider_policy property 仍可访问
        assert pool.provider_policy.retry_count == 5

    def test_backward_compat_init_pool_provider_policy(self):
        """init_pool 仍接受 provider_policy= 参数（向后兼容）"""
        import server.llm_pool.singleton as s
        from server.llm_pool.singleton import init_pool, is_initialized
        policy = ProviderPolicy(name="legacy", retry_count=6)
        init_pool([], stats_file="", provider_policy=policy)
        assert is_initialized()
        assert s.get_pool().default_policy.retry_count == 6
        # 清理全局状态
        s._global_pool = None


class TestPoolStatus:
    def test_get_status_returns_dict(self, pool):
        status = pool.get_status()
        assert "total_keys" in status
        assert "active_keys" in status
        assert "expired_keys" in status
        assert "current_active" in status
        assert "keys" in status

    def test_status_counts(self, pool, sample_keys):
        sample_keys[0].stats.expired = True
        sample_keys[1].cooldown_until = time.time() + 60
        status = pool.get_status()
        assert status["total_keys"] == 3
        assert status["expired_keys"] >= 1
        # cooldown 体现在 keys 列表的 cooldown_remaining 字段
        in_cooldown = sum(1 for k in status["keys"] if k.get("cooldown_remaining", 0) > 0)
        assert in_cooldown >= 1


# ==================== v12: OmniRoute 设计借鉴 ====================

class TestModelLockout:
    """v12: Model Lockout——单 model 429 不冻结整个 key"""

    def test_record_model_cooldown_stores_entry(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1", "m2"])
        cd_until = time.time() + 60
        k.record_model_cooldown("m1", cd_until)
        assert "m1" in k.model_cooldowns
        assert k.model_cooldowns["m1"] == cd_until

    def test_record_model_cooldown_uses_max(self):
        """新 cooldown 不应覆盖更长的现有 cooldown"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        long_cd = time.time() + 120
        short_cd = time.time() + 30
        k.record_model_cooldown("m1", long_cd)
        k.record_model_cooldown("m1", short_cd)
        assert k.model_cooldowns["m1"] == long_cd

    def test_model_cooldown_remaining_returns_seconds(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        k.record_model_cooldown("m1", time.time() + 50)
        remaining = k.model_cooldown_remaining("m1")
        assert 45 <= remaining <= 55

    def test_model_cooldown_remaining_zero_when_expired(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        k.record_model_cooldown("m1", time.time() - 10)  # 过期 10 秒
        assert k.model_cooldown_remaining("m1") == 0

    def test_model_cooldown_remaining_zero_for_unknown(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        assert k.model_cooldown_remaining("unknown_model") == 0

    def test_is_model_available_when_fresh(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"], max_concurrency=3)
        assert k.is_model_available("m1") is True

    def test_is_model_available_false_when_in_model_cooldown(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1", "m2"], max_concurrency=3)
        k.record_model_cooldown("m1", time.time() + 60)
        # m1 在 cooldown，m2 仍可用
        assert k.is_model_available("m1") is False
        assert k.is_model_available("m2") is True

    def test_is_model_available_false_when_key_in_cooldown(self):
        """key 级 cooldown 时所有 model 不可用"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"], max_concurrency=3)
        k.cooldown_until = time.time() + 60
        assert k.is_model_available("m1") is False

    def test_is_model_available_false_when_max_concurrency(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"], max_concurrency=2)
        k.active_count = 2
        assert k.is_model_available("m1") is False

    def test_post_init_filters_expired_model_cooldowns(self):
        """__post_init__ 应过滤掉已过期的 model_cooldowns 条目"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        # 直接构造已过期的 cooldowns
        k.model_cooldowns = {"m1": time.time() - 100, "m2": time.time() + 100}
        # 重新触发 post_init 不可行，但 is_model_available 会运行时检测
        # 这里通过 model_cooldown_remaining 验证过期条目返回 0
        assert k.model_cooldown_remaining("m1") == 0
        assert k.model_cooldown_remaining("m2") > 0


class TestSaturationReflow:
    """v12: Saturation Reflow——上游响应头饱和信号写回"""

    def test_parse_openai_rate_limit_saturation_returns_zero_for_empty(self):
        from server.llm_pool.pool import _parse_openai_rate_limit_saturation
        assert _parse_openai_rate_limit_saturation({}) == 0.0

    def test_parse_openai_rate_limit_saturation_from_requests(self):
        from server.llm_pool.pool import _parse_openai_rate_limit_saturation
        # x-ratelimit-remaining-requests=10, x-ratelimit-limit-requests=100
        # saturation = 1 - 10/100 = 0.9
        headers = {
            "x-ratelimit-remaining-requests": "10",
            "x-ratelimit-limit-requests": "100",
        }
        sat = _parse_openai_rate_limit_saturation(headers)
        assert 0.85 <= sat <= 0.95

    def test_parse_openai_rate_limit_saturation_picks_max(self):
        from server.llm_pool.pool import _parse_openai_rate_limit_saturation
        # requests saturation=0.5, tokens saturation=0.8 → 取 max=0.8
        headers = {
            "x-ratelimit-remaining-requests": "50",
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-tokens": "200",
            "x-ratelimit-limit-tokens": "1000",
        }
        sat = _parse_openai_rate_limit_saturation(headers)
        assert 0.75 <= sat <= 0.85

    def test_parse_anthropic_rate_limit_saturation_from_unified(self):
        from server.llm_pool.pool import _parse_anthropic_rate_limit_saturation
        # anthropic-ratelimit-unified-requests-utilization="0.45" → 0.45
        headers = {
            "anthropic-ratelimit-unified-requests-utilization": "0.45",
        }
        sat = _parse_anthropic_rate_limit_saturation(headers)
        assert 0.40 <= sat <= 0.50

    def test_parse_anthropic_rate_limit_saturation_fallback_to_tokens(self):
        from server.llm_pool.pool import _parse_anthropic_rate_limit_saturation
        # 无 unified，fallback 到 tokens limit/remaining
        headers = {
            "anthropic-ratelimit-tokens-remaining": "3000",
            "anthropic-ratelimit-tokens-limit": "10000",
        }
        sat = _parse_anthropic_rate_limit_saturation(headers)
        # saturation = 1 - 3000/10000 = 0.7
        assert 0.65 <= sat <= 0.75

    def test_llmkey_update_saturation_clamps_to_range(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        k.update_saturation(1.5)
        assert k.saturation == 1.0
        k.update_saturation(-0.5)
        assert k.saturation == 0.0

    def test_llmkey_effective_saturation_zero_when_expired(self):
        """TTL 过期后 effective_saturation 返回 0"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        k.update_saturation(0.8)
        # 模拟过期：把 saturation_updated_at 设为很久以前
        k.saturation_updated_at = time.time() - 100
        assert k.effective_saturation(ttl_seconds=30.0) == 0.0

    def test_llmkey_effective_saturation_returns_value_when_fresh(self):
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"])
        k.update_saturation(0.7)
        # 刚写入，TTL 内
        assert abs(k.effective_saturation(ttl_seconds=30.0) - 0.7) < 0.01

    def test_release_records_saturation_on_rate_limited(self):
        """429 时若未显式传 saturation，自动写 1.0"""
        k = LLMKey(key="sk-abc", base_url="http://x", models=["m1"], max_concurrency=2)
        k.active_count = 1
        pool = LLMPool([k], stats_file="")
        pool._release(k, rate_limited=True, cooldown=30, model="m1")
        assert k.saturation == 1.0
        assert k.effective_saturation() > 0.9


class TestPoolDeduped:
    """v12: Pool-Deduped——同 pool_key 共享上游配额去重"""

    def test_get_status_includes_dedup_fields(self, pool):
        status = pool.get_status()
        assert "total_max_concurrency_deduped" in status
        assert "dedup_ratio" in status
        assert "pool_keys" in status

    def test_dedup_ratio_one_when_no_pool_key(self, pool):
        """无 pool_key 时每 key 独立成组，dedup_ratio = 1.0"""
        status = pool.get_status()
        assert status["dedup_ratio"] == 1.0
        assert status["total_max_concurrency_deduped"] == status["total_max_concurrency"]

    def test_dedup_groups_by_pool_key(self):
        """同 pool_key 的多个 key 取 max(max_concurrency)"""
        keys = [
            LLMKey(key="sk-1", base_url="http://x", models=["m"],
                   max_concurrency=2, pool_key="upstream-A"),
            LLMKey(key="sk-2", base_url="http://x", models=["m"],
                   max_concurrency=3, pool_key="upstream-A"),  # 同上游
            LLMKey(key="sk-3", base_url="http://x", models=["m"],
                   max_concurrency=1, pool_key="upstream-B"),
        ]
        pool = LLMPool(keys, stats_file="")
        status = pool.get_status()
        # naive: 2+3+1=6, deduped: max(2,3)+max(1)=3+1=4
        assert status["total_max_concurrency"] == 6
        assert status["total_max_concurrency_deduped"] == 4
        assert status["dedup_ratio"] == round(4 / 6, 3)
        # pool_keys 列表应有 2 组
        assert len(status["pool_keys"]) == 2
        # upstream-A 组：deduped_concurrency=3, keys_count=2
        upstream_a = next(p for p in status["pool_keys"] if p["pool_key"] == "upstream-A")
        assert upstream_a["deduped_concurrency"] == 3
        assert upstream_a["keys_count"] == 2

    def test_get_status_keys_include_pool_key(self, pool):
        status = pool.get_status()
        for k_info in status["keys"]:
            assert "pool_key" in k_info

    def test_get_models_summary_includes_dedup_fields(self, pool):
        summary = pool.get_models_summary()
        for m_info in summary["models"]:
            assert "naive_max_concurrency" in m_info
            assert "deduped_max_concurrency" in m_info
            assert "dedup_ratio" in m_info


class TestLKGPStickySession:
    """v12: LKGP 会话粘性——同一 session_id 优先复用上次成功的 (key, model)"""

    def test_acquire_without_session_id_uses_round_robin(self, pool):
        """无 session_id 时走正常 round-robin"""
        k1 = pool._acquire(timeout=1)
        pool._release(k1, success=True)
        k2 = pool._acquire(timeout=1)
        pool._release(k2, success=True)
        assert k1.key != k2.key  # round-robin 应切换

    def test_acquire_with_session_id_sticks_to_first_success(self, pool):
        """session_id 命中后，后续调用复用同一 key"""
        # 第一次 acquire（无粘性），假设成功
        k1 = pool._acquire(timeout=1, session_id="sess-1")
        assert k1 is not None
        pool._release(k1, success=True, tokens=10, model=k1.default_model)
        # 记录粘性（模拟 call() 成功分支）
        pool._record_session_stickiness("sess-1", k1, k1.default_model)

        # 第二次 acquire 同一 session，应粘到 k1
        k2 = pool._acquire(timeout=1, session_id="sess-1")
        assert k2 is not None
        assert k2.key == k1.key
        pool._release(k2, success=True, tokens=10, model=k2.default_model)

    def test_acquire_with_different_session_does_not_collide(self, pool):
        """不同 session_id 各自独立粘性"""
        k1 = pool._acquire(timeout=1, session_id="sess-A")
        pool._release(k1, success=True, model=k1.default_model)
        pool._record_session_stickiness("sess-A", k1, k1.default_model)

        k2 = pool._acquire(timeout=1, session_id="sess-B")
        pool._release(k2, success=True, model=k2.default_model)
        pool._record_session_stickiness("sess-B", k2, k2.default_model)

        # sess-A 应粘到 k1，sess-B 应粘到 k2
        k_a = pool._acquire(timeout=1, session_id="sess-A")
        k_b = pool._acquire(timeout=1, session_id="sess-B")
        assert k_a.key == k1.key
        assert k_b.key == k2.key
        pool._release(k_a, success=True, model=k_a.default_model)
        pool._release(k_b, success=True, model=k_b.default_model)

    def test_clear_session_removes_stickiness(self, pool):
        """clear_session 后粘性失效"""
        k1 = pool._acquire(timeout=1, session_id="sess-X")
        pool._release(k1, success=True, model=k1.default_model)
        pool._record_session_stickiness("sess-X", k1, k1.default_model)
        assert pool.get_session_stickiness()["total_sessions"] == 1

        pool.clear_session("sess-X")
        assert pool.get_session_stickiness()["total_sessions"] == 0

        # 清理后 acquire 应走 round-robin
        k2 = pool._acquire(timeout=1, session_id="sess-X")
        # 不应保证粘到 k1（粘性已失效）
        pool._release(k2, success=True, model=k2.default_model)

    def test_stickiness_skips_when_model_in_cooldown(self):
        """粘性命中但粘性 model 在 cooldown 时，粘性未命中（last_used 不刷新）

        注：_acquire 跳过粘性后会走 round-robin，仍可能返回原 key（key 整体可用，仅 m1 在 cooldown）。
        call() 内部会用 exclude_set 跳过 m1 选 m2（同 key 的其他可用 model）。
        所以本测试只验证"粘性未命中"——即 last_used 没被刷新。
        """
        from server.llm_pool import LLMKey as _K
        k1 = _K(key="sk-sticky-1", base_url="http://x", models=["m1", "m2"], max_concurrency=2)
        pool = LLMPool([k1], stats_file="")
        pool._record_session_stickiness("sess-Y", k1, "m1")
        old_last_used = pool._session_stickiness["sess-Y"]["last_used"]
        # 让 k1 的 m1 进入 model_cooldown
        k1.record_model_cooldown("m1", time.time() + 60)

        # 命中粘性但因 m1 在 cooldown，应回退到 round-robin（粘性未命中）
        k_acquired = pool._acquire(timeout=0.1, session_id="sess-Y", model="m1")
        assert k_acquired is not None
        # 粘性未命中：last_used 没被刷新（仍是旧值）
        assert pool._session_stickiness["sess-Y"]["last_used"] == old_last_used
        pool._release(k_acquired, success=True, model="m1")

    def test_stickiness_expired_after_ttl(self, pool):
        """TTL 过期后粘性自动失效"""
        k1 = pool._acquire(timeout=1, session_id="sess-TTL")
        pool._release(k1, success=True, model=k1.default_model)
        pool._record_session_stickiness("sess-TTL", k1, k1.default_model)

        # 模拟 TTL 过期：手动把 last_used 改到很久以前
        with pool.lock:
            pool._session_stickiness["sess-TTL"]["last_used"] = time.time() - 99999

        # 现在 acquire 应走 round-robin（粘性 TTL 已过期被清理）
        k2 = pool._acquire(timeout=1, session_id="sess-TTL")
        # 仍能 acquire，但不再保证粘到 k1
        assert k2 is not None
        pool._release(k2, success=True, model=k2.default_model)

    def test_get_status_includes_session_stickiness_summary(self, pool):
        status = pool.get_status()
        assert "session_stickiness_count" in status
        assert "session_stickiness_ttl_seconds" in status
        assert "session_stickiness_max" in status

    def test_get_session_stickiness_returns_detail(self, pool):
        k1 = pool._acquire(timeout=1, session_id="sess-detail")
        pool._release(k1, success=True, model=k1.default_model)
        pool._record_session_stickiness("sess-detail", k1, k1.default_model)

        sticky = pool.get_session_stickiness()
        assert sticky["total_sessions"] == 1
        assert sticky["sessions"][0]["session_id"] == "sess-detail"
        # key_id 在 _record_session_stickiness 内会回退到 name（当 key.key_id 为空时）
        assert sticky["sessions"][0]["key_id"] == (k1.key_id or k1.name)
        assert sticky["sessions"][0]["model"] == k1.default_model


# ==================== v13: Circuit Breaker / 多维配额 / 延迟分位 ====================

class TestCircuitBreaker:
    """Provider 级 Circuit Breaker 三态转换测试"""

    def test_closed_to_open_on_threshold(self):
        from server.llm_pool.pool import CB_CLOSED, CB_OPEN, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=3, open_seconds=60, success_threshold=2)
        assert cb.state == CB_CLOSED
        cb.record_failure(time.time())
        cb.record_failure(time.time())
        assert cb.state == CB_CLOSED  # 2 < 3
        cb.record_failure(time.time())
        assert cb.state == CB_OPEN  # 3 >= 3

    def test_open_blocks_requests(self):
        from server.llm_pool.pool import CB_OPEN, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=1, open_seconds=60)
        cb.record_failure(time.time())
        assert cb.state == CB_OPEN
        assert cb.allow_request(time.time()) is False

    def test_open_to_half_open_after_timeout(self):
        from server.llm_pool.pool import CB_HALF_OPEN, CB_OPEN, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=1, open_seconds=60, success_threshold=2)
        cb.record_failure(time.time())
        assert cb.state == CB_OPEN
        # 模拟 open_seconds 已过（直接回拨 _opened_at，避免真实 sleep 60s）
        cb._opened_at = time.time() - 61
        # allow_request 内部会触发 OPEN→HALF_OPEN 转换
        assert cb.allow_request(time.time()) is True
        assert cb.state == CB_HALF_OPEN

    def test_half_open_to_closed_on_success(self):
        from server.llm_pool.pool import CB_CLOSED, CB_HALF_OPEN, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=1, open_seconds=60, success_threshold=2)
        cb.record_failure(time.time())
        cb._opened_at = time.time() - 61  # 模拟超时
        cb.allow_request(time.time())  # 触发转 HALF_OPEN + 占用探针位
        assert cb.state == CB_HALF_OPEN
        cb.record_success(time.time())
        assert cb.state == CB_HALF_OPEN  # 1 < 2
        cb.record_success(time.time())
        assert cb.state == CB_CLOSED  # 2 >= 2

    def test_half_open_to_open_on_failure(self):
        from server.llm_pool.pool import CB_HALF_OPEN, CB_OPEN, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=1, open_seconds=60, success_threshold=2)
        cb.record_failure(time.time())
        cb._opened_at = time.time() - 61  # 模拟超时
        cb.allow_request(time.time())  # 转 HALF_OPEN
        assert cb.state == CB_HALF_OPEN
        cb.record_failure(time.time())  # 探针失败
        assert cb.state == CB_OPEN  # 立即回退 OPEN

    def test_success_resets_fail_count_in_closed(self):
        from server.llm_pool.pool import CB_CLOSED, CircuitBreaker
        cb = CircuitBreaker(fail_threshold=3)
        cb.record_failure(time.time())
        cb.record_failure(time.time())
        cb.record_success(time.time())  # 成功重置计数
        cb.record_failure(time.time())
        cb.record_failure(time.time())
        assert cb.state == CB_CLOSED  # 2 < 3，未达阈值

    def test_snapshot_returns_state(self):
        from server.llm_pool.pool import CircuitBreaker
        cb = CircuitBreaker(fail_threshold=5, open_seconds=30, success_threshold=2)
        snap = cb.snapshot(time.time())
        assert snap["state"] == "closed"
        assert snap["fail_threshold"] == 5
        assert snap["open_seconds"] == 30
        assert snap["success_threshold"] == 2


class TestPoolCircuitBreaker:
    """LLMPool 集成 Circuit Breaker 测试"""

    def test_pool_initializes_breaker_config(self):
        pool = LLMPool([], stats_file="")
        assert hasattr(pool, "_breaker_enabled")
        assert hasattr(pool, "_breakers")
        assert pool._breakers == {}

    def test_breaker_blocks_acquire_after_failures(self):
        from server.llm_pool.pool import CB_OPEN
        k1 = LLMKey(key="sk-cb-1", base_url="http://provider-a/v1",
                    models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        pool._breaker_fail_threshold = 2
        pool._breaker_open_seconds = 60
        # 连续 2 次失败 → OPEN
        pool._release(k1, error="fail", model="m1")
        pool._release(k1, error="fail", model="m1")
        breaker = pool._breakers["http://provider-a/v1"]
        assert breaker.state == CB_OPEN
        # acquire 应返回 None（provider 被熔断）
        result = pool._acquire(timeout=0.1, model="m1")
        assert result is None

    def test_breaker_does_not_block_other_provider(self):
        from server.llm_pool.pool import CB_OPEN
        k1 = LLMKey(key="sk-cb-a", base_url="http://provider-a/v1",
                    models=["m1"], max_concurrency=3)
        k2 = LLMKey(key="sk-cb-b", base_url="http://provider-b/v1",
                    models=["m1"], max_concurrency=3)
        pool = LLMPool([k1, k2], stats_file="")
        pool._breaker_fail_threshold = 1
        # 让 provider-a 熔断
        pool._release(k1, error="fail", model="m1")
        assert pool._breakers["http://provider-a/v1"].state == CB_OPEN
        # provider-b 仍可获取
        k_acquired = pool._acquire(timeout=0.1, model="m1")
        assert k_acquired is not None
        assert k_acquired.base_url == "http://provider-b/v1"
        pool._release(k_acquired, success=True, model="m1")

    def test_expired_does_not_trigger_breaker(self):
        """401/402 expired 不计入熔断器失败（key 失效 ≠ provider 故障）"""
        k1 = LLMKey(key="sk-exp", base_url="http://provider-exp/v1",
                    models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        pool._breaker_fail_threshold = 1
        # expired 不应触发熔断
        pool._release(k1, expired=True, model="m1")
        assert "http://provider-exp/v1" not in pool._breakers or \
               pool._breakers["http://provider-exp/v1"].state == "closed"

    def test_get_breakers_snapshot(self):
        k1 = LLMKey(key="sk-snap", base_url="http://provider-snap/v1",
                    models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        pool._release(k1, error="fail", model="m1")
        snap = pool.get_breakers_snapshot()
        assert snap["enabled"] is True
        assert "http://provider-snap/v1" in snap["breakers"]
        assert snap["breakers"]["http://provider-snap/v1"]["state"] == "closed"


class TestQuotaTracking:
    """多维配额跟踪测试（SlidingWindowCounter + KeyStats）"""

    def test_sliding_window_counter_basic(self):
        from server.llm_pool.types import SlidingWindowCounter
        c = SlidingWindowCounter(window_seconds=1)
        c.add(100)
        c.add(200)
        assert c.sum() == 300.0
        assert c.count() == 2

    def test_sliding_window_evicts_expired(self):
        from server.llm_pool.types import SlidingWindowCounter
        c = SlidingWindowCounter(window_seconds=0.1)
        c.add(100, now=0.0)
        c.add(200, now=0.05)
        # 0.2s 后，前两条都过期
        assert c.sum(now=0.2) == 0.0
        assert c.count(now=0.2) == 0

    def test_sliding_window_partial_eviction(self):
        from server.llm_pool.types import SlidingWindowCounter
        c = SlidingWindowCounter(window_seconds=1.0)
        c.add(100, now=0.0)  # 将在 1.0 时过期
        c.add(200, now=0.5)  # 仍在窗口内
        c.add(300, now=0.8)  # 仍在窗口内
        assert c.sum(now=1.1) == 500.0  # 200 + 300
        assert c.count(now=1.1) == 2

    def test_parse_quota_dimension_valid(self):
        from server.llm_pool.types import parse_quota_dimension
        assert parse_quota_dimension("requests/hour") == ("requests", 3600)
        assert parse_quota_dimension("tokens/day") == ("tokens", 86400)
        assert parse_quota_dimension("requests/minute") == ("requests", 60)

    def test_parse_quota_dimension_invalid(self):
        from server.llm_pool.types import parse_quota_dimension
        assert parse_quota_dimension("invalid") is None
        assert parse_quota_dimension("requests/century") is None
        assert parse_quota_dimension("") is None

    def test_keystats_record_quota_requests(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        dims = [("requests/hour", 3600), ("tokens/hour", 3600)]
        ks.record_quota(dims, tokens=0)  # tokens=0 不计入 tokens/*
        ks.record_quota(dims, tokens=0)
        snap = ks.quota_snapshot()
        assert snap["requests/hour"]["sum"] == 2.0
        assert snap["requests/hour"]["count"] == 2
        # tokens/hour 不应有计数（tokens=0）
        assert snap["tokens/hour"]["sum"] == 0.0

    def test_keystats_record_quota_tokens(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        dims = [("requests/hour", 3600), ("tokens/hour", 3600)]
        ks.record_quota(dims, tokens=150)
        ks.record_quota(dims, tokens=250)
        snap = ks.quota_snapshot()
        assert snap["requests/hour"]["sum"] == 2.0
        assert snap["tokens/hour"]["sum"] == 400.0

    def test_pool_release_records_quota(self):
        k1 = LLMKey(key="sk-q", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        # 模拟成功调用
        pool._release(k1, success=True, tokens=200, model="m1")
        snap = k1.stats.quota_snapshot()
        # 默认配置应含 requests/hour
        assert "requests/hour" in snap
        assert snap["requests/hour"]["sum"] == 1.0
        # tokens/day 默认应含
        assert "tokens/day" in snap
        assert snap["tokens/day"]["sum"] == 200.0
        # model_stats 也应记录
        ms_snap = k1.model_stats["m1"].quota_snapshot()
        assert ms_snap["requests/hour"]["sum"] == 1.0

    def test_get_status_includes_quota_summary(self):
        k1 = LLMKey(key="sk-qs", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        status = pool.get_status()
        assert "quota_tracking_enabled" in status
        assert "quota_tracking_dimensions" in status
        assert isinstance(status["quota_tracking_dimensions"], list)


class TestLatencyPercentiles:
    """p50/p95/p99 延迟分位测试"""

    def test_percentile_empty_returns_none(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        assert ks.p50() is None
        assert ks.p95() is None
        assert ks.p99() is None
        assert ks.latency_sample_count() == 0

    def test_percentile_single_sample(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        ks.record_latency(150.0)
        assert ks.p50() == 150.0
        assert ks.p95() == 150.0
        assert ks.p99() == 150.0

    def test_percentile_multiple_samples(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        for d in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]:
            ks.record_latency(d)
        # 10 样本，nearest-rank p50 = rank=ceil(0.5*10)=5 → sorted[4]=500
        assert ks.p50() == 500.0
        # p95 = rank=ceil(0.95*10)=10 → sorted[9]=1000
        assert ks.p95() == 1000.0

    def test_record_latency_ignores_negative(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        ks.record_latency(-10)
        ks.record_latency(None)
        assert ks.latency_sample_count() == 0

    def test_sliding_window_maxlen(self):
        from server.llm_pool.types import KeyStats
        ks = KeyStats()
        # maxlen=100，写 150 个样本应只保留最后 100 个
        for i in range(150):
            ks.record_latency(float(i))
        assert ks.latency_sample_count() == 100
        # 最旧的 50 个被淘汰，保留 [50, 51, ..., 149]
        # p50: rank=ceil(0.5*100)=50 → sorted[49] = 50+49 = 99
        assert ks.p50() == 99.0
        # p95: rank=ceil(0.95*100)=95 → sorted[94] = 50+94 = 144
        assert ks.p95() == 144.0

    def test_percentile_of_helper(self):
        from server.llm_pool.pool import _percentile_of
        assert _percentile_of([], 50) is None
        assert _percentile_of([42], 50) == 42.0
        assert _percentile_of([1, 2, 3, 4, 5], 50) == 3.0
        assert _percentile_of([1, 2, 3, 4, 5], 95) == 5.0

    def test_pool_release_records_latency(self):
        k1 = LLMKey(key="sk-lat", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        # 模拟 3 次调用（不同延迟）
        pool._release(k1, success=True, model="m1", duration_ms=100.0)
        pool._release(k1, success=True, model="m1", duration_ms=300.0)
        pool._release(k1, success=True, model="m1", duration_ms=500.0)
        # key 级和 model 级都应有样本
        assert k1.stats.latency_sample_count() == 3
        assert k1.model_stats["m1"].latency_sample_count() == 3
        assert k1.stats.p50() == 300.0  # sorted=[100,300,500], rank=ceil(0.5*3)=2 → sorted[1]=300

    def test_get_status_includes_latency_fields(self):
        k1 = LLMKey(key="sk-lat-s", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1], stats_file="")
        pool._release(k1, success=True, model="m1", duration_ms=200.0)
        status = pool.get_status()
        key_stats = status["keys"][0]["stats"]
        assert "latency_p50" in key_stats
        assert "latency_p95" in key_stats
        assert "latency_p99" in key_stats
        assert "latency_sample_count" in key_stats
        assert key_stats["latency_p50"] == 200.0
        assert key_stats["latency_sample_count"] == 1
        # per-model stats 也应有 latency
        model_stats = status["keys"][0]["models"][0]["stats"]
        assert "latency_p50" in model_stats
        assert model_stats["latency_p50"] == 200.0

    def test_get_models_summary_includes_latency(self):
        k1 = LLMKey(key="sk-lat-m", base_url="http://x", models=["m1"], max_concurrency=3)
        k2 = LLMKey(key="sk-lat-m2", base_url="http://y", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1, k2], stats_file="")
        pool._release(k1, success=True, model="m1", duration_ms=100.0)
        pool._release(k2, success=True, model="m1", duration_ms=300.0)
        summary = pool.get_models_summary()
        m1_info = next(m for m in summary["models"] if m["name"] == "m1")
        # 跨 key 聚合分位
        assert "latency_p50" in m1_info["stats"]
        assert m1_info["stats"]["latency_p50"] == 100.0  # sorted=[100,300], rank=ceil(0.5*2)=1 → sorted[0]=100
        assert m1_info["stats"]["latency_sample_count"] == 2


# ==================== v14: 压缩 + 评分路由 ====================

class TestCompression:
    """v14 Phase 3.1+3.2 RTK + Caveman 消息压缩"""

    def test_compress_off_returns_original(self):
        from server.llm_pool.compression import compress_messages
        msgs = [{"role": "user", "content": "hello world"}]
        result, stats = compress_messages(msgs, mode="off")
        assert result is msgs
        assert stats.applied is False
        assert stats.mode == "off"

    def test_compress_invalid_mode_treated_as_off(self):
        from server.llm_pool.compression import compress_messages
        msgs = [{"role": "user", "content": "x" * 5000}]
        result, stats = compress_messages(msgs, mode="invalid_mode")
        assert result is msgs
        assert stats.applied is False

    def test_compress_lite_strips_code_block_line_numbers(self):
        from server.llm_pool.compression import compress_messages
        # 构造一个带行号前缀的 code block，超过 min_length 阈值
        code_lines = [f"  {i}: line content {i}" for i in range(200)]
        code_block = "```python\n" + "\n".join(code_lines) + "\n```"
        msgs = [{"role": "user", "content": code_block}]
        result, stats = compress_messages(msgs, mode="lite", min_length=100)
        assert stats.applied is True
        assert "strip_line_numbers" in stats.rules_applied
        assert len(result[0]["content"]) < len(code_block)

    def test_compress_lite_dedup_stack_trace(self):
        from server.llm_pool.compression import compress_messages
        # 构造重复 stack trace 行
        stack_lines = ["  at com.example.Foo.bar(Foo.java:42)"] * 50
        # 补足长度超 min_length
        code = "```java\n" + "\n".join(stack_lines) + "\n" + "x" * 3000 + "\n```"
        msgs = [{"role": "user", "content": code}]
        result, stats = compress_messages(msgs, mode="lite", min_length=100)
        assert stats.applied is True
        assert any(r.startswith("dedup_stack_trace") for r in stats.rules_applied)
        assert len(result[0]["content"]) < len(code)

    def test_compress_lite_truncates_long_block(self):
        from server.llm_pool.compression import compress_messages
        # 构造超长 code block（>50 行触发截断）
        lines = [f"line_{i}" for i in range(200)]
        code = "```\n" + "\n".join(lines) + "\n```"
        msgs = [{"role": "user", "content": code}]
        result, stats = compress_messages(msgs, mode="lite", min_length=100)
        assert stats.applied is True
        assert any(r.startswith("truncate_middle") for r in stats.rules_applied)
        assert "lines omitted" in result[0]["content"]

    def test_compress_standard_collapses_whitespace_latin(self):
        from server.llm_pool.compression import compress_messages
        # 多余空白 + 重复标点 + 冗长短语
        text = "Hello    world!!!    In order to test???" + "x" * 3000
        msgs = [{"role": "user", "content": text}]
        result, stats = compress_messages(msgs, mode="standard", min_length=100)
        assert stats.applied is True
        assert "collapse_whitespace" in stats.rules_applied
        assert "merge_punctuation" in stats.rules_applied
        assert "to" in result[0]["content"]  # "in order to" → "to"

    def test_compress_standard_cjk_whitespace(self):
        from server.llm_pool.compression import compress_messages
        # 中文 + 全角空格
        text = "你好　　世界   　　测试" + "字" * 3000
        msgs = [{"role": "user", "content": text}]
        result, stats = compress_messages(msgs, mode="standard", min_length=100)
        assert stats.applied is True
        assert "collapse_cjk_whitespace" in stats.rules_applied

    def test_compress_aggressive_cjk_filler_filter(self):
        from server.llm_pool.compression import compress_messages
        # aggressive 模式过滤冗余虚词
        text = "事实上，总的来说，这段代码很好。" + "字" * 3000
        msgs = [{"role": "user", "content": text}]
        result, stats = compress_messages(msgs, mode="aggressive", min_length=100)
        assert stats.applied is True
        assert any(r.startswith("cjk_filler_filter") for r in stats.rules_applied)
        assert "事实上" not in result[0]["content"]

    def test_bloat_protection_single_message(self):
        """压缩后变长则回退原文（单条保护）"""
        from server.llm_pool.compression import compress_messages
        # 短文本不会被压缩（不达 min_length 阈值）
        text = "short text"
        msgs = [{"role": "user", "content": text}]
        result, stats = compress_messages(msgs, mode="standard", min_length=2000)
        assert result is msgs  # 未压缩
        assert stats.applied is False

    def test_bloat_protection_global(self):
        """整体未压缩则返回原 messages"""
        from server.llm_pool.compression import compress_messages
        # 所有消息都太短，未达 min_length
        msgs = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "reply"},
        ]
        result, stats = compress_messages(msgs, mode="standard", min_length=2000)
        assert result is msgs
        assert stats.applied is False

    def test_min_length_threshold(self):
        """min_length 阈值控制：低于阈值不压缩"""
        from server.llm_pool.compression import compress_messages
        # 构造一个会触发压缩的内容（500 字空白）
        text = "hello    " * 200  # ~2000 chars，无 code block，仅 prose 规则
        msgs = [{"role": "user", "content": text}]
        # min_length=10000 时不会触发（估算 token 不足）
        result1, stats1 = compress_messages(msgs, mode="standard", min_length=10000)
        assert stats1.applied is False
        # min_length=100 时会触发
        result2, stats2 = compress_messages(msgs, mode="standard", min_length=100)
        assert stats2.applied is True

    def test_multimodal_content_untouched(self):
        """多模态 content（list 类型）原样保留"""
        from server.llm_pool.compression import compress_messages
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "x" * 5000},
                {"type": "image_url", "image_url": "http://x"},
            ]}
        ]
        result, stats = compress_messages(msgs, mode="standard", min_length=100)
        assert result is msgs  # 多模态不动
        assert stats.applied is False

    def test_fail_open_on_exception(self):
        """异常时返回原 messages"""
        from server.llm_pool.compression import compress_messages
        # 传入非 messages 类型触发异常
        result, stats = compress_messages(None, mode="standard", min_length=100)
        assert result is None
        assert stats.applied is False

    def test_estimate_tokens_cjk(self):
        from server.llm_pool.compression import estimate_tokens, is_cjk
        # CJK 文本估算：2 char/token
        assert estimate_tokens("你好") == 1  # 2/2=1
        # 拉丁文本：4 char/token
        assert estimate_tokens("abcd") == 1  # 4/4=1
        # 混合
        mixed = "你好abcd"
        assert is_cjk(mixed) is True
        # 2 CJK + 4 latin = 2/2 + 4/4 = 1 + 1 = 2
        assert estimate_tokens(mixed) == 2


class TestScoring:
    """v14 Phase 3.3 Auto 评分路由（6 因子加权）"""

    def test_score_basic_returns_float(self):
        from server.llm_pool.scoring import score_key
        k = LLMKey(key="sk-s", base_url="http://x", models=["m1"], max_concurrency=3)
        s = score_key(k)
        assert isinstance(s, float)
        assert 0.0 <= s <= 1.0

    def test_score_health_high_for_successful_key(self):
        from server.llm_pool.scoring import WEIGHT_HEALTH, score_key
        k = LLMKey(key="sk-ok", base_url="http://x", models=["m1"], max_concurrency=3)
        # 模拟成功调用
        k.stats.ok = 10
        k.stats.fail = 0
        k.stats.rate_limited = 0
        s = score_key(k)
        # health 因子 = 1.0，权重 0.25
        # 其他因子可能不全为 1.0，但 health 应贡献至少 0.25
        assert s >= WEIGHT_HEALTH * 1.0  # 至少 health 部分

    def test_score_health_low_for_failing_key(self):
        from server.llm_pool.scoring import score_key
        k = LLMKey(key="sk-bad", base_url="http://x", models=["m1"], max_concurrency=3)
        k.stats.ok = 0
        k.stats.fail = 10
        k.stats.rate_limited = 0
        s = score_key(k)
        # health 因子 = 0.0
        assert s < 0.5  # 失败 key 分数应较低

    def test_score_quota_low_for_saturated_key(self):
        from server.llm_pool.scoring import score_key
        k = LLMKey(key="sk-sat", base_url="http://x", models=["m1"], max_concurrency=3)
        k.saturation = 1.0  # 完全饱和
        k.saturation_updated_at = time.time()
        s = score_key(k)
        # quota_remaining = 1 - 1.0 = 0.0
        assert s < 0.7  # 饱和 key 分数受限

    def test_score_latency_high_for_low_latency_key(self):
        from server.llm_pool.scoring import score_key
        k = LLMKey(key="sk-fast", base_url="http://x", models=["m1"], max_concurrency=3)
        # 模拟低延迟调用
        for ms in [50, 60, 70, 80, 90]:
            k.stats.record_latency(ms)
        s_fast = score_key(k)
        # 模拟高延迟调用
        k2 = LLMKey(key="sk-slow", base_url="http://x", models=["m1"], max_concurrency=3)
        for ms in [5000, 6000, 7000]:
            k2.stats.record_latency(ms)
        s_slow = score_key(k2)
        assert s_fast > s_slow
        # latency 贡献应可衡量
        assert (s_fast - s_slow) > 0.01

    def test_score_cost_low_for_high_tier(self):
        """tier 1（最便宜）cost_inv=1.0；tier 5（最贵）cost_inv=0.2"""
        from server.llm_pool.scoring import _factor_cost_inv
        k1 = LLMKey(key="sk-t1", base_url="http://x", models=["m1"], max_concurrency=3,
                    model_tiers={"m1": 1})
        k5 = LLMKey(key="sk-t5", base_url="http://x", models=["m5"], max_concurrency=3,
                    model_tiers={"m5": 5})
        c1 = _factor_cost_inv(k1, (1, 5))
        c5 = _factor_cost_inv(k5, (1, 5))
        assert c1 == 1.0
        assert c5 == 0.2
        assert c1 > c5

    def test_score_tier_match_uses_registry(self):
        from server.llm_pool.scoring import _factor_tier_match
        # agent_chat 的 default_tier = (3, 5)
        k_matched = LLMKey(key="sk-m", base_url="http://x", models=["m1"],
                           max_concurrency=3, model_tiers={"m1": 3})
        k_unmatched = LLMKey(key="sk-u", base_url="http://x", models=["m2"],
                             max_concurrency=3, model_tiers={"m2": 1})
        # 命中：tier 3 在 agent_chat 的 (3,5) 范围内
        assert _factor_tier_match(k_matched, "agent_chat", (0, 0)) == 1.0
        # 未命中：tier 1 不在 (3,5) 范围内
        assert _factor_tier_match(k_unmatched, "agent_chat", (0, 0)) == 0.0

    def test_score_lkgp_bonus_for_sticky_session(self):
        from server.llm_pool.scoring import _factor_lkgp_bonus
        k = LLMKey(key="sk-sess", base_url="http://x", models=["m1"], max_concurrency=3,
                   key_id="key-001")
        # 命中：session_id 提供且 sticky_key_id 匹配
        assert _factor_lkgp_bonus(k, "sess-1", "key-001") == 1.0
        # 未命中：sticky_key_id 不匹配
        assert _factor_lkgp_bonus(k, "sess-1", "key-999") == 0.0
        # 无 session_id
        assert _factor_lkgp_bonus(k, None, None) == 0.0

    def test_score_neutral_for_no_data(self):
        """冷启动期无统计数据时给中性分"""
        from server.llm_pool.scoring import _NEUTRAL, score_key
        k = LLMKey(key="sk-cold", base_url="http://x", models=["m1"], max_concurrency=3)
        # 无 ok/fail/rate_limited 数据，无 latency_samples
        s = score_key(k)
        # health + latency 给中性分 0.5；其他因子也多数中性
        # 总分应在 0.3-0.7 之间（不会过度偏向任何方向）
        assert 0.2 < s < 0.8
        assert _NEUTRAL == 0.5

    def test_score_keys_sorted_descending(self):
        from server.llm_pool.scoring import score_keys
        k_high = LLMKey(key="sk-h", base_url="http://x", models=["m1"], max_concurrency=3)
        k_high.stats.ok = 100
        k_high.stats.fail = 0
        k_low = LLMKey(key="sk-l", base_url="http://x", models=["m1"], max_concurrency=3)
        k_low.stats.ok = 0
        k_low.stats.fail = 100
        scored = score_keys([k_low, k_high])  # 故意把 low 放前面
        assert scored[0][0] is k_high  # 最高分在最前
        assert scored[0][1] > scored[1][1]

    def test_score_fail_open_returns_neutral(self):
        """评分异常时返回中性分（不抛异常）"""
        from server.llm_pool.scoring import _NEUTRAL, score_key
        # 构造一个会让 stats 访问异常的对象（通过 mock）
        class BrokenKey:
            key_id = "broken"
            name = "broken"
            models = []
            model_tiers = {}
            stats = None  # 故意设为 None 触发异常
            base_url = ""
            @property
            def effective_saturation(self):
                return lambda now=0: 0.0
        s = score_key(BrokenKey())  # type: ignore
        assert s == _NEUTRAL


class TestPoolCompressionRouting:
    """v14 压缩 + 评分路由集成测试"""

    def test_pool_init_loads_compression_config(self):
        k = LLMKey(key="sk-c", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        # 默认 compression_mode = "off"（config.toml 未配置时）
        assert pool._compression_mode == "off"
        assert pool._compression_min_length == 2000

    def test_pool_init_loads_routing_config(self):
        k = LLMKey(key="sk-r", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        # 默认 strategy = "round_robin"
        assert pool._routing_strategy == "round_robin"
        assert pool._routing_mode_pack == "balanced"

    def test_pool_get_status_includes_v14_fields(self):
        k = LLMKey(key="sk-s", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        status = pool.get_status()
        assert "compression_mode" in status
        assert "compression_min_length" in status
        assert "compression_total" in status
        assert "compression_saved_chars" in status
        assert "routing_strategy" in status
        assert "routing_mode_pack" in status
        assert "routing_last_scores" in status
        assert status["compression_mode"] == "off"
        assert status["routing_strategy"] == "round_robin"

    def test_pool_round_robin_default(self):
        """默认 round_robin 策略下，连续 acquire 走 round-robin"""
        k1 = LLMKey(key="sk-rr1", base_url="http://a", models=["m1"], max_concurrency=3)
        k2 = LLMKey(key="sk-rr2", base_url="http://b", models=["m1"], max_concurrency=3)
        pool = LLMPool([k1, k2], stats_file="")
        # 强制 round_robin（默认值）
        pool._routing_strategy = "round_robin"
        acquired = []
        for _ in range(4):
            k = pool._acquire(timeout=0.1)
            if k:
                acquired.append(k.key)
                pool._release(k, success=True)
        # round-robin 应交替
        assert len(set(acquired)) == 2

    def test_pool_auto_strategy_uses_scoring(self, monkeypatch):
        """auto 策略下用评分选路"""
        k_high = LLMKey(key="sk-h", base_url="http://h", models=["m1"], max_concurrency=3,
                        model_tiers={"m1": 3})
        k_low = LLMKey(key="sk-l", base_url="http://l", models=["m1"], max_concurrency=3,
                       model_tiers={"m1": 3})
        # 让 k_high 评分明显高于 k_low
        k_high.stats.ok = 100
        k_high.stats.fail = 0
        k_low.stats.ok = 0
        k_low.stats.fail = 100
        pool = LLMPool([k_low, k_high], stats_file="")  # 故意把 k_low 放前面
        # 强制 auto 策略
        pool._routing_strategy = "auto"
        # 连续 acquire 多次，应该总是选 k_high（评分更高）
        acquired = []
        for _ in range(5):
            k = pool._acquire(timeout=0.1)
            if k:
                acquired.append(k.key)
                pool._release(k, success=True)
        # auto 策略应倾向高评分 key（k_high）
        assert acquired.count("sk-h") > acquired.count("sk-l")

    def test_pool_auto_updates_last_scores(self):
        """auto 策略 acquire 后 _last_scores 被更新"""
        k1 = LLMKey(key="sk-a1", base_url="http://x", models=["m1"], max_concurrency=3,
                    key_id="id-1")
        k2 = LLMKey(key="sk-a2", base_url="http://y", models=["m1"], max_concurrency=3,
                    key_id="id-2")
        pool = LLMPool([k1, k2], stats_file="")
        pool._routing_strategy = "auto"
        k = pool._acquire(timeout=0.1)
        assert k is not None
        pool._release(k, success=True)
        # _last_scores 应有两条记录
        assert len(pool._last_scores) == 2
        assert "id-1" in pool._last_scores or "sk-a1..." in pool._last_scores
        assert "id-2" in pool._last_scores or "sk-a2..." in pool._last_scores


class TestPoolModelHealth:
    """v15 model 级降级测试（consecutive_fails + disabled + 探针恢复 + 错误类型退避）"""

    def test_model_disabled_after_consecutive_fails(self):
        """连续失败达阈值后 model 被标记 disabled"""
        k = LLMKey(key="sk-mh", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 3  # 缩小阈值便于测试
        # 连续 3 次失败
        for _ in range(3):
            k.active_count += 1
            pool._release(k, model="m1", error="HTTP 500", error_type="http_5xx")
        ms = k.model_stats["m1"]
        assert ms.consecutive_fails == 3
        assert "m1" in k.model_disabled
        assert k.is_model_available("m1") is False

    def test_consecutive_fails_reset_on_success(self):
        """成功调用清零 consecutive_fails"""
        k = LLMKey(key="sk-rs", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 5
        # 2 次失败
        for _ in range(2):
            k.active_count += 1
            pool._release(k, model="m1", error="HTTP 500", error_type="http_5xx")
        assert k.model_stats["m1"].consecutive_fails == 2
        # 1 次成功
        k.active_count += 1
        pool._release(k, success=True, model="m1", tokens=10)
        assert k.model_stats["m1"].consecutive_fails == 0

    def test_cooldown_backoff_exponential(self):
        """指数退避：cooldown = min(base * 2^attempt, cooldown_max)"""
        k = LLMKey(key="sk-exp", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 10  # 防止过早 disabled
        pool._cooldown_base_timeout = 5.0
        pool._cooldown_max = 60.0
        # 第 1 次失败：attempt=0 → base*1 = 5s
        k.active_count += 1
        pool._release(k, model="m1", error="timeout", error_type="timeout")
        cd1 = k.model_cooldown_remaining("m1")
        assert 4.5 < cd1 <= 5.0, f"第 1 次失败 cooldown 应≈5s，实际={cd1}"
        # 第 2 次失败：attempt=1 → base*2 = 10s
        k.active_count += 1
        pool._release(k, model="m1", error="timeout", error_type="timeout")
        cd2 = k.model_cooldown_remaining("m1")
        assert 9.0 < cd2 <= 10.0, f"第 2 次失败 cooldown 应≈10s，实际={cd2}"
        # 第 3 次失败：attempt=2 → base*4 = 20s
        k.active_count += 1
        pool._release(k, model="m1", error="timeout", error_type="timeout")
        cd3 = k.model_cooldown_remaining("m1")
        assert 19.0 < cd3 <= 20.0, f"第 3 次失败 cooldown 应≈20s，实际={cd3}"
        # 第 4 次失败：attempt=3 → base*8 = 40s
        k.active_count += 1
        pool._release(k, model="m1", error="timeout", error_type="timeout")
        cd4 = k.model_cooldown_remaining("m1")
        assert 39.0 < cd4 <= 40.0, f"第 4 次失败 cooldown 应≈40s，实际={cd4}"
        # 第 5 次失败：attempt=4 → base*16=80s，但封顶 60s
        k.active_count += 1
        pool._release(k, model="m1", error="timeout", error_type="timeout")
        cd5 = k.model_cooldown_remaining("m1")
        assert 59.0 < cd5 <= 60.0, f"第 5 次失败 cooldown 应封顶 60s，实际={cd5}"

    def test_error_type_cooldown_mapping(self):
        """不同错误类型对应不同 cooldown base"""
        k = LLMKey(key="sk-et", base_url="http://x", models=["m1", "m2", "m3", "m4", "m5"],
                   max_concurrency=10)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 100  # 防 disabled
        pool._cooldown_base_timeout = 5.0
        pool._cooldown_base_http_5xx = 10.0
        pool._cooldown_base_http_other = 10.0
        pool._cooldown_base_empty_content = 15.0
        pool._cooldown_base_exception = 20.0

        cases = [
            ("m1", "timeout", 5.0),
            ("m2", "http_5xx", 10.0),
            ("m3", "http_other", 10.0),
            ("m4", "empty_content", 15.0),
            ("m5", "exception", 20.0),
        ]
        for model, etype, expected_base in cases:
            k.active_count += 1
            pool._release(k, model=model, error=f"err {etype}", error_type=etype)
            cd = k.model_cooldown_remaining(model)
            assert expected_base - 0.5 < cd <= expected_base, \
                f"error_type={etype} 应≈{expected_base}s，实际={cd}"

    def test_429_not_count_consecutive_fails(self):
        """429 rate_limited 不计 consecutive_fails"""
        k = LLMKey(key="sk-429", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 3
        # 连续 5 次 429
        for _ in range(5):
            k.active_count += 1
            pool._release(k, rate_limited=True, cooldown=10, model="m1")
        ms = k.model_stats["m1"]
        assert ms.consecutive_fails == 0, "429 不应计 consecutive_fails"
        assert "m1" not in k.model_disabled, "429 不应触发 disabled"
        # 但 rate_limited 计数应有 5
        assert ms.rate_limited == 5

    def test_model_probe_release_after_interval(self):
        """disabled model 距 last_probe_at >= probe_interval 后可放探针，成功后解除 disabled"""
        k = LLMKey(key="sk-pr", base_url="http://x", models=["m1"], max_concurrency=3)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 2
        pool._model_health_probe_interval = 100.0  # 100s 间隔
        pool._model_health_probe_max_concurrency = 1
        # 2 次失败触发 disabled
        for _ in range(2):
            k.active_count += 1
            pool._release(k, model="m1", error="HTTP 500", error_type="http_5xx")
        assert "m1" in k.model_disabled
        # 立即尝试探针：应失败（未到窗口）
        assert k.try_acquire_probe("m1", 100.0, 1) is False
        # 模拟时间过去 101s（直接修改 last_probe_at）
        k.model_disabled["m1"]["last_probe_at"] = time.time() - 101.0
        # 现在探针应成功
        assert k.try_acquire_probe("m1", 100.0, 1) is True
        assert k.model_disabled["m1"]["probe_in_flight"] is True
        # 探针成功 → resolve_probe(success=True) 解除 disabled
        k.resolve_probe("m1", success=True)
        assert "m1" not in k.model_disabled
        assert k.model_stats["m1"].consecutive_fails == 0

    def test_model_probe_concurrency_limit(self):
        """probe_max_concurrency=1 时，同时只能有一个探针在飞"""
        k = LLMKey(key="sk-pc", base_url="http://x", models=["m1", "m2"], max_concurrency=5)
        pool = LLMPool([k], stats_file="")
        pool._model_health_threshold = 1  # 1 次失败就 disabled
        pool._model_health_probe_interval = 0.0  # 立即可探针
        pool._model_health_probe_max_concurrency = 1
        # 让 m1 和 m2 都 disabled
        k.active_count += 1
        pool._release(k, model="m1", error="err", error_type="exception")
        k.active_count += 1
        pool._release(k, model="m2", error="err", error_type="exception")
        assert "m1" in k.model_disabled
        assert "m2" in k.model_disabled
        # 第 1 个探针应成功
        got1 = k.try_acquire_probe("m1", 0.0, 1)
        assert got1 is True
        # 第 2 个探针应失败（并发已满）
        got2 = k.try_acquire_probe("m2", 0.0, 1)
        assert got2 is False
        # 释放第 1 个探针后，第 2 个应能成功
        k.resolve_probe("m1", success=False)
        # m2 的 last_probe_at 已在 try_acquire_probe 失败时未更新（失败时不更新）
        # 但 probe_interval=0，所以仍可尝试
        got3 = k.try_acquire_probe("m2", 0.0, 1)
        assert got3 is True


# ==================== Ticket 03：pool.call/_acquire 集成测试 ====================

class TestPoolAcquireModelHealth:
    """v15 pool 调用链路集成 model_disabled 的测试

    验证 A 保险（_acquire 跳过无可用 model 的 key）、C 保险（call req_model 排除 disabled）、
    _acquire=None 记 recent_calls、所有 model disabled 时的探针 fallback 行为。
    """

    def test_model_disabled_excluded_from_acquire(self):
        """A 保险：disabled model 不计入 has_avail，但 key 因 has_disabled=True 被保留给探针 fallback

        构造仅含 disabled model 的 key，验证 _acquire 仍返回该 key（不跳过），
        保留给 call() 的 try_acquire_probe 探针路径处理。
        """
        k = LLMKey(key="sk-a", base_url="http://x", models=["m1"], max_concurrency=2)
        k.mark_model_disabled("m1")
        pool = LLMPool([k], stats_file="")
        got = pool._acquire(timeout=0.1)
        assert got is not None
        assert got.key == "sk-a"

    def test_model_disabled_excluded_from_req_model(self, monkeypatch):
        """C 保险：call() 的 req_model 选取排除 disabled model，自动选可用 model

        构造含 m1(disabled) + m2(可用) 的 key，mock get_sync_client().post 返回 200，
        验证实际请求的 model 是 m2 而非 m1。

        T07：pool._call_openai 改用 httpx.Client（get_sync_client()）替代 requests，
        mock 从 monkeypatch requests.post 改为 monkeypatch get_sync_client。
        """
        from server.llm_pool import pool as pool_mod

        k = LLMKey(key="sk-b", base_url="http://x", models=["m1", "m2"], max_concurrency=2)
        k.model_tiers = {"m1": 3, "m2": 3}  # 显式设 tier，否则 has_tier_in_range 因空 dict 返回 False
        k.mark_model_disabled("m1")
        pool = LLMPool([k], stats_file="")

        class _FakeResp:
            status_code = 200
            headers = {}
            text = ""

            def json(self):
                return {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 10},
                    "model": "m2",
                }

        called_models: list[str] = []

        class _FakeSyncClient:
            def post(self, url, **kwargs):
                called_models.append(kwargs.get("json", {}).get("model", ""))
                return _FakeResp()

        monkeypatch.setattr(pool_mod, "get_sync_client", lambda: _FakeSyncClient())
        # 传 use_case 走 default_tier=(3,5)，触发 tier 过滤（需 model_tiers 非空）
        result = pool.call(messages=[{"role": "user", "content": "hi"}],
                           retries=1, use_case="agent_chat")
        assert result["ok"] is True
        # 请求体里的 model 是 m2（可用），不是 m1（disabled）
        assert called_models == ["m2"]
        # recent_calls 记录的 model 也是 m2
        recent = pool.get_recent_calls(limit=10)
        assert any(r["model"] == "m2" and r["success"] for r in recent)

    def test_acquire_none_records_call(self):
        """_acquire 返回 None 时，recent_calls 记录失败调用（避免面板"消失"）

        空 keys 池 → _acquire 立即返回 None → call() 记 _record_call(success=False)。
        """
        pool = LLMPool([], stats_file="")
        result = pool.call(messages=[{"role": "user", "content": "hi"}], retries=1)
        assert result["ok"] is False
        assert "no available keys" in result["error"]
        recent = pool.get_recent_calls(limit=10)
        assert len(recent) >= 1
        assert recent[0]["success"] is False
        assert "no available keys" in recent[0]["error"]

    def test_key_all_models_disabled_skipped(self, monkeypatch):
        """所有 model 都 disabled 且探针未到窗口时，call() 无法完成调用

        _acquire 保留该 key（has_disabled=True），但 call() 的 req_model 选取失败
        （探针 fallback 因 probe_interval 未到而 try_acquire_probe 返回 False），
        重试耗尽后返回 {"ok": False}，且不应发起任何 HTTP 请求。

        T07：mock get_sync_client().post 替代 requests.post。
        """
        from server.llm_pool import pool as pool_mod

        k = LLMKey(key="sk-c", base_url="http://x", models=["m1"], max_concurrency=2)
        k.model_tiers = {"m1": 3}  # 显式设 tier，让 _acquire 的 tier 过滤通过
        k.mark_model_disabled("m1")  # last_probe_at=now，探针窗口未到
        pool = LLMPool([k], stats_file="")
        pool._model_health_probe_interval = 300.0  # 探针窗口 300s

        class _FakeSyncClient:
            def post(self, url, **kwargs):
                raise AssertionError("不应发起 HTTP 请求（所有 model disabled 且探针未到窗口）")

        monkeypatch.setattr(pool_mod, "get_sync_client", lambda: _FakeSyncClient())
        result = pool.call(messages=[{"role": "user", "content": "hi"}],
                           retries=1, use_case="agent_chat")
        assert result["ok"] is False

    def test_acquire_skips_key_when_tier_model_in_cooldown(self):
        """v16：A 保险补 cooldown 检查——tier 匹配 model 全在 429 冷却时跳过该 key

        构造含 m1(tier 3, cooldown 中) + m2(tier 1, 可用) 的 key，
        _acquire(use_case=agent_chat, tier=(3,5)) 应返回 None（key 被 A 保险跳过）。
        修复前：A 保险不查 cooldown → key 被选中 → call() fallback 到 m2(tier 1)。
        """
        k = LLMKey(key="sk-e", base_url="http://x", models=["m1", "m2"], max_concurrency=2)
        k.model_tiers = {"m1": 3, "m2": 1}
        k.model_cooldowns = {"m1": time.time() + 300}  # m1 在 429 冷却中
        pool = LLMPool([k], stats_file="")
        got = pool._acquire(timeout=0.1, use_case="agent_chat")
        assert got is None  # key 被 A 保险跳过（tier 匹配 model 全在 cooldown）

    def test_call_no_tier_leak_when_tier_model_in_cooldown(self, monkeypatch):
        """v16：call() tier 约束激活时，范围内无可用 model 不降级到非 tier 范围 model

        构造含 m1(tier 3, cooldown 中) + m2(tier 1, 可用) 的 key，
        禁用 model_health（让 _acquire 放行该 key，隔离测试 call() 的 model 选择），
        use_case="agent_chat"(default_tier=(3,5))，
        验证不会选 m2（tier 1 不在 3-5 范围内），不发起 HTTP 请求。
        修复前：_first_available fallback 选了 m2(tier 1) → tier 泄漏。

        T07：mock get_sync_client().post 替代 requests.post。
        """
        from server.llm_pool import pool as pool_mod

        k = LLMKey(key="sk-f", base_url="http://x", models=["m1", "m2"], max_concurrency=2)
        k.model_tiers = {"m1": 3, "m2": 1}
        k.model_cooldowns = {"m1": time.time() + 300}  # m1 在 429 冷却中
        pool = LLMPool([k], stats_file="")
        pool._model_health_enabled = False  # 禁用 A 保险，让 _acquire 放行该 key

        class _FakeSyncClient:
            def post(self, url, **kwargs):
                raise AssertionError(
                    f"不应发起 HTTP 请求（tier 范围内无可用 model），"
                    f"但请求了 model={kwargs.get('json', {}).get('model', '')}"
                )

        monkeypatch.setattr(pool_mod, "get_sync_client", lambda: _FakeSyncClient())
        result = pool.call(messages=[{"role": "user", "content": "hi"}],
                           retries=1, use_case="agent_chat")
        assert result["ok"] is False


# ==================== Ticket 04：memory 统一走 pool 集成测试 ====================

class TestMemoryUsesPool:
    """v15 memory 模块统一走 pool 的集成测试

    验证 maintainer._call_llm_validate_with_429_flag 和 compress._llm_compress
    实际通过 pool.call / pool.call_simple 调用 LLM，且 use_case 正确。
    """

    def test_memory_maintainer_uses_pool(self, monkeypatch):
        """maintainer 的 LLM 验证走 pool.call(use_case="memory_validate")"""
        import os
        import tempfile

        from server.llm_pool import singleton as singleton_mod
        from server.memory.config import MemoryConfig
        from server.memory.maintainer import MemoryMaintainer
        from server.memory.store import MemoryStore

        # 准备 in-memory store + maintainer
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = MemoryStore(db_path)
            store.initialize()
            # 插入一条 pending 的记忆
            store.conn.execute(
                "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?)",
                ("test_key", "test_value", "2020-01-01 00:00:00"),
            )
            store.conn.execute(
                "INSERT INTO memory_meta (key, stale_score, validation_status) "
                "VALUES (?, ?, 'pending')",
                ("test_key", 30.0),
            )
            store.conn.commit()

            cfg = MemoryConfig()
            maintainer = MemoryMaintainer(store, cfg)

            # 构造 fake pool，记录调用参数并返回预定义 JSON
            recorded_calls: list[dict] = []

            class _FakePool:
                def call(self, messages, **kwargs):
                    recorded_calls.append({"messages": messages, **kwargs})
                    return {
                        "ok": True,
                        "content": '{"is_accurate": true, "reason": "测试", "suggested_action": "keep"}',
                    }

            monkeypatch.setattr(singleton_mod, "get_pool", lambda: _FakePool())

            # 执行 run_validate
            import asyncio
            stats = asyncio.run(maintainer.run_validate())

            # pool.call 被调用且 use_case 正确
            assert len(recorded_calls) == 1
            assert recorded_calls[0]["use_case"] == "memory_validate"
            assert recorded_calls[0]["retries"] == 1
            # messages 含 system + user
            assert len(recorded_calls[0]["messages"]) == 2
            assert recorded_calls[0]["messages"][0]["role"] == "system"
            assert recorded_calls[0]["messages"][1]["role"] == "user"

            # 验证结果写回 DB
            assert stats["validated"] == 1
            assert stats["keep"] == 1
            cur = store.conn.execute(
                "SELECT validation_status FROM memory_meta WHERE key = ?",
                ("test_key",),
            )
            assert cur.fetchone()[0] == "keep"

            store.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_memory_compress_uses_pool(self, monkeypatch):
        """compress 的 LLM 压缩走 pool.call_simple(use_case="memory_compress")"""
        import asyncio
        import os
        import tempfile

        from server.llm_pool import singleton as singleton_mod
        from server.memory.compress import CompressionPipeline
        from server.memory.store import MemoryStore

        # 准备 in-memory store + pipeline
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = MemoryStore(db_path)
            store.initialize()
            pipeline = CompressionPipeline(store, threshold=1, batch_size=10)

            # 构造 fake pool，记录调用参数并返回预定义摘要
            recorded_calls: list[dict] = []

            class _FakePool:
                def call_simple(self, prompt, system_prompt=None, **kwargs):
                    recorded_calls.append({
                        "prompt": prompt,
                        "system_prompt": system_prompt,
                        **kwargs,
                    })
                    return "压缩后的摘要文本"

            monkeypatch.setattr(singleton_mod, "get_pool", lambda: _FakePool())

            # 直接调用 _llm_compress（绕过 store 数据准备）
            messages = [
                {"role": "user", "source": "user", "content": "你好"},
                {"role": "assistant", "source": "agent", "content": "你好，有什么可以帮你？"},
            ]
            result = asyncio.run(pipeline._llm_compress(messages))

            # call_simple 被调用且 use_case 正确
            assert len(recorded_calls) == 1
            assert recorded_calls[0]["use_case"] == "memory_compress"
            assert recorded_calls[0]["retries"] == 1
            assert recorded_calls[0]["max_tokens"] == 16384
            assert recorded_calls[0]["temperature"] == 0.3
            # system_prompt 是摘要助手
            assert "摘要" in recorded_calls[0]["system_prompt"]
            # prompt 含对话内容
            assert "你好" in recorded_calls[0]["prompt"]

            # 返回值是 pool 的返回
            assert result == "压缩后的摘要文本"

            store.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
