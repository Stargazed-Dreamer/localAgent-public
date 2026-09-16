"""统一 Key 管理：路由 + 读写 + 健康检查 + v4→v5→v6→v7→v8→v9 迁移

合并原 apikey.py 的 keys.json 读写 + key_manager.py 的 key 解析/健康检查。
所有 key 消费方（路径 A: get_key_for_use / 路径 B: LLMPool / 路径 C: VL）统一通过
resolve_keys() 路由层匹配 key。

v8 schema：model 级 tier (1-5) + scope
  KeyRecord.models: list[dict]，每个 model 含 name + scope + tier
  tier 值：1(最轻量) ~ 5(最强大)，按 benchmark 排行分配
  路由层按 tier 硬匹配 + model 软偏好（同 tier 内 model 不可用时自动 fallback）
  v14: limits 字段已删除（死配置，pool 用 429 冷却机制兜底）
  scope 值：llm | vl | aigc_image | aigc_video

v9 schema：VL provider 参数迁移到 key 级 vision 段（统一 key 来源）
  KeyRecord.vision: dict，VL provider 级参数（max_concurrency/timeout/rate_limit_cooldown）
  仅当 key 含 vl scope model 时有效；缺失时使用默认值 (1/60/60)
  ResolvedKey.vision_params: dict，路由结果中携带 vision 段（供 RemoteVLClient 直接使用）
  迁移源：config.toml [vision.vl_providers.<name>] 段 → keys.json key.vision 段（按 base_url 匹配）
  v9 后 config.toml 不再存放 VL provider 配置，keys.json 是唯一 key+provider 来源

多对多映射：
  Key 维度：多物理 key / 单 key 多 model / 每 model 独立 tier+scope / 安全等级 (privacy_warning)
  Use case 维度：14 个内置 use_case（8 LLM + 3 VL + 3 AIGC）+ 组件 manifest 动态注册，
  每个声明 scope/sensitive/default_tier(范围 [min,max])
  路由层：resolve_keys(use_case, tier) 做多对多匹配，tier 硬过滤，model 软偏好
"""

import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# v12: UseCaseDef / USE_CASE_REGISTRY / TIER_NAMES / _LEGACY_TIER_MAP / SENSITIVE_USES
# 迁入 types.py 以打破 types ↔ key_store 循环依赖；此处反向导入保持外部 import 兼容。
# 每行末尾的 # noqa: F401 标记这些符号为 backward-compat re-export，ruff 不报 unused。
from server.llm_pool.types import (  # noqa: F401
    _LEGACY_TIER_MAP,
    SENSITIVE_USES,
    TIER_NAMES,
    USE_CASE_REGISTRY,
    ProviderPolicy,
    UseCaseDef,
)

logger = logging.getLogger("localagent.key_store")

# keys.json 当前 schema 版本（迁移链末态）。CRUD 写 wrapper 时必须引用本常量，
# 禁止写版本字面量——否则每次写盘后 _load_keys_cached 都会重跑迁移链并重建 .json.v8backup（5-11）
SCHEMA_VERSION = 14

# =====================================================================
# v9 默认值：VL provider 级参数（key.vision 段缺失时使用）
# 与原 config.example.toml [vision.vl_providers.*] 默认值一致
# =====================================================================
DEFAULT_VL_MAX_CONCURRENCY = 1
DEFAULT_VL_TIMEOUT = 60
DEFAULT_VL_RATE_LIMIT_COOLDOWN = 60.0


def _normalize_vision_params(vision: dict | None) -> dict:
    """规范化 vision 段：补全缺失字段，类型转换。

    返回: {"max_concurrency": int, "timeout": int, "rate_limit_cooldown": float}
    """
    if not isinstance(vision, dict):
        vision = {}
    return {
        "max_concurrency": int(vision.get("max_concurrency", DEFAULT_VL_MAX_CONCURRENCY)),
        "timeout": int(vision.get("timeout", DEFAULT_VL_TIMEOUT)),
        "rate_limit_cooldown": float(vision.get("rate_limit_cooldown", DEFAULT_VL_RATE_LIMIT_COOLDOWN)),
    }


# =====================================================================
# 数据结构
# =====================================================================


def _normalize_tier(tier) -> int:
    """将 tier 参数标准化为 int (1-5)。

    - int: 直接返回（截断到 1-5）
    - str: 旧名称 ("default"/"cheap"/"powerful") → 数字
    - None / 0: 返回 0（表示不指定，由 use_case.default_tier 决定）

    0 必须保持 0：它是"未指定"哨兵。若 clamp 到 1，_normalize_tier_range((0,0))
    会被误归一成 (1,1)，无 tier 的 call()/stream() 调用将被硬过滤到仅 tier-1 key。
    """
    if tier is None:
        return 0
    if isinstance(tier, str):
        s = tier.strip().lower()
        if s in _LEGACY_TIER_MAP:
            return _LEGACY_TIER_MAP[s]
        try:
            v = int(s)
        except ValueError:
            return 0
        return max(1, min(5, v)) if v else 0
    try:
        v = int(tier)
    except (TypeError, ValueError):
        return 0
    return max(1, min(5, v)) if v else 0


def _normalize_tier_range(tier) -> tuple[int, int]:
    """将 tier 参数标准化为 (min, max) 范围。

    - None: (0, 0)（不指定，由 use_case.default_tier 决定）
    - int: (tier, tier)（精确匹配）
    - str "cheap"/"default"/"powerful": (2,2)/(3,3)/(5,5)
    - str "3": (3, 3)
    - tuple/list [min, max]: (min, max)
    - tuple/list [t]: (t, t)
    """
    if tier is None:
        return (0, 0)
    if isinstance(tier, (tuple, list)):
        vals = [_normalize_tier(t) for t in tier]
        vals = [v for v in vals if v > 0]
        if not vals:
            return (0, 0)
        return (min(vals), max(vals))
    t = _normalize_tier(tier)
    return (t, t) if t > 0 else (0, 0)


