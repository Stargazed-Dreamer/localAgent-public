"""v6-lite-streaming-gui T00 单元测试：pool.stream + ToolCallAccumulator + _iter_lines_async

测试范围（spec D02/D03/D04/D14）：
- ToolCallAccumulator：半包累积 + build_all（单/多 tool_call）
- _iter_lines_async：requests.Response 桥接为 async iterator
- pool.stream()：MockProvider SSE 流 → 事件序列断言
- 客户端断开 → key 释放（D14）

不依赖后端运行，全部用 mock。

T07：pool._stream_openai_sse 改用 httpx.AsyncClient.stream + aiter_lines，
测试 mock 从 patch requests.post 改为 httpx.MockTransport + patch get_async_client
（spec Anti-Cheat：测试用 httpx.MockTransport 测试 HTTP 调用）。
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

# 确保项目根在 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.llm_pool.pool import (  # noqa: E402
    _STREAM_SENTINEL,
    ToolCallAccumulator,
    _iter_lines_async,
    _safe_next_line,
)

# =====================================================================
# ToolCallAccumulator 测试（D04）
# =====================================================================

def test_tool_call_accumulator_single_call_multi_chunk():
    """单个 tool_call 跨多个 chunk 累积"""
    acc = ToolCallAccumulator()
    # 用变量避免行内嵌套引号解析问题
    full_args = chr(123) + '"city":"BJ"' + chr(125)  # {"city":"BJ"}
    chunk1 = full_args[:9]   # {"city":"
    chunk2 = full_args[9:11]  # BJ
    chunk3 = full_args[11:]   # "}
    # chunk 1：id + name + arguments 开头
    acc.on_delta({"index": 0, "id": "call_abc", "type": "function",
                  "function": {"name": "get_weather", "arguments": chunk1}})
    # chunk 2：arguments 增量
    acc.on_delta({"index": 0, "function": {"arguments": chunk2}})
    # chunk 3：arguments 增量
    acc.on_delta({"index": 0, "function": {"arguments": chunk3}})

    result = acc.build_all()
    assert len(result) == 1
    assert result[0]["id"] == "call_abc"
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "get_weather"
    assert result[0]["function"]["arguments"] == full_args


def test_tool_call_accumulator_multiple_calls():
    """多个 tool_call（不同 index）同时累积"""
    acc = ToolCallAccumulator()
    acc.on_delta({"index": 0, "id": "call_1", "function": {"name": "foo", "arguments": '{"a":1}'}})
    acc.on_delta({"index": 1, "id": "call_2", "function": {"name": "bar", "arguments": '{"b":2}'}})

    result = acc.build_all()
    assert len(result) == 2
    # 按 index 排序
    assert result[0]["id"] == "call_1"
    assert result[0]["function"]["name"] == "foo"
    assert result[1]["id"] == "call_2"
    assert result[1]["function"]["name"] == "bar"


def test_tool_call_accumulator_empty():
    """无 delta 时 build_all 返回空列表"""
    acc = ToolCallAccumulator()
    assert acc.build_all() == []


def test_tool_call_accumulator_default_index():
    """delta 无 index 时默认 index=0"""
    acc = ToolCallAccumulator()
    acc.on_delta({"id": "call_x", "function": {"name": "fn", "arguments": "{}"}})
    result = acc.build_all()
    assert len(result) == 1
    assert result[0]["id"] == "call_x"


# =====================================================================
# _iter_lines_async / _safe_next_line 测试
# =====================================================================

def test_safe_next_line_normal():
    """正常读取下一行"""
    it = iter(["line1", "line2", "line3"])
    assert _safe_next_line(it) == "line1"
    assert _safe_next_line(it) == "line2"
    assert _safe_next_line(it) == "line3"
    assert _safe_next_line(it) is _STREAM_SENTINEL


def test_iter_lines_async_basic():
    """_iter_lines_async 把 mock response 桥接为 async iterator"""
    mock_resp = MagicMock()
    mock_resp.iter_lines = MagicMock(return_value=iter([
        'data: {"choices":[{"delta":{"content":"hello"}}]}',
        '',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: [DONE]',
    ]))

    async def run():
        lines = []
        async for line in _iter_lines_async(mock_resp):
            lines.append(line)
        return lines

    lines = asyncio.run(run())
    assert len(lines) == 4  # 含空行
    assert "hello" in lines[0]
    assert "world" in lines[2]


# =====================================================================
# pool.stream() MockProvider 测试（D02/D03）
# =====================================================================

def _make_sse_handler(lines: list[str], status_code: int = 200):
    """构造 httpx.MockTransport handler：返回 SSE 流式响应

    T07：替代原 _make_mock_sse_response（mock requests.Response）。
    handler 返回 httpx.Response，content 为 SSE 行拼接的 bytes，
    被 client.stream() + resp.aiter_lines() 消费。
    """
    sse_bytes = b"".join(line.encode("utf-8") + b"\n" for line in lines)
    err_body = b"error body" if status_code != 200 else b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "text/event-stream"},
            content=sse_bytes if status_code == 200 else err_body,
        )
    return handler


def _make_mock_async_client(handler) -> httpx.AsyncClient:
    """构造用 MockTransport 的 httpx.AsyncClient（不真实联网）。

    调用方负责在测试结束后 await client.aclose()。
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_mock_key(protocol: str = "openai"):
    """构造 mock LLMKey"""
    key = MagicMock()
    key.name = "test_key"
    key.protocol = protocol
    key.key = "sk-test"
    key.base_url = "https://api.test.com/v1"
    key.models = ["test-model"]
    key.default_model = "test-model"
    key.get_best_model_in_range = MagicMock(return_value="test-model")
    return key


