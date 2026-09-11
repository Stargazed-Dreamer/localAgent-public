"""lib.approval_router 纯路由层单元测试。

覆盖：
- heartbeat：心跳阈值边界（注入时钟）、面板在线/离线判断
- store：队列 FIFO、ApprovalItem 状态流转、activity 重置 deadline
- router：路由决策（面板在线→入队 / 离线→fallback）、await_decision 超时 / activity 重置 / notify_decision 唤醒
"""

import asyncio

import pytest

from lib.approval_router import heartbeat, router, store
from lib.approval_router.store import ApprovalItem


@pytest.fixture(autouse=True)
def reset_state():
    """每个测试前重置所有模块状态。"""
    router.reset()
    heartbeat.reset_clock()
    yield
    router.reset()
    heartbeat.reset_clock()


# ========== heartbeat 测试 ==========


class TestHeartbeat:
    def test_no_heartbeat_means_offline(self):
        assert heartbeat.is_panel_online() is False
        assert heartbeat.is_panel_online(threshold=1000) is False

    def test_within_threshold_is_online(self):
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        heartbeat.update_heartbeat("1.0")
        t[0] = 129.0  # 29s 后
        assert heartbeat.is_panel_online() is True

    def test_beyond_threshold_is_offline(self):
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        heartbeat.update_heartbeat("1.0")
        t[0] = 131.0  # 31s 后
        assert heartbeat.is_panel_online() is False

    def test_threshold_boundary_exclusive(self):
        """刚好等于阈值时判定离线（< threshold 才在线）。"""
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        heartbeat.update_heartbeat()
        t[0] = 130.0  # 刚好 30s
        assert heartbeat.is_panel_online() is False

    def test_get_status(self):
        t = [200.0]
        heartbeat.set_clock(lambda: t[0])
        heartbeat.update_heartbeat("2.0")
        status = heartbeat.get_status()
        assert status["panel_online"] is True
        assert status["last_heartbeat"] == 200.0
        assert status["panel_version"] == "2.0"

    def test_reset(self):
        heartbeat.update_heartbeat("1.0")
        assert heartbeat.is_panel_online() is True
        heartbeat.reset()
        assert heartbeat.is_panel_online() is False
        assert heartbeat.get_status()["panel_version"] == ""


# ========== store 测试 ==========


