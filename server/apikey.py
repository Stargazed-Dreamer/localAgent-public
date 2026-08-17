"""API Key 测试路由 - 验证Key有效性、查询余额、CRUD管理统一Key库

keys.json 的读写/迁移/路由统一由 server.llm_pool.key_store 处理。
本模块仅保留：VENDOR_REGISTRY（厂商测试配置）、HTTP 路由、_mask_key、_test_key_sync。
"""

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from lib.schema import BaseSchema
from server.llm_pool.key_store import (
    USE_CASE_REGISTRY,
    KeyRecord,
)
from server.llm_pool.key_store import (
    add_key as ks_add_key,
)
from server.llm_pool.key_store import (
    check_health as ks_check_health,
)
from server.llm_pool.key_store import (
    delete_key as ks_delete_key,
)
from server.llm_pool.key_store import (
    get_key_by_id as ks_get_key_by_id,
)
from server.llm_pool.key_store import (
    load_keys as ks_load_keys,
)
from server.llm_pool.key_store import (
    resolve_keys as ks_resolve_keys,
)
from server.llm_pool.key_store import (
    update_key as ks_update_key,
)

logger = logging.getLogger("localagent.apikey")
router = APIRouter(prefix="/apikey", tags=["ApiKey"])

# ========== 厂商注册表 ==========

