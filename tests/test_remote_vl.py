"""remote_vl v9 测试

v9 重构后覆盖：
- ProviderRuntimeState: cooldown/active_count 属性
- RemoteVLClient: reload_config（v9 不再加载 provider 列表）、enabled/available
- vl_status_fields: 字段提取

v9 前（RemoteVLProvider / _load_vl_keys_from_unified / vl_default_provider / vl_providers config）
已全部移除，旧测试同步删除。新测试聚焦不依赖 keys.json 的核心组件；
依赖 resolve_keys 的 _pick_provider / _call_with_failover 留待集成测试覆盖。
"""

import time

import pytest

from server.vl.remote_vl import (
    ProviderRuntimeState,
    RemoteVLClient,
)

# ==================== ProviderRuntimeState ====================

class TestProviderRuntimeState:
    def test_default_state_not_in_cooldown(self):
        """新建状态：不在冷却中"""
        s = ProviderRuntimeState(key_id="k1")
        assert s.in_cooldown is False
        assert s.cooldown_remaining == 0.0

    def test_in_cooldown_when_future_timestamp(self):
        """cooldown_until 在未来 → in_cooldown=True"""
        s = ProviderRuntimeState(key_id="k1")
        s.cooldown_until = time.time() + 60
        assert s.in_cooldown is True

    def test_cooldown_remaining_returns_seconds(self):
        """cooldown_remaining 返回剩余秒数"""
        s = ProviderRuntimeState(key_id="k1")
        s.cooldown_until = time.time() + 30
        remaining = s.cooldown_remaining
        assert 25 < remaining <= 30

    def test_cooldown_remaining_zero_when_expired(self):
        """冷却已过 → cooldown_remaining=0"""
        s = ProviderRuntimeState(key_id="k1")
        s.cooldown_until = time.time() - 10
        assert s.cooldown_remaining == 0.0

    def test_active_count_default_zero(self):
        """active_count 默认 0"""
        s = ProviderRuntimeState(key_id="k1")
        assert s.active_count == 0
        assert s.last_error == ""
        assert s.last_success == 0.0


# ==================== RemoteVLClient: config loading ====================

@pytest.fixture
def vl_client(monkeypatch, tmp_path):
    """创建一个配置好的 RemoteVLClient，不依赖真实 config.toml

    关键：注入 tmp_path 作为 429 日志目录 + 测试用 VLQuotaManager 实例，
    避免测试触发的 429/daily_exhausted 写入生产文件污染数据。
    """
    client = RemoteVLClient()
    # 隔离 429 日志：写到 tmp_path 而非项目根 data/activity/
    client._log_dir = tmp_path
    # 隔离 vl_quota：测试用独立实例，quota_dir 也指向 tmp_path
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
            "vl_retry_backoffs": (2, 4, 8, 16, 32, 60),
        },
    )
    # 固定 privacy 配置，避免读取真实 config.toml 干扰 sensitive 过滤
    monkeypatch.setattr(
        "server.config.get_privacy_config",
        lambda: {"allow_privacy_warning_for_sensitive": False},
    )
    return client


class TestRemoteVLClientConfig:
    def test_reload_config_loads_global_params(self, vl_client):
        """reload_config 加载全局编码参数（v9 不再加载 provider 列表）"""
        vl_client.reload_config()
        assert vl_client._enabled is True
        assert vl_client._max_image_edge == 1280
        assert vl_client._jpeg_quality == 85
        assert vl_client._retry_backoffs == (2, 4, 8, 16, 32, 60)

    def test_reload_config_disabled(self, monkeypatch):
        """vl_enabled=False → _enabled=False"""
        client = RemoteVLClient()
        monkeypatch.setattr(
            "server.config.get_vision_config",
            lambda: {
                "vl_enabled": False,
                "vl_max_image_edge": 1280,
                "vl_jpeg_quality": 85,
                "vl_retry_backoffs": (2, 4, 8, 16, 32, 60),
            },
        )
        client.reload_config()
        assert client._enabled is False

    def test_enabled_property(self, vl_client):
        """enabled 属性反映 vl_enabled"""
        assert vl_client.enabled is True

    def test_running_alias(self, vl_client):
        """running 是 enabled 的别名（v9 兼容）"""
        assert vl_client.running is True