@dataclass
class KeyRecord:
    """keys.json v14 schema：v12 + key 级 pool 段（per-key 重试/限流策略）

    v14 新增字段：
    - pool: dict per-key 重试/限流策略（ProviderPolicy 参数）
      包含 retry_count/retry_base_delay/retry_max_delay/retry_jitter/
      rate_limit_cooldown_seconds/rate_limit_backoff_multiplier/rate_limit_max_cooldown
      空字典 = 使用 config.toml [llm.pool] 全局默认

    v12 新增字段：
    - pool_key: str 共享上游池标识（OmniRoute pool-deduped 借鉴）
      同一 pool_key 的多个 key 共享上游配额池，get_status 计算总容量时按 pool_key 取 max
      空字符串 = 独立池（按 key 个体计数）

    v11 新增字段：
    - protocol: str 连接协议（"openai" 默认 | "anthropic"）
      决定 call() 时构造 OpenAI 兼容请求还是 Anthropic /v1/messages 请求
    - models[].display_name: str 展示名（用于 UI 显示，缺失则用 name）

    v9 schema：v8 + key 级 vision 段（VL provider 参数）"""
    id: str
    label: str
    key: str
    base_url: str
    models: list[dict] = field(default_factory=list)  # name/scope/tier/enabled/display_name
    max_concurrency: int = 3
    privacy_warning: str = ""
    group: str = ""
    enabled: bool = True
    allowed_uses: list[str] = field(default_factory=list)
    status: dict = field(default_factory=lambda: {
        "works": True, "fail_count": 0,
        "last_health_check": "", "last_check_status": "", "last_check_detail": ""
    })
    # v14: limits 字段已删除（死配置，pool 用 429 冷却机制兜底，不读 limits 做限流）
    # v9 新增：VL provider 级参数。仅当 key 含 vl scope model 时有效。
    # 缺失时 _to_record 会填充默认值 {max_concurrency=1, timeout=60, rate_limit_cooldown=60.0}
    vision: dict = field(default_factory=dict)
    # v11 新增：连接协议（默认 "openai"，支持 "anthropic"）
    protocol: str = "openai"
    # v12 新增：共享上游池标识（默认空字符串 = 独立池）
    pool_key: str = ""
    # v14 新增：per-key 重试/限流策略（空字典 = 用全局默认 [llm.pool]）
    pool: dict = field(default_factory=dict)


def _key_scope(rec: KeyRecord) -> list[str]:
    """从 rec.models 聚合 scope（供前端展示/筛选用）"""
    scopes: list[str] = []
    for m in rec.models:
        if not m.get("enabled", True):
            continue
        for s in m.get("scope", ["llm"]):
            if s not in scopes:
                scopes.append(s)
    return scopes or ["llm"]


@dataclass
class ResolvedKey:
    """路由结果：一个 key + 选定的 model（v9：含 vision_params）"""
    key_id: str
    api_key: str
    base_url: str
    model: str           # 调用方应使用的 model（tier 匹配的或候选 model[0]）
    models: list[str]    # 该 key 支持的所有 model 名（不含 scope，向后兼容 LLMKey 消费）
    label: str
    privacy_warning: str
    max_concurrency: int
    source: str = "unified_json"
    # v9 新增：VL provider 级参数（仅 vl scope key 有意义，LLM/AIGC key 为默认值）
    # RemoteVLClient 直接使用此字段构造运行时 provider，不再独立读 keys.json
    vision_params: dict = field(default_factory=dict)


# =====================================================================
# keys.json 读写（单一锁保护 + TTL 缓存）
# =====================================================================

# 模块级 fallback：config 读取失败时使用。基于 __file__ 的绝对路径，不依赖 cwd。
# key_store.py 在 server/llm_pool/ 下，parent×3 才是项目根 localAgent/
_UNIFIED_KEYS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "llm" / "keys.json"
_keys_file_lock = threading.RLock()
_cache: list[KeyRecord] | None = None
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS = 5.0  # 5 秒内重复读取用缓存


def _resolve_against_project_root(p: str | Path) -> Path:
    """相对路径相对于项目根目录解析；绝对路径原样返回。"""
    p = Path(p)
    if p.is_absolute():
        return p
    # key_store.py 在 server/llm_pool/ 下，parent×3 才是项目根 localAgent/
    return Path(__file__).resolve().parent.parent.parent / p


def _get_unified_keys_path() -> Path:
    """从 config.toml 读取统一 key 库路径，默认 data/llm/keys.json

    委托给 lib/secret.get_llm_keys_path() 作为单一真源，
    避免路径解析逻辑分散在多处。
    """
    try:
        from lib.secret import get_llm_keys_path
        return get_llm_keys_path()
    except Exception:
        # lib/secret 不可用时回退到原逻辑（保持向后兼容）
        try:
            from server.config import get_llm_storage_config
            storage = get_llm_storage_config()
            path_str = storage.get("unified_keys_file", "data/llm/keys.json")
            return _resolve_against_project_root(path_str)
        except Exception:
            return _UNIFIED_KEYS_FILE


def _to_record(item: dict) -> KeyRecord:
    """dict → KeyRecord（v14 格式；旧版数据由 migrate 链负责转换）"""
    models_raw = item.get("models", [])
    # v8：保留 name + scope + tier；v11：保留 display_name；v14：删除 limits
    models = []
    for m in models_raw:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        model_dict = {
            "name": m["name"],
            "scope": list(m.get("scope", ["llm"])),
            "tier": _normalize_tier(m.get("tier")) or 3,  # 缺失默认 tier 3（中等）
            "enabled": bool(m.get("enabled", True)),
        }
        # v11：保留 display_name（缺失时不写入，get_display_name 自动 fallback 到 name）
        display_name = m.get("display_name")
        if display_name and isinstance(display_name, str) and display_name.strip():
            model_dict["display_name"] = display_name.strip()
        models.append(model_dict)

    status = item.get("status", {})
    if not isinstance(status, dict):
        status = {}

    # v9：vision 段规范化（缺失字段补默认值）
    vision = _normalize_vision_params(item.get("vision"))

    # v11：protocol 规范化（默认 openai）
    protocol = str(item.get("protocol", "openai") or "openai").lower().strip()
    if protocol not in ("openai", "anthropic"):
        protocol = "openai"

    # v12：pool_key 规范化（默认空字符串 = 独立池）
    pool_key = str(item.get("pool_key", "") or "").strip()

    # v14：pool 段规范化（per-key 重试/限流策略）
    pool = item.get("pool", {})
    if not isinstance(pool, dict):
        pool = {}

    return KeyRecord(
        id=item.get("id", ""),
        label=item.get("label", ""),
        key=item.get("key", ""),
        base_url=item.get("base_url", ""),
        models=models,
        max_concurrency=int(item.get("max_concurrency", 3)),
        privacy_warning=item.get("privacy_warning", ""),
        group=item.get("group", ""),
        enabled=bool(item.get("enabled", True)),
        allowed_uses=list(item.get("allowed_uses", [])),
        status=status,
        vision=vision,
        protocol=protocol,
        pool_key=pool_key,
        pool=pool,
    )


def _to_dict(rec: KeyRecord) -> dict:
    """KeyRecord → dict（v14 格式，含 tier + vision + protocol + display_name；limits 已删除）"""
    models_out = []
    for m in rec.models:
        m_dict = {
            "name": m["name"],
            "scope": list(m["scope"]),
            "tier": int(m.get("tier", 3)),
            "enabled": bool(m.get("enabled", True)),
        }
        # v11：只保留非空 display_name（避免空值污染）
        dn = m.get("display_name")
        if dn and isinstance(dn, str) and dn.strip():
            m_dict["display_name"] = dn.strip()
        models_out.append(m_dict)
    return {
        "id": rec.id,
        "label": rec.label,
        "key": rec.key,
        "base_url": rec.base_url,
        "models": models_out,
        "max_concurrency": rec.max_concurrency,
        "privacy_warning": rec.privacy_warning,
        "group": rec.group,
        "enabled": rec.enabled,
        "allowed_uses": list(rec.allowed_uses),
        "status": dict(rec.status),
        "vision": _normalize_vision_params(rec.vision),
        "protocol": rec.protocol,
        # v12：pool_key 仅在非空时写入（避免空字符串污染 schema）
        **({"pool_key": rec.pool_key} if rec.pool_key else {}),
        # v14：pool 段仅在非空时写入（空 = 用全局默认 [llm.pool]）
        **({"pool": dict(rec.pool)} if rec.pool else {}),
    }


