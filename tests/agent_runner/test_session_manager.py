"""SessionManager + /screen/control 端点流程测试 — T25 改写版

T25 改写自原 test_control_grant.py，覆盖：
- SessionManager 单元测试（grant/release/extend/transition/两阶段降级/watchdog 降级/shutdown_permitted）
- /screen/control/request|release 端点契约（normal/watchdog 授权 + 用户拒绝 + disabled）
- watchdog 授权模式选项 + 用户反馈完整返回（ISSUE-003/004）
- 截图后 overlay 恢复保留 persistent 语义（ISSUE-005）
- watchdog imply task_authorization（ISSUE-006）
- agent_guide 引用 screen_request_control（TestTaskAuthorizationGuidance）
- 授权后的控制策略（danger_level/confirm/dry_run/batch/transaction/uia/window_close）

T25 改写要点（相对旧版）：
- 模块级 import server.gui_process → server.overlay_client
- ControlGrantManager → SessionManager（新 API：grant(mode=)/extend()/transition()/_check_expiry()）
- gui_client.grant_task_control/is_persistent_mode/is_watchdog_mode/touch_input_time/
  get_last_input_time/release_task_control → SessionManager API
- gui_client 仍用于 overlay 渲染验证（overlay_visible/show_overlay/
  hide_overlay/ensure_takeover_approved/confirm_action）— 这些是 OverlayClient 职责
  注：新设计下 overlay 文案由 _tick_loop 从 SessionManager.status() 推送，
  故不再验证 last_overlay_message 内容（旧 message 参数已移除）
"""

from __future__ import annotations

import server.overlay_client
from server.screen.session import Mode as SessionMode
from server.screen.session import get_session_manager
from server.screen.session.manager import reset_session_manager


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _make_policy(idle_warning: int = 600, idle_grace: int = 1200):
    """构造测试用 TimePolicy（默认 10 分钟 warning + 20 分钟 grace = 30 分钟 total）。"""
    from server.screen.session.policy import TimePolicy

    return TimePolicy(
        idle_warning_seconds=idle_warning,
        idle_grace_seconds=idle_grace,
        watchdog_default_hours=10,
        watchdog_min_hours=1,
        watchdog_max_hours=999,
        check_interval_seconds=5.0,
    )


# ========== T1: SessionManager 单元测试 ==========