class TestStore:
    def _make_item(self, approval_id="test_001", created_at=100.0):
        return ApprovalItem(
            approval_id=approval_id,
            item_type="shell",
            payload={"type": "shell", "command": "rm -rf /tmp/test"},
            created_at=created_at,
            base_timeout=180.0,
        )

    def test_add_and_get(self):
        item = self._make_item()
        store.add(item)
        got = store.get("test_001")
        assert got is not None
        assert got.approval_id == "test_001"
        assert got.status == "pending"

    def test_get_nonexistent_returns_none(self):
        assert store.get("nonexistent") is None

    def test_remove(self):
        item = self._make_item()
        store.add(item)
        assert store.remove("test_001") is True
        assert store.get("test_001") is None
        assert store.remove("test_001") is False

    def test_list_pending_fifo_order(self):
        for i, t in enumerate([100.0, 200.0, 300.0]):
            store.add(self._make_item(f"id_{i}", created_at=t))
        pending = store.list_pending()
        assert [p.approval_id for p in pending] == ["id_0", "id_1", "id_2"]

    def test_list_pending_excludes_decided(self):
        store.add(self._make_item("pending_1"))
        item2 = self._make_item("decided_1")
        store.add(item2)
        store.set_decision("decided_1", "approve", "ok", "token_123")
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0].approval_id == "pending_1"

    def test_list_all_includes_timeout(self):
        store.add(self._make_item("pending_1"))
        item2 = self._make_item("timeout_1")
        store.add(item2)
        store.mark_timeout("timeout_1")
        all_items = store.list_all()
        assert len(all_items) == 2

    def test_mark_timeout(self):
        item = self._make_item()
        store.add(item)
        assert store.mark_timeout("test_001") is True
        got = store.get("test_001")
        assert got.status == "timeout"
        assert got.decision == "timeout"
        # 重复标记返 False
        assert store.mark_timeout("test_001") is False

    def test_mark_timeout_nonexistent(self):
        assert store.mark_timeout("nonexistent") is False

    def test_record_activity_resets_deadline(self):
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        item = self._make_item(created_at=100.0)
        store.add(item)
        original_expires = item.expires_at
        t[0] = 200.0  # 推进 100s
        assert store.record_activity("test_001") is True
        new_expires = store.get("test_001").expires_at
        assert new_expires > original_expires
        assert new_expires == 200.0 + 180.0

    def test_record_activity_nonexistent(self):
        assert store.record_activity("nonexistent") is False

    def test_record_activity_on_decided_returns_false(self):
        item = self._make_item()
        store.add(item)
        store.set_decision("test_001", "deny", "no")
        assert store.record_activity("test_001") is False

    def test_set_decision(self):
        item = self._make_item()
        store.add(item)
        assert store.set_decision("test_001", "approve", "允许", "token_abc") is True
        got = store.get("test_001")
        assert got.status == "decided"
        assert got.decision == "approve"
        assert got.feedback == "允许"
        assert got.approval_token == "token_abc"

    def test_set_decision_on_non_pending_returns_false(self):
        item = self._make_item()
        store.add(item)
        store.mark_timeout("test_001")
        assert store.set_decision("test_001", "approve", "") is False

    def test_ack_timeout(self):
        item = self._make_item()
        store.add(item)
        store.mark_timeout("test_001")
        assert store.ack_timeout("test_001") is True
        assert store.get("test_001").status == "acked"
        # 重复 ack 返 False
        assert store.ack_timeout("test_001") is False

    def test_ack_timeout_on_pending_returns_false(self):
        item = self._make_item()
        store.add(item)
        assert store.ack_timeout("test_001") is False

    def test_pending_count(self):
        assert store.pending_count() == 0
        store.add(self._make_item("a"))
        store.add(self._make_item("b"))
        assert store.pending_count() == 2
        store.set_decision("a", "approve", "")
        assert store.pending_count() == 1

    def test_to_pending_dict(self):
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        item = ApprovalItem(
            approval_id="http_001",
            item_type="http",
            payload={
                "type": "http",
                "method": "POST",
                "path": "/shutdown",
                "body_preview": '{"force": true}',
                "guard_reason": "破坏性操作",
                "agent_reason": "用户要求关机",
                "llm_opinion": "高风险",
            },
            created_at=100.0,
            base_timeout=200.0,
        )
        store.add(item)
        t[0] = 150.0  # 50s 后
        d = store.get("http_001").to_pending_dict()
        assert d["approval_id"] == "http_001"
        assert d["type"] == "http"
        assert d["method"] == "POST"
        assert d["path"] == "/shutdown"
        assert d["body_preview"] == '{"force": true}'
        assert d["guard_reason"] == "破坏性操作"
        assert d["agent_reason"] == "用户要求关机"
        assert d["llm_opinion"] == "高风险"
        assert d["created_at"] == 100.0
        assert d["status"] == "pending"
        # seconds_left = (100 + 200) - 150 = 150
        assert d["seconds_left"] == 150

    def test_expires_at_uses_last_activity(self):
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])
        item = self._make_item(created_at=100.0)
        store.add(item)
        assert item.expires_at == 280.0  # 100 + 180
        t[0] = 150.0
        store.record_activity("test_001")
        assert store.get("test_001").expires_at == 330.0  # 150 + 180


# ========== router 测试 ==========


class TestRouteApproval:
    def test_offline_returns_fallback(self):
        # 无心跳 → 离线
        result = router.route_approval("id_1", {"type": "shell"}, 200.0)
        assert result == {"route": "fallback"}

    def test_online_returns_panel(self):
        heartbeat.update_heartbeat("1.0")
        result = router.route_approval("id_1", {"type": "shell", "command": "ls"}, 200.0)
        assert result["route"] == "panel"
        assert result["approval_id"] == "id_1"
        # 验证入队
        item = store.get("id_1")
        assert item is not None
        assert item.item_type == "shell"
        assert item.payload["command"] == "ls"
        assert item.base_timeout == 200.0

    def test_online_with_http_payload(self):
        heartbeat.update_heartbeat("1.0")
        payload = {
            "type": "http",
            "method": "POST",
            "path": "/shutdown",
            "body_preview": "{}",
        }
        result = router.route_approval("http_001", payload, 200.0)
        assert result["route"] == "panel"
        item = store.get("http_001")
        assert item.item_type == "http"