def _read_raw_keys_file() -> tuple[list[dict], dict | None]:
    """读取 keys.json 原始数据，返回 (records, wrapper_or_None)

    wrapper 非空表示字典包裹格式 {"version":5, "updated_at":"...", "keys":[...]}
    """
    path = _get_unified_keys_path()
    if not path.exists():
        return [], None
    try:
        text = path.read_text(encoding="utf-8-sig").lstrip("\ufeff")
        data = json.loads(text)
    except Exception as e:
        logger.warning("Failed to read keys.json: %s", e)
        return [], None

    if isinstance(data, dict) and "keys" in data and isinstance(data["keys"], list):
        return data["keys"], data
    if isinstance(data, list):
        return data, None
    logger.warning("Unrecognized keys.json format")
    return [], None


def _write_raw_keys_file(records: list[dict], wrapper: dict | None = None) -> None:
    """写回 keys.json，保持原格式"""
    path = _get_unified_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if wrapper is not None:
        out = dict(wrapper)
        out["keys"] = records
        out["updated_at"] = date.today().isoformat()
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _keys_file_mtime() -> float | None:
    """keys.json 当前 mtime（文件不存在返回 None）

    check_health 用于「写入前比对 mtime」检测健康检查期间的并发修改。
    """
    try:
        return _get_unified_keys_path().stat().st_mtime
    except OSError:
        return None


# =====================================================================
# v4 → v5 迁移
# =====================================================================


def migrate_v4_to_v5(data: dict) -> dict:
    """v4 (model: str) → v5 (models: list)

    按 (key, base_url) 分组合并同物理 key 的多条 record：
      1. 收集所有同物理 key 的 record
      2. models = [r["model"] for r in group]（去重保序）
      3. allowed_uses：若任一 record 含 "llm_pool" 或空 → 设为 []；否则取并集移除 "llm_pool"
      4. 其他字段取第一条 record 的值
      5. 删除 vendor/model 字段，保留 group
    """
    old_keys = data.get("keys", [])
    if not isinstance(old_keys, list):
        return data

    # 按 (key, base_url) 分组
    groups: dict[tuple, list[dict]] = defaultdict(list)
    order: list[tuple] = []
    for rec in old_keys:
        if not isinstance(rec, dict):
            continue
        dedup_key = (str(rec.get("key", "")), str(rec.get("base_url", "")).rstrip("/"))
        if dedup_key not in groups:
            order.append(dedup_key)
        groups[dedup_key].append(rec)

    new_keys: list[dict] = []
    for dedup_key in order:
        group = groups[dedup_key]
        first = dict(group[0])  # 基础字段

        # 合并 models：去重保序
        models: list[str] = []
        seen_models: set[str] = set()
        for r in group:
            m = r.get("model", "")
            if m and m not in seen_models:
                models.append(m)
                seen_models.add(m)

        # 合并 allowed_uses
        all_uses: list[str] = []
        any_unrestricted = False
        for r in group:
            uses = r.get("allowed_uses", [])
            if not uses:
                any_unrestricted = True
                break
            for u in uses:
                if u not in all_uses and u != "llm_pool":
                    all_uses.append(u)
        final_uses = [] if any_unrestricted else all_uses

        # 合并 scope（取并集）
        scope_set: list[str] = []
        for r in group:
            for s in r.get("scope", ["llm"]):
                if s not in scope_set:
                    scope_set.append(s)

        # 合并 status（取最老的 last_health_check，works 任一为 false 则 false）
        merged_status = dict(first.get("status", {}))
        for r in group[1:]:
            r_status = r.get("status", {})
            if r_status.get("works") is False:
                merged_status["works"] = False
            r_lhc = r_status.get("last_health_check", "")
            if r_lhc and (not merged_status.get("last_health_check")
                          or r_lhc < merged_status.get("last_health_check", "")):
                merged_status["last_health_check"] = r_lhc

        # 构造 v5 record
        new_rec = {
            "id": first.get("id", ""),
            "label": first.get("label", ""),
            "key": first.get("key", ""),
            "base_url": first.get("base_url", ""),
            "models": models,
            "scope": scope_set,
            "max_concurrency": int(first.get("max_concurrency", 3)),
            "privacy_warning": first.get("privacy_warning", ""),
            "group": first.get("group", ""),
            "enabled": bool(first.get("enabled", True)),
            "allowed_uses": final_uses,
            "status": merged_status,
        }
        new_keys.append(new_rec)

    data["keys"] = new_keys
    data["version"] = 5
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v5_to_v6(data: dict) -> dict:
    """v5 (models: list[str], scope: list[str]) → v6 (models: list[dict])

    幂等检测：若 models[0] 已是 dict，跳过（避免重复迁移破坏数据）。
    这不是 v5 兼容读，而是迁移函数的幂等性保证。
    """
    for rec in data.get("keys", []):
        old_models = rec.get("models", [])
        if not old_models:
            rec.pop("scope", None)
            continue
        if isinstance(old_models[0], dict):
            rec.pop("scope", None)
            continue
        old_scope = list(rec.get("scope", ["llm"]))
        rec["models"] = [{"name": m, "scope": list(old_scope)} for m in old_models]
        rec.pop("scope", None)
    data["version"] = 6
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v6_to_v7(data: dict) -> dict:
    """v6 (models 无 limits) → v7 (models + key 级均含 limits: dict)

    幂等检测：若 model 已含 limits key 则跳过该 model；若 record 已含 limits key 则跳过 key 级。
    """
    for rec in data.get("keys", []):
        # key 级 limits
        if "limits" not in rec:
            rec["limits"] = {}
        # model 级 limits
        models = rec.get("models", [])
        if isinstance(models, list):
            for m in models:
                if isinstance(m, dict) and "limits" not in m:
                    m["limits"] = {}
    data["version"] = 7
    data["updated_at"] = date.today().isoformat()
    return data


# v7 → v8 迁移：默认 tier 推断表（按 model 名关键字匹配）
# 未匹配的 model 默认 tier 3（中等）
_V8_TIER_INFERENCE: list[tuple[str, int]] = [
    # Tier 5（最强）
    ("glm-5", 5), ("glm5", 5),
    ("deepseek-v4-pro", 5), ("deepseek-v3-pro", 5),
    ("qwen3-vl-235b", 5), ("qwen-vl-max", 5),
    ("gpt-4o", 5), ("gpt-4-turbo", 5), ("claude-3.5-sonnet", 5),
    # Tier 4（强大）
    ("nemotron-3-ultra", 4), ("nemotron-ultra", 4),
    ("minimax-m3", 4), ("minimax-text", 4),
    ("claude-3-haiku", 4), ("gpt-4", 4),
    # Tier 2（轻量）
    ("mimo-v2.5", 2), ("big-pickle", 2), ("north-mini-code", 2),
    ("gpt-3.5", 2), ("qwen-turbo", 2),
]