def test_pool_stream_openai_text_delta():
    """pool.stream() OpenAI 真流式：text_delta 事件序列"""
    from server.llm_pool.pool import LLMPool

    sse_lines = [
        'data: {"model":"test-model","choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
        'data: [DONE]',
    ]
    mock_key = _make_mock_key("openai")

    pool = LLMPool.__new__(LLMPool)  # 不调 __init__（避免加载 keys）
    pool._acquire = MagicMock(return_value=mock_key)
    pool._release = MagicMock()

    async def run():
        events = []
        async for event in pool.stream(
            messages=[{"role": "user", "content": "hi"}],
            project="test",
        ):
            events.append(event)
        return events

    # T07：用 httpx.MockTransport + patch get_async_client 替代 patch requests.post
    mock_client = _make_mock_async_client(_make_sse_handler(sse_lines))
    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            events = asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())

    # 断言事件序列
    types = [e["type"] for e in events]
    assert "text_delta" in types
    assert "done" in types
    # text_delta 内容
    text_deltas = [e for e in events if e["type"] == "text_delta"]
    assert text_deltas[0]["delta"] == "Hello"
    assert text_deltas[1]["delta"] == " world"
    # done 事件
    done = [e for e in events if e["type"] == "done"]
    assert done[0]["finish_reason"] == "stop"
    # usage 事件
    usage = [e for e in events if e["type"] == "usage"]
    assert usage[0]["total_tokens"] == 7
    # key 释放（D14）
    pool._release.assert_called_once()
    call_kwargs = pool._release.call_args
    assert call_kwargs.kwargs.get("success") is True


