"""VL Provider 级额度耗尽 Fallback 测试

SDD: temp/sdd/vl-provider-fallback/spec.md
覆盖 V1-V5 验收：
- V1 single_exhausted：单 provider 耗尽不阻止其他 provider
- V2 max_consecutive：连续 3 次额度耗尽当日不再试
- V3 all_exhausted：所有 provider 都 exhausted → 标记全局 + 返回 error
- V4 cross_day_reset：跨日清空 _provider_exhausted
- V5 health_status：status() 返回 provider_exhausted dict
"""

import datetime
import time

import pytest
from PIL import Image

from server.vl.remote_vl import RemoteVLClient

# ==================== fixtures ====================

@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    """创建隔离的 RemoteVLClient，不依赖真实 config.toml / keys.json"""
    client = RemoteVLClient()
    client._log_dir = tmp_path
    # 隔离 vl_quota
    from server.activity_tracker.vl_quota import VLQuotaManager
    test_quota = VLQuotaManager()
    test_quota.configure(quota_dir=tmp_path)
    client._vl_quota_override = test_quota
    monkeypatch.setattr(
        "server.config.get_vision_config",
        lambda: {
            "vl_enabled": True,
            "vl_max_image_edge": 1280,
            "vl_jpeg_quality": 85,
            # 短退避序列加速测试（3 次重试，每次 0.01s）
            "vl_retry_backoffs": (0.01, 0.01, 0.01),
        },
    )
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    # mock time.sleep 避免 fallback 测试中等待冷却的真实 sleep
    monkeypatch.setattr("server.vl.remote_vl.time.sleep", lambda x: None)
    client.reload_config()
    return client


def _make_resolved_key(key_id, label, model, base_url, tier=4, max_concurrency=1):
    """构造 mock ResolvedKey"""
    from server.llm_pool.key_store import ResolvedKey
    return ResolvedKey(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        base_url=base_url,
        model=model,
        models=[model],
        label=label,
        privacy_warning="",
        max_concurrency=max_concurrency,
        source="test",
        vision_params={"max_concurrency": max_concurrency, "timeout": 60, "rate_limit_cooldown": 60},
    )


def MODELSCOPE_KEY():
    return _make_resolved_key("ms", "ModelScope", "qwen3-vl", "https://api.modelscope.cn/v1", tier=4, max_concurrency=1)


def ICE_KEY():
    return _make_resolved_key("ice", "ICE", "gpt-5.6-luna", "https://api.ice.com/v1", tier=5, max_concurrency=10)


def _next_day_midnight_ts() -> float:
    """次日 00:00 的 time.time() 时间戳"""
    now = datetime.datetime.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


# ==================== Ticket 01: _mark_provider_exhausted + 配置 + 连续止损 ====================

