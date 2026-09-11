"""command_guard 核心逻辑 + REST 端点测试（原 command_guard_router 已合并）

覆盖：
- 核心逻辑：check_command（dcg禁用/token放行/不可用fail-closed/fail-open）、
  _fingerprint、_cleanup、get_pending、record_user_decision、find_dcg、consume_token
- REST 端点：GET /command-guard/status、POST /command-guard/decision、
  POST /command-guard/request-approval
"""

import asyncio
import time

import pytest

from server import command_guard
from server.command_guard import GuardDecision, check_command, consume_token
from server.exec import CmdRequest, exec_cmd


@pytest.fixture(autouse=True)
def clear_guard_state(monkeypatch):
    command_guard._pending.clear()
    command_guard._tokens.clear()
    command_guard._invalidate_dcg_cache()
    # 危险操作测试铁律兜底：默认 mock asyncio.create_subprocess_exec，防止新测试
    # 忘 mock 真启动 dcg / command_approval_gui 子进程（铁律要求 autouse 默认 mock 危险 API）。
    # 需真调子进程的测试可在测试内 monkeypatch.setattr(asyncio, "create_subprocess_exec", ...)
    # 覆盖本 fake（同一 monkeypatch 实例，后注册覆盖先注册）。
    async def _safe_create_subprocess_exec(*args, **kwargs):
        class _SafeFakeProc:
            returncode = 0

            def kill(self):
                pass

            async def communicate(self, input=None):
                return (b"", b"")

            async def wait(self):
                return 0

        return _SafeFakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _safe_create_subprocess_exec)
    yield
    command_guard._pending.clear()
    command_guard._tokens.clear()
    command_guard._invalidate_dcg_cache()


@pytest.fixture
def cfg(monkeypatch):
    """提供可配置的 command_guard 配置"""
    config = {
        "enabled": True,
        "fail_closed": True,
        "approval_ttl_seconds": 300,
        "token_ttl_seconds": 120,
        "timeout_seconds": 3.0,
        "dcg_path": "",
    }
    monkeypatch.setattr(
        command_guard,
        "get_command_guard_config",
        lambda: config,
    )
    return config


# ==================== 核心逻辑：token + decision ====================

class TestApprovalToken:
    def test_token_bound_and_single_use(self, cfg, tmp_path):
        """token 绑定特定命令且一次性使用"""
        blocked = command_guard._blocked(
            "git reset --hard", "cmd", str(tmp_path), "danger", "core.git:reset-hard"
        )
        result = command_guard.record_user_decision(
            blocked.approval_id, "approve", "已确认只执行这一次"
        )
        token = result["approval_token"]

        # 不同命令不能用此 token
        assert not consume_token(token, "git clean -fdx", "cmd", str(tmp_path))

        # 正确命令能用一次
        blocked2 = command_guard._blocked(
            "git reset --hard", "cmd", str(tmp_path), "danger", "core.git:reset-hard"
        )
        token2 = command_guard.record_user_decision(blocked2.approval_id, "approve")["approval_token"]
        assert consume_token(token2, "git reset --hard", "cmd", str(tmp_path))
        # 第二次用不了
        assert not consume_token(token2, "git reset --hard", "cmd", str(tmp_path))

    def test_empty_token_rejected(self):
        """空 token 直接返回 False"""
        assert not consume_token("", "ls", "cmd", ".")

    def test_token_fingerprint_differs_by_cwd(self, cfg, tmp_path):
        """不同 cwd 产生不同 fingerprint，token 不互通"""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        # token 1: 绑定 dir_a，尝试在 dir_b 用 → 无效（consume_token 会 pop）
        blocked_a = command_guard._blocked("rm -rf .", "cmd", str(dir_a), "danger", "test")
        token_a = command_guard.record_user_decision(blocked_a.approval_id, "approve")["approval_token"]
        assert not consume_token(token_a, "rm -rf .", "cmd", str(dir_b))

        # token 2: 绑定 dir_a，在 dir_a 用 → 有效
        blocked_a2 = command_guard._blocked("rm -rf .", "cmd", str(dir_a), "danger", "test")
        token_a2 = command_guard.record_user_decision(blocked_a2.approval_id, "approve")["approval_token"]
        assert consume_token(token_a2, "rm -rf .", "cmd", str(dir_a))

    def test_denial_preserves_feedback(self, cfg, tmp_path):
        """拒绝时保留用户反馈"""
        blocked = command_guard._blocked("rd /s data", "cmd", str(tmp_path), "danger", "windows.filesystem")
        result = command_guard.record_user_decision(
            blocked.approval_id, "deny", "不要删除 data，请先生成清单"
        )
        assert result == {
            "approved": False,
            "decision": "deny",
            "feedback": "不要删除 data，请先生成清单",
        }


