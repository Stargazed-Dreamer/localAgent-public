"""MCP 网关审批 token 指纹一致性回归测试

背景（2026-08-25 修复的 P0 bug）：
- 旧实现：人审路径 user_review_for_llm_deny 用 args_body（含 _approval_token
  前的完整 arguments 序列化）签发 token；重试验证 check_approval 却固定用 b""。
- 后果：指纹必然不匹配 → 人审批准后 token 永远无效 → 审批死循环。

修复：签发与验证统一使用 _approval_body(arguments)——剥离 _approval_token 后
紧凑 JSON 序列化。本测试锁住该行为：
1. _approval_body 确定性、剥离内部字段
2. 模拟网关流程：create_pending(approval_body) → approve 签发 token →
   用同一 approval_body 验证必须通过
3. 旧 bug 回归：若验证侧退回 b""，token 必须失败（证明测试有效）
"""

import json

import pytest

from server import http_guard
from server.core.mcp_gateway import _approval_body
from server.http_guard import check_approval, create_pending, record_http_decision


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    """隔离 command_guard 配置 + 清理全局审批存储。"""
    config = {
        "enabled": True,
        "approval_ttl_seconds": 300,
        "token_ttl_seconds": 120,
    }
    monkeypatch.setattr(http_guard, "get_command_guard_config", lambda: config)
    http_guard._pending_http.clear()
    http_guard._tokens_http.clear()
    yield
    http_guard._pending_http.clear()
    http_guard._tokens_http.clear()


class TestApprovalBody:
    """_approval_body 测试。"""

    def test_deterministic(self):
        """同一 arguments 两次调用 → 完全相同的 bytes。"""
        args = {"params": {"task_id": "a.b"}, "tool": "loop_run_task"}
        assert _approval_body(args) == _approval_body(args)

    def test_strips_approval_token(self):
        """剥离 _approval_token：带/不带 token 的 body 一致。"""
        base = {"tool": "exec_python", "code": "print(1)"}
        with_token = {**base, "_approval_token": "some-token"}
        assert _approval_body(with_token) == _approval_body(base)

    def test_non_dict_returns_empty(self):
        """非 dict arguments → 空 bytes。"""
        assert _approval_body(None) == b""
        assert _approval_body("string") == b""
        assert _approval_body([1, 2]) == b""

    def test_valid_json_compact(self):
        """输出是合法紧凑 JSON（ensure_ascii=False）。"""
        args = {"cmd": "echo 你好"}
        body = _approval_body(args)
        parsed = json.loads(body.decode("utf-8"))
        assert parsed == args
        assert b"\n" not in body  # 无缩进


class TestTokenFingerprintRoundTrip:
    """模拟 mcp_gateway 人审→重试全流程，锁定指纹一致行为。"""

    VIRTUAL_PATH = "/mcp/tool/exec_python"

    def _simulate_flow(self, arguments: dict) -> str:
        """按 mcp_gateway 的方式走一遍：签发 → 批准 → 返回 token。"""
        approval_body = _approval_body(arguments)
        approval_id = create_pending("POST", self.VIRTUAL_PATH, approval_body)
        return record_http_decision(approval_id, "approve")["approval_token"]

    def test_user_approved_token_accepted_on_retry(self):
        """人审批准后，agent 用相同 arguments 重试 → token 有效放行。"""
        arguments = {"code": "print('hello')"}
        token = self._simulate_flow(arguments)

        retry_body = _approval_body({**arguments, "_approval_token": token})
        result = check_approval("POST", self.VIRTUAL_PATH, retry_body, token=token)
        assert result is None, "人审批准的 token 在重试时必须有效（P0 死循环回归测试）"

    def test_loop_run_tool_full_round_trip(self):
        """loop_run_task 场景全链路：嵌套 params 也保持指纹一致。

        注意 virtual_path 必须与 _simulate_flow 签发时一致（指纹含 path）——
        这正是旧 bug 的另一面：path 或 body 任一不一致都会导致 token 无效。
        """
        arguments = {
            "params": {"task_id": "activity_tracker.hourly_summarize"},
            "tool": "loop_run_task",
        }
        token = self._simulate_flow(arguments)
        retry_body = _approval_body({**arguments, "_approval_token": token})
        assert check_approval("POST", self.VIRTUAL_PATH, retry_body, token=token) is None

    def test_old_bug_regression_empty_body_verify_fails(self):
        """旧 bug 回归锚点：若验证侧退回 b""，token 必须无效。
        此测试证明 TestTokenFingerprintRoundTrip 的通过不是偶然。"""
        arguments = {"code": "print(1)"}
        token = self._simulate_flow(arguments)
        # 旧实现用空 body 验证 → 必须失败
        assert check_approval("POST", self.VIRTUAL_PATH, b"", token=token) is not None

    def test_different_args_rejected(self):
        """不同 arguments 重试 → token 无效（指纹绑定仍生效）。"""
        arguments = {"code": "print(1)"}
        token = self._simulate_flow(arguments)
        other = _approval_body({"code": "rm -rf /"})
        assert check_approval("POST", self.VIRTUAL_PATH, other, token=token) is not None

    def test_token_single_use_in_gateway_flow(self):
        """token 一次性：网关重试消费后再试 → 失败。"""
        arguments = {"code": "print(1)"}
        token = self._simulate_flow(arguments)
        retry_body = _approval_body({**arguments, "_approval_token": token})
        assert check_approval("POST", self.VIRTUAL_PATH, retry_body, token=token) is None
        # 二次使用失败
        retry_body2 = _approval_body(arguments)
        assert check_approval("POST", self.VIRTUAL_PATH, retry_body2, token=token) is not None


class TestRouteTagsLoopExclusion:
    """loop 控制端点已移出审批清单（2026-08-25）。"""

    def test_loop_tasks_post_is_safe_all_levels(self, monkeypatch):
        """/loop/tasks 下所有 POST 在任何 approval_level 都是 safe。"""
        from server.route_tags import classify_safety_runtime

        for level in ("strict", "moderate", "loose", "none"):
            import server.route_tags as rt

            monkeypatch.setattr(rt, "get_approval_level", lambda _lv=level: _lv)
            for path in (
                "/loop/tasks",
                "/loop/tasks/x/run",
                "/loop/tasks/x/pause",
                "/loop/tasks/x/resume",
            ):
                safety = classify_safety_runtime("POST", path)
                assert safety == "safe", f"{level} {path} 应为 safe，实际 {safety}"

    def test_exec_python_still_requires_approval_in_strict(self, monkeypatch):
        """对照：strict 下 exec 类端点仍需审批（清单移除未误伤）。

        显式固定 level=strict，避免依赖本机 config.toml 的实际值。
        """
        import server.route_tags as rt

        monkeypatch.setattr(rt, "get_approval_level", lambda: "strict")
        assert rt.classify_safety_runtime("POST", "/exec/python") == "approval_required"
