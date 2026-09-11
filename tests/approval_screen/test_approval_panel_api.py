"""server.approval_panel_router 的 HTTP 端点测试。

覆盖：
- GET  /approvals/status           → 心跳快照 + pending 计数
- GET  /approvals/pending           → pending 队列
- POST /approvals/heartbeat         → 面板心跳上报
- POST /approvals/{id}/activity     → 用户操作重置 deadline
- POST /approvals/{id}/decision     → 用户决策（approve/deny）
- POST /approvals/{id}/ack          → 用户对已超时卡片点"收到"

用最小化 FastAPI app（只挂 approval_panel_router）避免拉起完整 server.main。
lib 层逻辑由 test_approval_router.py 覆盖，本文件只测 HTTP 桥接。
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.approval_router import heartbeat, store
from lib.approval_router import router as approval_router
from lib.approval_router.store import ApprovalItem
from server.approval_panel_router import router as approval_panel_router


@pytest.fixture
def app():
    """最小化 FastAPI app，只挂 approval_panel_router。"""
    app = FastAPI()
    app.include_router(approval_panel_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    """每个测试前重置 lib.approval_router 全部状态。"""
    approval_router.reset()
    heartbeat.reset_clock()
    yield
    approval_router.reset()
    heartbeat.reset_clock()


@pytest.fixture
def online_panel():
    """让面板处于在线状态。"""
    heartbeat.update_heartbeat("test_1.0")
    return heartbeat.is_panel_online()


@pytest.fixture
def pending_item(online_panel):
    """入队一个 pending 审批项。"""
    item = ApprovalItem(
        approval_id="test_001",
        item_type="shell",
        payload={
            "type": "shell",
            "command": "rm -rf /tmp/test",
            "guard_reason": "destructive",
            "agent_reason": "test reason",
        },
        created_at=heartbeat.now(),
        base_timeout=200.0,
    )
    store.add(item)
    return item


@pytest.fixture
def timeout_item(online_panel):
    """入队一个已超时的审批项。"""
    item = ApprovalItem(
        approval_id="test_timeout",
        item_type="shell",
        payload={"type": "shell", "command": "rm -rf /tmp/x"},
        created_at=heartbeat.now(),
        base_timeout=200.0,
    )
    store.add(item)
    store.mark_timeout(item.approval_id)
    return item


# ========== GET /approvals/status ==========


class TestStatus:
    def test_status_no_heartbeat(self, client):
        """无心跳 → panel_online=False, pending_count=0。"""
        resp = client.get("/approvals/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["panel_online"] is False
        assert data["pending_count"] == 0
        assert data["last_heartbeat"] == ""

    def test_status_online(self, client, online_panel, pending_item):
        """有心跳 + 有 pending → panel_online=True, pending_count=1。"""
        resp = client.get("/approvals/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["panel_online"] is True
        assert data["pending_count"] == 1
        assert data["panel_version"] == "test_1.0"
        assert data["last_heartbeat"]  # 非空字符串


# ========== GET /approvals/pending ==========


class TestPending:
    def test_empty_pending(self, client):
        """无 pending → count=0。"""
        resp = client.get("/approvals/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["pending"] == []

    def test_list_pending(self, client, pending_item):
        """有 pending → 返回列表含完整字段。"""
        resp = client.get("/approvals/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        item = data["pending"][0]
        assert item["approval_id"] == "test_001"
        assert item["type"] == "shell"
        assert item["command"] == "rm -rf /tmp/test"
        assert item["guard_reason"] == "destructive"
        assert item["agent_reason"] == "test reason"
        assert "created_at" in item
        assert "expires_at" in item
        assert "seconds_left" in item
        assert item["status"] == "pending"

    def test_pending_excludes_timeout(self, client, timeout_item):
        """timeout 状态的项不出现在 pending 列表中。"""
        # 加一个 pending 项
        store.add(ApprovalItem(
            approval_id="test_pending",
            item_type="shell",
            payload={"type": "shell", "command": "ls"},
            created_at=heartbeat.now(),
            base_timeout=200.0,
        ))
        resp = client.get("/approvals/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["pending"][0]["approval_id"] == "test_pending"


# ========== POST /approvals/heartbeat ==========


class TestHeartbeat:
    def test_heartbeat_sets_online(self, client):
        """POST heartbeat 后面板变在线。"""
        assert heartbeat.is_panel_online() is False
        resp = client.post("/approvals/heartbeat", json={"panel_version": "1.2.3"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert heartbeat.is_panel_online() is True
        assert heartbeat.get_status()["panel_version"] == "1.2.3"

    def test_heartbeat_empty_version(self, client):
        """空 version 也能心跳（不更新 version）。"""
        resp = client.post("/approvals/heartbeat", json={})
        assert resp.status_code == 200
        assert heartbeat.is_panel_online() is True


# ========== POST /approvals/{id}/activity ==========


class TestActivity:
    def test_activity_resets_deadline(self, client, pending_item):
        """activity 重置该请求的 deadline。"""
        # 记录原 last_activity
        item_before = store.get("test_001")
        original_activity = item_before.last_activity

        # 推进时间
        t = [heartbeat.now()]
        heartbeat.set_clock(lambda: t[0])
        t[0] += 50.0  # 推进 50s

        resp = client.post("/approvals/test_001/activity")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # last_activity 应被更新为当前时钟
        item_after = store.get("test_001")
        assert item_after.last_activity > original_activity

    def test_activity_nonexistent(self, client):
        """不存在的 approval_id → 404。"""
        resp = client.post("/approvals/nonexistent/activity")
        assert resp.status_code == 404

    def test_activity_on_timeout_item_fails(self, client, timeout_item):
        """对已超时项 activity → 404（record_activity 仅对 pending 有效）。"""
        resp = client.post("/approvals/test_timeout/activity")
        assert resp.status_code == 404


# ========== POST /approvals/{id}/decision ==========


class TestDecision:
    def test_decision_nonexistent(self, client):
        """不存在的 approval_id → 404。"""
        resp = client.post(
            "/approvals/nonexistent/decision",
            json={"decision": "approve", "feedback": ""},
        )
        assert resp.status_code == 404

    def test_decision_invalid_decision(self, client, pending_item):
        """非法 decision 值 → 422。"""
        resp = client.post(
            "/approvals/test_001/decision",
            json={"decision": "maybe", "feedback": ""},
        )
        assert resp.status_code == 422

    def test_decision_timeout_marks_timeout(self, client, pending_item, monkeypatch):
        """decision=timeout 标记卡片为 timeout 状态（而非 decided）。"""
        monkeypatch.setattr(
            "server.approval_panel_router._router.notify_timeout",
            lambda aid: store.mark_timeout(aid),
        )
        resp = client.post(
            "/approvals/test_001/decision",
            json={"decision": "timeout", "feedback": ""},
        )
        assert resp.status_code == 200
        item = store.get("test_001")
        assert item.status == "timeout"
        assert item.decision == "timeout"

    def test_decision_calls_notify(self, client, pending_item, monkeypatch):
        """decision 端点调 notify_decision 唤醒等待中的 await_decision。"""
        called = {"notified": False}

        def fake_notify(approval_id, decision, feedback, token=""):
            called["notified"] = True
            called["approval_id"] = approval_id
            called["decision"] = decision
            called["feedback"] = feedback
            # 真实调 store.set_decision，让 store 状态正确
            return store.set_decision(approval_id, decision, feedback, token)

        monkeypatch.setattr("server.approval_panel_router._router.notify_decision", fake_notify)

        resp = client.post(
            "/approvals/test_001/decision",
            json={"decision": "approve", "feedback": "ok"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert called["notified"] is True
        assert called["approval_id"] == "test_001"
        assert called["decision"] == "approve"
        assert called["feedback"] == "ok"

    def test_decision_on_timeout_item_fails(self, client, timeout_item):
        """对已超时项 decision → 409（状态不可重置）。"""
        resp = client.post(
            "/approvals/test_timeout/decision",
            json={"decision": "approve", "feedback": ""},
        )
        assert resp.status_code == 409

    def test_decision_denial(self, client, pending_item, monkeypatch):
        """deny 决策也能提交。"""
        monkeypatch.setattr(
            "server.approval_panel_router._router.notify_decision",
            lambda aid, d, f, t="": store.set_decision(aid, d, f, t),
        )
        resp = client.post(
            "/approvals/test_001/decision",
            json={"decision": "deny", "feedback": "不要执行"},
        )
        assert resp.status_code == 200
        item = store.get("test_001")
        assert item.status == "decided"
        assert item.decision == "deny"
        assert item.feedback == "不要执行"


# ========== POST /approvals/{id}/ack ==========


class TestAck:
    def test_ack_timeout_item(self, client, timeout_item):
        """对已超时项 ack → 200，项被移除。"""
        assert store.get("test_timeout") is not None
        resp = client.post("/approvals/test_timeout/ack")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert store.get("test_timeout") is None

    def test_ack_nonexistent(self, client):
        """不存在的 approval_id → 404。"""
        resp = client.post("/approvals/nonexistent/ack")
        assert resp.status_code == 404

    def test_ack_pending_item_fails(self, client, pending_item):
        """对 pending 项 ack → 409（仅 timeout 状态可 ack）。"""
        resp = client.post("/approvals/test_001/ack")
        assert resp.status_code == 409

    def test_ack_decided_item_fails(self, client, pending_item, monkeypatch):
        """对 decided 项 ack → 409。"""
        monkeypatch.setattr(
            "server.approval_panel_router._router.notify_decision",
            lambda aid, d, f, t="": store.set_decision(aid, d, f, t),
        )
        client.post(
            "/approvals/test_001/decision",
            json={"decision": "approve", "feedback": ""},
        )
        resp = client.post("/approvals/test_001/ack")
        assert resp.status_code == 409


# ========== 集成：run_gui_dialog 路由决策 ==========


class TestRunGuiDialogRouting:
    """验证 command_guard.run_gui_dialog 在面板在线/离线时的路由决策。"""

    def test_panel_offline_falls_back_to_subprocess(self, monkeypatch):
        """面板离线 → run_gui_dialog 走 subprocess 路径。"""
        from server import command_guard

        # 确保面板离线
        assert heartbeat.is_panel_online() is False

        # mock subprocess 调用
        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):
                return (b'{"decision":"deny","feedback":"no"}', b"")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        # mock config
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"gui_timeout_seconds": 180},
        )

        result = asyncio.run(command_guard.run_gui_dialog(
            {"type": "shell", "command": "ls"},
            approval_id="test_002",
        ))
        assert result["decision"] == "deny"
        assert result["feedback"] == "no"

    def test_panel_online_routes_to_panel(self, online_panel, monkeypatch):
        """面板在线 → run_gui_dialog 走面板路径（route_approval + await_decision）。"""
        from server import command_guard

        # mock await_decision 返回 approve
        async def fake_await(approval_id, base_timeout, max_total_timeout=86400.0):
            return {"decision": "approve", "feedback": "ok", "approval_token": "tok"}

        monkeypatch.setattr(
            "lib.approval_router.router.await_decision",
            fake_await,
        )
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"gui_timeout_seconds": 180},
        )

        # 不 mock subprocess，若错误走到 subprocess 路径会因 create_subprocess_exec 失败
        result = asyncio.run(command_guard.run_gui_dialog(
            {"type": "shell", "command": "ls"},
            approval_id="test_003",
        ))
        assert result["decision"] == "approve"
        assert result["feedback"] == "ok"
        # 验证确实入队了
        item = store.get("test_003")
        assert item is not None
        assert item.item_type == "shell"

    def test_panel_online_timeout_returns_timeout(self, online_panel, monkeypatch):
        """面板在线 + 超时 → 返回 decision=timeout（不抛异常）。"""
        from server import command_guard

        async def fake_await(approval_id, base_timeout, max_total_timeout=86400.0):
            return {"decision": "timeout", "feedback": "", "approval_token": ""}

        monkeypatch.setattr(
            "lib.approval_router.router.await_decision",
            fake_await,
        )
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"gui_timeout_seconds": 180},
        )

        result = asyncio.run(command_guard.run_gui_dialog(
            {"type": "shell", "command": "ls"},
            approval_id="test_004",
        ))
        assert result["decision"] == "timeout"

    def test_no_approval_id_falls_back(self, monkeypatch):
        """无 approval_id → 直接走 subprocess（向后兼容）。"""
        from server import command_guard

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):
                return (b'{"decision":"approve","feedback":""}', b"")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"gui_timeout_seconds": 180},
        )

        # 即使面板在线，无 approval_id 也走 subprocess
        heartbeat.update_heartbeat("1.0")
        result = asyncio.run(command_guard.run_gui_dialog(
            {"type": "shell", "command": "ls"},
        ))
        assert result["decision"] == "approve"
