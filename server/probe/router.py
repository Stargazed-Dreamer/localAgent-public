"""chat-mgmt-probe Ticket 03: 探针 HTTP 路由

端点：
- POST /probe/runs：启动探针会话，返回 ProbeSummary

通过 localagent_advanced_tool(tool="probe_run", params={...}) 网关调用。
operation_id="probe_run"。

设计参考：server/activity_tracker/headless_endpoints.py 的 router 模式。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.schema import BaseSchema
from server.probe.runner import ProbeParams, ProbeSummary, run_probe

logger = logging.getLogger("localagent.probe.router")

router = APIRouter(prefix="/probe", tags=["probe"])


# ============================================================================
# Pydantic 模型
# ============================================================================


class ProbeRunRequest(BaseSchema):
    """POST /probe/runs 请求体。"""

    trigger_text: str = Field(..., description="触发任务文本（如 hello）")
    mode: str = Field("script", description='"script" | "pattern"')
    script: list = Field(default_factory=list, description="script 模式：每条 str | dict")
    pattern: str = Field("", description="pattern 模式：pattern 名（Ticket 04）")
    use_stream: bool = Field(False, description="是否走流式路径（Ticket 05）")
    trigger_approval: bool = Field(False, description="是否触发审批路径（Ticket 05）")
    max_context_messages: int = Field(0, description="上下文压缩触发阈值，0=不限制（Ticket 05）")
    auto_cleanup: bool = Field(True, description="跑完是否自动删除会话")
    db_path: str = Field("", description="独立 DB 路径，空则用默认 data/client/agent.db")
    base_url: str = Field("http://127.0.0.1:8766", description="后端 base_url")


class ProbeSummaryResponse(BaseSchema):
    """POST /probe/runs 响应体（ProbeSummary 的 Pydantic 版本）。"""

    session_id: str
    mode: str
    status: str
    messages_count: int
    events_count: int
    tool_calls_count: int
    messages_preview: list[dict]
    tool_calls_preview: list[dict]
    llm_calls: list[dict]
    run_dir: str
    cleaned_up: bool


# ============================================================================
# 端点
# ============================================================================


@router.post(
    "/runs",
    operation_id="probe_run",
    response_model=ProbeSummaryResponse,
)
async def probe_run_endpoint(req: ProbeRunRequest) -> ProbeSummaryResponse:
    """启动探针会话。

    用 MockLLM 替代真 LLM 跑完整对话生命周期，采集数据落盘，返回摘要。
    用于评估项目自己的对话管理系统（EventStore + SessionRunner + 工具调用）。

    Ticket 02 范围：script 模式 + 纯文本 + 非流式 + 无工具。
    Ticket 04+ 扩展 pattern 模式 + tool_call。
    Ticket 05+ 扩展流式 / 审批 / 压缩。
    """
    logger.info(
        "probe_run request: mode=%s trigger_len=%d use_stream=%s auto_cleanup=%s",
        req.mode, len(req.trigger_text), req.use_stream, req.auto_cleanup,
    )
    try:
        params = ProbeParams(
            trigger_text=req.trigger_text,
            mode=req.mode,
            script=req.script,
            pattern=req.pattern,
            use_stream=req.use_stream,
            trigger_approval=req.trigger_approval,
            max_context_messages=req.max_context_messages,
            auto_cleanup=req.auto_cleanup,
            db_path=req.db_path,
            base_url=req.base_url,
        )
        summary: ProbeSummary = await run_probe(params)
        return ProbeSummaryResponse(
            session_id=summary.session_id,
            mode=summary.mode,
            status=summary.status,
            messages_count=summary.messages_count,
            events_count=summary.events_count,
            tool_calls_count=summary.tool_calls_count,
            messages_preview=summary.messages_preview,
            tool_calls_preview=summary.tool_calls_preview,
            llm_calls=summary.llm_calls,
            run_dir=summary.run_dir,
            cleaned_up=summary.cleaned_up,
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=f"Not implemented yet: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("probe_run failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