def test_pool_stream_openai_tool_call_accumulation():
    """pool.stream() OpenAI 真流式：tool_call 半包累积（D04）"""
    from server.llm_pool.pool import LLMPool

    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"foo","arguments":"{\\"a\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
        'data: {"choices":[{"finish_reason":"tool_calls"}]}',
        'data: [DONE]',
    ]
    mock_key = _make_mock_key("openai")

    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=mock_key)
    pool._release = MagicMock()

    async def run():
        events = []
        async for event in pool.stream(
            messages=[{"role": "user", "content": "call foo"}],
            project="test",
        ):
            events.append(event)
        return events

    # T07：用 httpx.MockTransport + patch get_async_client 替代 patch requests.post
    mock_client = _make_mock_async_client(_make_sse_handler(sse_lines))
    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            events = asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())

    # tool_call_delta 事件（D04：完整后才 yield，应只有 1 个）
    tc_events = [e for e in events if e["type"] == "tool_call_delta"]
    assert len(tc_events) == 1
    tc = tc_events[0]["tool_call"]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "foo"
    assert tc["function"]["arguments"] == '{"a":1}'
    # done 事件
    done = [e for e in events if e["type"] == "done"]
    assert done[0]["finish_reason"] == "tool_calls"


def test_pool_stream_thinking_delta():
    """pool.stream() thinking_delta（reasoning_content + thinking 合并，D03）"""
    from server.llm_pool.pool import LLMPool

    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"thinking step 1"}}]}',
        'data: {"choices":[{"delta":{"thinking":" step 2"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    mock_key = _make_mock_key("openai")

    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=mock_key)
    pool._release = MagicMock()

    async def run():
        events = []
        async for event in pool.stream(messages=[{"role": "user", "content": "hi"}], project="test"):
            events.append(event)
        return events

    # T07：用 httpx.MockTransport + patch get_async_client 替代 patch requests.post
    mock_client = _make_mock_async_client(_make_sse_handler(sse_lines))
    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            events = asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())

    thinking_deltas = [e for e in events if e["type"] == "thinking_delta"]
    assert len(thinking_deltas) == 2
    assert thinking_deltas[0]["delta"] == "thinking step 1"
    assert thinking_deltas[1]["delta"] == " step 2"


def test_pool_stream_provider_error_release_key():
    """pool.stream() provider 错误时 key 释放（D14）"""
    from server.llm_pool.pool import LLMPool

    mock_key = _make_mock_key("openai")

    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=mock_key)
    pool._release = MagicMock()

    async def run():
        events = []
        async for event in pool.stream(messages=[{"role": "user", "content": "hi"}], project="test"):
            events.append(event)
        return events

    # T07：用 httpx.MockTransport + patch get_async_client 替代 patch requests.post
    # 429 状态码触发 provider_error 路径
    mock_client = _make_mock_async_client(_make_sse_handler([], status_code=429))
    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            events = asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())

    # provider_error + done
    types = [e["type"] for e in events]
    assert "provider_error" in types
    assert "done" in types
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "error"
    # key 释放（D14：即使错误也要 release）
    pool._release.assert_called_once()
    assert pool._release.call_args.kwargs.get("success") is False


def test_pool_stream_no_available_keys():
    """pool.stream() 无可用 key 时 yield error + done"""
    from server.llm_pool.pool import LLMPool

    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=None)
    pool._release = MagicMock()

    async def run():
        events = []
        async for event in pool.stream(messages=[{"role": "user", "content": "hi"}], project="test"):
            events.append(event)
        return events

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert "provider_error" in types
    assert "done" in types
    assert events[-1]["finish_reason"] == "error"
    # 无 key 时不应调 _release
    pool._release.assert_not_called()


if __name__ == "__main__":
    # 简单 runner（不依赖 pytest）
    import traceback
    tests = [
        test_tool_call_accumulator_single_call_multi_chunk,
        test_tool_call_accumulator_multiple_calls,
        test_tool_call_accumulator_empty,
        test_tool_call_accumulator_default_index,
        test_safe_next_line_normal,
        test_iter_lines_async_basic,
        test_pool_stream_openai_text_delta,
        test_pool_stream_openai_tool_call_accumulation,
        test_pool_stream_thinking_delta,
        test_pool_stream_provider_error_release_key,
        test_pool_stream_no_available_keys,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
