"""LLM Agent路由 - 评分、对话等"""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema

from .llm_pool import call_llm, call_llm_simple, is_initialized
from .llm_pool.key_store import TIER_NAMES, resolve_key

logger = logging.getLogger("localagent.agent")
router = APIRouter(prefix="/agent", tags=["Agent"])


# ========== 请求/响应模型 ==========

class ScoreRequest(BaseSchema):
    """这是一个用于表示评分请求的 Pydantic 模型类。

    功能：
        封装评分请求的相关数据，包括内容、提示词和模型等级。

    参数：
        content (str): 必填参数，表示需要评分的内容。
        prompt (str): 可选参数，表示提示词，默认为空字符串。
        model_tier (str): 可选参数，表示模型等级，默认为 'default'，可选值包括 'default'、'cheap'、'powerful'。

    返回值：
        无，这是一个数据模型类，用于实例化评分请求对象。
    """
    content: str
    prompt: str = ""
    model_tier: str = "default"  # 可选值：default、cheap、powerful

class ScoreResponse(BaseSchema):
    """
    用于封装评分响应结果的数据模型类。

    参数:
        success (bool): 表示评分请求是否成功。
        result (str): 评分结果，通常为分数或等级。
        model (str): 用于评分的模型名称或标识。
        model_tier (str): 模型的类别或层级。
        elapsed_ms (int): 评分过程所消耗的时间，单位为毫秒。

    返回值:
        该类本身是一个数据容器，用于结构化地存储和传递评分响应信息。
    """
    success: bool  # 请求是否成功的标志
    result: str  # 评分结果的具体内容
    model: str  # 使用的模型名称
    model_tier: str  # 模型所属的层级或类别
    elapsed_ms: int  # 评分过程耗时（毫秒）

"""封装聊天请求的参数。

该类用于表示一个聊天请求的数据结构，通常作为发送给AI模型的请求体。

参数:
    messages (list[dict]): 对话消息列表，每个字典代表一条消息。
    model_tier (str): 模型服务层级，默认为"default"。
    model (str): 具体的模型名称，默认为空字符串，由系统决定。
    temperature (float): 生成文本的随机性参数，值越低确定性越高，默认为0.3。

返回:
    ChatRequest: 一个包含指定参数的ChatRequest实例。
"""
class ChatRequest(BaseSchema):
    messages: list[dict]  # 对话历史记录列表
    model_tier: str = "default"  # 指定使用模型的服务等级
    model: str = ""  # 具体模型名称，留空则使用默认模型
    temperature: float = 0.3  # 控制输出的随机性，0.3表示较低随机性

class ChatResponse(BaseSchema):
    """此类用于表示聊天响应。
    参数：
        success (bool): 表示请求是否成功。
        content (str): 响应内容。
        model (str): 使用的模型名称。
        model_tier (str): 模型层级。
        usage (dict): 使用情况统计，如token使用量。
        elapsed_ms (int): 响应耗时，单位为毫秒。
    返回：
        ChatResponse: 一个表示聊天响应的实例。
    """
    success: bool
    content: str
    model: str
    model_tier: str
    usage: dict
    elapsed_ms: int

class AgentStatusResponse(BaseSchema):
    """
    代理状态响应数据类。

    用于表示代理（Agent）的当前配置状态及可用的模型信息。

    参数：
        configured (bool): 标识代理是否已完成配置。
        models (dict): 一个字典，键表示服务层级（tier），值表示对应的模型名称（model_name）。

    用途：
        通常作为API响应体，向调用方返回代理的服务状态详情。
    """
    configured: bool  # 布尔值，指示代理是否已配置
    models: dict  # 字典，结构为 {服务层级: 模型名称}


