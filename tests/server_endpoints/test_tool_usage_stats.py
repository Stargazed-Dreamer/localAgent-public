"""T04: 参数使用率统计测试（spec D10）。"""
from pathlib import Path

from server.tool_usage_stats import (
    _bucket_code_length,
    get_stats,
    record_exec_python_call,
)


def test_bucket_code_length():
    """代码长度分桶正确。"""
    assert _bucket_code_length(50) == "<100"
    assert _bucket_code_length(99) == "<100"
    assert _bucket_code_length(100) == "100-1k"
    assert _bucket_code_length(999) == "100-1k"
    assert _bucket_code_length(1000) == "1k-10k"
    assert _bucket_code_length(9999) == "1k-10k"
    assert _bucket_code_length(10000) == ">10k"
    assert _bucket_code_length(100000) == ">10k"


def test_record_exec_python_call(tmp_path, monkeypatch):
    """记录 exec_python 调用，统计数据正确落盘。"""
    # 重定向 stats 文件到临时目录
    stats_file = tmp_path / "tool_usage_stats.json"
    monkeypatch.setattr("server.tool_usage_stats._STATS_FILE", stats_file)

    # 第一次调用
    record_exec_python_call(
        code="print('hello')",  # 14 chars → <100
        environment="localagent",
        cwd="",
    )
    # 第二次调用
    record_exec_python_call(
        code="x = 1\n" * 200,  # 1400 chars → 1k-10k
        environment="workspace_venv",
        cwd="/some/path",
        elapsed_ms=500,
    )

    # 验证落盘数据
    stats = get_stats()
    assert "exec_python" in stats
    entry = stats["exec_python"]
    assert entry["call_count"] == 2
    assert entry["code_length_buckets"]["<100"] == 1
    assert entry["code_length_buckets"]["1k-10k"] == 1
    assert entry["environment"]["localagent"] == 1
    assert entry["environment"]["workspace_venv"] == 1
    assert entry["has_cwd_false"] == 1
    assert entry["has_cwd_true"] == 1
    assert entry["total_elapsed_ms"] == 500
    assert entry["elapsed_count"] == 1
    assert entry["last_updated"] != ""


def test_record_failure_no_crash(tmp_path, monkeypatch):
    """统计失败不抛异常（best-effort）。"""
    # 指向一个不可写的路径
    bad_path = Path("Z:/nonexistent_drive_xyz/tool_usage_stats.json")
    monkeypatch.setattr("server.tool_usage_stats._STATS_FILE", bad_path)

    # 不应抛异常
    record_exec_python_call(
        code="print(1)",
        environment="localagent",
        cwd="",
    )