def _infer_tier_for_model(model_name: str) -> int:
    """根据 model 名推断 tier（v7→v8 迁移用）"""
    name_lower = model_name.lower()
    for keyword, tier in _V8_TIER_INFERENCE:
        if keyword in name_lower:
            return tier
    return 3  # 默认中等


def migrate_v7_to_v8(data: dict) -> dict:
    """v7 (models 无 tier) → v8 (models 含 tier: int 1-5)

    幂等检测：若 model 已含 tier 字段则跳过。
    缺失 tier 的 model 按名称推断（_infer_tier_for_model），推断失败默认 tier 3。
    """
    for rec in data.get("keys", []):
        models = rec.get("models", [])
        if isinstance(models, list):
            for m in models:
                if isinstance(m, dict) and "tier" not in m:
                    m["tier"] = _infer_tier_for_model(m.get("name", ""))
    data["version"] = 8
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v8_to_v9(data: dict, config_vl_providers: dict | None = None) -> dict:
    """v8 (无 vision 段) → v9 (key 级 vision 段)

    v9 将 VL provider 级参数从 config.toml [vision.vl_providers.<name>] 迁移到
    keys.json 的 key.vision 段，按 base_url 匹配。

    幂等检测：若 key 已含 vision 段（非空 dict 且含 max_concurrency 键）则跳过该 key。

    Args:
        data: keys.json 解析后的 dict
        config_vl_providers: 可选，从 config.toml 读取的 {name: {base_url, max_concurrency,
            timeout, rate_limit_cooldown, ...}} 映射。若为 None 则只补默认值 vision 段
            （不从 config.toml 迁移实际值，用于 _maybe_migrate 自动迁移场景）。
            手动迁移应通过 tools/migrate_keys_v8_to_v9.py 传入实际 config_vl_providers。
    """
    for rec in data.get("keys", []):
        # 幂等：已有 vision 段则跳过
        existing_vision = rec.get("vision")
        if isinstance(existing_vision, dict) and "max_concurrency" in existing_vision:
            continue

        # 只为含 vl scope model 的 key 添加 vision 段
        has_vl_model = False
        for m in rec.get("models", []) or []:
            if isinstance(m, dict) and "vl" in m.get("scope", []):
                has_vl_model = True
                break
        if not has_vl_model:
            continue

        # 从 config_vl_providers 按 base_url 匹配，找不到则用默认值
        base_url = rec.get("base_url", "").rstrip("/")
        vision = {
            "max_concurrency": DEFAULT_VL_MAX_CONCURRENCY,
            "timeout": DEFAULT_VL_TIMEOUT,
            "rate_limit_cooldown": DEFAULT_VL_RATE_LIMIT_COOLDOWN,
        }
        if config_vl_providers:
            for pcfg in config_vl_providers.values():
                if not isinstance(pcfg, dict):
                    continue
                if pcfg.get("base_url", "").rstrip("/") == base_url:
                    vision["max_concurrency"] = int(pcfg.get("max_concurrency", vision["max_concurrency"]))
                    vision["timeout"] = int(pcfg.get("timeout", vision["timeout"]))
                    vision["rate_limit_cooldown"] = float(pcfg.get("rate_limit_cooldown", vision["rate_limit_cooldown"]))
                    break
        rec["vision"] = vision

    data["version"] = 9
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v9_to_v10(data: dict) -> dict:
    """v9 (无 model_stats) → v10 (per-model stats + is_free 推断标记)

    v10 在运行时（LLMKey.__post_init__）自动推断 is_free，无需 keys.json 写入字段。
    此迁移函数仅作为版本号升级的占位，幂等无操作。

    v10 实际新增的字段（model_stats/is_free）都在 LLMKey 运行时构造，
    keys.json 层面 v9 和 v10 schema 完全相同。
    """
    data["version"] = 10
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v10_to_v11(data: dict) -> dict:
    """v10 → v11 (key 级 protocol + model 级 display_name)

    v11 新增字段：
    - key.protocol: 连接协议（"openai" 默认 | "anthropic"）
    - models[].display_name: 展示名（用于 UI 显示，缺失则用 name）

    幂等检测：
    - key 已含 protocol 字段则跳过该 key
    - model 已含 display_name 字段则跳过该 model
    """
    for rec in data.get("keys", []):
        # key 级 protocol
        if "protocol" not in rec:
            rec["protocol"] = "openai"
        else:
            # 规范化已有值
            proto = str(rec.get("protocol") or "openai").lower().strip()
            rec["protocol"] = proto if proto in ("openai", "anthropic") else "openai"

        # model 级 display_name：v11 不强制写入（缺失时 get_display_name 自动 fallback 到 name）
        # 这里不做主动写入，保持 keys.json 简洁；只在用户显式设置时才保留
        models = rec.get("models", [])
        if isinstance(models, list):
            for m in models:
                if isinstance(m, dict):
                    # 清理空 display_name（避免空字符串污染）
                    dn = m.get("display_name")
                    if dn is not None and (not isinstance(dn, str) or not dn.strip()):
                        m.pop("display_name", None)

    data["version"] = 11
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v11_to_v12(data: dict) -> dict:
    """v11 → v12 (key 级 pool_key：共享上游池去重)

    v12 新增字段：
    - key.pool_key: str 共享上游池标识（OmniRoute pool-deduped 借鉴）
      同一 pool_key 的多个 key 共享上游配额池，get_status 计算总容量时按 pool_key 取 max
      空字符串 = 独立池（按 key 个体计数）

    幂等检测：key 已含 pool_key 字段则跳过该 key。
    迁移策略：不主动写入空 pool_key（保持 keys.json 简洁），仅升级 version 号。
    用户需通过 /keys API 显式设置 pool_key 才会写入文件。
    """
    # 仅升级 version 号；pool_key 字段由用户按需设置（_to_dict 仅在非空时写入）
    data["version"] = 12
    data["updated_at"] = date.today().isoformat()
    return data


def migrate_v12_to_v14(data: dict) -> dict:
    """v12/v13 → v14 (key 级 pool 段 + 删除 limits 死字段)

    v14 变更：
    - 新增 pool: dict per-key 重试/限流策略（空 = 用 [llm.pool] 全局默认）
    - 删除 limits: key 级和 model 级 limits 字段（死配置，pool 用 429 冷却兜底）

    幂等检测：pool 段不存在时无需补默认值（_to_dict 仅在非空时写入）。
    limits 字段直接 pop（即使不存在也不报错）。

    注：v12 和 v13 在 schema 上等价（v13 未有独立迁移函数），统一迁移到 v14。
    """
    # 删除 limits 死字段（key 级和 model 级）
    for rec in data.get("keys", []):
        rec.pop("limits", None)
        models = rec.get("models", [])
        if isinstance(models, list):
            for m in models:
                if isinstance(m, dict):
                    m.pop("limits", None)
    data["version"] = 14
    data["updated_at"] = date.today().isoformat()
    return data