# ==================== 核心逻辑：check_command ====================

class TestCheckCommand:
    def test_disabled_config_allows_all(self, cfg, tmp_path):
        """enabled=False 时所有命令放行"""
        cfg["enabled"] = False
        decision = asyncio.run(check_command("rm -rf /", "cmd", str(tmp_path)))
        assert decision.allowed is True
        assert decision.blocked is False

    def test_valid_token_allows(self, cfg, tmp_path):
        """有效 token 放行"""
        blocked = command_guard._blocked("git reset", "cmd", str(tmp_path), "danger", "test")
        token = command_guard.record_user_decision(blocked.approval_id, "approve")["approval_token"]
        decision = asyncio.run(check_command("git reset", "cmd", str(tmp_path), approval_token=token))
        assert decision.allowed is True
        assert decision.blocked is False

    def test_dcg_unavailable_fail_closed(self, cfg, tmp_path, monkeypatch):
        """dcg 不可用 + fail_closed=True → blocked"""
        monkeypatch.setattr(command_guard, "find_dcg", lambda: None)
        cfg["fail_closed"] = True
        decision = asyncio.run(check_command("rm -rf data", "cmd", str(tmp_path)))
        assert decision.blocked is True
        assert decision.allowed is False
        assert "fail-closed" in decision.reason or "dcg" in decision.reason.lower()

    def test_dcg_unavailable_fail_open(self, cfg, tmp_path, monkeypatch):
        """dcg 不可用 + fail_closed=False → 放行但 unavailable=True"""
        monkeypatch.setattr(command_guard, "find_dcg", lambda: None)
        cfg["fail_closed"] = False
        decision = asyncio.run(check_command("ls", "cmd", str(tmp_path)))
        assert decision.allowed is True
        assert decision.unavailable is True

    def test_dcg_deny_returncode_1(self, cfg, tmp_path, monkeypatch):
        """dcg 返回 returncode=1 → blocked"""
        class FakeProc:
            returncode = 1
            async def communicate(self):
                return (b'{"decision":"deny","reason":"destructive","rule_id":"dcg:rm"}', b"")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        async def fake_wait_for(coro, timeout):
            return await coro

        monkeypatch.setattr(command_guard, "find_dcg", lambda: "/fake/dcg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        decision = asyncio.run(check_command("rm -rf /", "cmd", str(tmp_path)))
        assert decision.blocked is True
        assert decision.rule_id == "dcg:rm"

    def test_dcg_allow_returncode_0(self, cfg, tmp_path, monkeypatch):
        """dcg 返回 returncode=0 → 放行"""
        class FakeProc:
            returncode = 0
            async def communicate(self):
                return (b'{"decision":"allow"}', b"")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        async def fake_wait_for(coro, timeout):
            return await coro

        monkeypatch.setattr(command_guard, "find_dcg", lambda: "/fake/dcg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        decision = asyncio.run(check_command("ls", "cmd", str(tmp_path)))
        assert decision.allowed is True
        assert decision.blocked is False

    def test_dcg_timeout_fail_closed(self, cfg, tmp_path, monkeypatch):
        """dcg 超时 + fail_closed → blocked"""
        class FakeProc:
            returncode = -1
            def kill(self):
                pass
            async def communicate(self):
                return (b"", b"")

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        async def fake_wait_for(coro, timeout):
            raise TimeoutError()

        monkeypatch.setattr(command_guard, "find_dcg", lambda: "/fake/dcg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        cfg["fail_closed"] = True

        decision = asyncio.run(check_command("rm -rf /", "cmd", str(tmp_path)))
        assert decision.blocked is True
        assert "超时" in decision.reason or "timeout" in decision.reason.lower()


# ==================== 核心逻辑：辅助函数 ====================

class TestFindDcg:
    def test_returns_none_when_not_found(self, cfg, monkeypatch, tmp_path):
        """dcg 不存在时返回 None"""
        cfg["dcg_path"] = ""
        monkeypatch.setattr(command_guard.os.environ, "get", lambda k, d="": d if k == "DCG_EXECUTABLE" else "")
        monkeypatch.setattr(command_guard.shutil, "which", lambda _: None)
        # mock Path.home() 避免 ~/.local/bin/dcg.exe 干扰
        monkeypatch.setattr(command_guard.Path, "home", classmethod(lambda cls: tmp_path))
        assert command_guard.find_dcg() is None

    def test_returns_path_from_config(self, cfg, tmp_path, monkeypatch):
        """从 config dcg_path 找到存在的文件"""
        fake_dcg = tmp_path / "dcg.exe"
        fake_dcg.write_text("fake")
        cfg["dcg_path"] = str(fake_dcg)
        monkeypatch.setattr(command_guard.shutil, "which", lambda _: None)
        result = command_guard.find_dcg()
        assert result is not None
        assert "dcg.exe" in result


class TestGetPending:
    def test_returns_pending_dict(self, cfg, tmp_path):
        """get_pending 返回待审批信息"""
        blocked = command_guard._blocked("rm -rf .", "cmd", str(tmp_path), "danger", "test")
        pending = command_guard.get_pending(blocked.approval_id)
        assert pending["command"] == "rm -rf ."
        assert pending["reason"] == "danger"

    def test_raises_for_unknown_id(self, cfg):
        """未知 approval_id 抛 ValueError"""
        with pytest.raises(ValueError, match="不存在或已过期"):
            command_guard.get_pending("approval_unknown")


class TestRecordUserDecision:
    def test_raises_for_unknown_id(self, cfg):
        """未知 approval_id 抛 ValueError"""
        with pytest.raises(ValueError, match="不存在或已过期"):
            command_guard.record_user_decision("approval_unknown", "approve")

    def test_approve_issues_token(self, cfg, tmp_path):
        """approve 签发 token"""
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "danger", "test")
        result = command_guard.record_user_decision(blocked.approval_id, "approve", "ok")
        assert result["approved"] is True
        assert "approval_token" in result
        assert result["expires_in_seconds"] == cfg["token_ttl_seconds"]

    def test_deny_no_token(self, cfg, tmp_path):
        """deny 不签发 token"""
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "danger", "test")
        result = command_guard.record_user_decision(blocked.approval_id, "deny", "no")
        assert result["approved"] is False
        assert "approval_token" not in result


class TestCleanup:
    def test_cleanup_removes_expired(self, cfg, tmp_path):
        """_cleanup 清除过期的 pending 和 token"""
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "danger", "test")
        # 手动设置过期
        command_guard._pending[blocked.approval_id]["expires_at"] = time.time() - 1
        command_guard._cleanup()
        assert blocked.approval_id not in command_guard._pending


