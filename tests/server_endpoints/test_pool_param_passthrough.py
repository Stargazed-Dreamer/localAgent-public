"""pool 参数透传测试（2026-09-03「中转只做透传，不替调用方写死参数」）。

背景：pool.call()/stream() 原先把 temperature=0.3 / max_tokens=4096 写进签名默认值，
且无条件塞进 request_body —— 入站网关想"客户端没传就别下发"做不到，只能自己编一个
兜底值，等价于通道层替调用方决定采样参数与输出预算。

现口径：None = 不下发该字段，由 provider 用自己的默认值；调用方显式传值则原样透传。
唯 Anthropic 例外 —— /v1/messages 的 max_tokens 是 API 必填字段，省略即 400，
故未指定时回退 ANTHROPIC_REQUIRED_MAX_TOKENS。

不依赖后端运行，全部 httpx.MockTransport / MagicMock。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from server.llm_pool.pool import ANTHROPIC_REQUIRED_MAX_TOKENS, LLMPool  # noqa: E402
from server.llm_pool.types import ProviderPolicy  # noqa: E402

# --------------------------- 非流式：请求体捕获 ---------------------------

_OK_RESPONSE = {
    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    "model": "test-model",
}


def _make_mock_key(protocol: str) -> MagicMock:
    key = MagicMock()
    key.name = "test_key"
    key.protocol = protocol
    key.key = "sk-test"
    key.base_url = "https://api.test.com/v1"
    key.models = ["test-model"]
    key.default_model = "test-model"
    key.get_best_model_in_range = MagicMock(return_value="test-model")
    # 必须是真实容器：call() 会迭代 .items()，MagicMock 不可迭代
    key.model_cooldowns = {}
    key.model_disabled = {}
    key.model_tiers = {}
    return key


def _new_pool(protocol: str) -> LLMPool:
    pool = LLMPool.__new__(LLMPool)
    pool._acquire = MagicMock(return_value=_make_mock_key(protocol))
    pool._release = MagicMock()
    pool._record_usage = MagicMock()
    pool._record_call = MagicMock()
    # call() 会读 default_policy.retry_count 作为尝试次数（range(retry_count)），
    # 绕开 __init__ 就得手动补上；必须 >=1，否则循环一次都不跑，请求根本发不出去。
    pool.default_policy = ProviderPolicy(name="test", retry_count=1)
    # 压缩/健康探针配置（call() 路径会读，绕开 __init__ 需手动补齐）
    pool._compression_mode = "off"
    pool._compression_min_length = 2000
    pool._compression_saved_chars_total = 0
    pool._compression_stats_total = 0
    pool._model_health_enabled = False  # 跳过 disabled-model 探针分支
    pool._model_health_probe_interval = 300.0
    pool._model_health_probe_max_concurrency = 1
    return pool


def _call_openai_capture(**kwargs) -> dict:
    """跑一次 pool.call()（OpenAI 协议），返回实际发出的 request_body。"""
    captured: dict = {}

    def fake_post(url, headers=None, json=None, **kw):
        captured["body"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _OK_RESPONSE
        return resp

    pool = _new_pool("openai")
    client = MagicMock()
    client.post = fake_post
    with patch("server.llm_pool.pool.get_sync_client", return_value=client):
        pool.call(messages=[{"role": "user", "content": "hi"}], project="test", **kwargs)
    return captured.get("body") or {}


class TestNonStreamPassthrough:
    """非流式 request_body 的字段存在性。"""

    def test_unspecified_params_are_omitted(self):
        """不传 temperature/max_tokens → 请求体里根本不出现这两个字段。"""
        body = _call_openai_capture()
        assert "temperature" not in body, "未指定时不应下发 temperature"
        assert "max_tokens" not in body, "未指定时不应下发 max_tokens"
        # 结构性字段必须还在，否则请求本身就不成立
        assert body["model"] == "test-model"
        assert body["stream"] is False

    def test_explicit_params_passed_through_verbatim(self):
        """客户端给多少就发多少，池不夹取、不上限裁剪。"""
        body = _call_openai_capture(temperature=0.9, max_tokens=65536)
        assert body["temperature"] == 0.9
        assert body["max_tokens"] == 65536

    def test_extreme_max_tokens_not_clamped(self):
        """历史上 4096 会把 reasoning 模型的思考链截断；现在必须能透传大值。"""
        body = _call_openai_capture(max_tokens=131072)
        assert body["max_tokens"] == 131072

    def test_zero_is_preserved_not_treated_as_unset(self):
        """0 是合法取值（greedy 解码），不能被当成「未指定」丢掉。"""
        body = _call_openai_capture(temperature=0.0)
        assert body["temperature"] == 0.0


class TestAnthropicRequiredMaxTokens:
    """Anthropic 把 max_tokens 列为必填，省略会 400 —— 必须回退而不是省略。"""

    def test_anthropic_falls_back_when_unspecified(self):
        captured: dict = {}

        def fake_post(url, headers=None, json=None, **kw):
            captured["body"] = json
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "model": "claude-x",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            return resp

        pool = _new_pool("anthropic")
        client = MagicMock()
        client.post = fake_post
        with patch("server.llm_pool.pool.get_sync_client", return_value=client):
            pool.call(messages=[{"role": "user", "content": "hi"}], project="test")

        body = captured.get("body") or {}
        assert "max_tokens" in body, "Anthropic 必填字段，绝不能省略"
        assert body["max_tokens"] == ANTHROPIC_REQUIRED_MAX_TOKENS
        assert "temperature" not in body, "temperature 非必填，未指定时不下发"

    def test_anthropic_respects_explicit_value(self):
        captured: dict = {}

        def fake_post(url, headers=None, json=None, **kw):
            captured["body"] = json
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "model": "claude-x",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            return resp

        pool = _new_pool("anthropic")
        client = MagicMock()
        client.post = fake_post
        with patch("server.llm_pool.pool.get_sync_client", return_value=client):
            pool.call(messages=[{"role": "user", "content": "hi"}],
                      max_tokens=1024, project="test")

        assert captured["body"]["max_tokens"] == 1024


# --------------------------- 流式 ---------------------------

_SSE_MINIMAL = [
    'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
    'data: [DONE]',
]


def _stream_capture(**kwargs) -> dict:
    """跑一次 pool.stream()（OpenAI 协议），返回实际发出的 request_body。"""
    captured: dict = {}
    sse_bytes = b"".join(line.encode("utf-8") + b"\n" for line in _SSE_MINIMAL)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_bytes)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        async for _ in (await _stream_gen(kwargs)):
            pass

    async def _stream_gen(kw):
        return _new_pool("openai").stream(
            messages=[{"role": "user", "content": "hi"}], project="test", **kw)

    try:
        with patch("server.llm_pool.pool.get_async_client", return_value=mock_client):
            asyncio.run(run())
    finally:
        asyncio.run(mock_client.aclose())
    return captured.get("body") or {}


class TestStreamPassthrough:
    """流式与非流式必须同口径（此前流式是另一套硬编码默认值）。"""

    def test_stream_omits_unspecified_params(self):
        body = _stream_capture()
        assert "temperature" not in body
        assert "max_tokens" not in body
        assert body["stream"] is True

    def test_stream_response_format_is_forwarded(self):
        """流式签名原本没有 response_format，客户端传了会被静默丢弃。"""
        body = _stream_capture(response_format={"type": "json_object"})
        assert body["response_format"] == {"type": "json_object"}

    def test_stream_passthrough_verbatim(self):
        body = _stream_capture(temperature=0.7, max_tokens=32768)
        assert body["temperature"] == 0.7
        assert body["max_tokens"] == 32768
