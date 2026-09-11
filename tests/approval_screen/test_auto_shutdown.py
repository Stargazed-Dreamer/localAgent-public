"""auto_shutdown 模块测试 — REST 端点 + 副作用容错

覆盖：
- T01: trigger dry_run 路径 + trigger_count 递增 + reset_state
- T02: trigger 真关机路径（mock subprocess.Popen）+ cancel 端点
- T03: 截屏 + inbox 副作用 + 失败容错
- T04: /health 集成

测试原则：
- 不真关机（dry_run=true 或 mock subprocess.Popen）
- 不真推 inbox（autouse fixture mock _push_inbox，详见 _safety_mock_popen_and_screenshot）
- 不真截屏（mock mss）
"""

from __future__ import annotations

import pytest

from server import auto_shutdown


@pytest.fixture(autouse=True)
def _reset_state_before_each():
    """每个测试前重置模块级状态，避免测试间污染"""
    auto_shutdown.reset_state()
    yield
    auto_shutdown.reset_state()


@pytest.fixture(autouse=True)
def _safety_mock_popen_and_screenshot(monkeypatch):
    """安全网（autouse）：默认 mock subprocess.Popen + _capture_screenshot +
    _push_inbox，防止任何测试意外触发真关机命令、真截屏或真推 inbox。

    本 fixture 是机制级防护——即便某个测试忘了传 dry_run=True 或忘了 mock Popen，
    也不会真的调 `shutdown /s /t 120`，也不会真往 inbox 写条目（后端在跑时
    _push_inbox 会真发 POST /inbox 污染收件箱）。

    需要验证 Popen 被调用参数 / 截屏行为 / inbox 失败容错的测试，自行 monkeypatch
    覆盖（后注册的 monkeypatch 会覆盖本 fixture，行为仍走 mock，安全）。

    T19 后：trigger 真关机路径（dry_run=False）需 watchdog + shutdown_permitted 授权。
    本 fixture 默认 grant watchdog，让现有 dry_run=False 测试无需逐个改签名即可通过权限检查。
    无权限 403 场景由 test_trigger_no_permission_returns_403 显式 reset 后验证。
    """
    def _fake_popen(cmd, *args, **kwargs):
        return object()
    monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)
    monkeypatch.setattr(
        "server.auto_shutdown._capture_screenshot",
        lambda task_id: None,
    )
    # inbox 推送默认短路：测试不应真调 POST /inbox 污染收件箱
    # T04：_push_inbox 改为 async，mock 也需 async
    async def _fake_push_inbox(*a, **k):
        return True
    monkeypatch.setattr("server.auto_shutdown._push_inbox", _fake_push_inbox)

    # T19：默认 grant watchdog + shutdown_permitted，让 dry_run=False 测试通过权限检查
    # conftest.mock_overlay_client 已 reset_session_manager()，此处重新 grant
    from server.screen.session import Mode as SessionMode
    from server.screen.session import get_session_manager
    get_session_manager().grant(
        mode=SessionMode.WATCHDOG,
        task_description="auto_shutdown test",
        source="test",
        max_duration_hours=1,
        shutdown_permitted=True,
    )


# ========== T01: trigger dry_run + trigger_count + reset_state ==========

