"""入站网关路由：/v1/*（OpenAI 兼容）+ /inbound/*（管理）

Bounds（spec，2026-09-03 更新）: pool 通过 get_pool() 延迟获取，测试可整体替换。
server/llm_pool/ 原为只读子树；2026-09-03 为实现「真透传」（参数 None=不下发、
stream() 补 response_format）经用户拍板修改了 pool.py——此后对池的改动需做
内部调用点兼容性审计（见 CHANGELOG 同期条目），不再是绝对禁区。
TTFT 在本层计时（首个产出事件到达时刻：正文 / 思考 / 工具调用任一，ttft_kind 落库标记类型）。
统计口径: 一切聚合走 call_log.py 的 SQLite（网关独立账本，ADR-0031：与池统计解耦，
且是"近 n 次请求备查 / 计费"的落库底座）。自 2026-09-02 起池的 stream() 也补记了
project/key 级 token，但网关仍以自身 SQLite 为准，二者互不依赖。

known limitation: 无（原 pool.stream() 事件不含上游 key/模型，流式日志 upstream_key 记空串）。
现已由 pool.stream() 在 usage/done 事件附带 key_name + 实际 model，本层回填日志：
流式调用同样记录真实上游 key、兜底后实际模型，并置 substituted 标记，使兜底路由可观测。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lib.llm_usage import collect_usage, text_from_messages
from server.inbound_gateway import call_log
from server.inbound_gateway.key_store import KeyNotFoundError, get_key_store
from server.llm_pool.opencode import derive_session_id

logger = logging.getLogger(__name__)

router = APIRouter()

# 入站转发的专属 use_case（已注册进 server/llm_pool/types.py USE_CASE_REGISTRY）。
# 显式透传让出站 key 的 allowed_uses 白名单对入站流量生效（不传则绕过白名单）。
INBOUND_USE_CASE = "inbound_gateway"

# ---------- OpenAI 风格错误体 ----------


def _err(status: int, message: str, err_type: str = "invalid_request_error",
         code: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"message": message, "type": err_type}}
    if code:
        body["error"]["code"] = code
    return JSONResponse(status_code=status, content=body)


def _auth_key(request: Request) -> dict[str, Any] | JSONResponse:
    """Bearer 鉴权。失败返回 JSONResponse（调用方直接 return）。"""
    auth = request.headers.get("authorization", "")
    secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    key = get_key_store().verify(secret)
    if key is None:
        return _err(401, "Incorrect API key provided or the key has been disabled.",
                    code="invalid_api_key")
    return key


# ---------- pool 访问（延迟获取，测试可替换） ----------


def _get_pool():
    from server.llm_pool import get_pool, is_initialized
    if not is_initialized():
        return None
    return get_pool()


def _pool_model_names(pool) -> set[str]:
    """池内全部模型名（list_models 内部持锁，安全）。"""
    summary = pool.list_models()
    names: set[str] = set()
    for group in summary.get("grouped_by_tier", {}).values():
        for entry in group:
            names.add(entry["model"])
    return names


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return ""


# ---------- /v1/models ----------


@router.get("/v1/models", operation_id="inbound_v1_models")
async def v1_models(request: Request):
    """池内全量模型 + 全部 key 的别名聚合（不做 per-key 过滤，design-decisions #7）。

    与 /v1/chat/completions 一样要求有效入站 key（P4: 无效/停用 key 一律 401）。
    """
    auth = _auth_key(request)
    if isinstance(auth, JSONResponse):
        return auth
    pool = _get_pool()
    ids: set[str] = set()
    if pool is not None:
        ids |= _pool_model_names(pool)
    ids |= set(get_key_store().all_alias_names())
    data = [{"id": m, "object": "model", "owned_by": "localagent"} for m in sorted(ids)]
    return {"object": "list", "data": data}


# ---------- /v1/chat/completions ----------


@router.post("/v1/chat/completions", operation_id="inbound_v1_chat_completions")
async def chat_completions(request: Request):
    """OpenAI Chat Completions 兼容端点（stream / 非 stream）。"""
    t0_mono = time.monotonic()
    t0_wall = time.time()

    auth = _auth_key(request)
    if isinstance(auth, JSONResponse):
        return auth
    key: dict[str, Any] = auth

    try:
        body = await request.json()
    except Exception:
        return _err(400, "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _err(400, "Request body must be a JSON object.")
    requested_model = body.get("model")
    messages = body.get("messages")
    if not isinstance(requested_model, str) or not requested_model:
        return _err(400, "'model' is a required string field.")
    if not isinstance(messages, list) or not messages:
        return _err(400, "'messages' must be a non-empty list.")
    is_stream = bool(body.get("stream", False))

    pool = _get_pool()
    if pool is None:
        return _err(503, "LLM pool is not initialized.", err_type="server_error")

    # 模型解析：key 别名 → 池内精确匹配 → 404（决策#2；兜底 Tier 为按别名显式可选）
    pool_models = _pool_model_names(pool)
    resolved = get_key_store().resolve_model(key["key_id"], requested_model, pool_models)
    if resolved is None:
        available = ", ".join(sorted(pool_models)[:30]) or "(empty)"
        return _err(404,
                    f"The model `{requested_model}` does not exist. "
                    f"Available models: {available}",
                    code="model_not_found")
    # 兜底 Tier 范围（映射弹窗可按别名配置）：作为 pool tier (lo, hi) 硬过滤透传，
    # 目标模型不可用时停留在该范围内兜底；None = 不兜底（原行为）
    fallback_tier = get_key_store().get_fallback_tier_range(key["key_id"], requested_model)

    # opencode 会话亲和（2026-09-06 上游强制 x-opencode-session，缺失即 400 MissingSessionID）：
    # 客户端显式发了就原样透传（尊重客户端自己的会话身份）；没发则按入站 key + 首条
    # user 消息内容派生稳定 ID（同一对话跨轮稳定，见 llm_pool/opencode.py）。
    # 该 session_id 同时激活池的 LKGP 会话粘性（同会话优先复用上次成功的 (key, model)）。
    client_session = request.headers.get("x-opencode-session") or None

    if is_stream:
        # 真流式透传：网关只"转发 pool.stream() 事件 + 中间记一层统计"，不改数据语义（ADR-0033 采集器）
        stream_gen = _stream_sse(
            pool, key, requested_model, resolved, body, t0_mono, t0_wall,
            _client_ip(request), fallback_tier, client_session)
        return StreamingResponse(
            stream_gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _call_non_stream(pool, key, requested_model, resolved, body,
                                  t0_mono, t0_wall, _client_ip(request), fallback_tier,
                                  client_session)


def _base_entry(key: dict, requested: str, resolved: str, is_stream: bool,
                client_ip: str, t0_wall: float) -> dict[str, Any]:
    return {
        "ts": t0_wall,
        "key_id": key["key_id"],
        "key_name": key["name"],
        "stream": is_stream,
        "model_requested": requested,
        "model_used": resolved,
        "upstream_key": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "ttft_ms": None,
        "ttft_kind": "",
        "duration_ms": 0,
        "status": "error",
        "http_status": 200,
        "retries": 0,
        "client_ip": client_ip,
        "error": "",
        "resolved_model": resolved,
        "substituted": 0,
        "prompt_chars": 0,
        "completion_chars": 0,
        "usage_source": "",
        # opencode 会话亲和 ID（透传或派生，_build_pool_kwargs 的实发值；排查会话归类用）
        "session_id": "",
        # 上游 prompt 缓存命中 token；None = provider 未上报（与 0 区分）
        "cached_tokens": None,
    }


def _finish_entry(entry: dict[str, Any], t0_mono: float) -> None:
    entry["duration_ms"] = int((time.monotonic() - t0_mono) * 1000)
    call_log.record(entry)


def _finish_detail(entry: dict[str, Any], request_text: str | None,
                   response_text: str, response_type: str,
                   usage_json: str | None = None) -> None:
    """detail logging 收口：request_text 为 None 表示开关关（由调用方预判），零开销跳过。

    必须在主表 _finish_entry 之后调用，此时 entry 已带最终 status/error/model 等快照。
    无论该次调用成功/失败都落（用户要求"全含，能读的都要，无论成功失败，排查问题"）。
    usage_json：provider 原始 usage 原样 JSON（兜底落盘——归一白名单漏掉的字段可离线补统计）。
    """
    if request_text is None:
        return
    call_log.record_detail(entry, request_body=request_text,
                           response_body=response_text, response_type=response_type,
                           usage_json=usage_json)


def _norm_usage(usage: dict[str, Any] | None) -> tuple[
        int, int, int, int | None, int | None, int | None]:
    """(prompt, completion, total, cached, reasoning, cache_creation)；None = provider 未上报。"""
    if not usage:
        return 0, 0, 0, None, None, None
    p = int(usage.get("prompt_tokens") or 0)
    c = int(usage.get("completion_tokens") or 0)
    cached = usage.get("cached_tokens")
    reasoning = usage.get("reasoning_tokens")
    ccreation = usage.get("cache_creation_tokens")
    return (p, c, int(usage.get("total_tokens") or (p + c)),
            int(cached) if cached is not None else None,
            int(reasoning) if reasoning is not None else None,
            int(ccreation) if ccreation is not None else None)


# 客户端未指定 max_tokens 时透传给池的兜底值。中转只做转发，不该替客户端拍输出预算：
# 池默认 4096 对 reasoning 模型过窄——思考链会吃光预算导致正文为空（finish_reason=length）。
# 取 16384 对齐项目内 reasoning 场景的既有共识（见 ADR-0004）。客户端显式给值时一律
# 原样透传，不覆盖、不裁剪。
DEFAULT_MAX_TOKENS = 16384

# TTFT 口径：首个「产出事件」到达时刻 − 请求开始，以下三类任一先到即计时。
# 只认 text_delta 会让 reasoning 模型的思考段、以及纯工具调用 turn（入站流量的绝大多数）
# 恒为 None，账本拿不到数。
_TTFT_EVENTS = ("text_delta", "thinking_delta", "tool_call_delta")


def _build_pool_kwargs(key: dict, resolved: str, body: dict,
                       fallback_tier: tuple[int, int] | None,
                       session_id: str | None = None) -> dict[str, Any]:
    """构造 pool.call() / pool.stream() 的公共 kwargs（透传优先，网关不塞魔数）。

    原则：客户端显式给了的参数原样转发；没给的不替它填（交给池/上游各自的默认）。
    max_tokens 是唯一例外——必须给兜底，否则落回池默认 4096 会卡住 reasoning 长思考链。
    显式传 null 等同于未传（OpenAI 语义）。其余未知参数（n/logprobs/logit_bias/
    service_tier/user/...）按 spec Trade 忽略：本地场景无感 / 泄露顾虑 / 响应侧不透传
    则请求侧透传无意义（详见 CHANGELOG 2026-09-08 白名单扩展条目）。
    session_id：opencode 会话亲和（透传或内容派生，见 chat_completions 注释），
    同时供池做 LKGP 会话粘性；仅 opencode 端点会实际下发对应 header。
    """
    kwargs: dict[str, Any] = {
        "messages": body["messages"],
        "model": resolved,
        "project": key["project"],
        "use_case": INBOUND_USE_CASE,  # 显式用途：出站 key 的 allowed_uses 白名单据此生效
        "tier": fallback_tier,  # None = 不加 tier 约束（池默认行为）；否则 (lo, hi) 范围硬过滤
        "session_id": session_id or derive_session_id(body["messages"], namespace=key["key_id"]),
    }
    if body.get("temperature") is not None:
        kwargs["temperature"] = body["temperature"]
    if body.get("tools") is not None:
        kwargs["tools"] = body["tools"]
    if body.get("tool_choice") is not None:
        kwargs["tool_choice"] = body["tool_choice"]
    if body.get("response_format") is not None:
        kwargs["response_format"] = body["response_format"]
    mt = body.get("max_tokens")
    if mt is None:
        # 新版 OpenAI SDK / o 系模型用 max_completion_tokens（语义等价：输出预算上限）。
        # 客户端发这个字段此前被静默丢弃、落回兜底值——输出预算被改写且客户端无感。
        mt = body.get("max_completion_tokens")
    kwargs["max_tokens"] = mt if mt is not None else DEFAULT_MAX_TOKENS
    # A 档采样参数透传（2026-09-08 白名单扩展，用户拍板）：None = 不下发，交 provider 默认。
    # B 档（n/logprobs/logit_bias/service_tier/user 等）仍按 spec Trade 忽略——
    # 本地场景无感/泄露顾虑/响应侧不支持的见 CHANGELOG 同期条目。
    kwargs["top_p"] = body.get("top_p")
    kwargs["stop"] = body.get("stop")
    kwargs["presence_penalty"] = body.get("presence_penalty")
    kwargs["frequency_penalty"] = body.get("frequency_penalty")
    kwargs["seed"] = body.get("seed")
    kwargs["parallel_tool_calls"] = body.get("parallel_tool_calls")
    return kwargs


# ---------- 非流式（T2） ----------


async def _call_non_stream(pool, key: dict, requested: str, resolved: str,
                           body: dict, t0_mono: float, t0_wall: float,
                           client_ip: str,
                           fallback_tier: tuple[int, int] | None = None,
                           session_id: str | None = None) -> JSONResponse | dict:
    entry = _base_entry(key, requested, resolved, False, client_ip, t0_wall)
    kwargs = _build_pool_kwargs(key, resolved, body, fallback_tier, session_id)
    entry["session_id"] = kwargs["session_id"] or ""  # 实发会话 ID（透传或派生）
    # detail logging：仅在开关开时序列化请求体（避免每次全量 json.dumps 大 body）
    detail_on = call_log.detail_logging_enabled()
    request_text = json.dumps(body, ensure_ascii=False) if detail_on else None
    resp_text = ""

    def _invoke():
        return pool.call(**kwargs)

    try:
        import asyncio
        result = await asyncio.to_thread(_invoke)
    except Exception as e:  # 线程池异常兜底
        entry.update(error=f"{type(e).__name__}: {str(e)[:500]}", http_status=502)
        _finish_entry(entry, t0_mono)
        _finish_detail(entry, request_text, resp_text, "non_stream")
        return _err(502, f"Upstream call failed: {str(e)[:200]}", err_type="server_error")

    entry["upstream_key"] = result.get("key_name", "") if isinstance(result, dict) else ""
    # 兜底生效时池实际用的模型可能与目标不同，日志记实际值
    if isinstance(result, dict) and result.get("model"):
        entry["model_used"] = result["model"]
        if result["model"] != resolved:
            entry["substituted"] = 1  # 请求的目标模型没被满足，池换了别的

    if not (isinstance(result, dict) and result.get("ok")):
        err = str(result.get("error", "unknown"))[:500] if isinstance(result, dict) else str(result)[:500]
        entry.update(error=err, http_status=502)
        _finish_entry(entry, t0_mono)
        _finish_detail(entry, request_text, resp_text, "non_stream")
        return _err(502, err, err_type="server_error")

    usage = result.get("usage") or {}
    completion_text = result.get("content", "") or ""
    thinking = result.get("thinking") or ""
    # chars 口径与流式对齐：thinking 计入 completion（池流式 completion_parts 含思考段）
    u = collect_usage(usage, text_from_messages(body["messages"]),
                      completion_text + thinking)
    p, c, t = u["prompt_tokens"], u["completion_tokens"], u["total_tokens"]
    entry.update(status="ok", http_status=200,
                 prompt_tokens=p, completion_tokens=c, total_tokens=t,
                 prompt_chars=u["prompt_chars"], completion_chars=u["completion_chars"],
                 usage_source=u["source"], cached_tokens=u.get("cached_tokens"),
                 reasoning_tokens=u.get("reasoning_tokens"),
                 cache_creation_tokens=u.get("cache_creation_tokens"),
                 finish_reason=str(result.get("finish_reason") or ""))
    resp_text = completion_text
    if thinking:
        resp_text += "\n\n--- reasoning ---\n" + thinking
    # usage_json：provider 原始 usage 原样 JSON（兜底落盘）
    usage_json = None
    if result.get("usage_raw"):
        try:
            usage_json = json.dumps(result["usage_raw"], ensure_ascii=False)
        except Exception:
            usage_json = None
    _finish_entry(entry, t0_mono)
    _finish_detail(entry, request_text, resp_text, "non_stream", usage_json)

    message: dict[str, Any] = {"role": "assistant", "content": completion_text}
    if thinking:
        # OpenRouter 风格 reasoning_content，与流式 delta 字段名一致
        message["reasoning_content"] = thinking
    tool_calls = result.get("tool_calls")
    if tool_calls:
        message["tool_calls"] = tool_calls
    client_usage: dict[str, Any] = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}
    if u.get("cached_tokens") is not None:
        # OpenAI 风格缓存命中明细，客户端可观测（对齐 prompt_tokens_details 约定）
        client_usage["prompt_tokens_details"] = {"cached_tokens": u["cached_tokens"]}
    if u.get("reasoning_tokens") is not None:
        client_usage["completion_tokens_details"] = {"reasoning_tokens": u["reasoning_tokens"]}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(t0_wall),
        "model": requested,  # 回显请求名（OpenAI 语义），实际用模型在日志可查
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": result.get("finish_reason") or "stop",
        }],
        "usage": client_usage,
    }


# ---------- 流式（T3） ----------


def _sse_chunk(chat_id: str, created: int, model: str, delta: dict | None,
               finish: str | None, usage: dict | None = None) -> str:
    chunk: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _sse_error(message: str, err_type: str = "server_error") -> str:
    """OpenRouter 风格 SSE 错误 chunk：让 harness 明确知道流中断了，而非静默截断。"""
    return (f"data: {json.dumps({'error': {'message': message, 'type': err_type}}, ensure_ascii=False)}\n\n")


def _sse_usage_chunk(chat_id: str, created: int, model: str,
                     prompt_tokens: int, completion_tokens: int,
                     total_tokens: int, cached_tokens: int | None = None,
                     reasoning_tokens: int | None = None) -> str:
    """OpenAI include_usage 约定的收尾 usage chunk：choices 为空数组，仅带 usage。

    真流式把它透传给调用方，让外部客户端能就地显示 token（网关只做统计，不改数字来源）。
    provider 上报了缓存命中/思考 token 时按 OpenAI 约定字段位附明细。
    """
    usage: dict[str, Any] = {"prompt_tokens": prompt_tokens,
                             "completion_tokens": completion_tokens,
                             "total_tokens": total_tokens}
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    chunk = {
        "id": chat_id, "object": "chat.completion.chunk", "created": created,
        "model": model, "choices": [], "usage": usage,
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _stream_sse(pool, key: dict, requested: str, resolved: str, body: dict,
                      t0_mono: float, t0_wall: float, client_ip: str,
                      fallback_tier: tuple[int, int] | None = None,
                      session_id: str | None = None) -> AsyncIterator[str]:
    """转接 pool.stream() 的真 SSE，D03 事件 → OpenAI chunk 序列。

    TTFT = 首个产出事件到达时刻 − 请求开始（正文 / 思考 / 工具调用任一先到即计时）。
    只认正文会让 reasoning 模型的思考段与纯工具调用 turn 恒为 None（见 _TTFT_EVENTS）。
    断连: finally 里 record 日志；generator 被 aclose 时 try/finally 仍执行，
    pool.stream() 自身 try/finally 归还 key/semaphore（D14），本层不重复管理。
    """
    entry = _base_entry(key, requested, resolved, True, client_ip, t0_wall)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(t0_wall)
    ttft_ms: int | None = None
    ttft_kind: str = ""
    usage_raw: dict[str, Any] | None = None
    upstream_err = ""
    # 透传池的真实收尾原因（stop/length/tool_calls/…），不再硬写 "stop"
    finish_reason_out: str = "stop"
    # pool.stream() 在 usage/done 事件上附带实际选中的 key_name/model（兜底后可能与
    # resolved 不同）；这里捕获，finally 回填日志，让流式调用的兜底路由也可观测。
    actual_model = ""
    actual_key = ""
    # detail logging：聚合正文（text）+ 思考（reasoning）供回看；聚合期即限量防内存爆炸，
    # 具体落库再在 record_detail 内按 max_single_bytes 二次截断兜底。
    detail_on = call_log.detail_logging_enabled()
    request_text = json.dumps(body, ensure_ascii=False) if detail_on else None
    text_parts: list[str] = []
    think_parts: list[str] = []
    text_bytes = 0
    think_bytes = 0
    agg_cap = call_log._DETAIL_CFG_DEFAULTS["max_single_bytes"]
    _agg_cut = False

    def _append_part(delta: str, to_think: bool) -> None:
        """聚合期限量追加；超 agg_cap 即停止（_agg_cut 置位，落库仍会二次截断）。"""
        nonlocal text_bytes, think_bytes, _agg_cut
        if not delta:
            return
        bucket = think_parts if to_think else text_parts
        base = think_bytes if to_think else text_bytes
        delta_b = len(delta.encode("utf-8"))
        if base + delta_b > agg_cap:
            _agg_cut = True
            return
        bucket.append(delta)
        if to_think:
            think_bytes = base + delta_b
        else:
            text_bytes = base + delta_b

    try:
        yield _sse_chunk(chat_id, created, requested, {"role": "assistant", "content": ""}, None)
        # 与流式共用同一套参数构造：网关不写死温度/输出预算，客户端给了就原样转发
        stream_kwargs = _build_pool_kwargs(key, resolved, body, fallback_tier, session_id)
        entry["session_id"] = stream_kwargs["session_id"] or ""  # 实发会话 ID（透传或派生）
        async for ev in pool.stream(**stream_kwargs):
            et = ev.get("type")
            if et in _TTFT_EVENTS and ttft_ms is None:
                # 首个产出事件即计时：正文 / 思考 / 工具调用，谁先到算谁
                ttft_ms = int((time.monotonic() - t0_mono) * 1000)
                ttft_kind = et.removesuffix("_delta")
            if et == "text_delta":
                if detail_on:
                    _append_part(str(ev.get("delta", "")), to_think=False)
                yield _sse_chunk(chat_id, created, requested, {"content": ev.get("delta", "")}, None)
            elif et == "thinking_delta":
                if detail_on:
                    _append_part(str(ev.get("delta", "")), to_think=True)
                # OpenRouter 风格 reasoning_content；不认的 harness 会忽略该字段，不崩
                yield _sse_chunk(chat_id, created, requested,
                                 {"reasoning_content": ev.get("delta", "")}, None)
            elif et == "tool_call_delta":
                yield _sse_chunk(chat_id, created, requested,
                                 {"tool_calls": [ev.get("tool_call")]}, None)
            elif et == "usage":
                usage_raw = ev
                actual_model = ev.get("model") or actual_model
                actual_key = ev.get("key_name") or actual_key
            elif et == "provider_error":
                upstream_err = str(ev.get("error", ""))[:500]
            elif et == "done":
                actual_model = ev.get("model") or actual_model
                actual_key = ev.get("key_name") or actual_key
                finish_reason_out = ev.get("finish_reason") or finish_reason_out
                if ev.get("finish_reason") == "error":
                    upstream_err = upstream_err or "upstream error before completion"
                yield _sse_chunk(chat_id, created, requested, {}, finish_reason_out)
        # 尾部 usage chunk：把池已归一的权威 usage 透传给调用方（对齐 OpenAI
        # stream_options.include_usage 约定：choices 空数组，带 usage）。客户端拿到
        # usage 事件才转发；被提前 aclose（未收到 usage）则不发，避免伪 0。
        if usage_raw is not None:
            p, c, t, cached, reasoning, _cc = _norm_usage(usage_raw)
            yield _sse_usage_chunk(chat_id, created, requested, p, c, t, cached, reasoning)
        if upstream_err:
            # OpenRouter 风格错误 chunk：让 harness 明确知道流中断了，而非静默截断
            yield _sse_error(upstream_err)
        yield "data: [DONE]\n\n"
    finally:
        p, c, t, cached, reasoning, ccreation = _norm_usage(usage_raw)
        if actual_model:
            entry["model_used"] = actual_model
            if actual_model != resolved:
                entry["substituted"] = 1
        if actual_key:
            entry["upstream_key"] = actual_key
        # usage_json：provider 原始 usage 原样 JSON（池 usage 事件附带 raw 键）
        usage_json = None
        raw = (usage_raw or {}).get("raw")
        if raw:
            try:
                usage_json = json.dumps(raw, ensure_ascii=False)
            except Exception:
                usage_json = None
        entry.update(
            prompt_tokens=p, completion_tokens=c, total_tokens=t,
            prompt_chars=int((usage_raw or {}).get("prompt_chars") or 0),
            completion_chars=int((usage_raw or {}).get("completion_chars") or 0),
            usage_source=str((usage_raw or {}).get("source") or ""),
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            cache_creation_tokens=ccreation,
            # 错误流没有真实终止原因，不把默认值 "stop" 误记成观测数据
            finish_reason="" if upstream_err else finish_reason_out,
            ttft_ms=ttft_ms,
            ttft_kind=ttft_kind,
            status="ok" if not upstream_err else "error",
            http_status=200,
            error=upstream_err,
        )
        _finish_entry(entry, t0_mono)
        # detail logging：流式无论成功/失败/断连都在 finally 收口落明细。
        # response_body = 聚合正文 +（若含思考）reasoning 段；聚合已被 _append_part 限量，
        # 落库再经 record_detail 按 max_single_bytes 二次截断兜底。
        resp_text = "".join(text_parts)
        if think_parts:
            resp_text += "\n\n--- reasoning ---\n" + "".join(think_parts)
        _finish_detail(entry, request_text, resp_text, "stream", usage_json)


class KeyCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    project: str = Field(min_length=1)
    aliases: dict[str, str] = Field(default_factory=dict)
    alias_fallback_tiers: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class KeyUpdateRequest(BaseModel):
    name: str | None = None
    project: str | None = None
    aliases: dict[str, str] | None = None
    alias_fallback_tiers: dict[str, Any] | None = None
    enabled: bool | None = None


@router.get("/inbound/keys", operation_id="inbound_keys_list")
async def inbound_keys_list():
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    today_counts = call_log.today_count_by_key()
    keys = []
    for k in get_key_store().list_keys():
        k = dict(k)
        k["today_calls"] = today_counts.get(k["key_id"], 0)
        k["today"] = today
        keys.append(k)
    return {"keys": keys, "today": today}


@router.post("/inbound/keys", operation_id="inbound_keys_create")
async def inbound_keys_create(req: KeyCreateRequest):
    try:
        key = get_key_store().create(req.name, req.project, req.aliases, req.enabled,
                                     req.alias_fallback_tiers)
    except ValueError as e:
        return _err(400, str(e))
    return {"key": key}


@router.patch("/inbound/keys/{key_id}", operation_id="inbound_keys_update")
async def inbound_keys_update(key_id: str, req: KeyUpdateRequest):
    try:
        key = get_key_store().update(key_id, name=req.name, project=req.project,
                                     aliases=req.aliases, enabled=req.enabled,
                                     alias_fallback_tiers=req.alias_fallback_tiers)
    except KeyNotFoundError:
        return _err(404, f"key `{key_id}` not found")
    except ValueError as e:
        return _err(400, str(e))
    return {"key": key}


@router.delete("/inbound/keys/{key_id}", operation_id="inbound_keys_delete")
async def inbound_keys_delete(key_id: str):
    try:
        get_key_store().delete(key_id)
    except KeyNotFoundError:
        return _err(404, f"key `{key_id}` not found")
    return {"ok": True}


@router.get("/inbound/calls", operation_id="inbound_calls_list")
async def inbound_calls_list(key_id: str | None = None, model: str | None = None,
                             status: str | None = None, stream: bool | None = None,
                             date_from: str | None = None, date_to: str | None = None,
                             limit: int = 200):
    rows = call_log.query(key_id=key_id, model=model, status=status, stream=stream,
                          date_from=date_from, date_to=date_to, limit=min(limit, 1000))
    return {"calls": rows, "count": len(rows)}


@router.get("/inbound/stats", operation_id="inbound_stats")
async def inbound_stats(days: int = 1, date_from: str | None = None,
                        date_to: str | None = None):
    if date_from or date_to:
        df = date_from or date_to or ""
        dt = date_to or date_from or ""
        try:
            d0 = datetime.strptime(df, "%Y-%m-%d")
            d1 = datetime.strptime(dt, "%Y-%m-%d")
        except ValueError:
            return _err(400, "date_from/date_to must be YYYY-MM-DD")
        if d1 < d0:
            return _err(400, "date_to must be >= date_from")
        if (d1 - d0).days >= 30:
            return _err(400, "自定义范围不能超过 30 天（保留期上限）")
        return call_log.aggregate(date_from=df, date_to=dt)
    if days not in (1, 7, 30):
        return _err(400, "days must be one of 1/7/30")
    return call_log.aggregate(days=days)
