"""健康度算法纯函数 — 多维加权评分

实现 spec「健康度算法」节定义的多维加权公式，纯函数无 UI 依赖，便于：
1. OverviewPage（Ticket 05）调它算分
2. 验收脚本（spec Proof 节「健康度算法可计算」命令）复用同一函数算分（避免算法漂移）

公式：
- 维度 1：过时率 × 40%        — stale_total / facts, > 0.2 → red else yellow
- 维度 2：嵌入可用性 × 20%    — embedding_ready=False → red
- 维度 3：待处理堆积 × 15%    — pending / 100, > 50 触发，< 100 yellow else red
- 维度 4：维护新鲜度 × 15%    — last_run 为 None 或距今超 interval_hours → 触发 yellow
- 维度 5：DB 增长率 × 10%     — db_size_history 长度 ≥ 2 时算周增长率，> 20% 触发 yellow

降级：缺 db_size_history 或长度 < 2 时维度 5 跳过，其他 4 维权重重新归一化
（40% → 44.4% / 20% → 22.2% / 15% → 16.7% / 15% → 16.7%）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_hours_ago(last_run: Any) -> float | None:
    """解析 last_run（ISO 字符串）为距今小时数。解析失败返回 None。"""
    if not isinstance(last_run, str):
        return None
    try:
        # ISO 格式，如 "2026-08-18T14:30:00" 或 "2026-08-18T14:30:00.123456"
        dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = now - dt
        return max(0.0, delta.total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None


def calculate_health_score(status: dict) -> tuple[int, str, list[dict]]:
    """计算记忆系统健康度评分。

    Args:
        status: GET /memory/status 返回的 dict（含 facts / embedding_ready /
            pending_messages / maintainer / db_size_history 等字段）。

    Returns:
        (score 0-100, level 'green'|'yellow'|'red', concerns list)
        concerns 每条: {dimension, severity, message, jump_to}
        - dimension: 维度名（中文，如「过时率」）
        - severity: 'red' | 'yellow'（红需立即关注，黄需关注）
        - message: 描述（中文）
        - jump_to: 跳转目标（如 'memory_list?stale=true' / 'overview' / 'timeline'）

    降级逻辑：
        - 缺 db_size_history 或长度 < 2 → 维度 5 跳过，其他 4 维权重按 0.90 归一化
        - 缺 maintainer 整段 → 维度 1（stale_total）+ 维度 4（last_run）按缺字段处理
        - 缺 facts（=0）→ 维度 1 stale_rate 按 stale_total 是否 > 0 二值处理
    """
    if not isinstance(status, dict):
        # 防御性：非 dict 视为满分（不应发生，但避免崩溃）
        return 100, "green", []

    # 判断维度 5 是否参与（缺 db_size_history 或长度 < 2 时跳过 + 权重归一化）
    raw_history = status.get("db_size_history") or []
    history: list[dict] = raw_history if isinstance(raw_history, list) else []
    has_db_dim = len(history) >= 2

    # 权重归一化
    if has_db_dim:
        w_stale = 0.40
        w_embedding = 0.20
        w_pending = 0.15
        w_maint = 0.15
        w_db_growth = 0.10
    else:
        # 维度 5 跳过，其他 4 维权重 / (1 - 0.10) = / 0.90
        w_stale = 0.40 / 0.90       # ≈ 0.4444
        w_embedding = 0.20 / 0.90   # ≈ 0.2222
        w_pending = 0.15 / 0.90     # ≈ 0.1667
        w_maint = 0.15 / 0.90       # ≈ 0.1667
        w_db_growth = 0.0

    score = 100.0
    concerns: list[dict] = []

    # —— 维度 1：过时率 × 40% / 44.4% ——
    maint = status.get("maintainer") or {}
    if not isinstance(maint, dict):
        maint = {}
    stale_total = maint.get("stale_total", 0) or 0
    facts = status.get("facts", 0) or 0
    # facts=0 时：有 stale_total（数据异常）→ 满扣；无 stale_total → 不扣
    stale_rate = min(stale_total / facts, 1.0) if facts > 0 else 1.0 if stale_total > 0 else 0.0
    score -= stale_rate * 100 * w_stale
    if stale_total > 0:
        concerns.append({
            "dimension": "过时率",
            "severity": "red" if stale_rate > 0.2 else "yellow",
            "message": f"{stale_total} 条过时记忆（占总事实 {stale_rate * 100:.1f}%）",
            "jump_to": "memory_list?stale=true",
        })

    # —— 维度 2：嵌入可用性 × 20% / 22.2% ——
    embedding_ready = bool(status.get("embedding_ready", False))
    if not embedding_ready:
        score -= 100 * w_embedding
        concerns.append({
            "dimension": "嵌入可用性",
            "severity": "red",
            "message": "Embedding 不可用，语义搜索将降级",
            "jump_to": "overview",
        })

    # —— 维度 3：待处理堆积 × 15% / 16.7%（阈值 100 条）——
    pending = status.get("pending_messages", 0) or 0
    pending_rate = min(pending / 100, 1.0)
    score -= pending_rate * 100 * w_pending
    if pending > 50:
        concerns.append({
            "dimension": "待处理堆积",
            "severity": "yellow" if pending < 100 else "red",
            "message": f"{pending} 条消息待处理（阈值 100）",
            "jump_to": "timeline",
        })

    # —— 维度 4：维护新鲜度 × 15% / 16.7% ——
    interval_hours = maint.get("interval_hours", 24) or 24
    last_run = maint.get("last_run")
    if last_run is None:
        score -= 100 * w_maint
        concerns.append({
            "dimension": "维护新鲜度",
            "severity": "yellow",
            "message": "维护器从未运行",
            "jump_to": "overview",
        })
    else:
        hours_ago = _parse_hours_ago(last_run)
        if hours_ago is None:
            # 解析失败 → 满扣（数据异常，需关注）
            score -= 100 * w_maint
            concerns.append({
                "dimension": "维护新鲜度",
                "severity": "yellow",
                "message": f"维护器上次运行时间无法解析: {last_run}",
                "jump_to": "overview",
            })
        elif hours_ago > interval_hours:
            # 超时 → 按超时比例扣分（线性，最大满扣）
            overdue_ratio = min(hours_ago / interval_hours, 2.0) / 2.0
            score -= overdue_ratio * 100 * w_maint
            concerns.append({
                "dimension": "维护新鲜度",
                "severity": "yellow",
                "message": (
                    f"维护器已 {int(hours_ago)} 小时未运行"
                    f"（间隔 {interval_hours} 小时）"
                ),
                "jump_to": "overview",
            })

    # —— 维度 5：DB 增长率 × 10%（仅 has_db_dim）——
    if has_db_dim:
        try:
            latest = float(history[-1].get("size_mb", 0) or 0)
            week_ago = float(history[0].get("size_mb", 0) or 0)
            if week_ago > 0:
                growth = (latest - week_ago) / week_ago
                if growth > 0.2:  # 周增长 > 20%
                    # penalty 按 spec 公式：min(growth * 100 * 0.1, 100 * 0.1)
                    penalty = min(growth * 100 * w_db_growth, 100 * w_db_growth)
                    score -= penalty
                    concerns.append({
                        "dimension": "DB 增长率",
                        "severity": "yellow",
                        "message": f"DB 周增长 {growth * 100:.1f}%（阈值 20%）",
                        "jump_to": "overview",
                    })
        except (TypeError, IndexError, ValueError, KeyError):
            # history 数据结构异常 → 跳过该维度（不扣分不告警）
            pass

    # 收尾：clamp + level
    score_int = max(0, min(100, int(round(score))))
    if score_int >= 80:
        level = "green"
    elif score_int >= 60:
        level = "yellow"
    else:
        level = "red"

    return score_int, level, concerns


__all__ = ["calculate_health_score"]
