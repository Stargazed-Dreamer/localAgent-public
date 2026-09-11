"""C6 · USER_DENIED 缓存（spec D6 审批效率）

测试范围：
- 用户拒绝审批后，缓存 (tool_name, args_hash) → USER_DENIED
- 同一 (tool_name, args_hash) 再次调用时直接返回缓存的 USER_DENIED，不再弹审批
- 不同 args 不命中缓存（仍走正常审批）
- 缓存粒度：(tool_name, args_hash)（D6/C6 决策）

设计依据：
- spec D6：用户拒绝审批后，同一工具同一参数不应反复弹审批窗
- v6-lite §3 W3：审批桥接路径 A（user_denied）/ 路径 B（approval_required）
- ToolResultVariant.USER_DENIED 标记用户拒绝

不依赖后端运行，全部用 mock。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import HttpClientToolExecutor, ToolRegistry  # noqa: E402
from client.core.agent.types import ToolCall, ToolResultVariant  # noqa: E402

# ============================================================================
# Helpers
# ============================================================================


def _make_test_openapi_spec() -> dict:
    """构造测试用 OpenAPI spec（含 exec_python approval_required 端点）。"""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/exec/python": {
                "post": {
                    "operationId": "exec_python",
                    "summary": "Execute Python code",
                    "x-agent-callable": True,
                    "x-tool-safety": "approval_required",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                    },
                                    "required": ["code"],
                                }
                            }
                        }
                    },
                },
            },
            "/test/safe": {
                "get": {
                    "operationId": "safe_op",
                    "summary": "Safe operation",
                    "x-agent-callable": True,
                    "x-tool-safety": "read_only",
                },
            },
        },
    }


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg._entries = reg._materialize(_make_test_openapi_spec())
    return reg


def _make_mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
        if text is None:
            text = json.dumps(json_data)
    resp.text = text or ""
    return resp


# ============================================================================
# C6: USER_DENIED 缓存
# ============================================================================


class TestUserDeniedCache:
    """C6: 用户拒绝审批后，同 (tool_name, args_hash) 再次调用直接返回缓存。"""

    def test_second_call_same_args_returns_cached_denial(self):
        """路径 A 拒绝后，同参数再次调用 → 直接返回缓存 USER_DENIED，不发 HTTP。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=403,
            json_data={
                "error": "user_denied",
                "user_feedback": "不允许执行此代码",
            },
        )

        tc = ToolCall(id="c1", name="exec_python", args={"code": "print(1)"})

        # 第一次调用：走 HTTP，返回 USER_DENIED
        with patch.object(ex._session, "post", return_value=mock_resp) as mock_post:
            result1 = asyncio.run(ex.execute(tc))

        assert result1.variant == ToolResultVariant.USER_DENIED
        assert "不允许执行此代码" in result1.content
        assert mock_post.call_count == 1

        # 第二次调用同参数：应直接返回缓存 USER_DENIED，不再发 HTTP
        with patch.object(ex._session, "post", return_value=mock_resp) as mock_post:
            result2 = asyncio.run(ex.execute(tc))

        assert result2.variant == ToolResultVariant.USER_DENIED
        assert mock_post.call_count == 0, (
            f"C6: Second call with same args should use cache, "
            f"not make HTTP request. post called {mock_post.call_count} times"
        )

    def test_different_args_not_cached(self):
        """不同参数不命中缓存，仍走正常审批。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=403,
            json_data={"error": "user_denied", "user_feedback": "no"},
        )

        tc1 = ToolCall(id="c1", name="exec_python", args={"code": "print(1)"})
        tc2 = ToolCall(id="c2", name="exec_python", args={"code": "print(2)"})

        with patch.object(ex._session, "post", return_value=mock_resp):
            result1 = asyncio.run(ex.execute(tc1))
            result2 = asyncio.run(ex.execute(tc2))

        assert result1.variant == ToolResultVariant.USER_DENIED
        assert result2.variant == ToolResultVariant.USER_DENIED
        # 两次都走了 HTTP（不同 args）

    def test_success_does_not_cache(self):
        """成功结果不缓存（只有 USER_DENIED 缓存）。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = _make_mock_response(
            status_code=200,
            json_data={"result": "ok"},
        )

        tc = ToolCall(id="c1", name="exec_python", args={"code": "print(1)"})

        with patch.object(ex._session, "post", return_value=mock_resp) as mock_post:
            result1 = asyncio.run(ex.execute(tc))
            result2 = asyncio.run(ex.execute(tc))

        assert result1.variant == ToolResultVariant.SUCCESS
        assert result2.variant == ToolResultVariant.SUCCESS
        # 两次都走了 HTTP（成功不缓存）
        assert mock_post.call_count == 2

    def test_cache_key_is_tool_name_plus_args_hash(self):
        """缓存键是 (tool_name, args_hash)，同 tool 不同 args 不冲突。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        deny_resp = _make_mock_response(
            status_code=403,
            json_data={"error": "user_denied", "user_feedback": "denied"},
        )
        success_resp = _make_mock_response(
            status_code=200,
            json_data={"result": "ok"},
        )

        tc1 = ToolCall(id="c1", name="exec_python", args={"code": "print(1)"})
        tc2 = ToolCall(id="c2", name="exec_python", args={"code": "print(2)"})

        # tc1 被拒绝 → 缓存
        with patch.object(ex._session, "post", return_value=deny_resp):
            asyncio.run(ex.execute(tc1))

        # tc2 不同 args → 走 HTTP，可能成功
        with patch.object(ex._session, "post", return_value=success_resp) as mock_post:
            result2 = asyncio.run(ex.execute(tc2))

        assert result2.variant == ToolResultVariant.SUCCESS
        assert mock_post.call_count == 1, "tc2 should not hit tc1's cache (different args)"

    def test_path_b_denial_also_cached(self):
        """路径 B 审批拒绝也缓存（_handle_approval_challenge 返回 USER_DENIED 时）。"""
        reg = _make_registry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        # 路径 B：第一次 403 approval_required → 调 request-approval → 拒绝
        resp_403 = _make_mock_response(
            status_code=403,
            json_data={
                "error": "approval_required",
                "approval_id": "appr_123",
                "message": "needs approval",
            },
        )
        resp_deny = _make_mock_response(
            status_code=200,
            json_data={
                "approved": False,
                "decision": "deny",
                "feedback": "不许删",
            },
        )

        tc = ToolCall(id="c1", name="exec_python", args={"code": "print(1)"})

        # 第一次：路径 B 走完 → 拒绝 → USER_DENIED
        with (
            patch.object(ex._session, "post", return_value=resp_403) as mock_post,
            patch.object(ex._session, "get", return_value=resp_deny) as mock_get,
        ):
            result1 = asyncio.run(ex.execute(tc))

        assert result1.variant == ToolResultVariant.USER_DENIED

        # 第二次：同参数 → 应直接返回缓存，不再发 HTTP
        with (
            patch.object(ex._session, "post", return_value=resp_403) as mock_post,
            patch.object(ex._session, "get", return_value=resp_deny) as mock_get,
        ):
            result2 = asyncio.run(ex.execute(tc))

        assert result2.variant == ToolResultVariant.USER_DENIED
        second_call_count = mock_post.call_count + mock_get.call_count
        assert second_call_count == 0, (
            f"C6: Path B denial should be cached. "
            f"Second call made {second_call_count} HTTP requests (expected 0)"
        )
