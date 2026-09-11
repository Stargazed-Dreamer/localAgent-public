"""Model Lifecycle Manager — 统一控制面路由（design §10）

端点（全部纳入审批分类，design §10 P0）：
- GET    /models                  → 全部注册模型状态（read_only）
- GET    /models/pressure         → 各资源压力态 + used/total + 最近迁移事件（read_only）
- POST   /models/{id}/load        → 手动加载（approval_required）
- POST   /models/{id}/unload      → 手动卸载（approval_required）
- POST   /models/pause            → 手动超驰：暂停守护（approval_required）
- POST   /models/resume           → 恢复守护（approval_required）

设计要点：
- 变更类 POST 全部归 approval_required，由 route_tags 的 /models 前缀匹配自动分类
- GET 端点 read_only，agent 可直接查（低频，不进白名单也安全）
- 端点契约：last_load_ms 可空（design §5.5）；pause/resume 的 resource 参数可选
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.model_manager.routes")

router = APIRouter(prefix="/models", tags=["ModelManager"])


class ModelActionResponse(BaseSchema):
    """单模型操作响应（load/unload 通用）"""
    model_id: str
    status: str            # ok | refused | error | already_unloaded
    detail: str | None = None
    load_ms: int | None = None
    reason: str | None = None        # refused 时的原因码
    fail_count: int | None = None
    cooldown_sec: float | None = None
    reload_degraded: bool | None = None


class PauseResumeRequest(BaseSchema):
    """pause/resume 请求：resource 缺省=全部"""
    resource: str | None = None      # "gpu" | "cpu" | None=全部


class PauseResumeResponse(BaseSchema):
    status: str
    events: list[dict] = []


def _get_manager():
    from server.model_manager import get_model_manager
    return get_model_manager()


@router.get("", operation_id="models_list")
async def list_models() -> dict:
    """列出全部注册模型的状态（design §10）"""
    return {"models": _get_manager().models_report()}


@router.get("/pressure", operation_id="models_pressure")
async def get_pressure() -> dict:
    """各资源压力态 + used/total + 最近迁移事件（design §10）"""
    return _get_manager().pressure_report()


@router.post("/{model_id}/load", response_model=ModelActionResponse, operation_id="models_load")
async def load_model(model_id: str) -> ModelActionResponse:
    """手动加载模型（design §10）

    受压力态约束，但豁免冷却/降级（操作者显式重试）。
    拒绝时返回 status=refused + reason（不抛 503，让调用方程序化区分）。
    """
    result = await _get_manager().manual_load(model_id)
    return ModelActionResponse(
        model_id=model_id,
        status=result.get("status", "error"),
        detail=result.get("detail"),
        load_ms=result.get("load_ms"),
        reason=result.get("reason"),
        fail_count=result.get("fail_count"),
        cooldown_sec=result.get("cooldown_sec"),
        reload_degraded=result.get("reload_degraded"),
    )


@router.post("/{model_id}/unload", response_model=ModelActionResponse, operation_id="models_unload")
async def unload_model(model_id: str) -> ModelActionResponse:
    """手动卸载模型（design §10）

    不走压力门控（操作者显式动作）；仍走线程与超时保护。
    未加载时返回 status=ok, detail=already_unloaded（幂等）。
    """
    result = await _get_manager().manual_unload(model_id)
    return ModelActionResponse(
        model_id=model_id,
        status=result.get("status", "error"),
        detail=result.get("detail"),
    )


@router.post("/pause", response_model=PauseResumeResponse, operation_id="models_pause")
async def pause_manager(req: PauseResumeRequest) -> PauseResumeResponse:
    """手动超驰：暂停守护（design §10）

    PAUSED 期间准入放行且暂停逐出。resource 缺省=全部资源。
    """
    events = _get_manager().pause(req.resource)
    return PauseResumeResponse(status="paused", events=events)


@router.post("/resume", response_model=PauseResumeResponse, operation_id="models_resume")
async def resume_manager(req: PauseResumeRequest) -> PauseResumeResponse:
    """恢复守护（design §10）"""
    events = _get_manager().resume(req.resource)
    return PauseResumeResponse(status="resumed", events=events)
