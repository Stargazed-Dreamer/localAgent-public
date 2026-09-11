"""契约测试：provider 真实形态样本 → 字段全链存活断言（防"白名单归一丢字段"复发）。

背景（2026-09-08 字段审计）：usage/内容从 provider 到落库要过 5 道手工白名单
（池解析 → collect_usage 归一 → 网关 _norm_usage → 响应重建 → DB 列），
每道都是"没列出的就静默丢"，历史上先后丢过 cache_read/reasoning/cache_creation/
thinking/finish_reason/max_completion_tokens。本文件固化 4 种协议形态
（OpenAI/Anthropic × 流式/非流式）的真实样本，逐层断言观测字段存活，
并覆盖 DB 迁移 roundtrip 与 usage_json 兜底落盘。

不依赖后端运行，全部 mock httpx（同步 post / 异步 stream）。
"""

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from lib.llm_usage import collect_usage, collect_usage_anthropic  # noqa: E402
from server.inbound_gateway.call_log import CallLog  # noqa: E402
from server.inbound_gateway.router import (  # noqa: E402
    _build_pool_kwargs, _norm_usage, _sse_usage_chunk)
from server.llm_pool import pool as pool_mod  # noqa: E402
from server.llm_pool.pool import LLMPool  # noqa: E402
from server.llm_pool.types import LLMKey  # noqa: E402

# --------------------------- 真实形态样本 fixture ---------------------------

# OpenAI 非流式：思考模型（DeepSeek-R1 系）响应，含 reasoning_content + usage 明细
OPENAI_NONSTREAM = {
    "model": "test-model",
    "choices": [{
        "message": {"content": "答案", "reasoning_content": "思考过程"},
        "finish_reason": "stop",
    }],
    "usage": {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 30},
    },
}

# OpenAI 流式：正文 delta + finish_reason chunk + 独立尾部 usage chunk（OpenAI 标准姿势）
OPENAI_STREAM_LINES = [
    'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"index":0,"delta":{"content":"你好"}}]}',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,'
    '"total_tokens":150,"prompt_tokens_details":{"cached_tokens":80},'
    '"completion_tokens_details":{"reasoning_tokens":30}}}',
    "data: [DONE]",
]

# Anthropic 非流式：extended thinking（thinking 块 + 缓存读/写 usage）
ANTHROPIC_NONSTREAM = {
    "model": "claude-test",
    "content": [
        {"type": "thinking", "thinking": "先想一下"},
        {"type": "text", "text": "答"},
    ],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 100, "output_tokens": 50,
              "cache_read_input_tokens": 60, "cache_creation_input_tokens": 40},
}

# Anthropic 流式：message_start 带缓存 usage，thinking/text delta，message_delta 带累计 output
ANTHROPIC_STREAM_LINES = [
    'data: {"type":"message_start","message":{"model":"claude-test","usage":'
    '{"input_tokens":100,"output_tokens":1,"cache_read_input_tokens":60,'
    '"cache_creation_input_tokens":40}}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"想"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"答"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":50}}',
    'data: {"type":"message_stop"}',
]


def _openai_key() -> LLMKey:
    return LLMKey(key="sk-test", base_url="https://api.test.com/v1",
                  models=["test-model"], name="test-openai", protocol="openai")


def _anthropic_key() -> LLMKey:
    return LLMKey(key="sk-test", base_url="https://api.test.com/v1",
                  models=["claude-test"], name="test-anthropic", protocol="anthropic")


def _new_pool() -> LLMPool:
    pool = LLMPool.__new__(LLMPool)
    pool._release = lambda *a, **k: None
    pool._record_usage = lambda *a, **k: None
    pool._record_call = lambda *a, **k: None
    return pool


class _FakeSyncResp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.headers: dict = {}
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeSyncClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.captured: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.captured.append({"url": url, "headers": headers, "json": json})
        return _FakeSyncResp(self.payload)


class _FakeStreamResp:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200
        self.headers: dict = {}

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCM:
    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self) -> _FakeStreamResp:
        return _FakeStreamResp(self._lines)

    async def __aexit__(self, *a) -> bool:
        return False


class _FakeAsyncClient:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.captured: list[dict] = []

    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.captured.append({"url": url, "headers": headers})
        return _FakeStreamCM(self._lines)


_MESSAGES = [{"role": "user", "content": "问题"}]


# --------------------------- 1. 归一层（lib/llm_usage） ---------------------------

def test_collect_usage_openai_preserves_details():
    u = collect_usage(OPENAI_NONSTREAM["usage"], "p", "c")
    assert u["cached_tokens"] == 80
    assert u["reasoning_tokens"] == 30
    assert u["source"] == "provider"
    assert (u["prompt_tokens"], u["completion_tokens"], u["total_tokens"]) == (100, 50, 150)