VENDOR_REGISTRY: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": {
            "method": "GET",
            "path": "/user/balance",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
            "parser": "_parse_deepseek_balance",
        },
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "zhipu": {
        "name": "智谱AI (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas",
        "test": {
            "method": "GET",
            "path": "/v4/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn",
        "test": {
            "method": "GET",
            "path": "/v1/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "qwen": {
        "name": "通义千问 (DashScope)",
        "base_url": "https://dashscope.aliyuncs.com",
        "test": {
            "method": "GET",
            "path": "/compatible-mode/v1/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "siliconflow": {
        "name": "SiliconFlow (硅基流动)",
        "base_url": "https://api.siliconflow.cn",
        "test": {
            "method": "GET",
            "path": "/v1/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": {
            "method": "GET",
            "path": "/v1/user/info",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
            "parser": "_parse_siliconflow_balance",
        },
    },
    "mimo": {
        "name": "小米 MiMo (Token Plan)",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "test": {
            "method": "POST",
            "path": "/chat/completions",
            "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            "body": {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "OK"}], "max_tokens": 5},
        },
        "balance": None,
    },
    "modelscope": {
        "name": "魔搭 ModelScope",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "opencode": {
        "name": "OpenCode Zen (免费)",
        "base_url": "https://opencode.ai/zen/v1",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "agnes": {
        "name": "Agnes AI (免费多模态)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
    "custom": {
        "name": "自定义",
        "base_url": "",
        "test": {
            "method": "GET",
            "path": "/models",
            "headers": lambda key: {"Authorization": f"Bearer {key}"},
        },
        "balance": None,
    },
}


# ========== 余额解析器 ==========

def _parse_deepseek_balance(data: dict) -> dict:
    """解析DeepSeek余额响应"""
    balance_infos = data.get("balance_infos", [])
    result = {}
    for info in balance_infos:
        currency = info.get("currency", "unknown")
        total = info.get("total_balance", "0")
        granted = info.get("granted_balance", "0")
        topped = info.get("topped_up_balance", "0")
        result[currency] = {
            "total_balance": total,
            "granted_balance": granted,
            "topped_up_balance": topped,
        }
    return result


def _parse_siliconflow_balance(data: dict) -> dict:
    """解析SiliconFlow余额响应"""
    return {
        "totalBalance": data.get("totalBalance", "0"),
        "availableBalance": data.get("availableBalance", "0"),
        "frozenBalance": data.get("frozenBalance", "0"),
    }


# ========== Key 脱敏 ==========


def _mask_key(key: str) -> str:
    """脱敏 key：tp-***...***d4"""
    if len(key) <= 8:
        return key[:2] + "***" + key[-1:]
    return key[:3] + "***" + key[-2:]


# v15：base_url 规范化——用户常误填完整端点路径（含 /chat/completions 或 /messages 后缀），
# 但代码会按 protocol 自动拼接后缀，此处去掉误填的后缀避免重复拼接。
_BASE_URL_SUFFIX_STRIP = (
    "/chat/completions",  # openai 协议会自动拼
    "/messages",          # anthropic 协议会自动拼
    "/completions",       # 部分用户可能误填
)


def _normalize_base_url(base_url: str) -> str:
    """规范化 base_url：去除尾部斜杠和误填的端点后缀

    用户在添加 key 时常误把完整请求路径当 base_url（如
    `https://open.bigmodel.cn/api/paas/v4/chat/completions`），但代码会按
    protocol 自动拼接 `/chat/completions` 或 `/messages`，导致 URL 重复。
    此函数去掉误填的后缀，保留到 /vN 级别。
    """
    if not base_url:
        return ""
    url = base_url.strip().rstrip("/")
    lower = url.lower()
    for suffix in _BASE_URL_SUFFIX_STRIP:
        if lower.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    return url


def _test_key_sync(api_key: str, vendor_id: str, base_url: str = "",
                   protocol: str = "openai") -> dict:
    """同步测试 key（供 CRUD 端点内部调用），返回 status dict

    vendor_id 为空时，用 base_url 做 GET /models 兜底测试（OpenAI 兼容 API 通用）。
    v11：增加 protocol 参数，anthropic 协议用专属测试逻辑（POST /v1/messages）。
    T07：同步 requests 改 httpx.Client（连接池复用）；async 端点已用 asyncio.to_thread 包装。
    """
    from lib.async_http import get_sync_client
    # v11：规范化 protocol
    proto = (protocol or "openai").lower().strip()
    if proto not in ("openai", "anthropic"):
        proto = "openai"

    # v11：anthropic 协议单独测试
    if proto == "anthropic":
        return _test_anthropic_key_sync(api_key, base_url)

    if vendor_id and vendor_id not in VENDOR_REGISTRY:
        return {"works": False, "fail_count": 1, "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check_status": "error", "last_check_detail": f"unknown vendor: {vendor_id}"}

    if vendor_id in VENDOR_REGISTRY:
        vendor = VENDOR_REGISTRY[vendor_id]
        url = (base_url or vendor["base_url"]).rstrip("/") + vendor["test"]["path"]
        headers = vendor["test"]["headers"](api_key)
        method = vendor["test"]["method"]
        body = vendor["test"].get("body")
    else:
        # vendor_id 为空：用 base_url + GET /models 兜底（OpenAI 兼容）
        if not base_url:
            return {"works": False, "fail_count": 1, "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_check_status": "error", "last_check_detail": "no vendor_id and no base_url"}
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        method = "GET"
        body = None

    try:
        client = get_sync_client()
        if method == "GET":
            resp = client.get(url, headers=headers, timeout=15)
        else:
            resp = client.post(url, headers=headers, json=body, timeout=15)
        works = resp.status_code in (200, 429)
        detail = "" if works else f"HTTP {resp.status_code}: {resp.text[:200]}"
        return {
            "works": works,
            "fail_count": 0 if works else 1,
            "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_check_status": "ok" if works else "fail",
            "last_check_detail": detail,
        }
    except Exception as e:
        return {
            "works": False,
            "fail_count": 1,
            "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_check_status": "error",
            "last_check_detail": str(e)[:200],
        }


def _test_anthropic_key_sync(api_key: str, base_url: str) -> dict:
    """同步测试 anthropic 协议 key（v11 新增）

    使用 POST /v1/messages 发送最小请求，2xx + 4xx(认证错误) 都表示可连通；
    401/403 → key 无效但 API 可达；200/400 → key 有效；429 → key 有效但限流。
    T07：同步 requests 改 httpx.Client（连接池复用）。
    """
    from lib.async_http import get_sync_client
    if not base_url:
        return {"works": False, "fail_count": 1,
                "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check_status": "error",
                "last_check_detail": "anthropic 协议需要 base_url"}

    # 处理 endpoint：若 base_url 已含 /v1 则只追加 /messages，否则追加 /v1/messages
    url = base_url.rstrip("/")
    url += "/messages" if url.endswith("/v1") else "/v1/messages"

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-3-5-haiku-20241022",  # 用最便宜模型做探活
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "OK"}],
    }

    try:
        resp = get_sync_client().post(url, headers=headers, json=body, timeout=15)
        # 200 / 400（参数错误但能响应，说明 key 有效） / 429（限流）→ key 可连通
        # 401 / 403 → key 无效
        # 404 → endpoint 不对（可能不是 anthropic 协议）
        if resp.status_code in (200, 400, 429):
            return {
                "works": True, "fail_count": 0,
                "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check_status": "ok",
                "last_check_detail": f"HTTP {resp.status_code}" if resp.status_code != 200 else "",
            }
        elif resp.status_code in (401, 403):
            return {
                "works": False, "fail_count": 1,
                "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check_status": "fail",
                "last_check_detail": f"HTTP {resp.status_code}: 认证失败/权限不足",
            }
        else:
            return {
                "works": False, "fail_count": 1,
                "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check_status": "fail",
                "last_check_detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
    except Exception as e:
        return {
            "works": False, "fail_count": 1,
            "last_health_check": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_check_status": "error",
            "last_check_detail": str(e)[:200],
        }



# ========== 请求/响应模型 ==========

class ApiKeyTestRequest(BaseSchema):
    """API密钥测试请求模型，用于封装测试API连接所需的认证与配置信息。

    功能：
    - 作为请求体模型，验证并结构化传递API测试所需的凭证和设置。
    - 支持自定义厂商与端点，兼容多种AI服务提供商的接口规范。

    参数：
    - api_key (str): 必需参数，待测试的API密钥字符串。
    - vendor (str): 必需参数，服务厂商标识，如 "deepseek"、"openai"、"zhipu" 等。
    - base_url (str): 可选参数，默认为空字符串。若提供，则用于覆盖该厂商的默认API基础URL。

    返回值：
    - 本类实例包含结构化且经过验证的测试请求数据，可直接用于后续的API调用测试。
    """
    api_key: str  # 必需的API密钥字符串
    vendor: str  # 厂商标识，如 deepseek / openai / zhipu 等
    base_url: str = ""  # 可选自定义base_url，覆盖默认值


class ApiKeyTestResponse(BaseSchema):
    """
    ApiKeyTestResponse 类用于表示API密钥测试的响应。

    功能：封装API密钥测试的结果信息，常用于存储和验证测试返回的数据。

    参数（类属性）：
    - success: bool，表示测试是否成功执行。
    - vendor: str，表示供应商的标识符。
    - vendor_name: str，表示供应商的显示名称。
    - valid: bool，表示测试的API密钥是否有效。
    - balance: Optional[dict]，可选参数，表示API密钥的余额或使用情况信息，通常为字典格式。
    - models: Optional[list[str]]，可选参数，表示该API密钥支持或可用的模型列表。
    - error: Optional[str]，可选参数，表示测试过程中发生的错误信息，如果无错误则为None。
    - elapsed_ms: int，表示测试过程所耗费的时间，单位为毫秒。

    返回值：无直接返回值，这是一个数据模型类，实例化后对象包含上述所有属性，用于结构化表示响应数据。
    """
    success: bool  # 测试是否成功
    vendor: str  # 供应商标识
    vendor_name: str  # 供应商名称
    valid: bool  # API密钥是否有效
    balance: dict | None = None  # 可选，余额信息，通常为字典
    models: list[str] | None = None  # 可选，支持的模型列表
    error: str | None = None  # 可选，错误信息
    elapsed_ms: int  # 测试耗时，单位为毫秒


class ApiKeyVendorsResponse(BaseSchema):
    vendors: list[dict]  # [{id, name, supports_balance}]


class ApiKeyStatusResponse(BaseSchema):
    """表示API密钥状态响应的类。

    参数：
        supported_vendors (int): 支持的供应商数量。
        last_test (Optional[dict]): 最近一次测试的结果，可选字典。

    返回值：
        无，这是一个数据模型类。
    """
    supported_vendors: int
    last_test: dict | None = None


# ========== 测试历史 ==========

_test_history: list[dict] = []
MAX_HISTORY = 20


def get_status() -> dict:
    """ApiKey 状态概览（供 /health 调用，不暴露 raw 私有 list）"""
    return {
        "supported_vendors": len(VENDOR_REGISTRY),
        "last_test": _test_history[-1] if _test_history else None,
    }


# ========== 路由 ==========

@router.get("/status", response_model=ApiKeyStatusResponse, operation_id="apikey_status")
async def apikey_status():
    """查询ApiKey模块状态"""
    return ApiKeyStatusResponse(
        supported_vendors=len(VENDOR_REGISTRY),
        last_test=_test_history[-1] if _test_history else None,
    )


@router.get("/vendors", response_model=ApiKeyVendorsResponse, operation_id="apikey_vendors")
async def list_vendors():
    """列出所有支持的厂商"""
    vendors = []
    for vid, v in VENDOR_REGISTRY.items():
        vendors.append({
            "id": vid,
            "name": v["name"],
            "supports_balance": v.get("balance") is not None,
        })
    return ApiKeyVendorsResponse(vendors=vendors)


@router.post("/test", response_model=ApiKeyTestResponse, operation_id="apikey_test")
async def test_api_key(req: ApiKeyTestRequest):
    """测试API Key有效性，并尝试查询余额"""
    vendor_id = req.vendor.lower().strip()
    if vendor_id not in VENDOR_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的厂商: {req.vendor}，支持: {', '.join(VENDOR_REGISTRY.keys())}",
        )

    vendor = VENDOR_REGISTRY[vendor_id]
    base_url = req.base_url or vendor["base_url"]
    test_cfg = vendor["test"]
    balance_cfg = vendor.get("balance")

    t0 = time.perf_counter()
    result = ApiKeyTestResponse(
        success=False,
        vendor=vendor_id,
        vendor_name=vendor["name"],
        valid=False,
        elapsed_ms=0,
    )

    # T07：用模块级 httpx.AsyncClient 单例（连接池复用），不每次创建
    from lib.async_http import get_async_client
    client = get_async_client()
    # 1. 测试Key有效性
    try:
        test_url = base_url.rstrip("/") + test_cfg["path"]
        test_headers = test_cfg["headers"](req.api_key)

        if test_cfg["method"] == "GET":
            resp = await client.get(test_url, headers=test_headers, timeout=15.0)
        else:
            post_body = test_cfg.get("body")
            resp = await client.post(test_url, headers=test_headers, json=post_body, timeout=15.0)

        if resp.status_code == 200:
            result.valid = True
            result.success = True
            # 尝试提取模型列表
            try:
                body = resp.json()
                model_list = body.get("data", [])
                if isinstance(model_list, list):
                    result.models = [m.get("id", str(m)) for m in model_list[:50]]
            except Exception:
                pass
        elif resp.status_code == 401:
            result.valid = False
            result.success = True
            result.error = "认证失败：API Key无效或已过期"
        elif resp.status_code == 403:
            result.valid = False
            result.success = True
            result.error = "权限不足：Key无权访问此接口"
        elif resp.status_code == 429:
            # 429说明Key有效，只是限流
            result.valid = True
            result.success = True
            result.error = "Key有效，但触发限流"
        else:
            result.valid = False
            result.success = True
            result.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.ConnectError:
        result.error = "连接失败：无法访问API服务器"
    except httpx.TimeoutException:
        result.error = "请求超时"
    except Exception as e:
        result.error = f"请求异常: {e}"

    # 2. 如果Key有效且有余额查询配置，查询余额
    if result.valid and balance_cfg:
        try:
            bal_url = base_url.rstrip("/") + balance_cfg["path"]
            bal_headers = balance_cfg["headers"](req.api_key)

            if balance_cfg["method"] == "GET":
                bal_resp = await client.get(bal_url, headers=bal_headers, timeout=15.0)
            else:
                bal_resp = await client.post(bal_url, headers=bal_headers, timeout=15.0)

            if bal_resp.status_code == 200:
                bal_data = bal_resp.json()
                parser_name = balance_cfg.get("parser")
                if parser_name:
                    parser_fn = globals().get(parser_name)
                    if parser_fn:
                        result.balance = parser_fn(bal_data)
                    else:
                        result.balance = bal_data
                else:
                    result.balance = bal_data
            else:
                result.balance = {"error": f"余额查询失败: HTTP {bal_resp.status_code}"}
        except Exception as e:
            result.balance = {"error": f"余额查询异常: {e}"}

    result.elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # 记录测试历史
    _test_history.append({
        "vendor": vendor_id,
        "vendor_name": vendor["name"],
        "valid": result.valid,
        "has_balance": result.balance is not None,
        "elapsed_ms": result.elapsed_ms,
        "time": time.strftime("%H:%M:%S"),
    })
    if len(_test_history) > MAX_HISTORY:
        _test_history.pop(0)

    return result


@router.get("/history", operation_id="apikey_history")
async def test_history():
    """获取测试历史"""
    return {"history": _test_history}


# ========== 统一 Key 库 CRUD（委托给 key_store）==========


def _record_to_dict(rec: KeyRecord, mask: bool = True) -> dict:
    """KeyRecord → dict（v11 格式，含 protocol + display_name；前端从 models 聚合 scope）"""
    # v11：model 序列化时保留 display_name（非空才输出）
    models_out = []
    for m in rec.models:
        md = {"name": m["name"], "scope": list(m["scope"]),
              "tier": int(m.get("tier", 3)),
              "enabled": bool(m.get("enabled", True))}
        dn = m.get("display_name")
        if dn and isinstance(dn, str) and dn.strip():
            md["display_name"] = dn.strip()
        models_out.append(md)
    return {
        "id": rec.id,
        "label": rec.label,
        "key": _mask_key(rec.key) if mask else rec.key,
        "base_url": rec.base_url,
        "models": models_out,
        "max_concurrency": rec.max_concurrency,
        "privacy_warning": rec.privacy_warning,
        "group": rec.group,
        "enabled": rec.enabled,
        "allowed_uses": list(rec.allowed_uses),
        "status": dict(rec.status),
        "protocol": rec.protocol,  # v11
        "vision": dict(rec.vision) if rec.vision else {},  # v9
        "pool_key": rec.pool_key or "",  # v12
        "pool": dict(rec.pool) if rec.pool else {},  # v14
    }


class ModelEntrySchema(BaseSchema):
    """v8: 单个 model 条目（name + scope + tier）
    v11: 增加 display_name（展示名，UI 显示用；调用时用 name）
    v14: 删除 limits 字段（死配置）
    """
    name: str
    scope: list[str] = Field(default_factory=lambda: ["llm"])
    tier: int = Field(default=3, ge=1, le=5)  # v8: 1-5
    enabled: bool = True
    display_name: str | None = None  # v11


class KeyStatusModel(BaseSchema):
    works: bool | None = None
    fail_count: int = 0
    last_health_check: str = ""
    last_check_status: str = ""
    last_check_detail: str = ""


class KeyCreateRequest(BaseSchema):
    label: str
    key: str
    base_url: str = ""
    models: list[ModelEntrySchema] = Field(default_factory=list)
    max_concurrency: int = 3
    privacy_warning: str = ""
    group: str = ""
    enabled: bool = True
    allowed_uses: list[str] = Field(default_factory=list)
    protocol: str = "openai"  # v11: "openai" | "anthropic"
    pool: dict = Field(default_factory=dict)  # v14: per-key pool 策略
    vision: dict = Field(default_factory=dict)  # v9: VL provider 参数
    pool_key: str = ""  # v12: 共享上游池标识


class KeyUpdateRequest(BaseSchema):
    label: str | None = None
    key: str | None = None
    base_url: str | None = None
    models: list[ModelEntrySchema] | None = None
    max_concurrency: int | None = None
    privacy_warning: str | None = None
    group: str | None = None
    enabled: bool | None = None
    allowed_uses: list[str] | None = None
    protocol: str | None = None  # v11: "openai" | "anthropic"
    pool: dict | None = None  # v14: per-key pool 策略
    vision: dict | None = None  # v9: VL provider 参数
    pool_key: str | None = None  # v12: 共享上游池标识


@router.get("/use-cases", operation_id="apikey_use_cases")
async def list_use_cases():
    """返回所有 use_case 定义（供 client 拉取填充 UI）

    返回 USE_CASE_REGISTRY 的序列化形式。
    """
    return {"use_cases": [
        {"name": uc.name, "scope": uc.scope, "sensitive": uc.sensitive,
         "default_tier": list(uc.default_tier), "desc": uc.desc}
        for uc in USE_CASE_REGISTRY.values()
    ]}


@router.get("/keys", operation_id="apikey_list_keys")
async def list_keys(unmasked: bool = Query(False)):
    """列出所有已存储的 key（默认脱敏）"""
    records = ks_load_keys()
    return {
        "keys": [_record_to_dict(r, mask=not unmasked) for r in records],
    }


@router.get("/keys/usage", operation_id="apikey_keys_usage")
async def keys_usage():
    """返回 key 用量映射：每个 use_case 用哪些 key

    遍历 USE_CASE_REGISTRY，对每个 use_case 调 resolve_keys() 取匹配的 key_ids。
    用于 UI 显示"谁在用什么 key"。
    """
    records = ks_load_keys()

    # 为每个 key 计算它被哪些 use_case 使用
    key_used_by: dict[str, list[str]] = {r.id: [] for r in records}
    use_case_keys: dict[str, dict] = {}

    for uc_name, uc in USE_CASE_REGISTRY.items():
        resolved = ks_resolve_keys(uc_name)
        matched_ids = [rk.key_id for rk in resolved]
        for kid in matched_ids:
            if kid in key_used_by:
                key_used_by[kid].append(uc_name)
        use_case_keys[uc_name] = {
            "key_ids": matched_ids,
            "key_count": len(matched_ids),
            "scope": uc.scope,
            "sensitive": uc.sensitive,
            "default_tier": list(uc.default_tier),
            "desc": uc.desc,
        }

    keys_summary = [
        {
            "id": r.id,
            "label": r.label,
            "base_url": r.base_url,
            "models": [{"name": m["name"], "scope": list(m["scope"]),
                        "tier": int(m.get("tier", 3)),
                        "enabled": bool(m.get("enabled", True)),
                        "display_name": m.get("display_name") or None}  # v11
                       for m in r.models],
            "enabled": r.enabled,
            "allowed_uses": list(r.allowed_uses),
            "privacy_warning": r.privacy_warning,
            "group": r.group,
            "status_works": r.status.get("works", True),
            "used_by": key_used_by.get(r.id, []),
            "protocol": r.protocol,  # v11
        }
        for r in records
    ]

    return {"use_cases": use_case_keys, "keys": keys_summary}


@router.get("/keys/{key_id}", operation_id="apikey_get_key")
async def get_key(key_id: str, unmasked: bool = Query(False)):
    """获取单个 key 详情"""
    rec = ks_get_key_by_id(key_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
    return _record_to_dict(rec, mask=not unmasked)


@router.post("/keys", operation_id="apikey_create_key")
async def create_key(req: KeyCreateRequest):
    """添加新 key，添加后自动触发测试"""
    # v15：规范化 base_url（去掉误填的 /chat/completions 或 /messages 后缀）
    base_url = _normalize_base_url(req.base_url)
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    # v11：根据 protocol 选择测试方式（anthropic 协议用专属测试）
    # T07：async 端点中的同步 HTTP 调用包入 asyncio.to_thread，避免阻塞事件循环
    status = await asyncio.to_thread(
        _test_key_sync, req.key, "", base_url, protocol=req.protocol
    )
    # v11：规范化 protocol（默认 openai）
    protocol = (req.protocol or "openai").lower().strip()
    if protocol not in ("openai", "anthropic"):
        protocol = "openai"
    rec = KeyRecord(
        id="",
        label=req.label,
        key=req.key,
        base_url=base_url,
        models=[m.model_dump() for m in req.models],
        max_concurrency=req.max_concurrency,
        privacy_warning=req.privacy_warning,
        group=req.group,
        enabled=req.enabled,
        allowed_uses=list(req.allowed_uses),
        status=status,
        protocol=protocol,  # v11
    )
    new_id = ks_add_key(rec)
    rec.id = new_id
    return _record_to_dict(rec, mask=True)


@router.put("/keys/{key_id}", operation_id="apikey_update_key")
async def update_key(key_id: str, req: KeyUpdateRequest):
    """更新 key（部分字段更新）"""
    updates = {}
    for f in ["label", "key", "base_url",
              "max_concurrency", "privacy_warning", "group", "enabled", "allowed_uses",
              "protocol",  # v11
              "pool_key"]:  # v12
        val = getattr(req, f, None)
        if val is not None:
            if f == "protocol":
                # v11：规范化 protocol
                proto = str(val).lower().strip()
                if proto in ("openai", "anthropic"):
                    updates[f] = proto
            elif f == "base_url":
                # v15：规范化 base_url（去掉误填的 /chat/completions 或 /messages 后缀）
                updates[f] = _normalize_base_url(str(val))
            else:
                updates[f] = val
    if req.models is not None:
        updates["models"] = [m.model_dump() for m in req.models]
    # v14: pool 策略（dict，空字典 = 用全局默认）
    if req.pool is not None:
        updates["pool"] = dict(req.pool)
    # v9: vision 参数（dict）
    if req.vision is not None:
        updates["vision"] = dict(req.vision)
    rec = ks_update_key(key_id, updates)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
    return _record_to_dict(rec, mask=True)


@router.delete("/keys/{key_id}", operation_id="apikey_delete_key")
async def delete_key(key_id: str):
    """删除 key"""
    ok = ks_delete_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
    return {"deleted": key_id}


@router.post("/keys/{key_id}/test", operation_id="apikey_test_key")
async def test_stored_key(key_id: str):
    """测试单个已存储的 key，更新 status"""
    rec = ks_get_key_by_id(key_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id}")
    # v11：传递 protocol 给 _test_key_sync
    # T07：async 端点中的同步 HTTP 调用包入 asyncio.to_thread，避免阻塞事件循环
    status = await asyncio.to_thread(
        _test_key_sync, rec.key, "", rec.base_url, protocol=rec.protocol
    )
    ks_update_key(key_id, {"status": status})
    return {"id": key_id, "status": status}


@router.post("/keys/health-check-all", operation_id="apikey_health_check_all")
async def health_check_all():
    """批量健康检查所有 key（并发，按物理 key 去重，立即返回结果）

    委托给 key_store.check_health()，该函数按 (key, base_url) 去重，
    每物理 key 只测一次，结果同步应用到所有同物理 key 的 record。
    """
    # T07：async 端点中的同步 HTTP 调用（内部 ThreadPoolExecutor + requests）
    # 包入 asyncio.to_thread，避免阻塞事件循环
    result = await asyncio.to_thread(ks_check_health)
    return result