# ==================== exec_cmd 集成 ====================

class TestExecCmdBlocked:
    def test_returns_structured_block_without_spawning(self, monkeypatch, tmp_path):
        """blocked 时不启动进程，返回结构化阻止响应"""
        async def fake_check(command, shell, cwd, approval_token=""):
            return GuardDecision(
                allowed=False,
                blocked=True,
                reason="blocked for test",
                rule_id="test:danger",
                approval_id="approval_test",
            )

        monkeypatch.setattr("server.exec.check_command", fake_check)
        response = asyncio.run(exec_cmd(CmdRequest(cmd="git reset --hard", cwd=str(tmp_path))))

        assert response.blocked is True
        assert response.exit_code == 126
        assert response.approval_id == "approval_test"
        assert "不要尝试" in response.agent_instruction


# ==================== REST 端点：command_guard（原 command_guard_router 合并） ====================

class TestCommandGuardStatusEndpoint:
    def test_status_returns_fields(self, client, monkeypatch):
        """GET /command-guard/status 返回 enabled + dcg_path + agent_instruction

        Mock command_guard.get_command_guard_config 锁定 enabled=True，
        避免本地 config.toml 的 enabled=false 导致测试不稳定。本测试验证端点字段形状，
        而非 config.toml 的真实值。
        """
        from server import command_guard
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {
                "enabled": True,
                "approval_level": "strict",
                "dcg_path": "",
                "fail_closed": True,
                "timeout_seconds": 3.0,
                "approval_ttl_seconds": 300,
                "token_ttl_seconds": 120,
                "gui_timeout_seconds": 180,
                "llm_review_enabled": True,
                "llm_review_timeout": 5,
                "llm_review_endpoints": ["exec_python"],
                "detailed_audit_log": False,
            },
        )
        resp = client.get("/command-guard/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert "dcg_path" in data
        assert "agent_instruction" in data
        assert isinstance(data["agent_instruction"], str)
        assert len(data["agent_instruction"]) > 10


class TestCommandGuardDecisionEndpoint:
    def test_decision_approve_issues_token(self, client, monkeypatch, tmp_path):
        """POST /command-guard/decision approve → 签发 token"""
        # 先创建一个 pending
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")

        resp = client.post("/command-guard/decision", json={
            "approval_id": blocked.approval_id,
            "decision": "approve",
            "feedback": "test approve",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] is True
        assert "approval_token" in data

    def test_decision_deny(self, client, monkeypatch, tmp_path):
        """POST /command-guard/decision deny → approved=False"""
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")

        resp = client.post("/command-guard/decision", json={
            "approval_id": blocked.approval_id,
            "decision": "deny",
            "feedback": "no way",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] is False
        assert data["feedback"] == "no way"

    def test_decision_unknown_approval_404(self, client):
        """未知 approval_id → 404"""
        resp = client.post("/command-guard/decision", json={
            "approval_id": "approval_nonexistent",
            "decision": "approve",
            "feedback": "",
        })
        assert resp.status_code == 404

    def test_decision_invalid_decision_value(self, client, monkeypatch, tmp_path):
        """decision 值不是 approve/deny → 422 验证错误"""
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")
        resp = client.post("/command-guard/decision", json={
            "approval_id": blocked.approval_id,
            "decision": "maybe",
            "feedback": "",
        })
        assert resp.status_code == 422

    def test_decision_missing_approval_id(self, client):
        """缺少 approval_id → 422"""
        resp = client.post("/command-guard/decision", json={
            "decision": "approve",
        })
        assert resp.status_code == 422


class TestCommandGuardRequestApprovalEndpoint:
    def test_request_approval_unknown_id_404(self, client):
        """未知 approval_id → 404"""
        resp = client.post("/command-guard/request-approval", json={
            "approval_id": "approval_nonexistent",
            "agent_reason": "test reason",
        })
        assert resp.status_code == 404

    def test_request_approval_missing_agent_reason(self, client):
        """缺少 agent_reason → 422（min_length=1）"""
        resp = client.post("/command-guard/request-approval", json={
            "approval_id": "approval_test",
            "agent_reason": "",
        })
        assert resp.status_code == 422

    def test_request_approval_starts_subprocess(self, client, monkeypatch, tmp_path):
        """request-approval 启动子进程并返回决定"""
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")

        # mock 子进程返回 approve
        class FakeProc:
            returncode = 0
            async def communicate(self, input_data):
                return (b'{"decision": "approve", "feedback": "gui approved"}', b"")

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        resp = client.post("/command-guard/request-approval", json={
            "approval_id": blocked.approval_id,
            "agent_reason": "需要执行 ls 查看目录内容",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] is True
        assert "approval_token" in data

    def test_request_approval_subprocess_deny(self, client, monkeypatch, tmp_path):
        """子进程返回 deny → approved=False"""
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")

        class FakeProc:
            returncode = 0
            async def communicate(self, input_data):
                return (b'{"decision": "deny", "feedback": "user denied"}', b"")

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        resp = client.post("/command-guard/request-approval", json={
            "approval_id": blocked.approval_id,
            "agent_reason": "需要执行 ls",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] is False
        assert data["feedback"] == "user denied"

    def test_request_approval_subprocess_failure(self, client, monkeypatch, tmp_path):
        """子进程返回非0退出码 → 500"""
        monkeypatch.setattr(
            command_guard,
            "get_command_guard_config",
            lambda: {"approval_ttl_seconds": 300, "token_ttl_seconds": 120},
        )
        blocked = command_guard._blocked("ls", "cmd", str(tmp_path), "test", "test:rule")

        class FakeProc:
            returncode = 1
            async def communicate(self, input_data):
                return (b"", b"GUI crashed")

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        resp = client.post("/command-guard/request-approval", json={
            "approval_id": blocked.approval_id,
            "agent_reason": "需要执行 ls",
        })
        assert resp.status_code == 500
