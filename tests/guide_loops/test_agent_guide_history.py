"""agent_guide 调用历史记录测试。

覆盖：
- _record_usage 写入 history.json
- FIFO 砍尾保留最近 HISTORY_MAX_SIZE (50) 条
- 持久化（重启后端后可读）
- context 和 strong_match 字段记录
"""

from __future__ import annotations

import json

import pytest

from server.agent_guide import (
    HISTORY_MAX_SIZE,
    _history,
    _record_usage,
)


@pytest.fixture(autouse=True)
def isolate_history(tmp_path, monkeypatch):
    """每个测试用独立 history.json，避免污染真实数据。

    autouse=True 确保所有测试自动隔离。
    """
    # 重置模块级状态
    import server.agent_guide as ag

    fake_file = tmp_path / "history.json"
    monkeypatch.setattr(ag, "HISTORY_FILE", fake_file)
    monkeypatch.setattr(ag, "_history_loaded", False)
    # 原地清空 _history list（不重新绑定，保持外部引用有效）
    ag._history.clear()
    yield fake_file
    # 清理：重置状态供下一个测试
    ag._history.clear()
    ag._history_loaded = False


class TestRecordHistory:
    """_record_usage 写入历史记录。"""

    def test_single_record(self, isolate_history):
        """单次调用应写入一条记录。"""
        _record_usage("task", "adhoc.web_archive", "爬一下文章", context="tabs.json", strong_match=True)

        assert len(_history) == 1
        r = _history[0]
        assert r["mode"] == "task"
        assert r["task"] == "爬一下文章"
        assert r["task_type"] == "adhoc.web_archive"
        assert r["context"] == "tabs.json"
        assert r["strong_match"] is True
        assert "ts" in r

    def test_null_fields(self, isolate_history):
        """无参调用应记录 null 字段。"""
        _record_usage("general", None, None, context=None, strong_match=None)

        assert len(_history) == 1
        r = _history[0]
        assert r["task"] == ""
        assert r["task_type"] is None
        assert r["context"] is None
        assert r["strong_match"] is None

    def test_task_truncation(self, isolate_history):
        """超长 task query 应截断到 200 字符。"""
        long_task = "x" * 500
        _record_usage("task", None, long_task)

        assert len(_history) == 1
        assert len(_history[0]["task"]) == 200

    def test_context_truncation(self, isolate_history):
        """超长 context 应截断到 100 字符。"""
        long_ctx = "y" * 300
        _record_usage("task", None, "test", context=long_ctx)

        assert len(_history[0]["context"]) == 100


class TestFIFOCap:
    """FIFO 砍尾：保留最近 HISTORY_MAX_SIZE 条。"""

    def test_cap_at_max_size(self, isolate_history):
        """超过 HISTORY_MAX_SIZE 条时砍最旧。"""
        # 写入 HISTORY_MAX_SIZE + 10 条
        for i in range(HISTORY_MAX_SIZE + 10):
            _record_usage("task", None, f"query_{i}")

        assert len(_history) == HISTORY_MAX_SIZE
        # 最旧的应该是 query_10（前 10 条被砍）
        assert _history[0]["task"] == f"query_{10}"
        # 最新的是 query_{HISTORY_MAX_SIZE + 9}
        assert _history[-1]["task"] == f"query_{HISTORY_MAX_SIZE + 9}"

    def test_exactly_max_size_no_cap(self, isolate_history):
        """正好 HISTORY_MAX_SIZE 条时不砍。"""
        for i in range(HISTORY_MAX_SIZE):
            _record_usage("task", None, f"query_{i}")

        assert len(_history) == HISTORY_MAX_SIZE
        assert _history[0]["task"] == "query_0"

    def test_fifo_order_preserved(self, isolate_history):
        """FIFO 顺序：先写的在前，后写的在后。"""
        for i in range(5):
            _record_usage("task", None, f"q{i}")

        tasks = [r["task"] for r in _history]
        assert tasks == ["q0", "q1", "q2", "q3", "q4"]


class TestPersistence:
    """持久化：写入文件，重启后可读。"""

    def test_file_written(self, isolate_history):
        """记录应写入 history.json 文件。"""
        _record_usage("task", "adhoc.web_archive", "爬一下文章", strong_match=True)

        # 等异步写入完成（threading.Thread daemon）
        import time
        time.sleep(0.1)

        assert isolate_history.exists()
        data = json.loads(isolate_history.read_text(encoding="utf-8"))
        assert data["max_size"] == HISTORY_MAX_SIZE
        assert len(data["records"]) == 1
        assert data["records"][0]["task"] == "爬一下文章"
        assert data["records"][0]["strong_match"] is True

    def test_reload_after_restart(self, isolate_history):
        """重启后端（重新加载模块）应能读到历史记录。"""
        _record_usage("task", "adhoc.web_archive", "query1")

        import time
        time.sleep(0.1)

        # 模拟重启：重置 _history_loaded 并重新加载
        import server.agent_guide as ag
        ag._history.clear()
        ag._history_loaded = False
        ag._ensure_history_loaded()

        assert len(_history) == 1
        assert _history[0]["task"] == "query1"

    def test_corrupt_file_handled(self, isolate_history):
        """损坏的 history.json 应被忽略，不报错。"""
        isolate_history.write_text("not valid json {{{", encoding="utf-8")

        import server.agent_guide as ag
        ag._history_loaded = False
        ag._ensure_history_loaded()

        # 损坏文件应被忽略，_history 为空
        assert len(_history) == 0
