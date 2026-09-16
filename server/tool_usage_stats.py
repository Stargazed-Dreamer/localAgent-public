"""T04: exec_python 参数使用率统计（spec D10）。

简单落盘到 data/tool_usage_stats.json，无面板。
后续想起来审计时能知道有，能看到统计数据即可。

统计维度：
- tool_name: exec_python（当前只统计 exec_python）
- call_count: 总调用次数
- code_length_buckets: 代码长度分桶（<100, 100-1k, 1k-10k, >10k）
- environment: 使用的 environment 分布（localagent/workspace_venv/system）
- has_cwd: 是否指定了 cwd
- avg_elapsed_ms: 平均耗时（done 状态）

数据结构（data/tool_usage_stats.json）：
{
  "exec_python": {
    "call_count": 1232,
    "code_length_buckets": {"<100": 500, "100-1k": 600, "1k-10k": 120, ">10k": 12},
    "environment": {"localagent": 1200, "workspace_venv": 30, "system": 2},
    "has_cwd": true_count: 800,
    "has_cwd": false_count: 432,
    "last_updated": "2026-08-01T21:00:00"
  }
}
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("localagent.tool_usage_stats")

_STATS_FILE = Path(__file__).parent.parent / "data" / "tool_usage_stats.json"
_LOCK = threading.Lock()

# 6-11: 节流落盘——原实现每次 exec_python 调用同步读+写整个 stats JSON（热路径双 IO）。
# 现内存缓存 + dirty 计数，每 20 次调用或距上次落盘超 60s 才写盘（参照 vl_quota
# 批量写盘模式）。代价：进程退出丢最近最多 60s/19 次统计，对使用率统计可接受。
_stats_cache: dict | None = None
_dirty_since_save = 0
_last_save_monotonic = 0.0
_SAVE_EVERY_N_CALLS = 20
_SAVE_INTERVAL_S = 60.0


def _load_stats() -> dict:
    """加载现有统计数据（文件不存在返回空 dict）。"""
    if not _STATS_FILE.exists():
        return {}
    try:
        with _STATS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载 tool_usage_stats 失败: {e}")
        return {}


def _get_stats_cached() -> dict:
    """带内存缓存的加载（6-11：避免每次调用都读盘）。调用方须持 _LOCK。"""
    global _stats_cache
    if _stats_cache is None:
        _stats_cache = _load_stats()
    return _stats_cache


def _save_stats(stats: dict) -> None:
    """保存统计数据到文件。"""
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _STATS_FILE.open("w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 tool_usage_stats 失败: {e}")


def _bucket_code_length(length: int) -> str:
    """代码长度分桶。"""
    if length < 100:
        return "<100"
    if length < 1000:
        return "100-1k"
    if length < 10000:
        return "1k-10k"
    return ">10k"


def record_exec_python_call(
    code: str,
    environment: str,
    cwd: str,
    elapsed_ms: float = 0,
) -> None:
    """记录一次 exec_python 调用（spec D10）。

    线程安全（用 threading.Lock）。失败不抛异常（best-effort）。
    节流落盘（6-11）：更新只进内存缓存，每 20 次调用或距上次落盘超 60s 才写盘。

    Args:
        code: 用户代码（只统计长度，不存内容）
        environment: localagent / workspace_venv / system
        cwd: 工作目录（空字符串表示未指定）
        elapsed_ms: 耗时（毫秒，0=未统计/未完成）
    """
    global _dirty_since_save, _last_save_monotonic
    try:
        with _LOCK:
            stats = _get_stats_cached()
            entry = stats.setdefault("exec_python", {
                "call_count": 0,
                "code_length_buckets": {"<100": 0, "100-1k": 0, "1k-10k": 0, ">10k": 0},
                "environment": {},
                "has_cwd_true": 0,
                "has_cwd_false": 0,
                "total_elapsed_ms": 0,
                "elapsed_count": 0,
                "last_updated": "",
            })

            entry["call_count"] += 1

            # 代码长度分桶
            bucket = _bucket_code_length(len(code))
            entry["code_length_buckets"][bucket] = entry["code_length_buckets"].get(bucket, 0) + 1

            # environment 分布
            entry["environment"][environment] = entry["environment"].get(environment, 0) + 1

            # cwd 使用情况
            if cwd:
                entry["has_cwd_true"] += 1
            else:
                entry["has_cwd_false"] += 1

            # 耗时统计
            if elapsed_ms > 0:
                entry["total_elapsed_ms"] += elapsed_ms
                entry["elapsed_count"] += 1

            entry["last_updated"] = datetime.now().isoformat(timespec="seconds")
            # 6-11: 节流落盘（dirty 计数 + 时间间隔，参照 vl_quota._persist_batch 模式）
            _dirty_since_save += 1
            now = time.monotonic()
            if (
                _dirty_since_save >= _SAVE_EVERY_N_CALLS
                or (now - _last_save_monotonic) >= _SAVE_INTERVAL_S
            ):
                _save_stats(stats)
                _dirty_since_save = 0
                _last_save_monotonic = now
    except Exception as e:
        logger.warning(f"record_exec_python_call 失败: {e}")


def get_stats() -> dict:
    """获取当前统计数据（供审计用）。

    6-11: 返回内存缓存快照（含尚未落盘的节流增量），JSON round-trip 深拷贝
    防调用方修改污染缓存。
    """
    with _LOCK:
        return json.loads(json.dumps(_get_stats_cached()))
