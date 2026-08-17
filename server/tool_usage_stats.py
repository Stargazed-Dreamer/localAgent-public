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
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("localagent.tool_usage_stats")

_STATS_FILE = Path(__file__).parent.parent / "data" / "tool_usage_stats.json"
_LOCK = threading.Lock()


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

    Args:
        code: 用户代码（只统计长度，不存内容）
        environment: localagent / workspace_venv / system
        cwd: 工作目录（空字符串表示未指定）
        elapsed_ms: 耗时（毫秒，0=未统计/未完成）
    """
    try:
        with _LOCK:
            stats = _load_stats()
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
            _save_stats(stats)
    except Exception as e:
        logger.warning(f"record_exec_python_call 失败: {e}")


def get_stats() -> dict:
    """获取当前统计数据（供审计用）。"""
    with _LOCK:
        return _load_stats()
