"""入站网关 HTTP 端点测试（external behavior：状态码/响应形状/日志落库/SSE 序列）

用最小化 FastAPI app 只挂 inbound router（prior art: test_approval_panel_api.py），
pool 用 FakePool 替换（单元层允许 mock；P1/P2 的真实 pool 验收走 T6 curl + 真实后端，
spec Anti-Cheat 要求在 PROGRESS 注明）。

覆盖：
- 鉴权: 无头/错前缀/不存在/停用 → 401
- /v1/models: 池内模型 + 别名聚合
- 非流式: 200 + usage；别名解析透传；未知模型 404 带可用列表；上游失败 502；日志落库
- 流式: SSE chunk 序列 + [DONE]；TTFT 落库；usage 缺失记 0
- /inbound/keys CRUD；/inbound/calls 过滤；/inbound/stats 聚合形状
- GATEWAY_EXCLUDE 含全部 inbound operation_id
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.inbound_gateway.router as gw_mod  # 模块对象（__init__ 把 router 属性遮蔽成 APIRouter，勿混用）
from server import mcp_whitelist
from server.inbound_gateway import call_log, key_store
from server.inbound_gateway.router import router as gw_api_router

# ==================== fixtures ====================


class FakePool:
    """替身 pool：记录调用参数，回放注入的结果/事件。"""

    def __init__(self, models=("deepseek-v4-chat",), call_result=None, stream_events=()):
        self.models = set(models)
        self.call_result = call_result if call_result is not None else {
            "ok": True, "content": "hello",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "deepseek-v4-chat", "key_name": "modelscope_01",
            "tool_calls": [], "finish_reason": "stop",
        }
        self.stream_events = list(stream_events)
        self.calls: list[dict] = []
        self.methods: list[str] = []   # 记录被调用的是 call(非流式) 还是 stream(真流式)

    def list_models(self):
        return {
            "grouped_by_tier": {
                "tier3-medium": [
                    {"model": m, "tier": 3, "provider": "fake"} for m in sorted(self.models)
                ]
            },
            "default_tier": "tier3-medium", "default_model": "deepseek-v4-chat",
        }

    def call(self, **kw):
        self.methods.append("call")
        self.calls.append(kw)
        return self.call_result

    async def stream(self, **kw):
        self.methods.append("stream")
        self.calls.append(kw)
        for ev in self.stream_events:
            yield ev


@pytest.fixture()
def store(tmp_path):
    return key_store.reset_key_store(tmp_path / "keys.json")


@pytest.fixture()
def logdb(tmp_path):
    return call_log.reset_call_log(tmp_path / "calls.db")


@pytest.fixture()
def client(store, logdb, monkeypatch):
    app = FastAPI()
    app.include_router(gw_api_router)
    return TestClient(app)


def _make_key(store, name="cline-work", project="cline", aliases=None, enabled=True):
    return store.create(name=name, project=project, aliases=aliases or {}, enabled=enabled)


def _mount_pool(monkeypatch, pool):
    monkeypatch.setattr(gw_mod, "_get_pool", lambda: pool)


# ==================== 鉴权（P4） ====================


class TestAuth:
    def test_missing_header_401(self, client):
        r = client.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"

    def test_wrong_prefix_401(self, client):
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer sk-other-abc"},
                        json={"model": "m", "messages": []})
        assert r.status_code == 401

    def test_unknown_key_401(self, client):
        r = client.get("/v1/models", headers={"Authorization": "Bearer sk-la-doesnotexist0000"})
        assert r.status_code == 401

    def test_disabled_key_401(self, client, store):
        k = _make_key(store, enabled=False)
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 401

    def test_valid_key_models_200(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 200


# ==================== /v1/models（P5） ====================


class TestModels:
    def test_lists_pool_models_and_aliases(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        _mount_pool(monkeypatch, FakePool(models=("deepseek-v4-chat", "claude-3-5-sonnet")))
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        ids = {m["id"] for m in body["data"]}
        # 池内全量 + 别名聚合（别名指向的池内模型去重）
        assert {"deepseek-v4-chat", "claude-3-5-sonnet", "gpt-4o"} <= ids

    def test_empty_pool_still_lists_aliases(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        _mount_pool(monkeypatch, FakePool(models=()))
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 200
        assert {m["id"] for m in r.json()["data"]} == {"gpt-4o"}


# ==================== 非流式（P1 / P3） ====================


class TestChatNonStream:
    def _post(self, client, secret, model="deepseek-v4-chat", **extra):
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        body.update(extra)
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"}, json=body)

    def test_ok_with_usage(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        body = r.json()
        assert body["usage"]["total_tokens"] == 15
        assert body["choices"][0]["message"]["content"] == "hello"
        assert body["object"] == "chat.completion"

    def test_alias_resolved_before_pool(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        pool = FakePool()
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"], model="gpt-4o")
        assert r.status_code == 200
        assert pool.calls[0]["model"] == "deepseek-v4-chat"  # 池收到的是映射后模型

    def test_unknown_model_404_with_available_list(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(models=("deepseek-v4-chat",)))
        r = self._post(client, k["secret"], model="no-such-model")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "model_not_found"
        assert "deepseek-v4-chat" in r.json()["error"]["message"]  # body 带可用列表

    def test_upstream_failure_502_and_logged(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(call_result={"ok": False, "error": "boom"}))
        r = self._post(client, k["secret"])
        assert r.status_code == 502
        logdb.flush()  # record 异步入队，先同步刷库再断言
        rows = logdb.query(key_id=k["key_id"])
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert "boom" in rows[0]["error"]

    def test_call_logged_full_fields(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["key_name"] == "cline-work"
        assert row["model_requested"] == "deepseek-v4-chat"
        assert row["model_used"] == "deepseek-v4-chat"
        assert row["upstream_key"] == "modelscope_01"
        assert row["prompt_tokens"] == 10 and row["completion_tokens"] == 5
        assert row["usage_source"] == "provider"         # 采集器：拿到非零 usage
        assert row["prompt_chars"] == 2                  # 精确字符数（"hi"）
        assert row["completion_chars"] == 5              # 精确字符数（"hello"）
        assert row["ttft_ms"] is None          # 非流式 TTFT 为 NULL（决策#8）
        assert row["stream"] == 0
        assert row["status"] == "ok"
        assert row["duration_ms"] >= 0
        assert row["client_ip"]                # 127.0.0.1 由 TestClient 注入

    def test_missing_messages_400(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {k['secret']}"},
                        json={"model": "deepseek-v4-chat"})
        assert r.status_code == 400


# ==================== 流式（P2） ====================

STREAM_EVENTS = [
    {"type": "text_delta", "delta": "He"},
    {"type": "text_delta", "delta": "llo"},
    {"type": "usage", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
     "prompt_chars": 2, "completion_chars": 5, "source": "provider"},
    {"type": "done", "finish_reason": "stop"},
]


def _sse_chunks(r):
    """把 SSE 响应体拆成 data JSON chunk 列表（末尾 [DONE] 单独判定，不计入）。"""
    lines = [ln for ln in r.text.splitlines() if ln.startswith("data:")]
    assert lines[-1].strip() == "data: [DONE]"
    return [json.loads(ln[5:].strip()) for ln in lines[:-1]]


class TestChatStream:
    def _post(self, client, secret, model="deepseek-v4-chat", **extra):
        body = {"model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True}
        body.update(extra)
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"}, json=body)

    def test_sse_chunk_sequence(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(stream_events=STREAM_EVENTS))
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        data_lines = [ln for ln in r.text.splitlines() if ln.startswith("data:")]
        assert len(data_lines) > 1                       # P2: chunk 序列
        assert data_lines[-1].strip() == "data: [DONE]"  # 末尾 [DONE]
        chunks = [json.loads(ln[5:]) for ln in data_lines[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        contents = [c["choices"][0]["delta"].get("content") for c in chunks
                    if c.get("choices") and c["choices"][0]["delta"].get("content")]
        assert "".join(contents) == "Hello"

    def test_ttft_logged_and_usage(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(stream_events=STREAM_EVENTS))
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"], stream=True)
        assert len(rows) == 1
        assert rows[0]["ttft_ms"] is not None            # 流式必有 TTFT
        assert rows[0]["ttft_ms"] >= 0
        assert rows[0]["total_tokens"] == 15             # usage 事件回填
        # 采集器字段落库（usage 事件带 chars/source）
        assert rows[0]["usage_source"] == "provider"
        assert rows[0]["prompt_chars"] == 2
        assert rows[0]["completion_chars"] == 5

    def test_ttft_logged_for_thinking_only_turn(self, client, store, logdb, monkeypatch):
        """只吐思考、没有正文的 turn 也必须记到 TTFT。

        只认 text_delta 的旧口径下这类 turn 恒为 None，而 reasoning 模型的入站流量里
        绝大多数正是这种形态（思考 + 工具调用，正文为空）。
        """
        k = _make_key(store)
        events = [{"type": "thinking_delta", "delta": "让我想想"},
                  {"type": "done", "finish_reason": "stop"}]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"], stream=True)
        assert len(rows) == 1
        assert rows[0]["ttft_ms"] is not None
        assert rows[0]["ttft_ms"] >= 0

    def test_ttft_logged_for_tool_call_only_turn(self, client, store, logdb, monkeypatch):
        """纯工具调用 turn（无正文、无思考）同样记 TTFT：客户端确实收到了首个产出。"""
        k = _make_key(store)
        events = [{"type": "tool_call_delta",
                   "tool_call": {"id": "call_1", "type": "function",
                                 "function": {"name": "Read", "arguments": "{}"}}},
                  {"type": "done", "finish_reason": "tool_calls"}]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"], stream=True)
        assert rows[0]["ttft_ms"] is not None
        assert rows[0]["ttft_ms"] >= 0

    def test_stream_passthrough_client_params(self, client, store, monkeypatch):
        """客户端显式给的参数原样透传给池：不覆盖、不裁剪（中转只做转发）。"""
        k = _make_key(store)
        pool = FakePool(stream_events=STREAM_EVENTS)
        _mount_pool(monkeypatch, pool)
        self._post(client, k["secret"], temperature=0.9, max_tokens=65536)
        assert pool.calls[0]["temperature"] == 0.9
        assert pool.calls[0]["max_tokens"] == 65536      # 大过兜底值也不裁剪

    def test_stream_defaults_not_conservative(self, client, store, monkeypatch):
        """客户端未指定时的兜底：max_tokens 远离池默认 4096；温度不替客户端填。"""
        k = _make_key(store)
        pool = FakePool(stream_events=STREAM_EVENTS)
        _mount_pool(monkeypatch, pool)
        self._post(client, k["secret"])
        assert pool.calls[0]["max_tokens"] == gw_mod.DEFAULT_MAX_TOKENS
        assert pool.calls[0]["max_tokens"] > 4096        # 思考链吃光预算的元凶
        assert "temperature" not in pool.calls[0]        # 未指定则交给池/上游默认

    def test_missing_usage_recorded_as_zero(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        events = [{"type": "text_delta", "delta": "x"},
                  {"type": "done", "finish_reason": "stop"}]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"], stream=True)
        assert rows[0]["prompt_tokens"] == 0             # 决策#6: 缺失记 0，不估算
        assert rows[0]["completion_tokens"] == 0

    def test_upstream_error_logged(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        events = [{"type": "provider_error", "error": "timeout after 120s"},
                  {"type": "done", "finish_reason": "error"}]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        r = self._post(client, k["secret"])
        assert r.status_code == 200                      # SSE 已 200，错误在流内
        assert '"type": "server_error"' in r.text or '"type":"server_error"' in r.text
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"], stream=True)
        assert rows[0]["status"] == "error"
        assert "timeout" in rows[0]["error"]

    def test_sse_forwards_trailing_usage_chunk(self, client, store, monkeypatch):
        """真流式透传：池的权威 usage 以尾部 choices:[] + usage chunk 转发给客户端。"""
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(stream_events=STREAM_EVENTS))
        r = self._post(client, k["secret"])
        chunks = _sse_chunks(r)
        usage_chunks = [ch for ch in chunks if ch.get("usage") is not None]
        assert usage_chunks, "真流式应转发 usage chunk"
        assert chunks[-1] is usage_chunks[-1]              # usage chunk 收尾（[DONE] 前最后一条）
        last = usage_chunks[-1]
        assert last["object"] == "chat.completion.chunk"
        assert last["choices"] == []                       # OpenAI include_usage 约定
        assert last["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_sse_relays_real_finish_reason(self, client, store, monkeypatch):
        """不再硬写 stop：透传池真实收尾原因（此处 tool_calls），且 usage chunk 随之转发。"""
        k = _make_key(store)
        events = [
            {"type": "tool_call_delta", "tool_call": {
                "id": "c1", "type": "function",
                "function": {"name": "f", "arguments": "{}"}}},
            {"type": "usage", "prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            {"type": "done", "finish_reason": "tool_calls"},
        ]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        r = self._post(client, k["secret"])
        chunks = _sse_chunks(r)
        finish = [ch for ch in chunks if ch.get("choices")
                  and ch["choices"][0].get("finish_reason")]
        assert finish and finish[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert chunks[-1].get("usage") == {
            "prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    def test_sse_forwards_zero_usage_when_provider_missing(self, client, store, logdb, monkeypatch):
        """provider_missing（全 0）也如实透传：客户端收到 0-usage chunk，与落库一致。"""
        k = _make_key(store)
        events = [
            {"type": "text_delta", "delta": "x"},
            {"type": "usage", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "source": "provider_missing"},
            {"type": "done", "finish_reason": "stop"},
        ]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        r = self._post(client, k["secret"])
        chunks = _sse_chunks(r)
        usage_chunks = [ch for ch in chunks if ch.get("usage") is not None]
        assert usage_chunks and usage_chunks[-1]["usage"] == {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        logdb.flush()
        row = logdb.query(key_id=k["key_id"], stream=True)[0]
        assert row["usage_source"] == "provider_missing"
        assert row["total_tokens"] == 0

    def test_sse_no_usage_event_forwards_no_usage_chunk(self, client, store, monkeypatch):
        """池未发 usage 事件（如提前断连）→ 不伪造 usage chunk。"""
        k = _make_key(store)
        events = [{"type": "text_delta", "delta": "x"},
                  {"type": "done", "finish_reason": "stop"}]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        r = self._post(client, k["secret"])
        chunks = _sse_chunks(r)
        assert not [ch for ch in chunks if "usage" in ch]

    def test_unknown_model_stream_404(self, client, store, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(models=("deepseek-v4-chat",)))
        r = self._post(client, k["secret"], model="nope")
        assert r.status_code == 404


# ==================== 兜底 Tier 范围（按别名可选） ====================


class TestFallbackTier:
    def _make_tier_key(self, store, tier_map):
        return store.create(name="cline-work", project="cline",
                            aliases={"gpt-4o": "deepseek-v4-chat"},
                            alias_fallback_tiers=tier_map)

    def _post(self, client, secret, model="gpt-4o", stream=False):
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"},
                           json={"model": model, "stream": stream,
                                 "messages": [{"role": "user", "content": "hi"}]})

    def test_fallback_range_passed_to_pool(self, client, store, monkeypatch):
        k = self._make_tier_key(store, {"gpt-4o": [2, 4]})
        pool = FakePool()
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        assert pool.calls[0]["tier"] == (2, 4)     # 兜底范围作为硬过滤透传

    def test_single_int_normalized_to_range(self, client, store, monkeypatch):
        """旧格式/单档值归一为 [n, n]。"""
        k = self._make_tier_key(store, {"gpt-4o": 3})
        pool = FakePool()
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        assert pool.calls[0]["tier"] == (3, 3)

    def test_no_fallback_passes_none(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        pool = FakePool()
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        assert pool.calls[0]["tier"] is None       # 未配置 = 原行为

    def test_fallback_range_stream(self, client, store, monkeypatch):
        k = self._make_tier_key(store, {"gpt-4o": [1, 3]})
        pool = FakePool(stream_events=STREAM_EVENTS)
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"], stream=True)
        assert r.status_code == 200
        assert pool.calls[0]["tier"] == (1, 3)

    def test_log_records_actual_model(self, client, store, logdb, monkeypatch):
        """兜底生效时池实际用的模型可能与目标不同，日志记实际值。"""
        k = self._make_tier_key(store, {"gpt-4o": [3, 4]})
        result = {"ok": True, "content": "x",
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                  "model": "tier3-other-model", "key_name": "k1",
                  "tool_calls": [], "finish_reason": "stop"}
        _mount_pool(monkeypatch, FakePool(call_result=result))
        self._post(client, k["secret"])
        logdb.flush()
        rows = logdb.query(key_id=k["key_id"])
        assert rows[0]["model_used"] == "tier3-other-model"
        assert rows[0]["model_requested"] == "gpt-4o"

    def test_invalid_tier_values_dropped(self, store):
        k = store.create(name="x", project="p",
                         aliases={"a": "m1", "b": "m2", "c": "m3", "d": "m4", "e": "m5"},
                         alias_fallback_tiers={"a": 0, "b": "3", "c": [9],
                                               "d": [4, 5], "e": ["x", "y"]})  # type: ignore[dict-item]
        assert k["alias_fallback_tiers"] == {"d": [4, 5]}
        assert store.get_fallback_tier_range(k["key_id"], "a") is None
        assert store.get_fallback_tier_range(k["key_id"], "d") == (4, 5)
        assert store.get_fallback_tier_range(k["key_id"], "nope") is None

    def test_reversed_and_out_of_bounds_range_normalized(self, store):
        """顺序颠倒自动换序；越界钳制到 1-5。"""
        k = store.create(name="x", project="p",
                         aliases={"a": "m1", "b": "m2"},
                         alias_fallback_tiers={"a": [4, 2], "b": [2, 9]})
        assert k["alias_fallback_tiers"] == {"a": [2, 4], "b": [2, 5]}
        assert store.get_fallback_tier_range(k["key_id"], "a") == (2, 4)

    def test_create_endpoint_with_fallback_tiers(self, client, store):
        r = client.post("/inbound/keys", json={
            "name": "a", "project": "p",
            "aliases": {"m": "deepseek-v4-chat"},
            "alias_fallback_tiers": {"m": [1, 2]}})
        assert r.status_code == 200
        assert r.json()["key"]["alias_fallback_tiers"] == {"m": [1, 2]}

    def test_update_endpoint_roundtrip(self, client, store):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        r = client.patch(f"/inbound/keys/{k['key_id']}",
                         json={"alias_fallback_tiers": {"gpt-4o": [4, 5]}})
        assert r.status_code == 200
        assert r.json()["key"]["alias_fallback_tiers"] == {"gpt-4o": [4, 5]}


# ==================== 兜底路由可观测（substituted / resolved_model） ====================


class TestSubstitutionVisibility:
    """目标模型没被满足时，池换用别的模型 → 日志记真实模型/key + substituted 标记。"""

    def _post(self, client, secret, model="gpt-4o", stream=False):
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"},
                           json={"model": model, "stream": stream,
                                 "messages": [{"role": "user", "content": "hi"}]})

    def test_nonstream_substituted_flag(self, client, store, logdb, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "target-model"})
        result = {"ok": True, "content": "x",
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                  "model": "other-model", "key_name": "real_key",
                  "tool_calls": [], "finish_reason": "stop"}
        _mount_pool(monkeypatch, FakePool(models=("target-model",), call_result=result))
        self._post(client, k["secret"])
        logdb.flush()
        row = logdb.query(key_id=k["key_id"])[0]
        assert row["model_requested"] == "gpt-4o"       # harness 原始请求名（别名）
        assert row["resolved_model"] == "target-model"  # 别名解析后的目标
        assert row["model_used"] == "other-model"       # 池实际换用的模型
        assert row["upstream_key"] == "real_key"
        assert row["substituted"] == 1

    def test_nonstream_no_substitution(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(models=("deepseek-v4-chat",)))  # 默认结果模型同名
        self._post(client, k["secret"], model="deepseek-v4-chat")
        logdb.flush()
        row = logdb.query(key_id=k["key_id"])[0]
        assert row["substituted"] == 0
        assert row["resolved_model"] == "deepseek-v4-chat"
        assert row["model_used"] == "deepseek-v4-chat"

    def test_stream_records_actual_key_and_model(self, client, store, logdb, monkeypatch):
        """修复 known limitation：流式现在也能记录真实上游 key + 兜底后实际模型。"""
        k = _make_key(store, aliases={"gpt-4o": "target-model"})
        events = [
            {"type": "text_delta", "delta": "h"},
            {"type": "usage", "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7,
             "model": "other-model", "key_name": "stream_key"},
            {"type": "done", "finish_reason": "stop",
             "model": "other-model", "key_name": "stream_key"},
        ]
        _mount_pool(monkeypatch, FakePool(models=("target-model",), stream_events=events))
        r = self._post(client, k["secret"], stream=True)
        assert r.status_code == 200
        logdb.flush()
        row = logdb.query(key_id=k["key_id"])[0]
        assert row["resolved_model"] == "target-model"
        assert row["model_used"] == "other-model"
        assert row["upstream_key"] == "stream_key"      # 不再是空串
        assert row["substituted"] == 1
        assert row["total_tokens"] == 7

    def test_stream_same_model_not_substituted(self, client, store, logdb, monkeypatch):
        k = _make_key(store)
        events = [
            {"type": "text_delta", "delta": "x"},
            {"type": "usage", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
             "model": "deepseek-v4-chat", "key_name": "kk"},
            {"type": "done", "finish_reason": "stop"},
        ]
        _mount_pool(monkeypatch, FakePool(models=("deepseek-v4-chat",), stream_events=events))
        self._post(client, k["secret"], model="deepseek-v4-chat", stream=True)
        logdb.flush()
        row = logdb.query(key_id=k["key_id"])[0]
        assert row["substituted"] == 0
        assert row["model_used"] == "deepseek-v4-chat"
        assert row["upstream_key"] == "kk"

    def test_stats_counts_substitutions(self, client, store, logdb):
        import time
        k = _make_key(store)
        now = time.time()
        logdb.record({"ts": now, "key_id": k["key_id"], "key_name": k["name"], "stream": False,
                      "model_requested": "gpt-4o", "model_used": "other", "resolved_model": "target",
                      "upstream_key": "r1", "substituted": 1, "status": "ok", "http_status": 200,
                      "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        logdb.record({"ts": now, "key_id": k["key_id"], "key_name": k["name"], "stream": False,
                      "model_requested": "m", "model_used": "m", "resolved_model": "m",
                      "upstream_key": "r1", "substituted": 0, "status": "ok", "http_status": 200})
        logdb.flush()
        body = client.get("/inbound/stats", params={"days": 1}).json()
        assert body["overview"]["substituted"] == 1
        assert body["by_key"][0]["substituted"] == 1

    def test_legacy_db_migration_adds_columns(self, tmp_path):
        """老库缺列 → CallLog 初始化自动 ALTER 补齐，读写正常。"""
        import sqlite3

        from server.inbound_gateway.call_log import CallLog
        p = tmp_path / "legacy.db"
        c = sqlite3.connect(p)
        c.execute("""CREATE TABLE inbound_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
            key_id TEXT, key_name TEXT, stream INTEGER, model_requested TEXT, model_used TEXT,
            upstream_key TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
            ttft_ms INTEGER, duration_ms INTEGER, status TEXT, http_status INTEGER, retries INTEGER,
            client_ip TEXT, error TEXT)""")
        c.commit()
        c.close()
        cl = CallLog(p)
        cols = {row[1] for row in sqlite3.connect(p).execute("PRAGMA table_info(inbound_calls)")}
        assert {"resolved_model", "substituted",
                "prompt_chars", "completion_chars", "usage_source"} <= cols
        cl.record({"ts": 1, "key_id": "k", "key_name": "n", "stream": False,
                   "model_requested": "a", "model_used": "b", "resolved_model": "a",
                   "substituted": 1, "status": "ok", "http_status": 200,
                   "prompt_chars": 2, "completion_chars": 5, "usage_source": "provider"})
        cl.flush()
        rows = cl.query(key_id="k")
        assert rows[0]["substituted"] == 1
        assert rows[0]["resolved_model"] == "a"
        assert rows[0]["prompt_chars"] == 2
        assert rows[0]["completion_chars"] == 5
        assert rows[0]["usage_source"] == "provider"


# ==================== 入站专属 use_case ====================


class TestInboundUseCase:
    """网关转发必须显式声明 use_case，出站 key 的 allowed_uses 白名单才对入站流量生效。"""

    def _post(self, client, secret, stream=False):
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"},
                           json={"model": "deepseek-v4-chat", "stream": stream,
                                 "messages": [{"role": "user", "content": "hi"}]})

    def test_non_stream_passes_use_case(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        pool = FakePool()
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"])
        assert r.status_code == 200
        assert pool.calls[0]["use_case"] == "inbound_gateway"

    def test_stream_passes_use_case(self, client, store, monkeypatch):
        k = _make_key(store, aliases={"gpt-4o": "deepseek-v4-chat"})
        pool = FakePool(stream_events=STREAM_EVENTS)
        _mount_pool(monkeypatch, pool)
        r = self._post(client, k["secret"], stream=True)
        assert r.status_code == 200
        assert pool.calls[0]["use_case"] == "inbound_gateway"

    def test_use_case_registered(self):
        from server.llm_pool.types import USE_CASE_REGISTRY
        uc = USE_CASE_REGISTRY.get("inbound_gateway")
        assert uc is not None
        assert uc.scope == "llm"
        assert uc.default_tier == (0, 0)  # 不带默认 tier 约束（兜底与否由别名配置决定）

    def test_allowed_uses_whitelist_enforced(self):
        """出站 key 白名单不含 inbound_gateway → use_case_eligible 拒绝。"""
        from server.llm_pool.types import LLMKey
        restricted = LLMKey(key="k", base_url="http://x", allowed_uses=["agent_chat"])
        assert restricted.use_case_eligible("inbound_gateway") is False
        unrestricted = LLMKey(key="k", base_url="http://x", allowed_uses=[])
        assert unrestricted.use_case_eligible("inbound_gateway") is True
        explicit = LLMKey(key="k", base_url="http://x",
                          allowed_uses=["agent_chat", "inbound_gateway"])
        assert explicit.use_case_eligible("inbound_gateway") is True


# ==================== /inbound/keys 管理 ====================


class TestKeysAdmin:
    def test_create_returns_skla_secret(self, client, store):
        r = client.post("/inbound/keys", json={"name": "cherry", "project": "cherry"})
        assert r.status_code == 200
        key = r.json()["key"]
        assert key["secret"].startswith("sk-la-")
        assert key["enabled"] is True

    def test_create_requires_name(self, client, store):
        r = client.post("/inbound/keys", json={"name": "", "project": "p"})
        assert r.status_code == 422  # pydantic min_length 校验（生产 main.py 422 handler 同状态码）

    def test_disable_then_401(self, client, store):
        k = _make_key(store)
        r = client.patch(f"/inbound/keys/{k['key_id']}", json={"enabled": False})
        assert r.status_code == 200
        r2 = client.get("/v1/models", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r2.status_code == 401

    def test_delete_then_404_on_update(self, client, store):
        k = _make_key(store)
        assert client.delete(f"/inbound/keys/{k['key_id']}").status_code == 200
        assert client.patch(f"/inbound/keys/{k['key_id']}", json={"enabled": True}).status_code == 404

    def test_list_includes_today_calls(self, client, store, logdb):
        k = _make_key(store)
        logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                      "key_name": k["name"], "stream": False,
                      "model_requested": "m", "model_used": "m",
                      "status": "ok", "http_status": 200})
        logdb.flush()
        r = client.get("/inbound/keys")
        assert r.status_code == 200
        keys = r.json()["keys"]
        mine = [x for x in keys if x["key_id"] == k["key_id"]][0]
        assert "today_calls" in mine and "last_used_at" in mine


# ==================== /inbound/calls 与 /inbound/stats（P6） ====================


class TestLogQueryAndStats:
    def test_calls_filter_by_key(self, client, store, logdb):
        k1 = _make_key(store, name="a")
        k2 = _make_key(store, name="b")
        for k in (k1, k2):
            logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                          "key_name": k["name"], "stream": True,
                          "model_requested": "gpt-4o", "model_used": "deepseek-v4-chat",
                          "ttft_ms": 400, "status": "ok", "http_status": 200,
                          "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        logdb.flush()
        r = client.get("/inbound/calls", params={"key_id": k1["key_id"]})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["calls"][0]["ttft_ms"] == 400        # P6: 字段完整含 ttft_ms

    def test_stats_aggregation_shape(self, client, store, logdb):
        k = _make_key(store)
        logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                      "key_name": k["name"], "stream": True,
                      "model_requested": "gpt-4o", "model_used": "deepseek-v4-chat",
                      "upstream_key": "modelscope_01", "ttft_ms": 400,
                      "duration_ms": 6000, "status": "ok", "http_status": 200,
                      "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        logdb.flush()
        r = client.get("/inbound/stats", params={"days": 1})
        assert r.status_code == 200
        body = r.json()
        ov = body["overview"]
        assert ov["requests"] == 1 and ov["total_tokens"] == 150
        assert ov["success_rate"] == 100.0
        assert ov["ttft_p50_ms"] == 400
        assert body["by_key"][0]["key_name"] == k["name"]
        assert body["by_upstream"][0]["upstream_key"] == "modelscope_01"

    def test_stats_percentile(self, client, store, logdb):
        k = _make_key(store)
        for ttft in (100, 200, 300, 400, 500):
            logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                          "key_name": k["name"], "stream": True,
                          "model_requested": "m", "model_used": "m",
                          "ttft_ms": ttft, "status": "ok", "http_status": 200})
        logdb.flush()
        body = client.get("/inbound/stats", params={"days": 1}).json()
        assert body["overview"]["ttft_p50_ms"] == 300
        assert body["overview"]["ttft_p95_ms"] >= 460

    def test_stats_rejects_bad_days(self, client, store):
        assert client.get("/inbound/stats", params={"days": 5}).status_code == 400

    def test_stats_custom_range_includes_today(self, client, store, logdb):
        """自定义范围（反馈⑥后端）：起止=今天 能聚合到今天的记录"""
        from datetime import datetime
        k = _make_key(store)
        logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                      "key_name": k["name"], "stream": False,
                      "model_requested": "m", "model_used": "m",
                      "status": "ok", "http_status": 200,
                      "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        logdb.flush()
        today = datetime.now().strftime("%Y-%m-%d")
        r = client.get("/inbound/stats", params={"date_from": today, "date_to": today})
        assert r.status_code == 200
        assert r.json()["overview"]["requests"] == 1

    def test_stats_custom_range_excludes_out_of_window(self, client, store, logdb):
        """范围不含今天 → 聚合不到今天的记录"""
        from datetime import datetime, timedelta
        k = _make_key(store)
        logdb.record({"ts": __import__("time").time(), "key_id": k["key_id"],
                      "key_name": k["name"], "stream": False,
                      "model_requested": "m", "model_used": "m",
                      "status": "ok", "http_status": 200})
        logdb.flush()
        past = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        body = client.get("/inbound/stats", params={"date_from": past, "date_to": past}).json()
        assert body["overview"]["requests"] == 0

    def test_stats_custom_range_rejects_bad_format(self, client, store):
        r = client.get("/inbound/stats", params={"date_from": "2026/09/01"})
        assert r.status_code == 400

    def test_stats_custom_range_rejects_reversed(self, client, store):
        r = client.get("/inbound/stats",
                       params={"date_from": "2026-08-20", "date_to": "2026-08-10"})
        assert r.status_code == 400

    def test_stats_custom_range_rejects_over_30_days(self, client, store):
        """跨度超过 30 天保留期上限 → 400"""
        from datetime import datetime, timedelta
        today = datetime.now()
        r = client.get("/inbound/stats", params={
            "date_from": (today - timedelta(days=30)).strftime("%Y-%m-%d"),
            "date_to": today.strftime("%Y-%m-%d"),
        })
        assert r.status_code == 400


# ==================== 明细留存 detail logging ====================


def _detail_rows(logdb):
    """直连 logdb 的 db 文件读 inbound_call_details（明细不暴露端点，测试自读库表）。"""
    import sqlite3

    conn = sqlite3.connect(logdb._path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM inbound_call_details ORDER BY ts, id").fetchall()]
    finally:
        conn.close()


def _force_detail(call_log_mod, monkeypatch, enabled=True, **over):
    """覆盖 _detail_cfg，绕开真实 config.toml 对 detail logging 的开关控制。"""
    base = dict(call_log_mod._DETAIL_CFG_DEFAULTS)
    base["enabled"] = enabled
    base.update(over)
    monkeypatch.setattr(call_log_mod, "_detail_cfg", lambda: base)


class TestDetailLogging:
    """可开关的请求/响应正文明细留存（旁路表 inbound_call_details）。

    - 开关 off：不落明细（即使真实 config 开着，也要保证默认/显式关时不落）
    - 开关 on：请求正文 + 响应正文（非流式 content / 流式聚合含 thinking）都落
    - 无论成功/失败都落（排查问题诉求）
    - 明细不进 /inbound/calls 与 /inbound/stats（正文不暴露）
    """

    def _post_non_stream(self, client, secret, model="deepseek-v4-chat"):
        return client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {secret}"},
                           json={"model": model,
                                 "messages": [{"role": "user", "content": "hi"}]})

    def test_off_does_not_record(self, client, store, logdb, monkeypatch):
        _force_detail(call_log, monkeypatch, enabled=False)
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        r = self._post_non_stream(client, k["secret"])
        assert r.status_code == 200
        logdb.flush()
        assert _detail_rows(logdb) == []

    def test_on_records_request_and_response_non_stream(self, client, store, logdb, monkeypatch):
        _force_detail(call_log, monkeypatch, enabled=True)
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        r = self._post_non_stream(client, k["secret"])
        assert r.status_code == 200
        logdb.flush()
        rows = _detail_rows(logdb)
        assert len(rows) == 1
        d = rows[0]
        assert d["status"] == "ok"
        assert d["key_id"] == k["key_id"]
        assert '"hi"' in d["request_body"]          # 请求原文含 messages
        assert d["response_type"] == "non_stream"
        assert d["response_body"] == "hello"        # 非流式 content

    def test_on_records_even_on_failure(self, client, store, logdb, monkeypatch):
        """上游失败（502）也落明细：请求正文在，响应为空串，status=error（排查问题核心诉求）。"""
        _force_detail(call_log, monkeypatch, enabled=True)
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool(call_result={"ok": False, "error": "boom"}))
        r = self._post_non_stream(client, k["secret"])
        assert r.status_code == 502
        logdb.flush()
        rows = _detail_rows(logdb)
        assert len(rows) == 1
        d = rows[0]
        assert d["status"] == "error"
        assert "boom" in d["error"]
        assert '"hi"' in d["request_body"]          # 失败也要能看到发出的请求
        assert d["response_body"] == ""

    def test_stream_records_aggregated_text_and_thinking(self, client, store, logdb, monkeypatch):
        """流式 response_body = 聚合正文 + reasoning 段（含思考，用户拍板）。"""
        _force_detail(call_log, monkeypatch, enabled=True)
        k = _make_key(store)
        events = [
            {"type": "text_delta", "delta": "He"},
            {"type": "text_delta", "delta": "llo"},
            {"type": "thinking_delta", "delta": "让我想想"},
            {"type": "usage", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            {"type": "done", "finish_reason": "stop"},
        ]
        _mount_pool(monkeypatch, FakePool(stream_events=events))
        r = TestChatStream()._post(client, k["secret"])
        assert r.status_code == 200
        logdb.flush()
        rows = _detail_rows(logdb)
        assert len(rows) == 1
        d = rows[0]
        assert d["response_type"] == "stream"
        assert "Hello" in d["response_body"]
        assert "让我想想" in d["response_body"]     # 思考被聚合进正文
        assert d["status"] == "ok"

    def test_detail_not_exposed_in_calls_or_stats(self, client, store, logdb, monkeypatch):
        """/inbound/calls 与 /inbound/stats 不返回正文明细（无读取端点约束）。"""
        _force_detail(call_log, monkeypatch, enabled=True)
        k = _make_key(store)
        _mount_pool(monkeypatch, FakePool())
        self._post_non_stream(client, k["secret"])
        logdb.flush()
        assert _detail_rows(logdb), "前置：明细确实落了"
        calls = client.get("/inbound/calls").json()
        assert "request_body" not in calls["calls"][0]
        assert "response_body" not in calls["calls"][0]
        stats = client.get("/inbound/stats", params={"days": 1}).json()
        assert "request_body" not in stats and "response_body" not in stats


class TestDetailLogWindow:
    """call_log 层的窗口让位与单条截断（不依赖 HTTP）。"""

    def _make(self, tmp_path):
        from server.inbound_gateway.call_log import CallLog
        return CallLog(tmp_path / "detail.db")

    def _snap(self, key_id="k", i=0):
        import time
        return {"ts": time.time() + i, "key_id": key_id, "key_name": "n",
                "stream": False, "model_requested": "m", "model_used": "m",
                "status": "ok", "error": ""}

    def test_window_keeps_most_recent(self, tmp_path, monkeypatch):
        cl = self._make(tmp_path)
        from server.inbound_gateway import call_log as clmod
        _force_detail(clmod, monkeypatch, enabled=True, max_records=3,
                      max_total_bytes=10 ** 9, max_single_bytes=10 ** 6)
        for i in range(5):
            cl.record_detail(self._snap(i=i), request_body=f'{{"n":{i}}}',
                             response_body=f"resp{i}", response_type="non_stream")
        cl.flush()
        import sqlite3
        conn = sqlite3.connect(cl._path)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT request_body FROM inbound_call_details ORDER BY ts")]
        finally:
            conn.close()
        assert len(ids) == 3                      # 窗口只留最新 3
        assert ids == ['{"n":2}', '{"n":3}', '{"n":4}']  # 最老 2 条被让位

    def test_single_oversize_truncated(self, tmp_path, monkeypatch):
        cl = self._make(tmp_path)
        from server.inbound_gateway import call_log as clmod
        _force_detail(clmod, monkeypatch, enabled=True, max_records=50,
                      max_total_bytes=10 ** 9, max_single_bytes=50)
        big = "x" * 1000
        cl.record_detail(self._snap(), request_body=big, response_body="ok",
                         response_type="non_stream")
        cl.flush()
        import sqlite3
        conn = sqlite3.connect(cl._path)
        conn.row_factory = sqlite3.Row
        try:
            d = dict(conn.execute("SELECT * FROM inbound_call_details").fetchone())
        finally:
            conn.close()
        assert d["truncated"] == 1
        assert d["truncated_what"] == "request"
        assert d["request_bytes"] == 1000          # 截断前字节数
        assert len(d["request_body"].encode("utf-8")) <= 50  # 落库被截断
        assert "[truncated]" in d["request_body"]


# ==================== MCP 排除 ====================


class TestMcpExclude:
    def test_all_inbound_ops_excluded(self):
        expected = {
            "inbound_v1_models", "inbound_v1_chat_completions",
            "inbound_keys_list", "inbound_keys_create",
            "inbound_keys_update", "inbound_keys_delete",
            "inbound_calls_list", "inbound_stats",
        }
        assert expected <= mcp_whitelist.GATEWAY_EXCLUDE