def test_collect_usage_missing_reports_none_not_zero():
    u = collect_usage(None, "p", "c")
    assert u["cached_tokens"] is None and u["reasoning_tokens"] is None
    assert u["source"] == "provider_missing"


def test_collect_usage_anthropic_preserves_cache_write():
    u = collect_usage_anthropic(100, 50, "p", "c",
                                cached_tokens=60, cache_creation_tokens=40)
    assert u["cached_tokens"] == 60
    assert u["cache_creation_tokens"] == 40
    # Anthropic 无 reasoning 概念（thinking 已计入 output_tokens）
    assert u["reasoning_tokens"] is None


# --------------------------- 2. 池层非流式 ---------------------------

def test_call_openai_thinking_and_usage_raw(monkeypatch):
    fake = _FakeSyncClient(OPENAI_NONSTREAM)
    monkeypatch.setattr(pool_mod, "get_sync_client", lambda: fake)
    pool = _new_pool()
    result = pool._call_openai(
        key=_openai_key(), req_model="test-model", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None, session_id=None)
    assert result["ok"] is True
    # 思考内容存活（此前 message.reasoning_content 被静默丢弃）
    assert result["thinking"] == "思考过程"
    # provider 原始 usage 原样透出（usage_json 兜底落盘的数据源）
    assert result["usage_raw"]["completion_tokens_details"]["reasoning_tokens"] == 30
    assert result["finish_reason"] == "stop"


def test_call_anthropic_thinking_blocks_and_cache_write(monkeypatch):
    fake = _FakeSyncClient(ANTHROPIC_NONSTREAM)
    monkeypatch.setattr(pool_mod, "get_sync_client", lambda: fake)
    pool = _new_pool()
    result = pool._call_anthropic(
        key=_anthropic_key(), req_model="claude-test", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None, session_id=None)
    assert result["ok"] is True
    # thinking 块存活（此前被静默丢弃：客户端拿不到思考、chars 少记）
    assert result["thinking"] == "先想一下"
    assert result["content"] == "答"
    assert result["usage"]["cache_creation_tokens"] == 40
    assert result["usage"]["cached_tokens"] == 60
    assert result["usage_raw"]["cache_creation_input_tokens"] == 40
    assert result["finish_reason"] == "end_turn"


# --------------------------- 3. 池层流式 ---------------------------

def test_stream_openai_usage_event_survives_details(monkeypatch):
    fake = _FakeAsyncClient(OPENAI_STREAM_LINES)
    monkeypatch.setattr(pool_mod, "get_async_client", lambda: fake)
    pool = _new_pool()
    gen = pool._stream_openai_sse(
        key=_openai_key(), req_model="test-model", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        project="default", attempt_start_ts=time.time(), session_id=None)

    async def _drive():
        return [ev async for ev in gen]

    events = asyncio.run(_drive())
    usage_evt = next(e for e in events if e.get("type") == "usage")
    assert usage_evt["cached_tokens"] == 80
    assert usage_evt["reasoning_tokens"] == 30
    assert usage_evt["prompt_tokens"] == 100
    # raw 键：provider 原始 usage（网关 usage_json 兜底落盘数据源）
    assert usage_evt["raw"]["total_tokens"] == 150
    done = next(e for e in events if e.get("type") == "done")
    assert done["finish_reason"] == "stop"


def test_stream_anthropic_cache_write_and_raw_merge(monkeypatch):
    fake = _FakeAsyncClient(ANTHROPIC_STREAM_LINES)
    monkeypatch.setattr(pool_mod, "get_async_client", lambda: fake)
    pool = _new_pool()
    gen = pool._stream_anthropic_sse(
        key=_anthropic_key(), req_model="claude-test", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, project="default",
        attempt_start_ts=time.time(), tools=None, tool_choice=None, session_id=None)

    async def _drive():
        return [ev async for ev in gen]

    events = asyncio.run(_drive())
    thinking = [e for e in events if e.get("type") == "thinking_delta"]
    assert thinking and thinking[0]["delta"] == "想"
    usage_evt = next(e for e in events if e.get("type") == "usage")
    assert usage_evt["cached_tokens"] == 60
    assert usage_evt["cache_creation_tokens"] == 40
    # raw 合并视图：message_start 的 input/cache + message_delta 的累计 output
    assert usage_evt["raw"]["input_tokens"] == 100
    assert usage_evt["raw"]["output_tokens"] == 50
    done = next(e for e in events if e.get("type") == "done")
    assert done["finish_reason"] == "end_turn"


# --------------------------- 4. 网关层 ---------------------------

_GATEWAY_BODY = {"messages": _MESSAGES, "model": "m1"}
_GATEWAY_KEY = {"key_id": "inb_x", "name": "n", "project": "p"}