def _maybe_migrate(data: dict) -> dict:
    """检测并执行 v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13 → v14 迁移（在 _load_keys_cached 中调用）

    v8 → v9 自动迁移只补默认值 vision 段（不从 config.toml 读取实际值）。
    如需从 config.toml 迁移实际 provider 参数，运行 tools/migrate_keys_v8_to_v9.py。
    v9 → v10 → v11 → v12 → v14 迁移在 keys.json 层面只补 protocol 默认值
    （display_name / pool_key / pool 由用户按需设置，不主动写入）。
    """
    version = data.get("version", 2) if isinstance(data, dict) else 2
    if version >= 14:
        return data
    # 备份原文件（首次迁移时创建）
    path = _get_unified_keys_path()
    if path.exists():
        try:
            backup = path.with_suffix(".json.v8backup")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
                logger.info("Created v8 backup: %s", backup)
        except Exception as e:
            logger.warning("Failed to create v8 backup: %s", e)
    # v3/v4 → v5
    if version < 5:
        data = migrate_v4_to_v5(data)
    # v5 → v6
    if version < 6:
        data = migrate_v5_to_v6(data)
    # v6 → v7
    if version < 7:
        data = migrate_v6_to_v7(data)
    # v7 → v8
    if version < 8:
        data = migrate_v7_to_v8(data)
    # v8 → v9（自动迁移：只补默认值，不读 config.toml）
    if version < 9:
        data = migrate_v8_to_v9(data, config_vl_providers=None)
    # v9 → v10（运行时字段，keys.json 无变化，仅升级版本号）
    if version < 10:
        data = migrate_v9_to_v10(data)
    # v10 → v11（补 protocol 默认值；display_name 由用户设置，不主动写入）
    if version < 11:
        data = migrate_v10_to_v11(data)
    # v11 → v12（仅升级版本号；pool_key 由用户设置，不主动写入）
    if version < 12:
        data = migrate_v11_to_v12(data)
    # v12/v13 → v14（仅升级版本号；pool 段由用户设置，不主动写入）
    if version < 14:
        data = migrate_v12_to_v14(data)
    # 立即写回迁移后的数据
    if isinstance(data, dict) and "keys" in data:
        _write_raw_keys_file(data["keys"], data)
        logger.info("Migrated keys.json → v14: %d records", len(data["keys"]))
    return data


# =====================================================================
# 缓存 + 加载
# =====================================================================


def _invalidate_cache() -> None:
    """清空缓存（写操作后调用）"""
    global _cache, _cache_loaded_at
    _cache = None
    _cache_loaded_at = 0.0


def _load_keys_cached(force: bool = False) -> list[KeyRecord]:
    """加载 keys.json，带 TTL 缓存"""
    global _cache, _cache_loaded_at
    with _keys_file_lock:
        if not force and _cache is not None and (time.time() - _cache_loaded_at) < _CACHE_TTL_SECONDS:
            return _cache

        records_raw, wrapper = _read_raw_keys_file()
        if wrapper is not None:
            wrapper = _maybe_migrate(wrapper)
            records_raw = wrapper.get("keys", [])
        elif records_raw:
            # 扁平数组格式（旧 v3 mimo_keys_tested.json），不迁移直接读
            pass

        records = [_to_record(item) for item in records_raw if isinstance(item, dict)]
        _cache = records
        _cache_loaded_at = time.time()
        return records


# =====================================================================
# 路由层：resolve_keys() / resolve_key()
# =====================================================================


def _pick_model_for_tier(rec: KeyRecord, tier, required_scope: str,
                         preferred_model: str | None = None) -> str:
    """从 record 的 models 中选 model（tier 范围匹配 + model 软偏好）：

    tier 支持范围：
    - int: 精确匹配 (tier, tier)
    - tuple/list [min, max]: 范围匹配
    - None: 不过滤 tier

    1. 按 required_scope + tier 范围过滤候选 model
    2. 候选 model 按 tier 升序排列（优先更低 tier 省 cost）
    3. preferred_model 在候选中 → 优先返回（软偏好）
    4. 否则返回候选第一个（最低 tier）
    """
    min_t, max_t = _normalize_tier_range(tier) if tier is not None else (0, 0)
    candidates = [m for m in rec.models
                  if m.get("enabled", True)
                  and required_scope in m.get("scope", ["llm"])]
    if max_t > 0:
        candidates = [m for m in candidates
                      if min_t <= int(m.get("tier", 3)) <= max_t]
    if not candidates:
        return ""
    # 按 tier 升序（优先更低 tier）
    candidates.sort(key=lambda m: int(m.get("tier", 3)))
    if preferred_model:
        for m in candidates:
            if m["name"] == preferred_model:
                return m["name"]
    return candidates[0]["name"]


def _to_resolved_key(rec: KeyRecord, model: str) -> ResolvedKey:
    return ResolvedKey(
        key_id=rec.id,
        api_key=rec.key,
        base_url=rec.base_url,
        model=model,
        models=[m["name"] for m in rec.models if m.get("enabled", True)],
        label=rec.label,
        privacy_warning=rec.privacy_warning,
        max_concurrency=rec.max_concurrency,
        source="unified_json",
        vision_params=_normalize_vision_params(rec.vision),
    )


def resolved_key_from_record(rec: KeyRecord, model: str) -> ResolvedKey:
    """公有包装：从 KeyRecord 构建 ResolvedKey（3-12）。

    供跨模块（如 vl.remote_vl 的 status 链路）调用，避免依赖私有
    _to_resolved_key 被重构后静默破坏。逻辑不搬，仅暴露稳定入口。
    """
    return _to_resolved_key(rec, model)


def resolve_keys(use_case: str, tier=None) -> list[ResolvedKey]:
    """统一路由入口：按 use_case + tier 筛选可用 key

    tier 参数（向后兼容旧调用方）：
        - int (1-5): 精确匹配该 tier
        - str "default"/"cheap"/"powerful": 旧名称，自动转换为 3/2/5
        - tuple/list [min, max]: 范围匹配
        - None: 使用 use_case.default_tier（范围）

    筛选链：
        1. enabled == True
        2. use_case 的 scope 要求在某个 model 的 scope 中（model 级 scope）
        3. status.works != False
        4. allowed_uses 为空（无限制）或包含 use_case
        5. use_case.sensitive + key.privacy_warning 非空 → 跳过
           （除非 config [llm.privacy] allow_privacy_warning_for_sensitive=true 放行）
        6. tier 范围匹配：key 必须含至少一个 tier 在范围内的 model（同 scope）
    """
    uc = USE_CASE_REGISTRY.get(use_case)
    if uc is None:
        logger.warning("resolve_keys: unknown use_case '%s'", use_case)
        return []

    # tier 解析为范围：显式传入 > use_case.default_tier
    resolved_range = _normalize_tier_range(tier)
    if resolved_range == (0, 0):
        resolved_range = uc.default_tier

    # 读取隐私放行开关：true 时跳过 sensitive+privacy_warning 过滤
    # v14: 使用 types._get_allow_privacy_warning 共用方法，与 use_case_eligible 一致
    from server.llm_pool.types import _get_allow_privacy_warning
    allow_privacy_warning = _get_allow_privacy_warning()

    records = _load_keys_cached()
    matched: list[ResolvedKey] = []

    for rec in records:
        if not rec.enabled:
            continue
        # scope 过滤：key 必须含至少一个 uc.scope 的 model
        has_scope_model = any(
            m.get("enabled", True) and uc.scope in m.get("scope", ["llm"])
            for m in rec.models
        )
        if not has_scope_model:
            continue
        if rec.status.get("works") is False:
            continue
        if rec.allowed_uses and use_case not in rec.allowed_uses:
            continue
        if uc.sensitive and rec.privacy_warning and not allow_privacy_warning:
            continue

        # tier 范围匹配：在 uc.scope 候选中必须有 tier 在范围内的 model
        chosen_model = _pick_model_for_tier(rec, resolved_range, uc.scope)
        if not chosen_model:
            continue
        rk = _to_resolved_key(rec, chosen_model)
        matched.append(rk)

    return matched


