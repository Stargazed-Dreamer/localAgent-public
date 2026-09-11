"""LLM 并发池端点：/llm/pool/*"""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from lib.schema import BaseSchema
from server.core.lifecycle import _ensure_pool_initialized

logger = logging.getLogger("localagent.llm_endpoints")


# ============================================================================
# v6-lite T08: SSE 伪流式 helpers
# ============================================================================


def _sse_event(event_type: str, data: dict) -> str:
    """格式化 SSE 事件：data: {"type": "...", ...}\\n\\n"""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """把文本切分成 chunks 用于伪流式发送。

    策略：按 chunk_size 个字符切分（CJK 友好），保留原始空白。
    最后一 chunk 可能不足 chunk_size。
    """
    if not text:
        return []
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


class LLMPoolCallRequest(BaseSchema):
    """LLM 池完整调用请求

    temperature / max_tokens 默认 None = 不下发给 provider，用 provider 自己的默认值
    （池是通道，不替调用方决定采样参数与输出预算）。要约束就显式传值。
    """
    messages: list[dict]
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int = 120
    retries: int = 4
    project: str = "default"
    model_tier: int | str | list | None = None  # v8: 1-5 数字 / [min,max] 范围 / 旧 "default/cheap/powerful" 字符串。
                            # 传给 pool._acquire 做 tier 范围匹配；None 时用 use_case.default_tier。
    response_format: dict | None = None  # OpenAI 结构化输出 json_schema；None 时不传
    use_case: str | None = None  # 指定后按 use_case 过滤 key（敏感用途排除 privacy_warning key）
    session_id: str | None = None  # v12: LKGP 会话粘性——同 session_id 优先复用上次成功的 (key, model)


class LLMPoolCallSimpleRequest(BaseSchema):
    """LLM 池简化调用请求（temperature / max_tokens 语义同 LLMPoolCallRequest）"""
    prompt: str
    system_prompt: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int = 120
    retries: int = 4
    project: str = "default"
    model_tier: int | str | list | None = None  # v8: 1-5 数字或旧 str / [min,max] 范围，传给 pool 做 tier 匹配
    use_case: str | None = None  # 指定后按 use_case 过滤 key
    session_id: str | None = None  # v12: LKGP 会话粘性


class LLMPoolChatToolsRequest(BaseSchema):
    """v6-lite T05: 带工具的 LLM 调用请求（引擎专用，非 agent 工具）

    与 LLMPoolCallRequest 的区别：增加 tools / tool_choice 字段，
    供 v6-lite 引擎的 LLMGateway 调用——模型可在响应中返回 tool_calls，
    由引擎的 SessionRunner 解析并执行。

    此端点 NOT 在 agent tool catalog 中（已加入 mcp_whitelist.GATEWAY_EXCLUDE），
    避免引擎把它当作工具调用造成递归。
    """
    messages: list[dict]
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int = 120
    retries: int = 4
    project: str = "default"
    model_tier: int | str | list | None = None
    response_format: dict | None = None
    use_case: str | None = None
    session_id: str | None = None
    tools: list[dict] | None = None  # T05: OpenAI 格式工具定义
    tool_choice: str | dict | None = None  # T05: "auto"/"none"/"required"/{"type":"function","function":{"name":"..."}}


