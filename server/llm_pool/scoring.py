"""LLM 池 Auto 评分路由模块（OmniRoute Phase 3.3 借鉴）

实现 ADR-0001 的 6 因子加权评分路由：
  - health          0.25  近 N 次成功率
  - quota_remaining 0.25  1 - effective_saturation
  - latency_p95_inv 0.20  p95 延迟倒数（越低延迟分越高）
  - cost_inv        0.10  tier 反比映射（tier 1=1.0 最便宜）
  - tier_match      0.10  USE_CASE_REGISTRY tier 范围命中
  - lkgp_bonus      0.10  LKGP 会话粘性命中

精简自 OmniRoute 12 因子版本（去掉本地不适用或暂未实现的 caveman_state /
provider_health / retry_count_remaining / request_priority / cache_hit /
semantic_similarity）。

设计原则：
- 所有因子归一化到 [0, 1]，加权求和得综合分（0.0-1.0）
- 无数据时给中性分 0.5（避免冷启动期评分失真）
- Fail-Open：任何异常返回 0.5（中性分），不阻塞主选路
- 评分只在"已通过基础过滤的可用 key"间做加权选择，不替代 v12/v13 的分层限制
  （OPEN 熔断器 / expired / cooldown / model lockout / max_concurrency 满仍直接过滤）
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.llm_pool.types import LLMKey

# 6 因子权重（与 ADR-0001 一致，总和 = 1.0）
WEIGHT_HEALTH = 0.25
WEIGHT_QUOTA = 0.25
WEIGHT_LATENCY = 0.20
WEIGHT_COST = 0.10
WEIGHT_TIER_MATCH = 0.10
WEIGHT_LKGP = 0.10

# tier → cost_inv 映射（tier 1 = 1.0 最便宜/最优，tier 5 = 0.2 最贵）
# 取匹配 tier 范围内的最低 tier 作为 cost 代表
_TIER_COST_INV: dict[int, float] = {
    1: 1.0,
    2: 0.8,
    3: 0.6,
    4: 0.4,
    5: 0.2,
}

# 中性分（无数据或异常时使用）
_NEUTRAL = 0.5


def _factor_health(key: LLMKey) -> float:
    """health 因子：近 N 次成功率

    =(ok) / (ok + fail + rate_limited)
    无数据时返回 0.5（中性）
    """
    total = key.stats.ok + key.stats.fail + key.stats.rate_limited
    if total == 0:
        return _NEUTRAL
    return key.stats.ok / total


def _factor_quota_remaining(key: LLMKey, now: float) -> float:
    """quota_remaining 因子：1 - effective_saturation

    saturation ∈ [0, 1]，0=充裕，1=耗尽
    quota_remaining 越高越好（key 配额余量充足）
    """
    try:
        sat = key.effective_saturation(now)
        # 钳制到 [0, 1] 防 saturation 溢出
        sat = max(0.0, min(1.0, sat))
        return 1.0 - sat
    except Exception:
        return _NEUTRAL


def _factor_latency_p95_inv(key: LLMKey) -> float:
    """latency_p95_inv 因子：p95 延迟倒数归一化

    归一化公式：1 / (1 + p95_ms / 1000)
    - p95=0ms    → 1.0（瞬时）
    - p95=1000ms → 0.5
    - p95=4000ms → 0.2
    无数据时返回 0.5（中性）
    """
    try:
        p95 = key.stats.p95()
        if p95 is None or p95 <= 0:
            return _NEUTRAL
        return 1.0 / (1.0 + p95 / 1000.0)
    except Exception:
        return _NEUTRAL


def _factor_cost_inv(key: LLMKey, resolved_tier_range: tuple[int, int]) -> float:
    """cost_inv 因子：tier 反比映射

    取 key 中匹配 resolved_tier_range 的最低 tier（最便宜可用 model）
    tier 1 → 1.0（最便宜/最优），tier 5 → 0.2（最贵）
    无匹配 model 时返回 0.5（中性）
    """
    if resolved_tier_range == (0, 0):
        return _NEUTRAL
    try:
        t_low, t_high = resolved_tier_range
        matching_tiers = []
        for model_name in key.models:
            tier = key.model_tiers.get(model_name)
            if tier is not None and t_low <= tier <= t_high:
                matching_tiers.append(tier)
        if not matching_tiers:
            return _NEUTRAL
        min_tier = min(matching_tiers)
        return _TIER_COST_INV.get(min_tier, _NEUTRAL)
    except Exception:
        return _NEUTRAL


def _factor_tier_match(
    key: LLMKey,
    use_case: str | None,
    resolved_tier_range: tuple[int, int],
) -> float:
    """tier_match 因子：USE_CASE_REGISTRY tier 范围命中

    若 use_case 指定且其 default_tier 与 key 的 model_tiers 有交集 → 1.0
    否则 → 0.0
    若 use_case 未指定 → 0.5（中性，无偏好）
    """
    if not use_case:
        return _NEUTRAL
    try:
        from server.llm_pool.types import USE_CASE_REGISTRY
        uc = USE_CASE_REGISTRY.get(use_case)
        if uc is None:
            return _NEUTRAL
        # use_case 的 default_tier 范围
        uc_low, uc_high = uc.default_tier
        # 调用方传入的 resolved_tier_range（可能不同，取交集判断）
        if resolved_tier_range != (0, 0):
            r_low, r_high = resolved_tier_range
            # 取两个范围的交集
            inter_low = max(uc_low, r_low)
            inter_high = min(uc_high, r_high)
            if inter_low > inter_high:
                return 0.0  # 范围无交集
            check_low, check_high = inter_low, inter_high
        else:
            check_low, check_high = uc_low, uc_high
        # 检查 key 的 model_tiers 是否有 tier 落在范围内
        for model_name in key.models:
            tier = key.model_tiers.get(model_name)
            if tier is not None and check_low <= tier <= check_high:
                return 1.0
        return 0.0
    except Exception:
        return _NEUTRAL


def _factor_lkgp_bonus(
    key: LLMKey,
    session_id: str | None,
    sticky_key_id: str | None,
) -> float:
    """lkgp_bonus 因子：LKGP 会话粘性命中

    若 session_id 提供且 sticky 记录的 key_id 匹配此 key → 1.0
    否则 → 0.0
    """
    if not session_id or not sticky_key_id:
        return 0.0
    try:
        return 1.0 if key.key_id == sticky_key_id else 0.0
    except Exception:
        return 0.0


def score_key(
    key: LLMKey,
    *,
    use_case: str | None = None,
    resolved_tier_range: tuple[int, int] = (0, 0),
    session_id: str | None = None,
    sticky_key_id: str | None = None,
    now: float | None = None,
) -> float:
    """计算 key 的综合评分（0.0-1.0）

    实现 ADR-0001 的 6 因子加权评分。所有因子归一化到 [0, 1]，加权求和。

    Args:
        key: LLMKey 实例
        use_case: 当前调用的 use_case（影响 tier_match）
        resolved_tier_range: 调用方传入的 tier 范围（影响 cost_inv 和 tier_match）
        session_id: LKGP 会话 ID（影响 lkgp_bonus）
        sticky_key_id: 当前 session 的粘性 key_id（影响 lkgp_bonus）
        now: 当前时间戳（用于 effective_saturation TTL 检查）

    Returns:
        0.0-1.0 的综合评分，越高越优先
        Fail-Open：任何异常返回 0.5（中性分），不阻塞主选路
    """
    try:
        ts = now if now is not None else time.time()
        health = _factor_health(key)
        quota = _factor_quota_remaining(key, ts)
        latency = _factor_latency_p95_inv(key)
        cost = _factor_cost_inv(key, resolved_tier_range)
        tier_m = _factor_tier_match(key, use_case, resolved_tier_range)
        lkgp = _factor_lkgp_bonus(key, session_id, sticky_key_id)

        return (
            WEIGHT_HEALTH * health
            + WEIGHT_QUOTA * quota
            + WEIGHT_LATENCY * latency
            + WEIGHT_COST * cost
            + WEIGHT_TIER_MATCH * tier_m
            + WEIGHT_LKGP * lkgp
        )
    except Exception:
        # Fail-Open: 评分失败给中性分，让 round-robin 兜底
        return _NEUTRAL


def score_keys(
    keys: list[LLMKey],
    *,
    use_case: str | None = None,
    resolved_tier_range: tuple[int, int] = (0, 0),
    session_id: str | None = None,
    sticky_key_id: str | None = None,
    now: float | None = None,
) -> list[tuple[LLMKey, float]]:
    """批量计算 key 评分

    Returns:
        list of (key, score) tuples，按 score 降序排序（最高分在最前）
    """
    ts = now if now is not None else time.time()
    scored = [
        (k, score_key(
            k,
            use_case=use_case,
            resolved_tier_range=resolved_tier_range,
            session_id=session_id,
            sticky_key_id=sticky_key_id,
            now=ts,
        ))
        for k in keys
    ]
    # 稳定排序：按 score 降序
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# 模式包：影响评分权重的预设配置
# ADR-0001 默认用 balanced；其他 mode_pack 是 future 扩展占位（暂未实现差异化权重）
MODE_PACKS: dict[str, dict[str, float]] = {
    "balanced": {
        "health": WEIGHT_HEALTH,
        "quota": WEIGHT_QUOTA,
        "latency": WEIGHT_LATENCY,
        "cost": WEIGHT_COST,
        "tier_match": WEIGHT_TIER_MATCH,
        "lkgp": WEIGHT_LKGP,
    },
    # 以下 mode_pack 暂未实现差异化权重，保留接口供 future 扩展
    "fast": {},       # 偏向 latency（待实现）
    "quality": {},    # 偏向 health + tier_match（待实现）
    "cheap": {},      # 偏向 cost（待实现）
    "offline": {},    # 偏向 quota_remaining（待实现）
}