def resolve_key(use_case: str, tier=None) -> ResolvedKey | None:
    """便捷函数：取第一个匹配的 key（替代旧 get_key_for_use）

    tier 参数同 resolve_keys（int/str/None）。
    """
    keys = resolve_keys(use_case, tier)
    return keys[0] if keys else None


# =====================================================================
# CRUD
# =====================================================================


def load_keys() -> list[KeyRecord]:
    """公开 API：加载所有 key record（带缓存）"""
    return _load_keys_cached()


def get_key_by_id(key_id: str) -> KeyRecord | None:
    """按 id 查找"""
    for rec in _load_keys_cached():
        if rec.id == key_id:
            return rec
    return None


def _gen_key_id(label: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in label.lower())[:32] or "key"
    return f"{slug}_{uuid.uuid4().hex[:8]}"


def add_key(record: KeyRecord) -> str:
    """添加新 key record，返回新 id"""
    with _keys_file_lock:
        records_raw, wrapper = _read_raw_keys_file()
        if wrapper is None:
            wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(), "keys": records_raw}
            wrapper = _maybe_migrate(wrapper)
            records_raw = wrapper.get("keys", [])

        if not record.id:
            record.id = _gen_key_id(record.label)

        records_raw.append(_to_dict(record))
        _write_raw_keys_file(records_raw, wrapper)
        _invalidate_cache()
        return record.id


def update_key(key_id: str, updates: dict) -> KeyRecord | None:
    """部分更新 key record，返回更新后的 record"""
    with _keys_file_lock:
        records_raw, wrapper = _read_raw_keys_file()
        if wrapper is None:
            wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(), "keys": records_raw}

        for rec in records_raw:
            if rec.get("id") == key_id:
                # 字段白名单（v9：含 vision 段；v11：含 protocol；v12：含 pool_key；v14：删 limits + 加 pool）
                for f in ["label", "key", "base_url", "models",
                          "max_concurrency", "privacy_warning", "group",
                          "enabled", "allowed_uses", "vision", "protocol",
                          "pool_key", "pool"]:
                    if f in updates and updates[f] is not None:
                        rec[f] = updates[f]
                rec.pop("scope", None)   # 强制清理 v5 残留
                rec.pop("vendor", None)
                rec.pop("model", None)   # v4 残留
                _write_raw_keys_file(records_raw, wrapper)
                _invalidate_cache()
                return _to_record(rec)
        return None


def delete_key(key_id: str) -> bool:
    """删除 key record"""
    with _keys_file_lock:
        records_raw, wrapper = _read_raw_keys_file()
        if wrapper is None:
            wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(), "keys": records_raw}

        before = len(records_raw)
        records_raw = [r for r in records_raw if r.get("id") != key_id]
        after = len(records_raw)
        if after == before:
            return False
        _write_raw_keys_file(records_raw, wrapper)
        _invalidate_cache()
        return True


def save_keys(records: list[KeyRecord]) -> None:
    """全量保存（覆盖写）"""
    with _keys_file_lock:
        wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(),
                   "keys": [_to_dict(r) for r in records]}
        _write_raw_keys_file(wrapper["keys"], wrapper)
        _invalidate_cache()


# =====================================================================
# 健康检查（按物理 key 去重）
# =====================================================================


HEALTH_CHECK_INTERVAL_DAYS = 10
HEALTH_CHECK_MAX_FAILS = 3


def _test_key(api_key: str, base_url: str, model: str, timeout: int = 30,
              protocol: str = "openai") -> dict:
    """测试单个 key+端点+模型是否可用

    v11：增加 protocol 参数；anthropic 协议用 POST /v1/messages 测试
    T07：同步 requests 改 httpx.Client（连接池复用）；check_health 由 async 端点
    经 asyncio.to_thread 调用，本函数运行在线程池中，不阻塞事件循环。
    """
    from lib.async_http import get_sync_client
    from server.llm_pool.opencode import build_opencode_headers, derive_session_id
    # v11：规范化 protocol
    proto = (protocol or "openai").lower().strip()
    if proto not in ("openai", "anthropic"):
        proto = "openai"

    # opencode 端点健康检查也要带会话亲和 header（2026-09-06 上游强制，缺失即 400
    # MissingSessionID，会把健康 key 误标 REMOVED）。健康检查消息恒为"回复OK"，
    # 用 key 指纹做 namespace 派生稳定会话 ID（不泄露 key 本体，仅本地参与哈希）。
    oc_headers = build_opencode_headers(
        base_url, derive_session_id([{"role": "user", "content": "回复OK"}],
                                    namespace=f"healthcheck:{api_key[:12]}"))

    try:
        if proto == "anthropic":
            # anthropic 协议：POST /v1/messages（base_url 含 /v1 时只追加 /messages）
            url = base_url.rstrip("/")
            url = url + "/messages" if url.endswith("/v1") else url + "/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                **oc_headers,
            }
            payload = {
                "model": model or "claude-3-5-haiku-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "回复OK"}],
            }
            resp = get_sync_client().post(url, headers=headers, json=payload, timeout=timeout)
        else:
            # OpenAI 兼容协议：POST /chat/completions
            resp = get_sync_client().post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    **oc_headers,
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "回复OK"}],
                    "max_tokens": 10,
                    "temperature": 0.1,
                    "stream": False,
                },
                timeout=timeout,
            )
        if resp.status_code == 200:
            return {"works": True, "status": 200, "model": model}
        # v11：anthropic 协议 400/429 也视为可连通
        if proto == "anthropic" and resp.status_code in (400, 429):
            return {"works": True, "status": resp.status_code, "model": model}
        return {
            "works": False,
            "status": resp.status_code,
            "error": resp.text[:200],
        }
    except Exception as e:
        return {"works": False, "status": -1, "error": str(e)[:150]}