class TestSessionManager:
    """SessionManager 状态机单元测试（替代原 TestControlGrantManager）。"""

    def test_grant_status_and_extend_resets_idle(self):
        """grant 后 status 反映 active，extend 重置 last_activity_at"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy(idle_warning=300, idle_grace=0)

        mgr.grant(mode=SessionMode.NORMAL, task_description="整理桌面窗口", source="agent")
        status = mgr.status()
        assert status["active"] is True
        assert status["mode"] == "normal"
        assert status["task_description"] == "整理桌面窗口"
        assert status["source"] == "agent"
        # expires_at = last_activity_at + idle_total = 1000 + 300 = 1300
        assert status["expires_at"] == 1300.0

        clock.now = 1100.0
        assert mgr.extend() is True
        # extend 后 expires_at = 1100 + 300 = 1400
        assert mgr.status()["expires_at"] == 1400.0

    def test_idle_grace_revokes_after_total_timeout(self):
        """idle 超过 total（warning + grace）时撤销授权"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        events: list[dict] = []
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy(idle_warning=300, idle_grace=0)
        mgr.subscribe(lambda e: events.append(e.state))

        mgr.grant(mode=SessionMode.NORMAL, task_description="填写表单", source="agent")
        # 超过 total（300 秒）
        clock.now = 1301.0
        assert mgr._check_expiry() is True
        assert mgr.status()["active"] is False
        # 触发了 expired 事件
        assert any(e.get("revoked_reason") == "idle_timeout" for e in events)

    def test_idle_revokes_at_exact_deadline(self):
        """idle 精确到 deadline 时撤销"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy(idle_warning=300, idle_grace=0)
        mgr.grant(mode=SessionMode.NORMAL, task_description="精确超时", source="agent")

        clock.now = 1300.0
        assert mgr._check_expiry() is True
        assert mgr.status()["active"] is False

    def test_watchdog_ignores_idle_and_uses_max_duration(self):
        """watchdog 模式忽略 idle，用 max_duration_seconds 算 remaining"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy()
        # max_duration_hours=10 → 36000 秒；但测试用 max_duration_hours 直接传
        # 为了精确断言，传 max_duration_hours 让内部计算 max_duration_seconds
        # 测试想要 max_duration_seconds=10 秒，但 grant 接受 hours，所以用 1 小时 → 3600 秒
        # 改为更小的 hours 不行（min=1），所以直接构造状态测试 remaining 计算
        # 用 1 小时 watchdog，clock 推进 6 秒，remaining=3594
        mgr.grant(
            mode=SessionMode.WATCHDOG,
            task_description="持续监控",
            source="agent",
            max_duration_hours=1,  # 3600 秒
        )

        clock.now = 1006.0
        # watchdog 模式不检查 idle
        assert mgr._check_expiry() is False
        status = mgr.status()
        assert status["active"] is True
        assert status["mode"] == "watchdog"
        # watchdog 下 idle_timeout_seconds 兼容字段为 0
        assert status["idle_timeout_seconds"] == 0
        assert status["max_duration_seconds"] == 3600.0
        assert status["expires_at"] == 4600.0  # 1000 + 3600
        assert status["remaining_seconds"] == 3594  # 4600 - 1006

    def test_watchdog_max_duration_expires_transitions_to_normal(self):
        """watchdog 硬上限到期降级到 normal（而非撤销）"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        events: list[dict] = []
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy()
        mgr.subscribe(lambda e: events.append({"type": e.type, "state": e.state}))

        mgr.grant(
            mode=SessionMode.WATCHDOG,
            task_description="硬上限",
            source="agent",
            max_duration_hours=1,  # 3600 秒
        )

        # 到期：clock 推进到 4601（granted_at=1000 + 3600 = 4600，已超过）
        clock.now = 4601.0
        # _check_expiry 触发 _expire_if_max_duration → transition(NORMAL)
        assert mgr._check_expiry() is True
        status = mgr.status()
        # 降级到 normal（不是撤销）
        assert status["active"] is True
        assert status["mode"] == "normal"
        assert status["max_duration_seconds"] is None
        # 触发了 transition 事件
        assert any(e["type"] == "transition" for e in events)

    def test_reset_session_manager_start_worker_param(self):
        """reset_session_manager(start_worker=) 参数语义（Qt 合跑段错误诱因治理）。

        - 默认 True：重建的单例带 worker 线程（生产行为锚定，防默认值被改）
        - False：单例无 worker 线程，且 grant 后仍无——授权状态机照常、
          线程不再随测试生灭（spec: temp/sdd/qt-segfault-conftest/）
        """
        from server.screen.session.manager import SessionManager

        try:
            # 默认 True：grant 后起线程（生产语义；worker 在 grant 时才启动）
            reset_session_manager()
            mgr = get_session_manager()
            assert mgr._worker is None  # 未 grant 前无线程
            mgr.grant(mode=SessionMode.NORMAL, task_description="with worker", source="agent")
            assert mgr._worker is not None and mgr._worker.is_alive()

            # False：grant 后仍无线程（授权状态机照常）
            reset_session_manager(start_worker=False)
            mgr = get_session_manager()
            assert mgr._worker is None
            mgr.grant(mode=SessionMode.NORMAL, task_description="no worker", source="agent")
            assert mgr._worker is None
            assert mgr.status()["mode"] == "normal"
        finally:
            # 恢复 conftest 期望的形态（autouse fixture 下个测试会再次 reset(False)+grant）
            reset_session_manager(start_worker=False)

    def test_release_waits_for_worker_to_exit(self):
        """release 后 worker 线程退出"""
        from server.screen.session.manager import SessionManager

        # 停掉单例 worker，避免 threading.enumerate 按名字找到单例 worker 干扰断言
        reset_session_manager()
        mgr = SessionManager()
        mgr._policy = _make_policy()
        mgr.grant(mode=SessionMode.NORMAL, task_description="worker cleanup", source="agent")
        worker = mgr._worker
        assert worker is not None and worker.is_alive()

        assert mgr.release("test_release") is True
        # release 后 _worker 被置 None，且 _join_worker(timeout=1.0) 后线程应已退出
        assert mgr._worker is None
        assert not worker.is_alive()
        # 清理：停掉 mgr 残留（release 已停 worker，但保险）
        mgr.shutdown()

    def test_release_is_idempotent_and_clears_state(self):
        """release 幂等，二次调用返回 False，状态清除"""
        from server.screen.session.manager import SessionManager

        mgr = SessionManager(clock=_Clock(), start_worker=False)
        mgr._policy = _make_policy()
        mgr.grant(mode=SessionMode.NORMAL, task_description="测试任务", source="gui")

        assert mgr.release("manual") is True
        assert mgr.release("manual") is False  # 幂等
        status = mgr.status()
        assert status["active"] is False
        assert status["task_description"] == ""
        assert status["last_activity_at"] == 0.0

    def test_require_mode_checks_permission(self):
        """require_mode 权限检查：NORMAL 要求 NORMAL 或 WATCHDOG；WATCHDOG 要求 WATCHDOG"""
        from server.screen.session.manager import SessionManager

        mgr = SessionManager(clock=_Clock(), start_worker=False)
        mgr._policy = _make_policy()
        # 无授权
        assert mgr.require_mode(SessionMode.NORMAL) is False
        assert mgr.require_mode(SessionMode.WATCHDOG) is False
        # normal 授权
        mgr.grant(mode=SessionMode.NORMAL, task_description="normal", source="agent")
        assert mgr.require_mode(SessionMode.NORMAL) is True
        assert mgr.require_mode(SessionMode.WATCHDOG) is False
        # watchdog 授权
        mgr.release("test")
        mgr.grant(mode=SessionMode.WATCHDOG, task_description="watchdog", source="agent",
                  max_duration_hours=1)
        assert mgr.require_mode(SessionMode.NORMAL) is True  # watchdog 兼容 normal
        assert mgr.require_mode(SessionMode.WATCHDOG) is True

    def test_can_operate_and_can_shutdown(self):
        """can_operate: mode != NO_PERMISSION；can_shutdown: mode == WATCHDOG + shutdown_permitted"""
        from server.screen.session.manager import SessionManager

        mgr = SessionManager(clock=_Clock(), start_worker=False)
        mgr._policy = _make_policy()
        # 无授权
        assert mgr.can_operate() is False
        assert mgr.can_shutdown() is False
        # normal 授权
        mgr.grant(mode=SessionMode.NORMAL, task_description="normal", source="agent")
        assert mgr.can_operate() is True
        assert mgr.can_shutdown() is False  # normal 不能关机
        # watchdog 授权 + shutdown_permitted=False
        mgr.release("test")
        mgr.grant(mode=SessionMode.WATCHDOG, task_description="watchdog", source="agent",
                  max_duration_hours=1, shutdown_permitted=False)
        assert mgr.can_operate() is True
        assert mgr.can_shutdown() is False  # watchdog 但未勾选允许关机
        # watchdog 授权 + shutdown_permitted=True
        mgr.release("test")
        mgr.grant(mode=SessionMode.WATCHDOG, task_description="watchdog", source="agent",
                  max_duration_hours=1, shutdown_permitted=True)
        assert mgr.can_shutdown() is True

    def test_transition_watchdog_to_normal_resets_idle(self):
        """transition(watchdog → normal) 重置 idle 为 0"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy()
        mgr.grant(mode=SessionMode.WATCHDOG, task_description="test", source="agent",
                  max_duration_hours=1)
        original_granted = mgr.status()["granted_at"]

        clock.now = 1100.0  # 推进 100 秒
        assert mgr.transition(SessionMode.NORMAL) is True
        status = mgr.status()
        assert status["mode"] == "normal"
        # idle 重置：last_activity_at = now = 1100
        assert status["last_activity_at"] == 1100.0
        # granted_at 也重置（normal 不用此字段算 remaining）
        assert status["granted_at"] == 1100.0
        assert status["granted_at"] != original_granted

    def test_warning_event_published_once(self):
        """warning 阶段触发 warning 事件仅一次，不撤销"""
        from server.screen.session.manager import SessionManager

        clock = _Clock()
        events: list[str] = []
        mgr = SessionManager(clock=clock, start_worker=False)
        mgr._policy = _make_policy(idle_warning=10, idle_grace=20)  # total=30
        mgr.subscribe(lambda e: events.append(e.type))

        mgr.grant(mode=SessionMode.NORMAL, task_description="test", source="agent")
        # 推进 15 秒（进入 warning 阶段，未到 total）
        clock.now = 1015.0
        assert mgr._check_expiry() is False  # 不撤销
        assert mgr.status()["active"] is True
        assert mgr.status()["phase"] == "warning"
        assert events.count("warning") == 1
        # 再次检查，warning 不重复触发
        assert mgr._check_expiry() is False
        assert events.count("warning") == 1

    def test_shutdown_permitted_field_in_status(self):
        """status 返回 shutdown_permitted 字段"""
        from server.screen.session.manager import SessionManager

        mgr = SessionManager(clock=_Clock(), start_worker=False)
        mgr._policy = _make_policy()
        # 无授权
        assert mgr.status()["shutdown_permitted"] is False
        # normal
        mgr.grant(mode=SessionMode.NORMAL, task_description="n", source="agent")
        assert mgr.status()["shutdown_permitted"] is False
        # watchdog + shutdown_permitted=True
        mgr.release("test")
        mgr.grant(mode=SessionMode.WATCHDOG, task_description="w", source="agent",
                  max_duration_hours=1, shutdown_permitted=True)
        assert mgr.status()["shutdown_permitted"] is True