class TestProviderExhaustedMark:
    """V2: 连续 3 次额度耗尽当日不再试 + 基础标记逻辑"""

    def test_single_mark_sets_cooldown_1h(self, vl_client):
        """单次标记：consecutive_count=1, cooldown_until ≈ now + 3600"""
        before = time.time()
        vl_client._mark_provider_exhausted("ms", "quota exceeded", kind="quota_exhausted", source="modelscope")
        after = time.time()
        pex = vl_client._provider_exhausted["ms"]
        assert pex["consecutive_count"] == 1
        assert pex["kind"] == "quota_exhausted"
        assert pex["source"] == "modelscope"
        assert pex["reason"] == "quota exceeded"
        # cooldown_until 在 [before+3600, after+3600] 区间
        assert before + 3600 <= pex["cooldown_until"] <= after + 3600
        assert pex["date"] == time.strftime("%Y%m%d")

    def test_consecutive_below_max_still_1h_cooldown(self, vl_client):
        """连续 2 次（< max=3）仍是 1h cooldown"""
        vl_client._mark_provider_exhausted("ms", "r1", kind="quota_exhausted", source="modelscope")
        before = time.time()
        vl_client._mark_provider_exhausted("ms", "r2", kind="quota_exhausted", source="modelscope")
        after = time.time()
        pex = vl_client._provider_exhausted["ms"]
        assert pex["consecutive_count"] == 2
        assert before + 3600 <= pex["cooldown_until"] <= after + 3600

    def test_max_consecutive_sets_next_day_midnight(self, vl_client):
        """连续 3 次额度耗尽 → cooldown_until 设为次日 00:00"""
        vl_client._mark_provider_exhausted("ms", "r1", kind="quota_exhausted", source="modelscope")
        vl_client._mark_provider_exhausted("ms", "r2", kind="quota_exhausted", source="modelscope")
        vl_client._mark_provider_exhausted("ms", "r3", kind="quota_exhausted", source="modelscope")
        pex = vl_client._provider_exhausted["ms"]
        assert pex["consecutive_count"] == 3
        expected = _next_day_midnight_ts()
        # cooldown_until 应在次日 00:00 附近（允许 5s 误差）
        assert abs(pex["cooldown_until"] - expected) < 5.0

    def test_is_provider_exhausted_respects_cooldown(self, vl_client):
        """_is_provider_exhausted：cooldown 未过期=True，过期=False"""
        vl_client._mark_provider_exhausted("ms", "r1", kind="quota_exhausted", source="modelscope")
        assert vl_client._is_provider_exhausted("ms") is True
        # 手动过期
        vl_client._provider_exhausted["ms"]["cooldown_until"] = time.time() - 1
        assert vl_client._is_provider_exhausted("ms") is False

    def test_is_provider_exhausted_unknown_key_false(self, vl_client):
        """未标记的 key → not exhausted"""
        assert vl_client._is_provider_exhausted("unknown") is False

    def test_config_defaults_loaded(self, vl_client):
        """reload_config 加载默认配置：cooldown=3600, max_consecutive=3"""
        assert vl_client._provider_exhausted_cooldown_seconds == 3600
        assert vl_client._provider_exhausted_max_consecutive == 3

    def test_config_override(self, monkeypatch, tmp_path):
        """配置项可覆盖：cooldown=1800, max_consecutive=5"""
        client = RemoteVLClient()
        client._log_dir = tmp_path
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {
                "vl_enabled": True,
                "vl_max_image_edge": 1280,
                "vl_jpeg_quality": 85,
                "vl_retry_backoffs": (2, 4, 8, 16, 32, 60),
                "vl_provider_exhausted_cooldown_seconds": 1800,
                "vl_provider_exhausted_max_consecutive": 5,
            },
        )
        client.reload_config()
        assert client._provider_exhausted_cooldown_seconds == 1800
        assert client._provider_exhausted_max_consecutive == 5
        # 连续 5 次才止损
        for i in range(4):
            client._mark_provider_exhausted("ms", f"r{i}", kind="quota_exhausted", source="modelscope")
            assert client._provider_exhausted["ms"]["consecutive_count"] == i + 1
            # 4 次 < 5，仍是 1h（1800s）cooldown
            assert client._provider_exhausted["ms"]["cooldown_until"] > time.time() + 1700
        client._mark_provider_exhausted("ms", "r5", kind="quota_exhausted", source="modelscope")
        # 第 5 次达止损 → 次日 00:00
        expected = _next_day_midnight_ts()
        assert abs(client._provider_exhausted["ms"]["cooldown_until"] - expected) < 5.0

    def test_reason_truncated_to_300(self, vl_client):
        """reason 超过 300 字符截断"""
        long_reason = "x" * 500
        vl_client._mark_provider_exhausted("ms", long_reason, kind="quota_exhausted", source="modelscope")
        assert len(vl_client._provider_exhausted["ms"]["reason"]) == 300


# ==================== Ticket 02: _call_with_failover provider 级 fallback ====================