def test_gateway_max_completion_tokens_fallback():
    # 只发新字段名（新版 OpenAI SDK / o 系模型）→ 不再被静默丢弃
    kw = _build_pool_kwargs(_GATEWAY_KEY, "m1",
                            {**_GATEWAY_BODY, "max_completion_tokens": 777}, None, None)
    assert kw["max_tokens"] == 777
    # 两个字段都发 → max_tokens 优先（OpenAI 语义：max_tokens 是旧名，客户端显式给了就尊重）
    kw2 = _build_pool_kwargs(_GATEWAY_KEY, "m1",
                             {**_GATEWAY_BODY, "max_tokens": 11,
                              "max_completion_tokens": 777}, None, None)
    assert kw2["max_tokens"] == 11
    # 都不发 → 兜底
    kw3 = _build_pool_kwargs(_GATEWAY_KEY, "m1", _GATEWAY_BODY, None, None)
    assert kw3["max_tokens"] == 16384


def test_gateway_norm_usage_six_tuple():
    p, c, t, cached, reasoning, ccreation = _norm_usage({
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "cached_tokens": 80, "reasoning_tokens": 30, "cache_creation_tokens": 40})
    assert (p, c, t, cached, reasoning, ccreation) == (100, 50, 150, 80, 30, 40)
    assert _norm_usage(None) == (0, 0, 0, None, None, None)


def test_gateway_sse_usage_chunk_details():
    chunk = json.loads(_sse_usage_chunk("id", 1, "m", 100, 50, 150, 80, 30)
                       .removeprefix("data: ").strip())
    assert chunk["usage"]["prompt_tokens_details"] == {"cached_tokens": 80}
    assert chunk["usage"]["completion_tokens_details"] == {"reasoning_tokens": 30}
    # 未上报 → 不造明细键
    chunk2 = json.loads(_sse_usage_chunk("id", 1, "m", 100, 50, 150)
                        .removeprefix("data: ").strip())
    assert "prompt_tokens_details" not in chunk2["usage"]
    assert "completion_tokens_details" not in chunk2["usage"]


# --------------------------- 6. A 档采样参数白名单（2026-09-08 扩展） ---------------------------

def test_gateway_whitelist_params_passthrough():
    """客户端发的 A 档参数 → kwargs 原样；没发 → None（不下发）。"""
    body = {**_GATEWAY_BODY, "top_p": 0.9, "stop": ["END"], "seed": 42,
            "presence_penalty": 0.3, "frequency_penalty": 0.5,
            "parallel_tool_calls": False}
    kw = _build_pool_kwargs(_GATEWAY_KEY, "m1", body, None, None)
    assert kw["top_p"] == 0.9
    assert kw["stop"] == ["END"]
    assert kw["seed"] == 42
    assert kw["presence_penalty"] == 0.3
    assert kw["frequency_penalty"] == 0.5
    assert kw["parallel_tool_calls"] is False
    kw2 = _build_pool_kwargs(_GATEWAY_KEY, "m1", _GATEWAY_BODY, None, None)
    assert all(kw2[k] is None for k in
               ("top_p", "stop", "presence_penalty", "frequency_penalty",
                "seed", "parallel_tool_calls"))


def test_call_openai_whitelist_params_in_body(monkeypatch):
    """池 OpenAI 协议：A 档参数进请求体；None 不下发。"""
    fake = _FakeSyncClient(OPENAI_NONSTREAM)
    monkeypatch.setattr(pool_mod, "get_sync_client", lambda: fake)
    pool = _new_pool()
    pool._call_openai(
        key=_openai_key(), req_model="test-model", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None,
        top_p=0.9, stop=["END"], presence_penalty=0.3, frequency_penalty=0.5,
        seed=42, parallel_tool_calls=False, session_id=None)
    body = fake.captured[0]["json"]
    assert body["top_p"] == 0.9
    assert body["stop"] == ["END"]
    assert body["presence_penalty"] == 0.3
    assert body["frequency_penalty"] == 0.5
    assert body["seed"] == 42
    assert body["parallel_tool_calls"] is False
    # None = 不下发
    pool._call_openai(
        key=_openai_key(), req_model="test-model", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None, session_id=None)
    body2 = fake.captured[1]["json"]
    assert all(k not in body2 for k in
               ("top_p", "stop", "presence_penalty", "frequency_penalty",
                "seed", "parallel_tool_calls"))