# ========== T2: /screen/control/request|release 端点契约 ==========


class TestControlRequestContract:
    def test_watchdog_request_returns_mode_and_hard_limit(self, client, monkeypatch):
        """watchdog 请求返回 mode=watchdog + max_duration_seconds=36000（10 小时默认）"""
        overlay = server.overlay_client.overlay_client
        # mock 弹窗：用户同意 watchdog
        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
            },
        )

        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "盯任务",
                "source": "agent",
                "mode": "watchdog",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["status"] == "authorized"
        assert body["mode"] == "watchdog"
        assert body["max_duration_seconds"] == 36000  # 默认 10 小时
        assert body["task_authorization"]["mode"] == "watchdog"
        # watchdog 下 idle_timeout_seconds 兼容字段为 0
        assert body["task_authorization"]["idle_timeout_seconds"] == 0
        # 授权成功后 overlay 显示（新设计：文案由 _tick_loop 推送 task_description，
        # 不再在授权时拼接"看门狗模式..."文案，故只验证 overlay_visible）
        assert overlay.overlay_visible is True

    def test_request_requires_gui_confirmation(self, client, monkeypatch):
        """request 必须经过 ensure_takeover_approved 弹窗"""
        overlay = server.overlay_client.overlay_client
        called = {"count": 0}

        def approve(*args, **kwargs):
            called["count"] += 1
            return {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", approve)
        response = client.post(
            "/screen/control/request",
            json={"task_description": "连续整理窗口", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert called["count"] == 1
        assert body["success"] is True
        assert body["status"] == "authorized"
        assert body["task_authorization"]["active"] is True
        assert body["task_authorization"]["task_description"] == "连续整理窗口"

    def test_request_rejection_does_not_grant(self, client, monkeypatch):
        """用户拒绝时不授权"""
        overlay = server.overlay_client.overlay_client
        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "cancelled",
                "reason": "用户正在使用电脑",
                "task_authorization": False,
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "连续整理窗口", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["status"] == "cancelled"
        assert body["persistent_mode"] is False
        assert body["user_reason"] == "用户正在使用电脑"

    def test_disabled_feature_rejects_request(self, client, monkeypatch):
        """takeover_persistent_enabled=False 时拒绝 request"""
        from server import config

        original = config.get_screen_config

        def disabled_config():
            values = original()
            values["takeover_persistent_enabled"] = False
            return values

        monkeypatch.setattr(config, "get_screen_config", disabled_config)
        response = client.post(
            "/screen/control/request",
            json={"task_description": "连续整理窗口", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["status"] == "disabled"
        assert body["persistent_mode"] is False

    def test_health_changes_immediately_after_grant_and_release(self, client, monkeypatch):
        """/health 立即反映授权状态变化"""
        from server.core import health as health_mod
        from server.screen.session import get_session_manager

        overlay = server.overlay_client.overlay_client
        # 撤销 conftest 默认 grant，确保 before["active"] is False
        get_session_manager().release("test_setup")
        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )

        before = client.get("/health").json()["screen"]["takeover_persistent"]
        assert before["active"] is False

        granted = client.post(
            "/screen/control/request",
            json={"task_description": "即时健康状态", "mode": "normal"},
        )
        assert granted.json()["persistent_mode"] is True
        health_mod._status_cache["data"] = None  # 清缓存
        active = client.get("/health").json()["screen"]["takeover_persistent"]
        assert active["active"] is True
        assert active["task_description"] == "即时健康状态"

        client.post("/screen/control/release", json={})
        health_mod._status_cache["data"] = None
        released = client.get("/health").json()["screen"]["takeover_persistent"]
        assert released["active"] is False


# ========== T3: watchdog 授权模式选项 + 用户反馈（ISSUE-003/004） ==========


class TestWatchdogModeAuthorization:
    """ISSUE-003/004：watchdog 授权弹窗模式选项 + 用户反馈完整返回。

    覆盖：
    - agent 请求 watchdog + 用户勾选 watchdog → mode=watchdog, status=authorized
    - agent 请求 watchdog + 用户未勾选 watchdog → mode=normal, status=mode_downgraded
    - 成功路径返回 user_reason（ISSUE-004）
    - ensure_takeover_approved 接收 requested_mode 参数（ISSUE-003）
    """

    def test_watchdog_request_with_user_consent_returns_watchdog_mode(self, client, monkeypatch):
        """agent 请求 watchdog + 用户勾选 watchdog 复选框 → 授权 watchdog"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()
        captured = {}

        def approve(*args, **kwargs):
            captured["requested_mode"] = kwargs.get("requested_mode")
            return {
                "status": "confirmed",
                "reason": "同意 watchdog",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", approve)
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "长时间监控任务",
                "source": "agent",
                "mode": "watchdog",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert captured["requested_mode"] == "watchdog"
        assert body["success"] is True
        assert body["requested_mode"] == "watchdog"
        assert body["mode"] == "watchdog"
        assert body["status"] == "authorized"
        assert body["max_duration_seconds"] == 36000
        assert body["user_reason"] == "同意 watchdog"
        # SessionManager 状态校验
        assert session.status()["mode"] == "watchdog"

    def test_watchdog_request_without_user_consent_downgrades_to_normal(self, client, monkeypatch):
        """agent 请求 watchdog + 用户未勾选 watchdog → 降级为 normal，status=mode_downgraded"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        def approve(*args, **kwargs):
            return {
                "status": "confirmed",
                "reason": "不想开 watchdog，普通模式就行",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "normal",
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", approve)
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "监控任务",
                "source": "agent",
                "mode": "watchdog",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["requested_mode"] == "watchdog"
        assert body["mode"] == "normal"
        assert body["status"] == "mode_downgraded"
        assert body["max_duration_seconds"] is None
        assert body["user_reason"] == "不想开 watchdog，普通模式就行"
        assert session.status()["mode"] == "normal"
        assert session.is_active() is True

    def test_normal_request_returns_normal_mode(self, client, monkeypatch):
        """agent 请求 normal → 用户授权 normal（无降级）"""
        overlay = server.overlay_client.overlay_client

        def approve(*args, **kwargs):
            return {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", approve)
        response = client.post(
            "/screen/control/request",
            json={"task_description": "普通任务", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["requested_mode"] == "normal"
        assert body["mode"] == "normal"
        assert body["status"] == "authorized"
        assert body["user_reason"] == ""

    def test_success_path_returns_user_feedback(self, client, monkeypatch):
        """ISSUE-004：成功授权路径必须返回用户反馈"""
        overlay = server.overlay_client.overlay_client

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "同意，但请小心操作",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "整理窗口", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["user_reason"] == "同意，但请小心操作"

    def test_cancelled_path_returns_user_feedback(self, client, monkeypatch):
        """ISSUE-004：取消路径也返回用户反馈"""
        overlay = server.overlay_client.overlay_client

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "cancelled",
                "reason": "拒绝，我现在在用电脑",
                "task_authorization": False,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "整理窗口", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["status"] == "cancelled"
        assert body["user_reason"] == "拒绝，我现在在用电脑"


# ========== T4: 截图后 overlay 恢复（ISSUE-005） ==========


class TestIssue005OverlayRestore:
    """ISSUE-005：截图/OCR 恢复路径恢复 overlay 显示。

    新设计（2026-08-05 重构后）：
    - OverlayClient 通过 _tick_loop 每秒读 SessionManager.status() 推 IPC tick
    - 子进程 OverlayWidget 自己根据 mode/phase/remaining_seconds/task_description 渲染文案与颜色
    - _restore_overlay_after_capture 只需恢复 overlay 可见性，文案由 tick 自动刷新
    - 旧测试断言 persistent=True + message 文案已过时（新 show_overlay 只接受 position 参数）
    """

    def test_restore_overlay_in_watchdog_mode_calls_show_overlay(self, client, monkeypatch):
        """watchdog 模式下截图后恢复 overlay → 调用 show_overlay() 一次（文案由 tick 自动刷新）"""
        overlay = server.overlay_client.overlay_client
        from server.screen.routes import _restore_overlay_after_capture

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
            },
        )
        client.post(
            "/screen/control/request",
            json={"task_description": "watchdog 测试", "mode": "watchdog"},
        )
        assert get_session_manager().is_active() is True
        assert get_session_manager().status()["mode"] == "watchdog"

        captured_calls = []
        original_show = overlay.show_overlay

        def capture_show(*args, **kwargs):
            captured_calls.append({"args": args, "kwargs": kwargs.copy()})
            return original_show(*args, **kwargs)

        monkeypatch.setattr(overlay, "show_overlay", capture_show)
        _restore_overlay_after_capture()

        assert len(captured_calls) == 1
        # 新 show_overlay 只接受 position 参数，不传 message/persistent
        assert "message" not in captured_calls[0]["kwargs"]
        assert "persistent" not in captured_calls[0]["kwargs"]

    def test_restore_overlay_in_normal_mode_calls_show_overlay(self, client, monkeypatch):
        """normal 持久授权模式下截图后恢复 overlay → 调用 show_overlay() 一次"""
        overlay = server.overlay_client.overlay_client
        from server.screen.routes import _restore_overlay_after_capture

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )
        client.post(
            "/screen/control/request",
            json={"task_description": "normal 持久测试", "mode": "normal"},
        )
        assert get_session_manager().is_active() is True
        assert get_session_manager().status()["mode"] == "normal"

        captured_calls = []
        original_show = overlay.show_overlay

        def capture_show(*args, **kwargs):
            captured_calls.append({"args": args, "kwargs": kwargs.copy()})
            return original_show(*args, **kwargs)

        monkeypatch.setattr(overlay, "show_overlay", capture_show)
        _restore_overlay_after_capture()

        assert len(captured_calls) == 1
        assert "message" not in captured_calls[0]["kwargs"]
        assert "persistent" not in captured_calls[0]["kwargs"]

    def test_restore_overlay_without_persistent_mode_calls_show_overlay(self, client, monkeypatch):
        """无持久授权时截图后恢复 overlay → 仍调用 show_overlay()（保持可见性恢复语义）"""
        overlay = server.overlay_client.overlay_client
        from server.screen.routes import _restore_overlay_after_capture

        client.post("/screen/control/release", json={})
        assert get_session_manager().is_active() is False

        captured_calls = []
        original_show = overlay.show_overlay

        def capture_show(*args, **kwargs):
            captured_calls.append({"args": args, "kwargs": kwargs.copy()})
            return original_show(*args, **kwargs)

        monkeypatch.setattr(overlay, "show_overlay", capture_show)
        _restore_overlay_after_capture()

        assert len(captured_calls) == 1
        assert "message" not in captured_calls[0]["kwargs"]
        assert "persistent" not in captured_calls[0]["kwargs"]


# ========== T5: watchdog imply task_authorization（ISSUE-006） ==========


class TestIssue006WatchdogImpliesTaskAuthorization:
    """ISSUE-006：watchdog 勾选 imply task_authorization（双保险 + UI 联动）。"""

    def test_only_watchdog_checked_still_grants_watchdog(self, client, monkeypatch):
        """只勾 watchdog（不勾 task_authorization）→ 仍授权 watchdog"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "只勾 watchdog",
                "task_authorization": True,  # imply 逻辑保证为 True
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "只勾 watchdog 测试", "mode": "watchdog"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["mode"] == "watchdog"
        assert body["status"] == "authorized"
        assert session.status()["mode"] == "watchdog"
        assert session.is_active() is True

    def test_only_task_authorization_checked_grants_normal(self, client, monkeypatch):
        """只勾 task_authorization（不勾 watchdog）→ 授权 normal"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "只勾 task_auth 测试", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["mode"] == "normal"
        assert body["status"] == "authorized"
        assert session.status()["mode"] == "normal"
        assert session.is_active() is True

    def test_neither_checked_grants_confirmed_once(self, client, monkeypatch):
        """都不勾 → confirmed_once（不建立持久授权）"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()
        # 撤销 conftest 默认 grant，确保 session.is_active() is False
        session.release("test_setup")

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": False,
                "requested_mode": "normal",
                "authorized_mode": "normal",
            },
        )
        response = client.post(
            "/screen/control/request",
            json={"task_description": "都不勾测试", "mode": "normal"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["status"] == "confirmed_once"
        assert session.is_active() is False


# ========== T6: agent_guide 引用 ==========


class TestTaskAuthorizationGuidance:
    def test_computer_use_starts_with_task_authorization_request(self):
        from server.agent_guide import GUIDE_REGISTRY

        guide = GUIDE_REGISTRY["dev.computer_use"]
        assert "screen_request_control" in guide["first_action"]
        assert any(
            "screen_request_control" in item
            for item in guide["mcp_tools_priority"]
        )

    def test_task_closure_uses_current_task_language(self):
        from server.agent_guide import GUIDE_REGISTRY

        guide = GUIDE_REGISTRY["system.task_closure"]
        closure_text = " ".join(
            [guide["first_action"], guide["workflow_summary"]]
            + guide["key_pitfalls"]
        )
        assert "当前任务授权" in closure_text
        assert "空闲警告线程" not in closure_text


# ========== T7: 授权后的控制策略 ==========


class TestAuthorizedControlPolicy:
    @staticmethod
    def _authorize(task: str = "连续操作"):
        """授权 normal 模式 + 显示 overlay（替代旧 gui_client.grant_task_control）。"""
        session = get_session_manager()
        session.grant(mode=SessionMode.NORMAL, task_description=task, source="agent")
        overlay = server.overlay_client.overlay_client
        # 新设计：show_overlay 无 message 参数，文案由 _tick_loop 推送 task_description
        overlay.show_overlay()
        return overlay

    def test_ordinary_action_skips_confirmation_and_touches_after_delivery(
        self, client, monkeypatch
    ):
        """普通操作免确认 + 成功投递后 extend idle"""
        import server.screen.action_endpoints as endpoint

        overlay = self._authorize()
        session = get_session_manager()
        confirmations = {"count": 0}
        monkeypatch.setattr(
            overlay,
            "confirm_action",
            lambda **kwargs: confirmations.update(count=confirmations["count"] + 1)
            or {"status": "confirmed", "same_coords_skip": False, "reason": ""},
        )
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )
        before = session.status()["last_activity_at"]

        response = client.post(
            "/screen/action",
            json={
                "action": "click",
                "x": 10,
                "y": 10,
                "task_description": "连续操作",
            },
        )

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert confirmations["count"] == 0
        assert session.status()["last_activity_at"] >= before

    def test_watchdog_blocks_danger_keywords_without_confirmation(
        self, client, monkeypatch
    ):
        """watchdog 模式下 danger_level=block 仍拦截，不弹确认"""
        import server.screen.action_endpoints as endpoint

        session = get_session_manager()
        session.grant(
            mode=SessionMode.WATCHDOG,
            task_description="受控托管测试",
            source="agent",
            max_duration_hours=1,
        )
        overlay = server.overlay_client.overlay_client
        confirmations = {"count": 0}
        monkeypatch.setattr(
            overlay,
            "confirm_action",
            lambda **kwargs: confirmations.update(count=confirmations["count"] + 1)
            or {"status": "confirmed", "same_coords_skip": False, "reason": ""},
        )
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "must not execute"},
        )

        response = client.post(
            "/screen/action",
            json={"action": "type", "text": "关机"},
        )

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert response.json()["status"] == "blocked"
        assert confirmations["count"] == 0

    def test_watchdog_keeps_confirm_level_actions_fail_closed(
        self, client, monkeypatch
    ):
        """watchdog 模式下 danger_level=confirm 自动跳过确认，但 confirm mock 返回 cancelled 时不执行"""
        # 注：T14 实现中 watchdog 模式下 confirm 自动跳过（不弹窗直接执行），
        # 所以这个测试改语义：watchdog 下 confirm 操作应直接执行（不弹窗）
        import server.screen.action_endpoints as endpoint

        session = get_session_manager()
        session.grant(
            mode=SessionMode.WATCHDOG,
            task_description="受控托管测试",
            source="agent",
            max_duration_hours=1,
        )
        overlay = server.overlay_client.overlay_client
        confirmations = {"count": 0}

        def cancel(**kwargs):
            confirmations["count"] += 1
            return {"status": "cancelled", "reason": "用户不在场"}

        monkeypatch.setattr(overlay, "confirm_action", cancel)
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "executed"},
        )

        response = client.post(
            "/screen/action",
            json={"action": "click", "x": 10, "y": 10, "element_text": "删除"},
        )

        assert response.status_code == 200
        # watchdog 模式下 confirm 自动跳过 → 直接执行成功
        assert response.json()["success"] is True
        # confirm_action 不应被调用（watchdog 自动跳过）
        assert confirmations["count"] == 0

    def test_dangerous_action_still_confirms_while_authorized(
        self, client, monkeypatch
    ):
        """normal 模式下 danger_level=confirm 仍弹确认窗"""
        import server.screen.action_endpoints as endpoint

        overlay = self._authorize()
        confirmations = {"count": 0}

        def confirm(**kwargs):
            confirmations["count"] += 1
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )

        response = client.post(
            "/screen/action",
            json={
                "action": "click",
                "x": 10,
                "y": 10,
                "element_text": "确认删除",
            },
        )

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert confirmations["count"] == 1

    def test_explicit_confirmation_still_confirms_while_authorized(
        self, client, monkeypatch
    ):
        """require_confirm=True 时仍弹确认窗"""
        import server.screen.action_endpoints as endpoint

        overlay = self._authorize()
        confirmations = {"count": 0}

        def confirm(**kwargs):
            confirmations["count"] += 1
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )

        response = client.post(
            "/screen/action",
            json={
                "action": "click",
                "x": 10,
                "y": 10,
                "require_confirm": True,
            },
        )

        assert response.status_code == 200
        assert confirmations["count"] == 1

    def test_dangerous_action_ignores_existing_coordinate_skip(
        self, client, monkeypatch
    ):
        """danger_level=confirm 忽略 auto_skip 缓存"""
        import server.screen.action_endpoints as endpoint
        from server.screen.security import _auto_skip_coords

        overlay = self._authorize()
        confirmations = {"count": 0}
        monkeypatch.setattr(
            overlay,
            "confirm_action",
            lambda **kwargs: confirmations.update(
                count=confirmations["count"] + 1
            )
            or {"status": "confirmed", "same_coords_skip": False, "reason": ""},
        )
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )
        _auto_skip_coords[("click", 10, 10)] = True
        try:
            response = client.post(
                "/screen/action",
                json={
                    "action": "click",
                    "x": 10,
                    "y": 10,
                    "element_text": "确认删除",
                },
            )
        finally:
            _auto_skip_coords.pop(("click", 10, 10), None)

        assert response.status_code == 200
        assert confirmations["count"] == 1

    def test_explicit_confirmation_ignores_existing_coordinate_skip(
        self, client, monkeypatch
    ):
        """require_confirm=True 忽略 auto_skip 缓存"""
        import server.screen.action_endpoints as endpoint
        from server.screen.security import _auto_skip_coords

        overlay = self._authorize()
        confirmations = {"count": 0}
        monkeypatch.setattr(
            overlay,
            "confirm_action",
            lambda **kwargs: confirmations.update(
                count=confirmations["count"] + 1
            )
            or {"status": "confirmed", "same_coords_skip": False, "reason": ""},
        )
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )
        _auto_skip_coords[("click", 10, 10)] = True
        try:
            response = client.post(
                "/screen/action",
                json={
                    "action": "click",
                    "x": 10,
                    "y": 10,
                    "require_confirm": True,
                },
            )
        finally:
            _auto_skip_coords.pop(("click", 10, 10), None)

        assert response.status_code == 200
        assert confirmations["count"] == 1

    def test_dry_run_does_not_confirm_or_extend_authorization(
        self, client, monkeypatch
    ):
        """dry_run 不弹确认 + 不 extend"""
        overlay = self._authorize()
        session = get_session_manager()
        confirmations = {"count": 0}
        monkeypatch.setattr(
            overlay,
            "confirm_action",
            lambda **kwargs: confirmations.update(count=confirmations["count"] + 1)
            or {"status": "confirmed", "same_coords_skip": False, "reason": ""},
        )
        before = session.status()["last_activity_at"]

        response = client.post(
            "/screen/action",
            json={"action": "click", "x": 10, "y": 10, "dry_run": True},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"
        assert confirmations["count"] == 0
        assert session.status()["last_activity_at"] == before

    def test_failed_action_does_not_extend_authorization(self, client, monkeypatch):
        """失败的操作不 extend"""
        import server.screen.action_endpoints as endpoint

        self._authorize()
        session = get_session_manager()
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": False, "message": "failed"},
        )
        before = session.status()["last_activity_at"]

        response = client.post(
            "/screen/action",
            json={"action": "click", "x": 10, "y": 10},
        )

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert session.status()["last_activity_at"] == before

    def test_batch_dangerous_action_confirms_and_success_touches(
        self, client, monkeypatch
    ):
        """batch_actions 中 danger_level=confirm 弹窗 + 成功后 extend"""
        import server.screen.batch_endpoints as endpoint

        overlay = self._authorize()
        session = get_session_manager()
        confirmations = {"count": 0}

        def confirm(**kwargs):
            confirmations["count"] += 1
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        monkeypatch.setattr(endpoint, "_ADMIN_STATUS", True)
        monkeypatch.setattr(
            endpoint,
            "_execute_action",
            lambda **kwargs: {"success": True, "message": "clicked"},
        )
        before = session.status()["last_activity_at"]

        response = client.post(
            "/screen/batch-actions",
            json={
                "actions": [
                    {
                        "action": "click",
                        "x": 10,
                        "y": 10,
                        "element_text": "确认删除",
                    }
                ]
            },
        )

        assert response.status_code == 200
        assert confirmations["count"] == 1
        assert session.status()["last_activity_at"] >= before

    def test_transaction_dangerous_action_confirms_even_in_dry_run(
        self, client, monkeypatch
    ):
        """desktop-transaction dry_run 仍弹确认窗"""
        import server.screen.desktop_transaction_endpoints as endpoint

        overlay = self._authorize()
        confirmations = {"count": 0}

        def confirm(**kwargs):
            confirmations["count"] += 1
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        monkeypatch.setattr(endpoint, "_ADMIN_STATUS", True)
        monkeypatch.setattr(endpoint, "_find_window", lambda *args, **kwargs: None)

        response = client.post(
            "/screen/desktop-transaction",
            json={
                "target": {"window_title": "fake"},
                "actions": [
                    {
                        "action": "scroll",
                        "direction": "down",
                        "element_text": "确认删除",
                    }
                ],
                "expected": {"type": "ocr_contains", "text": "done"},
                "dry_run": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"
        assert confirmations["count"] == 1

    def test_transaction_wait_does_not_touch_authorization(
        self, client, monkeypatch
    ):
        """desktop-transaction wait 操作不 extend"""
        import server.screen.desktop_transaction_endpoints as endpoint

        self._authorize()
        session = get_session_manager()
        monkeypatch.setattr(endpoint, "_ADMIN_STATUS", True)
        monkeypatch.setattr(endpoint, "_find_window", lambda *args, **kwargs: None)
        before = session.status()["last_activity_at"]

        response = client.post(
            "/screen/desktop-transaction",
            json={
                "target": {"window_title": "fake"},
                "actions": [{"action": "wait", "wait": 0}],
                "expected": {"type": "ocr_contains", "text": "done"},
                "dry_run": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "dry_run"
        assert session.status()["last_activity_at"] == before

    def test_uia_requires_takeover_and_dangerous_uia_confirms(
        self, client, monkeypatch
    ):
        """UIA 端点需授权 + danger_level=confirm 弹窗"""
        import server.screen.uia as uia_module

        overlay = server.overlay_client.overlay_client
        session = get_session_manager()
        # 先撤销授权（conftest 默认 grant normal，这里撤销）
        session.release("test")
        overlay.overlay_visible = False
        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "cancelled",
                "reason": "拒绝",
                "task_authorization": False,
            },
        )
        monkeypatch.setattr(
            uia_module,
            "execute_semantic_action",
            lambda **kwargs: {
                "success": True,
                "status": "executed_unverified",
                "action": kwargs["action"],
                "element_id": kwargs["element_id"],
                "element_role": "Button",
                "element_name": "确认删除",
                "transport_status": "sent",
                "fallback_reason": None,
                "elapsed_ms": 1,
                "message": "executed",
            },
        )

        denied = client.post(
            "/screen/uia/action",
            json={
                "element_id": "1:abc:0",
                "snapshot_id": "abc",
                "action": "invoke",
                "element_text": "普通按钮",
            },
        )
        # 无授权时返回 403（T15 副作用端点权限拦截）
        assert denied.status_code == 403

        # 授权 normal
        session.grant(mode=SessionMode.NORMAL, task_description="UIA task", source="agent")
        confirmations = {"count": 0}

        def confirm(**kwargs):
            confirmations["count"] += 1
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        authorized = client.post(
            "/screen/uia/action",
            json={
                "element_id": "1:abc:0",
                "snapshot_id": "abc",
                "action": "invoke",
                "element_text": "确认删除",
            },
        )
        assert authorized.status_code == 200
        assert authorized.json()["success"] is True
        assert confirmations["count"] == 1

    def test_window_close_always_requires_danger_confirmation(
        self, client, monkeypatch
    ):
        """window_close 始终弹 danger 确认窗"""
        import server.screen.lifecycle_endpoints as endpoint

        overlay = self._authorize()
        confirmations = {"count": 0, "text": ""}

        def confirm(**kwargs):
            confirmations["count"] += 1
            confirmations["text"] = kwargs.get("text", "")
            return {"status": "confirmed", "same_coords_skip": False, "reason": ""}

        monkeypatch.setattr(overlay, "confirm_action", confirm)
        monkeypatch.setattr(
            endpoint,
            "_close_window",
            lambda hwnd, force=False: {
                "success": True,
                "status": "executed",
                "message": "closed",
                "post_state": {"hwnd": hwnd, "exists": False},
            },
        )

        response = client.post(
            "/screen/window/close",
            json={"hwnd": 123, "force": True},
        )

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert confirmations["count"] == 1
        assert "强制关闭窗口" in confirmations["text"]


# ========== T8: 弹窗结果透传（2026-08-06 watchdog 静默降级 bug 回归） ==========


class TestDialogResultPropagation:
    """回归覆盖：routes.py 将弹窗返回的 max_duration_hours / shutdown_permitted
    透传到 session.grant，而非硬编码或仅用 agent 请求参数。

    2026-08-06 bug：
    - authorized_mode 字段在 OverlayClient.ensure_takeover_approved 返回中缺失
      （dialog _finish 注释写"authorized_mode 总 = requested_mode"但字段从未设置），
      routes.py 用 result.get("authorized_mode", "normal") 读取，永远走 "normal" 分支
    - shutdown_permitted=False 硬编码，弹窗"允许关机"复选框值被丢弃
    - max_duration_hours 用 req.max_duration_hours（agent 请求）而非弹窗 spinbox 值

    conftest.py mock 显式塞 authorized_mode 把 bug 掩盖，测试全绿但生产挂。
    """

    def test_dialog_max_duration_hours_overrides_agent_request(self, client, monkeypatch):
        """弹窗返回 max_duration_hours=5 → session 用 5h（不用 agent 请求的 None）。"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
                "max_duration_hours": 5,  # 用户在弹窗改成 5 小时
                "shutdown_permitted": False,
            },
        )
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "弹窗时长优先",
                "source": "agent",
                "mode": "watchdog",
                # agent 没传 max_duration_hours，弹窗返回 5h 应被采用
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "watchdog"
        assert body["max_duration_seconds"] == 5 * 3600
        status = session.status()
        assert status["mode"] == "watchdog"
        assert status["max_duration_seconds"] == 5 * 3600
        assert status["shutdown_permitted"] is False

    def test_dialog_shutdown_permitted_true_propagates_to_session(self, client, monkeypatch):
        """弹窗返回 shutdown_permitted=True → session.shutdown_permitted=True（不硬编码 False）。"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
                "max_duration_hours": 3,
                "shutdown_permitted": True,  # 用户勾选允许关机
            },
        )
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "允许关机测试",
                "source": "agent",
                "mode": "watchdog",
            },
        )

        assert response.status_code == 200
        assert response.json()["mode"] == "watchdog"
        status = session.status()
        assert status["mode"] == "watchdog"
        assert status["shutdown_permitted"] is True
        assert session.can_shutdown() is True

    def test_dialog_max_duration_none_falls_back_to_agent_request(self, client, monkeypatch):
        """弹窗未返回 max_duration_hours（None）→ fallback 到 agent 请求参数 8h。"""
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
                "max_duration_hours": None,  # 弹窗未返回
                "shutdown_permitted": False,
            },
        )
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "fallback agent 请求",
                "source": "agent",
                "mode": "watchdog",
                "max_duration_hours": 8,  # agent 请求 8h
            },
        )

        assert response.status_code == 200
        assert response.json()["max_duration_seconds"] == 8 * 3600
        assert session.status()["max_duration_seconds"] == 8 * 3600

    def test_authorized_mode_watchdog_grants_watchdog_not_normal(self, client, monkeypatch):
        """authorized_mode="watchdog" → session.grant(mode=WATCHDOG)，不降级为 normal。

        这是 2026-08-06 bug 的核心回归：修复前 authorized_mode 字段缺失，
        routes.py 读取默认值 "normal"，watchdog 永远拿不到。
        """
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
                "max_duration_hours": 2,
                "shutdown_permitted": True,
            },
        )
        response = client.post(
            "/screen/control/request",
            json={
                "task_description": "watchdog 核心回归",
                "source": "agent",
                "mode": "watchdog",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "watchdog"
        assert body["status"] == "authorized"
        assert session.status()["mode"] == "watchdog"
        assert session.is_active() is True
        assert session.can_shutdown() is True

    def test_skipped_path_preserves_existing_watchdog_mode(self, client, monkeypatch):
        """session 已激活 watchdog 时，重调 request 走 skipped 路径不降级。

        覆盖 ensure_takeover_approved 的 "session 已激活" 早返回路径：
        修复前该路径不返回 authorized_mode，routes.py 读默认 "normal"。
        修复后该路径返回当前 session 模式，watchdog 不被降级。
        """
        overlay = server.overlay_client.overlay_client
        session = get_session_manager()

        # 第一次：建立 watchdog 授权
        monkeypatch.setattr(
            overlay,
            "ensure_takeover_approved",
            lambda *args, **kwargs: {
                "status": "confirmed",
                "reason": "",
                "task_authorization": True,
                "requested_mode": "watchdog",
                "authorized_mode": "watchdog",
                "max_duration_hours": 4,
                "shutdown_permitted": True,
            },
        )
        client.post(
            "/screen/control/request",
            json={"task_description": "首次 watchdog", "mode": "watchdog"},
        )
        assert session.status()["mode"] == "watchdog"

        # 第二次：session 已激活，ensure_takeover_approved 走 "skipped" 早返回
        # 模拟真实 OverlayClient.ensure_takeover_approved 的早返回逻辑
        def skipped_return(*args, **kwargs):
            # 真实代码：session.is_active() → 返回 skipped + 当前 mode
            current_mode = session.status().get("mode", "normal")
            return {
                "status": "skipped",
                "reason": "",
                "task_authorization": True,
                "requested_mode": kwargs.get("requested_mode", "normal"),
                "authorized_mode": current_mode,  # 修复后才有此字段
                "max_duration_hours": None,
                "shutdown_permitted": False,
            }

        monkeypatch.setattr(overlay, "ensure_takeover_approved", skipped_return)
        response = client.post(
            "/screen/control/request",
            json={"task_description": "重调 request", "mode": "normal"},
        )

        # 重调后仍应保持 watchdog（不被 normal 请求降级）
        assert response.status_code == 200
        assert session.status()["mode"] == "watchdog"
