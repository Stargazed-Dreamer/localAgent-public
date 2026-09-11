"""流式 usage 采集器测试（2026-09-02「补齐流式 token 记账」，纯采集器不估算）。

覆盖：
- Anthropic 原生 SSE：message_start 的 input_tokens + message_delta 的**累计**
  output_tokens（含 message_start 先报 0、真实值在 message_delta 的经典场景）；
  tool_use 的 input_json_delta 半包累积；无 usage 时 provider_missing。
- OpenAI SSE：全零 usage chunk 过滤（代理常见）→ provider_missing；
  只给 prompt+completion 未给 total 时 total 回填为二者之和。

不依赖后端运行，全部 httpx.MockTransport。
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from server.llm_pool.pool import LLMPool  # noqa: E402


def _make_sse_handler(lines: list[str], status_code: int = 200):
    sse_bytes = b"".join(line.encode("utf-8") + b"\n" for line in lines)
    err_body = b"error body" if status_code != 200 else b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "text/event-stream"},
            content=sse_bytes if status_code == 200 else err_body,
        )
    return handler


def _make_mock_key(protocol: str) -> MagicMock:
    key = MagicMock()
    key.name = "test_key"
    key.protocol = protocol
    key.key = "sk-test"
    key.base_url = "https://api.test.com/v1"
    key.models = ["test-model"]
    key.default_model = "test-model"
    key.get_best_model_in_range = MagicMock(return_value="test-model")
    return key


def _collect_stream(pool: LLMPool, sse_lines: list[str]) -> list[dict]:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_make_sse_handler(sse_lines)))

    async def run():
        events = []
        async for ev in pool.stream(
            messages=[{"role": "user", "content": "hi"}], project="test"):
            events.append(ev)
        return events

    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            return asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())


def _new_pool(protocol: str):
    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=_make_mock_key(protocol))
    pool._release = MagicMock()
    pool._record_usage = MagicMock()
    return pool


def _usage_of(events: list[dict]) -> dict:
    usages = [e for e in events if e["type"] == "usage"]
    assert len(usages) == 1, f"期望恰好 1 条 usage 事件，实际 {len(usages)}"
    return usages[0]


# --------------------------- Anthropic 原生 SSE ---------------------------

def test_anthropic_stream_usage_from_start_and_cumulative_delta():
    """message_start 报 input=12/output=0，真实 output 累计在 message_delta=5 → 12+5=17。"""
    sse = [
        'event: message_start',
        'data: {"type":"message_start","message":{"id":"m1","model":"claude-x","usage":{"input_tokens":12,"output_tokens":0}}}',
        'event: content_block_start',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
        'event: content_block_stop',
        'data: {"type":"content_block_stop","index":0}',
        'event: message_delta',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}',
        'event: message_stop',
        'data: {"type":"message_stop"}',
    ]
    pool = _new_pool("anthropic")
    events = _collect_stream(pool, sse)

    types = [e["type"] for e in events]
    assert types.count("text_delta") == 2
    usage = _usage_of(events)
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 17
    assert usage["source"] == "provider"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "end_turn"
    # 池侧记账补齐
    pool._release.assert_called_once()
    assert pool._release.call_args.kwargs.get("tokens") == 17
    pool._record_usage.assert_called_once()
    assert pool._record_usage.call_args.args[1]["total_tokens"] == 17


def test_anthropic_stream_tool_input_json_accumulation():
    """tool_use 参数 input_json_delta 半包累积成完整 OpenAI 格式 tool_calls。"""
    sse = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":20,"output_tokens":1}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"get_weather"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\"Beijing\\"}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":8}}',
        'data: {"type":"message_stop"}',
    ]
    pool = _new_pool("anthropic")
    events = _collect_stream(pool, sse)

    tc_events = [e for e in events if e["type"] == "tool_call_delta"]
    assert len(tc_events) == 1
    tc = tc_events[0]["tool_call"]
    assert tc["id"] == "toolu_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == '{"city":"Beijing"}'
    usage = _usage_of(events)
    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 8
    assert usage["total_tokens"] == 28
    assert usage["source"] == "provider"


def test_anthropic_stream_no_usage_marks_provider_missing():
    """provider 全程不给 usage → token 记 0、标 provider_missing，但字符数仍精确。"""
    sse = [
        'data: {"type":"message_start","message":{}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        'data: {"type":"message_stop"}',
    ]
    pool = _new_pool("anthropic")
    events = _collect_stream(pool, sse)

    usage = _usage_of(events)
    assert usage["total_tokens"] == 0
    assert usage["source"] == "provider_missing"
    assert usage["completion_chars"] == len("hi")
    # 无 usage 时 release tokens=0，仍不调用 project 记账写入非零（total=0）
    assert pool._release.call_args.kwargs.get("tokens") == 0


def test_anthropic_stream_error_event():
    """流中 error 事件 → provider_error + done(error)，key 以 success=False 释放。"""
    sse = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
        'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    ]
    pool = _new_pool("anthropic")
    events = _collect_stream(pool, sse)

    assert any(e["type"] == "provider_error" for e in events)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "error"
    pool._release.assert_called_once()
    assert pool._release.call_args.kwargs.get("success") is False


# --------------------------- OpenAI 采集器分支 ---------------------------

def test_openai_stream_all_zero_usage_filtered_to_missing():
    """代理常在流里塞全零 usage chunk → 过滤后视为 provider_missing（记 0，不臆造）。"""
    sse = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}',
        'data: [DONE]',
    ]
    pool = _new_pool("openai")
    events = _collect_stream(pool, sse)

    usage = _usage_of(events)
    assert usage["total_tokens"] == 0
    assert usage["source"] == "provider_missing"


def test_openai_stream_usage_total_filled_from_parts():
    """provider 只给 prompt+completion 未给 total → total 回填为二者之和。"""
    sse = [
        'data: {"choices":[{"delta":{"content":"x"}}]}',
        'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4}}',
        'data: [DONE]',
    ]
    pool = _new_pool("openai")
    events = _collect_stream(pool, sse)

    usage = _usage_of(events)
    assert usage["prompt_tokens"] == 3
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 7
    assert usage["source"] == "provider"


def test_openai_stream_usage_in_trailing_empty_choices_chunk():
    """回归（opencode/Copilot 等真实场景）：OpenAI 规范把 usage 放在 finish_reason **之后**
    的独立空-choices 尾 chunk。历史 bug：看到 finish_reason 就 break，读不到尾 chunk，
    把明明给了 provider usage 的流误记 provider_missing。修复后须捕获尾部 usage。"""
    sse = [
        'data: {"choices":[{"delta":{"content":"He","reasoning_content":"think"}}]}',
        'data: {"choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":75,"completion_tokens":77,"total_tokens":152,'
        '"completion_tokens_details":{"reasoning_tokens":70}}}',
        'data: [DONE]',
    ]
    pool = _new_pool("openai")
    events = _collect_stream(pool, sse)

    # 思维仍逐字流出（修复不能牺牲直播思维）
    assert sum(1 for e in events if e["type"] == "thinking_delta") == 1
    usage = _usage_of(events)
    assert usage["source"] == "provider"
    assert usage["prompt_tokens"] == 75
    assert usage["completion_tokens"] == 77
    assert usage["total_tokens"] == 152
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "stop"
    # 池侧记账补齐为真实 total
    assert pool._release.call_args.kwargs.get("tokens") == 152


def test_openai_stream_finish_without_any_usage_marks_missing():
    """provider 只发 finish_reason + [DONE]、全程无 usage → 仍 provider_missing（记 0），
    且不因不再在 finish_reason break 而漏标 done / 挂死。"""
    sse = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    pool = _new_pool("openai")
    events = _collect_stream(pool, sse)

    usage = _usage_of(events)
    assert usage["source"] == "provider_missing"
    assert usage["total_tokens"] == 0
    assert usage["completion_chars"] == len("hi")
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "stop"


if __name__ == "__main__":
    import traceback
    tests = [
        test_anthropic_stream_usage_from_start_and_cumulative_delta,
        test_anthropic_stream_tool_input_json_accumulation,
        test_anthropic_stream_no_usage_marks_provider_missing,
        test_anthropic_stream_error_event,
        test_openai_stream_all_zero_usage_filtered_to_missing,
        test_openai_stream_usage_total_filled_from_parts,
        test_openai_stream_usage_in_trailing_empty_choices_chunk,
        test_openai_stream_finish_without_any_usage_marks_missing,
    ]
    passed = failed = 0
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