DEFAULT_SCORE_PROMPT = """你是一个知识库内容评分专家。请对以下文章进行多维度评分。

评分维度（每项0-10分）：
1. 知识/技术深度：内容的深度和专业性
2. 实用可操作性：能否直接指导实践
3. 思想启发性：是否提供新视角或突破认知
4. 信息稀缺度：是否难以从其他渠道获取
5. 结构清晰度：组织是否清晰、逻辑是否连贯
6. 趣味/可读性：是否引人入胜

输出格式：
- 标题：（提取文章标题）
- 作者：（如有）
- 体裁：（深度复盘/盘点推荐/观点论述/随笔杂谈/教程指南）
- 关键词：（3-5个）
- 核心摘要：（100字以内）
- 评分：知识X 实用X 启发X 稀缺X 结构X 可读X | 总分:XX
- 推荐理由：（一句话，总分>=40时标注⭐）

待评分内容：
"""


# ========== 路由 ==========

def _resolve_key_for_use(use_case: str, tier=None) -> dict:
    """统一获取 LLM key：从 unified keys.json 读取（tier 自动从 use_case 推断）

    返回 {"api_key", "base_url", "model", "source", "label", "privacy_warning"}
    无可用 key 时返回 {"api_key": ""}。
    """
    resolved = resolve_key(use_case, tier)
    if resolved and resolved.api_key:
        return {
            "api_key": resolved.api_key,
            "base_url": resolved.base_url,
            "model": resolved.model,
            "source": "unified_json",
            "label": resolved.label,
            "privacy_warning": resolved.privacy_warning,
        }
    return {"api_key": "", "base_url": "", "model": "", "source": "none",
            "label": "", "privacy_warning": ""}


@router.get("/status", response_model=AgentStatusResponse, operation_id="agent_status")
async def agent_status():
    """查询Agent配置状态"""
    tiers = {}
    for tier in (2, 3, 5):  # 旧 cheap/default/powerful 对应 2/3/5
        resolved = resolve_key("agent_chat", tier)
        if resolved and resolved.api_key:
            tiers[TIER_NAMES[tier]] = resolved.model
        else:
            tiers[TIER_NAMES[tier]] = "(no key)"
    configured = bool(tiers.get("tier3-medium") and tiers["tier3-medium"] != "(no key)")
    return AgentStatusResponse(configured=configured, models=tiers)


@router.post("/score", response_model=ScoreResponse, operation_id="agent_score")
async def agent_score(req: ScoreRequest):
    """使用LLM对内容进行评分"""
    cfg = _resolve_key_for_use("agent_chat", req.model_tier)
    if not cfg["api_key"] or cfg["api_key"] == "sk-your-api-key-here":
        raise HTTPException(status_code=400, detail="无可用 LLM key，请在密钥面板配置")
    if not is_initialized():
        raise HTTPException(status_code=503, detail="LLM 池未初始化")

    prompt = req.prompt or DEFAULT_SCORE_PROMPT
    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            call_llm_simple,
            req.content, prompt,
            temperature=0.3,
            use_case="agent_chat",
            tier=req.model_tier,
            project="agent",
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        if result is None:
            raise HTTPException(status_code=500, detail="LLM 调用失败（pool 返回 None）")
        return ScoreResponse(success=True, result=result, model=cfg["model"], model_tier=req.model_tier, elapsed_ms=elapsed)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM调用失败: {e}") from None


@router.post("/chat", response_model=ChatResponse, operation_id="agent_chat")
async def agent_chat(req: ChatRequest):
    """通用LLM对话接口"""
    cfg = _resolve_key_for_use("agent_chat", req.model_tier)
    model = req.model or cfg["model"]
    if not cfg["api_key"] or cfg["api_key"] == "sk-your-api-key-here":
        raise HTTPException(status_code=400, detail="无可用 LLM key，请在密钥面板配置")
    if not is_initialized():
        raise HTTPException(status_code=503, detail="LLM 池未初始化")

    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            call_llm,
            req.messages,
            temperature=req.temperature,
            use_case="agent_chat",
            tier=req.model_tier,
            project="agent",
            model=req.model or None,
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=f"LLM调用失败: {result.get('error', '?')}")
        return ChatResponse(
            success=True,
            content=result["content"],
            model=result.get("model", model),
            model_tier=req.model_tier,
            usage=result.get("usage", {}),
            elapsed_ms=elapsed,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM调用失败: {e}") from None


# community/summarize 端点已移除：社群总结改由 workspace/community_review/community_llm.py
# 直接调 /llm/pool/call 完成，不再注册到主后端