class TestAwaitDecision:
    def test_nonexistent_returns_timeout(self):
        result = asyncio.run(router.await_decision("nonexistent", 1.0))
        assert result["decision"] == "timeout"
        assert result["approval_token"] == ""

    def test_timeout_when_no_decision(self):
        """无决策时超时返 timeout。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 0.3)  # 0.3s 超时
        result = asyncio.run(router.await_decision("id_1", 0.3))
        assert result["decision"] == "timeout"
        # 验证 store 中标记为 timeout
        item = store.get("id_1")
        assert item.status == "timeout"

    def test_notify_decision_wakes_await(self):
        """notify_decision 唤醒等待中的 await_decision。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 5.0)

        async def _run():
            task = asyncio.create_task(router.await_decision("id_1", 5.0))
            await asyncio.sleep(0.1)  # 等待 await_decision 开始
            router.notify_decision("id_1", "approve", "允许", "token_xyz")
            result = await task
            return result

        result = asyncio.run(_run())
        assert result["decision"] == "approve"
        assert result["feedback"] == "允许"
        assert result["approval_token"] == "token_xyz"

    def test_activity_resets_deadline(self):
        """activity 信号重置 deadline，避免超时。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 0.3)  # 0.3s 超时

        async def _run():
            task = asyncio.create_task(router.await_decision("id_1", 0.3))
            await asyncio.sleep(0.15)  # 过了一半时间
            # 用户开始打字，重置 deadline
            store.record_activity("id_1")
            await asyncio.sleep(0.2)  # 超过原 0.3s deadline
            # 任务应仍在运行（deadline 被重置）
            assert not task.done()
            # 用户决策
            router.notify_decision("id_1", "deny", "拒绝")
            result = await task
            return result

        result = asyncio.run(_run())
        assert result["decision"] == "deny"
        assert result["feedback"] == "拒绝"

    def test_activity_multiple_resets(self):
        """多次 activity 重置，最终超时。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 0.3)

        async def _run():
            task = asyncio.create_task(router.await_decision("id_1", 0.3))
            await asyncio.sleep(0.15)
            store.record_activity("id_1")  # 第一次重置
            await asyncio.sleep(0.15)
            store.record_activity("id_1")  # 第二次重置
            await asyncio.sleep(0.15)
            store.record_activity("id_1")  # 第三次重置
            await asyncio.sleep(0.15)
            # 仍未超时（每次重置后 0.3s）
            assert not task.done()
            # 不再重置，等超时
            result = await asyncio.wait_for(task, timeout=0.5)
            return result

        result = asyncio.run(_run())
        assert result["decision"] == "timeout"

    def test_notify_timeout_wakes_await(self):
        """面板端倒计时归零时 notify_timeout 唤醒 await。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 5.0)

        async def _run():
            task = asyncio.create_task(router.await_decision("id_1", 5.0))
            await asyncio.sleep(0.1)
            router.notify_timeout("id_1")
            result = await task
            return result

        result = asyncio.run(_run())
        assert result["decision"] == "timeout"
        assert store.get("id_1").status == "timeout"

    def test_notify_decision_on_nonexistent_returns_false(self):
        assert router.notify_decision("nonexistent", "approve", "") is False

    def test_notify_decision_on_timeout_returns_false(self):
        """已超时的审批不能再决策。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 5.0)
        store.mark_timeout("id_1")
        assert router.notify_decision("id_1", "approve", "") is False


class TestIntegrationFlow:
    """端到端流程：路由 → 等待 → 决策 → 移除。"""

    def test_approve_flow(self):
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell", "command": "ls"}, 5.0)

        async def _run():
            task = asyncio.create_task(router.await_decision("id_1", 5.0))
            await asyncio.sleep(0.05)
            router.notify_decision("id_1", "approve", "允许", "token_123")
            return await task

        result = asyncio.run(_run())
        assert result["decision"] == "approve"
        # 用户决策后移除
        store.remove("id_1")
        assert store.get("id_1") is None

    def test_timeout_then_ack_flow(self):
        """超时 → 卡片留存 → 用户点"收到" → 移除。"""
        heartbeat.update_heartbeat("1.0")
        router.route_approval("id_1", {"type": "shell"}, 0.2)

        result = asyncio.run(router.await_decision("id_1", 0.2))
        assert result["decision"] == "timeout"

        # 超时后卡片仍在队列（只读留存）
        item = store.get("id_1")
        assert item is not None
        assert item.status == "timeout"

        # 不能再决策
        assert router.notify_decision("id_1", "approve", "") is False

        # 用户点"收到"
        assert store.ack_timeout("id_1") is True
        assert store.get("id_1").status == "acked"

        # 移除
        store.remove("id_1")
        assert store.get("id_1") is None

    def test_multiple_pending_fifo(self):
        """多个审批同时存在，按 FIFO 顺序。"""
        heartbeat.update_heartbeat("1.0")
        t = [100.0]
        heartbeat.set_clock(lambda: t[0])

        for i in range(3):
            t[0] = 100.0 + i * 0.1
            router.route_approval(f"id_{i}", {"type": "shell"}, 5.0)

        pending = store.list_pending()
        assert [p.approval_id for p in pending] == ["id_0", "id_1", "id_2"]
