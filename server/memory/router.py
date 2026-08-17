"""三层记忆系统 FastAPI 路由

完全向后兼容 /memory/* 端点，同时新增语义搜索等接口。
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Path, Query
from pydantic import Field, field_validator

from lib.schema import BaseSchema
from server.memory.manager import get_memory_manager

logger = logging.getLogger("localagent.memory.router")

router = APIRouter(prefix="/memory", tags=["memory"])

_KEY_PATTERN = r'^[a-zA-Z0-9_-]{1,128}$'

# v6.1 T29：5 类 closed taxonomy（spec D7）
# 君子协议：未知类型落 NULL + log warning，不强制拒绝
VALID_FACT_TYPES = ("user", "feedback", "project", "reference", "experience")

# v6.1 T31：staleness 阈值表（spec D14，按 fact_type 分类）
STALENESS_THRESHOLDS_DAYS = {
    "user": 90,        # 用户偏好/身份极稳定
    "feedback": 30,    # 反馈会随偏好演进而变化
    "project": 7,      # 项目迭代快，架构/约定易改
    "reference": 30,   # 链接/命令可能失效但不一定过时
    "experience": 14,  # 库版本更新会修复坑点，经验易过时
}


# ─── Request/Response 模型 ──────────────────────────────────

class MemorySetRequest(BaseSchema):
    """写入记忆请求（兼容旧接口，支持深度合并 + v3 结构化字段）

    v3 新增：fact_type / occurred_at / consumption_contexts / trigger_keywords
    这些字段作为 fact 的元数据写入 facts 表的独立列，不属于 data。
    若同时通过 data 内部传入同名键，请求顶层字段优先（覆盖）。

    v6.1 T29：fact_type 收敛为 5 类 closed（user/feedback/project/reference/experience）。
    君子协议：未知类型不强制拒绝，validator 落 None + log warning。
    """
    data: dict = Field(..., description="记忆数据")
    merge: bool = Field(True, description="是否深度合并（旧接口兼容）")
    # v3 结构化字段（可选）
    fact_type: str | None = Field(
        None,
        description="事实类型（v6.1 closed）: user|feedback|project|reference|experience",
    )
    occurred_at: str | None = Field(
        None, description="事实发生时间 ISO 字符串（区别于写入时间）"
    )
    consumption_contexts: list[str] | None = Field(
        None,
        description=(
            "消费场景列表，哪些 task_type 应读取此记忆。"
            '支持通配 "*"（全场景）和 "scope.*"（如 "recurring.*"）'
        ),
    )
    trigger_keywords: list[str] | None = Field(
        None,
        description="触发关键词列表，任务描述/用户消息出现这些词时应读取此记忆",
    )

    @field_validator("fact_type")
    @classmethod
    def _validate_fact_type(cls, v: str | None) -> str | None:
        """v6.1 T29：closed taxonomy 君子协议。

        - None / 空 → 透传 None（不改）
        - 5 类之内 → 透传
        - 5 类之外（含旧值 preference/transaction）→ 落 None + log warning
          （后端不强制拒绝，但 prompt 已强约束；旧值由 migrate_fact_type_v6_1.py 处理）
        """
        if v is None or v == "":
            return None
        if v in VALID_FACT_TYPES:
            return v
        logger.warning(
            "fact_type=%r 不在 closed taxonomy %r 内（君子协议：落 NULL，不强制拒绝）",
            v, VALID_FACT_TYPES,
        )
        return None


class MemorySearchRequest(BaseSchema):
    """语义搜索请求"""
    query: str = Field(..., min_length=1, max_length=1000, description="搜索查询")
    top_k: int = Field(10, ge=1, le=100, description="返回结果数")
    since: float | None = Field(None, description="起始时间戳")
    until: float | None = Field(None, description="结束时间戳")
    source: str | None = Field(None, description="消息来源过滤")


class MemoryRecordRequest(BaseSchema):
    """手动记录请求"""
    content: str = Field(..., min_length=1, description="内容")
    source: str = Field("manual", description="来源: agent/user/tool/system/manual")
    role: str = Field("assistant", description="角色: user/assistant/tool/system")
    key: str | None = Field(None, pattern=_KEY_PATTERN, description="可选 key（兼容 KV 接口，仅字母/数字/下划线/连字符，1-128 字符）")
    session_id: str | None = Field(None, description="会话 ID")


# ─── 向后兼容端点 ───────────────────────────────────────────

@router.get("/status", operation_id="memory_status",
            summary="记忆系统状态")
async def memory_status():
    """获取记忆系统状态（增强版）"""
    mgr = get_memory_manager()
    return await asyncio.to_thread(mgr.get_status)


@router.get("/list", operation_id="memory_list",
            summary="列出所有记忆 key")
async def memory_list():
    """列出所有记忆 key 及摘要"""
    mgr = get_memory_manager()
    keys = await asyncio.to_thread(mgr.list_keys)
    return {"keys": keys, "total": len(keys)}


@router.post("/cleanup/orphaned", operation_id="memory_cleanup_orphaned",
             summary="清理孤立记忆")
async def memory_cleanup_orphaned():
    """清理超过 orphan_cleanup_days 天的已压缩消息和超过 summary_cleanup_days 天的旧摘要"""
    mgr = get_memory_manager()
    cutoff = time.time() - mgr.config.orphan_cleanup_days * 86400

    # 清理旧消息
    old_messages = mgr.store.query_messages(until=cutoff, compressed=1, limit=1000)
    deleted_msgs = 0
    for msg in old_messages:
        # 只删除已压缩的旧消息
        mgr.store.delete_message(msg["id"])
        deleted_msgs += 1

    # 清理旧摘要
    summary_cutoff = time.time() - mgr.config.summary_cleanup_days * 86400
    mgr.store.conn.execute(
        "DELETE FROM summaries WHERE end_time < ?", (summary_cutoff,)
    )
    mgr.store.conn.commit()

    return {
        "deleted_messages": deleted_msgs,
        "message_cutoff_days": mgr.config.orphan_cleanup_days,
        "summary_cutoff_days": mgr.config.summary_cleanup_days,
        "status": "ok",
    }


# ─── 新增端点 ───────────────────────────────────────────────

@router.post("/search", operation_id="memory_search",
             summary="语义搜索记忆")
async def memory_search(req: MemorySearchRequest):
    """混合语义搜索（向量 + BM25）

    支持降级模式：嵌入模型不可用时自动降级为 BM25-only。

    v6.1 T31：返回结果每条追加 staleness 字段（memory_age / staleness_warning / trust_recall_hint）。
    """
    mgr = get_memory_manager()
    results = await asyncio.to_thread(
        mgr.search,
        query=req.query,
        top_k=req.top_k,
        since=req.since,
        until=req.until,
        source=req.source,
    )
    # T31: 追加 staleness 字段（results 是 list[dict]，原地修改）
    _enrich_with_staleness(results)
    return {"results": results, "total": len(results), "query": req.query}


@router.get("/timeline", operation_id="memory_timeline",
            summary="时间线查询")
async def memory_timeline(
    since: float | None = Query(None, description="起始时间戳"),
    until: float | None = Query(None, description="结束时间戳"),
    source: str | None = Query(None, description="消息来源过滤"),
    limit: int = Query(50, ge=1, le=500, description="返回数量"),
):
    """按时间范围查询消息"""
    mgr = get_memory_manager()
    messages = await asyncio.to_thread(
        mgr.timeline, since=since, until=until, source=source, limit=limit
    )
    return {"messages": messages, "total": len(messages)}


@router.post("/compress", operation_id="memory_compress",
             summary="手动触发记忆压缩")
async def memory_compress(force: bool = Query(False, description="是否强制压缩")):
    """手动触发记忆压缩

    将旧消息压缩为摘要，节省存储空间和上下文窗口。
    """
    mgr = get_memory_manager()
    result = await mgr.compress.compress(force=force)
    return result


@router.post("/record", operation_id="memory_record",
             summary="手动记录交互")
async def memory_record(req: MemoryRecordRequest):
    """手动记录一条交互（也可由自动记录器调用）"""
    mgr = get_memory_manager()
    msg_id = mgr.store.insert_message(
        content=req.content,
        source=req.source,
        role=req.role,
        key=req.key,
        session_id=req.session_id,
    )
    # 建立语义索引
    mgr.semantic.index_message(msg_id, req.content)
    # 写入 Recent 层
    mgr.recent.add(req.content, source=req.source, role=req.role, key=req.key)

    return {"id": msg_id, "status": "ok"}


@router.get("/stats", operation_id="memory_stats",
            summary="详细记忆统计")
async def memory_stats():
    """获取详细的记忆系统统计信息"""
    mgr = get_memory_manager()
    stats = mgr.get_status()

    # 添加 BM25 统计
    bm25_stats = mgr.store.get_bm25_stats()
    stats["bm25"] = bm25_stats

    # 添加摘要统计
    summaries = mgr.store.query_summaries(limit=5)
    stats["recent_summaries"] = len(summaries)

    return stats


@router.post("/reindex", operation_id="memory_reindex",
             summary="重建语义索引")
async def memory_reindex():
    """重建向量索引和 BM25 统计（维护操作）"""
    mgr = get_memory_manager()

    # 重建向量索引
    vector_result = mgr.semantic.rebuild_vector_index()

    # 重建 BM25 统计
    bm25_result = mgr.semantic.bm25.rebuild_stats()

    return {
        "vectors": vector_result,
        "bm25": bm25_result,
        "status": "ok",
    }


# ─── 维护器端点（必须在 /{key} 动态路由之前） ────────────────

class MaintainRequest(BaseSchema):
    """手动触发维护请求"""
    force: bool = Field(False, description="是否强制运行（忽略 should_run 检查）")


@router.post("/maintain", operation_id="memory_maintain",
             summary="手动触发记忆维护（老化+验证）")
async def memory_maintain(req: MaintainRequest):
    """手动触发记忆维护器：扫描 stale 记忆 + LLM 验证。

    通常自动触发（每 maintain_interval_hours 小时），也可手动调用。
    force=True 时忽略 should_run 检查立即执行。
    """
    mgr = get_memory_manager()
    if not req.force and not mgr.maintainer.should_run():
        return {"status": "skipped", "reason": "not_due_yet",
                "last_run": mgr.maintainer.get_status().get("last_run")}
    result = await mgr.maintainer.run()
    return {"status": "ok", "result": result}


@router.get("/maintain/status", operation_id="memory_maintain_status",
            summary="记忆维护器状态")
async def memory_maintain_status():
    """获取维护器状态：上次运行时间、stale 数量、验证统计"""
    mgr = get_memory_manager()
    return mgr.maintainer.get_status()


# ─── Search Tracer 端点 (v3 C) ────────────────────────────────────

@router.get("/search/traces", operation_id="memory_search_traces_list",
            summary="最近检索 trace 列表")
async def memory_search_traces_list(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    since_id: int | None = Query(None, description="游标分页（仅返回 id > since_id 的记录）"),
):
    """列出最近 search_traces 记录（不含 candidates/final_results 详情）"""
    mgr = get_memory_manager()
    traces = await asyncio.to_thread(
        mgr.search_tracer.get_recent, limit=limit, since_id=since_id
    )
    return {"traces": traces, "total": len(traces)}


@router.get("/search/traces/{trace_id}", operation_id="memory_search_traces_detail",
            summary="单条检索 trace 详情")
async def memory_search_traces_detail(trace_id: int):
    """获取单条 trace 详情（含 candidates、final_results 完整 JSON）"""
    mgr = get_memory_manager()
    detail = await asyncio.to_thread(mgr.search_tracer.get_detail, trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"trace_id={trace_id} 不存在")
    return detail


@router.delete("/search/traces/cleanup", operation_id="memory_search_traces_cleanup",
               summary="清理过期检索 trace")
async def memory_search_traces_cleanup(
    days: int | None = Query(None, ge=1, le=365,
                              description="覆盖 retention_days 配置（可选）"),
):
    """清理超过 retention_days 的旧 trace

    days 参数可选，未传时使用配置中的 search_tracer_retention_days。
    """
    mgr = get_memory_manager()
    if days is not None:
        # 临时覆盖 retention_days
        original = mgr.search_tracer.retention_days
        mgr.search_tracer.retention_days = days
        try:
            result = await asyncio.to_thread(mgr.search_tracer.cleanup_old)
        finally:
            mgr.search_tracer.retention_days = original
    else:
        result = await asyncio.to_thread(mgr.search_tracer.cleanup_old)
    return {"status": "ok", "result": result}


# ─── Evidence Ledger 端点 (v3 A) ────────────────────────────────────

@router.get("/evidence/recent", operation_id="memory_evidence_recent",
            summary="最近证据集列表")
async def memory_evidence_recent(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
):
    """列出最近 evidence_ledger 记录（按 id DESC）"""
    mgr = get_memory_manager()
    evidences = await asyncio.to_thread(mgr.evidence_ledger.get_recent, limit=limit)
    return {"evidences": evidences, "total": len(evidences)}


@router.get("/evidence/{evidence_id}", operation_id="memory_evidence_detail",
            summary="单条证据详情")
async def memory_evidence_detail(evidence_id: int):
    """获取单条证据详情（含 conflicting_ids 解析）"""
    mgr = get_memory_manager()
    detail = await asyncio.to_thread(mgr.evidence_ledger.get_detail, evidence_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"evidence_id={evidence_id} 不存在")
    return detail


@router.delete("/evidence/cleanup", operation_id="memory_evidence_cleanup",
               summary="清理过期证据")
async def memory_evidence_cleanup(
    days: int | None = Query(None, ge=1, le=365,
                              description="覆盖 evidence_audit_keep_days 配置（可选）"),
):
    """清理超过 evidence_audit_keep_days 的旧证据

    days 参数可选，未传时使用配置中的 evidence_audit_keep_days。
    """
    mgr = get_memory_manager()
    result = await asyncio.to_thread(mgr.evidence_ledger.cleanup_old, days)
    return {"status": "ok", "result": result}


# ─── 动态 key 端点必须放在固定路径之后 ───────────────────────

@router.get("/{key}", operation_id="memory_get",
            summary="读取记忆")
async def memory_get(key: str = Path(..., pattern=_KEY_PATTERN, description="记忆 key（仅字母、数字、下划线、连字符）")):
    """按 key 读取记忆（兼容旧 KV 接口，同时搜索 facts 和 messages）

    v6.1 T31：返回结果追加 staleness 字段（memory_age / staleness_warning / trust_recall_hint）。
    仅当 fact_type 在 5 类 closed 内且超阈值时填 staleness_warning。
    """
    mgr = get_memory_manager()
    data = await asyncio.to_thread(mgr.get, key)
    if data is None:
        raise HTTPException(status_code=404, detail=f"记忆 '{key}' 不存在")
    _enrich_with_staleness(data)
    return data


@router.post("/{key}", operation_id="memory_set",
             summary="写入记忆")
async def memory_set(key: str = Path(..., pattern=_KEY_PATTERN), req: MemorySetRequest = Body(...)):  # noqa: B008
    """写入记忆（兼容旧 KV 接口，同时写入 facts 和 messages）

    支持深度合并：如果 key 已存在，新数据会与旧数据深度合并。

    v3 新增：支持 fact_type / occurred_at / consumption_contexts / trigger_keywords
    顶层字段，这些字段会作为 fact 元数据写入 facts 表的独立列，
    供 find_consumable_memories 用 SQL 索引快速过滤。
    若 data 内部也含同名键，顶层字段优先（覆盖）。
    """
    mgr = get_memory_manager()

    if req.merge:
        # 深度合并模式
        existing = mgr.get(key)
        merged = _deep_merge(existing, req.data) if existing and isinstance(existing, dict) else req.data
    else:
        merged = req.data

    # v3 结构化字段（仅传非 None 的，None 表示未指定，保留现有值）
    structured: dict = {}
    if req.fact_type is not None:
        structured["fact_type"] = req.fact_type
    if req.occurred_at is not None:
        structured["occurred_at"] = req.occurred_at
    if req.consumption_contexts is not None:
        structured["consumption_contexts"] = req.consumption_contexts
    if req.trigger_keywords is not None:
        structured["trigger_keywords"] = req.trigger_keywords

    result = mgr.set(key, merged, structured=structured or None)
    return result


@router.delete("/{key}", operation_id="memory_delete",
               summary="删除记忆")
async def memory_delete(key: str = Path(..., pattern=_KEY_PATTERN)):
    """删除指定 key 的记忆"""
    mgr = get_memory_manager()
    deleted = mgr.delete(key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"记忆 '{key}' 不存在")
    return {"status": "deleted", "key": key}


# ─── 内部工具 ───────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# v6.1 T31：staleness 警告计算（spec D14 + D9）
_TRUST_RECALL_HINT = "引用文件/函数前请用 file_read/file_grep 验证仍存在"


def _format_memory_age(days: int) -> str:
    """相对时间格式（回收 v6-07:260-271）。

    0 天 → "today"，1 天 → "yesterday"，其他 → "N days ago"。
    模型对相对时间推理优于 ISO 时间戳。
    """
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _compute_staleness(fact: dict) -> dict:
    """计算单条 fact 的 staleness 字段（spec D14）。

    Args:
        fact: 含 fact_type / updated_at 字段的 dict（来自 facts 表或 memory_index 项）

    Returns:
        dict 含三个字段（仅追加，不修改原 dict）：
        - memory_age: str — 相对时间格式（today/yesterday/N days ago）
        - staleness_warning: str | None — 超阈值时的警告文案；未超阈值或 fact_type 未知时为 None
        - trust_recall_hint: str — 常驻提示"引用前请验证"

    阈值表（STALENESS_THRESHOLDS_DAYS）：
    - user 90 / feedback 30 / project 7 / reference 30 / experience 14
    - fact_type=NULL 或未知 → 不算 staleness（staleness_warning=None）
    """
    from datetime import datetime as _dt

    fact_type = fact.get("fact_type")
    updated_at = fact.get("updated_at")

    # 默认值
    memory_age: str | None = None
    staleness_warning: str | None = None

    if updated_at:
        try:
            # facts 表的 updated_at 格式：'YYYY-MM-DD HH:MM:SS'（sqlite datetime('now', 'localtime')）
            updated_dt = _dt.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
            days_since = (_dt.now() - updated_dt).days
            memory_age = _format_memory_age(days_since)

            # 仅当 fact_type 在 closed taxonomy 内时算 staleness
            if fact_type and fact_type in STALENESS_THRESHOLDS_DAYS:
                threshold = STALENESS_THRESHOLDS_DAYS[fact_type]
                if days_since > threshold:
                    staleness_warning = (
                        f"⚠️ 此记忆最后更新于 {memory_age}"
                        f"（fact_type={fact_type} 类型阈值 {threshold} 天），可能过时。"
                        f"{_TRUST_RECALL_HINT}。"
                    )
        except (ValueError, TypeError):
            # updated_at 解析失败 → memory_age=None, staleness_warning=None
            pass

    return {
        "memory_age": memory_age,
        "staleness_warning": staleness_warning,
        "trust_recall_hint": _TRUST_RECALL_HINT,
    }


def _enrich_with_staleness(data: dict | list) -> dict | list:
    """对 memory_get / memory_search 返回结果追加 staleness 字段。

    - dict：单条 fact，直接追加
    - list：多条结果，逐条追加
    - 其他类型：透传不动

    幂等：已有 staleness_warning 字段时跳过（防重复计算）。
    """
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "staleness_warning" not in item:
                item.update(_compute_staleness(item))
        return data
    if isinstance(data, dict) and "staleness_warning" not in data:
        data.update(_compute_staleness(data))
    return data
