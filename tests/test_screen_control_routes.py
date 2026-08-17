"""/screen/control/request|release 端点流程测试（原 test_takeover_persistent.py）

测试 SessionManager 状态机、/screen/control/request|release 端点、
_ensure_takeover_approved 短路、idle 两阶段降级、task_closure 自动 release。

主 seam：REST API 层（/screen/control/request + /screen/control/release）。
OverlayClient 用 conftest 的 mock_overlay_client fixture（SessionManager 状态机为真实逻辑）。

T26 改写要点（相对旧版）：
- 模块级 import server.gui_process → server.overlay_client
- 旧 API（is_persistent_mode/set_persistent_mode/release_task_control/
  touch_input_time/get_last_input_time/get_persistent_source）→ SessionManager API
  （is_active/grant/release/extend/status()["last_activity_at"]/status()["source"]）
- ControlGrantManager 直接测试 → SessionManager（保留 idle_warning/grace 两阶段语义）
- /health 断言保留 takeover_persistent 兼容字段（enabled/source/overlay_visible）
"""

import time

import pytest

import server.overlay_client
import server.user_message as um
from server.screen.session import Mode as SessionMode
from server.screen.session import get_session_manager
from server.screen.session.manager import reset_session_manager


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试前清空 user_message + 重置 SessionManager + overlay 状态。

    conftest.mock_overlay_client 已默认 grant normal 授权（让副作用端点测试通过权限检查），
    本测试组需要从"无授权"初始状态开始，所以 reset_session_manager() 撤销默认授权。
    """
    um.clear_all()
    reset_session_manager()
    server.overlay_client.overlay_client.hide_overlay()
    yield
    um.clear_all()
    reset_session_manager()
    server.overlay_client.overlay_client.hide_overlay()


# ========== T1: request/release 状态流转 ==========


class TestRequestRelease:
    """持久授权 request/release 端点状态流转（T26：基于 SessionManager）"""

    def test_request_then_release(self, client):
        """request 开启 normal 模式 → release 释放"""
        session = get_session_manager()
        overlay = server.overlay_client.overlay_client
        # 初始状态：未授权
        assert session.is_active() is False
        assert session.status()["source"] == ""

        # 1. request 开启 normal 模式
        resp = client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "整理下载文件夹",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["persistent_mode"] is True  # 兼容字段 = active
        assert data["source"] == "agent"
        assert data["mode"] == "normal"
        assert data["status"] == "authorized"
        # SessionManager 状态校验
        assert session.is_active() is True
        assert session.status()["mode"] == "normal"
        assert session.status()["source"] == "agent"
        # overlay 渲染校验
        assert overlay.overlay_visible is True
        assert "普通操作免确认" in overlay.last_overlay_message
        assert "危险操作仍需确认" in overlay.last_overlay_message

        # 2. release 释放
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["persistent_mode"] is False
        assert data["status"] == "released"
        assert session.is_active() is False
        assert session.status()["source"] == ""
        assert overlay.overlay_visible is False

    def test_release_idempotent(self, client):
        """未授权时 release 幂等不报错"""
        session = get_session_manager()
        assert session.is_active() is False
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["persistent_mode"] is False
        assert "幂等" in data["message"]

    def test_request_idempotent(self, client):
        """重复 request 幂等不报错（session 已 active 时短路）"""
        session = get_session_manager()
        client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "第一次",
        })
        um.clear_all()
        resp = client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "第二次",
        })
        assert resp.status_code == 200
        assert resp.json()["persistent_mode"] is True
        assert session.is_active() is True

    def test_request_with_task_description(self, client):
        """request 带 task_description 也能开启"""
        resp = client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "整理下载文件夹",
        })
        assert resp.status_code == 200
        assert resp.json()["persistent_mode"] is True


# ========== T2: _ensure_takeover_approved 短路 + 成功投递后更新时间 ==========


class TestPersistentShortCircuit:
    """当前任务授权跳过 takeover prompt，但 gate 本身不算控制活动。"""

    def test_persistent_mode_skips_confirm(self, client, monkeypatch):
        """active 状态下 _ensure_takeover_approved 直接返回 True 不弹窗"""
        session = get_session_manager()
        overlay = server.overlay_client.overlay_client
        session.grant(mode=SessionMode.NORMAL, task_description="test", source="agent")

        # 监视 overlay.ensure_takeover_approved 是否被调用
        called = {"ensure": False}
        orig_ensure = overlay.ensure_takeover_approved

        def spy(*args, **kwargs):
            called["ensure"] = True
            return orig_ensure(*args, **kwargs)

        monkeypatch.setattr(overlay, "ensure_takeover_approved", spy)

        # 用真实 _ensure_takeover_approved（conftest 已保存为 _orig 后缀）
        from server.screen.routes import _ensure_takeover_approved_orig
        approved, reason = _ensure_takeover_approved_orig(
            {"takeover_confirm_enabled": True}, "test task"
        )
        assert approved is True
        assert called["ensure"] is False  # active 状态下不弹窗

    def test_gate_does_not_reset_idle_timer(self, client):
        """授权 gate 不刷新计时器，只有成功投递的控制操作才刷新（extend）。"""
        session = get_session_manager()
        session.grant(mode=SessionMode.NORMAL, task_description="test", source="agent")
        t0 = session.status()["last_activity_at"]
        assert t0 > 0
        time.sleep(0.01)

        from server.screen.routes import _ensure_takeover_approved_orig
        approved, _ = _ensure_takeover_approved_orig(
            {"takeover_confirm_enabled": True}, "test task"
        )
        assert approved is True
        t1 = session.status()["last_activity_at"]
        assert t1 == t0  # gate 不刷新 idle

        time.sleep(0.01)
        assert session.extend() is True
        assert session.status()["last_activity_at"] > t1

    def test_non_input_does_not_reset(self, client):
        """非键鼠端点（如 /health）不调 extend，不更新 last_activity_at"""
        session = get_session_manager()
        session.grant(mode=SessionMode.NORMAL, task_description="test", source="agent")
        t0 = session.status()["last_activity_at"]
        time.sleep(0.01)

        # /health 不调用 _ensure_takeover_approved 也不调 extend
        resp = client.get("/health")
        assert resp.status_code == 200
        t1 = session.status()["last_activity_at"]
        assert t1 == t0  # 计时器未变

    def test_invalid_timeout_action_fails_closed(self, client, monkeypatch):
        """takeover_timeout_action 非法时 fail-closed（cancel）"""
        session = get_session_manager()
        overlay = server.overlay_client.overlay_client
        session.release("test")
        overlay.overlay_visible = False
        captured = {}

        def capture_timeout(*args, **kwargs):
            captured["timeout_action"] = kwargs["timeout_action"]
            return {
                "status": "cancelled",
                "reason": "timeout_auto_cancel",
                "task_authorization": False,
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", capture_timeout)
        from server.screen.routes import _ensure_takeover_approved_orig

        approved, _ = _ensure_takeover_approved_orig(
            {
                "takeover_confirm_enabled": True,
                "takeover_timeout_action": "invalid",
            },
            "test task",
        )

        assert approved is False
        assert captured["timeout_action"] == "cancel"


# ========== T3: 两阶段 idle 降级（normal 模式） ==========


class TestIdleWarning:
    """normal 模式两阶段 idle 降级（warning 阶段不撤销，grace 结束撤销）。

    T26 改写：原 TestIdleWarning 用 ControlGrantManager（单阶段 300s 撤销），
    现改用 SessionManager（两阶段：idle_warning_seconds 触发 warning 事件但不撤销，
    idle_total_seconds 到期撤销）。
    """

    def test_idle_grace_revokes_authorization(self, client):
        """idle 超过 total（warning + grace）时撤销授权"""
        # 用极短 warning + grace 测试两阶段（idle_total = warning + grace = 2s）
        now = [100.0]
        # 直接构造 SessionManager 实例，绕过单例，用自定义 clock + policy
        from server.screen.session.manager import SessionManager
        from server.screen.session.policy import TimePolicy

        policy = TimePolicy(
            idle_warning_seconds=1,
            idle_grace_seconds=1,
            watchdog_default_hours=10,
            watchdog_min_hours=1,
            watchdog_max_hours=999,
            check_interval_seconds=5.0,
        )
        mgr = SessionManager(clock=lambda: now[0], start_worker=False)
        mgr._policy = policy  # 测试专用：替换 policy
        mgr.grant(mode=SessionMode.NORMAL, task_description="测试", source="agent")
        # warning 阶段（1 秒）：不撤销
        now[0] = 101.5
        assert mgr._check_expiry() is False
        assert mgr.status()["active"] is True
        assert mgr.status()["phase"] == "warning"
        # grace 结束（2 秒）：撤销
        now[0] = 102.5
        assert mgr._check_expiry() is True
        assert mgr.status()["active"] is False

    def test_idle_not_triggered_under_warning_threshold(self, client):
        """idle 未达 warning 阈值时不撤销，phase 仍为 normal"""
        from server.screen.session.manager import SessionManager
        from server.screen.session.policy import TimePolicy

        now = [100.0]
        policy = TimePolicy(
            idle_warning_seconds=10,
            idle_grace_seconds=20,
            watchdog_default_hours=10,
            watchdog_min_hours=1,
            watchdog_max_hours=999,
            check_interval_seconds=5.0,
        )
        mgr = SessionManager(clock=lambda: now[0], start_worker=False)
        mgr._policy = policy
        mgr.grant(mode=SessionMode.NORMAL, task_description="测试", source="agent")
        now[0] = 105.0  # 5 秒，未到 warning(10s)
        assert mgr._check_expiry() is False
        assert mgr.status()["active"] is True
        assert mgr.status()["phase"] == "normal"

    def test_idle_not_triggered_without_authorization(self, client):
        """无授权时 _check_expiry 返回 False"""
        from server.screen.session.manager import SessionManager

        mgr = SessionManager(start_worker=False)
        assert mgr._check_expiry() is False

    def test_idle_is_idempotent_after_revocation(self, client):
        """撤销后再次 expire_if_idle 幂等返回 False"""
        from server.screen.session.manager import SessionManager
        from server.screen.session.policy import TimePolicy

        now = [100.0]
        policy = TimePolicy(
            idle_warning_seconds=1,
            idle_grace_seconds=1,
            watchdog_default_hours=10,
            watchdog_min_hours=1,
            watchdog_max_hours=999,
            check_interval_seconds=5.0,
        )
        mgr = SessionManager(clock=lambda: now[0], start_worker=False)
        mgr._policy = policy
        mgr.grant(mode=SessionMode.NORMAL, task_description="测试", source="agent")
        now[0] = 102.5
        assert mgr._check_expiry() is True
        assert mgr._check_expiry() is False  # 幂等


# ========== T4: task_closure 自动 release 集成 ==========


class TestTaskClosureAutoRelease:
    """task_closure step_1 兜底释放持久授权

    task_closure skill 文档和 agent_guide 的 task_closure_workflow.step_1_assess
    已集成"持久授权兜底释放"指令：任务结束（完成/中断）时若 session active，
    必须调 screen_release_control。本测试验证该兜底行为。
    """

    def test_task_closure_auto_release(self, client):
        """task_closure step_1 兜底释放持久授权"""
        session = get_session_manager()
        # 模拟 agent 在任务中开启了持久授权
        client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "测试任务",
        })
        assert session.is_active() is True
        um.clear_all()

        # 模拟 task_closure step_1：检查 is_active，若是则调 release
        if session.is_active():
            resp = client.post("/screen/control/release", json={})
            assert resp.status_code == 200

        # 断言兜底释放成功
        assert session.is_active() is False
        assert server.overlay_client.overlay_client.overlay_visible is False
        assert session.status()["source"] == ""

    def test_task_closure_no_release_when_not_active(self, client):
        """未授权时 task_closure 不需要调 release"""
        session = get_session_manager()
        assert session.is_active() is False
        # task_closure step_1 检查：未授权则跳过 release
        assert session.is_active() is False


# ========== T5: release 恢复 + /health 状态反映 ==========


class TestReleaseRestoresState:
    """release 后 session 清除，/health.takeover_persistent 反映真实状态。"""

    def test_release_restores_state(self, client):
        """release 后 session 清除，下次 show_overlay 不进入 active 状态"""
        session = get_session_manager()
        overlay = server.overlay_client.overlay_client
        # 开启 normal 模式
        client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "测试",
        })
        assert session.is_active() is True
        assert overlay.overlay_visible is True

        # release
        resp = client.post("/screen/control/release", json={})
        assert resp.status_code == 200
        assert session.is_active() is False
        assert overlay.overlay_visible is False
        assert session.status()["source"] == ""

        # 再次 show_overlay —— 模拟 agent 下次普通键鼠操作
        # show_overlay 不再自动 grant；session 仍为 inactive
        overlay.show_overlay(message="临时操作", persistent=False)
        assert overlay.overlay_visible is True
        assert session.is_active() is False  # 不进入 active 状态

    def test_health_reports_persistent_status(self, client):
        """/health 的 screen.takeover_persistent 反映真实状态"""
        from server.core import health as health_mod

        # 初始：未授权
        health_mod._status_cache["data"] = None
        resp = client.get("/health")
        assert resp.status_code == 200
        persistent = resp.json()["screen"]["takeover_persistent"]
        assert persistent["enabled"] is False

        # 开启 normal 模式
        client.post("/screen/control/request", json={
            "mode": "normal",
            "task_description": "测试",
        })
        health_mod._status_cache["data"] = None  # 清缓存
        resp = client.get("/health")
        persistent = resp.json()["screen"]["takeover_persistent"]
        assert persistent["enabled"] is True
        assert persistent["source"] == "agent"
        assert persistent["overlay_visible"] is True
        assert persistent["mode"] == "normal"

        # release
        client.post("/screen/control/release", json={})
        health_mod._status_cache["data"] = None  # 清缓存
        resp = client.get("/health")
        persistent = resp.json()["screen"]["takeover_persistent"]
        assert persistent["enabled"] is False
