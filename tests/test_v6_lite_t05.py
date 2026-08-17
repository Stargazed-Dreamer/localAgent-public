"""v6-lite T05 验收测试：W2 pool tools 透传 + ToolRegistry + 只读工具端到端

覆盖 T05 acceptance（temp/sdd/v6-lite/tickets.md）：
- [x] ToolRegistry 从 /openapi.json 物化 catalog，只收 x-agent-callable=true
- [x] ToolEntry.to_openai_tool 转换正确（含 safety 提示）
- [x] ToolEntry.build_request 拼 path/query/body 正确
- [x] HttpClientToolExecutor 带 X-Agent-Caller 头（mock server 验证）
- [x] LLMPoolGateway 调 /llm/pool/chat-tools（mock server 验证）
- [x] _openai_tools_to_anthropic / _anthropic_tool_use_to_openai_tool_calls 转换
- [x] runner 集成 ToolRegistry：tools 透传 + safety 设置
- [x] 端到端："问→模型调 list_dir→结果回灌→模型总结"全链路（mock server）
- [x] 事件全部落库可回放

测试策略：
- 用 unittest.mock.patch 模拟 requests.Session，不依赖真实 server
- 构造 OpenAPI spec dict 直接测 _materialize
- mock /llm/pool/chat-tools 响应（含 tool_calls）
- mock /test/echo（fake tool endpoint）响应
- 用真实 EventStore + SessionRunner + LLMPoolGateway + HttpClientToolExecutor + ToolRegistry
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent import (  # noqa: E402
    AGENT_CALLER_HEADER_VALUE,
    CHAT_TOOLS_PATH,
    EventStore,
    HttpClientToolExecutor,
    LLMPoolGateway,
    LLMRequest,
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    SessionRunner,
    ToolCall,
    ToolEntry,
    ToolRegistry,
)
from client.core.agent.types import (  # noqa: E402
    TOOL_SAFETY_APPROVAL_REQUIRED,
    TOOL_SAFETY_READ_ONLY,
    TOOL_STATUS_COMPLETED,
)
from server.llm_pool.pool import (  # noqa: E402
    _anthropic_tool_use_to_openai_tool_calls,
    _openai_tool_choice_to_anthropic,
    _openai_tools_to_anthropic,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def store(tmp_db_path) -> EventStore:
    s = EventStore(db_path=tmp_db_path)
    s.init()
    yield s
    s.close()


def _make_openai_tool_call(
    call_id: str = "call_1", name: str = "list_dir",
    args: dict | None = None,
) -> dict:
    """构造 OpenAI 格式 tool_call dict。"""
    import json as _json
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": _json.dumps(args or {"path": "."}),
        },
    }


def _make_test_openapi_spec() -> dict:
    """构造一个测试用 OpenAPI spec，含 3 个路由：
    - list_dir (GET, x-agent-callable=true, read_only)
    - exec_python (POST, x-agent-callable=true, approval_required)
    - internal_admin (POST, x-agent-callable=false → 不应被收录)
    """
    return {
        "openapi": "3.0.0",
        "paths": {
            "/test/list-dir": {
                "get": {
                    "operationId": "list_dir",
                    "summary": "List directory contents",
                    "description": "Returns entries in a directory.",
                    "x-agent-callable": True,
                    "x-tool-safety": "read_only",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "description": "Directory path",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                },
            },
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
                                        "code": {"type": "string", "description": "Python code"},
                                    },
                                    "required": ["code"],
                                }
                            }
                        }
                    },
                },
            },
            "/admin/internal": {
                "post": {
                    "operationId": "admin_internal",
                    "summary": "Internal admin endpoint",
                    "x-agent-callable": False,  # fail-closed
                    "x-tool-safety": "approval_required",
                },
            },
            "/test/echo-with-id/{echo_id}": {
                "get": {
                    "operationId": "echo_with_id",
                    "summary": "Echo with path parameter",
                    "x-agent-callable": True,
                    "x-tool-safety": "read_only",
                    "parameters": [
                        {
                            "name": "echo_id",
                            "in": "path",
                            "description": "Echo ID",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "description": "Verbose output",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                    ],
                },
            },
        },
    }


# ============================================================================
# 1. ToolRegistry 物化测试
# ============================================================================


class TestToolRegistryMaterialize:
    def test_materialize_only_collects_agent_callable_true(self):
        """T05 acceptance: ToolRegistry 只收 x-agent-callable=true 路由。"""
        reg = ToolRegistry()
        spec = _make_test_openapi_spec()
        entries = reg._materialize(spec)
        # list_dir / exec_python / echo_with_id 应被收录，admin_internal 不应被收录
        assert "list_dir" in entries
        assert "exec_python" in entries
        assert "echo_with_id" in entries
        assert "admin_internal" not in entries
        assert len(entries) == 3

    def test_materialize_extracts_safety(self):
        """T05: safety 从 x-tool-safety 取。"""
        reg = ToolRegistry()
        entries = reg._materialize(_make_test_openapi_spec())
        assert entries["list_dir"].safety == TOOL_SAFETY_READ_ONLY
        assert entries["exec_python"].safety == TOOL_SAFETY_APPROVAL_REQUIRED

    def test_materialize_extracts_path_params(self):
        """T05: path 参数被正确提取。"""
        reg = ToolRegistry()
        entries = reg._materialize(_make_test_openapi_spec())
        echo = entries["echo_with_id"]
        assert echo.path == "/test/echo-with-id/{echo_id}"
        assert len(echo.path_params) == 1
        assert echo.path_params[0]["name"] == "echo_id"
        assert echo.path_params[0]["required"] is True

    def test_materialize_extracts_request_body_schema(self):
        """T05: requestBody JSON Schema 被正确提取。"""
        reg = ToolRegistry()
        entries = reg._materialize(_make_test_openapi_spec())
        exec_py = entries["exec_python"]
        assert exec_py.parameters.get("type") == "object"
        assert "code" in exec_py.parameters.get("properties", {})
        assert "code" in exec_py.parameters.get("required", [])

    def test_materialize_safety_fallback_by_method(self):
        """T05: 无 x-tool-safety 时按 method 推断（GET→read_only, POST→approval_required）。"""
        reg = ToolRegistry()
        spec = {
            "paths": {
                "/no-safety-get": {
                    "get": {
                        "operationId": "no_safety_get",
                        "x-agent-callable": True,
                        # 无 x-tool-safety
                    },
                },
                "/no-safety-post": {
                    "post": {
                        "operationId": "no_safety_post",
                        "x-agent-callable": True,
                    },
                },
            },
        }
        entries = reg._materialize(spec)
        assert entries["no_safety_get"].safety == TOOL_SAFETY_READ_ONLY
        assert entries["no_safety_post"].safety == TOOL_SAFETY_APPROVAL_REQUIRED

    def test_materialize_skips_agent_blocked(self):
        """T05: x-tool-safety=agent_blocked 时跳过。"""
        reg = ToolRegistry()
        spec = {
            "paths": {
                "/blocked": {
                    "get": {
                        "operationId": "blocked_tool",
                        "x-agent-callable": True,
                        "x-tool-safety": "agent_blocked",
                    },
                },
            },
        }
        entries = reg._materialize(spec)
        assert "blocked_tool" not in entries

    def test_refresh_uses_mocked_http(self):
        """T05: refresh() 通过 HTTP 拉取 spec。"""
        reg = ToolRegistry(base_url="http://fake.test")
        spec = _make_test_openapi_spec()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = spec
        with patch("requests.get", return_value=mock_resp) as mock_get:
            count = reg.refresh()
        assert count == 3
        mock_get.assert_called_once_with(
            "http://fake.test/openapi.json", timeout=10.0
        )

    def test_refresh_fail_open_keeps_old_catalog(self):
        """T05: refresh 失败时保留旧 catalog。"""
        reg = ToolRegistry(base_url="http://fake.test")
        # 先填充一份 catalog
        reg._entries = reg._materialize(_make_test_openapi_spec())
        old_count = len(reg._entries)
        # 模拟请求失败
        with patch("requests.get", side_effect=Exception("network error")):
            count = reg.refresh()
        assert count == old_count  # 保留旧数据
        assert len(reg._entries) == old_count


# ============================================================================
# 2. ToolEntry 转换测试
# ============================================================================


class TestToolEntryConversion:
    def test_to_openai_tool_format(self):
        """T05: ToolEntry.to_openai_tool 输出 OpenAI tools 格式。"""
        entry = ToolEntry(
            operation_id="list_dir",
            method="GET",
            path="/test/list-dir",
            summary="List directory contents",
            safety=TOOL_SAFETY_READ_ONLY,
        )
        tool = entry.to_openai_tool()
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "list_dir"
        assert "List directory contents" in tool["function"]["description"]
        assert "[read-only" in tool["function"]["description"]
        assert "HTTP GET" in tool["function"]["description"]
        assert tool["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_to_openai_tool_includes_safety_hint(self):
        """T05: description 含 safety 提示，模型可区分审批工具。"""
        entry = ToolEntry(
            operation_id="exec_python",
            method="POST",
            path="/exec/python",
            summary="Execute Python code",
            safety=TOOL_SAFETY_APPROVAL_REQUIRED,
        )
        tool = entry.to_openai_tool()
        assert "[requires user approval]" in tool["function"]["description"]

    def test_build_request_get_with_query(self):
        """T05: GET 请求 path 参数 + query 参数。"""
        entry = ToolEntry(
            operation_id="list_dir",
            method="GET",
            path="/test/list-dir",
            safety=TOOL_SAFETY_READ_ONLY,
        )
        path, query, body = entry.build_request({"path": "/tmp", "recursive": True})
        assert path == "/test/list-dir"
        assert query == {"path": "/tmp", "recursive": True}
        assert body is None

    def test_build_request_post_with_body(self):
        """T05: POST 请求参数作为 JSON body。"""
        entry = ToolEntry(
            operation_id="exec_python",
            method="POST",
            path="/exec/python",
            safety=TOOL_SAFETY_APPROVAL_REQUIRED,
            parameters={"type": "object", "properties": {"code": {"type": "string"}}},
        )
        path, query, body = entry.build_request({"code": "print('hi')"})
        assert path == "/exec/python"
        assert query is None
        assert body == {"code": "print('hi')"}

    def test_build_request_path_param_substitution(self):
        """T05: path 参数 {echo_id} 被替换。"""
        entry = ToolEntry(
            operation_id="echo_with_id",
            method="GET",
            path="/test/echo-with-id/{echo_id}",
            safety=TOOL_SAFETY_READ_ONLY,
            path_params=[{"name": "echo_id", "required": True}],
        )
        path, query, body = entry.build_request({"echo_id": "abc123", "verbose": True})
        assert path == "/test/echo-with-id/abc123"
        assert query == {"verbose": True}
        assert body is None

    def test_build_request_path_param_url_encoded(self):
        """T05: path 参数含特殊字符时 URL-encode。"""
        entry = ToolEntry(
            operation_id="get_item",
            method="GET",
            path="/items/{item_id}",
            safety=TOOL_SAFETY_READ_ONLY,
            path_params=[{"name": "item_id", "required": True}],
        )
        path, _, _ = entry.build_request({"item_id": "a b/c"})
        # 空格和斜杠都应被编码
        assert " " not in path
        assert path == "/items/a%20b%2Fc"

    def test_merged_parameters_combines_path_and_body(self):
        """T05: 合并 path_params + requestBody 成一个 JSON Schema。"""
        entry = ToolEntry(
            operation_id="update_item",
            method="PUT",
            path="/items/{item_id}",
            safety=TOOL_SAFETY_APPROVAL_REQUIRED,
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            path_params=[{"name": "item_id", "required": True}],
        )
        schema = entry._build_merged_parameters()
        assert schema["type"] == "object"
        assert "item_id" in schema["properties"]
        assert "name" in schema["properties"]
        # 两个字段都是 required
        assert "item_id" in schema["required"]
        assert "name" in schema["required"]


# ============================================================================
# 3. OpenAI ↔ Anthropic 格式转换测试
# ============================================================================


class TestFormatConversion:
    def test_openai_tools_to_anthropic_basic(self):
        """T05: OpenAI tools 格式转 Anthropic 格式。"""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": "List directory",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            },
        ]
        anth = _openai_tools_to_anthropic(tools)
        assert len(anth) == 1
        assert anth[0]["name"] == "list_dir"
        assert anth[0]["description"] == "List directory"
        assert anth[0]["input_schema"]["type"] == "object"

    def test_openai_tools_to_anthropic_empty(self):
        """T05: 空列表 / None 输入返回空列表。"""
        assert _openai_tools_to_anthropic(None) == []
        assert _openai_tools_to_anthropic([]) == []

    def test_openai_tools_to_anthropic_skips_invalid(self):
        """T05: 无 name 的 tool 定义被跳过。"""
        tools = [
            {"type": "function", "function": {"description": "no name"}},
            {"type": "function", "function": {"name": "valid", "description": "ok"}},
        ]
        anth = _openai_tools_to_anthropic(tools)
        assert len(anth) == 1
        assert anth[0]["name"] == "valid"

    def test_openai_tool_choice_to_anthropic_string(self):
        """T05: 字符串 tool_choice 转换。"""
        assert _openai_tool_choice_to_anthropic("auto") == {"type": "auto"}
        assert _openai_tool_choice_to_anthropic("none") is None
        assert _openai_tool_choice_to_anthropic("required") == {"type": "any"}
        assert _openai_tool_choice_to_anthropic(None) is None

    def test_openai_tool_choice_to_anthropic_dict(self):
        """T05: dict tool_choice 转换。"""
        result = _openai_tool_choice_to_anthropic(
            {"type": "function", "function": {"name": "list_dir"}}
        )
        assert result == {"type": "tool", "name": "list_dir"}

    def test_anthropic_tool_use_to_openai_tool_calls(self):
        """T05: Anthropic tool_use 块转 OpenAI tool_calls。"""
        blocks = [
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "call_abc",
                "name": "list_dir",
                "input": {"path": "/tmp"},
            },
        ]
        tool_calls = _anthropic_tool_use_to_openai_tool_calls(blocks)
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "call_abc"
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "list_dir"
        # input dict → arguments JSON string
        args = json.loads(tool_calls[0]["function"]["arguments"])
        assert args == {"path": "/tmp"}

    def test_anthropic_tool_use_to_openai_tool_calls_empty_input(self):
        """T05: input=None 时 arguments='{}'。"""
        blocks = [
            {"type": "tool_use", "id": "call_1", "name": "ping", "input": None},
        ]
        tool_calls = _anthropic_tool_use_to_openai_tool_calls(blocks)
        assert tool_calls[0]["function"]["arguments"] == "{}"


# ============================================================================
# 4. HttpClientToolExecutor 测试（mock requests.Session）
# ============================================================================


class TestHttpClientToolExecutor:
    def test_implements_tool_executor_protocol(self):
        """T05: HttpClientToolExecutor 实现 ToolExecutor 协议（runtime_checkable）。"""
        from client.core.agent.types import ToolExecutor
        reg = ToolRegistry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")
        assert isinstance(ex, ToolExecutor)

    def test_execute_success_returns_tool_result(self):
        """T05: 成功执行返回 ToolResult(is_error=False)。"""
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        # mock 响应
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"entries": ["a.txt", "b.txt"]}'
        mock_resp.json.return_value = {"entries": ["a.txt", "b.txt"]}

        with patch.object(ex._session, "get", return_value=mock_resp) as mock_get:
            tc = ToolCall(id="c1", name="list_dir", args={"path": "/tmp"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is False
        assert "a.txt" in result.content
        # 验证 X-Agent-Caller 头
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["X-Agent-Caller"] == AGENT_CALLER_HEADER_VALUE

    def test_execute_not_found_in_registry(self):
        """T05: 工具不在 registry 中 → is_error=True。"""
        reg = ToolRegistry()
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")
        tc = ToolCall(id="c1", name="nonexistent_tool", args={})
        result = asyncio.run(ex.execute(tc))
        assert result.is_error is True
        assert "not found" in result.content.lower()

    def test_execute_http_error_returns_is_error(self):
        """T05: HTTP 500 → is_error=True，content 含状态码。"""
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.side_effect = ValueError("not json")

        with patch.object(ex._session, "get", return_value=mock_resp):
            tc = ToolCall(id="c1", name="list_dir", args={"path": "/tmp"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "500" in result.content

    def test_execute_timeout_returns_is_error(self):
        """T05: 超时 → is_error=True，不抛异常。"""
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test", timeout=0.1)

        import requests as _requests
        with patch.object(ex._session, "get", side_effect=_requests.exceptions.Timeout("timed out")):
            tc = ToolCall(id="c1", name="list_dir", args={"path": "/tmp"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "timed out" in result.content.lower()

    def test_execute_approval_403_passes_approval_info(self):
        """T05: 非 approval 的 403（如权限不足）透传 HTTP 状态码和错误信息给模型。

        T06 后：403 + approval_id 触发路径 B 审批挑战（在 T06 测试套件覆盖）；
        403 + error=user_denied 触发路径 A 拒绝（在 T06 测试套件覆盖）；
        此测试覆盖"其他 403"（无 approval_id、无 user_denied）的直接透传。
        """
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"error": "forbidden", "message": "insufficient permissions"}'
        mock_resp.json.return_value = {
            "error": "forbidden",
            "message": "insufficient permissions",
        }

        with patch.object(ex._session, "post", return_value=mock_resp):
            tc = ToolCall(id="c1", name="exec_python", args={"code": "print('hi')"})
            result = asyncio.run(ex.execute(tc))

        assert result.is_error is True
        assert "403" in result.content
        assert "forbidden" in result.content or "insufficient" in result.content

    def test_execute_path_param_in_url(self):
        """T05: path 参数被替换到 URL 中。"""
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"echo": "abc123"}'
        mock_resp.json.return_value = {"echo": "abc123"}

        with patch.object(ex._session, "get", return_value=mock_resp) as mock_get:
            tc = ToolCall(id="c1", name="echo_with_id", args={"echo_id": "abc123", "verbose": True})
            asyncio.run(ex.execute(tc))

        # 验证 URL 含 path 参数
        args, kwargs = mock_get.call_args
        assert "abc123" in args[0]
        assert "/test/echo-with-id/abc123" in args[0]
        # verbose 作为 query 参数
        assert kwargs["params"] == {"verbose": True}


# ============================================================================
# 5. LLMPoolGateway 测试（mock requests.Session）
# ============================================================================


class TestLLMPoolGateway:
    def test_implements_llm_gateway_protocol(self):
        """T05: LLMPoolGateway 实现 LLMGateway 协议。"""
        from client.core.agent.types import LLMGateway
        gw = LLMPoolGateway(base_url="http://fake.test")
        assert isinstance(gw, LLMGateway)

    def test_call_success_returns_llm_response(self):
        """T05: 成功调用返回 LLMResponse。"""
        gw = LLMPoolGateway(base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "content": "Hello!",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            "model": "gpt-4",
            "finish_reason": "stop",
            "tool_calls": [],
        }

        with patch.object(gw._session, "post", return_value=mock_resp) as mock_post:
            req = LLMRequest(messages=[Message(role="user", content="hi")])
            resp = asyncio.run(gw.call(req))

        assert resp.content == "Hello!"
        assert resp.stop_reason == "end_turn"
        assert resp.model == "gpt-4"
        assert resp.tool_calls == []
        # 验证请求路径
        args, kwargs = mock_post.call_args
        assert args[0] == "http://fake.test" + CHAT_TOOLS_PATH

    def test_call_returns_tool_calls(self):
        """T05: 响应含 tool_calls 时正确解析。"""
        gw = LLMPoolGateway(base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "content": "",
            "usage": {"total_tokens": 10},
            "model": "gpt-4",
            "finish_reason": "tool_calls",
            "tool_calls": [_make_openai_tool_call("c1", "list_dir", {"path": "/tmp"})],
        }

        with patch.object(gw._session, "post", return_value=mock_resp):
            req = LLMRequest(messages=[Message(role="user", content="list /tmp")])
            resp = asyncio.run(gw.call(req))

        assert resp.stop_reason == "tool_use"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "list_dir"

    def test_call_passes_tools_to_request_body(self):
        """T05: tools / tool_choice 被透传到请求体。"""
        gw = LLMPoolGateway(base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True, "content": "ok", "usage": {}, "model": "m",
            "finish_reason": "stop", "tool_calls": [],
        }

        tools = [{"type": "function", "function": {"name": "list_dir"}}]
        with patch.object(gw._session, "post", return_value=mock_resp) as mock_post:
            req = LLMRequest(
                messages=[Message(role="user", content="hi")],
                tools=tools,
                tool_choice="auto",
            )
            asyncio.run(gw.call(req))

        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert body["tools"] == tools
        assert body["tool_choice"] == "auto"

    def test_call_pool_failure_returns_error_response(self):
        """T05: pool 返回 ok=false 时返回 stop_reason=error。"""
        gw = LLMPoolGateway(base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": False, "error": "no available keys"}

        with patch.object(gw._session, "post", return_value=mock_resp):
            req = LLMRequest(messages=[Message(role="user", content="hi")])
            resp = asyncio.run(gw.call(req))

        assert resp.stop_reason == "error"
        assert "no available keys" in resp.content

    def test_call_http_error_returns_error_response(self):
        """T05: HTTP 500 时返回 stop_reason=error。"""
        gw = LLMPoolGateway(base_url="http://fake.test")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch.object(gw._session, "post", return_value=mock_resp):
            req = LLMRequest(messages=[Message(role="user", content="hi")])
            resp = asyncio.run(gw.call(req))

        assert resp.stop_reason == "error"
        assert "500" in resp.content

    def test_call_timeout_returns_error_response(self):
        """T05: 超时不抛异常，返回 stop_reason=error。"""
        gw = LLMPoolGateway(base_url="http://fake.test", timeout=0.1)

        import requests as _requests
        with patch.object(gw._session, "post", side_effect=_requests.exceptions.Timeout("t/o")):
            req = LLMRequest(messages=[Message(role="user", content="hi")])
            resp = asyncio.run(gw.call(req))

        assert resp.stop_reason == "error"
        assert "timed out" in resp.content.lower()

    def test_message_to_dict_preserves_tool_calls(self):
        """T05: assistant 消息的 tool_calls 被保留。"""
        gw = LLMPoolGateway(base_url="http://fake.test")
        msg = Message(
            role="assistant",
            content="Let me check.",
            tool_calls=[_make_openai_tool_call("c1", "list_dir", {"path": "."})],
        )
        d = gw._message_to_dict(msg)
        assert d["role"] == "assistant"
        assert d["content"] == "Let me check."
        assert len(d["tool_calls"]) == 1
        assert d["tool_calls"][0]["function"]["name"] == "list_dir"

    def test_message_to_dict_preserves_tool_call_id(self):
        """T05: tool 消息的 tool_call_id 被保留。"""
        gw = LLMPoolGateway(base_url="http://fake.test")
        msg = Message(
            role="tool",
            content="result text",
            tool_call_id="c1",
            source="tool_result",
        )
        d = gw._message_to_dict(msg)
        assert d["role"] == "tool"
        assert d["tool_call_id"] == "c1"

    def test_normalize_stop_reason(self):
        """T05: finish_reason 归一化。"""
        gw = LLMPoolGateway(base_url="http://fake.test")
        assert gw._normalize_stop_reason("stop") == "end_turn"
        assert gw._normalize_stop_reason("end_turn") == "end_turn"
        assert gw._normalize_stop_reason("tool_calls") == "tool_use"
        assert gw._normalize_stop_reason("tool_use") == "tool_use"
        assert gw._normalize_stop_reason("length") == "max_tokens"
        assert gw._normalize_stop_reason("max_tokens") == "max_tokens"
        assert gw._normalize_stop_reason("") == "end_turn"


# ============================================================================
# 6. Runner 集成 ToolRegistry 测试
# ============================================================================


class TestRunnerToolRegistryIntegration:
    def test_runner_deps_has_tool_registry_field(self):
        """T05: RunnerDeps 含 tool_registry 字段（默认 None）。"""
        from client.core.agent import MockLLM
        deps = RunnerDeps(llm_gateway=MockLLM())
        assert deps.tool_registry is None

    def test_build_request_populates_tools_from_registry(self):
        """T05: _build_request 从 registry.to_openai_tools() 填充 LLMRequest.tools。

        T25/D8 设计：REST 端点折叠成 1 个桶工具（rest_endpoints），
        不再每个端点一个独立工具。_materialize 物化仍返回 3 个 entries。
        """
        from client.core.agent import MockLLM
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())
        # 物化仍返回 3 个 entries（list_dir / exec_python / echo_with_id，
        # admin_internal 因 x-agent-callable=false 被 fail-closed 过滤）
        assert len(reg._entries) == 3
        assert set(reg._entries.keys()) == {"list_dir", "exec_python", "echo_with_id"}
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=MockLLM(), tool_registry=reg),
            config=RunnerConfig(session_id="test"),
        )
        req = runner._build_request([Message(role="user", content="hi")])
        # D8: REST 端点折叠成 1 个桶工具，function name 为 rest_endpoints
        assert len(req.tools) == 1
        tool_names = {t["function"]["name"] for t in req.tools}
        assert "rest_endpoints" in tool_names

    def test_build_request_no_registry_no_tools(self):
        """T05: 无 tool_registry 时 tools 为空（T01/T02 行为，向后兼容）。"""
        from client.core.agent import MockLLM
        runner = SessionRunner(
            deps=RunnerDeps(llm_gateway=MockLLM()),
            config=RunnerConfig(session_id="test"),
        )
        req = runner._build_request([Message(role="user", content="hi")])
        assert req.tools == []

    def test_runner_sets_safety_from_registry(self, store):
        """T05: runner 解析 tool_calls 后从 registry 查 ToolEntry 设置 safety。"""
        from client.core.agent import MockLLM, MockToolExecutor
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())

        async def run():
            await store.create_session(session_id="s1")
            await store.append_message(
                "s1", Message(role="user", content="list /tmp")
            )
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="",
                    tool_calls=[_make_openai_tool_call("c1", "list_dir", {"path": "/tmp"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm,
                    event_store=store,
                    tool_executor=MockToolExecutor(default_result='{"entries": []}'),
                    tool_registry=reg,
                ),
                config=RunnerConfig(session_id="s1"),
            )
            outcome = await runner.run()
            assert outcome.status == "completed"
            # 验证 tool_call 的 safety 来自 registry（list_dir = read_only）
            tool_calls = await store.load_tool_calls("s1")
            assert len(tool_calls) == 1
            assert tool_calls[0].safety == TOOL_SAFETY_READ_ONLY

        asyncio.run(run())

    def test_runner_sets_approval_safety_from_registry(self, store):
        """T05: approval_required 工具的 safety 正确设置。"""
        from client.core.agent import MockLLM, MockToolExecutor
        reg = ToolRegistry()
        reg._entries = reg._materialize(_make_test_openapi_spec())

        async def run():
            await store.create_session(session_id="s1")
            await store.append_message(
                "s1", Message(role="user", content="run print('hi')")
            )
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="",
                    tool_calls=[_make_openai_tool_call("c1", "exec_python", {"code": "print('hi')"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm,
                    event_store=store,
                    tool_executor=MockToolExecutor(default_result="ok"),
                    tool_registry=reg,
                ),
                config=RunnerConfig(session_id="s1"),
            )
            outcome = await runner.run()
            assert outcome.status == "completed"
            tool_calls = await store.load_tool_calls("s1")
            assert tool_calls[0].safety == TOOL_SAFETY_APPROVAL_REQUIRED

        asyncio.run(run())

    def test_runner_fallback_safety_when_registry_has_no_entry(self, store):
        """T05: registry 中找不到工具时 safety fallback 为 read_only。"""
        from client.core.agent import MockLLM, MockToolExecutor
        reg = ToolRegistry()  # 空 registry

        async def run():
            await store.create_session(session_id="s1")
            await store.append_message(
                "s1", Message(role="user", content="call unknown tool")
            )
            mock_llm = MockLLM(script=[
                LLMResponse(
                    content="",
                    tool_calls=[_make_openai_tool_call("c1", "unknown_tool", {})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ])
            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=mock_llm,
                    event_store=store,
                    tool_executor=MockToolExecutor(default_result="ok"),
                    tool_registry=reg,
                ),
                config=RunnerConfig(session_id="s1"),
            )
            outcome = await runner.run()
            assert outcome.status == "completed"
            tool_calls = await store.load_tool_calls("s1")
            # fallback 为 read_only
            assert tool_calls[0].safety == TOOL_SAFETY_READ_ONLY

        asyncio.run(run())


# ============================================================================
# 7. 端到端：MockLLM + Mock server + 真 ToolRegistry + 真 HttpClientToolExecutor
# ============================================================================


class TestEndToEndWithMockServer:
    """端到端测试：模拟 server /openapi.json + /llm/pool/chat-tools + /test/list-dir，
    用真实的 ToolRegistry / HttpClientToolExecutor / SessionRunner / EventStore 跑完整链路。
    """

    def test_full_loop_list_dir_via_mock_server(self, store):
        """T05 acceptance: 问→模型调 list_dir→结果回灌→模型总结 全链路。

        模拟场景：
        - server /openapi.json 返回 list_dir 工具定义
        - LLM 第一轮返回 tool_calls=[list_dir(path=".")]
        - server /test/list-dir 返回目录内容
        - LLM 第二轮返回总结文本
        - 验证：events 全部落库可回放，tool_call safety=read_only
        """
        async def run():
            # 1. 准备 mock server 响应
            spec = _make_test_openapi_spec()
            mock_openapi_resp = MagicMock()
            mock_openapi_resp.status_code = 200
            mock_openapi_resp.json.return_value = spec

            mock_chat_tools_resp_1 = MagicMock()
            mock_chat_tools_resp_1.status_code = 200
            mock_chat_tools_resp_1.json.return_value = {
                "ok": True,
                "content": "",
                "usage": {"total_tokens": 10},
                "model": "gpt-4",
                "finish_reason": "tool_calls",
                "tool_calls": [_make_openai_tool_call("c1", "list_dir", {"path": "."})],
            }
            mock_chat_tools_resp_2 = MagicMock()
            mock_chat_tools_resp_2.status_code = 200
            mock_chat_tools_resp_2.json.return_value = {
                "ok": True,
                "content": "Found 2 files: a.txt and b.txt.",
                "usage": {"total_tokens": 20},
                "model": "gpt-4",
                "finish_reason": "stop",
                "tool_calls": [],
            }

            mock_list_dir_resp = MagicMock()
            mock_list_dir_resp.status_code = 200
            mock_list_dir_resp.text = '{"entries": ["a.txt", "b.txt"]}'
            mock_list_dir_resp.json.return_value = {"entries": ["a.txt", "b.txt"]}

            # 2. 构造 ToolRegistry（直接物化，不走 HTTP）
            reg = ToolRegistry(base_url="http://fake.test")
            reg._entries = reg._materialize(spec)

            # 3. 构造 HttpClientToolExecutor，mock _session.get
            ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

            # 4. 构造 LLMPoolGateway，mock _session.post 按调用顺序返回不同响应
            gw = LLMPoolGateway(base_url="http://fake.test")
            call_count = [0]

            def mock_post(url, json=None, timeout=None):
                if url.endswith(CHAT_TOOLS_PATH):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        return mock_chat_tools_resp_1
                    return mock_chat_tools_resp_2
                return MagicMock(status_code=404)

            # 5. 跑 SessionRunner
            await store.create_session(session_id="e2e-1")
            await store.append_message(
                "e2e-1", Message(role="user", content="list current directory")
            )

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=gw,
                    event_store=store,
                    tool_executor=ex,
                    tool_registry=reg,
                ),
                config=RunnerConfig(session_id="e2e-1", use_stream=False),  # call 模式端到端
            )

            with patch.object(gw._session, "post", side_effect=mock_post), \
                 patch.object(ex._session, "get", return_value=mock_list_dir_resp):
                outcome = await runner.run()

            # 6. 验证 outcome
            assert outcome.status == "completed"
            assert outcome.iterations == 2

            # 7. 验证 events 全部落库可回放
            events = await store.load_events("e2e-1")
            event_types = [e.type for e in events]
            assert "session_created" in event_types
            assert "user_message_appended" in event_types
            assert "assistant_message_appended" in event_types
            assert "tool_call_pending" in event_types
            assert "tool_call_status_changed" in event_types
            assert "transition" in event_types
            assert "session_finalized" in event_types

            # 8. 验证 tool_calls 落库（safety=read_only，status=completed）
            tool_calls = await store.load_tool_calls("e2e-1")
            assert len(tool_calls) == 1
            assert tool_calls[0].name == "list_dir"
            assert tool_calls[0].safety == TOOL_SAFETY_READ_ONLY
            assert tool_calls[0].status == TOOL_STATUS_COMPLETED

            # 9. 验证 messages 落库（user / assistant / tool_result / final assistant）
            messages = await store.load_messages("e2e-1")
            assert len(messages) == 4
            assert messages[0].role == "user"
            assert messages[1].role == "assistant"
            assert messages[1].tool_calls  # 第一轮 assistant 带 tool_calls
            assert messages[2].role == "tool"
            assert messages[2].tool_call_id == "c1"
            assert "a.txt" in messages[2].content
            assert messages[3].role == "assistant"
            assert "a.txt" in messages[3].content or "2 files" in messages[3].content

            # 10. 验证 replay_events 可回放整个会话
            replay = await store.replay_events("e2e-1")
            assert len(replay) == len(events)
            assert all("type" in r for r in replay)

        asyncio.run(run())

    def test_x_agent_caller_header_sent(self, store):
        """T05 acceptance: 工具调用经 X-Agent-Caller 头过 http_guard。"""
        async def run():
            spec = _make_test_openapi_spec()
            reg = ToolRegistry(base_url="http://fake.test")
            reg._entries = reg._materialize(spec)
            ex = HttpClientToolExecutor(registry=reg, base_url="http://fake.test")

            mock_list_dir_resp = MagicMock()
            mock_list_dir_resp.status_code = 200
            mock_list_dir_resp.text = '{"entries": ["a.txt"]}'
            mock_list_dir_resp.json.return_value = {"entries": ["a.txt"]}

            mock_chat_tools_resp_1 = MagicMock()
            mock_chat_tools_resp_1.status_code = 200
            mock_chat_tools_resp_1.json.return_value = {
                "ok": True, "content": "", "usage": {}, "model": "m",
                "finish_reason": "tool_calls",
                "tool_calls": [_make_openai_tool_call("c1", "list_dir", {"path": "."})],
            }
            mock_chat_tools_resp_2 = MagicMock()
            mock_chat_tools_resp_2.status_code = 200
            mock_chat_tools_resp_2.json.return_value = {
                "ok": True, "content": "ok", "usage": {}, "model": "m",
                "finish_reason": "stop", "tool_calls": [],
            }

            gw = LLMPoolGateway(base_url="http://fake.test")
            chat_count = [0]

            def mock_post(url, json=None, timeout=None):
                if url.endswith(CHAT_TOOLS_PATH):
                    chat_count[0] += 1
                    return mock_chat_tools_resp_1 if chat_count[0] == 1 else mock_chat_tools_resp_2
                return MagicMock(status_code=404)

            await store.create_session(session_id="e2e-2")
            await store.append_message(
                "e2e-2", Message(role="user", content="list dir")
            )

            runner = SessionRunner(
                deps=RunnerDeps(
                    llm_gateway=gw, event_store=store,
                    tool_executor=ex, tool_registry=reg,
                ),
                config=RunnerConfig(session_id="e2e-2", use_stream=False),  # call 模式端到端
            )

            with patch.object(gw._session, "post", side_effect=mock_post), \
                 patch.object(ex._session, "get", return_value=mock_list_dir_resp) as mock_get:
                outcome = await runner.run()

            assert outcome.status == "completed"
            # 验证 X-Agent-Caller 头被发送
            _, kwargs = mock_get.call_args
            assert "X-Agent-Caller" in kwargs["headers"]
            assert kwargs["headers"]["X-Agent-Caller"] == AGENT_CALLER_HEADER_VALUE

        asyncio.run(run())