class TestTriggerDryRun:
    def test_trigger_success_dry_run(self, client):
        """dry_run=true：返回 success + shutdown_scheduled，不调 subprocess.Popen"""
        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "test_dry",
            "reason": "单元测试",
            "screenshot": False,
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["shutdown_scheduled"] is True
        assert data["dry_run"] is True
        assert data["task_id"] == "test_dry"
        assert data["trigger_count"] == 1
        assert data["cancel_command"] == "shutdown /a"

    def test_trigger_count_increments(self, client):
        """连续调 3 次，trigger_count 递增到 3"""
        for i in range(3):
            resp = client.post("/auto_shutdown/trigger", json={
                "task_id": f"task_{i}",
                "screenshot": False,
                "dry_run": True,
            })
            assert resp.status_code == 200
        assert auto_shutdown.get_status()["trigger_count"] == 3

    def test_reset_state(self, client):
        """reset_state() 后 trigger_count 归零"""
        client.post("/auto_shutdown/trigger", json={
            "task_id": "t1", "screenshot": False, "dry_run": True,
        })
        assert auto_shutdown.get_status()["trigger_count"] == 1
        auto_shutdown.reset_state()
        assert auto_shutdown.get_status()["trigger_count"] == 0
        assert auto_shutdown.get_status()["last_trigger_at"] is None
        assert auto_shutdown.get_status()["last_trigger_task_id"] is None

    def test_trigger_minimal_request(self, client):
        """只传 task_id，其他用默认值（显式 dry_run=True 防止真关机，
        即便已有 autouse 安全网 fixture 兜底）"""
        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "minimal",
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data
        assert data["task_id"] == "minimal"

    def test_trigger_no_permission_returns_403(self, client):
        """T19：无授权时 trigger（dry_run=False）返回 403 + shutdown_not_permitted。

        显式 reset SessionManager 撤销 autouse fixture grant 的 watchdog 授权，
        验证 fail-closed 行为。
        """
        from server.screen.session import reset_session_manager
        reset_session_manager()  # 撤销 autouse fixture 的 watchdog grant

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "no_perm",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["error"] == "shutdown_not_permitted"
        assert data["detail"]["current_mode"] == "no_permission"

    def test_trigger_normal_mode_returns_403(self, client):
        """T19：normal 模式（非 watchdog）trigger 返回 403。

        验证只有 watchdog + shutdown_permitted 才能关机，normal 模式不行。
        """
        from server.screen.session import Mode as SessionMode
        from server.screen.session import get_session_manager
        # 撤销 autouse 的 watchdog，改为 normal
        get_session_manager().grant(
            mode=SessionMode.NORMAL,
            task_description="normal mode test",
            source="test",
        )

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "normal_mode",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["error"] == "shutdown_not_permitted"
        assert data["detail"]["current_mode"] == "normal"

    def test_trigger_watchdog_without_shutdown_permitted_returns_403(self, client):
        """T19：watchdog 模式但未勾选允许关机，trigger 返回 403。"""
        from server.screen.session import Mode as SessionMode
        from server.screen.session import get_session_manager
        # 撤销 autouse 的 watchdog（shutdown_permitted=True），改为 shutdown_permitted=False
        get_session_manager().grant(
            mode=SessionMode.WATCHDOG,
            task_description="watchdog no shutdown",
            source="test",
            max_duration_hours=1,
            shutdown_permitted=False,
        )

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "watchdog_no_shutdown",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["error"] == "shutdown_not_permitted"
        assert data["detail"]["current_mode"] == "watchdog"
        assert data["detail"]["shutdown_permitted"] is False


# ========== T02: trigger 真关机 + cancel ==========