def test_call_anthropic_whitelist_params_mapping(monkeypatch):
    """池 Anthropic 协议：top_p 直传、stop→stop_sequences、OpenAI 独有参数不下发。"""
    fake = _FakeSyncClient(ANTHROPIC_NONSTREAM)
    monkeypatch.setattr(pool_mod, "get_sync_client", lambda: fake)
    pool = _new_pool()
    pool._call_anthropic(
        key=_anthropic_key(), req_model="claude-test", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None,
        top_p=0.9, stop="END", presence_penalty=0.3, frequency_penalty=0.5,
        seed=42, parallel_tool_calls=False, session_id=None)
    body = fake.captured[0]["json"]
    assert body["top_p"] == 0.9
    # str stop → stop_sequences 单元素列表
    assert body["stop_sequences"] == ["END"]
    # OpenAI 独有：Anthropic 无对应概念，不下发
    assert all(k not in body for k in
               ("stop", "presence_penalty", "frequency_penalty",
                "seed", "parallel_tool_calls"))
    # list stop 原样转列表
    pool._call_anthropic(
        key=_anthropic_key(), req_model="claude-test", messages=_MESSAGES,
        temperature=None, max_tokens=100, timeout=30, response_format=None,
        attempt_start_ts=time.time(), attempt=0, failed_models=set(),
        project="default", tools=None, tool_choice=None,
        top_p=None, stop=["A", "B"], session_id=None)
    body2 = fake.captured[1]["json"]
    assert body2["stop_sequences"] == ["A", "B"]
    assert "top_p" not in body2


# --------------------------- 5. 落库层（roundtrip + 迁移） ---------------------------

def test_call_log_roundtrip_new_fields(tmp_path):
    log = CallLog(tmp_path / "calls.db")
    entry = {"ts": time.time(), "key_id": "k1", "key_name": "n", "stream": False,
             "model_requested": "m1", "model_used": "m2", "status": "ok",
             "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
             "cached_tokens": 80, "reasoning_tokens": 30, "cache_creation_tokens": 40,
             "finish_reason": "stop", "session_id": "ocs-x"}
    log.record(entry)
    log.record_detail(entry, request_body="{}", response_body="ok",
                      response_type="non_stream", usage_json='{"input_tokens":100}')
    log.flush()
    row = log.query(key_id="k1")[0]
    assert row["cached_tokens"] == 80
    assert row["reasoning_tokens"] == 30
    assert row["cache_creation_tokens"] == 40
    assert row["finish_reason"] == "stop"
    conn = sqlite3.connect(tmp_path / "calls.db")
    try:
        d = conn.execute("SELECT usage_json, session_id FROM inbound_call_details").fetchone()
        assert d[0] == '{"input_tokens":100}'
        assert d[1] == "ocs-x"
    finally:
        conn.close()


def test_call_log_migration_from_pre_session_schema(tmp_path):
    """老库（无 session_id/cached_tokens 及更新列）启动迁移 roundtrip。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE inbound_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            key_id TEXT NOT NULL, key_name TEXT NOT NULL, stream INTEGER NOT NULL,
            model_requested TEXT NOT NULL, model_used TEXT NOT NULL,
            upstream_key TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
            ttft_ms INTEGER, ttft_kind TEXT DEFAULT '', duration_ms INTEGER DEFAULT 0,
            status TEXT NOT NULL, http_status INTEGER DEFAULT 200, retries INTEGER DEFAULT 0,
            client_ip TEXT DEFAULT '', error TEXT DEFAULT '');
        CREATE TABLE inbound_call_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            key_id TEXT NOT NULL, key_name TEXT NOT NULL, stream INTEGER NOT NULL,
            model_requested TEXT NOT NULL, model_used TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT DEFAULT '', request_body TEXT NOT NULL,
            response_body TEXT NOT NULL DEFAULT '',
            response_type TEXT NOT NULL DEFAULT 'non_stream',
            request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0, truncated_what TEXT NOT NULL DEFAULT '',
            stored_at REAL NOT NULL);
    """)
    conn.commit()
    conn.close()
    CallLog(db)  # _init_db 幂等补列
    conn = sqlite3.connect(db)
    try:
        main_cols = {r[1] for r in conn.execute("PRAGMA table_info(inbound_calls)")}
        detail_cols = {r[1] for r in conn.execute("PRAGMA table_info(inbound_call_details)")}
    finally:
        conn.close()
    assert {"session_id", "cached_tokens", "reasoning_tokens",
            "cache_creation_tokens", "finish_reason"} <= main_cols
    assert {"session_id", "usage_json"} <= detail_cols


def test_call_log_aggregate_includes_new_fields(tmp_path):
    log = CallLog(tmp_path / "agg.db")
    base = {"ts": time.time(), "key_id": "k1", "key_name": "n", "stream": False,
            "model_requested": "m1", "model_used": "m1", "status": "ok"}
    log.record({**base, "reasoning_tokens": 30, "cache_creation_tokens": 40})
    log.record({**base})  # 未上报 → None，不计入 samples
    log.flush()
    ov = log.aggregate(days=1)["overview"]
    assert ov["reasoning_tokens"] == 30 and ov["reasoning_samples"] == 1
    assert ov["cache_creation_tokens"] == 40 and ov["cache_creation_samples"] == 1
