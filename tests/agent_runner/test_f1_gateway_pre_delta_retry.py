"""LLMPoolGateway.stream() 预增量重试（2026-09-12 通宵调试新增）。

背景：实测中 provider 上游瞬时不可用（HTTP 400 upstream failed / 连接异常）会直接
以 "[stream error]" 终止整轮对话，对用户表现为"不太能用"。修复：首个 delta 到达前的
失败在 gateway 层静默重试（pool 服务端会换 key/提供商）；已产出增量后不重试（防重复输出）。

覆盖：
- F1-1: 非 200（上游 400）→ 重试后 200 + 正常流 → runner 只看到成功流
- F1-2: 3 次全部失败 → 最终仍 yield provider_error + done（error-as-output-variant 兜底不丢）
- F1-3: 已产出增量后收到 provider_error → 照旧透传（不重试，保留已累积文本）
- F1-4: 413 context overflow → 立即透传 context_overflow，不重试
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from client.core.agent.llm_pool_gateway import (
    LLMPoolGateway,
    SSE_EVENT_CONTEXT_OVERFLOW,
    SSE_EVENT_DONE,
    SSE_EVENT_PROVIDER_ERROR,
    SSE_EVENT_TEXT_DELTA,
)


def _make_gateway() -> LLMPoolGateway:
    return LLMPoolGateway(
        base_url="http://127.0.0.1:8766",
        stream_idle_timeout_secs=0,
        stall_detection_window_secs=0,
    )


def _sse_response(lines: list[str], status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if status_code == 200:
        resp.iter_lines = lambda decode_unicode=True: iter(lines)
    else:
        resp.text = json.dumps(
            {"error": {"type": "server_error", "message": "Upstream request failed"}}
        )
    return resp


def _sse_lines(*events: dict[str, Any]) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


def _request() -> Any:
    from client.core.agent.types import LLMRequest

    return LLMRequest(messages=[], model="")


def _run(gen) -> list[dict]:
    async def _collect():
        return [ev async for ev in gen]

    return asyncio.run(_collect())


def test_retry_after_http_400_succeeds() -> None:
    """F1-1: 第一次 400（上游故障）→ 第二次 200 正常流，最终只有成功流的事件。"""
    gw = _make_gateway()
    responses = [
        _sse_response([], status_code=400),
        _sse_response(
            _sse_lines(
                {"type": SSE_EVENT_TEXT_DELTA, "delta": "你好"},
                {"type": SSE_EVENT_DONE, "finish_reason": "stop"},
            )
        ),
    ]
    with patch.object(gw._session, "post", side_effect=responses) as mock_post:
        events = _run(gw.stream(_request()))
    assert mock_post.call_count == 2
    types = [e["type"] for e in events]
    assert SSE_EVENT_PROVIDER_ERROR not in types
    assert events[-1]["type"] == SSE_EVENT_DONE
    assert any(e.get("delta") == "你好" for e in events if e["type"] == SSE_EVENT_TEXT_DELTA)


def test_retry_exhausted_yields_provider_error() -> None:
    """F1-2: 三次全部 400 → 最终仍 yield provider_error + done（不吞错误）。"""
    gw = _make_gateway()
    with patch.object(gw._session, "post", return_value=_sse_response([], status_code=400)) as mp:
        events = _run(gw.stream(_request()))
    assert mp.call_count == 3
    assert events[-2]["type"] == SSE_EVENT_PROVIDER_ERROR
    assert "HTTP 400" in events[-2]["error"]
    assert events[-1]["type"] == SSE_EVENT_DONE


def test_post_delta_provider_error_not_retried() -> None:
    """F1-3: 已产出 text_delta 后收到 provider_error → 透传（不重试，防重复输出）。"""
    gw = _make_gateway()
    resp = _sse_response(
        _sse_lines(
            {"type": SSE_EVENT_TEXT_DELTA, "delta": "部分"},
            {"type": SSE_EVENT_PROVIDER_ERROR, "error": "mid-stream stall"},
            {"type": SSE_EVENT_DONE, "finish_reason": "error"},
        )
    )
    with patch.object(gw._session, "post", return_value=resp) as mp:
        events = _run(gw.stream(_request()))
    assert mp.call_count == 1
    assert any(e["type"] == SSE_EVENT_PROVIDER_ERROR for e in events)
    assert events[-1]["type"] == SSE_EVENT_DONE


def test_context_overflow_not_retried() -> None:
    """F1-4: 413 → 立即透传 context_overflow（重试无意义）。"""
    gw = _make_gateway()
    resp = MagicMock()
    resp.status_code = 413
    resp.text = "context length exceeded"
    with patch.object(gw._session, "post", return_value=resp) as mp:
        events = _run(gw.stream(_request()))
    assert mp.call_count == 1
    assert any(e["type"] == SSE_EVENT_CONTEXT_OVERFLOW for e in events)
    assert events[-1]["type"] == SSE_EVENT_DONE


def test_pre_delta_provider_error_retried() -> None:
    """F1-5: 200 连接成功但流开始前 provider_error → 静默重试后成功。"""
    gw = _make_gateway()
    responses = [
        _sse_response(_sse_lines({"type": SSE_EVENT_PROVIDER_ERROR, "error": "idle timeout"}),
                      status_code=200),
        _sse_response(
            _sse_lines(
                {"type": SSE_EVENT_TEXT_DELTA, "delta": "ok"},
                {"type": SSE_EVENT_DONE, "finish_reason": "stop"},
            )
        ),
    ]
    with patch.object(gw._session, "post", side_effect=responses) as mp:
        events = _run(gw.stream(_request()))
    assert mp.call_count == 2
    assert not any(e["type"] == SSE_EVENT_PROVIDER_ERROR for e in events)
    assert events[-1]["type"] == SSE_EVENT_DONE


@pytest.mark.parametrize("attr", ["stream_idle_timeout_secs", "stall_detection_window_secs"])
def test_watchdog_defaults(attr: str) -> None:
    """默认值契约：idle 90s 保留，stall 默认禁用（推理模型静默思考不被误杀）。"""
    gw = LLMPoolGateway(base_url="http://127.0.0.1:8766")
    expected = 90 if attr == "stream_idle_timeout_secs" else 0
    assert getattr(gw, attr) == expected
