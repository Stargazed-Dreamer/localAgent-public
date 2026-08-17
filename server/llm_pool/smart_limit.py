"""智能 VL 配额上限调整建议器（冗余模块，只读模式）

读取 data/activity/vl_429_log.jsonl 分析最近 N 天的非并发 429 模式：
- 若多天意外触发 daily_exhausted（每日额度耗尽），
  推送 inbox 建议降低 vl_daily_quota 配置。
- 不会自动调整 vl_quota 的 effective_limit（保持只读模式）
- 不会写入 config.toml（用户决策后手动改）

设计约束：
- 冗余模块，不绑进 VL 核心调用链（不 import 进 remote_vl 的热路径）
- 只读 + inbox 推送，零副作用（除 inbox 写入）
- 失败不影响主流程

触发：通过 Loop 任务 smart_limit_check 每日运行（见 loop_actions.py）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("localagent.smart_limit")


def _project_root() -> Path:
    """项目根目录（server/llm_pool/smart_limit.py → 上 3 级）"""
    return Path(__file__).resolve().parent.parent.parent


def _resolve(p: str | Path) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else _project_root() / pp


def _read_429_log(log_path: Path, days: int = 7) -> list[dict]:
    """读取最近 N 天的 429 日志"""
    if not log_path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    records: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts = r.get("ts", "")
                    if ts:
                        dt = datetime.fromisoformat(ts)
                        if dt >= cutoff:
                            records.append(r)
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception as e:
        logger.warning(f"读取 429 日志失败 {log_path}: {e}")
    return records


def analyze_429_pattern(
    log_path: str | Path = "data/activity/vl_429_log.jsonl",
    days: int = 7,
    current_quota: int | None = None,
) -> dict:
    """分析最近 N 天的 429 日志模式

    Args:
        log_path: vl_429_log.jsonl 路径
        days: 回看天数（默认 7）
        current_quota: 当前 vl_daily_quota 配置值（用于计算建议值）
                       None 时用保守默认 100

    Returns:
        {
            "total_429": int,
            "concurrent_429": int,
            "daily_exhausted_count": int,  # 非并发 429 次数
            "daily_exhausted_days": list[str],  # 触发过的日期 YYYYMMDD
            "suggestion": str | None,  # 建议文案，无建议时为 None
            "suggested_quota": int | None,  # 建议的新配额
            "analyzed_days": int,
            "log_records": int,
        }
    """
    log_path = _resolve(log_path)
    records = _read_429_log(log_path, days=days)

    concurrent_count = 0
    daily_exhausted_count = 0
    daily_exhausted_dates: set[str] = set()

    for r in records:
        if r.get("is_concurrent"):
            concurrent_count += 1
        else:
            daily_exhausted_count += 1
            ts = r.get("ts", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    daily_exhausted_dates.add(dt.strftime("%Y%m%d"))
                except ValueError:
                    pass

    # 建议规则：
    # - 过去 N 天内 >= 2 天触发 daily_exhausted → 建议下调配额
    # - 过去 N 天内 >= 4 天触发 daily_exhausted → 强烈建议下调
    # ModelScope 文档：每日额度最高 200，实际可远小于 200
    suggestion: str | None = None
    suggested_quota: int | None = None
    if len(daily_exhausted_dates) >= 2:
        base = current_quota if current_quota and current_quota > 0 else 100
        # 每次建议下调 20%，至少 20（避免过小）
        suggested_quota = max(20, int(base * 0.8))
        severity = "强烈建议" if len(daily_exhausted_dates) >= 4 else "建议"
        suggestion = (
            f"{severity}降低 vl_daily_quota 到 {suggested_quota}："
            f"过去 {days} 天内有 {len(daily_exhausted_dates)} 天触发非并发 429"
            f"（daily_exhausted），说明 ModelScope 实际每日额度可能低于配置值。"
            f"日期: {sorted(daily_exhausted_dates)}"
        )

    return {
        "total_429": len(records),
        "concurrent_429": concurrent_count,
        "daily_exhausted_count": daily_exhausted_count,
        "daily_exhausted_days": sorted(daily_exhausted_dates),
        "suggestion": suggestion,
        "suggested_quota": suggested_quota,
        "analyzed_days": days,
        "log_records": len(records),
    }


def push_suggestion_to_inbox(analysis: dict) -> bool:
    """推送建议到 inbox（如果有建议）

    Returns:
        True 表示已推送，False 表示无建议或推送失败
    """
    suggestion = analysis.get("suggestion")
    if not suggestion:
        return False
    try:
        from server.inbox import get_store
        get_store().create({
            "source": "smart_limit",
            "category": "vl_quota_suggestion",
            "title": "VL 配额建议：考虑下调每日限额",
            "description": suggestion,
            "payload": {
                "suggested_quota": analysis.get("suggested_quota"),
                "daily_exhausted_days": analysis.get("daily_exhausted_days", []),
                "analyzed_days": analysis.get("analyzed_days"),
                "log_records": analysis.get("log_records"),
            },
        })
        return True
    except Exception as e:
        logger.warning(f"推送 inbox 失败: {e}")
        return False


def run_check(
    log_path: str | Path = "data/activity/vl_429_log.jsonl",
    days: int = 7,
    current_quota: int | None = None,
) -> dict:
    """主入口：分析 + 推送（除 inbox 外无副作用）

    Args:
        log_path: 429 日志路径
        days: 回看天数
        current_quota: 当前配额（用于计算建议值）

    Returns:
        {"analysis": dict, "inbox_pushed": bool}
    """
    analysis = analyze_429_pattern(log_path=log_path, days=days, current_quota=current_quota)
    pushed = push_suggestion_to_inbox(analysis)
    if pushed:
        logger.info(
            "smart_limit 推送建议到 inbox: %s 天内 %d 天 daily_exhausted",
            days, len(analysis.get("daily_exhausted_days", [])),
        )
    else:
        logger.debug(
            "smart_limit 无建议（%d 天内 %d 天 daily_exhausted）",
            days, len(analysis.get("daily_exhausted_days", [])),
        )
    return {"analysis": analysis, "inbox_pushed": pushed}
