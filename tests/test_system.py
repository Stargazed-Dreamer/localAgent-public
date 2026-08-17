"""系统控制模块测试 — KeepAwakeManager + REST 端点

KeepAwakeManager 通过 Windows SetThreadExecutionState 阻止系统休眠。
测试 mock ctypes 调用，不依赖真实平台 API。
"""

import sys
import time
from unittest.mock import MagicMock

import pytest

from server.system import KeepAwakeManager, keep_awake


@pytest.fixture
def fresh_manager(monkeypatch):
    """每个测试用独立的 KeepAwakeManager，mock 为 Windows 平台"""
    mgr = KeepAwakeManager()
    # 强制视为 Windows 平台
    monkeypatch.setattr(mgr, "_supported", True)
    # mock _apply 避免真实调用 SetThreadExecutionState
    monkeypatch.setattr(mgr, "_apply", lambda: True)
    return mgr


# ========== KeepAwakeManager ==========

class TestKeepAwakeManager:
    def test_initial_state(self, fresh_manager):
        """初始状态：未启用"""
        m = fresh_manager
        assert m.enabled is False
        assert m.keep_display_on is False
        assert m.reason is None
        assert m.enabled_at is None
        assert m.last_error is None

    def test_enable_system_only(self, fresh_manager):
        """启用仅系统防休眠"""
        m = fresh_manager
        ok = m.enable(keep_display_on=False, reason="爬虫运行中")
        assert ok is True
        assert m.enabled is True
        assert m.keep_display_on is False
        assert m.reason == "爬虫运行中"
        assert m.enabled_at is not None

    def test_enable_with_display(self, fresh_manager):
        """启用系统+显示器防休眠"""
        m = fresh_manager
        ok = m.enable(keep_display_on=True, reason="训练任务")
        assert ok is True
        assert m.enabled is True
        assert m.keep_display_on is True
        assert m.reason == "训练任务"

    def test_enable_no_reason(self, fresh_manager):
        """启用不带原因"""
        m = fresh_manager
        m.enable()
        assert m.reason is None
        assert m.enabled is True

    def test_disable(self, fresh_manager):
        """关闭防休眠"""
        m = fresh_manager
        m.enable(reason="test")
        ok = m.disable()
        assert ok is True
        assert m.enabled is False
        assert m.keep_display_on is False
        assert m.reason is None
        assert m.enabled_at is None

    def test_disable_when_not_enabled(self, fresh_manager):
        """未启用时关闭也返回 True"""
        m = fresh_manager
        ok = m.disable()
        assert ok is True

    def test_status_dict(self, fresh_manager):
        """status 返回完整状态"""
        m = fresh_manager
        m.enable(keep_display_on=True, reason="测试")
        s = m.status()
        assert s["supported"] is True
        assert s["enabled"] is True
        assert s["keep_display_on"] is True
        assert s["reason"] == "测试"
        assert s["enabled_at"] is not None
        assert s["enabled_duration_seconds"] is not None
        assert s["enabled_duration_seconds"] >= 0

    def test_status_disabled_duration_none(self, fresh_manager):
        """未启用时 duration 为 None"""
        m = fresh_manager
        s = m.status()
        assert s["enabled"] is False
        assert s["enabled_duration_seconds"] is None

    def test_apply_failure_sets_error(self, monkeypatch):
        """_apply 失败时记录 last_error"""
        m = KeepAwakeManager()
        monkeypatch.setattr(m, "_supported", True)
        # mock SetThreadExecutionState 返回 0（失败）
        mock_windll = MagicMock()
        mock_windll.kernel32.SetThreadExecutionState.return_value = 0
        monkeypatch.setattr(ctypes_mod(), "windll", mock_windll)

        ok = m.enable()
        assert ok is False
        assert m.last_error is not None

    def test_unsupported_platform(self, monkeypatch):
        """非 Windows 平台"""
        m = KeepAwakeManager()
        monkeypatch.setattr(m, "_supported", False)
        ok = m.enable()
        assert ok is False
        assert "不支持" in m.last_error

    def test_duration_increases(self, fresh_manager):
        """启用时长随时间增长"""
        m = fresh_manager
        m.enable()
        d1 = m.status()["enabled_duration_seconds"]
        time.sleep(0.1)
        d2 = m.status()["enabled_duration_seconds"]
        assert d2 >= d1


def ctypes_mod():
    """获取 ctypes 模块（用于 monkeypatch）"""
    import ctypes
    return ctypes


# ========== 全局单例 ==========

class TestGlobalInstance:
    def test_global_keep_awake_exists(self):
        """全局 keep_awake 实例存在"""
        assert isinstance(keep_awake, KeepAwakeManager)

    def test_global_supported_matches_platform(self):
        """全局实例 supported 匹配当前平台"""
        assert keep_awake.supported == (sys.platform == "win32")


# ========== REST 端点 ==========

class TestSystemREST:
    def test_system_status(self, client):
        """GET /system/status"""
        resp = client.get("/system/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "keep_awake_supported" in data
        assert "keep_awake_enabled" in data
        assert "keep_awake_keep_display_on" in data

    def test_keep_awake_status(self, client):
        """GET /system/keep-awake"""
        resp = client.get("/system/keep-awake")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data
        assert "enabled" in data
        assert "message" in data

    def test_set_keep_awake_enable(self, client, monkeypatch):
        """POST /system/keep-awake enable=true"""
        # mock 全局单例避免真实调用
        monkeypatch.setattr(keep_awake, "_supported", True)
        monkeypatch.setattr(keep_awake, "_apply", lambda: True)

        resp = client.post("/system/keep-awake", json={
            "enable": True,
            "keep_display_on": False,
            "reason": "测试启用",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["enabled"] is True
        assert data["reason"] == "测试启用"

    def test_set_keep_awake_disable(self, client, monkeypatch):
        """POST /system/keep-awake enable=false"""
        monkeypatch.setattr(keep_awake, "_supported", True)
        monkeypatch.setattr(keep_awake, "_apply", lambda: True)
        keep_awake.enable(reason="先启用")

        resp = client.post("/system/keep-awake", json={"enable": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["enabled"] is False

    def test_set_keep_awake_with_display(self, client, monkeypatch):
        """POST /system/keep-awake keep_display_on=true"""
        monkeypatch.setattr(keep_awake, "_supported", True)
        monkeypatch.setattr(keep_awake, "_apply", lambda: True)

        resp = client.post("/system/keep-awake", json={
            "enable": True, "keep_display_on": True, "reason": "显示器不休",
        })
        assert resp.status_code == 200
        assert resp.json()["keep_display_on"] is True

    def test_set_keep_awake_unsupported_400(self, client, monkeypatch):
        """不支持的平台返回 400"""
        monkeypatch.setattr(keep_awake, "_supported", False)

        resp = client.post("/system/keep-awake", json={"enable": True})
        assert resp.status_code == 400

    @pytest.fixture(autouse=True)
    def cleanup_keep_awake(self, monkeypatch):
        """每个测试后恢复全局 keep_awake 状态"""
        orig_enabled = keep_awake.enabled
        orig_display = keep_awake.keep_display_on
        orig_reason = keep_awake.reason
        yield
        # 恢复
        keep_awake._enabled = orig_enabled
        keep_awake._keep_display_on = orig_display
        keep_awake._reason = orig_reason
        if not orig_enabled:
            keep_awake._enabled_at = None
