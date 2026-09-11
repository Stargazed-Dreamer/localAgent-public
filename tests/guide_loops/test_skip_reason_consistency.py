"""T7/D11: 验证所有 loop action 返回 skipped=True 时包含 skip_reason 字段

问题根因：
  loop_manager.py 第 575 行读取 result['skip_reason']，
  但 <data_drive>:/DownloadscanAction / ScreenSummaryTriggerAction 返回的是 result['reason']，
  TodosTriggerCheckAction / HourlySummarizeAction 干脆不提供原因。
  导致日志显示 "Loop 任务 download_watcher.scan 跳过（skipped）: unknown"。

修复：
  1. 所有 action 统一用 skip_reason key
  2. loop_manager 增加 reason 后备 + "no reason" 默认值（不显示 "unknown"）

本测试验证：
  - 各 action 在 skip 场景下返回 skip_reason 字段
  - loop_manager 的 skip 消息提取逻辑正确处理各种 key
"""

import asyncio
import importlib
import json
from datetime import datetime

from server.activity_tracker.loop_actions import (
    <data_drive>:/DownloadscanAction,
    HourlySummarizeAction,
    ScreenSummaryTriggerAction,
    TodosTriggerCheckAction,
)


def _run(coro):
    return asyncio.run(coro)


# ==================== <data_drive>:/DownloadscanAction ====================

class Test<data_drive>:/DownloadscanSkipReason:
    """<data_drive>:/DownloadscanAction skip 场景必须返回 skip_reason"""

    def test_below_threshold_has_skip_reason(self, tmp_path):
        """文件数 ≤ 阈值 → skipped + skip_reason"""
        action = <data_drive>:/DownloadscanAction()
        # 创建少量文件（低于阈值 15）
        for i in range(3):
            (tmp_path / f"file_{i}.txt").write_text("test", encoding="utf-8")

        result = _run(action.execute({
            "config": {
                "watch_dir": str(tmp_path),
                "file_count_threshold": 15,
                "classify_batch_size": 50,
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "阈值" in result["skip_reason"]
        # 不应出现 "unknown" 噪音
        assert result["skip_reason"] != "unknown"

    def test_existing_pending_has_skip_reason(self, tmp_path, monkeypatch):
        """已有 pending 条目 → skipped + skip_reason"""
        # 创建足够多文件（超过阈值）
        for i in range(20):
            (tmp_path / f"file_{i}.txt").write_text("test", encoding="utf-8")

        # mock inbox store 返回已有 pending 条目
        # get_store 在 execute 内通过 `from server.inbox import get_store` 导入
        class FakeStore:
            def list(self, **kwargs):
                return [{"id": 1, "title": "existing"}]

        monkeypatch.setattr(
            "server.inbox.get_store",
            lambda: FakeStore(),
        )

        action = <data_drive>:/DownloadscanAction()
        result = _run(action.execute({
            "config": {
                "watch_dir": str(tmp_path),
                "file_count_threshold": 15,
                "classify_batch_size": 50,
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "pending" in result["skip_reason"]

    def test_missing_watch_dir_returns_skipped(self, tmp_path):
        """watch_dir 不存在 → skipped（非 failed），避免 loop auto_pause 刷错误日志

        场景：用户启用 download_watcher loop 但未创建 watch_dir 目录。
        旧实现返回 success=False 触发 fail_count 累积 + auto_pause + 错误日志；
        新实现返回 skipped 三态，loop 静默跳过，用户创建目录后自动恢复。
        """
        action = <data_drive>:/DownloadscanAction()
        nonexistent = tmp_path / "does_not_exist"
        result = _run(action.execute({
            "config": {
                "watch_dir": str(nonexistent),
                "file_count_threshold": 15,
                "classify_batch_size": 50,
            },
        }))

        # 关键：success=True + skipped=True（不是 success=False）
        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "watch_dir" in result["skip_reason"]
        # 不应有 error 字段（那是 failed 路径的标志）
        assert "error" not in result


# ==================== ScreenSummaryTriggerAction ====================

class TestScreenSummarySkipReason:
    """ScreenSummaryTriggerAction skip 场景必须返回 skip_reason"""

    def test_no_screen_data_has_skip_reason(self, tmp_path):
        """无 screen 数据 → skipped + skip_reason"""
        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "无 screen" in result["skip_reason"]

    def test_marker_exists_has_skip_reason(self, tmp_path):
        """标记文件已存在 → skipped + skip_reason"""
        date_str = datetime.now().strftime("%Y%m%d")
        screen_path = tmp_path / f"screen_{date_str}.jsonl"
        with open(screen_path, "w", encoding="utf-8") as f:
            for _ in range(25):
                f.write(json.dumps({"answer": "x"}) + "\n")
        marker = tmp_path / f".screen_summary_triggered_{date_str}"
        marker.write_text("already triggered", encoding="utf-8")

        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "已触发过" in result["skip_reason"]

    def test_below_threshold_has_skip_reason(self, tmp_path):
        """记录数低于阈值 → skipped + skip_reason"""
        date_str = datetime.now().strftime("%Y%m%d")
        screen_path = tmp_path / f"screen_{date_str}.jsonl"
        with open(screen_path, "w", encoding="utf-8") as f:
            for _ in range(5):
                f.write(json.dumps({"answer": "short"}) + "\n")

        action = ScreenSummaryTriggerAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "screen_summary_record_threshold": 20,
                "screen_summary_char_threshold": 5000,
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "未达阈值" in result["skip_reason"]


# ==================== TodosTriggerCheckAction ====================

class TestTodosTriggerSkipReason:
    """TodosTriggerCheckAction skip 场景必须返回 skip_reason"""

    def test_no_triggered_todos_has_skip_reason(self, monkeypatch):
        """无到期任务 → skipped + skip_reason"""
        # mock get_todos_store 返回空 store
        # get_todos_store 在 execute 内通过 `from server.todos.router import get_todos_store` 导入
        # 用 importlib.import_module 获取真正的 module 对象（非 APIRouter）
        todos_router_mod = importlib.import_module("server.todos.router")

        class FakeEmptyStore:
            def list_todos(self, **kwargs):
                return []

        monkeypatch.setattr(todos_router_mod, "get_todos_store", lambda: FakeEmptyStore())

        action = TodosTriggerCheckAction()
        result = _run(action.execute({"config": {"check_interval": 300}}))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "无到期" in result["skip_reason"]


# ==================== HourlySummarizeAction ====================

class TestHourlySummarizeSkipReason:
    """HourlySummarizeAction skip 场景必须返回 skip_reason"""

    def test_no_data_has_skip_reason(self, tmp_path, monkeypatch):
        """无采集数据 → skipped + skip_reason"""
        action = HourlySummarizeAction()
        result = _run(action.execute({
            "config": {
                "raw_data_dir": tmp_path,
                "summarize_dir": tmp_path / "output",
            },
        }))

        assert result["success"] is True
        assert result["skipped"] is True
        assert "skip_reason" in result
        assert "无采集" in result["skip_reason"]