def check_health(max_workers: int = 10) -> dict:
    """对所有物理 key 做健康检查（按 (key, base_url) 去重，每物理 key 只测一次）

    - 成功: fail_count 重置为 0
    - 失败: fail_count += 1
    - fail_count >= 3: 标记为 works=false
    更新 last_health_check 时间戳。

    5-3 重构：锁内只做「读快照 + 提取去重待测列表」和「应用结果 + 写回」两段，
    网络测试在锁外执行。此前全程持有 _keys_file_lock 跑 ThreadPoolExecutor
    （每 key timeout=30s），每 12h 自动健康检查期间 _load_keys_cached /
    resolve_keys / add_key / update_key 等消费者全部被阻塞几十秒。
    写回前重新读最新文件快照应用结果，配合 mtime 比对防覆盖并发修改。
    """
    # ---- 阶段 1：持锁读快照 + 提取去重待测列表，随即释放锁 ----
    with _keys_file_lock:
        records_raw, _wrapper = _read_raw_keys_file()
        snapshot_mtime = _keys_file_mtime()

        # 按 (key, base_url) 去重，每物理 key 取一个测试 model
        # v8：优先选 LLM scope model（AIGC model 无法用 /chat/completions 测试）
        # v11：保留 protocol 字段
        # 注意：只提取纯数据（str）出锁，不持有 record 引用
        unique: dict[tuple, dict] = {}
        for rec in records_raw:
            dedup = (rec.get("key", ""), rec.get("base_url", "").rstrip("/"))
            if dedup in unique:
                continue
            models = rec.get("models") or []
            # 默认测试 model：取第一个 LLM scope model 名，找不到则空字符串（_test_key 会处理）
            test_model = ""
            if models and isinstance(models[0], dict):
                llm_models = [m for m in models if "llm" in m.get("scope", ["llm"])]
                test_model = (llm_models[0].get("name", "") if llm_models else models[0].get("name", ""))
            # v11：读取 protocol
            protocol = str(rec.get("protocol", "openai") or "openai").lower().strip()
            if protocol not in ("openai", "anthropic"):
                protocol = "openai"
            unique[dedup] = {"key": rec.get("key", ""),
                             "base_url": rec.get("base_url", ""),
                             "test_model": test_model,
                             "protocol": protocol}

    # ---- 阶段 2：锁外网络测试（不阻塞 key_store 消费者） ----
    def _check_one(item: dict) -> dict:
        result = _test_key(item["key"], item["base_url"],
                           item["test_model"], protocol=item.get("protocol", "openai"))
        is_ok = result.get("works") or result.get("status") == 429
        return {"key": item["key"], "base_url": item["base_url"],
                "is_ok": is_ok, "result": result}

    results_map: dict[tuple, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_one, item): dedup for dedup, item in unique.items()}
        for fut in as_completed(futures):
            dedup = futures[fut]
            try:
                results_map[dedup] = fut.result()
            except Exception as e:
                results_map[dedup] = {"is_ok": False, "result": {"error": str(e)[:100]}}

    # ---- 阶段 3：重新持锁，基于最新文件快照应用结果并写回 ----
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with _keys_file_lock:
        records_raw, wrapper = _read_raw_keys_file()
        if _keys_file_mtime() != snapshot_mtime:
            logger.info(
                "keys.json 在健康检查期间被并发修改（mtime 变化），基于最新快照应用健康检查结果")
        if wrapper is None:
            wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(), "keys": records_raw}

        # 应用结果到所有被测 record（同物理 key 多条记录共享 status）。
        # 检查期间新写入的 record（不在 results_map 中）跳过，保持原 status 不动。
        ok_count = 0
        fail_count = 0
        removed_count = 0
        details = []
        for rec in records_raw:
            dedup = (rec.get("key", ""), rec.get("base_url", "").rstrip("/"))
            if dedup not in results_map:
                continue
            r = results_map[dedup]
            is_ok = r["is_ok"]
            result = r["result"]

            st = rec.get("status", {})
            if not isinstance(st, dict):
                st = {}
                rec["status"] = st
            old_fail = st.get("fail_count", 0)

            if is_ok:
                st["fail_count"] = 0
                st["works"] = True
                ok_count += 1
                status = "ok"
            else:
                new_fail = old_fail + 1
                st["fail_count"] = new_fail
                if new_fail >= HEALTH_CHECK_MAX_FAILS:
                    st["works"] = False
                    removed_count += 1
                    status = f"REMOVED (fail={new_fail})"
                else:
                    fail_count += 1
                    status = f"fail={new_fail}/{HEALTH_CHECK_MAX_FAILS}"

            st["last_health_check"] = now_str
            st["last_check_status"] = status
            st["last_check_detail"] = result.get("error", "")[:100] if not is_ok else ""

            details.append({
                "name": rec.get("label", rec.get("id", "")),
                "status": status,
                "fail_count": st["fail_count"],
                "works": st["works"],
            })

        _write_raw_keys_file(records_raw, wrapper)
        _invalidate_cache()

        return {
            "total": len(unique),
            "ok": ok_count,
            "fail": fail_count,
            "removed": removed_count,
            "checked_at": now_str,
            "details": details,
        }


def cleanup_expired(backup: bool = True) -> dict:
    """物理删除 status.works=false 的 record（删除前自动备份）"""
    with _keys_file_lock:
        path = _get_unified_keys_path()
        records_raw, wrapper = _read_raw_keys_file()
        if wrapper is None:
            wrapper = {"version": SCHEMA_VERSION, "updated_at": date.today().isoformat(), "keys": records_raw}

        before = len(records_raw)
        new_records = [r for r in records_raw
                       if r.get("status", {}).get("works", True) is not False]
        after = len(new_records)

        backup_path = None
        if backup and before > after and path.exists():
            try:
                ts = time.strftime("%Y%m%d-%H%M%S")
                backup_path = path.with_name(f"{path.name}.cleanup-backup-{ts}")
                backup_path.write_bytes(path.read_bytes())
                # 修剪旧备份（保留最近 5 个）
                backups = sorted(path.parent.glob(f"{path.name}.cleanup-backup-*"))
                for old in backups[:-5]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Failed to create cleanup backup: %s", e)

        _write_raw_keys_file(new_records, wrapper)
        _invalidate_cache()
        return {"before": before, "after": after, "removed": before - after, "backup": str(backup_path) if backup_path else None}


def should_run_health_check(interval_days: int = HEALTH_CHECK_INTERVAL_DAYS) -> bool:
    """检查是否需要运行健康检查（距上次检查超过 interval_days 天）"""
    records = _load_keys_cached()
    if not records:
        return False
    oldest_check = ""
    for rec in records:
        lc = rec.status.get("last_health_check", "")
        if not lc:
            return True
        if not oldest_check or lc < oldest_check:
            oldest_check = lc
    try:
        last_ts = time.mktime(time.strptime(oldest_check, "%Y-%m-%d %H:%M:%S"))
        elapsed_days = (time.time() - last_ts) / 86400
        return elapsed_days >= interval_days
    except Exception:
        return True


# =====================================================================
# Pool 适配
# =====================================================================