class TestTriggerRealAndCancel:
    def test_trigger_success_real(self, client, monkeypatch):
        """dry_run=false：subprocess.Popen 被调一次 with TRIGGER_COMMAND"""
        calls = []
        def _fake_popen(cmd, *args, **kwargs):
            calls.append(cmd)
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "real_trigger",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["shutdown_scheduled"] is True
        assert data["dry_run"] is False
        assert len(calls) == 1
        assert calls[0] == auto_shutdown.TRIGGER_COMMAND

    def test_trigger_subprocess_failure(self, client, monkeypatch):
        """subprocess.Popen 抛 OSError：返回 success=False + error，不 500"""
        def _fake_popen(cmd, *args, **kwargs):
            raise OSError("mock failure")
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "fail_test",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "error" in data
        assert "mock failure" in data["error"]

    def test_cancel_success_dry_run(self, client):
        """cancel dry_run=true：返回 success=True"""
        resp = client.post("/auto_shutdown/cancel", json={"dry_run": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["dry_run"] is True
        assert data["cancel_command"] == "shutdown /a"

    def test_cancel_success_real(self, client, monkeypatch):
        """cancel dry_run=false：subprocess.Popen 被调一次 with CANCEL_COMMAND"""
        calls = []
        def _fake_popen(cmd, *args, **kwargs):
            calls.append(cmd)
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        resp = client.post("/auto_shutdown/cancel", json={"dry_run": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(calls) == 1
        assert calls[0] == auto_shutdown.CANCEL_COMMAND


# ========== T03: 截屏 + inbox 容错 ==========

class TestSideEffectsTolerance:
    def test_trigger_screenshot_failure_tolerant(self, client, monkeypatch):
        """mss 抛异常：screenshot_path=None + success=True"""
        def _fake_popen(cmd, *args, **kwargs):
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        def _raise(*args, **kwargs):
            raise RuntimeError("mss unavailable")
        monkeypatch.setattr("server.auto_shutdown._capture_screenshot", _raise)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "screenshot_fail",
            "screenshot": True,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["screenshot_path"] is None

    def test_trigger_inbox_failure_tolerant(self, client, monkeypatch):
        """inbox POST 抛异常：success=True（inbox 失败不阻塞 shutdown）"""
        def _fake_popen(cmd, *args, **kwargs):
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        async def _raise(*args, **kwargs):
            raise RuntimeError("inbox down")
        monkeypatch.setattr("server.auto_shutdown._push_inbox", _raise)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "inbox_fail",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_trigger_screenshot_disabled(self, client, monkeypatch):
        """screenshot=False：不调 _capture_screenshot + screenshot_path=None"""
        calls = []
        def _fake_popen(cmd, *args, **kwargs):
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        def _capture_should_not_be_called(task_id):
            calls.append(task_id)
            return "should_not_be_called.png"
        monkeypatch.setattr("server.auto_shutdown._capture_screenshot", _capture_should_not_be_called)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "no_screenshot",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["screenshot_path"] is None
        assert len(calls) == 0


# ========== T04: dry_run 默认 True + 失败通知 ==========

class TestT04DryRunDefaultAndFailureNotify:
    def test_trigger_request_dry_run_defaults_true(self):
        """T04：TriggerRequest().dry_run 默认 True（忘传也不真关机）"""
        req = auto_shutdown.TriggerRequest(task_id="check_default")
        assert req.dry_run is True

    def test_cancel_request_dry_run_defaults_true(self):
        """T04：CancelRequest().dry_run 默认 True"""
        req = auto_shutdown.CancelRequest()
        assert req.dry_run is True

    def test_trigger_default_dry_run_no_popen(self, client, monkeypatch):
        """T04：不传 dry_run 时默认走 dry_run 路径，subprocess.Popen 不被调用"""
        calls = []
        def _fake_popen(cmd, *args, **kwargs):
            calls.append(cmd)
            return object()
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "default_dry_run",
            "screenshot": False,
            # 不传 dry_run，验证默认 True
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["success"] is True
        assert len(calls) == 0  # 未真调 shutdown 命令

    def test_trigger_shutdown_failure_pushes_inbox_warning(self, client, monkeypatch):
        """T04：shutdown 命令失败时推 inbox warning 通知用户（简化看门狗权限检查）"""
        def _fake_popen(cmd, *args, **kwargs):
            raise OSError("access denied")
        monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", _fake_popen)

        pushed = []
        async def _capture_push(*args, **kwargs):
            pushed.append({"source": kwargs.get("source") or (args[0] if args else None),
                            "category": kwargs.get("category") or (args[1] if len(args) > 1 else None),
                            "title": kwargs.get("title") or (args[2] if len(args) > 2 else None)})
            return True
        monkeypatch.setattr("server.auto_shutdown._push_inbox", _capture_push)

        resp = client.post("/auto_shutdown/trigger", json={
            "task_id": "fail_with_notify",
            "screenshot": False,
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "access denied" in data["error"]
        # 验证推送了 warning 通知（第一次是触发通知，第二次是失败警告）
        titles = [p["title"] for p in pushed]
        assert "关机命令执行失败" in titles


# ========== T04: /health 集成 ==========

class TestHealthIntegration:
    def test_health_contains_auto_shutdown(self, client):
        """/health 响应含 auto_shutdown 字段"""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "auto_shutdown" in data
        as_data = data["auto_shutdown"]
        assert "trigger_count" in as_data
        assert isinstance(as_data["trigger_count"], int)