# ==================== RemoteVLClient: runtime state ====================

class TestRemoteVLClientRuntimeState:
    def test_get_runtime_state_creates_on_demand(self, vl_client):
        """_get_runtime_state 首次调用创建状态"""
        vl_client.reload_config()
        state = vl_client._get_runtime_state("new_key_id")
        assert state.key_id == "new_key_id"
        assert state.active_count == 0

    def test_get_runtime_state_returns_same_instance(self, vl_client):
        """_get_runtime_state 同 key_id 返回同一实例"""
        vl_client.reload_config()
        s1 = vl_client._get_runtime_state("k1")
        s2 = vl_client._get_runtime_state("k1")
        assert s1 is s2

    def test_get_per_key_lock_creates_on_demand(self, vl_client):
        """_get_per_key_lock 首次调用创建 lock"""
        vl_client.reload_config()
        lock = vl_client._get_per_key_lock("k1")
        assert lock is not None
        # 同 key_id 返回同一 lock
        assert vl_client._get_per_key_lock("k1") is lock

    def test_mark_cooldown_sets_state(self, vl_client):
        """_mark_cooldown 设置 cooldown_until 和 last_error"""
        vl_client.reload_config()
        state = vl_client._get_runtime_state("k1")
        vl_client._mark_cooldown(state, 60.0, "test error")
        assert state.in_cooldown is True
        assert state.last_error == "test error"

    def test_mark_success_clears_error(self, vl_client):
        """_mark_success 清空 last_error 并设置 last_success"""
        vl_client.reload_config()
        state = vl_client._get_runtime_state("k1")
        state.last_error = "old error"
        vl_client._mark_success(state)
        assert state.last_error == ""
        assert state.last_success > 0


# ==================== RemoteVLClient: daily exhausted ====================

class TestRemoteVLClientDailyExhausted:
    def test_not_exhausted_by_default(self, vl_client):
        """默认未耗尽"""
        vl_client.reload_config()
        assert vl_client.is_daily_exhausted() is False

    def test_mark_daily_exhausted(self, vl_client, monkeypatch):
        """_mark_daily_exhausted 设置标志 + 同步给 _vl_quota_override"""
        vl_client.reload_config()
        # vl_client fixture 已注入 _vl_quota_override（测试用 VLQuotaManager 实例）
        # 验证 _mark_daily_exhausted 会调用它的 mark_daily_exhausted
        # 新签名 mark_daily_exhausted(reason, kind=, source=)，记录所有参数便于断言
        vl_quota_calls = []

        def _capture(reason, kind="upstream_overload", source=None):
            vl_quota_calls.append({"reason": reason, "kind": kind, "source": source})

        monkeypatch.setattr(
            vl_client._vl_quota_override,
            "mark_daily_exhausted",
            _capture,
        )
        vl_client._mark_daily_exhausted("test reason")
        assert vl_client.is_daily_exhausted() is True
        assert vl_quota_calls == [{
            "reason": "test reason",
            "kind": "upstream_overload",  # 默认 kind
            "source": None,
        }]

    def test_daily_exhausted_resets_across_day(self, vl_client, monkeypatch):
        """跨日时 daily_exhausted 自动重置"""
        vl_client.reload_config()
        vl_client._daily_exhausted = True
        vl_client._daily_exhausted_date = "20200101"  # 很早的日期
        vl_client._daily_exhausted_reason = "old reason"
        # 触发重置检查
        vl_client._check_daily_exhausted_reset()
        assert vl_client._daily_exhausted is False
        assert vl_client._daily_exhausted_date == ""