def register_llm_routes(app: FastAPI):
    """注册 /llm/pool/* 端点"""

    @app.get("/llm/pool/status", operation_id="llm_pool_status")
    async def llm_pool_status(
        summary: bool = Query(False, description="true=只返回聚合统计，省略 keys 详情数组（推荐 agent 默认用 summary=true 精简输出）"),
        include_recent_calls: bool = Query(False, description="true=附带最近 N 次调用历史（默认 50，最多 200）"),
        recent_calls_limit: int = Query(50, description="附带调用历史的条数上限（1-200）"),
        only_failed_calls: bool = Query(False, description="true=只返回失败的调用历史"),
    ):
        """查询 LLM 并发池状态

        返回池中所有 key 的状态、并发数、token 消耗等。
        summary=true 时省略 keys 数组，适合 agent 快速查看池健康度；
        需要 per-key 详情时显式传 summary=false（默认）。

        v10 增强：
        - keys[].models：每个 key 的所有 model 详情（含 tier、is_free、per-model stats）
        - keys[].is_free / keys[].is_available：是否免费 / 当前是否可分配
        - free_keys_count / paid_keys_count：免费/paid key 数量
        - recent_calls_count：近期调用历史总数（环形缓冲）
        - include_recent_calls=true 时附带 recent_calls 数组
        """
        try:
            from server.llm_pool import get_pool, is_initialized
            if not is_initialized():
                return {"initialized": False, "message": "池未初始化，请先调用 /llm/pool/init"}
            pool = get_pool()
            status = pool.get_status()
            if summary:
                status.pop("keys", None)
                status["summary"] = True
                status["hint"] = "已省略 keys 详情。需要 per-key 详情时传 summary=false。"
            if include_recent_calls:
                status["recent_calls"] = pool.get_recent_calls(
                    limit=recent_calls_limit, only_failed=only_failed_calls
                )
            return {"initialized": True, **status}
        except Exception as e:
            return {"initialized": False, "error": str(e)}

    @app.get("/llm/pool/models", operation_id="llm_pool_models")
    async def llm_pool_models():
        """跨 key 聚合 per-model 统计 + 按 tier 分组的可用 model 清单

        现有字段（向后兼容 GUI 监控面板，来自 pool.get_models_summary()）：
        - total_models / free_models_count / disabled_models_count / probe_in_flight_count
        - models: 跨 key 聚合的 per-model 统计数组（按调用次数倒序）

        v6-lite-chat-fix T02 新增字段（供前端模型选择器拉取，来自 pool.list_models()）：
        - grouped_by_tier: 按 tier 分组的 model 清单
            {"tier1-lightest": [{"model": "...", "tier": 1, "provider": "..."}, ...], ...}
        - default_tier: 取自 USE_CASE_REGISTRY['agent_chat'].default_tier 的 min（如 "tier3-medium"）
        - default_model: default_tier 分组中首个 model（前端启动时默认选中）
        """
        try:
            from server.llm_pool import get_pool, is_initialized
            if not is_initialized():
                return {"initialized": False, "message": "池未初始化，请先调用 /llm/pool/init"}
            pool = get_pool()
            # 现有字段（向后兼容：保留 models / total_models 等供 GUI 监控面板使用）
            result = {"initialized": True, **pool.get_models_summary()}
            # T02 新增字段：按 tier 分组的可用 model 清单（供前端模型选择器）
            result.update(pool.list_models())
            return result
        except Exception as e:
            return {"initialized": False, "error": str(e)}

    @app.get("/llm/pool/recent-calls", operation_id="llm_pool_recent_calls")
    async def llm_pool_recent_calls(
        limit: int = Query(50, description="最多返回条数（1-200）"),
        model: str | None = Query(None, description="按 model 名过滤"),
        key_name: str | None = Query(None, description="按 key 名过滤"),
        only_failed: bool = Query(False, description="true=只返回失败的调用"),
    ):
        """查询近期调用历史（最新在前）

        返回最近 N 次调用的元信息：
        - ts / ts_str：调用时间戳和本地时间字符串
        - key_name / model：使用的 key 和 model
        - success / tokens / error / duration_ms：调用结果

        用于监控面板展示 per-model 近期调用情况。
        环形缓冲上限 200 条（RECENT_CALL_HISTORY_MAX），溢出自动淘汰最旧的。
        """
        try:
            from server.llm_pool import get_pool, is_initialized
            if not is_initialized():
                return {"initialized": False, "message": "池未初始化"}
            pool = get_pool()
            calls = pool.get_recent_calls(limit=limit, model=model,
                                          key_name=key_name, only_failed=only_failed)
            return {"initialized": True, "count": len(calls), "calls": calls}
        except Exception as e:
            return {"initialized": False, "error": str(e)}

    @app.post("/llm/pool/init", operation_id="llm_pool_init")
    async def llm_pool_init():
        """初始化 LLM 并发池

        从 data/llm/keys.json（v6 schema，unified key 库）加载已测试的 API keys 并初始化池。
        v14：pool 策略从 config.toml [llm.pool] 段加载全局默认，per-key 策略从 keys.json pool 段加载。
        """
        try:

            from server.llm_pool import _load_default_policy, init_pool, load_provider_keys
            keys = load_provider_keys()
            if not keys:
                return {
                    "status": "error",
                    "message": "未加载到任何 key。请检查 data/llm/keys.json 是否存在且含 scope=llm 的记录。",
                }
            default_policy = _load_default_policy()
            from server.llm_pool import _resolve_default_stats_file
            stats_file = _resolve_default_stats_file()
            init_pool(keys, stats_file=stats_file, default_policy=default_policy)
            return {
                "status": "ok",
                "keys_loaded": len(keys),
                "policy": {
                    "name": default_policy.name,
                    "retry_count": default_policy.retry_count,
                    "rate_limit_cooldown_seconds": default_policy.rate_limit_cooldown_seconds,
                },
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/llm/pool/stats", operation_id="llm_pool_stats")
    async def llm_pool_stats():
        """查询 per-project token 消耗统计

        返回各项目的 prompt_tokens / completion_tokens / total_tokens / calls。
        合并所有独立的 stats 文件（每个脚本一个文件，避免多进程写冲突）。
        """
        try:
            from server.llm_pool import _resolve_default_stats_file
            stats_file = Path(_resolve_default_stats_file())
            stats_dir = stats_file.parent
            stats_files = sorted(stats_dir.glob("*.json"))
            merged = {}
            sources = []
            for sf in stats_files:
                if not sf.exists():
                    continue
                try:
                    data = json.loads(sf.read_text(encoding="utf-8"))
                    projects = data.get("projects", {})
                    if projects:
                        sources.append(sf.name)
                        for proj, st in projects.items():
                            if proj not in merged:
                                merged[proj] = {"prompt_tokens": 0, "completion_tokens": 0,
                                                "total_tokens": 0, "calls": 0}
                            merged[proj]["prompt_tokens"] += st.get("prompt_tokens", 0)
                            merged[proj]["completion_tokens"] += st.get("completion_tokens", 0)
                            merged[proj]["total_tokens"] += st.get("total_tokens", 0)
                            merged[proj]["calls"] += st.get("calls", 0)
                except Exception:
                    continue
            return {"projects": merged, "sources": sources}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/llm/pool/health-check", operation_id="llm_pool_health_check")
    async def llm_pool_health_check(cleanup: bool = False):
        """对 key 池进行健康检查

        测试所有 key 的可用性：
        - 成功: fail_count 重置为 0
        - 失败: fail_count += 1
        - 连续失败 3 次: 标记为 works=false

        Args:
            cleanup: 是否同时物理删除已标记为 works=false 的 key（默认 False）。
                若 cleanup=True，删除前会自动备份到 <path>.cleanup-backup-<timestamp>。
                建议先 cleanup=False 检查，再调 /llm/pool/cleanup 显式清理。
        """
        try:
            # 路径常量从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
            from lib.secret import get_llm_keys_path
            from server.llm_pool import check_keys_health, cleanup_expired_keys, should_run_health_check
            _keys_file = str(get_llm_keys_path())
            keys_files = [_keys_file]
            seen = set()
            merged_result = {"files": [], "total": 0, "ok": 0, "fail": 0, "removed": 0, "details": []}
            for kf in keys_files:
                if kf in seen or not Path(kf).exists():
                    continue
                seen.add(kf)
                # v12 起 key_store 函数不再接受 filepath 参数（统一走 lib.secret 单一真源）；
                # 健康检查含网络探测，用 asyncio.to_thread 避免阻塞事件循环
                result = await asyncio.to_thread(check_keys_health)
                if "error" in result:
                    merged_result["files"].append({"file": kf, "error": result["error"]})
                    continue
                if cleanup and result.get("removed", 0) > 0:
                    cleanup_result = await asyncio.to_thread(cleanup_expired_keys, backup=True)
                    result["cleanup"] = cleanup_result
                result["auto_check_needed"] = should_run_health_check()
                merged_result["files"].append({"file": kf, **result})
                merged_result["total"] += result.get("total", 0)
                merged_result["ok"] += result.get("ok", 0)
                merged_result["fail"] += result.get("fail", 0)
                merged_result["removed"] += result.get("removed", 0)
                merged_result["details"].extend(result.get("details", []))
            return merged_result
        except Exception as e:
            return {"error": str(e)}

    @app.post("/llm/pool/cleanup", operation_id="llm_pool_cleanup")
    async def llm_pool_cleanup():
        """显式清理所有 keys 文件中 works=false 的 key 条目。

        删除前自动备份到 <path>.cleanup-backup-<timestamp>（保留最近 5 个备份）。
        建议先调 /llm/pool/health-check (cleanup=false) 查看哪些 key 会被删除，
        再调本端点执行物理删除。
        """
        try:
            from lib.secret import get_llm_keys_path
            from server.llm_pool import cleanup_expired_keys
            _keys_file = str(get_llm_keys_path())
            keys_files = [_keys_file]
            seen = set()
            merged_result = {"files": [], "total_removed": 0, "backups": []}
            for kf in keys_files:
                if kf in seen or not Path(kf).exists():
                    continue
                seen.add(kf)
                result = await asyncio.to_thread(cleanup_expired_keys, backup=True)
                if "error" in result:
                    merged_result["files"].append({"file": kf, "error": result["error"]})
                    continue
                merged_result["files"].append({"file": kf, **result})
                merged_result["total_removed"] += result.get("removed", 0)
                if result.get("backup"):
                    merged_result["backups"].append(result["backup"])
            return merged_result
        except Exception as e:
            return {"error": str(e)}

    @app.get("/llm/pool/health-status", operation_id="llm_pool_health_status")
    async def llm_pool_health_status():
        """查询 key 池健康检查状态（不执行检查）

        返回上次检查时间、是否需要检查、各 key 的 fail_count 等。
        读取统一 keys.json（v6 schema）。
        """
        try:
            # 路径常量从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
            from lib.secret import get_llm_keys_path
            from server.llm_pool import _get_status_dict, _load_keys_records, should_run_health_check
            _keys_file = get_llm_keys_path()
            keys_files = [_keys_file]
            seen = set()
            all_keys = []
            files_info = []
            for kf in keys_files:
                if str(kf) in seen or not kf.exists():
                    continue
                seen.add(str(kf))
                try:
                    data, _wrapper = _load_keys_records(kf)
                except Exception:
                    continue  # 非 JSON 文件（如纯文本 key），跳过健康状态统计
                needs_check = should_run_health_check()
                last_check = ""
                if data:
                    # 找该文件中所有记录的最晚 last_health_check
                    for rec in data:
                        lc = _get_status_dict(rec).get("last_health_check", "")
                        if lc and lc > last_check:
                            last_check = lc
                    if not last_check:
                        last_check = "从未检查"
                files_info.append({
                    "file": str(kf),
                    "total_keys": len(data),
                    "active_keys": sum(1 for d in data if _get_status_dict(d).get("works", True)),
                    "needs_health_check": needs_check,
                    "last_check": last_check,
                })
                all_keys.extend(data)
            if not all_keys:
                return {"error": "没有找到任何 keys 文件"}
            return {
                "total_keys": len(all_keys),
                "active_keys": sum(1 for d in all_keys if _get_status_dict(d).get("works", True)),
                "files": files_info,
                "keys": [
                    {
                        "name": d.get("name", "") or d.get("label", ""),
                        "model": d.get("model", ""),
                        "base_url": d.get("base_url", ""),
                        "works": _get_status_dict(d).get("works", True),
                        "fail_count": _get_status_dict(d).get("fail_count", 0),
                        "last_check_status": _get_status_dict(d).get("last_check_status", ""),
                        "last_health_check": _get_status_dict(d).get("last_health_check", ""),
                        "privacy_warning": d.get("privacy_warning", ""),
                    }
                    for d in all_keys
                ],
            }
        except Exception as e:
            return {"error": str(e)}

    @app.post("/llm/pool/call", operation_id="llm_pool_call")
    def llm_pool_call(req: LLMPoolCallRequest):
        """通过后端 LLM 并发池调用 LLM（完整版，返回 content + usage）

        供外部脚本通过 HTTP 代理调用，由后端统一管理 key 并发、重试、token 统计。
        如果池未初始化，会自动初始化。

        Args:
            messages: OpenAI 消息格式
            project: 项目标签，用于 per-project token 统计
        """
        try:
            if not _ensure_pool_initialized():
                return {"ok": False, "error": "LLM 池未初始化且自动初始化失败"}
            from server.llm_pool import get_pool
            pool = get_pool()
            # v8: model_tier 直接传给 pool 做 tier 范围匹配 + model 软偏好。
            # pool 内部会 _normalize_tier_range() 处理 int/str/tuple；None 时用 use_case.default_tier。
            return pool.call(
                req.messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                timeout=req.timeout,
                retries=req.retries,
                project=req.project,
                model=None,  # 不再手动查 model_name；由 pool 在 tier 匹配的 key 中选
                response_format=req.response_format,
                use_case=req.use_case,
                tier=req.model_tier,
                session_id=req.session_id,  # v12: LKGP 会话粘性
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    @app.post("/llm/pool/call-simple", operation_id="llm_pool_call_simple")
    def llm_pool_call_simple(req: LLMPoolCallSimpleRequest):
        """通过后端 LLM 并发池调用 LLM（简化版，返回文本）

        供外部脚本通过 HTTP 代理调用，由后端统一管理 key 并发、重试、token 统计。
        如果池未初始化，会自动初始化。

        Args:
            prompt: 用户提示文本
            project: 项目标签，用于 per-project token 统计
        """
        try:
            if not _ensure_pool_initialized():
                return {"ok": False, "error": "LLM 池未初始化且自动初始化失败"}
            from server.llm_pool import get_pool
            pool = get_pool()
            # v8: model_tier 直接传给 pool 做 tier 硬匹配（详见 /llm/pool/call 注释）
            content = pool.call_simple(
                req.prompt,
                system_prompt=req.system_prompt,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                timeout=req.timeout,
                retries=req.retries,
                project=req.project,
                model=None,
                use_case=req.use_case,
                tier=req.model_tier,
                session_id=req.session_id,  # v12: LKGP 会话粘性
            )
            if content is not None:
                return {"ok": True, "content": content}
            return {"ok": False, "error": "调用失败（详见后端日志）"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    @app.post(
        "/llm/pool/chat-tools",
        operation_id="llm_pool_chat_tools",
        openapi_extra={
            "x-agent-callable": False,  # 引擎专用 LLM gateway，不进 agent tool catalog（防递归）
            "x-tool-safety": "safe",
        },
    )
    def llm_pool_chat_tools(req: LLMPoolChatToolsRequest):
        """v6-lite T05: 带工具的 LLM 调用（引擎 LLMGateway 专用）

        与 /llm/pool/call 的区别：增加 tools / tool_choice 透传，
        返回值额外含 tool_calls 字段（OpenAI 格式 list[dict]）。

        供 v6-lite 引擎的 LLMGateway 调用——模型可在响应中返回 tool_calls，
        由 SessionRunner 解析并执行。引擎不应把此端点当作工具调用（已 x-agent-callable=false）。

        返回：
        - 成功: {"ok": true, "content": str, "usage": dict, "model": str,
                "key_name": str, "finish_reason": str, "tool_calls": list[dict]}
        - 失败: {"ok": false, "error": str}
        """
        try:
            if not _ensure_pool_initialized():
                return {"ok": False, "error": "LLM 池未初始化且自动初始化失败"}
            from server.llm_pool import get_pool
            pool = get_pool()
            return pool.call(
                req.messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                timeout=req.timeout,
                retries=req.retries,
                project=req.project,
                model=None,
                response_format=req.response_format,
                use_case=req.use_case,
                tier=req.model_tier,
                session_id=req.session_id,
                tools=req.tools,
                tool_choice=req.tool_choice,
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ==================== v6-lite T08: SSE 伪流式端点 ====================

    @app.post(
        "/llm/pool/stream",
        operation_id="llm_pool_stream",
        openapi_extra={
            "x-agent-callable": False,  # 引擎专用，不进 agent tool catalog
            "x-tool-safety": "safe",
        },
    )
    async def llm_pool_stream(req: LLMPoolChatToolsRequest, request: Request):
        """v6-lite-streaming-gui T00: SSE 真流式 LLM 调用端点

        真流式策略（D02）：
        1. 调 pool.stream()（async generator，对接 provider stream=True）
        2. 透传 provider SSE 事件给客户端（text_delta/thinking_delta/tool_call_delta/usage/done）
        3. 客户端断开 → 停止迭代 → pool.stream() 的 finally 释放 key/semaphore（D14）
        4. tool_call_delta 半包累积在 pool.stream() 内部完成（D04），端点只透传完整事件
        5. Anthropic 协议降级为伪流式（pool.stream() 内部处理）
        """
        async def event_generator():
            try:
                if not _ensure_pool_initialized():
                    yield _sse_event("provider_error", {"error": "LLM pool not initialized"})
                    yield _sse_event("done", {"finish_reason": "error"})
                    return

                from server.llm_pool import get_pool
                pool = get_pool()

                # 真流式：pool.stream() 是 async generator，逐事件透传
                async for event in pool.stream(
                    req.messages,
                    temperature=req.temperature,
                    max_tokens=req.max_tokens,
                    timeout=req.timeout,
                    project=req.project,
                    model=None,
                    use_case=req.use_case,
                    tier=req.model_tier,
                    session_id=req.session_id,
                    tools=req.tools,
                    tool_choice=req.tool_choice,
                ):
                    # 客户端断开检测（D14）：停止迭代，pool.stream 的 finally 释放 key
                    if await request.is_disconnected():
                        logger.info("llm_pool_stream: client disconnected, stopping stream")
                        return
                    # pool.stream 返回的事件已是完整 dict（含 type 字段），直接序列化
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") == "done":
                        break

            except Exception as e:
                logger.exception("llm_pool_stream error")
                yield _sse_event("provider_error", {
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                })
                yield _sse_event("done", {"finish_reason": "error"})

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx 不缓冲
            },
        )

    # ==================== v12: LKGP 会话粘性端点 ====================

    @app.get("/llm/pool/session", operation_id="llm_pool_session_list")
    async def llm_pool_session_list():
        """查询 LKGP 会话粘性详情（v12）

        返回当前所有活跃的会话粘性记录：
        - total_sessions: 当前粘性会话数
        - ttl_seconds: 粘性 TTL（默认 1800s）
        - max_sessions: 上限（防内存膨胀，默认 500，LRU 淘汰）
        - sessions: 每条粘性的 session_id/key_id/model/last_used/age_seconds/expires_in_seconds

        用途：监控同一会话粘到哪个 (key, model)，调试会话连续性问题。
        """
        try:
            from server.llm_pool import get_pool, is_initialized
            if not is_initialized():
                return {"initialized": False, "message": "池未初始化"}
            pool = get_pool()
            return {"initialized": True, **pool.get_session_stickiness()}
        except Exception as e:
            return {"initialized": False, "error": str(e)}

    @app.delete("/llm/pool/session/{session_id}", operation_id="llm_pool_session_delete")
    async def llm_pool_session_delete(session_id: str):
        """主动清除指定会话的粘性（v12）

        用户主动结束会话或换主题时调用，让下次调用走正常 round-robin 选 key。
        返回 {"cleared": true/false}。
        """
        try:
            from server.llm_pool import get_pool, is_initialized
            if not is_initialized():
                return {"initialized": False, "message": "池未初始化"}
            pool = get_pool()
            pool.clear_session(session_id)
            return {"initialized": True, "cleared": True, "session_id": session_id}
        except Exception as e:
            return {"initialized": False, "error": str(e)}