class TestSingleExhaustedFallback:
    """V1: 单 provider 耗尽不阻止其他 provider"""

    def test_modelscope_exhausted_fallback_to_ice(self, vl_client, monkeypatch):
        """modelscope 非并发 429 → 标记 provider 耗尽 → fallback 到 ice 成功"""
        candidates = [MODELSCOPE_KEY(), ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        call_log = []

        def _mock_call_chat(rk, messages, timeout=None):
            call_log.append(rk.key_id)
            if rk.key_id == "ms":
                raise Exception("429 Too Many Requests - quota exceeded")
            # ice 成功
            return {"choices": [{"message": {"content": "ice answer"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        # 设置 per-provider last_call_finished_ts 触发非并发判定（距上次 >= 60s）
        vl_client._get_runtime_state("ms").last_call_finished_ts = time.time() - 120

        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        assert result["status"] == "ok"
        assert result["answer"] == "ice answer"
        # modelscope 被标记为 exhausted
        assert "ms" in vl_client._provider_exhausted
        assert vl_client._provider_exhausted["ms"]["consecutive_count"] == 1
        # ice 未被标记
        assert "ice" not in vl_client._provider_exhausted
        # 全局 daily_exhausted 未被标记（单 provider 耗尽不标记全局）
        assert vl_client._daily_exhausted is False

    def test_exhausted_provider_skipped_on_next_call(self, vl_client, monkeypatch):
        """已耗尽的 provider 在下次调用时被跳过，直接用 ice"""
        # 预先标记 modelscope 耗尽
        vl_client._mark_provider_exhausted("ms", "prev 429", kind="quota_exhausted", source="modelscope")
        candidates = [MODELSCOPE_KEY(), ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        call_log = []

        def _mock_call_chat(rk, messages, timeout=None):
            call_log.append(rk.key_id)
            return {"choices": [{"message": {"content": "ice answer"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        assert result["status"] == "ok"
        # modelscope 被跳过，只有 ice 被调用
        assert "ms" not in call_log
        assert "ice" in call_log


class TestAllExhausted:
    """V3: 所有 provider 都 exhausted → 标记全局 + 返回 error"""

    def test_all_providers_exhausted_marks_global(self, vl_client, monkeypatch):
        """两个 provider 都非并发 429 → 返回 daily_exhausted + vl_quota.mark_daily_exhausted 被调"""
        candidates = [MODELSCOPE_KEY(), ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        def _mock_call_chat(rk, messages, timeout=None):
            raise Exception("429 Too Many Requests - quota exceeded")

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        # 设置 per-provider last_call_finished_ts 触发非并发判定（距上次 >= 60s）
        vl_client._get_runtime_state("ms").last_call_finished_ts = time.time() - 120
        vl_client._get_runtime_state("ice").last_call_finished_ts = time.time() - 120

        # 捕获 vl_quota.mark_daily_exhausted 调用
        quota_calls = []
        monkeypatch.setattr(
            vl_client._vl_quota_override,
            "mark_daily_exhausted",
            lambda reason, kind="upstream_overload", source=None: quota_calls.append({"reason": reason, "kind": kind, "source": source}),
        )

        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        assert result["status"] == "error"
        assert result.get("daily_exhausted") is True
        # 两个 provider 都被标记
        assert "ms" in vl_client._provider_exhausted
        assert "ice" in vl_client._provider_exhausted
        # 全局 daily_exhausted 被标记
        assert vl_client._daily_exhausted is True
        # vl_quota.mark_daily_exhausted 被调
        assert len(quota_calls) >= 1

    def test_all_exhausted_skips_already_exhausted(self, vl_client, monkeypatch):
        """modelscope 已耗尽 → 只试 ice → ice 也 429 → 标记全局"""
        vl_client._mark_provider_exhausted("ms", "prev 429", kind="quota_exhausted", source="modelscope")
        candidates = [MODELSCOPE_KEY(), ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        call_log = []

        def _mock_call_chat(rk, messages, timeout=None):
            call_log.append(rk.key_id)
            raise Exception("429 Too Many Requests - quota exceeded")

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        # ice 的 last_call_finished_ts 设为 120s 前，触发非并发判定
        vl_client._get_runtime_state("ice").last_call_finished_ts = time.time() - 120

        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        assert result["status"] == "error"
        assert result.get("daily_exhausted") is True
        # modelscope 已耗尽被跳过，只有 ice 被调（并 429）
        assert "ms" not in call_log
        assert "ice" in call_log


# ==================== 503 持续失败标记 provider exhausted ====================

class TestServerErrorExhausted:
    """503/502/500/504 持续失败达阈值后标记 provider exhausted

    场景：ice（~80% 可用率）503 持续失败时，不应无限短冷却循环，
    应标记 provider exhausted 让聚合检查触发 daily_exhausted → loop_actions 走 OCR 兜底。
    """

    def test_503_below_threshold_marks_cooldown_not_exhausted(self, vl_client, monkeypatch):
        """503 连续 < max_consecutive 次 → 只 mark_cooldown，不标记 exhausted"""
        candidates = [ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        call_count = [0]
        def _mock_call_chat(rk, messages, timeout=None):
            call_count[0] += 1
            if call_count[0] <= 2:  # 前 2 次 503
                raise Exception("HTTP Error 503: Service Unavailable")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        # 第 3 次成功 → 返回 ok
        assert result["status"] == "ok"
        # 503 两次未达阈值（3），不标记 exhausted
        assert "ice" not in vl_client._provider_exhausted
        # 但 consecutive_server_errors 应在第 3 次成功时清零
        state = vl_client._get_runtime_state("ice")
        assert state.consecutive_server_errors == 0

    def test_503_consecutive_marks_provider_exhausted(self, vl_client, monkeypatch):
        """503 连续 >= max_consecutive 次 → 标记 provider exhausted"""
        candidates = [MODELSCOPE_KEY(), ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates
        # 降低阈值让 2 次 503 就标记（默认 max_attempts=3 不够 3 次 503）
        vl_client._provider_exhausted_max_consecutive = 2

        def _mock_call_chat(rk, messages, timeout=None):
            if rk.key_id == "ms":
                raise Exception("429 Too Many Requests - quota exceeded")
            raise Exception("HTTP Error 503: Service Unavailable")

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        # modelscope 非并发 429
        vl_client._get_runtime_state("ms").last_call_finished_ts = time.time() - 120

        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        # 所有 provider 都耗尽 → daily_exhausted
        assert result["status"] == "error"
        assert result.get("daily_exhausted") is True
        # modelscope 被 429 标记
        assert "ms" in vl_client._provider_exhausted
        # ice 被 503 连续 2 次标记
        assert "ice" in vl_client._provider_exhausted
        assert vl_client._provider_exhausted["ice"]["kind"] == "upstream_overload"

    def test_503_success_resets_consecutive_count(self, vl_client, monkeypatch):
        """503 后成功 → consecutive_server_errors 清零"""
        candidates = [ICE_KEY()]
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: candidates)
        vl_client._all_vl_candidates = lambda: candidates

        call_count = [0]
        def _mock_call_chat(rk, messages, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("HTTP Error 503: Service Unavailable")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(vl_client, "_call_chat", _mock_call_chat)
        img = Image.new("RGB", (10, 10), "white")
        result = vl_client._call_with_failover(img, "test", use_case="vl_ocr")

        assert result["status"] == "ok"
        state = vl_client._get_runtime_state("ice")
        # 1 次 503 → consecutive=1，然后成功 → 清零
        assert state.consecutive_server_errors == 0


# ==================== Ticket 03: 跨日重置 + /health ====================

class TestCrossDayReset:
    """V4: 跨日清空 _provider_exhausted"""

    def test_cross_day_clears_provider_exhausted(self, vl_client):
        """跨日时 _provider_exhausted dict 清空"""
        vl_client._mark_provider_exhausted("ms", "r1", kind="quota_exhausted", source="modelscope")
        vl_client._mark_provider_exhausted("ice", "r1", kind="quota_exhausted", source="ice")
        assert len(vl_client._provider_exhausted) == 2
        # 模拟跨日
        vl_client._daily_exhausted = True
        vl_client._daily_exhausted_date = "20200101"
        vl_client._provider_exhausted["ms"]["date"] = "20200101"
        vl_client._provider_exhausted["ice"]["date"] = "20200101"
        vl_client._check_daily_exhausted_reset()
        assert vl_client._provider_exhausted == {}
        assert vl_client._daily_exhausted is False

    def test_same_day_no_reset(self, vl_client):
        """同日不重置"""
        vl_client._mark_provider_exhausted("ms", "r1", kind="quota_exhausted", source="modelscope")
        vl_client._check_daily_exhausted_reset()
        assert "ms" in vl_client._provider_exhausted


class TestHealthStatus:
    """V5: status() 返回 provider_exhausted dict"""

    def test_status_includes_provider_exhausted_field(self, vl_client, monkeypatch):
        """status() 包含 provider_exhausted 字段"""
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: [])
        vl_client._all_vl_candidates = lambda: []
        status = vl_client.status()
        assert "provider_exhausted" in status
        assert isinstance(status["provider_exhausted"], dict)

    def test_status_shows_exhausted_provider(self, vl_client, monkeypatch):
        """status() 展示已耗尽 provider 的详细信息"""
        vl_client._mark_provider_exhausted("ms", "quota exceeded", kind="quota_exhausted", source="modelscope")
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: [])
        vl_client._all_vl_candidates = lambda: []
        status = vl_client.status()
        pex = status["provider_exhausted"]
        assert "ms" in pex
        assert pex["ms"]["consecutive_count"] == 1
        assert pex["ms"]["kind"] == "quota_exhausted"
        assert pex["ms"]["source"] == "modelscope"
        assert "cooldown_until" in pex["ms"]
        assert "cooldown_remaining" in pex["ms"]
        assert "reason" in pex["ms"]

    def test_status_preserves_global_daily_exhausted(self, vl_client, monkeypatch):
        """status() 保留全局 daily_exhausted 字段（向后兼容）"""
        monkeypatch.setattr("server.llm_pool.key_store.resolve_keys", lambda uc: [])
        vl_client._all_vl_candidates = lambda: []
        vl_client._daily_exhausted = True
        vl_client._daily_exhausted_at = "2026-08-01T10:00:00"
        vl_client._daily_exhausted_reason = "all exhausted"
        status = vl_client.status()
        assert status["daily_exhausted"] is True
        assert status["daily_exhausted_at"] == "2026-08-01T10:00:00"
        assert status["daily_exhausted_reason"] == "all exhausted"