def _pool_dict_to_policy(pool: dict, name: str = "key") -> "ProviderPolicy | None":
    """从 keys.json 的 pool 段构造 ProviderPolicy（v14 新增）

    pool 为空字典时返回 None（表示用全局默认）。
    pool 非空时按字段构造 ProviderPolicy，缺失字段用 ProviderPolicy 默认值。
    """
    if not pool or not isinstance(pool, dict):
        return None
    from server.llm_pool.types import ProviderPolicy
    return ProviderPolicy(
        name=name,
        retry_count=int(pool.get("retry_count", 4)),
        retry_base_delay=float(pool.get("retry_base_delay", 2.0)),
        retry_max_delay=float(pool.get("retry_max_delay", 30.0)),
        retry_jitter=float(pool.get("retry_jitter", 0.2)),
        rate_limit_cooldown_seconds=float(pool.get("rate_limit_cooldown_seconds", 5.0)),
        rate_limit_backoff_multiplier=float(pool.get("rate_limit_backoff_multiplier", 2.0)),
        rate_limit_max_cooldown=float(pool.get("rate_limit_max_cooldown", 300.0)),
    )


def load_llm_keys_for_pool() -> list:
    """加载所有含 LLM scope model 的 key 为 LLMKey（供 LLMPool 初始化）

    跳过 enabled=False / status.works=False / 无 LLM scope model 的 record。
    每个 LLMKey 携带 model_tiers 字典（model_name → tier），供 pool._acquire 做 tier 硬匹配。

    v11 新增：携带 protocol（连接协议）和 model_display_names（model_name → display_name）。
    v14 新增：携带 pool_policy（per-key 重试/限流策略，从 rec.pool 段加载；空则 None = 用全局默认）。
    """
    from server.llm_pool.types import LLMKey

    records = _load_keys_cached()
    keys: list[LLMKey] = []
    for rec in records:
        if not rec.enabled:
            continue
        # v8: 收集 LLM scope model 及其 tier
        llm_models: list[str] = []
        model_tiers: dict[str, int] = {}
        model_display_names: dict[str, str] = {}  # v11：model_name → display_name
        for m in rec.models:
            if m.get("enabled", True) and "llm" in m.get("scope", ["llm"]):
                name = m["name"]
                llm_models.append(name)
                model_tiers[name] = int(m.get("tier", 3))
                # v11：收集 display_name（只保留非空值）
                dn = m.get("display_name")
                if dn and isinstance(dn, str) and dn.strip():
                    model_display_names[name] = dn.strip()
        if not llm_models:
            continue
        if rec.status.get("works") is False:
            continue
        # v14：从 rec.pool 段构造 per-key ProviderPolicy（空则 None = 用全局默认）
        pool_policy = _pool_dict_to_policy(rec.pool, name=rec.label or rec.id)
        keys.append(LLMKey(
            key=rec.key,
            base_url=rec.base_url,
            models=llm_models,
            name=rec.label or rec.id,
            max_concurrency=rec.max_concurrency,
            privacy_warning=rec.privacy_warning,
            allowed_uses=list(rec.allowed_uses),
            key_id=rec.id,
            default_model=llm_models[0],
            model_tiers=model_tiers,
            protocol=rec.protocol,  # v11：连接协议
            model_display_names=model_display_names,  # v11：展示名映射
            pool_key=rec.pool_key,  # v12：上游池去重标识（OmniRoute pool-deduped）
            pool_policy=pool_policy,  # v14：per-key pool 策略
        ))
    return keys


# =====================================================================
# Provider policy 辅助（v12 从 key_manager.py 合并；v14 删除 _load_provider_policy）
# =====================================================================


def _load_default_policy() -> "ProviderPolicy":
    """加载全局默认 pool 策略（v14 新增）

    从 config.toml [llm.pool] 段读取。无配置时返回 ProviderPolicy(name="default") 默认值。
    v14: 替代已删除的 _load_provider_policy（原从 [llm.providers.<name>.pool] 读取）。
    """
    cfg = {}
    try:
        from server.config import get_pool_defaults
        cfg = get_pool_defaults()
    except Exception:
        cfg = {}
    return ProviderPolicy(
        name="default",
        retry_count=int(cfg.get("retry_count", 4)),
        retry_base_delay=float(cfg.get("retry_base_delay", 2.0)),
        retry_max_delay=float(cfg.get("retry_max_delay", 30.0)),
        retry_jitter=float(cfg.get("retry_jitter", 0.2)),
        rate_limit_cooldown_seconds=float(cfg.get("rate_limit_cooldown_seconds", 5.0)),
        rate_limit_backoff_multiplier=float(cfg.get("rate_limit_backoff_multiplier", 2.0)),
        rate_limit_max_cooldown=float(cfg.get("rate_limit_max_cooldown", 300.0)),
    )


def _read_text_lines(filepath: Path) -> list[str]:
    """读取文本文件所有行（兼容 UTF-8/UTF-8 BOM/GB18030/GBK 编码）"""
    if not filepath.exists():
        return []
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            return filepath.read_text(encoding=enc).splitlines()
        except Exception:
            continue
    return []


def _get_status_dict(item: dict) -> dict:
    """从 key 记录提取 status 字典（兼容 v4 schema 和旧扁平格式）

    v4 schema: {"key":..., "status": {"works": true, ...}} → 返回 item["status"]
    旧扁平格式: {"key":..., "works": true, ...} → 返回 item 本身
    """
    if "status" in item and isinstance(item["status"], dict):
        return item["status"]
    return item


def _extract_key_records_from_file(filepath: Path) -> list[dict]:
    """从 key 文件提取 key 记录，兼容纯文本和 JSON tested 文件

    - JSON 格式：{"keys":[...]} 或 [...] → 提取含 "key" 字段的 dict 列表
    - 纯文本格式：每行一个 key（sk-/tp- 开头），返回 [{"key": "..."}]
    """
    if not filepath.exists():
        return []
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        if isinstance(data, list):
            records = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip()
                if not key:
                    continue
                records.append(item)
            if records:
                return records
    except Exception:
        pass

    records = []
    for line in _read_text_lines(filepath):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower() in {"pro", "plan", "月度", "pro版"}:
            continue
        if s.startswith("tp-") or s.startswith("sk-"):
            records.append({"key": s})
    return records


def _collect_key_records_from_files(filepaths: list[str]) -> list[dict]:
    """从多个 key 文件收集记录并去重（按 key 值）"""
    seen: set[str] = set()
    records: list[dict] = []
    for fp in filepaths:
        for record in _extract_key_records_from_file(Path(fp)):
            key = record.get("key", "")
            if key and key not in seen:
                seen.add(key)
                records.append(record)
    return records


def _load_keys_records(filepath) -> tuple[list[dict], dict]:
    """从 JSON 文件加载 key 记录（兼容 v4 schema 和扁平数组）

    返回 (records, wrapper)。wrapper 为 {"version": ...} 等元字段或空 dict。
    非 JSON 文件抛出异常（调用方应 catch）。
    """
    data = json.loads(Path(filepath).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "keys" in data and isinstance(data["keys"], list):
        wrapper = {k: v for k, v in data.items() if k != "keys"}
        return data["keys"], wrapper
    if isinstance(data, list):
        return data, {}
    raise ValueError(f"无法识别的 key 文件格式: {filepath}")
